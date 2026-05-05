from datetime import datetime

from pydantic import BaseModel, Field

from app.models.flra import FLRAStatus


class FLRACreate(BaseModel):
    permitId: str | None = None
    plantId: str
    date: datetime
    location: str
    jobDescription: str = Field(min_length=10)
    teamMemberIds: list[str] = Field(min_length=1)
    toolboxTalkById: str
    toolboxTalkConfirmed: bool = False
    hazards: str  # JSON-encoded list — kept as string for back-compat with Node payload


class FLRARedoRequest(BaseModel):
    reason: str = Field(min_length=5)


class CrewSignatureOut(BaseModel):
    id: str
    userId: str
    signed: bool
    signedAt: datetime | None
    trainingValidAtSignature: bool
    trainingExpiresAt: datetime | None

    model_config = {"from_attributes": True}


class FLRAOut(BaseModel):
    id: str
    number: str
    permitId: str | None
    plantId: str
    date: datetime
    location: str
    jobDescription: str
    leaderId: str
    hazards: str
    toolboxTalkById: str | None
    toolboxTalkConfirmed: bool
    status: FLRAStatus
    completedAt: datetime | None
    supersededById: str | None
    supersededReason: str | None
    createdAt: datetime
    updatedAt: datetime

    model_config = {"from_attributes": True}
