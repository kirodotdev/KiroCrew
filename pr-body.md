## Problem / Motivation

**Goal:** Stop in-flight subagents when the user presses the dashboard Stop button.

When a user clicks "Stop" in the KiroCrew dashboard, only the parent agent turn is cancelled. Any subagents that were already spawned continue running as orphans — their spinners keep showing and they consume resources until they time out.

## Why it matters

Users expect "Stop" to mean stop. Orphaned subagents confuse users (spinners keep going after the stop badge appears), waste compute, and can produce stale results that arrive after the user has moved on.

## Not a goal

- Cancelling subagents spawned by other sessions or other slots.
- Adding a "cancel subagents only" UI button separate from Stop.
- Changing the messaging-API (Slack) stop path, which already works correctly.

## What changed (motivation → approach → change)

The messaging-API stop (`run_control.py:845`) already calls `state.subagents.cancel_for_parent(cancel_key)` to cascade cancellation to in-flight subagents. The dashboard stop paths (`stop_slot_turn` for soft/hard stop and `api_chat_slot_interrupt`) skip this call entirely.

The fix adds the same `cancel_for_parent` call to all three dashboard stop code paths in `chat_handlers.py`, guarded by `getattr(state, "subagents", None)` since the subagent manager is optional and test fakes may omit it.

## Backwards compatibility

Compatible: adds a call that was already a no-op path (subagents would eventually time out). No existing behavior is removed or restricted — subagents that are already finished are unaffected, and `cancel_for_parent` is idempotent.

Removes nothing: no API surface, key, function, or capability is taken away.

## Tests

7 new tests in `test/test_stop_cancels_subagents.py`:

| Test | Behavior locked in |
|---|---|
| `test_soft_stop_calls_cancel_for_parent` | Cooperative stop cascades to subagents |
| `test_soft_stop_no_subagents_manager` | `subagents=None` doesn't crash |
| `test_soft_stop_no_subagents_attr` | Missing `subagents` attr doesn't crash (the `getattr` guard) |
| `test_soft_stop_cancel_for_parent_exception_swallowed` | Manager exception doesn't block the stop |
| `test_idle_outcome_still_cascades` | Even idle-outcome stops cascade |
| `test_hard_stop_calls_cancel_for_parent` | Hard kill escalation cascades |
| `test_hard_stop_cancel_exception_swallowed` | Manager exception doesn't block hard kill |

All 7 pass locally on Python 3.12.12. Existing 33 tests in `test_stop_handler_idempotent.py` also pass with zero regression.

## Manual verification

N/A — unit coverage sufficient. The fix adds calls to an existing, well-tested method (`cancel_for_parent`) that the messaging-API stop path already exercises in production. The three new call sites mirror that pattern exactly.

## Screenshots / video

N/A — no UI change. The Stop button behavior changes only in its backend side-effects (subagent cancellation), not in its rendered appearance.

## Related Issues

Fixes #18625

## Pattern harvest

Rule candidate: review-prompt
Pattern: when adding a new "stop" or "cancel" code path for a session, verify it cascades to child processes/subagents — check all stop entry points, not just the one being tested.

## Checklist

- [x] At most two commits (one is the norm), with a Conventional Commits title (`feat|fix|docs|style|refactor|perf|test|chore|ci|build|revert: ...`)
- [x] Existing tests pass and new tests added for new functionality
- [x] Self-review completed; code follows project style guidelines
- [x] Documentation updated (if applicable)
- [x] No secrets, credentials, or internal references in the diff
