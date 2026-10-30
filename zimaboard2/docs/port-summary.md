# Peregrine on the ZimaBoard 2: what changed and what we gained

*Plain-language summary, 2026-10-27. The technical design is in
[docs/ZimaBoard2/README.md](../../docs/ZimaBoard2/README.md); the measured
numbers are in [benchmarks.md](benchmarks.md); installing is covered in
[install.md](install.md).*

The ZimaBoard 2 is Peregrine's premium board; the Radxa Dragon Q6A remains
the budget board, and the better pick for a really small battery bank. Buy a
ZimaBoard 2 (8 GB): [ZimaBoard 2 at the Zimaspace shop](https://shop.zimaspace.com/products/zimaboard2-single-board-server?sca_ref=12564611.fDJvFR9EWxuoB4a&utm_source=dave-floyd&utm_medium=affiliate&utm_campaign=zimaspace_affiliate&utm_content=Dave-Floyd&sca_source=GitHub)

> **Affiliate link.** TrailCurrent earns a small percentage if you buy
> through this link. It costs you nothing extra.

## In one paragraph

Peregrine, the TrailCurrent voice assistant, now runs on a ZimaBoard 2 as
well as on the Radxa Dragon Q6A. The ZimaBoard runs a language model three
times the size of the Q6A's: 3 billion "parameters" instead of 1 billion. It
answers at about the same speed, with eight times as much working memory for
each conversation. The ZimaBoard's built-in graphics chip was supposed to run
the model, but a driver problem makes it slower than the main processor
today, so the model runs on the processor instead. Peregrine replaced ZimaOS
on the board, installed over the network with no USB stick needed.

---

## 1. What we did to make Peregrine run on this board

The two boards are very different machines. The Q6A uses a phone-style
Qualcomm chip with a special AI accelerator (an "NPU"). The ZimaBoard is a
small Intel PC (an N150 with 8 GB of memory). Peregrine's main program is the
same on both. Only the layer underneath it changed.

| What | Q6A | ZimaBoard 2 |
|---|---|---|
| Runs the language model | Qualcomm's NPU, through Qualcomm's "Genie" software | **llama.cpp**, a widely used open-source engine, on the Intel processor |
| The model | Llama 3.2, 1 billion parameters | **Llama 3.2, 3 billion parameters** |
| Conversation memory ("context") | 1,024 tokens (roughly 750 words) | **8,192 tokens** (roughly 6,000 words) |
| Operating system | Ubuntu 24.04 for Arm | Ubuntu 24.04 for Intel/AMD PCs |

The pieces we built:

- **A translator ("bridge") for the language model.** Peregrine's voice and
  chat programs were written to talk to Qualcomm's Genie. Rather than rewrite
  them, we wrote a small go-between that speaks Genie's language on one side
  and llama.cpp's on the other. Peregrine cannot tell which board it is on.
- **Board settings.** These cover which processor cores do what, memory
  limits, the audio device and the network. All the differences live in the
  ZimaBoard's own configuration files. The shared program reads them and has
  no "if ZimaBoard…" branches.
- **A "yield gate".** When you speak a command, Peregrine briefly pauses any
  long web-chat answer in progress, so your command is not slowed down. A
  5-second safety timer resumes the chat if anything goes wrong.
- **A model manager.** `peregrine-model` lists, switches, imports and tests
  language models. Switching runs a quick check and switches back by itself
  if the new model fails.
- **Two installers.**
  - A **USB installer**: write it to a stick, boot the board from it, type
    `ERASE`, done.
  - A **network installer**, which is what replaced ZimaOS. It logs into the
    old system and restarts the board straight into the Peregrine installer.
    The installer downloads everything into memory and checks it before it
    erases anything. If something fails before the erase, a power cycle
    brings the old system back.
- **A test rig** that installs and boots the whole system inside a virtual
  machine on a workstation, so changes can be checked before they touch a
  real board.

Only one shared file changed, `src/assistant.py`, the main voice program. The
Q6A behaves exactly as before unless a ZimaBoard setting is switched on, apart
from the one bug fix described below, which helps both boards.

## 2. Problems found and fixed on the real board

| What you would have noticed | The cause | Fix |
|---|---|---|
| The model was very slow to start answering | Memory used through the graphics driver counted against the model's memory limit, so it ran pinned at the limit | One conversation slot instead of two |
| It heard you, beeped, then said nothing | A speech-recognition library had released an incompatible update five days earlier | Pinned the library to the last compatible version |
| "What is an ESP32?" always answered "I didn't get a response" | A safety filter, meant to stop the model repeating its internal instructions, rejected every answer containing technical words like ESP32, MQTT or UART | Those words now only count if you did not use them in your question. **This bug affects the Q6A too.** |
| The wake word worked only sometimes | A volume gate skipped quiet audio, and the pause between "hey" and "peregrine" counted as quiet | Gate switched off (the Intel chip has CPU to spare); microphone gain raised |
| The web chat showed a socket error on the first message | The first answer took about 24 seconds to begin, and the browser gave up waiting | It works once the model has read the chat's instructions. The proper fix is pending (see section 6). |

## 3. Why the built-in graphics (iGPU) isn't used

The plan was for the board's graphics chip to run the model. That would
leave the four processor cores free for listening and speech, which is what
the Q6A's NPU does.

**What happened:** on the graphics chip the model produced only about 4 words
per second, against about 10 on the processor. The system log showed why.
Intel's graphics driver (`i915`) gives each piece of graphics work a deadline,
and it kept stopping the model's work at that deadline. The message
`Fence expiration time out` appeared every 20 seconds. The driver is
cancelling and stalling the work, so it is the driver that's at fault, not the
chip.

**What it would take to make it work.** This is in rough order of effort, and
none of it is guaranteed to beat the processor.

1. **Try Intel's newer graphics driver, `xe`.** It was built for recent Intel
   chips and handles long-running compute work differently. It is a one-line
   change to the board's startup settings, plus a re-test.
2. **Loosen the old driver's deadlines.** `i915` has timeout and "heartbeat"
   settings that can be raised so long jobs are not cut off.
3. **Wait for updates.** Newer Linux kernels, newer Mesa (the open-source
   graphics library) and newer llama.cpp releases keep improving Intel
   graphics. A later kernel or llama.cpp version may simply work.
4. **Even when working, it may not win.** Generating text is limited mostly
   by memory speed, and the graphics chip shares the same memory as the
   processor. The real gain would be in *reading* long prompts (the slow
   first word in web chat) and in keeping the processor free for voice. It
   needs measuring before anyone counts on it.

Switching back is one configuration change, and the graphics engine is
already installed.

## 4. Speed: Q6A against ZimaBoard

Two speeds matter for a voice assistant:

- **Time to first word.** How long after you stop talking before the answer
  starts. This depends on how fast the model *reads* its instructions and
  your question.
- **Words per second.** How fast the answer comes once it starts. Peregrine
  begins speaking after the first sentence, so this mostly matters for long
  answers and the web chat.

Models count in "tokens", pieces of words. 100 tokens is about 75 words.

| | Q6A (Llama 3.2 1B on the NPU) | ZimaBoard 2 (Llama 3.2 3B on the CPU) |
|---|---|---|
| **Answer speed (generation)** | about 12 tokens/s | **about 10 tokens/s** (measured 9.2–10.2) |
| **Reading speed (prompt)** | not measured | about 20 tokens/s |
| **Voice question, model warmed up** | about 3 s per whole query (recorded in the repo) | **about 0.7 s** to the first token |
| **Voice question, cold** (first one, or after a web chat) | not measured | **about 4–7 s** to the first token |
| **Web chat, first message, cold** | not measured | **about 24 s** to the first token |
| **Web chat, later messages** | not measured | **about 0.7 s** to the first token |

"Warm" means the model has already read Peregrine's instructions and kept
them in memory, so it only reads your new words. "Cold" means it has to read
the instructions again. Today the board keeps one set of instructions at a
time, so a web chat pushes out the voice instructions and the other way
round. Section 6 has the fix.

**What this means for you:** a short spoken answer starts within about a
second when warm, a few seconds when cold. Simple commands such as "turn on
the lights" skip the language model entirely on both boards, so they stay
instant. The Q6A's time-to-first-word was never measured, so a strict
like-for-like figure needs that measurement first (plan step 0).

### Why the processor works here when it didn't on the Q6A

Before the Q6A moved to its NPU in March 2026, it ran the language model on
its processor, and it went badly. Answers ran at about 9 tokens/s, and even
simple voice commands, the ones the built-in phrases catch without the
model, slowed down dramatically. The ZimaBoard now runs on its processor and
gets about the same 9–10 tokens/s, with a model **six times larger**. These
are the differences, kept separate from what has not been measured yet.

**What the records show:**

| | Q6A on its processor (March 2026) | ZimaBoard 2 on its processor (now) |
|---|---|---|
| Model | Qwen 2.5, **0.5 billion** parameters (398 MB file) | Llama 3.2, **3 billion** parameters (1.87 GiB file) |
| Engine | Ollama | llama.cpp |
| Threads used by the model | **8** ("Use all 8 cores for inference") | **3** |
| Threads used by speech recognition | **8** | 3 |
| Cores kept free for voice | **None.** Neither service had any core limits or priority | Core 0 belongs to voice alone. Voice outranks the model 50 to 1 on the shared cores |
| Pausing the model during a voice command | None | **Yield gate:** the model is frozen the moment the wake word is heard (tested on the board) |
| Answer speed | about 9 tokens/s (reported) | 9.3 tokens/s on 3 threads (measured) |

*Sources: the Q6A columns come from the repository at commit `9a29e50`
(`setup/setup-board.sh`, `src/assistant.py`,
`config/voice-assistant.service`, 2026-03-12), the 398 MB figure from Ollama's
registry entry for `qwen2.5:0.5b`, and the 9 tokens/s from the maintainer.
The ZimaBoard column comes from the board's configuration and measurements
taken on it on 2026-10-27.*

**What follows from those numbers (arithmetic, not opinion):**

- To produce each token, a model has to read through all of its weights once.
  At 9 tokens/s, the Q6A's processor got through about **3.6 GB of model per
  second** (0.398 GB × 9), using all eight cores. The ZimaBoard gets through
  about **18.6 GB per second** (2.0 GB × 9.3) using **three** cores. On this
  workload the ZimaBoard's processor does about five times the work with
  fewer than half the cores.
- On the Q6A, the model and speech recognition each asked for every core at
  once, and nothing told the system which mattered more. On the ZimaBoard the
  voice pipeline has a core to itself, always wins on the shared ones, and
  freezes the model while it handles your command.

**What is *not* known yet:**

- **Why the Q6A's processor was that slow.** The repository records its
  settings, not where the time went, so these notes do not name a cause. It
  could be measured again on a Q6A with the same settings.
- **Whether ZimaBoard voice commands stay fast while a long answer is being
  generated.** The design above is meant to guarantee it, and the yield gate
  works on the board, but the timing under load has not been measured yet.
  This is test §5.3 of the port plan: time voice commands while a web chat is
  generating, and compare them with the same commands on an idle board.

## 5. How much smarter is it?

The model went from **1 billion to 3 billion parameters**. Parameters are,
roughly, the model's learned knowledge and reasoning ability. Bigger is not
automatically better, but within the same family, trained the same way, the
difference is large.

### Published test scores (Meta, the model's maker)

Same tests, same conditions, both models at full precision:

| What the test measures | Test | 1B (Q6A's model) | **3B (ZimaBoard)** | In plain terms |
|---|---|---:|---:|---|
| General knowledge across 57 subjects | MMLU | 49.3% | **63.4%** | From coin-flip territory on hard questions to mostly right |
| Grade-school maths word problems | GSM8K | 44.4% | **77.7%** | From under half right to more than three in four |
| Harder maths | MATH | 30.6% | **48.0%** | Roughly 1.5× as many solved |
| Common-sense reasoning | ARC-Challenge | 59.4% | **78.6%** | One wrong in five instead of two in five |
| Following instructions ("answer in two sentences") | IFEval | 59.5% | **77.4%** | Does what it is told much more reliably |
| **Using tools: producing correct commands** | BFCL v2 | 25.7% | **67.0%** | **2.6× better.** This is the skill behind "turn on the awning" style commands that miss the built-in phrases |
| Finding facts in a long text | Multi-needle | 75.0% | **84.7%** | Better use of a long conversation |

For scale, Meta's next size up, Llama 3.1 **8B**, scores 69.4% on general
knowledge. On that test the 3B model closes about **70% of the gap** between
the 1B and the 8B.

Source: Llama 3.2 model card, `meta-llama/llama-models`,
`models/llama3_2/MODEL_CARD.md`, instruction-tuned table.

### The same scores as a school report card

Each test score above becomes a letter grade on the usual scale: 90 and up
is an A, 80s a B, 70s a C, 60s a D, below 60 an F. Meta's next size up, the
8B model, is included as the student at the top of the class. The grades are
the published scores themselves; nothing here is estimated.

| Subject (test) | Q6A: Llama 3.2 **1B** | ZimaBoard: Llama 3.2 **3B** | For scale: Llama 3.1 **8B** |
|---|:---:|:---:|:---:|
| General knowledge (MMLU) | **F** (49) | **D** (63) | **D+** (69) |
| Maths word problems (GSM8K) | **F** (44) | **C+** (78) | **B** (85) |
| Harder maths (MATH) | **F** (31) | **F** (48) | **F** (52) |
| Reasoning (ARC-Challenge) | **F** (59) | **C+** (79) | **B** (83) |
| Doing what it's told (IFEval) | **F** (59.5, just under the line) | **C+** (77) | **B−** (80) |
| Turning requests into commands (BFCL v2) | **F** (26) | **D+** (67) | **D+** (67) |
| Remembering a long conversation (Multi-needle) | **C** (75) | **B** (85) | **A+** (99) |

**As students:**

- **The Q6A's 1B model is the student failing almost every subject.** It
  speaks fluently and is pleasant company, but it can't be trusted with a
  word problem, often ignores how it was asked to answer, and gets about one
  freely worded command in four right. Its one passing grade is memory, and
  even that is a C.
- **The ZimaBoard's 3B model is a solid C student.** It passes maths word
  problems, reasoning and following directions, earns a B for memory, and has
  gone from an F to a D+ on turning everyday requests into device commands. In
  tool use it ties the 8B, the top student. It still fails the hardest maths,
  like everyone in this class.
- **The 8B is a B student.** It is better than the 3B, but not dramatically,
  and the jump from 1B to 3B is far bigger than the jump from 3B to 8B.

**Keep in mind:** these tests are hard. The general-knowledge test covers 57
subjects up to university and professional level, so an F there is not "can't
hold a conversation". The report card compares the students against each
other; it does not measure them against people. And like any student
answering from memory, even the C student gets specific facts wrong (see the
Denver–Moab example below).

### Two more things that make it feel smarter

- **Eight times the working memory.** The Q6A's model holds 1,024 tokens, so
  Peregrine has to cut its instructions to about 2,200 characters, which
  leaves out much of your device list. The ZimaBoard holds 8,192 tokens, so
  Peregrine gives it 8,000 characters of instructions and lets web chats run
  about ten times longer before trimming.
- **Less damage from compression.** To fit the NPU, the Q6A's model is
  compressed to its NPU format, and Meta's own tests show compressed 1B models
  scoring lower again. On general knowledge, for example, 43–49% against 49%
  uncompressed. The ZimaBoard's model uses a standard 4-bit format that
  usually keeps it close to its full-precision scores.

### What we saw on the board

- *"What should I check on my trailer tires before a long drive?"* →
  "Check your trailer tire pressure, tread depth, and look for any signs of
  wear, cracks, or damage." Correct and to the point.
- *"Tell me a bedtime story."* → a coherent short story, spoken smoothly.
- *"How far is it from Denver to Moab?"* → "approximately 560 miles". **That's
  wrong** (it's about 355). A 3B model is far better at reasoning and
  following instructions than a 1B, but it still guesses at specific facts
  such as distances, dates and numbers. Facts Peregrine needs to get right
  (sensor readings, location, device states) come from TrailCurrent's own
  data, not from the model's memory.

**In short:** expect answers that follow instructions, stay on topic, handle
simple reasoning and maths, and turn loosely worded requests into the right
device commands far more often. Do not expect it to know trivia reliably.

## 6. Still open

- **Faster first words for web chat:** keep the voice and chat instructions
  in memory side by side (two slots, using a memory-saving loading mode), and
  have the web page wait patiently instead of giving up.
- **The graphics chip:** try the `xe` driver (section 3).
- **Measurements:** the Q6A's time to first word, power use against the Q6A,
  heat under load and a 24-hour soak test (plan section 5).
- **MQTT** (the link to the rest of TrailCurrent) needs the broker address
  set on the board before voice commands can switch devices.
- **Rebuild the installers** so they include every fix from section 2.
