# Model fallback

`agent.fallback_model` controls an optional retry after an active model exhausts
its transient-error budget before activity begins. The implementation is
Kiro Crew-side: `llm_helpers.advance_fallback_candidate` changes the model through
the substitute `set_model` seam, and `test/test_llm_helpers_fallback_chain.py` pins that a
successful swap is observable before it is recorded.

On the ACP provider that seam is `AcpProvider.set_model`, so every fallback swap
and every primary restore re-applies the slot's reasoning effort to the newly
selected model (`reapply_live_effort`); an effort re-apply that fails is logged as a
warning and does not fail the model change.

## Configuration

`AgentConfig.fallback_model` is normalized by
`config/sections_agent.py:coerce_fallback_model`. `llm_helpers.configured_fallback_chain`
is the shared derivation used by the fallback callers:

- The automatic-routing sentinel is the field default; its configured value
  produces a chain containing that sentinel.
- A concrete model value produces a chain that tries the configured value before
  the automatic-routing sentinel.
- An empty value produces an empty chain, so the fallback branch remains inert
  and the existing terminal error path handles the failure.

`TestFallbackModelLoad` in `test/test_config_loader_agent_section.py` and
`TestCoerceFallbackModel` in `test/test_role_models.py` pin the default,
normalization, concrete-value ordering, and empty-value opt-out. The dashboard
PATCH schema in `dashboard/handlers/core_config_schema.py` applies the model-id grammar and
`_validate_role_model`; that validator allows the automatic and empty values,
rejects a known unusable concrete value, and permits a concrete value when no
advertised-model set is available, except that on an `acp`-namespace backend it
refuses a registered model with no kiro id (`model_registry.lacks_acp_id`).

## Trigger and candidate walk

The fallback path is eligible only after the normal transient retry budget is
spent on a transient error with no qualifying prior activity. Each surface
tracks that condition in its own stream loop: `llm_helpers.stream_and_collect`
requires no result text or tool activity, `dashboard/chat_runner.py` requires
no emitted or thought activity, and `subagent_manager/run_turn.py::_run_inner_impl`'s nested `_stream_with_transient_retry`
requires no activity. Post-activity recovery does not enter the model-fallback
walk.

`llm_helpers.advance_fallback_candidate` owns candidate selection for all three
paths. It preserves the original primary from an existing provider marker when
available, otherwise from the active model. It then skips the primary, the
currently active model, and candidates absent from a known advertised-model
set. Membership and both skips are judged through `resolve_pin_spelling_on` with
the provider's backend, so on a backend that advertises pair ids a bare chain entry
folds to the advertised spelling, and without a backend the generic fold applies (a
persisted entry carrying a stale `<namespace>::<bare-id>` qualifier still matches a
backend that advertises the bare id); an entry absent under both spellings stays
skipped. An unavailable advertised set does not reject a
candidate; this is load-bearing because entitlement cannot be determined
without that set.

The helper calls the substitute `set_model` path and verifies that the serving
model changed before it records the candidate, publishes `TURN_FALLBACK_ATTR`,
or logs the swap. This witness prevents a non-raising no-op model selection from
being announced as a fallback. The wire call, the witness, the marker, and the
walked/active records all carry the advertised spelling of the candidate
(`_fallback_wire_spelling`); the chain's own spelling drives only the walk
bookkeeping. `TestSetModelWitness`,
`TestAdvanceFallbackCandidateAutoPrimary`,
`TestAdvanceFallbackCandidateNamespacedChain`, and `TestFallbackState` in
`test/test_llm_helpers_fallback_chain.py` cover no-op handling, marker-seeded primary
selection, advertised-model filtering, spelling folds, and chain progress.

`FallbackState.should_retry_active` owns the per-candidate retry allowance, and
`fallback_rewound_transient_budget` derives the dashboard counter from the same
allowance. The pinning tests in `test/test_llm_helpers_fallback_restore.py` and
`TestRunChatModelFallback` in `test/test_dashboard_chat.py` prevent the three
surfaces from granting different candidate budgets.

