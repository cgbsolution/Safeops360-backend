"""Workflow engine — direct port of `src/lib/workflow/engine.ts`.

Generic Maker → Checker → Assignee → Verifier → Closure pipeline used by every
operational module. Enforces:
  • segregation of duties (assignee match)
  • RBAC triple-check (assignee + role + module permission)
  • full audit trail via WorkflowHistory rows
  • role-based assignee resolution that reads UserRole (multi-role aware)

Public surface mirrors the TS WorkflowEngine object:
  initiate(...)   — create instance + first task
  approve(...)    — checker approves
  reject(...)     — checker rejects
  submit_execution(...) — assignee task done
  verify(...)     — verifier accepts / rejects
  resubmit(...)   — initiator re-submits a rejected workflow
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import User, UserRole
from app.models.workflow import (
    Action,
    InstanceStatus,
    StepType,
    TaskStatus,
    TaskType,
    WorkflowDefinition,
    WorkflowHistory,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTask,
)
from app.services.permissions import get_user_role_codes


class WorkflowError(Exception):
    """Workflow-level error. Routers convert to HTTP 400 / 403."""


# ───────────────────────────────────────────────────────────────────────────
# Assignee resolution
# ───────────────────────────────────────────────────────────────────────────


async def _find_user_by_roles(
    db: AsyncSession, role_codes: list[str], plant_id: str | None
) -> str | None:
    """Pick a user holding any of the given roles. UserRole-driven so multi-role
    users are visible regardless of which role they hold as primary."""
    if not role_codes:
        return None
    now = datetime.now(timezone.utc)
    stmt = (
        select(UserRole, User)
        .join(User, User.id == UserRole.userId)
        .where((UserRole.validTo.is_(None)) | (UserRole.validTo > now))
        .options(selectinload(UserRole.role))
        .order_by(User.createdAt)
    )
    result = await db.execute(stmt)
    rows = [(ur, u) for (ur, u) in result.all() if ur.role.code in role_codes and ur.role.isActive]
    if not rows:
        return None

    if plant_id:
        # Prefer users at the requested plant
        for ur, u in rows:
            same_plant = u.plantId == plant_id
            scope_ok = ur.scopeType is None or ur.scopeType != "PLANT" or ur.scopeValue == plant_id
            if same_plant and scope_ok:
                return u.id
        # Fall back to globally-scoped role rows
        for ur, u in rows:
            if ur.scopeType is None or ur.scopeType != "PLANT":
                return u.id
    return rows[0][1].id


async def _close_pending_tasks(
    db: AsyncSession, *, instance_id: str, except_task_id: str | None = None
) -> int:
    """Mark all PENDING tasks on an instance as SKIPPED (except optionally
    the one currently being processed). Used when the workflow reaches a
    terminal state — leftover pending tasks from old bugs or duplicates
    would otherwise keep showing as 'Awaiting Action'."""
    rows = (
        await db.execute(
            select(WorkflowTask)
            .where(WorkflowTask.instanceId == instance_id)
            .where(WorkflowTask.status == TaskStatus.PENDING.value)
        )
    ).scalars().all()
    closed = 0
    now = datetime.now(timezone.utc)
    for t in rows:
        if except_task_id and t.id == except_task_id:
            continue
        t.status = TaskStatus.SKIPPED.value
        t.completedAt = now
        closed += 1
    return closed


async def _enrich_record_data(
    db: AsyncSession, *, module: str, record_id: str, base: dict[str, Any]
) -> dict[str, Any]:
    """Pull the canonical owner/observer/assignee fields off the actual
    record so the next step's approverField lookup ('ACTION_OWNER',
    'RESPONSIBLE_PERSON', 'ORIGINATOR' …) resolves to the right user
    even when the caller passed a partial record_data dict.

    Caller-supplied keys win — base values aren't overwritten."""
    merged = dict(base or {})
    try:
        if module == "OBSERVATION":
            from app.models.observation import Observation

            obs = await db.get(Observation, record_id)
            if obs is not None:
                merged.setdefault("observerId", obs.observerId)
                merged.setdefault("responsiblePersonId", obs.responsiblePersonId)
                merged.setdefault("actionOwnerId", obs.responsiblePersonId)
                merged.setdefault("plantId", obs.plantId)
                merged.setdefault("severity", obs.severity.value if hasattr(obs.severity, "value") else obs.severity)
                merged.setdefault("type", obs.type.value if hasattr(obs.type, "value") else obs.type)
                merged.setdefault("category", obs.category.value if hasattr(obs.category, "value") else obs.category)
        elif module == "INCIDENT":
            from app.models.incident import Incident

            inc = await db.get(Incident, record_id)
            if inc is not None:
                merged.setdefault("reporterId", inc.reporterId)
                merged.setdefault("plantId", inc.plantId)
        elif module == "NEAR_MISS":
            from app.models.near_miss import NearMiss

            nm = await db.get(NearMiss, record_id)
            if nm is not None:
                merged.setdefault("reporterId", nm.reporterId)
                merged.setdefault("actionOwnerId", nm.actionOwnerId)
                merged.setdefault("responsiblePersonId", nm.actionOwnerId)
                merged.setdefault("plantId", nm.plantId)
        # Other modules can be added as their workflows wire up. Falling
        # through with the base dict is safe — we just lose the safety net.
    except Exception:
        # Enrichment is best-effort; never let it block task creation.
        pass
    return merged


