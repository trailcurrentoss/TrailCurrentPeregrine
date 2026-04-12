#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine — Build host preflight
#
# Verifies the build host has everything needed to run build.sh, clones
# the rsdk keyring repos, and (with --download-cache) downloads the NPU
# LLM model and Piper TTS voice into image_build/cache/.
#
# Idempotent — safe to re-run any time.
#
# Usage:
#   ./image_build/preflight.sh                 # check only
#   ./image_build/preflight.sh --download-cache  # check + download cache
#   ./image_build/preflight.sh --force-cache     # re-download cache
# ============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
RSDK_DIR="${SCRIPT_DIR}/rsdk"
KEYRINGS_DIR="${RSDK_DIR}/externals/keyrings"
CACHE_DIR="${SCRIPT_DIR}/cache"
FIRMWARE_DIR="${SCRIPT_DIR}/firmware"

NPU_MODEL_ID="radxa/Llama3.2-1B-1024-qairt-v68"
NPU_CACHE="${CACHE_DIR}/npu-model"

PIPER_VOICE="en_US-libritts_r-medium"
PIPER_BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts_r/medium"
PIPER_CACHE="${CACHE_DIR}/piper-voice"

GREEN='\033[38;5;70m'
TEAL='\033[38;5;30m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
GRAY='\033[38;5;245m'
BOLD='\033[1m'
RESET='\033[0m'

ok()    { echo -e "  ${GREEN}\xE2\x9C\x93${RESET} $*"; }
fail()  { echo -e "  ${RED}\xE2\x9C\x97${RESET} $*"; ERRORS=$((ERRORS+1)); }
warn()  { echo -e "  ${YELLOW}!${RESET} $*"; }
step()  { echo ""; echo -e "${BOLD}${TEAL}── ${1} ──${RESET}"; }

DOWNLOAD_CACHE=false
FORCE_CACHE=false
for arg in "$@"; do
    case "$arg" in
        --download-cache) DOWNLOAD_CACHE=true ;;
        --force-cache)    DOWNLOAD_CACHE=true; FORCE_CACHE=true ;;
        -h|--help)
            sed -n '2,16p' "$0"
            exit 0
            ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

ERRORS=0

echo ""
echo -e "${BOLD}${GREEN}Trail${TEAL}Current${RESET} ${BOLD}Peregrine — Build Host Preflight${RESET}"
echo ""

# ── 1. APT build dependencies ───────────────────────────────────────────────
step "1. APT build dependencies"

REQUIRED_TOOLS=(jsonnet bdebstrap guestfish qemu-aarch64-static sgdisk parted git curl gpg dtc)
MISSING_PKGS=()

for tool in "${REQUIRED_TOOLS[@]}"; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool"
    else
        fail "$tool — missing"
        case "$tool" in
            jsonnet)            MISSING_PKGS+=(jsonnet) ;;
            bdebstrap)          MISSING_PKGS+=(bdebstrap) ;;
            guestfish)          MISSING_PKGS+=(libguestfs-tools) ;;
            qemu-aarch64-static) MISSING_PKGS+=(qemu-user-static binfmt-support) ;;
            sgdisk)             MISSING_PKGS+=(gdisk) ;;
            parted)             MISSING_PKGS+=(parted) ;;
            git)                MISSING_PKGS+=(git) ;;
            curl)               MISSING_PKGS+=(curl) ;;
            gpg)                MISSING_PKGS+=(gpg) ;;
            dtc)                MISSING_PKGS+=(device-tree-compiler) ;;
        esac
    fi
done

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    UNIQ_PKGS=$(printf '%s\n' "${MISSING_PKGS[@]}" | sort -u | tr '\n' ' ')
    echo ""
    echo "  To install missing dependencies:"
    echo "    sudo apt install -y $UNIQ_PKGS"
fi

# ── 2. QEMU binfmt for arm64 ────────────────────────────────────────────────
step "2. QEMU arm64 binfmt"

