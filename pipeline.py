"""End-to-end closed loop: forecast inputs -> QUBO -> QAOA (Qiskit) -> LP dispatch -> benchmark."""
from __future__ import annotations

import time
import numpy as np
from grid import Problem, SCENARIOS
from qaoa import QAOASolver

DEFAULT_PENALTY = 500.0
DEFAULT_NOMINAL = 0.75
DEFAULT_RESERVE = 0.05


def naive_all_on(pb):
    return np.ones((pb.G, pb.T), dtype=int)


def greedy_merit_order(pb):
    """Classical heuristic used as a simple baseline: cheapest plants first until capacity covers demand."""
    u = np.zeros((pb.G, pb.T), dtype=int)
    order = np.argsort(pb.mc)
    for t in range(pb.T):
        need = pb.D[t] - pb.R[t]
        cap = 0.0
        for i in order:
            if cap >= need * 1.05:
                break
            u[i, t] = 1; cap += pb.pmax[i]
    return u


def optimise(demand_scale=1.0, solar_scale=1.0, wind_scale=1.0, reps=2, restarts=3,
             maxiter=120, shots=4096, warm=None, seed=7):
    """One full re-optimisation. Pass warm=theta from the previous run for a fast update."""
    pb = Problem(demand_scale, solar_scale, wind_scale, penalty=DEFAULT_PENALTY, nominal_frac=DEFAULT_NOMINAL,
                 reserve=DEFAULT_RESERVE)
    solver = QAOASolver(pb, reps=reps, seed=seed)
    t0 = time.time()
    q = solver.run(restarts=restarts, maxiter=maxiter if warm is None else 60, shots=shots, warm=warm)
    u_milp, f_milp = pb.solve_milp()
    t_milp = time.time() - t0 - q["total_s"]
    ev_q = q["eval"]
    ev_m = pb.evaluate(u_milp)
    ev_n = pb.evaluate(naive_all_on(pb))
    ev_g = pb.evaluate(greedy_merit_order(pb))
    return dict(
        problem=pb, solver=solver, qaoa=q, milp=ev_m, naive=ev_n, greedy=ev_g, quantum=ev_q,
        gap_pct=(ev_q["cost"] / ev_m["cost"] - 1) * 100,
        saving_vs_naive_pct=(1 - ev_q["cost"] / ev_n["cost"]) * 100,
        saving_vs_greedy_pct=(1 - ev_q["cost"] / ev_g["cost"]) * 100,
        robust=pb.robust_table(ev_q["u"]),
        worst_unserved={k: max(v["unserved_mwh"] for v in pb.robust_table(e["u"]).values())
                        for k, e in [("quantum", ev_q), ("milp", ev_m), ("greedy", ev_g), ("naive", ev_n)]},
        t_qaoa=q["total_s"], t_milp=max(t_milp, 0.0),
    )
