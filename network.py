"""
Five-node AP-style network model for Use Case 04.

Nodes (substations): VSKP, VZM, VJA, KNL, TPT
Lines (transport/flow model -- NOT full AC/DC power flow):
    VSKP-VZM  300 MW
    VZM-VJA   250 MW
    VJA-KNL   200 MW
    KNL-TPT   150 MW
    VJA-TPT   200 MW

Generator placement:
    VSKP: Coal-1, Coal-2
    VJA : Gas
    KNL : Hydro

Renewables (illustrative):
    Solar: VSKP 30%, VZM 40%, VJA 30%  (of total SOLAR_CAP per block)
    Wind : VSKP 60%, KNL 40%            (of total WIND_CAP per block)

Demand shares (must sum to 1.0):
    VSKP 0.30 | VZM 0.15 | VJA 0.25 | KNL 0.15 | TPT 0.15

Provides:
    demand_simulator()    -- synthetic MW per node per block
    read_demand_csv()     -- load real SLDC data  (columns: block,node,demand_mw)
    NetworkLP             -- LP dispatch + flows (fixed schedule)
    NetworkQP             -- quadratic-cost dispatch + flows (SLSQP)
    NetworkMILP           -- joint commitment + dispatch + flows (HiGHS)

Honest notes:
    * Data are illustrative assumptions; replace with real SLDC load data.
    * Transport model: flow conservation at each node, line capacity limits only.
      No AC/DC power flow, no reactive power, no ramp or min-up-down constraints.
    * VOLL = 50,000 Rs/MWh; curtailment penalty = 100 Rs/MWh.
"""
from __future__ import annotations

import os
import numpy as np
import pandas as pd
from scipy.optimize import linprog, minimize, Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

from grid import GENS, DEMAND, SOLAR_CAP, SOLAR_CF, WIND_CAP, WIND_CF, H, BLOCKS
from grid import CARBON, VOLL, CURT, OVER, SCENARIOS

# ---------------------------------------------------------------------------
# Network topology
# ---------------------------------------------------------------------------
NODES = ["VSKP", "VZM", "VJA", "KNL", "TPT"]
N_NODES = len(NODES)
NODE_IDX = {n: i for i, n in enumerate(NODES)}

# Lines: (from_node, to_node, capacity_MW)
LINES = [
    ("VSKP", "VZM", 300.0),
    ("VZM",  "VJA", 250.0),
    ("VJA",  "KNL", 200.0),
    ("KNL",  "TPT", 150.0),
    ("VJA",  "TPT", 200.0),
]
N_LINES = len(LINES)

# Incidence matrix: A[n, l] = +1 if line l leaves node n, -1 if enters, 0 otherwise
INCIDENCE = np.zeros((N_NODES, N_LINES))
for l, (frm, to, _) in enumerate(LINES):
    INCIDENCE[NODE_IDX[frm], l] = +1.0
    INCIDENCE[NODE_IDX[to],  l] = -1.0
LINE_CAP = np.array([cap for _, _, cap in LINES])

# Generator-to-node mapping (index into GENS list)
GEN_NODE = {0: "VSKP", 1: "VSKP", 2: "VJA", 3: "KNL"}   # Coal-1, Coal-2, Gas, Hydro

# Demand shares per node (must sum to 1.0)
DEMAND_SHARE = np.array([0.30, 0.15, 0.25, 0.15, 0.15])   # VSKP VZM VJA KNL TPT

# Renewable distribution per node
SOLAR_SHARE = np.array([0.30, 0.40, 0.30, 0.00, 0.00])    # VSKP VZM VJA KNL TPT
WIND_SHARE  = np.array([0.60, 0.00, 0.00, 0.40, 0.00])


