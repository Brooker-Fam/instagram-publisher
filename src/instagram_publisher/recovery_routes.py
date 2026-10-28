import time

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import update

from instagram_publisher.api_dependencies import require_admin
from instagram_publisher.credentials import account_token, credentials_valid
from instagram_publisher.db import (
    Account,
    Post,
    audit,
)
from instagram_publisher.meta import MetaError
from instagram_publisher.serializers import iso, post_dict

router = APIRouter()


@router.get("/api/posts/{post_id}/remote-status", dependencies=[Depends(require_admin)], tags=["Recovery"])
def remote_status(request: Request, post_id: str):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    client = request.app.state.meta_client
    if settings.publisher_mode != "live":
        raise HTTPException(409, "Remote status is available in live mode only")
    with session_factory() as session:
        post = session.get(Post, post_id)
        if post is None:
            raise HTTPException(404, "Post not found")
        account = session.get(Account, post.account_id) if post.account_id else None
        if not post.container_id or not credentials_valid(account, time.time()):
            raise HTTPException(409, "This post needs a saved container and a current Instagram connection")
        try:
            token = account_token(account, settings)
            provider = request.app.state.unified_client if account.auth_provider == "UNIFIED" else client
            result = provider.container_status(post.container_id, token)
        except MetaError as exc:
            raise HTTPException(502, str(exc)) from exc
        except InvalidToken as exc:
            raise HTTPException(409, "Restore the encryption key or reconnect Instagram") from exc
        status_code = result.get("status_code")
        if status_code not in {"IN_PROGRESS", "FINISHED", "PUBLISHED", "ERROR", "EXPIRED"}:
            raise HTTPException(502, "Instagram returned an unknown container status")
        return {
            "post_id": post_id,
            "container_id": post.container_id,
            "remote_status": status_code,
            "local_status": post.status,
            "checked_at": iso(time.time()),
            "note": "This check never submits a publication. FINISHED alone does not prove an earlier attempt failed.",
        }


@router.post("/api/posts/{post_id}/reconcile", dependencies=[Depends(require_admin)], tags=["Recovery"])
def reconcile(request: Request, post_id: str):
    session_factory = request.app.state.session_factory
    result = remote_status(request, post_id)
    if result["remote_status"] != "PUBLISHED":
        return result | {"resolved": False}
    with session_factory.begin() as session:
        changed = session.execute(
            update(Post)
            .where(
                Post.id == post_id,
                Post.status == "NEEDS_ATTENTION",
                Post.container_id == result["container_id"],
            )
            .values(
                status="PUBLISHED",
                updated_at=time.time(),
                error="Instagram confirmed publication; the original media ID and exact publication time were not returned.",
            )
        )
        if changed.rowcount:
            audit(
                session,
                "post.reconciled",
                post_id,
                "Meta container confirms PUBLISHED; no publication was resubmitted",
            )
        return result | {
            "resolved": bool(changed.rowcount),
            "post": post_dict(session.get(Post, post_id)),
        }
