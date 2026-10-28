from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ClientSettings(BaseSettings):
    """Remote API callers need no Meta secret or database encryption key."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    public_base_url: str = "http://localhost:8000"
    admin_api_key: SecretStr


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    publisher_env: str = "development"
    publisher_mode: str = "demo"
    public_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./data/publisher.db"
    media_root: Path = Path("data/uploads")
    admin_api_key: SecretStr
    token_encryption_key: SecretStr
    media_signing_key: SecretStr
    meta_app_id: str = ""
    meta_app_secret: SecretStr = SecretStr("")
    meta_unified_app_id: str = ""
    meta_unified_app_secret: SecretStr = SecretStr("")
    meta_api_version: str = "v26.0"
    worker_poll_seconds: float = 10.0
    worker_lease_seconds: int = 900
    container_poll_seconds: float = 60.0
    container_poll_attempts: int = 5
    media_url_ttl_seconds: int = 86_400

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/auth/instagram/callback"

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.publisher_mode not in {"demo", "live"}:
            raise ValueError("PUBLISHER_MODE must be demo or live")
        if self.publisher_env not in {"development", "production"}:
            raise ValueError("PUBLISHER_ENV must be development or production")
        for secret in (self.admin_api_key, self.media_signing_key):
            if len(secret.get_secret_value()) < 32:
                raise ValueError("Admin and signing keys must each contain at least 32 characters")
        try:
            Fernet(self.token_encryption_key.get_secret_value().encode())
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY must be a Fernet key; run scripts/bootstrap-env.py"
            ) from exc
        url = urlparse(self.public_base_url)
        if (
            not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("PUBLIC_BASE_URL must be an origin with no path, credentials, query or fragment")
        if url.scheme not in {"http", "https"}:
            raise ValueError("PUBLIC_BASE_URL must use http or https")
        if self.publisher_mode == "live":
            if url.scheme != "https" or url.hostname in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Live publishing requires a public HTTPS origin")
            legacy = self.meta_app_id and self.meta_app_secret.get_secret_value()
            unified = self.meta_unified_app_id and self.meta_unified_app_secret.get_secret_value()
            if not legacy and not unified:
                raise ValueError(
                    "Live publishing requires a complete legacy or Unified Meta app credential pair"
                )
        if not 1 <= self.worker_poll_seconds <= 300:
            raise ValueError("WORKER_POLL_SECONDS must be between 1 and 300")
        if self.worker_lease_seconds < 600:
            raise ValueError("WORKER_LEASE_SECONDS must be at least 600")
        if not 1 <= self.container_poll_attempts <= 5 or not 0 <= self.container_poll_seconds <= 60:
            raise ValueError("Invalid container polling configuration")
        if self.publisher_mode == "live" and self.container_poll_seconds < 60:
            raise ValueError("Live polling must be no more frequent than once a minute")
        if not 3600 <= self.media_url_ttl_seconds <= 172800:
            raise ValueError("Media URL TTL must be between one and 48 hours")
        return self
