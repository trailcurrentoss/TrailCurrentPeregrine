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
# SHA256 hashes of known-good NPU model files.
# If these change, the model was updated — re-run with --force-cache to replace.
NPU_EXPECTED_HASHES=(
    "db6ba32ae2040cf25ca10c8b2fff5a79cad00e08a6382628e9ae4f2ee8bbac21  ${NPU_CACHE}/genie-t2t-run"
    "468972fb7949c1d8d71fe5b26a684c0d1745f6f45d93168a56c03702fe02ad1a  ${NPU_CACHE}/libGenie.so"
    "dfe35ce9624c4231779ae52cf2d66a0154a941fb477f8df1eadf1d9ea675eb9a  ${NPU_CACHE}/models/weight_sharing_model_1_of_1.serialized.bin"
)

PIPER_VOICE="en_US-libritts_r-medium"
PIPER_BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts_r/medium"
PIPER_CACHE="${CACHE_DIR}/piper-voice"
# SHA256 hashes of known-good Piper voice files.
PIPER_EXPECTED_HASHES=(
    "10bb85e071d616fcf4071f369f1799d0491492ab3c5d552ec19fb548fac13195  ${PIPER_CACHE}/en_US-libritts_r-medium.onnx"
    "b471dc60d2d8335e819c393d196d6fbf792817f40051257b269878505bc9afb3  ${PIPER_CACHE}/en_US-libritts_r-medium.onnx.json"
)

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

# Verify a list of "hash  path" entries. Sets HASH_OK=false and calls fail()
# for any mismatch. Prints ok() for each passing file.
verify_hashes() {
    local label="$1"; shift
    local entries=("$@")
    local all_ok=true
    for entry in "${entries[@]}"; do
        local expected_hash filepath
        read -r expected_hash filepath <<< "$entry"
        if [ ! -f "$filepath" ]; then
            fail "$label: missing file $(basename "$filepath")"
            all_ok=false
            continue
        fi
        local actual_hash
        actual_hash=$(sha256sum "$filepath" | cut -d' ' -f1)
        if [ "$actual_hash" = "$expected_hash" ]; then
            ok "$label: $(basename "$filepath") hash verified"
        else
            fail "$label: $(basename "$filepath") hash mismatch — re-run with --force-cache"
            all_ok=false
        fi
    done
    $all_ok
}

download_npu_model() {
    if ! command -v modelscope >/dev/null 2>&1; then
        warn "modelscope CLI not found — installing"
        if command -v pipx >/dev/null 2>&1; then
            pipx install modelscope || { fail "modelscope install failed"; return 1; }
        else
            pip3 install --user --break-system-packages modelscope || { fail "modelscope install failed"; return 1; }
        fi
    fi
    rm -rf "${NPU_CACHE}.tmp"
    mkdir -p "${NPU_CACHE}.tmp"
    if modelscope download --model "$NPU_MODEL_ID" --local "${NPU_CACHE}.tmp"; then
        rm -rf "$NPU_CACHE"
        mv "${NPU_CACHE}.tmp" "$NPU_CACHE"
        ok "NPU model downloaded ($(du -sh "$NPU_CACHE" | cut -f1))"
        return 0
    else
        fail "NPU model download failed"
        return 1
    fi
}

