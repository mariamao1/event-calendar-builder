from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta
from fastapi import Depends, FastAPI, Header, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import service as postgres_service
from . import sqlite_service
from .config import Settings
from .database import Database, SQLiteDatabase, create_database
from .errors import ApiError, ValidationError
from .rate_limit import RateLimiter
from .schemas import (
    EventRevisionInput,
    GroupCreate,
    GroupUpdate,
    LoginInput,
    RemovalInput,
    ReviewInput,
    normalize_scope,
)
from .security import SessionStore, tokens_equal, verify_password

logger = logging.getLogger(__name__)


def _parse_boundary(
    value: str, zone: ZoneInfo, name: str
) -> tuple[datetime, date, bool]:
    try:
        if "T" not in value and " " not in value:
            day = date.fromisoformat(value)
            return datetime.combine(day, time.min, zone).astimezone(UTC), day, True
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{name} must be an ISO 8601 date or datetime") from exc
    if moment.tzinfo is None:
        raise ValidationError(f"{name} datetime must include a UTC offset")
    local = moment.astimezone(zone)
    return moment.astimezone(UTC), local.date(), False


def _calendar_range(
    start: str,
    end: str,
    timezone: str,
    settings: Settings,
) -> tuple[datetime, datetime, date, date]:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError("timezone must be a valid IANA timezone") from exc
    start_at, start_date, _ = _parse_boundary(start, zone, "start")
    end_at, end_date, end_is_date = _parse_boundary(end, zone, "end")
    if end_at <= start_at:
        raise ValidationError("end must be after start; ranges are [start, end)")
    if end_at - start_at > timedelta(days=366):
        raise ValidationError("calendar ranges cannot exceed 366 days")

    # A datetime range ending during a local day overlaps that all-day bucket;
    # midnight and plain-date endpoints retain normal exclusive semantics.
    end_local = end_at.astimezone(zone)
    if not end_is_date and end_local.time() != time.min:
        end_date += timedelta(days=1)

    today = datetime.now(UTC).date()
    coverage_start = today - timedelta(days=settings.materialization_past_days)
    coverage_end = today + relativedelta(months=settings.materialization_future_months)
    if start_date < coverage_start or end_date > coverage_end:
        raise ValidationError(
            f"range is outside guaranteed coverage [{coverage_start}, {coverage_end})"
        )
    return start_at, end_at, start_date, end_date


