---
title: Idle compaction before prompt-cache expiry, an opt-in session setting
status: draft
author: Pearcekieser, with Kiro
created: 2026-10-19
last-audited: 2026-10-19
audited-at: cea787cbd5
doc-pr: 22149
implementation-prs: []
tracking-issues: [22148]
supersedes: []
superseded-by: []
---
# RFC: Idle compaction before prompt-cache expiry, an opt-in session setting

- Status: draft. Nothing is built.
- Author: Pearcekieser, with Kiro
- Created: 2026-10-19
- Measured at: `cea787cbd5`
- Related: `../system-specs/modules/session.md` (automatic compaction),
  `../system-specs/modules/config.md` (`session.autocompact_pct`),
  `../system-specs/modules/providers.md` (`compact()` per backend)

## 1. Summary

Add one setting, `session.idle_compaction`, off by default. When it is on, a
dashboard chat that has gone quiet is compacted once, a little before its
backend's prompt cache is known to expire. The next turn, whenever it comes,
still starts cold, because a compaction replaces the cached prefix, but it
sends a summary of about 14% context instead of the whole history.

It pays only for large conversations. On kiro the first-request saving covers
the compaction's own cost from about 41% context, so the minimum context
defaults to 50% (section 2.3). Each idle compaction is one extra model call,
it costs credits even if the user never comes back, a user who returns just
after it fires waits for it, and the summary drops detail the agent had in its
context. The setting says all of that.

## 2. Motivation

### 2.1 Current state

Crew compacts a session in one situation: context use crosses a threshold.
`CompactionCoordinator.check_context_usage` in `src/kiro_crew/session_compaction.py`
runs at turn end and calls `_trigger_compaction` once usage reaches
`effective_autocompact_pct`, which resolves the slot override, then the
crew's `agents.<name>.autocompact_pct`, then `session.autocompact_pct`
(default 70). No code path compacts because time has passed. The only timer over
idle sessions is the idle expiry sweep, `_expire_idle`
in `src/kiro_crew/session_cleanup.py`, which ends the process after
`session.timeout_secs`; it does not compact. A search of `src/kiro_crew` at
`cea787cbd5` for `idle_compact`, `cache_ttl` and `prompt_cache` finds no other
candidate, and Crew sets no prompt-cache lifetime on any backend.

### 2.2 The problem

Providers keep a conversation's prefix cached for a short time after each
request. A turn that starts inside that window pays the cache-read rate for the
history; a turn that starts after it pays to write the whole history again.
Users step away from a chat for longer than the window all the time, so the
first turn after a break is the most expensive turn of the conversation.

On kiro-cli the window is 300 s. This was measured from 6,238 recorded turns
in kiro-cli 2.32.0 session files on one host, comparing each turn's first
request credit with the median of its later, warm requests:

| idle gap before the turn | turns | first-request cost / warm cost | share above 2x |
|---|---|---|---|
| 0 to 270 s | 2,554 | about 1.0 | 6 to 12% |
| 290 to 300 s | 85 | | 21% |
| 300 to 310 s | 107 | | 99% |
| 600 to 900 s | 550 | 3.29 | 99% |
| 1,800 to 3,600 s | 626 | 3.23 | 99% |
| over 3,600 s | 78 | 3.19 | 97% |

So on kiro-cli, coming back after five minutes makes the first request of the
turn cost about 3.3 times a warm one, and the cost grows with the size of the
history. Nearly all of those turns ran on one model (claude-opus-5.5); other
models had 15 turns between them, so the 300 s figure is established for that
model only. kiro-cli does not report cache token counts on ACP or in its
session files, which is why the measurement uses credits.

### 2.3 What an idle compaction costs, and when it pays

An idle compaction does not make the next request warm. A compaction replaces
the conversation prefix, so the first request after one is a cache miss: 99% of
251 post-compaction first requests at 20 to 30% context were cold, including 138
that came less than 270 s later. What it buys is a cold request at the
post-compaction context, about 14%, instead of a cold request at the full
context X, plus cheaper warm requests for as long as the conversation stays
smaller.