# Patch the upstream NPU config so QNN doesn't busy-poll 3 cores at idle.
# Upstream ships poll=true (lowest per-token latency) and perf_profile=burst
# (NPU always at max clock). On QCS6490 that caused cpu5/6/7 to sit at ~90%
# with the package hitting ~90 C even when no query was running.
# poll=false and perf_profile=balanced drop idle CPU load to ~0% and idle
# package temps to ~62 C with no measurable latency hit on small queries.
# Idempotent — safe to run every preflight check.
patch_npu_model_config() {
    local cfg="$NPU_CACHE/htp-model-config-llama32-1b-gqa.json"
    local ext="$NPU_CACHE/htp_backend_ext_config.json"
    [ -f "$cfg" ] || return 0
    [ -f "$ext" ] || return 0
    python3 - "$cfg" "$ext" <<'PYEOF' || { fail "NPU config patch failed"; return 1; }
import json, sys
cfg_path, ext_path = sys.argv[1], sys.argv[2]
changed = False
with open(cfg_path) as f: cfg = json.load(f)
qnn = cfg.get("dialog", {}).get("engine", {}).get("backend", {}).get("QnnHtp", {})
if qnn.get("poll") is not False:
    qnn["poll"] = False; changed = True
with open(ext_path) as f: ext = json.load(f)
for dev in ext.get("devices", []):
    for core in dev.get("cores", []):
        if core.get("perf_profile") != "balanced":
            core["perf_profile"] = "balanced"; changed = True
if changed:
    with open(cfg_path, "w") as f: json.dump(cfg, f, indent=4)
    with open(ext_path, "w") as f: json.dump(ext, f, indent=4)
    print("PATCHED")
else:
    print("ALREADY_PATCHED")
PYEOF
    ok "NPU config patched (poll=false, perf_profile=balanced)"
}

# 8a. NPU model
if $FORCE_CACHE; then
    warn "force-cache: removing NPU model cache for re-download"
    rm -rf "$NPU_CACHE"
fi

if [ ! -f "${NPU_CACHE}/genie-t2t-run" ] || [ ! -d "${NPU_CACHE}/models" ]; then
    if $DOWNLOAD_CACHE; then
        warn "NPU model cache missing — downloading"
        download_npu_model
    else
        fail "NPU model cache missing — re-run with --download-cache"
    fi
fi

if [ -f "${NPU_CACHE}/genie-t2t-run" ]; then
    if ! verify_hashes "NPU model" "${NPU_EXPECTED_HASHES[@]}"; then
        if $DOWNLOAD_CACHE; then
            warn "NPU model hash mismatch — re-downloading"
            rm -rf "$NPU_CACHE"
            download_npu_model
            verify_hashes "NPU model" "${NPU_EXPECTED_HASHES[@]}" || true
        fi
    fi
    patch_npu_model_config
fi

# 8b. Piper voice
mkdir -p "$PIPER_CACHE"
PIPER_ONNX="${PIPER_CACHE}/${PIPER_VOICE}.onnx"
PIPER_JSON="${PIPER_CACHE}/${PIPER_VOICE}.onnx.json"

if $FORCE_CACHE; then
    warn "force-cache: removing Piper voice cache for re-download"
    rm -f "$PIPER_ONNX" "$PIPER_JSON"
fi

if [ ! -f "$PIPER_ONNX" ] || [ ! -f "$PIPER_JSON" ]; then
    if $DOWNLOAD_CACHE; then
        warn "Piper voice missing — downloading"
        curl -fsSL --output "$PIPER_ONNX" "${PIPER_BASE_URL}/${PIPER_VOICE}.onnx" && \
        curl -fsSL --output "$PIPER_JSON" "${PIPER_BASE_URL}/${PIPER_VOICE}.onnx.json" && \
            ok "Piper voice downloaded" || fail "Piper voice download failed"
    else
        fail "Piper voice cache missing — re-run with --download-cache"
    fi
fi

if [ -f "$PIPER_ONNX" ]; then
    if ! verify_hashes "Piper voice" "${PIPER_EXPECTED_HASHES[@]}"; then
        if $DOWNLOAD_CACHE; then
            warn "Piper voice hash mismatch — re-downloading"
            rm -f "$PIPER_ONNX" "$PIPER_JSON"
            curl -fsSL --output "$PIPER_ONNX" "${PIPER_BASE_URL}/${PIPER_VOICE}.onnx" && \
            curl -fsSL --output "$PIPER_JSON" "${PIPER_BASE_URL}/${PIPER_VOICE}.onnx.json" && \
                ok "Piper voice re-downloaded" || fail "Piper voice re-download failed"
            verify_hashes "Piper voice" "${PIPER_EXPECTED_HASHES[@]}" || true
        fi
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
