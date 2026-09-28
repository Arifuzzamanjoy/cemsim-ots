# CemSim process model — equations and assumptions

This document is the reference for every equation in the simulator. Code references are
`module:function`. Units throughout: kmol, kmol/s, kJ, kW, K, Pa (gauge).

## 1. Thermodynamics (`cemsim/thermo.py`)

Every species carries its **absolute enthalpy**

    h_i(T) = ΔHf,i(298.15 K) + ∫_298^T cp_i dT

Because formation enthalpies are included, all reaction heats (calcination, clinker-phase
formation, combustion) come out of a plain enthalpy balance. No reaction-heat term is
inserted by hand, so energy is conserved by construction. This is verified in
`tests/test_plant.py::test_mass_and_energy_conservation`: closure is better than 0.3 % of
fuel heat over 15 min, and mass closure is better than 10⁻⁵.

| Species | cp source | ΔHf (kJ/mol) |
|---|---|---|
| N2, O2, CO2, H2O(g), CO, NO | NIST Shomate (piecewise), tabulated at 1 K | NIST-JANAF |
| CaCO3, CaO, SiO2, Al2O3, Fe2O3, MgO | Maier–Kelley a + bT + c/T² (Barin, Kubaschewski) | NIST-JANAF |
| C2S, C3S, C3A, C4AF | Maier–Kelley (Taylor, *Cement Chemistry*) | −2307.5, −2929.2, −3587.8, −5082 |
| Coal (pseudo-species, per kg) | 1.3 kJ/(kg K) | chosen so combustion releases exactly the LHV (§3) |

**Clinker melt.** The latent heat of fusion is L = 420 kJ/kg of liquid. The potential liquid
follows Lea & Parker, L = 3.0 A + 2.25 F + MgO (≤ 2 %). The molten fraction is a
smoothstep between 1280 °C and 1400 °C. It enters the solid enthalpy, so the heat is
absorbed in the burning zone and released in the cooler.

Checks (`tests/test_physics.py`):

| Quantity | Model | Reference |
|---|---|---|
| Calcination ΔH at 25 °C | 178.3 kJ/mol | 178.3 kJ/mol |
| Calcination ΔH at 900 °C | 166.7 kJ/mol | 166.7 kJ/mol |
| Theoretical heat of clinker formation | 1700 kJ/kg | 1650–1800 kJ/kg |
| Coal adiabatic flame temperature (λ = 1.1, 25 °C air) | ≈ 2000 °C | ≈ 2000 °C |

## 2. Chemistry (`cemsim/chemistry.py`)

| | Reaction | ΔH298 (kJ/mol) | Rate law |
|---|---|---|---|
| R1 | CaCO3 → CaO + CO2 | +178.3 | k1 n_CaCO3 (1 − pCO2/peq(T)) |
| R2 | 2CaO + SiO2 → C2S | −126.6 | k2 n_SiO2 f_CaO |
| R3 | C2S + CaO → C3S | +13.4 | k3 n_C2S f_CaO f_liq(T) |
| R4 | 3CaO + Al2O3 → C3A | −6.8 | k4 max(0, n_A − n_F) f_CaO |
| R5 | 4CaO + Al2O3 + Fe2O3 → C4AF | −41.7 | k5 min(n_A, n_F) f_CaO |

- **Rate constants and driving terms**
  - k = A·exp(−E/RT), with E taken from Mastorakos et al. (1999). Pre-exponentials are
    recalibrated for the first-order holdup formulation.
  - f_CaO = n_CaO / (n_CaO + 0.02 n_tot) is the free-lime availability.
  - peq(T) = 4.137·10⁷·exp(−20474/T) atm (Baker 1962). It gives 894 °C at 1 atm CO2.
- **Bed atmosphere:** in the kiln bed, pCO2 = 1 atm (CO2 blanket). In the preheater and
  calciner, pCO2 is the local gas value.
- **Integration**
  - Each step uses the exact exponential integrator (unconditionally stable), and every
    extent is clipped to the available reactants.
  - The calcination extent is additionally capped by the sensible heat above T_eq(pCO2).
    This stops a large explicit step from undercooling the material, which is impossible
    physically.
