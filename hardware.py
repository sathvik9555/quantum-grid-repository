"""
IBM Quantum Platform Backend Runner — Unit Commitment QAOA

PURPOSE
-------
1. Train QAOA parameters on the local exact statevector simulator (fast, noiseless)
2. Submit the trained circuit to IBM Quantum Platform for REAL hardware sampling
3. Retrieve job results and print direct IBM Platform URLs to view:
      - Circuit diagram   (Composer)
      - Measurement histogram
      - Job runtime + gate statistics
4. Print ON/OFF schedule and supply optimisation summary
5. Save full results to ibm_results.json (for your custom frontend)

USAGE
-----
  py hardware.py                         # real IBM hardware (least-busy backend)
  py hardware.py --backend ibm_brisbane  # specific backend
  py hardware.py --fake                  # local noisy fake backend (no IBM account needed)
  py hardware.py --shots 8192            # more shots for better statistics

ONE-TIME ACCOUNT SETUP (only needed once)
  from qiskit_ibm_runtime import QiskitRuntimeService
  QiskitRuntimeService.save_account(token="YOUR_API_KEY", overwrite=True)

WHAT YOU SEE ON IBM PLATFORM
------------------------------
After running, open the printed IBM Platform URL to see:
  - Transpiled circuit with gate-level diagram
  - Measurement histogram (bitstring probabilities)
  - Job runtime, queue time, backend info
  - 2Q gate counts and circuit depth

The instance is 9 qubits (3 plants x 3 time blocks) — sized for NISQ hardware.
Parameters are trained on the exact simulator; only SAMPLING runs on real hardware.
"""
from __future__ import annotations

import sys
import io
# Force UTF-8 output on Windows so Unicode characters don't crash cp1252
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
import json
import time
import numpy as np

from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from grid import GENS, Problem
from qaoa import QAOASolver


# ──────────────────────────────────────────────────────────────────────────────
# Small problem instance (9 qubits: 3 generators × 3 time blocks)
# ──────────────────────────────────────────────────────────────────────────────

def build_problem():
    """3-generator, 3-block subset sized for NISQ hardware."""
    gens = [GENS[0], GENS[2], GENS[3]]   # Coal-1, Gas, Hydro
    return Problem(gens=gens, blocks=[0, 1, 2],
                   penalty=500.0, nominal_frac=0.75, reserve=0.05)


# ──────────────────────────────────────────────────────────────────────────────
# Step 1 — Train QAOA parameters on exact statevector simulator
# ──────────────────────────────────────────────────────────────────────────────

def train_qaoa(pb: Problem, reps: int = 2, restarts: int = 3, maxiter: int = 120,
               ansatz_type: str = "custom", optimizer: str = "COBYLA",
               use_estimator: bool = False):
    """Returns a trained QAOASolver and the ideal (Aer) result dict."""
    print("\n" + "=" * 60)
    ansatz_str = "Custom Grid-Topology + MA-QAOA" if ansatz_type == "custom" else "Standard qaoa_ansatz"
    print(f"  STEP 1 -- Train QAOA ({ansatz_str}) with {optimizer}")
    print("=" * 60)
    solver = QAOASolver(pb, reps=reps, ansatz_type=ansatz_type)
    t0 = time.time()
    theta, e_train = solver.train(restarts=restarts, maxiter=maxiter,
                                  optimizer=optimizer, use_estimator=use_estimator)
    elapsed = time.time() - t0
    print(f"  Qubits        : {pb.n}")
    print(f"  Ansatz        : {ansatz_type.upper()} ({len(solver.params)} params, {reps} layers)")
    print(f"  Optimizer     : {optimizer}")
    print(f"  Train time    : {elapsed:.1f}s")
    print(f"  <E> achieved  : {e_train/1e5:.2f} lakh   "
          f"(random={solver.E.mean()/1e5:.2f}, optimum={solver.E.min()/1e5:.2f})")
    return solver, theta


# ──────────────────────────────────────────────────────────────────────────────
# Step 2 — Build transpiled circuit and pick backend
# ──────────────────────────────────────────────────────────────────────────────

