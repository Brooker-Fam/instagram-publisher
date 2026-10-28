"""Durable, deliberately conservative Instagram publishing worker.

The queue is the database, not an in-memory timer. An approved revision is claimed
atomically, and the publish intention is committed before sending media_publish.
Unknown outcomes are held for human reconciliation instead of being retried.
"""

import argparse
import logging
import signal
import threading
import time
import uuid
from pathlib import Path

from cryptography.fernet import InvalidToken
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert

from maple_publisher.config import Settings
from maple_publisher.db import Account, Asset, Post, WorkerHeartbeat, audit, make_session_factory
from maple_publisher.meta import MetaClient, MetaError
from maple_publisher.security import decrypt_token, encrypt_token, media_url

logger = logging.getLogger(__name__)


class PublishingError(Exception):
    """A safe, actionable error generated locally, with no provider secrets."""


class LeaseLost(PublishingError):
    pass


class PublicationUncertain(PublishingError):
    pass


def _check_shutdown(stop_event: threading.Event | None):
    if stop_event is not None and stop_event.is_set():
        raise PublishingError("Worker shutdown requested. Review this post before retrying.")


def _wait_interruptibly(seconds, sleep, stop_event: threading.Event | None):
    _check_shutdown(stop_event)
    if stop_event is not None and sleep is time.sleep:
        stop_event.wait(seconds)
    else:
        sleep(seconds)
    _check_shutdown(stop_event)


def _heartbeat(session_factory, mode: str):
    now = time.time()
    with session_factory.begin() as session:
        session.execute(
            insert(WorkerHeartbeat)
            .values(id="publisher", updated_at=now, mode=mode)
            .on_conflict_do_update(
                index_elements=[WorkerHeartbeat.id], set_={"updated_at": now, "mode": mode}
            )
        )


def refresh_due_accounts(settings, session_factory, client=None, stop_event=None) -> int:
    """Refresh eligible long-lived tokens with an hourly attempt lease.

    A maintenance heartbeat is distinct from token_refreshed_at: a failed
    provider call must not make a token appear fresh. Its atomic upsert also
    prevents two worker processes refreshing the same account simultaneously.
    """
    if settings.publisher_mode != "live" or (stop_event is not None and stop_event.is_set()):
        return 0
    now = time.time()
    with session_factory() as session:
        accounts = session.scalars(
            select(Account).where(
                Account.token_expires_at > now,
                Account.token_expires_at < now + 7 * 86400,
                Account.token_refreshed_at < now - 86460,
            )
        ).all()
    if not accounts:
        return 0
    owned_client = False
    refreshed = 0
    try:
        for account in accounts:
            if stop_event is not None and stop_event.is_set():
                break
            with session_factory.begin() as session:
                lease = session.execute(
                    insert(WorkerHeartbeat)
                    .values(id=f"r:{account.id}", updated_at=now, mode="live")
                    .on_conflict_do_update(
                        index_elements=[WorkerHeartbeat.id],
                        set_={"updated_at": now, "mode": "live"},
                        where=WorkerHeartbeat.updated_at < now - 3600,
                    )
                )
                if not lease.rowcount:
                    continue
            try:
                if client is None:
                    client = MetaClient(
                        app_id=settings.meta_app_id,
                        app_secret=settings.meta_app_secret.get_secret_value(),
                        redirect_uri=settings.redirect_uri,
                        api_version=settings.meta_api_version,
                    )
                    owned_client = True
                key = settings.token_encryption_key.get_secret_value()
                _check_shutdown(stop_event)
                result = client.refresh_token(decrypt_token(account.token_ciphertext, key))
                token = result.get("access_token")
                expires_in = result.get("expires_in")
                if not isinstance(token, str) or not token or not isinstance(expires_in, int) or expires_in <= 0:
                    raise PublishingError("Instagram returned incomplete token refresh data.")
                completed_at = time.time()
                with session_factory.begin() as session:
                    changed = session.execute(
                        update(Account)
                        .where(
                            Account.id == account.id,
                            Account.token_ciphertext == account.token_ciphertext,
                            Account.token_refreshed_at == account.token_refreshed_at,
                        )
                        .values(
                            token_ciphertext=encrypt_token(token, key),
                            token_expires_at=completed_at + expires_in,
                            token_refreshed_at=completed_at,
                        )
                    )
                    if changed.rowcount:
                        audit(session, "account_token_refreshed", detail=account.id)
                        refreshed += 1
            except Exception:
                # Neither credentials nor provider URLs/bodies belong in logs.
                logger.warning("Instagram account %s token refresh failed; retry is deferred", account.id)
                with session_factory.begin() as session:
                    audit(session, "account_token_refresh_failed", detail=account.id)
    finally:
        if owned_client:
            client.close()
    return refreshed


