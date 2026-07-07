"""Safety Culture Management — request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


# ── §3 Leadership walks ──────────────────────────────────────────────────────
class LeadershipWalkCreate(_Base):
    plantId: str
    leaderId: str
    scheduledDate: datetime
    areaVisited: str | None = None
    cadence: str | None = None  # WEEKLY | MONTHLY (recurring intent)
    notes: str | None = None


class LeadershipWalkComplete(_Base):
    completedDate: datetime | None = None
    areaVisited: str | None = None
    workersInteracted: int = 0
    observationsRaised: int = 0
    hazardsIdentified: int = 0
    notes: str | None = None
    followUpActionIds: list[str] = Field(default_factory=list)


# ── §2 BBS closure loop ──────────────────────────────────────────────────────
class ObservationLinkAction(_Base):
    linkedCapaId: str | None = None
    linkedActionId: str | None = None
    # If true and no CAPA is linked yet, spawn a SAFETY_CULTURE CAPA for this obs.
    spawnCapa: bool = False


class ObservationVerifyClosure(_Base):
    reobservationDate: datetime | None = None
    verified: bool = True


# ── §4 Perception surveys ────────────────────────────────────────────────────
class SurveyQuestion(_Base):
    id: str
    text: str
    dimension: str  # TrustInReporting|PsychologicalSafety|ManagementCommitment|PeerAccountability
    scaleType: str = "likert5"


class SurveyTemplateCreate(_Base):
    name: str
    description: str | None = None
    industryVertical: str | None = None
    cadence: str = "QUARTERLY"
    questions: list[SurveyQuestion]


class SurveyAnswer(_Base):
    questionId: str
    score: int = Field(ge=1, le=5)


class SurveyResponseSubmit(_Base):
    plantId: str
    period: str  # e.g. 2026-Q3
    responses: list[SurveyAnswer]


# ── §6 Recognition ───────────────────────────────────────────────────────────
class RecognitionAwardRequest(_Base):
    plantId: str
    period: str | None = None  # defaults to current month
