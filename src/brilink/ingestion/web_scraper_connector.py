"""Selenium-based web scraper connector for full Indonesian news articles.

Strategy:
1. Use Google News RSS to discover relevant article URLs without an API key.
2. Open article pages with Selenium headless Chrome/Chromium so JavaScript-rendered
   content can be captured.
3. Extract article text using outlet-specific CSS selectors with generic fallbacks.
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

GENERIC_SELECTORS = ["article", "main", "div.article-body", "div.post-content", "div.content"]
BLOCKED_SELECTORS = "script, style, nav, header, footer, aside, .sidebar, .ads, .advertisement"


class WebScraperConnector(BaseConnector):
    name = "web_scraper"
    platform = "news"
    description = "Selenium scraping of Indonesian news articles about BRILink"
    default_enabled = True

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        try:
            import feedparser
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"feedparser not installed: {exc}") from exc

        sleeper = PoliteSleeper(self.settings.ingestion.request_delay_seconds)
        emitted = 0
        seen_urls: set[str] = set()

        with self._browser() as driver:
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
                    body = self._scrape_article(driver, article_url)
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
                        raw_payload={"collector": "selenium-web-scraper", "outlet": outlet},
                    )
                    emitted += 1

    def _browser(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"selenium not installed: {exc}") from exc

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--window-size=1366,768")
        options.add_argument(f"--user-agent={self.settings.ingestion.user_agent}")
        return webdriver.Chrome(options=options)

    def _scrape_article(self, driver, url: str) -> str | None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as ec
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"selenium not installed: {exc}") from exc

        try:
            driver.set_page_load_timeout(self.settings.ingestion.request_timeout_seconds)
            driver.get(url)
            WebDriverWait(driver, self.settings.ingestion.request_timeout_seconds).until(
                ec.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except Exception as exc:
            logger.debug("Selenium article fetch failed %s: %s", url, exc)
            return None

        _remove_unwanted_nodes(driver)
        for selector in _selectors_for_domain(_extract_domain(url)):
            text = _text_from_selector(driver, selector)
            if len(text) > 200:
                return text

        paragraph_text = _paragraph_text(driver)
        if len(paragraph_text) > 200:
            return paragraph_text
        return None


def _selectors_for_domain(domain: str) -> list[str]:
    selectors: list[str] = []
    for outlet_pattern, outlet_selectors in OUTLET_SELECTORS.items():
        if outlet_pattern in domain:
            selectors.extend(outlet_selectors)
            break
    selectors.extend(GENERIC_SELECTORS)
    return selectors


def _remove_unwanted_nodes(driver) -> None:
    driver.execute_script(
        "document.querySelectorAll(arguments[0]).forEach(function(el) { el.remove(); });",
        BLOCKED_SELECTORS,
    )


def _text_from_selector(driver, selector: str) -> str:
    elements = driver.find_elements("css selector", selector)
    return "\n".join(el.text.strip() for el in elements if el.text and el.text.strip()).strip()


def _paragraph_text(driver) -> str:
    paragraphs = driver.find_elements("tag name", "p")
    return "\n".join(p.text.strip() for p in paragraphs if p.text and p.text.strip()).strip()


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
