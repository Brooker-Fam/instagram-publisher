import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from instagram_publisher.meta import SCOPES, MetaClient, MetaError


def client(handler):
    return MetaClient(
        app_id="123",
        app_secret="private-app-secret",
        redirect_uri="https://publisher.example/auth/instagram/callback",
        transport=httpx.MockTransport(handler),
    )


def test_authorization_uses_minimum_scopes_and_preserves_state():
    with client(lambda _: httpx.Response(200, json={})) as meta:
        result = urlsplit(meta.authorization_url("random-state+/="))
    assert result.netloc == "www.instagram.com"
    query = parse_qs(result.query)
    assert query["scope"] == [",".join(SCOPES)]
    assert query["state"] == ["random-state+/="]
    assert query["force_reauth"] == ["true"]
    assert query["enable_fb_login"] == ["false"]
    assert "client_secret" not in query


@pytest.mark.parametrize("wrapped", [True, False])
def test_exchange_code_accepts_current_and_legacy_shapes(wrapped):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "api.instagram.com":
            form = parse_qs(request.content.decode())
            assert request.method == "POST"
            assert form["grant_type"] == ["authorization_code"]
            assert form["code"] == ["auth-code"]
            assert "private-app-secret" not in str(request.url)
            value = {"access_token": "short-token", "user_id": "987", "permissions": ",".join(SCOPES)}
            return httpx.Response(200, json={"data": [value]} if wrapped else value)
        assert request.method == "GET"
        assert request.url.path == "/access_token"
        assert request.url.params["grant_type"] == "ig_exchange_token"
        assert request.url.params["access_token"] == "short-token"
        return httpx.Response(200, json={"access_token": "long-token", "expires_in": 5184000})

    with client(handler) as meta:
        result = meta.exchange_code("auth-code")
    assert result["access_token"] == "long-token"
    assert result["user_id"] == "987"
    assert result["expires_in"] == 5184000
    assert len(seen) == 2


def test_exchange_refuses_ambiguous_multi_account_response():
    with client(lambda _: httpx.Response(200, json={"data": [{"access_token": "a"}, {"access_token": "b"}]})) as meta:
        with pytest.raises(MetaError) as error:
            meta.exchange_code("code")
    assert error.value.ambiguous


def test_declined_publishing_scope_does_not_exchange_long_token():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"access_token": "short", "permissions": "instagram_business_basic"})

    with client(handler) as meta:
        with pytest.raises(MetaError, match="content publishing"):
            meta.exchange_code("code")
    assert len(requests) == 1


def test_refresh_uses_unversioned_refresh_endpoint():
    def handler(request):
        assert request.url.path == "/refresh_access_token"
        assert request.url.params["grant_type"] == "ig_refresh_token"
        return httpx.Response(200, json={"access_token": "refreshed", "expires_in": 5184000})

    with client(handler) as meta:
        assert meta.refresh_token("old")["access_token"] == "refreshed"


@pytest.mark.parametrize("wrapped", [True, False])
def test_profile_normalizes_professional_id(wrapped):
    def handler(request):
        assert request.url.path == "/v26.0/me"
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert "secret-token" not in str(request.url)
        value = {"id": "111", "user_id": "987", "username": "publisher_test", "account_type": "Business"}
        return httpx.Response(200, json={"data": [value]} if wrapped else value)

    with client(handler) as meta:
        result = meta.get_profile("secret-token")
    assert result["id"] == result["user_id"] == "987"
    assert result["app_scoped_id"] == "111"


def test_image_creation_and_publish_are_separate_bearer_requests():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert "secret-token" not in str(request.url)
        assert request.headers["Content-Type"].startswith("application/json")
        body = json.loads(request.content)
        if request.url.path.endswith("/media"):
            assert body == {"image_url": "https://media.example/photo.jpg", "caption": "Small wins matter", "alt_text": "Orange graphic"}
            return httpx.Response(200, json={"id": "222"})
        assert body == {"creation_id": "222"}
        return httpx.Response(200, json={"id": "333"})

    with client(handler) as meta:
        container = meta.create_container("987", "secret-token", image_url="https://media.example/photo.jpg", caption="Small wins matter", alt_text="Orange graphic")
        assert len(calls) == 1  # Creation never implicitly publishes.
        assert meta.publish_container("987", "secret-token", container) == "333"
    assert len(calls) == 2


