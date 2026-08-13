"""RSS ingestion: Google News keyword search plus direct newsroom feeds.

Two complementary strategies, both restricted to **Indonesian news**:

* **Google News RSS search** - one request per keyword returns cross-outlet
  coverage with no API key, and is the most reliable keyword-driven news source
  available for Indonesian media. Results are filtered to Indonesian outlets.
* **Direct outlet feeds** - lower recall for a specific keyword, but gives us
  first-party publication timestamps and survives when Google throttles.

Only Indonesian newsrooms are accepted. Direct feeds are all Indonesian by
construction; Google News hits pass a source gate: ``.id`` ccTLD, a listed
Indonesian outlet (``detik.com``, ``tempo.co``, ...), or Indonesian-language
text from an outlet that is not a listed foreign organisation. Foreign coverage
can never leak into the corpus.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse

from ..logging_config import get_logger
from ..utils.ratelimit import PoliteSleeper
from ..utils.text import contains_any, detect_language
from .base import BaseConnector, ConnectorUnavailable, Document

logger = get_logger(__name__)

GOOGLE_NEWS_TEMPLATE = "https://news.google.com/rss/search?q={query}&hl=id&gl=ID&ceid=ID:id"

# Indonesian outlets that publish on generic/commercial TLDs (.com, .co) and
# therefore cannot be matched by the ".id" ccTLD rule below. Verified working
# feeds are listed in DIRECT_FEEDS; this set also covers Google News hits from
# outlets that have no working public feed of their own.
ID_NEWS_DOMAINS = {
    "afederasi.com",
    "antaranews.com",
    "balipost.com",
    "bekasikinian.com",
    "bisnis.com",
    "bojonegoro.com",
    "cnbcindonesia.com",
    "cnnindonesia.com",
    "detik.com",
    "gatra.com",
    "genpi.co",
    "gosulsel.com",
    "gridoto.com",
    "halloriau.com",
    "idntimes.com",
    "idxchannel.com",
    "infobanknews.com",
    "inilampung.com",
    "investing.com",
    "jatimnow.com",
    "jawapos.com",
    "joglojateng.com",
    "jpnn.com",
    "kabarbursa.com",
    "kebumenekspres.com",
    "kompas.com",
    "koranpelita.com",
    "kumparan.com",
    "liputan6.com",
    "malangtimes.com",
    "mediaindonesia.com",
    "merdeka.com",
    "metrotvnews.com",
    "okezone.com",
    "padangkita.com",
    "pikiran-rakyat.com",
    "rm.id",
    "rmol.id",
    "sindonews.com",
    "suaramerdeka.com",
    "suara.com",
    "tempo.co",
    "tradingview.com",
    "tribratakutim.com",
    "tribunnews.com",
    "viva.co.id",
}

# Foreign news organisations, including their Indonesian-language editions.
# Google News scoped to ID almost never surfaces these, but blocking them keeps
# the "Indonesian news only" guarantee airtight even when language detection on
# a short headline is inconclusive.
FOREIGN_NEWS_DOMAINS = {
    "afp.com",
    "aljazeera.com",
    "apnews.com",
    "bbc.co.uk",
    "bbc.com",
    "benarnews.org",
    "bloomberg.com",
    "channelnewsasia.com",
    "cnn.com",
    "dw.com",
    "france24.com",
    "malaymail.com",
    "nytimes.com",
    "reuters.com",
    "straitstimes.com",
    "theguardian.com",
    "voanews.com",
}

# Social and platform domains are not newsrooms. Google News occasionally
# indexes official pages, which are irrelevant to a news-sentiment corpus.
NON_NEWS_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "linktr.ee",
    "linkedin.com",
    "t.me",
    "telegram.org",
    "tiktok.com",
    "twitter.com",
    "whatsapp.com",
    "x.com",
    "youtube.com",
}

DIRECT_FEEDS: dict[str, str] = {
    "antaranews": "https://www.antaranews.com/rss/ekonomi.xml",
    "cnbcindonesia": "https://www.cnbcindonesia.com/market/rss",
    "kontan": "https://keuangan.kontan.co.id/rss",
    "katadata": "https://katadata.co.id/rss",
    "tempo": "https://rss.tempo.co/bisnis",
    "liputan6": "https://feed.liputan6.com/rss/bisnis",
    "republika": "https://republika.co.id/rss",
}


def _is_indonesian_source(domain: str, text: str) -> bool:
    """Gate every news document on its outlet being Indonesian.

    Rules, in order: any ``.id`` ccTLD domain is Indonesian; listed Indonesian
    outlets pass; listed foreign organisations are rejected; everything else
    falls back to language detection, where an English article from an
    unrecognised outlet is treated as non-Indonesian news.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return False
    if domain.endswith(".id"):
        return True
    if any(domain == root or domain.endswith("." + root) for root in ID_NEWS_DOMAINS):
        return True
    if any(domain == root or domain.endswith("." + root) for root in FOREIGN_NEWS_DOMAINS):
        return False
    if any(domain == root or domain.endswith("." + root) for root in NON_NEWS_DOMAINS):
        return False
    return detect_language(text) != "en"


def _entry_source_domain(entry, link: str | None) -> str:
    source = entry.get("source")
    href = source.get("href") if isinstance(source, dict) else None
    return urlparse(href or link or "").netloc.lower()


class RSSConnector(BaseConnector):
    name = "rss"
    platform = "news"
    description = "Google News keyword RSS + direct Indonesian newsroom feeds"

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        try:
            import feedparser
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"feedparser not installed: {exc}") from exc

        sleeper = PoliteSleeper(self.settings.ingestion.request_delay_seconds)
        emitted = 0

        for keyword in self.keywords:
            if emitted >= limit:
                return
            url = GOOGLE_NEWS_TEMPLATE.format(query=quote_plus(f'"{keyword}"'))
            sleeper.wait()
            for doc in self._parse_feed(feedparser, url, since, source_hint=None):
                yield doc
                emitted += 1
                if emitted >= limit:
                    return

        for outlet, url in DIRECT_FEEDS.items():
            if emitted >= limit:
                return
            sleeper.wait()
            for doc in self._parse_feed(
                feedparser, url, since, source_hint=outlet, keyword_filter=True
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
            logger.warning("RSS fetch failed for %s: %s", url, exc)
            return

        if getattr(parsed, "bozo", 0) and not parsed.entries:
            logger.warning("RSS feed unusable: %s", url)
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

            domain = _entry_source_domain(entry, link)
            if not _is_indonesian_source(domain, blob):
                logger.info(
                    "Dropping non-Indonesian news source: %s",
                    domain or (source_hint or "unknown"),
                )
                continue

            published = _entry_datetime(entry)
            if published and published < since:
                continue

            source = source_hint or _google_source(entry) or "google-news"
            yield Document(
                source_platform=self.platform,
                source_name=source.lower(),
                external_id=link,
                title=title,
                body=summary,
                url=link,
                author=entry.get("author"),
                published_at=published,
                raw_payload={"feed": url, "collector": "rss"},
            )


def _entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(time.mktime(value), tz=timezone.utc)
    return None


def _google_source(entry) -> str | None:
    source = entry.get("source")
    if isinstance(source, dict):
        return source.get("title")
    title = entry.get("title") or ""
    if " - " in title:
        return title.rsplit(" - ", 1)[-1]
    return None
