# 6. Troubleshooting

Common failures broken down by phase. The "Cost to retry" column tells you
how much time you'll burn re-doing the step if you guess wrong about the fix.

## Build failures

### Hook 2 — apt repo install fails
```
[hook 2] adding Radxa QCS6490 apt repo
curl: (6) Could not resolve host: radxa-repo.github.io
```
**Cause:** the chroot couldn't reach the network. Either your build host has
no network, or DNS inside the chroot is broken.
**Fix:** check `ping radxa-repo.github.io` from the build host.
**Cost to retry:** ~10 min (rsdk re-runs from the start of the chroot phase).

### Hook 3 — `fastrpc` or `libcdsprpc1` not found
```
E: Unable to locate package fastrpc
```
**Cause:** Hook 2 silently failed to add the Radxa repo.
**Fix:** Verify Hook 2's output above. If `apt-get update` showed no Radxa
repo, the install URL changed — check `https://radxa-repo.github.io/`.
**Cost to retry:** ~10 min.

### Hook 8 — pip install fails
```
ERROR: Could not find a version that satisfies the requirement faster-whisper
```
**Cause:** PyPI is down, or the build host's network is flaky, or you're
behind a corporate proxy.
**Fix:** Test with `pip install --user faster-whisper` on the build host
itself. If that works, retry the build — pip in qemu sometimes hits transient
network issues.
**Cost to retry:** ~25 min (the build re-runs from the start of the chroot
phase). With `--debug` you can resume mid-build.

### Hook 10 — venv smoke test fails
```
[hook 10] CHECKPOINT — venv smoke test
ImportError: cannot import name 'Model' from 'openwakeword'
```
**Cause:** pip installed an incompatible openwakeword version, OR a
dependency was silently dropped because of `--no-deps`.
**Fix:** Pin a known-good openwakeword version in
`image_build/rsdk/src/share/rsdk/build/rootfs.jsonnet` hook 8 (e.g.
`openwakeword==0.6.0`).
**Cost to retry:** ~25 min.

### Hook 16 — `plymouth-set-default-theme: No such file or directory`
```
chroot: failed to run command 'plymouth-set-default-theme': No such file or directory
```
**Cause:** `plymouth-set-default-theme` was **removed** in Ubuntu Noble 24.04.
Plymouth theme selection now uses `update-alternatives`. Early versions of
hook 16 called the old command, which silently failed (caught by `|| true`)
and the theme was never actually selected — so Plymouth fell back to the
default spinner instead of the TrailCurrent branded splash.

**Fix:** Hook 16 now uses:
```bash
chroot "$1" update-alternatives --install \
    /usr/share/plymouth/themes/default.plymouth \
    default.plymouth \
    /usr/share/plymouth/themes/trailcurrent/trailcurrent.plymouth 200
chroot "$1" update-alternatives --set default.plymouth \
    /usr/share/plymouth/themes/trailcurrent/trailcurrent.plymouth
```
If you see this error, your `rootfs.jsonnet` is from before the fix. Pull
latest and rebuild.

**Cost to retry:** ~5–10 min (rsdk resumes from cache).

### Hook 16 — `update-initramfs` warning under qemu
```
update-initramfs: failed to determine canonical name for ...
```
**Cause:** initramfs-tools doesn't always run cleanly under qemu arm64
emulation on an x86 host.
**Effect:** Plymouth splash may not appear on first boot, but the rest of the
system boots fine. The hook logs a warning and continues.
**Fix:** Non-fatal — the hook already swallows this error. If you need a
working Plymouth splash, build on a native arm64 host, or accept the silent
boot (everything else works).
**Cost to retry:** N/A — non-fatal.

### Hook 29 — final checkpoint reports missing artifact
```
✗ MISSING: /home/trailcurrent/Llama3.2-1B-1024-v68/genie-t2t-run
```
**Cause:** Hook 5 failed silently.
**Fix:** Scroll back to find Hook 5's output. Most likely your NPU model
cache is corrupted.
```bash
rm -rf image_build/cache/npu-model
./image_build/preflight.sh --download-cache
```
**Cost to retry:** ~50 min (full rebuild).

### `rsdk-build` fails before any hooks run
```
mmdebstrap: E: cannot find keyring 'debian-archive-keyring.gpg'
```
**Cause:** rsdk keyrings not cloned.
**Fix:**
```bash
./image_build/preflight.sh
```
**Cost to retry:** <5 min.

### Hook fails with `PEREGRINE_STAGING not set`
```
exec: 3: PEREGRINE_STAGING: PEREGRINE_STAGING not set
E: setup failed: E: command failed: set -e
echo "[hook 5] staging NPU LLM model"
STAGING="${PEREGRINE_STAGING:?PEREGRINE_STAGING not set}"
```
**Cause:** `mmdebstrap` cleans its subprocess environment before invoking
customize-hooks, so `export`'d variables from `build.sh` don't reach the
hook. This was a real bug in early versions — hooks now use
`STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"` so the fallback
matches what `build.sh` stages to.

