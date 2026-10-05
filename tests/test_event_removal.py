from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database

ADMIN = {"X-Admin-Key": "test-admin-key"}


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'event-removal.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def _payload(**overrides) -> dict:
    day = datetime.now(UTC).date() + timedelta(days=4)
    payload = {
        "title": "Harbor cleanup",
        "description": "Gloves provided.",
        "location_name": "Pier 9",
        "is_all_day": False,
        "starts_at": datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC).isoformat(),
        "ends_at": datetime(day.year, day.month, day.day, 11, 0, tzinfo=UTC).isoformat(),
        "timezone": "UTC",
        "recurrence_rule": None,
        "recurrence_dates": [],
        "group_ids": [],
        "submitter": {"name": "Robin", "channel": "email", "contact": "robin@example.org"},
    }
    payload.update(overrides)
    return payload


def _publish(client: TestClient, payload: dict) -> dict:
    response = client.post("/api/v1/admin/events", headers=ADMIN, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _calendar_titles(client: TestClient) -> list[str]:
    day = datetime.now(UTC).date() + timedelta(days=4)
    response = client.get(
        f"/api/v1/calendar?start={day.isoformat()}"
        f"&end={(day + timedelta(days=1)).isoformat()}&timezone=UTC"
    )
    assert response.status_code == 200, response.text
    return [item["title"] for item in response.json()["items"]]


def test_cancel_keeps_event_visible_and_marked(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = _publish(client, _payload())
        event_id = created["event_id"]

        cancelled = client.post(
            f"/api/v1/events/{event_id}/cancel",
            headers=ADMIN,
            json={"actor": "admin", "note": "Storm coming"},
        )
        assert cancelled.status_code == 200, cancelled.text
        body = cancelled.json()
        assert body["is_cancelled"] is True
        assert body["event_id"] == event_id

        # A cancelled event stays on the calendar, marked as cancelled.
        calendar = client.get(
            f"/api/v1/calendar?start={(datetime.now(UTC).date() + timedelta(days=4)).isoformat()}"
            f"&end={(datetime.now(UTC).date() + timedelta(days=5)).isoformat()}&timezone=UTC"
        )
        assert calendar.status_code == 200, calendar.text
        items = [item for item in calendar.json()["items"] if item["event_id"] == event_id]
        assert len(items) == 1
        assert items[0]["is_cancelled"] is True

        # The detail view stays reachable and says the event is cancelled.
        detail = client.get(f"/api/v1/events/{event_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["is_cancelled"] is True

        # Cancelling twice is a conflict, not a silent no-op.
        again = client.post(f"/api/v1/events/{event_id}/cancel", headers=ADMIN)
        assert again.status_code == 409, again.text


def test_delete_removes_event_entirely(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = _publish(client, _payload())
        event_id = created["event_id"]
        assert _calendar_titles(client) == ["Harbor cleanup"]

        deleted = client.delete(f"/api/v1/events/{event_id}", headers=ADMIN)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted"] is True

        # A deleted event should no longer exist anywhere public.
        assert client.get(f"/api/v1/events/{event_id}").status_code == 404
        assert _calendar_titles(client) == []
        assert client.get(f"/api/v1/admin/events/{event_id}", headers=ADMIN).status_code == 404
        queue = client.get("/api/v1/admin/events?status=approved", headers=ADMIN)
        assert queue.status_code == 200, queue.text
        assert [item["event_id"] for item in queue.json()["items"]] == []

        # Creator management reads and edits are gone too, for any credential.
        assert client.get(f"/api/v1/events/{event_id}/manage", headers=ADMIN).status_code == 404
        assert client.post(f"/api/v1/events/{event_id}/cancel", headers=ADMIN).status_code == 404

        # Deleting twice reports the event as gone.
        assert client.delete(f"/api/v1/events/{event_id}", headers=ADMIN).status_code == 404


def test_only_admin_or_creator_may_cancel_or_delete(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        submitted = client.post("/api/v1/events", json=_payload(title="Creator owned"))
        assert submitted.status_code == 202, submitted.text
        identity = submitted.json()
        event_id, token = identity["event_id"], identity["management_token"]
        creator = {"X-Event-Management-Token": token}

        # Strangers cannot cancel or delete.
        assert client.post(f"/api/v1/events/{event_id}/cancel").status_code == 401
        assert client.delete(f"/api/v1/events/{event_id}").status_code == 401
        wrong = {"X-Event-Management-Token": "wrong-token"}
        assert client.post(f"/api/v1/events/{event_id}/cancel", headers=wrong).status_code == 401
        assert client.delete(f"/api/v1/events/{event_id}", headers=wrong).status_code == 401

        # The creator can cancel their own event.
        cancelled = client.post(f"/api/v1/events/{event_id}/cancel", headers=creator)
        assert cancelled.status_code == 200, cancelled.text

        # And the creator can delete it afterwards.
        deleted = client.delete(f"/api/v1/events/{event_id}", headers=creator)
        assert deleted.status_code == 200, deleted.text
        assert client.get(f"/api/v1/events/{event_id}").status_code == 404


def test_creator_can_delete_pending_submission(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        submitted = client.post("/api/v1/events", json=_payload(title="Second thoughts"))
        assert submitted.status_code == 202, submitted.text
        identity = submitted.json()
        creator = {"X-Event-Management-Token": identity["management_token"]}

        deleted = client.delete(f"/api/v1/events/{identity['event_id']}", headers=creator)
        assert deleted.status_code == 200, deleted.text

        queue = client.get("/api/v1/admin/events?status=pending", headers=ADMIN)
        assert queue.status_code == 200, queue.text
        assert queue.json()["items"] == []


def test_admin_aliases_cancel_and_delete(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        first = _publish(client, _payload(title="First"))
        cancelled = client.post(
            f"/api/v1/admin/events/{first['event_id']}/cancel",
            headers=ADMIN,
            json={"actor": "admin"},
        )
        assert cancelled.status_code == 200, cancelled.text
        assert client.get(f"/api/v1/events/{first['event_id']}").json()["is_cancelled"] is True

        second = _publish(client, _payload(title="Second"))
        deleted = client.delete(f"/api/v1/admin/events/{second['event_id']}", headers=ADMIN)
        assert deleted.status_code == 200, deleted.text
        assert client.get(f"/api/v1/events/{second['event_id']}").status_code == 404

        # Admin routes still require admin credentials.
        third = _publish(client, _payload(title="Third"))
        assert client.post(f"/api/v1/admin/events/{third['event_id']}/cancel").status_code in (401, 503)
        assert client.delete(f"/api/v1/admin/events/{third['event_id']}").status_code in (401, 503)


def _write_stale_schema(db_path: Path) -> None:
    """Create a database with the pre-removal schema (old audit CHECK)."""
    import sqlite3

    schema = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "calendar_api"
        / "sqlite_schema.sql"
    ).read_text()
    stale = schema.replace(
        ",\n  cancelled_at TEXT,\n  cancelled_by TEXT,\n  cancel_reason TEXT", ""
    ).replace(
        "('approve', 'reject', 'revoke', 'cancel', 'delete')",
        "('approve', 'reject', 'revoke')",
    )
    assert stale != schema
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(stale)
        connection.commit()
    finally:
        connection.close()


def _write_legacy_approved_event(db_path: Path) -> None:
    """Insert one approved event directly, as an older release would."""
    import sqlite3

    now = "2026-01-01T00:00:00+00:00"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            f"""
            INSERT INTO events (
              id, original_submitter_name, original_submitter_channel,
              original_submitter_contact, management_token_hash,
              submitted_at, updated_at
            ) VALUES (
              '11111111-1111-1111-1111-111111111111', 'Robin', 'email',
              'robin@example.org', zeroblob(32), '{now}', '{now}'
            );
            INSERT INTO event_revisions (
              id, event_id, revision_number, approval_status, title,
              description, is_all_day, starts_at, ends_at, timezone,
              submitted_by_name, submitted_by_channel, submitted_by_contact,
              submitted_at, reviewed_at, reviewed_by
            ) VALUES (
              '22222222-2222-2222-2222-222222222222',
              '11111111-1111-1111-1111-111111111111', 1, 'approved',
              'Legacy potluck', '', 0,
              '2026-02-01T18:00:00+00:00', '2026-02-01T20:00:00+00:00', 'UTC',
              'Robin', 'email', 'robin@example.org', '{now}', '{now}', 'mod'
            );
            UPDATE events
               SET current_revision_id = '22222222-2222-2222-2222-222222222222',
                   published_revision_id = '22222222-2222-2222-2222-222222222222'
             WHERE id = '11111111-1111-1111-1111-111111111111';
            INSERT INTO event_review_actions (
              id, event_id, event_revision_id, action, actor, occurred_at
            ) VALUES (
              '33333333-3333-3333-3333-333333333333',
              '11111111-1111-1111-1111-111111111111',
              '22222222-2222-2222-2222-222222222222', 'approve', 'mod', '{now}'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_stale_database_upgrades_for_cancel_and_delete(tmp_path: Path) -> None:
    db_path = tmp_path / "stale.db"
    _write_stale_schema(db_path)
    _write_legacy_approved_event(db_path)
    url = f"sqlite:///{db_path}"
    with TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    ) as client:
        first = _publish(client, _payload(title="Stale cancel"))
        cancelled = client.post(
            f"/api/v1/events/{first['event_id']}/cancel", headers=ADMIN
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["is_cancelled"] is True

        second = _publish(client, _payload(title="Stale delete"))
        deleted = client.delete(f"/api/v1/events/{second['event_id']}", headers=ADMIN)
        assert deleted.status_code == 200, deleted.text
        assert client.get(f"/api/v1/events/{second['event_id']}").status_code == 404

    # The pre-existing audit row survives the CHECK-constraint rebuild.
    import sqlite3

    connection = sqlite3.connect(db_path)
    try:
        actions = sorted(
            row[0]
            for row in connection.execute("SELECT action FROM event_review_actions")
        )
    finally:
        connection.close()
    assert actions == ["approve", "approve", "approve", "cancel", "delete"]


def test_shell_contains_removal_actions(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        html = client.get("/").text
        javascript = client.get("/static/app.js").text
        stylesheet = client.get("/static/styles.css").text

    for marker in ("id=\"cancel-event-button\"", "id=\"delete-event-button\""):
        assert marker in html
    for marker in (
        "cancel-event-button",
        "delete-event-button",
        "cancelEventFromDetail",
        "deleteEventFromDetail",
        "This event is cancelled",
        "/cancel",
    ):
        assert marker in javascript
    assert "is-cancelled" in stylesheet