def create_app(
    settings: Settings | None = None,
    database: Database | SQLiteDatabase | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    database = database or create_database(settings.database_url)
    service = sqlite_service if database.dialect == "sqlite" else postgres_service

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.open()
        try:
            yield
        finally:
            database.close()

    app = FastAPI(
        title="Event Calendar API",
        version="0.1.0",
        lifespan=lifespan,
    )
    sessions = SessionStore()
    limiter = RateLimiter()
    app.state.database = database
    app.state.service = service
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.rate_limiter = limiter
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                "X-Admin-Key",
                "X-Calendar-Token",
                "X-Event-Management-Token",
                "Authorization",
            ],
        )

    if not settings.calendar_access_token:
        logger.warning(
            "CALENDAR_ACCESS_TOKEN is not set; public routes are open. "
            "Set it in production so the calendar is only reachable via its link."
        )

    @app.middleware("http")
    async def robots_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        # The calendar is private to its link: keep every response,
        # including errors, out of search engine indexes.
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=exc.headers,
        )

    def client_ip(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
        if request.client is not None:
            return request.client.host
        return "unknown"

    def enforce_rate_limit(
        request: Request, *, scope: str, limit: int, window_seconds: int
    ) -> None:
        allowed, retry_after = limiter.check(
            f"{scope}:{client_ip(request)}",
            limit=limit,
            window_seconds=window_seconds,
        )
        if not allowed:
            raise ApiError(
                429,
                "rate_limited",
                "too many requests; slow down and retry later",
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )

    def require_link(request: Request) -> None:
        """Gate no-account routes on the unguessable calendar link token.

        The token travels as `?token=` (or `?access_token=`) so the calendar
        stays reachable by link, or as an `X-Calendar-Token` header for API
        clients. Comparison is constant-time and enforced server-side.
        """
        expected = settings.calendar_access_token
        if not expected:
            return
        provided = (
            request.query_params.get("token")
            or request.query_params.get("access_token")
            or request.headers.get("x-calendar-token")
        )
        if not tokens_equal(provided, expected):
            raise ApiError(
                401, "unauthorized", "a valid calendar link token is required"
            )

    def admin_identity(
        x_admin_key: str | None, authorization: str | None
    ) -> str | None:
        bearer: str | None = None
        if authorization and authorization.lower().startswith("bearer "):
            bearer = authorization[7:].strip() or None
        if bearer:
            username = sessions.validate(bearer)
            if username is not None:
                return username
        expected = settings.admin_api_key
        if expected and tokens_equal(x_admin_key, expected):
            return settings.admin_username
        return None

    def require_admin(
        request: Request,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> str:
        """Require real admin authentication: a login session or the API key.

        Sessions come from POST /api/v1/admin/login (password verified
        against a salted PBKDF2 hash). The X-Admin-Key service key remains
        accepted so existing operators keep working.
        """
        identity = admin_identity(x_admin_key, authorization)
        if identity is not None:
            return identity
        expected = settings.admin_api_key
        if not expected and not settings.admin_password_hash:
            raise ApiError(
                503,
                "admin_not_configured",
                "no admin credential is configured",
            )
        raise ApiError(401, "unauthorized", "valid admin credentials are required")

    def require_event_editor(
        event_id: UUID,
        management_token: str | None,
        x_admin_key: str | None,
        authorization: str | None,
    ) -> None:
        identity = admin_identity(x_admin_key, authorization)
        with database.connection() as connection:
            if not service.event_exists(connection, event_id):
                # Deleted events no longer exist, regardless of credentials.
                raise ApiError(404, "not_found", "event not found")
            if identity is not None:
                return
            matches = service.event_management_token_matches(
                connection, event_id, management_token
            )
        if not matches:
            raise ApiError(
                401,
                "unauthorized",
                "the event creator's management token or admin credentials are required",
            )

    admin = Depends(require_admin)
    link = Depends(require_link)
    frontend_dir = Path(__file__).with_name("frontend")

    def resolve_scope(
        body_scope: str | None,
        body_occurrence: UUID | None,
        query_scope: str | None,
        query_occurrence: UUID | None,
        *,
        default: str = "series",
    ) -> tuple[str, UUID | None]:
        """Merge body and query scope selectors into one canonical scope.

        The body wins when both are present. "single" (this occurrence only)
        and "future" (this and all future occurrences) require the targeted
        occurrence id; "series" (the entire series) is the default.
        """
        raw = body_scope if body_scope else (query_scope if query_scope else default)
        scope = normalize_scope(raw)
        occurrence = body_occurrence if body_occurrence is not None else query_occurrence
        if scope in ("single", "future") and occurrence is None:
            raise ValidationError(
                f"scope '{scope}' requires the targeted occurrence: pass "
                "?occurrence=<id> (or occurrence_id in the body)"
            )
        return scope, occurrence

    def require_path_occurrence(
        body_occurrence: UUID | None, path_occurrence: UUID
    ) -> None:
        """Reject a body occurrence id that disagrees with the URL path."""
        if body_occurrence is not None and body_occurrence != path_occurrence:
            raise ValidationError(
                "occurrence_id in the body must match the occurrence in the URL"
            )

    # The browser client is kept alongside the API package so a normal
    # `calendar-api serve` command is the only process needed in production.
    app.mount(
        "/static",
        StaticFiles(directory=frontend_dir / "static"),
        name="static",
    )

    @app.get("/", dependencies=[link], include_in_schema=False)
    def month_view() -> FileResponse:
        return FileResponse(frontend_dir / "index.html", media_type="text/html")

    @app.get("/health")
    def health() -> dict:
        with database.connection() as connection:
            connection.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/robots.txt", include_in_schema=False)
    def robots_txt() -> PlainTextResponse:
        # The calendar is private to its link: ask every crawler to stay out.
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    @app.post("/api/v1/admin/login")
    def admin_login(payload: LoginInput, request: Request) -> dict:
        """Authenticate with username + password and receive a session token.

        Passwords are verified against the salted PBKDF2 hash in
        ADMIN_PASSWORD_HASH using a constant-time comparison. Attempts are
        rate limited per client IP to slow down credential guessing.
        """
        enforce_rate_limit(
            request,
            scope="login",
            limit=settings.login_rate_limit_max,
            window_seconds=settings.login_rate_limit_window_seconds,
        )
        if not settings.admin_password_hash:
            raise ApiError(
                503,
                "admin_not_configured",
                "ADMIN_PASSWORD_HASH is not configured",
            )
        username_ok = tokens_equal(payload.username, settings.admin_username)
        password_ok = verify_password(
            payload.password, settings.admin_password_hash
        )
        if not (username_ok and password_ok):
            raise ApiError(401, "unauthorized", "invalid username or password")
        token, expires_at = sessions.create(
            settings.admin_username,
            ttl_seconds=settings.admin_session_ttl_seconds,
        )
        return {
            "token": token,
            "token_type": "bearer",
            "username": settings.admin_username,
            "expires_at": datetime.fromtimestamp(expires_at, UTC).isoformat(),
        }

    @app.post("/api/v1/admin/logout", dependencies=[admin])
    def admin_logout(request: Request) -> dict:
        """Revoke the calling admin session token, if one was used."""
        authorization = request.headers.get("authorization", "")
        bearer: str | None = None
        if authorization.lower().startswith("bearer "):
            bearer = authorization[7:].strip() or None
        return {"revoked": sessions.revoke(bearer)}

    @app.get("/api/v1/groups", dependencies=[link])
    def groups() -> dict:
        with database.connection() as connection:
            items = service.list_groups(connection, include_inactive=False)
        return {"items": items}

    @app.get("/api/v1/admin/groups", dependencies=[admin])
    def admin_groups() -> dict:
        with database.connection() as connection:
            items = service.list_groups(connection, include_inactive=True)
        return {"items": items}

    @app.post(
        "/api/v1/admin/groups",
        status_code=status.HTTP_201_CREATED,
        dependencies=[admin],
    )
    def post_group(payload: GroupCreate) -> dict:
        with database.transaction() as connection:
            return service.create_group(connection, payload)

    @app.patch("/api/v1/admin/groups/{group_id}", dependencies=[admin])
    def patch_group(group_id: UUID, payload: GroupUpdate) -> dict:
        with database.transaction() as connection:
            return service.update_group(connection, group_id, payload)

    @app.post(
        "/api/v1/events",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[link],
    )
    def post_event(payload: EventRevisionInput, request: Request) -> dict:
        # Anyone holding the link can submit with just a name, so submissions
        # are rate limited per client IP to bound spam and abuse.
        enforce_rate_limit(
            request,
            scope="submit",
            limit=settings.submit_rate_limit_max,
            window_seconds=settings.submit_rate_limit_window_seconds,
        )
        if (
            normalize_scope(payload.scope) != "series"
            or payload.occurrence_id is not None
        ):
            raise ValidationError(
                "scope and occurrence_id apply to edits of a recurring event, "
                "not to new submissions"
            )
        with database.transaction() as connection:
            return service.create_event(connection, payload)

    @app.post(
        "/api/v1/admin/events",
        status_code=status.HTTP_201_CREATED,
    )
    def post_admin_event(
        payload: EventRevisionInput,
        actor: str = Depends(require_admin),
    ) -> dict:
        """Create and publish an admin-authored event in one transaction."""
        if (
            normalize_scope(payload.scope) != "series"
            or payload.occurrence_id is not None
        ):
            raise ValidationError(
                "scope and occurrence_id apply to edits of a recurring event, "
                "not to new events"
            )
        with database.transaction() as connection:
            created = service.create_event(connection, payload)
            approved = service.approve_revision(
                connection,
                created["event_id"],
                created["revision_id"],
                ReviewInput(actor=actor, note="Created and approved by admin"),
                past_days=settings.materialization_past_days,
                future_months=settings.materialization_future_months,
            )
        return {**created, **approved}

    @app.post(
        "/api/v1/events/{event_id}/revisions",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def post_revision(
        event_id: UUID,
        payload: EventRevisionInput,
        query_scope: Annotated[str | None, Query(alias="scope")] = None,
        query_occurrence: Annotated[UUID | None, Query(alias="occurrence")] = None,
        query_occurrence_id: Annotated[
            UUID | None, Query(alias="occurrence_id")
        ] = None,
        x_event_management_token: Annotated[
            str | None, Header(alias="X-Event-Management-Token")
        ] = None,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Submit an edit; scoped edits of a recurring event apply at once.

        With `scope=single` (this occurrence only) or `scope=future` (this and
        all future occurrences) plus the targeted `?occurrence=` id, the edit
        applies immediately for any authorized editor, like cancellation and
        deletion. Without a scope the edit targets the entire series and keeps
        the usual review flow (pending for creators, immediate for admins).
        """
        require_event_editor(
            event_id, x_event_management_token, x_admin_key, authorization
        )
        scope, occurrence = resolve_scope(
            payload.scope,
            payload.occurrence_id,
            query_scope,
            query_occurrence or query_occurrence_id,
        )
        if scope != "series":
            assert occurrence is not None
            editor = admin_identity(x_admin_key, authorization) or "creator"
            with database.transaction() as connection:
                if scope == "single":
                    return service.edit_single_occurrence(
                        connection, event_id, occurrence, payload, actor=editor
                    )
                return service.edit_future_occurrences(
                    connection,
                    event_id,
                    occurrence,
                    payload,
                    actor=editor,
                    past_days=settings.materialization_past_days,
                    future_months=settings.materialization_future_months,
                )
        with database.transaction() as connection:
            return service.create_revision(connection, event_id, payload)

    @app.post(
        "/api/v1/admin/events/{event_id}/revisions",
        status_code=status.HTTP_201_CREATED,
    )
    def post_admin_revision(
        event_id: UUID,
        payload: EventRevisionInput,
        actor: str = Depends(require_admin),
        query_scope: Annotated[str | None, Query(alias="scope")] = None,
        query_occurrence: Annotated[UUID | None, Query(alias="occurrence")] = None,
        query_occurrence_id: Annotated[
            UUID | None, Query(alias="occurrence_id")
        ] = None,
    ) -> dict:
        """Create and immediately publish an admin-authored edit."""
        scope, occurrence = resolve_scope(
            payload.scope,
            payload.occurrence_id,
            query_scope,
            query_occurrence or query_occurrence_id,
        )
        if scope != "series":
            assert occurrence is not None
            with database.transaction() as connection:
                if scope == "single":
                    return service.edit_single_occurrence(
                        connection, event_id, occurrence, payload, actor=actor
                    )
                return service.edit_future_occurrences(
                    connection,
                    event_id,
                    occurrence,
                    payload,
                    actor=actor,
                    past_days=settings.materialization_past_days,
                    future_months=settings.materialization_future_months,
                )
        with database.transaction() as connection:
            created = service.create_revision(connection, event_id, payload)
            approved = service.approve_revision(
                connection,
                event_id,
                created["revision_id"],
                ReviewInput(actor=actor, note="Edited and approved by admin"),
                past_days=settings.materialization_past_days,
                future_months=settings.materialization_future_months,
            )
        return {**created, **approved}

    @app.get("/api/v1/events/{event_id}/manage")
    def editable_event(
        event_id: UUID,
        x_event_management_token: Annotated[
            str | None, Header(alias="X-Event-Management-Token")
        ] = None,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        require_event_editor(
            event_id, x_event_management_token, x_admin_key, authorization
        )
        with database.connection() as connection:
            return service.get_editable_event(connection, event_id)

    @app.get("/api/v1/events/{event_id}", dependencies=[link])
    def event(event_id: UUID, occurrence: UUID | None = None) -> dict:
        """One published event; `occurrence` selects a date of its series."""
        with database.connection() as connection:
            return service.get_published_event(
                connection, event_id, occurrence_id=occurrence
            )

    @app.get("/api/v1/calendar", dependencies=[link])
    def calendar(
        start: str,
        end: str,
        timezone: str = "UTC",
        group: Annotated[list[str] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=2000)] = 500,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict:
        start_at, end_at, start_date, end_date = _calendar_range(
            start, end, timezone, settings
        )
        group_slugs = list(dict.fromkeys(group or []))
        with database.connection() as connection:
            items, has_more = service.calendar_occurrences(
                connection,
                start_at=start_at,
                end_at=end_at,
                start_date=start_date,
                end_date=end_date,
                group_slugs=group_slugs,
                limit=limit,
                offset=offset,
            )
        return {
            "items": items,
            "meta": {
                "start": start,
                "end": end,
                "timezone": timezone,
                "groups": group_slugs,
                "count": len(items),
                "has_more": has_more,
                "limit": limit,
                "offset": offset,
            },
        }

    @app.get("/api/v1/admin/events", dependencies=[admin])
    def admin_events(
        approval_status: Literal["pending", "approved", "rejected", "revoked"] = Query(
            "pending", alias="status"
        ),
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict:
        with database.connection() as connection:
            items = service.review_queue(
                connection, status=approval_status, limit=limit, offset=offset
            )
        return {"items": items, "meta": {"limit": limit, "offset": offset}}

    @app.get("/api/v1/admin/events/{event_id}", dependencies=[admin])
    def admin_event(event_id: UUID) -> dict:
        with database.connection() as connection:
            return service.get_admin_event(connection, event_id)

    @app.post(
        "/api/v1/admin/events/{event_id}/revisions/{revision_id}/approve",
        dependencies=[admin],
    )
    def approve(event_id: UUID, revision_id: UUID, payload: ReviewInput) -> dict:
        with database.transaction() as connection:
            return service.approve_revision(
                connection,
                event_id,
                revision_id,
                payload,
                past_days=settings.materialization_past_days,
                future_months=settings.materialization_future_months,
            )

    @app.post(
        "/api/v1/admin/events/{event_id}/revisions/{revision_id}/reject",
        dependencies=[admin],
    )
    def reject(event_id: UUID, revision_id: UUID, payload: ReviewInput) -> dict:
        with database.transaction() as connection:
            return service.reject_revision(connection, event_id, revision_id, payload)

    @app.post("/api/v1/admin/events/{event_id}/revoke", dependencies=[admin])
    def revoke(event_id: UUID, payload: ReviewInput) -> dict:
        with database.transaction() as connection:
            return service.revoke_event(connection, event_id, payload)

    def removal_actor(
        payload: RemovalInput | None, identity: str | None
    ) -> tuple[str, str | None]:
        """Resolve who cancelled/deleted and the note they left."""
        actor = (payload.actor.strip() if payload and payload.actor else "") or (
            identity or "creator"
        )
        note = None
        if payload is not None:
            note = payload.note or payload.reason or None
        return actor, note

    @app.post("/api/v1/events/{event_id}/cancel")
    def cancel_event(
        event_id: UUID,
        payload: RemovalInput | None = None,
        query_scope: Annotated[str | None, Query(alias="scope")] = None,
        query_occurrence: Annotated[UUID | None, Query(alias="occurrence")] = None,
        query_occurrence_id: Annotated[
            UUID | None, Query(alias="occurrence_id")
        ] = None,
        x_event_management_token: Annotated[
            str | None, Header(alias="X-Event-Management-Token")
        ] = None,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Cancel an event while keeping it visible, marked as cancelled.

        Cancellation means "this event is cancelled": the published content
        stays on the calendar and in the detail view with `is_cancelled`,
        because people may already have planned around it. Only an admin or
        the event creator (via `X-Event-Management-Token`) may cancel. For a
        recurring event, `scope=single` (this occurrence only) or
        `scope=future` (this and all future occurrences) plus the targeted
        `?occurrence=` id cancels only those dates instead of the series.
        """
        identity = admin_identity(x_admin_key, authorization)
        actor, note = removal_actor(payload, identity)
        scope, occurrence = resolve_scope(
            payload.scope if payload else None,
            (payload.occurrence_id or payload.occurrence) if payload else None,
            query_scope,
            query_occurrence or query_occurrence_id,
        )
        with database.transaction() as connection:
            return service.cancel_occurrences(
                connection,
                event_id,
                scope=scope,
                occurrence_id=occurrence,
                actor=actor,
                note=note,
                is_admin=identity is not None,
                management_token=x_event_management_token,
                past_days=settings.materialization_past_days,
                future_months=settings.materialization_future_months,
            )

    @app.delete("/api/v1/events/{event_id}")
    def delete_event(
        event_id: UUID,
        payload: RemovalInput | None = None,
        query_scope: Annotated[str | None, Query(alias="scope")] = None,
        query_occurrence: Annotated[UUID | None, Query(alias="occurrence")] = None,
        query_occurrence_id: Annotated[
            UUID | None, Query(alias="occurrence_id")
        ] = None,
        x_event_management_token: Annotated[
            str | None, Header(alias="X-Event-Management-Token")
        ] = None,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Delete an event so it no longer exists.

        Deletion means "this event should no longer exist": it leaves the
        calendar, the detail view, the review queue, and creator reads. Only
        an admin or the event creator (via `X-Event-Management-Token`) may
        delete. For a recurring event, `scope=single` (this occurrence only)
        or `scope=future` (this and all future occurrences) plus the targeted
        `?occurrence=` id removes only those dates instead of the series.
        """
        identity = admin_identity(x_admin_key, authorization)
        actor, note = removal_actor(payload, identity)
        scope, occurrence = resolve_scope(
            payload.scope if payload else None,
            (payload.occurrence_id or payload.occurrence) if payload else None,
            query_scope,
            query_occurrence or query_occurrence_id,
        )
        with database.transaction() as connection:
            return service.delete_occurrences(
                connection,
                event_id,
                scope=scope,
                occurrence_id=occurrence,
                actor=actor,
                note=note,
                is_admin=identity is not None,
                management_token=x_event_management_token,
                past_days=settings.materialization_past_days,
                future_months=settings.materialization_future_months,
            )

    @app.post("/api/v1/events/{event_id}/occurrences/{occurrence_id}/edit")
    def edit_occurrence(
        event_id: UUID,
        occurrence_id: UUID,
        payload: EventRevisionInput,
        query_scope: Annotated[str | None, Query(alias="scope")] = None,
        x_event_management_token: Annotated[
            str | None, Header(alias="X-Event-Management-Token")
        ] = None,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Edit one date of a recurring event without touching the series.

        Defaults to `scope=single` (this occurrence only); `scope=future`
        edits this and all future occurrences. The whole series is edited
        through the revision routes instead (`scope=series` is rejected
        here). Applies immediately for any authorized editor, like
        cancellation and deletion.
        """
        require_event_editor(
            event_id, x_event_management_token, x_admin_key, authorization
        )
        require_path_occurrence(payload.occurrence_id, occurrence_id)
        scope, _ = resolve_scope(
            payload.scope,
            payload.occurrence_id,
            query_scope,
            occurrence_id,
            default="single",
        )
        if scope == "series":
            raise ValidationError(
                "the occurrence routes edit one date or future dates; "
                "edit the entire series through the revision routes"
            )
        editor = admin_identity(x_admin_key, authorization) or "creator"
        with database.transaction() as connection:
            if scope == "single":
                return service.edit_single_occurrence(
                    connection, event_id, occurrence_id, payload, actor=editor
                )
            return service.edit_future_occurrences(
                connection,
                event_id,
                occurrence_id,
                payload,
                actor=editor,
                past_days=settings.materialization_past_days,
                future_months=settings.materialization_future_months,
            )

    @app.post("/api/v1/events/{event_id}/occurrences/{occurrence_id}/cancel")
    def cancel_occurrence(
        event_id: UUID,
        occurrence_id: UUID,
        payload: RemovalInput | None = None,
        query_scope: Annotated[str | None, Query(alias="scope")] = None,
        x_event_management_token: Annotated[
            str | None, Header(alias="X-Event-Management-Token")
        ] = None,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Cancel one date (`scope=single`, the default) or future dates of a
        recurring event, keeping each date visible, flagged as cancelled."""
        require_event_editor(
            event_id, x_event_management_token, x_admin_key, authorization
        )
        identity = admin_identity(x_admin_key, authorization)
        actor, note = removal_actor(payload, identity)
        if payload:
            require_path_occurrence(
                payload.occurrence_id or payload.occurrence, occurrence_id
            )
        scope, _ = resolve_scope(
            payload.scope if payload else None,
            (payload.occurrence_id or payload.occurrence) if payload else None,
            query_scope,
            occurrence_id,
            default="single",
        )
        if scope == "series":
            raise ValidationError(
                "the occurrence routes remove one date or future dates; "
                "cancel the entire series through the event routes"
            )
        with database.transaction() as connection:
            return service.cancel_occurrences(
                connection,
                event_id,
                scope=scope,
                occurrence_id=occurrence_id,
                actor=actor,
                note=note,
                is_admin=identity is not None,
                management_token=x_event_management_token,
                past_days=settings.materialization_past_days,
                future_months=settings.materialization_future_months,
            )

    @app.delete("/api/v1/events/{event_id}/occurrences/{occurrence_id}")
    def delete_occurrence(
        event_id: UUID,
        occurrence_id: UUID,
        payload: RemovalInput | None = None,
        query_scope: Annotated[str | None, Query(alias="scope")] = None,
        x_event_management_token: Annotated[
            str | None, Header(alias="X-Event-Management-Token")
        ] = None,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Delete one date (`scope=single`, the default) or future dates of a
        recurring event, removing only those dates from the calendar."""
        require_event_editor(
            event_id, x_event_management_token, x_admin_key, authorization
        )
        identity = admin_identity(x_admin_key, authorization)
        actor, note = removal_actor(payload, identity)
        if payload:
            require_path_occurrence(
                payload.occurrence_id or payload.occurrence, occurrence_id
            )
        scope, _ = resolve_scope(
            payload.scope if payload else None,
            (payload.occurrence_id or payload.occurrence) if payload else None,
            query_scope,
            occurrence_id,
            default="single",
        )
        if scope == "series":
            raise ValidationError(
                "the occurrence routes remove one date or future dates; "
                "delete the entire series through the event routes"
            )
        with database.transaction() as connection:
            return service.delete_occurrences(
                connection,
                event_id,
                scope=scope,
                occurrence_id=occurrence_id,
                actor=actor,
                note=note,
                is_admin=identity is not None,
                management_token=x_event_management_token,
                past_days=settings.materialization_past_days,
                future_months=settings.materialization_future_months,
            )

    @app.post("/api/v1/admin/events/{event_id}/cancel")
    def admin_cancel_event(
        event_id: UUID,
        payload: RemovalInput | None = None,
        actor: str = Depends(require_admin),
        query_scope: Annotated[str | None, Query(alias="scope")] = None,
        query_occurrence: Annotated[UUID | None, Query(alias="occurrence")] = None,
        query_occurrence_id: Annotated[
            UUID | None, Query(alias="occurrence_id")
        ] = None,
    ) -> dict:
        """Admin-only alias of event cancellation (see `cancel_event`)."""
        resolved, note = removal_actor(payload, actor)
        scope, occurrence = resolve_scope(
            payload.scope if payload else None,
            (payload.occurrence_id or payload.occurrence) if payload else None,
            query_scope,
            query_occurrence or query_occurrence_id,
        )
        with database.transaction() as connection:
            return service.cancel_occurrences(
                connection,
                event_id,
                scope=scope,
                occurrence_id=occurrence,
                actor=resolved,
                note=note,
                is_admin=True,
                past_days=settings.materialization_past_days,
                future_months=settings.materialization_future_months,
            )

    @app.delete("/api/v1/admin/events/{event_id}")
    def admin_delete_event(
        event_id: UUID,
        payload: RemovalInput | None = None,
        actor: str = Depends(require_admin),
        query_scope: Annotated[str | None, Query(alias="scope")] = None,
        query_occurrence: Annotated[UUID | None, Query(alias="occurrence")] = None,
        query_occurrence_id: Annotated[
            UUID | None, Query(alias="occurrence_id")
        ] = None,
    ) -> dict:
        """Admin-only alias of event deletion (see `delete_event`)."""
        resolved, note = removal_actor(payload, actor)
        scope, occurrence = resolve_scope(
            payload.scope if payload else None,
            (payload.occurrence_id or payload.occurrence) if payload else None,
            query_scope,
            query_occurrence or query_occurrence_id,
        )
        with database.transaction() as connection:
            return service.delete_occurrences(
                connection,
                event_id,
                scope=scope,
                occurrence_id=occurrence,
                actor=resolved,
                note=note,
                is_admin=True,
                past_days=settings.materialization_past_days,
                future_months=settings.materialization_future_months,
            )

    return app


app = create_app()
