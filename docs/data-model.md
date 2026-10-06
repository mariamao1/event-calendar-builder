# Data model decisions

This document records the choices that are costly to reverse. The executable
schema is in `db/migrations/001_initial_schema.sql` and targets PostgreSQL 15+.

## Model at a glance

```text
groups <--- event_revision_groups ---> event_revisions ---> events
  ^                                          |                 |
  |                                          |                 v
subscription_groups                          +----> event_occurrences
  |                                                            |
subscriptions ---> notification_deliveries ---> delivery_items-+
```

## Events, revisions, and approval

`events` is the stable identity and holds the original, accountless submitter's
name, contact channel, optional contact value, submission time, and a SHA-256
digest of its 256-bit creator management token. The raw token is returned once
and proves ownership for later edits. Every content version is an
`event_revision`; it repeats submitter/editor provenance so the audit trail does
not depend on a user account.

The event's moderation state is
`events.current_revision_id -> event_revisions.approval_status`. Group
associations, timing, descriptive content, and recurrence are all revisioned.
That prevents an edit from bypassing review.

An edit always creates a new `pending` revision and moves
`current_revision_id` to it. It does **not** replace
`published_revision_id`. Consequently, an approved event stays publicly visible
with its last approved content while an edit is reviewed:

- Approve: mark the pending revision `approved`, record an approval action, and
  atomically move `published_revision_id` to it.
- Reject: mark it `rejected` and record the action; the last published revision
  is unchanged.
- Revoke/unpublish: clear `published_revision_id`, mark that revision `revoked`,
  record the action, and cancel its future occurrence rows.
- Cancel: stamp `events.cancelled_at` (with actor and reason) and record a
  `cancel` action. The published revision and its occurrences are untouched,
  so the event stays visible everywhere, flagged as cancelled.
- Delete: stamp `events.archived_at` and record a `delete` action. Archived
  events are excluded from the calendar, the detail view, the review queue,
  and creator reads, so a deleted event no longer exists. Future occurrences
  are cancelled for downstream history, matching revoke.

Approved/rejected/revoked revision content is immutable. A correction is another
revision. Database triggers enforce this for the revision and its group and
recurrence-date rows. The `event_review_actions` table is the append-only
moderation audit trail.

Create an event and its first revision in one transaction: insert the event,
insert revision 1, then set `current_revision_id`. The deferred composite keys
both permit this cycle and guarantee that current/published revisions belong to
the event. The published-pointer trigger additionally requires an approved
revision.

## Time and recurrence

All-day ranges are stored as `[start_date, end_date)`: the end is exclusive. A
one-day event therefore has `end_date = start_date + 1`. Timed events store
absolute `timestamptz` instants plus the originating IANA timezone. Keeping the
timezone is necessary for display and daylight-saving-aware recurrence.

Recurring events use a hybrid representation:

1. The approved revision is authoritative. `recurrence_rule` contains an RFC
   5545 RRULE without `DTSTART`; the revision's start supplies `DTSTART`.
   `event_revision_recurrence_dates` supplies local RDATE/EXDATE values.
2. `event_occurrences` materializes a finite rolling window for calendar reads,
   reminders, digests, and delivery history. Materialize 90 days back through
   18 months ahead on first approval, retain past rows indefinitely, and extend
   the forward edge daily. The exact covered range is recorded in
   `event_occurrence_materializations`.

This avoids expanding an infinite rule at read/send time while preserving the
compact source rule for future expansion. It also gives every notified
occurrence a durable foreign-key target.

`recurrence_id` is the occurrence's original wall-clock slot in the event
timezone. It is stable when one occurrence is rescheduled; actual start/end can
change and `is_exception` becomes true. A non-recurring event keeps its sole
occurrence row across approved edits.

Scoped single/future edits diverge individual occurrences from the published
series. `event_occurrences.content_override` holds a JSON object with any
subset of the descriptive fields (`title`, `description`, `location_name`,
`location_address`, `event_url`); reads merge it over the published revision
and report diverged dates via `has_override`. Timing divergence reuses the
per-occurrence start/end columns with `is_exception`. A scoped cancellation
sets `event_occurrences.instance_cancelled` instead: the date stays visible,
flagged as cancelled (scoped deletion instead flips the row to `cancelled`).
A later series-wide approval refreshes retained rows and clears both columns,
so the latest change to the series always persists over earlier exceptions.
Groups stay series-level: group changes ride on revisions, never on overrides.