When no candidate can advance, `FallbackState.exhaustion_story` describes the
candidates actually tried. `stream_and_collect` and the sub-agent attach that
story to the terminal exception for their delivery paths; the dashboard retains
its slot-local walked candidates to render its terminal error. `fallback_story_of`
redacts and bounds the attached story centrally, and `append_fallback_story`
uses it for cron alerts, sub-agent errors, and heartbeat failure logs. Central
handling is load-bearing because fallback model values are configuration input
and each error surface must receive the same safe text.

## Surfaces

- `llm_helpers.stream_and_collect` accepts a caller-supplied fallback chain.
  The Slack gateway passes `configured_fallback_chain()` for its cron and
  heartbeat work, then `annotate_model_fallback` prefixes a delivered result
  while the provider marker remains active.
- `dashboard/chat_runner.py` swaps through `_fallback_swap_for_turn`
  (`dashboard/chat_turn/model_fallback.py`, which owns the dashboard's four swap and restore
  helpers named below) after its pre-activity transient branch; `_on_acp_transient_exhausted`
  (`chat_turn/acp_recovery.py`) persists a notice and requeues the same message as a synthetic
  recovery item. The swap's condition excludes nested prompts. On exhaustion `_on_acp_error_card`
  (`chat_turn/turn_failures.py`) renders the slot-local walk in the terminal error.
- `subagent_manager/run_turn.py::_run_inner_impl`'s nested `_stream_with_transient_retry` follows the zero-activity branch,
  uses the shared candidate helper, and applies `annotate_model_fallback` to
  the completed result.

`annotate_model_fallback` redacts model values before rendering and leaves the
marker in place. That persistence is load-bearing: subsequent successful turns
on the fallback remain visibly identified until restoration succeeds.

## Sticky restore

A successful swap is sticky on the provider through `TURN_FALLBACK_ATTR`.
`probe_fallback_restore` makes one start-of-turn attempt to restore the primary
for unattended callers. It keeps the fallback on a transient restore failure;
if the session no longer serves the recorded fallback, it clears stale state
without selecting a model.

The dashboard adapter,
`chat_turn/model_fallback._probe_fallback_restore_for_slot_locked`, applies the same probe to
slot-held state. It snapshots `slot.model` and `_model_pick_gen` when fallback
activates. Explicit single-slot and bulk model picks, plus a provider switch,
increment the generation; a rejected single-slot pick restores its prior
value. A changed generation makes the fallback record stale, so restore cannot
override an explicit user choice. When automatic provider backfill changes
`slot.model` without changing the generation, the restore hook reinstates the
activation snapshot before clearing fallback state. This prevents an automatic
fallback selection from becoming a persistent user pin.

`TestRunChatModelFallback` in `test/test_dashboard_chat.py` and
`TestSlotProbeWrapsSharedRestoreBody` in `test/test_dashboard_chat_model_pick.py`,
together with the restore tests in
`test/test_llm_helpers_fallback_restore.py`, pin the stale-record, explicit-pick, and backfill
invariants.

## Refusal fallback (`agent.refusal_fallback_model`)

A second, independent fallback with a different trigger: the model's CONTENT
FILTER declined the turn (`STOP_REASON_REFUSAL`), which the transient ladder
never enters because a refusal is deterministic for the model that issued it.
The premise of this feature is that refusal is NOT deterministic across model
families — a different model routinely accepts what another's filter declined
— so the dashboard retries the declined message once on a configured model.

- `AgentConfig.refusal_fallback_model` is normalized by
  `config/sections_agent_fields.py:coerce_refusal_fallback_model`: the empty default
  disables the feature (junk input collapses to disabled, never to enabled);
  the automatic sentinel defers to the `recommended_model` the provider's
  refusal envelope names, resolving to disabled when it names none; a concrete
  id is used directly. The dashboard PATCH schema applies the same grammar and
  validator as `agent.fallback_model`.
