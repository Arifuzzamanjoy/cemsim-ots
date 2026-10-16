"""Simulator plug-in contract and registry: how "other simulators" plug into the platform.

A plug-in is *trusted code* installed by the operator (an entry point in the group
`duosim.simulators`, or a directory under SIMULATORS_DIR that exposes `PLUGIN`).  Tenants do
not upload code: running untrusted simulators would need sandboxing, which is out of scope.

Contract
--------
Manifest        what the simulator is: id, tags (name/unit/range), commands, time step, and a
                `design_prompt` that the Design Studio can use to generate its 3-D model.
Plugin          `manifest` + `create(seed) -> SimulatorInstance`
SimulatorInstance
                `t`, `step(n)`, `tags()`, `command(name, **args)`, `snapshot()`, `restore()`

`conformance(plugin)` is the executable definition of "a well-behaved simulator" and is run by
the test-suite for every registered plug-in and by `python -m duosim.platform check <id>`.
"""

from __future__ import annotations

import copy
import importlib
import importlib.metadata
import importlib.util
import math
import os
import random
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
TAG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
ENTRY_POINT_GROUP = "duosim.simulators"


class CommandRejected(ValueError):
    """The simulator refused a command (unknown name, bad argument, wrong state)."""


@dataclass(frozen=True)
class TagSpec:
    name: str
    unit: str = ""
    desc: str = ""
    lo: float | None = None
    hi: float | None = None


@dataclass(frozen=True)
class CommandSpec:
    name: str
    desc: str = ""
    args: dict[str, str] = field(default_factory=dict)  # argument name -> "number" | "string" | "bool"


@dataclass(frozen=True)
class Manifest:
    id: str
    name: str
    domain: str
    version: str
    description: str
    dt: float
    tags: tuple[TagSpec, ...]
    commands: tuple[CommandSpec, ...] = ()
    design_prompt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "domain": self.domain,
            "version": self.version,
            "description": self.description,
            "dt": self.dt,
            "tags": [t.__dict__ for t in self.tags],
            "commands": [{"name": c.name, "desc": c.desc, "args": c.args} for c in self.commands],
            "design_prompt": self.design_prompt,
        }

    def problems(self) -> list[str]:
        out = []
        if not ID_RE.match(self.id):
            out.append(f"id {self.id!r} must match {ID_RE.pattern}")
        if not (math.isfinite(self.dt) and 0 < self.dt <= 3600):
            out.append(f"dt {self.dt!r} must be a finite number in (0, 3600] seconds")
        names = [t.name for t in self.tags]
        if not names:
            out.append("a simulator must declare at least one tag")
        for n in names:
            if not TAG_RE.match(n):
                out.append(f"tag name {n!r} must match {TAG_RE.pattern}")
        if len(set(names)) != len(names):
            out.append("duplicate tag names")
        cn = [c.name for c in self.commands]
        if len(set(cn)) != len(cn):
            out.append("duplicate command names")
        return out


class SimulatorInstance(Protocol):
    t: float

    def step(self, n: int = 1) -> None: ...
    def tags(self) -> dict[str, float]: ...
    def command(self, name: str, **args: Any) -> dict[str, Any]: ...
    def snapshot(self) -> dict[str, Any]: ...
    def restore(self, snap: dict[str, Any]) -> None: ...


class SimulatorPlugin(Protocol):
    manifest: Manifest

    def create(self, seed: int | None = None) -> SimulatorInstance: ...


