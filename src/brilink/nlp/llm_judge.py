"""Optional LLM arbiter for low-confidence documents.

Used sparingly and on purpose: an LLM call per document would be slow and
expensive, so the ensemble only escalates the tail where the cheap signals
disagree.  If no API key is configured the judge reports itself unavailable and
those documents simply land in the human review queue instead - the pipeline
behaviour is identical, only the automation ceiling changes.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from ..logging_config import get_logger
from ..settings import get_settings
from ..utils.text import truncate

logger = get_logger(__name__)

SYSTEM_PROMPT = (
    "Anda adalah analis sentimen untuk layanan perbankan agen BRILink (BRI). "
    "Tugas Anda menilai sentimen teks berbahasa Indonesia terhadap BRILink. "
    "Perhatikan negasi, sarkasme, kalimat kutipan/bantahan, dan kalimat "
    "kondisional. Berita yang hanya melaporkan angka atau kegiatan tanpa "
    "penilaian adalah 'neutral'. "
    "Jawab HANYA dengan JSON: "
    '{"label":"positive|neutral|negative","score":<-1..1>,'
    '"confidence":<0..1>,"reason":"<maks 25 kata>"}'
)


@dataclass
class JudgeVerdict:
    score: float
    label: str
    confidence: float
    reason: str
    available: bool = True

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
        }


UNAVAILABLE = JudgeVerdict(0.0, "neutral", 0.0, "llm judge disabled", available=False)


class LLMJudge:
    def __init__(self) -> None:
        settings = get_settings()
        self.enabled = settings.sentiment.enable_llm_judge
        self._api_key = settings.credentials.anthropic_api_key
        self._client = None
        self._attempted = False

    @property
    def available(self) -> bool:
        return self.enabled and self._ensure() is not None

    def _ensure(self):
        if self._client is not None or self._attempted:
            return self._client
        self._attempted = True
        if not self.enabled or not self._api_key:
            return None
        try:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self._api_key)
            logger.info("LLM judge enabled")
        except Exception as exc:
            logger.warning("LLM judge unavailable: %s", exc)
            self._client = None
        return self._client

    def judge(self, texts: Sequence[str]) -> list[JudgeVerdict]:
        client = self._ensure()
        if client is None:
            return [UNAVAILABLE for _ in texts]

        verdicts: list[JudgeVerdict] = []
        for text in texts:
            verdicts.append(self._judge_one(client, text))
        return verdicts

    def _judge_one(self, client, text: str) -> JudgeVerdict:
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": truncate(text, 2500)}],
            )
            payload = json.loads(response.content[0].text.strip())
            return JudgeVerdict(
                score=float(payload.get("score", 0.0)),
                label=str(payload.get("label", "neutral")),
                confidence=float(payload.get("confidence", 0.5)),
                reason=str(payload.get("reason", ""))[:200],
            )
        except Exception as exc:
            logger.warning("LLM judge call failed: %s", exc)
            return UNAVAILABLE
