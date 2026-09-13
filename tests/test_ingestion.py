from datetime import datetime, timedelta, timezone

import pytest

from brilink.ingestion.base import Document
from brilink.ingestion.registry import REGISTRY, catalog, resolve
from brilink.ingestion.seed_connector import SeedConnector
from brilink.utils.ratelimit import Checkpoint, CircuitBreaker, Cooldown, StateStore


@pytest.fixture
def store():
    return StateStore(client=None)


def test_registry_exposes_every_connector():
    names = {c["name"] for c in catalog()}
    assert names == set(REGISTRY)
    assert {
        "rss",
        "expanded_rss",
        "web_scraper",
        "playstore",
        "reddit",
        "youtube",
        "twitter",
        "kaskus",
        "google_trends",
        "google_maps",
        "seed",
    } <= names


def test_resolve_all_excludes_the_seed_fallback():
    names = {c.name for c in resolve("all")}
    assert "seed" not in names
    assert "rss" in names
    assert "kaskus" in names
    assert "google_trends" in names


def test_resolve_rejects_unknown_connectors():
    with pytest.raises(ValueError, match="Unknown connector"):
        resolve("does_not_exist")


def test_document_finalize_populates_derived_fields():
    doc = Document(
        source_platform="news",
        source_name="detik",
        external_id="abc",
        title="  Agen <b>BRILink</b> membantu  ",
        body="Isi berita tentang brilink di desa.",
    ).finalize(["brilink"])

    assert doc.doc_uid and doc.content_hash_value
    assert "<b>" not in doc.title
    assert doc.language == "id"
    assert doc.relevance == 1.0
    assert doc.source_type == "editorial"


def test_document_row_matches_column_contract():
    from brilink.ingestion.base import DOCUMENT_COLUMNS

    doc = Document(
        source_platform="playstore", source_name="brimo", external_id="1", body="bagus"
    ).finalize(["brilink"])
    assert len(doc.as_row("run-1")) == len(DOCUMENT_COLUMNS)


def test_simhash_is_stored_within_bigint_range():
    doc = Document(
        source_platform="news", source_name="x", external_id="1", body="a" * 200
    ).finalize([])
    stored = doc.as_row("run-1")[15]
    assert -(2**63) <= stored < 2**63


def test_cooldown_blocks_then_expires(store):
    cooldown = Cooldown(store, "demo", minutes=1)
    assert not cooldown.active()
    cooldown.arm()
    assert cooldown.active()
    assert cooldown.remaining() > 0


def test_zero_minute_cooldown_never_blocks(store):
    cooldown = Cooldown(store, "demo", minutes=0)
    cooldown.arm()
    assert not cooldown.active()


def test_checkpoint_round_trip(store):
    checkpoint = Checkpoint(store, "demo")
    assert checkpoint.read() is None
    moment = datetime.now(timezone.utc) - timedelta(days=2)
    checkpoint.write(moment)
    assert abs((checkpoint.read() - moment).total_seconds()) < 1


def test_circuit_breaker_opens_and_closes(store):
    breaker = CircuitBreaker(store, "demo", threshold=2, reset_seconds=60)
    assert not breaker.is_open()
    breaker.record_failure()
    assert not breaker.is_open()
    breaker.record_failure()
    assert breaker.is_open()
    breaker.record_success()
    assert not breaker.is_open()


def test_state_store_falls_back_to_memory_without_redis(store):
    store.set("k", "v", ttl=30)
    assert store.get("k") == "v"
    store.delete("k")
    assert store.get("k") is None


def test_seed_connector_generates_a_diverse_corpus():
    connector = SeedConnector(total=200)
    docs = list(connector.collect(datetime.now(timezone.utc) - timedelta(days=400), 200))

    assert len(docs) >= 200
    platforms = {d.source_platform for d in docs}
    assert {"news", "playstore", "reddit", "twitter"} <= platforms
    assert all(d.text for d in docs)
    assert all(d.doc_uid for d in docs)
    assert len({d.doc_uid for d in docs}) == len(docs)

    ratings = [d.rating for d in docs if d.rating]
    assert set(ratings) <= {1, 2, 3, 4, 5}


def test_seed_connector_is_deterministic():
    a = list(SeedConnector(total=50).collect(datetime.now(timezone.utc), 50))
    b = list(SeedConnector(total=50).collect(datetime.now(timezone.utc), 50))
    assert [d.text for d in a] == [d.text for d in b]


def test_seed_corpus_is_flagged_synthetic():
    docs = list(SeedConnector(total=30).collect(datetime.now(timezone.utc), 30))
    assert all(d.raw_payload.get("is_synthetic") for d in docs)
