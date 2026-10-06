from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

from .errors import ValidationError

MAX_OCCURRENCES_PER_WINDOW = 10_000


@dataclass(frozen=True, slots=True)
class OccurrenceSpec:
    recurrence_id: datetime
    is_exception: bool
    is_all_day: bool
    timezone: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None


def _valid_local(naive: datetime, zone: ZoneInfo) -> datetime | None:
    """Attach a zone, rejecting wall times skipped by a DST transition."""
    candidate = naive.replace(tzinfo=zone, fold=0)
    round_trip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    return candidate if round_trip == naive else None


def reattach_wall_time(naive: datetime, tzname: str) -> datetime:
    """Attach an IANA timezone to a naive wall time (fold=0).

    Rejects wall times skipped by a DST transition with a 422
    ValidationError. Used when a scoped edit moves the series' wall-clock
    start: the moved start must still exist in the event timezone.
    """
    zone = ZoneInfo(tzname)
    candidate = _valid_local(naive.replace(microsecond=0), zone)
    if candidate is None:
        raise ValidationError(
            f"{naive.isoformat()} does not exist in {tzname} "
            "because of a clock change"
        )
    return candidate


def truncate_rule_before(rule_text: str, until_token: str) -> str:
    """Return a copy of an RRULE that ends at `until_token`.

    Any existing COUNT/UNTIL is replaced by `UNTIL=<until_token>`, keeping the
    pattern (FREQ/INTERVAL/BY*) untouched. Timed series pass a UTC
    `YYYYMMDDTHHMMSSZ` instant; all-day series pass a `YYYYMMDD` date.
    """
    parts = [
        piece
        for piece in rule_text.upper().removeprefix("RRULE:").split(";")
        if piece
        and not piece.startswith("COUNT=")
        and not piece.startswith("UNTIL=")
    ]
    parts.append(f"UNTIL={until_token}")
    return ";".join(parts)


def _in_window(
    start: datetime | date,
    end: datetime | date,
    window_start: date,
    window_end: date,
    zone: ZoneInfo,
) -> bool:
    if isinstance(start, datetime):
        lower = datetime.combine(window_start, time.min, zone).astimezone(UTC)
        upper = datetime.combine(window_end, time.min, zone).astimezone(UTC)
        return start < upper and end > lower
    return start < window_end and end > window_start


def expand_revision(
    revision: Mapping[str, Any],
    recurrence_dates: Iterable[Mapping[str, Any]],
    window_start: date,
    window_end: date,
) -> list[OccurrenceSpec]:
    """Expand one revision into stable, local recurrence identifiers.

    The bounded output is suitable for persistence in event_occurrences.  RFC
    invalid local times (for example 02:30 during a spring-forward gap) are
    omitted. Ambiguous fall-back wall times use fold=0 consistently.
    """
    if window_end <= window_start:
        raise ValidationError("materialization window must have a positive length")

    zone = ZoneInfo(revision["timezone"])
    is_all_day = revision["is_all_day"]
    rule_text = revision.get("recurrence_rule")

    if is_all_day:
        base_local = datetime.combine(revision["start_date"], time.min)
        duration = revision["end_date"] - revision["start_date"]
    else:
        starts_at = revision["starts_at"]
        ends_at = revision["ends_at"]
        base_local = starts_at.astimezone(zone).replace(tzinfo=None)
        duration = ends_at - starts_at

    date_rows = list(recurrence_dates)
    include_ids = {
        row["local_start"].replace(microsecond=0)
        for row in date_rows
        if row["kind"] == "include"
    }
    exclude_ids = {
        row["local_start"].replace(microsecond=0)
        for row in date_rows
        if row["kind"] == "exclude"
    }

    generated_ids: set[datetime] = {base_local.replace(microsecond=0)}
    if rule_text:
        try:
            if is_all_day:
                rule_start = base_local
                cursor = datetime.combine(window_start, time.min) - duration
                upper = datetime.combine(window_end, time.min)
            else:
                rule_start = _valid_local(base_local, zone)
                if rule_start is None:
                    raise ValidationError(
                        "event start is not a real local time in its timezone"
                    )
                cursor = datetime.combine(window_start, time.min, zone) - duration
                upper = datetime.combine(window_end, time.min, zone)
            rule = rrulestr(rule_text, dtstart=rule_start)
            occurrence = rule.after(cursor, inc=True)
            count = 0
            while occurrence is not None and occurrence < upper:
                local_id = (
                    occurrence.replace(microsecond=0)
                    if occurrence.tzinfo is None
                    else occurrence.astimezone(zone).replace(tzinfo=None, microsecond=0)
                )
                generated_ids.add(local_id)
                count += 1
                if count > MAX_OCCURRENCES_PER_WINDOW:
                    raise ValidationError(
                        f"recurrence produces more than {MAX_OCCURRENCES_PER_WINDOW} "
                        "occurrences in the materialization window"
                    )
                occurrence = rule.after(occurrence, inc=False)
        except ValidationError:
            raise
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError(f"could not expand recurrence rule: {exc}") from exc

    recurrence_ids = (generated_ids | include_ids) - exclude_ids
    specs: list[OccurrenceSpec] = []
    for recurrence_id in sorted(recurrence_ids):
        local_start = _valid_local(recurrence_id, zone)
        if local_start is None:
            continue
        is_exception = (
            recurrence_id in include_ids and recurrence_id not in generated_ids
        )

        if is_all_day:
            if recurrence_id.time() != time.min:
                raise ValidationError("all-day RDATE/EXDATE values must be at 00:00:00")
            occurrence_start = recurrence_id.date()
            occurrence_end = occurrence_start + duration
            if not _in_window(
                occurrence_start, occurrence_end, window_start, window_end, zone
            ) and (rule_text or date_rows):
                continue
            specs.append(
                OccurrenceSpec(
                    recurrence_id=recurrence_id,
                    is_exception=is_exception,
                    is_all_day=True,
                    timezone=revision["timezone"],
                    start_date=occurrence_start,
                    end_date=occurrence_end,
                )
            )
        else:
            occurrence_start = local_start.astimezone(UTC)
            occurrence_end = occurrence_start + duration
            if not _in_window(
                occurrence_start, occurrence_end, window_start, window_end, zone
            ) and (rule_text or date_rows):
                continue
            specs.append(
                OccurrenceSpec(
                    recurrence_id=recurrence_id,
                    is_exception=is_exception,
                    is_all_day=False,
                    timezone=revision["timezone"],
                    starts_at=occurrence_start,
                    ends_at=occurrence_end,
                )
            )

    return specs
