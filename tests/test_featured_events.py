from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from calendar_api import sqlite_service
from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database

ADMIN = {"X-Admin-Key": "test-admin-key"}
LINK_TOKEN = "test-link-token-with-at-least-256-bits-of-entropy-aaaa"


def _client(tmp_path: Path, **settings) -> TestClient:
    url = f"sqlite:///{tmp_path / 'featured.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key", **settings),
            create_database(url),
        )
    )


def _day(offset: int):
    return datetime.now(UTC).date() + timedelta(days=offset)


def _payload(days_ahead: int = 3, **overrides) -> dict:
    day = _day(days_ahead)
    payload = {
        "title": "Harvest festival",
        "description": "Music and food.",
        "location_name": "Town square",
        "is_all_day": False,
        "starts_at": datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC).isoformat(),
        "ends_at": datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC).isoformat(),
        "timezone": "UTC",
        "recurrence_rule": None,
        "recurrence_dates": [],
        "group_ids": [],
        "submitter": {"name": "Robin", "channel": "email", "contact": "robin@example.org"},
    }
    payload.update(overrides)
    return payload


def _publish(client: TestClient, days_ahead: int = 3, **overrides) -> str:
    response = client.post(
        "/api/v1/admin/events", headers=ADMIN, json=_payload(days_ahead, **overrides)
    )
    assert response.status_code == 201, response.text
    return response.json()["event_id"]


def _feature(client: TestClient, event_id: str, **kwargs):
    return client.put(
        f"/api/v1/admin/events/{event_id}/featured", headers=ADMIN, **kwargs
    )


def _featured_ids(client: TestClient) -> list[str]:
    response = client.get("/api/v1/featured")
    assert response.status_code == 200, response.text
    return [item["event_id"] for item in response.json()["items"]]


def _travel_to(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(sqlite_service, "_now", lambda: moment)


def test_nothing_is_featured_by_default(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client)
        listed = client.get("/api/v1/featured")
        assert listed.status_code == 200, listed.text
        assert listed.json()["items"] == []
        assert listed.json()["meta"]["limit"] == 3
        assert client.get(f"/api/v1/events/{event_id}").json()["is_featured"] is False


def test_admin_features_and_unfeatures_an_event(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client)
        featured = _feature(client, event_id)
        assert featured.status_code == 200, featured.text
        assert featured.json()["event_id"] == event_id
        assert featured.json()["is_featured"] is True
        assert featured.json()["featured_at"]

        items = client.get("/api/v1/featured").json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["event_id"] == event_id
        assert item["title"] == "Harvest festival"
        assert item["location_name"] == "Town square"
        assert item["occurrence_id"]
        assert item["starts_at"].startswith(_day(3).isoformat())
        assert item["groups"] == []

        assert client.get(f"/api/v1/events/{event_id}").json()["is_featured"] is True
        calendar = client.get(
            f"/api/v1/calendar?start={_day(3).isoformat()}"
            f"&end={_day(4).isoformat()}&timezone=UTC"
        ).json()["items"]
        assert [entry["is_featured"] for entry in calendar] == [True]

        removed = client.delete(
            f"/api/v1/admin/events/{event_id}/featured", headers=ADMIN
        )
        assert removed.status_code == 200, removed.text
        assert removed.json() == {"event_id": event_id, "is_featured": False}
        assert _featured_ids(client) == []
        assert client.get(f"/api/v1/events/{event_id}").json()["is_featured"] is False


def test_featuring_requires_admin(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client)
        path = f"/api/v1/admin/events/{event_id}/featured"
        assert client.put(path).status_code == 401
        assert client.delete(path).status_code == 401
        token = client.post(
            "/api/v1/auth/register",
            json={"username": "maya", "password": "long enough"},
        ).json()["token"]
        member = {"Authorization": f"Bearer {token}"}
        assert client.put(path, headers=member).status_code == 403
        assert client.delete(path, headers=member).status_code == 403
        assert _featured_ids(client) == []


def test_featuring_twice_keeps_the_original_pin(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client)
        first = _feature(client, event_id).json()
        second = _feature(client, event_id)
        assert second.status_code == 200, second.text
        assert second.json()["featured_at"] == first["featured_at"]
        assert _featured_ids(client) == [event_id]


def test_at_most_three_events_are_featured(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_ids = [_publish(client, title=f"Event {index}") for index in range(4)]
        for event_id in event_ids[:3]:
            assert _feature(client, event_id).status_code == 200
        refused = _feature(client, event_ids[3])
        assert refused.status_code == 409, refused.text
        assert "unfeature" in refused.json()["error"]["message"].lower()

        client.delete(f"/api/v1/admin/events/{event_ids[0]}/featured", headers=ADMIN)
        assert _feature(client, event_ids[3]).status_code == 200
        # Newest pin first.
        assert _featured_ids(client) == [event_ids[3], event_ids[2], event_ids[1]]


def test_past_event_leaves_the_feature_and_frees_its_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path) as client:
        soon = _publish(client, 1, title="Soon")
        later = [_publish(client, 10, title=f"Later {index}") for index in range(3)]
        for event_id in (soon, *later[:2]):
            assert _feature(client, event_id).status_code == 200
        assert _feature(client, later[2]).status_code == 409

        # Once "Soon" has ended it is no longer featured anywhere.
        _travel_to(monkeypatch, datetime.combine(_day(2), datetime.min.time(), UTC))
        assert soon not in _featured_ids(client)
        assert client.get(f"/api/v1/events/{soon}").json()["is_featured"] is False

        # ...and its slot is free again.
        assert _feature(client, later[2]).status_code == 200
        assert _featured_ids(client) == [later[2], later[1], later[0]]

        # Pinning cleared the expired pin for good.
        with client.app.state.database.connection() as connection:
            row = connection.execute(
                "SELECT featured_at FROM events WHERE id = ?", (soon,)
            ).fetchone()
        assert row["featured_at"] is None


def test_series_stays_featured_until_its_last_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, 1, recurrence_rule="FREQ=WEEKLY;COUNT=3")
        assert _feature(client, event_id).status_code == 200
        first = client.get("/api/v1/featured").json()["items"][0]
        assert first["starts_at"].startswith(_day(1).isoformat())

        # After the first date the card moves on to the next one.
        _travel_to(monkeypatch, datetime.combine(_day(2), datetime.min.time(), UTC))
        item = client.get("/api/v1/featured").json()["items"][0]
        assert item["event_id"] == event_id
        assert item["starts_at"].startswith(_day(8).isoformat())
        assert item["occurrence_id"] != first["occurrence_id"]

        # After the last date the series is past.
        _travel_to(monkeypatch, datetime.combine(_day(16), datetime.min.time(), UTC))
        assert _featured_ids(client) == []


