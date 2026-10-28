import hashlib
import json
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from instagram_publisher.api_dependencies import require_admin
from instagram_publisher.connections import account_dict
from instagram_publisher.credentials import credentials_valid
from instagram_publisher.db import (
    Account,
    Post,
    audit,
)
from instagram_publisher.draft_validation import validate_assets
from instagram_publisher.schemas import DraftUpdate, PostDraft, RevisionRequest
from instagram_publisher.security import (
    media_url,
)
from instagram_publisher.serializers import draft_values, post_dict

router = APIRouter()


@router.post(
    "/api/posts", status_code=201, dependencies=[Depends(require_admin)], tags=["Drafts and approval"]
)
def create_post(
    request: Request, draft: PostDraft, idempotency_key: Annotated[str | None, Header(max_length=128)] = None
):
    session_factory = request.app.state.session_factory
    values = draft_values(draft)
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    with session_factory() as session:
        if idempotency_key:
            existing = session.scalar(select(Post).where(Post.idempotency_key == idempotency_key))
            if existing:
                if existing.request_hash != digest:
                    raise HTTPException(409, "This Idempotency-Key was already used for a different draft")
                return post_dict(existing)
        validate_assets(session, draft)
        post = Post(**values, idempotency_key=idempotency_key, request_hash=digest)
        session.add(post)
        try:
            session.flush()
            audit(session, "post.created", post.id)
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            # A simultaneous identical request may have created the draft first.
            existing = (
                session.scalar(select(Post).where(Post.idempotency_key == idempotency_key))
                if idempotency_key
                else None
            )
            if existing and existing.request_hash == digest:
                return post_dict(existing)
            raise HTTPException(409, "A conflicting draft already exists") from exc
        return post_dict(post)


