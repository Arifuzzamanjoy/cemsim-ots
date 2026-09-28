"""Thermodynamic property data for all species in the simulator.

Convention (used everywhere in cemsim):
  amount  : kmol          flow   : kmol/s
  enthalpy: kJ/kmol       energy : kJ          power: kW
  T       : K             p      : Pa (gauge unless stated)

Every species carries its *absolute* enthalpy  h(T) = dHf(298.15 K) + int_298^T cp dT.
Because formation enthalpies are included, every reaction heat (calcination,
clinker-phase formation, combustion) follows automatically from a plain
enthalpy balance - no hand-inserted reaction-heat terms, energy is conserved
by construction.

Sources
  gases  : NIST Chemistry WebBook Shomate coefficients (piecewise), fitted once
           at import to a smooth 5-term basis (max cp error printed by tests).
  solids : Maier-Kelley cp = a + bT + c/T^2 (Barin / Kubaschewski / Taylor,
           "Cement Chemistry" 2nd ed. Table 2.x for the clinker phases).
  dHf    : NIST-JANAF; clinker phases from Taylor (C2S -2307.5, C3S -2929.2,
           C3A -3587.8, C4AF -5082 kJ/mol).
"""

from __future__ import annotations

import numpy as np

T0 = 298.15  # reference temperature, K
R_GAS = 8.314462618  # kJ/(kmol K)  == J/(mol K)
P_ATM = 101325.0  # Pa
SIGMA = 5.670374419e-8  # W/(m2 K4)

# --------------------------------------------------------------------------
# Species definitions
# --------------------------------------------------------------------------
SOLIDS = ["CaCO3", "CaO", "SiO2", "Al2O3", "Fe2O3", "MgO", "H2O_l", "C2S", "C3S", "C3A", "C4AF"]
GASES = ["N2", "O2", "CO2", "H2O", "CO", "NO", "FUELK", "FUELC"]
# FUELK / FUELC are pseudo-species for pulverised coal carried by the gas
# (kiln main-burner coal and calciner coal).  Their "kmol" is 1 kg (M = 1).

S = {n: i for i, n in enumerate(SOLIDS)}
G = {n: i for i, n in enumerate(GASES)}
NS, NG = len(SOLIDS), len(GASES)

M_SOLID = np.array(
    [100.086, 56.077, 60.084, 101.961, 159.688, 40.304, 18.015, 172.238, 228.315, 270.192, 485.957]
)  # kg/kmol
M_GAS = np.array([28.013, 31.998, 44.009, 18.015, 28.010, 30.006, 1.0, 1.0])

# Standard formation enthalpies, kJ/kmol
HF_SOLID = 1000.0 * np.array(
    [-1206.9, -635.1, -910.7, -1675.7, -824.2, -601.6, -285.83, -2307.5, -2929.2, -3587.8, -5082.0]
)
HF_GAS = 1000.0 * np.array(
    [0.0, 0.0, -393.522, -241.826, -110.527, 90.291, 0.0, 0.0]
)  # fuel Hf set per fuel (see fuels.py)

# Maier-Kelley cp = a + b T + c / T^2   [J/(mol K)] == kJ/(kmol K)
MK_SOLID = np.array(
    [
        [104.52, 21.92e-3, -25.94e5],  # CaCO3 calcite
        [49.62, 4.52e-3, -6.95e5],  # CaO
        [56.00, 12.00e-3, -13.50e5],  # SiO2 (quartz, averaged over alpha/beta)
        [114.77, 12.80e-3, -35.44e5],  # Al2O3
        [110.00, 35.00e-3, -15.00e5],  # Fe2O3 (smoothed through magnetic peak)
        [42.59, 7.28e-3, -6.19e5],  # MgO
        [75.30, 0.0, 0.0],  # H2O liquid (free moisture)
        [151.67, 36.93e-3, -30.35e5],  # beta-C2S
        [208.60, 36.07e-3, -42.47e5],  # C3S
        [260.58, 19.17e-3, -50.12e5],  # C3A
        [374.40, 72.80e-3, 0.0],  # C4AF
    ]
)

