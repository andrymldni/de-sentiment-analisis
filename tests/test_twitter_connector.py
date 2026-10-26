"""X/Twitter connector: recent search over API v2.

HTTP is served by ``httpx.MockTransport``, so these tests need no network and
no bearer token.
"""

from datetime import datetime, timedelta, timezone

import pytest

httpx = pytest.importorskip("httpx")

from brilink.ingestion.base import ConnectorUnavailable  # noqa: E402
from brilink.ingestion.social_connectors import TwitterConnector  # noqa: E402
from brilink.settings import IngestionSettings, Settings  # noqa: E402

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


class FixedClockTwitter(TwitterConnector):
    def _now(self) -> datetime:
        return NOW


def make_connector():
    return FixedClockTwitter(Settings(ingestion=IngestionSettings(keywords="brilink,agen brilink")))


def tweet(tid, text="agen brilink ramah", when="2026-09-28T08:00:00.000Z"):
    return {
        "id": tid,
        "text": text,
        "author_id": "42",
        "created_at": when,
        "public_metrics": {"like_count": 5, "retweet_count": 1, "reply_count": 2},
    }


def serve(handler):
    requests = []

    def record(request):
        requests.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(record)), requests


def collect(handler, since=NOW - timedelta(days=1), limit=100):
    client, requests = serve(handler)
    docs = list(make_connector()._fetch_with(client, since, limit))
    return docs, requests


# ----------------------------------------------------------------------
def test_maps_tweets_to_documents():
    docs, requests = collect(lambda _: httpx.Response(200, json={"data": [tweet("1")]}))

    [doc] = docs
    assert doc.source_platform == "twitter"
    assert doc.external_id == "1"
    assert doc.url == "https://x.com/i/status/1"
    assert doc.published_at == datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)
    assert doc.engagement == {"likes": 5, "retweets": 1, "replies": 2}

    params = requests[0].url.params
    assert requests[0].url.host == "api.x.com"
    assert params["query"] == '("brilink" OR "agen brilink") lang:id -is:retweet'
    assert params["start_time"] == "2026-09-28T12:00:00Z"


def test_start_time_is_clamped_to_the_seven_day_window():
    _, requests = collect(
        lambda _: httpx.Response(200, json={"meta": {"result_count": 0}}),
        since=NOW - timedelta(days=90),
    )

    start = datetime.strptime(requests[0].url.params["start_time"], "%Y-%m-%dT%H:%M:%SZ")
    assert NOW - start.replace(tzinfo=timezone.utc) < timedelta(days=7)


def test_follows_next_token_until_the_limit():
    def handler(request):
        if request.url.params.get("next_token") == "p2":
            return httpx.Response(200, json={"data": [tweet("3"), tweet("4")]})
        return httpx.Response(
            200, json={"data": [tweet("1"), tweet("2")], "meta": {"next_token": "p2"}}
        )

    docs, requests = collect(handler, limit=3)

    assert [d.external_id for d in docs] == ["1", "2", "3"]
    assert len(requests) == 2


def test_client_not_enrolled_makes_the_connector_unavailable():
    body = {
        "title": "Client Forbidden",
        "reason": "client-not-enrolled",
        "detail": "must use keys and tokens from a developer App that is attached to a Project",
    }
    with pytest.raises(ConnectorUnavailable, match="403.*client-not-enrolled"):
        collect(lambda _: httpx.Response(403, json=body))


def test_out_of_credits_makes_the_connector_unavailable():
    with pytest.raises(ConnectorUnavailable, match="402"):
        collect(lambda _: httpx.Response(402, json={"title": "CreditsDepleted"}))


def test_first_page_rate_limit_fails_the_run_instead_of_silent_success():
    with pytest.raises(RuntimeError, match="429"):
        collect(lambda _: httpx.Response(429, json={"title": "Too Many Requests"}))


def test_later_page_failure_keeps_what_was_collected():
    def handler(request):
        if request.url.params.get("next_token"):
            return httpx.Response(503, json={"title": "Service Unavailable"})
        return httpx.Response(200, json={"data": [tweet("1")], "meta": {"next_token": "p2"}})

    docs, _ = collect(handler)

    assert [d.external_id for d in docs] == ["1"]
