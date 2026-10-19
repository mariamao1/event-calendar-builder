from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from calendar_api.address_search import (
    AddressSearch,
    AddressSearchUnavailable,
    format_feature,
)
from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database

PHOTON = "https://photon.example.org/api/"


def _feature(**properties) -> dict:
    return {"type": "Feature", "geometry": {"type": "Point"}, "properties": properties}


LIBRARY = _feature(
    osm_key="amenity",
    osm_value="library",
    type="house",
    name="Central Library",
    housenumber="10",
    street="Grand Army Plaza",
    city="New York",
    state="New York",
    postcode="11238",
    country="United States",
)
HOUSE = _feature(
    osm_key="place",
    osm_value="house",
    type="house",
    housenumber="1",
    street="Main Street",
    city="Springfield",
    state="Illinois",
    postcode="62701",
    country="United States",
)
STREET = _feature(
    osm_key="highway",
    osm_value="residential",
    type="street",
    name="Main Street",
    city="Springfield",
    state="Illinois",
    country="United States",
)
CITY = _feature(osm_key="place", osm_value="city", type="city", name="Springfield", country="United States")


class FakePhoton:
    def __init__(self, features: list[dict] | None = None, *, fail: bool = False):
        self.features = features if features is not None else [LIBRARY, HOUSE]
        self.fail = fail
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.urls.append(url)
        if self.fail:
            raise OSError("connection refused")
        return {"type": "FeatureCollection", "features": self.features}


def test_format_feature_builds_street_addresses() -> None:
    assert format_feature(LIBRARY) == {
        "name": "Central Library",
        "address": "10 Grand Army Plaza, New York, New York 11238, United States",
        "label": "Central Library, 10 Grand Army Plaza, New York, New York 11238, United States",
    }
    assert format_feature(HOUSE) == {
        "name": None,
        "address": "1 Main Street, Springfield, Illinois 62701, United States",
        "label": "1 Main Street, Springfield, Illinois 62701, United States",
    }
    # A street itself is a usable address line; its name is not a venue.
    assert format_feature(STREET)["address"] == "Main Street, Springfield, Illinois, United States"
    assert format_feature(STREET)["name"] is None
    # A bare town is not a street address.
    assert format_feature(CITY) is None


def test_suggest_queries_photon_and_caches() -> None:
    fake = FakePhoton([LIBRARY, HOUSE, HOUSE])
    search = AddressSearch(PHOTON, near=(40.67, -73.96), fetch=fake)

    items = search.suggest("  10 Grand   Army ")
    assert [item["address"] for item in items] == [
        "10 Grand Army Plaza, New York, New York 11238, United States",
        "1 Main Street, Springfield, Illinois 62701, United States",
    ]
    [url] = fake.urls
    assert url.startswith(PHOTON)
    params = parse_qs(urlparse(url).query)
    assert params["q"] == ["10 Grand Army"]
    assert params["layer"] == ["house", "street"]
    assert params["lat"] == ["40.67"] and params["lon"] == ["-73.96"]

    # Same text (any case/spacing) is answered from the cache.
    assert search.suggest("10 grand army") == items
    assert len(fake.urls) == 1


def test_suggest_keeps_one_venue_per_address() -> None:
    tenant = _feature(**{**LIBRARY["properties"], "name": "Library Cafe"})
    search = AddressSearch(PHOTON, fetch=FakePhoton([LIBRARY, tenant, HOUSE]))
    assert [item["name"] for item in search.suggest("10 Grand Army Plaza")] == [
        "Central Library",
        None,
    ]


def test_suggest_failures_are_reported_and_not_cached() -> None:
    fake = FakePhoton(fail=True)
    search = AddressSearch(PHOTON, fetch=fake)
    with pytest.raises(AddressSearchUnavailable):
        search.suggest("1 Main Street")
    fake.fail = False
    assert search.suggest("1 Main Street")
    assert len(fake.urls) == 2


def _client(tmp_path: Path, fake: FakePhoton | None = None, **settings) -> TestClient:
    url = f"sqlite:///{tmp_path / 'address.db'}"
    options = {"address_search_url": PHOTON, **settings}
    client = TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key", **options),
            create_database(url),
        )
    )
    if fake is not None:
        client.app.state.address_search.fetch = fake
    return client


def test_endpoint_returns_suggestions(tmp_path: Path) -> None:
    fake = FakePhoton()
    with _client(tmp_path, fake) as client:
        short = client.get("/api/v1/address-suggestions", params={"q": "10"})
        assert short.status_code == 200
        assert short.json() == {"items": []}
        assert fake.urls == []

        response = client.get("/api/v1/address-suggestions", params={"q": "10 Grand"})
        assert response.status_code == 200, response.text
        assert [item["name"] for item in response.json()["items"]] == ["Central Library", None]


def test_endpoint_reports_upstream_failure(tmp_path: Path) -> None:
    with _client(tmp_path, FakePhoton(fail=True)) as client:
        response = client.get("/api/v1/address-suggestions", params={"q": "1 Main"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "address_search_unavailable"


def test_endpoint_is_rate_limited(tmp_path: Path) -> None:
    with _client(tmp_path, FakePhoton(), address_search_rate_limit_max=2) as client:
        for text in ("1 Main", "2 Main"):
            assert client.get("/api/v1/address-suggestions", params={"q": text}).status_code == 200
        limited = client.get("/api/v1/address-suggestions", params={"q": "3 Main"})
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) >= 1


def test_endpoint_can_be_turned_off(tmp_path: Path) -> None:
    with _client(tmp_path, address_search_url="") as client:
        response = client.get("/api/v1/address-suggestions", params={"q": "1 Main"})
        assert response.status_code == 404


def test_endpoint_requires_link_token(tmp_path: Path) -> None:
    with _client(tmp_path, FakePhoton(), calendar_access_token="secret-link") as client:
        path = "/api/v1/address-suggestions"
        assert client.get(path, params={"q": "1 Main"}).status_code == 401
        assert client.get(path, params={"q": "1 Main", "token": "secret-link"}).status_code == 200


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADDRESS_SEARCH_NEAR", " 40.67, -73.96 ")
    monkeypatch.setenv("ADDRESS_SEARCH_URL", "")
    settings = Settings.from_env()
    assert settings.address_search_near == (40.67, -73.96)
    assert settings.address_search_url == ""

    monkeypatch.delenv("ADDRESS_SEARCH_URL")
    monkeypatch.delenv("ADDRESS_SEARCH_NEAR")
    defaults = Settings.from_env()
    assert defaults.address_search_url == "https://photon.komoot.io/api/"
    assert defaults.address_search_near is None

    monkeypatch.setenv("ADDRESS_SEARCH_NEAR", "somewhere")
    with pytest.raises(ValueError, match="ADDRESS_SEARCH_NEAR"):
        Settings.from_env()


def test_frontend_offers_address_suggestions(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        html = client.get("/").text
        javascript = client.get("/static/app.js").text
    for marker in (
        'role="combobox"',
        'aria-controls="event-address-suggestions"',
        'id="event-address-suggestions"',
        'role="listbox"',
        "OpenStreetMap",
    ):
        assert marker in html
    for marker in (
        "/api/v1/address-suggestions",
        "function chooseAddressSuggestion",
        "aria-activedescendant",
        "new AbortController()",
    ):
        assert marker in javascript
