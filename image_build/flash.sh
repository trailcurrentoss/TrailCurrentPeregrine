#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine — Flash helper
#
# Wraps edl-ng with safe defaults for the Radxa Dragon Q6A.
# Two operations:
#
#   --firmware              Flash SPI NOR firmware (one-time per board).
#   --os <image.img>        Flash the OS image to NVMe.
#
# Both require the board to be in EDL mode and connected via USB-C.
# Verify with: lsusb | grep 9008
#
# Usage:
#   sudo ./image_build/flash.sh --firmware
#   sudo ./image_build/flash.sh --os image_build/output/peregrine-q6a-v1.0.img
# ============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIRMWARE_DIR="${SCRIPT_DIR}/firmware"
WORK_DIR="/tmp/peregrine-flash"

GREEN='\033[38;5;70m'
TEAL='\033[38;5;30m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
RESET='\033[0m'

log()   { echo -e "${GREEN}[+]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[!]${RESET} $*"; }
err()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
fatal() { err "$*"; exit 1; }
section(){ echo ""; echo -e "${BOLD}${TEAL}── $* ──${RESET}"; }

# ── Parse args ──────────────────────────────────────────────────────────────
MODE=""
OS_IMAGE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --firmware) MODE="firmware"; shift ;;
        --os)       MODE="os"; OS_IMAGE="$2"; shift 2 ;;
        -h|--help)  sed -n '2,18p' "$0"; exit 0 ;;
        *)          fatal "Unknown option: $1" ;;
    esac
done

[ -n "$MODE" ] || fatal "Specify --firmware or --os <image>"
[ "$(id -u)" -eq 0 ] || fatal "flash.sh must be run as root (sudo)"

# ── Verify board in EDL mode ────────────────────────────────────────────────
section "EDL mode check"
if lsusb 2>/dev/null | grep -q "05c6:9008"; then
    log "Board detected in EDL mode (05c6:9008)"
else
    err "Board not in EDL mode. To enter EDL:"
    err "  1. Disconnect power from the Q6A"
    err "  2. Hold the EDL button"
    err "  3. Apply power (still holding EDL)"
    err "  4. Release after ~2 seconds"
    err "  5. Verify: lsusb | grep 9008"
    exit 1
fi

# ── Extract firmware archive (idempotent) ───────────────────────────────────
section "Extracting firmware archive"
mkdir -p "$WORK_DIR"

FW_ZIP="${FIRMWARE_DIR}/dragon-q6a_flat_build_wp_260120.zip"
EDL_ZIP="${FIRMWARE_DIR}/edl-ng-dist.zip"
[ -f "$FW_ZIP" ]  || fatal "Missing $FW_ZIP — run preflight to verify firmware/"
[ -f "$EDL_ZIP" ] || fatal "Missing $EDL_ZIP"

if [ ! -d "${WORK_DIR}/q6a-firmware/flat_build" ]; then
    log "Extracting firmware..."
    rm -rf "${WORK_DIR}/q6a-firmware"
    unzip -q "$FW_ZIP" -d "${WORK_DIR}/q6a-firmware"
fi

if [ ! -d "${WORK_DIR}/edl-ng" ]; then
    log "Extracting edl-ng..."
    rm -rf "${WORK_DIR}/edl-ng"
    mkdir -p "${WORK_DIR}/edl-ng"
    unzip -q "$EDL_ZIP" -d "${WORK_DIR}/edl-ng"
    # Handle double-wrapped zip: the outer archive may contain only an inner
    # edl-ng-dist.zip with the actual binaries. Extract the inner zip if present.
    INNER_ZIP=$(find "${WORK_DIR}/edl-ng" -maxdepth 1 -name '*.zip' -type f 2>/dev/null | head -1)
    if [ -n "$INNER_ZIP" ]; then
        log "  found nested zip — extracting inner archive..."
        unzip -qo "$INNER_ZIP" -d "${WORK_DIR}/edl-ng"
        rm -f "$INNER_ZIP"
    fi
fi

# Locate edl-ng binary (prefer linux-x64, fall back to linux-arm64)
EDL_NG=$(find "${WORK_DIR}/edl-ng" -path '*/linux-x64/edl-ng' -type f 2>/dev/null | head -1)
[ -n "$EDL_NG" ] || EDL_NG=$(find "${WORK_DIR}/edl-ng" -type f -name 'edl-ng' ! -name '*.exe' 2>/dev/null | head -1)
[ -n "$EDL_NG" ] || fatal "edl-ng binary not found inside edl-ng-dist.zip"
chmod +x "$EDL_NG"
log "edl-ng: $EDL_NG"

# Locate firehose loader
LOADER=$(find "${WORK_DIR}/q6a-firmware" -type f -name 'prog_firehose_ddr.elf' 2>/dev/null | head -1)
[ -n "$LOADER" ] || fatal "prog_firehose_ddr.elf not found inside firmware archive"
log "Loader: $LOADER"

# ── Mode dispatch ───────────────────────────────────────────────────────────
case "$MODE" in
    firmware)
        section "Flashing SPI NOR firmware"
        warn "This is a ONE-TIME operation per board. Skip if already flashed."
        warn "It writes the EDK2 UEFI bootloader and Qualcomm firmware components"
        warn "to the on-board SPI NOR flash. Without this, the board cannot boot."
        echo ""
        read -rp "  Continue? [y/N] " yn
        [[ "${yn,,}" == "y" || "${yn,,}" == "yes" ]] || exit 0

        SPINOR_DIR=$(dirname "$LOADER")
        log "Flashing from: $SPINOR_DIR"
        cd "$SPINOR_DIR"

        sudo "$EDL_NG" rawprogram "rawprogram*.xml" \
            --loader "$LOADER" \
            --memory SPINOR

        log "SPI NOR firmware flashed"
        echo ""
        echo "  Next: flash the OS image with:"
        echo "    sudo ./image_build/flash.sh --os <path/to/peregrine-q6a-vX.Y.img>"
        ;;

    os)
        section "Flashing OS image to NVMe"
        [ -f "$OS_IMAGE" ] || fatal "OS image not found: $OS_IMAGE"
        IMG_SIZE=$(du -h "$OS_IMAGE" | cut -f1)
        log "Image:  $OS_IMAGE ($IMG_SIZE)"

        echo ""
        warn "This will OVERWRITE the entire NVMe drive on the Q6A."
        echo ""
        read -rp "  Continue? [y/N] " yn
        [[ "${yn,,}" == "y" || "${yn,,}" == "yes" ]] || exit 0

        log "Writing image to NVMe sector 0 (this takes ~3-8 minutes)..."
        sudo "$EDL_NG" write-sector 0 "$OS_IMAGE" \
            --loader "$LOADER" \
            --memory NVME

        log "OS image flashed"
        echo ""
        echo "  Next:"
        echo "    1. Disconnect USB-C cable from the Q6A"
        echo "    2. Connect Ethernet"
        echo "    3. Apply 12V power"
        echo "    4. Wait ~3 minutes for first-boot service to complete"
        echo "    5. SSH:  ssh trailcurrent@peregrine.local  (password: trailcurrent)"
        echo ""
        ;;
esac