def get_backend_and_circuit(solver: QAOASolver, theta, shots: int,
                             fake: bool, backend_name: str | None):
    qc = solver._bind(theta)
    qc.measure_all()

    from qiskit import qasm3
    with open("qaoa_circuit.qasm", "w") as f:
        qasm3.dump(qc, f)
    print("  [OK] Saved circuit to qaoa_circuit.qasm (paste into IBM Quantum Composer)")

    if fake:
        from qiskit_ibm_runtime.fake_provider import FakeSherbrooke
        from qiskit_ibm_runtime import SamplerV2 as Sampler
        backend = FakeSherbrooke()
        label = "FAKE ibm_sherbrooke (noisy local simulation)"
    else:
        from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2 as Sampler
        _TOKEN = "UcwEoEXnxx8FtQYgXJTRurmONZSJ-bd-nMWORnHEj3Q8"
        service = QiskitRuntimeService(channel="ibm_quantum_platform", token=_TOKEN, instance="open-instance")
        if backend_name:
            backend = service.backend(backend_name)
        else:
            backend = service.least_busy(
                operational=True, simulator=False,
                min_num_qubits=solver.n
            )
        label = backend.name

    pm = generate_preset_pass_manager(optimization_level=3, backend=backend)
    isa = pm.run(qc)
    sampler = (lambda b: b)(backend)   # re-bound below

    print("\n" + "=" * 60)
    print("  STEP 2 -- Transpile circuit for backend")
    print("=" * 60)
    print(f"  Backend       : {label}")
    print(f"  Circuit depth : {isa.depth()}")
    cx  = isa.count_ops().get("cx",  0)
    ecr = isa.count_ops().get("ecr", 0)
    cz  = isa.count_ops().get("cz",  0)
    try:
        from qiskit.qasm2 import dumps as qasm2_dumps
        qasm_str = qasm2_dumps(isa)
        with open("qaoa_circuit.qasm", "w") as f:
            f.write(qasm_str)
        print("  [OK] Saved transpiled OpenQASM circuit to qaoa_circuit.qasm")
    except Exception:
        pass

    print(f"  Shots planned : {shots}")

    if fake:
        from qiskit_ibm_runtime import SamplerV2 as Sampler
        sampler = Sampler(mode=backend)
    else:
        from qiskit_ibm_runtime import SamplerV2 as Sampler
        sampler = Sampler(mode=backend)

    return sampler, isa, backend, label


# ──────────────────────────────────────────────────────────────────────────────
# Step 3 — Submit to IBM and wait for results
# ──────────────────────────────────────────────────────────────────────────────

def submit_and_fetch(sampler, isa, shots: int, fake: bool, theta=None, label="ibm_fez"):
    """Submit job; return (counts dict, job_id, queue_s, run_s)."""
    print("\n" + "=" * 60)
    print("  STEP 3 -- Submit to IBM Quantum Platform")
    print("=" * 60)
    t_submit = time.time()
    job = sampler.run([isa], shots=shots)
    job_id = getattr(job, "job_id", lambda: "local")()
    print(f"  Job submitted!  ID = {job_id}")

    # Persist job state immediately so it can be fetched even if internet drops
    if not fake and job_id != "local":
        try:
            state = {"job_id": job_id, "theta": theta.tolist() if theta is not None else [0.5]*4, "label": label, "fake": fake}
            with open("ibm_job_state.json", "w") as f:
                json.dump(state, f, indent=2)
            print(f"  [OK] Saved job state to ibm_job_state.json")
        except Exception:
            pass

    if not fake:
        print(f"\n  >> View circuit + histogram on IBM Platform:")
        print(f"     https://quantum.cloud.ibm.com/jobs/{job_id}")
        print(f"\n  Waiting for results (may queue on real hardware) ...")

    try:
        result = job.result()
    except Exception as e:
        print("\n" + "!" * 60)
        print("  [NETWORK / TIMEOUT NOTICE]")
        print("  Internet connection was lost or timed out while waiting for IBM Quantum.")
        print(f"  Your job IS safely submitted and running on IBM servers!")
        print(f"  Job ID : {job_id}")
        print(f"  URL    : https://quantum.cloud.ibm.com/jobs/{job_id}")
        print("\n  Once network connection is restored, fetch results with:")
        print(f"     python ibm_run.py fetch {job_id}")
        print("!" * 60 + "\n")
        raise SystemExit(1) from e

    elapsed = time.time() - t_submit
    counts = result[0].data.meas.get_counts()
    queue_s = 0.0
    run_s = elapsed
    print(f"  Done!  Elapsed: {elapsed:.1f}s  |  Distinct bitstrings: {len(counts)}")
    return counts, job_id, queue_s, run_s


# ──────────────────────────────────────────────────────────────────────────────
# Step 4 — ON/OFF schedule + supply optimisation summary
# ──────────────────────────────────────────────────────────────────────────────