- `chat_turn.model_fallback._refusal_fallback_retry` (called with the turn) gates the
  retry: interactive prompts only (no nested depth, and only a turn whose
  ledger actor is `user` — a cron, autonudge, or sub-agent wake keeps the
  terminal refusal, since nobody attends its announced swap), not while a
  Stop is suppressing requeues, never while a USER
  follow-up is already queued (`_has_user_queued_followup` — user speech in
  the queue is the user's next
  intent, often a correction of the refused message, and the replay's
  index-0 insert would jump it; a queued cron notification or sub-agent
  completion is orchestration and does not suppress the retry), and at most
  once per user message
  (`slot._refusal_fallback_attempted`) so a refusal from the fallback too is
  terminal and renders the ordinary card plus a fallback-also-declined note.
  The Stop and queue gates are re-checked after the swap lands: a stop or a
  follow-up arriving during the `set_model` await unwinds the swap through
  the witnessed restore helper and surfaces the refusal instead. Both gates
  read the monotonic `_stop_pressed()` counters alongside the stop-state
  check, so a Stop that pressed and already resolved back to idle during
  the turn (or during the swap await itself) still suppresses the retry —
  the state check alone would miss it, and the enqueue-time stop-generation
  snapshots would bake the moved counter in, blinding the drain purge.
- `chat_turn/model_fallback._refusal_fallback_swap` performs one explicit hop through the
  substitute `set_model` seam with the same silent-no-op witness as the chain
  walk, records `(primary, candidate)` on the SLOT (deliberately not
  `TURN_FALLBACK_ATTR`, whose start-of-turn probe would restore the primary
  before the retry ran), snapshots `_model_pick_gen` so a later explicit pick
  is detectable at restore, and refuses a candidate equal to the refusing
  model. When the session is unpinned (the automatic-routing sentinel, so the
  active model reads as unknown), the swap runs only when the sentinel is
  advertised as a switch target: the restore leg would have to
  `set_model("auto")`, and a partition that does not serve the sentinel
  refuses it, which would strand the session on the candidate for every
  later turn. Unprovable return leg → the refusal surfaces unswapped. A
  CHAINED swap — a second refusal arriving while a kept record still
  names the true primary (the prior restore failed or silently no-oped) —
  preserves that recorded primary instead of overwriting it with the stale
  fallback the session is stranded on, so the eventual restore returns to the
  user's real model rather than to fallback A. While the record is live,
  both `slot.model` backfill sites (which write only a slot whose mate
  pins a model, see model-selection.md) treat the
  session like an active throttle fallback: the provider's resolved model is
  the temporary candidate and must never be persisted into the durable pin,
  and usage rows attribute to the model that actually served the turn.
