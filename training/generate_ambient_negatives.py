#!/usr/bin/env python3
"""Generate ambient noise clips as negative wake word examples.

The wake word model triggers on silence and ambient noise because all existing
negative examples are speech.  This script:

  1. Generates synthetic noise clips (silence, white/pink/brown noise, hum,
     fan, road, rain) via numpy
  2. Downloads real-world noise recordings from MS-SNSD (MIT licensed) and
     slices them into training clips

Output goes into real_clips_negative/ subdirectories so the existing training
pipeline picks them up automatically.

Usage:
    python generate_ambient_negatives.py [--count 2000] [--no-download]
"""

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_BASE = SCRIPT_DIR / "real_clips_negative"

SR = 16000  # 16 kHz mono, matches training pipeline


def silence(rng, n_samples):
    """Near-zero amplitude with a tiny noise floor."""
    level = rng.uniform(1e-5, 5e-4)
    return rng.normal(0, level, n_samples).astype(np.float32)


def white_noise(rng, n_samples):
    """White noise at a random level."""
    level = rng.uniform(0.005, 0.15)
    return rng.normal(0, level, n_samples).astype(np.float32)


def pink_noise(rng, n_samples):
    """Pink noise (1/f) via Voss-McCartney algorithm."""
    n_rows = 16
    n_cols = n_samples
    array = rng.standard_normal((n_rows, n_cols))
    cols = np.empty(n_cols)
    col_sum = np.zeros(n_cols)
    for i in range(n_rows):
        stride = 1 << i
        array_row = array[i]
        held = array_row[0]
        for j in range(n_cols):
            if j % stride == 0:
                held = array_row[j % len(array_row)]
            col_sum[j] += held
    cols = col_sum / n_rows
    # Normalize and scale
    cols = cols / (np.abs(cols).max() + 1e-8)
    level = rng.uniform(0.005, 0.12)
    return (cols * level).astype(np.float32)


def brown_noise(rng, n_samples):
    """Brown (Brownian/red) noise — integrated white noise."""
    white = rng.normal(0, 1, n_samples)
    brown = np.cumsum(white)
    # Normalize
    brown = brown / (np.abs(brown).max() + 1e-8)
    level = rng.uniform(0.005, 0.10)
    return (brown * level).astype(np.float32)


def hum_60hz(rng, n_samples):
    """60 Hz electrical hum with harmonics."""
    t = np.arange(n_samples) / SR
    fundamental = rng.uniform(59.5, 60.5)
    sig = np.sin(2 * np.pi * fundamental * t)
    # Add 2nd and 3rd harmonics
    sig += rng.uniform(0.2, 0.6) * np.sin(2 * np.pi * 2 * fundamental * t)
    sig += rng.uniform(0.05, 0.3) * np.sin(2 * np.pi * 3 * fundamental * t)
    level = rng.uniform(0.005, 0.08)
    sig = sig / (np.abs(sig).max() + 1e-8) * level
    # Add a tiny noise floor
    sig += rng.normal(0, level * 0.1, n_samples)
    return sig.astype(np.float32)


def fan_noise(rng, n_samples):
    """Simulated fan / HVAC — low-pass filtered noise."""
    white = rng.normal(0, 1, n_samples)
    # Simple low-pass via exponential moving average
    alpha = rng.uniform(0.01, 0.05)  # lower = more bass-heavy
    filtered = np.empty(n_samples)
    filtered[0] = white[0]
    for i in range(1, n_samples):
        filtered[i] = alpha * white[i] + (1 - alpha) * filtered[i - 1]
    filtered = filtered / (np.abs(filtered).max() + 1e-8)
    level = rng.uniform(0.01, 0.12)
    return (filtered * level).astype(np.float32)


def road_noise(rng, n_samples):
    """Road/wind noise — bandpass-ish filtered noise (100-800 Hz emphasis)."""
    white = rng.normal(0, 1, n_samples)
    # Two-pole IIR bandpass approximation
    # Low-pass at ~800 Hz
    alpha_lp = 2 * np.pi * 800 / SR
    alpha_lp = alpha_lp / (alpha_lp + 1)
    lp = np.empty(n_samples)
    lp[0] = white[0]
    for i in range(1, n_samples):
        lp[i] = alpha_lp * white[i] + (1 - alpha_lp) * lp[i - 1]
    # High-pass at ~100 Hz (subtract low-pass at 100 Hz)
    alpha_hp = 2 * np.pi * 100 / SR
    alpha_hp = alpha_hp / (alpha_hp + 1)
    lp2 = np.empty(n_samples)
    lp2[0] = lp[0]
    for i in range(1, n_samples):
        lp2[i] = alpha_hp * lp[i] + (1 - alpha_hp) * lp2[i - 1]
    bp = lp - lp2
    bp = bp / (np.abs(bp).max() + 1e-8)
    level = rng.uniform(0.01, 0.15)
    return (bp * level).astype(np.float32)