# NIST Shomate coefficients: list of (Tmax, [A,B,C,D,E,F,H])
_SHOMATE = {
    "N2": [
        (500.0, [28.98641, 1.853978, -9.647459, 16.63537, 0.000117, -8.671914, 0.0]),
        (2000.0, [19.50583, 19.88705, -8.598535, 1.369784, 0.527601, -4.935202, 0.0]),
        (6000.0, [35.51872, 1.128728, -0.196103, 0.014662, -4.553760, -18.97091, 0.0]),
    ],
    "O2": [
        (700.0, [31.32234, -20.23531, 57.86644, -36.50624, -0.007374, -8.903471, 0.0]),
        (2000.0, [30.03235, 8.772972, -3.988133, 0.788313, -0.741599, -11.32468, 0.0]),
        (6000.0, [20.91111, 10.72071, -2.020498, 0.146449, 9.245722, 5.337651, 0.0]),
    ],
    "CO2": [
        (1200.0, [24.99735, 55.18696, -33.69137, 7.948387, -0.136638, -403.6075, -393.5224]),
        (6000.0, [58.16639, 2.720074, -0.492289, 0.038844, -6.447293, -425.9186, -393.5224]),
    ],
    "H2O": [
        (1700.0, [30.09200, 6.832514, 6.793435, -2.534480, 0.082139, -250.8810, -241.8264]),
        (6000.0, [41.96426, 8.622053, -1.499780, 0.098119, -11.15764, -272.1797, -241.8264]),
    ],
    "CO": [
        (1300.0, [25.56759, 6.096130, 4.054656, -2.671301, 0.131021, -118.0089, -110.5271]),
        (6000.0, [35.15070, 1.300095, -0.205921, 0.013550, -3.282780, -127.8375, -110.5271]),
    ],
    "NO": [
        (1200.0, [23.83491, 12.58878, -1.139011, -1.497459, 0.214194, 83.35783, 90.29114]),
        (6000.0, [35.99169, 0.957170, -0.148032, 0.009974, -3.004088, 73.10787, 90.29114]),
    ],
}

FUEL_CP = 1.30  # kJ/(kg K) pulverised coal sensible heat capacity


def _shomate_dh(name: str, T: np.ndarray) -> np.ndarray:
    """H(T) - H(298.15) in kJ/kmol from piecewise Shomate."""
    T = np.asarray(T, float)
    out = np.empty_like(T)
    lo = 0.0
    for tmax, (A, B, C, D, E, F, H) in _SHOMATE[name]:
        m = (lo < T) & (tmax >= T) if lo > 0 else (tmax >= T)
        t = T[m] / 1000.0
        out[m] = 1000.0 * (A * t + B * t**2 / 2 + C * t**3 / 3 + D * t**4 / 4 - E / t + F - H)
        lo = tmax
    return out


def _shomate_cp(name: str, T: np.ndarray) -> np.ndarray:
    T = np.asarray(T, float)
    out = np.empty_like(T)
    lo = 0.0
    for tmax, (A, B, C, D, E, _F, _H) in _SHOMATE[name]:
        m = (lo < T) & (tmax >= T) if lo > 0 else (tmax >= T)
        t = T[m] / 1000.0
        out[m] = A + B * t + C * t**2 + D * t**3 + E / t**2
        lo = tmax
    return out


# --------------------------------------------------------------------------
# Unified smooth basis:  h(T) = Hf + sum_k coef_k * phi_k(T)
#   phi = [u, u^2, u^3, u^4, (1/T - 1/T0)*1000],  u = (T - T0)/1000
# All basis functions vanish at T0 so h(T0) == Hf exactly.
# --------------------------------------------------------------------------
NBASIS = 5


def basis(T):
    """Enthalpy basis, shape (..., NBASIS)."""
    T = np.asarray(T, float)
    u = (T - T0) / 1000.0
    return np.stack([u, u * u, u**3, u**4, (1.0 / T - 1.0 / T0) * 1000.0], axis=-1)


def dbasis(T):
    """d(basis)/dT, shape (..., NBASIS)."""
    T = np.asarray(T, float)
    u = (T - T0) / 1000.0
    return np.stack([np.full_like(u, 1e-3), 2e-3 * u, 3e-3 * u * u, 4e-3 * u**3, -1000.0 / (T * T)], axis=-1)


