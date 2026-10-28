"""Verify Meta deauthorization and data-deletion callback signatures.

Meta posts a form field named ``signed_request``. The signature authenticates
the *encoded* payload, not its decoded JSON. These helpers do no I/O and no
deletion: the callback handler owns account lookup and idempotency.

The callback specification does not define a maximum delivery/retry age. We
therefore honor an explicit expiry, reject implausible future timestamps, and
do not invent a short freshness window that could discard valid deletion
requests. Verification alone cannot prevent replay. Handlers must retain a
callback ledger and must not let an old deauthorization revoke a newer login.

Source: https://developers.facebook.com/docs/development/create-an-app/
app-dashboard/data-deletion-callback/
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import time
from typing import Any

MAX_SIGNED_REQUEST_LENGTH = 16_384
FUTURE_CLOCK_TOLERANCE_SECONDS = 300
_ENCODED = re.compile(r"[A-Za-z0-9_-]+={0,2}\Z")
_USER_ID = re.compile(r"[1-9][0-9]{0,39}\Z")


class SignedRequestError(ValueError):
    """A deliberately generic error safe to display without callback contents."""


def _invalid() -> SignedRequestError:
    return SignedRequestError("Invalid Meta signed request.")


def _decode_urlsafe(value: str) -> bytes:
    if not _ENCODED.fullmatch(value):
        raise _invalid()
    unpadded = value.rstrip("=")
    try:
        raw = base64.b64decode(unpadded + "=" * (-len(unpadded) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise _invalid() from None
    # Reject non-canonical encodings whose ignored bits create replay aliases.
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != unpadded:
        raise _invalid()
    return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise _invalid()


def _timestamp(payload: dict[str, Any], key: str) -> int | None:
    if key not in payload:
        return None
    value = payload[key]
    # bool is an int subclass; accept only genuine JSON integer timestamps.
    if type(value) is not int or value < 0:
        raise _invalid()
    return value


def verify_signed_request(
    signed_request: str, app_secret: str, now: int | float | None = None
) -> dict[str, Any]:
    """Authenticate and validate one Meta lifecycle callback payload.

    ``now`` is a UTC Unix timestamp, injectable for tests. ``issued_at`` and
    ``expires`` are optional because the docs show them as example fields
    without a required-field contract. An absent/zero ``expires`` imposes no
    expiry. The five-minute future tolerance is an application clock-skew
    check, not a Meta delivery-age requirement.

    The returned ``user_id`` is a normalized app-scoped ID, not necessarily
    the professional account ID used by the publishing API. Extra signed
    fields are preserved; never log or return this entire object to clients.
    """
    if not isinstance(app_secret, str) or not app_secret:
        raise SignedRequestError("Meta callback verification is not configured.")
    if (
        not isinstance(signed_request, str)
        or not 1 <= len(signed_request) <= MAX_SIGNED_REQUEST_LENGTH
        or signed_request.count(".") != 1
    ):
        raise _invalid()
    encoded_signature, encoded_payload = signed_request.split(".", 1)
    signature = _decode_urlsafe(encoded_signature)
    if len(signature) != hashlib.sha256().digest_size:
        raise _invalid()
    # Validating base64 first also ensures the signed data is bounded ASCII.
    raw_payload = _decode_urlsafe(encoded_payload)
    expected_signature = hmac.new(
        app_secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(signature, expected_signature):
        raise _invalid()
    try:
        payload = json.loads(
            raw_payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid() from None
    if not isinstance(payload, dict):
        raise _invalid()
    algorithm = payload.get("algorithm")
    if not isinstance(algorithm, str) or algorithm.upper() != "HMAC-SHA256":
        raise _invalid()
    user_id = payload.get("user_id")
    if type(user_id) is int:
        user_id = str(user_id)
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise _invalid()
    current_time = time.time() if now is None else now
    if isinstance(current_time, bool) or not isinstance(current_time, (int, float)):
        raise ValueError("now must be a finite UTC Unix timestamp.")
    if not math.isfinite(current_time) or current_time < 0:
        raise ValueError("now must be a finite UTC Unix timestamp.")
    issued_at = _timestamp(payload, "issued_at")
    expires = _timestamp(payload, "expires")
    if issued_at is not None and issued_at > current_time + FUTURE_CLOCK_TOLERANCE_SECONDS:
        raise _invalid()
    if expires:
        if expires <= current_time or (issued_at is not None and expires < issued_at):
            raise _invalid()
    return {**payload, "algorithm": "HMAC-SHA256", "user_id": user_id}
