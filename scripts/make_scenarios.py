"""Derive training initial conditions (filesets) from the calibrated nominal state."""

import sys

sys.path.insert(0, ".")
from cemsim.sim import Simulator

sim = Simulator()
sim.load_file("nominal")
sim.run(1800)  # settle trace species (NO) etc.
sim.events.rows.clear()
sim.save_file("nominal", "Nominal operation 3000 t/d (calibrated steady state)")
k = sim.kpi
print(
    "nominal:",
    {
        x: round(float(k[x]), 1)
        for x in (
            "T_bz",
            "free_lime_pct",
            "O2_kiln_inlet",
            "NO_kiln_inlet_ppm",
            "T_kiln_inlet",
            "spec_heat_kJkg",
            "clinker_tph_avg",
        )
    },
    flush=True,
)

# hot standby: kiln turning slowly, main burner on low fire, no feed, calciner off.
# The feed-stop transient drives the preheater exit above the 450 C trip (stored
# refractory heat, no meal), so that trip is bypassed during the transition only.
sim.trip_bypass = {"tex"}
t_hold = sim.plant.t
sim.log("TRAINER", "BYPASS", "tex", "scenario generation")
sim.cmd_group("G_FEED", "stop", "TRAINER")
sim.cmd_group("G_CALC", "stop", "TRAINER")
sim.cmd_pid("TIC_CAL", "mode", "MAN", "TRAINER")
sim.cmd_drive("COAL_KILN", "sp", 1.2, "TRAINER")
sim.cmd_drive("KILN_DRIVE", "sp", 1.0, "TRAINER")
sim.cmd_pid("PIC_UG", "mode", "MAN", "TRAINER")
sim.cmd_drive("GRATE", "sp", 4.0, "TRAINER")
sim.pids["AIC_O2"].set_mode("MAN")
sim.cmd_drive("ID_FAN", "sp", 22.0, "TRAINER")
for i in range(1, 7):
    sim.cmd_drive(f"CF{i}", "sp", 45.0, "TRAINER")
sim.cmd_drive("TAD", "sp", 20.0, "TRAINER")
sim.run(4 * 3600)  # let the post-feed-stop transient pass
for _ in range(8 * 6):  # then cool down until exit < 480 C (max 12 h)
    if sim.kpi["T_ph_exit"] < 480:
        break
    sim.run(600)
print(f"hot standby reached after {(sim.plant.t - t_hold) / 3600:.1f} h")
sim.trip_bypass = set()
sim.run(600)  # all interlocks live again
trips = [e for e in sim.events.rows if e["source"] == "INTERLOCK"]
assert not trips, f"hot standby generation tripped: {trips}"
assert sim.drives["COAL_KILN"].running, "main burner not running in hot standby"
assert sim.kpi["T_ph_exit"] < 500, f"hot standby PH exit too hot: {sim.kpi['T_ph_exit']:.0f} C"
sim.events.rows.clear()
sim.alarms.active.clear()
sim.save_file("hot_standby", "Kiln hot on low fire, no feed, calciner off - practise feed start-up")
k = sim.kpi
print(
    "hot standby:",
    {x: round(k[x], 1) for x in ("T_bz", "T_kiln_inlet", "O2_kiln_inlet", "T_calciner", "T_ph_exit", "p_hood")},
)
