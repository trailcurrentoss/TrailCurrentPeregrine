#!/usr/bin/env python3
"""Rebuild negative_features_ambient.npy from all ambient WAV clips.

Collects clips from:
  - real_clips_negative/ambient_*  (synthetic noise)
  - real_clips_negative/mssnsd_*   (MS-SNSD, MIT)
  - real_clips_negative/musan_*    (MUSAN noise, CC0)

Extracts openWakeWord embeddings and saves to the trainer data dir.

Usage:
    python build_ambient_features.py
"""
import sys
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "openwakeword-trainer"))

import soundfile as sf
from openwakeword.utils import AudioFeatures

TRAINING_DIR = Path(__file__).resolve().parent
CLIPS_DIR = TRAINING_DIR / "real_clips_negative"
OUT_PATH = TRAINING_DIR / "openwakeword-trainer" / "data" / "negative_features_ambient.npy"

patterns = ["ambient_*/*.wav", "mssnsd_*/*.wav", "musan_*/*.wav"]
wav_files = []
for pat in patterns:
    wav_files.extend(sorted(CLIPS_DIR.glob(pat)))

print(f"Found {len(wav_files)} ambient clips")
if not wav_files:
    print("ERROR: No clips found!")
    sys.exit(1)

# openWakeWord training expects features with exactly 16 frames (~1 second).
# Chunk each clip into 16000-sample (1-second) windows so embed_clips produces
# (N, 16, 96) features matching the broad negatives and positives.
CHUNK_SAMPLES = 32000  # 2 seconds at 16 kHz = 16 embedding frames

chunks = []
for i, wav_path in enumerate(wav_files):
    data, sr = sf.read(str(wav_path), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    # Resample to 16 kHz if needed
    if sr != 16000:
        import librosa
        data = librosa.resample(data, orig_sr=sr, target_sr=16000)
    data_int16 = (data * 32767).clip(-32768, 32767).astype(np.int16)
    # Split into 1-second chunks, discard remainder < 1 second
    for start in range(0, len(data_int16) - CHUNK_SAMPLES + 1, CHUNK_SAMPLES):
        chunks.append(data_int16[start:start + CHUNK_SAMPLES])
    if (i + 1) % 1000 == 0:
        print(f"  Loaded {i+1}/{len(wav_files)} clips")

print(f"Split {len(wav_files)} clips into {len(chunks)} 2-second chunks, extracting features...")

padded = np.array(chunks, dtype=np.int16)  # (N, 16000) — uniform length

af = AudioFeatures()
embeddings = af.embed_clips(padded)

print(f"Embeddings shape: {embeddings.shape}")
assert embeddings.shape[1] == 16, (
    f"Expected 16 frames per clip but got {embeddings.shape[1]}. "
    f"Check CHUNK_SAMPLES value."
)
np.save(str(OUT_PATH), embeddings)
print(f"Done! {OUT_PATH.name} — {len(embeddings)} chunks, shape {embeddings.shape}")