kiro-cli bills the compaction request but records that bill nowhere Crew can
read, so its cost is modelled from the same 6,238 metered claude-opus-5.5 turns.
The low bound assumes the compaction request reads the warm cache; the high
bound assumes it misses. Durations are measured from 385 automatic compactions
in dashboard transcripts.

| context before | compaction cost, credits (model) | median duration (p75; p90) | first-request saving, cold(X) - cold(14%) |
|---|---|---|---|
| 20 to 30% | 2.5 to 4.4 | 39 s (55 s), n=266 | about 1.0 |
| 30 to 40% | 2.9 to 5.7 | not measured, n=2 | about 2.4 |
| 40 to 60% | 3.3 to 7.0 | not measured, n=1 | about 3.7 |
| 60% and up | 4.2 to 10.3 | 76 s (88 s; 304 s), n=48 | about 6.9 |

The compaction pays when the probability that the user returns after the cache
window, times the saving, exceeds its cost. On the first-request saving alone
and the low cost bound, it never pays below about 41% context even if every
user returns; at 46% the user must return 88% of the time, at 74% 61% of the
time. On the high cost bound it never pays on the first request alone, at any
context. Counting about 10 later warm requests at the smaller context lowers the
needed return rate to 22 to 48% at 46% context and 15 to 37% at 74%.

Two cases lose credits outright: the user never returns, and the user returns
between the fire time and the end of the window, where a warm turn at X becomes
a cold turn at 14% plus the compaction. Of 4,191 returns after 255 s or more in
this data, 1,236 (29%) landed between 255 s and 300 s.

So the setting's minimum context defaults to 50%, above the 41% first-request
break-even, and the help text says the setting saves credits only on large
conversations that the user comes back to. The compaction cost is a model; a
single measured `/compact` with its metering notification logged would replace
it, and it is the Phase 1 entry criterion (section 6, and section 10,
question 5).

## 3. Goals

1. One opt-in setting. Off, behaviour is byte-for-byte what it is today.
2. On, compact an eligible idle dashboard chat once, before its backend's known
   cache lifetime runs out.
3. Use a lifetime per backend, from evidence. A backend whose lifetime Crew
   cannot know is skipped, not guessed.
4. Never compete with the user or the agent: no idle compaction while a turn,
   tool, approval, question card, subagent, pending injection or armed monitor
   loop is live. A message that arrives during one waits for a single
   compaction round-trip at most, and that wait is stated (section 5.5).
5. At most one idle compaction per idle stretch, deduplicated with the
   threshold compaction.
6. The setting's help text states the cost and the loss of detail.

## 4. Non-goals

- Changing which cache lifetime a backend requests, for example asking a
  harness for a longer cache tier. That changes the user's bill and is its own
  decision.
- Compacting channel threads, cron sessions, subagents, task-runner steps or the
  background runtimes in the first version.
- Promising a saving. Whether an idle compaction pays off depends on whether and
  when the user returns, which Crew cannot know.
- Persisting idle timers across a gateway restart (see 5.6).

## 5. Design

### 5.1 The setting

```python
idle_compaction: bool = field(
    default=False,
    metadata=_meta(
        "Compact Idle Chats Before Cache Expiry",
        "When a dashboard chat has been idle for slightly less than its "
        "backend's prompt-cache lifetime, compact it once so your next turn "
        "re-sends a summary instead of the full history. It saves credits only "
        "on large chats you come back to: each idle compaction is an extra "
        "model call that costs credits even if you never return, if you return "
        "just after it starts you wait up to a minute or more for it to finish, "
        "and the summary loses detail from earlier in the conversation. Only "
        "chats whose context is at least Idle Compaction Minimum Context full "
        "are compacted. Backends with an unknown cache lifetime are skipped.",
    ),
)
idle_compaction_min_context_pct: float = field(
    default=50.0,
    metadata=_meta(
        "Idle Compaction Minimum Context",
        "Context usage percentage below which an idle chat is left alone. "
        "Below about 40% a compaction costs more than re-sending the history "
        "it replaces, so values below 40 are raised to 40 (40-90).",
    ),
)
```

