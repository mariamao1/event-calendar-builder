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
- Only scheduled occurrences of approved, published revisions are returned.
  Events without a group are included in unfiltered reads; events assigned only
  to inactive groups remain hidden. A pending edit leaves its last approved
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

Submission validation requires the event start to be the first date the RRULE
produces. Expansion always keeps the start as an occurrence, but dateutil
skips a `DTSTART` that does not match the rule, so an off-pattern start (for
example a Saturday start with `BYDAY=TU`) would add a stray date and make
`COUNT` one short. A rule whose `UNTIL` falls before the start is rejected too.
Timed series must express `UNTIL` in UTC; the event form sends 23:59 local time
on the chosen last day, so the whole final day is included.

The event form builds these RRULE shapes from the series' first date:

| Pattern | RRULE |
| --- | --- |
| Every N days | `FREQ=DAILY;INTERVAL=N` |
| Every N weeks on chosen weekdays | `FREQ=WEEKLY;INTERVAL=N;BYDAY=TU,TH` |
| Monthly on the start's date | `FREQ=MONTHLY;BYMONTHDAY=15` |
| Monthly on the Nth / last weekday | `FREQ=MONTHLY;BYDAY=2TU` / `BYDAY=-1FR` |
| Monthly on the last day | `FREQ=MONTHLY;BYMONTHDAY=-1` |
| Yearly on the start's date | `FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=26` |
| Yearly on the Nth / last weekday of the month | `FREQ=YEARLY;BYMONTH=11;BYDAY=4TH` |

Each series never ends, ends on a date (`UNTIL`), or ends after a number of
times (`COUNT`, 1–999). Rules outside this set are still accepted from the API;
the form keeps them unchanged as a "Custom schedule" unless the editor picks a
new pattern.

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
| `GET` | `/api/v1/events/{event_id}` | One published event; `?occurrence=` adds that date and series context |
| `POST` | `/api/v1/events` | Submit revision 1 for review |
| `GET` | `/api/v1/events/{event_id}/manage` | Load creator-owned editable content |
| `POST` | `/api/v1/events/{event_id}/revisions` | Submit a creator-owned edit for review (or a scoped edit; see below) |
| `POST` | `/api/v1/events/{event_id}/cancel` | Cancel an event (creator or admin); it stays visible, marked cancelled (or a scoped cancel; see below) |
| `DELETE` | `/api/v1/events/{event_id}` | Delete an event (creator or admin); it no longer exists (or a scoped delete; see below) |
| `POST` | `/api/v1/events/{event_id}/occurrences/{occurrence_id}/edit` | Edit one date (`scope=single`, default) or future dates (`scope=future`) |
| `POST` | `/api/v1/events/{event_id}/occurrences/{occurrence_id}/cancel` | Cancel one date or future dates, kept visible, marked cancelled |
| `DELETE` | `/api/v1/events/{event_id}/occurrences/{occurrence_id}` | Delete one date or future dates from the calendar |

