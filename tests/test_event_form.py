from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'event-form.db'}"
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


def test_creator_token_controls_edit_access(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        group = _group(client)
        created = client.post("/api/v1/events", json=_payload(group["id"]))
        assert created.status_code == 202, created.text
        identity = created.json()
        assert identity["management_token"]

        admin_headers = {"X-Admin-Key": "test-admin-key"}
        manage_url = f"/api/v1/events/{identity['event_id']}/manage"
        assert client.get(manage_url).status_code == 401
        assert client.get(
            manage_url, headers={"X-Event-Management-Token": "wrong"}
        ).status_code == 401
        creator_headers = {
            "X-Event-Management-Token": identity["management_token"]
        }
        detail = client.get(manage_url, headers=creator_headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["title"] == "Block party"

        pending_edit = client.post(
            f"/api/v1/events/{identity['event_id']}/revisions",
            headers=creator_headers,
            json=_payload(group["id"], "Block party — corrected"),
        )
        assert pending_edit.status_code == 202, pending_edit.text
        assert pending_edit.json()["revision_id"] == identity["revision_id"]
        assert pending_edit.json()["updated_pending_revision"] is True

        # Review the corrected pending content, then verify the creator can
        # submit another edit as a new pending revision.
        approved = client.post(
            f"/api/v1/admin/events/{identity['event_id']}"
            f"/revisions/{identity['revision_id']}/approve",
            headers=admin_headers,
            json={"actor": "moderator"},
        )
        assert approved.status_code == 200, approved.text

        edited = client.post(
            f"/api/v1/events/{identity['event_id']}/revisions",
            headers=creator_headers,
            json=_payload(group["id"], "Block party — new details"),
        )
        assert edited.status_code == 202, edited.text
        assert edited.json()["approval_status"] == "pending"


def test_admin_form_route_creates_and_approves(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        group = _group(client)
        created = client.post(
            "/api/v1/admin/events",
            headers={"X-Admin-Key": "test-admin-key"},
            json=_payload(group["id"]),
        )
        assert created.status_code == 201, created.text
        assert created.json()["approval_status"] == "approved"
        assert created.json()["occurrence_count"] == 1

        detail = client.get(f"/api/v1/events/{created.json()['event_id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["title"] == "Block party"

        edited = client.post(
            f"/api/v1/admin/events/{created.json()['event_id']}/revisions",
            headers={"X-Admin-Key": "test-admin-key"},
            json=_payload(group["id"], "Block party — updated by admin"),
        )
        assert edited.status_code == 201, edited.text
        assert edited.json()["approval_status"] == "approved"
        public = client.get(f"/api/v1/events/{created.json()['event_id']}")
        assert public.json()["title"] == "Block party — updated by admin"


def test_calendar_shell_contains_complete_event_form(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        shell = client.get("/").text
        javascript = client.get("/static/app.js").text

    for marker in (
        'id="create-event-button"',
        'id="event-form-dialog"',
        'id="event-title"',
        'id="event-description"',
        'id="event-starts-at"',
        'id="event-start-date"',
        'id="event-recurrence-rule"',
        'id="event-groups"',
        'id="submitter-contact"',
        'id="admin-login-form"',
    ):
        assert marker in shell
    for symbol in (
        "buildEventPayload",
        "openEditEventForm",
        "zonedLocalToIso",
        "X-Event-Management-Token",
        "managementLink",
    ):
        assert symbol in javascript

    form = shell.split('<form id="event-form"', 1)[1].split("</form>", 1)[0]
    assert form.count(" required") == 2
    assert '<select id="event-timezone"' in form
    assert '<select id="event-recurrence-rule"' in form
    assert '<span class="field-heading">Title <span aria-hidden="true">*</span>' in form
    assert '<span class="field-heading">Name <span aria-hidden="true">*</span>' in form


def test_optional_contact_and_groups_still_publish_unfiltered(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        group = _group(client)
        payload = _payload(group["id"], "Ungrouped event")
        payload["group_ids"] = []
        payload["submitter"].pop("contact")
        created = client.post(
            "/api/v1/admin/events",
            headers={"X-Admin-Key": "test-admin-key"},
            json=payload,
        )
        assert created.status_code == 201, created.text

        day = datetime.now(UTC).date() + timedelta(days=5)
        calendar = client.get(
            f"/api/v1/calendar?start={day.isoformat()}"
            f"&end={(day + timedelta(days=1)).isoformat()}&timezone=UTC"
        )
        assert calendar.status_code == 200, calendar.text
        assert [item["title"] for item in calendar.json()["items"]] == [
            "Ungrouped event"
        ]
        assert calendar.json()["items"][0]["groups"] == []
