"""Google Maps reviews connector — extract reviews from BRILink locations.

Google Maps reviews are a rich, untapped source of BRILink sentiment data.
Users leave detailed reviews about their experience with agents, including
service quality, wait times, and transaction issues.

Strategy
--------
1. Use the Google Places Text Search API (requires a free API key from
   Google Cloud Console) to find BRILink agent locations.
2. For each place, use the Place Details API to retrieve reviews.
3. Falls back to web-scraping Google Maps search results when no API key
   is available (limited but functional).

The ``googlemaps`` package is used for the official API path; ``requests``
+ ``BeautifulSoup`` for the scraping fallback.
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

SEARCH_QUERIES = [
    "agen BRILink",
    "BRILink BRI",
    "agen BRI laku pandai",
    "BRILink terdekat",
]

# Google Maps search URL for scraping fallback.
GMAPS_SEARCH_URL = "https://www.google.com/maps/search/{query}/@-6.2088,106.8456,12z"


class GoogleMapsConnector(BaseConnector):
    name = "google_maps"
    platform = "google_maps"
    description = "Google Maps reviews for BRILink agent locations in Indonesia"
    requires_credentials = ("google_maps_api_key",)
    default_enabled = False  # Requires API key; enabled only when configured.

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        creds = self.settings.credentials
        if getattr(creds, "google_maps_api_key", None):
            yield from self._fetch_via_api(since, limit)
        else:
            yield from self._fetch_via_scraping(since, limit)

    def _fetch_via_api(self, since: datetime, limit: int) -> Iterable[Document]:
        """Use Google Places API (official, requires API key)."""
        try:
            import googlemaps
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"googlemaps not installed: {exc}") from exc

        gmaps = googlemaps.Client(key=self.settings.credentials.google_maps_api_key)
        sleeper = PoliteSleeper(self.settings.ingestion.request_delay_seconds)
        emitted = 0

        for query in SEARCH_QUERIES:
            if emitted >= limit:
                return
            sleeper.wait()
            try:
                results = gmaps.places_nearby(
                    location=(-6.2088, 106.8456),  # Jakarta center
                    radius=50000,
                    keyword=query,
                    language="id",
                    type="finance",
                )
            except Exception as exc:
                logger.warning("Google Maps nearby search failed for '%s': %s", query, exc)
                continue

            for place in results.get("results", []):
                if emitted >= limit:
                    return
                place_id = place.get("place_id")
                if not place_id:
                    continue

                sleeper.wait()
                try:
                    details = gmaps.place(
                        place_id=place_id,
                        fields=["name", "reviews", "formatted_address", "rating"],
                        language="id",
                    )
                except Exception as exc:
                    logger.warning("Place details failed %s: %s", place_id, exc)
                    continue

                result = details.get("result", {})
                place_name = result.get("name", "Unknown")
                place_addr = result.get("formatted_address", "")

                for review in result.get("reviews", []):
                    published = _parse_ts(review.get("time"))
                    if published and published < since:
                        continue

                    text = review.get("text", "")
                    if text and not contains_any(text, self.keywords):
                        continue

                    author = review.get("author_name", "anonymous")
                    ext_id = f"gmaps_{place_id}_{review.get('time', 0)}"
                    rating_val = review.get("rating")

                    yield Document(
                        source_platform=self.platform,
                        source_name="google_maps_api",
                        external_id=ext_id,
                        title=f"Review of {place_name}",
                        body=text,
                        url=f"https://maps.google.com/maps/place/?place_id={place_id}",
                        author=author,
                        rating=rating_val,
                        published_at=published,
                        engagement={"place_rating": result.get("rating")},
                        raw_payload={
                            "collector": "google-maps-api",
                            "place_id": place_id,
                            "place_name": place_name,
                            "place_address": place_addr,
                        },
                    )
                    emitted += 1
                    if emitted >= limit:
                        return

    def _fetch_via_scraping(self, since: datetime, limit: int) -> Iterable[Document]:
        """Fallback: scrape Google Maps search results (no API key needed).

        This is limited and fragile, but captures some reviews when the API
        key is not configured.
        """
        try:
            import requests
            from bs4 import BeautifulSoup
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"requests, beautifulsoup4 not installed: {exc}") from exc

        sleeper = PoliteSleeper(self.settings.ingestion.request_delay_seconds)
        emitted = 0
        headers = {"User-Agent": self.settings.ingestion.user_agent}

        for query in SEARCH_QUERIES:
            if emitted >= limit:
                return
            url = GMAPS_SEARCH_URL.format(query=quote_plus(query))
            sleeper.wait()
            try:
                resp = requests.get(
                    url, headers=headers, timeout=self.settings.ingestion.request_timeout_seconds
                )
                resp.raise_for_status()
            except Exception as exc:
                logger.warning("Google Maps scraping failed for '%s': %s", query, exc)
                continue

            soup = BeautifulSoup(resp.text, "lxml")
            # Google Maps search results are JS-rendered, so HTML scraping is
            # limited. We extract what we can from meta tags and structured data.
            for script_tag in soup.select('script[type="application/ld+json"]'):
                try:
                    import json

                    data = json.loads(script_tag.string or "")
                    if isinstance(data, dict) and data.get("@type") == "LocalBusiness":
                        name = data.get("name", "")
                        desc = data.get("description", "")
                        if desc and contains_any(desc, self.keywords):
                            dt_now = datetime.now(timezone.utc)
                            yield Document(
                                source_platform=self.platform,
                                source_name="google_maps_scrape",
                                external_id=f"gmaps_scrape_{hashlib_md5(name)}",
                                title=f"BRILink location: {name}",
                                body=desc,
                                url=url,
                                author="google_maps",
                                published_at=dt_now,
                                raw_payload={"collector": "google-maps-scrape"},
                            )
                            emitted += 1
                            if emitted >= limit:
                                return
                except (json.JSONDecodeError, TypeError):
                    continue

        if emitted == 0:
            logger.info(
                "Google Maps scraping yielded no results (expected for JS-rendered pages). "
                "Configure GOOGLE_MAPS_API_KEY for full access."
            )


def _parse_ts(ts_value) -> datetime | None:
    if not ts_value:
        return None
    try:
        dt = datetime.fromtimestamp(int(ts_value), tz=timezone.utc)
        return dt
    except (ValueError, OSError):
        return None


def hashlib_md5(text: str) -> str:
    import hashlib

    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
