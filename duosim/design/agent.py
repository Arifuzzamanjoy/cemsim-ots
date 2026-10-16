"""Design agent: a budgeted, traceable workflow from a brief (text, reference images, an imported CAD draft)
to a validated, critiqued and - when a FreeCAD worker is available - built and visually judged design.

    intake -> plan -> retrieve -> generate (N candidates) -> validate/repair -> critique -> select
           -> [repair from critique] -> build + verify -> [visual judge -> repair -> rebuild]

The steps follow what works in recent text/image-to-CAD research: an explicit plan before geometry
(plan-and-solve), retrieved worked examples (retrieval-augmented generation), validator-in-the-loop repair
(self-refine / Reflexion style feedback, but from deterministic checkers rather than self-assessment),
best-of-N selection by a verifier score (self-consistency), exact kernel measurements (the FreeCAD
verification in builder.py) combined with a vision-language judge of the rendered result.  Every LLM call
is counted against a mode budget and a wall-clock deadline, every decision is recorded in the trace, and a
later step can never make the design worse: a repair is kept only if its critic score does not drop.

Modes      LLM calls (max)   what runs
fast       2                 generate + one validation repair (the original single-shot behaviour)
standard   5                 + plan, retrieval, critique and one critique-driven repair
deep       10                + two candidates, a second repair round, visual judge of the FreeCAD render
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from . import templates
from .critic import Critique, critique
from .llm import LLMError, Provider
from .service import SPEC_SCHEMA, SYSTEM_PROMPT, DesignError, _add_usage, extract_json, sanitize_prompt
from .spec import Spec, SpecError, parse_spec

log = logging.getLogger("duosim.design.agent")

MODES = ("fast", "standard", "deep")
BUDGET = {"fast": 2, "standard": 5, "deep": 10}
CANDIDATES = {"fast": 1, "standard": 1, "deep": 2}
CRITIC_ROUNDS = {"fast": 0, "standard": 1, "deep": 2}
ACCEPT_SCORE = 85  # critique score at which no repair is attempted
JUDGE_ACCEPT = 7  # visual judge score (0-10) at which no repair is attempted

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer"},
                    "primitives": {"type": "string"},
                    "dimensions": {"type": "string"},
                    "placement": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        "overall": {
            "type": "object",
            "properties": {
                "length_m": {"type": "number"},
                "width_m": {"type": "number"},
                "height_m": {"type": "number"},
            },
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "components"],
}

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "verdict": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "parts": {"type": "array", "items": {"type": "string"}},
                    "problem": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["problem"],
            },
        },
    },
    "required": ["score", "issues"],
}

PLAN_SYSTEM = """\
You are a senior process-plant layout engineer. Before any geometry is drawn you write a short, concrete \
PLAN for the design brief: the equipment items with counts, realistic dimensions in metres, which simple \
primitives (box, cylinder, cone, sphere, tube, pipe) will represent each, where each sits relative to the \
others, and the overall footprint and height. If reference images are attached, describe what they show and \
estimate dimensions from visible references (people, doors, standard equipment). List the assumptions you \
make where the brief is silent, and up to three questions a client should answer to make the design \
better (you still proceed with your assumptions). Output ONE JSON object only."""

JUDGE_SYSTEM = """\
You are a strict design reviewer. The first image is an isometric render of a 3-D layout built from simple \
primitives (boxes, cylinders, cones, spheres, tubes, pipes); any further images are the client's reference \
images. Judge how well the render matches the brief (and the references): recognisable equipment, \
proportions, arrangement, connections, nothing floating or missing. Score 0-10 (10 = an engineer would accept \
it as a block model of the brief). List concrete issues, each naming part ids where possible and the fix. \
Output ONE JSON object only."""


@dataclass
class Step:
    name: str
    status: str  # ok | skipped | failed | info
    ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"step": self.name, "status": self.status, "ms": self.ms, **self.detail}


@dataclass
class Candidate:
    spec: Spec
    warnings: list[str]
    crit: Critique
    raw: str = ""
    label: str = "A"


@dataclass
class AgentResult:
    spec: Spec
    warnings: list[str]
    provider: str
    model: str
    mode: str
    llm_calls: int
    latency_s: float
    usage: dict[str, Any]
    trace: list[Step]
    plan: dict[str, Any] | None
    critique: Critique
    build: Any = None  # BuildResult when built
    build_error: str | None = None
    judge: dict[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "llm_calls": self.llm_calls,
            "plan": self.plan,
            "critique": self.critique.to_dict(),
            "judge": self.judge,
            "trace": [s.to_dict() for s in self.trace],
        }


