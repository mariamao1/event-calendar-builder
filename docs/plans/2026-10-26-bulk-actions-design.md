# Bulk actions: design

## Intent

Calendar owners (admins) currently act on one item at a time: each pending
submission is approved or rejected with its own click, and the only way to
add a group tag to a published event is to open its full edit form. Owners
should be able to select several items and act on them at once, in two
places:

1. **The review queue** — approve or reject several pending submissions
   together.
2. **The calendar** — add one or more group tags to several events together.

The typical tagging session is catching up on the events in the dates on
screen (usually "this month's events"), found by searching and/or filtering,
then checking them off a list.

## Goals

- Review queue: multi-select pending submissions; **Approve selected** and
  **Reject selected**, with one shared review note recorded on every item in
  the batch.
- Calendar: an admin-only select mode on the results list under the search
  bar; **Add tags…** adds the chosen groups to every checked event.
- Each item in a batch succeeds or fails on its own; the owner sees a summary
  ("12 approved, 1 skipped: already reviewed") naming any item that failed
  and why.
- One request per batch (server-side batch actions).

## Non-goals

- Removing tags in bulk (only adding was asked for).
- Bulk actions beyond approve, reject, and add tags (bulk cancel, delete,
  feature, unpublish).
- Per-item review notes inside a batch.
- A tagging list that reaches beyond the dates on screen (e.g. "every jazz
  event, ever"). The list follows the current month/week/day view.
- Bulk actions for non-admin accounts or creators.

## Anti-goals (would be a failure even if it technically works)

- Bulk tagging that wipes single-date edits on repeating events, changes
  dates, or re-materializes occurrences. Tagging must change tags and nothing
  else.
- Bulk tagging that is silently lost later: if an event has an edit awaiting
  review, approving that edit must not drop the tags that were just added.
- One stale item (already reviewed by someone else, deleted meanwhile)
  blocking the whole batch.
- A batch left half-applied because the browser tab closed mid-way.

## Constraints (from the existing system)

- **Two storage backends with identical behavior:** `service.py`
  (PostgreSQL) and `sqlite_service.py` (SQLite) must both implement every
  new operation; the test suite runs against SQLite.
- **Approved revisions are immutable in PostgreSQL.** Triggers in
  `db/migrations/001_initial_schema.sql` forbid changing an approved
  revision's columns and its `event_revision_groups` rows. Pending revisions
  (and their group rows) are mutable.
- **Tags live on the published revision.** Calendar, detail, featured, and
  export reads take groups from `events.published_revision_id`. Occurrences
  keep their own `source_revision_id`, `content_override`, and exception
  flags.
- **A series-wide edit clears per-date overrides.** Reusing the admin
  "edit and immediately approve" path for tagging would reconcile
  occurrences and wipe single-date edits, so tagging needs its own path.
- **Approval rules already exist** in `approve_revision`/`reject_revision`:
  only the event's current revision, only while pending; approval
  materializes occurrences within the configured window. Bulk review reuses
  these rules per item.
- **Tags must be active groups** (`_require_active_groups`).
- **Admin auth:** all new routes require the existing admin dependency.
  `ReviewInput` carries `actor` (the frontend sends the signed-in username)
  and an optional `note` (≤ 4000 chars).
- **The calendar results list** (`renderSearchResults` in `app.js`) is
  built from `state.events`, the same occurrence rows the grid shows for the
  current view. It appears only when there is search text, shows at most 50
  rows, and lists a repeating event once per date.

## Approaches considered

**A. Server-side batch actions (chosen).** New admin routes each take a list
and process every item independently inside one request, returning a
per-item result.
*Trade-off:* more server code, but one request per batch, no half-applied
batch if the tab closes, and every rule lives on the server where the
Python tests cover it.

**B. Browser loops over existing per-item routes**, plus a new single-event
"add tags" route. Least server code, but N requests per batch, a closed tab
leaves a partial batch, and the partial-failure handling lives in untested
browser code.

**C. All-or-nothing batch transactions.** Dropped: one stale item would
block the whole batch.

The owner picked **A**.

## Decided behavior

### Review queue

- Each card in the review queue dialog gets a checkbox; the list gets a
  **Select all** control covering the loaded cards (the queue loads up to
  50; this is unchanged).
- When at least one card is checked, an action bar shows the count, a
  **Review note** field (optional, shared), **Approve selected**, and
  **Reject selected**.
- The note is recorded on every item in the batch, exactly as if it had been
  typed for each one individually (same `review_note` and review action
  `note`).
- The existing per-card Approve and the detail dialog keep working
  unchanged.
- The queue holds both new submissions and pending edits of already
  published events; bulk actions apply to both, with the same semantics as
  the single-item actions (approving an edit publishes it; rejecting an edit
  keeps the prior publication).
- After the batch, the queue refreshes, the calendar reloads if anything was
  approved, and a summary is shown in the dialog. Items that failed stay in
  the list (if still pending) with their reason in the summary.

### Calendar tagging

- Admin-only. Non-admins see the results list exactly as today.
- The results list also appears when group chips are selected without
  search text (so "this month's music events" works), not only for a
  search. For non-admins this is a visible change too; that is acceptable
  (it is the same list with the same rows the grid shows).
- A **Select** toggle on the list switches it into select mode:
  - One row per event (a repeating event appears once, not once per date),
    each with a checkbox, plus **Select all**.
  - Rows are collapsed per event *before* any cap, and select mode lists
    every event in view with no 50-row cap, so **Select all** selects
    exactly the rows on screen (designer's call). If more than 500 events
    are selected (the tag batch limit), **Add tags…** is disabled with a
    note asking to narrow the search or switch to a shorter view.
  - Rows still show title, a date (the first date in view for a repeating
    event), and current group pills.
  - Clicking a row's checkbox selects; the row no longer opens the event
    while in select mode.
- An action bar shows the selected count and **Add tags…**, which opens a
  group picker listing active groups (multi-select). Confirming adds the
  chosen groups to every selected event.
- Groups an event already has are left alone; an event that already has all
  chosen groups counts as "unchanged", not as an error.
- After the batch, the calendar reloads, select mode stays on with the
  selection cleared, and a summary shows how many events were tagged, how
  many were already tagged, and names any that failed and why.

### Server: batch routes

All under the admin dependency. Body shapes are a starting point; exact
names are the builder's call as long as they read like the existing API.

- `POST /api/v1/admin/events/bulk/approve`
  `{actor, note?, items: [{event_id, revision_id}, …]}`
- `POST /api/v1/admin/events/bulk/reject`
  `{actor, note?, items: [{event_id, revision_id}, …]}`
- `POST /api/v1/admin/events/bulk/tags`
  `{actor, event_ids: [...], group_ids: [...]}`

Common rules:

- A review batch carries 1–200 items (matches the admin list's max page
  size); a tag batch carries 1–500 events (matches the calendar read's
  default page, so a full month fits in one request). Duplicates are
  collapsed. Empty or oversize lists → `422`.
- Each item runs in its own savepoint within one request transaction, so a
  failure rolls back only that item. The request returns `200` with
  `{results: [{event_id, ok, status, error?: {code, message}}], counts:
  {...}}`, results in request order. Whole-request errors (auth, validation,
  inactive/unknown group in a tag batch) use the usual status codes.
- Per-item failures reuse the existing error codes/messages of the
  single-item operations (not found, not pending, not current revision,
  etc.).

Bulk approve/reject call the same service logic as the single-item routes
(`approve_revision`, `reject_revision`), so their audit rows, occurrence
materialization, and rules are identical.

### Server: adding tags without an edit

New service operation (both backends), for one event, called per item by
the batch route:

- The event must exist, not be deleted, and have a published revision;
  otherwise the item fails as not found.
- "Add tags" means "this event should carry these tags", on both the
  published revision and any pending edit (designer's call). The item is
  `unchanged`, and writes nothing, only when the published revision *and*
  any pending edit already carry every requested group. If only the
  pending edit is missing some (for example, the creator's edit dropped
  Music while the published revision still has it), add them to the
  pending edit alone, with no new approved revision; the item counts as
  tagged.
- If the published revision is missing some requested groups, compute
  `new_groups = published groups ∪ requested groups` and mint a new
  **approved** revision that copies every column and
  recurrence-date row of the published revision, with `new_groups` as its
  groups, `supersedes_revision_id` = the published revision, `reviewed_by` =
  actor, and a review note such as "Tags added". Point
  `events.published_revision_id` at it. Record an audit row in
  `event_review_actions` (action name: builder's call, e.g. `approve` with
  that note, or a new `tag` action if the enum/check allows it cleanly).
- **Do not** reconcile, re-materialize, or touch `event_occurrences`:
  dates, per-date content overrides, cancellations, and skipped dates stay
  exactly as they are. (Reads take groups from the published revision, so
  every date shows the new tags.)
- **Pending edit present:** if `current_revision_id` is a pending revision
  (an edit awaiting review of a published event), also insert the requested
  groups into that pending revision's group rows (allowed, since pending
  rows are mutable), and leave `current_revision_id` pointing at the pending
  revision so the queue and approval still work. Approving the edit later
  therefore keeps the tags. When the current revision is not pending
  (approved, or a rejected edit), point `current_revision_id` at the new
  tag revision too, so the edit form loads the tagged content.
- If someone later saves a correction to that pending edit from a form
  opened before the bulk tag, the latest save wins and may drop the tag,
  same as any other form edit today (designer's call; no stale-form
  guard).
- **Revision numbering:** the new approved revision takes `max + 1` for
  the event. Existing code that numbers a new revision as
  `current.revision_number + 1` (`create_revision`,
  `_commit_scoped_revision`, and their SQLite twins) must switch to
  `max + 1` as well, or it collides after a tag revision. iCal `SEQUENCE`
  currently uses `revision_number` (`app.py`); it must never go backwards
  for an event when an older-numbered pending edit is approved after a tag
  revision.
- **PostgreSQL triggers** block inserting group rows into an already
  approved revision: insert the revision as pending, add its group and
  recurrence-date rows, then flip it to approved (no content change in the
  transition, so the lifecycle trigger allows it).
- Works on cancelled events too (they are still shown on the calendar).
- Must not change `is_featured`, RSVPs, uploaded files, or the management
  token.

## Left to the builder

- Exact route paths, response field names, and the audit action name, as
  long as they match existing conventions and are documented in
  `docs/backend.md` and the README. If a new action name doesn't fit
  cleanly into both schemas, use `approve` with a "Tags added" note.
- How iCal `SEQUENCE` stays monotonic (see revision numbering above).
- How savepoints are wrapped for each backend (a small shared helper is
  fine).
- Whether bulk approve/reject loop over the existing service functions
  directly or through a thin per-item wrapper.
- Visual design of checkboxes, action bars, the group picker, and the
  summary, following existing dialog and button styles in `styles.css`;
  keyboard and screen-reader access consistent with the rest of the UI.
- Which date a collapsed repeating-event row shows.
- Tests: service and HTTP tests for each batch route (success, mixed
  success/failure, duplicates, limits, auth), the tagging invariants above
  (single-date edits preserved, pending edit keeps tags, unchanged when
  already tagged, inactive group rejected), plus frontend behavior in the
  existing style of `tests/test_review_queue.py` and
  `tests/test_event_tags.py`.
