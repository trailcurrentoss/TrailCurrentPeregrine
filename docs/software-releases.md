# Software releases

How to package a change to `src/`, models, or service files and push it to
fielded Peregrine boards **without** reflashing the image.

This is a parallel release track to the image release process in
[image_build/docs/08-cutting-a-release.md](../image_build/docs/08-cutting-a-release.md).
The two tracks differ in scope:

| Track | Touches | Tool | Time | Required when |
|---|---|---|---|---|
| **Software release** (this doc) | `src/`, `models/`, `config/*.service` | `deploy.sh` over SSH | ~10 sec/board | Code-only or model-only changes |
| **Image release** ([08-cutting-a-release.md](../image_build/docs/08-cutting-a-release.md)) | Everything (OS, venv, NPU model, Piper voice, branding, hooks) | `build.sh` + `flash.sh` | ~50 min + ~10 min flash per board | Anything outside the software-release scope |

For pure iteration during development, use `deploy.sh` ad-hoc — see
[image_build/docs/07-development.md](../image_build/docs/07-development.md).
This doc is about turning a tested change into a labelled release that
goes out to every board in the field.

## What a software release contains

`deploy.sh` pushes exactly this set (and nothing else):

- `src/assistant.py`
- `src/tts.py`
- `src/genie_server.py`
- `models/hey_peregrine.onnx` (+ `.data`)
- `config/voice-assistant.service`
- `config/genie-server.service`
- `image_build/files/systemd/cpu-performance.service`
- A `pip install --force-reinstall --no-deps openwakeword`
- A `pip install timezonefinder`

If your change is not in that list, it is **not** a software release —
cut a new image instead. Examples that require a new image: new Python
dependency, version bump on an existing dep (other than the two above),
new NPU model, Piper voice change, new systemd unit, kernel/sysctl
change, anything in `image_build/files/`.

## Versioning

Software releases extend the image version with a `PATCH` component:

```
<MAJOR>.<MINOR>.<PATCH>
^^^^^^^^^^^^^^         image version baked into /etc/peregrine-release
              ^^^^^^^^ software-only revision deployable to that image
```

| Bump | Example | When |
|---|---|---|
| `PATCH` | `1.0.0` → `1.0.1` | Bug fix, wake-word model update, prompt tweak, voice-command additions |
| `MINOR` / `MAJOR` | `1.0.x` → `1.1.0` | Anything that requires a new image — cut an image release, not a software release |

A board running image `v1.0` can take any `1.0.PATCH` software release.
It cannot take `1.1.x` without being reflashed. Keep this invariant or
boards get into states `deploy.sh` can't reach.

There is no software-version constant in the source today, and no
on-board record of which PATCH a given board is running. The git tag is
the source of truth for "what was released" and the deploy log (see
below) is the source of truth for "what landed on which board."

## Pre-release checklist

- [ ] Working tree clean (`git status`)
- [ ] On `main`, up to date
- [ ] Changes are scoped to the software-release set above
- [ ] Tested on a dev board via `deploy.sh` for at least one full
       conversation cycle (wake → STT → LLM → TTS)
- [ ] If the wake-word model changed, false-positive rate sanity-checked
       (board sits idle for a few minutes without spurious wakes)
- [ ] If MQTT command surface changed, verified against a real broker
- [ ] No in-progress training artifacts in `models/`

## Cut the release

```bash
# 1. Confirm everything intended for the release is committed on main.
git status
git log -10 --oneline

# 2. Tag.
git tag -a v1.0.1 -m "Peregrine software v1.0.1 — <one-line summary>"
git push origin v1.0.1
```

There is no build artifact to publish — the "artifact" is the git tag.
`deploy.sh` pushes whatever is in the working tree, so a deployer just
checks out the tag before running it.

The Google Drive release folder
([from 08-cutting-a-release.md](../image_build/docs/08-cutting-a-release.md#distribute))
is **not used** for software releases. It's image-only.

## Deploy to a fielded board

For each board:

```bash
# Make sure the working tree matches the released tag.
git checkout v1.0.1

./deploy.sh peregrine.local
ssh -t trailcurrent@peregrine.local sudo systemctl restart voice-assistant
```

The restart kicks `voice-assistant` only. `genie-server` only needs a
restart if `genie_server.py` or `genie-server.service` changed:

```bash
ssh -t trailcurrent@peregrine.local sudo systemctl restart genie-server
```

For multiple boards, loop:

```bash
git checkout v1.0.1
for host in peregrine-shop.local peregrine-truck.local peregrine-shed.local; do
    echo "=== $host ==="
    ./deploy.sh "$host" || { echo "FAIL on $host"; break; }
    ssh -t "trailcurrent@$host" 'sudo systemctl restart voice-assistant'
done
```

A `break` on failure is intentional — diagnose before continuing. A
half-rolled-out release with mixed versions across boards is the worst
outcome.

## Verify on each board

```bash
ssh trailcurrent@peregrine.local

# Confirm the service is healthy
systemctl status voice-assistant --no-pager
sudo journalctl -u voice-assistant -n 50 --no-pager

# Run the on-board self-test
peregrine-self-test

# Confirm the image version is what you expected (PATCH is not recorded;
# this only shows MAJOR.MINOR)
cat /etc/peregrine-release
```

Then say "hey peregrine" and run through whatever flow the release
changed. If anything's off, roll back before moving to the next board.

## Recording what landed where

Today this is manual. Two practical options:

1. **A deploy log file** in the repo (gitignored or a private branch),
   one line per `host,tag,timestamp,operator`.
2. **A small marker file on the board** — at the end of a deploy, write
   the tag into `~/peregrine-src-release` on the target:
   ```bash
   ssh "trailcurrent@$host" "echo 'v1.0.1' > /home/trailcurrent/peregrine-src-release"
   ```
   Then `cat ~/peregrine-src-release` answers "what's on this board?"
   without needing the deployer's notes.

Neither is wired into `deploy.sh` itself. If you find yourself doing
many software releases across many boards, that's the obvious next
automation step.

## Rollback

`deploy.sh` is non-atomic — it overwrites files in place. There is no
automatic rollback. To revert a fielded board to the previous software
release:

```bash
git checkout v1.0.0       # the previous tag
./deploy.sh peregrine.local
ssh -t trailcurrent@peregrine.local sudo systemctl restart voice-assistant
```

If the release also bumped the wake-word model and the new model
turned out to be worse, that's covered by the same flow — `deploy.sh`
ships the model from the working tree, so checking out the old tag
ships the old model.

If `voice-assistant` won't start on the new release (crash loop), the
service log will tell you:

```bash
ssh trailcurrent@peregrine.local 'sudo journalctl -u voice-assistant -n 200 --no-pager'
```

If `deploy.sh` itself broke partway through (e.g. SSH dropped between
copying `assistant.py` and installing the service file), you can re-run
it — it's idempotent.

## When in doubt: cut an image release

Software releases are a deliberate optimization for the common case
(code changes, wake-word retrains). Anything that doesn't cleanly fit
the software-release set above should go through the full image
release. The cost is ~60 minutes per board on first flash vs. ~10
seconds per board on `deploy.sh`, but the image release is the only
mechanism that can update the venv, the NPU model, or the OS — and a
fielded board diverging from the image baseline in ways `deploy.sh`
can't capture is technical debt you don't want.