The minimum is clamped at load to [40, 90]. The floor sits at kiro's
first-request break-even (section 2.3), so no value a user can set compacts a
chat only to lose credits on the first request.

It lives in the `session` section next to `autocompact_pct`, and on the Chat
settings page beside the Auto-Compact Threshold control. A per-chat override,
following the `slot.autocompact_pct` pattern
in `src/kiro_crew/dashboard/chat_api/slot_settings.py`, is left for later.

### 5.2 Lifetime per backend

| backend | lifetime used | idle compaction fires at | evidence |
|---|---|---|---|
| kiro (`""`) | 300 s | 255 s | measured, section 2.2 |
| codex | 30 min | 27 min | OpenAI documents a 30 minute minimum after the last write or reuse for current models; non-ZDR organizations default to extended retention, typically about 30 minutes. Codex sends a stable `prompt_cache_key` and no retention override. |
| claude | only when `CLAUDE_CODE_PROMPT_CACHE_TTL` is set in the backend's environment: `5m` gives 255 s, `1h` gives 54 min | as stated | Claude Code requests a one hour cache on a subscription within plan usage and five minutes otherwise; Crew cannot see which applies, so without an explicit value the backend is skipped. |
| kas | skipped | | KAS manages its own compaction (`ACP_BACKENDS_HARNESS_MANAGED_COMPACTION`) and its cache lifetime is undocumented. |
| opencode, goose, pi | skipped | | The lifetime depends on the provider the user configured. |
| deepseek | skipped | | Not compactable, only recyclable (`ACP_BACKENDS_CONTEXT_RECYCLE`), which drops history. |

The table is one constant mapping a backend id to a lifetime resolver, next to
the capability sets in `src/kiro_crew/agent_sdk/backends_turns.py`. A backend
absent from it is skipped. The margin, 45 s on lifetimes up to 600 s and 10% on longer
ones, covers the time from the trigger to the request reaching the provider;
Anthropic documents that the lifetime is counted from the start of the last
request, so the margin also absorbs the final response's own duration, which is
why the timer starts when the last turn ends rather than when it began. The
45 s kiro margin is provisional: the Phase 1 entry criterion (section 6)
re-derives it from the measured return-time distribution and compaction cost.

### 5.3 The trigger

A per-session timer, not the idle sweep: `_expire_idle`'s tick is
`min(max(timeout_secs // 6, 60), 300)` (`_adopt_idle_policy` in `session_cleanup.py`), five minutes
by default, which is coarser than the kiro window.

- **Arm** at turn end, in the same place `check_context_usage` runs, when the
  setting is on, the key is a `dashboard:` session, the backend has a lifetime,
  and context use is at or above `idle_compaction_min_context_pct`.
- **Cancel** when the session is next claimed for a turn, when it is reset,
  recycled or removed, and when the setting is turned off.
- **Fire** by calling a new `compact_if_idle(key)`.

The timer, its generation check, the live-signal probes and `compact_if_idle`
live in a new module, `session_idle_compaction.py`. They are a separate
responsibility from the threshold path, and `session_compaction.py` is already
close to the repository's file-size split threshold. The new module calls the
coordinator's existing `_compact_session` rather than duplicating it, and calls
`_gate_rung` with a new `skip_threshold=True` keyword (section 5.4).

The timer reads a monotonic clock through an injectable clock and scheduler so
tests drive it without real time.

### 5.4 What `compact_if_idle` checks, in order, with no await between check and commit

1. The setting is still on and the timer's generation matches the session's
   current one, so a timer that outlived a newer turn does nothing.
