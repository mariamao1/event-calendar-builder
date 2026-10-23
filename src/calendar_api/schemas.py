from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .errors import ValidationError

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# These limits reject accidental year/decade-long ranges while still allowing
# festivals, retreats, and long-running all-day listings. Recurrence describes
# how often an event repeats; it should not be encoded as one enormous event.
MAX_TIMED_EVENT_DURATION = timedelta(days=7)
MAX_ALL_DAY_EVENT_DURATION = timedelta(days=366)


def _timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("must be a valid IANA timezone") from exc
    return value


TimezoneName = Annotated[NonBlank, AfterValidator(_timezone)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Groups are identified on the calendar by a color. Colors are stored as
# lowercase #rrggbb so the frontend can derive band and text tints from them.
# New groups without an explicit color take the first palette entry no live
# group uses yet (cycling once every entry is taken).
GROUP_COLOR_PALETTE = (
    "#447d68",
    "#d8674b",
    "#4f8097",
    "#b98420",
    "#7c6ca6",
    "#b5527a",
    "#5e7d2f",
    "#8a6a4f",
)


def _group_color(value: str) -> str:
    # Normalize before matching: a pattern constraint would see the raw input.
    value = value.strip().lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", value):
        raise ValueError("must be a hex color like #447d68")
    return value


GroupColor = Annotated[str, AfterValidator(_group_color)]


def next_group_color(used_colors: list[str]) -> str:
    used = set(used_colors)
    for color in GROUP_COLOR_PALETTE:
        if color not in used:
            return color
    return GROUP_COLOR_PALETTE[len(used_colors) % len(GROUP_COLOR_PALETTE)]


class GroupCreate(StrictModel):
    slug: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            to_lower=True,
            min_length=1,
            max_length=80,
            pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        ),
    ]
    name: Annotated[NonBlank, StringConstraints(max_length=120)]
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=2000)
    ] = ""
    color: GroupColor | None = None


class GroupUpdate(StrictModel):
    name: Annotated[NonBlank, StringConstraints(max_length=120)] | None = None
    description: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None
    ) = None
    color: GroupColor | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def at_least_one_change(self) -> GroupUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        for field in ("name", "description", "color", "is_active"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class Submitter(StrictModel):
    name: Annotated[NonBlank, StringConstraints(max_length=160)]
    channel: Literal["email", "sms"]
    contact: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=320)
    ] = ""


# The calendar owner pins at most this many events to the featured strip.
MAX_FEATURED_EVENTS = 3

# RSVP answers. An event opts in with `rsvp_enabled`; signed-in users then
# answer whether they are attending. Canonical answers are "attending" and
# "declined"; common spellings ("yes"/"no", "going", "accepted"/"decline",
# "not_attending", ...) normalize to them so clients do not need to guess.
RSVP_STATUSES = ("attending", "declined")

_RSVP_STATUS_ALIASES = {
    "attending": "attending",
    "accepted": "attending",
    "yes": "attending",
    "going": "attending",
    "will_attend": "attending",
    "declined": "declined",
    "decline": "declined",
    "no": "declined",
    "not_attending": "declined",
    "cannot_attend": "declined",
    "cant_go": "declined",
}


