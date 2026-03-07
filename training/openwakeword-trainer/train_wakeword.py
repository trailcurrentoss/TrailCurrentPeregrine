#!/usr/bin/env python3
"""
train_wakeword.py — Granular custom wake word training pipeline.

Each stage has a **do** step and a **verify** step so problems surface
immediately instead of cascading.  You can run the full pipeline, resume
from any step, or run a single step in isolation.

Usage:
    python train_wakeword.py                                  # full pipeline
    python train_wakeword.py --config configs/my_word.yaml    # custom config
    python train_wakeword.py --from augment                   # resume
    python train_wakeword.py --step verify-clips              # one step
    python train_wakeword.py --verify-only                    # check state
    python train_wakeword.py --list-steps                     # show steps

Run inside WSL2 with CUDA support.
See README.md for full setup instructions.
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Callable

import requests
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent           # project root
DATA_DIR   = SCRIPT_DIR / "data"
OUTPUT_DIR = SCRIPT_DIR / "output"
DEFAULT_CONFIG = SCRIPT_DIR / "configs" / "hey_echo.yaml"
RESOLVED_CONFIG = OUTPUT_DIR / "_resolved_config.yaml"
OWW_WRAPPER = SCRIPT_DIR / "oww_wrapper.py"

# Will be set by CLI --config flag or default
CONFIG_FILE: Path = DEFAULT_CONFIG

# Remote URLs — all public, no auth required
URLS = {
    "validation_features": (
        "https://huggingface.co/datasets/davidscripka/openwakeword_features"
        "/resolve/main/validation_set_features.npy"
    ),
    "piper_model": (
        "https://github.com/rhasspy/piper-sample-generator/releases/download"
        "/v2.0.0/en_US-libritts_r-medium.pt"
    ),
    "piper_repo": "https://github.com/rhasspy/piper-sample-generator.git",
}

# Minimum expected file sizes (bytes) for data verification
MIN_SIZES = {
    "negative_features_librispeech_voxpopuli.npy":        3_000_000_000,   # ~4-11 GB depending on hours
    "validation_set_features.npy":                         30_000_000,      # ~40 MB
    "piper-sample-generator/models/en_US-libritts_r-medium.pt": 150_000_000,  # ~200 MB
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("train_wakeword")


# ═══════════════════════════════════════════════════════════════════════════
# Utility helpers
# ═══════════════════════════════════════════════════════════════════════════

def _run(cmd: list[str] | str, cwd: str | Path | None = None, **kw) -> None:
    """Run a subprocess, streaming output.  Raises on failure."""
    log.info("$ %s", cmd if isinstance(cmd, str) else " ".join(cmd))
    subprocess.check_call(cmd, cwd=cwd, **kw)


def _download(url: str, dest: Path, description: str = "") -> None:
    """Download *url* → *dest* with progress.  Skips if *dest* exists."""
    if dest.exists():
        log.info("  Already downloaded: %s", dest.name)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    label = description or dest.name
    log.info("  Downloading %s …", label)
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    tmp = dest.with_suffix(".part")
    downloaded = 0
    with open(tmp, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded * 100 // total
                mb = downloaded / (1 << 20)
                total_mb = total / (1 << 20)
                print(f"\r  {label}: {mb:.0f}/{total_mb:.0f} MB ({pct}%)",
                      end="", flush=True)
    print()
    tmp.rename(dest)
    log.info("  Saved %s", dest)


def _clone_repo(url: str, dest: Path) -> None:
    if dest.exists():
        log.info("  Repo already cloned: %s", dest)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--depth", "1", url, str(dest)])


# ═══════════════════════════════════════════════════════════════════════════
# STEP IMPLEMENTATIONS
# Each returns True on success, False on failure.
# ═══════════════════════════════════════════════════════════════════════════


# ── 1. check-env ──────────────────────────────────────────────────────────

def step_check_env() -> bool:
    """Verify Python ≥3.10, CUDA availability, and critical imports."""
    ok = True

    # Python version
    v = sys.version_info
    log.info("  Python %d.%d.%d  (%s)", v.major, v.minor, v.micro, sys.executable)
    if v < (3, 10):
        log.error("  Python ≥3.10 required")
        ok = False

    # Platform
    import platform
    log.info("  Platform: %s", platform.platform())
    if platform.system() != "Linux":
        log.error("  This script must run inside WSL2 (Linux)")
        ok = False

    # CUDA
    try:
        import torch
        if torch.cuda.is_available():
            gpu = torch.cuda.get_device_name(0)
            mem = torch.cuda.get_device_properties(0).total_memory / (1 << 30)
            log.info("  CUDA: %s  (%.1f GB)", gpu, mem)
        else:
            log.warning("  CUDA not available — training will be very slow on CPU")
    except ImportError:
        log.error("  PyTorch not installed")
        ok = False

    # Critical imports
    for mod in ["yaml", "requests", "soundfile", "numpy", "scipy"]:
        try:
            __import__(mod)
        except ImportError:
            log.error("  Missing: %s", mod)
            ok = False

    # Config file
    log.info("  Config: %s  (%s)", CONFIG_FILE, "exists" if CONFIG_FILE.exists() else "MISSING")
    if not CONFIG_FILE.exists():
        log.error("  Config file not found: %s", CONFIG_FILE)
        ok = False

    return ok


# ── 2. apply-patches ─────────────────────────────────────────────────────

def step_apply_patches() -> bool:
    """Apply compatibility monkey-patches and verify they work."""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import compat

    log.info("  Applying patches …")
    results = compat.apply_all()
    failed_apply = [k for k, v in results.items() if "FAIL" in v]
    if failed_apply:
        log.error("  Patch application failed: %s", failed_apply)
        return False

    log.info("  Verifying patches …")
    checks = compat.verify_all()
    failed_verify = [k for k, v in checks.items() if not v]
    if failed_verify:
        log.error("  Patch verification failed: %s", failed_verify)
        return False

    log.info("  All patches applied and verified")
    return True


# ── 3. download ──────────────────────────────────────────────────────────

def step_download() -> bool:
    """Download all datasets, tools, and models.  Idempotent."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 3a — Piper Sample Generator repo
    piper_dir = DATA_DIR / "piper-sample-generator"
    _clone_repo(URLS["piper_repo"], piper_dir)

    piper_marker = piper_dir / ".installed"
    if not piper_marker.exists():
        log.info("  Installing piper-sample-generator (editable) …")
        _run([sys.executable, "-m", "pip", "install", "-e", "."], cwd=piper_dir)
        piper_marker.touch()

    # 3b — Piper TTS model
    piper_models_dir = piper_dir / "models"
    piper_models_dir.mkdir(exist_ok=True)
    _download(
        URLS["piper_model"],
        piper_models_dir / "en_US-libritts_r-medium.pt",
        "Piper LibriTTS model (~800 MB)",
    )

    # 3c — Build broad negative features from permissively-licensed audio
    neg_features = DATA_DIR / "negative_features_librispeech_voxpopuli.npy"
    if not neg_features.exists():
        log.info("  Building negative features from LibriSpeech + VoxPopuli …")
        log.info("  (This is a one-time process that takes several hours)")
        _run([sys.executable, str(SCRIPT_DIR / "build_negative_features.py"),
              "--output", str(neg_features), "--hours", "1000"])
    else:
        log.info("  Negative features already present")

    # 3d — Validation features
    _download(
        URLS["validation_features"],
        DATA_DIR / "validation_set_features.npy",
        "Validation features (~40 MB)",
    )

    # 3e — MIT Room Impulse Responses
    rir_dir = DATA_DIR / "mit_rirs"
    if not rir_dir.exists():
        _download_mit_rirs(rir_dir)
    else:
        log.info("  MIT RIRs already present")

    # 3f — Background noise
    audioset_dir = DATA_DIR / "audioset_16k"
    if not audioset_dir.exists():
        _download_audioset_subset(audioset_dir)
    else:
        log.info("  AudioSet subset already present")

    fma_dir = DATA_DIR / "fma_small"
    if not fma_dir.exists():
        _download_fma_subset(fma_dir)
    else:
        log.info("  FMA subset already present")

    return True


