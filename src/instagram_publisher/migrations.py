import argparse
import os
import sqlite3
import time
from pathlib import Path

from sqlalchemy import MetaData
from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.schema import CreateTable

from instagram_publisher.db import Account, UnifiedGrant

LEGACY_COLUMNS = (
    "id",
    "instagram_user_id",
    "app_scoped_id",
    "username",
    "account_type",
    "token_ciphertext",
    "token_expires_at",
    "token_refreshed_at",
    "created_at",
    "connected_at",
)


def _check_foreign_keys(connection):
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("Migration found invalid foreign keys; the original schema has been restored")


def _backup(path):
    destination = path.with_name(f"{path.name}.pre-unified-{time.time_ns()}.bak")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with sqlite3.connect(path) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    return destination


def _rebuild_accounts(connection, legacy_app_id):
    metadata = MetaData()
    grants = UnifiedGrant.__table__.to_metadata(metadata)
    accounts = Account.__table__.to_metadata(metadata, name="accounts_unified")
    connection.execute(str(CreateTable(grants).compile(dialect=dialect())))
    connection.execute(str(CreateTable(accounts).compile(dialect=dialect())))
    columns = ", ".join(LEGACY_COLUMNS)
    connection.execute(
        f"INSERT INTO accounts_unified ({columns}, auth_provider, issuer_app_id) "
        f"SELECT {columns}, 'INSTAGRAM', ? FROM accounts",
        (legacy_app_id,),
    )
    connection.execute("DROP TABLE accounts")
    connection.execute("ALTER TABLE accounts_unified RENAME TO accounts")
    _check_foreign_keys(connection)


def migrate_database(database_url, *, legacy_app_id=""):
    if not database_url.startswith("sqlite:///") or database_url == "sqlite:///:memory:":
        raise ValueError("Migration requires a file-backed SQLite database URL")
    path = Path(database_url.removeprefix("sqlite:///"))
    if not path.is_file():
        raise ValueError("The database does not exist")
    if not isinstance(legacy_app_id, str) or (legacy_app_id and not legacy_app_id.isdigit()):
        raise ValueError("Provide the original Instagram Login app ID with --legacy-app-id")
    connection = sqlite3.connect(path, timeout=1)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(accounts)")}
        if "auth_provider" in columns:
            connection.rollback()
            return None
        if columns != set(LEGACY_COLUMNS):
            raise RuntimeError("Unrecognized accounts schema; no changes were made")
        if not legacy_app_id and connection.execute("SELECT count(*) FROM accounts").fetchone()[0]:
            raise ValueError("Provide the original Instagram Login app ID with --legacy-app-id")
        backup = _backup(path)
        _rebuild_accounts(connection, legacy_app_id)
        connection.commit()
        return backup
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(
        description="Migrate account storage while the API and worker are stopped"
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--legacy-app-id", default="", help="Original Instagram Login app ID; required for saved accounts"
    )
    args = parser.parse_args()
    try:
        backup = migrate_database(args.database_url, legacy_app_id=args.legacy_app_id)
    except (ValueError, RuntimeError, sqlite3.Error):
        parser.exit(
            1,
            "Migration failed; no schema changes committed. Check the database and stop API/worker processes.\n",
        )
    print(
        f"Migration complete. Private backup: {backup}"
        if backup
        else "Schema already supports Unified Login."
    )


if __name__ == "__main__":
    main()
