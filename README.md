# TrailCurrent Peregrine

Local voice assistant for the TrailCurrent platform. Wake word, speech
recognition, the language model and speech synthesis all run on the board,
with no cloud service in the loop. Peregrine is built as a ready-to-install
Ubuntu 24.04 image for either of two boards.

<p align="center">
  <img src="CAD/peregrine_case.png" alt="TrailCurrent Peregrine case" width="480">
</p>

## Choose your board

Peregrine supports two boards. Both run the same assistant code, answer to
the same wake word, control the same TrailCurrent devices and show up on
your network as `peregrine.local`. What differs is how smart the language
model is, how much power the board draws, and what it costs.

|  | **Radxa Dragon Q6A** (budget) | **ZimaBoard 2** (premium) |
|---|---|---|
| **Pick it when** | You want the lowest cost, or you run a really small battery bank | You want the smartest answers and are willing to pay for them |
| **Price** | Lower | Slightly higher |
| **Power draw** | Lower. A phone-class Arm chip, with the model on a dedicated low-power AI accelerator (NPU) | Higher. An Intel x86 mini-PC chip, with the model on the processor cores |
| **Language model** | Llama 3.2 **1B** | Llama 3.2 **3B**, three times the size |
| **Answer speed** | About 12 tokens/s | About 10 tokens/s (measured 9.3–10.2) |
| **Conversation memory** | 1,024 tokens (about 750 words) | 8,192 tokens (about 6,000 words) |
| **Processor / RAM** | Qualcomm QCS6490 (8 Arm cores) + Hexagon NPU, 8 GB | Intel N150 (4 x86 cores), 8 GB |
| **Storage** | M.2 2230 NVMe SSD, bought separately | 32 GB eMMC built in |
| **Installing** | Build an image, flash it over USB-C in EDL mode (or put the NVMe in a USB enclosure) | Build an installer ISO, boot the board from a USB stick |
| **Updating code on a live board** | `deploy.sh` pushes `src/` in seconds | Rebuild and reinstall the image |

### Radxa Dragon Q6A: the budget option

- **Costs the least.** The cheapest way to put Peregrine in a rig.
- **Draws the least power.** The language model runs on Qualcomm's Hexagon
  NPU, an accelerator built to do this work at low power. If you have a
  really small battery bank, this is the board to choose.
- **Quick to iterate on.** `deploy.sh` pushes code changes to a running board
  in a few seconds.
- **The trade-off is intelligence.** A 1B model is fine for small talk and
  simple questions, but it is weak at reasoning, at following instructions
  and at turning loosely worded requests into device commands. Its short
  memory also means Peregrine has to trim your device list out of its
  instructions.

### ZimaBoard 2: the premium option

