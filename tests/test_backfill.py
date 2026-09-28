"""Historical backfill: ``run_ingestion --since`` and the connectors behind it.

Everything runs offline: the orchestrator persists into a stub, Google News is
served by a fake ``feedparser`` and Redis falls back to the in-memory store.
"""

import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from brilink.ingestion import orchestrator as orch
from brilink.ingestion import run_ingestion
from brilink.ingestion.base import BaseConnector, ConnectorUnavailable, Document
from brilink.ingestion.rss_connector import (
    BACKFILL_AFTER,
    GOOGLE_NEWS_CAP,
    RSSConnector,
    _month_windows,
)
from brilink.ingestion.store_connectors import PlayStoreConnector
from brilink.settings import IngestionSettings, Settings
from brilink.utils.ratelimit import Checkpoint, CircuitBreaker, Cooldown, StateStore

NOW = datetime.now(timezone.utc)
SINCE_2019 = datetime(2019, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
class RecordingConnector(BaseConnector):
    name = "recording"
    platform = "playstore"

    def __init__(self):
        super().__init__()
        self.fail = False
        self.calls: list[tuple[datetime, int]] = []

    def fetch(self, since, limit):
        self.calls.append((since, limit))
        if self.fail:
            raise RuntimeError("upstream down")
        yield Document(
            source_platform=self.platform,
            source_name="x",
            external_id="1",
            body="agen brilink ramah",
            published_at=NOW - timedelta(days=1),
        )


def _finish(self, report, status, message=None):
    report.status, report.message = status, message
    return report


@pytest.fixture
def harness(monkeypatch):
    """An orchestrator wired to one connector, with every DB write stubbed out."""
    store = StateStore(client=None)
    connector = RecordingConnector()
    cls = orch.IngestionOrchestrator
    monkeypatch.setattr(orch, "resolve", lambda names, settings: [connector])
    monkeypatch.setattr(cls, "_persist", staticmethod(lambda docs, run_id: len(docs)))
    monkeypatch.setattr(cls, "_record_run", lambda *a: None)
    monkeypatch.setattr(cls, "_should_seed", lambda *a: False)
    monkeypatch.setattr(cls, "_finish", _finish)
    return cls(store=store), connector, store


def test_backfill_uses_the_requested_window_and_limit(harness):
    orchestrator, connector, _ = harness

    [report] = orchestrator.run("recording", force=True, since=SINCE_2019, limit=12345)

    assert connector.calls == [(SINCE_2019, 12345)]
    assert report.status == "success"
    assert report.inserted == 1
    assert report.message == "backfill since 2019-01-01"


def test_backfill_leaves_checkpoint_and_cooldown_alone(harness):
    orchestrator, _, store = harness
    orchestrator.run("recording", force=True, since=SINCE_2019)

    assert Checkpoint(store, "recording").read() is None
    assert not Cooldown(store, "recording", minutes=90).active()


def test_backfill_failure_does_not_trip_the_circuit_breaker(harness):
    orchestrator, connector, store = harness
    connector.fail = True

    for _ in range(5):
        [report] = orchestrator.run("recording", force=True, since=SINCE_2019)
        assert report.status == "failed"

    assert not CircuitBreaker(store, "recording").is_open()


def test_incremental_run_still_moves_the_checkpoint(harness):
    orchestrator, connector, store = harness
    [report] = orchestrator.run("recording", force=True)

    assert report.status == "success"
    assert connector.calls[0][1] == connector.max_items
    assert Checkpoint(store, "recording").read() is not None
    assert Cooldown(store, "recording", minutes=90).active()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_passes_since_and_limit_through(monkeypatch):
    seen = {}

    class FakeOrchestrator:
        def __init__(self, settings):
            pass

        def run(self, names, force=False, since=None, limit=None):
            seen.update(names=names, force=force, since=since, limit=limit)
            return []

    monkeypatch.setattr(run_ingestion, "IngestionOrchestrator", FakeOrchestrator)
    code = run_ingestion.main(
        ["--connectors", "rss", "--force", "--since", "2019-01-01", "--limit", "5000"]
    )

    assert code == 0
    assert seen == {"names": "rss", "force": True, "since": SINCE_2019, "limit": 5000}


@pytest.mark.parametrize("value", ["01-01-2019", "kemarin", "2999-01-01"])
def test_cli_rejects_bad_since(value):
    with pytest.raises(SystemExit):
        run_ingestion.main(["--since", value])


def test_cli_rejects_non_positive_limit():
    with pytest.raises(SystemExit):
        run_ingestion.main(["--limit", "0"])


# ---------------------------------------------------------------------------
# Google News backfill
# ---------------------------------------------------------------------------
def test_month_windows_cover_the_range_without_gaps():
    assert _month_windows(date(2019, 1, 15), date(2019, 4, 3)) == [
        (date(2019, 1, 15), date(2019, 2, 1)),
        (date(2019, 2, 1), date(2019, 3, 1)),
        (date(2019, 3, 1), date(2019, 4, 1)),
        (date(2019, 4, 1), date(2019, 4, 3)),
    ]
    assert _month_windows(date(2019, 12, 1), date(2020, 1, 1)) == [
        (date(2019, 12, 1), date(2020, 1, 1))
    ]


class _Parsed(dict):
    """Mimics feedparser's FeedParserDict just enough for the connector."""

    def __init__(self, entries):
        super().__init__(status=200)
        self.entries = entries
        self.bozo = 0
        self.status = 200


class FakeFeedparser:
    """Answers date-sliced Google News queries from a table of entry counts."""

    def __init__(self, counts):
        self.counts = counts  # {(after, before): number of entries}
        self.queries: list[tuple[str, str]] = []

    def parse(self, url, agent=None):
        query = parse_qs(urlparse(url).query).get("q", [""])[0]
        if "after:" not in query:  # per-keyword search or a direct outlet feed
            return _Parsed([])
        after = query.split("after:")[1].split()[0]
        before = query.split("before:")[1].split()[0]
        self.queries.append((after, before))
        day = datetime.fromisoformat(after).replace(tzinfo=timezone.utc)
        return _Parsed(
            [
                {
                    "title": f"Agen BRILink {after} #{i} - detikcom",
                    "summary": "agen brilink melayani warga desa",
                    "link": f"https://news.detik.com/{after}/{i}",
                    "source": {"href": "https://news.detik.com", "title": "detikcom"},
                    "published_parsed": time.gmtime(day.timestamp()),
                }
                for i in range(self.counts.get((after, before), 0))
            ]
        )


def _rss(monkeypatch, fake, today):
    monkeypatch.setitem(sys.modules, "feedparser", fake)
    connector = RSSConnector(Settings(ingestion=IngestionSettings(request_delay_seconds=0)))
    monkeypatch.setattr(connector, "_now", lambda: today)
    return connector


def test_rss_backfill_queries_month_by_month_newest_first(monkeypatch):
    today = datetime(2019, 3, 20, tzinfo=timezone.utc)
    fake = FakeFeedparser({("2019-01-01", "2019-02-01"): 3, ("2019-03-01", "2019-03-21"): 2})
    connector = _rss(monkeypatch, fake, today)

    docs = list(connector.collect(SINCE_2019, 1000))

    assert fake.queries == [
        ("2019-03-01", "2019-03-21"),
        ("2019-02-01", "2019-03-01"),
        ("2019-01-01", "2019-02-01"),
    ]
    assert len(docs) == 5
    assert all(d.source_platform == "news" for d in docs)


def test_rss_backfill_splits_a_window_that_hits_the_cap(monkeypatch):
    today = datetime(2024, 3, 31, tzinfo=timezone.utc)
    fake = FakeFeedparser(
        {
            ("2024-03-01", "2024-04-01"): GOOGLE_NEWS_CAP,  # capped: must be split
            ("2024-03-01", "2024-03-16"): 40,
            ("2024-03-16", "2024-04-01"): 60,
            ("2024-02-01", "2024-03-01"): 7,
        }
    )
    connector = _rss(monkeypatch, fake, today)

    docs = list(connector.collect(datetime(2024, 2, 1, tzinfo=timezone.utc), 1000))

    assert fake.queries == [
        ("2024-03-01", "2024-04-01"),
        ("2024-03-16", "2024-04-01"),
        ("2024-03-01", "2024-03-16"),
        ("2024-02-01", "2024-03-01"),
    ]
    # The capped window's own 100 entries are discarded in favour of its halves.
    assert len(docs) == 60 + 40 + 7


def test_rss_incremental_window_keeps_the_per_keyword_queries(monkeypatch):
    today = datetime(2026, 9, 28, tzinfo=timezone.utc)
    fake = FakeFeedparser({})
    connector = _rss(monkeypatch, fake, today)

    list(connector.collect(today - BACKFILL_AFTER + timedelta(days=1), 100))

    assert fake.queries == []  # no after:/before: slicing on a normal run


# ---------------------------------------------------------------------------
# Play Store app selection
# ---------------------------------------------------------------------------
def test_playstore_apps_follow_the_setting():
    only_agent = PlayStoreConnector(
        Settings(ingestion=IngestionSettings(playstore_apps="brilink_mobile"))
    )
    assert only_agent.apps == {"brilink_mobile": "id.co.bri.brilinkmobile"}
    assert set(PlayStoreConnector(Settings()).apps) == {"brimo", "brilink_mobile"}


def test_playstore_rejects_an_unknown_app_selection():
    connector = PlayStoreConnector(Settings(ingestion=IngestionSettings(playstore_apps="nope")))
    with pytest.raises(ConnectorUnavailable, match="INGEST_PLAYSTORE_APPS"):
        list(connector.fetch(NOW, 10))