# ---------------------------------------------------------------------------
# Demand simulator
# ---------------------------------------------------------------------------
def demand_simulator(
    demand_scale: float = 1.0,
    solar_scale:  float = 1.0,
    wind_scale:   float = 1.0,
    noise_std:    float = 0.02,
    seed:         int   = 42,
) -> dict:
    """
    Returns demand_mw[node_idx, block_idx] and renewable_mw[node_idx, block_idx].

    demand_scale: multiplier on total system demand (e.g. 1.15 for heat-wave)
    solar_scale:  multiplier on solar output    (e.g. 0.5 for cloudy day)
    wind_scale:   multiplier on wind output     (e.g. 0.4 for calm day)
    noise_std:    fractional Gaussian noise on demand per node (reproducible)
    """
    rng = np.random.default_rng(seed)
    # Total demand per block
    total = DEMAND * demand_scale                              # shape (T,)
    # Distribute across nodes with small noise
    noise = 1.0 + rng.normal(0, noise_std, (N_NODES, len(BLOCKS)))
    noise = np.clip(noise, 0.8, 1.2)
    demand_mw = (DEMAND_SHARE[:, None] * total[None, :]) * noise
    # Re-normalise so each block total equals the scaled total demand
    demand_mw = demand_mw / demand_mw.sum(axis=0) * total[None, :]

    # Renewable per node per block
    solar_total = SOLAR_CAP * SOLAR_CF * solar_scale          # shape (T,)
    wind_total  = WIND_CAP  * WIND_CF  * wind_scale           # shape (T,)
    solar_mw = SOLAR_SHARE[:, None] * solar_total[None, :]
    wind_mw  = WIND_SHARE[:, None]  * wind_total[None, :]
    renewable_mw = solar_mw + wind_mw

    return {
        "demand_mw":    demand_mw,          # (N_NODES, T)
        "renewable_mw": renewable_mw,       # (N_NODES, T)
        "solar_mw":     solar_mw,
        "wind_mw":      wind_mw,
        "total_demand": total,
        "blocks":       BLOCKS,
        "nodes":        NODES,
    }


def read_demand_csv(path: str) -> dict:
    """
    Read a CSV with columns: block, node, demand_mw
    (optionally: solar_mw, wind_mw)
    Returns the same dict shape as demand_simulator().
    Missing renewables default to 0.
    """
    df = pd.read_csv(path)
    T = len(BLOCKS)
    demand_mw    = np.zeros((N_NODES, T))
    renewable_mw = np.zeros((N_NODES, T))
    for _, row in df.iterrows():
        t = BLOCKS.index(str(row["block"])) if str(row["block"]) in BLOCKS else int(row["block"])
        n = NODE_IDX.get(str(row["node"]), -1)
        if n < 0:
            continue
        demand_mw[n, t] = float(row["demand_mw"])
        if "solar_mw" in df.columns:
            renewable_mw[n, t] += float(row.get("solar_mw", 0))
        if "wind_mw" in df.columns:
            renewable_mw[n, t] += float(row.get("wind_mw", 0))
    return {
        "demand_mw": demand_mw, "renewable_mw": renewable_mw,
        "solar_mw": np.zeros((N_NODES, T)), "wind_mw": np.zeros((N_NODES, T)),
        "total_demand": demand_mw.sum(axis=0), "blocks": BLOCKS, "nodes": NODES,
    }