def _solid_coefs() -> np.ndarray:
    """Exact mapping of Maier-Kelley to the basis. shape (NBASIS, NS)."""
    C = np.zeros((NBASIS, NS))
    for i, (a, b, c) in enumerate(MK_SOLID):
        # int cp = a dT + b/2 (T^2 - T0^2) - c (1/T - 1/T0)
        # T^2 - T0^2 = dT^2 + 2 T0 dT ;  dT = 1000 u
        C[0, i] = 1000.0 * (a + b * T0)
        C[1, i] = 1e6 * b / 2.0
        C[4, i] = -c / 1000.0
    return C


# Gases: exact piecewise-Shomate enthalpy tabulated at 1 K and linearly
# interpolated (cp then exact to within the table step; no fitting error).
_TAB_T0, _TAB_T1 = 200.0, 3500.0
_TAB_T = np.arange(_TAB_T0, _TAB_T1 + 1.0, 1.0)


def _gas_table() -> np.ndarray:
    tab = np.zeros((_TAB_T.size, NG))
    for name, j in G.items():
        if name.startswith("FUEL"):
            tab[:, j] = FUEL_CP * (_TAB_T - T0)  # kJ/kg (M = 1)
        else:
            tab[:, j] = _shomate_dh(name, _TAB_T)
        # anchor exactly: h(T0) - Hf == 0 on the interpolated table
        tab[:, j] -= np.interp(T0, _TAB_T, tab[:, j])
    return tab


_GAS_TAB = _gas_table()
_GAS_DTAB = np.diff(_GAS_TAB, axis=0)  # per 1 K step == cp


def _gas_lookup(T):
    T = np.clip(np.asarray(T, float), _TAB_T0, _TAB_T1 - 1e-9)
    x = T - _TAB_T0
    i = x.astype(int)
    f = (x - i)[..., None]
    return _GAS_TAB[i] + f * _GAS_DTAB[i], _GAS_DTAB[i]


COEF_SOLID = _solid_coefs()


def h_solid(T):
    """Species enthalpies kJ/kmol, shape (..., NS)."""
    return HF_SOLID + basis(T) @ COEF_SOLID


def cp_solid(T):
    return dbasis(T) @ COEF_SOLID


def h_gas(T, hf_fuel=(0.0, 0.0)):
    """Species enthalpies kJ/kmol (fuel pseudo-species: kJ/kg), shape (..., NG)."""
    h = HF_GAS + _gas_lookup(T)[0]
    h[..., G["FUELK"]] += hf_fuel[0]
    h[..., G["FUELC"]] += hf_fuel[1]
    return h


def cp_gas(T):
    return _gas_lookup(T)[1]


# --------------------------------------------------------------------------
# Clinker melt: latent heat of the liquid phase
# --------------------------------------------------------------------------
L_MELT = 420.0  # kJ per kg of liquid formed (≈100 kJ/kg clinker at 25 % liquid)
T_LIQ_START = 1553.15  # 1280 C: first liquid in industrial raw mixes (minor components)
T_LIQ_FULL = 1673.15  # 1400 C: liquid content ~ at its 1450 C value


