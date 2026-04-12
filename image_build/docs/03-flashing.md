# 3. Flashing a board

Flashing a fresh Radxa Dragon Q6A is a **two-step process**:

1. **SPI NOR firmware** (one-time per board) — writes the EDK2 UEFI bootloader.
   Without this, the board cannot boot from any storage.
2. **OS image** (every reflash) — writes the Peregrine image to the M.2 NVMe.

Both steps go over USB-C using Qualcomm's EDL (Emergency Download) protocol.
The `flash.sh` helper wraps the underlying `edl-ng` calls with safe defaults.

## Hardware needed

- Radxa Dragon Q6A board
- M.2 NVMe SSD (128 GB minimum, 256 GB recommended) installed in the M.2 slot
- USB-C cable (data, not charge-only)
- 12V DC power supply (used after flashing, not during)
- A Linux build host where you ran `build.sh`

## NVMe sector size

The image must be built with a sector size that matches your NVMe drive's
**logical** sector size. Most consumer drives use **512 bytes** (the default).
Some enterprise/datacenter drives use **4096 bytes**.

If you're not sure, check the drive's spec sheet, or if the drive is
accessible from a Linux machine:

```bash
# From the build host (if the NVMe is in a USB enclosure)
sudo nvme id-ns /dev/nvmeXn1 -H | grep "LBA Format"

# Or more simply
cat /sys/block/nvmeXn1/queue/logical_block_size
```

A value of `512` means build with the default. A value of `4096` means:

```bash
sudo ./image_build/build.sh --sector-size 4096
```

**If you flash and the board drops to a UEFI `Shell>` prompt with no `FSx:`
entries in `map -r`**, you almost certainly have the wrong sector size.
See [06-troubleshooting.md](06-troubleshooting.md#flash-succeeds-but-board-drops-to-uefi-shell).

## Step 0: Allow USB access to the EDL device

EDL mode exposes the board as USB ID `05c6:9008`. By default that requires
root, but you can let your user access it without sudo:

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="05c6", ATTR{idProduct}=="9008", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/51-edl.rules
sudo udevadm control --reload-rules
```

(Optional but recommended.)

## Step 1: Enter EDL mode

There are two cases:

### Brand-new board (never flashed)

A board with no firmware boots into EDL automatically when powered on via
USB-C. No button press needed.

### Already-flashed board

Hold the **EDL button** (small button on the PCB next to the USB-C port)
while connecting USB-C. Release after ~2 seconds.

### Verify

```bash
lsusb | grep 9008
```

You should see:

```
Bus 002 Device 015: ID 05c6:9008 Qualcomm, Inc. Gobi Wireless Modem (QDL mode)
```

If you see nothing, the board is not in EDL mode — disconnect, hold the EDL
button, reconnect.

## Step 2: Flash SPI NOR firmware (one-time)

```bash
sudo ./image_build/flash.sh --firmware
```

This writes the EDK2 UEFI bootloader, TrustZone, hypervisor, and other
Qualcomm firmware components to the on-board SPI NOR. Takes ~30 seconds.

**Skip this step on subsequent reflashes** — the SPI NOR is non-volatile
and the firmware never changes for Peregrine.

The firmware files come from `image_build/firmware/` (committed to the repo).
The script extracts them on the fly into `/tmp/peregrine-flash/` and
invokes `edl-ng rawprogram`.

## Step 3: Flash the OS image to NVMe

```bash
sudo ./image_build/flash.sh --os image_build/output/peregrine-q6a-v1.0.img
```

This writes the entire ~6 GB image (including the GPT, EFI partition, and
rootfs) starting at sector 0 of the NVMe. Takes 3–8 minutes depending on USB
throughput.

**The board must still be in EDL mode for this step.** If you rebooted between
steps 2 and 3, hold the EDL button and re-power the board first.

## Step 4: Boot for the first time

1. **Disconnect the USB-C cable** from the Q6A.
2. Connect Ethernet.
3. Apply 12V DC power.
4. Wait ~3 minutes for the first-boot service to complete (it expands the
   root partition, regenerates SSH host keys, and reboots once).

The board's hostname is `peregrine` and it advertises via mDNS, so you can
SSH in by hostname:

```bash
ssh trailcurrent@peregrine.local
```

Default password is `trailcurrent`. The first-login wizard will force a
password change immediately.

If `peregrine.local` doesn't resolve (some networks block mDNS), find the
board on your DHCP server or LAN router and use its IP.

## Troubleshooting flash

| Symptom | Likely cause | Fix |
|---|---|---|
| `Could not connect to device` | USB cable is charge-only | Use a USB-C data cable |
| `lsusb` doesn't show 9008 | Board not in EDL mode | Hold EDL button while powering on |
| `Permission denied` opening `/dev/bus/usb/...` | Missing udev rule | See Step 0 |
| `rawprogram*.xml: No such file` | Firmware archive corrupted | Re-run preflight to verify firmware/ |
| Flash hangs at 0% | NVMe not seated properly | Re-seat the M.2 module |
| Flash succeeds but board doesn't boot | SPI NOR firmware not flashed | Run `flash.sh --firmware` first |

## Next

→ [04-first-boot.md](04-first-boot.md) for what happens during the first boot.
