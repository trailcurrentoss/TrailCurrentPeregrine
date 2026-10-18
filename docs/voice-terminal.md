# Voice terminal endpoint (`/api/voice`)

An additional HTTP endpoint on `peregrine-chat.service` that lets a **dumb
remote device** (an ESP32-P4 with a mic + speaker, a browser tab, an MQTT
bridge — anything that can POST a WAV) act as a push-to-talk terminal for
the Peregrine LLM. No wake word required — the client decides when to
capture and when to send.

The first consumer is the ESP32-P4 HMI

## What the endpoint does

Runs the same pipeline the on-board voice loop does, but as a single HTTP
request/response:

```
POST /api/voice   (audio/wav upload, ~16 kHz mono S16LE)
  │
  ├─▶ faster-whisper (base.en, INT8) ── transcript
  │
  ├─▶ 127.0.0.1:11435/api/intent   ── canned answer? use it and skip LLM
  │       │
  │       └── no match ──▶ /api/enrich (add "you are in <city>" if relevant)
  │                             │
  │                             └──▶ genie-server /api/chat (Llama 3.2 1B on NPU)
  │                                       │
  │                                       └── if JSON emitted, run through
  │                                           /api/command (device control),
  │                                           use spoken confirmation as reply
  │
  ├─▶ Piper TTS (en_US-libritts_r-medium, 22.05 kHz output)
  │
  └─▶ audioop.ratecv resample to 16 kHz mono S16LE
          │
          ▼
   audio/wav response
   + X-Peregrine-Transcript header (URL-encoded)
   + X-Peregrine-Response   header (URL-encoded)
```

Everything runs in the `peregrine-chat.service` process, in the same venv
that `voice-assistant.service` already uses.

## Security model

`POST /api/voice` is served on two listeners:

- **HTTPS (`WEB_CHAT_HTTPS_PORT`, 443) — the default path.** Same listener
  and same trust as `/api/chat`: the client is on the LAN and trusts
  Peregrine's CA. No token. Headwaters hands its Peregrine URL and CA to
  devices such as Tiercel, so nothing extra is provisioned. Only offered
  when TLS is enabled.
- **Plain HTTP (`PEREGRINE_VOICE_PORT`, 8081) — legacy.** For terminals
  that can't verify the CA. Because it has no TLS, it is gated by the bearer
  token below and stays closed when no token is set.

The rest of this section applies to the plain-HTTP listener.

- Plain HTTP, not HTTPS. Trust boundary is "already on the LAN with the LLM
  box" plus the token.
- Bearer token required on every request. When `PEREGRINE_VOICE_TOKEN` is
  empty the endpoint stays closed (`[voice] endpoint disabled` in the
  journal) — that's the safe default.
- Token comparison uses `hmac.compare_digest` (constant-time).

## Setup — enable the endpoint on a running board

Do this AFTER a normal `./deploy.sh peregrine.local` has landed the current
`src/` (which includes `stt.py` alongside `assistant.py`, `tts.py`,
`web_chat.py`).

```bash
# ── 1. On your workstation: generate + save the shared token ──────────────
openssl rand -hex 32 > ~/.peregrine-voice-token
chmod 600 ~/.peregrine-voice-token

# ── 2. Install the token as a systemd drop-in on the board ────────────────
# Expand the substitution locally so it ends up as a literal in the
# heredoc — if you SSH first and then run $(cat …) you'll cat a file that
# only exists on your workstation, and the token will silently become "".
TOKEN=$(cat ~/.peregrine-voice-token | tr -d '\n')
ssh -t trailcurrent@peregrine.local "
  sudo mkdir -p /etc/systemd/system/peregrine-chat.service.d &&
  printf '[Service]\nEnvironment=PEREGRINE_VOICE_TOKEN=%s\n' '$TOKEN' |
    sudo tee /etc/systemd/system/peregrine-chat.service.d/voice.conf > /dev/null &&
  sudo systemctl daemon-reload &&
  sudo systemctl restart peregrine-chat.service &&
  sleep 3 &&
  sudo journalctl -u peregrine-chat.service -n 10 --no-pager | grep -E 'voice|listening'
"
```

Expected journal output:

```
[voice] listening on http://0.0.0.0:8081 (target_sr=16000, max_bytes=1048576)
[web-chat] HTTPS listening on https://0.0.0.0:443 (cert=…)
[web-chat] HTTP redirector on :80 → HTTPS (also serves /ca.pem unencrypted)
```

If instead you see `[voice] endpoint disabled (PEREGRINE_VOICE_TOKEN unset)`,
the drop-in didn't take — most likely the `$TOKEN` substitution was empty
(see gotcha below).

## Verifying — curl round-trip

