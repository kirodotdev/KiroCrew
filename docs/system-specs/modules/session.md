# Session Manager Module

## Overview

Maps thread keys to LLMProvider instances (`session.py`). Each thread gets
its own kiro-cli session with idle expiry, context compaction, circuit
breaker, per-session semaphore, and persistent background session.

Chat sessions are served from the warm pool when eligible (default pool
agent, default cwd, no resume mapping); otherwise they cold-start on first
message via `get_or_create()`.

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

## Private member session ownership

An ordinary dashboard chat that has already used private member memory keeps
that ownership for its lifetime. The agent-switch endpoint reads the protected
binding for the effective session key before changing any slot fields, resetting
the provider or writing history. Choosing another member (including the default
assistant) returns `409 private_memory_session_pinned` and asks the owner to start
a new conversation. An unreadable binding returns 503; resetting the same member
and switching an unbound V1 conversation keep their existing behavior.

The provider's in-turn agent-switch event follows the same private boundary in
every dashboard slot mode. Before provider allocation, the runner validates the
store off the event loop and freezes its V2 owner from the memory version and
ownership record. Database schema migrations do not identify a V2 turn. Any
provider-reported switch on that private turn leaves the selected member intact,
shows a pinned-member notice, stops consuming further events and resets the
provider. The notice prevents an empty-response retry from replaying completed
tool actions. Ordinary unbound V1 chats retain their agent-switch behavior.

Member chat turns validate the member's private memory before provider
allocation and persist the binding used by memory tools and consolidation.
Binding resolution runs off-loop using captured member, project and session
selections. The runner rechecks those fields before private binding and again
before provider allocation; a changed or replaced slot refuses. Owner
create/switch paths recheck slot identity after resolution;
switches retain their commit-token rollback and last pre-reset busy checks. This
does not allow a protected session key to acquire a different member's store.
History can restore a displayed agent and recorded store, but cannot grant a
new private assignment. Recent-session restore, explicit resume, dormant-slot
rehydration and channel surfacing require an existing protected session binding
before a V2 turn. This also applies when an empty historical agent now resolves
to a private configured default. An unresolved member, changed binding or
unreadable private store surfaces an error instead of borrowing Global Memory V1.

An owner creating a private chat pins the selected member before saving history.
Opening a member from Members can pin its canonical session after positive owner
authorization. A transcript's linked key and the legacy DM binding file cannot
authorize another session; a colliding member slug needs an existing protected
match. Uninitialized legacy members remain openable for explicit initialization.
An explicit owner agent choice on an unbound restored ordinary chat admits its
next turn only after the existing switch rollback checks succeed. Restarting
before that turn requires the owner to choose again. Internal callers and
restored metadata cannot perform that admission.

Cron tabs require the protected assignment published by private cron dispatch.
A legacy job's provider-template alias cannot become a private member on a
dashboard follow-up; if it resolves to V2 without that assignment, the turn
refuses and explains how the owner can select a member explicitly.

Speculative eager allocation stops for every private V2 binding, including
resume prefetch. The actual turn verifies the protected binding and persists
the slot before provider allocation.
It also stops when an explicitly selected member is unresolved, when a restored
store disagrees with the current resolver, or when its declaration is
unavailable or inconsistent, and leaves the user-facing explanation to that
turn. Global Memory V1 and a valid named V1 declaration retain speculative
eager allocation. Store identity is part of the eager binding snapshot, and
slot replacement, a running real turn or any binding change after an awaited
lookup makes the eager task stand down before allocation.

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
`ACP_BACKENDS_ACP_RUNTIME`, never an inequality (harness parity):

- **runtime-capable backend** (`ACP_BACKENDS_ACP_RUNTIME`) — each caller (title
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
  misdescribe it) and is logged at WARNING as a diagnostic instead.