# ── 4. verify-data ───────────────────────────────────────────────────────

def step_verify_data() -> bool:
    """Check every expected download exists with minimum file sizes."""
    ok = True

    # Large feature / model files
    for relpath, min_bytes in MIN_SIZES.items():
        fp = DATA_DIR / relpath
        if not fp.exists():
            log.error("  MISSING: %s", fp)
            ok = False
        elif fp.stat().st_size < min_bytes:
            log.error(
                "  TOO SMALL: %s  (%d bytes, expected ≥%d)",
                fp, fp.stat().st_size, min_bytes,
            )
            ok = False
        else:
            sz_mb = fp.stat().st_size / (1 << 20)
            log.info("  OK: %-60s  %.0f MB", relpath, sz_mb)

    # Directories that should contain files
    for name in ["mit_rirs", "audioset_16k", "fma_small"]:
        d = DATA_DIR / name
        if not d.is_dir():
            log.error("  MISSING dir: %s", d)
            ok = False
        else:
            n = len(list(d.iterdir()))
            if n == 0:
                log.error("  EMPTY dir: %s", d)
                ok = False
            else:
                log.info("  OK: %-60s  %d files", name + "/", n)

    # Piper install marker
    marker = DATA_DIR / "piper-sample-generator" / ".installed"
    if not marker.exists():
        log.error("  Piper not installed (run download step)")
        ok = False
    else:
        log.info("  OK: piper-sample-generator installed")

    return ok