def rain_noise(rng, n_samples):
    """Rain-like noise — white noise with random amplitude modulation."""
    white = rng.normal(0, 1, n_samples)
    # Slow amplitude modulation (random "intensity" changes)
    mod_freq = rng.uniform(0.5, 3.0)
    t = np.arange(n_samples) / SR
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * mod_freq * t + rng.uniform(0, 2 * np.pi))
    # Add some randomness to envelope
    envelope += rng.uniform(-0.1, 0.1, n_samples)
    envelope = np.clip(envelope, 0.1, 1.0)
    sig = white * envelope
    sig = sig / (np.abs(sig).max() + 1e-8)
    level = rng.uniform(0.01, 0.12)
    return (sig * level).astype(np.float32)


# Generator registry: (name, function, weight)
# Weight controls how many clips of each type to generate (relative)
GENERATORS = [
    ("silence",    silence,     3.0),  # Most important — this is what triggers false positives
    ("white_noise", white_noise, 1.5),
    ("pink_noise",  pink_noise,  1.5),
    ("brown_noise", brown_noise, 1.0),
    ("hum_60hz",    hum_60hz,    1.5),
    ("fan_noise",   fan_noise,   2.0),  # Common in RV/trailer environment
    ("road_noise",  road_noise,  2.0),  # Common while driving
    ("rain_noise",  rain_noise,  1.0),
]


def generate_clips(total_count: int, duration_range: tuple[float, float], seed: int = 42):
    """Generate ambient noise clips into real_clips_negative/ subdirs."""
    rng = np.random.default_rng(seed)

    # Distribute count by weight
    total_weight = sum(w for _, _, w in GENERATORS)
    allocations = []
    remaining = total_count
    for i, (name, fn, weight) in enumerate(GENERATORS):
        if i == len(GENERATORS) - 1:
            n = remaining  # last one gets the remainder
        else:
            n = round(total_count * weight / total_weight)
            remaining -= n
        allocations.append((name, fn, n))

    grand_total = 0
    for name, fn, count in allocations:
        out_dir = OUTPUT_BASE / f"ambient_{name}"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Check how many already exist
        existing = len(list(out_dir.glob("*.wav")))
        if existing >= count:
            print(f"  {name}: {existing} clips already exist (need {count}), skipping")
            grand_total += existing
            continue

        to_generate = count - existing
        print(f"  {name}: generating {to_generate} clips ({existing} existing) → {out_dir.name}/")

        for i in range(to_generate):
            dur = rng.uniform(*duration_range)
            n_samples = int(SR * dur)
            audio = fn(rng, n_samples)
            idx = existing + i
            sf.write(str(out_dir / f"ambient_{name}_{idx:04d}.wav"), audio, SR)

        grand_total += count

    print(f"\nTotal ambient negative clips: {grand_total}")
    print(f"Output: {OUTPUT_BASE}/ambient_*/")


