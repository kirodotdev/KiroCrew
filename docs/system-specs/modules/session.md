# Session Manager Module

## Overview

Maps thread keys to LLMProvider instances (`session.py`). Each thread gets
its own kiro-cli session with idle expiry, context compaction, circuit
breaker, per-session semaphore, and persistent background session.

Chat sessions are served from the warm pool when eligible (default pool
agent, default cwd, no resume mapping); otherwise they cold-start on first
message via `get_or_create()`.

Successful native ACP resume suppresses replay from both disk and the
dashboard's live slot window. The runner honors the actual provider client's
resumed state as well as the SessionManager result. A real cold start uses the
canonical merged replay; an explicit reset suppresses that replay instead of
silently reloading a fallback. The current request is delivered once and is not
replayed as historical input. This includes cron, recovery and user-replay
injections; queue drain supplies the exact appended row to the runner.

## Dashboard app launch intents

The App SDK's `slotKey` selects an existing dashboard slot through ordinary
activation on both cold entry and navigation within an already-mounted chat.
The session controller claims the target intent before URL synchronization and
releases its message only after a fulfilled `switchSlot`. Slow activation does
not expire a claimed message; failure shows the existing session-open error
with the unsent message available to copy and never sends to a fallback. A routed
chat waits for that exact target before using the message; an embedded chat never
consumes the dashboard intent.
`window.__mc_chat_launch` is claimed once: the session controller owns explicit
targets and fresh drafts; `ChatPage` owns untargeted automatic sends. Unclaimed
intents expire after ten seconds. A newer intent supersedes a pending target
by ref identity, so its later completion cannot release the older message.
`autoSend: false` seeds
an unsent draft, appending to existing text when the target already has a draft.
Without a target, the existing new-session controller creates
one session and stages the draft before navigation, retaining it on creation
failure for retry. Agent selection applies to new sessions only. These options
do not attach app task metadata or change backend session authorization.
App auto-send uses only the supplied text, leaving staged files, pasted content,
knowledge and session references untouched. Existing composer sends and legacy
URL auto-send retain their prior behavior. Refused app turns and failed app
session creation keep their text in the page-level copyable error notice, not
an unrelated draft. Recovery adds no user row to a busy slot. That notice survives slot changes
but is not persisted across page unmounts. Creation failure does not re-arm a
new-session intent for the next manual send.

## Implementation Boundaries

`SessionManager` remains the compatibility facade in `session.py`; callers keep
using its existing public, import, and monkeypatch seams. Mutable policy and
state are composed behind that facade:

- `session_allocation.py` — live registry, key folding, semaphore leases,
  cold-start allocation, claims, and companion runtimes
- `session_pool.py` — warm-provider spawn, inventory, claim, health, and discard
- `session_background.py` — persistent and multiplexed background runtimes
- `session_compaction.py` — context gates, compaction execution, verdicts,
  cooldowns, and guarded escalation
- `session_lifecycle.py` — refresh/reload, reset/remove/destroy/discard,
  identity retirement, stop, drain, and close ordering
- `session_cleanup.py` — cleanup-task state, watchdog hooks, idle/RSS/stuck-turn
  policy, and process/filesystem sweeps

A dashboard slot bound to a remote crew keeps one memory boundary on both sides.
`remote_relay.create_peer_slot()` always includes the validated `memory_mode` in
the peer's `POST /api/chat/slots` payload, while agent and model remain sparse
explicit picks. Omitting the mode would let a local Incognito or Temporary row
execute as Persistent on the peer and read or write memory the user disabled.

Cross-boundary calls that were observable on `SessionManager` route back through
the facade, and patchable module dependencies are resolved through injected
call-time functions. Persistence remains owned by the existing `SessionMap`
contract; this decomposition does not change its stored format.

The owner protocols and forwarding dependency adapters are transitional
compatibility boundaries, not extension points. New internal behavior belongs
on the service that owns its state; widen a protocol or adapter only when an
existing facade/import/monkeypatch seam requires it. Individual adapters may be
retired in follow-up changes after repository-wide callers and characterization
tests have moved off the corresponding legacy seam.

## Agent selection provenance

Each session owns one canonical `execution_context` in its ordinary metadata.
The frozen record binds `member_id` (or explicit Global/V1), `MemoryStoreRef`,
member/template namespace, provider template, privacy mode and app attribution.
A configured member has an immutable persisted ID independent of its editable
label. Templates, projects and display names cannot select member memory.
Discovery of a same-named member cannot reinterpret an existing template session.
Resolved dispatch bindings use the canonical `default` name for Global memory,
including when restored from a captured execution record. Equivalent aliases
therefore compare equally without changing the selected conversation identity.

Admission captures the record before asynchronous work and passes it directly to
provider allocation and prompt construction. Automatic selection publication
compares the captured record; an explicit concurrent choice wins over delayed
preparation. Rollback compares only the record this operation published, including
live restricted-session records. Slot/session locks, drained cancellation,
ordinary owner/app permissions and governance remain in force. A malformed or
missing member carrier reports memory unavailable and never chooses Global.
Existing ordinary V1 sessions retain their V1 behavior; old V2 grants are not
migrated or used as a second authority.

Persistent sessions serialize this record in their existing owner metadata.
Incognito and Temporary sessions keep it in live session state and suppress
Crew transcript/body persistence. Incognito may read memory; Temporary does not.
Both refuse learned-memory writes. Children inherit the strictest admitted mode,
even after a parent closes; replacing a parent cannot broaden their policy.
If an existing persistent session becomes restricted, its existing owner record
atomically tightens only retention metadata. No restricted body is added or
rewritten, and restart cannot recover a weaker mode. A new restricted session
does not create a durable owner record.

Tab close and idle archival snapshot the live restricted identity before yielding;
cleanup does not read durable session metadata. Tab close retires its nudge loop
before other asynchronous work. After the last consumer and provider stop, cleanup
releases only the captured identity, preserving any replacement session carrier.

## Private member session ownership

The section name is retained for existing documentation links. Member memory is
application routing, not a confidentiality boundary against arbitrary code under
the same OS user. There is no separate protected session/run grant, memory PID
ancestry database, member HMAC capability, namespace mount or Seatbelt memory rule.
Ordinary transport authentication, owner/app permissions, audit integrity,
credential redaction and the host sandbox remain independent requirements.

The member and store in an admitted session stay fixed. An explicit provider
switch may change a template only through the session selection path; it cannot
select another member store. A new member choice opens a new conversation where
required by the dashboard ownership contract. Restoring history restores its
canonical record, rather than resolving the latest display alias or project.
Cron, workflow and subagent owners carry the same record in their own durable
state so they do not depend on the lifetime of an originating chat.

Permanent rules, briefing and profile documents are read directly using stable
member identity. They remain available when the learned database is unavailable.
Normal member prompts do not inject learned/core rows; explicit recall reports
an unavailable or mismatched database without falling back to Global.

Essential-context receipts belong to the serving provider, not a shared builder
or logical key. Inner client, native session ID and process-instance identity
prevent replacement providers from inheriting receipts. Compaction invalidates
receipts and late pre-compaction completions cannot restore them. Fresh/resumed
member lifecycle rules still control reinjection. Member prompt documents are
prepared before process launch; a warm runtime prepared without them is bypassed.

## Member capability generations

Enrolled members prepare capabilities only when allocating a new runtime.
`session_capabilities.prepare_runtime` reconciles ordinary Parent updates and
verifies the saved materialization off-loop before provider construction. It
passes the immutable template explicitly while preserving the canonical member,
member memory binding, history key, caller model and approval policy. An explicit
or resumed cwd wins; otherwise the member's configured workspace is used. A cwd
that disagrees with the saved Parent identity refuses startup.
When no caller model is supplied, allocation resolves the member's model pin by
its canonical alias, including members that have not enrolled capabilities.

Enrolled allocations bypass warm and shared processes. Full-spec loading is
supported by the dedicated Kiro backend; other harnesses refuse explicitly rather
than falling back to the default agent. A successful mode handshake, fresh process
instance, live session id, and post-start saved-byte/ownership/governance checks
are all required before `_Session.loaded_capabilities` is stamped. MCP hot reload
is not evidence that prompt, resources and the rest of the spec were loaded.
The applied view also checks that each enabled MCP connection in that saved
version has reported ready through the provider's own MCP report. Missing reports
remain unverified, authentication requests remain pending, and initialization
failures or unresolved tool refs report failure. Later ready reports can clear
that state without restarting the conversation; raw provider errors are not
included in capability status responses.

`SessionManager.capability_runtime_view(member, saved_revision)` delegates to
`SessionAllocationService`, which projects its owned `SessionRegistryState` on
the event loop through `session_capabilities.runtime_view`. The projection reads
live occupants and failed allocations from that same state and returns fresh
response rows, never mutable registry dictionaries. Dashboard handlers do not
access the manager's private registries. Old live sessions report pending and keep
their current turn and context; saving never resets them or requests history
replay. A changed process, handle, active template or governance generation removes
the applied claim. Replacing a provider clears its stamp. Failed starts leave a
bounded retryable diagnostic; a successful retry replaces it with the real session.
The owner capabilities GET and PUT handlers call this helper on the event loop
for the saved revision. Preview never claims runtime adoption. A failed saved-byte
or source validation remains failed even if an older provider is still alive;
persistence alone cannot claim application.

## Background Session

`BACKGROUND_KEY = "_bg"` is a persistent shared session for lightweight
background work. It is:

- **Created on startup** by `start_pool()` alongside the warm pool
- **Never expired** by idle cleanup (`_expire_idle` skips it)
- **Serialized** by the per-session semaphore (one background task at a time)
  — applies to the **non-kiro** `_bg` path only; see "Multiplexed _bg runtime"
- **Shared by**: heartbeat tasks, lesson extraction (NOT cron — see below)

This eliminates the cost of spawning/tearing down a kiro-cli process for
every cron job or heartbeat tick. Background tasks acquire the semaphore,
do their work, and release — the process stays warm.

### Context Overflow Protection

`recycle_background()` is called after every background task completes.
It checks context usage and **recycles** (kill + fresh spawn) the session
if needed — no compaction, since background tasks are stateless:

- At ≥ 70% context → recycle (same threshold as chat's default compaction)
- A reported 0% that the provider flags as *unknown* (`context_usage_unknown` —
  the backend compacted in place) → recycle
- After 40 prompts (`_BG_BLIND_RECYCLE_PROMPTS`) → recycle (blind backstop).
  This backstop is **not** gated on the reported percentage: background turns are
  tiny text prompts that never approach 70%, so keying it on "the backend reports
  no metadata" retired it permanently as soon as any real percentage was read,
  leaving the provider with no lifetime bound for the whole gateway uptime.
  `recycle_background()` counts the turn itself (`check_context_usage` is a
  chat-turn hook and never advances `_bg`), and the log names the backstop rather
  than the percentage that did not trigger it.
- Below thresholds → no-op (session stays warm)

Callers: heartbeat callback, taskrunner lesson extraction.

### Multiplexed _bg runtime

`get_bg_session()` acquires a `_bg` handle, dispatching by `agent.acp_backend`
and returning `AcpSessionHandle | _ProviderBgSession`. Dispatch is via
`_bg_backend_supports_runtime()` — positive membership in
`_bg_runtime_backends()` — the intersection of `ACP_BACKENDS_ACP_RUNTIME`,
`ACP_BACKENDS_SESSION_EVICTION` and `selectable_backends()` — never an
inequality (harness parity). The selectability term is defense-in-depth: a
runtime-capable harness that is not operator-selectable must not be spawnable
here from a config object that skipped the loader's normalisation.

The eviction term is what decides whether a harness that runs on the shared
runtime may also serve this path. Background handles are the high-churn ones —
title generation, suggestions, folders and nav each take their own ephemeral
`sessionId`, many per conversation, at a rate the user never controls — so a
teardown that does not evict is unbounded growth here. Membership in
`ACP_BACKENDS_SESSION_EVICTION` is the claim that the teardown verb Crew sends
actually frees one session, and it is earned by measurement. Codex is a member
on that basis: its teardown is the standard `session/close` sent as a request,
after which the same `sessionId` no longer answers `session/set_config_option`
(measured live against codex-acp 1.11.0). It was excluded for as long as the
verb Crew sent was `session/cancel`, after which the session kept answering and
a further `session/prompt`'s `cachedReadTokens` showed the context resident —
`cancel` interrupts a turn, it does not end a session. The delivery is part of
the fact: the same `close` sent as a notification is ignored and evicts
nothing, which is why the harness declares `notification=False` and why a gated
live test re-measures both on every install with the adapter.

The background path is not the only reader. `AcpSessionProvider.new_conversation`
— the warm-reset primitive the workflow pool reaches for — reads the same set,
because the whole reason that path is cheap is that `old.destroy()` reclaims the
previous session on the already-running process. On a non-evicting harness that
call frees nothing, so each pool reset would leave one more resident session, and
here the "person opens chats" bound does not apply: a pooled workflow resets at
whatever rate its steps run. Nor can a recycle rule be relied on to rescue it,
because a rule that measures a narrower scope than where the sessions live
never sees the growth, leaving the age ceiling as the only reaper. So a
non-evicting backend is REFUSED there before any session is created, and
`WorkerPool.reset` takes its existing hard-reset fallback — slower, and correct
for every harness. Refusing before the `session/new` rather than after is the
whole point: creating first would leak exactly the session the refusal exists to
prevent.

- **runtime-capable backend** (`_bg_runtime_backends()`) — each caller (title
  generation, suggestions, folders, nav) gets its **own** ephemeral `sessionId`
  multiplexed on a single shared `_bg_runtime` (an `AcpRuntime` spawned under
  the CONFIGURED backend), created lazily under `_bg_runtime_lock`.
  `create_session()` runs **outside** the lock so independent callers aren't
  serialized. The runtime is respawned-and-retried once on `AcpRuntimeDead`
  (`max_retries=1`, 2 attempts total).
- **any other backend** — falls back to a `_ProviderBgSession` over the shared
  `BACKGROUND_KEY` `_Session`, serialized by its `Semaphore(1)`. In the public
  Kiro Crew edition `agent.provider` is fixed to `acp` and only kiro and KAS are
  selectable, so this branch is the dormant fallback for the reserved
  `ACP_BACKEND_CLAUDE` seam only.

Two conditions displace the cached `_bg_runtime`: a **backend switch** and
**staleness** (`AcpRuntime._is_stale()` → `"age"` past 6h, or `"rss"` past
500 MiB across the descendant tree). The displacement policy has ONE
implementation, `_detach_bg_runtime_locked(runtime, cause)`: the runtime is
killed if idle, and **parked on `_draining_bg_runtimes` if it has live or
initializing handles** — parked runtimes never receive a new session (only
`_bg_runtime` is offered to callers), their in-flight work finishes untouched
(killing mid-turn would abort an in-flight title generation), and
`_reap_drained_bg_runtimes_locked()` kills each once its last handle drains.
Either way the slot is freed in the same lock hold that spawns the replacement,
so the very next background call runs on a fresh process even while the old one
is still draining. `cause` is threaded into every log line the displacement
emits, because a staleness recycle and a backend flap have different remedies
and must not read alike.

Two paths reach it. The backend-switch adapter
`_displace_bg_runtime_locked(runtime, cached_backend, configured_backend)` is
called from `_retire_stale_backend_bg_runtime()` and from the `acp_backend`
mismatch check inside `get_bg_session()`'s runtime branch (the mismatch outranks
staleness). The staleness probe sits in that same branch and is run on **every**
eligible runtime, busy or idle, using the full `_is_stale()` predicate rather
than the cheap age-only `_stale_by_age()`: waiting for a zero-session window is
not a bound, since a multiplexed runtime under sustained background load never
has one, and RSS — not age — was the growth mode observed (multi-GB over ~24h).
The cost of probing the busy path too is that `_bg_runtime_lock` is now held
across `_is_stale()`'s offloaded RSS read for busy runtimes as well; that is
bounded by `_RSS_PROBE_MIN_AGE_SECS` (5 min), below which the probe returns
without any executor round-trip, and a runtime that answers "stale" is displaced
rather than re-probed. `_draining_bg_runtimes` has **no cap** — a retiree whose
handles never drain stays parked and sweep-shielded — so the
`%d _bg runtimes are parked draining` warning is the signal that displacement is
outpacing the drain.

Parked runtimes stay shielded from the orphan-PID sweep
(`_companion_runtime_pids`), block the account-identity sweep's completeness
(`_retire_kiro_bg_runtime`) while they drain, and are reaped by a periodic
watchdog hook (`bg_drain_reap`) as the backstop for an idle gateway where no
other trigger runs. `close_all()` detaches both holders atomically under
`_bg_runtime_lock` and kills the detached snapshot; its counterpart `_closing`
gate in `get_bg_session()` refuses to spawn or park once shutdown has started.
Note there is currently no dashboard edit surface for
`agent.acp_backend`, but a file or CLI edit does not wait for the next gateway
start: the config watcher dispatches it as a `_FACTORY_CONFIG_PATHS` change, so
`refresh_defaults()` rebuilds the factory and retires stale-backend background
runtimes on the write. A future edit surface gets retirement for free
by routing through it like the other `agent.*` defaults. The provider-path
retirement trigger is dormant in the public edition for the same reason the
`ACP_BACKEND_CLAUDE` branch is: every selectable backend is runtime-capable.

Both paths yield `AcpEvent` through the shared
`acp/_dispatch.parse_session_update` parser, so there is no behavioral drift
between them. Callers **MUST** call `session.destroy()` in a `finally` block
when done. See [acp-client.md](acp-client.md) for `AcpRuntime` /
`AcpSessionHandle`.

**Cheapest-model bg tasks**: the categorical/classification background tasks
(folder-icon `chat_folders.py`, link-summary `chat_nav.py`, session title
`chat_title.py`, session-summary `handlers/sessions.py`, STT endpointing
`stt_stream.py`, and the lesson-contradiction check `dashboard/handlers/cron.py`,
plus tips generation) express a `"auto"` model preference and pass it to a
best-effort per-session `set_model`. The wire chokepoint
(`AcpSessionHandle.set_model` → `resolve_usable_model`) mirrors the interactive
`_wire_model_id`: it sends a served id (a persisted pin carrying a stale
`<namespace>::` qualifier is folded to the advertised spelling via
`resolve_pin_spelling`), sends `"auto"` only when the backend
advertises it, and for anything else — `"auto"` where a partition doesn't serve
it, or an unentitled concrete id — resolves to `""` and **skips the
send**, inheriting the session's served backend default. So these tasks never
put an unserved model or a literal unavailable `"auto"` on the wire (which would
fail with `Invalid model ID`). A reactive retry in `run_bg_oneliner`
(retry once with the first advertised model on a mid-prompt rejection) remains a
thin backstop for the fail-open case where the advertised set was unknown at
send time.

## Key Behaviors

- **Empty-response recovery ladder** (dashboard chat runner, depth-0 turns
  only): a completed turn with no visible output, no refusal reasons, and no
  cancellation is treated as a transient provider failure and recovered
  through a bounded three-rung ladder driven by `slot._empty_response_retries`:
  1. **first empty** → the ORIGINAL message is silently re-queued at the
     front of the slot queue (no visible card). Reached ONLY by a turn with no
     activity — see the productive-turn exclusion below;
  2. **later empties** (the same-message retry also produced nothing) → a
     synthetic continue nudge (`_EMPTY_AUTO_CONTINUE_MSG` — a DIFFERENT
     message, since re-sending the identical prompt tends to reproduce the
     identical empty generation) is queued on the SAME live session, with a
     transcript-visible notice card. The budget is
     `session.empty_response_max_continues` (default 1 — one nudge, notice
     "auto-continuing once"; above 1 consecutive failures keep continuing and
     the notice shows "recovery N of M"). Gated by
     `session.empty_response_auto_continue` (default ON; the gate fails open),
     and suppressed while a Stop is active;
  3. **budget exhausted** (the nudges also produced nothing) → terminal notice card
     asking the user to send a message; the counter resets so the next
     genuine user turn gets a fresh budget. The card's wording is cause-aware,
     mirroring rung 2's split: a productive turn is told the turn ended
     without a closing reply and that completed steps will not re-run (never
     "returned nothing", which is false for it and — read back by the model
     via the transcript — invites a redo of landed side effects). For
     non-productive turns the recovery clause appears only when the counter
     shows budget was spent, and claims only that automatic recovery was
     attempted — the counter counts budget, not which rungs ran (with the
     auto-continue gate off, give-up arrives at one with no auto-continue).
     Give-up with the counter at zero is reachable non-productive only on
     nested depth>0 turns, where the card reports only the empty turn (the
     gate-off zero-counter path is productive by construction and takes the
     productive wording).

  **A PRODUCTIVE turn never reaches rung 1.** "Empty" at this branch means only
  that the FINAL assistant segment is empty, which is not the same as "the turn
  did nothing": `assistant_text` is reset at every tool boundary, so a turn that
  streamed an answer and then called a tool arrives here with its answer already
  flushed, persisted and on screen, and a tool-only turn arrives here having run
  real side effects. Rung 1 re-queues the user's own message, so for either shape
  it re-executes completed tool calls (a second `send_message`, a second write, a
  second PR) and re-derives an answer the user has already read — observed in the
  field as two consecutive billed `end_turn` turns, each with a preamble and
  successful tool calls, both classified empty and the first verbatim-replayed.
  `chat_utils.EmptyTurnActivity.productive` is the guard: a flushed visible
  segment, a dispatched tool call, or thinking. A productive turn skips to rung 2,
  which carries `_ACTIVITY_NO_REPLY_CONTINUE_MSG` instead — the same
  `EMPTY_RESPONSE_RECOVERY_PREFIX` marker (so no new recovery card or locale pair
  is needed) with a body that does NOT claim the turn produced nothing, because
  that body is read by the model and would invite it to redo work whose side
  effects already landed. Its notice card differs for the same reason. The ladder
  bound is unchanged: a productive turn spends the same budget, it simply never
  spends it on a replay. `_produced_visible_output` deliberately does NOT cover
  this case — its narrow meaning (only the mid-turn resets that are not tool
  boundaries: steer cut, compaction, clear, agent switch) is load-bearing for the
  promise-only guard.

  A successfully delivered non-blocking `ask_question` directive is different
  from a generic productive tool-only turn: its card is the intended terminal
  output, and the tool explicitly tells the model to end until the user's answer
  arrives as a new message. The runner therefore records the successful card
  outcome and skips the entire empty-response ladder. Delivery failures keep the
  normal behavior so the model can fall back to a plain-text question.

  **Turn-end diagnostics.** The branch emits ONE privacy-safe WARNING per empty
  verdict, after the rung is chosen, naming a closed `cause` and `rung` plus
  booleans: `provider_empty`, `tool_only`, `thinking_only`, `visible_partial`,
  `no_terminal_event`, `synthetic_completion` or `other`
  (`chat_utils.classify_empty_turn`, ranked most-specific first), and `replay` /
  `continue` / `give_up`. `EmptyTurnActivity` carries whether a terminal
  `EVENT_COMPLETE` arrived, whether the provider SYNTHESIZED it, the terminal stop
  reason normalised onto a closed set (`chat_utils.normalize_stop_reason` — an
  omitted reason answers `absent`, which is a distinct observation from a clean
  `end_turn` and must not be laundered into one, and an unrecognised backend
  string answers `other` rather than being echoed), whether text streamed, whether
  a visible segment was flushed at a tool boundary, whether tools ran, whether
  thinking ran, and whether the provider reported ANY billing dimension. Every
  field is a bool or a closed constant by contract: no prompts, responses,
  thinking, tool arguments or results, paths, identities, token counts or costs.
  The predecessor logged only `Empty model response (attempt N)`, which could not
  separate a provider that generated nothing from a turn whose answer a tool
  boundary flushed away from a turn no terminal event ever closed — three faults
  with three different owners, and one field incident hit all three in three
  consecutive attempts.

  Recovery rungs 1–2 skip persistence/consolidation/success-recording (the
  empty turn is never saved) and preserve all other retry budgets. Synthetic
  recovery messages (`_SYNTHETIC_RECOVERY_MSGS`: the post-transient CONTINUE
  instruction and the empty-response nudge) are excluded from the
  genuine-new-turn allowance reset, so a recovery turn can never refresh its
  own budget; on the queue-drain path they classify as **recovery**
  STRUCTURALLY — ``queue_insert`` tags the entry ``kind="synthetic_recovery"``
  and every queue consumer (merge predicate, sub-agent hold, drain-role
  assignment, reset-notice consumption) dispatches on that metadata, never on
  content equality, so classification survives queue transformations and a
  user pasting the recovery text verbatim still classifies as plain user
  speech. The transcript append uses the `inject` role (never `user`, so an
  internal orchestration instruction is never persisted as user-authored
  history or mirrored to linked channels), draining one does not cancel a
  pending synthesis, and the tag is merge-breaking so a nudge is never folded
  into a `[N queued messages merged]` user turn. At `_prompt_depth > 0` the ladder is disabled entirely (terminal
  notice on the first empty) to prevent nested-turn re-queue loops.
