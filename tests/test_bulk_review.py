from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'bulk-review.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def _payload(title: str) -> dict:
    first_day = datetime.now(UTC).date() + timedelta(days=5)
    return {
        "title": title,
        "description": "A community gathering.",
        "location_name": "Hall",
        "location_address": "1 Main St",
        "event_url": None,
        "is_all_day": False,
        "starts_at": datetime(
            first_day.year, first_day.month, first_day.day, 19, 0, tzinfo=UTC
        ).isoformat(),
        "ends_at": datetime(
            first_day.year, first_day.month, first_day.day, 21, 0, tzinfo=UTC
        ).isoformat(),
        "timezone": "America/New_York",
        "recurrence_rule": None,
        "recurrence_dates": [],
        "group_ids": [],
        "submitter": {"name": "Robin", "channel": "email", "contact": "robin@example.org"},
    }


def _headers() -> dict[str, str]:
    return {"X-Admin-Key": "test-admin-key"}


def _submit(client: TestClient, title: str) -> dict:
    created = client.post("/api/v1/events", json=_payload(title))
    assert created.status_code == 202, created.text
    body = created.json()
    return {"event_id": body["event_id"], "revision_id": body["revision_id"]}


def _calendar_titles(client: TestClient) -> list[str]:
    day = datetime.now(UTC).date() + timedelta(days=5)
    response = client.get(
        f"/api/v1/calendar?start={day.isoformat()}"
        f"&end={(day + timedelta(days=1)).isoformat()}&timezone=UTC"
    )
    assert response.status_code == 200, response.text
    return sorted(item["title"] for item in response.json()["items"])


def _pending_ids(client: TestClient) -> list[str]:
    queue = client.get("/api/v1/admin/events?status=pending", headers=_headers())
    assert queue.status_code == 200, queue.text
    return [item["event_id"] for item in queue.json()["items"]]


def _bulk(client: TestClient, action: str, items: list[dict], **extra):
    return client.post(
        f"/api/v1/admin/events/bulk/{action}",
        headers=_headers(),
        json={"actor": "moderator", "items": items, **extra},
    )


def _revision(client: TestClient, item: dict) -> dict:
    detail = client.get(f"/api/v1/admin/events/{item['event_id']}", headers=_headers())
    assert detail.status_code == 200, detail.text
    return next(r for r in detail.json()["revisions"] if r["id"] == item["revision_id"])


def test_bulk_approve_publishes_every_selected_submission(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        a, b, c = (_submit(client, title) for title in ("Alpha", "Bravo", "Charlie"))
        response = _bulk(client, "approve", [a, b], note="Looks good")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["counts"] == {"approved": 2, "failed": 0}
        assert [r["event_id"] for r in body["results"]] == [a["event_id"], b["event_id"]]
        assert all(r["ok"] and r["status"] == "approved" for r in body["results"])

        assert _calendar_titles(client) == ["Alpha", "Bravo"]
        assert _pending_ids(client) == [c["event_id"]]
        revision = _revision(client, a)
        assert revision["approval_status"] == "approved"
        assert revision["review_note"] == "Looks good"
        assert revision["reviewed_by"] == "moderator"


def test_bulk_reject_records_the_shared_note(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        a, b = _submit(client, "Alpha"), _submit(client, "Bravo")
        response = _bulk(client, "reject", [a, b], note="Duplicate listing")
        assert response.status_code == 200, response.text
        assert response.json()["counts"] == {"rejected": 2, "failed": 0}
        assert _calendar_titles(client) == []
        for item in (a, b):
            revision = _revision(client, item)
            assert revision["approval_status"] == "rejected"
            assert revision["review_note"] == "Duplicate listing"


def test_bulk_approve_skips_already_reviewed_items_and_keeps_the_rest(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        a, b = _submit(client, "Alpha"), _submit(client, "Bravo")
        single = client.post(
            f"/api/v1/admin/events/{a['event_id']}/revisions/{a['revision_id']}/approve",
            headers=_headers(),
            json={"actor": "someone else"},
        )
        assert single.status_code == 200, single.text

        response = _bulk(client, "approve", [a, b])
        assert response.status_code == 200, response.text
        body = response.json()
        first, second = body["results"]
        assert first["ok"] is False
        assert first["status"] == "failed"
        assert first["error"]["code"] == "conflict"
        assert second["ok"] is True and second["status"] == "approved"
        assert body["counts"] == {"approved": 1, "failed": 1}
        assert _calendar_titles(client) == ["Alpha", "Bravo"]


def test_bulk_review_unknown_item_reports_not_found(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        a = _submit(client, "Alpha")
        ghost = {"event_id": str(uuid4()), "revision_id": str(uuid4())}
        response = _bulk(client, "reject", [ghost, a])
        assert response.status_code == 200, response.text
        missing, rejected = response.json()["results"]
        assert missing["event_id"] == ghost["event_id"]
        assert missing["error"]["code"] == "not_found"
        assert rejected["status"] == "rejected"


def test_bulk_review_collapses_duplicates(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        a, b = _submit(client, "Alpha"), _submit(client, "Bravo")
        response = _bulk(client, "approve", [a, a, b])
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["results"]) == 2
        assert body["counts"] == {"approved": 2, "failed": 0}


def test_bulk_review_limits_and_auth(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        a = _submit(client, "Alpha")
        assert _bulk(client, "approve", []).status_code == 422
        many = [{"event_id": str(uuid4()), "revision_id": str(uuid4())} for _ in range(201)]
        assert _bulk(client, "approve", many).status_code == 422
        assert _bulk(client, "reject", [a], note="x" * 4001).status_code == 422
        unauthenticated = client.post(
            "/api/v1/admin/events/bulk/approve",
            json={"actor": "moderator", "items": [a]},
        )
        assert unauthenticated.status_code == 401
        assert _pending_ids(client) == [a["event_id"]]
