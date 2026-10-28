import hashlib
import html
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

from cryptography.fernet import InvalidToken
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError

from instagram_publisher.config import Settings
from instagram_publisher.db import (
    Account,
    Asset,
    AuditEvent,
    CallbackReceipt,
    OAuthState,
    Post,
    WorkerHeartbeat,
    audit,
    make_session_factory,
)
from instagram_publisher.guards import RequestGuard
from instagram_publisher.media import (
    MAX_VIDEO_BYTES,
    MediaValidationError,
    storage_path,
    store_upload,
    verify_media_signature,
)
from instagram_publisher.meta import MetaClient, MetaError
from instagram_publisher.schemas import DraftUpdate, PostDraft, RevisionRequest
from instagram_publisher.security import (
    decrypt_token,
    encrypt_token,
    media_url,
    state_digest,
    valid_admin_key,
)

bearer = HTTPBearer(auto_error=False)


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def asset_dict(asset):
    return {key: getattr(asset, key) for key in (
        "id", "mime_type", "size_bytes", "width", "height", "duration", "sha256"
    )} | {"created_at": iso(asset.created_at)}


def account_dict(account):
    return {key: getattr(account, key) for key in (
        "id", "instagram_user_id", "username", "account_type"
    )} | {"token_expires_at": iso(account.token_expires_at), "created_at": iso(account.created_at)}


def post_dict(post):
    values = {key: getattr(post, key) for key in (
        "id", "account_id", "kind", "asset_ids", "cover_asset_id", "caption", "alt_text",
        "share_to_feed", "status", "revision", "approved_revision", "approved_mode", "container_id",
        "published_media_id", "permalink", "error",
    )}
    values.update({key: iso(getattr(post, key)) for key in (
        "scheduled_at", "approved_at", "published_at", "created_at", "updated_at", "publish_requested_at"
    )})
    return values


def draft_values(draft):
    values = draft.model_dump(exclude={"revision"})
    if values["scheduled_at"] is not None:
        values["scheduled_at"] = values["scheduled_at"].timestamp()
    return values


def validate_assets(session, draft):
    for asset_id in draft.asset_ids:
        asset = session.get(Asset, asset_id)
        if asset is None:
            raise HTTPException(422, "An asset does not exist")
        is_image = asset.mime_type == "image/jpeg"
        if draft.kind in {"IMAGE", "CAROUSEL"}:
            if not is_image or not 0.8 <= asset.width / asset.height <= 1.91:
                raise HTTPException(422, "Feed posts need JPEG images with a 4:5 to 1.91:1 aspect ratio")
        elif draft.kind == "REEL":
            if asset.mime_type != "video/mp4" or not asset.duration or not 3 <= asset.duration <= 900:
                raise HTTPException(422, "A Reel needs an MP4 video lasting 3 seconds to 15 minutes")
        elif draft.kind == "STORY":
            if abs((asset.width / asset.height) / (9 / 16) - 1) > 0.01:
                raise HTTPException(422, "Stories in this service require 9:16 media")
            if not is_image and (not asset.duration or not 3 <= asset.duration <= 60):
                raise HTTPException(422, "Story videos must last 3 to 60 seconds")
    if draft.cover_asset_id:
        cover = session.get(Asset, draft.cover_asset_id)
        if cover is None or cover.mime_type != "image/jpeg":
            raise HTTPException(422, "The Reel cover must be an uploaded JPEG image")
    if draft.account_id and session.get(Account, draft.account_id) is None:
        raise HTTPException(422, "The selected Instagram account does not exist")