- **Context compaction**: at ≥ configured threshold (`session.autocompact_pct`, default 70%, valid 5–90), compacts **in place** on a backend that can serve
  `/compact`: kiro-cli via a `/compact` **prompt** (`session/prompt` +
  `_kiro.dev/compaction/status` watch — never the string form of
  `_kiro.dev/commands/execute`, which kiro-cli 2.14.0 exits rc=0 on),
  claude via SDK `/compact`. A backend OUTSIDE `ACP_BACKENDS_COMPACT` is
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
- **Post-semaphore revalidation** (`_reacquire_and_validate`): the per-session
  semaphore may be held for a full turn, so it is ALWAYS acquired with the
  global `self._lock` RELEASED (pinning the lock across that wait would freeze
  session creation for every key and reintroduce a lock-ordering deadlock).
  Because a session can be recycled/removed, its backing process can die, or its
  project directory can change while a caller waits on the semaphore, every reuse
  path re-checks identity + liveness + BOUND CWD AFTER acquiring it, through the
  single shared helper
  `_reacquire_and_validate(key, sess, *, cwd=None)`. Its contract: it returns `True` with
  the semaphore **still held** (caller MUST `release`), or `False` having
  **already released** it (session went stale — caller evicts via
  `_evict_stale_session` and cold-starts). The cwd dimension is validated HERE and
  not at the reuse decision, because that decision runs before the semaphore is
  claimed and evicting there could tear down a turn still streaming. Both sides of
  that comparison are normalized through `str(Path(...))` — the request AND the
  provider's reported binding — so the result cannot depend on which side happened to
  be canonicalised already, and a caller naming a stable directory (trailing slash,
  doubled separator, or a platform whose `Path` rewrites the separators) cannot churn
  a warm session every turn. A caller
  passing no `cwd` states no requirement, so it cannot trigger a mismatch — which is
  why a teardown that has to be REFUSED additionally ARMS THE KEY via
  `mark_retire_on_next_claim`: a cleared project cannot be expressed as a cwd
  requirement, so the arm, not the directory, is what stops the next claim being handed
  the stale binding. The arm is keyed by STRING rather than by session object because a
  cold start holds no registry entry until it finishes, so "nothing registered" can mean
  a provider bound to the pre-change directory is already on its way; the key is
  consumed as it is read, costing one cold start rather than refusing that key forever.

  Alongside the arm, each key carries a MONOTONIC GENERATION (`RetireArm.generation`),
  bumped by every project change — whether it arms (`mark_retire_on_next_claim`) or
  not (`note_project_change`, which the agent- and workspace-switch handlers call
  because they commit a new project and tear the session down directly, recording the
  directory they committed so an evicted start's RETRY binds that rather than the one its
  own frame carried). A cold start
  snapshots the generation before its first `await` and is refused at registration if
  the key has moved past it. This exists because the DIRECTORY a claim states cannot
  order two claims: a start begun before the change and a slot recreated under the same
  name after it both name something other than the armed target, so only the generation
  separates them. Supersession of the arm ITSELF is a different and simpler mechanism: both
  producers write the arm and bump the generation in one statement block, so a later change
  to the same key OVERWRITES the earlier arm in the map and there is no stale entry for a
  stamp comparison to find. An earlier version carried a per-arm generation stamp and a
  `_live_arm` reader that dropped superseded entries; that branch was unreachable by
  construction and has been removed, so nothing here reads a stamp.

  Why not fold staleness into the KEY instead. The obvious alternative removes this whole
  mechanism by construction: put a binding epoch in the folded session key, so a stale start
  registers under a dead identity and needs no arm, no transfer and no settle loop. It loses
  on the key's ownership, not on elegance. The folded key is a PERSISTED, externally owned
  identity: a channel member's `agent.session_key` and a cron job's `session_key` are written
  to disk and re-read by a later process, and a Slack-born key is a thread timestamp its own
  surface maps back to. Appending an epoch changes the key every time a project moves, so
  every record already persisted under the old spelling stops resolving — and the failure is
  silent, because a key that no longer folds onto a live session reads exactly like a session
  that has ended. `_fold_key` also already carries a compatibility burden for that reason:
  it resolves exact, then `canonical_key`, then `legacy_key` onto one live entry. An epoch
  adds a second unbounded dimension to that same lookup, and every alias spelling would need
  its epoch to agree.

  It also cannot cover the case the arm exists for. A cold start caches its resume SID BEFORE
  it registers, so at spawn time there is no entry to name — an epoch would have to be decided
  by the spawner and could be superseded during the start it was meant to fence, which is the
  race itself rather than a fix for it. The per-key arm is consulted at REGISTRATION, after the
  change has landed, which is why it catches that start. So the arm map is the compatible
  shape, not the preferred one: it keeps the key a stable identity that persisted records and
  external surfaces can still resolve, and pays for that with a fencing protocol whose
  producers must self-classify. `_ARM_KEY_SETTLE_PASSES` and the park flag are the cost of
  that choice and are named here so a later reader can price the alternative honestly.

  This counter is NOT the existing `ownership_generations`, and the two cannot be unified,
  for two reasons visible in their keys. `advance_ownership_generation` buckets by
  `_generation_key`, i.e. `canonical_key(key)`, a bucket deliberately SHARED by a key's
  canonical and legacy Slack aliases; the arm's counter is per FOLDED key. Sharing one
  counter would therefore let a project change on one alias refuse an in-flight start on a
  sibling alias whose project never moved. They also advance on unrelated events: ownership
  advances when a logical key CHANGES HANDS, while this one advances when a key's PROJECT
  changes, and it must survive `spend()` — the arm's directory and agent drop while the
  counter stays, because a start compares against it at registration. Folding them together
  would make an ownership transfer read as staleness to starts that were never stale, and a
  project change read as a change of owner to every claim that consults ownership.

  The spend/keep matrix is asymmetric and deliberate, and it follows from what each
  teardown does to the SESSION MAP rather than from a per-path preference. The arm names
  a directory a SUCCESSOR must bind, so it may only be dropped where no successor of that
  slot can arrive. A refused reset never spends the arm (the reason for the teardown has
  not gone away); a landed reset does not either (the arm is keyed on the target
  directory, so the eager respawn is reused rather than torn down). `destroy` DELETES the
  session-map entry and `close_all` ends the process, so both spend it — the only claim
  left for it to apply to would be a different slot recreated under the same name, which
  would silently inherit the previous project. `remove` and `remove_if_unclaimed` PRESERVE
  that entry, so they spend nothing: the arm is still owed to a real claim, and it doubles
  as the retry target for a start the cleanup evicts, whose own frame carries only the
  pre-change directory. That keeps the arm alive past the SLOT, though — the close and sweep
  paths both run `remove` — and a recreated slot only overwrites it if the user happens to
  change its project, which a fresh tab accepting the default never does. So the arm cannot
  be left for the next occupant to supersede; slot CREATION drops it.

  Two allocation-layer primitives drop it. `spend_retire_arm(key)` is called only from
  `destroy`, whose contract is a teardown that ends the slot generation.
  `supersede_arm_for_new_slot(key)` is called only from slot creation's mint path, on the
  grounds that a freshly minted slot has never been armed, so any arm on its key belongs to
  a previous occupant; slot REUSE returns earlier and so keeps the retry target `remove`
  preserves the arm for. It bumps the GENERATION rather than dropping it, because clearing
  would let a start still in flight from the previous occupant compare equal to a fresh
  key's zero and bind here. The keep-side paths deliberately call nothing: keeping the arm
  IS the absence of a call, so a no-op primitive existing to be countable by a census would
  be dead weight. What ratchets a NEW teardown path onto one side is
  `test_only_slot_ending_teardowns_spend_the_retirement_arm`, which asserts the keep-side
  as an absence. The key's GENERATION survives every teardown here, which is what still
  refuses a start already in flight.

  **The `linked_session_key` writers are serialized against the settle.** The settle was
  probe-then-act: it re-read `linked_session_key` a bounded number of times
  (`_ARM_KEY_SETTLE_PASSES`), which damped the race without closing it, because a rebind
  landing after the last pass stranded the arm on the abandoned key. That is the shape this
  repo's own rule rejects — a probe followed by a separate act cannot protect a resource whose
  state changes between the two — so it is closed here rather than recorded as owed.
  `settling_key` excludes the whole settle-and-arm region; `bind_linked_session_key` is the
  single writer and PARKS a rebind arriving inside it; `key_rebind_deferred` reports that one
  is pending, and an unsettled settle publishes no arm and RETRACTS the source arm rather than
  leaving it owed to a binding nobody is on. All eight assignment sites across five modules —
  `dashboard/handlers/cron.py`, `dashboard/chat_persistence.py` (three), `dashboard/state.py`
  (two), `dashboard/workflow_inject.py` and `dashboard/cron_inject.py` — route through that
  single writer.

  Deliberately NOT the slot lock, for two independently disqualifying reasons: it is not
  reentrant and one transfer already runs inside it, so a second acquisition would never
  return; and seven of the eight writers are synchronous functions, which cannot acquire an
  asyncio lock at all. Deferring rather than blocking also cannot deadlock, which a lock
  taken by a writer that already holds it could.

  WHAT REMAINS, and what does not. Writer-versus-writer ordering is deliberately not imposed
  and is not owed: a binding is a latest-value fact, so writers racing outside a region
  resolve last-write-wins and only the newest key parked inside one survives the unwind, which
  is the intended semantic in both cases. A NINTH surface sets the field without naming the
  attribute — `get_or_create_slot`'s `linked_session_key` argument, which every channel and
  cron surfacing passes. It needs no guard because the factory returns an existing slot before
  reaching the bind, so it can only bind a slot it just minted, where no arm can yet be
  settling. TRIGGER: any new writer must route through `bind_linked_session_key`. The writer
  census in the settling tests fails a module that reaches the field by a dotted assignment
  without it, and is structurally blind to the keyword form, so that surface carries its own
  control rather than relying on the census.

  WEIGHED ALTERNATIVE — every claim states its own requirement. Instead of a store the
  producer writes and the claim reads, each claim could carry the cwd and agent it requires and
  be refused when the live session does not match; `CWD_CLEARED` already makes "cleared" an
  expressible requirement, so the vocabulary exists. That shape is genuinely smaller in state:
  it removes both pending maps and leaves only the generation counter.

  It is NOT taken here, for one reason that is structural rather than a matter of taste. The arm
  covers a session that is NOT YET REGISTERED: a cold start holds no registry entry until it
  finishes, so a claim-time comparison has nothing to compare against while a provider bound to
  the pre-change directory is already on its way. The requirement would be stated by the very
  claim that is in flight, which is the window the arm exists to close. A claim-stated
  requirement therefore cannot replace the arm; it can only replace the part of it that a
  registered session already covers.

  Two costs beside that. Every claim site becomes a writer of the requirement, and the channel
  dispatch in `messaging/dispatch.py` calls `get_or_create` with no cwd at all today, so the
  blast radius is every caller rather than one subsystem. And a requirement stated per claim is
  re-derived by each caller, where the store derives it once at the moment of change.

  What would make the alternative win: a registry that records a cold start BEFORE its provider
  binds, so a claim has something to compare against for the whole window. With that in place the
  arm store's remaining job is only the generation counter, and this section should be revisited.

  **OWED FOLLOW-UP — record a cold start in the registry BEFORE its provider binds.** This is
  now the ONLY open root cause the arm store compensates for rather than removes; the other —
  the unserialized `linked_session_key` writers — is closed above. Closing this one at cause
  means recording the key when the start is REQUESTED, not when its provider binds, so a claim
  has an entry to compare against for the whole window. Until it lands, a claim arriving inside
  that window has nothing to compare against, so correctness rests on the producer having armed
  before the start was requested. TRIGGER: this is owed before any new caller can start a
  session outside the arming producers named here. Landing it would shrink the arm store's
  remaining job to the generation counter alone, which is why no further producer should be
  added to the arm store before it: each one that is makes this replacement more expensive,
  not less.

  Two properties of the arm's LIFECYCLE are load-bearing and neither is local to the
  allocation layer, so both are recorded here rather than as comments at their sites.
  It is raised by the PRODUCER, synchronously. A project change defers the RESET — the
  endpoint is reachable over loopback HTTP from inside the kiro-cli process group, so an
  inline teardown would `killpg` the caller — but deferring the ARM as well would leave a
  window: the consumer runs behind the eager task's 1.5s debounce, and a channel turn
  arriving inside it states no directory, so the claim-time cwd check cannot fire and the
  arm is the only thing that would refuse the pre-change session. Arming is in-memory
  bookkeeping and carries none of the `killpg` risk that forced the reset to be deferred,
  so the two are deferred separately: the reset waits, the arm does not.

  It is satisfied by a BINDING, not only by a stated directory. A claim that names no cwd
  cannot state agreement with the armed target, and channel turns are exactly that shape —
  the messaging dispatch builds its `get_or_create` kwargs with `model` and nothing else.
  Gating satisfaction on a stated cwd therefore left the arm permanently unsatisfiable for
  that consumer: every channel turn read as retire-applies, evicted, and cold-started, so
  the change cost a cold start on EVERY turn instead of the single one it is supposed to
  cost. A registered session already bound to exactly the armed directory is the successor
  the arm was waiting for, whether or not the claimant restated that directory.

  Which makes the two producers NOT interchangeable, and this is the sharp edge. Both
  record the committed directory and bump the generation; `mark_retire_on_next_claim` also
  flags the REGISTERED session via `retire_on_identity_change`. Because satisfaction is a
  directory test, a change that moves the DIRECTORY is fully expressed by the arm alone —
  that is `note_project_change`. A change of IDENTITY is not: an agent switch on a
  project-scope agent keeps `slot.project`, so the arm names the directory the live session
  is already bound to, a cwd-less channel claim reads it as satisfied, and the
  switched-away agent serves the next turn. The identity flag is the only part of that a
  matching directory cannot express, and it survives the busy-deferred reset that leaves
  the old session alive. So: directory moved → `note_project_change`; session must be
  replaced whatever its directory → `mark_retire_on_next_claim`.

  Two arms, two questions -- but ONE spend. `cwd_satisfied` and `agent_satisfied` are
  independent predicates, and each is the only thing that can answer its own half. They are
  not independent SPENDS: an arm is dropped exactly when the claim is ACCEPTED, never one
  half at a time. Spending each on its own answer loses the directory requirement on a
  claim the same frame is about to REFUSE, and the case is ordinary rather than exotic. A
  project-scope agent switch keeps `slot.project`, so it arms the directory the session is
  already bound to plus the agent a successor must run; a cwd-less claim then satisfies the
  directory half by its binding while the identity half cannot be satisfied at all. That
  claim is refused -- and if the directory arm went with it, the SUCCESSOR is unguarded: it
  can bind any directory, nothing refuses it, and its relative writes land outside the
  selected project with no recovery path.

  What an empty cwd resolves to is per-SESSION, not shared. The provider factory resolves a
  falsy `cwd` to `workspace_root()/<key>`, which is a different root from
  `config_dir()/"workspace"`, so `resolved_cwd` has to be told the key or it answers a
  directory no provider ever binds. Two harms follow from the divergence, and they are the
  two `default_workspace_dir` exists to prevent: a cleared-project claim never matches its
  own binding, so the slot cold-starts every turn or exhausts its retry budget and wedges;
  and anything that BINDS that answer puts sessions meant to be isolated in one directory,
  where their relative writes overwrite each other. `session_default_cwd` is the one symbol
  both the factory and the resolver go through, so the agreement is an invariant rather than
  a convention -- keyless callers keep the shared answer, for the paths that genuinely share
  one workspace.

  A REBIND is the third producer shape, and it needs a MOVE rather than a write. The
  deferred reset arms the key the producer could see; if the slot rebinds before the
  consume, its turns run under another key and both facts are true at once. Arming the
  live key alone leaves the abandoned key's arm in the map, and nothing drops it — an arm
  is cleared by the claim that satisfies it, and no claim arrives under a key the slot
  left. That residue is not inert, because the map is keyed by STRING: a later session
  registered under the same string, a channel re-link reusing the id or a recreated slot
  of the same name, reads an arm naming a directory chosen for a binding that no longer
  exists. So `_consume_pending_reset` calls `transfer_retire_arm`, which arms the live key
  and clears the abandoned one in one synchronous step, carrying the agent requirement
  across because only the arm states which agent a successor must run. It is distinct from
  `spend_retire_arm`, whose contract is a teardown that ENDS the slot generation and so
  drops an arm no successor is owed; here the arm is still owed and only its ADDRESS was
  wrong. The abandoned key's GENERATION stays, for the reason `spend_retire_arm` keeps its
  own: that counter refuses a start still in flight under the old key, which a rebind makes
  more likely rather than less.

  WIRE CONTRACT CHANGE — a project set during a rebind now SUCCEEDS. `api_chat_slot_project`
  previously refused with `409` when the slot rebound between the request's gate and its commit,
  on the ground that arming the new key would arm one the app gate never authorized. It now
  answers `200` and transfers the arm, because that ground is addressed rather than assumed: the
  live key is re-authorized through `_app_cancel_denied` before any arm moves, and a denial
  retracts the source arm so no arm survives on either key. A caller that treated the `409` as
  "retry the set" therefore sees a success where it previously retried; the retry was always
  going to succeed on the next attempt, so the observable difference is one fewer round trip, not
  a different final state. An unauthorized rebind still refuses.

  An EQUIVALENT same-key re-arm keeps the generation. `_consume_pending_reset` is re-entered
  by the deferred-reset retry every few seconds for as long as sub-agents stay attached, and
  each pass transfers the arm onto the SAME key with the SAME target. Bumping the generation
  there refuses a cold start that has not finished resolving its own model, so a slow start is
  rejected on every attempt until the path gives up — the counter meant to refuse a STALE
  start starves a current one instead. Equivalence is deliberately narrow: same folded key AND
  the already-armed target equal to the requested one. A different target on the same key is a
  genuine change and must supersede, and the agent needs no comparison because the carried
  value is read from that same key. The registered session is still flagged on the preserved
  path — a session that registers between the first arm and a retry pass must honour the arm,
  which is the arm's whole purpose.

  A ROLLBACK is a producer too, and it is the one that gets the identity wrong. The agent
  switch arms BEFORE its awaits, so an abandoned switch leaves an arm naming a binding the
  slot no longer holds; leaving it there makes the arm permanently unsatisfiable, so the
  rollback re-points it. The subtlety is WHICH binding to name. The rollback unwinds each
  field on its commit token's IDENTITY, never on value, so an unlocked writer — the in-turn
  `/agent` directive, `members`, `openai_compat` — that wrote during the awaits keeps its
  value and the rollback stands down for that field. The arm must then name the PRESERVED
  agent, resolved, because the registration matches on `kiro_agent or slot.agent`: arming
  the prior agent's target refuses the next cwd-less claim for an identity the slot never
  names, and the retry re-points the session to an agent nobody selected. This is not a
  remote combination — the rollback path is reached precisely BECAUSE a turn is in flight,
  which is exactly when an in-turn directive lands. The same rule already governs the
  metadata restore beside it, which writes post-rollback `slot.agent` rather than
  `prior_agent` for this reason. Only when the rollback actually RESTORED the field is the
  prior agent's resolution the right answer.

  What the arm falls back to when the project is EMPTY. Both arm sites name
  `slot.project or <fallback>`, and the post-switch project is legitimately empty:
  `default_project_dir(workspace)` answers `""` for a workspace directory that is missing or
  sensitive (its own `is_sensitive_path` guard in `config/loader.py`). The fallback must
  therefore be the CLEARED
  per-session default, resolved through `resolve_arm_cwd(key, CWD_CLEARED)` — never a
  project, and in particular never the PRE-switch project. Resolving it from the pre-switch
  project made an agent switch into such a workspace arm the directory it was abandoning, so
  the next cwd-less claim bound the old repository and relative writes landed there with
  nothing to signal it. The resolution is UNCONDITIONAL: `slot.project` has writers that take no lock, so a clear
  landing during the handler.s awaits empties both candidate projects after any gate on them
  has already decided, and the synchronous arm would then resolve `""` on the event loop -- the
  one thing `mark_retire_on_next_claim` documents its callers must prevent. A rollback is a
  producer too, so its own arm must follow the revert rather than precede it.

  The resolution runs once both candidate projects are final and
  before either is committed, and it is not gated on either being empty — see UNCONDITIONAL
  above. It therefore costs one off-thread resolve per switch, and a failure answers
  `503 workspace_unavailable` after unwinding the agent commit that precedes it.

  What this writes to DISK, declared because it is a persistence change and not only an
  in-memory one. `project` and `project_cleared` are now written unconditionally rather than
  only when a project is set, because the ABSENCE of a key cannot distinguish "never scoped"
  from "explicitly cleared" — and that distinction is the whole of `claim_cwd`. A record
  written by an older build therefore carries neither key, and is read as never-scoped, which
  is the safe direction: it states no directory, keeps the warm pool, and cannot resurrect a
  clear that build never recorded. The merge is an upsert that cannot delete a key, so a slot
  cleared after `/old` was persisted still carries `/old` on disk; `claim_cwd` reads the
  cleared marker FIRST for exactly that reason. `resolved_cwd` (`config/paths.py`) is the one
  place a falsy directory becomes the concrete per-session default, so no caller has to know
  whether a provider was handed `""` or a path.

  What a CLEARED project costs at claim time. `CWD_CLEARED` names the per-session default,
  which no pooled child can be sitting in, so `cwd_blocks_pool` routes such a turn to
  `pool_decision = "bypass_cwd"` and it cold-starts through the factory. That cost is
  intended, not incidental: a warm hit would bind the shared pool directory and so serve
  precisely the stale binding this module exists to refuse. The decision is named in the
  claim's own telemetry, so the bypass rate is observable rather than silent.

  What bounds a refused clear. A member turn holding the session is not unbounded, which is
  why the refusal's "retry when idle" is advice that arrives: every ACP prompt resolves its
  wait from `agent.chat_turn_timeout_secs`, clamped to
  `[CHAT_TURN_TIMEOUT_MIN, CHAT_TURN_TIMEOUT_MAX]` = 300s..86400s — applied at
  `acp/session_handle.py`'s `_effective_prompt_timeout_async` and enforced by the transport
  wait on `_turn_done`. So the ceiling is the transport's, not the dashboard's: channel
  members are dispatched by `handlers_channel.py`'s `_spawn_agent_task`, a bare
  `create_task` that adds no ceiling of its own, so a member turn is bounded by that prompt
  timeout alone and by nothing shorter. That is the bound a wedged turn is released by, and
  the reason the endpoint refuses rather than forcing: an unconditional clear can only make
  the refusal disappear by discarding the state of a turn that is still running.

  What that bound COSTS, recorded because the refusal is a deliberate trade rather than a free
  one. The unconditional clear was also the only in-band recovery for a wedged member turn, and
  refusing it removes that: the channel route table carries `wake`, `approve`, `update` and
  `dismiss` for a member and no stop or interrupt, so the remaining remedies are dismissing the
  member — which discards the member, not merely its turn — or waiting out the prompt timeout,
  whose default is 14400s. A `force` parameter is deliberately NOT added, because it could only
  clear by discarding the state of a live turn, which is the harm the refusal exists to
  prevent. OWED FOLLOW-UP — a member-level stop that ends the TURN and leaves the member in
  place is the lever this trade is missing; until it exists the prompt timeout is the sole
  bound, and that is a choice a maintainer should make knowingly rather than inherit. TRIGGER:
  owed before any further surface is given a refusing clear, since each one widens a window the
  user has no in-product answer for.

  The queued reset's consume then reads `reset`'s return with that split in mind. `reset`
  answers `session is not None`, so a False is AMBIGUOUS: refused-because-busy, or nothing
  live to tear down. Only the first is a deferral. The second has already achieved what the
  flag asks for, and leaving the flag armed there would have a later consume tear down the
  session the eager spawn just created — paying the cold start that path exists to hide — so
  `has_session` disambiguates and the flag is cleared. Safety does not rest on that
  bookkeeping: the arm is raised BEFORE any branch and keyed on the CURRENT effective key,
  so it still covers an in-flight cold start, which is invisible to both probes precisely
  because it holds no registry entry yet. Where the session IS registered the flag stays
  armed and the bounded retry task owns the follow-up, because a channel-linked slot's turns
  cross no dashboard turn boundary and would otherwise never retry the decline.

  Flagging the registered session is not sufficient on its own, because a COLD START has no
  registered object to flag. Consider a channel turn that resolves its agent, then the
  switch lands, and only then does the turn reach `get_or_create`: its generation snapshot
  is taken AFTER the bump so the ordering test reads clean, and a project-scope switch left
  the directory alone so the arm's directory test reads clean too — yet the session about to
  register runs the agent the switch replaced. Nothing self-corrects, because the next
  satisfied claim spends the arm. So an identity arm also records the DESIRED AGENT
  (`RetireArm.agent`), and registration is refused when the registering session's agent
  is not it.

  Refusing is only half of it: a refusal has to leave the retry able to SATISFY it. The
  retry already re-points `cwd` at the arm for exactly this reason, and the agent is
  re-pointed the same way — a turn carrying the switched-away agent replayed unchanged is
  refused identically every time, so without the re-point the won-race budget runs out and
  the slot wedges. That failure is not hypothetical: it is what the first version of this
  guard did, caught by `test_a_post_switch_cold_start_cannot_register_the_old_agent`.

  Two constraints on the identity arm follow from that, and both were defects first.

  It must be COMPARABLE to the namespace the REGISTRATION reads, without freezing the
  mapping. A claim's agent is `kiro_agent or slot.agent` — an alias's resolved TARGET,
  preferred over the alias name — so arming the name the user picked cannot be compared
  directly for any alias whose target differs. The consequence is not a missed refusal but an
  inverted one: the correctly-resolved cold start is refused, and the retry re-points to the
  armed value, running the wrong identity. Arming the RESOLVED target instead fixes the
  comparison and introduces the mirror defect: the snapshot outlives the config, so an alias
  re-pointed during the arm window leaves the retry re-pointing at the OLD target. So the arm
  records the stable ALIAS and `resolve_runtime_agent` maps it to the CURRENT target at every
  consume site — the reuse comparison, the cold-start stale check, and the retry rebind. All
  THREE must accept both spellings: the retry re-points through the resolver, so a site still
  comparing the raw alias can never agree with it, and because the stale branch sets
  `retire_on_identity_change` the eviction KEEPS the arm — the retry then re-reads the same
  alias until the won-race budget runs out and the claim raises instead of starting.
  It resolves only a name the
  config DEFINES, because the binding resolver substitutes the default agent for an unknown
  one, which would answer a different identity rather than resolve this one; anything else
  degrades to the alias, exactly where the claim also degrades to `slot.agent`. The resolve
  is OFF-THREAD at every site, and not because it is expensive on average: the config load is
  cheap on a cache HIT, but any config edit makes the next one read, parse and schema-validate,
  and on the loop that stalls every other session's turn and the heartbeat with it. Each site
  resolves BEFORE its critical section — the claim's decision window takes no await, and the
  cold-start check runs under the registry lock, where a synchronous read wedges every session
  — then uses the pre-resolved value only while the arm still names the alias it resolved.

  And it must be satisfied SEPARATELY from the directory. `cwd_satisfied` is a directory
  test, and a project-scope switch keeps the directory, so letting a cwd match spend the
  identity arm defeats the mechanism on its own intended consumer path — the claim reuses a
  session still running the switched-away agent and the arm is gone. Each arm is answered by
  its own predicate, the session is retired unless BOTH are satisfied, and each is spent only
  by its own answer.
  TWO of the three arm maps are released together; the generation counter is not one of them.
  `remove` and
  `remove_if_unclaimed` — the session-level eviction paths — PRESERVE the arm deliberately,
  because it is still owed to a retry. What reclaims it is the FINAL slot teardown: the close
  handler and the archive sweep both call `supersede_arm_for_new_slot` once
  `_slot_still_ours` confirms the slot is theirs to end, so a transient key — a channel link,
  or a cron, workflow or subagent slot — stops leaving `RetireArm.cwd` and
  `RetireArm.agent` entries resident until process exit. It releases nothing a recreate
  would have kept, because the mint path is the same verb. `RetireArm.generation` is deliberately
  RETAINED and bumped rather than cleared, so a start still in flight from the previous
  occupant cannot compare equal to a fresh key's zero: one integer per key the process has
  ever armed stays resident by design, which is the price of that guard.
  The remaining releases are `spend_retire_arm` (inside `destroy`), `transfer_retire_arm` for a
  rebind, and the bulk `discard_all_retire_arms` that `close_all` runs.
  On
  `False` the helper evicts BEFORE releasing the permit: releasing first would
  leave the session registered with a free permit, letting another acquirer win a
  provider that the eviction then shuts down mid-command. Cancellation while parked on
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
  (`dashboard:{slot_key}`) idle-expire like any other session. The policy is
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
  `rss_threshold`, `stuck_turn`, and `bg_drain_reap`.
  `SessionCleanup._cleanup_loop` then directly coordinates the session-root,
  sandbox-artifact, bytecode-cache, periodic tracked-PID, and untracked-MCP
  sweeps.
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
  the shared `subagents_attached` predicate) right before `reset`, and a probe
  that raises counts as attached.
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