async def _resolve_assignee(
    db: AsyncSession,
    *,
    approver_role: str | None,
    approver_field: str | None,
    approver_user_id: str | None,
    approver_group_roles: str | None,
    record_data: dict[str, Any],
    initiator_id: str,
    plant_id: str | None,
) -> str | None:
    """Maps a step's approver* fields to a concrete user id."""
    if approver_user_id:
        return approver_user_id

    if approver_field:
        field_val = record_data.get(approver_field) or record_data.get(approver_field.lower())
        if isinstance(field_val, str):
            return field_val
        if approver_field == "ORIGINATOR":
            return (
                record_data.get("observerId")
                or record_data.get("reporterId")
                or record_data.get("originatorId")
                or initiator_id
            )
        if approver_field == "ACTION_OWNER":
            return record_data.get("actionOwnerId") or record_data.get("responsiblePersonId")
        if approver_field == "RESPONSIBLE_PERSON":
            return record_data.get("responsiblePersonId") or record_data.get("actionOwnerId")
        if approver_field == "ASSIGNED_INSPECTOR":
            return record_data.get("inspectorId")
        if approver_field == "RECEIVER":
            return record_data.get("receiverId")
        if approver_field == "ISSUER":
            return record_data.get("issuerId")
        if approver_field == "TRAINER":
            return record_data.get("trainerId")

    if approver_group_roles:
        try:
            roles = json.loads(approver_group_roles)
            if isinstance(roles, list) and roles:
                user_id = await _find_user_by_roles(db, roles, plant_id)
                if user_id:
                    return user_id
        except (json.JSONDecodeError, TypeError):
            pass

    if approver_role:
        return await _find_user_by_roles(db, [approver_role], plant_id)

    return None


# ───────────────────────────────────────────────────────────────────────────
# Condition evaluation (port of evaluateCondition)
# ───────────────────────────────────────────────────────────────────────────


def _evaluate_rule(rule: dict, record_data: dict) -> bool:
    actual = record_data.get(rule.get("field"))
    op = rule.get("operator")
    value = rule.get("value")
    if op == "=":
        return actual == value
    if op == "!=":
        return actual != value
    if op == "in":
        items = value if isinstance(value, list) else [v.strip() for v in str(value).split(",")]
        return actual in items
    if op == "not_in":
        items = value if isinstance(value, list) else [v.strip() for v in str(value).split(",")]
        return actual not in items
    if op == ">":
        try:
            return float(actual) > float(value)
        except (TypeError, ValueError):
            return False
    if op == "<":
        try:
            return float(actual) < float(value)
        except (TypeError, ValueError):
            return False
    return False


