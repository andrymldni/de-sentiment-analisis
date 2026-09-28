"""Social / community connectors: Reddit, YouTube comments, Twitter-X.

Each requires a credential.  Missing credentials do not raise: the orchestrator
skips the connector and records the reason in ``raw.ingestion_runs``, so the
run log explains exactly why a source contributed nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import Any

from ..logging_config import get_logger
from ..utils.ratelimit import PoliteSleeper
from ..utils.text import contains_any
from .base import BaseConnector, ConnectorUnavailable, Document

logger = get_logger(__name__)

SUBREDDITS = ("indonesia", "finansial", "IndonesiaFinance", "indonesias")


class RedditConnector(BaseConnector):
    name = "reddit"
    platform = "reddit"
    description = "Reddit submissions and comments from Indonesian finance subreddits"
    requires_credentials = ("reddit_client_id", "reddit_client_secret")

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        try:
            import praw
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"praw not installed: {exc}") from exc

        creds = self.settings.credentials
        reddit = praw.Reddit(
            client_id=creds.reddit_client_id,
            client_secret=creds.reddit_client_secret,
            user_agent=creds.reddit_user_agent,
            check_for_async=False,
        )
        reddit.read_only = True

        emitted = 0
        query = " OR ".join(f'"{k}"' for k in self.keywords[:4])

        for subreddit_name in SUBREDDITS:
            if emitted >= limit:
                return
            try:
                subreddit = reddit.subreddit(subreddit_name)
                submissions = subreddit.search(query, sort="new", time_filter="year")
            except Exception as exc:
                logger.warning("Reddit search failed on r/%s: %s", subreddit_name, exc)
                continue

            for submission in submissions:
                if emitted >= limit:
                    return
                created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
                if created < since:
                    continue

                yield Document(
                    source_platform=self.platform,
                    source_name=f"r/{subreddit_name}",
                    external_id=f"t3_{submission.id}",
                    title=submission.title,
                    body=submission.selftext,
                    url=f"https://reddit.com{submission.permalink}",
                    author=str(submission.author) if submission.author else None,
                    published_at=created,
                    engagement={
                        "score": submission.score,
                        "comments": submission.num_comments,
                    },
                    raw_payload={"kind": "submission", "collector": "praw"},
                )
                emitted += 1

                try:
                    submission.comments.replace_more(limit=0)
                    for comment in submission.comments.list()[:25]:
                        if emitted >= limit:
                            return
                        if not contains_any(comment.body or "", self.keywords):
                            continue
                        yield Document(
                            source_platform=self.platform,
                            source_name=f"r/{subreddit_name}",
                            external_id=f"t1_{comment.id}",
                            body=comment.body,
                            url=f"https://reddit.com{submission.permalink}{comment.id}/",
                            author=str(comment.author) if comment.author else None,
                            published_at=datetime.fromtimestamp(
                                comment.created_utc, tz=timezone.utc
                            ),
                            engagement={"score": comment.score},
                            raw_payload={"kind": "comment", "collector": "praw"},
                        )
                        emitted += 1
                except Exception as exc:
                    logger.debug("Comment expansion failed: %s", exc)


class YouTubeConnector(BaseConnector):
    """Comments mentioning BRILink, read straight from the official BRI channel.

    One ``commentThreads`` call with ``allThreadsRelatedToChannelId`` covers
    every video on the channel, so a comment posted today on a months-old
    video is still found - unlike searching for recently *uploaded* videos.
    ``searchTerms`` narrows the stream server-side; the keyword check here is
    the authority, because the API's matching is looser than ours.

    Comments written by the channel itself (BRI's own replies) are dropped:
    they are the bank talking, not customer sentiment.
    """

    name = "youtube"
    platform = "youtube"
    description = "Comments mentioning BRILink on the official Bank BRI YouTube channel"
    requires_credentials = ("youtube_api_key",)

    # Safety cap per search term; the ``since`` cut-off normally stops far earlier.
    MAX_PAGES_PER_TERM = 20
    # videos.list accepts at most 50 ids per call.
    VIDEO_BATCH = 50

    @property
    def channel_ids(self) -> list[str]:
        raw = self.settings.ingestion.youtube_channel_ids
        return [c.strip() for c in raw.split(",") if c.strip()]

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"google-api-python-client not installed: {exc}") from exc

        if not self.channel_ids:
            raise ConnectorUnavailable("INGEST_YOUTUBE_CHANNEL_IDS is empty")

        youtube = build(
            "youtube",
            "v3",
            developerKey=self.settings.credentials.youtube_api_key,
            cache_discovery=False,
        )
        yield from self._fetch_with(youtube, since, limit)

    def _fetch_with(self, youtube: Any, since: datetime, limit: int) -> Iterator[Document]:
        sleeper = PoliteSleeper(self.settings.ingestion.request_delay_seconds)
        candidates: list[tuple[str, dict, str]] = []  # (comment id, snippet, channel id)
        seen: set[str] = set()
        succeeded = failed = 0

        for channel_id in self.channel_ids:
            for term in self.keywords:
                if len(candidates) >= limit:
                    break
                try:
                    for thread in self._threads(youtube, channel_id, term, since, sleeper):
                        for comment_id, snippet in _thread_comments(thread):
                            if comment_id in seen or len(candidates) >= limit:
                                continue
                            if not self._is_customer_mention(snippet, channel_id, since):
                                continue
                            seen.add(comment_id)
                            candidates.append((comment_id, snippet, channel_id))
                    succeeded += 1
                except Exception as exc:
                    failed += 1
                    logger.warning(
                        "YouTube comment search failed (channel=%s, term=%r): %s",
                        channel_id,
                        term,
                        exc,
                    )

        if failed and not succeeded:
            # Every request failed (bad key, quota, API disabled): report a failed
            # run instead of a silent "success, 0 rows".
            raise RuntimeError(f"all {failed} YouTube comment searches failed; see logs")

        titles = self._video_titles(youtube, {s.get("videoId", "") for _, s, _ in candidates})
        for comment_id, snippet, channel_id in candidates:
            video_id = snippet.get("videoId", "")
            yield Document(
                source_platform=self.platform,
                source_name=f"youtube:{video_id}",
                external_id=comment_id,
                # The video title stays out of the scored text: BRI's own
                # promotional wording would bias the comment's sentiment.
                title=None,
                body=snippet.get("textOriginal") or snippet.get("textDisplay"),
                url=f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}",
                author=snippet.get("authorDisplayName"),
                published_at=_iso(snippet.get("publishedAt")),
                engagement={"likes": snippet.get("likeCount", 0)},
                raw_payload={
                    "video_id": video_id,
                    "video_title": titles.get(video_id),
                    "channel_id": channel_id,
                    "is_reply": "." in comment_id,
                    "collector": "youtube-data-api",
                },
            )

    def _threads(
        self, youtube: Any, channel_id: str, term: str, since: datetime, sleeper: PoliteSleeper
    ) -> Iterator[dict]:
        """Channel comment threads matching ``term``, newest first, down to ``since``."""
        page_token = None
        for _ in range(self.MAX_PAGES_PER_TERM):
            sleeper.wait()
            params = {
                "part": "snippet,replies",
                "allThreadsRelatedToChannelId": channel_id,
                "searchTerms": term,
                "order": "time",
                "maxResults": 100,
                "textFormat": "plainText",
            }
            if page_token:
                params["pageToken"] = page_token
            response = youtube.commentThreads().list(**params).execute()

            for thread in response.get("items", []):
                top = thread["snippet"]["topLevelComment"]["snippet"]
                published = _iso(top.get("publishedAt"))
                if published and published < since:
                    return
                yield thread

            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def _is_customer_mention(self, snippet: dict, channel_id: str, since: datetime) -> bool:
        author_channel = (snippet.get("authorChannelId") or {}).get("value")
        if author_channel == channel_id:
            return False
        published = _iso(snippet.get("publishedAt"))
        if published and published < since:
            return False
        text = snippet.get("textOriginal") or snippet.get("textDisplay") or ""
        return contains_any(text, self.keywords)

    def _video_titles(self, youtube: Any, video_ids: set[str]) -> dict[str, str]:
        """Best-effort lookup; a missing title never blocks ingestion."""
        ids = sorted(v for v in video_ids if v)
        titles: dict[str, str] = {}
        for start in range(0, len(ids), self.VIDEO_BATCH):
            batch = ids[start : start + self.VIDEO_BATCH]
            try:
                response = youtube.videos().list(part="snippet", id=",".join(batch)).execute()
            except Exception as exc:
                logger.warning("YouTube video title lookup failed: %s", exc)
                continue
            for item in response.get("items", []):
                titles[item["id"]] = item["snippet"].get("title", "")
        return titles


def _thread_comments(thread: dict) -> Iterator[tuple[str, dict]]:
    """The top-level comment plus the replies the API embeds (up to 5).

    Reply ids are ``<thread id>.<suffix>``, so they never collide with a
    thread id and dedupe naturally on ``doc_uid``.
    """
    top = thread["snippet"]["topLevelComment"]
    yield top["id"], top["snippet"]
    for reply in (thread.get("replies") or {}).get("comments", []):
        yield reply["id"], reply["snippet"]


class TwitterConnector(BaseConnector):
    name = "twitter"
    platform = "twitter"
    description = "Recent X/Twitter posts mentioning BRILink"
    requires_credentials = ("twitter_bearer_token",)

    ENDPOINT = "https://api.twitter.com/2/tweets/search/recent"

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"httpx not installed: {exc}") from exc

        headers = {
            "Authorization": f"Bearer {self.settings.credentials.twitter_bearer_token}",
            "User-Agent": self.settings.ingestion.user_agent,
        }
        query = "(" + " OR ".join(f'"{k}"' for k in self.keywords[:3]) + ") lang:id -is:retweet"
        params = {
            "query": query,
            "max_results": 100,
            "tweet.fields": "created_at,public_metrics,author_id,lang",
            "start_time": since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

        emitted = 0
        with httpx.Client(
            timeout=self.settings.ingestion.request_timeout_seconds, headers=headers
        ) as client:
            next_token = None
            while emitted < limit:
                if next_token:
                    params["next_token"] = next_token
                try:
                    response = client.get(self.ENDPOINT, params=params)
                    response.raise_for_status()
                    payload = response.json()
                except Exception as exc:
                    logger.warning("Twitter API request failed: %s", exc)
                    return

                for tweet in payload.get("data", []):
                    if emitted >= limit:
                        return
                    metrics = tweet.get("public_metrics", {})
                    yield Document(
                        source_platform=self.platform,
                        source_name="x.com",
                        external_id=tweet["id"],
                        body=tweet.get("text"),
                        url=f"https://x.com/i/status/{tweet['id']}",
                        author=tweet.get("author_id"),
                        published_at=_iso(tweet.get("created_at")),
                        engagement={
                            "likes": metrics.get("like_count", 0),
                            "retweets": metrics.get("retweet_count", 0),
                            "replies": metrics.get("reply_count", 0),
                        },
                        raw_payload={"collector": "twitter-api-v2"},
                    )
                    emitted += 1

                next_token = payload.get("meta", {}).get("next_token")
                if not next_token:
                    return


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
