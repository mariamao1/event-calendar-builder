from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .schemas import GROUP_COLOR_PALETTE


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

    @contextmanager
    def savepoint(self, connection: Connection) -> Iterator[None]:
        """Roll back only this block on error, inside an open transaction."""
        with connection.transaction():
            yield


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
            if "featured_at" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN featured_at TEXT")
            revision_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(event_revisions)")
            }
            if "virtual_url" not in revision_columns:
                connection.execute(
                    "ALTER TABLE event_revisions ADD COLUMN virtual_url TEXT"
                )
            if "attachments" not in revision_columns:
                connection.execute(
                    "ALTER TABLE event_revisions ADD COLUMN attachments TEXT"
                )
            if "rsvp_enabled" not in revision_columns:
                connection.execute(
                    "ALTER TABLE event_revisions ADD COLUMN rsvp_enabled INTEGER "
                    "NOT NULL DEFAULT 0 CHECK (rsvp_enabled IN (0, 1))"
                )
            _migrate_event_rsvps(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS event_rsvps (
                  event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                  user_id TEXT NOT NULL,
                  username TEXT NOT NULL CHECK (trim(username) <> ''),
                  status TEXT NOT NULL CHECK (status IN ('attending', 'declined')),
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (event_id, user_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS local_event_rsvps_user_idx
                  ON event_rsvps(user_id)
                """
            )
            _migrate_review_actions(connection, schema)
            _migrate_occurrence_overrides(connection)
            _migrate_groups(connection, schema)
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS groups_live_slug_unique
                  ON groups(slug) WHERE deleted_at IS NULL
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS local_events_featured_idx
                  ON events(featured_at) WHERE featured_at IS NOT NULL
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

    @contextmanager
    def savepoint(self, connection: sqlite3.Connection) -> Iterator[None]:
        """Roll back only this block on error, inside an open transaction."""
        connection.execute("SAVEPOINT bulk_item")
        try:
            yield
        except BaseException:
            connection.execute("ROLLBACK TO bulk_item")
            connection.execute("RELEASE bulk_item")
            raise
        connection.execute("RELEASE bulk_item")


def _migrate_review_actions(connection: sqlite3.Connection, schema: str) -> None:
    """Rebuild event_review_actions when its CHECK predates newer actions.

    SQLite cannot alter a CHECK constraint, so a database created by an older
    release would reject the `cancel`, `delete`, `restore`, and `tag` audit
    actions with a 500.
    The table is a leaf (nothing references it), so rename, recreate from the
    current schema, copy, and drop preserves every existing audit row.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'event_review_actions'"
    ).fetchone()
    if row is None or "'tag'" in row[0]:
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


def _migrate_event_rsvps(connection: sqlite3.Connection) -> None:
    """Replace a foreign `event_rsvps` table with the canonical RSVP shape.

    A database that once ran an incompatible RSVP implementation may carry
    an `event_rsvps` table with different columns (per-occurrence answers
    instead of per-event attending/declined rows). `CREATE TABLE IF NOT
    EXISTS` would keep that table and every RSVP read would fail with
    `no such column`. Move it aside to `event_rsvps_legacy` — its rows are
    preserved but no longer consulted — so the canonical table below is
    created fresh. Its answers cannot be carried over: they name occurrences
    and use a different vocabulary than this implementation.
    """
    existing = {row[1] for row in connection.execute("PRAGMA table_info(event_rsvps)")}
    if not existing or {"event_id", "user_id", "username", "status"} <= existing:
        return
    legacy_exists = (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'event_rsvps_legacy'"
        ).fetchone()
        is not None
    )
    if legacy_exists:
        connection.execute("DROP TABLE event_rsvps")
    else:
        connection.execute("ALTER TABLE event_rsvps RENAME TO event_rsvps_legacy")


def _migrate_groups(connection: sqlite3.Connection, schema: str) -> None:
    """Rebuild groups for colors and deletion on older local databases.

    Older releases declared `slug UNIQUE` inline, which SQLite cannot drop, so
    a deleted group's slug could never be reused. Following SQLite's
    documented table-rebuild procedure (create, copy, drop, rename — never
    rename the referenced table away, which would repoint its foreign keys)
    adds `color` and `deleted_at` while every revision keeps its groups.
    Existing groups get distinct palette colors in name order.
    """
    group_columns = {row[1] for row in connection.execute("PRAGMA table_info(groups)")}
    if "deleted_at" in group_columns:
        return
    start = schema.index("CREATE TABLE IF NOT EXISTS groups")
    end = schema.index(";", start)
    create_table = schema[start:end].replace(
        "CREATE TABLE IF NOT EXISTS groups", "CREATE TABLE groups_rebuilt", 1
    )
    connection.executescript(
        f"""
        PRAGMA foreign_keys = OFF;
        BEGIN;
        {create_table};
        INSERT INTO groups_rebuilt (
          id, slug, name, description, is_active, created_at, updated_at
        ) SELECT id, slug, name, description, is_active, created_at, updated_at
            FROM groups;
        DROP TABLE groups;
        ALTER TABLE groups_rebuilt RENAME TO groups;
        COMMIT;
        PRAGMA foreign_keys = ON;
        """
    )
    group_ids = [
        row[0]
        for row in connection.execute("SELECT id FROM groups ORDER BY name, id")
    ]
    connection.executemany(
        "UPDATE groups SET color = ? WHERE id = ?",
        [
            (GROUP_COLOR_PALETTE[index % len(GROUP_COLOR_PALETTE)], group_id)
            for index, group_id in enumerate(group_ids)
        ],
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
