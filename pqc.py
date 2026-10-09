"""
pqc.py  –  Post-Quantum Cryptography module for FallF Grid Optimiser
=====================================================================

Uses NIST-standardised ML-KEM-768 (Kyber) for key encapsulation and
ML-DSA-65 (Dilithium) for digital signatures, with AES-256-GCM for
symmetric encryption.

WHY THIS MATTERS
  Power-grid dispatch schedules are critical national infrastructure.
  A quantum-capable adversary could break RSA/ECDH today and decrypt
  harvested ciphertexts once a CRQC is available ("harvest now, decrypt
  later").  ML-KEM + ML-DSA give us quantum-resistant confidentiality
  AND authenticity right now.

PUBLIC API
  protect(payload, ek, sk, sender)     → encrypted package (dict of bytes)
  unprotect(pkg, dk, vk)               → original payload (dict)
  protect_file(src, dst, ek, sk)       → encrypts a JSON file on disk
  unprotect_file(src, dst, dk, vk)     → decrypts it back
  load_or_create_keys(path)            → (ek, dk, vk, sk) with persistence

Run standalone to self-test:
  python pqc.py
"""
from __future__ import annotations

import os
import sys
import io
import json
import time
import base64
import pickle
from pathlib import Path
from typing import Union

from kyber_py.ml_kem import ML_KEM_768
from dilithium_py.ml_dsa import ML_DSA_65
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# ──────────────────────────────────────────────────────────────────────────────
# Key management  –  persist keys so every run uses the same identity
# ──────────────────────────────────────────────────────────────────────────────
_DEFAULT_KEY_FILE = Path(__file__).with_name("pqc_keys.bin")


def load_or_create_keys(
    path: Union[str, os.PathLike] = _DEFAULT_KEY_FILE,
) -> tuple:
    """
    Return (ek, dk, vk, sk).
    If `path` exists, load from it; otherwise generate fresh keys and save.
    """
    path = Path(path)
    if path.is_file():
        with open(path, "rb") as f:
            keys = pickle.load(f)
        return keys["ek"], keys["dk"], keys["vk"], keys["sk"]
    # First run – generate and persist
    ek, dk = ML_KEM_768.keygen()
    vk, sk = ML_DSA_65.keygen()
    with open(path, "wb") as f:
        pickle.dump({"ek": ek, "dk": dk, "vk": vk, "sk": sk}, f)
    return ek, dk, vk, sk


# Module-level keys (lazy-loaded on first import)
ek, dk, vk, sk = load_or_create_keys()


# ──────────────────────────────────────────────────────────────────────────────
# Core  protect / unprotect  (your original API, unchanged)
# ──────────────────────────────────────────────────────────────────────────────
def protect(payload: dict, ek=ek, sk=sk, sender: str = "DISCOM-01") -> dict:
    """Encrypt + sign a JSON-serialisable payload dict.

    Returns a dict whose values are raw bytes (kem_ct, nonce, ct, sig)
    plus a string `sender`.
    """
    data = json.dumps(
        {**payload, "ts": time.time(), "sender": sender}
    ).encode()
    sig = ML_DSA_65.sign(sk, data)                          # sign plaintext
    key, kem_ct = ML_KEM_768.encaps(ek)                     # 32-byte shared key
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, data, sender.encode())  # AEAD
    return dict(kem_ct=kem_ct, nonce=nonce, ct=ct, sig=sig, sender=sender)


def unprotect(pkg: dict, dk=dk, vk=vk) -> dict:
    """Decrypt + verify.  Raises on tamper."""
    key = ML_KEM_768.decaps(dk, pkg["kem_ct"])
    data = AESGCM(key).decrypt(
        pkg["nonce"], pkg["ct"], pkg["sender"].encode()
    )
    ok = ML_DSA_65.verify(vk, data, pkg["sig"])
    if not ok:
        raise ValueError("PQC signature verification FAILED – data tampered!")
    return json.loads(data)


# ──────────────────────────────────────────────────────────────────────────────
# Serialisation helpers  –  bytes ↔ base64 so the package is JSON-safe
# ──────────────────────────────────────────────────────────────────────────────
def _b64(v):
    """Encode bytes to base64 string; pass strings through."""
    return base64.b64encode(v).decode() if isinstance(v, (bytes, bytearray)) else v


