"""Design agent (plan / retrieve / generate / critique / repair / build / judge), the geometric critic, the
template library, reference images and the OpenAI-compatible provider."""

import base64
import json
import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from duosim import api_design, server
from duosim.design import agent as agent_mod
from duosim.design.agent import DesignAgent
from duosim.design.critic import aabb, critique
from duosim.design.inputs import ImageError, parse_image, parse_images
from duosim.design.llm import (
    ClaudeProvider,
    GeminiProvider,
    LLMError,
    LLMResult,
    OpenAIProvider,
    provider_from_secret,
    providers_from_env,
)
from duosim.design.service import DesignError, DesignService
from duosim.design.spec import parse_spec
from duosim.design.templates import TEMPLATES, search
from duosim.platform.store import Store


def png(w=16, h=12) -> bytes:
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def data_url(b: bytes, mime="image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(b).decode()}"


GOOD = {"name": "Tank", "parts": [
    {"id": "slab", "kind": "box", "size": [6, 6, 0.4], "at": [-3, -3, -0.4]},
    {"id": "tank_shell", "kind": "cylinder", "radius": 2, "height": 5, "at": [0, 0, 0]},
    {"id": "outlet", "kind": "pipe", "radius": 0.15, "path": [[2, 0, 0.8], [2.0, 0, 3]]},
]}  # fmt: skip
FLOATING = {"name": "Bad", "parts": [
    {"id": "slab", "kind": "box", "size": [6, 6, 0.4], "at": [-3, -3, 0]},
    {"id": "fan", "kind": "cylinder", "radius": 1, "height": 1, "at": [10, 10, 6]},
    {"id": "duct", "kind": "pipe", "radius": 0.2, "path": [[30, 0, 1], [40, 0, 1]]},
]}  # fmt: skip
PLAN = {"summary": "a tank on a slab", "components": [{"name": "tank shell"}, {"name": "slab"}], "overall": {"length_m": 6, "width_m": 6, "height_m": 5},
        "assumptions": ["water service"], "questions": ["what capacity?"]}  # fmt: skip


class Script:
    """A provider that answers by system prompt: plan / spec (in order) / judge."""

    name, model = "script", "s-1"

    def __init__(self, specs, plan=PLAN, judge=None, vision=True):
        self.specs, self.plan, self.judge, self.vision = list(specs), plan, judge, vision
        self.calls = []

    def generate(self, system, user, *, schema=None, timeout=0, images=None):
        kind = "plan" if "PLAN" in system else "judge" if "reviewer" in system else "spec"
        self.calls.append((kind, len(images or []), user))
        if kind == "plan":
            body = self.plan
        elif kind == "judge":
            body = self.judge
        else:
            body = self.specs.pop(0) if len(self.specs) > 1 else self.specs[0]
        if isinstance(body, Exception):
            raise body
        return LLMResult(
            body if isinstance(body, str) else json.dumps(body), "script", "s-1", 0.01, {"output_tokens": 10}
        )


# --------------------------------------------------------------------------- critic
def test_critic_accepts_a_sound_design_and_flags_floating_parts_and_open_pipes():
    good = critique(parse_spec(GOOD)[0], PLAN)
    assert good.score >= 90 and good.errors == 0, good.feedback()
    bad = critique(parse_spec(FLOATING)[0])
    rules = {f.rule for f in bad.findings}
    assert {"floating", "open_pipe", "islands"} <= rules and bad.errors >= 1 and bad.score < good.score
    assert "fan floats" in bad.feedback() and "Fix:" in bad.feedback()


def test_critic_checks_scale_and_coverage_against_the_plan():
    plan = {"components": [{"name": "stack"}], "overall": {"length_m": 60, "width_m": 6}}
    c = critique(parse_spec(GOOD)[0], plan)
    rules = [f.rule for f in c.findings]
    assert "scale" in rules and "coverage" in rules


def test_aabb_applies_yaw_and_axes():
    spec, _ = parse_spec({"name": "x", "parts": [{"id": "b", "kind": "box", "size": [2, 1, 1], "at": [0, 0, 0], "yaw_deg": 90},
                                                 {"id": "c", "kind": "cylinder", "radius": 1, "height": 4, "axis": "-z", "at": [0, 0, 4]}]})  # fmt: skip
    assert aabb(spec.parts[0]) == pytest.approx([-1, 0, 0, 0, 2, 1])
    assert aabb(spec.parts[1]) == pytest.approx([-1, -1, 0, 1, 1, 4])


def test_every_template_is_a_valid_spec_and_retrieval_ranks_by_relevance():
    for t in TEMPLATES:
        spec, warns = parse_spec(t.spec())
        assert spec.parts and not warns, t.id
        assert critique(spec).errors == 0, (t.id, critique(spec).feedback())
    assert search("rotary kiln with tyres and girth gear")[0].id == "rotary_kiln"
    assert search("bag filter dedusting")[0].id == "bag_filter"
    assert search("") == [] and search("zzzz qqqq") == []


# --------------------------------------------------------------------------- agent
def test_fast_mode_is_a_single_call_with_validator_repair():
    p = Script(["not json at all", GOOD])
    r = DesignAgent(p, mode="fast").run("a tank")
    assert [c[0] for c in p.calls] == ["spec", "spec"] and r.llm_calls == 2 and r.plan is None
    assert "rejected by the validator" in p.calls[1][2]
    steps = [s.name for s in r.trace]
    assert steps[:3] == ["intake", "plan", "retrieve"] and r.trace[1].status == "skipped"


def test_standard_mode_plans_retrieves_and_repairs_from_the_critique():
    p = Script([FLOATING, GOOD])
    r = DesignAgent(p, mode="standard").run("a water tank on a slab with an outlet pipe")
    kinds = [c[0] for c in p.calls]
    assert kinds == ["plan", "spec", "spec"], kinds
    gen_prompt = p.calls[1][2]
    assert "Engineering plan" in gen_prompt and "Reference patterns" in gen_prompt and "vertical_tank" not in gen_prompt
    assert "Vertical storage tank" in gen_prompt, "retrieved template is shown to the model"
    assert "A reviewer found these problems" in p.calls[2][2] and "fan floats" in p.calls[2][2]
    assert r.spec.name == "Tank" and r.critique.score >= 90 and r.plan["questions"] == ["what capacity?"]
    decisions = [s.detail.get("decision") for s in r.trace if s.name == "decide"]
    assert any(d and d.startswith("repair") for d in decisions)


def test_a_repair_that_makes_things_worse_is_discarded():
    p = Script([GOOD, FLOATING])
    a = DesignAgent(p, mode="standard")
    # force a repair round by lowering the acceptance bar
    agent_mod.ACCEPT_SCORE, saved = 101, agent_mod.ACCEPT_SCORE
    try:
        r = a.run("a tank")
    finally:
        agent_mod.ACCEPT_SCORE = saved
    assert r.spec.name == "Tank", "the worse repair (floating parts) was not taken"
    assert any(s.detail.get("kept") is False for s in r.trace)


def test_deep_mode_generates_two_candidates_builds_and_runs_the_visual_judge():
    class Built:
        def __init__(self, spec):
            self.ok, self.png, self.checks, self.spec = True, png(), [], spec

    judge = {
        "score": 4,
        "verdict": "the tank floats",
        "issues": [{"parts": ["tank_shell"], "problem": "too small", "fix": "radius 3 m"}],
    }
    p = Script([GOOD, FLOATING, GOOD], judge=judge)
    builds = []

    def builder(spec):
        builds.append(spec)
        return Built(spec)

    r = DesignAgent(p, mode="deep", builder=builder).run("a water tank", images=[parse_image(data_url(png()))])
    kinds = [c[0] for c in p.calls]
    assert kinds.count("spec") >= 3 and "judge" in kinds and kinds[0] == "plan"
    judge_call = next(c for c in p.calls if c[0] == "judge")
    assert judge_call[1] == 2, "the judge sees the FreeCAD render and the reference image"
    crit = next(s for s in r.trace if s.name == "critique")
    assert set(crit.detail["candidates"]) == {"A", "B"} and crit.detail["chosen"] in ("A", "B")
    assert r.judge["score"] == 4 and len(builds) == 2, "rebuilt after the visual repair"
    assert r.llm_calls <= agent_mod.BUDGET["deep"]


def test_images_need_a_vision_provider_and_provider_failures_are_upstream():
    with pytest.raises(DesignError, match="does not accept images"):
        DesignAgent(Script([GOOD], vision=False), mode="fast").run("x", images=[parse_image(data_url(png()))])
    with pytest.raises(DesignError) as e:
        DesignAgent(Script([LLMError("quota")]), mode="fast").run("x")
    assert e.value.upstream
    # a plan failure is not fatal: the agent continues without a plan
    r = DesignAgent(Script([GOOD], plan="garbage"), mode="standard").run("x")
    assert r.plan is None and r.spec.name == "Tank"


def test_budget_is_never_exceeded():
    p = Script(["{}"])  # always invalid
    with pytest.raises(DesignError):
        DesignAgent(p, mode="standard").run("x")
    assert len(p.calls) <= agent_mod.BUDGET["standard"]


# --------------------------------------------------------------------------- reference images
def test_images_are_sniffed_not_trusted():
    im = parse_image(data_url(png(40, 30)))
    assert im.mime == "image/png" and (im.width, im.height) == (40, 30)
    jpeg_claim_png = parse_image(data_url(png(), "image/jpeg"))
    assert jpeg_claim_png.mime == "image/png", "the bytes decide, not the declared type"
    for bad, needle in [
        ("http://x/y.png", "data:image"),
        (data_url(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/png"), "not a PNG"),
        ("data:image/png;base64,@@@", "base64"),
        (data_url(png(4, 4)), "outside the accepted size"),
    ]:
        with pytest.raises(ImageError, match=needle):
            parse_image(bad)
    with pytest.raises(ImageError, match="at most"):
        parse_images([data_url(png())] * 5)


# --------------------------------------------------------------------------- providers
class FakeTransport:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, json.loads(body)))
        st, data = self.replies.pop(0)
        return st, json.dumps(data).encode() if not isinstance(data, bytes) else data


def ok_chat(text='{"name":"x","parts":[]}'):
    return 200, {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        "model": "m",
    }


def test_openai_provider_request_shape_images_and_secret_handling():
    t = FakeTransport(ok_chat())
    p = OpenAIProvider("sk-SECRET", "gpt-x", transport=t)
    r = p.generate("SYS", "USER", images=[parse_image(data_url(png()))])
    url, headers, body = t.calls[0]
    assert url == "https://api.openai.com/v1/chat/completions" and headers["authorization"] == "Bearer sk-SECRET"
    assert body["response_format"] == {"type": "json_object"} and "max_completion_tokens" in body
    content = body["messages"][1]["content"]
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,") and content[-1]["text"] == "USER"
    assert r.usage == {"input_tokens": 5, "output_tokens": 7}
    bad = OpenAIProvider(
        "sk-SECRET", "gpt-x", transport=FakeTransport((401, {"error": {"message": "bad key sk-SECRET"}}))
    )
    with pytest.raises(LLMError) as e:
        bad.generate("s", "u")
    assert "sk-SECRET" not in str(e.value) and "check the API key" in str(e.value)


def test_openai_compatible_server_without_json_mode_is_retried_without_it(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    t = FakeTransport((400, {"error": {"message": "response_format is not supported"}}), (503, {}), ok_chat())
    p = OpenAIProvider("", "llama", name="local", base_url="http://127.0.0.1:11434/v1", transport=t, vision=False)
    p.generate("s", "u")
    assert (
        "response_format" in t.calls[0][2] and "response_format" not in t.calls[1][2] and "max_tokens" in t.calls[0][2]
    )
    assert "authorization" not in t.calls[0][1], "no key, no header"
    for status, data, needle in [(200, {"choices": []}, "no choices"), (200, {"choices": [{"message": {"content": ""}}]}, "empty"),
                                 (200, {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}, "cut off")]:  # fmt: skip
        with pytest.raises(LLMError, match=needle):
            OpenAIProvider("k", "m", transport=FakeTransport((status, data))).generate("s", "u")


def test_gemini_and_claude_send_images_in_their_native_shapes():
    t = FakeTransport((200, {"candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}]}))
    GeminiProvider("k", "gemini-x", transport=t, thinking="").generate("s", "u", images=[parse_image(data_url(png()))])
    parts = t.calls[0][2]["contents"][0]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/png" and parts[-1] == {"text": "u"}

    seen = {}

    class Msgs:
        def create(self, **kw):
            seen.update(kw)

            class R:
                stop_reason, content, usage, _request_id = (
                    "end_turn",
                    [type("B", (), {"type": "text", "text": "{}"})()],
                    None,
                    "r",
                )

            return R()

    class Client:
        messages = Msgs()
        beta = type("Beta", (), {"messages": Msgs()})()

    ClaudeProvider("k", client=Client(), fallbacks=False).generate("s", "u", images=[parse_image(data_url(png()))])
    blocks = seen["messages"][0]["content"]
    assert (
        blocks[0]["type"] == "image"
        and blocks[0]["source"]["media_type"] == "image/png"
        and blocks[-1] == {"type": "text", "text": "u"}
    )
    assert seen["model"] == "claude-opus-5-5"


def test_provider_discovery_from_env_and_workspace_secrets(monkeypatch):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("LOCAL_KEY", "lk")
    monkeypatch.setenv(
        "DUOSIM_PROVIDERS",
        json.dumps(
            [
                {
                    "name": "local",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": "llava",
                    "vision": True,
                    "api_key_env": "LOCAL_KEY",
                },
                {"broken": 1},
            ]
        ),
    )
    ps = providers_from_env()
    assert (
        set(ps) == {"openai", "local"}
        and ps["local"].vision
        and ps["local"].model == "llava"
        and ps["local"]._key == "lk"
    )
    p = provider_from_secret(
        {
            "type": "openai_compatible",
            "name": "or",
            "key": "k",
            "model": "m",
            "base_url": "https://openrouter.ai/api/v1",
            "vision": True,
        }
    )
    assert p.name == "or" and p._base == "https://openrouter.ai/api/v1"
    assert provider_from_secret({"type": "claude", "name": "c", "key": "k"}).name == "c"
    with pytest.raises(LLMError):
        provider_from_secret({"type": "other", "name": "x", "key": "k"})


# --------------------------------------------------------------------------- HTTP: generate with images and modes
@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.delenv("DUOSIM_DEV_OPEN", raising=False)
    store = Store(tmp_path / "p.sqlite3")
    prov = Script([GOOD])
    monkeypatch.setattr(api_design, "_STORE", store)
    monkeypatch.setattr(api_design, "_SERVICE", DesignService({"script": prov}))
    monkeypatch.setattr(api_design, "_ENV_LOADED", True)
    monkeypatch.setattr(api_design, "get_builder", lambda: None)
    monkeypatch.setattr(api_design, "get_client", lambda: None)
    key = store.create_api_key(store.workspace("acme"), "t")[1]
    return TestClient(server.app), {"X-API-Key": key}, prov


def test_generate_from_an_image_reports_the_agent_and_charges_the_image(http):
    c, k, prov = http
    r = c.post(
        "/api/design/generate",
        json={"prompt": "", "images": [data_url(png())], "mode": "standard", "build": False},
        headers=k,
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert (
        j["agent"]["mode"] == "standard" and j["agent"]["critique"]["score"] >= 90 and j["credits"]["charged"] == 4 + 2
    )
    assert all(n == 1 for kind, n, _ in prov.calls if kind != "judge"), "every model call saw the image"
    assert "reference image" in prov.calls[0][2]


def test_generate_validates_inputs_before_charging(http):
    c, k, _ = http
    bal = c.get("/api/design/status", headers=k).json()["credits"]
    for body, code in [({"prompt": "", "build": False}, 422), ({"prompt": "x", "mode": "turbo"}, 422),
                       ({"prompt": "x", "images": ["data:image/png;base64,AAAA"]}, 422), ({"prompt": "x", "provider": "nope"}, 422)]:  # fmt: skip
        assert c.post("/api/design/generate", json=body, headers=k).status_code == code, body
    assert c.get("/api/design/status", headers=k).json()["credits"] == bal
    st = c.get("/api/design/status", headers=k).json()
    assert (
        st["modes"] == ["fast", "standard"] and ".step" in st["import_formats"] and st["costs"]["generate.deep"] == 10
    )