def save_demand_template(path: str = "data/demand_template.csv"):
    """Save a CSV template that users can fill in with real SLDC data."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sim = demand_simulator()
    rows = []
    for ni, node in enumerate(NODES):
        for ti, block in enumerate(BLOCKS):
            rows.append({
                "block": block, "node": node,
                "demand_mw": round(sim["demand_mw"][ni, ti], 1),
                "solar_mw":  round(sim["solar_mw"][ni, ti], 1),
                "wind_mw":   round(sim["wind_mw"][ni, ti], 1),
            })
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


# ---------------------------------------------------------------------------
# Network LP -- dispatch + flows for a FIXED ON/OFF schedule
# ---------------------------------------------------------------------------
class NetworkLP:
    """
    LP: minimise generation cost + unserved/curtailment penalties
    subject to: nodal balance, generator min/max, line capacity.
    The ON/OFF schedule u (G x T) must be supplied externally (e.g. from QAOA).
    """

    def solve(self, u: np.ndarray, net: dict, gens: list | None = None, blocks: list | None = None) -> dict:
        """
        u: (G, T) binary ON/OFF schedule
        net: output of demand_simulator() or read_demand_csv()
        gens: list of generator dicts (defaults to GENS or subset matching u)
        blocks: list of block indices or names (defaults to BLOCKS or subset matching u)
        Returns dict with dispatch, flows, costs per block.
        """
        GEN_NAME_NODE = {"Coal-1": "VSKP", "Coal-2": "VSKP", "Gas": "VJA", "Hydro": "KNL"}
        if gens is None:
            gens = GENS if u.shape[0] == len(GENS) else [GENS[0], GENS[2], GENS[3]]
        if blocks is None:
            blocks = list(range(u.shape[1]))

        G = len(gens)
        T = u.shape[1]
        mc   = np.array([g["fuel"] + CARBON * g["co2"] for g in gens])
        pmin = np.array([g["pmin"] for g in gens], float)
        pmax = np.array([g["pmax"] for g in gens], float)

        results = []
        total_cost = 0.0

        for col_idx in range(T):
            b_val = blocks[col_idx]
            t_block = BLOCKS.index(b_val) if isinstance(b_val, str) else int(b_val)
            dem  = net["demand_mw"][:, t_block]          # (N_NODES,)
            ren  = net["renewable_mw"][:, t_block]       # (N_NODES,)
            on_g = u[:, col_idx]                         # (G,)

            # Variables: p[G], f[L], curt[N], uns[N]
            nv = G + N_LINES + 2 * N_NODES
            ip = 0; ifl = G; ic = G + N_LINES; iu = G + N_LINES + N_NODES

            # Objective
            obj = np.zeros(nv)
            obj[ip:ip+G]       = mc * H * on_g        # generation cost (0 if OFF)
            obj[ic:ic+N_NODES] = CURT * H             # curtailment penalty
            obj[iu:iu+N_NODES] = VOLL * H             # unserved penalty

            # Bounds
            lb = np.zeros(nv)
            ub = np.full(nv, np.inf)
            for i in range(G):
                lb[ip+i] = pmin[i] * on_g[i]
                ub[ip+i] = pmax[i] * on_g[i]
            for l in range(N_LINES):
                lb[ifl+l] = -LINE_CAP[l]
                ub[ifl+l] =  LINE_CAP[l]
            ub[ic:ic+N_NODES] = ren              # can't curtail more than available renewable
            # uns: unbounded above

            # Nodal balance equality: for each node n
            A_eq = np.zeros((N_NODES, nv))
            for i in range(G):
                g_node = gens[i].get("node", GEN_NAME_NODE.get(gens[i]["name"], "VSKP"))
                n = NODE_IDX[g_node]
                A_eq[n, ip+i] = 1.0
            for l in range(N_LINES):
                for n in range(N_NODES):
                    A_eq[n, ifl+l] = -INCIDENCE[n, l]
            for n in range(N_NODES):
                A_eq[n, ic+n] = -1.0   # curtailment reduces supply surplus
                A_eq[n, iu+n] =  1.0   # unserved adds to "supply"
            b_eq = dem - ren

            res = linprog(obj, A_eq=A_eq, b_eq=b_eq,
                          bounds=list(zip(lb, ub)), method="highs")

            if res.status != 0:
                results.append({"status": "infeasible", "cost": VOLL * H * dem.sum(), "t": t_block})
                total_cost += VOLL * H * dem.sum()
                continue

            x = res.x
            p    = x[ip:ip+G]
            f    = x[ifl:ifl+N_LINES]
            curt = x[ic:ic+N_NODES]
            uns  = x[iu:iu+N_NODES]
            results.append({
                "t": t_block, "block": BLOCKS[t_block], "status": "ok",
                "p_mw": p, "flow_mw": f,
                "curt_mw": curt, "uns_mw": uns,
                "cost": res.fun,
                "line_loading_pct": np.abs(f) / LINE_CAP * 100,
                "demand_mw": float(dem.sum()),
                "renewable_mw": float(ren.sum()),
                "thermal_mw": float(p.sum()),
            })
            total_cost += res.fun

        # Startup costs
        prev = np.array([g["init"] for g in gens], float)
        startup_cost = 0.0
        for t in range(T):
            starts = np.maximum(u[:, t] - prev, 0)
            startup_cost += float((starts * np.array([g["start"] for g in gens])).sum())
            prev = u[:, t].astype(float)
        total_cost += startup_cost

        # Aggregate totals
        p_all    = np.array([r.get("p_mw",    np.zeros(G)) for r in results if r["status"] == "ok"])
        curt_all = np.array([r.get("curt_mw", np.zeros(N_NODES)) for r in results if r["status"] == "ok"])
        uns_all  = np.array([r.get("uns_mw",  np.zeros(N_NODES)) for r in results if r["status"] == "ok"])
        co2      = np.array([g["co2"] for g in gens])

        return {
            "blocks": results,
            "cost":         total_cost,
            "startup_cost": startup_cost,
            "emissions_t":  float((p_all * co2[None, :]).sum() * H) if len(p_all) else 0.0,
            "curtail_mwh":  float(curt_all.sum() * H) if len(curt_all) else 0.0,
            "unserved_mwh": float(uns_all.sum() * H)  if len(uns_all)  else 0.0,
            "u": u,
        }


# ---------------------------------------------------------------------------
# Network QP -- quadratic (marginal-cost**2) dispatch + flows (SLSQP)
# ---------------------------------------------------------------------------
class NetworkQP:
    """
    Quadratic-cost dispatch: minimise sum_i alpha_i * p_i^2 + beta_i * p_i
    subject to nodal balance + line limits.
    Useful for showing economic dispatch with marginal cost curves.
    alpha_i = mc_i / (2 * pmax_i)  (illustrative linear marginal cost up to pmax)
    """

    def solve(self, u: np.ndarray, net: dict) -> dict:
        G = len(GENS)
        T = len(BLOCKS)
        mc   = np.array([g["fuel"] + CARBON * g["co2"] for g in GENS])
        pmin = np.array([g["pmin"] for g in GENS], float)
        pmax = np.array([g["pmax"] for g in GENS], float)
        alpha = mc / (2.0 * np.maximum(pmax, 1.0))  # quadratic coefficient

        results = []
        total_cost = 0.0

        for t in range(T):
            dem = net["demand_mw"][:, t]
            ren = net["renewable_mw"][:, t]
            on_g = u[:, t]

            nv = G + N_LINES + 2 * N_NODES
            ip = 0; ifl = G; ic = G + N_LINES; iu = G + N_LINES + N_NODES

            def obj(x):
                p = x[ip:ip+G]
                curt = x[ic:ic+N_NODES]
                uns  = x[iu:iu+N_NODES]
                gen_cost = float((alpha * on_g * p**2 + mc * H * on_g * p).sum())
                return gen_cost + CURT * H * curt.sum() + VOLL * H * uns.sum()

            def grad(x):
                p = x[ip:ip+G]
                curt = x[ic:ic+N_NODES]
                uns  = x[iu:iu+N_NODES]
                g_arr = np.zeros(nv)
                g_arr[ip:ip+G] = (2 * alpha * on_g * p + mc * H * on_g)
                g_arr[ic:ic+N_NODES] = CURT * H
                g_arr[iu:iu+N_NODES] = VOLL * H
                return g_arr

            # Bounds
            lb = np.zeros(nv); ub = np.full(nv, np.inf)
            for i in range(G):
                lb[ip+i] = pmin[i] * on_g[i]; ub[ip+i] = pmax[i] * on_g[i]
            for l in range(N_LINES):
                lb[ifl+l] = -LINE_CAP[l]; ub[ifl+l] = LINE_CAP[l]
            ub[ic:ic+N_NODES] = ren

            # Nodal balance equality
            A_eq = np.zeros((N_NODES, nv))
            for i in range(G):
                n = NODE_IDX[GEN_NODE[i]]
                A_eq[n, ip+i] = 1.0
            for l in range(N_LINES):
                for n in range(N_NODES):
                    A_eq[n, ifl+l] = -INCIDENCE[n, l]
            for n in range(N_NODES):
                A_eq[n, ic+n] = -1.0; A_eq[n, iu+n] = 1.0
            b_eq = dem - ren

            x0 = np.zeros(nv)
            for i in range(G):
                x0[ip+i] = (pmin[i] + pmax[i]) / 2 * on_g[i]
            x0[iu:iu+N_NODES] = np.maximum(b_eq - A_eq[:, ip:ip+G] @ x0[ip:ip+G], 0)

            from scipy.optimize import minimize as _min
            res = _min(obj, x0, jac=grad,
                       method="SLSQP",
                       bounds=list(zip(lb, ub)),
                       constraints={"type": "eq", "fun": lambda x: A_eq @ x - b_eq},
                       options={"ftol": 1e-8, "maxiter": 500})

            x = res.x
            p    = x[ip:ip+G]
            f    = x[ifl:ifl+N_LINES]
            curt = x[ic:ic+N_NODES]
            uns  = x[iu:iu+N_NODES]
            results.append({
                "t": t, "block": BLOCKS[t], "status": "ok",
                "p_mw": p, "flow_mw": f,
                "curt_mw": curt, "uns_mw": uns, "cost": res.fun,
                "line_loading_pct": np.abs(f) / LINE_CAP * 100,
            })
            total_cost += res.fun

        # Startup costs
        prev = np.array([g["init"] for g in GENS], float)
        startup_cost = 0.0
        for t in range(T):
            starts = np.maximum(u[:, t] - prev, 0)
            startup_cost += float((starts * np.array([g["start"] for g in GENS])).sum())
            prev = u[:, t].astype(float)
        total_cost += startup_cost

        p_all    = np.array([r["p_mw"]    for r in results])
        curt_all = np.array([r["curt_mw"] for r in results])
        uns_all  = np.array([r["uns_mw"]  for r in results])
        co2      = np.array([g["co2"] for g in GENS])

        return {
            "blocks": results, "cost": total_cost, "startup_cost": startup_cost,
            "emissions_t":  float((p_all * co2[None, :]).sum() * H),
            "curtail_mwh":  float(curt_all.sum() * H),
            "unserved_mwh": float(uns_all.sum() * H),
            "u": u,
        }


# ---------------------------------------------------------------------------
# Network MILP -- joint commitment + dispatch + flows
# ---------------------------------------------------------------------------
class NetworkMILP:
    """
    MILP: simultaneously optimise ON/OFF decisions AND dispatch AND line flows.
    Variables: u[G,T], p[G,T], s[G,T](startup), f[L,T], curt[N,T], uns[N,T]
    """

    def solve(self, net: dict, scen: tuple = (1.0, 1.0)) -> dict:
        G = len(GENS)
        T = len(BLOCKS)
        L = N_LINES
        N = N_NODES
        mc   = np.array([g["fuel"] + CARBON * g["co2"] for g in GENS])
        pmin = np.array([g["pmin"] for g in GENS], float)
        pmax = np.array([g["pmax"] for g in GENS], float)
        start= np.array([g["start"] for g in GENS], float)
        init = np.array([g["init"]  for g in GENS], float)

        ix = lambda i, t: i * T + t

        # Layout: u(G*T) | p(G*T) | s(G*T) | f(L*T) | curt(N*T) | uns(N*T)
        o_u  = 0
        o_p  = G * T
        o_s  = 2 * G * T
        o_f  = 3 * G * T
        o_c  = 3 * G * T + L * T
        o_n  = 3 * G * T + L * T + N * T
        nv   = 3 * G * T + L * T + 2 * N * T

        cost = np.zeros(nv)
        for i in range(G):
            for t in range(T):
                cost[o_p + ix(i, t)] = mc[i] * H
                cost[o_s + ix(i, t)] = start[i]
        for n in range(N):
            for t in range(T):
                cost[o_c + n * T + t] = CURT * H
                cost[o_n + n * T + t] = VOLL * H

        lb = np.zeros(nv); ub = np.full(nv, np.inf)
        ub[o_u:o_u + G * T] = 1.0
        ub[o_s:o_s + G * T] = 1.0
        for i in range(G):
            for t in range(T):
                ub[o_p + ix(i, t)] = pmax[i]
        for l in range(L):
            for t in range(T):
                lb[o_f + l * T + t] = -LINE_CAP[l]
                ub[o_f + l * T + t] =  LINE_CAP[l]
        ren = net["renewable_mw"] * np.array([scen[0], scen[0], scen[0], scen[1], scen[1]])[:, None]
        ren = ren * 0 + net["renewable_mw"]  # use sim values (scen applied externally)
        for n in range(N):
            for t in range(T):
                ub[o_c + n * T + t] = ren[n, t]

        integrality = np.zeros(nv)
        integrality[o_u:o_u + G * T] = 1
        integrality[o_s:o_s + G * T] = 1

        n_rows = 2 * G * T + G * T + N * T
        A = lil_matrix((n_rows, nv))
        lo = np.full(n_rows, -np.inf)
        hi = np.full(n_rows, np.inf)
        r = 0

        # p >= pmin * u  and  p <= pmax * u
        for i in range(G):
            for t in range(T):
                A[r, o_p + ix(i, t)] = 1; A[r, o_u + ix(i, t)] = -pmin[i]; lo[r] = 0; r += 1
                A[r, o_p + ix(i, t)] = 1; A[r, o_u + ix(i, t)] = -pmax[i]; hi[r] = 0; r += 1

        # startup: s >= u_t - u_{t-1}
        for i in range(G):
            for t in range(T):
                A[r, o_s + ix(i, t)] = 1; A[r, o_u + ix(i, t)] = -1
                if t == 0:
                    lo[r] = -init[i]
                else:
                    A[r, o_u + ix(i, t - 1)] = 1; lo[r] = 0
                r += 1

        # Nodal balance: for each node n and block t
        for n in range(N):
            for t in range(T):
                # generation at this node
                for i in range(G):
                    if GEN_NODE[i] == NODES[n]:
                        A[r, o_p + ix(i, t)] = 1
                # net line flow INTO this node = -INCIDENCE[n,l] * f[l]
                for l in range(L):
                    A[r, o_f + l * T + t] = -INCIDENCE[n, l]
                # curtailment and unserved
                A[r, o_c + n * T + t] = -1
                A[r, o_n + n * T + t] =  1
                lo[r] = hi[r] = net["demand_mw"][n, t] - ren[n, t]
                r += 1

        from scipy.optimize import milp as _milp
        res = _milp(cost,
                    constraints=LinearConstraint(A.tocsr(), lo, hi),
                    integrality=integrality,
                    bounds=Bounds(lb, ub))

        x = res.x
        u_sol = np.round(x[o_u:o_u + G * T]).reshape(G, T).astype(int)
        p_sol = x[o_p:o_p + G * T].reshape(G, T)
        f_sol = x[o_f:o_f + L * T].reshape(L, T)
        c_sol = x[o_c:o_c + N * T].reshape(N, T)
        n_sol = x[o_n:o_n + N * T].reshape(N, T)
        co2   = np.array([g["co2"] for g in GENS])

        blocks_out = []
        for t in range(T):
            blocks_out.append({
                "t": t, "block": BLOCKS[t], "status": "ok",
                "p_mw": p_sol[:, t], "flow_mw": f_sol[:, t],
                "curt_mw": c_sol[:, t], "uns_mw": n_sol[:, t],
                "line_loading_pct": np.abs(f_sol[:, t]) / LINE_CAP * 100,
            })

        return {
            "blocks": blocks_out, "cost": res.fun,
            "u": u_sol, "p": p_sol, "flows": f_sol,
            "emissions_t":  float((p_sol * co2[:, None]).sum() * H),
            "curtail_mwh":  float(c_sol.sum() * H),
            "unserved_mwh": float(n_sol.sum() * H),
        }
