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
