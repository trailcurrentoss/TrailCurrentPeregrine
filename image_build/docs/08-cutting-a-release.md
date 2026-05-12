# 8. Cutting an image release

The earlier docs cover building and flashing as an operational task. This
one is the release-engineer view: how to take the current state of the
repo and produce a versioned, distributable Peregrine **image** — and how
to get it onto fielded boards.

> **Looking for software-only releases?** For changes scoped to `src/`,
> `models/`, or `config/*.service` that don't require a reflash, see
> [docs/software-releases.md](../../docs/software-releases.md). Image
> releases (this doc) are the only path for venv changes, OS package
> changes, NPU model updates, branding, or anything in `image_build/`.

## What a Peregrine release is

A release is three artifacts, all named after `PEREGRINE_VERSION`:

```
image_build/output/peregrine-q6a-v<VERSION>.img        # raw flashable image (~6 GB)
image_build/output/peregrine-q6a-v<VERSION>.img.xz     # compressed for download
image_build/output/peregrine-q6a-v<VERSION>.img.zst    # compressed (faster decompress)
```

plus a matching `v<VERSION>` git tag and a SHA256 of the raw `.img`. The
version string is also written into `/etc/peregrine-release` on every
flashed board so you can confirm what's running with one command.

The image is the heaviest unit of release. Source-only changes can be
shipped as a separate, lighter software release (see
[docs/software-releases.md](../../docs/software-releases.md)) — image
releases (this doc) and software releases run as parallel tracks against
the same git history.

## Versioning

Semantic-ish, two-component: `MAJOR.MINOR`.

| Bump | When |
|---|---|
| `MAJOR` | Breaking change to MQTT topics, voice-command grammar, on-disk layout, or hardware requirements |
| `MINOR` | New voice commands, new sensors, dependency bumps, model updates, bug fixes |

Pre-1.0 releases use `0.x`. Once shipped to a third party, never re-use
or rewrite a tag — cut a new minor.

## Pre-release checklist

Run through this before bumping the version:

- [ ] Working tree clean (`git status`)
- [ ] On `main` and up to date
- [ ] All changes you want in the release are committed
- [ ] Latest `src/` has been tested end-to-end on a real board via `deploy.sh`
- [ ] `models/hey_peregrine.onnx` is the model you intend to ship
       (no in-progress training artifacts)
- [ ] `image_build/cache/` is populated (`./image_build/preflight.sh --download-cache`)
- [ ] If any pip dep or apt package changed, plan to nuke `image_build/rsdk/out`
       (see [README cleanup table](../README.md#quick-reference))
- [ ] If `--sector-size` differs from the previous release, plan accordingly
- [ ] Free disk space — the build needs ~50 GB headroom in `image_build/rsdk/out`
- [ ] No other build, flash, or training job is running on the host

## Bump the version

The version lives in **one place** — `image_build/build.sh`:

```bash
# image_build/build.sh
PEREGRINE_VERSION="1.1"
```

The README and the image_build README both reference the current image
filename (`peregrine-q6a-v1.0.img`). Update those examples to match the
new version:

- `README.md` — the Quick-start block
- `image_build/README.md` — the TL;DR flash command

You can also pass `--version 1.1` on the command line for a one-off build
without editing `build.sh`, but for a real release commit the bump so the
default tracks the tag.

## Build the artifact

```bash
sudo ./image_build/build.sh
```

For a release you generally want a fresh rootfs to guarantee reproducibility:

```bash
sudo rm -rf image_build/rsdk/out
sudo ./image_build/build.sh
```

This is the full ~50-minute build. See [02-building-the-image.md](02-building-the-image.md)
for what the build does and what to watch for.

When it finishes you have:

```
image_build/output/peregrine-q6a-v1.1.img
```

The script prints its SHA256 at the end. **Save that** — it goes into the
release notes.

## Compress for distribution

The raw `.img` is ~6 GB. Two compressed flavors live alongside it:

```bash
cd image_build/output

# .xz — slow to compress, best ratio, universally available
xz -k -T 0 -9 peregrine-q6a-v1.1.img

# .zst — much faster decompress, smaller than gzip
zstd --keep -19 -T0 peregrine-q6a-v1.1.img
```

Flags worth knowing:

| Flag | Effect |
|---|---|
| `-k` / `--keep` | Keep the original `.img` (you need it to flash locally) |
| `-T 0` / `-T0` | Use all CPU threads |
| `-9` / `-19` | Maximum compression |

Expect ~2 GB for `.xz` and ~2.5 GB for `.zst`.

## Verify the image

Smoke test before publishing:

1. Flash a real board (see [03-flashing.md](03-flashing.md)) — preferably
   a board *not* used for daily development so you exercise the
   first-boot wizard from scratch.
2. SSH in, run the self-test:
   ```bash
   ssh trailcurrent@peregrine.local
   peregrine-self-test
   cat /etc/peregrine-release    # confirm PEREGRINE_VERSION matches
   ```
3. Verify the voice loop end-to-end: wake word → STT → LLM → TTS.
4. If MQTT is part of this release, point it at a broker and check that
   the voice commands listed in the top-level README still work.

If anything fails, **do not tag**. Fix, rebuild, re-verify.

## Tag and record the release

Once verification passes — these are commands for you to run, not me:

```bash
# In the project root
git tag -a v1.1 -m "Peregrine v1.1"
git push origin v1.1
```

Record the SHA256 of the raw `.img` in your release notes alongside the
tag so downstream users can verify their download.

## Distribute

The image artifacts are too large to live in git. They're published to
the Peregrine Releases folder on Google Drive:

**[Peregrine Releases (Google Drive)](https://drive.google.com/drive/folders/1GEdjCTWaRK4qn3HWSrRRwkeypdLBgr2B?usp=drive_link)**

For each release, upload:

- `peregrine-q6a-v<VERSION>.img.xz` (primary download)
- `peregrine-q6a-v<VERSION>.img.zst` (faster decompress alternative)
- A `peregrine-q6a-v<VERSION>.sha256` text file containing the SHA256 of
  the **raw** `.img` (the same hash `build.sh` prints at the end of the
  build)

Conventions for the Drive folder:

- One sub-folder per release named `v<VERSION>` (e.g. `v1.0/`, `v1.1/`)
- Do not delete prior releases — users may need them for rollback
- Sharing: link-accessible to anyone with the URL (read-only)

`flash.sh` accepts a path to either a `.img` or a decompressed `.img` —
users download the `.xz` / `.zst` from Drive and decompress locally
before flashing.

## Updating fielded boards

Two paths, depending on what changed:

| Scope of change | How to update |
|---|---|
| `src/`, wake-word model, service unit files | `./deploy.sh peregrine.local` — see [07-development.md](07-development.md). Survives reboot but is lost on reflash. |
| Anything else (OS packages, venv, NPU model, Piper voice, branding, hooks) | Full reflash with the new image — see [03-flashing.md](03-flashing.md). |

There is no in-place OS upgrade path today. A new image release means a
flash. The plumbing for OTA is sketched in
[07-development.md](07-development.md#future-ota-updates) but not built.

## Rollback

The previous `.img` lives in `image_build/output/` until you delete it.
To roll a board back to the prior release, flash that image again — the
NVMe is wiped, the wizard runs again, the user re-enters their MQTT
config. There is no partial rollback because there are no partial
releases.

Keep at least the most recent shipped `.img` (plus its `.xz` / `.zst`)
in a durable location outside the repo. The artifacts in
`image_build/output/` are gitignored and easy to lose.

## Next

You've reached the end of the docs. For the build-pipeline overview see
the top-level [README](../README.md).