- **Bogue consistency:** ferrite forms first and C3A uses only the Al2O3 left over. At full
  conversion the phase composition therefore equals Bogue exactly (tested).
- **Raw mix:** the target LSF/SM/AM give clinker oxides, which give raw meal
  (CaO as CaCO3, plus free moisture). The kiln feed LSF is 1.00, because coal ash absorbed
  in the burning zone lowers the clinker LSF to about 0.95.

## 3. Fuel and combustion (`cemsim/fuels.py`)

The coal is defined by its as-fired ultimate analysis (C, H, O, N, moisture, ash) and its
LHV. Its pseudo-species formation enthalpy is

    Hf_fuel = Σ ν_i ΔHf_i(products: CO2, H2O(g), N2, ash oxides) + LHV

so burning 1 kg at 25 °C releases exactly the LHV (tested to 10⁻⁶). Partial oxidation to CO
automatically releases 283 kJ/mol less.

- **CO/CO2 split:** logistic in the local O2 fraction. It gives about 0.1 % CO at 3 % O2
  and 50 % at 0.6 % O2.
- **CO burnout:** limited by mixing and temperature.
- **Ash:** the ash oxides of burnt coal join the solids where the coal burns (kiln bed or
  calciner meal), which lowers clinker LSF.

## 4. Rotary kiln (`cemsim/units/kiln.py`)

The kiln is split into 30 axial cells (62 m × 4.0 m inside brick). The bed and wall are
dynamic; the gas is quasi-steady (gas residence is about 1 s, versus minutes to hours for
bed and wall).

- **Bed.** Species holdup n_j and absolute enthalpy U_j. Transport uses the Sullivan
  residence time t = 1.77 L √β F / (S D N) [min], applied as tanks in series with exact
  exponential outflow. The kiln speed (rpm) therefore sets residence time, fill degree and
  bed geometry.
- **Geometry.** The segment angle θ comes from the fill fraction, (θ − sin θ)/2π = f. The
  bed chord, exposed wall arc and covered wall arc follow from θ.
- **Gas.** The gas is marched from the burner to the inlet, solving the enthalpy balance of
  each cell for T_g by Newton:

      H_in + CO2(bed, at T_b) − H_out(T_g) − H_ash(T_g) − Q_gb(T_g) − Q_gw(T_g) = 0

- **Heat transfer** (grey gas, Hottel form; ε_eff = ε_g ε_s / (1 − (1−ε_g)(1−ε_s))):

  | Path | Equation |
  |---|---|
  | Gas → bed | Q_gb = A_b [h_gb ΔT + σ ε_gb (T_g⁴ − T_b⁴)] |
  | Gas → exposed wall | Q_gw = A_we [h_gw ΔT + σ ε_gw (T_g⁴ − T_w⁴)] |
  | Exposed wall → bed | Q_wb = A_b σ ε_wb (1 − ε_g)(T_w⁴ − T_b⁴) |
  | Covered wall → bed (regenerative contact) | Q_cwb = A_wc h_cwb (T_w − T_b) |
  | Wall → shell → ambient | Q_sh = A_sh (T_w − T_sh)/R_wall, with shell convection + radiation solved for T_sh |

  - ε_g = 1 − exp(−κ L_m), with L_m = 0.9 D.
  - κ = k_g (pCO2 + pH2O) + k_dust + k_flame · luminosity.
- **Refractory.** Lumped brick: C_w dT_w/dt = Q_gw − Q_wb − Q_cwb − Q_sh. The shell scanner
  shows T_sh.
- **Flame.** Coal burnout follows a Weibull(k = 2) profile in distance from the burner tip,
  with scale L_f. Each cell burns the conditional fraction of the remaining coal, limited by
  O2. L_f ∝ (primary-air momentum)^−½ (T_sec,ref/T_sec)^½ × the trainer's burner-wear factor.
- **Thermal NO.** Zeldovich rate 2 k1 [O][N2] with equilibrium O atoms. The reverse reaction
  enters as an approach to equilibrium for N2 + O2 ⇌ 2NO, Kp = 21.9 exp(−21650/T).
