"""The ensemble scoring engine.

Why an ensemble at all?  Because each individual signal has a well-known blind
spot on this data:

| Signal      | Strength                            | Blind spot                        |
|-------------|--------------------------------------|-------------------------------------|
| IndoBERT    | Context, implicit framing, negation | Domain drift, long texts, sarcasm |
| Emotion     | Affect intensity in UGC             | Weak on factual news              |
| Star rating | Ground-truth-ish for app reviews    | Only exists for reviews           |
| Aspect vote | Rewards specificity (which topic?)  | Absent when nothing is mentioned  |

All polarity judgement is delegated to a pretrained IndoBERT sentiment model
(see ``transformer_model.py``) instead of a hand-maintained keyword lexicon.
A lexicon only ever knows the words someone thought to add and needs manual
tuning for negation, sarcasm and intensifiers; IndoBERT was fine-tuned on
labelled Indonesian sentiment data, so it generalises to phrasing nobody
hand-coded - at the cost of being a model we consult rather than a word list
we can point to.

The engine combines whatever signals are available, re-normalising the weights
so a missing model degrades accuracy rather than breaking the run.  It then
produces a *confidence* from four measurable quantities - inter-signal
agreement, signal coverage, model evidence and distance from the neutral
band - and routes low-confidence documents to a human review queue.

Every intermediate value is persisted as JSONB, so a Metabase user can drill
from a label all the way down to the model's probability distribution.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..logging_config import get_logger
from ..settings import get_settings
from ..utils.text import normalize_for_nlp, tokenize
from .aspects import AspectHit, detect_aspects, label_for, score_aspect_hits
from .llm_judge import LLMJudge
from .transformer_model import EmotionTransformer, SentimentTransformer, TransformerOutput

logger = get_logger(__name__)

# Base weights. Renormalised over the signals actually present.
BASE_WEIGHTS: dict[str, float] = {
    "transformer": 0.55,
    "emotion": 0.15,
    "rating": 0.18,
    "aspect": 0.12,
}
LLM_WEIGHT = 0.35  # applied only when the judge is consulted

# Confidence blend.
CONF_WEIGHTS = {"agreement": 0.35, "coverage": 0.2, "evidence": 0.2, "margin": 0.25}

# UGC leans on affect; editorial leans on framing. Small, explicit nudges.
PLATFORM_WEIGHT_ADJUSTMENTS: dict[str, dict[str, float]] = {
    "news": {"emotion": 0.6, "transformer": 1.05},
    "playstore": {"rating": 1.25, "emotion": 1.15},
    "appstore": {"rating": 1.25, "emotion": 1.15},
    "reddit": {"emotion": 1.2},
    "youtube": {"emotion": 1.2},
    "twitter": {"emotion": 1.2},
}


@dataclass
class ScoreInput:
    document_id: int | str
    title: str | None = None
    body: str | None = None
    rating: int | None = None
    source_platform: str = "news"

    @property
    def text(self) -> str:
        parts = [p for p in (self.title, self.body) if p]
        return " ".join(parts).strip()


@dataclass
class Signal:
    name: str
    score: float
    weight: float
    available: bool
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "weight": round(self.weight, 4),
            "available": self.available,
            **self.detail,
        }


@dataclass
class DocumentScore:
    document_id: int | str
    label: str
    score: float
    confidence: float
    agreement: float
    requires_review: bool
    review_reason: str | None
    model_version: str
    engine_mode: str
    signals: dict[str, Signal]
    aspects: list[AspectHit]
    explanation: dict[str, Any]

    def signals_payload(self) -> dict:
        return {name: signal.as_dict() for name, signal in self.signals.items()}


class EnsembleSentimentEngine:
    def __init__(
        self,
        transformer: SentimentTransformer | None = None,
        emotion: EmotionTransformer | None = None,
        judge: LLMJudge | None = None,
    ) -> None:
        self.settings = get_settings().sentiment
        mode = self.settings.engine_mode

        self._use_transformer = self.settings.enable_transformer
        self._use_emotion = self.settings.enable_emotion and mode == "ensemble"

        self.transformer = (
            transformer
            if transformer is not None
            else (SentimentTransformer() if self._use_transformer else None)
        )
        self.emotion = (
            emotion
            if emotion is not None
            else (EmotionTransformer() if self._use_emotion else None)
        )
        self.judge = judge if judge is not None else LLMJudge()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def score_batch(self, items: Sequence[ScoreInput]) -> list[DocumentScore]:
        if not items:
            return []

        texts = [item.text for item in items]

        transformer_out = (
            self.transformer.score_documents(texts)
            if self.transformer is not None
            else [None] * len(items)
        )
        emotion_out = (
            self.emotion.score_documents(texts) if self.emotion is not None else [None] * len(items)
        )

        # Aspect *detection* is cheap keyword matching and runs per document,
        # but aspect *scoring* reuses the IndoBERT model, so every window from
        # every document in the batch is flattened into one model call.
        aspect_hits_per_doc = [detect_aspects(t) for t in texts]
        if self.transformer is not None:
            score_aspect_hits(aspect_hits_per_doc, self.transformer.score_documents)

        scored = [
            self._score_one(item, transformer_out[i], emotion_out[i], aspect_hits_per_doc[i])
            for i, item in enumerate(items)
        ]

        self._apply_llm_arbitration(items, scored)
        return scored

    def score_one(self, item: ScoreInput) -> DocumentScore:
        return self.score_batch([item])[0]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _score_one(
        self,
        item: ScoreInput,
        transformer_out: TransformerOutput | None,
        emotion_out: TransformerOutput | None,
        aspect_hits: list[AspectHit],
    ) -> DocumentScore:
        text = item.text
        signals: dict[str, Signal] = {}

        # --- transformer (IndoBERT) ------------------------------------
        if transformer_out is not None:
            signals["transformer"] = Signal(
                name="transformer",
                score=transformer_out.score,
                weight=BASE_WEIGHTS["transformer"],
                available=transformer_out.available,
                detail=transformer_out.as_dict(),
            )

        # --- emotion --------------------------------------------------
        if emotion_out is not None:
            signals["emotion"] = Signal(
                name="emotion",
                score=emotion_out.score,
                weight=BASE_WEIGHTS["emotion"],
                available=emotion_out.available,
                detail=emotion_out.as_dict(),
            )

        # --- explicit star rating -------------------------------------
        rating_signal = self._rating_signal(item.rating)
        if rating_signal is not None:
            signals["rating"] = rating_signal

        # --- aspect consensus -------------------------------------------
        # Scored upstream in score_batch() (one shared transformer call);
        # here we just fold the per-aspect scores into an aspect signal.
        aspect_signal = self._aspect_signal(aspect_hits)
        if aspect_signal is not None:
            signals["aspect"] = aspect_signal

        self._adjust_for_platform(signals, item.source_platform)

        final_score, contributions = self._combine(signals)
        agreement = self._agreement(signals)
        confidence = self._confidence(signals, final_score, agreement, transformer_out, text)
        label = label_for(final_score, self.settings.neutral_band)

        requires_review, reason = self._review_decision(confidence, signals, final_score, label)

        explanation = {
            "contributions": contributions,
            "aspects": [hit.as_dict() for hit in aspect_hits],
            "text_stats": {
                "tokens": len(tokenize(normalize_for_nlp(text))),
                "chars": len(text),
                "has_rating": item.rating is not None,
            },
        }

        return DocumentScore(
            document_id=item.document_id,
            label=label,
            score=round(final_score, 4),
            confidence=round(confidence, 4),
            agreement=round(agreement, 4),
            requires_review=requires_review,
            review_reason=reason,
            model_version=self.settings.model_version,
            engine_mode=self.settings.engine_mode,
            signals=signals,
            aspects=aspect_hits,
            explanation=explanation,
        )

    @staticmethod
    def _rating_signal(rating: int | None) -> Signal | None:
        if rating is None:
            return None
        try:
            value = int(rating)
        except (TypeError, ValueError):
            return None
        if not 1 <= value <= 5:
            return None
        # 1->-1, 2->-0.5, 3->0, 4->0.5, 5->1
        score = (value - 3) / 2
        return Signal(
            name="rating",
            score=score,
            weight=BASE_WEIGHTS["rating"],
            available=True,
            detail={"stars": value},
        )

    @staticmethod
    def _aspect_signal(hits: Sequence[AspectHit]) -> Signal | None:
        scored = [h for h in hits if h.score_available]
        if not scored:
            return None
        weights = [1.0 + 0.15 * min(h.mentions, 5) for h in scored]
        total = sum(weights)
        value = sum(h.score * w for h, w in zip(scored, weights, strict=False)) / total
        spread = statistics.pstdev([h.score for h in scored]) if len(scored) > 1 else 0.0
        return Signal(
            name="aspect",
            score=value,
            weight=BASE_WEIGHTS["aspect"],
            available=True,
            detail={
                "aspect_count": len(scored),
                "dispersion": round(spread, 4),
                "per_aspect": {h.aspect.code: round(h.score, 3) for h in scored},
            },
        )

    @staticmethod
    def _adjust_for_platform(signals: dict[str, Signal], platform: str) -> None:
        adjustments = PLATFORM_WEIGHT_ADJUSTMENTS.get((platform or "").lower())
        if not adjustments:
            return
        for name, factor in adjustments.items():
            if name in signals:
                signals[name].weight *= factor

    @staticmethod
    def _combine(signals: dict[str, Signal]) -> tuple[float, dict[str, float]]:
        active = {n: s for n, s in signals.items() if s.available}
        if not active:
            return 0.0, {}
        total_weight = sum(s.weight for s in active.values())
        if total_weight <= 0:
            return 0.0, {}
        contributions = {
            name: round(s.score * s.weight / total_weight, 4) for name, s in active.items()
        }
        score = sum(contributions.values())
        return max(min(score, 1.0), -1.0), contributions

    @staticmethod
    def _agreement(signals: dict[str, Signal]) -> float:
        """1.0 = every signal points the same way; 0.0 = maximal conflict."""
        values = [s.score for s in signals.values() if s.available]
        if len(values) < 2:
            return 0.6  # single-signal documents are neither agreed nor conflicted
        spread = statistics.pstdev(values)
        # Max meaningful spread on a [-1, 1] scale is 1.0 (e.g. -1 and +1).
        return max(0.0, 1.0 - min(spread, 1.0))

    def _confidence(
        self,
        signals: dict[str, Signal],
        score: float,
        agreement: float,
        transformer_out: TransformerOutput | None,
        text: str,
    ) -> float:
        available = sum(1 for s in signals.values() if s.available)
        coverage = available / max(len(BASE_WEIGHTS), 1)

        length_adequacy = min(len(text.split()) / 40, 1.0)
        if transformer_out is not None and transformer_out.available:
            # A peaked probability distribution (low entropy) is the model
            # being decisive; a flat one means it genuinely can't tell.
            decisiveness = 1.0 - transformer_out.detail.get("normalized_entropy", 1.0)
            evidence = 0.5 * length_adequacy + 0.5 * max(0.0, min(decisiveness, 1.0))
        else:
            evidence = length_adequacy * 0.5

        band = max(self.settings.neutral_band, 1e-6)
        margin = min(abs(score) / (band * 3), 1.0)

        confidence = (
            CONF_WEIGHTS["agreement"] * agreement
            + CONF_WEIGHTS["coverage"] * coverage
            + CONF_WEIGHTS["evidence"] * evidence
            + CONF_WEIGHTS["margin"] * margin
        )
        return max(0.0, min(confidence, 1.0))

    def _review_decision(
        self,
        confidence: float,
        signals: dict[str, Signal],
        score: float,
        label: str,
    ) -> tuple[bool, str | None]:
        threshold = self.settings.review_confidence_threshold

        strong = [s for s in signals.values() if s.available and abs(s.score) >= 0.35]
        signs = {1 if s.score > 0 else -1 for s in strong}
        if len(signs) > 1:
            return True, "signal_conflict"

        if confidence < threshold:
            return True, "low_confidence"

        if label != "neutral" and abs(score) < self.settings.neutral_band * 1.4:
            return True, "borderline_label"

        available = sum(1 for s in signals.values() if s.available)
        if available <= 1:
            return True, "insufficient_signals"

        return False, None

    def _apply_llm_arbitration(
        self, items: Sequence[ScoreInput], scored: list[DocumentScore]
    ) -> None:
        if not self.judge.available:
            return

        candidates = [
            idx
            for idx, doc in enumerate(scored)
            if doc.requires_review and doc.review_reason != "insufficient_signals"
        ]
        if not candidates:
            return

        logger.info("Escalating %d low-confidence documents to LLM judge", len(candidates))
        verdicts = self.judge.judge([items[i].text for i in candidates])

        for idx, verdict in zip(candidates, verdicts, strict=False):
            if not verdict.available:
                continue
            doc = scored[idx]
            doc.signals["llm_judge"] = Signal(
                name="llm_judge",
                score=verdict.score,
                weight=LLM_WEIGHT,
                available=True,
                detail=verdict.as_dict(),
            )
            new_score, contributions = self._combine(doc.signals)
            doc.score = round(new_score, 4)
            doc.label = label_for(new_score, self.settings.neutral_band)
            doc.agreement = round(self._agreement(doc.signals), 4)
            doc.confidence = round(min(1.0, max(doc.confidence, verdict.confidence * 0.9)), 4)
            doc.explanation["contributions"] = contributions
            doc.explanation["llm_reason"] = verdict.reason
            doc.requires_review = doc.confidence < self.settings.review_confidence_threshold
            doc.review_reason = "low_confidence_after_llm" if doc.requires_review else None
