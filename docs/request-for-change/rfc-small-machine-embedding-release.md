---
title: Small machines release the embedding model sooner, and nothing waits for it
status: accepted
author: kirocrew agent session, directed by the repository owner
created: 2026-11-13
last-audited: 2026-11-13
audited-at: 911bde870d
doc-pr: null
implementation-prs: [37128]
tracking-issues: [26319]
supersedes: []
superseded-by: []
---

# RFC: Small machines release the embedding model sooner, and nothing waits for it

Status: accepted. The repository owner asked for this change in the conductor chat on
2026-11-13 and accepted this document on 2026-11-14; it takes effect when the status
change merges. The implementation is
[#37128](https://github.com/kirodotdev/KiroCrew/pull/37128), which rebases onto the
base that carries this status. This document landed first because it changes a
shipped default (`memory.embedding_idle_evict_secs`), and the First Principles lane
reads that decision off the base branch. Every code reference was read at `911bde870d`.

## 1. Problem

The local embedding model (the bundled Qwen3 0.6B GGUF, run in process by llama.cpp)
is the largest single part of a gateway's memory. Measured on Linux in one process
with the real `LlamaCppEmbedder`:

| State | VmRSS | RssAnon (private) |
|---|---:|---:|
| Before the first load | 48 MB | 36 MB |
| Loaded | 1,568 MB | 944 MB |
| After the idle unload | 74 MB | 51 MB |

The model stays loaded for `memory.embedding_idle_evict_secs` after the last embed:
3600 s by default (`_DEFAULT_IDLE_EVICT_SECS` in `src/kiro_crew/embeddings_settings.py`).
A 2-hour soak in [#26319](https://github.com/kirodotdev/KiroCrew/issues/26319) showed
the gateway at 1,773 MB until exactly 3,600 s after its last embed, then 297 MB. On a
16 GiB laptop that is about 9% of all memory, on 8 GiB about 18%, held for an hour in
which a browser, an editor and the agent engines compete for the same memory.

[#26319](https://github.com/kirodotdev/KiroCrew/issues/26319) and
[#26348](https://github.com/kirodotdev/KiroCrew/pull/26348) propose 600 s for every
machine. The data posted on #26348 puts the cost at about 35 more reloads a day and
about 66 s a day of added waiting at its measured usage, and the reload at 1.53 s
(Linux aarch64, model file in the page cache). That proposal changes larger machines
that do not need it, and a person whose context build needs a vector during a reload
waits for it. This RFC narrows it to small machines and removes the wait.

Two side effects must not come with it:

1. **A person waiting for the reload.** The first turn that needs a vector after an
   unload waits for the load inside its build.
2. **An operator's choice read as the default.** `config.json` is a full
   materialization of the schema: every field is written whether or not anyone set it,
   and the config layer has no per-key provenance (`docs/system-specs/modules/config.md`,
   "Superseded Defaults"). A stored 3600 cannot be told apart from the shipped default,
   so reading it as the default would override an operator who chose an hour on purpose.

## 2. Proposal

### 2.1 Which machines are small

Physical memory of 16 GiB or less (`platform_compat.host_total_mib`: `sysconf` on Linux
and macOS, `GlobalMemoryStatusEx` on Windows), read once per process. 16 GiB exactly,
because a 16 GB machine reports at most 16,384 MiB on every platform (macOS exactly
that, Linux and Windows a little less) and the next common size reports over
30,000 MiB. A machine whose size cannot be read is not small, so it keeps today's
behaviour. A container's own memory limit is not part of this rule.

### 2.2 A new `"auto"` value: no stored number is ever guessed at

`memory.embedding_idle_evict_secs` becomes `int | str` with the default `"auto"`, the
same shape as `agent.session_start_concurrency`:

| Stored value | Small machine | Any other machine |
|---|---|---|
| `"auto"`, missing, or not a number | 600 s | 3600 s |
| any number of seconds >= 0, 3600 included | that number | that number |
| `0` | never unload | never unload |

600 s is the idle release the local speech model already uses (`stt.idle_evict_secs`).

A fresh install writes `"auto"`. An existing install keeps the number it has, 3600
included, so nothing changes under anyone who did not ask for it. The change is
announced through the existing superseded-default registry
(`config/superseded_defaults.py`): one report-only row,
`memory.embedding_idle_evict_secs` `3600 -> "auto"`, whose `applies` predicate is "this
is a small machine" (elsewhere `"auto"` is 3600, so a stored 3600 is not drift).
`kirocrew doctor` and `kirocrew config defaults` show it; `--adopt` takes `"auto"`,
`--keep` keeps the hour. It is report-only, not auto-adopt: an hour is something an
operator sets on purpose, which is the line `config.md` draws for auto-adoption.

### 2.3 The model is loaded again as soon as a person shows up

`keep_warm()` on the embedding backend restarts a loaded model's idle clock, or starts
the background load. It never blocks its caller and never builds an embedder the
process had not built. It runs off the event loop, on a small machine only, at:

| Signal | Where |
|---|---|
| A person opens, focuses or types in a dashboard chat | `dashboard/ws.py` `_handle_slot_focused` |
| A dashboard message is received, with or without a focus frame | the dashboard chat-send handler |
| Any channel message (Slack, Discord, Telegram, Teams and the rest) | `messaging/dispatch_admission.py`: `admit_inbound_callback` and `hold_inbound_callback`, after a granted reservation, for a routed message |

Channel admission is the one seam every channel's dispatch callback passes, after the
transport has authorized the sender, so a stranger's message never reaches it. The raw
Slack envelope admission in `slack/events.py` and Discord's monitor completions pass no
route and do not warm.

### 2.4 Measured: no channel message waits

A real gateway with its real Telegram channel, pointed with `TELEGRAM_API_BASE_URL` at a
loopback stand-in for the Bot API, on Linux x86_64, every turn through the product's
admission, session start, context build and model turn. The operator timeout was 45 s
so the model unloaded between rows. The first build had no early load; the second had
the admission early load of 2.3. Load wait is the time the context build waited for
the model:

| Case | Without the early load | With it |
|---|---|---|
| New conversation, model unloaded | 1,235 to 1,308 ms, median 1,262 (n = 7) | 0 ms (n = 5) |
| New conversation, model loaded | 0 ms | 0 ms (n = 5) |
| Follow-up in a live conversation, model unloaded | 0 ms (n = 6) | 0 ms (n = 5) |
| Follow-up in a live conversation, model loaded | 0 ms (n = 4) | 0 ms (n = 5) |
| Message received to first reply text, new conversation, median | unloaded 8,233 ms, loaded 6,981 ms | unloaded 6,978 ms, loaded 7,425 ms |

Why it holds: the build that embeds is a new conversation's session-start build
(`build_message` sets the embedding deadline only around `build_session_context`), and
it starts about 2 s after admission, behind the ACP session start. The reload took
1.3 s every time. A follow-up's build does not embed, so it never waits for the model.

### 2.5 The model is never unloaded under work

The evictor refuses while an embed is between its entry and its vector (a build, a
recall, a write), while a background sweep pauses between two texts, while a job is
queued, while inference holds the lock, and while a load is running. These refusals
come with [#36612](https://github.com/kirodotdev/KiroCrew/pull/36612); the
implementation of this RFC builds on them and changes none of them.

## 3. Guarantees

1. A machine that is not small behaves exactly as today, for every stored value, and
   runs no early load.
2. A stored number is always honoured; only `"auto"` (or no value) picks by size.
3. No dashboard turn and no channel message waits for a reload that the early load
   could have started first.
4. The model is never unloaded while an embed, a sweep or a load is in flight or queued.
5. Vectors are byte-identical: only when the model is resident changes.

## 4. Limits

1. The headroom is the gap between admission and the session-start build minus the
   reload: about 0.7 to 1.7 s on the measured host. A machine whose page cache has
   dropped the model file reloads more slowly (#26319 measured 14.7 s cold against
   2.2 s warm), and a new conversation then waits for the rest. That happens today
   after an hour idle; a 10-minute timer makes it more likely only if the page cache
   drops the file within those minutes.
2. macOS and Windows rows are owed before the implementation merges; they run the same
   measurement on real 16 GiB-or-smaller hardware, where `small_memory_host()` must
   answer true unpatched.
3. The early load also fires on a follow-up, which does not need it, so a busy channel
   keeps the model loaded. That costs some of the memory saving, never time.
4. Unattended turns (cron, heartbeat, conductor workers) get no early signal. Their
   first embed after an unload waits for the reload, with no person waiting on it.

## 5. Rollback

- Per install: `kirocrew config set memory.embedding_idle_evict_secs 3600`.
- Per release: revert the implementation. An older build reads `"auto"` through
  `_safe_int` as its default 3600, and its next save writes 3600: the downgrade lands on
  today's behaviour, and a later upgrade keeps that 3600 as an operator value, with the
  report row only.

## 6. Implementation

One pull request after this document is accepted and #36612 has merged:
`small_memory_host()` and the `"auto"` resolution in `embeddings_settings.py`; the
`int | str` field and its builder in `config/`; the report-only superseded-default
row; `keep_warm()` on the backends and its two helpers in `embeddings_shared.py`; the
three call sites of 2.3; `config-baseline.json`; tests for the resolution table on both
machine sizes, the unchanged answers on a larger machine, the admission warm-up for a
routed message only, and the superseded-default row; the owning specs
(`memory-skills-hooks.md`, `config.md`, `messaging.md`) and the user docs
(`memory-and-learning.md`, `configuration.md`) in the same commit. This document moves
to `implemented` in that pull request.

## 7. Not in this RFC

- A container's cgroup memory limit as "small".
- A faster reload (turning weight repacking off saves memory but slows every embed,
  per the data on #26348).
