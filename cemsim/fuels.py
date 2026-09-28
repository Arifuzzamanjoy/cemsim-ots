"""Solid fuel definition and combustion stoichiometry.

A fuel is described by its as-fired ultimate analysis and lower heating value.
Its pseudo-species formation enthalpy is chosen so that burning 1 kg to CO2,
H2O(g), N2 and ash oxides at 25 C releases exactly the LHV:

    Hf_fuel = sum(products nu_i Hf_i) + LHV          [kJ/kg]

so combustion needs no explicit heat-release term: the enthalpy balance of the
gas yields the flame temperature, and partial oxidation to CO automatically
releases 283 kJ/mol less.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .thermo import HF_GAS, HF_SOLID, M_SOLID, NS, G, S

M_C, M_H2, M_O2, M_N2, M_H2O = 12.011, 2.016, 31.998, 28.013, 18.015

# typical bituminous coal ash (mass fractions of ash)
DEFAULT_ASH = {"SiO2": 0.52, "Al2O3": 0.28, "Fe2O3": 0.12, "CaO": 0.06, "MgO": 0.02}


@dataclass
class Fuel:
    name: str = "coal"
    C: float = 0.650
    H: float = 0.042
    O: float = 0.080
    N: float = 0.013
    moisture: float = 0.015
    ash: float = 0.200
    LHV: float = 25600.0  # kJ/kg as fired
    ash_comp: dict = field(default_factory=lambda: dict(DEFAULT_ASH))

    def normalised(self) -> Fuel:
        tot = self.C + self.H + self.O + self.N + self.moisture + self.ash
        k = 1.0 / tot
        return Fuel(
            self.name,
            self.C * k,
            self.H * k,
            self.O * k,
            self.N * k,
            self.moisture * k,
            self.ash * k,
            self.LHV,
            dict(self.ash_comp),
        )

    # ---- stoichiometry (per kg fuel) ----------------------------------
    def o2_stoich(self) -> float:
        """kmol O2 per kg fuel for complete combustion."""
        return self.C / M_C + self.H / (2 * M_H2) - self.O / M_O2

    def air_stoich_kg(self) -> float:
        return self.o2_stoich() / 0.2095 * (0.2095 * M_O2 + 0.7905 * M_N2)

    def ash_kmol(self) -> np.ndarray:
        """Solid species vector (kmol) of ash oxides per kg fuel."""
        n = np.zeros(NS)
        for ox, frac in self.ash_comp.items():
            key = "CaO" if ox == "CaO" else ox
            n[S[key]] += self.ash * frac / M_SOLID[S[key]]
        return n

    def hf(self) -> float:
        """Pseudo-species formation enthalpy, kJ/kg."""
        prod = (self.C / M_C) * HF_GAS[G["CO2"]] + (self.H / M_H2 + self.moisture / M_H2O) * HF_GAS[G["H2O"]]
        prod += float(self.ash_kmol() @ HF_SOLID)
        return prod + self.LHV

    def hhv(self) -> float:
        return self.LHV + 2442.0 * (self.H * M_H2O / M_H2 + self.moisture)


def burn(g: np.ndarray, idx: int, fuel: Fuel, amount_kg: float, co_frac: float):
    """Burn `amount_kg` of fuel pseudo-species g[idx] in gas stream g (in place).

    Carbon goes to CO2 except the fraction `co_frac` that goes to CO.  The burnt
    amount is limited by the O2 available.  Returns (burnt_kg, ash_kmol_vector).
    """
    amount_kg = max(0.0, min(amount_kg, g[idx]))
    if amount_kg <= 0.0:
        return 0.0, np.zeros(NS)
    o2_per_kg = (fuel.C / M_C) * (1.0 - 0.5 * co_frac) + fuel.H / (2 * M_H2) - fuel.O / M_O2
    if o2_per_kg > 0:
        amount_kg = min(amount_kg, max(g[G["O2"]], 0.0) / o2_per_kg)
    if amount_kg <= 0.0:
        return 0.0, np.zeros(NS)
    g[idx] -= amount_kg
    g[G["O2"]] -= amount_kg * o2_per_kg
    g[G["CO2"]] += amount_kg * fuel.C / M_C * (1.0 - co_frac)
    g[G["CO"]] += amount_kg * fuel.C / M_C * co_frac
    g[G["H2O"]] += amount_kg * (fuel.H / M_H2 + fuel.moisture / M_H2O)
    g[G["N2"]] += amount_kg * fuel.N / M_N2
    g[G["O2"]] = max(g[G["O2"]], 0.0)
    return amount_kg, fuel.ash_kmol() * amount_kg


def burn_co(g: np.ndarray, frac: float) -> float:
    """Oxidise a fraction of the CO present (O2-limited). Returns kmol CO burnt."""
    x = min(g[G["CO"]] * max(0.0, min(frac, 1.0)), 2.0 * g[G["O2"]])
    if x <= 0:
        return 0.0
    g[G["CO"]] -= x
    g[G["CO2"]] += x
    g[G["O2"]] -= 0.5 * x
    return x


def co_split(x_o2: float) -> float:
    """Fraction of burning carbon that leaves the flame front as CO.

    Empirical logistic in local O2 mole fraction: ~0.1 % at 3 % O2, ~7 % at 1.5 %
    and 50 % at 0.6 % O2 - reproduces the steep CO rise operators see when kiln
    inlet O2 falls below ~1.5 %.
    """
    return 1.0 / (1.0 + np.exp(min((x_o2 - 0.006) / 0.0035, 50.0)))
