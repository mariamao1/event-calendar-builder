# Event Calendar Builder

The production PostgreSQL data model is defined in:

- [`db/migrations/001_initial_schema.sql`](db/migrations/001_initial_schema.sql) — executable PostgreSQL 15+ schema
- [`db/tests/001_initial_schema_smoke.sql`](db/tests/001_initial_schema_smoke.sql) — transactional constraint smoke test
- [`docs/data-model.md`](docs/data-model.md) — model decisions, invariants, and lifecycle behavior

For a PostgreSQL deployment, apply the migration to an empty database with a
role that can enable the `pgcrypto` extension. Local SQLite setup is below and
does not require these commands.

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/001_initial_schema.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/tests/001_initial_schema_smoke.sql
```

## Backend API

The backend is a Python 3.12/FastAPI service with zero-install SQLite storage
for local use and PostgreSQL for production. It exposes a calendar-oriented
date-range query, public event submission, group reads, and protected moderation
workflows. Recurring events are materialized when a revision is approved, so
calendar reads remain indexed range scans.

```sh
uv sync --extra test
cp .env.example .env
uv run calendar-api migrate
uv run calendar-api serve
```

OpenAPI documentation is available at `http://127.0.0.1:8000/docs`. See
[`docs/backend.md`](docs/backend.md) for endpoint semantics, recurrence
materialization, operations, and test commands.

## Month calendar

The same server exposes the primary calendar at `http://127.0.0.1:8000/`.
It shows only approved occurrences, renders all-day and cross-date events as
week-spanning bands, and keeps crowded days usable through a complete day
dialog. Use the arrows to move one month at a time, **Today** to return to the
current month, or select the month heading to jump directly to a month/year.

When link access is enabled, open the UI with the private token in the URL:

```text
http://127.0.0.1:8000/?token=YOUR_CALENDAR_ACCESS_TOKEN
```

The browser forwards that token only to calendar API reads. Static assets do
not contain calendar data and remain independently cacheable.
