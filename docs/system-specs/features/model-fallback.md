# Model Fallback (throttle-exhaustion)

When the active model stays throttled past the same-model transient-retry budget,
the turn is retried on a fallback model instead of failing — visibly, never
silently. kiro-cli has no fallback mechanism; this feature is entirely
Kiro Crew-side, built on the substitute `set_model` path.

## Config

`agent.fallback_model` (`AgentConfig`, `config/loader.py`) — a SINGLE value with
three shapes. Coercion (`coerce_fallback_model`) normalizes registry
keys/aliases to acp ids and matches `"auto"` case-insensitively:

- **`"auto"` (the default)** — defer to the backend's availability-aware
  routing when the active model stays throttled. Team-decided default: the
  backend knows live availability and every account serves *some* model, so
  auto is the one fallback that works regardless of subscription plan or
  partition.
- **a concrete model id** — tried first, with `"auto"` as the final
  fallthrough (walk order `(id, "auto")`, derived in
  `configured_fallback_chain` — the one shared derivation).
- **`""`** — fallback explicitly disabled: every surface behaves byte-for-byte
  as before the feature existed (regression-pinned; this is the rollback
  story).

Editable via `kirocrew config set agent.fallback_model <id|auto|''>`, the
config PATCH API (str type, model-id grammar, `_validate_role_model` — the same
entitlement validation as the role-model pins, so `""`/`"auto"` always allow),
and Settings → Chat → Rate-limit fallback (single-select dropdown fed by the
advertised-model list — no free text, so a typo'd id cannot exist).

## Trigger and walk

The trigger is the exhaustion of the SAME transient budget that governs today's
retries (`TRANSIENT_RETRIES`, classifier `acp_error_is_transient`) with no output
streamed — exactly where the error used to surface. The chain walk is ONE shared
body, `llm_helpers.advance_fallback_candidate`, used by all three surfaces so
skip rules and marker semantics cannot diverge:

- seeds the primary from a surviving sticky marker first (a session already on a
  fallback whose true primary only the marker remembers), the active model second;
- walks the derived chain in order, skipping the primary, unadvertised ids
  (fail-open when the advertised list is unknown, matching `model_is_unusable`),
  and the currently-active failing candidate. `"auto"` is a legitimate candidate
  filtered by the same advertised check: a partition that does not serve it
  skips it rather than sending a no-op swap;
- applies the first candidate whose substitute `set_model` lands (the path that
  never puts an unserved model on the wire; its own `resolve_usable_model`
  sends `"auto"` only when advertised), publishes the sticky marker, and
  logs `model fallback: X -> Y (reason=throttle-exhaustion, surface=...)`.

Each candidate gets `FALLBACK_CANDIDATE_ATTEMPTS` (2: initial + one ~2s retry) —
deliberately not a fresh full budget, because throttle events are frequently
cell-scoped and model-agnostic. A non-transient error mid-chain propagates
immediately. Chain exhaustion surfaces the original error class with the chain's
story attached (`_kc_fallback_story`).

## Surfaces

- **`stream_and_collect`** (Case 2.75; cron and heartbeat pass the chain via
  `fallback_models=configured_fallback_chain()`): walks in-place and re-prompts.
  Delivered results are prefixed with a redacted warning line
  (`_annotate_model_fallback`, `slack/gateway.py`).
- **Dashboard** (`chat_runner`): the pre-token exhaustion branch swaps, appends a
  persisted notice card ("⚠️ X is throttled — running on Y until X recovers."),
  and re-queues the same message on the same live session
  (`SYNTHETIC_RECOVERY_KIND`). The per-candidate budget rides the existing
  transient counter (`TRANSIENT_RETRIES - 1` rewind = one in-branch retry).
  Nested turns (`_prompt_depth > 0`) get no fallback. Chain exhaustion falls
  through to the unchanged terminal branch with the story prepended.
- **Sub-agents** (`subagent._stream_with_transient_retry`): same walk on the
  zero-activity arm; the delivered result carries the warning prefix.

## Sticky state and restore

A swap is sticky for the session. The marker `TURN_FALLBACK_ATTR`
(`(primary, candidate)` on the provider) drives the unattended restore:
`probe_fallback_restore` runs at the next `stream_and_collect` entry and tries
one `set_model(primary)` — quiet on success (log only; recovery is the expected
state), staying on the fallback on transient failure, and dropping a stale
marker without touching the model when the session moved on by other means.

The dashboard mirrors this with slot state (`_active_fallback_model`,
`_fallback_primary_model`) and one extra rule: **an explicit user pick is never
overridden.** Picks are detected by a generation counter
(`slot._model_pick_gen`) bumped ONLY by the explicit set-model surfaces
(single-slot pick, bulk switch, provider-switch clear) and rolled back when a
pick is refused (`AcpModelUnavailable`) — never by the automatic provider
backfill, which writes the served model into an unpinned slot's `slot.model` and
must not read as a pick. On restore, a backfill-polluted `slot.model` is healed
back to the activation snapshot (`_fallback_slot_model`), because `slot.model`
is re-sent as a pin on resume.

## Visibility (mandatory, not configurable)

Every swap is announced: notice card (dashboard), warning-prefixed result
(cron/heartbeat/sub-agent), and a warning log for swap and restore. Model ids
originate in config (LLM-reachable via MCP), so every rendered occurrence passes
`redact_exfiltration_urls` + `redact_credentials`. `meta.turn_stats.model`
records the model that actually served the turn (both `set_model` paths sync
`_model`/`_resolved_model_id`, which `read_turn_model` reads).

## Non-goals

- Post-token / mid-stream model swap (the one-shot CONTINUE recovery is untouched).
- Per-crew / per-cron / per-role chains (v1 is global).
- A dedicated per-turn model field on the ACP wire.
- kiro-cli changes.

## Tests

`test/test_llm_helpers.py` (candidate selection, Case 2.75 matrix, restore
probe, marker-seeded primary), `test/test_dashboard_chat.py`
(`TestRunChatModelFallback`), `test/test_subagent_turn_resilience.py`,
`test/test_cron_gateway_integration.py` (`TestThrottleFallbackCronWiring`),
`test/test_role_models.py` / `test/test_config_loader.py` (coercion + load),
`test/test_dashboard_handlers_core_coverage.py` (fallback-model PATCH validation).
