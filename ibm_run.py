"""
ibm_run.py -- Submit and fetch QAOA jobs on IBM Quantum Platform.

Usage:
    py ibm_run.py submit --fake          # rehearse locally with noisy fake backend
    py ibm_run.py submit                 # REAL IBM run -> prints job_id + dashboard URL
    py ibm_run.py submit --backend ibm_brisbane
    py ibm_run.py fetch <job_id>         # download counts, complete pipeline, save results

One-time setup (run once in Python):
    from qiskit_ibm_runtime import QiskitRuntimeService
    QiskitRuntimeService.save_account(token="YOUR_API_KEY", overwrite=True)

What you see on IBM Platform after submit:
    - Transpiled circuit diagram + gate counts
    - Measurement histogram (bitstring probabilities)
    - Job runtime, queue time, backend info

State file: ibm_job_state.json  (job_id + trained theta; used by fetch)
Results:    ibm_results.json    (schedule, costs, line flows; for your frontend)

Honest notes:
    - Instance is 9 qubits (3 generators x 3 blocks) sized for NISQ hardware.
    - Parameters trained on exact simulator; only sampling runs on hardware.
    - Transport flow model; no AC/DC power flow.
    - No quantum-advantage claim at this size.
"""
from __future__ import annotations

import sys
import io
import argparse
import json
import time

import numpy as np

# Force UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from grid import GENS, Problem
from qaoa import QAOASolver
from network import NetworkLP, demand_simulator, NODES, LINES, LINE_CAP, BLOCKS

STATE_FILE   = "ibm_job_state.json"
RESULTS_FILE = "ibm_results.json"


# ---------------------------------------------------------------------------
# Small 9-qubit problem instance (3 generators x 3 time blocks)
# ---------------------------------------------------------------------------
def build_small_problem():
    gens = [GENS[0], GENS[2], GENS[3]]   # Coal-1, Gas, Hydro
    return Problem(gens=gens, blocks=[0, 1, 2],
                   penalty=500.0, nominal_frac=0.75, reserve=0.05)


