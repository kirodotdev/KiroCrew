---
name: babysit
description: Use when a user asks to babysit, monitor, keep checking, keep an eye on, or report when a pull request, CI run, ticket, deployment, or other changing target reaches an outcome.
inject_on_trigger: false
triggers: babysit, keep checking, keep an eye on, let me know when, monitor pull request, monitor pr, watch pull request, watch pr
tags: [skill, kirocrew, monitor, babysit]
---

# Babysit

## Overview

Use a structured monitor when its typed provider observes every fact needed to
decide the objective. Kiro Crew probes before invoking the model, so unchanged
polling does not consume agent turns. Use the finite legacy path when required
evidence is outside the structured provider.

Structured watches can be created only from dashboard, Slack, and Discord
sessions. On Webex, use the finite legacy path even for a supported pull request;
Webex does not have structured wake delivery and completion correlation.

## GitHub pull request: one bounded watch

Call this once with the canonical pull-request URL when review readiness depends
only on pull-request lifecycle, mergeability, review decision, unresolved review
threads, and check conclusions:

```text
monitor_watch({
  "kind": "github_pull_request",
  "target": "https://github.com/OWNER/REPO/pull/NUMBER",
  "objective": "review_ready",
  "interval_secs": 300,
  "max_runtime_secs": 14400,
  "max_agent_turns": 8,
  "max_tokens": 250000,
  "max_provider_errors": 3,
  "wake_instructions": "Inspect the named blocker, fetch only the needed details, act safely, and verify the result."
})
```

The reply is a pending application request, not proof that the monitor armed.
End the turn so the session-aware consumer can apply it. The operator can then
confirm it in the dashboard. The agent cannot inspect after ending that same
turn; at the start of a later user/wake turn, call `monitor_inspect({})` before
acting or reporting. Its retained session-bound state is authoritative and it
takes no monitor id, target, or session key.

The server performs ordinary probes. Unchanged state, provider retry, success,
and terminal blockers use zero model turns. A new actionable fingerprint wakes
the session at most once with a compact, bounded summary. Only then fetch logs,
comments, or diffs needed to act. The token cap applies only when the provider
reports usage; `token_usage_known` exposes whether it did. Positive runtime and
completed-turn caps remain hard fallbacks. Provider errors are also bounded.

A terminal success uses zero model turns, so a structured watch does not create
a final reporting turn. If the user requires a final report or notification even
when no action is needed, use the finite legacy path.

Use `monitor_update({...})` without an id to change `interval_secs`, positive
budgets, or `wake_instructions`. Those edits preserve the comparison baseline;
changing `target` or `objective` starts a new baseline. Terminal records are
read-only, so start an explicit new watch to restart. Use
`monitor_stop({"reason": "User ended the watch."})` for a deliberate stop; the
retained outcome is `user_stop`. `autonudge_stop` remains a compatibility alias.

The GitHub provider does not observe generic issue or pull-request comments or
advisory review findings that are not represented by a review thread or check
conclusion. When the user's definition of readiness depends on that evidence,
call the finite legacy path directly. Do not ask `monitor_watch` to represent
evidence its typed provider cannot observe.

## Example: legacy comment-aware watch

Use a prompt loop when `monitor_watch` does not support the target/objective, or
when required evidence is outside the structured provider:

```text
monitor_start({
  "message": "Watch https://github.com/kirodotdev/KiroCrew/pull/123. On each injected cycle, inspect current review comments and checks. Act only on a real change. If the pull request is ready, terminal, or needs a human decision, report the outcome and call autonudge_stop with a reason.",
  "interval_secs": 300,
  "max_cycles": 24,
  "max_runtime_secs": 14400,
  "gate": false
})
```

Keep `gate: false` for this fallback: provider-fact gating cannot observe the
missing evidence and could suppress cycles while new comments await review.
The finite loop checks that evidence on every scheduled cycle.

All three limits must be nonzero; use a larger finite value, never `0`, for a
longer request. Every delivered legacy cycle is a full agent turn that extends
session context and spends model/tool tokens even when nothing changed. The reply
is only an arm request; verify the loop through the dashboard monitor/goal-loop
surface or `GET /api/autonudge`.

Legacy tool calls remain under normal PreToolUse governance and approval policy.
An unattended Slack or Discord turn has no blanket grant: approval may be
requested, rejected, or time out, and the loop records an approval stall instead
of silently succeeding.