if [ -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
    ok "qemu-aarch64 binfmt registered"
else
    fail "qemu-aarch64 binfmt not registered"
    echo "  Try: sudo systemctl restart binfmt-support"
fi

# ── 3. rsdk keyrings ────────────────────────────────────────────────────────
step "3. rsdk keyring repos"

mkdir -p "$KEYRINGS_DIR"

clone_or_skip() {
    local name="$1"
    local url="$2"
    local target="${KEYRINGS_DIR}/${name}"

    if [ -d "$target/.git" ] || [ -f "$target/Makefile" ]; then
        ok "${name} keyring (already cloned)"
    else
        echo "  cloning ${name}..."
        rm -rf "$target"
        if git clone --depth=1 --quiet "$url" "$target"; then
            ok "${name} keyring"
        else
            fail "${name} keyring (clone failed)"
        fi
    fi
}

clone_or_skip debian   https://salsa.debian.org/release-team/debian-archive-keyring.git
clone_or_skip ubuntu   https://git.launchpad.net/ubuntu/+source/ubuntu-keyring
clone_or_skip radxa    https://github.com/radxa-pkg/radxa-archive-keyring.git
clone_or_skip vscodium https://gitlab.com/paulcarroty/vscodium-deb-rpm-repo.git

# ── 4. Wake-word model ──────────────────────────────────────────────────────
step "4. hey_peregrine wake-word model"

if [ -f "${PROJECT_DIR}/models/hey_peregrine.onnx" ]; then
    ok "models/hey_peregrine.onnx ($(du -h "${PROJECT_DIR}/models/hey_peregrine.onnx" | cut -f1))"
    if [ -f "${PROJECT_DIR}/models/hey_peregrine.onnx.data" ]; then
        ok "models/hey_peregrine.onnx.data"
    fi
else
    fail "models/hey_peregrine.onnx missing — train one or copy from a backup"
fi

# ── 5. Application source ──────────────────────────────────────────────────
step "5. Application source"

for f in src/assistant.py src/genie_server.py; do
    if [ -f "${PROJECT_DIR}/${f}" ]; then
        ok "$f"
    else
        fail "$f missing"
    fi
done

# ── 6. Service files ────────────────────────────────────────────────────────
step "6. Service definitions"

for f in config/voice-assistant.service config/genie-server.service; do
    if [ -f "${PROJECT_DIR}/${f}" ]; then
        if grep -q "User=trailcurrent" "${PROJECT_DIR}/${f}"; then
            ok "$f (User=trailcurrent)"
        else
            warn "$f does not specify User=trailcurrent"
        fi
    else
        fail "$f missing"
    fi
done

# ── 7. SPI NOR firmware files ───────────────────────────────────────────────
step "7. SPI NOR firmware (for first-time board flashing)"

for f in dragon-q6a_flat_build_wp_260120.zip edl-ng-dist.zip; do
    if [ -f "${FIRMWARE_DIR}/${f}" ]; then
        ok "firmware/${f} ($(du -h "${FIRMWARE_DIR}/${f}" | cut -f1))"
    else
        fail "firmware/${f} missing"
    fi
done

# ── 8. Cache (NPU model + Piper voice) ──────────────────────────────────────
step "8. Build cache"

mkdir -p "$CACHE_DIR"

# 8a. NPU model
NPU_OK=false
if [ -f "${NPU_CACHE}/genie-t2t-run" ] && [ -d "${NPU_CACHE}/models" ]; then
    SIZE=$(du -sh "$NPU_CACHE" 2>/dev/null | cut -f1)
    ok "NPU model cache present (${SIZE})"
    NPU_OK=true
else
    if $DOWNLOAD_CACHE; then
        warn "NPU model cache missing — downloading via modelscope"
        if ! command -v modelscope >/dev/null 2>&1; then
            warn "modelscope CLI not found — installing in user pipx env"
            if command -v pipx >/dev/null 2>&1; then
                pipx install modelscope || fail "modelscope install failed"
            else
                pip3 install --user --break-system-packages modelscope || fail "modelscope install failed"
            fi
        fi
        rm -rf "${NPU_CACHE}.tmp"
        mkdir -p "${NPU_CACHE}.tmp"
        if modelscope download --model "$NPU_MODEL_ID" --local "${NPU_CACHE}.tmp"; then
            rm -rf "$NPU_CACHE"
            mv "${NPU_CACHE}.tmp" "$NPU_CACHE"
            ok "NPU model downloaded ($(du -sh "$NPU_CACHE" | cut -f1))"
            NPU_OK=true
        else
            fail "NPU model download failed"
        fi
    else
        fail "NPU model cache missing — re-run with --download-cache"
    fi
fi

if $FORCE_CACHE && $NPU_OK; then
    warn "force-cache requested — re-downloading NPU model"
    rm -rf "$NPU_CACHE"
    NPU_OK=false
fi

# 8b. Piper voice
mkdir -p "$PIPER_CACHE"
PIPER_ONNX="${PIPER_CACHE}/${PIPER_VOICE}.onnx"
PIPER_JSON="${PIPER_CACHE}/${PIPER_VOICE}.onnx.json"

if [ -f "$PIPER_ONNX" ] && [ -f "$PIPER_JSON" ]; then
    ok "Piper voice cached ($(du -h "$PIPER_ONNX" | cut -f1))"
else
    if $DOWNLOAD_CACHE; then
        warn "Piper voice missing — downloading"
        curl -fsSL --output "$PIPER_ONNX" "${PIPER_BASE_URL}/${PIPER_VOICE}.onnx" && \
        curl -fsSL --output "$PIPER_JSON" "${PIPER_BASE_URL}/${PIPER_VOICE}.onnx.json" && \
            ok "Piper voice downloaded" || fail "Piper voice download failed"
    else
        fail "Piper voice cache missing — re-run with --download-cache"
    fi
fi

# ── Summary ─────────────────────────────────────────────────────────────────
echo ""
if [ "$ERRORS" -eq 0 ]; then
    echo -e "${BOLD}${GREEN}Preflight passed${RESET} — ready to run ${BOLD}sudo ./image_build/build.sh${RESET}"
    echo ""
    exit 0
else
    echo -e "${BOLD}${RED}Preflight failed${RESET} with ${ERRORS} error(s) — fix the issues above and re-run."
    echo ""
    exit 1
fi