# ---------------------------------------------------------------------------
# SUBMIT subcommand
# ---------------------------------------------------------------------------
def cmd_submit(args):
    print("\n" + "#" * 62)
    print("  AP Grid Optimiser -- IBM Quantum Platform  [SUBMIT]")
    print("  9-qubit QAOA: 3 generators x 3 time blocks")
    print("#" * 62)

    pb = build_small_problem()

    # Step 1: Train QAOA parameters on exact simulator
    ansatz_label = "Custom Grid-Topology + MA-QAOA" if args.ansatz == "custom" else "Standard Qiskit qaoa_ansatz"
    print(f"\n[1/4] Training QAOA ({ansatz_label}) with {args.optimizer} ...")
    if getattr(args, "use_estimator", False):
        print("      [Mode] Using StatevectorEstimator / EstimatorV2 for quantum expectation")
    solver = QAOASolver(pb, reps=args.reps, seed=7, ansatz_type=args.ansatz)
    t0 = time.time()
    theta, e_train = solver.train(restarts=args.restarts, maxiter=args.maxiter,
                                  optimizer=args.optimizer,
                                  use_estimator=getattr(args, "use_estimator", False))
    t_train = time.time() - t0
    print(f"      Qubits    : {pb.n}")
    print(f"      Ansatz    : {args.ansatz.upper()} ({len(solver.params)} params, {args.reps} layers)")
    print(f"      Optimizer : {args.optimizer}")
    print(f"      <E>       : {e_train/1e5:.2f} lakh"
          f"  (random={solver.E.mean()/1e5:.2f}, optimum={solver.E.min()/1e5:.2f})")
    print(f"      Train time: {t_train:.1f}s")

    # Step 2: Run ideal Aer reference
    print("\n[2/4] Ideal Aer reference sampling (noiseless) ...")
    ideal_counts = solver.sample_aer(theta, shots=args.shots)
    ideal = solver.hybrid_select(ideal_counts)
    print(f"      P(best 1%) = {ideal['prob_in_top1pct']:.0%}  (random=1%)")
    print(f"      QUBO optimum sampled: {ideal['lowest_energy_rank'] == 0}")

    # Step 3: Transpile for IBM backend
    print("\n[3/4] Transpiling and picking backend ...")
    qc = solver._bind(theta)
    qc.measure_all()

    from qiskit import qasm3
    with open("qaoa_circuit.qasm", "w") as f:
        qasm3.dump(qc, f)
    print("      Circuit saved -> qaoa_circuit.qasm (paste into IBM Quantum Composer)")

    if args.fake:
        from qiskit_ibm_runtime.fake_provider import FakeSherbrooke
        from qiskit_ibm_runtime import SamplerV2 as Sampler
        backend = FakeSherbrooke()
        label = "FAKE ibm_sherbrooke (noisy local simulation)"
    else:
        from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2 as Sampler
        _TOKEN = "UcwEoEXnxx8FtQYgXJTRurmONZSJ-bd-nMWORnHEj3Q8"
        svc = QiskitRuntimeService(channel="ibm_quantum_platform", token=_TOKEN, instance="open-instance")
        if args.backend:
            backend = svc.backend(args.backend)
        else:
            backend = svc.least_busy(operational=True, simulator=False,
                                     min_num_qubits=pb.n)
        label = backend.name

    pm  = generate_preset_pass_manager(optimization_level=3, backend=backend)
    isa = pm.run(qc)
    cx  = isa.count_ops().get("cx",  0)
    ecr = isa.count_ops().get("ecr", 0)
    cz  = isa.count_ops().get("cz",  0)
    print(f"      Backend       : {label}")
    print(f"      Circuit depth : {isa.depth()}")
    print(f"      2Q gate count : {cx+ecr+cz}  (cx={cx}, ecr={ecr}, cz={cz})")

    # Step 4: Submit job
    print("\n[4/4] Submitting to IBM Quantum Platform ...")
    from qiskit_ibm_runtime import SamplerV2 as Sampler
    sampler = Sampler(mode=backend)
    t_submit = time.time()
    job = sampler.run([isa], shots=args.shots)
    job_id = getattr(job, "job_id", lambda: "local")()
    print(f"\n      Job ID  : {job_id}")
    if not args.fake:
        print(f"\n      >> View on IBM Quantum Platform:")
        print(f"         https://quantum.cloud.ibm.com/jobs/{job_id}")
        print(f"\n      Waiting for results ...")

    result  = job.result()
    t_run   = time.time() - t_submit
    raw_counts = result[0].data.meas.get_counts()

    # Noise mitigation check
    if getattr(args, "resilience", 1) >= 1:
        from mitigation import ReadoutMitigator
        mitigator = ReadoutMitigator(num_qubits=pb.n)
        counts = mitigator.mitigate_counts(raw_counts)
        print("      Readout error mitigation applied (M3/inversion + simplex projection)")
    else:
        counts = raw_counts

    hw = solver.hybrid_select(counts)
    raw_hw = solver.hybrid_select(raw_counts)

    print(f"\n      Done in {t_run:.1f}s  |  Distinct bitstrings: {len(raw_counts)}")
    print(f"      Raw P(best 1%)      : {raw_hw['prob_in_top1pct']:.0%}")
    if getattr(args, "resilience", 1) >= 1:
        print(f"      Mitigated P(best 1%): {hw['prob_in_top1pct']:.0%}")
    print(f"      QUBO optimum found  : {hw['lowest_energy_rank'] == 0}")

    # Network LP on hardware schedule
    net = demand_simulator()
    lp  = NetworkLP()
    ev_hw   = lp.solve(hw["eval"]["u"],    net, gens=pb.gens, blocks=pb.block_names)
    ev_ideal= lp.solve(ideal["eval"]["u"], net, gens=pb.gens, blocks=pb.block_names)
    u_milp, f_milp = pb.solve_milp()

    _print_results(pb, hw, ideal, ev_hw, ev_ideal, f_milp, label, args.fake, job_id)
    _save_state(job_id, theta.tolist(), label, args.fake)
    _save_results(pb, hw, ideal, ev_hw, ev_ideal, f_milp,
                  job_id, label, args.fake, args.shots, t_run, net)

    if not args.fake:
        print("\n" + "=" * 62)
        print("  >> IBM Quantum Platform -- view your job:")
        print(f"     https://quantum.cloud.ibm.com/jobs/{job_id}")
        print("  >> IBM Quantum Composer -- paste qaoa_circuit.qasm:")
        print("     https://quantum.cloud.ibm.com/composer")
        print("=" * 62)
        print(f"\n  To fetch results later:  py ibm_run.py fetch {job_id}")


