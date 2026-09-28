"""Run the plant at nominal actuators and print key values periodically."""

import sys
import time

sys.path.insert(0, ".")
from cemsim.plant import Plant, nominal_actuators

hours = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
dt = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
p = Plant()
act = nominal_actuators()
keys = [
    "T_bz",
    "T_kiln_inlet",
    "O2_kiln_inlet",
    "CO_kiln_inlet_ppm",
    "NO_kiln_inlet_ppm",
    "T_calciner",
    "hot_meal_doc_pct",
    "T_ph_exit",
    "O2_ph_exit",
    "p_ph_exit",
    "p_hood",
    "T_sec_air",
    "T_ter_air",
    "T_clinker_out",
    "clinker_tph",
    "spec_heat_kJkg",
    "free_lime_pct",
    "C3S_pct",
    "kiln_drive_kW",
    "kiln_fill_pct",
    "m_sec",
    "m_ta",
    "m_vent",
    "shell_max_C",
    "flame_len_m",
]
t0 = time.time()
steps = int(hours * 3600 / dt)
for i in range(steps):
    k = p.step(dt, act)
    if (i + 1) % int(1800 / dt) == 0 or i == steps - 1:
        print(f"--- t={p.t / 3600:.2f} h  wall={time.time() - t0:.1f}s")
        print("  ".join(f"{kk}={k[kk]:.1f}" for kk in keys))
from pathlib import Path

from cemsim import fileset

fileset.dump(p.snapshot(), Path("scratch_state.json"))
