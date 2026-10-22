"""RFC 5545 (iCalendar) export for published events and calendar ranges.

Link holders use this to carry events into their own calendar app (Apple
Calendar, Outlook, Google Calendar, ...). Single dates are emitted in UTC so
they land on the right wall-clock time everywhere. A repeating series keeps
its wall-clock time in the event's own timezone (with a VTIMEZONE), so a
weekly 9 PM event stays at 9 PM across daylight-saving changes; per-date
edits, cancellations, and skips travel as RECURRENCE-ID overrides and
EXDATEs. All-day events use DATE values with an exclusive end, matching the
read model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo

PRODID = "-//Event Calendar//Community Calendar//EN"
CALENDAR_NAME = "Community Calendar"
MAX_LINE_OCTETS = 75
# Explicit VTIMEZONE transitions run this many years past the later of the
# series start and today; clients fall back to their own tz database for
# well-known TZIDs beyond that.
VTIMEZONE_YEARS_AHEAD = 10


def escape_text(value: str | None) -> str:
    """Escape a TEXT value per RFC 5545 section 3.3.11."""
    if not value:
        return ""
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def fold_line(line: str) -> str:
    """Fold one content line to 75 octets, continuing with a single space."""
    encoded = line.encode("utf-8")
    if len(encoded) <= MAX_LINE_OCTETS:
        return line
    parts: list[str] = []
    chunk = encoded
    limit = MAX_LINE_OCTETS
    while len(chunk) > limit:
        cut = limit
        # Never split a multi-byte UTF-8 sequence.
        while cut > 0 and (chunk[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(chunk[:cut].decode("utf-8"))
        chunk = chunk[cut:]
        limit = MAX_LINE_OCTETS - 1  # continuation lines start with a space
    parts.append(chunk.decode("utf-8"))
    return "\r\n ".join(parts)


def format_dt_utc(value: datetime) -> str:
    """Format an aware datetime as a UTC `YYYYMMDDTHHMMSSZ` value."""
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def format_dt_local(value: datetime) -> str:
    """Format a wall-clock time as a floating `YYYYMMDDTHHMMSS` value."""
    return value.strftime("%Y%m%dT%H%M%S")


def format_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def _moment(value: str | datetime) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"expected an offset-aware datetime, got {value!r}")
    return moment


def _day(value: str | date) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(value)


def _wall(value: str | datetime) -> datetime:
    """A naive local slot (recurrence ids and recurrence dates)."""
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return moment.replace(tzinfo=None, microsecond=0)


def location_text(item: Mapping[str, Any]) -> str:
    """The physical place, or the online link for an online-only event."""
    place = " · ".join(
        part for part in (item.get("location_name"), item.get("location_address")) if part
    )
    return place or _http_url(item, "virtual_url") or ""


def description_text(item: Mapping[str, Any]) -> str:
    """The description, with the online link on its own line so calendar
    apps make it clickable even when LOCATION holds a street address."""
    description = item.get("description") or ""
    link = _http_url(item, "virtual_url")
    if not link:
        return description
    return f"{description}\n\nJoin online: {link}" if description else f"Join online: {link}"


def _http_url(item: Mapping[str, Any], field: str) -> str | None:
    url = (item.get(field) or "").strip()
    if url.startswith(("http://", "https://")):
        return url
    return None


def _event_url(item: Mapping[str, Any]) -> str | None:
    return _http_url(item, "event_url")


def _attachment_urls(item: Mapping[str, Any]) -> list[str]:
    """Linked file attachments (at most three) as ATTACH URIs."""
    urls = []
    attachments = item.get("attachments") or []
    if isinstance(attachments, str):
        return []
    for attachment in attachments:
        url = (attachment.get("url") if isinstance(attachment, Mapping) else "") or ""
        url = url.strip()
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
    return urls


def uid(leaf: UUID | str) -> str:
    """Stable UID so re-downloads update the same calendar entry."""
    return f"{leaf}@event-calendar"


def _utc_timing(item: Mapping[str, Any]) -> list[str]:
    if item.get("is_all_day"):
        return [
            f"DTSTART;VALUE=DATE:{format_date(_day(item['start_date']))}",
            f"DTEND;VALUE=DATE:{format_date(_day(item['end_date']))}",
        ]
    return [
        f"DTSTART:{format_dt_utc(_moment(item['starts_at']))}",
        f"DTEND:{format_dt_utc(_moment(item['ends_at']))}",
    ]


def _content(item: Mapping[str, Any], *, cancelled: bool) -> list[str]:
    lines = [f"SUMMARY:{escape_text(item.get('title') or 'Untitled event')}"]
    description = description_text(item)
    if description:
        lines.append(f"DESCRIPTION:{escape_text(description)}")
    location = location_text(item)
    if location:
        lines.append(f"LOCATION:{escape_text(location)}")
    url = _event_url(item)
    if url:
        lines.append(f"URL:{url}")
    for attachment_url in _attachment_urls(item):
        lines.append(f"ATTACH:{attachment_url}")
    names = [group["name"] for group in item.get("groups") or [] if group.get("name")]
    if names:
        lines.append(f"CATEGORIES:{','.join(escape_text(name) for name in names)}")
    # Cancelled content exports with STATUS:CANCELLED so an entry already in
    # someone's calendar updates instead of silently lingering.
    if cancelled:
        lines.append("STATUS:CANCELLED")
    return lines


def _component(lines: Iterable[str]) -> str:
    return "\r\n".join(fold_line(line) for line in lines)


def _vevent(
    leaf: UUID | str, sequence: int, now: datetime, body: list[str]
) -> str:
    return _component(
        [
            "BEGIN:VEVENT",
            f"UID:{uid(leaf)}",
            f"DTSTAMP:{format_dt_utc(now)}",
            f"SEQUENCE:{max(int(sequence or 0), 0)}",
            *body,
            "END:VEVENT",
        ]
    )


def single_vevent(
    item: Mapping[str, Any],
    *,
    leaf: UUID | str,
    sequence: int = 0,
    now: datetime | None = None,
) -> str:
    """Render one date (or a one-off event) as a standalone UTC VEVENT.

    `item` carries `is_all_day` plus either `starts_at`/`ends_at`
    (offset-aware) or `start_date`/`end_date` (end exclusive), and the
    content fields of the date it describes.
    """
    return _vevent(
        leaf,
        sequence,
        now or datetime.now(UTC),
        _utc_timing(item) + _content(item, cancelled=bool(item.get("is_cancelled"))),
    )


def _offset(value: timedelta) -> str:
    seconds = int(value.total_seconds())
    sign = "-" if seconds < 0 else "+"
    hours, rest = divmod(abs(seconds), 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{sign}{hours:02d}{minutes:02d}" + (f"{seconds:02d}" if seconds else "")


def _transitions(
    zone: ZoneInfo, start: datetime, end: datetime
) -> list[tuple[datetime, timedelta, timedelta]]:
    """UTC instants in [start, end) where the zone's UTC offset changes."""
    found: list[tuple[datetime, timedelta, timedelta]] = []
    step = timedelta(days=1)
    cursor = start
    before = cursor.astimezone(zone).utcoffset()
    while cursor < end:
        following = cursor + step
        after = following.astimezone(zone).utcoffset()
        if after != before:
            low, high = cursor, following
            while high - low > timedelta(seconds=1):
                middle = low + (high - low) / 2
                if middle.astimezone(zone).utcoffset() == before:
                    low = middle
                else:
                    high = middle
            found.append((high.replace(microsecond=0), before, after))
            before = after
        cursor = following
    return found


