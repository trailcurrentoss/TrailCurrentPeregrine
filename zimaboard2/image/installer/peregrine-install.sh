#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine (ZimaBoard 2) — installer
#
# Runs on tty1 of the live system. Copies Peregrine to the board's internal
# storage and makes it bootable:
#
#   1. choose the target disk (the eMMC by default) and confirm the erase
#   2. verify the payload checksums (catches a bad USB write or download)
#   3. GPT: 512 MiB EFI system partition + ext4 root filling the disk
#   4. unpack the root filesystem, copy the LLM model
#   5. fstab, per-board SSH host keys, GRUB (NVRAM entry + removable path)
#
# The payload comes from the USB stick, or — for a network install started
# by netinstall.sh — over HTTP. A network payload is downloaded into RAM and
# verified in full before anything is erased.
#
# Kernel command line switches:
#   peregrine.unattended=1   no questions: erase the sole eMMC
#   peregrine.target=/dev/X  install to X instead of auto-detecting
#   peregrine.serial=1       mirror the log to /dev/ttyS0 (VM testing)
#   peregrine.payload=URL    fetch the payload from URL (…/peregrine)
#   peregrine.done=reboot    reboot when finished (default: power off when
#                            unattended, wait for Enter otherwise)
# ============================================================================

set -Eeuo pipefail

LOG=/run/peregrine-install.log
TARGET_MNT=/target
CMDLINE=" $(cat /proc/cmdline) "
UNATTENDED=false
[[ "$CMDLINE" == *" peregrine.unattended=1 "* ]] && UNATTENDED=true
FORCED_TARGET=$(sed -n 's/.* peregrine\.target=\([^ ]*\) .*/\1/p' <<< "$CMDLINE")
PAYLOAD_URL=$(sed -n 's/.* peregrine\.payload=\([^ ]*\) .*/\1/p' <<< "$CMDLINE")
DONE_ACTION=$(sed -n 's/.* peregrine\.done=\([^ ]*\) .*/\1/p' <<< "$CMDLINE")

if [[ "$CMDLINE" == *" peregrine.serial=1 "* && -w /dev/ttyS0 ]]; then
    exec > >(tee -a "$LOG" /dev/ttyS0) 2>&1
else
    exec > >(tee -a "$LOG") 2>&1
fi

GREEN='\033[38;5;70m'; TEAL='\033[38;5;30m'; RED='\033[0;31m'
YELLOW='\033[1;33m'; BOLD='\033[1m'; RESET='\033[0m'

# A network install reports progress back to netinstall.sh on the build host.
report() {
    [[ -n "$PAYLOAD_URL" ]] || return 0
    curl -s -m 5 -X POST --data-binary "$*" "${PAYLOAD_URL%/peregrine}/status" >/dev/null 2>&1 || true
}

say()  { echo -e "$*"; }
step() { echo ""; echo -e "${BOLD}${TEAL}── $* ──${RESET}"; report "── $*"; }
ok()   { echo -e "  ${GREEN}✓${RESET} $*"; report "  ✓ $*"; }
warn() { echo -e "  ${YELLOW}!${RESET} $*"; report "  ! $*"; }

finish() {   # finish OK|FAILED
    if $UNATTENDED; then
        echo "PEREGRINE-INSTALL: $1"
        report "PEREGRINE-INSTALL: $1"
        # The kernel log reaches every console, even one a getty has hung up.
        echo "PEREGRINE-INSTALL: $1" > /dev/kmsg 2>/dev/null || true
        sleep 3
        if [[ "$1" == OK && "$DONE_ACTION" == reboot ]]; then
            systemctl reboot --force --force
        elif [[ "$1" == OK || -z "$PAYLOAD_URL" ]]; then
            systemctl poweroff --force --force
        fi
        # A failed network install stays up with its network and log, so
        # the cause can be read off the screen. Nothing was erased unless the
        # failure came after the download was verified.
    fi
}

on_error() {
    local rc=$? line=$1
    echo ""
    echo -e "${RED}${BOLD}Installation FAILED${RESET} (line $line, exit $rc)."
    echo "  Log: $LOG"
    umount -R "$TARGET_MNT" 2>/dev/null || true
    finish FAILED
    echo ""
    echo "  Dropping to a shell for diagnosis. Type 'reboot' when done."
    exec bash -i </dev/tty1 >/dev/tty1 2>&1
}
trap 'on_error $LINENO' ERR

ask() {   # ask "prompt" -> reply in $REPLY
    read -rp "$1" REPLY </dev/tty1
}

clear 2>/dev/null || true
say ""
say "${BOLD}${GREEN}Trail${TEAL}Current${RESET} ${BOLD}Peregrine — ZimaBoard 2 installer${RESET}"