2. The existing gate rungs other than the threshold: `cc_managed`,
   `compact_unsupported`, `unconfirmed`, `in_progress`, `cooldown`, plus a
   pending verdict. `_gate_rung` in `session_compaction.py` checks
   `below_threshold` second, before the others, so as written it would refuse
   every idle chat under `autocompact_pct`, which is the whole range this
   feature targets. Phase 1 adds a `skip_threshold` keyword to `_gate_rung`
   that drops only that rung; the threshold path keeps calling it unchanged.
   The idle minimum context replaces the threshold rung for this path.
3. The backend compacts in place. Unmanaged backends are refused here so the
   idle path can never reach `_recycle_unmanaged`.
4. Nothing is live: the session semaphore is free, the slot has no running or
   reserved turn and an empty queue, no question card or approval is pending
   for it, no subagent is attached or queued (the existing fail-closed probe),
   no completion injection is pending, the session is not recycling, and no
   auto-nudge or monitor loop is armed on the slot.

Any refusal ends the idle stretch without retrying; the next real turn re-arms.
On success the coordinator commits `_set_compacting(key, True)` and awaits the
same `_compact_session` the threshold path uses, with a `trigger="idle"`
argument.

### 5.5 Races and failure

- A message that arrives while the idle compaction runs queues behind the
  session semaphore, exactly as it does behind a threshold compaction today, and
  runs when the compaction ends. The user's Stop cancels the compaction through
  the existing `_settle_cancelled` path.
- The compaction holds the permit for one `/compact` round-trip. Because
  step 4 refuses a held permit, an idle compaction never waits for a turn.
- A user who returns while an idle compaction runs waits for it. Measured
  automatic compactions take a median of 39 s at 20 to 30% context and 76 s at
  60% and above, with a p90 of 304 s in that bucket (section 2.3). On kiro this
  window opens at 255 s, and 29% of returns after 255 s in the measured data fell
  before 300 s, where the cache was still warm. That turn costs more than it
  would have without the setting. This RFC accepts that cost for an opt-in
  setting and states it in the help text, rather than cancelling the
  compaction when a message arrives: Crew cannot tell whether a harness has
  already replaced the prefix when its `/compact` is cancelled, so cancelling
  could leave the turn both cold and uncompacted. The user's Stop still cancels
  it explicitly. Section 10 keeps cancel-on-claim open.
- `state.compacting` is the single dedupe set, so a threshold and an idle
  compaction can never run together.
- An idle compaction's own `/compact` does not re-arm the timer, so an idle
  session is compacted once, not every few minutes.
- A failure arms the existing cooldown and stops. The idle path never takes the
  restart-on-failure arm (`_restart_held`), because restarting a session the
  user is not using to fix a compaction they did not ask for would lose history
  for nothing.
- History is not deleted. The transcript Crew keeps is unchanged; the harness
  holds the summary as it does for any compaction.

### 5.6 Restart

No idle timer state is persisted, and restoring a session arms nothing: a
restored session has no confirmed context reading until a turn ends. Its first
turn end after the restart arms the timer from that moment. The provider cache
is very likely cold after a restart anyway, so a session that is restored and
never used again is simply not compacted.

### 5.7 Visibility

- `kirocrew.context.compactions` gains a `trigger` label, `threshold` or `idle`,
  threaded through `_fire_compact_callback` in `session_compaction.py`.
- The dashboard compaction notice says the chat was compacted while idle, so a
  user who returns knows why the context meter dropped.

## 6. Migration plan

### Phase 1: backend and config key

The setting, the lifetime table, the timer, `compact_if_idle`, the metric label,
the `config-baseline.json` and `configuration.md` rows, and the session and
config spec updates. The key is readable from `config.json`; it has no UI yet.

Entry criterion: before the Phase 1 PR leaves draft, one kiro `/compact` is run
at a known context of at least 50% with its `meteringUsage` notification logged,
and the result is recorded here. It settles which of the two cost bounds in
section 2.3 holds. If the low bound holds, the 50% default stands or is moved to
the measured break-even. If the high bound holds, the first-request saving never
covers the compaction, and Phase 1 does not ship on kiro: the kiro row of the
lifetime table is removed so the backend is skipped, and the RFC is revised.

