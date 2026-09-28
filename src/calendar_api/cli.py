from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from . import service as postgres_service
from . import sqlite_service
from .config import Settings
from .database import apply_migrations, create_database


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calendar-api")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("serve", help="run the HTTP API")
    subcommands.add_parser(
        "migrate", help="initialize or migrate the configured database"
    )
    subcommands.add_parser(
        "materialize", help="extend every published event's occurrence window"
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = Settings.from_env()
    if args.command == "serve":
        uvicorn.run(
            "calendar_api.app:app",
            host=settings.host,
            port=settings.port,
            reload=False,
        )
        return
    if args.command == "migrate":
        if settings.database_url.startswith("sqlite:///"):
            database = create_database(settings.database_url)
            database.open()
            database.close()
            print("initialized SQLite database")
            return
        migrations_dir = Path(
            os.environ.get("MIGRATIONS_DIR", "db/migrations")
        ).resolve()
        if not migrations_dir.is_dir():
            raise SystemExit(f"migration directory not found: {migrations_dir}")
        for filename in apply_migrations(settings.database_url, migrations_dir):
            print(f"applied {filename}")
        return

    database = create_database(settings.database_url, min_size=1, max_size=1)
    service = sqlite_service if database.dialect == "sqlite" else postgres_service
    database.open()
    try:
        with database.transaction() as connection:
            result = service.materialize_all(
                connection,
                past_days=settings.materialization_past_days,
                future_months=settings.materialization_future_months,
            )
        print(
            f"processed {result['events_processed']} event(s); "
            f"inserted {result['occurrences_inserted']} occurrence(s)"
        )
    finally:
        database.close()


if __name__ == "__main__":
    main()
