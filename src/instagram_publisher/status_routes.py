import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, text

from instagram_publisher.api_dependencies import require_admin
from instagram_publisher.db import (
    AuditEvent,
    Post,
    WorkerHeartbeat,
)
from instagram_publisher.serializers import iso

router = APIRouter()


@router.get("/", include_in_schema=False)
def home(request: Request):
    return RedirectResponse("/docs")


@router.get("/healthz", tags=["Health"])
def health(request: Request):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        session.execute(text("SELECT 1"))
    return {"status": "ok", "mode": settings.publisher_mode}


@router.get("/api/status", dependencies=[Depends(require_admin)], tags=["Health"])
def status(request: Request):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        heartbeat = session.get(WorkerHeartbeat, "publisher")
        counts = session.execute(select(Post.status, func.count()).group_by(Post.status)).all()
        return {
            "mode": settings.publisher_mode,
            "instagram_configured": bool(
                settings.meta_app_id and settings.meta_app_secret.get_secret_value()
            ),
            "unified_configured": bool(
                settings.meta_unified_app_id and settings.meta_unified_app_secret.get_secret_value()
            ),
            "redirect_uri": settings.redirect_uri,
            "worker_last_seen": iso(heartbeat.updated_at) if heartbeat else None,
            "worker_healthy": bool(
                heartbeat
                and heartbeat.mode == settings.publisher_mode
                and time.time() - heartbeat.updated_at < settings.worker_lease_seconds
            ),
            "worker_mode": heartbeat.mode if heartbeat else None,
            "posts": dict(counts),
        }


@router.get("/api/events", dependencies=[Depends(require_admin)], tags=["Audit"])
def events(request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        return [
            {
                "id": e.id,
                "post_id": e.post_id,
                "action": e.action,
                "detail": e.detail,
                "created_at": iso(e.created_at),
            }
            for e in session.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit))
        ]
