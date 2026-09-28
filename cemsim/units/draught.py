"""Gas draught / pressure network of the kiln line.

Nodes: hood (p_h), kiln inlet = calciner bottom (p_c), preheater exit (p_x);
all gauge pressures in Pa.  Flow branches (turbulent, dp = R m^2 / rho, and
rho ~ 1/T, hence dp = R' m^2 T/1000):

  kiln      : p_h - p_c = R_k  (m_sec + m_pa)^2  T_k/1000
  TA duct   : p_h - p_c = R_t / phi(TAD)^2 m_ta^2 T_t/1000,  phi = (pos/100)^1.5 + 0.02
  preheater : p_c - p_x = R_ph m_ph^2 T_ph/1000
  ID fan    : -p_x = (rho/rho_d) (a n^2 - b Q^2),  Q = m_ph / rho    (fan laws)
  vent fan  : p_h + a_v n_v^2 = (R_v + b_v) m_v^2                  (closed form)
  hood leak : m_leak = C sign(-p_h) sqrt|p_h|   (+ = ambient in-leak)
  seal/false air at kiln inlet and preheater ~ C sqrt(-p)

Unknowns (p_h, p_c) solved each step by damped 2-D Newton from the previous
solution:  hood mass balance  and  ID fan operating point.
All resistances and fan constants are back-calculated from the design point in
DraughtParams so the network reproduces it exactly.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.optimize import brentq

from ..params import DraughtParams


def _sqrt_signed(x):
    return math.copysign(math.sqrt(abs(x)), x)


class Draught:
    def __init__(self, d: DraughtParams):
        self.d = d
        # --- back-calculate resistances at design ---
        dpk = d.p_hood - d.p_kiln_inlet
        self.R_k = dpk / (d.m_kiln_air**2 * d.T_kiln_mean / 1000.0)
        phi_d = (d.tad_design / 100.0) ** 1.5 + 0.02
        self.R_t = dpk * phi_d**2 / (d.m_ta**2 * d.T_ta / 1000.0)
        self.R_ph = (d.p_kiln_inlet - d.p_ph_exit) / (d.m_ph**2 * d.T_ph_mean / 1000.0)
        # ID fan:  -p_x = a n^2 - b Q^2  at design density rho_d
        self.rho_d = 0.60
        Qd = d.m_ph / self.rho_d
        nd = d.id_speed_design / 100.0
        dp_d = -d.p_ph_exit
        dp_shut = d.id_shutoff_ratio * dp_d
        self.a_id = dp_shut / nd**2
        self.b_id = (dp_shut - dp_d) / Qd**2
        # vent fan (mass based, near-constant density)
        nv = d.vent_speed_design / 100.0
        dpv = d.vent_dp_design
        self.a_v = d.vent_shutoff_ratio * dpv / nv**2
        self.b_v = (d.vent_shutoff_ratio * dpv - dpv) / d.m_vent**2
        self.R_v = (dpv + d.p_hood) / d.m_vent**2
        self.p_h, self.p_c = d.p_hood, d.p_kiln_inlet
        self.seal_mult = 1.0  # disturbance: worn kiln inlet seal
        self.ph_false_mult = 1.0
        self.ph_R_mult = 1.0  # disturbance: cyclone blockage / build-up
        self.kiln_R_mult = 1.0  # disturbance: kiln ring
        self.out = {}

    # ------------------------------------------------------------------
    def flows(self, p_h, p_c, u):
        d = self.d
        Tk = u["T_kiln_mean"]
        dp = p_h - p_c
        m_kiln = _sqrt_signed(dp / (self.R_k * self.kiln_R_mult * Tk / 1000.0))
        m_sec = m_kiln - u["m_pa"]
        phi = (max(u["tad"], 0.0) / 100.0) ** 1.5 + 0.02
        m_ta = _sqrt_signed(dp * phi**2 / (self.R_t * u["T_ta"] / 1000.0))
        nv = u["vent"] / 100.0
        m_v = math.sqrt(max(0.0, p_h + self.a_v * nv * nv) / (self.R_v + self.b_v)) if nv > 0 else 0.0
        m_leak = d.hood_leak_C * _sqrt_signed(-p_h)
        m_seal = d.inlet_seal_C * self.seal_mult * math.sqrt(max(-p_c, 0.0))
        p_mid = 0.5 * (p_c + u.get("p_x_prev", -3000.0))
        m_fa_ph = d.ph_false_air_C * self.ph_false_mult * math.sqrt(max(-p_mid, 0.0))
        m_ph = m_kiln + m_ta + m_seal + m_fa_ph + u["m_gen"]
        p_x = p_c - self.R_ph * self.ph_R_mult * m_ph * abs(m_ph) * u["T_ph_mean"] / 1000.0
        return {
            "m_kiln": m_kiln,
            "m_sec": m_sec,
            "m_ta": m_ta,
            "m_vent": m_v,
            "m_leak": m_leak,
            "m_seal": m_seal,
            "m_fa_ph": m_fa_ph,
            "m_ph": m_ph,
            "p_x": p_x,
        }

    def residuals(self, x, u):
        p_h, p_c = x
        f = self.flows(p_h, p_c, u)
        r1 = u["m_cool"] + f["m_leak"] - f["m_sec"] - f["m_ta"] - f["m_vent"]
        # ID fan
        n = u["id"] / 100.0
        rho = u["rho_exit"]
        Q = max(f["m_ph"], 0.0) / rho
        dp_fan = (rho / self.rho_d) * (self.a_id * n * n - self.b_id * Q * Q) if n > 0 else 0.0
        # when the fan is stopped the system becomes stagnant (natural draught ignored)
        r2 = (-f["p_x"] - dp_fan) / 1000.0
        return np.array([r1, r2]), f

    def solve(self, u):
        """u: dict of boundary conditions. Returns flow/pressure dict."""
        x = np.array([self.p_h, self.p_c], float)
        if not np.all(np.isfinite(x)) or abs(x[0]) > 5000 or abs(x[1]) > 20000:
            x = np.array([self.d.p_hood, self.d.p_kiln_inlet], float)
        for _ in range(40):
            r, f = self.residuals(x, u)
            if abs(r[0]) < 1e-5 and abs(r[1]) < 1e-5:
                break
            J = np.zeros((2, 2))
            for k, h in enumerate((1.0, 1.0)):
                xp = x.copy()
                xp[k] += h
                J[:, k] = (self.residuals(xp, u)[0] - r) / h
            try:
                dx = np.linalg.solve(J, -r)
            except np.linalg.LinAlgError:
                dx = np.array([-r[0] * 10.0, -r[1] * 100.0])
            step = np.clip(dx, -3000.0, 3000.0)
            # damping: halve until residual decreases
            lam = 1.0
            nr = np.linalg.norm(r)
            for _ in range(8):
                xt = x + lam * step
                if np.linalg.norm(self.residuals(xt, u)[0]) < nr:
                    break
                lam *= 0.5
            x = x + lam * step
            x[0] = min(max(x[0], -5000.0), 5000.0)
            x[1] = min(max(x[1], -20000.0), 5000.0)
        r, f = self.residuals(x, u)
        if not (np.all(np.isfinite(r)) and abs(r[0]) < 1e-4 and abs(r[1]) < 1e-4):
            # Newton failed (typically near zero flow, where dm/dp of the sqrt law is
            # unbounded).  Both residuals are monotone in their own pressure, so a
            # nested bracketing solve (Brent) is guaranteed to converge.
            x = self._bracket_solve(u)
            r, f = self.residuals(x, u)
        self.p_h, self.p_c = float(x[0]), float(x[1])
        n = u["id"] / 100.0
        Q = max(f["m_ph"], 0.0) / u["rho_exit"]
        dp_fan = -f["p_x"]
        f.update(
            p_h=self.p_h,
            p_c=self.p_c,
            Q_id=Q,
            id_kW=max(Q * dp_fan, 0.0) / 0.78 / 1000.0 + (30.0 if n > 0 else 0.0),
            vent_kW=max(f["m_vent"] / 1.0 * (self.a_v * (u["vent"] / 100) ** 2 - self.b_v * f["m_vent"] ** 2), 0.0)
            / 0.78
            / 1000.0,
            resid=float(np.linalg.norm(r)),
        )
        self.out = f
        return f

    PH_LIM = (-5000.0, 5000.0)
    PC_LIM = (-20000.0, 5000.0)

    def _brent(self, fun, lo, hi):
        flo, fhi = fun(lo), fun(hi)
        if flo * fhi > 0:  # no sign change: best end point
            return lo if abs(flo) < abs(fhi) else hi
        return brentq(fun, lo, hi, xtol=1e-6, rtol=1e-10, maxiter=100)

    def _bracket_solve(self, u):
        def p_h_for(p_c):  # hood balance, decreasing in p_h
            return self._brent(lambda ph: self.residuals((ph, p_c), u)[0][0], *self.PH_LIM)

        def fan(p_c):  # ID fan balance, decreasing in p_c
            return self.residuals((p_h_for(p_c), p_c), u)[0][1]

        p_c = self._brent(fan, *self.PC_LIM)
        return np.array([p_h_for(p_c), p_c])

    def state(self):
        return {
            "p_h": self.p_h,
            "p_c": self.p_c,
            "seal_mult": self.seal_mult,
            "ph_false_mult": self.ph_false_mult,
            "ph_R_mult": self.ph_R_mult,
            "kiln_R_mult": self.kiln_R_mult,
        }

    def load(self, st):
        for k, v in st.items():
            setattr(self, k, v)
