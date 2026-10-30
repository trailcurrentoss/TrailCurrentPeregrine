# ZimaBoard 2 benchmarks

Plan §5 results. Every figure here was measured on the board; nothing is
estimated.

## LLM throughput (§5.1), first pass — 2026-10-27

Board: ZimaBoard 2, 8 GB, Peregrine 0.1.0-dev, kernel 7.0.0-38-generic (Noble
HWE), Mesa ANV on *Intel(R) Graphics (ADL-N)* `[8086:46d4]`, llama.cpp
`b11700`. iGPU frequency range 200–1000 MHz, uncapped.

Model: Phi-4-mini-instruct Q4_K_M (2.31 GiB). `llama-bench -p 512 -n 64 -r 2`,
run outside the service with peregrine-llm stopped.

| Backend | Flags | pp512 tok/s | tg64 tok/s |
|---|---|---:|---:|
| iGPU (Vulkan) | `-ngl 99 -t 1 -fa 1 -ctk q8_0 -ctv q8_0` (the plan's config) | 25.58 | 4.11 |
| iGPU (Vulkan) | `-ngl 99 -t 1 -fa 0` (f16 KV) | 25.58 | 4.44 |
| CPU | `-ngl 0 -t 4 -fa 1 -ctk q8_0 -ctv q8_0` | 17.18 | 9.92 |
| CPU | `-ngl 0 -t 3 -fa 1 -ctk q8_0 -ctv q8_0` | 17.18 | 8.32 |

With `-ngl 0`, llama.cpp still sends large prompt batches to the Vulkan
device, so the CPU rows' pp512 is GPU-assisted.

**These iGPU figures are not the iGPU's real speed.** While llama.cpp runs on
Vulkan, the kernel logs `Fence expiration time out i915-0000:00:02.0:llama-bench`
every 20 s: i915 is timing out its GPU work. pp512 came out at exactly 25.58
for every model, which is 20.0 s per 512 tokens, a timeout and not compute.
With `-ngl 0` llama.cpp still sends large prompt batches to Vulkan, so the CPU
rows above hit the same timeouts in prompt processing. Next step for the iGPU
(plan §6.1 fallback b): the `xe` driver (`i915.force_probe=!46d4
xe.force_probe=46d4`) or i915 timeout tuning, then re-measure.

### 3B models, CPU only (`-dev none`: no Vulkan, no fence timeouts)

| Model | Licence | Size | 3 threads pp512 / tg64 | 4 threads pp512 / tg64 |
|---|---|---:|---:|---:|
| Llama-3.2-3B-Instruct Q4_K_M | Llama 3.2 | 1.87 GiB | 19.08 / **9.29** | 21.92 / 10.69 |
| SmolLM3-3B Q4_K_M | Apache 2.0 | 1.78 GiB | 18.43 / 9.66 | 22.22 / 11.09 |

**Chosen default: Llama-3.2-3B-Instruct on the CPU lane.** Three threads on
cores 1–3 (`LLM_DEVICE=none LLM_NGL=0 LLM_THREADS=3`, `AllowedCPUs=1-3`),
core 0 kept for the voice loop, the yield gate on. Through the real
`llama-server` with a short prompt it measured **10.2 tok/s** generation and
15.0 tok/s prompt. It is the same model family and licence as the Q6A's
Llama 3.2 1B (about 12 tok/s on Genie), at three times the parameters.

Quality spot check: tyre-safety advice was correct. The Denver–Moab distance
came back as "approximately 560 miles", against about 355 in reality. Like
any 3B model it still guesses at specific facts.

### Yield gate on hardware

`freeze` → `frozen`, `thaw` → `running`, and a freeze with no thaw is undone
by the watchdog after **5.1 s**. (It needed `AccuracySec=100ms`: the default
timer accuracy is one minute.)

## Memory: GPU buffers are charged to the cgroup (§2.2 unknown, answered)

With two 8K slots, peregrine-llm sat at 4607 / 4608 MiB (`memory.events max`
5664) with 4089 MiB of shmem: the weights, the KV caches and the compute
buffers allocated through Vulkan. Through the server, prompt processing fell
to 2.2 tok/s under the cap. One 8K slot brings it to 3639 MiB with no hits on
the limit, so `LLM_SLOTS=1` is now the default.

## Still to measure

§5.2 fast path (needs the Jabra), §5.3 under load, §5.4 power, §5.5 thermal,
memory and soak, plus the Q6A step-0 baseline.
