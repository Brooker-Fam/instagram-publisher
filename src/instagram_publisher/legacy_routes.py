import html
import secrets
import time
from typing import Annotated

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select, update

from instagram_publisher.api_dependencies import require_admin
from instagram_publisher.connections import account_dict
from instagram_publisher.credentials import credentials_valid
from instagram_publisher.db import (
    Account,
    OAuthState,
    audit,
)
from instagram_publisher.meta import MetaError
from instagram_publisher.security import (
    decrypt_token,
    encrypt_token,
    state_digest,
)

router = APIRouter()


@router.post("/api/accounts/connect", dependencies=[Depends(require_admin)], tags=["Instagram connection"])
def connect(request: Request):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    client = request.app.state.meta_client
    if not settings.meta_app_id or not settings.meta_app_secret.get_secret_value():
        raise HTTPException(409, "Configure META_APP_ID and META_APP_SECRET first; see docs/meta-setup.md")
    state = secrets.token_urlsafe(32)
    with session_factory.begin() as session:
        session.execute(delete(OAuthState).where(OAuthState.expires_at < time.time() - 86400))
        session.add(OAuthState(digest=state_digest(state), expires_at=time.time() + 600))
    return {"authorization_url": client.authorization_url(state), "expires_in": 600}


@router.get("/auth/instagram/callback", response_class=HTMLResponse, include_in_schema=False)
def callback(
    request: Request,
    state: Annotated[str, Query(max_length=512)],
    code: Annotated[str | None, Query(max_length=4096)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    client = request.app.state.meta_client
    with session_factory.begin() as session:
        result = session.execute(
            update(OAuthState)
            .where(
                OAuthState.digest == state_digest(state),
                OAuthState.expires_at > time.time(),
                OAuthState.consumed_at.is_(None),
            )
            .values(consumed_at=time.time())
        )
        if result.rowcount != 1:
            raise HTTPException(
                400, "This connection link is invalid, expired, or already used. Start a new connection."
            )
    if error or not code:
        raise HTTPException(
            400, "Instagram authorization was not completed. Start a new connection to try again."
        )
    try:
        tokens = client.exchange_code(code)
        token = tokens["access_token"]
        profile = client.get_profile(token)
        instagram_id = str(profile.get("user_id") or profile["id"])
        username = str(profile["username"])
        app_scoped_id = profile.get("app_scoped_id") or tokens.get("user_id")
        if not app_scoped_id:
            raise HTTPException(
                502, "Instagram did not return the app-scoped account ID. Reconnect the account."
            )
        account_type = str(profile.get("account_type", "")).upper()
        if account_type not in {"BUSINESS", "CREATOR", "MEDIA_CREATOR"}:
            raise HTTPException(422, "Connect an Instagram Business or Creator account")
        with session_factory.begin() as session:
            account = session.scalar(
                select(Account).where(
                    Account.auth_provider == "INSTAGRAM",
                    Account.issuer_app_id == settings.meta_app_id,
                    Account.instagram_user_id == instagram_id,
                )
            )
            if account is None:
                account = Account(instagram_user_id=instagram_id, issuer_app_id=settings.meta_app_id)
                session.add(account)
            account.username = username
            account.app_scoped_id = str(app_scoped_id)
            account.account_type = account_type
            account.token_ciphertext = encrypt_token(token, settings.token_encryption_key.get_secret_value())
            account.token_expires_at = time.time() + int(tokens["expires_in"])
            account.token_refreshed_at = time.time()
            account.connected_at = time.time()
            audit(session, "account.connected")
    except MetaError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            502, "Instagram returned an incomplete account response. Start a new connection."
        ) from exc
    return HTMLResponse(
        "<!doctype html><html><head><title>Instagram connected</title></head><body>"
        f"<h1>Connected @{html.escape(username)}</h1>"
        "<p>You can close this window and return to Instagram Publisher. Connecting does not publish any posts.</p>"
        "</body></html>"
    )


@router.get("/api/accounts", dependencies=[Depends(require_admin)], tags=["Instagram connection"])
def accounts(request: Request):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        return [account_dict(a) for a in session.scalars(select(Account).order_by(Account.created_at))]


@router.post(
    "/api/accounts/{account_id}/refresh", dependencies=[Depends(require_admin)], tags=["Instagram connection"]
)
def refresh(request: Request, account_id: str):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    client = request.app.state.meta_client
    with session_factory() as session:
        account = session.get(Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        if account.auth_provider == "UNIFIED":
            raise HTTPException(409, "Unified Login tokens are not Instagram-refreshable; import a new token")
        if not credentials_valid(account, time.time()):
            raise HTTPException(409, "The token expired; reconnect Instagram")
        if time.time() - account.token_refreshed_at < 86400:
            raise HTTPException(409, "Instagram tokens must be at least 24 hours old before refresh")
        try:
            token = decrypt_token(account.token_ciphertext, settings.token_encryption_key.get_secret_value())
            result = client.refresh_token(token)
            refreshed_token = result["access_token"]
            lifetime = result["expires_in"]
            if (
                not isinstance(refreshed_token, str)
                or not refreshed_token
                or not isinstance(lifetime, int)
                or lifetime <= 0
            ):
                raise ValueError("Invalid token response")
            encrypted = encrypt_token(refreshed_token, settings.token_encryption_key.get_secret_value())
        except MetaError as exc:
            raise HTTPException(502, str(exc)) from exc
        except InvalidToken as exc:
            raise HTTPException(
                409, "Stored credentials cannot be decrypted; restore the key or reconnect"
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(502, "Instagram returned an incomplete access-token response") from exc
        old_ciphertext = account.token_ciphertext
        result = session.execute(
            update(Account)
            .where(
                Account.id == account_id,
                Account.token_ciphertext == old_ciphertext,
            )
            .values(
                token_ciphertext=encrypted,
                token_expires_at=time.time() + lifetime,
                token_refreshed_at=time.time(),
            )
        )
        if not result.rowcount:
            raise HTTPException(
                409, "The account connection changed during refresh; reload its current status"
            )
        audit(session, "account.refreshed")
        session.commit()
        session.expire_all()
        account = session.get(Account, account_id)
        return account_dict(account)
