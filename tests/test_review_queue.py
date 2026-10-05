from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'review-queue.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def _payload(title: str = "Rooftop cinema night") -> dict:
    first_day = datetime.now(UTC).date() + timedelta(days=5)
    return {
        "title": title,
        "description": "An outdoor screening under the stars.",
        "location_name": "Rooftop hall",
        "location_address": "42 Skyline Ave",
        "event_url": "https://example.org/rooftop-cinema",
        "is_all_day": False,
        "starts_at": datetime(
            first_day.year, first_day.month, first_day.day, 19, 0, tzinfo=UTC
        ).isoformat(),
        "ends_at": datetime(
            first_day.year, first_day.month, first_day.day, 21, 30, tzinfo=UTC
        ).isoformat(),
        "timezone": "America/New_York",
        "recurrence_rule": None,
        "recurrence_dates": [],
        "group_ids": [],
        "submitter": {
            "name": "Robin",
            "channel": "email",
            "contact": "robin@example.org",
        },
    }


def _headers() -> dict[str, str]:
    return {"X-Admin-Key": "test-admin-key"}


def _calendar_titles(client: TestClient, day) -> list[str]:
    response = client.get(
        f"/api/v1/calendar?start={day.isoformat()}"
        f"&end={(day + timedelta(days=1)).isoformat()}&timezone=UTC"
    )
    assert response.status_code == 200, response.text
    return [item["title"] for item in response.json()["items"]]


def test_pending_submission_waits_in_queue_until_approved(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/api/v1/events", json=_payload())
        assert created.status_code == 202, created.text
        identity = created.json()
        event_id, revision_id = identity["event_id"], identity["revision_id"]
        day = datetime.now(UTC).date() + timedelta(days=5)

        # Pending content stays off the public calendar and event read.
        assert _calendar_titles(client, day) == []
        assert client.get(f"/api/v1/events/{event_id}").status_code == 404

        # The queue lists the whole proposal for the admin.
        queue = client.get("/api/v1/admin/events?status=pending", headers=_headers())
        assert queue.status_code == 200, queue.text
        items = queue.json()["items"]
        assert [item["event_id"] for item in items] == [event_id]
        assert items[0]["title"] == "Rooftop cinema night"
        assert items[0]["description"] == "An outdoor screening under the stars."
        assert items[0]["submitted_by_name"] == "Robin"

        detail = client.get(
            f"/api/v1/admin/events/{event_id}", headers=_headers()
        )
        assert detail.status_code == 200, detail.text
        revisions = detail.json()["revisions"]
        assert len(revisions) == 1
        assert revisions[0]["description"] == "An outdoor screening under the stars."
        assert revisions[0]["location_address"] == "42 Skyline Ave"
        assert revisions[0]["event_url"] == "https://example.org/rooftop-cinema"

        # Approval publishes the event for every viewer.
        approved = client.post(
            f"/api/v1/admin/events/{event_id}/revisions/{revision_id}/approve",
            headers=_headers(),
            json={"actor": "moderator"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["approval_status"] == "approved"
        assert _calendar_titles(client, day) == ["Rooftop cinema night"]
        public = client.get(f"/api/v1/events/{event_id}")
        assert public.status_code == 200, public.text
        assert public.json()["title"] == "Rooftop cinema night"

        # Nothing is left waiting in the queue.
        remaining = client.get(
            "/api/v1/admin/events?status=pending", headers=_headers()
        ).json()["items"]
        assert remaining == []


def test_rejected_submission_never_reaches_calendar(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/api/v1/events", json=_payload("Late-night rave"))
        assert created.status_code == 202, created.text
        event_id, revision_id = created.json()["event_id"], created.json()["revision_id"]
        day = datetime.now(UTC).date() + timedelta(days=5)

        rejected = client.post(
            f"/api/v1/admin/events/{event_id}/revisions/{revision_id}/reject",
            headers=_headers(),
            json={"actor": "moderator", "note": "not a community event"},
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["approval_status"] == "rejected"

        assert _calendar_titles(client, day) == []
        assert client.get(f"/api/v1/events/{event_id}").status_code == 404
        remaining = client.get(
            "/api/v1/admin/events?status=pending", headers=_headers()
        ).json()["items"]
        assert remaining == []


def test_admin_can_edit_submission_before_approving(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/api/v1/events", json=_payload())
        assert created.status_code == 202, created.text
        event_id, revision_id = created.json()["event_id"], created.json()["revision_id"]
        day = datetime.now(UTC).date() + timedelta(days=5)

        # An admin correction of a queued submission keeps it pending.
        edited = client.post(
            f"/api/v1/events/{event_id}/revisions",
            headers=_headers(),
            json=_payload("Rooftop cinema night — corrected time"),
        )
        assert edited.status_code == 202, edited.text
        assert edited.json()["approval_status"] == "pending"
        assert _calendar_titles(client, day) == []

        queue = client.get(
            "/api/v1/admin/events?status=pending", headers=_headers()
        ).json()["items"]
        assert [item["title"] for item in queue] == [
            "Rooftop cinema night — corrected time"
        ]

        # Approving the corrected revision publishes the edited content.
        approved = client.post(
            f"/api/v1/admin/events/{event_id}/revisions/{revision_id}/approve",
            headers=_headers(),
            json={"actor": "moderator"},
        )
        assert approved.status_code == 200, approved.text
        assert _calendar_titles(client, day) == [
            "Rooftop cinema night — corrected time"
        ]


def test_calendar_shell_contains_review_queue(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        shell = client.get("/").text
        javascript = client.get("/static/app.js").text

    for marker in (
        'id="review-queue-button"',
        'id="review-queue-dialog"',
        'id="review-queue-list"',
        'id="review-detail-dialog"',
        'id="review-detail-title"',
        'id="review-note"',
        'id="review-approve-button"',
        'id="review-reject-button"',
        'id="review-edit-button"',
    ):
        assert marker in shell
    for symbol in (
        "openReviewQueue",
        "openReviewDetail",
        "moderateReview",
        "editReviewSubmission",
        "refreshReviewQueue",
        "review-queue-button",
    ):
        assert symbol in javascript