When an approved revision changes:

- reconcile rows by `(event_id, recurrence_id)`;
- update retained rows from the new revision and increment `version` for every
  user-visible change;
- insert newly introduced slots at version 1;
- mark removed future slots `cancelled` and increment their version;
- never delete materialized occurrences that may have notification history.

A wholesale recurrence shift naturally cancels old slots and creates new ones.
Approval and occurrence reconciliation should commit together so readers never
observe a published revision with stale occurrences. Validate RRULE syntax and
IANA timezone names in the service before insertion; PostgreSQL has no native
RRULE or timezone-name constraint suitable for this schema.

## Groups and subscriptions

An event revision and a subscription can each belong to multiple groups through
join tables. Group changes to an event therefore go through approval like any
other content edit.

A subscription has one delivery channel/value, one or more groups, and supports
`immediate`, `daily`, or `weekly` cadence. Digest delivery uses the subscriber's
timezone, local `digest_time`, and (for weekly cadence) weekday `0..6` where 0 is
Sunday. Contact values must be normalized by the service (lowercased email;
E.164 SMS number) before uniqueness checks.

Accountless management uses an application-generated 256-bit random token,
encoded base64url in the link. Only its 32-byte SHA-256 digest is stored in
`management_token_hash`; compare digests in constant time. The management token
is stable until an explicit security rotation. Confirmation uses a separate,
expiring token so consuming it does not invalidate management links. Raw tokens
must not appear in logs, analytics, or database records.

Submitter and subscriber contacts are personal data. Restrict access, redact
logs, and define a retention/deletion policy before production use.

## Notification ledger and change behavior

`notification_deliveries` is both an outbox and the durable record of an actual
provider message. Immediate messages normally have one item; daily and weekly
digests can have many. The recipient and event revision are snapshotted by
reference/value so later edits do not make the audit record ambiguous. Each
provider attempt is recorded separately.

The unique key on
`(subscription_id, occurrence_id, occurrence_version)` prevents duplicate
sends even if queue work is retried. A retry reuses the same delivery and its
stable provider `idempotency_key`; it does not insert a new item. Create delivery
and item rows transactionally before calling an external provider, claim work
with `FOR UPDATE SKIP LOCKED`, and mark it sent only after provider acceptance.

Recipient/change rules are:

- First approved appearance: queue `event_published` for matching active,
  confirmed subscriptions.
- Approved content/time/group change: increment affected occurrence versions.
  Send `event_updated` only to a subscriber with a prior sent item for that
  occurrence. A newly matching subscriber receives `event_published` instead.
- Removed, revoked, or cancelled occurrence: increment its version and send
  `event_cancelled` only to subscribers who previously received it.
- Pending or rejected revisions: send nothing, because published content did
  not change.
- Losing the last matching group counts as cancellation for a subscriber who
  was already notified; gaining a first matching group counts as publication.

`notification_delivery_item_groups` records which group intersection caused a
recipient to qualify. Suppressed deliveries are retained to explain why a send
did not occur. This ledger is the basis for Tasks 17, 18, and 23; querying only
current event/subscription state is not sufficient for reliable change mail.

## Service-level transaction invariants

The database encodes shape, ownership, and deduplication constraints. The
application transaction layer must additionally enforce:

1. `current_revision_id` is non-null after event creation and points to the
   highest revision number.
2. Revision numbers are allocated while locking the event row.
3. A revision cannot change after it leaves `pending`; review is a valid state
   transition, not a content edit.
4. Every active subscription has at least one group. Event group assignment is
   optional; an ungrouped event appears only in unfiltered calendar reads.
5. RRULE/timezone/contact normalization is validated before persistence.
6. Publication, occurrence reconciliation, version increments, and notification
   outbox inserts are committed atomically.
7. A delivery item's `occurrence_version` equals the occurrence version whose
   content was rendered into that message.