`stop_turn()` is the shared orchestration layer for both dashboard and Slack stop surfaces. Sequence:

1. `clear_queue(key)` — queue drop is unconditional on first press.
2. If `force=True`: skip cancel, go straight to hard kill (step 4).
3. Send `session/cancel` via `provider.cancel(wait_ack_timeout=budget)`:
   - `"acked"` → set `session.prev_turn_cancelled = True`, call `on_soft` callback, return `"soft"`.
   - `"no_turn"` → return `"idle"`.
   - `"timeout"` or `"error"` → fall through to hard kill.
4. Hard kill: `reset(key)` → fire-and-forget `_eager_respawn(key)` task → call `on_hard` callback → return `"hard"`.

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
`chat_utils.subagents_attached` predicate, because the parent turn ends before
its children do. The predicate fails closed on an unreadable probe: unknown
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
  returns whether rows were owed and reached disk, and every caller honours it —
  because this frame is the last reference to those rows, so nothing will retry and
  nothing else will ever report them. Both PRE-SAVE hand-over exits therefore turn
  a False into their path's own failure: `close_slot` raises
  `SlotCloseError(code="history_save_failed")` (the same code as an ordinary failed
  archive — from the caller's side it is one thing, a close whose history write did
  not land) and cleanup adds the key to `failed`. There is nothing to roll back on
  either exit, so the report IS the whole remedy; a 200 there would claim durability
  the close does not have. The two FAILURE-arm drains need no branch of their own:
  those arms already end in `SlotCloseError` / `failed.append(name)`, so a lost tail
  reaches the caller regardless, and the drain only decides whether the rows
  survived. Every failure is also logged with the exact row count, which is the only
  report anything in the process can still make about the rows themselves.

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
**"active in another process"** error. Recovery happens at the resume
chokepoint (`AcpProvider._load_session_with_retry`, `providers/acp.py`) and
self-heals regardless of *why* the resume failed — it never depends on the dead
holder cooperating (unlike cooperative drain), so it covers every kill mode:

