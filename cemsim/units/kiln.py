"""Rotary kiln: 1-D counter-current cell model.

Per axial cell j (j = 0 at the feed end, N-1 at the burner end):

  bed   : dynamic species holdup n_j (kmol) and enthalpy U_j (kJ, absolute incl.
          formation + latent melt heat); T_b from U by Newton inversion.
  wall  : dynamic refractory temperature T_w (lumped brick, C_w = rho c e A).
  shell : quasi-steady T_sh from conduction = convection + radiation to ambient.
  gas   : quasi-steady (residence ~1 s << bed/wall time constants); the gas is
          marched from the burner end to the inlet, solving each cell's
          enthalpy balance for T_g with Newton:
              H_in + H_src(T_b) - H_out(T_g) - H_ash(T_g) - Q_gb(T_g) - Q_gw(T_g) = 0

Heat transfer (per cell, Hottel grey-gas with segment geometry of the bed):
  Q_gb  = A_bed [h_gb (Tg-Tb) + s e_gb (Tg^4-Tb^4)]      gas -> bed surface
  Q_gw  = A_wexp[h_gw (Tg-Tw) + s e_gw (Tg^4-Tw^4)]      gas -> exposed wall
  Q_wb  = A_bed s e_wb (1-e_g) (Tw^4-Tb^4)               exposed wall -> bed
  Q_cwb = A_wcov h_cwb (Tw-Tb)                           covered wall -> bed
  Q_sh  = A_sh (Tw-Tsh)/R_wall                           wall -> shell -> ambient
  e_g = 1 - exp(-kappa L_m),  kappa = k_g (pCO2+pH2O) + k_dust + k_flame*luminous

Solids transport: Sullivan (US Bur. Mines 1927) residence time
  t[min] = 1.77 L sqrt(beta) F / (S D N)   (beta repose deg, S slope deg, N rpm)
distributed over N tanks-in-series (exact exponential outflow per step).

Flame: pulverised coal burnout follows a Weibull(k=2) profile in distance from
the burner tip with scale L_f; each cell burns the conditional fraction of the
remaining coal, O2-limited.  L_f shortens with primary-air momentum and hotter
secondary air.  CO from the O2-dependent split; thermal NO by the Zeldovich
rate with equilibrium O atoms (Westenberg 1971) and reverse reaction
(approach to N2+O2 <=> 2NO equilibrium).
"""

from __future__ import annotations

import math

import numpy as np

from .. import thermo as th
from ..chemistry import calcination_cap, react
from ..fuels import Fuel, burn, burn_co, co_split
from ..params import KilnParams, bed_angle_from_fill

SIG = th.SIGMA
G, S = th.G, th.S


