"""App-store review connectors.

App reviews are the highest-signal source in this project: they are first-hand,
high-volume, and ship with a star rating that acts as a weak label for the
sentiment engine (and, in aggregate, a sanity check on it).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from typing import Any

from ..logging_config import get_logger
from .base import BaseConnector, ConnectorUnavailable, Document

logger = get_logger(__name__)

# Floor per app, so a small global limit still yields a useful sample of each.
MIN_REVIEWS_PER_APP = 50
# google-play-scraper returns at most this many reviews per request.
PLAY_PAGE_SIZE = 200

# BRI's consumer app and the agent-facing app. Both surface BRILink experience.
PLAY_APPS: dict[str, str] = {
    "brimo": "id.co.bri.brimo",
    "brilink_mobile": "id.co.bri.brilinkmobile",
}
APPSTORE_APPS: dict[str, int] = {
    "brimo": 1493288523,
}


class PlayStoreConnector(BaseConnector):
    name = "playstore"
    platform = "playstore"
    description = "Google Play reviews for BRImo and BRILink Mobile"

    @property
    def apps(self) -> dict[str, str]:
        """Apps selected by ``INGEST_PLAYSTORE_APPS`` (aliases from ``PLAY_APPS``)."""
        wanted = {
            a.strip().lower()
            for a in self.settings.ingestion.playstore_apps.split(",")
            if a.strip()
        }
        return {alias: pkg for alias, pkg in PLAY_APPS.items() if alias in wanted}

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        try:
            from google_play_scraper import Sort, reviews
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"google-play-scraper not installed: {exc}") from exc

        apps = self.apps
        if not apps:
            raise ConnectorUnavailable(
                f"INGEST_PLAYSTORE_APPS matches no known app (known: {', '.join(PLAY_APPS)})"
            )
        per_app = max(limit // len(apps), MIN_REVIEWS_PER_APP)
        for app_alias, package in apps.items():
            yield from self._app_reviews(reviews, Sort.NEWEST, app_alias, package, since, per_app)

    def _app_reviews(
        self,
        reviews: Callable[..., Any],
        newest: Any,
        app_alias: str,
        package: str,
        since: datetime,
        budget: int,
    ) -> Iterator[Document]:
        """Page one app's reviews newest-first until ``since``, ``budget`` or the last page."""
        token = None
        collected = 0
        while collected < budget:
            try:
                batch, token = reviews(
                    package,
                    lang="id",
                    country="id",
                    sort=newest,
                    count=min(PLAY_PAGE_SIZE, budget - collected),
                    continuation_token=token,
                )
            except Exception as exc:
                logger.warning("Play Store fetch failed for %s: %s", package, exc)
                return

            for review in batch:
                published = _as_utc(review.get("at"))
                if published and published < since:
                    return
                yield self._review_document(review, app_alias, package, published)
                collected += 1

            if not batch or token is None:
                return

    def _review_document(
        self, review: dict, app_alias: str, package: str, published: datetime | None
    ) -> Document:
        return Document(
            source_platform=self.platform,
            source_name=app_alias,
            external_id=str(review.get("reviewId")),
            title=None,
            body=review.get("content"),
            url=f"https://play.google.com/store/apps/details?id={package}",
            author=review.get("userName"),
            rating=review.get("score"),
            published_at=published,
            engagement={"thumbs_up": review.get("thumbsUpCount", 0)},
            raw_payload={
                "package": package,
                "app_version": review.get("reviewCreatedVersion"),
                "reply": bool(review.get("replyContent")),
                "collector": "google-play-scraper",
            },
        )


class AppStoreConnector(BaseConnector):
    name = "appstore"
    platform = "appstore"
    description = "Apple App Store reviews (Indonesian storefront)"

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        try:
            from app_store_web_scraper import AppStoreEntry
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"app-store-web-scraper not installed: {exc}") from exc

        per_app = max(limit // max(len(APPSTORE_APPS), 1), MIN_REVIEWS_PER_APP)

        for alias, app_id in APPSTORE_APPS.items():
            try:
                entry = AppStoreEntry(app_id=app_id, country="id")
                iterator = entry.reviews()
            except Exception as exc:
                logger.warning("App Store fetch failed for %s: %s", alias, exc)
                continue

            for index, review in enumerate(iterator):
                if index >= per_app:
                    break
                published = _as_utc(getattr(review, "date", None))
                if published and published < since:
                    break

                yield Document(
                    source_platform=self.platform,
                    source_name=alias,
                    external_id=str(getattr(review, "id", index)),
                    title=getattr(review, "title", None),
                    body=getattr(review, "content", None),
                    url=f"https://apps.apple.com/id/app/id{app_id}",
                    author=getattr(review, "user_name", None),
                    rating=getattr(review, "rating", None),
                    published_at=published,
                    raw_payload={"app_id": app_id, "collector": "app-store-web-scraper"},
                )


def _as_utc(value: datetime | None) -> datetime | None:
    """Scrapers return naive UTC datetimes; make them comparable with `since`."""
    if value and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
