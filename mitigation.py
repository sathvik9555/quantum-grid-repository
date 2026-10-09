"""
mitigation.py -- Quantum Error Mitigation for AP Grid QAOA.

Provides:
1. Zero Noise Extrapolation (ZNE):
   - Digital unitary gate folding (U -> U U† U) for noise scaling factors λ ∈ {1, 2, 3}
   - Linear and polynomial Richardson extrapolation to the zero-noise limit λ -> 0
2. Readout Error Mitigation:
   - Confusion matrix calibration (tensor-product single-qubit measurement errors)
   - Matrix inversion with simplex projection for physical probabilities
3. Qiskit Runtime Resilience integration:
   - Helper to configure EstimatorV2 with resilience_level=1 (Readout) and resilience_level=2 (ZNE)
"""
from __future__ import annotations

import numpy as np
from qiskit import QuantumCircuit
from qiskit.quantum_info import SparsePauliOp
from qiskit_aer import AerSimulator


# ---------------------------------------------------------------------------
# 1. Zero Noise Extrapolation (ZNE) via Digital Gate Folding
# ---------------------------------------------------------------------------
def fold_circuit(qc: QuantumCircuit, scale_factor: float) -> QuantumCircuit:
    """
    Scale noise by digitally folding 2-qubit gates.
    For an integer scale factor λ = 2k + 1:
        each 2Q gate G is replaced by G (G† G)^k  (identical unitary, amplified noise).
    For non-integer λ, a random fraction of 2Q gates is folded.
    """
    if scale_factor <= 1.01:
        return qc.copy()

    # Determine folding count
    k_base = int((scale_factor - 1.0) / 2.0)
    remainder = ((scale_factor - 1.0) / 2.0) - k_base

    folded = QuantumCircuit(*qc.qregs, *qc.cregs)
    rng = np.random.default_rng(42)

    for instruction in qc.data:
        op = instruction.operation
        qubits = instruction.qubits
        clbits = instruction.clbits

        folded.append(op, qubits, clbits)

        # Only fold 2-qubit gates (dominant noise source in NISQ)
        if len(qubits) == 2:
            num_folds = k_base + (1 if rng.uniform() < remainder else 0)
            for _ in range(num_folds):
                folded.append(op.inverse(), qubits, clbits)
                folded.append(op, qubits, clbits)

    return folded


class ZeroNoiseExtrapolator:
    """
    Performs Zero Noise Extrapolation (ZNE) on expectation values.
    """
    def __init__(self, scale_factors: list[float] | None = None, method: str = "linear"):
        self.scale_factors = scale_factors or [1.0, 2.0, 3.0]
        self.method = method

    def extrapolate(self, lambdas: list[float], values: list[float]) -> tuple[float, dict]:
        """
        Extrapolate measured values at noise scales λ to λ = 0.
        Returns (mitigated_value, fit_info).
        """
        lambdas = np.array(lambdas, dtype=float)
        values = np.array(values, dtype=float)

        if self.method == "linear" or len(lambdas) == 2:
            poly = np.polyfit(lambdas, values, 1)
            mitigated = float(np.polyval(poly, 0.0))
        else:
            poly = np.polyfit(lambdas, values, min(2, len(lambdas) - 1))
            mitigated = float(np.polyval(poly, 0.0))

        return mitigated, {
            "lambdas": lambdas.tolist(),
            "measured_values": values.tolist(),
            "mitigated_value": mitigated,
            "raw_value": float(values[0]),
            "correction": float(mitigated - values[0]),
            "poly_coeffs": poly.tolist(),
        }

    def run_zne_estimator(self, qc: QuantumCircuit, op: SparsePauliOp,
                          estimator, theta: np.ndarray | None = None) -> dict:
        """
        Evaluates the expectation value across folded circuits using a modern Estimator.
        """
        measured_vals = []
        for s in self.scale_factors:
            folded_qc = fold_circuit(qc, s)
            if theta is not None:
                # bind if parameterized
                bound_qc = folded_qc.assign_parameters(theta) if folded_qc.parameters else folded_qc
            else:
                bound_qc = folded_qc
            pub = (bound_qc, op)
            job = estimator.run([pub])
            val = float(job.result()[0].data.evs)
            measured_vals.append(val)

        mitigated, info = self.extrapolate(self.scale_factors, measured_vals)
        return info


# ---------------------------------------------------------------------------
# 2. Readout Error Mitigation (Matrix Inversion + Simplex Projection)
# ---------------------------------------------------------------------------
class ReadoutMitigator:
    """
    Calibrates single-qubit measurement confusion matrices and corrects raw counts.
    """
    def __init__(self, num_qubits: int, p01: float = 0.02, p10: float = 0.03):
        """
        p01: Probability of measuring 1 when state was 0 (false excitation)
        p10: Probability of measuring 0 when state was 1 (relaxation)
        """
        self.num_qubits = num_qubits
        self.p01 = p01
        self.p10 = p10
        # 1-qubit assignment matrix: M[meas, prep]
        self.M1 = np.array([[1.0 - p01, p10],
                            [p01,       1.0 - p10]])
        self.M1_inv = np.linalg.inv(self.M1)

    def mitigate_counts(self, raw_counts: dict[str, int]) -> dict[str, float]:
        """
        Applies tensored single-qubit matrix inversion to bitstring probabilities,
        followed by Euclidean projection onto the probability simplex.
        """
        total = sum(raw_counts.values())
        if total == 0:
            return {}

        n = self.num_qubits
        dim = 2 ** n

        # Convert counts to probability vector
        p_raw = np.zeros(dim)
        for bs, cnt in raw_counts.items():
            k = int(bs.replace(" ", ""), 2)
            if k < dim:
                p_raw[k] = cnt / total

        # Tensored inversion bit by bit for efficiency
        p_mit = p_raw.copy()
        for qubit in range(n):
            stride = 2 ** qubit
            for block in range(0, dim, 2 * stride):
                for offset in range(stride):
                    idx0 = block + offset
                    idx1 = idx0 + stride
                    v = np.array([p_mit[idx0], p_mit[idx1]])
                    v_corr = self.M1_inv @ v
                    p_mit[idx0] = v_corr[0]
                    p_mit[idx1] = v_corr[1]

        # Simplex projection (clip negative values and normalize)
        p_mit = np.maximum(p_mit, 0.0)
        s = p_mit.sum()
        if s > 0:
            p_mit /= s
        else:
            p_mit = p_raw

        # Rebuild counts dict
        mitigated_counts = {}
        for k in range(dim):
            if p_mit[k] > 1e-6:
                bs = format(k, f"0{n}b")
                mitigated_counts[bs] = float(p_mit[k] * total)

        return mitigated_counts


# ---------------------------------------------------------------------------
# 3. IBM Runtime Resilience Level Configuration
# ---------------------------------------------------------------------------
def configure_runtime_resilience(options, resilience_level: int = 1):
    """
    Configures Qiskit Runtime options for error mitigation:
        Level 0: No mitigation
        Level 1: Readout Error Mitigation (Twirled Readout / M3)
        Level 2: Zero Noise Extrapolation (ZNE) + Gate Twirling
    """
    if hasattr(options, "resilience_level"):
        options.resilience_level = resilience_level
    if hasattr(options, "resilience"):
        options.resilience.measure_mitigation = (resilience_level >= 1)
        if resilience_level >= 2:
            options.resilience.zne_mitigation = True
    return options