# ── Locate the payload ──────────────────────────────────────────────────────
MEDIUM=""
if [[ -n "$PAYLOAD_URL" ]]; then
    step "Downloading the installer files"
    for _ in $(seq 1 60); do
        curl -sf -m 5 -o /dev/null "$PAYLOAD_URL/release" && break
        sleep 2
    done
    # RAM, not the target disk: everything is verified before the erase.
    PAYLOAD=/run/peregrine-payload
    mkdir -p "$PAYLOAD"
    mount -t tmpfs -o size=6G tmpfs "$PAYLOAD"
    curl -sf -m 10 -o "$PAYLOAD/release" "$PAYLOAD_URL/release"
    curl -sf -m 10 -o "$PAYLOAD/SHA256SUMS" "$PAYLOAD_URL/SHA256SUMS"
    while read -r _sum name; do
        mkdir -p "$PAYLOAD/$(dirname "$name")"
        curl -sf --retry 3 -o "$PAYLOAD/$name" "$PAYLOAD_URL/$name"
        ok "$name ($(du -h "$PAYLOAD/$name" | cut -f1))"
    done < "$PAYLOAD/SHA256SUMS"
    # shellcheck disable=SC1091
    source "$PAYLOAD/release"
    ok "Peregrine ${PEREGRINE_VERSION} (built ${PEREGRINE_BUILD_DATE}) from the network"
else
    step "Installer medium"
    for d in /cdrom /run/live/medium /media/cdrom; do
        if [[ -f "$d/peregrine/rootfs.tar.zst" ]]; then MEDIUM="$d"; break; fi
    done
    if [[ -z "$MEDIUM" ]]; then
        dev=$(blkid -L PEREGRINE_ZB2 2>/dev/null || true)
        [[ -n "$dev" ]] || { echo "Cannot find the Peregrine payload (label PEREGRINE_ZB2)."; false; }
        mkdir -p /media/peregrine
        mount -o ro "$dev" /media/peregrine
        MEDIUM=/media/peregrine
    fi
    PAYLOAD="$MEDIUM/peregrine"
    # shellcheck disable=SC1091
    source "$PAYLOAD/release"
    ok "Peregrine ${PEREGRINE_VERSION} (built ${PEREGRINE_BUILD_DATE}) on ${MEDIUM}"
fi

# The disk we booted from is never offered as a target. A hybrid ISO written
# to a USB stick carries its filesystem on the whole device, so PKNAME is
# empty there and the device itself is the boot disk.
MEDIUM_DEV=$(findmnt -no SOURCE "$MEDIUM" 2>/dev/null || true)
BOOT_DISK=$(lsblk -no PKNAME "$MEDIUM_DEV" 2>/dev/null | head -1 || true)
[[ -z "$BOOT_DISK" && -n "$MEDIUM_DEV" ]] && BOOT_DISK=$(basename "$MEDIUM_DEV")

# ── Choose the target disk ──────────────────────────────────────────────────
step "Target disk"
mapfile -t CANDIDATES < <(
    lsblk -dnpo NAME,TYPE,SIZE -b | awk '$2 == "disk" && $3 >= 15000000000 {print $1}' |
        grep -Ev '/dev/(loop|zram|sr|ram)|boot[0-9]+$|rpmb$' |
        grep -v "^/dev/${BOOT_DISK:-NONE}$" || true
)

describe() {
    lsblk -dno SIZE,MODEL,TRAN "$1" 2>/dev/null | sed 's/  */ /g; s/^ //'
}

TARGET=""
if [[ -n "$FORCED_TARGET" ]]; then
    TARGET="$FORCED_TARGET"
    [[ -b "$TARGET" ]] || { echo "peregrine.target=$TARGET is not a block device"; false; }