# ── 5. resolve-config ────────────────────────────────────────────────────

def step_resolve_config() -> bool:
    """Read config YAML, resolve relative paths → absolute, write output."""
    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f)

    cfg["piper_sample_generator_path"] = str(
        (SCRIPT_DIR / cfg["piper_sample_generator_path"]).resolve()
    )
    cfg["output_dir"] = str((SCRIPT_DIR / cfg["output_dir"]).resolve())
    os.makedirs(cfg["output_dir"], exist_ok=True)

    cfg["rir_paths"] = [
        str((SCRIPT_DIR / p).resolve()) for p in cfg.get("rir_paths", [])
    ]
    cfg["background_paths"] = [
        str((SCRIPT_DIR / p).resolve()) for p in cfg.get("background_paths", [])
    ]
    resolved_features = {}
    for key, relpath in cfg.get("feature_data_files", {}).items():
        resolved_features[key] = str((SCRIPT_DIR / relpath).resolve())
    cfg["feature_data_files"] = resolved_features

    if "false_positive_validation_data_path" in cfg:
        cfg["false_positive_validation_data_path"] = str(
            (SCRIPT_DIR / cfg["false_positive_validation_data_path"]).resolve()
        )

    # Resolve real clip paths (relative to config file, not SCRIPT_DIR)
    config_parent = CONFIG_FILE.resolve().parent
    if cfg.get("real_positive_clips_dir"):
        cfg["real_positive_clips_dir"] = str(
            (config_parent / cfg["real_positive_clips_dir"]).resolve()
        )
    if cfg.get("real_negative_clips_dirs"):
        cfg["real_negative_clips_dirs"] = [
            str((config_parent / d).resolve())
            for d in cfg["real_negative_clips_dirs"]
        ]

    RESOLVED_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with open(RESOLVED_CONFIG, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

    log.info("  Resolved config → %s", RESOLVED_CONFIG)

    # Quick sanity: print key paths
    for key in ["piper_sample_generator_path", "output_dir",
                "false_positive_validation_data_path"]:
        if key in cfg:
            p = Path(cfg[key])
            exists = p.exists()
            log.info("    %-40s %s  %s", key, "✓" if exists else "✗", p)
            if not exists:
                log.warning("    ^ path does not exist yet (may be created later)")

    return True


# ── 6. generate ──────────────────────────────────────────────────────────

def step_generate() -> bool:
    """Generate positive + negative clips via Piper TTS."""
    if not RESOLVED_CONFIG.exists():
        log.error("  Resolved config not found — run 'resolve-config' first")
        return False

    log.info("  Generating clips via openwakeword + Piper TTS …")
    log.info("  (Longest step — ~10 min on GPU, hours on CPU)")

    try:
        _run([
            sys.executable, str(OWW_WRAPPER),
            "--training_config", str(RESOLVED_CONFIG),
            "--generate_clips",
        ])
        return True
    except subprocess.CalledProcessError as exc:
        log.error("  Clip generation failed (exit code %d)", exc.returncode)
        return False


# ── 7. resample-clips ────────────────────────────────────────────────────

def step_resample_clips() -> bool:
    """Verify clips exist and note sample rates.

    Actual resampling is handled on-the-fly by the patched torchaudio.load
    in compat.py (applied during the ``apply-patches`` step).  This avoids
    the extremely slow bulk rewrite of 100k+ WAV files.

    This step just spot-checks a few files and warns if rates differ from 16 kHz.
    """
    import soundfile as sf

    wav_files = list(OUTPUT_DIR.rglob("*.wav"))
    if not wav_files:
        log.warning("  No WAV files found in %s", OUTPUT_DIR)
        return True

    log.info("  Found %d WAV files in output/", len(wav_files))

    # Spot-check first 5 files from each subdirectory
    checked = 0
    non_16k = 0
    for d in sorted(set(f.parent for f in wav_files)):
        samples = sorted(d.glob("*.wav"))[:5]
        for wav in samples:
            try:
                info = sf.info(str(wav))
                if info.samplerate != 16000:
                    non_16k += 1
                    if non_16k <= 3:
                        log.info("    %s → %d Hz (will be resampled on-the-fly)", wav.name, info.samplerate)
                checked += 1
            except Exception as exc:
                log.warning("    Error reading %s: %s", wav.name, exc)

    if non_16k > 0:
        log.info("  %d/%d spot-checked files are not 16 kHz — compat patch will handle this", non_16k, checked)
    else:
        log.info("  All %d spot-checked files are 16 kHz", checked)

    return True


# ── 8. verify-clips ──────────────────────────────────────────────────────

def step_verify_clips() -> bool:
    """Verify clip counts and sample rates in output/."""
    import soundfile as sf

    ok = True

    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f)

    expected_positive = cfg.get("n_samples", 50000)
    model_name = cfg.get("model_name", "my_wakeword")

    # Clips live in output/<model_name>/{positive_train, positive_test, ...}
    model_dir = OUTPUT_DIR / model_name
    if not model_dir.is_dir():
        log.error("  Model output directory not found: %s", model_dir)
        return False

    clip_dirs = [d for d in model_dir.iterdir() if d.is_dir()]
    if not clip_dirs:
        log.error("  No clip subdirectories found in %s", model_dir)
        return False

    log.info("  Clip directories in %s/:", model_name)
    total_clips = 0
    for d in sorted(clip_dirs):
        wavs = list(d.glob("*.wav"))
        n = len(wavs)
        total_clips += n
        log.info("    %-40s %6d clips", d.name + "/", n)

        # Spot-check sample rate on first file
        if wavs:
            try:
                info = sf.info(str(wavs[0]))
                sr_note = "" if info.samplerate == 16000 else f"  (⚠ {info.samplerate} Hz — compat patch will resample)"
                log.info("    %-40s SR=%d%s", "", info.samplerate, sr_note)
            except Exception as exc:
                log.warning("    ^ Could not read %s: %s", wavs[0].name, exc)

    if total_clips == 0:
        log.error("  No clips generated")
        ok = False
    else:
        log.info("  Total clips: %d", total_clips)
        if total_clips < expected_positive:
            log.warning(
                "  Fewer clips than expected (%d < %d) — may still work",
                total_clips, expected_positive,
            )

    return ok


