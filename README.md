# TrailCurrentPeregrine

Local voice assistant for the TrailCurrent platform, running on a Radxa Dragon Q6A (RK3588S, 8 GB RAM, Ubuntu Noble 24.04).

## Architecture

The assistant runs an entirely offline voice pipeline:

1. **Wake word** — openWakeWord (custom `hey_peregrine` model)
2. **Speech-to-text** — faster-whisper (`base.en`, INT8 on CPU)
3. **LLM** — Ollama with Qwen 2.5 0.5B (fits in ~1 GB RAM)
4. **Text-to-speech** — Piper TTS (`en_US-libritts_r-medium`)
5. **Device control** — MQTT integration with TrailCurrent (lights, sensors)

All processing happens on-device. No cloud services required.

## Project Structure

```
TrailCurrentPeregrine/
├── src/
│   └── assistant.py              # Main voice assistant loop
├── config/
│   ├── voice-assistant.service   # systemd unit file
│   └── pulse-default.pa          # PulseAudio config for assistant user
├── setup/
│   └── setup-assistant.sh        # One-shot board provisioning script
├── deploy.sh                     # Deploy to board via SSH
├── models/
│   ├── hey_peregrine.onnx        # Custom wake word model (graph)
│   └── hey_peregrine.onnx.data   # Custom wake word model (weights)
├── training/
│   ├── record_wake_word.py       # Record positive & negative clips for training
│   ├── generate_tts_variants.py  # Generate voice-cloned TTS training clips
│   ├── real_clips/               # Recorded wake word samples (positive)
│   ├── real_clips_negative/      # Recorded similar-phrase samples (negative)
│   └── openwakeword-trainer/     # Wake word training pipeline
└── README.md
```

## Quick Start

### 1. Provision the board

Copy the repo to the Radxa board and run the setup script as root:

```bash
cd setup/
chmod +x setup-assistant.sh
sudo ./setup-assistant.sh
```

This installs all dependencies, downloads models, creates the `assistant` user, deploys the custom wake word model, and registers the systemd service.

### 2. Verify audio

Switch to the assistant user and test hardware:

```bash
su - assistant

# List audio devices
aplay -l
arecord -l

# Speaker test
speaker-test -t wav -c 2 -l 1

# Record and playback
arecord -d 5 -f S16_LE -r 16000 /tmp/test.wav && aplay /tmp/test.wav
```

### 3. Configure MQTT (optional)

To enable device control, edit the systemd service with your MQTT broker details:

```bash
sudo systemctl edit voice-assistant
```

Add the following overrides:

```ini
[Service]
Environment=MQTT_BROKER=192.168.x.x
Environment=MQTT_PORT=8883
Environment=MQTT_USE_TLS=true
Environment=MQTT_CA_CERT=/home/assistant/ca.pem
Environment=MQTT_USERNAME=your_username
Environment=MQTT_PASSWORD=your_password
```

Or edit `/etc/systemd/system/voice-assistant.service` directly and reload:

```bash
sudo systemctl daemon-reload
sudo systemctl restart voice-assistant
```

Without MQTT configured, the assistant still works for general questions — device control commands will just report "not connected."

### 4. Run interactively

```bash
su - assistant
~/assistant-env/bin/python3 ~/assistant.py
```

Say "Hey Peregrine" and ask a question or give a command.

### 5. Enable as a service

```bash
sudo systemctl start voice-assistant
sudo journalctl -u voice-assistant -f
```

## Voice Commands

With MQTT connected, the assistant responds to:

| Command | Example phrases |
|---|---|
| **Lights on/off** | "Turn on the lights", "Lights off", "Turn off light 3" |
| **Light status** | "Are the lights on?", "Which lights are on?" |
| **Temperature** | "What's the temperature?", "How hot is it?" |
| **Humidity** | "What's the humidity?" |
| **Battery/energy** | "What's the battery level?", "How much solar?" |
| **Location** | "Where are we?", "What's our location?" |
| **General questions** | Anything else is answered by the LLM |

Light commands and sensor queries use fast regex-based intent matching (no LLM round-trip). Unrecognized requests fall through to the LLM.

## Configuration

All settings are controlled via environment variables (set in the systemd unit or shell):