def f_liquid(T):
    """Fraction of the potential liquid that is molten (smoothstep in T)."""
    x = np.clip((np.asarray(T, float) - T_LIQ_START) / (T_LIQ_FULL - T_LIQ_START), 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def df_liquid(T):
    T = np.asarray(T, float)
    w = T_LIQ_FULL - T_LIQ_START
    x = np.clip((T - T_LIQ_START) / w, 0.0, 1.0)
    inside = (x > 0.0) & (x < 1.0)
    return np.where(inside, 6.0 * x * (1.0 - x) / w, 0.0)


def liquid_potential_kg(n):
    """Potential liquid mass (kg) at 1450 C, Lea & Parker:  3.0 A + 2.25 F + MgO (<=2 %).

    A and F are the total Al2O3 / Fe2O3 contents whether free or bound in C3A/C4AF.
    `n` is a solid species amount vector (kmol) - may be 2-D (cells x NS).
    """
    n = np.asarray(n, float)
    A = (n[..., S["Al2O3"]] + n[..., S["C3A"]] + n[..., S["C4AF"]]) * M_SOLID[S["Al2O3"]]
    F = (n[..., S["Fe2O3"]] + n[..., S["C4AF"]]) * M_SOLID[S["Fe2O3"]]
    mass = n @ M_SOLID
    mgo = np.minimum(n[..., S["MgO"]] * M_SOLID[S["MgO"]], 0.02 * mass)
    # only meaningful once the material is (nearly) calcined
    calc = 1.0 - _safe_div(n[..., S["CaCO3"]] * M_SOLID[0], mass)
    return (3.0 * A + 2.25 * F + mgo) * np.clip(calc, 0.0, 1.0)


def _safe_div(a, b):
    return np.divide(a, b, out=np.zeros_like(np.asarray(a, float)), where=np.asarray(b) > 1e-12)


# --------------------------------------------------------------------------
# Mixture helpers
# --------------------------------------------------------------------------
def solid_enthalpy(n, T):
    """Total enthalpy (kJ) of solid holdup n (kmol) at T, incl. latent melt heat."""
    return (n * h_solid(T)).sum(-1) + L_MELT * liquid_potential_kg(n) * f_liquid(T)


def solid_heat_capacity(n, T):
    """dH/dT (kJ/K) of solid holdup n at T, incl. apparent melt heat."""
    return (n * cp_solid(T)).sum(-1) + L_MELT * liquid_potential_kg(n) * df_liquid(T)


def solve_T_solid(n, H, T_guess, extra_C=0.0, extra_H=None, tol=1e-6, maxit=30):
    """Invert H = solid_enthalpy(n,T) + extra_C*(T - T0) for T (vectorised Newton).

    `extra_C` is an additional lumped heat capacity (kJ/K) e.g. refractory lining
    sharing the node temperature.
    """
    T = np.array(T_guess, float, copy=True)
    # an (almost) empty holdup has no defined temperature: leave it at the guess
    active = (solid_heat_capacity(n, T) + extra_C) > 1e-6
    for _ in range(maxit):
        f = solid_enthalpy(n, T) + extra_C * (T - T0) - H
        d = solid_heat_capacity(n, T) + extra_C
        d = np.maximum(d, 1e-9)
        dT = np.where(active, np.clip(f / d, -300.0, 300.0), 0.0)
        T = np.clip(T - dT, 250.0, 2600.0)
        if np.all(np.abs(dT) < tol):
            break
    return T


def gas_enthalpy_flow(g, T, hf_fuel):
    """Enthalpy flow (kW) of gas stream g (kmol/s) at T."""
    return float(np.dot(g, h_gas(T, hf_fuel)))


def solve_T_gas(g, Hdot, T_guess, hf_fuel, tol=1e-6, maxit=30):
    """Invert Hdot = g·h_gas(T) for T (scalar)."""
    T = float(T_guess)
    for _ in range(maxit):
        f = float(np.dot(g, h_gas(T, hf_fuel))) - Hdot
        d = max(float(np.dot(g, cp_gas(T))), 1e-12)
        dT = max(-400.0, min(400.0, f / d))
        T = min(max(T - dT, 250.0), 3200.0)
        if abs(dT) < tol:
            break
    return T


def gas_mass(g):
    return float(np.dot(g, M_GAS))


def mole_fractions(g, wet=True):
    """Mole fractions of the real gas species (fuel pseudo-species excluded)."""
    real = g[:6].copy()
    if not wet:
        real[G["H2O"]] = 0.0
    tot = real.sum()
    return real / tot if tot > 0 else real


def gas_density(g, T, p_abs=P_ATM):
    """kg/m3 of the real gas (ideal gas)."""
    real = g[:6]
    n = real.sum()
    if n <= 0:
        return 1.2
    Mmix = float(np.dot(real, M_GAS[:6])) / n
    return p_abs * Mmix / (R_GAS * 1000.0 * T)


def gas_volume_flow(g, T, p_abs=P_ATM):
    """Actual m3/s of the real gas."""
    return float(g[:6].sum()) * R_GAS * 1000.0 * T / p_abs


def air(kg_per_s: float) -> np.ndarray:
    """Dry air stream (kmol/s) for a given mass flow; Ar lumped into N2."""
    y = np.zeros(NG)
    y[G["O2"]] = 0.2095
    y[G["N2"]] = 0.7905
    M = float(np.dot(y, M_GAS))
    return y * (kg_per_s / M)
