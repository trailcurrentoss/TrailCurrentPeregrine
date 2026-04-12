# 1. Build host setup

The build host is a Debian or Ubuntu Linux machine (native arm64 or x86_64
with QEMU). It needs ~80 GB of free disk and a working internet connection.

This setup runs once. Re-running is safe.

## Install build dependencies

```bash
sudo apt update
sudo apt install -y \
    jsonnet \
    bdebstrap \
    libguestfs-tools \
    qemu-user-static \
    binfmt-support \
    device-tree-compiler \
    gdisk \
    parted \
    git \
    curl \
    gpg \
    pipx \
    rsync \
    unzip
```

| Package | Purpose |
|---|---|
| `jsonnet` | Compiles rsdk's `.jsonnet` build templates |
| `bdebstrap` | YAML frontend for `mmdebstrap` — builds the rootfs |
| `libguestfs-tools` | Provides `guestfish` for assembling the disk image |
| `qemu-user-static` + `binfmt-support` | ARM64 emulation for cross-compilation on x86_64 |
| `device-tree-compiler` | Provides `dtc` (used by some rsdk hooks) |
| `gdisk` / `parted` | GPT partition manipulation |
| `pipx` | Installs `modelscope` in an isolated env (used by preflight to download the NPU model) |

## Verify QEMU binfmt is active

```bash
ls /proc/sys/fs/binfmt_misc/qemu-aarch64
```

Should print a path. If not:

```bash
sudo systemctl restart binfmt-support
```

## Run preflight

The preflight script verifies your build host is ready, clones the four
upstream keyring repos that `mmdebstrap` needs, downloads the NPU LLM model
(~3 GB) and the Piper TTS voice (~75 MB) into `image_build/cache/`.

```bash
cd /path/to/TrailCurrentPeregrine
./image_build/preflight.sh --download-cache
```

Expected output:

```
TrailCurrent Peregrine — Build Host Preflight

── 1. APT build dependencies ──
  ✓ jsonnet
  ✓ bdebstrap
  ✓ guestfish
  ...

── 3. rsdk keyring repos ──
  cloning debian...
  ✓ debian keyring
  ...

── 8. Build cache ──
  warn NPU model cache missing — downloading via modelscope
  ...
  ✓ NPU model downloaded (3.2G)
  ✓ Piper voice downloaded

Preflight passed — ready to run sudo ./image_build/build.sh
```

The first run downloads ~3 GB and takes 5–10 minutes depending on your
connection. Subsequent runs are <5 seconds (it skips anything already cached).

## Disk space

| Location | Size | Notes |
|---|---|---|
| `image_build/cache/` | ~3.2 GB | NPU model + Piper voice (re-usable across builds) |
| `image_build/rsdk/externals/keyrings/` | ~3 MB | Cloned keyring repos |
| `image_build/rsdk/out/` | ~50 GB | rsdk build artifacts (rootfs tarballs, intermediate images) |
| `image_build/output/` | ~6 GB | Final flashable image |

**Total: ~60 GB** for a full build cycle. The `out/` and `output/` directories
are gitignored.

## Next

Once preflight passes, build the image — see [02-building-the-image.md](02-building-the-image.md).
