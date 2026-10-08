from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database
from calendar_api.schemas import GROUP_COLOR_PALETTE


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'group-form.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def test_group_slug_autopopulates_from_name(tmp_path: Path) -> None:
    """The create-group slug is derived from the name until manually edited.

    Regression test: the name field once filled the slug only while it was
    completely empty, so the slug went stale on any later name edit and never
    recovered. The shell must instead explain the slug and wire it to follow
    the name until the admin touches it.
    """
    with _client(tmp_path) as client:
        shell = client.get("/").text
        javascript = client.get("/static/app.js").text

    assert 'id="group-create-slug"' in shell
    assert "derived from the name" in shell
    for symbol in (
        "suggestGroupSlug",
        "groupSlugTouched",
        'querySelector("#group-create-name").addEventListener("input"',
        'querySelector("#group-create-slug").addEventListener("input"',
        "nextGroupColor",
        "groupCreateColorTouched",
        'querySelector("#group-create-color").addEventListener("input"',
    ):
        assert symbol in javascript


def test_group_create_color_palette_matches_server(tmp_path: Path) -> None:
    """The form must offer the same next-unused-color palette as the API.

    The shell keeps its own palette copy to default the color input; if it
    drifts from the server palette, new groups would start with colors the
    backend would never assign.
    """
    with _client(tmp_path) as client:
        javascript = client.get("/static/app.js").text
    match = re.search(r"const GROUP_COLOR_PALETTE = \[(.*?)\];", javascript, re.S)
    assert match is not None
    found = [item.strip('"') for item in re.findall(r'"#[0-9a-f]{6}"', match.group(1))]
    assert found == list(GROUP_COLOR_PALETTE)