def print_schedule_and_optimisation(pb: Problem, solver: QAOASolver,
                                    counts: dict, ideal_counts: dict,
                                    label: str):
    """Hybrid-select best schedule, print ON/OFF grid and supply analysis."""
    hw   = solver.hybrid_select(counts)
    ideal = solver.hybrid_select(ideal_counts)
    u_milp, f_milp = pb.solve_milp()

    gen_names = [g["name"] for g in pb.gens]
    blocks    = pb.block_names
    u_hw   = hw["eval"]["u"]
    u_ideal = ideal["eval"]["u"]

    def schedule_table(u, title):
        print(f"\n  {title}")
        print("  " + " " * 12 + "  ".join(f"{b:^7}" for b in blocks))
        for i, name in enumerate(gen_names):
            row = "  ".join("  ON  " if u[i, t] else " off  " for t in range(pb.T))
            print(f"  {name:<12}{row}")

    print("\n" + "=" * 60)
    print("  STEP 4 -- ON/OFF Schedule & Supply Optimisation")
    print("=" * 60)
    schedule_table(u_ideal, "Ideal Aer schedule (noiseless simulator):")
    schedule_table(u_hw,    f"Hardware schedule ({label}):")

    print(f"\n  {'Metric':<32} {'Ideal Aer':>12} {'Hardware':>12}  {'Exact MILP':>12}")
    print("  " + "-" * 70)

    def row(label_, vi, vhw, vm):
        print(f"  {label_:<32} {vi:>12} {vhw:>12}  {vm:>12}")

    ev_i  = ideal["eval"]
    ev_hw = hw["eval"]
    ev_m  = pb.evaluate(u_milp)

    row("Daily cost  (Rs lakh)",
        f"{ev_i['cost']/1e5:.1f}", f"{ev_hw['cost']/1e5:.1f}", f"{f_milp/1e5:.1f}")
    row("CO2 emissions  (tonnes)",
        f"{ev_i['emissions_t']:.0f}", f"{ev_hw['emissions_t']:.0f}", f"{ev_m['emissions_t']:.0f}")
    row("Curtailed renewables (MWh)",
        f"{ev_i['curtail_mwh']:.0f}", f"{ev_hw['curtail_mwh']:.0f}", f"{ev_m['curtail_mwh']:.0f}")
    row("Unserved energy  (MWh)",
        f"{ev_i['unserved_mwh']:.0f}", f"{ev_hw['unserved_mwh']:.0f}", f"{ev_m['unserved_mwh']:.0f}")
    row("QUBO optimum sampled?",
        str(ideal["lowest_energy_rank"] == 0),
        str(hw["lowest_energy_rank"] == 0), "—")
    row("P(best 1% of states)",
        f"{ideal['prob_in_top1pct']:.0%}", f"{hw['prob_in_top1pct']:.0%}", "—  (random=1%)")

    # Per-block supply vs demand breakdown
    print("\n  ── Supply vs Demand (MW) — Hardware schedule ──")
    print("  " + f"{'Block':^8} {'Demand':>8} {'Thermal':>9} {'Renewable':>11} {'Total':>8} {'Status':>10}")
    for t in range(pb.T):
        d   = ev_hw["p"][:, t].sum()
        r   = ev_hw["r_used"][t]
        dem = pb.D[t]
        status = "OK" if ev_hw["unserved_mwh"] < 1 else "UNSERVED"
        print(f"  {blocks[t]:^8} {dem:>8.0f} {d:>9.0f} {r:>11.0f} {d+r:>8.0f} {status:>10}")

    return hw, ideal, ev_m


# ──────────────────────────────────────────────────────────────────────────────
# Step 5 — Save results JSON for custom frontend
# ──────────────────────────────────────────────────────────────────────────────

