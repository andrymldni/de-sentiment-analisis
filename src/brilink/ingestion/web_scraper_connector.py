"""Direct web scraper connector — fetch BRILink articles from major outlets.

Unlike the RSS connector, this scrapes article pages directly from news
outlet websites to capture fuller article text for better sentiment analysis.

Strategy:
1. Use Google News RSS to discover article URLs.
2. Fetch full HTML and extract article body with BeautifulSoup.
3. Yield richer Document objects with full article text.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timezone
from urllib.parse import quote_plus

from ..logging_config import get_logger
from ..utils.ratelimit import PoliteSleeper
from ..utils.text import contains_any
from .base import BaseConnector, ConnectorUnavailable, Document

logger = get_logger(__name__)

GOOGLE_NEWS_URL = "https://news.google.com/rss/search?q={query}&hl=id&gl=ID&ceid=ID:id"

# CSS selectors per outlet for article body extraction.
OUTLET_SELECTORS: dict[str, list[str]] = {
    "detik.com": ["div.detail_text", "div.article_content"],
    "kompas.com": ["div#divArticleBody", "div.article__body"],
    "cnbcindonesia.com": ["div.detail_text"],
    "tempo.co": ["div#isi", "div.paywall"],
    "tribunnews.com": ["div#ArtBody", "div.txt"],
    "merdeka.com": ["span#docArticleBody", "div.article-content"],
}

GENERIC_SELECTORS = ["article", "div.article-body", "div.post-content", "div.content"]


class WebScraperConnector(BaseConnector):
    name = "web_scraper"
    platform = "news"
    description = "Direct web scraping of Indonesian news articles about BRILink"
    default_enabled = True

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        try:
            import feedparser
            import requests
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"feedparser, requests, bs4 not installed: {exc}") from exc

        sleeper = PoliteSleeper(self.settings.ingestion.request_delay_seconds)
        emitted = 0
        headers = {"User-Agent": self.settings.ingestion.user_agent}
        seen_urls: set[str] = set()

        for keyword in self.keywords:
            if emitted >= limit:
                return
            feed_url = GOOGLE_NEWS_URL.format(query=quote_plus(f'"{keyword}"'))
            sleeper.wait()
            try:
                parsed = feedparser.parse(feed_url, agent=self.settings.ingestion.user_agent)
            except Exception as exc:
                logger.warning("Web scraper feed failed for '%s': %s", keyword, exc)
                continue

            for entry in parsed.entries:
                if emitted >= limit:
                    return
                article_url = entry.get("link")
                if not article_url or article_url in seen_urls:
                    continue
                seen_urls.add(article_url)

                title = entry.get("title", "")
                outlet = title.rsplit(" - ", 1)[-1].strip().lower() if " - " in title else "unknown"
                published = _entry_datetime(entry)
                if published and published < since:
                    continue

                sleeper.wait()
                body = self._scrape_article(requests, headers, article_url)
                if not body or not contains_any(f"{title} {body}", self.keywords):
                    continue

                doc_title = title.rsplit(" - ", 1)[0] if " - " in title else title
                ext_id = hashlib.sha256(article_url.encode()).hexdigest()[:16]

                yield Document(
                    source_platform=self.platform,
                    source_name=outlet,
                    external_id=f"webscraper_{ext_id}",
                    title=doc_title,
                    body=body,
                    url=article_url,
                    author=outlet,
                    published_at=published,
                    raw_payload={"collector": "web-scraper", "outlet": outlet},
                )
                emitted += 1

    def _scrape_article(self, requests_mod, headers: dict, url: str) -> str | None:
        try:
            resp = requests_mod.get(
                url, headers=headers, timeout=self.settings.ingestion.request_timeout_seconds
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.debug("Article fetch failed %s: %s", url, exc)
            return None

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup.select("script, style, nav, header, footer, aside, .sidebar"):
            tag.decompose()

        domain = _extract_domain(url)
        for outlet_pattern, selectors in OUTLET_SELECTORS.items():
            if outlet_pattern in domain:
                for sel in selectors:
                    el = soup.select_one(sel)
                    if el:
                        return el.get_text(separator="\n", strip=True)

        for sel in GENERIC_SELECTORS:
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) > 200:
                return el.get_text(separator="\n", strip=True)

        paragraphs = soup.find_all("p")
        if paragraphs:
            text = "\n".join(p.get_text(strip=True) for p in paragraphs)
            if len(text) > 200:
                return text
        return None


def _entry_datetime(entry) -> datetime | None:
    import time as _time

    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(_time.mktime(value), tz=timezone.utc)
    return None


def _extract_domain(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc.lower()