- The retry is requeued at queue index 0 as a synthetic recovery item, in one
  of two shapes decided by `_turn_tool_calls`. A turn refused before it
  dispatched any tool replays the declined message verbatim, carrying its
  attachment lists in their original typed form (`files` and `dirs` stay
  distinct meta keys, so a folder attachment retries as a folder); streamed
  text alone does not change that. A turn refused AFTER dispatching a tool is
  continued instead: the fallback model, in the same session, receives
  `_REFUSAL_FALLBACK_RESUME_MSG` as a CONTINUATION payload with no attachments
  (the original turn already delivered them), because replaying the message
  would run its side effects twice. Because a verbatim replay is the
  user's own words, it cannot be
  recognized by the fixed synthetic-recovery texts — and not by text at
  all: the drain redacts credentials and exfiltration URLs after the swap
  records the raw message, so string equality breaks for exactly the
  content most likely to draw a refusal. Recognition is the drained
  entry's queue id, matched by the drain (the entry must drain alone; a
  merge folding user input into the dispatch is a correction, not the
  retry) and passed into the runner as a parameter. The consuming turn
  re-validates before honoring it — recorded binding, slot and session
  stop generations, pending steers, queued user follow-ups — because the
  drain's own checks run before the turn task is spawned and a Stop,
  correction, or rebind can land in that window; a tripped check aborts
  the replay with the same cancellation notice the drain purge uses, the
  allowance stays spent, and the model swap is unwound by the next genuine
  turn's restore probe. The abort returns before the turn's `try`, so
  `_run_chat`'s exit guard runs the turn's tail (learn-cron-dashboard.md,
  "Every turn exit runs the turn's tail"): the superseding message
  dispatches unless the sub-agent hold or the admission sweep keeps it, and
  otherwise `_finish_queue_cycle` starts an armed synthesis or sends the
  terminal `chat_done`. The later pre-stream supersession abort sits inside
  the `try` and leaves the tail to the turn's `finally`. The model-access
  and image-history replays carry the same consume-seam check and the same
  exit, and all three families revoke by one rule
  (`RecoveryReplays.revalidate` in `dashboard/recovery_replays.py`: a
  rebind away from the recorded
  session, a Stop past the enqueue snapshots or in flight, or user input
  queued behind the replay), at the drain and again at the consume seam,
  each posting its family's cancellation notice. The model-access replay is
  identified only by the queue id its swap recorded: the drain matches the
  drained entry against it and claims it for the turn (`_run_chat`'s
  `_replay`), which keeps
  the spent one-shot for that replay alone, so a record whose entry left the
  queue another way (a hard kill's clear, the admission sweep) can never
  make a later turn read as the replay. A sub-agent completion the model
  never consumed is classified once, at the requeue
  (`_replay_owes_delivery`), and only when the turn's message IS the
  completion: dispatched as one, or a recovery entry `_queue_recovery`
  marked as its verbatim requeue (`_replays_completion`). It is a result the
  parent is still owed, so a
  model-access or image-history requeue queues it as the completion it is
  (`SUBAGENT_COMPLETION_KIND`, as the sign-in retry does), with no family
  record, and a soft Stop in flight does not skip the model-access swap
  for it; a hard kill (`_stop_state == "killing"`) discards it like the rest
  of the queue. Nothing else revokes it, the admission sweep exempts it like any queued
  completion, and a newer user message runs after it. Without a family
  record its turn refreshes the model-access one-shot like any completion
  turn; the bound on a second swap is then the candidate selector itself,
  because a swap fires only for a model absent from the advertised list and
  its candidate is always taken from that list. The synthesis turn (actor
  `subagent`, runner-authored), a runner-written continuation or retry
  prompt queued for the completion, and a turn after the completion was
  consumed owe nothing and stay ordinary, cancellable recoveries. A
  genuine new message drops a stale record (a Stop
  can purge the queued replay) and re-arms the one-retry allowance —
  GENUINE meaning user-origin: a kind-tagged recovery requeue of the
  user's own words (a pre-output process failure re-queueing the turn)
  never re-arms, and a recovery requeue of the replay itself — whichever
  recovery family issued it — carries the
  retry identity onto the fresh entry with refreshed stop snapshots, so
  the re-run is still the episode's one retry rather than a fresh episode
  (a crashing fallback replay cannot re-arm and cycle), and the drain's
  refusal-specific validation covers the requeue-to-dispatch window a
  family's own requeue-time guards cannot see.
- Restore is single-message: at the start of the first turn that is neither
  the replay nor a synthetic recovery continuation (a runner-authored
  continuation of the fallback turn must finish on the model that produced
  it), `chat_turn/model_fallback._restore_refusal_fallback` moves the session back to the
  recorded primary — a definite restore, not a probe, because the primary is
  not throttled. The record is applied only under the binding the swap
  recorded: when the slot has since rebound to a different session, the
  client on hand serves the rebound session, and applying the record
  through it would move that session's model to a primary it never chose —
  the restore drops the record with a warning instead (the recorded
  session is unreachable through the slot). It runs immediately
  after session acquisition, before the
  `slot.model` backfill and any model-window-dependent prompt construction,
  so the genuine turn is assembled against the primary's context window. The
  restore is witnessed like the swap: a non-raising `set_model` that did not
  move the model keeps the record for the next turn instead of stranding the
  session on the fallback. A pick-generation that moved since the swap, or a
  session already off the candidate by other means (explicit pick, reset),
  clears the record without selecting a model; a failed restore keeps the
  record for the next turn. The explicit-pick guard is two-layered because
  two slots can drive one wire session (a channel-born slot and its
  dashboard alias share `effective_session_key`): the slot-local pick
  generation covers picks on this slot, and a CLIENT-scoped explicit-pick
  epoch — stamped by the live-switch path on the shared client object and
  snapshotted at swap time — covers a pick made through any alias. The
  stamp lands immediately after `set_model` succeeds, before the effort
  reapply: the wire session serves the pick from that moment, so a reapply
  failure that falls back to reset must not leave the landed pick
  unstamped for a sibling restore to overwrite. A pick
  that took the session-reset path replaces the client entirely; the
  moved-off check catches it except when the alias picked exactly the
  candidate, an accepted compound-corner residual. Both the swap and the
  restore run under the same session-scoped switch lock the pick handlers
  take (`llm_helpers.slot_switch_session_lock`), acquired before the slot's
  pick lock. The lock keys off the TURN's binding, captured once at turn
  start and threaded into the swap, which stamps it on the slot; the
  restore locks on that recorded key rather than a live re-derivation. A
  slot's binding is mutable mid-turn (a cron result binds an unbound slot,
  a relink moves a bound one), and deriving at each seam would put swap and
  restore in disjoint lock domains while the replay drained onto a session
  the refused turn never ran on — the drain purges the replay when the
  live binding differs from the recorded one (the unbound-to-bound case is
  already dropped by the admission sweep's fail-closed booleans; the
  recorded-key check catches the bound-to-bound relink those booleans
  cannot see, and clears the episode's dispatch-gate record either way).
  The restore reads the epoch snapshot once before its
  `set_model` await, so without that serialization a pick landing inside
  the await window would be applied first and silently overwritten when the
  restore's write completed last; the swap writes the snapshot AFTER its
  own `set_model` await, so an unserialized pick landing inside that await
  would be folded into the snapshot and blind the restore's guard. Under
  the lock the switches are strictly
  ordered — a pick either completes first (the epoch check drops the
  record) or starts after the restore finishes and wins by ordering. Two seams cooperate with that rule. The
  same-value pick short-circuit in `chat_handlers` (the "nothing to switch"
  early return) is suppressed while either fallback record is live, so a user
  re-picking the displayed primary actually moves the wire model back — the
  pick-generation bump then makes the restore a deliberate no-op rather than
  the only path off the fallback. And when the moved-off model is explained
  by an ACTIVE throttle walk whose recorded restore target is the refusal
  candidate (the walk advanced off it mid-retry), the restore hands over
  instead of dropping: it rewrites the walk's restore target to the refusal
  primary, so the walk's own restore returns the session there rather than to
  the intermediate candidate.

The refusal retry is dashboard-only. Unattended surfaces (cron, heartbeat,
sub-agents) keep the terminal refusal behavior.

## Engine-side fallback (kiro-cli)

Users read this as the settings page's Kiro harness fallback note (`website/src/pages/settings/KiroHarnessFallbackNote.tsx`).

kiro-cli can move a turn onto another model by itself, inside one
`session/prompt`, under both of its engines: KAS (the `kas` harness,
`kiro-cli acp --agent-engine v3`) when its model fallback is enabled, and the V2
engine (the default `kiro` harness). Crew sees
an ordinary terminal for such a turn, so its own fallback above never runs for
it; the engine reports the move in a notice instead. `acp/engine_fallback.py`
is the only reader of the two wire shapes:

- KAS: `session_info_update` with `_meta.kiro.kind: "model_fallback"`,
  `{from, to?, trigger, scope, reason?, agentSubtaskId?}`. `scope: "session"`
  means KAS pinned the target as the session's model.
- V2: `_kiro.dev/session/update` with `update.sessionUpdate: "model_fallback"`,
  `{from, to, cause, source, promoted}`; the promotion is a second notice.

Both decode to `ModelFallbackNotice` with the rules kiro-cli's own terminal UI applies,
so the two clients tell a user the same thing: an id is accepted only as one
token of the engine's model-id grammar (and only when the credential scrub
leaves it unchanged), an unknown `trigger` is reported as `unknown`, an unknown
`scope` keeps the hop without promoting, a KAS notice carrying `agentSubtaskId`
is a sub-agent's own move and is not shown, and a same-id move is a retry on
one model rather than a switch. `_dispatch.classify_notification` (and
`AcpClient`'s classifier) route the V2 notice as action `model_fallback`
rather than as sub-agent activity; the KAS notice arrives through
`AcpSessionHandle._handle_kas_session_info`. Both emit `EVENT_MODEL_FALLBACK`.

State follows the engine, through `EngineFallbackTracker` on the handle and on
`AcpClient`:

- A turn-scoped hop sets `_resolved_model_id` to the target, so the turn's usage
  and stats (`read_effective_model`) are credited to the model that answered.
  The next prompt puts the prior value back, unless something else changed it
  since, because the engine starts that prompt on the user's model. Each move
  (hop, promotion, restore) rebases the context stats to the new model's window
  (`AcpPromptStats.rebase_to_window`), as `set_model` does. A model
  switch that lands in between (`set_model` on the handle or the client) cancels
  the restore through `EngineFallbackTracker.note_model_set`, including a switch
  onto the hop's own target, which the value comparison alone cannot tell apart
  from the hop.
- A promotion is the model kiro-cli is now on, and Crew reflects it.
  `_resolved_model_id` takes the target, and so does `_model` when the session
  was pinned; an unpinned session stays unpinned. On the dashboard
  (`dashboard/engine_fallback.py`) a pinned slot's `slot.model` takes it (a pin
  the session withheld at startup, `model_pin_refused`, which the engine never
  ran, is left alone, and so is a pick the user made during the turn,
  `_deferred_model_switch_key`, which the turn boundary applies; the line then
  reads `for this turn`) and the promotion counts as a pick
  (`_model_pick_gen` and the client pick epoch move),
  so a re-apply (respawn, warm-pool claim, cold start) or a pending Crew restore
  does not put the session back on the model kiro-cli left. Chained promotions
  (A to B, then B to C) count as one move from A to C, unless a channel already
  followed the first, which leaves its pick on B.
  `test_engine_fallback.py::test_the_next_cold_start_requests_the_model_the_engine_promoted`
  pins it.
