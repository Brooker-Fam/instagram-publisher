import secrets
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from maple_publisher.app import create_app
from maple_publisher.config import Settings
from maple_publisher.db import make_session_factory

TEST_META_TOKEN = "IG-test-access-token-never-publish-0000000000"
TEST_REFRESHED_TOKEN = "IG-test-refreshed-token-never-publish-000000"


class FakeInstagram:
    """Deterministic account provider; publishing is forbidden in API tests."""

    def __init__(self):
        self.exchanged_codes = []
        self.refreshed_tokens = []
        self.profile = {"user_id": "17841400000000001", "username": "maple_baby_test", "account_type": "BUSINESS"}

    def authorization_url(self, state):
        return "https://www.instagram.com/oauth/authorize?" + urlencode({"state": state, "response_type": "code"})

    def exchange_code(self, code):
        self.exchanged_codes.append(code)
        return {"access_token": TEST_META_TOKEN, "expires_in": 5_184_000, "user_id": "998877665544"}

    def get_profile(self, token):
        assert token == TEST_META_TOKEN
        return self.profile.copy()

    def refresh_token(self, token):
        self.refreshed_tokens.append(token)
        return {"access_token": TEST_REFRESHED_TOKEN, "expires_in": 5_184_000}

    def create_container(self, *args, **kwargs):
        raise AssertionError("The API must not publish; only the separate worker may do so")

    def publish_container(self, *args, **kwargs):
        raise AssertionError("The API must not publish; only the separate worker may do so")


@pytest.fixture
def api_bundle(tmp_path):
    settings = Settings(
        _env_file=None,
        publisher_env="development",
        publisher_mode="demo",
        public_base_url="https://publisher.example.test",
        database_url=f"sqlite:///{tmp_path / 'publisher.db'}",
        media_root=tmp_path / "uploads",
        admin_api_key=secrets.token_urlsafe(40),
        token_encryption_key=Fernet.generate_key().decode(),
        media_signing_key=secrets.token_urlsafe(40),
        meta_app_id="123456789",
        meta_app_secret=secrets.token_urlsafe(32),
    )
    sessions = make_session_factory(settings.database_url)
    provider = FakeInstagram()
    app = create_app(settings, sessions, provider)
    with TestClient(app, base_url=settings.public_base_url) as client:
        yield SimpleNamespace(
            app=app, client=client, settings=settings, sessions=sessions, provider=provider,
            headers={"Authorization": f"Bearer {settings.admin_api_key.get_secret_value()}"},
        )
    sessions.kw["bind"].dispose()
