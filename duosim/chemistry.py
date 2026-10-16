"""Raw-mix design, clinker quality indices and solid-state reaction kinetics.

Reactions (molar extents, kmol):
  R1  CaCO3            -> CaO + CO2(g)        dH298 = +178.3 kJ/mol
  R2  2 CaO + SiO2     -> C2S                 dH298 = -126.6
  R3  C2S + CaO        -> C3S   (liquid phase) dH298 =  +13.4
  R4  3 CaO + Al2O3    -> C3A                 dH298 =   -6.8
  R5  4 CaO + Al2O3 + Fe2O3 -> C4AF           dH298 =  -41.7
Heats are *not* inserted here: they follow from the formation enthalpies in
thermo.py when the energy balance is written on absolute enthalpies.

Rate laws (Arrhenius, activation energies after Mastorakos et al. 1999,
Appl. Math. Modelling 23:55; pre-exponentials recalibrated for the first-order
holdup formulation used here so that the classic industrial time scales hold:
belite forms 1000-1300 C, alite needs melt and completes within ~10-15 min at
1450 C, calcination is equilibrium/heat-transfer limited):
  r1 = k1(T) n_CaCO3 max(0, 1 - pCO2/peq(T))          Baker (1962) peq
  r2 = k2(T) n_SiO2 fCaO
  r3 = k3(T) n_C2S  fCaO fliq(T)
  r4 = k4(T) max(0, nA - nF) fCaO     (Al2O3 left after ferrite)
  r5 = k5(T) min(nA, nF) fCaO
with fCaO = nCaO / (nCaO + 0.02 n_tot) a saturating free-lime availability.
The ordering R5 before R4 and the Al/Fe split make the end state identical to
Bogue's assumptions, so a fully burnt clinker reproduces Bogue composition.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .thermo import M_SOLID, NS, R_GAS, S, f_liquid

# oxide molar masses
M_CaO, M_SiO2, M_Al2O3, M_Fe2O3, M_MgO = 56.077, 60.084, 101.961, 159.688, 40.304

# --------------------------------------------------------------------------
# Kinetic constants
# --------------------------------------------------------------------------
KIN = {
    #        A [1/s]     E [kJ/kmol]
    "calc": (2.0e8, 175.7e3),  # CaCO3 decomposition; A calibrated: DoC ~92 % at 875 C calciner exit
    "c2s": (1.0e7, 240.0e3),
    "c3s": (1.2e10, 420.0e3),
    "c3a": (1.0e9, 310.0e3),
    "c4af": (1.5e10, 330.0e3),
}


def k_arr(name: str, T):
    A, E = KIN[name]
    return A * np.exp(-E / (R_GAS * np.asarray(T, float)))


def peq_co2_atm(T):
    """CaCO3 equilibrium CO2 pressure, atm (Baker 1962)."""
    return 4.137e7 * np.exp(-20474.0 / np.asarray(T, float))


def T_eq_calcination(p_co2_atm):
    """Temperature at which peq == pCO2 (lower bound for calcining material)."""
    p = np.clip(np.asarray(p_co2_atm, float), 1e-4, 10.0)
    return 20474.0 / np.log(4.137e7 / p)


# --------------------------------------------------------------------------
# Reaction step
# --------------------------------------------------------------------------
STOICH = np.zeros((5, NS))
STOICH[0, [S["CaCO3"], S["CaO"]]] = [-1, 1]
STOICH[1, [S["CaO"], S["SiO2"], S["C2S"]]] = [-2, -1, 1]
STOICH[2, [S["C2S"], S["CaO"], S["C3S"]]] = [-1, -1, 1]
STOICH[3, [S["CaO"], S["Al2O3"], S["C3A"]]] = [-3, -1, 1]
STOICH[4, [S["CaO"], S["Al2O3"], S["Fe2O3"], S["C4AF"]]] = [-4, -1, -1, 1]


def react(n, T, dt, p_co2_atm, clinkering=True, calc_cap=None):
    """Advance solid composition by dt (s).  Vectorised over leading axis.

    Returns (n_new, co2_released_kmol).  Uses the exact exponential integrator
    for each first-order law (unconditionally stable) and clips every extent
    to the reactants actually available, so amounts never go negative.
    Energy is handled by the caller through the enthalpy balance.
    `calc_cap` (kmol, per row) optionally bounds the calcination extent - the
    caller passes the amount whose endotherm can be supplied by the sensible
    heat above the equilibrium temperature, so a large explicit step can never
    cool calcining material below T_eq (physically impossible).
    """
    n = np.array(n, float, copy=True)
    T = np.asarray(T, float)
    one = n.ndim == 1
    if one:
        n = n[None, :]
        T = np.atleast_1d(T)
    p = np.broadcast_to(np.asarray(p_co2_atm, float), T.shape)

    # R1 calcination
    drive = np.clip(1.0 - p / peq_co2_atm(T), 0.0, 1.0)
    x1 = n[:, S["CaCO3"]] * (1.0 - np.exp(-k_arr("calc", T) * drive * dt))
    if calc_cap is not None:
        x1 = np.minimum(x1, np.maximum(np.atleast_1d(calc_cap), 0.0))
    n[:, S["CaCO3"]] -= x1
    n[:, S["CaO"]] += x1
    co2 = x1

    if clinkering:
        ntot = n.sum(1) + 1e-12

        def fcao():
            c = n[:, S["CaO"]]
            return c / (c + 0.02 * ntot)

        # R5 ferrite
        nA, nF = n[:, S["Al2O3"]], n[:, S["Fe2O3"]]
        x5 = np.minimum(nA, nF) * (1.0 - np.exp(-k_arr("c4af", T) * fcao() * dt))
        x5 = np.minimum(x5, n[:, S["CaO"]] / 4.0)
        n += x5[:, None] * STOICH[4]
        # R4 aluminate from Al2O3 not needed by ferrite
        availA = np.maximum(n[:, S["Al2O3"]] - n[:, S["Fe2O3"]], 0.0)
        x4 = availA * (1.0 - np.exp(-k_arr("c3a", T) * fcao() * dt))
        x4 = np.minimum(x4, n[:, S["CaO"]] / 3.0)
        n += x4[:, None] * STOICH[3]
        # R2 belite
        x2 = n[:, S["SiO2"]] * (1.0 - np.exp(-k_arr("c2s", T) * fcao() * dt))
        x2 = np.minimum(x2, n[:, S["CaO"]] / 2.0)
        n += x2[:, None] * STOICH[1]
        # R3 alite (requires melt)
        x3 = n[:, S["C2S"]] * (1.0 - np.exp(-k_arr("c3s", T) * fcao() * f_liquid(T) * dt))
        x3 = np.minimum(x3, n[:, S["CaO"]])
        n += x3[:, None] * STOICH[2]

    np.maximum(n, 0.0, out=n)
    if one:
        return n[0], float(co2[0])
    return n, co2


# --------------------------------------------------------------------------
# Raw mix design / quality indices
# --------------------------------------------------------------------------
@dataclass
class RawMix:
    """Target clinker moduli -> raw meal composition."""

    LSF: float = 1.00  # kiln feed; coal ash absorption lowers clinker LSF to ~0.95
    SM: float = 2.5
    AM: float = 1.6
    MgO: float = 0.015  # clinker mass fraction
    moisture: float = 0.005  # free moisture of kiln feed, kg/kg wet

    def clinker_oxides(self):
        """Clinker oxide mass fractions (C, S, A, F, M) summing to 1 - minors."""
        # F = x ; A = AM x ; S = SM (A+F) ; C = LSF (2.8 S + 1.18 A + 0.65 F)
        f, a, s = 1.0, self.AM, self.SM * (1.0 + self.AM)
        c = self.LSF * (2.8 * s + 1.18 * a + 0.65 * f)
        total = 1.0 - self.MgO
        x = total / (f + a + s + c)
        return {"CaO": c * x, "SiO2": s * x, "Al2O3": a * x, "Fe2O3": f * x, "MgO": self.MgO}

    def meal_kmol_per_kg(self) -> np.ndarray:
        """Species amounts (kmol) in 1 kg of wet kiln feed."""
        ox = self.clinker_oxides()
        n = np.zeros(NS)
        n[S["CaCO3"]] = ox["CaO"] / M_CaO
        n[S["SiO2"]] = ox["SiO2"] / M_SiO2
        n[S["Al2O3"]] = ox["Al2O3"] / M_Al2O3
        n[S["Fe2O3"]] = ox["Fe2O3"] / M_Fe2O3
        n[S["MgO"]] = ox["MgO"] / M_MgO
        dry_mass = float(n @ M_SOLID)
        n *= (1.0 - self.moisture) / dry_mass
        n[S["H2O_l"]] = self.moisture / M_SOLID[S["H2O_l"]]
        return n


def oxides_of(n) -> dict:
    """Total oxide masses (kg) contained in a solid species vector."""
    n = np.asarray(n, float)
    C = (n[S["CaCO3"]] + n[S["CaO"]] + 2 * n[S["C2S"]] + 3 * n[S["C3S"]] + 3 * n[S["C3A"]] + 4 * n[S["C4AF"]]) * M_CaO
    Si = (n[S["SiO2"]] + n[S["C2S"]] + n[S["C3S"]]) * M_SiO2
    A = (n[S["Al2O3"]] + n[S["C3A"]] + n[S["C4AF"]]) * M_Al2O3
    F = (n[S["Fe2O3"]] + n[S["C4AF"]]) * M_Fe2O3
    M = n[S["MgO"]] * M_MgO
    return {"CaO": C, "SiO2": Si, "Al2O3": A, "Fe2O3": F, "MgO": M}


def moduli(n) -> dict:
    o = oxides_of(n)
    C, Si, A, F = o["CaO"], o["SiO2"], o["Al2O3"], o["Fe2O3"]
    den = 2.8 * Si + 1.18 * A + 0.65 * F
    return {"LSF": C / den if den > 0 else 0.0, "SM": Si / (A + F) if A + F > 0 else 0.0, "AM": A / F if F > 0 else 0.0}


def bogue(n) -> dict:
    """Potential phase composition (mass fractions of the loss-free oxides)."""
    o = oxides_of(n)
    tot = sum(o.values())
    if tot <= 0:
        return {"C3S": 0, "C2S": 0, "C3A": 0, "C4AF": 0}
    C, Si, A, F = (o[k] / tot for k in ("CaO", "SiO2", "Al2O3", "Fe2O3"))
    c3s = 4.071 * C - 7.600 * Si - 6.718 * A - 1.430 * F
    c2s = 2.867 * Si - 0.7544 * c3s
    return {"C3S": c3s, "C2S": c2s, "C3A": 2.650 * A - 1.692 * F, "C4AF": 3.043 * F}


def phase_fractions(n) -> dict:
    n = np.asarray(n, float)
    m = n * M_SOLID
    tot = m.sum()
    if tot <= 0:
        return {}
    return {
        "C3S": m[S["C3S"]] / tot,
        "C2S": m[S["C2S"]] / tot,
        "C3A": m[S["C3A"]] / tot,
        "C4AF": m[S["C4AF"]] / tot,
        "freeCaO": m[S["CaO"]] / tot,
        "CaCO3": m[S["CaCO3"]] / tot,
    }


def degree_of_calcination(n) -> float:
    """Fraction of all Ca that is no longer carbonate (plant 'DoC' of hot meal)."""
    n = np.asarray(n, float)
    ca_tot = oxides_of(n)["CaO"] / M_CaO
    return 1.0 - n[S["CaCO3"]] / ca_tot if ca_tot > 0 else 0.0


def calcination_cap(n, T, p_co2_atm, extra_C=0.0):
    """Max CaCO3 (kmol) that can calcine using sensible heat above T_eq(pCO2).

    n: (..., NS) holdup, T: current temperature, extra_C: lumped capacity (kJ/K)
    sharing the temperature (refractory).  Returns 0 where T <= T_eq.
    """
    from .thermo import G, h_gas, h_solid, solid_enthalpy

    n = np.asarray(n, float)
    T = np.asarray(T, float)
    Teq = T_eq_calcination(p_co2_atm)
    Teq = np.broadcast_to(Teq, T.shape)
    avail = (solid_enthalpy(n, T) - solid_enthalpy(n, Teq)) + extra_C * (T - Teq)
    hs, hg = h_solid(Teq), h_gas(Teq)
    dh = hs[..., S["CaO"]] + hg[..., G["CO2"]] - hs[..., S["CaCO3"]]
    return np.where(Teq < T, np.maximum(avail, 0.0) / dh, 0.0)
