"""Optional LLM arbiter for low-confidence documents.

Used sparingly and on purpose: an LLM call per document would be slow and
expensive, so the ensemble only escalates the tail where the cheap signals
disagree.  If no API key is configured the judge reports itself unavailable and
those documents simply land in the human review queue instead - the pipeline
behaviour is identical, only the automation ceiling changes.

Providers
---------
* ``deepseek``  - OpenAI-compatible chat API called directly with ``httpx``
  (already a dependency), JSON output mode, thinking mode off.
* ``anthropic`` - the ``anthropic`` SDK, which must be installed separately.

``SENTIMENT_LLM_PROVIDER=auto`` (the default) picks whichever key is set,
DeepSeek first.  A key that is rejected or an account with no balance
disables the judge for the rest of the run instead of failing every document.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..logging_config import get_logger
from ..settings import get_settings
from ..utils.text import truncate

logger = get_logger(__name__)

DEFAULT_MODELS = {
    "deepseek": "deepseek-flash",
    "anthropic": "claude-haiku-4-5-20251001",
}
MAX_INPUT_CHARS = 2500
MAX_OUTPUT_TOKENS = 200
VALID_LABELS = ("positive", "neutral", "negative")
LABEL_ALIASES = {"positif": "positive", "negatif": "negative", "netral": "neutral"}
# Used when the model returns a label but no usable score.
LABEL_DEFAULT_SCORE = {"positive": 0.6, "neutral": 0.0, "negative": -0.6}
# Transient failures worth another attempt.
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
# Failures that will repeat identically for every document: bad key, no balance.
FATAL_STATUS = frozenset({401, 402, 403})

SYSTEM_PROMPT = (
    "Anda adalah analis sentimen untuk layanan perbankan agen BRILink (BRI). "
    "Tugas Anda menilai sentimen teks berbahasa Indonesia terhadap BRILink. "
    "Perhatikan negasi, sarkasme, kalimat kutipan/bantahan, dan kalimat "
    "kondisional. Berita yang hanya melaporkan angka atau kegiatan tanpa "
    "penilaian adalah 'neutral'. "
    "Jawab HANYA dengan JSON: "
    '{"label":"positive|neutral|negative","score":<-1..1>,'
    '"confidence":<0..1>,"reason":"<maks 25 kata>"}. '
    "Contoh output json: "
    '{"label":"negative","score":-0.7,"confidence":0.85,'
    '"reason":"Nasabah mengeluhkan saldo tertahan dan biaya admin mahal"}'
)


class LLMCallError(Exception):
    """A provider call that did not produce a reply."""

    def __init__(self, message: str, *, retryable: bool = False, fatal: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.fatal = fatal


@dataclass
class JudgeVerdict:
    score: float
    label: str
    confidence: float
    reason: str
    available: bool = True
    model: str = ""

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
            "model": self.model,
        }


UNAVAILABLE = JudgeVerdict(0.0, "neutral", 0.0, "llm judge disabled", available=False)


def _as_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} is not a number: {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is not a number: {value!r}") from exc


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_verdict(content: str | None, model: str = "") -> JudgeVerdict:
    """Turn a model reply into a verdict.

    Raises ``ValueError`` for anything that can't be trusted: empty text, no
    JSON object, an unknown label, or a label whose sign contradicts the score.
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("empty response")

    # Tolerate ```json fences or a sentence around the object.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        raise ValueError(f"no JSON object in response: {truncate(text, 120)!r}")
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("response JSON is not an object")

    label = str(payload.get("label", "")).strip().lower()
    label = LABEL_ALIASES.get(label, label)
    if label not in VALID_LABELS:
        raise ValueError(f"unknown label {label!r}")

    raw_score = payload.get("score")
    score = (
        LABEL_DEFAULT_SCORE[label]
        if raw_score is None
        else _clamp(_as_float(raw_score, "score"), -1.0, 1.0)
    )
    if (label == "positive" and score < 0) or (label == "negative" and score > 0):
        raise ValueError(f"label {label!r} contradicts score {score}")

    confidence = _clamp(_as_float(payload.get("confidence", 0.5), "confidence"), 0.0, 1.0)
    reason = str(payload.get("reason", "")).strip()[:200]
    return JudgeVerdict(score=score, label=label, confidence=confidence, reason=reason, model=model)


def _error_detail(response: Any) -> str:
    try:
        payload = response.json()
    except ValueError:
        return truncate(response.text, 200)
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return truncate(str(error.get("message", "")), 200)
    return truncate(response.text, 200)


class _DeepSeekBackend:
    """OpenAI-compatible chat completions, called with plain ``httpx``."""

    provider = "deepseek"

    def __init__(
        self, api_key: str, model: str, base_url: str, timeout: float, transport: Any = None
    ) -> None:
        import httpx

        self.model = model
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def complete(self, text: str) -> str:
        import httpx

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "response_format": {"type": "json_object"},
            # Short classification: reasoning tokens would only add cost and latency.
            "thinking": {"type": "disabled"},
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0.0,
            "stream": False,
        }
        try:
            response = self._client.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise LLMCallError(f"network error: {exc}", retryable=True) from exc

        status = response.status_code
        if status != 200:
            raise LLMCallError(
                f"HTTP {status}: {_error_detail(response)}",
                retryable=status in RETRYABLE_STATUS,
                fatal=status in FATAL_STATUS,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMCallError("response is not JSON", retryable=True) from exc

        usage = data.get("usage") or {}
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)

        choices = data.get("choices") or []
        if not choices:
            raise LLMCallError("response has no choices", retryable=True)
        # JSON mode can occasionally return empty content; parse_verdict rejects
        # it and the judge retries.
        return (choices[0].get("message") or {}).get("content") or ""

    def close(self) -> None:
        self._client.close()


