# TrailCurrent Peregrine — Image Build

Build a flashable Radxa Dragon Q6A image with the Peregrine voice assistant
fully baked in. Flash, boot, log in once — done.

```
                /\
               /  \              /\
              /    \            /  \
             /      \          /    \
            /  /\    \        /      \
           /  /  \    \      /        \
          /  /    \    \    /          \
         /  /      \    \  /            \
    ____/  /        \    \/              \____

    TrailCurrent Peregrine  |  Voice Assistant
```

## TL;DR

```bash
# 1. One-time build host setup
sudo apt install -y jsonnet bdebstrap libguestfs-tools \
    qemu-user-static binfmt-support device-tree-compiler \
    gdisk parted git curl gpg pipx rsync unzip
./image_build/preflight.sh --download-cache

# 2. Build the image (~30-50 min, defaults to 512-byte sectors)
#    If your NVMe uses 4096-byte sectors: add --sector-size 4096
sudo ./image_build/build.sh

# 3. Put board in EDL mode (hold EDL button while powering on USB-C)
lsusb | grep 9008   # verify

# 4. Flash SPI NOR firmware (one-time per board)
sudo ./image_build/flash.sh --firmware

# 5. Flash the OS image to NVMe
sudo ./image_build/flash.sh --os image_build/output/peregrine-q6a-v1.0.img

# 6. Disconnect USB, connect Ethernet + 12V power, wait ~3 min

# 7. SSH in and run the wizard
ssh trailcurrent@peregrine.local
# Default password: trailcurrent
# (the wizard forces a change on first login)

# 8. Say "hey peregrine"
```

## What you get

A board that out of the box does:

- Wake-word detection (custom `hey_peregrine` model on CPU)
- STT via faster-whisper (`base.en`, INT8 quantized)
- LLM via Llama 3.2 1B running on the Hexagon DSP NPU (~12 tok/s)
- TTS via Piper (`en_US-libritts_r-medium`)
- All four piped together as a continuous conversation loop
- Optional MQTT telemetry to a TrailCurrent broker
- TrailCurrent-branded boot splash and SSH MOTD
- Hardened minimal CLI Ubuntu 24.04 — no desktop, no WiFi, no Snap

Everything runs on-device. No cloud dependency for the core loop.

## Project layout

```
image_build/
├── README.md              You are here
├── docs/                  Detailed walkthroughs (7 files, read in order)
├── build.sh               Top-level build orchestrator
├── preflight.sh           Build host setup verification + cache download
├── flash.sh               Wrapper around edl-ng for SPI NOR + NVMe flashing
├── rsdk/                  Vendored Radxa SDK with Peregrine rootfs.jsonnet
├── firmware/              SPI NOR firmware (committed to repo)
├── files/                 Static files baked into the image
│   ├── plymouth/          TrailCurrent boot splash theme
│   ├── motd/              SSH MOTD ASCII art + console issue
│   ├── systemd/           Image-only systemd units (firstboot, power-save)
│   ├── scripts/           First-boot, first-login wizard, self-test
│   ├── sysctl/            Kernel tuning
│   ├── modprobe/          WiFi blacklist
│   ├── ssh/               sshd_config drop-in
│   ├── env/               assistant.env template
│   └── profile/           Branded shell prompt + aliases
├── cache/                 NPU model + Piper voice (gitignored, ~3.2 GB)
└── output/                Built images (gitignored, ~6 GB each)
```

## Documentation

Read the docs in order:

1. [Build host setup](docs/01-build-host-setup.md) — what to install on your laptop
2. [Building the image](docs/02-building-the-image.md) — what `build.sh` does and how long it takes
3. [Flashing](docs/03-flashing.md) — EDL mode, SPI NOR, NVMe flash
4. [First boot](docs/04-first-boot.md) — what the board does in the first 3 minutes
5. [First login](docs/05-first-login.md) — the wizard, password change, MQTT, self-test
6. [Troubleshooting](docs/06-troubleshooting.md) — when things go wrong
7. [Development](docs/07-development.md) — `deploy.sh` for fast iteration on `src/`

## Total time, fresh board to working assistant

| Step | First time | Subsequent boards |
|---|---|---|
| Build host setup | ~15 min | 0 (one-time) |
| Cache download | ~10 min | 0 (cached) |
| Image build | ~50 min | ~5 min (cached rootfs) |
| Flash SPI NOR | ~1 min | 0 (one-time per board) |
| Flash OS | ~5 min | ~5 min |
| First boot + first login | ~5 min | ~5 min |
| **Total** | **~85 min** | **~15 min** |

## What's in the image (sizes approximate)

| Component | Size |
|---|---|
| Ubuntu Noble 24.04 base | ~600 MB |
| Python venv (faster-whisper, openwakeword, piper, mqtt, etc.) | ~1.5 GB |
| NPU LLM model (Llama 3.2 1B INT8) | ~3.2 GB |
| Piper voice (libritts_r medium) | ~75 MB |
| Wake-word model | ~850 KB |
| **Total uncompressed image** | **~6 GB** |

The image is sized to fit a 128 GB NVMe with plenty of headroom for logs,
training data updates, and OTA staging.

## Default credentials