# --------------------------------------------------------------------------- authoring helper
class SimpleModel:
    """Base class for plug-in authors.

    Keep *all* state in `self.state` (JSON-able) and randomness in `self.rng`: then snapshot,
    restore and determinism come for free.  Implement `initial_state`, `advance`, `read_tags`
    and `handle`.
    """

    manifest: Manifest  # set by the subclass

    def __init__(self, seed: int | None = None):
        self.t = 0.0
        self.rng = random.Random(seed)
        self.state: dict[str, Any] = self.initial_state()

    # -- to implement ------------------------------------------------------
    def initial_state(self) -> dict[str, Any]:
        raise NotImplementedError

    def advance(self, dt: float) -> None:
        """Advance `self.state` by dt seconds."""
        raise NotImplementedError

    def read_tags(self) -> dict[str, float]:
        raise NotImplementedError

    def handle(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute a declared command; raise CommandRejected for bad arguments."""
        raise CommandRejected(f"command {name!r} is not implemented")

    # -- provided ----------------------------------------------------------
    def step(self, n: int = 1) -> None:
        dt = self.manifest.dt
        for _ in range(int(n)):
            self.advance(dt)
            self.t += dt

    def tags(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.read_tags().items()}

    def command(self, name: str, **args: Any) -> dict[str, Any]:
        spec = next((c for c in self.manifest.commands if c.name == name), None)
        if spec is None:
            raise CommandRejected(f"unknown command {name!r}; known: {[c.name for c in self.manifest.commands]}")
        unknown = set(args) - set(spec.args)
        if unknown:
            raise CommandRejected(f"{name}: unknown argument(s) {sorted(unknown)}")
        for a, kind in spec.args.items():
            if a not in args:
                raise CommandRejected(f"{name}: missing argument {a!r}")
            v = args[a]
            if kind == "number" and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)):
                raise CommandRejected(f"{name}.{a}: must be a finite number")
            if kind == "string" and not isinstance(v, str):
                raise CommandRejected(f"{name}.{a}: must be a string")
            if kind == "bool" and not isinstance(v, bool):
                raise CommandRejected(f"{name}.{a}: must be true or false")
        return {"ok": True, **self.handle(name, args)}

    def snapshot(self) -> dict[str, Any]:
        return {"t": self.t, "state": copy.deepcopy(self.state), "rng": self.rng.getstate()}

    def restore(self, snap: dict[str, Any]) -> None:
        self.t = snap["t"]
        self.state = copy.deepcopy(snap["state"])
        self.rng.setstate(snap["rng"])


@dataclass(frozen=True)
class Plugin:
    """A plug-in built from a manifest and a model class (`SimpleModel` subclass or compatible)."""

    manifest: Manifest
    model: Callable[..., SimulatorInstance]

    def create(self, seed: int | None = None) -> SimulatorInstance:
        return self.model(seed)


# --------------------------------------------------------------------------- conformance
def conformance(plugin: SimulatorPlugin, steps: int = 40) -> list[str]:
    """Return a list of problems (empty = the plug-in honours the contract)."""
    m = plugin.manifest
    out = [f"manifest: {p}" for p in m.problems()]
    if out:
        return out
    declared = [t.name for t in m.tags]
    try:
        a, b = plugin.create(1), plugin.create(1)
    except Exception as e:
        return [f"create() raised {type(e).__name__}: {e}"]
    if a is b:
        out.append("create() must return a new, independent instance each time")
    try:
        t0 = a.t
        a.step(steps)
        if not math.isclose(a.t - t0, steps * m.dt, rel_tol=1e-9, abs_tol=1e-9):
            out.append(f"t advanced by {a.t - t0}, expected {steps * m.dt} ({steps} steps x dt {m.dt})")
        tg = a.tags()
    except Exception as e:
        return [*out, f"step()/tags() raised {type(e).__name__}: {e}"]
    if set(tg) != set(declared):
        out.append(
            f"tags() keys differ from the manifest: extra={sorted(set(tg) - set(declared))} "
            f"missing={sorted(set(declared) - set(tg))}"
        )
    for k, v in tg.items():
        if not isinstance(v, float) or not math.isfinite(v):
            out.append(f"tag {k} = {v!r} is not a finite float")
    for spec in m.tags:
        v = tg.get(spec.name)
        if v is None or not math.isfinite(v):
            continue
        if (spec.lo is not None and v < spec.lo - 1e-9) or (spec.hi is not None and v > spec.hi + 1e-9):
            out.append(f"tag {spec.name} = {v:g} outside its declared range [{spec.lo}, {spec.hi}]")
    # snapshot / restore must reproduce the future exactly
    try:
        snap = a.snapshot()
        a.step(10)
        want = a.tags()
        b.restore(snap)
        b.step(10)
        got = b.tags()
        for k in declared:
            if not math.isclose(want[k], got[k], rel_tol=1e-9, abs_tol=1e-9):
                out.append(f"restore() is not faithful: {k} {got[k]!r} != {want[k]!r}")
                break
    except Exception as e:
        out.append(f"snapshot()/restore() raised {type(e).__name__}: {e}")
    # a rejected command must be reported, not crash the simulator
    try:
        a.command("__no_such_command__")
        out.append("an unknown command was accepted")
    except CommandRejected:
        pass
    except Exception as e:
        out.append(f"unknown command raised {type(e).__name__}, expected CommandRejected")
    try:
        a.step(1)
    except Exception as e:
        out.append(f"simulator unusable after a rejected command: {type(e).__name__}: {e}")
    return out


# --------------------------------------------------------------------------- registry
class Registry:
    def __init__(self) -> None:
        self._plugins: dict[str, SimulatorPlugin] = {}

    def register(self, plugin: SimulatorPlugin) -> None:
        probs = plugin.manifest.problems()
        if probs:
            raise ValueError(f"invalid manifest for {plugin.manifest.id!r}: {'; '.join(probs)}")
        if plugin.manifest.id in self._plugins:
            raise ValueError(f"simulator id {plugin.manifest.id!r} is already registered")
        self._plugins[plugin.manifest.id] = plugin

    def get(self, sim_id: str) -> SimulatorPlugin:
        try:
            return self._plugins[sim_id]
        except KeyError:
            raise KeyError(f"unknown simulator {sim_id!r}; available: {sorted(self._plugins)}") from None

    def ids(self) -> list[str]:
        return sorted(self._plugins)

    def manifests(self) -> list[Manifest]:
        return [self._plugins[i].manifest for i in self.ids()]

    # -- discovery -----------------------------------------------------------
    def load_entry_points(self) -> list[str]:
        loaded = []
        for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
            self.register(ep.load())
            loaded.append(ep.name)
        return loaded

    def load_directory(self, directory: str | Path) -> list[str]:
        """Import every `<directory>/<id>/__init__.py` that defines PLUGIN (operator-trusted code)."""
        d = Path(directory)
        loaded = []
        if not d.is_dir():
            return loaded
        for pkg in sorted(d.iterdir()):
            init = pkg / "__init__.py"
            if not (pkg.is_dir() and init.is_file()) or not ID_RE.match(pkg.name):
                continue
            name = f"duosim_plugin_{pkg.name}"
            spec = importlib.util.spec_from_file_location(name, init, submodule_search_locations=[str(pkg)])
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            plugin = getattr(mod, "PLUGIN", None)
            if plugin is None:
                raise ValueError(f"{init}: no PLUGIN defined")
            self.register(plugin)
            loaded.append(plugin.manifest.id)
        return loaded


_DEFAULT: Registry | None = None


def default_registry() -> Registry:
    """Built-in simulators + $DUOSIM_SIMULATORS_DIR (default ./simulators) + entry points."""
    global _DEFAULT
    if _DEFAULT is None:
        from .plugins import builtin

        r = Registry()
        for p in builtin():
            r.register(p)
        r.load_directory(os.environ.get("DUOSIM_SIMULATORS_DIR") or Path.cwd() / "simulators")
        r.load_entry_points()
        _DEFAULT = r
    return _DEFAULT
