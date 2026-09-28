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

import calendar
from collections.abc import Iterable, Iterator
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse

from ..logging_config import get_logger
from ..utils.ratelimit import PoliteSleeper
from ..utils.text import contains_any, detect_language
from .base import BaseConnector, ConnectorUnavailable, Document

logger = get_logger(__name__)

GOOGLE_NEWS_TEMPLATE = "https://news.google.com/rss/search?q={query}&hl=id&gl=ID&ceid=ID:id"

# Google News caps a query at ~100 items. Windows longer than BACKFILL_AFTER
# are fetched month by month (see ``_google_news_backfill``).
GOOGLE_NEWS_CAP = 100
BACKFILL_AFTER = timedelta(days=45)
MIN_BACKFILL_WINDOW = timedelta(days=7)

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
    "bisnis": "https://www.bisnis.com/rss",
    "kompas": "https://money.kompas.com/rss",
    "detik": "https://finance.detik.com/rss",
    "tribunnews": "https://www.tribunnews.com/rss",
    "okezone": "https://economy.okezone.com/rss",
    "merdeka": "https://www.merdeka.com/rss",
    "suara": "https://www.suara.com/rss",
    "jawa_pos": "https://www.jawapos.com/rss",
    "sindonews": "https://www.sindonews.com/rss",
    "idntimes": "https://www.idntimes.com/topic/feed",
    "beritasatu": "https://www.beritasatu.com/rss/ekonomi",
    "inilah_com": "https://www.inilah.com/rss/ekonomi",
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

        if self._now() - since > BACKFILL_AFTER:
            google_docs = self._google_news_backfill(feedparser, since, sleeper)
        else:
            google_docs = self._google_news_recent(feedparser, since, sleeper)

        for doc in google_docs:
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

    def _google_news_recent(self, feedparser, since: datetime, sleeper) -> Iterator[Document]:
        """One query per keyword - enough for an incremental window."""
        for keyword in self.keywords:
            url = GOOGLE_NEWS_TEMPLATE.format(query=quote_plus(f'"{keyword}"'))
            sleeper.wait()
            yield from self._parse_feed(feedparser, url, since, source_hint=None)

    def _google_news_backfill(self, feedparser, since: datetime, sleeper) -> Iterator[Document]:
        """Long history as date-sliced queries, newest window first.

        A Google News query returns at most ~100 items, so a single query over
        years would only ever see the latest 100. Each calendar month is asked
        for separately with ``after:``/``before:``; a month that hits the cap
        is split in half until it fits or is down to ``MIN_BACKFILL_WINDOW``.
        """
        terms = " OR ".join(f'"{k}"' for k in self.keywords)
        pending = _month_windows(since.date(), self._now().date() + timedelta(days=1))
        seen: set[str] = set()
        requests = 0

        while pending:
            start, end = pending.pop()
            query = f"({terms}) after:{start.isoformat()} before:{end.isoformat()}"
            url = GOOGLE_NEWS_TEMPLATE.format(query=quote_plus(query))
            sleeper.wait()
            requests += 1
            entries = self._load_entries(feedparser, url)

            if len(entries) >= GOOGLE_NEWS_CAP and end - start > MIN_BACKFILL_WINDOW:
                middle = start + timedelta(days=(end - start).days // 2)
                # Pushed so the newer half is popped (processed) first.
                pending.extend([(start, middle), (middle, end)])
                continue

            for doc in self._entries_to_docs(entries, url, since, source_hint=None):
                if doc.external_id in seen:
                    continue
                seen.add(doc.external_id)
                yield doc

        logger.info("Google News backfill: %d queries, %d unique articles", requests, len(seen))

    def _load_entries(self, feedparser, url: str) -> list:
        try:
            parsed = feedparser.parse(url, agent=self.settings.ingestion.user_agent)
        except Exception as exc:
            logger.warning("RSS fetch failed for %s: %s", url, exc)
            return []

        status = getattr(parsed, "status", None) or parsed.get("status")
        if isinstance(status, int) and status >= 400:
            # Throttling shows up here; an empty page would otherwise look like
            # "no news that month".
            logger.warning("RSS feed returned HTTP %s: %s", status, url)
            return []
        if getattr(parsed, "bozo", 0) and not parsed.entries:
            logger.warning("RSS feed unusable: %s", url)
            return []
        return list(parsed.entries)

    def _parse_feed(
        self,
        feedparser,
        url: str,
        since: datetime,
        source_hint: str | None,
        keyword_filter: bool = False,
    ) -> Iterable[Document]:
        entries = self._load_entries(feedparser, url)
        yield from self._entries_to_docs(entries, url, since, source_hint, keyword_filter)

    def _entries_to_docs(
        self,
        entries: list,
        url: str,
        since: datetime,
        source_hint: str | None,
        keyword_filter: bool = False,
    ) -> Iterator[Document]:
        for entry in entries:
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


def _month_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar-month ``[start, end)`` windows, oldest first."""
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor < end:
        following = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
        windows.append((cursor, min(following, end)))
        cursor = following
    return windows


def _entry_datetime(entry) -> datetime | None:
    # feedparser normalises to a UTC struct_time; timegm (not mktime, which
    # assumes local time) keeps it correct on hosts outside UTC.
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def _google_source(entry) -> str | None:
    source = entry.get("source")
    if isinstance(source, dict):
        return source.get("title")
    title = entry.get("title") or ""
    if " - " in title:
        return title.rsplit(" - ", 1)[-1]
    return None