```bash
# ── Make a valid 16 kHz mono S16LE test WAV (pure sine — Whisper will
#    return empty transcript and the endpoint will respond with the
#    "I didn't catch that." fallback, which still exercises the whole
#    STT → intent → TTS → resample chain).
python3 -c "
import wave, math, struct
sr = 16000
frames = [int(30000*math.sin(2*math.pi*300*t/sr)) for t in range(3*sr)]
with wave.open('/tmp/what-time.wav','wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
    w.writeframes(b''.join(struct.pack('<h', f) for f in frames))
"

# ── POST it and grab the reply ────────────────────────────────────────────
TOKEN=$(cat ~/.peregrine-voice-token | tr -d '\n')
curl -sS -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: audio/wav" \
     --data-binary @/tmp/what-time.wav \
     -D /tmp/reply-headers.txt -o /tmp/reply.wav \
     -w "http_code=%{http_code} time=%{time_total}s size=%{size_download}B\n" \
     http://peregrine.local:8081/api/voice
cat /tmp/reply-headers.txt
file /tmp/reply.wav          # → RIFF … WAVE audio, 16-bit mono 16000 Hz
aplay /tmp/reply.wav
```

Expected: `http_code=200`, ~30 KB WAV, `X-Peregrine-Response:
I%20didn%27t%20catch%20that.`, ~4–5 sec round trip.

For a real speech test, record on the workstation (`arecord -f S16_LE -c1
-r16000 -d5 real.wav`) and POST that instead.

## Environment variables

Set in `peregrine-chat.service` (see [`config/peregrine-chat.service`](../config/peregrine-chat.service)):

| Variable | Default | Notes |
|---|---|---|
| `PEREGRINE_VOICE_PORT` | `8081` | LAN listener. Change if it clashes. |
| `PEREGRINE_VOICE_TOKEN` | *(empty)* | **Required**. Empty disables the endpoint. |
| `PEREGRINE_VOICE_MAX_BYTES` | `1048576` | 1 MB cap on upload body. ~30 sec of 16 kHz mono. |
| `PEREGRINE_VOICE_TARGET_SR` | `16000` | Output sample rate. Match the client codec. |
| `PIPER_MODEL` | `~/piper-voices/en_US-libritts_r-medium.onnx` | Voice for the reply. |
| `WHISPER_SIZE` | `base.en` | Model size. Match what's installed for `assistant.py`. |
| `CPU_THREADS` | `8` | Q6A has 8 cores — use them all for STT. |

The service uses the **assistant venv Python**
(`/home/trailcurrent/assistant-env/bin/python3`) because faster-whisper and
piper live there. `web_chat.py` was stdlib-only before this endpoint;
switching to the venv is purely additive.

## Gotchas (things that already broke us once)

**1. Heredoc `$(cat ~/.peregrine-voice-token)` expands on the wrong host.**

If you SSH into Peregrine and *then* write the drop-in:

```bash
ssh trailcurrent@peregrine.local
sudo tee /etc/…/voice.conf <<EOF
[Service]
Environment=PEREGRINE_VOICE_TOKEN=$(cat ~/.peregrine-voice-token)   # ← expands ON PEREGRINE
EOF
```

…the substitution runs against Peregrine's home dir, which doesn't have
the file, so the value becomes empty and the endpoint stays disabled. The
journal shows `[Service]\nEnvironment=PEREGRINE_VOICE_TOKEN=` with a bare
trailing `=` — that's the tell.

Fix: expand `TOKEN` locally first, then interpolate into the ssh command
(the setup block above does this correctly).

**2. `deploy.sh` doesn't restart services.**

By design — the tail of `deploy.sh` prints the restart command instead of
running it, so an operator can eyeball the changes first. After a deploy
that touches `web_chat.py` or `peregrine-chat.service`, you still need:

```bash
ssh -t trailcurrent@peregrine.local sudo systemctl restart peregrine-chat.service
```

**3. `web_chat.py` runs under the venv, not system Python.**

If you see `[voice] pipeline error: No module named 'faster_whisper'`,
`ExecStart` still points at `/usr/bin/python3`. Point it at
`/home/trailcurrent/assistant-env/bin/python3` (already the default in the
committed unit).

**4. `X-Peregrine-Transcript` / `X-Peregrine-Response` are URL-encoded.**

HTTP header values can't carry newlines or non-ASCII cleanly, so both
headers are `urllib.parse.quote`d. Decode on the client side:

```bash
python3 -c "import urllib.parse; print(urllib.parse.unquote('$(grep -i X-Peregrine-Response /tmp/reply-headers.txt | cut -d: -f2- | tr -d ' \r\n')'))"
```

## Files

- [`src/web_chat.py`](../src/web_chat.py) — the `VoiceHandler` class + listener wiring
- [`src/stt.py`](../src/stt.py) — reusable `STTEngine` wrapping faster-whisper
- [`src/tts.py`](../src/tts.py) — `TTSEngine.render_to_wav_bytes()` synthesizes without aplay
- [`config/peregrine-chat.service`](../config/peregrine-chat.service) — env vars + venv Python path
- [`deploy.sh`](../deploy.sh) — copies all four files above to the running board

## Extending

- **Streaming response**: today the endpoint is unary (full WAV back in one
  response). Chunked `Transfer-Encoding` + on-the-fly Piper output could cut
  first-audio latency in half. Not needed for the MVP.
- **Multi-turn context**: the endpoint treats each request as a fresh
  single-turn message. If you want conversation memory the client can
  maintain a history and POST a JSON envelope wrapping the WAV (or a
  separate `X-Peregrine-History` header).
- **Alternative voices**: any Piper `.onnx` in `~/piper-voices/` — set
  `PIPER_MODEL` in the drop-in.
