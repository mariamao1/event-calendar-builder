from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta
from fastapi import Depends, FastAPI, File, Header, Query, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles

from . import ical
from .address_search import AddressSearch, AddressSearchUnavailable
from . import service as postgres_service
from . import sqlite_service
from .config import Settings
from .database import Database, SQLiteDatabase, create_database
from .errors import ApiError, ConflictError, NotFoundError, ValidationError
from .occurrence_scopes import CONTENT_OVERRIDE_FIELDS, apply_content_override
from .rate_limit import RateLimiter
from .schemas import (
    MAX_ATTACHMENT_BYTES,
    MAX_FEATURED_EVENTS,
    EventRevisionInput,
    GroupCreate,
    GroupUpdate,
    LoginInput,
    RegisterInput,
    RemovalInput,
    ReviewInput,
    RsvpInput,
    UserRoleUpdate,
    normalize_scope,
)
from .security import (
    SessionStore,
    hash_password,
    reject_password,
    tokens_equal,
    verify_password,
)

logger = logging.getLogger(__name__)

# A calendar.ics subscription without an explicit range covers recent and
# upcoming dates (clamped to the materialized window), and asks calendar
# apps to poll for changes this often.
FEED_PAST_DAYS = 30
FEED_FUTURE_MONTHS = 12
FEED_REFRESH = timedelta(hours=1)
FEED_PAGE_SIZE = 2000

# Session subjects: the configured operator admin, or a registered account
# by id. Roles are never stored in a session; they are read per request.
OPERATOR_SESSION = "operator"
USER_SESSION = "user:"


@dataclass(frozen=True, slots=True)
class Account:
    """A signed-in identity. `id` is None for the configured operator."""

    id: UUID | str | None
    username: str
    is_admin: bool

    def public(self) -> dict:
        return {
            "id": None if self.id is None else str(self.id),
            "username": self.username,
            "is_admin": self.is_admin,
        }


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
    zone = _zone(timezone)
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


def _combine_search(*values: str | None) -> str:
    """Merge the `q`/`search`/`query` aliases into one normalized query.

    `q` is canonical; the others are accepted so callers do not need to
    guess the name. Multiple values combine with a space and tokenize
    together downstream; blank input means no filtering.
    """
    return " ".join(part.strip() for part in values if part and part.strip())


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError("timezone must be a valid IANA timezone") from exc


def _feed_range(
    timezone: str, settings: Settings
) -> tuple[datetime, datetime, date, date]:
    """The default calendar.ics range, so a subscription URL never goes stale."""
    zone = _zone(timezone)
    today = datetime.now(zone).date()
    start_date = today - timedelta(
        days=min(FEED_PAST_DAYS, settings.materialization_past_days)
    )
    end_date = today + relativedelta(
        months=min(FEED_FUTURE_MONTHS, settings.materialization_future_months)
    )
    return (
        datetime.combine(start_date, time.min, zone).astimezone(UTC),
        datetime.combine(end_date, time.min, zone).astimezone(UTC),
        start_date,
        end_date,
    )


