#!/usr/bin/env python3
"""Record wake word clips for training.

Uses arecord (ALSA) — no Python audio libraries needed.

Usage:
    # Positive clips (the wake word itself)
    python3 record_wake_word.py --phrase "hey peregrine" --count 50
    python3 record_wake_word.py --phrase "hey peregrine" --count 50 --auto

    # Negative clips (similar-sounding phrases the model should reject)
    python3 record_wake_word.py --phrase "hey pelican" --count 10 --negative
    python3 record_wake_word.py --phrase "hey penguin" --count 10 --negative --auto
"""

import argparse
import os
import subprocess
import sys
import time
import uuid
import wave
import struct
import math


SAMPLE_RATE = 16000
DURATION = 2.0  # seconds per clip

# Phrases that sound similar to "hey peregrine" — most useful for reducing
# false positives. Ordered roughly by phonetic similarity (most confusable first).
NEGATIVE_PHRASES = [
    # Close rhymes / partial matches
    ("hey pelican", "shares 'hey pe-' prefix and similar cadence"),
    ("hey penguin", "shares 'hey pe-' prefix, common word"),
    ("hey there again", "similar rhythm and ending '-gain' ≈ '-grine'"),
    ("hey Catherine", "'-erine' ending matches '-egrine'"),
    ("hey everyone", "similar vowel pattern and length"),
    # Partial word overlap
    ("peregrine falcon", "contains the wake word without 'hey'"),
    ("hey pretty green", "'pre-' + '-green' ≈ 'peregrine'"),
    ("hey better ring", "similar consonant/vowel pattern"),
    ("hey veteran", "'-eteran' has similar rhythm to '-eregrine'"),
    ("hey predator", "'hey pred-' close to 'hey pereg-'"),
    # Common speech the model might hear
    ("hey everybody", "starts with 'hey' + multi-syllable word"),
    ("hey there", "most common 'hey' phrase"),
    ("hey person", "'hey per-' prefix match"),
    ("definitely", "'-finitely' has similar stress to '-eregrine'"),
    ("apparently", "similar syllable count and rhythm"),
]


def print_negative_suggestions():
    """Print suggested negative phrases for training."""
    print("Suggested negative phrases for 'hey peregrine':\n")
    print("Most confusable (record 10-20 each):")
    for phrase, reason in NEGATIVE_PHRASES[:5]:
        print(f"  \"{phrase}\"  — {reason}")
    print("\nModerately confusable (record 5-10 each):")
    for phrase, reason in NEGATIVE_PHRASES[5:10]:
        print(f"  \"{phrase}\"  — {reason}")
    print("\nGeneral negative (record 5 each):")
    for phrase, reason in NEGATIVE_PHRASES[10:]:
        print(f"  \"{phrase}\"  — {reason}")
    print()
    print("Usage:")
    print('  python3 record_wake_word.py --phrase "hey pelican" --count 10 --negative')
    print('  python3 record_wake_word.py --phrase "hey pelican" --count 10 --negative --auto')


def get_wav_rms(filepath):
    """Calculate RMS of a WAV file."""
    with wave.open(filepath, "rb") as wf:
        data = wf.readframes(wf.getnframes())
    count = len(data) // 2
    if count == 0:
        return 0
    shorts = struct.unpack(f"{count}h", data)
    return math.sqrt(sum(s * s for s in shorts) / count)


def record_clip(filepath, duration=DURATION):
    """Record a clip using arecord."""
    cmd = [
        "arecord",
        "-f", "S16_LE",
        "-r", str(SAMPLE_RATE),
        "-c", "1",
        "-t", "wav",
        "-d", str(int(duration)),
        "-q",  # quiet
        filepath,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=duration + 5)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"    Recording failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Record wake word clips")
    parser.add_argument("--phrase", default="hey peregrine",
                        help="The phrase to record")
    parser.add_argument("--count", type=int, default=50,
                        help="Number of clips to record")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory")
    parser.add_argument("--duration", type=float, default=DURATION,
                        help="Clip duration in seconds (default: 2.0)")
    parser.add_argument("--negative", action="store_true",
                        help="Record as negative (rejection) clips")
    parser.add_argument("--auto", action="store_true",
                        help="Auto-record with countdown (no Enter needed)")
    parser.add_argument("--suggest", action="store_true",
                        help="Show suggested negative phrases and exit")
    args = parser.parse_args()

    if args.suggest:
        print_negative_suggestions()
        return

    if args.output_dir is None:
        safe_name = args.phrase.replace(" ", "_")
        subdir = "real_clips_negative" if args.negative else "real_clips"
        args.output_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            subdir, safe_name
        )

    os.makedirs(args.output_dir, exist_ok=True)
    existing = len([f for f in os.listdir(args.output_dir) if f.endswith(".wav")])

    clip_type = "NEGATIVE" if args.negative else "POSITIVE"
    print(f"Recording '{args.phrase}' clips ({clip_type})")
    print(f"  Output:   {args.output_dir}")
    print(f"  Duration: {args.duration}s per clip")
    print(f"  Target:   {args.count} clips ({existing} already exist)")
    print()
    print("Tips: vary your distance, volume, and speed between clips.")
    print()

    if args.auto:
        print("AUTO MODE: Say the phrase right after 'GO!'")
        print("Press Ctrl+C to stop.\n")
    else:
        print("Press Enter to record, 'q' to quit, 's' to skip.\n")

    recorded = 0
    skipped = 0

    try:
        for i in range(args.count):
            clip_num = existing + recorded + 1

            if args.auto:
                print(f"  Clip {clip_num}: ", end="", flush=True)
                for n in [3, 2, 1]:
                    print(f"{n}...", end="", flush=True)
                    time.sleep(1)
                print(" GO!", flush=True)
            else:
                resp = input(f"  Clip {clip_num}/{existing + args.count} "
                             f"[Enter=record, q=quit, s=skip]: ").strip().lower()
                if resp == "q":
                    break
                if resp == "s":
                    skipped += 1
                    continue

            print(f"    >> Say '{args.phrase}' NOW <<", flush=True)
            time.sleep(0.1)

            filename = f"real_{uuid.uuid4().hex[:12]}.wav"
            filepath = os.path.join(args.output_dir, filename)

            if not record_clip(filepath, args.duration):
                skipped += 1
                continue

            rms = get_wav_rms(filepath)
            if rms < 300:
                print(f"    Too quiet (rms={rms:.0f}) — discarded")
                os.remove(filepath)
                skipped += 1
                continue

            recorded += 1
            print(f"    Saved: {filename} (rms={rms:.0f})")

            if args.auto:
                time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n\nStopped.")

    total = existing + recorded
    print(f"\nDone! Recorded {recorded} new clips, skipped {skipped}")
    print(f"Total clips in output dir: {total}")


if __name__ == "__main__":
    main()
