"""Ensemble behaviour: weighting, degradation, confidence and routing."""

import pytest

from brilink.nlp.ensemble import EnsembleSentimentEngine, ScoreInput

from .fake_transformer import FakeTransformer


def test_engine_produces_labels_from_the_transformer(engine):
    result = engine.score_one(ScoreInput(1, body="Pelayanannya sangat memuaskan"))
    assert result.label == "positive"
    assert result.signals["transformer"].available


def test_star_rating_is_used_as_a_signal(engine):
    low = engine.score_one(ScoreInput(1, body="aplikasinya begitulah", rating=1))
    high = engine.score_one(ScoreInput(2, body="aplikasinya begitulah", rating=5))
    assert low.score < high.score
    assert low.signals["rating"].score == -1.0
    assert high.signals["rating"].score == 1.0


def test_invalid_rating_is_ignored(engine):
    result = engine.score_one(ScoreInput(1, body="teks apa saja", rating=9))
    assert "rating" not in result.signals


def test_without_transformer_or_rating_there_is_no_polarity_signal():
    """Documents the real trade-off of dropping the lexicon: IndoBERT is now
    the only source of polarity for text with no star rating. If it's
    unavailable and there's nothing to fall back on, the document should be
    neutral, low-confidence, and routed for a human to look at - never a
    confident guess."""
    engine = EnsembleSentimentEngine(transformer=FakeTransformer(available=False), emotion=None)
    result = engine.score_one(ScoreInput(1, body="Biaya adminnya mahal sekali"))
    assert not result.signals["transformer"].available
    assert result.label == "neutral"
    assert result.requires_review


def test_a_rating_still_gives_a_signal_when_the_transformer_is_down():
    engine = EnsembleSentimentEngine(transformer=FakeTransformer(available=False), emotion=None)
    result = engine.score_one(ScoreInput(1, body="Biaya adminnya mahal sekali", rating=1))
    assert result.signals["rating"].available
    assert result.label == "negative"


def test_weights_renormalise_when_a_model_is_missing():
    """A missing transformer must degrade accuracy, never break the run."""
    engine = EnsembleSentimentEngine(
        transformer=FakeTransformer(0.0, available=False), emotion=None
    )
    result = engine.score_one(ScoreInput(1, body="Biaya adminnya mahal sekali", rating=2))
    assert not result.signals["transformer"].available
    contributions = result.explanation["contributions"]
    assert "transformer" not in contributions
    assert pytest.approx(sum(contributions.values()), abs=1e-3) == result.score


def test_transformer_score_drives_the_final_label():
    text = "biaya adminnya mahal"
    negative = EnsembleSentimentEngine(transformer=FakeTransformer(-0.9), emotion=None).score_one(
        ScoreInput(1, body=text)
    )
    positive = EnsembleSentimentEngine(transformer=FakeTransformer(0.9), emotion=None).score_one(
        ScoreInput(1, body=text)
    )
    assert positive.score > negative.score


def test_conflicting_signals_lower_agreement_and_trigger_review():
    engine = EnsembleSentimentEngine(transformer=FakeTransformer(0.9), emotion=None)
    result = engine.score_one(ScoreInput(1, body="penipuan dan penggelapan dana nasabah", rating=1))
    assert result.agreement < 0.8
    assert result.requires_review
    assert result.review_reason == "signal_conflict"


def test_agreeing_signals_produce_high_confidence():
    engine = EnsembleSentimentEngine(transformer=FakeTransformer(-0.85), emotion=None)
    result = engine.score_one(
        ScoreInput(
            1,
            body=(
                "Saldo tertahan tiga hari, biaya admin mahal banget, "
                "aplikasi sering error dan agennya susah dihubungi"
            ),
            rating=1,
        )
    )
    assert result.label == "negative"
    assert result.confidence > 0.6
    assert result.agreement > 0.7
    assert not result.requires_review


def test_thin_evidence_is_routed_to_review(engine):
    result = engine.score_one(ScoreInput(1, body="ok"))
    assert result.requires_review


def test_scores_are_bounded_and_serialisable(engine):
    result = engine.score_one(ScoreInput(1, body="penipuan penggelapan pembobolan " * 20, rating=1))
    assert -1.0 <= result.score <= 1.0
    assert 0.0 <= result.confidence <= 1.0
    payload = result.signals_payload()
    assert set(payload) <= {"transformer", "emotion", "rating", "aspect", "llm_judge"}


def test_aspects_are_attached_to_the_document_score(engine):
    result = engine.score_one(ScoreInput(1, body="Biaya adminnya mahal tapi agennya ramah sekali"))
    codes = {hit.aspect.code for hit in result.aspects}
    assert {"biaya_tarif", "layanan_agen"} <= codes
    assert result.explanation["aspects"]


def test_platform_adjustment_changes_weighting(engine):
    text = "aplikasinya begitulah"
    news = engine.score_one(ScoreInput(1, body=text, rating=1, source_platform="news"))
    store = engine.score_one(ScoreInput(2, body=text, rating=1, source_platform="playstore"))
    # Reviews trust the star rating more than news does.
    assert store.score <= news.score


def test_batch_scoring_matches_single_scoring(engine):
    items = [
        ScoreInput(1, body="sangat membantu warga desa"),
        ScoreInput(2, body="biaya adminnya mahal banget"),
    ]
    batch = engine.score_batch(items)
    singles = [engine.score_one(i) for i in items]
    assert [b.label for b in batch] == [s.label for s in singles]
    assert [round(b.score, 6) for b in batch] == [round(s.score, 6) for s in singles]


def test_empty_batch_returns_empty(engine):
    assert engine.score_batch([]) == []