def _evaluate_condition(expr: str | None, record_data: dict) -> bool:
    if not expr:
        return True
    try:
        cond = json.loads(expr)
    except (json.JSONDecodeError, TypeError):
        return True
    if isinstance(cond, dict) and cond.get("version") == 2:
        rules = cond.get("rules") or []
        if not rules:
            return True
        results = [_evaluate_rule(r, record_data) for r in rules]
        return any(results) if cond.get("combinator") == "OR" else all(results)
    # v1 legacy: { field: value | array } — keys ANDed
    if isinstance(cond, dict):
        for field, expected in cond.items():
            actual = record_data.get(field)
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True
    return True


def _find_next_applicable_step(
    steps: list[WorkflowStep], from_sequence: int, record_data: dict
) -> WorkflowStep | None:
    for s in steps:
        if s.sequence <= from_sequence:
            continue
        if not _evaluate_condition(s.conditionExpr, record_data):
            continue
        return s
    return None


def _calc_due_at(step: WorkflowStep) -> datetime | None:
    if not step.slaHours:
        return None
    return datetime.now(timezone.utc) + timedelta(hours=step.slaHours)


# ───────────────────────────────────────────────────────────────────────────
# RBAC triple-check — gates every transition
# ───────────────────────────────────────────────────────────────────────────


async def _rbac_gate(
    db: AsyncSession,
    *,
    task: WorkflowTask,
    step: WorkflowStep,
    user_id: str,
    action: str,  # APPROVE | EXECUTE | VERIFY | REJECT
) -> None:
    # 1. Assignee — direct match. Group-queue match is disabled because the
    # eligibleGroupRoles column isn't in Prisma's schema; task.assignedToId
    # is always populated by resolve_assignee().
    if task.assignedToId != user_id:
        raise WorkflowError("Not your task")

    # 2. Role — the step's required role must be one the user holds
    if step.approverRole:
        user_roles = await get_user_role_codes(db, user_id)
        if step.approverRole not in user_roles:
            raise WorkflowError(f"This step requires the '{step.approverRole}' role.")

    # 3. Permission — verify the user holds the module action grant at ALL
    # (any scope). We deliberately skip the scope check here: the assignee
    # match in (1) + role match in (2) already prove the user is the
    # legitimate actor for this specific record. Re-enforcing OWN_PLANT /
    # OWN_DEPARTMENT scope at this point would require loading the record
    # and inferring its scope-context for every module — and would
    # incorrectly deny assignees whose role grants the action only with a
    # narrower scope (e.g. SUPERVISOR has OBSERVATION.APPROVE under
    # OWN_DEPARTMENT, but they were specifically picked for this task).
    perm_action = "APPROVE" if action == "REJECT" else action
    perm_code = f"{task.module}.{perm_action}"
    rows = await _user_permission_codes(db, user_id)
    if perm_code not in rows:
        raise WorkflowError(f"Missing permission '{perm_code}'.")


async def _user_permission_codes(db: AsyncSession, user_id: str) -> set[str]:
    from app.services.permissions import _load_user_permissions

    rows = await _load_user_permissions(db, user_id)
    return {r.permission_code for r in rows}


# ───────────────────────────────────────────────────────────────────────────
# Status sync — keeps record-level status enums in lockstep with workflow state
# ───────────────────────────────────────────────────────────────────────────