def _recover_expired(session_factory):
    now = time.time()
    with session_factory.begin() as session:
        rows = session.scalars(
            select(Post).where(
                Post.status == "PROCESSING",
                or_(Post.lease_expires_at <= now, Post.lease_expires_at.is_(None)),
            )
        ).all()
        for row in rows:
            ambiguous = row.publish_requested_at is not None
            status = "NEEDS_ATTENTION" if ambiguous else "FAILED"
            error = (
                "Worker stopped after publication was requested. Check Instagram and reconcile this post; "
                "it will not be published again automatically."
                if ambiguous
                else "Worker stopped before publication. Review the post before retrying."
            )
            result = session.execute(
                update(Post)
                .where(
                    Post.id == row.id,
                    Post.status == "PROCESSING",
                    or_(Post.lease_expires_at <= now, Post.lease_expires_at.is_(None)),
                )
                .values(status=status, error=error, lease_owner=None, lease_expires_at=None, updated_at=now)
            )
            if result.rowcount:
                audit(session, "worker_lease_expired", row.id, status)


def _claim(settings, session_factory) -> tuple[str, str] | None:
    now = time.time()
    eligible = (
        Post.status == "QUEUED",
        Post.approved_revision == Post.revision,
        Post.approved_mode == settings.publisher_mode,
        Post.publish_requested_at.is_(None),
        or_(Post.scheduled_at.is_(None), Post.scheduled_at <= now),
    )
    with session_factory() as session:
        candidates = list(
            session.scalars(
                select(Post.id).where(*eligible).order_by(Post.scheduled_at, Post.created_at).limit(20)
            )
        )
    for post_id in candidates:
        owner = str(uuid.uuid4())
        with session_factory.begin() as session:
            result = session.execute(
                update(Post)
                .where(Post.id == post_id, *eligible)
                .values(
                    status="PROCESSING",
                    lease_owner=owner,
                    lease_expires_at=now + settings.worker_lease_seconds,
                    error=None,
                    updated_at=now,
                )
            )
            if result.rowcount:
                audit(session, "worker_claimed", post_id, settings.publisher_mode)
                return post_id, owner
    return None


def _fence(settings, session_factory, post_id: str, owner: str, **fields):
    """Refresh the lease, atomically requiring that this worker still owns it."""
    now = time.time()
    with session_factory.begin() as session:
        result = session.execute(
            update(Post)
            .where(
                Post.id == post_id,
                Post.status == "PROCESSING",
                Post.lease_owner == owner,
                Post.lease_expires_at > now,
                Post.approved_revision == Post.revision,
                Post.approved_mode == settings.publisher_mode,
            )
            .values(lease_expires_at=now + settings.worker_lease_seconds, updated_at=now, **fields)
        )
        if not result.rowcount:
            raise LeaseLost("Publishing lease expired or changed. Review the post before taking further action.")
    _heartbeat(session_factory, settings.publisher_mode)


def _complete(settings, session_factory, post_id: str, owner: str, status: str, **fields):
    now = time.time()
    with session_factory.begin() as session:
        result = session.execute(
            update(Post)
            .where(
                Post.id == post_id,
                Post.status == "PROCESSING",
                Post.lease_owner == owner,
                Post.lease_expires_at > now,
                Post.approved_revision == Post.revision,
                Post.approved_mode == settings.publisher_mode,
            )
            .values(status=status, lease_owner=None, lease_expires_at=None, updated_at=now, **fields)
        )
        if not result.rowcount:
            raise LeaseLost("Publishing result needs reconciliation because its worker lease changed.")
        audit(session, "worker_completed", post_id, status)


