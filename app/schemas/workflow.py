from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ApproveRequest(BaseModel):
    taskId: str
    comments: str | None = None
    attachments: list[str] | None = None
    recordData: dict[str, Any] | None = None
    plantId: str | None = None


class RejectRequest(BaseModel):
    taskId: str
    reason: str = Field(min_length=1)
    comments: str | None = None


class SubmitExecutionRequest(BaseModel):
    taskId: str
    executionData: dict[str, Any] | None = None
    comments: str | None = None
    attachments: list[str] | None = None
    recordData: dict[str, Any] | None = None
    plantId: str | None = None


class VerifyRequest(BaseModel):
    taskId: str
    accepted: bool
    comments: str | None = None
    recordData: dict[str, Any] | None = None
    plantId: str | None = None


class ResubmitRequest(BaseModel):
    instanceId: str
    comments: str | None = None
    recordData: dict[str, Any] | None = None
    plantId: str | None = None


class ReassignRequest(BaseModel):
    taskId: str
    toUserId: str
    reason: str | None = None


class MyCountResponse(BaseModel):
    """Inbox counters for the workflow-task header / dashboard pill.

    The legacy `count` field is kept for back-compat with older callers; new
    clients should consume the structured pending / overdue / completed
    triplet which mirrors the mobile inbox layout.
    """

    count: int
    pending: int = 0
    overdue: int = 0
    completed: int = 0


class WorkflowTaskOut(BaseModel):
    id: str
    module: str
    recordId: str
    recordNumber: str | None = None
    recordTitle: str | None = None
    stepName: str
    taskType: str
    status: str
    priority: str
    assignedAt: datetime
    dueAt: datetime | None = None

    model_config = {"from_attributes": True}


class WorkflowTaskListResponse(BaseModel):
    items: list[WorkflowTaskOut]
    total: int


# ─── Definition admin ────────────────────────────────────────────────────


class StepInput(BaseModel):
    id: str | None = None
    sequence: int
    stepType: str
    name: str
    approverRole: str | None = None
    approverField: str | None = None
    approverUserId: str | None = None
    approverGroupRoles: str | None = None
    slaHours: int | None = None
    slaUnit: str | None = None
    escalationRole: str | None = None
    isOptional: bool = False
    conditionExpr: str | None = None
    notes: str | None = None


class DefinitionCreate(BaseModel):
    module: str
    recordType: str | None = None
    name: str
    description: str | None = None
    isActive: bool = True


class DefinitionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    recordType: str | None = None
    isActive: bool | None = None
    steps: list[StepInput] | None = None
    changeNote: str | None = None


class StepOut(BaseModel):
    id: str
    sequence: int
    stepType: str
    name: str
    approverRole: str | None
    approverField: str | None
    approverUserId: str | None
    approverGroupRoles: str | None
    slaHours: int | None
    slaUnit: str | None
    escalationRole: str | None
    isOptional: bool
    conditionExpr: str | None
    notes: str | None

    model_config = {"from_attributes": True}


class DefinitionOut(BaseModel):
    id: str
    module: str
    recordType: str | None
    name: str
    description: str | None
    isActive: bool
    createdAt: datetime
    updatedAt: datetime
    steps: list[StepOut]

    model_config = {"from_attributes": True}
