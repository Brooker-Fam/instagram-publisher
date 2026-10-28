"""Worker guarantees at the public run_once boundary, without contacting Meta."""

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest
from cryptography.fernet import Fernet
from PIL import Image
from sqlalchemy import select

from instagram_publisher.config import Settings
from instagram_publisher.db import Account, Asset, Post, make_session_factory
from instagram_publisher.meta import MetaError
from instagram_publisher.security import decrypt_token, encrypt_token
from instagram_publisher.worker import refresh_due_accounts, run_once


class FakeMeta:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.status = "FINISHED"
        self.quota_usage = 0
        self.quota_total = 50
        self.create_error = None
        self.publish_error = None
        self.media_error = None
        self._container_number = 0

    def record(self, method, *args, **fields):
        with self.lock:
            self.calls.append((method, args, fields))

    def publishing_limit(self, account_id, token):
        self.record("publishing_limit", account_id, token)
        return {"data": [{"quota_usage": self.quota_usage, "config": {"quota_total": self.quota_total}}]}

    def create_container(self, account_id, token, **fields):
        self.record("create_container", account_id, token, **fields)
        if self.create_error:
            raise self.create_error
        with self.lock:
            self._container_number += 1
            return str(2000 + self._container_number)

    def container_status(self, container_id, token):
        self.record("container_status", container_id, token)
        return {"status_code": self.status}

    def publish_container(self, account_id, token, creation_id):
        self.record("publish_container", account_id, token, creation_id)
        if self.publish_error:
            raise self.publish_error
        return "3001"

    def get_media(self, media_id, token):
        self.record("get_media", media_id, token)
        if self.media_error:
            raise self.media_error
        return {"id": media_id, "permalink": "https://www.instagram.com/p/example/"}

    def count(self, method):
        return sum(call[0] == method for call in self.calls)


@dataclass
class WorkerContext:
    settings: Settings
    sessions: object
    account_id: str
    asset_ids: list[str]

    def post(self, **overrides):
        values = dict(
            account_id=self.account_id,
            kind="IMAGE",
            asset_ids=[self.asset_ids[0]],
            caption="Your small wins matter.",
            alt_text="Cream lettering on an orange background.",
            status="QUEUED",
            revision=1,
            approved_revision=1,
            approved_mode=self.settings.publisher_mode,
            approved_at=time.time(),
        )
        values.update(overrides)
        with self.sessions() as session:
            post = Post(**values)
            session.add(post)
            session.commit()
            return post.id

    def read(self, post_id):
        with self.sessions() as session:
            return session.get(Post, post_id)


@pytest.fixture
def context(tmp_path):
    settings = Settings(
        _env_file=None,
        publisher_mode="live",
        public_base_url="https://publisher.example.test",
        database_url=f"sqlite:///{tmp_path / 'worker.db'}",
        media_root=tmp_path / "uploads",
        admin_api_key="test-admin-" + "a" * 32,
        token_encryption_key=Fernet.generate_key().decode(),
        media_signing_key="test-media-" + "b" * 32,
        meta_app_id="1234",
        meta_app_secret="test-meta-secret",
    )
    sessions = make_session_factory(settings.database_url)
    settings.media_root.mkdir()
    with sessions() as session:
        account = Account(
            instagram_user_id="1001",
            username="publisher_test",
            account_type="BUSINESS",
            token_ciphertext=encrypt_token("test-access-token", settings.token_encryption_key.get_secret_value()),
            token_expires_at=time.time() + 3600,
        )
        session.add(account)
        assets = []
        for index in range(2):
            path = settings.media_root / f"test-{index}.jpg"
            Image.new("RGB", (1080, 1350), (240, 220, 200)).save(path, "JPEG")
            data = path.read_bytes()
            asset = Asset(
                storage_key=path.name,
                mime_type="image/jpeg",
                size_bytes=len(data),
                width=1080,
                height=1350,
                duration=None,
                sha256=hashlib.sha256(data).hexdigest(),
            )
            session.add(asset)
            assets.append(asset)
        session.commit()
        result = WorkerContext(settings, sessions, account.id, [asset.id for asset in assets])
    yield result
    sessions.kw["bind"].dispose()