def _mark_failure(session_factory, post_id: str, owner: str, exc: Exception):
    if isinstance(exc, LeaseLost):
        # The current owner or lease recovery is responsible for the record.
        return
    if isinstance(exc, (PublishingError, MetaError)):
        message = str(exc)[:600]
    elif isinstance(exc, InvalidToken):
        message = "Stored account credentials could not be decrypted. Restore the encryption key or reconnect."
    else:
        # Never store repr(exc), request URLs or provider response bodies.
        message = "Publishing encountered an unexpected error. Check the service and review this post."
    with session_factory.begin() as session:
        row = session.get(Post, post_id)
        if row is None or row.status != "PROCESSING" or row.lease_owner != owner:
            return
        status = (
            "NEEDS_ATTENTION"
            if row.publish_requested_at is not None or isinstance(exc, PublicationUncertain)
            else "FAILED"
        )
        if status == "NEEDS_ATTENTION":
            message = (
                "Instagram may have received this publication. Check Instagram before taking further action. "
                "Automatic retry is disabled. " + message
            )
        result = session.execute(
            update(Post)
            .where(Post.id == post_id, Post.status == "PROCESSING", Post.lease_owner == owner)
            .values(status=status, error=message, lease_owner=None, lease_expires_at=None, updated_at=time.time())
        )
        if result.rowcount:
            audit(session, "worker_failed", post_id, status)


def _load_and_validate(settings, session_factory, post_id: str):
    with session_factory() as session:
        post = session.get(Post, post_id)
        if post is None:
            raise PublishingError("Post no longer exists.")
        if post.kind not in {"IMAGE", "CAROUSEL", "REEL", "STORY"}:
            raise PublishingError("Unsupported post type.")
        expected = range(2, 11) if post.kind == "CAROUSEL" else (1,)
        if len(post.asset_ids) not in expected or len(set(post.asset_ids)) != len(post.asset_ids):
            raise PublishingError("Post contains an invalid number of media assets.")
        assets = [session.get(Asset, asset_id) for asset_id in post.asset_ids]
        cover = session.get(Asset, post.cover_asset_id) if post.cover_asset_id else None
        if post.cover_asset_id and (post.kind != "REEL" or cover is None):
            raise PublishingError("Only Reels can have a cover, and that cover must exist.")
        for asset in assets + ([cover] if cover else []):
            if asset is None:
                raise PublishingError("A media asset is missing. Upload it again and review the draft.")
            if Path(asset.storage_key).name != asset.storage_key:
                raise PublishingError("A media storage reference is invalid.")
            path = settings.media_root / asset.storage_key
            if not path.is_file() or path.stat().st_size != asset.size_bytes:
                raise PublishingError("A media file is missing or has changed. Restore it before retrying.")
        if post.kind == "IMAGE" and assets[0].mime_type != "image/jpeg":
            raise PublishingError("Feed images must use a prepared JPEG asset.")
        if post.kind == "REEL" and assets[0].mime_type != "video/mp4":
            raise PublishingError("Reels require an MP4 video, not a cover image.")
        if cover and cover.mime_type != "image/jpeg":
            raise PublishingError("A Reel cover must use a prepared JPEG asset.")
        for asset in assets:
            if asset.mime_type not in {"image/jpeg", "video/mp4"}:
                raise PublishingError("Unsupported prepared media type.")
            if not asset.width or not asset.height:
                raise PublishingError("Media dimensions are missing.")
            ratio = asset.width / asset.height
            if post.kind in {"IMAGE", "CAROUSEL"} and not 0.8 <= ratio <= 1.91:
                raise PublishingError("Feed media must have an aspect ratio between 4:5 and 1.91:1.")
            if post.kind == "STORY":
                if abs(ratio / (9 / 16) - 1) > 0.01:
                    raise PublishingError("Stories must use a 9:16 asset.")
                if asset.mime_type == "video/mp4" and (asset.duration is None or not 3 <= asset.duration <= 60):
                    raise PublishingError("Story videos must be between 3 and 60 seconds.")
            if post.kind == "CAROUSEL" and asset.mime_type == "video/mp4":
                if asset.duration is None or not 3 <= asset.duration <= 60:
                    raise PublishingError("Carousel videos must be between 3 and 60 seconds.")
        account = session.get(Account, post.account_id) if post.account_id else None
        if settings.publisher_mode == "live":
            if account is None:
                raise PublishingError("Connect an Instagram professional account before publishing.")
            if account.token_expires_at <= time.time() + 600:
                raise PublishingError("Instagram authorization is expired or expires soon. Refresh or reconnect first.")
            account_type = account.account_type.upper().replace(" ", "_")
            if account_type not in {"BUSINESS", "MEDIA_CREATOR", "CREATOR"}:
                raise PublishingError("Publishing requires an Instagram professional account.")
            if post.kind == "STORY" and account_type != "BUSINESS":
                raise PublishingError("Automatic Story publishing requires an Instagram Business account.")
        return post, assets, cover, account


