import base64
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from instagram_publisher.db import Account, AuditEvent, CallbackReceipt, Post
from instagram_publisher.meta import MetaError

APP_SCOPED_ID = "998877665544"


def connect_account(api):
    response = api.client.post("/api/accounts/connect", headers=api.headers)
    assert response.status_code == 200
    state = parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]
    response = api.client.get("/auth/instagram/callback", params={"state": state, "code": "test-code"})
    assert response.status_code == 200
    return api.client.get("/api/accounts", headers=api.headers).json()[0]


def signed_request(api, payload=None, *, spaced=False, wrong_secret=False):
    payload = payload or {"algorithm": "HMAC-SHA256", "user_id": APP_SCOPED_ID}
    raw = json.dumps(payload, indent=2 if spaced else None, separators=None if spaced else (",", ":"))
    encoded = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    secret = "wrong-signing-key" if wrong_secret else api.settings.meta_app_secret.get_secret_value()
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode().rstrip("=") + "." + encoded


def add_post(api, account_id, *, status="NEEDS_ATTENTION", container_id="18000000000000001"):
    with api.sessions.begin() as session:
        post = Post(
            account_id=account_id, kind="IMAGE", asset_ids=[], caption="Test publication",
            status=status, container_id=container_id, publish_requested_at=time.time() - 60,
            error="Original publication result was uncertain",
        )
        session.add(post)
        session.flush()
        session.add(AuditEvent(post_id=post.id, action="test.record"))
        return post.id


def set_remote_status(api, value):
    calls = []

    def read_status(container_id, token):
        calls.append((container_id, token))
        if isinstance(value, Exception):
            raise value
        return {"status_code": value, "status": "provider body that must not be reflected"}

    api.provider.container_status = read_status
    return calls


def test_remote_status_requires_live_mode_and_does_not_contact_provider_in_demo(api_bundle):
    api = api_bundle
    account = connect_account(api)
    post_id = add_post(api, account["id"])
    calls = set_remote_status(api, "PUBLISHED")
    response = api.client.get(f"/api/posts/{post_id}/remote-status", headers=api.headers)
    assert response.status_code == 409
    assert calls == []


def test_remote_status_is_read_only_and_redacts_provider_details(api_bundle):
    api = api_bundle
    account = connect_account(api)
    post_id = add_post(api, account["id"])
    api.settings.publisher_mode = "live"
    calls = set_remote_status(api, "PUBLISHED")
    response = api.client.get(f"/api/posts/{post_id}/remote-status", headers=api.headers)
    assert response.status_code == 200
    assert response.json()["remote_status"] == "PUBLISHED"
    assert response.json()["local_status"] == "NEEDS_ATTENTION"
    assert len(calls) == 1
    assert calls[0][0] == "18000000000000001"
    assert calls[0][1] not in response.text
    assert "provider body" not in response.text
    with api.sessions() as session:
        assert session.get(Post, post_id).status == "NEEDS_ATTENTION"


def test_reconciliation_resolves_only_confirmed_publication_without_inventing_media_id(api_bundle):
    api = api_bundle
    account = connect_account(api)
    post_id = add_post(api, account["id"])
    api.settings.publisher_mode = "live"
    calls = set_remote_status(api, "PUBLISHED")
    response = api.client.post(f"/api/posts/{post_id}/reconcile", headers=api.headers)
    assert response.status_code == 200
    assert response.json()["resolved"] is True
    assert response.json()["post"]["status"] == "PUBLISHED"
    assert response.json()["post"]["published_media_id"] is None
    assert response.json()["post"]["published_at"] is None
    assert response.json()["post"]["permalink"] is None
    assert len(calls) == 1
    repeated = api.client.post(f"/api/posts/{post_id}/reconcile", headers=api.headers)
    assert repeated.status_code == 200
    assert repeated.json()["resolved"] is False
    with api.sessions() as session:
        audits = list(session.scalars(select(AuditEvent).where(AuditEvent.action == "post.reconciled")))
        assert len(audits) == 1


@pytest.mark.parametrize("remote", ["FINISHED", "IN_PROGRESS", "ERROR", "EXPIRED"])
def test_reconciliation_never_requeues_or_republishes_an_uncertain_attempt(api_bundle, remote):
    api = api_bundle
    account = connect_account(api)
    post_id = add_post(api, account["id"])
    api.settings.publisher_mode = "live"
    calls = set_remote_status(api, remote)
    response = api.client.post(f"/api/posts/{post_id}/reconcile", headers=api.headers)
    assert response.status_code == 200
    assert response.json()["resolved"] is False
    assert len(calls) == 1
    with api.sessions() as session:
        assert session.get(Post, post_id).status == "NEEDS_ATTENTION"


def test_recovery_provider_failure_returns_safe_error_and_keeps_uncertain_state(api_bundle):
    api = api_bundle
    account = connect_account(api)
    post_id = add_post(api, account["id"])
    api.settings.publisher_mode = "live"
    set_remote_status(api, MetaError("Instagram is temporarily unavailable.", retryable=True))
    response = api.client.post(f"/api/posts/{post_id}/reconcile", headers=api.headers)
    assert response.status_code == 502
    with api.sessions() as session:
        assert session.get(Post, post_id).status == "NEEDS_ATTENTION"


@pytest.mark.parametrize("route", ["deauthorize", "data-deletion"])
def test_lifecycle_callbacks_reject_unsigned_or_forged_requests_without_changes(api_bundle, route):
    api = api_bundle
    account = connect_account(api)
    for request in ["invalid", signed_request(api, wrong_secret=True)]:
        response = api.client.post(f"/auth/instagram/{route}", data={"signed_request": request})
        assert response.status_code == 400
        assert request not in response.text
    with api.sessions() as session:
        assert session.get(Account, account["id"]).token_ciphertext
        assert list(session.scalars(select(CallbackReceipt))) == []


