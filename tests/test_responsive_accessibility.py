from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database


def _assets(tmp_path: Path) -> tuple[str, str, str]:
    database_url = f"sqlite:///{tmp_path / 'responsive.db'}"
    with TestClient(
        create_app(
            Settings(database_url=database_url, admin_api_key="test-admin-key"),
            create_database(database_url),
        )
    ) as client:
        responses = [client.get(path) for path in ("/", "/static/app.js", "/static/styles.css")]
    for response in responses:
        assert response.status_code == 200
    return tuple(response.text for response in responses)


def test_shell_landmarks_and_roles(tmp_path: Path) -> None:
    shell, _, _ = _assets(tmp_path)
    # Keyboard users can bypass the header actions.
    assert '<a class="skip-link" href="#calendar">' in shell
    assert 'id="calendar" tabindex="-1"' in shell
    # The month grid explains its arrow-key navigation.
    assert 'role="grid" aria-describedby="month-grid-help"' in shell
    assert 'id="month-grid-help" hidden' in shell
    # Week/day views are reached with Tab, so they are regions, not grids.
    assert 'id="week-view" class="time-view" role="region"' in shell
    assert 'id="day-view" class="time-view" role="region"' in shell


def test_month_grid_keyboard_and_touch_helpers(tmp_path: Path) -> None:
    _, javascript, _ = _assets(tmp_path)
    for symbol in (
        # Roving tab stop and WAI-ARIA date-grid keys.
        "monthFocusKey",
        "moveMonthFocus",
        "bindMonthGridKeys",
        '"PageUp"',
        '"Home"',
        'setAttribute("aria-current", "date")',
        # Tapping anywhere in a day cell opens its list.
        "openMonthDay",
        # Phone-width cells hold fewer items; the week view scrolls.
        "COMPACT_MAX_TIMED_PER_DAY",
        "compactLayout",
        "scrollWeekToAnchor",
    ):
        assert symbol in javascript
    # Time-view columns are labeled groups, not orphaned grid cells.
    assert 'setAttribute("role", "gridcell");\n    column' not in javascript
    assert 'column.setAttribute("role", "group")' in javascript


def test_stylesheet_responsive_and_accessible_rules(tmp_path: Path) -> None:
    _, _, stylesheet = _assets(tmp_path)
    for rule in (
        ".skip-link",
        "--coral-ink",
        "@media (max-width: 640px)",
        "#week-view .time-gutter",
        "@media (pointer: coarse)",
        "@supports (height: 100dvh)",
        "@media (forced-colors: active)",
        "@media (prefers-reduced-motion: reduce)",
    ):
        assert rule in stylesheet
    # The Today button stays available on phones.
    assert ".today-button {\n    display: none;" not in stylesheet