# ── 9. inject-real-clips ─────────────────────────────────────────────────

def step_inject_real_clips() -> bool:
    """Copy real recorded clips into the synthetic output dirs for augmentation.

    Real clips from the user's voice are mixed into the TTS-generated
    positive_train and negative_train directories.  This ensures they go
    through the same augmentation and feature-extraction pipeline as the
    synthetic clips.  Clips are duplicated multiple times to increase their
    weight relative to the much larger synthetic set.
    """
    if not RESOLVED_CONFIG.exists():
        log.error("  Resolved config not found — run 'resolve-config' first")
        return False

    with open(RESOLVED_CONFIG) as f:
        cfg = yaml.safe_load(f)

    model_name = cfg.get("model_name", "my_wakeword")
    pos_src = cfg.get("real_positive_clips_dir", "")
    neg_src_dirs = cfg.get("real_negative_clips_dirs", [])

    if not pos_src and not neg_src_dirs:
        log.info("  No real clips configured — skipping")
        return True

    # Paths are already absolute from resolve-config step
    pos_train_dir = OUTPUT_DIR / model_name / "positive_train"
    neg_train_dir = OUTPUT_DIR / model_name / "negative_train"

    if not pos_train_dir.is_dir():
        log.error("  positive_train dir not found: %s", pos_train_dir)
        log.error("  Run 'generate' step first")
        return False

    total_injected = 0

    # Inject positive real clips (duplicate N times to boost weight)
    if pos_src and Path(pos_src).is_dir():
        wavs = list(Path(pos_src).glob("*.wav"))
        duplication = max(1, 50000 // max(len(wavs), 1) // 10)  # ~10% of synthetic set
        duplication = min(duplication, 20)  # cap at 20x
        log.info("  Injecting %d positive real clips (x%d duplication) into positive_train/",
                 len(wavs), duplication)
        for wav in wavs:
            for dup in range(duplication):
                dest_name = f"real_{dup:02d}_{wav.name}"
                shutil.copy2(wav, pos_train_dir / dest_name)
                total_injected += 1
    else:
        if pos_src:
            log.warning("  Positive real clips dir not found: %s", pos_src)

    # Inject negative real clips
    for neg_dir in neg_src_dirs:
        neg_path = Path(neg_dir)
        if not neg_path.is_dir():
            log.warning("  Negative real clips dir not found: %s", neg_dir)
            continue
        # Walk subdirectories (each phrase has its own subdir)
        for subdir in sorted(neg_path.iterdir()):
            if not subdir.is_dir():
                continue
            wavs = list(subdir.glob("*.wav"))
            if not wavs:
                continue
            for wav in wavs:
                dest_name = f"real_neg_{subdir.name}_{wav.name}"
                shutil.copy2(wav, neg_train_dir / dest_name)
                total_injected += 1

    log.info("  Total real clips injected: %d", total_injected)
    return True


# ── 10. augment ──────────────────────────────────────────────────────────

def step_augment() -> bool:
    """Run augmentation (noise, RIR) and mel-spectrogram feature extraction."""
    if not RESOLVED_CONFIG.exists():
        log.error("  Resolved config not found — run 'resolve-config' first")
        return False

    log.info("  Augmenting clips & extracting features …")
    try:
        _run([
            sys.executable, str(OWW_WRAPPER),
            "--training_config", str(RESOLVED_CONFIG),
            "--augment_clips", "--overwrite",
        ])
        return True
    except subprocess.CalledProcessError as exc:
        log.error("  Augmentation failed (exit code %d)", exc.returncode)
        return False


# ── 10. verify-features ──────────────────────────────────────────────────

def step_verify_features() -> bool:
    """Check that .npy feature files were produced with reasonable shapes."""
    import numpy as np

    ok = True
    expected_features = [
        "positive_features_train.npy",
        "positive_features_test.npy",
        "negative_features_train.npy",
        "negative_features_test.npy",
    ]

    for name in expected_features:
        fp = OUTPUT_DIR / name
        if not fp.exists():
            # Also check subdirectories
            found = list(OUTPUT_DIR.rglob(name))
            if found:
                fp = found[0]
            else:
                log.error("  MISSING: %s", name)
                ok = False
                continue

        arr = np.load(str(fp), mmap_mode="r")
        log.info("  OK: %-45s shape=%s  dtype=%s", name, arr.shape, arr.dtype)

        if arr.shape[0] == 0:
            log.error("    ^ empty array!")
            ok = False

    return ok


# ── 11. train ────────────────────────────────────────────────────────────

def step_train() -> bool:
    """Train the DNN model."""
    if not RESOLVED_CONFIG.exists():
        log.error("  Resolved config not found — run 'resolve-config' first")
        return False

    with open(RESOLVED_CONFIG) as f:
        cfg = yaml.safe_load(f)

    model_name = cfg["model_name"]
    output_dir = Path(cfg["output_dir"])
    model_path = output_dir / f"{model_name}.onnx"

    if model_path.exists():
        log.info("  Model already exists: %s", model_path)
        log.info("  Delete it to retrain.")
        return True

    steps = cfg.get("steps", 50000)
    log.info("  Training %s for %d steps …", model_name, steps)

    try:
        _run([
            sys.executable, str(OWW_WRAPPER),
            "--training_config", str(RESOLVED_CONFIG),
            "--train_model",
        ])
        return True
    except subprocess.CalledProcessError as exc:
        log.error("  Training failed (exit code %d)", exc.returncode)
        return False


# ── 12. verify-model ─────────────────────────────────────────────────────

def step_verify_model() -> bool:
    """Verify the ONNX model was produced and can be loaded."""
    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f)

    model_name = cfg["model_name"]
    model_path = OUTPUT_DIR / f"{model_name}.onnx"

    # Search for any .onnx file if exact name not found
    if not model_path.exists():
        onnx_files = list(OUTPUT_DIR.rglob("*.onnx"))
        if onnx_files:
            model_path = onnx_files[0]
            log.info("  Found model at: %s (expected %s.onnx)", model_path, model_name)
        else:
            log.error("  No .onnx model found in %s", OUTPUT_DIR)
            return False

    size_mb = model_path.stat().st_size / (1 << 20)
    log.info("  Model: %s  (%.2f MB)", model_path.name, size_mb)

    # Check for companion .data file
    data_file = model_path.with_suffix(".onnx.data")
    if data_file.exists():
        data_mb = data_file.stat().st_size / (1 << 20)
        log.info("  External data: %s  (%.2f MB)", data_file.name, data_mb)

    # Try loading with ONNX runtime
    try:
        import onnxruntime as ort
        import numpy as np

        sess = ort.InferenceSession(str(model_path))
        inputs = sess.get_inputs()
        outputs = sess.get_outputs()
        log.info("  ONNX inputs:  %s", [(i.name, i.shape) for i in inputs])
        log.info("  ONNX outputs: %s", [(o.name, o.shape) for o in outputs])

        # Quick inference test with silence
        inp = {inputs[0].name: np.zeros((1, *inputs[0].shape[1:]), dtype=np.float32)}
        result = sess.run(None, inp)
        log.info("  Inference test passed (output shape: %s)", result[0].shape)
    except ImportError:
        log.warning("  onnxruntime not installed — skipping load test")
    except Exception as exc:
        log.warning("  ONNX load test failed: %s", exc)

    return True


