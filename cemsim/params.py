"""Plant design basis and model parameters.

Reference plant: 3000 t/d clinker dry-process line - 5-stage single-string
preheater with in-line calciner (tertiary air duct), 4.0 m x 62 m rotary kiln on
3 piers, reciprocating grate cooler with 6 fan compartments.
All values are typical of such plants (VDZ / Peray "The Rotary Cement Kiln",
Duda "Cement Data Book"); every value is exposed here for calibration.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class KilnParams:
    length: float = 62.0  # m
    d_inner: float = 4.0  # m, inside brick
    d_shell: float = 4.4  # m
    slope_pct: float = 3.5  # % inclination
    n_cells: int = 30
    repose_deg: float = 35.0  # dynamic angle of repose (raw material)
    sullivan_F: float = 1.6  # constriction factor in Sullivan residence formula
    rho_bulk: float = 1450.0  # kg/m3 bed bulk density
    brick_rho_cp: float = 2600.0 * 1.00  # kJ/(m3 K)
    brick_thick: float = 0.20  # m
    # refractory + coating conduction resistance inner face -> shell, m2K/W
    r_wall_inlet: float = 0.085
    r_wall_bz: float = 0.125  # coated burning zone
    eps_bed: float = 0.90
    eps_wall: float = 0.85
    eps_shell: float = 0.85
    h_gas_bed: float = 12.0  # W/m2K convective gas->bed
    h_gas_wall: float = 8.0  # W/m2K convective gas->exposed wall
    h_wall_bed: float = 100.0  # W/m2K covered-wall -> bed contact (regenerative)
    k_abs_gas: float = 0.18  # 1/(atm m) grey absorption coeff of CO2+H2O
    k_abs_dust: float = 0.01  # 1/m dust/clinker fines
    k_abs_flame: float = 0.25  # 1/m additional in the luminous (burning) region
    h_shell_conv: float = 12.0  # W/m2K shell natural+forced convection
    flame_L0: float = 25.0  # m, e-folding burnout length at design
    no_factor: float = 1.0  # thermal-NO multiplier (1 = pure Zeldovich + equilibrium limit)
    drive_eff: float = 0.90
    drive_noload_kw: float = 60.0


@dataclass
class PreheaterParams:
    # node order: S1 (top) .. S4, CAL (calciner + stage-5 cyclone)
    eta: tuple = (0.95, 0.85, 0.85, 0.85, 0.88)  # cyclone separation eff.
    tau_solid: tuple = (8.0, 8.0, 8.0, 8.0, 7.0)  # s solids holdup residence
    tau_gas: tuple = (1.5, 1.5, 1.5, 1.5, 4.0)  # s gas residence (burnout)
    c_eff: tuple = (9000.0, 9000.0, 10000.0, 12000.0, 25000.0)  # kJ/K participating refractory
    ua_loss: tuple = (0.8, 0.9, 1.0, 1.1, 2.2)  # kW/K shell loss
    burn_A: float = 3.5e5  # 1/s char+volatile burnout
    burn_E: float = 120.0e3  # kJ/kmol
    no_reduction: float = 0.35  # fraction of kiln NO reduced by calciner reburn


@dataclass
class CoolerParams:
    n_cells: int = 18  # 1 m plug-flow cells along the grate
    grate_length: float = 18.0  # m
    grate_width: float = 4.0  # m
    # fan compartments: [start m, end m) under the grate
    comp_bounds: tuple = ((0, 2), (2, 5), (5, 8), (8, 11), (11, 14), (14, 18))
    stroke_m: float = 0.13
    transport_eff: float = 0.42  # clinker advance per stroke / stroke length
    rho_bulk: float = 1500.0
    air_design: tuple = (11.0, 16.0, 17.0, 17.0, 17.0, 16.0)  # kg/s per fan at 100 %
    ntu_per_m: float = 4.0  # NTU per m bed depth at design specific air
    bed_design: float = 0.60  # m
    ua_loss: float = 1.5  # kW/K cooler casing
    dp_ug_design: float = 5500.0  # Pa undergrate pressure comp.1 at design

    @property
    def n_comp(self):
        return len(self.comp_bounds)


@dataclass
class DraughtParams:
    # design point used to back-calculate resistances
    p_hood: float = -50.0  # Pa
    p_kiln_inlet: float = -350.0  # Pa
    p_ph_exit: float = -5600.0  # Pa
    m_kiln_air: float = 14.0  # kg/s secondary+primary at design
    T_kiln_mean: float = 1500.0  # K
    m_ta: float = 26.0  # kg/s
    T_ta: float = 1150.0
    tad_design: float = 60.0  # %
    m_ph: float = 64.0  # kg/s preheater exit gas
    T_ph_mean: float = 900.0
    id_speed_design: float = 85.0  # %
    id_shutoff_ratio: float = 1.35  # shut-off pressure / design pressure
    vent_speed_design: float = 75.0
    m_vent: float = 56.0
    vent_shutoff_ratio: float = 1.4
    vent_dp_design: float = 2500.0  # Pa vent fan pressure at design
    hood_leak_C: float = 0.07  # kg/s/sqrt(Pa)
    inlet_seal_C: float = 0.055
    ph_false_air_C: float = 0.035


@dataclass
class Nominal:
    """Nominal operating point (initial setpoints)."""

    kiln_feed_tph: float = 200.0
    coal_kiln_tph: float = 3.2
    coal_cal_tph: float = 11.9
    kiln_rpm: float = 3.6
    id_fan_pct: float = 85.0
    vent_fan_pct: float = 75.0
    primary_air_pct: float = 80.0
    primary_air_kgs: float = 1.9  # at 100 %
    tad_pct: float = 95.0
    cooler_fan_pct: tuple = (100.0, 100.0, 100.0, 100.0, 100.0, 100.0)
    grate_spm: float = 12.0
    T_amb: float = 298.15
    T_feed: float = 333.15  # kiln feed 60 C
    T_coal: float = 338.15  # pulverised coal 65 C
    T_primary: float = 318.15


@dataclass
class PlantParams:
    kiln: KilnParams = field(default_factory=KilnParams)
    ph: PreheaterParams = field(default_factory=PreheaterParams)
    cooler: CoolerParams = field(default_factory=CoolerParams)
    draught: DraughtParams = field(default_factory=DraughtParams)
    nominal: Nominal = field(default_factory=Nominal)


def bed_angle_from_fill(f):
    """Central angle theta (rad) of a circular segment with area fraction f.

    Solves (theta - sin theta) / (2 pi) = f by Newton (vectorised).
    """
    f = np.clip(np.asarray(f, float), 1e-5, 0.5)
    th = np.cbrt(12.0 * np.pi * f) + 0.0  # small-angle start
    for _ in range(25):
        g = (th - np.sin(th)) / (2 * np.pi) - f
        dg = (1 - np.cos(th)) / (2 * np.pi)
        th = np.clip(th - g / np.maximum(dg, 1e-9), 1e-4, np.pi)
    return th
