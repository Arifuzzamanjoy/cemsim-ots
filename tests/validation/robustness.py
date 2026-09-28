"""Robustness sweep: every disturbance at its extremes from nominal, checked for
exceptions, NaN/inf, physical bounds, draught convergence and conservation."""

import json
import math
import sys
import traceback

sys.path.insert(0, ".")
import numpy as np

from cemsim import thermo as th
from cemsim.sim import Simulator
from cemsim.trainer import DISTURBANCES


def check(sim, tag):
    issues = []
    k = sim.kpi
    for key, v in k.items():
        if isinstance(v, (float, int, np.floating)) and not math.isfinite(float(v)):
            issues.append(f"{key} not finite")
    p = sim.plant
    for name, arr in (("kiln.n", p.kiln.n), ("ph.n", p.ph.n), ("cooler.n", p.cooler.n)):
        if np.any(arr < -1e-9):
            issues.append(f"{name} negative")
        if not np.all(np.isfinite(arr)):
            issues.append(f"{name} nan")
    for name, T in (
        ("kiln.T_b", p.kiln.T_b),
        ("kiln.T_w", p.kiln.T_w),
        ("kiln.T_g", p.kiln.T_g),
        ("ph.T", p.ph.T),
        ("cooler.T", p.cooler.T),
    ):
        if np.any(T < 240) or np.any(T > 3100) or not np.all(np.isfinite(T)):
            issues.append(f"{name} out of range [{np.nanmin(T):.0f},{np.nanmax(T):.0f}]")
    if k.get("draught_resid", 0) > 1e-2:
        issues.append(f"draught resid {k['draught_resid']:.3g}")
    return issues


def run_case(did, value, minutes):
    sim = Simulator()
    sim.load_file("nominal")
    sim.plant.acc = dict.fromkeys(sim.plant.acc, 0.0)
    m0, H0 = sim.plant.holdup()

    def b():
        return (
            float(sim.plant.clk_n @ th.M_SOLID + sim.plant.hot_meal @ th.M_SOLID),
            sim.plant.clk_H + sim.plant.hot_meal_H,
        )

    b0 = b()
    worst = []
    try:
        sim.cmd_disturbance(did, value)
        for i in range(minutes):
            sim.run(60)
            iss = check(sim, did)
            if iss:
                worst = iss
                break
    except Exception:
        return {"case": f"{did}={value}", "error": traceback.format_exc(limit=3)}
    m1, H1 = sim.plant.holdup()
    b1 = b()
    a = sim.plant.acc
    merr = (a["m_in"] - a["m_out"] - (m1 + b1[0] - m0 - b0[0])) / max(a["m_in"], 1)
    eerr = (a["H_in"] - a["H_out"] - (H1 + b1[1] - H0 - b0[1])) / (15.0 / 3.6 * 25600 * minutes * 60)
    k = sim.kpi
    return {
        "case": f"{did}={value}",
        "issues": worst,
        "mass_err": merr,
        "energy_err": eerr,
        "BZ": round(k["T_bz"]),
        "FL": round(k["free_lime_pct"], 2),
        "O2k": round(k["O2_kiln_inlet"], 2),
        "COk": round(k["CO_kiln_inlet_ppm"]),
        "Tcal": round(k["T_calciner"]),
        "Tex": round(k["T_ph_exit"]),
        "p_hood": round(k["p_hood"], 2),
        "alarms": len(sim.alarms.active),
    }


def failed(r):
    if "error" in r:
        return "exception"
    if r["issues"]:
        return "; ".join(r["issues"])
    if abs(r["mass_err"]) > 1e-4:
        return f"mass closure {r['mass_err']:.1e}"
    if abs(r["energy_err"]) > 5e-3:
        return f"energy closure {r['energy_err']:.1e}"
    return None


if __name__ == "__main__":
    ids = sys.argv[1:]
    bad = []
    for d in DISTURBANCES:
        if ids and d["id"] not in ids:
            continue
        vals = [d["min"], d["max"]] if d["kind"] == "value" else [None]
        for v in vals:
            r = run_case(d["id"], v, 60)
            print(json.dumps(r, default=str), flush=True)
            why = failed(r)
            if why:
                bad.append(f"{r['case']}: {why}")
    print("\nrobustness:", "all cases clean" if not bad else f"{len(bad)} FAILED -> " + " | ".join(bad))
    sys.exit(1 if bad else 0)
