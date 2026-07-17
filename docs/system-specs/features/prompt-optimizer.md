# Native Prompt Optimizer

Last Updated: 2026-07-13

## Overview

Pre-send prompt optimization via `Cmd+Shift+Enter` or sparkle button. Rewrites the user's draft message for clarity, specificity, and effectiveness before sending to the agent.

## Architecture

- **Backend** (`dashboard/handlers/optimizer.py`): `POST /api/optimizer/optimize` (registered in `dashboard/server.py`). Dedicated `_optimizer` session key on the `kirocrew-lite` agent (no semaphore contention with main chat). 30-second timeout. Context is the joined recent-message string, truncated server-side to the last 2000 chars. Permission requests are auto-rejected during optimization (no tool calls). The LLM's `UNCHANGED` sentinel — and any output that matches the original — returns `{changed: false}`. Optimizer output is passed through `redact_exfiltration_urls` + `redact_credentials` before returning.
- **Frontend** (`ChatInput.tsx`): Sparkle button in compose bar, keyboard shortcut `Cmd+Shift+Enter`. Shows optimized text in input for user review before sending. No auto-send — user must confirm.

## Flow

1. User types message in chat input
2. Triggers optimizer via Cmd+Shift+Enter or sparkle button
3. Backend receives draft + recent context (last ~10 messages)
4. LLM rewrites for clarity/specificity (dedicated session, no tool calls)
5. Optimized text replaces input content
6. User reviews, edits if needed, then sends normally

## Config

No config needed — always available. The optimizer session is isolated from the main chat session to prevent semaphore contention.

## Key Files

- `src/kiro_crew/dashboard/handlers/optimizer.py` — backend optimization logic (`POST /api/optimizer/optimize`, registered in `dashboard/server.py`)
- Frontend: `ChatInput.tsx` sparkle button + keyboard shortcut handler
