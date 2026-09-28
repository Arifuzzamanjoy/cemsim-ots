"""Kiln-line process model: couples draught, cooler, kiln and preheater.

Coupling scheme per time step (explicit, one-step lag between units, dt<=2 s,
far below the fastest unit time constant of ~30 s):

    draught network  ->  flows / pressures
    cooler           <-  clinker from kiln (previous step), air flows
    kiln             <-  hot meal (previous step), secondary air, primary air, coal
    preheater+CAL    <-  kiln gas + seal false air, tertiary air, calciner coal, feed

A running account of every mass and enthalpy stream crossing the plant
boundary is kept in `self.acc` so that tests can verify closure:
    in - out - accumulation = 0.
"""

from __future__ import annotations

import copy

import numpy as np

from . import thermo as th
from .chemistry import RawMix, degree_of_calcination, moduli, phase_fractions
from .fuels import Fuel
from .params import PlantParams
from .units.cooler import Cooler
from .units.draught import Draught
from .units.kiln import Kiln
from .units.preheater import Preheater

G, S = th.G, th.S
PH_FALSE_SPLIT = np.array([0.30, 0.25, 0.20, 0.15, 0.10])


class Plant:
    def __init__(self, params: PlantParams | None = None):
        self.P = params or PlantParams()
        self.kiln = Kiln(self.P.kiln)
        self.ph = Preheater(self.P.ph)
        self.cooler = Cooler(self.P.cooler)
        self.dr = Draught(self.P.draught)
        self.rawmix = RawMix()
        self.fuel_k = Fuel(name="kiln coal")
        self.fuel_c = Fuel(name="calciner coal")
        self.T_amb = self.P.nominal.T_amb
        self.t = 0.0
        # inter-unit buffers (previous-step values)
        self.clk_n = np.zeros(th.NS)
        self.clk_H = 0.0
        self.hot_meal = np.zeros(th.NS)
        self.hot_meal_H = 0.0
        self.m_gen = 20.0
        self.exit_gas = th.air(55.0)
        self.T_exit = 600.0
        self.p_x = -5000.0
        self.clinker_ema = 34.7
        self.kpi = {}
        self.acc = {"m_in": 0.0, "m_out": 0.0, "H_in": 0.0, "H_out": 0.0}
        self._init_state()

    # ------------------------------------------------------------------
    def _init_state(self):
        """Hot operating-like profile; exact steady state is reached by simulation.

        Kiln bed: design holdup of meal pre-reacted at the local temperature
        (so no unphysical flash-calcination at t=0).  Cooler: design bed depth.
        """
        from .chemistry import react

        k = self.kiln
        k.init_profile(1150.0, 1720.0, 1250.0, 1600.0)
        meal = self.rawmix.meal_kmol_per_kg()
        meal[S["H2O_l"]] = 0.0
        meal_per_cell = 34.7 * 1.55 * k.residence_time(self.P.nominal.kiln_rpm) / k.N
        for j in range(k.N):
            nj, _ = react(meal * meal_per_cell, k.T_b[j], 1800.0, 1.0)
            k.n[j] = nj
        k.U = th.solid_enthalpy(k.n, k.T_b)
        ph = self.ph
        ph.T = np.array([620.0, 800.0, 950.0, 1060.0, 1150.0])
        ph.U = th.solid_enthalpy(ph.n, ph.T) + ph.C * (ph.T - th.T0)
        c = self.cooler
        c.T = 380.0 + (1450.0 - 380.0) * np.exp(-np.linspace(0.0, 3.0, c.NC))
        clk, _ = react(meal, 1723.0, 1e5, 1.0)
        clk_per_kg = clk / float(clk @ th.M_SOLID)
        m_comp = self.P.cooler.bed_design * self.P.cooler.rho_bulk * self.P.cooler.grate_width * c.Lk
        c.n = np.tile(clk_per_kg * m_comp, (c.NC, 1))
        c.U = th.solid_enthalpy(c.n, c.T)

    def hf(self):
        return (self.fuel_k.hf(), self.fuel_c.hf())

    # ------------------------------------------------------------------
    def step(self, dt, act: dict):
        """Advance the process by dt seconds with actuator values `act`."""
        P, hf, Tam = self.P, self.hf(), self.T_amb
        nom = P.nominal
        m_pa = nom.primary_air_kgs * act["pa_pct"] / 100.0
        fan = np.asarray(act["cooler_fan_pct"], float)
        m_cool_each = np.array(P.cooler.air_design) * fan / 100.0
        m_cool_each = np.where(self.cooler.fan_fault, 0.0, m_cool_each)
        coal_k = act["coal_kiln_tph"] / 3.6
        coal_c = act["coal_cal_tph"] / 3.6
        feed_kgs = act["kiln_feed_tph"] / 3.6

        # ---------------- draught ------------------------------------
        rho_exit = th.gas_density(self.exit_gas, self.T_exit, th.P_ATM + self.p_x)
        u = {
            "T_kiln_mean": float(np.mean(self.kiln.T_g)),
            "m_pa": m_pa,
            "tad": act["tad_pct"],
            "T_ta": max(self.cooler.out.get("T_ter", 1100.0), 400.0),
            "vent": act["vent_pct"],
            "m_cool": float(m_cool_each.sum()),
            "m_gen": self.m_gen,
            "T_ph_mean": float(np.mean(self.ph.T)),
            "id": act["id_pct"],
            "rho_exit": rho_exit,
            "p_x_prev": self.p_x,
        }
        fl = self.dr.solve(u)
        self.p_x = fl["p_x"]
        m_sec = max(fl["m_sec"], 0.0)
        m_ta = max(fl["m_ta"], 0.0)

        # ---------------- cooler ------------------------------------
        co = self.cooler.step(dt, self.clk_n, self.clk_H, fan, act["grate_spm"], m_sec, m_ta, fl["m_leak"], Tam, hf)

        # ---------------- kiln --------------------------------------
        sec = th.air(co["sec_supplied"])
        pa = th.air(m_pa)
        gas_in = sec + pa
        gas_in[G["FUELK"]] += coal_k
        gas_in_H = (
            co["H_sec"] + float(pa @ th.h_gas(nom.T_primary, hf)) + coal_k * float(th.h_gas(nom.T_coal, hf)[G["FUELK"]])
        )
        mom = max(act["pa_pct"], 5.0) / nom.primary_air_pct
        flame_factor = mom**-0.5 * (1400.0 / max(co["T_sec"], 500.0)) ** 0.5
        flame_factor *= act.get("flame_shape", 1.0)
        rpm = act["kiln_rpm"]
        ko = self.kiln.step(
            dt, self.hot_meal, self.hot_meal_H, gas_in, gas_in_H, self.fuel_k, hf, rpm, flame_factor, Tam
        )

        # ---------------- preheater / calciner -----------------------
        meal_per_kg = self.rawmix.meal_kmol_per_kg()
        feed = meal_per_kg * feed_kgs
        seal = th.air(fl["m_seal"])
        kg = ko["gas_out"] + seal
        kg_H = float(ko["gas_out"] @ th.h_gas(ko["gas_out_T"], hf)) + float(seal @ th.h_gas(Tam, hf))
        T_kg = th.solve_T_gas(kg, kg_H, ko["gas_out_T"], hf)
        ta = th.air(co["ter_supplied"])
        fa = PH_FALSE_SPLIT * fl["m_fa_ph"]
        po = self.ph.step(
            dt,
            feed,
            P.nominal.T_feed,
            kg,
            T_kg,
            ta,
            co["T_ter"],
            coal_c,
            nom.T_coal,
            fa,
            self.fuel_c,
            self.fuel_k,
            hf,
            Tam,
        )

        # ---------------- buffers -----------------------------------
        self.clk_n, self.clk_H = ko["clinker_n"], ko["clinker_H"]
        self.hot_meal, self.hot_meal_H = po["hot_meal"], po["hot_meal_H"]
        self.exit_gas, self.T_exit = po["exit_gas"], po["exit_T"]
        # gas generated inside the line (CO2, H2O, fuel volatiles) = exit gas - air
        # actually admitted; first-order filtered (tau 10 s) to break the
        # algebraic loop with the draught solution (physically: duct volumes).
        air_in = co["sec_supplied"] + m_pa + co["ter_supplied"] + fl["m_seal"] + fl["m_fa_ph"]
        gen = float(np.clip(th.gas_mass(self.exit_gas) - air_in, 0.0, 80.0))
        self.m_gen += (gen - self.m_gen) * min(1.0, dt / 10.0)

        # ---------------- boundary accounting -----------------------
        m_air_amb = float(m_cool_each.sum()) + max(fl["m_leak"], 0.0) + fl["m_seal"] + fl["m_fa_ph"] + m_pa
        air_unit = th.air(1.0)
        h_air_amb = float(air_unit @ th.h_gas(Tam, hf))
        m_in = feed_kgs + coal_k + coal_c + m_air_amb
        H_in = (
            float(feed @ th.h_solid(nom.T_feed))
            + coal_k * float(th.h_gas(nom.T_coal, hf)[G["FUELK"]])
            + coal_c * float(th.h_gas(nom.T_coal, hf)[G["FUELC"]])
            + (m_air_amb - m_pa) * h_air_amb
            + float(pa @ th.h_gas(nom.T_primary, hf))
        )
        clk_out_m = float(co["clinker_out"] @ th.M_SOLID)
        m_out = (
            th.gas_mass(self.exit_gas)
            + float(po["dust"] @ th.M_SOLID)
            + clk_out_m
            + co["m_vent_pool"]
            + max(-fl["m_leak"], 0.0)
        )
        puff_H = 0.0
        if fl["m_leak"] < 0:
            puff_H = -fl["m_leak"] * float(air_unit @ th.h_gas(co["T_ter"], hf))
        H_out = (
            float(self.exit_gas @ th.h_gas(self.T_exit, hf))
            + po["dust_H"]
            + co["clinker_out_H"]
            + co["H_vent"]
            + puff_H
            + ko["Q_shell_kW"]
            + po["shell_loss_kW"]
            + co["loss_kW"]
        )
        self.streams = {
            "feed": (feed, nom.T_feed),
            "coal_k": (coal_k, nom.T_coal),
            "coal_c": (coal_c, nom.T_coal),
            "air_amb": (m_air_amb - m_pa, Tam),
            "air_pa": (m_pa, nom.T_primary),
            "exit_gas": (self.exit_gas.copy(), self.T_exit),
            "dust": (po["dust"].copy(), self.T_exit),
            "clinker": (co["clinker_out"].copy(), co["T_clinker_out"]),
            "vent": (co["m_vent_pool"], co["T_vent"], co["H_vent"]),
            "puff": (max(-fl["m_leak"], 0.0), co["T_ter"], puff_H),
            "loss_kiln": ko["Q_shell_kW"],
            "loss_ph": po["shell_loss_kW"],
            "loss_cooler": co["loss_kW"],
            "H_in": H_in,
            "H_out": H_out,
        }
        self.acc["m_in"] += m_in * dt
        self.acc["m_out"] += m_out * dt
        self.acc["H_in"] += H_in * dt
        self.acc["H_out"] += H_out * dt
        self.t += dt

        # ---------------- KPIs --------------------------------------
        self._kpis(dt, act, fl, co, ko, po, T_kg, kg, coal_k, coal_c, feed_kgs, m_pa)
        return self.kpi

    # ------------------------------------------------------------------
    def holdup(self):
        """Total mass (kg) and absolute enthalpy (kJ) stored in the plant."""
        m = float(
            (self.kiln.n @ th.M_SOLID).sum()
            + (self.ph.n @ th.M_SOLID).sum()
            + (self.cooler.n @ th.M_SOLID).sum()
            + (self.ph.cone_buildup @ th.M_SOLID).sum()
        )
        H = (
            float(self.kiln.U.sum())
            + float(self.kiln.C_w * (self.kiln.T_w - th.T0).sum())
            + float(self.ph.U.sum())
            + float(self.cooler.U.sum())
            + float(self.ph.cone_buildup_H.sum())
        )
        # the inter-unit buffers are "in flight" for one step
        return m, H

    # ------------------------------------------------------------------
    def _kpis(self, dt, act, fl, co, ko, po, T_kg, kg, coal_k, coal_c, feed_kgs, m_pa):
        k, ph = self.kiln, self.ph
        clk_m = float(co["clinker_out"] @ th.M_SOLID)
        kiln_clk_m = float(ko["clinker_n"] @ th.M_SOLID)
        a = dt / 900.0
        self.clinker_ema += a * (kiln_clk_m - self.clinker_ema)
        heat_kw = coal_k * self.fuel_k.LHV + coal_c * self.fuel_c.LHV
        cl_ref = max(self.clinker_ema, 1e-3)
        x_k = th.mole_fractions(kg, wet=False)
        x_x = th.mole_fractions(self.exit_gas, wet=False)
        cell_mass = k.n @ th.M_SOLID
        w_bed = np.clip(cell_mass / 500.0, 0.0, 1.0)
        t_seen = w_bed * k.T_b + (1.0 - w_bed) * k.T_w
        exit_n = k.n[-1]
        pf = phase_fractions(exit_n)
        hm = ph.n[-1]
        stage_dp = np.linspace(fl["p_c"], fl["p_x"], 6)[1:][::-1]  # S1..CAL approx
        kp = {
            "t": self.t,
            # production
            "kiln_feed_tph": feed_kgs * 3.6,
            "clinker_tph": kiln_clk_m * 3.6,
            "clinker_cooler_tph": clk_m * 3.6,
            "clinker_tph_avg": self.clinker_ema * 3.6,
            "heat_MW": heat_kw / 1000.0,
            "spec_heat_kJkg": heat_kw / cl_ref if self.clinker_ema > 1.0 else 0.0,
            "coal_kiln_tph": coal_k * 3.6,
            "coal_cal_tph": coal_c * 3.6,
            "main_burner_share": coal_k / max(coal_k + coal_c, 1e-9) * 100.0,
            # kiln
            # pyrometer: sees the material where there is a bed, else the
            # brick/coating surface (an empty kiln must not read an overheated trace)
            "T_bz": float(np.max(t_seen)) - 273.15,
            "T_bz_pos_m": float(k.x_mid[int(np.argmax(t_seen))]),
            "T_bed_max": float(np.max(k.T_b)) - 273.15,
            "T_gas_max": float(np.max(k.T_g)) - 273.15,
            "T_clinker_kiln_out": float(k.T_b[-1]) - 273.15,
            "T_kiln_inlet": T_kg - 273.15,
            "O2_kiln_inlet": x_k[G["O2"]] * 100.0,
            "CO_kiln_inlet_ppm": x_k[G["CO"]] * 1e6,
            "NO_kiln_inlet_ppm": x_k[G["NO"]] * 1e6,
            "p_kiln_inlet": fl["p_c"] / 100.0,
            "kiln_rpm": act["kiln_rpm"],
            "kiln_drive_kW": ko["drive_kW"],
            "kiln_fill_pct": ko["fill_pct"],
            "kiln_holdup_t": ko["holdup_t"],
            "kiln_residence_min": ko["residence_min"],
            "flame_len_m": ko["flame_L"],
            "shell_max_C": float(np.max(k.T_sh)) - 273.15,
            "free_lime_pct": pf.get("freeCaO", 0.0) * 100.0,
            "C3S_pct": pf.get("C3S", 0.0) * 100.0,
            "C2S_pct": pf.get("C2S", 0.0) * 100.0,
            "C3A_pct": pf.get("C3A", 0.0) * 100.0,
            "C4AF_pct": pf.get("C4AF", 0.0) * 100.0,
            "LSF_clinker": moduli(exit_n)["LSF"] if exit_n.sum() > 0 else 0.0,
            # preheater / calciner
            "T_calciner": float(ph.T[4]) - 273.15,
            "T_S1": float(ph.T[0]) - 273.15,
            "T_S2": float(ph.T[1]) - 273.15,
            "T_S3": float(ph.T[2]) - 273.15,
            "T_S4": float(ph.T[3]) - 273.15,
            "p_S1": stage_dp[0] / 100,
            "p_S2": stage_dp[1] / 100,
            "p_S3": stage_dp[2] / 100,
            "p_S4": stage_dp[3] / 100,
            "p_CAL": stage_dp[4] / 100,
            "hot_meal_doc_pct": degree_of_calcination(hm) * 100.0 if hm.sum() > 0 else 0.0,
            "T_ph_exit": self.T_exit - 273.15,
            "O2_ph_exit": x_x[G["O2"]] * 100.0,
            "CO_ph_exit_ppm": x_x[G["CO"]] * 1e6,
            "NO_ph_exit_ppm": x_x[G["NO"]] * 1e6,
            "CO2_ph_exit": x_x[G["CO2"]] * 100.0,
            "p_ph_exit": fl["p_x"] / 100.0,
            "ph_gas_kgs": th.gas_mass(self.exit_gas),
            "ph_gas_Nm3h": float(self.exit_gas[:6].sum()) * 22.414 * 3600.0,
            "dust_tph": float(po["dust"] @ th.M_SOLID) * 3.6,
            "unburnt_coal_kgs": po["unburnt_kgs"],
            "cone_buildup_t": float((ph.cone_buildup @ th.M_SOLID).sum()) / 1000.0,
            # air / draught
            "p_hood": fl["p_h"] / 100.0,
            "m_sec": fl["m_sec"],
            "m_ta": fl["m_ta"],
            "m_vent": fl["m_vent"],
            "m_leak_hood": fl["m_leak"],
            "m_seal": fl["m_seal"],
            "m_pa": m_pa,
            "id_fan_kW": fl["id_kW"],
            "id_fan_Qm3h": fl["Q_id"] * 3600.0,
            "draught_resid": fl["resid"],
            # cooler
            "T_sec_air": co["T_sec"] - 273.15,
            "T_ter_air": co["T_ter"] - 273.15,
            "T_vent": co["T_vent"] - 273.15,
            "T_clinker_out": co["T_clinker_out"] - 273.15,
            "dp_undergrate": co["dp_ug"] / 100.0,
            "cooler_bed_m": float(co["bed_m"][0]),
            "cooler_air_kgs": float(np.sum(co["m_air"])),
            "cooler_air_Nm3kg": float(np.sum(co["m_air"])) / 1.293 / max(self.clinker_ema, 1e-3),
        }
        self.kpi = kp

    # ------------------------------------------------------------------
    def profiles(self):
        k = self.kiln
        return {
            "x": k.x_mid.tolist(),
            "T_bed": (k.T_b - 273.15).tolist(),
            "T_gas": (k.T_g - 273.15).tolist(),
            "T_wall": (k.T_w - 273.15).tolist(),
            "T_shell": (k.T_sh - 273.15).tolist(),
            "burnt": k.out.get("burnt_profile", np.zeros(k.N)).tolist(),
            "cooler_T": (self.cooler.T - 273.15).tolist(),
            "ph_T": (self.ph.T - 273.15).tolist(),
        }

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        return copy.deepcopy(
            {
                "t": self.t,
                "kiln": self.kiln.state(),
                "ph": self.ph.state(),
                "cooler": self.cooler.state(),
                "dr": self.dr.state(),
                "buf": {
                    "clk_n": self.clk_n,
                    "clk_H": self.clk_H,
                    "hot_meal": self.hot_meal,
                    "hot_meal_H": self.hot_meal_H,
                    "m_gen": self.m_gen,
                    "exit_gas": self.exit_gas,
                    "T_exit": self.T_exit,
                    "p_x": self.p_x,
                    "clinker_ema": self.clinker_ema,
                },
                "rawmix": vars(self.rawmix),
                "fuel_k": vars(self.fuel_k),
                "fuel_c": vars(self.fuel_c),
                "T_amb": self.T_amb,
            }
        )

    def restore(self, snap: dict):
        snap = copy.deepcopy(snap)
        self.t = snap["t"]
        self.kiln.load(snap["kiln"])
        self.ph.load(snap["ph"])
        self.cooler.load(snap["cooler"])
        self.dr.load(snap["dr"])
        for k, v in snap["buf"].items():
            setattr(self, k, v)
        self.rawmix = RawMix(**snap["rawmix"])
        self.fuel_k = Fuel(**snap["fuel_k"])
        self.fuel_c = Fuel(**snap["fuel_c"])
        self.T_amb = snap["T_amb"]
        self.acc = {"m_in": 0.0, "m_out": 0.0, "H_in": 0.0, "H_out": 0.0}


