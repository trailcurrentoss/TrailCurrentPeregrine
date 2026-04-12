# 5. First login

The first time you SSH into a freshly-flashed Peregrine board, an interactive
wizard runs automatically. This document walks through what it does.

## Connecting

```bash
ssh trailcurrent@peregrine.local
```

Default credentials: **`trailcurrent`** / **`trailcurrent`**

(If `peregrine.local` doesn't resolve on your network, use the board's IP.)

## What you'll see

```
                                          o
                                   .
                /\
               /  \              /\
              /    \            /  \
             /      \          /    \
            /  /\    \        /      \
           /  /  \    \      /        \
          /  /    \    \    /          \
         /  /      \    \  /            \
    ____/  /        \    \/              \____

    TrailCurrent Peregrine v1.0  |  Voice Assistant
    up 2 minutes


============================================
  TrailCurrent Peregrine — First-time setup
============================================

── Change password ──
  This board ships with the default password 'trailcurrent'.
  You must change it before continuing.

  Changing password for trailcurrent.
  Current password:
  New password:
  Retype new password:
  passwd: password updated successfully
  ✓ Password changed
```

The wizard then walks through four steps:

## Step 1 — Change password (forced)

Required. The wizard will keep prompting until you successfully change the
password. There is no way to skip this on first run.

## Step 2 — MQTT broker (optional)

```
── MQTT broker (optional) ──
  Configure MQTT now? [Y/n]: y
  Broker hostname or IP: 192.168.1.100
  Port [8883]:
  Use TLS? [Y/n]:
  Username (blank to skip): peregrine
  Password (blank to skip): ********
  ✓ Wrote /home/trailcurrent/assistant.env
```

If you enable TLS, the wizard reminds you to copy your CA certificate from
your laptop:

```bash
scp ca.pem trailcurrent@peregrine.local:/home/trailcurrent/ca.pem
```

You can skip this step entirely by answering `n`. The assistant will run
without MQTT — wake-word + STT + LLM + TTS still all work locally; you just
don't get cloud telemetry.

## Step 3 — Static IP (optional)

```
── Network (optional) ──
  Configure a static IP? [y/N]: y
  Address with prefix (e.g. 192.168.1.50/24): 192.168.1.50/24
  Gateway (e.g. 192.168.1.1): 192.168.1.1
  DNS server [1.1.1.1]:
  ✓ Static IP configured on Wired connection 1
```

The wizard uses `nmcli` to write the address to the existing wired connection
profile. If your network has reliable DHCP you can skip this entirely.

## Step 4 — Hardware self-test (optional)

```
── Hardware self-test (optional) ──
  Run hardware self-test now? [Y/n]:
```

This runs `peregrine-self-test` which checks:

1. ALSA capture device (Jabra Speak)
2. ALSA playback device
3. Speaker emits a 1-second tone
4. Microphone captures non-silent audio
5. NPU CDSP remoteproc is `running`
6. Genie LLM server responds to a test inference
7. Wake-word model loads
8. `voice-assistant` service is active

A passing run looks like:

```
1. ALSA capture device
  ✓ Capture device found: card 1: USB [Jabra SPEAK 410 USB], device 0...
2. ALSA playback device
  ✓ Playback device found: card 1: USB [Jabra SPEAK 410 USB], device 0...
3. Speaker (1-second tone)
  ✓ Speaker emitted tone
4. Microphone (3-second capture, RMS check)
  ✓ Microphone captured audio (RMS=147)
5. NPU CDSP remoteproc
  ✓ CDSP running (qcom/cdsp.mbn)
6. Genie NPU LLM server
  ✓ genie-server.service is active
  ✓ Genie server responded to inference request
7. Custom wake-word model
  ✓ hey_peregrine.onnx loaded successfully
8. Voice assistant service
  ✓ voice-assistant.service is active

Summary: 8 passed, 0 warnings, 0 failed
```

If anything fails, the wizard still completes — but you should investigate
before relying on the assistant.

## Step 5 — Done

```
── Starting voice assistant ──
  ✓ Services restarted

============================================
  TrailCurrent Peregrine — Setup complete
============================================
  Quick reference:
    Logs:      sudo journalctl -u voice-assistant -f
    Restart:   sudo systemctl restart voice-assistant
    Edit cfg:  nano ~/assistant.env
    Self-test: peregrine-self-test

  Say "hey peregrine" to wake the assistant.
```

The wizard creates `~/.peregrine-setup-complete` so it never runs again on
this board automatically. You can re-run it manually any time:

```bash
/usr/local/bin/peregrine-first-login.sh
```

## Trying it out

After the wizard exits, the assistant is operational. Wake it by saying:

> "Hey Peregrine, what time is it?"

You should hear a Piper TTS reply within ~2 seconds.

To watch what's happening in real time:

```bash
sudo journalctl -u voice-assistant -f
```

## Reference: aliases installed by the build

The shell prompt and a few aliases are set up in `/etc/profile.d/trailcurrent-prompt.sh`:

| Alias | Expands to |
|---|---|
| `peregrine-logs` | `sudo journalctl -u voice-assistant -f` |
| `peregrine-genie-logs` | `sudo journalctl -u genie-server -f` |
| `peregrine-restart` | `sudo systemctl restart voice-assistant` |
| `peregrine-status` | `systemctl status voice-assistant genie-server --no-pager` |
| `peregrine-self-test` | `/usr/local/bin/peregrine-self-test.sh` |

## Next

Day-to-day operation → [06-troubleshooting.md](06-troubleshooting.md)

Iterating on `src/` without rebuilding the whole image → [07-development.md](07-development.md)
