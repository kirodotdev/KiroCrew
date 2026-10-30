# Per-Turn Stats Footer (Elapsed Time + Credits)

## Overview

The dashboard persists per-turn measurement metadata on an assistant message and renders it as a muted footer when that message is the completed turn's final assistant message. `_attach_turn_stats` (`dashboard/chat_turn/turn_stats.py`, called through `chat_runner`) keeps the metadata on the message before persistence, so the `chat_done` refresh can restore the same footer after reconnects.

## Data flow

```
ACP metadata / provider usage
        │
        ▼
TurnUsage on EVENT_COMPLETE
        │
        ▼
chat_runner captures elapsed, credits, cost, and model
        │
        ▼
_attach_turn_stats(slot, ...) → meta["turn_stats"] on a current-turn assistant message
        │
        ▼
chat_done → refreshSlot → persisted metadata
        │
        ▼
AssistantMessage renders the footer when its presentation gates permit it
```

## Backend (`src/kiro_crew/dashboard/chat_runner.py`, `chat_turn/turn_stats.py`)

`_run_chat` captures a monotonic start time and message boundary at turn start. On `EVENT_COMPLETE`, `_on_complete` (`chat_turn/turn_stats.py`) prefers `TurnUsage.duration_ms` when present and otherwise measures elapsed time locally; it reads credits and cost from `TurnUsage` and calls `read_turn_model(client)`. ACP credit telemetry is accumulated from `meteringUsage` entries whose unit is `credit` by `acp.client_usage._track_metadata`; `acp._dispatch.parse_metadata` applies the same unit filter for the shared dispatch path.

`_attach_turn_stats(slot, elapsed_ms, credits, cost_usd, turn_boundary, model, ttft_ms)`, defined in `chat_turn/turn_stats.py` beside the first-token clock (`_FirstVisibleClock`, `_turn_clock`) whose reading `_turn_persist_and_settle` (`chat_turn/stop_recovery.py`) passes as `ttft_ms`, and bound on `chat_runner` (the binding tests import), writes `meta["turn_stats"]` on the last assistant message appended at or after `turn_boundary` and returns whether a row received it. `TestAttachTurnStats.test_error_only_turn_does_not_overwrite_previous_turn` and `test_boundary_scopes_to_current_turn_assistant` enforce this boundary: without it, an error-only turn could overwrite the prior turn's measurement. The helper does not fabricate a message, and it returns without a positive elapsed measurement. The post-turn persistence block skips the helper for `_retrying_empty` turns.

The helper preserves pre-existing `meta`, always records a positive `elapsed_ms`, records `ttft_ms` only when positive (chat turns pass it; the footer does not render it), records `retries` only when positive (how many times a top-level chat turn re-sent the user turn after a pre-output transient error, from `slot._turn_retries`; the footer does not render it either), and omits non-positive credits, cost, and empty model values. It also drops a non-finite credit or cost (`json.dumps` writes `inf`/`nan` as a bare `Infinity`/`NaN` that no browser parses), scrubs the model id with `_redact_acp_string` (credentials and suspicious URLs), and drops a model longer than `_MAX_MODEL_ID_LEN` — all of which applies to chat turns too, since `turn_stats_meta` is the one builder both writers share. `TestAttachTurnStats.test_credits_rounded` pins credit precision; `test_preserves_existing_meta`, `test_zero_credits_key_omitted`, `test_zero_cost_key_omitted`, and `test_model_omitted_when_unattributable` pin the remaining contract.

### Turns that end abnormally

Every abnormal end goes through `_persist_partial_reply`: it saves the streamed text as the interrupted reply row, then `_record_abnormal_turn_usage` writes the usage row and the footer. The callers are the `CancelledError` arm (a Stop press, labelled `STOP_REASON_CANCELLED`, and the involuntary `error: cancelled`: tab close, idle sweep, shutdown, turn ceiling), the `AcpError` / `AcpProcessDied` / `AcpAuthRequired` arms (terminal errors, a post-token transient), the generic `except Exception` arm (a shared-runtime prompt timeout, `AcpRequestTimeout`), and the four recovery arms that return before the end-of-turn flush (`stale_recover`, `error: tool stall`, `error: compaction failed`, the retryable `error:` family). Without the save those arms leave the text as `chunk` rows, which no save writes. The re-queued continuation is unchanged by the save: `build_stale_recovery_prompt` and `build_tool_stall_recovery_prompt` read no transcript rows, and a pipe-death requeue comes from `build_recovery_requeue(message, ...)`. A history replay after a failed resume sees the saved partial as the assistant text the backend already streamed, which is the intended record of the turn.

The footer comes from one of two sources:

| The turn | Footer figures |
|---|---|
| reached `EVENT_COMPLETE` (`_turn_stats_captured`): a recovery arm, or a cancel landing after the completion (the turn ceiling's task cancel after the agent acknowledged its stop, a late Stop) | the completion's own elapsed time, credits, cost, model and `ttft_ms`, written before the once-per-turn usage guard returns, so the complete branch's row stays the only one |
| did not | the wall-clock elapsed time and the seam's ONE `provider_last_turn_usage` read, shared with `_persist_abnormal_turn_usage(usage=...)`: the read consumes the accumulated multi-attempt total, so a second read would give the row only the last attempt |

The attach is gated on `_turn_boundary_set`, which dispatch sets when it captures `_turn_msg_boundary`: an end before dispatch (during pre-prompt preparation) still reads boundary 0, would overwrite the previous reply's footer, and so writes only the usage row. It is written through `_attach_owned_turn_stats`, the one narrowed scan both the end-of-turn attach and this seam call (the end-of-turn attach falls back to the plain boundary scan only when the turn recorded no reply row, such as a command's own confirmation), so that only a row this runner appended (`slot._turn_reply_mids`, recorded by `_note_reply_row`) carries it, so a workflow or sub-agent result another writer appends into the window mid-turn never takes this turn's figures. A turn that appended no reply row gets no footer, since there is no row to carry it. `_reply_flushed` keeps the text to one saved row per turn: the end-of-turn flush and `_persist_partial_reply` both set it, so a cancel or error landing after either save does not append the reply again. The in-place meta write reaches disk because the turn's `finally` awaits `_flush_file_changes_off_loop`, whose on-loop attach marks the slot dirty on every exit. `TestStoppedTurnFooter` and `TestAbnormalEndFooter` in `test/test_chat_runner_abnormal_turn_usage.py` pin the footer on each end, the shared read, the elapsed-only footer of an unbilled stop, that a pre-dispatch Stop leaves the previous footer alone, that a late cancel keeps the completed footer whole, and the one saved row.

### Cron run results

`chat_turn.turn_stats.turn_stats_meta` builds the same `turn_stats` dict for both writers. The single-agent cron run in `slack/gateway_runtime/cron_turn.py` builds it from the run's `provider_last_turn_usage` and `read_turn_model`, and `cron_inject.inject_cron_result_to_dashboard` passes it as `meta` on the result row's `slot.append`, so the live broadcast and the slot window both carry it. It also passes it (as `row_meta`) to the durable `cron:{id}` append, so a restart before the slot save keeps the footer. Cron results have no `chat_done`; the broadcast is what an open tab renders. `test/test_cron_turn_stats_footer.py` pins all three — the broadcast row, the durable `cron:{id}` row (`test_durable_cron_row_carries_turn_stats`), and that an inject with no stats stamps nothing.

### Model attribution

`dashboard.handlers.usage.read_turn_model` returns a concrete resolved model identifier when one is available, the `auto` sentinel for an Auto request without a resolved identifier, or an empty string when neither is known. A session on the default model counts as an Auto request: its start path never calls `set_model`, so `_model` stays empty, while `session/new` stores the backend's `currentModelId` of `auto` as `_resolved_model_id`, and that backend-reported `auto` yields the sentinel. An empty `_model` with an empty or missing `_resolved_model_id` stays empty. `TestReadTurnModel` enforces the precedence and the unattributable case, and `TestBackendReportedAutoIsAttributed` (`test/test_usage_model_attribution.py`) pins the default-model session shape on the real provider classes. The sentinel distinguishes an explicit Auto selection from absent attribution; callers that need a concrete identifier for pricing or context-window lookup use `read_effective_model` instead.

## Frontend

`website/src/pages/chat/page/transcriptRendering.tsx` (ChatPage's bubble renderer, `bubbleRenderer`) passes `m.meta.turn_stats` to `AssistantMessage` only when `ChatConfig.showTurnStats` is true. `ChatSettings.loadChatConfig` defaults and repairs that persisted setting as enabled under `mc-chat-config`; `ChatPanel` does not currently expose a control for it. `website/src/app-sdk/messageRenderers.tsx` separately forwards `turn_stats` without consulting `ChatConfig`.

`website/src/pages/chat/AssistantMessage.tsx` renders the footer only when the message is not streaming, `showFooter` is true, and `turnStats.elapsed_ms` is positive. ChatPage's bubble renderer computes `showFooter` for the last assistant message before a user message or, at the end of the transcript, once the slot is no longer running. A positive persisted `turn_stats` record keeps that completed footer visible if a later run starts before another message is appended. Together with the backend boundary, these gates prevent an in-progress or earlier assistant segment from presenting the completed-turn footer without retracting an already completed turn's metadata.

The footer is visible rather than hover-revealed, uses muted tabular numerals, and places a clock beside elapsed time. Only the optional model label uses the monospace face; `messageFooterFont.test.tsx` protects the footer-level font-setting contract. It displays credits when positive and otherwise displays positive dollar cost; elapsed time always follows the billed value. `fmtTurnModel` trims known routing prefixes for the inline label while the title retains the untrimmed model identifier. `fmtTurnElapsed` (`AssistantMessage.tsx`) and `fmtCredits` (`website/src/i18n/format.ts`) format the values, and their formatter tests in `AssistantMessage.test.tsx` pin the display rules. Missing `turn_stats`, `showFooter=false`, and streaming messages render no stats footer.

## Tests

- `test/test_turn_stats.py` (`TestAttachTurnStats`) covers attachment, omission, rounding, current-turn targeting, model attribution, and meta coexistence.
- `test/test_usage.py` (`TestReadTurnModel`) covers resolved, Auto, and unattributable model states.
- `website/src/test/AssistantMessage.test.tsx` (`turn stats footer`) covers rendering, ordering, model display, suppression gates, and formatters.
- `website/src/test/ChatPage.footerNotRetracted.test.ts` protects a completed footer from being retracted by a later run.
- `website/src/test/ChatSettings.test.tsx` covers the persisted `showTurnStats` default and validation.
- `website/src/test/messageFooterFont.test.tsx` verifies that the footer follows the configured font family.
