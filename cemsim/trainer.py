"""Trainer station: disturbances/scenarios, event log, session scoring, lab.

Scoring follows the Simulex idea of "monitoring subjects": each criterion
watches one process value against a reference with a tolerance and a method,
contributes with a weight, and the session score is the weighted mean.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Disturbance catalogue
# ---------------------------------------------------------------------------
DISTURBANCES = [
    # id, label, group, unit, default, min, max, kind ("value" | "trigger")
    {
        "id": "coal_lhv_kiln",
        "label": "Main burner coal LHV",
        "group": "Fuel",
        "unit": "kJ/kg",
        "default": 25600,
        "min": 18000,
        "max": 32000,
        "kind": "value",
    },
    {
        "id": "coal_lhv_cal",
        "label": "Calciner coal LHV",
        "group": "Fuel",
        "unit": "kJ/kg",
        "default": 25600,
        "min": 18000,
        "max": 32000,
        "kind": "value",
    },
    {
        "id": "coal_ash",
        "label": "Coal ash content",
        "group": "Fuel",
        "unit": "%",
        "default": 20.0,
        "min": 5.0,
        "max": 35.0,
        "kind": "value",
    },
    {
        "id": "coal_fluct",
        "label": "Coal dosing fluctuation",
        "group": "Fuel",
        "unit": "% amp",
        "default": 0.0,
        "min": 0.0,
        "max": 20.0,
        "kind": "value",
    },
    {
        "id": "flame_shape",
        "label": "Flame length factor (burner wear)",
        "group": "Fuel",
        "unit": "x",
        "default": 1.0,
        "min": 0.5,
        "max": 2.5,
        "kind": "value",
    },
    {
        "id": "rawmix_lsf",
        "label": "Kiln feed LSF",
        "group": "Raw meal",
        "unit": "-",
        "default": 1.00,
        "min": 0.88,
        "max": 1.10,
        "kind": "value",
    },
    {
        "id": "rawmix_sm",
        "label": "Kiln feed silica modulus",
        "group": "Raw meal",
        "unit": "-",
        "default": 2.5,
        "min": 1.8,
        "max": 3.4,
        "kind": "value",
    },
    {
        "id": "rawmix_moisture",
        "label": "Kiln feed moisture",
        "group": "Raw meal",
        "unit": "%",
        "default": 0.5,
        "min": 0.0,
        "max": 5.0,
        "kind": "value",
    },
    {
        "id": "feed_fluct",
        "label": "Kiln feed fluctuation",
        "group": "Raw meal",
        "unit": "% amp",
        "default": 0.0,
        "min": 0.0,
        "max": 20.0,
        "kind": "value",
    },
    {
        "id": "seal_false_air",
        "label": "Kiln inlet seal leakage",
        "group": "Plant",
        "unit": "x",
        "default": 1.0,
        "min": 0.5,
        "max": 6.0,
        "kind": "value",
    },
    {
        "id": "ph_false_air",
        "label": "Preheater false air",
        "group": "Plant",
        "unit": "x",
        "default": 1.0,
        "min": 0.5,
        "max": 5.0,
        "kind": "value",
    },
    {
        "id": "kiln_ring",
        "label": "Kiln ring (transport/draught)",
        "group": "Plant",
        "unit": "x",
        "default": 1.0,
        "min": 1.0,
        "max": 2.0,
        "kind": "value",
    },
    {
        "id": "ambient_T",
        "label": "Ambient temperature",
        "group": "Plant",
        "unit": "degC",
        "default": 25.0,
        "min": -20.0,
        "max": 50.0,
        "kind": "value",
    },
    {"id": "cyclone4_block", "label": "Cyclone 4 blockage", "group": "Failures", "kind": "trigger"},
    {"id": "cyclone4_release", "label": "Cyclone 4 blockage released (flush)", "group": "Failures", "kind": "trigger"},
    {"id": "cooler_fan1_fail", "label": "Cooler fan 1 failure", "group": "Failures", "kind": "trigger"},
    {"id": "id_fan_trip", "label": "ID fan trip", "group": "Failures", "kind": "trigger"},
    {"id": "kiln_drive_trip", "label": "Kiln main drive trip", "group": "Failures", "kind": "trigger"},
    {"id": "pa_fan_trip", "label": "Primary air fan trip", "group": "Failures", "kind": "trigger"},
]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
@dataclass
class Criterion:
    tag: str
    desc: str
    ref: float
    tol: float
    method: str = "band"  # band | average | max | min | iae
    weight: float = 1.0
    subject: str = ""
    # accumulators
    t: float = 0.0
    s_v: float = 0.0
    s_abs: float = 0.0
    t_in: float = 0.0
    vmax: float = -1e30
    vmin: float = 1e30

    def add(self, v, dt):
        if v is None or not math.isfinite(v):
            return
        self.t += dt
        self.s_v += v * dt
        self.s_abs += abs(v - self.ref) * dt
        if abs(v - self.ref) <= self.tol:
            self.t_in += dt
        self.vmax = max(self.vmax, v)
        self.vmin = min(self.vmin, v)

    def score(self):
        if self.t <= 0:
            return None
        avg = self.s_v / self.t
        tol = max(self.tol, 1e-9)
        if self.method == "band":
            s = 100.0 * self.t_in / self.t
        elif self.method == "average":
            s = 100.0 * max(0.0, 1.0 - max(0.0, abs(avg - self.ref) - tol) / (2 * tol))
        elif self.method == "max":
            s = (
                100.0
                if self.vmax <= self.ref + tol
                else max(0.0, 100.0 * (1 - (self.vmax - self.ref - tol) / (2 * tol)))
            )
        elif self.method == "min":
            s = (
                100.0
                if self.vmin >= self.ref - tol
                else max(0.0, 100.0 * (1 - (self.ref - tol - self.vmin) / (2 * tol)))
            )
        elif self.method == "iae":
            s = 100.0 * max(0.0, 1.0 - (self.s_abs / self.t) / (2 * tol))
        else:
            s = 0.0
        return s

    def result(self):
        return {
            "tag": self.tag,
            "desc": self.desc,
            "ref": self.ref,
            "tol": self.tol,
            "method": self.method,
            "weight": self.weight,
            "subject": self.subject,
            "avg": self.s_v / self.t if self.t else None,
            "max": self.vmax if self.t else None,
            "min": self.vmin if self.t else None,
            "in_band_pct": 100 * self.t_in / self.t if self.t else None,
            "score": self.score(),
        }


def default_criteria():
    return [
        Criterion("T_bz", "Burning zone material temperature", 1450, 30, "band", 3, "burning zone"),
        Criterion("free_lime_pct", "Free lime (kiln outlet)", 1.2, 0.8, "band", 3, "clinker quality"),
        Criterion("O2_kiln_inlet", "O2 kiln inlet", 3.0, 1.0, "band", 2, "kiln inlet"),
        Criterion("CO_kiln_inlet_ppm", "CO kiln inlet", 0.0, 1000, "max", 2, "kiln inlet"),
        Criterion("T_calciner", "Calciner outlet temperature", 875, 15, "band", 2, "calciner"),
        Criterion("spec_heat_kJkg", "Specific heat consumption", 3250, 150, "average", 3, "burner fuels"),
        Criterion("clinker_tph_avg", "Clinker production", 125, 8, "average", 2, "production"),
        Criterion("T_ph_exit", "Preheater exit gas temperature", 330, 30, "max", 1, "preheater"),
        Criterion("p_hood", "Kiln hood pressure", -0.5, 0.7, "band", 1, "cooler"),
        Criterion("T_clinker_out", "Clinker temperature after cooler", 120, 40, "max", 1, "cooler"),
    ]


class Session:
    def __init__(self, name="session", criteria=None):
        self.name = name
        self.criteria = criteria or default_criteria()
        self.running = False
        self.t_start = None
        self.t_end = None

    def start(self, t):
        self.running, self.t_start = True, t
        for c in self.criteria:
            c.t = c.s_v = c.s_abs = c.t_in = 0.0
            c.vmax, c.vmin = -1e30, 1e30

    def stop(self, t):
        self.running, self.t_end = False, t

    def add(self, kpi, dt):
        if not self.running:
            return
        for c in self.criteria:
            c.add(kpi.get(c.tag), dt)

    def result(self):
        rows = [c.result() for c in self.criteria]
        w = sum(r["weight"] for r in rows if r["score"] is not None)
        tot = sum(r["score"] * r["weight"] for r in rows if r["score"] is not None) / w if w else None
        return {
            "name": self.name,
            "running": self.running,
            "t_start": self.t_start,
            "t_end": self.t_end,
            "total": tot,
            "criteria": rows,
        }


# ---------------------------------------------------------------------------
class EventLog:
    def __init__(self, maxlen=5000):
        self.rows = []
        self.maxlen = maxlen

    def add(self, t_sim, source, tag, value, comment="", old=None):
        self.rows.append(
            {
                "t": t_sim,
                "wall": time.time(),
                "source": source,
                "tag": tag,
                "value": value,
                "old": old,
                "comment": comment,
            }
        )
        if len(self.rows) > self.maxlen:
            self.rows = self.rows[-self.maxlen :]


class Lab:
    """Hourly spot samples of clinker (free lime, phases) with analysis delay."""

    def __init__(self, interval=3600.0, delay=1200.0):
        self.interval, self.delay = interval, delay
        self.next_sample = 0.0
        self.pending = []
        self.last = {}
        self.results = []

    def scan(self, t, kpi):
        if t >= self.next_sample:
            self.pending.append(
                (
                    t,
                    {
                        k: kpi.get(k)
                        for k in (
                            "free_lime_pct",
                            "C3S_pct",
                            "C2S_pct",
                            "C3A_pct",
                            "C4AF_pct",
                            "LSF_clinker",
                            "hot_meal_doc_pct",
                        )
                    },
                )
            )
            self.next_sample = t + self.interval
        while self.pending and t >= self.pending[0][0] + self.delay:
            ts, vals = self.pending.pop(0)
            self.last = {"t_sample": ts, **vals}
            self.results.append(self.last)
        return self.last
