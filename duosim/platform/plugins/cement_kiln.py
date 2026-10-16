"""The cement kiln line (the original DuoSim simulator) as a platform plug-in.

A thin adapter: each session gets its own `Simulator`, started from the calibrated "nominal"
fileset, stepped headless (no HMI).  The HMI at `/` keeps using its own global instance.
"""

from __future__ import annotations

import math
from typing import Any

from ...sim import TREND_TAGS, CommandError, Simulator
from ..registry import CommandRejected, CommandSpec, Manifest, Plugin, TagSpec

_UNITS = {
    "T_bz": "degC", "T_kiln_inlet": "degC", "T_calciner": "degC", "T_ph_exit": "degC", "T_sec_air": "degC",
    "T_ter_air": "degC", "T_clinker_out": "degC", "T_S1": "degC", "T_S2": "degC", "T_S3": "degC", "T_S4": "degC",
    "T_vent": "degC", "shell_max_C": "degC", "O2_kiln_inlet": "%", "O2_ph_exit": "%", "CO_kiln_inlet_ppm": "ppm",
    "NO_kiln_inlet_ppm": "ppm", "CO_ph_exit_ppm": "ppm", "clinker_tph": "t/h", "clinker_tph_avg": "t/h",
    "kiln_feed_tph": "t/h", "coal_kiln_tph": "t/h", "coal_cal_tph": "t/h", "spec_heat_kJkg": "kJ/kg",
    "free_lime_pct": "%", "C3S_pct": "%", "kiln_rpm": "rpm", "kiln_drive_kW": "kW", "id_fan_kW": "kW",
    "flame_len_m": "m", "p_ph_exit": "Pa", "p_hood": "Pa", "p_kiln_inlet": "Pa",
}  # fmt: skip

MANIFEST = Manifest(
    id="cement_kiln",
    name="Cement kiln line (3000 t/d)",
    domain="cement",
    version="0.1.0",
    description="5-stage preheater + calciner, rotary kiln, grate cooler, draught system; first-principles model "
    "with DCS layer (drives, groups, interlocks, controllers) and trainer disturbances.",
    dt=1.0,
    tags=tuple(TagSpec(t, _UNITS.get(t, "")) for t in TREND_TAGS),
    commands=(
        CommandSpec("drive", "drive command", {"tag": "string", "cmd": "string", "value": "number"}),
        CommandSpec("group", "group sequence command", {"tag": "string", "cmd": "string"}),
        CommandSpec("pid", "controller change", {"tag": "string", "field": "string", "value": "number"}),
        CommandSpec("disturbance", "trainer disturbance", {"id": "string", "value": "number"}),
    ),
    design_prompt=(
        "A cement kiln line: a 5-stage cyclone preheater tower on a steel frame with a calciner, a long "
        "horizontal rotary kiln on three piers with a girth gear and drive, a hood with burner at the "
        "outlet, a grate cooler with six fans, an ID fan and a tall stack."
    ),
)


class KilnInstance:
    def __init__(self, seed: int | None = None):
        self.sim = Simulator()
        try:
            self.sim.load_file("nominal")
        except CommandError:
            self.sim.set_all_running()
            self.sim.run(60)
        if seed is not None:
            self.sim._rng.seed(seed)
        self.sim.running = False
        self._t0 = self.sim.plant.t

    @property
    def t(self) -> float:
        return self.sim.plant.t - self._t0

    def step(self, n: int = 1) -> None:
        for _ in range(int(n)):
            self.sim.step()

    def tags(self) -> dict[str, float]:
        k = self.sim.kpi
        out = {}
        for t in TREND_TAGS:
            v = k.get(t, 0.0)
            f = float(v) if isinstance(v, (int, float)) else 0.0
            out[t] = f if math.isfinite(f) else 0.0
        return out

    def command(self, name: str, **a: Any) -> dict[str, Any]:
        s = self.sim
        try:
            if name == "drive":
                s.cmd_drive(a["tag"], a["cmd"], a.get("value"), source="API")
            elif name == "group":
                ok, why = s.cmd_group(a["tag"], a["cmd"], source="API")
                return {"ok": ok, "msg": why}
            elif name == "pid":
                s.cmd_pid(a["tag"], a["field"], a["value"], source="API")
            elif name == "disturbance":
                s.cmd_disturbance(a["id"], a.get("value"), source="API")
            else:
                raise CommandRejected(f"unknown command {name!r}")
        except (CommandError, KeyError) as e:
            raise CommandRejected(str(e)) from None
        return {"ok": True}

    def snapshot(self) -> dict[str, Any]:
        return {"sim": self.sim.snapshot(), "rng": self.sim._rng.getstate(), "t0": self._t0}

    def restore(self, snap: dict[str, Any]) -> None:
        self.sim.restore(snap["sim"])
        self.sim._rng.setstate(snap["rng"])
        self._t0 = snap["t0"]


PLUGIN = Plugin(MANIFEST, KilnInstance)
