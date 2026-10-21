from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database
from calendar_api.security import hash_password

LINK_TOKEN = "test-link-token-with-at-least-256-bits-of-entropy-aaaa"
OPERATOR_PASSWORD = "operator-pass"


@pytest.fixture(scope="module")
def operator_hash() -> str:
    return hash_password(OPERATOR_PASSWORD, iterations=1000)


@pytest.fixture()
def database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'accounts.db'}"


def _client(database_url: str, **overrides) -> TestClient:
    settings = Settings(**{"database_url": database_url, "admin_api_key": None, **overrides})
    database = create_database(database_url)
    return TestClient(create_app(settings, database))


def _register(client: TestClient, username: str, password: str = "long enough", **params):
    return client.post(
        "/api/v1/auth/register",
        params=params,
        json={"username": username, "password": password},
    )


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _promote(client: TestClient, operator_hash: str, username: str) -> None:
    operator = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": OPERATOR_PASSWORD},
    ).json()["token"]
    users = client.get("/api/v1/admin/users", headers=_bearer(operator)).json()["items"]
    user_id = next(user["id"] for user in users if user["username"] == username)
    response = client.patch(
        f"/api/v1/admin/users/{user_id}",
        headers=_bearer(operator),
        json={"is_admin": True},
    )
    assert response.status_code == 200, response.text


# --- Registration -----------------------------------------------------------


def test_register_signs_in_a_non_admin_account(database_url: str) -> None:
    with _client(database_url) as client:
        response = _register(client, "maya")
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["username"] == "maya"
        assert body["is_admin"] is False
        assert body["token_type"] == "bearer"
        assert "password" not in str(body)
        me = client.get("/api/v1/auth/me", headers=_bearer(body["token"]))
        assert me.status_code == 200
        assert me.json()["username"] == "maya"
        assert me.json()["is_admin"] is False


def test_register_requires_calendar_link_when_configured(database_url: str) -> None:
    with _client(database_url, calendar_access_token=LINK_TOKEN) as client:
        assert _register(client, "maya").status_code == 401
        assert _register(client, "maya", token=LINK_TOKEN).status_code == 201


def test_usernames_are_unique_ignoring_case(database_url: str) -> None:
    with _client(database_url) as client:
        assert _register(client, "Maya").status_code == 201
        duplicate = _register(client, "maya")
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "conflict"


def test_register_cannot_claim_the_configured_admin_username(
    database_url: str, operator_hash: str
) -> None:
    with _client(database_url, admin_password_hash=operator_hash) as client:
        assert _register(client, "Admin").status_code == 409


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("ab", "long enough"),  # too short
        ("has space", "long enough"),
        ("-dash", "long enough"),  # must start with a letter or digit
        ("x" * 33, "long enough"),
        ("maya", "short"),  # passwords need 8+ characters
    ],
)
def test_register_validates_username_and_password(
    database_url: str, username: str, password: str
) -> None:
    with _client(database_url) as client:
        assert _register(client, username, password).status_code == 422


def test_register_is_rate_limited(database_url: str) -> None:
    with _client(
        database_url,
        register_rate_limit_max=2,
        register_rate_limit_window_seconds=3600,
    ) as client:
        assert _register(client, "first").status_code == 201
        assert _register(client, "second").status_code == 201
        limited = _register(client, "third")
        assert limited.status_code == 429
        assert "Retry-After" in limited.headers


# --- Sign in / sign out -----------------------------------------------------


def test_login_and_logout_with_a_registered_account(database_url: str) -> None:
    with _client(database_url) as client:
        _register(client, "maya", "correct horse")
        wrong = client.post(
            "/api/v1/auth/login", json={"username": "maya", "password": "nope nope"}
        )
        assert wrong.status_code == 401
        unknown = client.post(
            "/api/v1/auth/login", json={"username": "nobody", "password": "nope nope"}
        )
        assert unknown.status_code == 401
        assert unknown.json() == wrong.json()

        login = client.post(
            "/api/v1/auth/login", json={"username": "MAYA", "password": "correct horse"}
        )
        assert login.status_code == 200, login.text
        token = login.json()["token"]
        assert login.json()["username"] == "maya"
        assert client.get("/api/v1/auth/me", headers=_bearer(token)).status_code == 200

        logout = client.post("/api/v1/auth/logout", headers=_bearer(token))
        assert logout.json() == {"revoked": True}
        assert client.get("/api/v1/auth/me", headers=_bearer(token)).status_code == 401


def test_me_requires_a_session(database_url: str) -> None:
    with _client(database_url) as client:
        assert client.get("/api/v1/auth/me").status_code == 401
        assert client.get("/api/v1/auth/me", headers=_bearer("bogus")).status_code == 401


def test_configured_admin_signs_in_through_the_shared_login(
    database_url: str, operator_hash: str
) -> None:
    with _client(database_url, admin_password_hash=operator_hash) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": OPERATOR_PASSWORD},
        )
        assert login.status_code == 200, login.text
        assert login.json()["is_admin"] is True
        token = login.json()["token"]
        me = client.get("/api/v1/auth/me", headers=_bearer(token)).json()
        assert me == {"id": None, "username": "admin", "is_admin": True}
        assert client.get("/api/v1/admin/groups", headers=_bearer(token)).status_code == 200


# --- Admin capability -------------------------------------------------------


