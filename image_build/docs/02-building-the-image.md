# 2. Building the image

## Quick command

```bash
sudo ./image_build/build.sh
```

That's it. The build takes **30–50 minutes** on the first run and produces
`image_build/output/peregrine-q6a-v1.0.img`.

## What the build does

`build.sh` runs three phases:

1. **Preflight** (~5 sec)
   Re-runs `preflight.sh` silently. Fails fast if the build host is not ready.

2. **Stage** (~30 sec)
   Copies `src/`, `config/`, `models/`, `image_build/files/`, and
   `image_build/cache/` into `/tmp/peregrine-staging/` so the rsdk customize
   hooks can find them inside the chroot.

3. **rsdk-build** (~30–50 min)
   Invokes `rsdk-build radxa-dragon-q6a noble cli` which:
   - Builds an Ubuntu Noble (24.04) arm64 rootfs from scratch via `mmdebstrap`
   - Runs all 29 Peregrine customize-hooks (numbered `[hook 1]` through `[hook 29]`) inside a qemu-arm64 chroot
   - Assembles the final disk image with `guestfish`

After success the output image is copied to
`image_build/output/peregrine-q6a-v1.0.img`.

## Watch for the checkpoints

Two hooks are designed to **fail fast** so you don't wait 50 minutes only to
discover a problem at the end.

### Hook 10 — venv smoke test (~25 min into build)

```
[hook 10] CHECKPOINT — venv smoke test
  openwakeword OK
  faster_whisper import OK
  all dependencies importable
  ✓ venv smoke test passed
```

If this fails, the most common causes are:

| Symptom | Likely cause | Fix |
|---|---|---|
| `ModuleNotFoundError: openwakeword` | `--no-deps` install fetched a broken wheel | Re-run the build (transient PyPI hiccup) |
| Resource model 404 | openWakeWord upstream changed download URL | Update openwakeword version pin |
| Compile error during pip install | Missing apt build dep | Add the missing `-dev` package to the rsdk packages list |

### Hook 29 — final artifact verification (~50 min into build)

```
[hook 29] CHECKPOINT — final artifact verification
  ✓ /home/trailcurrent/assistant.py
  ✓ /home/trailcurrent/genie_server.py
  ...
  ✓ All artifacts present — image is ready
```

This is the last gate before `guestfish` builds the final disk image. If
anything is missing here, the corresponding earlier hook silently failed —
scroll up in the log to find the `[hook N]` line that didn't print its
expected output.

## Build options

```bash
# Custom version string (written into /etc/peregrine-release)
sudo ./image_build/build.sh --version 1.1

# Legacy 512-byte sector size (most NVMe drives use 4096)
sudo ./image_build/build.sh --sector-size 512

# rsdk debug mode — keeps the rootfs as a directory between runs
sudo ./image_build/build.sh --debug
```

## Iterating on a single hook

If you're tweaking a hook and don't want to wait for a full rebuild:

1. Run with `--debug` once to keep the rootfs around
2. Edit the hook in `image_build/rsdk/src/share/rsdk/build/rootfs.jsonnet`
3. Manually `chroot` into `image_build/rsdk/out/radxa-dragon-q6a_noble_cli/rootfs/`
   and run your hook content by hand
4. When happy, run a full rebuild

For most changes a clean rebuild is simpler — most hooks are <30 sec each;
only the venv (hook 8) is genuinely slow.

## Build time breakdown

Approximate timings on a modern x86_64 host (16-core, NVMe):

| Phase | Time |
|---|---|
| mmdebstrap (rootfs base) | 6–10 min |
| Hook 2-3 (apt repos + NPU pkgs) | 1 min |
| Hook 5-7 (cache → chroot copies) | 1–2 min |
| Hook 8 (pip install in qemu) | 15–25 min |
| Hook 9-10 (resource models + smoke test) | 2–3 min |
| Hook 11-28 (file copies, services, cleanup) | 1 min |
| guestfish (assemble disk image) | 3–5 min |

The pip install step (hook 8) is by far the slowest because pip runs under
qemu-arm64 emulation. There's no good way to speed this up short of building
on an actual arm64 host.

## When the build fails

The first thing to do is **scroll back to find the last `[hook N]` line**.
Every hook logs its number when it starts, so you can see exactly which one
exploded. Then look at [06-troubleshooting.md](06-troubleshooting.md).

## Next

After a successful build → [03-flashing.md](03-flashing.md)
