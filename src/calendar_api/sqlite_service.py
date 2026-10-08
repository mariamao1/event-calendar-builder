from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

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
from .schemas import (
    EventRevisionInput,
    GroupCreate,
    GroupUpdate,
    ReviewInput,
    next_group_color,
)
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
        SELECT g.id, g.slug, g.name, g.color
          FROM event_revision_groups rg
          JOIN groups g ON g.id = rg.group_id
         WHERE rg.event_revision_id = ? AND g.deleted_at IS NULL {suffix}
         ORDER BY g.name, g.id
        """,
        (revision_id,),
    ).fetchall()
    return [dict(item) for item in rows]


_GROUP_FIELDS = "id, slug, name, description, color, is_active, created_at, updated_at"


def _group(connection: sqlite3.Connection, group_id: str) -> dict:
    result = dict(
        connection.execute(
            f"SELECT {_GROUP_FIELDS} FROM groups WHERE id = ?", (group_id,)
        ).fetchone()
    )
    result["is_active"] = bool(result["is_active"])
    return result


def list_groups(
    connection: sqlite3.Connection, *, include_inactive: bool = False
) -> list[dict]:
    rows = connection.execute(
        f"""
        SELECT {_GROUP_FIELDS}
          FROM groups
         WHERE deleted_at IS NULL AND (is_active = 1 OR ?)
         ORDER BY name, id
        """,
        (int(include_inactive),),
    ).fetchall()
    return [{**dict(item), "is_active": bool(item["is_active"])} for item in rows]


def create_group(connection: sqlite3.Connection, payload: GroupCreate) -> dict:
    group_id = str(uuid4())
    now = _timestamp()
    color = payload.color or next_group_color([
        row["color"]
        for row in connection.execute(
            "SELECT color FROM groups WHERE deleted_at IS NULL"
        ).fetchall()
    ])
    try:
        connection.execute(
            """
            INSERT INTO groups
              (id, slug, name, description, color, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (group_id, payload.slug, payload.name, payload.description, color, now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(f"group slug '{payload.slug}' already exists") from exc
    return _group(connection, group_id)


def update_group(
    connection: sqlite3.Connection, group_id: UUID, payload: GroupUpdate
) -> dict:
    changes = payload.model_dump(exclude_unset=True)
    if "is_active" in changes:
        changes["is_active"] = int(changes["is_active"])
    changes["updated_at"] = _timestamp()
    assignments = ", ".join(f"{field} = ?" for field in changes)
    cursor = connection.execute(
        f"UPDATE groups SET {assignments} WHERE id = ? AND deleted_at IS NULL",
        (*changes.values(), str(group_id)),
    )
    if cursor.rowcount == 0:
        raise NotFoundError("group not found")
    return _group(connection, str(group_id))


def delete_group(connection: sqlite3.Connection, group_id: UUID) -> None:
    """Delete a group, leaving a tombstone for history to reference.

    Its events stay on the calendar: they simply no longer carry the group,
    and an event whose only groups are deleted reads as ungrouped. The slug
    becomes free for a new group.
    """
    now = _timestamp()
    cursor = connection.execute(
        """
        UPDATE groups SET deleted_at = ?, is_active = 0, updated_at = ?
         WHERE id = ? AND deleted_at IS NULL
        """,
        (now, now, str(group_id)),
    )
    if cursor.rowcount == 0:
        raise NotFoundError("group not found")


def _require_active_groups(
    connection: sqlite3.Connection, group_ids: list[UUID]
) -> None:
    if not group_ids:
        return
    placeholders = ",".join("?" for _ in group_ids)
    count = connection.execute(
        f"SELECT count(*) FROM groups WHERE id IN ({placeholders}) AND is_active = 1"
        " AND deleted_at IS NULL",
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
        """
        SELECT id, instance_exception FROM event_occurrences
         WHERE event_id = ? AND recurrence_id = ?
        """,
        (event_id, values["recurrence_id"]),
    ).fetchone()
    now = _timestamp()
    if existing:
        if not update_existing:
            return False
        # A series-wide approval refreshes every retained occurrence from the
        # new revision, clearing per-occurrence edits: the latest change to
        # the series always persists over earlier single/future edits. A
        # single-date cancellation or skip is kept, though: the series still
        # produces the date, and it should stay cancelled or skipped. Skipped
        # rows refresh their timing invisibly, ready for a restore.
        exception = existing["instance_exception"]
        skipped = exception == EXCEPTION_SKIPPED
        connection.execute(
            """
            UPDATE event_occurrences SET
              source_revision_id = ?, status = ?, version = version + ?,
              is_exception = ?, is_all_day = ?, starts_at = ?, ends_at = ?,
              start_date = ?, end_date = ?, timezone = ?, cancellation_reason = ?,
              content_override = NULL, instance_cancelled = ?,
              updated_at = ?
             WHERE id = ?
            """,
            (
                revision_id,
                "cancelled" if skipped else "scheduled",
                0 if skipped else 1,
                values["is_exception"],
                values["is_all_day"],
                values["starts_at"],
                values["ends_at"],
                values["start_date"],
                values["end_date"],
                values["timezone"],
                SKIPPED_REASON if skipped else None,
                int(exception == EXCEPTION_CANCELLED),
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


def _apply_exception(
    connection: sqlite3.Connection, event_id: str, recurrence_id: str, exception: str
) -> None:
    """Mark one slot cancelled or skipped on its own, unless it already is."""
    if exception == EXCEPTION_SKIPPED:
        connection.execute(
            """
            UPDATE event_occurrences SET status = 'cancelled',
              cancellation_reason = ?, instance_cancelled = 0,
              instance_exception = ?, version = version + 1, updated_at = ?
             WHERE event_id = ? AND recurrence_id = ? AND instance_exception IS NULL
            """,
            (SKIPPED_REASON, exception, _timestamp(), event_id, recurrence_id),
        )
    else:
        connection.execute(
            """
            UPDATE event_occurrences SET instance_cancelled = 1,
              instance_exception = ?, version = version + 1, updated_at = ?
             WHERE event_id = ? AND recurrence_id = ? AND instance_exception IS NULL
            """,
            (exception, _timestamp(), event_id, recurrence_id),
        )


def _carry_over_exceptions(
    connection: sqlite3.Connection,
    event_id: str,
    desired: set[str],
    *,
    from_rid: str = "",
) -> None:
    """Keep single-date removals on their day when the series moves slots.

    Call after the new slots exist. An exception whose slot (at or after
    `from_rid`) the series no longer produces moves to that day's new slot
    when there is exactly one (a time-of-day change); otherwise it lapses,
    because the series itself no longer has that date.
    """
    orphans = [
        dict(item)
        for item in connection.execute(
            """
            SELECT id, recurrence_id, instance_exception FROM event_occurrences
             WHERE event_id = ? AND instance_exception IS NOT NULL
               AND recurrence_id >= ?
            """,
            (event_id, from_rid),
        ).fetchall()
        if item["recurrence_id"] not in desired
    ]
    if not orphans:
        return
    connection.executemany(
        "UPDATE event_occurrences SET instance_exception = NULL WHERE id = ?",
        [(item["id"],) for item in orphans],
    )
    for orphan, slot in match_exceptions_by_day(orphans, desired):
        _apply_exception(connection, event_id, slot, orphan["instance_exception"])


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
        if revision["recurrence_rule"] is None and not _recurrence_dates(
            connection, revision["id"]
        ):
            # An event that no longer repeats has no dates to skip.
            connection.execute(
                """
                UPDATE event_occurrences SET instance_exception = NULL
                 WHERE event_id = ? AND instance_exception IS NOT NULL
                """,
                (event["id"],),
            )
        for spec in specs:
            _insert_occurrence(
                connection, event["id"], revision["id"], spec, update_existing=True
            )
        desired = {_local_timestamp(spec.recurrence_id) for spec in specs}
        _carry_over_exceptions(connection, event["id"], desired)
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
               o.timezone, o.content_override, o.instance_cancelled,
               e.cancelled_at IS NOT NULL AS is_cancelled,
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
               JOIN groups assigned_g ON assigned_g.id = assigned_rg.group_id
                WHERE assigned_rg.event_revision_id = r.id
                  AND assigned_g.deleted_at IS NULL
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
        # A scoped per-date cancellation flags the date while the series
        # itself stays published; a series-wide cancellation flags every date.
        item["is_cancelled"] = bool(item.get("is_cancelled")) or bool(
            item.pop("instance_cancelled", 0)
        )
        # A diverged date carries its own content merged over the series.
        apply_content_override(item, item.pop("content_override", None))
        item["groups"] = _groups_for_revision(
            connection, item["revision_id"], active_only=True
        )
        items.append(item)
    return items, has_more


_SERIES_UPCOMING_LIMIT = 6
_SERIES_SKIPPED_LIMIT = 20
_OCCURRENCE_FIELDS = """
    id AS occurrence_id, recurrence_id, is_exception, is_all_day, starts_at,
    ends_at, start_date, end_date, timezone, content_override,
    instance_cancelled, instance_exception
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
    result["instance_cancelled"] = bool(result.get("instance_cancelled"))
    # Per-date cancellation; callers OR this with the event-level flag.
    result["is_cancelled"] = bool(result.get("instance_cancelled"))
    result["has_override"] = bool(parse_content_override(result.get("content_override")))
    return result


def _series_context(
    connection: sqlite3.Connection,
    event_id: str,
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
    skipped = connection.execute(
        f"""
        SELECT {_OCCURRENCE_FIELDS} FROM event_occurrences
         WHERE event_id = ? AND status = 'cancelled' AND instance_exception = ?
           AND ((is_all_day = 0 AND ends_at > ?) OR (is_all_day = 1 AND end_date > ?))
         ORDER BY {_OCCURRENCE_KEY}, id LIMIT ?
        """,
        (
            event_id,
            EXCEPTION_SKIPPED,
            _timestamp(now),
            now.date().isoformat(),
            _SERIES_SKIPPED_LIMIT,
        ),
    ).fetchall()
    coverage = connection.execute(
        """
        SELECT window_end_exclusive FROM event_occurrence_materializations
         WHERE event_id = ?
        """,
        (event_id,),
    ).fetchone()

    def _public(item: dict | None) -> dict | None:
        # Series neighbours carry timing plus divergence/cancel flags; the
        # raw override blob stays server-side.
        if item is None:
            return None
        item.pop("content_override", None)
        item.pop("instance_cancelled", None)
        return item

    return {
        "previous": _public(previous),
        "next": _public(following),
        "upcoming": [_public(_occurrence(item)) for item in upcoming],
        "upcoming_count": upcoming_count,
        "skipped": [_public(_occurrence(item)) for item in skipped],
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
               JOIN groups assigned_g ON assigned_g.id = assigned_rg.group_id
                WHERE assigned_rg.event_revision_id = r.id
                  AND assigned_g.deleted_at IS NULL
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
        if selected is not None:
            # The selected date shows its own content when it diverges from
            # the series (scoped single/future edit), and its own cancelled
            # flag alongside the event-level one.
            base = {field: result.get(field) for field in (
                "title",
                "description",
                "location_name",
                "location_address",
                "event_url",
            )}
            selected.update(base)
            apply_content_override(selected, selected.pop("content_override", None))
            selected["is_cancelled"] = bool(result.get("is_cancelled")) or bool(
                selected.pop("instance_cancelled", False)
            )
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


def _scoped_event(connection: sqlite3.Connection, event_id: UUID) -> dict:
    """Load an event for a scoped operation (deleted events read as missing)."""
    row = connection.execute(
        "SELECT * FROM events WHERE id = ?", (str(event_id),)
    ).fetchone()
    if row is None or row["archived_at"] is not None:
        raise NotFoundError("event not found")
    return dict(row)


def _published_for_scope(
    connection: sqlite3.Connection, event: dict
) -> tuple[dict, list[dict]]:
    """Load the published revision a scoped operation applies on top of."""
    if event["published_revision_id"] is None:
        raise NotFoundError("event is not published")
    revision = _revision(
        connection.execute(
            "SELECT * FROM event_revisions WHERE id = ?",
            (event["published_revision_id"],),
        ).fetchone()
    )
    return revision, _recurrence_dates(connection, event["published_revision_id"])


def _require_no_pending(connection: sqlite3.Connection, event: dict) -> dict:
    """Refuse revision-minting scoped ops while an edit awaits review.

    "Future" operations create a new approved revision; running one on top of
    a pending edit would strand that edit off the current chain, so callers
    must approve or reject it first. Single-date operations touch only one
    occurrence row and stay compatible with a later approval (which then wins,
    per the latest-change rule).
    """
    current = connection.execute(
        "SELECT revision_number, approval_status FROM event_revisions WHERE id = ?",
        (event["current_revision_id"],),
    ).fetchone()
    if current is not None and current["approval_status"] == "pending":
        raise ConflictError(
            "event has a pending edit awaiting review; approve or reject it "
            "before changing future dates"
        )
    return dict(current)


def _scope_target(
    connection: sqlite3.Connection, event_id: str, occurrence_id: UUID
) -> dict:
    """Load the targeted occurrence; gone dates read as missing."""
    row = connection.execute(
        "SELECT * FROM event_occurrences WHERE id = ? AND event_id = ?",
        (str(occurrence_id), event_id),
    ).fetchone()
    if row is None or row["status"] != "scheduled":
        raise NotFoundError("occurrence not found or no longer scheduled")
    return dict(row)


def _require_recurring(revision: dict, dates: list[dict]) -> None:
    if not revision["recurrence_rule"] and not any(
        item["kind"] == "include" for item in dates
    ):
        raise ValidationError(
            "event does not repeat; choose the entire series for one-off events"
        )


def _published_group_ids(connection: sqlite3.Connection, revision_id: str) -> set[str]:
    # Deleted groups are gone from the event: they are neither compared
    # against an edit's groups nor carried into revisions minted from it.
    rows = connection.execute(
        """
        SELECT rg.group_id FROM event_revision_groups rg
        JOIN groups g ON g.id = rg.group_id
         WHERE rg.event_revision_id = ? AND g.deleted_at IS NULL
        """,
        (revision_id,),
    ).fetchall()
    return {item["group_id"] for item in rows}


def _check_scoped_recurrence(
    connection: sqlite3.Connection,
    payload: EventRevisionInput,
    revision: dict,
    dates: list[dict],
    revision_id: str,
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
    if not allow_group_changes and {
        str(group_id) for group_id in payload.group_ids
    } != _published_group_ids(connection, revision_id):
        raise ValidationError(
            "changing groups applies to the entire series; "
            "edit the series instead of a single date"
        )


def _log_scope_action(
    connection: sqlite3.Connection,
    event_id: str,
    revision_id: str,
    action: str,
    actor: str,
    note: str | None,
    now: datetime | None = None,
) -> tuple[str, str]:
    occurred_at = _timestamp(now)
    action_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO event_review_actions
          (id, event_id, event_revision_id, action, actor, note, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (action_id, event_id, revision_id, action, actor, note, occurred_at),
    )
    return action_id, occurred_at


def edit_single_occurrence(
    connection: sqlite3.Connection,
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
    if payload.is_all_day:
        is_all_day, starts_at, ends_at = (
            1,
            None,
            None,
        )
        start_date, end_date = (
            payload.start_date.isoformat(),
            payload.end_date.isoformat(),
        )
    else:
        is_all_day, start_date, end_date = 0, None, None
        starts_at, ends_at = _timestamp(payload.starts_at), _timestamp(payload.ends_at)
    updated_at = _timestamp()
    connection.execute(
        """
        UPDATE event_occurrences SET
          source_revision_id = ?, is_exception = 1, is_all_day = ?,
          starts_at = ?, ends_at = ?, start_date = ?, end_date = ?,
          timezone = ?, content_override = ?,
          instance_cancelled = (instance_exception IS 'cancelled'),
          version = version + 1, updated_at = ?
         WHERE id = ?
        """,
        (
            revision["id"],
            is_all_day,
            starts_at,
            ends_at,
            start_date,
            end_date,
            payload.timezone,
            override,
            updated_at,
            target["id"],
        ),
    )
    return {
        "event_id": event["id"],
        "occurrence_id": target["id"],
        "scope": "single",
        "version": target["version"] + 1,
        "has_override": override is not None,
        "updated_at": updated_at,
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
    group_ids: list[str],
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
    connection: sqlite3.Connection,
    event_id: str,
    revision_number: int,
    supersedes_revision_id: str,
    validated: EventRevisionInput,
    *,
    actor: str,
    note: str | None,
    now_text: str,
) -> dict:
    revision_id = str(uuid4())
    contact = normalize_contact(
        validated.submitter.channel, validated.submitter.contact
    )
    connection.execute(
        """
        INSERT INTO event_revisions (
          id, event_id, revision_number, supersedes_revision_id,
          approval_status, title, description, location_name, location_address,
          event_url, is_all_day, starts_at, ends_at, start_date, end_date,
          timezone, recurrence_rule, submitted_by_name, submitted_by_channel,
          submitted_by_contact, submitted_at, reviewed_at, reviewed_by,
          review_note
        ) VALUES (
          ?, ?, ?, ?, 'approved', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
          ?, ?, ?, ?
        )
        """,
        (
            revision_id,
            event_id,
            revision_number,
            supersedes_revision_id,
            validated.title,
            validated.description,
            validated.location_name or None,
            validated.location_address or None,
            validated.event_url or None,
            int(validated.is_all_day),
            _timestamp(validated.starts_at) if validated.starts_at else None,
            _timestamp(validated.ends_at) if validated.ends_at else None,
            validated.start_date.isoformat() if validated.start_date else None,
            validated.end_date.isoformat() if validated.end_date else None,
            validated.timezone,
            validated.recurrence_rule,
            validated.submitter.name,
            validated.submitter.channel,
            contact,
            now_text,
            now_text,
            actor,
            note,
        ),
    )
    connection.executemany(
        """
        INSERT INTO event_revision_groups
          (event_revision_id, group_id, created_at) VALUES (?, ?, ?)
        """,
        [(revision_id, str(group_id), now_text) for group_id in validated.group_ids],
    )
    connection.executemany(
        """
        INSERT INTO event_revision_recurrence_dates
          (event_revision_id, local_start, kind, created_at) VALUES (?, ?, ?, ?)
        """,
        [
            (
                revision_id,
                _local_timestamp(item.local_start),
                item.kind,
                now_text,
            )
            for item in validated.recurrence_dates
        ],
    )
    return dict(
        connection.execute(
            "SELECT * FROM event_revisions WHERE id = ?", (revision_id,)
        ).fetchone()
    )


def _approve_scoped_revision(
    connection: sqlite3.Connection,
    event: dict,
    revision_row: dict,
    validated: EventRevisionInput,
    *,
    scope_rid: str,
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
    revision = _revision(revision_row)
    expand_dates = [
        {"local_start": item.local_start, "kind": item.kind}
        for item in validated.recurrence_dates
    ]
    materialization = connection.execute(
        "SELECT * FROM event_occurrence_materializations WHERE event_id = ?",
        (event["id"],),
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
    specs = expand_revision(revision, expand_dates, window_start, window_end)

    in_scope = [
        spec for spec in specs if _local_timestamp(spec.recurrence_id) >= scope_rid
    ]
    for spec in in_scope:
        _insert_occurrence(
            connection, event["id"], revision_row["id"], spec, update_existing=True
        )
    desired = {_local_timestamp(spec.recurrence_id) for spec in in_scope}
    _carry_over_exceptions(connection, event["id"], desired, from_rid=scope_rid)
    affected = 0
    for item in connection.execute(
        """
        SELECT id, recurrence_id FROM event_occurrences
         WHERE event_id = ? AND status = 'scheduled'
        """,
        (event["id"],),
    ).fetchall():
        if item["recurrence_id"] < scope_rid or item["recurrence_id"] in desired:
            continue
        if mode == "mark":
            connection.execute(
                """
                UPDATE event_occurrences SET instance_cancelled = 1,
                  version = version + 1, updated_at = ? WHERE id = ?
                """,
                (_timestamp(), item["id"]),
            )
        else:
            connection.execute(
                """
                UPDATE event_occurrences SET source_revision_id = ?,
                  status = 'cancelled', version = version + 1,
                  cancellation_reason = ?, updated_at = ? WHERE id = ?
                """,
                (revision_row["id"], removal_reason, _timestamp(), item["id"]),
            )
        affected += 1
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
            revision_row["id"],
            window_start.isoformat(),
            window_end.isoformat(),
            _timestamp(),
        ),
    )
    return in_scope, affected


def _commit_scoped_revision(
    connection: sqlite3.Connection,
    event: dict,
    validated: EventRevisionInput,
    *,
    scope_rid: str,
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
        "SELECT revision_number FROM event_revisions WHERE id = ?",
        (event["current_revision_id"],),
    ).fetchone()
    now_text = _timestamp(now)
    revision_row = _insert_approved_revision(
        connection,
        event["id"],
        current["revision_number"] + 1,
        event["current_revision_id"],
        validated,
        actor=actor,
        note=note,
        now_text=now_text,
    )
    connection.execute(
        """
        UPDATE events SET current_revision_id = ?, published_revision_id = ?,
          updated_at = ? WHERE id = ?
        """,
        (revision_row["id"], revision_row["id"], now_text, event["id"]),
    )
    in_scope, affected = _approve_scoped_revision(
        connection,
        event,
        revision_row,
        validated,
        scope_rid=scope_rid,
        mode=mode,
        removal_reason=removal_reason,
        past_days=past_days,
        future_months=future_months,
        now=now,
    )
    action_id, reviewed_at = _log_scope_action(
        connection, event["id"], revision_row["id"], "approve", actor, note, now
    )
    return {
        "revision_row": revision_row,
        "in_scope": in_scope,
        "affected": affected,
        "review_action_id": action_id,
        "reviewed_at": reviewed_at,
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
        if payload.start_date is None or target["start_date"] is None:
            raise ValidationError("all-day scoped edits require start_date")
        target_date = date.fromisoformat(target["start_date"])
        return timedelta(days=(payload.start_date - target_date).days)
    if payload.starts_at is None or target["starts_at"] is None:
        raise ValidationError("timed scoped edits require starts_at")
    target_start = datetime.fromisoformat(target["starts_at"])
    target_wall = target_start.astimezone(zone).replace(tzinfo=None, microsecond=0)
    payload_wall = payload.starts_at.astimezone(zone).replace(
        tzinfo=None, microsecond=0
    )
    return payload_wall - target_wall


def edit_future_occurrences(
    connection: sqlite3.Connection,
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
        if revision["starts_at"] is None:
            raise ValidationError("timed scoped edits require a timed series")
        zone = ZoneInfo(revision["timezone"])
        series_wall = revision["starts_at"].astimezone(zone).replace(
            tzinfo=None, microsecond=0
        )
        new_start_utc = reattach_wall_time(
            series_wall + delta, revision["timezone"]
        ).astimezone(UTC)
        duration = payload.ends_at - payload.starts_at
        new_end_utc = new_start_utc + duration
        new_start_date = new_end_date = None
        new_starts_at, new_ends_at = new_start_utc, new_end_utc
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
            if _local_timestamp(item["local_start"]) < scope_rid
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
            recurrence_dates=[
                {
                    "local_start": item["local_start"].isoformat()
                    if isinstance(item["local_start"], datetime)
                    else item["local_start"],
                    "kind": item["kind"],
                }
                for item in new_dates
            ],
            group_ids=[str(group_id) for group_id in payload.group_ids],
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
        "revision_id": committed["revision_row"]["id"],
        "revision_number": committed["revision_row"]["revision_number"],
        "approval_status": "approved",
        "scope": "future",
        "occurrence_id": target["id"],
        "occurrence_count": len(committed["in_scope"]),
        "review_action_id": committed["review_action_id"],
        "reviewed_at": committed["reviewed_at"],
    }


def _pin_past_content(
    connection: sqlite3.Connection,
    event_id: str,
    old_revision: dict,
    new_revision: EventRevisionInput,
    scope_rid: str,
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
    for item in connection.execute(
        """
        SELECT id, content_override FROM event_occurrences
         WHERE event_id = ? AND status = 'scheduled' AND recurrence_id < ?
        """,
        (event_id, scope_rid),
    ).fetchall():
        existing = parse_content_override(item["content_override"])
        merged = {**frozen, **existing}
        if merged == existing:
            continue
        connection.execute(
            """
            UPDATE event_occurrences SET content_override = ?,
              version = version + 1, updated_at = ? WHERE id = ?
            """,
            (json.dumps(merged, sort_keys=True), _timestamp(), item["id"]),
        )
        pinned += 1
    return pinned


def _truncate_series_before(
    revision: dict, dates: list[dict], scope_rid: str
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
    scope_day = date.fromisoformat(scope_rid[:10])
    expand_dates = [
        {"local_start": item["local_start"], "kind": item["kind"]} for item in dates
    ]
    specs = expand_revision(
        revision, expand_dates, series_start, scope_day + timedelta(days=1)
    )
    kept = [
        spec
        for spec in specs
        if _local_timestamp(spec.recurrence_id) < scope_rid
    ]
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
        {"local_start": item["local_start"], "kind": item["kind"]}
        for item in dates
        if _local_timestamp(item["local_start"]) < scope_rid
    ]
    return new_rule, kept_dates


def _commit_truncation(
    connection: sqlite3.Connection,
    event: dict,
    revision: dict,
    dates: list[dict],
    scope_rid: str,
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
    group_ids = sorted(_published_group_ids(connection, revision["id"]))
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
            recurrence_dates=[
                {
                    "local_start": item["local_start"].isoformat()
                    if isinstance(item["local_start"], datetime)
                    else item["local_start"],
                    "kind": item["kind"],
                }
                for item in kept_dates
            ],
            group_ids=group_ids,
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
    action_id, occurred_at = _log_scope_action(
        connection,
        event["id"],
        committed["revision_row"]["id"],
        removal_action,
        actor,
        note,
        now,
    )
    return {
        "revision_id": committed["revision_row"]["id"],
        "affected": committed["affected"],
        "review_action_id": action_id,
        "reviewed_at": occurred_at,
    }


def cancel_occurrences(
    connection: sqlite3.Connection,
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
    event = _scoped_event(connection, event_id)
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
    revision, dates = _published_for_scope(connection, event)
    _require_recurring(revision, dates)
    target = _scope_target(connection, event["id"], occurrence_id)
    if scope == "single":
        if target.get("instance_cancelled"):
            raise ConflictError("occurrence is already cancelled")
        occurred_at = _timestamp(now)
        connection.execute(
            """
            UPDATE event_occurrences SET instance_cancelled = 1,
              instance_exception = ?, version = version + 1, updated_at = ?
             WHERE id = ?
            """,
            (EXCEPTION_CANCELLED, occurred_at, target["id"]),
        )
        action_id, reviewed_at = _log_scope_action(
            connection, event["id"], revision["id"], "cancel", actor, note, now
        )
        return {
            "event_id": event["id"],
            "occurrence_id": target["id"],
            "scope": "single",
            "is_cancelled": True,
            "cancelled_at": occurred_at,
            "version": target["version"] + 1,
            "review_action_id": action_id,
            "reviewed_at": reviewed_at,
        }
    _require_no_pending(connection, event)
    truncated = _commit_truncation(
        connection,
        event,
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
        "event_id": event["id"],
        "occurrence_id": target["id"],
        "scope": "future",
        "is_cancelled": True,
        "cancelled_occurrence_count": truncated["affected"],
        "revision_id": truncated["revision_id"],
        "review_action_id": truncated["review_action_id"],
        "reviewed_at": truncated["reviewed_at"],
    }


def delete_occurrences(
    connection: sqlite3.Connection,
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
    event = _scoped_event(connection, event_id)
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
    revision, dates = _published_for_scope(connection, event)
    _require_recurring(revision, dates)
    target = _scope_target(connection, event["id"], occurrence_id)
    if scope == "single":
        occurred_at = _timestamp(now)
        connection.execute(
            """
            UPDATE event_occurrences SET status = 'cancelled',
              version = version + 1, cancellation_reason = ?,
              instance_cancelled = 0, instance_exception = ?,
              updated_at = ? WHERE id = ?
            """,
            (SKIPPED_REASON, EXCEPTION_SKIPPED, occurred_at, target["id"]),
        )
        action_id, reviewed_at = _log_scope_action(
            connection, event["id"], revision["id"], "delete", actor, note, now
        )
        return {
            "event_id": event["id"],
            "occurrence_id": target["id"],
            "scope": "single",
            "deleted_occurrence_count": 1,
            "archived_at": occurred_at,
            "review_action_id": action_id,
            "reviewed_at": reviewed_at,
        }
    _require_no_pending(connection, event)
    truncated = _commit_truncation(
        connection,
        event,
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
        "event_id": event["id"],
        "occurrence_id": target["id"],
        "scope": "future",
        "deleted_occurrence_count": truncated["affected"],
        "revision_id": truncated["revision_id"],
        "review_action_id": truncated["review_action_id"],
        "reviewed_at": truncated["reviewed_at"],
    }


def restore_occurrence(
    connection: sqlite3.Connection,
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
    event = _scoped_event(connection, event_id)
    _require_removal_permission(
        event, is_admin=is_admin, management_token=management_token
    )
    revision, _ = _published_for_scope(connection, event)
    row = connection.execute(
        "SELECT * FROM event_occurrences WHERE id = ? AND event_id = ?",
        (str(occurrence_id), event["id"]),
    ).fetchone()
    if row is None:
        raise NotFoundError("occurrence not found")
    exception = row["instance_exception"]
    if exception is None:
        raise ConflictError(
            "only a date cancelled or deleted on its own can be restored"
        )
    occurred_at = _timestamp(now)
    connection.execute(
        """
        UPDATE event_occurrences SET status = 'scheduled',
          cancellation_reason = NULL, instance_cancelled = 0,
          instance_exception = NULL, version = version + 1, updated_at = ?
         WHERE id = ?
        """,
        (occurred_at, row["id"]),
    )
    action_id, reviewed_at = _log_scope_action(
        connection, event["id"], revision["id"], "restore", actor, note, now
    )
    return {
        "event_id": event["id"],
        "occurrence_id": row["id"],
        "restored": exception,
        "version": row["version"] + 1,
        "restored_at": occurred_at,
        "review_action_id": action_id,
        "reviewed_at": reviewed_at,
    }
