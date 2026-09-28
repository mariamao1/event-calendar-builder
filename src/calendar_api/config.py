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

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL",
                "sqlite:///.calendar-data/calendar.db",
            ),
            admin_api_key=os.environ.get("ADMIN_API_KEY"),
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
