#!/usr/bin/env python3
"""Generate negative wake word clips using ChatterboxTTS via ComfyUI.

Produces diverse speech clips that should NOT trigger the "hey peregrine" wake word.
Uses multiple voice references for variety.
"""

import json
import os
import random
import shutil
import time
import urllib.request

COMFYUI_URL = "http://localhost:8188"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "real_clips_negative")

# Voice references in ~/ComfyUI/input/
VOICES = [
    "common_voice_en_42710088.mp3",
    "MatthewM.mp3",
    "dave_voice_ref.wav",
    "File_Alec_Coles_voice.flac",
    "Male_Adil_Ray_voice.flac",
    "Annissa_Essaibi_George_Salutes_Dorchester_on_Groundhog_Day.flac",
]

# Phrases that sound similar to "hey peregrine" — the model must learn to reject these
CONFUSABLE_PHRASES = [
    "hey pelican",
    "hey pergola",
    "hey paradigm",
    "hey penguin",
    "hey pilgrim",
    "hey predator",
    "hey parakeet",
    "hey perimeter",
    "hey everyone",
    "hey everybody",
    "hey catherine",
    "hey veteran",
    "hey person",
    "hey there",
    "hey pretty green",
    "peregrine falcon",
    "apparently",
    "definitely",
    "hey better ring",
    "hey there again",
    # New confusables
    "hey paragraph",
    "hey pentagon",
    "hey paramedic",
    "hey paragon",
    "hey peppermint",
    "hey passenger",
    "hey Patrick",
    "hey Patricia",
    "hey permanent",
    "hey personal",
    "hey periscope",
    "hey petroleum",
    "peregrine",
    "hey program",
    "hey programmer",
    "hey parent",
    "hey paradise",
    "hey parallel",
]

# General speech — things people might say near the device that should NOT trigger it
GENERAL_SPEECH = [
    "What time is it?",
    "Can you turn off the lights please?",
    "I think we should head out soon.",
    "The weather looks pretty good today.",
    "Did you remember to lock the door?",
    "Let's check the battery level.",
    "How far are we from the campground?",
    "I'm going to make some coffee.",
    "Pass me that wrench, will you?",
    "The trailer is all hooked up.",
    "We need to fill up on water.",
    "Check the tire pressure before we go.",
    "It's getting cold out here.",
    "Turn the heater on.",
    "I love this campsite.",
    "What's for dinner tonight?",
    "The sunset is beautiful.",
    "Hand me my phone.",
    "Are we level?",
    "The slides are out.",
    "Let me grab a jacket.",
    "Kids, come inside!",
    "Honey, where are the keys?",
    "I need to dump the tanks.",
    "The generator is running low.",
    "Close the awning, it's getting windy.",
    "Do we have enough propane?",
    "The fridge isn't cold enough.",
    "I hear something outside.",
    "Good morning!",
    "Good night, sleep tight.",
    "Alexa, play some music.",
    "Hey Google, set a timer.",
    "Hey Siri, what's the weather?",
    "OK Google, navigate home.",
    "The dogs need to go out.",
    "Where did I put my glasses?",
    "This is a really nice spot.",
    "We should come back here next year.",
    "How much solar are we getting?",
]

CLIPS_PER_PHRASE = 3  # clips per phrase per voice subset


def generate_speech(text, voice_ref, filename_prefix, seed=None):
    if seed is None:
        seed = random.randint(0, 2**32)
    workflow = {
        "4": {
            "class_type": "LoadAudio",
            "inputs": {"audio": voice_ref}
        },
        "28": {
            "class_type": "ChatterboxTTS",
            "inputs": {
                "audio_prompt": ["4", 0],
                "model_pack_name": "resembleai_default_voice",
                "text": text,
                "max_new_tokens": 2000,
                "flow_cfg_scale": 0.7,
                "exaggeration": 0.5,
                "temperature": 0.8,
                "cfg_weight": 0.5,
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
        headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read())
    return result["prompt_id"]


def wait_for_job(prompt_id, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        resp = urllib.request.urlopen(f"{COMFYUI_URL}/history/{prompt_id}")
        history = json.loads(resp.read())
        if prompt_id in history:
            outputs = history[prompt_id].get("outputs", {})
            if "7" in outputs and outputs["7"].get("audio"):
                return outputs["7"]["audio"][0]
            status = history[prompt_id].get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"TTS failed: {status.get('messages')}")
        time.sleep(2)
    raise TimeoutError(f"Job {prompt_id} timed out after {timeout}s")


def phrase_to_dirname(phrase):
    return phrase.lower().replace(" ", "_").replace("'", "").replace(",", "")


def main():
    comfyui_output = os.path.expanduser("~/ComfyUI/output")
    all_phrases = CONFUSABLE_PHRASES + GENERAL_SPEECH
    total_clips = len(all_phrases) * CLIPS_PER_PHRASE
    print(f"Generating {total_clips} negative clips ({len(all_phrases)} phrases x {CLIPS_PER_PHRASE} clips each)")
    print(f"Using {len(VOICES)} voice references")
    print()

    generated = 0
    failed = 0

    for phrase in all_phrases:
        dirname = phrase_to_dirname(phrase) + "_chatterbox"
        out_dir = os.path.join(OUTPUT_DIR, dirname)
        os.makedirs(out_dir, exist_ok=True)

        existing = len([f for f in os.listdir(out_dir) if f.endswith((".flac", ".wav", ".mp3"))])
        if existing >= CLIPS_PER_PHRASE:
            print(f"  [{dirname}] already has {existing} clips, skipping")
            generated += CLIPS_PER_PHRASE
            continue

        for i in range(CLIPS_PER_PHRASE):
            voice = VOICES[i % len(VOICES)]
            prefix = f"neg_clips/{dirname}_{i:02d}"
            try:
                print(f"  [{generated+1}/{total_clips}] \"{phrase}\" (voice: {voice})")
                prompt_id = generate_speech(phrase, voice, prefix)
                result = wait_for_job(prompt_id)

                # Copy from ComfyUI output to real_clips_negative
                src = os.path.join(comfyui_output, result.get("subfolder", ""), result["filename"])
                dst = os.path.join(out_dir, f"{i:02d}_{voice.split('.')[0]}.flac")
                shutil.copy2(src, dst)
                generated += 1
                print(f"    -> {dst}")
            except Exception as e:
                print(f"    FAILED: {e}")
                failed += 1

    print(f"\nDone: {generated} generated, {failed} failed")
    print(f"Clips saved to: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
