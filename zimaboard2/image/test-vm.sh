#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine (ZimaBoard 2) — QEMU/KVM + OVMF install-and-boot test
#
#   ./zimaboard2/image/test-vm.sh [--boot-only] [path/to/peregrine-zimaboard2-*.iso]
#
#   --boot-only  skip the install; boot the card the last run installed
#
#   1. install: boots the ISO's live installer (unattended) onto an emulated
#      32 GiB SD/MMC card, so the installer sees mmcblk0 just like the eMMC
#   2. boot:    boots the installed card with FRESH UEFI variables (no NVRAM
#               entry), which proves the removable EFI/BOOT path works
#   3. check:   over SSH — services, llama-server, bridge, self-test
#
# Audio and the iGPU cannot be tested in a VM; those self-test items fail
# here by design. Runs as a normal user (needs /dev/kvm access).
# ============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOT_ONLY=false
[[ "${1:-}" == --boot-only ]] && { BOOT_ONLY=true; shift; }
ISO="${1:-$(ls -1t "$HERE"/output/peregrine-zimaboard2-*.iso 2>/dev/null | head -1)}"
[[ -f "$ISO" ]] || { echo "no ISO given and none in $HERE/output" >&2; exit 1; }
WORK="$HERE/output/vm"
OVMF_CODE=/usr/share/OVMF/OVMF_CODE_4M.fd
OVMF_VARS=/usr/share/OVMF/OVMF_VARS_4M.fd
SSH_PORT="${PEREGRINE_VM_SSH_PORT:-2222}"
HTTPS_PORT="${PEREGRINE_VM_HTTPS_PORT:-8443}"

log() { echo "[test-vm] $*"; }
for t in qemu-system-x86_64 qemu-img xorriso ssh; do
    command -v "$t" >/dev/null || { echo "missing: $t" >&2; exit 1; }
done
[[ -r /dev/kvm && -w /dev/kvm ]] || { echo "no access to /dev/kvm" >&2; exit 1; }
[[ -f "$OVMF_CODE" ]] || { echo "OVMF not installed (apt install ovmf)" >&2; exit 1; }

# No emulated display: the headless bochs adapter floods the serial console
# with DRM vblank-timeout traces, which slows the guest to a crawl.
QEMU_BASE=(qemu-system-x86_64 -enable-kvm -machine q35 -cpu host -smp 4 -m 8G
    -drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE"
    -device sdhci-pci -device sd-card,drive=emmc
    -drive "id=emmc,if=none,format=qcow2,file=$WORK/emmc.qcow2"
    -vga none -display none -no-reboot)

if $BOOT_ONLY; then
    [[ -f "$WORK/emmc.qcow2" ]] || { echo "--boot-only: no $WORK/emmc.qcow2 from a previous run" >&2; exit 1; }
else
rm -rf "$WORK"; mkdir -p "$WORK"

# ── 1. install ──────────────────────────────────────────────────────────────
log "ISO: $ISO"
qemu-img create -q -f qcow2 "$WORK/emmc.qcow2" 32G
cp "$OVMF_VARS" "$WORK/vars-install.fd"
xorriso -osirrox on -indev "$ISO" -extract /casper/vmlinuz "$WORK/vmlinuz" \
    -extract /casper/initrd "$WORK/initrd" >/dev/null 2>&1
chmod u+w "$WORK/vmlinuz" "$WORK/initrd"

log "installing (unattended, serial log: $WORK/install-serial.log)"
"${QEMU_BASE[@]}" \
    -drive "if=pflash,format=raw,file=$WORK/vars-install.fd" \
    -drive "file=$ISO,media=cdrom,readonly=on" \
    -kernel "$WORK/vmlinuz" -initrd "$WORK/initrd" \
    -append "boot=casper noprompt fsck.mode=skip peregrine.unattended=1 peregrine.serial=1 console=tty1 console=ttyS0,115200" \
    -serial "file:$WORK/install-serial.log" &
