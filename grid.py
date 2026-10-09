"""
Core grid model for Use Case 04 (Grid load balancing and unit commitment).

Contains:
  * illustrative AP-style data (generators, demand, solar/wind)   -> ASSUMPTIONS, replace with APTRANSCO/SLDC data
  * QUBO builder for the ON/OFF (unit commitment) decision
  * LP economic dispatch for a fixed schedule (with curtailment / unserved-energy slack)
  * exact classical MILP baseline (HiGHS via scipy) for benchmarking
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, linprog, milp
from scipy.sparse import lil_matrix

# ----------------------------------------------------------------------------------------
# Illustrative data (assumptions - clearly state these in the pitch)
# ----------------------------------------------------------------------------------------
H = 6.0                                           # hours per time block
BLOCKS = ["00-06", "06-12", "12-18", "18-24"]
CARBON = 1000.0                                   # Rs/tCO2 shadow carbon price
VOLL = 50000.0                                    # Rs/MWh value of lost load (unserved energy)
CURT = 100.0                                      # Rs/MWh penalty on curtailed renewable energy
OVER = 50000.0                                    # Rs/MWh penalty on over-generation

GENS = [
    dict(name="Coal-1", pmin=200, pmax=400, fuel=3200, co2=0.95, start=600_000, init=1),
    dict(name="Coal-2", pmin=150, pmax=300, fuel=3600, co2=0.98, start=450_000, init=0),
    dict(name="Gas",    pmin=50,  pmax=200, fuel=6500, co2=0.45, start=80_000,  init=0),
    dict(name="Hydro",  pmin=0,   pmax=150, fuel=1200, co2=0.00, start=20_000,  init=0),
]
DEMAND = np.array([520.0, 700.0, 780.0, 880.0])   # MW per block (AP-style daily shape)
SOLAR_CAP, WIND_CAP = 250.0, 120.0                # MW installed
SOLAR_CF = np.array([0.00, 0.40, 0.55, 0.03])     # average capacity factor per block
WIND_CF = np.array([0.30, 0.25, 0.30, 0.35])

# (solar multiplier, wind multiplier) used to test the schedule under uncertainty
SCENARIOS = {
    "base":   (1.0, 1.0),
    "cloudy": (0.5, 1.0),
    "calm":   (1.0, 0.4),
    "windy":  (0.9, 1.6),
}


class Problem:
    """One day-ahead unit-commitment instance."""

    def __init__(self, demand_scale=1.0, solar_scale=1.0, wind_scale=1.0,
                 gens=None, blocks=None, penalty=2000.0, nominal_frac=0.8, reserve=0.0):
        idx = list(range(len(BLOCKS))) if blocks is None else list(blocks)
        self.gens = list(GENS if gens is None else gens)
        self.G, self.T = len(self.gens), len(idx)
        self.n = self.G * self.T
        self.block_names = [BLOCKS[i] for i in idx]
        self.D = DEMAND[idx] * demand_scale
        self.solar = SOLAR_CAP * SOLAR_CF[idx] * solar_scale
        self.wind = WIND_CAP * WIND_CF[idx] * wind_scale
        self.R = self.solar + self.wind
        self.penalty = penalty
        self.reserve = reserve   # planning margin on demand (spinning reserve) used only in the QUBO
        self.nominal_frac = nominal_frac
        self.mc = np.array([g["fuel"] + CARBON * g["co2"] for g in self.gens])  # Rs/MWh
        self.pmin = np.array([g["pmin"] for g in self.gens], float)
        self.pmax = np.array([g["pmax"] for g in self.gens], float)
        self.start = np.array([g["start"] for g in self.gens], float)
        self.init = np.array([g["init"] for g in self.gens], float)
        self.co2 = np.array([g["co2"] for g in self.gens], float)

    # ---- indexing -------------------------------------------------------------------
    def var(self, i, t):
        """Qubit / binary index of generator i in time block t."""
        return t * self.G + i

    def to_matrix(self, x):
        """bit-vector (length n) -> G x T 0/1 schedule."""
        x = np.asarray(x).astype(int)
        return np.array([[x[self.var(i, t)] for t in range(self.T)] for i in range(self.G)])

    def to_bits(self, u):
        return np.array([u[i][t] for t in range(self.T) for i in range(self.G)], dtype=int)

    # ---- QUBO -----------------------------------------------------------------------
    def build_qubo(self):
        """
        Minimise   sum_{i,t} mc_i * nom_i * H * u_it                 (fuel + carbon at nominal output)
                 + sum_{i,t} start_i * u_it * (1 - u_i,t-1)          (start-up cost, quadratic)
                 + P * sum_t ( sum_i nom_i u_it + R_t - D_t )^2      (supply = demand penalty)
        Returns upper-triangular Q (x^T Q x) and constant c.  nom_i = nominal_frac * pmax_i.
        The exact MW split is fixed afterwards by the LP dispatch.
        """
        n, G, T, P = self.n, self.G, self.T, self.penalty
        nom = self.nominal_frac * self.pmax
        Q = np.zeros((n, n))
        c = 0.0
        for t in range(T):
            for i in range(G):
                k = self.var(i, t)
                Q[k, k] += self.mc[i] * nom[i] * H
                if t == 0:
                    Q[k, k] += self.start[i] * (1.0 - self.init[i])
                else:
                    Q[k, k] += self.start[i]
                    Q[self.var(i, t - 1), k] -= self.start[i]
            resid = self.R[t] - self.D[t] * (1.0 + self.reserve)
            for i in range(G):
                ki = self.var(i, t)
                Q[ki, ki] += P * (nom[i] ** 2 + 2.0 * nom[i] * resid)
                for j in range(i + 1, G):
                    Q[ki, self.var(j, t)] += 2.0 * P * nom[i] * nom[j]
            c += P * resid ** 2
        return Q, c

    def energy_table(self, Q, c):
        """QUBO energy of all 2^n bit-strings (index bit k = qubit k). Feasible for n <= ~20."""
        n = self.n
        idx = np.arange(1 << n, dtype=np.int64)
        X = ((idx[:, None] >> np.arange(n)) & 1).astype(float)
        return ((X @ Q) * X).sum(axis=1) + c

    # ---- dispatch (LP) --------------------------------------------------------------
    def dispatch_block(self, on, t, scen=(1.0, 1.0)):
        """LP for one block with the ON-set fixed. Returns dict with MW per generator."""
        ons = [i for i in range(self.G) if on[i]]
        r_avail = self.solar[t] * scen[0] + self.wind[t] * scen[1]
        m = len(ons)
        # variables: p_i (m), curtail, unserved, over
        c = np.array([self.mc[i] * H for i in ons] + [CURT * H, VOLL * H, OVER * H])
        A_eq = np.array([[1.0] * m + [-1.0, 1.0, -1.0]])
        b_eq = [self.D[t] - r_avail]
        bounds = [(self.pmin[i], self.pmax[i]) for i in ons] + [(0, r_avail), (0, None), (0, None)]
        res = linprog(c, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
        p = np.zeros(self.G)
        if res.status != 0:  # should not happen thanks to slack variables
            return dict(p=p, curt=0.0, unserved=self.D[t], over=0.0, cost=VOLL * H * self.D[t], r_used=0.0)
        for k, i in enumerate(ons):
            p[i] = res.x[k]
        curt, uns, over = res.x[m:m + 3]
        return dict(p=p, curt=curt, unserved=uns, over=over, cost=res.fun, r_used=r_avail - curt)

    def evaluate(self, u, scen=(1.0, 1.0)):
        """Full cost / emissions / curtailment of schedule u (G x T) under a scenario."""
        u = np.asarray(u)
        P_mw = np.zeros((self.G, self.T))
        curt = np.zeros(self.T); uns = np.zeros(self.T); over = np.zeros(self.T); r_used = np.zeros(self.T)
        cost = 0.0
        for t in range(self.T):
            d = self.dispatch_block(u[:, t], t, scen)
            P_mw[:, t] = d["p"]; curt[t] = d["curt"]; uns[t] = d["unserved"]; over[t] = d["over"]
            r_used[t] = d["r_used"]; cost += d["cost"]
        prev = np.concatenate([self.init[:, None], u[:, :-1]], axis=1)
        startups = np.maximum(u - prev, 0)
        cost += float((startups * self.start[:, None]).sum())
        return dict(
            u=u, p=P_mw, cost=cost,
            emissions_t=float((P_mw * self.co2[:, None]).sum() * H),
            curtail_mwh=float(curt.sum() * H),
            curtail_mw=curt, r_used=r_used,
            unserved_mwh=float(uns.sum() * H), over_mwh=float(over.sum() * H),
            startups=int(startups.sum()),
        )

    def repair(self, u):
        """If a block cannot serve demand, switch on the cheapest OFF plant (classical post-processing)."""
        u = np.array(u).copy(); fixes = 0
        for t in range(self.T):
            while True:
                d = self.dispatch_block(u[:, t], t)
                if d["unserved"] <= 1e-6:
                    break
                off = [i for i in range(self.G) if u[i, t] == 0]
                if not off:
                    break
                u[min(off, key=lambda i: self.mc[i]), t] = 1
                fixes += 1
        return u, fixes

    def robust_table(self, u):
        """Cost under each scenario for a fixed schedule (real-time re-dispatch allowed)."""
        return {name: self.evaluate(u, sc) for name, sc in SCENARIOS.items()}

    # ---- exact classical baseline (MILP) -------------------------------------------
    def solve_milp(self, scen=(1.0, 1.0)):
        G, T = self.G, self.T
        nu = G * T
        # layout: u | p | s | curt | uns | over
        o_u, o_p, o_s = 0, nu, 2 * nu
        o_c, o_n, o_o = 3 * nu, 3 * nu + T, 3 * nu + 2 * T
        N = 3 * nu + 3 * T
        ix = lambda i, t: i * T + t
        cost = np.zeros(N)
        for i in range(G):
            for t in range(T):
                cost[o_p + ix(i, t)] = self.mc[i] * H
                cost[o_s + ix(i, t)] = self.start[i]
        for t in range(T):
            cost[o_c + t] = CURT * H; cost[o_n + t] = VOLL * H; cost[o_o + t] = OVER * H
        lb = np.zeros(N); ub = np.full(N, np.inf)
        ub[:nu] = 1; ub[o_s:o_s + nu] = 1
        for t in range(T):
            ub[o_c + t] = self.solar[t] * scen[0] + self.wind[t] * scen[1]
        integrality = np.zeros(N); integrality[:nu] = 1; integrality[o_s:o_s + nu] = 1
        rows = 2 * nu + nu + T
        A = lil_matrix((rows, N)); lo = np.full(rows, -np.inf); hi = np.full(rows, np.inf)
        r = 0
        for i in range(G):
            for t in range(T):
                A[r, o_p + ix(i, t)] = 1; A[r, o_u + ix(i, t)] = -self.pmin[i]; lo[r] = 0; r += 1   # p >= pmin u
                A[r, o_p + ix(i, t)] = 1; A[r, o_u + ix(i, t)] = -self.pmax[i]; hi[r] = 0; r += 1   # p <= pmax u
        for i in range(G):
            for t in range(T):
                A[r, o_s + ix(i, t)] = 1; A[r, o_u + ix(i, t)] = -1
                if t == 0:
                    lo[r] = -self.init[i]
                else:
                    A[r, o_u + ix(i, t - 1)] = 1; lo[r] = 0
                r += 1
        for t in range(T):
            for i in range(G):
                A[r, o_p + ix(i, t)] = 1
            A[r, o_c + t] = -1; A[r, o_n + t] = 1; A[r, o_o + t] = -1
            lo[r] = hi[r] = self.D[t] - (self.solar[t] * scen[0] + self.wind[t] * scen[1]); r += 1
        res = milp(cost, constraints=LinearConstraint(A.tocsr(), lo, hi),
                   integrality=integrality, bounds=Bounds(lb, ub))
        x = res.x
        u = np.round(x[:nu]).reshape(G, T).astype(int)
        return u, res.fun
