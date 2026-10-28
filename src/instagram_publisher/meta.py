"""Small, synchronous client for the Instagram API with Instagram Login.

Publishing is deliberately two-phase: create a container, wait for FINISHED,
then publish it. This module never retries HTTP requests. In particular, a
publish timeout must be reconciled by the caller instead of blindly retried.

OAuth token exchange/refresh are documented GET endpoints with credentials in
the query string. The application must not enable HTTP wire/request logging.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

import httpx

SCOPES = ("instagram_business_basic", "instagram_business_content_publish")
GRAPH_HOST = "https://graph.instagram.com"
AUTHORIZATION_ENDPOINT = "https://www.instagram.com/oauth/authorize"
TOKEN_ENDPOINT = "https://api.instagram.com/oauth/access_token"


class MetaError(Exception):
    """A safe error: never contains provider response bodies or credentials.

    ``retryable`` indicates a transient condition, not permission to repeat a
    mutation. If ``ambiguous`` is true, reconcile remote state first.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        ambiguous: bool = False,
        code: int | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.code = code
        self.status = status


class MetaClient:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        redirect_uri: str,
        api_version: str = "v26.0",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not re.fullmatch(r"v\d+\.\d+", api_version):
            raise ValueError("Meta API version must have the form v26.0.")
        self.app_id = app_id
        self.app_secret = app_secret
        self.redirect_uri = redirect_uri
        self.api_version = api_version
        self.base_url = f"{GRAPH_HOST}/{api_version}"
        self._http = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "InstagramPublisher/0.1"},
        )

    def __enter__(self) -> MetaClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _oauth_configured(self) -> None:
        if not all((self.app_id, self.app_secret, self.redirect_uri)):
            raise MetaError("Instagram app ID, app secret, and redirect URI are required.")

    @staticmethod
    def _object_id(value: str) -> str:
        result = str(value)
        if not re.fullmatch(r"[0-9]{1,40}", result):
            raise MetaError("Invalid Instagram object ID.")
        return result

    @staticmethod
    def _single(payload: dict[str, Any], *, ambiguous: bool = False) -> dict[str, Any]:
        # New Instagram Login responses wrap their single object in data[].
        if "data" not in payload:
            return payload
        data = payload["data"]
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            return data[0]
        raise MetaError("Instagram returned an unexpected response.", ambiguous=ambiguous)

    @staticmethod
    def _error(payload: Any, status: int, *, mutation: bool) -> MetaError:
        detail = payload.get("error", payload) if isinstance(payload, dict) else {}
        if not isinstance(detail, dict):
            detail = {}
        raw_code = detail.get("code")
        code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
        transient = detail.get("is_transient") is True or status == 429 or status >= 500
        transient = transient or code in {1, 2, 4, 17, 32, 341, 613, 80004}
        if code in {102, 190} or status == 401:
            message = "Instagram authorization expired or was revoked. Reconnect the account."
        elif code in {10, 200, 294} or status == 403:
            message = "Instagram denied this operation. Check account eligibility and app permissions."
        elif status == 429 or code in {4, 17, 32, 341, 613, 80004}:
            message = "Instagram is rate limiting requests. Try again after the limit resets."
        elif status >= 500:
            message = "Instagram is temporarily unavailable."
        elif 300 <= status < 400:
            message = "Instagram returned an unexpected redirect."
        else:
            message = "Instagram rejected the request. Check the account and media requirements."
        # A gateway/server failure may happen after a mutation was committed.
        return MetaError(
            message,
            retryable=transient,
            ambiguous=mutation and status >= 500,
            code=code,
            status=status,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mutation = method != "GET"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = self._http.request(method, url, headers=headers, params=params, data=data, json=json)
        except httpx.HTTPError:
            # No chained httpx exception: its URL can contain OAuth credentials.
            raise MetaError(
                "Could not complete the Instagram request.",
                retryable=True,
                ambiguous=mutation,
            ) from None
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError):
            if not response.is_success:
                raise self._error({}, response.status_code, mutation=mutation) from None
            raise MetaError(
                "Instagram returned an unreadable response.",
                retryable=not mutation,
                ambiguous=mutation,
                status=response.status_code,
            ) from None
        if not response.is_success or (
            isinstance(payload, dict) and ("error" in payload or "error_type" in payload)
        ):
            raise self._error(payload, response.status_code, mutation=mutation)
        if not isinstance(payload, dict):
            raise MetaError(
                "Instagram returned an unexpected response.",
                ambiguous=mutation,
                status=response.status_code,
            )
        return payload

    def authorization_url(self, state: str) -> str:
        self._oauth_configured()
        if not state:
            raise MetaError("An OAuth state value is required.")
        return (
            AUTHORIZATION_ENDPOINT
            + "?"
            + urlencode(
                {
                    "client_id": self.app_id,
                    "redirect_uri": self.redirect_uri,
                    "response_type": "code",
                    "scope": ",".join(SCOPES),
                    "state": state,
                    "enable_fb_login": "false",
                    "force_reauth": "true",
                }
            )
        )

    @staticmethod
    def _token_result(payload: dict[str, Any]) -> dict[str, Any]:
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not token or not isinstance(expires_in, int) or expires_in <= 0:
            raise MetaError("Instagram returned an incomplete access-token response.")
        return {"access_token": token, "expires_in": expires_in, "token_type": "bearer"}

    def exchange_code(self, code: str) -> dict[str, Any]:
        self._oauth_configured()
        if not code:
            raise MetaError("An Instagram authorization code is required.")
        short = self._single(
            self._request(
                "POST",
                TOKEN_ENDPOINT,
                data={
                    "client_id": self.app_id,
                    "client_secret": self.app_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": self.redirect_uri,
                    "code": code,
                },
            ),
            ambiguous=True,
        )
        short_token = short.get("access_token")
        if not isinstance(short_token, str) or not short_token:
            raise MetaError("Instagram did not return an access token.")
        permissions = short.get("permissions")
        if isinstance(permissions, str):
            granted = {p.strip() for p in permissions.split(",")}
            if not set(SCOPES).issubset(granted):
                raise MetaError("Grant both Instagram basic access and content publishing permissions.")
        result = self._token_result(
            self._request(
                "GET",
                f"{GRAPH_HOST}/access_token",
                params={
                    "grant_type": "ig_exchange_token",
                    "client_secret": self.app_secret,
                    "access_token": short_token,
                },
            )
        )
        if short.get("user_id") is not None:
            result["user_id"] = self._object_id(short["user_id"])
        if permissions is not None:
            result["permissions"] = permissions
        return result

    def refresh_token(self, token: str) -> dict[str, Any]:
        """Refresh only tokens already >=24h old and not expired (caller enforces)."""
        return self._token_result(
            self._request(
                "GET",
                f"{GRAPH_HOST}/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": token},
            )
        )

    def get_profile(self, token: str) -> dict[str, Any]:
        profile = self._single(
            self._request(
                "GET",
                f"{self.base_url}/me",
                token=token,
                params={"fields": "id,user_id,username,account_type"},
            )
        )
        user_id = profile.get("user_id") or profile.get("id")
        if user_id is None or not profile.get("username"):
            raise MetaError("Instagram did not return a professional account profile.")
        # Publishing uses the Instagram professional ID, not app-scoped id.
        normalized = dict(profile)
        if profile.get("id") and str(profile["id"]) != str(user_id):
            normalized["app_scoped_id"] = str(profile["id"])
        normalized["id"] = normalized["user_id"] = self._object_id(user_id)
        return normalized

    def create_container(self, account_id: str, token: str, **fields: Any) -> str:
        if "access_token" in fields:
            raise MetaError("Pass Instagram credentials separately from media fields.")
        if isinstance(fields.get("children"), (list, tuple)):
            fields["children"] = ",".join(self._object_id(value) for value in fields["children"])
        payload = self._request(
            "POST",
            f"{self.base_url}/{self._object_id(account_id)}/media",
            token=token,
            json=fields,
        )
        return self._created_id(payload)

    @staticmethod
    def _created_id(payload: dict[str, Any]) -> str:
        value = payload.get("id")
        if value is None or not re.fullmatch(r"[0-9]{1,40}", str(value)):
            raise MetaError("Instagram did not return the created media ID.", ambiguous=True)
        return str(value)

    def container_status(self, container_id: str, token: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"{self.base_url}/{self._object_id(container_id)}",
            token=token,
            params={"fields": "status_code,status"},
        )

    def publish_container(self, account_id: str, token: str, creation_id: str) -> str:
        payload = self._request(
            "POST",
            f"{self.base_url}/{self._object_id(account_id)}/media_publish",
            token=token,
            json={"creation_id": self._object_id(creation_id)},
        )
        return self._created_id(payload)

    def get_media(self, media_id: str, token: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"{self.base_url}/{self._object_id(media_id)}",
            token=token,
            params={"fields": "id,permalink,media_type,timestamp"},
        )

    def publishing_limit(self, account_id: str, token: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"{self.base_url}/{self._object_id(account_id)}/content_publishing_limit",
            token=token,
            params={"fields": "config,quota_usage"},
        )
