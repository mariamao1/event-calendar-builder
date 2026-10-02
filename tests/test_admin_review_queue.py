"""Focused coverage for the admin review queue.

Admins need a queue of pending submissions to work through: listing
everything awaiting review, seeing the entire proposal, approving or
rejecting it (approval publishes to the calendar for all viewers), and
editing a submission before it is approved.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'admin-review-queue.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def _group(client: TestClient) -> dict:
    return client.post(
        "/api/v1/admin/groups",
        headers={"X-Admin-Key": "test-admin-key"},
        json={"slug": "neighbors", "name": "Neighbors"},
    ).json()


def _payload(group_id: str, title: str = "Block party") -> dict:
    first_day = datetime.now(UTC).date() + timedelta(days=5)
    return {
        "title": title,
        "description": "Meet your neighbors.",
        "location_name": "Community garden",
        "location_address": "10 Main Street",
        "event_url": "https://example.org/block-party",
        "is_all_day": True,
        "start_date": first_day.isoformat(),
        "end_date": (first_day + timedelta(days=1)).isoformat(),
        "timezone": "America/New_York",
        "recurrence_rule": None,
        "recurrence_dates": [],
        "group_ids": [group_id],
        "submitter": {
            "name": "Taylor",
            "channel": "email",
            "contact": "taylor@example.org",
        },
    }


def _calendar_titles(client: TestClient, day) -> list[str]:
    response = client.get(
        f"/api/v1/calendar?start={day.isoformat()}"
        f"&end={(day + timedelta(days=2)).isoformat()}&timezone=UTC"
    )
    assert response.status_code == 200, response.text
    return [item["title"] for item in response.json()["items"]]


def test_approved_submission_appears_on_calendar(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        group = _group(client)
        submitted = client.post("/api/v1/events", json=_payload(group["id"]))
        assert submitted.status_code == 202, submitted.text
        identity = submitted.json()
        day = datetime.now(UTC).date() + timedelta(days=5)

        # Pending submissions wait in the admin queue, invisible to viewers.
        queue = client.get(
            "/api/v1/admin/events?status=pending",
            headers={"X-Admin-Key": "test-admin-key"},
        )
        assert queue.status_code == 200, queue.text
        assert [item["event_id"] for item in queue.json()["items"]] == [
            identity["event_id"]
        ]
        assert _calendar_titles(client, day) == []

        # The admin audit view exposes the entire proposal for review.
        audit = client.get(
            f"/api/v1/admin/events/{identity['event_id']}",
            headers={"X-Admin-Key": "test-admin-key"},
        )
        assert audit.status_code == 200, audit.text
        current = audit.json()["revisions"][0]
        assert current["title"] == "Block party"
        assert current["location_name"] == "Community garden"
        assert current["location_address"] == "10 Main Street"
        assert current["description"] == "Meet your neighbors."

        approved = client.post(
            f"/api/v1/admin/events/{identity['event_id']}"
            f"/revisions/{identity['revision_id']}/approve",
            headers={"X-Admin-Key": "test-admin-key"},
            json={"actor": "moderator"},
        )
        assert approved.status_code == 200, approved.text

        # Approval publishes the event so every viewer sees it on the calendar.
        assert _calendar_titles(client, day) == ["Block party"]
        assert (
            client.get(
                "/api/v1/admin/events?status=pending",
                headers={"X-Admin-Key": "test-admin-key"},
            ).json()["items"]
            == []
        )


def test_rejected_submission_stays_off_calendar(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        group = _group(client)
        submitted = client.post("/api/v1/events", json=_payload(group["id"]))
        assert submitted.status_code == 202, submitted.text
        identity = submitted.json()
        day = datetime.now(UTC).date() + timedelta(days=5)

        rejected = client.post(
            f"/api/v1/admin/events/{identity['event_id']}"
            f"/revisions/{identity['revision_id']}/reject",
            headers={"X-Admin-Key": "test-admin-key"},
            json={"actor": "moderator", "note": "Duplicate event"},
        )
        assert rejected.status_code == 200, rejected.text
        assert _calendar_titles(client, day) == []


def test_admin_edits_submission_then_it_publishes(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        group = _group(client)
        submitted = client.post("/api/v1/events", json=_payload(group["id"]))
        assert submitted.status_code == 202, submitted.text
        identity = submitted.json()
        day = datetime.now(UTC).date() + timedelta(days=5)

        # An admin corrects the submission; the corrected event is published.
        edited = client.post(
            f"/api/v1/admin/events/{identity['event_id']}/revisions",
            headers={"X-Admin-Key": "test-admin-key"},
            json=_payload(group["id"], "Block party — corrected by admin"),
        )
        assert edited.status_code == 201, edited.text
        assert edited.json()["approval_status"] == "approved"
        assert _calendar_titles(client, day) == ["Block party — corrected by admin"]


def test_calendar_shell_contains_review_queue(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        shell = client.get("/").text
        javascript = client.get("/static/app.js").text
        stylesheet = client.get("/static/styles.css").text

    for marker in (
        'id="review-queue-button"',
        'id="review-queue-dialog"',
        'id="review-queue-list"',
        'id="review-detail-dialog"',
        'id="review-detail-title"',
        'id="review-approve-button"',
        'id="review-reject-button"',
        'id="review-edit-button"',
    ):
        assert marker in shell
    for symbol in (
        "loadReviewQueue",
        "renderReviewQueue",
        "openReviewQueue",
        "openReviewDetail",
        "approveRevision",
        "rejectRevision",
        "review-queue-list",
        "status=pending",
        "/api/v1/admin/events",
        "/revisions/",
        '"approve"',
        '"reject"',
    ):
        assert symbol in javascript
    for selector in (
        ".review-queue-list",
        ".review-card",
        ".review-actions",
        ".approve-button",
        ".reject-button",
    ):
        assert selector in stylesheet