The same entry step re-checks the 45 s margin on kiro. A later fire time
shrinks the window where a returning user finds a warm cache and pays for a
compaction anyway (29% of returns after 255 s landed before 300 s), but leaves
less room before the cache expires (21% of turns resumed at 290 to 300 s
already missed). With the measured compaction cost, the expected credit
result is computed from the section 2.2 and 2.3 data for fire times from 255 s
to 285 s in 10 s steps, and the fire time with the best result is the one
Phase 1 ships, recorded here with its numbers.

Exit criteria, all as tests on a fake clock:
- Off: no timer is ever armed and no extra compaction runs.
- On, kiro, context above the minimum: exactly one compaction at 255 s idle.
- Below the minimum context, on a skipped backend, or on a non-`dashboard:` key:
  nothing is armed.
- A turn starting at 254 s cancels the timer; a turn starting after the
  compaction began waits on the permit and then runs.
- Each live signal in 5.4 step 4 blocks the fire.
- Threshold and idle triggers never compact the same session twice.
- A failed idle compaction arms the cooldown and does not restart the session.
- After a simulated restart, nothing is armed until the first turn ends, and
  the timer then arms from that turn end.

### Phase 2: Settings UI

The Chat settings page control, i18n strings, regenerated settings registry and
UI index, and the compaction notice wording. Exit criteria: the control
round-trips the value, the settings search finds it, and the i18n checks pass.

Phases may land together if the combined diff stays reviewable.

## 7. Backward compatibility

The default is off and both keys are new, so no existing config changes
meaning. An older Crew that reads a config setting them keeps them as unknown
keys inside the `session` section and does not act on them
(the unknown-key capture in `config/loader_config.py`).

## 8. Security considerations

No new input crosses a trust boundary: the setting is local config, and the
compaction is the same `/compact` request the threshold path already sends on
the user's own session and credentials. The cost exposure is bounded by one
compaction per idle stretch per eligible session.

## 9. Alternatives considered

- **Run it from the idle sweep.** Rejected: the sweep's default five-minute
  tick is longer than the kiro window, and shortening it for every session to
  serve an opt-in feature costs every user.
- **One universal timeout.** Rejected: lifetimes range from 300 s on kiro to
  30 minutes on codex and depend on billing on claude; one number would compact
  too late on one backend and needlessly on another.
- **Keep the cache warm with a cheap ping instead of compacting.** Not taken
  in this RFC, but not ruled out. An unbounded ping costs a request every few
  minutes for as long as the chat is open and never shrinks the history. A
  capped one, one to three pings at about warm cost each, keeps detail and may
  cost less than a 2.5 to 10.3 credit compaction for users who return within
  15 minutes. It does nothing for a user who returns later, and the RFC's data
  has no breakdown of return times past 300 s to size that. Section 10,
  question 6 keeps it open as a comparison to make before Phase 1.
- **Ask the harness for a longer cache tier.** Out of scope (section 4); it
  changes billing.

## 10. Open questions

1. The 40% floor and 50% default come from kiro's measured cache and modelled
   compaction cost. Should a backend with a longer cache, such as codex, get
   its own floor once its compaction cost is measured?
2. Should channel threads be eligible behind the same setting in Phase 1?
3. Should an armed monitor loop block idle compaction outright, or only when it
   is due to fire before the cache lifetime ends? The design blocks outright,
   which is the cheaper and safer default.
4. Should a message that arrives while an idle compaction runs cancel it,
   once each compactable harness is shown to leave the conversation intact
   when its `/compact` is cancelled?
5. What does one kiro `/compact` actually cost? The Phase 1 entry criterion
   answers it by running one at a known context of at least 50% with its
   `meteringUsage` notification logged. The result settles the low or high cost
   bound in section 2.3 and, with it, the 50% default.
6. Would a capped keep-warm ping, one to three requests at warm cost, beat an
   idle compaction for some return-time range? Answering it needs the
   distribution of return times past 300 s, which the measured data in
   section 2.3 does not break down.
