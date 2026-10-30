#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine (ZimaBoard 2) — build the installer ISO
#
# Runs inside the builder container after build-rootfs.sh. Builds a small
# live Ubuntu (casper) whose only job is peregrine-install on tty1, then lays
# out a UEFI-bootable hybrid ISO (works burned or dd'ed to a USB stick):
#
#   EFI/boot/bootx64.efi          shim (Microsoft-signed; Secure Boot OK)
#   EFI/boot/grubx64.efi          Canonical-signed GRUB (CD variant)
#   boot/grub/grub.cfg            install menu
#   casper/vmlinuz, initrd        live kernel
#   casper/filesystem.squashfs    live installer system
#   peregrine/rootfs.tar.zst      the system to install
#   peregrine/models/*.gguf       the LLM, copied as-is
#   peregrine/SHA256SUMS          checked by the installer before erasing
# ============================================================================

set -euo pipefail

HERE=/work/zimaboard2/image
# shellcheck source=versions.env
source "$HERE/versions.env"

VERSION="${PEREGRINE_VERSION:-dev}"
LIVE=/build/live
ISO=/build/iso
OUT="$HERE/output"
NAME="peregrine-zimaboard2-$VERSION"

log() { echo "[build-iso] $*"; }

[[ -f /build/rootfs.tar.zst ]] || { echo "run build-rootfs.sh first" >&2; exit 1; }

# ── Live installer system ───────────────────────────────────────────────────
LIVE_PKGS=(
    systemd-sysv udev dbus kmod util-linux casper initramfs-tools
    linux-image-generic-hwe-24.04
    gdisk dosfstools e2fsprogs zstd pv parted kbd less mawk curl
    shim-signed grub-efi-amd64-signed
)

log "mmdebstrap live installer -> $LIVE"
rm -rf "$LIVE"
mmdebstrap \
    --mode=root \
    --variant=apt \
    --architectures=amd64 \
    --components=main,restricted,universe \
    --aptopt='APT::Install-Recommends "false"' \
    --dpkgopt='path-exclude=/usr/share/doc/*' \
    --dpkgopt='path-exclude=/usr/share/man/*' \
    --dpkgopt='path-exclude=/usr/share/locale/*' \
    --include="$(IFS=,; echo "${LIVE_PKGS[*]}")" \
    --customize-hook='echo peregrine-installer > "$1/etc/hostname"' \
    --customize-hook="install -m 755 $HERE/installer/peregrine-install.sh \"\$1/usr/local/sbin/peregrine-install\"" \
    --customize-hook="install -m 644 $HERE/installer/peregrine-installer.service \"\$1/etc/systemd/system/peregrine-installer.service\"" \
    --customize-hook='chroot "$1" systemctl enable peregrine-installer.service' \
    --customize-hook='chroot "$1" systemctl mask getty@tty1.service serial-getty@ttyS0.service casper-md5check.service' \
    --customize-hook='printf "[Match]\nName=en*\n\n[Network]\nDHCP=yes\n" > "$1/etc/systemd/network/20-wired.network"' \
    --customize-hook='chroot "$1" systemctl enable systemd-networkd.service' \
    --customize-hook='printf "export USERNAME=installer\nexport USERFULLNAME=\"Peregrine installer\"\nexport HOST=peregrine-installer\nexport BUILD_SYSTEM=Ubuntu\n" > "$1/etc/casper.conf"' \
    --customize-hook='find "$1/usr/lib/firmware" -mindepth 1 -maxdepth 1 ! -name i915 -exec rm -rf {} +' \
    --customize-hook='chroot "$1" update-initramfs -u -k all' \
    --customize-hook='chroot "$1" apt-get clean; rm -rf "$1"/var/lib/apt/lists/*; : > "$1/etc/machine-id"' \
    "$SUITE" "$LIVE" \
    "deb $UBUNTU_MIRROR $SUITE main restricted universe" \
    "deb $UBUNTU_MIRROR $SUITE-updates main restricted universe" \
    "deb http://security.ubuntu.com/ubuntu $SUITE-security main restricted universe"

# ── ISO tree ────────────────────────────────────────────────────────────────
log "assembling ISO tree"
rm -rf "$ISO"
mkdir -p "$ISO"/{casper,boot/grub,EFI/boot,.disk,peregrine/models}

KVER=$(basename "$(ls -1 "$LIVE"/boot/vmlinuz-* | sort -V | tail -1)" | sed 's/^vmlinuz-//')
cp "$LIVE/boot/vmlinuz-$KVER" "$ISO/casper/vmlinuz"
cp "$LIVE/boot/initrd.img-$KVER" "$ISO/casper/initrd"
log "live kernel $KVER"