QPID=$!
if ! timeout 1800 tail --pid=$QPID -f /dev/null; then
    kill $QPID 2>/dev/null; echo "install timed out" >&2; exit 1
fi
if grep -q 'PEREGRINE-INSTALL: OK' "$WORK/install-serial.log"; then
    log "install OK"
else
    tail -40 "$WORK/install-serial.log"
    echo "install FAILED" >&2; exit 1
fi
fi

# ── 2. boot the installed disk with fresh NVRAM ─────────────────────────────
# OVMF has no SD/MMC driver, so it cannot boot the emulated card. The same
# image goes on a SATA port for the boot test; fstab and GRUB use UUIDs, so
# the different device name changes nothing. (The ZimaBoard firmware boots
# its eMMC natively.)
cp "$OVMF_VARS" "$WORK/vars-boot.fd"
log "booting installed system (ssh on localhost:$SSH_PORT)"
qemu-system-x86_64 -enable-kvm -machine q35 -cpu host -smp 4 -m 8G \
    -drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE" \
    -drive "if=pflash,format=raw,file=$WORK/vars-boot.fd" \
    -drive "id=disk,if=none,format=qcow2,file=$WORK/emmc.qcow2" \
    -device ide-hd,drive=disk,bus=ide.0 \
    -vga none -display none -no-reboot \
    -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22,hostfwd=tcp:127.0.0.1:$HTTPS_PORT-:443" \
    -device virtio-net-pci,netdev=n0 \
    -monitor "unix:$WORK/monitor.sock,server,nowait" \
    -serial "file:$WORK/boot-serial.log" &
QPID=$!
trap 'kill $QPID 2>/dev/null || true' EXIT

# Password auth without sshpass: OpenSSH's askpass hook.
ASKPASS="$WORK/askpass.sh"
printf '#!/bin/sh\necho trailcurrent\n' > "$ASKPASS"; chmod 700 "$ASKPASS"
vm() {
    SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force DISPLAY=none \
    ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -o ConnectTimeout=5 -o PubkeyAuthentication=no \
        trailcurrent@127.0.0.1 "$@"
}

for i in $(seq 1 120); do
    vm true 2>/dev/null && break
    kill -0 $QPID 2>/dev/null || { echo "VM exited during boot" >&2; exit 1; }
    sleep 5
done
vm true || { echo "ssh never came up" >&2; exit 1; }
log "ssh up after ~$((i * 5)) s"

# ── 3. checks ───────────────────────────────────────────────────────────────
log "waiting for peregrine-llm (first boot compiles shaders / loads the model)"
vm 'for i in $(seq 1 180); do systemctl is-active --quiet peregrine-llm-bridge && exit 0; sleep 5; done; exit 1' \
    || log "WARNING: bridge not active after 15 min"

vm 'cat /etc/peregrine-release; echo; lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT; echo;
    efibootmgr 2>/dev/null | head -5; echo;
    systemctl --no-pager --plain list-units "peregrine-*" voice-assistant.service ssh.service | head -20;
    echo; systemctl --failed --no-pager --plain; echo;
    free -h; echo; df -h /'
log "LLM through the bridge:"
vm 'curl -s -m 300 http://127.0.0.1:11434/api/generate -H "Content-Type: application/json" \
    -d "{\"prompt\":\"In one sentence, what is a travel trailer?\",\"system\":\"You are a concise assistant.\",\"options\":{\"num_predict\":60}}"; echo'
log "web chat HTTPS:"
curl -sk -m 10 -o /dev/null -w "  https://localhost:$HTTPS_PORT/ -> HTTP %{http_code}\n" "https://127.0.0.1:$HTTPS_PORT/" || true
log "self-test (audio + iGPU fail in a VM by design):"
vm 'sudo -S -p "" true <<< trailcurrent; echo trailcurrent | sudo -S peregrine-self-test.sh' || true

log "shutting down"
vm 'echo trailcurrent | sudo -S systemctl poweroff' 2>/dev/null || true
timeout 60 tail --pid=$QPID -f /dev/null || kill $QPID
trap - EXIT
log "done — logs in $WORK"
