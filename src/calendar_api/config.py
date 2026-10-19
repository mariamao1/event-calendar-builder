from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    admin_api_key: str | None
    host: str = "127.0.0.1"
    port: int = 8000
    materialization_past_days: int = 90
    materialization_future_months: int = 18
    cors_origins: tuple[str, ...] = ()
    # Unguessable link token gating the public (no-account) routes. When set,
    # every public read/submission must present it; when unset, public routes
    # stay open for local development.
    calendar_access_token: str | None = None
    # Real admin authentication: username plus a PBKDF2 hash produced by
    # `calendar-api hash-password`. Sessions issued by /admin/login expire
    # after admin_session_ttl_seconds.
    admin_username: str = "admin"
    admin_password_hash: str | None = None
    admin_session_ttl_seconds: int = 12 * 3600
    # Server-side abuse guards. Anyone holding the link can submit, so public
    # submissions are rate limited per client IP; admin logins are limited to
    # slow down credential guessing.
    submit_rate_limit_max: int = 30
    submit_rate_limit_window_seconds: int = 3600
    login_rate_limit_max: int = 10
    login_rate_limit_window_seconds: int = 900
    # Street-address suggestions in the event form come from a Photon
    # (OpenStreetMap) geocoder; an empty URL turns them off. `near` biases
    # results toward a lat/lon, and lookups are limited per client IP per
    # minute to respect the upstream's fair-use policy.
    address_search_url: str = "https://photon.komoot.io/api/"
    address_search_near: tuple[float, float] | None = None
    address_search_rate_limit_max: int = 60

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL",
                "sqlite:///.calendar-data/calendar.db",
            ),
            admin_api_key=os.environ.get("ADMIN_API_KEY"),
            calendar_access_token=os.environ.get("CALENDAR_ACCESS_TOKEN"),
            admin_username=os.environ.get("ADMIN_USERNAME", "admin"),
            admin_password_hash=os.environ.get("ADMIN_PASSWORD_HASH"),
            admin_session_ttl_seconds=int(
                os.environ.get("ADMIN_SESSION_TTL_SECONDS", str(12 * 3600))
            ),
            submit_rate_limit_max=int(
                os.environ.get("SUBMIT_RATE_LIMIT_MAX", "30")
            ),
            submit_rate_limit_window_seconds=int(
                os.environ.get("SUBMIT_RATE_LIMIT_WINDOW_SECONDS", "3600")
            ),
            login_rate_limit_max=int(os.environ.get("LOGIN_RATE_LIMIT_MAX", "10")),
            login_rate_limit_window_seconds=int(
                os.environ.get("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "900")
            ),
            address_search_url=os.environ.get(
                "ADDRESS_SEARCH_URL", "https://photon.komoot.io/api/"
            ).strip(),
            address_search_near=_lat_lon(os.environ.get("ADDRESS_SEARCH_NEAR")),
            address_search_rate_limit_max=int(
                os.environ.get("ADDRESS_SEARCH_RATE_LIMIT_MAX", "60")
            ),
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000")),
            materialization_past_days=int(
                os.environ.get("MATERIALIZATION_PAST_DAYS", "90")
            ),
            materialization_future_months=int(
                os.environ.get("MATERIALIZATION_FUTURE_MONTHS", "18")
            ),
            cors_origins=tuple(
                origin.strip()
                for origin in os.environ.get("CORS_ORIGINS", "").split(",")
                if origin.strip()
            ),
        )


def _lat_lon(value: str | None) -> tuple[float, float] | None:
    if not value or not value.strip():
        return None
    try:
        lat, lon = (float(part) for part in value.split(","))
    except ValueError:
        raise ValueError(
            f"ADDRESS_SEARCH_NEAR must be 'latitude,longitude', got {value!r}"
        ) from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"ADDRESS_SEARCH_NEAR is out of range: {value!r}")
    return lat, lon
