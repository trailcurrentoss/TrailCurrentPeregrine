#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine — Hardware self-test
# Validates that the audio chain, iGPU, and inference pipeline all work.
# Safe to run any time. Exit code 0 if everything passes, non-zero otherwise.
#
# Modes:
#   peregrine-self-test.sh             Full hardware + service self-test
#   peregrine-self-test.sh --show-ca   Print the web-chat CA cert + SHA-256
#                                      fingerprint, then exit. Used when an
#                                      operator needs to install the CA on a
#                                      client device. Requires shell access
#                                      to the board (i.e. SSH credentials),
#                                      so it's the gated bootstrap path.
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
WAKE_MODEL="${PEREGRINE_HOME}/models/hey_peregrine.onnx"
CA_PATH="${PEREGRINE_HOME}/certs/ca.pem"

# ── Subcommand: print the CA cert + fingerprint and exit ────────────────────
if [[ "${1:-}" == "--show-ca" || "${1:-}" == "show-ca" || "${1:-}" == "ca" ]]; then
    if [[ ! -f "$CA_PATH" ]]; then
        echo "ERROR: CA certificate not found at $CA_PATH" >&2
        echo "       Run 'sudo peregrine-gen-certs.sh' to mint one." >&2
        exit 1
    fi
    echo ""
    echo -e "${BOLD}${GREEN}Trail${TEAL}Current${RESET} ${BOLD}Peregrine — Web UI CA Certificate${RESET}"
    echo ""
    echo -e "${BOLD}Subject:${RESET}"
    openssl x509 -in "$CA_PATH" -noout -subject -issuer -dates \
        | sed -E 's/^/  /'
    echo ""
    echo -e "${BOLD}SHA-256 fingerprint${RESET} (verify this on the install side):"
    openssl x509 -in "$CA_PATH" -noout -fingerprint -sha256 \
        | sed -E 's|.*=|  |'
    echo ""
    echo -e "${BOLD}PEM:${RESET}  (copy everything between BEGIN/END lines into a .pem file)"
    echo ""
    cat "$CA_PATH"
    echo ""
    echo -e "${BOLD}To install on a client:${RESET}"
    echo "  scp trailcurrent@peregrine.local:${CA_PATH} ./peregrine-ca.pem"
    echo "  # then import peregrine-ca.pem into your OS / browser trust store"
    echo ""
    exit 0
fi

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

# ── 4. iGPU Vulkan device ──────────────────────────────────────────────────
section "4. iGPU (Vulkan)"
if command -v vulkaninfo >/dev/null 2>&1; then
    GPU=$(vulkaninfo --summary 2>/dev/null | awk -F= '/deviceName/ {gsub(/^ +/, "", $2); print $2}' | grep -vi llvmpipe | head -1)
    if [[ -n "$GPU" ]]; then
        ok "Vulkan GPU: ${GPU}"
    else
        err "No hardware Vulkan device (only llvmpipe or none) — LLM would run on CPU"
    fi
else
    warn "vulkaninfo not installed"
fi

# ── 5. LLM: llama-server, bridge, yield gate ────────────────────────────────
section "5. LLM server"
if systemctl is-active --quiet peregrine-llm; then
    ok "peregrine-llm.service is active ($(basename "$(sed -n 's/^LLM_MODEL=//p' /etc/peregrine/llm.env)"))"
    if curl -sf -m 5 http://127.0.0.1:8080/health >/dev/null; then
        ok "llama-server /health OK"
    else
        err "llama-server is not healthy"
    fi
else
    err "peregrine-llm.service is not active"
fi
if systemctl is-active --quiet peregrine-llm-bridge; then
    ok "peregrine-llm-bridge.service is active"
    if timeout 65 curl -sf -m 60 http://127.0.0.1:11434/api/generate \
        -H 'Content-Type: application/json' \
        -d '{"prompt":"hi","system":"Reply in one word.","options":{"num_predict":8}}' >/dev/null; then
        ok "Bridge answered an inference request"
    else
        warn "Bridge is running but did not answer within 60s"
    fi
else
    err "peregrine-llm-bridge.service is not active"
fi
if systemctl is-active --quiet peregrine-llm-gate.socket; then
    ok "Yield gate socket listening"
else
    err "peregrine-llm-gate.socket is not active"
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
