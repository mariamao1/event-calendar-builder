from __future__ import annotations

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


class GroupUpdate(StrictModel):
    name: Annotated[NonBlank, StringConstraints(max_length=120)] | None = None
    description: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None
    ) = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def at_least_one_change(self) -> GroupUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class Submitter(StrictModel):
    name: Annotated[NonBlank, StringConstraints(max_length=160)]
    channel: Literal["email", "sms"]
    contact: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=320)
    ] = ""


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


class EventRevisionInput(StrictModel):
    title: Annotated[NonBlank, StringConstraints(max_length=240)]
    description: Annotated[str, StringConstraints(max_length=20_000)] = ""
    location_name: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=240)] | None
    ) = None
    location_address: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None
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
    group_ids: list[UUID] = Field(default_factory=list, max_length=100)
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

    @field_validator("event_url")
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
