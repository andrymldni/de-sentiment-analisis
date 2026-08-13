"""Connector registry - the single place a new source gets wired in."""

from __future__ import annotations

from collections.abc import Iterable

from ..settings import Settings, get_settings
from .base import BaseConnector
from .rss_connector import RSSConnector
from .seed_connector import SeedConnector
from .social_connectors import RedditConnector, TwitterConnector, YouTubeConnector
from .store_connectors import AppStoreConnector, PlayStoreConnector

# News is sourced from RSS only (Google News keyword search + direct outlet
# feeds). A prior `news-watch` scraper connector covered 60+ outlets directly,
# but its scrapers broke whenever an outlet changed its markup, which starved
# the pipeline of news documents entirely. RSS is slower to add new outlets to
# but it's a stable, maintained protocol - every publisher already keeps its
# feed working because it also feeds their own reader apps.
CONNECTOR_CLASSES: tuple[type[BaseConnector], ...] = (
    RSSConnector,
    PlayStoreConnector,
    AppStoreConnector,
    RedditConnector,
    YouTubeConnector,
    TwitterConnector,
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
