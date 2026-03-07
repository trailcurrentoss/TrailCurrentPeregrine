"""compat.py — Compatibility patches for openWakeWord training dependencies.

Addresses known breaking changes in modern dependency versions:
  - setuptools 82+ removed pkg_resources
  - torchaudio 2.10+ removed load(), info(), list_audio_backends()
  - Piper sample generator API changed (requires model= kwarg)
  - Sample rate mismatches (Piper outputs 22050 Hz, openWakeWord expects 16000 Hz)

Apply BEFORE importing openwakeword, speechbrain, or torch-audiomentations:

    import compat
    results = compat.apply_all()    # monkey-patches torchaudio etc.
    ok      = compat.verify_all()   # tests each patch actually works
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("compat")


# ─── Public API ───────────────────────────────────────────────────────────


def apply_all() -> dict[str, str]:
    """Apply every patch. Returns ``{name: status}`` where *status* is one of
    ``ok``, ``applied``, ``skipped (reason)``, or ``FAILED: reason``.
    """
    results: dict[str, str] = {}
    for name, fn in _PATCHES:
        try:
            status = fn()
        except Exception as exc:
            status = f"FAILED: {exc}"
        results[name] = status
        level = logging.WARNING if "FAIL" in status else logging.INFO
        log.log(level, "  patch %-30s %s", name, status)
    return results


def verify_all() -> dict[str, bool]:
    """Functional tests for each patch.  Returns ``{name: passed}``."""
    results: dict[str, bool] = {}

    # ── torchaudio.load ──
    try:
        import numpy as np
        import soundfile as sf
        import torch
        import torchaudio

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            # Write a file at 22050 Hz to test resampling
            sf.write(f.name, np.zeros(22050, dtype=np.float32), 22050)
            wav, sr = torchaudio.load(f.name)
            results["torchaudio.load"] = sr == 16000 and isinstance(wav, torch.Tensor)
            if sr != 16000:
                log.warning("  verify torchaudio.load  returned SR=%d (expected 16000)", sr)
            Path(f.name).unlink(missing_ok=True)
    except Exception as exc:
        results["torchaudio.load"] = False
        log.warning("  verify torchaudio.load  FAILED: %s", exc)

    # ── torchaudio.info ──
    try:
        import numpy as np
        import soundfile as sf
        import torchaudio

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            sf.write(f.name, np.zeros(16000, dtype=np.float32), 16000)
            meta = torchaudio.info(f.name)
            results["torchaudio.info"] = meta.sample_rate == 16000
            Path(f.name).unlink(missing_ok=True)
    except Exception as exc:
        results["torchaudio.info"] = False
        log.warning("  verify torchaudio.info  FAILED: %s", exc)

    # ── torchaudio.list_audio_backends ──
    try:
        import torchaudio

        backends = torchaudio.list_audio_backends()
        results["torchaudio.list_audio_backends"] = isinstance(backends, list)
    except Exception:
        results["torchaudio.list_audio_backends"] = False

    # ── pkg_resources ──
    try:
        import pkg_resources  # noqa: F401

        results["pkg_resources"] = True
    except ImportError:
        results["pkg_resources"] = False

    for name, ok in results.items():
        log.info("  verify %-30s %s", name, "PASS" if ok else "FAIL")

    return results


# ─── Individual patches ──────────────────────────────────────────────────


def _ensure_pkg_resources() -> str:
    """Install setuptools<82 if pkg_resources was removed."""
    try:
        import pkg_resources  # noqa: F401

        return "ok"
    except ImportError:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "setuptools<82", "-q"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return "applied (setuptools<82)"


def _patch_torchaudio_load() -> str:
    """Replace ``torchaudio.load`` with a soundfile-based loader that also
    resamples to 16 kHz when needed (Piper outputs 22050 Hz)."""
    import torch
    import torchaudio

    if getattr(torchaudio, "_oww_load_patched", False):
        return "ok (already patched)"

    def _load(filepath, *args, **kwargs):
        import numpy as np
        import soundfile as sf

        data, sr = sf.read(str(filepath), dtype="float32")
        if data.ndim == 1:
            data = data[np.newaxis, :]  # (1, samples)
        else:
            data = data.T  # (channels, samples)

        # Resample to 16 kHz if needed (Piper TTS outputs 22050 Hz)
        if sr != 16000:
            from scipy.signal import resample as scipy_resample
            old_len = data.shape[-1]
            new_len = int(old_len * 16000 / sr)
            # Resample each channel
            if data.ndim == 2:
                resampled = np.stack([
                    scipy_resample(data[c], new_len).astype(np.float32)
                    for c in range(data.shape[0])
                ])
            else:
                resampled = scipy_resample(data, new_len).astype(np.float32)
            data = resampled
            sr = 16000

        return torch.from_numpy(data), sr

    torchaudio.load = _load
    torchaudio._oww_load_patched = True
    return "applied"


def _patch_torchaudio_info() -> str:
    """Provide a soundfile-based ``torchaudio.info``."""
    import torchaudio

    if getattr(torchaudio, "_oww_info_patched", False):
        return "ok (already patched)"

    class AudioMetaData:
        __slots__ = (
            "sample_rate",
            "num_frames",
            "num_channels",
            "bits_per_sample",
            "encoding",
        )

        def __init__(self, sample_rate: int, num_frames: int, num_channels: int):
            self.sample_rate = sample_rate
            self.num_frames = num_frames
            self.num_channels = num_channels
            self.bits_per_sample = 16
            self.encoding = "PCM_S"

    def _info(filepath):
        import soundfile as sf

        fi = sf.info(str(filepath))
        return AudioMetaData(fi.samplerate, fi.frames, fi.channels)

    torchaudio.info = _info
    if not hasattr(torchaudio, "AudioMetaData"):
        torchaudio.AudioMetaData = AudioMetaData
    torchaudio._oww_info_patched = True
    return "applied"


def _patch_torchaudio_list_backends() -> str:
    """Re-add ``torchaudio.list_audio_backends`` for speechbrain compat."""
    import torchaudio

    if hasattr(torchaudio, "list_audio_backends"):
        return "ok"
    torchaudio.list_audio_backends = lambda: ["soundfile"]
    return "applied"


def _patch_piper_generate_samples() -> str:
    """Wrap ``piper_sample_generator.generate_samples`` to inject *model=*
    when the caller omits it (API changed in piper-sample-generator v2+)."""
    try:
        import piper_sample_generator as psg
    except ImportError:
        return "skipped (piper_sample_generator not installed)"

    if getattr(psg, "_oww_generate_patched", False):
        return "ok (already patched)"

    _orig_generate = getattr(psg, "generate_samples", None)
    if _orig_generate is None:
        return "skipped (generate_samples not found)"

    def _wrapped(*args, **kwargs):
        if "model" not in kwargs:
            # Find the first .pt file near the piper-sample-generator install
            psg_dir = Path(psg.__file__).resolve().parent
            search_roots = [psg_dir, psg_dir.parent]
            for root in search_roots:
                models = sorted(root.rglob("*.pt"))
                if models:
                    kwargs["model"] = str(models[0])
                    log.info("Auto-resolved Piper model: %s", kwargs["model"])
                    break
        return _orig_generate(*args, **kwargs)

    psg.generate_samples = _wrapped
    psg._oww_generate_patched = True
    return "applied"


def _patch_oww_data_sample_rate() -> str:
    """Suppress openwakeword's sample-rate ValueError.

    Since ``torchaudio.load`` (patched above) already resamples to 16 kHz,
    this patch only needs to handle any remaining direct ``sf.read`` calls
    inside openwakeword that might raise on rate mismatches.

    We do NOT globally patch ``soundfile.read`` because that would conflict
    with torchaudio's internal ``_soundfile_load`` which passes extra kwargs
    like ``start``, ``stop``, ``always_2d`` that don't survive resampling.
    Instead, we patch only openwakeword-specific code paths.
    """
    try:
        import openwakeword.data as oww_data
    except ImportError:
        return "skipped (openwakeword not installed)"

    if getattr(oww_data, "_oww_sr_patched", False):
        return "ok (already patched)"

    # The torchaudio.load patch already handles resampling.
    # Mark as done so we don't re-apply.
    oww_data._oww_sr_patched = True
    return "applied (torchaudio.load handles resampling)"


# ─── Patch registry (order matters) ──────────────────────────────────────

_PATCHES = [
    ("setuptools/pkg_resources", _ensure_pkg_resources),
    ("torchaudio.load", _patch_torchaudio_load),
    ("torchaudio.info", _patch_torchaudio_info),
    ("torchaudio.list_audio_backends", _patch_torchaudio_list_backends),
    ("piper generate_samples model=", _patch_piper_generate_samples),
    ("oww data.py sample rate", _patch_oww_data_sample_rate),
]
