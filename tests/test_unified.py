import base64
import hashlib
import hmac
import io
import json
import time

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from instagram_publisher.db import Account, Post
from instagram_publisher.security import decrypt_token
from instagram_publisher.worker import refresh_due_accounts, run_once

TOKEN = "synthetic-unified-token-for-unit-tests"


def responses(app_id="123456789"):
    return {
        "/v26.0/debug_token": {
            "data": {
                "app_id": app_id,
                "is_valid": True,
                "type": "SYSTEM_USER",
                "user_id": "7001",
                "scopes": ["instagram_basic", "instagram_content_publish"],
                "expires_at": 0,
                "data_access_expires_at": 0,
            }
        },
        "/v26.0/me": {"id": "7001", "client_business_id": "8001"},
        "/v26.0/me/assigned_instagram_accounts": {
            "data": [{"id": "9001", "username": "synthetic_publisher"}]
        },
    }


def install_provider(api, payloads=None):
    from instagram_publisher.unified import UnifiedMetaClient

    payloads = payloads or responses()
    calls = []

    def respond(request):
        calls.append(request)
        result = payloads.get(request.url.path)
        if callable(result):
            return result(request)
        assert result is not None, request.url.path
        return httpx.Response(200, json=result)

    from pydantic import SecretStr

    api.settings.meta_unified_app_id = "123456789"
    api.settings.meta_unified_app_secret = SecretStr("synthetic-unified-app-secret")
    provider = UnifiedMetaClient(
        app_id=api.settings.meta_unified_app_id,
        app_secret=api.settings.meta_unified_app_secret.get_secret_value(),
        redirect_uri=api.settings.redirect_uri,
        transport=httpx.MockTransport(respond),
    )
    api.app.state.unified_client = provider
    return provider, calls


def import_account(api, username="@SYNTHETIC_PUBLISHER"):
    return api.client.post(
        "/api/accounts/import-unified-token",
        headers=api.headers,
        json={"access_token": TOKEN, "username": username},
    )


def upload(api, width=1080, height=1350):
    data = io.BytesIO()
    Image.new("RGB", (width, height), "orange").save(data, "JPEG")
    result = api.client.post(
        "/api/assets",
        headers=api.headers,
        files={"file": ("synthetic.jpg", data.getvalue(), "image/jpeg")},
        data={"kind": "story" if height == 1920 else "image"},
    )
    assert result.status_code == 201
    return result.json()["id"]


def draft(api, account_id, **extra):
    result = api.client.post(
        "/api/posts",
        headers=api.headers,
        json={"account_id": account_id, "asset_ids": [upload(api)], **extra},
    )
    assert result.status_code == 201
    return result.json()["id"]


def test_import_uses_debug_token_identity_when_me_omits_id(api_bundle):
    payloads = responses()
    payloads["/v26.0/me"] = {"client_business_id": "8001"}
    provider, _ = install_provider(api_bundle, payloads)
    response = import_account(api_bundle)
    assert response.status_code == 200
    with api_bundle.sessions() as session:
        account = session.get(Account, response.json()["id"])
        assert account.unified_grant.system_user_id == "7001"
        assert account.unified_grant.business_id == "8001"
    provider.close()


def test_import_stores_grant_separately_and_never_returns_credentials(api_bundle):
    provider, calls = install_provider(api_bundle)
    response = import_account(api_bundle)
    assert response.status_code == 200
    data = response.json()
    assert data["auth_provider"] == "UNIFIED"
    assert data["account_type"] is None
    assert data["token_expires_at"] is None
    assert TOKEN not in response.text
    assert "token_ciphertext" not in response.text
    with api_bundle.sessions() as session:
        account = session.get(Account, data["id"])
        assert account.token_ciphertext is None
        assert account.token_refreshed_at is None
        assert account.token_expires_at is None
        assert account.app_scoped_id is None
        grant = account.unified_grant
        assert (grant.system_user_id, grant.business_id, grant.issuer_app_id) == ("7001", "8001", "123456789")
        assert (
            decrypt_token(grant.token_ciphertext, api_bundle.settings.token_encryption_key.get_secret_value())
            == TOKEN
        )
    assert all(request.url.host == "graph.facebook.com" for request in calls)
    assert all(request.method == "GET" for request in calls)
    assert TOKEN not in calls[0].headers["authorization"]
    assert calls[0].url.params["input_token"] == TOKEN
    provider.close()