else
    EMMC=()
    for d in "${CANDIDATES[@]}"; do [[ "$d" == /dev/mmcblk* ]] && EMMC+=("$d"); done
    if $UNATTENDED; then
        if [[ ${#EMMC[@]} -ne 1 ]]; then
            echo "Unattended install needs exactly one eMMC; found ${#EMMC[@]}."
            false
        fi
        TARGET="${EMMC[0]}"
    else
        if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
            echo "No internal disk of 15 GB or more was found."
            false
        fi
        DEFAULT=1
        i=1
        for d in "${CANDIDATES[@]}"; do
            tag=""
            [[ "$d" == /dev/mmcblk* ]] && { tag="  (internal eMMC)"; DEFAULT=$i; }
            say "  [$i] $d  $(describe "$d")$tag"
            i=$((i + 1))
        done
        while true; do
            ask "  Install to which disk? [$DEFAULT]: "
            n="${REPLY:-$DEFAULT}"
            if [[ "$n" =~ ^[0-9]+$ ]] && (( n >= 1 && n <= ${#CANDIDATES[@]} )); then
                TARGET="${CANDIDATES[$((n - 1))]}"
                break
            fi
        done
    fi
fi
ok "Target: $TARGET  $(describe "$TARGET")"

if ! $UNATTENDED; then
    say ""
    say "  ${RED}${BOLD}Everything on $TARGET will be erased.${RESET}"
    ask "  Type ERASE to continue, anything else to cancel: "
    if [[ "$REPLY" != "ERASE" ]]; then
        say "  Cancelled. Nothing was changed. Remove the USB drive and reboot."
        trap - ERR
        exec bash -i </dev/tty1 >/dev/tty1 2>&1
    fi
fi

# ── Verify the payload ──────────────────────────────────────────────────────
step "Verifying the installer files"
( cd "$PAYLOAD" && sha256sum --quiet -c SHA256SUMS )
ok "Checksums match"

# ── Partition and format ────────────────────────────────────────────────────
step "Partitioning $TARGET"
umount "${TARGET}"* 2>/dev/null || true
wipefs -a -q "$TARGET"
sgdisk --zap-all "$TARGET" >/dev/null
sgdisk -n1:0:+512M -t1:ef00 -c1:EFI \
       -n2:0:0     -t2:8304 -c2:peregrine-root "$TARGET" >/dev/null
partprobe "$TARGET" 2>/dev/null || true
udevadm settle
if [[ "$TARGET" =~ [0-9]$ ]]; then P="${TARGET}p"; else P="$TARGET"; fi
ESP="${P}1"; ROOT="${P}2"
for _ in $(seq 1 20); do [[ -b "$ROOT" ]] && break; sleep 0.5; done
mkfs.vfat -F 32 -n EFI "$ESP" >/dev/null
mkfs.ext4 -q -F -L peregrine-root "$ROOT"
ok "EFI $ESP, root $ROOT"

mkdir -p "$TARGET_MNT"
mount "$ROOT" "$TARGET_MNT"
mkdir -p "$TARGET_MNT/boot/efi"
mount "$ESP" "$TARGET_MNT/boot/efi"

# ── Copy the system ─────────────────────────────────────────────────────────
step "Copying Peregrine (this takes a few minutes)"
pv -pterb -s "$(stat -c %s "$PAYLOAD/rootfs.tar.zst")" "$PAYLOAD/rootfs.tar.zst" |
    zstd -dc | tar -x -p --numeric-owner --xattrs --xattrs-include='*' --acls -C "$TARGET_MNT"
ok "Root filesystem unpacked"

MODEL_DIR="$TARGET_MNT/var/lib/peregrine/llm/models"
mkdir -p "$MODEL_DIR"
for f in "$PAYLOAD"/models/*; do
    pv -pterb "$f" > "$MODEL_DIR/$(basename "$f").part"
    mv "$MODEL_DIR/$(basename "$f").part" "$MODEL_DIR/$(basename "$f")"
    chmod 644 "$MODEL_DIR/$(basename "$f")"
done
ok "LLM model copied: $(cd "$PAYLOAD/models" && ls ./*.gguf | sed 's|^\./||')"

# ── Configure ───────────────────────────────────────────────────────────────
step "Configuring"
ROOT_UUID=$(blkid -s UUID -o value "$ROOT")
ESP_UUID=$(blkid -s UUID -o value "$ESP")
cat > "$TARGET_MNT/etc/fstab" <<EOF
# TrailCurrent Peregrine — written by the installer
UUID=$ROOT_UUID  /          ext4  errors=remount-ro,noatime  0  1
UUID=$ESP_UUID   /boot/efi  vfat  umask=0077                 0  1
EOF
ok "fstab"

for fs in dev dev/pts proc sys run; do mount --bind "/$fs" "$TARGET_MNT/$fs"; done
if [[ -d /sys/firmware/efi/efivars ]]; then
    mount --bind /sys/firmware/efi/efivars "$TARGET_MNT/sys/firmware/efi/efivars" 2>/dev/null || true
fi

# Every board gets its own SSH host keys; the image's keys are shared by
# every install from the same USB stick.
rm -f "$TARGET_MNT"/etc/ssh/ssh_host_*
chroot "$TARGET_MNT" ssh-keygen -A >/dev/null
ok "SSH host keys minted"

# GRUB: a named NVRAM entry, plus the removable fallback path
# (EFI/BOOT/BOOTX64.EFI) so the board boots even if the firmware forgets it.
NVRAM_OPT=()
[[ -d /sys/firmware/efi/efivars ]] || NVRAM_OPT=(--no-nvram)
chroot "$TARGET_MNT" grub-install --target=x86_64-efi --efi-directory=/boot/efi \
    --bootloader-id=ubuntu "${NVRAM_OPT[@]}" --recheck 2>&1 | sed 's/^/  /'
chroot "$TARGET_MNT" grub-install --target=x86_64-efi --efi-directory=/boot/efi \
    --removable --no-nvram 2>&1 | sed 's/^/  /'
chroot "$TARGET_MNT" update-grub 2>&1 | sed 's/^/  /'
ok "Bootloader installed"

{
    echo "INSTALLED_FROM=\"$PEREGRINE_VERSION\""
    echo "INSTALL_DATE=\"$(date -R)\""
    echo "INSTALL_TARGET=\"$TARGET\""
} > "$TARGET_MNT/var/lib/peregrine/install-info"

sync
umount -R "$TARGET_MNT"
ok "Done"

trap - ERR
say ""
say "${BOLD}${GREEN}Peregrine is installed.${RESET}"
say ""
say "  On the next boot the board sets itself up (about a minute), then:"
say "    ssh trailcurrent@peregrine.local      (password: trailcurrent)"
say "  The first login asks you to change the password."
finish OK
say ""
ask "  Remove the USB drive, then press Enter to reboot. "
systemctl reboot
