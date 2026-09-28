from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta
from fastapi import Depends, FastAPI, Header, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import service as postgres_service
from . import sqlite_service
from .config import Settings
from .database import Database, SQLiteDatabase, create_database
from .errors import ApiError, ValidationError
from .schemas import EventRevisionInput, GroupCreate, GroupUpdate, ReviewInput


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
    app.state.database = database
    app.state.service = service
    app.state.settings = settings
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
            allow_headers=["Content-Type", "X-Admin-Key"],
        )

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    def require_admin(
        x_admin_key: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = settings.admin_api_key
        if not expected:
            raise ApiError(
                503, "admin_not_configured", "ADMIN_API_KEY is not configured"
            )
        if x_admin_key is None or not hmac.compare_digest(x_admin_key, expected):
            raise ApiError(401, "unauthorized", "a valid X-Admin-Key is required")

    admin = Depends(require_admin)

    @app.get("/health")
    def health() -> dict:
        with database.connection() as connection:
            connection.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/api/v1/groups")
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

    @app.post("/api/v1/events", status_code=status.HTTP_202_ACCEPTED)
    def post_event(payload: EventRevisionInput) -> dict:
        with database.transaction() as connection:
            return service.create_event(connection, payload)

    @app.post(
        "/api/v1/events/{event_id}/revisions",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[admin],
    )
    def post_revision(event_id: UUID, payload: EventRevisionInput) -> dict:
        with database.transaction() as connection:
            return service.create_revision(connection, event_id, payload)

    @app.get("/api/v1/events/{event_id}")
    def event(event_id: UUID) -> dict:
        with database.connection() as connection:
            return service.get_published_event(connection, event_id)

    @app.get("/api/v1/calendar")
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

    return app


app = create_app()