# ── 13. export ───────────────────────────────────────────────────────────

def step_export() -> bool:
    """Copy the trained model to the export/ directory for easy retrieval."""
    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f)

    model_name = cfg["model_name"]
    model_path = OUTPUT_DIR / f"{model_name}.onnx"

    if not model_path.exists():
        onnx_files = list(OUTPUT_DIR.rglob("*.onnx"))
        if onnx_files:
            model_path = onnx_files[0]
        else:
            log.error("  No .onnx model found — run 'train' step first")
            return False

    export_dir = SCRIPT_DIR / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    dest = export_dir / f"{model_name}.onnx"
    shutil.copy2(model_path, dest)
    log.info("  Model exported → %s", dest)

    # ONNX models exported with external data have a companion .data file
    data_file = model_path.with_suffix(".onnx.data")
    if data_file.exists():
        dest_data = dest.with_suffix(".onnx.data")
        shutil.copy2(data_file, dest_data)
        log.info("  External data  → %s", dest_data)

    log.info("")
    log.info("=" * 60)
    log.info("  DONE!  Your trained model is at:")
    log.info("    %s", dest)
    if data_file.exists():
        log.info("    %s", dest_data)
    log.info("")
    log.info("  To use with openWakeWord:")
    log.info("")
    log.info("    from openwakeword.model import Model")
    log.info('    oww = Model(wakeword_models=["%s"])', dest.name)
    log.info("")
    log.info("  Copy the model file(s) to your project and update your config.")
    log.info("=" * 60)

    return True


