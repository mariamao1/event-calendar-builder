from __future__ import annotations

import json
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
