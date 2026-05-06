"""Workflow action router. Mirrors /api/workflow/* on the Node side.

Auth-only at the router boundary — the workflow engine itself does the
RBAC triple-check (assignee + role + module-action permission). See
`services/workflow_engine.py:_rbac_gate`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.user import User, UserRole
from app.models.workflow import (
    Action,
    InstanceStatus,
    StepType,
    TaskStatus,
    WorkflowDefinition,
    WorkflowHistory,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTask,
)
from sqlalchemy.orm import selectinload
from app.schemas.workflow import (
    ApproveRequest,
    MyCountResponse,
    ReassignRequest,
    RejectRequest,
    ResubmitRequest,
    SubmitExecutionRequest,
    VerifyRequest,
)
from app.services import workflow_engine
from app.services.permissions import get_user_role_codes
from app.services.workflow_engine import WorkflowError

router = APIRouter(prefix="/api/workflow", tags=["workflow"])


def _bad_request(e: WorkflowError) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.post("/approve")
async def approve(
    payload: ApproveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        # Section Head Review (CHECKER) is where the responsible person is
        # now assigned for OBSERVATION records. If recordData carries a
        # responsiblePersonId, persist it on the Observation BEFORE the
        # engine advances — so the next ASSIGNEE_TASK step's
        # _resolve_assignee finds it via _enrich_record_data.
        #
        # IMPORTANT: query only the scalar columns (module, recordId) so
        # SQLAlchemy doesn't load the WorkflowTask entity into the
        # identity map. If we used `db.get(WorkflowTask, ...)` here, the
        # engine's later `_load_task_with_definition` would receive that
        # cached instance and silently ignore its `selectinload(instance)`
        # option → `task.instance` would trigger a lazy load and fail
        # with MissingGreenlet under async.
        rp_id = (payload.recordData or {}).get("responsiblePersonId")
        if rp_id:
            row = (
                await db.execute(
                    select(WorkflowTask.module, WorkflowTask.recordId)
                    .where(WorkflowTask.id == payload.taskId)
                )
            ).first()
            if row is not None and row.module == "OBSERVATION":
                from app.models.observation import Observation

                obs = await db.get(Observation, row.recordId)
                if obs is not None and not obs.responsiblePersonId:
                    obs.responsiblePersonId = rp_id
                    await db.flush()

        return await workflow_engine.approve(
            db,
            task_id=payload.taskId,
            user_id=user.id,
            comments=payload.comments,
            attachments=payload.attachments,
            record_data=payload.recordData,
            plant_id=payload.plantId or user.plantId,
        )
    except WorkflowError as e:
        raise _bad_request(e) from e


@router.post("/reject")
async def reject(
    payload: RejectRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return await workflow_engine.reject(
            db,
            task_id=payload.taskId,
            user_id=user.id,
            reason=payload.reason,
            comments=payload.comments,
        )
    except WorkflowError as e:
        raise _bad_request(e) from e


@router.post("/submit-execution")
async def submit_execution(
    payload: SubmitExecutionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return await workflow_engine.submit_execution(
            db,
            task_id=payload.taskId,
            user_id=user.id,
            execution_data=payload.executionData,
            comments=payload.comments,
            attachments=payload.attachments,
            record_data=payload.recordData,
            plant_id=payload.plantId or user.plantId,
        )
    except WorkflowError as e:
        raise _bad_request(e) from e


@router.post("/verify")
async def verify(
    payload: VerifyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return await workflow_engine.verify(
            db,
            task_id=payload.taskId,
            user_id=user.id,
            accepted=payload.accepted,
            comments=payload.comments,
            record_data=payload.recordData,
            plant_id=payload.plantId or user.plantId,
        )
    except WorkflowError as e:
        raise _bad_request(e) from e


@router.post("/resubmit")
async def resubmit(
    payload: ResubmitRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return await workflow_engine.resubmit(
            db,
            instance_id=payload.instanceId,
            user_id=user.id,
            comments=payload.comments,
            record_data=payload.recordData,
            plant_id=payload.plantId or user.plantId,
        )
    except WorkflowError as e:
        raise _bad_request(e) from e


@router.post("/reassign")
async def reassign(
    payload: ReassignRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Reassign a pending task to another user. Allowed by the current task
    holder OR HSE_MANAGER / ADMIN. Records an audit-trail entry so the
    workflow tracker shows who reassigned to whom and why."""
    task = await db.get(WorkflowTask, payload.taskId)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    if task.status != TaskStatus.PENDING.value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Task is not pending")
    role_codes = await get_user_role_codes(db, user.id)
    is_privileged = any(r in {"HSE_MANAGER", "ADMIN", "SYSTEM_ADMIN"} for r in role_codes)
    if task.assignedToId != user.id and not is_privileged:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the current task holder or HSE Manager can reassign")

    # Resolve old / new user names so the audit entry is human-readable
    # without the UI having to dereference IDs after the fact.
    old_user = await db.get(User, task.assignedToId)
    new_user = await db.get(User, payload.toUserId)
    if new_user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Target user not found")

    reason = (payload.reason or "").strip() or "(no reason provided)"
    audit_msg = (
        f"Reassigned from {old_user.name if old_user else task.assignedToId} → "
        f"{new_user.name}. Reason: {reason}"
    )

    # Write the audit row BEFORE mutating the task — this way both rows
    # land in one flush and the history reflects the actor + the change.
    db.add(
        WorkflowHistory(
            instanceId=task.instanceId,
            stepId=task.stepId,
            stepName=task.stepName,
            action=Action.REASSIGNED.value,
            performedById=user.id,
            comments=audit_msg,
        )
    )

    task.assignedToId = payload.toUserId
    task.assignedAt = datetime.now(timezone.utc)
    await db.flush()
    return {
        "ok": True,
        "task": {
            "id": task.id,
            "assignedToId": task.assignedToId,
            "assignedTo": {"id": new_user.id, "name": new_user.name, "designation": new_user.designation},
        },
        "audit": {
            "action": "REASSIGNED",
            "comments": audit_msg,
            "performedBy": user.name,
        },
    }


