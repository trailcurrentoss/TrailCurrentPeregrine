# 7. Development workflow

The image build pipeline is the **canonical** way to install Peregrine on a
fresh board. But once you have a board running, you don't want to rebuild
and reflash the whole image every time you tweak `assistant.py`. This doc
covers the lighter-weight dev cycle.

## The two install paths

| Path | When to use | Time per cycle |
|---|---|---|
| **Image build + flash** (`build.sh` + `flash.sh`) | New board, OS package changes, model updates, dependency changes | ~50 min build + ~10 min flash |
| **deploy.sh** | Iterating on `src/assistant.py` or `src/genie_server.py` on a running board | ~5 sec |

`deploy.sh` is a development tool. **It is not part of the production install
path.** Anything you change with `deploy.sh` will be lost the next time you
reflash an image — that's a feature, not a bug. The image build is the
single source of truth.

## Using `deploy.sh`

```bash
./deploy.sh peregrine.local
```

This SCPs the following files to the board:
- `src/assistant.py` → `/home/trailcurrent/assistant.py`
- `src/genie_server.py` → `/home/trailcurrent/genie_server.py`
- `models/hey_peregrine.onnx` → `/home/trailcurrent/models/hey_peregrine.onnx`
- `config/voice-assistant.service` → `/etc/systemd/system/voice-assistant.service`
- `config/genie-server.service` → `/etc/systemd/system/genie-server.service`

Then it runs `systemctl daemon-reload`. After deploy, restart manually:

```bash
ssh trailcurrent@peregrine.local sudo systemctl restart voice-assistant
ssh trailcurrent@peregrine.local sudo journalctl -u voice-assistant -f
```

The first SSH connection prompts for the trailcurrent user's password (the
one you set during the first-login wizard). Subsequent commands reuse the
same connection via SSH ControlMaster.

## Setting up SSH keys (skip the password prompt)

Once you've gone through the first-login wizard:

```bash
# On your laptop
ssh-copy-id trailcurrent@peregrine.local
```

Now `deploy.sh` runs without any password prompts.

## What requires a full rebuild

You need to rebuild the image (not just `deploy.sh`) when you change:

- Anything in `image_build/` (hooks, scripts, services that aren't in `config/`)
- The Python venv (new pip dependency, version pin)
- The NPU LLM model
- The Piper TTS voice
- System packages
- Plymouth theme
- Branding
- The `trailcurrent` user setup

`deploy.sh` does NOT touch any of those — it's strictly for application code.

## Iterating on the wake-word model

The wake-word training pipeline lives in `training/openwakeword-trainer/`.
After re-training:

```bash
# 1. Copy the new ONNX into models/
cp training/openwakeword-trainer/output/hey_peregrine.onnx models/

# 2. Push to the board
./deploy.sh peregrine.local

# 3. Restart
ssh trailcurrent@peregrine.local sudo systemctl restart voice-assistant
```

You don't need to rebuild the image to test a new wake-word model. Once the
model is dialed in, rebuild the image to bake in the new version for
fresh-board flashes.

See `training/openwakeword-trainer/README.md` for the training pipeline.

## Iterating on the LLM prompts / behavior

Most assistant behavior changes are in `src/assistant.py`:

```bash
# Edit, push, restart in one shot:
$EDITOR src/assistant.py
./deploy.sh peregrine.local
ssh trailcurrent@peregrine.local 'sudo systemctl restart voice-assistant && sudo journalctl -u voice-assistant -f'
```

## Future: OTA updates

The current dev workflow is `deploy.sh` + manual restart. The longer-term
plan is OTA — `deploy.sh` becomes a dev tool only, and production boards
poll a release server for new image versions.

The plumbing for this isn't built yet. The image build pipeline is the
foundation: every image has a `/etc/peregrine-release` with `PEREGRINE_VERSION`,
which an OTA agent could check against a manifest. The build script's
`--version` flag exists specifically to support this.

## Building for a different board variant

The current pipeline targets Radxa Dragon Q6A specifically. To target a
different board you would need to:

1. Add the product to `image_build/rsdk/src/share/rsdk/configs/products.json`
2. Possibly fork `rootfs.jsonnet` for product-specific hooks
3. Verify the NPU model works on the new SoC

This is out of scope for v1.0 — Peregrine is Q6A-only.

## Cleaning up

```bash
# Remove all build artifacts (force a clean rebuild)
sudo rm -rf image_build/rsdk/out image_build/output

# Remove the cache (force re-download)
rm -rf image_build/cache
./image_build/preflight.sh --download-cache
```

## Next

You've reached the end of the docs. For the build pipeline overview see
the top-level [README](../README.md).
