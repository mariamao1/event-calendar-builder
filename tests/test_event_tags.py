from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database

ADMIN = {"X-Admin-Key": "test-admin-key"}


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'event-tags.db'}"
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


def _payload(group_ids: list[str], title: str = "Tag night") -> dict:
    day = datetime.now(UTC).date() + timedelta(days=4)
    return {
        "title": title,
        "is_all_day": False,
        "starts_at": datetime(day.year, day.month, day.day, 17, tzinfo=UTC).isoformat(),
        "ends_at": datetime(day.year, day.month, day.day, 19, tzinfo=UTC).isoformat(),
        "timezone": "UTC",
        "group_ids": group_ids,
        "submitter": {"name": "Robin", "channel": "email", "contact": "robin@example.org"},
    }


def test_events_tagged_with_multiple_groups(tmp_path: Path) -> None:
    """Events keep every assigned group and expose those tags to readers."""
    with _client(tmp_path) as client:
        arts = _group(client, "arts")
        music = _group(client, "music")

        response = client.post(
            "/api/v1/admin/events",
            headers=ADMIN,
            json=_payload([arts["id"], music["id"]]),
        )
        assert response.status_code == 201, response.text
        event_id = response.json()["event_id"]

        day = datetime.now(UTC).date()
        calendar = client.get(
            f"/api/v1/calendar?start={day}&end={day + timedelta(days=60)}&timezone=UTC"
        )
        assert calendar.status_code == 200, calendar.text
        [item] = calendar.json()["items"]
        assert {group["slug"] for group in item["groups"]} == {"arts", "music"}
        assert all(group["color"] for group in item["groups"])

        detail = client.get(f"/api/v1/events/{event_id}")
        assert detail.status_code == 200, detail.text
        assert {group["slug"] for group in detail.json()["groups"]} == {"arts", "music"}

        # Filtering by either group still finds the multi-tagged event.
        for slug in ("arts", "music"):
            filtered = client.get(
                f"/api/v1/calendar?start={day}&end={day + timedelta(days=60)}"
                f"&timezone=UTC&group={slug}"
            )
            assert filtered.status_code == 200, filtered.text
            assert [entry["event_id"] for entry in filtered.json()["items"]] == [event_id]


def test_calendar_views_render_group_tags(tmp_path: Path) -> None:
    """Month/week/day items show one visible tag per group, not just a color."""
    with _client(tmp_path) as client:
        javascript = client.get("/static/app.js").text
        stylesheet = client.get("/static/styles.css").text
        shell = client.get("/").text

    # The submission form offers every active group as a checkbox for tagging.
    assert 'id="event-groups"' in shell
    assert "loadEventGroups" in javascript

    # One tag per group, rendered on every calendar view.
    assert "function eventTagGroups" in javascript
    assert "function appendGroupTags" in javascript
    for renderer in (
        "renderBand",
        "renderTimedEvent",
        "renderTimeBlock",
        "allday-chip",
        "openDayDialog",
    ):
        section = javascript
        if renderer.startswith("render") or renderer == "openDayDialog":
            start = javascript.index(f"function {renderer}")
            end = javascript.index("function ", start + len(renderer) + 10)
            section = javascript[start:end]
        assert "appendGroupTags" in section, renderer
        assert "event-tag" in javascript, renderer
    assert ".event-tag" in stylesheet
    assert ".event-tags" in stylesheet