**Fix:** If you see this on a fresh checkout, your `rootfs.jsonnet` is from
before the fix. Pull latest, or replace every
`${PEREGRINE_STAGING:?PEREGRINE_STAGING not set}` with
`${PEREGRINE_STAGING:-/tmp/peregrine-staging}` and rebuild.

**Rule:** never rely on `export VAR` reaching a customize-hook. Either
hardcode the path, bake the value into the jsonnet source, or write a
file into the rootfs and read it back inside the hook.

**Cost to retry:** ~5–10 min (rsdk resumes from cache).

### Hook 28 is followed by `cannot create .../tmp/bdebstrap-output/manifest: Directory nonexistent`
```
I: running --customize-hook in shell: sh -c 'chroot "$1" dpkg-query -f=...'
exec: 1: cannot create /tmp/mmdebstrap.XXXXXX/tmp/bdebstrap-output/manifest: Directory nonexistent
```
**Cause:** `bdebstrap` appends its own post-customize manifest-generator
hook that writes `/tmp/bdebstrap-output/manifest` inside the rootfs via
shell redirection. That hook runs **after** all of your customize-hooks.
If any earlier hook does `rm -rf "$1"/tmp/*` or similar, bdebstrap's
redirect fails because its output directory no longer exists.

Early versions of hook 28 (golden image cleanup) did exactly this and
broke the build. Fixed by removing the `/tmp/*` wipe — mmdebstrap's own
final pass handles `/tmp` cleanup.

**Rule:** **never wipe `/tmp` or `/var/tmp` from a customize-hook.**
bdebstrap treats `/tmp/bdebstrap-output/` as its private workspace and
expects it to survive until the image is assembled. `mmdebstrap`'s
internal finalization drops `/tmp` for you anyway.

**Fix:** If you see this on a fresh checkout, your hook 28 is from before
the fix. Pull latest, or delete the line `rm -rf "$1"/tmp/* "$1"/var/tmp/*`
from hook 28 and rebuild.

**Cost to retry:** ~5–10 min (rsdk resumes from cache).

## Flash failures

### `lsusb` doesn't show `05c6:9008`
**Cause:** Board not in EDL mode.
**Fix:** Hold the EDL button while applying USB power. Verify with `lsusb`.