Buy a ZimaBoard 2: **[ZimaBoard 2 at the Zimaspace shop](https://shop.zimaspace.com/products/zimaboard2-single-board-server?sca_ref=12564611.fDJvFR9EWxuoB4a&utm_source=dave-floyd&utm_medium=affiliate&utm_campaign=zimaspace_affiliate&utm_content=Dave-Floyd&sca_source=GitHub)**
(get the 8 GB model)

> **Affiliate link.** TrailCurrent earns a small percentage if you buy
> through this link. It costs you nothing extra.

- **Noticeably smarter.** The 3B model scores far higher than the 1B on
  Meta's published tests:

  | What it measures | Q6A (1B) | ZimaBoard 2 (3B) |
  |---|---:|---:|
  | General knowledge (MMLU) | 49% | **63%** |
  | Maths word problems (GSM8K) | 44% | **78%** |
  | Doing what it is told (IFEval) | 60% | **77%** |
  | Turning requests into device commands (BFCL v2) | 26% | **67%** |

- **Eight times the conversation memory.** Peregrine can give the model your
  whole device list, and web chats run far longer before they are trimmed.
- **Easier to install.** Write a USB stick, boot from it, type `ERASE`. There
  is no EDL mode and no separate SSD to buy.
- **The trade-offs.** It costs a little more and draws more power, so size
  your battery with that in mind. Code changes ship as a rebuilt image rather
  than through `deploy.sh`. Like any small model, it still guesses at trivia
  such as distances and dates.

The full comparison, including how the port works and what was measured, is
in [zimaboard2/docs/port-summary.md](zimaboard2/docs/port-summary.md).

**On both boards,** everyday commands such as "turn on the lights" or "what
are my water levels?" are caught by built-in phrases and never reach the
language model. The model only answers everything else.

## Architecture

Entirely offline voice pipeline:

1. **Wake word** — openWakeWord (custom `hey_peregrine` model)
2. **Speech-to-text** — faster-whisper (`base.en`, INT8 on CPU)
3. **LLM** — board-specific, behind the same loopback API on port 11434:
   - **Radxa Dragon Q6A:** Llama 3.2 1B on the Hexagon NPU via Qualcomm
     Genie (`genie_server.py`, ~12 tok/s)
   - **ZimaBoard 2:** Llama 3.2 3B in llama.cpp's `llama-server` on the CPU,
     behind a Genie-compatible bridge (`zimaboard2/src/llm_bridge.py`,
     ~10 tok/s)
4. **Text-to-speech** — Piper TTS (`en_US-libritts_r-medium`)
5. **Device control** — MQTT integration with TrailCurrent (lights, relays, sensors)
6. **Web chat UI** — `https://peregrine.local/` exposes the same on-device LLM
   to any browser on the LAN. TLS terminates inside `web_chat.py` using a
   per-board self-signed certificate; port 80 redirects to 443 and also
   serves `/ca.pem` unencrypted so first-time clients can install the CA
   before completing a TLS handshake. The chat backend proxies to the LLM
   server on loopback.
7. **Voice terminal endpoint** — `POST http://peregrine.local:8081/api/voice`
   accepts a WAV upload, runs STT → intent-or-LLM → TTS on-board, and returns
   a WAV of the spoken reply. Lets a dumb remote push-to-talk device (e.g.
   the ESP32-P4) act as a mic/speaker for Peregrine
   without needing a wake word or a separate ML pipeline. Bearer-token
   gated, LAN-only, disabled by default — see
   [docs/voice-terminal.md](docs/voice-terminal.md) for setup.

All processing happens on-device. No cloud services required for the core loop.

The assistant code in `src/` is shared. Everything board-specific lives in
the board's own layer: `image_build/` and `config/` for the Q6A,
`zimaboard2/` for the ZimaBoard 2.

## Quick start: Radxa Dragon Q6A

You need an x86_64 Linux build machine, a Radxa Dragon Q6A, an M.2 2230 NVMe
SSD, a 12 V supply, an Ethernet connection and a USB speakerphone (or a USB
mic and speaker).

```bash
# 1. Get the code
git clone https://github.com/trailcurrentoss/TrailCurrentPeregrine.git
cd TrailCurrentPeregrine

# 2. Build host setup (one time)
sudo apt install -y jsonnet bdebstrap libguestfs-tools \
    qemu-user-static binfmt-support device-tree-compiler \
    gdisk parted git curl gpg pipx rsync unzip
./image_build/preflight.sh --download-cache

# 3. Build the image (~30-50 min)
sudo ./image_build/build.sh

# 4. Put board in EDL mode and flash
sudo ./image_build/flash.sh --firmware     # one-time per board
sudo ./image_build/flash.sh --os image_build/output/peregrine-q6a-v1.0.img

# 5. Boot the board (Ethernet + 12V), wait ~3 minutes
ssh trailcurrent@peregrine.local           # password: trailcurrent
# First-login wizard runs automatically and forces a password change.

# 6. Say "hey peregrine"
```

Full walkthrough → [image_build/README.md](image_build/README.md) and the eight docs in [image_build/docs/](image_build/docs/).

## Quick start: ZimaBoard 2

You need an x86_64 Linux build machine with Docker, a
[ZimaBoard 2 (8 GB)](https://shop.zimaspace.com/products/zimaboard2-single-board-server?sca_ref=12564611.fDJvFR9EWxuoB4a&utm_source=dave-floyd&utm_medium=affiliate&utm_campaign=zimaspace_affiliate&utm_content=Dave-Floyd&sca_source=GitHub)
(affiliate link: we earn a small percentage, it costs you nothing extra),
a USB stick of 8 GB or more, a monitor and keyboard for the install, an
Ethernet connection and a USB speakerphone.

```bash
# 1. Get the code
git clone https://github.com/trailcurrentoss/TrailCurrentPeregrine.git
cd TrailCurrentPeregrine

# 2. Build the installer ISO (runs in Docker; your user must be in the docker group)
./zimaboard2/image/build.sh --version 1.0

# 3. Write it to the USB stick (the whole device, e.g. /dev/sdX, not /dev/sdX1)
sudo dd if=zimaboard2/image/output/peregrine-zimaboard2-1.0.iso \
    of=/dev/sdX bs=4M conv=fsync status=progress

# 4. In the board's firmware setup: boot from USB, and set
#    "Restore on AC power loss" to Power on

# 5. Boot from the stick, accept the eMMC, type ERASE, remove the stick,
#    press Enter. The board reboots into Peregrine.

# 6. Connect Ethernet and the speakerphone
ssh trailcurrent@peregrine.local           # password: trailcurrent
# First-login wizard runs automatically and forces a password change.

# 7. Say "hey peregrine"
```

Full walkthrough, including a network install with no USB stick →
[zimaboard2/docs/install.md](zimaboard2/docs/install.md).

## Releases

Two release tracks:
- **Image release** — a full reinstall, used when the OS, Python packages, LLM, or branding change. Q6A: [image_build/docs/08-cutting-a-release.md](image_build/docs/08-cutting-a-release.md). ZimaBoard 2: build a new ISO with `./zimaboard2/image/build.sh --version X.Y`.
- **Software release** ([docs/software-releases.md](docs/software-releases.md)) — `src/`, models, and service files only, pushed to live Q6A boards via `deploy.sh`. ZimaBoard 2 boards take the same changes in their next image.

## Project structure

```
TrailCurrentPeregrine/
├── README.md                       This file
├── LICENSE
│
├── src/                            ★ Shared by both boards ★
│   ├── assistant.py                Main voice assistant loop
│   ├── genie_server.py             NPU LLM HTTP server (loopback, Q6A only)
│   ├── stt.py                      Reusable faster-whisper wrapper (used by web_chat.py)
│   ├── tts.py                      Piper TTS streaming helper
│   └── web_chat.py                 LAN-facing chat UI (443) + voice terminal endpoint (8081)
│
├── models/
│   ├── hey_peregrine.onnx          Custom wake word model (both boards)
│   └── hey_peregrine.onnx.data
│
├── scripts/
│   └── generate-certs.sh           Per-board web UI certificate (both boards)
│
├── docs/
│   ├── ZimaBoard2/README.md        ZimaBoard 2 port design
│   ├── mqtt-ca-cert.md             MQTT CA certificate install and rotation
│   ├── software-releases.md        Software-only releases (Q6A)
│   ├── voice-terminal.md           Push-to-talk /api/voice endpoint
│   └── future-vision-modes.md      Vision modes roadmap
│
├── deploy.sh                       Dev tool — push src/ to a running Q6A
│
├── config/                         Q6A systemd units (baked into the image)
│   ├── voice-assistant.service
│   ├── genie-server.service
│   └── peregrine-chat.service
│
├── image_build/                    ★ Radxa Dragon Q6A image build ★
│   ├── README.md                   Quick start
│   ├── docs/                       8 detailed walkthroughs
│   ├── build.sh                    Top-level build orchestrator
│   ├── preflight.sh                Build host setup verification
│   ├── flash.sh                    edl-ng wrapper for SPI NOR + NVMe flashing
│   ├── rsdk/                       Vendored Radxa SDK with Peregrine hooks
│   ├── firmware/                   SPI NOR firmware (committed)
│   ├── files/                      Static files baked into the image
│   ├── cache/                      NPU model + Piper voice (gitignored)
│   └── output/                     Built images (gitignored)
│
├── zimaboard2/                     ★ ZimaBoard 2 image build ★
│   ├── README.md                   Overview and layout
│   ├── docs/                       Install guide, benchmarks, port summary
│   ├── src/llm_bridge.py           Genie-compatible API in front of llama-server
│   ├── config/                     ZimaBoard systemd units, llm.env, ALSA, netplan
│   ├── tools/peregrine-model       List, switch, import and test LLMs
│   └── image/                      Installer ISO build (Docker), network installer, VM test
│
├── training/                       Wake-word training pipeline (large, mostly gitignored)
│   ├── *.py                        Recording / generation scripts
│   ├── recordings/                 Stray clips (gitignored)
│   └── openwakeword-trainer/       Training pipeline + configs
│
├── CAD/                            Enclosure
└── EDA/                            HAT PCB
```

## Install and update paths

| Board | Path | When | Time |
|---|---|---|---|
| Q6A | **Image build + flash** ([image_build/](image_build/)) | New board, OS package change, model update, dependency change | ~50 min build + ~10 min flash |
| Q6A | **`deploy.sh`** | Iterating on `src/` on an already-flashed board | ~5 sec |
| ZimaBoard 2 | **ISO build + USB install** ([zimaboard2/docs/install.md](zimaboard2/docs/install.md)) | New board, and any change at all | Build, then a few minutes to install |
| ZimaBoard 2 | **Network install** (`zimaboard2/image/netinstall.sh`) | Replacing the OS on a board you can SSH into | As above, no USB stick |

The image build is the **canonical install path** on both boards. `deploy.sh`
is a Q6A development tool, and its changes are lost on the next reflash (see
[image_build/docs/07-development.md](image_build/docs/07-development.md)). It
checks the board first and refuses to push to a ZimaBoard 2, whose units
differ from the Q6A's.

## Verifying audio on a running board

```bash
ssh trailcurrent@peregrine.local
peregrine-self-test                # audio, LLM, wake word (plus the NPU on a Q6A)

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

# Monitor live logs
sudo journalctl -u voice-assistant -f
```

Without MQTT configured, the assistant still answers general questions —
device-control commands will just report "not connected."

For TLS to a self-signed Headwaters broker, the CA cert is installed at
`/home/trailcurrent/ca.pem` and obtained from the **Headwaters Settings
page** (https://headwaters.local → Settings → CA Certificate panel). For
the install/rotation procedure see [docs/mqtt-ca-cert.md](docs/mqtt-ca-cert.md).

## Time sync

The clock is set on every boot by `systemd-timesyncd` (the Q6A has no RTC
battery, so it depends on this). Both images ship with
`/etc/systemd/timesyncd.conf.d/10-trailcurrent.conf` pointing at
`headwaters.local` (resolved via mDNS through libnss-mdns + avahi). If
Headwaters isn't reachable — bench/dev networks, vehicle off the LAN —
timesyncd falls back to `pool.ntp.org`. Check sync status with
`timedatectl status`.

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
| **Water tanks** | "What are my water levels?", "How much fresh water do I have?", "How full is the grey tank?", "Black tank status" |
| **Temperature** | "What's the temperature?", "How hot is it?" |
| **Humidity** | "What's the humidity?" |
| **Battery/energy** | "What's the battery level?", "How much solar?" |
| **Location** | "Where are we?", "What's our location?" |
| **General questions** | Anything else is answered by the LLM |

Device commands and sensor queries use fast regex-based intent matching (no LLM round-trip). Unrecognized requests fall through to the LLM.

The same intent + LLM path is exposed over HTTP at `POST /api/voice` on port 8081, letting a push-to-talk remote (e.g. the ESP32-P4) act as a mic/speaker for Peregrine without shipping any ML on the client side. See [docs/voice-terminal.md](docs/voice-terminal.md) to enable and test.

Device names are configured in the TrailCurrent web UI and synced via MQTT. Both Torrent (PWM lights) and Switchback (relays) devices are supported. Only Torrent lights support brightness — Switchback relays are on/off only. When you say "turn off all the lights", both Torrent lights and any Switchback relays configured as type "light" are turned off.

## Configuration

All settings are controlled via environment variables. On the board, site-specific config lives in `~/assistant.env` (loaded by the systemd service). This file is never overwritten by setup or deploy. A reinstall does replace it, so copy it off first.

The defaults below are the code's. Each board's `voice-assistant.service` overrides a few of them for its hardware; the ZimaBoard 2 column shows those overrides.

| Variable | Default | ZimaBoard 2 | Description |
|---|---|---|---|
| `WAKE_MODEL` | `hey_peregrine` | | openWakeWord model name |
| `WAKE_MODEL_PATH` | *(auto-detected)* | | Path to custom `.onnx` wake word model |
| `WAKE_THRESHOLD` | `0.85` | | Wake word detection threshold (0.0–1.0) |
| `WAKE_ACTIVATIONS` | `3` | | Consecutive frames above threshold required to trigger |
| `WHISPER_SIZE` | `base.en` | | Whisper model size |
| `OLLAMA_URL` | `http://localhost:11434` | | LLM API endpoint (`genie_server.py` on the Q6A, `llm_bridge.py` on the ZimaBoard 2) |
| `OLLAMA_MODEL` | `llama3.2:1b-npu` | | Model identifier for LLM requests |
| `PIPER_MODEL` | `~/piper-voices/en_US-libritts_r-medium.onnx` | | Path to Piper voice model |
| `SILENCE_THRESHOLD` | `500` | | Amplitude below which audio is silence |
| `SILENCE_DURATION` | `1.5` | | Seconds of silence before stopping recording |
| `MAX_RECORD_SECONDS` | `15` | `30` | Longest a single spoken request may run |
| `WAKE_MIN_RMS` | `300` | `0` (off) | Skip the wake-word model on chunks quieter than this. The ZimaBoard has CPU to spare, so it listens to every chunk |
| `LLM_SYSTEM_PROMPT_BUDGET_CHARS` | `2200` | `8000` | Size cap on the LLM's instructions. Sized to the model's context window |
| `PEREGRINE_LLM_GATE` | *(empty = off)* | `/run/peregrine/llm-gate.sock` | Yield gate: pauses a long web-chat answer while a voice command runs |
| `CPU_THREADS` | `8` | `3` | Threads for faster-whisper inference |
| `MQTT_BROKER` | *(empty = disabled)* | | MQTT broker hostname or IP |
| `MQTT_PORT` | `8883` | | MQTT broker port |
| `MQTT_USE_TLS` | `true` | | Enable TLS for MQTT connection |
| `MQTT_CA_CERT` | *(empty)* | | Path to CA certificate for self-signed certs |
| `MQTT_USERNAME` | *(empty)* | | MQTT authentication username |
| `MQTT_PASSWORD` | *(empty)* | | MQTT authentication password |
| `HEADWATERS_URL` | `https://<MQTT_BROKER>` | | Base URL of the Headwaters REST API |
| `HEADWATERS_API_KEY` | *(empty)* | | `rv_...` API key from Headwaters → Settings → API Keys. Required for the "where am I", "nearby cities", and "capital of the state I'm in" intents (falls back to raw lat/lon without one). |
| `INTENT_RPC_PORT` | `11435` | | Loopback port the chat UI uses to reach the intent handler |
| `PEREGRINE_VOICE_PORT` | `8081` | | LAN listener for the `/api/voice` push-to-talk endpoint |
| `PEREGRINE_VOICE_TOKEN` | *(empty = disabled)* | | Bearer token required on every `/api/voice` request. See [docs/voice-terminal.md](docs/voice-terminal.md) |
| `PEREGRINE_VOICE_MAX_BYTES` | `1048576` | | Upload body cap (~30 sec of 16 kHz mono) |
| `PEREGRINE_VOICE_TARGET_SR` | `16000` | | Sample rate of the returned WAV (match the client codec) |

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
| `local/water/status` | `{"fresh": %, "grey": %, "black": %}` (0–100 percent) |
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

**Radxa Dragon Q6A (budget)**

- **Board**: Radxa Dragon Q6A (Qualcomm QCS6490 SoC, 8 GB RAM)
- **NPU**: Hexagon DSP v68 (12 TOPS) — runs LLM inference at ~12 tok/s
- **Audio**: USB microphone + speaker (auto-detected via ALSA, uses arecord/aplay)
- **Storage**: M.2 2230 NVMe SSD with Radxa OS Noble 24.04 (Ubuntu-based)
- **Power**: 12 V

**ZimaBoard 2 (premium)**: [buy a ZimaBoard 2](https://shop.zimaspace.com/products/zimaboard2-single-board-server?sca_ref=12564611.fDJvFR9EWxuoB4a&utm_source=dave-floyd&utm_medium=affiliate&utm_campaign=zimaspace_affiliate&utm_content=Dave-Floyd&sca_source=GitHub)
(affiliate link: we earn a small percentage, it costs you nothing extra)

- **Board**: ZimaBoard 2, 8 GB model (Intel N150, 4 cores)
- **LLM**: llama.cpp on three CPU cores, ~10 tok/s; one core kept for the voice loop
- **Audio**: USB speakerphone (auto-detected via ALSA, uses arecord/aplay); onboard audio is disabled
- **Storage**: 32 GB internal eMMC with Ubuntu Noble 24.04 (amd64)
- **Power**: wired to the house battery; see [zimaboard2/docs/install.md](zimaboard2/docs/install.md) for the firmware power-loss setting

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
source ~/.oww-trainer-venv/bin/activate      # your openwakeword trainer venv
rm -f output/hey_peregrine.onnx output/hey_peregrine.onnx.data
rm -f export/hey_peregrine.onnx export/hey_peregrine.onnx.data
```

- Stop anything else that holds GPU memory (image generators, other ML jobs). On an 8 GB card, training runs out of memory if another job holds a few GB.
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

After training, copy the model to the board. The model-only update works on
both boards:

```bash
scp models/hey_peregrine.onnx* trailcurrent@peregrine.local:/home/trailcurrent/models/
ssh trailcurrent@peregrine.local "sudo systemctl restart voice-assistant"

# Q6A only: full deploy (code + model + service) — see image_build/docs/07-development.md
./deploy.sh peregrine.local
```

For a permanent install (so the new model survives a reinstall), leave the
new files in `models/` and rebuild the image. Both builds take the wake word
from `models/`: `sudo ./image_build/build.sh` for the Q6A,
`./zimaboard2/image/build.sh --version X.Y` for the ZimaBoard 2.

## Third-Party Licenses

### Runtime Components

| Component | License |
|---|---|
| [openWakeWord](https://github.com/dscripka/openWakeWord) | Apache 2.0 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT |
| [Llama 3.2 1B](https://huggingface.co/meta-llama/Llama-3.2-1B) (Q6A) | Llama 3.2 Community |
| Qualcomm Genie (genie-t2t-run) (Q6A) | Qualcomm proprietary (bundled with NPU model) |
| [Llama 3.2 3B Instruct](https://huggingface.co/meta-llama/Llama-3.2-3B-Instruct) (ZimaBoard 2) | Llama 3.2 Community |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) (ZimaBoard 2) | MIT |
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
