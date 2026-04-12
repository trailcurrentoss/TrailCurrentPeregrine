#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine — First-boot setup
# Runs once on the first boot after flashing the golden image, before any
# user-facing services. Regenerates per-board identity, expands rootfs to
# fill the NVMe, then disables itself and reboots so all changes take hold.
# ============================================================================

set -uo pipefail

log() { echo "[firstboot] $*"; }

mkdir -p /var/lib/peregrine

# 1. Regenerate machine-id (was cleared in the golden image)
if [[ ! -s /etc/machine-id ]]; then
    systemd-machine-id-setup
    log "Regenerated /etc/machine-id"
fi

# 2. Regenerate SSH host keys (were removed in the golden image)
if ! ls /etc/ssh/ssh_host_*_key &>/dev/null; then
    dpkg-reconfigure -f noninteractive openssh-server
    log "Regenerated SSH host keys"
fi

# 3. Expand the root partition to fill the NVMe
ROOT_DEV=$(findmnt -n -o SOURCE /)
ROOT_DISK=$(lsblk -ndo PKNAME "$ROOT_DEV" 2>/dev/null || echo "")

if [[ -n "$ROOT_DISK" ]]; then
    PART_NUM=$(echo "$ROOT_DEV" | grep -oP '\d+$')
    if [[ -n "$PART_NUM" ]]; then
        log "Expanding /dev/${ROOT_DISK} partition ${PART_NUM} to fill disk..."
        growpart "/dev/${ROOT_DISK}" "${PART_NUM}" 2>/dev/null || \
            log "growpart reported no change (already at max)"
        resize2fs "$ROOT_DEV" 2>/dev/null || \
            log "resize2fs reported no change"
        log "Root filesystem expanded"
    else
        log "WARNING: Could not parse partition number from $ROOT_DEV"
    fi
else
    log "WARNING: Could not determine root disk for expansion"
fi

# 4. Mark complete and disable this service
touch /var/lib/peregrine/.firstboot-done
systemctl disable peregrine-firstboot.service 2>/dev/null || true
log "First-boot complete — service disabled"

# 5. Reboot to apply all changes cleanly (sysctl, hostname, expanded fs)
log "Rebooting in 3 seconds..."
sleep 3
systemctl reboot
