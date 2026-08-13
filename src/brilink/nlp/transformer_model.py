"""Transformer signal providers (IndoBERT sentiment + Indonesian emotion).

Both wrappers share three properties that matter in a pipeline:

* **Lazy, fail-soft loading.** If Hugging Face is unreachable the provider
  reports itself unavailable and the ensemble re-normalises its weights over
  the signals that *are* present. The DAG never dies because a CDN blinked.
* **Sliding-window chunking.** Truncating a 900-word article to 384 tokens
  throws away most of the evidence; we score overlapping windows and combine
  them with a length weight plus a mild recency bias toward the lede.
* **Full distribution output.** We keep the whole probability vector, not just
  argmax, so the ensemble can reason about model uncertainty.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..logging_config import get_logger
from ..settings import get_settings
from ..utils.text import chunk_words

logger = get_logger(__name__)

# mdhugol/indonesia-bert-sentiment-classification label order.
SENTIMENT_LABEL_MAP = {
    "LABEL_0": "positive",
    "LABEL_1": "neutral",
    "LABEL_2": "negative",
    "positive": "positive",
    "neutral": "neutral",
    "negative": "negative",
}

# Emotion -> valence contribution. Anger and sadness are not equally negative.
EMOTION_VALENCE = {
    "anger": -0.85,
    "marah": -0.85,
    "sadness": -0.6,
    "sedih": -0.6,
    "fear": -0.55,
    "takut": -0.55,
    "disgust": -0.8,
    "jijik": -0.8,
    "happy": 0.8,
    "joy": 0.8,
    "senang": 0.8,
    "love": 0.75,
    "cinta": 0.75,
    "surprise": 0.05,
    "terkejut": 0.05,
    "neutral": 0.0,
}


@dataclass
class TransformerOutput:
    score: float
    label: str
    probabilities: dict[str, float]
    chunks: int
    available: bool
    detail: dict

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "label": self.label,
            "probabilities": {k: round(v, 4) for k, v in self.probabilities.items()},
            "chunks": self.chunks,
            **self.detail,
        }


UNAVAILABLE = TransformerOutput(0.0, "neutral", {}, 0, False, {"reason": "unavailable"})


class _BasePipeline:
    task = "text-classification"
    model_name = ""

    def __init__(self, model_name: str | None = None, max_length: int | None = None):
        settings = get_settings()
        self.model_name = model_name or self.model_name
        self.max_length = max_length or settings.sentiment.max_tokens
        self.batch_size = settings.sentiment.batch_size
        self._pipeline = None
        self._load_attempted = False
        self._load_error: str | None = None

    @property
    def available(self) -> bool:
        return self._ensure() is not None

    def _ensure(self):
        if self._pipeline is not None or self._load_attempted:
            return self._pipeline
        self._load_attempted = True
        try:
            from transformers import pipeline as hf_pipeline

            self._pipeline = hf_pipeline(
                self.task,
                model=self.model_name,
                tokenizer=self.model_name,
                truncation=True,
                max_length=self.max_length,
                top_k=None,
            )
            logger.info("Loaded transformer model %s", self.model_name)
        except Exception as exc:
            self._load_error = str(exc)
            logger.warning(
                "Transformer %s unavailable (%s) - ensemble will re-weight " "remaining signals",
                self.model_name,
                exc,
            )
            self._pipeline = None
        return self._pipeline

    def _classify(self, texts: Sequence[str]) -> list[dict[str, float]]:
        pipe = self._ensure()
        if pipe is None or not texts:
            return []
        out: list[dict[str, float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            for prediction in pipe(batch):
                if isinstance(prediction, dict):
                    prediction = [prediction]
                out.append({p["label"]: float(p["score"]) for p in prediction})
        return out


class SentimentTransformer(_BasePipeline):
    def __init__(self, model_name: str | None = None):
        settings = get_settings()
        super().__init__(model_name or settings.sentiment.transformer_model)
        self._chunk_words = settings.sentiment.chunk_words
        self._chunk_overlap = settings.sentiment.chunk_overlap_words
        self._max_chunks = settings.sentiment.max_chunks

    def score_documents(self, texts: Sequence[str]) -> list[TransformerOutput]:
        if not self.available:
            return [UNAVAILABLE for _ in texts]

        # Flatten every document into chunks, classify once, then re-assemble.
        flat: list[str] = []
        spans: list[tuple[int, int]] = []
        for text in texts:
            chunks = chunk_words(
                text or "",
                size=self._chunk_words,
                overlap=self._chunk_overlap,
                max_chunks=self._max_chunks,
            ) or [""]
            spans.append((len(flat), len(flat) + len(chunks)))
            flat.extend(chunks)

        raw = self._classify(flat)
        if not raw:
            return [UNAVAILABLE for _ in texts]

        results: list[TransformerOutput] = []
        for start, end in spans:
            results.append(self._combine(raw[start:end], flat[start:end]))
        return results

    @staticmethod
    def _combine(chunk_probs: list[dict[str, float]], chunk_texts: list[str]) -> TransformerOutput:
        if not chunk_probs:
            return UNAVAILABLE

        aggregate: dict[str, float] = {}
        total_weight = 0.0
        for position, (probs, text) in enumerate(zip(chunk_probs, chunk_texts, strict=False)):
            # Longer chunks carry more evidence; earlier chunks (lede) matter
            # slightly more in news writing.
            length_weight = max(len(text.split()), 1) ** 0.5
            recency_weight = 1.0 / (1.0 + 0.12 * position)
            weight = length_weight * recency_weight
            total_weight += weight
            for raw_label, prob in probs.items():
                label = SENTIMENT_LABEL_MAP.get(raw_label, raw_label.lower())
                aggregate[label] = aggregate.get(label, 0.0) + prob * weight

        if total_weight:
            aggregate = {k: v / total_weight for k, v in aggregate.items()}

        positive = aggregate.get("positive", 0.0)
        negative = aggregate.get("negative", 0.0)
        neutral = aggregate.get("neutral", 0.0)

        # Signed expectation over the distribution: a 0.5/0.5 pos-neg split
        # correctly lands on 0 instead of confidently picking a side.
        score = positive - negative
        label = max(aggregate, key=aggregate.get) if aggregate else "neutral"
        entropy = -sum(p * math.log(p + 1e-9) for p in aggregate.values())
        max_entropy = math.log(max(len(aggregate), 2))

        return TransformerOutput(
            score=round(max(min(score, 1.0), -1.0), 4),
            label=label,
            probabilities=aggregate,
            chunks=len(chunk_probs),
            available=True,
            detail={
                "neutral_mass": round(neutral, 4),
                "normalized_entropy": round(entropy / max_entropy, 4) if max_entropy else 0.0,
            },
        )


class EmotionTransformer(_BasePipeline):
    def __init__(self, model_name: str | None = None):
        settings = get_settings()
        super().__init__(model_name or settings.sentiment.emotion_model)

    def score_documents(self, texts: Sequence[str]) -> list[TransformerOutput]:
        if not self.available:
            return [UNAVAILABLE for _ in texts]

        trimmed = [" ".join((t or "").split()[:160]) for t in texts]
        raw = self._classify(trimmed)
        if not raw:
            return [UNAVAILABLE for _ in texts]

        results: list[TransformerOutput] = []
        for probs in raw:
            normalized = {k.lower(): v for k, v in probs.items()}
            valence = sum(
                EMOTION_VALENCE.get(label, 0.0) * prob for label, prob in normalized.items()
            )
            dominant = max(normalized, key=normalized.get) if normalized else "neutral"
            results.append(
                TransformerOutput(
                    score=round(max(min(valence, 1.0), -1.0), 4),
                    label=dominant,
                    probabilities=normalized,
                    chunks=1,
                    available=True,
                    detail={"dominant_emotion": dominant},
                )
            )
        return results
