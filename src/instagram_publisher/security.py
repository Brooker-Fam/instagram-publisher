import hashlib
import hmac
import time

from cryptography.fernet import Fernet


def state_digest(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def encrypt_token(token: str, key: str) -> str:
    return Fernet(key.encode()).encrypt(token.encode()).decode()


def decrypt_token(ciphertext: str, key: str) -> str:
    return Fernet(key.encode()).decrypt(ciphertext.encode()).decode()


def valid_admin_key(provided: str, expected: str) -> bool:
    return hmac.compare_digest(provided.encode(), expected.encode())


def media_url(asset_id: str, settings, *, expires: int | None = None) -> str:
    from instagram_publisher.media import sign_media_path

    expires = expires or int(time.time()) + settings.media_url_ttl_seconds
    signature = sign_media_path(asset_id, expires, settings.media_signing_key.get_secret_value())
    return f"{settings.public_base_url.rstrip('/')}/media/{asset_id}?expires={expires}&signature={signature}"