def vtimezone(tzname: str, start: datetime, end: datetime) -> str:
    """A VTIMEZONE for `tzname` covering [start, end) with explicit
    STANDARD/DAYLIGHT observances, one per offset change."""
    zone = ZoneInfo(tzname)
    first = start.astimezone(zone)
    observances = [(first, first.utcoffset(), first.utcoffset())]
    for instant, before, after in _transitions(zone, start, end):
        observances.append((instant.astimezone(zone), before, after))
    lines = ["BEGIN:VTIMEZONE", f"TZID:{tzname}"]
    for local, before, after in observances:
        kind = "DAYLIGHT" if local.dst() else "STANDARD"
        # An observance's DTSTART is the wall time just before it begins.
        onset = (local.astimezone(UTC) + before).replace(tzinfo=None)
        lines += [
            f"BEGIN:{kind}",
            f"DTSTART:{format_dt_local(onset)}",
            f"TZOFFSETFROM:{_offset(before)}",
            f"TZOFFSETTO:{_offset(after)}",
        ]
        if local.tzname():
            lines.append(f"TZNAME:{escape_text(local.tzname())}")
        lines.append(f"END:{kind}")
    lines.append("END:VTIMEZONE")
    return _component(lines)


def _rule(rule: str | None) -> str | None:
    text = (rule or "").strip().upper().removeprefix("RRULE:")
    return text or None


