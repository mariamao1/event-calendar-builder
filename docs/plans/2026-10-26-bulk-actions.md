# Bulk Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use 10x-engineer:subagent-driven-development (recommended) or 10x-engineer:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let admins approve or reject many review-queue submissions at once, and add group tags to many calendar events at once.

**Architecture:** Three admin batch routes process each item in its own
savepoint inside one request transaction and return per-item results. Bulk
review reuses the existing `approve_revision`/`reject_revision`. Tagging is a
new service operation (in both backends) that mints an approved copy of the
published revision with extra groups, without touching occurrences, and adds
the groups to any pending edit. The frontend adds select modes to the review
queue dialog and to the calendar results list.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3 (PostgreSQL) and stdlib
sqlite3 (twin service modules), vanilla JS/HTML/CSS, pytest + FastAPI
TestClient.

**Spec:** `docs/plans/2026-10-26-bulk-actions-design.md`. Read it before
starting any task.

## Global Constraints

- Every service operation exists in both `src/calendar_api/service.py`
  (PostgreSQL, `%s` params) and `src/calendar_api/sqlite_service.py`
  (SQLite, `?` params) with identical behavior. Tests run on SQLite.
- All new routes use the existing `admin` dependency.
- Review batch: 1–200 items; tag batch: 1–500 events; duplicates collapsed
  (first occurrence wins, request order kept); empty/oversize → `422`.
- Shared review note ≤ 4000 chars, recorded on every item exactly as a
  single-item review would record it.
- Tagging never reconciles, re-materializes, or updates `event_occurrences`,
  and never changes `featured_at`, RSVPs, files, or the management token.
- Test command: `uv run pytest <file> -k <name>`; full suite `uv run pytest`
  (baseline: 225 passed).
