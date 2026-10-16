"""Prompt -> validated design spec: orchestration (prompt framing, LLM call, validate, repair)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMError, LLMResult, Provider
from .spec import AXES, KINDS, MAX_PARTS, Spec, SpecError, parse_spec

MAX_PROMPT_CHARS = 2000
MAX_REPAIRS = 1

_VEC3 = {"type": "array", "items": {"type": "number"}}
SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "group": {"type": "string"},
                    "color": {"type": "string"},
                    "opacity": {"type": "number"},
                    "yaw_deg": {"type": "number"},
                    "at": _VEC3,
                    "size": _VEC3,
                    "radius": {"type": "number"},
                    "radius2": {"type": "number"},
                    "inner_radius": {"type": "number"},
                    "height": {"type": "number"},
                    "axis": {"type": "string", "enum": list(AXES)},
                    "path": {"type": "array", "items": _VEC3},
                },
                "required": ["id", "kind"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "parts"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = f"""\
You are a mechanical / process-plant layout designer. You turn a short brief into a 3-D layout \
made ONLY of the primitives below. You output ONE JSON object and nothing else - no prose, no \
markdown, no code.

UNITS AND AXES: metres. X east, Y north, Z up. The ground is z = 0; equipment stands on it \
(z >= 0). Use realistic, to-scale dimensions.

SCHEMA
{{"name": str, "description": str, "parts": [Part, ...]}}   (1..{MAX_PARTS} parts; 15-80 is typical)

Part (all numbers in metres):
- common: "id" (unique, letters/digits/underscore, starts with a letter, <= 32 chars), "kind", \
"group" (label such as "preheater", "civil", "piping"), "color" ("#RRGGBB"), "opacity" \
(0.05-1), "yaw_deg" (rotation about the vertical line through "at", degrees)
- "box":      "size": [dx, dy, dz], "at": [x, y, z] = the minimum corner
- "cylinder": "radius", "height", "axis", "at" = centre of the BASE disc
- "cone":     "radius" (at the base), "radius2" (at the top, 0 = pointed), "height", "axis", "at" = \
centre of the base
- "sphere":   "radius", "at" = centre
- "tube":     hollow cylinder: "radius" (outer), "inner_radius" (< radius), "height", "axis", "at" = \
centre of the base
- "pipe":     "radius", "path": [[x, y, z], ...] (2..24 points each, at most 120 points over all pipes; straight runs with round elbows)
"axis" is one of "x", "y", "z", "-x", "-y", "-z": the direction in which the part extends from \
"at" (default "z" = upward).

RULES
- Use only these kinds and fields. Inclined equipment (a rotary kiln has a ~3.5 % slope) cannot \
be expressed: draw it horizontal.
- Build recognisable equipment from several primitives (e.g. a cyclone = cylinder + cone with \
axis "-z" below it + a smaller cylinder on top; a fan = short wide cylinder + a box motor).
- Put each piece where it physically belongs: supported on the ground or on a structure, \
connected by pipes/ducts whose end points touch the equipment.
- Colour by role (vessels blue-grey, ducts yellow, steel structure dark grey, drives green, hot \
equipment red/orange) and give related parts the same "group".
- Keep ids stable and descriptive (e.g. "cyclone1_body", "kiln_shell").

