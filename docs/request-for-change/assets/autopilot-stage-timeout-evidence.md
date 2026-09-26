# Autopilot 30-minute stage-timeout evidence

This appendix supports
[`rfc-autopilot-stage-budgets.md`](../rfc-autopilot-stage-budgets.md). It contains
sanitized lifecycle evidence from the incident owner's local Kiro Crew records.
Raw transcripts are deliberately not committed: they contain unrelated prompts,
local paths, tool arguments, and project content that are unnecessary to verify
the defect.

## Scope and method

- Evidence collected: 2026-09-26.
- Code baseline: Kiro Crew `main` at `7f1f4fb1d`.
- Dashboard corpus: the incident owner's `dashboard_*.jsonl` session records.
- Search signature: exact assistant text matching
  `Stage [0-9]+ timed out after 30m`.
- Deduplication: stable transcript message id plus timestamp. Session copies can
  contain the same original row and do not count as new incidents.
- Native correlation: only the two incidents whose backing ACP records were
  recovered are used to claim post-dashboard continuation.
- Redaction: project names, user prompts, absolute paths, tool arguments, model
  reasoning, credentials, URLs, and unrelated transcript rows are omitted.

## Incident inventory

The bounded scan found six distinct timeout events:

| UTC timestamp | Stage | Dashboard line |
|---|---:|---|
| 2026-09-17 22:18:46 | 2 | `⏱️ Stage 2 timed out after 30m. Auto-run stopped.` |
| 2026-09-18 13:46:10 | 3 | `⏱️ Stage 3 timed out after 30m. Auto-run stopped.` |
| 2026-09-22 11:35:00 | 1 | `⏱️ Stage 1 timed out after 30m. Auto-run stopped.` |
| 2026-09-24 23:40:20 | 2 | `⏱️ Stage 2 timed out after 30m. Auto-run stopped.` |
| 2026-09-25 20:44:15 | 1 | `⏱️ Stage 1 timed out after 30m. Auto-run stopped.` |
| 2026-09-25 22:20:19 | 1 | `⏱️ Stage 1 timed out after 30m. Auto-run stopped.` |

This table proves recurrence only. The detailed consequences below are claimed
only where dashboard and native records were both available.

## Incident A: dashboard timeout, native build success

### Timeline

| UTC timestamp | Surface | Sanitized event |
|---|---|---|
| 2026-09-24 23:36:59 | Dashboard | A polling wait began for a detached build whose log lived in session scratch. |
| 2026-09-24 23:40:20 | Dashboard | `Stage 2 timed out after 30m. Auto-run stopped.` |
| after 23:40:20 | Native ACP | The same turn continued and read the build result. |
| after 23:40:20 | Native ACP | Python 3.10: `3424 passed, 8 skipped, 1 deselected`. |
| after 23:40:20 | Native ACP | Python 3.12: `3424 passed, 8 skipped, 1 deselected`. |
| after 23:40:20 | Native ACP | `BUILD SUCCEEDED`. |

### Consequence

The dashboard declared the stage stopped while the native turn remained live. The
late success existed only in the native ACP record after dashboard ownership had
ended. The detached build's session-scratch log was later reclaimed, so a copied
session could establish that no build remained but could not recover the final
result from the dashboard record.

### Exact native excerpt

```text
[CPython310-release] ===== 3424 passed, 8 skipped, 1 deselected, 1 warning in 123.88s (0:02:03) =====
[CPython312-release] ===== 3424 passed, 8 skipped, 1 deselected, 1 warning in 109.79s (0:01:49) =====
BUILD SUCCEEDED
```

## Incident B: dashboard timeout, native source writes continue

### Timeline

| UTC timestamp | Surface | Sanitized event |
|---|---|---|
| 2026-09-25 20:44:15 | Dashboard | `Stage 1 timed out after 30m. Auto-run stopped.` |
| through at least 20:44:45 | Native ACP | The same native turn continued after the dashboard timeout. |
| after 20:44:15 | Native ACP | Three additional writes to one source file were initiated. |
| after 20:44:15 | Dashboard | Those writes did not appear in the dashboard transcript. |

### Consequence

The user had to inspect the worktree for changes made after the UI said work had
stopped. The session's preserved stage boundary also held later ordinary messages,
so the visible conversation looked unresponsive while the unobserved native turn
could still mutate files.

No source content or file path is reproduced here. The evidence needed for the
lifecycle defect is the ordering: dashboard terminal claim first, native write
activity afterward.

## UI evidence

Two screenshots supplied by the incident owner showed the same state in separate
sessions:

1. The old `Auto-run stopped` card had no Go/Cancel recovery controls.
2. A later ordinary user message was visibly queued behind the preserved stage
   boundary.
3. The stage had been doing productive edit/test work, not sitting idle.

