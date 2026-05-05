"""FastAPI app factory.

`uvicorn app.main:app --reload` to run in development.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.db import engine, warmup
from app.routers import (
    anomalies,
    auth,
    flra,
    incidents,
    inspections,
    manhours,
    near_miss,
    observations,
    ptw,
    training,
    users,
    workflow,
    workflow_definitions,
)

settings = get_settings()
logging.basicConfig(level=settings.log_level)
log = logging.getLogger("safeops360")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Pre-warm the connection pool so the first user request doesn't pay
    # the cold TCP + TLS + auth handshake to Supabase.
    try:
        await warmup()
        log.info("DB connection warmed up")
    except Exception as e:  # noqa: BLE001
        log.warning(f"DB warmup failed (non-fatal): {e}")
    yield
    await engine.dispose()


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

    # Order matters only for OpenAPI grouping.
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(observations.router)
    app.include_router(near_miss.router)
    app.include_router(ptw.router)
    app.include_router(flra.router)
    app.include_router(incidents.router)
    app.include_router(training.router)
    app.include_router(inspections.router)
    app.include_router(manhours.router)
    app.include_router(workflow.router)
    app.include_router(workflow_definitions.router)
    app.include_router(anomalies.router)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env}

    return app


app = create_app()
