"""Reciprocating grate clinker cooler: cross-flow, plug-flow cells along the grate.

The grate is divided into n_cells 1 m cells (plug flow of clinker approximated
by tanks in series); the fan compartments are mapped onto those cells.  Each
cell holds a well-mixed clinker bed (species n_c, enthalpy U_c).

Grate transport: v = spm * stroke * eff / 60, outflow fraction per step
1 - exp(-v dt / dx).  Bed height H_c = m_c / (rho_b W dx).

Cross-flow heat exchange (air through the bed):
  eps_c = 1 - exp(-NTU_c),  NTU_c = ntu_per_m * H_c * (m_c / m0_c)^-0.4
  (volumetric h_v ~ G^0.6  =>  NTU = h_v H / (G cp) ~ H G^-0.4)
  T_air,out = T_amb + eps_c (T_c - T_amb)
Undergrate pressure (Ergun, turbulent term dominant): dp ~ H m^1.8.

Hot air is pooled front-to-back: kiln secondary air takes the hottest (front)
air, tertiary air the next, the vent fan the remainder.  Hood in-leak
(negative hood pressure) mixes ambient air into the secondary air;
out-puffing (positive pressure) removes recuperation air.
"""

from __future__ import annotations

import math

import numpy as np

from .. import thermo as th
from ..params import CoolerParams

G = th.G


class Cooler:
    def __init__(self, p: CoolerParams):
        self.p = p
        self.NC = p.n_cells
        self.K = p.n_comp
        self.dx = p.grate_length / self.NC
        x = (np.arange(self.NC) + 0.5) * self.dx
        self.comp_of = np.array([next(k for k, (a, b) in enumerate(p.comp_bounds) if a <= xi < b) for xi in x])
        comp_len = np.array([b - a for a, b in p.comp_bounds], float)
        self.cell_share = self.dx / comp_len[self.comp_of]  # air share per cell
        self.n = np.zeros((self.NC, th.NS))
        self.T = np.linspace(1400.0, 380.0, self.NC)
        self.U = th.solid_enthalpy(self.n, self.T)
        self.fan_fault = np.zeros(self.K, bool)
        self.out = {}
        self._air_unit = th.air(1.0)

    @property
    def Lk(self):
        return self.dx

    def _h_air(self, T, hf):
        return th.h_gas(np.asarray(T, float), hf) @ self._air_unit

    def step(self, dt, clk_n, clk_H, fan_pct, spm, m_sec, m_ter, m_leak, T_amb, hf):
        p = self.p
        n, U = self.n, self.U
        v = spm * p.stroke_m * p.transport_eff / 60.0
        a = 1.0 - math.exp(-v * dt / self.dx) if v > 0 else 0.0
        out = n * a
        outU = U * a
        n = n - out
        U = U - outU
        n[1:] += out[:-1]
        U[1:] += outU[:-1]
        n[0] += clk_n * dt
        U[0] += clk_H * dt
        clinker_out = out[-1] / dt
        clinker_out_H = outU[-1] / dt
        T = th.solve_T_solid(n, U, self.T)

        mass = n @ th.M_SOLID
        H = mass / (p.rho_bulk * p.grate_width * self.dx)
        m_comp = np.array(p.air_design) * np.asarray(fan_pct, float) / 100.0
        m_comp = np.where(self.fan_fault, 0.0, m_comp)
        m0_comp = np.array(p.air_design)
        m_cell = m_comp[self.comp_of] * self.cell_share
        rel = np.maximum(m_comp[self.comp_of] / m0_comp[self.comp_of], 1e-3)
        ntu = np.clip(p.ntu_per_m * H * rel**-0.4, 0.0, 15.0)
        eps = 1.0 - np.exp(-ntu)
        T_air = T_amb + eps * (T - T_amb)
        h_amb = float(self._h_air(T_amb, hf))
        h_out = self._h_air(T_air, hf)
        Q = m_cell * (h_out - h_amb)
        loss = p.ua_loss / self.NC * (T - T_amb)
        U = U - dt * (Q + loss)
        T = th.solve_T_solid(n, U, T)
        self.n, self.U, self.T = n, U, T

        # ---- pool air front to back --------------------------------------
        avail_m = m_cell.copy()

        def take(m_need):
            got, Hsum = 0.0, 0.0
            for c in range(self.NC):
                if got >= m_need:
                    break
                if avail_m[c] <= 0:
                    continue
                x_ = min(avail_m[c], m_need - got)
                got += x_
                Hsum += x_ * h_out[c]
                avail_m[c] -= x_
            return got, Hsum

        m_out_puff = max(0.0, -m_leak)
        m_in_leak = max(0.0, m_leak)
        got_s, H_s = take(max(0.0, m_sec - m_in_leak))
        H_s += m_in_leak * h_amb
        got_s += m_in_leak
        got_t, H_t = take(m_ter)
        if m_out_puff > 0:
            take(m_out_puff)
        m_vent = float(avail_m.sum())
        H_v = float(avail_m @ h_out)
        au = self._air_unit

        def T_of(mm, HH, guess):
            return th.solve_T_gas(au * mm, HH, guess, hf) if mm > 1e-9 else T_amb

        T_sec = T_of(got_s, H_s, 1300.0)
        T_ter = T_of(got_t, H_t, 1100.0)
        T_vent = T_of(m_vent, H_v, 500.0)

        # compartment undergrate pressures
        Hk = np.array([H[self.comp_of == k].mean() for k in range(self.K)])
        dp_ug = p.dp_ug_design * (Hk / p.bed_design) * (m_comp / m0_comp) ** 1.8
        self.out = {
            "T_sec": T_sec,
            "T_ter": T_ter,
            "T_vent": T_vent,
            "m_vent_pool": m_vent,
            "sec_supplied": got_s,
            "ter_supplied": got_t,
            "clinker_out": clinker_out,
            "clinker_out_H": clinker_out_H,
            "T_clinker_out": float(T[-1]),
            "bed_m": Hk,
            "dp_ug": float(dp_ug[0]),
            "dp_ug_all": dp_ug,
            "m_air": m_comp,
            "T_air_cells": T_air,
            "Q_air_kW": float(Q.sum()),
            "loss_kW": float(loss.sum()),
            "holdup_t": float(mass.sum() / 1000.0),
            "H_vent": H_v,
            "H_sec": H_s,
            "H_ter": H_t,
        }
        return self.out

    def state(self):
        return {
            "n": self.n.copy(),
            "U": self.U.copy(),
            "T": self.T.copy(),
            "fan_fault": self.fan_fault.copy(),
            "out": dict(self.out),
        }

    def load(self, st):
        for k, v in st.items():
            setattr(self, k, dict(v) if k == "out" else np.array(v, copy=True))