def normalize_rsvp_status(value: str | None) -> str:
    """Map a user-supplied RSVP answer to "attending" or "declined".

    Raises a 422 ValidationError for anything else (including "maybe"),
    so callers get a clean API error instead of a silently stored answer.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError("status must be 'attending' or 'declined'")
    canonical = _RSVP_STATUS_ALIASES.get(
        value.strip().lower().replace("-", "_").replace(" ", "_")
    )
    if canonical is None:
        raise ValidationError(
            "status must be 'attending' or 'declined'; got "
            f"{value!r}"
        )
    return canonical


class RsvpInput(StrictModel):
    """One RSVP answer: either a status or a plain attending flag."""

    status: str | None = None
    attending: bool | None = None

    @model_validator(mode="after")
    def exactly_one_answer(self) -> RsvpInput:
        if (self.status is None) == (self.attending is None):
            raise ValueError("provide either 'status' or 'attending'")
        return self

    def resolved_status(self) -> str:
        if self.status is not None:
            return normalize_rsvp_status(self.status)
        assert self.attending is not None
        return "attending" if self.attending else "declined"


class RecurrenceDate(StrictModel):
    local_start: datetime
    kind: Literal["include", "exclude"]

    @field_validator("local_start")
    @classmethod
    def must_be_local(cls, value: datetime) -> datetime:
        if value.tzinfo is not None:
            raise ValueError(
                "must not include a UTC offset; it is local to the event timezone"
            )
        return value.replace(microsecond=0)


# A file attached to an event by link (at most MAX_ATTACHMENTS per event).
# Creators paste the file's URL plus an optional display name; an image URL
# renders as an image in the event detail view.
MAX_ATTACHMENTS = 3


class EventAttachment(StrictModel):
    url: Annotated[NonBlank, StringConstraints(max_length=2000)]
    name: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]
    ) = ""

    @field_validator("url")
    @classmethod
    def valid_attachment_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("must be an absolute http(s) URL")
        return value


# Files uploaded from the creator's own machine. They are stored by the
# server and served back to viewers, so they need no public URL of their
# own. Uploaded files and link attachments share one cap of MAX_ATTACHMENTS
# per event; each file is further bounded by MAX_ATTACHMENT_BYTES and an
# allowlist of image/PDF types.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

ATTACHMENT_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
}


def sanitize_attachment_filename(value: str | None) -> str:
    """A safe display name for an uploaded file.

    Keeps only the final path segment, collapses whitespace, and caps the
    length so storage and download headers stay predictable.
    """
    name = (value or "").split("/")[-1].split("\\")[-1].strip()
    name = " ".join(name.split())
    if not name or name in {".", ".."}:
        return "file"
    if len(name) > 120:
        stem, dot, extension = name.rpartition(".")
        if dot and len(extension) < 119:
            name = stem[: 119 - len(extension)] + dot + extension
        else:
            name = name[:120]
    return name


def attachment_mime_type(filename: str) -> str | None:
    """Canonical served type for an upload, from its extension."""
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ATTACHMENT_MIME_TYPES.get(extension)


class EventRevisionInput(StrictModel):
    title: Annotated[NonBlank, StringConstraints(max_length=240)]
    description: Annotated[str, StringConstraints(max_length=20_000)] = ""
    location_name: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=240)] | None
    ) = None
    location_address: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None
    ) = None
    virtual_url: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None
    ) = None
    event_url: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None
    ) = None
    is_all_day: bool = False
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None
    timezone: TimezoneName
    recurrence_rule: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None
    ) = None
    recurrence_dates: list[RecurrenceDate] = Field(default_factory=list, max_length=500)
    attachments: list[EventAttachment] = Field(
        default_factory=list, max_length=MAX_ATTACHMENTS
    )
    group_ids: list[UUID] = Field(default_factory=list, max_length=100)
    # Optional RSVP toggle: when true, signed-in users may RSVP to the event
    # and see each other's RSVPs. Off by default; revisioned like the rest of
    # the event content, so enabling it follows the normal review flow.
    rsvp_enabled: bool = False
    submitter: Submitter
    # Scoped edits of a recurring event. These ride alongside the full
    # revision payload so one form submission carries both the new content
    # and which occurrences it applies to. They are only honored on the
    # revision (edit) routes; creation rejects any non-series scope.
    scope: str | None = None
    occurrence_id: UUID | None = None

    @field_validator("group_ids")
    @classmethod
    def unique_groups(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("must not contain duplicates")
        return value

    @field_validator("attachments")
    @classmethod
    def unique_attachments(
        cls, value: list[EventAttachment]
    ) -> list[EventAttachment]:
        urls = [item.url for item in value]
        if len(set(urls)) != len(urls):
            raise ValueError("must not contain duplicate urls")
        return value

    @field_validator("recurrence_rule")
    @classmethod
    def valid_rrule(cls, value: str | None) -> str | None:
        if value is None:
            return None
        rule = value.upper().removeprefix("RRULE:")
        if any(token in rule for token in ("\n", "\r", "DTSTART", "RDATE", "EXDATE")):
            raise ValueError(
                "must contain one RRULE only, without DTSTART/RDATE/EXDATE"
            )
        return rule

    @field_validator("event_url", "virtual_url")
    @classmethod
    def valid_event_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("must be an absolute http(s) URL")
        return value

    @model_validator(mode="after")
    def timing_shape(self) -> EventRevisionInput:
        if self.is_all_day:
            if self.start_date is None or self.end_date is None:
                raise ValueError("all-day events require start_date and end_date")
            if self.starts_at is not None or self.ends_at is not None:
                raise ValueError("all-day events cannot have starts_at or ends_at")
            if self.end_date <= self.start_date:
                raise ValueError("end_date must be after start_date (exclusive)")
            if self.end_date - self.start_date > MAX_ALL_DAY_EVENT_DURATION:
                raise ValueError("all-day event duration cannot exceed 366 days")
        else:
            if self.starts_at is None or self.ends_at is None:
                raise ValueError("timed events require starts_at and ends_at")
            if self.start_date is not None or self.end_date is not None:
                raise ValueError("timed events cannot have start_date or end_date")
            if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
                raise ValueError("starts_at and ends_at must include UTC offsets")
            if self.ends_at <= self.starts_at:
                raise ValueError("ends_at must be after starts_at")
            if self.ends_at - self.starts_at > MAX_TIMED_EVENT_DURATION:
                raise ValueError("timed event duration cannot exceed 7 days")

        recurrence_ids = [item.local_start for item in self.recurrence_dates]
        if len(set(recurrence_ids)) != len(recurrence_ids):
            raise ValueError(
                "recurrence_dates must not contain duplicate local_start values"
            )
        if self.is_all_day and any(
            item.local_start.time() != time.min for item in self.recurrence_dates
        ):
            raise ValueError("all-day recurrence_dates must use 00:00:00")

        if self.recurrence_rule:
            zone = ZoneInfo(self.timezone)
            recurrence_start = (
                datetime.combine(self.start_date, time.min)
                if self.is_all_day
                else self.starts_at.astimezone(zone)
            )
            try:
                rule = rrulestr(self.recurrence_rule, dtstart=recurrence_start)
                first = rule.after(recurrence_start, inc=True)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"invalid RFC 5545 RRULE: {exc}") from exc
            # Expansion always keeps the event start as an occurrence, while
            # dateutil skips a DTSTART that does not match the rule. Requiring
            # the start to be the series' first date keeps COUNT honest and
            # stops an off-pattern extra date from appearing.
            if first is None:
                raise ValueError("recurrence ends before the event starts")
            if first != recurrence_start:
                raise ValueError(
                    "the event start must be the first date of its repeating "
                    "schedule; move the start onto the pattern"
                )
        return self


# Scope of an edit or removal targeting a recurring event:
# - "single": only the selected occurrence changes.
# - "future": the selected occurrence and every later one change.
# - "series": the entire series changes (the default, preserving old behavior).
_SCOPE_ALIASES = {
    "single": "single",
    "this": "single",
    "this_occurrence": "single",
    "occurrence": "single",
    "one": "single",
    "instance": "single",
    "future": "future",
    "this_and_future": "future",
    "this_and_following": "future",
    "following": "future",
    "onward": "future",
    "onwards": "future",
    "series": "series",
    "all": "series",
    "entire": "series",
    "entire_series": "series",
    "whole": "series",
    "whole_series": "series",
}

EDIT_SCOPES = ("single", "future", "series")


def normalize_scope(value: str | None) -> str:
    """Map a user-supplied scope to its canonical "single"/"future"/"series".

    Raises a 422 ValidationError for unknown values so callers get a clean
    API error instead of silently applying the wrong scope.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return "series"
    canonical = _SCOPE_ALIASES.get(value.strip().lower().replace("-", "_"))
    if canonical is None:
        raise ValidationError(
            "scope must be one of 'single' (this occurrence only), "
            "'future' (this and all future occurrences), or 'series' "
            f"(the entire series); got {value!r}"
        )
    return canonical