def _check_quota(result):
    rows = result.get("data", []) if isinstance(result, dict) else []
    if not rows or not isinstance(rows[0], dict):
        raise PublishingError("Instagram did not return its publishing quota. Try again after checking the account.")
    entry = rows[0]
    quota = entry.get("config", {}).get("quota_total")
    usage = entry.get("quota_usage")
    if not isinstance(quota, (int, float)) or not isinstance(usage, (int, float)) or quota <= 0 or usage < 0:
        raise PublishingError("Instagram returned an unrecognized publishing quota.")
    if usage >= quota:
        raise PublishingError("Instagram's rolling publishing limit has been reached. Review and reschedule later.")


def _wait_for_container(
    settings, session_factory, post_id, owner, client, container_id, token, sleep, stop_event=None
):
    for attempt in range(settings.container_poll_attempts):
        _check_shutdown(stop_event)
        _fence(settings, session_factory, post_id, owner)
        _check_shutdown(stop_event)
        result = client.container_status(container_id, token)
        status = str(result.get("status_code", "")).upper()
        if status == "FINISHED":
            return
        if status == "PUBLISHED":
            # This should be impossible for a freshly created container. Never publish it twice.
            raise PublicationUncertain("Instagram reports this container was already published. Check the account.")
        if status in {"ERROR", "EXPIRED"}:
            raise PublishingError(f"Instagram media processing ended with {status}. Review the media and retry.")
        if status != "IN_PROGRESS":
            raise PublishingError("Instagram returned an unknown media-processing status.")
        if attempt + 1 < settings.container_poll_attempts:
            _wait_interruptibly(settings.container_poll_seconds, sleep, stop_event)
    raise PublishingError("Instagram media is still processing. Publication was not requested; review before retrying.")


