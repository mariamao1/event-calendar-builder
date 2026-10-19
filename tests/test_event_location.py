from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database

ADMIN = {"X-Admin-Key": "test-admin-key"}
LINK = "https://meet.example.org/choir-room"


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'event-location.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def _first_day():
    return datetime.now(UTC).date() + timedelta(days=4)


def _payload(**overrides) -> dict:
    day = _first_day()
    payload = {
        "title": "Tuesday choir",
        "description": "All voices welcome.",
        "location_name": None,
        "location_address": None,
        "virtual_url": LINK,
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


def _occurrences(client: TestClient, event_id: str, **params) -> list[dict]:
    start = _first_day() - timedelta(days=1)
    response = client.get(
        "/api/v1/calendar",
        params={"start": str(start), "end": str(start + timedelta(days=60)), "timezone": "UTC", **params},
    )
    assert response.status_code == 200, response.text
    return [item for item in response.json()["items"] if item["event_id"] == event_id]


def _unfold(body: str) -> list[str]:
    return body.replace("\r\n ", "").split("\r\n")


def _value(lines: list[str], name: str) -> str | None:
    for line in lines:
        if line.startswith(f"{name}:"):
            return line.split(":", 1)[1]
    return None


def test_virtual_url_is_optional_and_must_be_http(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        for bad in ("zoom.us/j/123", "javascript:alert(1)", "ftp://example.org/room"):
            response = client.post("/api/v1/events", json=_payload(virtual_url=bad))
            assert response.status_code == 422, (bad, response.text)

        # No location of any kind is still a valid event.
        bare = _payload(virtual_url=None)
        bare.pop("virtual_url")
        event_id = _publish(client, bare)
        assert client.get(f"/api/v1/events/{event_id}").json()["virtual_url"] is None

        blank = _publish(client, _payload(title="Blank link", virtual_url="  "))
        assert client.get(f"/api/v1/events/{blank}").json()["virtual_url"] is None


def test_virtual_url_round_trips_through_review_and_reads(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        submitted = client.post(
            "/api/v1/events",
            json=_payload(location_name="Library hall", location_address="1 Main Street"),
        )
        assert submitted.status_code == 202, submitted.text
        event_id = submitted.json()["event_id"]
        revision_id = submitted.json()["revision_id"]

        revisions = client.get(f"/api/v1/admin/events/{event_id}", headers=ADMIN).json()["revisions"]
        assert revisions[0]["virtual_url"] == LINK
        # The creator's edit form loads the link back.
        editable = client.get(
            f"/api/v1/events/{event_id}/manage",
            headers={"X-Event-Management-Token": submitted.json()["management_token"]},
        )
        assert editable.status_code == 200, editable.text
        assert editable.json()["virtual_url"] == LINK

        # An admin correction before approval rewrites the pending revision.
        edited_link = "https://meet.example.org/new-room"
        edited = client.post(
            f"/api/v1/events/{event_id}/revisions",
            headers=ADMIN,
            json=_payload(
                location_name="Library hall",
                location_address="1 Main Street",
                virtual_url=edited_link,
            ),
        )
        assert edited.status_code == 202, edited.text
        approved = client.post(
            f"/api/v1/admin/events/{event_id}/revisions/{revision_id}/approve",
            headers=ADMIN,
            json={"actor": "moderator"},
        )
        assert approved.status_code == 200, approved.text

        detail = client.get(f"/api/v1/events/{event_id}").json()
        assert detail["location_name"] == "Library hall"
        assert detail["virtual_url"] == edited_link
        [item] = _occurrences(client, event_id)
        assert item["virtual_url"] == edited_link

        # A later edit of the published event can drop the link again.
        cleared = client.post(
            f"/api/v1/admin/events/{event_id}/revisions",
            headers=ADMIN,
            json=_payload(location_name="Library hall", virtual_url=None),
        )
        assert cleared.status_code == 201, cleared.text
        assert client.get(f"/api/v1/events/{event_id}").json()["virtual_url"] is None


def test_single_date_can_have_its_own_link(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload(recurrence_rule="FREQ=WEEKLY;COUNT=3"))
        occurrences = _occurrences(client, event_id)
        assert len(occurrences) == 3
        target = occurrences[1]
        guest_link = "https://meet.example.org/guest-night"

        edited = client.post(
            f"/api/v1/admin/events/{event_id}/revisions"
            f"?scope=single&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_payload(
                virtual_url=guest_link,
                starts_at=target["starts_at"],
                ends_at=target["ends_at"],
            ),
        )
        assert edited.status_code == 201, edited.text

        links = [item["virtual_url"] for item in _occurrences(client, event_id)]
        assert links == [LINK, guest_link, LINK]
        detail = client.get(
            f"/api/v1/events/{event_id}?occurrence={target['occurrence_id']}"
        ).json()
        assert detail["virtual_url"] == LINK
        assert detail["occurrence"]["virtual_url"] == guest_link


def test_search_matches_virtual_link(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        _publish(client, _payload(title="Book club", virtual_url=None, location_name="Cafe"))
        found = _occurrences(client, event_id, q="meet.example.org")
        assert [item["event_id"] for item in found] == [event_id]


def test_existing_sqlite_database_gains_virtual_url(tmp_path: Path) -> None:
    path = tmp_path / "event-location.db"
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload(virtual_url=None, location_name="Hall"))
    # Simulate a calendar created before the column existed.
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE event_revisions DROP COLUMN virtual_url")
    connection.commit()
    connection.close()

    with _client(tmp_path) as client:
        detail = client.get(f"/api/v1/events/{event_id}").json()
        assert detail["location_name"] == "Hall"
        assert detail["virtual_url"] is None
        online = _publish(client, _payload(title="Online choir"))
        assert client.get(f"/api/v1/events/{online}").json()["virtual_url"] == LINK


def test_exports_include_virtual_link(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        online = _publish(client, _payload())
        hybrid = _publish(
            client,
            _payload(title="Hybrid choir", location_name="Library hall", location_address="1 Main Street"),
        )

        lines = _unfold(client.get(f"/api/v1/events/{online}/ics").text)
        assert _value(lines, "LOCATION") == LINK
        assert f"Join online: {LINK}" in _value(lines, "DESCRIPTION")
        assert _value(lines, "URL") == "https://example.org/choir"

        lines = _unfold(client.get(f"/api/v1/events/{hybrid}/ics").text)
        assert _value(lines, "LOCATION") == "Library hall · 1 Main Street"
        assert _value(lines, "DESCRIPTION") == f"All voices welcome.\\n\\nJoin online: {LINK}"

        redirect = client.get(f"/api/v1/events/{online}/google", follow_redirects=False)
        assert redirect.status_code == 303
        params = parse_qs(urlparse(redirect.headers["location"]).query)
        assert params["location"] == [LINK]
        assert params["details"] == [f"All voices welcome.\n\nJoin online: {LINK}"]


def test_frontend_offers_online_link(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        html = client.get("/").text
        javascript = client.get("/static/app.js").text
    for marker in ('id="event-virtual-url"', "Event location", "Online link"):
        assert marker in html
    for marker in (
        'document.querySelector("#event-virtual-url")',
        "virtual_url:",
        'shown("virtual_url")',
        "revision.virtual_url",
        "function onlineLinkRow",
    ):
        assert marker in javascript