async def _sync_record_status(
    db: AsyncSession,
    *,
    module: str,
    record_id: str,
    next_step_type: StepType | None,
    instance_completed: bool,
) -> None:
    """Mirror of TS syncRecordStatus(). Module-specific status mapping kept here
    to centralise the rules. Only PTW has a non-trivial status enum that the
    engine drives; other modules use generic statuses that are status-set by
    the route handlers themselves."""
    if module == "PTW":
        from app.models.permit import Permit, PermitStatus

        permit = await db.get(Permit, record_id)
        if permit is None or permit.status in {PermitStatus.SUSPENDED, PermitStatus.EXPIRED, PermitStatus.REJECTED}:
            return

        if instance_completed:
            permit.status = PermitStatus.CLOSED
            permit.closedAt = datetime.now(timezone.utc)
        elif next_step_type in (StepType.ASSIGNEE_TASK.value, StepType.CLOSURE.value):
            permit.status = PermitStatus.ACTIVE
        else:
            permit.status = PermitStatus.SUBMITTED
        await db.flush()
    elif module == "OBSERVATION" and instance_completed:
        from app.models.observation import Observation, ObservationStatus

        obs = await db.get(Observation, record_id)
        if obs is not None and obs.status != ObservationStatus.CLOSED:
            obs.status = ObservationStatus.CLOSED
            obs.closedAt = datetime.now(timezone.utc)
            await db.flush()

        # Post-closure cross-module triggers (Dimension 4). Today this is
        # just the LessonsDistributionAgent (Anthropic) — additional rules
        # (focused inspection on repeats, contractor score, PPE trend etc.)
        # can be ported from src/lib/observation/post-closure-rules.ts.
        # Each trigger is best-effort; failures must NEVER block closure.
        # SAVEPOINT so an internal flush failure doesn't poison the main
        # workflow transaction.
        try:
            async with db.begin_nested():
                from app.services.post_closure_rules import run_post_closure_rules

                await run_post_closure_rules(db, observation_id=record_id)
        except Exception as e:  # noqa: BLE001
            import sys
            print(f"[post-closure] OBSERVATION {record_id}: {e}", file=sys.stderr)


# ───────────────────────────────────────────────────────────────────────────
# Task creation
# ───────────────────────────────────────────────────────────────────────────


async def _create_task_for_step(
    db: AsyncSession,
    *,
    instance: WorkflowInstance,
    step: WorkflowStep,
    record_data: dict[str, Any],
    record_number: str | None,
    record_title: str | None,
    module: str,
    record_id: str,
    initiator_id: str,
    plant_id: str | None,
) -> WorkflowTask | None:
    # SQLAlchemy returns step.stepType as a StepType enum (str-subclass).
    # Coerce to plain str so dict lookups + comparisons are predictable
    # whether SQLAlchemy hands back an enum or a raw string.
    step_type = step.stepType.value if hasattr(step.stepType, "value") else str(step.stepType)

    if step_type == StepType.MAKER.value:
        # MAKER is implicit — the act of submitting the record IS the maker step.
        return None

    task_type_map = {
        StepType.CHECKER.value: TaskType.APPROVAL.value,
        StepType.ASSIGNEE_TASK.value: TaskType.EXECUTION.value,
        StepType.VERIFIER.value: TaskType.VERIFICATION.value,
        # CLOSURE — HSE Manager (or designated closer) confirms the record
        # can be closed. Modelled as an APPROVAL task so the existing
        # approval panel handles the UI; approving it advances past the
        # last step → instance COMPLETED → record marked closed.
        StepType.CLOSURE.value: TaskType.APPROVAL.value,
    }
    task_type = task_type_map.get(step_type)
    if task_type is None:
        return None

    # Safety net: callers (the approval/execution panels) sometimes pass a
    # partial record_data that doesn't include the fields needed by the
    # next step's approverField (e.g. ACTION_OWNER → responsiblePersonId).
    # Load the actual record from DB and merge its key fields so assignee
    # resolution can succeed without depending on what the UI sent.
    enriched = await _enrich_record_data(db, module=module, record_id=record_id, base=record_data)

    assignee_id = await _resolve_assignee(
        db,
        approver_role=step.approverRole,
        approver_field=step.approverField,
        approver_user_id=step.approverUserId,
        approver_group_roles=step.approverGroupRoles,
        record_data=enriched,
        initiator_id=initiator_id,
        plant_id=plant_id,
    )

    # Prisma's WorkflowTask requires a non-null assignedToId. If the
    # resolver returned None (no eligible user found), fall back to the
    # workflow initiator so the row is at least valid; the task can be
    # reassigned manually later.
    if not assignee_id:
        assignee_id = initiator_id

    # task_type is already the .value string from task_type_map; do NOT call
    # .value on it again — that throws AttributeError, which the route's
    # outer try/except swallows, leaving the workflow instance with zero
    # pending tasks and an empty "Awaiting Action" panel on the detail page.
    task = WorkflowTask(
        instanceId=instance.id,
        stepId=step.id,
        stepName=step.name,
        taskType=task_type,
        module=module,
        recordId=record_id,
        recordNumber=record_number,
        recordTitle=record_title,
        assignedToId=assignee_id,
        status=TaskStatus.PENDING.value,
        dueAt=_calc_due_at(step),
    )
    db.add(task)
    await db.flush()
    return task


