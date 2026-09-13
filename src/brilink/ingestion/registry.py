"""Connector registry - the single place a new source gets wired in."""

from __future__ import annotations

from collections.abc import Iterable

from ..settings import Settings, get_settings
from .base import BaseConnector
from .expanded_rss_connector import ExpandedRSSConnector
from .google_maps_connector import GoogleMapsConnector
from .google_trends_connector import GoogleTrendsConnector
from .kaskus_connector import KaskusConnector
from .rss_connector import RSSConnector
from .seed_connector import SeedConnector
from .social_connectors import RedditConnector, TwitterConnector, YouTubeConnector
from .store_connectors import AppStoreConnector, PlayStoreConnector
from .web_scraper_connector import WebScraperConnector

# News is sourced from RSS (Google News keyword search + direct outlet feeds)
# and expanded RSS (broader queries + more Indonesian outlets).  A dedicated
# web scraper connector fetches full article text for richer sentiment.
CONNECTOR_CLASSES: tuple[type[BaseConnector], ...] = (
    RSSConnector,
    ExpandedRSSConnector,
    WebScraperConnector,
    PlayStoreConnector,
    AppStoreConnector,
    RedditConnector,
    YouTubeConnector,
    TwitterConnector,
    KaskusConnector,
    GoogleTrendsConnector,
    GoogleMapsConnector,
    SeedConnector,
)

REGISTRY: dict[str, type[BaseConnector]] = {cls.name: cls for cls in CONNECTOR_CLASSES}


def resolve(
    names: str | Iterable[str] | None, settings: Settings | None = None
) -> list[BaseConnector]:
    """Turn a config string into instantiated connectors.

    ``"all"`` expands to every connector marked ``default_enabled`` - the seed
    generator is excluded because it is a fallback, not a source.
    """
    settings = settings or get_settings()

    if names is None:
        names = settings.ingestion.connectors
    if isinstance(names, str):
        requested = [n.strip().lower() for n in names.split(",") if n.strip()]
    else:
        requested = [str(n).strip().lower() for n in names]

    if not requested or "all" in requested:
        selected = [cls for cls in CONNECTOR_CLASSES if cls.default_enabled]
    else:
        unknown = [n for n in requested if n not in REGISTRY]
        if unknown:
            raise ValueError(
                f"Unknown connector(s): {', '.join(unknown)}. "
                f"Available: {', '.join(sorted(REGISTRY))}"
            )
        selected = [REGISTRY[n] for n in requested]

    return [cls(settings=settings) for cls in selected]


def catalog() -> list[dict]:
    return [
        {
            "name": cls.name,
            "platform": cls.platform,
            "description": cls.description,
            "requires_credentials": list(cls.requires_credentials),
            "default_enabled": cls.default_enabled,
        }
        for cls in CONNECTOR_CLASSES
    ]
