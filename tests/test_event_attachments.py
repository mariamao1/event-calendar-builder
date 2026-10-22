from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database

ADMIN = {"X-Admin-Key": "test-admin-key"}
FLYER = {"url": "https://example.org/flyer.png", "name": "Flyer"}
AGENDA = {"url": "https://example.org/agenda.pdf", "name": "Agenda"}


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'event-attachments.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def _first_day():
    return datetime.now(UTC).date() + timedelta(days=4)


def _payload(**overrides) -> dict:
    day = _first_day()
    payload = {
        "title": "Tuesday choir",
        "description": "All voices welcome.",
        "location_name": None,
        "location_address": None,
        "event_url": "https://example.org/choir",
        "attachments": [FLYER, AGENDA],
        "is_all_day": False,
        "starts_at": datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC).isoformat(),
        "ends_at": datetime(day.year, day.month, day.day, 19, 30, tzinfo=UTC).isoformat(),
        "timezone": "UTC",
        "recurrence_rule": None,
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


def _occurrences(client: TestClient, event_id: str, **params) -> list[dict]:
    start = _first_day() - timedelta(days=1)
    response = client.get(
        "/api/v1/calendar",
        params={"start": str(start), "end": str(start + timedelta(days=60)), "timezone": "UTC", **params},
    )
    assert response.status_code == 200, response.text
    return [item for item in response.json()["items"] if item["event_id"] == event_id]


def _unfold(body: str) -> list[str]:
    return body.replace("\r\n ", "").split("\r\n")


def _values(lines: list[str], name: str) -> list[str]:
    return [line.split(":", 1)[1] for line in lines if line.startswith(f"{name}:")]


def test_attachments_capped_at_three_and_urls_must_be_http(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        too_many = [
            {"url": f"https://example.org/file-{index}.pdf"} for index in range(4)
        ]
        response = client.post("/api/v1/events", json=_payload(attachments=too_many))
        assert response.status_code == 422, response.text

        for bad in ("example.org/flyer.png", "javascript:alert(1)", "ftp://example.org/flyer.png"):
            response = client.post(
                "/api/v1/events",
                json=_payload(attachments=[{"url": bad}]),
            )
            assert response.status_code == 422, (bad, response.text)

        response = client.post(
            "/api/v1/events",
            json=_payload(attachments=[FLYER, {"url": FLYER["url"], "name": "Again"}]),
        )
        assert response.status_code == 422, response.text

        # No attachments is still a valid event, and names are optional.
        bare = _payload(attachments=[])
        bare.pop("attachments")
        event_id = _publish(client, bare)
        assert client.get(f"/api/v1/events/{event_id}").json()["attachments"] == []

        unnamed = _publish(
            client, _payload(title="Nameless", attachments=[{"url": FLYER["url"]}])
        )
        assert client.get(f"/api/v1/events/{unnamed}").json()["attachments"] == [
            {"url": FLYER["url"], "name": ""}
        ]


def test_attachments_round_trip_through_review_and_reads(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        submitted = client.post("/api/v1/events", json=_payload())
        assert submitted.status_code == 202, submitted.text
        event_id = submitted.json()["event_id"]
        revision_id = submitted.json()["revision_id"]

        revisions = client.get(f"/api/v1/admin/events/{event_id}", headers=ADMIN).json()["revisions"]
        assert revisions[0]["attachments"] == [FLYER, AGENDA]
        # The creator's edit form loads the attachments back.
        editable = client.get(
            f"/api/v1/events/{event_id}/manage",
            headers={"X-Event-Management-Token": submitted.json()["management_token"]},
        )
        assert editable.status_code == 200, editable.text
        assert editable.json()["attachments"] == [FLYER, AGENDA]

        # An admin correction before approval rewrites the pending revision.
        edited = client.post(
            f"/api/v1/events/{event_id}/revisions",
            headers=ADMIN,
            json=_payload(attachments=[AGENDA]),
        )
        assert edited.status_code == 202, edited.text
        approved = client.post(
            f"/api/v1/admin/events/{event_id}/revisions/{revision_id}/approve",
            headers=ADMIN,
            json={"actor": "moderator"},
        )
        assert approved.status_code == 200, approved.text

        detail = client.get(f"/api/v1/events/{event_id}").json()
        assert detail["attachments"] == [AGENDA]
        [item] = _occurrences(client, event_id)
        assert item["attachments"] == [AGENDA]

        # A later edit of the published event can drop them again.
        cleared = client.post(
            f"/api/v1/admin/events/{event_id}/revisions",
            headers=ADMIN,
            json=_payload(attachments=[]),
        )
        assert cleared.status_code == 201, cleared.text
        assert client.get(f"/api/v1/events/{event_id}").json()["attachments"] == []


def test_single_date_can_have_its_own_attachments(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload(recurrence_rule="FREQ=WEEKLY;COUNT=3"))
        occurrences = _occurrences(client, event_id)
        assert len(occurrences) == 3
        target = occurrences[1]
        rehearsal = {"url": "https://example.org/rehearsal.png", "name": "Rehearsal"}

        edited = client.post(
            f"/api/v1/admin/events/{event_id}/revisions"
            f"?scope=single&occurrence={target['occurrence_id']}",
            headers=ADMIN,
            json=_payload(
                attachments=[rehearsal],
                starts_at=target["starts_at"],
                ends_at=target["ends_at"],
            ),
        )
        assert edited.status_code == 201, edited.text

        attachments = [item["attachments"] for item in _occurrences(client, event_id)]
        assert attachments == [[FLYER, AGENDA], [rehearsal], [FLYER, AGENDA]]
        detail = client.get(
            f"/api/v1/events/{event_id}?occurrence={target['occurrence_id']}"
        ).json()
        assert detail["attachments"] == [FLYER, AGENDA]
        assert detail["occurrence"]["attachments"] == [rehearsal]


def test_search_matches_attachment_text(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())
        _publish(client, _payload(title="Book club", attachments=[]))
        found = _occurrences(client, event_id, q="agenda.pdf")
        assert [item["event_id"] for item in found] == [event_id]
        found = _occurrences(client, event_id, q="Flyer")
        assert [item["event_id"] for item in found] == [event_id]


def test_existing_sqlite_database_gains_attachments(tmp_path: Path) -> None:
    path = tmp_path / "event-attachments.db"
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload(attachments=[]))
    # Simulate a calendar created before the column existed.
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE event_revisions DROP COLUMN attachments")
    connection.commit()
    connection.close()

    with _client(tmp_path) as client:
        detail = client.get(f"/api/v1/events/{event_id}").json()
        assert detail["attachments"] == []
        attached = _publish(client, _payload(title="With flyer"))
        assert client.get(f"/api/v1/events/{attached}").json()["attachments"] == [
            FLYER,
            AGENDA,
        ]


def test_exports_include_attachments(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload())

        lines = _unfold(client.get(f"/api/v1/events/{event_id}/ics").text)
        assert _values(lines, "ATTACH") == [FLYER["url"], AGENDA["url"]]
        assert _values(lines, "URL") == ["https://example.org/choir"]

        redirect = client.get(f"/api/v1/events/{event_id}/google", follow_redirects=False)
        assert redirect.status_code == 303
        params = parse_qs(urlparse(redirect.headers["location"]).query)
        assert "attach" not in params


PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415478da6360000000020001e221bc330000000049454e44ae426082"
)
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"