# ───────────────────────────────────────────────────────────────────────────
# Public engine surface
# ───────────────────────────────────────────────────────────────────────────


async def initiate(
    db: AsyncSession,
    *,
    module: str,
    record_id: str,
    record_number: str | None,
    record_title: str | None,
    record_data: dict[str, Any],
    initiator_id: str,
    plant_id: str | None,
) -> WorkflowInstance:
    """Find the active definition for `module` (+optional recordType filter via
    record_data['type']) and create the WorkflowInstance + first task."""
    record_type = record_data.get("type")
    stmt = (
        select(WorkflowDefinition)
        .where(WorkflowDefinition.module == module, WorkflowDefinition.isActive == True)
        .options(selectinload(WorkflowDefinition.steps))
    )
    candidates = (await db.execute(stmt)).scalars().all()
    # Prefer recordType match, else fall back to default (recordType is null)
    definition = next((d for d in candidates if d.recordType == record_type), None)
    if definition is None:
        definition = next((d for d in candidates if d.recordType is None), None)
    if definition is None:
        raise WorkflowError(f"No active workflow definition found for module {module}")

    # Sort steps once
    steps = sorted(definition.steps, key=lambda s: s.sequence)
    maker = next((s for s in steps if s.stepType == StepType.MAKER.value), None)
    if maker is None:
        raise WorkflowError("Workflow has no MAKER step")

    next_step = _find_next_applicable_step(steps, maker.sequence, record_data)
    if next_step is None:
        raise WorkflowError("Workflow has no executable step after Maker")

    # Prisma schema has no recordTitle / recordData / plantId on
    # WorkflowInstance — they used to be Python-only fields. Drop them at
    # write time; callers that still pass them are no-ops.
    instance = WorkflowInstance(
        definitionId=definition.id,
        module=module,
        recordId=record_id,
        recordNumber=record_number,
        initiatedById=initiator_id,
        status=InstanceStatus.IN_PROGRESS.value,
        currentStepId=next_step.id,
        currentStepName=next_step.name,
    )
    db.add(instance)
    await db.flush()

    db.add(
        WorkflowHistory(
            instanceId=instance.id,
            stepId=maker.id,
            stepName=maker.name,
            action=Action.SUBMITTED.value,
            performedById=initiator_id,
        )
    )

    await _create_task_for_step(
        db,
        instance=instance,
        step=next_step,
        record_data=record_data,
        record_number=record_number,
        record_title=record_title,
        module=module,
        record_id=record_id,
        initiator_id=initiator_id,
        plant_id=plant_id,
    )
    await _sync_record_status(
        db,
        module=module,
        record_id=record_id,
        next_step_type=next_step.stepType,
        instance_completed=False,
    )
    return instance


