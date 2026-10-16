"""LLM providers (fake transports - no network), prompt service and repair loop."""

import json
from types import SimpleNamespace

import pytest

from duosim.design.llm import (
    CLAUDE_DEFAULT_MODEL,
    GEMINI_DEFAULT_MODEL,
    ClaudeProvider,
    GeminiProvider,
    LLMError,
    LLMResult,
    load_dotenv,
    providers_from_env,
)
from duosim.design.service import (
    SPEC_SCHEMA,
    SYSTEM_PROMPT,
    DesignError,
    DesignService,
    extract_json,
    sanitize_prompt,
)

KEY = "AQ.test-secret-key-0123456789"
SPEC_JSON = json.dumps({"name": "n", "parts": [{"id": "s", "kind": "sphere", "radius": 1, "at": [0, 0, 0]}]})


def gemini_ok(text=SPEC_JSON, **extra):
    body = {
        "candidates": [
            {"content": {"parts": [{"text": "hmm", "thought": True}, {"text": text}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20, "thoughtsTokenCount": 5},
    }
    body.update(extra)
    return 200, json.dumps(body).encode()


class FakeTransport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), json.loads(body), timeout))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


# ------------------------------------------------------------------ Gemini
def test_gemini_request_shape_and_secret_handling():
    t = FakeTransport(gemini_ok())
    r = GeminiProvider(KEY, transport=t).generate("SYS", "USER")
    url, headers, body, _ = t.calls[0]
    assert KEY not in url, "the API key must travel in a header, never in the URL"
    assert headers["x-goog-api-key"] == KEY
    assert url.endswith(f"/models/{GEMINI_DEFAULT_MODEL}:generateContent")
    assert body["systemInstruction"]["parts"][0]["text"] == "SYS"
    assert body["contents"][0]["parts"][0]["text"] == "USER"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert r.text == SPEC_JSON, "thought parts must not leak into the answer"
    assert r.usage == {"input_tokens": 10, "output_tokens": 20, "thinking_tokens": 5}


@pytest.mark.parametrize(
    ("status", "needle"),
    [(401, "GEMINI_API_KEY"), (403, "GEMINI_API_KEY"), (429, "quota"), (400, "HTTP 400")],
)
def test_gemini_errors_are_actionable_and_never_echo_the_key(status, needle):
    err = {"error": {"message": f"bad key {KEY} supplied"}}
    t = FakeTransport((status, json.dumps(err).encode()))
    with pytest.raises(LLMError) as e:
        GeminiProvider(KEY, transport=t, retries=0).generate("s", "u")
    assert needle in str(e.value) and KEY not in str(e.value)


def test_gemini_retries_transient_errors_then_succeeds(monkeypatch):
    monkeypatch.setattr("duosim.design.llm.time.sleep", lambda s: None)
    t = FakeTransport((503, b"{}"), (429, b"{}"), gemini_ok())
    assert GeminiProvider(KEY, transport=t).generate("s", "u").text == SPEC_JSON
    assert len(t.calls) == 3


def test_gemini_falls_back_to_the_next_model_when_one_is_gone(monkeypatch):
    monkeypatch.setattr("duosim.design.llm.time.sleep", lambda s: None)
    gone = (404, json.dumps({"error": {"message": "no longer available to new users"}}).encode())
    t = FakeTransport(gone, gemini_ok())
    r = GeminiProvider(KEY, transport=t, retries=0).generate("s", "u")
    assert r.model != GEMINI_DEFAULT_MODEL and t.calls[1][0].split("/models/")[1].split(":")[0] == r.model


