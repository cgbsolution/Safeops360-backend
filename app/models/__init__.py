"""Re-exports for `from app.models import *` style imports.

Each module groups related tables; importing this package guarantees Alembic's
autogenerate sees every model.
"""

from app.models.anomaly import Anomaly
from app.models.equipment import Equipment, Inspection
from app.models.flra import FLRA, FLRACrewSignature, FLRAStatus, FLRATeamMember
from app.models.incident import (
    Incident,
    IncidentAttachment,
    IncidentInvestigationMember,
    IncidentStatus,
    IncidentType,
)
from app.models.manhours import Manhours
from app.models.masters import ContractorCompany, Department, MasterItem
from app.models.near_miss import NearMiss, NearMissStatus
from app.models.near_miss_children import (
    NearMissAttachment,
    NearMissCapa,
    NearMissComment,
    NearMissPersonAffected,
    NearMissPersonInvolved,
    NearMissWitness,
)
from app.models.observation import (
    Observation,
    ObservationAttachment,
    ObservationCategory,
    ObservationStatus,
    ObservationType,
    Severity,
)
from app.models.permit import (
    Permit,
    PermitCrewMember,
    PermitStatus,
    PermitType,
)
from app.models.plant import Area, Plant
from app.models.training import (
    TrainingAssessment,
    TrainingAssessmentResponse,
    TrainingAttendance,
    TrainingCertificate,
    TrainingProgram,
    TrainingProgramMaterial,
    TrainingProgramQuestion,
    TrainingRecord,
    TrainingRegistration,
    TrainingSchedule,
    TrainingSession,
)
from app.models.user import (
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.models.workflow import (
    InstanceStatus,
    StepType,
    TaskStatus,
    TaskType,
    WorkflowDefinition,
    WorkflowDefinitionVersion,
    WorkflowHistory,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTask,
)

__all__ = [
    "Anomaly",
    "Area",
    "Equipment",
    "FLRA",
    "FLRACrewSignature",
    "FLRAStatus",
    "FLRATeamMember",
    "Incident",
    "IncidentAttachment",
    "IncidentInvestigationMember",
    "IncidentStatus",
    "IncidentType",
    "Inspection",
    "InstanceStatus",
    "Manhours",
    "NearMiss",
    "NearMissStatus",
    "Observation",
    "ObservationAttachment",
    "ObservationCategory",
    "ObservationStatus",
    "ObservationType",
    "Permission",
    "Permit",
    "PermitCrewMember",
    "PermitStatus",
    "PermitType",
    "Plant",
    "Role",
    "RolePermission",
    "Severity",
    "StepType",
    "TaskStatus",
    "TaskType",
    "TrainingAssessment",
    "TrainingAssessmentResponse",
    "TrainingAttendance",
    "TrainingCertificate",
    "TrainingProgram",
    "TrainingProgramMaterial",
    "TrainingProgramQuestion",
    "TrainingRecord",
    "TrainingRegistration",
    "TrainingSchedule",
    "TrainingSession",
    "User",
    "UserRole",
    "WorkflowDefinition",
    "WorkflowDefinitionVersion",
    "WorkflowHistory",
    "WorkflowInstance",
    "WorkflowStep",
    "WorkflowTask",
]
