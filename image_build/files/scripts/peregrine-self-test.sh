#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine — Hardware self-test
# Validates that the audio chain, NPU, and inference pipeline all work.
# Safe to run any time. Exit code 0 if everything passes, non-zero otherwise.
# ============================================================================

set -uo pipefail

GREEN='\033[38;5;70m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
TEAL='\033[38;5;30m'
BOLD='\033[1m'
RESET='\033[0m'

PEREGRINE_HOME="/home/trailcurrent"
VENV="${PEREGRINE_HOME}/assistant-env"
NPU_DIR="${PEREGRINE_HOME}/Llama3.2-1B-1024-v68"
WAKE_MODEL="${PEREGRINE_HOME}/models/hey_peregrine.onnx"

PASS=0
FAIL=0
WARN=0

ok()   { echo -e "  ${GREEN}\xE2\x9C\x93${RESET} $*"; PASS=$((PASS+1)); }
err()  { echo -e "  ${RED}\xE2\x9C\x97${RESET} $*"; FAIL=$((FAIL+1)); }
warn() { echo -e "  ${YELLOW}!${RESET} $*"; WARN=$((WARN+1)); }
section() { echo ""; echo -e "${BOLD}${TEAL}${1}${RESET}"; }

echo ""
echo -e "${BOLD}${GREEN}Trail${TEAL}Current${RESET} ${BOLD}Peregrine — Hardware Self-Test${RESET}"
echo ""

# ── Stop voice assistant for audio tests (holds the Jabra device open) ───────
VA_WAS_RUNNING=false
if systemctl is-active --quiet voice-assistant 2>/dev/null; then
    VA_WAS_RUNNING=true
    sudo systemctl stop voice-assistant 2>/dev/null || true
    sleep 1
fi

# ── 1. ALSA capture device ──────────────────────────────────────────────────
section "1. ALSA capture device"
if arecord -l 2>/dev/null | grep -q "card "; then
    DEV=$(arecord -l 2>/dev/null | grep "card " | head -1)
    ok "Capture device found: ${DEV}"
else
    err "No ALSA capture device — is the Jabra Speak connected?"
fi

# ── 2. ALSA playback device ─────────────────────────────────────────────────
section "2. ALSA playback device"
if aplay -l 2>/dev/null | grep -q "card "; then
    DEV=$(aplay -l 2>/dev/null | grep "card " | head -1)
    ok "Playback device found: ${DEV}"
else
    err "No ALSA playback device"
fi

# ── 3. Microphone capture (3 sec, RMS check) ────────────────────────────────
section "3. Microphone (3-second capture, RMS check)"
TMP_WAV=$(mktemp --suffix=.wav)
if timeout 5 arecord -d 3 -f S16_LE -r 16000 -c 1 "$TMP_WAV" >/dev/null 2>&1; then
    RMS=$(python3 -c "
import wave, audioop, sys
try:
    w = wave.open('${TMP_WAV}', 'rb')
    data = w.readframes(w.getnframes())
    w.close()
    print(audioop.rms(data, 2))
except Exception as e:
    print(0)
")
    if [[ "$RMS" -gt 5 ]]; then
        ok "Microphone captured audio (RMS=${RMS})"
    else
        warn "Microphone captured silence (RMS=${RMS}) — check mute switch"
    fi
else
    err "arecord failed"
fi
rm -f "$TMP_WAV"

# ── Restart voice assistant now that audio tests are done ───────────────────
if $VA_WAS_RUNNING; then
    sudo systemctl start voice-assistant 2>/dev/null || true
fi

# ── 4. CDSP remoteproc state ────────────────────────────────────────────────
section "4. NPU CDSP remoteproc"
CDSP_OK=false
for rp in /sys/class/remoteproc/remoteproc*/; do
    [[ -f "${rp}firmware" ]] || continue
    fw=$(cat "${rp}firmware" 2>/dev/null)
    state=$(cat "${rp}state" 2>/dev/null)
    if echo "$fw" | grep -q cdsp; then
        if [[ "$state" == "running" ]]; then
            ok "CDSP running (${fw})"
            CDSP_OK=true
        else
            err "CDSP state=${state} (expected running)"
        fi
    fi
done
$CDSP_OK || err "No CDSP remoteproc found"

# ── 5. Genie server (NPU LLM HTTP wrapper) ──────────────────────────────────
section "5. Genie NPU LLM server"
if systemctl is-active --quiet genie-server; then
    ok "genie-server.service is active"
    if timeout 15 curl -sf -m 10 http://localhost:11434/api/generate \
        -H 'Content-Type: application/json' \
        -d '{"prompt":"hi","system":"Reply in one word."}' >/dev/null; then
        ok "Genie server responded to inference request"
    else
        warn "Genie server is running but did not respond within 30s"
    fi
else
    err "genie-server.service is not active"
fi

# ── 6. Wake-word model load ─────────────────────────────────────────────────
section "6. Custom wake-word model"
if [[ -f "$WAKE_MODEL" ]]; then
    if "${VENV}/bin/python3" -c "
from openwakeword.model import Model
m = Model(wakeword_models=['${WAKE_MODEL}'])
print('OK')
" 2>/dev/null | grep -q OK; then
        ok "hey_peregrine.onnx loaded successfully"
    else
        err "Wake-word model failed to load"
    fi
else
    err "${WAKE_MODEL} missing"
fi

# ── 7. Voice assistant service ──────────────────────────────────────────────
section "7. Voice assistant service"
if systemctl is-active --quiet voice-assistant; then
    ok "voice-assistant.service is active"
else
    err "voice-assistant.service is not active"
fi

# ── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Summary:${RESET} ${GREEN}${PASS} passed${RESET}, ${YELLOW}${WARN} warnings${RESET}, ${RED}${FAIL} failed${RESET}"
echo ""
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
