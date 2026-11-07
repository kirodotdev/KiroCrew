---
title: Rewrite a kept main-agent spec in place as soon as it reads again
status: draft
author: kirocrew agent session, directed by jeeshofone
created: 2026-11-02
last-audited: 2026-11-05
audited-at: 664ae5bdae
doc-pr: 28664
implementation-prs: []
tracking-issues: [18860]
supersedes: []
superseded-by: []
---

# RFC: Rewrite a kept main-agent spec in place as soon as it reads again

Status: draft. Acceptance requested from a maintainer of agent spec projection and fork governance. Part of [#18860](https://github.com/kirodotdev/KiroCrew/issues/18860). Main-side references were read at `664ae5bdae`. References to the unreadable-spec mechanism were read on the current revision of [#27535](https://github.com/kirodotdev/KiroCrew/pull/27535), which is open and not on main. No code in this document can land before #27535 does.

Revised 2026-11-05 to the scope the maintainer signed on this PR after the decision on #27535: this document no longer admits or refuses any start.

## Summary

[#27535](https://github.com/kirodotdev/KiroCrew/pull/27535) (moved from #15546) stops a rebuild from overwriting an unreadable `kirocrew.json` with defaults. The rebuild keeps the file (`_skip_unreadable_main_spec` in `agent_parts/spec_unreadable.py`) and opens an episode. While the file stays unreadable, the heartbeat and worker specs mirrored from it are not re-derived. Their existing `allowedTools` and `autoApprove` are re-filtered in place through the current governance ceiling (`_refilter_kept_mirrors` in `agent_parts/spec_siblings.py`). #27535 refuses no start. A start while the file is unreadable behaves as it does on main today.

The episode closes when a later rebuild writes the spec again (`_note_main_spec_readable`). With #27535 alone, that rebuild comes from the hourly `retry_held_conductor_specs` (`agent_parts/spec_rebuild.py`), a config change the watcher applies, or a restart. Until then the kept spec is not rewritten, even after the file reads again.

This document proposes rewriting the kept spec in place as soon as it reads. A main-agent start that finds an open episode and a readable file joins one shared rebuild before it loads the spec. Overlapping callers share that rebuild, no thread is created, a rebuild never nests inside another, and a backoff stops repeated rebuilds against a file that still fails. The rebuild reads the governance generation before it reads either layer. The start goes ahead in every case, exactly as on main. Recovery only decides when the kept spec gets rewritten.

## Motivation

### The gap

After a user restores read access to `kirocrew.json`, the kept spec stays as it was until the next rebuild writes it, which can be up to an hour. During that time the main agent loads a spec projected under an older ceiling and profile set. The heartbeat and worker mirrors are re-filtered, but the main spec itself is not re-derived, and holds that `_spec_hold_open` drives (the boot baseline, the pending ceiling memo, the default-model applier's "not landed" result) stay open.

A main-agent start is the one caller that both needs the spec now and can tell cheaply that the file reads again. Rewriting the spec on that path closes the gap with no timer.

### Why it was cut from #15546

The #15546 author set a rule that a fourth review round in one design family moves that layer into its own PR. The recovery code took four rounds of findings from the GPT review lane. Two of them still shape this design:

1. Comparing only the ceiling counter missed a profile-only change. The rebuild compares `governance_answer_generation()` (ceiling plus profile layers, `platform/governance_profiles.py`), with `poll_profiles_fresh()` called first.
2. The generation was read after the rebuild had read the layers, so a layer that moved mid-rebuild was recorded as the newer value over grants built from the older one. The rebuild reads it before it reads either layer.

The other two rounds concerned admitting a start, which this document no longer does.

## Goals

- The kept spec is rewritten as soon as a main-agent start finds it readable, with no wait for the hourly retry.
- Overlapping callers share one rebuild. No caller creates a thread.
- The generation a rewrite records is the one read before the rebuild read either layer.
- No start is refused, delayed past a bounded wait, or admitted differently from main.

## Non-goals

- Refusing or admitting any start based on the episode. #27535 refuses none, and this document adds none.
- Admitting or refusing a start while the file still cannot be read. Such a start behaves as main does today.
- An early-retry timer or any other scheduled rebuild. #27535 no longer has one, and this document adds none.
- Fork gating at `set_mode` and binding every start in any process to the verdict ([#18646](https://github.com/kirodotdev/KiroCrew/issues/18646)).
- A process launched on a view from before an episode ([#14112](https://github.com/kirodotdev/KiroCrew/issues/14112)).
- MCP handlers reporting success after a skipped rebuild ([#18679](https://github.com/kirodotdev/KiroCrew/issues/18679)).

## 1. Background: what #27535 provides

These are the pieces this design builds on, on the current revision of #27535:

- `_AgentSpecUnreadable` is raised when a rebuild read of a present `kirocrew.json` fails with an `OSError` other than `FileNotFoundError`. `_skip_unreadable_main_spec` keeps the file, re-filters the kept heartbeat and worker mirrors through the current ceiling, runs the sibling steps, reports the rebuild as unwritten and held, and opens an episode (`_main_spec_unreadable`, `_main_spec_episode_revision`).
- `_note_main_spec_readable(path, episode_revision)` closes the episode when a rebuild writes the spec, unless a newer skip raced the write.
- `_spec_hold_open()` keeps the rebuild held while the episode is open, and the hourly maintenance wake retries it.
- Nothing refuses a start, and there is no early-retry timer.

## 2. Options

### A. Do nothing

Keep #27535 as it is. The kept spec is rewritten at the next hourly retry, watched config change or restart. Safe, but a main agent started after access is restored loads the kept spec for up to an hour.

### B. Rewrite on the start path (recommended)

Section 3. A main-agent start that sees an open episode and a readable file runs or joins one shared rebuild, then goes ahead as on main.

### C. A scheduled retry

Rebuild on a short schedule while an episode is open. Rejected: it runs rebuilds with nobody waiting, needs its own thread and gateway start and shutdown hooks, and is the stopgap #27535 removed.

## 3. Recommended design

### 3.1 Invariant

**No start is refused, and no start is admitted differently from main, by this change.** Recovery runs a rebuild and lets it write the spec. Whatever the rebuild does, the start then goes ahead on the same path it takes on main today. The one added delay is the wait for that rebuild: only a start that finds an open episode and a readable file waits, and it waits for one rebuild's duration, at most the waiter's 30-second timeout (§3.3, open question 1). A start with no open episode waits for nothing. A start while the file is unreadable behaves as main does today.

### 3.2 Generation read before the layers

`rebuild_agent_config` (`agent_parts/spec_rebuild.py`) calls `poll_profiles_fresh()` and reads `governance_answer_generation()` once, before it reads the ceiling or a profile, and records that value as the generation the write projected. If the generation polled after the write differs, the shared rebuild runs once more, so the rewritten spec reflects a layer that moved mid-rebuild. A layer that moves after that is the same race a host with a readable spec already has, and the next rebuild projects it. Nothing waits on or admits on the comparison.

### 3.3 Shared rebuild

`_MainSpecRebuild` holds one in-flight rebuild, in `_main_spec_rebuild` under a lock. It is one per process, matching the episode. `_shared_main_spec_rebuild()` starts the rebuild on the calling thread if none is in flight, or waits on the in-flight one's event with a bounded timeout. No thread is created: the first caller runs it and the others wait. A waiter that times out stops waiting and its start goes ahead as on main. Each `_MainSpecRebuild` carries its own result, and the entry is cleared when its rebuild ends, so a later caller never reads an earlier rebuild's result.

`rebuild_agent_config` sets a per-thread "in rebuild" marker for its whole run, whoever called it (the shared slot, the hourly retry, the watcher or boot). Recovery on a thread that carries the marker returns at once, so no rebuild runs inside another or waits on itself.

After a shared rebuild that did not write the spec, the next recovery cannot start a rebuild for 5 seconds, so repeated starts against a file that still fails do not run rebuilds back to back. Those starts go ahead as on main. The hourly retry and the config watcher keep calling `rebuild_agent_config` directly and are not subject to the backoff.

### 3.4 Where recovery runs

Recovery is a new function, `recover_main_spec_episode() -> None`, in `agent_parts/spec_unreadable.py`. It is called on the main-agent start path, off the event loop, before the start loads the spec. It runs these steps and returns in every case:

1. Return if no episode is open.
2. Return if called on the event loop (`asyncio.get_running_loop()` succeeds on this thread) or on a thread that carries the "in rebuild" marker.
3. Probe the file with one `read_bytes_with_retry`. If it still fails, return. The probe only decides whether a rebuild is worth running.
4. Join `_shared_main_spec_rebuild()`, subject to the backoff in section 3.3.

The start continues after step 4 whether or not the episode closed. Recovery raises nothing a start would see. An error inside the rebuild is logged and the start goes ahead as on main.

### 3.5 Cost: holds with no start waiting

Holds that `_spec_hold_open` drives and that no start triggers (the boot baseline, the pending ceiling memo, the default-model applier's "not landed" result, held conductor and fork specs) still clear only at the next start, watched config change, hourly retry or restart. This is the same as #27535 alone. Open question 2 asks whether a maintainer wants a hold-side trigger.

## 4. Building on #27535

The implementation PR is based on main after #27535 merges and touches `agent_parts/spec_unreadable.py`, `agent_parts/spec_rebuild.py`, the main-agent start path and their tests. If #27535 changes the episode's shape in its remaining review, this document is revised against the merged head before code starts. The implementation does not stack on an unmerged #27535.

## 5. Test plan

In `test/test_agent_install.py` (`TestInstallAgent`) and the #27535 spec-unreadable tests:

- **Readable again:** a main-agent start after access is restored runs one rebuild, the spec is rewritten, and the episode closes.
- **Still unreadable:** a start runs no rebuild and goes ahead exactly as on main, with no SEL row added.
- **Start behaviour unchanged:** parameterised over readable, still unreadable, rebuild error and waiter timeout; the start takes the same path and returns the same result as on main.
- **Generation read order:** `poll_profiles_fresh` is called before `governance_answer_generation`, and the generation is read before the rebuild reads either layer.
- **Layer moved during the rebuild:** parameterised `[unchanged|ceiling|profile]`; a moved generation causes one more shared rebuild and no more.
- **Raced skip:** a write whose episode revision moved does not close the episode.
- **Event loop:** recovery called on the event loop runs no rebuild.
- **Nested rebuild:** recovery inside a rebuild started by the hourly retry or the watcher does not start a second rebuild.
- **Lock:** `poll_profiles_fresh` is not called while `_main_spec_episode_lock` is held.
- **Overlapping starts:** 8 concurrent starts share 1 rebuild, and the thread count does not grow.
- **Waiter timeout:** a waiter that times out stops waiting and its start goes ahead.
- **Backoff:** a second start within 5 seconds of a rebuild that did not write runs no rebuild and goes ahead.

The repo's contract-test family and both AI review lanes run before the implementation is pushed.

## Open questions

1. Is the waiter's bounded timeout better set from the rebuild's measured duration or as a constant? This document proposes a constant (30 seconds), after which the start goes ahead as on main.
2. Should a hold-side trigger rewrite the kept spec when no start arrives (section 3.5), or is the hourly retry enough?
