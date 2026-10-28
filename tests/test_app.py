import io
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import pytest
from conftest import TEST_META_TOKEN, TEST_REFRESHED_TOKEN
from PIL import Image
from sqlalchemy import select

from maple_publisher.db import Account, Asset, OAuthState, Post
from maple_publisher.security import decrypt_token, media_url, state_digest


def image_bytes(size=(800, 1000)):
    output = io.BytesIO()
    Image.new("RGBA", size, (190, 100, 60, 200)).save(output, format="PNG")
    return output.getvalue()


def upload(api, *, kind="image", size=(800, 1000)):
    response = api.client.post(
        "/api/assets", headers=api.headers, data={"kind": kind},
        files={"file": ("../../untrusted.png", image_bytes(size), "image/png")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def draft(api, *, asset=None, **changes):
    asset = asset or upload(api)
    payload = {"kind": "IMAGE", "asset_ids": [asset["id"]], "caption": "Your small wins matter."}
    payload.update(changes)
    response = api.client.post("/api/posts", headers=api.headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json(), payload


def begin_connection(api):
    response = api.client.post("/api/accounts/connect", headers=api.headers)
    assert response.status_code == 200, response.text
    assert response.json()["expires_in"] == 600
    return parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]


def connect_account(api):
    state = begin_connection(api)
    response = api.client.get("/auth/instagram/callback", params={"state": state, "code": "test-auth-code"})
    assert response.status_code == 200, response.text
    accounts = api.client.get("/api/accounts", headers=api.headers).json()
    assert len(accounts) == 1
    return accounts[0]


@pytest.mark.parametrize("path", ["/api/status", "/api/accounts", "/api/assets", "/api/posts", "/api/events"])
def test_private_reads_require_correct_bearer_token(api_bundle, path):
    api = api_bundle
    assert api.client.get(path).status_code == 401
    assert api.client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.client.get(path, headers=api.headers).status_code == 200


def test_private_writes_require_authentication(api_bundle):
    api = api_bundle
    assert api.client.post("/api/accounts/connect").status_code == 401
    assert api.client.post("/api/posts", json={"asset_ids": ["missing"]}).status_code == 401
    response = api.client.post("/api/assets", files={"file": ("image.png", image_bytes(), "image/png")})
    assert response.status_code == 401
    assert list(api.settings.media_root.iterdir()) == []


def test_public_health_and_security_headers_do_not_leak_keys(api_bundle):
    api = api_bundle
    response = api.client.get("/healthz")
    assert response.json() == {"status": "ok", "mode": "demo"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    response = api.client.get("/api/status", headers=api.headers)
    assert response.json()["instagram_configured"] is True
    for secret in [api.settings.admin_api_key, api.settings.token_encryption_key,
                   api.settings.media_signing_key, api.settings.meta_app_secret]:
        assert secret.get_secret_value() not in response.text


def test_upload_normalizes_and_signed_preview_serves_without_admin_auth(api_bundle):
    api = api_bundle
    asset = upload(api)
    assert asset["mime_type"] == "image/jpeg"
    assert "storage_key" not in asset
    assert "untrusted" not in str(asset)
    with api.sessions() as session:
        stored = session.get(Asset, asset["id"])
        assert (api.settings.media_root / stored.storage_key).is_file()
    response = api.client.get(media_url(asset["id"], api.settings))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(response.content)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.mode == "RGB"
        assert decoded.size == (800, 1000)


def test_media_links_require_valid_unexpired_signature(api_bundle):
    api = api_bundle
    asset = upload(api)
    expired = media_url(asset["id"], api.settings, expires=int(time.time()) - 1)
    assert api.client.get(expired).status_code == 403
    url = urlsplit(media_url(asset["id"], api.settings))
    query = parse_qs(url.query)
    query["signature"] = ["0" * 64]
    forged = urlunsplit(url._replace(query=urlencode(query, doseq=True)))
    assert api.client.get(forged).status_code == 403
    assert api.client.get(f"/media/{asset['id']}").status_code == 422
    unknown = media_url("missing-asset", api.settings)
    assert api.client.get(unknown).status_code == 404


def test_valid_signature_cannot_be_reused_for_another_asset(api_bundle):
    api = api_bundle
    first, second = upload(api), upload(api)
    first_url = media_url(first["id"], api.settings)
    assert api.client.get(first_url.replace(first["id"], second["id"])).status_code == 403


def test_invalid_upload_leaves_no_database_record_or_file(api_bundle):
    api = api_bundle
    response = api.client.post(
        "/api/assets", headers=api.headers, data={"kind": "image"},
        files={"file": ("image.jpg", b"not an image", "image/jpeg")},
    )
    assert response.status_code == 422
    assert api.client.get("/api/assets", headers=api.headers).json() == []
    assert list(api.settings.media_root.iterdir()) == []


def test_declared_oversize_request_rejected_before_body_processing(api_bundle):
    response = api_bundle.client.post(
        "/api/assets", headers=api_bundle.headers | {"Content-Length": "102000001"}, content=b"x",
    )
    assert response.status_code == 413


def test_create_draft_review_and_audit_show_current_revision(api_bundle):
    api = api_bundle
    post, payload = draft(api)
    assert post["status"] == "DRAFT"
    assert post["revision"] == 1
    assert post["approved_revision"] is None
    review = api.client.get(f"/api/posts/{post['id']}/review", headers=api.headers).json()
    assert review["post"]["caption"] == payload["caption"]
    assert review["approval"]["body"] == {"revision": 1}
    assert review["mode"] == "demo"
    assert "never contacts Instagram" in review["warnings"][0]
    assert api.client.get(review["preview_urls"][0]).status_code == 200
    events = api.client.get("/api/events", headers=api.headers).json()
    assert [(e["action"], e["post_id"]) for e in events] == [("post.created", post["id"])]


def test_idempotency_replay_creates_one_draft_but_changed_payload_conflicts(api_bundle):
    api = api_bundle
    asset = upload(api)
    body = {"asset_ids": [asset["id"]], "caption": "original"}
    headers = api.headers | {"Idempotency-Key": "test-create-1"}
    first = api.client.post("/api/posts", headers=headers, json=body)
    second = api.client.post("/api/posts", headers=headers, json=body)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    conflict = api.client.post("/api/posts", headers=headers, json=body | {"caption": "different"})
    assert conflict.status_code == 409
    assert len(api.client.get("/api/posts", headers=api.headers).json()) == 1
    assert len(api.client.get("/api/events", headers=api.headers).json()) == 1


def test_edit_advances_revision_and_stale_approval_is_rejected(api_bundle):
    api = api_bundle
    post, payload = draft(api)
    edit = api.client.put(
        f"/api/posts/{post['id']}", headers=api.headers,
        json=payload | {"revision": 1, "caption": "Updated copy."},
    )
    assert edit.status_code == 200
    assert edit.json()["revision"] == 2
    assert edit.json()["caption"] == "Updated copy."
    assert api.client.post(f"/api/posts/{post['id']}/approve", headers=api.headers, json={"revision": 1}).status_code == 409
    assert api.client.put(f"/api/posts/{post['id']}", headers=api.headers, json=payload | {"revision": 1}).status_code == 409
    review = api.client.get(f"/api/posts/{post['id']}/review", headers=api.headers).json()
    assert review["approval"]["body"] == {"revision": 2}


def test_approve_queues_exact_revision_without_publishing_and_cancel_is_idempotent(api_bundle):
    api = api_bundle
    post, payload = draft(api)
    url = f"/api/posts/{post['id']}"
    first = api.client.post(f"{url}/approve", headers=api.headers, json={"revision": 1})
    assert first.status_code == 200
    assert first.json()["status"] == "QUEUED"
    assert first.json()["approved_revision"] == 1
    assert first.json()["approved_mode"] == "demo"
    assert first.json()["published_media_id"] is None
    again = api.client.post(f"{url}/approve", headers=api.headers, json={"revision": 1})
    assert again.status_code == 200
    assert api.client.put(url, headers=api.headers, json=payload | {"revision": 1}).status_code == 409
    assert api.client.post(f"{url}/cancel", headers=api.headers).json()["status"] == "CANCELED"
    assert api.client.post(f"{url}/cancel", headers=api.headers).status_code == 200
    assert api.client.post(f"{url}/approve", headers=api.headers, json={"revision": 1}).status_code == 409


def test_post_after_publication_attempt_cannot_be_retried_or_canceled(api_bundle):
    api = api_bundle
    post, _ = draft(api)
    with api.sessions.begin() as session:
        row = session.get(Post, post["id"])
        row.status = "FAILED"
        row.publish_requested_at = time.time()
    for action in ["retry", "cancel"]:
        assert api.client.post(f"/api/posts/{post['id']}/{action}", headers=api.headers).status_code == 409


def test_failed_prepublication_post_returns_to_unapproved_new_revision(api_bundle):
    api = api_bundle
    post, _ = draft(api)
    with api.sessions.begin() as session:
        row = session.get(Post, post["id"])
        row.status, row.approved_revision, row.approved_mode = "FAILED", 1, "demo"
        row.error, row.container_id = "Temporary error", "17890000000000001"
    response = api.client.post(f"/api/posts/{post['id']}/retry", headers=api.headers)
    assert response.status_code == 200
    retried = response.json()
    assert retried["status"] == "DRAFT"
    assert retried["revision"] == 2
    assert retried["approved_revision"] is None
    assert retried["approved_mode"] is None
    assert retried["container_id"] is None
    assert retried["error"] is None


@pytest.mark.parametrize("change", [
    {"kind": "REEL"},
    {"kind": "STORY", "caption": ""},
    {"kind": "CAROUSEL"},
    {"kind": "UNSUPPORTED"},
    {"asset_ids": []},
    {"asset_ids": ["missing-id"]},
    {"account_id": "missing-account"},
    {"caption": "x" * 2201},
    {"scheduled_at": "2027-01-01T12:00:00"},
    {"unrecognized_field": "bad"},
])
def test_invalid_draft_schema_or_media_rejected(api_bundle, change):
    api = api_bundle
    asset = upload(api)
    response = api.client.post(
        "/api/posts", headers=api.headers,
        json={"kind": "IMAGE", "asset_ids": [asset["id"]], "caption": "Draft"} | change,
    )
    assert response.status_code == 422
    assert api.client.get("/api/posts", headers=api.headers).json() == []


def test_duplicate_carousel_asset_rejected(api_bundle):
    asset = upload(api_bundle)
    response = api_bundle.client.post(
        "/api/posts", headers=api_bundle.headers,
        json={"kind": "CAROUSEL", "asset_ids": [asset["id"], asset["id"]]},
    )
    assert response.status_code == 422


def test_schedule_is_normalized_to_utc_and_preserved_on_approval(api_bundle):
    api = api_bundle
    post, _ = draft(api, scheduled_at="2027-01-01T09:30:00-05:00")
    assert post["scheduled_at"] == "2027-01-01T14:30:00+00:00"
    response = api.client.post(f"/api/posts/{post['id']}/approve", headers=api.headers, json={"revision": 1})
    assert response.json()["scheduled_at"] == post["scheduled_at"]
    with api.sessions() as session:
        stored = session.get(Post, post["id"])
        assert stored.scheduled_at == datetime(2027, 1, 1, 14, 30, tzinfo=timezone.utc).timestamp()


def test_oauth_state_is_hashed_single_use_and_tokens_are_encrypted(api_bundle):
    api = api_bundle
    state = begin_connection(api)
    with api.sessions() as session:
        oauth = session.get(OAuthState, state_digest(state))
        assert oauth is not None
        assert oauth.digest != state
        assert oauth.consumed_at is None
    callback_url = "/auth/instagram/callback"
    response = api.client.get(callback_url, params={"state": state, "code": "test-auth-code"})
    assert response.status_code == 200
    assert "Connected @maple_baby_test" in response.text
    assert TEST_META_TOKEN not in response.text
    assert api.client.get(callback_url, params={"state": state, "code": "test-auth-code"}).status_code == 400
    assert api.provider.exchanged_codes == ["test-auth-code"]
    with api.sessions() as session:
        account = session.scalar(select(Account))
        assert account.token_ciphertext != TEST_META_TOKEN
        assert TEST_META_TOKEN not in account.token_ciphertext
        assert decrypt_token(account.token_ciphertext, api.settings.token_encryption_key.get_secret_value()) == TEST_META_TOKEN
        assert session.get(OAuthState, state_digest(state)).consumed_at is not None
    for route in ["/api/accounts", "/api/status", "/api/events"]:
        response = api.client.get(route, headers=api.headers)
        assert TEST_META_TOKEN not in response.text
        assert "token_ciphertext" not in response.text
    db_path = api.settings.database_url.removeprefix("sqlite:///")
    assert TEST_META_TOKEN.encode() not in Path(db_path).read_bytes()


def test_oauth_missing_random_or_expired_state_never_exchanges_code(api_bundle):
    api = api_bundle
    assert api.client.get("/auth/instagram/callback", params={"code": "code"}).status_code == 422
    assert api.client.get("/auth/instagram/callback", params={"code": "code", "state": "invalid"}).status_code == 400
    state = begin_connection(api)
    with api.sessions.begin() as session:
        session.get(OAuthState, state_digest(state)).expires_at = time.time() - 1
    assert api.client.get("/auth/instagram/callback", params={"code": "code", "state": state}).status_code == 400
    assert api.provider.exchanged_codes == []


def test_oauth_denial_consumes_state_without_exchanging_code(api_bundle):
    api = api_bundle
    state = begin_connection(api)
    denied = api.client.get("/auth/instagram/callback", params={"state": state, "error": "access_denied"})
    assert denied.status_code == 400
    replay = api.client.get("/auth/instagram/callback", params={"state": state, "code": "later-code"})
    assert replay.status_code == 400
    assert api.provider.exchanged_codes == []


def test_oauth_callback_escapes_username_and_reconnect_updates_one_account(api_bundle):
    api = api_bundle
    api.provider.profile["username"] = "<script>alert(1)</script>"
    state = begin_connection(api)
    response = api.client.get("/auth/instagram/callback", params={"state": state, "code": "code"})
    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text
    first = api.client.get("/api/accounts", headers=api.headers).json()[0]
    second = connect_account(api)
    assert second["id"] == first["id"]


def test_personal_account_is_rejected(api_bundle):
    api = api_bundle
    api.provider.profile["account_type"] = "PERSONAL"
    state = begin_connection(api)
    response = api.client.get("/auth/instagram/callback", params={"state": state, "code": "code"})
    assert response.status_code == 422
    assert api.client.get("/api/accounts", headers=api.headers).json() == []


def test_refresh_waits_24_hours_then_reencrypts_new_token(api_bundle):
    api = api_bundle
    account = connect_account(api)
    refresh_url = f"/api/accounts/{account['id']}/refresh"
    assert api.client.post(refresh_url, headers=api.headers).status_code == 409
    assert api.provider.refreshed_tokens == []
    with api.sessions.begin() as session:
        session.get(Account, account["id"]).token_refreshed_at = time.time() - 86401
    response = api.client.post(refresh_url, headers=api.headers)
    assert response.status_code == 200
    assert api.provider.refreshed_tokens == [TEST_META_TOKEN]
    assert TEST_REFRESHED_TOKEN not in response.text
    with api.sessions() as session:
        row = session.get(Account, account["id"])
        assert decrypt_token(row.token_ciphertext, api.settings.token_encryption_key.get_secret_value()) == TEST_REFRESHED_TOKEN
        assert row.token_refreshed_at > time.time() - 5


def test_expired_token_requires_reconnection_without_refresh_call(api_bundle):
    api = api_bundle
    account = connect_account(api)
    with api.sessions.begin() as session:
        row = session.get(Account, account["id"])
        row.token_expires_at = time.time() - 1
        row.token_refreshed_at = time.time() - 86401
    response = api.client.post(f"/api/accounts/{account['id']}/refresh", headers=api.headers)
    assert response.status_code == 409
    assert api.provider.refreshed_tokens == []


def test_live_approval_requires_a_connected_account(api_bundle):
    api = api_bundle
    api.settings.publisher_mode = "live"
    post, _ = draft(api)
    response = api.client.post(f"/api/posts/{post['id']}/approve", headers=api.headers, json={"revision": 1})
    assert response.status_code == 409
    assert api.client.get(f"/api/posts/{post['id']}", headers=api.headers).json()["status"] == "DRAFT"


def test_live_approval_requires_token_valid_through_schedule(api_bundle):
    api = api_bundle
    account = connect_account(api)
    api.settings.publisher_mode = "live"
    scheduled = datetime.fromtimestamp(time.time() + 7_000_000, timezone.utc).isoformat()
    post, _ = draft(api, account_id=account["id"], scheduled_at=scheduled)
    response = api.client.post(f"/api/posts/{post['id']}/approve", headers=api.headers, json={"revision": 1})
    assert response.status_code == 409


def test_live_story_requires_business_account_and_does_not_publish(api_bundle):
    api = api_bundle
    api.provider.profile["account_type"] = "CREATOR"
    account = connect_account(api)
    api.settings.publisher_mode = "live"
    asset = upload(api, kind="story", size=(900, 1600))
    post, _ = draft(api, asset=asset, account_id=account["id"], kind="STORY", caption="")
    url = f"/api/posts/{post['id']}/approve"
    assert api.client.post(url, headers=api.headers, json={"revision": 1}).status_code == 422
    with api.sessions.begin() as session:
        session.get(Account, account["id"]).account_type = "BUSINESS"
    response = api.client.post(url, headers=api.headers, json={"revision": 1})
    assert response.status_code == 200
    assert response.json()["approved_mode"] == "live"
    assert response.json()["status"] == "QUEUED"
    assert response.json()["published_media_id"] is None


def test_switching_to_live_does_not_reapprove_existing_demo_queue(api_bundle):
    api = api_bundle
    post, _ = draft(api)
    url = f"/api/posts/{post['id']}/approve"
    assert api.client.post(url, headers=api.headers, json={"revision": 1}).status_code == 200
    api.settings.publisher_mode = "live"
    assert api.client.post(url, headers=api.headers, json={"revision": 1}).status_code == 409


def test_unconfigured_meta_app_has_actionable_connection_error(api_bundle):
    api = api_bundle
    api.settings.meta_app_id = ""
    response = api.client.post("/api/accounts/connect", headers=api.headers)
    assert response.status_code == 409
    assert "META_APP_ID" in response.json()["detail"]
    with api.sessions() as session:
        assert session.scalar(select(OAuthState)) is None


def test_incomplete_oauth_response_is_safe_gateway_error(api_bundle):
    api = api_bundle
    api.provider.exchange_code = lambda code: {}
    state = begin_connection(api)
    response = api.client.get("/auth/instagram/callback", params={"state": state, "code": "code"})
    assert response.status_code == 502
    assert api.client.get("/api/accounts", headers=api.headers).json() == []


def test_malformed_refresh_response_is_safe_gateway_error_and_preserves_token(api_bundle):
    api = api_bundle
    account = connect_account(api)
    with api.sessions.begin() as session:
        row = session.get(Account, account["id"])
        row.token_refreshed_at = time.time() - 86401
        previous_ciphertext = row.token_ciphertext
    api.provider.refresh_token = lambda token: {}
    response = api.client.post(f"/api/accounts/{account['id']}/refresh", headers=api.headers)
    assert response.status_code == 502
    with api.sessions() as session:
        assert session.get(Account, account["id"]).token_ciphertext == previous_ciphertext