class ReviewInput(StrictModel):
    actor: Annotated[NonBlank, StringConstraints(max_length=160)]
    note: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] | None
    ) = None


class LoginInput(StrictModel):
    username: Annotated[NonBlank, StringConstraints(max_length=160)]
    password: str = Field(min_length=1, max_length=1024)


# Usernames are 3-32 letters, digits, dots, underscores, or hyphens, starting
# with a letter or digit, so they stay readable as submitter/actor labels.
USERNAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$"


class RegisterInput(StrictModel):
    username: Annotated[
        str, StringConstraints(strip_whitespace=True, pattern=USERNAME_PATTERN)
    ]
    password: str = Field(min_length=8, max_length=1024)


class UserRoleUpdate(StrictModel):
    is_admin: bool


class RemovalInput(BaseModel):
    """Optional note for event cancellation or deletion.

    Every field is optional and unknown fields are ignored so callers may
    send `{"actor": ...}`, `{"note": ...}`, `{"reason": ...}`, or no body at
    all. The effective actor defaults to the admin identity, falling back to
    "creator" for management-token callers.
    """

    model_config = ConfigDict(extra="ignore")

    actor: str | None = None
    note: str | None = None
    reason: str | None = None
    # Scoped cancellation/deletion of a recurring event. Mirrors the query
    # parameters of the same names; when both are present the body wins.
    scope: str | None = None
    occurrence_id: UUID | None = None
    occurrence: UUID | None = None
