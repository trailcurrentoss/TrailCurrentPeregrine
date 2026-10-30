#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine (ZimaBoard 2) — builder container entry point
#
# Started by build.sh with the repo at /work and a work volume at /build.
#   1. build-llama.sh   llama.cpp (cached by tag)
#   2. build-rootfs.sh  the installed system  -> /build/rootfs.tar.zst
#   3. build-iso.sh     the live installer    -> output/*.iso
# ============================================================================

set -euo pipefail

HERE=/work/zimaboard2/image
export PEREGRINE_IN_BUILDER=1

cleanup() {
    # Hand everything the container wrote under the repo back to the caller.
    if [[ -n "${HOST_UID:-}" ]]; then
        chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" "$HERE/cache" "$HERE/output" 2>/dev/null || true
    fi
}
trap cleanup EXIT

echo "[container] installing build tools"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    mmdebstrap ca-certificates zstd xorriso squashfs-tools mtools dosfstools \
    python3 >/dev/null

bash "$HERE/build-llama.sh"
bash "$HERE/build-kexec.sh"
if [[ "${PEREGRINE_REUSE_ROOTFS:-0}" == 1 && -f /build/rootfs.tar.zst ]]; then
    echo "[container] reusing /build/rootfs.tar.zst from the previous build"
else
    bash "$HERE/build-rootfs.sh"
fi
bash "$HERE/build-iso.sh"