def execute(context, client):
    return run_once(context.settings, context.sessions, client=client, sleep=lambda _: None)


def test_drafts_never_reach_meta(context):
    post_id = context.post(status="DRAFT", approved_revision=None, approved_at=None)
    client = FakeMeta()

    assert execute(context, client) is False
    assert client.calls == []
    assert context.read(post_id).status == "DRAFT"


def test_future_schedule_is_not_claimed(context):
    post_id = context.post(scheduled_at=time.time() + 3600)
    client = FakeMeta()

    assert execute(context, client) is False
    assert client.calls == []
    assert context.read(post_id).status == "QUEUED"


def test_demo_completion_does_not_call_any_provider_method(context):
    context.settings = context.settings.model_copy(update={"publisher_mode": "demo"})
    post_id = context.post()
    client = FakeMeta()

    assert execute(context, client) is True
    post = context.read(post_id)
    assert post.status == "DEMO_PUBLISHED"
    assert post.publish_requested_at is None
    assert post.published_media_id is None
    assert client.calls == []


def test_demo_approval_cannot_be_used_after_switch_to_live(context):
    post_id = context.post(approved_mode="demo")
    client = FakeMeta()

    assert execute(context, client) is False
    assert context.read(post_id).status == "QUEUED"
    assert client.calls == []


def test_edit_revision_invalidates_older_approval(context):
    post_id = context.post(revision=2, approved_revision=1)
    client = FakeMeta()

    assert execute(context, client) is False
    assert context.read(post_id).status == "QUEUED"
    assert client.calls == []


def test_ready_image_publishes_once_and_records_result(context):
    post_id = context.post()
    client = FakeMeta()

    assert execute(context, client) is True
    post = context.read(post_id)
    assert post.status == "PUBLISHED"
    assert post.container_id == "2001"
    assert post.published_media_id == "3001"
    assert post.permalink == "https://www.instagram.com/p/example/"
    assert post.publish_requested_at is not None
    assert post.published_at is not None
    assert client.count("publish_container") == 1
    create = next(call for call in client.calls if call[0] == "create_container")
    assert create[2]["caption"] == "Your small wins matter."
    assert create[2]["alt_text"] == "Cream lettering on an orange background."
    assert create[2]["image_url"].startswith("https://publisher.example.test/media/")
    assert "signature=" in create[2]["image_url"]

    assert execute(context, client) is False
    assert client.count("publish_container") == 1


def test_publish_timeout_requires_attention_and_is_not_retried(context):
    post_id = context.post()
    client = FakeMeta()
    client.publish_error = MetaError("The Instagram response was lost.", retryable=True, ambiguous=True)

    assert execute(context, client) is True
    post = context.read(post_id)
    assert post.status == "NEEDS_ATTENTION"
    assert post.publish_requested_at is not None
    assert post.published_media_id is None
    assert client.count("publish_container") == 1

    execute(context, client)
    assert client.count("publish_container") == 1
    assert context.read(post_id).status == "NEEDS_ATTENTION"


def test_unexpected_exception_after_publish_intent_does_not_leak_secrets(context):
    post_id = context.post()
    client = FakeMeta()
    client.publish_error = RuntimeError("https://graph.instagram.com?access_token=very-secret")

    execute(context, client)
    post = context.read(post_id)
    assert post.status == "NEEDS_ATTENTION"
    assert "very-secret" not in (post.error or "")
    assert "access_token" not in (post.error or "")


def test_prepublication_failure_is_failed_without_publish_intent(context):
    post_id = context.post()
    client = FakeMeta()
    client.create_error = MetaError("Instagram rejected the media.")

    assert execute(context, client) is True
    post = context.read(post_id)
    assert post.status == "FAILED"
    assert post.publish_requested_at is None
    assert client.count("publish_container") == 0


@pytest.mark.parametrize("publish_requested,status", [(None, "FAILED"), (1.0, "NEEDS_ATTENTION")])
def test_expired_processing_lease_is_recovered_without_republishing(context, publish_requested, status):
    post_id = context.post(
        status="PROCESSING",
        lease_owner="old-worker",
        lease_expires_at=time.time() - 60,
        publish_requested_at=publish_requested,
        container_id="9999",
    )
    client = FakeMeta()

    execute(context, client)
    assert context.read(post_id).status == status
    assert client.calls == []


