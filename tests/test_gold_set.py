"""Accuracy floor for the *real* engine. Improve it; don't silently regress it.

This is deliberately not part of the fast unit-test tier: it needs torch,
transformers, and (on first run) a network connection to download IndoBERT.
It's skipped automatically wherever those aren't available, and CI runs it in
its own job with `requirements/nlp.txt` installed - see `nlp-benchmark` in
`.github/workflows/ci.yml`. Locally, `make bench` (tests/gold_report.py) gives
the same numbers with a readable diff of every miss.
"""

from __future__ import annotations

import pytest

from .gold_cases import GOLD_CASES

pytest.importorskip("transformers", reason="gold-set benchmark needs torch/transformers installed")

from brilink.nlp.ensemble import EnsembleSentimentEngine, ScoreInput  # noqa: E402

MIN_ACCURACY = 0.75
MIN_NON_NEUTRAL_DIRECTION_ACCURACY = 0.8


@pytest.fixture(scope="module")
def real_engine():
    engine = EnsembleSentimentEngine()
    if not (engine.transformer and engine.transformer.available):
        pytest.skip("IndoBERT model could not be loaded (no network / no cached weights)")
    return engine


def _predictions(real_engine):
    items = [ScoreInput(i, body=text) for i, (text, _, _) in enumerate(GOLD_CASES)]
    return real_engine.score_batch(items)


def test_gold_set_accuracy(real_engine):
    results = _predictions(real_engine)
    correct = sum(
        1
        for result, (_, expected, _) in zip(results, GOLD_CASES, strict=False)
        if result.label == expected
    )
    accuracy = correct / len(GOLD_CASES)

    failures = [
        f"  {expected:>8} != {result.label:<8} ({why}) :: {text[:70]}"
        for result, (text, expected, why) in zip(results, GOLD_CASES, strict=False)
        if result.label != expected
    ]
    assert (
        accuracy >= MIN_ACCURACY
    ), f"Gold-set accuracy {accuracy:.0%} below floor {MIN_ACCURACY:.0%}\n" + "\n".join(failures)


def test_directional_accuracy_on_polarised_cases(real_engine):
    """Getting the *direction* wrong on a clearly polarised text is severe."""
    results = _predictions(real_engine)
    polarised = [
        (r, e)
        for r, (_, e, _) in zip(results, GOLD_CASES, strict=False)
        if e in {"positive", "negative"}
    ]
    correct = sum(1 for r, e in polarised if r.label == e)
    assert correct / len(polarised) >= MIN_NON_NEUTRAL_DIRECTION_ACCURACY


def test_no_polarity_inversions(real_engine):
    """Positive must never be predicted negative, or vice versa."""
    results = _predictions(real_engine)
    inversions = [
        (text, expected, r.label)
        for r, (text, expected, _) in zip(results, GOLD_CASES, strict=False)
        if {expected, r.label} == {"positive", "negative"}
    ]
    assert not inversions, f"Hard inversions: {inversions}"


@pytest.mark.parametrize("text,expected,why", GOLD_CASES)
def test_scores_are_well_formed(real_engine, text, expected, why):
    result = real_engine.score_one(ScoreInput(0, body=text))
    assert -1.0 <= result.score <= 1.0
    assert 0.0 <= result.confidence <= 1.0
    assert result.label in {"positive", "neutral", "negative"}
