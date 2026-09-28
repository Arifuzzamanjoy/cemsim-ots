"""Cyclone preheater (S1..S4) + in-line calciner with stage-5 cyclone (CAL).

Each node i is the riser duct + cyclone where gas coming from node i+1 meets
the meal separated by cyclone i-1.  Gas-solid exchange in a riser reaches
~thermal equilibrium within ~0.1 s, so gas and solids leave the node at a
common temperature T_i; the node carries a lumped effective refractory
capacity C_i (the brick hot-face mass that follows the gas on the minute time
scale).  Balances (explicit Euler in energy, exact exponential in holdup):

  dn_i/dt = sep_{i-1} + carry_{i+1} - n_i/tau_i + R_i          (species, kmol)
  dU_i/dt = H_in - H_out(T_i) - UA_i (T_i - T_amb)             (kJ, absolute)
  U_i     = sum n_i h(T_i) + C_i (T_i - T0)

Cyclone i separates eta_i of its solids outflow downwards (to node i+1 / the
kiln for CAL); (1-eta_i) is carried upwards with the gas (internal dust
recirculation).  S1 carry-over is dust loss to the filter.

CAL additionally receives tertiary air and calciner coal; coal burnout
beta = 1 - exp(-k(T) tau_g) (global char+volatile rate), O2-limited, with the
same CO split as the kiln flame.  Unburnt coal continues upward and may burn in
S4.  Calcination: Arrhenius with CO2 back-pressure (1 - pCO2/peq(T)), capped
by the heat available above T_eq(pCO2).
"""

from __future__ import annotations

import math

import numpy as np

from .. import thermo as th
from ..chemistry import calcination_cap, react
from ..fuels import Fuel, burn, burn_co, co_split
from ..params import PreheaterParams

G, S = th.G, th.S
NAMES = ["S1", "S2", "S3", "S4", "CAL"]


