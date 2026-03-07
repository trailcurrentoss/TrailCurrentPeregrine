#!/usr/bin/env python3
"""Generate voice-cloned TTS variants via ComfyUI ChatterboxTTS.

Uses a real voice clip as reference to generate positive and negative
wake word variants in the same voice. This helps the model learn to
discriminate words rather than just recognizing a voice pattern.

Usage:
    python3 generate_tts_variants.py
"""

import json
import os
import random
import shutil
import time
import urllib.request
import wave

COMFYUI_URL = "http://localhost:8188"
VOICE_REF = "dave_voice_ref.wav"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Output directories
POS_OUT = os.path.join(SCRIPT_DIR, "real_clips", "hey_peregrine_tts")
NEG_OUT = os.path.join(SCRIPT_DIR, "real_clips_negative")

# Positive: variations of the wake word
POSITIVE_PHRASES = [
    "hey peregrine",
    "Hey Peregrine",
    "hey, peregrine",
    "Hey peregrine!",
    "hey Peregrine?",
]

# Negative: similar-sounding phrases (most confusable first)
NEGATIVE_PHRASES = [
    "hey pelican",
    "hey penguin",
    "hey there again",
    "hey Catherine",
    "hey everyone",
    "peregrine falcon",
    "hey pretty green",
    "hey better ring",
    "hey veteran",
    "hey predator",
    "hey everybody",
    "hey there",
    "hey person",
    "definitely",
    "apparently",
]

CLIPS_PER_POSITIVE = 10   # 5 phrases x 10 = 50 positive clips
CLIPS_PER_NEGATIVE = 5    # 15 phrases x 5 = 75 negative clips


def queue_tts(text, filename_prefix, seed=None):
    """Queue a ChatterboxTTS job and return the prompt_id."""
    if seed is None:
        seed = random.randint(0, 2**32)
    workflow = {
        "4": {
            "class_type": "LoadAudio",
            "inputs": {"audio": VOICE_REF}
        },
        "28": {
            "class_type": "ChatterboxTTS",
            "inputs": {
                "audio_prompt": ["4", 0],
                "model_pack_name": "resembleai_default_voice",
                "text": text,
                "max_new_tokens": 2000,
                "flow_cfg_scale": 0.7,
                "exaggeration": 0.3,
                "temperature": 0.8,
                "cfg_weight": 0.7,
                "repetition_penalty": 1.2,
                "min_p": 0.05,
                "top_p": 1.0,
                "seed": seed,
                "use_watermark": False,
            }
        },
        "7": {
            "class_type": "SaveAudio",
            "inputs": {"audio": ["28", 0], "filename_prefix": filename_prefix}
        }
    }
    data = json.dumps({"prompt": workflow}).encode("utf-8")
    req = urllib.request.Request(
        f"{COMFYUI_URL}/prompt", data=data,
        headers={"Content-Type": "application/json"}
    )
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read())
    return result["prompt_id"]


def wait_for_result(prompt_id, timeout=120):
    """Poll until the TTS job completes. Returns output info or None."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = urllib.request.urlopen(f"{COMFYUI_URL}/history/{prompt_id}")
            history = json.loads(resp.read())
            if prompt_id in history:
                outputs = history[prompt_id].get("outputs", {})
                if "7" in outputs and outputs["7"].get("audio"):
                    return outputs["7"]["audio"][0]
                status = history[prompt_id].get("status", {})
                if status.get("status_str") == "error":
                    print(f"    ERROR: {status.get('messages')}")
                    return None
        except Exception:
            pass
        time.sleep(3)
    print("    TIMEOUT waiting for TTS result")
    return None


def flac_to_wav_16k(flac_path, wav_path):
    """Convert FLAC to 16kHz mono WAV using ffmpeg."""
    import subprocess
    subprocess.run([
        "ffmpeg", "-y", "-i", flac_path,
        "-ar", "16000", "-ac", "1", "-f", "wav", wav_path
    ], check=True, capture_output=True)


def main():
    os.makedirs(POS_OUT, exist_ok=True)

    # Check ComfyUI is running
    try:
        urllib.request.urlopen(f"{COMFYUI_URL}/queue")
    except Exception:
        print(f"ERROR: ComfyUI not reachable at {COMFYUI_URL}")
        print("Start it first: cd ~/ComfyUI && python main.py")
        return

    comfy_output = os.path.expanduser("~/ComfyUI/output")
    total_generated = 0

    # Generate positive variants
    print(f"=== Generating positive variants ({len(POSITIVE_PHRASES)} phrases x {CLIPS_PER_POSITIVE} clips) ===\n")
    for phrase in POSITIVE_PHRASES:
        for i in range(CLIPS_PER_POSITIVE):
            seed = random.randint(0, 2**32)
            prefix = f"ww_pos/{phrase.lower().replace(' ', '_')}_{i:02d}"
            print(f"  [{total_generated+1}] \"{phrase}\" (seed={seed})...", end="", flush=True)

            prompt_id = queue_tts(phrase, prefix, seed)
            result = wait_for_result(prompt_id)
            if result is None:
                print(" FAILED")
                continue

            # Convert FLAC to 16kHz WAV
            flac_path = os.path.join(comfy_output, result.get("subfolder", ""), result["filename"])
            wav_name = f"tts_{seed & 0xFFFFFF:06x}.wav"
            wav_path = os.path.join(POS_OUT, wav_name)
            flac_to_wav_16k(flac_path, wav_path)
            total_generated += 1
            print(f" OK → {wav_name}")

    # Generate negative variants
    print(f"\n=== Generating negative variants ({len(NEGATIVE_PHRASES)} phrases x {CLIPS_PER_NEGATIVE} clips) ===\n")
    for phrase in NEGATIVE_PHRASES:
        safe_name = phrase.lower().replace(" ", "_").replace(",", "")
        phrase_out = os.path.join(NEG_OUT, safe_name + "_tts")
        os.makedirs(phrase_out, exist_ok=True)

        for i in range(CLIPS_PER_NEGATIVE):
            seed = random.randint(0, 2**32)
            prefix = f"ww_neg/{safe_name}_{i:02d}"
            print(f"  [{total_generated+1}] \"{phrase}\" (seed={seed})...", end="", flush=True)

            prompt_id = queue_tts(phrase, prefix, seed)
            result = wait_for_result(prompt_id)
            if result is None:
                print(" FAILED")
                continue

            flac_path = os.path.join(comfy_output, result.get("subfolder", ""), result["filename"])
            wav_name = f"tts_{seed & 0xFFFFFF:06x}.wav"
            wav_path = os.path.join(phrase_out, wav_name)
            flac_to_wav_16k(flac_path, wav_path)
            total_generated += 1
            print(f" OK → {wav_name}")

    print(f"\nDone! Generated {total_generated} clips total.")
    print(f"  Positive: {POS_OUT}")
    print(f"  Negative: {NEG_OUT}/*_tts/")


if __name__ == "__main__":
    main()