- A promotion while Crew's own throttle or refusal fallback serves the slot
  (`_active_fallback_model` or `_refusal_fallback_primary` set) is the one
  exception on the dashboard. The model the engine moved off is Crew's fallback
  candidate, not the user's model, so `slot.model` keeps the user's primary and
  neither the pick generation nor the epoch moves. Crew's record follows the
  engine instead (`_retarget_crew_fallback` moves `_active_fallback_model`, the
  turn marker and `_refusal_fallback_candidate` onto the target), so Crew's
  restore still finds the session on its candidate and returns it to the
  primary. The line reads `answered by <to> for this turn`, because Crew moves
  the session off that model. The unattended surfaces hold only the provider's
  marker, so `probe_fallback_restore` does the same at the next turn's start: when
  the tracker's last promotion moved Crew's candidate to the model now serving,
  the marker follows and the restore runs. The probe consumes that promotion,
  so a later one chains from the new candidate even when the restore failed. A
  promotion back onto the primary ends Crew's fallback on every surface: the
  record is cleared, there is nothing to restore, and the line names the primary
  as the conversation's model.
- Every usage row bills an engine move to the model the engine ran, through one
  rule, `llm_helpers.engine_attributed_model(pin, client)`: the dashboard row
  (`dashboard/chat_turn/turn_stats.py`), cron sequential and isolated
  (`slack/gateway_runtime/cron_turn.py`) and sub-agent rows (`subagent_manager/run_turn.py`). A
  turn-scoped hop leaves a pinned `slot.model` on the pin, so the row cannot
  read the pin; Crew's own fallback keeps its separate
  `provider_fallback_active` check.

