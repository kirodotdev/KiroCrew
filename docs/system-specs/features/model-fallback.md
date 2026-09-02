# Model fallback

`agent.fallback_model` controls an optional retry after an active model exhausts
its transient-error budget before activity begins. The implementation is
Kiro Crew-side: `llm_helpers.advance_fallback_candidate` changes the model through
the substitute `set_model` seam, and `test/test_llm_helpers.py` pins that a
successful swap is observable before it is recorded.

## Configuration

`AgentConfig.fallback_model` is normalized by
`config/loader.py:coerce_fallback_model`. `llm_helpers.configured_fallback_chain`
is the shared derivation used by the fallback callers:

- The automatic-routing sentinel is the field default; its configured value
  produces a chain containing that sentinel.
- A concrete model value produces a chain that tries the configured value before
  the automatic-routing sentinel.
- An empty value produces an empty chain, so the fallback branch remains inert
  and the existing terminal error path handles the failure.

`TestFallbackModelLoad` in `test/test_config_loader.py` and
`TestCoerceFallbackModel` in `test/test_role_models.py` pin the default,
normalization, concrete-value ordering, and empty-value opt-out. The dashboard
PATCH schema in `dashboard/handlers/core.py` applies the model-id grammar and
`_validate_role_model`; that validator allows the automatic and empty values,
rejects a known unusable concrete value, and permits a concrete value when no
advertised-model set is available.

## Trigger and candidate walk

The fallback path is eligible only after the normal transient retry budget is
spent on a transient error with no qualifying prior activity. Each surface
tracks that condition in its own stream loop: `llm_helpers.stream_and_collect`
requires no result text or tool activity, `dashboard/chat_runner.py` requires
no emitted or thought activity, and `subagent._stream_with_transient_retry`
requires no activity. Post-activity recovery does not enter the model-fallback
walk.

`llm_helpers.advance_fallback_candidate` owns candidate selection for all three
paths. It preserves the original primary from an existing provider marker when
available, otherwise from the active model. It then skips the primary, the
currently active model, and candidates absent from a known advertised-model
set. An unavailable advertised set does not reject a candidate; this is
load-bearing because entitlement cannot be determined without that set.

The helper calls the substitute `set_model` path and verifies that the serving
model changed before it records the candidate, publishes `TURN_FALLBACK_ATTR`,
or logs the swap. This witness prevents a non-raising no-op model selection from
being announced as a fallback. `TestSetModelWitness`,
`TestAdvanceFallbackCandidateAutoPrimary`, and `TestFallbackState` in
`test/test_llm_helpers.py` cover no-op handling, marker-seeded primary
selection, advertised-model filtering, and chain progress.

`FallbackState.should_retry_active` owns the per-candidate retry allowance, and
`fallback_rewound_transient_budget` derives the dashboard counter from the same
allowance. The pinning tests in `test/test_llm_helpers.py` and
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
- `dashboard/chat_runner.py` swaps through `_fallback_swap_for_turn` after its
  pre-activity transient branch, persists a notice, and requeues the same
  message as a synthetic recovery item. Its fallback condition excludes nested
  prompts. On exhaustion it renders the slot-local walk in the terminal error.
- `subagent._stream_with_transient_retry` follows the zero-activity branch,
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
`chat_runner._probe_fallback_restore_for_slot_locked`, applies the same probe to
slot-held state. It snapshots `slot.model` and `_model_pick_gen` when fallback
activates. Explicit single-slot and bulk model picks, plus a provider switch,
increment the generation; a rejected single-slot pick restores its prior
value. A changed generation makes the fallback record stale, so restore cannot
override an explicit user choice. When automatic provider backfill changes
`slot.model` without changing the generation, the restore hook reinstates the
activation snapshot before clearing fallback state. This prevents an automatic
fallback selection from becoming a persistent user pin.

`TestRunChatModelFallback` and `TestSlotProbeWrapsSharedRestoreBody` in
`test/test_dashboard_chat.py`, together with the restore tests in
`test/test_llm_helpers.py`, pin the stale-record, explicit-pick, and backfill
invariants.

## Non-goals

- Swapping models after activity has begun; the existing continuation recovery
  handles that path.
- Per-crew, per-cron, or per-role fallback chains.
- A dedicated per-turn model field on the ACP wire.
- kiro-cli changes.

## Tests

`test/test_llm_helpers.py` covers chain derivation, candidate selection, retry
state, story handling, and unattended restore. `test/test_dashboard_chat.py`
covers dashboard fallback and slot restore. `test/test_subagent_turn_resilience.py`
and `test/test_cron_gateway_integration.py` cover sub-agent and gateway wiring.
`test/test_config_loader.py`, `test/test_role_models.py`, and
`test/test_dashboard_handlers_core_coverage.py` cover configuration loading and
PATCH validation.
