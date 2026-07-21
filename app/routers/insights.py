"""AI Insights router — deterministic, airgap-safe insight layer for list screens.

`GET /api/insights/{module}` returns bar-level insights + row-level signals for
a module, scoped to a plant + date range. This is the house GET-analytics
convention (cams/dashboard/risk-dashboard) standing in for the spec's
`POST /api/ai-insights/:module`: the engine pulls from the existing models
server-side rather than the browser shipping the record set up (spec §1.1). The
response is fully computed from real records; no network egress occurs.

Auth is the platform gate (get_current_user). Plant scope is supplied by the
caller (the list screens already resolve the active plant for their strips);
`plant` is passed through to the query filter. Cross-plant scope hardening via
QueryScope is a follow-up — documented in the DECISIONS log.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.insights import InsightResponse
from app.services.insights import SUPPORTED_MODULES, compute

router = APIRouter(prefix="/api/insights", tags=["insights"])


@router.get("/modules")
async def list_modules(
    user: User = Depends(get_current_user),  # noqa: ARG001 — auth gate only
) -> dict[str, list[str]]:
    return {"modules": list(SUPPORTED_MODULES)}


@router.get("/{module}", response_model=InsightResponse)
async def module_insights(
    module: str,
    plant: str | None = Query(default=None, description="Active plant id to scope insights to"),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    user: User = Depends(get_current_user),  # noqa: ARG001 — auth gate only
    db: AsyncSession = Depends(get_db),
) -> InsightResponse:
    if module not in SUPPORTED_MODULES:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No insight rules for module '{module}'. Supported: {', '.join(SUPPORTED_MODULES)}",
        )
    return await compute(db, module, plant=plant, date_from=date_from, date_to=date_to)
