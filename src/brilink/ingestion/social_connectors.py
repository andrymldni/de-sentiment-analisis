"""Social / community connectors: Reddit, YouTube comments, Twitter-X.

Each requires a credential.  Missing credentials do not raise: the orchestrator
skips the connector and records the reason in ``raw.ingestion_runs``, so the
run log explains exactly why a source contributed nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from ..logging_config import get_logger
from ..utils.ratelimit import PoliteSleeper
from ..utils.text import contains_any
from .base import BaseConnector, ConnectorUnavailable, Document

logger = get_logger(__name__)

SUBREDDITS = ("indonesia", "finansial", "IndonesiaFinance", "indonesias")
YOUTUBE_SEARCH_TERMS = ("agen BRILink", "BRILink penipuan", "cara daftar BRILink")


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
    name = "youtube"
    platform = "youtube"
    description = "Comment threads on Indonesian BRILink videos"
    requires_credentials = ("youtube_api_key",)

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"google-api-python-client not installed: {exc}") from exc

        youtube = build(
            "youtube",
            "v3",
            developerKey=self.settings.credentials.youtube_api_key,
            cache_discovery=False,
        )
        sleeper = PoliteSleeper(self.settings.ingestion.request_delay_seconds)
        emitted = 0

        for term in YOUTUBE_SEARCH_TERMS:
            if emitted >= limit:
                return
            sleeper.wait()
            try:
                search = (
                    youtube.search()
                    .list(
                        q=term,
                        part="id,snippet",
                        type="video",
                        maxResults=10,
                        relevanceLanguage="id",
                        regionCode="ID",
                        publishedAfter=since.astimezone(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    )
                    .execute()
                )
            except Exception as exc:
                logger.warning("YouTube search failed for %r: %s", term, exc)
                continue

            for item in search.get("items", []):
                video_id = item["id"]["videoId"]
                video_title = item["snippet"]["title"]
                if emitted >= limit:
                    return
                sleeper.wait()
                try:
                    threads = (
                        youtube.commentThreads()
                        .list(
                            videoId=video_id,
                            part="snippet",
                            maxResults=100,
                            textFormat="plainText",
                            order="time",
                        )
                        .execute()
                    )
                except Exception as exc:
                    logger.debug("Comments disabled for %s: %s", video_id, exc)
                    continue

                for thread in threads.get("items", []):
                    if emitted >= limit:
                        return
                    snippet = thread["snippet"]["topLevelComment"]["snippet"]
                    text = snippet.get("textDisplay", "")
                    if not contains_any(text, self.keywords):
                        continue
                    yield Document(
                        source_platform=self.platform,
                        source_name=f"youtube:{video_id}",
                        external_id=thread["id"],
                        title=video_title,
                        body=text,
                        url=f"https://www.youtube.com/watch?v={video_id}",
                        author=snippet.get("authorDisplayName"),
                        published_at=_iso(snippet.get("publishedAt")),
                        engagement={"likes": snippet.get("likeCount", 0)},
                        raw_payload={"video_id": video_id, "collector": "youtube-data-api"},
                    )
                    emitted += 1


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
