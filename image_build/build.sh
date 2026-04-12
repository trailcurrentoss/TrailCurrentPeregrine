#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine — Image build orchestrator
#
# Builds a flashable Radxa Dragon Q6A image with the voice assistant
# fully baked in. Output: image_build/output/peregrine-q6a-vX.Y.img
#
# Must be run as root (mmdebstrap/bdebstrap require it for chroot setup).
#
# Usage:
#   sudo ./image_build/build.sh                  # full build (~30-50 min)
#   sudo ./image_build/build.sh --sector-size 512  # legacy sector size
#   sudo ./image_build/build.sh --debug           # rsdk debug mode
#
# After a successful build:
#   sudo ./image_build/flash.sh --firmware       # one-time SPI NOR
#   sudo ./image_build/flash.sh --os <image>     # NVMe OS
# ============================================================================

set -uo pipefail

PEREGRINE_VERSION="1.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
RSDK_DIR="${SCRIPT_DIR}/rsdk"
CACHE_DIR="${SCRIPT_DIR}/cache"
OUTPUT_DIR="${SCRIPT_DIR}/output"
STAGING_DIR="/tmp/peregrine-staging"

SECTOR_SIZE=512
DEBUG_FLAG=""

GREEN='\033[38;5;70m'
TEAL='\033[38;5;30m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
GRAY='\033[38;5;245m'
BOLD='\033[1m'
RESET='\033[0m'

log()    { echo -e "${GREEN}[+]${RESET} $*"; }
warn()   { echo -e "${YELLOW}[!]${RESET} $*"; }
err()    { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
fatal()  { err "$*"; exit 1; }
section(){ echo ""; echo -e "${BOLD}${TEAL}════ $* ════${RESET}"; echo ""; }

# ── Parse args ──────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --sector-size) SECTOR_SIZE="$2"; shift 2 ;;
        --debug)       DEBUG_FLAG="--debug"; shift ;;
        --version)     PEREGRINE_VERSION="$2"; shift 2 ;;
        -h|--help)     sed -n '2,18p' "$0"; exit 0 ;;
        *) fatal "Unknown option: $1" ;;
    esac
done

# ── Preflight ───────────────────────────────────────────────────────────────
section "Preflight"

[ "$(id -u)" -eq 0 ] || fatal "build.sh must be run as root (sudo)"

if ! "$SCRIPT_DIR/preflight.sh" >/dev/null 2>&1; then
    err "Preflight checks failed. Run for full output:"
    err "  ./image_build/preflight.sh"
    exit 1
fi
log "Preflight passed"

START_TIME=$SECONDS

# ── Stage files for the build ───────────────────────────────────────────────
section "Staging files for rsdk hooks"

rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"

log "Staging into $STAGING_DIR"
rsync -a "${PROJECT_DIR}/src/"     "${STAGING_DIR}/src/"
rsync -a "${PROJECT_DIR}/config/"  "${STAGING_DIR}/config/"
rsync -a "${PROJECT_DIR}/models/"  "${STAGING_DIR}/models/"
rsync -a "${SCRIPT_DIR}/files/"    "${STAGING_DIR}/files/"
rsync -a "${SCRIPT_DIR}/cache/"    "${STAGING_DIR}/cache/"

# Strip __pycache__ from staged src
find "${STAGING_DIR}/src" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

STAGE_SIZE=$(du -sh "$STAGING_DIR" | cut -f1)
log "Staged $STAGE_SIZE total"

export PEREGRINE_STAGING="$STAGING_DIR"
export PEREGRINE_VERSION="$PEREGRINE_VERSION"

# ── Run rsdk-build ──────────────────────────────────────────────────────────
section "Building rootfs and image (rsdk)"

log "Product:    radxa-dragon-q6a"
log "Suite:      noble (Ubuntu 24.04)"
log "Edition:    cli (minimal, no desktop)"
log "Sector:     ${SECTOR_SIZE}"
log "Version:    ${PEREGRINE_VERSION}"
log ""
log "This step takes ~30-50 minutes on the first build."
log "The venv smoke test (hook 10) and final checkpoint (hook 29) are"
log "fail-fast points — watch the journal for those checkpoint messages."
log ""

cd "$RSDK_DIR"

# Always regenerate the guestfish disk-assembly script. rsdk caches this
# separately from rootfs.tar and does NOT regenerate it when --sector-size
# changes. Stale build-image = wrong GPT sector size = unbootable image.
rm -f "$RSDK_DIR/out/radxa-dragon-q6a_noble_cli/build-image"

if ! "$RSDK_DIR/src/libexec/rsdk/rsdk-build" \
        $DEBUG_FLAG \
        --sector-size "$SECTOR_SIZE" \
        radxa-dragon-q6a \
        noble \
        cli; then
    err "rsdk-build failed — see output above for the failing hook"
    err "Common failure points:"
    err "  hook 8  — pip install (check network/PyPI)"
    err "  hook 10 — venv smoke test (a Python dep is broken)"
    err "  hook 29 — final artifact verification"
    exit 1
fi

# ── Post-build: move output ─────────────────────────────────────────────────
section "Post-build"

RSDK_OUT="${RSDK_DIR}/out/radxa-dragon-q6a_noble_cli/output.img"
if [ ! -f "$RSDK_OUT" ]; then
    fatal "rsdk reported success but $RSDK_OUT does not exist"
fi

mkdir -p "$OUTPUT_DIR"
FINAL_IMG="${OUTPUT_DIR}/peregrine-q6a-v${PEREGRINE_VERSION}.img"

cp --reflink=auto "$RSDK_OUT" "$FINAL_IMG"

IMG_SIZE=$(du -h "$FINAL_IMG" | cut -f1)
SHA=$(sha256sum "$FINAL_IMG" | cut -d' ' -f1)

# Cleanup staging
rm -rf "$STAGING_DIR"

ELAPSED=$((SECONDS - START_TIME))
ELAPSED_MIN=$((ELAPSED / 60))
ELAPSED_SEC=$((ELAPSED % 60))

# ── Done ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  Build complete in ${ELAPSED_MIN}m ${ELAPSED_SEC}s${RESET}"
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════════${RESET}"
echo ""
echo "  Image:   $FINAL_IMG"
echo "  Size:    $IMG_SIZE"
echo "  SHA256:  $SHA"
echo ""
echo "  Next steps:"
echo ""
echo "    1. Put the board in EDL mode (hold EDL button while powering on)"
echo "    2. Verify with: lsusb | grep 9008"
echo "    3. Flash SPI NOR firmware (one-time per board):"
echo "         sudo ./image_build/flash.sh --firmware"
echo "    4. Flash the OS image to NVMe:"
echo "         sudo ./image_build/flash.sh --os $FINAL_IMG"
echo "    5. Connect Ethernet, power on, wait ~3 min for first boot"
echo "    6. SSH:"
echo "         ssh trailcurrent@peregrine.local"
echo "         (default password: trailcurrent — first-login wizard will force a change)"
echo ""
