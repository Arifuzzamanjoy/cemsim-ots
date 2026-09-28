"""Settle the nominal operating point with closed loops + a slow 'expert operator'.

Loops in AUTO: hood pressure (vent fan), calciner temperature (calciner coal),
undergrate pressure (grate), preheater exit O2 (ID fan).  The script acts as an
experienced operator on a slow time scale: main-burner coal to hold the
burning-zone temperature,
tertiary-air damper to hold kiln-inlet O2.  Saves data/snapshots/nominal.json.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
import json

from cemsim.sim import Simulator

hours = float(sys.argv[1]) if len(sys.argv) > 1 else 6
OUT = sys.argv[3] if len(sys.argv) > 3 else "nominal"
OVR = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
from cemsim import chemistry

for k_, v_ in OVR.get("kin", {}).items():
    chemistry.KIN[k_] = tuple(v_)
sim = Simulator()
for k_, v_ in OVR.get("kiln", {}).items():
    setattr(sim.plant.kiln.p, k_, v_)
try:
    from cemsim import fileset

    sim.plant.restore(fileset.load(Path("scratch_state.json")))
except (FileNotFoundError, OSError):
    pass
sim.set_all_running()
sim.pids["AIC_O2"].set_mode("AUTO")
BZ_SP, O2K_SP = 1450.0, 3.0
start = sys.argv[2] if len(sys.argv) > 2 else None
if start:
    sim.load_file(start)
    from cemsim.chemistry import RawMix

    sim.plant.rawmix = RawMix()  # current design raw mix
    sim.dist["rawmix_lsf"] = sim.plant.rawmix.LSF
    sim.set_all_running()
    sim.pids["AIC_O2"].set_mode("AUTO")
i_bz = sim.drives["COAL_KILN"].sp
# Phase 1 (first 60 %): brick heat capacity / 10 so the kiln wall reaches its
# steady state quickly (steady state is independent of heat capacities).
# Phase 2: real capacity restored, plant settles again.
C_w_real = sim.plant.kiln.C_w
sim.plant.kiln.C_w = C_w_real / 10.0
t0 = time.time()
N = int(hours * 3600)
for i in range(N):
    if i == int(0.6 * N):
        # keep wall energy consistent: the wall state is its temperature
        sim.plant.kiln.C_w = C_w_real
        print("-- real brick capacity restored", flush=True)
    sim.step()
    k = sim.kpi
    if i % 60 == 0 and i > 0:  # operator acts every minute
        d = sim.drives
        e_bz = BZ_SP - k["T_bz"]  # PI on burning zone (slow: kiln ~30 min)
        i_bz += 0.0002 * max(min(e_bz, 150), -150)
        d["COAL_KILN"].sp = min(max(i_bz + 0.004 * max(min(e_bz, 150), -150), 2.0), 9.5)
        i_bz = min(max(i_bz, 2.0), 9.5)
        e_o2 = k["O2_kiln_inlet"] - O2K_SP  # too much kiln air -> open TAD
        d["TAD"].sp = min(max(d["TAD"].sp + 0.15 * max(min(e_o2, 3), -3), 20.0), 100.0)
    if i % 1800 == 0:
        print(
            f"t={sim.plant.t / 3600:5.2f}h wall={time.time() - t0:5.0f}s BZ={k['T_bz']:.0f} FL={k['free_lime_pct']:.2f} "
            f"C3S={k['C3S_pct']:.1f} O2k={k['O2_kiln_inlet']:.2f} COk={k['CO_kiln_inlet_ppm']:.0f} NOk={k['NO_kiln_inlet_ppm']:.0f} "
            f"Tki={k['T_kiln_inlet']:.0f} Tcal={k['T_calciner']:.0f} DoC={k['hot_meal_doc_pct']:.1f} "
            f"Tex={k['T_ph_exit']:.0f} O2x={k['O2_ph_exit']:.2f} px={k['p_ph_exit']:.1f} ph={k['p_hood']:.2f} "
            f"Tsec={k['T_sec_air']:.0f} Tta={k['T_ter_air']:.0f} Tclk={k['T_clinker_out']:.0f} "
            f"cl={k['clinker_tph_avg']:.1f} q={k['spec_heat_kJkg']:.0f} coalK={sim.drives['COAL_KILN'].sp:.2f} "
            f"coalC={sim.drives['COAL_CAL'].sp:.2f} ID={sim.drives['ID_FAN'].pv:.1f} TAD={sim.drives['TAD'].pv:.1f} "
            f"vent={sim.drives['VENT_FAN'].pv:.1f} grate={sim.drives['GRATE'].pv:.1f} kW={k['kiln_drive_kW']:.0f} shell={k['shell_max_C']:.0f}",
            flush=True,
        )
sim.pids["AIC_O2"].set_mode("MAN")
for g in sim.groups.values():
    g.state = "RUNNING"
sim.plant.acc = dict.fromkeys(sim.plant.acc, 0.0)
p = sim.save_file(OUT, "Nominal operation 3000 t/d (calibrated steady state)")
print("saved", p)
print({k: round(v) for k, v in sim.heat_balance().items()})