def save_results(pb, hw, ideal, ev_milp, job_id, label,
                 run_s, shots, fake):
    out = dict(
        job_id=job_id,
        backend=label,
        fake=fake,
        shots=shots,
        run_time_s=round(run_s, 2),
        ibm_platform_url=f"https://quantum.cloud.ibm.com/jobs/{job_id}",
        hardware=dict(
            schedule=hw["eval"]["u"].tolist(),
            cost_lakh=round(hw["eval"]["cost"] / 1e5, 2),
            emissions_t=round(hw["eval"]["emissions_t"], 1),
            curtail_mwh=round(hw["eval"]["curtail_mwh"], 1),
            unserved_mwh=round(hw["eval"]["unserved_mwh"], 1),
            prob_in_top1pct=round(hw["prob_in_top1pct"], 4),
            qubo_optimum_sampled=(hw["lowest_energy_rank"] == 0),
        ),
        ideal=dict(
            schedule=ideal["eval"]["u"].tolist(),
            cost_lakh=round(ideal["eval"]["cost"] / 1e5, 2),
            emissions_t=round(ideal["eval"]["emissions_t"], 1),
            curtail_mwh=round(ideal["eval"]["curtail_mwh"], 1),
            unserved_mwh=round(ideal["eval"]["unserved_mwh"], 1),
            prob_in_top1pct=round(ideal["prob_in_top1pct"], 4),
            qubo_optimum_sampled=(ideal["lowest_energy_rank"] == 0),
        ),
        milp=dict(
            cost_lakh=round(ev_milp["cost"] / 1e5, 2),
            emissions_t=round(ev_milp["emissions_t"], 1),
        ),
        generators=[g["name"] for g in pb.gens],
        blocks=pb.block_names,
        demand_mw=pb.D.tolist(),
        solar_mw=pb.solar.tolist(),
        wind_mw=pb.wind.tolist(),
    )
    with open("ibm_results.json", "w") as f:
        json.dump(out, f, indent=2)

    # PQC-encrypt the results (ML-KEM-768 + ML-DSA-65 + AES-256-GCM)
    try:
        from pqc import protect_file, ek, sk
        protect_file("ibm_results.json", "ibm_results.enc.json", ek, sk,
                      sender="GRID-OPT")
        print("\n  [OK] Saved ibm_results.json  (clear-text)")
        print("  [OK] Saved ibm_results.enc.json  (PQC-encrypted, ML-KEM-768 + ML-DSA-65)")
    except ImportError:
        print("\n  [OK] Saved ibm_results.json  (PQC not available – install kyber-py, dilithium-py)")
    print("       Connect your custom frontend to this file")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Run QAOA unit-commitment circuit on IBM Quantum Platform")
    ap.add_argument("--fake",    action="store_true",
                    help="Use a noisy fake backend locally (no IBM account needed)")
    ap.add_argument("--backend", default=None,
                    help="Specific IBM backend name, e.g. ibm_brisbane")
    ap.add_argument("--shots",   type=int, default=4096,
                    help="Number of measurement shots (default 4096)")
    ap.add_argument("--reps",    type=int, default=2,
                    help="QAOA circuit layers p (default 2)")
    ap.add_argument("--ansatz",  default="custom", choices=["custom", "standard"],
                    help="Circuit ansatz: 'custom' (Grid-Topology + MA-QAOA) or 'standard' (qaoa_ansatz)")
    ap.add_argument("--optimizer", default="COBYLA", choices=["COBYLA", "SPSA", "PARAM_SHIFT"],
                    help="Optimizer: 'COBYLA', 'SPSA' (quantum-native stochastic), or 'PARAM_SHIFT' (exact quantum gradients)")
    ap.add_argument("--resilience", type=int, default=1, choices=[0, 1, 2],
                    help="Error mitigation level: 0=raw, 1=readout error mitigation, 2=ZNE")
    ap.add_argument("--use-estimator", action="store_true",
                    help="Use StatevectorEstimator for expectation values")
    ap.add_argument("--maxiter", type=int, default=120,
                    help="Max optimizer iterations")
    ap.add_argument("--restarts", type=int, default=3,
                    help="Number of optimizer restarts")
    a = ap.parse_args()

    print("\n" + "#" * 60)
    print("  Hybrid Quantum Grid Optimiser -- IBM Quantum Backend")
    print("  Unit Commitment: 3 generators x 3 time blocks (9 qubits)")
    print("#" * 60)

    pb = build_problem()

    # 1. Train on simulator
    solver, theta = train_qaoa(pb, reps=a.reps, ansatz_type=a.ansatz,
                               optimizer=a.optimizer, use_estimator=a.use_estimator,
                               maxiter=a.maxiter, restarts=a.restarts)

    # 2. Ideal Aer sampling (noiseless reference)
    print("\n  Running ideal Aer reference (noiseless) ...")
    ideal_counts = solver.sample_aer(theta, shots=a.shots)

    # 3. Transpile + pick backend
    sampler, isa, backend, label = get_backend_and_circuit(
        solver, theta, a.shots, a.fake, a.backend)

    # 4. Submit and wait
    raw_counts, job_id, q_s, run_s = submit_and_fetch(sampler, isa, a.shots, a.fake)

    if a.resilience >= 1:
        from mitigation import ReadoutMitigator
        mitigator = ReadoutMitigator(num_qubits=pb.n)
        counts = mitigator.mitigate_counts(raw_counts)
        print("  [OK] Readout error mitigation applied (M3/inversion + simplex projection)")
    else:
        counts = raw_counts

    # 5. Print schedule + supply analysis
    hw, ideal, ev_milp = print_schedule_and_optimisation(
        pb, solver, counts, ideal_counts, label)

    # 6. IBM Platform link
    if not a.fake:
        print("\n" + "=" * 60)
        print("  >> IBM Quantum Platform -- view your job:")
        print(f"     https://quantum.cloud.ibm.com/jobs/{job_id}")
        print("  >> IBM Quantum Composer -- paste qaoa_circuit.qasm:")
        print("     https://quantum.cloud.ibm.com/composer")
        print("=" * 60)

    # 7. Save JSON for custom frontend
    save_results(pb, hw, ideal, ev_milp, job_id, label,
                 run_s, a.shots, a.fake)


if __name__ == "__main__":
    main()