def test_all_day_event_is_featured_through_its_last_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(
            client,
            is_all_day=True,
            starts_at=None,
            ends_at=None,
            start_date=_day(2).isoformat(),
            end_date=_day(3).isoformat(),
        )
        assert _feature(client, event_id).status_code == 200
        _travel_to(monkeypatch, datetime.combine(_day(2), datetime.min.time(), UTC) + timedelta(hours=12))
        assert _featured_ids(client) == [event_id]
        _travel_to(monkeypatch, datetime.combine(_day(4), datetime.min.time(), UTC) + timedelta(hours=1))
        assert _featured_ids(client) == []


def test_past_events_cannot_be_featured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, 1)
        _travel_to(monkeypatch, datetime.combine(_day(5), datetime.min.time(), UTC))
        refused = _feature(client, event_id)
        assert refused.status_code == 409, refused.text
        assert _featured_ids(client) == []


def test_cancelled_events_leave_the_feature(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client)
        assert _feature(client, event_id).status_code == 200
        cancelled = client.post(
            f"/api/v1/events/{event_id}/cancel", headers=ADMIN, json={"actor": "admin"}
        )
        assert cancelled.status_code == 200, cancelled.text
        assert _featured_ids(client) == []
        assert client.get(f"/api/v1/events/{event_id}").json()["is_featured"] is False
        assert _feature(client, event_id).status_code == 409


def test_unpublished_and_deleted_events_leave_the_feature(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        revoked = _publish(client, title="Revoked")
        deleted = _publish(client, title="Deleted")
        for event_id in (revoked, deleted):
            assert _feature(client, event_id).status_code == 200
        client.post(
            f"/api/v1/admin/events/{revoked}/revoke",
            headers=ADMIN,
            json={"actor": "admin", "note": "duplicate"},
        )
        client.delete(f"/api/v1/events/{deleted}", headers=ADMIN)
        assert _featured_ids(client) == []
        assert _feature(client, revoked).status_code == 404
        assert _feature(client, deleted).status_code == 404
        # Neither occupies a slot.
        for index in range(3):
            assert _feature(client, _publish(client, title=f"New {index}")).status_code == 200


def test_pending_and_missing_events_cannot_be_featured(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        pending = client.post("/api/v1/events", json=_payload()).json()["event_id"]
        assert _feature(client, pending).status_code == 404
        missing = "00000000-0000-4000-8000-000000000000"
        assert _feature(client, missing).status_code == 404
        assert (
            client.delete(
                f"/api/v1/admin/events/{missing}/featured", headers=ADMIN
            ).status_code
            == 404
        )


def test_featured_list_requires_the_link_token(tmp_path: Path) -> None:
    with _client(tmp_path, calendar_access_token=LINK_TOKEN) as client:
        assert client.get("/api/v1/featured").status_code == 401
        allowed = client.get(f"/api/v1/featured?token={LINK_TOKEN}")
        assert allowed.status_code == 200, allowed.text


def test_older_local_database_gains_the_featured_column(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'featured.db'}"
    database = create_database(url)
    database.open()
    with database.connection() as connection:
        connection.execute("DROP INDEX local_events_featured_idx")
        connection.execute("ALTER TABLE events DROP COLUMN featured_at")
    with _client(tmp_path) as client:
        event_id = _publish(client)
        assert _feature(client, event_id).status_code == 200
        assert _featured_ids(client) == [event_id]


def test_shell_contains_featured_strip_and_toggle(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        html = client.get("/").text
        javascript = client.get("/static/app.js").text
        stylesheet = client.get("/static/styles.css").text
    for marker in ('id="featured-events"', 'id="feature-event-button"'):
        assert marker in html
    for marker in ("/api/v1/featured", "/featured`", "function renderFeatured"):
        assert marker in javascript
    assert ".featured-strip" in stylesheet
    assert ".featured-marker" in stylesheet


def test_featured_cards_keep_a_fixed_size(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        javascript = client.get("/static/app.js").text
        stylesheet = client.get("/static/styles.css").text
    # One column per slot whether one or three events are featured, so a
    # lone card never stretches across the strip.
    rule = stylesheet[stylesheet.index(".featured-list {"):]
    rule = rule[: rule.index("}")]
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in rule
    assert "auto-fit" not in rule
    # Every card reserves its location line, so cards share one height.
    assert 'place.textContent = where || "\\u00a0"' in javascript