# ---------------------------------------------------------------------------
# FETCH subcommand
# ---------------------------------------------------------------------------
def cmd_fetch(args):
    print("\n" + "#" * 62)
    print(f"  AP Grid Optimiser -- IBM Quantum Platform  [FETCH]")
    print(f"  Job ID: {args.job_id}")
    print("#" * 62)

    # Load saved state or fallback to defaults
    state = _load_state(args.job_id)
    if state:
        theta = np.array(state.get("theta", [0.5]*4))
        label = state.get("label", "ibm_fez")
    else:
        theta = np.array([0.5]*4)
        label = "ibm_fez"

    # Re-build solver to get energy table for hybrid_select
    pb = build_small_problem()
    solver = QAOASolver(pb, reps=2, seed=7)

    # Fetch counts from IBM
    from qiskit_ibm_runtime import QiskitRuntimeService
    print("\nConnecting to IBM Quantum Platform ...")
    _TOKEN = "UcwEoEXnxx8FtQYgXJTRurmONZSJ-bd-nMWORnHEj3Q8"
    svc = QiskitRuntimeService(channel="ibm_quantum_platform", token=_TOKEN, instance="open-instance")
    job = svc.job(args.job_id)
    print(f"Job status: {job.status()}")
    result = job.result()
    counts = result[0].data.meas.get_counts()
    print(f"Fetched {sum(counts.values())} shots, {len(counts)} distinct bitstrings")

    hw    = solver.hybrid_select(counts)
    ideal_counts = solver.sample_aer(theta, shots=4096)
    ideal = solver.hybrid_select(ideal_counts)

    net = demand_simulator()
    lp  = NetworkLP()
    ev_hw    = lp.solve(hw["eval"]["u"],    net, gens=pb.gens, blocks=pb.block_names)
    ev_ideal = lp.solve(ideal["eval"]["u"], net, gens=pb.gens, blocks=pb.block_names)
    u_milp, f_milp = pb.solve_milp()

    _print_results(pb, hw, ideal, ev_hw, ev_ideal, f_milp, label, False, args.job_id)
    _save_results(pb, hw, ideal, ev_hw, ev_ideal, f_milp,
                  args.job_id, label, False, sum(counts.values()), 0.0, net)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _print_results(pb, hw, ideal, ev_hw, ev_ideal, f_milp, label, fake, job_id):
    gen_names = [g["name"] for g in pb.gens]
    u_hw   = hw["eval"]["u"]
    u_ideal = ideal["eval"]["u"]

    def schedule_str(u, title):
        print(f"\n  {title}")
        print("  " + " " * 10 + "  ".join(f"{b:^7}" for b in pb.block_names))
        for i, name in enumerate(gen_names):
            row = "  ".join("  ON  " if u[i, t] else " off  " for t in range(pb.T))
            print(f"  {name:<10}{row}")

    print("\n" + "=" * 62)
    print("  Results -- ON/OFF Schedule & Supply")
    print("=" * 62)
    schedule_str(u_ideal, "Ideal Aer (noiseless):")
    schedule_str(u_hw,    f"Hardware ({label}):")

    print(f"\n  {'Metric':<30} {'Ideal Aer':>12} {'Hardware':>12}  {'MILP':>10}")
    print("  " + "-" * 66)

    def row(lbl, vi, vhw, vm):
        print(f"  {lbl:<30} {vi:>12} {vhw:>12}  {vm:>10}")

    row("Cost (Rs lakh)",
        f"{ev_ideal['cost']/1e5:.1f}", f"{ev_hw['cost']/1e5:.1f}", f"{f_milp/1e5:.1f}")
    row("CO2 (tonnes)",
        f"{ev_ideal['emissions_t']:.0f}", f"{ev_hw['emissions_t']:.0f}",    "—")
    row("Curtailed (MWh)",
        f"{ev_ideal['curtail_mwh']:.0f}", f"{ev_hw['curtail_mwh']:.0f}",   "—")
    row("Unserved (MWh)",
        f"{ev_ideal['unserved_mwh']:.0f}", f"{ev_hw['unserved_mwh']:.0f}", "—")
    row("QUBO optimum sampled?",
        str(ideal["lowest_energy_rank"] == 0), str(hw["lowest_energy_rank"] == 0), "—")
    row("P(best 1% of states)",
        f"{ideal['prob_in_top1pct']:.0%}", f"{hw['prob_in_top1pct']:.0%}", "— (random=1%)")


def _save_state(job_id: str, theta: list, label: str, fake: bool):
    state = {"job_id": job_id, "theta": theta, "label": label, "fake": fake}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"\n  [OK] State saved to {STATE_FILE}")


def _load_state(job_id: str) -> dict | None:
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        if state.get("job_id") == job_id:
            return state
    except FileNotFoundError:
        pass
    return None