class Preheater:
    NN = 5

    def __init__(self, p: PreheaterParams):
        self.p = p
        self.n = np.zeros((self.NN, th.NS))
        self.T = np.array([600.0, 800.0, 950.0, 1050.0, 1150.0])
        self.C = np.array(p.c_eff, float)
        self.U = th.solid_enthalpy(self.n, self.T) + self.C * (self.T - th.T0)
        self.gas_T = self.T.copy()
        self.pco2 = np.full(self.NN, 0.25)
        self.xo2 = np.full(self.NN, 0.03)
        self.blocked = np.zeros(self.NN, bool)  # cyclone blockage flags
        self.cone_buildup = np.zeros((self.NN, th.NS))
        self.cone_buildup_H = np.zeros(self.NN)
        self.flush = np.zeros(self.NN, bool)
        self.out = {}

    # ------------------------------------------------------------------
    def step(
        self,
        dt,
        feed_rate,
        feed_T,
        kiln_gas,
        kiln_gas_T,
        ta_gas,
        ta_T,
        coal_cal_kgs,
        coal_T,
        false_air_kgs,
        fuel_c: Fuel,
        fuel_k: Fuel,
        hf,
        T_amb,
    ):
        p, NN = self.p, self.NN
        n, U, T = self.n, self.U, self.T
        eta = np.array(p.eta)
        tau = np.array(p.tau_solid)

        # ---------------- solids outflows at current holdup ---------------
        a = 1.0 - np.exp(-dt / tau)
        out = n * a[:, None]  # kmol over dt
        hs_T = th.h_solid(T)  # (NN, NS)
        # outflow enthalpy (holdup homogeneous at T_i incl. latent - negligible here)
        out_H = np.einsum("ij,ij->i", out, hs_T)
        sep = out * eta[:, None]
        carry = out * (1.0 - eta)[:, None]
        sep_H = out_H * eta
        carry_H = out_H * (1.0 - eta)

        # blockage: separated meal of a blocked cyclone stays in its cone
        to_next = sep.copy()
        to_next_H = sep_H.copy()
        for i in range(NN):
            if self.blocked[i]:
                self.cone_buildup[i] += sep[i]
                self.cone_buildup_H[i] += sep_H[i]
                to_next[i] = 0.0
                to_next_H[i] = 0.0
            elif self.flush[i] and self.cone_buildup[i].sum() > 0:
                # blockage released: the cone empties within ~20 s (avalanche)
                fr = min(1.0, dt / 20.0)
                to_next[i] += self.cone_buildup[i] * fr
                to_next_H[i] += self.cone_buildup_H[i] * fr
                self.cone_buildup[i] *= 1.0 - fr
                self.cone_buildup_H[i] *= 1.0 - fr
                if self.cone_buildup[i].sum() < 1e-6:
                    self.flush[i] = False

        n = n - out
        # solids in
        feed_H = float(feed_rate @ th.h_solid(feed_T))
        n[0] += feed_rate * dt
        Hin = np.zeros(NN)
        Hin[0] += feed_H * dt
        for i in range(1, NN):
            n[i] += to_next[i - 1]
            Hin[i] += to_next_H[i - 1]
        for i in range(NN - 1):
            n[i] += carry[i + 1]
            Hin[i] += carry_H[i + 1]
        Hout = out_H.copy()
        hot_meal = to_next[NN - 1] / dt
        hot_meal_H = to_next_H[NN - 1] / dt
        dust = carry[0] / dt
        dust_H = carry_H[0] / dt

        # ---------------- gas path CAL -> S1 ------------------------------
        g = kiln_gas.astype(float).copy()
        Hg_in_total = np.zeros(NN)
        Hg_out_total = np.zeros(NN)
        H_flow = float(kiln_gas @ th.h_gas(kiln_gas_T, hf))
        # CAL extras: tertiary air + calciner coal
        g += ta_gas
        H_flow += float(ta_gas @ th.h_gas(ta_T, hf))
        g[G["FUELC"]] += coal_cal_kgs
        H_flow += coal_cal_kgs * float(th.h_gas(coal_T, hf)[G["FUELC"]])
        no_in_kiln = kiln_gas[G["NO"]]

        co2_rel = np.zeros(NN)
        h2o_rel = np.zeros(NN)
        ash_add = np.zeros((NN, th.NS))
        burnt = np.zeros(NN)
        for i in range(NN - 1, -1, -1):
            # false air
            fa = th.air(false_air_kgs[i])
            g += fa
            H_flow += float(fa @ th.h_gas(T_amb, hf))
            Hg_in_total[i] = H_flow
            # reactions in the solids (current T)
            n_i = n[i]
            cap = calcination_cap(n_i[None, :], np.array([T[i]]), np.array([self.pco2[i]]), extra_C=self.C[i])[0]
            n_new, co2 = react(n_i, T[i], dt, self.pco2[i], clinkering=(i == NN - 1), calc_cap=cap)
            # free-moisture evaporation
            kev = 0.5 * min(1.0, max(0.0, (T[i] - 330.0) / 40.0))
            ev = n_new[S["H2O_l"]] * (1.0 - math.exp(-kev * dt))
            n_new[S["H2O_l"]] -= ev
            n[i] = n_new
            co2_rel[i], h2o_rel[i] = co2, ev
            g[G["CO2"]] += co2 / dt
            g[G["H2O"]] += ev / dt
            # combustion of any coal in the gas; char oxidation ~ half order in O2,
            # referenced to the calibrated nominal calciner outlet O2 (2.1 %)
            o2_fac = math.sqrt(max(self.xo2[i], 0.0) / 0.021)
            beta = 1.0 - math.exp(-p.burn_A * math.exp(-p.burn_E / (th.R_GAS * T[i])) * p.tau_gas[i] * o2_fac)
            for idx, fu in ((G["FUELC"], fuel_c), (G["FUELK"], fuel_k)):
                if g[idx] > 1e-9:
                    tot = g[:6].sum()
                    xo2 = g[G["O2"]] / tot if tot > 1e-12 else 0.0
                    b, ash = burn(g, idx, fu, g[idx] * beta, co_split(xo2))
                    burnt[i] += b
                    ash_add[i] += ash * dt
            if g[G["CO"]] > 0:
                tot = g[:6].sum()
                xo2 = g[G["O2"]] / tot if tot > 1e-12 else 0.0
                burn_co(g, 0.6 / (1.0 + math.exp(-(T[i] - 1000.0) / 40.0)) * min(1.0, xo2 / 0.01))
            if i == NN - 1:
                red = p.no_reduction * min(1.0, coal_cal_kgs / 2.5)
                dno = g[G["NO"]] * red
                g[G["NO"]] -= dno
                g[G["N2"]] += 0.5 * dno
                g[G["O2"]] += 0.5 * dno
            tot = g[:6].sum()
            if tot > 1e-12:  # no gas flow (all fans stopped): keep last composition
                self.pco2[i] = g[G["CO2"]] / tot
                self.xo2[i] = g[G["O2"]] / tot
            Hg_out = float(g @ th.h_gas(T[i], hf))
            Hg_out_total[i] = Hg_out
            H_flow = Hg_out
        exit_gas = g

        # energy of CO2/H2O released is inside Hg_out (gas at T_i); solids lose
        # the carbonate/moisture species, so node balance closes automatically.
        n += ash_add
        loss = np.array(p.ua_loss) * (T - T_amb)
        U = U + Hin - Hout + dt * (Hg_in_total - Hg_out_total - loss)
        T_new = th.solve_T_solid(n, U, T, extra_C=self.C)

        self.n, self.U, self.T = n, U, T_new
        self.out = {
            "hot_meal": hot_meal,
            "hot_meal_H": hot_meal_H,
            "hot_meal_T": float(T_new[-1]),
            "exit_gas": exit_gas,
            "exit_T": float(T_new[0]),
            "dust": dust,
            "dust_H": dust_H,
            "burnt_kgs": burnt / 1.0,
            "unburnt_kgs": float(exit_gas[G["FUELC"]] + exit_gas[G["FUELK"]]),
            "shell_loss_kW": float(loss.sum()),
            "co2_calc_kmol_s": float(co2_rel.sum() / dt),
            "no_in_kiln": float(no_in_kiln),
        }
        return self.out

    def state(self):
        st = {
            k: np.array(getattr(self, k), copy=True)
            for k in ("n", "U", "T", "pco2", "xo2", "blocked", "cone_buildup", "cone_buildup_H", "flush")
        }
        st["out"] = dict(self.out)
        return st

    def load(self, st):
        for k, v in st.items():
            setattr(self, k, dict(v) if k == "out" else np.array(v, copy=True))