What the user sees is kiro-cli's end-of-turn line (`EngineFallbackWalk.line`):
`"<from> refused|was unavailable|yielded"` per hop, then `answered by <to> for
this turn`, `answered by <to>, now this conversation's model`, `No model
answered`, or `cancelled`, with a promotion named on every line that does not
already name it. Which ending it is comes from the turn's terminal stop reason,
for both engines (`acp/engine_fallback.py:walk_outcome`): `end_turn` answered, a
cancel cancelled, anything else (a refusal, a timeout, an `error:` reason, an
empty one) did not. A promotion is NOT read as an answer: KAS pins its target at
move time, before that target answers.

The terminal is trustworthy on both engines because of one rule at the ACP
layer, not in any consumer. `AcpPromptStats.terminal_refusal` relabels a
prompt's terminal `refusal` whenever a refusal was recorded on
`_kiro.dev/metadata`, because Kiro ends a filtered turn `end_turn`. In a V2
refusal fallback that recorded refusal belongs to the model being left. On the
wire it arrives as a `_kiro.dev/metadata` refusal, then the `model_fallback`
hop, then the prompt's answer: `stopReason: end_turn` when a later hop answered,
`refusal` when none did. That order is kiro-cli 2.32.0's, captured off the wire
in `test/fixtures/acp_frames/kiro/refusal-fallback-live.jsonl`, and
`test_engine_fallback.py` replays the capture through a session handle. So
`EngineFallbackTracker.apply` drops the recorded refusal on a V2 refusal hop
(`_drop_superseded_refusal`). A target that refuses too records a fresh refusal,
which folds as usual. A decline, an availability hop and every KAS notice leave
it alone; KAS reports its own terminal. Clearing at the hop rather than the
promotion matters, because the promotion notice can reach the client after a
refusal the target sent, and clearing there would wipe that refusal.