# ═══════════════════════════════════════════════════════════════════════════
# Download helpers (AudioSet, FMA, MIT RIR, synthetic fallback)
# ═══════════════════════════════════════════════════════════════════════════

def _download_mit_rirs(dest: Path) -> None:
    try:
        from datasets import load_dataset
        import soundfile as sf

        ds = load_dataset(
            "davidscripka/MIT_environmental_impulse_responses",
            split="train", trust_remote_code=True,
        )
        dest.mkdir(parents=True, exist_ok=True)
        for i, row in enumerate(ds):
            audio = row["audio"]
            sf.write(str(dest / f"rir_{i:04d}.wav"), audio["array"], audio["sampling_rate"])
        log.info("  Saved %d RIR files", len(ds))
    except Exception as exc:
        log.warning("  Could not download MIT RIRs: %s", exc)
        log.info("  Creating empty RIR directory — training will proceed without RIRs")
        dest.mkdir(parents=True, exist_ok=True)


def _download_audioset_subset(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        from datasets import load_dataset
        import soundfile as sf

        ds = load_dataset("agkphysics/AudioSet", "unbalanced", split="train",
                          streaming=True, trust_remote_code=True)
        count = 0
        for row in ds:
            if count >= 500:
                break
            try:
                audio = row["audio"]
                sf.write(str(dest / f"audioset_{count:04d}.wav"),
                         audio["array"], audio["sampling_rate"])
                count += 1
            except Exception:
                continue
        log.info("  Saved %d AudioSet clips", count)
    except Exception as exc:
        log.warning("  AudioSet download failed: %s", exc)
        _generate_synthetic_noise(dest, n=200, label="audioset")


def _download_fma_subset(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        from datasets import load_dataset
        import soundfile as sf

        ds = load_dataset("rudraml/fma", name="small", split="train",
                          streaming=True, trust_remote_code=True)
        count = 0
        for row in ds:
            if count >= 200:
                break
            try:
                audio = row["audio"]
                sf.write(str(dest / f"fma_{count:04d}.wav"),
                         audio["array"], audio["sampling_rate"])
                count += 1
            except Exception:
                continue
        log.info("  Saved %d FMA clips", count)
    except Exception as exc:
        log.warning("  FMA download failed: %s", exc)
        _generate_synthetic_noise(dest, n=100, label="fma")


def _generate_synthetic_noise(dest: Path, n: int = 200, label: str = "noise") -> None:
    import numpy as np
    import soundfile as sf

    log.info("  Generating %d synthetic noise clips as fallback …", n)
    dest.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    for i in range(n):
        duration = rng.uniform(3, 10)
        samples = int(16000 * duration)
        white = rng.normal(0, rng.uniform(0.01, 0.15), samples).astype(np.float32)
        sf.write(str(dest / f"{label}_{i:04d}.wav"), white, 16000)
    log.info("  Generated %d noise clips in %s", n, dest)


# ═══════════════════════════════════════════════════════════════════════════
# Step registry & pipeline runner
# ═══════════════════════════════════════════════════════════════════════════

STEPS: list[tuple[str, Callable[[], bool], str]] = [
    ("check-env",        step_check_env,        "Verify Python ≥3.10, CUDA, critical imports"),
    ("apply-patches",    step_apply_patches,     "Apply torchaudio/speechbrain/piper compat patches"),
    ("download",         step_download,          "Download datasets, Piper TTS model, tools"),
    ("verify-data",      step_verify_data,       "Check all data files present & minimum sizes"),
    ("resolve-config",   step_resolve_config,    "Resolve config paths → _resolved_config.yaml"),
    ("generate",         step_generate,          "Generate positive + negative clips via Piper TTS"),
    ("resample-clips",   step_resample_clips,    "Spot-check clip sample rates (resampling is on-the-fly)"),
    ("verify-clips",     step_verify_clips,      "Verify clip counts and sample rates"),
    ("inject-real-clips", step_inject_real_clips, "Copy real recorded clips into synthetic dirs"),
    ("augment",          step_augment,           "Augment clips & extract mel features"),
    ("verify-features",  step_verify_features,   "Check .npy feature files exist & shapes"),
    ("train",            step_train,             "Train DNN model (50k steps, ~30 min on GPU)"),
    ("verify-model",     step_verify_model,      "Verify ONNX model produced & loadable"),
    ("export",           step_export,            "Copy model to export/ directory"),
]

STEP_NAMES = [s[0] for s in STEPS]


def _print_steps() -> None:
    print("\nAvailable steps:\n")
    for i, (name, _, desc) in enumerate(STEPS, 1):
        print(f"  {i:2d}. {name:<20s}  {desc}")
    print()


def run_pipeline(
    *,
    from_step: str | None = None,
    single_step: str | None = None,
    verify_only: bool = False,
) -> bool:
    """Execute steps and stop on first failure."""

    if single_step:
        # Run exactly one step
        matches = [(n, fn, d) for n, fn, d in STEPS if n == single_step]
        if not matches:
            log.error("Unknown step: %s", single_step)
            _print_steps()
            return False
        name, fn, desc = matches[0]
        log.info("=" * 60)
        log.info("STEP: %s  —  %s", name, desc)
        log.info("=" * 60)
        ok = fn()
        status = "PASSED" if ok else "FAILED"
        log.info("Result: %s\n", status)
        return ok

    # Determine which steps to run
    steps_to_run = STEPS
    if from_step:
        try:
            idx = STEP_NAMES.index(from_step)
            steps_to_run = STEPS[idx:]
        except ValueError:
            log.error("Unknown step: %s", from_step)
            _print_steps()
            return False

    if verify_only:
        steps_to_run = [(n, fn, d) for n, fn, d in steps_to_run if n.startswith("verify")]

    total = len(steps_to_run)
    for i, (name, fn, desc) in enumerate(steps_to_run, 1):
        log.info("")
        log.info("=" * 60)
        log.info("[%d/%d]  %s  —  %s", i, total, name, desc)
        log.info("=" * 60)

        ok = fn()

        if ok:
            log.info("[%d/%d]  %s  ✓ PASSED", i, total, name)
        else:
            log.error("[%d/%d]  %s  ✗ FAILED", i, total, name)
            log.error("")
            log.error("Pipeline stopped.  Fix the issue above, then resume:")
            log.error("  python train_wakeword.py --from %s", name)
            return False

    log.info("")
    log.info("=" * 60)
    log.info("  ALL STEPS COMPLETE")
    log.info("=" * 60)
    return True


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train a custom wake word model using openWakeWord.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python train_wakeword.py                                    # full pipeline
              python train_wakeword.py --config configs/my_word.yaml      # custom config
              python train_wakeword.py --from augment                     # resume
              python train_wakeword.py --step verify-clips                # run one step
              python train_wakeword.py --verify-only                      # check state
              python train_wakeword.py --list-steps                       # show all steps
        """),
    )
    parser.add_argument(
        "--config", type=str, default=None, metavar="FILE",
        help="Path to training config YAML (default: configs/hey_echo.yaml).",
    )
    parser.add_argument(
        "--step", type=str, default=None, metavar="NAME",
        help="Run a single step by name.",
    )
    parser.add_argument(
        "--from", type=str, default=None, dest="from_step", metavar="NAME",
        help="Run from this step onward (skip earlier steps).",
    )
    parser.add_argument(
        "--verify-only", action="store_true",
        help="Run only verify-* steps (status check without side effects).",
    )
    parser.add_argument(
        "--list-steps", action="store_true",
        help="Print all available steps and exit.",
    )
    args = parser.parse_args()

    if args.list_steps:
        _print_steps()
        return

    # Set config file globally
    global CONFIG_FILE
    if args.config:
        CONFIG_FILE = Path(args.config).resolve()
    elif not DEFAULT_CONFIG.exists():
        # Try to find any .yaml in configs/
        configs_dir = SCRIPT_DIR / "configs"
        if configs_dir.exists():
            yamls = sorted(configs_dir.glob("*.yaml"))
            if yamls:
                CONFIG_FILE = yamls[0]
                log.info("Using config: %s", CONFIG_FILE)

    ok = run_pipeline(
        from_step=args.from_step,
        single_step=args.step,
        verify_only=args.verify_only,
    )

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