@router.get("/api/posts", dependencies=[Depends(require_admin)], tags=["Drafts and approval"])
def posts(
    request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50, offset: Annotated[int, Query(ge=0)] = 0
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        return [
            post_dict(p)
            for p in session.scalars(
                select(Post).order_by(Post.created_at.desc()).limit(limit).offset(offset)
            )
        ]


@router.get("/api/posts/{post_id}", dependencies=[Depends(require_admin)], tags=["Drafts and approval"])
def get_post(request: Request, post_id: str):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        post = session.get(Post, post_id)
        if post is None:
            raise HTTPException(404, "Post not found")
        return post_dict(post)


@router.put("/api/posts/{post_id}", dependencies=[Depends(require_admin)], tags=["Drafts and approval"])
def edit_post(request: Request, post_id: str, draft: DraftUpdate):
    session_factory = request.app.state.session_factory
    with session_factory.begin() as session:
        post = session.get(Post, post_id)
        if post is None:
            raise HTTPException(404, "Post not found")
        validate_assets(session, draft)
        result = session.execute(
            update(Post)
            .where(
                Post.id == post_id,
                Post.status == "DRAFT",
                Post.revision == draft.revision,
            )
            .values(**draft_values(draft), revision=draft.revision + 1, updated_at=time.time())
        )
        if result.rowcount != 1:
            raise HTTPException(409, "Only the current revision of a draft can be edited")
        audit(session, "post.edited", post_id)
        session.expire_all()
        return post_dict(session.get(Post, post_id))


@router.get(
    "/api/posts/{post_id}/review", dependencies=[Depends(require_admin)], tags=["Drafts and approval"]
)
def review(request: Request, post_id: str):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        post = session.get(Post, post_id)
        if post is None:
            raise HTTPException(404, "Post not found")
        account = session.get(Account, post.account_id) if post.account_id else None
        warnings = []
        if settings.publisher_mode == "demo":
            warnings.append("Demo mode: approval simulates publication and never contacts Instagram")
        elif not account:
            warnings.append("Connect and select an Instagram account before approval")
        if post.kind == "STORY":
            warnings.append("API Stories do not include interactive link or question stickers")
        return {
            "post": post_dict(post),
            "account": account_dict(account) if account else None,
            "mode": settings.publisher_mode,
            "warnings": warnings,
            "preview_urls": [media_url(a, settings, expires=int(time.time()) + 3600) for a in post.asset_ids],
            "cover_preview_url": media_url(post.cover_asset_id, settings, expires=int(time.time()) + 3600)
            if post.cover_asset_id
            else None,
            "approval": {
                "method": "POST",
                "path": f"/api/posts/{post_id}/approve",
                "body": {"revision": post.revision},
            },
        }


@router.post(
    "/api/posts/{post_id}/approve", dependencies=[Depends(require_admin)], tags=["Drafts and approval"]
)
def approve(request: Request, post_id: str, approval: RevisionRequest):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    with session_factory.begin() as session:
        post = session.get(Post, post_id)
        if post is None:
            raise HTTPException(404, "Post not found")
        if (
            post.status == "QUEUED"
            and post.approved_revision == approval.revision
            and post.approved_mode == settings.publisher_mode
        ):
            return post_dict(post)
        if post.status != "DRAFT" or post.revision != approval.revision:
            raise HTTPException(409, "Review the latest draft revision before approval")
        validate_assets(session, post)
        if settings.publisher_mode == "live":
            account = session.get(Account, post.account_id) if post.account_id else None
            if not credentials_valid(account, max(time.time(), post.scheduled_at or 0)):
                raise HTTPException(409, "Select an account with a token valid through the scheduled time")
            if post.kind == "STORY" and (account.account_type or "").upper() != "BUSINESS":
                raise HTTPException(422, "Story publishing requires an Instagram Business account")
        result = session.execute(
            update(Post)
            .where(
                Post.id == post_id,
                Post.status == "DRAFT",
                Post.revision == approval.revision,
            )
            .values(
                status="QUEUED",
                approved_revision=approval.revision,
                approved_mode=settings.publisher_mode,
                approved_at=time.time(),
                scheduled_at=post.scheduled_at or time.time(),
                updated_at=time.time(),
            )
        )
        if result.rowcount != 1:
            raise HTTPException(409, "The draft changed; review it again")
        audit(
            session,
            "post.approved",
            post_id,
            f"revision={approval.revision}; mode={settings.publisher_mode}",
        )
        session.expire_all()
        return post_dict(session.get(Post, post_id))


@router.post(
    "/api/posts/{post_id}/cancel", dependencies=[Depends(require_admin)], tags=["Drafts and approval"]
)
def cancel(request: Request, post_id: str):
    session_factory = request.app.state.session_factory
    with session_factory.begin() as session:
        post = session.get(Post, post_id)
        if post is None:
            raise HTTPException(404, "Post not found")
        if post.status == "CANCELED":
            return post_dict(post)
        result = session.execute(
            update(Post)
            .where(
                Post.id == post_id,
                Post.status.in_(["DRAFT", "QUEUED", "FAILED"]),
                Post.publish_requested_at.is_(None),
            )
            .values(status="CANCELED", updated_at=time.time())
        )
        if result.rowcount != 1:
            raise HTTPException(409, "A processing or published post cannot be canceled")
        audit(session, "post.canceled", post_id)
        session.expire_all()
        return post_dict(session.get(Post, post_id))


@router.post(
    "/api/posts/{post_id}/retry", dependencies=[Depends(require_admin)], tags=["Drafts and approval"]
)
def retry(request: Request, post_id: str):
    session_factory = request.app.state.session_factory
    with session_factory.begin() as session:
        result = session.execute(
            update(Post)
            .where(
                Post.id == post_id,
                Post.status == "FAILED",
                Post.publish_requested_at.is_(None),
            )
            .values(
                status="DRAFT",
                revision=Post.revision + 1,
                approved_revision=None,
                approved_mode=None,
                approved_at=None,
                container_id=None,
                child_container_ids=[],
                error=None,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=time.time(),
            )
        )
        if result.rowcount != 1:
            raise HTTPException(409, "Only failed posts with no publication attempt can return to draft")
        audit(session, "post.returned_to_draft", post_id)
        session.expire_all()
        return post_dict(session.get(Post, post_id))
