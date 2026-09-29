"""Selenium-based web scraper connector for full Indonesian news articles.

Strategy:
1. Use Google News RSS to discover relevant article URLs without an API key.
2. Open article pages with Selenium headless Chrome/Chromium so JavaScript-rendered
   content can be captured.
3. Extract article text using outlet-specific CSS selectors with generic fallbacks.
"""

from __future__ import annotations

import calendar
import hashlib
from collections.abc import Iterable
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse

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

                    document = self._entry_document(driver, entry, article_url, since, sleeper)
                    if document is not None:
                        yield document
                        emitted += 1

    def _entry_document(
        self,
        driver,
        entry: dict,
        article_url: str,
        since: datetime,
        sleeper: PoliteSleeper,
    ) -> Document | None:
        """Scrape one feed entry; ``None`` when it is too old, unreachable or off-topic."""
        title = entry.get("title", "")
        headline, outlet = _split_title(title)
        published = _entry_datetime(entry)
        if published and published < since:
            return None

        sleeper.wait()
        scraped = self._scrape_article(driver, article_url)
        if scraped is None:
            return None
        body, final_url = scraped
        if not contains_any(f"{title} {body}", self.keywords):
            return None

        ext_id = hashlib.sha256(article_url.encode()).hexdigest()[:16]
        return Document(
            source_platform=self.platform,
            source_name=outlet,
            external_id=f"webscraper_{ext_id}",
            title=headline,
            body=body,
            url=final_url,
            author=outlet,
            published_at=published,
            raw_payload={
                "collector": "selenium-web-scraper",
                "outlet": outlet,
                "google_news_url": article_url,
                "domain": _extract_domain(final_url),
            },
        )

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
        # News pages are ad-heavy; waiting for every tracker to finish ("normal"
        # strategy) routinely stalls the renderer past the timeout. The article
        # text is in the DOM long before that, so stop at DOMContentLoaded.
        options.page_load_strategy = "eager"
        options.add_argument("--blink-settings=imagesEnabled=false")

        # Prefer the distro Chromium + chromedriver baked into the image. Without
        # this, Selenium Manager does not recognise the `chromium` binary as
        # Chrome and tries to download Chrome for Testing at runtime.
        binary = _first_existing(CHROMIUM_BINARIES)
        if binary:
            options.binary_location = binary
        driver_path = _first_existing(CHROMEDRIVER_BINARIES)
        try:
            if driver_path:
                from selenium.webdriver.chrome.service import Service

                return webdriver.Chrome(service=Service(driver_path), options=options)
            return webdriver.Chrome(options=options)
        except Exception as exc:
            raise ConnectorUnavailable(f"could not start headless Chrome: {exc}") from exc

    def _scrape_article(self, driver, url: str) -> tuple[str, str] | None:
        """Return ``(article_text, resolved_url)`` or ``None`` when unusable.

        Google News RSS links are ``news.google.com/rss/articles/...`` redirects,
        so outlet-specific selectors must be chosen from the URL the browser
        lands on, not from the feed link.
        """
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as ec
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"selenium not installed: {exc}") from exc

        timeout = self.settings.ingestion.request_timeout_seconds
        try:
            driver.set_page_load_timeout(timeout)
            driver.get(url)
            WebDriverWait(driver, timeout).until(
                lambda d: not _is_google_host(_extract_domain(d.current_url))
            )
            WebDriverWait(driver, timeout).until(
                ec.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except Exception as exc:
            logger.debug("Selenium article fetch failed %s: %s", url, exc)
            return None

        # One slow or hostile page must cost one article, not the whole run.
        try:
            final_url = driver.current_url or url
            domain = _extract_domain(final_url)
            if _is_google_host(domain):
                # Consent wall or unresolved redirect - no article text here.
                return None

            _remove_unwanted_nodes(driver)
            for selector in _selectors_for_domain(domain):
                text = _text_from_selector(driver, selector)
                if len(text) > 200:
                    return text, final_url

            paragraph_text = _paragraph_text(driver)
            if len(paragraph_text) > 200:
                return paragraph_text, final_url
        except Exception as exc:
            logger.debug("Selenium article extraction failed %s: %s", url, exc)
        return None


CHROMIUM_BINARIES = ("/usr/bin/chromium", "/usr/bin/chromium-browser")
CHROMEDRIVER_BINARIES = ("/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver")


def _first_existing(paths: tuple[str, ...]) -> str | None:
    import os

    return next((p for p in paths if os.path.exists(p)), None)


def _is_google_host(domain: str) -> bool:
    return domain == "google.com" or domain.endswith(".google.com")


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
    # feedparser normalises to a UTC struct_time; timegm (not mktime, which
    # assumes local time) keeps it correct on hosts outside UTC.
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def _split_title(title: str) -> tuple[str, str]:
    """Split a Google News ``"Headline - Outlet"`` title into (headline, outlet)."""
    if " - " not in title:
        return title, "unknown"
    headline, outlet = title.rsplit(" - ", 1)
    return headline, outlet.strip().lower()


def _extract_domain(url: str) -> str:
    return urlparse(url).netloc.lower()