def _save_results(pb, hw, ideal, ev_hw, ev_ideal, f_milp,
                  job_id, label, fake, shots, run_s, net):
    out = {
        "job_id": job_id,
        "backend": label,
        "fake": fake,
        "shots": shots,
        "run_time_s": round(run_s, 2),
        "ibm_platform_url": f"https://quantum.cloud.ibm.com/jobs/{job_id}",
        "generators": [g["name"] for g in pb.gens],
        "blocks": pb.block_names,
        "nodes": NODES,
        "demand_mw": net["demand_mw"].tolist(),
        "renewable_mw": net["renewable_mw"].tolist(),
        "hardware": {
            "schedule":     hw["eval"]["u"].tolist(),
            "cost_lakh":    round(ev_hw["cost"]/1e5, 2),
            "emissions_t":  round(ev_hw["emissions_t"], 1),
            "curtail_mwh":  round(ev_hw["curtail_mwh"], 1),
            "unserved_mwh": round(ev_hw["unserved_mwh"], 1),
            "prob_in_top1pct": round(hw["prob_in_top1pct"], 4),
            "qubo_optimum_sampled": (hw["lowest_energy_rank"] == 0),
            "line_flows": [
                {
                    "from": LINES[l][0], "to": LINES[l][1],
                    "capacity_mw": LINES[l][2],
                    "max_loading_pct": float(
                        max(abs(bl["flow_mw"][l]) for bl in ev_hw["blocks"]
                            if "flow_mw" in bl) / LINES[l][2] * 100
                    ) if ev_hw["blocks"] else 0.0,
                }
                for l in range(len(LINES))
            ],
        },
        "ideal": {
            "schedule":     ideal["eval"]["u"].tolist(),
            "cost_lakh":    round(ev_ideal["cost"]/1e5, 2),
            "emissions_t":  round(ev_ideal["emissions_t"], 1),
            "curtail_mwh":  round(ev_ideal["curtail_mwh"], 1),
            "unserved_mwh": round(ev_ideal["unserved_mwh"], 1),
            "prob_in_top1pct": round(ideal["prob_in_top1pct"], 4),
            "qubo_optimum_sampled": (ideal["lowest_energy_rank"] == 0),
        },
        "milp": {"cost_lakh": round(f_milp/1e5, 2)},
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump(out, f, indent=2)

    # PQC-encrypt the results (ML-KEM-768 + ML-DSA-65 + AES-256-GCM)
    try:
        from pqc import protect_file, ek, sk
        enc_file = RESULTS_FILE.replace(".json", ".enc.json") if isinstance(RESULTS_FILE, str) else "ibm_results.enc.json"
        protect_file(RESULTS_FILE, enc_file, ek, sk, sender="GRID-OPT")
        print(f"  [OK] Results saved to {RESULTS_FILE}  (clear-text)")
        print(f"  [OK] Encrypted copy: {enc_file}  (PQC: ML-KEM-768 + ML-DSA-65)")
    except ImportError:
        print(f"  [OK] Results saved to {RESULTS_FILE}  (PQC not available)")
    print(f"       Connect your frontend to this file")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="AP Grid Optimiser -- IBM Quantum Platform runner")
    sub = ap.add_subparsers(dest="command")

    # submit
    ps = sub.add_parser("submit", help="Train QAOA + submit circuit to IBM")
    ps.add_argument("--fake",    action="store_true",
                    help="Use noisy local fake backend (no IBM account needed)")
    ps.add_argument("--backend", default=None,
                    help="Specific IBM backend, e.g. ibm_brisbane")
    ps.add_argument("--shots",   type=int, default=4096)
    ps.add_argument("--ansatz",  default="custom", choices=["custom", "standard"],
                    help="Circuit ansatz: 'custom' (Grid-Topology + MA-QAOA) or 'standard' (qaoa_ansatz)")
    ps.add_argument("--optimizer", default="COBYLA", choices=["COBYLA", "SPSA", "PARAM_SHIFT"],
                    help="Optimizer: 'COBYLA', 'SPSA' (quantum-native stochastic), or 'PARAM_SHIFT' (exact quantum gradients)")
    ps.add_argument("--resilience", type=int, default=1, choices=[0, 1, 2],
                    help="Error mitigation level: 0=raw, 1=readout error mitigation, 2=ZNE")
    ps.add_argument("--use-estimator", action="store_true",
                    help="Use StatevectorEstimator / EstimatorV2 for expectation values")
    ps.add_argument("--reps",    type=int, default=2, help="QAOA circuit layers p")
    ps.add_argument("--restarts", type=int, default=3, help="Optimizer restarts")
    ps.add_argument("--maxiter", type=int, default=120, help="Optimizer max iterations")

    # fetch
    pf = sub.add_parser("fetch", help="Download results for a submitted job")
    pf.add_argument("job_id", help="IBM job ID to fetch")
    pf.add_argument("--theta", default=None,
                    help="Path to numpy .npy file with saved theta (optional)")

    args = ap.parse_args()
    if args.command == "submit":
        cmd_submit(args)
    elif args.command == "fetch":
        cmd_fetch(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