- **Kiln drive.** Torque = Σ m g r_cg sin β, where β is the dynamic repose angle and rises
  with melt content (sticky clinker). Power = τ ω / η + no-load power. This is why operators
  read kiln amps as a burning-zone indicator.

## 5. Preheater and calciner (`cemsim/units/preheater.py`)

There are 5 nodes: S1..S4 and CAL (calciner + stage-5 cyclone). Each node is a riser plus
cyclone, where gas from below meets meal from the cyclone above. Gas and meal leave at a
common T_i: riser exchange reaches equilibrium in about 0.1 s.

    dn_i/dt = sep_{i−1} + carry_{i+1} − n_i/τ_i + R_i
    dU_i/dt = H_in − H_out(T_i) − UA_i (T_i − T_amb),     U_i = Σ n h(T_i) + C_i (T_i − T0)

- **Cyclones.** Separation efficiency η_i. The (1 − η) carry-over recirculates upward
  (internal dust cycle); S1 carry-over is lost to the filter as dust.
- **Refractory.** C_i is the effective participating capacity, set so a node responds on the
  30–60 s scale.
- **Calciner combustion.** Burnout β = 1 − exp(−k(T) τ_g), limited by O2. Unburnt coal
  continues upward.
- **Blockage.** A blocked cyclone holds its separated meal in the cone. On release the cone
  empties in about 20 s, a flush of cold meal into the kiln.

## 6. Grate cooler (`cemsim/units/cooler.py`)

The 18 m grate is split into 1 m plug-flow cells, with 6 fan compartments mapped onto them.

- **Transport:** v = spm · stroke · η_t / 60. Bed height H_c = m_c / (ρ W Δx).
- **Cross-flow exchange:** ε = 1 − exp(−NTU), with NTU = ntu_per_m · H · (m/m0)^−0.4
  (volumetric h_v ∝ G^0.6), and T_air = T_amb + ε (T_c − T_amb).
- **Undergrate pressure:** Δp ∝ H m^1.8 (Ergun, turbulent term).
- **Air pooling:** front to back. Secondary air takes the hottest, then tertiary air, and the
  vent takes the rest. Hood in-leak (negative hood pressure) dilutes the secondary air;
  positive hood pressure puffs recuperation air out.

## 7. Draught (`cemsim/units/draught.py`)

Branch resistances follow Δp = R m² T/1000 (ρ ∝ 1/T). The tertiary air damper characteristic
is φ = (pos/100)^1.5 + 0.02. The ID fan follows the fan laws, −p_x = (ρ/ρ_d)(a n² − b Q²).
The vent fan has a closed-form operating point. Hood leak, kiln seal and preheater false air
all scale as C √|p|.

The unknowns (p_hood, p_kiln inlet) are solved each step by damped 2-D Newton from two
equations: the hood mass balance and the ID fan operating point. All resistances are
back-calculated from the design point in `params.py` (tested).

Coupling: gas generated inside the line (CO2 + H2O + combustion products) is fed back with a
10 s first-order lag. This breaks the algebraic loop and represents the duct volumes.

## 8. Control layer (`cemsim/control.py`, `cemsim/sim.py`)

- **Drives:** STOPPED → STARTING → RUNNING → STOPPING state machine, FAULT on trip (reset
  needed), with rate-limited speed/position.
- **Groups:** ordered start sequences with delays and permissives. For example, the main
  burner runs primary air fan → 60 s purge (ID fan must run) → coal dosing.
- **Interlocks:**
  - ID fan off → coal and feed tripped
  - primary air fan off → kiln coal tripped
  - kiln drive or cooler off → feed tripped
  - CO > 0.5 % at preheater exit for 5 s → all fuel tripped (filter protection)
- **PID loops:** positional PI with anti-windup and bumpless MAN/AUTO transfer.

  | Loop | PV → output |
  |---|---|
  | Hood pressure | → vent fan |
  | Calciner outlet T | → calciner coal |
  | Undergrate pressure | → grate speed |
  | Preheater exit O2 | → ID fan (MAN by default) |

- **Alarms:** HH/H/L/LL limits with deadband and delay, following ISA-18.2 states.

## 9. Trainer (`cemsim/trainer.py`)