def _calendar_file(body: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-cache",
        },
    )


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
    app.state.address_search = AddressSearch(
        settings.address_search_url, near=settings.address_search_near
    )
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
        # The browser client must revalidate its page and assets (a cheap 304
        # via ETag) so it never keeps running a stale app.js after an update.
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-cache")
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

    def bearer_token(authorization: str | None) -> str | None:
        if authorization and authorization.lower().startswith("bearer "):
            return authorization[7:].strip() or None
        return None

    def operator_account() -> Account:
        return Account(id=None, username=settings.admin_username, is_admin=True)

    def session_account(authorization: str | None) -> Account | None:
        """The signed-in account behind a bearer session, if any.

        Registered accounts are re-read on every request, so a deleted
        account or a revoked admin role takes effect on existing sessions.
        """
        subject = sessions.validate(bearer_token(authorization))
        if subject is None:
            return None
        if subject == OPERATOR_SESSION:
            return operator_account()
        with database.connection() as connection:
            user = service.get_user(connection, subject.removeprefix(USER_SESSION))
        if user is None:
            return None
        return Account(id=user["id"], username=user["username"], is_admin=user["is_admin"])

    def admin_identity(
        x_admin_key: str | None, authorization: str | None
    ) -> str | None:
        """The acting admin's username; plain accounts are never admins."""
        account = session_account(authorization)
        if account is not None and account.is_admin:
            return account.username
        expected = settings.admin_api_key
        if expected and tokens_equal(x_admin_key, expected):
            return settings.admin_username
        return None

    def admin_login_configured() -> bool:
        """Whether any admin can sign in with a password."""
        if settings.admin_password_hash:
            return True
        with database.connection() as connection:
            return service.has_admin_user(connection)

    def admin_configured() -> bool:
        return bool(settings.admin_api_key) or admin_login_configured()

    def authenticate(username: str, password: str) -> Account | None:
        """Check a username/password against the operator, then accounts."""
        if (
            settings.admin_password_hash
            and tokens_equal(username, settings.admin_username)
            and verify_password(password, settings.admin_password_hash)
        ):
            return operator_account()
        with database.connection() as connection:
            user = service.get_user_credentials(connection, username)
        if user is None:
            reject_password(password)
            return None
        if not verify_password(password, user["password_hash"]):
            return None
        return Account(id=user["id"], username=user["username"], is_admin=user["is_admin"])

    def start_session(account: Account) -> dict:
        subject = OPERATOR_SESSION if account.id is None else f"{USER_SESSION}{account.id}"
        token, expires_at = sessions.create(
            subject, ttl_seconds=settings.admin_session_ttl_seconds
        )
        return {
            "token": token,
            "token_type": "bearer",
            **account.public(),
            "expires_at": datetime.fromtimestamp(expires_at, UTC).isoformat(),
        }

    def limit_logins(request: Request) -> None:
        enforce_rate_limit(
            request,
            scope="login",
            limit=settings.login_rate_limit_max,
            window_seconds=settings.login_rate_limit_window_seconds,
        )

    def require_admin(
        request: Request,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> str:
        """Require an admin: a signed-in admin account or the API key.

        Sessions come from POST /api/v1/auth/login (or /admin/login); only
        accounts with admin access pass, so being signed in is not enough.
        The X-Admin-Key service key remains accepted so existing operators
        keep working.
        """
        identity = admin_identity(x_admin_key, authorization)
        if identity is not None:
            return identity
        if session_account(authorization) is not None:
            raise ApiError(403, "forbidden", "this account does not have admin access")
        if not admin_configured():
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

    @app.post(
        "/api/v1/auth/register",
        status_code=status.HTTP_201_CREATED,
        dependencies=[link],
    )
    def register(payload: RegisterInput, request: Request) -> dict:
        """Create an account for anyone holding the calendar link.

        New accounts are signed in right away and never start as admins.
        Registrations are rate limited per client IP. The configured
        operator username is reserved.
        """
        enforce_rate_limit(
            request,
            scope="register",
            limit=settings.register_rate_limit_max,
            window_seconds=settings.register_rate_limit_window_seconds,
        )
        if payload.username.casefold() == settings.admin_username.casefold():
            raise ConflictError(f"username '{payload.username}' is already taken")
        password_hash = hash_password(payload.password)
        with database.transaction() as connection:
            user = service.create_user(connection, payload.username, password_hash)
        return start_session(
            Account(id=user["id"], username=user["username"], is_admin=False)
        )

    @app.post("/api/v1/auth/login")
    def login(payload: LoginInput, request: Request) -> dict:
        """Sign in with username + password and receive a session token.

        Works for every account; the response says whether it has admin
        access. Passwords are checked against salted PBKDF2 hashes in
        constant time, and attempts are rate limited per client IP.
        """
        limit_logins(request)
        account = authenticate(payload.username, payload.password)
        if account is None:
            raise ApiError(401, "unauthorized", "invalid username or password")
        return start_session(account)

    @app.post("/api/v1/auth/logout")
    def logout(authorization: Annotated[str | None, Header()] = None) -> dict:
        """Revoke the calling session token, if one was sent."""
        return {"revoked": sessions.revoke(bearer_token(authorization))}

    @app.get("/api/v1/auth/me")
    def me(authorization: Annotated[str | None, Header()] = None) -> dict:
        """The signed-in account and whether it has admin access."""
        account = session_account(authorization)
        if account is None:
            raise ApiError(401, "unauthorized", "sign in to continue")
        return account.public()

    @app.post("/api/v1/admin/login")
    def admin_login(payload: LoginInput, request: Request) -> dict:
        """Sign in, but only issue a session to accounts with admin access."""
        limit_logins(request)
        if not admin_login_configured():
            raise ApiError(
                503,
                "admin_not_configured",
                "no admin account or ADMIN_PASSWORD_HASH is configured",
            )
        account = authenticate(payload.username, payload.password)
        if account is None:
            raise ApiError(401, "unauthorized", "invalid username or password")
        if not account.is_admin:
            raise ApiError(403, "forbidden", "this account does not have admin access")
        return start_session(account)

    @app.post("/api/v1/admin/logout", dependencies=[admin])
    def admin_logout(authorization: Annotated[str | None, Header()] = None) -> dict:
        """Revoke the calling admin session token, if one was used."""
        return {"revoked": sessions.revoke(bearer_token(authorization))}

    @app.get("/api/v1/admin/users", dependencies=[admin])
    def admin_users() -> dict:
        with database.connection() as connection:
            return {"items": service.list_users(connection)}

    @app.patch("/api/v1/admin/users/{user_id}")
    def update_user_role(
        user_id: UUID,
        payload: UserRoleUpdate,
        actor: str = Depends(require_admin),
    ) -> dict:
        """Grant or revoke admin access for an account.

        Admins cannot remove their own admin access, so a calendar is never
        left without the admin who made the change.
        """
        with database.transaction() as connection:
            user = service.get_user(connection, user_id)
            if user is None:
                raise NotFoundError("user not found")
            if not payload.is_admin and user["username"].casefold() == actor.casefold():
                raise ConflictError("you cannot remove your own admin access")
            return service.set_user_admin(connection, user_id, payload.is_admin)

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

    @app.delete(
        "/api/v1/admin/groups/{group_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[admin],
    )
    def delete_group(group_id: UUID) -> Response:
        """Delete a group. Its events stay on the calendar without it."""
        with database.transaction() as connection:
            service.delete_group(connection, group_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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

    def is_event_editor(
        connection: object,
        event_id: UUID,
        management_token: str | None,
        x_admin_key: str | None,
        authorization: str | None,
    ) -> bool:
        """Whether the caller may manage one event (admin or its creator)."""
        if admin_identity(x_admin_key, authorization) is not None:
            return True
        return service.event_management_token_matches(
            connection, event_id, management_token
        )

    @app.post(
        "/api/v1/events/{event_id}/files",
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_event_files(
        event_id: UUID,
        request: Request,
        files: Annotated[list[UploadFile], File()],
        x_event_management_token: Annotated[
            str | None, Header(alias="X-Event-Management-Token")
        ] = None,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Attach files chosen on the creator's own machine to an event.

        Accepts images (png, jpg, gif, webp, svg) and PDFs up to 5 MB each.
        Uploaded files and link attachments share one cap of 3 per event.
        Files belong to the event and take effect immediately for its
        creator or an admin, like scoped single-date edits.
        """
        require_event_editor(
            event_id, x_event_management_token, x_admin_key, authorization
        )
        enforce_rate_limit(
            request,
            scope="submit",
            limit=settings.submit_rate_limit_max,
            window_seconds=settings.submit_rate_limit_window_seconds,
        )
        if not files:
            raise ValidationError("at least one file is required")
        stored = []
        with database.transaction() as connection:
            for upload in files:
                # Bound memory: read one byte past the cap to detect overflow
                # without loading an unbounded body.
                data = await upload.read(MAX_ATTACHMENT_BYTES + 1)
                stored.append(
                    service.upload_event_file(
                        connection,
                        event_id,
                        upload.filename,
                        upload.content_type,
                        data,
                    )
                )
        return {"event_id": event_id, "files": stored}

    @app.get("/api/v1/events/{event_id}/files", dependencies=[link])
    def event_files(
        event_id: UUID,
        x_event_management_token: Annotated[
            str | None, Header(alias="X-Event-Management-Token")
        ] = None,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """List an event's uploaded files (nothing when it carries none)."""
        with database.connection() as connection:
            try:
                detail = service.get_published_event(connection, event_id)
            except NotFoundError:
                detail = None
            if detail is not None:
                return {"event_id": event_id, "files": detail["files"]}
            if is_event_editor(
                connection,
                event_id,
                x_event_management_token,
                x_admin_key,
                authorization,
            ) and service.event_exists(connection, event_id):
                return {
                    "event_id": event_id,
                    "files": service.list_event_files(connection, event_id),
                }
            raise NotFoundError("event not found")

    @app.get("/api/v1/files/{file_id}", dependencies=[link])
    def download_event_file(
        file_id: UUID,
        x_event_management_token: Annotated[
            str | None, Header(alias="X-Event-Management-Token")
        ] = None,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        """Serve one uploaded file.

        Published events' files are reachable by anyone holding the
        calendar link; files of an event still awaiting review need its
        creator token or admin credentials instead.
        """
        with database.connection() as connection:
            stored = service.get_event_file(connection, file_id)
            if not stored["is_published"] and not is_event_editor(
                connection,
                stored["event_id"],
                x_event_management_token,
                x_admin_key,
                authorization,
            ):
                raise NotFoundError("file not found")
        ascii_name = "".join(
            character
            if (character.isascii() and character.isalnum()) or character in "._-"
            else "_"
            for character in stored["filename"]
        ).strip() or "file"
        image = stored["content_type"].startswith("image/")
        disposition = (
            f'inline; filename="{ascii_name}"'
            if image
            else f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(stored['filename'])}"
        )
        return Response(
            content=stored["data"],
            media_type=stored["content_type"],
            headers={
                "Content-Disposition": disposition,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=3600",
            },
        )

    @app.delete("/api/v1/events/{event_id}/files/{file_id}")
    def delete_event_file(
        event_id: UUID,
        file_id: UUID,
        x_event_management_token: Annotated[
            str | None, Header(alias="X-Event-Management-Token")
        ] = None,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        """Remove an uploaded file from an event (creator or admin)."""
        require_event_editor(
            event_id, x_event_management_token, x_admin_key, authorization
        )
        with database.transaction() as connection:
            service.delete_event_file(connection, event_id, file_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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

    @app.get("/api/v1/address-suggestions", dependencies=[link])
    def address_suggestions(request: Request, q: str = "") -> dict:
        """Street-address suggestions for the event form, from OpenStreetMap.

        Queries shorter than three characters return no items without an
        upstream call. Lookups are rate limited per client IP; when the
        geocoder is unreachable the form simply keeps free-text entry.
        """
        address_search: AddressSearch = app.state.address_search
        if not address_search.enabled:
            raise NotFoundError("address suggestions are turned off")
        if len(" ".join(q.split())) < 3:
            return {"items": []}
        enforce_rate_limit(
            request,
            scope="address",
            limit=settings.address_search_rate_limit_max,
            window_seconds=60,
        )
        try:
            items = address_search.suggest(q)
        except AddressSearchUnavailable as exc:
            logger.warning("address search unavailable: %s", exc)
            raise ApiError(
                503,
                "address_search_unavailable",
                "address suggestions are unavailable right now",
            ) from exc
        return {"items": items}

    @app.get("/api/v1/events/{event_id}", dependencies=[link])
    def event(event_id: UUID, occurrence: UUID | None = None) -> dict:
        """One published event; `occurrence` selects a date of its series."""
        with database.connection() as connection:
            return service.get_published_event(
                connection, event_id, occurrence_id=occurrence
            )

    def require_account(authorization: str | None) -> Account:
        """The signed-in account behind a Bearer [REDACTED]; 401 without one."""
        account = session_account(authorization)
        if account is None:
            raise ApiError(401, "unauthorized", "sign in to continue")
        return account

    def rsvp_user_key(account: Account) -> tuple[str, str]:
        """The `(user_id, username)` an RSVP row is stored under.

        Registered accounts use their id. The configured operator is not a
        users row, so it answers under the stable "operator" key instead.
        """
        if account.id is None:
            return ("operator", account.username)
        return (str(account.id), account.username)

    def set_event_rsvp(
        event_id: UUID, payload: RsvpInput, authorization: str | None
    ) -> dict:
        """Create or change the caller's RSVP answer for an event."""
        account = require_account(authorization)
        status = payload.resolved_status()
        user_id, username = rsvp_user_key(account)
        with database.transaction() as connection:
            return service.set_rsvp(connection, event_id, user_id, username, status)

    @app.post("/api/v1/events/{event_id}/rsvp")
    def post_event_rsvp(
        event_id: UUID,
        payload: RsvpInput,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """RSVP to an event as the signed-in user.

        The event opts in with its `rsvp_enabled` toggle; answering again
        changes the answer instead of adding a row. Accepts either
        `{"status": "attending"}` (`"declined"` to decline) or
        `{"attending": true}` (`false` to decline).
        """
        return set_event_rsvp(event_id, payload, authorization)

    @app.post("/api/v1/events/{event_id}/rsvps")
    def post_event_rsvps(
        event_id: UUID,
        payload: RsvpInput,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Alias of `POST /api/v1/events/{event_id}/rsvp`."""
        return set_event_rsvp(event_id, payload, authorization)

    @app.put("/api/v1/events/{event_id}/rsvp")
    def put_event_rsvp(
        event_id: UUID,
        payload: RsvpInput,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Alias of `POST /api/v1/events/{event_id}/rsvp`."""
        return set_event_rsvp(event_id, payload, authorization)

    @app.get("/api/v1/events/{event_id}/rsvps")
    def list_event_rsvps(
        event_id: UUID,
        x_event_management_token: Annotated[
            str | None, Header(alias="X-Event-Management-Token")
        ] = None,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Every RSVP for an event, with per-answer counts.

        When the event's RSVP toggle is on, any signed-in account sees the
        guest list, as do the event's creator (via `X-Event-Management-Token`)
        and admins. Anything else is `401`.
        """
        if (
            session_account(authorization) is None
            and admin_identity(x_admin_key, authorization) is None
        ):
            with database.connection() as connection:
                if not service.event_management_token_matches(
                    connection, event_id, x_event_management_token
                ):
                    raise ApiError(401, "unauthorized", "sign in to continue")
        with database.connection() as connection:
            return service.list_rsvps(connection, event_id)

    @app.get("/api/v1/events/{event_id}/rsvp")
    def get_my_event_rsvp(
        event_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """The caller's own RSVP answer (`null` when they have not answered)."""
        account = require_account(authorization)
        user_id, _ = rsvp_user_key(account)
        with database.connection() as connection:
            return {
                "event_id": event_id,
                "rsvp": service.get_rsvp(connection, event_id, user_id),
            }

    @app.delete("/api/v1/events/{event_id}/rsvp")
    def delete_my_event_rsvp(
        event_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Withdraw the caller's RSVP answer."""
        account = require_account(authorization)
        user_id, _ = rsvp_user_key(account)
        with database.transaction() as connection:
            return service.delete_rsvp(connection, event_id, user_id)

    @app.get("/api/v1/calendar", dependencies=[link])
    def calendar(
        start: str,
        end: str,
        timezone: str = "UTC",
        group: Annotated[list[str] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=2000)] = 500,
        offset: Annotated[int, Query(ge=0)] = 0,
        q: Annotated[str | None, Query()] = None,
        search: Annotated[str | None, Query()] = None,
        query: Annotated[str | None, Query()] = None,
    ) -> dict:
        start_at, end_at, start_date, end_date = _calendar_range(
            start, end, timezone, settings
        )
        group_slugs = list(dict.fromkeys(group or []))
        search_text = _combine_search(q, search, query)
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
                search=search_text,
            )
        return {
            "items": items,
            "meta": {
                "start": start,
                "end": end,
                "timezone": timezone,
                "groups": group_slugs,
                "search": search_text,
                "count": len(items),
                "has_more": has_more,
                "limit": limit,
                "offset": offset,
            },
        }

    @app.get("/api/v1/featured", dependencies=[link])
    def featured() -> dict:
        """Events the calendar owner pinned, newest pin first.

        Each item is the event at its next date that has not ended. Events
        whose dates are all past (or that were cancelled or unpublished)
        leave the list on their own and free their slot.
        """
        with database.connection() as connection:
            items = service.featured_events(connection)
        return {"items": items, "meta": {"limit": MAX_FEATURED_EVENTS}}

    def export_row(detail: dict, row: dict) -> dict:
        """One materialized date of a series with its own content merged in."""
        item = {field: detail.get(field) for field in CONTENT_OVERRIDE_FIELDS}
        item.update(row)
        apply_content_override(item, item.pop("content_override", None))
        item["is_cancelled"] = bool(detail["is_cancelled"]) or bool(
            item.pop("instance_cancelled", False)
        )
        item["groups"] = detail["groups"]
        return item

    def published_export(
        event_id: UUID, occurrence: UUID | None
    ) -> tuple[dict, dict | None, list[dict]]:
        """The published event, the requested date (if any), and its rows."""
        with database.connection() as connection:
            detail = service.get_published_event(
                connection, event_id, occurrence_id=occurrence
            )
            rows = (
                service.event_occurrence_rows(connection, event_id)
                if detail["series"] is not None or occurrence is not None
                else []
            )
        selected = None
        if occurrence is not None:
            if detail["occurrence"] is None:
                raise NotFoundError("occurrence not found")
            row = next(
                (item for item in rows if str(item["occurrence_id"]) == str(occurrence)),
                None,
            )
            if row is None:
                raise NotFoundError("occurrence not found")
            selected = export_row(detail, row)
        return detail, selected, rows

    @app.get("/api/v1/events/{event_id}/ics", dependencies=[link])
    def event_ics(event_id: UUID, occurrence: UUID | None = None) -> Response:
        """Download a published event for a calendar app (RFC 5545 .ics).

        A repeating event exports its whole series, keeping its wall-clock
        time in its own timezone, with per-date edits, cancellations, and
        skips; `occurrence` exports only that date. Re-downloading updates
        the same entry, since UIDs are stable.
        """
        detail, selected, rows = published_export(event_id, occurrence)
        if selected is not None:
            components = [
                ical.single_vevent(
                    selected,
                    leaf=selected["occurrence_id"],
                    sequence=selected["version"],
                )
            ]
        elif detail["series"] is None:
            components = [
                ical.single_vevent(
                    detail,
                    leaf=detail["event_id"],
                    sequence=detail.get("revision_number") or 0,
                )
            ]
        else:
            components = ical.series_components(
                detail, [export_row(detail, row) for row in rows]
            )
        return _calendar_file(
            ical.build_calendar(components),
            ical.slug_filename(detail["title"], event_id),
        )

    @app.get("/api/v1/events/{event_id}/google", dependencies=[link])
    def event_google(
        event_id: UUID, occurrence: UUID | None = None
    ) -> RedirectResponse:
        """Redirect to Google Calendar's "add event" page for this event.

        A repeating event carries its rule and timezone; Google's template
        cannot carry per-date exceptions, which the .ics export does.
        `occurrence` adds only that date.
        """
        detail, selected, _ = published_export(event_id, occurrence)
        item = selected or detail
        repeating = selected is None and detail["series"] is not None
        url = ical.google_calendar_url(
            title=item.get("title") or "",
            starts_at=item.get("starts_at"),
            ends_at=item.get("ends_at"),
            start_date=item.get("start_date"),
            end_date=item.get("end_date"),
            is_all_day=bool(item["is_all_day"]),
            description=ical.description_text(item),
            location=ical.location_text(item),
            timezone=detail["timezone"] if repeating else None,
            recurrence_rule=detail.get("recurrence_rule") if repeating else None,
        )
        return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/api/v1/calendar.ics", dependencies=[link])
    def calendar_ics(
        start: str | None = None,
        end: str | None = None,
        timezone: str = "UTC",
        group: Annotated[list[str] | None, Query()] = None,
        q: Annotated[str | None, Query()] = None,
        search: Annotated[str | None, Query()] = None,
        query: Annotated[str | None, Query()] = None,
    ) -> Response:
        """The published calendar as a feed for calendar apps (RFC 5545).

        Subscribe to this URL (for example as `webcal://`) to keep a calendar
        app in sync, or download it as a snapshot. It holds the same dates as
        `GET /api/v1/calendar`, one event per date. Without `start`/`end` it
        covers the last 30 days through the next 12 months, so a subscription
        stays current; `group` narrows it like the calendar filter and `q`
        (also accepted as `search`/`query`) narrows it by event text.
        """
        if (start is None) != (end is None):
            raise ValidationError("pass both start and end, or neither")
        if start is None or end is None:
            start_at, end_at, start_date, end_date = _feed_range(timezone, settings)
        else:
            start_at, end_at, start_date, end_date = _calendar_range(
                start, end, timezone, settings
            )
        group_slugs = list(dict.fromkeys(group or []))
        search_text = _combine_search(q, search, query)
        items: list[dict] = []
        with database.connection() as connection:
            has_more = True
            while has_more:
                page, has_more = service.calendar_occurrences(
                    connection,
                    start_at=start_at,
                    end_at=end_at,
                    start_date=start_date,
                    end_date=end_date,
                    group_slugs=group_slugs,
                    limit=FEED_PAGE_SIZE,
                    offset=len(items),
                    search=search_text,
                )
                items.extend(page)
            names = {
                item["slug"]: item["name"]
                for item in service.list_groups(connection, include_inactive=False)
            }
        now = datetime.now(UTC)
        components = [
            ical.single_vevent(
                item, leaf=item["occurrence_id"], sequence=item["version"], now=now
            )
            for item in items
        ]
        name = ical.CALENDAR_NAME
        if group_slugs:
            name += " · " + ", ".join(names.get(slug, slug) for slug in group_slugs)
        if search_text:
            name += f" · “{search_text}”"
        return _calendar_file(
            ical.build_calendar(components, name=name, refresh=FEED_REFRESH),
            "community-calendar.ics",
        )

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

    @app.put("/api/v1/admin/events/{event_id}/featured", dependencies=[admin])
    def feature_event(event_id: UUID) -> dict:
        """Pin a published event with an upcoming date to the featured strip.

        At most `MAX_FEATURED_EVENTS` events are featured at once (`409`
        beyond that); pinning a featured event again keeps its pin.
        """
        with database.transaction() as connection:
            return service.feature_event(connection, event_id)

    @app.delete("/api/v1/admin/events/{event_id}/featured", dependencies=[admin])
    def unfeature_event(event_id: UUID) -> dict:
        """Take an event off the featured strip."""
        with database.transaction() as connection:
            return service.unfeature_event(connection, event_id)

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

    @app.post("/api/v1/events/{event_id}/occurrences/{occurrence_id}/restore")
    def restore_occurrence(
        event_id: UUID,
        occurrence_id: UUID,
        payload: RemovalInput | None = None,
        x_event_management_token: Annotated[
            str | None, Header(alias="X-Event-Management-Token")
        ] = None,
        x_admin_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Undo a single-date cancellation or skip of a recurring event.

        Only dates cancelled or deleted on their own (`scope=single`) can be
        restored; anything else reports `409`.
        """
        require_event_editor(
            event_id, x_event_management_token, x_admin_key, authorization
        )
        identity = admin_identity(x_admin_key, authorization)
        actor, note = removal_actor(payload, identity)
        if payload:
            require_path_occurrence(
                payload.occurrence_id or payload.occurrence, occurrence_id
            )
        with database.transaction() as connection:
            return service.restore_occurrence(
                connection,
                event_id,
                occurrence_id,
                actor=actor,
                note=note,
                is_admin=identity is not None,
                management_token=x_event_management_token,
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
