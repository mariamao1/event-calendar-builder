from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database

ADMIN = {"X-Admin-Key": "test-admin-key"}


def _client(tmp_path: Path, **settings) -> TestClient:
    url = f"sqlite:///{tmp_path / 'event-detail.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key", **settings),
            create_database(url),
        )
    )


def _first_day():
    return datetime.now(UTC).date() + timedelta(days=3)


def _payload(**overrides) -> dict:
    day = _first_day()
    payload = {
        "title": "Tuesday choir",
        "description": "Bring water.\nAll voices welcome.",
        "location_name": "Library hall",
        "location_address": "1 Main Street",
        "event_url": "https://example.org/choir",
        "is_all_day": False,
        "starts_at": datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC).isoformat(),
        "ends_at": datetime(day.year, day.month, day.day, 19, 30, tzinfo=UTC).isoformat(),
        "timezone": "UTC",
        "recurrence_rule": None,
        "recurrence_dates": [],
        "group_ids": [],
        "submitter": {"name": "Robin", "channel": "email", "contact": "robin@example.org"},
    }
    payload.update(overrides)
    return payload


def _publish(client: TestClient, payload: dict) -> str:
    response = client.post("/api/v1/admin/events", headers=ADMIN, json=payload)
    assert response.status_code == 201, response.text
    return response.json()["event_id"]


def _occurrence_ids(client: TestClient, event_id: str) -> list[str]:
    start = _first_day() - timedelta(days=1)
    response = client.get(
        f"/api/v1/calendar?start={start}&end={start + timedelta(days=90)}&timezone=UTC"
    )
    assert response.status_code == 200, response.text
    return [
        item["occurrence_id"]
        for item in response.json()["items"]
        if item["event_id"] == event_id
    ]


def test_detail_includes_series_context_for_recurring_event(tmp_path: Path) -> None:
    day = _first_day()
    skipped = day + timedelta(days=14)
    extra = day + timedelta(days=1)
    with _client(tmp_path) as client:
        group = client.post(
            "/api/v1/admin/groups", headers=ADMIN, json={"slug": "music", "name": "Music"}
        ).json()
        event_id = _publish(
            client,
            _payload(
                recurrence_rule="FREQ=WEEKLY;COUNT=5",
                recurrence_dates=[
                    {"local_start": f"{skipped}T18:00:00", "kind": "exclude"},
                    {"local_start": f"{extra}T18:00:00", "kind": "include"},
                ],
                group_ids=[group["id"]],
            ),
        )
        occurrences = _occurrence_ids(client, event_id)
        # Five weekly dates, minus one excluded, plus one added.
        assert len(occurrences) == 5

        response = client.get(f"/api/v1/events/{event_id}?occurrence={occurrences[1]}")
        assert response.status_code == 200, response.text
        detail = response.json()

        assert detail["title"] == "Tuesday choir"
        assert detail["description"] == "Bring water.\nAll voices welcome."
        assert detail["location_address"] == "1 Main Street"
        assert detail["event_url"] == "https://example.org/choir"
        assert [item["name"] for item in detail["groups"]] == ["Music"]
        assert detail["recurrence_rule"] == "FREQ=WEEKLY;COUNT=5"
        assert {item["kind"] for item in detail["recurrence_dates"]} == {"include", "exclude"}
        # Submitter details are moderation-only and never published.
        assert "submitted_by_contact" not in detail
        assert "submitted_by_name" not in detail

        selected = detail["occurrence"]
        assert selected["occurrence_id"] == occurrences[1]
        assert selected["is_exception"] is True
        assert selected["starts_at"].startswith(f"{extra}T18:00")

        series = detail["series"]
        assert series["previous"]["occurrence_id"] == occurrences[0]
        assert series["next"]["occurrence_id"] == occurrences[2]
        assert [item["occurrence_id"] for item in series["upcoming"]] == occurrences
        assert series["upcoming_count"] == 5
        assert series["coverage_end"]

        first = client.get(f"/api/v1/events/{event_id}?occurrence={occurrences[0]}").json()
        assert first["series"]["previous"] is None
        last = client.get(f"/api/v1/events/{event_id}?occurrence={occurrences[-1]}").json()
        assert last["series"]["next"] is None


def test_one_off_event_has_no_series(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload(title="Book swap"))
        [occurrence_id] = _occurrence_ids(client, event_id)

        detail = client.get(f"/api/v1/events/{event_id}?occurrence={occurrence_id}").json()
        assert detail["series"] is None
        assert detail["recurrence_dates"] == []
        assert detail["occurrence"]["occurrence_id"] == occurrence_id

        # The event alone is enough for a shared link.
        bare = client.get(f"/api/v1/events/{event_id}").json()
        assert bare["occurrence"] is None
        assert bare["title"] == "Book swap"


def test_unknown_or_foreign_occurrence_falls_back_to_event(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        first = _publish(client, _payload(title="First"))
        second = _publish(client, _payload(title="Second"))
        [foreign] = _occurrence_ids(client, second)

        detail = client.get(f"/api/v1/events/{first}?occurrence={foreign}")
        assert detail.status_code == 200
        assert detail.json()["occurrence"] is None
        assert client.get(f"/api/v1/events/{first}?occurrence=nope").status_code == 422


def test_revoked_and_pending_events_are_not_shareable(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        pending = client.post("/api/v1/events", json=_payload(title="Pending")).json()
        assert client.get(f"/api/v1/events/{pending['event_id']}").status_code == 404

        event_id = _publish(client, _payload(title="Cancelled talk"))
        assert client.get(f"/api/v1/events/{event_id}").status_code == 200
        revoked = client.post(
            f"/api/v1/admin/events/{event_id}/revoke",
            headers=ADMIN,
            json={"actor": "admin", "note": "Speaker unavailable"},
        )
        assert revoked.status_code == 200, revoked.text
        assert client.get(f"/api/v1/events/{event_id}").status_code == 404
        assert _occurrence_ids(client, event_id) == []


def test_event_detail_requires_link_token(tmp_path: Path) -> None:
    with _client(tmp_path, calendar_access_token="secret-link") as client:
        response = client.post(
            "/api/v1/admin/events", headers=ADMIN, json=_payload(title="Private")
        )
        event_id = response.json()["event_id"]
        assert client.get(f"/api/v1/events/{event_id}").status_code == 401
        assert client.get(f"/api/v1/events/{event_id}?token=secret-link").status_code == 200
        # The shared deep link is the calendar shell with event params.
        assert client.get(f"/?event={event_id}").status_code == 401
        assert client.get(f"/?event={event_id}&token=secret-link").status_code == 200


def test_shell_contains_event_detail_view(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        html = client.get("/").text
        javascript = client.get("/static/app.js").text
        stylesheet = client.get("/static/styles.css").text

    # Exactly one website link; the detail view owns its sections and actions.
    assert html.count('id="event-dialog-link"') == 1
    for marker in (
        'id="event-dialog-series"',
        'id="event-dialog-admin"',
        'id="copy-event-link-button"',
        'id="edit-event-button"',
        'id="review-event-edit-button"',
        'id="unpublish-event-button"',
    ):
        assert marker in html
    for marker in (
        "function describeRecurrence",
        "function eventShareUrl",
        "function loadEventDetail",
        "function openEventFromUrl",
        'searchParams.set("event"',
        'searchParams.delete("occurrence"',
        "/revoke",
    ):
        assert marker in javascript
    assert ".series-date" in stylesheet
    assert ".event-admin-panel" in stylesheet
