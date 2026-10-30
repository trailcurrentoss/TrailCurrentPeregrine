#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine (ZimaBoard 2) — static kexec for network installs
#
# Runs INSIDE the builder container. netinstall.sh copies this binary to the
# board's current OS (e.g. ZimaOS) and uses it to jump into the installer
# kernel. Static, so it runs whatever libc the current OS has. Cached.
# ============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=versions.env
source "$HERE/versions.env"
DEST="$HERE/cache/kexec/kexec-$KEXEC_VERSION-static"

log() { echo "[build-kexec] $*"; }

if [[ -x "$DEST" ]]; then
    log "kexec $KEXEC_VERSION already built"
    exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq --no-install-recommends build-essential curl xz-utils ca-certificates >/dev/null

SRC="$(mktemp -d)"
trap 'rm -rf "$SRC"' EXIT
curl -fsSL -o "$SRC/k.tar.xz" "$KEXEC_URL"
echo "$KEXEC_SHA256  $SRC/k.tar.xz" | sha256sum -c --quiet
tar -xJf "$SRC/k.tar.xz" -C "$SRC"
cd "$SRC/kexec-tools-$KEXEC_VERSION"
log "building kexec-tools $KEXEC_VERSION (static)"
./configure --quiet --without-zlib --without-lzma --without-xen LDFLAGS=-static >/dev/null
make -s -j "$(nproc)" build/sbin/kexec >/dev/null
mkdir -p "$(dirname "$DEST")"
install -m 755 build/sbin/kexec "$DEST"
strip "$DEST"
file "$DEST" 2>/dev/null || true
log "done — $DEST"