def _unb64(v):
    """Decode base64 string back to bytes; pass bytes through."""
    return base64.b64decode(v) if isinstance(v, str) else v


def package_to_json(pkg: dict) -> dict:
    """Convert a protect() package to a JSON-serialisable dict."""
    return {k: _b64(v) for k, v in pkg.items()}


def package_from_json(d: dict) -> dict:
    """Reverse of package_to_json."""
    out = {}
    for k, v in d.items():
        if k == "sender":
            out[k] = v          # sender stays as string
        else:
            out[k] = _unb64(v)  # everything else is base64-encoded bytes
    return out


# ──────────────────────────────────────────────────────────────────────────────
# File-level helpers  –  encrypt / decrypt any JSON file on disk
# ──────────────────────────────────────────────────────────────────────────────
def protect_file(
    src_path: Union[str, os.PathLike],
    dst_path: Union[str, os.PathLike],
    ek=ek, sk=sk,
    sender: str = "GRID-OPT",
) -> None:
    """Read a JSON file, PQC-encrypt it, write the encrypted JSON package."""
    with open(src_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    pkg = protect(payload, ek=ek, sk=sk, sender=sender)
    with open(dst_path, "w", encoding="utf-8") as f:
        json.dump(package_to_json(pkg), f, indent=2)


def unprotect_file(
    src_path: Union[str, os.PathLike],
    dst_path: Union[str, os.PathLike],
    dk=dk, vk=vk,
) -> dict:
    """Read an encrypted JSON package, decrypt + verify, write clear JSON.

    Also returns the decrypted payload dict.
    """
    with open(src_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    pkg = package_from_json(raw)
    payload = unprotect(pkg, dk=dk, vk=vk)
    with open(dst_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return payload


# ──────────────────────────────────────────────────────────────────────────────
# Self-test  (run: python pqc.py)
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Force UTF-8 on Windows
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")

    print("=" * 60)
    print("  PQC Self-Test  (ML-KEM-768 + ML-DSA-65 + AES-256-GCM)")
    print("=" * 60)

    # 1. In-memory round-trip
    sample = {"schedule": [[1, 0, 1], [0, 1, 1]], "cost_lakh": 352.7}
    pkg = protect(sample, ek, sk, sender="TEST-DISCOM")
    recovered = unprotect(pkg, dk, vk)
    assert recovered["schedule"] == sample["schedule"], "schedule mismatch!"
    assert recovered["cost_lakh"] == sample["cost_lakh"], "cost mismatch!"
    print("  [OK] In-memory protect/unprotect round-trip passed")

    # 2. JSON serialisation round-trip
    j = package_to_json(pkg)
    assert all(isinstance(v, str) for v in j.values()), "not all strings!"
    pkg2 = package_from_json(j)
    recovered2 = unprotect(pkg2, dk, vk)
    assert recovered2["schedule"] == sample["schedule"]
    print("  [OK] JSON serialisation round-trip passed")

    # 3. File-level round-trip
    tmp_plain = Path("_pqc_test_plain.json")
    tmp_enc   = Path("_pqc_test_enc.json")
    tmp_dec   = Path("_pqc_test_dec.json")
    with open(tmp_plain, "w") as f:
        json.dump(sample, f)
    protect_file(tmp_plain, tmp_enc, ek, sk)
    dec = unprotect_file(tmp_enc, tmp_dec, dk, vk)
    assert dec["schedule"] == sample["schedule"]
    print("  [OK] File-level protect/unprotect round-trip passed")
    # clean up
    for p in [tmp_plain, tmp_enc, tmp_dec]:
        p.unlink(missing_ok=True)

    # 4. Tamper detection
    pkg_bad = dict(pkg)
    pkg_bad["ct"] = pkg_bad["ct"][:-1] + bytes([pkg_bad["ct"][-1] ^ 0xFF])
    try:
        unprotect(pkg_bad, dk, vk)
        print("  [FAIL] Tamper was NOT detected!")
    except Exception:
        print("  [OK] Tamper detection works (corrupted ciphertext rejected)")

    # 5. Key persistence
    ek2, dk2, vk2, sk2 = load_or_create_keys()
    assert ek2 == ek and dk2 == dk, "key persistence broken!"
    print("  [OK] Key persistence verified (pqc_keys.bin)")

    print("\n  All PQC tests PASSED")
    print("  Keys stored in:", _DEFAULT_KEY_FILE)
    print("=" * 60)