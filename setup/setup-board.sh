#!/usr/bin/env bash
# ============================================================================
# Voice Assistant — Board Setup & Hardening
# For Radxa Dragon Q6A (RK3588S, Ubuntu Noble 24.04, 8 GB RAM)
#
# Combines provisioning (packages, users, models) with system hardening
# (disable desktop, tune CPU/Ollama, power-down unused hardware).
#
# Idempotent — safe to re-run on a fresh board or an existing one.
# Each step checks whether work is already done and skips if so.
#
# Run as root on the board:
#   chmod +x setup-board.sh && ./setup-board.sh
# ============================================================================

set -uo pipefail

ASSISTANT_USER="assistant"
ASSISTANT_HOME="/home/${ASSISTANT_USER}"
VENV_DIR="${ASSISTANT_HOME}/assistant-env"
OLLAMA_MODEL="qwen2.5:0.5b"
PIPER_VOICE="en_US-libritts_r-medium"
PIPER_VOICE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts_r/medium"
PIPER_DIR="${ASSISTANT_HOME}/piper-voices"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; }
fatal(){ echo -e "${RED}[FATAL]${NC} $*"; exit 1; }

ERRORS=0
step_fail() { err "$*"; ERRORS=$((ERRORS + 1)); }

[[ $(id -u) -eq 0 ]] || fatal "Run this script as root"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "============================================"
echo "  Voice Assistant — Board Setup & Hardening"
echo "============================================"
echo ""

# ============================================================================
# 1. System packages
# ============================================================================
log "1. System packages"

apt update -y || step_fail "apt update failed"
apt upgrade -y || warn "apt upgrade had issues (continuing)"

apt install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    git \
    curl \
    wget \
    ffmpeg \
    alsa-utils \
    libsndfile1 \
    libasound2-dev \
    htop \
|| step_fail "Some packages failed to install"

# ============================================================================
# 2. Create dedicated user
# ============================================================================
log "2. Assistant user"

if id "${ASSISTANT_USER}" &>/dev/null; then
    log "  User '${ASSISTANT_USER}' already exists"
else
    useradd -m -s /bin/bash -G audio "${ASSISTANT_USER}" \
        || step_fail "Failed to create user '${ASSISTANT_USER}'"
fi

usermod -aG audio "${ASSISTANT_USER}" 2>/dev/null || true

# ============================================================================
# 3. Ollama
# ============================================================================
log "3. Ollama"

if command -v ollama &>/dev/null; then
    log "  Ollama already installed"
else
    curl -fsSL https://ollama.com/install.sh | sh || step_fail "Ollama install failed"
fi

systemctl enable ollama 2>/dev/null || true
systemctl start ollama 2>/dev/null || true

# Tune Ollama for RK3588S before pulling model
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/override.conf << 'EOF'
[Service]
# Use all 8 cores for inference
Environment=OLLAMA_NUM_THREADS=8
# Keep models loaded indefinitely (assistant is the only consumer)
Environment=OLLAMA_KEEP_ALIVE=-1
# Bind to localhost only
Environment=OLLAMA_HOST=127.0.0.1:11434
EOF
systemctl daemon-reload
systemctl restart ollama 2>/dev/null || true
log "  Ollama tuned: 8 threads, keep_alive=-1"

log "  Waiting for Ollama API..."
OLLAMA_READY=false
for i in $(seq 1 30); do
    if curl -s http://localhost:11434/api/tags &>/dev/null; then
        OLLAMA_READY=true
        break
    fi
    sleep 2
done

if [[ "${OLLAMA_READY}" == "true" ]]; then
    if ollama list | grep -q "${OLLAMA_MODEL%%:*}"; then
        log "  Model '${OLLAMA_MODEL}' already pulled"
    else
        log "  Pulling model: ${OLLAMA_MODEL} (this will take a while)..."
        ollama pull "${OLLAMA_MODEL}" || step_fail "Failed to pull Ollama model"
    fi
else
    step_fail "Ollama API not reachable after 60s"
fi

# ============================================================================
# 4. Python virtual environment and packages
# ============================================================================
log "4. Python virtual environment"

if [[ -x "${VENV_DIR}/bin/python3" ]]; then
    log "  Venv already exists at ${VENV_DIR}"
else
    python3 -m venv "${VENV_DIR}" || step_fail "Failed to create venv"
