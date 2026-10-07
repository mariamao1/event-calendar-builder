"""Skipping or cancelling one date of a recurring event.

A date cancelled or deleted on its own (scope=single) is a durable exception:
the series is not ended or altered, later series-wide edits keep the date
cancelled or skipped, and editors can restore it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database

ADMIN = {"X-Admin-Key": "test-admin-key"}


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'occurrence-exceptions.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def _first_day():
    return datetime.now(UTC).date() + timedelta(days=6)


def _payload(hour: int = 18, **overrides) -> dict:
    day = _first_day()
    payload = {
        "title": "Tuesday choir",
        "description": "Bring water.",
        "location_name": "Library hall",
        "is_all_day": False,
        "starts_at": datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC).isoformat(),
        "ends_at": datetime(day.year, day.month, day.day, hour + 1, 30, tzinfo=UTC).isoformat(),
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


def _edit_series(client: TestClient, event_id: str, payload: dict) -> None:
    response = client.post(
        f"/api/v1/admin/events/{event_id}/revisions", headers=ADMIN, json=payload
    )
    assert response.status_code == 201, response.text


def _occurrences(client: TestClient, event_id: str) -> list[dict]:
    day = datetime.now(UTC).date()
    response = client.get(
        f"/api/v1/calendar?start={day}&end={day + timedelta(days=60)}&timezone=UTC"
    )
    assert response.status_code == 200, response.text
    return [item for item in response.json()["items"] if item["event_id"] == event_id]


def _days(items: list[dict]) -> list[str]:
    return [item["starts_at"][:10] for item in items]


def _cancel(client: TestClient, event_id: str, occurrence: dict, headers=ADMIN) -> None:
    response = client.post(
        f"/api/v1/events/{event_id}/occurrences/{occurrence['occurrence_id']}/cancel",
        headers=headers,
    )
    assert response.status_code == 200, response.text


def _skip(client: TestClient, event_id: str, occurrence: dict, headers=ADMIN) -> None:
    response = client.delete(
        f"/api/v1/events/{event_id}/occurrences/{occurrence['occurrence_id']}",
        headers=headers,
    )
    assert response.status_code == 200, response.text


def _restore(client: TestClient, event_id: str, occurrence_id: str, headers=ADMIN):
    return client.post(
        f"/api/v1/events/{event_id}/occurrences/{occurrence_id}/restore",
        headers=headers,
    )


def _series(client: TestClient, event_id: str) -> dict:
    response = client.get(f"/api/v1/events/{event_id}")
    assert response.status_code == 200, response.text
    return response.json()["series"]


def test_skip_and_cancel_leave_the_series_rule_alone(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        _skip(client, event_id, occurrences[1])
        _cancel(client, event_id, occurrences[3])

        detail = client.get(f"/api/v1/events/{event_id}").json()
        assert detail["recurrence_rule"] == "FREQ=WEEKLY;COUNT=5"
        assert detail["recurrence_dates"] == []
        assert detail["revision_number"] == 1

        items = _occurrences(client, event_id)
        assert _days(items) == _days(occurrences[:1] + occurrences[2:])
        assert [item["is_cancelled"] for item in items] == [False, False, True, False]

        # The skipped date is listed so editors can bring it back.
        skipped = detail["series"]["skipped"]
        assert [item["occurrence_id"] for item in skipped] == [occurrences[1]["occurrence_id"]]
        assert skipped[0]["instance_exception"] == "skipped"
        cancelled = client.get(
            f"/api/v1/events/{event_id}?occurrence={occurrences[3]['occurrence_id']}"
        ).json()["occurrence"]
        assert cancelled["instance_exception"] == "cancelled"
        assert cancelled["is_cancelled"] is True


def test_exceptions_survive_series_wide_edits(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        _skip(client, event_id, occurrences[1])
        _cancel(client, event_id, occurrences[3])

        _edit_series(client, event_id, _payload(title="Choir (new room)"))

        items = _occurrences(client, event_id)
        assert [item["title"] for item in items] == ["Choir (new room)"] * 4
        assert _days(items) == _days(occurrences[:1] + occurrences[2:])
        assert [item["is_cancelled"] for item in items] == [False, False, True, False]
        assert [item["occurrence_id"] for item in _series(client, event_id)["skipped"]] == [
            occurrences[1]["occurrence_id"]
        ]


def test_exceptions_survive_an_approved_creator_edit(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        submitted = client.post("/api/v1/events", json=_payload())
        assert submitted.status_code == 202, submitted.text
        identity = submitted.json()
        event_id = identity["event_id"]
        creator = {"X-Event-Management-Token": identity["management_token"]}
        approved = client.post(
            f"/api/v1/admin/events/{event_id}/revisions/{identity['revision_id']}/approve",
            headers=ADMIN,
            json={"actor": "mod"},
        )
        assert approved.status_code == 200, approved.text
        occurrences = _occurrences(client, event_id)

        # The creator can skip and cancel single dates, even while an edit of
        # theirs waits for review, and approving that edit keeps both.
        pending = client.post(
            f"/api/v1/events/{event_id}/revisions",
            headers=creator,
            json=_payload(description="Bring water and a folder."),
        )
        assert pending.status_code == 202, pending.text
        _skip(client, event_id, occurrences[0], headers=creator)
        _cancel(client, event_id, occurrences[2], headers=creator)

        approved = client.post(
            f"/api/v1/admin/events/{event_id}/revisions/{pending.json()['revision_id']}/approve",
            headers=ADMIN,
            json={"actor": "mod"},
        )
        assert approved.status_code == 200, approved.text
        items = _occurrences(client, event_id)
        assert [item["description"] for item in items] == ["Bring water and a folder."] * 4
        assert _days(items) == _days(occurrences[1:])
        assert [item["is_cancelled"] for item in items] == [False, True, False, False]


def test_exceptions_follow_a_time_of_day_change(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload(hour=18))
        occurrences = _occurrences(client, event_id)
        _skip(client, event_id, occurrences[1])
        _cancel(client, event_id, occurrences[3])

        # Moving the series to 19:00 mints new slots; the skipped and
        # cancelled days stay skipped and cancelled.
        _edit_series(client, event_id, _payload(hour=19))

        items = _occurrences(client, event_id)
        assert [item["starts_at"][11:16] for item in items] == ["19:00"] * 4
        assert _days(items) == _days(occurrences[:1] + occurrences[2:])
        assert [item["is_cancelled"] for item in items] == [False, False, True, False]

        skipped = _series(client, event_id)["skipped"]
        assert len(skipped) == 1
        assert skipped[0]["starts_at"][:16] == occurrences[1]["starts_at"][:11] + "19:00"

        # Restoring the skipped day brings it back at the series' new time.
        restored = _restore(client, event_id, skipped[0]["occurrence_id"])
        assert restored.status_code == 200, restored.text
        assert restored.json()["restored"] == "skipped"
        items = _occurrences(client, event_id)
        assert _days(items) == _days(occurrences)
        assert items[1]["starts_at"][11:16] == "19:00"


def test_future_edit_keeps_later_exceptions(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        _skip(client, event_id, occurrences[3])
        _cancel(client, event_id, occurrences[4])

        target = occurrences[1]
        detail = client.get(
            f"/api/v1/events/{event_id}?occurrence={target['occurrence_id']}"
        ).json()["occurrence"]
        future = client.post(
            f"/api/v1/events/{event_id}/occurrences/{target['occurrence_id']}/edit?scope=future",
            headers=ADMIN,
            json=_payload(
                title="Late series",
                starts_at=detail["starts_at"],
                ends_at=detail["ends_at"],
                recurrence_rule=None,
            ),
        )
        assert future.status_code == 200, future.text

        items = _occurrences(client, event_id)
        assert [item["title"] for item in items] == ["Tuesday choir"] + ["Late series"] * 3
        assert _days(items) == _days(occurrences[:3] + occurrences[4:])
        assert [item["is_cancelled"] for item in items] == [False, False, False, True]


def test_editing_a_cancelled_date_keeps_it_cancelled(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        target = _occurrences(client, event_id)[2]
        _cancel(client, event_id, target)

        edited = client.post(
            f"/api/v1/events/{event_id}/occurrences/{target['occurrence_id']}/edit",
            headers=ADMIN,
            json=_payload(
                description="Called off: the hall is flooded.",
                starts_at=target["starts_at"],
                ends_at=target["ends_at"],
                recurrence_rule=None,
            ),
        )
        assert edited.status_code == 200, edited.text
        item = _occurrences(client, event_id)[2]
        assert item["description"] == "Called off: the hall is flooded."
        assert item["is_cancelled"] is True


def test_restore_a_cancelled_date(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        target = _occurrences(client, event_id)[2]
        _cancel(client, event_id, target)

        restored = _restore(client, event_id, target["occurrence_id"])
        assert restored.status_code == 200, restored.text
        body = restored.json()
        assert body["restored"] == "cancelled"
        assert body["occurrence_id"] == target["occurrence_id"]
        assert [item["is_cancelled"] for item in _occurrences(client, event_id)] == [False] * 5
        selected = client.get(
            f"/api/v1/events/{event_id}?occurrence={target['occurrence_id']}"
        ).json()["occurrence"]
        assert selected["instance_exception"] is None

        # Nothing left to restore; the date can be cancelled again.
        assert _restore(client, event_id, target["occurrence_id"]).status_code == 409
        _cancel(client, event_id, target)

        database = client.app.state.database
        with database.connection() as connection:
            actions = [
                row["action"]
                for row in connection.execute(
                    "SELECT action FROM event_review_actions WHERE event_id = ?"
                    " ORDER BY occurred_at, rowid",
                    (event_id,),
                )
            ]
        assert actions == ["approve", "cancel", "restore", "cancel"]


def test_restore_a_skipped_date(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        _skip(client, event_id, occurrences[2])
        assert len(_occurrences(client, event_id)) == 4

        restored = _restore(client, event_id, occurrences[2]["occurrence_id"])
        assert restored.status_code == 200, restored.text
        items = _occurrences(client, event_id)
        assert [item["occurrence_id"] for item in items] == [
            item["occurrence_id"] for item in occurrences
        ]
        assert _series(client, event_id)["skipped"] == []


def test_restore_rules(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        submitted = client.post("/api/v1/events", json=_payload())
        identity = submitted.json()
        event_id = identity["event_id"]
        client.post(
            f"/api/v1/admin/events/{event_id}/revisions/{identity['revision_id']}/approve",
            headers=ADMIN,
            json={"actor": "mod"},
        )
        occurrences = _occurrences(client, event_id)
        creator = {"X-Event-Management-Token": identity["management_token"]}
        _cancel(client, event_id, occurrences[0])

        # Only an editor may restore; the creator's token is enough.
        assert _restore(client, event_id, occurrences[0]["occurrence_id"], headers={}).status_code == 401
        assert (
            _restore(
                client,
                event_id,
                occurrences[0]["occurrence_id"],
                headers={"X-Event-Management-Token": "wrong"},
            ).status_code
            == 401
        )
        assert _restore(client, event_id, occurrences[0]["occurrence_id"], headers=creator).status_code == 200

        # A date that was never removed is not restorable.
        assert _restore(client, event_id, occurrences[1]["occurrence_id"]).status_code == 409
        # Unknown occurrences read as missing.
        missing = "00000000-0000-0000-0000-000000000000"
        assert _restore(client, event_id, missing).status_code == 404

        # "This and future" cancellations end the series rule; restoring one
        # of those dates means editing the series instead.
        future = client.post(
            f"/api/v1/events/{event_id}/occurrences/{occurrences[3]['occurrence_id']}/cancel?scope=future",
            headers=ADMIN,
        )
        assert future.status_code == 200, future.text
        assert _restore(client, event_id, occurrences[4]["occurrence_id"]).status_code == 409


def test_exception_lapses_when_the_series_drops_the_date(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        _skip(client, event_id, occurrences[4])

        # The series now ends before the skipped date, then grows back: the
        # date returns as an ordinary date, since the skip no longer applied.
        _edit_series(client, event_id, _payload(recurrence_rule="FREQ=WEEKLY;COUNT=4"))
        assert _series(client, event_id)["skipped"] == []
        _edit_series(client, event_id, _payload(recurrence_rule="FREQ=WEEKLY;COUNT=5"))
        items = _occurrences(client, event_id)
        assert _days(items) == _days(occurrences)
        assert all(item["is_cancelled"] is False for item in items)


def test_event_that_stops_repeating_drops_exceptions(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        occurrences = _occurrences(client, event_id)
        _skip(client, event_id, occurrences[0])

        # A one-off on the skipped day is still visible: only a repeating
        # series has dates to skip.
        _edit_series(client, event_id, _payload(recurrence_rule=None))
        items = _occurrences(client, event_id)
        assert _days(items) == _days(occurrences[:1])
        assert items[0]["is_cancelled"] is False


def test_materialize_keeps_exceptions(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload(recurrence_rule="FREQ=WEEKLY"))
        occurrences = _occurrences(client, event_id)
        _skip(client, event_id, occurrences[1])
        _cancel(client, event_id, occurrences[2])

        database = client.app.state.database
        with database.transaction() as connection:
            client.app.state.service.materialize_all(
                connection,
                past_days=90,
                future_months=24,
            )
        items = _occurrences(client, event_id)
        assert occurrences[1]["occurrence_id"] not in {item["occurrence_id"] for item in items}
        assert [item["is_cancelled"] for item in items[:3]] == [False, True, False]


def test_stale_database_gains_restorable_skips(tmp_path: Path) -> None:
    """A database from before durable exceptions upgrades in place."""
    import sqlite3

    db_path = tmp_path / "stale.db"
    url = f"sqlite:///{db_path}"
    with _client_for(url) as client:
        event_id = _publish(client, _payload())
        victim = _occurrences(client, event_id)[1]
        _skip(client, event_id, victim)

    # Recreate the previous release's shape: no instance_exception column and
    # an audit CHECK without 'restore'.
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("UPDATE event_occurrences SET instance_exception = NULL")
        connection.execute("ALTER TABLE event_occurrences DROP COLUMN instance_exception")
        legacy = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'event_review_actions'"
        ).fetchone()[0]
        connection.executescript(
            f"""
            ALTER TABLE event_review_actions RENAME TO old_actions;
            {legacy.replace(", 'restore'", "")};
            INSERT INTO event_review_actions SELECT * FROM old_actions;
            DROP TABLE old_actions;
            """
        )
        connection.commit()
    finally:
        connection.close()

    with _client_for(url) as client:
        skipped = _series(client, event_id)["skipped"]
        assert [item["occurrence_id"] for item in skipped] == [victim["occurrence_id"]]
        restored = _restore(client, event_id, victim["occurrence_id"])
        assert restored.status_code == 200, restored.text
        assert len(_occurrences(client, event_id)) == 5


def _client_for(url: str) -> TestClient:
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def test_shell_contains_restore_controls(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        html = client.get("/").text
        javascript = client.get("/static/app.js").text
        stylesheet = client.get("/static/styles.css").text

    for marker in ('id="restore-date-button"', 'id="event-series-skipped"'):
        assert marker in html
    for marker in (
        "restoreOccurrence",
        "/restore",
        "instance_exception",
        "series.skipped",
    ):
        assert marker in javascript
    assert ".series-skipped" in stylesheet
