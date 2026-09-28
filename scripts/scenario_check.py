"""Run classic operator-training upsets from nominal and print the response."""

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
    "hot_meal_doc_pct",
    "T_ph_exit",
    "kiln_drive_kW",
    "NO_kiln_inlet_ppm",
    "coal_cal_tph",
)


def row(sim, tag):
    k = sim.kpi
    print(
        f"{tag:>26s} t={sim.plant.t / 60 - t0:6.1f}min "
        + " ".join(f"{x.split('_')[0]}{'_' + x.split('_')[1] if len(x.split('_')) > 1 else ''}={k[x]:.1f}" for x in K),
        flush=True,
    )


case = sys.argv[1]
sim = Simulator()
sim.load_file("nominal")
t0 = sim.plant.t / 60
row(sim, "baseline")
if case == "lhv":
    sim.cmd_disturbance("coal_lhv_kiln", 21000)
    for m in (10, 20, 30, 45, 60, 90):
        sim.run(m * 60 - (sim.plant.t / 60 - t0) * 60)
        row(sim, "main coal LHV 25.6->21 MJ")
elif case == "cyclone":
    sim.cmd_disturbance("cyclone4_block")
    for m in (2, 5, 10):
        sim.run(m * 60 - (sim.plant.t / 60 - t0) * 60)
        row(sim, f"S4 blocked (cone {sim.kpi['cone_buildup_t']:.1f} t)")
    sim.cmd_disturbance("cyclone4_release")
    for m in (11, 13, 16, 20, 30, 45):
        sim.run(m * 60 - (sim.plant.t / 60 - t0) * 60)
        row(sim, "S4 released (flush)")
elif case == "idtrip":
    sim.cmd_disturbance("id_fan_trip")
    for m in (0.2, 1, 3, 10):
        sim.run(m * 60 - (sim.plant.t / 60 - t0) * 60)
        row(sim, "ID fan trip")
    print("drives:", {t: d.state for t, d in sim.drives.items() if d.state != "RUNNING"})
    print("events:", [(e["source"], e["tag"], e["value"]) for e in sim.events.rows[-6:]])
elif case == "rpm":
    sim.cmd_drive("KILN_DRIVE", "sp", 4.2)
    for m in (5, 15, 30, 60):
        sim.run(m * 60 - (sim.plant.t / 60 - t0) * 60)
        row(sim, "kiln speed 3.6->4.2 rpm")