def _upload(client: TestClient, event_id: str, files: list, headers: dict | None = None):
    return client.post(
        f"/api/v1/events/{event_id}/files", headers=headers, files=files
    )


def _png(name: str = "flyer.png"):
    return ("files", (name, PNG_BYTES, "image/png"))


def test_files_upload_download_delete_round_trip(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload(attachments=[]))

        uploaded = _upload(client, event_id, [_png()], ADMIN)
        assert uploaded.status_code == 201, uploaded.text
        [meta] = uploaded.json()["files"]
        assert meta["name"] == "flyer.png"
        assert meta["content_type"] == "image/png"
        assert meta["size"] == len(PNG_BYTES)
        assert meta["is_image"] is True
        file_id = meta["file_id"]

        listed = client.get(f"/api/v1/events/{event_id}/files").json()["files"]
        assert [item["file_id"] for item in listed] == [file_id]

        download = client.get(f"/api/v1/files/{file_id}")
        assert download.status_code == 200, download.text
        assert download.content == PNG_BYTES
        assert download.headers["content-type"] == "image/png"
        assert download.headers["x-content-type-options"] == "nosniff"
        assert "inline" in download.headers["content-disposition"]

        pdf = _upload(
            client, event_id, [("files", ("agenda.pdf", PDF_BYTES, "application/pdf"))], ADMIN
        )
        assert pdf.status_code == 201, pdf.text
        pdf_id = pdf.json()["files"][0]["file_id"]
        assert "attachment" in client.get(f"/api/v1/files/{pdf_id}").headers["content-disposition"]

        deleted = client.delete(f"/api/v1/events/{event_id}/files/{file_id}", headers=ADMIN)
        assert deleted.status_code == 204, deleted.text
        assert client.get(f"/api/v1/files/{file_id}").status_code == 404
        assert client.delete(
            f"/api/v1/events/{event_id}/files/{file_id}", headers=ADMIN
        ).status_code == 404