def test_regular_accounts_get_no_admin_capabilities(
    database_url: str, operator_hash: str
) -> None:
    with _client(database_url, admin_password_hash=operator_hash) as client:
        token = _register(client, "maya").json()["token"]
        headers = _bearer(token)
        # Signed in but not an admin: forbidden, not "please sign in".
        assert client.get("/api/v1/admin/groups", headers=headers).status_code == 403
        assert client.get("/api/v1/admin/events", headers=headers).status_code == 403
        assert client.get("/api/v1/admin/users", headers=headers).status_code == 403
        created = client.post(
            "/api/v1/admin/groups",
            headers=headers,
            json={"slug": "arts", "name": "Arts", "description": ""},
        )
        assert created.status_code == 403
        assert created.json()["error"]["code"] == "forbidden"


def test_admin_login_route_refuses_non_admin_accounts(
    database_url: str, operator_hash: str
) -> None:
    with _client(database_url, admin_password_hash=operator_hash) as client:
        _register(client, "maya", "correct horse")
        response = client.post(
            "/api/v1/admin/login",
            json={"username": "maya", "password": "correct horse"},
        )
        assert response.status_code == 403
        assert "token" not in response.json()


def test_promoted_account_gains_admin_capabilities(
    database_url: str, operator_hash: str
) -> None:
    with _client(database_url, admin_password_hash=operator_hash) as client:
        token = _register(client, "maya").json()["token"]
        _promote(client, operator_hash, "maya")
        headers = _bearer(token)
        # The role is read on every request, so the existing session upgrades.
        assert client.get("/api/v1/auth/me", headers=headers).json()["is_admin"] is True
        assert client.get("/api/v1/admin/groups", headers=headers).status_code == 200
        admin_login = client.post(
            "/api/v1/admin/login",
            json={"username": "maya", "password": "long enough"},
        )
        assert admin_login.status_code == 200
        assert admin_login.json()["is_admin"] is True


def test_revoking_admin_takes_effect_immediately(
    database_url: str, operator_hash: str
) -> None:
    with _client(database_url, admin_password_hash=operator_hash) as client:
        token = _register(client, "maya").json()["token"]
        _promote(client, operator_hash, "maya")
        user_id = client.get("/api/v1/auth/me", headers=_bearer(token)).json()["id"]
        operator = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": OPERATOR_PASSWORD},
        ).json()["token"]
        response = client.patch(
            f"/api/v1/admin/users/{user_id}",
            headers=_bearer(operator),
            json={"is_admin": False},
        )
        assert response.status_code == 200
        assert response.json()["is_admin"] is False
        assert client.get("/api/v1/admin/groups", headers=_bearer(token)).status_code == 403


def test_admins_cannot_remove_their_own_admin_access(
    database_url: str, operator_hash: str
) -> None:
    with _client(database_url, admin_password_hash=operator_hash) as client:
        token = _register(client, "maya").json()["token"]
        _promote(client, operator_hash, "maya")
        user_id = client.get("/api/v1/auth/me", headers=_bearer(token)).json()["id"]
        response = client.patch(
            f"/api/v1/admin/users/{user_id}",
            headers=_bearer(token),
            json={"is_admin": False},
        )
        assert response.status_code == 409


def test_admin_user_list_never_exposes_password_hashes(
    database_url: str, operator_hash: str
) -> None:
    with _client(database_url, admin_password_hash=operator_hash) as client:
        _register(client, "maya")
        _register(client, "lee")
        operator = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": OPERATOR_PASSWORD},
        ).json()["token"]
        response = client.get("/api/v1/admin/users", headers=_bearer(operator))
        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["username"] for item in items] == ["lee", "maya"]
        assert all(set(item) == {"id", "username", "is_admin", "created_at"} for item in items)


def test_unknown_user_cannot_be_promoted(database_url: str, operator_hash: str) -> None:
    with _client(database_url, admin_password_hash=operator_hash) as client:
        operator = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": OPERATOR_PASSWORD},
        ).json()["token"]
        response = client.patch(
            "/api/v1/admin/users/00000000-0000-4000-8000-000000000000",
            headers=_bearer(operator),
            json={"is_admin": True},
        )
        assert response.status_code == 404


def test_database_admin_keeps_admin_routes_available_without_env_credentials(
    database_url: str, operator_hash: str
) -> None:
    with _client(database_url, admin_password_hash=operator_hash) as client:
        _register(client, "maya")
        _promote(client, operator_hash, "maya")
    # Restart with no env-configured admin credential at all.
    with _client(database_url) as client:
        assert client.get("/api/v1/admin/groups").status_code == 401
        login = client.post(
            "/api/v1/admin/login",
            json={"username": "maya", "password": "long enough"},
        )
        assert login.status_code == 200, login.text


# --- Link mode still works --------------------------------------------------


def test_signed_in_non_admin_can_still_manage_events_by_management_token(
    database_url: str,
) -> None:
    starts_at = (datetime.now(UTC) + timedelta(days=5)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    with _client(database_url, admin_api_key="key") as client:
        group = client.post(
            "/api/v1/admin/groups",
            headers={"X-Admin-Key": "key"},
            json={"slug": "arts", "name": "Arts", "description": ""},
        ).json()
        created = client.post(
            "/api/v1/events",
            json={
                "title": "Picnic",
                "is_all_day": False,
                "starts_at": starts_at.isoformat(),
                "ends_at": (starts_at + timedelta(hours=2)).isoformat(),
                "timezone": "UTC",
                "group_ids": [group["id"]],
                "submitter": {"name": "Maya", "channel": "email", "contact": "m@example.org"},
            },
        )
        assert created.status_code == 202, created.text
        event_id = created.json()["event_id"]
        management_token = created.json()["management_token"]
        session = _register(client, "maya").json()["token"]
        response = client.get(
            f"/api/v1/events/{event_id}/manage",
            headers={
                **_bearer(session),
                "X-Event-Management-Token": management_token,
            },
        )
        assert response.status_code == 200, response.text
