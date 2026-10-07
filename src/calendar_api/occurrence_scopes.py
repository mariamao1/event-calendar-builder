from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Any

# Revision content fields that a single occurrence may override. Timing
# already lives per occurrence (starts_at/ends_at/start_date/end_date), and
# groups stay series-level, so only these descriptive fields need a merge.
CONTENT_OVERRIDE_FIELDS = (
    "title",
    "description",
    "location_name",
    "location_address",
    "event_url",
)

# Single-date removals that persist across later series-wide edits, stored in
# event_occurrences.instance_exception. "cancelled" keeps the date visible,
# flagged as cancelled; "skipped" removes it from the calendar. Both can be
# restored. Removals from a "future" scope truncate the series rule instead
# and are not recorded here.
EXCEPTION_CANCELLED = "cancelled"
EXCEPTION_SKIPPED = "skipped"
SKIPPED_REASON = "deleted single occurrence"


def build_content_override(payload: Any, published: dict) -> str | None:
    """Build the JSON override capturing how one occurrence diverges.

    Returns None when the payload content matches the published revision, so
    readers can treat a NULL column as "in sync with the series".
    """
    override = {}
    for field in CONTENT_OVERRIDE_FIELDS:
        new_value = getattr(payload, field, None) or None
        old_value = published.get(field) or None
        if (new_value or None) != (old_value or None):
            override[field] = new_value
    if not override:
        return None
    return json.dumps(override, sort_keys=True)


def parse_content_override(raw: Any) -> dict:
    """Parse a stored override blob into a plain dict (empty when absent)."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def apply_content_override(item: dict, raw: Any) -> dict:
    """Merge a stored override over revision-sourced content fields.

    Always sets `has_override` so readers and the UI can mark diverged dates.
    """
    override = parse_content_override(raw)
    item["has_override"] = bool(override)
    for field in CONTENT_OVERRIDE_FIELDS:
        if field in override:
            item[field] = override[field]
    return item


def _slot_day(recurrence_id: Any) -> str:
    """The local calendar day of a slot (a naive datetime or ISO string)."""
    if isinstance(recurrence_id, datetime):
        return recurrence_id.date().isoformat()
    return str(recurrence_id)[:10]


def match_exceptions_by_day(
    orphans: Iterable[dict], slot_ids: Iterable[Any]
) -> list[tuple[dict, Any]]:
    """Pair single-date exceptions whose slot vanished with that day's new slot.

    `recurrence_id` is a slot's local wall-clock start, so a series edit that
    moves the time of day mints new slots. A date skipped or cancelled on its
    own should stay skipped or cancelled on that day, so each orphaned
    exception row (no longer produced by the series) is paired with the new
    slot on the same local day. Days with several orphans or several new
    slots are ambiguous and pair nothing.
    """
    orphans_by_day: dict[str, list[dict]] = defaultdict(list)
    for orphan in orphans:
        orphans_by_day[_slot_day(orphan["recurrence_id"])].append(orphan)
    slots_by_day: dict[str, list[Any]] = defaultdict(list)
    for slot_id in slot_ids:
        slots_by_day[_slot_day(slot_id)].append(slot_id)
    pairs = []
    for day, day_orphans in orphans_by_day.items():
        slots = slots_by_day.get(day, [])
        if len(day_orphans) == 1 and len(slots) == 1:
            pairs.append((day_orphans[0], slots[0]))
    return pairs
