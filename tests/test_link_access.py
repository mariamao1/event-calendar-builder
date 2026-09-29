from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import apply_migrations, create_database
from calendar_api.security import (
    SessionStore,
    hash_password,
    new_token,
    tokens_equal,
    verify_password,
)

LINK_TOKEN = "test-link-token-with-at-least-256-bits-of-entropy-aaaa"


def _settings(**overrides) -> Settings:
    defaults: dict = {
        "database_url": "sqlite:///:memory:",
        "admin_api_key": "test-admin-key",
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture()
def database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'access.db'}"


def _client(database_url: str, **overrides) -> TestClient:
    root = Path(__file__).resolve().parents[1]
    if not database_url.startswith("sqlite:///"):
        apply_migrations(database_url, root / "db" / "migrations")
    settings = _settings(database_url=database_url, **overrides)
    database = create_database(database_url, min_size=1, max_size=1)
    app = create_app(settings, database)
    return TestClient(app)


def _event_payload(group_id: str) -> dict:
    zone = ZoneInfo("America/New_York")
    starts_at = (datetime.now(zone) + timedelta(days=7)).replace(
        hour=18, minute=0, second=0, microsecond=0
    )
    return {
        "title": "Link-gated talk",
        "is_all_day": False,
        "starts_at": starts_at.isoformat(),
        "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
        "timezone": "America/New_York",
        "group_ids": [group_id],
        "submitter": {
            "name": "Sam",
            "channel": "email",
            "contact": "sam@example.org",
        },
    }


def _calendar_url(token: str | None = None) -> str:
    today = datetime.now(UTC).date()
    url = (
        f"/api/v1/calendar?start={today.isoformat()}"
        f"&end={(today + timedelta(days=20)).isoformat()}"
        "&timezone=UTC"
    )
    return f"{url}&token={token}" if token else url


# --- Password hashing ------------------------------------------------------


