"""Guided Field Capture — request/response schemas.

The wizard's payload mirrors the spec's document shape but flattened for
Postgres columns. Everything optional except the idempotency key, type and
location plant — no free-text field is ever mandatory (spec 1.1.1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SubmissionType = Literal["observation", "near_miss", "unsafe_condition", "incident", "ptw", "flra"]
SelfSeverity = Literal["low", "medium", "high"]


class LocationIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    areaId: str | None = None
    mapPinX: float | None = Field(default=None, ge=0, le=100)  # % of site layout image
    mapPinY: float | None = Field(default=None, ge=0, le=100)
    equipmentId: str | None = None
    qrScanned: bool = False


class CategoryIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    l1Id: str | None = None
    l2Id: str | None = None
    # offline clients may only know stable codes (taxonomy cache) — the server
    # resolves codes → ids, following TaxonomyAlias for stale caches.
    l1Code: str | None = None
    l2Code: str | None = None
    aiSuggested: bool = False
    aiConfidence: float | None = None


class VoiceIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    langCode: str | None = None
    # on-device Web Speech transcript, when the browser produced one
    transcriptOriginal: str | None = None
    clientMediaId: str | None = None  # links to the VOICE attachment


class CaptureMetaIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tapCount: int | None = Field(default=None, ge=0, le=500)
    durationMs: int | None = Field(default=None, ge=0)
    offline: bool = False
    appVersion: str | None = None
    deviceLang: str | None = None


class SubmissionCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    clientSubmissionId: str = Field(min_length=8, max_length=64)
    type: SubmissionType = "observation"
    plantId: str | None = None  # defaults to the reporter's plant
    anonymous: bool = False
    location: LocationIn = Field(default_factory=LocationIn)
    category: CategoryIn | None = None
    severity: SelfSeverity = "medium"
    description: str | None = None
    voice: VoiceIn | None = None
    capture: CaptureMetaIn | None = None
    createdAtClient: datetime | None = None
    taxonomyVersion: int | None = None


class TriageBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hiraLikelihood: int = Field(ge=1, le=5)
    hiraSeverity: int = Field(ge=1, le=5)
    note: str | None = None


class ConvertBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target: Literal["observation", "near_miss", "incident", "ptw", "flra"]
    # officer can override/complete the narrative before conversion; when
    # absent a description is synthesised from category labels + transcript.
    description: str | None = None
    # incident conversions must classify the initial type (existing Phase-1 contract)
    incidentType: str | None = None

    # ── PTW conversion: the authorisation-chain fields a field technician
    # cannot supply — the officer completes them at triage (spec §8.2). ──
    permitType: str | None = None
    validFrom: datetime | None = None
    validTo: datetime | None = None
    issuerId: str | None = None
    receiverId: str | None = None

    # ── FLRA conversion: crew + toolbox-talk the officer supplies. ──
    teamMemberIds: list[str] = Field(default_factory=list)
    toolboxTalkById: str | None = None


class CleanupTextBody(BaseModel):
    """AI grammar/clarity cleanup request (spec §7a)."""
    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=4000)
    lang: str = "hi"


class SuggestCategoryBody(BaseModel):
    """Text → hazard category suggestion request (spec §7b)."""
    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=4000)
    lang: str = "hi"


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str = Field(min_length=3)
