# TrailCurrent Peregrine

Local voice assistant for the TrailCurrent platform, running on a Radxa
Dragon Q6A (Qualcomm QCS6490, 8 GB RAM). Built as a flashable Ubuntu
Noble 24.04 image — fresh board to working assistant in ~85 minutes.

<p align="center">
  <img src="CAD/peregrine_case.png" alt="TrailCurrent Peregrine case" width="480">
</p>

## Architecture

Entirely offline voice pipeline:

1. **Wake word** — openWakeWord (custom `hey_peregrine` model)
2. **Speech-to-text** — faster-whisper (`base.en`, INT8 on CPU)
3. **LLM** — Llama 3.2 1B on Hexagon NPU via `genie-t2t-run` (~12 tok/s)
4. **Text-to-speech** — Piper TTS (`en_US-libritts_r-medium`)
5. **Device control** — MQTT integration with TrailCurrent (lights, relays, sensors)

All processing happens on-device. No cloud services required for the core loop.

## Quick start — fresh board to working assistant

```bash
# 1. Build host setup (one time)
sudo apt install -y jsonnet bdebstrap libguestfs-tools \
    qemu-user-static binfmt-support device-tree-compiler \
    gdisk parted git curl gpg pipx rsync unzip
./image_build/preflight.sh --download-cache

# 2. Build the image (~30-50 min)
sudo ./image_build/build.sh

# 3. Put board in EDL mode and flash
sudo ./image_build/flash.sh --firmware     # one-time per board
sudo ./image_build/flash.sh --os image_build/output/peregrine-q6a-v1.0.img

# 4. Boot the board (Ethernet + 12V), wait ~3 minutes
ssh trailcurrent@peregrine.local           # password: trailcurrent
# First-login wizard runs automatically and forces a password change.

# 5. Say "hey peregrine"
```

Full walkthrough → [image_build/README.md](image_build/README.md) and the seven docs in [image_build/docs/](image_build/docs/).

## Project structure

```
TrailCurrentPeregrine/
├── README.md                       This file
├── CLAUDE.md                       Project instructions
├── LICENSE
│
├── src/
│   ├── assistant.py                Main voice assistant loop
│   └── genie_server.py             NPU LLM HTTP server
│
├── config/
│   ├── voice-assistant.service     systemd unit (canonical, baked into image)
│   └── genie-server.service        systemd unit (canonical, baked into image)
│
├── models/
│   ├── hey_peregrine.onnx          Custom wake word model
│   └── hey_peregrine.onnx.data
│
├── docs/
│   └── future-vision-modes.md      Vision modes roadmap
│
├── deploy.sh                       Dev tool — push src/ to a running board
│
├── image_build/                    ★ Image build pipeline ★
│   ├── README.md                   Quick start
│   ├── docs/                       7 detailed walkthroughs
│   ├── build.sh                    Top-level build orchestrator
│   ├── preflight.sh                Build host setup verification
│   ├── flash.sh                    edl-ng wrapper for SPI NOR + NVMe flashing
│   ├── rsdk/                       Vendored Radxa SDK with Peregrine hooks
│   ├── firmware/                   SPI NOR firmware (committed)
│   ├── files/                      Static files baked into the image
│   ├── cache/                      NPU model + Piper voice (gitignored)
│   └── output/                     Built images (gitignored)
│
├── training/                       Wake-word training pipeline (large, mostly gitignored)
│   ├── *.py                        Recording / generation scripts
│   ├── recordings/                 Stray clips (gitignored)
│   └── openwakeword-trainer/       Training pipeline + configs
│
├── CAD/                            Enclosure
└── EDA/                            HAT PCB
```

## Two install paths

| Path | When | Time |
|---|---|---|
| **Image build + flash** ([image_build/](image_build/)) | New board, OS package change, model update, dependency change | ~50 min build + ~10 min flash |
| **`deploy.sh`** | Iterating on `src/` on an already-flashed board | ~5 sec |

The image build is the **canonical install path**. `deploy.sh` is a development
tool — its changes are lost on the next reflash. See
[image_build/docs/07-development.md](image_build/docs/07-development.md).

## Verifying audio on a running board

```bash
ssh trailcurrent@peregrine.local
peregrine-self-test                # full audio + NPU + LLM + wake word check

# Or by hand
aplay -l
arecord -l
speaker-test -t wav -c 2 -l 1
arecord -d 5 -f S16_LE -r 16000 /tmp/test.wav && aplay /tmp/test.wav
```

## Configuring MQTT

The first-login wizard offers MQTT setup. To change it later:

```bash
ssh trailcurrent@peregrine.local
nano ~/assistant.env
sudo systemctl restart voice-assistant
```

Without MQTT configured, the assistant still answers general questions —
device-control commands will just report "not connected."

## Voice Commands

With MQTT connected, the assistant responds to:

| Command | Example phrases |
|---|---|
| **Lights on/off** | "Turn on the lights", "Lights off", "Turn off light 3" |
| **Light brightness** | "Set lights to 50 percent", "Dim light 2 to 25 percent" |
| **Light status** | "Are the lights on?", "Which lights are on?" |
| **Named devices** | "Turn on the water pump", "Turn off the awning light" |
| **Relay status** | "What relays are on?", "Switch status" |
| **Everything** | "Turn off everything", "Turn on all the lights" |
| **Temperature** | "What's the temperature?", "How hot is it?" |
| **Humidity** | "What's the humidity?" |
| **Battery/energy** | "What's the battery level?", "How much solar?" |
| **Location** | "Where are we?", "What's our location?" |
| **General questions** | Anything else is answered by the LLM |

Device commands and sensor queries use fast regex-based intent matching (no LLM round-trip). Unrecognized requests fall through to the LLM.

Device names are configured in the TrailCurrent web UI and synced via MQTT. Both Torrent (PWM lights) and Switchback (relays) devices are supported. Only Torrent lights support brightness — Switchback relays are on/off only. When you say "turn off all the lights", both Torrent lights and any Switchback relays configured as type "light" are turned off.

## Configuration

All settings are controlled via environment variables. On the board, site-specific config lives in `~/assistant.env` (loaded by the systemd service). This file is never overwritten by setup or deploy.

| Variable | Default | Description |
|---|---|---|
| `WAKE_MODEL` | `hey_peregrine` | openWakeWord model name |
| `WAKE_MODEL_PATH` | *(auto-detected)* | Path to custom `.onnx` wake word model |
| `WAKE_THRESHOLD` | `0.85` | Wake word detection threshold (0.0–1.0) |
| `WAKE_ACTIVATIONS` | `3` | Consecutive frames above threshold required to trigger |
| `WHISPER_SIZE` | `base.en` | Whisper model size |
| `OLLAMA_URL` | `http://localhost:11434` | LLM API endpoint (Genie NPU server) |
| `OLLAMA_MODEL` | `llama3.2:1b-npu` | Model identifier for LLM requests |
| `PIPER_MODEL` | `~/piper-voices/en_US-libritts_r-medium.onnx` | Path to Piper voice model |
| `SILENCE_THRESHOLD` | `500` | Amplitude below which audio is silence |
| `SILENCE_DURATION` | `1.5` | Seconds of silence before stopping recording |
| `CPU_THREADS` | `8` | Threads for faster-whisper inference |
| `MQTT_BROKER` | *(empty = disabled)* | MQTT broker hostname or IP |
| `MQTT_PORT` | `8883` | MQTT broker port |
| `MQTT_USE_TLS` | `true` | Enable TLS for MQTT connection |
| `MQTT_CA_CERT` | *(empty)* | Path to CA certificate for self-signed certs |
| `MQTT_USERNAME` | *(empty)* | MQTT authentication username |
| `MQTT_PASSWORD` | *(empty)* | MQTT authentication password |

## MQTT Topics

The assistant subscribes to these topics for sensor data and device config:

| Topic | Data |
|---|---|
| `local/airquality/temphumid` | `{"tempInC": °C, "tempInF": °F, "humidity": %}` |
| `local/airquality/status` | `{"tvoc_ppb": ppb, "eco2_ppm": ppm}` |
| `local/energy/status` | `{"battery_percent": %, "battery_voltage": V, "solar_watts": W}` |
| `local/gps/latlon` | `{"latitude": float, "longitude": float}` |
| `local/gps/time` | `{"year": int, "month": int, "day": int, "hour": int, "minute": int, "second": int}` (UTC) |
| `local/gps/alt` | `{"altitudeInMeters": float, "altitudeFeet": int}` |
| `local/lights/+/status` | `{"state": 0/1, "name": "..."}` (Torrent lights) |
| `local/relays/+/status` | `{"state": 0/1}` (Switchback relays) |
| `local/thermostat/status` | Thermostat state |
| `local/config/pdm_channels` | Torrent channel names/types (retained) |
| `local/config/relay_channels` | Switchback channel names/types (retained) |

For device commands, it publishes:

| Topic | Payload |
|---|---|
| `local/lights/{id}/command` | `{"state": 0/1}` (Torrent lights) |
| `local/lights/{id}/brightness` | `{"brightness": 0-255}` (Torrent lights only) |
| `local/relays/{channel}/command` | `{"state": 0/1}` (Switchback individual relay toggle) |
| `local/relays/all/command` | `{"state": 0/1}` (Switchback all relays on/off) |

## Hardware

- **Board**: Radxa Dragon Q6A (Qualcomm QCS6490 SoC, 8 GB RAM)
- **NPU**: Hexagon DSP v68 (12 TOPS) — runs LLM inference at ~12 tok/s
- **Audio**: USB microphone + speaker (auto-detected via ALSA, uses arecord/aplay)
- **Storage**: M.2 2230 NVMe SSD with Radxa OS Noble 24.04 (Ubuntu-based)

