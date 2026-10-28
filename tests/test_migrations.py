import sqlite3

import pytest
from sqlalchemy import text

from instagram_publisher.db import Account, make_session_factory


def legacy_database(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE accounts (
            id VARCHAR(36) PRIMARY KEY NOT NULL, instagram_user_id VARCHAR(80) UNIQUE NOT NULL,
            app_scoped_id VARCHAR(80) UNIQUE, username VARCHAR(200) NOT NULL, account_type VARCHAR(40) NOT NULL,
            token_ciphertext TEXT NOT NULL, token_expires_at FLOAT NOT NULL, token_refreshed_at FLOAT NOT NULL,
            created_at FLOAT NOT NULL, connected_at FLOAT NOT NULL
        );
        CREATE TABLE retained_posts (id TEXT PRIMARY KEY, account_id VARCHAR(36) REFERENCES accounts(id));
        INSERT INTO accounts VALUES ('saved-id', '901', '902', 'synthetic', 'BUSINESS', 'synthetic-cipher', 12345, 1000, 900, 901);
        INSERT INTO retained_posts VALUES ('saved-post', 'saved-id');
    """)
    connection.close()
    return f"sqlite:///{path}"


def test_startup_requires_explicit_migration_of_existing_account_schema(tmp_path):
    database = legacy_database(tmp_path / "old.db")
    with pytest.raises(RuntimeError, match="migrations"):
        make_session_factory(database)


def test_offline_migration_preserves_ids_tokens_and_foreign_keys_and_is_idempotent(tmp_path):
    from instagram_publisher.migrations import migrate_database

    database = legacy_database(tmp_path / "old.db")
    result = migrate_database(database, legacy_app_id="123456789")
    assert result is not None
    assert result.stat().st_mode & 0o777 == 0o600
    sessions = make_session_factory(database)
    with sessions() as session:
        account = session.get(Account, "saved-id")
        assert account.instagram_user_id == "901"
        assert account.app_scoped_id == "902"
        assert account.token_ciphertext == "synthetic-cipher"
        assert account.token_expires_at == 12345
        assert account.token_refreshed_at == 1000
        assert account.created_at == 900
        assert account.connected_at == 901
        assert account.auth_provider == "INSTAGRAM"
        assert account.issuer_app_id == "123456789"
        assert session.execute(text("SELECT account_id FROM retained_posts")).scalar_one() == account.id
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
    sessions.kw["bind"].dispose()
    assert migrate_database(database, legacy_app_id="123456789") is None


def test_migration_failure_rolls_back_original_schema_and_data(tmp_path, monkeypatch):
    from instagram_publisher import migrations

    database = legacy_database(tmp_path / "old.db")

    def fail(*args):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(migrations, "_check_foreign_keys", fail)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        migrations.migrate_database(database, legacy_app_id="123456789")
    with sqlite3.connect(tmp_path / "old.db") as connection:
        assert "auth_provider" not in {row[1] for row in connection.execute("PRAGMA table_info(accounts)")}
        assert connection.execute("SELECT token_ciphertext FROM accounts").fetchone()[0] == "synthetic-cipher"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_empty_legacy_schema_can_migrate_without_inventing_app_identity(tmp_path):
    from instagram_publisher.migrations import migrate_database

    database = legacy_database(tmp_path / "old.db")
    with sqlite3.connect(tmp_path / "old.db") as connection:
        connection.execute("DELETE FROM retained_posts")
        connection.execute("DELETE FROM accounts")
    assert migrate_database(database) is not None
    sessions = make_session_factory(database)
    with sessions() as session:
        assert session.execute(text("SELECT count(*) FROM accounts")).scalar_one() == 0
    sessions.kw["bind"].dispose()
