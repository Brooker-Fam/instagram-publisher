import base64
import hashlib
import hmac
import json

import pytest

from instagram_publisher.lifecycle import (
    MAX_SIGNED_REQUEST_LENGTH,
    SignedRequestError,
    verify_signed_request,
)

SECRET = "private-instagram-app-secret"
NOW = 1_800_000_000


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def sign(payload, secret=SECRET):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    encoded_payload = encode(raw)
    signature = hmac.new(secret.encode(), encoded_payload.encode(), hashlib.sha256).digest()
    return f"{encode(signature)}.{encoded_payload}"


def base_payload(**fields):
    return {"algorithm": "HMAC-SHA256", "user_id": "123456789", "issued_at": NOW - 60, **fields}


def test_verifies_current_meta_callback_shape_and_preserves_app_scoped_id():
    result = verify_signed_request(sign(base_payload(expires=NOW + 60)), SECRET, now=NOW)
    assert result == base_payload(expires=NOW + 60)


def test_numeric_id_and_algorithm_are_normalized():
    result = verify_signed_request(sign(base_payload(user_id=123, algorithm="hmac-sha256")), SECRET, now=NOW)
    assert result["user_id"] == "123"
    assert result["algorithm"] == "HMAC-SHA256"


def test_signature_is_over_encoded_payload_not_decoded_bytes():
    raw = json.dumps(base_payload()).encode()
    incorrect_signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()
    with pytest.raises(SignedRequestError):
        verify_signed_request(f"{encode(incorrect_signature)}.{encode(raw)}", SECRET, now=NOW)


def test_tampered_payload_and_wrong_app_secret_fail():
    signed = sign(base_payload())
    signature, _ = signed.split(".")
    tampered = f"{signature}.{encode(json.dumps(base_payload(user_id='999')).encode())}"
    for value, secret in [(tampered, SECRET), (signed, "wrong-app-secret")]:
        with pytest.raises(SignedRequestError) as caught:
            verify_signed_request(value, secret, now=NOW)
        assert SECRET not in str(caught.value)
        assert value not in str(caught.value)


@pytest.mark.parametrize("value", ["", "abc", "a.b.c", ".payload", "a.", "!!!!.aaaa", "a.a", "a\nb.payload"])
def test_malformed_or_non_base64_data_fails(value):
    with pytest.raises(SignedRequestError):
        verify_signed_request(value, SECRET, now=NOW)


def test_request_length_is_bounded_before_decoding():
    with pytest.raises(SignedRequestError):
        verify_signed_request("x" * MAX_SIGNED_REQUEST_LENGTH + ".y", SECRET, now=NOW)


@pytest.mark.parametrize("raw", [
    b"not json private-instagram-app-secret",
    b"[]",
    b"null",
    b'{"algorithm":"HMAC-SHA256","user_id":"123","user_id":"456"}',
    b'{"algorithm":"HMAC-SHA256","user_id":"123","issued_at":NaN}',
    b"\xff\xfe",
])
def test_invalid_json_is_rejected_without_payload_leaks(raw):
    with pytest.raises(SignedRequestError) as caught:
        verify_signed_request(sign(raw), SECRET, now=NOW)
    assert SECRET not in str(caught.value)
    assert str(caught.value) == "Invalid Meta signed request."


@pytest.mark.parametrize("algorithm", [None, "SHA256", "HMAC-SHA1", "none", 123])
def test_rejects_wrong_or_missing_algorithm(algorithm):
    with pytest.raises(SignedRequestError):
        verify_signed_request(sign(base_payload(algorithm=algorithm)), SECRET, now=NOW)


@pytest.mark.parametrize("user_id", [None, True, "", "0", "-1", "1/2", "1?x=y", "x" * 100, "9" * 41])
def test_rejects_invalid_user_id(user_id):
    with pytest.raises(SignedRequestError):
        verify_signed_request(sign(base_payload(user_id=user_id)), SECRET, now=NOW)


@pytest.mark.parametrize("fields", [
    {"expires": NOW},
    {"expires": NOW - 1},
    {"issued_at": NOW + 301},
    {"issued_at": -1},
    {"issued_at": True},
    {"issued_at": "1800000000"},
    {"expires": 1.5},
    {"expires": False},
    {"issued_at": NOW + 200, "expires": NOW + 100},
])
def test_expired_future_or_malformed_timestamps_fail(fields):
    with pytest.raises(SignedRequestError):
        verify_signed_request(sign(base_payload(**fields)), SECRET, now=NOW)


def test_old_callbacks_without_expiry_are_not_rejected_by_invented_ttl():
    # Meta documents neither a callback delivery TTL nor a retry interval.
    for expires in ({}, {"expires": 0}):
        payload = base_payload(issued_at=NOW - 86_400 * 30, **expires)
        assert verify_signed_request(sign(payload), SECRET, now=NOW)["user_id"] == "123456789"


def test_optional_example_timestamps_not_assumed_mandatory():
    payload = {"algorithm": "HMAC-SHA256", "user_id": "123"}
    assert verify_signed_request(sign(payload), SECRET, now=NOW) == payload


def test_pure_verification_does_not_claim_replay_protection():
    signed = sign(base_payload())
    assert verify_signed_request(signed, SECRET, now=NOW) == verify_signed_request(signed, SECRET, now=NOW)


def test_missing_app_secret_fails_closed():
    with pytest.raises(SignedRequestError, match="not configured"):
        verify_signed_request(sign(base_payload()), "", now=NOW)


def test_constant_time_comparison_is_used(monkeypatch):
    calls = []
    real_compare = hmac.compare_digest

    def compare(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(hmac, "compare_digest", compare)
    verify_signed_request(sign(base_payload()), SECRET, now=NOW)
    assert len(calls) == 1
    assert len(calls[0][0]) == len(calls[0][1]) == 32
