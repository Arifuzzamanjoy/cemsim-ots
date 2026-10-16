"""LLM providers for design-by-prompt and design-by-image.

  gemini              Google Gemini (REST)
  claude              Anthropic Claude (official SDK)
  openai              OpenAI Chat Completions (REST)
  <any name>          any OpenAI-compatible endpoint: OpenRouter, Groq, Together, Mistral, DeepSeek, vLLM, Ollama,
                      LM Studio, Azure-style gateways ... (operator: DUOSIM_PROVIDERS; tenants: workspace keys)

All return JSON text; nothing the model says is executed (see spec.py / builder.py).
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

log = logging.getLogger("duosim.design.llm")

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
    vision: bool  # accepts reference images

    def generate(
        self, system: str, user: str, *, schema: dict | None = None, timeout: float = 120.0, images: list | None = None
    ) -> LLMResult: ...


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
    vision = True

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        transport: Transport | None = None,
        base_url: str = GEMINI_BASE,
        retries: int = 2,
        fallback_models: tuple[str, ...] | None = None,
        thinking: str | None = None,
        name: str | None = None,
    ):
        if name:
            self.name = name
        # Design JSON from a structured prompt does not need deep reasoning, and thinking tokens are
        # latency a user waits for. "low" is the default; DUOSIM_GEMINI_THINKING=default leaves it to
        # the model. Models that do not know thinkingLevel (2.x) answer 400: we then drop it and retry.
        level = thinking if thinking is not None else os.environ.get("DUOSIM_GEMINI_THINKING", "low")
        self._thinking = None if level in ("", "default") else {"thinkingLevel": level}
        self._key = api_key
        self.model = model or os.environ.get("DUOSIM_GEMINI_MODEL") or GEMINI_DEFAULT_MODEL
        if fallback_models is None:
            fallback_models = GEMINI_FALLBACK_MODELS if model is None else ()
        self._models = [self.model, *[m for m in fallback_models if m != self.model]]
        self._transport = transport or _urllib_transport
        self._base = base_url.rstrip("/")
        self._retries = retries

    def generate(
        self, system: str, user: str, *, schema: dict | None = None, timeout: float = 120.0, images: list | None = None
    ) -> LLMResult:
        parts: list[dict[str, Any]] = [{"inline_data": {"mime_type": im.mime, "data": im.b64()}} for im in images or []]
        parts.append({"text": user})
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
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
                hint = f" (model {used!r} not available to this key; set DUOSIM_GEMINI_MODEL)"
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
    """Claude through the official `anthropic` SDK (optional dependency: pip install 'duosim[ai]').

    claude-opus-5-5 rules (see the claude-api reference): thinking is always on (no `thinking`
    parameter), forced `tool_choice` is rejected, so structured JSON comes from
    `output_config.format`.  Server-side refusal fallback ("default") is opted into.
    """

    name = "claude"
    vision = True

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: Any = None,
        fallbacks: bool = True,
        effort: str = "medium",
        name: str | None = None,
    ):
        self.model = model or os.environ.get("DUOSIM_CLAUDE_MODEL") or CLAUDE_DEFAULT_MODEL
        if name:
            self.name = name
        self._fallbacks = fallbacks
        self._effort = effort
        self._key = api_key or ""
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise LLMError("the 'anthropic' package is not installed (pip install 'duosim[ai]')") from None
            self._client = anthropic.Anthropic(api_key=self._key or None, max_retries=2)
        return self._client

    def generate(
        self, system: str, user: str, *, schema: dict | None = None, timeout: float = 120.0, images: list | None = None
    ) -> LLMResult:
        client = self._get_client()
        out_cfg: dict[str, Any] = {"effort": self._effort}
        if schema:
            out_cfg["format"] = {"type": "json_schema", "schema": schema}
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": _claude_content(user, images)}],
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


def _claude_content(user: str, images: list | None) -> Any:
    if not images:
        return user
    blocks: list[dict[str, Any]] = [
        {"type": "image", "source": {"type": "base64", "media_type": im.mime, "data": im.b64()}} for im in images
    ]
    blocks.append({"type": "text", "text": user})
    return blocks


# --------------------------------------------------------------------------- OpenAI and OpenAI-compatible
OPENAI_BASE = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-5-mini"


class OpenAIProvider:
    """Chat Completions over plain HTTPS: OpenAI itself, or any server that speaks the same protocol.

    JSON mode (`response_format: json_object`) is requested; servers that reject it get the request again
    without it (the system prompt already demands one JSON object, and the validator checks the answer)."""

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        *,
        name: str = "openai",
        base_url: str = OPENAI_BASE,
        vision: bool = True,
        transport: Transport | None = None,
        retries: int = 2,
        allow_http: bool = False,
    ):
        self.name = name
        self._key = api_key
        self.model = model or os.environ.get("DUOSIM_OPENAI_MODEL") or OPENAI_DEFAULT_MODEL
        self._base = base_url.rstrip("/")
        self.vision = vision
        self._retries = retries
        self._json_mode = True
        self._official = self._base == OPENAI_BASE
        if transport is None:
            transport = _urllib_transport if not allow_http else _plain_transport
        self._transport = transport

    def generate(
        self, system: str, user: str, *, schema: dict | None = None, timeout: float = 120.0, images: list | None = None
    ) -> LLMResult:
        content: Any = user
        if images:
            content = [{"type": "image_url", "image_url": {"url": im.data_url()}} for im in images]
            content.append({"type": "text", "text": user})
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        }
        # OpenAI's current models take max_completion_tokens; most compatible servers still read max_tokens
        body["max_completion_tokens" if self._official else "max_tokens"] = MAX_OUTPUT_TOKENS
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"content-type": "application/json"}
        if self._key:
            headers["authorization"] = f"Bearer {self._key}"
        url = f"{self._base}/chat/completions"
        deadline = time.monotonic() + timeout
        t0 = time.perf_counter()
        attempt, status, raw = 0, 0, b""
        while True:
            left = deadline - time.monotonic()
            if left < 1.0:
                raise LLMError(f"{self.name} did not answer within {timeout:.0f}s; try again shortly")
            try:
                status, raw = self._transport(url, headers, json.dumps(body).encode(), left)
            except LLMError:
                status, raw = 0, b""
                if attempt >= self._retries:
                    raise
            if status == 400 and "response_format" in body and b"response_format" in raw[:2000]:
                del body["response_format"]  # this server has no JSON mode
                self._json_mode = False
                continue
            if (status not in RETRY_STATUS and status != 0) or attempt >= self._retries:
                break
            attempt += 1
            time.sleep(max(0.0, min(1.5 * attempt, deadline - time.monotonic() - 1.0)))
        dt = time.perf_counter() - t0
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        if status != 200:
            err = data.get("error") if isinstance(data, dict) else None
            msg = err.get("message") if isinstance(err, dict) else (err or raw[:200].decode(errors="replace"))
            hint = {
                401: " (check the API key)",
                403: " (check the API key)",
                404: " (check the model name / base URL)",
                429: " (rate limit / quota)",
            }.get(status, "")
            raise LLMError(f"{self.name} API HTTP {status}: {_redact(str(msg), self._key)[:300]}{hint}")
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.name} returned no choices")
        ch = choices[0]
        msg = ch.get("message") or {}
        if msg.get("refusal"):
            raise LLMError(f"{self.name} declined the request")
        text = msg.get("content") or ""
        if isinstance(text, list):  # some servers return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        if ch.get("finish_reason") == "length":
            raise LLMError(f"{self.name}'s answer was cut off (max tokens); simplify the request")
        if not text.strip():
            raise LLMError(f"{self.name} returned an empty answer")
        u = data.get("usage") or {}
        usage = {"input_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens")}
        return LLMResult(text, self.name, data.get("model") or self.model, round(dt, 3), usage)


def _plain_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    """Operator-configured local endpoints (e.g. Ollama on http://127.0.0.1) may use plain HTTP."""
    if not url.startswith(("https://", "http://")):
        raise LLMError("endpoint must be http(s)")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310 - scheme checked
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"network error talking to the model API: {type(e).__name__}") from None