async def _load_task_with_definition(db: AsyncSession, task_id: str) -> tuple[WorkflowTask, WorkflowInstance, list[WorkflowStep]]:
    task = await db.get(
        WorkflowTask,
        task_id,
        options=[
            selectinload(WorkflowTask.instance)
            .selectinload(WorkflowInstance.definition)
            .selectinload(WorkflowDefinition.steps)
        ],
    )
    if task is None:
        raise WorkflowError("Task not found")
    instance = task.instance
    steps = sorted(instance.definition.steps, key=lambda s: s.sequence)
    return task, instance, steps


async def _advance(
    db: AsyncSession,
    *,
    task: WorkflowTask,
    instance: WorkflowInstance,
    steps: list[WorkflowStep],
    user_id: str,
    action: Action,
    comments: str | None,
    attachments: list[str] | None,
    record_data: dict[str, Any],
    plant_id: str | None,
) -> dict[str, Any]:
    current_step = next((s for s in steps if s.id == task.stepId), None)
    if current_step is None:
        raise WorkflowError("Step missing")
    next_step = _find_next_applicable_step(steps, current_step.sequence, record_data)

    task.status = TaskStatus.COMPLETED.value
    task.completedAt = datetime.now(timezone.utc)
    db.add(
        WorkflowHistory(
            instanceId=task.instanceId,
            stepId=current_step.id,
            stepName=current_step.name,
            action=action,
            performedById=user_id,
            comments=comments,
            attachments=json.dumps(attachments) if attachments else None,
        )
    )

    if next_step:
        instance.currentStepId = next_step.id
        instance.currentStepName = next_step.name
    else:
        instance.currentStepId = None
        instance.currentStepName = "Completed"
        instance.status = InstanceStatus.COMPLETED.value
        instance.completedAt = datetime.now(timezone.utc)
        # Clean up any other PENDING tasks for this instance — duplicates
        # from past engine bugs (or stale tasks from a recovered orphan)
        # would otherwise show as live "Awaiting Action" entries even
        # though the workflow is closed.
        await _close_pending_tasks(db, instance_id=instance.id, except_task_id=task.id)
    await db.flush()

    if next_step:
        await _create_task_for_step(
            db,
            instance=instance,
            step=next_step,
            record_data=record_data,
            record_number=task.recordNumber,
            record_title=task.recordTitle,
            module=task.module,
            record_id=task.recordId,
            initiator_id=instance.initiatedById,
            plant_id=plant_id,
        )

    await _sync_record_status(
        db,
        module=task.module,
        record_id=task.recordId,
        next_step_type=next_step.stepType if next_step else None,
        instance_completed=next_step is None,
    )
    return {"ok": True, "advancedTo": next_step.name if next_step else "Completed"}


async def approve(
    db: AsyncSession,
    *,
    task_id: str,
    user_id: str,
    comments: str | None = None,
    attachments: list[str] | None = None,
    record_data: dict[str, Any] | None = None,
    plant_id: str | None = None,
) -> dict[str, Any]:
    task, instance, steps = await _load_task_with_definition(db, task_id)
    if task.status != TaskStatus.PENDING.value:
        raise WorkflowError("Task is not pending")
    current_step = next((s for s in steps if s.id == task.stepId), None)
    if current_step is None:
        raise WorkflowError("Step missing")
    await _rbac_gate(db, task=task, step=current_step, user_id=user_id, action="APPROVE")
    return await _advance(
        db,
        task=task,
        instance=instance,
        steps=steps,
        user_id=user_id,
        action=Action.APPROVED.value,
        comments=comments,
        attachments=attachments,
        record_data=record_data or {},
        plant_id=plant_id,
    )


