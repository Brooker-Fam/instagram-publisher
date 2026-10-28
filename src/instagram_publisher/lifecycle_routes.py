import hashlib
import json
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert

from instagram_publisher.db import (
    Account,
    AuditEvent,
    CallbackReceipt,
    Post,
    audit,
)

router = APIRouter()


def lifecycle_callback(request: Request, signed_request: str, kind: str):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    from instagram_publisher.lifecycle import verify_signed_request

    if not settings.meta_app_secret.get_secret_value():
        raise HTTPException(503, "Instagram is not configured")
    try:
        payload = verify_signed_request(signed_request, settings.meta_app_secret.get_secret_value())
    except ValueError as exc:
        raise HTTPException(400, "Invalid Instagram callback signature") from exc
    # Fingerprint the verified claims, not the signature's base64 padding.
    digest = hashlib.sha256(
        (kind + ":" + json.dumps(payload, sort_keys=True, separators=(",", ":"))).encode()
    ).hexdigest()
    with session_factory.begin() as session:
        code = secrets.token_urlsafe(24)
        result = session.execute(
            insert(CallbackReceipt)
            .values(
                digest=digest,
                kind=kind,
                confirmation_code=code,
            )
            .on_conflict_do_nothing(index_elements=[CallbackReceipt.digest])
        )
        receipt = session.get(CallbackReceipt, digest)
        code = receipt.confirmation_code
        if result.rowcount:
            # The write lock precedes account inspection; duplicate deliveries
            # cannot disconnect an account that was reconnected later.
            account = session.scalar(
                select(Account).where(
                    Account.auth_provider == "INSTAGRAM",
                    Account.issuer_app_id.in_([settings.meta_app_id, ""]),
                    Account.app_scoped_id == str(payload["user_id"]),
                )
            )
            issued_at = payload.get("issued_at")
            if account and (issued_at is None or float(issued_at) >= account.connected_at - 1):
                if kind == "delete":
                    post_ids = list(session.scalars(select(Post.id).where(Post.account_id == account.id)))
                    session.execute(delete(AuditEvent).where(AuditEvent.post_id.in_(post_ids)))
                    session.execute(delete(Post).where(Post.id.in_(post_ids)))
                    session.delete(account)
                else:
                    account.token_ciphertext = ""
                    account.token_expires_at = 0
                    session.execute(
                        update(Post)
                        .where(
                            Post.account_id == account.id,
                            Post.status.in_(["DRAFT", "QUEUED", "FAILED"]),
                        )
                        .values(
                            status="CANCELED",
                            updated_at=time.time(),
                            error="Instagram connection was deauthorized",
                        )
                    )
                audit(session, "account.data_deleted" if kind == "delete" else "account.deauthorized")
            elif account:
                receipt.status = "ignored_stale"
    return {
        "url": f"{settings.public_base_url.rstrip('/')}/auth/instagram/deletion-status/{code}",
        "confirmation_code": code,
    }


@router.post("/auth/instagram/deauthorize", include_in_schema=False)
def deauthorize(request: Request, signed_request: Annotated[str, Form(max_length=16384)]):
    lifecycle_callback(request, signed_request, "deauthorize")
    return {"status": "ok"}


@router.post("/auth/instagram/data-deletion", include_in_schema=False)
def delete_instagram_data(request: Request, signed_request: Annotated[str, Form(max_length=16384)]):
    return lifecycle_callback(request, signed_request, "delete")


@router.get("/auth/instagram/deletion-status/{confirmation_code}", include_in_schema=False)
def deletion_status(request: Request, confirmation_code: str):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        receipt = session.scalar(
            select(CallbackReceipt).where(CallbackReceipt.confirmation_code == confirmation_code)
        )
        if not receipt:
            raise HTTPException(404, "Deletion request not found")
        return {"confirmation_code": confirmation_code, "status": receipt.status}
