#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine — Development push
#
# Copies updated source files (assistant.py, genie_server.py, wake-word
# model, service definitions) to a running Peregrine board over SSH.
#
# This is a DEVELOPMENT TOOL — for production installs, build and flash an
# image with image_build/build.sh + image_build/flash.sh. Use this only when
# iterating on src/ between full image rebuilds.
#
# The board must already be flashed with a Peregrine image and reachable
# via SSH as the trailcurrent user.
#
# Usage:
#   ./deploy.sh peregrine.local
#   ./deploy.sh 192.168.1.50
#   ./deploy.sh trailcurrent@192.168.1.50
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <hostname-or-ip>"
    echo "  e.g. $0 peregrine.local"
    exit 1
fi

TARGET="$1"
if [[ "$TARGET" != *@* ]]; then
    TARGET="trailcurrent@${TARGET}"
fi

REMOTE_HOME="/home/trailcurrent"

echo "Deploying to ${TARGET}..."
echo ""

# ── ControlMaster: one auth, many commands ─────────────────────────────────
SOCK="/tmp/peregrine-deploy-$$"
ssh -o ControlMaster=yes -o ControlPersist=60 -o ControlPath="$SOCK" -fN "$TARGET" || {
    echo "ERROR: Could not connect to ${TARGET}"
    echo "  Make sure the board is reachable and the trailcurrent user can log in."
    exit 1
}

cleanup() {
    ssh -o ControlPath="$SOCK" -O exit "$TARGET" 2>/dev/null || true
}
trap cleanup EXIT

SCP="scp -o ControlPath=$SOCK"
SSH="ssh -o ControlPath=$SOCK"

# ── 1. Refresh openwakeword (--no-deps; tflite-runtime has no aarch64 wheel) ─
echo "[1/5] Refreshing openwakeword + timezonefinder..."
$SSH "$TARGET" "${REMOTE_HOME}/assistant-env/bin/pip install -q --force-reinstall --no-deps openwakeword 2>&1 | tail -1"
$SSH "$TARGET" "${REMOTE_HOME}/assistant-env/bin/pip install -q timezonefinder 2>&1 | tail -1"

# ── 2. assistant.py ─────────────────────────────────────────────────────────
echo "[2/5] Copying assistant.py..."
$SCP "${SCRIPT_DIR}/src/assistant.py" "${TARGET}:${REMOTE_HOME}/assistant.py"

# ── 3. genie_server.py ──────────────────────────────────────────────────────
echo "[3/5] Copying genie_server.py..."
$SCP "${SCRIPT_DIR}/src/genie_server.py" "${TARGET}:${REMOTE_HOME}/genie_server.py"

# ── 4. Wake-word model ──────────────────────────────────────────────────────
echo "[4/5] Copying wake-word model..."
$SSH "$TARGET" "mkdir -p ${REMOTE_HOME}/models"
$SCP "${SCRIPT_DIR}/models/hey_peregrine.onnx" "${TARGET}:${REMOTE_HOME}/models/hey_peregrine.onnx"
if [ -f "${SCRIPT_DIR}/models/hey_peregrine.onnx.data" ]; then
    $SCP "${SCRIPT_DIR}/models/hey_peregrine.onnx.data" "${TARGET}:${REMOTE_HOME}/models/hey_peregrine.onnx.data"
fi

# ── 5. Service files ────────────────────────────────────────────────────────
echo "[5/5] Copying service files..."
$SCP "${SCRIPT_DIR}/config/voice-assistant.service" "${TARGET}:/tmp/voice-assistant.service"
$SCP "${SCRIPT_DIR}/config/genie-server.service"   "${TARGET}:/tmp/genie-server.service"
$SSH "$TARGET" "sudo install -m 644 /tmp/voice-assistant.service /etc/systemd/system/voice-assistant.service && \
                sudo install -m 644 /tmp/genie-server.service   /etc/systemd/system/genie-server.service && \
                rm -f /tmp/voice-assistant.service /tmp/genie-server.service && \
                sudo systemctl daemon-reload"

echo ""
echo "Deploy complete. Files copied:"
echo "  ${REMOTE_HOME}/assistant.py"
echo "  ${REMOTE_HOME}/genie_server.py"
echo "  ${REMOTE_HOME}/models/hey_peregrine.onnx"
echo "  /etc/systemd/system/voice-assistant.service"
echo "  /etc/systemd/system/genie-server.service"
echo ""
echo "To restart the assistant:"
echo "  ssh ${TARGET} sudo systemctl restart voice-assistant"
echo ""
echo "To watch logs:"
echo "  ssh ${TARGET} sudo journalctl -u voice-assistant -f"
echo ""
