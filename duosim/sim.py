"""Simulator: process model + DCS layer + trainer, advanced in fixed steps.

Scan order per step (dt = 1 s):
  1. PID loops (AUTO) write drive setpoints from the last measured PVs
  2. group sequences advance, drives ramp -> actuator vector
  3. interlocks (trips) from the last PVs
  4. process model step
  5. alarms, lab, session scoring, trend history
"""

from __future__ import annotations

import math
import random
import re
from collections import deque
from dataclasses import asdict
from pathlib import Path

import numpy as np

from . import fileset
from .control import FAULT, PID, RUNNING, STARTING, STOPPED, STOPPING, AlarmDef, AlarmManager, Drive, Group
from .plant import Plant, heat_balance
from .trainer import DISTURBANCES, EventLog, Lab, Session

DATA = Path(__file__).resolve().parent.parent / "data"
DRIVE_STATE_FIELDS = ("state", "pv", "sp", "timer", "fault_text")
PID_STATE_FIELDS = ("mode", "sp", "out", "pv", "_i")
SNAP_NAME = re.compile(r"^[A-Za-z0-9_-]{1,60}$")


class CommandError(ValueError):
    """A rejected operator/trainer command (bad tag, value, state)."""


def _num(value, what, lo=None, hi=None):
    """Finite float within [lo, hi], else CommandError."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise CommandError(f"{what}: '{value}' is not a number") from None
    if not math.isfinite(v):
        raise CommandError(f"{what}: value must be finite")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise CommandError(f"{what}: {v:g} outside allowed range [{lo:g}, {hi:g}]")
    return v


TREND_TAGS = [
    "T_bz",
    "T_kiln_inlet",
    "O2_kiln_inlet",
    "CO_kiln_inlet_ppm",
    "NO_kiln_inlet_ppm",
    "T_calciner",
    "T_ph_exit",
    "O2_ph_exit",
    "CO_ph_exit_ppm",
    "p_ph_exit",
    "p_hood",
    "p_kiln_inlet",
    "T_sec_air",
    "T_ter_air",
    "T_clinker_out",
    "clinker_tph",
    "clinker_tph_avg",
    "spec_heat_kJkg",
    "free_lime_pct",
    "C3S_pct",
    "kiln_drive_kW",
    "hot_meal_doc_pct",
    "kiln_feed_tph",
    "coal_kiln_tph",
    "coal_cal_tph",
    "kiln_rpm",
    "id_fan_pct",
    "vent_fan_pct",
    "dp_undergrate",
    "grate_spm",
    "T_S1",
    "T_S2",
    "T_S3",
    "T_S4",
    "shell_max_C",
    "id_fan_kW",
    "T_vent",
    "flame_len_m",
    "tad_pct",
    "pa_fan_pct",
    "cone_buildup_t",
]


def _mk_drives(P):
    n = P.nominal
    d = [
        Drive("ID_FAN", "ID fan", sp=n.id_fan_pct, lo=0, hi=100, rate=1.0, start_time=20, coast=3.0),
        Drive("VENT_FAN", "Cooler vent fan", sp=n.vent_fan_pct, lo=0, hi=100, rate=1.0, start_time=10, coast=3.0),
        Drive("PA_FAN", "Primary air fan", sp=n.primary_air_pct, lo=0, hi=100, rate=2.0, start_time=8, coast=5.0),
        Drive("CRUSHER", "Clinker crusher", variable=False, hi=100, start_time=3, coast=100),
        Drive(
            "GRATE",
            "Cooler grate drive",
            sp=n.grate_spm,
            lo=0,
            hi=25,
            rate=0.5,
            start_time=4,
            min_run=3.0,
            coast=100,
            unit="spm",
        ),
    ]
    for i, f in enumerate(n.cooler_fan_pct):
        d.append(Drive(f"CF{i + 1}", f"Cooler fan {i + 1}", sp=f, lo=0, hi=100, rate=2.0, start_time=6, coast=4.0))
    d += [
        Drive(
            "KILN_DRIVE",
            "Kiln main drive",
            sp=n.kiln_rpm,
            lo=0,
            hi=4.5,
            rate=0.02,
            start_time=15,
            min_run=0.3,
            coast=0.2,
            unit="rpm",
        ),
        Drive(
            "KILN_FEED", "Kiln feed", sp=n.kiln_feed_tph, lo=0, hi=260, rate=1.0, start_time=10, coast=100, unit="t/h"
        ),
        Drive(
            "COAL_KILN",
            "Main burner coal dosing",
            sp=n.coal_kiln_tph,
            lo=0,
            hi=10,
            rate=0.05,
            start_time=5,
            min_run=0.8,
            coast=100,
            unit="t/h",
        ),  # 12:1 rotor turndown
        Drive(
            "COAL_CAL",
            "Calciner coal dosing",
            sp=n.coal_cal_tph,
            lo=0,
            hi=14,
            rate=0.05,
            start_time=5,
            min_run=2.0,
            coast=100,
            unit="t/h",
        ),
        Drive("TAD", "Tertiary air damper", sp=n.tad_pct, lo=0, hi=100, rate=1.0, start_time=0.5, coast=0, unit="%"),
    ]
    return {x.tag: x for x in d}


class Simulator:
    def __init__(self, plant: Plant | None = None):
        self.plant = plant or Plant()
        P = self.plant.P
        self.dt = 1.0
        self.speed = 1.0
        self.running = False
        self.drives = _mk_drives(P)
        self.drives["TAD"].state = RUNNING
        self.drives["TAD"].pv = P.nominal.tad_pct
        self.pids = {
            "PIC_HOOD": PID(
                "PIC_HOOD",
                "Hood pressure -> vent fan",
                kp=6.0,
                ti=25.0,
                sp=-0.5,
                out_lo=5.0,
                out_hi=100.0,
                direct=True,
                mode="AUTO",
                out=P.nominal.vent_fan_pct,
            ),
            "TIC_CAL": PID(
                "TIC_CAL",
                "Calciner temperature -> calciner coal",
                kp=0.03,
                ti=90.0,
                sp=875.0,
                out_lo=2.0,
                out_hi=14.0,
                direct=False,
                mode="AUTO",
                out=P.nominal.coal_cal_tph,
            ),
            "PIC_UG": PID(
                "PIC_UG",
                "Undergrate pressure -> grate speed",
                kp=0.25,
                ti=90.0,
                sp=55.0,
                out_lo=3.0,
                out_hi=25.0,
                direct=True,
                mode="AUTO",
                out=P.nominal.grate_spm,
            ),
            "AIC_O2": PID(
                "AIC_O2",
                "Preheater exit O2 -> ID fan",
                kp=2.0,
                ti=120.0,
                sp=2.8,
                out_lo=20.0,
                out_hi=100.0,
                direct=False,
                mode="MAN",
                out=P.nominal.id_fan_pct,
            ),
        }
        self.pid_sp_range = {
            "PIC_HOOD": (-10.0, 5.0),
            "TIC_CAL": (700.0, 1000.0),
            "PIC_UG": (20.0, 90.0),
            "AIC_O2": (0.5, 8.0),
        }
        self.pid_target = {
            "PIC_HOOD": ("VENT_FAN", "p_hood"),
            "TIC_CAL": ("COAL_CAL", "T_calciner"),
            "PIC_UG": ("GRATE", "dp_undergrate"),
            "AIC_O2": ("ID_FAN", "O2_ph_exit"),
        }
        self.groups = self._mk_groups()
        self.alarms = AlarmManager(self._alarm_defs())
        self.events = EventLog()
        self.lab = Lab()
        self.session = Session()
        self.dist = {d["id"]: d.get("default") for d in DISTURBANCES if d["kind"] == "value"}
        self.history = deque(maxlen=int(12 * 3600 / 5))  # 12 h @ 5 s
        self._hist_timer = 0.0
        self._trip_timers: dict[str, float] = {}
        self.trip_bypass: set[str] = set()  # maintenance bypass of analog trips (co, tex, tcal)
        self._rng = random.Random(1)
        self._feed_noise = 0.0
        self._coal_noise = 0.0
        self.kpi = {}
        self.lab_last = {}
        self.snapshots: dict[str, dict] = {}

    # ------------------------------------------------------------------
    def _mk_groups(self):
        d = self.drives

        def running(t):
            return d[t].running

        def perm_burner():
            if not running("ID_FAN"):
                return False, "ID fan not running"
            return True, ""

        def perm_calc():
            if not running("ID_FAN"):
                return False, "ID fan not running"
            if self.kpi.get("T_calciner", 0) < 750:
                return False, "calciner < 750 C (coal will not ignite reliably)"
            if d["TAD"].pv < 10:
                return False, "tertiary air damper < 10 %"
            return True, ""

        def perm_feed():
            for t, why in (
                ("KILN_DRIVE", "kiln drive"),
                ("ID_FAN", "ID fan"),
                ("GRATE", "cooler grate"),
                ("CF1", "cooler fan 1"),
            ):
                if not running(t):
                    return False, f"{why} not running"
            return True, ""

        return {
            "G_EXH": Group("G_EXH", "Kiln exhaust (ID fan)", [("ID_FAN", 0)]),
            "G_COOLER": Group(
                "G_COOLER",
                "Clinker cooler",
                [("CRUSHER", 2), ("GRATE", 2), ("VENT_FAN", 3)] + [(f"CF{i}", 2) for i in range(1, 7)],
            ),
            "G_KILN": Group("G_KILN", "Kiln main drive", [("KILN_DRIVE", 0)]),
            "G_BURNER": Group(
                "G_BURNER",
                "Main burner (BMS)",
                [("PA_FAN", 0), ("PURGE", 60), ("COAL_KILN", 0)],
                permissive=perm_burner,
            ),
            "G_CALC": Group("G_CALC", "Calciner burner", [("COAL_CAL", 0)], permissive=perm_calc),
            "G_FEED": Group("G_FEED", "Kiln feed", [("KILN_FEED", 0)], permissive=perm_feed),
        }

    def _alarm_defs(self):
        return [
            AlarmDef("T_bz", "Burning zone temperature", hh=1560, h=1520, l=1390, ll=1340, db=5, unit="C"),
            AlarmDef("O2_kiln_inlet", "O2 kiln inlet", h=6.0, l=1.5, ll=0.8, db=0.2, unit="%"),
            AlarmDef("CO_kiln_inlet_ppm", "CO kiln inlet", hh=5000, h=1500, db=500, unit="ppm"),
            AlarmDef("T_calciner", "Calciner outlet temperature", hh=960, h=920, l=840, db=3, unit="C"),
            AlarmDef("T_ph_exit", "Preheater exit temperature", hh=450, h=380, db=3, unit="C"),
            AlarmDef("CO_ph_exit_ppm", "CO preheater exit", hh=5000, h=2000, db=100, unit="ppm"),
            AlarmDef("p_hood", "Kiln hood pressure", h=0.0, l=-3.0, db=0.1, unit="mbar"),
            AlarmDef("T_clinker_out", "Clinker temperature cooler outlet", hh=220, h=160, db=3, unit="C"),
            AlarmDef("T_sec_air", "Secondary air temperature", l=850, db=5, unit="C"),
            AlarmDef("shell_max_C", "Kiln shell temperature", hh=420, h=360, db=3, unit="C"),
            AlarmDef("kiln_drive_kW", "Kiln drive power", h=750, db=10, unit="kW"),
            AlarmDef("T_kiln_inlet", "Kiln inlet gas temperature", h=1230, l=900, db=5, unit="C"),
            AlarmDef("dp_undergrate", "Cooler undergrate pressure", h=75, l=30, db=1, unit="mbar"),
            AlarmDef("cone_buildup_t", "Cyclone 4 cone material build-up", h=1.0, db=0.1, unit="t"),
            AlarmDef("lab_free_lime", "Free lime (lab)", hh=3.0, h=2.0, db=0.1, delay=0, unit="%"),
        ]

    # ------------------------------------------------------------------
    # Operator / trainer commands
    # ------------------------------------------------------------------
    def log(self, source, tag, value, comment="", old=None):
        self.events.add(self.plant.t, source, tag, value, comment, old)

    def cmd_group(self, gtag, cmd, source="OPERATOR"):
        g = self.groups.get(gtag)
        if g is None:
            raise CommandError(f"unknown group '{gtag}'")
        if cmd not in ("start", "stop", "fast_stop", "stop_start", "ack"):
            raise CommandError(f"unknown group command '{cmd}'")
        if cmd == "start":
            ok, why = g.permissive()
            if not ok:
                g.msg = f"start blocked: {why}"
                self.log(source, gtag, "START BLOCKED", why)
                return False, why
            if g.state in (STOPPED, STOPPING):
                g.state, g.idx, g.timer, g.msg = STARTING, 0, 0.0, "starting"
            self.log(source, gtag, "START", g.desc)
        elif cmd in ("stop", "fast_stop"):
            if cmd == "fast_stop":
                for t, _ in g.steps:
                    if t in self.drives:
                        self.drives[t].cmd_stop()
                g.state, g.msg = STOPPED, "fast stop"
            else:
                g.state, g.idx, g.timer, g.msg = STOPPING, len(g.steps) - 1, 0.0, "stopping"
            self.log(source, gtag, cmd.upper(), g.desc)
        elif cmd == "stop_start":
            if g.state == STARTING:
                g.state, g.msg = STOPPED, "start-up interrupted"
                self.log(source, gtag, "STOP START-UP", g.desc)
        elif cmd == "ack":
            for t, _ in g.steps:
                if t in self.drives:
                    self.drives[t].reset()
            self.alarms.ack()
            g.msg = ""
            self.log(source, gtag, "ACK", "fault reset")
        return True, ""

    def cmd_drive(self, tag, cmd, value=None, source="OPERATOR"):
        d = self.drives.get(tag)
        if d is None:
            raise CommandError(f"unknown drive '{tag}'")
        if cmd not in ("sp", "start", "stop", "reset"):
            raise CommandError(f"unknown drive command '{cmd}'")
        if cmd == "sp":
            v = _num(value, f"{tag} setpoint ({d.unit})", d.lo, d.hi)  # reject, like a DCS
            old = d.sp
            d.sp = v
            for pt, (dt_, _) in self.pid_target.items():
                if dt_ == tag and self.pids[pt].mode == "AUTO":
                    self.pids[pt].set_mode("MAN")
                    self.log("SYSTEM", pt, "MAN", "manual setpoint entry on output")
            self.log(source, tag + ".SP", round(d.sp, 3), d.desc, old)
        elif cmd == "start":
            d.cmd_start()
            self.log(source, tag, "START", d.desc)
        elif cmd == "stop":
            d.cmd_stop()
            self.log(source, tag, "STOP", d.desc)
        elif cmd == "reset":
            d.reset()
            self.log(source, tag, "RESET", d.desc)

    def cmd_pid(self, tag, field, value, source="OPERATOR"):
        p = self.pids.get(tag)
        if p is None:
            raise CommandError(f"unknown controller '{tag}'")
        if field not in ("mode", "sp", "out"):
            raise CommandError(f"unknown controller field '{field}'")
        if field == "mode":
            if value not in ("AUTO", "MAN"):
                raise CommandError(f"mode must be AUTO or MAN, not '{value}'")
            old = p.mode
            p.set_mode(value)
            if value == "MAN":
                p.out = self.drives[self.pid_target[tag][0]].sp
            self.log(source, tag + ".MODE", value, p.desc, old)
        elif field == "sp":
            lo, hi = self.pid_sp_range[tag]
            v = _num(value, f"{tag} setpoint", lo, hi)
            old = p.sp
            p.sp = v
            self.log(source, tag + ".SP", p.sp, p.desc, old)
        elif field == "out":
            p.out = min(max(_num(value, f"{tag} output"), p.out_lo), p.out_hi)
            self.drives[self.pid_target[tag][0]].sp = p.out
            self.log(source, tag + ".OUT", p.out, p.desc)

    def cmd_disturbance(self, did, value=None, source="TRAINER"):
        pl = self.plant
        spec = next((d for d in DISTURBANCES if d["id"] == did), None)
        if spec is None:
            raise CommandError(f"unknown disturbance '{did}'")
        if spec["kind"] == "value":
            v = _num(value, spec["label"], spec["min"], spec["max"])
            old = self.dist.get(did)
            self.dist[did] = v
            if did == "coal_lhv_kiln":
                pl.fuel_k.LHV = v
            elif did == "coal_lhv_cal":
                pl.fuel_c.LHV = v
            elif did == "coal_ash":
                for f in (pl.fuel_k, pl.fuel_c):
                    rest = 1.0 - f.ash
                    k = (1.0 - v / 100.0) / rest
                    f.C, f.H, f.O, f.N, f.moisture = (x * k for x in (f.C, f.H, f.O, f.N, f.moisture))
                    f.ash = v / 100.0
                    # LHV follows the combustible content
                    f.LHV = f.LHV * k
                self.dist["coal_lhv_kiln"] = pl.fuel_k.LHV
                self.dist["coal_lhv_cal"] = pl.fuel_c.LHV
            elif did == "rawmix_lsf":
                pl.rawmix.LSF = v
            elif did == "rawmix_sm":
                pl.rawmix.SM = v
            elif did == "rawmix_moisture":
                pl.rawmix.moisture = v / 100.0
            elif did == "seal_false_air":
                pl.dr.seal_mult = v
            elif did == "ph_false_air":
                pl.dr.ph_false_mult = v
            elif did == "kiln_ring":
                pl.kiln.ring_factor = v
                pl.dr.kiln_R_mult = 1.0 + 3.0 * (v - 1.0)
            elif did == "ambient_T":
                pl.T_amb = v + 273.15
            self.log(source, "DIST." + did, v, spec["label"], old)
        else:
            if did == "cyclone4_block":
                pl.ph.blocked[3] = True
                pl.dr.ph_R_mult = 1.15
            elif did == "cyclone4_release":
                pl.ph.blocked[3] = False
                pl.ph.flush[3] = True
                pl.dr.ph_R_mult = 1.0
            elif did == "cooler_fan1_fail":
                self.drives["CF1"].trip("motor overload")
            elif did == "id_fan_trip":
                self.drives["ID_FAN"].trip("motor protection")
            elif did == "kiln_drive_trip":
                self.drives["KILN_DRIVE"].trip("drive fault")
            elif did == "pa_fan_trip":
                self.drives["PA_FAN"].trip("motor protection")
            self.log(source, "DIST." + did, "TRIGGER", spec["label"])

    # ------------------------------------------------------------------
    def _scan_groups(self, dt):
        for g in self.groups.values():
            if g.state == STARTING:
                if g.idx >= len(g.steps):
                    g.state, g.msg = RUNNING, "running"
                    continue
                tag, delay = g.steps[g.idx]
                if tag == "PURGE":
                    g.timer += dt
                    g.msg = f"purge {max(0, delay - g.timer):.0f} s"
                    if not self.drives["ID_FAN"].running:
                        g.state, g.msg = STOPPED, "purge interrupted (ID fan)"
                    elif g.timer >= delay:
                        g.idx, g.timer = g.idx + 1, 0.0
                    continue
                d = self.drives[tag]
                if d.state == FAULT:
                    g.state, g.msg = STOPPED, f"{tag} fault"
                    continue
                if d.state in (STOPPED, STOPPING):
                    d.cmd_start()
                if d.running:
                    g.timer += dt
                    if g.timer >= delay:
                        g.idx, g.timer = g.idx + 1, 0.0
            elif g.state == STOPPING:
                if g.idx < 0:
                    g.state, g.msg = STOPPED, "stopped"
                    continue
                tag, _ = g.steps[g.idx]
                if tag != "PURGE":
                    self.drives[tag].cmd_stop()
                g.idx -= 1
            elif g.state == RUNNING:
                if any(self.drives[t].state == FAULT for t, _ in g.steps if t in self.drives):
                    g.state, g.msg = FAULT, "drive fault"

    def _trip(self, tags, why):
        for t in tags:
            d = self.drives[t]
            if d.state in (RUNNING, STARTING) and d.trip(why):
                self.log("INTERLOCK", t, "TRIP", why)
                self.alarms.raise_event(f"{t}.TRIP", t, "TRIP", f"{d.desc} tripped: {why}", 0, 0, self.plant.t)

    def _interlocks(self, dt):
        d = self.drives
        if d["ID_FAN"].state in (FAULT, STOPPED):
            self._trip(["COAL_KILN", "COAL_CAL", "KILN_FEED"], "ID fan not running")
        if d["PA_FAN"].state in (FAULT, STOPPED):
            self._trip(["COAL_KILN"], "primary air fan not running")
        if d["KILN_DRIVE"].state in (FAULT, STOPPED):
            self._trip(["KILN_FEED"], "kiln drive not running")
        if d["GRATE"].state in (FAULT, STOPPED) or d["CF1"].state in (FAULT, STOPPED):
            self._trip(["KILN_FEED"], "cooler not running")
        k = self.kpi
        for key, cond, delay, tags, why in (
            (
                "co",
                k.get("CO_ph_exit_ppm", 0.0) > 5000,
                5.0,
                ["COAL_KILN", "COAL_CAL"],
                "CO > 0.5 % at preheater exit (filter protection)",
            ),
            (
                "tex",
                k.get("T_ph_exit", 0.0) > 550.0,
                10.0,
                ["COAL_KILN", "COAL_CAL"],
                "preheater exit gas > 550 C (ID fan / conditioning tower protection)",
            ),
            (
                "tcal",
                k.get("T_calciner", 0.0) > 1050.0,
                10.0,
                ["COAL_CAL"],
                "calciner outlet > 1050 C (refractory protection)",
            ),
        ):
            if key in self.trip_bypass:
                self._trip_timers[key] = 0.0
                continue
            self._trip_timers[key] = self._trip_timers.get(key, 0.0) + dt if cond else 0.0
            if self._trip_timers[key] > delay:
                self._trip(tags, why)
        # a faulted cooler fan also stops the air in the model
        self.plant.cooler.fan_fault[:] = [d[f"CF{i}"].state == FAULT for i in range(1, 7)]

    def actuators(self):
        d = self.drives
        feed = d["KILN_FEED"].pv * (1.0 + self._feed_noise)
        coal_k = d["COAL_KILN"].pv * (1.0 + self._coal_noise)
        coal_c = d["COAL_CAL"].pv * (1.0 + self._coal_noise * 0.7)
        return {
            "kiln_feed_tph": max(feed, 0.0),
            "coal_kiln_tph": max(coal_k, 0.0),
            "coal_cal_tph": max(coal_c, 0.0),
            "kiln_rpm": d["KILN_DRIVE"].pv,
            "id_pct": d["ID_FAN"].pv,
            "vent_pct": d["VENT_FAN"].pv,
            "pa_pct": d["PA_FAN"].pv,
            "tad_pct": d["TAD"].pv,
            "cooler_fan_pct": [d[f"CF{i}"].pv for i in range(1, 7)],
            "grate_spm": d["GRATE"].pv,
            "flame_shape": self.dist.get("flame_shape", 1.0),
        }

    # ------------------------------------------------------------------
    def step(self):
        dt = self.dt
        k = self.kpi
        for tag, pid in self.pids.items():
            drv, pv = self.pid_target[tag]
            if pv in k and pid.mode == "AUTO" and self.drives[drv].running:
                self.drives[drv].sp = pid.scan(k[pv], dt)
            else:
                pid.pv = k.get(pv, pid.pv)
                if pid.mode == "AUTO":
                    pid._i = self.drives[drv].sp  # track (no windup while drive stopped)
                    pid.out = self.drives[drv].sp
        self._scan_groups(dt)
        for d in self.drives.values():
            d.scan(dt)
        self._interlocks(dt)
        # random-walk fluctuations (trainer)
        fa = self.dist.get("feed_fluct", 0.0) / 100.0
        ca = self.dist.get("coal_fluct", 0.0) / 100.0
        self._feed_noise += -self._feed_noise * dt / 120.0 + fa * math.sqrt(2 * dt / 120.0) * self._rng.gauss(0, 1)
        self._coal_noise += -self._coal_noise * dt / 30.0 + ca * math.sqrt(2 * dt / 30.0) * self._rng.gauss(0, 1)
        self.kpi = self.plant.step(dt, self.actuators())
        k = self.kpi
        k["tad_pct"] = self.drives["TAD"].pv
        k["id_fan_pct"] = self.drives["ID_FAN"].pv
        k["vent_fan_pct"] = self.drives["VENT_FAN"].pv
        k["pa_fan_pct"] = self.drives["PA_FAN"].pv
        k["grate_spm"] = self.drives["GRATE"].pv
        self.lab_last = self.lab.scan(self.plant.t, k)
        if self.lab_last:
            k["lab_free_lime"] = self.lab_last.get("free_lime_pct")
        self.alarms.scan(k, self.plant.t, dt)
        self.session.add(k, dt)
        self._hist_timer += dt
        if self._hist_timer >= 5.0:
            self._hist_timer = 0.0
            self.history.append({"t": self.plant.t, **{t: float(k.get(t, 0.0) or 0.0) for t in TREND_TAGS}})

    def run(self, seconds):
        n = round(seconds / self.dt)
        for _ in range(n):
            self.step()

    # ------------------------------------------------------------------
    def status(self):
        """Compact JSON-able status for the HMI."""
        k = {kk: (float(v) if isinstance(v, (int, float, np.floating)) else v) for kk, v in self.kpi.items()}
        return {
            "t": self.plant.t,
            "speed": self.speed,
            "running": self.running,
            "kpi": k,
            "drives": {
                t: {
                    "state": d.state,
                    "pv": d.pv,
                    "sp": d.sp,
                    "desc": d.desc,
                    "unit": d.unit,
                    "lo": d.lo,
                    "hi": d.hi,
                    "fault": d.fault_text,
                }
                for t, d in self.drives.items()
            },
            "groups": {
                t: {"state": g.state, "desc": g.desc, "msg": g.msg, "steps": [s for s, _ in g.steps]}
                for t, g in self.groups.items()
            },
            "pids": {
                t: {
                    "mode": p.mode,
                    "sp": p.sp,
                    "pv": p.pv,
                    "out": p.out,
                    "desc": p.desc,
                    "target": self.pid_target[t][0],
                }
                for t, p in self.pids.items()
            },
            "alarms": self.alarms.listing(),
            "lab": self.lab_last,
            "dist": self.dist,
            "profiles": self.plant.profiles(),
            "cooler_bed_m": [float(x) for x in self.plant.cooler.out.get("bed_m", [])],
        }

    # ------------------------------------------------------------------
    def snapshot(self, comment="") -> dict:
        return {
            "comment": comment,
            "plant": self.plant.snapshot(),
            "drives": {t: asdict(d) for t, d in self.drives.items()},
            "pids": {t: asdict(p) for t, p in self.pids.items()},
            "groups": {
                t: {"state": g.state, "idx": g.idx, "timer": g.timer, "msg": g.msg} for t, g in self.groups.items()
            },
            "dist": dict(self.dist),
            "kpi": dict(self.kpi),
        }

    def restore(self, snap: dict):
        self.plant.restore(snap["plant"])
        # only *runtime state* is restored; configuration (ranges, rates, minimum
        # speeds, controller gains) always comes from the code, so fixes to the
        # plant definition are never overridden by an old fileset
        for t, st in snap["drives"].items():
            if t in self.drives:
                for k in DRIVE_STATE_FIELDS:
                    if k in st:
                        setattr(self.drives[t], k, st[k])
        for t, st in snap["pids"].items():
            if t in self.pids:
                for k in PID_STATE_FIELDS:
                    if k in st:
                        setattr(self.pids[t], k, st[k])
        for t, st in snap["groups"].items():
            for k, v in st.items():
                setattr(self.groups[t], k, v)
        self.dist = dict(snap.get("dist", self.dist))
        self.kpi = dict(snap.get("kpi", {}))
        self.history.clear()
        self.alarms.active.clear()
        self.log("TRAINER", "SNAPSHOT", "LOAD", snap.get("comment", ""))

    @staticmethod
    def _snap_path(name):
        if not isinstance(name, str) or not SNAP_NAME.match(name):
            raise CommandError("fileset name: 1-60 characters of A-Z a-z 0-9 _ -")
        return DATA / "snapshots" / f"{name}.json"

    def save_file(self, name, comment=""):
        path = self._snap_path(name)
        fileset.dump(self.snapshot(comment), path)
        return path

    def load_file(self, name):
        path = self._snap_path(name)
        if not path.exists():
            raise CommandError(f"fileset '{name}' not found")
        try:
            snap = fileset.load(path)
        except ValueError as e:
            raise CommandError(str(e)) from None
        self.restore(snap)

    @staticmethod
    def list_files():
        d = DATA / "snapshots"
        if not d.exists():
            return []
        return [{"name": p.stem, "comment": fileset.comment(p)} for p in sorted(d.glob("*.json"))]

    def set_all_running(self):
        """Put every drive/group into RUNNING at its setpoint (nominal plant)."""
        for d in self.drives.values():
            d.state, d.pv = RUNNING, (d.sp if d.variable else d.hi)
        for g in self.groups.values():
            g.state, g.msg = RUNNING, "running"

    def heat_balance(self):
        return heat_balance(self.plant)