# --------------------------------------------------------------------------- discovery
def providers_from_env() -> dict[str, Provider]:
    """Providers whose key is configured.  Never raises.

    DUOSIM_PROVIDERS adds OpenAI-compatible endpoints, e.g.
      [{"name": "llama", "base_url": "http://127.0.0.1:11434/v1", "model": "llama3.2-vision", "vision": true},
       {"name": "openrouter", "base_url": "https://openrouter.ai/api/v1", "model": "qwen/qwen3-vl",
        "api_key_env": "OPENROUTER_API_KEY"}]"""
    out: dict[str, Provider] = {}
    gk = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if gk:
        out["gemini"] = GeminiProvider(gk)
    ak = os.environ.get("ANTHROPIC_API_KEY")
    if ak:
        out["claude"] = ClaudeProvider(ak, fallbacks=os.environ.get("DUOSIM_CLAUDE_FALLBACKS", "1") != "0")
    ok = os.environ.get("OPENAI_API_KEY")
    if ok:
        out["openai"] = OpenAIProvider(ok)
    raw = os.environ.get("DUOSIM_PROVIDERS", "").strip()
    if raw:
        try:
            entries = json.loads(raw)
            assert isinstance(entries, list)
        except (ValueError, AssertionError):
            log.warning("DUOSIM_PROVIDERS is not a JSON list; ignored")
            entries = []
        for e in entries:
            try:
                name, base, model = str(e["name"])[:32], str(e["base_url"]), str(e["model"])
            except (KeyError, TypeError):
                log.warning("DUOSIM_PROVIDERS entry needs name, base_url and model; ignored: %r", e)
                continue
            key = os.environ.get(str(e.get("api_key_env", "")), "") if e.get("api_key_env") else ""
            out[name] = OpenAIProvider(
                key, model, name=name, base_url=base, vision=bool(e.get("vision", False)), allow_http=True
            )
    return out


def provider_from_secret(secret: dict[str, Any]) -> Provider:
    """A workspace's own provider (bring-your-own-key), from `SaaS.provider_secret`."""
    t, name, key, model = secret["type"], secret["name"], secret["key"], secret.get("model") or None
    if t == "gemini":
        return GeminiProvider(key, model, name=name)
    if t == "claude":
        return ClaudeProvider(key, model, name=name)
    if t == "openai":
        return OpenAIProvider(key, model, name=name, vision=bool(secret.get("vision", True)))
    if t == "openai_compatible":
        return OpenAIProvider(
            key,
            model,
            name=name,
            base_url=secret["base_url"],
            vision=bool(secret.get("vision")),
            allow_http=os.environ.get("DUOSIM_ALLOW_PRIVATE_URLS") == "1",
        )
    raise LLMError(f"unknown provider type {t!r}")
