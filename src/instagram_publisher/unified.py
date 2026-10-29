import re
import time

from instagram_publisher.meta import MetaClient, MetaError

UNIFIED_SCOPES = {"instagram_basic", "instagram_content_publish"}


def normalize_username(value: str) -> str:
    return value.strip().removeprefix("@").lower()


class UnifiedMetaClient(MetaClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.base_url = f"https://graph.facebook.com/{self.api_version}"

    @staticmethod
    def _expiry(data, name):
        value = data.get(name)
        if type(value) is not int or value < 0:
            raise MetaError("Meta did not return a valid token expiry.")
        if value and value <= time.time():
            raise MetaError("Meta authorization expired. Generate and import a new Unified Login token.")
        return value or None

    def inspect_grant(self, token):
        if not self.app_id or not self.app_secret:
            raise MetaError("Configure the Unified Login app ID and app secret first.")
        result = self._request(
            "GET",
            f"{self.base_url}/debug_token",
            token=f"{self.app_id}|{self.app_secret}",
            params={"input_token": token},
        ).get("data")
        if not isinstance(result, dict) or result.get("is_valid") is not True:
            raise MetaError("Meta rejected this token. Generate a new Unified Login token.")
        if str(result.get("app_id")) != self.app_id:
            raise MetaError("This token belongs to a different Meta app.")
        if result.get("type") is not None and result["type"] != "SYSTEM_USER":
            raise MetaError("Import a Unified Login system-user token.")
        scopes = result.get("scopes")
        if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
            raise MetaError("Meta did not return the token permissions.")
        if not UNIFIED_SCOPES.issubset(scopes):
            raise MetaError("Grant Instagram basic and content publishing permissions before importing.")
        grant = {
            "issuer_app_id": self.app_id,
            "system_user_id": self._object_id(result.get("user_id")),
            "token_expires_at": self._expiry(result, "expires_at"),
            "data_access_expires_at": self._expiry(result, "data_access_expires_at"),
        }
        identity = self._request(
            "GET", f"{self.base_url}/me", token=token, params={"fields": "id,client_business_id"}
        )
        if "id" in identity and self._object_id(identity["id"]) != grant["system_user_id"]:
            raise MetaError("Meta returned inconsistent system-user identities.")
        grant["business_id"] = self._object_id(identity.get("client_business_id"))
        return grant

    def assigned_accounts(self, token):
        accounts = {}
        cursors = set()
        params = {"fields": "id,username", "limit": 100}
        for _ in range(100):
            payload = self._request(
                "GET", f"{self.base_url}/me/assigned_instagram_accounts", token=token, params=params
            )
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise MetaError("Meta did not return assigned Instagram accounts.")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("username"), str):
                    raise MetaError("Meta returned an incomplete assigned account.")
                username = normalize_username(row["username"])
                if not re.fullmatch(r"[a-z0-9._]{1,30}", username):
                    raise MetaError("Meta returned an invalid account username.")
                account_id = self._object_id(row.get("id"))
                accounts[account_id] = {"instagram_user_id": account_id, "username": username}
            paging = payload.get("paging", {})
            if not isinstance(paging, dict):
                raise MetaError("Meta returned invalid account pagination.")
            if not paging.get("next"):
                return list(accounts.values())
            pagination_cursors = paging.get("cursors")
            if not isinstance(pagination_cursors, dict):
                raise MetaError("Meta returned invalid account pagination.")
            cursor = pagination_cursors.get("after")
            if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in cursors:
                raise MetaError("Meta returned invalid account pagination.")
            cursors.add(cursor)
            params["after"] = cursor
        raise MetaError("Too many assigned accounts. Import requires a bounded account list.")

    def import_identity(self, token, username):
        grant = self.inspect_grant(token)
        assigned = self.assigned_accounts(token)
        matches = [account for account in assigned if account["username"] == normalize_username(username)]
        if len(matches) != 1:
            raise MetaError("The requested username must match exactly one assigned Instagram account.")
        return grant, matches[0], {account["instagram_user_id"] for account in assigned}
