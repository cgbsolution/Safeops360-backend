"""Pydantic schemas for the Facilities module (Factory Profile Master).

camelCase throughout; doubles as the API contract the Next.js frontend mirrors
in src/app/(dashboard)/facilities/lib.ts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FactoryStatusLit = Literal[
    "OPERATIONAL", "UNDER_CONSTRUCTION", "PARTIAL_OPERATION", "SHUTDOWN", "DECOMMISSIONED"
]
OwnershipTypeLit = Literal["OWNED", "LEASED", "CONTRACT_MANUFACTURING", "JOINT_VENTURE"]
ProfileStatusLit = Literal["DRAFT", "ACTIVE", "REVIEW_DUE"]
BuildingTypeLit = Literal[
    "PRODUCTION", "WAREHOUSE", "ADMIN_OFFICE", "UTILITY", "CANTEEN",
    "DORMITORY", "ETP_PLANT", "BOILER_HOUSE", "STORE", "OTHER",
]
CertificationTypeLit = Literal[
    "SA8000", "ISO_9001", "ISO_14001", "ISO_45001", "WRAP",
    "BSCI", "OEKO_TEX", "GOTS", "SEDEX_SMETA", "OTHER",
]
CertStatusLit = Literal["VALID", "EXPIRING_SOON", "EXPIRED", "UNDER_RENEWAL", "SUSPENDED"]
ContactRoleLit = Literal[
    "FACTORY_MANAGER", "SAFETY_OFFICER", "COMPLIANCE_OFFICER", "HR_HEAD", "ENVIRONMENT_OFFICER", "OTHER",
]


class RegistrationNo(BaseModel):
    type: str
    number: str


# ════════════════════════════════════════════════════════════════════════════
# Buildings
# ════════════════════════════════════════════════════════════════════════════
class BuildingCreate(BaseModel):
    buildingName: str = Field(min_length=1)
    buildingType: BuildingTypeLit = "PRODUCTION"
    floors: int = Field(1, ge=1)
    areaSqm: float | None = Field(None, ge=0)
    maxOccupancy: int | None = Field(None, ge=0)
    currentOccupancy: int | None = Field(None, ge=0)
    yearBuilt: int | None = None
    assemblyPoint: str | None = None
    emergencyExits: int | None = Field(None, ge=0)
    occupancyCertificateNo: str | None = None
    isActive: bool = True


class BuildingUpdate(BaseModel):
    buildingName: str | None = None
    buildingType: BuildingTypeLit | None = None
    floors: int | None = None
    areaSqm: float | None = None
    maxOccupancy: int | None = None
    currentOccupancy: int | None = None
    yearBuilt: int | None = None
    assemblyPoint: str | None = None
    emergencyExits: int | None = None
    occupancyCertificateNo: str | None = None
    isActive: bool | None = None


class BuildingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    buildingName: str
    buildingType: str
    floors: int
    areaSqm: float | None = None
    maxOccupancy: int | None = None
    currentOccupancy: int | None = None
    yearBuilt: int | None = None
    assemblyPoint: str | None = None
    emergencyExits: int | None = None
    occupancyCertificateNo: str | None = None
    isActive: bool
    updatedAt: datetime | None = None


# ════════════════════════════════════════════════════════════════════════════
# Workforce Composition (SA8000 lens)
# ════════════════════════════════════════════════════════════════════════════
class WorkforceCompositionCreate(BaseModel):
    asOfDate: datetime | None = None  # defaults to now
    permanentCount: int = Field(0, ge=0)
    contractCount: int = Field(0, ge=0)
    apprenticeTraineeCount: int = Field(0, ge=0)
    maleCount: int = Field(0, ge=0)
    femaleCount: int = Field(0, ge=0)
    otherGenderCount: int = Field(0, ge=0)
    migrantWorkerCount: int | None = Field(None, ge=0)
    differentlyAbledCount: int | None = Field(None, ge=0)
    notes: str | None = None


class WorkforceCompositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    asOfDate: datetime
    isCurrent: bool
    permanentCount: int
    contractCount: int
    apprenticeTraineeCount: int
    maleCount: int
    femaleCount: int
    otherGenderCount: int
    migrantWorkerCount: int | None = None
    differentlyAbledCount: int | None = None
    totalCount: int
    # computed enrichment (set in the router)
    genderTotal: int = 0
    genderMismatch: bool = False
    notes: str | None = None
    updatedAt: datetime | None = None


# ════════════════════════════════════════════════════════════════════════════
# Production Process
# ════════════════════════════════════════════════════════════════════════════
class ProductionProcessCreate(BaseModel):
    processName: str = Field(min_length=1)
    processCategory: str | None = None
    description: str | None = None
    sequenceOrder: int | None = None
    shiftPattern: str | None = None
    installedCapacity: str | None = None
    keyHazards: list[str] = []
    isActive: bool = True


class ProductionProcessUpdate(BaseModel):
    processName: str | None = None
    processCategory: str | None = None
    description: str | None = None
    sequenceOrder: int | None = None
    shiftPattern: str | None = None
    installedCapacity: str | None = None
    keyHazards: list[str] | None = None
    isActive: bool | None = None


class ProductionProcessOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    processName: str
    processCategory: str | None = None
    description: str | None = None
    sequenceOrder: int | None = None
    shiftPattern: str | None = None
    installedCapacity: str | None = None
    keyHazards: list[str] = []
    isActive: bool
    updatedAt: datetime | None = None


# ════════════════════════════════════════════════════════════════════════════
# Factory Certification (status engine)
# ════════════════════════════════════════════════════════════════════════════
class FactoryCertificationCreate(BaseModel):
    certificationType: CertificationTypeLit
    certificateNo: str | None = None
    issuingBody: str | None = None
    issueDate: datetime | None = None
    expiryDate: datetime | None = None
    renewalLeadDays: int = Field(60, ge=0)
    # Manual override only for UNDER_RENEWAL / SUSPENDED; VALID/EXPIRING_SOON/
    # EXPIRED are always derived from the dates.
    status: CertStatusLit | None = None
    scopeNotes: str | None = None
    attachmentIds: list[str] = []


class FactoryCertificationUpdate(BaseModel):
    certificationType: CertificationTypeLit | None = None
    certificateNo: str | None = None
    issuingBody: str | None = None
    issueDate: datetime | None = None
    expiryDate: datetime | None = None
    renewalLeadDays: int | None = None
    status: CertStatusLit | None = None
    scopeNotes: str | None = None
    attachmentIds: list[str] | None = None


class FactoryCertificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    certificationType: str
    certificateNo: str | None = None
    issuingBody: str | None = None
    issueDate: datetime | None = None
    expiryDate: datetime | None = None
    renewalLeadDays: int
    status: str  # effective status, computed in the router
    daysToExpiry: int | None = None  # computed
    scopeNotes: str | None = None
    attachmentIds: list[str] = []
    updatedAt: datetime | None = None


# ════════════════════════════════════════════════════════════════════════════
# Factory Contact
# ════════════════════════════════════════════════════════════════════════════
class FactoryContactCreate(BaseModel):
    role: ContactRoleLit = "OTHER"
    name: str = Field(min_length=1)
    phone: str | None = None
    email: str | None = None
    isPrimary: bool = False


class FactoryContactUpdate(BaseModel):
    role: ContactRoleLit | None = None
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    isPrimary: bool | None = None


class FactoryContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    role: str
    name: str
    phone: str | None = None
    email: str | None = None
    isPrimary: bool
    updatedAt: datetime | None = None


# ════════════════════════════════════════════════════════════════════════════
# Factory Profile
# ════════════════════════════════════════════════════════════════════════════
class FactoryProfileCreate(BaseModel):
    siteId: str  # Plant.id — mandatory 1:1 link
    factoryName: str = Field(min_length=2)
    factoryCode: str | None = None  # auto-generated when omitted
    status: FactoryStatusLit = "OPERATIONAL"
    ownershipType: OwnershipTypeLit = "OWNED"
    addressLine: str = ""
    city: str = ""
    state: str = ""
    pincode: str = ""
    latitude: float | None = None
    longitude: float | None = None
    establishedYear: int | None = None
    factoryLicenseNo: str | None = None
    factoryLicenseValidUntil: datetime | None = None
    registrationNos: list[RegistrationNo] = []
    applicableActs: list[str] = []
    pollutionControlBoard: str | None = None
    totalLandAreaSqm: float | None = None
    builtUpAreaSqm: float | None = None
    buildingCount: int | None = None  # manual when no Building rows entered
    primaryIndustry: str = "Garments / Textile"
    # Quick-add child records from the wizard (all optional — can be added later).
    buildings: list[BuildingCreate] = []
    workforce: WorkforceCompositionCreate | None = None  # initial composition
    processes: list[ProductionProcessCreate] = []


class FactoryProfileUpdate(BaseModel):
    factoryName: str | None = None
    status: FactoryStatusLit | None = None
    ownershipType: OwnershipTypeLit | None = None
    addressLine: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    establishedYear: int | None = None
    factoryLicenseNo: str | None = None
    factoryLicenseValidUntil: datetime | None = None
    registrationNos: list[RegistrationNo] | None = None
    applicableActs: list[str] | None = None
    pollutionControlBoard: str | None = None
    totalLandAreaSqm: float | None = None
    builtUpAreaSqm: float | None = None
    buildingCount: int | None = None
    primaryIndustry: str | None = None
    profileStatus: ProfileStatusLit | None = None


class FactoryProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    siteId: str
    siteName: str | None = None  # enriched from Plant
    factoryCode: str
    factoryName: str
    status: str
    ownershipType: str
    addressLine: str
    city: str
    state: str
    pincode: str
    latitude: float | None = None
    longitude: float | None = None
    establishedYear: int | None = None
    factoryLicenseNo: str | None = None
    factoryLicenseValidUntil: datetime | None = None
    registrationNos: list[RegistrationNo] = []
    applicableActs: list[str] = []
    pollutionControlBoard: str | None = None
    totalLandAreaSqm: float | None = None
    builtUpAreaSqm: float | None = None
    buildingCount: int
    totalEmployees: int
    primaryIndustry: str
    profileStatus: str
    lastReviewedAt: datetime | None = None
    nextReviewDate: datetime | None = None
    # cert roll-up (computed in the list endpoint)
    certCount: int = 0
    certsExpiringCount: int = 0  # EXPIRING_SOON + EXPIRED
    updatedAt: datetime | None = None


class FactoryProfileDetail(FactoryProfileOut):
    """Profile + its building register, current workforce (+ history) and
    production processes (F-02 detail page)."""

    buildings: list[BuildingOut] = []
    currentWorkforce: WorkforceCompositionOut | None = None
    workforceHistory: list[WorkforceCompositionOut] = []
    processes: list[ProductionProcessOut] = []
    certifications: list[FactoryCertificationOut] = []
    contacts: list[FactoryContactOut] = []


class FactoryProfileListResponse(BaseModel):
    items: list[FactoryProfileOut]
    total: int
    # roll-ups powering the F-01 KPI strip + filter chips
    totalBuildings: int = 0
    totalEmployees: int = 0
    certsExpiring: int = 0  # group-wide EXPIRING_SOON + EXPIRED
    statusCounts: dict[str, int] = {}
    stateCounts: dict[str, int] = {}