class _AnthropicBackend:
    provider = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float) -> None:
        import anthropic

        self.model = model
        self.prompt_tokens = 0
        self.completion_tokens = 0
        # Retries are handled by LLMJudge so both providers behave the same.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=0)

    def complete(self, text: str) -> str:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            raise LLMCallError(
                str(exc),
                retryable=status is None or status in RETRYABLE_STATUS,
                fatal=status in FATAL_STATUS,
            ) from exc

        usage = getattr(response, "usage", None)
        self.prompt_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.completion_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        return response.content[0].text if response.content else ""

    def close(self) -> None:
        return None


class LLMJudge:
    """Second opinion for the documents the ensemble is unsure about.

    ``backend`` and ``sleep`` exist for tests: pass a fake backend to avoid any
    network call, and a no-op sleep to skip the retry back-off.
    """

    def __init__(self, backend: Any = None, sleep: Callable[[float], None] = time.sleep) -> None:
        settings = get_settings()
        self._config = settings.sentiment
        self._credentials = settings.credentials
        self.enabled = self._config.enable_llm_judge or backend is not None
        self._backend = backend
        self._attempted = backend is not None
        self._sleep = sleep
        self._disabled_reason: str | None = None
        self.documents_sent = 0
        self.failures = 0
        self.over_budget = 0

    @property
    def available(self) -> bool:
        return self.enabled and self._disabled_reason is None and self._ensure() is not None

    def _resolve_provider(self) -> tuple[str, str] | None:
        keys = {
            "deepseek": (self._credentials.deepseek_api_key or "").strip(),
            "anthropic": (self._credentials.anthropic_api_key or "").strip(),
        }
        wanted = self._config.llm_provider
        candidates = ("deepseek", "anthropic") if wanted == "auto" else (wanted,)
        for provider in candidates:
            if keys.get(provider):
                return provider, keys[provider]
        return None

    def _ensure(self) -> Any:
        if self._backend is not None or self._attempted:
            return self._backend
        self._attempted = True
        if not self.enabled:
            return None

        resolved = self._resolve_provider()
        if resolved is None:
            logger.warning(
                "LLM judge is enabled but no API key is set for provider %r - "
                "low-confidence documents stay in the review queue",
                self._config.llm_provider,
            )
            return None

        provider, api_key = resolved
        model = self._config.llm_model.strip() or DEFAULT_MODELS[provider]
        timeout = self._config.llm_timeout_seconds
        try:
            if provider == "deepseek":
                backend: Any = _DeepSeekBackend(
                    api_key, model, self._config.deepseek_base_url, timeout
                )
            else:
                backend = _AnthropicBackend(api_key, model, timeout)
        except Exception as exc:
            logger.warning("LLM judge unavailable (%s): %s", provider, exc)
            return None

        logger.info("LLM judge enabled: provider=%s model=%s", provider, model)
        self._backend = backend
        return backend

    def judge(self, texts: Sequence[str]) -> list[JudgeVerdict]:
        backend = self._ensure()
        if backend is None:
            return [UNAVAILABLE for _ in texts]

        limit = self._config.llm_max_documents
        verdicts: list[JudgeVerdict] = []
        skipped_for_budget = 0
        for text in texts:
            if self._disabled_reason is not None:
                verdicts.append(UNAVAILABLE)
                continue
            if self.documents_sent >= limit:
                skipped_for_budget += 1
                verdicts.append(UNAVAILABLE)
                continue
            self.documents_sent += 1
            verdicts.append(self._judge_one(backend, text))

        if skipped_for_budget:
            self.over_budget += skipped_for_budget
            logger.warning(
                "LLM judge limit of %d documents per run reached; %d documents left "
                "for human review (raise SENTIMENT_LLM_MAX_DOCUMENTS to judge more)",
                limit,
                skipped_for_budget,
            )
        return verdicts

    def _judge_one(self, backend: Any, text: str) -> JudgeVerdict:
        attempts = max(1, self._config.llm_max_retries)
        prompt = truncate(text, MAX_INPUT_CHARS)
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                return parse_verdict(backend.complete(prompt), model=backend.model)
            except LLMCallError as exc:
                last_error = str(exc)
                if exc.fatal:
                    # A bad key or an empty balance fails every call the same way.
                    self._disabled_reason = last_error
                    logger.error("LLM judge disabled for the rest of this run: %s", last_error)
                    break
                if not exc.retryable:
                    break
            except ValueError as exc:  # includes json.JSONDecodeError
                last_error = f"unusable reply: {exc}"
            if attempt < attempts:
                self._sleep(min(2.0 ** (attempt - 1), 10.0))

        self.failures += 1
        logger.warning("LLM judge gave no verdict: %s", last_error)
        return UNAVAILABLE

    def stats(self) -> dict:
        backend = self._backend
        return {
            "enabled": self.enabled,
            "provider": getattr(backend, "provider", None),
            "model": getattr(backend, "model", None),
            "documents_sent": self.documents_sent,
            "failures": self.failures,
            "over_budget": self.over_budget,
            "disabled_reason": self._disabled_reason,
            "prompt_tokens": getattr(backend, "prompt_tokens", 0),
            "completion_tokens": getattr(backend, "completion_tokens", 0),
        }

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()
