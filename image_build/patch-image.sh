#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine — Patch an existing image in-place
#
# All 11 fixes from the live board audit, applied directly to the image.
# No rebuild required — takes ~1 minute.
#
# Usage:
#   sudo ./image_build/patch-image.sh image_build/output/peregrine-q6a-v1.0.img
# ============================================================================

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: sudo $0 <path-to-image.img>"
    exit 1
fi

[[ $(id -u) -eq 0 ]] || { echo "Must run as root (sudo)"; exit 1; }

IMG="$1"
[[ -f "$IMG" ]] || { echo "Image not found: $IMG"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MOUNT_DIR=$(mktemp -d /tmp/peregrine-patch.XXXXXX)
EFI_MOUNT=$(mktemp -d /tmp/peregrine-efi.XXXXXX)
LOOP_DEV=""

cleanup() {
    set +e
    umount -lf "$EFI_MOUNT" 2>/dev/null
    umount -lf "$MOUNT_DIR" 2>/dev/null
    [[ -n "$LOOP_DEV" ]] && losetup -d "$LOOP_DEV" 2>/dev/null
    rmdir "$MOUNT_DIR" "$EFI_MOUNT" 2>/dev/null
}
trap cleanup EXIT

echo ""
echo "=== TrailCurrent Peregrine — Image Patcher (v2) ==="
echo ""

# ── Mount ────────────────────────────────────────────────────────────────────
echo "[mount] Loop-mounting image..."
LOOP_DEV=$(losetup --find --show --partscan "$IMG")
sleep 1

# Identify partitions (GPT: p1=config/FAT, p2=efi/FAT, p3=rootfs/ext4)
ROOT_PART="" ; EFI_PART=""
for p in "${LOOP_DEV}p1" "${LOOP_DEV}p2" "${LOOP_DEV}p3"; do
    [[ -b "$p" ]] || continue
    TYPE=$(blkid -s TYPE -o value "$p" 2>/dev/null || true)
    LABEL=$(blkid -s LABEL -o value "$p" 2>/dev/null || true)
    case "$LABEL" in
        efi)    EFI_PART="$p" ;;
        rootfs) ROOT_PART="$p" ;;
    esac
    [[ -z "$ROOT_PART" && "$TYPE" == "ext4" ]] && ROOT_PART="$p"
done

[[ -n "$ROOT_PART" ]] || { echo "ERROR: no ext4 rootfs partition found"; exit 1; }
mount "$ROOT_PART" "$MOUNT_DIR"
echo "  rootfs: $ROOT_PART"

if [[ -n "$EFI_PART" ]]; then
    mount "$EFI_PART" "$EFI_MOUNT"
    echo "  efi:    $EFI_PART"
else
    echo "  WARNING: no EFI partition found (boot cmdline won't be patched)"
fi

PASS=0
fix() { echo "[fix $((++PASS))] $1"; }

# ── Fix 1: SSH host keys ────────────────────────────────────────────────────
fix "Generate SSH host keys (so sshd starts immediately)"
mkdir -p "$MOUNT_DIR/etc/ssh"
for type in rsa ecdsa ed25519; do
    keyfile="$MOUNT_DIR/etc/ssh/ssh_host_${type}_key"
    rm -f "$keyfile" "${keyfile}.pub"
    ssh-keygen -t "$type" -f "$keyfile" -N "" -q
done
chmod 600 "$MOUNT_DIR"/etc/ssh/ssh_host_*_key
chmod 644 "$MOUNT_DIR"/etc/ssh/ssh_host_*_key.pub
echo "  $(ls "$MOUNT_DIR"/etc/ssh/ssh_host_*_key | wc -l) key pairs generated"

# ── Fix 2: power-save-hw exclude HID (class 03) ─────────────────────────────
fix "Update power-save-hw.service to exclude keyboards/mice"
cp "$SCRIPT_DIR/files/systemd/power-save-hw.service" \
    "$MOUNT_DIR/etc/systemd/system/power-save-hw.service"

# ── Fix 3: Kernel cmdline usbcore.autosuspend=-1 ────────────────────────────
fix "Add usbcore.autosuspend=-1 to kernel boot params"
# /etc/kernel/cmdline
CMDLINE="$MOUNT_DIR/etc/kernel/cmdline"
if [[ -f "$CMDLINE" ]] && ! grep -q "usbcore.autosuspend" "$CMDLINE"; then
    sed -i 's/$/ usbcore.autosuspend=-1/' "$CMDLINE"
    echo "  patched /etc/kernel/cmdline"
