"""Guided Field Capture — AI provider abstractions (spec 1.2 screens 3+5).

Two seams, both tenant-feature-flagged and fail-soft:

* ``ITranscriptionProvider`` — voice-note → transcript. Ships with the STUB
  (store audio only, transcript stays null) exactly per spec; the platform's
  only AI vendor (Anthropic) does no speech-to-text, so a real provider is a
  drop-in for later (set ``CAPTURE_TRANSCRIPTION_PROVIDER`` when one exists).
  On-device Web Speech transcripts arrive separately from the client and are
  translated by the scheduler job regardless of this provider.

* ``IVisionSuggestProvider`` — photo → suggested hazard category + one-line
  draft description. The Anthropic implementation is real (Claude vision);
  the stub returns None so the wizard degrades to manual selection.

Every provider returns ``None`` on any failure — the flow never blocks on AI.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from typing import Any, Protocol, TypedDict

from app.services.ai.anthropic_client import _get_client, is_configured


class TranscriptResult(TypedDict):
    text: str
    languageCode: str | None


class VisionSuggestion(TypedDict):
    l1Code: str | None
    l2Code: str | None
    description: str  # one line, in the reporter's language
    descriptionEn: str
    confidence: float  # 0..1


# ── Transcription ─────────────────────────────────────────────────────────────
class ITranscriptionProvider(Protocol):
    name: str

    async def transcribe(
        self, audio: bytes, mime_type: str, lang_hint: str | None
    ) -> TranscriptResult | None: ...


class StubTranscriptionProvider:
    """Spec-mandated default: audio is stored, transcript stays null."""

    name = "stub"

    async def transcribe(
        self, audio: bytes, mime_type: str, lang_hint: str | None
    ) -> TranscriptResult | None:
        return None


def get_transcription_provider() -> ITranscriptionProvider:
    # Future providers register here keyed by env CAPTURE_TRANSCRIPTION_PROVIDER.
    return StubTranscriptionProvider()


# ── Vision suggest ────────────────────────────────────────────────────────────
_VISION_SYSTEM = """You are a factory-safety triage assistant for an Indian garment manufacturer.
You are shown a photo taken by a low-literacy field worker reporting a hazard,
plus the hazard taxonomy (level-1 and level-2 codes with English labels).
Respond ONLY with a JSON object:
{"l1Code": <best level-1 code or null>, "l2Code": <best child code of that l1 or null>,
 "description": <ONE short sentence describing the hazard in LANG>,
 "descriptionEn": <the same sentence in English>,
 "confidence": <0..1 — how sure you are about l1Code>}
If the photo shows no recognisable workplace hazard, use nulls and confidence 0."""

_VISION_MEDIA_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}


class IVisionSuggestProvider(Protocol):
    name: str

    async def suggest(
        self, image: bytes, mime_type: str, taxonomy: list[dict[str, Any]], lang: str
    ) -> VisionSuggestion | None: ...


class StubVisionProvider:
    name = "stub"

    async def suggest(
        self, image: bytes, mime_type: str, taxonomy: list[dict[str, Any]], lang: str
    ) -> VisionSuggestion | None:
        return None


class AnthropicVisionProvider:
    name = "anthropic"

    async def suggest(
        self, image: bytes, mime_type: str, taxonomy: list[dict[str, Any]], lang: str
    ) -> VisionSuggestion | None:
        client = _get_client()
        if client is None:
            return None
        media_type = "image/jpeg" if mime_type == "image/jpg" else mime_type
        if media_type not in _VISION_MEDIA_TYPES:
            return None
        if len(image) > 4.5 * 1024 * 1024:  # API image cap ~5MB base64-decoded
            return None
        from app.core.config import get_settings

        tax_lines = [
            {"code": n.get("code"), "parent": n.get("parentCode"), "label": (n.get("labels") or {}).get("en")}
            for n in taxonomy
        ]
        user_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(image).decode(),
                },
            },
            {
                "type": "text",
                "text": "Taxonomy:\n" + json.dumps(tax_lines, ensure_ascii=False) + f"\nLANG={lang}",
            },
        ]
        try:
            msg = await asyncio.to_thread(
                client.messages.create,
                model=get_settings().anthropic_model,
                max_tokens=400,
                temperature=0.1,
                system=_VISION_SYSTEM,
                messages=[{"role": "user", "content": user_content}],
            )
        except Exception as e:  # noqa: BLE001
            print(f"[ai] vision-suggest call failed: {e}", file=sys.stderr)
            return None
        parts = [getattr(b, "text", "") for b in (msg.content or [])]
        raw = "".join(p for p in parts if isinstance(p, str)).strip()
        try:
            start, end = raw.find("{"), raw.rfind("}")
            data = json.loads(raw[start : end + 1])
        except Exception:  # noqa: BLE001
            return None
        try:
            return VisionSuggestion(
                l1Code=data.get("l1Code"),
                l2Code=data.get("l2Code"),
                description=str(data.get("description") or ""),
                descriptionEn=str(data.get("descriptionEn") or ""),
                confidence=max(0.0, min(1.0, float(data.get("confidence") or 0))),
            )
        except Exception:  # noqa: BLE001
            return None


def get_vision_provider(enabled: bool) -> IVisionSuggestProvider:
    if enabled and is_configured():
        return AnthropicVisionProvider()
    return StubVisionProvider()


__all__ = [
    "TranscriptResult",
    "VisionSuggestion",
    "ITranscriptionProvider",
    "IVisionSuggestProvider",
    "StubTranscriptionProvider",
    "StubVisionProvider",
    "AnthropicVisionProvider",
    "get_transcription_provider",
    "get_vision_provider",
]