def test_nonfinished_container_is_not_published(context):
    post_id = context.post()
    client = FakeMeta()
    client.status = "IN_PROGRESS"

    execute(context, client)
    assert client.count("container_status") == context.settings.container_poll_attempts
    assert client.count("publish_container") == 0
    assert context.read(post_id).status == "FAILED"
    assert context.read(post_id).publish_requested_at is None


def test_worker_uses_dynamic_account_quota_before_creating_media(context):
    post_id = context.post()
    client = FakeMeta()
    client.quota_total = 3
    client.quota_usage = 3

    execute(context, client)
    assert context.read(post_id).status == "FAILED"
    assert client.count("create_container") == 0
    assert client.count("publish_container") == 0


def test_expired_token_prevents_provider_calls(context):
    post_id = context.post()
    with context.sessions() as session:
        account = session.get(Account, context.account_id)
        account.token_expires_at = time.time() - 1
        session.commit()
    client = FakeMeta()

    execute(context, client)
    assert context.read(post_id).status == "FAILED"
    assert client.calls == []


def test_permalink_fetch_failure_preserves_successful_publication(context):
    post_id = context.post()
    client = FakeMeta()
    client.media_error = MetaError("Instagram is temporarily unavailable.", retryable=True)

    execute(context, client)
    post = context.read(post_id)
    assert post.status == "PUBLISHED"
    assert post.published_media_id == "3001"
    assert client.count("publish_container") == 1
    execute(context, client)
    assert client.count("publish_container") == 1


def test_concurrent_workers_claim_a_post_only_once(context):
    post_id = context.post()
    client = FakeMeta()
    barrier = threading.Barrier(2)

    def simultaneous_run():
        barrier.wait(timeout=5)
        return execute(context, client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: simultaneous_run(), range(2)))

    assert results.count(True) == 1
    assert context.read(post_id).status == "PUBLISHED"
    assert client.count("publish_container") == 1


def test_carousel_children_keep_original_asset_order(context):
    post_id = context.post(kind="CAROUSEL", asset_ids=list(context.asset_ids), alt_text="")
    client = FakeMeta()

    execute(context, client)
    post = context.read(post_id)
    assert post.status == "PUBLISHED"
    assert post.child_container_ids == ["2001", "2002"]
    assert post.container_id == "2003"
    creates = [call[2] for call in client.calls if call[0] == "create_container"]
    assert len(creates) == 3
    assert context.asset_ids[0] in creates[0]["image_url"]
    assert context.asset_ids[1] in creates[1]["image_url"]
    assert creates[0]["is_carousel_item"] in (True, "true")
    assert creates[1]["is_carousel_item"] in (True, "true")
    assert creates[2]["media_type"] == "CAROUSEL"
    children = creates[2]["children"]
    assert children in (["2001", "2002"], "2001,2002")
    assert client.count("publish_container") == 1


def test_existing_publish_intent_is_never_claimed_even_if_status_was_reset(context):
    post_id = context.post(publish_requested_at=time.time() - 60, container_id="9999")
    client = FakeMeta()

    execute(context, client)
    assert client.count("publish_container") == 0
    assert context.read(post_id).status != "PUBLISHED"
    with context.sessions() as session:
        assert len(session.scalars(select(Post)).all()) == 1


@pytest.mark.parametrize(
    "field,value",
    [("lease_owner", "replacement-worker"), ("approved_mode", "demo"), ("revision", 2)],
)
def test_changed_ownership_or_approval_fences_final_publish(context, field, value):
    post_id = context.post()

    class MutatingMeta(FakeMeta):
        def container_status(self, container_id, token):
            result = super().container_status(container_id, token)
            with context.sessions() as session:
                row = session.get(Post, post_id)
                setattr(row, field, value)
                session.commit()
            return result

    client = MutatingMeta()
    execute(context, client)
    post = context.read(post_id)

    assert client.count("publish_container") == 0
    assert post.publish_requested_at is None
    assert getattr(post, field) == value
    assert post.status == "PROCESSING"
    assert post.published_media_id is None


