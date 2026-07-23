# Per-Turn Stats Footer (Elapsed Time + Credits) — Design Document

Last Updated: 2026-07-23 (initial)

## Overview

kiro-cli natively prints an end-of-turn line with elapsed time and credits used; the dashboard chat previously showed neither. This feature attaches per-turn stats (`elapsed_ms`, `credits`, `cost_usd`) to the final assistant message of each completed turn and renders them as an always-visible muted footer line under that message — parity with the CLI.

## Data flow

```
kiro-cli meteringUsage (unit=="credit")           claude_code result payload
        │ (acp/_dispatch.py accumulates)                  │ (duration_ms, cost_usd)
        ▼                                                 ▼
TurnUsage on EVENT_COMPLETE  ──►  chat_runner captures _turn_elapsed_ms /
                                  _turn_credits / _turn_cost_usd
                                          │
                                          ▼
                     _attach_turn_stats(slot, …)  →  meta["turn_stats"] on the
                     last assistant message (before _save_slot_to_history)
                                          │
                                          ▼
                     chat_done WS → frontend refreshSlot re-fetch → meta arrives
                                          │
                                          ▼
                     AssistantMessage renders the stats line (showFooter only)
```

## Backend (`src/kiro_crew/dashboard/chat_runner.py`)

- **Turn start stamp**: `_turn_t0 = time.monotonic()` is recorded immediately before the event-stream loop of each turn.
- **Capture at `EVENT_COMPLETE`**: `_turn_elapsed_ms` prefers the provider-reported `TurnUsage.duration_ms` (claude_code fills it; kiro/acp reports 0) and falls back to local wall clock. `_turn_credits` is kiro-cli's per-turn `meteringUsage` sum; `_turn_cost_usd` is claude_code's API-reported cost.
- **`_attach_turn_stats(slot, elapsed_ms, credits, cost_usd, turn_boundary)`**: mirrors `_flush_file_changes` — walks `slot.messages[turn_boundary:]` backwards and sets `meta["turn_stats"]` on the last assistant message. `turn_boundary` is `len(slot.messages)` captured at turn start, restricting the scan to messages appended during THIS turn — an error/refusal-only turn (no assistant message) therefore attaches nothing rather than overwriting the previous turn's stats. Runs in the `not _retrying_empty` post-turn block, *before* `_flush_file_changes` and `_save_slot_to_history`, so the meta both persists and reaches live tabs via the existing `chat_done` → `refreshSlot` re-fetch (no new WS event).

### Meta contract

```json
"turn_stats": { "elapsed_ms": 84210, "credits": 2.5, "cost_usd": 0.0231 }
```

| Field | Presence | Meaning |
|-------|----------|---------|
| `elapsed_ms` | always (> 0) | Turn wall clock (provider duration preferred) |
| `credits` | only when > 0 (rounded to 4 dp) | kiro per-turn credit spend |
| `cost_usd` | only when > 0 (rounded to 6 dp) | claude_code API cost |

Edge cases: no attach when `elapsed_ms <= 0` (turn never reached `EVENT_COMPLETE`); no synthetic message is fabricated and no earlier turn is touched when a turn produced no assistant message (error-only turns — enforced by `turn_boundary`). Empty-response re-queue turns skip attachment entirely (the whole post-turn block is skipped).

## Frontend (`website/src/pages/chat/AssistantMessage.tsx`)

- New `turnStats?: TurnStats` prop, passed from `ChatPage.tsx` via `m.meta.turn_stats` only when the persisted `ChatConfig.showTurnStats` setting is enabled.
- **User control**: Chat Settings includes a `Show elapsed time and credits` switch. It defaults to enabled for existing/new users, persists in `mc-chat-config`, and hides only the presentation — collection and persistence continue so re-enabling restores stats on existing messages.
- Rendered only on messages where `showFooter` is true (the last assistant message of each completed turn) and never while streaming — so exactly one stats line per turn.
- Always visible (unlike the hover-revealed action footer): 11px muted mono line with a clock icon, e.g. `⏱ 1m 24s · 2.50 credits`. `cost_usd` renders as `· $0.02` only when credits are absent (providers bill in one or the other).
- Formatting: `fmtTurnElapsed` — `3.5s` under 10 s, `42s` under a minute, `2m 34s` beyond; `fmtCredits` — 2 decimals under 10, 1 above.
- Messages persisted before this feature simply lack the meta and render nothing.

## Tests

- `test/test_turn_stats.py` — attach/omit semantics, last-assistant targeting, rounding, meta coexistence with `file_changes`.
- `website/src/test/AssistantMessage.test.tsx` (`turn stats footer` suite) — render variants (credits / cost / elapsed-only), hidden while streaming / `showFooter=false` / missing meta, formatter contracts.