fi

if [[ -x "${VENV_DIR}/bin/python3" ]]; then
    log "  Installing/upgrading Python packages..."
    "${VENV_DIR}/bin/pip" install --upgrade pip 2>/dev/null || true
    "${VENV_DIR}/bin/pip" install \
        faster-whisper \
        piper-tts \
        paho-mqtt \
        numpy \
        requests \
    || step_fail "Some Python packages failed to install"

    # openwakeword installed separately with --no-deps because tflite-runtime
    # has no aarch64 wheel. We only use ONNX inference so tflite is not needed.
    "${VENV_DIR}/bin/pip" install --no-deps openwakeword \
    || step_fail "openwakeword install failed"

    log "  Verifying wake word model loads..."
    "${VENV_DIR}/bin/python3" -c "
from openwakeword.model import Model
m = Model()
print('  Wake word model OK')
del m
" || warn "Wake word model verification failed (non-fatal)"
fi

# ============================================================================
# 5. Custom wake word model
# ============================================================================
log "5. Custom wake word model (hey_peregrine)"

WAKE_MODEL_DIR="${ASSISTANT_HOME}/models"
mkdir -p "${WAKE_MODEL_DIR}"
MODELS_SRC="${SCRIPT_DIR}/../models"
if [[ -f "${MODELS_SRC}/hey_peregrine.onnx" ]]; then
    cp "${MODELS_SRC}/hey_peregrine.onnx" "${WAKE_MODEL_DIR}/"
    cp "${MODELS_SRC}/hey_peregrine.onnx.data" "${WAKE_MODEL_DIR}/" 2>/dev/null || true
    log "  Installed hey_peregrine wake word model"
else
    warn "  hey_peregrine.onnx not found in ${MODELS_SRC} — using default wake word"
fi

# ============================================================================
# 6. Piper TTS voice
# ============================================================================
log "6. Piper TTS voice"

mkdir -p "${PIPER_DIR}"

if [[ -f "${PIPER_DIR}/${PIPER_VOICE}.onnx" ]]; then
    log "  Piper voice already downloaded"
else
    wget -q --show-progress -O "${PIPER_DIR}/${PIPER_VOICE}.onnx" \
        "${PIPER_VOICE_URL}/${PIPER_VOICE}.onnx" \
    || step_fail "Failed to download Piper voice model"

    wget -q -O "${PIPER_DIR}/${PIPER_VOICE}.onnx.json" \
        "${PIPER_VOICE_URL}/${PIPER_VOICE}.onnx.json" \
    || warn "Failed to download Piper voice config (non-fatal)"
fi

# ============================================================================
# 7. systemd service
# ============================================================================
log "7. systemd service"

cat > /etc/systemd/system/voice-assistant.service << EOF
[Unit]
Description=Local Voice Assistant
After=network.target sound.target ollama.service
Wants=ollama.service

[Service]
Type=simple
User=${ASSISTANT_USER}
Group=audio
WorkingDirectory=${ASSISTANT_HOME}
ExecStartPre=/bin/sleep 5
ExecStart=${VENV_DIR}/bin/python3 ${ASSISTANT_HOME}/assistant.py
Restart=on-failure
RestartSec=10

