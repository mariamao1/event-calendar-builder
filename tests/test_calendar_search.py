from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database

ADMIN = {"X-Admin-Key": "test-admin-key"}


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'calendar-search.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def _group(client: TestClient, slug: str) -> dict:
    response = client.post(
        "/api/v1/admin/groups",
        headers=ADMIN,
        json={"slug": slug, "name": slug.title()},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _payload(title: str, group_ids: list[str] | None = None, **overrides) -> dict:
    day = datetime.now(UTC).date() + timedelta(days=4)
    payload = {
        "title": title,
        "is_all_day": False,
        "starts_at": datetime(day.year, day.month, day.day, 17, tzinfo=UTC).isoformat(),
        "ends_at": datetime(day.year, day.month, day.day, 19, tzinfo=UTC).isoformat(),
        "timezone": "UTC",
        "group_ids": group_ids or [],
        "submitter": {"name": "Robin", "channel": "email", "contact": "robin@example.org"},
    }
    payload.update(overrides)
    return payload


def _publish(client: TestClient, payload: dict) -> str:
    response = client.post("/api/v1/admin/events", headers=ADMIN, json=payload)
    assert response.status_code == 201, response.text
    return response.json()["event_id"]


def _calendar(client: TestClient, query: str = "") -> dict:
    day = datetime.now(UTC).date()
    response = client.get(
        f"/api/v1/calendar?start={day}&end={day + timedelta(days=60)}&timezone=UTC{query}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _seed(client: TestClient) -> dict:
    music = _group(client, "music")
    jazz = _publish(
        client,
        _payload(
            "Jazz Night",
            [music["id"]],
            description="An evening of smooth saxophone",
            location_name="Blue Hall",
        ),
    )
    pottery = _publish(
        client,
        _payload(
            "Pottery Class",
            description="Clay, kiln, and glazing basics",
            location_name="Art Room",
        ),
    )
    return {"music": music, "jazz": jazz, "pottery": pottery}


def test_search_matches_title_description_and_location(tmp_path: Path) -> None:
    """One query searches titles, descriptions, and locations alike."""
    with _client(tmp_path) as client:
        ids = _seed(client)

        assert [_calendar(client)["meta"]["search"]] == [""]
        assert len(_calendar(client)["items"]) == 2

        for query in ("q=jazz", "search=jazz", "query=jazz"):
            body = _calendar(client, f"&{query}")
            assert [item["event_id"] for item in body["items"]] == [ids["jazz"]]
            assert body["meta"]["search"] == "jazz"

        # Case does not matter; description and location are searchable too.
        assert [item["event_id"] for item in _calendar(client, "&q=JAZZ")["items"]] == [
            ids["jazz"]
        ]
        assert [item["event_id"] for item in _calendar(client, "&q=saxophone")["items"]] == [
            ids["jazz"]
        ]
        assert [item["event_id"] for item in _calendar(client, "&q=blue+hall")["items"]] == [
            ids["jazz"]
        ]

        # Several words all have to match somewhere on the event.
        body = _calendar(client, "&q=clay+kiln")
        assert [item["event_id"] for item in body["items"]] == [ids["pottery"]]
        assert _calendar(client, "&q=clay+jazz")["items"] == []

        # Blank queries and wildcard characters behave literally.
        assert len(_calendar(client, "&q=++")["items"]) == 2
        assert _calendar(client, "&q=jazz%25")["items"] == []


def test_search_combines_with_group_filter(tmp_path: Path) -> None:
    """Search narrows within the selected groups instead of replacing them."""
    with _client(tmp_path) as client:
        ids = _seed(client)

        body = _calendar(client, "&group=music&q=jazz")
        assert [item["event_id"] for item in body["items"]] == [ids["jazz"]]

        assert _calendar(client, "&group=music&q=pottery")["items"] == []
        assert _calendar(client, "&q=pottery&group=music")["items"] == []


def test_search_feed_matches_calendar(tmp_path: Path) -> None:
    """The subscribable feed honors the same query as the calendar read."""
    with _client(tmp_path) as client:
        _seed(client)
        day = datetime.now(UTC).date()
        feed = client.get(
            f"/api/v1/calendar.ics?start={day}&end={day + timedelta(days=60)}"
            "&timezone=UTC&q=jazz"
        )
        assert feed.status_code == 200, feed.text
        assert "Jazz" in feed.text
        assert "Pottery" not in feed.text


def test_search_box_filters_alongside_group_chips(tmp_path: Path) -> None:
    """The calendar shell offers a search box wired into calendar requests."""
    with _client(tmp_path) as client:
        shell = client.get("/").text
        javascript = client.get("/static/app.js").text

    assert 'id="event-search"' in shell
    assert 'id="search-results"' in shell
    assert 'params.set("q"' in javascript
    assert "function setSearch" in javascript
    assert "function searchSummary" in javascript

    # Matching activities are listed under the search bar; selecting a row
    # opens that event's detail view.
    assert "function renderSearchResults" in javascript
    assert "search-results-list" in javascript
    assert ".search-results" in client.get("/static/styles.css").text
    section_start = javascript.index("function renderSearchResults")
    section_end = javascript.index("function ", section_start + 10)
    section = javascript[section_start:section_end]
    assert "openEventDialog" in section