1. **Phase 1 — bounded retry (lossless).** Re-issue `session/load` up to
   `_RESUME_MAX_ATTEMPTS` (4) times with exponential backoff
   (`_RESUME_BACKOFF_BASE_S` → 1s, 2s, 4s). If the stale lock releases, the
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
   injects history from `compress_thread_history()` (KiroCrew's conversation_log)
6. The flag is consumed (set to False) — replay fires exactly once per switch

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

**Tracking**: at session init, `AcpClient.ensure_ready()` snapshots all
descendant PIDs and persists them to `kiro_pids.txt` as `child_pid:parent_pid`
pairs via `_track_child_pids(pids, parent_pid=self._pid)`.  On clean shutdown,
`_reset_state()` removes them via `_untrack_child_pids()`.  If the gateway
crashes, the entries remain in the file for the next startup.

**Detection**: reads `kiro_pids.txt`, processes only `child:parent` lines
(bare PID lines are kiro-cli parents handled by `cleanup_orphaned_sessions()`).
If the child is alive but its parent PID is dead, the child is orphaned and
killed.

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
  `mcp_shared._resolve_excluded_tools` (policy walk),
  `mcp_caller.CallerContext.from_env` (host-pid + walk; also serves
  `mcp_gateway/stub.py`), and `mcp_gateway/gatewayd._resolve_peer_identity`
  (server-side peer walk). The sidecar is additive.
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
- **Private member API authority**: the trusted publisher also writes the live
  private process incarnation, session and immutable target store to
  `member-memory-bindings/pids/<pid>.json`, under the precreated sandbox-readonly
  root. Private V2 recall, lesson writes, and consolidation require a positive
  kernel peer/ancestor match to this record, or a live-process-bound delegated proof
  issued by the trusted MCP gateway after the same check. The shared internal
  secret and legacy writable sidecars alone grant no private member authority.
  A rekey, recycled process, unreadable record, or expired proof refuses the
  request. Proof signing material is under the sandbox-hidden `memory_stores`
  root; pooled backends receive proof only in the current call's trusted metadata.
  Global V1 publication leaves no private PID binding, preserving shared V1 tab
  behavior. A private session's first trusted preparation additionally pins
  `member-memory-bindings/sessions/<sha256-key>/memory.json`; later metadata must
  agree, including after restart. Neither erasing the metadata nor changing it
  to another store can change this permanent private identity.
- **Private runtime allocation**: every allocation resolves that trusted binding
  off the event loop before reusing a provider or claiming a warm process. Private
  cron, consolidation, delegated and interactive sessions bypass the V1 warm pool
  and cannot share an ACP runtime. A task with a private parent or target uses its
  own provider; a private parent with an unbound child refuses execution until the
  child's trusted memory binding is established. An already-live provider whose isolation differs from its binding
  is explicitly refused until the session is restarted. Global V1 allocation and
  sharing stay unchanged. MCP caller discovery checks protected process ancestry
  before cached identity, environment or legacy sidecars; a malformed protected
  record remains unresolved and never falls back to those legacy sources.
- **Threat model** (full version in the `session_pid_sig.py` module
  docstring): file forgery, cross-pid replay, tampering, and symlink
  planting are blocked; deliberate same-uid impersonation via
  attacker-chosen env in self-launched processes is out of scope (identical
  capability exists against env-only resolution) and is tracked as the
  SO_PEERCRED gateway-authentication follow-up (issue #302).

### Stateless session-directive tools (`session_directive.py`, #755)

Seven session-bound MCP tools — `monitor_start`, `monitor_update`, `autonudge_stop`, `set_project`, `suggest_followup`, `ask_question`, `reset_conversation` — used to resolve their OWN session identity (the strict sidecar resolver above) and call a loopback HTTP endpoint, which only produced a usable per-call caller when MCP-gateway **pooling** was enabled. They are now **stateless**: the tool validates its arguments and returns a *directive* — a human-readable confirmation line plus a machine-readable marker (`session_directive.encode`) carrying the validated payload and NO session key. The session-aware consumer, `dashboard/chat_runner._run_chat`'s `EVENT_TOOL_RESULT` handler, decodes the marker (`session_directive.decode`) and applies the effect IN-PROCESS against ITS OWN `slot`/`session_key` via `dashboard/session_directive_apply.py`, then strips the marker from the stored transcript. This works with pooling OFF (the default) because the consumer already owns the session, so no per-process identity source is needed.

Subagent isolation is therefore **structural, not cryptographic**: a subagent's tool result flows through the subagent's own runner and can only ever bind to the subagent's session, never its parent's — there is no `/proc` walk to get wrong. The tools still call `_resolve_session_key_strict()`, but only as a context guard to short-circuit sessions where a directive can never be applied (cron/hook/subagent) and to steer non-`dashboard:` `ask_question` callers to the `[OPTIONS:]` tag — not to bind the effect.

Security properties (enforced in `session_directive.decode` plus the applier):

- **Forgery gate keyed on canonical identity**: because the marker is model-visible (it returns as the tool-result text), a directive is honoured ONLY when the tool call was recorded — via kiro-cli's out-of-band `_meta` channel — as an MCP call whose canonical `_meta.kiro.toolName` (with `_meta.kiro.mcpServerName` set) is in `DIRECTIVE_TOOLS`, never the LLM-authored `title`. A shell command titled `monitor_start` whose stdout forges the marker resolves to no directive tool and is ignored; the gate fails closed when `_meta` identity is absent.
- **Native sub-agent calls refused**: they surface as flat events in the parent loop but have no independently bindable slot, so the applier declines them.
- **SEL audit on every application**: `apply_session_directive` emits a tool-invocation event tagged `source="mcp-directive"` with outcome `success` / `denied` (e.g. a `set_project` sensitive-path block) / `error`, since the effect now runs in the consumer rather than in the tool body or an HTTP endpoint.

The applier reuses the SAME effect cores the HTTP endpoints call — `authorize_and_add_nudge` / `authorize_and_update_nudge` / `svc.remove` for the monitor trio, `slot.project` plus the recent-projects save for `set_project`, `deliver_ws_owners` for `suggest_followup`, and `post_question_card` for `ask_question` — so behavior is unchanged except that `ask_question` is now non-blocking (full contract in `learn-cron-dashboard.md` → "Agent Questions"). `reset_conversation` is the one directive whose core is not reachable inline: it queues `SessionManager.discard_conversation` on the slot for `chat_runner._consume_pending_reset` to apply at a turn boundary, because the discard is a full provider teardown and the producer is mid-turn — the same deferral `set_project` uses, and the reason the immediate route (`POST /api/chat/slots/{slot}/reset-conversation`) answers 409 on a busy slot rather than tearing down a turn mid-write. It queues the session key THIS TURN ran on, passed in by the consumer, never re-resolved from the slot: `linked_session_key` is mutable, so a cron or workflow injection that rebinds the slot between the request and the consume would otherwise discard whatever the slot points at by then and leave the caller's conversation alone. Only the END-OF-TURN consume may apply a discard (`allow_discard`); the two earlier consume points run just before a turn acquires the session, where a teardown lands under a channel turn already streaming on it. Even at that boundary it does not assume: the discard goes through `discard_conversation(..., skip_if_busy=True)`, which refuses under the same session lock that pops the session, mirroring `reset`'s own guard. Probing from the consumer and tearing down afterwards would leave a window in which a channel message acquires the session's semaphore and begins streaming a reply the teardown then destroys — and the semaphore is the stricter signal anyway, since `provider.has_active_turn()` cannot see a turn holding the semaphore with no prompt in flight yet. A refusal returns False and changes nothing, replay flag and session map included, so the consumer leaves the flag armed for a later boundary. The sid clear runs in the SAME tick as the pop, with no await between them — deferring it past the shutdown awaits lets a concurrent channel turn map a SUCCESSOR session under the key while the old provider is still shutting down, and the clear then erases the successor's pointer instead of the discarded one. Sub-agent children are the other wait — `discard_conversation` releases the shared runtime they run on, so a running or queued child, or an in-flight completion-event delivery, also leaves the flag ARMED rather than killing the child's work. Both that consume and the route's 409 read one predicate, `chat_utils.subagents_attached`, so the two cannot drift. The queued flag is in-memory slot state: a gateway restart while it sits armed drops the reset the confirmation promised, which is accepted rather than persisted — the cost is one un-applied reset the caller can ask for again, against durable state for a transient intent. `set_project` and `reset_conversation` additionally require structural user-turn provenance: injected cron, task-runner, sub-agent, auto-nudge, orchestration, app-authenticated unattended turns, and app-authored Spec Builder seed/handoff prompts cannot retarget a borrowed destination slot even when its session key is user-facing. Spec Builder rejects app-token message and decision submissions before they can enter its human-provenance relay or durable decision ledger. Queue entries preserve this provenance, replacement text adopts the editor's provenance, and mixed or untagged merges fail closed.

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
