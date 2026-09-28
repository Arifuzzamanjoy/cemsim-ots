"""DCS/PLC layer: drives, group start/stop sequences, interlocks, PID loops, alarms.

Everything an operator touches goes through here; the process model only sees
the resulting actuator values (Plant.step(dt, actuators)).

Drives   : state machine STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED,
           FAULT on trip (needs acknowledge/reset).  Speed/position follows the
           setpoint with a rate limit (VFD / damper actuator ramp).
Groups   : ordered start sequences with inter-step delays and permissives
           (e.g. main burner: primary-air fan -> 60 s purge -> coal dosing),
           stop in reverse order, fast stop = all at once.
Interlocks: trips evaluated every scan (ID fan trip -> fuel & feed off, ...).
PID      : positional PI(D) with anti-windup, MAN/AUTO with bumpless transfer.
Alarms   : HH/H/L/LL limits with deadband, on- and off-delay; ISA-18.2 states
           (UNACK_ACTIVE, ACK_ACTIVE, UNACK_RTN).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

STOPPED, STARTING, RUNNING, STOPPING, FAULT = "STOPPED", "STARTING", "RUNNING", "STOPPING", "FAULT"


# ---------------------------------------------------------------------------
@dataclass
class Drive:
    tag: str
    desc: str
    sp: float = 0.0  # speed/position setpoint (engineering units)
    lo: float = 0.0
    hi: float = 100.0
    rate: float = 2.0  # EU per second ramp
    start_time: float = 5.0  # s to reach RUNNING
    min_run: float = 0.0  # running minimum (e.g. dosing rotor min)
    state: str = STOPPED
    pv: float = 0.0  # actual speed/position
    timer: float = 0.0
    fault_text: str = ""
    variable: bool = True  # False: on/off only (pv = hi when running)
    coast: float = 5.0  # EU/s run-down after stop/trip
    unit: str = "%"

    def cmd_start(self):
        if self.state in (STOPPED, STOPPING):
            self.state, self.timer = STARTING, 0.0
            return True
        return False

    def cmd_stop(self):
        if self.state in (RUNNING, STARTING):
            self.state, self.timer = STOPPING, 0.0
            return True
        return False

    def trip(self, why: str):
        if self.state != FAULT:
            self.state, self.fault_text = FAULT, why
            return True
        return False

    def reset(self):
        if self.state == FAULT:
            self.state, self.fault_text = STOPPED, ""
            return True
        return False

    @property
    def running(self):
        return self.state == RUNNING

    def scan(self, dt):
        if self.state == STARTING:
            self.timer += dt
            if self.timer >= self.start_time:
                self.state = RUNNING
        elif self.state == STOPPING:
            self.timer += dt
            if self.timer >= min(self.start_time, 3.0):
                self.state = STOPPED
        target = 0.0
        if self.state == RUNNING:
            target = max(self.sp, self.min_run) if self.variable else self.hi
            target = min(max(target, self.lo), self.hi)
        elif self.state == STARTING and self.variable:
            target = min(max(self.min_run, self.lo), self.hi)
        if self.state in (FAULT, STOPPED):
            # coast down (fans/drives) - dosing rotors have a very high rate => instant stop
            self.pv = max(0.0, self.pv - self.coast * dt)
        else:
            step = self.rate * dt
            self.pv += max(-step, min(step, target - self.pv))


@dataclass
class Group:
    tag: str
    desc: str
    steps: list  # [(drive_tag, delay_after_s)]
    permissive: Callable[[], tuple[bool, str]] = lambda: (True, "")
    state: str = STOPPED
    idx: int = 0
    timer: float = 0.0
    msg: str = ""


# ---------------------------------------------------------------------------
@dataclass
class PID:
    tag: str
    desc: str
    kp: float
    ti: float  # s (0 = no integral)
    sp: float
    out_lo: float
    out_hi: float
    direct: bool = False  # True: output rises when PV > SP
    mode: str = "MAN"  # MAN / AUTO
    out: float = 0.0
    pv: float = 0.0
    _i: float = 0.0

    def scan(self, pv, dt):
        self.pv = pv
        if self.mode != "AUTO":
            self._i = self.out
            return self.out
        e = (pv - self.sp) if self.direct else (self.sp - pv)
        p = self.kp * e
        if self.ti > 0:
            self._i += self.kp * e * dt / self.ti
        u = p + self._i
        if u > self.out_hi:
            u = self.out_hi
            self._i = min(self._i, self.out_hi - p)
        elif u < self.out_lo:
            u = self.out_lo
            self._i = max(self._i, self.out_lo - p)
        self.out = u
        return u

    def set_mode(self, mode):
        if mode == "AUTO" and self.mode != "AUTO":
            self._i = self.out  # bumpless
        self.mode = mode


# ---------------------------------------------------------------------------
@dataclass
class AlarmDef:
    tag: str  # KPI key
    desc: str
    hh: float | None = None
    h: float | None = None
    l: float | None = None
    ll: float | None = None
    db: float = 0.0
    delay: float = 5.0  # on-delay, s
    off_delay: float = 15.0  # off-delay, s (ISA-18.2 anti-chattering)
    unit: str = ""


@dataclass
class Alarm:
    id: str
    tag: str
    level: str
    desc: str
    value: float
    limit: float
    t_on: float
    state: str = "UNACK_ACTIVE"
    t_off: float | None = None
    priority: int = 2


class AlarmManager:
    PRI = {"HH": 1, "LL": 1, "H": 2, "L": 2, "TRIP": 1, "SYS": 3}

    def __init__(self, defs: list[AlarmDef]):
        self.defs = defs
        self.active: dict[str, Alarm] = {}
        self.history: deque = deque(maxlen=5000)  # bounded journal
        self._pending: dict[str, float] = {}
        self._off: dict[str, float] = {}

    def raise_event(self, aid, tag, level, desc, value, limit, t):
        if aid in self.active and self.active[aid].state != "UNACK_RTN":
            return None
        a = Alarm(aid, tag, level, desc, value, limit, t, priority=self.PRI.get(level, 2))
        self.active[aid] = a
        self.history.append(
            {"t": t, "id": aid, "tag": tag, "level": level, "desc": desc, "value": value, "event": "ACTIVE"}
        )
        return a

    def clear(self, aid, t):
        a = self.active.get(aid)
        if not a or a.state == "UNACK_RTN":
            return
        if a.state == "ACK_ACTIVE":
            del self.active[aid]
        else:
            a.state, a.t_off = "UNACK_RTN", t
        self.history.append(
            {"t": t, "id": aid, "tag": a.tag, "level": a.level, "desc": a.desc, "value": a.value, "event": "RTN"}
        )

    def scan(self, kpi: dict, t: float, dt: float):
        for d in self.defs:
            v = kpi.get(d.tag)
            if v is None:
                continue
            for lvl, lim, above in (("HH", d.hh, True), ("H", d.h, True), ("L", d.l, False), ("LL", d.ll, False)):
                if lim is None:
                    continue
                aid = f"{d.tag}.{lvl}"
                on = v > lim if above else v < lim
                off = v < lim - d.db if above else v > lim + d.db
                if on:
                    self._pending[aid] = self._pending.get(aid, 0.0) + dt
                    if self._pending[aid] >= d.delay:
                        self.raise_event(aid, d.tag, lvl, f"{d.desc} {lvl}", float(v), lim, t)
                        if aid in self.active:
                            self.active[aid].value = float(v)
                else:
                    self._pending.pop(aid, None)
                    if off and aid in self.active and self.active[aid].state != "UNACK_RTN":
                        self._off[aid] = self._off.get(aid, 0.0) + dt
                        if self._off[aid] >= d.off_delay:
                            self._off.pop(aid, None)
                            self.clear(aid, t)
                    else:
                        self._off.pop(aid, None)

    def ack(self, aid=None):
        ids = [aid] if aid else list(self.active)
        for i in ids:
            a = self.active.get(i)
            if not a:
                continue
            if a.state == "UNACK_ACTIVE":
                a.state = "ACK_ACTIVE"
            elif a.state == "UNACK_RTN":
                del self.active[i]

    def listing(self):
        return sorted(
            (
                {
                    "id": a.id,
                    "tag": a.tag,
                    "level": a.level,
                    "desc": a.desc,
                    "value": a.value,
                    "limit": a.limit,
                    "t_on": a.t_on,
                    "state": a.state,
                    "priority": a.priority,
                }
                for a in self.active.values()
            ),
            key=lambda r: (r["priority"], -r["t_on"]),
        )
