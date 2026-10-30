#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine (ZimaBoard 2) — build the installer ISO
#
#   ./zimaboard2/image/build.sh [--version X.Y] [--reuse-rootfs]
#
# Output: zimaboard2/image/output/peregrine-zimaboard2-<version>.iso
#
# Write the ISO to a USB stick (8 GB or larger), boot the ZimaBoard from it,
# and the installer copies Peregrine onto the internal eMMC. See
# zimaboard2/docs/install.md.
#
# Runs as your normal user. The privileged part (mmdebstrap, chroots) runs as
# root inside a pinned ubuntu:24.04 container, so the build host needs only
# Docker and needs no other packages installed.
# ============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# shellcheck source=versions.env
source "$HERE/versions.env"

VERSION="${PEREGRINE_VERSION:-dev}"
REUSE_ROOTFS=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        # Development shortcut: rebuild only the ISO around the last rootfs.
        --reuse-rootfs) REUSE_ROOTFS=1; shift ;;
        -h|--help) sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

[[ "$(uname -m)" == x86_64 ]] || { echo "build.sh refuses to run on a non-x86_64 host" >&2; exit 1; }

"$HERE/preflight.sh"

# Work dirs (rootfs, live chroot) hold device nodes, so they live in a Docker
# volume rather than on the repo's filesystem, which may be mounted nodev.
docker volume create peregrine-zb2-work >/dev/null

echo "[build] starting builder container ($BUILDER_IMAGE)"
docker run --rm --privileged \
    -v "$REPO":/work \
    -v peregrine-zb2-work:/build \
    -e PEREGRINE_VERSION="$VERSION" \
    -e PEREGRINE_REUSE_ROOTFS="$REUSE_ROOTFS" \
    -e PEREGRINE_BUILD_HOST="$(hostname)" \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    "$BUILDER_IMAGE" \
    bash /work/zimaboard2/image/build-in-container.sh

echo ""
echo "[build] done:"
ls -lh "$HERE/output/"*.iso
