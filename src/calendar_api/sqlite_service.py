from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from dateutil.relativedelta import relativedelta

from .errors import ConflictError, NotFoundError, UnauthorizedError, ValidationError
from .normalization import normalize_contact
from .recurrence import OccurrenceSpec, expand_revision
from .schemas import EventRevisionInput, GroupCreate, GroupUpdate, ReviewInput
from .security import new_token, token_digest_bytes, token_matches_digest


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(UTC).isoformat()


def _local_timestamp(value: datetime) -> str:
    return value.replace(tzinfo=None, microsecond=0).isoformat()


def _groups_for_revision(
    connection: sqlite3.Connection, revision_id: str, *, active_only: bool
) -> list[dict]:
    suffix = "AND g.is_active = 1" if active_only else ""
    rows = connection.execute(
        f"""
        SELECT g.id, g.slug, g.name
          FROM event_revision_groups rg
          JOIN groups g ON g.id = rg.group_id
         WHERE rg.event_revision_id = ? {suffix}
         ORDER BY g.name, g.id
        """,
        (revision_id,),
    ).fetchall()
    return [dict(item) for item in rows]


def list_groups(
    connection: sqlite3.Connection, *, include_inactive: bool = False
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT id, slug, name, description, is_active, created_at, updated_at
          FROM groups
         WHERE is_active = 1 OR ?
         ORDER BY name, id
        """,
        (int(include_inactive),),
    ).fetchall()
    return [{**dict(item), "is_active": bool(item["is_active"])} for item in rows]


def create_group(connection: sqlite3.Connection, payload: GroupCreate) -> dict:
    group_id = str(uuid4())
    now = _timestamp()
    try:
        connection.execute(
            """
            INSERT INTO groups
              (id, slug, name, description, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (group_id, payload.slug, payload.name, payload.description, now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(f"group slug '{payload.slug}' already exists") from exc
    result = dict(
        connection.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    )
    result["is_active"] = bool(result["is_active"])
    return result


def update_group(
    connection: sqlite3.Connection, group_id: UUID, payload: GroupUpdate
) -> dict:
    changes = payload.model_dump(exclude_unset=True)
    if "is_active" in changes:
        changes["is_active"] = int(changes["is_active"])
    changes["updated_at"] = _timestamp()
    assignments = ", ".join(f"{field} = ?" for field in changes)
    cursor = connection.execute(
        f"UPDATE groups SET {assignments} WHERE id = ?",
        (*changes.values(), str(group_id)),
    )
    if cursor.rowcount == 0:
        raise NotFoundError("group not found")
    result = dict(
        connection.execute(
            "SELECT * FROM groups WHERE id = ?", (str(group_id),)
        ).fetchone()
    )
    result["is_active"] = bool(result["is_active"])
    return result


def _require_active_groups(
    connection: sqlite3.Connection, group_ids: list[UUID]
) -> None:
    if not group_ids:
        return
    placeholders = ",".join("?" for _ in group_ids)
    count = connection.execute(
        f"SELECT count(*) FROM groups WHERE id IN ({placeholders}) AND is_active = 1",
        tuple(map(str, group_ids)),
    ).fetchone()[0]
    if count != len(group_ids):
        raise ValidationError("every group_id must reference an active group")


def _insert_revision(
    connection: sqlite3.Connection,
    event_id: str,
    revision_number: int,
    supersedes_revision_id: str | None,
    payload: EventRevisionInput,
) -> dict:
    revision_id = str(uuid4())
    submitted_at = _timestamp()
    contact = normalize_contact(payload.submitter.channel, payload.submitter.contact)
    connection.execute(
        """
        INSERT INTO event_revisions (
          id, event_id, revision_number, supersedes_revision_id,
          approval_status, title, description, location_name, location_address,
          event_url, is_all_day, starts_at, ends_at, start_date, end_date,
          timezone, recurrence_rule, submitted_by_name, submitted_by_channel,
          submitted_by_contact, submitted_at
        ) VALUES (
          ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            revision_id,
            event_id,
            revision_number,
            supersedes_revision_id,
            payload.title,
            payload.description,
            payload.location_name or None,
            payload.location_address or None,
            payload.event_url or None,
            int(payload.is_all_day),
            _timestamp(payload.starts_at) if payload.starts_at else None,
            _timestamp(payload.ends_at) if payload.ends_at else None,
            payload.start_date.isoformat() if payload.start_date else None,
            payload.end_date.isoformat() if payload.end_date else None,
            payload.timezone,
            payload.recurrence_rule,
            payload.submitter.name,
            payload.submitter.channel,
            contact,
            submitted_at,
        ),
    )
    now = _timestamp()
    connection.executemany(
        """
        INSERT INTO event_revision_groups
          (event_revision_id, group_id, created_at) VALUES (?, ?, ?)
        """,
        [(revision_id, str(group_id), now) for group_id in payload.group_ids],
    )
    connection.executemany(
        """
        INSERT INTO event_revision_recurrence_dates
          (event_revision_id, local_start, kind, created_at) VALUES (?, ?, ?, ?)
        """,
        [
            (revision_id, _local_timestamp(item.local_start), item.kind, now)
            for item in payload.recurrence_dates
        ],
    )
    return dict(
        connection.execute(
            "SELECT * FROM event_revisions WHERE id = ?", (revision_id,)
        ).fetchone()
    )


def create_event(connection: sqlite3.Connection, payload: EventRevisionInput) -> dict:
    _require_active_groups(connection, payload.group_ids)
    event_id = str(uuid4())
    submitted_at = _timestamp()
    contact = normalize_contact(payload.submitter.channel, payload.submitter.contact)
    management_token = new_token()
    connection.execute(
        """
        INSERT INTO events (
          id, original_submitter_name, original_submitter_channel,
          original_submitter_contact, management_token_hash, submitted_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            payload.submitter.name,
            payload.submitter.channel,
            contact,
            token_digest_bytes(management_token),
            submitted_at,
            submitted_at,
        ),
    )
    revision = _insert_revision(connection, event_id, 1, None, payload)
    connection.execute(
        "UPDATE events SET current_revision_id = ? WHERE id = ?",
        (revision["id"], event_id),
    )
    return {
        "event_id": event_id,
        "revision_id": revision["id"],
        "revision_number": 1,
        "approval_status": "pending",
        "submitted_at": submitted_at,
        "management_token": management_token,
    }


def event_exists(connection: sqlite3.Connection, event_id: UUID) -> bool:
    """Whether the event exists and has not been deleted."""
    row = connection.execute(
        "SELECT 1 AS one FROM events WHERE id = ? AND archived_at IS NULL",
        (str(event_id),),
    ).fetchone()
    return row is not None


def event_management_token_matches(
    connection: sqlite3.Connection, event_id: UUID, token: str | None
) -> bool:
    row = connection.execute(
        "SELECT management_token_hash FROM events WHERE id = ? AND archived_at IS NULL",
        (str(event_id),),
    ).fetchone()
    return row is not None and token_matches_digest(token, row["management_token_hash"])


def get_editable_event(connection: sqlite3.Connection, event_id: UUID) -> dict:
    row = connection.execute(
        """
        SELECT r.id AS revision_id, r.event_id, r.revision_number,
               r.approval_status, r.title, r.description, r.location_name,
               r.location_address, r.event_url, r.is_all_day, r.starts_at,
               r.ends_at, r.start_date, r.end_date, r.timezone,
               r.recurrence_rule, r.submitted_by_name, r.submitted_by_channel,
               r.submitted_by_contact, r.submitted_at
          FROM events e
          JOIN event_revisions r ON r.id = e.current_revision_id
         WHERE e.id = ? AND e.archived_at IS NULL
        """,
        (str(event_id),),
    ).fetchone()
    if row is None:
        raise NotFoundError("event not found")
    result = dict(row)
    result["is_all_day"] = bool(result["is_all_day"])
    result["groups"] = _groups_for_revision(
        connection, result["revision_id"], active_only=False
    )
    dates = connection.execute(
        """
        SELECT local_start, kind FROM event_revision_recurrence_dates
         WHERE event_revision_id = ? ORDER BY local_start
        """,
        (result["revision_id"],),
    ).fetchall()
    result["recurrence_dates"] = [dict(item) for item in dates]
    return result


def create_revision(
    connection: sqlite3.Connection, event_id: UUID, payload: EventRevisionInput
) -> dict:
    _require_active_groups(connection, payload.group_ids)
    event = connection.execute(
        """
        SELECT e.*, r.revision_number, r.approval_status
          FROM events e
          JOIN event_revisions r ON r.id = e.current_revision_id
         WHERE e.id = ?
        """,
        (str(event_id),),
    ).fetchone()
    if event is None or event["archived_at"] is not None:
        raise NotFoundError("event not found")
    if event["approval_status"] == "pending":
        contact = normalize_contact(
            payload.submitter.channel, payload.submitter.contact
        )
        submitted_at = _timestamp()
        connection.execute(
            """
            UPDATE event_revisions SET
              title = ?, description = ?, location_name = ?,
              location_address = ?, event_url = ?, is_all_day = ?,
              starts_at = ?, ends_at = ?, start_date = ?, end_date = ?,
              timezone = ?, recurrence_rule = ?, submitted_by_name = ?,
              submitted_by_channel = ?, submitted_by_contact = ?,
              submitted_at = ?
             WHERE id = ?
            """,
            (
                payload.title,
                payload.description,
                payload.location_name or None,
                payload.location_address or None,
                payload.event_url or None,
                int(payload.is_all_day),
                _timestamp(payload.starts_at) if payload.starts_at else None,
                _timestamp(payload.ends_at) if payload.ends_at else None,
                payload.start_date.isoformat() if payload.start_date else None,
                payload.end_date.isoformat() if payload.end_date else None,
                payload.timezone,
                payload.recurrence_rule,
                payload.submitter.name,
                payload.submitter.channel,
                contact,
                submitted_at,
                event["current_revision_id"],
            ),
        )
        revision_id = event["current_revision_id"]
        connection.execute(
            "DELETE FROM event_revision_groups WHERE event_revision_id = ?",
            (revision_id,),
        )
        now = _timestamp()
        connection.executemany(
            """
            INSERT INTO event_revision_groups
              (event_revision_id, group_id, created_at) VALUES (?, ?, ?)
            """,
            [(revision_id, str(group_id), now) for group_id in payload.group_ids],
        )
        connection.execute(
            "DELETE FROM event_revision_recurrence_dates WHERE event_revision_id = ?",
            (revision_id,),
        )
        connection.executemany(
            """
            INSERT INTO event_revision_recurrence_dates
              (event_revision_id, local_start, kind, created_at) VALUES (?, ?, ?, ?)
            """,
            [
                (revision_id, _local_timestamp(item.local_start), item.kind, now)
                for item in payload.recurrence_dates
            ],
        )
        return {
            "event_id": str(event_id),
            "revision_id": revision_id,
            "revision_number": event["revision_number"],
            "approval_status": "pending",
            "submitted_at": submitted_at,
            "updated_pending_revision": True,
        }
    revision = _insert_revision(
        connection,
        str(event_id),
        event["revision_number"] + 1,
        event["current_revision_id"],
        payload,
    )
    connection.execute(
        "UPDATE events SET current_revision_id = ?, updated_at = ? WHERE id = ?",
        (revision["id"], _timestamp(), str(event_id)),
    )
    return {
        "event_id": str(event_id),
        "revision_id": revision["id"],
        "revision_number": revision["revision_number"],
        "approval_status": "pending",
        "submitted_at": revision["submitted_at"],
    }


def _recurrence_dates(connection: sqlite3.Connection, revision_id: str) -> list[dict]:
    rows = connection.execute(
        """
        SELECT local_start, kind FROM event_revision_recurrence_dates
         WHERE event_revision_id = ? ORDER BY local_start
        """,
        (revision_id,),
    ).fetchall()
    return [
        {
            "local_start": datetime.fromisoformat(item["local_start"]),
            "kind": item["kind"],
        }
        for item in rows
    ]


def _revision(row: sqlite3.Row | dict) -> dict:
    result = dict(row)
    result["is_all_day"] = bool(result["is_all_day"])
    for field in ("starts_at", "ends_at"):
        if result.get(field):
            result[field] = datetime.fromisoformat(result[field])
    for field in ("start_date", "end_date"):
        if result.get(field):
            result[field] = date.fromisoformat(result[field])
    return result


def _spec_values(spec: OccurrenceSpec) -> dict[str, Any]:
    return {
        "recurrence_id": _local_timestamp(spec.recurrence_id),
        "is_exception": int(spec.is_exception),
        "is_all_day": int(spec.is_all_day),
        "starts_at": _timestamp(spec.starts_at) if spec.starts_at else None,
        "ends_at": _timestamp(spec.ends_at) if spec.ends_at else None,
        "start_date": spec.start_date.isoformat() if spec.start_date else None,
        "end_date": spec.end_date.isoformat() if spec.end_date else None,
        "timezone": spec.timezone,
    }


def _insert_occurrence(
    connection: sqlite3.Connection,
    event_id: str,
    revision_id: str,
    spec: OccurrenceSpec,
    *,
    update_existing: bool,
) -> bool:
    values = _spec_values(spec)
    existing = connection.execute(
        "SELECT id FROM event_occurrences WHERE event_id = ? AND recurrence_id = ?",
        (event_id, values["recurrence_id"]),
    ).fetchone()
    now = _timestamp()
    if existing:
        if not update_existing:
            return False
        connection.execute(
            """
            UPDATE event_occurrences SET
              source_revision_id = ?, status = 'scheduled', version = version + 1,
              is_exception = ?, is_all_day = ?, starts_at = ?, ends_at = ?,
              start_date = ?, end_date = ?, timezone = ?, cancellation_reason = NULL,
              updated_at = ?
             WHERE id = ?
            """,
            (
                revision_id,
                values["is_exception"],
                values["is_all_day"],
                values["starts_at"],
                values["ends_at"],
                values["start_date"],
                values["end_date"],
                values["timezone"],
                now,
                existing["id"],
            ),
        )
        return False
    connection.execute(
        """
        INSERT INTO event_occurrences (
          id, event_id, source_revision_id, recurrence_id, status, version,
          is_exception, is_all_day, starts_at, ends_at, start_date, end_date,
          timezone, materialized_at, updated_at
        ) VALUES (?, ?, ?, ?, 'scheduled', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            event_id,
            revision_id,
            values["recurrence_id"],
            values["is_exception"],
            values["is_all_day"],
            values["starts_at"],
            values["ends_at"],
            values["start_date"],
            values["end_date"],
            values["timezone"],
            now,
            now,
        ),
    )
    return True


def _is_non_recurring(connection: sqlite3.Connection, revision_id: str) -> bool:
    row = connection.execute(
        """
        SELECT r.recurrence_rule,
               EXISTS (SELECT 1 FROM event_revision_recurrence_dates d
                        WHERE d.event_revision_id = r.id) AS has_dates
          FROM event_revisions r WHERE r.id = ?
        """,
        (revision_id,),
    ).fetchone()
    return bool(row and row["recurrence_rule"] is None and not row["has_dates"])


def _reconcile(
    connection: sqlite3.Connection,
    event: dict,
    revision: dict,
    specs: list[OccurrenceSpec],
    window_start: date,
    window_end: date,
    now: datetime,
) -> None:
    previous_id = event["published_revision_id"] or revision["supersedes_revision_id"]
    stable_one_off = False
    if (
        previous_id
        and _is_non_recurring(connection, previous_id)
        and revision["recurrence_rule"] is None
        and not _recurrence_dates(connection, revision["id"])
        and len(specs) == 1
    ):
        rows = connection.execute(
            "SELECT id FROM event_occurrences WHERE event_id = ?",
            (event["id"],),
        ).fetchall()
        if len(rows) == 1:
            values = _spec_values(specs[0])
            connection.execute(
                """
                UPDATE event_occurrences SET
                  source_revision_id = ?, recurrence_id = ?, status = 'scheduled',
                  version = version + 1, is_exception = ?, is_all_day = ?,
                  starts_at = ?, ends_at = ?, start_date = ?, end_date = ?,
                  timezone = ?, cancellation_reason = NULL, updated_at = ?
                 WHERE id = ?
                """,
                (
                    revision["id"],
                    values["recurrence_id"],
                    values["is_exception"],
                    values["is_all_day"],
                    values["starts_at"],
                    values["ends_at"],
                    values["start_date"],
                    values["end_date"],
                    values["timezone"],
                    _timestamp(),
                    rows[0]["id"],
                ),
            )
            stable_one_off = True

    if not stable_one_off:
        for spec in specs:
            _insert_occurrence(
                connection, event["id"], revision["id"], spec, update_existing=True
            )
        desired = {_local_timestamp(spec.recurrence_id) for spec in specs}
        rows = connection.execute(
            """
            SELECT id, recurrence_id, is_all_day, starts_at, start_date
              FROM event_occurrences
             WHERE event_id = ? AND status = 'scheduled'
            """,
            (event["id"],),
        ).fetchall()
        for item in rows:
            if item["recurrence_id"] in desired:
                continue
            is_future = (
                date.fromisoformat(item["start_date"]) >= now.date()
                if item["is_all_day"]
                else datetime.fromisoformat(item["starts_at"]) >= now
            )
            if is_future:
                connection.execute(
                    """
                    UPDATE event_occurrences SET source_revision_id = ?,
                      status = 'cancelled', version = version + 1,
                      cancellation_reason = 'removed by approved revision',
                      updated_at = ? WHERE id = ?
                    """,
                    (revision["id"], _timestamp(), item["id"]),
                )

    connection.execute(
        """
        INSERT INTO event_occurrence_materializations (
          event_id, source_revision_id, window_start, window_end_exclusive,
          completed_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
          source_revision_id = excluded.source_revision_id,
          window_start = min(window_start, excluded.window_start),
          window_end_exclusive = max(window_end_exclusive, excluded.window_end_exclusive),
          completed_at = excluded.completed_at
        """,
        (
            event["id"],
            revision["id"],
            window_start.isoformat(),
            window_end.isoformat(),
            _timestamp(),
        ),
    )


def approve_revision(
    connection: sqlite3.Connection,
    event_id: UUID,
    revision_id: UUID,
    review: ReviewInput,
    *,
    past_days: int,
    future_months: int,
    now: datetime | None = None,
) -> dict:
    now = now or _now()
    event_row = connection.execute(
        """
        SELECT e.*, r.approval_status
          FROM events e JOIN event_revisions r ON r.id = ? AND r.event_id = e.id
         WHERE e.id = ?
        """,
        (str(revision_id), str(event_id)),
    ).fetchone()
    if event_row is None:
        raise NotFoundError("event revision not found")
    event = dict(event_row)
    if event["current_revision_id"] != str(revision_id):
        raise ConflictError("only the event's current revision can be approved")
    if event["approval_status"] != "pending":
        raise ConflictError("revision is not pending")
    revision = _revision(
        connection.execute(
            "SELECT * FROM event_revisions WHERE id = ?", (str(revision_id),)
        ).fetchone()
    )
    materialization = connection.execute(
        "SELECT * FROM event_occurrence_materializations WHERE event_id = ?",
        (str(event_id),),
    ).fetchone()
    window_start = now.date() - timedelta(days=past_days)
    window_end = now.date() + relativedelta(months=future_months)
    if materialization:
        window_start = min(
            window_start, date.fromisoformat(materialization["window_start"])
        )
        window_end = max(
            window_end, date.fromisoformat(materialization["window_end_exclusive"])
        )
    specs = expand_revision(
        revision,
        _recurrence_dates(connection, str(revision_id)),
        window_start,
        window_end,
    )
    reviewed_at = _timestamp(now)
    connection.execute(
        """
        UPDATE event_revisions SET approval_status = 'approved', reviewed_at = ?,
          reviewed_by = ?, review_note = ? WHERE id = ?
        """,
        (reviewed_at, review.actor, review.note, str(revision_id)),
    )
    _reconcile(connection, event, revision, specs, window_start, window_end, now)
    connection.execute(
        "UPDATE events SET published_revision_id = ?, updated_at = ? WHERE id = ?",
        (str(revision_id), reviewed_at, str(event_id)),
    )
    action_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO event_review_actions
          (id, event_id, event_revision_id, action, actor, note, occurred_at)
        VALUES (?, ?, ?, 'approve', ?, ?, ?)
        """,
        (
            action_id,
            str(event_id),
            str(revision_id),
            review.actor,
            review.note,
            reviewed_at,
        ),
    )
    return {
        "event_id": str(event_id),
        "revision_id": str(revision_id),
        "approval_status": "approved",
        "occurrence_count": len(specs),
        "review_action_id": action_id,
        "reviewed_at": reviewed_at,
    }


def reject_revision(
    connection: sqlite3.Connection,
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
          FROM events e JOIN event_revisions r ON r.id = ? AND r.event_id = e.id
         WHERE e.id = ?
        """,
        (str(revision_id), str(event_id)),
    ).fetchone()
    if row is None:
        raise NotFoundError("event revision not found")
    if row["current_revision_id"] != str(revision_id):
        raise ConflictError("only the event's current revision can be rejected")
    if row["approval_status"] != "pending":
        raise ConflictError("revision is not pending")
    reviewed_at = _timestamp(now)
    connection.execute(
        """
        UPDATE event_revisions SET approval_status = 'rejected', reviewed_at = ?,
          reviewed_by = ?, review_note = ? WHERE id = ?
        """,
        (reviewed_at, review.actor, review.note, str(revision_id)),
    )
    action_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO event_review_actions
          (id, event_id, event_revision_id, action, actor, note, occurred_at)
        VALUES (?, ?, ?, 'reject', ?, ?, ?)
        """,
        (
            action_id,
            str(event_id),
            str(revision_id),
            review.actor,
            review.note,
            reviewed_at,
        ),
    )
    return {
        "event_id": str(event_id),
        "revision_id": str(revision_id),
        "approval_status": "rejected",
        "review_action_id": action_id,
        "reviewed_at": reviewed_at,
    }


def revoke_event(
    connection: sqlite3.Connection,
    event_id: UUID,
    review: ReviewInput,
    *,
    now: datetime | None = None,
) -> dict:
    now = now or _now()
    event = connection.execute(
        "SELECT * FROM events WHERE id = ?", (str(event_id),)
    ).fetchone()
    if event is None:
        raise NotFoundError("event not found")
    revision_id = event["published_revision_id"]
    if revision_id is None:
        raise ConflictError("event is not published")
    occurred_at = _timestamp(now)
    connection.execute(
        "UPDATE events SET published_revision_id = NULL, updated_at = ? WHERE id = ?",
        (occurred_at, str(event_id)),
    )
    connection.execute(
        "UPDATE event_revisions SET approval_status = 'revoked' WHERE id = ?",
        (revision_id,),
    )
    cancelled = 0
    for item in connection.execute(
        """
        SELECT id, is_all_day, starts_at, start_date FROM event_occurrences
         WHERE event_id = ? AND status = 'scheduled'
        """,
        (str(event_id),),
    ).fetchall():
        is_future = (
            date.fromisoformat(item["start_date"]) >= now.date()
            if item["is_all_day"]
            else datetime.fromisoformat(item["starts_at"]) >= now
        )
        if is_future:
            connection.execute(
                """
                UPDATE event_occurrences SET status = 'cancelled',
                  version = version + 1, cancellation_reason = 'event revoked',
                  updated_at = ? WHERE id = ?
                """,
                (occurred_at, item["id"]),
            )
            cancelled += 1
    action_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO event_review_actions
          (id, event_id, event_revision_id, action, actor, note, occurred_at)
        VALUES (?, ?, ?, 'revoke', ?, ?, ?)
        """,
        (action_id, str(event_id), revision_id, review.actor, review.note, occurred_at),
    )
    return {
        "event_id": str(event_id),
        "revision_id": revision_id,
        "approval_status": "revoked",
        "cancelled_occurrence_count": cancelled,
        "review_action_id": action_id,
        "reviewed_at": occurred_at,
    }


def _load_removable_event(
    connection: sqlite3.Connection, event_id: UUID
) -> dict:
    """Load an event row for cancellation or deletion.

    Deleted events (archived) no longer exist, so they read as missing. The
    existence check comes before authorization so removals of missing events
    report 404 regardless of credentials.
    """
    row = connection.execute(
        """
        SELECT id, current_revision_id, published_revision_id, archived_at,
               cancelled_at, management_token_hash
          FROM events WHERE id = ?
        """,
        (str(event_id),),
    ).fetchone()
    if row is None:
        raise NotFoundError("event not found")
    event = dict(row)
    if event["archived_at"] is not None:
        raise NotFoundError("event not found")
    return event


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
    connection: sqlite3.Connection,
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

    occurred_at = _timestamp(now)
    connection.execute(
        """
        UPDATE events SET cancelled_at = ?, cancelled_by = ?, cancel_reason = ?,
          updated_at = ? WHERE id = ?
        """,
        (occurred_at, actor, note, occurred_at, event["id"]),
    )
    action_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO event_review_actions
          (id, event_id, event_revision_id, action, actor, note, occurred_at)
        VALUES (?, ?, ?, 'cancel', ?, ?, ?)
        """,
        (
            action_id,
            event["id"],
            event["current_revision_id"],
            actor,
            note,
            occurred_at,
        ),
    )
    return {
        "event_id": event["id"],
        "revision_id": event["current_revision_id"],
        "is_cancelled": True,
        "cancelled_at": occurred_at,
        "review_action_id": action_id,
        "reviewed_at": occurred_at,
    }


def delete_event(
    connection: sqlite3.Connection,
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

    occurred_at = _timestamp(now)
    connection.execute(
        "UPDATE events SET archived_at = ?, updated_at = ? WHERE id = ?",
        (occurred_at, occurred_at, event["id"]),
    )
    cancelled = 0
    for item in connection.execute(
        """
        SELECT id, is_all_day, starts_at, start_date FROM event_occurrences
         WHERE event_id = ? AND status = 'scheduled'
        """,
        (event["id"],),
    ).fetchall():
        is_future = (
            date.fromisoformat(item["start_date"]) >= now.date()
            if item["is_all_day"]
            else datetime.fromisoformat(item["starts_at"]) >= now
        )
        if is_future:
            connection.execute(
                """
                UPDATE event_occurrences SET status = 'cancelled',
                  version = version + 1, cancellation_reason = 'event deleted',
                  updated_at = ? WHERE id = ?
                """,
                (occurred_at, item["id"]),
            )
            cancelled += 1
    action_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO event_review_actions
          (id, event_id, event_revision_id, action, actor, note, occurred_at)
        VALUES (?, ?, ?, 'delete', ?, ?, ?)
        """,
        (
            action_id,
            event["id"],
            event["current_revision_id"],
            actor,
            note,
            occurred_at,
        ),
    )
    return {
        "event_id": event["id"],
        "deleted": True,
        "archived_at": occurred_at,
        "cancelled_occurrence_count": cancelled,
        "review_action_id": action_id,
        "reviewed_at": occurred_at,
    }


def materialize_all(
    connection: sqlite3.Connection,
    *,
    past_days: int,
    future_months: int,
    now: datetime | None = None,
) -> dict[str, int]:
    now = now or _now()
    target_start = now.date() - timedelta(days=past_days)
    target_end = now.date() + relativedelta(months=future_months)
    rows = connection.execute(
        """
        SELECT e.*, m.window_start, m.window_end_exclusive
          FROM events e
          LEFT JOIN event_occurrence_materializations m ON m.event_id = e.id
         WHERE e.published_revision_id IS NOT NULL
        """
    ).fetchall()
    processed = inserted = 0
    for event_row in rows:
        event = dict(event_row)
        old_start = (
            date.fromisoformat(event["window_start"]) if event["window_start"] else None
        )
        old_end = (
            date.fromisoformat(event["window_end_exclusive"])
            if event["window_end_exclusive"]
            else None
        )
        if old_start and old_start <= target_start and old_end >= target_end:
            continue
        window_start = min(target_start, old_start) if old_start else target_start
        window_end = max(target_end, old_end) if old_end else target_end
        revision_id = event["published_revision_id"]
        revision = _revision(
            connection.execute(
                "SELECT * FROM event_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        )
        specs = expand_revision(
            revision,
            _recurrence_dates(connection, revision_id),
            window_start,
            window_end,
        )
        inserted += sum(
            _insert_occurrence(
                connection, event["id"], revision_id, spec, update_existing=False
            )
            for spec in specs
        )
        processed += 1
        connection.execute(
            """
            INSERT INTO event_occurrence_materializations (
              event_id, source_revision_id, window_start, window_end_exclusive,
              completed_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
              source_revision_id = excluded.source_revision_id,
              window_start = excluded.window_start,
              window_end_exclusive = excluded.window_end_exclusive,
              completed_at = excluded.completed_at
            """,
            (
                event["id"],
                revision_id,
                window_start.isoformat(),
                window_end.isoformat(),
                _timestamp(),
            ),
        )
    return {"events_processed": processed, "occurrences_inserted": inserted}


def calendar_occurrences(
    connection: sqlite3.Connection,
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
        placeholders = ",".join("?" for _ in group_slugs)
        found = connection.execute(
            f"SELECT slug FROM groups WHERE slug IN ({placeholders}) AND is_active = 1",
            tuple(group_slugs),
        ).fetchall()
        missing = sorted(set(group_slugs) - {item["slug"] for item in found})
        if missing:
            raise ValidationError(
                f"unknown or inactive group slug(s): {', '.join(missing)}"
            )

    group_clause = ""
    params: list[Any] = [
        _timestamp(end_at),
        _timestamp(start_at),
        end_date.isoformat(),
        start_date.isoformat(),
    ]
    if group_slugs:
        placeholders = ",".join("?" for _ in group_slugs)
        group_clause = f"""
          AND EXISTS (
            SELECT 1 FROM event_revision_groups filter_rg
            JOIN groups filter_g ON filter_g.id = filter_rg.group_id
            WHERE filter_rg.event_revision_id = r.id
              AND filter_g.slug IN ({placeholders}) AND filter_g.is_active = 1
          )
        """
        params.extend(group_slugs)
    params.extend((limit + 1, offset))
    rows = connection.execute(
        f"""
        SELECT o.id AS occurrence_id, o.event_id, o.version, o.is_exception,
               o.is_all_day, o.starts_at, o.ends_at, o.start_date, o.end_date,
               o.timezone, e.cancelled_at IS NOT NULL AS is_cancelled,
               r.id AS revision_id, r.title, r.description,
               r.location_name, r.location_address, r.event_url,
               r.recurrence_rule
          FROM event_occurrences o
          JOIN events e ON e.id = o.event_id
          JOIN event_revisions r ON r.id = e.published_revision_id
         WHERE e.archived_at IS NULL AND r.approval_status = 'approved'
           AND o.status = 'scheduled'
           AND (
             NOT EXISTS (
               SELECT 1 FROM event_revision_groups assigned_rg
                WHERE assigned_rg.event_revision_id = r.id
             )
             OR EXISTS (
               SELECT 1 FROM event_revision_groups visible_rg
               JOIN groups visible_g ON visible_g.id = visible_rg.group_id
               WHERE visible_rg.event_revision_id = r.id AND visible_g.is_active = 1
             )
           )
           AND ((o.is_all_day = 0 AND o.starts_at < ? AND o.ends_at > ?)
             OR (o.is_all_day = 1 AND o.start_date < ? AND o.end_date > ?))
           {group_clause}
         ORDER BY coalesce(o.starts_at, o.start_date), o.event_id, o.id
         LIMIT ? OFFSET ?
        """,
        tuple(params),
    ).fetchall()
    has_more = len(rows) > limit
    items: list[dict] = []
    for source in rows[:limit]:
        item = dict(source)
        item["is_exception"] = bool(item["is_exception"])
        item["is_all_day"] = bool(item["is_all_day"])
        item["is_cancelled"] = bool(item.get("is_cancelled"))
        item["groups"] = _groups_for_revision(
            connection, item["revision_id"], active_only=True
        )
        items.append(item)
    return items, has_more


_SERIES_UPCOMING_LIMIT = 6
_OCCURRENCE_FIELDS = """
    id AS occurrence_id, recurrence_id, is_exception, is_all_day, starts_at,
    ends_at, start_date, end_date, timezone
"""
# Timed and all-day values never mix within one event's scheduled rows, so
# the ISO strings order chronologically; the id breaks exact ties.
_OCCURRENCE_KEY = "coalesce(starts_at, start_date)"


def _occurrence(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    result["is_exception"] = bool(result["is_exception"])
    result["is_all_day"] = bool(result["is_all_day"])
    return result


def _series_context(
    connection: sqlite3.Connection,
    event_id: str,
    selected: dict | None,
    now: datetime,
) -> dict:
    """Neighbouring and upcoming scheduled dates of a recurring event.

    Only materialized occurrences are visible, so counts and lists are bounded
    by the rolling window that ends at `coverage_end`.
    """
    previous = following = None
    if selected is not None:
        key = selected["starts_at"] or selected["start_date"]
        params = (event_id, key, selected["occurrence_id"])
        previous = _occurrence(connection.execute(
            f"""
            SELECT {_OCCURRENCE_FIELDS} FROM event_occurrences
             WHERE event_id = ? AND status = 'scheduled'
               AND ({_OCCURRENCE_KEY}, id) < (?, ?)
             ORDER BY {_OCCURRENCE_KEY} DESC, id DESC LIMIT 1
            """,
            params,
        ).fetchone())
        following = _occurrence(connection.execute(
            f"""
            SELECT {_OCCURRENCE_FIELDS} FROM event_occurrences
             WHERE event_id = ? AND status = 'scheduled'
               AND ({_OCCURRENCE_KEY}, id) > (?, ?)
             ORDER BY {_OCCURRENCE_KEY}, id LIMIT 1
            """,
            params,
        ).fetchone())
    not_ended = """
        event_id = ? AND status = 'scheduled'
        AND ((is_all_day = 0 AND ends_at > ?) OR (is_all_day = 1 AND end_date > ?))
    """
    not_ended_params = (event_id, _timestamp(now), now.date().isoformat())
    upcoming = connection.execute(
        f"""
        SELECT {_OCCURRENCE_FIELDS} FROM event_occurrences
         WHERE {not_ended} ORDER BY {_OCCURRENCE_KEY}, id LIMIT ?
        """,
        (*not_ended_params, _SERIES_UPCOMING_LIMIT),
    ).fetchall()
    upcoming_count = connection.execute(
        f"SELECT count(*) FROM event_occurrences WHERE {not_ended}",
        not_ended_params,
    ).fetchone()[0]
    coverage = connection.execute(
        """
        SELECT window_end_exclusive FROM event_occurrence_materializations
         WHERE event_id = ?
        """,
        (event_id,),
    ).fetchone()
    return {
        "previous": previous,
        "next": following,
        "upcoming": [_occurrence(item) for item in upcoming],
        "upcoming_count": upcoming_count,
        "coverage_end": coverage["window_end_exclusive"] if coverage else None,
    }


def get_published_event(
    connection: sqlite3.Connection,
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
        SELECT e.id AS event_id, r.id AS event_revision_id, r.revision_number,
               r.title, r.description, r.location_name, r.location_address,
               r.event_url, r.is_all_day, r.starts_at, r.ends_at, r.start_date,
               r.end_date, r.timezone, r.recurrence_rule, r.submitted_at,
               r.reviewed_at, e.cancelled_at, e.cancel_reason
          FROM events e JOIN event_revisions r ON r.id = e.published_revision_id
         WHERE e.id = ? AND e.archived_at IS NULL
           AND r.approval_status = 'approved'
           AND (
             NOT EXISTS (
               SELECT 1 FROM event_revision_groups assigned_rg
                WHERE assigned_rg.event_revision_id = r.id
             )
             OR EXISTS (
               SELECT 1 FROM event_revision_groups rg
               JOIN groups g ON g.id = rg.group_id
               WHERE rg.event_revision_id = r.id AND g.is_active = 1
             )
           )
        """,
        (str(event_id),),
    ).fetchone()
    if row is None:
        raise NotFoundError("published event not found")
    result = dict(row)
    result["is_all_day"] = bool(result["is_all_day"])
    result["is_cancelled"] = result.get("cancelled_at") is not None
    result["groups"] = _groups_for_revision(
        connection, result["event_revision_id"], active_only=True
    )
    result["recurrence_dates"] = _recurrence_dates(
        connection, result["event_revision_id"]
    )
    selected = None
    if occurrence_id is not None:
        selected = _occurrence(connection.execute(
            f"""
            SELECT {_OCCURRENCE_FIELDS} FROM event_occurrences
             WHERE id = ? AND event_id = ? AND status = 'scheduled'
            """,
            (str(occurrence_id), str(event_id)),
        ).fetchone())
    result["occurrence"] = selected
    is_recurring = bool(result["recurrence_rule"]) or any(
        item["kind"] == "include" for item in result["recurrence_dates"]
    )
    result["series"] = (
        _series_context(connection, str(event_id), selected, now)
        if is_recurring
        else None
    )
    return result


def review_queue(
    connection: sqlite3.Connection, *, status: str, limit: int, offset: int
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
               r.reviewed_by, r.review_note
          FROM events e JOIN event_revisions r ON r.id = e.current_revision_id
         WHERE e.archived_at IS NULL AND r.approval_status = ?
         ORDER BY r.submitted_at, e.id LIMIT ? OFFSET ?
        """,
        (status, limit, offset),
    ).fetchall()
    items = []
    for source in rows:
        item = dict(source)
        item["is_all_day"] = bool(item["is_all_day"])
        item["is_cancelled"] = bool(item.get("is_cancelled"))
        item["groups"] = _groups_for_revision(
            connection, item["revision_id"], active_only=False
        )
        items.append(item)
    return items


def get_admin_event(connection: sqlite3.Connection, event_id: UUID) -> dict:
    event = connection.execute(
        """
        SELECT e.id AS event_id, e.current_revision_id, e.published_revision_id,
               current.approval_status,
               current.revision_number AS current_revision_number,
               e.original_submitter_name, e.original_submitter_channel,
               e.original_submitter_contact, e.submitted_at, e.archived_at,
               e.cancelled_at, e.cancelled_by, e.cancel_reason, e.updated_at
          FROM events e JOIN event_revisions current ON current.id = e.current_revision_id
         WHERE e.id = ?
        """,
        (str(event_id),),
    ).fetchone()
    if event is None:
        raise NotFoundError("event not found")
    if event["archived_at"] is not None:
        raise NotFoundError("event not found")
    revisions = connection.execute(
        "SELECT * FROM event_revisions WHERE event_id = ? ORDER BY revision_number DESC",
        (str(event_id),),
    ).fetchall()
    result = dict(event)
    result["revisions"] = []
    for source in revisions:
        revision = dict(source)
        revision["is_all_day"] = bool(revision["is_all_day"])
        revision["groups"] = _groups_for_revision(
            connection, revision["id"], active_only=False
        )
        dates = connection.execute(
            """
            SELECT local_start, kind FROM event_revision_recurrence_dates
             WHERE event_revision_id = ? ORDER BY local_start
            """,
            (revision["id"],),
        ).fetchall()
        revision["recurrence_dates"] = [dict(item) for item in dates]
        result["revisions"].append(revision)
    return result
