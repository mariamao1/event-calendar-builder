from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database

ADMIN = {"X-Admin-Key": "test-admin-key"}


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'bulk-tags.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def _group(client: TestClient, slug: str) -> str:
    response = client.post(
        "/api/v1/admin/groups", headers=ADMIN, json={"slug": slug, "name": slug.title()}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _payload(title: str = "Tag night", group_ids: list[str] | None = None, **overrides) -> dict:
    day = datetime.now(UTC).date() + timedelta(days=6)
    payload = {
        "title": title,
        "description": "Bring water.",
        "location_name": "Library hall",
        "is_all_day": False,
        "starts_at": datetime(day.year, day.month, day.day, 18, tzinfo=UTC).isoformat(),
        "ends_at": datetime(day.year, day.month, day.day, 19, 30, tzinfo=UTC).isoformat(),
        "timezone": "UTC",
        "recurrence_rule": None,
        "recurrence_dates": [],
        "group_ids": group_ids or [],
        "submitter": {"name": "Robin", "channel": "email", "contact": "robin@example.org"},
    }
    payload.update(overrides)
    return payload


def _publish(client: TestClient, title: str = "Tag night", **kwargs) -> str:
    response = client.post("/api/v1/admin/events", headers=ADMIN, json=_payload(title, **kwargs))
    assert response.status_code == 201, response.text
    return response.json()["event_id"]


def _publish_as_creator(client: TestClient, group_ids: list[str] | None = None) -> tuple[str, str]:
    """Submit as a community member and approve; returns (event_id, token)."""
    created = client.post("/api/v1/events", json=_payload("Creator night", group_ids))
    assert created.status_code == 202, created.text
    body = created.json()
    approved = client.post(
        f"/api/v1/admin/events/{body['event_id']}/revisions/{body['revision_id']}/approve",
        headers=ADMIN,
        json={"actor": "moderator"},
    )
    assert approved.status_code == 200, approved.text
    return body["event_id"], body["management_token"]


def _creator_edit(client: TestClient, event_id: str, token: str, **changes) -> str:
    response = client.post(
        f"/api/v1/events/{event_id}/revisions",
        headers={"X-Event-Management-Token": token},
        json=_payload("Creator night (edited)", **changes),
    )
    assert response.status_code in (201, 202), response.text
    return response.json()["revision_id"]


def _tag(client: TestClient, event_ids: list[str], group_ids: list[str], headers=ADMIN):
    return client.post(
        "/api/v1/admin/events/bulk/tags",
        headers=headers,
        json={"actor": "moderator", "event_ids": event_ids, "group_ids": group_ids},
    )


def _occurrences(client: TestClient, event_id: str) -> list[dict]:
    day = datetime.now(UTC).date()
    response = client.get(
        f"/api/v1/calendar?start={day}&end={day + timedelta(days=60)}&timezone=UTC"
    )
    assert response.status_code == 200, response.text
    return [item for item in response.json()["items"] if item["event_id"] == event_id]


def _slugs(groups: list[dict]) -> set[str]:
    return {group["slug"] for group in groups}


def _event(client: TestClient, event_id: str) -> dict:
    response = client.get(f"/api/v1/events/{event_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _admin_event(client: TestClient, event_id: str) -> dict:
    response = client.get(f"/api/v1/admin/events/{event_id}", headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def _approve(client: TestClient, event_id: str, revision_id: str) -> None:
    response = client.post(
        f"/api/v1/admin/events/{event_id}/revisions/{revision_id}/approve",
        headers=ADMIN,
        json={"actor": "moderator"},
    )
    assert response.status_code == 200, response.text


def _sequence(client: TestClient, event_id: str) -> int:
    response = client.get(f"/api/v1/events/{event_id}/ics")
    assert response.status_code == 200, response.text
    return int(re.search(r"SEQUENCE:(\d+)", response.text).group(1))


def test_bulk_tags_add_groups_to_every_selected_event(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        arts, music = _group(client, "arts"), _group(client, "music")
        a = _publish(client, "Alpha", group_ids=[arts])
        b = _publish(client, "Bravo")
        response = _tag(client, [a, b], [music])
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["counts"] == {"tagged": 2, "failed": 0}
        assert [r["event_id"] for r in body["results"]] == [a, b]
        assert _slugs(_occurrences(client, a)[0]["groups"]) == {"arts", "music"}
        assert _slugs(_occurrences(client, b)[0]["groups"]) == {"music"}


def test_event_already_carrying_the_tags_is_unchanged(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        music = _group(client, "music")
        a = _publish(client, group_ids=[music])
        before = len(_admin_event(client, a)["revisions"])
        response = _tag(client, [a], [music])
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["status"] == "unchanged"
        assert response.json()["counts"] == {"unchanged": 1, "failed": 0}
        assert len(_admin_event(client, a)["revisions"]) == before


def test_tagging_keeps_single_date_edits_cancellations_and_skips(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        music = _group(client, "music")
        event_id = _publish(client, "Tuesday choir", recurrence_rule="FREQ=WEEKLY;COUNT=5")
        occurrences = _occurrences(client, event_id)
        assert len(occurrences) == 5
        edited, cancelled, skipped = occurrences[1], occurrences[2], occurrences[3]

        detail = _event(client, event_id)
        edit = client.post(
            f"/api/v1/admin/events/{event_id}/revisions"
            f"?scope=single&occurrence={edited['occurrence_id']}",
            headers=ADMIN,
            json=_payload(
                "Choir (special guest)",
                starts_at=edited["starts_at"],
                ends_at=edited["ends_at"],
                description=detail["description"],
            ),
        )
        assert edit.status_code == 201, edit.text
        cancel = client.post(
            f"/api/v1/events/{event_id}/occurrences/{cancelled['occurrence_id']}/cancel",
            headers=ADMIN,
        )
        assert cancel.status_code == 200, cancel.text
        skip = client.delete(
            f"/api/v1/events/{event_id}/occurrences/{skipped['occurrence_id']}",
            headers=ADMIN,
        )
        assert skip.status_code == 200, skip.text
        before = {item["occurrence_id"]: item for item in _occurrences(client, event_id)}

        response = _tag(client, [event_id], [music])
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["status"] == "tagged"

        after = {item["occurrence_id"]: item for item in _occurrences(client, event_id)}
        assert set(after) == set(before)
        for occurrence_id, item in after.items():
            assert _slugs(item["groups"]) == {"music"}
            assert item["version"] == before[occurrence_id]["version"]
            assert item["title"] == before[occurrence_id]["title"]
        assert after[edited["occurrence_id"]]["title"] == "Choir (special guest)"
        assert after[cancelled["occurrence_id"]]["is_cancelled"] is True
        assert skipped["occurrence_id"] not in after
        skipped_ids = [item["occurrence_id"] for item in _event(client, event_id)["series"]["skipped"]]
        assert skipped["occurrence_id"] in skipped_ids


def test_tags_survive_approving_a_pending_edit(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        music = _group(client, "music")
        event_id, token = _publish_as_creator(client)
        pending = _creator_edit(client, event_id, token)

        response = _tag(client, [event_id], [music])
        assert response.json()["results"][0]["status"] == "tagged"
        assert _slugs(_event(client, event_id)["groups"]) == {"music"}
        queue = client.get("/api/v1/admin/events?status=pending", headers=ADMIN).json()
        assert [item["event_id"] for item in queue["items"]] == [event_id]
        assert _slugs(queue["items"][0]["groups"]) == {"music"}

        _approve(client, event_id, pending)
        published = _event(client, event_id)
        assert published["title"] == "Creator night (edited)"
        assert _slugs(published["groups"]) == {"music"}


def test_tag_restores_group_a_pending_edit_dropped(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        music = _group(client, "music")
        event_id, token = _publish_as_creator(client, [music])
        pending = _creator_edit(client, event_id, token, group_ids=[])
        before = len(_admin_event(client, event_id)["revisions"])

        response = _tag(client, [event_id], [music])
        assert response.json()["results"][0]["status"] == "tagged"
        assert len(_admin_event(client, event_id)["revisions"]) == before

        _approve(client, event_id, pending)
        assert _slugs(_event(client, event_id)["groups"]) == {"music"}


def test_ics_sequence_never_decreases(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        music = _group(client, "music")
        event_id, token = _publish_as_creator(client)
        pending = _creator_edit(client, event_id, token)
        before_tag = _sequence(client, event_id)
        assert _tag(client, [event_id], [music]).json()["results"][0]["status"] == "tagged"
        after_tag = _sequence(client, event_id)
        assert after_tag > before_tag
        _approve(client, event_id, pending)
        assert _sequence(client, event_id) > after_tag


def test_tagging_writes_an_audit_action_and_keeps_featured(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        music = _group(client, "music")
        a = _publish(client)
        assert client.put(f"/api/v1/admin/events/{a}/featured", headers=ADMIN).status_code == 200
        _tag(client, [a], [music])
        assert _event(client, a)["is_featured"] is True
        audit = _admin_event(client, a)
        newest = max(audit["revisions"], key=lambda r: r["revision_number"])
        assert newest["id"] == audit["published_revision_id"]
        assert newest["reviewed_by"] == "moderator"
        assert newest["review_note"] == "Tags added"


def test_cancelled_event_can_be_tagged(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        music = _group(client, "music")
        a = _publish(client)
        assert client.post(f"/api/v1/events/{a}/cancel", headers=ADMIN).status_code == 200
        response = _tag(client, [a], [music])
        assert response.json()["results"][0]["status"] == "tagged"
        event = _event(client, a)
        assert event["is_cancelled"] is True
        assert _slugs(event["groups"]) == {"music"}


def test_editing_after_a_rejected_edit_loads_tagged_content(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        music = _group(client, "music")
        event_id, token = _publish_as_creator(client)
        pending = _creator_edit(client, event_id, token)
        rejected = client.post(
            f"/api/v1/admin/events/{event_id}/revisions/{pending}/reject",
            headers=ADMIN,
            json={"actor": "moderator"},
        )
        assert rejected.status_code == 200, rejected.text
        _tag(client, [event_id], [music])
        manage = client.get(
            f"/api/v1/events/{event_id}/manage",
            headers={"X-Event-Management-Token": token},
        )
        assert manage.status_code == 200, manage.text
        assert manage.json()["title"] == "Creator night"
        assert _slugs(manage.json()["groups"]) == {"music"}


def test_inactive_group_rejects_the_whole_batch(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        arts, music = _group(client, "arts"), _group(client, "music")
        a, b = _publish(client, "Alpha"), _publish(client, "Bravo")
        deactivated = client.patch(
            f"/api/v1/admin/groups/{music}", headers=ADMIN, json={"is_active": False}
        )
        assert deactivated.status_code == 200, deactivated.text
        response = _tag(client, [a, b], [arts, music])
        assert response.status_code == 422, response.text
        assert _event(client, a)["groups"] == []
        assert _event(client, b)["groups"] == []


def test_missing_or_deleted_event_fails_alone(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        music = _group(client, "music")
        a, b = _publish(client, "Alpha"), _publish(client, "Bravo")
        assert client.delete(f"/api/v1/events/{a}", headers=ADMIN).status_code in (200, 204)
        ghost = str(uuid4())
        response = _tag(client, [a, ghost, b], [music])
        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert [r["error"]["code"] for r in results[:2]] == ["not_found", "not_found"]
        assert results[2]["status"] == "tagged"
        assert response.json()["counts"] == {"tagged": 1, "failed": 2}


def test_bulk_tags_limits_duplicates_and_auth(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        music = _group(client, "music")
        a = _publish(client)
        doubled = _tag(client, [a, a], [music])
        assert doubled.status_code == 200, doubled.text
        assert len(doubled.json()["results"]) == 1
        assert _tag(client, [], [music]).status_code == 422
        assert _tag(client, [str(uuid4()) for _ in range(501)], [music]).status_code == 422
        assert _tag(client, [a], []).status_code == 422
        assert _tag(client, [a], [music], headers={}).status_code == 401


def test_tag_revision_keeps_the_original_submission_time(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        music = _group(client, "music")
        a = _publish(client)
        original = _admin_event(client, a)["revisions"][0]["submitted_at"]
        _tag(client, [a], [music])
        audit = _admin_event(client, a)
        tagged = next(r for r in audit["revisions"] if r["id"] == audit["published_revision_id"])
        assert tagged["review_note"] == "Tags added"
        assert tagged["submitted_at"] == original