# Environment
Environment=HOME=${ASSISTANT_HOME}
Environment=WAKE_MODEL_PATH=${ASSISTANT_HOME}/models/hey_peregrine.onnx
Environment=PATH=${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
# Site-specific config (MQTT, thresholds, etc.) lives in this file on the board.
# It is never overwritten by setup or deploy. Edit it with: nano ~/assistant.env
EnvironmentFile=-${ASSISTANT_HOME}/assistant.env

# Hardening
ProtectSystem=strict
ReadWritePaths=${ASSISTANT_HOME} /tmp
ProtectHome=tmpfs
BindPaths=${ASSISTANT_HOME}
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable voice-assistant 2>/dev/null || true
log "  Service installed and enabled"

# Create default env file if it doesn't exist (never overwrite)
if [[ ! -f "${ASSISTANT_HOME}/assistant.env" ]]; then
    cat > "${ASSISTANT_HOME}/assistant.env" << 'ENVEOF'
# Voice assistant environment config — persists across deploys and setup runs.
# Edit with: nano ~/assistant.env
# Then restart: sudo systemctl restart voice-assistant

# MQTT
#MQTT_BROKER=192.168.x.x
#MQTT_PORT=8883
#MQTT_USE_TLS=true
#MQTT_CA_CERT=/home/assistant/ca.pem
#MQTT_USERNAME=
#MQTT_PASSWORD=

# Audio tuning
#WAKE_THRESHOLD=0.5
#SILENCE_THRESHOLD=500
#SILENCE_DURATION=1.5
ENVEOF
    log "  Created default assistant.env (edit to configure MQTT)"
else
    log "  assistant.env already exists (not overwritten)"
fi

# ============================================================================
# 8. File ownership
# ============================================================================
log "8. File ownership"

chown -R "${ASSISTANT_USER}:${ASSISTANT_USER}" "${ASSISTANT_HOME}"

# ============================================================================
# 9. Disable graphical desktop
# ============================================================================
log "9. Disable graphical desktop"

if systemctl get-default | grep -q graphical; then
    systemctl set-default multi-user.target
    log "  Set default target to multi-user (CLI only)"
else
    log "  Already CLI-only"
fi

for dm in gdm3 gdm lightdm sddm; do
    if systemctl is-enabled "$dm" 2>/dev/null | grep -q enabled; then
        systemctl disable --now "$dm" 2>/dev/null
        log "  Disabled $dm"
    fi
done

# ============================================================================
# 10. Disable unnecessary services
# ============================================================================
log "10. Disable unnecessary services"

DISABLE_SERVICES=(
    accounts-daemon
    colord
    switcheroo-control
    power-profiles-daemon
    udisks2
    avahi-daemon
    cups cups-browsed
    ModemManager
    wpa_supplicant
    bluetooth
    snapd snapd.socket snapd.seeded
    fwupd
    packagekit
    unattended-upgrades
    apt-daily.timer
    apt-daily-upgrade.timer
    motd-news.timer
    man-db.timer
    e2scrub_all.timer
    fstrim.timer
)

for svc in "${DISABLE_SERVICES[@]}"; do
    if systemctl is-enabled "$svc" 2>/dev/null | grep -qE "enabled|static"; then
        systemctl disable --now "$svc" 2>/dev/null
        log "  Disabled $svc"
    fi
done

# ============================================================================
# 11. Disable PulseAudio / PipeWire (assistant uses ALSA directly)
# ============================================================================
log "11. Disable PulseAudio/PipeWire"

for svc in pulseaudio pipewire pipewire-pulse wireplumber; do
    systemctl --global disable "$svc.service" "$svc.socket" 2>/dev/null || true
done
killall pulseaudio 2>/dev/null || true

# System-wide autospawn disable
if [[ -f /etc/pulse/client.conf ]]; then
    grep -q "autospawn = no" /etc/pulse/client.conf 2>/dev/null || \
        echo "autospawn = no" >> /etc/pulse/client.conf
else
    mkdir -p /etc/pulse
    echo "autospawn = no" > /etc/pulse/client.conf
fi

# Per-user disable
mkdir -p "${ASSISTANT_HOME}/.config/pulse"
cat > "${ASSISTANT_HOME}/.config/pulse/client.conf" << 'PAEOF'
autospawn = no
PAEOF
chown -R "${ASSISTANT_USER}:${ASSISTANT_USER}" "${ASSISTANT_HOME}/.config"

su - "${ASSISTANT_USER}" -c "systemctl --user mask pulseaudio.service pulseaudio.socket 2>/dev/null" || true
loginctl enable-linger "${ASSISTANT_USER}" 2>/dev/null || warn "enable-linger failed"

log "  Audio daemons disabled, autospawn blocked"

# ============================================================================
# 12. Kernel / sysctl tuning
# ============================================================================
log "12. Kernel tuning"

cat > /etc/sysctl.d/90-assistant.conf << 'EOF'
# Reduce swap pressure — keep inference models in RAM
vm.swappiness = 10

# Reduce filesystem dirty page writebacks (less I/O contention)
vm.dirty_ratio = 20
vm.dirty_background_ratio = 5

# Increase inotify limits (Ollama model loading)
fs.inotify.max_user_watches = 65536

# Reduce kernel log verbosity
kernel.printk = 4 4 1 7
EOF

sysctl --system > /dev/null 2>&1
log "  Applied sysctl tuning"

# ============================================================================
# 13. CPU governor — performance
# ============================================================================
log "13. CPU governor"

if [[ -d /sys/devices/system/cpu/cpu0/cpufreq ]]; then
    for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        echo performance > "$cpu" 2>/dev/null || true
    done

    cat > /etc/systemd/system/cpu-performance.service << 'EOF'
[Unit]
Description=Set CPU governor to performance
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > "$g"; done'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable cpu-performance 2>/dev/null
    log "  CPU governor set to performance (persists across reboots)"
else
    warn "  cpufreq not available — skipping"
fi

# ============================================================================
# 14. Remove snap
# ============================================================================
log "14. Remove snap"

if command -v snap &>/dev/null; then
    snap list 2>/dev/null | tail -n+2 | awk '{print $1}' | while read -r pkg; do
        snap remove --purge "$pkg" 2>/dev/null || true
    done
    apt remove -y --purge snapd 2>/dev/null || true
    rm -rf /snap /var/snap /var/lib/snapd
    log "  Snap removed"
else
    log "  Snap not installed"
fi

# ============================================================================
# 15. Clean up unnecessary packages
# ============================================================================
log "15. Package cleanup"

REMOVE_PKGS=""
for pkg in xserver-xorg x11-common gnome-shell ubuntu-desktop firefox thunderbird libreoffice-core; do
    if dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
        REMOVE_PKGS="$REMOVE_PKGS $pkg"
    fi
done

if [[ -n "$REMOVE_PKGS" ]]; then
    log "  Desktop packages found:$REMOVE_PKGS"
    log "  Run manually to remove: apt remove -y --purge$REMOVE_PKGS && apt autoremove -y"
    log "  (Not auto-removing to avoid surprises — review first)"
else
    log "  No desktop packages to remove"
fi

apt autoremove -y 2>/dev/null || true
apt clean 2>/dev/null || true

# ============================================================================
# 16. RK3588S power: disable unused hardware
# ============================================================================
log "16. Disable unused RK3588S hardware (GPU, NPU, HDMI, video codecs)"

# USB runtime power management (skip audio devices — they must stay awake)
for dev in /sys/bus/usb/devices/*/power/control; do
    devpath=$(dirname "$dev")
    # Keep audio devices active (class 01 = audio)
    is_audio=false
    for iface in "$devpath"/*:*/bInterfaceClass; do
        if [[ -f "$iface" ]] && grep -q "01" "$iface" 2>/dev/null; then
            is_audio=true
            break
        fi
    done
    if [[ -f "$devpath/product" ]] && grep -qi "jabra\|audio\|sound\|speak" "$devpath/product" 2>/dev/null; then
        is_audio=true
    fi
    if $is_audio; then
        echo on > "$dev" 2>/dev/null || true
    else
        echo auto > "$dev" 2>/dev/null || true
    fi
done
log "  USB runtime power management: auto (audio devices excluded)"

# GPU (Mali G610 MP4) — lock to minimum frequency, unbind driver
for gpu in /sys/class/devfreq/*gpu*; do
    if [[ -f "$gpu/governor" ]]; then
        echo powersave > "$gpu/governor" 2>/dev/null && \
            log "  GPU devfreq governor: powersave"
    fi
    if [[ -f "$gpu/min_freq" && -f "$gpu/available_frequencies" ]]; then
        min_freq=$(awk '{print $1}' "$gpu/available_frequencies" 2>/dev/null)
        if [[ -n "$min_freq" ]]; then
            echo "$min_freq" > "$gpu/max_freq" 2>/dev/null
            echo "$min_freq" > "$gpu/min_freq" 2>/dev/null
            log "  GPU frequency locked to minimum: ${min_freq}Hz"
        fi
    fi
done

# Unbind unused platform drivers
_unbind_driver() {
    local drv_path="/sys/bus/platform/drivers/$1"
    local label="$2"
    if [[ -d "$drv_path" ]]; then
        for dev in "$drv_path"/*/; do
            local devname
            devname=$(basename "$dev")
            [[ "$devname" == "module" || "$devname" == "uevent" ]] && continue
            echo "$devname" > "$drv_path/unbind" 2>/dev/null && \
                log "  Unbound ${label}: $devname"
        done
    fi
}

