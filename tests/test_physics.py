"""Physics verification: thermodynamic data, chemistry, combustion, conservation."""

import numpy as np
import pytest

from cemsim import thermo as th
from cemsim.chemistry import RawMix, T_eq_calcination, bogue, moduli, peq_co2_atm, react
from cemsim.fuels import Fuel, burn


def test_gas_cp_matches_nist_shomate():
    T = np.array([300.0, 600.0, 1000.0, 1500.0, 2200.0])
    for name in ("N2", "O2", "CO2", "H2O", "CO", "NO"):
        cp = th.cp_gas(T)[:, th.G[name]]
        ref = th._shomate_cp(name, T)
        assert np.max(np.abs(cp / ref - 1)) < 0.005, name


def test_calcination_enthalpy():
    for T, ref in ((298.15, 178.3), (1173.15, 166.7)):
        hs, hg = th.h_solid(T), th.h_gas(T)
        dh = (hs[th.S["CaO"]] + hg[th.G["CO2"]] - hs[th.S["CaCO3"]]) / 1000
        assert dh == pytest.approx(ref, abs=0.6)


def test_solid_cp_at_room_temperature():
    # literature cp at 298 K, J/(mol K)
    ref = {"CaCO3": 83.5, "CaO": 42.0, "Al2O3": 79.0, "MgO": 37.2, "C3S": 171.9, "C2S": 128.7, "C3A": 209.8}
    cp = th.cp_solid(298.15)
    for k, v in ref.items():
        assert cp[th.S[k]] == pytest.approx(v, rel=0.04), k


def test_equilibrium_calcination_temperature():
    # CaCO3 decomposes at ~ 894 C in pure CO2 at 1 atm (Baker 1962)
    assert T_eq_calcination(1.0) - 273.15 == pytest.approx(894, abs=3)
    assert peq_co2_atm(T_eq_calcination(0.3)) == pytest.approx(0.3, rel=1e-6)


def test_rawmix_reproduces_moduli_and_bogue():
    rm = RawMix(LSF=0.95, SM=2.5, AM=1.6)
    n = rm.meal_kmol_per_kg()
    m = moduli(n)
    assert m["LSF"] == pytest.approx(0.95, abs=1e-9)
    assert m["SM"] == pytest.approx(2.5, abs=1e-9)
    assert m["AM"] == pytest.approx(1.6, abs=1e-9)
    # fully burnt clinker must reproduce the Bogue potential composition
    nn = n.copy()
    nn[th.S["H2O_l"]] = 0
    nc, _ = react(nn, 1723.0, 1e5, 1.0)
    b = bogue(nn)
    mass = nc @ th.M_SOLID
    for ph in ("C3S", "C2S", "C3A", "C4AF"):
        assert nc[th.S[ph]] * th.M_SOLID[th.S[ph]] / mass == pytest.approx(b[ph], abs=0.003)


def test_theoretical_heat_of_clinker_formation():
    rm = RawMix(LSF=0.95)
    n = rm.meal_kmol_per_kg()
    n[th.S["H2O_l"]] = 0
    nc, co2 = react(n.copy(), 1723.0, 1e5, 1.0)
    dH = float(nc @ th.h_solid(th.T0)) + co2 * th.HF_GAS[th.G["CO2"]] - float(n @ th.h_solid(th.T0))
    q = dH / float(nc @ th.M_SOLID)
    assert 1600 < q < 1800  # literature 1650-1800 kJ/kg clinker


def test_fuel_lhv_is_released_exactly():
    f = Fuel()
    hf = (f.hf(), 0.0)
    g = th.air(f.air_stoich_kg() * 1.2)
    g[th.G["FUELK"]] = 1.0
    H0 = th.gas_enthalpy_flow(g, th.T0, hf)
    _, ash = burn(g, th.G["FUELK"], f, 1.0, 0.0)
    H1 = th.gas_enthalpy_flow(g, th.T0, hf) + float(ash @ th.h_solid(th.T0))
    assert H0 - H1 == pytest.approx(f.LHV, rel=1e-6)


def test_incomplete_combustion_releases_less():
    f = Fuel()
    hf = (f.hf(), 0.0)
    out = []
    for co in (0.0, 0.5):
        g = th.air(f.air_stoich_kg() * 1.2)
        g[th.G["FUELK"]] = 1.0
        H0 = th.gas_enthalpy_flow(g, th.T0, hf)
        _, ash = burn(g, th.G["FUELK"], f, 1.0, co)
        out.append(H0 - th.gas_enthalpy_flow(g, th.T0, hf) - float(ash @ th.h_solid(th.T0)))
    lost = out[0] - out[1]
    assert lost == pytest.approx(0.5 * f.C / 12.011 * 282990, rel=1e-3)


def test_adiabatic_flame_temperature_coal():
    f = Fuel()
    hf = (f.hf(), 0.0)
    g = th.air(f.air_stoich_kg() * 1.1)
    g[th.G["FUELK"]] = 1.0
    H = th.gas_enthalpy_flow(g, th.T0, hf)
    _, ash = burn(g, th.G["FUELK"], f, 1.0, 0.0)
    lo, hi = 300.0, 3500.0
    for _ in range(60):
        m = 0.5 * (lo + hi)
        if th.gas_enthalpy_flow(g, m, hf) + float(ash @ th.h_solid(m)) > H:
            hi = m
        else:
            lo = m
    assert 1900 < m - 273.15 < 2100


def test_reaction_step_never_negative_and_conserves_elements():
    rm = RawMix()
    n = rm.meal_kmol_per_kg() * 1000
    for T in (900.0, 1200.0, 1500.0, 1750.0):
        n2, co2 = react(n, T, 50.0, 0.3)
        assert np.all(n2 >= 0)

        # calcium balance
        def ca(v):
            return v[0] + v[1] + 2 * v[7] + 3 * v[8] + 3 * v[9] + 4 * v[10]

        assert ca(n2) == pytest.approx(ca(n), rel=1e-12)

        def si(v):
            return v[2] + v[7] + v[8]

        assert si(n2) == pytest.approx(si(n), rel=1e-12)
        # mass: solids lost == CO2 released
        assert float((n - n2) @ th.M_SOLID) == pytest.approx(co2 * 44.009, rel=1e-9, abs=1e-9)