| Variable | Default | Description |
|---|---|---|
| `WAKE_MODEL` | `hey_peregrine` | openWakeWord model name |
| `WAKE_MODEL_PATH` | *(auto-detected)* | Path to custom `.onnx` wake word model |
| `WAKE_THRESHOLD` | `0.5` | Wake word detection threshold (0.0–1.0) |
| `WHISPER_SIZE` | `base.en` | Whisper model size |
| `OLLAMA_MODEL` | `qwen2.5:0.5b` | Ollama model tag |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API endpoint |
| `PIPER_MODEL` | `~/piper-voices/en_US-libritts_r-medium.onnx` | Path to Piper voice model |
| `SILENCE_THRESHOLD` | `500` | Amplitude below which audio is silence |
| `SILENCE_DURATION` | `1.5` | Seconds of silence before stopping recording |
| `MQTT_BROKER` | *(empty = disabled)* | MQTT broker hostname or IP |
| `MQTT_PORT` | `8883` | MQTT broker port |
| `MQTT_USE_TLS` | `true` | Enable TLS for MQTT connection |
| `MQTT_CA_CERT` | *(empty)* | Path to CA certificate for self-signed certs |
| `MQTT_USERNAME` | *(empty)* | MQTT authentication username |
| `MQTT_PASSWORD` | *(empty)* | MQTT authentication password |

## MQTT Topics

The assistant subscribes to these topics for sensor data:

| Topic | Data |
|---|---|
| `local/airquality/temphumid` | `{"tempInC": °C, "tempInF": °F, "humidity": %}` |
| `local/airquality/status` | `{"tvoc_ppb": ppb, "eco2_ppm": ppm}` |
| `local/energy/status` | `{"battery_percent": %, "battery_voltage": V, "solar_watts": W}` |
| `local/gps/latlon` | `{"latitude": float, "longitude": float}` |
| `local/gps/time` | `{"year": int, "month": int, "day": int, "hour": int, "minute": int, "second": int}` (UTC) |
| `local/gps/alt` | `{"altitudeInMeters": float, "altitudeFeet": int}` |
| `local/lights/+/status` | `{"state": 0/1, "name": "..."}` |
| `local/thermostat/status` | Thermostat state |

For light commands, it publishes:

| Topic | Payload |
|---|---|
| `local/lights/{id}/command` | `{"state": 0/1}` |
| `can/outbound` | CAN frame for "all lights" broadcast |

## Hardware

- **Board**: Radxa Dragon Q6A (RK3588S SoC, 8 GB RAM)
- **Audio**: USB microphone + speaker (auto-detected via ALSA, uses arecord/aplay)
- **Storage**: eMMC or SD card with Ubuntu Noble 24.04

## Wake Word Training

The custom "Hey Peregrine" wake word model is trained using the pipeline in `training/openwakeword-trainer/`. See that directory's README for full instructions.

To record additional real voice clips for improving the model:

```bash
cd training/

# Positive clips (the wake word)
python3 record_wake_word.py --phrase "hey peregrine" --count 50

# See suggested negative phrases to reduce false positives
python3 record_wake_word.py --suggest

# Record negative clips (similar-sounding phrases the model should reject)
python3 record_wake_word.py --phrase "hey pelican" --count 10 --negative --auto
```

Real voice clips significantly improve detection accuracy over synthetic-only training. Aim for 200+ positive clips and 50-100 negative clips from similar-sounding phrases.

## Deployment

Use the deploy script to push code, models, and the service file to the board:

```bash
./deploy.sh <board-ip>
# or with explicit user
./deploy.sh assistant@192.168.1.100
```

This copies `src/assistant.py`, the wake word model, and the systemd service file, then reloads systemd. It also creates a default `~/assistant.env` on the board if one doesn't exist.

After deploying, restart the service on the board:

```bash
sudo systemctl restart voice-assistant
sudo journalctl -u voice-assistant -f
```

For wake word model updates only:

```bash
scp models/hey_peregrine.onnx* assistant@<board-ip>:~/models/
sudo systemctl restart voice-assistant
```

## Third-Party Licenses

### Runtime Components

| Component | License |
|---|---|
| [openWakeWord](https://github.com/dscripka/openWakeWord) | Apache 2.0 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT |
| [Ollama](https://github.com/ollama/ollama) | MIT |
| [Qwen 2.5 0.5B](https://huggingface.co/Qwen/Qwen2.5-0.5B) | Apache 2.0 |
| [Piper TTS](https://github.com/rhasspy/piper) | MIT |
| [Piper voice: en_US-libritts_r-medium](https://huggingface.co/rhasspy/piper-voices) | CC-BY-4.0 |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | MIT |
| [paho-mqtt](https://github.com/eclipse/paho.mqtt.python) | EPL-2.0 / EDL-1.0 |

### Wake Word Training Data

| Dataset | License |
|---|---|
| [LibriSpeech](https://www.openslr.org/12) | CC-BY-4.0 |
| [VoxPopuli](https://github.com/facebookresearch/voxpopuli) | CC0 |
| [MS-SNSD](https://github.com/microsoft/MS-SNSD) | MIT |
| [MUSAN](https://www.openslr.org/17/) | CC0 / CC-BY-3.0 |

The Piper `libritts_r` voice model is derived from [LibriTTS-R](https://www.openslr.org/141/) (CC-BY-4.0). Attribution: Koizumi et al., "LibriTTS-R: A Restored Multi-Speaker Text-to-Speech Corpus", 2023.
