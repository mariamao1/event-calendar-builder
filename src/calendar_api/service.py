from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from psycopg import Connection
from psycopg.errors import UniqueViolation

from .errors import ConflictError, NotFoundError, UnauthorizedError, ValidationError
from .normalization import normalize_contact
from .occurrence_scopes import (
    CONTENT_OVERRIDE_FIELDS,
    EXCEPTION_CANCELLED,
    EXCEPTION_SKIPPED,
    SKIPPED_REASON,
    apply_content_override,
    build_content_override,
    match_exceptions_by_day,
    parse_content_override,
)
from .recurrence import (
    OccurrenceSpec,
    expand_revision,
    reattach_wall_time,
    truncate_rule_before,
)
from .schemas import EventRevisionInput, GroupCreate, GroupUpdate, ReviewInput
from .security import new_token, token_digest_bytes, token_matches_digest


def _now() -> datetime:
    return datetime.now(UTC)


def _as_dict(row: Any) -> dict[str, Any]:
    return dict(row)


def _execute_many(connection: Connection, query: str, params: Iterable[Any]) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(query, params)


def list_groups(
    connection: Connection, *, include_inactive: bool = False
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT id, slug, name, description, is_active, created_at, updated_at
          FROM groups
         WHERE is_active OR %(include_inactive)s
         ORDER BY name, id
        """,
        {"include_inactive": include_inactive},
    ).fetchall()
    return [_as_dict(row) for row in rows]


def create_group(connection: Connection, payload: GroupCreate) -> dict:
    try:
        row = connection.execute(
            """
            INSERT INTO groups (slug, name, description)
            VALUES (%s, %s, %s)
            RETURNING id, slug, name, description, is_active, created_at, updated_at
            """,
            (payload.slug, payload.name, payload.description),
        ).fetchone()
    except UniqueViolation as exc:
        raise ConflictError(f"group slug '{payload.slug}' already exists") from exc
    return _as_dict(row)


def update_group(connection: Connection, group_id: UUID, payload: GroupUpdate) -> dict:
    changes = payload.model_dump(exclude_unset=True)
    assignments = ", ".join(f"{field} = %({field})s" for field in changes)
    row = connection.execute(
        f"""
        UPDATE groups SET {assignments}
         WHERE id = %(group_id)s
        RETURNING id, slug, name, description, is_active, created_at, updated_at
        """,
        {**changes, "group_id": group_id},
    ).fetchone()
    if row is None:
        raise NotFoundError("group not found")
    return _as_dict(row)


def _require_active_groups(connection: Connection, group_ids: list[UUID]) -> None:
    if not group_ids:
        return
    rows = connection.execute(
        "SELECT id FROM groups WHERE id = ANY(%s) AND is_active", (group_ids,)
    ).fetchall()
    if len(rows) != len(group_ids):
        raise ValidationError("every group_id must reference an active group")


def _insert_revision(
    connection: Connection,
    event_id: UUID,
    revision_number: int,
    supersedes_revision_id: UUID | None,
    payload: EventRevisionInput,
) -> dict:
    contact = normalize_contact(payload.submitter.channel, payload.submitter.contact)
    values = {
        "event_id": event_id,
        "revision_number": revision_number,
        "supersedes_revision_id": supersedes_revision_id,
        "title": payload.title,
        "description": payload.description,
        "location_name": payload.location_name or None,
        "location_address": payload.location_address or None,
        "event_url": payload.event_url or None,
        "is_all_day": payload.is_all_day,
        "starts_at": payload.starts_at,
        "ends_at": payload.ends_at,
        "start_date": payload.start_date,
        "end_date": payload.end_date,
        "timezone": payload.timezone,
        "recurrence_rule": payload.recurrence_rule,
        "submitted_by_name": payload.submitter.name,
        "submitted_by_channel": payload.submitter.channel,
        "submitted_by_contact": contact,
    }
    revision = connection.execute(
        """
        INSERT INTO event_revisions (
          event_id, revision_number, supersedes_revision_id,
          title, description, location_name, location_address, event_url,
          is_all_day, starts_at, ends_at, start_date, end_date, timezone,
          recurrence_rule, submitted_by_name, submitted_by_channel,
          submitted_by_contact
        ) VALUES (
          %(event_id)s, %(revision_number)s, %(supersedes_revision_id)s,
          %(title)s, %(description)s, %(location_name)s, %(location_address)s,
          %(event_url)s, %(is_all_day)s, %(starts_at)s, %(ends_at)s,
          %(start_date)s, %(end_date)s, %(timezone)s, %(recurrence_rule)s,
          %(submitted_by_name)s, %(submitted_by_channel)s,
          %(submitted_by_contact)s
        )
        RETURNING *
        """,
        values,
    ).fetchone()
    _execute_many(
        connection,
        "INSERT INTO event_revision_groups (event_revision_id, group_id) VALUES (%s, %s)",
        [(revision["id"], group_id) for group_id in payload.group_ids],
    )
    if payload.recurrence_dates:
        _execute_many(
            connection,
            """
            INSERT INTO event_revision_recurrence_dates
              (event_revision_id, local_start, kind)
            VALUES (%s, %s, %s)
            """,
            [
                (revision["id"], item.local_start, item.kind)
                for item in payload.recurrence_dates
            ],
        )
    return _as_dict(revision)


def create_event(connection: Connection, payload: EventRevisionInput) -> dict:
    _require_active_groups(connection, payload.group_ids)
    contact = normalize_contact(payload.submitter.channel, payload.submitter.contact)
    management_token = new_token()
    event = connection.execute(
        """
        INSERT INTO events (
          original_submitter_name, original_submitter_channel,
          original_submitter_contact, management_token_hash
        ) VALUES (%s, %s, %s, %s)
        RETURNING id, submitted_at
        """,
        (
            payload.submitter.name,
            payload.submitter.channel,
            contact,
            token_digest_bytes(management_token),
        ),
    ).fetchone()
    revision = _insert_revision(connection, event["id"], 1, None, payload)
    connection.execute(
        "UPDATE events SET current_revision_id = %s WHERE id = %s",
        (revision["id"], event["id"]),
    )
    return {
        "event_id": event["id"],
        "revision_id": revision["id"],
        "revision_number": 1,
        "approval_status": "pending",
        "submitted_at": event["submitted_at"],
        "management_token": management_token,
    }


def event_exists(connection: Connection, event_id: UUID) -> bool:
    """Whether the event exists and has not been deleted."""
    row = connection.execute(
        "SELECT 1 AS one FROM events WHERE id = %s AND archived_at IS NULL",
        (event_id,),
    ).fetchone()
    return row is not None


def event_management_token_matches(
    connection: Connection, event_id: UUID, token: str | None
) -> bool:
    row = connection.execute(
        "SELECT management_token_hash FROM events WHERE id = %s AND archived_at IS NULL",
        (event_id,),
    ).fetchone()
    return row is not None and token_matches_digest(token, row["management_token_hash"])


def get_editable_event(connection: Connection, event_id: UUID) -> dict:
    row = connection.execute(
        """
        SELECT r.id AS revision_id, r.event_id, r.revision_number,
               r.approval_status, r.title, r.description, r.location_name,
               r.location_address, r.event_url, r.is_all_day, r.starts_at,
               r.ends_at, r.start_date, r.end_date, r.timezone,
               r.recurrence_rule, r.submitted_by_name, r.submitted_by_channel,
               r.submitted_by_contact, r.submitted_at,
               (SELECT COALESCE(jsonb_agg(
                         jsonb_build_object('id', g.id, 'slug', g.slug, 'name', g.name)
                         ORDER BY g.name, g.id
                       ), '[]'::jsonb)
                  FROM event_revision_groups rg
                  JOIN groups g ON g.id = rg.group_id
                 WHERE rg.event_revision_id = r.id) AS groups,
               (SELECT COALESCE(jsonb_agg(
                         jsonb_build_object('local_start', d.local_start, 'kind', d.kind)
                         ORDER BY d.local_start
                       ), '[]'::jsonb)
                  FROM event_revision_recurrence_dates d
                 WHERE d.event_revision_id = r.id) AS recurrence_dates
          FROM events e
          JOIN event_revisions r ON r.id = e.current_revision_id
         WHERE e.id = %s AND e.archived_at IS NULL
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("event not found")
    return _as_dict(row)


def create_revision(
    connection: Connection, event_id: UUID, payload: EventRevisionInput
) -> dict:
    _require_active_groups(connection, payload.group_ids)
    event = connection.execute(
        """
        SELECT e.id, e.current_revision_id, e.archived_at,
               r.revision_number, r.approval_status
          FROM events e
          JOIN event_revisions r ON r.id = e.current_revision_id
         WHERE e.id = %s
         FOR UPDATE OF e, r
        """,
        (event_id,),
    ).fetchone()
    if event is None or event["archived_at"] is not None:
        raise NotFoundError("event not found")
    if event["approval_status"] == "pending":
        # A creator may correct a submission that is still waiting in the
        # review queue. Pending content is intentionally mutable; once it is
        # reviewed, normal edits create a new immutable audit revision.
        contact = normalize_contact(
            payload.submitter.channel, payload.submitter.contact
        )
        revision = connection.execute(
            """
            UPDATE event_revisions SET
              title = %(title)s, description = %(description)s,
              location_name = %(location_name)s,
              location_address = %(location_address)s,
              event_url = %(event_url)s, is_all_day = %(is_all_day)s,
              starts_at = %(starts_at)s, ends_at = %(ends_at)s,
              start_date = %(start_date)s, end_date = %(end_date)s,
              timezone = %(timezone)s, recurrence_rule = %(recurrence_rule)s,
              submitted_by_name = %(submitted_by_name)s,
              submitted_by_channel = %(submitted_by_channel)s,
              submitted_by_contact = %(submitted_by_contact)s,
              submitted_at = now()
             WHERE id = %(revision_id)s
            RETURNING *
            """,
            {
                "revision_id": event["current_revision_id"],
                "title": payload.title,
                "description": payload.description,
                "location_name": payload.location_name or None,
                "location_address": payload.location_address or None,
                "event_url": payload.event_url or None,
                "is_all_day": payload.is_all_day,
                "starts_at": payload.starts_at,
                "ends_at": payload.ends_at,
                "start_date": payload.start_date,
                "end_date": payload.end_date,
                "timezone": payload.timezone,
                "recurrence_rule": payload.recurrence_rule,
                "submitted_by_name": payload.submitter.name,
                "submitted_by_channel": payload.submitter.channel,
                "submitted_by_contact": contact,
            },
        ).fetchone()
        connection.execute(
            "DELETE FROM event_revision_groups WHERE event_revision_id = %s",
            (revision["id"],),
        )
        _execute_many(
            connection,
            "INSERT INTO event_revision_groups (event_revision_id, group_id) VALUES (%s, %s)",
            [(revision["id"], group_id) for group_id in payload.group_ids],
        )
        connection.execute(
            "DELETE FROM event_revision_recurrence_dates WHERE event_revision_id = %s",
            (revision["id"],),
        )
        if payload.recurrence_dates:
            _execute_many(
                connection,
                """
                INSERT INTO event_revision_recurrence_dates
                  (event_revision_id, local_start, kind) VALUES (%s, %s, %s)
                """,
                [
                    (revision["id"], item.local_start, item.kind)
                    for item in payload.recurrence_dates
                ],
            )
        return {
            "event_id": event_id,
            "revision_id": revision["id"],
            "revision_number": revision["revision_number"],
            "approval_status": "pending",
            "submitted_at": revision["submitted_at"],
            "updated_pending_revision": True,
        }

    revision = _insert_revision(
        connection,
        event_id,
        event["revision_number"] + 1,
        event["current_revision_id"],
        payload,
    )
    connection.execute(
        "UPDATE events SET current_revision_id = %s WHERE id = %s",
        (revision["id"], event_id),
    )
    return {
        "event_id": event_id,
        "revision_id": revision["id"],
        "revision_number": revision["revision_number"],
        "approval_status": "pending",
        "submitted_at": revision["submitted_at"],
    }


def _load_recurrence_dates(connection: Connection, revision_id: UUID) -> list[dict]:
    rows = connection.execute(
        """
        SELECT local_start, kind
          FROM event_revision_recurrence_dates
         WHERE event_revision_id = %s
         ORDER BY local_start
        """,
        (revision_id,),
    ).fetchall()
    return [_as_dict(row) for row in rows]


def _occurrence_values(
    event_id: UUID, revision_id: UUID, spec: OccurrenceSpec
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "source_revision_id": revision_id,
        "recurrence_id": spec.recurrence_id,
        "is_exception": spec.is_exception,
        "is_all_day": spec.is_all_day,
        "starts_at": spec.starts_at,
        "ends_at": spec.ends_at,
        "start_date": spec.start_date,
        "end_date": spec.end_date,
        "timezone": spec.timezone,
    }


_OCCURRENCE_UPSERT = """
INSERT INTO event_occurrences (
  event_id, source_revision_id, recurrence_id, status, is_exception,
  is_all_day, starts_at, ends_at, start_date, end_date, timezone
) VALUES (
  %(event_id)s, %(source_revision_id)s, %(recurrence_id)s, 'scheduled',
  %(is_exception)s, %(is_all_day)s, %(starts_at)s, %(ends_at)s,
  %(start_date)s, %(end_date)s, %(timezone)s
)
ON CONFLICT (event_id, recurrence_id) DO UPDATE SET
  source_revision_id = EXCLUDED.source_revision_id,
  -- A series-wide approval clears per-occurrence edits, so the latest change
  -- to the series always persists over earlier single/future edits. A
  -- single-date cancellation or skip is kept, though: the series still
  -- produces the date, and it should stay cancelled or skipped. Skipped rows
  -- refresh their timing invisibly, ready for a restore.
  status = CASE WHEN event_occurrences.instance_exception = 'skipped'
                THEN 'cancelled'::occurrence_status
                ELSE 'scheduled'::occurrence_status END,
  version = event_occurrences.version
    + CASE WHEN event_occurrences.instance_exception = 'skipped' THEN 0 ELSE 1 END,
  is_exception = EXCLUDED.is_exception,
  is_all_day = EXCLUDED.is_all_day,
  starts_at = EXCLUDED.starts_at,
  ends_at = EXCLUDED.ends_at,
  start_date = EXCLUDED.start_date,
  end_date = EXCLUDED.end_date,
  timezone = EXCLUDED.timezone,
  cancellation_reason = CASE WHEN event_occurrences.instance_exception = 'skipped'
                             THEN event_occurrences.cancellation_reason END,
  content_override = NULL,
  instance_cancelled =
    event_occurrences.instance_exception IS NOT DISTINCT FROM 'cancelled'
"""

_OCCURRENCE_INSERT_IGNORE = (
    _OCCURRENCE_UPSERT.split("ON CONFLICT (event_id, recurrence_id)")[0]
    + "ON CONFLICT (event_id, recurrence_id) DO NOTHING"
)


def _apply_exception(
    connection: Connection, event_id: UUID, recurrence_id: datetime, exception: str
) -> None:
    """Mark one slot cancelled or skipped on its own, unless it already is."""
    if exception == EXCEPTION_SKIPPED:
        connection.execute(
            """
            UPDATE event_occurrences SET status = 'cancelled',
              cancellation_reason = %s, instance_cancelled = FALSE,
              instance_exception = %s, version = version + 1, updated_at = now()
             WHERE event_id = %s AND recurrence_id = %s
               AND instance_exception IS NULL
            """,
            (SKIPPED_REASON, exception, event_id, recurrence_id),
        )
    else:
        connection.execute(
            """
            UPDATE event_occurrences SET instance_cancelled = TRUE,
              instance_exception = %s, version = version + 1, updated_at = now()
             WHERE event_id = %s AND recurrence_id = %s
               AND instance_exception IS NULL
            """,
            (exception, event_id, recurrence_id),
        )


def _carry_over_exceptions(
    connection: Connection,
    event_id: UUID,
    desired: list[datetime],
    *,
    from_rid: datetime | None = None,
) -> None:
    """Keep single-date removals on their day when the series moves slots.

    Call after the new slots exist. An exception whose slot (at or after
    `from_rid`) the series no longer produces moves to that day's new slot
    when there is exactly one (a time-of-day change); otherwise it lapses,
    because the series itself no longer has that date.
    """
    wanted = set(desired)
    orphans = [
        _as_dict(item)
        for item in connection.execute(
            """
            SELECT id, recurrence_id, instance_exception FROM event_occurrences
             WHERE event_id = %s AND instance_exception IS NOT NULL
               AND (%s::timestamp IS NULL OR recurrence_id >= %s)
            """,
            (event_id, from_rid, from_rid),
        ).fetchall()
        if item["recurrence_id"] not in wanted
    ]
    if not orphans:
        return
    connection.execute(
        "UPDATE event_occurrences SET instance_exception = NULL WHERE id = ANY(%s)",
        ([item["id"] for item in orphans],),
    )
    for orphan, slot in match_exceptions_by_day(orphans, desired):
        _apply_exception(connection, event_id, slot, orphan["instance_exception"])


def _reconcile_occurrences(
    connection: Connection,
    event: dict,
    revision: dict,
    specs: list[OccurrenceSpec],
    window_start: date,
    window_end: date,
    now: datetime,
) -> None:
    previous_id = event["published_revision_id"] or revision["supersedes_revision_id"]
    prior_non_recurring = False
    if previous_id is not None:
        prior = connection.execute(
            """
            SELECT r.recurrence_rule,
                   EXISTS (
                     SELECT 1 FROM event_revision_recurrence_dates d
                      WHERE d.event_revision_id = r.id
                   ) AS has_dates
              FROM event_revisions r WHERE r.id = %s
            """,
            (previous_id,),
        ).fetchone()
        prior_non_recurring = (
            prior["recurrence_rule"] is None and not prior["has_dates"]
        )
    new_non_recurring = revision[
        "recurrence_rule"
    ] is None and not _load_recurrence_dates(connection, revision["id"])

    # A one-off event keeps its occurrence identity even when its time changes.
    desired_ids = [spec.recurrence_id for spec in specs]
    stable_non_recurring = False
    if prior_non_recurring and new_non_recurring and len(specs) == 1:
        existing = connection.execute(
            "SELECT id FROM event_occurrences WHERE event_id = %s FOR UPDATE",
            (event["id"],),
        ).fetchall()
        if len(existing) == 1:
            values = _occurrence_values(event["id"], revision["id"], specs[0])
            values["id"] = existing[0]["id"]
            connection.execute(
                """
                UPDATE event_occurrences SET
                  source_revision_id = %(source_revision_id)s,
                  recurrence_id = %(recurrence_id)s,
                  status = 'scheduled', version = version + 1,
                  is_exception = %(is_exception)s,
                  is_all_day = %(is_all_day)s, starts_at = %(starts_at)s,
                  ends_at = %(ends_at)s, start_date = %(start_date)s,
                  end_date = %(end_date)s, timezone = %(timezone)s,
                  cancellation_reason = NULL,
                  content_override = NULL, instance_cancelled = FALSE
                WHERE id = %(id)s
                """,
                values,
            )
            specs = []
            stable_non_recurring = True

    if new_non_recurring and not stable_non_recurring:
        # An event that no longer repeats has no dates to skip.
        connection.execute(
            """
            UPDATE event_occurrences SET instance_exception = NULL
             WHERE event_id = %s AND instance_exception IS NOT NULL
            """,
            (event["id"],),
        )
    if specs:
        _execute_many(
            connection,
            _OCCURRENCE_UPSERT,
            [_occurrence_values(event["id"], revision["id"], spec) for spec in specs],
        )
    if not stable_non_recurring:
        _carry_over_exceptions(connection, event["id"], desired_ids)

    # Removed future slots remain durable cancellation records. Past slots are
    # historical facts and are intentionally left untouched.
    if not stable_non_recurring:
        connection.execute(
            """
            UPDATE event_occurrences
               SET source_revision_id = %(revision_id)s,
                   status = 'cancelled', version = version + 1,
                   cancellation_reason = 'removed by approved revision'
             WHERE event_id = %(event_id)s
               AND status = 'scheduled'
               AND (%(desired_ids)s::timestamp[] = '{}'::timestamp[]
                    OR NOT (recurrence_id = ANY(%(desired_ids)s)))
               AND ((NOT is_all_day AND starts_at >= %(now)s)
                    OR (is_all_day AND start_date >= %(today)s))
            """,
            {
                "event_id": event["id"],
                "revision_id": revision["id"],
                "desired_ids": desired_ids,
                "now": now,
                "today": now.date(),
            },
        )

    connection.execute(
        """
        INSERT INTO event_occurrence_materializations (
          event_id, source_revision_id, window_start, window_end_exclusive
        ) VALUES (%s, %s, %s, %s)
        ON CONFLICT (event_id) DO UPDATE SET
          source_revision_id = EXCLUDED.source_revision_id,
          window_start = LEAST(
            event_occurrence_materializations.window_start, EXCLUDED.window_start
          ),
          window_end_exclusive = GREATEST(
            event_occurrence_materializations.window_end_exclusive,
            EXCLUDED.window_end_exclusive
          ),
          completed_at = now()
        """,
        (event["id"], revision["id"], window_start, window_end),
    )


def approve_revision(
    connection: Connection,
    event_id: UUID,
    revision_id: UUID,
    review: ReviewInput,
    *,
    past_days: int,
    future_months: int,
    now: datetime | None = None,
) -> dict:
    now = now or _now()
    event = connection.execute(
        """
        SELECT e.*, r.approval_status
          FROM events e
          JOIN event_revisions r ON r.id = %s AND r.event_id = e.id
         WHERE e.id = %s
         FOR UPDATE OF e, r
        """,
        (revision_id, event_id),
    ).fetchone()
    if event is None:
        raise NotFoundError("event revision not found")
    event = _as_dict(event)
    if event["current_revision_id"] != revision_id:
        raise ConflictError("only the event's current revision can be approved")
    if event["approval_status"] != "pending":
        raise ConflictError("revision is not pending")

    revision = connection.execute(
        "SELECT * FROM event_revisions WHERE id = %s", (revision_id,)
    ).fetchone()
    revision = _as_dict(revision)
    previous_window = connection.execute(
        "SELECT window_start, window_end_exclusive FROM event_occurrence_materializations WHERE event_id = %s",
        (event_id,),
    ).fetchone()
    window_start = now.date() - timedelta(days=past_days)
    window_end = now.date() + relativedelta(months=future_months)
    if previous_window:
        window_start = min(window_start, previous_window["window_start"])
        window_end = max(window_end, previous_window["window_end_exclusive"])

    recurrence_dates = _load_recurrence_dates(connection, revision_id)
    specs = expand_revision(revision, recurrence_dates, window_start, window_end)

    connection.execute(
        """
        UPDATE event_revisions
           SET approval_status = 'approved', reviewed_at = %s,
               reviewed_by = %s, review_note = %s
         WHERE id = %s
        """,
        (now, review.actor, review.note, revision_id),
    )
    _reconcile_occurrences(
        connection, event, revision, specs, window_start, window_end, now
    )
    connection.execute(
        "UPDATE events SET published_revision_id = %s WHERE id = %s",
        (revision_id, event_id),
    )
    action = connection.execute(
        """
        INSERT INTO event_review_actions
          (event_id, event_revision_id, action, actor, note, occurred_at)
        VALUES (%s, %s, 'approve', %s, %s, %s)
        RETURNING id, occurred_at
        """,
        (event_id, revision_id, review.actor, review.note, now),
    ).fetchone()
    return {
        "event_id": event_id,
        "revision_id": revision_id,
        "approval_status": "approved",
        "occurrence_count": len(specs),
        "review_action_id": action["id"],
        "reviewed_at": action["occurred_at"],
    }


def reject_revision(
    connection: Connection,
    event_id: UUID,
    revision_id: UUID,
    review: ReviewInput,
    *,
    now: datetime | None = None,
) -> dict:
    now = now or _now()
    row = connection.execute(
        """
        SELECT e.current_revision_id, r.approval_status
          FROM events e
          JOIN event_revisions r ON r.id = %s AND r.event_id = e.id
         WHERE e.id = %s
         FOR UPDATE OF e, r
        """,
        (revision_id, event_id),
    ).fetchone()
    if row is None:
        raise NotFoundError("event revision not found")
    if row["current_revision_id"] != revision_id:
        raise ConflictError("only the event's current revision can be rejected")
    if row["approval_status"] != "pending":
        raise ConflictError("revision is not pending")

    connection.execute(
        """
        UPDATE event_revisions
           SET approval_status = 'rejected', reviewed_at = %s,
               reviewed_by = %s, review_note = %s
         WHERE id = %s
        """,
        (now, review.actor, review.note, revision_id),
    )
    action = connection.execute(
        """
        INSERT INTO event_review_actions
          (event_id, event_revision_id, action, actor, note, occurred_at)
        VALUES (%s, %s, 'reject', %s, %s, %s)
        RETURNING id, occurred_at
        """,
        (event_id, revision_id, review.actor, review.note, now),
    ).fetchone()
    return {
        "event_id": event_id,
        "revision_id": revision_id,
        "approval_status": "rejected",
        "review_action_id": action["id"],
        "reviewed_at": action["occurred_at"],
    }


def revoke_event(
    connection: Connection,
    event_id: UUID,
    review: ReviewInput,
    *,
    now: datetime | None = None,
) -> dict:
    now = now or _now()
    event = connection.execute(
        "SELECT * FROM events WHERE id = %s FOR UPDATE", (event_id,)
    ).fetchone()
    if event is None:
        raise NotFoundError("event not found")
    revision_id = event["published_revision_id"]
    if revision_id is None:
        raise ConflictError("event is not published")

    connection.execute(
        "UPDATE events SET published_revision_id = NULL WHERE id = %s", (event_id,)
    )
    connection.execute(
        "UPDATE event_revisions SET approval_status = 'revoked' WHERE id = %s",
        (revision_id,),
    )
    cancelled = connection.execute(
        """
        UPDATE event_occurrences
           SET status = 'cancelled', version = version + 1,
               cancellation_reason = 'event revoked'
         WHERE event_id = %s AND status = 'scheduled'
           AND ((NOT is_all_day AND starts_at >= %s)
                OR (is_all_day AND start_date >= %s))
        RETURNING id
        """,
        (event_id, now, now.date()),
    ).fetchall()
    action = connection.execute(
        """
        INSERT INTO event_review_actions
          (event_id, event_revision_id, action, actor, note, occurred_at)
        VALUES (%s, %s, 'revoke', %s, %s, %s)
        RETURNING id, occurred_at
        """,
        (event_id, revision_id, review.actor, review.note, now),
    ).fetchone()
    return {
        "event_id": event_id,
        "revision_id": revision_id,
        "approval_status": "revoked",
        "cancelled_occurrence_count": len(cancelled),
        "review_action_id": action["id"],
        "reviewed_at": action["occurred_at"],
    }


def _load_removable_event(
    connection: Connection, event_id: UUID
) -> dict:
    """Load an event row for cancellation or deletion.

    Deleted events (archived) no longer exist, so they read as missing. The
    existence check comes before authorization so removals of missing events
    report 404 regardless of credentials.
    """
    event = connection.execute(
        """
        SELECT id, current_revision_id, published_revision_id, archived_at,
               cancelled_at, management_token_hash
          FROM events WHERE id = %s FOR UPDATE
        """,
        (event_id,),
    ).fetchone()
    if event is None or event["archived_at"] is not None:
        raise NotFoundError("event not found")
    return _as_dict(event)


def _require_removal_permission(
    event: dict, *, is_admin: bool, management_token: str | None
) -> None:
    if is_admin:
        return
    if not token_matches_digest(management_token, event["management_token_hash"]):
        raise UnauthorizedError(
            "the event creator's management token or admin credentials are required"
        )


def cancel_event(
    connection: Connection,
    event_id: UUID,
    *,
    actor: str,
    note: str | None = None,
    is_admin: bool = False,
    management_token: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Mark an event cancelled while keeping it visible.

    Cancellation means "this event is cancelled": the published content stays
    on the calendar and in the detail view, flagged as cancelled, because
    people may already have planned around it. Only an admin or the event
    creator (via the management token) may cancel.
    """
    now = now or _now()
    event = _load_removable_event(connection, event_id)
    _require_removal_permission(
        event, is_admin=is_admin, management_token=management_token
    )
    if event["cancelled_at"] is not None:
        raise ConflictError("event is already cancelled")

    connection.execute(
        """
        UPDATE events
           SET cancelled_at = %s, cancelled_by = %s, cancel_reason = %s
         WHERE id = %s
        """,
        (now, actor, note, event["id"]),
    )
    action = connection.execute(
        """
        INSERT INTO event_review_actions
          (event_id, event_revision_id, action, actor, note, occurred_at)
        VALUES (%s, %s, 'cancel', %s, %s, %s)
        RETURNING id, occurred_at
        """,
        (event["id"], event["current_revision_id"], actor, note, now),
    ).fetchone()
    return {
        "event_id": event["id"],
        "revision_id": event["current_revision_id"],
        "is_cancelled": True,
        "cancelled_at": now.isoformat(),
        "review_action_id": action["id"],
        "reviewed_at": action["occurred_at"],
    }


def delete_event(
    connection: Connection,
    event_id: UUID,
    *,
    actor: str,
    note: str | None = None,
    is_admin: bool = False,
    management_token: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Delete an event so it no longer exists.

    Deletion means "this event should no longer exist": the event leaves the
    calendar, the detail view, the review queue, and creator management reads.
    Future occurrences are cancelled for downstream history, matching revoke.
    Only an admin or the event creator (via the management token) may delete.
    """
    now = now or _now()
    event = _load_removable_event(connection, event_id)
    _require_removal_permission(
        event, is_admin=is_admin, management_token=management_token
    )

    connection.execute(
        "UPDATE events SET archived_at = %s WHERE id = %s", (now, event["id"])
    )
    cancelled = connection.execute(
        """
        UPDATE event_occurrences
           SET status = 'cancelled', version = version + 1,
               cancellation_reason = 'event deleted'
         WHERE event_id = %s AND status = 'scheduled'
           AND ((NOT is_all_day AND starts_at >= %s)
                OR (is_all_day AND start_date >= %s))
        RETURNING id
        """,
        (event["id"], now, now.date()),
    ).fetchall()
    action = connection.execute(
        """
        INSERT INTO event_review_actions
          (event_id, event_revision_id, action, actor, note, occurred_at)
        VALUES (%s, %s, 'delete', %s, %s, %s)
        RETURNING id, occurred_at
        """,
        (event["id"], event["current_revision_id"], actor, note, now),
    ).fetchone()
    return {
        "event_id": event["id"],
        "deleted": True,
        "archived_at": now.isoformat(),
        "cancelled_occurrence_count": len(cancelled),
        "review_action_id": action["id"],
        "reviewed_at": action["occurred_at"],
    }


def materialize_all(
    connection: Connection,
    *,
    past_days: int,
    future_months: int,
    now: datetime | None = None,
) -> dict[str, int]:
    now = now or _now()
    target_start = now.date() - timedelta(days=past_days)
    target_end = now.date() + relativedelta(months=future_months)
    event_ids = connection.execute(
        """
        SELECT e.id
          FROM events e
          LEFT JOIN event_occurrence_materializations m ON m.event_id = e.id
         WHERE e.published_revision_id IS NOT NULL
           AND (m.event_id IS NULL OR m.window_start > %s
                OR m.window_end_exclusive < %s)
         ORDER BY e.id
        """,
        (target_start, target_end),
    ).fetchall()
    inserted = 0
    processed = 0
    for id_row in event_ids:
        event = connection.execute(
            "SELECT * FROM events WHERE id = %s FOR UPDATE", (id_row["id"],)
        ).fetchone()
        if event is None or event["published_revision_id"] is None:
            continue
        revision = connection.execute(
            "SELECT * FROM event_revisions WHERE id = %s",
            (event["published_revision_id"],),
        ).fetchone()
        materialization = connection.execute(
            "SELECT * FROM event_occurrence_materializations WHERE event_id = %s",
            (event["id"],),
        ).fetchone()
        window_start = (
            min(target_start, materialization["window_start"])
            if materialization
            else target_start
        )
        window_end = (
            max(target_end, materialization["window_end_exclusive"])
            if materialization
            else target_end
        )
        specs = expand_revision(
            revision,
            _load_recurrence_dates(connection, revision["id"]),
            window_start,
            window_end,
        )
        before = connection.execute(
            "SELECT count(*) AS count FROM event_occurrences WHERE event_id = %s",
            (event["id"],),
        ).fetchone()["count"]
        _execute_many(
            connection,
            _OCCURRENCE_INSERT_IGNORE,
            [_occurrence_values(event["id"], revision["id"], spec) for spec in specs],
        )
        after = connection.execute(
            "SELECT count(*) AS count FROM event_occurrences WHERE event_id = %s",
            (event["id"],),
        ).fetchone()["count"]
        inserted += after - before
        processed += 1
        connection.execute(
            """
            INSERT INTO event_occurrence_materializations
              (event_id, source_revision_id, window_start, window_end_exclusive)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (event_id) DO UPDATE SET
              window_start = EXCLUDED.window_start,
              window_end_exclusive = EXCLUDED.window_end_exclusive,
              source_revision_id = EXCLUDED.source_revision_id,
              completed_at = now()
            """,
            (event["id"], revision["id"], window_start, window_end),
        )
    return {"events_processed": processed, "occurrences_inserted": inserted}


def calendar_occurrences(
    connection: Connection,
    *,
    start_at: datetime,
    end_at: datetime,
    start_date: date,
    end_date: date,
    group_slugs: list[str],
    limit: int,
    offset: int,
) -> tuple[list[dict], bool]:
    if group_slugs:
        found = connection.execute(
            "SELECT slug FROM groups WHERE slug = ANY(%s) AND is_active",
            (group_slugs,),
        ).fetchall()
        found_slugs = {row["slug"] for row in found}
        missing = sorted(set(group_slugs) - found_slugs)
        if missing:
            raise ValidationError(
                f"unknown or inactive group slug(s): {', '.join(missing)}"
            )

    rows = connection.execute(
        """
        SELECT
          o.id AS occurrence_id, o.event_id, o.version, o.is_exception,
          o.is_all_day, o.starts_at, o.ends_at, o.start_date, o.end_date,
          o.timezone, o.content_override, o.instance_cancelled,
          e.cancelled_at IS NOT NULL AS is_cancelled,
          r.id AS revision_id, r.title, r.description, r.location_name,
          r.location_address, r.event_url, r.recurrence_rule,
          grouped.groups
        FROM event_occurrences o
        JOIN events e ON e.id = o.event_id
        JOIN event_revisions r ON r.id = e.published_revision_id
        CROSS JOIN LATERAL (
          SELECT COALESCE(jsonb_agg(
                   jsonb_build_object('id', g.id, 'slug', g.slug, 'name', g.name)
                   ORDER BY g.name, g.id
                 ), '[]'::jsonb) AS groups
            FROM event_revision_groups rg
            JOIN groups g ON g.id = rg.group_id
           WHERE rg.event_revision_id = r.id
             AND g.is_active
        ) grouped
        WHERE e.archived_at IS NULL
          AND r.approval_status = 'approved'
          AND o.status = 'scheduled'
          AND (
            NOT EXISTS (
              SELECT 1 FROM event_revision_groups assigned_rg
               WHERE assigned_rg.event_revision_id = r.id
            )
            OR EXISTS (
              SELECT 1
                FROM event_revision_groups visible_rg
                JOIN groups visible_g ON visible_g.id = visible_rg.group_id
               WHERE visible_rg.event_revision_id = r.id
                 AND visible_g.is_active
            )
          )
          AND (
            (NOT o.is_all_day AND o.starts_at < %(end_at)s
                              AND o.ends_at > %(start_at)s)
            OR
            (o.is_all_day AND o.start_date < %(end_date)s
                          AND o.end_date > %(start_date)s)
          )
          AND (
            %(group_slugs)s::text[] = '{}'::text[]
            OR EXISTS (
              SELECT 1
                FROM event_revision_groups filter_rg
                JOIN groups filter_g ON filter_g.id = filter_rg.group_id
               WHERE filter_rg.event_revision_id = r.id
                 AND filter_g.slug = ANY(%(group_slugs)s)
                 AND filter_g.is_active
            )
          )
        ORDER BY
          COALESCE(o.starts_at, o.start_date::timestamp AT TIME ZONE o.timezone),
          o.event_id, o.id
        LIMIT %(fetch_limit)s
        OFFSET %(offset)s
        """,
        {
            "start_at": start_at,
            "end_at": end_at,
            "start_date": start_date,
            "end_date": end_date,
            "group_slugs": group_slugs,
            "fetch_limit": limit + 1,
            "offset": offset,
        },
    ).fetchall()
    has_more = len(rows) > limit
    items = []
    for row in rows[:limit]:
        item = _as_dict(row)
        # A scoped per-date cancellation flags the date while the series
        # itself stays published; a series-wide cancellation flags every date.
        item["is_cancelled"] = bool(item.get("is_cancelled")) or bool(
            item.pop("instance_cancelled", False)
        )
        # A diverged date carries its own content merged over the series.
        apply_content_override(item, item.pop("content_override", None))
        items.append(item)
    return items, has_more


_SERIES_UPCOMING_LIMIT = 6
_SERIES_SKIPPED_LIMIT = 20
_OCCURRENCE_FIELDS = """
    id AS occurrence_id, recurrence_id, is_exception, is_all_day, starts_at,
    ends_at, start_date, end_date, timezone, content_override,
    instance_cancelled, instance_exception
"""
_OCCURRENCE_KEY = "COALESCE(starts_at, start_date::timestamp AT TIME ZONE timezone)"


def _series_context(
    connection: Connection,
    event_id: UUID,
    selected: dict | None,
    now: datetime,
) -> dict:
    """Neighbouring, upcoming, and skipped dates of a recurring event.

    Only materialized occurrences are visible, so counts and lists are bounded
    by the rolling window that ends at `coverage_end`. `skipped` lists upcoming
    dates deleted on their own, which editors can restore.
    """
    previous = following = None
    if selected is not None:
        params = {"event_id": event_id, "occurrence_id": selected["occurrence_id"]}
        anchor = f"""
            (SELECT {_OCCURRENCE_KEY}, id FROM event_occurrences
              WHERE id = %(occurrence_id)s)
        """
        previous = connection.execute(
            f"""
            SELECT {_OCCURRENCE_FIELDS} FROM event_occurrences
             WHERE event_id = %(event_id)s AND status = 'scheduled'
               AND ({_OCCURRENCE_KEY}, id) < {anchor}
             ORDER BY {_OCCURRENCE_KEY} DESC, id DESC LIMIT 1
            """,
            params,
        ).fetchone()
        following = connection.execute(
            f"""
            SELECT {_OCCURRENCE_FIELDS} FROM event_occurrences
             WHERE event_id = %(event_id)s AND status = 'scheduled'
               AND ({_OCCURRENCE_KEY}, id) > {anchor}
             ORDER BY {_OCCURRENCE_KEY}, id LIMIT 1
            """,
            params,
        ).fetchone()
    not_ended = """
        event_id = %(event_id)s AND status = 'scheduled'
        AND ((NOT is_all_day AND ends_at > %(now)s)
          OR (is_all_day AND end_date > %(today)s))
    """
    not_ended_params = {"event_id": event_id, "now": now, "today": now.date()}
    upcoming = connection.execute(
        f"""
        SELECT {_OCCURRENCE_FIELDS} FROM event_occurrences
         WHERE {not_ended} ORDER BY {_OCCURRENCE_KEY}, id LIMIT %(limit)s
        """,
        {**not_ended_params, "limit": _SERIES_UPCOMING_LIMIT},
    ).fetchall()
    upcoming_count = connection.execute(
        f"SELECT count(*) AS total FROM event_occurrences WHERE {not_ended}",
        not_ended_params,
    ).fetchone()["total"]
    skipped = connection.execute(
        f"""
        SELECT {_OCCURRENCE_FIELDS} FROM event_occurrences
         WHERE event_id = %(event_id)s AND status = 'cancelled'
           AND instance_exception = %(skipped)s
           AND ((NOT is_all_day AND ends_at > %(now)s)
             OR (is_all_day AND end_date > %(today)s))
         ORDER BY {_OCCURRENCE_KEY}, id LIMIT %(limit)s
        """,
        {
            **not_ended_params,
            "skipped": EXCEPTION_SKIPPED,
            "limit": _SERIES_SKIPPED_LIMIT,
        },
    ).fetchall()
    coverage = connection.execute(
        """
        SELECT window_end_exclusive FROM event_occurrence_materializations
         WHERE event_id = %s
        """,
        (event_id,),
    ).fetchone()

    def _public(row: Any) -> dict | None:
        # Series neighbours carry timing plus divergence/cancel flags; the
        # raw override blob stays server-side.
        if row is None:
            return None
        item = _as_dict(row)
        item["is_cancelled"] = bool(item.pop("instance_cancelled", False))
        item["has_override"] = bool(
            parse_content_override(item.pop("content_override", None))
        )
        return item

    return {
        "previous": _public(previous),
        "next": _public(following),
        "upcoming": [_public(item) for item in upcoming],
        "upcoming_count": upcoming_count,
        "skipped": [_public(item) for item in skipped],
        "coverage_end": coverage["window_end_exclusive"] if coverage else None,
    }


def get_published_event(
    connection: Connection,
    event_id: UUID,
    *,
    occurrence_id: UUID | None = None,
    now: datetime | None = None,
) -> dict:
    """Return a published event with its occurrence and series context.

    `occurrence` is the requested scheduled date, or null when none was asked
    for or it is no longer scheduled. `series` is null for one-off events.
    """
    now = now or _now()
    row = connection.execute(
        """
        SELECT p.*,
               e.cancelled_at AS cancelled_at,
               e.cancel_reason AS cancel_reason,
               (SELECT COALESCE(jsonb_agg(
                         jsonb_build_object('id', g.id, 'slug', g.slug, 'name', g.name)
                         ORDER BY g.name, g.id
                       ), '[]'::jsonb)
                  FROM event_revision_groups rg
                  JOIN groups g ON g.id = rg.group_id
                 WHERE rg.event_revision_id = p.event_revision_id
                   AND g.is_active) AS groups
          FROM published_events p
          JOIN events e ON e.id = p.event_id
         WHERE p.event_id = %s
           AND (
             NOT EXISTS (
               SELECT 1 FROM event_revision_groups assigned_rg
                WHERE assigned_rg.event_revision_id = p.event_revision_id
             )
             OR EXISTS (
               SELECT 1
                 FROM event_revision_groups visible_rg
                 JOIN groups visible_g ON visible_g.id = visible_rg.group_id
                WHERE visible_rg.event_revision_id = p.event_revision_id
                  AND visible_g.is_active
             )
           )
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("published event not found")
    result = _as_dict(row)
    result["is_cancelled"] = result.get("cancelled_at") is not None
    result["recurrence_dates"] = _load_recurrence_dates(
        connection, result["event_revision_id"]
    )
    selected = None
    if occurrence_id is not None:
        found = connection.execute(
            f"""
            SELECT {_OCCURRENCE_FIELDS} FROM event_occurrences
             WHERE id = %s AND event_id = %s AND status = 'scheduled'
            """,
            (occurrence_id, event_id),
        ).fetchone()
        selected = _as_dict(found) if found else None
        if selected is not None:
            # The selected date shows its own content when it diverges from
            # the series (scoped single/future edit), and its own cancelled
            # flag alongside the event-level one.
            selected.update(
                {
                    field: result.get(field)
                    for field in (
                        "title",
                        "description",
                        "location_name",
                        "location_address",
                        "event_url",
                    )
                }
            )
            apply_content_override(
                selected, selected.pop("content_override", None)
            )
            selected["is_cancelled"] = bool(
                result.get("is_cancelled")
            ) or bool(selected.pop("instance_cancelled", False))
    result["occurrence"] = selected
    is_recurring = bool(result["recurrence_rule"]) or any(
        item["kind"] == "include" for item in result["recurrence_dates"]
    )
    result["series"] = (
        _series_context(connection, event_id, selected, now)
        if is_recurring
        else None
    )
    return result


def review_queue(
    connection: Connection, *, status: str, limit: int, offset: int
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT e.id AS event_id, e.published_revision_id,
               e.cancelled_at IS NOT NULL AS is_cancelled,
               r.id AS revision_id, r.revision_number, r.approval_status,
               r.title, r.description, r.is_all_day, r.starts_at, r.ends_at,
               r.start_date, r.end_date, r.timezone, r.recurrence_rule,
               r.submitted_by_name, r.submitted_by_channel,
               r.submitted_by_contact, r.submitted_at, r.reviewed_at,
               r.reviewed_by, r.review_note,
               (SELECT COALESCE(jsonb_agg(
                         jsonb_build_object('id', g.id, 'slug', g.slug, 'name', g.name)
                         ORDER BY g.name, g.id
                       ), '[]'::jsonb)
                  FROM event_revision_groups rg
                  JOIN groups g ON g.id = rg.group_id
                 WHERE rg.event_revision_id = r.id) AS groups
          FROM events e
          JOIN event_revisions r ON r.id = e.current_revision_id
         WHERE e.archived_at IS NULL AND r.approval_status = %s
         ORDER BY r.submitted_at, e.id
         LIMIT %s OFFSET %s
        """,
        (status, limit, offset),
    ).fetchall()
    items = []
    for row in rows:
        item = _as_dict(row)
        item["is_cancelled"] = bool(item.get("is_cancelled"))
        items.append(item)
    return items


def get_admin_event(connection: Connection, event_id: UUID) -> dict:
    event = connection.execute(
        """
        SELECT e.id AS event_id, e.current_revision_id, e.published_revision_id,
               current_revision.approval_status,
               current_revision.revision_number AS current_revision_number,
               e.original_submitter_name, e.original_submitter_channel,
               e.original_submitter_contact, e.submitted_at, e.archived_at,
               e.cancelled_at, e.cancelled_by, e.cancel_reason, e.updated_at
          FROM events e
          JOIN event_revisions current_revision ON current_revision.id = e.current_revision_id
         WHERE e.id = %s
        """,
        (event_id,),
    ).fetchone()
    if event is None or event["archived_at"] is not None:
        raise NotFoundError("event not found")
    revisions = connection.execute(
        """
        SELECT r.*,
               (SELECT COALESCE(jsonb_agg(
                         jsonb_build_object('id', g.id, 'slug', g.slug, 'name', g.name)
                         ORDER BY g.name, g.id
                       ), '[]'::jsonb)
                  FROM event_revision_groups rg
                  JOIN groups g ON g.id = rg.group_id
                 WHERE rg.event_revision_id = r.id) AS groups,
               (SELECT COALESCE(jsonb_agg(
                         jsonb_build_object('local_start', d.local_start, 'kind', d.kind)
                         ORDER BY d.local_start
                       ), '[]'::jsonb)
                  FROM event_revision_recurrence_dates d
                 WHERE d.event_revision_id = r.id) AS recurrence_dates
          FROM event_revisions r
         WHERE r.event_id = %s
         ORDER BY r.revision_number DESC
        """,
        (event_id,),
    ).fetchall()
    result = _as_dict(event)
    result["revisions"] = [_as_dict(row) for row in revisions]
    return result


# ---------------------------------------------------------------------------
# Scoped single/future edits and removals of recurring events.
#
# A scoped edit or removal targets one materialized occurrence (by its
# occurrence id) and applies to that date only ("single") or to that date and
# every later one ("future"). The whole-series behaviour ("series") stays on
# the existing revision/removal paths.
#
# Single-occurrence edits diverge one row: timing columns are updated in
# place (recurrence_id keeps its stable slot identity and is_exception marks
# the rescheduling) and descriptive content is stored as a JSON override that
# readers merge over the published revision. Future edits and truncations
# create a new approved revision and reconcile only occurrences at or after
# the target slot, leaving earlier rows untouched. Every series-wide approval
# refreshes retained rows and clears per-occurrence divergence, so the latest
# change to the series always persists over earlier exceptions.


def _scoped_event(connection: Connection, event_id: UUID) -> dict:
    """Load an event for a scoped operation (deleted events read as missing)."""
    row = connection.execute(
        "SELECT * FROM events WHERE id = %s FOR UPDATE", (event_id,)
    ).fetchone()
    if row is None or row["archived_at"] is not None:
        raise NotFoundError("event not found")
    return _as_dict(row)


def _published_for_scope(
    connection: Connection, event: dict
) -> tuple[dict, list[dict]]:
    """Load the published revision a scoped operation applies on top of."""
    if event["published_revision_id"] is None:
        raise NotFoundError("event is not published")
    revision = _as_dict(
        connection.execute(
            "SELECT * FROM event_revisions WHERE id = %s",
            (event["published_revision_id"],),
        ).fetchone()
    )
    return revision, _load_recurrence_dates(connection, event["published_revision_id"])


def _require_no_pending(connection: Connection, event: dict) -> dict:
    """Refuse revision-minting scoped ops while an edit awaits review.

    "Future" operations create a new approved revision; running one on top of
    a pending edit would strand that edit off the current chain, so callers
    must approve or reject it first. Single-date operations touch only one
    occurrence row and stay compatible with a later approval (which then wins,
    per the latest-change rule).
    """
    current = connection.execute(
        "SELECT revision_number, approval_status FROM event_revisions WHERE id = %s",
        (event["current_revision_id"],),
    ).fetchone()
    if current is not None and current["approval_status"] == "pending":
        raise ConflictError(
            "event has a pending edit awaiting review; approve or reject it "
            "before changing future dates"
        )
    return _as_dict(current)


def _scope_target(
    connection: Connection, event_id: UUID, occurrence_id: UUID
) -> dict:
    """Load the targeted occurrence; gone dates read as missing."""
    row = connection.execute(
        """
        SELECT * FROM event_occurrences
         WHERE id = %s AND event_id = %s FOR UPDATE
        """,
        (occurrence_id, event_id),
    ).fetchone()
    if row is None or row["status"] != "scheduled":
        raise NotFoundError("occurrence not found or no longer scheduled")
    return _as_dict(row)


def _require_recurring(revision: dict, dates: list[dict]) -> None:
    if not revision["recurrence_rule"] and not any(
        item["kind"] == "include" for item in dates
    ):
        raise ValidationError(
            "event does not repeat; choose the entire series for one-off events"
        )


def _published_group_ids(connection: Connection, revision_id: UUID) -> set[UUID]:
    rows = connection.execute(
        "SELECT group_id FROM event_revision_groups WHERE event_revision_id = %s",
        (revision_id,),
    ).fetchall()
    return {item["group_id"] for item in rows}


def _check_scoped_recurrence(
    connection: Connection,
    payload: EventRevisionInput,
    revision: dict,
    dates: list[dict],
    revision_id: UUID,
    *,
    allow_group_changes: bool,
) -> None:
    """Enforce what a scoped payload may and may not change.

    Scoped payloads never carry a new repeat pattern: any recurrence they
    contain is ignored and the server reuses (or, for removals, truncates)
    the published rule, so an echoed-back schedule can never read as an
    attempt to change the pattern. Content and timing may change, and — for
    "future" — groups; group changes on a single date belong to a
    series-wide edit.
    """
    if not allow_group_changes and set(payload.group_ids) != _published_group_ids(
        connection, revision_id
    ):
        raise ValidationError(
            "changing groups applies to the entire series; "
            "edit the series instead of a single date"
        )


def _log_scope_action(
    connection: Connection,
    event_id: UUID,
    revision_id: UUID,
    action: str,
    actor: str,
    note: str | None,
    now: datetime | None = None,
) -> dict:
    return _as_dict(
        connection.execute(
            """
            INSERT INTO event_review_actions
              (event_id, event_revision_id, action, actor, note, occurred_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, occurred_at
            """,
            (event_id, revision_id, action, actor, note, now or _now()),
        ).fetchone()
    )


def edit_single_occurrence(
    connection: Connection,
    event_id: UUID,
    occurrence_id: UUID,
    payload: EventRevisionInput,
    *,
    actor: str,
    note: str | None = None,
) -> dict:
    """Edit one occurrence without changing the rest of its series.

    Timing is rewritten in place (the stable recurrence_id still identifies
    the original slot) and divergent descriptive content is stored as an
    override. A date cancelled on its own stays cancelled. A later
    series-wide edit refreshes and clears the override, so the latest series
    change always wins. Applies immediately for any authorized
    editor (admin or the event creator), like cancellation and deletion.
    """
    event = _scoped_event(connection, event_id)
    revision, dates = _published_for_scope(connection, event)
    _require_recurring(revision, dates)
    target = _scope_target(connection, event["id"], occurrence_id)
    _check_scoped_recurrence(
        connection,
        payload,
        revision,
        dates,
        revision["id"],
        allow_group_changes=False,
    )
    override = build_content_override(payload, revision)
    connection.execute(
        """
        UPDATE event_occurrences SET
          source_revision_id = %(revision_id)s, is_exception = TRUE,
          is_all_day = %(is_all_day)s,
          starts_at = %(starts_at)s, ends_at = %(ends_at)s,
          start_date = %(start_date)s, end_date = %(end_date)s,
          timezone = %(timezone)s, content_override = %(override)s,
          instance_cancelled =
            instance_exception IS NOT DISTINCT FROM 'cancelled',
          version = version + 1, updated_at = now()
         WHERE id = %(id)s
        """,
        {
            "revision_id": revision["id"],
            "is_all_day": payload.is_all_day,
            "starts_at": payload.starts_at,
            "ends_at": payload.ends_at,
            "start_date": payload.start_date,
            "end_date": payload.end_date,
            "timezone": payload.timezone,
            "override": override,
            "id": target["id"],
        },
    )
    return {
        "event_id": event["id"],
        "occurrence_id": target["id"],
        "scope": "single",
        "version": target["version"] + 1,
        "has_override": override is not None,
        "updated_at": _now().isoformat(),
    }


def _validate_synth_revision(values: dict) -> EventRevisionInput:
    """Validate a server-synthesized revision with the public input rules."""
    from pydantic import ValidationError as PydanticValidationError

    try:
        return EventRevisionInput.model_validate(values)
    except PydanticValidationError as exc:
        raise ValidationError(str(exc)) from exc


def _synth_values(
    *,
    title: str,
    description: str,
    location_name: str | None,
    location_address: str | None,
    event_url: str | None,
    is_all_day: bool,
    starts_at: datetime | None,
    ends_at: datetime | None,
    start_date: date | None,
    end_date: date | None,
    timezone: str,
    recurrence_rule: str | None,
    recurrence_dates: list[dict],
    group_ids: list[Any],
    submitter_name: str,
    submitter_channel: str,
    submitter_contact: str,
) -> dict:
    return {
        "title": title,
        "description": description,
        "location_name": location_name,
        "location_address": location_address,
        "event_url": event_url,
        "is_all_day": is_all_day,
        "starts_at": starts_at.isoformat() if starts_at else None,
        "ends_at": ends_at.isoformat() if ends_at else None,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "timezone": timezone,
        "recurrence_rule": recurrence_rule,
        "recurrence_dates": [
            {"local_start": item["local_start"], "kind": item["kind"]}
            for item in recurrence_dates
        ],
        "group_ids": group_ids,
        "submitter": {
            "name": submitter_name,
            "channel": submitter_channel,
            "contact": submitter_contact,
        },
    }


def _insert_approved_revision(
    connection: Connection,
    event_id: UUID,
    revision_number: int,
    supersedes_revision_id: UUID,
    validated: EventRevisionInput,
    *,
    actor: str,
    note: str | None,
    now: datetime,
) -> dict:
    contact = normalize_contact(
        validated.submitter.channel, validated.submitter.contact
    )
    revision = connection.execute(
        """
        INSERT INTO event_revisions (
          event_id, revision_number, supersedes_revision_id,
          approval_status, title, description, location_name, location_address,
          event_url, is_all_day, starts_at, ends_at, start_date, end_date,
          timezone, recurrence_rule, submitted_by_name, submitted_by_channel,
          submitted_by_contact, submitted_at, reviewed_at, reviewed_by,
          review_note
        ) VALUES (
          %(event_id)s, %(revision_number)s, %(supersedes_revision_id)s,
          'approved', %(title)s, %(description)s, %(location_name)s,
          %(location_address)s, %(event_url)s, %(is_all_day)s, %(starts_at)s,
          %(ends_at)s, %(start_date)s, %(end_date)s, %(timezone)s,
          %(recurrence_rule)s, %(submitted_by_name)s, %(submitted_by_channel)s,
          %(submitted_by_contact)s, %(now)s, %(now)s, %(actor)s, %(note)s
        )
        RETURNING *
        """,
        {
            "event_id": event_id,
            "revision_number": revision_number,
            "supersedes_revision_id": supersedes_revision_id,
            "title": validated.title,
            "description": validated.description,
            "location_name": validated.location_name or None,
            "location_address": validated.location_address or None,
            "event_url": validated.event_url or None,
            "is_all_day": validated.is_all_day,
            "starts_at": validated.starts_at,
            "ends_at": validated.ends_at,
            "start_date": validated.start_date,
            "end_date": validated.end_date,
            "timezone": validated.timezone,
            "recurrence_rule": validated.recurrence_rule,
            "submitted_by_name": validated.submitter.name,
            "submitted_by_channel": validated.submitter.channel,
            "submitted_by_contact": contact,
            "now": now,
            "actor": actor,
            "note": note,
        },
    ).fetchone()
    _execute_many(
        connection,
        "INSERT INTO event_revision_groups (event_revision_id, group_id) VALUES (%s, %s)",
        [(revision["id"], group_id) for group_id in validated.group_ids],
    )
    if validated.recurrence_dates:
        _execute_many(
            connection,
            """
            INSERT INTO event_revision_recurrence_dates
              (event_revision_id, local_start, kind)
            VALUES (%s, %s, %s)
            """,
            [
                (revision["id"], item.local_start, item.kind)
                for item in validated.recurrence_dates
            ],
        )
    return _as_dict(revision)


def _approve_scoped_revision(
    connection: Connection,
    event: dict,
    revision: dict,
    validated: EventRevisionInput,
    *,
    scope_rid: datetime,
    mode: str,
    removal_reason: str,
    past_days: int,
    future_months: int,
    now: datetime,
) -> tuple[list[OccurrenceSpec], int]:
    """Expand a scoped revision and reconcile only its target range.

    Occurrences on or after `scope_rid` are refreshed from the new revision
    (clearing earlier per-date edits but keeping single-date cancellations
    and skips); scheduled rows in range that the
    new definition no longer produces are removed according to `mode`:
    "refresh" cancels them (edited away), "remove" cancels them as deleted,
    and "mark" keeps them visible, flagged as cancelled.
    """
    previous_window = connection.execute(
        "SELECT window_start, window_end_exclusive FROM event_occurrence_materializations WHERE event_id = %s",
        (event["id"],),
    ).fetchone()
    window_start = now.date() - timedelta(days=past_days)
    window_end = now.date() + relativedelta(months=future_months)
    if previous_window:
        window_start = min(window_start, previous_window["window_start"])
        window_end = max(window_end, previous_window["window_end_exclusive"])
    expand_dates = [
        {"local_start": item.local_start, "kind": item.kind}
        for item in validated.recurrence_dates
    ]
    specs = expand_revision(revision, expand_dates, window_start, window_end)

    in_scope = [spec for spec in specs if spec.recurrence_id >= scope_rid]
    if in_scope:
        _execute_many(
            connection,
            _OCCURRENCE_UPSERT,
            [_occurrence_values(event["id"], revision["id"], spec) for spec in in_scope],
        )
    desired = [spec.recurrence_id for spec in in_scope]
    _carry_over_exceptions(connection, event["id"], desired, from_rid=scope_rid)
    if mode == "mark":
        cursor = connection.execute(
            """
            UPDATE event_occurrences
               SET instance_cancelled = TRUE, version = version + 1,
                   updated_at = now()
             WHERE event_id = %(event_id)s
               AND status = 'scheduled'
               AND recurrence_id >= %(scope_rid)s
               AND (%(desired)s::timestamp[] = '{}'::timestamp[]
                    OR NOT (recurrence_id = ANY(%(desired)s)))
            """,
            {"event_id": event["id"], "scope_rid": scope_rid, "desired": desired},
        )
    else:
        cursor = connection.execute(
            """
            UPDATE event_occurrences
               SET source_revision_id = %(revision_id)s,
                   status = 'cancelled', version = version + 1,
                   cancellation_reason = %(reason)s, updated_at = now()
             WHERE event_id = %(event_id)s
               AND status = 'scheduled'
               AND recurrence_id >= %(scope_rid)s
               AND (%(desired)s::timestamp[] = '{}'::timestamp[]
                    OR NOT (recurrence_id = ANY(%(desired)s)))
            """,
            {
                "event_id": event["id"],
                "revision_id": revision["id"],
                "reason": removal_reason,
                "scope_rid": scope_rid,
                "desired": desired,
            },
        )
    affected = cursor.rowcount or 0
    connection.execute(
        """
        INSERT INTO event_occurrence_materializations (
          event_id, source_revision_id, window_start, window_end_exclusive
        ) VALUES (%s, %s, %s, %s)
        ON CONFLICT (event_id) DO UPDATE SET
          source_revision_id = EXCLUDED.source_revision_id,
          window_start = LEAST(
            event_occurrence_materializations.window_start, EXCLUDED.window_start
          ),
          window_end_exclusive = GREATEST(
            event_occurrence_materializations.window_end_exclusive,
            EXCLUDED.window_end_exclusive
          ),
          completed_at = now()
        """,
        (event["id"], revision["id"], window_start, window_end),
    )
    return in_scope, affected


def _commit_scoped_revision(
    connection: Connection,
    event: dict,
    validated: EventRevisionInput,
    *,
    scope_rid: datetime,
    mode: str,
    removal_reason: str,
    actor: str,
    note: str | None,
    past_days: int,
    future_months: int,
    now: datetime,
) -> dict:
    """Insert, point at, and reconcile a scoped approved revision."""
    current = connection.execute(
        "SELECT revision_number FROM event_revisions WHERE id = %s",
        (event["current_revision_id"],),
    ).fetchone()
    revision = _insert_approved_revision(
        connection,
        event["id"],
        current["revision_number"] + 1,
        event["current_revision_id"],
        validated,
        actor=actor,
        note=note,
        now=now,
    )
    connection.execute(
        "UPDATE events SET current_revision_id = %s, published_revision_id = %s WHERE id = %s",
        (revision["id"], revision["id"], event["id"]),
    )
    in_scope, affected = _approve_scoped_revision(
        connection,
        event,
        revision,
        validated,
        scope_rid=scope_rid,
        mode=mode,
        removal_reason=removal_reason,
        past_days=past_days,
        future_months=future_months,
        now=now,
    )
    action = _log_scope_action(
        connection, event["id"], revision["id"], "approve", actor, note, now
    )
    return {
        "revision": revision,
        "in_scope": in_scope,
        "affected": affected,
        "review_action_id": action["id"],
        "reviewed_at": action["occurred_at"],
    }


def _pin_past_content(
    connection: Connection,
    event_id: UUID,
    old_revision: dict,
    new_revision: EventRevisionInput,
    scope_rid: datetime,
) -> int:
    """Freeze changed content onto past dates after a future edit.

    Earlier rows are never reconciled (their timing stays as materialized),
    but their descriptive content renders from the published revision — which
    just changed. Writing the old values as overrides keeps past dates showing
    what viewers saw, limited to fields that actually changed. Explicit
    per-date overrides win over the frozen values. A later series-wide edit
    clears these pins, so the latest series change always wins.
    """
    frozen = {}
    for field in CONTENT_OVERRIDE_FIELDS:
        old_value = old_revision.get(field) or None
        new_value = getattr(new_revision, field, None) or None
        if old_value != new_value:
            frozen[field] = old_value
    if not frozen:
        return 0
    pinned = 0
    rows = connection.execute(
        """
        SELECT id, content_override FROM event_occurrences
         WHERE event_id = %s AND status = 'scheduled' AND recurrence_id < %s
        """,
        (event_id, scope_rid),
    ).fetchall()
    for item in rows:
        existing = parse_content_override(item["content_override"])
        merged = {**frozen, **existing}
        if merged == existing:
            continue
        connection.execute(
            """
            UPDATE event_occurrences SET content_override = %s,
              version = version + 1, updated_at = now() WHERE id = %s
            """,
            (json.dumps(merged, sort_keys=True), item["id"]),
        )
        pinned += 1
    return pinned


def edit_future_occurrences(
    connection: Connection,
    event_id: UUID,
    occurrence_id: UUID,
    payload: EventRevisionInput,
    *,
    actor: str,
    note: str | None = None,
    past_days: int,
    future_months: int,
    now: datetime | None = None,
) -> dict:
    """Edit one occurrence and every later one, leaving earlier dates alone.

    The published pattern is kept: content, timing, and groups move forward
    from the target date while earlier rows keep their materialized values.
    A later series-wide edit refreshes every row, so the latest series change
    always wins. Applies immediately for any authorized editor (admin or the
    event creator), like cancellation and deletion.
    """
    now = now or _now()
    event = _scoped_event(connection, event_id)
    _require_no_pending(connection, event)
    revision, dates = _published_for_scope(connection, event)
    _require_recurring(revision, dates)
    if payload.is_all_day != bool(revision["is_all_day"]):
        raise ValidationError(
            "changing between timed and all-day applies to the entire series; "
            "edit the series instead of future dates"
        )
    if payload.timezone != revision["timezone"]:
        raise ValidationError(
            "changing the timezone applies to the entire series; "
            "edit the series instead of future dates"
        )
    target = _scope_target(connection, event["id"], occurrence_id)
    _require_active_groups(connection, payload.group_ids)
    _check_scoped_recurrence(
        connection,
        payload,
        revision,
        dates,
        revision["id"],
        allow_group_changes=True,
    )
    scope_rid = target["recurrence_id"]
    delta = _wall_delta(revision, target, payload)
    if payload.is_all_day:
        new_start_date = revision["start_date"] + delta
        new_end_date = revision["end_date"] + delta
        new_starts_at = new_ends_at = None
    else:
        zone = ZoneInfo(revision["timezone"])
        series_wall = revision["starts_at"].astimezone(zone).replace(
            tzinfo=None, microsecond=0
        )
        new_start_utc = reattach_wall_time(
            series_wall + delta, revision["timezone"]
        ).astimezone(UTC)
        duration = payload.ends_at - payload.starts_at
        new_starts_at, new_ends_at = new_start_utc, new_start_utc + duration
        new_start_date = new_end_date = None
    if delta == timedelta(0):
        new_dates = [
            {"local_start": item["local_start"], "kind": item["kind"]}
            for item in dates
        ]
    else:
        # A rescheduled future drops future added/skipped dates: they name
        # slots of the old pattern that no longer occur.
        new_dates = [
            {"local_start": item["local_start"], "kind": item["kind"]}
            for item in dates
            if item["local_start"] < scope_rid
        ]
    validated = _validate_synth_revision(
        _synth_values(
            title=payload.title,
            description=payload.description,
            location_name=payload.location_name,
            location_address=payload.location_address,
            event_url=payload.event_url,
            is_all_day=payload.is_all_day,
            starts_at=new_starts_at,
            ends_at=new_ends_at,
            start_date=new_start_date,
            end_date=new_end_date,
            timezone=revision["timezone"],
            recurrence_rule=revision["recurrence_rule"],
            recurrence_dates=new_dates,
            group_ids=list(payload.group_ids),
            submitter_name=payload.submitter.name,
            submitter_channel=payload.submitter.channel,
            submitter_contact=payload.submitter.contact,
        ),
    )
    committed = _commit_scoped_revision(
        connection,
        event,
        validated,
        scope_rid=scope_rid,
        mode="refresh",
        removal_reason="removed by approved revision",
        actor=actor,
        note=note,
        past_days=past_days,
        future_months=future_months,
        now=now,
    )
    _pin_past_content(connection, event["id"], revision, validated, scope_rid)
    return {
        "event_id": event["id"],
        "revision_id": committed["revision"]["id"],
        "revision_number": committed["revision"]["revision_number"],
        "approval_status": "approved",
        "scope": "future",
        "occurrence_id": target["id"],
        "occurrence_count": len(committed["in_scope"]),
        "review_action_id": committed["review_action_id"],
        "reviewed_at": committed["reviewed_at"],
    }


def _truncate_series_before(
    revision: dict, dates: list[dict], scope_rid: datetime
) -> tuple[str, list[dict]] | None:
    """Truncate the published rule so the series ends before `scope_rid`.

    Returns the replacement RRULE plus the kept recurrence dates, or None
    when nothing before the target would remain (callers then fall back to
    the whole-series removal). COUNT becomes an UNTIL on the last kept local
    day so the surviving set is exact regardless of prior skips and extras.
    """
    series_start = (
        revision["start_date"]
        if revision["is_all_day"]
        else revision["starts_at"].astimezone(ZoneInfo(revision["timezone"])).date()
    )
    specs = expand_revision(
        revision,
        dates,
        series_start,
        scope_rid.date() + timedelta(days=1),
    )
    kept = [spec for spec in specs if spec.recurrence_id < scope_rid]
    if not kept:
        return None
    last_day = max(spec.recurrence_id.date() for spec in kept)
    if revision["is_all_day"]:
        until_token = last_day.strftime("%Y%m%d")
    else:
        end_of_day = datetime(last_day.year, last_day.month, last_day.day, 23, 59)
        until_token = (
            reattach_wall_time(end_of_day, revision["timezone"])
            .astimezone(UTC)
            .strftime("%Y%m%dT%H%M%SZ")
        )
    new_rule = truncate_rule_before(revision["recurrence_rule"], until_token)
    kept_dates = [
        item for item in dates if item["local_start"] < scope_rid
    ]
    return new_rule, kept_dates


def _commit_truncation(
    connection: Connection,
    event: dict,
    revision: dict,
    dates: list[dict],
    scope_rid: datetime,
    *,
    mode: str,
    removal_reason: str,
    removal_action: str,
    actor: str,
    note: str | None,
    past_days: int,
    future_months: int,
    now: datetime,
) -> dict | None:
    """End a series before one date for scoped future cancel/delete.

    When the target is the first date (nothing would remain), returns None so
    callers fall back to the whole-series removal instead.
    """
    truncated = _truncate_series_before(revision, dates, scope_rid)
    if truncated is None:
        return None
    new_rule, kept_dates = truncated
    validated = _validate_synth_revision(
        _synth_values(
            title=revision["title"],
            description=revision["description"],
            location_name=revision["location_name"],
            location_address=revision["location_address"],
            event_url=revision["event_url"],
            is_all_day=bool(revision["is_all_day"]),
            starts_at=revision["starts_at"],
            ends_at=revision["ends_at"],
            start_date=revision["start_date"],
            end_date=revision["end_date"],
            timezone=revision["timezone"],
            recurrence_rule=new_rule,
            recurrence_dates=kept_dates,
            group_ids=list(_published_group_ids(connection, revision["id"])),
            submitter_name=revision["submitted_by_name"],
            submitter_channel=revision["submitted_by_channel"],
            submitter_contact=revision["submitted_by_contact"],
        ),
    )
    committed = _commit_scoped_revision(
        connection,
        event,
        validated,
        scope_rid=scope_rid,
        mode=mode,
        removal_reason=removal_reason,
        actor=actor,
        note=note,
        past_days=past_days,
        future_months=future_months,
        now=now,
    )
    removal = _log_scope_action(
        connection,
        event["id"],
        committed["revision"]["id"],
        removal_action,
        actor,
        note,
        now,
    )
    return {
        "revision_id": committed["revision"]["id"],
        "affected": committed["affected"],
        "approve_action_id": committed["review_action_id"],
        "review_action_id": removal["id"],
        "reviewed_at": removal["occurred_at"],
    }


def cancel_occurrences(
    connection: Connection,
    event_id: UUID,
    *,
    scope: str,
    occurrence_id: UUID | None,
    actor: str,
    note: str | None = None,
    is_admin: bool = False,
    management_token: str | None = None,
    past_days: int,
    future_months: int,
    now: datetime | None = None,
) -> dict:
    """Cancel one date, future dates, or the whole series of a recurring event.

    Single and future scopes keep the event published: a single date (or every
    date from the target on) stays visible, flagged as cancelled, while other
    dates are untouched. Deletion instead removes the dates from the calendar.
    A single-date cancellation survives later series edits and can be undone
    with `restore_occurrence`.
    """
    now = now or _now()
    event = _load_removable_event(connection, event_id)
    _require_removal_permission(
        event, is_admin=is_admin, management_token=management_token
    )
    if scope == "series" or occurrence_id is None:
        return cancel_event(
            connection,
            event_id,
            actor=actor,
            note=note,
            is_admin=is_admin,
            management_token=management_token,
            now=now,
        )
    full_event = _scoped_event(connection, event_id)
    revision, dates = _published_for_scope(connection, full_event)
    _require_recurring(revision, dates)
    target = _scope_target(connection, full_event["id"], occurrence_id)
    if scope == "single":
        if target["instance_cancelled"]:
            raise ConflictError("occurrence is already cancelled")
        connection.execute(
            """
            UPDATE event_occurrences SET instance_cancelled = TRUE,
              instance_exception = %s, version = version + 1, updated_at = now()
             WHERE id = %s
            """,
            (EXCEPTION_CANCELLED, target["id"]),
        )
        action = _log_scope_action(
            connection, full_event["id"], revision["id"], "cancel", actor, note, now
        )
        return {
            "event_id": full_event["id"],
            "occurrence_id": target["id"],
            "scope": "single",
            "is_cancelled": True,
            "cancelled_at": now.isoformat(),
            "version": target["version"] + 1,
            "review_action_id": action["id"],
            "reviewed_at": action["occurred_at"],
        }
    _require_no_pending(connection, full_event)
    truncated = _commit_truncation(
        connection,
        full_event,
        revision,
        dates,
        target["recurrence_id"],
        mode="mark",
        removal_reason="cancelled future occurrences",
        removal_action="cancel",
        actor=actor,
        note=note,
        past_days=past_days,
        future_months=future_months,
        now=now,
    )
    if truncated is None:
        return cancel_event(
            connection,
            event_id,
            actor=actor,
            note=note,
            is_admin=True,
            management_token=None,
            now=now,
        )
    return {
        "event_id": full_event["id"],
        "occurrence_id": target["id"],
        "scope": "future",
        "is_cancelled": True,
        "cancelled_occurrence_count": truncated["affected"],
        "revision_id": truncated["revision_id"],
        "review_action_id": truncated["review_action_id"],
        "reviewed_at": truncated["reviewed_at"],
    }


def delete_occurrences(
    connection: Connection,
    event_id: UUID,
    *,
    scope: str,
    occurrence_id: UUID | None,
    actor: str,
    note: str | None = None,
    is_admin: bool = False,
    management_token: str | None = None,
    past_days: int,
    future_months: int,
    now: datetime | None = None,
) -> dict:
    """Delete one date, future dates, or the whole series of a recurring event.

    Single and future scopes remove only those dates from the calendar (past
    dates and, for single, later dates stay); the event itself is never
    archived. A single deleted date is a skip: it stays skipped through later
    series edits that still produce it and can be undone with
    `restore_occurrence`.
    """
    now = now or _now()
    event = _load_removable_event(connection, event_id)
    _require_removal_permission(
        event, is_admin=is_admin, management_token=management_token
    )
    if scope == "series" or occurrence_id is None:
        return delete_event(
            connection,
            event_id,
            actor=actor,
            note=note,
            is_admin=is_admin,
            management_token=management_token,
            now=now,
        )
    full_event = _scoped_event(connection, event_id)
    revision, dates = _published_for_scope(connection, full_event)
    _require_recurring(revision, dates)
    target = _scope_target(connection, full_event["id"], occurrence_id)
    if scope == "single":
        connection.execute(
            """
            UPDATE event_occurrences SET status = 'cancelled',
              version = version + 1, cancellation_reason = %s,
              instance_cancelled = FALSE, instance_exception = %s,
              updated_at = now() WHERE id = %s
            """,
            (SKIPPED_REASON, EXCEPTION_SKIPPED, target["id"]),
        )
        action = _log_scope_action(
            connection, full_event["id"], revision["id"], "delete", actor, note, now
        )
        return {
            "event_id": full_event["id"],
            "occurrence_id": target["id"],
            "scope": "single",
            "deleted_occurrence_count": 1,
            "archived_at": now.isoformat(),
            "review_action_id": action["id"],
            "reviewed_at": action["occurred_at"],
        }
    _require_no_pending(connection, full_event)
    truncated = _commit_truncation(
        connection,
        full_event,
        revision,
        dates,
        target["recurrence_id"],
        mode="remove",
        removal_reason="deleted future occurrences",
        removal_action="delete",
        actor=actor,
        note=note,
        past_days=past_days,
        future_months=future_months,
        now=now,
    )
    if truncated is None:
        return delete_event(
            connection,
            event_id,
            actor=actor,
            note=note,
            is_admin=True,
            management_token=None,
            now=now,
        )
    return {
        "event_id": full_event["id"],
        "occurrence_id": target["id"],
        "scope": "future",
        "deleted_occurrence_count": truncated["affected"],
        "revision_id": truncated["revision_id"],
        "review_action_id": truncated["review_action_id"],
        "reviewed_at": truncated["reviewed_at"],
    }


def restore_occurrence(
    connection: Connection,
    event_id: UUID,
    occurrence_id: UUID,
    *,
    actor: str,
    note: str | None = None,
    is_admin: bool = False,
    management_token: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Undo a single-date cancellation or skip of a recurring event.

    A cancelled date loses its cancelled flag; a skipped date returns to the
    calendar with the series' current timing (and any per-date edits it had).
    Only dates cancelled or deleted on their own are restorable: removals
    from a "future" scope ended the series rule, so restoring those dates
    means editing the series.
    """
    now = now or _now()
    event = _load_removable_event(connection, event_id)
    _require_removal_permission(
        event, is_admin=is_admin, management_token=management_token
    )
    full_event = _scoped_event(connection, event_id)
    revision, _ = _published_for_scope(connection, full_event)
    row = connection.execute(
        """
        SELECT * FROM event_occurrences
         WHERE id = %s AND event_id = %s FOR UPDATE
        """,
        (occurrence_id, full_event["id"]),
    ).fetchone()
    if row is None:
        raise NotFoundError("occurrence not found")
    exception = row["instance_exception"]
    if exception is None:
        raise ConflictError(
            "only a date cancelled or deleted on its own can be restored"
        )
    connection.execute(
        """
        UPDATE event_occurrences SET status = 'scheduled',
          cancellation_reason = NULL, instance_cancelled = FALSE,
          instance_exception = NULL, version = version + 1, updated_at = now()
         WHERE id = %s
        """,
        (row["id"],),
    )
    action = _log_scope_action(
        connection, full_event["id"], revision["id"], "restore", actor, note, now
    )
    return {
        "event_id": full_event["id"],
        "occurrence_id": row["id"],
        "restored": exception,
        "version": row["version"] + 1,
        "restored_at": now.isoformat(),
        "review_action_id": action["id"],
        "reviewed_at": action["occurred_at"],
    }


def _wall_delta(
    published: dict, target: dict, payload: EventRevisionInput
) -> timedelta:
    """Wall-clock shift between a scoped payload and its target occurrence.

    The returned timedelta is added to the series start so the whole future
    moves with the edited date. Timed and all-day shapes must already match.
    """
    zone = ZoneInfo(published["timezone"])
    if payload.is_all_day:
        assert payload.start_date is not None
        target_date = target["start_date"]
        return payload.start_date - target_date
    assert payload.starts_at is not None
    target_wall = (
        target["starts_at"].astimezone(zone).replace(tzinfo=None, microsecond=0)
    )
    payload_wall = payload.starts_at.astimezone(zone).replace(
        tzinfo=None, microsecond=0
    )
    return payload_wall - target_wall
