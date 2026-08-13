"""App-store review connectors.

App reviews are the highest-signal source in this project: they are first-hand,
high-volume, and ship with a star rating that acts as a weak label for the
sentiment engine (and, in aggregate, a sanity check on it).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from ..logging_config import get_logger
from .base import BaseConnector, ConnectorUnavailable, Document

logger = get_logger(__name__)

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

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        try:
            from google_play_scraper import Sort, reviews
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"google-play-scraper not installed: {exc}") from exc

        per_app = max(limit // max(len(PLAY_APPS), 1), 50)

        for app_alias, package in PLAY_APPS.items():
            token = None
            collected = 0
            while collected < per_app:
                try:
                    batch, token = reviews(
                        package,
                        lang="id",
                        country="id",
                        sort=Sort.NEWEST,
                        count=min(200, per_app - collected),
                        continuation_token=token,
                    )
                except Exception as exc:
                    logger.warning("Play Store fetch failed for %s: %s", package, exc)
                    break

                if not batch:
                    break

                stop = False
                for review in batch:
                    published = review.get("at")
                    if published and published.tzinfo is None:
                        published = published.replace(tzinfo=timezone.utc)
                    if published and published < since:
                        stop = True
                        break

                    yield Document(
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
                    collected += 1

                if stop or token is None:
                    break


class AppStoreConnector(BaseConnector):
    name = "appstore"
    platform = "appstore"
    description = "Apple App Store reviews (Indonesian storefront)"

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        try:
            from app_store_web_scraper import AppStoreEntry
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"app-store-web-scraper not installed: {exc}") from exc

        per_app = max(limit // max(len(APPSTORE_APPS), 1), 50)

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
                published = getattr(review, "date", None)
                if published and published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
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