def _publish(settings, session_factory, post_id, owner, client, sleep, stop_event=None):
    _check_shutdown(stop_event)
    post, assets, cover, account = _load_and_validate(settings, session_factory, post_id)
    if settings.publisher_mode == "demo":
        _complete(settings, session_factory, post_id, owner, "DEMO_PUBLISHED", published_at=time.time())
        return
    token = decrypt_token(account.token_ciphertext, settings.token_encryption_key.get_secret_value())
    _fence(settings, session_factory, post_id, owner)
    _check_shutdown(stop_event)
    _check_quota(client.publishing_limit(account.instagram_user_id, token))

    def create(**fields):
        _check_shutdown(stop_event)
        _fence(settings, session_factory, post_id, owner)
        _check_shutdown(stop_event)
        return client.create_container(account.instagram_user_id, token, **fields)

    if post.kind == "CAROUSEL":
        children = []
        for asset in assets:
            fields = {"is_carousel_item": True}
            if asset.mime_type == "image/jpeg":
                fields["image_url"] = media_url(asset.id, settings)
            else:
                fields.update(media_type="VIDEO", video_url=media_url(asset.id, settings))
            child_id = create(**fields)
            children.append(child_id)
            _fence(settings, session_factory, post_id, owner, child_container_ids=list(children))
            _wait_for_container(
                settings, session_factory, post_id, owner, client, child_id, token, sleep, stop_event
            )
        fields = {"media_type": "CAROUSEL", "children": ",".join(children), "caption": post.caption}
    elif post.kind == "REEL":
        fields = {
            "media_type": "REELS",
            "video_url": media_url(assets[0].id, settings),
            "caption": post.caption,
            "share_to_feed": post.share_to_feed,
        }
        if cover:
            fields["cover_url"] = media_url(cover.id, settings)
    elif post.kind == "STORY":
        field = "image_url" if assets[0].mime_type == "image/jpeg" else "video_url"
        fields = {"media_type": "STORIES", field: media_url(assets[0].id, settings)}
    else:
        fields = {"image_url": media_url(assets[0].id, settings), "caption": post.caption}
        if post.alt_text:
            fields["alt_text"] = post.alt_text
    container_id = create(**fields)
    _fence(settings, session_factory, post_id, owner, container_id=container_id)
    _wait_for_container(
        settings, session_factory, post_id, owner, client, container_id, token, sleep, stop_event
    )
    # Commit before the call. A timeout, process kill, or DB outage after this line
    # is an uncertain external side effect, never an invitation to repeat a POST.
    _check_shutdown(stop_event)
    _fence(settings, session_factory, post_id, owner, publish_requested_at=time.time())
    _check_shutdown(stop_event)
    published_id = client.publish_container(account.instagram_user_id, token, container_id)
    if not published_id:
        raise PublishingError("Instagram did not return a published media ID.")
    _complete(
        settings,
        session_factory,
        post_id,
        owner,
        "PUBLISHED",
        published_media_id=published_id,
        published_at=time.time(),
    )
    if stop_event is not None and stop_event.is_set():
        return
    try:
        # Publication is already durable; an optional link lookup must never
        # change a published record to FAILED or cause another publication.
        info = client.get_media(published_id, token)
        permalink = info.get("permalink")
        if isinstance(permalink, str) and permalink.startswith("https://"):
            with session_factory.begin() as session:
                session.execute(
                    update(Post)
                    .where(Post.id == post_id, Post.status == "PUBLISHED", Post.published_media_id == published_id)
                    .values(permalink=permalink, updated_at=time.time())
                )
    except Exception:
        logger.warning("Published post %s; its optional permalink is not yet available", post_id)


def run_once(settings, session_factory, client=None, sleep=time.sleep, stop_event=None) -> bool:
    """Claim and process at most one due post. No implicit provider retries."""
    _heartbeat(session_factory, settings.publisher_mode)
    _recover_expired(session_factory)
    if stop_event is not None and stop_event.is_set():
        return False
    refresh_due_accounts(settings, session_factory, client=client, stop_event=stop_event)
    if stop_event is not None and stop_event.is_set():
        return False
    claimed = _claim(settings, session_factory)
    if claimed is None:
        return False
    post_id, owner = claimed
    owned_client = False
    try:
        if settings.publisher_mode == "live" and client is None:
            client = MetaClient(
                app_id=settings.meta_app_id,
                app_secret=settings.meta_app_secret.get_secret_value(),
                redirect_uri=settings.redirect_uri,
                api_version=settings.meta_api_version,
            )
            owned_client = True
        _publish(settings, session_factory, post_id, owner, client, sleep, stop_event)
    except Exception as exc:
        _mark_failure(session_factory, post_id, owner, exc)
        logger.warning("Post %s stopped; inspect its stored publishing status", post_id)
    finally:
        if owned_client:
            client.close()
        _heartbeat(session_factory, settings.publisher_mode)
    return True


def main():
    parser = argparse.ArgumentParser(description="Run Maple Baby's durable Instagram publishing queue")
    parser.add_argument("--once", action="store_true", help="Process at most one due approved post and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # OAuth token exchanges contain secrets in query parameters by Meta design.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    settings = Settings()
    session_factory = make_session_factory(settings.database_url)
    stop = threading.Event()

    def shutdown(*_):
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    while True:
        try:
            worked = run_once(settings, session_factory, stop_event=stop)
        except Exception:
            # A DB outage should not emit configuration or exception request data.
            logger.error("Worker could not access the queue. Check storage and configuration.")
            worked = False
        if args.once or stop.is_set():
            break
        if not worked:
            stop.wait(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