- **Disturbances:**
  - coal LHV, ash content and dosing fluctuation
  - flame shape (burner wear)
  - kiln feed LSF, SM and moisture
  - feed fluctuation
  - kiln seal and preheater false air
  - kiln ring
  - ambient temperature
  - cyclone 4 blockage and release
  - trips of cooler fan 1, ID fan, kiln drive and primary air fan
- **Event log:** every operator, trainer and interlock action.
- **Lab:** hourly clinker spot samples with a 20 min analysis delay.
- **Scoring:** weighted criteria (band / average / max / min / IAE) against references with
  tolerances.

## 10. Numerical scheme

- **Time step:** fixed dt = 1 s. The fastest dynamic element is about 30 s (calciner
  refractory node), and explicit coupling between units uses a one-step lag.
- **Stability:** transport and reactions use exact exponential updates. Temperatures are
  recovered from enthalpy by vectorised Newton.
- **Speed:** about 12 ms per step on one core, so up to about 50× real time.

## 11. Calibrated operating point (`data/snapshots/nominal.json`)

These values are held by `scripts/calibrate.py` (closed loops plus a slow "expert operator"
on burning-zone T and kiln-inlet O2) and converged over 30+ simulated hours.

| Variable | Model | Typical 3000 t/d plant |
|---|---|---|
| Clinker production | 124.7 t/h | 125 t/h |
| Specific heat consumption | 3083 kJ/kg | 3000–3250 kJ/kg |
| Burning-zone material T | 1450 °C | 1420–1480 °C |
| Free lime / C3S | 1.8 % / 52 % | 0.8–2 % / 50–65 % |
| Kiln inlet gas T / O2 / CO / NO | 937 °C / 2.9 % / 19 ppm / 654 ppm | 1000–1150 °C / 2–4 % / < 500 ppm / 500–1500 ppm |
| Calciner outlet T / hot meal DoC | 875 °C / 94.6 % | 870–890 °C / 90–95 % |
| Preheater exit T / O2 / p | 302 °C / 2.8 % / −53 mbar | 290–340 °C / 2–3 % / −50…−65 mbar |
| Secondary / tertiary air | 1081 / 745 °C | 1000–1150 / 800–950 °C |
| Clinker after cooler | 144 °C | 100–150 °C |
| Cooling air | 2.1 Nm³/kg | 1.9–2.3 Nm³/kg |
| Heat balance closure | < 0.4 % of fuel | — |

**Known deviations**

- **Main-burner share.** The main burner carries about 20 % of fuel, against 35–45 % in
  typical plants. As a result the kiln inlet gas temperature (≈ 940 °C) and tertiary air
  temperature are on the low side. The model's kiln exchanges heat more efficiently than a
  real one. Missing mechanisms that would lower that efficiency:
  - kiln dust recirculation
  - the bed-surface "active layer" temperature excess
  - coating
- **Preheater-exit NO** is low (no fuel-NO from calciner coal).

Upset responses are checked by `scripts/scenario_check.py`:

| Upset | Response |
|---|---|
| Lower coal LHV | BZ T falls, free lime and NO fall/rise accordingly |
| Cyclone blockage then release | calciner overheats while blocked; release flushes cold meal into the kiln (BZ collapse, CO) |
| ID fan trip | interlock trips fuel and feed |
| Higher kiln speed | drive power spikes, residence time shortens, free lime rises |

## 12. Known limitations (MVP)

- No SO2/alkali/chloride cycles and no bypass. NO from fuel-N is not modelled; only thermal
  NO and calciner reburn reduction.
- No coating build-up or ring dynamics, apart from the constant `kiln_ring` disturbance.
- Raw mill, coal mill and cement mill are not modelled. Dust from S1 is lost rather than
  returned to the feed.
- Gas and meal in a cyclone stage leave at one temperature. Real stages show a 10–30 K
  gas–meal difference.
- The flame is a burnout profile, not a CFD jet: ignition distance, swirl and entrainment
  are lumped into L_f.
- Parameters are calibrated to *typical* 3000 t/d plant values, not to a specific plant.
  Calibrating to a real plant needs its data (see `scripts/calibrate.py`).
