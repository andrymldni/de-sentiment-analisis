"""YouTube connector: comments from the official BRI channel.

The Data API client is replaced with an in-memory fake, so these tests need
no network and no API key.
"""

from datetime import datetime, timezone

import pytest

from brilink.ingestion.base import ConnectorUnavailable
from brilink.ingestion.social_connectors import YouTubeConnector, _thread_comments
from brilink.settings import IngestionSettings, Settings

CHANNEL = "UCRHFE_ooDrkEiRRJbog3EjA"
SINCE = datetime(2026, 9, 1, tzinfo=timezone.utc)


def comment(cid, text, when, video="vid1", author="UCuser"):
    return {
        "id": cid,
        "snippet": {
            "videoId": video,
            "textOriginal": text,
            "textDisplay": text,
            "authorDisplayName": f"@{author}",
            "authorChannelId": {"value": author},
            "publishedAt": when,
            "likeCount": 3,
        },
    }


def thread(top, *replies):
    item = {"id": top["id"], "snippet": {"topLevelComment": top}}
    if replies:
        item["replies"] = {"comments": list(replies)}
    return item


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeYouTube:
    """Serves commentThreads pages keyed by (searchTerms, pageToken)."""

    def __init__(self, pages, fail_terms=(), titles=None):
        self.pages = pages
        self.fail_terms = set(fail_terms)
        self.titles = titles or {}
        self.calls = []

    def commentThreads(self):  # noqa: N802 - mirrors the googleapiclient API
        return self

    def videos(self):
        return _Videos(self.titles)

    def list(self, **params):
        self.calls.append(params)
        term, token = params["searchTerms"], params.get("pageToken")

        def run():
            if term in self.fail_terms:
                raise RuntimeError("quotaExceeded")
            return self.pages.get((term, token), {"items": []})

        return _Request(run)


class _Videos:
    def __init__(self, titles):
        self.titles = titles

    def list(self, part, id):  # noqa: A002 - mirrors the googleapiclient API
        items = [
            {"id": v, "snippet": {"title": self.titles[v]}}
            for v in id.split(",")
            if v in self.titles
        ]
        return _Request(lambda: {"items": items})


def make_connector(keywords="brilink,agen brilink", channels=CHANNEL):
    settings = Settings(
        ingestion=IngestionSettings(
            keywords=keywords, youtube_channel_ids=channels, request_delay_seconds=0
        )
    )
    return YouTubeConnector(settings)


def collect(connector, youtube, limit=100):
    return list(connector._fetch_with(youtube, SINCE, limit))


# ----------------------------------------------------------------------
def test_defaults_to_the_official_bri_channel():
    assert IngestionSettings().youtube_channel_ids == CHANNEL
    assert make_connector(channels=f" {CHANNEL} , UCother ,").channel_ids == [CHANNEL, "UCother"]


def test_keeps_customer_mentions_and_drops_the_rest():
    bri_reply = comment(
        "t1.r2", "Halo, silakan ke agen BRILink terdekat", "2026-09-20T10:00:00Z", author=CHANNEL
    )
    pages = {
        ("brilink", None): {
            "items": [
                thread(
                    comment("t1", "Agen BRILink di desa saya ramah", "2026-09-20T09:00:00Z"),
                    comment("t1.r1", "betul, brilink sangat membantu", "2026-09-20T09:30:00Z"),
                    bri_reply,
                    comment("t1.r3", "setuju", "2026-09-20T11:00:00Z"),
                ),
                thread(comment("t2", "Aplikasi BRImo error terus", "2026-09-19T08:00:00Z")),
            ]
        }
    }
    youtube = FakeYouTube(pages, titles={"vid1": "Promo BRI"})
    docs = collect(make_connector(), youtube)

    assert [d.external_id for d in docs] == ["t1", "t1.r1"]
    first = docs[0]
    assert first.source_platform == "youtube"
    assert first.source_name == "youtube:vid1"
    assert first.title is None  # the promo video title must not be scored
    assert first.body == "Agen BRILink di desa saya ramah"
    assert first.url == "https://www.youtube.com/watch?v=vid1&lc=t1"
    assert first.published_at == datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
    assert first.raw_payload["video_title"] == "Promo BRI"
    assert first.raw_payload["channel_id"] == CHANNEL
    assert first.raw_payload["is_reply"] is False
    assert docs[1].raw_payload["is_reply"] is True


