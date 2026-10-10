---
title: Efficient local inference -- an efficient CPU floor, accelerators when present, and a battery policy
status: draft
author: Sergii Pogorielov
created: 2026-10-07
last-audited: 2026-10-10
audited-at: d32c084921
doc-pr: 18560
implementation-prs: [18548]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Efficient local inference -- an efficient CPU floor, accelerators when present, and a battery policy

- Status: draft. Phase 1 (§5) is on main as `235e6b78a0`
  ([#18548](https://github.com/kirodotdev/KiroCrew/pull/18548)); nothing else in
  this document is built. Every claim about current behaviour was verified
  against `67c1304b06`, the baseline before Phase 1. The status stays `draft`
  rather than `partial` because the design is not accepted: Phase 1 is a
  performance fix that needed no RFC and landed on its own, and `partial`
  would let the First Principles lane read the later phases as decided.
- Scope: the local models the gateway runs on the user's own machine. Those are
  live speech-to-text (whisper.cpp through `pywhispercpp`), embeddings (the
  bundled llama.cpp runtime), the Jev decision models (the PyTorch servers in
  `decisions/local_servers/`) and Knowledge Library extraction. Text-to-speech
  (piper) is in scope for the battery policy only. It was not measured.
- Builds on [stt-streaming](../system-specs/modules/stt-streaming.md), which
  already measured the fixed per-decode floor that §4.1 removes. It agrees with
  that spec's reason for having no `stt.local_backend` key, and §4.2 is shaped
  by that reason.
- Relationship to [#1693](https://github.com/kirodotdev/KiroCrew/issues/1693)
  (pluggable model providers): orthogonal. This RFC adds no cloud provider and
  no agent-model provider. Its endpoint layer (§4.3) is loopback-only by default
  and covers background and voice workloads, not the agent.

## 1. Summary

Kiro Crew runs more and more inference on the user's own machine. Each workload
picks its own thread count and its own cadence. None of them knows whether the
machine is on battery, and on Linux and Windows none can use the GPU or NPU that
most laptops sold since 2023 carry. On a laptop, a single 40-minute meeting
transcription can take a large share of the battery.

This RFC proposes three layers and one policy, in order of how many machines they
help:

1. **An efficient CPU floor (every machine, no new dependency, no new key).**
   Stop encoding 30 seconds of padding for every live preview, then right-size
   threads where measurements across machines support it. On the reference
   machine, at today's thread count, sizing the encoder window cut each live
   preview's CPU and latency about 2.5x. Per second of speech, CPU fell 28% while
   previews arrived 27% more often, because the cadence counts from the end of a
   decode (§2.2).
2. **Accelerators when present, the CPU path as the fallback.** Ship or detect a
   GPU build of the same runtime (Metal on macOS, Vulkan elsewhere), and use the
   OS speech engine where the platform offers one. A build that fails the
   existing preflight probe lands on layer 1. This layer is a packaging and
   loader change, not a config key.
3. **A local endpoint for vendor stacks, NPUs included.** Kiro Crew does not
   bundle vendor SDKs. It speaks the OpenAI-compatible API to a server on the
   same machine (Lemonade on AMD, an OpenVINO server on Intel, and so on), and
   falls back to the in-process engine when that server is absent or slow.
4. **One battery policy.** A shared `power_source()` read, plus a
   `power.on_battery` setting each workload maps to its own safe degradation.
   Background work may pause, and interactive work only degrades its preview.

## 2. Motivation

### 2.1 Current state (verified at `67c1304b06`)

**Speech-to-text.**

- **Threads.** `thread_count()` in `stt/engine.py` takes half the CPUs this
  process may run on, at most `THREAD_CEILING` (16), a choice its docstring
  records as measured on a 32-core Graviton3 host. On a 16-core / 32-thread
  laptop that is 16 threads for the 148 MB `base` model.
- **Encoder window.** `audio_ctx` is set nowhere under `src/kiro_crew/stt/`, so
  whisper.cpp encodes the full 1500-frame (30 s) window for every decode. That
  includes a live partial over 1-8 s of speech (`MAX_PHRASE_SECS` in
  `stt/session.py`). stt-streaming's "Decode cost is a fixed floor plus a small
  marginal term" section measured the consequence: about 0.78 s fixed plus
  0.08 s per audio-second on a 32-core build. Phase 1 has since sized the
  window for partials and phrase commits (`235e6b78a0`); finals still encode
  the full window.
- **Cadence.** A partial is decoded every `DEFAULT_PARTIAL_INTERVAL_MS` (400) in
  `stt/limits.py`, counted from when the previous inference completes. The spec
  notes that this default sits below the fixed floor, so on a CPU build the
  cadence is bounded by inference. While someone speaks, the decoder alternates
  "decode, wait 400 ms" for as long as they speak.
- **Long sessions.** A dictation session ends at `MAX_SESSION_SECS` (600 s) in
  `stt/session.py`. The Meetings app's live transcription
  (`useMeetingTranscription` in `website/src/apps/meetings/hooks/`) reuses the
  same stream, and its watchdog reconnects a dropped or ended socket. A
  40-minute talk is therefore 40 minutes of the cadence above.
- **Build.** The `voice` extra in `extras.py` pins `pywhispercpp>=1.5,<2`. The
  PyPI wheels are CPU builds; `stt/capabilities.py` documents this and reads the
  linked backend honestly from `whisper_print_system_info()`. On the reference
  machine the installed 1.5.1 wheel reports
  `CPU : SSE3 AVX AVX2 F16C FMA BMI2 OPENMP REPACK`: no GPU backend, and no
  AVX-512 although the CPU supports it.

**Embeddings.** The bundled llama.cpp runtime's per-platform library table in
`embeddings.py` lists `libggml-metal` for both macOS entries and only
`libggml-cpu` for Linux and Windows. So macOS already has the layer-2 shape this
RFC proposes, and the other platforms do not. `MemoryConfig.embedding_threads`
defaults to 4.

**Jev decision models.** The local runtime builds each preset's environment from
`TORCH_CPU_INDEX` (`decisions/local_runtime.py`), and every launcher in
`decisions/local_servers/` (`plumb_cpu.py`, `laya_cpu.py`,
`strands_decider_cpu.py`) pins `device="cpu"`. Only the Strands launcher bounds
its torch threads, to half the usable CPUs and at most 16, and admits one
inference at a time
([#17618](https://github.com/kirodotdev/KiroCrew/pull/17618)). The Plumb and Laya
launchers still let torch take every core. The `tool.risk` point consults the
model on every tool call. On the reference machine, one working day logged 197
decisions (169 `tool.risk`, 28 `model.route`) at about 1 s median each, with
Plumb-4B resident at 8.1 GB RSS.

**Power awareness.** None. `power.py` holds `SleepInhibitor`, which keeps the
host *awake* during a task. Nothing under `src/` reads whether the host is on
battery: `git grep power_source` and `git grep POWER_BATTERY` return nothing.

### 2.2 Measurement

Reference machine: AMD Ryzen AI Max+ 395 (16 cores / 32 threads, Zen 5),
Linux 7.2, Python 3.14, `pywhispercpp` 1.5.1 CPU wheel, whisper `base`
(`ggml-base.bin`), a running gateway on the same machine.

Method: espeak-ng speech resampled to 16 kHz, `single_segment=True`,
`no_context=True` and `no_timestamps=True` as the engine sets them, one warm-up
decode, then timed decodes. CPU time is the process's `getrusage` user+sys.
"Cores busy" is CPU time over `decode + 400 ms`, the live cadence while someone
speaks.

**One 8 s phrase, 10 decodes per row.** 8 s is `MAX_PHRASE_SECS`, the longest
phrase a partial decodes; 512 frames is the window §4.1 gives it.

| Threads | Window | Wall / partial | CPU / partial | Cores busy while speaking |
|---:|---|---:|---:|---:|
| **16 (today)** | **full (1500)** | **359 ms** | **5.75 s** | **7.6 (24% of 32)** |
| 16 | 512 | 150 ms | 2.42 s | 4.4 |
| 8 | full | 480 ms | 3.85 s | 4.4 |
| 8 | 512 | 186 ms | 1.52 s | 2.6 |
| 4 | full | 751 ms | 3.02 s | 2.6 |
| 4 | 512 | 272 ms | 1.11 s | 1.7 |

The first row is today's behaviour. It matches what the reporter saw while
transcribing a 40-minute talk in the Meetings app: about a quarter of the
machine, continuously.

**The window sweep: 4 voices (English, German, French, Ukrainian), every phrase
length from 1 s to 8 s in 0.5 s steps, 16 threads, `language=auto`.** The window
of §4.1 (512 frames for every clip in the sweep) against the full window, one
decode each:

| | Full window | Window of §4.1 |
|---|---:|---:|
| CPU, 57 clips | 543 s | 220 s (2.5x less) |
| Wall time, 57 clips | 33.8 s | 13.6 s (2.5x less) |
| Clips where it was slower | -- | 2 (both Ukrainian) |

- The text differed mostly in capitalisation, trailing punctuation and words the
  full window itself changes from one phrase length to the next.
- espeak-ng's Ukrainian voice is unintelligible to `base` at either window, so
  its two slow clips say little about real Ukrainian speech.
- The full window also misfired on its own: one 6.5 s French clip came back as
  Thai text and took 1.3 s.

**Why a floor, and why 512.** A window sized tightly to the clip is not safe:

- At 128-384 frames, some 1-2 s clips, and every 8 s clip whose window was
  shorter than the audio, sent whisper.cpp into its temperature fallback. That
  is up to five re-decodes at rising temperature, which cost 2-12 s per partial.
  A 1 s clip took 2.5 s at 320 frames against 0.36 s at the full window.
- From 448 frames up, no clip in the grid fell back.
- Capping `max_tokens` does not help: it stops the repetition check that
  triggers the fallback, and shows the repetition instead.

**Switching windows on one context works.** The engine keeps one context and
alternates sized previews with full-window finals. On the real binding:

- Every call returned status 0, and the params came back to 0 after each one.
- The final transcript was byte-identical every time. That holds for the
  encoding. Under `language: auto` a final's language detection can differ
  (below).
- Under `auto`, the first sized decode after a final costs about a full one
  (~300 ms), because its language detection runs at the final's full window.
  The ones after it cost ~150 ms. With a pinned language there is no such
  penalty.

**Threads and the OpenMP wait policy, for later phases.**

- At the full window, 16 to 8 threads cut CPU 33% but added 34% latency.
  With the sized window it cut CPU 37% for 24% more latency. That trade is
  upstream's to make (open question 6).
- A passive OpenMP wait policy saved about 10% of CPU at 16 threads (open
  question 1).

**Per second of speech, the saving is smaller than per preview.** The partial
interval starts when a decode ends, so a faster decode buys more previews as
well as idle time. The same 54 s English talk was streamed through a real
`LocalSession` in real time, 100 ms at a time like the browser, after a
gateway-style prewarm:

| | Full window | Window of §4.1 |
|---|---:|---:|
| Average CPU while speaking | 9.4 cores | 6.8 cores (-28%) |
| Preview decodes | 64 | 81 (+27%) |
| Median time per decode | 507 ms | 242 ms |

That leaves a choice the battery policy can make (§4.4): keep the fresher
previews, or lengthen the interval on battery and take the whole saving as
idle time.

**On battery, live.** The reference machine on battery, screen and load held
steady, sampling the battery's reported power every 2 s:

- Idle: 19.5-22.5 W.
- A 7.5-minute Meetings transcription: 30 W.
- 1.5 minutes of dictation: 39 W.

Whisper's threads were the only busy process. In the dictation, the gateway's own
decode timings showed every preview at full-window cost (500-700 ms). The first
version of the breaker, which tripped at 1x its reference, had turned sizing off
for the loaded model on a busy host. §4.1 records the fix.

After the fix, a second dictation of the same length on the same machine:

| Dictation, on battery | 1x breaker (tripped) | 2x breaker (§4.1) |
|---|---:|---:|
| Preview decode, median | ~530 ms | ~185 ms |
| Gateway CPU while dictating | 9.8 cores | 6.2 cores (-37%) |
| Power above idle | +19.6 W | +12.2 W (-38%) |

- All 20 previews and 8 phrase commits ran sized, at 150-240 ms.
- Only the first preview after a final took 290-445 ms.
- The two dictations spoke different words, so this is a field check, not a
  controlled comparison. The replay above is the controlled one.

**Preview quality on real speech.** Two read-aloud recordings by one speaker,
captured in the browser the way the dashboard captures them: 74 s of English and
62 s of Ukrainian, each scored against the text that was read. Each was streamed
through a real `LocalSession` in real time with the language pinned, and every
partial and phrase commit was decoded again at the full window on the same
audio. Each preview was aligned to its best-matching stretch of the text, and
the count is the word errors the window of §4.1 added over the full window on
that same clip, over two runs per window:

| Extra word errors per 100 preview words | English | Ukrainian |
|---|---:|---:|
| 512 frames (§4.1) | +0.2 (203 previews) | +5.2 (178 previews) |
| 768 frames | +1.5 | -2.7 |
| 1024 frames | -2.1 | +4.4 |
| Full window under 2 s, else 512 | +1.8 | +4.7 |

- Finals always encode at the full window. Under `language: auto` their
  language detection changes, which the next paragraph measures.
- English: no measurable difference. The 95% interval of the per-preview
  difference spans zero.
- Ukrainian: about 49% of preview words wrong at the full window and 54% at 512
  frames. `base` transcribes this speaker's Ukrainian poorly at either window.
- A larger floor does not reliably close that gap: 768 frames removed it, 1024
  frames brought it back, and keeping the full window for short clips left it.
  The effect does not follow the window's size, which suggests a weak model
  flipping uncertain words under any change of window rather than missing
  context. 768 frames would also cost about 40% more per preview.
- The text on screen at the end of a session cannot rank windows: each run cuts
  phrases differently, so the full window alone scored 28.9-29.6% (English) and
  51.9-60.5% (Ukrainian) across runs. Hence the paired count per preview.
- The language matters more than the window. With `language: auto`, previews
  switched to Polish, Spanish, Russian or Tamil at both windows, and pinning
  Ukrainian cut its on-screen error rate from about 80% to about 52%. Pinning a
  session's language once it is confidently detected is a separate change.

**Finals under `language: auto`.** whisper.cpp detects the language before it
applies a call's window, so detection uses the previous call's. A final
normally follows a sized preview, so it detects over 512 frames (~10 s)
instead of 30 s, while its encoding still uses the full window. On 47 finals
of 3-25 s of real speech from the same recordings, with `base`, each decoded
after a final and again after a preview:

| Finals under `auto` | English | Ukrainian |
|---|---:|---:|
| Word errors against the read text, after a final -> after a preview | 119 -> 117 | 334 -> 312 |
| Finals better / worse / same | 2 / 1 / 21 | 10 / 2 / 11 |
| Detected language changed | 0 | 5, all Russian -> Ukrainian |

- Short finals gain: detection reads mostly speech instead of 20 s or more of
  padding. The median final also got faster, 0.50 -> 0.34 s.
- It can lose on audio that opens with ~10 s of silence, or that switches
  language after its first 10 s: a constructed 12 s English plus 15 s Ukrainian
  final came out all English. The session starts an utterance at speech
  onset, which rules out the first in practice.
- Restoring the 30 s detection costs a full-window encode per final, since no
  cheaper call resets the width. That would make finals slower than before
  Phase 1, so Phase 1 keeps the measured behaviour.
- A pinned language is unaffected.

Caveats, which Phase 0 (§5) turns into tested numbers:

- One machine and one model. Synthetic voices, plus two read-aloud recordings by
  one speaker.
- CPU-seconds approximate energy. The battery readings above lag by several
  seconds and include the screen and everything else running.

## 3. Goals and non-goals

### Goals

1. Every laptop gets the CPU win (layer 1) with no setup, no new dependency, no
   new config key and no change to committed transcript text.
2. A machine with a usable accelerator build uses it. A build that fails, at
   load or in the probe, lands back on the efficient CPU path without user
   action.
3. NPUs and vendor runtimes are reachable without Kiro Crew bundling or tracking
   any vendor SDK.
4. One battery signal and one setting cover every local workload. Each workload
   decides what "reduced" means for itself.
5. Defaults are set from measurements across many machines, not from one
   laptop.

### Non-goals

- Cloud or remote model providers, and the agent model (#1693).
- Bundling NPU SDKs (Ryzen AI / Vitis AI, OpenVINO, QNN, CoreML conversion).
- Moving Jev to a GPU. Its runtime is a PyTorch CPU environment by design (the
  `local_runtime.py` module docstring). This RFC only bounds its threads and
  residency.
- Changing default models or model downloads.
- A backend-selection config key without a loader that acts on it (the reason
  stt-streaming gives for having no `stt.local_backend`).
- Reviving the adaptive partial-cadence budget stt-streaming retired. The
  cadence stays `stt.partial_interval_ms`.
- Throttling interactive work: a battery policy never blocks a user-started
  dictation, query or import. It degrades only previews and background work.

## 4. Design

### 4.1 Layer 1 -- the efficient CPU floor

**Speech-to-text.** One change in `stt/engine.py` and its two callers in
`stt/session.py`, with no new config key:

- **A sized encoder window for display-only decodes.** Partials and phrase
  commits ask for `audio_ctx` = the clip plus a 2 s margin, at 50 frames per
  second, rounded up to 64, and never under 512. A clip within a step of 30 s
  gets the full window.
  - The window is set on the shared params for that one native call and
    restored afterwards.
  - The `Model.transcribe` fallback path keeps the full window, because
    pywhispercpp would store the parameter there for good.
- **Finals keep the full window.** The text the user keeps is always encoded at
  the full window. Under `language: auto` a final's language detection follows
  the preceding preview's window (§2.2), which measured slightly better on real
  speech. Sizing finals
  is a separate, measured change (open question 2).
- **A breaker.** The engine keeps the most recent full-window decode's time as a
  reference. After 3 sized decodes IN A ROW take more than twice that reference,
  the engine stops sizing for the rest of the loaded model's life.
  - It catches whisper.cpp's temperature fallback, which made a sized decode
    7-21x the full window, not ordinary load, which moves a preview by tens of
    percent. A 1x threshold tripped in live use (§2.2).
  - The prewarm, a decode of 1 s of silence, may be the first reference, so a
    session with no final yet still has one.
  - Requiring a run, not one slow decode, matters: under `auto`, the first
    sized decode after a final is about as slow as a full one (§2.2).
  - An aborted decode records nothing, because its early unwind would understate
    the reference.
- **Threads stay at `thread_count()` in this phase.** Changing them trades
  latency for CPU, a trade upstream already measured once and decided on (open
  question 6).

**OpenMP wait policy.** At today's 16 threads a passive policy saved about 10%
(§2.2). It is a process-wide setting for the gateway and the bundled embedding
runtime carries its own `libgomp`, so it is left as open question 1, not a
phase.

**Jev.** Extend the bound #17618 gave the Strands launcher (half the usable
CPUs, at most 16, one inter-op thread) to the Plumb and Laya launchers. Add an idle-unload timeout, matching what STT
already has (`stt.idle_evict_secs`), so an 8 GB model is not resident on a
machine that has made no decision in an hour.

**Embeddings.** No code change: the default (4) is already sane. The benchmark
(Phase 0) reports it so outliers are visible.

### 4.2 Layer 2 -- accelerators when present

stt-streaming states the constraint this layer must respect: installing
acceleration is a packaging problem, and a backend selector is only worth
shipping with a loader that acts on it. The pieces that already exist:

- `stt/capabilities.py` reads which backend a build actually linked. It
  deliberately ignores `use_gpu`, which is granted even on a CPU-only build.
- `probe_native` in `stt/preflight.py` probes a native build in a separate
  process before the gateway loads it in-process. A build that crashes there
  never takes the gateway down.

What this layer adds:

- **An accelerated build to install.** One Vulkan build covers AMD, Intel and
  NVIDIA GPUs, integrated and discrete, on Linux and Windows. Metal covers
  macOS. Who builds and hosts it is open question 3.
- **A loader that can fall back.** When the accelerated build is present, it is
  the first choice. If it fails `probe_native`, fails to load, or decodes slower
  than the CPU build, the loader uses the CPU build, and status and
  `kirocrew doctor` show the reason. Both builds are installed side by side;
  nothing is selected by a key.
- **The OS speech engine.** On macOS it already exists as the `apple` STT
  provider (`_VALID_STT_PROVIDERS` in `config/sections.py`). Whether the battery
  policy should suggest it is open question 4.
- **Embeddings.** The same pattern extends to Linux and Windows: a Vulkan entry
  beside today's per-platform library table in `embeddings.py`.

### 4.3 Layer 3 -- local endpoints for vendor stacks and NPUs

On-device NPU support is fragmented. Each vendor has its own SDK and operating
system coverage, and it changes from release to release. For example, on the
reference machine Lemonade reports the XDNA2 NPU as available, but its
whisper.cpp NPU backend as "Requires Windows". Vendor stacks increasingly ship a
local server that speaks the OpenAI-compatible API, so Kiro Crew should be a
client of that API and not an integrator of the SDKs.

- **Provider.** `openai_compat` for each workload that has a standard route:
  - Knowledge extraction: `/v1/chat/completions` with a capped JSON schema.
  - Speech-to-text: `/v1/audio/transcriptions` for finals. A server that offers
    a realtime route can also take partials.

  This is a real provider with its own code path, not a hint to the in-process
  engine.
- **Egress guard.** Loopback-only by default. A non-loopback base URL is
  refused unless an explicit `*_allow_remote` is set. Every allow or refuse
  decision is audited to SEL, because document chunks and audio are private.
- **Fallback.** An unreachable, failing or slower-than-real-time endpoint falls
  back to the in-process engine for that request. It is reported in status, and
  never retried in a tight loop.
- The extraction half is already implemented on a branch (Phase 4).

### 4.4 The battery policy

- **Signal.** `platform_compat.power_source()` returns `ac`, `battery` or
  `unknown`:
  - Linux: any online `type == Mains` supply under `/sys/class/power_supply`
    means mains. That includes USB-C PD, which the `AC0` supply alone misses.
  - macOS: IOKit's providing power type.
  - Windows: `GetSystemPowerStatus`.

  `unknown` is never treated as battery. Deferring work on a probe that cannot
  read the power source would be a silent work stoppage.
- **Setting.** `power.on_battery = normal | reduced | paused`, default `normal`,
  which changes nothing. Each workload may override it with its own key, which
  takes the same three values plus `inherit`, its default, meaning "follow
  `power.on_battery`". So one workload can stay `normal` while the global value
  is `reduced` or `paused`.

| Workload | `reduced` | `paused` |
|---|---|---|
| Knowledge background sweeps | chunk budget / 4, floored | skip the sweep; explicit imports still run |
| STT partials | effective interval x3, CPU build pinned | finals only (no live preview); dictation still works |
| Embeddings, bulk re-embed | fewer bulk threads, lower duty | defer until on mains |
| Jev local model | fewer threads, shorter idle-unload | point falls back to its non-local lane, except `tool.risk` (below) |
| TTS | unchanged | unchanged (user-requested output) |

- **Latching.** The decision is made once per unit of work, so unplugging
  mid-sweep never aborts work in flight.
- **A risk decision never loosens on battery.** `tool.risk` runs on every tool
  call and feeds governance, so the power source must never change which model
  judges a call, or send tool context off the host. It is exempt from `paused`:
  on battery it takes at most `reduced` (fewer threads, shorter idle-unload) and
  stays on its local model.

## 5. Migration plan

Each phase is independently shippable and independently abandonable.

**Phase 0 -- measure (`kirocrew bench local-inference`).** Extend the existing
`kirocrew bench` command (`cli_bench.py`, today a memory benchmark).

- For STT, embeddings and, if configured, the Jev endpoint, it reports: the
  backend linked, threads, wall time and CPU-seconds per unit, and, where the
  OS exposes it, the battery discharge rate.
- Results are numbers only. No audio or text leaves the machine, and nothing
  is uploaded.
- The PR adds a results template, so the community can post rows from their
  own laptops.

*Exit criteria:* the command runs on Linux, macOS and Windows with only the
`voice` extra installed, and prints a stable, machine-readable result row.

**Phase 1 -- sized window for display-only STT decodes.** The §4.1 window and
breaker, with no new config key. On main as `235e6b78a0`
([#18548](https://github.com/kirodotdev/KiroCrew/pull/18548)).

*Exit criteria:*
- A final never decodes with a sized window, and the shared params are back at
  the full window after every sized call, including one whose native call
  raised. Unit tests in `test_stt_engine.py` and `test_stt_session.py` assert
  both.
- On the reference machine, at today's thread count, preview CPU and wall time
  drop at least 2x across the §2.2 sweep, and CPU per second of speech drops at
  least 20% in the real-time session replay.
- In a live session on a busy host, previews stay sized: the gateway's own decode
  timings show sized-window times, not full-window ones.
- No partial falls into the temperature fallback at the floor in that sweep
  that did not also do so at the full window, or the breaker turns sizing off.

**Phase 2 -- `power_source()` and `power.on_battery`.** The shared signal and
setting, with knowledge sweeps and STT partials as the first consumers. The
knowledge half is implemented on a branch (`knowledge.battery_sweep_mode`,
with tests). That key's `inherit` value means what `normal` means here, so it
is renamed to match before it ships.

*Exit criteria:*
- Unit tests cover each platform reader against fixtures. Among them: USB-C PD
  online with `AC0` offline reads as `ac`; no mains supply reads as `unknown`;
  and `unknown` never throttles.
- An explicit import or dictation is never blocked by the policy.

**Phase 3 -- accelerated builds with CPU fallback.** Packaging for a Vulkan and
a Metal build, and the fallback loader of §4.2.

*Exit criteria:*
- On a machine with the accelerated build, Phase 0 shows it selected and
  faster.
- A build made to fail `probe_native` falls back to the CPU build, with the
  reason shown in status and in `kirocrew doctor`.

*Blocked on* open question 3 (distribution).

**Phase 4 -- local endpoint providers.** First knowledge extraction
(`knowledge.extraction_provider = openai_compat`, loopback guard, SEL audit,
schema-capped replies), then STT finals over `/v1/audio/transcriptions`.

*Exit criteria:*
- A non-loopback URL is refused without `*_allow_remote`, and audited.
- A stopped endpoint falls back within one request timeout.
- Existing ACP extraction is byte-for-byte the default.

**Phase 5 -- Jev threads and idle unload.** The #17618 thread bound in the
Plumb and Laya launchers, and an idle-unload timeout in `local_runtime.py`.

*Exit criteria:* the decision log's latency p50 does not regress, and RSS
returns to zero after the idle timeout with no decision in flight.

## 6. Backward compatibility

- Phase 1 adds no config key. It changes how previews are decoded. Finals keep
  the full window, and under `language: auto` only their language detection
  narrows (§2.2).
- Every key added later defaults to today's behaviour:
  - `power.on_battery = normal`, the default, is a no-op, and a per-workload
    key's default, `inherit`, follows it.
  - Endpoint providers are off until configured.
- No config migration is needed. A user who set `memory.embedding_threads` or
  `stt.partial_interval_ms` keeps their value.

## 7. Security considerations

- Endpoint providers send private document chunks or audio to another process.
  They are loopback-only by default, with explicit opt-in for remote hosts and a
  SEL audit row for each allow or refuse decision. They reuse the existing
  credential-by-env-name pattern and never store keys in config.
- Accelerated builds are native code loaded into the gateway. They pass the
  existing sha256-pinned download path and the out-of-process `probe_native`
  check before the first in-process load, as the CPU build does today.
- The benchmark uploads nothing. Its output is a local file the user may choose
  to share.
- The battery policy never loosens a governance decision: `tool.risk` stays on
  its local model on battery (§4.4), so the power source changes neither the
  model that judges a tool call nor where the call's context goes.

## 8. Alternatives considered

- **Raise `DEFAULT_PARTIAL_INTERVAL_MS`.** Cheaper, but it makes the live
  preview feel worse on every machine, and every preview still pays the 30 s
  encoder window.
- **Size the window exactly to the clip.** Measured and rejected (§2.2): small
  windows send short clips into whisper.cpp's temperature fallback, which costs
  far more than the full window.
- **Cap `max_tokens` on previews.** It shows the repetition the temperature
  fallback exists to throw away.
- **An adaptive cadence budget.** stt-streaming already retired one. With a
  sized window the decode fits inside the existing interval, so the fixed
  interval works as it was meant to.
- **A backend-selection key.** Rejected for the reason stt-streaming records:
  without a loader that acts on it, it is surface with no function. §4.2 adds
  the loader and no key.
- **Ship a GPU build by default, with no CPU fallback.** Driver crashes and
  wheel size land on every user, including those without a usable GPU. Phase 3
  gets the benefit with the existing probe as the guard.
- **Integrate NPU SDKs directly.** Every vendor differs by OS and release. The
  endpoint layer gets the same hardware with a single standard client.
- **Switch STT runtime (for example to CTranslate2 / faster-whisper).** A new
  dependency, and it does not reach AMD and Intel GPUs. whisper.cpp already
  covers Metal, Vulkan and CUDA from one codebase.
- **Leave it to configuration.** Users cannot tune what they cannot see. The
  reference machine's quarter of the machine (about 7.6 cores) while
  transcribing came from defaults, not from a misconfiguration.

## 9. Open questions

1. **OpenMP wait policy.** Worth about 10% at today's thread count (§2.2). Is a
   process-wide passive policy acceptable for the gateway, given the bundled
   embedding runtime shares the setting?
2. **Reduced `audio_ctx` on finals.** Should finals also use a sized window,
   behind a measured word-error-rate gate on real multilingual speech? Phase 1
   keeps finals unchanged until this is answered.
3. **Who builds and hosts accelerated builds?** Options: Kiro Crew's model CDN
   (already sha256-pinned), upstream `pywhispercpp` release assets, or
   user-built only, with the loader simply detecting them. (Phase 3 is blocked
   on this.)
4. **The GPU and the OS engine on battery.** On small models, waking an
   integrated GPU can cost more energy than the efficient CPU path. Should the
   battery policy pin the CPU build, and should it suggest the `apple` provider
   on macOS? Phase 0 data from several vendors should decide this, not this
   document.
5. **Jev on battery.** §4.4 keeps `tool.risk` on its local model on battery.
   Should `tool.risk` on read-only tools skip the model entirely on battery,
   which would save the most, or is that a loosening §4.4 rules out?
6. **Threads.** 16 to 8 threads saves a third of the CPU for about a quarter
   more latency on the reference machine (§2.2). Should the default move, should
   it follow the battery policy instead (fewer threads only on battery), or does
   Phase 0 data show enough spread to need a key?