### `Permission denied` opening `/dev/bus/usb/...`
**Cause:** No udev rule for the EDL device.
**Fix:** Either run as root, or add the udev rule (see [03-flashing.md](03-flashing.md#step-0-allow-usb-access-to-the-edl-device)).

### `flash.sh --firmware` complains `rawprogram*.xml: No such file`
**Cause:** Firmware archive corrupted or wrong version.
**Fix:**
```bash
ls -lh image_build/firmware/
./image_build/preflight.sh
```
The archive must be `dragon-q6a_flat_build_wp_260120.zip` (~11 MB).

### `edl-ng binary not found inside edl-ng-dist.zip`
**Cause:** The `edl-ng-dist.zip` committed to `image_build/firmware/` is a
**double-wrapped zip** — the outer archive contains a single inner
`edl-ng-dist.zip` which holds the actual platform binaries
(`linux-x64/edl-ng`, `linux-arm64/edl-ng`, etc.). Early versions of
`flash.sh` only extracted the outer layer and couldn't find the binary.

**Fix:** `flash.sh` now detects and extracts the nested zip automatically.
If you see this error, your `flash.sh` is from before the fix — pull latest.
If the stale temp dir from a previous failed attempt exists, clean it first:
```bash
sudo rm -rf /tmp/peregrine-flash
sudo ./image_build/flash.sh --os <image>
```

### Flash hangs at 0%
**Cause:** USB cable is charge-only, NVMe not seated, or `edl-ng` is talking
to the wrong device.
**Fix:** Try a different USB-C cable, re-seat the M.2 module, verify
`lsusb | grep 9008` shows exactly one device.

### Flash succeeds but board drops to UEFI shell
```
Shell>
```
**Cause:** Most likely a **sector size mismatch**. The image was built with
`--sector-size 4096` but the NVMe uses 512-byte logical sectors (the norm
for consumer drives). UEFI sees the raw NVMe device but can't parse its
GPT — no filesystem entries appear in `map -r`, so there's no bootloader
to launch.

**Diagnosis:** At the `Shell>` prompt, run `map -r`. If you see `BLK` entries
for the NVMe but **no `FSx:` entries**, it's a sector size mismatch. If you
DO see `FSx:` entries, try `fs0:\EFI\BOOT\BOOTAA64.EFI` to boot manually —
the issue is boot order, not the image.

**Fix:** Rebuild with 512-byte sectors (the default since this bug was found).

**CRITICAL:** rsdk caches the guestfish disk-assembly script (`build-image`)
separately from the rootfs. Deleting only `output.img` is NOT enough — you
must also delete `build-image` or rsdk reuses the old sector size:
```bash
sudo rm -f image_build/rsdk/out/radxa-dragon-q6a_noble_cli/build-image
sudo rm -f image_build/rsdk/out/radxa-dragon-q6a_noble_cli/output.img
sudo ./image_build/build.sh
```
This only re-runs the image assembly step (~30 seconds, not the full
2-hour build). Then reflash:
```bash
sudo rm -rf /tmp/peregrine-flash
sudo ./image_build/flash.sh --os image_build/output/peregrine-q6a-v1.0.img
```

**Rule:** `build.sh` defaults to 512-byte sectors. Only use `--sector-size 4096`
if you know your specific NVMe drive requires it (enterprise / datacenter drives).

### Flash succeeds but board doesn't boot at all
**Cause:** Either the SPI NOR firmware was never flashed (you skipped Step 2),
or the OS image is corrupt.
**Fix:**
```bash
sudo ./image_build/flash.sh --firmware
sudo ./image_build/flash.sh --os image_build/output/peregrine-q6a-v1.0.img
```

## Boot / runtime failures

### Board never appears on the network
**Check:**
```bash
# From your laptop
ping peregrine.local
arp -a | grep -i radxa
```
If neither works, the board didn't reach the network. Possible causes:
1. Ethernet cable not connected
2. No DHCP server on your network
3. First-boot service crashed (no SSH host keys generated)
4. Kernel panic (need a serial console to see)

### `peregrine.local` doesn't resolve but the board IS pingable by IP
**Cause:** Your network blocks mDNS (corporate VLANs often do).
**Fix:** Use the IP directly. Find it via your DHCP server, or:
```bash
# Linux/macOS — scan a /24
sudo arp-scan --localnet | grep -i radxa
```

### `voice-assistant.service` keeps restarting
**Check:**
```bash
sudo journalctl -u voice-assistant -n 100 --no-pager
```
Common causes:
- **No audio device** → connect the Jabra Speak via USB
- **`Failed to load wake-word model`** → run `peregrine-self-test`, check `~/models/hey_peregrine.onnx`
- **`genie-server unreachable`** → `genie-server.service` is dead, see below

### `genie-server.service` won't start
```
Failed to start: device not found: /dev/fastrpc-cdsp
```
**Cause:** The Hexagon DSP isn't running. Either the SPI NOR firmware is
broken or the wrong version, or `libcdsprpc1` failed to install.
**Check:**
```bash
ls /dev/fastrpc*
cat /sys/class/remoteproc/*/state
dpkg -l libcdsprpc1
```
If `/dev/fastrpc-cdsp` doesn't exist, reflash the SPI NOR firmware.

### Wake word fires constantly on silence
**Cause:** The trained model is over-sensitive to ambient noise.
**Fix:** Edit `~/assistant.env` and raise the threshold:
```
WAKE_THRESHOLD=0.7
```
Then restart: `peregrine-restart`.

### Wake word never fires
**Cause:** Threshold too high, or microphone is muted/at zero gain.
**Check:**
```bash
arecord -d 5 -f S16_LE -r 16000 -c 1 /tmp/test.wav
aplay /tmp/test.wav
```
If you can't hear yourself, fix the audio chain first.

## Reverting and starting over

If you've messed up a board badly enough that you want a clean slate:

1. Re-enter EDL mode (hold EDL button while powering on)
2. Reflash both SPI NOR (`flash.sh --firmware`) and OS (`flash.sh --os`)
3. The board boots fresh — `firstboot.service` runs again, the wizard runs again

There's no concept of "rolling back" an OTA update yet — that's
[a planned feature](07-development.md#future-ota-updates).

## Getting more debug output

| What | Where |
|---|---|
| Build phase | `sudo ./image_build/build.sh 2>&1 \| tee /tmp/build.log` |
| First boot | `sudo journalctl -u peregrine-firstboot --no-pager` |
| Genie server | `sudo journalctl -u genie-server -f` |
| Voice assistant | `sudo journalctl -u voice-assistant -f` |
| All services since boot | `sudo journalctl -b 0 --no-pager` |
| Kernel | `dmesg \| tail -200` |
| NPU | `cat /sys/class/remoteproc/*/state /sys/class/remoteproc/*/firmware` |

## Next

→ [07-development.md](07-development.md) for the dev workflow without full rebuilds.
