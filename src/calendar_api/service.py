from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from dateutil.relativedelta import relativedelta
from psycopg import Connection
from psycopg.errors import UniqueViolation

from .errors import ConflictError, NotFoundError, ValidationError
from .normalization import normalize_contact
from .recurrence import OccurrenceSpec, expand_revision
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
  status = 'scheduled',
  version = event_occurrences.version + 1,
  is_exception = EXCLUDED.is_exception,
  is_all_day = EXCLUDED.is_all_day,
  starts_at = EXCLUDED.starts_at,
  ends_at = EXCLUDED.ends_at,
  start_date = EXCLUDED.start_date,
  end_date = EXCLUDED.end_date,
  timezone = EXCLUDED.timezone,
  cancellation_reason = NULL
"""

_OCCURRENCE_INSERT_IGNORE = (
    _OCCURRENCE_UPSERT.split("ON CONFLICT (event_id, recurrence_id)")[0]
    + "ON CONFLICT (event_id, recurrence_id) DO NOTHING"
)


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
                  cancellation_reason = NULL
                WHERE id = %(id)s
                """,
                values,
            )
            specs = []
            stable_non_recurring = True

    if specs:
        _execute_many(
            connection,
            _OCCURRENCE_UPSERT,
            [_occurrence_values(event["id"], revision["id"], spec) for spec in specs],
        )

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
          o.timezone,
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
    return [_as_dict(row) for row in rows[:limit]], has_more


_SERIES_UPCOMING_LIMIT = 6
_OCCURRENCE_FIELDS = """
    id AS occurrence_id, recurrence_id, is_exception, is_all_day, starts_at,
    ends_at, start_date, end_date, timezone
"""
_OCCURRENCE_KEY = "COALESCE(starts_at, start_date::timestamp AT TIME ZONE timezone)"


def _series_context(
    connection: Connection,
    event_id: UUID,
    selected: dict | None,
    now: datetime,
) -> dict:
    """Neighbouring and upcoming scheduled dates of a recurring event.

    Only materialized occurrences are visible, so counts and lists are bounded
    by the rolling window that ends at `coverage_end`.
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
    coverage = connection.execute(
        """
        SELECT window_end_exclusive FROM event_occurrence_materializations
         WHERE event_id = %s
        """,
        (event_id,),
    ).fetchone()
    return {
        "previous": _as_dict(previous) if previous else None,
        "next": _as_dict(following) if following else None,
        "upcoming": [_as_dict(item) for item in upcoming],
        "upcoming_count": upcoming_count,
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
               (SELECT COALESCE(jsonb_agg(
                         jsonb_build_object('id', g.id, 'slug', g.slug, 'name', g.name)
                         ORDER BY g.name, g.id
                       ), '[]'::jsonb)
                  FROM event_revision_groups rg
                  JOIN groups g ON g.id = rg.group_id
                 WHERE rg.event_revision_id = p.event_revision_id
                   AND g.is_active) AS groups
          FROM published_events p
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
         WHERE r.approval_status = %s
         ORDER BY r.submitted_at, e.id
         LIMIT %s OFFSET %s
        """,
        (status, limit, offset),
    ).fetchall()
    return [_as_dict(row) for row in rows]


def get_admin_event(connection: Connection, event_id: UUID) -> dict:
    event = connection.execute(
        """
        SELECT e.id AS event_id, e.current_revision_id, e.published_revision_id,
               current_revision.approval_status,
               current_revision.revision_number AS current_revision_number,
               e.original_submitter_name, e.original_submitter_channel,
               e.original_submitter_contact, e.submitted_at, e.archived_at,
               e.updated_at
          FROM events e
          JOIN event_revisions current_revision ON current_revision.id = e.current_revision_id
         WHERE e.id = %s
        """,
        (event_id,),
    ).fetchone()
    if event is None:
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
