"""Water tank with a level controller: the smallest useful second simulator.

Not a cement process - it exists to show (and test) that the platform is not cement-specific,
and as a template for writing others.  State is the stored volume V; flows follow

    q_in  = q_in_max * valve/100                       inlet valve
    q_out = q_pump_max * pump/100 * sqrt(h/h_ref) + demand      pump (head-dependent) + consumers
    dV/dt = q_in - q_out

integrated with RK4.  The cumulative in/out volumes use the *same* RK4 weights as V, and water
lost to overflow (or not available when the tank runs dry) is booked as outflow, so the mass
balance closes to rounding error in every regime by construction (a test proves it).
"""

from __future__ import annotations

import math
from typing import Any

from ..registry import CommandRejected, CommandSpec, Manifest, Plugin, SimpleModel, TagSpec

AREA = 12.0  # m2   (D = 3.9 m)
H_MAX = 6.0  # m
H_REF = 3.0  # m   pump curve reference level
Q_IN_MAX = 90.0  # m3/h
Q_PUMP_MAX = 80.0  # m3/h at h = H_REF
KP, TI = 150.0, 900.0  # level PI: % per m, integral time s (0.9 % overshoot on a 1.5 m step)


class WaterTank(SimpleModel):
    manifest = Manifest(
        id="water_tank",
        name="Water tank with level control",
        domain="water / utilities",
        version="1.0.0",
        description="Cylindrical tank, inlet valve, head-dependent outlet pump, consumer demand and a PI level controller.",
        dt=1.0,
        tags=(
            TagSpec("level_m", "m", "tank level", 0.0, H_MAX),
            TagSpec("volume_m3", "m3", "stored volume", 0.0, AREA * H_MAX),
            TagSpec("q_in_m3h", "m3/h", "inlet flow", 0.0, Q_IN_MAX),
            TagSpec("q_out_m3h", "m3/h", "outlet flow (pump + demand)", 0.0),
            TagSpec("valve_pct", "%", "inlet valve position", 0.0, 100.0),
            TagSpec("pump_pct", "%", "pump speed", 0.0, 100.0),
            TagSpec("level_sp_m", "m", "level set-point", 0.5, 5.5),
            TagSpec("balance_error_m3", "m3", "mass-balance residual: stored - (in - out); ~0 in every regime"),
        ),
        commands=(
            CommandSpec("set_level_sp", "level set-point", {"value": "number"}),
            CommandSpec("set_valve", "inlet valve (manual mode)", {"value": "number"}),
            CommandSpec("set_pump", "pump speed", {"value": "number"}),
            CommandSpec("set_demand", "consumer demand m3/h", {"value": "number"}),
            CommandSpec("set_auto", "level controller on/off", {"on": "bool"}),
        ),
        design_prompt=(
            "A cylindrical water storage tank 3.9 m in diameter and 6 m high on a concrete ring foundation, "
            "with an inlet pipe and control valve on the side, an outlet pipe to a centrifugal pump with a "
            "motor on a skid, and a vent stack on the roof."
        ),
    )

    def initial_state(self) -> dict[str, Any]:
        return {
            "V": AREA * 3.0,
            "valve": 58.889,  # equilibrium: (60 % pump at 3 m + 5 m3/h demand) / 90 m3/h
            "pump": 60.0,
            "demand": 5.0,
            "sp": 3.0,
            "auto": True,
            "integ": 58.889,
            "cum_in": 0.0,
            "cum_out": 0.0,
            "V0": AREA * 3.0,
        }

    # flows in m3/h as functions of volume
    @staticmethod
    def _q_out(V: float, pump: float, demand: float) -> float:
        h = max(V, 0.0) / AREA
        return Q_PUMP_MAX * pump / 100.0 * math.sqrt(h / H_REF) + demand

    def advance(self, dt: float) -> None:
        s = self.state
        h = s["V"] / AREA
        if s["auto"]:  # PI on level, output = inlet valve %
            err = s["sp"] - h
            u = s["integ"] + KP * err
            s["valve"] = min(max(u, 0.0), 100.0)
            # conditional-integration anti-windup: do not integrate further into a saturated valve
            if u == s["valve"] or (u > 100.0 and err < 0.0) or (u < 0.0 and err > 0.0):
                s["integ"] += KP / TI * err * dt
        q_in = Q_IN_MAX * s["valve"] / 100.0
        hrs = dt / 3600.0

        def f(V: float) -> float:
            return q_in - self._q_out(V, s["pump"], s["demand"])

        V = s["V"]
        k1 = f(V)
        k2 = f(V + 0.5 * hrs * k1)
        k3 = f(V + 0.5 * hrs * k2)
        k4 = f(V + hrs * k3)
        # the same RK4 weights for the cumulative flows -> exact bookkeeping
        V_new = V + hrs * (k1 + 2 * k2 + 2 * k3 + k4) / 6.0
        s["V"] = max(0.0, min(V_new, AREA * H_MAX))
        w = [V, V + 0.5 * hrs * k1, V + 0.5 * hrs * k2, V + hrs * k3]
        qo = [self._q_out(v, s["pump"], s["demand"]) for v in w]
        s["cum_in"] += hrs * q_in
        # overflow (V_new > full) leaves as spill = extra outflow; running dry (V_new < 0) means
        # less left than demanded = less outflow (negative excess).  Either way the books close.
        s["cum_out"] += hrs * (qo[0] + 2 * qo[1] + 2 * qo[2] + qo[3]) / 6.0 + (V_new - s["V"])

    def read_tags(self) -> dict[str, float]:
        s = self.state
        return {
            "level_m": s["V"] / AREA,
            "volume_m3": s["V"],
            "q_in_m3h": Q_IN_MAX * s["valve"] / 100.0,
            "q_out_m3h": self._q_out(s["V"], s["pump"], s["demand"]),
            "valve_pct": s["valve"],
            "pump_pct": s["pump"],
            "level_sp_m": s["sp"],
            "balance_error_m3": (s["V"] - s["V0"]) - (s["cum_in"] - s["cum_out"]),
        }

    def handle(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        s = self.state
        v = args.get("value")
        if name == "set_level_sp":
            if not 0.5 <= v <= 5.5:
                raise CommandRejected("set_level_sp: value must be within [0.5, 5.5] m")
            s["sp"] = float(v)
        elif name == "set_valve":
            if s["auto"]:
                raise CommandRejected("set_valve: controller is in AUTO; switch it off first (set_auto)")
            if not 0 <= v <= 100:
                raise CommandRejected("set_valve: value must be within [0, 100] %")
            s["valve"] = s["integ"] = float(v)
        elif name == "set_pump":
            if not 0 <= v <= 100:
                raise CommandRejected("set_pump: value must be within [0, 100] %")
            s["pump"] = float(v)
        elif name == "set_demand":
            if not 0 <= v <= 200:
                raise CommandRejected("set_demand: value must be within [0, 200] m3/h")
            s["demand"] = float(v)
        elif name == "set_auto":
            s["auto"] = bool(args["on"])
            s["integ"] = s["valve"]
        return {}


PLUGIN = Plugin(WaterTank.manifest, WaterTank)
