#!/usr/bin/env python3
"""Headless voice assistant: wake word -> STT -> LLM -> TTS -> speaker.

Supports MQTT integration with TrailCurrent for device control and sensor queries.
"""

import shutil
import subprocess
import tempfile
import wave
import time
import os
import json
import ssl
import re
import signal
import difflib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    from timezonefinder import TimezoneFinder
    _tz_finder = TimezoneFinder()
except ImportError:
    _tz_finder = None

import numpy as np
from openwakeword.model import Model as WakeModel
from faster_whisper import WhisperModel
import requests

# --- Config (override via environment variables) ---
WAKE_MODEL = os.getenv("WAKE_MODEL", "hey_peregrine")
WHISPER_SIZE = os.getenv("WHISPER_SIZE", "base.en")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:1b-npu")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
PIPER_MODEL = os.getenv("PIPER_MODEL",
    os.path.expanduser("~/piper-voices/en_US-libritts_r-medium.onnx"))

SAMPLE_RATE = 16000
CHUNK = 1280  # 80ms at 16kHz
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.8"))
SILENCE_THRESHOLD = int(os.getenv("SILENCE_THRESHOLD", "500"))
SILENCE_DURATION = float(os.getenv("SILENCE_DURATION", "1.5"))
MAX_RECORD_SECONDS = 15

