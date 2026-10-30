# Peregrine on ZimaBoard 2

This is the ZimaBoard 2 (Intel N150, 8 GB) target layer for Peregrine: the
**premium** option. It runs Llama 3.2 3B, three times the size of the Radxa
Dragon Q6A's 1B model, with eight times the conversation memory. It costs a
little more than the Q6A and draws more power. If you want the lowest cost or
run a really small battery bank, use the Q6A instead. The
[main README](../README.md#choose-your-board) compares the two boards.

Buy a ZimaBoard 2: **[ZimaBoard 2 at the Zimaspace shop](https://shop.zimaspace.com/products/zimaboard2-single-board-server?sca_ref=12564611.fDJvFR9EWxuoB4a&utm_source=dave-floyd&utm_medium=affiliate&utm_campaign=zimaspace_affiliate&utm_content=Dave-Floyd&sca_source=GitHub)**
(get the 8 GB model)

> **Affiliate link.** TrailCurrent earns a small percentage if you buy
> through this link. It costs you nothing extra.

The ZimaBoard runs the same `src/` as the Q6A. What differs is under this
directory:

- The LLM is llama.cpp's `llama-server` running Llama-3.2-3B-Instruct on
  three CPU cores. The iGPU (Vulkan) lane is built in, but i915 currently
  times out its work; see [docs/benchmarks.md](docs/benchmarks.md).
- [src/llm_bridge.py](src/llm_bridge.py) serves the same API on
  `127.0.0.1:11434` that `genie_server.py` serves on the Q6A. Neither
  `assistant.py` nor `web_chat.py` knows which board it runs on.
- All target-specific settings come from the unit files in [config/](config/).

The design is in [docs/ZimaBoard2/README.md](../docs/ZimaBoard2/README.md).
A plain-language account of the port and what it gained is in
[docs/port-summary.md](docs/port-summary.md).

## Quick start

Build on an x86_64 Linux machine with Docker (your user in the `docker`
group). No other packages are needed on the build host.

```bash
./zimaboard2/image/build.sh --version 1.0     # -> zimaboard2/image/output/peregrine-zimaboard2-1.0.iso
./zimaboard2/image/test-vm.sh                 # optional: install + boot it in QEMU
```

Write the ISO to a USB stick and boot the board from it. The full procedure,
from clone to a talking board, is in [docs/install.md](docs/install.md).

## Updating a board

`deploy.sh` at the repo root is for the Q6A only. It refuses to push to a
ZimaBoard, because it would replace the ZimaBoard's units with the Q6A's.
To update a ZimaBoard, build a new ISO and reinstall (see
[docs/install.md](docs/install.md#reinstalling)). A reinstall erases
`~/assistant.env`, so copy it off first. A new wake-word model can be copied
on directly; see the main README's "Deploying a New Model".

## Layout

| Path | What |
|---|---|
| `src/llm_bridge.py` | Genie-compatible API → `llama-server` |
| `config/` | systemd units, `llm.env.example`, ALSA, modprobe, netplan |
| `tools/peregrine-model` | List, switch, import, bench and remove GGUF models |
| `image/build.sh` | Host entry point. Runs the build in a pinned `ubuntu:24.04` container |
| `image/versions.env` | Every external pin (llama.cpp tag, GGUF, Whisper, Piper) with SHA-256 |
| `image/build-llama.sh` | llama.cpp (Vulkan + CPU), cached per tag |
| `image/build-rootfs.sh`, `image/hooks/` | mmdebstrap Noble amd64 rootfs, one hook per stage |
| `image/build-iso.sh`, `image/installer/` | Live installer (casper) and the hybrid UEFI ISO |
| `image/files/` | Board scripts adapted from `../image_build/files` |
| `image/test-vm.sh` | QEMU/OVMF install-and-boot test |
| `docs/install.md` | Installing on a board |
| `docs/benchmarks.md` | Measured LLM speed and memory on the board |
| `docs/port-summary.md` | Plain-language summary: what changed, what it gained, Q6A comparison |

## Runtime overview

| Unit | Runs | User |
|---|---|---|
| `peregrine-llm` | `llama-server`, 3 threads on CPUs 1–3 (CPU lane; set in `llm.env`) | `peregrine-llm` (locked) |
| `peregrine-llm-bridge` | `llm_bridge.py` on :11434 | `peregrine-llm` |
| `voice-assistant` | `assistant.py`, CPUs 0–2 | `trailcurrent` |
| `peregrine-chat` | `web_chat.py`, HTTPS UI and `/api/voice` | `trailcurrent` |
| `peregrine-llm-gate.socket` | Yield gate: freezes the LLM while a voice command runs | root |
| `peregrine-power` | EPP, USB autosuspend (not audio/HID), SATA ALPM, optional iGPU cap | root |
| `peregrine-firstboot` | machine-id, grow root, per-board web CA | root |