@router.post("/repair-orphan")
async def repair_orphan(
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Self-heal endpoint. Finds the WorkflowInstance for module+recordId; if
    it has currentStepId set but no PENDING task, creates the missing task
    for the current step. Used by the observation/incident detail pages to
    repair instances orphaned by past task-creation bugs."""
    module = str(payload.get("module") or "").strip()
    record_id = str(payload.get("recordId") or "").strip()
    if not module or not record_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "module and recordId required")

    inst_stmt = (
        select(WorkflowInstance)
        .where(WorkflowInstance.module == module, WorkflowInstance.recordId == record_id)
        .options(
            selectinload(WorkflowInstance.definition).selectinload(WorkflowDefinition.steps)
        )
    )
    instance = (await db.execute(inst_stmt)).scalar_one_or_none()
    if instance is None:
        return {"repaired": False, "reason": "no instance"}

    # Workflow is already in a terminal state but stale PENDING tasks
    # remain (typically from past duplicate-creation bugs). Close them
    # so the page stops showing "Awaiting Action" / the action panel.
    if instance.status in (InstanceStatus.COMPLETED.value, "COMPLETED"):
        closed = await workflow_engine._close_pending_tasks(db, instance_id=instance.id)
        if closed:
            await db.flush()
            return {"repaired": True, "closedStaleTasks": closed, "reason": "instance is COMPLETED"}
        return {"repaired": False, "reason": "instance is COMPLETED"}

    # Recover instances that were marked REJECTED by an old verifier
    # rejection (pre-rework-flow). Convert to IN_PROGRESS, point at the
    # most recent ASSIGNEE_TASK step, and create a rework task.
    if instance.status == InstanceStatus.REJECTED.value:
        steps_sorted = sorted(instance.definition.steps, key=lambda s: s.sequence)
        last_history = (
            await db.execute(
                select(WorkflowHistory)
                .where(WorkflowHistory.instanceId == instance.id)
                .order_by(WorkflowHistory.performedAt.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        rejected_by_verifier = False
        if last_history is not None and last_history.action == Action.REJECTED.value:
            verifier_step = next((s for s in steps_sorted if s.id == last_history.stepId), None)
            if verifier_step is not None:
                vt = verifier_step.stepType.value if hasattr(verifier_step.stepType, "value") else str(verifier_step.stepType)
                rejected_by_verifier = vt == StepType.VERIFIER.value
        if rejected_by_verifier:
            rework_step = next(
                (
                    s
                    for s in reversed(steps_sorted)
                    if (s.stepType.value if hasattr(s.stepType, "value") else s.stepType)
                    == StepType.ASSIGNEE_TASK.value
                ),
                None,
            )
            if rework_step is not None:
                instance.status = InstanceStatus.IN_PROGRESS.value
                instance.currentStepId = rework_step.id
                instance.currentStepName = f"Rework — {rework_step.name}"
                instance.completedAt = None
                await db.flush()
                task = await workflow_engine._create_task_for_step(
                    db,
                    instance=instance,
                    step=rework_step,
                    record_data={},
                    record_number=instance.recordNumber,
                    record_title=None,
                    module=module,
                    record_id=record_id,
                    initiator_id=instance.initiatedById,
                    plant_id=None,
                )
                return {
                    "repaired": True,
                    "reworkRecovered": True,
                    "stepName": rework_step.name,
                    "taskId": task.id if task else None,
                }
        return {"repaired": False, "reason": "instance status is REJECTED (not recoverable)"}

    if instance.status != "IN_PROGRESS":
        return {"repaired": False, "reason": f"instance status is {instance.status}"}
    if not instance.currentStepId:
        return {"repaired": False, "reason": "no current step"}

    pending_rows = (
        await db.execute(
            select(WorkflowTask)
            .where(WorkflowTask.instanceId == instance.id)
            .where(WorkflowTask.status == TaskStatus.PENDING.value)
            .order_by(WorkflowTask.assignedAt.desc())
        )
    ).scalars().all()
    if pending_rows:
        # Dedupe in-place: keep the newest PENDING task per (stepId, assignee)
        # and mark the rest SKIPPED. Past engine bugs occasionally produced
        # duplicates; this collapses them so the UI shows one entry.
        seen: set[tuple[str, str]] = set()
        skipped = 0
        for t in pending_rows:
            key = (t.stepId, t.assignedToId)
            if key in seen:
                t.status = TaskStatus.SKIPPED.value
                t.completedAt = datetime.now(timezone.utc)
                skipped += 1
            else:
                seen.add(key)

        # Re-resolve assignee for tasks that were created when record_data
        # was incomplete (the old approval panel didn't pass
        # responsiblePersonId, so ACTION_OWNER steps fell back to the
        # workflow initiator). Only fix when the current holder IS the
        # initiator — that's the fingerprint of the fallback path; never
        # overwrite an intentional reassignment.
        reassigned = 0
        for t in pending_rows:
            if t.status != TaskStatus.PENDING.value:
                continue
            if t.assignedToId != instance.initiatedById:
                continue
            step = next((s for s in instance.definition.steps if s.id == t.stepId), None)
            if step is None or not step.approverField:
                continue
            enriched = await workflow_engine._enrich_record_data(
                db, module=module, record_id=record_id, base={}
            )
            new_assignee = await workflow_engine._resolve_assignee(
                db,
                approver_role=step.approverRole,
                approver_field=step.approverField,
                approver_user_id=step.approverUserId,
                approver_group_roles=step.approverGroupRoles,
                record_data=enriched,
                initiator_id=instance.initiatedById,
                plant_id=enriched.get("plantId"),
            )
            if new_assignee and new_assignee != t.assignedToId:
                t.assignedToId = new_assignee
                t.assignedAt = datetime.now(timezone.utc)
                reassigned += 1

        if skipped or reassigned:
            await db.flush()
            return {
                "repaired": True,
                "deduped": skipped,
                "reassigned": reassigned,
                "reason": "cleaned duplicates and/or re-resolved assignees",
            }
        return {"repaired": False, "reason": "already has pending task"}

    step = next((s for s in instance.definition.steps if s.id == instance.currentStepId), None)
    if step is None:
        return {"repaired": False, "reason": "current step missing from definition"}

    step_type_value = step.stepType.value if hasattr(step.stepType, "value") else str(step.stepType)
    # MAKER is implicit (no task). CLOSURE now creates an approval task,
    # so it's a valid repair target.
    if step_type_value == StepType.MAKER.value:
        return {"repaired": False, "reason": f"current step is {step_type_value}"}

    task = await workflow_engine._create_task_for_step(
        db,
        instance=instance,
        step=step,
        record_data={},
        record_number=instance.recordNumber,
        record_title=None,
        module=module,
        record_id=record_id,
        initiator_id=instance.initiatedById,
        plant_id=None,
    )
    if task is None:
        return {"repaired": False, "reason": "engine returned no task"}
    return {"repaired": True, "taskId": task.id, "stepName": step.name}


@router.get("/my-count", response_model=MyCountResponse)
async def my_count(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MyCountResponse:
    """Inbox unread badge — counts pending tasks directly assigned to the user.
    Group-queue support is disabled because the `eligibleGroupRoles` column
    isn't in Prisma's schema."""
    direct = (
        select(func.count())
        .select_from(WorkflowTask)
        .where(WorkflowTask.assignedToId == user.id)
        .where(WorkflowTask.status == TaskStatus.PENDING.value)
    )
    direct_count = (await db.execute(direct)).scalar_one()
    group_count = 0
    return MyCountResponse(count=int(direct_count) + int(group_count))