@pytest.mark.parametrize(
    "change",
    [
        {"app_id": "different-app"},
        {"is_valid": False},
        {"type": "USER"},
        {"scopes": ["instagram_basic"]},
        {"expires_at": 1},
        {"data_access_expires_at": 1},
        {"expires_at": True},
        {"expires_at": None},
        {"user_id": "7002"},
    ],
)
def test_import_rejects_invalid_grant(api_bundle, change):
    payloads = responses()
    payloads["/v26.0/debug_token"]["data"].update(change)
    provider, _ = install_provider(api_bundle, payloads)
    response = import_account(api_bundle)
    assert response.status_code == 422
    assert TOKEN not in response.text
    with api_bundle.sessions() as session:
        assert session.scalars(select(Account)).all() == []
    provider.close()


@pytest.mark.parametrize(
    "body",
    [
        {"access_token": TOKEN},
        {"access_token": {"nested": TOKEN}, "username": "synthetic"},
        {"access_token": TOKEN, "username": "", "unexpected": TOKEN},
        {"access_token": TOKEN, "username": [TOKEN]},
    ],
)
def test_import_validation_never_reflects_secret_input(api_bundle, body):
    response = api_bundle.client.post(
        "/api/accounts/import-unified-token", headers=api_bundle.headers, json=body
    )
    assert response.status_code == 422
    assert TOKEN not in response.text


def test_import_requires_exact_username_and_authentication(api_bundle):
    provider, calls = install_provider(api_bundle)
    denied = api_bundle.client.post(
        "/api/accounts/import-unified-token", json={"access_token": TOKEN, "username": "synthetic_publisher"}
    )
    assert denied.status_code == 401
    assert calls == []
    response = import_account(api_bundle, "somebody_else")
    assert response.status_code == 422
    assert len(calls) == 3
    provider.close()


def test_discovery_paginates_only_trusted_endpoint_and_rejects_ambiguous_username(api_bundle):
    payloads = responses()

    def page(request):
        if request.url.params.get("after") == "second-page":
            return httpx.Response(200, json={"data": [{"id": "9001", "username": "synthetic_publisher"}]})
        return httpx.Response(
            200,
            json={
                "data": [{"id": "9002", "username": "synthetic_publisher"}],
                "paging": {"cursors": {"after": "second-page"}, "next": "https://malicious.example/steal"},
            },
        )

    payloads["/v26.0/me/assigned_instagram_accounts"] = page
    provider, calls = install_provider(api_bundle, payloads)
    response = import_account(api_bundle)
    assert response.status_code == 422
    assert len(calls) == 4
    assert all(request.url.host == "graph.facebook.com" for request in calls)
    provider.close()


def test_legacy_and_unified_matching_numeric_ids_are_separate_accounts(api_bundle):
    from test_recovery_and_callbacks import connect_account

    api_bundle.provider.profile["user_id"] = "9001"
    legacy = connect_account(api_bundle)
    provider, _ = install_provider(api_bundle)
    unified = import_account(api_bundle).json()
    assert unified["id"] != legacy["id"]
    with api_bundle.sessions() as session:
        assert len(session.scalars(select(Account)).all()) == 2
    provider.close()


def test_shared_grant_rotation_and_revocation_apply_to_all_linked_assets(api_bundle):
    payloads = responses()
    provider, _ = install_provider(api_bundle, payloads)
    first = import_account(api_bundle).json()
    payloads["/v26.0/me/assigned_instagram_accounts"]["data"] = [
        {"id": "9001", "username": "synthetic_publisher"},
        {"id": "9002", "username": "other_synthetic"},
    ]
    second = import_account(api_bundle, "other_synthetic").json()
    assert first["grant_id"] == second["grant_id"]
    post_ids = [draft(api_bundle, account["id"]) for account in (first, second)]
    response = api_bundle.client.post(f"/api/grants/{first['grant_id']}/revoke", headers=api_bundle.headers)
    assert response.status_code == 200
    with api_bundle.sessions() as session:
        assert all(session.get(Post, post_id).status == "CANCELED" for post_id in post_ids)
        assert session.get(Account, first["id"]).unified_grant.token_ciphertext == ""
    provider.close()


