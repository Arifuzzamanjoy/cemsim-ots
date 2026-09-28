"""Plant-level verification: conservation, draught, control logic, response directions."""

import pytest

from cemsim import thermo as th
from cemsim.control import PID, Drive
from cemsim.params import DraughtParams
from cemsim.sim import CommandError, Simulator
from cemsim.units.draught import Draught


@pytest.fixture(scope="module")
def nominal():
    sim = Simulator()
    try:
        sim.load_file("nominal")
    except CommandError:
        pytest.skip("run scripts/calibrate.py first to create data/snapshots/nominal.json")
    sim.run(30)
    return sim.snapshot()


def fresh(snap):
    sim = Simulator()
    sim.restore(snap)
    return sim


def test_draught_reproduces_design_point():
    d = Draught(DraughtParams())
    D = DraughtParams()
    u = {
        "T_kiln_mean": D.T_kiln_mean,
        "m_pa": 0.0,
        "tad": D.tad_design,
        "T_ta": D.T_ta,
        "vent": D.vent_speed_design,
        "m_cool": 0.0,
        "m_gen": 0.0,
        "T_ph_mean": D.T_ph_mean,
        "id": D.id_speed_design,
        "rho_exit": d.rho_d,
        "p_x_prev": D.p_ph_exit,
    }
    f0 = d.flows(D.p_hood, D.p_kiln_inlet, u)
    assert f0["m_kiln"] == pytest.approx(D.m_kiln_air, rel=1e-9)
    assert f0["m_ta"] == pytest.approx(D.m_ta, rel=1e-9)


def test_mass_and_energy_conservation(nominal):
    sim = fresh(nominal)
    p = sim.plant
    p.acc = dict.fromkeys(p.acc, 0.0)
    dt = sim.dt
    m0, H0 = p.holdup()
    b0 = (float(p.clk_n @ th.M_SOLID + p.hot_meal @ th.M_SOLID) * dt, (p.clk_H + p.hot_meal_H) * dt)
    sim.run(900)
    m1, H1 = p.holdup()
    b1 = (float(p.clk_n @ th.M_SOLID + p.hot_meal @ th.M_SOLID) * dt, (p.clk_H + p.hot_meal_H) * dt)
    a = p.acc
    mass_err = a["m_in"] - a["m_out"] - ((m1 + b1[0]) - (m0 + b0[0]))
    assert abs(mass_err) / a["m_in"] < 1e-5
    fuel_energy = (sim.kpi["coal_kiln_tph"] + sim.kpi["coal_cal_tph"]) / 3.6 * 25600 * 900
    e_err = a["H_in"] - a["H_out"] - ((H1 + b1[1]) - (H0 + b0[1]))
    assert abs(e_err) / fuel_energy < 3e-3


def test_nominal_state_is_realistic(nominal):
    k = fresh(nominal).kpi
    assert 1400 < k["T_bz"] < 1500
    assert 850 < k["T_calciner"] < 900
    assert 85 < k["hot_meal_doc_pct"] < 99
    assert 1.5 < k["O2_kiln_inlet"] < 5
    assert 250 < k["T_ph_exit"] < 380
    assert 115 < k["clinker_tph_avg"] < 135
    assert 2900 < k["spec_heat_kJkg"] < 3600
    assert k["free_lime_pct"] < 3.0
    assert 900 < k["T_sec_air"] < 1250


def test_more_main_fuel_heats_burning_zone(nominal):
    base = fresh(nominal)
    base.run(1800)
    hot = fresh(nominal)
    hot.cmd_drive("COAL_KILN", "sp", hot.drives["COAL_KILN"].sp + 0.8)
    hot.run(1800)
    assert hot.kpi["T_bz"] > base.kpi["T_bz"] + 15
    assert hot.kpi["O2_kiln_inlet"] < base.kpi["O2_kiln_inlet"]


def test_id_fan_raises_o2(nominal):
    sim = fresh(nominal)
    o2 = sim.kpi["O2_ph_exit"]
    sim.pids["AIC_O2"].set_mode("MAN")
    sim.cmd_drive("ID_FAN", "sp", sim.drives["ID_FAN"].sp + 5)
    sim.run(300)
    assert sim.kpi["O2_ph_exit"] > o2 + 0.3


def test_id_fan_trip_interlocks_fuel_and_feed(nominal):
    sim = fresh(nominal)
    sim.cmd_disturbance("id_fan_trip")
    sim.run(5)
    for t in ("COAL_KILN", "COAL_CAL", "KILN_FEED"):
        assert sim.drives[t].state == "FAULT", t
    assert any(e["source"] == "INTERLOCK" for e in sim.events.rows)


def test_burner_group_start_requires_purge(nominal):
    sim = fresh(nominal)
    sim.cmd_group("G_BURNER", "fast_stop")
    sim.run(10)
    assert sim.drives["COAL_KILN"].state == "STOPPED"
    sim.cmd_group("G_BURNER", "start")
    sim.run(40)
    assert sim.drives["PA_FAN"].running and sim.drives["COAL_KILN"].state == "STOPPED"  # still purging
    sim.run(60)
    assert sim.drives["COAL_KILN"].running


def test_snapshot_roundtrip(nominal):
    a = fresh(nominal)
    a.run(60)
    snap = a.snapshot()
    b = Simulator()
    b.restore(snap)
    a.run(120)
    b.run(120)
    assert a.kpi["T_bz"] == pytest.approx(b.kpi["T_bz"], abs=1e-6)


def test_pid_bumpless_and_antiwindup():
    p = PID("X", "x", kp=1.0, ti=10.0, sp=0.0, out_lo=0.0, out_hi=10.0, mode="MAN", out=4.0)
    p.set_mode("AUTO")
    assert p.scan(0.0, 1.0) == pytest.approx(4.0)
    for _ in range(1000):
        p.scan(-100.0, 1.0)  # large error -> saturate
    assert p.out == 10.0
    out = p.scan(0.0, 1.0)
    assert out <= 10.0 and p._i <= 10.0


def test_drive_ramp():
    d = Drive("F", "fan", sp=50, rate=2.0, start_time=3)
    d.cmd_start()
    for _ in range(3):
        d.scan(1.0)
    assert d.running and d.pv == pytest.approx(2.0)  # ramps from the scan it becomes RUNNING
    d.scan(1.0)
    assert d.pv == pytest.approx(4.0)
