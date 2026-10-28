import asyncio

import pytest
from starlette.exceptions import HTTPException

from instagram_publisher.guards import RequestGuard

ADMIN_KEY = "test-admin-key-with-at-least-thirty-two-characters"


def scope(path="/api/assets", headers=()):
    return {"type": "http", "method": "POST", "path": path, "headers": list(headers)}


def authorized_headers(*extra):
    return [(b"authorization", f"Bearer {ADMIN_KEY}".encode()), *extra]


def test_unauthorized_upload_never_reads_body_or_calls_application():
    sent = []

    async def application(*_):
        pytest.fail("Unauthorized request reached the application")

    async def receive():
        pytest.fail("Unauthorized upload body was consumed")

    async def send(message):
        sent.append(message)

    guard = RequestGuard(application, ADMIN_KEY, upload_limit=16)
    asyncio.run(guard(scope(), receive, send))
    assert sent[0]["status"] == 401
    assert (b"www-authenticate", b"Bearer") in sent[0]["headers"]


@pytest.mark.parametrize("authorization", [b"Basic abc", b"Bearer incorrect", b"Bearer", b""])
def test_invalid_authentication_does_not_consume_even_declared_body(authorization):
    sent = []

    async def fail(*_):
        pytest.fail("Authentication must be checked before parsing")

    async def send(message):
        sent.append(message)

    guard = RequestGuard(fail, ADMIN_KEY, upload_limit=16)
    request_scope = scope(headers=[(b"authorization", authorization), (b"content-length", b"1000000")])
    asyncio.run(guard(request_scope, fail, send))
    assert sent[0]["status"] == 401


@pytest.mark.parametrize("length", [b"17", b"-1", b"invalid"])
def test_invalid_or_oversize_content_length_rejected_before_receive(length):
    sent = []

    async def fail(*_):
        pytest.fail("Rejected body should not be consumed")

    async def send(message):
        sent.append(message)

    guard = RequestGuard(fail, ADMIN_KEY, upload_limit=16)
    asyncio.run(guard(scope(headers=authorized_headers((b"content-length", length))), fail, send))
    assert sent[0]["status"] == 413


def test_chunked_upload_is_limited_when_content_length_is_missing():
    chunks = iter([b"12345678", b"12345678", b"X"])
    seen = []

    async def receive():
        return {"type": "http.request", "body": next(chunks), "more_body": True}

    async def send(_):
        pytest.fail("This test application should not send a response")

    async def application(_, receive, __):
        while True:
            seen.append((await receive())["body"])

    guard = RequestGuard(application, ADMIN_KEY, upload_limit=16)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(guard(scope(headers=authorized_headers()), receive, send))
    assert raised.value.status_code == 413
    assert seen == [b"12345678", b"12345678"]


def test_legal_chunked_body_and_disconnect_are_forwarded():
    messages = [
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"5678", "more_body": False},
        {"type": "http.disconnect"},
    ]
    source = iter(messages)
    seen = []

    async def receive():
        return next(source)

    async def send(_):
        pass

    async def application(_, receive, __):
        for _ in messages:
            seen.append(await receive())

    guard = RequestGuard(application, ADMIN_KEY, upload_limit=8)
    asyncio.run(guard(scope(headers=authorized_headers()), receive, send))
    assert seen == messages


def test_non_api_callback_is_not_subject_to_admin_authentication():
    seen = []

    async def receive():
        return {"type": "http.request", "body": b"signed_request=value", "more_body": False}

    async def send(_):
        pass

    async def application(_, receive, __):
        seen.append(await receive())

    guard = RequestGuard(application, ADMIN_KEY, upload_limit=8)
    asyncio.run(guard(scope(path="/auth/instagram/data-deletion"), receive, send))
    assert seen[0]["body"] == b"signed_request=value"


def test_non_http_scope_is_forwarded_unchanged():
    received = []

    async def receive():
        pass

    async def send(_):
        pass

    async def application(actual_scope, actual_receive, actual_send):
        received.append((actual_scope, actual_receive, actual_send))

    guard = RequestGuard(application, ADMIN_KEY, upload_limit=8)
    lifespan = {"type": "lifespan"}
    asyncio.run(guard(lifespan, receive, send))
    assert received == [(lifespan, receive, send)]
