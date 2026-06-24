"""FastAPI app factory.

`uvicorn app.main:app --reload` to run in development.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.db import engine, warmup
from app.licensing.enforcement import require_module
from app.licensing.router_map import ROUTER_MODULE
from app.licensing.state import refresh_state
from app.routers import (
    agents,
    agents_config,
    anomalies,
    audit_compliance,
    auth,
    cams,
    capa,
    competency,
    dashboard,
    devices,
    eai,
    erm,
    erm_p2,
    erm_p3,
    erm_t3,
    epc_contractors,
    epc_dashboard,
    epc_gate,
    epc_induction,
    epc_mobilization,
    epc_sites,
    epc_workers,
    factory,
    factory_ext,
    plants,
    flra,
    hira,
    incidents,
    inspections,
    kaizen,
    licensing,
    manhours,
    moc,
    near_miss,
    observations,
    ppe,
    ptw,
    ptw_active,
    risk_dashboard,
    risk_register,
    sci,
    scr,
    training,
    users,
    workflow,
    workflow_definitions,
)

settings = get_settings()
logging.basicConfig(level=settings.log_level)
log = logging.getLogger("safeops360")

# Every router this app mounts, keyed by its import name (matches ROUTER_MODULE).
# Order only affects OpenAPI grouping.
_ROUTERS = {
    "auth": auth, "users": users, "observations": observations, "near_miss": near_miss,
    "ptw": ptw, "ptw_active": ptw_active, "flra": flra, "incidents": incidents,
    "training": training, "inspections": inspections, "manhours": manhours,
    "workflow": workflow, "workflow_definitions": workflow_definitions, "anomalies": anomalies,
    "agents": agents, "agents_config": agents_config, "hira": hira, "capa": capa, "eai": eai,
    "erm": erm, "erm_p2": erm_p2, "erm_p3": erm_p3, "erm_t3": erm_t3, "competency": competency,
    "moc": moc, "risk_register": risk_register, "risk_dashboard": risk_dashboard,
    "scr": scr, "sci": sci, "kaizen": kaizen, "ppe": ppe,
    "epc_sites": epc_sites, "epc_contractors": epc_contractors, "epc_workers": epc_workers,
    "epc_mobilization": epc_mobilization, "epc_gate": epc_gate, "epc_induction": epc_induction,
    "epc_dashboard": epc_dashboard, "audit_compliance": audit_compliance, "cams": cams,
    "factory": factory, "factory_ext": factory_ext, "devices": devices, "plants": plants,
    "dashboard": dashboard, "licensing": licensing,
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Pre-warm the connection pool so the first user request doesn't pay
    # the cold TCP + TLS + auth handshake to Supabase.
    try:
        await warmup()
        log.info("DB connection warmed up")
    except Exception as e:  # noqa: BLE001
        log.warning(f"DB warmup failed (non-fatal): {e}")

    # Validate the licence on boot (offline; no network). A failure here MUST
    # NOT crash the app — it fails closed to a locked state instead.
    try:
        state = await refresh_state()
        log.info("Licence boot validation: status=%s", state.status)
    except Exception as e:  # noqa: BLE001
        log.warning("Licence boot validation failed (fails closed): %s", e)

    # Load the per-factory module-allocation cache (within the licence ceiling).
    try:
        from app.licensing import factory_entitlements
        await factory_entitlements.refresh()
    except Exception as e:  # noqa: BLE001
        log.warning("Factory-entitlement cache load failed: %s", e)

    recheck = asyncio.create_task(_licence_recheck_loop())
    try:
        yield
    finally:
        recheck.cancel()
        await engine.dispose()


async def _licence_recheck_loop() -> None:
    """Periodic re-validation — catches expiry roll-over, grace transitions, and
    clock tamper between boots without needing a restart (build prompt §5.1)."""
    interval = max(60, settings.licence_recheck_seconds)
    while True:
        try:
            await asyncio.sleep(interval)
            await refresh_state()
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            log.warning("Periodic licence re-check failed (fails closed): %s", e)


def create_app() -> FastAPI:
    app = FastAPI(
        title="SafeOps360 — Backend",
        version="1.0.0",
        description="Python backend for the SafeOps360 EHS platform.",
        debug=not settings.is_production,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount every router, attaching the module-entitlement guard to gated ones.
    # The guard is the API security boundary — a disabled module's endpoints
    # 403 regardless of the UI (build prompt §5.2, TL-01).
    for name, module in _ROUTERS.items():
        module_code = ROUTER_MODULE.get(name)
        deps = [Depends(require_module(module_code))] if module_code else []
        app.include_router(module.router, dependencies=deps)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env}

    @app.get("/api/meta/min-version", tags=["meta"])
    async def min_supported_version() -> dict[str, str]:
        """Force-update gate consumed by the mobile Bootstrapper. Returns the
        oldest app version we still allow to talk to this backend. Bump these
        to push users off a known-broken build."""
        return {"ios": "1.0.0", "android": "1.0.0"}

    return app


app = create_app()
