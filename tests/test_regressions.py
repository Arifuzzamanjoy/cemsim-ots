"""Regression tests for defects found during validation (see docs/VALIDATION.md)."""

import math

import numpy as np
import pytest

from cemsim import thermo as th
from cemsim.sim import CommandError, Simulator


@pytest.fixture(scope="module")
def nominal_snap():
    sim = Simulator()
    try:
        sim.load_file("nominal")
    except CommandError:
        pytest.skip("nominal fileset missing")
    return sim.snapshot()


def fresh(snap):
    s = Simulator()
    s.restore(snap)
    return s


# ---- V1: command validation -------------------------------------------------
@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.cmd_pid("TIC_CAL", "sp", float("nan")),
        lambda s: s.cmd_pid("TIC_CAL", "sp", 5000),
        lambda s: s.cmd_pid("TIC_CAL", "mode", "TURBO"),
        lambda s: s.cmd_drive("ID_FAN", "sp", None),
        lambda s: s.cmd_drive("ID_FAN", "sp", 150),
        lambda s: s.cmd_drive("NOPE", "sp", 1),
        lambda s: s.cmd_disturbance("coal_lhv_kiln", -5),
        lambda s: s.cmd_disturbance("nope", 1),
        lambda s: s.cmd_group("G_FEED", "dance"),
        lambda s: s.load_file("../../README"),
        lambda s: s.load_file("does_not_exist"),
    ],
)
def test_bad_commands_rejected(nominal_snap, call):
    sim = fresh(nominal_snap)
    before = (sim.pids["TIC_CAL"].sp, sim.drives["ID_FAN"].sp, sim.plant.fuel_k.LHV)
    with pytest.raises(CommandError):
        call(sim)
    assert (sim.pids["TIC_CAL"].sp, sim.drives["ID_FAN"].sp, sim.plant.fuel_k.LHV) == before


# ---- V2: zero gas flow (all fans stopped) -------------------------------------
def test_all_fans_stopped_no_crash_and_draught_converges(nominal_snap):
    sim = fresh(nominal_snap)
    for g in ("G_FEED", "G_CALC", "G_BURNER"):
        sim.cmd_group(g, "stop")
    sim.run(300)
    sim.cmd_group("G_COOLER", "stop")
    sim.cmd_group("G_EXH", "stop")
    sim.run(900)
    k = sim.kpi
    assert all(math.isfinite(float(v)) for v in k.values() if isinstance(v, (float, np.floating)))
    assert k["draught_resid"] < 1e-3
    assert abs(k["p_hood"]) < 0.5


def test_id_fan_trip_draught_converges(nominal_snap):
    sim = fresh(nominal_snap)
    sim.cmd_disturbance("id_fan_trip")
    sim.run(600)
    assert sim.kpi["draught_resid"] < 1e-3
    assert sim.kpi["p_hood"] > 0  # cooler fans pressurise the hood


# ---- V3: emptying kiln must not create unphysical temperatures -------------------
def test_empty_kiln_no_overheating(nominal_snap):
    sim = fresh(nominal_snap)
    for g in ("G_FEED", "G_CALC", "G_BURNER"):
        sim.cmd_group(g, "stop")
    sim.run(4200)
    kl = sim.plant.kiln
    mass = kl.n @ th.M_SOLID
    hot_src = np.maximum(kl.T_w, kl.T_g) + 1.0
    filled = mass > 1.0
    assert np.all(kl.T_b[filled] <= hot_src[filled] + 50.0)
    assert sim.kpi["T_bz"] < 1500.0
    assert sim.kpi["T_bed_max"] < 1600.0


# ---- V4: filesets restore state only, never configuration ---------------------------
def test_restore_does_not_override_configuration(nominal_snap):
    snap = dict(nominal_snap)
    snap["drives"] = {t: dict(v) for t, v in nominal_snap["drives"].items()}
    snap["drives"]["COAL_KILN"]["min_run"] = 7.7
    snap["drives"]["COAL_KILN"]["hi"] = 1.0
    sim = Simulator()
    sim.restore(snap)
    assert sim.drives["COAL_KILN"].min_run == 0.8
    assert sim.drives["COAL_KILN"].hi == 10.0


# ---- V5: protection interlocks and permissives ------------------------------------
def test_preheater_exit_high_high_trips_fuel(nominal_snap):
    sim = fresh(nominal_snap)
    sim.cmd_group("G_FEED", "stop")  # no meal -> exit gas overheats
    sim.run(1200)
    assert sim.drives["COAL_KILN"].state == "FAULT"
    assert any("550" in e["comment"] for e in sim.events.rows if e["source"] == "INTERLOCK")


def test_trip_bypass_is_honoured(nominal_snap):
    sim = fresh(nominal_snap)
    sim.trip_bypass = {"tex"}
    sim.cmd_group("G_FEED", "stop")
    sim.run(600)
    assert sim.drives["COAL_KILN"].running


def test_calciner_burner_permissive(nominal_snap):
    sim = fresh(nominal_snap)
    sim.cmd_group("G_CALC", "fast_stop")
    sim.kpi["T_calciner"] = 700.0
    ok, why = sim.cmd_group("G_CALC", "start")
    assert not ok and "750" in why


def test_calciner_coal_ignites_easier_in_air():
    """Char burnout ~ sqrt(O2): at 18 % O2 a cold calciner burns more coal than at 2 %."""
    from cemsim.params import PreheaterParams

    p = PreheaterParams()
    T = 950.0

    def beta(xo2):
        return 1 - math.exp(-p.burn_A * math.exp(-p.burn_E / (8.314 * T)) * p.tau_gas[4] * math.sqrt(xo2 / 0.021))

    assert beta(0.18) > 2.0 * beta(0.02)


# ---- V6: shipped hot standby is consistent with the interlocks -------------------------
def test_hot_standby_does_not_trip_on_load():
    sim = Simulator()
    try:
        sim.load_file("hot_standby")
    except CommandError:
        pytest.skip("hot_standby fileset missing")
    sim.run(600)
    assert not [e for e in sim.events.rows if e["source"] == "INTERLOCK"]
    assert sim.drives["COAL_KILN"].running