def test_story_requires_business_account_before_any_provider_call(context):
    with context.sessions() as session:
        account = session.get(Account, context.account_id)
        account.account_type = "MEDIA_CREATOR"
        asset = session.get(Asset, context.asset_ids[0])
        path = context.settings.media_root / asset.storage_key
        Image.new("RGB", (1080, 1920), (240, 220, 200)).save(path, "JPEG")
        data = path.read_bytes()
        asset.height = 1920
        asset.size_bytes = len(data)
        asset.sha256 = hashlib.sha256(data).hexdigest()
        session.commit()
    post_id = context.post(kind="STORY", caption="", alt_text="")
    client = FakeMeta()

    execute(context, client)

    assert context.read(post_id).status == "FAILED"
    assert "Business" in context.read(post_id).error
    assert client.calls == []


def test_reel_rejects_a_still_cover_as_its_video(context):
    post_id = context.post(kind="REEL", alt_text="")
    client = FakeMeta()

    execute(context, client)

    assert context.read(post_id).status == "FAILED"
    assert client.calls == []


def test_already_published_container_needs_attention_and_cannot_be_published_again(context):
    post_id = context.post()
    client = FakeMeta()
    client.status = "PUBLISHED"

    execute(context, client)

    assert context.read(post_id).status == "NEEDS_ATTENTION"
    assert client.count("publish_container") == 0
    execute(context, client)
    assert client.count("create_container") == 1
    assert client.count("publish_container") == 0


class RefreshMeta(FakeMeta):
    def __init__(self, *, error=None, during_refresh=None):
        super().__init__()
        self.refresh_error = error
        self.during_refresh = during_refresh

    def refresh_token(self, token):
        self.record("refresh_token", token)
        if self.during_refresh:
            self.during_refresh()
        if self.refresh_error:
            raise self.refresh_error
        return {"access_token": "new-refreshed-token", "expires_in": 60 * 86400}


def mark_account_refresh_due(context):
    with context.sessions() as session:
        account = session.get(Account, context.account_id)
        account.token_refreshed_at = time.time() - 3 * 86400
        account.token_expires_at = time.time() + 2 * 86400
        session.commit()
        return account.token_ciphertext, account.token_refreshed_at


def test_due_token_is_refreshed_and_saved_encrypted(context):
    old_ciphertext, old_refreshed = mark_account_refresh_due(context)
    client = RefreshMeta()
    started = time.time()

    assert refresh_due_accounts(context.settings, context.sessions, client=client) == 1

    with context.sessions() as session:
        account = session.get(Account, context.account_id)
        assert account.token_ciphertext != old_ciphertext
        assert account.token_ciphertext != "new-refreshed-token"
        assert decrypt_token(account.token_ciphertext, context.settings.token_encryption_key.get_secret_value()) == (
            "new-refreshed-token"
        )
        assert account.token_refreshed_at > old_refreshed
        assert account.token_expires_at >= started + 59 * 86400
    assert client.count("refresh_token") == 1


def test_refresh_failure_retains_original_token_age_and_is_backed_off(context):
    old_ciphertext, old_refreshed = mark_account_refresh_due(context)
    client = RefreshMeta(error=MetaError("Instagram is temporarily unavailable.", retryable=True))

    assert refresh_due_accounts(context.settings, context.sessions, client=client) == 0
    assert refresh_due_accounts(context.settings, context.sessions, client=client) == 0

    with context.sessions() as session:
        account = session.get(Account, context.account_id)
        assert account.token_ciphertext == old_ciphertext
        assert account.token_refreshed_at == old_refreshed
    assert client.count("refresh_token") == 1


@pytest.mark.parametrize("token_age,remaining", [(3600, 2 * 86400), (3 * 86400, -1), (3 * 86400, 14 * 86400)])
def test_ineligible_token_is_not_refreshed(context, token_age, remaining):
    with context.sessions() as session:
        account = session.get(Account, context.account_id)
        account.token_refreshed_at = time.time() - token_age
        account.token_expires_at = time.time() + remaining
        session.commit()
    client = RefreshMeta()

    assert refresh_due_accounts(context.settings, context.sessions, client=client) == 0
    assert client.calls == []


