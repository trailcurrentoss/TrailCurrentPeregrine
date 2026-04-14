# 3. Flashing a board

You have two options depending on what hardware you have available. Both produce
the same result — pick whichever is easier.

| | Path A: NVMe enclosure | Path B: EDL over USB |
|---|---|---|
| **Extra hardware** | USB M.2 enclosure | USB-C data cable |
| **NVMe removal** | Yes — remove, flash, reinstall | No — stays in board |
| **OS** | Windows / Mac / Linux | Linux only |
| **Tool** | Balena Etcher (or any disk imager) | `flash.sh` |

---

## Path A: NVMe enclosure + Balena Etcher

1. Power down the Q6A.
2. Remove the NVMe SSD from the M.2 slot and install it in a USB enclosure.
3. Open [Balena Etcher](https://etcher.balena.io/). Select **Flash from file**
   and choose `image_build/output/peregrine-q6a-v1.0.img`.
4. Select the NVMe enclosure as the target and click **Flash**.
   Takes 3–8 minutes.
5. Remove the NVMe from the enclosure and reinstall it in the Q6A.

→ Skip to [First boot](#first-boot).

---

## Path B: EDL over USB (Linux only)

### Enter EDL mode

**Brand-new board:** boots into EDL automatically when powered via USB-C.
No button press needed.

**Already-flashed board:** hold the **EDL button** (small button next to the
USB-C port) while connecting USB-C. Release after ~2 seconds.

Verify:

```bash
lsusb | grep 9008
# Bus 002 Device 015: ID 05c6:9008 Qualcomm, Inc. Gobi Wireless Modem (QDL mode)
```

If nothing appears, disconnect, hold the EDL button, and reconnect.

### Flash

```bash
sudo ./image_build/flash.sh --os image_build/output/peregrine-q6a-v1.0.img
```

Takes 3–8 minutes. Once complete, disconnect the USB-C cable.

---

## First boot

1. Connect Ethernet.
2. Apply 12V DC power.
3. Wait ~3 minutes — the first-boot service expands the root partition,
   regenerates SSH host keys, and reboots once automatically.

```bash
ssh trailcurrent@peregrine.local
# Default password: trailcurrent
# The first-login wizard forces a password change immediately.
```

If `peregrine.local` doesn't resolve, find the board's IP on your router.

---

## NVMe sector size

The image must match your NVMe's **logical** sector size. Most drives use
**512 bytes** (the default). Some enterprise drives use **4096 bytes**.

Check with the drive in a USB enclosure:

```bash
cat /sys/block/nvmeXn1/queue/logical_block_size
```

If it returns `4096`, rebuild with:

```bash
sudo ./image_build/build.sh --sector-size 4096
```

**If the board boots to a UEFI `Shell>` prompt**, this is almost certainly the
cause. See [06-troubleshooting.md](06-troubleshooting.md#flash-succeeds-but-board-drops-to-uefi-shell).

---

## SPI NOR firmware (rarely needed)

The Q6A ships from Radxa with SPI boot firmware pre-flashed. **Most users
never need to touch this.**

### When it's needed

Update the SPI firmware if you see any of the following:

- **Board purchased before October 2025** — Radxa updated the firmware in late
  2025. Older boards shipped with an earlier version that may not boot newer OS
  images correctly.
- **Board powers on but never boots** — fans/LEDs come on, Ethernet link
  activates, but `peregrine.local` never appears and SSH never responds, even
  after waiting 5+ minutes.
- **Board drops to a UEFI `Shell>` prompt** and you've already confirmed the
  NVMe sector size is correct (see above) — a stale bootloader is the next
  most likely cause.

If you're unsure which firmware version is on your board, check the
[Radxa Dragon Q6A SPI firmware docs](https://docs.radxa.com/en/dragon/q6a/low-level-dev/spi-fw)
— they document the current required version and how to identify what's
installed.

### How to update

This must be done via EDL mode from a Linux machine. It cannot be performed
from within a running Peregrine install — the SPI NOR is not writable from
the booted OS without special tooling that is not included in our image.

```bash
# 1. Put the board in EDL mode (see Path B above)
# 2. Flash the firmware
sudo ./image_build/flash.sh --firmware
# 3. Reflash the OS image as normal (firmware flash does not write the NVMe)
sudo ./image_build/flash.sh --os image_build/output/peregrine-q6a-v1.0.img
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Etcher shows NVMe as read-only | Enclosure write-protect switch | Check the enclosure's lock switch |
| `lsusb` doesn't show `9008` | Board not in EDL mode | Hold EDL button while connecting USB-C |
| `Could not connect to device` | Charge-only USB-C cable | Use a data-capable cable |
| `Permission denied` on `/dev/bus/usb/...` | No udev rule | `echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="05c6", ATTR{idProduct}=="9008", MODE="0666"' \| sudo tee /etc/udev/rules.d/51-edl.rules && sudo udevadm control --reload-rules` |
| Flash hangs at 0% | NVMe not seated | Re-seat the M.2 module |
| Board drops to UEFI `Shell>` | Wrong sector size | Rebuild with correct `--sector-size` |
| Board doesn't boot at all | Old SPI firmware | See SPI NOR section above |

## Next

→ [04-first-boot.md](04-first-boot.md) for what happens during the first boot.
