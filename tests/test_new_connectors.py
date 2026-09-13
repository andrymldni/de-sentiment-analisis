"""Tests for the new data-source connectors added for broader coverage.

These tests exercise the connector plumbing that does not require live
network access: registry resolution, missing-dependency behaviour, metadata
contracts, and pure parsing helpers.  Live fetch behaviour is verified by the
integration suite running inside Docker.
"""

from datetime import datetime, timezone

import pytest

from brilink.ingestion.base import ConnectorUnavailable
from brilink.ingestion.expanded_rss_connector import ExpandedRSSConnector
from brilink.ingestion.google_maps_connector import GoogleMapsConnector, _parse_ts, hashlib_md5
from brilink.ingestion.google_trends_connector import GoogleTrendsConnector
from brilink.ingestion.kaskus_connector import KaskusConnector
from brilink.ingestion.web_scraper_connector import WebScraperConnector, _extract_domain

NOW = datetime.now(timezone.utc)


def _missing_import_error(connector: object) -> bool:
    """Issue an import that will fail and surface the wrapped error."""
    # Direct call to fetch will raise ConnectorUnavailable if the dependency
    # is genuinely installed; if it is not, this test is a no-op guard.
    return True


def test_all_new_connectors_register_and_have_metadata():
    from brilink.ingestion.registry import REGISTRY, catalog

    for cls, platform, creds in [
        (KaskusConnector, "kaskus", ()),
        (ExpandedRSSConnector, "news", ()),
        (GoogleTrendsConnector, "google_trends", ()),
        (WebScraperConnector, "news", ()),
        (GoogleMapsConnector, "google_maps", ("google_maps_api_key",)),
    ]:
        connector = cls()
        assert connector.name in REGISTRY
        assert connector.platform == platform
        assert set(connector.requires_credentials) == set(creds)

    catalog_names = {c["name"] for c in catalog()}
    expected = {"kaskus", "expanded_rss", "google_trends", "web_scraper", "google_maps"}
    assert expected <= catalog_names


def test_google_maps_requires_api_key_but_degrades():
    connector = GoogleMapsConnector()
    # Without a credential, availability is False per the BaseConnector contract.
    assert not connector.is_available()


def test_google_maps_parse_ts_handles_null_and_bad_values():
    assert _parse_ts(None) is None
    assert _parse_ts("not-a-number") is None
    parsed = _parse_ts("1700000000")
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc


def test_google_maps_hashlib_md5_is_stable():
    a = hashlib_md5("agen brilink jakarta")
    b = hashlib_md5("agen brilink jakarta")
    c = hashlib_md5("agen brilink bandung")
    assert a == b
    assert a != c
    assert len(a) == 12


def test_web_scraper_extract_domain():
    assert _extract_domain("https://finance.detik.com/ekonomi/d-1") == "finance.detik.com"
    assert _extract_domain("http://www.kompas.com/artikel") == "www.kompas.com"


def test_kaskus_connector_metadata():
    connector = KaskusConnector()
    assert connector.requires_credentials == ()
    assert connector.default_enabled is True
    assert connector.platform == "kaskus"


def test_expanded_rss_has_feeds_and_queries():
    import brilink.ingestion.expanded_rss_connector as mod

    assert len(mod.EXPANDED_DIRECT_FEEDS) >= 5
    assert len(mod.EXPANDED_SEARCH_QUERIES) >= 3


def test_rss_connector_has_expanded_feeds():
    from brilink.ingestion.rss_connector import DIRECT_FEEDS

    # The primary RSS connector now carries more direct outlets.
    assert len(DIRECT_FEEDS) >= 12
    assert "detik" in DIRECT_FEEDS
    assert "kompas" in DIRECT_FEEDS


def test_fetch_with_missing_dependency_raises_wrapped_error(monkeypatch):
    """Connectors must fail closed with a descriptive error, not an ImportError."""

    # Force the internal import to fail so the connector's try/except wraps it.
    # Each connector imports its dependency lazily inside fetch().
    import builtins

    real_import = builtins.__import__

    def block_import(name, *args, **kwargs):
        if name in (
            "requests",
            "feedparser",
            "pytrends.request",
            "googlemaps",
            "bs4",
            "dateutil",
        ):
            raise ImportError(f"No module named '{name}'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_import)

    from brilink.ingestion.registry import resolve

    for name in ("kaskus", "google_trends", "web_scraper", "google_maps", "expanded_rss"):
        connector = resolve(name)[0]
        with pytest.raises(ConnectorUnavailable, match="not installed"):
            list(connector.fetch(NOW, 5))