def test_queries_the_channel_feed_newest_first():
    youtube = FakeYouTube({})
    collect(make_connector(), youtube)
    assert {c["searchTerms"] for c in youtube.calls} == {"brilink", "agen brilink"}
    for call in youtube.calls:
        assert call["allThreadsRelatedToChannelId"] == CHANNEL
        assert call["order"] == "time"
        assert "videoId" not in call


def test_the_same_comment_found_by_two_terms_is_emitted_once():
    item = thread(comment("t1", "agen brilink mantap", "2026-09-20T09:00:00Z"))
    pages = {("brilink", None): {"items": [item]}, ("agen brilink", None): {"items": [item]}}
    docs = collect(make_connector(), FakeYouTube(pages))
    assert [d.external_id for d in docs] == ["t1"]


def test_paging_stops_at_the_since_cutoff():
    pages = {
        ("brilink", None): {
            "items": [thread(comment("new", "brilink ok", "2026-09-10T00:00:00Z"))],
            "nextPageToken": "p2",
        },
        ("brilink", "p2"): {
            "items": [thread(comment("old", "brilink lama", "2026-08-01T00:00:00Z"))],
            "nextPageToken": "p3",
        },
    }
    youtube = FakeYouTube(pages)
    docs = collect(make_connector(keywords="brilink"), youtube)

    assert [d.external_id for d in docs] == ["new"]
    assert [c.get("pageToken") for c in youtube.calls] == [None, "p2"]


def test_a_recent_thread_can_contribute_only_its_matching_reply():
    item = thread(
        comment("t9", "tanya soal tabungan", "2026-09-05T00:00:00Z"),
        comment("t9.r1", "coba ke agen brilink saja", "2026-09-25T00:00:00Z"),
    )
    docs = collect(
        make_connector(keywords="brilink"), FakeYouTube({("brilink", None): {"items": [item]}})
    )
    assert [d.external_id for d in docs] == ["t9.r1"]


def test_limit_caps_the_number_of_documents():
    items = [
        thread(comment(f"t{i}", "brilink", f"2026-09-2{i}T00:00:00Z")) for i in range(5, 0, -1)
    ]
    docs = collect(make_connector(), FakeYouTube({("brilink", None): {"items": items}}), limit=2)
    assert len(docs) == 2


def test_a_failing_term_does_not_sink_the_others():
    item = thread(comment("t1", "agen brilink", "2026-09-20T00:00:00Z"))
    pages = {("agen brilink", None): {"items": [item]}}
    docs = collect(make_connector(), FakeYouTube(pages, fail_terms={"brilink"}))
    assert [d.external_id for d in docs] == ["t1"]


def test_every_request_failing_is_reported_as_a_failure():
    youtube = FakeYouTube({}, fail_terms={"brilink", "agen brilink"})
    with pytest.raises(RuntimeError, match="all 2 YouTube comment searches failed"):
        collect(make_connector(), youtube)


def test_empty_channel_list_makes_the_connector_unavailable():
    pytest.importorskip("googleapiclient")
    with pytest.raises(ConnectorUnavailable, match="INGEST_YOUTUBE_CHANNEL_IDS"):
        list(make_connector(channels=" , ").fetch(SINCE, 10))


def test_thread_comments_includes_embedded_replies():
    item = thread(
        comment("t1", "a", "2026-09-20T00:00:00Z"), comment("t1.r", "b", "2026-09-21T00:00:00Z")
    )
    assert [cid for cid, _ in _thread_comments(item)] == ["t1", "t1.r"]
    assert [cid for cid, _ in _thread_comments(thread(comment("t2", "c", "x")))] == ["t2"]