async def reject(
    db: AsyncSession,
    *,
    task_id: str,
    user_id: str,
    reason: str,
    comments: str | None = None,
) -> dict[str, Any]:
    task, instance, steps = await _load_task_with_definition(db, task_id)
    if task.status != TaskStatus.PENDING.value:
        raise WorkflowError("Task is not pending")
    current_step = next((s for s in steps if s.id == task.stepId), None)
    if current_step is None:
        raise WorkflowError("Step missing")
    await _rbac_gate(db, task=task, step=current_step, user_id=user_id, action="REJECT")

    task.status = TaskStatus.COMPLETED.value
    task.completedAt = datetime.now(timezone.utc)
    instance.status = InstanceStatus.REJECTED.value
    instance.currentStepName = "Rejected — rework required"
    db.add(
        WorkflowHistory(
            instanceId=task.instanceId,
            stepId=task.stepId,
            stepName=task.stepName,
            action=Action.REJECTED.value,
            performedById=user_id,
            comments=f"{reason}\n\n{comments}" if comments else reason,
            fromStatus=InstanceStatus.IN_PROGRESS.value,
            toStatus=InstanceStatus.REJECTED.value,
        )
    )
    await _close_pending_tasks(db, instance_id=instance.id, except_task_id=task.id)
    await db.flush()
    return {"ok": True, "status": InstanceStatus.REJECTED.value}


async def submit_execution(
    db: AsyncSession,
    *,
    task_id: str,
    user_id: str,
    execution_data: dict[str, Any] | None = None,
    comments: str | None = None,
    attachments: list[str] | None = None,
    record_data: dict[str, Any] | None = None,
    plant_id: str | None = None,
) -> dict[str, Any]:
    task, instance, steps = await _load_task_with_definition(db, task_id)
    if task.status != TaskStatus.PENDING.value:
        raise WorkflowError("Task is not pending")
    current_step = next((s for s in steps if s.id == task.stepId), None)
    if current_step is None:
        raise WorkflowError("Step missing")
    await _rbac_gate(db, task=task, step=current_step, user_id=user_id, action="EXECUTE")

    # PTW–FLRA gate: a permit cannot transition out of its receiver step
    # without a COMPLETED FLRA whose crew has all signed.
    if task.module == "PTW" and current_step.stepType == StepType.ASSIGNEE_TASK.value:
        from app.services.flra_gate import get_flra_gate_status

        gate = await get_flra_gate_status(db, task.recordId)
        if not gate.ok:
            raise WorkflowError(gate.reason or "FLRA gate is closed for this permit.")

    merged = {**(record_data or {}), **(execution_data or {})}
    return await _advance(
        db,
        task=task,
        instance=instance,
        steps=steps,
        user_id=user_id,
        action=Action.EXECUTED.value,
        comments=comments,
        attachments=attachments,
        record_data=merged,
        plant_id=plant_id,
    )


