"""Thin wrapper around the Anthropic SDK.

All AI agents should call `complete_json()` here rather than touch the SDK
directly. The wrapper:
  • Returns `None` when the API key isn't configured (so calling code can
    log + fall through gracefully — never blocks the workflow).
  • Defaults to Haiku for cost; agents can override to Sonnet/Opus when
    quality matters more than latency.
  • Forces a JSON-shaped response by including the schema description in
    the prompt and parsing the model output. Production-grade tool-use
    forcing can replace this when needed.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from app.core.config import get_settings

_client = None


def _get_client():
    """Lazy-init the Anthropic client. Returns None when no API key is set."""
    global _client
    settings = get_settings()
    if not settings.anthropic_api_key:
        return None
    if _client is None:
        try:
            from anthropic import Anthropic  # noqa: PLC0415

            _client = Anthropic(api_key=settings.anthropic_api_key)
        except Exception as e:  # noqa: BLE001
            print(f"[ai] Failed to init Anthropic client: {e}", file=sys.stderr)
            return None
    return _client


def is_configured() -> bool:
    return get_settings().anthropic_api_key is not None


async def complete_json(
    *,
    system: str,
    user: str,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Synchronous Anthropic call wrapped in async-friendly shape.

    The Anthropic SDK is sync; for our low-volume agent calls (one per
    observation closure / submission) we're fine running it inline.
    Returns the parsed JSON dict, or None on any failure (key missing,
    API error, parse error). Callers MUST handle None.
    """
    client = _get_client()
    if client is None:
        return None
    settings = get_settings()
    used_model = model or settings.anthropic_model
    try:
        msg = client.messages.create(
            model=used_model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:  # noqa: BLE001
        print(f"[ai] Anthropic call failed (model={used_model}): {e}", file=sys.stderr)
        return None

    # Extract the text content. SDK returns a list of content blocks.
    parts: list[str] = []
    for block in msg.content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    raw = "".join(parts).strip()
    if not raw:
        return None

    # Try direct parse first; fall back to extracting the first {...} block.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        snippet = raw[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError as e:
            print(f"[ai] Failed to parse JSON from model output: {e}\n---\n{raw[:500]}", file=sys.stderr)
            return None
    return None