# MQTT config
MQTT_BROKER = os.getenv("MQTT_BROKER", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_CA_CERT = os.getenv("MQTT_CA_CERT", "")  # path to ca.pem for self-signed certs
MQTT_USE_TLS = os.getenv("MQTT_USE_TLS", "true").lower() in ("true", "1", "yes")

BASE_SYSTEM_PROMPT = (
    "You are a voice assistant for a vehicle and trailer system called TrailCurrent. "
    "You can control lights and read sensor data. "
    "Keep answers concise and conversational, ideally under 3 sentences. "
    "Do not use markdown, bullet points, or formatting since your response will be spoken aloud.\n\n"
    "When the user wants to control a device, respond ONLY with a JSON object on a single line, nothing else:\n"
    "- Turn lights on/off: {\"action\": \"light\", \"id\": \"all\", \"state\": 1}\n"
    "  (id can be \"all\" or a number like \"1\", \"2\". state: 1 means on, 0 means off)\n"
    "- Set brightness: {\"action\": \"light\", \"id\": \"1\", \"brightness\": 50}\n"
    "  (brightness is 0-100 percent. Only works per-light, not \"all\")\n\n"
    "When the user asks about sensor data, use the current readings below to answer conversationally.\n"
    "If no sensor data is available, say you don't have that information yet.\n\n"
)

# --- Init ---
print("=" * 50)
print("Voice Assistant Starting")
print("=" * 50)
print(f"  Wake word:  {WAKE_MODEL}")
print(f"  STT model:  {WHISPER_SIZE}")
print(f"  LLM model:  {OLLAMA_MODEL}")
print(f"  TTS model:  {os.path.basename(PIPER_MODEL)}")
print(f"  MQTT:       {MQTT_BROKER}:{MQTT_PORT}" if MQTT_BROKER else "  MQTT:       disabled")
print()

print("Loading wake word model...")
# Check for custom model file (local path or ~/models/)
_wake_model_path = os.getenv("WAKE_MODEL_PATH", "")
if not _wake_model_path:
    # Look for a local .onnx file matching the model name
    for _candidate in [
        os.path.expanduser(f"~/models/{WAKE_MODEL}.onnx"),
        os.path.expanduser(f"~/{WAKE_MODEL}.onnx"),
    ]:
        if os.path.isfile(_candidate):
            _wake_model_path = _candidate
            break
if _wake_model_path:
    print(f"  Using custom model: {_wake_model_path}")
    # v0.4.0 uses wakeword_model_paths; v0.6+ uses wakeword_models
    try:
        wake_model = WakeModel(wakeword_model_paths=[_wake_model_path])
    except TypeError:
        wake_model = WakeModel(wakeword_models=[_wake_model_path], inference_framework="onnx")
else:
    wake_model = WakeModel()

print("Loading Whisper STT model...")
# RK3588S has 8 cores — use them all for transcription
_CPU_THREADS = int(os.getenv("CPU_THREADS", "8"))
whisper_model = WhisperModel(WHISPER_SIZE, device="cpu", compute_type="int8",
                             cpu_threads=_CPU_THREADS)


# --- Audio I/O via arecord / aplay (replaces PyAudio) ---

_alsa_capture_device = None   # e.g. "plughw:0,0" — Jabra mic
_alsa_playback_device = None  # e.g. "plughw:0,0" or "default"


def _wait_for_audio_device(timeout=30):
    """Wait for a USB audio device to appear. Sets separate capture/playback devices."""
    global _alsa_capture_device, _alsa_playback_device
    start = time.time()
    usb_card = None
    while time.time() - start < timeout:
        try:
            out = subprocess.check_output(["arecord", "-l"], stderr=subprocess.DEVNULL, text=True)
            for line in out.splitlines():
                line_lower = line.lower()
                if "card " in line_lower and ("usb" in line_lower or "jabra" in line_lower):
                    usb_card = line.split(":")[0].split()[-1]
                    break
            if usb_card:
                break
            if "card " in out.lower():
                # No USB, but some capture device exists
                _alsa_capture_device = "default"
                _alsa_playback_device = "default"
                print(f"  Audio device found (using default) ({time.time() - start:.1f}s)")
                return True
        except subprocess.CalledProcessError:
            pass
        elapsed = time.time() - start
        if elapsed > 5 and int(elapsed) % 5 == 0:
            print(f"  Still waiting for USB audio device... ({elapsed:.0f}s)")
        time.sleep(1)

    if usb_card is None:
        _alsa_capture_device = "default"
        _alsa_playback_device = "default"
        print(f"  WARNING: No USB audio device found after {timeout}s, using default")
        return False

    _alsa_capture_device = f"plughw:{usb_card},0"
    print(f"  USB audio capture: card {usb_card} -> {_alsa_capture_device}")

    # Check if this USB card also has a playback device
    try:
        play_out = subprocess.check_output(["aplay", "-l"], stderr=subprocess.DEVNULL, text=True)
        usb_has_playback = False
        for line in play_out.splitlines():
            line_lower = line.lower()
            if f"card {usb_card}:" in line_lower or ("usb" in line_lower and "card " in line_lower):
                usb_has_playback = True
                break
        if usb_has_playback:
            _alsa_playback_device = f"plughw:{usb_card},0"
            print(f"  USB audio playback: card {usb_card} -> {_alsa_playback_device}")
        else:
            # USB device is capture-only in ALSA — use default (PulseAudio) for playback
            _alsa_playback_device = "default"
            print(f"  USB has no ALSA playback device — using 'default' for output")
    except subprocess.CalledProcessError:
        _alsa_playback_device = "default"
        print(f"  Could not query playback devices — using 'default' for output")

    return True


def _start_arecord():
    """Start a persistent arecord subprocess that streams raw S16_LE mono 16kHz to stdout."""
    cmd = [
        "arecord", "-D", _alsa_capture_device,
        "-f", "S16_LE", "-c", "1", "-r", str(SAMPLE_RATE),
        "-t", "raw", "--buffer-size", "8192",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    return proc


def _stop_arecord(proc):
    """Gracefully stop an arecord subprocess."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _play_wav(wav_path, timeout=30):
    """Play a WAV file. Tries paplay (PulseAudio) first, falls back to aplay."""
    if _has_pulseaudio:
        try:
            result = subprocess.run(
                ["paplay", wav_path],
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode == 0:
                return True
            print(f"  (paplay failed: {result.stderr.strip()[:100]})")
        except Exception as e:
            print(f"  (paplay error: {e})")
    # Fallback to aplay
    try:
        result = subprocess.run(
            ["aplay", "-D", _alsa_playback_device, wav_path],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            print(f"  (aplay failed: {result.stderr.strip()[:100]})")
            return False
        return True
    except Exception as e:
        print(f"  (aplay error: {e})")
        return False


def _play_raw_audio(raw_bytes, sample_rate=22050, channels=1):
    """Play raw S16_LE audio. Writes to temp WAV for paplay, falls back to aplay."""
    # Write to temp WAV so paplay can handle it
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tmp_path = tf.name
            with wave.open(tf, "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(raw_bytes)
        return _play_wav(tmp_path)
    except Exception as e:
        print(f"  (playback error: {e})")
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _generate_beep_wav(path, freq=440, duration=0.4, sample_rate=48000):
    """Write a beep tone as a WAV file (S16_LE mono).

    Includes a short silence lead-in so the USB audio device has time to
    initialize before the tone starts (avoids clipped/pop-only playback).
    """
    lead_in = int(sample_rate * 0.05)  # 50ms silence lead-in
    n_samples = int(sample_rate * duration)
    t = np.linspace(0, duration, n_samples, dtype=np.float32)
    tone = (np.sin(2 * np.pi * freq * t) * 0.8 * 32767).astype(np.int16)
    # Apply fade-in/out to avoid clicks
    fade = min(int(sample_rate * 0.02), n_samples // 4)
    tone[:fade] = (tone[:fade] * np.linspace(0, 1, fade)).astype(np.int16)
    tone[-fade:] = (tone[-fade:] * np.linspace(1, 0, fade)).astype(np.int16)
    # Prepend silence lead-in
    silence = np.zeros(lead_in, dtype=np.int16)
    audio = np.concatenate([silence, tone])
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio.tobytes())


print("Waiting for audio device...")
_wait_for_audio_device()

# Log available ALSA devices for debugging
for _cmd_label, _cmd in [("Capture", ["arecord", "-l"]), ("Playback", ["aplay", "-l"])]:
    try:
        _out = subprocess.check_output(_cmd, stderr=subprocess.DEVNULL, text=True)
        print(f"\n  ALSA {_cmd_label} devices:")
        for _line in _out.strip().splitlines():
            print(f"    {_line}")
    except subprocess.CalledProcessError:
        print(f"\n  ALSA {_cmd_label} devices: (none)")


def _ensure_pulseaudio():
    """Start PulseAudio for this user if not already running."""
    if not shutil.which("pulseaudio"):
        print("\n  PulseAudio: not installed")
        return False
    check = subprocess.run(["pactl", "info"], capture_output=True, timeout=5)
    if check.returncode == 0:
        print("\n  PulseAudio: running")
        return True
    print("\n  PulseAudio: starting...")
    subprocess.run(
        ["pulseaudio", "--start", "--exit-idle-time=-1"],
        capture_output=True, timeout=10,
    )
    for _ in range(10):
        time.sleep(0.5)
        if subprocess.run(["pactl", "info"], capture_output=True, timeout=5).returncode == 0:
            print("  PulseAudio: started")
            return True
    print("  PulseAudio: failed to start")
    return False


_has_pulseaudio = _ensure_pulseaudio()

if _has_pulseaudio:
    # PulseAudio owns the devices now — use its ALSA plugin for both directions
    _alsa_capture_device = "pulse"
    _alsa_playback_device = "pulse"
    print(f"  Using PulseAudio for capture and playback")
    try:
        _out = subprocess.check_output(
            ["pactl", "list", "short", "sinks"], stderr=subprocess.DEVNULL, text=True)
        print(f"  Sinks:")
        for _line in _out.strip().splitlines():
            print(f"    {_line}")
    except Exception:
        pass
    try:
        _out = subprocess.check_output(
            ["pactl", "list", "short", "sources"], stderr=subprocess.DEVNULL, text=True)
        print(f"  Sources:")
        for _line in _out.strip().splitlines():
            print(f"    {_line}")
    except Exception:
        pass

# Pre-generate beep WAV files
_beep_dir = tempfile.mkdtemp(prefix="assistant_beeps_")
BEEP_WAKE_WAV = os.path.join(_beep_dir, "wake.wav")
BEEP_DONE_WAV = os.path.join(_beep_dir, "done.wav")
_generate_beep_wav(BEEP_WAKE_WAV, freq=800, duration=0.3)
_generate_beep_wav(BEEP_DONE_WAV, freq=600, duration=0.2)
print(f"Beep WAVs generated in {_beep_dir}")

# --- MQTT Setup ---
sensor_data = {}
known_light_ids = set()  # populated from local/lights/+/status messages
mqtt = None

# Device registry: populated from MQTT retained topics
#   local/config/pdm_channels   (Torrent lights)
#   local/config/relay_channels  (Switchback relays)
_device_registry = {}      # "living room" -> {"id": 1, "type": "light", "name": "Living Room"}
_device_names_by_id = {}   # 1 -> "Living Room"
_device_types_by_id = {}   # 1 -> "light"
_relay_channel_by_id = {}  # 101 -> 1  (maps device ID to MQTT relay channel number)
_DEVICE_CACHE_PATH = os.path.expanduser("~/device_registry.json")
_RELAY_CACHE_PATH = os.path.expanduser("~/relay_registry.json")


def _save_device_cache(channels):
    """Persist the raw channel list to disk."""
    try:
        with open(_DEVICE_CACHE_PATH, "w") as f:
            json.dump(channels, f)
    except OSError as e:
        print(f"  Warning: could not save device cache: {e}")


def _load_device_cache():
    """Load cached channel list from disk, if available."""
    try:
        with open(_DEVICE_CACHE_PATH, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _rebuild_device_registry():
    """Merge PDM and relay entries into the unified device registry."""
    global _device_registry, _device_names_by_id, _device_types_by_id
    merged = {}
    names = {}
    types = {}
    for entries in (_pdm_entries, _relay_entries):
        for ch_id, info in entries.items():
            key = info["name"].strip().lower()
            merged[key] = info
            names[ch_id] = info["name"]
            types[ch_id] = info["type"]
    _device_registry = merged
    _device_names_by_id = names
    _device_types_by_id = types
    print(f"  Device registry updated: {len(merged)} devices")
    for key, info in merged.items():
        print(f"    [{info['id']}] {info['name']} ({info['type']})")


# Internal stores for PDM vs relay entries (keyed by device ID)
_pdm_entries = {}    # {1: {"id": 1, "type": "light", "name": "Living Room"}, ...}
_relay_entries = {}  # {101: {"id": 101, "type": "relay", "name": "Water Pump"}, ...}


def _update_device_registry(channels, save=True):
    """Rebuild the PDM portion of the device registry."""
    global _pdm_entries
    _pdm_entries = {}
    for ch in channels:
        ch_id = ch.get("id")
        ch_name = ch.get("name", "")
        ch_type = ch.get("type", "unknown")
        if ch_id is None or not ch_name:
            continue
        _pdm_entries[int(ch_id)] = {"id": int(ch_id), "type": ch_type, "name": ch_name}
    _rebuild_device_registry()
    if save:
        _save_device_cache(channels)


def _save_relay_cache(channels):
    """Persist the raw relay channel list to disk."""
    try:
        with open(_RELAY_CACHE_PATH, "w") as f:
            json.dump(channels, f)
    except OSError as e:
        print(f"  Warning: could not save relay cache: {e}")


def _load_relay_cache():
    """Load cached relay channel list from disk, if available."""
    try:
        with open(_RELAY_CACHE_PATH, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _update_relay_registry(channels, save=True):
    """Rebuild the relay portion of the device registry."""
    global _relay_entries, _relay_channel_by_id
    _relay_entries = {}
    _relay_channel_by_id = {}
    for ch in channels:
        ch_id = ch.get("id")
        ch_name = ch.get("name", "")
        relay_ch = ch.get("relay_channel")
        if ch_id is None or not ch_name or relay_ch is None:
            continue
        ch_type = ch.get("type", "other")
        _relay_entries[int(ch_id)] = {"id": int(ch_id), "type": ch_type, "name": ch_name}
        _relay_channel_by_id[int(ch_id)] = int(relay_ch)
    _rebuild_device_registry()
    if save:
        _save_relay_cache(channels)


# Load cached registries at startup (before MQTT connects)
_cached_channels = _load_device_cache()
if _cached_channels:
    print("Loading cached device registry...")
    _update_device_registry(_cached_channels, save=False)
else:
    print("No cached device registry found (will populate from MQTT).")

_cached_relays = _load_relay_cache()
if _cached_relays:
    print("Loading cached relay registry...")
    _update_relay_registry(_cached_relays, save=False)
else:
    print("No cached relay registry found (will populate from MQTT).")

import threading

_mqtt_connected = threading.Event()

def _connect_mqtt():
    """Connect to MQTT broker with retries. Blocks until connected."""
    global mqtt
    import paho.mqtt.client as paho_mqtt

    def on_mqtt_connect(client, userdata, flags, rc, properties=None):
        if rc == 0 or rc.value == 0:
            print("MQTT connected")
            client.subscribe("local/energy/status")
            client.subscribe("local/airquality/temphumid")
            client.subscribe("local/airquality/status")
            client.subscribe("local/gps/latlon")
            client.subscribe("local/gps/time")
            client.subscribe("local/gps/alt")
            client.subscribe("local/lights/+/status")
            client.subscribe("local/relays/+/status")
            client.subscribe("local/thermostat/status")
            client.subscribe("local/config/pdm_channels")
            client.subscribe("local/config/relay_channels")
            _mqtt_connected.set()
        else:
            print(f"MQTT connection failed (rc={rc})")

    def on_mqtt_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
            sensor_data[msg.topic] = payload
            # Track discovered light IDs from status messages
            if msg.topic.startswith("local/lights/") and msg.topic.endswith("/status"):
                parts = msg.topic.split("/")
                if len(parts) >= 3:
                    try:
                        known_light_ids.add(int(parts[2]))
                    except ValueError:
                        pass
            # Update device registry from PDM channel config
            elif msg.topic == "local/config/pdm_channels":
                channels = payload.get("channels", [])
                _update_device_registry(channels)
            # Update relay registry from Switchback channel config
            elif msg.topic == "local/config/relay_channels":
                channels = payload.get("channels", [])
                _update_relay_registry(channels)
        except (json.JSONDecodeError, ValueError):
            pass

    retry_delay = 5
    max_delay = 60
    while True:
        try:
            client = paho_mqtt.Client(paho_mqtt.CallbackAPIVersion.VERSION2)
            if MQTT_USERNAME:
                client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
            if MQTT_USE_TLS:
                if MQTT_CA_CERT and os.path.exists(MQTT_CA_CERT):
                    client.tls_set(
                        ca_certs=MQTT_CA_CERT,
                        cert_reqs=ssl.CERT_REQUIRED,
                        tls_version=ssl.PROTOCOL_TLSv1_2
                    )
                else:
                    client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLSv1_2)
                    client.tls_insecure_set(True)
            client.on_connect = on_mqtt_connect
            client.on_message = on_mqtt_message
            client.connect(MQTT_BROKER, MQTT_PORT)
            client.loop_start()
            mqtt = client
            # Wait for on_connect callback to confirm
            if _mqtt_connected.wait(timeout=10):
                return
            # Timed out waiting for on_connect — tear down and retry
            print(f"MQTT connect timed out, retrying in {retry_delay}s...")
            client.loop_stop()
            client.disconnect()
            mqtt = None
        except Exception as e:
            print(f"MQTT connection failed: {e} — retrying in {retry_delay}s...")
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, max_delay)

if MQTT_BROKER:
    try:
        _connect_mqtt()
    except ImportError:
        print("WARNING: paho-mqtt not installed, MQTT disabled")


def get_sensor_summary():
    """Build a human-readable summary of current sensor data for the LLM."""
    lines = []
    temp_data = sensor_data.get("local/airquality/temphumid")
    if temp_data:
        temp_f = temp_data.get("tempInF")
        humidity = temp_data.get("humidity")
        if temp_f is not None:
            lines.append(f"Temperature: {temp_f}°F")
        if humidity is not None:
            lines.append(f"Humidity: {humidity}%")

    aq_data = sensor_data.get("local/airquality/status")
    if aq_data:
        tvoc = aq_data.get("tvoc_ppb")
        if tvoc is not None:
            lines.append(f"TVOC: {tvoc} ppb")
        eco2 = aq_data.get("eco2_ppm")
        if eco2 is not None:
            lines.append(f"CO2: {eco2} ppm")

    energy = sensor_data.get("local/energy/status")
    if energy:
        batt = energy.get("battery_percent")
        solar = energy.get("solar_watts")
        voltage = energy.get("battery_voltage")
        if batt is not None:
            lines.append(f"Battery: {batt}%")
        if voltage is not None:
            lines.append(f"Battery voltage: {voltage}V")
        if energy.get("time_remaining_minutes") is not None:
            lines.append(f"Time to empty: {energy['time_remaining_minutes']} minutes")
        if energy.get("consumption_watts") is not None:
            lines.append(f"Consumption: {energy['consumption_watts']}W")
        if solar is not None:
            lines.append(f"Solar: {solar}W")
        if energy.get("charge_type") is not None:
            lines.append(f"Charge state: {energy['charge_type']}")

    gps = sensor_data.get("local/gps/latlon")
    if gps:
        lat = gps.get("latitude")
        lon = gps.get("longitude")
        if lat is not None and lon is not None:
            lines.append(f"Location: {lat}, {lon}")

    if not lines:
        return "No sensor data available yet."
    return "Current sensor readings:\n" + "\n".join(lines)


def get_system_prompt():
    """Build the full system prompt with current sensor data."""
    if not MQTT_BROKER:
        return (
            "You are a helpful voice assistant. Keep answers concise and conversational, "
            "ideally under 3 sentences. Do not use markdown, bullet points, or formatting "
            "since your response will be spoken aloud."
        )
    prompt = BASE_SYSTEM_PROMPT + get_sensor_summary()

    # Append device registry so the LLM knows device names and types
    if _device_registry:
        prompt += "\n\nConnected devices:\n"
        for key, info in sorted(_device_registry.items(), key=lambda x: x[1]["id"]):
            prompt += f"- ID {info['id']}: \"{info['name']}\" (type: {info['type']})\n"
        prompt += (
            "Use the device name in responses. "
            "Only lights support brightness. Relays and other devices are on/off only.\n"
        )

    return prompt


def _build_can_message(can_id, data_bytes):
    """Build a CAN outbound message matching the backend's format."""
    bit_arrays = []
    for byte in data_bytes:
        bits = [(byte >> i) & 1 for i in range(7, -1, -1)]
        bit_arrays.append(bits)
    while len(bit_arrays) < 8:
        bit_arrays.append([0, 0, 0, 0, 0, 0, 0, 0])
    return {
        "identifier": f"0x{can_id:x}",
        "data_length_code": min(len(data_bytes), 8),
        "data": bit_arrays[:8],
        "extd": 0, "rtr": 0, "ss": 0, "self": 0
    }



def _execute_light_command(light_id, state):
    """Send a light command via MQTT. Returns spoken confirmation.

    Handles both PDM lights (local/lights/{id}/command) and Switchback relays
    typed as "light" (local/relays/{channel}/command).
    """
    if not mqtt:
        return "Sorry, I'm not connected to the device system right now."

    state_word = "on" if state else "off"

    if light_id == "all":
        light_ids = [did for did, dtype in _device_types_by_id.items() if dtype == "light"]
        if not light_ids:
            return "I don't know which lights are available yet."
        for lid in sorted(light_ids):
            relay_ch = _relay_channel_by_id.get(lid)
            if relay_ch is not None:
                # Switchback relay typed as "light" — use relay topic
                topic = f"local/relays/{relay_ch}/command"
                payload = json.dumps({"state": state})
                mqtt.publish(topic, payload)
                print(f"  MQTT publish: {topic} -> {payload}")
            else:
                # PDM light — use lights topic
                topic = f"local/lights/{lid}/command"
                payload = json.dumps({"state": state})
                mqtt.publish(topic, payload)
                print(f"  MQTT publish: {topic} -> {payload}")
        return f"Turning {state_word} all lights."
    else:
        topic = f"local/lights/{light_id}/command"
        payload = json.dumps({"state": state})
        mqtt.publish(topic, payload)

        print(f"  MQTT publish: {topic} -> {payload}")
        name = _device_names_by_id.get(int(light_id), f"light {light_id}")
        return f"Turning {state_word} {name}."


def _execute_brightness_command(light_id, percent):
    """Set light brightness via MQTT. percent is 0-100, converted to 0-255 for CAN.

    light_id can be a number string or "all" (sends to each known light).
    """
    if not mqtt:
        return "Sorry, I'm not connected to the device system right now."

    brightness = max(0, min(255, round(percent * 255 / 100)))
    payload = json.dumps({"brightness": brightness})

    if light_id == "all":
        light_ids = [did for did, dtype in _device_types_by_id.items() if dtype == "light"]
        if not light_ids:
            return "I don't know which lights are available yet."
        for lid in sorted(light_ids):
            topic = f"local/lights/{lid}/brightness"
            mqtt.publish(topic, payload)

            print(f"  MQTT publish: {topic} -> {payload} ({percent}%)")
        return f"Setting all lights to {percent} percent."

    topic = f"local/lights/{light_id}/brightness"
    mqtt.publish(topic, payload)
    print(f"  MQTT publish: {topic} -> {payload} ({percent}%)")
    name = _device_names_by_id.get(int(light_id), f"light {light_id}")
    return f"Setting {name} to {percent} percent."


def _execute_device_command(device_id, device_name, device_type, state):
    """Send an on/off command for any device via MQTT.

    Routes to relay topics for Switchback devices, light topics for PDM devices.
    """
    if not mqtt:
        return "Sorry, I'm not connected to the device system right now."

    # Route relay devices through the relay MQTT topics
    relay_ch = _relay_channel_by_id.get(int(device_id))
    if relay_ch is not None or device_type == "relay":
        return _execute_relay_command(device_id, device_name, state, relay_ch)

    state_word = "on" if state else "off"
    topic = f"local/lights/{device_id}/command"
    payload = json.dumps({"state": state})
    mqtt.publish(topic, payload)
    print(f"  MQTT publish: {topic} -> {payload}")
    return f"Turning {state_word} the {device_name}."


def _execute_relay_command(device_id, device_name, state, relay_channel=None):
    """Send a relay on/off command via MQTT.

    Individual relays are toggled via local/relays/{channel}/command.
    The Switchback CAN protocol only supports toggle for individual relays,
    so we check current state and skip if already in the desired state.
    """
    if not mqtt:
        return "Sorry, I'm not connected to the device system right now."
    state_word = "on" if state else "off"

    if relay_channel is None:
        relay_channel = _relay_channel_by_id.get(int(device_id))
    if relay_channel is None:
        return f"Sorry, I don't know the relay channel for {device_name}."

    topic = f"local/relays/{relay_channel}/command"
    payload = json.dumps({"state": state})
    mqtt.publish(topic, payload)
    print(f"  MQTT publish: {topic} -> {payload}")
    return f"Turning {state_word} the {device_name}."


def _execute_relay_all_command(state):
    """Send all-relays on/off command via MQTT."""
    if not mqtt:
        return "Sorry, I'm not connected to the device system right now."
    state_word = "on" if state else "off"

    if not _relay_channel_by_id:
        return "I don't know which relays are available yet."

    topic = "local/relays/all/command"
    payload = json.dumps({"state": state})
    mqtt.publish(topic, payload)
    print(f"  MQTT publish: {topic} -> {payload}")
    return f"Turning {state_word} all relays."


def _get_device_status_response(device_id, device_name):
    """Get status for any device from cached MQTT data."""
    # Check relay status first
    relay_ch = _relay_channel_by_id.get(int(device_id))
    if relay_ch is not None:
        topic = f"local/relays/{relay_ch}/status"
        payload = sensor_data.get(topic)
        if not payload:
            return f"I don't have status data for the {device_name}."
        state_word = "on" if payload.get("state") == 1 else "off"
        return f"The {device_name} is {state_word}."

    topic = f"local/lights/{device_id}/status"
    payload = sensor_data.get(topic)
    if not payload:
        return f"I don't have status data for the {device_name}."
    state_word = "on" if payload.get("state") == 1 else "off"
    return f"The {device_name} is {state_word}."


# --- Device resolution (named devices from MQTT config) ---

_FUZZY_THRESHOLD = 0.85


def _resolve_device(text):
    """Try to resolve a device name from spoken text.

    Returns (device_id, device_type, device_name) or None.
    Uses exact substring match first, then fuzzy n-gram matching.
    """
    if not _device_registry:
        return None

    text_lower = text.lower()

    # Pass 1: exact substring match (longest name first to prefer "living room" over "living")
    for key in sorted(_device_registry.keys(), key=len, reverse=True):
        if key in text_lower:
            info = _device_registry[key]
            return (info["id"], info["type"], info["name"])

    # No exact match — bail on generic phrases so fuzzy matching doesn't
    # false-match "the lights" to a specific device like "Kitchen Lights".
    if re.search(r"\b(?:everything|(?:(?:all|which|the|my)\s+(?:the\s+)?(?:lights?|lamps?|devices?)))\b", text, re.I):
        return None

    # Pass 2: fuzzy n-gram match
    words = text_lower.split()
    best_score = 0.0
    best_match = None
    for key, info in _device_registry.items():
        key_word_count = len(key.split())
        for i in range(len(words) - key_word_count + 1):
            ngram = " ".join(words[i:i + key_word_count])
            score = difflib.SequenceMatcher(None, ngram, key).ratio()
            if score > best_score:
                best_score = score
                best_match = info

    if best_score >= _FUZZY_THRESHOLD and best_match:
        print(f"  Fuzzy device match: '{best_match['name']}' (score={best_score:.2f})")
        return (best_match["id"], best_match["type"], best_match["name"])

    return None


# Generic command patterns (device-type agnostic, used with named device resolution)
_GENERIC_ON_PATTERNS = [
    re.compile(r"\b(?:turn|switch|put)\s+(?:\w+\s+)*on\b", re.I),
    re.compile(r"\b(?:turn|switch|put)\s+on\b", re.I),
    re.compile(r"\b(?:enable|activate|start)\b", re.I),
]
_GENERIC_OFF_PATTERNS = [
    re.compile(r"\b(?:turn|switch|put)\s+(?:\w+\s+)*off\b", re.I),
    re.compile(r"\b(?:turn|switch|put)\s+off\b", re.I),
    re.compile(r"\b(?:disable|deactivate|stop|kill|shut)\b", re.I),
]
_GENERIC_BRIGHTNESS_PATTERN = re.compile(
    r"\b(?:set|dim|adjust|change|brightness)?\s*(?:to\s+|at\s+)?(\d+)\s*percent\b", re.I
)


# --- Intent matching (fast path, no LLM needed) ---

# Whisper STT normalization: fix common mistranscriptions before matching.
# Each rule is (compiled_pattern, replacement). Applied in order to the raw text.
# Only context-safe substitutions — e.g. "light to" -> "light 2" won't fire
# on "I want to turn on the light" because we anchor after "light".
_STT_NORMALIZATIONS = [
    # Numbers after "light" — Whisper often writes homophones
    (re.compile(r"\blight\s+(?:won)\b", re.I), "light 1"),
    (re.compile(r"\blight\s+(?:too?)\b", re.I), "light 2"),
    (re.compile(r"\blight\s+(?:tree)\b", re.I), "light 3"),
    (re.compile(r"\blight\s+(?:for|fore)\b", re.I), "light 4"),
    (re.compile(r"\blight\s+(?:ate)\b", re.I), "light 8"),
    # Spoken number words after "light" -> digits
    (re.compile(r"\blight\s+one\b", re.I), "light 1"),
    (re.compile(r"\blight\s+two\b", re.I), "light 2"),
    (re.compile(r"\blight\s+three\b", re.I), "light 3"),
    (re.compile(r"\blight\s+four\b", re.I), "light 4"),
    (re.compile(r"\blight\s+five\b", re.I), "light 5"),
    (re.compile(r"\blight\s+six\b", re.I), "light 6"),
    (re.compile(r"\blight\s+seven\b", re.I), "light 7"),
    (re.compile(r"\blight\s+eight\b", re.I), "light 8"),
    (re.compile(r"\blight\s+nine\b", re.I), "light 9"),
    (re.compile(r"\blight\s+ten\b", re.I), "light 10"),
    # Ordinals after "light"
    (re.compile(r"\blight\s+(?:first|1st)\b", re.I), "light 1"),
    (re.compile(r"\blight\s+(?:second|2nd)\b", re.I), "light 2"),
    (re.compile(r"\blight\s+(?:third|3rd)\b", re.I), "light 3"),
    # Spoken number words after "relay" -> digits
    (re.compile(r"\brelay\s+one\b", re.I), "relay 1"),
    (re.compile(r"\brelay\s+two\b", re.I), "relay 2"),
    (re.compile(r"\brelay\s+three\b", re.I), "relay 3"),
    (re.compile(r"\brelay\s+four\b", re.I), "relay 4"),
    (re.compile(r"\brelay\s+five\b", re.I), "relay 5"),
    (re.compile(r"\brelay\s+six\b", re.I), "relay 6"),
    (re.compile(r"\brelay\s+seven\b", re.I), "relay 7"),
    (re.compile(r"\brelay\s+eight\b", re.I), "relay 8"),
    (re.compile(r"\brelay\s+(?:won)\b", re.I), "relay 1"),
    (re.compile(r"\brelay\s+(?:too?)\b", re.I), "relay 2"),
    (re.compile(r"\brelay\s+(?:tree)\b", re.I), "relay 3"),
    (re.compile(r"\brelay\s+(?:for|fore)\b", re.I), "relay 4"),
    # Common Whisper mishearings for key nouns
    (re.compile(r"\b(?:lites|lite)\b", re.I), "light"),
    (re.compile(r"\blied\b", re.I), "light"),  # "turn on the lied"
    # "humidity" mishearings
    (re.compile(r"\bhumid(?:idy|ity|idy)\b", re.I), "humidity"),
    # "temperature" mishearings
    (re.compile(r"\btempature\b", re.I), "temperature"),
    (re.compile(r"\btemprature\b", re.I), "temperature"),
    # "TVOC" / "CO2" mishearings
    (re.compile(r"\bt[\s-]*voc\b", re.I), "tvoc"),
    (re.compile(r"\btee\s*voc\b", re.I), "tvoc"),
    (re.compile(r"\bco\s*too\b", re.I), "co2"),
    (re.compile(r"\bceo\s*two\b", re.I), "co2"),
    (re.compile(r"\bsee\s*oh?\s*two\b", re.I), "co2"),
    # "battery" mishearings
    (re.compile(r"\bbattery?i\b", re.I), "battery"),
    # "status" mishearings
    (re.compile(r"\bstatis\b", re.I), "status"),
    # "percent" / "%" normalization — Whisper may write "50%" or "fifty percent"
    (re.compile(r"\b(\d+)\s*%", re.I), r"\1 percent"),
    # Spoken percentages -> digits
    (re.compile(r"\bten\s+percent\b", re.I), "10 percent"),
    (re.compile(r"\btwenty\s+percent\b", re.I), "20 percent"),
    (re.compile(r"\btwenty[\s-]*five\s+percent\b", re.I), "25 percent"),
    (re.compile(r"\bthirty\s+percent\b", re.I), "30 percent"),
    (re.compile(r"\bforty\s+percent\b", re.I), "40 percent"),
    (re.compile(r"\bfifty\s+percent\b", re.I), "50 percent"),
    (re.compile(r"\bsixty\s+percent\b", re.I), "60 percent"),
    (re.compile(r"\bseventy[\s-]*five\s+percent\b", re.I), "75 percent"),
    (re.compile(r"\bseventy\s+percent\b", re.I), "70 percent"),
    (re.compile(r"\beighty\s+percent\b", re.I), "80 percent"),
    (re.compile(r"\bninety\s+percent\b", re.I), "90 percent"),
    (re.compile(r"\bhundred\s+percent\b", re.I), "100 percent"),
    # "brightness" mishearings
    (re.compile(r"\bbright?nes\b", re.I), "brightness"),
    # Trailing punctuation cleanup (Whisper adds periods, commas)
    (re.compile(r"[.,!?]+$"), ""),
]


def _normalize_stt(text):
    """Apply STT normalization rules to fix common Whisper mistranscriptions."""
    for pattern, replacement in _STT_NORMALIZATIONS:
        text = pattern.sub(replacement, text)
    return text.strip()


# Question words that indicate a status query, not a command
_IS_QUESTION = re.compile(r"\b(?:are|is|which|what|how\s+many|any|status|check|currently)\b", re.I)

# Light command patterns: "turn on/off (the/all) lights", "lights on/off", "light 3 on"
_LIGHT_ON_PATTERNS = [
    re.compile(r"\b(?:turn|switch|put)\s+on\s+(?:(?:the|all|my)\s+)*(?:lights?|lamps?)(?:\s+\d+)?\b", re.I),
    re.compile(r"\b(?:turn|switch|put)\s+(?:(?:the|all|my)\s+)*(?:lights?|lamps?)\s+\d+\s+on\b", re.I),
    re.compile(r"\b(?:lights?|lamps?)(?:\s+\d+)?\s+on\b", re.I),
    re.compile(r"\b(?:enable|activate)\s+(?:(?:the|all|my)\s+)*(?:lights?|lamps?)(?:\s+\d+)?\b", re.I),
]
_LIGHT_OFF_PATTERNS = [
    re.compile(r"\b(?:turn|switch|put)\s+off\s+(?:(?:the|all|my)\s+)*(?:lights?|lamps?)(?:\s+\d+)?\b", re.I),
    re.compile(r"\b(?:turn|switch|put)\s+(?:(?:the|all|my)\s+)*(?:lights?|lamps?)\s+\d+\s+off\b", re.I),
    re.compile(r"\b(?:lights?|lamps?)(?:\s+\d+)?\s+off\b", re.I),
    re.compile(r"\b(?:disable|deactivate|kill)\s+(?:(?:the|all|my)\s+)*(?:lights?|lamps?)(?:\s+\d+)?\b", re.I),
]
_LIGHT_ID_PATTERN = re.compile(r"\blight\s+(\d+)\b", re.I)

# Brightness patterns: "set light 1 to 50 percent", "dim light 2 to 30 percent",
# "light 3 brightness 75 percent", "light 1 at 50 percent"
_BRIGHTNESS_PATTERN = re.compile(
    r"\b(?:set|dim|adjust|change)?\s*(?:(?:the|my)\s+)?light\s+(\d+)\s+"
    r"(?:to\s+|at\s+|brightness\s+(?:to\s+)?)?(\d+)\s*percent\b", re.I
)
# "set brightness of light 2 to 50 percent"
_BRIGHTNESS_PATTERN_ALT = re.compile(
    r"\b(?:set|change|adjust)\s+(?:the\s+)?brightness\s+(?:of\s+)?(?:(?:the|my)\s+)?light\s+(\d+)\s+"
    r"(?:to\s+)?(\d+)\s*percent\b", re.I
)
# "set all lights to 25 percent", "dim all the lights to 50 percent brightness",
# "all lights to 50 percent" (no verb)
_BRIGHTNESS_ALL_PATTERN = re.compile(
    r"\b(?:(?:set|dim|adjust|change)\s+)?(?:(?:the|all|my)\s+)*(?:lights?|lamps?)\s+"
    r"(?:to\s+|at\s+|brightness\s+(?:to\s+)?)?(\d+)\s*percent\b", re.I
)


def _extract_light_id(text):
    """Extract a numeric light ID from text (normalization already converted words to digits)."""
    m = _LIGHT_ID_PATTERN.search(text)
    return m.group(1) if m else None


# Light status query: "are the lights on", "which lights are on", "light status"
_LIGHT_STATUS_PATTERNS = [
    re.compile(r"\b(?:are|is)\b.*\b(?:lights?|lamps?)\b.*\b(?:on|off)\b", re.I),
    re.compile(r"\b(?:which|what)\s+(?:lights?|lamps?)\b", re.I),
    re.compile(r"\b(?:lights?|lamps?)\s+status\b", re.I),
    re.compile(r"\bstatus\s+(?:of\s+)?(?:\w+\s+)*(?:lights?|lamps?)\b", re.I),
    re.compile(r"\b(?:what|how)\b.*\bstatus\b.*\b(?:lights?|lamps?)\b", re.I),
    re.compile(r"\b(?:what|how)\b.*\b(?:lights?|lamps?)\b", re.I),
]
_RELAY_STATUS_PATTERNS = [
    re.compile(r"\b(?:are|is)\b.*\b(?:relays?|switches?|outlets?)\b.*\b(?:on|off)\b", re.I),
    re.compile(r"\b(?:which|what)\s+(?:relays?|switches?|outlets?)\b", re.I),
    re.compile(r"\b(?:relays?|switches?|outlets?)\s+status\b", re.I),
    re.compile(r"\bstatus\s+(?:of\s+)?(?:\w+\s+)*(?:relays?|switches?|outlets?)\b", re.I),
    re.compile(r"\b(?:what|how)\b.*\b(?:relays?|switches?|outlets?)\b", re.I),
]

# Sensor query patterns
_TEMP_CELSIUS_PATTERNS = [
    re.compile(r"\b(?:celsius|centigrade)\b", re.I),
    re.compile(r"\btemp.*\bin\s+c\b", re.I),
]
_TEMP_PATTERNS = [
    re.compile(r"\b(?:temperature|temp|how\s+(?:hot|cold|warm)|thermostat)\b", re.I),
    re.compile(r"\b(?:what(?:'s|s|\s+is))\s+(?:the\s+)?(?:temp|temperature)\b", re.I),
    re.compile(r"\bhow\s+(?:hot|cold|warm)\b", re.I),
]
_HUMIDITY_PATTERNS = [
    re.compile(r"\bhumidity\b", re.I),
    re.compile(r"\bhow\s+(?:humid|muggy|dry)\b", re.I),
]
_AIR_QUALITY_PATTERNS = [
    re.compile(r"\bair\s*quality\b", re.I),
    re.compile(r"\bvocs?\b.*\b(?:co2|carbon)\b", re.I),
    re.compile(r"\b(?:co2|carbon)\b.*\bvocs?\b", re.I),
]
_TVOC_PATTERNS = [
    re.compile(r"\b(?:tvoc|voc)s?\b", re.I),
    re.compile(r"\bvolatile\s+organic\b", re.I),
    re.compile(r"\bvoc\s*level\b", re.I),
]
_CO2_PATTERNS = [
    re.compile(r"\b(?:co2|carbon\s*(?:dioxide|monoxide))\b", re.I),
    re.compile(r"\bcarbon\b", re.I),
]
_BATTERY_PATTERNS = [
    re.compile(r"\b(?:battery|charge|power|energy|solar|voltage)\b", re.I),
    re.compile(r"\bhow\s+much\s+(?:power|charge|battery|energy)\b", re.I),
]
_LOCATION_PATTERNS = [
    re.compile(r"\b(?:location|where\s+(?:am\s+i|are\s+we)|gps|coordinates?)\b", re.I),
    re.compile(r"\bwhere\s+(?:am\s+i|are\s+we|is\s+(?:the|this))\b", re.I),
]
_TIME_PATTERNS = [
    re.compile(r"\bwhat\s+time\b", re.I),
    re.compile(r"\bwhat(?:'s|s|\s+is)\s+the\s+time\b", re.I),
    re.compile(r"\bcurrent\s+time\b", re.I),
]
_DATE_PATTERNS = [
    re.compile(r"\bwhat(?:'s|s|\s+is)\s+(?:the\s+)?date\b", re.I),
    re.compile(r"\bwhat\s+day\s+is\s+it\b", re.I),
    re.compile(r"\bwhat(?:'s|s|\s+is)\s+today\b", re.I),
    re.compile(r"\btoday(?:'s|s)?\s+(?:date|day)\b", re.I),
    re.compile(r"\bcurrent\s+date\b", re.I),
]
_DST_PATTERNS = [
    re.compile(r"\b(?:time\s+change|daylight\s+saving|dst)\b", re.I),
    re.compile(r"\b(?:spring|fall)\s+(?:forward|back)\b", re.I),
    re.compile(r"\bclocks?\s+(?:change|go\s+(?:forward|back))\b", re.I),
]
_ELEVATION_PATTERNS = [
    re.compile(r"\b(?:elevation|altitude)\b", re.I),
    re.compile(r"\bhow\s+high\s+(?:up\s+)?(?:am\s+i|are\s+we)\b", re.I),
]


def _get_light_status_response(light_id=None):
    """Build a spoken summary of light states from cached MQTT data.

    If light_id is given, report on that specific light only.
    """
    if light_id is not None:
        topic = f"local/lights/{light_id}/status"
        payload = sensor_data.get(topic)
        if not payload:
            return f"I don't have status data for light {light_id}."
        try:
            name = _device_names_by_id.get(int(light_id), f"light {light_id}")
        except (ValueError, TypeError):
            name = f"light {light_id}"
        state_word = "on" if payload.get("state") == 1 else "off"
        return f"{name} is {state_word}."

    on_lights = []
    off_lights = []
    for topic, payload in sensor_data.items():
        if topic.startswith("local/lights/") and topic.endswith("/status"):
            parts = topic.split("/")
            if len(parts) >= 3:
                lid = parts[2]
                try:
                    lid_int = int(lid)
                except (ValueError, TypeError):
                    continue
                # Only report devices of type "light"
                if _device_types_by_id.get(lid_int) not in (None, "light"):
                    continue
                name = _device_names_by_id.get(lid_int, f"light {lid}")
                if payload.get("state") == 1:
                    on_lights.append(name)
                else:
                    off_lights.append(name)

    if not on_lights and not off_lights:
        return "I don't have light status data right now."
    if on_lights and not off_lights:
        return f"All lights are on: {', '.join(on_lights)}."
    if off_lights and not on_lights:
        return "All lights are currently off."
    return f"{', '.join(on_lights)} {'is' if len(on_lights) == 1 else 'are'} on. {', '.join(off_lights)} {'is' if len(off_lights) == 1 else 'are'} off."


def _get_relay_status_response():
    """Build a spoken summary of relay states from cached MQTT data."""
    on_relays = []
    off_relays = []
    for dev_id, relay_ch in _relay_channel_by_id.items():
        topic = f"local/relays/{relay_ch}/status"
        payload = sensor_data.get(topic, {})
        name = _device_names_by_id.get(dev_id, f"relay {relay_ch}")
        if payload.get("state") == 1:
            on_relays.append(name)
        else:
            off_relays.append(name)

    if not on_relays and not off_relays:
        return "I don't have relay status data right now."
    if on_relays and not off_relays:
        return f"All relays are on: {', '.join(on_relays)}."
    if off_relays and not on_relays:
        return "All relays are currently off."
    return f"{', '.join(on_relays)} {'is' if len(on_relays) == 1 else 'are'} on. {', '.join(off_relays)} {'is' if len(off_relays) == 1 else 'are'} off."


def _gps_to_local_datetime(gps_time):
    """Convert GPS UTC time to local time using the IANA timezone for the
    current GPS coordinates.  This handles DST transitions correctly.

    Falls back to a simple longitude-based offset if timezonefinder is
    not installed.  Returns a naive local datetime or None.
    """
    gps = sensor_data.get("local/gps/latlon")
    if not gps or gps.get("longitude") is None:
        return None
    try:
        utc_dt = datetime(
            gps_time["year"], gps_time["month"], gps_time["day"],
            gps_time["hour"], gps_time.get("minute", 0),
            gps_time.get("second", 0), tzinfo=timezone.utc,
        )

        # Prefer real timezone lookup (DST-aware)
        if _tz_finder is not None and gps.get("latitude") is not None:
            tz_name = _tz_finder.timezone_at(
                lat=gps["latitude"], lng=gps["longitude"],
            )
            if tz_name:
                local_dt = utc_dt.astimezone(ZoneInfo(tz_name))
                return local_dt.replace(tzinfo=None)

        # Fallback: solar-time approximation (no DST)
        offset_hours = round(gps["longitude"] / 15)
        return utc_dt + timedelta(hours=offset_hours)
    except (ValueError, KeyError, TypeError):
        return None


def _next_dst_transition(tz_name, now_utc):
    """Find the next DST transition for a timezone.

    Scans day-by-day up to 365 days out looking for a UTC offset change.
    Returns (local_datetime, direction) where direction is 'spring forward'
    or 'fall back', or None if no transition found (e.g. timezone has no DST).
    """
    try:
        tz = ZoneInfo(tz_name)
        prev_offset = now_utc.astimezone(tz).utcoffset()
        for day in range(1, 366):
            check = now_utc + timedelta(days=day)
            cur_offset = check.astimezone(tz).utcoffset()
            if cur_offset != prev_offset:
                # Found the day of transition — binary search for exact moment
                lo = check - timedelta(days=1)
                hi = check
                for _ in range(20):  # converge to ~1 minute
                    mid = lo + (hi - lo) / 2
                    if mid.astimezone(tz).utcoffset() == prev_offset:
                        lo = mid
                    else:
                        hi = mid
                transition_local = hi.astimezone(tz)
                direction = "spring forward" if cur_offset > prev_offset else "fall back"
                return transition_local, direction
            prev_offset = cur_offset
    except Exception:
        pass
    return None


def match_intent(text):
    """Try to match user text to a known intent.

    Returns a spoken response string, or None if no intent matched (fall back to LLM).
    """
    original = text
    text = _normalize_stt(text)
    if text != original:
        print(f"  STT normalized: \"{original}\" -> \"{text}\"")
    is_question = bool(_IS_QUESTION.search(text))

    # --- Named device resolution (dynamic from MQTT config) ---
    device = _resolve_device(text)
    if device:
        dev_id, dev_type, dev_name = device
        print(f"  Resolved device: '{dev_name}' (id={dev_id}, type={dev_type})")

        # Status query
        if is_question:
            print(f"  Intent: device status query (id={dev_id})")
            return _get_device_status_response(dev_id, dev_name)

        # Brightness command (lights only — relays have no dimming)
        bm = _GENERIC_BRIGHTNESS_PATTERN.search(text)
        if bm:
            if dev_type != "light" or dev_id in _relay_channel_by_id:
                return f"The {dev_name} doesn't support brightness control."
            b_percent = max(0, min(100, int(bm.group(1))))
            print(f"  Intent: brightness (id={dev_id}, {b_percent}%)")
            return _execute_brightness_command(str(dev_id), b_percent)

        # On/off commands
        for pattern in _GENERIC_ON_PATTERNS:
            if pattern.search(text):
                print(f"  Intent: device on (id={dev_id})")
                return _execute_device_command(dev_id, dev_name, dev_type, 1)

        for pattern in _GENERIC_OFF_PATTERNS:
            if pattern.search(text):
                print(f"  Intent: device off (id={dev_id})")
                return _execute_device_command(dev_id, dev_name, dev_type, 0)

    # --- "Everything" / "all" commands (no specific device or light keyword) ---
    # "all the lights" → only type=="light" devices (both PDM and relay-backed)
    # "everything" → all devices including non-light relays
    if not is_question and re.search(r"\b(?:everything|all)\b", text, re.I):
        mentions_lights = bool(re.search(r"\b(?:lights?|lamps?)\b", text, re.I))
        include_all_relays = not mentions_lights and bool(_relay_channel_by_id)
        for pattern in _GENERIC_ON_PATTERNS:
            if pattern.search(text):
                print(f"  Intent: all {'devices' if include_all_relays else 'lights'} on")
                responses = []
                light_resp = _execute_light_command("all", 1)
                if light_resp:
                    responses.append(light_resp)
                if include_all_relays:
                    responses.append(_execute_relay_all_command(1))
                return " ".join(responses) if responses else "No devices available."
        for pattern in _GENERIC_OFF_PATTERNS:
            if pattern.search(text):
                print(f"  Intent: all {'devices' if include_all_relays else 'lights'} off")
                responses = []
                light_resp = _execute_light_command("all", 0)
                if light_resp:
                    responses.append(light_resp)
                if include_all_relays:
                    responses.append(_execute_relay_all_command(0))
                return " ".join(responses) if responses else "No devices available."

    # --- Light status queries (check before commands to avoid false triggers) ---
    for pattern in _LIGHT_STATUS_PATTERNS:
        if pattern.search(text):
            status_id = _extract_light_id(text)
            print(f"  Intent: light status query (id={status_id or 'all'})")
            return _get_light_status_response(light_id=status_id)

    # --- Relay status queries ---
    for pattern in _RELAY_STATUS_PATTERNS:
        if pattern.search(text):
            print("  Intent: relay status query")
            return _get_relay_status_response()

    # --- Brightness commands (check before on/off since "set light 1 to 50%" is more specific) ---
    if not is_question:
        # All-lights brightness: "set all lights to 25 percent"
        bm_all = _BRIGHTNESS_ALL_PATTERN.search(text)
        if bm_all:
            b_percent = max(0, min(100, int(bm_all.group(1))))
            print(f"  Intent: brightness (id=all, {b_percent}%)")
            return _execute_brightness_command("all", b_percent)
        # Per-light brightness: "set light 1 to 50 percent"
        for bp in (_BRIGHTNESS_PATTERN, _BRIGHTNESS_PATTERN_ALT):
            bm = bp.search(text)
            if bm:
                b_light_id = bm.group(1)
                b_percent = max(0, min(100, int(bm.group(2))))
                print(f"  Intent: brightness (id={b_light_id}, {b_percent}%)")
                return _execute_brightness_command(b_light_id, b_percent)

    # --- Light on/off commands (skip if it looks like a question) ---
    if not is_question:
        light_id = _extract_light_id(text) or "all"

        for pattern in _LIGHT_ON_PATTERNS:
            if pattern.search(text):
                print(f"  Intent: lights on (id={light_id})")
                return _execute_light_command(light_id, 1)

        for pattern in _LIGHT_OFF_PATTERNS:
            if pattern.search(text):
                print(f"  Intent: lights off (id={light_id})")
                return _execute_light_command(light_id, 0)

    # --- Sensor queries ---

    # Celsius-specific temperature request (check before general temp patterns)
    for pattern in _TEMP_CELSIUS_PATTERNS:
        if pattern.search(text):
            print("  Intent: temperature query (Celsius)")
            temp_data = sensor_data.get("local/airquality/temphumid")
            if temp_data and temp_data.get("tempInC") is not None:
                return f"It's currently {temp_data['tempInC']} degrees Celsius."
            return "I don't have temperature data right now."

    for pattern in _TEMP_PATTERNS:
        if pattern.search(text):
            print("  Intent: temperature query")
            temp_data = sensor_data.get("local/airquality/temphumid")
            if temp_data and temp_data.get("tempInF") is not None:
                temp = temp_data["tempInF"]
                humidity = temp_data.get("humidity")
                resp = f"It's currently {temp} degrees"
                if humidity is not None:
                    resp += f" with {humidity}% humidity"
                return resp + "."
            return "I don't have temperature data right now."

    for pattern in _HUMIDITY_PATTERNS:
        if pattern.search(text):
            print("  Intent: humidity query")
            temp_data = sensor_data.get("local/airquality/temphumid")
            if temp_data and temp_data.get("humidity") is not None:
                return f"Humidity is currently {temp_data['humidity']}%."
            return "I don't have humidity data right now."

    for pattern in _AIR_QUALITY_PATTERNS:
        if pattern.search(text):
            print("  Intent: air quality query")
            th = sensor_data.get("local/airquality/temphumid")
            aq = sensor_data.get("local/airquality/status")
            parts = []
            if aq:
                if aq.get("tvoc_ppb") is not None:
                    parts.append(f"TVOC is {aq['tvoc_ppb']} parts per billion")
                if aq.get("eco2_ppm") is not None:
                    parts.append(f"CO2 is {aq['eco2_ppm']} parts per million")
            if th:
                if th.get("tempInF") is not None:
                    parts.append(f"temperature is {th['tempInF']} degrees")
                if th.get("humidity") is not None:
                    parts.append(f"humidity is {th['humidity']}%")
            if parts:
                return "The " + ", ".join(parts) + "."
            return "I don't have air quality data right now."

    for pattern in _TVOC_PATTERNS:
        if pattern.search(text):
            print("  Intent: TVOC query")
            aq = sensor_data.get("local/airquality/status")
            if aq and aq.get("tvoc_ppb") is not None:
                return f"TVOC is currently {aq['tvoc_ppb']} parts per billion."
            return "I don't have TVOC data right now."

    for pattern in _CO2_PATTERNS:
        if pattern.search(text):
            print("  Intent: CO2 query")
            aq = sensor_data.get("local/airquality/status")
            if aq and aq.get("eco2_ppm") is not None:
                return f"CO2 is currently {aq['eco2_ppm']} parts per million."
            return "I don't have CO2 data right now."

    for pattern in _BATTERY_PATTERNS:
        if pattern.search(text):
            print("  Intent: battery/energy query")
            energy = sensor_data.get("local/energy/status")
            if energy:
                parts = []
                if energy.get("battery_percent") is not None:
                    parts.append(f"battery is at {energy['battery_percent']}%")
                if energy.get("battery_voltage") is not None:
                    parts.append(f"at {energy['battery_voltage']} volts")
                if parts:
                    response = "The " + " ".join(parts) + "."
                else:
                    response = ""
                # Time to empty
                ttg = energy.get("time_remaining_minutes")
                if ttg is not None and ttg > 0:
                    days, remainder = divmod(int(ttg), 1440)
                    hours, minutes = divmod(remainder, 60)
                    time_parts = []
                    if days > 0:
                        time_parts.append(f"{days} day{'s' if days != 1 else ''}")
                    if hours > 0:
                        time_parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
                    if minutes > 0 and days == 0:
                        time_parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
                    if time_parts:
                        response += f" Estimated time remaining is {' and '.join(time_parts)}."
                # Solar and consumption
                extras = []
                if energy.get("solar_watts") is not None:
                    extras.append(f"solar is producing {energy['solar_watts']} watts")
                if energy.get("consumption_watts") is not None:
                    extras.append(f"current draw is {energy['consumption_watts']} watts")
                if extras:
                    response += " " + ", and ".join(extras).capitalize() + "."
                if response:
                    return response.strip()
            return "I don't have energy data right now."

    for pattern in _LOCATION_PATTERNS:
        if pattern.search(text):
            print("  Intent: location query")
            gps = sensor_data.get("local/gps/latlon")
            if gps and gps.get("latitude") is not None:
                return f"We're at latitude {gps['latitude']}, longitude {gps['longitude']}."
            return "I don't have location data right now."

    for pattern in _TIME_PATTERNS:
        if pattern.search(text):
            print("  Intent: time query")
            gps_time = sensor_data.get("local/gps/time")
            if gps_time and gps_time.get("hour") is not None:
                hour, minute = gps_time["hour"], gps_time.get("minute", 0)
                local_dt = _gps_to_local_datetime(gps_time)
                if local_dt:
                    hour, minute = local_dt.hour, local_dt.minute
                ampm = "AM" if hour < 12 else "PM"
                hour_12 = hour % 12 or 12
                if minute == 0:
                    return f"It's {hour_12} {ampm}."
                return f"It's {hour_12}:{minute:02d} {ampm}."
            return "I don't have time data right now."

    for pattern in _DATE_PATTERNS:
        if pattern.search(text):
            print("  Intent: date query")
            gps_time = sensor_data.get("local/gps/time")
            if gps_time and gps_time.get("year") is not None:
                months = ["", "January", "February", "March", "April", "May",
                          "June", "July", "August", "September", "October",
                          "November", "December"]
                local_dt = _gps_to_local_datetime(gps_time)
                if local_dt:
                    m = local_dt.month
                    month_name = months[m] if 1 <= m <= 12 else str(m)
                    return f"Today is {month_name} {local_dt.day}, {local_dt.year}."
                m = gps_time.get("month", 0)
                month_name = months[m] if 1 <= m <= 12 else str(m)
                return f"Today is {month_name} {gps_time.get('day', '?')}, {gps_time['year']}."
            return "I don't have date data right now."

    for pattern in _DST_PATTERNS:
        if pattern.search(text):
            print("  Intent: DST / time change query")
            gps_time = sensor_data.get("local/gps/time")
            gps = sensor_data.get("local/gps/latlon")
            if (gps_time and gps_time.get("year") is not None
                    and _tz_finder is not None
                    and gps and gps.get("latitude") is not None):
                tz_name = _tz_finder.timezone_at(
                    lat=gps["latitude"], lng=gps["longitude"],
                )
                if tz_name:
                    now_utc = datetime(
                        gps_time["year"], gps_time["month"], gps_time["day"],
                        gps_time["hour"], gps_time.get("minute", 0),
                        gps_time.get("second", 0), tzinfo=timezone.utc,
                    )
                    result = _next_dst_transition(tz_name, now_utc)
                    if result:
                        trans_dt, direction = result
                        months = ["", "January", "February", "March", "April",
                                  "May", "June", "July", "August", "September",
                                  "October", "November", "December"]
                        month_name = months[trans_dt.month]
                        hour_12 = trans_dt.hour % 12 or 12
                        ampm = "AM" if trans_dt.hour < 12 else "PM"
                        return (
                            f"The next time change is {month_name} {trans_dt.day}, "
                            f"{trans_dt.year} at {hour_12} {ampm} when clocks {direction}."
                        )
                    return "Your current timezone doesn't observe daylight saving time."
            return "I don't have location data to determine time zone changes."

    for pattern in _ELEVATION_PATTERNS:
        if pattern.search(text):
            print("  Intent: elevation query")
            alt = sensor_data.get("local/gps/alt")
            if alt and alt.get("altitudeFeet") is not None:
                return f"We're at {alt['altitudeFeet']} feet, or about {alt['altitudeInMeters']:.0f} meters above sea level."
            return "I don't have elevation data right now."

    return None  # No intent matched, fall back to LLM


def _extract_json_objects(text):
    """Extract all JSON objects from a string (handles multiple on separate lines)."""
    objects = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Try the whole line
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                objects.append(obj)
                continue
        except (json.JSONDecodeError, ValueError):
            pass
        # Try extracting {...} from within the line
        start = line.find("{")
        end = line.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                obj = json.loads(line[start:end])
                if isinstance(obj, dict):
                    objects.append(obj)
            except (json.JSONDecodeError, ValueError):
                pass
    return objects


def handle_command(response_text):
    """Parse LLM response for JSON commands. Returns spoken confirmation or None."""
    commands = _extract_json_objects(response_text)
    if not commands:
        return None

    # Execute the first valid command
    cmd = commands[0]
    if "action" not in cmd:
        return None

    action = cmd.get("action")

    if action in ("light", "device", "relay", "on", "off"):
        raw_id = cmd.get("id", "all")
        state = int(cmd.get("state", 1 if action == "on" else 0))

        # Resolve device name to numeric ID if the LLM returned a string name
        device_id = str(raw_id)
        device_name = None
        if device_id != "all" and not device_id.isdigit():
            # Look up by name in the registry
            key = device_id.strip().lower()
            info = _device_registry.get(key)
            if not info:
                # Fuzzy fallback
                device = _resolve_device(device_id)
                if device:
                    info = {"id": device[0], "type": device[1], "name": device[2]}
            if info:
                device_id = str(info["id"])
                device_name = info["name"]
                device_type = info["type"]
                print(f"  LLM command resolved: '{raw_id}' -> id={device_id} ({device_name})")
                return _execute_device_command(int(device_id), device_name, device_type, state)
            else:
                print(f"  LLM command: unknown device '{raw_id}'")
                return f"Sorry, I don't know a device called {raw_id}."

        # Check if this is a brightness command
        brightness = cmd.get("brightness")
        if brightness is not None:
            percent = max(0, min(100, int(brightness)))
            return _execute_brightness_command(device_id, percent)
        # state > 1 means the LLM used state as a brightness percentage
        if state > 1:
            percent = max(0, min(100, state))
            return _execute_brightness_command(device_id, percent)
        return _execute_light_command(device_id, state)

    return None


# --- Audio functions using arecord/aplay ---

# Persistent arecord process for mic input
_arecord_proc = None


def _ensure_arecord():
    """Ensure the persistent arecord subprocess is running. Start it if not."""
    global _arecord_proc
    if _arecord_proc is not None and _arecord_proc.poll() is None:
        return _arecord_proc
    # Retry a few times — device may still be held by aplay
    for attempt in range(5):
        _arecord_proc = _start_arecord()
        time.sleep(0.2)
        if _arecord_proc.poll() is None:
            return _arecord_proc
        time.sleep(0.5)
    # Last attempt without checking
    _arecord_proc = _start_arecord()
    return _arecord_proc


def _kill_arecord():
    """Stop the persistent arecord subprocess."""
    global _arecord_proc
    _stop_arecord(_arecord_proc)
    _arecord_proc = None


def play_beep(wav_path=None):
    """Play a pre-generated beep WAV."""
    if wav_path is None:
        wav_path = BEEP_WAKE_WAV
    if not _play_wav(wav_path):
        print("  (beep failed)")


def listen_for_wake_word(ignore_seconds=0):
    """Block until wake word is detected.

    ignore_seconds: feed audio to the model but ignore detections for this
    many seconds (used to avoid speaker feedback re-triggering).
    """
    proc = _ensure_arecord()
    print(f"Listening for '{WAKE_MODEL}'...")
    ignore_chunks = int(ignore_seconds * SAMPLE_RATE / CHUNK)
    chunk_count = 0
    chunk_bytes = CHUNK * 2  # 16-bit = 2 bytes per sample
    while True:
        data = proc.stdout.read(chunk_bytes)
        if len(data) < chunk_bytes:
            # arecord died — restart
            print("  arecord ended unexpectedly, restarting...")
            _kill_arecord()
            time.sleep(0.5)
            proc = _ensure_arecord()
            continue
        samples = np.frombuffer(data, dtype=np.int16)
        result = wake_model.predict(samples)
        chunk_count += 1
        if chunk_count <= ignore_chunks:
            continue
        for name, score in result.items():
            if WAKE_MODEL in name and score > WAKE_THRESHOLD:
                print(f"  Wake word detected! ({name}: {score:.2f})")
                wake_model.reset()
                return


def _drain_audio_buffer():
    """Drain any stale audio sitting in the arecord pipe buffer.

    After playing a beep, up to ~2s of audio may have buffered while the
    sound was playing.  Reading it out (and discarding) ensures
    record_speech() only sees fresh audio captured after the beep.
    """
    proc = _ensure_arecord()
    import select as _sel
    chunk_bytes = CHUNK * 2
    drained = 0
    while _sel.select([proc.stdout], [], [], 0)[0]:
        data = proc.stdout.read(chunk_bytes)
        if not data:
            break
        drained += len(data)
    if drained:
        print(f"  Drained {drained} bytes of stale audio")


def record_speech():
    """Record until silence is detected. Returns raw PCM bytes.

    Waits for the user to start speaking (grace period) before arming
    silence detection, so we don't cut off on pre-speech silence.
    """
    proc = _ensure_arecord()
    print("  Recording your question...")
    frames = []
    silent_chunks = 0
    heard_speech = False
    max_silent = int(SILENCE_DURATION * SAMPLE_RATE / CHUNK)
    max_chunks = int(MAX_RECORD_SECONDS * SAMPLE_RATE / CHUNK)
    # Grace period: wait up to 3s for user to start speaking before giving up
    grace_chunks = int(3.0 * SAMPLE_RATE / CHUNK)
    chunk_bytes = CHUNK * 2

    for i in range(max_chunks):
        data = proc.stdout.read(chunk_bytes)
        if len(data) < chunk_bytes:
            break
        frames.append(data)
        samples = np.frombuffer(data, dtype=np.int16)
        amplitude = np.abs(samples).mean()
        if amplitude >= SILENCE_THRESHOLD:
            heard_speech = True
            silent_chunks = 0
        else:
            silent_chunks += 1

        if heard_speech:
            # User spoke and now went silent — done
            if silent_chunks > max_silent:
                break
        else:
            # Haven't heard speech yet — give up after grace period
            if i >= grace_chunks:
                print("  No speech detected during grace period")
                return b""

    return b"".join(frames)


def transcribe(pcm_bytes):
    """PCM bytes to text via faster-whisper."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
        with wave.open(f.name, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm_bytes)
        segments, _ = whisper_model.transcribe(f.name, beam_size=1)
        return " ".join(seg.text for seg in segments).strip()


def _clean_llm_response(text):
    """Strip meta-commentary that small LLMs append after the real answer."""
    # Cut at lines that look like model self-commentary rather than answer content
    meta_patterns = re.compile(
        r'^(This is a JSON|Here is|Note:|```|{'
        r'|\[{|"[a-z_]+"\s*:)',
        re.IGNORECASE
    )
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        if meta_patterns.match(line.strip()):
            break
        cleaned.append(line)
    return '\n'.join(cleaned).strip() or text.strip()


def ask_llm(prompt):
    """Send prompt to AI, return response text."""
    try:
        resp = requests.post(f"{OLLAMA_URL}/api/generate", json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "system": get_system_prompt(),
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "num_predict": 200,
                "num_thread": _CPU_THREADS,
            }
        }, timeout=60)
        resp.raise_for_status()
        raw = resp.json().get("response", "Sorry, I didn't get a response.")
        return _clean_llm_response(raw)
    except requests.exceptions.ConnectionError:
        return "Sorry, the language model is not running."
    except requests.exceptions.Timeout:
        return "Sorry, the language model took too long to respond."


def _strip_markdown(text):
    """Remove markdown formatting so TTS doesn't read punctuation aloud."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)   # **bold**
    text = re.sub(r'\*(.+?)\*', r'\1', text)        # *italic*
    text = re.sub(r'__(.+?)__', r'\1', text)        # __bold__
    text = re.sub(r'_(.+?)_', r'\1', text)          # _italic_
    text = re.sub(r'`(.+?)`', r'\1', text)          # `code`
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)  # headings
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)  # bullets
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)  # numbered lists
    return text.strip()