class Budget:
    def __init__(self, calls: int, deadline_s: float):
        self.calls, self.used = calls, 0
        self.deadline = time.monotonic() + deadline_s
        self._lock = threading.Lock()

    def left_s(self) -> float:
        return self.deadline - time.monotonic()

    def can(self, need_s: float = 20.0) -> bool:
        return self.used < self.calls and self.left_s() > need_s

    def take(self) -> None:
        with self._lock:
            self.used += 1


class DesignAgent:
    def __init__(
        self,
        provider: Provider,
        *,
        mode: str = "standard",
        builder: Callable[[Spec], Any] | None = None,
        deadline_s: float = 300.0,
        call_timeout_s: float = 150.0,
    ):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        self.p = provider
        self.mode = mode
        self.builder = builder
        self.deadline_s = deadline_s
        self.call_timeout_s = call_timeout_s
        self.trace: list[Step] = []
        self.usage: dict[str, Any] = {}
        self.llm_s = 0.0
        self.model = provider.model
        self._plan_cache: dict[str, Any] | None = None
        self._lock = threading.Lock()  # candidates are generated on parallel threads

    # ------------------------------------------------------------------ plumbing
    def _log(self, name: str, status: str, t0: float, **detail: Any) -> None:
        self.trace.append(Step(name, status, int((time.perf_counter() - t0) * 1000), detail))

    def _call(self, budget: Budget, system: str, user: str, schema: dict | None, images: list | None) -> str:
        budget.take()
        kw: dict[str, Any] = {"schema": schema, "timeout": max(5.0, min(self.call_timeout_s, budget.left_s() - 2))}
        if images:
            kw["images"] = images
        try:
            res = self.p.generate(system, user, **kw)
        except LLMError as e:
            raise DesignError(str(e), upstream=True) from None
        with self._lock:
            self.llm_s += res.latency_s
            self.usage = _add_usage(self.usage, res.usage)
            self.model = res.model
        return res.text

    def _spec_from(self, budget: Budget, user: str, images: list | None, label: str) -> tuple[Spec, list[str], str]:
        """One generation with validator-in-the-loop repair (at most one repair while the budget allows)."""
        errors: list[str] = []
        raw = ""
        for attempt in (1, 2):
            if attempt == 2 and not budget.can(15):
                break
            t0 = time.perf_counter()
            raw = self._call(budget, SYSTEM_PROMPT, user, SPEC_SCHEMA, images)
            try:
                spec, warns = parse_spec(extract_json(raw))
                self._log(f"generate {label}", "ok", t0, attempt=attempt, parts=len(spec.parts))
                return spec, warns, raw
            except SpecError as e:
                errors = e.errors
            except ValueError as e:
                errors = [str(e)]
            self._log(f"generate {label}", "failed", t0, attempt=attempt, errors=errors[:8])
            user = (
                f"{user}\n\nYour previous answer was rejected by the validator:\n"
                + "\n".join(f"- {m}" for m in errors[:20])
                + f"\n\nPrevious answer:\n{raw[:6000]}\n\nReturn the corrected, complete JSON object only."
            )
        raise DesignError("the model did not produce a valid design", errors, raw)

    # ------------------------------------------------------------------ steps
    def _plan(self, budget: Budget, brief: str, images: list, draft: dict | None) -> dict[str, Any] | None:
        t0 = time.perf_counter()
        if self.mode == "fast":
            self._log("plan", "skipped", t0, why="fast mode")
            return None
        user = f"Design brief:\n{brief}\n"
        if draft:
            user += f"\nAn imported CAD model is the starting point ({len(draft.get('parts', []))} parts, bounding box in the report).\n"
        user += "\nReturn the plan JSON now."
        try:
            raw = self._call(budget, PLAN_SYSTEM, user, PLAN_SCHEMA, images)
            plan = extract_json(raw)
            if not isinstance(plan, dict) or not isinstance(plan.get("components"), list):
                raise ValueError("plan has no components list")
        except ValueError as e:
            self._log("plan", "failed", t0, why=str(e)[:200], decision="continue without a plan")
            return None
        plan["components"] = [c for c in plan["components"] if isinstance(c, dict)][:40]
        self._log("plan", "ok", t0, components=len(plan["components"]), assumptions=len(plan.get("assumptions") or []))
        return plan

    def _retrieve(self, brief: str, plan: dict | None) -> list[templates.Template]:
        t0 = time.perf_counter()
        if self.mode == "fast":
            self._log("retrieve", "skipped", t0, why="fast mode")
            return []
        q = brief + " " + " ".join(str(c.get("name", "")) for c in (plan or {}).get("components", []))
        hits = templates.search(q, k=3)
        self._log("retrieve", "ok", t0, templates=[t.id for t in hits])
        return hits

    def _generation_prompt(self, brief: str, plan: dict | None, refs: list, draft: dict | None, variant: str) -> str:
        parts = [f"Design brief:\n{brief}"]
        if plan:
            slim = {k: plan.get(k) for k in ("summary", "components", "overall", "assumptions") if plan.get(k)}
            parts.append("Engineering plan to follow (dimensions in metres):\n" + json.dumps(slim, indent=1)[:6000])
        if refs:
            parts.append(
                "Reference patterns retrieved from the equipment library (adapt dimensions and positions to the "
                "brief; reuse the construction idea, never copy blindly):\n" + "\n".join(t.compact() for t in refs)
            )
        if draft:
            parts.append(
                "Starting point - an imported CAD model already converted to primitives. Keep its geometry unless the "
                "brief asks for changes; improve ids, groups and colours, and add what the brief asks for:\n"
                + json.dumps(draft, separators=(",", ":"))[:20000]
            )
        if variant == "B":
            parts.append("Variant B: favour faithful proportions and explicit connections (ducts, pipes, supports).")
        parts.append("Return the JSON object now.")
        return "\n\n".join(parts)

    def _repair(
        self, budget: Budget, base: Candidate, brief: str, feedback: str, images: list | None, why: str
    ) -> Candidate | None:
        t0 = time.perf_counter()
        user = (
            f"Design brief:\n{brief}\n\nCurrent design:\n{json.dumps(base.spec.to_dict(), separators=(',', ':'))[:24000]}\n\n"
            f"A reviewer found these problems:\n{feedback}\n\nReturn the complete corrected JSON design (all parts, "
            "not only the changed ones). Keep everything that is correct."
        )
        try:
            spec, warns, raw = self._spec_from(budget, user, images, f"repair ({why})")
        except DesignError as e:
            if e.upstream:
                raise
            self._log("repair", "failed", t0, why=str(e)[:200], decision="keep the previous design")
            return None
        return Candidate(spec, warns, critique(spec, self._plan_cache), raw, base.label + "'")

    # ------------------------------------------------------------------ run
    def run(self, brief: str, images: list | None = None, draft: dict | None = None) -> AgentResult:
        images = images or []
        t_start = time.perf_counter()
        budget = Budget(BUDGET[self.mode], self.deadline_s)
        if images and not getattr(self.p, "vision", False):
            raise DesignError(
                f"the {self.p.name} provider ({self.p.model}) does not accept images; choose a vision model"
            )
        if not brief.strip():
            brief = (
                "Reproduce the equipment shown in the reference image(s) as a to-scale 3-D block model."
                if images
                else "Clean up the imported CAD model: meaningful ids, groups and colours."
            )
        brief = sanitize_prompt(brief)
        t0 = time.perf_counter()
        self._log(
            "intake", "ok", t0, mode=self.mode, images=len(images), cad_draft=bool(draft), budget_calls=budget.calls
        )

        plan = self._plan(budget, brief, images, draft)
        self._plan_cache = plan
        refs = self._retrieve(brief, plan)

        # candidates (deep: two, generated in parallel)
        n = CANDIDATES[self.mode] if budget.calls - budget.used >= 2 * CANDIDATES[self.mode] else 1
        labels = ["A", "B"][:n]
        cands: list[Candidate] = []

        def one(label: str) -> Candidate:
            spec, warns, raw = self._spec_from(
                budget, self._generation_prompt(brief, plan, refs, draft, label), images, label
            )
            return Candidate(spec, warns, critique(spec, plan), raw, label)

        if n == 1:
            cands.append(one("A"))
        else:
            with ThreadPoolExecutor(max_workers=n) as ex:
                futs = [ex.submit(one, lb) for lb in labels]
                errs: list[DesignError] = []
                for f in futs:
                    try:
                        cands.append(f.result())
                    except DesignError as e:
                        errs.append(e)
                if not cands:
                    raise errs[0]
        t0 = time.perf_counter()
        best = max(cands, key=lambda c: (c.crit.score, -c.crit.errors))
        self._log(
            "critique",
            "ok",
            t0,
            candidates={c.label: c.crit.score for c in cands},
            chosen=best.label,
            errors=best.crit.errors,
            findings=len(best.crit.findings),
        )

        # critique-driven repair rounds
        for rnd in range(CRITIC_ROUNDS[self.mode]):
            t0 = time.perf_counter()
            if best.crit.score >= ACCEPT_SCORE and not best.crit.errors:
                self._log(
                    "decide", "info", t0, decision="accept", why=f"critic score {best.crit.score} >= {ACCEPT_SCORE}"
                )
                break
            if not budget.can(30):
                self._log("decide", "info", t0, decision="stop", why="budget or time exhausted")
                break
            self._log(
                "decide",
                "info",
                t0,
                decision=f"repair round {rnd + 1}",
                why=f"critic score {best.crit.score}, {best.crit.errors} error(s)",
            )
            fixed = self._repair(budget, best, brief, best.crit.feedback(), None, "critique")
            t0 = time.perf_counter()
            if fixed is not None and fixed.crit.score >= best.crit.score:
                self._log("critique", "ok", t0, after_repair=fixed.crit.score, before=best.crit.score, kept=True)
                best = fixed
            elif fixed is not None:
                self._log(
                    "critique",
                    "ok",
                    t0,
                    after_repair=fixed.crit.score,
                    before=best.crit.score,
                    kept=False,
                    decision="repair made it worse: keep the previous design",
                )

        result = AgentResult(
            best.spec,
            best.warnings,
            self.p.name,
            self.model,
            self.mode,
            budget.used,
            0.0,
            {},
            self.trace,
            plan,
            best.crit,
        )
        # build, verify, and (deep) visual judge
        if self.builder is not None:
            self._build(result, best)
            if self.mode == "deep" and result.build is not None and getattr(result.build, "png", None):
                self._judge_and_refine(budget, result, best, brief, images)
        result.llm_calls = budget.used
        result.usage = self.usage
        result.latency_s = round(time.perf_counter() - t_start, 3)
        result.trace = self.trace
        return result

    def _build(self, result: AgentResult, cand: Candidate) -> None:
        assert self.builder is not None
        t0 = time.perf_counter()
        try:
            res = self.builder(cand.spec)
        except Exception as e:  # the API layer turns this into a readable reason
            result.build, result.build_error = None, str(e)
            self._log("build", "failed", t0, why=str(e)[:200])
            return
        if res is None:
            self._log("build", "skipped", t0, why="no FreeCAD worker")
            return
        result.build = res
        bad = [c.id for c in getattr(res, "checks", []) if not c.ok]
        self._log("build", "ok" if not bad else "failed", t0, verified=getattr(res, "ok", None), failed_parts=bad[:10])

    def _judge_and_refine(self, budget: Budget, result: AgentResult, cand: Candidate, brief: str, images: list) -> None:
        t0 = time.perf_counter()
        if not getattr(self.p, "vision", False):
            self._log("visual judge", "skipped", t0, why="provider has no vision")
            return
        if not budget.can(40):
            self._log("visual judge", "skipped", t0, why="budget or time exhausted")
            return
        from .inputs import Image

        render = Image("image/png", result.build.png)
        user = f"Design brief:\n{brief}\n\nPart ids in the design: {', '.join(p.id for p in cand.spec.parts)[:3000]}\n\nReturn the review JSON now."
        try:
            j = extract_json(self._call(budget, JUDGE_SYSTEM, user, JUDGE_SCHEMA, [render, *images[:2]]))
            score = int(j.get("score", 0))
            issues = [i for i in (j.get("issues") or []) if isinstance(i, dict)][:12]
        except (ValueError, TypeError) as e:
            self._log("visual judge", "failed", t0, why=str(e)[:200])
            return
        result.judge = {"score": score, "verdict": str(j.get("verdict", ""))[:400], "issues": issues}
        self._log("visual judge", "ok", t0, score=score, issues=len(issues))
        t0 = time.perf_counter()
        if score >= JUDGE_ACCEPT or not issues:
            self._log("decide", "info", t0, decision="accept", why=f"judge score {score} >= {JUDGE_ACCEPT}")
            return
        if not budget.can(60):
            self._log("decide", "info", t0, decision="stop", why="budget or time exhausted")
            return
        self._log("decide", "info", t0, decision="repair from visual review", why=f"judge score {score}")
        feedback = "\n".join(
            f"- {i.get('problem', '')} Fix: {i.get('fix', '')} (parts: {', '.join(map(str, i.get('parts') or []))})"
            for i in issues
        )
        fixed = self._repair(budget, cand, brief, feedback, images or None, "visual review")
        if fixed is None or fixed.crit.score < cand.crit.score - 5:
            self._log(
                "decide",
                "info",
                time.perf_counter(),
                decision="keep the judged design",
                why="the repair failed or broke geometric checks",
            )
            return
        prev_build = result.build
        self._build(result, fixed)
        if result.build is None or not getattr(result.build, "ok", False):
            result.build, result.build_error = prev_build, None  # never trade a verified build for a broken one
            self._log(
                "decide", "info", time.perf_counter(), decision="keep the judged design", why="rebuild did not verify"
            )
            return
        result.spec, result.warnings, result.critique = fixed.spec, fixed.warnings, fixed.crit