cp "$LIVE/usr/lib/shim/shimx64.efi.signed.latest" "$ISO/EFI/boot/bootx64.efi"
cp "$LIVE/usr/lib/shim/mmx64.efi" "$ISO/EFI/boot/mmx64.efi"
cp "$LIVE/usr/lib/grub/x86_64-efi-signed/gcdx64.efi.signed" "$ISO/EFI/boot/grubx64.efi"

# The signed CD GRUB finds the ISO root by /.disk/info, then reads
# /boot/grub/grub.cfg.
echo "TrailCurrent Peregrine $VERSION ZimaBoard 2 installer" > "$ISO/.disk/info"

cat > "$ISO/boot/grub/grub.cfg" <<'EOF'
set timeout=15
set default=0

menuentry "Install TrailCurrent Peregrine  (asks before erasing anything)" {
    linux  /casper/vmlinuz boot=casper noprompt fsck.mode=skip quiet loglevel=3 systemd.show_status=false console=tty1 ---
    initrd /casper/initrd
}
menuentry "Install unattended  (erases the internal eMMC without asking, then powers off)" {
    linux  /casper/vmlinuz boot=casper noprompt fsck.mode=skip quiet loglevel=3 systemd.show_status=false peregrine.unattended=1 console=tty1 ---
    initrd /casper/initrd
}
menuentry "UEFI firmware settings" {
    fwsetup
}
EOF

log "squashing live system"
mksquashfs "$LIVE" "$ISO/casper/filesystem.squashfs" -noappend -comp xz -quiet \
    -wildcards -e 'boot/vmlinuz*' -e 'boot/initrd*' -e 'proc/*' -e 'sys/*' -e 'dev/*' -e 'run/*' -e 'tmp/*'
du -sx --block-size=1 "$LIVE" | cut -f1 > "$ISO/casper/filesystem.size"

log "adding payload"
cp /build/rootfs.tar.zst "$ISO/peregrine/rootfs.tar.zst"
cp "$HERE/cache/llm/$LLM_MODEL_FILE" "$ISO/peregrine/models/$LLM_MODEL_FILE"
{
    echo "PEREGRINE_VERSION=\"$VERSION\""
    echo "PEREGRINE_BUILD_DATE=\"$(date -u +%Y-%m-%dT%H:%MZ)\""
    echo "PEREGRINE_LLM_MODEL=\"$LLM_MODEL_NAME\""
} > "$ISO/peregrine/release"
( cd "$ISO/peregrine" && sha256sum rootfs.tar.zst models/* > SHA256SUMS )

# ── EFI system partition image ──────────────────────────────────────────────
EFIIMG=/build/efi.img
rm -f "$EFIIMG"
mkfs.vfat -C -n ZB2_EFI "$EFIIMG" 8192 >/dev/null
mmd -i "$EFIIMG" ::/EFI ::/EFI/boot
mcopy -i "$EFIIMG" "$ISO/EFI/boot/bootx64.efi" "$ISO/EFI/boot/grubx64.efi" \
    "$ISO/EFI/boot/mmx64.efi" ::/EFI/boot/

# ── Hybrid ISO: GPT with the ESP appended, El Torito EFI pointing at it ─────
mkiso() {   # mkiso TREE OUTFILE
    xorriso -as mkisofs \
        -r -J -joliet-long -iso-level 3 \
        -V PEREGRINE_ZB2 \
        -o "$2.part" \
        -partition_offset 16 \
        -append_partition 2 C12A7328-F81F-11D2-BA4B-00A0C93EC93B "$EFIIMG" \
        -appended_part_as_gpt \
        -e --interval:appended_partition_2:all:: -no-emul-boot \
        "$1" 2>&1 | grep -v '^xorriso : UPDATE' | tail -n 15
    mv "$2.part" "$2"
}

log "writing $NAME.iso"
mkdir -p "$OUT"
mkiso "$ISO" "$OUT/$NAME.iso"
( cd "$OUT" && sha256sum "$NAME.iso" > "$NAME.iso.sha256" )
log "done — $(du -h "$OUT/$NAME.iso" | cut -f1)  $OUT/$NAME.iso"

# ── Network install bundle (served by netinstall.sh) ────────────────────────
# casper's url= netboot downloads a whole ISO into RAM, so the netboot ISO is
# the live system alone; the installer fetches the payload separately.
NET="$OUT/$NAME-netinstall"
log "writing network-install bundle $NET"
rm -rf "$NET" /build/iso-net
mkdir -p "$NET" /build/iso-net
cp -a "$ISO/casper" "$ISO/boot" "$ISO/EFI" "$ISO/.disk" /build/iso-net/
mkiso /build/iso-net "$NET/netboot.iso"
cp "$ISO/casper/vmlinuz" "$ISO/casper/initrd" "$NET/"
cp -a "$ISO/peregrine" "$NET/peregrine"
cp "$HERE/cache/kexec/kexec-$KEXEC_VERSION-static" "$NET/kexec"
log "done — $(du -sh "$NET" | cut -f1)"
