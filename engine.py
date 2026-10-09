"""
engine.py -- runs and times ALL methods for a given scenario.

Methods compared:
    milp_network    NetworkMILP (joint commitment + dispatch + flows, HiGHS)
    lp_qaoa         NetworkLP   (LP dispatch + flows on QAOA schedule)
    lp_milp         NetworkLP   (LP dispatch + flows on MILP schedule, for cross-check)
    qp_qaoa         NetworkQP   (quadratic dispatch + flows on QAOA schedule)
    lp_greedy       NetworkLP   (greedy merit-order schedule)
    lp_allon        NetworkLP   (all plants ON)
    qaoa            QAOA unit commitment (single-bus QUBO -> best schedule)

All results are returned in a flat dict ready for compare.py and app.py.
"""
from __future__ import annotations

import time
import numpy as np

from grid import Problem, GENS
from qaoa import QAOASolver
from network import (
    NetworkLP, NetworkQP, NetworkMILP,
    demand_simulator, NODES, LINES, LINE_CAP, GEN_NODE,
)


# ---------------------------------------------------------------------------
# Classical heuristics (return G x T arrays)
# ---------------------------------------------------------------------------
def greedy_merit_order(pb: Problem) -> np.ndarray:
    """Cheapest plants first until capacity (+ renewables) covers demand."""
    u = np.zeros((pb.G, pb.T), dtype=int)
    order = np.argsort(pb.mc)
    for t in range(pb.T):
        need = pb.D[t] - pb.R[t]
        cap = 0.0
        for i in order:
            if cap >= need * 1.05:
                break
            u[i, t] = 1
            cap += pb.pmax[i]
    return u


def all_on(pb: Problem) -> np.ndarray:
    return np.ones((pb.G, pb.T), dtype=int)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def run_all(
    demand_scale: float = 1.0,
    solar_scale:  float = 1.0,
    wind_scale:   float = 1.0,
    reps:         int   = 2,
    restarts:     int   = 3,
    maxiter:      int   = 120,
    shots:        int   = 4096,
    warm_theta          = None,
    seed:         int   = 7,
    csv_path:     str | None = None,
    ansatz_type:  str   = "custom",
    optimizer:    str   = "COBYLA",
) -> dict:
    """
    Run all methods for one scenario. Returns a rich result dict.

    csv_path: path to real APSLDC or custom CSV file (overrides synthetic simulator)
    warm_theta: previous QAOA parameter vector for warm-start (faster re-optimisation)
    """
    if csv_path:
        from network import read_demand_csv
        net = read_demand_csv(csv_path)
        pb = Problem(
            demand_scale=1.0,
            solar_scale=1.0,
            wind_scale=1.0,
            penalty=500.0,
            nominal_frac=0.75,
            reserve=0.05,
        )
        pb.D = net["total_demand"]
        pb.R = net["renewable_mw"].sum(axis=0)
    else:
        net = demand_simulator(demand_scale, solar_scale, wind_scale)
        pb = Problem(
            demand_scale=demand_scale,
            solar_scale=solar_scale,
            wind_scale=wind_scale,
            penalty=500.0,
            nominal_frac=0.75,
            reserve=0.05,
        )

    # ---- QAOA ---------------------------------------------------------------
    t0 = time.perf_counter()
    solver = QAOASolver(pb, reps=reps, seed=seed, ansatz_type=ansatz_type)
    q_out = solver.run(
        restarts=restarts,
        maxiter=maxiter if warm_theta is None else 60,
        shots=shots,
        warm=warm_theta,
        optimizer=optimizer,
    )
    t_qaoa = time.perf_counter() - t0
    u_qaoa = q_out["eval"]["u"]

    # ---- Network LP on QAOA schedule ----------------------------------------
    t0 = time.perf_counter()
    lp = NetworkLP()
    lp_qaoa = lp.solve(u_qaoa, net)
    t_lp = time.perf_counter() - t0

    # ---- Network QP on QAOA schedule ----------------------------------------
    t0 = time.perf_counter()
    qp = NetworkQP()
    qp_qaoa = qp.solve(u_qaoa, net)
    t_qp = time.perf_counter() - t0

    # ---- Network MILP -------------------------------------------------------
    t0 = time.perf_counter()
    nmilp = NetworkMILP()
    milp_net = nmilp.solve(net)
    t_milp = time.perf_counter() - t0
    u_milp = milp_net["u"]

    # ---- LP on MILP schedule (cross-check) ----------------------------------
    lp_milp = lp.solve(u_milp, net)

    # ---- Classical baselines ------------------------------------------------
    u_greedy = greedy_merit_order(pb)
    u_allon  = all_on(pb)
    lp_greedy = lp.solve(u_greedy, net)
    lp_allon  = lp.solve(u_allon, net)

    # ---- Robustness: re-score QAOA schedule under all weather scenarios ------
    from grid import SCENARIOS
    robust = {}
    for name, (ss, ws) in SCENARIOS.items():
        net_sc = demand_simulator(demand_scale, ss, ws)
        robust[name] = lp.solve(u_qaoa, net_sc)

    # ---- Gap metrics --------------------------------------------------------
    gap_pct          = (lp_qaoa["cost"] / milp_net["cost"] - 1) * 100
    saving_vs_allon  = (1 - lp_qaoa["cost"] / lp_allon["cost"])  * 100
    saving_vs_greedy = (1 - lp_qaoa["cost"] / lp_greedy["cost"]) * 100

    worst_unserved = {
        "qaoa":   max(v["unserved_mwh"] for v in robust.values()),
        "milp":   lp_milp["unserved_mwh"],
        "greedy": lp_greedy["unserved_mwh"],
        "allon":  lp_allon["unserved_mwh"],
    }

    return dict(
        # Inputs
        net=net, problem=pb, solver=solver,
        # Schedules
        u_qaoa=u_qaoa, u_milp=u_milp, u_greedy=u_greedy, u_allon=u_allon,
        # Cost results
        lp_qaoa=lp_qaoa, qp_qaoa=qp_qaoa,
        milp_net=milp_net, lp_milp=lp_milp,
        lp_greedy=lp_greedy, lp_allon=lp_allon,
        # QAOA internals
        qaoa=q_out, theta=q_out["theta"],
        # Robustness
        robust=robust,
        worst_unserved=worst_unserved,
        # Metrics
        gap_pct=gap_pct,
        saving_vs_allon_pct=saving_vs_allon,
        saving_vs_greedy_pct=saving_vs_greedy,
        # Timing (seconds)
        t_qaoa=t_qaoa, t_lp=t_lp, t_qp=t_qp, t_milp=t_milp,
        # Scales
        demand_scale=demand_scale, solar_scale=solar_scale, wind_scale=wind_scale,
    )