_unbind_driver panfrost    "GPU"
_unbind_driver panthor     "GPU"
_unbind_driver rknpu       "NPU"
_unbind_driver rkvdec2     "video decoder"
_unbind_driver rkvenc      "video encoder"
_unbind_driver hantro-vpu  "video codec"
_unbind_driver rkisp       "camera ISP"
_unbind_driver dw-hdmi-qp-rockchip "HDMI"
_unbind_driver dwhdmi-rockchip     "HDMI"
_unbind_driver rockchip-vop2       "display controller"

# WiFi power save
iw dev 2>/dev/null | grep Interface | awk '{print $2}' | while read -r iface; do
    iw "$iface" set power_save on 2>/dev/null && \
        log "  WiFi power save enabled on $iface"
done

# Persist hardware power-down across reboots
cat > /etc/systemd/system/power-save-hw.service << 'PWREOF'
[Unit]
Description=Disable unused RK3588S hardware for power savings
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c '\
  for gpu in /sys/class/devfreq/*gpu*; do \
    [ -f "$gpu/governor" ] && echo powersave > "$gpu/governor"; \
    min=$(awk "{print \\$1}" "$gpu/available_frequencies" 2>/dev/null); \
    [ -n "$min" ] && echo "$min" > "$gpu/max_freq" && echo "$min" > "$gpu/min_freq"; \
  done; \
  for drv in panfrost panthor rknpu rkvdec2 rkvenc hantro-vpu rkisp \
             dw-hdmi-qp-rockchip dwhdmi-rockchip rockchip-vop2; do \
    d="/sys/bus/platform/drivers/$drv"; [ -d "$d" ] && \
    for dev in "$d"/*/; do n=$(basename "$dev"); \
      [ "$n" != module ] && [ "$n" != uevent ] && echo "$n" > "$d/unbind" 2>/dev/null; \
    done; \
  done; \
  for dev in /sys/bus/usb/devices/*/power/control; do \
    dp=$(dirname "$dev"); audio=false; \
    for ifc in "$dp"/*:*/bInterfaceClass; do \
      [ -f "$ifc" ] && grep -q 01 "$ifc" 2>/dev/null && audio=true && break; \
    done; \
    $audio && echo on > "$dev" 2>/dev/null || echo auto > "$dev" 2>/dev/null; \
  done; \
  true'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
