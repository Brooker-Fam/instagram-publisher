from instagram_publisher.meta import MetaClient
from instagram_publisher.security import decrypt_token
from instagram_publisher.unified import UnifiedMetaClient


def token_expiry(account):
    if account.auth_provider == "INSTAGRAM":
        return account.token_expires_at
    grant = account.unified_grant
    if grant is None:
        return None
    expiries = [
        value for value in (grant.token_expires_at, grant.data_access_expires_at) if value is not None
    ]
    return min(expiries) if expiries else None


def credentials_valid(account, through):
    if account is None:
        return False
    if account.auth_provider == "UNIFIED":
        grant = account.unified_grant
        if not grant or grant.revoked_at is not None or grant.issuer_app_id != account.issuer_app_id:
            return False
        ciphertext = grant.token_ciphertext
    elif account.auth_provider == "INSTAGRAM":
        ciphertext = account.token_ciphertext
        if account.token_expires_at is None:
            return False
    else:
        return False
    expiry = token_expiry(account)
    return bool(ciphertext) and (expiry is None or expiry > through)


def account_token(account, settings):
    holder = account.unified_grant if account.auth_provider == "UNIFIED" else account
    return decrypt_token(holder.token_ciphertext, settings.token_encryption_key.get_secret_value())


def client_for_account(settings, account):
    unified = account.auth_provider == "UNIFIED"
    client_type = UnifiedMetaClient if unified else MetaClient
    return client_type(
        app_id=settings.meta_unified_app_id if unified else settings.meta_app_id,
        app_secret=(
            settings.meta_unified_app_secret if unified else settings.meta_app_secret
        ).get_secret_value(),
        redirect_uri=settings.redirect_uri,
        api_version=settings.meta_api_version,
    )
