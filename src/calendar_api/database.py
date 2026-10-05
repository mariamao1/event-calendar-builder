from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    """Small connection-pool wrapper with explicit transaction boundaries."""

    def __init__(self, database_url: str, *, min_size: int = 1, max_size: int = 10):
        self.dialect = "postgresql"
        self.pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"row_factory": dict_row},
        )

    def open(self) -> None:
        self.pool.open(wait=True)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        with self.pool.connection() as connection:
            yield connection

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.pool.connection() as connection, connection.transaction():
            yield connection


class SQLiteDatabase:
    """Zero-install development database with the same service contract."""

    dialect = "sqlite"

    def __init__(self, database_url: str, **_: object):
        raw_path = database_url.removeprefix("sqlite:///")
        self.path = Path(raw_path).expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        schema = files("calendar_api").joinpath("sqlite_schema.sql").read_text()
        with self._connect() as connection:
            connection.executescript(schema)
            # sqlite_schema.sql is intentionally idempotent, but CREATE TABLE
            # cannot add fields to a database created by an older release.
            # Keep this tiny local migration here so existing calendars gain
            # creator-managed event edits without being recreated.
            event_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(events)")
            }
            if "management_token_hash" not in event_columns:
                connection.execute(
                    "ALTER TABLE events ADD COLUMN management_token_hash BLOB"
                )
            if "cancelled_at" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN cancelled_at TEXT")
            if "cancelled_by" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN cancelled_by TEXT")
            if "cancel_reason" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN cancel_reason TEXT")
            _migrate_review_actions(connection, schema)
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                  events_management_token_hash_unique
                  ON events(management_token_hash)
                 WHERE management_token_hash IS NOT NULL
                """
            )

    def close(self) -> None:
        pass

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _migrate_review_actions(connection: sqlite3.Connection, schema: str) -> None:
    """Rebuild event_review_actions when its CHECK predates cancel/delete.

    SQLite cannot alter a CHECK constraint, so a database created by an older
    release would reject the `cancel` and `delete` audit actions with a 500.
    The table is a leaf (nothing references it), so rename, recreate from the
    current schema, copy, and drop preserves every existing audit row.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'event_review_actions'"
    ).fetchone()
    if row is None or "'cancel'" in row[0]:
        return
    start = schema.index("CREATE TABLE IF NOT EXISTS event_review_actions")
    end = schema.index(";", start)
    create_table = schema[start:end].replace(
        "CREATE TABLE IF NOT EXISTS", "CREATE TABLE", 1
    )
    connection.executescript(
        f"""
        PRAGMA foreign_keys = OFF;
        ALTER TABLE event_review_actions RENAME TO event_review_actions_legacy;
        {create_table};
        INSERT INTO event_review_actions (
          id, event_id, event_revision_id, action, actor, note, occurred_at
        ) SELECT id, event_id, event_revision_id, action, actor, note, occurred_at
            FROM event_review_actions_legacy;
        DROP TABLE event_review_actions_legacy;
        PRAGMA foreign_keys = ON;
        """
    )


def create_database(database_url: str, **kwargs: object) -> Database | SQLiteDatabase:
    if database_url.startswith("sqlite:///"):
        return SQLiteDatabase(database_url, **kwargs)
    return Database(database_url, **kwargs)


def apply_migrations(database_url: str, migrations_dir: Path) -> list[str]:
    """Apply each SQL migration exactly once and return the applied filenames."""
    applied: list[str] = []
    with Connection.connect(database_url, autocommit=True) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              filename text PRIMARY KEY,
              applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        rows = connection.execute("SELECT filename FROM schema_migrations").fetchall()
        completed = {row[0] for row in rows}

        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in completed:
                continue
            connection.execute(path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
            )
            applied.append(path.name)
    return applied
