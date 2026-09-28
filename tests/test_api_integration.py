from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import apply_migrations, create_database


@pytest.fixture(scope="module")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    return os.environ.get("TEST_DATABASE_URL") or (
        f"sqlite:///{tmp_path_factory.mktemp('calendar') / 'test.db'}"
    )


@pytest.fixture(scope="module")
def client(database_url: str) -> TestClient:
    root = Path(__file__).resolve().parents[1]
    if not database_url.startswith("sqlite:///"):
        apply_migrations(database_url, root / "db" / "migrations")
    database = create_database(database_url, min_size=1, max_size=3)
    settings = Settings(database_url=database_url, admin_api_key="test-admin-key")
    app = create_app(settings, database)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_database(client: TestClient) -> None:
    database = client.app.state.database
    with database.transaction() as connection:
        if database.dialect == "postgresql":
            connection.execute("TRUNCATE groups, events RESTART IDENTITY CASCADE")
        else:
            connection.executescript(
                """
                UPDATE events SET current_revision_id = NULL, published_revision_id = NULL;
                DELETE FROM event_review_actions;
                DELETE FROM event_occurrence_materializations;
                DELETE FROM event_occurrences;
                DELETE FROM event_revision_recurrence_dates;
                DELETE FROM event_revision_groups;
                DELETE FROM event_revisions;
                DELETE FROM events;
                DELETE FROM groups;
                """
            )


def _headers() -> dict[str, str]:
    return {"X-Admin-Key": "test-admin-key"}


def _create_group(client: TestClient, slug: str = "arts") -> dict:
    response = client.post(
        "/api/v1/admin/groups",
        headers=_headers(),
        json={"slug": slug, "name": slug.title(), "description": "Local events"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _event_payload(group_id: str, *, title: str = "Drawing club") -> dict:
    zone = ZoneInfo("America/New_York")
    starts_at = (datetime.now(zone) + timedelta(days=7)).replace(
        hour=18, minute=0, second=0, microsecond=0
    )
    return {
        "title": title,
        "description": "Bring a sketchbook.",
        "location_name": "Library",
        "is_all_day": False,
        "starts_at": starts_at.isoformat(),
        "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
        "timezone": "America/New_York",
        "recurrence_rule": "FREQ=DAILY;COUNT=3",
        "group_ids": [group_id],
        "submitter": {
            "name": "Alex",
            "channel": "email",
            "contact": "ALEX@example.org",
        },
    }


def _calendar_url(slug: str = "arts") -> str:
    today = datetime.now(UTC).date()
    return (
        f"/api/v1/calendar?start={today.isoformat()}"
        f"&end={(today + timedelta(days=20)).isoformat()}"
        f"&timezone=America/New_York&group={slug}"
    )


def test_full_moderation_and_calendar_query_flow(client: TestClient) -> None:
    assert client.get("/api/v1/admin/events").status_code == 401
    group = _create_group(client)
    submitted = client.post("/api/v1/events", json=_event_payload(group["id"]))
    assert submitted.status_code == 202, submitted.text
    identity = submitted.json()

    # Pending content never leaks into public reads.
    assert client.get(_calendar_url()).json()["items"] == []
    assert client.get(f"/api/v1/events/{identity['event_id']}").status_code == 404

    approved = client.post(
        f"/api/v1/admin/events/{identity['event_id']}"
        f"/revisions/{identity['revision_id']}/approve",
        headers=_headers(),
        json={"actor": "moderator@example.org", "note": "Verified"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["occurrence_count"] == 3
    detail = client.get(
        f"/api/v1/admin/events/{identity['event_id']}", headers=_headers()
    )
    assert detail.json()["approval_status"] == "approved"

    calendar = client.get(_calendar_url())
    assert calendar.status_code == 200, calendar.text
    first_items = calendar.json()["items"]
    assert len(first_items) == 3
    unfiltered_url = _calendar_url().replace("&group=arts", "")
    assert len(client.get(unfiltered_url).json()["items"]) == 3
    assert {item["title"] for item in first_items} == {"Drawing club"}
    assert all(item["groups"][0]["slug"] == "arts" for item in first_items)
    original_ids = [item["occurrence_id"] for item in first_items]
    original_versions = [item["version"] for item in first_items]

    # Extending the rolling window is idempotent for rows already materialized.
    database = client.app.state.database
    with database.transaction() as connection:
        placeholder = "%s" if database.dialect == "postgresql" else "?"
        connection.execute(
            f"""
            UPDATE event_occurrence_materializations
               SET window_end_exclusive = current_date
             WHERE event_id = {placeholder}
            """,
            (identity["event_id"],),
        )
        result = client.app.state.service.materialize_all(
            connection, past_days=90, future_months=18
        )
    assert result == {"events_processed": 1, "occurrences_inserted": 0}
    unchanged_items = client.get(_calendar_url()).json()["items"]
    assert [item["version"] for item in unchanged_items] == original_versions

    revised = client.post(
        f"/api/v1/events/{identity['event_id']}/revisions",
        headers=_headers(),
        json=_event_payload(group["id"], title="Drawing club—new room"),
    )
    assert revised.status_code == 202, revised.text
    # Last approved content remains live while the edit is pending.
    assert {item["title"] for item in client.get(_calendar_url()).json()["items"]} == {
        "Drawing club"
    }

    revised_identity = revised.json()
    response = client.post(
        f"/api/v1/admin/events/{identity['event_id']}"
        f"/revisions/{revised_identity['revision_id']}/approve",
        headers=_headers(),
        json={"actor": "moderator@example.org"},
    )
    assert response.status_code == 200, response.text
    changed_items = client.get(_calendar_url()).json()["items"]
    assert {item["title"] for item in changed_items} == {"Drawing club—new room"}
    assert [item["occurrence_id"] for item in changed_items] == original_ids
    assert [item["version"] for item in changed_items] == [
        version + 1 for version in original_versions
    ]

    revoked = client.post(
        f"/api/v1/admin/events/{identity['event_id']}/revoke",
        headers=_headers(),
        json={"actor": "moderator@example.org", "note": "Cancelled"},
    )
    assert revoked.status_code == 200, revoked.text
    assert client.get(_calendar_url()).json()["items"] == []


def test_group_filter_and_rejection_preserve_publication(client: TestClient) -> None:
    arts = _create_group(client, "arts")
    _create_group(client, "sports")
    submitted = client.post("/api/v1/events", json=_event_payload(arts["id"])).json()
    client.post(
        f"/api/v1/admin/events/{submitted['event_id']}"
        f"/revisions/{submitted['revision_id']}/approve",
        headers=_headers(),
        json={"actor": "mod"},
    )
    assert len(client.get(_calendar_url("arts")).json()["items"]) == 3
    assert client.get(_calendar_url("sports")).json()["items"] == []
    assert client.get(_calendar_url("does-not-exist")).status_code == 422

    revised = client.post(
        f"/api/v1/events/{submitted['event_id']}/revisions",
        headers=_headers(),
        json=_event_payload(arts["id"], title="Rejected title"),
    ).json()
    rejected = client.post(
        f"/api/v1/admin/events/{submitted['event_id']}"
        f"/revisions/{revised['revision_id']}/reject",
        headers=_headers(),
        json={"actor": "mod", "note": "Not enough details"},
    )
    assert rejected.status_code == 200, rejected.text
    assert {item["title"] for item in client.get(_calendar_url()).json()["items"]} == {
        "Drawing club"
    }