@pytest.mark.parametrize("status", ["QUEUED", "PROCESSING", "NEEDS_ATTENTION"])
def test_import_does_not_rebind_active_or_uncertain_posts(api_bundle, status):
    payloads = responses()
    provider, _ = install_provider(api_bundle, payloads)
    first = import_account(api_bundle).json()
    with api_bundle.sessions.begin() as session:
        session.add(Post(account_id=first["id"], kind="IMAGE", asset_ids=[], status=status))
    payloads["/v26.0/debug_token"]["data"]["user_id"] = "7002"
    payloads["/v26.0/me"]["id"] = "7002"
    response = import_account(api_bundle)
    assert response.status_code == 409
    provider.close()


def test_unified_expiry_approval_refresh_and_story_checks(api_bundle):
    payloads = responses()
    payloads["/v26.0/debug_token"]["data"]["data_access_expires_at"] = int(time.time()) + 3600
    provider, calls = install_provider(api_bundle, payloads)
    account = import_account(api_bundle).json()
    api_bundle.settings.publisher_mode = "live"
    post_id = draft(api_bundle, account["id"])
    response = api_bundle.client.post(
        f"/api/posts/{post_id}/approve", headers=api_bundle.headers, json={"revision": 1}
    )
    assert response.status_code == 200
    response = api_bundle.client.post(f"/api/accounts/{account['id']}/refresh", headers=api_bundle.headers)
    assert response.status_code == 409
    assert refresh_due_accounts(api_bundle.settings, api_bundle.sessions, client=api_bundle.provider) == 0
    assert api_bundle.provider.refreshed_tokens == []
    story = api_bundle.client.post(
        "/api/posts",
        headers=api_bundle.headers,
        json={"account_id": account["id"], "kind": "STORY", "asset_ids": [upload(api_bundle, 1080, 1920)]},
    ).json()
    response = api_bundle.client.post(
        f"/api/posts/{story['id']}/approve", headers=api_bundle.headers, json={"revision": 1}
    )
    assert response.status_code == 422
    with api_bundle.sessions.begin() as session:
        session.get(Account, account["id"]).unified_grant.data_access_expires_at = time.time() - 1
    assert run_once(api_bundle.settings, api_bundle.sessions, client=provider)
    with api_bundle.sessions() as session:
        assert session.get(Post, post_id).status == "FAILED"
    assert len(calls) == 3
    provider.close()


def test_unified_publish_and_reconcile_use_facebook_graph_without_duplicate_publish(api_bundle, monkeypatch):
    payloads = responses()
    provider, calls = install_provider(api_bundle, payloads)
    account = import_account(api_bundle).json()
    api_bundle.settings.publisher_mode = "live"
    post_id = draft(api_bundle, account["id"])
    payloads.update(
        {
            "/v26.0/9001/content_publishing_limit": {
                "data": [{"quota_usage": 0, "config": {"quota_total": 100}}]
            },
            "/v26.0/9001/media": {"id": "10001"},
            "/v26.0/10001": {"status_code": "FINISHED"},
            "/v26.0/9001/media_publish": lambda request: (_ for _ in ()).throw(
                httpx.ReadTimeout("synthetic", request=request)
            ),
        }
    )
    response = api_bundle.client.post(
        f"/api/posts/{post_id}/approve", headers=api_bundle.headers, json={"revision": 1}
    )
    assert response.status_code == 200
    import instagram_publisher.worker as worker
    from instagram_publisher.credentials import client_for_account

    def worker_client(settings, account):
        selected = client_for_account(settings, account)
        selected._http.close()
        selected._http = provider._http
        return selected

    monkeypatch.setattr(worker, "client_for_account", worker_client)
    assert run_once(api_bundle.settings, api_bundle.sessions)
    with api_bundle.sessions() as session:
        assert session.get(Post, post_id).status == "NEEDS_ATTENTION"
    payloads["/v26.0/10001"] = {"status_code": "PUBLISHED"}
    replacement, reconciliation_calls = install_provider(api_bundle, payloads)
    response = api_bundle.client.post(f"/api/posts/{post_id}/reconcile", headers=api_bundle.headers)
    assert response.status_code == 200
    assert response.json()["resolved"] is True
    calls += reconciliation_calls
    replacement.close()
    assert all(request.url.host == "graph.facebook.com" for request in calls)
    assert sum(request.url.path.endswith("/media_publish") for request in calls) == 1
    provider.close()


