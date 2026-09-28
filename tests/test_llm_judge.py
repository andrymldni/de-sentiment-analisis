"""LLM judge: provider selection, DeepSeek wire format, parsing and failure modes.

No test here touches the network: DeepSeek calls go through
``httpx.MockTransport`` and everything else uses a scripted fake backend.
"""

import json

import httpx
import pytest

from brilink.nlp.ensemble import EnsembleSentimentEngine, ScoreInput
from brilink.nlp.llm_judge import (
    DEFAULT_MODELS,
    MAX_INPUT_CHARS,
    SYSTEM_PROMPT,
    LLMCallError,
    LLMJudge,
    _DeepSeekBackend,
    parse_verdict,
)
from brilink.settings import CredentialSettings, SentimentSettings

from .fake_transformer import FakeTransformer

NEGATIVE_REPLY = '{"label":"negative","score":-0.8,"confidence":0.9,"reason":"penipuan"}'


class ScriptedBackend:
    """Returns (or raises) the scripted replies in order, recording each prompt.
    The last reply repeats once the script runs out."""

    provider = "fake"
    model = "fake-model"
    prompt_tokens = 0
    completion_tokens = 0

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, text):
        self.prompts.append(text)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        pass


def make_judge(backend, **config):
    judge = LLMJudge(backend=backend, sleep=lambda _seconds: None)
    judge._config = SentimentSettings(enable_llm_judge=True, **config)
    return judge


def configured_judge(provider="auto", deepseek=None, anthropic=None):
    judge = LLMJudge()
    judge.enabled = True
    judge._config = SentimentSettings(enable_llm_judge=True, llm_provider=provider)
    judge._credentials = CredentialSettings(deepseek_api_key=deepseek, anthropic_api_key=anthropic)
    return judge


def deepseek_backend(handler):
    return _DeepSeekBackend(
        "sk-test",
        DEFAULT_MODELS["deepseek"],
        "https://api.deepseek.com/",
        timeout=5,
        transport=httpx.MockTransport(handler),
    )


def chat_response(content, prompt_tokens=120, completion_tokens=30):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


# ----------------------------------------------------------------------
# parse_verdict
# ----------------------------------------------------------------------
def test_parse_verdict_reads_the_documented_shape():
    verdict = parse_verdict(NEGATIVE_REPLY, model="m")
    assert (verdict.label, verdict.score, verdict.confidence) == ("negative", -0.8, 0.9)
    assert verdict.reason == "penipuan"
    assert verdict.as_dict()["model"] == "m"


def test_parse_verdict_tolerates_fences_indonesian_labels_and_clamps():
    verdict = parse_verdict('```json\n{"label":"Positif","score":3,"confidence":"1.7"}\n```')
    assert verdict.label == "positive"
    assert verdict.score == 1.0
    assert verdict.confidence == 1.0


def test_parse_verdict_fills_a_missing_score_from_the_label():
    assert parse_verdict('{"label":"netral"}').score == 0.0
    assert parse_verdict('{"label":"negative"}').score < 0


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "   ",
        "maaf, saya tidak bisa",
        '{"label":"mixed","score":0}',
        '{"label":"positive","score":-0.5}',
        '{"label":"negative","score":"banyak"}',
        '{"label":"negative","score":true}',
        '{"label": "negative"',
    ],
)
def test_parse_verdict_rejects_untrustworthy_replies(reply):
    with pytest.raises(ValueError):
        parse_verdict(reply)


# ----------------------------------------------------------------------
# DeepSeek wire format
# ----------------------------------------------------------------------
def test_deepseek_request_uses_json_mode_without_thinking():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return chat_response(NEGATIVE_REPLY)

    backend = deepseek_backend(handler)
    verdict = make_judge(backend).judge(["Agen BRILink bawa kabur uang nasabah"])[0]

    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    body = seen["body"]
    assert body["model"] == "deepseek-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}
    assert body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert body["messages"][1]["content"] == "Agen BRILink bawa kabur uang nasabah"
    # DeepSeek JSON mode requires the word "json" in the prompt.
    assert "json" in SYSTEM_PROMPT.lower()

    assert verdict.available and verdict.label == "negative"
    assert verdict.model == "deepseek-flash"
    assert (backend.prompt_tokens, backend.completion_tokens) == (120, 30)


def test_deepseek_long_documents_are_truncated_before_sending():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content)["messages"][1]["content"])
        return chat_response(NEGATIVE_REPLY)

    make_judge(deepseek_backend(handler)).judge(["kata " * 5000])
    assert len(sent[0]) <= MAX_INPUT_CHARS


def test_deepseek_empty_content_is_retried():
    replies = iter([chat_response(""), chat_response(NEGATIVE_REPLY)])
    judge = make_judge(deepseek_backend(lambda request: next(replies)))
    verdict = judge.judge(["teks"])[0]
    assert verdict.available and verdict.label == "negative"
    assert judge.failures == 0