PWREOF

systemctl daemon-reload
systemctl enable power-save-hw 2>/dev/null
log "  Hardware power-down service installed (persists across reboots)"

# ============================================================================
# Summary
# ============================================================================
echo ""
if [[ ${ERRORS} -eq 0 ]]; then
    log "============================================"
    log "  Setup complete! All steps passed."
    log "============================================"
else
    warn "============================================"
    warn "  Setup finished with ${ERRORS} error(s)."
    warn "  Review output above, fix issues, re-run."
    warn "============================================"
fi

echo ""
echo "Next steps:"
echo ""
echo "  1. Deploy assistant code from your dev machine:"
echo "     ./deploy.sh ${ASSISTANT_USER}@<board-ip>"
echo ""
echo "  2. Configure MQTT on the board:"
echo "     nano ${ASSISTANT_HOME}/assistant.env"
echo ""
echo "  3. Start the service:"
echo "     systemctl start voice-assistant"
echo "     journalctl -u voice-assistant -f"
echo ""
echo "  4. Reboot to apply all hardening changes:"
echo "     reboot"
echo ""
echo "Test commands:"
echo ""
echo "  # Test speaker"
echo "  speaker-test -D plughw:0,0 -t wav -c 2 -l 1"
echo ""
echo "  # Test microphone (record 5 sec, play back)"
echo "  arecord -D plughw:0,0 -d 5 -f S16_LE -r 16000 /tmp/test.wav && \\"
echo "    aplay -D plughw:0,0 /tmp/test.wav"
echo ""
echo "  # Test Piper TTS"
echo "  echo 'Hello, I am your voice assistant.' | \\"
echo "    ${VENV_DIR}/bin/piper --model ${PIPER_DIR}/${PIPER_VOICE}.onnx --output-raw | \\"
echo "    aplay -D plughw:0,0 -r 22050 -f S16_LE -c 1"
echo ""
echo "  # Test Ollama"
echo "  ollama run ${OLLAMA_MODEL} 'Say hello in one sentence'"
echo ""