fi
# extlinux.conf
EXTLINUX="$MOUNT_DIR/boot/extlinux/extlinux.conf"
if [[ -f "$EXTLINUX" ]] && ! grep -q "usbcore.autosuspend" "$EXTLINUX"; then
    sed -i '/^[[:space:]]*append/ s/$/ usbcore.autosuspend=-1/' "$EXTLINUX"
    echo "  patched extlinux.conf"
fi
# systemd-boot entries on EFI partition
if [[ -n "$EFI_PART" ]]; then
    for entry in "$EFI_MOUNT"/loader/entries/*.conf; do
        [[ -f "$entry" ]] || continue
        if ! grep -q "usbcore.autosuspend" "$entry"; then
            sed -i '/^options / s/$/ usbcore.autosuspend=-1/' "$entry"
            echo "  patched $(basename "$entry")"
        fi
    done
fi

# ── Fix 4: Mask systemd-networkd-wait-online + samba + sysupdate ────────────
fix "Mask unnecessary services"
for svc in systemd-networkd-wait-online samba-ad-dc systemd-sysupdate.timer systemd-sysupdate-reboot.timer; do
    ln -sf /dev/null "$MOUNT_DIR/etc/systemd/system/${svc}.service" 2>/dev/null || \
    ln -sf /dev/null "$MOUNT_DIR/etc/systemd/system/${svc}" 2>/dev/null || true
    echo "  masked $svc"
done

# ── Fix 5: Self-test timeouts ───────────────────────────────────────────────
fix "Add timeouts to self-test commands"
cp "$SCRIPT_DIR/files/scripts/peregrine-self-test.sh" \
    "$MOUNT_DIR/usr/local/bin/peregrine-self-test.sh"
chmod 755 "$MOUNT_DIR/usr/local/bin/peregrine-self-test.sh"

# ── Fix 6: Clean sshd config (remove deprecated directives) ─────────────────
fix "Install clean sshd config drop-in"
cp "$SCRIPT_DIR/files/ssh/sshd_config.d/10-trailcurrent.conf" \
    "$MOUNT_DIR/etc/ssh/sshd_config.d/10-trailcurrent.conf"

# ── Fix 7: Password not expired ─────────────────────────────────────────────
fix "Reset password to 'trailcurrent' with no expiry"
echo "trailcurrent:trailcurrent" | chroot "$MOUNT_DIR" chpasswd 2>/dev/null || {
    # chroot may fail under some configs; direct shadow manipulation
    HASH=$(openssl passwd -6 trailcurrent)
    sed -i "s|^trailcurrent:[^:]*:|trailcurrent:${HASH}:|" "$MOUNT_DIR/etc/shadow"
}
# Set last-change to today (epoch days since 1970-01-01)
DAYS=$(( $(date +%s) / 86400 ))
sed -i "s|^\(trailcurrent:[^:]*\):[^:]*:\(.*\)|\1:${DAYS}:\2|" "$MOUNT_DIR/etc/shadow"
echo "  password=trailcurrent, last-change=today, no forced change"

# ── Fix 8: User groups (systemd-journal, adm) ───────────────────────────────
fix "Add trailcurrent to systemd-journal and adm groups"
# Parse existing groups and add missing ones
for grp in systemd-journal adm; do
    if grep -q "^${grp}:" "$MOUNT_DIR/etc/group"; then
        if ! grep -q "^${grp}:.*trailcurrent" "$MOUNT_DIR/etc/group"; then
            sed -i "s/^\(${grp}:.*\)/\1,trailcurrent/" "$MOUNT_DIR/etc/group"
            # Handle case where group line ends with : (no members yet)
            sed -i "s/^\(${grp}:[^:]*:[^:]*:\),/\1/" "$MOUNT_DIR/etc/group"
            echo "  added to $grp"
        else
            echo "  already in $grp"
        fi
    fi
done

# ── Fix 9: ALSA default device config ───────────────────────────────────────
fix "Pin Jabra as default ALSA device"
cp "$SCRIPT_DIR/files/alsa/asound.conf" "$MOUNT_DIR/etc/asound.conf"

# ── Fix 10: MaxAuthTries already in fix 6 sshd config ───────────────────────
# (included in 10-trailcurrent.conf — MaxAuthTries 6)

# ── Fix 11: build-image cache (already in build.sh, no image change needed) ─

# ── Bonus: Disable first-login wizard auto-launch (SSH-first workflow) ───────
fix "Make first-login wizard opt-in (not auto-launch)"
BASH_PROFILE="$MOUNT_DIR/home/trailcurrent/.bash_profile"
if [[ -f "$BASH_PROFILE" ]]; then
    sed -i '/peregrine-first-login/d' "$BASH_PROFILE"
    sed -i '/peregrine-setup-complete/d' "$BASH_PROFILE"
fi
# Leave the wizard script installed — users can run it manually:
#   /usr/local/bin/peregrine-first-login.sh

# ── Bonus: Skip firstboot reboot (SSH keys pre-generated) ───────────────────
fix "Disable firstboot reboot (keys already present)"
FIRSTBOOT="$MOUNT_DIR/usr/local/sbin/peregrine-firstboot.sh"
if [[ -f "$FIRSTBOOT" ]]; then
    sed -i 's|^systemctl reboot|# systemctl reboot  # patched: SSH keys pre-generated|' "$FIRSTBOOT"
    sed -i 's|^sleep 3|# sleep 3|' "$FIRSTBOOT"
fi

# ── Verify ───────────────────────────────────────────────────────────────────
echo ""
echo "=== Verification ==="
FAIL=0
verify() {
    if [[ -e "$MOUNT_DIR$1" ]]; then
        echo "  ✓ $1"
    else
        echo "  ✗ $1"
        FAIL=$((FAIL+1))
    fi
}
verify /etc/ssh/ssh_host_ed25519_key
verify /etc/ssh/ssh_host_rsa_key
verify /etc/ssh/ssh_host_ecdsa_key
verify /etc/ssh/sshd_config.d/10-trailcurrent.conf
verify /etc/asound.conf
verify /etc/modprobe.d/disable-wifi.conf
verify /etc/systemd/system/power-save-hw.service
verify /usr/local/bin/peregrine-self-test.sh

# Verify kernel cmdline was patched
if grep -q "usbcore.autosuspend=-1" "$MOUNT_DIR/etc/kernel/cmdline" 2>/dev/null; then
    echo "  ✓ kernel cmdline has usbcore.autosuspend=-1"
else
    echo "  ✗ kernel cmdline missing usbcore.autosuspend=-1"
    FAIL=$((FAIL+1))
fi

# Verify sshd config parses (basic check)
if ! grep -q "ChallengeResponseAuthentication" "$MOUNT_DIR/etc/ssh/sshd_config.d/10-trailcurrent.conf"; then
    echo "  ✓ no deprecated ChallengeResponseAuthentication"
else
    echo "  ✗ deprecated directive still present"
    FAIL=$((FAIL+1))
fi

echo ""
if [[ $FAIL -eq 0 ]]; then
    echo "All checks passed."
else
    echo "WARNING: $FAIL check(s) failed — review output above"
fi

# ── Unmount ──────────────────────────────────────────────────────────────────
echo ""
echo "Unmounting..."
[[ -n "$EFI_PART" ]] && umount "$EFI_MOUNT"
umount "$MOUNT_DIR"
losetup -d "$LOOP_DEV"
LOOP_DEV=""

echo ""
echo "=== Image patched successfully ==="
echo ""
echo "  Fixes applied: $PASS"
echo "  Password:      trailcurrent / trailcurrent (no forced change)"
echo "  SSH:           host keys pre-generated, clean config"
echo "  USB:           autosuspend disabled via kernel cmdline"
echo "  Keyboard:      HID excluded from power-save"
echo "  Self-test:     all commands have timeouts"
echo "  Wizard:        disabled (run manually via SSH if needed)"
echo ""
echo "Reflash:"
echo "  sudo rm -rf /tmp/peregrine-flash"
echo "  sudo ./image_build/flash.sh --os $IMG"
echo ""
echo "Then from your laptop:"
echo "  ssh trailcurrent@peregrine.local"
echo "  (password: trailcurrent)"
echo ""
