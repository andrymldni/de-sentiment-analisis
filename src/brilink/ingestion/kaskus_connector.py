"""Kaskus forum connector — scrape BRILink-related discussion threads.

Kaskus is the largest Indonesian internet forum and a natural place where
users discuss financial services like BRILink.  Threads are fetched via the
public web interface (no API key required) and parsed with BeautifulSoup.

Strategy
--------
1. Search Kaskus for each keyword using the public search URL.
2. For each result, fetch the thread page and extract the opening post plus
   the first page of replies.
3. Only threads with recent activity (within ``since``) are yielded.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin

from ..logging_config import get_logger
from ..utils.ratelimit import PoliteSleeper
from ..utils.text import contains_any
from .base import BaseConnector, ConnectorUnavailable, Document

logger = get_logger(__name__)

KASKUS_SEARCH_URL = "https://www.kaskus.co.id/search?q={query}&sort=new"
KASKUS_BASE = "https://www.kaskus.co.id"


class KaskusConnector(BaseConnector):
    name = "kaskus"
    platform = "kaskus"
    description = "Kaskus forum threads and replies mentioning BRILink"
    default_enabled = True

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        try:
            import requests
            from bs4 import BeautifulSoup
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"requests and beautifulsoup4 not installed: {exc}") from exc

        sleeper = PoliteSleeper(self.settings.ingestion.request_delay_seconds)
        emitted = 0
        headers = {"User-Agent": self.settings.ingestion.user_agent}

        for keyword in self.keywords:
            if emitted >= limit:
                return
            search_url = KASKUS_SEARCH_URL.format(query=quote_plus(keyword))
            sleeper.wait()
            try:
                resp = requests.get(
                    search_url,
                    headers=headers,
                    timeout=self.settings.ingestion.request_timeout_seconds,
                )
                resp.raise_for_status()
            except Exception as exc:
                logger.warning("Kaskus search failed for '%s': %s", keyword, exc)
                continue

            soup = BeautifulSoup(resp.text, "lxml")
            thread_links = _extract_thread_links(soup)

            for thread_url in thread_links:
                if emitted >= limit:
                    return
                sleeper.wait()
                doc = self._fetch_thread(requests, headers, thread_url, since)
                if doc is not None:
                    yield doc
                    emitted += 1

    def _fetch_thread(
        self,
        requests_mod,
        headers: dict,
        url: str,
        since: datetime,
    ) -> Document | None:
        from bs4 import BeautifulSoup  # noqa: PLC0415 - imported in the caller scope otherwise

        try:
            resp = requests_mod.get(
                url,
                headers=headers,
                timeout=self.settings.ingestion.request_timeout_seconds,
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Kaskus thread fetch failed %s: %s", url, exc)
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        thread_data = _parse_thread_page(soup, url)
        if thread_data is None:
            return None

        title, body, author, published, replies_text = thread_data
        if published and published < since:
            return None

        combined = f"{title or ''} {body or ''} {replies_text}"
        if not contains_any(combined, self.keywords):
            return None

        return Document(
            source_platform=self.platform,
            source_name="kaskus",
            external_id=url,
            title=title,
            body=body,
            url=url,
            author=author,
            published_at=published,
            engagement={"replies_fetched": 1 if replies_text else 0},
            raw_payload={"collector": "kaskus-scraper"},
        )


def _extract_thread_links(soup) -> list[str]:
    """Pull thread URLs from a Kaskus search results page."""
    links: list[str] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if "/thread/" in href or "/read/" in href:
            full = href if href.startswith("http") else urljoin(KASKUS_BASE, href)
            if full not in links:
                links.append(full)
    return links[:20]


def _parse_thread_page(soup, url: str) -> tuple | None:
    """Extract title, body, author, date, and reply text from a thread page."""
    title_el = soup.select_one("h1, .title, [data-testid='thread-title']")
    title = title_el.get_text(strip=True) if title_el else None

    body_el = soup.select_one(
        ".post-content, .message-content, [data-testid='post-body'], .content"
    )
    body = body_el.get_text(separator="\n", strip=True) if body_el else None

    if not title and not body:
        return None

    author_el = soup.select_one(".username, .author, [data-testid='username']")
    author = author_el.get_text(strip=True) if author_el else None

    time_el = soup.select_one("time, .date, [datetime]")
    published = None
    if time_el:
        dt_str = time_el.get("datetime") or time_el.get_text(strip=True)
        published = _parse_date(dt_str)

    reply_parts: list[str] = []
    for reply_el in soup.select(".reply-content, .post-reply, [data-testid='reply-body']"):
        text = reply_el.get_text(separator=" ", strip=True)
        if text:
            reply_parts.append(text)

    return title, body, author, published, " ".join(reply_parts)


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    from dateutil import parser as dtparser

    try:
        dt = dtparser.parse(text, dayfirst=False)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, OverflowError):
        return None