async def verify(
    db: AsyncSession,
    *,
    task_id: str,
    user_id: str,
    accepted: bool,
    comments: str | None = None,
    record_data: dict[str, Any] | None = None,
    plant_id: str | None = None,
) -> dict[str, Any]:
    task, instance, steps = await _load_task_with_definition(db, task_id)
    if task.status != TaskStatus.PENDING.value:
        raise WorkflowError("Task is not pending")
    current_step = next((s for s in steps if s.id == task.stepId), None)
    if current_step is None:
        raise WorkflowError("Step missing")
    await _rbac_gate(db, task=task, step=current_step, user_id=user_id, action="VERIFY")

    if not accepted:
        # Verifier rejection — send back to the most recent ASSIGNEE_TASK
        # step so the action owner can redo the work. A new PENDING task
        # is created so the detail page shows an "Awaiting Action" entry
        # with the action owner. If no ASSIGNEE_TASK exists in this
        # workflow, fall through to the legacy "REJECTED back to MAKER"
        # behaviour (resubmit panel).
        task.status = TaskStatus.COMPLETED.value
        task.completedAt = datetime.now(timezone.utc)
        db.add(
            WorkflowHistory(
                instanceId=task.instanceId,
                stepId=task.stepId,
                stepName=task.stepName,
                action=Action.REJECTED.value,
                performedById=user_id,
                comments=comments,
                fromStatus=InstanceStatus.IN_PROGRESS.value,
                toStatus=InstanceStatus.IN_PROGRESS.value,
            )
        )

        rework_step = next(
            (
                s
                for s in reversed([x for x in steps if x.sequence < current_step.sequence])
                if (s.stepType.value if hasattr(s.stepType, "value") else s.stepType) == StepType.ASSIGNEE_TASK.value
            ),
            None,
        )
        if rework_step is not None:
            instance.currentStepId = rework_step.id
            instance.currentStepName = f"Rework — {rework_step.name}"
            # status stays IN_PROGRESS so the resubmit panel doesn't appear
            await db.flush()
            await _create_task_for_step(
                db,
                instance=instance,
                step=rework_step,
                record_data=record_data or {},
                record_number=task.recordNumber,
                record_title=task.recordTitle,
                module=task.module,
                record_id=task.recordId,
                initiator_id=instance.initiatedById,
                plant_id=plant_id,
            )
            return {"ok": True, "status": "REWORK", "sentTo": rework_step.name}

        # Legacy fallback: workflow has no assignee step → flag REJECTED
        instance.status = InstanceStatus.REJECTED.value
        instance.currentStepName = "Verification rejected — rework required"
        await db.flush()
        return {"ok": True, "status": InstanceStatus.REJECTED.value}

    return await _advance(
        db,
        task=task,
        instance=instance,
        steps=steps,
        user_id=user_id,
        action=Action.VERIFIED.value,
        comments=comments,
        attachments=None,
        record_data=record_data or {},
        plant_id=plant_id,
    )


async def resubmit(
    db: AsyncSession,
    *,
    instance_id: str,
    user_id: str,
    comments: str | None = None,
    record_data: dict[str, Any] | None = None,
    plant_id: str | None = None,
) -> dict[str, Any]:
    instance = await db.get(
        WorkflowInstance,
        instance_id,
        options=[selectinload(WorkflowInstance.definition).selectinload(WorkflowDefinition.steps)],
    )
    if instance is None:
        raise WorkflowError("Workflow instance not found")
    if instance.initiatedById != user_id:
        raise WorkflowError("Only the original submitter can re-submit this record")
    if instance.status != InstanceStatus.REJECTED.value:
        raise WorkflowError("Only rejected workflows can be re-submitted")

    steps = sorted(instance.definition.steps, key=lambda s: s.sequence)
    maker = next((s for s in steps if s.stepType == StepType.MAKER.value), steps[0])
    next_step = _find_next_applicable_step(steps, maker.sequence, record_data or {})
    if next_step is None:
        raise WorkflowError("Workflow has no reviewable step after the Maker")

    instance.status = InstanceStatus.IN_PROGRESS.value
    instance.currentStepId = next_step.id
    instance.currentStepName = next_step.name
    instance.completedAt = None
    db.add(
        WorkflowHistory(
            instanceId=instance.id,
            stepId=maker.id,
            stepName=maker.name,
            action=Action.SUBMITTED.value,
            performedById=user_id,
            comments=f"Re-submitted after rework. {comments or ''}".strip(),
            fromStatus=InstanceStatus.REJECTED.value,
            toStatus=InstanceStatus.IN_PROGRESS.value,
        )
    )
    await _create_task_for_step(
        db,
        instance=instance,
        step=next_step,
        record_data=record_data or {},
        record_number=instance.recordNumber,
        record_title=None,  # recordTitle column doesn't exist in Prisma
        module=instance.module,
        record_id=instance.recordId,
        initiator_id=instance.initiatedById,
        plant_id=plant_id,
    )
    await _sync_record_status(
        db,
        module=instance.module,
        record_id=instance.recordId,
        next_step_type=next_step.stepType,
        instance_completed=False,
    )
    return {"ok": True, "sentTo": next_step.name}