def nominal_actuators(P: PlantParams | None = None) -> dict:
    n = (P or PlantParams()).nominal
    return {
        "kiln_feed_tph": n.kiln_feed_tph,
        "coal_kiln_tph": n.coal_kiln_tph,
        "coal_cal_tph": n.coal_cal_tph,
        "kiln_rpm": n.kiln_rpm,
        "id_pct": n.id_fan_pct,
        "vent_pct": n.vent_fan_pct,
        "pa_pct": n.primary_air_pct,
        "tad_pct": n.tad_pct,
        "cooler_fan_pct": list(n.cooler_fan_pct),
        "grate_spm": n.grate_spm,
    }


def heat_balance(plant: Plant) -> dict:
    """Kiln-line heat balance in kJ per kg clinker (25 C reference, LHV basis).

    Built from the actual boundary streams of the last step, so at steady state
    it closes exactly (closure == accumulation in brick/holdup).  Every stream
    enthalpy is split into chemical (value at 25 C) and sensible parts:
        theoretical = LHV_burnt - [sum_in H(25C) - sum_out H(25C)]
    i.e. the chemical enthalpy absorbed by the material (clinkering, incl.
    ash reactions, moisture evaporation, dust formation) - rigorous, not a
    textbook correlation.
    """
    st = getattr(plant, "streams", None)
    if not st:
        return {}
    hf, T0 = plant.hf(), th.T0
    au = th.air(1.0)
    cl = max(plant.clinker_ema, 1e-6)

    def Hs(n, T):  # solids stream: (chemical, sensible) kW
        return float(n @ th.h_solid(T0)), float(n @ (th.h_solid(T) - th.h_solid(T0)))

    def Hg(g, T):
        return float(g @ th.h_gas(T0, hf)), float(g @ (th.h_gas(T, hf) - th.h_gas(T0, hf)))

    fk, fc = plant.fuel_k, plant.fuel_c
    feed_c, feed_s = Hs(*st["feed"])
    coal_k, Tck = st["coal_k"]
    coal_c, _ = st["coal_c"]
    coal_chem = coal_k * hf[0] + coal_c * hf[1]
    coal_sens = (coal_k + coal_c) * th.FUEL_CP * (Tck - T0)
    air_amb_s = st["air_amb"][0] * float(au @ (th.h_gas(st["air_amb"][1], hf) - th.h_gas(T0, hf)))
    pa_s = st["air_pa"][0] * float(au @ (th.h_gas(st["air_pa"][1], hf) - th.h_gas(T0, hf)))
    ex_c, ex_s = Hg(*st["exit_gas"])
    du_c, du_s = Hs(*st["dust"])
    ck_c, ck_s = Hs(*st["clinker"])
    vent_s = st["vent"][2] - st["vent"][0] * float(au @ th.h_gas(T0, hf))
    puff_s = st["puff"][2] - st["puff"][0] * float(au @ th.h_gas(T0, hf))
    eg = st["exit_gas"][0]
    unburnt = eg[G["FUELK"]] * fk.LHV + eg[G["FUELC"]] * fc.LHV + eg[G["CO"]] * 282990.0
    lhv = coal_k * fk.LHV + coal_c * fc.LHV
    chem_in = feed_c + coal_chem
    chem_out = ex_c + du_c + ck_c  # air chemical enthalpy is zero
    theo = lhv - unburnt - (chem_in - chem_out)
    out = {
        "fuel": lhv / cl,
        "sensible_inputs": (feed_s + coal_sens + air_amb_s + pa_s) / cl,
        "theoretical": theo / cl,
        "exit_gas": ex_s / cl,
        "vent_air": (vent_s + puff_s) / cl,
        "clinker": ck_s / cl,
        "dust": du_s / cl,
        "shell_kiln": st["loss_kiln"] / cl,
        "shell_preheater": st["loss_ph"] / cl,
        "shell_cooler": st["loss_cooler"] / cl,
        "unburnt_CO": unburnt / cl,
    }
    outs = (
        "theoretical",
        "exit_gas",
        "vent_air",
        "clinker",
        "dust",
        "shell_kiln",
        "shell_preheater",
        "shell_cooler",
        "unburnt_CO",
    )
    out["sum_out"] = sum(out[k] for k in outs)
    out["closure"] = out["fuel"] + out["sensible_inputs"] - out["sum_out"]
    return out
