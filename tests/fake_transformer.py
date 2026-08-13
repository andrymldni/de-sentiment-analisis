"""Deterministic stand-in for IndoBERT so the test suite needs no torch and
no network call.

Two modes:
  * ``FakeTransformer(0.9)``      - every document scores exactly 0.9. Use
    this when the test is about ensemble *mechanics* (weighting, review
    routing, degradation) and the actual polarity doesn't matter.
  * ``FakeTransformer()``         - score is inferred from a tiny built-in
    cue-word table. Use this when the test asserts a specific label/sign
    driven by the input text (e.g. "this text should come out positive").

This is test-only scaffolding; production code has no lexicon or keyword
table - see brilink.nlp.ensemble for why that responsibility now lives
entirely in the pretrained model.
"""

from __future__ import annotations

from brilink.nlp.transformer_model import TransformerOutput

POSITIVE_CUES = ("memuaskan", "ramah", "membantu", "bagus", "lancar", "cepat")
NEGATIVE_CUES = ("mahal", "tertahan", "error", "susah", "gagal", "penipuan")


class FakeTransformer:
    def __init__(self, score: float | None = None, available: bool = True):
        self.score = score
        self.available = available

    def score_documents(self, texts):
        return [self._score_one(text) for text in texts]

    def _score_one(self, text: str | None) -> TransformerOutput:
        score = self.score if self.score is not None else self._infer(text)
        label = "positive" if score > 0 else "negative" if score < 0 else "neutral"
        return TransformerOutput(
            score=score,
            label=label,
            probabilities={label: 0.8},
            chunks=1,
            available=self.available,
            detail={"normalized_entropy": 0.2},
        )

    @staticmethod
    def _infer(text: str | None) -> float:
        lowered = (text or "").lower()
        hits = sum(1 for w in POSITIVE_CUES if w in lowered) - sum(
            1 for w in NEGATIVE_CUES if w in lowered
        )
        return max(-1.0, min(1.0, hits * 0.6))
