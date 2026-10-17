---
title: Retire chat Autopilot mode — the plan / approve / stage-loop slot mode
status: implemented
author: iamwhatever
created: 2026-09-30
last-audited: 2026-10-06
audited-at: ea7e91c8e6
doc-pr: null
implementation-prs: [15362, 15359, 15682]
tracking-issues: []
supersedes: [rfc-autopilot-stage-budgets.md]
superseded-by: []
---
# RFC: Retire chat Autopilot mode

- Status: implemented. Backend removal [#15362](https://github.com/kirodotdev/KiroCrew/pull/15362) and frontend removal [#15359](https://github.com/kirodotdev/KiroCrew/pull/15359) have merged. The user-visible mode is gone and saved `"orchestrator"` sessions restore as plain chat. Rollout step 3 merged as [#15682](https://github.com/kirodotdev/KiroCrew/pull/15682): no `StageBoundary` / `_in_stage_execution` reference remains under `src/`. This decision supersedes [rfc-autopilot-stage-budgets.md](rfc-autopilot-stage-budgets.md), which was not accepted before the feature disappeared.
- Author: iamwhatever
- Created: 2026-09-30
- Related: [`docs/system-specs/modules/autopilot.md`](https://github.com/kirodotdev/KiroCrew/blob/2b91a6baddca220ce79ec95bec6d7ca3192fb6a4/docs/system-specs/modules/autopilot.md) (the shipped behaviour at `2b91a6badd`, deleted by #15362), [`../system-specs/modules/crew-mode.md`](../system-specs/modules/crew-mode.md) § "Retired: Crew Mode" (the precedent this follows), [rfc-orchestrator-chat-sessions.md](rfc-orchestrator-chat-sessions.md), and [rfc-autopilot-stage-budgets.md](rfc-autopilot-stage-budgets.md) (superseded incident record).

## Summary

Autopilot is the chat slot mode `"orchestrator"`: the model presents a staged plan ending in `[OPTION: Go | Go All | Cancel]`, the user approves it, and a Python-controlled stage loop runs one stage per turn. It is removed. A restored `"orchestrator"` slot comes back as plain chat, the same way a retired `"crew"` slot does today. Nothing replaces it: plain chat already plans when asked, and a long multi-step job has the monitor loop, sub-agents and dynamic workflows.

## Problem

The mode costs far more than it returns. Measured at `2b91a6badd`:

- `src/kiro_crew/dashboard/chat_orchestrator.py` is 2,376 lines, `docs/system-specs/modules/autopilot.md` is 642 lines, and `src/kiro_crew/config/prompt-orchestrator.md` is a second system prompt kept beside the main one.
- #15362 deletes 10,014 test lines across 55 files under `test/` that exist to hold this mode.
- 61 commits touched the mode's own files (the module, its prompt, its spec and its dedicated tests) since 2026-06-30.
- Its stage state (`StageBoundary`, `_in_stage_execution`) is threaded through 16 files outside the module, including `chat_runner.py`, `session_control.py`, `slack/gateway.py`, `subagent.py` and the Spec Builder app's `turn_guard.py`, so every change to the turn loop has to reason about a mode almost nobody runs.

Almost nobody runs it. On the maintainer's own host, 3 of 2,835 saved chat sessions ever carried `mode: "orchestrator"`, and the newest of them is from 2026-08-10. That is the heaviest user of the dashboard; a count this low there means the mode is not a workflow anyone depends on.

## Decision

1. Remove the mode, its module, its prompt, its `plan-action` endpoint and its tests (#15362), then its toggle, menu entries, stage chrome and plan-gate chips (#15359).
2. Add `"orchestrator"` to `_RETIRED_MODES` in `src/kiro_crew/dashboard/chat_persistence.py`, beside `"crew"`. A saved session in that mode restores as plain chat (`""`) with its transcript intact, rather than failing to load.
3. In old transcripts, the `[OPTION: Go | Go All | Cancel]` footer renders as plain option text. Clicking one sends that word as an ordinary message; nothing resumes a stage loop.
4. `PATCH /api/chat/slots/{slot}/mode` coerces a request naming `"orchestrator"` to `""`, so an older dashboard build still gets a working chat. An `orchestrator` section in `config.json` becomes a reserved key that loads as nothing and is not written back.

## Rollout

1. #15362 — backend removal plus the `_RETIRED_MODES` entry. Exit: `chat_orchestrator.py` and `prompt-orchestrator.md` are gone, and a history file with `mode: "orchestrator"` restores as a plain chat slot.
2. #15359 — frontend removal. Exit: no Autopilot toggle, menu item or stage UI renders, and an old plan footer shows as plain chips.
3. A follow-up PR removes the remaining `StageBoundary` / `_in_stage_execution` plumbing from the turn loop, Slack gateway, sub-agent path and Spec Builder's `turn_guard.py`. Exit: no reference to either name remains under `src/`.

The backend goes first so the frontend never offers a mode the server has dropped for longer than one merge.

## Alternatives considered

- Keep it. Rejected: the usage above does not pay for the maintenance, and the stage plumbing taxes every turn-loop change, including for users who never turn the mode on.
- Ship it as an opt-in app. Rejected: Autopilot is not a separable feature. It lives inside the turn loop (stage boundaries, per-stage turns, cancel semantics), so an app would need new core hooks into the loop, which is more core surface than today, built for a mode with three recorded sessions. If demand appears later, a plan-and-approve flow can be built on the existing app, workflow and monitor surfaces without the loop hooks.

## Risks

- A user who does rely on the mode loses it. Mitigation: their sessions still open, as plain chat, with the full transcript; the model can still plan and wait for approval when asked in plain chat.
- An old plan footer's chips look actionable but only send text. Accepted: that is what a chip in plain chat already does.
- Stage plumbing left behind between rollout steps 1 and 3 is dead code for a while. Accepted and bounded by step 3.