Every consumer reads the terminal from the same `EVENT_COMPLETE`: the dashboard
notice and refusal card, cron, heartbeat, the sub-agent run's outcome
classification, and the channel turns. So none of them carries an engine check
of its own.

The dashboard appends the line once from the turn's `finally`, as a persisted
notice, whatever path ended the turn. The unattended surfaces (cron sequential
and isolated, heartbeat, sub-agent runs) prefix it to the delivered result
through `annotate_model_fallback(..., stop_reason=)`, passing the terminal stop
reason each already captures; a caller that saw no terminal passes `None` and the
line reads as answered. While Crew's own fallback marker is set, a promotion is
worded as a move for this turn, because Crew restores its primary next run.
Crew's refusal retry is dashboard-only, so there is no
refusal card to suppress on those surfaces. Native Slack inbound runs through
the dashboard's `_run_chat` and gets the dashboard behavior. The standalone
channel transports (Telegram, WhatsApp, Discord, Webex, Slack transport mode,
and the autonudge wake turn) run through `messaging/driver.TurnDriver`, which
prints no fallback line, the engine's or Crew's own; the session's model state
still follows the engine there, because the session handle keeps it.

Where a surface SHOWS which model a conversation is on, it follows the engine
rather than naming the model that refused:

- The sub-agent tab's model chip, the `subagent_done` frame and the completion
  card all read `SubagentInfo.resolved_model`, set at spawn from the session's
  served model. At the end of a run, `subagent_manager/run_turn.py` replaces it with
  `engine_served_model(client)` (the walk's last target, else the session's
  standing promotion, which outlives a recovery prompt's new walk) when the
  engine moved the session. The run persists the model when the engine's
  notice arrives (`EVENT_MODEL_FALLBACK`), while it is live and holds its
  conversation, so every ending has it and no write lands after `done`, where
  it could roll back a continue's `keep`. The session's `served_model` cannot
  be used for this: a turn-scoped hop leaves a pinned `_model` alone.
- The Telegram, Discord and Webex `/model` pickers show their own per-conversation
  `_model_pref`, which is also what a cold start re-applies. After each turn,
  `follow_engine_promotion` moves a pinned pick onto a model the engine promoted,
  the same rule as a pinned dashboard slot. An Auto pick stays Auto, and a
  turn-scoped hop changes nothing. The caller passes the pick it read when the
  turn started, the same value it hands the cold start (`before=`), so a pick
  the user changed during the turn is left alone, and so is a pick naming a model
  other than the one the promotion replaced (a `/model` choice stored for the
  next session while the live one still ran the earlier model). The follow
  reads the session's standing promotion (`EngineFallbackTracker.promotion`),
  not the turn's walk, because a prompt the channel runs after the answer
  (post-turn compaction) starts a new walk. A promotion is read only while the
  session still serves it: an agent switch that activates its own model
  without `set_model` makes it stale for the served-model reader and the
  follow alike. A turn-scoped hop after the promotion does not, since the
  next prompt goes back to the promoted model (`next_prompt_model`). Each promotion is read once,
  followed or skipped, so a later turn cannot apply it again. Discord follows only after a prompt the call
  ran (`TurnOutcome.stop_reason` set), so a BUSY or refused monitor wake leaves
  the running turn's promotion to that turn. Discord and Webex follow before
  their post-turn attachment cleanup, the first await after the session is
  released, so a prompt that starts there cannot reset the walk first. A resumed dashboard session on
  Telegram or Discord is left alone, since its model is the dashboard slot's.

App code reaches all of this through `agent_sdk/drivers/acp_vocab.py`, which
re-exports the engine-fallback names, so no consumer imports `kiro_crew.acp`
for it (`scripts/check_agent_sdk_boundary.py`).

Crew's own fallback is unchanged and is not gated on the engine's: a turn the
engine moved that still ends in an error or a refusal enters Crew's walk or
refusal retry as before. A turn the engine recovered never reaches them, because
it ends `end_turn` on both engines (above), so they need no gate on the engine's
walk. Two
consequences are accepted. Crew may then take another hop in the same turn, past
the engine's own hop limit. And a turn-scoped hop is not reflected in
`served_model` (it prefers the pinned `_model`), so Crew's walk can choose the
engine's failed target as its own candidate when that model is also configured
as `agent.fallback_model`.

## Non-goals

- Swapping models after activity has begun on the transient/throttle path;
  the existing continuation recovery handles that path. (The refusal fallback
  above is a distinct trigger: it replays the whole message when no tool ran
  and continues from the completed work when one did.)
- Per-crew, per-cron, or per-role fallback chains.
- A dedicated per-turn model field on the ACP wire.
- kiro-cli changes. The engine-side fallback above is read, never driven.

## Tests

`test/test_engine_fallback.py` covers the engine notice decoders, the tracker,
the end-of-turn wording, and the handle, dispatcher and dashboard seams.

`test/test_llm_helpers_fallback_chain.py` and
`test/test_llm_helpers_fallback_restore.py` cover chain derivation, candidate
selection, retry state, story handling, and unattended restore. `test/test_dashboard_chat.py`
covers dashboard fallback and slot restore. `test/test_subagent_turn_resilience_retry.py`
and `test/test_cron_gateway_integration_llm.py` cover sub-agent and gateway wiring.
`test/test_config_loader_agent_section.py`, `test/test_role_models.py`, and
`test/test_dashboard_handlers_core_coverage_agent_settings.py` cover configuration
loading and PATCH validation.