EXAMPLE (a tank on a slab with an outlet pipe)
{{"name": "Tank", "description": "Day tank on a slab", "parts": [
 {{"id": "slab", "kind": "box", "size": [6, 6, 0.4], "at": [-3, -3, -0.4], "color": "#8d9399", "group": "civil"}},
 {{"id": "tank_shell", "kind": "cylinder", "radius": 2, "height": 5, "axis": "z", "at": [0, 0, 0], \
"color": "#6f8fb5", "group": "vessel"}},
 {{"id": "tank_roof", "kind": "cone", "radius": 2, "radius2": 0.4, "height": 1, "axis": "z", \
"at": [0, 0, 5], "color": "#6f8fb5", "group": "vessel"}},
 {{"id": "outlet", "kind": "pipe", "radius": 0.15, "path": [[2, 0, 0.8], [3.5, 0, 0.8], [3.5, 0, 3]], \
"color": "#d4ac0d", "group": "piping"}}]}}
"""


def sanitize_prompt(prompt: Any) -> str:
    if not isinstance(prompt, str):
        raise ValueError("prompt must be text")
    p = "".join(ch for ch in prompt if ch in "\n\t" or ord(ch) >= 32).strip()
    if not p:
        raise ValueError("prompt is empty")
    if len(p) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt too long ({len(p)} > {MAX_PROMPT_CHARS} characters)")
    return p


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def extract_json(text: str) -> Any:
    """The JSON object in a model answer (tolerates ``` fences and a little surrounding prose)."""
    t = _FENCE.sub("", text.strip())
    try:
        return json.loads(t)
    except ValueError:
        pass
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(t[a : b + 1])
        except ValueError as e:
            raise ValueError(f"answer is not valid JSON ({e})") from None
    raise ValueError("answer contains no JSON object")


class DesignError(RuntimeError):
    """No valid design could be produced; `.errors` has the last validation problems."""

    def __init__(self, msg: str, errors: list[str] | None = None, raw: str = "", upstream: bool = False):
        super().__init__(msg)
        self.errors = errors or []
        self.raw = raw
        self.upstream = upstream  # True: the model API failed (bad gateway), not "the answer was invalid"


@dataclass
class Generated:
    spec: Spec
    warnings: list[str]
    provider: str
    model: str
    attempts: int
    latency_s: float
    usage: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


class DesignService:
    def __init__(self, providers: dict[str, Provider], default: str | None = None):
        self.providers = providers
        self.default = default if default in providers else (next(iter(providers), None))

    def available(self) -> list[dict[str, Any]]:
        return [{"name": n, "model": p.model, "default": n == self.default} for n, p in self.providers.items()]

    def generate(self, prompt: str, provider: str | None = None, timeout: float = 150.0) -> Generated:
        text = sanitize_prompt(prompt)
        name = provider or self.default
        if name is None:
            raise DesignError("no LLM provider configured (set GEMINI_API_KEY or ANTHROPIC_API_KEY)")
        prov = self.providers.get(name)
        if prov is None:
            raise DesignError(f"unknown provider {name!r}; available: {', '.join(self.providers) or 'none'}")

        user = f"Design brief:\n{text}\n\nReturn the JSON object now."
        total = 0.0
        usage: dict[str, Any] = {}
        errors: list[str] = []
        raw = ""
        for attempt in range(1, MAX_REPAIRS + 2):
            try:
                res: LLMResult = prov.generate(SYSTEM_PROMPT, user, schema=SPEC_SCHEMA, timeout=timeout)
            except LLMError as e:
                raise DesignError(str(e), upstream=True) from None
            total += res.latency_s
            usage = _add_usage(usage, res.usage)
            raw = res.text
            try:
                spec, warns = parse_spec(extract_json(res.text))
                return Generated(spec, warns, res.provider, res.model, attempt, round(total, 3), usage, raw)
            except SpecError as e:
                errors = e.errors
            except ValueError as e:
                errors = [str(e)]
            user = (
                f"Design brief:\n{text}\n\n"
                f"Your previous answer was rejected by the validator:\n"
                + "\n".join(f"- {m}" for m in errors[:20])
                + f"\n\nPrevious answer:\n{raw[:6000]}\n\nReturn the corrected, complete JSON object only."
            )
        raise DesignError("the model did not produce a valid design", errors, raw)


def _add_usage(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, (int, float)) and isinstance(out.get(k), (int, float)):
            out[k] = out[k] + v
        elif v is not None:
            out[k] = v
    return out
