# Event Calendar Builder

The production PostgreSQL data model is defined in:

- [`db/migrations/`](db/migrations/) — ordered executable PostgreSQL 15+ migrations
- [`db/tests/001_initial_schema_smoke.sql`](db/tests/001_initial_schema_smoke.sql) — transactional constraint smoke test
- [`docs/data-model.md`](docs/data-model.md) — model decisions, invariants, and lifecycle behavior

For a PostgreSQL deployment, apply the migration to an empty database with a
role that can enable the `pgcrypto` extension. Local SQLite setup is below and
does not require these commands.

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/001_initial_schema.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/002_event_management_tokens.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/003_optional_event_contact.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/004_event_cancellation.sql
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

Use **Add an event** to submit every event field, including timing, recurrence,
location, groups/tags, and submitter contact. Under **Repeats**, an event can
recur every N days; every N weeks on chosen weekdays; monthly on its date, on
the Nth or last weekday (for example "the second Tuesday"), or on the last day
of the month; or yearly on its date or on the Nth/last weekday of its month
(for example "the fourth Thursday of November"). A series never ends, ends on
a chosen date, or ends after a number of times, and the form previews the
schedule in plain language as you edit. Extra or skipped dates can be added on
top. Repeating events appear on every view with a ↻ marker. Community submissions wait for
admin approval; the confirmation includes a creator-only edit link. Signed-in
admins create and edit events with immediate approval.

Use the **Month / Week / Day** switcher to change views. Week and day are
time grids: timed events are positioned by their start/end times, overlapping
events sit side by side, and all-day events stay in their own all-day region
above the grid. The grid always covers 7:00–19:00 and adaptively expands (with
an hour of padding, clamped to the full day) to include earlier or later
events. A marker shows the current time when today is visible. The view and
date live in the URL (`?view=week&date=2026-10-07`), so refreshing or sharing
the link lands back in the same place.

Select any event to open its detail view: the full schedule, location,
description, website, and groups/tags. Repeating events also show the series in
plain language (for example "Every week on Tuesday · 12 times"), added and
skipped dates, previous/next date navigation, and the upcoming dates. Viewers
can copy a link to the event, and its creator can edit it. Signed-in admins
also see moderation details (submitter, approval, any pending edit awaiting
review) and can edit, review a pending edit, or unpublish. The event's creator
or an admin can cancel the event (it stays visible, marked cancelled) or
delete it (it no longer exists). The open event lives in the URL
(`?event=…&occurrence=…`), so the link opens straight to that event and date.

When link access is enabled, open the UI with the private token in the URL:

```text
http://127.0.0.1:8000/?token=YOUR_CALENDAR_ACCESS_TOKEN
```

The browser forwards that token only to calendar API reads. Static assets do
not contain calendar data and remain independently cacheable.