def series_components(
    event: Mapping[str, Any],
    occurrences: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> list[str]:
    """Render a repeating event as a master VEVENT plus its exceptions.

    `event` is the published event (revision timing, `recurrence_rule`,
    `recurrence_dates`, content, `is_cancelled`, `revision_number`).
    `occurrences` are its materialized rows: `recurrence_id` (the original
    local slot), `status`, timing, and `content` already merged with any
    per-date override, plus `is_cancelled` and `has_override`. Scheduled
    rows that diverge from the series become RECURRENCE-ID overrides;
    removed rows become EXDATEs.
    """
    now = now or datetime.now(UTC)
    tzname = event["timezone"]
    zone = ZoneInfo(tzname)
    all_day = bool(event["is_all_day"])
    sequence = event.get("revision_number") or 0
    event_cancelled = bool(event.get("is_cancelled"))

    if all_day:
        series_start_day = _day(event["start_date"])
        day_span = _day(event["end_date"]) - series_start_day

        def slot_value(slot: datetime) -> str:
            return format_date(slot.date())

        slot_param = ";VALUE=DATE"
        timing = [
            f"DTSTART;VALUE=DATE:{format_date(series_start_day)}",
            f"DTEND;VALUE=DATE:{format_date(series_start_day + day_span)}",
        ]
    else:
        series_start = _moment(event["starts_at"])
        duration = _moment(event["ends_at"]) - series_start

        def slot_value(slot: datetime) -> str:
            return format_dt_local(slot)

        slot_param = f";TZID={tzname}"
        timing = [
            f"DTSTART{slot_param}:{format_dt_local(series_start.astimezone(zone))}",
            f"DTEND{slot_param}:{format_dt_local((series_start + duration).astimezone(zone))}",
        ]

    includes: set[datetime] = set()
    excludes: set[datetime] = set()
    for row in event.get("recurrence_dates") or []:
        (includes if row["kind"] == "include" else excludes).add(_wall(row["local_start"]))

    overrides: list[str] = []
    scheduled: set[datetime] = set()
    removed: set[datetime] = set()
    for row in occurrences:
        slot = _wall(row["recurrence_id"])
        if row["status"] != "scheduled":
            removed.add(slot)
            continue
        scheduled.add(slot)
        if all_day:
            moved = (
                _day(row["start_date"]) != slot.date()
                or _day(row["end_date"]) != slot.date() + day_span
            )
        else:
            expected = slot.replace(tzinfo=zone).astimezone(UTC)
            moved = (
                _moment(row["starts_at"]) != expected
                or _moment(row["ends_at"]) != expected + duration
            )
        cancelled = bool(row.get("is_cancelled")) and not event_cancelled
        if not (moved or cancelled or row.get("has_override")):
            continue
        overrides.append(
            _vevent(
                event["event_id"],
                sequence,
                now,
                [
                    f"RECURRENCE-ID{slot_param}:{slot_value(slot)}",
                    *_utc_timing(row),
                    *_content(row, cancelled=bool(row.get("is_cancelled"))),
                ],
            )
        )

    master = [*timing]
    rule = _rule(event.get("recurrence_rule"))
    if rule:
        master.append(f"RRULE:{rule}")
    added = sorted(includes - removed)
    if added:
        master.append(f"RDATE{slot_param}:{','.join(slot_value(slot) for slot in added)}")
    # A slot that is scheduled again (for example after a restore) must not
    # also be excluded.
    skipped = sorted((excludes | removed) - scheduled)
    if skipped:
        master.append(f"EXDATE{slot_param}:{','.join(slot_value(slot) for slot in skipped)}")
    master += _content(event, cancelled=event_cancelled)

    components = []
    if not all_day:
        first_year = series_start.astimezone(zone).year
        last_year = max(first_year, now.year) + VTIMEZONE_YEARS_AHEAD
        components.append(
            vtimezone(
                tzname,
                datetime(first_year, 1, 1, tzinfo=UTC),
                datetime(last_year, 1, 1, tzinfo=UTC),
            )
        )
    components.append(_vevent(event["event_id"], sequence, now, master))
    return components + overrides


def build_calendar(
    components: Iterable[str],
    *,
    name: str | None = None,
    refresh: timedelta | None = None,
) -> str:
    """Wrap rendered components in a VCALENDAR.

    `name` and `refresh` describe a subscribable feed: the display name
    calendar apps show, and how often they should poll for changes.
    """
    header = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    if name:
        header.append(f"X-WR-CALNAME:{escape_text(name)}")
    if refresh:
        minutes = max(int(refresh.total_seconds() // 60), 1)
        header.append(f"REFRESH-INTERVAL;VALUE=DURATION:PT{minutes}M")
        header.append(f"X-PUBLISHED-TTL:PT{minutes}M")
    body = [_component(header), *components, "END:VCALENDAR"]
    return "\r\n".join(body) + "\r\n"


def google_calendar_url(
    *,
    title: str,
    starts_at: str | datetime | None = None,
    ends_at: str | datetime | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    is_all_day: bool = False,
    description: str = "",
    location: str = "",
    timezone: str | None = None,
    recurrence_rule: str | None = None,
) -> str:
    """Build a Google Calendar event-template link.

    A repeating event passes its `recurrence_rule` and `timezone`: times are
    then given as wall-clock times in that zone so the series keeps its
    local time across daylight-saving changes. Google's template cannot
    carry per-date exceptions; the .ics export does.
    """
    rule = _rule(recurrence_rule)
    if is_all_day:
        if start_date is None or end_date is None:
            raise ValueError("all-day exports require start_date and end_date")
        dates = f"{format_date(_day(start_date))}/{format_date(_day(end_date))}"
    else:
        if starts_at is None or ends_at is None:
            raise ValueError("timed exports require starts_at and ends_at")
        start, end = _moment(starts_at), _moment(ends_at)
        if rule and timezone:
            zone = ZoneInfo(timezone)
            dates = (
                f"{format_dt_local(start.astimezone(zone))}"
                f"/{format_dt_local(end.astimezone(zone))}"
            )
        else:
            dates = f"{format_dt_utc(start)}/{format_dt_utc(end)}"
    params = {"action": "TEMPLATE", "text": title or "Untitled event", "dates": dates}
    if description:
        params["details"] = description
    if location:
        params["location"] = location
    if rule:
        params["recur"] = f"RRULE:{rule}"
        if timezone and not is_all_day:
            params["ctz"] = timezone
    return f"https://calendar.google.com/calendar/render?{urlencode(params)}"


def slug_filename(title: str, fallback: UUID | str) -> str:
    """Attachment filename derived from the title, falling back to the id.

    Restricted to ASCII so it is safe in a Content-Disposition header.
    """
    slug = "".join(
        character.lower() if (character.isascii() and character.isalnum()) else "-"
        for character in (title or "")
    )
    slug = "-".join(part for part in slug.split("-") if part)[:60]
    return f"{slug or str(fallback)}.ics"
