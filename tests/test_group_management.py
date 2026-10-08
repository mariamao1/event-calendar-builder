from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import SQLiteDatabase, create_database
from calendar_api.schemas import GROUP_COLOR_PALETTE

ADMIN = {"X-Admin-Key": "test-admin-key"}


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'groups.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def _group(client: TestClient, slug: str, **fields) -> dict:
    response = client.post(
        "/api/v1/admin/groups",
        headers=ADMIN,
        json={"slug": slug, "name": slug.title(), **fields},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _payload(group_ids: list[str], title: str = "Open studio", **overrides) -> dict:
    day = datetime.now(UTC).date() + timedelta(days=4)
    payload = {
        "title": title,
        "is_all_day": False,
        "starts_at": datetime(day.year, day.month, day.day, 17, tzinfo=UTC).isoformat(),
        "ends_at": datetime(day.year, day.month, day.day, 19, tzinfo=UTC).isoformat(),
        "timezone": "UTC",
        "group_ids": group_ids,
        "submitter": {"name": "Robin", "channel": "email", "contact": "robin@example.org"},
    }
    payload.update(overrides)
    return payload


def _publish(client: TestClient, payload: dict) -> str:
    response = client.post("/api/v1/admin/events", headers=ADMIN, json=payload)
    assert response.status_code == 201, response.text
    return response.json()["event_id"]


def _calendar(client: TestClient, query: str = "") -> list[dict]:
    day = datetime.now(UTC).date()
    response = client.get(
        f"/api/v1/calendar?start={day}&end={day + timedelta(days=60)}&timezone=UTC{query}"
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_group_management_requires_admin(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        group = _group(client, "arts")
        member = {"X-Admin-Key": "wrong"}
        for headers in ({}, member):
            assert client.get("/api/v1/admin/groups", headers=headers).status_code == 401
            created = client.post(
                "/api/v1/admin/groups",
                headers=headers,
                json={"slug": "music", "name": "Music", "color": "#123456"},
            )
            assert created.status_code == 401
            renamed = client.patch(
                f"/api/v1/admin/groups/{group['id']}",
                headers=headers,
                json={"name": "Hijacked", "color": "#000000"},
            )
            assert renamed.status_code == 401
            deleted = client.delete(
                f"/api/v1/admin/groups/{group['id']}", headers=headers
            )
            assert deleted.status_code == 401

        # Nothing changed; everyone can still read and filter by the group.
        public = client.get("/api/v1/groups").json()["items"]
        assert [(item["slug"], item["name"]) for item in public] == [("arts", "Arts")]
        assert _calendar(client, "&group=arts") == []


def test_groups_carry_a_color_identifier(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        # Without a color, each new group takes the next unused palette color.
        arts = _group(client, "arts")
        music = _group(client, "music")
        assert arts["color"] == GROUP_COLOR_PALETTE[0]
        assert music["color"] == GROUP_COLOR_PALETTE[1]

        sports = _group(client, "sports", color=" #A1B2C3 ")
        assert sports["color"] == "#a1b2c3"
        for bad in ("red", "#abc", "#12345g", "a1b2c3"):
            response = client.post(
                "/api/v1/admin/groups",
                headers=ADMIN,
                json={"slug": "bad", "name": "Bad", "color": bad},
            )
            assert response.status_code == 422, bad

        recolored = client.patch(
            f"/api/v1/admin/groups/{arts['id']}", headers=ADMIN, json={"color": "#0F0F0F"}
        )
        assert recolored.status_code == 200, recolored.text
        assert recolored.json()["color"] == "#0f0f0f"
        for field in ("color", "name"):
            cleared = client.patch(
                f"/api/v1/admin/groups/{arts['id']}", headers=ADMIN, json={field: None}
            )
            assert cleared.status_code == 422, field

        # The color reaches every reader: the group list, calendar, and detail.
        public = {item["slug"]: item for item in client.get("/api/v1/groups").json()["items"]}
        assert public["arts"]["color"] == "#0f0f0f"
        event_id = _publish(client, _payload([arts["id"], sports["id"]]))
        [item] = _calendar(client)
        assert {group["slug"]: group["color"] for group in item["groups"]} == {
            "arts": "#0f0f0f",
            "sports": "#a1b2c3",
        }
        detail = client.get(f"/api/v1/events/{event_id}").json()
        assert [group["color"] for group in detail["groups"]] == ["#0f0f0f", "#a1b2c3"]


def test_deleting_a_group_keeps_its_events_visible(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        arts = _group(client, "arts")
        music = _group(client, "music")
        only_arts = _publish(client, _payload([arts["id"]], "Sketch night"))
        both = _publish(client, _payload([arts["id"], music["id"]], "Song and paint"))

        response = client.delete(f"/api/v1/admin/groups/{arts['id']}", headers=ADMIN)
        assert response.status_code == 204, response.text

        # The group is gone for everyone, including admins.
        assert [g["slug"] for g in client.get("/api/v1/groups").json()["items"]] == ["music"]
        admin_list = client.get("/api/v1/admin/groups", headers=ADMIN).json()["items"]
        assert [g["slug"] for g in admin_list] == ["music"]

        # Its events stay on the calendar, just without the deleted group.
        items = {item["event_id"]: item for item in _calendar(client)}
        assert set(items) == {only_arts, both}
        assert items[only_arts]["groups"] == []
        assert [g["slug"] for g in items[both]["groups"]] == ["music"]
        assert client.get(f"/api/v1/events/{only_arts}").json()["groups"] == []
        assert [item["event_id"] for item in _calendar(client, "&group=music")] == [both]
        day = datetime.now(UTC).date()
        filtered = client.get(
            f"/api/v1/calendar?start={day}&end={day + timedelta(days=7)}"
            "&timezone=UTC&group=arts"
        )
        assert filtered.status_code == 422

        # The editable copy no longer offers it, and it can't be assigned.
        admin_event = client.get(f"/api/v1/admin/events/{both}", headers=ADMIN).json()
        assert [g["slug"] for g in admin_event["revisions"][-1]["groups"]] == ["music"]
        assigned = client.post("/api/v1/events", json=_payload([arts["id"]]))
        assert assigned.status_code == 422

        # A deleted group can't be changed or deleted again.
        assert client.patch(
            f"/api/v1/admin/groups/{arts['id']}", headers=ADMIN, json={"name": "Arts"}
        ).status_code == 404
        assert client.delete(
            f"/api/v1/admin/groups/{arts['id']}", headers=ADMIN
        ).status_code == 404

        # Its slug is free again; the new group is a different group.
        reborn = _group(client, "arts")
        assert reborn["id"] != arts["id"]
        assert items[only_arts]["groups"] == []
        assert _calendar(client, "&group=arts") == []


def test_deactivating_differs_from_deleting(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        arts = _group(client, "arts")
        music = _group(client, "music")
        _publish(client, _payload([arts["id"]], "Sketch night"))
        _publish(client, _payload([music["id"]], "Choir"))

        # Deactivating hides the group and events tagged only with it.
        client.patch(
            f"/api/v1/admin/groups/{arts['id']}", headers=ADMIN, json={"is_active": False}
        )
        assert [item["title"] for item in _calendar(client)] == ["Choir"]
        admin_list = client.get("/api/v1/admin/groups", headers=ADMIN).json()["items"]
        assert {g["slug"]: g["is_active"] for g in admin_list} == {
            "arts": False,
            "music": True,
        }

        # Deleting it (even while inactive) brings its events back ungrouped.
        assert client.delete(
            f"/api/v1/admin/groups/{arts['id']}", headers=ADMIN
        ).status_code == 204
        assert sorted(item["title"] for item in _calendar(client)) == [
            "Choir",
            "Sketch night",
        ]


def test_single_date_edit_after_group_deletion(tmp_path: Path) -> None:
    """The form only offers live groups, so a single-date edit of an event
    whose group was deleted sends no groups. That must not read as a
    (series-only) group change."""
    with _client(tmp_path) as client:
        arts = _group(client, "arts")
        music = _group(client, "music")
        event_id = _publish(
            client,
            _payload(
                [arts["id"], music["id"]],
                "Weekly jam",
                recurrence_rule="FREQ=WEEKLY;COUNT=3",
            ),
        )
        client.delete(f"/api/v1/admin/groups/{arts['id']}", headers=ADMIN)
        target = _calendar(client)[1]

        edited = client.post(
            f"/api/v1/admin/events/{event_id}/revisions"
            f"?scope=single&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_payload(
                [music["id"]],
                "Weekly jam (guest night)",
                starts_at=target["starts_at"],
                ends_at=target["ends_at"],
            ),
        )
        assert edited.status_code == 201, edited.text
        assert [item["title"] for item in _calendar(client)] == [
            "Weekly jam",
            "Weekly jam (guest night)",
            "Weekly jam",
        ]


def test_older_local_database_gains_colors_and_deletion(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    schema = files("calendar_api").joinpath("sqlite_schema.sql").read_text()
    start = schema.index("CREATE TABLE IF NOT EXISTS groups")
    end = schema.index(";", start)
    legacy_schema = schema[:start] + """CREATE TABLE IF NOT EXISTS groups (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL CHECK (trim(name) <> ''),
  description TEXT NOT NULL DEFAULT '',
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)""" + schema[end:]
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as connection:
        connection.executescript(legacy_schema)
        connection.executemany(
            "INSERT INTO groups VALUES (?, ?, ?, '', 1, ?, ?)",
            [
                (str(uuid4()), "music", "Music", now, now),
                (str(uuid4()), "arts", "Arts", now, now),
            ],
        )

    url = f"sqlite:///{path}"
    SQLiteDatabase(url).open()
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT slug, color, deleted_at FROM groups ORDER BY name"
        ).fetchall()
        assert rows == [
            ("arts", GROUP_COLOR_PALETTE[0], None),
            ("music", GROUP_COLOR_PALETTE[1], None),
        ]
        # Revisions still reference the rebuilt table.
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(event_revision_groups)"
        ).fetchall()
        assert {row[2] for row in foreign_keys} == {"event_revisions", "groups"}

    with TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    ) as client:
        assert client.post(
            "/api/v1/admin/groups", headers=ADMIN, json={"slug": "arts", "name": "Arts 2"}
        ).status_code == 409
        groups = client.get("/api/v1/admin/groups", headers=ADMIN).json()["items"]
        arts_id = next(g["id"] for g in groups if g["slug"] == "arts")
        assert client.delete(
            f"/api/v1/admin/groups/{arts_id}", headers=ADMIN
        ).status_code == 204
        assert client.post(
            "/api/v1/admin/groups", headers=ADMIN, json={"slug": "arts", "name": "Arts 2"}
        ).status_code == 201


def test_calendar_shell_offers_group_filter_and_management(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        page = client.get("/").text
        javascript = client.get("/static/app.js").text

    for marker in (
        'id="group-filter"',
        'id="manage-groups-button"',
        'id="groups-dialog"',
        'id="group-create-form"',
        'id="group-create-color"',
    ):
        assert marker in page
    for marker in ("renderGroupFilter", "openGroupsDialog", '"/api/v1/admin/groups"'):
        assert marker in javascript
