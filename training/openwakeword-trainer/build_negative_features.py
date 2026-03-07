#!/usr/bin/env python3
"""build_negative_features.py — Build broad negative feature set from permissive audio.

Downloads LibriSpeech (CC-BY-4.0) and VoxPopuli English (CC0), extracts
openWakeWord-compatible embeddings using AudioFeatures.embed_clips(), and
saves the result as a .npy file that can be used as a drop-in replacement
for the ACAV100M features.

Usage:
    python build_negative_features.py --output data/negative_features.npy --hours 1000
    python build_negative_features.py --verify data/negative_features.npy
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
from numpy.lib.format import open_memmap

log = logging.getLogger("build_negative_features")

# ---------------------------------------------------------------------------
# Audio source definitions
# ---------------------------------------------------------------------------

SOURCES = {
    "librispeech": [
        ("openslr/librispeech_asr", "train.clean.100"),
        ("openslr/librispeech_asr", "train.clean.360"),
        ("openslr/librispeech_asr", "train.other.500"),
    ],
    "voxpopuli": [
        ("facebook/voxpopuli", "train"),
    ],
}

SAMPLE_RATE = 16_000
CLIP_SAMPLES = 32_000  # 2 seconds


def _resample(samples: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """Resample audio from orig_sr to target_sr."""
    if orig_sr == target_sr:
        return samples
    from scipy.signal import resample

    n_out = int(len(samples) * target_sr / orig_sr)
    return resample(samples, n_out)


def audio_clip_generator(target_hours: float, sources: list[str] | None = None):
    """Yield int16 PCM clips of CLIP_SAMPLES length from streaming datasets.

    Yields:
        np.ndarray of shape (CLIP_SAMPLES,), dtype int16
    """
    from datasets import load_dataset

    if sources is None:
        sources = list(SOURCES.keys())

    total_target = int(target_hours * 3600 * SAMPLE_RATE)
    total_yielded = 0
    buffer = np.array([], dtype=np.float32)

    for source_name in sources:
        if total_yielded >= total_target:
            break
        for ds_name, split in SOURCES[source_name]:
            if total_yielded >= total_target:
                break

            log.info(f"Streaming {ds_name} split={split}")
            kwargs = {}
            if "voxpopuli" in ds_name:
                kwargs["name"] = "en"

            try:
                ds = load_dataset(ds_name, split=split, streaming=True, **kwargs)
            except Exception as e:
                log.warning(f"Failed to load {ds_name}/{split}: {e}, skipping")
                continue

            for row in ds:
                audio = row["audio"]
                samples = np.array(audio["array"], dtype=np.float32)
                sr = audio["sampling_rate"]

                samples = _resample(samples, sr, SAMPLE_RATE)
                buffer = np.concatenate([buffer, samples])

                while len(buffer) >= CLIP_SAMPLES:
                    clip_f32 = buffer[:CLIP_SAMPLES]
                    buffer = buffer[CLIP_SAMPLES:]

                    # Convert to int16 PCM (what AudioFeatures expects)
                    clip_i16 = np.clip(clip_f32 * 32767, -32768, 32767).astype(np.int16)
                    yield clip_i16
                    total_yielded += CLIP_SAMPLES

                    if total_yielded >= total_target:
                        return

    if total_yielded < total_target:
        log.warning(
            f"Only produced {total_yielded / SAMPLE_RATE / 3600:.1f} hours "
            f"of the requested {target_hours} hours"
        )


def build_features(
    output_path: str,
    target_hours: float = 1000,
    batch_size: int = 256,
    device: str = "cpu",
    sources: list[str] | None = None,
):
    """Extract openWakeWord embeddings from streamed audio and save as .npy."""
    from openwakeword.utils import AudioFeatures

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".npy.tmp")

    F = AudioFeatures(device=device)

    # Determine embedding shape for a single clip
    emb_shape = F.get_embedding_shape(CLIP_SAMPLES / SAMPLE_RATE)
    n_frames, n_features = emb_shape
    log.info(f"Embedding shape per clip: ({n_frames}, {n_features})")

    # Estimate total clips
    estimated_clips = int(target_hours * 3600 * SAMPLE_RATE / CLIP_SAMPLES)
    # Add 5% margin for mmap, will trim at end
    alloc_clips = int(estimated_clips * 1.05)

    # Check for resume
    row_counter = 0
    if tmp_path.exists():
        existing = np.load(str(tmp_path), mmap_mode="r")
        if existing.shape[1:] == (n_frames, n_features):
            # Count non-zero rows to find resume point
            for i in range(existing.shape[0] - 1, -1, -1):
                if np.any(existing[i]):
                    row_counter = i + 1
                    break
            if row_counter > 0:
                log.info(f"Resuming from row {row_counter} ({row_counter * CLIP_SAMPLES / SAMPLE_RATE / 3600:.1f} hrs)")
                alloc_clips = max(alloc_clips, existing.shape[0])
        else:
            log.warning("Existing tmp file has wrong shape, starting fresh")
            os.remove(str(tmp_path))

    if not tmp_path.exists():
        log.info(f"Allocating mmap file for {alloc_clips} clips ({alloc_clips * n_frames * n_features * 4 / 1e9:.1f} GB)")
        fp = open_memmap(
            str(tmp_path), mode="w+", dtype=np.float32,
            shape=(alloc_clips, n_frames, n_features),
        )
    else:
        fp = open_memmap(str(tmp_path), mode="r+")

    # Skip already-processed audio when resuming
    hours_already_done = row_counter * CLIP_SAMPLES / SAMPLE_RATE / 3600
    remaining_hours = target_hours - hours_already_done
    if remaining_hours <= 0:
        log.info("Already have enough features, skipping to trim")
    else:
        # On resume, only generate the remaining hours (don't re-stream
        # already-processed audio — these are random negatives so it
        # doesn't matter if we get different clips for the tail).
        gen = audio_clip_generator(remaining_hours, sources=sources)
        if row_counter > 0:
            log.info(f"Resuming: generating {remaining_hours:.1f} more hours to append from row {row_counter}")

        batch = []
        t0 = time.time()
        for clip in gen:
            batch.append(clip)

            if len(batch) >= batch_size:
                audio_batch = np.stack(batch)
                features = F.embed_clips(audio_batch, batch_size=batch_size)
                end = min(row_counter + features.shape[0], alloc_clips)
                n_write = end - row_counter
                fp[row_counter:end] = features[:n_write]
                fp.flush()
                row_counter = end
                batch = []

                elapsed = time.time() - t0
                hrs_done = row_counter * CLIP_SAMPLES / SAMPLE_RATE / 3600
                if elapsed > 0:
                    rate = hrs_done / (elapsed / 3600)
                    log.info(
                        f"  {hrs_done:.1f}/{target_hours} hrs "
                        f"({row_counter} clips, {rate:.1f}x realtime)"
                    )

                if row_counter >= alloc_clips:
                    break

        # Process remaining batch
        if batch and row_counter < alloc_clips:
            audio_batch = np.stack(batch)
            features = F.embed_clips(audio_batch, batch_size=len(batch))
            end = min(row_counter + features.shape[0], alloc_clips)
            n_write = end - row_counter
            fp[row_counter:end] = features[:n_write]
            fp.flush()
            row_counter = end

    # Trim and save final file
    log.info(f"Trimming to {row_counter} clips and saving to {output_path}")
    final = np.array(fp[:row_counter])
    np.save(str(output_path), final)

    # Clean up tmp
    del fp
    if tmp_path.exists():
        os.remove(str(tmp_path))

    total_hrs = row_counter * CLIP_SAMPLES / SAMPLE_RATE / 3600
    log.info(f"Done: {output_path} — {row_counter} clips, {total_hrs:.1f} hours, shape {final.shape}")


def verify_features(path: str):
    """Verify a negative features .npy file is well-formed."""
    path = Path(path)
    if not path.exists():
        log.error(f"File not found: {path}")
        return False

    data = np.load(str(path), mmap_mode="r")
    log.info(f"Shape: {data.shape}")
    log.info(f"Dtype: {data.dtype}")
    log.info(f"Size: {path.stat().st_size / 1e9:.2f} GB")

    if data.ndim != 3:
        log.error(f"Expected 3 dimensions, got {data.ndim}")
        return False

    n_clips, n_frames, n_features = data.shape
    if n_features != 96:
        log.error(f"Expected 96 features (embedding dim), got {n_features}")
        return False

    total_hrs = n_clips * CLIP_SAMPLES / SAMPLE_RATE / 3600
    log.info(f"Total audio represented: {total_hrs:.1f} hours ({n_clips} clips)")

    # Check a sample for sanity
    sample = np.array(data[:100])
    log.info(f"Sample stats — mean: {sample.mean():.4f}, std: {sample.std():.4f}, "
             f"min: {sample.min():.4f}, max: {sample.max():.4f}")

    # Check for all-zero rows in sample
    zero_rows = np.sum(np.all(sample == 0, axis=(1, 2)))
    if zero_rows > 0:
        log.warning(f"{zero_rows}/100 sampled rows are all zeros")

    log.info("Verification passed")
    return True


def main():
    parser = argparse.ArgumentParser(description="Build negative feature set from permissive audio")
    sub = parser.add_subparsers(dest="command")

    # Build command (default)
    build_p = sub.add_parser("build", help="Build features (default)")
    build_p.add_argument("--output", "-o", required=True, help="Output .npy file path")
    build_p.add_argument("--hours", type=float, default=1000, help="Target hours of audio (default: 1000)")
    build_p.add_argument("--batch-size", type=int, default=256, help="Batch size for embedding extraction")
    build_p.add_argument("--device", default="cpu", choices=["cpu", "gpu"], help="Device for inference")
    build_p.add_argument("--sources", nargs="+", choices=list(SOURCES.keys()),
                         help="Audio sources to use (default: all)")

    # Verify command
    verify_p = sub.add_parser("verify", help="Verify a features file")
    verify_p.add_argument("path", help="Path to .npy file to verify")

    # Also support --verify as a shortcut
    parser.add_argument("--verify", metavar="PATH", help="Verify a features file")
    parser.add_argument("--output", "-o", help="Output .npy file path (for default build mode)")
    parser.add_argument("--hours", type=float, default=1000)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--device", default="cpu", choices=["cpu", "gpu"])
    parser.add_argument("--sources", nargs="+", choices=list(SOURCES.keys()))

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")

    if args.verify:
        ok = verify_features(args.verify)
        sys.exit(0 if ok else 1)
    elif args.command == "verify":
        ok = verify_features(args.path)
        sys.exit(0 if ok else 1)
    elif args.command == "build" or args.output:
        output = args.output
        if not output:
            parser.error("--output is required for build")
        build_features(
            output_path=output,
            target_hours=args.hours,
            batch_size=args.batch_size,
            device=args.device,
            sources=args.sources,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