Admin routes (require admin authentication, see below):

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/admin/login` | Username/password login, issues a session token |
| `POST` | `/api/v1/admin/logout` | Revoke the calling session token |
| `GET/POST` | `/api/v1/admin/groups` | List all/create groups |
| `PATCH` | `/api/v1/admin/groups/{group_id}` | Rename, describe, activate/deactivate |
| `GET` | `/api/v1/admin/events?status=pending` | Moderation queue |
| `GET` | `/api/v1/admin/events/{event_id}` | Audit view with all revisions |
| `POST` | `/api/v1/admin/events` | Create and immediately approve an event |
| `POST` | `/api/v1/admin/events/{event_id}/revisions` | Edit and immediately approve an event |
| `POST` | `/api/v1/admin/events/{event_id}/revisions/{revision_id}/approve` | Publish atomically |
| `POST` | `/api/v1/admin/events/{event_id}/revisions/{revision_id}/reject` | Reject while retaining prior publication |
| `POST` | `/api/v1/admin/events/{event_id}/revoke` | Unpublish and cancel future occurrences |
| `POST` | `/api/v1/admin/events/{event_id}/cancel` | Admin-only alias of event cancellation |
| `DELETE` | `/api/v1/admin/events/{event_id}` | Admin-only alias of event deletion |

The single-event read powers the event detail view. Besides the published
revision and its active groups it returns `recurrence_dates` and `occurrence`
(the requested scheduled date, or `null` if none was requested or it is no
longer scheduled). It also returns `series`, which is `null` for one-off events.
For repeating events, `series` holds the `previous`/`next` scheduled dates
around the requested one, up to six `upcoming` dates with `upcoming_count`, and
`coverage_end`, where the materialized window ends. Submitter details are never
included.

Removal distinguishes cancellation from deletion. Cancelling
(`POST /api/v1/events/{event_id}/cancel`) means "this event is cancelled": the
published content stays on the calendar and in the detail view with
`is_cancelled` set, because people may already have planned around it.
Deleting (`DELETE /api/v1/events/{event_id}`) means "this event should no
longer exist": it leaves the calendar, the detail view, the review queue, and
creator reads (all report it as missing). Both operations require either admin
authentication or the event creator's `X-Event-Management-Token`; cancelling an
already-cancelled event reports `409`, and any operation on a deleted event
reports `404`.

## Scoped edits and removals of recurring events

Editing, cancelling, or deleting a date of a repeating event asks which dates
the change applies to:

- `single` — only the selected occurrence changes. Other dates stay exactly
  as they are.
- `future` — the selected occurrence and every later one change. Earlier
  dates keep their materialized timing and content.
- `series` — the entire series changes (the default, preserving old behavior).

The scope travels as `scope` with the targeted `occurrence` (an occurrence
id) in the query string (`?scope=single&occurrence=…`), in the JSON body
(`scope` plus `occurrence_id`), or — for the dedicated occurrence routes —
in the URL path. Scope names accept common aliases (`this`, `occurrence`,
`instance` for `single`; `this_and_future`, `following` for `future`; `all`,
`entire_series` for `series`). A `single`/`future` scope without an occurrence
reports `422`, as does scoping a one-off event. Scoped edit payloads carry
content and timing but no repeat pattern of their own — omit `recurrence_rule`
and `recurrence_dates` (the form does this for you); anything sent there is
ignored, since the server reuses the published schedule, so echoing the
series schedule back can never read as a pattern change.

Mechanics:

- A `single` edit rewrites one occurrence in place (its `recurrence_id` keeps
  identifying the original slot) and stores divergent descriptive content as
  a per-occurrence override that reads merge over the published revision.
  The edit payload carries content and timing; group changes belong to a
  series-wide edit (`422` otherwise). It applies immediately for any
  authorized editor (admin or the event creator), like cancellation and
  deletion.
- A `future` edit keeps the published pattern and moves content, timing, and
  groups forward from the target date: it mints a new approved revision and
  reconciles only occurrences at or after that date, freezing changed content
  onto earlier dates so they keep showing what viewers saw. Changing the
  pattern, the timezone, or the timed/all-day shape still requires a
  series-wide edit. It refuses with `409` while a pending edit awaits review.
- A `single` cancel flags one date (`is_cancelled`) while the series stays
  published; a `single` delete removes that date from the calendar. `future`
  cancel/delete truncate the series rule before the target date (a `COUNT`
  becomes an `UNTIL` on the last kept day) and mark or remove every later
  date. Targeting the first date falls back to the whole-series removal.
- A later series-wide edit refreshes every retained occurrence and clears
  per-occurrence overrides and flags, so the latest change to the series
  always persists over earlier single/future exceptions. The calendar and
  detail views expose diverged dates via `has_override` (and per-date
  `is_cancelled`), and the UI marks them Modified/Cancelled in the series
  list. Scoped cancellations and deletions record `cancel`/`delete` review
  actions; single-date edits bump the occurrence version against the published
  revision.

Creating an event returns a one-time `management_token`. Creator reads and edits
send it in `X-Event-Management-Token`; only its SHA-256 digest is stored. The
browser packages the raw token in the URL fragment of its creator edit link, so
it is not sent in HTTP requests or server logs. A pending submission can be
corrected in place. Once reviewed, every edit becomes a new pending revision,
leaving the last approved revision visible until the edit is approved.

## Access model

Three modes, all enforced server-side:

- **Link mode (no account).** The calendar is private but reachable by anyone
  holding its link: viewing groups, the calendar, published events, and
  submitting events needs no account; submitters identify themselves by name
  on each submission. The link carries an unguessable 256-bit token
  (`CALENDAR_ACCESS_TOKEN`, generated with
  `uv run calendar-api new-link-token`) sent as `?token=`, `?access_token=`,
  or the `X-Calendar-Token` header, and is verified with a constant-time
  comparison. When `CALENDAR_ACCESS_TOKEN` is unset (local development),
  public routes stay open and the server logs a warning; set it in
  production. Missing or wrong tokens return `401`.
- **Creator mode (event-specific capability).** The event management token can
  load and edit only its event, and can cancel or delete it. It does not grant
  calendar-wide or moderation access. Losing the token does not expose the event;
  an administrator can still edit, cancel, or delete it. Raw management tokens
  are returned only at creation time and are never persisted.
- **Admin mode (real authentication).** Approving, rejecting, revoking, group
  management, and revision edits require authentication: either a session
  token from `POST /api/v1/admin/login` (username from `ADMIN_USERNAME`,
  password verified against the salted PBKDF2-HMAC-SHA256 hash in
  `ADMIN_PASSWORD_HASH`, generated with `uv run calendar-api hash-password`)
  sent as `Authorization: Bearer <token>`, or the legacy `X-Admin-Key`
  service key. Sessions are 256-bit random tokens with an absolute expiry
  (`ADMIN_SESSION_TTL_SECONDS`, default 12h) and can be revoked via
  `POST /api/v1/admin/logout`. Raw passwords and tokens are never stored or
  logged. With no admin credential configured, admin routes fail closed with
  `503`.

**Search engines.** The calendar is excluded from indexing: every response
carries `X-Robots-Tag: noindex, nofollow`, and `GET /robots.txt` answers
`User-agent: *` / `Disallow: /`.

**Rate limiting (decision: yes).** Because anyone with the link can submit
with just a name, public submissions are rate limited per client IP with an
in-memory sliding window (default 30/hour via `SUBMIT_RATE_LIMIT_MAX` /
`SUBMIT_RATE_LIMIT_WINDOW_SECONDS`), and admin logins are limited (default
10/15min via `LOGIN_RATE_LIMIT_MAX` / `LOGIN_RATE_LIMIT_WINDOW_SECONDS`) to
slow credential guessing. Limits return `429` with a `Retry-After` header.
The client IP prefers `X-Forwarded-For` when present, so run behind a proxy
that sets it or directly. Sessions and rate-limit state are process-local;
single-process deployment is assumed.

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