- **Leaked tool-call notice** (dashboard chat runner, depth-0 turns only,
  issue #6112): a turn that ends normally with an invoke block emitted as
  TEXT and zero tool calls executed — the model wrote its invocation into the
  prose channel instead of dispatching it (observed with deferred MCP tools
  whose schema is not yet bound, and with large nested arguments) — surfaces
  a visible notice card and is marked un-landed (no success recording, no
  budget reset, no consolidation), so an unattended monitor/autonudge cycle
  that leaked never lands silently. Detection is machine-shaped
  (`chat_utils.has_leaked_tool_call`): an unquoted invoke open tag plus a
  parameter or close tag, with fenced code blocks and inline code spans
  stripped first so a pasted transcript or explained example never matches;
  the gate (`should_notice_leaked_tool_call`) is claimed ahead of the
  promise-only guard and excludes stage-execution turns (the orchestrator's
  stage loop reads the turn result for stage accounting). Deliberately **notice-only** — no continuation is
  queued, because an injected "re-issue that call" would carry runtime
  authority into sessions where the call auto-approves (slot trust, global
  yolo, or a static agent tool allowlist, the last invisible at the runner
  layer, so no fail-closed downgrade condition exists) and the leaked block
  may be untrusted external content the model merely reproduced. A loop loses
  one cycle, visibly, and retries on its own schedule. Scope limit: the
  notice/un-landing applies only to ZERO-tool-call turns — a mixed turn that
  executed tools and then leaked its final dispatch as text lands normally
  (un-landing a turn whose earlier calls had real side effects would
  misdescribe it) and gets its own notice card, worded for that shape:
  the earlier calls were ATTEMPTED and may already have taken effect, so the
  reader is told to check what landed rather than that nothing ran
  (`should_notice_mixed_turn_leak`).
- **Leak dropped at a compaction boundary** (issue #11995): a REAL
  (non-synthesized) mid-turn compaction terminal is a segment boundary, so the
  runner clears `assistant_text` there — text streamed before the
  summarization belongs to the window that was just summarized and must not
  carry into the segment flushed afterwards. Both gates above read that
  accumulator AT TURN END, so a leak that streamed BEFORE the boundary was
  invisible to them and both declined on an empty segment, while the raw
  invoke block had already reached the user (chunks stream to the wire as they
  arrive) and the boundary flushes nothing. The turn then took the
  post-compaction continuation arm precisely BECAUSE the segment was blank, so
  the session showed raw machine syntax followed by "continuing
  automatically", ran no tool, and accounted for neither. The scan therefore
  runs AT the boundary, before the clear, and only the resulting boolean
  travels to turn end (`should_notice_compaction_dropped_leak`); a scan placed
  after the clear reads an empty string and silently restores the defect, which
  is why the ordering carries a source-level ratchet, and the recorded fact
  ACCUMULATES (`or`) so a turn crossing two boundaries does not lose the first
  one's leak to a later clean segment. Strictly notice-only and weaker than both
  siblings: it un-lands nothing and sets no flag a recovery arm reads. Because it
  owns no turn outcome it is evaluated OUTSIDE the `if`/`elif` chain its siblings
  sit in — every arm of that chain owns the outcome and two are recoveries (the
  L1 infrastructure retry, the promise-only guard), and since these gates exclude
  neither shape, an exclusive slot would starve a turn that both dropped a leak
  and needed a recovery. One turn still gets one leak card, enforced by a flag
  the siblings set (`leak_already_noticed`) rather than by position. It needs no
  tool-count gate (that gate protects un-landing, and there is nothing here to
  un-land) and no stage-execution gate (a notice changes no turn result the stage
  loop reads).
- **Context compaction**: at ≥ configured threshold (`session.autocompact_pct`, default 70%, valid 5–90), compacts **in place** on a backend that can serve
  `/compact`: kiro-cli via a `/compact` **prompt** (`session/prompt` +
  `_kiro.dev/compaction/status` watch — never the string form of
  `_kiro.dev/commands/execute`, which kiro-cli 2.14.0 exits rc=0 on),
  claude via SDK `/compact`, codex via the `compact` command its
  `available_commands_update` advertises (the compaction is reported as a
  `tool_call` pair marked `_meta.contextCompaction`, translated by
  `_dispatch.parse_codex_compaction_update`). A backend OUTSIDE `ACP_BACKENDS_COMPACT` is
  declined instead (`"compact_unsupported"`, see the gate ladder below):
  KAS never answers the `/compact` prompt with a compaction status, so an
  ungated dispatch stranded the status wait for the whole budget WHILE
  HOLDING the turn semaphore and then recycled the session, losing the live
  conversation (#7812) — it summarizes on its own initiative and its
  `summarization_completed` frame resets the meter, so declining leaves
  nothing unmanaged. The
  process and session ID survive, so queued/agentic work continues
  automatically. kiro-cli only: if the in-place compact fails, times out,
  or the provider lacks native support, falls back to the legacy
  **recycle** (kill session; context re-injected via
  `build_session_context()` on next message). A recycle is never forced
  through a live turn — if the turn semaphore cannot be acquired within
  the budget, the attempt is deferred to the next turn-end check. A
  compaction whose IMMEDIATELY-MEASURED effect verdict (a confirmed reading
  taken right after the attempt, showing a real but < 5-point drop) is still
  ≥ `_POST_COMPACT_RESET_PCT` (95%) escalates to a reset with the native
  resume sid cleared in the same tick as the pop
  (`reset(clear_conversation=True)` via `_reset_still_critical` — promoted
  from the task runner's post-check, #4686). Deferred (next-reading) verdict
  settles are deliberately damping-only: that reading includes the following
  turn's own growth, so it cannot distinguish a failed compaction from a
  successful one regrown by a large turn — it arms the cooldown but never
  resets. The escalation is AWAITED (the `compact_if_needed` seam returns
  `"reset"`), performed BEFORE the compaction callback fires (the callback
  awaits arbitrary surface I/O, and a turn completing inside that window
  must not be erased by a verdict measured before it ran), pins the measured
  session's identity (`expect_session`) so a stale escalation never destroys
  a replacement registered under the same key, and honors `skip_if_busy` (a
  declined reset maps back to `"ok"`; the still-critical session re-attempts
  the whole compact-and-escalate cycle at its next threshold crossing after
  the cooldown, with the mid-stream overflow guard covering the interim).
  There is no prompt-count fallback on this path — the 40-prompt blind backstop
  belongs to `recycle_background()` alone (see "Context Overflow Protection").
- **Circuit breaker**: force-resets session after 5 consecutive failures.
- **Dead provider detection**: `get_or_create()` checks `provider.is_alive()`
  on the fast path. If the backing process died (crash, SIGKILL, orphan
  cleanup), the stale session entry is removed and a fresh cold-start
  occurs with `is_new=True` — ensuring full context re-injection. Without
  this, the context builder would see `is_new=False` and skip episodic
  memory, leaving the new ACP process with zero history.
- **Per-session semaphore**: serializes concurrent messages on the same
  thread key. `get_or_create()` acquires; caller must `release()` when done.
  This includes named workflow steps: retaining conversation state requires
  `release(cleanup=False)`, not retaining the semaphore between calls.
- **Post-semaphore revalidation** (`_reacquire_and_validate`): the per-session
  semaphore may be held for a full turn, so it is ALWAYS acquired with the
  global `self._lock` RELEASED (pinning the lock across that wait would freeze
  session creation for every key and reintroduce a lock-ordering deadlock).
  Because a session can be recycled/removed or its backing process can die
  while a caller waits on the semaphore, every reuse path re-checks identity +
  liveness AFTER acquiring it, through the single shared helper
  `_reacquire_and_validate(key, sess)`. Its contract: it returns `True` with
  the semaphore **still held** (caller MUST `release`), or `False` having
  **already released** it (session went stale — caller evicts via
  `_evict_stale_session` and cold-starts). Cancellation while parked on
  `self._lock` after the acquire releases the semaphore before propagating, so
  the key never stays permanently locked. Liveness uses
  `_provider_effectively_alive` (a dead Claude-Code `per_session` process
  counts as alive — it reconnects lazily on the next `stream()`).
  Consolidating this acquire→relock→revalidate dance in ONE place is
  deliberate: a divergent copy is exactly how the stale-provider bug class gets
  reintroduced. ALL three multiplexing reuse paths route through it — the
  `get_or_create` fast path, its won-by-another-coroutine race path, and
  `open_task_session` (both its fast path AND its lost-race branch, where a task
  step that loses the registration race would otherwise wait a turn on the
  winner's semaphore and be multiplexed onto a recycled/dead runtime). A stale
  winner triggers a bounded cold-start retry (`_WON_RACE_MAX_RETRIES`). The
  only bare `semaphore.acquire()` sites are: the helper itself; a
  brand-new session the caller just created and registered (no recycle window);
  and `try_acquire` (a non-blocking, no-`await`-suspension atomic take used by
  out-of-band `/compact`, which returns `False` on contention rather than
  waiting, so it has no stale-while-waiting window).
- **Agent-model resolution cache** (`_resolve_agent_model`, class-level
  `_agent_model_cache`): the per-agent model pin resolved from agent JSON is
  cached but invalidated on BOTH the agents-dir mtime changing (a new agent
  JSON appearing bumps the dir mtime) AND a TTL (`_AGENT_MODEL_CACHE_TTL`, for
  in-place edits that leave the dir mtime unchanged). Without invalidation an
  early `"auto"` miss (agent JSON not yet present) would be pinned forever, so a
  later create/edit of the agent config would never be observed. The scan reads
  each spec through `agent_discovery._read_agent_spec`, the hardened reader that
  module documents as the one reader for both agent scopes: `~/.kiro/agents` is
  user-writable and shared with kiro-cli, so the read is size-capped and refuses
  a link resolving onto a sensitive target rather than resolving a model out of
  whatever the link names. A refused spec is skipped like a malformed one, so
  the resolution falls through to `"auto"` exactly as an absent spec does.
- **Idle cleanup**: expires sessions after `session.timeout_secs` (default
  60min). Never expires `BACKGROUND_KEY`. Dashboard per-tab sessions
  (`dashboard:{slot_key}`) idle-expire like any other session.
  A session is also expired on a second, clock-independent axis: its owning
  dashboard slot is gone. `SessionCleanup._owner_is_gone()` answers that, and it
  asks a deliberately different question of two populations. A
  `dashboard:`-prefixed key is slot-owned by construction, so absence from
  `CleanupState.active_dashboard_slots` settles it; that set is published by
  `chat_utils._sync_dashboard_slots`, which sends the *effective* key of every
  open slot. A key of any other shape, such as a channel-born slot's channel key
  or a linked slot's `linked_session_key` (`taskrunner:{id}:chat:{tok}`,
  `cron:{job}`), is slot-owned only if a published live set once carried it.
  That is recorded in `CleanupState.slot_owned_keys` at publish time and pruned
  to the keys that still have a live session OR a slot open right now, so the
  record is bounded by two finite sets rather than by uptime. That prune runs at
  BOTH ends, each sweep and each publish, because the sweep is not guaranteed to
  run at all: `session.timeout_secs=0` leaves `idle_sweep_enabled` false, and the
  sweep is where the other prune lives, so a record bounded only there would grow
  with every key ever published and never shrink. The two use one expression, so
  they are idempotent rather than two policies. The
  second half of that bound is load-bearing: a slot publishes its linked key as
  soon as it opens, which can precede the first turn that creates the session,
  and a prune against the session map alone would forget the record while the
  slot is still open. A key kept only because it is in the live set cannot be
  reaped while it stays there, since `_owner_is_gone` requires absence from that
  same set. A key that `messaging.link.is_channel_session_key` recognises is
  never recorded at all: a dashboard slot opened on a channel conversation
  publishes the CONVERSATION's own key (`slack:{ts}` and its siblings), which
  the channel owns, so dismissing that viewer proves nothing about whether the
  thread is finished and reaping it would tear a live conversation's runtime
  down mid-thread. The sweep already skips `channel:`-prefixed keys for the same
  reason; the other channel namespaces do not carry that prefix, so the
  exclusion is spelled at the record as well.

  The record describes ONE incarnation, so on the TERMINATED-OWNER path it
  is released just before that session's `reset`, not left for the next sweep's
  prune: the prune runs during the scan, ahead of every reset, so a record left
  behind outlives its session and a later fire arriving under the same key would
  inherit a claim it never made and be reaped though it never had a tab. An IDLE
  reap deliberately keeps the record: there the slot is typically still open,
  which is why the owner-gone test said no, and the record describes the SLOT's
  claim, which outlives any one session under it. The
  release sits before the `reset` rather than after it succeeds, so a publish
  landing inside `reset`'s await re-adds the key, which is the right answer when
  a slot reopens for it. A `reset` that DECLINES because the session turned busy
  restores the record itself, because the release described a session that is
  still registered: without that restore the key would leave this axis entirely
  while its tab stays closed, since it is absent from the live set, carries no
  `dashboard:` prefix and nothing else re-adds it, and would fall back to the
  idle clock. That restore is also conditioned on the terminated-owner path, so
  a declined idle reset cannot CREATE a claim for a session no slot ever made
  one for. The record is what makes the axis safe:
  without it, absence from the live set is equally true of a `cron:` fire, a
  `taskrunner:{id}:task{n}` step or a `hook:` session that is running right now
  and never had a tab, so reaping on absence alone would end live work instead
  of finished work. Two further guards apply to this axis only, and the probe
  call one of them makes carries a third question the RSS recycle shares. It
  consults the
  same `CleanupDeps.has_attached_subagents` probe the RSS recycle uses,
  fail-closed, because with session sharing on a parent's children run on its
  runtime after its own turn ends and the busy semaphore cannot see them. That
  same wrapper answers a second question FIRST, and synchronously: whether a
  completion injection is in flight for the key
  (`CleanupDeps.has_pending_injection`, installed by the gateway over its
  `_cron_injecting` counter). The gateway raises that counter before awaiting
  the injected turn's store read, so until `get_or_create` runs the session
  holds no permit, the delivering sub-agent is already `done` and so absent from
  the running set, and a closed tab leaves no in-flight delivery either: every
  other signal reads "finished" while a turn is already committed to that
  session, and the reset discards the runtime that turn is about to write into.
  The counter is the only witness, which is why the gateway's own three reset
  sites consult it and the sweep, as the fourth resetter, now does too. It is
  read inside the SHARED fail-closed wrapper, so the RSS recycle honours it as
  well, and an unreadable counter keeps the session like any other unanswerable
  probe. The sweep asks it in its own right, BEFORE the axis split, so both reap
  branches honour it: the axis that elected a session says nothing about whether
  a turn is committed to it, and the clock alone can elect a never-tabbed
  `cron:{job}` parent whose `last_used` went stale during the very sub-agent run
  whose completion injection is in flight. For the idle branch that read is the
  last one before its reset. The two branches that suspend on the sub-agent probe
  -- the orphan branch and the RSS recycle -- read the counter ONCE MORE after
  that probe, because it suspends and an injection starting inside its await is
  invisible to any earlier read. On both, that re-read is the last statement
  before `reset` and nothing between them suspends. Neither read is the atomic
  one, though: `reset` itself suspends on the registry lock before it validates
  anything, so an injection beginning while that lock is contended is invisible
  to every read a caller took first. Both resetters therefore pass
  `reset(skip_if_injecting=True)`, which asks the counter UNDER that lock beside
  the identity and semaphore re-validations, so the answer is atomic with the
  pop. The sweep's own reads stay as a cheap early-out that names its own reason
  in the log. The option is opt-in, so a user-initiated reset still wins over an
  injection, and it fails closed locally: an unreadable counter declines that one
  reset rather than raising through a sweep with other candidates to visit.
  The guard DEFERS the reap rather
  than exempting the key: once the counter returns to zero the next sweep expires
  it, so the runtime this axis exists to release is not held for good by a window
  that has closed.
  Every verdict this sweep reaches is about ONE incarnation, so on this axis the
  reset is pinned to it: the scan carries the session object out with its key and
  passes it as `reset(expect_session=...)`, which revalidates identity under the
  registry lock and declines on a mismatch. Two awaits separate the scan from the
  act, so the key can change hands in between -- a cron job firing again, a tab
  reopened and a turn taken -- and a key-only reset would hand that newcomer a
  verdict reached about its predecessor. The record restore on a declined reset
  is conditioned on the same identity, which covers both reasons for a decline:
  a session that is merely busy is the one whose claim was released, so the claim
  goes back, while a key that changed hands must not have a claim invented for
  its new holder. The idle axis keeps its long-standing key-only reset. And
  the answer is then re-asserted against the current live set. That re-assert
  must be the LAST read of the live set before `reset`, which is why it sits
  after the probe rather than before it: two awaits separate the scan from the
  act, the lock release and the probe's off-loop task-store read, and a slot can
  reopen in either window. Everything between the re-assert and `reset` is
  synchronous by requirement, so a check placed any earlier reopens the window
  it exists to close and a session the user has just resumed loses its runtime.
  While no live set has been published at all
  (`active_dashboard_slots is None`) the axis expires nothing, so a build with
  no dashboard keeps the idle timer as its only reaper. The policy is
  **re-read every tick**, not frozen at loop start: `_adopt_idle_policy()` runs
  at the top of the loop and again before each sleep, taking
  `session.timeout_secs` and `session.watchdog_rss_max_mb` off the manager's
  current `_cfg` (which the config watcher keeps current) and re-applying the
  same bounds the loader does — the 60s floor, the `0` = sweep-disabled
  sentinel, and the non-negative-int coercion of the RSS ceiling. The sleep
  between sweeps is chopped into waits of at most `POLICY_REFRESH_SECS` (60s);
  each wake re-adopts the policy and, when the interval moved, re-anchors the
  next sweep to the last sweep plus the new interval, so a shortened timeout
  pulls the sweep forward within one refresh cadence rather than waiting out
  the old interval. Elapsed time is accounted from the waits the loop issued,
  not the wall clock, so the cadence is a property of the loop alone. A
  transition is logged ONCE, not per tick:
  `CleanupState.idle_policy_source` holds the `(timeout, rss_max)` pair the
  policy was last derived from and the log fires only when that pair moves.
- **Session Watchdog** (`watchdog.py`): `SessionCleanup` owns the cleanup-loop
  state and delegates named periodic behaviours to a `SessionWatchdog` — a
  stateless sequential dispatcher over `CleanupHook(name, run)` entries
  (`tick()` isolates a hook failure with a debug-level backstop only, never
  promoting the severity of errors the lifted inline blocks swallowed). The
  hooks are assembled through the `SessionManager` facade so existing
  monkeypatch seams remain observable: `idle_expiry`, `orphan_mcp`,
  `reap_agent_scopes`, `rss_threshold`, `stuck_turn`, and `bg_drain_reap`.
  `SessionCleanup._cleanup_loop` then directly coordinates the session-root,
  sandbox-artifact, bytecode-cache, periodic tracked-PID, and untracked-MCP
  sweeps.
- **Reaping abandoned agent scopes** (`session_scope_reap.py`,
  Linux/systemd only): each agent session runs inside a transient
  `systemd-run --user --scope` under a per-instance child of
  `kirocrew-agents.slice` (`sandbox._agents_slice_name`). `--scope` GCs a
  transient unit only after its process exits, so a hard gateway kill or restart
  strands the whole tree: the leader dies, the `launcher`/`kiro-cli`/
  `kiro-cli-chat` + MCP children reparent to the systemd user manager, and
  nothing reaps the scope — the idle/RSS watchdog iterates only
  `_sessions`, the PID sweeps know only tracked roots, and
  `session_pid._is_untracked_managed_agent_orphan` is report-only. The reaper
  reconciles the cgroup tree (the only authority on what this instance leaked)
  against the live registry. It runs on every cleanup tick via the
  `reap_agent_scopes` hook — never on the gateway boot path
  (`AUTOSDE.yaml` `no-new-work-on-gateway-boot-path`: an orphan sweep whose
  cost scales with leaked state must not delay `KIROCREW_READY`), so the first
  tick after a restart is what picks up a previous gateway's strays; the hook
  gathers the live provider/pool/in-flight
  PID set (so a scope containing any live tree is never touched) and the reaper
  requires a complete tracked-PID snapshot from the `session_pid` files. An
  unreadable or malformed snapshot aborts that tick before any scope is scanned.
  A scope is reclaimed
  only when ALL hold: (i) no member PID is tracked or a live provider; (ii-a)
  AT LEAST ONE member has positive agent-runtime argv identity — the generated
  launcher, an exact argv0 basename of `kiro-cli`, `kiro-cli-chat`,
  `claude-agent-acp`, or `claude`, or a marked MCP launcher — which is the
  scope-wide stop authorization; (ii-b) EVERY member is this install's own — it
  carries the `KIROCREW_SPAWNED` marker, or its `ppid` chain reaches a
  marker-bearing member without leaving the scope's member set (ownership is by
  tree: env-clearing grandchildren such as `chrome-headless` renderers under a
  playwright `node` daemon carry no marker, and that leaked daemon tree is the
  multi-GB survivor users report); an unreadable `environ` fails closed to "not
  reclaimable"; (iii) the group leader is dead OR the
  scope's `ActiveEnterTimestampMonotonic` predates this gateway's boot stamp;
  and (iv) the scope is older than the module's 600-second grace floor. Reclaim is
  `systemctl --user stop <unit>`, then a fallback SIGTERM → 3s → SIGKILL that
  re-reads `cgroup.procs`, opens a pidfd, requires a post-pin `cgroup.procs`
  read to retain that PID in the same scope, re-derives tree ownership, and
  signals through that pidfd. A recycled PID can therefore never redirect a
  signal; a host without pidfd support leaves the member untouched. `pid <= 1` and the
  gateway's own PID are never signalled, and each reclaimed scope emits a SEL
  `agent_scope_reap` event. Only THIS install's per-instance child slice is
  enumerated: a degraded instance token (no per-instance child) is treated as
  "nothing to reap here" rather than reaching into a co-resident gateway's
  scopes. A no-op off Linux or without cgroup v2 delegation
  (`sandbox._probe_cgroup_scope`). Marker inheritance by itself never authorizes
  a scope-wide stop, so an intentional detached server left after its agent
  runtime exits is preserved. The accepted fail-closed residual is that a scope
  whose runtime-anchor members have all died is never reclaimed, even if every
  survivor still has the marker or is an attributable env-cleared descendant;
  old skipped scopes are summarized at INFO by stable reason category, making
  that residual operator-visible. Stale
  `session_pid_<pid>.txt`/`.sig` files are separately pruned by
  `_prune_stale_session_pid_files` (below); the reaper adds no second deletion
  path.
- **Stuck-turn reporting** (`_stuck_turn_check`, threshold
  `_STUCK_TURN_REPORT_SECS` = 300s, not configurable): reports a turn whose
  consumer has stopped pulling events. Exists because the per-turn watchdog in
  `acp-client.md` cannot report on itself — it is the `TimeoutError` arm of an
  async generator, so a consumer awaiting inside its own `async for` body
  freezes the generator and that arm never runs again for the turn, which is why
  such a turn emits no stall WARNING at all. This loop has its own timer and no
  dependency on any consumer. Considers only sessions whose semaphore is held
  (the only in-flight signal at this layer), reads `parked_for_secs()` /
  `parked_since` / `awaiting_permission` duck-typed off `provider._handle` so any
  transport growing those accessors is covered, and latches on the park's
  monotonic start so a park outliving the tick is reported once rather than every
  pass. **Detection only**, deliberately: a turn awaiting a human is excluded
  because `agent.tool_approval_timeout_secs` already bounds that wait; ending a
  live turn stays with the in-band path that owns the terminal-event seam and the
  non-lethal continue-nudge; and what the park is blocked on is not knowable from
  here. Logs at WARNING and fires the optional `on_stuck_turn(key, parked_secs)`
  callback — a seam so a surface that can reach the user decides what to do,
  keeping the session layer free of any dashboard import. Swallows its own errors
  like its sibling hooks. The reasoning behind putting this check here rather
  than in the read loop — the placement criterion, what a hook may honestly read
  at this layer, and how out-of-band action stays clear of the in-band recovery
  path — is recorded in
  `../../architecture/design-notes/tool-stall-watchdog-placement.md`.
- **RSS-threshold recycle** (`_rss_threshold_check`, config
  `session.watchdog_rss_max_mb`, default 1536 MiB via
  `DEFAULT_WATCHDOG_RSS_MAX_MB`; 0 disables): recycles non-busy
  sessions whose `/proc` process-tree RSS (MiB) exceeds the ceiling. Skips
  persistent (`_PERSISTENT_KEYS`) and `channel:`-prefixed keys — the same
  protected set as the idle sweep — and any session whose turn is in flight.
  A parent with attached sub-agent work — running or queued children, or a
  completion delivery still landing — is never recycled by the ceiling: with
  session sharing on those children run on the parent's runtime after its own
  turn ended, so the check consults `CleanupDeps.has_attached_subagents`
  (installed by `chat_utils.wire_session_subagent_probe()` from both
  `server.py` start paths via `SessionManager.set_subagent_probe`, built over
  the shared attached-children predicate) right before `reset`, and a probe
  that raises counts as attached. The same wrapper asks one further question
  before that coroutine probe: whether a completion injection is in flight
  (`CleanupDeps.has_pending_injection`), so the ceiling cannot recycle a runtime
  an already-committed turn is about to write into either. The recycle then asks
  that counter AGAIN once the probe returns, as the last statement before
  `reset`: the wrapper's read happens before the probe suspends, so an injection
  starting inside that await would otherwise be invisible on this path, and
  neither `skip_if_busy` nor `expect_session` can see it -- an injecting session
  holds no semaphore, and the injected turn resolves to the same object. That predicate is a COROUTINE
  (`chat_utils.subagents_attached_async`): its queued half reads the task store
  and this sweep runs on the gateway loop, so `set_subagent_probe` accepts an
  awaitable answer and the cleanup boundary awaits it; a sync probe (a double, a
  build with no queue) still answers straight away.
  The `/proc` parent→child map is built ONCE per tick off-loop
  (`_build_child_map` on the maintenance executor) and shared across
  candidate trees (`_rss_mb_from_tree`); resident pages are summed across the
  tree and converted to MiB once at the end. Measurement happens off-lock, so
  the victim's session object is captured at collection time and handed to
  `reset(expect_session=..., skip_if_busy=True)`, which re-verifies identity +
  not-busy atomically under the lock; a recycle that actually happened logs a
  warning, bumps `Stats().inc_session_cleaned()`, and fires the recycle
  callback (`set_recycle_callback` — mirrors the compact callback; wired by
  `dashboard/state.wire_session_recycle_callback()` from both `server.py`
  start paths to post a user-visible "session recycled" notice into
  `dashboard:` slots, tagged `meta={"kind": "compaction"}` so the [OPTIONS:]
  backward scan skips it). Idle/orphan sweeps do NOT fire the recycle
  callback. Linux-only measurement (`get_session_rss_mb` returns 0 elsewhere),
  so the feature is inert off-Linux.

## APIs

| Method | Purpose |
|--------|---------|
| `start_pool(blocking=True)` | Pre-spawn warm + background sessions. `blocking=False` for non-blocking mode. |
| `get_or_create(key, agent=None, approval_policy="", speculative=False, speculative_resume=False)` | Returns `(LLMProvider, is_new, resumed)`. Uses warm pool for new sessions (default agent only). Sessions with a resume mapping skip warm pool (cold start needed for `session/load`). A `reasoning_effort_override` is applied post-claim via `provider.change_effort` (updating `_effort_per_model` and the `cli.json` overlay write) rather than bypassing the warm pool, recovering pool-hit startup latency. Every decision is counted via `_record_pool_decision` (`kirocrew.session.pool.decision`) with the single disqualifying reason, so the pool's hit rate and the frequency of the `bypass_resume` case are observable. Non-default agents skip warm pool and resolve their model by precedence via `_model_fallback()` — caller model > per-agent pin > global default: `model=None` (defer to kiro's agent-JSON resolution) only when the agent pins its own model, otherwise the global default, unless that default is the `"auto"` sentinel (also `None`). The per-agent pin is resolved off the event loop via `run_in_executor` using `_resolve_named_agent_model`; blank agents inherit the global, and `kirocrew` is excluded (tracks the global). `approval_policy` is persisted on the new `_Session` — callers (e.g. subagent) pass parent policy so the session inherits it. `speculative=True` (eager spawn) pre-creates ahead of a real first turn: the one-shot `_Session.first_turn` observation — a single three-member `FirstTurnState` enum (`NOTHING_ARMED` / `FRESH` / `RESUMED`), so a resume marker on an already-claimed session is unrepresentable rather than forbidden by convention — is registered ARMED (`FRESH`) and never consumed by speculative callers, and a resumable key raises `SpeculativeResumeRefused` — unless `speculative_resume=True` (resume prefetch) opts in, in which case the speculative creator performs the `session/load` and registers the observation as `RESUMED` when the load restored the transcript. The observation is consumed in one read-then-clear by the first real claimant under the per-session semaphore (fast path and won-race path alike), with the returned booleans derived from it at the return boundary — so that turn observes `(is_new=True, resumed=True)` exactly as if it had resumed itself, preserving its history-injection decision. |
| `check_context_usage(key, provider)` | Returns %. Triggers compaction at configured threshold (default 70%), warns one `CONTEXT_WARN_MARGIN_PCT` below it. |
| `compact_if_needed(key)` | Awaitable twin of the `check_context_usage` trigger for callers that must not start their next turn while a compaction is pending (the task runner's between-steps check, #4686). Same gates in the same order — both entry points consume the shared `_compaction_gate_decision` ladder, the single owner of the gate order (its docstring documents each rung) — then AWAITS `_compact_session`. Returns the outcome: `"absent"`, `"reset"` (the settled verdict on the prior attempt was ineffective-and-still-critical and the promoted escalation reset the session here, awaited), `"cc_managed"` (checked before the threshold, mirroring `check_context_usage`), `"below_threshold"`, `"compact_unsupported"` (the provider names a backend outside `ACP_BACKENDS_COMPACT`, so no `/compact` is dispatched and no semaphore is taken — checked AFTER the threshold so a declined backend keeps its per-turn usage log, #7812), `"unconfirmed"`, `"in_progress"`, `"cooldown"`, `"ok"`, `"busy"`, `"recycled"`, `"failed"`. A `"busy"` decline means a turn holds the semaphore — the caller leaves the session alone and retries later, never falls back to a direct `provider.compact()`. |
| `record_success(key)` / `record_failure(key)` | Circuit breaker tracking. |
| `release(key)` | Release per-session semaphore (must call in `finally`). |
| `cancel_current(key, *, wait_ack_timeout=0.0)` | Cancel in-flight operation without destroying session. Returns `CancelOutcome`. Default `wait_ack_timeout=0.0` preserves fire-and-forget behavior for internal callers (taskrunner, subagent, llm_helpers). |
| `stop_turn(key, *, force=False, on_soft=None, on_hard=None)` | Cooperative stop with kill fallback. Returns `StopOutcome` (`"soft"`, `"hard"`, or `"idle"`). Clears queue unconditionally, then sends `session/cancel` and waits up to `agent.soft_stop_budget_secs`; falls back to `reset()` + eager respawn on timeout or error. `force=True` skips cancel and goes straight to hard kill. `on_soft`/`on_hard` callbacks fire before return. |
| `reset(key, *, expect_session=None, skip_if_busy=False, clear_conversation=False)` | Kill session; returns `bool` (True iff a session was actually torn down). Does NOT delete session map entry (kiro-cli file persists for future resume). Optional guards evaluated atomically under the lock with the pop, used by the RSS-recycle watchdog: `expect_session` only resets if that exact session object still occupies the key (guards against recycling a reset+recreated session on a stale off-lock RSS reading); `skip_if_busy` skips when the current session's semaphore is held so a live stream is never cut mid-turn. `clear_conversation=True` additionally clears the native resume sid in the SAME event-loop tick as the pop (entry + channel bindings survive, as in `_recycle_held`) — used by the still-critical post-compaction escalation so the overflowed conversation is not reloaded, without a delayed clear ever erasing a racing successor's sid. |
| `discard_conversation(key)` | Kill session AND clear only the resume sid (`SessionMap.clear_sid`) — the map ENTRY survives, preserving Slack thread/channel linkage and the reverse thread→session index. The cleared sid is stashed as `discarded_sid` in the entry, so the discard is diagnosable and manually reversible (the native conversation persists on disk; only the pointer is dropped). The next turn cold-starts a fresh native conversation instead of `session/load`-ing the old one. Used by the poisoned-conversation escalation in `chat_runner` (canary-verified backend rejection of a specific persisted conversation) and by the Slack / Discord / Telegram `/compact` failure recovery: the conversation is unusable but the session's channel identity must persist. This is the shape every HOUSEKEEPING teardown takes — `SessionMap.prune` refuses to delete an entry carrying a channel binding, and `_recycle_held` clears the sid for the same reason. Only an explicit user action (`destroy`) may remove a channel identity. Sits between `reset` (sid kept, resume expected) and `remove` (entry deleted, no resume). |
| `remove(key)` | Shut down a session but PRESERVE the session map entry — the kiro-cli session files remain on disk, so a future `get_or_create` restores the conversation losslessly via `session/load`. For revivable teardown (tab close, agent switch, idle kill). Permanent deletion is `destroy(key)`. |
| `destroy(key)` | Permanently remove the live provider, compaction override, and session-map entry. The map entry is deleted in the yield-free registry-pop span before the awaited end metric, so a dashboard slot cannot adopt the predecessor binding during that metric write. |
| `destroy_if(key, expected_generation, should_destroy, *, preserve_autocompact_override=False)` | Conditional permanent removal for the monotonic canonical-key generation captured by `session_generation(key)`. Under the manager lock it requires no current allocation/claim reservation, requires the generation to remain equal, requires the current session semaphore to be idle, then evaluates the synchronous slot-owner predicate immediately before the registry pop and yield-free session-map delete. Any reservation, generation mismatch (including absent→successor→absent ABA), busy session, false predicate, or predicate exception leaves provider, override, and map untouched. History deletion passes `preserve_autocompact_override=True` because another process can claim the same logical transcript; ordinary conditional and unconditional destroy keep clearing the old override. Returns whether destruction occurred. |
| `remove_if_unclaimed(key)` | Conditional `remove` for the resume-prefetch TTL: removes the session only if the one-shot `first_turn` observation is still armed (not `NOTHING_ARMED` — no real turn claimed it) AND the per-session semaphore is unheld, checked atomically under the manager lock. Preserves the session map (mirrors `remove`'s revivable shape), so the next focus or first message resumes normally. Returns `True` iff a session was removed. A claimant handed the session object but not yet holding the semaphore loses benignly: its re-validate fails and it cold-starts. |
| `close_all(drain_timeout=None)` | Pre-shutdown **drain** of in-flight turns (via `drain_active_turns`), then save all active session mappings, shut down every session, and drain the warm pool. `drain_timeout` bounds that drain (`None` = full default budget); a caller wrapping `close_all()` in its own hard deadline (Slack's restart wraps it in `wait_for(..., 5s)`) passes a smaller budget (e.g. `2.0`) so the kill path still fits inside the deadline. A cancel that fires mid-drain (outer deadline) **propagates** (CancelledError is deliberately not caught) so the caller's hard deadline stays honest; recovery of a still-held native-session lock is the next-startup orphan reaper's job. |
| `drain_active_turns(timeout=None)` | Best-effort co-operative drain that brings in-flight prompts to a safe turn boundary **before** teardown, so kiro-cli closes its native turn and releases its session lock (`~/.kiro/sessions/cli/<uuid>.json`) on the subsequent SIGTERM — otherwise the next gateway's `session/load` hits "active in another process" and the slot returns empty completions (the Make-Live empty-response incident, #200). For each registered session with an **unfinished** turn (native turn-done not yet acked — independent of cancel state, so an already-cancelled-but-not-acked turn is still drained), it issues a graceful `session/cancel` and waits (bounded) for the ack; a turn already cancelled (`cancel()` → `"no_turn"`) is waited on directly via `wait_turn_done`. The whole operation is bounded by `timeout` (`None` → `_DRAIN_ACTIVE_TURNS_TIMEOUT_SECS`, default 5.0s; internal cap is `timeout+1.0`); on timeout it logs and returns so the caller falls through to the SIGTERM-first kill path — never hangs teardown, never raises. `timeout <= 0` disables the drain. Returns the count of unfinished turns (observability/tests). Only registered user sessions are drained; the warm pool holds never-prompted processes. |
| `pause_turn_admission_for_update()` | Atomically pauses new turn admission under the session registry lock by setting the existing `_closing` gate and recording `update_pause_owned`. Returns `False` when real shutdown already owns `_closing`, so update logic cannot mask or replace shutdown. The pause covers both new `get_or_create` calls and already-issued leases reaching `begin_turn`. Channel callbacks claim a synchronous `reserve_inbound_callback()` before card or command handling; task-backed callbacks hold it until their handler task ends, while inline pollers scope it to one dispatch so the poll task does not keep updates busy forever. Admitted callbacks and pre-start client `_handler_tasks` are census-visible, while a claim refused after the pause writes the existing resend-notice route before any pre-turn side effect. Subagent, direct cron script/command, TaskRunner, and dynamic-workflow launchers read the same `admission_closed` state immediately before registering work, with no suspension before registration: a launch either registers before the pause and appears in the busy count, or is rejected after it. The subagent pump's two re-registrations read it too — the window refill (`_refill_apply`) and a resume reservation (`resume_reserve`) — because both would grow the busy count AFTER the census read even though the store already accepted that work: a hydrated row would only be refused by the spawn gate, and a resumed run would run on in a process about to be replaced. The gateway treats this boundary as apply-safe only when provider/Slack turns, every live dashboard `slot.task` (including pre-provider and remote-relay turns), named stage-loop tasks between stage turns, shielded refusal writers, and all background workloads are idle. Subagent idleness is lifecycle-based rather than slot-based: queued/running work, unexpected-cancel recovery, shielded terminal reports, accepted follow-up watchers, one-shot orphan reconciliation, and detached state writers must all settle; the perpetual maintenance reaper is excluded. After apply, it drains callback tasks and refusal writers again; a timeout defers only the restart, reopens admission, and retries in five minutes without reapplying. A successful drain is followed immediately by `fence_update_restart()`, making any later refusal write synchronous through session teardown and the final drain, with no `await` between that drain and re-exec. Mandatory updates use a target-keyed ten-minute grace only for escalation logging; they still defer behind every active turn and background workload indefinitely. Automatic update preparation never calls `drain_active_turns()` and never cancels user work. |
| `resume_turn_admission_after_update()` | Releases `_closing` only when `update_pause_owned` is still true. `close_all()` revokes that ownership under the same lock before draining, so an update failure racing real shutdown cannot reopen admission. Used when automatic apply returns instead of replacing the process. |
| `begin_turn(key)` | **Synchronous** pre-dispatch gate against the lease-dispatch race (#200 / Codex HIGH). A caller holds the per-session semaphore *lease* from `get_or_create` through the whole turn, but the native turn only opens on the first `provider.stream(...)` iteration; the `get_or_create` `_closing` gate cannot revoke a lease already issued before `close_all` set `_closing`. Callers (dashboard `chat_runner`, Slack handler, and structured Slack/Discord monitor adapters through `TurnDriver.closing_gate`) MUST call `begin_turn` synchronously — **no `await` between it and the `async for` stream drive** — so the `_closing` read and the stream's turn registration (`AcpClient.stream_events` clears `_turn_done` before its first `await`) form one yield-free span, strictly ordered w.r.t. `close_all`'s `_closing` set: the turn is either registered before the drain snapshot (and drained) or the caller aborts. Raises `SessionClosingError` (a `RuntimeError`) when closing; the caller's `finally` releases the lease. Deliberately NOT `async`/lock-guarded (an `await` would reopen the race). |

## Live config: the watcher drives `refresh_defaults`

The manager copies `session.*` and `agent.*` values out of `config.json` at
construction, so a write reaches those copies only if something pushes the new
value at them. The constructor registers that push on the process config watcher
(`live.subscribe("session", "agent", "watchdog", "agents", "workspaces",
"default_workspace", callback=self._on_config_change, name="SessionManager")`,
kept on
`self._config_sub`; the watcher holds the bound method weakly, so a manager a
test or a provider reload discards drops out of the registry on its own). Every
writer — the dashboard, `kirocrew config set`, `$EDITOR` — lands in the same
applier, so none of the behaviour below depends on which one wrote. No gateway
restart is needed for any of it.

`_on_config_change` first fails closed like the owned appliers: while any of its
six sections is degraded (a section the loader discarded holds DEFAULTS for it;
the whole-config marker alone does not gate it — see the config spec) it raises
`ConfigDeferred` for the changed paths and keeps what is in force, because
adopting defaults would rebuild the provider factory on the default model and
backend and drain the warm pool; the watcher retries each tick until the
document validates. Otherwise it does three things, in order:

- **Adopt the new config as `_cfg`, always.** Most fields under these prefixes
  are read off `_cfg` (or fresh from the loader) at their point of use, so the
  adoption IS the whole apply — `agent.soft_stop_budget_secs` per stop, and the
  cleanup loop's idle policy below. The adoption happens under `_lock`.
- **Route a factory-bound default through `refresh_defaults(cfg=change.new)`.**
  `_FACTORY_CONFIG_PATHS` lists the paths a rebuilt provider factory or a
  re-derived warm pool is the only way to honour: `agent.model`,
  `agent.reasoning_effort`, `agent.acp_backend`, `agent.role_efforts`,
  `agent.tool_search{,_min_pct,_min_tokens}`, `agent.sandbox`,
  `agent.sandbox_allow_no_isolation`, `agent.sandbox_allow_unsandboxed_exec`,
  `agent.member_acp_backend`, and `session.pool_size` / `pool_agent` /
  `pool_ttl_secs`. `refresh_defaults` is the **live-session-preserving** path —
  it rebuilds the factory, re-derives the pool and drains the warm pool, but
  never touches a registered session, so in-flight turns keep running and only
  NEW sessions see the new defaults. `reload_provider_factory` (which retires
  sessions built by the old factory) is not on this path.
  `session.eager_spawn` is deliberately absent: it is read live per spawn in
  `chat_runner`, so draining the pool for it would be pure churn.
- **Re-clamp the watchdog windows on every live handle** when the change touches
  `watchdog.*`, `agent.chat_turn_timeout_secs`, or any
  `agents.<name>.watchdog_*` key — see
  [acp-client.md](acp-client.md) for the fan-out and why it re-runs the loader
  per handle instead of copying seconds across.

`refresh_defaults(cfg=None)` takes an **already-loaded** config: the watcher
hands in the one it just loaded so the apply needs no second read, and `None`
(the request-handler callers) loads off-loop inside the fill lock, as before. It
re-derives the whole warm-pool shape, not just the factory — `_pool_size`
(clamped to `_MAX_POOL`, floored at 0), `_pool_agent` (falling back to
`agent.default_agent` when `session.pool_agent` is blank), `_pool_ttl_secs`
(floored at 0) and `_pool_cwd` — using the same clamps
`WarmSessionPool._state_from_owner` applies at construction, so a hot value can
never be a raw copy that bypasses them. `pool_ttl_secs` in particular was
re-adopted by no path before. `_pool_cwd` is `default_project_dir()`, which is
resolved from `default_workspace` and `workspaces`, so both are factory paths
and both prefixes are subscribed: a workspace edit re-derives the pool instead of
leaving a cwd-less subagent in the previous directory. The resolution reads the
config file and stats the workspace directory, so it runs in a worker thread
before `_lock` is taken, like the load itself.

Two more reads on this area follow config without a restart:

- `AcpRuntime._session_start_budget` prefers `live.snapshot()` over its
  per-runtime memo, keeping the same builtin floor, and falls back to the memo
  in a process with no watcher armed.
- `ContextBuilder`'s `{bot_name}` substitution reads
  `live.snapshot().agent.bot_name`, falling back to the value captured at
  construction when there is no snapshot or the live one is blank.

`acp/client.py`'s `resolve_prompt_timeout` already loads config per prompt
(`_effective_prompt_timeout_async`), so `agent.chat_turn_timeout_secs` needed no
applier — a raised turn budget is in force on the next prompt.

## Stop Orchestration

`stop_turn()` is the shared orchestration layer for every stop surface (dashboard Stop button, Slack `/kirocrew stop`, transport stop verbs). Sequence:

1. Record the Stop: `stop_requests[key] += 1` (per folded key, on
   `SessionLifecycleState`). This runs BEFORE anything is awaited so the
   dashboard runner's end-of-turn gates -- which may run the moment the
   provider's cancel lands -- already see it; `prev_turn_cancelled` is set only
   after the ack and is too late for them.
2. `clear_queue(key)` — queue drop is unconditional on first press (skipped
   with `preserve_queue=True`).
3. If `force=True`: skip cancel, go straight to hard kill (step 5).
4. Send `session/cancel` via `provider.cancel(wait_ack_timeout=budget)`:
   - `"acked"` → set `session.prev_turn_cancelled = True`, call `on_soft` callback, return `"soft"`.
   - `"no_turn"` → return `"idle"`.
   - `"timeout"` or `"error"` → fall through to hard kill.
5. Hard kill: `reset(key)` → fire-and-forget `_eager_respawn(key)` task → call `on_hard` callback → return `"hard"`.

### Session-scoped Stop record

`SessionManager.stop_generation(key)` reads the count from step 1 (0 for a key
never stopped). It exists because a channel-born dashboard slot runs its turns
on the channel's session (`effective_session_key` returns
`linked_session_key`), so a stop issued on the channel side reaches
`stop_turn()` and the provider cancel but never the slot's own `_stop_state`.
The dashboard runner snapshots the count at turn entry and its live Stop
signal (`_stop_pressed()`) treats any later change as a user Stop, next to the
slot's in-flight state and the slot's own `_stop_generation`; every
end-of-turn continuation gate (refusal recovery, Stop-hook continuation,
promise-only recovery, post-compaction continuation) reads that one signal.

Lifetime: the record is keyed by session key rather than stored on the
`_Session` object, so it survives the `reset()` a hard stop performs (a flag on
the session would vanish with the very turn it stopped). It is popped on the
teardown paths that end the key's conversation for good -- `remove()`,
`remove_if_unclaimed()`, `destroy()`, and the identity-sweep retirement --
beside the sibling per-key dicts. `cancel_current()` does NOT record a stop: it
is the host's own best-effort abort (queue drain, injection retry, run
teardown), not a person pressing Stop, and must not suppress a continuation
the way a Stop does.

### Cancelled-turn context restore

`_Session.prev_turn_cancelled` is a one-shot flag set on soft-cancel
success. The next prompt handler (dashboard `_run_chat`, Slack
`handle_message`) reads and clears it, then calls
`context.build_cancelled_turn_preamble(conversation_log, session_key)` to
re-inject the cancelled user prompt and partial assistant output. This is
necessary because kiro-cli discards cancelled turns from its own ACP
conversation log, so the LLM has no memory of the interrupted request.

### Edit rewind context boundary

Dashboard Edit + Send replaces the ACP session and rebuilds context from the
retained canonical history. The discarded suffix is excluded from session
replay, stop recovery, and persisted-history context; stable memory, rules,
skills, and project context remain available.

The native conversation is discarded before the retained history rewrite, and
the cleared resume pointer is flushed durably before the rewrite is committed,
so a gateway restart cannot resurrect the discarded native session. If the
rewrite fails -- or the slot was concurrently rebound to another transcript
while it was in flight -- Edit + Send returns a 503 and restores the dashboard
slot, but it cannot restore the discarded native session; a later turn
cold-starts from the original persisted history instead of resuming it.
App-authenticated requests may rewind only a slot's own dashboard session:
a channel-linked slot is refused, because its effective session is a
conversation the app does not own.

**`edit-resend` is the same boundary, not a lighter one.** It truncates and
persists history exactly as rewind does, so it runs the same three-step sequence
— discard the native conversation, flush the cleared resume sid, then rewrite the
retained history — and refuses with `edit_resend_prepare_failed` /
`edit_resend_session_busy` / `edit_resend_save_failed` /
`edit_resend_slot_rebound` rather than reporting success on a boundary that did
not land. Its own error vocabulary is deliberate: a client must be able to tell
which endpoint refused without string-matching a sentence.

**A busy SESSION is not the same question as a busy slot.** `slot.running` tracks
only that slot's own task, while `discard_conversation` is a full teardown that
also releases the shared sub-agent runtime. So `edit-resend` applies the same two
guards the sibling `reset-conversation` teardown applies before the same call, in
the same order and with the same codes: `slot_orchestrating` (409) when
`slot._in_stage_execution` — an autopilot plan reads `running` False *between*
stages while still mid-plan — and `slot_subagents_running` (409) via the shared
`chat_utils.subagents_attached_async` predicate, because the parent turn ends
before its children do. The predicate fails closed on an unreadable probe: unknown
children are not zero children. `skip_if_busy=True` on the discard remains the
atomic backstop for a turn admitted after these guards answered False.

Eight properties are load-bearing on this boundary, and each fails toward the
permissive answer if dropped. `edit-resend` carries all eight; the bullets name
the four where `rewind` does not yet, so nobody reads them as already shared:

- **The edited window is prepared on a copy, and the copy is SEVERED.**
  `copy.copy` is shallow, so reassigning `messages` alone leaves `_queue`,
  `_pending`, `_question_pending`, `_on_question_retired`, and `event` aliased to
  the live slot — and `_ChatSlot.append` writes through four of them. An
  un-severed copy therefore publishes the edited row to the live stream reader
  and announces the live question cards as retired *before* any refusal path can
  run, leaving a phantom row and a card-less "needs input" behind for an edit the
  server rejected. The commit is the one place the prepared `_pending`,
  `_question_pending`, `event` state and the retirement announcement become live.
- **The slot is reserved before the awaits.** `slot.running` derives from
  `slot.task` and the send path is not serialized on `slot._lock`, so a send
  arriving while a durable boundary is pending would otherwise see an idle slot
  and dispatch a competing turn that the commit then erases. The reservation
  publishes a dispatch task that runs the turn only on commit; on abort it hands
  a send it diverted to the queue to the canonical successor dispatch, so nothing
  is stranded. An entry queued *before* the reservation keeps its own trigger.
- **The commit re-checks its target on every path**, success included, through
  ONE predicate so no path can check a different subset. Three axes move
  independently across the awaits: the **transcript key** (a cron or workflow
  injection re-links the slot; the snapshot froze the old routing, so the save's
  own `expected_history_key` guard cannot see the live slot move and only this
  loop-side check can), the **slot object** (a close-and-recreate under the same
  name is a different conversation that leaves the transcript key unchanged, so
  only object identity catches it), and the **dispatch reservation** (if
  something else has taken `slot.task`, committing would run this handler's turn
  alongside whatever now owns the slot — two concurrent turns writing one
  window). Any of the three refuses with a retryable 503.
- **The periodic dirty-slot flush is excluded for the whole rewrite.** Because
  the live slot keeps the full window until the commit, a flush tick can snapshot
  that stale window, block behind the rewrite on the per-session history lock,
  and then write the snapshot back on top — restoring every message the rewrite
  just discarded. `edit-resend` therefore saves through
  `chat_persistence.save_slot_off_loop` (with `expected_history_key`, and
  `best_effort=False` so a failure reaches its 503 rather than being swallowed
  and re-armed as a dirty retry) instead of a bare `asyncio.to_thread`. That
  helper raises `slot._metadata_persist_inflight` around the write and lowers it
  in a `finally` — the flag `flush_slot_now` already honours to keep the unpinned
  periodic writer off a slot with a guarded write pending. Shielding the
  *wrapper* rather than the inner future is what keeps the exclusion held: a
  cancellation reaching the shield leaves the coroutine running, so its `finally`
  cannot release the flag early.
- **The cancellation drain survives REPEATED cancellation.** The worker thread
  cannot be interrupted, so once the rewrite starts it lands whether the handler
  lives or not; the handler therefore has to learn the outcome and commit to
  match. `CancelledError` is a `BaseException`, so a second cancellation — a
  gateway shutdown reaching a handler already unwinding from a client disconnect
  — is not absorbed by an `except Exception` and a bare `await` on the save task
  abandons a landed rewrite. `edit-resend` re-shields the drain a bounded number
  of times (`_SAVE_DRAIN_ATTEMPTS`) and reads the outcome off the **settled**
  task rather than awaiting it, so a cancel landing between the two cannot lose
  it. Giving up leaves the live slot untouched, which is the safe half of the
  desync. **`rewind` still drains with a bare `await`**, so it remains exposed.
- **A row that arrives during the boundary is carried, not replaced away.**
  `workflow_inject` and `cron_inject` append through `append_and_surface` /
  `slot.append` on the event loop and take no `slot._lock`, so a completion
  landing mid-boundary reaches the live window while the boundary holds the lock.
  A wholesale `slot.messages = prospective_slot.messages` drops it, and the
  rewrite save cannot restore it because a rewrite deliberately skips the
  cross-process-append scan (`collect_foreign = not rewrite`) — leaving the row
  in neither the window nor the file. `edit-resend` therefore carries arrived
  rows (identified by row object, since the window front can be trimmed and a
  restore-path row has no `meta.mid`) onto the committed window and pending
  queue. Appending them after the prospective window is the correct order, not
  merely a convenient one: `monotonic_transcript_ts` only ever moves a row
  forward, so an arrived row can never be stamped *earlier* than the edited one —
  but it can be stamped **identically**, because on a coarse clock (Windows ticks
  in ~15.6 ms steps) both appends read the same instant, and list order is what
  separates that tie. Its question map is **retired in place** rather than adopted or
  intersected: the commit deletes exactly the ids the edit retired, so a card answered
  during the boundary stays retired (the answer pops it from the live dict) and a card
  raised during the boundary survives (an intersection against the frozen copy would
  erase it). A carried row reaches disk
  the ordinary way — the commit sets `_dirty`, so the next periodic flush writes
  the merged window — and deliberately **not** through a second guarded save
  after the commit: no await may sit between the commit and the dispatch release
  (see the next bullet), so that write is not available without paying a worse
  failure. **`rewind` now applies the same delta commit**: it carries arrived rows in
  both the window and the pending queue, drops a row the client already drained rather
  than requeueing it, and retires question ids in place. The identity sets keep the
  pre-await rows RETAINED, because an `id()` is only an identity while something holds
  a reference and a cap trim would otherwise let a freed row's address be reused by an
  arrival. The other rewrite-save callers (`regenerate`, `fork`) are **deliberately
  still open** to the injected-row loss; closing them belongs with the shared boundary
  contract rather than one endpoint, and the fixed subset is exactly `rewind` and
  `edit-resend`.
- **The commit and the dispatch release are separated by no await.** Once the
  live slot has adopted the truncated window, the reserved dispatch is armed and
  only `dispatch_ready.set()` in the handler's `finally` is left to run. An await
  in that gap lets a cron or workflow completion rebind the slot, and the
  released dispatch then runs the edited prompt against ANOTHER conversation —
  and the commit-target fence cannot rescue it, because refusing after the live
  slot has adopted the truncated window would leave a truncation with no turn.
  So post-commit work is left to the next periodic flush rather than awaited
  here. **`rewind` still awaits its orphan-session cleanup in that gap**, so it
  remains exposed.
- **App ownership is authorized through the shared gate**
  (`_check_slot_app_ownership`, plus `_reauthorize_after_await` across the
  body-read await), because discarding a native conversation is a destructive
  capability. It authorizes the `_app` binding, the effective SESSION key, and
  the TRANSCRIPT key, so a channel-linked slot and an unbound channel-origin slot
  are both covered by one check rather than a per-endpoint link test. Denials are
  404, not 403 — indistinguishable from a missing slot (anti-enumeration); the
  real reason is in the SEL audit log.

### Eager Respawn

After a hard kill, `_eager_respawn(key)` calls `get_or_create(key)` in a background task so the next user message finds a warm session. On failure, logs at debug and does nothing — the next message triggers `get_or_create` again via the normal path.

## Session Resume (SessionMap)

Persistent mapping of `session_key → kiro_session_id` stored at
`~/.kiro/crew/session_map.json`. Enables `session/load` to restore full
kiro-cli conversation history when a session is recycled.

**Only long-lived conversational sessions are mapped.** Stateless sessions
(cron, subagent, taskrunner, channel, secretary, side, heartbeat/background,
`wf-author:` workflow authoring, and `wf-pool:` warm workflow-pool workers) are
excluded via `_STATELESS_PREFIXES`. A `wf-author:` session is also explicitly
destroyed after each authoring attempt, which shuts down its provider, removes its
registry entry, and deletes any stale map entry; stateless classification prevents
resume lookup or persistence during acquisition. The `wf-pool:` prefix keeps
per-run pooled workers (workflows/agent_pool.py) from persisting a session_map entry
or resuming a prior transcript — their hard-reset fallback must hand the next task
a clean session, never a `session/load` replay of the previous task's conversation.
The `side:` prefix is included so
`/side` conversations never resume across KiroCrew restarts — each cold-start
triggers `is_first_turn=True` in `build_side_message` which re-seeds the
parent snapshot + accumulated side history.

**Lifecycle:**
- `get_or_create()`: looks up mapping → if found and `.json` file exists,
  sets `resume_session_id` on the ACP client and skips warm pool. After
  `ensure_ready()`, saves the new `session_key → session_id` mapping.
- `reset()`: does NOT delete mapping — the kiro-cli session file persists
  on disk. Next `get_or_create` will try `session/load`.
- `remove()`: deletes mapping — explicit tab delete, no resume expected.
- `close_all()`: saves all active mappings before killing processes.
- `start_pool()`: prunes stale entries (files deleted by kiro-cli GC).

### Asking for a fresh conversation on a slot that stays open

`POST /api/chat/slots/{slot}/reset-conversation` drops one slot's resume pointer
through the LIVE manager (`discard_conversation`), so its next turn cold-starts
instead of `session/load`-ing the accumulated conversation. The slot stays open,
the transcript stays on disk, and the map ENTRY survives with its channel
linkage.

This closes a gap rather than adding a capability: resume is key-driven and a
slot key is stable by design, which is correct for a tab reopened later and wrong
once a long-lived conversation has drifted, filled up, or outlived what it was
about. The only reachable way to break the link was `DELETE /api/sessions/{key}`,
which destroys the record in order to reset the pointer — so "start over" and
"erase this" were the same button.

**`replay` is what the caller means by "fresh", and it has to be asked for.**
Clearing the sid stops the provider resuming its own conversation — and "the
provider has no history" is precisely the condition that makes the next cold
start rebuild one from `conversation_log` as a `[CONVERSATION HISTORY]` block
(`chat_runner`, injected OUTSIDE the capped session context). So the two
mechanisms work against each other by construction: the caller discards the
conversation and the next turn is handed a reconstruction of it. Measured on one
app-owned session, that replay was 80,359 characters — 76% of the first turn's
injected context, and most of what discarding the conversation was meant to
reclaim. `discard_conversation(key, replay=False)` records a ONE-SHOT
suppression, consumed at the replay gate inside the cold-start branch so a warm
turn cannot spend it, and the route threads it from an optional `replay` field on
the request body. The default is `True`, which keeps every existing caller and
the dashboard's own copy ("Conversation history is preserved — your next message
starts a fresh process") true. Only the RE-INJECTION is suppressed: the
transcript is untouched, so the conversation stays readable in the dashboard and
on disk.

The flag cannot live on the session object the way `needs_context_reinjection`
does, because `discard_conversation` POPS that session — the decision is made by
the turn that tears the conversation down and acted on by the next turn, which
builds a new one. It is therefore a manager-level set, process-scoped on purpose:
a gateway restart also cold-starts the session, but there the replay is
legitimate, since nobody asked for a fresh conversation and re-anchoring is what
that surface has always done. Every teardown path that already clears the
compaction cooldown clears it too (`reset`, `remove`,
`retire_kiro_identity_sessions`, `remove_if_unclaimed`, `destroy`, and
`close_all`), because slot keys ARE reused and a leaked flag would starve the
NEXT holder of that key of its re-anchor.

Slot-key reuse is also why the dashboard close/teardown path re-checks identity
after it pops the slot. Both `close_slot` (shared by `api_chat_slot_delete` and
session-control's `close_target`) and `api_chat_slots_cleanup` pop `name` out of
`state._slots` and then run several AWAITS — cancel the task,
`save_slot_off_loop(..., closed=True)`, `sessions.remove(_history_key_for(name))`.
Across that window a concurrent same-key recreate (a `POST /api/chat`, or the
`session_close` MCP verb) can mint a REPLACEMENT slot under the same key, reusing
the same history key and the same session. Because both sites still hold the
popped object (`slot` / `removed`), a synchronous, race-free discriminator is
available, and there are TWO of them because the destructive steps do not all
answer to the same owner.

`_slot_still_ours(state, name, <popped>)` is the KEY-scoped one. It asks whether a
DIFFERENT object now owns the key — an absent key is the ordinary post-pop state of
every close, so `None` counts as still ours; reading it as "our object owns the key"
would make the guard fire on every close and skip the very teardown it guards. It
governs the two steps whose resource IS the key: `sessions.remove` (whose argument
is `_history_key_for(name)`, the session an unbound replacement runs on) and the
failure arms' restores (`state._slots[name] = slot` / `= removed`), which run only
when the key is still free or still the popped object and so never clobber a live
replacement. Cleanup's `archived` report is key-scoped too, and stays key-scoped:
it names slot keys, so a key with a live holder is never listed however its
transcript ended up.

`_replacement_shares_transcript(state, name, <popped>)` is the TRANSCRIPT-scoped
one, and it is what governs the `closed=True` save — because that save's argument
is not the key. It targets `slot_history_key(slot)`, so a slot carrying a
`linked_session_key` (channel-, cron- or workflow-born) writes the LINKED
transcript while a replacement minted by a plain `get_or_create_slot(name)` — what
`POST /api/chat` and the `session_close` verb take — is unbound and writes
`dashboard:{name}`. Same key, two files, so key identity cannot decide this step:
yielding the archive to a replacement that shares nothing would leave the
original's own transcript with no `closed` flag, and `channel_slots._close_stands`
reads an absent flag as "the user never dismissed this", so the reconcile pass
resurfaces the tab that was closed. The predicate therefore compares FILE identity
(`transcript_stems` on both sides) rather than key strings: `history._safe_key`
folds `slack:<ts>` and the `slack_<ts>` stem onto one `.jsonl` and a pre-migration
thread still resolves to its bare `thread_ts` stem, and the two errors are not
symmetric — over-reporting "shared" merely declines an archive the next close will
make, while under-reporting stamps `closed` on a file a live slot is writing.

When the replacement DOES share the transcript, the archive and the session
teardown are both skipped. The original was already popped and cancelled, so the
close is effectively complete for it: `close_slot` RETURNS rather than raising
`SlotCloseError`, which is what makes both its callers report success, and cleanup
takes its `continue` without counting the key archived. When it does NOT share the
transcript the archive runs normally on the original's own file and only the
key-scoped steps yield.

The `note_slot_closed` tombstone (below) still fires before these awaits for the
reconcile reader; it is NOT the vehicle for either guard, which are pure post-pop
re-checks confined to the two teardown paths.

What both guards cover is the WIDE window, not the durable write. `save_slot_off_loop`
reaches its commit through the process-wide default executor, so a recreate can
still land between the last synchronous check and the in-lock write, leaving
`closed=True` on a key a live replacement holds. That residual is what an unguarded
close carries as well, and the row is the same one a plain sequential
close-then-reopen of a reused key produces: `closed`/`closed_at` are in
`SLOT_OWNED_META_KEYS`, so the replacement's next full save drops them, and
`api_chat_slot_resume` compensates a stale flag with an in-lock compare-and-clear
(`clear_closed(..., only_if_closed_before=...)`). Closing it AT the commit needs an
ownership predicate evaluated inside `_locked(history_key)` on the write AND on the
resume's read-then-clear — a durable-metadata contract change rather than a
loop-side ordering one, so it is deliberately not what these two teardown paths do.

Yielding to a replacement carries four obligations, and they exist because the
state a close compensates is not all scoped the same way.

- **Key-scoped state moves with the key.** `state._restricted_keys` holds
  `dashboard:{name}` — a SESSION KEY, not a slot identity — and
  `_is_restricted_session` tests that set BEFORE it looks at the slot. So every exit
  of either teardown owes one postcondition, which is what
  `_resettle_restricted_key(state, name)` IS: the key is marked iff the slot
  currently AT `name` is restricted, an absent key counting as unrestricted. All six
  exits go through it rather than through a bare `discard` — the ordinary close
  (where the key is gone, so the marker drops and the next holder is not starved),
  and every exit that hands the key to a replacement (where it is re-derived from
  the REPLACEMENT). Re-derived, never blindly discarded: a replacement that is
  itself restricted keeps the marker, since dropping it is the fail-OPEN direction.
  Skipping it on a hand-over gives a persistent replacement an incognito original's
  403 on every memory, artifact and mcp-apps call for as long as that tab lives.
- **Slot-scoped compensation is coupled to the restore.** The failure arms owe the
  ORIGINAL two rollbacks, and both are conditional on the original getting its key
  back — not on the original merely existing. The nudge loop already is, through
  `_restore_slot_nudge_loop`'s own `state.get_slot(name) is slot` admission check.
  `notify_slot_close_undone` is coupled the same way rather than gated on
  `slot._app` alone: with a replacement on the key there is no tab to put back, so
  the dismissal DID happen for the original, and resuming the app's worker re-arms
  an autonomous crew whose `slot_key` its watchdog resolves with a bare
  `state.get_slot(...)` and no ownership test — handing the auto-approve grant, and
  then an unbounded nudge clock, to the user-owned replacement. Leaving the pause is
  the same answer the pre-save guard gives from the identical state, and it is a
  first-class visible one (a `paused_reason` row with a resume control). The bulk
  archive has no sibling here: it deliberately never calls `notify_slot_closed`, so
  it has no app dismissal to take back.
- **The original's unpersisted content is owed to its transcript, not to the
  original's slot object.** A hand-over exit stops referencing the popped
  slot, and `_flush_dirty_slots` iterates exactly `state._slots`, so an
  unreferenced slot has NO retry path: anything past its last commit —
  `messages[_disk_window_len:]`, plus a note the bulk path is still holding in
  `_deferred_notes` — would simply cease to exist from this gateway's own
  delivery paths. (Since #4093 a held note also has a durable copy in the
  slot's metadata line, so a dropped hold is re-delivered after the NEXT
  restart rather than lost outright — but deferring an acknowledged note to a
  hypothetical future restart is not delivery, so the hand-over drain below
  is still what honors it in this lifetime. One version-skew caveat: the
  retirement invariant holds only for gateways that stamp `meta.noteId` on
  delivered rows. An older gateway carries `deferred_notes` as unowned
  metadata, its flush stamps no id and its save retires nothing, so a
  downgrade-deliver-reupgrade cycle replays already-delivered notes as
  duplicates — bounded harm, and the chosen at-least-once direction, but the
  invariant silently does not hold across versions.) The pre-save
  exits need no store failure to reach it either; they return before the save is
  attempted, in a window that opens while a turn is in flight. So every hand-over
  exit routes through `_persist_handover_tail(state, name, slot)`, which flushes
  held notes into the window and writes it with **`closed=False`**. Those rows
  belong on the ORIGINAL's own transcript whether or not the replacement shares it;
  what must not happen is the archive flag and the session teardown, not the write.
  The
  target is `slot_history_key(slot)` and never a derived `dashboard:<slot>` — the
  forced save resolves its own target the same way and REFUSES a write whose
  `expected_history_key` names a different transcript, so the derived form would
  make the drain a silent no-op for every cron-, channel- or workflow-linked slot
  and would name a row-less file in the failure log. The write is non-destructive
  against the replacement's rows in both directions: `_save_slot_to_history`'s
  foreign-append scan carries through every on-disk line the saved window does not
  represent, so rows a replacement already committed survive. The METADATA line is
  a different matter and is not the drain's to move, so the write is `rows_only`.
  `_save_slot_to_history` is otherwise authoritative for `SLOT_OWNED_META_KEYS` and
  REBUILDS that line from whichever slot it is handed, so a default save here would
  revert a title, folder, tag set or pin the replacement had already published onto
  the shared transcript (`POST /api/chat/slots` persists a folder and a pinned title
  at birth) — silently undoing an acknowledged edit, and for a tab nobody types in
  again undoing it for good, so the next restart resurrects the dismissed tab's name
  and filing. `rows_only` keeps the on-disk value for every one of those fields and
  narrows this write's ownership to `ROWS_ONLY_OWNED_META_KEYS`: the file's identity
  and accounting, which every writer maintains and which the save carries forward
  from disk anyway. The set it defers,
  `ROWS_ONLY_DEFERRED_META_KEYS`, is named in full rather than derived as
  `SLOT_OWNED_META_KEYS - ROWS_ONLY_OWNED_META_KEYS`, because that difference
  under-approximates: the slot save also writes fields that DESCRIBE an owned one
  without being owned themselves, and a title's provenance and refresh budget
  (`title_origin`, `title_refresh_mark`) travel WITH the title rather than with the
  writer. Deferring the title while keeping those commits a line matching neither
  slot — read back beside another slot's title they either unlock the background
  refresh on a name the user typed by hand or lock a generated name out of refresh
  permanently — so they are deferred with it. `created_by` and `origin` are the same
  shape with AUTHORIZATION rather than presentation behind them, so they are deferred
  too: `created_by` is what session-control's member ownership boundary reads and is
  meaningless without the `mode` deferred beside it, and `origin` must round-trip with
  the deferred `app` because the pair decides `slots:user` visibility and the
  unattended approval window. Both describe the SLOT, so on a transcript with a live
  holder the holder's are the true ones — and deferring them fails CLOSED on a line
  that carries neither, since an absent `created_by` denies and an absent `origin`
  restores to the sentinel the rehydrate paths already treat as unattributed. The
  conversation's own MONOTONE once-flags (`auto_tagged`, `human_seen`,
  `channel_origin`, `channel_folder_filed`) are set and never cleared, so two writers
  on one transcript cannot disagree about them in a way that outlives the pair; they
  stay as written. Deferring to disk is deliberately not
  the same as deriving the line from the replacement — a recreate that published
  nothing has no metadata to protect, and re-deriving from it would ERASE a real
  title and filing the two slots' shared conversation has; leaving the line alone is
  what gets both directions right with one write. **The deferral is conditional on
  there actually being another writer to defer to**, and the line's `tab_id` — minted
  per slot object, stamped by every save — is the evidence: the flag holds fields
  back only on a line ANOTHER slot published, and a line this slot published itself
  (or no line at all) takes the ordinary rebuild. Without that test the flag would
  cost the original its own uncommitted metadata: a rename, re-file, tag or pin is
  acknowledged the instant it lands in memory and persists on a later `_dirty`
  flush, and the drain runs past the pop, where no flush will ever visit that slot
  again. The two errors are not symmetric, so unprovable ownership defers: a
  deferred edit of this slot's was never committed, while a rebuild over a live
  holder's line reverts what it already published and nothing rewrites that for a
  replacement nobody types in again. `closed`/`closed_at` are deferred on the same
  asymmetry, and the drain is open-shaped without being un-closing: on another
  holder's line a `closed` flag is that holder's own DISMISSAL, so erasing it would
  resurface a tab the user put away — permanently, since the holder that wrote it is
  popped too — and re-arm the channel reconciler on it, while leaving a stale flag
  costs nothing durable, because the live holder owns those keys on its next full
  save. The only path that clears a stale flag from outside the holder is the resume
  route, and it clears one only when it can prove the close predates its own
  boundary (`clear_closed(..., only_if_closed_before=...)`, compared inside the
  store's lock), for exactly this reason: an unconditional clear reopens a
  replacement the user closed. A rows-only save carries no such boundary, so
  clearing a stale flag is instead the job of the `tab_id` fallback above: on a line THIS slot
  published there is no other holder's dismissal to lose, so the ordinary rebuild
  runs and the open-shaped write erases it. The failure arms take the same route in place of the
  restore they skip: a store that rejected the `closed=True` write can still
  accept the next one, and a lock lost to the recreate is exactly that case.
- **A drain that fails is reported, not swallowed.** `_persist_handover_tail`
  returns a named result: `rows_committed` says whether rows were owed and
  reached disk, `prompts_lost` counts the durable-eligible queued prompts whose
  only copy dies with the popped slot. The result is a tuple and therefore
  always truthy, so callers read `rows_committed` rather than testing the
  result itself — and every caller honours it, because this frame is the last
  reference to those rows, so nothing will retry and
  nothing else will ever report them. Both PRE-SAVE hand-over exits therefore turn
  a failed commit into their path's own failure: `close_slot` raises
  `SlotCloseError(code="history_save_failed")` (the same code as an ordinary failed
  archive — from the caller's side it is one thing, a close whose history write did
  not land) and cleanup adds the key to `failed`. There is nothing to roll back on
  either exit, so the report IS the whole remedy; a 200 there would claim durability
  the close does not have. The two FAILURE-arm drains need no branch of their own:
  those arms already end in `SlotCloseError` / `failed.append(name)`, so a lost tail
  reaches the caller regardless, and the drain only decides whether the rows
  survived. Every failure is also logged with the exact row count, which is the only
  report anything in the process can still make about the rows themselves. A
  non-zero `prompts_lost` additionally posts a dashboard notification naming the
  slot and the count — never the prompt text, which may belong to a restricted
  session — because the gateway log is not reachable by the person whose words
  were dropped. Survival is judged by who writes the durable line next, not by
  the slot's own persistence signature: a live transcript-sharing holder
  rebuilds the shared line on its every full save, so an owed entry survives a
  hand-over only when that holder's own queue carries it (a rehydrated holder
  restores the entries as queue cards; a fresh recreate does not), while with
  no live sharing holder the line is at rest and answers directly. An owed
  entry with no durable future is counted lost on every exit — including the
  no-write one.

Three properties the route holds, each of which fails silently if broken:

- The key comes from `effective_session_key(slot)`, never a derived
  `dashboard:<slot>`: a channel-born slot's turns run on the channel's session,
  and the derived form yields a key no session ever had — the clear finds nothing
  and the call still reports success.
- `discard_conversation`, never `destroy`: the entry carries the Slack
  thread/channel linkage and the reverse index built from it.
- It is nonetheless a FULL teardown (provider shutdown plus
  `release_subagent_runtime`), so it takes the same guards the sibling `reload`
  route does, through the same shared helpers rather than a third policy:
  `_app_cancel_denied` on the resolved SESSION key, `provider.has_active_turn()`,
  `slot.running` widened with `slot._in_stage_execution`, and
  `_subagents_attached_response`. Each protects work invisible from outside — a
  turn on the session with no dashboard task behind it (an inbound channel
  message, which `slot.running` cannot see), a turn mid-write, a plan between
  stages, and children still running after their parent's turn ended.
  The four probes above are best-effort fast paths; the authoritative guard is
  the fifth, `discard_conversation(..., skip_if_busy=True)`, which probes the
  per-session SEMAPHORE atomically with the session pop and refuses with the
  same `turn_in_flight` 409. It closes the edge the fast paths share: a turn
  holding the semaphore before its prompt is in flight is invisible to
  `has_active_turn`. This is the same contract the sibling reload route rests
  on, so the two teardowns keep one notion of "busy". Of the route's refusal
  paths, only the atomic one emits a SEL `denied` record — it is the sole
  refusal that occurs after the route has committed to the teardown; the
  fast-path 409s are pre-checks and stay unlogged, as they are on the sibling.

Authorization is `_app_cancel_denied`, not a slot-ownership check, and that
distinction is load-bearing: `get_or_create_slot` resolves `linked_session_key`
from the session map for a name shaped like a channel stem, so an app that names a
live channel thread ends up OWNING a slot bound to a conversation it has no claim
on. Ownership alone would let it wipe that channel conversation's resume pointer.
The helper tests the key the caller will actually act on, and runs BEFORE the 409s
so a refusal cannot confirm the slot exists. Reaching the route needs
`/api/chat/slots` in the app's manifest `permissions.api`, and the capability it
grants is strictly smaller than the delete it already implies.

The transcript is deliberately left in place, so the tab still shows earlier
messages the model no longer remembers. That is the honest rendering — the record
is the user's, the context was the conversation's — and it is why this is an
explicit request rather than something the gateway does on its own.

### Load Recovery (stale native session lock — F2)

On restart / Make-Live cutover the previous gateway's kiro-cli is killed. If it
died uncleanly (SIGKILL, crash, OOM, or a drain timeout), its per-session lock
can stay held briefly, so the new gateway's `session/load` is rejected with an
**"active in another process"** error. The dashboard's hard-stop path has a
second shape of the same race: `stop_turn` resets the session and eagerly
respawns it, and kiro-cli's `session/load` in the new holder creates its lock
and re-reads it to confirm ownership — if the killed holder's exit handler
unlinks the same path in that window the load fails with **"failed to re-read
lock file ...: No such file or directory"**. Both are transient
(`_RESUME_TRANSIENT_LOCK_MARKERS`: `"active in another process"`, `"re-read lock
file"` — deliberately not a bare `"lock file"`, so a permanent failure such as
`Permission denied` on the lock path still fails fast to Phase 2)
and recovery happens at the resume
chokepoint (`AcpProvider._load_session_with_retry`, `providers/acp.py`) and
self-heals regardless of *why* the resume failed — it never depends on the dead
holder cooperating (unlike cooperative drain), so it covers every kill mode:

1. **Phase 1 — bounded retry (lossless).** Re-issue `session/load` up to
   `_RESUME_MAX_ATTEMPTS` (4) times with exponential backoff
   (`_RESUME_BACKOFF_BASE_S` → 1s, 2s, 4s). If the lock clears, the
   session resumes with full native history. A genuine (non-lock) load error is
   **not** retried, and a dead runtime aborts the loop immediately (the caller's
   respawn path takes over).
2. **Phase 2 — fresh session + history replay (backstop).** If the lock never
   clears, `_start_kiro_runtime_impl` falls through to a fresh `session/new` and
   sets `AcpProvider._history_replay_needed`. `get_or_create` reads that flag and
   sets `_Session.provider_switch_replay = True`, so `build_session_replay`
   injects KiroCrew's `conversation_log` into the new native session on the first
   prompt (the same replay path used for cross-provider switches). The slot
   resumes seamlessly instead of returning empty completions.

Observability: a successful Phase-1 recovery logs at INFO; exhausting all
attempts logs a single grep-able WARNING before migrating to Phase 2.

### Cross-Provider Continuity

kiro session IDs and the removed provider's session IDs are NOT interchangeable:
- kiro: arbitrary string, stored in `~/.kiro/sessions/cli/<sid>.{json,jsonl}`
- removed provider: UUID v4, stored in `~/.claude/projects/<encoded-cwd>/<sid>.jsonl`

When a user switches provider mid-session (e.g. config change from `acp` to
`claude_code`), conversation continuity is maintained via **history replay**,
never via session_id translation.

**Detection:** `detect_provider_switch(session_map, key, new_provider)` in
`session.py` compares the stored provider against the new one. Returns True
when a switch is detected (stored SID exists AND providers differ).

**Behavior on switch:**
1. `resume_sid` is discarded (not passed to the new provider process)
2. `SessionMap.clear_sid(key)` removes the stale SID from persistent state
3. `_Session.provider_switch_replay = True` flags the session for replay
4. The new provider's session_id (once obtained) is saved with the correct
   provider label
5. On the first prompt after the switch, `chat_runner` detects the flag and
   injects history from `compress_thread_history()` (Kiro Crew's conversation_log)
6. The flag remains armed through prompt acceptance and is settled only when the
   replay-bearing turn lands. ACP providers promote a deliberately deferred fresh
   SID before consuming the lease; non-ACP providers already published their SID
   during allocation and consume the lease directly. Cancelled, failed, empty, or
   synthetic terminals leave it armed for the next prompt.

**Replayed content carries no image reference.** `_replay_rows` and
`_recall_rows` — the two row builders behind every history vehicle
(`build_session_replay`, the thread-history fallback in `build_session_context`,
and the transcript `compress_thread_history` hands to the LLM compressor) — hand
each row out through
`kiro_crew.image_refs.strip_image_refs`, which replaces every local image
reference with `[image not carried into this context]`. Markdown references go
through the attachment store's own `iter_local_refs`; bare paths go through the
inliner's own `_PATH_RE`, narrowed to paths outside code spans that stand alone
rather than sit inside a URL query, because the inliner rewrites text only after
reading a file and an unconditional substitution would corrupt a URL or a code
snippet instead of scrubbing it. A row's picture belonged
to an earlier turn and a text vehicle cannot carry bytes, so the reference is
the only thing that would arrive, and both readings of it are wrong: while the
file is still readable `build_prompt_blocks` re-inlines it (a picture an earlier
compaction already dropped returns at full byte cost on every later cold start),
and once the file is gone the path is left in the prose next to the assistant's
own earlier description of what it showed. Stripping at the row builders rather
than at each consumer is what makes the guarantee hold for all three. The
CURRENT turn is unaffected — it is excluded from the replay by identity, so a
freshly attached image still becomes a real image block.

**Same-provider resume:** unaffected. Normal `session/load` path with full
native fidelity.

**Audit:** A `provider_switch_detected` SEL event is emitted with both the
stored and new provider names for observability.

**Atomic write:** tmp file + `os.replace()` prevents corruption on crash.

**Deferred flush (event loop only):** a mutation made on the event loop marks
the map dirty and schedules a debounced flush task; the task serializes the map
under `_MAP_LOCK` into an immutable JSON payload, then performs the tmp+rename
in a worker thread — the loop never pays the file write inline, and `_data`
never crosses the thread boundary. Coalescing never drops a trailing mutation
(the task loops until it observes a clean map), and a per-snapshot ticket keeps
a slow in-flight write from landing an older map over a newer forced one.
`SessionMap.flush()` (sync contexts) and `SessionMap.aflush()` (awaited) are the
deterministic durability points. `SessionMap.aclose()` is the shutdown boundary
used by `SessionManager.close_all()`: it cancels and awaits the registered
debounce task, preserves an unstarted or claimed-but-unwritten snapshot, lands
it through `aflush()`, and returns only after the task registration is retired.
Off the loop (CLI, tests, worker threads) every mutation still writes inline. Losing a pending
flush on a crash leaves a well-formed older map, never a truncated file.

**Auto-prune:** `SessionMap.get()` auto-removes entries whose `.json` file
no longer exists (the entry drops from memory immediately; the file write rides
the deferred flush). `SessionMap.prune()` bulk-removes all stale entries at
startup.

**Mapped-session enumeration:** `SessionMap.mapped_sids_by_key()` returns session
key → kiro-cli session ID for every entry that has one. Disk accounting
([session-storage](session-storage.md)) needs both halves of that relation: the IDs
to exclude from reclaiming (a mapped session is resumable), and the key each ID
belongs to so a session's transcript can be paired with its replay log. Returning
the mapping rather than only the ID set is what lets a caller reclaim a session
whole instead of leaving one half behind.

**Dashboard history key round-trip:** Session keys use `:` (e.g.
`dashboard:chat-1-xxx`) but JSONL filenames use `_safe_key()` which replaces
`:` with `_`. When a session is resumed from history, the slot name comes from
the filename stem (`dashboard_chat-1-xxx`), producing session key
`dashboard:dashboard_chat-1-xxx`. `SessionMap.get()` handles this by falling
back to the canonical form (`dashboard:chat-1-xxx`) when the direct lookup
fails.

**Slot-key filename normalization:** `get_or_create_slot()` folds every
caller-provided slot name to the `_safe_key()` filename charset
(`[A-Za-z0-9_\-.]`, via `_normalize_slot_key()` — `dashboard:`/`dashboard_`
transport-prefix strip mirroring `_history_key_for()`, then ASCII fold, then
filename fold), so a slot key always equals its persisted filename stem. Without this,
display-style slot names (e.g. `Artifact: My Doc` from the artifact iterate
flow) diverged from their sanitized filename: after a gateway restart,
`restore_open_slots()` rehydrated the raw key from `open_slots.json` while
`restore_recent_sessions()` derived a second slot from the filename stem,
producing duplicate sidebar sessions backed by one transcript.
`restore_open_slots()` and `_rehydrate_slot_from_history()` apply the same
fold on read so pre-fix snapshots carrying both key forms self-heal (the
second form hits the dedup guard). When normalization changes the name, the
original pretty form is preserved as the slot's initial title
(redaction-scrubbed, non-pinned so auto-title can still override).

**Permanent history deletion keeps ownership exact.**
`DELETE /api/sessions/{key}` unlinks the selected transcript first. History
aliases may locate a slot candidate, but they do not prove ownership. Before the
unlink awaits, the route captures the slot object, its transcript key, its
SessionManager key, and the key's monotonic ownership generation. Legacy Slack
aliases can name either a canonical or pre-migration bare file, so the off-loop
delete worker resolves the selected history key and captured slot transcript to
the filename each one actually uses while holding that transcript's cross-process
lock set. Slack deletion, restore, and ordinary transcript writers all use
`ConversationLog.locked_stems` to take canonical `slack_<ts>` and legacy `<ts>`
locks in sorted order; writers resolve their physical target only after that set
is held, so none can publish through an alias the others did not serialize. A
path mismatch rejects the candidate.

Deferred cleanup compares object identity, task identity, current transcript and
SessionManager routing, and the current manager generation with the immutable
claim in the same yield-free span as the pop. Cron, workflow, and channel
adoption can relink an existing slot object; a rerouted object is preserved even
though its identity is unchanged. A new turn on the same route changes its task
or generation and is preserved too. A captured absence never claims a later
successor.

Every `get_or_create` reserves its logical key under the registry lock before
resume lookup or provider startup. Reservation publication/removal and every
session registration/removal — including provider-reload and shutdown mass
clears — advance the generation shared by canonical and legacy Slack aliases.
Successful claims remove their token synchronously before returning, in the
same yield-free span that owns the acquired lease. Failure and cancellation
drain token removal under the registry lock. `destroy_if` requires the captured
generation to remain current, requires no reservation and an idle session, then
evaluates current live-slot ownership under the same manager lock as the
provider pop. The session-map entry is deleted before the awaited end metric.
History deletion uses the explicit override-preserving mode; ordinary destroy
continues to clear the old conversation's threshold.

Chat pins, work ledgers, and per-session autocompact overrides are preserved.
They are independent stores that can be claimed by a transcript created or
restored in another process after any owner scan or in-process epoch check.
Making their deletion atomic would require every cross-process transcript writer
and restore path to share one mutation protocol with in-memory dashboard state.
The request path chooses the smaller fail-safe rule instead: stale sidecars are
reversible, while deleting a successor's state is not.

## Slack Thread Linking

Sessions can be linked to Slack threads via `SessionMap` fields
`slack_thread_ts` and `slack_channel_id`. This enables bidirectional sync
between dashboard chat and Slack. Slack is the legacy special case: other
channels link through the generic ChannelLink mirror map (see
[messaging.md](messaging.md)). The `slack_*` fields are retained for backward
compatibility.

**API:**
- `SessionManager.set_slack_link(key, thread_ts, channel_id)` — persists to session map
- `SessionManager.get_slack_link(key) -> (thread_ts | None, channel_id | None)`
- `SessionManager.get_session_for_thread(thread_ts) -> key | None` — reverse lookup,
  keyed by the **bare** Slack `thread_ts`; returns the linked session key
  (canonical `slack:<ts>` for self-linked Slack threads, `dashboard:chat-N`
  for dashboard-linked threads)
- `SessionManager.set_channel(key, channel_id)` — backward-compat alias

**Slack handler:** calls `set_slack_link(session_key, reply_ts, channel)`
(where `reply_ts` is the bare Slack thread_ts and `session_key` is the
canonical `slack:<ts>` form) outside the `if is_new` guard so every message
refreshes the link.

## Slack Session-Key Alias Fold

Slack thread sessions have two historical key forms: the legacy bare
`thread_ts` (`"1783733803.877979"`) and the canonical namespaced form
(`"slack:1783733803.877979"`, `messaging/link.py`). The Slack handler derives
the canonical form at message entry (`canonical_key(thread_ts or msg_ts)`),
but legacy callers and persisted state may still present bare keys.

`SessionManager._fold_key(key)` resolves the two alias forms onto whichever
form is live in the in-memory registry (exact match → canonical alias →
legacy bare alias; unknown keys pass through unchanged, so non-Slack
namespaces are never rewritten). Every public key-taking method
(`get_or_create`, `has_session`, `get_provider`, `get_pid`, `release`,
`stop_turn`, `enqueue`/`dequeue`/queue helpers, `reset`, `remove`, `destroy`,
approval-policy accessors, `record_success`/`record_failure`,
`check_context_usage`, `cancel_current`, `is_provider_alive`) folds at entry.

Without the fold, the thread-index lookup (which returns canonical keys) and
a live session registered under the bare key disagree, so the second
in-thread message misses the live session, the disk resume is rejected by
kiro-cli ("Session is active in another process"), and a brand-new
context-free session silently splits the thread.

`ConversationLog._path()` applies the same back-compat: a canonical key whose
file doesn't exist yet falls back to the legacy bare-`thread_ts` filename
when that exists, so a thread active across the migration keeps one log file.

**Dashboard chat:** mirrors user messages to linked Slack threads via
`slack_client.post_message()`. The "Send to Slack" button (`slack/blocks.py`)
opens a DM thread, links the session, and posts the last 5 messages as context.

**Dashboard state:** `ChatSlot.summary()` includes `slack_linked: bool` so
the frontend can show a link indicator. `_ChatSlot.task` publishes ownership through a
property that increments `_turn_generation` for every new non-null task. The counter is
process-local and monotonic for the slot lifetime; unlike `task`, normal turn teardown
does not clear it, so code spanning an await can detect a turn that started and finished
inside that interval.

**Slash commands** (`slack/events.py`):
- `/kirocrew sessions` — lists active sessions with Slack link status
- `/kirocrew sessions resume <key>` — resumes a session in the current thread

**Block Kit builders** (`slack/blocks.py`): reusable Block Kit dict builders
for slash command UIs. Action IDs follow `mc_<command>_<action>[_<id>]`.

## DM Channel Session Keys & Mid-Turn Handling

DM channels (Telegram, WeCom) have no thread concept, so `messaging/link.py`
derives the session key with `build_dm_session_key(channel, agent, user, *,
gen, dm_scope)`:

- **Shape** (channel-first): `{channel}:{agent}:{chatType}:{user}` plus an
  optional `:gen{N}` suffix. The part before the suffix is a durable **bucket**
  (history and channel links hang off it); the **generation** rotates to start a
  fresh transcript within the bucket. `chatType` is `direct` today; `group` is
  reserved.
- **`dm_scope`** (`MessagingConfig.dm_scope`): `per-channel-peer` (default) —
  one bucket per `(channel, user)`; `unified` — all DMs collapse into a single
  `unified:{agent}` bucket for cross-surface continuity. `agent` is part of the
  bucket by design, so switching the configured agent starts a fresh session
  rather than replaying another agent's context.
- **Generation reset** rotates on `/new`, an idle window
  (`MessagingConfig.idle_reset_minutes`), or a daily boundary
  (`daily_reset_hour`), decided by `should_rotate_generation()`.
- **Explicit `/new` is durable on every DM channel.** Discord, Telegram, Teams,
  Webex, Feishu, iMessage, WhatsApp, Weixin and WeCom persist the new generation as
  a monotonic floor on the stable `SessionMap` bucket and await its flush before
  replying. A failed floor write leaves the in-memory bump intact but adds a
  restart-safety warning. A zero-turn generation creates no conversation-log row:
  it holds no work to recover, and repeated `/new` calls therefore update one floor
  integer instead of crowding the newest-first picker with empty placeholders. The
  first normal turn creates the real history row. Automatic idle/daily rotation still
  materializes only when its first real turn runs.
- **Restart-safe generation seeding.** The generation counter is in-memory (per
  `ConversationState`), so it resets on gateway restart. To stop `/new` from
  bumping a reset counter (0→1) straight onto a still-persisted generation and
  resurrecting that old conversation, the counter is seeded on first access to a
  bucket from the highest mapped generation or explicit-new floor via
  `SessionMap.max_generation(bucket)` (shared helper
  `messaging.link.seed_generation`, used by every DM dispatcher). A normal
  post-restart message then resumes the latest generation (continuity); `/new`
  always advances past every persisted generation, minting a genuinely fresh sid.

Legacy bare-thread Slack keys are unaffected — they keep the
`canonical_key`/`legacy_key` shim. The DM channels are recent, so the key shape
carries no prior persisted history to migrate.

### Mid-turn messages (steer / queue)

`SessionManager.is_busy(key)` reports whether a turn holds the session
semaphore. When a DM arrives mid-turn, the dispatcher acts on
`MessagingConfig.queue_mode`:

- `steer` (default): fold the message into the running turn via the provider's
  steer channel.
- `queue`: enqueue it — checked atomically against the semaphore, so a turn
  that finishes in the window runs the message instead of stranding it — and
  drain it after the turn, iteratively and capped (not recursively).

WeCom always steers regardless of `queue_mode`: its replies are bound to the
inbound request, so a queued-then-drained reply can't be delivered later
(capability-driven, like `supports_proactive_send=False`).

### Queued prompt durability

A prompt admitted while the slot is busy is answered `{"ok": true, "queued":
true}` and held in `slot._queue`. Its transcript row is written by the DRAIN,
not by the enqueue, so the queue is the only record until it runs — and a
gateway restart in that window (an auto-update, a watchdog exit) used to drop
the prompt with no row, no error card, and an empty queue on reload.

The slot save therefore persists it. `slot.durable_queue_entries()`
(`slot_queue_repository.durable_queue_entries`) selects the entries and the
metadata line carries them as `queued_prompts`, a `SLOT_OWNED_META_KEYS` field
so absence clears it.

- **The accept STARTS the write, it does not wait for the interval.**
  `queue_for_next_turn` hands `flush_slot_now` to the executor
  (`start_queue_persist`) so the residual loss window is one save's duration
  rather than one flush interval. BOTH accept sites use it: the busy-slot path
  and `chat_handlers`' sub-agent hold branch, which holds an IDLE slot where no
  drain is coming and the wait for the last sub-agent is unbounded. Each accepts
  onto the same queue under the same ceilings, so each starts the write.
  Started, not awaited: the acknowledgment keeps
  its existing meaning — accepted in memory, durable on a flush — because making
  durability a precondition would refuse a queued send on a slow or failing disk,
  taking the user's words away at the one moment they cannot be re-read from the
  transcript. A failed background write is logged and stays owed to the periodic
  flush; with no running loop (a synchronous caller) the flush owns it as before.
  **One writer per slot.** Two immediate writers would snapshot independently and
  the transcript's file lock orders their commits, not their reads, so the older
  snapshot could land last and put back a value missing an acknowledged prompt.
  A send arriving mid-write records the debt (`_queue_persist_owed`) and the
  finishing writer runs one follow-up pass when the queue still differs from disk.
  That flag gates only the writers it starts, so the save closes the rest: inside
  the history lock it compares the slot's committed-queue witness
  (`_queue_persisted_sig`) against the value held when it read the queue, and
  refuses the pass when another writer committed in between. Only a committed
  value refuses — a queue that merely moved is an ordinary consistent past state —
  and a refused pass leaves the queue owed rather than dropped, so losing the race
  costs one flush interval of lag instead of an acknowledged prompt. A rows-only
  write over another holder's line is exempt, since it defers the key instead of
  deciding it. Both refusals this adds mark the slot dirty on the way out, because
  `flush_slot_now` clears `_dirty` on any return that did not raise and compares
  `_dirty_gen` to spot a concurrent mark: without it a refused pass would drop
  window rows it never wrote, and the queue signal cannot cover them once the
  overtaking writer has satisfied it.

- **Only a plain user prompt is durable.** An entry carrying a `kind` is an
  injection whose producer is gone (a cron notification names an event, and a
  restart is not that event happening again); an entry carrying a `payload` is a
  synthetic recovery continuation; an entry carrying `_on_consumed` /
  `_on_irreversibly_consumed` acknowledges an automatic payload through a
  callback that does not survive the process. `meta` rides along verbatim,
  because it holds the admission-time containment snapshot the drain
  re-validates against and an entry without one fails closed.
- **Provenance does NOT survive the restart, and that is a security property.**
  `_directive_user_origin` / `_directive_channel_origin` record that an entry's
  words came from an authenticated human, and the drain reduces the consumed
  entries' flags into `producer_is_user_facing`, which admits user-surface and
  self-arming directives and exempts them from the LINKED containment
  constraint. The metadata line is an ordinary readable-writable file in the
  crew home, not a write-protected one, so a flag read back from it is
  indistinguishable from one a prompt-injected agent's shell wrote — authority
  granted to whoever can write the file. Neither half of the round trip moves
  it: the keys are absent from `_DURABLE_QUEUE_KEYS` so the writer never emits
  them, and `sanitize_restored_queue` drops a hand-added one, so a restored
  entry is non-directive by construction.
- **Correctness rests on the window and the queue being ONE observation.** The
  drain removes the entry and appends its row in the same event-loop step, and
  the save runs in the flush executor thread, so the save takes the pair under
  one consistency generation: read the queue, snapshot the window, read the
  queue again, retry while the two disagree, and REFUSE the save (nothing
  written, the entry still owed) when no pair is proven inside the budget. Read
  separately, the two halves could commit a file showing neither the entry nor
  its row. A crash between the drain and the save loses the row as well, so a
  replayed entry is a prompt the transcript never recorded — never a second copy
  of one it did.
- **Restored entries are handed back as queue CARDS, not dispatched.** Nothing
  drains an idle slot on boot, so the user sends, edits or deletes them. This is
  the same rule `sendTurn.ts` follows for an indeterminate send: a prompt whose
  turn may have run un-persisted must never be auto-resent.
- **Durability does not depend on the mutation site.** `_queue_persisted_sig`
  records what the last committed save wrote and `slot.queue_persist_pending`
  compares it against the live queue, so an in-place rewrite — a reorder, a
  plan-approval filter, a force-stop clear — is picked up by the periodic flush
  without each site marking the slot dirty. The flush and the resumed-slot no-op
  guard both read it beside `_dirty`.
- **A rows-only handover save defers the key** (it is in
  `ROWS_ONLY_DEFERRED_META_KEYS`): the line describes the live holder, whose
  queue this write does not own. The popped slot keeps owing its own entries,
  which is the conservative side. `_persist_handover_tail` therefore treats an
  owed queue as work — a queued prompt changes neither the window length nor
  `_dirty`, so its "nothing owed" test would otherwise answer True over one — and
  judges each owed entry's durable future by who writes the line next: an entry
  with none is reported in every register that can still hold it — the
  warning log, the `prompts_lost` field of its returned result, and a dashboard
  notification that names the slot and the count (never the prompt text).
  Nothing in the
  process visits that slot again, the same position the held `/note` lines are in.
- Bounds: `MAX_DURABLE_QUEUE_ENTRIES` entries and `MAX_DURABLE_QUEUE_BYTES` of
  serialized value, admitted front-first because the front runs first. An
  over-budget prompt is DROPPED, never truncated — a shortened prompt replayed
  as the user's own words is worse than one reported as not carried. The SAME
  two ceilings gate the restore, costed against the SAME key projection, because
  the writer's bounds bound only what this gateway wrote and the line can be
  edited outside it, while whatever the restore admits is retained live and
  re-serialized by every later save. The projection matters: the restore adds
  `kind: ""` to keep a restored entry out of the system-injection paths, and
  billing itself for that key would make the reader's budget the smaller of the
  two, so a queue written just under the ceiling would drop its tail on the way
  back in — losing a prompt that WAS durably written.
  `MAX_DURABLE_QUEUE_SCAN` bounds how much of the raw value is INSPECTED, which
  the retention cap does not: the apply phase reading it is loop-affine, so a
  hand-edited line must not buy startup work proportional to its own length.
  Entries past it are counted as not restored, never quietly ignored.
- The send is **not refused** when the bounds cannot carry it: taking the user's
  words away at the one moment they cannot be re-read from the transcript is
  worse than a best-effort durable copy. `durable_queue_view` returns the
  persisted entries and the candidate count from ONE read of the queue, and the
  save logs their difference once, naming the slot, so the omission is an
  operational fact rather than a silent one. The pairing matters: counted from
  two reads, an ordinary send landing during a save is reported as a prompt the
  bounds refused. The ACCEPT reports it too, and earlier:
  `warn_if_not_durable` logs at WARNING when the entry just queued is one the
  write will not keep, naming the slot, the entry, the candidate count, the
  carried count and which ceiling refused it. Both the verdict and the reason are
  read off `durable_queue_entries`' own output — an entry absent from a full set
  was refused by the count cap, absent from a short one by the byte budget — so
  nothing re-implements the interacting ceilings. This is a LOG, not a receipt
  field: a caller-visible `durable` boolean on the acknowledgments has no reader,
  so it is not shipped, and the on-screen queue-card marker belongs with its
  consumer (issue #11695).

## Cross-Surface Reply Mirror

The same conversation can appear on a channel and in the
dashboard. Two models relate the surfaces:

- **Slack — one session, two surfaces (fold-in).** A linked Slack thread folds
  into the dashboard session: the handler swaps the session key to the linked
  dashboard session via `get_session_for_thread`, so there is a single backing
  sid and Slack is a projection of it (see *Slack Thread Linking*).
- **Discord / Telegram / Webex / Teams / WeCom / Weixin — two sessions, bridged by a mirror.** The channel message
  runs under its own channel session (`{channel}:…:genN` → its own sid); the
  dashboard surfaces it as a separate slot with its own sid. One logical
  conversation therefore has two backing sids, bridged by the mirror.

`messaging.link.legacy_dashboard_mirror_key(channel_session_key)` computes the
dashboard-side key: `"dashboard:" + history._safe_key(channel_session_key)`. It
MUST use the same `_safe_key` sanitizer as the slot-naming path (every non-word
char → `_`, not only `:`); a narrower sanitizer silently mismatches for keys
containing spaces/unicode, so the mirror never fires despite `/link` succeeding.

**Directions.** Inbound (channel → dashboard display) is independent of the
mirror link and always on — the channel turn writes the shared `conv_log`, which
the dashboard rehydrates as a slot. Outbound (dashboard → channel echo) fires
only when a `mirror` `ChannelLink` exists on the dashboard-side key:

```
   Messaging channel                            Dashboard tab
  ┌────────────────────┐   inbound: ALWAYS ON   ┌────────────────────┐
  │ channel session    │ ═════════════════════▶ │ dashboard slot     │
  │ …:genN  (sid A)    │                        │ dashboard:…_genN   │
  │                    │ ◀── outbound: only ──  │ (sid B)            │
  └────────────────────┘      when /link is ON   └────────────────────┘
```

**API:**
- `SessionManager.set_mirror_link(key, link)` / `clear_mirror_link(key)` /
  `get_mirror_link(key)` — persist/read the outbound `ChannelLink` (Slack routes
  to `set_slack_link` so its reverse index stays intact).
- `SessionManager.clear_mirror_links_at(link)` — value-keyed sweep: clears
  EVERY session whose mirror targets that exact non-Slack location and returns
  the cleared keys. The write counterpart of `find_mirror_sessions`, and the
  only clear that reaches a binding stranded under a key spelling the
  conversation no longer derives (a rotated DM generation, a pre-unification
  `dashboard:` row).
- `POST /api/chat/slots/{name}/mirror-link` | `mirror-unlink` — dashboard-side
  endpoints (auth posture matches `slack-link`: under the `/api/chat`
  `mixed_internal_paths` prefix, never the strict `internal_paths` set).
  New links use `{channel_type, target_id}` and resolve the opaque configured
  target server-side; the legacy `{conversation_id, thread_id?}` body remains
  accepted for compatibility. A successful new link posts an anchor plus the
  last five redacted messages before persisting the mirror.
- `POST /api/chat/slots/{name}/slack-pause` | `mirror-pause` — disconnect (or
  reconnect) a channel while **retaining** its binding, so inbound still routes
  to the same session and a later reconnect needs no re-link. Same auth posture
  as the link/unlink pair. Body `{paused: bool}`; `mirror-pause` also takes
  `{origin: bool}` naming WHICH non-Slack delivery is meant, because a session
  can hold two at once and they mute independently. Returns 409
  `mirror_not_linked` when the named delivery does not exist. The disconnect
  itself is never governance-gated (it only ever reduces egress); a denial
  silences the courtesy note posted into the conversation and keeps the
  disconnect. That note is skipped entirely for an `origin` disconnect, since the
  mirror resolver addresses the EXPLICIT mirror — a different conversation.
- **Three persisted pause markers, each keyed differently.** A mute must live and
  die with the binding the user muted, so the key follows what the flag is about:
  - `slack_paused` — the Slack thread. Cleared when the binding is REBOUND
    (different ts or channel), NOT on an identical-coordinate write: the Slack
    inbound path re-writes the same ts/channel every turn as its thread registry,
    so clearing on any write let one inbound message silently un-disconnect a
    thread.
  - `mirror_paused` — the explicit `mirror` binding. Read/written through
    `_mirror_key`, following the binding between the canonical row and the legacy
    `dashboard:` spelling.
  - `origin_paused` — the conversation the session was BORN in. Read/written on
    the CANONICAL row, never through `_mirror_key`: that helper migrates rows
    depending on where a MIRROR lives, which stranded the pause the moment a
    mirror landed on the canonical row.
  Each existence check is per flag: a born-in conversation is permanent, while an
  explicit mirror must actually exist, so a flag with nothing behind it reads as
  connected rather than reporting a session that delivers nowhere as merely quiet.
  Enforcement lives at the send sites, not in storage — see
  [messaging](messaging.md) for the `SilentRenderer` substitution that stops a
  disconnected non-Slack conversation being written to.
- `GET /api/chat/channel-targets` — owner-authenticated union of Slack
  destinations and every registered transport's configured targets. The
  dashboard session menu renders this list with per-channel brand icons.
  Unavailable configured destinations are returned with a reason rather than
  silently omitted (Teams before first inbound; WeCom proactive send); the menu
  keeps those rows keyboard-focusable, shows the reason inline, and announces
  the same reason instead of presenting an unexplained disabled action.
- In-channel `/link` / `/unlink` — `/link` writes the link on the current
  conversation's `legacy_dashboard_mirror_key`; it does not control display, history,
  or the inbound direction — only the outbound echo. `/unlink` frees the
  LOCATION via the shared `messaging.link.release_conversation_location`
  helper (one implementation for every DM dispatcher): after the key-addressed
  clears it sweeps every binding whose mirror targets this conversation
  (`clear_mirror_links_at`), including a binding stranded under a rotated DM
  generation and another dashboard session's outbound mirror into the
  conversation — the same occupant set the Discord resume conflict check
  refuses on, so its "Run `!unlink` first" guidance is always followable. The
  reply reports the count when more than one binding was cleared.

**Delivery** (`chat_runner._deliver_cross_surface_reply` /
`_deliver_cross_surface_user_message`, via the shared `_resolve_mirror_target`
preamble) is best-effort and gated on: Slack skipped (its own inline mirror); a
registered transport with `supports_proactive_send` (WeCom is False → `/link`
rejected there); and the `channels` governance ceiling via
`governance_permits("channels", channel_type)`, so an operator policy
restricting outbound messaging is honored on this egress too (fail-closed on any
governance error — matching the Slack path). Egress text is redacted through the
canonical `redact_via_context` shim so a loaded companion's extra
credential/token regexes apply.

**Known asymmetry / future work.** Slack already runs the unified one-session
model; the other transports run two sessions bridged by the mirror. Folding the
dashboard channel tab into the channel session (as Slack does) would remove the
second sid and the live render-duplication it can cause, at the cost of a
dashboard-turn-loop refactor.

## Session Lifecycle at Startup

```
start_pool()
  ├── _spawn_warm() × pool_size   → warm pool queue (instant assignment)
  └── _ensure_background()        → BACKGROUND_KEY session (persistent)
```

## Removing a session from the registry: record the end

**Every path that removes an entry from `_sessions` must report that removal to
`metrics/sessions.py`** — with `await record_session_ended(key, end_reason=...)` for
a session that lived, `await record_sessions_ended(keys, end_reason=...)` for a path
that drains many at once, or `await discard_session_start(key)` for a registration
being rolled back before it ever became one. All three are coroutines: the
breadcrumb unlink is a filesystem syscall and must not run on the event loop, where
a slow or network-backed data home would park every gateway task behind one closing
session. This is a correctness requirement on lifecycle code, not a telemetry
nicety, and it is documented here rather than only in the metrics module because the
people who can break it are editing this subsystem.

The reason it matters more than a missing sample: a session start writes a
breadcrumb file that survives process death, which is what lets a session killed
with its gateway be counted at all. A removal that reports nothing leaves that
breadcrumb behind, and the next boot reads a surviving breadcrumb as a crash. So
an unrecorded removal does not lose a data point — it **fabricates a crash that
never happened**, inside the one population the instrument exists to expose.

Practical rules when you add or change a removal path:

- Report it while you still hold the session registry lock, in the same tick as the
  `pop` / `del` / `clear`. The call's own pop and histogram record happen before its
  single suspension point, and holding the lock across that point is what keeps a
  racing cold start from registering a successor under the same key and having its
  record consumed by the predecessor's teardown. Reporting AFTER the path's other
  awaits is the bug this rule exists to prevent.
- Drain many keys with ONE `record_sessions_ended` call, never a loop of
  `record_session_ended` awaits. A per-key await puts a cancellation point between
  two keys, so every key after it is popped but unrecorded — and on `close_all`,
  which a cancellation reaches by design, that fabricates a crash per remaining
  session.
- Keep your MANDATORY post-pop cleanup reachable. All three recording coroutines
  absorb a cancellation at their crumb hop for exactly this reason, so the call
  itself will not abort you — but the rule that makes that safe is yours to hold:
  once you have popped the registry you are past the point of no return, so the
  session-map mutation that finishes the teardown (`destroy`'s `delete`,
  `discard_conversation`'s and `reset`'s `clear_sid`) must not sit behind a
  suspension point that can be skipped. Put it in a `finally` that covers every
  await after the pop, and never add a bare `await` between the pop and it.
- Give it its own `end_reason` if it is genuinely a different event. The enum is
  closed and lives in `metrics/sessions.py`; reusing a label merges two
  populations, and a metric is not a good reason to grow a lifecycle signature.
- A registration cancelled mid-flight is NOT an end. Use `discard_session_start`,
  which consumes the breadcrumb without emitting a lifetime.

`test/metrics/test_session_duration.py::TestEveryRegistryRemovalRecordsAnEnd`
enforces this. It is fail-closed and discovers its own scope: an AST walk over
every module under `src/kiro_crew` that mentions `_sessions`, recognising the
`pop`, `del` and `clear` spellings, so a new removal path anywhere fails the gate
the day it lands rather than waiting to be added to a list. A container that
merely shares the attribute name can be exempted with a stated reason, and the
exemption self-voids if that module ever starts writing breadcrumbs.

## Security: PreToolUse Command Enforcement

Command denial is enforced by Kiro Crew's bundled `hooks.py` `PreToolUse` gate,
not by injecting `deniedCommands` into kiro agent specs. Agent config generation
keeps the bundled security hooks as the immutable base, merges user hooks after
them, and strips retired `deniedCommands` / `autoAllowReadonly` fields left by an
older installation. Session startup and periodic cleanup do not rewrite agent
configs.

## Orphaned MCP Server Cleanup

`_cleanup_orphaned_mcp_servers()` kills MCP server processes that survived
session teardown.  kiro-cli-chat spawns MCP servers (kiro_crew mcp-core/cron,
the internal MCP server, slack-mcp) in separate process groups.  When a
session dies, `killpg` only reaches the kiro-cli process group — MCP servers
in other groups get reparented to init and leak memory.

**Tracking**: both transports snapshot their descendant PIDs and persist
them to `kiro_pids.txt` as `child_pid:parent_pid[:start-id]` entries —
`AcpClient` appends them with `_track_child_pids(pids, parent_pid=<root pid>)`,
`AcpRuntime` rewrites its root's whole block with `_replace_child_pids` (see
below); the third field is the
child's process-start identity (`_pid_start_token`, colon-free, in-process
and non-blocking on every platform), omitted only when unreadable at track
time.  `AcpClient` snapshots in `ensure_ready()`.  `AcpRuntime` snapshots in
`_snapshot_descendants()`, called repeatedly for a reason the one-shot client
scan does not face: the runtime's registered PID is the sandbox launcher, and
the tree under it is `launcher -> agent -> agent chat process -> MCP servers`,
so the scan runs when the initialize handshake proves the agent came up and
then at the end of every `_finish_create_session()` and `load_session()`,
because each session start — fresh or resumed — forks another agent process
and re-initializes MCP servers the earlier scan could not have seen.

Nothing announces a descendant's exit: they are the gateway's
GRANDchildren, so there is no `SIGCHLD` to catch and no wait to reap (the
gateway does not set `PR_SET_CHILD_SUBREAPER`).  Looking is the only way to
learn, so each pass re-enumerates the tree and writes the whole answer through
`_replace_child_pids(records, parent_pid=<root>, drop=<gone>)` — one lock, one
atomic rewrite, a `bool` back.

One rule governs every field-3 write, and the reason is asymmetric harm.  A
token that is stale costs a MISSED reap: the sweep compares live against
recorded, sees them differ, and prunes the line without killing — a leak, and
the sweep still reports it.  A token that names the wrong process costs a WRONG
KILL: live and recorded agree (both the stranger's), the recycle guard does not
fire, and `_cleanup_orphaned_mcp_servers` signals a process that merely reused
the number.  So when identity is in doubt the answer is always "do not record",
never "record whatever holds the number now":

> **An identity may be written only if it was captured while that pid was
> confirmed to be ours.**

Everything else follows from it:

- **The writer never reads a live identity.**  `_replace_child_pids` persists
  `_recorded_start_token` of the mapping's value — the token its caller
  captured — and calls no reader of its own.  An append-based writer had no
  such hazard because it never refreshed field 3 at all.
- **A pid in the tree is confirmed by a second walk before its fresh identity
  is kept.**  One released between the enumeration and the capture can be held
  by an unrelated process when its identity is read; absent from the second
  walk it is not a descendant of this root, and it is dropped.  The window is
  not closed — crossing it now needs a pid to become a stranger and then become
  our descendant again.
- **A pid outside the tree is kept by IDENTITY, not liveness.**
  `_escapee_is_still_ours` compares its live start id against the recorded one.
  Liveness alone is the kill-a-stranger case: a descendant that exited and had
  its number taken reads as alive.  A match keeps the child that left the
  process group, whose record is the only handle anything has on it; a mismatch,
  an unreadable identity and an exit are all "not ours" and drop the line.
- **The root is bracketed too.**  `_root_identity_holds` compares the runtime's
  own pid against the start id recorded at spawn, before the first walk and
  again before the write, because a walk from a recycled root enumerates a
  stranger's whole tree.  No recorded identity means nothing is recorded.
- **Only the caller's own children are rewritten.**  A line is removed when its
  child pid is named in `records` or in `drop`, never merely for sitting under
  this `parent_pid`: a root pid is reused like any other, and a descendant that
  outlived an earlier runtime holding this number is still tracked under it.
- **One pass at a time per runtime.**  Each pass rewrites the block from what it
  read, so two overlapping passes let the older read win and drop the newer
  descendants; sessions start concurrently on a shared runtime, so the pass is
  serialized on a per-runtime lock.
- **The file is written before the in-memory record.**  A `False` publishes
  nothing, so the record never claims a line the file does not carry, and the
  next pass retries.
- **A failure never raises, a cancellation always does.**  Losing a snapshot
  must not fail a live session, so every `Exception` is logged at WARNING and
  swallowed.  A `CancelledError` is not a failure and reaches the caller's
  cleanup guard, which kills the half-built runtime or terminates the session
  it owns.

On clean shutdown each transport prunes entries by the DESCENDANT's own
liveness, never by the root's fate: `AcpClient._reset_state()` and
`AcpRuntime._kill_inner()` (through `_prune_dead_descendants`) untrack only the
children confirmed gone and log the survivors at WARNING.  A child that
escaped the group kill by calling `setsid` keeps its entry, because that entry
is the only handle the periodic sweep and the next startup cleanup have on it.
If the gateway crashes, the entries remain in the file for the next startup.

**Detection**: reads `kiro_pids.txt`, processes only `child:parent` lines
(bare PID lines are kiro-cli parents handled by `cleanup_orphaned_sessions()`).
If the child is alive but its parent PID is dead, the child is orphaned and
killed.  Two guards run first.  The start identity (entries carrying the
start-id field) is subtractive evidence: a live `_pid_start_token` that
differs from the recorded one proves the PID was recycled, and the stale
entry is pruned without killing.  A matching or unreadable token never
authorizes the kill by itself -- the tracking file is same-uid-writable, so
a forged line must not aim the sweep at an arbitrary process.  The kill is
authorized only by the reparent heuristic: a genuine orphan reparented to
init (pid 1), or still showing the dead parent's PID (kill/reparent race),
is killed outright, while a PPid in the same-uid `systemd --user` subreaper
set -- the same accepted-parent set `_our_orphan_pids()` uses, computed by
the shared `_accepted_subreaper_pids()` -- additionally requires BOTH the
`KIROCREW_SPAWNED` environ marker AND positive runtime argv identity
(`_tracked_child_has_runtime_identity`: managed agent runtime, MCP
entrypoint, or marked launcher shape; unreadable argv fails closed), because
every manager-started user service holds the manager's PID as its PPid for
its whole life and the marker is tree-wide, inherited even by intentional
survivors.  Any other PPid means recycled: pruned without killing.

**Why not ancestor walk?** MCP servers are spawned in separate process groups
and immediately reparented to init (ppid=1) even while the session is alive.
Walking the process tree would always conclude they are orphaned.  Storing the
parent PID explicitly avoids this.

**Safety**:
- Zero false positives — only kills PIDs we tracked, only when the specific
  parent session that spawned them is confirmed dead
- Dead children are silently pruned from the file
- Bare PID lines (kiro-cli parents) are ignored by MCP cleanup

**Invocation**:
- **At startup**: `cleanup_orphaned_sessions()` calls it after PID-file cleanup
- **Periodic**: `_cleanup_loop()` calls it alongside idle session expiry (~60s)
- **At shutdown**: `cleanup_orphaned_sessions()` on signal/exit

### Unreachable gatewayd reclamation

`mcp_gateway.gatewayd` daemons are their own session/process-group leaders
(`start_new_session=True`), so a launcher that dies without signalling one
(pytest teardown is the common case) leaves it resident forever — `killpg`
from the launcher's tree cannot reach it, and the marker-based orphan sweep
excludes gateway entrypoints (`_GATEWAY_MARKERS`) because a cmdline alone
cannot distinguish a live dev pod's daemon from a dead launcher's. Two layers
close the leak, both keyed on the one reachability signal that IS observable:
the daemon's `--socket` path. gatewayd creates that socket at bind, so once
the path is absent from disk no stub can ever connect again — the process is
provably unreachable regardless of who launched it.

- **Self-exit (primary, in-daemon)**: `gatewayd._socket_liveness_sweeper`
  stats its own socket path on the idle-sweep cadence, armed only after a
  successful bind. Three CONSECUTIVE `ENOENT` observations set `stop_event`,
  taking the same graceful drain as SIGTERM (backends drained and reaped).
  Any other stat failure (EACCES/EIO) is inconclusive and never counts.
  POSIX-only — a Windows named pipe has no directory entry to observe.
- **Sweep-side reap (defense in depth)**: `_is_sweepable_orphan_gatewayd` is
  a fourth positive-identity path in the untracked orphan sweep. It overrides
  the `_GATEWAY_MARKERS` exclusion only for a structural
  `-m kiro_crew.mcp_gateway.gatewayd` argv whose `--socket` path is gone
  (NUL-separated argv only — the space-joined `ps` fallback cannot delimit
  paths safely and fails closed, so the path is effectively Linux-only).
  `kiro_crew.cli` / `kiro_crew.__main__` stay unconditionally excluded. The
  kill is TERM-first (`_kill_orphan_gatewayd`) so the daemon drains its own
  pooled backends, escalating to `killpg` SIGKILL only after the daemon's full
  `TOTAL_SHUTDOWN_BUDGET_SECS` (shared with the supervisor's SIGTERM→SIGKILL
  grace, so a correctly-draining daemon is never killed mid-drain), with a
  cmdline re-verify guarding PID recycling. Same-uid + reparented-to-init
  candidacy, the age floor, and the kill budget all still apply.

### session_pid sidecar contract (`session_pid_sig.py`)

The gateway maps its direct child pid to a session key by publishing
`config_dir()/session_pid_<pid>.txt` on session claim (writers:
`dashboard/chat_runner.py`, `slack/handler.py` — both route through
`session_pid_sig.publish_session_pid`, the single legitimate publish path).
Because the `.txt` lives in the same-uid agent-writable config dir it is NOT
a trust root on its own; publication therefore also writes a
`session_pid_<pid>.sig` sidecar:

- **MAC**: HMAC-SHA256 over `"<pid>:<body>"`, where *body* is the full
  published `.txt` content — the session key alone (legacy), or
  `"<session_key>\n<start_token>"` (recycle-guarded, below). The pid is bound
  into the MAC so one pid's pair cannot be replayed under another pid, and
  covering the whole body signs the start token too — flipping only the token
  invalidates the MAC. A legacy body yields a byte-identical message to the
  pre-token scheme, so mappings signed before the format change still verify.
- **PID-recycle guard** (issue #8343): the mapping used to bind only the pid
  *number*, so a recycled pid kept verifying and answered for the new
  process with the previous owner's session key until the next restart's
  orphan sweep. Publication now appends the process start token
  (`platform_compat.get_process_start_id` — the same incarnation identity
  `session_pid.py` records in its `<gw>:<pid>:<start_token>` sweep entries)
  as a second line of the `.txt` (a line, not a colon field, because the
  session key itself contains colons). Both readers dual-parse legacy
  vs guarded forms and refuse on a PROVEN mismatch — the lenient reader
  included, since strict-then-lenient fallbacks (`peer_resolve`) would
  otherwise silently recover the stale attribution. An absent recorded
  token (legacy file) or an unreadable live token (Windows, exited process)
  is unknown, never a mismatch — those resolve as before. Same-uid only:
  a robustness/misattribution guard, not a privilege boundary.
- **Key**: a purpose-specific subkey derived from the SEL trust root via a
  domain-separation label (`HMAC(sel_hmac.key, "kirocrew.session_pid.sig.v1")`).
  The raw root never signs a sidecar; the sidecar protocol and the SEL audit
  chain never share a signing key (see `sel.md`). Only `SecurityEventLog`
  ever *creates* the key file.
- **Writes are atomic** (`atomic_write` → `os.replace`): a pre-planted
  symlink at the predictable paths is replaced, never followed.
- **Consumers**: STRICT identity resolvers accept the direct
  `KIROCREW_HOST_PID` → mapping lookup only via
  `session_pid_sig.verify_session_pid`, which fails closed to `""` on a
  missing/short key, missing files, or MAC mismatch. Their remaining callers
  are the computer-use MCP tools (`mcp_computer.py`, for audit attribution)
  and the dashboard messaging-identity path (`dashboard/handlers/messaging.py`).
  The former state-mutating session-bound tools that resolved identity here —
  `monitor_start`, `monitor_update`, `autonudge_stop`, `set_project` (plus
  `suggest_followup` and `ask_question`) — became STATELESS directive-return
  tools in issue #755 (see "Stateless session-directive tools" below); they
  still call the strict resolver, but only as a context guard, and no longer
  bind any effect to the key it returns. Lenient (read-only)
  resolvers keep reading the `.txt` without a signature check, but through
  the same hardened reader (`session_pid_sig.read_session_pid_txt`:
  no-follow, regular-file, size-bounded) — `session_pid_sig` owns both the
  read and write discipline for the file family. Every `.txt` reader routes
  through it: `mcp_core._resolve_session_key` (host-pid + walk),
  `mcp_shared._policy_session_key` (the policy walk, called by
  `_resolve_tool_policy`),
  `mcp_caller.CallerContext.from_env` (host-pid + walk; also serves
  `mcp_gateway/stub.py`), and `mcp_gateway/gatewayd._resolve_peer_identity`
  (server-side peer walk). The sidecar is additive. All four are pid-keyed and
  therefore answer with the PARENT for a session-sharing subagent, which is why each
  client-side one reads the per-SESSION token (`session_token_sig`) above these
  sources; the walk remains the last resort rather than the first answer.
- **Unsigned degrade**: if the SEL key is unavailable at publish time the
  `.txt` is still written (lenient readers keep working) and any stale
  sidecar is removed — strict resolvers fail closed for that pid.
- **Key rotation**: rotating/regenerating `sel_hmac.key` (e.g. snapshot
  restore, which deliberately excludes the key) invalidates every existing
  sidecar; strict resolvers fail closed until the next turn's publish
  re-signs the mapping. Benign and self-healing — no migration step.
- **Stale cleanup**: the orphan sweep removes `session_pid_<pid>.sig`
  alongside its `.txt` for dead pids (`session_pid.py`). "Dead" is not
  `pid_exists` alone: Linux numbers threads from the pid space, so a dead
  session's pid recycled as a THREAD of an unrelated live process still
  satisfies that probe and the mapping would survive forever (observed on a
  host whose pid counter had wrapped: 233 mappings, one naming a 6-day-dead
  session through a thread). `_prune_stale_session_pid_files` therefore
  removes a mapping when the pid is unsignalable, OR when it is absent from
  one `platform_compat.live_thread_group_leaders()` snapshot **and** a
  per-pid `platform_compat.is_thread_group_leader(pid)` re-read returns
  `False`. Both helpers answer `None` when the question is unknowable
  (non-Linux, unreadable `/proc`), and `None` never licenses a removal — so
  macOS and Windows keep the pre-existing `pid_exists`-only behaviour. Two
  orderings are load-bearing: the snapshot is taken AFTER the glob (a pid
  that starts in that window lands IN the set and is retained), and absence
  from it selects a *candidate* rather than the outcome, so a pid recycled
  since the snapshot — whose new owner has already republished the mapping at
  that same path — is retained by the re-read instead of losing a live
  session's identity. The snapshot costs one `/proc` directory read for the
  whole pass, which is still work the gateway boot path does not carry:
  `narrow_with_leaders=False` there per `no-new-work-on-gateway-boot-path`
  (and on the force-exit handler, which must reach its `os._exit`), while the
  graceful-shutdown sweep asks for the narrowing. This pass touches only the
  `session_pid_<pid>` family, never the shared `kiro_session_pids.txt` that
  pass 1 rewrites.
- **Member execution routing**: the ordinary session/run owner record carries
  the immutable member/store snapshot. Strict MCP caller identity still uses
  the existing transport token and signed `session_pid` publication. No separate
  member PID publication, process ancestry scan, or member proof token exists.
  Providers receive the admitted privacy mode before startup; restricted workers
  do not retain Crew native-transcript copies or raw-frame logs. External engine
  retention follows that provider's own supported behavior.
- **Threat model** (full version in the `session_pid_sig.py` module
  docstring): file forgery, cross-pid replay, tampering, and symlink
  planting are blocked; deliberate same-uid impersonation via
  attacker-chosen env in self-launched processes is out of scope (identical
  capability exists against env-only resolution) and is tracked as the
  SO_PEERCRED gateway-authentication follow-up (issue #302).

### Stateless session-directive tools (`session_directive.py`, #755)

Eight session-bound MCP tools — `monitor_start`, `monitor_update`, `autonudge_stop`, `set_project`, `suggest_followup`, `ask_question`, `reset_conversation`, `chat_tag` — used to resolve their OWN session identity (the strict sidecar resolver above) and call a loopback HTTP endpoint, which only produced a usable per-call caller when MCP-gateway **pooling** was enabled. They are now **stateless**: the tool validates its arguments and returns a *directive* — a human-readable confirmation line plus a machine-readable marker (`session_directive.encode`) carrying the validated payload and NO session key. The session-aware consumer, `dashboard/chat_runner._run_chat`'s `EVENT_TOOL_RESULT` handler, decodes the marker (`session_directive.decode`) and applies the effect IN-PROCESS against ITS OWN `slot`/`session_key` via `dashboard/session_directive_apply.py`, then strips the marker from the stored transcript. This works with pooling OFF (the default) because the consumer already owns the session, so no per-process identity source is needed.

Subagent isolation is therefore **structural, not cryptographic**: a subagent's tool result flows through the subagent's own runner and can only ever bind to the subagent's session, never its parent's — there is no `/proc` walk to get wrong. The tools still call `_resolve_session_key_strict()`, but only as a context guard to short-circuit sessions where a directive can never be applied (cron/hook/subagent) and to steer non-`dashboard:` `ask_question` callers to the `[OPTIONS:]` tag — not to bind the effect.

Security properties (enforced in `session_directive.decode` plus the applier):

- **Forgery gate keyed on canonical identity**: because the marker is model-visible (it returns as the tool-result text), a directive is honoured ONLY when the tool call was recorded — via kiro-cli's out-of-band `_meta` channel — as an MCP call whose canonical `_meta.kiro.toolName` (with `_meta.kiro.mcpServerName` set) is in `DIRECTIVE_TOOLS`, never the LLM-authored `title`. A shell command titled `monitor_start` whose stdout forges the marker resolves to no directive tool and is ignored; the gate fails closed when `_meta` identity is absent.
- **Native sub-agent calls refused**: they surface as flat events in the parent loop but have no independently bindable slot, so the applier declines them.
- **SEL audit on every application**: `apply_session_directive` emits a tool-invocation event tagged `source="mcp-directive"` with outcome `success` / `denied` (e.g. a `set_project` sensitive-path block) / `error`, since the effect now runs in the consumer rather than in the tool body or an HTTP endpoint.

The applier reuses the SAME effect cores the HTTP endpoints call — `authorize_and_add_nudge` / `authorize_and_update_nudge` / `svc.remove` for the monitor trio, `slot.project` plus the recent-projects save for `set_project`, `deliver_ws_owners` for `suggest_followup`, and `post_question_card` for `ask_question` — so behavior is unchanged except that `ask_question` is now non-blocking (full contract in `learn-cron-dashboard.md` → "Agent Questions"). `reset_conversation` is the one directive whose core is not reachable inline: it queues `SessionManager.discard_conversation` on the slot for `chat_runner._consume_pending_reset` to apply at a turn boundary, because the discard is a full provider teardown and the producer is mid-turn — the same deferral `set_project` uses, and the reason the immediate route (`POST /api/chat/slots/{slot}/reset-conversation`) answers 409 on a busy slot rather than tearing down a turn mid-write. It queues the session key THIS TURN ran on, passed in by the consumer, never re-resolved from the slot: `linked_session_key` is mutable, so a cron or workflow injection that rebinds the slot between the request and the consume would otherwise discard whatever the slot points at by then and leave the caller's conversation alone. Only the END-OF-TURN consume may apply a discard (`allow_discard`); the two earlier consume points run just before a turn acquires the session, where a teardown lands under a channel turn already streaming on it. Even at that boundary it does not assume: the discard goes through `discard_conversation(..., skip_if_busy=True)`, which refuses under the same session lock that pops the session, mirroring `reset`'s own guard. Probing from the consumer and tearing down afterwards would leave a window in which a channel message acquires the session's semaphore and begins streaming a reply the teardown then destroys — and the semaphore is the stricter signal anyway, since `provider.has_active_turn()` cannot see a turn holding the semaphore with no prompt in flight yet. A refusal returns False and changes nothing, replay flag and session map included, so the consumer leaves the flag armed for a later boundary. The sid clear runs in the SAME tick as the pop, with no await between them — deferring it past the shutdown awaits lets a concurrent channel turn map a SUCCESSOR session under the key while the old provider is still shutting down, and the clear then erases the successor's pointer instead of the discarded one. Sub-agent children are the other wait — `discard_conversation` releases the shared runtime they run on, so a running or queued child, or an in-flight completion-event delivery, also leaves the flag ARMED rather than killing the child's work. Both that consume and the route's 409 read one predicate, `chat_utils.subagents_attached_async`, so the two cannot drift. The queued flag is in-memory slot state: a gateway restart while it sits armed drops the reset the confirmation promised, which is accepted rather than persisted — the cost is one un-applied reset the caller can ask for again, against durable state for a transient intent. `set_project` and `reset_conversation` additionally require structural user-turn provenance: injected cron, task-runner, sub-agent, auto-nudge, orchestration, app-authenticated unattended turns, and app-authored Spec Builder seed/handoff prompts cannot retarget a borrowed destination slot even when its session key is user-facing. Spec Builder rejects app-token message and decision submissions before they can enter its human-provenance relay or durable decision ledger. Queue entries preserve this provenance, replacement text adopts the editor's provenance, and mixed or untagged merges fail closed.

Gateway-off (the default topology this targets), the model's tool result is the tool's OWN returned line delivered over kiro-cli's MCP pipe; the applier's confirmation string and SEL audit are recorded on KiroCrew's own surfaces (transcript / WS / hooks) and do NOT rewrite the model's tool result. Each tool therefore phrases its own message as a *request* that the consumer applies (and may refuse — no interactive session, invalid/sensitive path, capped/paused loop) rather than asserting the effect already landed.

```mermaid
sequenceDiagram
    participant M as Model
    participant T as MCP tool (kirocrew-core)
    participant R as chat_runner._run_chat<br/>(EVENT_TOOL_RESULT)
    participant A as session_directive_apply
    M->>T: call e.g. monitor_start(args)
    T->>T: validate args (resolves NO session identity for the effect)
    T-->>R: tool result = human line + directive marker
    R->>R: decode(result, canonical _meta.kiro.toolName)
    Note over R: forgery gate — canonical name in DIRECTIVE_TOOLS,<br/>not the LLM title; native sub-agent calls refused
    R->>A: apply_session_directive(slot, session_key, kind, args)
    A->>A: run effect core against the consumer's OWN slot
    A-->>R: confirmation string + SEL audit (source="mcp-directive")
    R->>R: strip marker from stored transcript
```

### Orphan Sweep Active Set

The periodic sweep of `kiro_session_pids.txt` (which kills tracked kiro-cli
PIDs no longer in `self._sessions`) builds its active set as the union of
`_collect_active_pids(self._sessions)` + `_pool_pids()` + `_in_flight_pids()`
+ `_companion_runtime_pids()`, re-checked against the same union in phase 2
before any kill. `_companion_runtime_pids()` returns the live PIDs of
`self._subagent_runtimes` (companion runtimes multiplexing a parent session's
subagents) and `self._bg_runtime` (the multiplexed `_bg` runtime), each guarded
on `is_alive()` — only alive runtimes are shielded, so dead ones are still
reaped.

**Failure it fixes**: since the `AcpRuntime` unify, *every* runtime records its
PID in `kiro_session_pids.txt` at spawn. These two runtime kinds live outside
`self._sessions`, so before this union the sweep saw their live PIDs as
untracked orphans and SIGKILLed them mid-chat (surfacing as
`process exited (rc=-9)`).

### Cross-platform process management (platform_compat)

All process liveness/kill/PID-file-lock operations in `session.py` and
`session_pid.py` go through `kiro_crew.platform_compat` so KiroCrew runs natively on
Windows as well as macOS/Linux. The critical correctness reason is that
**`os.kill(pid, 0)` is NOT a liveness probe on Windows — it terminates the process** —
so every liveness check uses `platform_compat.pid_exists(pid)` (or the tri-state
`pid_liveness`) instead, kills use `kill_pid` / `kill_process_tree`, the PID-reuse
guard reads the parent via `get_ppid`, the managed-agent check uses
`process_matches(pid, ("kiro-cli","claude"))`, and the PID-file locks use
`platform_compat.file_lock` / `acquire_lock` / `try_acquire_lock` (POSIX `flock`
vs Windows `msvcrt`). On POSIX the behavior is unchanged.

## Bytecode-Cache GC (periodic sweep hook)

The desktop app launches the gateway with `PYTHONPYCACHEPREFIX` pointed at
`<data home>/cache/pycache` (keeps the embedded interpreter's bytecode out of
the codesigned bundle). CPython only ever adds to that PEP 3147 mirror, so the
periodic sweep in `_cleanup_loop()` owns eviction: it calls
`pycache_gc.prune_pycache` (mtime TTL + oldest-first total-size cap; limits
owned by `pycache_gc.py`) on the maintenance executor, gated to at most once
per `PYCACHE_GC_INTERVAL_SECS` because the prune walks the whole cache tree —
far heavier than the sweep's ~5-minute tick. `_last_pycache_gc` starts `None`
so the first tick after the first session starts prunes pre-existing bloat
(`_cleanup_loop()` is launched by session registration, not gateway start),
and is stamped **before** the prune runs so a failing walk retries at GC
cadence, not every tick. The traversal is anchored to no-follow directory
handles (`O_NOFOLLOW | O_DIRECTORY` + `dir_fd`-relative unlink/rmdir), and
the root is opened component by component from the filesystem root, so a
symlink or junction substituted anywhere — under the cache root mid-walk, or
swapped into a writable ancestor such as `cache/` itself — fails the open
instead of redirecting deletion outside the cache (a legitimately symlinked
ancestor thus makes the prune a conservative no-op); on platforms without
`dir_fd` support (Windows) the prune is a fail-closed no-op. Deleting entries
is always safe: a `.pyc` regenerates on the next
import. The unbounded-growth *input* (foreign interpreters in the agent
subtree inheriting the prefix) is closed separately by the sandbox env scrub —
see [security](security.md) § Conditional Python-interpreter env strip.

## Recovery ladder (`recovery/policy.py`, `recovery/ladder.py`)

Every layer that retries reads ONE schedule. `RecoveryPolicy` is exponential
backoff (`base * 2**(attempt-1)`, base `agent.recovery_backoff_base_secs`=2s,
cap `agent.recovery_backoff_max_secs`=120s) with EQUAL jitter (a delay is drawn
from `[raw/2, raw]` -- full jitter can draw a near-zero delay and hot-loop a
layer whose failure is not transient yet); a server-stated `retry_after` is a
floor, never ignored, and still capped so a hostile hint cannot park a task.
Attempts are counted per unit (`RecoveryTracker`: a slot key, a backend key, a
runtime id, the daemon) and forgotten after a cooldown, so a unit that failed a
day ago starts at attempt 1. The ladder is tried bottom-up and each rung is
bounded; escalation happens only when the rung below exhausted its attempts:

| layer | unit | trigger | cleanup deadline | attempts before escalation | action |
|---|---|---|---|---|---|
| `L1_tool_call` | slot | JSON-RPC error classed `recoverable_infra`: the MCP stub's `-32001 capacity` (with `retry_after_secs`), backend gone, spawn-queue timeout | -- | 3 | re-issue the call: one continuation on the same session (`chat_runner`, below); task stays running |
| `L2_backend` | backend key | `BackendGone`, initialize timeout, breaker OPEN | per-backend shutdown budget (`POOL_SHUTDOWN_SECS`) | 2 | `gatewayd._respawn_backend_for_stub` under the spawn gate |
| `L3_acp_runtime` | runtime | `AcpRuntimeDead`, stall past the idle window, `session/new` abandoned | `TOTAL_SHUTDOWN_BUDGET_SECS` + process-tree kill | 2 | rebuild the runtime, `session/load` if continuable; task `recovering` |
| `L4_gatewayd` | daemon | liveness ping failed 3x (neither the fast nor the escalated probe answered) AND no backend progress | daemon drain | one respawn per 600s window (the second escalates) | `GatewayManager._run_watchdog` respawn; stubs reconnect within their 600s budget |
| `L5_gateway` | gateway | none automatic | -- | 0 | ONE notification per escalation; the user restarts. Never a `kirocrew restart`. |

**Where the rungs are recorded.** L1 for the main chat is `chat_runner`'s infra branch (below); for a sub-agent it is `subagent_manager/run.py::_yield_for_infra_retry`, which takes the ladder's delay and parks the run on the dependency coordinator's `mcp_gateway:<class>` scope ([subagent.md](subagent.md)). L2 is `gatewayd._respawn_backend_for_stub`: a completed respawn is `record_restart(L2_backend)` + `observe_success(L2, server)`, a give-up is `observe_failure(L2, server)` (the breaker's OPEN cooldown is the wait between rungs; the ladder counts, it does not sleep there). L3 is counted by the sub-agent run when the parent's shared runtime is unavailable (`observe_failure(L3, runtime:<parent>)`; the dedicated process stays the per-run recovery, the runtime's rebuild belongs to its owning session). L4 is the gatewayd supervisor. Distinct from these per-rung attempt counts is `SESSION_RECOVERY_MAX_ATTEMPTS` (3), the IN-PLACE budget for continuing one ACP session on the same runtime — the main chat's tool-stall / stale_recover nudges and pipe-death re-queues and the sub-agent's stop recovery all read it (`acp.types.STOP_RECOVERY_MAX_RETRIES` is its re-export).

Overload never enters the ladder: pressure lowers caps and pauses admission
([adaptive-concurrency](adaptive-concurrency.md)); only a unit that stopped
making progress AND failed an independent probe is torn down. L4 is `pinned`:
its floor/cap (1s/60s) are what the stub's 600s reconnect budget is sized
from, so the `agent.recovery_*` knobs move every layer but that one.

**Where the configured schedule is installed.** `RecoveryPolicy` imports no
config (the MCP stub depends on it), so the two keys are pushed in from outside:
`GatewayOrchestrator._init_subagents` calls `recovery.ladder.configure_default_ladder(cfg)`
once, on the config the process already holds, and every `default_ladder()`
consumer — the chat runner's L1 branch, the runner adapter's `decide_recovery`,
`task_executor`'s L3 backoff, `kirocrew doctor`'s ladder rows — then reads that
one snapshot. That step, not the module table, is what a configured install runs;
the table is the default. It sits in `_init_subagents` because that boot step is
unconditional and precedes the task runner, the dashboard and any chat slot, i.e.
every consumer above. The snapshot is one-shot, matching the keys' `restart=True`:
a later call keeps the first schedule and the attempt counts recorded under it, so
a knob change takes effect on the next start, and a ladder an earlier
`default_ladder()` already built adopts the configured schedule rather than
depending on boot order. L4 is skipped, being `pinned`. Two waits outside the
ladder reach the same two keys the same way, from a config already in hand: a
dependency wait through `taskq.dependency.coordinator_from_config` and a failed
store re-open through `taskq_bridge.taskq_arm_reopen(cfg)`. That is why
`kirocrew doctor` prints one number for the knob and not two.

The layers read the schedule instead of holding literals:
`mcp_gateway/manager.py::_RESPAWN_BACKOFF_START_SECS/_MAX_SECS` are
`LADDER.layer(L4).base_secs/max_secs` — the pinned rung, so import time is the
right time to read it — and every doubling goes through
`GatewayManager._next_respawn_backoff` (doubled, capped, jittered). Two sites read
the STATIC defaults at import and therefore do NOT follow the knobs:
`acp/client.py::_ACP_RESPAWN_BACKOFF_S` is L3's default base, spent on the single
permitted respawn delay, and `taskq/model.py`'s `recovery_backoff_secs` is the bare
default schedule (deterministic -- the dispatcher jitters when it wakes a row, and
the store orders rows by `next_run_at`, which wants a pure function).
`RecoveryLadder` emits
`kirocrew.recovery.{attempts,escalations,duration_secs,restarts}` and, given a
task id, one `task_events(kind="recover")` row per decision.

**L1 in the chat runner.** `AcpSessionHandle` classifies every tool result
once, at the protocol layer (`classify_infra_error` -> `handle.last_infra_error`,
cleared at turn start): the stub's `-32001` error object or its serialised
text, `class=capacity` + `retry_after_secs`, and a closed set of gateway
`recoverable_infra` markers; a result longer than 2000 chars is a document,
not an error, and never matches. The verdict describes the LAST tool result, so
dispatching a new tool call clears it: an output-less completion emits no
`EVENT_TOOL_RESULT` at all, and without that clear the consumer would re-issue a
refusal an intervening call had already superseded. It reaches the consumer
through the provider chain, the same three-level shape as
`last_compaction_transient` and never a cached copy: a read-only pass-through
property on `AcpSessionProvider` (from `_handle`) and on `AcpProvider` (from
`_client`), over `LLMProvider`'s `None` default for a provider that does not
classify tool results. Read-only because the handle is the sole writer -- a
caller that could set a verdict could force a tool re-issue.

At end of turn, when the turn ended normally and the LAST tool result was such
an error, `chat_runner` asks the ladder; on
`retry` it shows a notice, waits the jittered delay (`_recovery_delay`, a
module seam), RE-READS the interrupt signals, and only then queues ONE
continuation (`build_infra_retry_prompt`, opening
with `REFUSAL_RECOVERY_PREFIX` -- a capacity refusal is a tool refusal carried
back to the model) that asks for the same call again -- never a verbatim
replay of the user's message, because earlier calls this turn may have taken
effect. The re-read is the throttle-exhaustion fallback's shape and exists for
its reason: the guards in the branch condition were read before a multi-second
wait, and a Stop, a steer or a user follow-up arriving during it resolves while
no prompt is active, so nothing downstream catches it -- the dispatch-point
purge covers the promise-only and post-compaction continuations only. It reads
the LIVE signals (`_should_suppress_requeue`, `_stop_pressed()`, a queued user
follow-up, `_pending_steers`), so it also sees a stop issued on a linked channel
surface, which the slot-scoped generation in the condition cannot. On an
interrupt the re-queue is dropped, the ladder attempt is handed back with
`forget` (a run charged for a retry that never ran shortens the next real ladder),
the slot's own `_infra_retries` bump is given back for symmetry with the one line
that made it -- the landing's settlement clears it either way -- the persisted
"retrying it in Ns" card is
corrected in one line on the dispatch-point purge's trigger split (only a user's
own message takes over; a Stop ran nothing), and the turn LANDS:
`_recovering_infra` stays clear so it settles, saves and resets its budgets like
any other landing. A turn that DID re-queue the continuation is un-landed
(`_recovering_infra`, like `_recovering_promise`).
The wait is counted on the slot's OWN `_infra_retries` (health cause
`infra_capacity`), never on `_transient_5xx_retries`: that one is a live budget
read by the re-prompt gate, the backoff seed and the throttle-exhaustion
model-fallback threshold, so spending it here shortens the next real 5xx ladder,
inflates its backoff seed and brings the swap onto `agent.fallback_model` that
many errors closer -- over a wait the model had no part in. Like the transient
budget it is cleared on a landed turn AND on every arm that ends the turn without
re-queuing.

On `escalate` the runner stops retrying and says so, and marks the run spent
(`_l1_escalated`) -- distinct from `_recovering_infra`, which also suppresses
turn settlement and consolidation, because an escalated turn does land. A landed
turn that RECOVERED calls `observe_success(L1, slot.key)`, which measures the
outage; a landed turn after `escalate`, or after an interrupt during the backoff
dropped the re-queue (`_l1_interrupted`), calls `forget(L1, slot.key)` instead,
so the next user turn gets a fresh budget and no recovery duration is recorded
for an outage that never closed -- an interrupted run never ran its retry, so
nothing observed the dependency come back.

**Every recovery wait in `_run_chat` re-reads its interrupt signals after the
sleep, and WHAT it re-reads depends on what it re-queues.** All three waits on the
exception/settlement path check their guards, sleep a multi-second floor, then
insert at queue index 0. A Stop, steer or follow-up arriving during that sleep
resolves while no prompt is active, and the dispatch-point purge in
`_start_next_queued_turn` covers only the promise-only and post-compaction
continuations -- so each arm re-reads for itself.

| Wait | Re-reads | Handed back on a drop |
|---|---|---|
| L1 gateway-capacity (CONTINUATION) | suppress, `_stop_pressed()`, queued user follow-up, `_pending_steers` | the counted `_infra_retries`; the ladder run is `forget`-ed, not recorded as recovered |
| same-model transient 5xx (VERBATIM REPLAY) | suppress, `_stop_pressed()` **only** | all four per-turn budgets (`_transient_5xx_retries`, `_infra_retries`, `_fallback_candidate_idx`, `_fallback_walked`), byte-identical to the throttle-exhaustion drop arm |
| post-token one-shot (CONTINUATION) | suppress, `_stop_pressed()`, queued user follow-up, `_pending_steers` | nothing: `_posttoken_retry_used` is consumed BELOW the wait, so a dropped retry never spends the allowance |

The replay arm's two exclusions are deliberate, not an omission. It runs on
`not _turn_emitted`: no token and no tool call landed, so a mid-turn correction has
nothing to contradict and the user's request is still entirely un-run -- dropping it
would erase that request with no output anywhere in the transcript. Ordering keeps
both intents instead: the replay goes in at the HEAD, so a follow-up typed during
the backoff runs after it, and an unconsumed steer is degraded to a head card by
`_requeue_unconsumed_steers` in the same `finally`, which dequeues BEFORE the
replay. The throttle-exhaustion arm, which replays the identical message, reads the
identical two signals. Three blocks and not one helper, for that reason: a helper
checking the union would force the replay arm to drop on a follow-up, which is the
wrong answer there.

Every drop APPENDS a correction rather than retracting the pending card. The
pre-wait row carries `TRANSIENT_NOTICE_RETRYING` / `_RESUMING`, which the dashboard
renders as "retrying..." indefinitely; the drop appends `TRANSIENT_GIVE_UP_TEXT`
with `TRANSIENT_NOTICE_GIVE_UP` so the ErrorCard's Continue affordance comes back.
The post-token arm's persisted partial is never retracted (append-only).
`_recovery_delay` is the module seam these waits sleep through, so a pin can deliver
an interrupt DURING the wait without a process-wide `asyncio.sleep` patch; the
throttle-exhaustion arm still calls `asyncio.sleep` directly, and needs no seam
because it already re-read after its wait.

## Structured session health (`dashboard/session_health.py`)

`GET /api/sessions/health` classifies every running slot from STRUCTURED state,
in this order of authority: task rows (`taskq.TaskStore`), slot state
(`_ChatSlot.running`, open `_approval_futures`, `_question_pending`,
`_wait_state`, the recovery retry counters, children running for the slot), and
ACP handle liveness (`awaiting_permission`, the in-flight tool and its dispatch
age, `parked_for_secs`, the `_ingress_seq` / text-chunk / transcript progress
markers). Each slot is exactly one of `running`, `queued`, `waiting_children`,
`waiting_permission`, `waiting_dependency`, `waiting_input`, `recovering`,
`stalled`, with evidence and age. Only `stalled` is a defect: a slot whose
progress markers have not moved for `STALL_AFTER_SECS` (600s) with no wait
reason and no liveness-oracle `WORKING` verdict. A permission wait is never a
stall however old; a queued task is queue wait, not execution; a long tool call
that keeps producing events is running. The snapshot (`snapshot_state`) is
taken on the loop because it walks live objects; the classification
(`SessionHealthMonitor.compute`), the store read and the log tail run off it.
The `gateway.log` regex scan (`scan_log_for_stalls`) is a SECONDARY source: it
adds evidence to a running slot and is the sole source only when no state
objects are reachable; it never overrides a structured wait.

Payload: `{generated_at, stalled{slot: {reason, since_ts, evidence, age_secs}},
slots{slot: {classification, evidence, age_secs, since_ts, source}},
waiting[{kind: slot|task, ..., reason}], recovering[...], queued{available,
count, oldest_wait_secs, by_state}, effective_caps{lane_kind: {effective, ...}},
degrade_reason, counts{running, queued, waiting, recovering, stalled},
stall_after_secs, sources{slots, taskq, log}}`. `queued.count` sums
`session_health.TASK_QUEUED_STATES` — the states with nothing executing under
them that a dispatcher picks up on its own, `waiting_infra` included, since
infra capacity is exactly what that row waits for. `/api/tasks/summary`'s
`depth.queued` reads THAT constant rather than a second list, so the two panels
answer one number (`test_api_tasks.py::test_queued_set_is_one_constant_spelled_from_the_model`,
`::test_waiting_infra_is_queued_on_both_surfaces`). The task rows reported under
`waiting` are `taskq.model.WAITING` and nothing else: a live run yielded its
lane slot and its runtime is still resident. `effective_caps` and
`degrade_reason` come from sources the adaptive controller registers
(`default_monitor().register_cap_source(lane_kind, fn)` /
`register_pressure_source(fn)`); `subagents` is read from the manager directly.
Each computation samples `kirocrew.taskq.depth{state}`,
`kirocrew.taskq.oldest_wait_secs`, `kirocrew.taskq.effective_cap{lane_kind}` and
`kirocrew.taskq.pressure_reason{reason}` -- every attribute a closed-set value.
Tests: `test/test_session_health.py`, `test/test_sessions_health_cache.py`,
`test/test_recovery_policy.py`, `test/test_recovery_ladder.py`,
`test/test_recovery_l1_chat_runner.py`.

## Resource Budget (Gateway Mode)

| Session | Key Pattern | Lifetime | Process |
|---------|-------------|----------|---------|
| User chat | `slack:{thread_ts}` (legacy bare `{thread_ts}` folded) | Idle timeout (60 min) | Own kiro-cli |
| Dashboard tab | `dashboard:{slot_key}` | Idle timeout (60 min) | Own kiro-cli (from warm pool) |
| Cron job | `cron:{job_id}` | One-shot (reset after) | Own kiro-cli (from warm pool) |
| Background | `_bg` | Entire runtime (recycled at 70%) | Shared kiro-cli |
| Heartbeat | `_bg` | Shared | Shared kiro-cli |
| Lesson extract | `_bg` | Shared | Shared kiro-cli |
| Subagent | `subagent:{uuid}` | Task duration | Own kiro-cli |
| TaskRunner step | `taskrunner:{task_id}:step{N}` | Step duration (reset after) | Own kiro-cli (max 2 concurrent via semaphore) |
| TaskRunner decompose | `taskrunner:{task_id}:decompose` | Seconds | Own kiro-cli |
| TaskRunner review | `taskrunner:{task_id}:review` | Seconds | Own kiro-cli |
| TaskRunner acceptance | `taskrunner:{task_id}:acceptance` | Seconds | Own kiro-cli |
| Warm spare | _(in pool queue)_ | Until assigned | Pre-started kiro-cli |

**Cold-start admission**: `SessionManager._start_sem` bounds provider starts local
to one manager. The narrower common runtime chokepoint adds a gateway-wide
`AcpRuntime.spawn()` coordinator capped at 2 concurrent spawn + `initialize`
handshakes, matching worker-pool `max_starting=min(workers, 2)`. Authoring,
interactive, background, shared-runtime, and unpooled callers therefore share the
same expensive-start bound even when they bypass this manager or a worker pool.
Queued cancellation returns the permit, and runtime startup retains its existing
subprocess cleanup on cancellation or failure.

**Parallel step throttling**: TaskRunner limits concurrent step sessions
to `max_parallel_steps` (default 2) via `asyncio.Semaphore`. Cold starts
are staggered by 3s. A system load guard pauses spawning when CPU load
exceeds 85% of available cores.

## Compaction Race Handling

In-place compaction (both backends) keeps the `_sessions` entry healthy:
a concurrent `get_or_create()` reuses it, queueing on the session
semaphore behind the compact, then continues on the compacted session.

Only the kiro-cli failure recycle tears the entry down, and it runs inside
`_compact_in_place` under the turn semaphore that the compact attempt
already holds — never after releasing it. That is load-bearing: releasing
first and re-acquiring for the recycle leaves a gap a queued turn wins, and
that turn is then dispatched into a kiro-cli still finishing its compaction,
receives the late `completed` status instead of an `end_turn`, and hangs
holding the semaphore until the prompt timeout.

The recycle records the
exact session object under teardown in `_recycling` (distinct from
`_compacting`, which is just the trigger dedup gate): `get_or_create()`
skips reuse only when the map still holds that exact object, then
cold-starts fresh — a healthy replacement registered under the same key
during the teardown is reused normally, never overwritten. The recycle
pops by object identity — if a racing cold-start already replaced the
entry, only the old session object is shut down; the fresh replacement
and its session_map entry survive (the old provider is still reaped so
its process never leaks).