class Kiln:
    def __init__(self, p: KilnParams):
        self.p = p
        N = p.n_cells
        self.N = N
        self.dx = p.length / N
        self.x_mid = (np.arange(N) + 0.5) * self.dx  # from inlet
        self.R = p.d_inner / 2.0
        self.A_cross = math.pi * self.R**2
        self.A_shell = math.pi * p.d_shell * self.dx  # m2 per cell
        self.C_w = p.brick_rho_cp * math.pi * (p.d_inner + p.brick_thick) * p.brick_thick * self.dx
        # coated burning zone = last 35 % of the kiln
        frac = self.x_mid / p.length
        self.R_wall = np.where(frac > 0.65, p.r_wall_bz, p.r_wall_inlet)
        self.slope_deg = math.degrees(math.atan(p.slope_pct / 100.0))
        self.L_m = 0.9 * p.d_inner

        # ---- states (initialised to a hot-idle profile; see init_profile) ----
        self.n = np.zeros((N, th.NS))
        self.T_b = np.full(N, 1100.0)
        self.U = th.solid_enthalpy(self.n, self.T_b)
        self.T_w = np.full(N, 1300.0)
        self.T_g = np.full(N, 1400.0)
        self.T_sh = np.full(N, 550.0)
        self.no_ppm_out = 0.0
        self.eps_g_prev = np.full(N, 0.3)
        self.bed_limiter = np.ones(N)
        self.out = {}
        self.ring_factor = 1.0  # disturbance: ring/constriction (>1 slows transport)
        self.coating_factor = np.ones(N)

    # ------------------------------------------------------------------
    def init_profile(self, T_in, T_bz, T_w_in, T_w_bz):
        s = self.x_mid / self.p.length
        self.T_b = T_in + (T_bz - T_in) * s**1.5
        self.T_w = T_w_in + (T_w_bz - T_w_in) * s**1.5
        self.T_g = self.T_w + 150.0
        self.U = th.solid_enthalpy(self.n, self.T_b)

    def residence_time(self, rpm):
        """Total solids residence time (s), Sullivan formula."""
        if rpm < 0.02:
            return 1e9
        t_min = (
            1.77
            * self.p.length
            * math.sqrt(self.p.repose_deg)
            * self.p.sullivan_F
            / (self.slope_deg * self.p.d_inner * rpm)
        )
        return 60.0 * t_min * self.ring_factor

    # ------------------------------------------------------------------
    def step(self, dt, feed_rate, feed_H, gas_in, gas_in_H, fuel: Fuel, hf, rpm, flame_factor, T_amb):
        """Advance dt seconds.

        feed_rate : kmol/s species vector of hot meal entering at the inlet
        feed_H    : kW enthalpy flow of that meal
        gas_in    : kmol/s gas entering at the burner end (secondary + primary
                    air + coal pseudo-species FUELK)
        gas_in_H  : kW enthalpy flow of gas_in
        hf        : (Hf_kiln_fuel, Hf_calciner_fuel) kJ/kg
        flame_factor : multiplier on flame length (primary air, fineness...)
        """
        p, N, dx = self.p, self.N, self.dx
        n, U = self.n, self.U

        # ---------------- 1. solids transport (tanks in series) ----------
        tau_c = self.residence_time(rpm) / N
        a = 1.0 - math.exp(-dt / tau_c)
        out_n = n * a  # kmol leaving each cell during dt
        out_U = U * a
        n = n - out_n
        U = U - out_U
        n[1:] += out_n[:-1]
        U[1:] += out_U[:-1]
        n[0] += feed_rate * dt
        U[0] += feed_H * dt
        clinker_n = out_n[-1] / dt  # kmol/s to cooler
        clinker_H = out_U[-1] / dt  # kW
        self.T_b = th.solve_T_solid(n, U, self.T_b)

        # ---------------- 2. solid reactions ------------------------------
        # bed interstitial gas is CO2-blanketed during calcination: pCO2 = 1 atm
        cap = calcination_cap(n, self.T_b, 1.0)
        n_new, co2 = react(n, self.T_b, dt, 1.0, clinkering=True, calc_cap=cap)
        # CO2 leaves the bed carrying its enthalpy at bed temperature
        h_co2_b = th.h_gas(self.T_b)[:, G["CO2"]]
        U = U - co2 * h_co2_b
        n = n_new
        co2_rate = co2 / dt  # kmol/s per cell
        T_b = th.solve_T_solid(n, U, self.T_b)

        # ---------------- 3. bed geometry --------------------------------
        mass = n @ th.M_SOLID
        fill = mass / p.rho_bulk / (self.A_cross * dx)
        theta = bed_angle_from_fill(np.maximum(fill, 1e-4))
        chord = 2.0 * self.R * np.sin(theta / 2.0)
        A_b = chord * dx
        A_we = self.R * (2 * math.pi - theta) * dx
        A_wc = self.R * theta * dx

        # ---------------- bed exchange limiter (second law + stiffness) -----
        # A nearly empty cell has a tiny heat capacity, so an explicit step with
        # the full exchange could heat the bed beyond its hottest source (gas or
        # wall).  Estimate this step's exchange from the previous gas state and
        # scale it (factor sb) so the bed moves at most half-way to its source
        # temperature.  The same scaled flows are used in the gas, wall and bed
        # balances, so energy stays exactly conserved; for normally filled cells
        # sb == 1 (no effect).
        eg_prev = self.eps_g_prev
        egb_p = eg_prev * p.eps_bed / (1.0 - (1.0 - eg_prev) * (1.0 - p.eps_bed))
        e_wb0 = 1.0 / (1.0 / p.eps_wall + 1.0 / p.eps_bed - 1.0)
        Tg0, Tw0 = self.T_g, self.T_w
        q_est = (
            (
                A_b * (p.h_gas_bed * (Tg0 - T_b) + SIG * egb_p * (Tg0**4 - T_b**4))
                + A_b * SIG * e_wb0 * (1.0 - eg_prev) * (Tw0**4 - T_b**4)
                + A_wc * p.h_wall_bed * (Tw0 - T_b)
            )
            * 1e-3
            * dt
        )
        C_b = np.maximum(th.solid_heat_capacity(n, T_b), 1e-6)
        heat = q_est > 0
        span = np.where(heat, np.maximum(np.maximum(Tg0, Tw0) - T_b, 0.0), np.maximum(T_b - np.minimum(Tg0, Tw0), 0.0))
        dU_max = 0.5 * C_b * span
        sb = np.where(np.abs(q_est) > dU_max, dU_max / np.maximum(np.abs(q_est), 1e-12), 1.0)
        self.bed_limiter = sb

        # ---------------- 4. gas march burner -> inlet -------------------
        g = gas_in.astype(float).copy()
        H_flow = float(gas_in_H)
        fuel0 = max(g[G["FUELK"]], 1e-12)
        Lf = max(2.0, p.flame_L0 * flame_factor)
        T_g = self.T_g.copy()
        T_w = self.T_w
        Qgb = np.zeros(N)
        Qgw = np.zeros(N)
        eps_g_arr = np.zeros(N)
        ash_rate = np.zeros((N, th.NS))
        burnt_arr = np.zeros(N)
        V_cell = self.A_cross * dx
        o2_profile = np.zeros(N)
        hs = th.h_solid
        for j in range(N - 1, -1, -1):
            # sources: CO2 from bed
            if co2_rate[j] > 0:
                g[G["CO2"]] += co2_rate[j]
                H_flow += co2_rate[j] * h_co2_b[j]
            # combustion (conditional Weibull burnout, O2 limited)
            d_a = p.length - (j + 1) * dx
            d_b = p.length - j * dx
            d_a = max(d_a, 0.0)
            frac = 1.0 - math.exp(-(d_b * d_b - d_a * d_a) / (Lf * Lf))
            want = g[G["FUELK"]] * frac
            burnt = 0.0
            ash = None
            if want > 0:
                tot = g[:6].sum()
                xo2 = g[G["O2"]] / tot if tot > 0 else 0.0
                burnt, ash = burn(g, G["FUELK"], fuel, want, co_split(xo2))
            burnt_arr[j] = burnt
            if ash is not None:
                ash_rate[j] = ash
            # CO burnout (mixing limited)
            tot = g[:6].sum()
            xo2 = g[G["O2"]] / tot if tot > 0 else 0.0
            if g[G["CO"]] > 0:
                burn_co(g, 0.5 / (1.0 + math.exp(-(T_g[j] - 1050.0) / 50.0)) * min(1.0, xo2 / 0.01))
            # emissivity
            tot = g[:6].sum()
            pp = (g[G["CO2"]] + g[G["H2O"]]) / tot if tot > 0 else 0.0
            lum = min(1.0, burnt / (0.08 * fuel0)) if fuel0 > 1e-9 else 0.0
            kappa = p.k_abs_gas * pp + p.k_abs_dust + p.k_abs_flame * lum
            eg = 1.0 - math.exp(-kappa * self.L_m)
            eps_g_arr[j] = eg
            egb = eg * p.eps_bed / (1.0 - (1.0 - eg) * (1.0 - p.eps_bed))
            egw = eg * p.eps_wall / (1.0 - (1.0 - eg) * (1.0 - p.eps_wall))
            Ab, Aw = A_b[j] * sb[j], A_we[j]
            Tb, Tw = T_b[j], T_w[j]
            ash_j = ash_rate[j]
            has_ash = ash is not None
            # Newton on T_g
            T = T_g[j]
            for _ in range(30):
                hg = th.h_gas(T, hf)
                cpg = th.cp_gas(T)
                f = H_flow - float(g @ hg)
                df = -float(g @ cpg)
                if has_ash:
                    f -= float(ash_j @ hs(T))
                    df -= float(ash_j @ th.cp_solid(T))
                T3 = T * T * T
                qgb = Ab * (p.h_gas_bed * (T - Tb) + SIG * egb * (T3 * T - Tb**4)) * 1e-3
                qgw = Aw * (p.h_gas_wall * (T - Tw) + SIG * egw * (T3 * T - Tw**4)) * 1e-3
                f -= qgb + qgw
                df -= (Ab * (p.h_gas_bed + 4 * SIG * egb * T3) + Aw * (p.h_gas_wall + 4 * SIG * egw * T3)) * 1e-3
                dT = f / df
                dT = max(-300.0, min(300.0, dT))
                T = min(max(T - dT, 300.0), 3200.0)
                if abs(dT) < 1e-4:
                    break
            T_g[j] = T
            Qgb[j], Qgw[j] = qgb, qgw
            # thermal NO (Zeldovich, equilibrium O) using cell gas temperature
            tot = g[:6].sum()
            if T > 1500.0 and tot > 0:
                c = th.P_ATM / (th.R_GAS * T)  # mol/m3
                cO2 = g[G["O2"]] / tot * c
                cN2 = g[G["N2"]] / tot * c
                cO = 36.64 * math.sqrt(T) * math.sqrt(max(cO2, 0.0)) * math.exp(-27123.0 / T)
                k1 = 1.8e8 * math.exp(-38370.0 / T)
                rate = 2.0 * k1 * cO * cN2  # mol/m3/s
                # approach to equilibrium N2 + O2 <=> 2 NO  (Kp = 21.9 exp(-21650/T))
                x_no = g[G["NO"]] / tot
                x_eq = math.sqrt(21.9 * math.exp(-21650.0 / T) * max(cO2 / c, 0.0) * (cN2 / c))
                rate *= max(0.0, 1.0 - (x_no / x_eq) ** 2) if x_eq > 0 else 0.0
                dno = p.no_factor * rate * V_cell / 1000.0  # kmol/s
                dno = min(dno, 0.2 * g[G["O2"]], max(x_eq * tot - g[G["NO"]], 0.0))
                g[G["NO"]] += dno
                g[G["O2"]] -= 0.5 * dno
                g[G["N2"]] -= 0.5 * dno
            # outgoing enthalpy flow to next cell
            H_flow = float(g @ th.h_gas(T, hf))
            o2_profile[j] = g[G["O2"]] / tot if tot > 0 else 0.0

        self.T_g = T_g
        gas_out = g
        gas_out_T = T_g[0]

        # ---------------- 5. wall & shell ---------------------------------
        e_wb = 1.0 / (1.0 / p.eps_wall + 1.0 / p.eps_bed - 1.0)
        Qwb = sb * A_b * SIG * e_wb * (1.0 - eps_g_arr) * (T_w**4 - T_b**4) * 1e-3
        Qcwb = sb * A_wc * p.h_wall_bed * (T_w - T_b) * 1e-3
        self.eps_g_prev = eps_g_arr
        T_sh = self._shell_temperature(T_w, T_amb)
        Qsh = self.A_shell * (T_w - T_sh) / (self.R_wall * self.coating_factor) * 1e-3
        self.T_w = T_w + dt * (Qgw - Qwb - Qcwb - Qsh) / self.C_w
        self.T_sh = T_sh

        # ---------------- 6. bed energy -----------------------------------
        U = U + dt * (Qgb + Qwb + Qcwb)
        if ash_rate.any():
            n = n + ash_rate * dt
            U = U + dt * np.einsum("ij,ij->i", ash_rate, th.h_solid(T_g))
        T_b = th.solve_T_solid(n, U, T_b)
        # empty cells (< 1 g) carry no heat: report the brick surface temperature
        T_b = np.where(n @ th.M_SOLID < 1e-3, self.T_w, T_b)
        self.T_b = T_b
        self.n, self.U = n, U

        # ---------------- 7. outputs -------------------------------------
        liq = th.f_liquid(self.T_b)
        beta = np.radians(p.repose_deg + 10.0 * liq)
        r_cg = 4.0 * self.R * np.sin(theta / 2) ** 3 / (3.0 * np.maximum(theta - np.sin(theta), 1e-9))
        torque = float(np.sum(mass * 9.81 * r_cg * np.sin(beta)))  # N m
        omega = 2 * math.pi * rpm / 60.0
        drive_kw = (torque * omega / p.drive_eff) / 1000.0 + (p.drive_noload_kw if rpm > 0.02 else 0.0)

        self.out = {
            "gas_out": gas_out,
            "gas_out_T": gas_out_T,
            "clinker_n": clinker_n,
            "clinker_H": clinker_H,
            "Q_shell_kW": float(Qsh.sum()),
            "drive_kW": drive_kw,
            "torque_kNm": torque / 1000.0,
            "fill_pct": float(np.mean(fill) * 100.0),
            "holdup_t": float(mass.sum() / 1000.0),
            "residence_min": self.residence_time(rpm) / 60.0,
            "burnt_profile": burnt_arr,
            "o2_profile": o2_profile,
            "co2_bed_kmol_s": float(co2_rate.sum()),
            "unburnt_fuel_kgs": float(g[G["FUELK"]]),
            "flame_L": Lf,
        }
        return self.out

    # ------------------------------------------------------------------
    def _shell_temperature(self, T_w, T_amb):
        p = self.p
        R = self.R_wall * self.coating_factor
        Ts = np.clip(self.T_sh, T_amb + 1.0, T_w)
        for _ in range(20):
            f = (T_w - Ts) / R - (p.h_shell_conv * (Ts - T_amb) + p.eps_shell * SIG * (Ts**4 - T_amb**4))
            df = -1.0 / R - (p.h_shell_conv + 4 * p.eps_shell * SIG * Ts**3)
            Ts = Ts - f / df
        return np.clip(Ts, T_amb, T_w)

    # ------------------------------------------------------------------
    def state(self):
        return {
            "n": self.n.copy(),
            "U": self.U.copy(),
            "T_b": self.T_b.copy(),
            "T_w": self.T_w.copy(),
            "T_g": self.T_g.copy(),
            "T_sh": self.T_sh.copy(),
            "ring_factor": self.ring_factor,
            "coating_factor": self.coating_factor.copy(),
            "out": dict(self.out),
        }

    def load(self, st):
        for k, v in st.items():
            if k == "out":
                self.out = dict(v)
            else:
                setattr(self, k, np.array(v, copy=True) if isinstance(v, np.ndarray) else v)
