from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database


def _client(tmp_path: Path, *, token: str | None = None) -> TestClient:
    database_url = f"sqlite:///{tmp_path / 'week-day-view.db'}"
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


def _shell(tmp_path: Path) -> str:
    with _client(tmp_path) as client:
        response = client.get("/")
    assert response.status_code == 200
    return response.text


def _javascript(tmp_path: Path) -> str:
    with _client(tmp_path) as client:
        response = client.get("/static/app.js")
    assert response.status_code == 200
    return response.text


def _stylesheet(tmp_path: Path) -> str:
    with _client(tmp_path) as client:
        response = client.get("/static/styles.css")
    assert response.status_code == 200
    return response.text


def test_week_and_day_shell_markers(tmp_path: Path) -> None:
    shell = _shell(tmp_path)
    for marker in (
        'id="view-switcher"',
        'data-view="month"',
        'data-view="week"',
        'data-view="day"',
        'id="week-view"',
        'id="day-view"',
        'id="week-allday"',
        'id="day-allday"',
        'id="week-grid"',
        'id="day-grid"',
        # The month view keeps working alongside the new views.
        'id="month-grid"',
        'id="day-dialog"',
        'id="event-dialog"',
    ):
        assert marker in shell


def test_time_view_layout_helpers(tmp_path: Path) -> None:
    javascript = _javascript(tmp_path)
    for symbol in (
        # Per-day time slices clipped from timed events.
        "timedSegmentsForDay",
        # Side-by-side columns for overlapping events.
        "layoutTimedColumns",
        # All-day events stay out of the time grid.
        "allDayEventsForDay",
        # Adaptive visible hour range over the events present.
        "computeVisibleHours",
        "TIME_VIEW_DEFAULT_START",
        "TIME_VIEW_DEFAULT_END",
        "setView",
        "renderTimeView",
        "renderTimeBlock",
        # Month helpers keep working.
        "layoutBands",
        "MAX_TIMED_PER_DAY",
    ):
        assert symbol in javascript


def test_time_view_styles(tmp_path: Path) -> None:
    stylesheet = _stylesheet(tmp_path)
    for selector in (
        ".view-switcher",
        ".time-view",
        ".allday-row",
        ".allday-chip",
        ".time-grid",
        ".time-column",
        ".time-block",
        ".hour-label",
        ".now-line",
    ):
        assert selector in stylesheet


def test_hidden_month_grid_stays_hidden(tmp_path: Path) -> None:
    """Author `display` rules beat the UA `[hidden]` rule, so hiding the
    month grid needs an explicit selector or the month view leaks through
    on the Week/Day tabs."""
    stylesheet = _stylesheet(tmp_path)
    assert ".month-grid[hidden]" in stylesheet
    assert ".weekday-row[hidden]" in stylesheet


def test_view_selection_persists_in_url(tmp_path: Path) -> None:
    """The selected view and cursor date live in ?view=…&date=… (not browser
    storage), so a refresh or a shared link keeps the user in week/day view."""
    javascript = _javascript(tmp_path)
    for symbol in (
        "readUrlState",
        "writeUrlState",
        "parseAnchor",
        "replaceState",
        "searchParams",
    ):
        assert symbol in javascript
    assert "localStorage" not in javascript


def test_time_header_fits_its_row(tmp_path: Path) -> None:
    """The per-day header stacks weekday over date inside a fixed-height
    row; a third row spills into the time grid, so only two rows may be
    emitted and the row must be tall enough to hold them."""
    import re

    javascript = _javascript(tmp_path)
    assert "time-header-month" not in javascript
    assert "header.append(weekday, number);" in javascript
    stylesheet = _stylesheet(tmp_path)
    assert re.search(r"\.time-header-row\s*\{[^}]*height:\s*60px", stylesheet)
    assert re.search(r"\.time-gutter-head\s*\{[^}]*height:\s*60px", stylesheet)
    assert re.search(r"\.time-header\s*\{[^}]*overflow:\s*hidden", stylesheet)


def test_private_shell_still_gates_week_day_views(tmp_path: Path) -> None:
    with _client(tmp_path, token="private-calendar") as client:
        assert client.get("/").status_code == 401
        assert client.get("/?token=private-calendar").status_code == 200
        assert client.get("/static/app.js").status_code == 200