def test_files_share_the_three_attachment_cap_with_links(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload(attachments=[FLYER, AGENDA]))

        # Two links plus one file fill the cap.
        assert _upload(client, event_id, [_png()], ADMIN).status_code == 201
        over = _upload(client, event_id, [_png("second.png")], ADMIN)
        assert over.status_code == 422, over.text

        # So does adding a third link on top of one file.
        MAP = {"url": "https://example.org/map.png", "name": "Map"}
        one_file = _publish(client, _payload(title="File first", attachments=[]))
        assert _upload(client, one_file, [_png()], ADMIN).status_code == 201
        edited = client.post(
            f"/api/v1/admin/events/{one_file}/revisions",
            headers=ADMIN,
            json=_payload(title="File first", attachments=[FLYER, AGENDA, MAP]),
        )
        assert edited.status_code == 422, edited.text
        capped = client.post(
            f"/api/v1/admin/events/{one_file}/revisions",
            headers=ADMIN,
            json=_payload(title="File first", attachments=[FLYER, AGENDA]),
        )
        assert capped.status_code == 201, capped.text


def test_files_reject_bad_type_size_and_empty(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload(title="Uploads", attachments=[]))

        exe = _upload(client, event_id, [("files", ("run.exe", b"MZ", "application/x-msdownload"))], ADMIN)
        assert exe.status_code == 422, exe.text

        renamed = _upload(client, event_id, [("files", ("flyer.png", b"PK", "application/pdf"))], ADMIN)
        assert renamed.status_code == 422, renamed.text

        empty = _upload(client, event_id, [("files", ("blank.png", b"", "image/png"))], ADMIN)
        assert empty.status_code == 422, empty.text

        big = _upload(
            client, event_id, [("files", ("huge.png", b"x" * (5 * 1024 * 1024 + 1), "image/png"))], ADMIN
        )
        assert big.status_code == 422, big.text


def test_files_need_an_editor_and_hide_until_published(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        submitted = client.post("/api/v1/events", json=_payload(attachments=[]))
        assert submitted.status_code == 202, submitted.text
        event_id = submitted.json()["event_id"]
        token = {"X-Event-Management-Token": submitted.json()["management_token"]}

        # Strangers cannot upload, list, or fetch from a pending event.
        assert _upload(client, event_id, [_png()]).status_code == 401
        assert client.get(f"/api/v1/events/{event_id}/files").status_code == 404

        uploaded = _upload(client, event_id, [_png()], token)
        assert uploaded.status_code == 201, uploaded.text
        file_id = uploaded.json()["files"][0]["file_id"]
        assert client.get(f"/api/v1/files/{file_id}").status_code == 404
        creator_download = client.get(f"/api/v1/files/{file_id}", headers=token)
        assert creator_download.status_code == 200, creator_download.text
        assert creator_download.content == PNG_BYTES

        revision_id = submitted.json()["revision_id"]
        approved = client.post(
            f"/api/v1/admin/events/{event_id}/revisions/{revision_id}/approve",
            headers=ADMIN,
            json={"actor": "moderator"},
        )
        assert approved.status_code == 200, approved.text
        assert client.get(f"/api/v1/files/{file_id}").status_code == 200


def test_files_appear_in_event_reads(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        event_id = _publish(client, _payload(attachments=[FLYER]))
        file_id = _upload(client, event_id, [_png()], ADMIN).json()["files"][0]["file_id"]

        detail = client.get(f"/api/v1/events/{event_id}").json()
        assert [item["file_id"] for item in detail["files"]] == [file_id]
        assert detail["attachments"] == [FLYER]

        [item] = _occurrences(client, event_id)
        assert [entry["file_id"] for entry in item["files"]] == [file_id]

        editable = client.get(f"/api/v1/events/{event_id}/manage", headers=ADMIN)
        assert [entry["file_id"] for entry in editable.json()["files"]] == [file_id]

        revisions = client.get(f"/api/v1/admin/events/{event_id}", headers=ADMIN).json()
        assert [entry["file_id"] for entry in revisions["files"]] == [file_id]


def test_frontend_offers_attachments(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        html = client.get("/").text
        javascript = client.get("/static/app.js").text
    for marker in ('id="event-attachments"', "Attachments", "attachment"):
        assert marker in html
    for marker in (
        "attachments:",
        "attachmentRow",
        "isImageAttachment",
        "revision.attachments",
    ):
        assert marker in javascript
    for marker in ('id="event-file-input"', 'id="event-file-list"'):
        assert marker in html
    for marker in (
        "event-file-input",
        "uploadEventFiles",
        "fileAttachmentRow",
        "eventFileUrl",
    ):
        assert marker in javascript
