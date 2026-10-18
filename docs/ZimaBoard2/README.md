# Peregrine on ZimaBoard 2 — Port Plan (rev 2.1)

**Status:** Plan only. Nothing here is implemented. The maintainer has answered
the blocking questions (§9); implementation starts on approval of this revision.

**Goal:** Run Peregrine on a
[ZimaBoard 2](https://shop.zimaspace.com/products/zimaboard2-single-board-server)
(Intel N150, 8 GB LPDDR5, Intel UHD iGPU, 2× USB 3.1, eMMC, Ubuntu Server
24.04, no PCIe card). The requirements are:

1. **No fast-path regression.** The chain wake word → endpoint → STT → intent →
   MQTT → Piper confirmation (e.g. *"hey Peregrine, turn on all the lights"*)
   must be as fast as on the Q6A or faster. That includes while the LLM is
   generating an answer.
2. **A smarter LLM at the same or a better rate.** A 3–4B GGUF model served by
   `llama-server` (llama.cpp) replaces Genie and Llama 3.2 1B. Targets:
   ≥ 10 tok/s generation, 8K context, thinking disabled.
3. **Minimal power.** Idle draw within ~3 W of the Q6A build, measured on the
   same meter.
4. **Everything else unchanged.** MQTT over TLS, named devices from the
   TrailCurrent UI, web chat, the voice terminal, hardening, an idempotent
   build and a reproducible image.

### Revision history

| Rev | Date | Summary |
|---|---|---|
| 1 | 2026-10-14 | Isolated `zimaboard2/` fork; LLM on **CPU via Ollama**; model variety as the main goal (two role models, `peregrine-model` CLI, Ollama Modelfiles) |
| 2 | 2026-10-16 | LLM moves to the **iGPU via llama.cpp `llama-server` (Vulkan)** so the four CPU cores stay free for the fast path. **Shared `src/`** with per-target hardware layers replaces the full fork. Idle power and fast-path latency become hard acceptance gates. Model variety is kept, reduced to "swap a GGUF file" |
| 2.1 | 2026-10-16 | **This plan.** Maintainer answers applied (§9). A ZimaBoard-only **Genie-compatible bridge** in front of `llama-server` means the shared code needs **one** env-gated change, in `src/assistant.py`. The shared `llm_client.py` and the `web_chat.py` edits are dropped. Web chat **always yields** to a voice prompt. 8 GB SKU. Hostname stays `peregrine`. MIT models preferred |

**Sharing rule (maintainer, 2026-10-16).** The ZimaBoard shares the Q6A code
only while the Q6A-visible footprint stays at **`deploy.sh` plus one or two
other files**. If implementation ever needs more than that, stop and switch
to a separate fork (rev 1 layout) instead. With the bridge (§2.3), the
footprint is **one file: `src/assistant.py`**. `deploy.sh` does not change,
because no new shared file is added. Nothing under `config/` or
`image_build/` changes either.

What rev 2 keeps from rev 1:

- the baseline requirements table (§1.3)
- the LLM-interface gap analysis (G1–G9, now absorbed by a ZimaBoard-only bridge, §2.3)
- the licence rule for baked-in models
- the x86 image pipeline (mmdebstrap + hooks + QEMU/OVMF boot test)
- the install options (eMMC via live USB, or SATA)
- the BIOS checklist and the risk register

What rev 2 drops:

- Ollama. `llama-server` is a single binary with no model registry and no
  Modelfiles to maintain, and it has a working Vulkan backend for the iGPU.
- Separate voice and chat models. One model stays resident, because 8 GB
  cannot hold two 4B models next to the voice stack.
- The full code fork.

---

## 1. Current-state summary (what the port touches)

Read at commit `e355660`. The task brief mentioned `setup/setup-board.sh` and
`docs/SETUP.md`. **Neither exists any more.** Provisioning now lives entirely in
`image_build/` (rsdk + `rootfs.jsonnet` hooks), and `deploy.sh` handles
development pushes.

### 1.1 Runtime processes on the Q6A

| Unit | Runs | User | Notes |
|---|---|---|---|
| `genie-server.service` | `src/genie_server.py`. A persistent ctypes binding to `libGenie.so` that serves an Ollama-*shaped* `/api/generate` and `/api/chat` on `127.0.0.1:11434` | `trailcurrent` | **No hardening possible** because of `/dev/fastrpc-*`. It does server-side sentence dedup and maps a context overflow to `status=4` |
| `voice-assistant.service` | `src/assistant.py` (4281 lines): a single-threaded loop | `trailcurrent` | `ProtectSystem=strict`, `NoNewPrivileges`. Reads `~/assistant.env` |
| `peregrine-chat.service` | `src/web_chat.py`: HTTPS UI on 443, 80→443 redirect, `/api/voice` on 8081 for the ESP32-P4 terminal | `trailcurrent` | Heavily sandboxed. Uses `CAP_NET_BIND_SERVICE`. **Loads its own Whisper and Piper** for `/api/voice` |
| `cpu-performance.service` | Sets `schedutil` on all cores and raises `scaling_max_freq` | root | Q6A only. `intel_pstate` does not offer `schedutil` |
| `power-save-hw.service` | Pins the GPU to its minimum frequency, unbinds DSI/DP/camera, blocks Wi-Fi, enables USB autosuspend **except audio (01) and HID (03) classes** | root | QCS6490 only, except the USB rule, which carries over |

### 1.2 The fast path as it exists today (`src/assistant.py`)

1. **Capture.** A persistent `arecord` runs at 16 kHz S16_LE mono with 80 ms
   chunks (`CHUNK=1280`). The Jabra is found by matching `usb` or `jabra` in
   `arecord -l`. If PulseAudio is installed, the loop uses `pulse` instead.
2. **Wake word.** openWakeWord runs on ONNX. A chunk goes to the model only if
   its mean |x| ≥ `WAKE_MIN_RMS`. Detection needs `WAKE_ACTIVATIONS`
   consecutive frames above `WAKE_THRESHOLD`.
3. **Beep.** The loop sleeps 0.15 s, then plays the wake beep in blocking mode
   and drains the audio buffered during it.
4. **Endpointing is already energy-based, not a fixed window.**
   `record_speech()` allows a 3 s grace period for the user to start talking,
   then ends after `SILENCE_DURATION` (1.5 s) of amplitude below
   `SILENCE_THRESHOLD`. There is a hard cap of `MAX_RECORD_SECONDS = 15`
   (hard-coded). **The 15 s cap is the only thing that can truncate a long
   question.**
5. **STT.** faster-whisper `base.en` INT8 with `cpu_threads=CPU_THREADS`
   (default 8). It writes a temp WAV, uses `beam_size` 1, and passes an
   `initial_prompt` built from the device names.
6. **Intent.** `match_intent()` runs regex and phonetic matching against the
   device registry (retained MQTT topics `local/config/pdm_channels` and
   `relay_channels`), then publishes over MQTT/TLS (paho).
7. **Confirmation.** `TTSEngine` keeps an in-process `PiperVoice` and a WAV
   cache in `~/.cache/peregrine-tts`. "Turning on all lights." and the
   per-device confirmations are **pre-rendered at startup**, so the
   confirmation is normally a cache hit played by `aplay`.
8. **LLM fallback** (only when no intent matches).
   - It calls `ask_llm_stream()` → `POST {OLLAMA_URL}/api/generate` with
     top-level `system`, `num_predict 150` and `num_thread`.
   - Responses stream by sentence into Piper.
   - A response that begins with `{` is collected in full and passed to
     `handle_command()`.

The fast path **never calls the LLM.** The loop is sequential, so the voice
loop never runs its own LLM generation at the same moment as its own fast
path. **Contention can only come from `web_chat.py`**: a browser chat stream,
or an ESP32 `/api/voice` request running STT, LLM and TTS in that process.
That defines the "during generation" test (§5.3).

### 1.3 Baseline requirements carried over (from rev 1)

These carry over unchanged:

- **R1** wake word
- **R2** STT
- **R4** Piper
- **R5** ALSA, Jabra only
- **R6** regex intents and MQTT (Torrent, Switchback, Playbill, sensors)
- **R7** Headwaters REST with API key
- **R8** web chat on 443 with a per-board CA
- **R9** voice terminal on `:8081`
- **R10** intent RPC on `:11435`
- **R11** config through env vars; `~/assistant.env` is never overwritten
- **R12** offline at runtime (`HF_HUB_OFFLINE`, no downloads at boot)
- **R13** systemd hardening
- **R14** image → first boot → first-login wizard → `peregrine-self-test`
- **R15** avahi `peregrine.local`; timesyncd → `headwaters.local`
- **R16** minimal CLI image (no desktop, no Snap, branded MOTD and Plymouth)
- **R17** `deploy.sh`
- **R18** two release tracks
- **R19** MIT-compatible licensing for anything baked into the image

R3 (the LLM) changes to `llama-server` (§2.3).

### 1.4 Interface gaps a real non-Genie server exposes (G1–G9 from rev 1, re-checked)

| # | Gap | Where |
|---|---|---|
| G1 | `/api/chat` requests carry no `model` field | `web_chat.py` (3 call sites) |
| G2 | The system prompt is sent as a top-level `system` key on `/api/chat` | `web_chat.py` |
| G3 | Stream deltas are read from `evt["response"]` | `web_chat.py` `_proxy_genie_stream`, `_iter_genie_stream` |
| G4 | Budgets are hard-coded for a 1024-token window: `_SYSTEM_PROMPT_BUDGET_CHARS = 2200`, `WEB_CHAT_MAX_CONTEXT=700` | `assistant.py:869`, `web_chat.py:131` |
| G5 | Sentence-repetition dedup happens server-side | `genie_server.py` `_dedup_sentences` |
| G6 | Context overflow → spoken apology (`status=4`) | `genie_server.py` |
| G7 | `CPU_THREADS` defaults to 8; the N150 has 4 cores | `assistant.py`, `stt.py`, unit files |
| G8 | Model default is `llama3.2:1b-npu` | `assistant.py` |
| G9 | Error text says "NPU backend unavailable" | `web_chat.py` |

### 1.5 Repo facts that affect the port

- **Users and hardening.** The services run as `trailcurrent`, an interactive
  sudo user whose password is changed by the first-login wizard. Root is
  locked, there is no desktop, and Wi-Fi is blacklisted.
  - There is **no locked `assistant` service user** today. The task brief and
    the project `CLAUDE.md` both say there is; both are out of date.
  - The ZimaBoard keeps the same model for parity, and adds a locked system
    user only for `llama-server` (§2.4).
- **ALSA.** `image_build/files/alsa/asound.conf` pins `card 0`. That holds on
  the Q6A but not on x86, where the onboard HDA codec usually takes card 0.
- **Pre-existing Q6A image gap.** Not fixed by this plan, reported only.
  - `rootfs.jsonnet` hook 11 installs `assistant.py`, `tts.py`,
    `genie_server.py` and `web_chat.py`, but **not `stt.py`**.
  - `web_chat.py` imports `stt` lazily on the first `/api/voice` request.
  - A board flashed from the image therefore has a broken voice terminal
    until `deploy.sh` (which does copy `stt.py`) is run.
- **Measured figures in the repo:** ~12 tok/s for Genie 1B and 24 s → 3 s per
  query after the persistent NPU change.
  - There is **no measured Q6A idle wattage and no fast-path latency figure.**
    Both are needed as the baseline for goals 1 and 3. Step 0 of §7 measures
    them.
- **Power feed.** The Q6A uses an LM2597 buck. The ZimaBoard 2 needs no
  converter: it is **powered directly from the lithium house battery** through
  its 12 V barrel input (maintainer decision, 2026-10-16).

---

## 2. Target architecture

### 2.1 Resource allocation

| Resource | Assigned to | Why |
|---|---|---|
| **iGPU** (Intel UHD, Xe-LP, 24 EU) | `llama-server` (Vulkan backend): all layers (`-ngl 99`), prefill and decode | This is the only way to stop the LLM taking CPU time from the fast path (goal 1). On the Q6A the NPU did the same job. Token generation is memory-bandwidth-bound, so the iGPU should not lose much generation rate compared with the CPU. Prefill is compute-bound and should benefit from it. **Both points are measured in §5.1, not assumed** |
| **CPU 0–2** | `voice-assistant` (wake word, Whisper with `CPU_THREADS=3`, Piper), `peregrine-chat` (its own Whisper and Piper for `/api/voice`), MQTT, sshd, journald | Three uncontended cores for the fast path. Whisper with 3 threads on 0–2 vs 4 threads on 0–3 is measured in step 5. Keep 4 only if it wins in isolation **and** does not lose under generation |
| **CPU 3** | `llama-server` host threads (`-t 1 -tb 1`): Vulkan submission, sampling, HTTP | Fenced by `AllowedCPUs=3`. If Vulkan fence waits spin and fill core 3, the fast path is still unaffected |
| **Memory bandwidth** (single-channel LPDDR5-4800, 38.4 GB/s peak, shared by CPU and iGPU) | Shared. **This is the one resource the iGPU does not isolate** | Whisper and Piper would compete with decode for DRAM bandwidth. The **yield gate** (§2.5) is always on: it pauses generation for the ~1–2 s the fast path takes |
| **2nd 2.5 GbE port** | Link down (driver unbound) | Power (§4) |
| **2nd USB 3.1 port** | Unused, autosuspended | A second USB audio device would also break the Jabra lookup (first `usb` card wins) |
| **eMMC** | Root fs + one baked GGUF model | §2.6 |

### 2.2 Memory budget (8 GB)

Model sizes and KV-cache sizes below are **arithmetic from each model's
published config**, not predictions. Process RSS is filled in from
measurement (step 8).

| Consumer | Budget / cap | Basis |
|---|---|---|
| BIOS iGPU stolen memory (DVMT pre-alloc) | Set to the BIOS minimum | Vulkan buffers come from system RAM via GTT, not from stolen memory |
| Kernel, systemd, journald, sshd, avahi | *measure* | `smem -tk` on an idle hand-installed system |
| `voice-assistant` (Whisper base.en INT8, openWakeWord, Piper, Python) | *measure*; `MemoryHigh=1G` | `MemoryHigh` throttles before the OOM killer; it is not a hard kill |
| `peregrine-chat` (second Whisper + Piper) | *measure*; `MemoryHigh=768M` | |
| `llama-server` weights, Q4_K_M | Llama 3.2 3B ≈ 2.0 GB · Qwen3 4B ≈ 2.5 GB · Phi-4-mini ≈ 2.5 GB · Gemma 3 4B ≈ 2.5 GB | GGUF file sizes; confirm with `ls -l` |
| `llama-server` KV cache, 8K context **per slot**, q8_0 K and V | Llama 3B ≈ 0.47 GiB · Qwen3 4B ≈ 0.60 GiB · Phi-4-mini ≈ 0.53 GiB · Gemma 3 4B less (sliding-window layers) | 2 × layers × kv_heads × head_dim × 1 byte × 8192. f16 doubles it |
| `peregrine-llm-bridge` (stdlib Python HTTP proxy) | *measure*; `MemoryMax=128M` | Same footprint class as `genie_server.py` without the Genie library |
| `llama-server` with 2 slots (§2.3) | weights + 2 × KV + compute buffer → **`MemoryMax=4.5G`**, `OOMScoreAdjust=500` | A model that does not fit kills only the LLM, never the voice loop (`OOMScoreAdjust=-500`) |
| Headroom | ≥ 1 GB `MemAvailable` at peak = **acceptance gate** | §5.5 |

Unknown: whether i915 GEM buffers allocated through Vulkan are charged to the
`llama-server` cgroup. Step 2 checks this by comparing `memory.current` with
the change in `free`. If they are not charged, `MemoryMax` cannot cap the LLM,
and the cap moves to choosing the model and context size (§6.3).

### 2.3 LLM serving

- **Binary.** `llama-server` is built from a **pinned llama.cpp tag** with
  `-DGGML_VULKAN=ON -DGGML_NATIVE=OFF -DGGML_AVX2=ON`. It installs to
  `/opt/llama.cpp/<tag>/` with `/opt/llama.cpp/current` as a symlink. The CPU
  backend is always compiled in, which gives the fallback (§6.1) at no extra
  cost. Runtime packages: `libvulkan1` and `mesa-vulkan-drivers`.
- **Unit: `peregrine-llm.service`.** Its `ExecStart` is a single line:
  ```
  llama-server --model ${LLM_MODEL} --host 127.0.0.1 --port 8080 \
    -ngl 99 -t 1 -tb 1 -c ${LLM_CTX_TOTAL} -np ${LLM_SLOTS} \
    -fa on -ctk q8_0 -ctv q8_0 --jinja --cache-reuse 256 --no-mmap \
    ${LLM_EXTRA_ARGS}
  ```
  It listens on loopback port 8080. Port 11434 belongs to the bridge, below.
- **Config file: `/etc/peregrine/llm.env`.** It is written only by
  `peregrine-model`, and holds `LLM_MODEL`, `LLM_CTX_TOTAL=16384`,
  `LLM_SLOTS=2` and `LLM_EXTRA_ARGS`.
- **Swapping models takes no code change** (a constraint).
  - `--jinja` uses the chat template embedded in each GGUF.
  - Disabling thinking is per-model *server* config, e.g.
    `LLM_EXTRA_ARGS=--chat-template-kwargs {"enable_thinking":false}` for
    hybrid Qwen3. **Qwen3-4B-Instruct-2507** has no thinking mode at all, so
    it needs nothing.
  - Swapping a model means changing a file path and restarting the service.
- **Two slots of 8K each** (`-c 16384 -np 2`).
  - The voice and web-chat system prompts differ. With one slot, each request
    would evict the other's cached prefix and force a full re-prefill.
  - `llama-server` sends each request to the slot with the most similar cached
    prompt, so no client code is needed.
  - Cost: one extra KV cache (§2.2).
  - Fallback if memory is tight: `-np 1`.
- **Prompt cache.** The fast path's cache hits matter less than the LLM path's
  time-to-first-word.
  - `get_system_prompt()` currently puts the **sensor summary** (which changes
    every turn) right after the static schema, *before* the device and
    Playbill lists. Everything after it is therefore re-prefilled on every
    turn.
  - A new layout, `LLM_PROMPT_LAYOUT=cache`, orders the prompt as: static
    schema → devices → Playbills → sensor summary last.
  - It is **off by default**, so the Q6A prompt stays byte-identical.
- **Budgets.** On ZimaBoard, `assistant.env` sets
  `LLM_SYSTEM_PROMPT_BUDGET_CHARS=8000` (fits a full device list). The
  existing `WEB_CHAT_MAX_CONTEXT` variable is set to 7000 in the ZimaBoard
  unit. The Q6A defaults stay 2200 and 700.
- **Warm-up.** An `ExecStartPost` sends one minimal request so the weights are
  resident and the shaders are compiled before `voice-assistant` starts.
  `voice-assistant` uses `After=`/`Wants=peregrine-llm-bridge.service`.

**How the assistant calls it: a Genie-compatible bridge.**

`assistant.py` and `web_chat.py` already talk to an Ollama-*shaped* API on
`127.0.0.1:11434`: the one `genie_server.py` provides on the Q6A. On the
ZimaBoard, **`zimaboard2/src/llm_bridge.py`** serves that same API on the
same port, and forwards to `llama-server`. Neither client changes, and their
`OLLAMA_URL`/`GENIE_URL` defaults already point at it.

- **Lineage.** The bridge is forked from `genie_server.py` and keeps its HTTP
  layer, `_dedup_sentences` (G5) and the `status=4` → "too long" apology
  mapping (G6). Its Genie/ctypes backend is replaced by a streaming HTTP call
  to `llama-server`. It is ZimaBoard-only, just as `genie_server.py` is
  Q6A-only.
- **What it accepts.** It takes Genie's exact request shapes:
  - `/api/generate`: `prompt` + top-level `system`
  - `/api/chat`: `messages` + top-level `system`, no `model`

  That covers G1 and G2 on the server side.
- **What it forwards.** It sends `POST /v1/chat/completions` with
  `stream: true` and proper `system`/`user` messages. `llama-server` then
  applies the GGUF's own chat template, so the bridge holds **no
  model-specific template**. This differs from `genie_server.py`, which
  hard-codes Llama 3.2.
- **What it returns.** It answers in Genie's NDJSON shape: deltas in
  `response`, then `done: true`. That covers G3.
- **Option mapping.** `num_predict` → `max_tokens`. `num_thread` and
  `keep_alive` are ignored. A context overflow (an HTTP 400 from
  `llama-server`, or `finish_reason: "length"` on the prompt) maps to the
  same apology text Genie used.
- **Remaining gaps.** G4 is handled by env (budgets above). G7: the ZimaBoard
  unit sets `CPU_THREADS`. G8: the model name is ignored by the bridge. G9
  (the "NPU backend unavailable" error text in `web_chat.py`) is **left as
  is**. It is cosmetic, and fixing it would spend a file of the
  sharing-rule budget.
- **Cost.** One extra loopback HTTP hop (latency to be confirmed in §5.1 by
  comparing time to first delta through the bridge with `llama-server`
  direct), plus a small Python process.

### 2.4 systemd units on the ZimaBoard 2

| Unit | User | Placement | Hardening / limits |
|---|---|---|---|
| `peregrine-llm.service` | `peregrine-llm`: system user, `nologin`, no home, group `render` | `AllowedCPUs=3`, `Nice=10`, `CPUWeight=20`, `IOWeight=10` | `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp`, `NoNewPrivileges`, `DevicePolicy=closed` + `DeviceAllow=char-drm rw`, `RestrictAddressFamilies=AF_INET AF_UNIX`, `IPAddressDeny=any` + `IPAddressAllow=localhost`, `ReadOnlyPaths=/var/lib/peregrine/llm`, `MemoryMax=4.5G`, `OOMScoreAdjust=500`. This is properly sandboxed, unlike `genie-server` |
| `peregrine-llm-bridge.service` | `peregrine-llm` | `AllowedCPUs=3`, `Nice=10` | Stdlib-only, so sandboxed like `peregrine-chat`: `ProtectSystem=strict`, `ProtectHome=yes`, `NoNewPrivileges`, loopback-only `IPAddressAllow`, `MemoryMax=128M`. `After=`/`Requires=peregrine-llm.service` |
| `voice-assistant.service` | `trailcurrent` (as on the Q6A) | `AllowedCPUs=0-2`, `Nice=-5`, `CPUWeight=1000`, `OOMScoreAdjust=-500` | As on the Q6A. Env adds `CPU_THREADS=3`, `MAX_RECORD_SECONDS=30`, `LLM_SYSTEM_PROMPT_BUDGET_CHARS=8000`, `LLM_PROMPT_LAYOUT=cache`, `PEREGRINE_LLM_GATE=/run/peregrine/llm-gate.sock` |
| `peregrine-chat.service` | `trailcurrent` | `AllowedCPUs=0-2`, `CPUWeight=200` | As on the Q6A, plus `CPU_THREADS=3` and `WEB_CHAT_MAX_CONTEXT=7000` |
| `peregrine-power.service` | root, oneshot | — | §4 |
| `peregrine-llm-gate.socket` + `@.service` | root, socket-activated | — | Always installed (§2.5) |
| `peregrine-firstboot.service` | root | — | Copied from the Q6A and adapted: grows root on `mmcblk0` or `sdX`, never hard-coded |

`genie-server`, `cpu-performance` and `power-save-hw` are not installed.

### 2.5 Keeping the fast path isolated from LLM load

There are three layers, all on. Layer 3 is a product requirement, not a
fallback: **a web chat yields to a voice prompt** (maintainer, 2026-10-16).

1. **Placement (always on).** The LLM is on the iGPU and its host threads are
   fenced to CPU 3. The fast path has cores 0–2 and a CPU weight 50× higher.
   `OOMScoreAdjust` protects the voice loop.
2. **GPU frequency cap (always on, tuned in step 3).**
   - Decode is bandwidth-bound, so the lowest
     `/sys/class/drm/card*/gt/gt0/rps_max_freq_mhz` that keeps tok/s within
     5 % of uncapped also cuts the iGPU's share of package power and heat.
   - It also reduces DRAM pressure during prefill.
3. **Yield gate (always on).**
   - When `PEREGRINE_LLM_GATE` is set, `assistant.py` writes `freeze` to that
     socket the moment the wake word fires. Whisper, intent matching and the
     MQTT publish then run with no generation competing.
   - It writes `thaw` as soon as either of these happens:
     - the confirmation playback has started (intent path), or
     - just **before** its own LLM call (fallthrough path). The voice loop
       never waits on a frozen server.
   - On the Q6A the variable is unset, so the code does nothing.
   - The root-owned socket service runs
     `systemctl freeze|thaw peregrine-llm.service` (cgroup freezer, systemd
     255).
   - A **5 s watchdog auto-thaw** ensures a crash can never leave the LLM
     frozen.
   - Effect: a browser chat stream pauses for about the duration of a voice
     command. No requests are lost, because the HTTP connections simply
     stall.
   - The socket is mode 0660, group `trailcurrent`. It accepts only the two
     verbs.
   - **Known limit.** An ESP32 `/api/voice` request does not freeze the
     server. Making it do so would need a `web_chat.py` change, a second
     shared file. The ESP32 path is measured in §5.3. If it regresses, adding
     the same gate call to `web_chat.py` is the second (and last) file the
     sharing rule allows.

### 2.6 Model storage and swapping

- Models live in `/var/lib/peregrine/llm/models/*.gguf`, each with an optional
  `<name>.args` sidecar holding `LLM_EXTRA_ARGS`.
- **`peregrine-model`** is a bash tool in `/usr/local/bin` (rev 1's CLI, with
  the Ollama parts removed):

  | Command | Does |
  |---|---|
  | `list` | Files, sizes, licences, which model is active |
  | `set <name>` | Rewrites `llm.env`, restarts `peregrine-llm`, runs a smoke query, **rolls back** on failure |
  | `import <file.gguf>` | Offline copy from USB or SCP, with a SHA-256 check |
  | `bench <name>` | Runs §5.1 for that model |
  | `rm <name>` | Refuses on the active model |

  There is no `pull` and no network access at runtime (R12).
- **One model is baked into the image**, chosen by the licence rule in §5.1. A
  second fits on 32 GB eMMC; more go on SATA.

---

## 3. Repo changes (file by file)

The principle: **shared code in `src/`, one hardware/setup layer per target.**
The Q6A layer is the existing `config/`, `image_build/` and `deploy.sh`, kept
where it is so no Q6A path moves. The ZimaBoard layer is new, under
`zimaboard2/`.

### 3.1 Shared: `src/`. The only Q6A-visible change

| File | Change | Q6A effect |
|---|---|---|
| `src/assistant.py` | Five env-gated additions, each defaulting to today's behaviour (table below) | None with defaults. Each addition is a no-op unless its variable is set |
| `src/web_chat.py` | **Unchanged.** It talks to the bridge exactly as it talks to Genie. It is touched only if the ESP32 path needs the gate (§2.5 known limit) | — |
| `src/stt.py`, `src/tts.py`, `src/genie_server.py` | Unchanged | — |

The five `assistant.py` additions:

| Variable | Default | What it controls |
|---|---|---|
| `MAX_RECORD_SECONDS` | 15 | Recording cap |
| `LLM_SYSTEM_PROMPT_BUDGET_CHARS` | 2200 | System-prompt size |
| `LLM_PROMPT_LAYOUT` | `legacy` | Prompt section order (§2.3) |
| `PEREGRINE_TIMING=1` | off | Monotonic timestamps: wake, record end, STT end, intent end, MQTT publish, first audio |
| `PEREGRINE_LLM_GATE` | unset | Path of the yield-gate socket; unset = the code does nothing |

Endpointing stays energy-based with the same constants. The only change is
that the 15 s cap becomes configurable: 30 s on ZimaBoard, so long LLM
questions are not cut off. Moving to a VAD model is **not** proposed, because
there is no measured reason to change it.

### 3.2 Q6A layer: unchanged

`deploy.sh`, `config/` and `image_build/` are **not modified**. `deploy.sh`
already copies `assistant.py`, and no new shared file is added.

Total Q6A-visible footprint: **`src/assistant.py`, one file.** That is inside
the sharing rule (deploy.sh + one or two files), with one file of slack
reserved for the possible `web_chat.py` gate call. Because `assistant.py`
changes, the Q6A gets the §5.6 smoke test when it does.

The missing `stt.py` install in the Q6A image (§1.5) is a separate Q6A fix.
It is **not** part of this port and does not count against the budget (Q8).

### 3.3 ZimaBoard 2 layer: `zimaboard2/` (new)

```
zimaboard2/
├── README.md                       Quick start
├── deploy.sh                       Dev push: ../src + this layer. Target from argument or
│                                   PEREGRINE_DEVICE; otherwise fails with instructions. No default host
├── src/
│   └── llm_bridge.py               Genie-compatible API on :11434 → llama-server (§2.3). ZimaBoard-only
├── config/
│   ├── peregrine-llm.service       §2.3 / §2.4
│   ├── peregrine-llm-bridge.service
│   ├── voice-assistant.service     Same as ../config/ plus placement and LLM_* env
│   ├── peregrine-chat.service      Same as ../config/ plus placement and LLM_* env
│   ├── peregrine-power.service     §4
│   ├── peregrine-llm-gate.socket   Yield gate (§2.5)
│   ├── peregrine-llm-gate@.service
│   ├── llm.env.example             Documents every LLM_* setting
│   ├── alsa/asound.conf            Default PCM by card *id* of the Jabra (confirmed from /proc/asound/cards)
│   └── modprobe/
│       ├── blacklist-hda.conf      snd_hda_intel off: no onboard audio is used, Jabra becomes card 0, saves power
│       └── i915.conf               Only if step 1 shows the N150 needs force_probe or GuC options
├── tools/
│   ├── peregrine-model             §2.6
│   ├── bench-llm.sh                §5.1 wrapper around llama-bench + llama-server timings
│   ├── bench-fastpath.py           Parses PEREGRINE_TIMING journal lines → p50/p95 table (runs on both boards)
│   └── load-chat.sh                Drives a long web-chat generation for §5.3
├── image/
│   ├── build.sh                    mmdebstrap noble amd64 → GPT image (ESP + ext4)
│   ├── preflight.sh                Build-host checks; fetches the pinned llama.cpp tag, GGUF(s), Piper voice with SHA-256
│   ├── build-llama.sh              Builds llama.cpp (Vulkan + CPU) in a clean noble chroot; output is cached
│   ├── test-vm.sh                  QEMU/KVM + OVMF boot; runs self-test minus audio and iGPU
│   ├── hooks/NN-*.sh               hostname/users · peregrine-llm user · venv (pins identical to the Q6A hook 8) ·
│   │                               models · src + units · power policy · branding · GRUB
│   └── files/                      firstboot, first-login wizard (+ model choice), self-test
│                                   (Vulkan device + llama-server /health instead of CDSP),
│                                   MOTD/issue, Plymouth, sysctl, sshd, timesyncd, env example.
│                                   Adapted copies of ../image_build/files
└── docs/
    ├── install.md                  eMMC via live USB, or SATA; BIOS checklist (rev 1 §6)
    ├── power.md                    §4 procedure + results
    └── benchmarks.md               §5 results, Q6A baseline beside ZimaBoard
```

### 3.4 How the target is selected

| Action | Q6A | ZimaBoard 2 |
|---|---|---|
| Build an image | `sudo ./image_build/build.sh` | `sudo ./zimaboard2/image/build.sh` |
| Dev push | `./deploy.sh <host>` | `./zimaboard2/deploy.sh <host>` (or `PEREGRINE_DEVICE`) |
| Image artefact | `peregrine-q6a-vX.Y.img` | `peregrine-zimaboard2-vX.Y.img.zst` |

- The entry point *is* the target selector. Both builds stage the same `src/`.
- At runtime, target behaviour comes **only from the env in that target's unit
  files**. The Python code has no `if target == …` branches.
- `image/build.sh` refuses to run on a non-x86_64 host.

---

## 4. Power plan

### 4.1 Settings (`peregrine-power.service`, run every boot)

| Area | Setting | Why |
|---|---|---|
| CPU governor | `intel_pstate` active, governor **`powersave`** (the only dynamic HWP governor), EPP **`balance_performance`** | Idle power comes mostly from C-state residency, not from the governor. EPP sets how fast cores ramp when the wake word fires. `balance_power` is measured in step 7 and kept only if fast-path p95 stays within 5 %. `performance` is never used (the Q6A learned this at ~90 °C) |
| Turbo | Left on | The fast path is short and bursty. Turbo costs energy only while it is active |
| C-states | BIOS: all C-states and package C-states enabled; kernel default `intel_idle` | Check with `turbostat` package-C residency |
| iGPU | i915 RC6 enabled (default). `rps_max_freq_mhz` capped at the step-3 value. No display: headless, nothing connected to mini-DP, `consoleblank=60` | RC6 power-gates the GPU between requests. A resident model has no idle cost because weights in RAM are not GPU activity |
| USB autosuspend | `power/control=auto` on every USB device **except interfaces of class 01 (audio) and 03 (HID)**, which stay `on`. This is the same rule as the Q6A `power-save-hw` | The Jabra streams continuously (the always-on `arecord`), so it could not suspend anyway. Forcing it `on` avoids resume glitches. The free port and the hub autosuspend |
| USB audio and package C-states | No setting. **Measured** | A continuously streaming isochronous USB device can limit how deep the package sleeps. Both boards pay this, but it may cost more on x86. Step 7 measures package-C residency with `voice-assistant` stopped vs running, to quantify it |
| PCIe | `pcie_aspm.policy=powersupersave` on the kernel command line, if the BIOS lets the OS control ASPM | NICs and eMMC host |
| Ethernet | Port 2: unbind `igc` from it. Port 1: EEE on if the switch supports it (`ethtool --set-eee`) | One fewer PHY |
| SATA | ALPM `med_power_with_dipm`. Ports with nothing attached are disabled in BIOS where possible | |
| Onboard audio | `snd_hda_intel` blacklisted | Codec powered down; Jabra stays card 0 |
| Wake-word loop | Unchanged: RMS gating already skips inference on quiet chunks | |

### 4.2 Expected figures

These are not predictions. Per the project's measure-first rule, the plan
gives only spec-sheet bounds and the measurement that replaces each one.

| Figure | What is known | How it is measured |
|---|---|---|
| Q6A idle (baseline) | **Not recorded anywhere in the repo** | Step 0: same inline 12 V DC meter, 10 min average after 10 min settle, all services running, Ethernet up, Jabra attached, quiet room |
| ZimaBoard idle | Rev 1 quoted ~6 W from vendor/review material (stock OS, not Peregrine) | Step 1 (bare Ubuntu floor) and step 7 (full stack, tuned). **Gate: ≤ Q6A idle + 3 W** |
| ZimaBoard under load | Bounded by the BIOS package power limits (`/sys/class/powercap/intel-rapl:0/constraint_*_power_limit_uw`) plus board overhead (NICs, eMMC, DRAM, regulator loss) | Step 3: meter during a 30 min `llama-bench` loop. `turbostat --show PkgWatt,GFXWatt,CorWatt` splits it |
| Energy per day | Idle W × 24 h dominates. LLM use is minutes per day | Reported in `power.md` as Wh/day, the metric rev 1 set for RV-battery use |

---

## 5. Benchmark and acceptance plan

All results go in `zimaboard2/docs/benchmarks.md`, with the Q6A baseline
beside them. Unless stated otherwise, each latency figure is **20 trials,
reported as p50 and p95**.

### 5.1 LLM throughput (step 2)

For each candidate, using the
`bartowski`/`unsloth`/official GGUF at Q4_K_M with the SHA-256 recorded:

- Llama-3.2-3B-Instruct
- Qwen3-4B (hybrid) and Qwen3-4B-Instruct-2507
- Gemma-3-4B-it
- Phi-4-mini-instruct

Run:

```bash
# iGPU (Vulkan), the planned config
llama-bench -m $M -ngl 99 -t 1 -fa 1 -ctk q8_0 -ctv q8_0 \
            -p 512,2048 -n 128 -d 0,4096 -r 3 -o md
# CPU-only comparison (fallback lane, §6.1)
llama-bench -m $M -ngl 0 -t 4 -fa 1 -ctk q8_0 -ctv q8_0 \
            -p 512,2048 -n 128 -d 0,4096 -r 3 -o md
```

Then, through the real `llama-server` with the real Peregrine prompts (full
command schema, and the lean prose prompt), 10 questions each, read
`timings.prompt_per_second`, `timings.predicted_per_second` and time to the
first content delta from the server's response.

**A bandwidth ceiling to keep in mind.** This is not a prediction. Decoding
reads every weight once per token. With 38.4 GB/s peak:

| Model size | Theoretical ceiling at depth 0 | Bandwidth efficiency needed for 10 tok/s |
|---|---|---|
| ~2.0 GB (3B) | ~19 tok/s | ≥ ~52 % |
| ~2.5 GB (4B) | ~15 tok/s | ≥ ~65 % |

The KV-cache reads add to this as the context fills. **The 4B models' ability
to reach 10 tok/s is the biggest open number in this plan.**

**Pass criteria:**

- **≥ 10 tok/s** `predicted_per_second` at the real-prompt depth
- **≥ 8 tok/s at 4K depth**. This is a proposed secondary gate, so long chats
  do not crawl
- Model loads at 8K × 2 slots within the §2.2 cap
- **Quality harness** (from rev 1): 30 command phrasings that miss the regex
  intents, scored for valid and correct JSON, plus a fixed question set
  scored by hand.
- **Choose by licence, MIT first** (maintainer, 2026-10-16).
  - **Phi-4-mini-instruct (MIT)** is the default if it passes the rate gates.
  - A non-MIT candidate replaces it only if it also passes the rate gates
    **and** clearly beats Phi-4-mini on the quality harness. Non-MIT here
    means Qwen3 (Apache 2.0), Llama 3.2 (Community) or Gemma 3 (Gemma
    Terms).
  - In that case the side-by-side results go to the maintainer, who decides
    whether the gain justifies the licence.
  - If Phi-4-mini fails the rate gates, the comparison is between the
    passing non-MIT models, under the same review.

### 5.2 Fast path in isolation (step 5)

- **Stimulus.** Recorded WAVs, played from a phone or laptop at a fixed 1 m
  from the Jabra, at the same volume, on both boards:
  - "hey Peregrine, turn on all the lights"
  - "…turn off all the lights"
  - "…turn on the awning" (a named device)
  - A second USB audio device on the board is ruled out (§2.1).
- **Instrumentation:** `PEREGRINE_TIMING=1`, then
  `journalctl -u voice-assistant -o short-monotonic | zimaboard2/tools/bench-fastpath.py`.
- **Metrics:**

  | Metric | From | To | Notes |
  |---|---|---|---|
  | **L_cmd** | record end | MQTT publish | The goal-1 number |
  | **L_ack** | record end | confirmation playback start | |
  | L_stt | | | STT time alone |
  | L_wake | | | Wake-detection time; informational |

- **Also recorded:** the mosquitto broker log timestamps on Headwaters, as an
  independent check on L_cmd.
- **Pass:** ZimaBoard p50 **and** p95 of L_cmd and L_ack ≤ the Q6A's (step 0).
  The `SILENCE_DURATION` endpoint wait is identical config on both boards and
  sits outside these intervals.

### 5.3 Fast path during LLM generation (step 6)

- **Load:** `zimaboard2/tools/load-chat.sh` keeps one long web-chat generation
  running continuously (`/api/chat`, "write a detailed 800-word…", re-posted
  as each one finishes).
- **Variant:** the same with an ESP32-style `POST :8081/api/voice` loop. That
  path also runs Whisper and Piper inside `peregrine-chat` on cores 0–2, so it
  is the harsher case.
- **Q6A comparison:** run the same load on the Q6A for step-0 parity (its
  NPU-backed generation should barely move its fast path, which is the bar).
- **Pass:** ZimaBoard L_cmd/L_ack p50 and p95 under load ≤ **Q6A isolated**
  p50/p95, under both loads, with the yield gate on.
- **Also record:**
  - how long the chat stream stalls per voice command (should be about
    L_ack)
  - that no chat request errors
  - that the stream resumes within 1 s of the confirmation starting
  - one run with the gate off, to show how much the gate contributes
- **If the ESP32 `/api/voice` load fails** while the web-chat load passes, add
  the gate call to `web_chat.py` (§2.5 known limit) and re-run.

### 5.4 Power (step 7)

- **Meter:** an inline DC power meter on the 12 V input, the same meter and
  supply for both boards.
- **Idle protocol:** 10 min settle, then a 10 min average, with all services
  up, the model resident, Ethernet linked and nobody speaking.
- **Diagnostics alongside:**
  `sudo turbostat --quiet --interval 30 --show PkgWatt,CorWatt,GFXWatt,Pkg%pc2,Pkg%pc6,Pkg%pc8,Pkg%pc10`
  and `powertop --csv`.
- **Pass:** ZimaBoard idle ≤ Q6A idle + 3 W.
- **Report:** idle Wh/day, plus under-load watts from §5.5.

### 5.5 Thermal, memory, soak (steps 3 and 8)

| Check | Run | Pass |
|---|---|---|
| Thermal | 30 min back-to-back `llama-bench -n 512` in the final enclosure. Log `turbostat` (Bzy_MHz, GFX MHz, PkgTmp, PkgWatt) every 10 s | tok/s in the last 5 min ≥ 90 % of the first 5 min; no thermal trip |
| Memory | Everything resident, both slots filled to ~8K, an `/api/voice` request in flight | `MemAvailable` ≥ 1 GB; no OOM kills |
| Soak | 24 h: voice command every 10 min, chat every 15 min, voice terminal every 20 min | No service restarts (`systemctl show -p NRestarts`), no OOM, latency p95 still within §5.2 |

### 5.6 Q6A regression smoke (any time `src/` changes)

This is short on purpose. Deploy to the Q6A with `./deploy.sh`, then:

1. Run `peregrine-self-test`.
2. Say "turn on all the lights" and "turn off all the lights".
3. Ask one LLM question by voice.
4. Ask one question in the web chat UI.
5. Send one `/api/voice` request.
6. Re-run §5.2 for 5 trials. p95 must match the step-0 baseline.

---

## 6. Risks and unknowns

| # | Risk | How it is detected | Fallback |
|---|---|---|---|
| 6.1 | **iGPU backend does not work on this kernel and Mesa.** The N150 (Twin Lake / ADL-N) needs i915 PCI-ID support. The noble GA kernel (6.8) and Mesa may predate it or have bugs in the ANV Vulkan path for llama.cpp's shaders | Step 1: `lspci -nn \| grep VGA`, `dmesg \| grep i915`, `vulkaninfo --summary`, `llama-bench` produces sane output (compare a short generation against CPU output) | In this order: (a) the noble **HWE kernel** + current `mesa` from noble-updates; (b) `i915.force_probe=<id>` or the `xe` driver; (c) **CPU backend**: `-ngl 0 -t 3` pinned to cores 1–3, voice loop with priority on 0, the §2.5 gate **on by default**, and a 3B model if 4B misses 10 tok/s on CPU. Goal 1 is then held by the gate rather than by isolation |
| 6.2 | **Thermal throttling** of the passive N150 under sustained generation. Rev 1 noted reviews in the ~90 °C range | §5.5 thermal run in the real enclosure (and the vehicle, in summer) | The step-3 GPU frequency cap (often free for bandwidth-bound decode); lower PL1 through RAPL `powercap` (keeps temperature down at a known tok/s cost); a smaller `max_tokens` for voice; a 3B model; enclosure airflow notes. Voice replies are short, and long sustained load comes only from web chat |
| 6.3 | **8 GB headroom.** Two Whisper instances + Piper ×2 + a 4B model + 2 × 8K KV | §5.5 memory check. Step 2 also checks whether GPU memory is charged to the cgroup | In this order: `-np 1`; 4K chat context; q4_0 KV; a 3B model; load `peregrine-chat`'s Whisper lazily and drop it after an idle timeout (code change in `web_chat.py`, only if needed). Add a small zram swap only for page-cache relief, never for the model |
| 6.4 | Memory-bandwidth contention: the iGPU does not isolate DRAM | §5.3 | §2.5 gate |
| 6.5 | Vulkan host thread spin-waits and fills core 3 at 100 % (harmless to latency, but costs power) | `pidstat -u -p $(pgrep llama-server) 1` during decode | Fenced by placement anyway. Report upstream. Check whether the pinned llama.cpp tag has a wait-mode option |
| 6.6 | Prefill on the iGPU is slow for the ~2–8 KB system prompt → long time to first word on LLM answers | §5.1 `prompt_per_second` and time to first delta | `LLM_PROMPT_LAYOUT=cache` + `--cache-reuse`; warm the slot prefix in `ExecStartPost`; keep the lean prompt for prose |
| 6.7 | Shared `src/` breaks the Q6A, or the shared footprint grows past the sharing rule | §5.6 smoke on every `src/` change. Any planned change beyond `assistant.py` (+ possibly `web_chat.py`) | All new behaviour is env-gated with Q6A defaults. **If a third Q6A-visible file is ever needed, stop and fork** (rev 1 layout) |
| 6.8 | Model licences (R19): MIT preferred | — | The §5.1 rule: Phi-4-mini by default; a non-MIT model only on a clear quality win plus maintainer review. Users can import anything themselves |
| 6.11 | The bridge diverges from `genie_server.py` in behaviour clients rely on (stream framing, `done`, error text) | Step 4: the same client-side checks as the Q6A smoke, run against the bridge | The bridge is forked from `genie_server.py` with only the backend replaced, so the HTTP layer starts out identical |
| 6.9 | Direct house-battery feed: the lithium bank sits above 12 V while charging (up to ~14.6 V on a 4S LiFePO4 charge profile) and sags under heavy house loads | Step 1: read the vendor input-voltage spec; log the board's input voltage alongside the §5.4 meter while the bank is charging and under load | None planned. The board is powered directly from the battery (no converter). Revisit only if the measured range falls outside the vendor spec |
| 6.10 | Restore-on-AC-loss option missing from the BIOS | First unit | Documented workaround or vendor BIOS update |

---

## 7. Implementation sequence

Each step is small, runs on the board, and has a check that closes it.

| Step | Work | Closed by |
|---|---|---|
| **0** | **Q6A baseline.** Add `PEREGRINE_TIMING` (env-gated, log lines only) to `assistant.py` and deploy to the Q6A | §5.2 isolated and §5.3 under load on the Q6A; §5.4 Q6A idle on the meter; Genie tok/s and time to first word on the real prompts. Recorded in `benchmarks.md` |
| **1** | Hand-install Ubuntu Server 24.04 on the ZimaBoard. Bring up the iGPU and the Jabra | `vulkaninfo` lists the UHD GPU; `arecord`/`aplay` work on the Jabra; bare-OS idle on the meter and `turbostat` |
| **2** | Build the pinned llama.cpp (Vulkan + CPU). Run the candidate matrix. Check the cgroup charge of GPU memory | §5.1 for all candidates on both backends. **Decision point:** backend (iGPU or the §6.1 CPU lane) and default model |
| **3** | GPU frequency cap sweep + thermal run | §5.5 thermal pass; cap value recorded |
| **4** | `zimaboard2/src/llm_bridge.py` + its unit. In `assistant.py`: the `MAX_RECORD_SECONDS`, budget, `LLM_PROMPT_LAYOUT` and gate env additions | Through the bridge on the ZimaBoard: an LLM question by voice, a web chat turn, `/api/voice`, an LLM JSON command (Playbill tune), a forced context overflow (apology spoken). Bridge-vs-direct time to first delta recorded. **§5.6 Q6A smoke passes** |
| **5** | `zimaboard2/config/*` units + `zimaboard2/deploy.sh` | Full stack on a hand-installed board. §5.2 isolated passes. Whisper 3 threads vs 4 decided |
| **6** | Yield-gate socket units; contention tuning | §5.3 passes under both loads with the gate on |
| **7** | `peregrine-power.service`, modprobe and ALSA files, BIOS settings | §5.4 passes. EPP choice confirmed against §5.2 |
| **8** | `peregrine-model` + memory checks | Swap between three GGUFs with no code change and no restart of the voice loop; broken model rolls back; §5.5 memory passes |
| **9** | `zimaboard2/image/*`: build, hooks, `build-llama.sh`, self-test, wizard | `test-vm.sh` boots under OVMF and self-test passes (minus audio and iGPU); two consecutive builds give the same package set and file hashes outside `/var/log` (reproducibility) |
| **10** | Install the image to eMMC from a live USB, following `install.md` exactly | Fresh board → "hey Peregrine, turn on all the lights" works; §5.2 re-run on the image |
| **11** | 24 h soak, then release docs | §5.5 soak; `peregrine-zimaboard2-v1.0` published to its own release folder |

---

## 8. Things deliberately not changed

- **openWakeWord, faster-whisper `base.en`, Piper `libritts_r-medium`.** There
  is no measured reason to change them. On x86 they get CTranslate2 and
  onnxruntime AVX2 kernels.
- **Energy endpointing.** Only its cap becomes configurable.
- **The `trailcurrent` user model, `assistant.env`, MQTT/TLS, the Headwaters
  API key, the per-board web CA and the voice-terminal token.** All
  identical.
- **The Q6A `deploy.sh`, `config/`, `image_build/` and release docs.** None
  of them change.
- **Hostname `peregrine`.** It names the AI role in a TrailCurrent install,
  not the hardware. Both targets use it, and only one Peregrine runs per
  install.

---

## 9. Decisions and open questions

### Answered (maintainer, 2026-10-16)

| # | Question | Answer |
|---|---|---|
| Q1 | Shared `src/` or isolated fork? | **Share only if the Q6A footprint is `deploy.sh` + one or two other changes; otherwise fork.** With the bridge, the footprint is `src/assistant.py` alone (`deploy.sh` unchanged), so the plan shares. A third file means fork (§3.2, risk 6.7) |
| Q2 | SKU | **8 GB** confirmed |
| Q3 | Hostname | **`peregrine`.** The name is reserved for the AI role in a TrailCurrent install, whatever hardware runs it |
| Q4 | Power path | **Wired directly to the lithium house battery**, which can deliver far more current than the board needs. No converter |
| Q5 | Web chat during a voice command | **A web chat can and should yield to a voice prompt.** The gate is always on (§2.5) |
| Q6 | Model licence | **MIT preferred.** Other licences are considered only if the quality gain justifies them (§5.1) |

### Still open (the defaults apply unless changed)

| # | Question | Default |
|---|---|---|
| Q7 | Move `voice-assistant` and `peregrine-chat` to a locked, non-login system user on the ZimaBoard? Stronger hardening, but diverges from the Q6A | No. Keep parity; only `llama-server` and the bridge run as the locked `peregrine-llm` user |
| Q8 | Fix the Q6A image's missing `stt.py` install (§1.5)? | As a **separate Q6A change**, outside this port and its file budget |
