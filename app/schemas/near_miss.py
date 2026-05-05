from datetime import datetime

from pydantic import BaseModel, Field

from app.models.near_miss import NearMissStatus
from app.models.observation import Severity


class NearMissCreate(BaseModel):
    plantId: str
    areaId: str | None = None
    location: str | None = None
    date: datetime
    description: str = Field(min_length=10)
    activity: str | None = None
    potentialSeverity: Severity
    potentialConsequence: str  # CSV string from form, kept for back-compat


class NearMissUpdate(BaseModel):
    actionOwnerId: str | None = None
    correctiveActions: str | None = None
    rootCauseCategory: str | None = None
    rootCauseDetail: str | None = None
    targetDate: datetime | None = None


class NearMissOut(BaseModel):
    id: str
    number: str
    date: datetime
    plantId: str
    areaId: str | None
    location: str | None
    reporterId: str
    actionOwnerId: str | None
    description: str
    activity: str | None
    potentialSeverity: Severity
    potentialConsequence: str
    rootCauseCategory: str | None
    rootCauseDetail: str | None
    correctiveActions: str | None
    targetDate: datetime | None
    closedAt: datetime | None
    promotedToIncidentId: str | None
    status: NearMissStatus
    createdAt: datetime
    updatedAt: datetime

    model_config = {"from_attributes": True}
