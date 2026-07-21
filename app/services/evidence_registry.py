"""Evidence Attachment entity registry (Stream B §5).

Maps an `entityType` string to everything the generic attachment router needs to
serve it safely: the SQLAlchemy model (to prove the parent exists + resolve its
plant), the permission codes to gate read vs write, and the allowed upload
categories. Adding a new attachable module (EAI SDS, Contractor certs, Training
certs, …) is one `EntitySpec` here — the router never changes.

This is where the shared capability earns its keep: the spec names four priority
modules (CAMS/Statutory, EAI, Contractor, Training); each is a registry line.
This pass wires CAMS first (highest compliance cost, spec §5.3).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.cams import CamsFinding


@dataclass(frozen=True)
class EntitySpec:
    label: str
    model: type
    # Column on the model row holding the plant/site id for the permission
    # PermissionContext, or None for platform-scoped entities.
    plant_attr: str | None
    read_perm: str
    write_perm: str
    # Allowed per-entity `category` values for an upload.
    categories: frozenset[str]


REGISTRY: dict[str, EntitySpec] = {
    # ── CAMS audit-finding evidence (spec §5.3 #1 — CAMS/Statutory) ──────────
    # Attach the source document that substantiates a finding / its closure.
    "cams_finding": EntitySpec(
        label="Audit finding",
        model=CamsFinding,
        plant_attr="siteId",
        read_perm="CAMS.READ",
        write_perm="CAMS.FINDING_MANAGE",
        categories=frozenset(
            {"FINDING_EVIDENCE", "CLOSURE_EVIDENCE", "CERTIFICATE", "LICENSE", "REPORT", "OTHER"}
        ),
    ),
    # ── Follow-ups (spec §5.3 #2-4) — each is a single line once wired: ──────
    #   "eai_entry":        EAI SDS sheets      → read EAI.READ  / write EAI.UPDATE
    #   "contractor":       insurance/comp certs→ read EPC.READ  / write EPC.UPDATE
    #   "training_record":  training certs      → read TRAINING.READ / write TRAINING.UPDATE
}


def get_spec(entity_type: str) -> EntitySpec | None:
    return REGISTRY.get(entity_type)


def supported_entities() -> list[str]:
    return sorted(REGISTRY)