| Setting | Value |
|---|---|
| Username | `trailcurrent` |
| Password | `trailcurrent` (forced change on first login) |
| Hostname | `peregrine` |
| mDNS | `peregrine.local` |
| Root login | Disabled (`passwd -l root`) |
| Admin access | sudo via `trailcurrent` |

## Cleanup between builds

`build.sh` is designed to be re-runnable. Most rebuilds don't require any
manual cleanup — just run it again. But there are four distinct scenarios
with different cleanup needs, ordered from lightest to heaviest.

### Scenario 1 — Iterating on a hook or static file (fastest)

You changed something in `image_build/files/`, `image_build/rsdk/src/share/rsdk/build/rootfs.jsonnet`,
`src/`, or `config/`. Just re-run the build:

```bash
sudo ./image_build/build.sh
```

rsdk will re-run all the customize-hooks against a cached rootfs tarball.
Takes ~5–10 minutes because it skips `mmdebstrap` and the pip install.

**No cleanup needed.** `build.sh` already `rm -rf`'s its own staging dir
(`/tmp/peregrine-staging`) at the start of every run.

### Scenario 2 — Previous build failed mid-hook

Something exploded in a hook, the build stopped, and you fixed the root
cause. Re-run:

```bash
sudo ./image_build/build.sh
```

If rsdk's cache is intact it will pick up from where it left off. If it
complains about a corrupted cache state, nuke just the rsdk output:

```bash
sudo rm -rf image_build/rsdk/out/radxa-dragon-q6a_noble_cli
sudo ./image_build/build.sh
```

This forces a full re-run of `mmdebstrap` + all hooks (~30–50 min) but
leaves the build cache (NPU model, Piper voice) untouched.

Also clean up any abandoned mmdebstrap temp dirs from a killed build:

```bash
sudo rm -rf /tmp/mmdebstrap.* /tmp/peregrine-staging
```

### Scenario 2b — Changed `--sector-size` (critical!)

**rsdk caches the guestfish disk-assembly script (`build-image`)
separately from the rootfs tarball.** If you change `--sector-size` between
builds, the cached script still uses the OLD sector size. The rootfs is fine —
only the disk-assembly step needs to be regenerated.

```bash
# Delete ONLY the cached guestfish script and output image
sudo rm -f image_build/rsdk/out/radxa-dragon-q6a_noble_cli/build-image
sudo rm -f image_build/rsdk/out/radxa-dragon-q6a_noble_cli/output.img

# Rebuild — only regenerates the disk assembly (~30 seconds)
sudo ./image_build/build.sh
```

**Symptom if you skip this:** the board drops to a UEFI `Shell>` prompt
because the GPT was written with the old sector size. UEFI can't parse the
partition table correctly.

### Scenario 3 — Bumping a Python dependency or package version

You added a new package to `rootfs.jsonnet` hook 8, pinned a version, or
`pip` cached a broken wheel. The rootfs tarball cache is stale — rsdk
won't notice because it fingerprints on the build config, not on
`apt install` results.

```bash
# Nuke the entire rsdk build tree (forces re-bootstrap of the rootfs)
sudo rm -rf image_build/rsdk/out

# Re-run
sudo ./image_build/build.sh
```

Full ~50 min rebuild. The `cache/` directory is preserved so you don't
re-download the NPU model.

### Scenario 4 — Full clean slate

You want to verify reproducibility, you're burning cache to trace a weird
bug, or a new Peregrine version bumps the NPU model.

```bash
# 1. Remove everything the build generates
sudo rm -rf image_build/rsdk/out
sudo rm -rf image_build/output
rm -rf image_build/cache

# 2. Also remove any stale staging / mmdebstrap temp
sudo rm -rf /tmp/peregrine-staging /tmp/mmdebstrap.*

# 3. Re-download the cache (~10 min, ~3.2 GB)
./image_build/preflight.sh --download-cache

# 4. Full rebuild (~50 min)
sudo ./image_build/build.sh
```

This is the **~85-minute-from-scratch** path. Use it when you want to be
sure nothing stale is lingering.

### Quick reference

| Situation | What to nuke | Rebuild time |
|---|---|---|
| Tweaked a hook or file in `files/` | nothing | ~5–10 min |
| Previous build hit an error mid-hook | `…/out/radxa-dragon-q6a_noble_cli` + `/tmp/peregrine-staging` + `/tmp/mmdebstrap.*` | ~30–50 min |
| **Changed `--sector-size`** | `…/out/radxa-dragon-q6a_noble_cli/build-image` + `output.img` | **~30 sec** |
| Bumped Python dep or apt package | `image_build/rsdk/out` | ~50 min |
| Previous flash failed or was interrupted | `/tmp/peregrine-flash` (needs `sudo rm -rf`) | instant |
| Cache corruption or new model version | `image_build/rsdk/out` + `image_build/output` + `image_build/cache` + `/tmp/peregrine-staging` + `/tmp/mmdebstrap.*`, then re-preflight | ~85 min |

### Disk space recovery

After a successful build, `image_build/rsdk/out` balloons to ~50 GB. You
can free it any time — the next build will re-create what it needs:

```bash
sudo rm -rf image_build/rsdk/out     # frees ~50 GB
# Leaves image_build/output/*.img intact (your flashable artifact)
# Leaves image_build/cache/ intact (your 3.2 GB NPU model cache)
```

The final `.img` in `image_build/output/` is the only artifact you need
to keep around to flash more boards — everything else is rebuildable.