def download_mssnsd(clip_duration: float = 3.0, seed: int = 42):
    """Download MS-SNSD (MIT licensed) noise files and slice into clips.

    MS-SNSD contains real recordings of: air conditioner, vacuum cleaner,
    copy machine, washing machine, traffic, neighbor noise, typing, etc.
    All at 16 kHz mono WAV — perfect for our pipeline.

    Repo: https://github.com/microsoft/MS-SNSD  (MIT license)
    """
    mssnsd_cache = SCRIPT_DIR / ".mssnsd_cache"
    noise_dir = mssnsd_cache / "noise_train"

    # Clone if not already cached
    if not noise_dir.is_dir():
        print("\n=== Downloading MS-SNSD (MIT licensed) ===")
        print("  Source: https://github.com/microsoft/MS-SNSD")

        # Shallow clone just the noise files to save bandwidth
        if mssnsd_cache.exists():
            shutil.rmtree(mssnsd_cache)

        subprocess.check_call([
            "git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
            "https://github.com/microsoft/MS-SNSD.git",
            str(mssnsd_cache),
        ])
        subprocess.check_call(
            ["git", "sparse-checkout", "set", "noise_train", "noise_test"],
            cwd=str(mssnsd_cache),
        )
        # Trigger actual download of the noise WAVs
        subprocess.check_call(
            ["git", "checkout"],
            cwd=str(mssnsd_cache),
        )

    # Gather all noise WAV files from both train and test
    noise_files = []
    for subdir in ["noise_train", "noise_test"]:
        d = mssnsd_cache / subdir
        if d.is_dir():
            noise_files.extend(sorted(d.glob("*.wav")))

    if not noise_files:
        print("  WARNING: No WAV files found in MS-SNSD clone")
        return 0

    print(f"\n=== Slicing {len(noise_files)} MS-SNSD recordings into {clip_duration}s clips ===")

    rng = np.random.default_rng(seed)
    total_clips = 0

    for wav_path in noise_files:
        # Derive a category name from the filename (e.g., "AirConditioner_1.wav" -> "airconditioner")
        stem = wav_path.stem.lower()
        # Strip trailing numbers/underscores to group variants
        category = stem.rstrip("0123456789_")
        if not category:
            category = stem

        out_dir = OUTPUT_BASE / f"mssnsd_{category}"
        out_dir.mkdir(parents=True, exist_ok=True)

        existing = len(list(out_dir.glob("*.wav")))

        try:
            data, file_sr = sf.read(str(wav_path), dtype="float32")
        except Exception as e:
            print(f"  Skipping {wav_path.name}: {e}")
            continue

        # Convert to mono if stereo
        if data.ndim > 1:
            data = data.mean(axis=1)

        # Resample to 16kHz if needed
        if file_sr != SR:
            # Simple resampling via linear interpolation
            n_out = int(len(data) * SR / file_sr)
            indices = np.linspace(0, len(data) - 1, n_out)
            data = np.interp(indices, np.arange(len(data)), data).astype(np.float32)

        clip_samples = int(SR * clip_duration)
        n_clips = max(1, len(data) // clip_samples)

        # Skip if we already have enough clips from this file
        if existing >= n_clips:
            total_clips += existing
            continue

        clip_count = 0
        for start in range(0, len(data) - clip_samples + 1, clip_samples):
            clip = data[start:start + clip_samples]
            idx = existing + clip_count
            sf.write(str(out_dir / f"mssnsd_{category}_{idx:04d}.wav"), clip, SR)
            clip_count += 1

        # Also generate a few randomly-offset clips for variety
        n_random = min(n_clips, 5)
        for _ in range(n_random):
            start = rng.integers(0, max(1, len(data) - clip_samples))
            clip = data[start:start + clip_samples]
            # Randomly scale amplitude for variety
            clip = clip * rng.uniform(0.3, 1.0)
            idx = existing + clip_count
            sf.write(str(out_dir / f"mssnsd_{category}_{idx:04d}.wav"), clip, SR)
            clip_count += 1

        print(f"  {wav_path.name} -> {clip_count} clips in {out_dir.name}/")
        total_clips += clip_count

    print(f"  MS-SNSD total: {total_clips} clips")
    return total_clips


def download_musan_noise(clip_duration: float = 3.0, seed: int = 42):
    """Download MUSAN noise subset (CC0) and slice into clips.

    MUSAN contains ~929 noise files (~6 hours): wind, rain, thunder,
    car idling, rustling, crowd noise, DTMF tones, etc.

    Source: https://www.openslr.org/17/  (CC0 / public domain)
    """
    import tarfile
    import urllib.request

    musan_cache = SCRIPT_DIR / ".musan_cache"
    noise_dir = musan_cache / "musan" / "noise"

    if not noise_dir.is_dir():
        print("\n=== Downloading MUSAN noise subset (CC0) ===")
        print("  Source: https://www.openslr.org/17/")

        musan_cache.mkdir(parents=True, exist_ok=True)
        tar_path = musan_cache / "musan.tar.gz"

        if not tar_path.exists():
            url = "https://www.openslr.org/resources/17/musan.tar.gz"
            print(f"  Downloading {url} (~11 GB, extracting noise only) ...")

            # Stream download with progress
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(tar_path, "wb") as f:
                    while True:
                        chunk = resp.read(1024 * 1024)  # 1 MB chunks
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            pct = downloaded * 100 / total
                            print(f"\r  {downloaded / 1024 / 1024:.0f} / {total / 1024 / 1024:.0f} MB ({pct:.1f}%)", end="", flush=True)
                print()

        # Extract only the noise subdirectory
        print("  Extracting noise files from tarball ...")
        with tarfile.open(tar_path, "r:gz") as tar:
            noise_members = [m for m in tar.getmembers() if m.name.startswith("musan/noise/")]
            tar.extractall(path=str(musan_cache), members=noise_members)

        # Clean up tarball to save disk space
        tar_path.unlink()
        print(f"  Extracted {len(noise_members)} files to {noise_dir}")

    # Gather all WAV files from noise subdirectories
    wav_files = sorted(noise_dir.rglob("*.wav"))
    if not wav_files:
        print("  WARNING: No WAV files found in MUSAN noise dir")
        return 0

    print(f"\n=== Slicing {len(wav_files)} MUSAN noise recordings into {clip_duration}s clips ===")

    rng = np.random.default_rng(seed + 1000)  # different seed from MS-SNSD
    total_clips = 0

    for wav_path in wav_files:
        # Category from parent dir name (e.g., "free-sound", "sound-bible")
        category = wav_path.parent.name.replace("-", "_").lower()

        out_dir = OUTPUT_BASE / f"musan_{category}"
        out_dir.mkdir(parents=True, exist_ok=True)

        existing = len(list(out_dir.glob("*.wav")))

        try:
            data, file_sr = sf.read(str(wav_path), dtype="float32")
        except Exception as e:
            print(f"  Skipping {wav_path.name}: {e}")
            continue

        if data.ndim > 1:
            data = data.mean(axis=1)

        if file_sr != SR:
            n_out = int(len(data) * SR / file_sr)
            indices = np.linspace(0, len(data) - 1, n_out)
            data = np.interp(indices, np.arange(len(data)), data).astype(np.float32)

        clip_samples = int(SR * clip_duration)
        n_clips = max(1, len(data) // clip_samples)

        if existing >= n_clips:
            total_clips += existing
            continue

        clip_count = 0
        for start in range(0, len(data) - clip_samples + 1, clip_samples):
            clip = data[start:start + clip_samples]
            idx = existing + clip_count
            sf.write(str(out_dir / f"musan_{category}_{idx:04d}.wav"), clip, SR)
            clip_count += 1

        # A few random-offset clips
        n_random = min(n_clips, 5)
        for _ in range(n_random):
            start = rng.integers(0, max(1, len(data) - clip_samples))
            clip = data[start:start + clip_samples]
            clip = clip * rng.uniform(0.3, 1.0)
            idx = existing + clip_count
            sf.write(str(out_dir / f"musan_{category}_{idx:04d}.wav"), clip, SR)
            clip_count += 1

        total_clips += clip_count

    print(f"  MUSAN noise total: {total_clips} clips")
    return total_clips


def main():
    parser = argparse.ArgumentParser(description="Generate ambient noise negative clips")
    parser.add_argument("--count", type=int, default=2000,
                        help="Total synthetic clips to generate (default: 2000)")
    parser.add_argument("--min-duration", type=float, default=1.5,
                        help="Minimum clip duration in seconds (default: 1.5)")
    parser.add_argument("--max-duration", type=float, default=4.0,
                        help="Maximum clip duration in seconds (default: 4.0)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed (default: 42)")
    parser.add_argument("--no-download", action="store_true",
                        help="Skip downloading (synthetic only)")
    parser.add_argument("--download-only", action="store_true",
                        help="Only download datasets (skip synthetic generation)")
    args = parser.parse_args()

    if not args.download_only:
        print(f"Generating {args.count} synthetic ambient clips ({args.min_duration}-{args.max_duration}s each)")
        generate_clips(args.count, (args.min_duration, args.max_duration), args.seed)

    if not args.no_download:
        download_mssnsd(clip_duration=3.0, seed=args.seed)
        download_musan_noise(clip_duration=3.0, seed=args.seed)

    # Summary
    total = 0
    all_dirs = (sorted(OUTPUT_BASE.glob("ambient_*"))
                + sorted(OUTPUT_BASE.glob("mssnsd_*"))
                + sorted(OUTPUT_BASE.glob("musan_*")))
    print(f"\n=== Ambient negative clips summary ===")
    for d in all_dirs:
        n = len(list(d.glob("*.wav")))
        if n > 0:
            print(f"  {d.name}: {n} clips")
            total += n
    print(f"  TOTAL: {total} ambient noise clips")


if __name__ == "__main__":
    main()