def test_deepseek_rate_limit_and_timeout_are_retried():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        if len(calls) == 2:
            raise httpx.ReadTimeout("slow", request=request)
        return chat_response(NEGATIVE_REPLY)

    verdict = make_judge(deepseek_backend(handler)).judge(["teks"])[0]
    assert verdict.available
    assert len(calls) == 3


def test_deepseek_insufficient_balance_disables_the_judge_for_the_run():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})

    judge = make_judge(deepseek_backend(handler))
    verdicts = judge.judge(["a", "b", "c"])
    assert [v.available for v in verdicts] == [False, False, False]
    assert len(calls) == 1, "must not keep calling after a fatal error"
    assert "Insufficient Balance" in judge.stats()["disabled_reason"]
    assert not judge.available


def test_deepseek_bad_request_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "bad model"}})

    judge = make_judge(deepseek_backend(handler))
    assert not judge.judge(["teks"])[0].available
    assert len(calls) == 1
    assert judge.available, "a 400 is per-request, the judge stays on"


# ----------------------------------------------------------------------
# Judge behaviour (provider-independent)
# ----------------------------------------------------------------------
def test_persistent_garbage_gives_up_after_the_retry_budget():
    backend = ScriptedBackend("bukan json")
    judge = make_judge(backend, llm_max_retries=3)
    assert not judge.judge(["teks"])[0].available
    assert len(backend.prompts) == 3
    assert judge.failures == 1


def test_document_budget_caps_calls_per_run():
    backend = ScriptedBackend(NEGATIVE_REPLY)
    judge = make_judge(backend, llm_max_documents=2)
    first = judge.judge(["a", "b", "c"])
    second = judge.judge(["d"])
    assert [v.available for v in first + second] == [True, True, False, False]
    assert len(backend.prompts) == 2
    assert judge.stats()["over_budget"] == 2


def test_judge_is_off_by_default():
    assert SentimentSettings().enable_llm_judge is False
    judge = LLMJudge()
    judge.enabled = False
    assert not judge.available
    assert not judge.judge(["teks"])[0].available


@pytest.mark.parametrize(
    ("provider", "deepseek", "anthropic", "expected"),
    [
        ("auto", "sk-ds", None, "deepseek"),
        ("auto", "sk-ds", "sk-ant", "deepseek"),
        ("auto", None, "sk-ant", "anthropic"),
        ("anthropic", "sk-ds", "sk-ant", "anthropic"),
        ("deepseek", None, "sk-ant", None),
        ("auto", "  ", None, None),
    ],
)
def test_provider_resolution(provider, deepseek, anthropic, expected):
    resolved = configured_judge(provider, deepseek, anthropic)._resolve_provider()
    assert (resolved[0] if resolved else None) == expected


def test_enabled_without_a_key_degrades_to_unavailable():
    judge = configured_judge("deepseek", deepseek=None)
    assert not judge.available
    assert not judge.judge(["teks"])[0].available


def test_enabled_deepseek_builds_a_backend_without_calling_it():
    judge = configured_judge("auto", deepseek="sk-ds")
    assert judge.available
    stats = judge.stats()
    assert (stats["provider"], stats["model"]) == ("deepseek", "deepseek-flash")
    assert stats["documents_sent"] == 0
    judge.close()


# ----------------------------------------------------------------------
# Ensemble integration
# ----------------------------------------------------------------------
CONFLICT = ScoreInput(1, body="penipuan dan penggelapan dana nasabah", rating=1)


def test_confident_llm_verdict_resolves_a_review_case():
    backend = ScriptedBackend(NEGATIVE_REPLY)
    engine = EnsembleSentimentEngine(
        transformer=FakeTransformer(0.9), emotion=None, judge=make_judge(backend)
    )
    result = engine.score_one(CONFLICT)

    assert len(backend.prompts) == 1
    assert result.signals["llm_judge"].available
    assert result.signals_payload()["llm_judge"]["model"] == "fake-model"
    assert result.explanation["llm_reason"] == "penipuan"
    assert not result.requires_review


def test_failed_llm_call_leaves_the_document_for_a_human():
    backend = ScriptedBackend(LLMCallError("down", retryable=True))
    engine = EnsembleSentimentEngine(
        transformer=FakeTransformer(0.9), emotion=None, judge=make_judge(backend)
    )
    result = engine.score_one(CONFLICT)

    assert "llm_judge" not in result.signals
    assert result.requires_review
    assert result.review_reason == "signal_conflict"


def test_confident_documents_never_reach_the_llm():
    backend = ScriptedBackend(NEGATIVE_REPLY)
    engine = EnsembleSentimentEngine(
        transformer=FakeTransformer(-0.85), emotion=None, judge=make_judge(backend)
    )
    engine.score_one(
        ScoreInput(
            1,
            body=(
                "Saldo tertahan tiga hari, biaya admin mahal banget, "
                "aplikasi sering error dan agennya susah dihubungi"
            ),
            rating=1,
        )
    )
    assert backend.prompts == []
