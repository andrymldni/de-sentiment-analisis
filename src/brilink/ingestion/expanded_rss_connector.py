"""Expanded RSS connector — additional Indonesian news feeds and keywords.

This connector supplements the primary ``rss_connector.py`` by adding:

* **More direct outlet feeds** — regional newspapers and financial portals that
  are not in the primary feed list but regularly cover financial inclusion and
  branchless banking topics.
* **Google News RSS with broader queries** — searches like "BRILink agen",
  "laku pandai", "bankir keliling" to capture coverage that doesn't use the
  exact brand name.

The connector uses the same ``Document`` envelope and is registered alongside
the primary RSS connector.  Having a separate connector keeps the primary one
lean and makes it easy to tune feed lists independently.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from urllib.parse import quote_plus

from ..logging_config import get_logger
from ..utils.ratelimit import PoliteSleeper
from ..utils.text import contains_any
from .base import BaseConnector, ConnectorUnavailable, Document

logger = get_logger(__name__)

GOOGLE_NEWS_TEMPLATE = "https://news.google.com/rss/search?q={query}&hl=id&gl=ID&ceid=ID:id"

# Additional direct outlet feeds not in the primary rss_connector.
EXPANDED_DIRECT_FEEDS: dict[str, str] = {
    "bisnis.com": "https://www.bisnis.com/rss",
    "kumparan": "https://cdn-amp.kumparan.com/@kumparanbisnis/feed",
    "kontan": "https://www.kontan.co.id/rss/surat-kabar",
    "republika": "https://www.republika.co.id/rss",
    "sindonews": "https://www.sindonews.com/rss",
    "jakartaglobe": "https://www.jakartaglobe.id/rss",
    "thejakartapost": "https://www.thejakartapost.com/feed",
    "mediaindonesia": "https://mediaindonesia.com/rss",
    "pikiranrakyat": "https://www.pikiran-rakyat.com/rss",
    "suryamalang": "https://surabaya.tribunnews.com/rss",
    "tribunjualbeli": "https://jualbeli.tribunnews.com/rss",
    "suarasurabaya": "https://suarasurabaya.net/feed/",
    "indopos": "https://www.indopos.co.id/rss",
    "koranjakarta": "https://koranjakarta.com/feed/",
    "beritasatu": "https://www.beritasatu.com/rss",
}

# Broader search queries that may match BRILink coverage without
# using the exact keyword "BRILink".
EXPANDED_SEARCH_QUERIES = [
    "agen BRILink",
    "laku pandai BRI",
    "branchless banking BRI",
    "bankir keliling",
    "inklusi keuangan desa",
    "BRILink agen BRImo",
]


class ExpandedRSSConnector(BaseConnector):
    name = "expanded_rss"
    platform = "news"
    description = "Extended Indonesian news RSS feeds and broader search queries"
    default_enabled = True

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        try:
            import feedparser
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"feedparser not installed: {exc}") from exc

        sleeper = PoliteSleeper(self.settings.ingestion.request_delay_seconds)
        emitted = 0

        # Phase 1: Google News with broader queries
        for query in EXPANDED_SEARCH_QUERIES:
            if emitted >= limit:
                return
            url = GOOGLE_NEWS_TEMPLATE.format(query=quote_plus(f'"{query}"'))
            sleeper.wait()
            for doc in self._parse_feed(feedparser, url, since, source_hint=f"google-news:{query}"):
                yield doc
                emitted += 1
                if emitted >= limit:
                    return

        # Phase 2: Direct expanded outlet feeds (keyword-filtered)
        for outlet, feed_url in EXPANDED_DIRECT_FEEDS.items():
            if emitted >= limit:
                return
            sleeper.wait()
            for doc in self._parse_feed(
                feedparser, feed_url, since, source_hint=outlet, keyword_filter=True
            ):
                yield doc
                emitted += 1
                if emitted >= limit:
                    return

    def _parse_feed(
        self,
        feedparser,
        url: str,
        since: datetime,
        source_hint: str | None,
        keyword_filter: bool = False,
    ) -> Iterable[Document]:
        try:
            parsed = feedparser.parse(url, agent=self.settings.ingestion.user_agent)
        except Exception as exc:
            logger.warning("Expanded RSS fetch failed for %s: %s", url, exc)
            return

        if getattr(parsed, "bozo", 0) and not parsed.entries:
            logger.warning("Expanded RSS feed unusable: %s", url)
            return

        for entry in parsed.entries:
            title = entry.get("title")
            summary = entry.get("summary") or entry.get("description")
            link = entry.get("link")
            if not link or not title:
                continue

            blob = f"{title} {summary or ''}"
            if keyword_filter and not contains_any(blob, self.keywords):
                continue

            published = _entry_datetime(entry)
            if published and published < since:
                continue

            source = source_hint or "expanded-rss"
            yield Document(
                source_platform=self.platform,
                source_name=source.lower(),
                external_id=link,
                title=title,
                body=summary,
                url=link,
                author=entry.get("author"),
                published_at=published,
                raw_payload={"feed": url, "collector": "expanded-rss"},
            )


def _entry_datetime(entry) -> datetime | None:
    import time as _time

    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(_time.mktime(value), tz=timezone.utc)
    return None
