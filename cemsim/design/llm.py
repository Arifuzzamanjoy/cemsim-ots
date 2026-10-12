"""LLM providers for design-by-prompt: Google Gemini (REST) and Anthropic Claude (official SDK).

Both return JSON text; nothing the model says is executed (see spec.py / builder.py).
API keys come from the environment (or a git-ignored `.env`) and are sent only in request
headers - never in a URL, a log line or an error message.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("cemsim.design.llm")

# `gemini-flash-latest` tracks the current Flash; the pinned model is tried if it is unavailable/overloaded.
# (gemini-2.5-* answers 404 "no longer available to new users"; Pro models need a paid quota.)
GEMINI_DEFAULT_MODEL = "gemini-flash-latest"
GEMINI_FALLBACK_MODELS = ("gemini-3.8-flash",)
CLAUDE_DEFAULT_MODEL = "claude-opus-5-5"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
MAX_OUTPUT_TOKENS = 16000
RETRY_STATUS = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """The provider call failed (message is safe to show to users: no secrets)."""


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    latency_s: float
    usage: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str
    model: str

    def generate(self, system: str, user: str, *, schema: dict | None = None, timeout: float = 120.0) -> LLMResult: ...


def load_dotenv(path: str | Path) -> int:
    """Load KEY=VALUE lines into os.environ without overriding variables already set.
    Returns the number of variables set.  (Tiny on purpose: no extra dependency.)"""
    p = Path(path)
    n = 0
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return 0
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        k, v = k.strip(), v.strip().strip("'\"")
        if k and v and k not in os.environ:
            os.environ[k] = v
            n += 1
    return n


def url_for(base: str, model: str) -> str:
    return f"{base}/models/{model}:generateContent"


def _redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


# --------------------------------------------------------------------------- Gemini
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


def _urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    if not url.startswith("https://"):
        raise LLMError("refusing a non-HTTPS LLM endpoint")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310 - https checked
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"network error talking to the model API: {type(e).__name__}") from None


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        transport: Transport | None = None,
        base_url: str = GEMINI_BASE,
        retries: int = 2,
        fallback_models: tuple[str, ...] | None = None,
        thinking: str | None = None,
    ):
        # Design JSON from a structured prompt does not need deep reasoning, and thinking tokens are
        # latency a user waits for. "low" is the default; CEMSIM_GEMINI_THINKING=default leaves it to
        # the model. Models that do not know thinkingLevel (2.x) answer 400: we then drop it and retry.
        level = thinking if thinking is not None else os.environ.get("CEMSIM_GEMINI_THINKING", "low")
        self._thinking = None if level in ("", "default") else {"thinkingLevel": level}
        self._key = api_key
        self.model = model or os.environ.get("CEMSIM_GEMINI_MODEL") or GEMINI_DEFAULT_MODEL
        if fallback_models is None:
            fallback_models = GEMINI_FALLBACK_MODELS if model is None else ()
        self._models = [self.model, *[m for m in fallback_models if m != self.model]]
        self._transport = transport or _urllib_transport
        self._base = base_url.rstrip("/")
        self._retries = retries

    def generate(self, system: str, user: str, *, schema: dict | None = None, timeout: float = 120.0) -> LLMResult:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.2,
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
            },
        }
        if self._thinking:
            body["generationConfig"]["thinkingConfig"] = dict(self._thinking)
        headers = {"content-type": "application/json", "x-goog-api-key": self._key}
        t0 = time.perf_counter()
        # ONE wall-clock budget for everything below (retries and fallback models included): a user is
        # waiting, and per-attempt timeouts multiplied by retries and models would add up to many minutes.
        deadline = time.monotonic() + timeout
        floor = min(2.0, timeout / 10)
        status, raw, used = 0, b"", self.model
        failure: LLMError | None = None
        for used in self._models:
            attempt = 0
            while True:
                left = deadline - time.monotonic()
                if left < floor:
                    raise LLMError(
                        f"Gemini did not answer within {timeout:.0f}s (the model API is slow or overloaded); try again shortly"
                    )
                failure = None
                try:
                    status, raw = self._transport(url_for(self._base, used), headers, json.dumps(body).encode(), left)
                except LLMError as e:  # network error / timeout on this attempt
                    status, raw, failure = 0, b"", e
                if status == 400 and "thinkingConfig" in body["generationConfig"] and b"hink" in raw[:600]:
                    del body["generationConfig"]["thinkingConfig"]  # this model has no thinking control
                    self._thinking = None  # remember it: no wasted round trip on later requests
                    continue  # same attempt number: this is not a retry of a transient failure
                if (status not in RETRY_STATUS and status != 0) or attempt >= self._retries:
                    break
                attempt += 1
                time.sleep(max(0.0, min(1.5 * attempt, deadline - time.monotonic() - floor)))
            if status not in (0, 404, 503):  # model gone / overloaded / silent: try the next one, else stop here
                break
            log.warning("Gemini model %s unavailable (HTTP %s)", used, status or "no answer")
        if status == 0 and failure is not None:
            raise failure
        dt = time.perf_counter() - t0
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        if status != 200:
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            msg = _redact(str(err.get("message") or raw[:200].decode(errors="replace")), self._key)
            hint = ""
            if status in (401, 403):
                hint = " (check GEMINI_API_KEY)"
            elif status == 404:
                hint = f" (model {used!r} not available to this key; set CEMSIM_GEMINI_MODEL)"
            elif status == 429:
                hint = " (rate limit / quota)"
            raise LLMError(f"Gemini API HTTP {status}: {msg[:300]}{hint}")
        cands = data.get("candidates") or []
        if not cands:
            fb = data.get("promptFeedback") or {}
            raise LLMError(f"Gemini returned no candidates (blockReason={fb.get('blockReason')!r})")
        c0 = cands[0]
        parts = (c0.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        fin = c0.get("finishReason")
        if fin not in (None, "STOP"):
            raise LLMError(f"Gemini stopped early (finishReason={fin})")
        if not text.strip():
            raise LLMError("Gemini returned an empty answer")
        um = data.get("usageMetadata") or {}
        usage = {
            "input_tokens": um.get("promptTokenCount"),
            "output_tokens": um.get("candidatesTokenCount"),
            "thinking_tokens": um.get("thoughtsTokenCount"),
        }
        return LLMResult(text, self.name, used, round(dt, 3), usage)


# --------------------------------------------------------------------------- Claude
class ClaudeProvider:
    """Claude through the official `anthropic` SDK (optional dependency: pip install 'cemsim-ots[ai]').

    claude-opus-5-5 rules (see the claude-api reference): thinking is always on (no `thinking`
    parameter), forced `tool_choice` is rejected, so structured JSON comes from
    `output_config.format`.  Server-side refusal fallback ("default") is opted into.
    """

    name = "claude"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: Any = None,
        fallbacks: bool = True,
        effort: str = "medium",
    ):
        self.model = model or os.environ.get("CEMSIM_CLAUDE_MODEL") or CLAUDE_DEFAULT_MODEL
        self._fallbacks = fallbacks
        self._effort = effort
        self._key = api_key or ""
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise LLMError("the 'anthropic' package is not installed (pip install 'cemsim-ots[ai]')") from None
            self._client = anthropic.Anthropic(api_key=self._key or None, max_retries=2)
        return self._client

    def generate(self, system: str, user: str, *, schema: dict | None = None, timeout: float = 120.0) -> LLMResult:
        client = self._get_client()
        out_cfg: dict[str, Any] = {"effort": self._effort}
        if schema:
            out_cfg["format"] = {"type": "json_schema", "schema": schema}
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": out_cfg,
            "timeout": timeout,
        }
        t0 = time.perf_counter()
        try:
            if self._fallbacks:
                resp = client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            else:
                resp = client.messages.create(**kwargs)
        except LLMError:
            raise
        except Exception as e:  # SDK typed errors: keep the class name, drop anything secret
            name = type(e).__name__
            msg = _redact(str(getattr(e, "message", "") or e), self._key)
            raise LLMError(f"Claude API error ({name}): {msg[:300]}") from None
        dt = time.perf_counter() - t0
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            det = getattr(resp, "stop_details", None)
            raise LLMError(f"Claude declined the request (category={getattr(det, 'category', None)!r})")
        if stop == "max_tokens":
            raise LLMError("Claude's answer was cut off (max_tokens); simplify the request")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        if not text.strip():
            raise LLMError("Claude returned no text")
        u = getattr(resp, "usage", None)
        usage = {
            "input_tokens": getattr(u, "input_tokens", None),
            "output_tokens": getattr(u, "output_tokens", None),
            "request_id": getattr(resp, "_request_id", None),
        }
        return LLMResult(text, self.name, self.model, round(dt, 3), usage)


# --------------------------------------------------------------------------- discovery
def providers_from_env() -> dict[str, Provider]:
    """Providers whose key is configured.  Never raises."""
    out: dict[str, Provider] = {}
    gk = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if gk:
        out["gemini"] = GeminiProvider(gk)
    ak = os.environ.get("ANTHROPIC_API_KEY")
    if ak:
        out["claude"] = ClaudeProvider(ak, fallbacks=os.environ.get("CEMSIM_CLAUDE_FALLBACKS", "1") != "0")
    return out
