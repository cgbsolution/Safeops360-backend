from datetime import datetime

from pydantic import BaseModel, Field

from app.models.permit import PermitStatus, PermitType


class PermitCreate(BaseModel):
    type: PermitType
    plantId: str
    areaId: str | None = None
    location: str = Field(min_length=1)
    scopeOfWork: str = Field(min_length=10)
    validFrom: datetime
    validTo: datetime
    issuerId: str
    receiverId: str
    contractorName: str | None = None
    isolationsRequired: str | None = None
    ppeChecklist: str | None = None
    gasTestRequired: bool = False
    gasTestResult: str | None = None
    o2Level: str | None = None
    lelLevel: str | None = None
    h2sLevel: str | None = None
    fireWatchRequired: bool = False
    rescuePlan: str | None = None


class PermitOut(BaseModel):
    id: str
    number: str
    type: PermitType
    plantId: str
    areaId: str | None
    location: str
    scopeOfWork: str
    validFrom: datetime
    validTo: datetime
    originatorId: str
    issuerId: str | None
    receiverId: str | None
    contractorName: str | None
    status: PermitStatus
    issuerApprovedAt: datetime | None
    safetyApprovedAt: datetime | None
    plantHeadApprovedAt: datetime | None
    closedAt: datetime | None
    suspendedAt: datetime | None
    suspendedReason: str | None
    expiredAt: datetime | None
    createdAt: datetime
    updatedAt: datetime

    model_config = {"from_attributes": True}


class SuspendRequest(BaseModel):
    reason: str = Field(min_length=1)


class ResumeRequest(BaseModel):
    comments: str | None = None


class AdminResetRequest(BaseModel):
    status: str  # DRAFT or SUBMITTED only
