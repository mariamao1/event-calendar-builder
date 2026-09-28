# Backend and API

## Stack decision

The service uses Python 3.12 and FastAPI with two persistence modes:

- **SQLite for local development and tests.** It is built into Python, needs no
  Docker or database installation, and stores data in
  `.calendar-data/calendar.db` by default. Its Task 2 schema is packaged in
  `src/calendar_api/sqlite_schema.sql`.
- **PostgreSQL 15+ for production.** The canonical production schema remains
  `db/migrations/001_initial_schema.sql`; it adds database-enforced revision
  immutability, deferred ownership keys, and the notification ledger required
  by later tasks.

Both implementations expose the same service and HTTP behavior. SQLite uses
`BEGIN IMMEDIATE` to serialize moderation writes; PostgreSQL uses row locks.

FastAPI supplies an OpenAPI contract at `/docs`; the service layer owns
transactions and recurrence reconciliation, while the database enforces final
revision immutability and referential integrity. Admin routes require the
constant-time-checked `X-Admin-Key` header. If `ADMIN_API_KEY` is unset they
fail closed with `503`.

## Calendar-first read model

The primary read is:

```http
GET /api/v1/calendar?start=2026-10-01&end=2026-11-01&timezone=America/New_York&group=arts&group=schools
```

Its semantics are:

- The range is half-open `[start, end)`. Date values are local to `timezone`;
  datetime values must carry an offset. Both timed and all-day events use
  overlap semantics, so an event beginning before the range is included if it
  ends inside it.
- Repeated `group` values use **any-group** matching. No group parameter means
  all active groups. Unknown and inactive slugs return `422`, making frontend
  typos visible.
- Only scheduled occurrences of approved, published revisions assigned to at
  least one active group are returned. A pending edit leaves its last approved
  revision visible; rejected content is never exposed.
- Responses are ordered by occurrence start. `limit` defaults to 500 (maximum
  2,000), `offset` defaults to zero, and `meta.has_more` signals another page.
- A request can span at most 366 days and must remain in the guaranteed rolling
  window (90 days back through 18 months ahead by default).

The endpoint performs one indexed SQL range query. It does not interpret an
RRULE during a user request.

## Recurrence persistence

An approved revision retains its compact RFC 5545 RRULE plus local RDATE and
EXDATE rows. Approval expands that definition into `event_occurrences` for the
rolling window in the event's IANA timezone. Expansion preserves wall-clock
time across daylight-saving changes, drops nonexistent local times, uses
EXDATE precedence, and caps one event/window at 10,000 instances.

Approval, occurrence reconciliation, the published pointer, and the audit
action commit together. Existing `(event_id, recurrence_id)` rows keep their
IDs and increment `version`; removed future instances become durable cancelled
rows. A non-recurring event keeps its one occurrence ID even if an approved edit
moves it. This gives later notification work a stable identity and version.

Run this command daily before the current forward edge expires:

```sh
uv run calendar-api materialize
```

It takes row locks, inserts only missing slots, and advances
`event_occurrence_materializations`. Past occurrences remain stored. A normal
calendar read therefore stays cheap and side-effect free.

## Routes

Public routes:

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Database readiness |
| `GET` | `/api/v1/groups` | Active filter groups |
| `GET` | `/api/v1/calendar` | Published occurrences in a range |
| `GET` | `/api/v1/events/{event_id}` | One published event |
| `POST` | `/api/v1/events` | Submit revision 1 for review |

Admin routes (all require `X-Admin-Key`):

| Method | Route | Purpose |
| --- | --- | --- |
| `GET/POST` | `/api/v1/admin/groups` | List all/create groups |
| `PATCH` | `/api/v1/admin/groups/{group_id}` | Rename, describe, activate/deactivate |
| `GET` | `/api/v1/admin/events?status=pending` | Moderation queue |
| `GET` | `/api/v1/admin/events/{event_id}` | Audit view with all revisions |
| `POST` | `/api/v1/events/{event_id}/revisions` | Submit an edit as a new revision |
| `POST` | `/api/v1/admin/events/{event_id}/revisions/{revision_id}/approve` | Publish atomically |
| `POST` | `/api/v1/admin/events/{event_id}/revisions/{revision_id}/reject` | Reject while retaining prior publication |
| `POST` | `/api/v1/admin/events/{event_id}/revoke` | Unpublish and cancel future occurrences |

Edits are intentionally admin-only until an event-specific, accountless
management-token design is added. Publicly accepting an event ID alone would
let anyone create a blocking pending revision.

## Zero-install local operation

No Docker is needed. Copy `.env.example` to `.env`, then:

```sh
uv sync --extra test
uv run calendar-api migrate  # creates .calendar-data/calendar.db
uv run calendar-api serve
```

The SQLite file persists across restarts. The API and interactive OpenAPI client
are at `http://127.0.0.1:8000` and `http://127.0.0.1:8000/docs`.

The complete suite, including API integration tests, runs against a temporary
SQLite database:

```sh
uv run pytest
```

To verify production PostgreSQL separately, point the same suite at any local
or hosted PostgreSQL 15+ database:

```sh
TEST_DATABASE_URL=postgresql://calendar:calendar@localhost:5432/calendar uv run pytest
```
