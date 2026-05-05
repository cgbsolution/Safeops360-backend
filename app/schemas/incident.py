from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.incident import IncidentStatus, IncidentType


class IncidentCreate(BaseModel):
    type: IncidentType
    plantId: str
    areaId: str | None = None
    location: str
    date: datetime
    description: str = Field(min_length=10)
    injuredPersonName: str | None = None
    injuredPersonAge: int | None = None
    injuredPersonDesignation: str | None = None
    bodyPart: str | None = None
    natureOfInjury: str | None = None
    immediateCause: str | None = None
    rootCauseMethod: str | None = None
    rootCauseData: dict[str, Any] | None = None
    rootCauseDetail: str | None = None
    correctiveActions: str | None = None
    preventiveActions: str | None = None
    lostDays: int = 0
    propertyDamageCost: float | None = None
    investigationTeamIds: list[str] = []


class IncidentUpdate(BaseModel):
    immediateCause: str | None = None
    rootCauseMethod: str | None = None
    rootCauseData: dict[str, Any] | None = None
    rootCauseDetail: str | None = None
    correctiveActions: str | None = None
    preventiveActions: str | None = None
    lostDays: int | None = None
    propertyDamageCost: float | None = None
    investigationTeamIds: list[str] | None = None


class IncidentOut(BaseModel):
    id: str
    number: str
    date: datetime
    type: IncidentType
    plantId: str
    areaId: str | None
    location: str
    reporterId: str
    description: str
    injuredPersonName: str | None
    injuredPersonAge: int | None
    bodyPart: str | None
    natureOfInjury: str | None
    immediateCause: str | None
    rootCauseMethod: str | None
    rootCauseData: dict[str, Any] | None
    rootCauseSummary: str | None
    correctiveActions: str | None
    preventiveActions: str | None
    lostDays: int
    propertyDamageCost: float | None
    status: IncidentStatus
    closedAt: datetime | None
    createdAt: datetime
    updatedAt: datetime

    model_config = {"from_attributes": True}


# ─── Attachments ─────────────────────────────────────────────────────────


class AttachmentInit(BaseModel):
    phase: str = Field(pattern="^init$")
    category: str
    fileName: str
    fileSize: int = Field(gt=0)
    mimeType: str
    capaRef: str | None = None
    witnessRef: str | None = None


class AttachmentComplete(BaseModel):
    phase: str = Field(pattern="^complete$")
    attachmentId: str
    caption: str | None = None
    exifData: dict[str, Any] | None = None


class AttachmentUploader(BaseModel):
    id: str
    name: str
    designation: str | None = None

    model_config = {"from_attributes": True}


class AttachmentOut(BaseModel):
    id: str
    incidentId: str
    category: str
    fileName: str
    fileSize: int
    mimeType: str
    caption: str | None
    exifData: dict[str, Any] | None
    uploadedAt: datetime
    uploadedById: str
    uploadedBy: AttachmentUploader | None = None

    model_config = {"from_attributes": True}
