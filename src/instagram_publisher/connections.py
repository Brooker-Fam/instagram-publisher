import re
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from instagram_publisher.credentials import token_expiry
from instagram_publisher.db import Account, Post, UnifiedGrant, audit
from instagram_publisher.meta import MetaError
from instagram_publisher.security import encrypt_token
from instagram_publisher.serializers import iso
from instagram_publisher.unified import normalize_username

router = APIRouter(tags=["Instagram connection"])


def account_dict(account):
    return {
        key: getattr(account, key)
        for key in (
            "id",
            "instagram_user_id",
            "username",
            "account_type",
            "auth_provider",
            "issuer_app_id",
            "grant_id",
        )
    } | {"token_expires_at": iso(token_expiry(account)), "created_at": iso(account.created_at)}


class UnifiedTokenImport(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    access_token: SecretStr = Field(min_length=1, max_length=16384)
    username: str = Field(pattern=r"^@?[A-Za-z0-9._]{1,30}$")

    @field_validator("access_token")
    @classmethod
    def valid_token(cls, value):
        if not re.fullmatch(r"[\x21-\x7e]+", value.get_secret_value()):
            raise ValueError("The token must contain printable ASCII characters without spaces")
        return value

    @field_validator("username", mode="before")
    @classmethod
    def trim_username(cls, value):
        return value.strip() if isinstance(value, str) else value


def _save_connection(session, settings, token, grant_values, identity, assigned_ids):
    existing_grant = session.scalar(
        select(UnifiedGrant).where(
            UnifiedGrant.issuer_app_id == grant_values["issuer_app_id"],
            UnifiedGrant.system_user_id == grant_values["system_user_id"],
            UnifiedGrant.business_id == grant_values["business_id"],
        )
    )
    if existing_grant:
        linked_ids = set(
            session.scalars(select(Account.instagram_user_id).where(Account.grant_id == existing_grant.id))
        )
        if not linked_ids.issubset(assigned_ids):
            raise HTTPException(
                409,
                "The new token omits a connected account. Restore all linked assignments or use a different grant",
            )
    account = session.scalar(
        select(Account).where(
            Account.auth_provider == "UNIFIED",
            Account.issuer_app_id == grant_values["issuer_app_id"],
            Account.instagram_user_id == identity["instagram_user_id"],
        )
    )
    if account and (existing_grant is None or account.grant_id != existing_grant.id):
        outstanding = session.scalar(
            select(Post.id)
            .where(
                Post.account_id == account.id,
                Post.status.in_(["QUEUED", "PROCESSING", "NEEDS_ATTENTION"]),
            )
            .limit(1)
        )
        if outstanding:
            raise HTTPException(
                409, "Resolve or cancel outstanding posts before changing the account's grant"
            )
    grant = existing_grant or UnifiedGrant(**grant_values)
    for key, value in grant_values.items():
        setattr(grant, key, value)
    grant.token_ciphertext = encrypt_token(token, settings.token_encryption_key.get_secret_value())
    grant.connected_at = time.time()
    grant.revoked_at = None
    session.add(grant)
    session.flush()
    if account is None:
        account = Account(
            auth_provider="UNIFIED",
            issuer_app_id=grant.issuer_app_id,
            instagram_user_id=identity["instagram_user_id"],
        )
    account.username = identity["username"]
    account.unified_grant = grant
    account.connected_at = time.time()
    session.add(account)
    session.flush()
    audit(session, "account.unified_connected", detail=account.id)
    return account_dict(account)


@router.post("/api/accounts/import-unified-token")
def import_unified_token(body: UnifiedTokenImport, request: Request):
    state = request.app.state
    settings = state.settings
    if not settings.meta_unified_app_id or not settings.meta_unified_app_secret.get_secret_value():
        raise HTTPException(409, "Configure META_UNIFIED_APP_ID and META_UNIFIED_APP_SECRET first")
    token = body.access_token.get_secret_value()
    try:
        grant, identity, assigned_ids = state.unified_client.import_identity(
            token, normalize_username(body.username)
        )
        with state.session_factory.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            return _save_connection(session, settings, token, grant, identity, assigned_ids)
    except MetaError as exc:
        raise HTTPException(422, str(exc)) from None
    except IntegrityError:
        raise HTTPException(409, "The connection changed during import. Check its status and retry") from None


@router.post("/api/grants/{grant_id}/revoke")
def revoke_grant(grant_id: str, request: Request):
    with request.app.state.session_factory.begin() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        grant = session.get(UnifiedGrant, grant_id)
        if grant is None:
            raise HTTPException(404, "Grant not found")
        account_ids = list(session.scalars(select(Account.id).where(Account.grant_id == grant.id)))
        if session.scalar(
            select(Post.id)
            .where(
                Post.account_id.in_(account_ids),
                Post.status == "PROCESSING",
            )
            .limit(1)
        ):
            raise HTTPException(
                409, "Stop the worker and reconcile processing posts before revoking this grant"
            )
        grant.token_ciphertext = ""
        grant.revoked_at = time.time()
        session.execute(
            update(Post)
            .where(
                Post.account_id.in_(account_ids),
                Post.status.in_(["DRAFT", "QUEUED", "FAILED"]),
            )
            .values(status="CANCELED", updated_at=time.time(), error="Unified Login grant was disconnected")
        )
        audit(session, "grant.disconnected", detail=grant.id)
        return {
            "id": grant.id,
            "revoked_at": iso(grant.revoked_at),
            "accounts_disconnected": len(account_ids),
        }
