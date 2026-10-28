import time
import uuid
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class UnifiedGrant(Base):
    __tablename__ = "unified_grants"
    __table_args__ = (UniqueConstraint("issuer_app_id", "system_user_id", "business_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    issuer_app_id: Mapped[str] = mapped_column(String(80))
    system_user_id: Mapped[str] = mapped_column(String(80))
    business_id: Mapped[str] = mapped_column(String(80))
    token_ciphertext: Mapped[str] = mapped_column(Text)
    token_expires_at: Mapped[float | None] = mapped_column(Float)
    data_access_expires_at: Mapped[float | None] = mapped_column(Float)
    revoked_at: Mapped[float | None] = mapped_column(Float)
    connected_at: Mapped[float] = mapped_column(Float, default=time.time)


def legacy_refresh_time(context):
    return None if context.get_current_parameters().get("auth_provider") == "UNIFIED" else time.time()


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("auth_provider", "issuer_app_id", "instagram_user_id"),
        UniqueConstraint("auth_provider", "issuer_app_id", "app_scoped_id"),
        CheckConstraint(
            "(auth_provider = 'INSTAGRAM' AND grant_id IS NULL AND account_type IS NOT NULL "
            "AND token_ciphertext IS NOT NULL AND token_expires_at IS NOT NULL "
            "AND token_refreshed_at IS NOT NULL) OR "
            "(auth_provider = 'UNIFIED' AND grant_id IS NOT NULL AND app_scoped_id IS NULL "
            "AND account_type IS NULL AND token_ciphertext IS NULL AND token_expires_at IS NULL "
            "AND token_refreshed_at IS NULL)",
            name="account_credentials_match_provider",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    auth_provider: Mapped[str] = mapped_column(String(20), default="INSTAGRAM")
    issuer_app_id: Mapped[str] = mapped_column(String(80), default="")
    grant_id: Mapped[str | None] = mapped_column(ForeignKey("unified_grants.id"))
    unified_grant: Mapped[UnifiedGrant | None] = relationship(lazy="joined")
    instagram_user_id: Mapped[str] = mapped_column(String(80))
    app_scoped_id: Mapped[str | None] = mapped_column(String(80))
    username: Mapped[str] = mapped_column(String(200))
    account_type: Mapped[str | None] = mapped_column(String(40))
    token_ciphertext: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[float | None] = mapped_column(Float)
    token_refreshed_at: Mapped[float | None] = mapped_column(Float, default=legacy_refresh_time)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    connected_at: Mapped[float] = mapped_column(Float, default=time.time)


class OAuthState(Base):
    __tablename__ = "oauth_states"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[float] = mapped_column(Float)
    consumed_at: Mapped[float | None] = mapped_column(Float)


class CallbackReceipt(Base):
    __tablename__ = "callback_receipts"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    confirmation_code: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="completed")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    storage_key: Mapped[str] = mapped_column(String(100), unique=True)
    mime_type: Mapped[str] = mapped_column(String(60))
    size_bytes: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Float)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Post(Base):
    __tablename__ = "posts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    kind: Mapped[str] = mapped_column(String(30))
    asset_ids: Mapped[list[str]] = mapped_column(JSON)
    cover_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"))
    caption: Mapped[str] = mapped_column(Text, default="")
    alt_text: Mapped[str] = mapped_column(Text, default="")
    share_to_feed: Mapped[bool] = mapped_column(Boolean, default=True)
    scheduled_at: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    approved_revision: Mapped[int | None] = mapped_column(Integer)
    approved_mode: Mapped[str | None] = mapped_column(String(10))
    approved_at: Mapped[float | None] = mapped_column(Float)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    container_id: Mapped[str | None] = mapped_column(String(80))
    child_container_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    publish_requested_at: Mapped[float | None] = mapped_column(Float)
    published_media_id: Mapped[str | None] = mapped_column(String(100))
    permalink: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[str | None] = mapped_column(ForeignKey("posts.id"))
    action: Mapped[str] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeat"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    updated_at: Mapped[float] = mapped_column(Float)
    mode: Mapped[str] = mapped_column(String(10))


def make_session_factory(database_url: str):
    if not database_url.startswith("sqlite:"):
        raise ValueError(
            "This release supports SQLite on one host; use the supplied persistent-volume deployment"
        )
    if database_url.startswith("sqlite:///") and database_url != "sqlite:///:memory:":
        Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    inspector = inspect(engine)
    if inspector.has_table("accounts") and "auth_provider" not in {
        column["name"] for column in inspector.get_columns("accounts")
    }:
        engine.dispose()
        raise RuntimeError("Stop the API and worker, then run python -m instagram_publisher.migrations")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def audit(session, action: str, post_id: str | None = None, detail: str = ""):
    session.add(AuditEvent(action=action, post_id=post_id, detail=detail))
