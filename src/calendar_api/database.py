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
            _migrate_occurrence_overrides(connection)
            _migrate_groups_color_and_deletion(connection, schema)
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                  groups_live_slug_unique
                  ON groups(slug) WHERE deleted_at IS NULL
                """
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


def _migrate_review_actions(connection: sqlite3.Connection, schema: str) -> None:
    """Rebuild event_review_actions when its CHECK predates newer actions.

    SQLite cannot alter a CHECK constraint, so a database created by an older
    release would reject the `cancel`, `delete`, and `restore` audit actions
    with a 500.
    The table is a leaf (nothing references it), so rename, recreate from the
    current schema, copy, and drop preserves every existing audit row.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'event_review_actions'"
    ).fetchone()
    if row is None or "'restore'" in row[0]:
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


def _migrate_occurrence_overrides(connection: sqlite3.Connection) -> None:
    """Add per-occurrence scoped-edit columns to older local databases.

    SQLite's CREATE TABLE cannot add fields to an existing table, so a
    database created before scoped single/future edits gains `content_override`
    (a JSON object merged over the published revision), `instance_cancelled`
    (a scoped cancellation that stays visible), and `instance_exception` (a
    durable, restorable single-date removal) here instead of being recreated.
    """
    occurrence_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(event_occurrences)")
    }
    if "content_override" not in occurrence_columns:
        connection.execute("ALTER TABLE event_occurrences ADD COLUMN content_override TEXT")
    if "instance_cancelled" not in occurrence_columns:
        connection.execute(
            "ALTER TABLE event_occurrences ADD COLUMN instance_cancelled INTEGER "
            "NOT NULL DEFAULT 0 CHECK (instance_cancelled IN (0, 1))"
        )
    if "instance_exception" not in occurrence_columns:
        # Durable single-date removals. Earlier single-date deletions are
        # identifiable by their reason; earlier single-date cancellations
        # cannot be told apart from "future" ones, so they stay as they were.
        connection.execute(
            "ALTER TABLE event_occurrences ADD COLUMN instance_exception TEXT "
            "CHECK (instance_exception IN ('cancelled', 'skipped'))"
        )
        connection.execute(
            """
            UPDATE event_occurrences SET instance_exception = 'skipped'
             WHERE status = 'cancelled'
               AND cancellation_reason = 'deleted single occurrence'
            """
        )


def _migrate_groups_color_and_deletion(
    connection: sqlite3.Connection, schema: str
) -> None:
    """Give older local databases group colors and group deletion.

    SQLite cannot add CHECK constraints or drop the legacy UNIQUE on
    `groups.slug` with ALTER TABLE, so a database created before group
    colors and deletion rebuilds the table: rename, recreate from the
    current schema, copy every row (existing groups gain distinct palette
    colors in name order, the same palette the service assigns to new
    groups), and drop the legacy copy. `event_revision_groups` keeps
    referencing `groups` by name, so revision membership survives the
    rebuild. Slug uniqueness for live groups is enforced by the partial
    index created in `open()` after this runs.
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(groups)")}
    if {"color", "deleted_at"} <= columns:
        return
    from .schemas import GROUP_COLOR_PALETTE

    legacy_rows = connection.execute(
        """
        SELECT id, slug, name, description, is_active, created_at, updated_at
          FROM groups ORDER BY name, id
        """
    ).fetchall()
    used = set()
    colors: list[str] = []
    for _ in legacy_rows:
        for candidate in GROUP_COLOR_PALETTE:
            if candidate not in used:
                break
        else:
            candidate = GROUP_COLOR_PALETTE[len(used) % len(GROUP_COLOR_PALETTE)]
        used.add(candidate)
        colors.append(candidate)
    start = schema.index("CREATE TABLE IF NOT EXISTS groups")
    end = schema.index(";", start)
    create_table = schema[start:end].replace(
        "CREATE TABLE IF NOT EXISTS", "CREATE TABLE", 1
    )
    connection.executescript(
        f"""
        PRAGMA foreign_keys = OFF;
        ALTER TABLE groups RENAME TO groups_legacy;
        {create_table};
        PRAGMA foreign_keys = ON;
        """
    )
    connection.executemany(
        """
        INSERT INTO groups (
          id, slug, name, description, color, deleted_at,
          is_active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
        """,
        [
            (
                row[0],
                row[1],
                row[2],
                row[3],
                color,
                row[4],
                row[5],
                row[6],
            )
            for row, color in zip(legacy_rows, colors)
        ],
    )
    connection.execute("DROP TABLE groups_legacy")


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
