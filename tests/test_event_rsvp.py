from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database

ADMIN = {"X-Admin-Key": "test-admin-key"}


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'event-rsvp.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def _payload(**overrides) -> dict:
    day = datetime.now(UTC).date() + timedelta(days=3)
    payload = {
        "title": "Potluck dinner",
        "description": "Bring a dish.",
        "is_all_day": False,
        "starts_at": datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC).isoformat(),
        "ends_at": datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC).isoformat(),
        "timezone": "UTC",
        "recurrence_dates": [],
        "group_ids": [],
        "submitter": {"name": "Robin", "channel": "email", "contact": "robin@example.org"},
    }
    payload.update(overrides)
    return payload


def _publish(client: TestClient, **overrides) -> str:
    response = client.post("/api/v1/admin/events", headers=ADMIN, json=_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()["event_id"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _account(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "long enough"},
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def test_rsvp_toggle_defaults_off_and_detail_reports_counts(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client)
        detail = client.get(f"/api/v1/events/{event_id}").json()
        assert detail["rsvp_enabled"] is False
        assert detail["rsvp_counts"] == {"attending": 0, "declined": 0, "total": 0}


def test_rsvp_requires_a_signed_in_account(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, rsvp_enabled=True)
        assert client.post(f"/api/v1/events/{event_id}/rsvp").status_code == 422
        no_auth = client.post(
            f"/api/v1/events/{event_id}/rsvp", json={"status": "attending"}
        )
        assert no_auth.status_code == 401
        bogus = client.post(
            f"/api/v1/events/{event_id}/rsvp",
            headers=_bearer("bogus"),
            json={"status": "attending"},
        )
        assert bogus.status_code == 401
        assert client.get(f"/api/v1/events/{event_id}/rsvps").status_code == 401


def test_rsvp_set_change_and_guest_list_visibility(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, rsvp_enabled=True)
        maya = _account(client, "maya")
        lee = _account(client, "lee")

        created = client.post(
            f"/api/v1/events/{event_id}/rsvp",
            headers=_bearer(maya),
            json={"status": "attending"},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["status"] == "attending"
        assert body["attending"] is True
        assert body["username"] == "maya"

        declined = client.post(
            f"/api/v1/events/{event_id}/rsvp",
            headers=_bearer(lee),
            json={"status": "declined"},
        )
        assert declined.status_code == 200, declined.text
        assert declined.json()["attending"] is False

        # Every RSVP'ed user sees the other RSVPs.
        guests = client.get(
            f"/api/v1/events/{event_id}/rsvps", headers=_bearer(maya)
        )
        assert guests.status_code == 200, guests.text
        listed = guests.json()
        assert listed["rsvp_enabled"] is True
        assert listed["counts"] == {"attending": 1, "declined": 1, "total": 2}
        assert [(item["username"], item["status"]) for item in listed["items"]] == [
            ("lee", "declined"),
            ("maya", "attending"),
        ]

        # Answering again changes the answer instead of adding a row.
        changed = client.post(
            f"/api/v1/events/{event_id}/rsvp",
            headers=_bearer(maya),
            json={"status": "declined"},
        )
        assert changed.status_code == 200, changed.text
        relisted = client.get(
            f"/api/v1/events/{event_id}/rsvps", headers=_bearer(lee)
        ).json()
        assert relisted["counts"] == {"attending": 0, "declined": 2, "total": 2}
        assert len(relisted["items"]) == 2

        detail = client.get(f"/api/v1/events/{event_id}").json()
        assert detail["rsvp_counts"] == {"attending": 0, "declined": 2, "total": 2}


def test_rsvp_accepts_plain_attending_flag_and_aliases(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, rsvp_enabled=True)
        maya = _account(client, "maya")
        flagged = client.post(
            f"/api/v1/events/{event_id}/rsvp",
            headers=_bearer(maya),
            json={"attending": False},
        )
        assert flagged.status_code == 200, flagged.text
        assert flagged.json()["status"] == "declined"
        aliased = client.put(
            f"/api/v1/events/{event_id}/rsvp",
            headers=_bearer(maya),
            json={"status": "YES"},
        )
        assert aliased.status_code == 200, aliased.text
        assert aliased.json()["status"] == "attending"
        mine = client.get(
            f"/api/v1/events/{event_id}/rsvp", headers=_bearer(maya)
        ).json()
        assert mine["rsvp"]["status"] == "attending"


def test_rsvp_rejects_bad_answers(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, rsvp_enabled=True)
        maya = _account(client, "maya")
        for payload in ({"status": "maybe"}, {"status": ""}, {}, {"status": "x", "attending": True}):
            response = client.post(
                f"/api/v1/events/{event_id}/rsvp",
                headers=_bearer(maya),
                json=payload,
            )
            assert response.status_code == 422, payload


def test_rsvp_disabled_events_conflict(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client)
        maya = _account(client, "maya")
        assert (
            client.post(
                f"/api/v1/events/{event_id}/rsvp",
                headers=_bearer(maya),
                json={"status": "attending"},
            ).status_code
            == 409
        )
        assert (
            client.get(
                f"/api/v1/events/{event_id}/rsvps", headers=_bearer(maya)
            ).status_code
            == 409
        )


def test_rsvp_missing_or_unpublished_events_are_not_found(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        maya = _account(client, "maya")
        missing = "00000000-0000-4000-8000-000000000000"
        assert (
            client.post(
                f"/api/v1/events/{missing}/rsvp",
                headers=_bearer(maya),
                json={"status": "attending"},
            ).status_code
            == 404
        )
        pending = client.post("/api/v1/events", json=_payload(rsvp_enabled=True)).json()
        pending_id = pending["event_id"]
        assert (
            client.post(
                f"/api/v1/events/{pending_id}/rsvp",
                headers=_bearer(maya),
                json={"status": "attending"},
            ).status_code
            == 404
        )


def test_rsvp_withdrawal(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, rsvp_enabled=True)
        maya = _account(client, "maya")
        assert (
            client.delete(
                f"/api/v1/events/{event_id}/rsvp", headers=_bearer(maya)
            ).status_code
            == 404
        )
        client.post(
            f"/api/v1/events/{event_id}/rsvp",
            headers=_bearer(maya),
            json={"status": "attending"},
        )
        withdrawn = client.delete(
            f"/api/v1/events/{event_id}/rsvp", headers=_bearer(maya)
        )
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["withdrawn"] is True
        mine = client.get(
            f"/api/v1/events/{event_id}/rsvp", headers=_bearer(maya)
        ).json()
        assert mine["rsvp"] is None
        assert client.get(f"/api/v1/events/{event_id}").json()["rsvp_counts"] == {
            "attending": 0,
            "declined": 0,
            "total": 0,
        }


def test_foreign_rsvp_table_from_another_implementation_is_moved_aside(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'legacy-rsvp.db'}"
    database = create_database(url)
    database.open()
    # Simulate a database that once ran an incompatible RSVP implementation
    # (per-occurrence answers under the same table name).
    with database.connection() as connection:
        connection.execute("DROP TABLE event_rsvps")
        connection.execute(
            """
            CREATE TABLE event_rsvps (
              occurrence_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              response TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (occurrence_id, user_id)
            )
            """
        )
        connection.execute(
            "INSERT INTO event_rsvps VALUES ('occ', 'usr', 'going', 't', 't')"
        )
    with TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            database,
        )
    ) as client:
        event_id = _publish(client, rsvp_enabled=True)
        detail = client.get(f"/api/v1/events/{event_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["rsvp_counts"] == {
            "attending": 0,
            "declined": 0,
            "total": 0,
        }
        maya = _account(client, "maya")
        rsvp = client.post(
            f"/api/v1/events/{event_id}/rsvp",
            headers=_bearer(maya),
            json={"status": "attending"},
        )
        assert rsvp.status_code == 200, rsvp.text
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM event_rsvps_legacy"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM event_rsvps").fetchone()[0]
            == 1
        )


def test_admin_edit_flips_the_toggle_and_creator_sees_guests(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/api/v1/events", json=_payload()).json()
        event_id = created["event_id"]
        token = created["management_token"]
        revision = client.post(
            f"/api/v1/admin/events/{event_id}/revisions",
            headers=ADMIN,
            json=_payload(rsvp_enabled=True),
        )
        assert revision.status_code == 201, revision.text
        assert client.get(f"/api/v1/events/{event_id}").json()["rsvp_enabled"] is True
        maya = _account(client, "maya")
        client.post(
            f"/api/v1/events/{event_id}/rsvp",
            headers=_bearer(maya),
            json={"status": "attending"},
        )
        # The creator sees the guest list with only the management token.
        guests = client.get(
            f"/api/v1/events/{event_id}/rsvps",
            headers={"X-Event-Management-Token": token},
        )
        assert guests.status_code == 200, guests.text
        assert [item["username"] for item in guests.json()["items"]] == ["maya"]
