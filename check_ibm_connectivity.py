"""
check_ibm_connectivity.py
Verifies IBM Quantum Platform connectivity for the FallF QAOA project.
"""

import sys, os, importlib

API_KEY = "UcwEoEXnxx8FtQYgXJTRurmONZSJ-bd-nMWORnHEj3Q8"

REQUIRED = [
    "qiskit", "qiskit_ibm_runtime", "qiskit_aer",
    "numpy", "scipy", "pandas", "streamlit", "matplotlib", "networkx",
]

print("=" * 60)
print("  FallF  -  IBM Quantum Connectivity Check")
print("=" * 60)

print("\n[1] Package availability")
missing = []
for pkg in REQUIRED:
    try:
        mod = importlib.import_module(pkg)
        ver = getattr(mod, "__version__", "?")
        print(f"  OK  {pkg:<25} {ver}")
    except ImportError:
        print(f"  XX  {pkg:<25} MISSING")
        missing.append(pkg)

if missing:
    print(f"\n  WARN: Install missing packages: pip install {' '.join(missing)}")
else:
    print("\n  All required packages found.")

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CORE_FILES = [
    "app.py","qaoa.py","engine.py","hardware.py","ibm_run.py","compare.py",
    "mitigation.py","tradeoff.py","network.py","grid.py","pipeline.py",
    "run_demo.py","requirements.txt",
    os.path.join("data","apsldc_january_2026.csv"),
    os.path.join("data","apsldc_april_2025.csv"),
]

print("\n[2] Project file integrity")
all_present = True
for f in CORE_FILES:
    full = os.path.join(PROJECT_ROOT, f)
    exists = os.path.isfile(full)
    size = os.path.getsize(full) if exists else 0
    status = f"OK  {size:>7} B" if exists else "XX  MISSING"
    print(f"  {status}   {f}")
    if not exists:
        all_present = False

if all_present:
    print("\n  All core project files present.")
else:
    print("\n  WARN: Some files are missing.")

print("\n[3] IBM Quantum Platform connectivity")
try:
    from qiskit_ibm_runtime import QiskitRuntimeService

    try:
        QiskitRuntimeService.save_account(channel="ibm_quantum_platform", token=API_KEY, overwrite=True, set_as_default=True)
        print("  OK  Account credentials saved/updated.")
    except Exception as e:
        print(f"  WARN save_account: {e}")

    service = QiskitRuntimeService(channel="ibm_quantum_platform", token=API_KEY, instance="open-instance")
    print("  OK  QiskitRuntimeService instantiated successfully.")

    backends = service.backends()
    print(f"\n  Available backends ({len(backends)}):")
    for b in backends:
        try:
            st = b.status()
            st_str = "operational" if st.operational else "offline"
            pj = getattr(st, "pending_jobs", "?")
            print(f"    - {b.name:<35} [{st_str}]  {pj} pending")
        except Exception:
            print(f"    - {b.name:<35} [status unavailable]")

    print("\n  Selecting least-busy backend (>=5 qubits) ...")
    try:
        candidate = service.least_busy(min_num_qubits=5, simulator=False)
        print(f"  OK  Recommended backend: {candidate.name}")
    except Exception as e:
        print(f"  INFO: Could not find real-hardware backend: {e}")

    print("\n  IBM Quantum connectivity CONFIRMED.")

except ImportError:
    print("  XX  qiskit_ibm_runtime not installed.")
except Exception as e:
    print(f"  XX  Connection failed: {e}")

print("\n" + "=" * 60)
print("  Check complete.")
print("=" * 60)