def test_legacy_callback_never_revokes_unified_system_user(api_bundle):
    provider, _ = install_provider(api_bundle)
    account = import_account(api_bundle).json()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"algorithm": "HMAC-SHA256", "user_id": "7001"}).encode())
        .decode()
        .rstrip("=")
    )
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(
                api_bundle.settings.meta_app_secret.get_secret_value().encode(),
                payload.encode(),
                hashlib.sha256,
            ).digest()
        )
        .decode()
        .rstrip("=")
    )
    response = api_bundle.client.post(
        "/auth/instagram/deauthorize", data={"signed_request": signature + "." + payload}
    )
    assert response.status_code == 200
    with api_bundle.sessions() as session:
        assert session.get(Account, account["id"]).unified_grant.revoked_at is None
    provider.close()


def test_provider_failures_do_not_expose_token_or_body(api_bundle):
    payloads = responses()
    payloads["/v26.0/debug_token"] = lambda request: httpx.Response(400, json={"error": {"message": TOKEN}})
    provider, _ = install_provider(api_bundle, payloads)
    response = import_account(api_bundle)
    assert response.status_code == 422
    assert TOKEN not in response.text
    provider.close()


@pytest.mark.parametrize("cursors", [None, [], "synthetic"])
def test_discovery_rejects_malformed_cursors_safely(api_bundle, cursors):
    payloads = responses()
    payloads["/v26.0/me/assigned_instagram_accounts"]["paging"] = {
        "next": "https://example.test",
        "cursors": cursors,
    }
    provider, _ = install_provider(api_bundle, payloads)
    response = import_account(api_bundle)
    assert response.status_code == 422
    assert TOKEN not in response.text
    provider.close()


@pytest.mark.parametrize("token", ["synthetic\ncredential", "synthetic 🔑 credential"])
def test_non_header_tokens_are_rejected_before_provider_calls(api_bundle, token):
    provider, calls = install_provider(api_bundle)
    response = api_bundle.client.post(
        "/api/accounts/import-unified-token",
        headers=api_bundle.headers,
        json={"access_token": token, "username": "synthetic_publisher"},
    )
    assert response.status_code == 422
    assert calls == []
    assert token not in response.text
    provider.close()


def test_narrower_token_cannot_replace_shared_grant_credentials(api_bundle):
    payloads = responses()
    provider, _ = install_provider(api_bundle, payloads)
    first = import_account(api_bundle).json()
    post_id = draft(api_bundle, first["id"])
    with api_bundle.sessions.begin() as session:
        session.get(Post, post_id).status = "QUEUED"
    payloads["/v26.0/me/assigned_instagram_accounts"]["data"] = [
        {"id": "9002", "username": "other_synthetic"}
    ]
    response = api_bundle.client.post(
        "/api/accounts/import-unified-token",
        headers=api_bundle.headers,
        json={"access_token": "synthetic-narrower-token", "username": "other_synthetic"},
    )
    assert response.status_code == 409
    with api_bundle.sessions() as session:
        account = session.get(Account, first["id"])
        assert (
            decrypt_token(
                account.unified_grant.token_ciphertext,
                api_bundle.settings.token_encryption_key.get_secret_value(),
            )
            == TOKEN
        )
        assert session.get(Post, post_id).status == "QUEUED"
        assert len(session.scalars(select(Account)).all()) == 1
    provider.close()