def test_demo_token_maintenance_does_not_contact_meta(context):
    mark_account_refresh_due(context)
    context.settings = context.settings.model_copy(update={"publisher_mode": "demo"})
    client = RefreshMeta()

    assert refresh_due_accounts(context.settings, context.sessions, client=client) == 0
    assert client.calls == []


def test_concurrent_token_maintenance_refreshes_only_once(context):
    mark_account_refresh_due(context)
    client = RefreshMeta()
    barrier = threading.Barrier(2)

    def simultaneous_refresh():
        barrier.wait(timeout=5)
        return refresh_due_accounts(context.settings, context.sessions, client=client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: simultaneous_refresh(), range(2)))

    assert sum(results) == 1
    assert client.count("refresh_token") == 1


def test_token_refresh_does_not_overwrite_a_concurrent_reconnect(context):
    mark_account_refresh_due(context)
    reconnected_ciphertext = encrypt_token(
        "reconnected-new-account-token", context.settings.token_encryption_key.get_secret_value()
    )

    def reconnect_during_provider_call():
        with context.sessions() as session:
            account = session.get(Account, context.account_id)
            account.token_ciphertext = reconnected_ciphertext
            account.token_refreshed_at = time.time()
            account.token_expires_at = time.time() + 60 * 86400
            session.commit()

    client = RefreshMeta(during_refresh=reconnect_during_provider_call)

    assert refresh_due_accounts(context.settings, context.sessions, client=client) == 0

    with context.sessions() as session:
        account = session.get(Account, context.account_id)
        assert account.token_ciphertext == reconnected_ciphertext
    assert client.count("refresh_token") == 1


def test_preset_shutdown_event_leaves_queued_post_unclaimed(context):
    post_id = context.post()
    stop = threading.Event()
    stop.set()
    client = FakeMeta()

    assert run_once(context.settings, context.sessions, client=client, stop_event=stop) is False

    post = context.read(post_id)
    assert post.status == "QUEUED"
    assert post.lease_owner is None
    assert client.calls == []


def test_shutdown_during_container_polling_stops_without_publishing(context):
    post_id = context.post()
    stop = threading.Event()

    class StoppingMeta(FakeMeta):
        def container_status(self, container_id, token):
            self.record("container_status", container_id, token)
            stop.set()
            return {"status_code": "IN_PROGRESS"}

    client = StoppingMeta()
    assert run_once(context.settings, context.sessions, client=client, stop_event=stop) is True

    post = context.read(post_id)
    assert post.status == "FAILED"
    assert post.publish_requested_at is None
    assert post.lease_owner is None
    assert post.lease_expires_at is None
    assert "shutdown" in post.error.lower()
    assert client.count("container_status") == 1
    assert client.count("publish_container") == 0


def test_shutdown_after_first_carousel_child_keeps_its_id_and_stops(context):
    post_id = context.post(kind="CAROUSEL", asset_ids=list(context.asset_ids), alt_text="")
    stop = threading.Event()

    class StoppingMeta(FakeMeta):
        def create_container(self, account_id, token, **fields):
            container_id = super().create_container(account_id, token, **fields)
            stop.set()
            return container_id

    client = StoppingMeta()
    run_once(context.settings, context.sessions, client=client, stop_event=stop)

    post = context.read(post_id)
    assert post.status == "FAILED"
    assert post.child_container_ids == ["2001"]
    assert post.publish_requested_at is None
    assert post.lease_owner is None
    assert client.count("create_container") == 1
    assert client.count("container_status") == 0
    assert client.count("publish_container") == 0


def test_shutdown_after_successful_publish_still_saves_publication(context):
    post_id = context.post()
    stop = threading.Event()

    class StoppingMeta(FakeMeta):
        def publish_container(self, account_id, token, creation_id):
            published_id = super().publish_container(account_id, token, creation_id)
            stop.set()
            return published_id

    client = StoppingMeta()
    run_once(context.settings, context.sessions, client=client, stop_event=stop)

    post = context.read(post_id)
    assert post.status == "PUBLISHED"
    assert post.published_media_id == "3001"
    assert post.published_at is not None
    assert post.publish_requested_at is not None
    assert post.lease_owner is None
    assert client.count("publish_container") == 1
    assert client.count("get_media") == 0