@pytest.mark.parametrize(
    "reply",
    [
        (200, json.dumps({"candidates": []}).encode()),
        (
            200,
            json.dumps(
                {"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "MAX_TOKENS"}]}
            ).encode(),
        ),
        (200, json.dumps({"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]}).encode()),
    ],
)
def test_gemini_bad_answers_raise(reply):
    with pytest.raises(LLMError):
        GeminiProvider(KEY, transport=FakeTransport(reply), retries=0).generate("s", "u")


# ------------------------------------------------------------------ Claude
class FakeAnthropic:
    def __init__(self, response=None, exc=None):
        self.calls = []
        self._response, self._exc = response, exc
        outer = self

        class _Msgs:
            def create(self, **kw):
                outer.calls.append(("messages.create", kw))
                return outer._reply()

        class _Beta:
            messages = SimpleNamespace(
                create=lambda **kw: (outer.calls.append(("beta.messages.create", kw)), outer._reply())[1]
            )

        self.messages = _Msgs()
        self.beta = _Beta()

    def _reply(self):
        if self._exc:
            raise self._exc
        return self._response


def claude_resp(text=SPEC_JSON, stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        stop_details=SimpleNamespace(category="cyber"),
        usage=SimpleNamespace(input_tokens=11, output_tokens=22),
        _request_id="req_1",
    )


def test_claude_uses_opus_5_5_structured_output_and_fallback_opt_in():
    c = FakeAnthropic(claude_resp())
    r = ClaudeProvider("k", client=c).generate("SYS", "USER", schema=SPEC_SCHEMA)
    api, kw = c.calls[0]
    assert api == "beta.messages.create" and kw["model"] == CLAUDE_DEFAULT_MODEL == "claude-opus-5-5"
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": SPEC_SCHEMA}
    # claude-opus-5-5 rejects these: they must never be sent
    assert "thinking" not in kw and "tool_choice" not in kw and "temperature" not in kw
    assert kw["system"] == "SYS" and kw["messages"] == [{"role": "user", "content": "USER"}]
    assert r.text == SPEC_JSON and r.usage["output_tokens"] == 22


def test_claude_fallbacks_can_be_disabled():
    c = FakeAnthropic(claude_resp())
    ClaudeProvider("k", client=c, fallbacks=False).generate("s", "u")
    assert c.calls[0][0] == "messages.create" and "fallbacks" not in c.calls[0][1]


def test_claude_refusal_truncation_and_sdk_errors_become_llmerrors():
    with pytest.raises(LLMError, match="declined"):
        ClaudeProvider("k", client=FakeAnthropic(claude_resp(stop="refusal"))).generate("s", "u")
    with pytest.raises(LLMError, match="cut off"):
        ClaudeProvider("k", client=FakeAnthropic(claude_resp(stop="max_tokens"))).generate("s", "u")

    class RateLimitError(Exception):
        message = "slow down sk-ant-SECRET"

    with pytest.raises(LLMError) as e:
        ClaudeProvider("sk-ant-SECRET", client=FakeAnthropic(exc=RateLimitError())).generate("s", "u")
    assert "RateLimitError" in str(e.value) and "sk-ant-SECRET" not in str(e.value)


# ------------------------------------------------------------------ env
def test_dotenv_does_not_override_the_real_environment(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# c\nGEMINI_API_KEY='from-file'\nNEW_ONE=\"v\"\nEMPTY=\n")
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    monkeypatch.delenv("NEW_ONE", raising=False)
    assert load_dotenv(f) == 1
    import os

    assert os.environ["GEMINI_API_KEY"] == "from-env" and os.environ["NEW_ONE"] == "v"
    monkeypatch.delenv("NEW_ONE")


def test_providers_follow_the_environment(monkeypatch):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert providers_from_env() == {}
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert set(providers_from_env()) == {"gemini", "claude"}


# ------------------------------------------------------------------ service
class Scripted:
    name, model = "fake", "fake-1"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def generate(self, system, user, *, schema=None, timeout=0):
        self.calls.append(user)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return LLMResult(a, "fake", "fake-1", 0.5, {"input_tokens": 1, "output_tokens": 2})


def test_service_returns_a_valid_spec_first_time():
    g = DesignService({"fake": Scripted(SPEC_JSON)}).generate("a ball")
    assert g.attempts == 1 and g.spec.parts[0].id == "s" and g.provider == "fake"


def test_service_repairs_once_with_the_validator_messages():
    bad = json.dumps({"parts": [{"id": "s", "kind": "sphere", "at": [0, 0, 0]}]})  # no radius
    p = Scripted(bad, "```json\n" + SPEC_JSON + "\n```")
    g = DesignService({"fake": p}).generate("a ball")
    assert g.attempts == 2 and g.usage["output_tokens"] == 4
    assert "s.radius" in p.calls[1] or "radius" in p.calls[1]  # the model sees what was wrong


def test_service_gives_up_after_the_repair_budget():
    p = Scripted("not json", "still not json")
    with pytest.raises(DesignError) as e:
        DesignService({"fake": p}).generate("a ball")
    assert not e.value.upstream and e.value.errors and len(p.calls) == 2


def test_service_marks_provider_failures_as_upstream():
    with pytest.raises(DesignError) as e:
        DesignService({"fake": Scripted(LLMError("quota"))}).generate("a ball")
    assert e.value.upstream


def test_service_provider_selection_and_prompt_sanitising():
    svc = DesignService({"a": Scripted(), "b": Scripted()}, default="b")
    assert svc.default == "b" and [p["default"] for p in svc.available()] == [False, True]
    with pytest.raises(DesignError, match="unknown provider"):
        svc.generate("x", "zzz")
    with pytest.raises(DesignError, match="no LLM provider"):
        DesignService({}).generate("x")
    assert sanitize_prompt("  hi\x00 there\x07  ") == "hi there"
    for bad in ("", "   ", "x" * 2001, 5):
        with pytest.raises(ValueError):
            sanitize_prompt(bad)


def test_extract_json_tolerates_fences_and_prose_but_not_garbage():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"a": 1} Hope that helps.') == {"a": 1}
    for bad in ("", "no braces", "{broken"):
        with pytest.raises(ValueError):
            extract_json(bad)


def test_system_prompt_documents_every_kind_and_axis():
    for word in ("box", "cylinder", "cone", "sphere", "tube", "pipe", '"-z"', "metres", "JSON"):
        assert word in SYSTEM_PROMPT


def test_gemini_asks_for_low_thinking_by_default_and_can_be_told_not_to():
    t = FakeTransport(gemini_ok())
    GeminiProvider(KEY, transport=t).generate("s", "u")
    assert t.calls[0][2]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    t2 = FakeTransport(gemini_ok())
    GeminiProvider(KEY, transport=t2, thinking="default").generate("s", "u")
    assert "thinkingConfig" not in t2.calls[0][2]["generationConfig"]


def test_models_without_thinking_control_are_retried_once_without_it_and_remembered():
    reject = (400, json.dumps({"error": {"message": "Unknown name thinkingLevel: Cannot find field."}}).encode())
    t = FakeTransport(reject, gemini_ok(), gemini_ok())
    g = GeminiProvider(KEY, transport=t, retries=0)
    assert g.generate("s", "u").text == SPEC_JSON
    assert "thinkingConfig" in t.calls[0][2]["generationConfig"]
    assert "thinkingConfig" not in t.calls[1][2]["generationConfig"], "retried without it"
    g.generate("s", "u")
    assert len(t.calls) == 3 and "thinkingConfig" not in t.calls[2][2]["generationConfig"], "and remembered"


def test_other_400s_are_reported_not_retried():
    bad = (400, json.dumps({"error": {"message": "Request payload size exceeds the limit"}}).encode())
    t = FakeTransport(bad)
    with pytest.raises(LLMError, match="HTTP 400"):
        GeminiProvider(KEY, transport=t, retries=2).generate("s", "u")
    assert len(t.calls) == 1
