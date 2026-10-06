from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database

ADMIN = {"X-Admin-Key": "test-admin-key"}


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'occurrence-scope.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def _payload(**overrides) -> dict:
    day = datetime.now(UTC).date() + timedelta(days=6)
    payload = {
        "title": "Tuesday choir",
        "description": "Bring water.",
        "location_name": "Library hall",
        "is_all_day": False,
        "starts_at": datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC).isoformat(),
        "ends_at": datetime(day.year, day.month, day.day, 19, 30, tzinfo=UTC).isoformat(),
        "timezone": "UTC",
        "recurrence_rule": "FREQ=WEEKLY;COUNT=5",
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


def _occurrences(client: TestClient, event_id: str) -> list[dict]:
    day = datetime.now(UTC).date()
    response = client.get(
        f"/api/v1/calendar?start={day}&end={day + timedelta(days=60)}&timezone=UTC"
    )
    assert response.status_code == 200, response.text
    return [item for item in response.json()["items"] if item["event_id"] == event_id]


def _scoped_edit_payload(client: TestClient, event_id: str, occurrence: dict, **changes) -> dict:
    """Build a scoped-edit payload the way the UI does: the occurrence's own
    content and timing, with no repeat pattern of its own."""
    detail = client.get(
        f"/api/v1/events/{event_id}?occurrence={occurrence['occurrence_id']}"
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    selected = body["occurrence"]
    payload = _payload(
        title=selected.get("title", body["title"]),
        description=selected.get("description", body["description"]),
        recurrence_rule=None,
        recurrence_dates=[],
    )
    payload.update(
        {
            "is_all_day": False,
            "starts_at": selected["starts_at"],
            "ends_at": selected["ends_at"],
            "start_date": None,
            "end_date": None,
        }
    )
    payload.update(changes)
    return payload


def test_single_edit_changes_only_one_occurrence(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        assert len(occurrences) == 5
        target = occurrences[1]

        edited = client.post(
            f"/api/v1/admin/events/{event_id}/revisions"
            f"?scope=single&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(
                client,
                event_id,
                target,
                title="Choir (special guest)",
                description="Guest conductor night.",
                location_name="Chapel",
            ),
        )
        assert edited.status_code == 201, edited.text
        body = edited.json()
        assert body["scope"] == "single"
        assert body["occurrence_id"] == target["occurrence_id"]
        assert body["has_override"] is True

        items = _occurrences(client, event_id)
        assert [item["title"] for item in items] == (
            ["Tuesday choir", "Choir (special guest)"] + ["Tuesday choir"] * 3
        )
        assert items[1]["has_override"] is True
        assert items[1]["location_name"] == "Chapel"
        assert all(item.get("has_override") is False for item in items[:1] + items[2:])

        # The detail view shows the override for that date only, with every
        # content field the popup renders merged over the series.
        detail = client.get(
            f"/api/v1/events/{event_id}?occurrence={target['occurrence_id']}"
        ).json()
        assert detail["occurrence"]["title"] == "Choir (special guest)"
        assert detail["occurrence"]["description"] == "Guest conductor night."
        assert detail["occurrence"]["location_name"] == "Chapel"
        assert detail["occurrence"]["location_address"] == detail["location_address"]
        assert detail["occurrence"]["has_override"] is True
        other = client.get(
            f"/api/v1/events/{event_id}?occurrence={occurrences[0]['occurrence_id']}"
        ).json()
        assert other["occurrence"]["title"] == "Tuesday choir"
        assert other["occurrence"]["description"] == detail["description"]
        assert other["occurrence"].get("has_override") is False


def test_single_edit_can_reschedule_one_occurrence(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        target = occurrences[1]
        moved_start = datetime.fromisoformat(target["starts_at"]) + timedelta(hours=2)
        moved_end = datetime.fromisoformat(target["ends_at"]) + timedelta(hours=2)

        edited = client.post(
            f"/api/v1/admin/events/{event_id}/revisions",
            headers=ADMIN,
            json=_scoped_edit_payload(
                client,
                event_id,
                target,
                starts_at=moved_start.isoformat(),
                ends_at=moved_end.isoformat(),
                occurrence_id=target["occurrence_id"],
                scope="single",
            ),
        )
        assert edited.status_code == 201, edited.text

        items = _occurrences(client, event_id)
        assert len(items) == 5
        assert items[1]["starts_at"] == moved_start.isoformat()
        assert items[1]["is_exception"] is True
        assert items[0]["starts_at"] == occurrences[0]["starts_at"]
        assert items[2]["starts_at"] == occurrences[2]["starts_at"]


def test_series_edit_replaces_single_exception(tmp_path: Path) -> None:
    """The latest change to the series always persists over per-date edits."""
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        target = occurrences[1]
        single = client.post(
            f"/api/v1/admin/events/{event_id}/revisions?scope=single&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(client, event_id, target, title="One-off title"),
        )
        assert single.status_code == 201, single.text
        flagged = client.post(
            f"/api/v1/events/{event_id}/cancel?scope=single&occurrence={occurrences[0]['occurrence_id']}",
            headers=ADMIN,
        )
        assert flagged.status_code == 200, flagged.text

        series = client.post(
            f"/api/v1/admin/events/{event_id}/revisions",
            headers=ADMIN,
            json=_payload(title="Choir (new room)"),
        )
        assert series.status_code == 201, series.text

        items = _occurrences(client, event_id)
        assert [item["title"] for item in items] == ["Choir (new room)"] * 5
        assert all(item.get("has_override") is False for item in items)
        assert all(item["is_cancelled"] is False for item in items)


def test_future_edit_changes_target_and_later_only(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        target = occurrences[2]

        edited = client.post(
            f"/api/v1/admin/events/{event_id}/revisions?scope=future&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(client, event_id, target, title="Choir (late series)"),
        )
        assert edited.status_code == 201, edited.text
        assert edited.json()["scope"] == "future"

        items = _occurrences(client, event_id)
        assert [item["title"] for item in items] == (
            ["Tuesday choir"] * 2 + ["Choir (late series)"] * 3
        )


def test_future_edit_can_shift_future_times(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        target = occurrences[2]
        shifted_start = datetime.fromisoformat(target["starts_at"]) + timedelta(hours=1)
        shifted_end = datetime.fromisoformat(target["ends_at"]) + timedelta(hours=1)

        edited = client.post(
            f"/api/v1/admin/events/{event_id}/revisions?scope=future&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(
                client,
                event_id,
                target,
                starts_at=shifted_start.isoformat(),
                ends_at=shifted_end.isoformat(),
            ),
        )
        assert edited.status_code == 201, edited.text

        items = _occurrences(client, event_id)
        assert len(items) == 5
        assert [item["starts_at"] for item in items[:2]] == [
            item["starts_at"] for item in occurrences[:2]
        ]
        assert {item["starts_at"] for item in items[2:]} == {
            shifted_start.isoformat(),
            (shifted_start + timedelta(days=7)).isoformat(),
            (shifted_start + timedelta(days=14)).isoformat(),
        }


def test_single_delete_removes_one_date(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        victim = occurrences[3]

        deleted = client.delete(
            f"/api/v1/events/{event_id}?scope=single&occurrence={victim['occurrence_id']}",
            headers=ADMIN,
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["scope"] == "single"

        items = _occurrences(client, event_id)
        assert len(items) == 4
        assert victim["occurrence_id"] not in {item["occurrence_id"] for item in items}
        # The event itself still exists with its remaining dates.
        assert client.get(f"/api/v1/events/{event_id}").status_code == 200


def test_future_delete_removes_target_and_later(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        target = occurrences[1]

        deleted = client.delete(
            f"/api/v1/events/{event_id}?scope=future&occurrence={target['occurrence_id']}",
            headers=ADMIN,
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["scope"] == "future"
        assert deleted.json()["deleted_occurrence_count"] == 4

        items = _occurrences(client, event_id)
        assert [item["occurrence_id"] for item in items] == [occurrences[0]["occurrence_id"]]
        assert client.get(f"/api/v1/events/{event_id}").status_code == 200


def test_single_cancel_keeps_date_visible_and_flagged(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        target = occurrences[0]

        cancelled = client.post(
            f"/api/v1/events/{event_id}/cancel?scope=single&occurrence={target['occurrence_id']}",
            headers=ADMIN,
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["is_cancelled"] is True

        items = _occurrences(client, event_id)
        assert len(items) == 5
        flagged = [item for item in items if item["occurrence_id"] == target["occurrence_id"]]
        assert len(flagged) == 1 and flagged[0]["is_cancelled"] is True
        assert all(
            item["is_cancelled"] is False
            for item in items
            if item["occurrence_id"] != target["occurrence_id"]
        )

        detail = client.get(
            f"/api/v1/events/{event_id}?occurrence={target['occurrence_id']}"
        ).json()
        assert detail["occurrence"]["is_cancelled"] is True

        # Cancelling the same date twice is a conflict.
        again = client.post(
            f"/api/v1/events/{event_id}/cancel?scope=single&occurrence={target['occurrence_id']}",
            headers=ADMIN,
        )
        assert again.status_code == 409, again.text


def test_future_cancel_flags_later_dates(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        target = occurrences[2]

        cancelled = client.post(
            f"/api/v1/events/{event_id}/cancel?scope=future&occurrence={target['occurrence_id']}",
            headers=ADMIN,
        )
        assert cancelled.status_code == 200, cancelled.text

        items = _occurrences(client, event_id)
        assert len(items) == 5
        assert [item["is_cancelled"] for item in items] == [False, False, True, True, True]


def test_scope_aliases_and_occurrence_routes(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        target = _occurrences(client, event_id)[0]

        # Aliased scope names work on the revision routes.
        edited = client.post(
            f"/api/v1/admin/events/{event_id}/revisions?scope=this&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(client, event_id, target, title="Aliased"),
        )
        assert edited.status_code == 201, edited.text
        assert _occurrences(client, event_id)[0]["title"] == "Aliased"

        # The dedicated occurrence routes default to a single date.
        removed = client.delete(
            f"/api/v1/events/{event_id}/occurrences/{target['occurrence_id']}",
            headers=ADMIN,
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["scope"] == "single"
        assert len(_occurrences(client, event_id)) == 4

        # ... and accept an explicit future scope for cancellation.
        remaining = _occurrences(client, event_id)
        cancelled = client.post(
            f"/api/v1/events/{event_id}/occurrences/{remaining[1]['occurrence_id']}/cancel?scope=this_and_future",
            headers=ADMIN,
        )
        assert cancelled.status_code == 200, cancelled.text
        items = _occurrences(client, event_id)
        assert [item["is_cancelled"] for item in items] == [False, True, True, True]


def test_scope_validation(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        target = _occurrences(client, event_id)[0]

        # Unknown scopes are rejected.
        bad_scope = client.post(
            f"/api/v1/admin/events/{event_id}/revisions?scope=someday&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(client, event_id, target),
        )
        assert bad_scope.status_code == 422, bad_scope.text

        # Single/future scopes require the targeted occurrence.
        missing = client.post(
            f"/api/v1/admin/events/{event_id}/revisions?scope=single",
            headers=ADMIN,
            json=_scoped_edit_payload(client, event_id, target),
        )
        assert missing.status_code == 422, missing.text

        # Unknown occurrences read as missing.
        gone = client.post(
            f"/api/v1/admin/events/{event_id}/revisions?scope=single&occurrence=00000000-0000-0000-0000-000000000000",
            headers=ADMIN,
            json=_scoped_edit_payload(client, event_id, target),
        )
        assert gone.status_code == 404, gone.text

        # Scoping a one-off event is rejected.
        one_off = _publish(client, _payload(recurrence_rule=None))
        [only] = _occurrences(client, one_off)
        scoped = client.post(
            f"/api/v1/admin/events/{one_off}/revisions?scope=single&occurrence={only['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(client, one_off, only),
        )
        assert scoped.status_code == 422, scoped.text

        # Single edits cannot change groups.
        groups = client.post(
            f"/api/v1/admin/events/{event_id}/revisions?scope=single&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(
                client, event_id, target, group_ids=["00000000-0000-0000-0000-000000000000"]
            ),
        )
        assert groups.status_code == 422, groups.text


def test_scoped_edit_ignores_echoed_repeat_pattern(tmp_path: Path) -> None:
    """Regression: echoing the series schedule back on a scoped edit must not
    read as a pattern change. The form always resubmits the schedule it was
    given, so rejecting anything but a byte-identical rule made ordinary
    single/future edits fail."""
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        target = occurrences[1]

        # A pattern-looking rule riding along is ignored, not rejected.
        echoed = client.post(
            f"/api/v1/admin/events/{event_id}/revisions?scope=single&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(
                client,
                event_id,
                target,
                title="Echoed schedule",
                recurrence_rule="FREQ=DAILY;COUNT=3",
            ),
        )
        assert echoed.status_code == 201, echoed.text
        items = _occurrences(client, event_id)
        assert len(items) == 5
        assert items[1]["title"] == "Echoed schedule"

        # Same for the future scope: the published pattern is reused.
        later = _occurrences(client, event_id)[2]
        future = client.post(
            f"/api/v1/admin/events/{event_id}/revisions?scope=future&occurrence={later['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(
                client,
                event_id,
                later,
                title="Future echo",
                recurrence_rule="FREQ=DAILY;COUNT=9",
                recurrence_dates=[
                    {"local_start": "2030-01-01T18:00:00", "kind": "include"}
                ],
            ),
        )
        assert future.status_code == 201, future.text
        items = _occurrences(client, event_id)
        assert len(items) == 5
        assert [item["title"] for item in items][2:] == ["Future echo"] * 3


def test_single_edit_can_move_date_off_pattern(tmp_path: Path) -> None:
    """Regression: moving one instance to a day outside the series pattern
    must work. With the series rule attached the model rejects the
    off-pattern start, so scoped submits carry no recurrence of their own."""
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        target = _occurrences(client, event_id)[1]
        moved_start = datetime.fromisoformat(target["starts_at"]) + timedelta(days=3)
        moved_end = datetime.fromisoformat(target["ends_at"]) + timedelta(days=3)
        # Off the series' own weekday: the move leaves the weekly pattern.
        assert moved_start.weekday() != datetime.fromisoformat(target["starts_at"]).weekday()

        edited = client.post(
            f"/api/v1/admin/events/{event_id}/revisions?scope=single&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(
                client,
                event_id,
                target,
                starts_at=moved_start.isoformat(),
                ends_at=moved_end.isoformat(),
            ),
        )
        assert edited.status_code == 201, edited.text
        items = _occurrences(client, event_id)
        assert len(items) == 5
        assert items[1]["starts_at"] == moved_start.isoformat()
        # The rest of the series is untouched.
        assert items[2]["starts_at"] != moved_start.isoformat()


def test_future_scoped_writes_conflict_with_pending_edit(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        submitted = client.post("/api/v1/events", json=_payload(title="Pending series"))
        assert submitted.status_code == 202, submitted.text
        identity = submitted.json()
        # A pending community submission has no published dates yet.
        assert identity["event_id"]

        approved = client.post(
            f"/api/v1/admin/events/{identity['event_id']}"
            f"/revisions/{identity['revision_id']}/approve",
            headers=ADMIN,
            json={"actor": "mod"},
        )
        assert approved.status_code == 200, approved.text
        occurrences = _occurrences(client, identity["event_id"])
        assert len(occurrences) == 5

        creator = {"X-Event-Management-Token": identity["management_token"]}
        pending = client.post(
            f"/api/v1/events/{identity['event_id']}/revisions",
            headers=creator,
            json=_payload(title="Pending new title"),
        )
        assert pending.status_code == 202, pending.text

        # Future-scoped writes mint revisions, so they wait for the queue.
        target = occurrences[2]
        conflict = client.post(
            f"/api/v1/admin/events/{identity['event_id']}/revisions"
            f"?scope=future&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(client, identity["event_id"], target),
        )
        assert conflict.status_code == 409, conflict.text

        # A single-date edit still applies on top of the published series.
        single = client.post(
            f"/api/v1/admin/events/{identity['event_id']}/revisions"
            f"?scope=single&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(
                client, identity["event_id"], target, title="Single while pending"
            ),
        )
        assert single.status_code == 201, single.text


def test_creator_can_edit_a_single_date(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        submitted = client.post("/api/v1/events", json=_payload(title="Creator series"))
        assert submitted.status_code == 202, submitted.text
        identity = submitted.json()
        event_id = identity["event_id"]
        client.post(
            f"/api/v1/admin/events/{event_id}/revisions/{identity['revision_id']}/approve",
            headers=ADMIN,
            json={"actor": "mod"},
        )
        creator = {"X-Event-Management-Token": identity["management_token"]}
        target = _occurrences(client, event_id)[1]

        edited = client.post(
            f"/api/v1/events/{event_id}/revisions?scope=single&occurrence={target['occurrence_id']}",
            headers=creator,
            json=_scoped_edit_payload(client, event_id, target, title="Creator tweak"),
        )
        assert edited.status_code == 202, edited.text
        items = _occurrences(client, event_id)
        assert [item["title"] for item in items][1] == "Creator tweak"


def _all_day_payload(**overrides) -> dict:
    day = datetime.now(UTC).date() + timedelta(days=6)
    payload = {
        "title": "Morning run",
        "description": "Easy pace.",
        "is_all_day": True,
        "start_date": day.isoformat(),
        "end_date": (day + timedelta(days=1)).isoformat(),
        "timezone": "America/New_York",
        "recurrence_rule": "FREQ=DAILY;COUNT=4",
        "recurrence_dates": [],
        "group_ids": [],
        "submitter": {"name": "Robin", "channel": "email", "contact": "robin@example.org"},
    }
    payload.update(overrides)
    return payload


def _all_day_scoped_payload(client: TestClient, event_id: str, occurrence: dict, **changes) -> dict:
    detail = client.get(
        f"/api/v1/events/{event_id}?occurrence={occurrence['occurrence_id']}"
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    selected = body["occurrence"]
    payload = _all_day_payload(
        title=selected.get("title", body["title"]),
        description=selected.get("description", body["description"]),
        recurrence_rule=None,
        recurrence_dates=[],
        start_date=selected["start_date"],
        end_date=selected["end_date"],
    )
    payload.update(changes)
    return payload


def test_all_day_single_edit_and_future_delete(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _all_day_payload())
        occurrences = _occurrences(client, event_id)
        assert len(occurrences) == 4

        target = occurrences[1]
        edited = client.post(
            f"/api/v1/admin/events/{event_id}/revisions?scope=single&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_all_day_scoped_payload(client, event_id, target, title="Trail run"),
        )
        assert edited.status_code == 201, edited.text
        items = _occurrences(client, event_id)
        assert [item["title"] for item in items] == (
            ["Morning run", "Trail run"] + ["Morning run"] * 2
        )

        deleted = client.delete(
            f"/api/v1/events/{event_id}?scope=future&occurrence={items[2]['occurrence_id']}",
            headers=ADMIN,
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted_occurrence_count"] == 2
        assert [item["title"] for item in _occurrences(client, event_id)] == [
            "Morning run",
            "Trail run",
        ]


def test_future_removal_from_first_date_falls_back_to_series(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        [first, *_] = _occurrences(client, event_id)

        deleted = client.delete(
            f"/api/v1/events/{event_id}?scope=future&occurrence={first['occurrence_id']}",
            headers=ADMIN,
        )
        assert deleted.status_code == 200, deleted.text
        # Nothing before the first date would remain, so the whole event is gone.
        assert client.get(f"/api/v1/events/{event_id}").status_code == 404

        second_id = _publish(client, _payload(title="Second series"))
        [second_first, *_] = _occurrences(client, second_id)
        cancelled = client.post(
            f"/api/v1/events/{second_id}/cancel?scope=future&occurrence={second_first['occurrence_id']}",
            headers=ADMIN,
        )
        assert cancelled.status_code == 200, cancelled.text
        assert client.get(f"/api/v1/events/{second_id}").json()["is_cancelled"] is True


def test_materialize_does_not_resurrect_scoped_deletions(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload(recurrence_rule="FREQ=DAILY;COUNT=30"))
        assert len(_occurrences(client, event_id)) == 30
        occurrences = _occurrences(client, event_id)
        victim = occurrences[5]

        deleted = client.delete(
            f"/api/v1/events/{event_id}?scope=single&occurrence={victim['occurrence_id']}",
            headers=ADMIN,
        )
        assert deleted.status_code == 200, deleted.text

        database = client.app.state.database
        with database.transaction() as connection:
            result = client.app.state.service.materialize_all(
                connection, past_days=90, future_months=18
            )
        assert result["occurrences_inserted"] == 0
        assert len(_occurrences(client, event_id)) == 29


def test_scoped_routes_require_an_editor(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        target = _occurrences(client, event_id)[0]
        payload = _scoped_edit_payload(client, event_id, target)

        assert (
            client.post(
                f"/api/v1/events/{event_id}/occurrences/{target['occurrence_id']}/edit",
                json=payload,
            ).status_code
            == 401
        )
        assert (
            client.post(
                f"/api/v1/events/{event_id}/occurrences/{target['occurrence_id']}/cancel"
            ).status_code
            == 401
        )
        assert (
            client.delete(
                f"/api/v1/events/{event_id}/occurrences/{target['occurrence_id']}"
            ).status_code
            == 401
        )
        assert (
            client.post(
                f"/api/v1/events/{event_id}/revisions?scope=single&occurrence={target['occurrence_id']}",
                json=payload,
            ).status_code
            == 401
        )


def test_explicit_series_scope_keeps_existing_behavior(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        submitted = client.post("/api/v1/events", json=_payload(title="Series scoped"))
        assert submitted.status_code == 202, submitted.text
        identity = submitted.json()
        creator = {"X-Event-Management-Token": identity["management_token"]}

        # An explicit series scope on the revision route keeps the review flow:
        # a creator edit stays pending instead of applying at once.
        edited = client.post(
            f"/api/v1/events/{identity['event_id']}/revisions?scope=series",
            headers=creator,
            json=_payload(title="Series scoped edit"),
        )
        assert edited.status_code == 202, edited.text
        assert edited.json()["approval_status"] == "pending"


def test_scoped_ops_with_added_and_skipped_dates(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        day = datetime.now(UTC).date() + timedelta(days=6)
        iso = lambda moment: moment.isoformat()
        naive = lambda moment: moment.replace(tzinfo=None).isoformat()
        base_day = datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC)
        extra = base_day + timedelta(days=3)
        event_id = _publish(
            client,
            _payload(
                recurrence_rule="FREQ=WEEKLY;COUNT=5",
                recurrence_dates=[
                    {"local_start": naive(extra), "kind": "include"},
                    {"local_start": naive(base_day + timedelta(days=21)), "kind": "exclude"},
                ],
            ),
        )
        occurrences = _occurrences(client, event_id)
        # The skipped fourth week is gone; the added date joins the series.
        assert len(occurrences) == 5, [(o["starts_at"]) for o in occurrences]

        added = next(
            item for item in occurrences if item["starts_at"] == iso(extra)
        )
        edited = client.post(
            f"/api/v1/admin/events/{event_id}/revisions?scope=single&occurrence={added['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(client, event_id, added, title="Pop-up rehearsal"),
        )
        assert edited.status_code == 201, edited.text
        items = _occurrences(client, event_id)
        assert [item["title"] for item in items] == (
            ["Tuesday choir", "Pop-up rehearsal"] + ["Tuesday choir"] * 3
        )

        target = next(
            item
            for item in _occurrences(client, event_id)
            if item["starts_at"] == iso(base_day + timedelta(days=14))
        )
        future = client.post(
            f"/api/v1/admin/events/{event_id}/revisions?scope=future&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_scoped_edit_payload(client, event_id, target, title="Late series"),
        )
        assert future.status_code == 201, future.text
        items = _occurrences(client, event_id)
        assert len(items) == 5
        assert [item["title"] for item in items] == (
            ["Tuesday choir", "Pop-up rehearsal", "Tuesday choir"]
            + ["Late series"] * 2
        )


def test_shell_contains_scope_choice(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        html = client.get("/").text
        javascript = client.get("/static/app.js").text
        stylesheet = client.get("/static/styles.css").text

    for marker in (
        'id="scope-dialog"',
        'id="scope-title"',
        'name="scope-choice"',
        'id="scope-confirm"',
        'id="event-scope-note"',
        "Only this occurrence",
        "This and all future occurrences",
        "Entire series",
    ):
        assert marker in html
    for marker in (
        "selectedOccurrence?.[field]",
        'shown("title")',
        'shown("description")',
        "askOccurrenceScope",
        "settleScopeChoice",
        "scopedOccurrence",
        "applyScopedPrefill",
        "?scope=${scope}&occurrence=${occurrenceId}",
        "This date is cancelled.",
        "This date has its own details.",
    ):
        assert marker in javascript
    assert ".scope-options" in stylesheet