def test_password_hash_verifies_and_rejects() -> None:
    encoded = hash_password("correct horse", iterations=1000)
    assert verify_password("correct horse", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("correct horse", "not-a-hash")
    assert not verify_password("correct horse", "")


def test_tokens_equal_fails_closed() -> None:
    assert tokens_equal("abc", "abc")
    assert not tokens_equal("abc", "abd")
    assert not tokens_equal(None, "abd")
    assert not tokens_equal("abc", None)
    assert not tokens_equal("", "")


# --- Link-gated public routes ----------------------------------------------


def test_public_routes_open_without_configured_token(
    database_url: str,
) -> None:
    with _client(database_url) as client:
        assert client.get("/api/v1/groups").status_code == 200
        assert client.get(_calendar_url()).status_code == 200


def test_public_routes_require_link_token(database_url: str) -> None:
    with _client(database_url, calendar_access_token=LINK_TOKEN) as client:
        assert client.get("/api/v1/groups").status_code == 401
        assert client.get("/api/v1/groups?token=wrong").status_code == 401
        assert client.get(_calendar_url()).status_code == 401
        assert (
            client.get("/api/v1/groups?token=" + LINK_TOKEN).status_code == 200
        )
        assert (
            client.get(
                "/api/v1/groups?access_token=" + LINK_TOKEN
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/api/v1/groups", headers={"X-Calendar-Token": LINK_TOKEN}
            ).status_code
            == 200
        )


def test_event_submission_requires_link_token(database_url: str) -> None:
    with _client(database_url, calendar_access_token=LINK_TOKEN) as client:
        group = client.post(
            "/api/v1/admin/groups",
            headers={"X-Admin-Key": "test-admin-key"},
            json={"slug": "arts", "name": "Arts", "description": ""},
        ).json()
        payload = _event_payload(group["id"])
        assert client.post("/api/v1/events", json=payload).status_code == 401
        response = client.post(
            "/api/v1/events?token=" + LINK_TOKEN, json=payload
        )
        assert response.status_code == 202, response.text


def test_health_and_robots_stay_public(database_url: str) -> None:
    with _client(database_url, calendar_access_token=LINK_TOKEN) as client:
        assert client.get("/health").status_code == 200
        robots = client.get("/robots.txt")
        assert robots.status_code == 200
        assert "Disallow: /" in robots.text


def test_robots_header_marks_everything_noindex(database_url: str) -> None:
    with _client(database_url, calendar_access_token=LINK_TOKEN) as client:
        for path in ("/health", "/robots.txt", "/api/v1/groups"):
            response = client.get(path)
            assert (
                response.headers.get("X-Robots-Tag") == "noindex, nofollow"
            ), path


# --- Admin authentication ---------------------------------------------------


def test_admin_login_flow(database_url: str) -> None:
    password_hash = hash_password("s3cret", iterations=1000)
    with _client(
        database_url,
        admin_api_key=None,
        admin_username="root",
        admin_password_hash=password_hash,
    ) as client:
        assert client.get("/api/v1/admin/groups").status_code == 401
        bad = client.post(
            "/api/v1/admin/login",
            json={"username": "root", "password": "wrong"},
        )
        assert bad.status_code == 401
        good = client.post(
            "/api/v1/admin/login",
            json={"username": "root", "password": "s3cret"},
        )
        assert good.status_code == 200, good.text
        token = good.json()["token"]
        groups = client.get(
            "/api/v1/admin/groups",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert groups.status_code == 200, groups.text
        logout = client.post(
            "/api/v1/admin/logout",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert logout.json() == {"revoked": True}
        assert (
            client.get(
                "/api/v1/admin/groups",
                headers={"Authorization": f"Bearer {token}"},
            ).status_code
            == 401
        )


def test_admin_api_key_still_accepted(database_url: str) -> None:
    password_hash = hash_password("s3cret", iterations=1000)
    with _client(database_url, admin_password_hash=password_hash) as client:
        response = client.get(
            "/api/v1/admin/groups", headers={"X-Admin-Key": "test-admin-key"}
        )
        assert response.status_code == 200


def test_admin_login_unconfigured_returns_503(database_url: str) -> None:
    with _client(database_url, admin_password_hash=None) as client:
        response = client.post(
            "/api/v1/admin/login",
            json={"username": "admin", "password": "anything"},
        )
        assert response.status_code == 503


def test_admin_routes_fail_closed_without_credentials(
    database_url: str,
) -> None:
    with _client(database_url, admin_api_key=None) as client:
        assert client.get("/api/v1/admin/groups").status_code == 503


# --- Rate limiting ----------------------------------------------------------


def test_submission_rate_limit_returns_retry_after(
    database_url: str,
) -> None:
    with _client(
        database_url,
        submit_rate_limit_max=2,
        submit_rate_limit_window_seconds=3600,
    ) as client:
        group = client.post(
            "/api/v1/admin/groups",
            headers={"X-Admin-Key": "test-admin-key"},
            json={"slug": "arts", "name": "Arts", "description": ""},
        ).json()
        payload = _event_payload(group["id"])
        assert client.post("/api/v1/events", json=payload).status_code == 202
        assert client.post("/api/v1/events", json=payload).status_code == 202
        limited = client.post("/api/v1/events", json=payload)
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "rate_limited"
        assert "Retry-After" in limited.headers


def test_login_rate_limit(database_url: str) -> None:
    password_hash = hash_password("s3cret", iterations=1000)
    with _client(
        database_url,
        admin_api_key=None,
        admin_password_hash=password_hash,
        login_rate_limit_max=2,
        login_rate_limit_window_seconds=3600,
    ) as client:
        body = {"username": "root", "password": "wrong"}
        assert client.post("/api/v1/admin/login", json=body).status_code == 401
        assert client.post("/api/v1/admin/login", json=body).status_code == 401
        assert client.post("/api/v1/admin/login", json=body).status_code == 429


def test_session_store_expiry_and_revocation() -> None:
    store = SessionStore()
    token, _ = store.create("admin", ttl_seconds=3600)
    assert store.validate(token) == "admin"
    assert store.validate("bogus") is None
    assert store.revoke(token) is True
    assert store.validate(token) is None
    expired, _ = store.create("admin", ttl_seconds=-1)
    assert store.validate(expired) is None


def test_new_tokens_are_unique_and_unguessable() -> None:
    assert new_token() != new_token()
    assert len(new_token()) >= 32