@pytest.mark.parametrize("media_type,fields", [
    ("REELS", {"video_url": "https://media.example/video.mp4", "share_to_feed": True}),
    ("STORIES", {"image_url": "https://media.example/story.jpg"}),
    ("CAROUSEL", {"children": ["222", "333"]}),
])
def test_container_fields_for_supported_media(media_type, fields):
    def handler(request):
        body = json.loads(request.content)
        assert body["media_type"] == media_type
        if media_type == "CAROUSEL":
            assert body["children"] == "222,333"
        if media_type == "REELS":
            assert body["share_to_feed"] is True
        return httpx.Response(200, json={"id": "444"})

    with client(handler) as meta:
        assert meta.create_container("987", "secret", media_type=media_type, **fields) == "444"


def test_status_media_and_quota_reads_request_correct_fields():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/content_publishing_limit"):
            assert request.url.params["fields"] == "config,quota_usage"
            return httpx.Response(200, json={"data": [{"quota_usage": 2, "config": {"quota_total": 100}}]})
        if request.url.path.endswith("/222"):
            assert request.url.params["fields"] == "status_code,status"
            return httpx.Response(200, json={"status_code": "FINISHED"})
        assert request.url.params["fields"] == "id,permalink,media_type,timestamp"
        return httpx.Response(200, json={"id": "333", "permalink": "https://www.instagram.com/p/example/"})

    with client(handler) as meta:
        assert meta.container_status("222", "token")["status_code"] == "FINISHED"
        assert meta.get_media("333", "token")["id"] == "333"
        assert meta.publishing_limit("987", "token")["data"][0]["quota_usage"] == 2
    assert len(paths) == 3


@pytest.mark.parametrize("status,body,expected_code,retryable,ambiguous", [
    (400, {"error": {"code": 190, "message": "secret-token private-app-secret"}}, 190, False, False),
    (403, {"error": {"code": 10, "message": "secret-token"}}, 10, False, False),
    (429, {"error": {"code": 4, "message": "secret-token"}}, 4, True, False),
    (503, {"error": {"code": 2, "message": "secret-token"}}, 2, True, True),
])
def test_errors_are_safe_and_mutations_not_retried(status, body, expected_code, retryable, ambiguous):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    with client(handler) as meta:
        with pytest.raises(MetaError) as caught:
            meta.publish_container("987", "secret-token", "222")
    error = caught.value
    assert error.code == expected_code
    assert error.retryable is retryable
    assert error.ambiguous is ambiguous
    assert "secret-token" not in str(error)
    assert "private-app-secret" not in repr(error)
    assert len(calls) == 1


def test_publish_timeout_is_ambiguous_and_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("url contains secret-token", request=request)

    with client(handler) as meta:
        with pytest.raises(MetaError) as caught:
            meta.publish_container("987", "secret-token", "222")
    assert caught.value.retryable and caught.value.ambiguous
    assert "secret-token" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert len(calls) == 1


@pytest.mark.parametrize("response", [httpx.Response(200, content=b"invalid secret-token"), httpx.Response(200, json={})])
def test_unreadable_success_is_ambiguous_after_publish(response):
    with client(lambda _: response) as meta:
        with pytest.raises(MetaError) as caught:
            meta.publish_container("987", "secret-token", "222")
    assert caught.value.ambiguous
    assert "secret-token" not in str(caught.value)


def test_redirect_not_followed_and_secret_stays_on_origin():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://attacker.example/"})

    with client(handler) as meta:
        with pytest.raises(MetaError, match="redirect"):
            meta.get_profile("secret-token")
    assert len(calls) == 1


@pytest.mark.parametrize("bad_id", ["../me", "1?access_token=stolen", "1/2", "", "abc"])
def test_invalid_object_id_never_reaches_network(bad_id):
    def handler(_):
        pytest.fail("Invalid ID reached network")

    with client(handler) as meta:
        with pytest.raises(MetaError, match="object ID"):
            meta.create_container(bad_id, "token", image_url="https://media.example/a.jpg")