## Wake Word Training

The custom "Hey Peregrine" wake word model is trained using the pipeline in `training/openwakeword-trainer/`. See that directory's [README](training/openwakeword-trainer/README.md) for the full 13-step pipeline and configuration reference.

Training runs on a dev workstation with an NVIDIA GPU (not on the target board).

### Recording Real Voice Clips

Real voice clips significantly improve detection accuracy over synthetic-only training:

```bash
cd training/

# Positive clips (the wake word)
python3 record_wake_word.py --phrase "hey peregrine" --count 50

# See suggested negative phrases to reduce false positives
python3 record_wake_word.py --suggest

# Record negative clips (similar-sounding phrases the model should reject)
python3 record_wake_word.py --phrase "hey pelican" --count 10 --negative --auto
```

Aim for 200+ positive clips and 50-100 negative clips from similar-sounding phrases.

### Ambient Noise Negatives

The model also needs non-speech negatives (silence, fan noise, road noise) to avoid false triggers on ambient sounds. There are two sources: synthetic/downloaded clips, and real recordings from a microphone.

#### Synthetic + downloaded clips

```bash
cd training/

# Generate synthetic noise clips + download MS-SNSD (MIT) and MUSAN (CC0) recordings
python3 generate_ambient_negatives.py
```

This populates `real_clips_negative/` with ambient noise clips across categories (fan noise, road noise, rain, HVAC, silence, etc.).

#### Recording real ambient noise from a microphone

For best results, also capture real ambient audio from the environment where the device will be used (vehicle interior, campsite, etc.). The `record_ambient_negatives.py` script records 2-second clips in a continuous loop for a set duration.

1. Find your microphone's ALSA device:
   ```bash
   arecord -l
   ```
   Look for your USB mic (e.g. `card 2: USB [Jabra SPEAK 410 USB], device 0` → `hw:2,0`).

2. Run a recording session (from the `training/` directory):
   ```bash
   python3 record_ambient_negatives.py --device hw:2,0 --minutes 30
   ```
   This records ~720 clips over 30 minutes, saving them to `training/real_clips_negative/ambient_recorded/`. Clips that are too quiet are automatically discarded. Press Ctrl+C to stop early.

   Additional options: `--pause 1.0` (gap between clips, default 0.5s), `--min-rms 100` (minimum loudness to keep, default 50).

3. Run multiple sessions to capture different environments — clips are appended, never overwritten.

#### Build the ambient feature file

After adding clips from either source, rebuild the feature file used by training:

```bash
python3 build_ambient_features.py
```

This produces `negative_features_ambient.npy` used during training.

### Retraining the Model

After recording new clips and rebuilding features, retrain the wake word model.

**1. Prepare (from the `training/` directory):**

```bash
cd openwakeword-trainer
pkill -f ComfyUI
source /home/dave/.oww-trainer-venv/bin/activate
rm -f output/hey_peregrine.onnx output/hey_peregrine.onnx.data
rm -f export/hey_peregrine.onnx export/hey_peregrine.onnx.data
```

- ComfyUI must be stopped — it holds ~4 GB of GPU VRAM and training will OOM.
- Old model files must be deleted — the pipeline skips training if they exist.

**2. Run training (~16 minutes on an NVIDIA GPU):**

```bash
python train_wakeword.py --config configs/hey_peregrine.yaml --from train
```

The `onnx_tf` error at the end is harmless — the ONNX model exports successfully, it just can't convert to TFLite (which isn't used).

**3. After training completes, copy the model to the project `models/` directory:**

```bash
cp output/hey_peregrine.onnx ../../models/
cp output/hey_peregrine.onnx.data ../../models/
```

### Deploying a New Model

After training, copy the model to the board:

```bash
# Full deploy (code + model + service) — see image_build/docs/07-development.md
./deploy.sh peregrine.local

# Or model-only update
scp models/hey_peregrine.onnx* trailcurrent@peregrine.local:/home/trailcurrent/models/
ssh trailcurrent@peregrine.local "sudo systemctl restart voice-assistant"
```

For a permanent install (so the new model survives a reflash), bake it into
the next image build by leaving the new files in `models/` and re-running
`sudo ./image_build/build.sh`.

## Third-Party Licenses

### Runtime Components

| Component | License |
|---|---|
| [openWakeWord](https://github.com/dscripka/openWakeWord) | Apache 2.0 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT |
| [Llama 3.2 1B](https://huggingface.co/meta-llama/Llama-3.2-1B) | Llama 3.2 Community |
| Qualcomm Genie (genie-t2t-run) | Qualcomm proprietary (bundled with NPU model) |
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