def test_deauthorization_revokes_matching_app_scoped_account_and_cancels_pending_work(api_bundle):
    api = api_bundle
    account = connect_account(api)
    queued_id = add_post(api, account["id"], status="QUEUED")
    uncertain_id = add_post(api, account["id"])
    response = api.client.post("/auth/instagram/deauthorize", data={"signed_request": signed_request(api)})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    with api.sessions() as session:
        stored = session.get(Account, account["id"])
        assert stored.app_scoped_id == APP_SCOPED_ID
        assert stored.token_ciphertext == ""
        assert stored.token_expires_at == 0
        assert session.get(Post, queued_id).status == "CANCELED"
        assert session.get(Post, uncertain_id).status == "NEEDS_ATTENTION"


def test_same_deauthorization_cannot_revoke_a_later_reconnection(api_bundle):
    api = api_bundle
    account = connect_account(api)
    request = signed_request(api)
    response = api.client.post("/auth/instagram/deauthorize", data={"signed_request": request})
    assert response.status_code == 200
    reconnected = connect_account(api)
    assert reconnected["id"] == account["id"]
    with api.sessions() as session:
        current_ciphertext = session.get(Account, account["id"]).token_ciphertext
    assert current_ciphertext
    replay = api.client.post("/auth/instagram/deauthorize", data={"signed_request": request})
    assert replay.status_code == 200
    with api.sessions() as session:
        assert session.get(Account, account["id"]).token_ciphertext == current_ciphertext


def test_semantically_identical_signed_payload_is_not_a_replay_alias(api_bundle):
    api = api_bundle
    account = connect_account(api)
    first = signed_request(api, {"algorithm": "HMAC-SHA256", "user_id": APP_SCOPED_ID})
    alias = signed_request(api, {"user_id": APP_SCOPED_ID, "algorithm": "HMAC-SHA256"}, spaced=True)
    assert first != alias
    assert api.client.post("/auth/instagram/deauthorize", data={"signed_request": first}).status_code == 200
    connect_account(api)
    with api.sessions() as session:
        current_ciphertext = session.get(Account, account["id"]).token_ciphertext
    assert api.client.post("/auth/instagram/deauthorize", data={"signed_request": alias}).status_code == 200
    with api.sessions() as session:
        assert session.get(Account, account["id"]).token_ciphertext == current_ciphertext
        assert len(list(session.scalars(select(CallbackReceipt)))) == 1


@pytest.mark.parametrize("route", ["deauthorize", "data-deletion"])
def test_old_issued_at_does_not_revoke_or_delete_a_newer_connection(api_bundle, route):
    api = api_bundle
    account = connect_account(api)
    payload = {"algorithm": "HMAC-SHA256", "user_id": APP_SCOPED_ID, "issued_at": int(time.time()) - 300}
    response = api.client.post(f"/auth/instagram/{route}", data={"signed_request": signed_request(api, payload)})
    assert response.status_code == 200
    with api.sessions() as session:
        assert session.get(Account, account["id"]).token_ciphertext
        assert session.scalar(select(CallbackReceipt)).status == "ignored_stale"


def test_deletion_removes_account_posts_and_their_audits_with_an_opaque_receipt(api_bundle):
    api = api_bundle
    account = connect_account(api)
    first = add_post(api, account["id"])
    second = add_post(api, account["id"], status="PUBLISHED")
    request = signed_request(api)
    response = api.client.post("/auth/instagram/data-deletion", data={"signed_request": request})
    assert response.status_code == 200
    receipt = response.json()
    assert set(receipt) == {"url", "confirmation_code"}
    assert len(receipt["confirmation_code"]) >= 24
    assert APP_SCOPED_ID not in response.text
    assert account["instagram_user_id"] not in response.text
    assert account["username"] not in response.text
    with api.sessions() as session:
        assert session.get(Account, account["id"]) is None
        assert session.get(Post, first) is None
        assert session.get(Post, second) is None
        assert list(session.scalars(select(AuditEvent).where(AuditEvent.post_id.in_([first, second])))) == []
    status = api.client.get(receipt["url"])
    assert status.status_code == 200
    assert status.json() == {"confirmation_code": receipt["confirmation_code"], "status": "completed"}
    assert api.client.get("/auth/instagram/deletion-status/unknown").status_code == 404
    repeated = api.client.post("/auth/instagram/data-deletion", data={"signed_request": request})
    assert repeated.json() == receipt


def test_deletion_uses_app_scoped_identity_without_disclosing_unmatched_accounts(api_bundle):
    api = api_bundle
    account = connect_account(api)
    wrong_identity = {"algorithm": "HMAC-SHA256", "user_id": account["instagram_user_id"]}
    response = api.client.post("/auth/instagram/data-deletion", data={"signed_request": signed_request(api, wrong_identity)})
    assert response.status_code == 200
    assert set(response.json()) == {"url", "confirmation_code"}
    with api.sessions() as session:
        assert session.get(Account, account["id"]) is not None


def test_concurrent_repeated_deletion_returns_one_stable_receipt(api_bundle):
    api = api_bundle
    account = connect_account(api)
    add_post(api, account["id"])
    request = signed_request(api)

    def deliver(_):
        return api.client.post("/auth/instagram/data-deletion", data={"signed_request": request})

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(deliver, range(4)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["confirmation_code"] for response in responses}) == 1
    with api.sessions() as session:
        assert session.get(Account, account["id"]) is None
        assert len(list(session.scalars(select(CallbackReceipt)))) == 1
