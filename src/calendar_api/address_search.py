"""Street-address suggestions for the event form, backed by Photon.

Photon (https://photon.komoot.io) is an OpenStreetMap geocoder built for
search-as-you-type. The browser asks this server, which asks Photon, so the
submitter's IP never reaches the third party and the upstream can be swapped
for a self-hosted instance through ADDRESS_SEARCH_URL. Results are cached per
normalized query to keep repeat lookups off the public instance.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

USER_AGENT = "event-calendar-builder/0.1 (community calendar address search)"
MIN_QUERY_LENGTH = 3
MAX_QUERY_LENGTH = 200
DEFAULT_LIMIT = 5
CACHE_SIZE = 256
TIMEOUT_SECONDS = 3.0

# Photon feature types that name a road rather than a venue; their `name` is
# the street line itself.
_STREET_TYPES = {"street"}


class AddressSearchUnavailable(Exception):
    """The upstream geocoder failed, timed out, or returned garbage."""


def normalize_query(value: str) -> str:
    return " ".join(value.split())[:MAX_QUERY_LENGTH]


def format_feature(feature: Mapping[str, Any]) -> dict | None:
    """Turn one Photon GeoJSON feature into {name, address, label}.

    `address` is one line: street, city, "state postcode", country. `name` is
    the venue name for named places (a library, a park), else None. Features
    without a street line (towns, regions) are not street addresses: None.
    """
    props = feature.get("properties") or {}
    name = (props.get("name") or "").strip() or None
    street = (props.get("street") or "").strip()
    housenumber = (props.get("housenumber") or "").strip()
    is_street = props.get("type") in _STREET_TYPES
    if street:
        street_line = f"{housenumber} {street}".strip()
    elif is_street and name:
        street_line = name
    else:
        return None
    if is_street:
        name = None
    city = props.get("city") or props.get("town") or props.get("village") or ""
    region = " ".join(
        part for part in (props.get("state"), props.get("postcode")) if part
    )
    address = ", ".join(
        part for part in (street_line, city, region, props.get("country")) if part
    )
    return {
        "name": name,
        "address": address,
        "label": f"{name}, {address}" if name else address,
    }


# Some Python builds look for CA certificates where the host keeps none (a
# Linux path on macOS, say), which fails every HTTPS lookup. Fall back to the
# usual system bundles; an explicit SSL_CERT_FILE still wins.
_SYSTEM_CA_BUNDLES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
)
_tls_context: ssl.SSLContext | None = None


def _tls() -> ssl.SSLContext:
    global _tls_context
    if _tls_context is None:
        context = ssl.create_default_context()
        if not context.cert_store_stats()["x509_ca"] and not os.environ.get("SSL_CERT_FILE"):
            for path in _SYSTEM_CA_BUNDLES:
                if os.path.exists(path):
                    context.load_verify_locations(cafile=path)
                    break
        _tls_context = context
    return _tls_context


def _fetch_json(url: str) -> Any:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=TIMEOUT_SECONDS, context=_tls()) as response:
        return json.load(response)


class AddressSearch:
    def __init__(
        self,
        base_url: str,
        *,
        near: tuple[float, float] | None = None,
        fetch: Callable[[str], Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self.near = near
        # Swappable so tests never touch the network.
        self.fetch = fetch or _fetch_json
        self._cache: OrderedDict[str, list[dict]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def suggest(self, query: str, *, limit: int = DEFAULT_LIMIT) -> list[dict]:
        text = normalize_query(query)
        if len(text) < MIN_QUERY_LENGTH:
            return []
        key = text.lower()
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        params: list[tuple[str, Any]] = [
            ("q", text),
            # Ask for a few extra so de-duplication still fills the list.
            ("limit", limit + 3),
            ("layer", "house"),
            ("layer", "street"),
        ]
        if self.near is not None:
            params += [("lat", self.near[0]), ("lon", self.near[1])]
        try:
            body = self.fetch(f"{self.base_url}?{urlencode(params)}")
            features = body["features"]
        except Exception as exc:  # network, HTTP, JSON, or shape errors
            raise AddressSearchUnavailable(str(exc)) from exc
        items: list[dict] = []
        seen: set[str] = set()
        for feature in features:
            item = format_feature(feature) if isinstance(feature, Mapping) else None
            # Several venues often share one building; keep the top-ranked.
            if item is None or item["address"] in seen:
                continue
            seen.add(item["address"])
            items.append(item)
            if len(items) == limit:
                break
        with self._lock:
            self._cache[key] = items
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return items