def create_app(settings: Settings | None = None, session_factory=None, meta_client=None):
    settings = settings or Settings()
    session_factory = session_factory or make_session_factory(settings.database_url)
    settings.media_root.mkdir(parents=True, exist_ok=True)
    # OAuth token exchange endpoints put credentials in query parameters. Never log request URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    owned_client = meta_client is None
    client = meta_client or MetaClient(
        app_id=settings.meta_app_id,
        app_secret=settings.meta_app_secret.get_secret_value(),
        redirect_uri=settings.redirect_uri,
        api_version=settings.meta_api_version,
    )

    @asynccontextmanager
    async def lifespan(_app):
        yield
        if owned_client:
            client.close()

    app = FastAPI(
        title="Instagram Publisher",
        description=(
            "Private Instagram publishing API. Upload media, create a draft, review its revision, "
            "then approve to queue it. The separate worker performs publishing. Demo mode never posts "
            "to Instagram. Use Authorize with your admin API key; secrets never appear in responses."
        ),
        version="0.1.0", lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory

    def require_admin(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if credentials is None or not valid_admin_key(credentials.credentials, settings.admin_api_key.get_secret_value()):
            raise HTTPException(401, "A valid admin Bearer token is required", headers={"WWW-Authenticate": "Bearer"})

    admin = [Depends(require_admin)]

    @app.middleware("http")
    async def security_headers(request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            length = request.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > MAX_VIDEO_BYTES + 2_000_000):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Request body exceeds the upload limit"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if settings.public_base_url.startswith("https://"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.get("/", include_in_schema=False)
    def home():
        return RedirectResponse("/docs")

    @app.get("/healthz", tags=["Health"])
    def health():
        with session_factory() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ok", "mode": settings.publisher_mode}

    @app.get("/api/status", dependencies=admin, tags=["Health"])
    def status():
        with session_factory() as session:
            heartbeat = session.get(WorkerHeartbeat, "publisher")
            counts = session.execute(select(Post.status, func.count()).group_by(Post.status)).all()
            return {
                "mode": settings.publisher_mode,
                "instagram_configured": bool(settings.meta_app_id and settings.meta_app_secret.get_secret_value()),
                "redirect_uri": settings.redirect_uri,
                "worker_last_seen": iso(heartbeat.updated_at) if heartbeat else None,
                "worker_healthy": bool(heartbeat and heartbeat.mode == settings.publisher_mode and
                                       time.time() - heartbeat.updated_at < settings.worker_lease_seconds),
                "worker_mode": heartbeat.mode if heartbeat else None,
                "posts": dict(counts),
            }

    @app.post("/api/accounts/connect", dependencies=admin, tags=["Instagram connection"])
    def connect():
        if not settings.meta_app_id or not settings.meta_app_secret.get_secret_value():
            raise HTTPException(409, "Configure META_APP_ID and META_APP_SECRET first; see docs/meta-setup.md")
        state = secrets.token_urlsafe(32)
        with session_factory.begin() as session:
            session.execute(delete(OAuthState).where(OAuthState.expires_at < time.time() - 86400))
            session.add(OAuthState(digest=state_digest(state), expires_at=time.time() + 600))
        return {"authorization_url": client.authorization_url(state), "expires_in": 600}

    @app.get("/auth/instagram/callback", response_class=HTMLResponse, include_in_schema=False)
    def callback(
        state: Annotated[str, Query(max_length=512)],
        code: Annotated[str | None, Query(max_length=4096)] = None,
        error: Annotated[str | None, Query(max_length=200)] = None,
    ):
        with session_factory.begin() as session:
            result = session.execute(update(OAuthState).where(
                OAuthState.digest == state_digest(state), OAuthState.expires_at > time.time(),
                OAuthState.consumed_at.is_(None),
            ).values(consumed_at=time.time()))
            if result.rowcount != 1:
                raise HTTPException(400, "This connection link is invalid, expired, or already used. Start a new connection.")
        if error or not code:
            raise HTTPException(400, "Instagram authorization was not completed. Start a new connection to try again.")
        try:
            tokens = client.exchange_code(code)
            token = tokens["access_token"]
            profile = client.get_profile(token)
            instagram_id = str(profile.get("user_id") or profile["id"])
            username = str(profile["username"])
            app_scoped_id = profile.get("app_scoped_id") or tokens.get("user_id")
            if not app_scoped_id:
                raise HTTPException(502, "Instagram did not return the app-scoped account ID. Reconnect the account.")
            account_type = str(profile.get("account_type", "")).upper()
            if account_type not in {"BUSINESS", "CREATOR", "MEDIA_CREATOR"}:
                raise HTTPException(422, "Connect an Instagram Business or Creator account")
            with session_factory.begin() as session:
                account = session.scalar(select(Account).where(Account.instagram_user_id == instagram_id))
                if account is None:
                    account = Account(instagram_user_id=instagram_id)
                    session.add(account)
                account.username = username
                account.app_scoped_id = str(app_scoped_id)
                account.account_type = account_type
                account.token_ciphertext = encrypt_token(token, settings.token_encryption_key.get_secret_value())
                account.token_expires_at = time.time() + int(tokens["expires_in"])
                account.token_refreshed_at = time.time()
                account.connected_at = time.time()
                audit(session, "account.connected")
        except MetaError as exc:
            raise HTTPException(502, str(exc)) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(502, "Instagram returned an incomplete account response. Start a new connection.") from exc
        return HTMLResponse(
            "<!doctype html><html><head><title>Instagram connected</title></head><body>"
            f"<h1>Connected @{html.escape(username)}</h1>"
            "<p>You can close this window and return to Instagram Publisher. Connecting does not publish any posts.</p>"
            "</body></html>"
        )

    @app.get("/api/accounts", dependencies=admin, tags=["Instagram connection"])
    def accounts():
        with session_factory() as session:
            return [account_dict(a) for a in session.scalars(select(Account).order_by(Account.created_at))]

    @app.post("/api/accounts/{account_id}/refresh", dependencies=admin, tags=["Instagram connection"])
    def refresh(account_id: str):
        with session_factory() as session:
            account = session.get(Account, account_id)
            if not account:
                raise HTTPException(404, "Account not found")
            if account.token_expires_at <= time.time():
                raise HTTPException(409, "The token expired; reconnect Instagram")
            if time.time() - account.token_refreshed_at < 86400:
                raise HTTPException(409, "Instagram tokens must be at least 24 hours old before refresh")
            try:
                token = decrypt_token(account.token_ciphertext, settings.token_encryption_key.get_secret_value())
                result = client.refresh_token(token)
                refreshed_token = result["access_token"]
                lifetime = result["expires_in"]
                if not isinstance(refreshed_token, str) or not refreshed_token or not isinstance(lifetime, int) or lifetime <= 0:
                    raise ValueError("Invalid token response")
                encrypted = encrypt_token(refreshed_token, settings.token_encryption_key.get_secret_value())
            except MetaError as exc:
                raise HTTPException(502, str(exc)) from exc
            except InvalidToken as exc:
                raise HTTPException(409, "Stored credentials cannot be decrypted; restore the key or reconnect") from exc
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(502, "Instagram returned an incomplete access-token response") from exc
            old_ciphertext = account.token_ciphertext
            result = session.execute(update(Account).where(
                Account.id == account_id, Account.token_ciphertext == old_ciphertext,
            ).values(token_ciphertext=encrypted, token_expires_at=time.time() + lifetime, token_refreshed_at=time.time()))
            if not result.rowcount:
                raise HTTPException(409, "The account connection changed during refresh; reload its current status")
            audit(session, "account.refreshed")
            session.commit()
            session.expire_all()
            account = session.get(Account, account_id)
            return account_dict(account)

    @app.post("/api/assets", status_code=201, dependencies=admin, tags=["Media"])
    def upload(file: Annotated[UploadFile, File()], kind: Annotated[str, Form()] = "image"):
        chunks, size = [], 0
        try:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_VIDEO_BYTES:
                    raise HTTPException(413, "File exceeds the 100 MB upload limit")
                chunks.append(chunk)
            result = store_upload(b"".join(chunks), file.filename or "upload", settings.media_root, kind)
        except MediaValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            file.file.close()
        try:
            with session_factory.begin() as session:
                asset = Asset(**result)
                session.add(asset)
                session.flush()
                return asset_dict(asset)
        except Exception:
            storage_path(settings.media_root, result["storage_key"]).unlink(missing_ok=True)
            raise

    @app.get("/api/assets", dependencies=admin, tags=["Media"])
    def assets(limit: Annotated[int, Query(ge=1, le=100)] = 50, offset: Annotated[int, Query(ge=0)] = 0):
        with session_factory() as session:
            return [asset_dict(a) for a in session.scalars(select(Asset).order_by(Asset.created_at.desc()).limit(limit).offset(offset))]

    @app.get("/media/{asset_id}", include_in_schema=False)
    def serve_media(asset_id: str, expires: int, signature: str):
        if not verify_media_signature(asset_id, expires, signature, settings.media_signing_key.get_secret_value()):
            raise HTTPException(403, "Invalid or expired media link")
        with session_factory() as session:
            asset = session.get(Asset, asset_id)
            if not asset:
                raise HTTPException(404, "Media not found")
            path = storage_path(settings.media_root, asset.storage_key)
            if not path.is_file():
                raise HTTPException(404, "Media not found")
            return FileResponse(path, media_type=asset.mime_type)

    @app.post("/api/posts", status_code=201, dependencies=admin, tags=["Drafts and approval"])
    def create_post(draft: PostDraft, idempotency_key: Annotated[str | None, Header(max_length=128)] = None):
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
                existing = session.scalar(select(Post).where(Post.idempotency_key == idempotency_key)) if idempotency_key else None
                if existing and existing.request_hash == digest:
                    return post_dict(existing)
                raise HTTPException(409, "A conflicting draft already exists") from exc
            return post_dict(post)

    @app.get("/api/posts", dependencies=admin, tags=["Drafts and approval"])
    def posts(limit: Annotated[int, Query(ge=1, le=100)] = 50, offset: Annotated[int, Query(ge=0)] = 0):
        with session_factory() as session:
            return [post_dict(p) for p in session.scalars(select(Post).order_by(Post.created_at.desc()).limit(limit).offset(offset))]

    @app.get("/api/posts/{post_id}", dependencies=admin, tags=["Drafts and approval"])
    def get_post(post_id: str):
        with session_factory() as session:
            post = session.get(Post, post_id)
            if post is None:
                raise HTTPException(404, "Post not found")
            return post_dict(post)

    @app.put("/api/posts/{post_id}", dependencies=admin, tags=["Drafts and approval"])
    def edit_post(post_id: str, draft: DraftUpdate):
        with session_factory.begin() as session:
            post = session.get(Post, post_id)
            if post is None:
                raise HTTPException(404, "Post not found")
            validate_assets(session, draft)
            result = session.execute(update(Post).where(
                Post.id == post_id, Post.status == "DRAFT", Post.revision == draft.revision,
            ).values(**draft_values(draft), revision=draft.revision + 1, updated_at=time.time()))
            if result.rowcount != 1:
                raise HTTPException(409, "Only the current revision of a draft can be edited")
            audit(session, "post.edited", post_id)
            session.expire_all()
            return post_dict(session.get(Post, post_id))

    @app.get("/api/posts/{post_id}/review", dependencies=admin, tags=["Drafts and approval"])
    def review(post_id: str):
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
                "post": post_dict(post), "account": account_dict(account) if account else None,
                "mode": settings.publisher_mode, "warnings": warnings,
                "preview_urls": [media_url(a, settings, expires=int(time.time()) + 3600) for a in post.asset_ids],
                "cover_preview_url": media_url(post.cover_asset_id, settings, expires=int(time.time()) + 3600) if post.cover_asset_id else None,
                "approval": {"method": "POST", "path": f"/api/posts/{post_id}/approve", "body": {"revision": post.revision}},
            }

    @app.post("/api/posts/{post_id}/approve", dependencies=admin, tags=["Drafts and approval"])
    def approve(post_id: str, approval: RevisionRequest):
        with session_factory.begin() as session:
            post = session.get(Post, post_id)
            if post is None:
                raise HTTPException(404, "Post not found")
            if post.status == "QUEUED" and post.approved_revision == approval.revision and post.approved_mode == settings.publisher_mode:
                return post_dict(post)
            if post.status != "DRAFT" or post.revision != approval.revision:
                raise HTTPException(409, "Review the latest draft revision before approval")
            validate_assets(session, post)
            if settings.publisher_mode == "live":
                account = session.get(Account, post.account_id) if post.account_id else None
                if not account or account.token_expires_at <= max(time.time(), post.scheduled_at or 0):
                    raise HTTPException(409, "Select an account with a token valid through the scheduled time")
                if post.kind == "STORY" and account.account_type.upper() != "BUSINESS":
                    raise HTTPException(422, "Story publishing requires an Instagram Business account")
            result = session.execute(update(Post).where(
                Post.id == post_id, Post.status == "DRAFT", Post.revision == approval.revision,
            ).values(status="QUEUED", approved_revision=approval.revision, approved_mode=settings.publisher_mode,
                     approved_at=time.time(), scheduled_at=post.scheduled_at or time.time(), updated_at=time.time()))
            if result.rowcount != 1:
                raise HTTPException(409, "The draft changed; review it again")
            audit(session, "post.approved", post_id, f"revision={approval.revision}; mode={settings.publisher_mode}")
            session.expire_all()
            return post_dict(session.get(Post, post_id))

    @app.post("/api/posts/{post_id}/cancel", dependencies=admin, tags=["Drafts and approval"])
    def cancel(post_id: str):
        with session_factory.begin() as session:
            post = session.get(Post, post_id)
            if post is None:
                raise HTTPException(404, "Post not found")
            if post.status == "CANCELED":
                return post_dict(post)
            result = session.execute(update(Post).where(
                Post.id == post_id, Post.status.in_(["DRAFT", "QUEUED", "FAILED"]), Post.publish_requested_at.is_(None),
            ).values(status="CANCELED", updated_at=time.time()))
            if result.rowcount != 1:
                raise HTTPException(409, "A processing or published post cannot be canceled")
            audit(session, "post.canceled", post_id)
            session.expire_all()
            return post_dict(session.get(Post, post_id))

    @app.post("/api/posts/{post_id}/retry", dependencies=admin, tags=["Drafts and approval"])
    def retry(post_id: str):
        with session_factory.begin() as session:
            result = session.execute(update(Post).where(
                Post.id == post_id, Post.status == "FAILED", Post.publish_requested_at.is_(None),
            ).values(status="DRAFT", revision=Post.revision + 1, approved_revision=None, approved_mode=None,
                     approved_at=None, container_id=None, child_container_ids=[], error=None,
                     lease_owner=None, lease_expires_at=None, updated_at=time.time()))
            if result.rowcount != 1:
                raise HTTPException(409, "Only failed posts with no publication attempt can return to draft")
            audit(session, "post.returned_to_draft", post_id)
            session.expire_all()
            return post_dict(session.get(Post, post_id))

    @app.get("/api/events", dependencies=admin, tags=["Audit"])
    def events(limit: Annotated[int, Query(ge=1, le=200)] = 50):
        with session_factory() as session:
            return [{"id": e.id, "post_id": e.post_id, "action": e.action, "detail": e.detail,
                     "created_at": iso(e.created_at)}
                    for e in session.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit))]

    def remote_status(post_id: str):
        if settings.publisher_mode != "live":
            raise HTTPException(409, "Remote status is available in live mode only")
        with session_factory() as session:
            post = session.get(Post, post_id)
            if post is None:
                raise HTTPException(404, "Post not found")
            account = session.get(Account, post.account_id) if post.account_id else None
            if not post.container_id or not account or account.token_expires_at <= time.time():
                raise HTTPException(409, "This post needs a saved container and a current Instagram connection")
            try:
                token = decrypt_token(account.token_ciphertext, settings.token_encryption_key.get_secret_value())
                result = client.container_status(post.container_id, token)
            except MetaError as exc:
                raise HTTPException(502, str(exc)) from exc
            except InvalidToken as exc:
                raise HTTPException(409, "Restore the encryption key or reconnect Instagram") from exc
            status_code = result.get("status_code")
            if status_code not in {"IN_PROGRESS", "FINISHED", "PUBLISHED", "ERROR", "EXPIRED"}:
                raise HTTPException(502, "Instagram returned an unknown container status")
            return {"post_id": post_id, "container_id": post.container_id, "remote_status": status_code,
                    "local_status": post.status, "checked_at": iso(time.time()),
                    "note": "This check never submits a publication. FINISHED alone does not prove an earlier attempt failed."}

    app.get("/api/posts/{post_id}/remote-status", dependencies=admin, tags=["Recovery"])(remote_status)

    @app.post("/api/posts/{post_id}/reconcile", dependencies=admin, tags=["Recovery"])
    def reconcile(post_id: str):
        result = remote_status(post_id)
        if result["remote_status"] != "PUBLISHED":
            return result | {"resolved": False}
        with session_factory.begin() as session:
            changed = session.execute(update(Post).where(
                Post.id == post_id, Post.status == "NEEDS_ATTENTION", Post.container_id == result["container_id"],
            ).values(status="PUBLISHED", updated_at=time.time(),
                     error="Instagram confirmed publication; the original media ID and exact publication time were not returned."))
            if changed.rowcount:
                audit(session, "post.reconciled", post_id, "Meta container confirms PUBLISHED; no publication was resubmitted")
            return result | {"resolved": bool(changed.rowcount), "post": post_dict(session.get(Post, post_id))}

    def lifecycle_callback(signed_request: str, kind: str):
        from instagram_publisher.lifecycle import verify_signed_request

        if not settings.meta_app_secret.get_secret_value():
            raise HTTPException(503, "Instagram is not configured")
        try:
            payload = verify_signed_request(signed_request, settings.meta_app_secret.get_secret_value())
        except ValueError as exc:
            raise HTTPException(400, "Invalid Instagram callback signature") from exc
        # Fingerprint the verified claims, not the signature's base64 padding.
        digest = hashlib.sha256((kind + ":" + json.dumps(payload, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()
        with session_factory.begin() as session:
            code = secrets.token_urlsafe(24)
            result = session.execute(insert(CallbackReceipt).values(
                digest=digest, kind=kind, confirmation_code=code,
            ).on_conflict_do_nothing(index_elements=[CallbackReceipt.digest]))
            receipt = session.get(CallbackReceipt, digest)
            code = receipt.confirmation_code
            if result.rowcount:
                # The write lock precedes account inspection; duplicate deliveries
                # cannot disconnect an account that was reconnected later.
                account = session.scalar(select(Account).where(Account.app_scoped_id == str(payload["user_id"])))
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
                        session.execute(update(Post).where(
                            Post.account_id == account.id, Post.status.in_(["DRAFT", "QUEUED", "FAILED"]),
                        ).values(status="CANCELED", updated_at=time.time(), error="Instagram connection was deauthorized"))
                    audit(session, "account.data_deleted" if kind == "delete" else "account.deauthorized")
                elif account:
                    receipt.status = "ignored_stale"
        return {"url": f"{settings.public_base_url.rstrip('/')}/auth/instagram/deletion-status/{code}",
                "confirmation_code": code}

    @app.post("/auth/instagram/deauthorize", include_in_schema=False)
    def deauthorize(signed_request: Annotated[str, Form(max_length=16384)]):
        lifecycle_callback(signed_request, "deauthorize")
        return {"status": "ok"}

    @app.post("/auth/instagram/data-deletion", include_in_schema=False)
    def delete_instagram_data(signed_request: Annotated[str, Form(max_length=16384)]):
        return lifecycle_callback(signed_request, "delete")

    @app.get("/auth/instagram/deletion-status/{confirmation_code}", include_in_schema=False)
    def deletion_status(confirmation_code: str):
        with session_factory() as session:
            receipt = session.scalar(select(CallbackReceipt).where(CallbackReceipt.confirmation_code == confirmation_code))
            if not receipt:
                raise HTTPException(404, "Deletion request not found")
            return {"confirmation_code": confirmation_code, "status": receipt.status}

    app.add_middleware(RequestGuard, admin_key=settings.admin_api_key.get_secret_value(),
                       upload_limit=MAX_VIDEO_BYTES + 2_000_000)
    return app
