"""Operating sequences driven only through operator commands (as from the HMI)."""

import sys

sys.path.insert(0, ".")
from cemsim.sim import Simulator

K = (
    "T_bz",
    "free_lime_pct",
    "T_kiln_inlet",
    "O2_kiln_inlet",
    "CO_kiln_inlet_ppm",
    "T_calciner",
    "T_ph_exit",
    "p_hood",
    "clinker_tph_avg",
    "spec_heat_kJkg",
    "kiln_feed_tph",
)


def show(sim, what):
    k = sim.kpi
    print(f"{sim.plant.t / 60:7.1f} min {what:32s} " + " ".join(f"{x}={k[x]:.1f}" for x in K), flush=True)


def startup():
    """Hot standby -> full production, operator procedure (no trainer tricks)."""
    sim = Simulator()
    sim.load_file("hot_standby")
    show(sim, "hot standby")
    # 1. raise the fire, close TAD so hot kiln gas heats the calciner
    sim.cmd_drive("TAD", "sp", 10)
    sim.cmd_drive("COAL_KILN", "sp", 2.8)
    sim.cmd_drive("ID_FAN", "sp", 55)
    sim.cmd_drive("KILN_DRIVE", "sp", 2.0)
    ok, why = sim.cmd_group("G_CALC", "start")
    print("calciner start (too early):", ok, why)
    for _ in range(40):
        sim.run(60)
        if sim.kpi["T_calciner"] > 780:
            break
    show(sim, "calciner heated")
    # 2. light calciner at minimum, manual
    sim.cmd_pid("TIC_CAL", "mode", "MAN")
    sim.cmd_drive("COAL_CAL", "sp", 3.0)
    ok, why = sim.cmd_group("G_CALC", "start")
    print("calciner start:", ok, why)
    sim.run(120)
    # 3. feed on at 50 %, calciner to AUTO
    sim.cmd_drive("KILN_FEED", "sp", 100)
    ok, why = sim.cmd_group("G_FEED", "start")
    print("feed start:", ok, why)
    sim.cmd_pid("TIC_CAL", "sp", 870)
    sim.cmd_pid("TIC_CAL", "mode", "AUTO")
    for i in range(1, 7):
        sim.cmd_drive(f"CF{i}", "sp", 75)
    sim.cmd_pid("PIC_UG", "mode", "AUTO")
    for m in range(4):
        sim.cmd_drive("TAD", "sp", 25 + 15 * m)
        sim.run(600)
        show(sim, "feed 100 t/h, TAD opening")
    # 4. competent-operator ramp: air before fuel, feed only on a hot kiln,
    #    tripped equipment acknowledged and restarted
    feed, i_bz, t_ok = 100.0, sim.drives["COAL_KILN"].sp, 0
    sim.pids["AIC_O2"].sp = 2.8
    sim.cmd_pid("AIC_O2", "mode", "AUTO")
    for minute in range(12 * 60):
        k, D = sim.kpi, sim.drives
        # air: kiln-inlet O2 to 3 % with the tertiary-air damper
        D["TAD"].sp = min(max(D["TAD"].sp + 0.3 * max(min(k["O2_kiln_inlet"] - 3.0, 3), -3), 10.0), 95.0)
        # fuel: BZ PI, but never add fuel while the kiln is short of air
        e = 1450.0 - k["T_bz"]
        if not (e > 0 and k["O2_kiln_inlet"] < 2.0):
            i_bz = min(max(i_bz + 0.0003 * max(min(e, 150), -150), 1.0), 9.5)
        D["COAL_KILN"].sp = min(max(i_bz + 0.004 * max(min(e, 150), -150), 1.0), 9.5)
        # restart anything that tripped
        for g, drv in (("G_CALC", "COAL_CAL"), ("G_BURNER", "COAL_KILN"), ("G_FEED", "KILN_FEED")):
            if D[drv].state == "FAULT":
                sim.cmd_group(g, "ack")
                sim.cmd_group(g, "start")
        # feed ramp on a hot, well-aerated kiln
        if k["T_bz"] > 1420 and k["free_lime_pct"] < 3.0 and k["O2_kiln_inlet"] > 2.0 and feed < 200:
            t_ok += 1
            if t_ok >= 10:
                feed = min(200.0, feed + 10.0)
                t_ok = 0
                D["KILN_FEED"].sp = feed
                D["KILN_DRIVE"].sp = 1.8 + 1.8 * feed / 200.0
                for i in range(1, 7):
                    D[f"CF{i}"].sp = min(100.0, 60 + 40 * feed / 200.0)
        else:
            t_ok = 0
        sim.run(60)
        if minute % 60 == 59:
            show(sim, f"feed {feed:.0f}, coalK {D['COAL_KILN'].pv:.2f} TAD {D['TAD'].pv:.0f}")
    trips = [e for e in sim.events.rows if e["source"] == "INTERLOCK"]
    print("interlock trips:", [(round(e["t"] / 60), e["tag"], e["comment"]) for e in trips])
    k = sim.kpi
    assert not trips, f"start-up tripped: {trips}"
    assert k["kiln_feed_tph"] >= 199, f"full feed not reached: {k['kiln_feed_tph']:.0f} t/h"
    assert 1400 <= k["T_bz"] <= 1500, f"burning zone {k['T_bz']:.0f} C"
    assert k["clinker_tph_avg"] > 115, f"clinker {k['clinker_tph_avg']:.1f} t/h"
    assert k["CO_kiln_inlet_ppm"] < 1000, f"CO {k['CO_kiln_inlet_ppm']:.0f} ppm"
    print("start-up: PASSED")
    return sim


def shutdown():
    sim = Simulator()
    sim.load_file("nominal")
    show(sim, "nominal")
    for g in ("G_FEED", "G_CALC", "G_BURNER"):
        sim.cmd_group(g, "stop")
        sim.run(60)
        show(sim, f"stop {g}")
    sim.run(1800)
    show(sim, "all fuel off 30 min")
    sim.cmd_group("G_COOLER", "stop")
    sim.cmd_group("G_EXH", "stop")
    sim.run(600)
    show(sim, "cooler+ID off")
    sim.cmd_group("G_KILN", "stop")
    sim.run(3600)
    show(sim, "kiln stopped 1 h")
    # restart burner without ID fan -> must be refused
    ok, why = sim.cmd_group("G_BURNER", "start")
    print("burner start w/o ID fan:", ok, why)
    import math

    assert not ok and "ID fan" in why, "burner start without ID fan must be refused"
    assert all(math.isfinite(float(v)) for v in sim.kpi.values() if isinstance(v, float)), "non-finite KPI"
    assert sim.kpi["kiln_holdup_t"] < 1.0, f"kiln not empty: {sim.kpi['kiln_holdup_t']:.1f} t"
    print("shutdown: PASSED")
    return sim


if __name__ == "__main__":
    {"startup": startup, "shutdown": shutdown}[sys.argv[1]]()
