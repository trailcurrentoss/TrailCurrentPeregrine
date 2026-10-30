#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine (ZimaBoard 2) — build the installed root filesystem
#
# Runs inside the builder container. mmdebstrap builds Ubuntu Noble amd64
# into /build/rootfs, runs hooks/NN-*.sh against it (one customize-hook each,
# so a failure names the hook), then packs it into /build/rootfs.tar.zst for
# the installer. The LLM model is NOT in the rootfs: it ships next to it on
# the ISO and the installer copies it, which keeps the tarball small.
# ============================================================================

set -euo pipefail

HERE=/work/zimaboard2/image
REPO=/work
# shellcheck source=versions.env
source "$HERE/versions.env"

ROOTFS=/build/rootfs
STAGING=/build/staging
export PEREGRINE_STAGING="$STAGING"
export PEREGRINE_VERSION="${PEREGRINE_VERSION:-dev}"
export PEREGRINE_BUILD_HOST="${PEREGRINE_BUILD_HOST:-builder}"
export LLAMA_TAG LLM_MODEL_FILE LLM_MODEL_NAME LLM_MODEL_LICENSE WHISPER_REPO WHISPER_REVISION PIPER_VOICE

log() { echo "[build-rootfs] $*"; }

# ── Stage everything the hooks copy in ──────────────────────────────────────
log "staging files"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -a "$REPO/src" "$STAGING/src"
cp -a "$REPO/models" "$STAGING/models"
cp -a "$REPO/scripts" "$STAGING/scripts"
cp -a "$REPO/image_build/files" "$STAGING/q6a-files"     # shared, unchanged files
cp -a "$REPO/zimaboard2/src" "$STAGING/zb2-src"
cp -a "$REPO/zimaboard2/config" "$STAGING/config"
cp -a "$REPO/zimaboard2/tools" "$STAGING/tools"
cp -a "$HERE/files" "$STAGING/files"
cp -a "$HERE/cache/piper-voice" "$STAGING/piper-voice"
cp -a "$HERE/cache/llama.cpp/$LLAMA_TAG" "$STAGING/llama.cpp"

# ── Package set ─────────────────────────────────────────────────────────────
PKGS=(
    # base system
    systemd-sysv systemd-resolved systemd-timesyncd udev dbus kmod
    iproute2 iputils-ping netplan.io ca-certificates curl wget gnupg
    lsb-release sudo openssh-server avahi-daemon avahi-utils libnss-mdns
    rfkill cloud-guest-utils parted gdisk htop nano less bash-completion
    pciutils usbutils ethtool openssl tzdata console-setup keyboard-configuration mawk
    # kernel: HWE for Alder Lake-N / Twin Lake i915 support (plan risk 6.1)
    linux-image-generic-hwe-24.04 linux-firmware intel-microcode
    initramfs-tools zstd
    # UEFI boot (Secure Boot capable)
    grub-efi-amd64 grub-efi-amd64-signed shim-signed efibootmgr
    # iGPU / llama.cpp runtime
    mesa-vulkan-drivers libvulkan1 vulkan-tools libgomp1
    # power measurement (turbostat) for plan steps 3 and 7
    linux-tools-generic-hwe-24.04
    # Python runtime
    python3 python3-venv python3-pip
    # audio (ALSA only)
    alsa-utils
    # boot splash
    plymouth plymouth-themes
)

HOOK_ARGS=()
for h in "$HERE"/hooks/[0-9][0-9]-*.sh; do
    HOOK_ARGS+=(--customize-hook="bash $h \"\$1\"")
done

log "mmdebstrap ${SUITE} amd64 -> $ROOTFS"
rm -rf "$ROOTFS"
mmdebstrap \
    --mode=root \
    --variant=apt \
    --architectures=amd64 \
    --components=main,restricted,universe,multiverse \
    --aptopt='APT::Install-Recommends "false"' \
    --dpkgopt='path-exclude=/usr/share/doc/*' \
    --dpkgopt='path-include=/usr/share/doc/*/copyright' \
    --dpkgopt='path-exclude=/usr/share/man/*' \
    --include="$(IFS=,; echo "${PKGS[*]}")" \
    "${HOOK_ARGS[@]}" \
    "$SUITE" "$ROOTFS" \
    "deb $UBUNTU_MIRROR $SUITE main restricted universe multiverse" \
    "deb $UBUNTU_MIRROR $SUITE-updates main restricted universe multiverse" \
    "deb http://security.ubuntu.com/ubuntu $SUITE-security main restricted universe multiverse"

log "packing rootfs.tar.zst ($(du -sh "$ROOTFS" | cut -f1) unpacked)"
tar --numeric-owner --xattrs --xattrs-include='*' --acls \
    -C "$ROOTFS" -cf - . | zstd -q -T0 -12 -o /build/rootfs.tar.zst.part -f
mv /build/rootfs.tar.zst.part /build/rootfs.tar.zst
log "done — $(du -h /build/rootfs.tar.zst | cut -f1)"