def speak(text):
    """Text to speech via Piper, played through speaker."""
    text = _strip_markdown(text)
    print(f"  Speaking: {text[:100]}{'...' if len(text) > 100 else ''}")
    try:
        piper_proc = subprocess.Popen(
            ["piper", "--model", PIPER_MODEL, "--output-raw"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        raw_audio, piper_err = piper_proc.communicate(input=text.encode("utf-8"))
        if piper_proc.returncode != 0:
            print(f"  Piper error (rc={piper_proc.returncode}): {piper_err.decode()[:200]}")
            return
        if not raw_audio:
            print("  Piper produced no audio output")
            return
        print(f"  Piper produced {len(raw_audio)} bytes of audio")
        _play_raw_audio(raw_audio, sample_rate=22050)
    except FileNotFoundError:
        print("  ERROR: 'piper' not found in PATH")
    except Exception as e:
        print(f"  TTS error: {e}")


# --- Pre-warm models ---
print("Pre-warming AI model...")
try:
    requests.post(f"{OLLAMA_URL}/api/generate", json={
        "model": OLLAMA_MODEL,
        "prompt": "hi",
        "system": "Reply with one word.",
        "stream": False,
        "keep_alive": "30m",
        "options": {"num_predict": 5, "num_thread": _CPU_THREADS}
    }, timeout=30)
    print("  AI model loaded and warm")
except Exception as e:
    print(f"  AI warmup failed (will load on first query): {e}")

print("Pre-warming Piper TTS...")
try:
    proc = subprocess.Popen(
        ["piper", "--model", PIPER_MODEL, "--output-raw"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    proc.communicate(input=b"ready", timeout=15)
    print("  Piper model loaded")
except Exception as e:
    print(f"  Piper warmup failed: {e}")

# --- Main loop ---
print("=== Voice Assistant Ready ===\n")

# Start persistent mic capture
_ensure_arecord()

ignore_after_speak = 0  # seconds to ignore wake detections after speaking

while True:
    try:
        listen_for_wake_word(ignore_seconds=ignore_after_speak)
        ignore_after_speak = 0  # reset for next iteration
        time.sleep(0.15)  # brief pause so wake beep doesn't feel instant
        play_beep(BEEP_WAKE_WAV)
        _drain_audio_buffer()  # discard audio that buffered during beep

        pcm = record_speech()
        play_beep(BEEP_DONE_WAV)

        user_text = transcribe(pcm)
        print(f"  You said: \"{user_text}\"")

        if not user_text or len(user_text.strip()) < 2:
            speak("I didn't catch that.")
            ignore_after_speak = 3
            continue

        # Try fast intent matching first
        response = match_intent(user_text)
        if response:
            speak(response)
        else:
            # Fall back to LLM for general questions
            print("  Asking LLM...")
            reply = ask_llm(user_text)
            print(f"  Reply: \"{reply[:100]}{'...' if len(reply) > 100 else ''}\"")
            confirmation = handle_command(reply)
            if confirmation:
                speak(confirmation)
            elif reply.strip().startswith("{"):
                # LLM returned unrecognized JSON — don't speak it
                print("  (LLM returned unrecognized JSON, ignoring)")
                speak("Sorry, I'm not sure how to help with that.")
            else:
                speak(reply)

        ignore_after_speak = 5  # ignore wake detections for 5s after speaking
        wake_model.reset()
        print()

    except KeyboardInterrupt:
        print("\nShutting down...")
        break
    except Exception as e:
        print(f"  Loop error: {e}")
        time.sleep(1)

_kill_arecord()
if mqtt:
    mqtt.loop_stop()
    mqtt.disconnect()
# Clean up temp beep files
try:
    shutil.rmtree(_beep_dir, ignore_errors=True)
except Exception:
    pass
print("Goodbye.")
