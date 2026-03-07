#!/usr/bin/env python3
"""oww_wrapper.py — Run ``openwakeword.train`` with compat patches.

This wrapper applies monkey-patches for torchaudio 2.10+, speechbrain, and
piper-sample-generator BEFORE openwakeword is imported.  All ``sys.argv``
arguments are forwarded transparently.

Usage (instead of ``python -m openwakeword.train``):
    python oww_wrapper.py --training_config ... --generate_clips
"""

import os
import sys

# Ensure the project directory is on sys.path so ``import compat`` works.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# ── Apply patches BEFORE any openwakeword / speechbrain imports ──
import compat  # noqa: E402

compat.apply_all()

# ── Verify patches are functional ──
results = compat.verify_all()
failed = [k for k, v in results.items() if not v]
if failed:
    print(f"WARNING: Some compat patches failed verification: {failed}", file=sys.stderr)
    print("Training may still work — proceeding.", file=sys.stderr)

# ── Delegate to openwakeword.train as if called via ``python -m`` ──
import runpy  # noqa: E402

sys.argv[0] = "openwakeword.train"
runpy.run_module("openwakeword.train", run_name="__main__", alter_sys=True)
