# Per-Turn Stats Footer (Elapsed Time + Credits)

## Overview

The dashboard persists per-turn measurement metadata on an assistant message and renders it as a muted footer when that message is the completed turn's final assistant message. `chat_runner._attach_turn_stats` keeps the metadata on the message before persistence, so the `chat_done` refresh can restore the same footer after reconnects.

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

## Backend (`src/kiro_crew/dashboard/chat_runner.py`)

`_run_chat` captures a monotonic start time and message boundary at turn start. On `EVENT_COMPLETE`, it prefers `TurnUsage.duration_ms` when present and otherwise measures elapsed time locally; it reads credits and cost from `TurnUsage` and calls `read_turn_model(client)`. ACP credit telemetry is accumulated from `meteringUsage` entries whose unit is `credit` by `acp.client.AcpClient._track_metadata`; `acp._dispatch.parse_metadata` applies the same unit filter for the shared dispatch path.

`_attach_turn_stats(slot, elapsed_ms, credits, cost_usd, turn_boundary, model)` writes `meta["turn_stats"]` on the last assistant message appended at or after `turn_boundary`. `TestAttachTurnStats.test_error_only_turn_does_not_overwrite_previous_turn` and `test_boundary_scopes_to_current_turn_assistant` enforce this boundary: without it, an error-only turn could overwrite the prior turn's measurement. The helper does not fabricate a message, and it returns without a positive elapsed measurement. The post-turn persistence block skips the helper for `_retrying_empty` turns.

The helper preserves pre-existing `meta`, always records a positive `elapsed_ms`, and omits non-positive credits, cost, and empty model values. `TestAttachTurnStats.test_credits_rounded` pins credit precision; `test_preserves_existing_meta`, `test_zero_credits_key_omitted`, `test_zero_cost_key_omitted`, and `test_model_omitted_when_unattributable` pin the remaining contract.

### Model attribution

`dashboard.handlers.usage.read_turn_model` returns a concrete resolved model identifier when one is available, the `auto` sentinel for an Auto request without a resolved identifier, or an empty string when neither is known. `TestReadTurnModel` enforces the precedence and the unattributable case. The sentinel distinguishes an explicit Auto selection from absent attribution; callers that need a concrete identifier for pricing or context-window lookup use `read_effective_model` instead.

## Frontend

`website/src/pages/chat/useChatPageTranscriptController.tsx` passes `m.meta.turn_stats` to `AssistantMessage` only when `ChatConfig.showTurnStats` is true. `ChatSettings.loadChatConfig` defaults and repairs that persisted setting as enabled under `mc-chat-config`; `ChatPanel` does not currently expose a control for it. `website/src/app-sdk/messageRenderers.tsx` separately forwards `turn_stats` without consulting `ChatConfig`.

`website/src/pages/chat/AssistantMessage.tsx` renders the footer only when the message is not streaming, `showFooter` is true, and `turnStats.elapsed_ms` is positive. `useChatPageTranscriptController.tsx` computes `showFooter` for the last assistant message before a user message or, at the end of the transcript, after the slot is no longer running. Together with the backend boundary, these gates prevent an in-progress or earlier assistant segment from presenting the completed-turn footer.

The footer is visible rather than hover-revealed, uses muted tabular numerals, and places a clock beside elapsed time. Only the optional model label uses the monospace face; `messageFooterFont.test.tsx` protects the footer-level font-setting contract. It displays credits when positive and otherwise displays positive dollar cost; elapsed time always follows the billed value. `fmtTurnModel` trims known routing prefixes for the inline label while the title retains the untrimmed model identifier. `fmtTurnElapsed`, `fmtCredits`, and their formatter tests in `AssistantMessage.test.tsx` pin the display rules. Missing `turn_stats`, `showFooter=false`, and streaming messages render no stats footer.

## Tests

- `test/test_turn_stats.py` (`TestAttachTurnStats`) covers attachment, omission, rounding, current-turn targeting, model attribution, and meta coexistence.
- `test/test_usage.py` (`TestReadTurnModel`) covers resolved, Auto, and unattributable model states.
- `website/src/test/AssistantMessage.test.tsx` (`turn stats footer`) covers rendering, ordering, model display, suppression gates, and formatters.
- `website/src/test/ChatSettings.test.tsx` covers the persisted `showTurnStats` default and validation.
- `website/src/test/messageFooterFont.test.tsx` verifies that the footer follows the configured font family.