- **Do not commit.** Leave all changes uncommitted in the working tree
  (owner's standing instruction). There are no commit steps.
- Document every new route and behavior in `docs/backend.md` and the
  README in the task that adds it, matching the existing tone.

## Review Focus

- A batch naming the same item twice: it is processed once and appears
  once in `results` (Task 1 and Task 2 tests).
- A submission someone else approved a moment earlier inside a bulk
  approve: that item reports `conflict`; the others still publish (Task 1).
- A repeating event with a single-date edit, a cancelled date, and a
  skipped date gets tagged: all three survive unchanged (Task 2).
- Tagging an event with a pending edit, then approving the edit: tags stay,
  and the `.ics` `SEQUENCE` never goes down (Task 2).
- A group deactivated between opening the picker and applying: the whole
  tag request is `422` and nothing is written (Task 2).

---

### Task 1: Per-item savepoints and bulk approve/reject routes

**Files:**
- Modify: `src/calendar_api/database.py` (add `savepoint` to `Database` and `SQLiteDatabase`)
- Create: `src/calendar_api/bulk.py`
- Modify: `src/calendar_api/schemas.py` (near `ReviewInput`, ~line 451)
- Modify: `src/calendar_api/app.py` (next to the approve/reject routes, ~line 1351)
- Modify: `docs/backend.md`, `README.md`
- Test: `tests/test_bulk_review.py` (new; copy `_client`, `_payload`, `_headers` helpers from `tests/test_review_queue.py`)

**Interfaces:**
- Produces:
  - `Database.savepoint(connection) -> ContextManager[None]` and the same on
    `SQLiteDatabase`. PostgreSQL: a nested `connection.transaction()`.
    SQLite: `SAVEPOINT bulk_item` / `RELEASE` on success,
    `ROLLBACK TO bulk_item` then `RELEASE` on exception, then re-raise.
  - `bulk.run_each(keys: list, savepoint, fn: Callable[[key], str]) -> dict`
    in `src/calendar_api/bulk.py`. Deduplicates `keys` preserving order,
    calls `fn(key)` inside `savepoint(...)` per key; `fn` returns the
    success status string. Catches `ApiError` only (anything else
    propagates and fails the request). Returns
    `{"results": [{"event_id": str, "ok": bool, "status": str, "error"?: {"code": str, "message": str}}], "counts": {status: int, ..., "failed": int}}`
    with failed items getting `"status": "failed"`. `event_id` is taken from
    the key (`key.event_id` for review items, the key itself for UUIDs).
  - `BulkReviewItem(StrictModel)`: `event_id: UUID`, `revision_id: UUID`.
  - `BulkReviewInput(StrictModel)`: `actor` and `note` exactly as in
    `ReviewInput`; `items: list[BulkReviewItem]` with `min_length=1, max_length=200`.
  - `POST /api/v1/admin/events/bulk/approve` and
    `POST /api/v1/admin/events/bulk/reject`, body `BulkReviewInput`, `200`
    with the `run_each` result. Per item they call
    `service.approve_revision(...)` (same `past_days`/`future_months` as the
    single route) / `service.reject_revision(...)` with
    `ReviewInput(actor=..., note=...)`, returning status `"approved"` /
    `"rejected"`. Dedupe key: `(event_id, revision_id)`.
  - Declare these routes **before** any `/api/v1/admin/events/{event_id}`
    POST route that could shadow `bulk`.

- [ ] **Step 1: Write the failing tests** in `tests/test_bulk_review.py`

```python
def test_bulk_approve_publishes_every_selected_submission(tmp_path):
    # submit 3 events; bulk approve 2 with note "Looks good"
    # -> 200, counts == {"approved": 2, "failed": 0}, results in request order
    # -> both on the calendar, third still in the pending queue
    # -> GET /api/v1/admin/events/{id}: approved revision review_note == "Looks good",
    #    reviewed_by == the actor

def test_bulk_reject_records_the_shared_note(tmp_path):
    # bulk reject 2 with note "Duplicate listing" -> counts {"rejected": 2, "failed": 0}
    # -> neither on calendar; both revisions approval_status "rejected",
    #    review_note "Duplicate listing"

def test_bulk_approve_skips_already_reviewed_items_and_keeps_the_rest(tmp_path):
    # approve A singly first; bulk approve [A, B]
    # -> A: ok False, status "failed", error.code "conflict"; B: ok True, "approved"
    # -> counts {"approved": 1, "failed": 1}; B on calendar

def test_bulk_review_unknown_item_reports_not_found(tmp_path):
    # random uuid pair -> that item error.code "not_found"; others proceed

def test_bulk_review_collapses_duplicates(tmp_path):
    # items [A, A, B] -> len(results) == 2, counts {"approved": 2, "failed": 0}

def test_bulk_review_limits_and_auth(tmp_path):
    # items [] -> 422; 201 items -> 422; note of 4001 chars -> 422
    # no admin header -> 401 (same as single-item admin routes)
```

- [ ] **Step 2: Run** `uv run pytest tests/test_bulk_review.py` — expect FAIL (404 on the new routes).

- [ ] **Step 3: Implement** `savepoint`, `bulk.run_each`, the two schemas, and the two routes per the Interfaces block. The route opens `database.transaction()`, then `bulk.run_each(payload.items, lambda: database.savepoint(connection), ...)`.

- [ ] **Step 4: Run** `uv run pytest tests/test_bulk_review.py` — expect PASS; then `uv run pytest` — expect all passing.

- [ ] **Step 5: Document** both routes in the admin routes table and a short "Bulk actions" section in `docs/backend.md` (per-item savepoints, result shape, limits), and one sentence in the README's review-queue paragraph (filled in fully in Task 3).

---

### Task 2: Adding tags without an edit, and the bulk tag route

**Files:**
- Modify: `src/calendar_api/service.py` and `src/calendar_api/sqlite_service.py`
  (new `add_event_tags`, `require_active_groups`, `_next_revision_number`;
  change `create_revision` ~line 491 / sqlite ~522, `_commit_scoped_revision`
  ~2571 / sqlite ~2672, `approve_revision`)
- Create: `db/migrations/014_event_tag_action.sql`
- Modify: `src/calendar_api/sqlite_schema.sql:101` (add `'tag'` to the CHECK)
- Modify: `src/calendar_api/database.py` (`_migrate_review_actions` sentinel `'restore'` → `'tag'`, docstring)
- Modify: `tests/test_event_removal.py:196`, `tests/test_occurrence_exceptions.py:427-438` (their legacy CHECK strings must account for `'tag'` so the legacy-rebuild tests still exercise a rebuild)
- Modify: `src/calendar_api/schemas.py`, `src/calendar_api/app.py`
- Modify: `docs/backend.md`, `docs/data-model.md`, `README.md` (add the 014 `psql` line)
- Test: `tests/test_bulk_tags.py` (new)

**Interfaces:**
- Consumes: `bulk.run_each`, `Database.savepoint` (Task 1).
- Produces:
  - `require_active_groups(connection, group_ids: list[UUID]) -> None` in
    both services: public name for the existing `_require_active_groups`
    (keep the private name working).
  - `_next_revision_number(connection, event_id) -> int`: `MAX(revision_number) + 1` for the event.
  - `add_event_tags(connection, event_id: UUID, group_ids: list[UUID], *, actor: str, now: datetime | None = None) -> dict`
    returning `{"event_id", "status": "tagged" | "unchanged", "revision_id": UUID | None}`
    (`revision_id` = the new approved revision, or `None` when none was minted).
  - `BulkTagInput(StrictModel)`: `actor` (as in `ReviewInput`),
    `event_ids: list[UUID]` (`min_length=1, max_length=500`),
    `group_ids: list[UUID]` (`min_length=1`).
  - `POST /api/v1/admin/events/bulk/tags`, body `BulkTagInput`: inside one
    transaction, `service.require_active_groups` first (whole request
    `422` if any group is unknown/inactive/deleted), then `run_each` over
    `event_ids` calling `add_event_tags`, status from its result.

`add_event_tags` behavior (from the spec's "adding tags without an edit"):

1. Lock the event row (`FOR UPDATE` in PG). Missing, `archived_at` set, or
   `published_revision_id` null → `NotFoundError("event not found")`.
2. `missing_pub` = requested − published groups (ignore deleted groups when
   comparing). If the current revision is pending, `missing_pend` =
   requested − its groups; else empty. Both empty → `unchanged`, write nothing.
3. If `missing_pend`: insert those rows into the pending revision's
   `event_revision_groups`.
4. If `missing_pub`: insert a new revision copying every content column of
   the published revision as **pending** with
   `revision_number = _next_revision_number`, `supersedes_revision_id` =
   published id, `submitted_at = now`; insert groups (published ∪ requested)
   and copy recurrence-date rows; then `UPDATE` it to `approved` setting
   only `approval_status, reviewed_at = now, reviewed_by = actor,
   review_note = 'Tags added'` (PG lifecycle trigger requires exactly this).
   Set `published_revision_id` to it, and `current_revision_id` to it too
   unless the current revision is pending. Insert an `event_review_actions`
   row with action `'tag'`, note `'Tags added'`.
5. Return `tagged` if step 3 or 4 wrote anything.

Numbering changes so revision numbers (and iCal `SEQUENCE`, which reads the
published `revision_number`) keep increasing in publication order:
- `create_revision` and `_commit_scoped_revision` number new revisions with
  `_next_revision_number` instead of `current + 1`.
- `approve_revision`: before flipping to approved, if the pending revision's
  number is below the event's max, `UPDATE` it to `_next_revision_number`
  (allowed while pending).

Migration `014_event_tag_action.sql`: header comment in the style of
`006_occurrence_exceptions.sql`, then
`ALTER TYPE event_review_action ADD VALUE IF NOT EXISTS 'tag';`.

- [ ] **Step 1: Write the failing tests** in `tests/test_bulk_tags.py` (helpers: create groups via `POST /api/v1/admin/groups`, publish via `POST /api/v1/admin/events`, as in `tests/test_event_tags.py` / `tests/test_featured_events.py`)

```python
def test_bulk_tags_add_groups_to_every_selected_event(tmp_path):
    # events A (tagged arts), B (untagged); add [music] to [A, B]
    # -> counts {"tagged": 2, "failed": 0}
    # -> calendar read: A groups {arts, music}, B groups {music}

def test_event_already_carrying_the_tags_is_unchanged(tmp_path):
    # A tagged music; add [music] -> status "unchanged";
    # admin audit view revision count unchanged

def test_tagging_keeps_single_date_edits_cancellations_and_skips(tmp_path):
    # weekly series; single-date edit title on date 2, single cancel date 3,
    # single delete date 4; tag [music]
    # -> date 2 still shows edited title, date 3 is_cancelled/instance_exception
    #    "cancelled", date 4 listed under series.skipped; every date has music;
    #    occurrence ids and versions unchanged

def test_tags_survive_approving_a_pending_edit(tmp_path):
    # publish (creator flow + approve), creator submits edit (pending), tag [music]
    # -> calendar shows music now; queue still lists the pending edit
    # -> approve the edit -> calendar still shows music

def test_tag_restores_group_a_pending_edit_dropped(tmp_path):
    # published has music; creator's pending edit drops music; tag [music]
    # -> status "tagged", no new approved revision; approving the edit keeps music

def test_ics_sequence_never_decreases(tmp_path):
    # publish, pending edit, tag, approve edit; read SEQUENCE from
    # GET /api/v1/events/{id}/ics after the tag and after the approval
    # -> second > first

def test_tagging_writes_an_audit_action_and_keeps_featured(tmp_path):
    # feature A; tag -> still is_featured; admin view: newest revision
    # reviewed_by actor, review_note "Tags added"

def test_cancelled_event_can_be_tagged(tmp_path):
    # cancel A (stays visible); tag [music] -> "tagged"; still is_cancelled

def test_editing_after_a_rejected_edit_loads_tagged_content(tmp_path):
    # creator edit rejected; tag [music]; GET .../manage (creator token)
    # -> groups include music

def test_inactive_group_rejects_the_whole_batch(tmp_path):
    # deactivate music; tag [music] on [A, B] -> 422; A and B groups unchanged

def test_missing_or_deleted_event_fails_alone(tmp_path):
    # [deleted A, random uuid, B] -> A and random "not_found"; B "tagged"

def test_bulk_tags_limits_duplicates_and_auth(tmp_path):
    # [A, A] -> one result; [] -> 422; 501 ids -> 422; group_ids [] -> 422;
    # no admin -> 401
```

- [ ] **Step 2: Run** `uv run pytest tests/test_bulk_tags.py` — expect FAIL.

- [ ] **Step 3: Implement** the migration, SQLite schema/migration sentinel, numbering changes, `require_active_groups`, and `add_event_tags` in both services.

- [ ] **Step 4: Implement** `BulkTagInput` and the route.

- [ ] **Step 5: Run** `uv run pytest tests/test_bulk_tags.py` — expect PASS; then `uv run pytest` — expect all passing (including the updated legacy-CHECK tests).

- [ ] **Step 6: Document** the route, the tagging semantics (no occurrence changes, pending edits carry the tags, `'tag'` audit action, revision renumbering on approval), and the 014 migration in `docs/backend.md`, `docs/data-model.md`, and the README.

---

### Task 3: Review queue multi-select

**Files:**
- Modify: `src/calendar_api/frontend/index.html` (inside `#review-queue-dialog`, ~lines 323-337)
- Modify: `src/calendar_api/frontend/static/app.js` (`reviewCard`, `renderReviewQueue`, `refreshReviewQueue`, near `moderateReview` ~4452)
- Modify: `src/calendar_api/frontend/static/styles.css`
- Modify: `README.md`
- Test: `tests/test_review_queue.py` (extend)

**Interfaces:**
- Consumes: the bulk approve/reject routes (Task 1).
- Produces (Task 4 reuses): `function bulkSummaryText(result, labels, titleFor) -> string`
  where `labels` maps success statuses to words (e.g. `{approved: "approved"}`)
  and `titleFor(eventId)` names failed items. Output shape:
  `"12 approved, 1 skipped: Rooftop cinema night (revision is not pending)"`;
  zero-count statuses are omitted.

Markup to add: `#review-select-all` (checkbox labelled "Select all"),
`#review-bulk-bar` (hidden until something is checked) containing
`#review-bulk-count` ("3 selected"), `#review-bulk-note` (textarea,
`maxlength="4000"`, placeholder "Optional note recorded on every selected
submission"), `#review-bulk-approve` ("Approve selected"),
`#review-bulk-reject` ("Reject selected"), and `#review-bulk-summary`
(`role="status"`).

Behavior: `reviewSelection` (a `Set` of event ids); each `reviewCard` gets a
`.review-card-select` checkbox with `aria-label` `Select "<title>"`;
`renderReviewQueue` prunes the selection to loaded items; `updateReviewBulkBar()`
syncs the count, the bar's visibility, and the select-all state (checked /
indeterminate). `async function bulkModerateReview(action)` posts
`{actor: adminDisplayName(), note?, items: [{event_id, revision_id}]}` to
`/api/v1/admin/events/bulk/${action}`, disables the dialog's buttons while
in flight, uses `handleReviewAuthError` like `moderateReview`, then clears
the selection and note, shows `bulkSummaryText`, calls
`refreshReviewQueue()`, and calls `loadMonth()` when anything was approved.

- [ ] **Step 1: Write the failing test** `test_review_queue_supports_bulk_review` in `tests/test_review_queue.py`, in the style of `test_calendar_shell_contains_review_queue`: assert the six ids above are in the shell; `bulkModerateReview`, `bulkSummaryText`, `updateReviewBulkBar`, `review-card-select`, and `/api/v1/admin/events/bulk/` are in `app.js`; `.review-card-select` and `.bulk-bar` are in `styles.css`.

- [ ] **Step 2: Run** `uv run pytest tests/test_review_queue.py` — expect FAIL.

- [ ] **Step 3: Implement** markup, JS, and styles (reuse existing dialog/button classes; checkboxes reachable by keyboard).

- [ ] **Step 4: Run** `uv run pytest` — expect all passing.

- [ ] **Step 5: Manually verify** with `uv run calendar-api serve`: sign in as admin, submit three events, select two, approve with a note, confirm the summary, the queue, and the calendar. Then approve one singly in a second tab and bulk-approve it again to see the "skipped" summary.

- [ ] **Step 6: Document** in the README's review-queue paragraph: checking submissions, approve/reject selected, the shared note, the summary.

---

### Task 4: Calendar results select mode and Add tags

**Files:**
- Modify: `src/calendar_api/frontend/index.html` (new `#bulk-tags-dialog`, alongside the other dialogs)
- Modify: `src/calendar_api/frontend/static/app.js` (`renderSearchResults` ~766, admin sign-in/out handling ~4090)
- Modify: `src/calendar_api/frontend/static/styles.css`
- Modify: `README.md`
- Test: `tests/test_event_tags.py` (extend)

**Interfaces:**
- Consumes: `POST /api/v1/admin/events/bulk/tags` (Task 2), `bulkSummaryText` (Task 3).
- Produces: `function collapseByEvent(events) -> Array`: the first
  occurrence per `event_id` in `state.events` order.

Behavior:
- `renderSearchResults` shows the list when `state.search` or
  `state.groupFilter.length` is set. With no search text the heading reads
  `"N events in the selected groups"` (`"No events in the selected groups in this view."` when empty).
- Admins only (`isAdminSignedIn()`): a `.search-select-toggle` button
  ("Select" / "Done", `aria-pressed`) in the heading row. `state.selectMode`
  and `tagSelection` (a `Set` of event ids) live in module state; signing out
  or toggling off clears both.
- Select mode renders `collapseByEvent(state.events)` with no 50-row cap.
  Each row is a `<label class="search-result">` with a
  `.search-result-select` checkbox, the title, the date of that first
  occurrence, and the group pills; clicking does not open the event. A
  "Select all" checkbox covers exactly the listed rows. Each render prunes
  `tagSelection` to the listed event ids.
- The action bar (`.bulk-bar`) shows `"N selected"` and **Add tags…**,
  disabled when nothing is selected or more than 500 are selected; above
  500 show "Select at most 500 events at a time. Narrow the search or switch to a shorter view."
- **Add tags…** opens `#bulk-tags-dialog`: `#bulk-tags-options` (one
  checkbox per group in `state.groups`, using `groupPill` colors),
  `#bulk-tags-apply` ("Add tags", disabled until a group is chosen),
  `#bulk-tags-error` (`role="alert"`). `async function applyBulkTags()`
  posts `{actor: adminDisplayName(), event_ids, group_ids}`; a `422` shows in
  `#bulk-tags-error` and keeps the dialog open. On success: close the dialog,
  clear `tagSelection`, stay in select mode, `await loadMonth()`, and show
  `bulkSummaryText(result, {tagged: "tagged", unchanged: "already tagged"}, ...)`
  in `#search-bulk-summary` (`role="status"`, rendered inside `#search-results`).

- [ ] **Step 1: Write the failing test** `test_admin_can_bulk_tag_from_results` in `tests/test_event_tags.py`: assert `id="bulk-tags-dialog"`, `id="bulk-tags-options"`, `id="bulk-tags-apply"`, `id="bulk-tags-error"` in the shell; `collapseByEvent`, `applyBulkTags`, `search-select-toggle`, `search-result-select`, `search-bulk-summary`, `/api/v1/admin/events/bulk/tags`, and `state.groupFilter.length` in `app.js`; and that the `renderSearchResults` function section contains `isAdminSignedIn` and `collapseByEvent` (slice the function as the existing test does).

- [ ] **Step 2: Run** `uv run pytest tests/test_event_tags.py` — expect FAIL.

- [ ] **Step 3: Implement** markup, JS, and styles.

- [ ] **Step 4: Run** `uv run pytest` — expect all passing.

- [ ] **Step 5: Manually verify** with `uv run calendar-api serve`: as admin, pick a group chip with no search text, enter select mode, confirm a weekly series is listed once, select all, add two groups, check the summary and that every date of the series shows the new circles. Sign out and confirm the toggle is gone and the list still works for search.

- [ ] **Step 6: Document** in the README's groups/search paragraph: the list now also appears for a group filter; admins can select events and add tags; repeats appear once; existing per-date edits are kept.
