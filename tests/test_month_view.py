from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database


def _client(tmp_path: Path, *, token: str | None = None) -> TestClient:
    database_url = f"sqlite:///{tmp_path / 'month-view.db'}"
    return TestClient(
        create_app(
            Settings(
                database_url=database_url,
                admin_api_key="test-admin-key",
                calendar_access_token=token,
            ),
            create_database(database_url),
        )
    )


def test_root_serves_calendar_shell(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    for marker in (
        'id="month-grid"',
        'id="month-jump"',
        'id="day-dialog"',
        'id="event-dialog"',
        "/static/app.js",
        "/static/styles.css",
    ):
        assert marker in response.text


def test_calendar_assets_and_unknown_path(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        javascript = client.get("/static/app.js")
        stylesheet = client.get("/static/styles.css")
        missing = client.get("/static/not-found.js")

    assert javascript.status_code == 200
    assert "javascript" in javascript.headers["content-type"]
    assert "layoutBands" in javascript.text
    assert "MAX_TIMED_PER_DAY" in javascript.text
    assert stylesheet.status_code == 200
    assert ".month-grid" in stylesheet.text
    assert ".more-btn" in stylesheet.text
    assert missing.status_code == 404


def test_private_calendar_shell_requires_link_token(tmp_path: Path) -> None:
    with _client(tmp_path, token="private-calendar") as client:
        assert client.get("/").status_code == 401
        assert client.get("/?token=private-calendar").status_code == 200
        # Assets contain no calendar data and can be cached without a token.
        assert client.get("/static/app.js").status_code == 200