The original screenshots are not committed because this appendix can reproduce
the relevant UI facts from the transcript, and the frames include unrelated
session content.

## Source provenance

Hashes let the incident owner re-identify the exact local records without
publishing their paths or contents:

| Source | Bytes | SHA-256 |
|---|---:|---|
| Incident A dashboard record | 13,263,398 | `a804caf5067b2e799559d7b47d2fb8632cc11d928db97a5fb64e36c11cab35db` |
| Incident A native ACP record | 1,078,327 | `888d004b7c4cb00ff04a552ab9fcebe54f0e0187ce1d281f273f2a2fbdf1edda` |
| Incident B dashboard record | 74,755,105 | `d4b8447d5d7a2aac3bd5f3a64f70bc9e418aa5fc7e6fae7940bb43c8bdf86bfc` |
| Incident B native ACP record | 982,978 | `cc0f71f64d736cc364f287289a6d2e0ac20759517bb55bb51e605561691b0252` |

These hashes are provenance, not a promise that the raw files are safe to share.
They remain local.

## Code-path correlation on `main` at `7f1f4fb1d`

1. `OrchestrationTracker.start_stage` in
   `src/kiro_crew/context_management.py` starts an absolute stage clock.
2. `_stage_loop` in `src/kiro_crew/dashboard/chat_orchestrator.py` checks the
   clock at a stage boundary.
3. The same loop wraps the active `_run_chat` turn through `_bounded_turn` using
   the configured 30-minute value.
4. `_bounded_turn` in `src/kiro_crew/dashboard/turn_dispatch.py` cancels the
   inner task when its timer fires and records the fact even if the inner
   coroutine absorbs cancellation.
5. `_run_chat`'s `CancelledError` branch in
   `src/kiro_crew/dashboard/chat_runner.py` persists partial dashboard text, but
   does not call native `session/cancel`.
6. The native ACP turn therefore retains execution authority after the dashboard
   controller has emitted a terminal-looking card.

The recovered traces match this path exactly: the dashboard timer fired, the
local observer ended, and the provider-side turn continued.

## Reproduction

A deterministic regression does not need a 30-minute test:

1. Configure a short `stage_timeout_seconds` in a dashboard test.
2. Start Go All with at least two stages.
3. Make Stage 1's `_run_chat` remain productively active past that value.
4. Observe current behavior: `_bounded_turn` cancels Stage 1 and the dashboard
   emits the old timeout card.
5. Use a cancellation-aware fake provider to distinguish local coroutine
   cancellation from native `session/cancel`.
6. Assert the current bug: no native cancellation was sent, or a pre-lease
   key-only cancel targets another turn.
7. For the proposed contract, assert instead that Stage 1 completes, its result is
   captured, Stage 2 is not automatically admitted, and an explicit Go can start
   Stage 2.

Additional interleavings required by the RFC:

- cancellation before lease acquisition;
- native cancel requested before ACP stream-active state is cleared;
- a second cancellation while native cancel is in flight;
- native cancellation acknowledgement timeout;
- `no_turn` returned after local unwind while native work is unfinished;
- an ordinary plan-shaped message arriving while the interrupted stage boundary
  remains armed;
- final stage crosses the budget but has no next stage.

## Prototype review evidence

The first implementation prototype moved native cancellation into `_run_chat`'s
`CancelledError` handler. Current-head GPT 5.6 and Opus 5 reviews independently
identified the same ordering defect:

```text
chat deadline fires
→ dashboard task cancellation unwinds the ACP stream
→ the ACP finalizer clears active-turn state
→ _run_chat's CancelledError handler calls cancel_current
→ provider sees no active turn and returns no_turn
→ native session/cancel is never sent
```

This is stronger than a speculative review concern because it matches the
recovered incidents: the dashboard turn ended while the native tool continued.
The RFC therefore requires native cancellation to begin through an exact leased
turn target before dashboard stream unwind, and refuses to treat `no_turn` as an
acknowledgement when unfinished native work is still owned.

The same review round also confirmed that changing the 30-minute stage value from
a running-turn ceiling to an automatic admission budget is a product-shape
decision. That is why the implementation PR returned to draft and this RFC is a
separate change request.

## Claims this appendix does not make

- It does not claim all six incidents caused file corruption. Two have confirmed
  post-dashboard continuation; four establish recurrence of the false timeout.
- It does not claim every long stage is healthy. The ACP liveness oracle remains
  responsible for distinguishing WORKING, UNKNOWN, dead, and stuck-input states.
- It does not claim session scratch retention caused the timeout. Scratch cleanup
  made the first incident harder to reconstruct after dashboard ownership was
  lost.
- It does not claim a particular model caused the defect. The timer and
  cancellation ownership live in the Kiro Crew host.
