from datetime import datetime

from pydantic import BaseModel, Field


class TrainingCreate(BaseModel):
    employeeId: str
    programId: str
    trainerId: str | None = None
    trainerName: str | None = None
    date: datetime
    durationHours: int = Field(gt=0)
    score: int | None = None
    passed: bool = True
    certificateUrl: str | None = None
    remarks: str | None = None


class TrainingProgramOut(BaseModel):
    id: str
    code: str
    name: str
    description: str | None
    durationHours: int
    validityMonths: int
    category: str | None
    plantId: str | None
    isActive: bool

    model_config = {"from_attributes": True}


class TrainingRecordOut(BaseModel):
    id: str
    employeeId: str
    programId: str
    trainerId: str | None
    trainerName: str | None
    date: datetime
    durationHours: int
    score: int | None
    passed: bool
    validUntil: datetime
    certificateUrl: str | None
    remarks: str | None
    createdAt: datetime

    model_config = {"from_attributes": True}
