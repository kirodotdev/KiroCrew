---
title: "A first-party app's own agent calls the app's own declared tools without a prompt"
status: implemented
author: rayrayxu, with kirocrew
created: 2026-11-19
last-audited: 2026-11-23
audited-at: d597493a9b5
doc-pr: 39570
implementation-prs: [39500]
tracking-issues: [26967]
supersedes: []
superseded-by: []
---

# RFC: App Own-Agent Tools Grant

- Status: implemented by [#39500](https://github.com/kirodotdev/KiroCrew/pull/39500). Accepted 2026-11-20 by Bolin Chen (maintainer, bolichen97) in [the design sign-off on #39570](https://github.com/kirodotdev/KiroCrew/pull/39570#pullrequestreview-5987012246). This document landed first because the change alters when a tool call asks for approval, and the First Principles lane reads that decision off the base branch.
- Tracking issue: [#26967](https://github.com/kirodotdev/KiroCrew/issues/26967)
- Implementation: [#39500](https://github.com/kirodotdev/KiroCrew/pull/39500)

This amends [rfc-app-agent-tools-third-party-grant.md](rfc-app-agent-tools-third-party-grant.md), whose Non-goals say of first-party apps: "Their tools stay on `kirocrew-core`, with no grant family." That document is accepted and is not edited here. The tools still stay on `kirocrew-core` and still get no grant family. This document decides one thing that one left open: which callers reach those tools without a prompt.

## Summary

An app's own agent may call a tool the same app declares in its manifest `agentTools` without an approval prompt. The grant holds only when three facts name one first-party app: the gateway's stamp on the run, the agent that actually ran, and the app that declares the tool. Every other call asks as it does today.

## Motivation

### Current state

Verified at `c0d672b7805`:

1. A first-party app's `agentTools` are listed under the `kirocrew-core` MCP server (`apps/app_agent_tools.py`, `mcp_tools/app_tools.py`).
2. The default agent's `allowedTools` grants `@kirocrew-core` as a whole server (`config/defaults.json`). A person's chat on that agent calls these tools with no prompt.
3. An app's own agent carries no such grant. Its call reaches the tool gate (`HookManager.judge`, rows in `hooks.GATE_TIERS`).
4. The gate's `app-own-server` row (`_tier_app_own_server` in `hook_runtime/gate_tiers.py`) approves an app agent's call to the app's own MCP server, named `<app>:<server>`. An `agentTools` tool is on `kirocrew-core`, so that row does not match it.
5. The cron turn (`slack/gateway_runtime/cron_turn.py`) passes the gate a session key and an agent name. It passes no app and no resolved agent, so no app row can decide a cron run.
6. An unattended approval prompt expires after `_BACKGROUND_APPROVAL_TIMEOUT_SECS`, which is 180 seconds (`dashboard/state_parts/dashboard_state.py`).

### Problem

An app that runs its cron on its own agent cannot call its own API tool. The call asks, nobody is there, and the call is refused when the prompt expires. [#38867](https://github.com/kirodotdev/KiroCrew/pull/38867) moves Ops Mission Control's crons onto the app's own agents, and its review blocks on this: rotation and ledger upkeep stop ([review comment](https://github.com/kirodotdev/KiroCrew/pull/38867#issuecomment-6965799255)).

Measured on a real gateway at base `8301e686635` (see Evidence): the app's cron on the app's own agent called `ops_mission_control_api`, was asked, and was refused when the prompt expired, in 3 of 3 runs, after 183 to 184 seconds each. The basis for "about 180 seconds" is item 6 above plus those three measurements.

## Goals

- An app's own agent reaches the app's own declared tools unattended.
- The call is still judged at the gate on every call.
- No caller other than the app's own agent gains anything.

## Non-goals

- Third-party apps. Their `agentTools` block is not read on main, and their grant is [rfc-app-agent-tools-third-party-grant.md](rfc-app-agent-tools-third-party-grant.md).
- Any tool the app does not declare: a core tool, another app's tool, a built-in, a shell call, a file write.
- A manifest `approval_mode`, a cron auto-approval field, or a consent screen.
- Which agent an app's cron may run on. That is #38867.
- Changing `app-own-server`, the declared-tool reader, or governance inputs of a cron run.

## Design

### The decision

The gate gets one grant row, `app-own-tools`, after `app-own-server`. It approves a call only when all of these hold:

| # | Condition | Where the fact comes from |
|---|---|---|
| 1 | The run is stamped as app X | The slot's `_app` (set only from an authenticated app scope), or for a cron run the job's `created_by`, which the host writes from a verified app claim |
| 2 | The agent that actually ran is one app X ships | `_builtin_app_for_agent` on the resolved agent; the slot's or job's agent name is never read |
| 3 | The call is `@kirocrew-core/<tool>` by verified identity | The backend's tool identity, never the title |
| 4 | App X declares that tool | `app_agent_tools.declared_tools()`, minus any name a core tool has, read at the call |
| 5 | App X is readably enabled | `app_enabled_state` |
| 6 | No operator `confirm_tools` pattern names the call | `hooks._operator_confirms` |

The stamp and the agent come from one source per call: the slot's pair, or the cron run's pair. They are never mixed. Any missing or unreadable fact means the normal prompt.

### What still runs first

The row is a grant row. Every deny row and governance run before it, so a deny rule, a ceiling, a profile or `auto_deny_tools` on the tool still refuses the call. `confirm_tools` still asks (condition 6). Under the read-only policy the row is skipped, as every grant row is.

### Which declarations count

The row adds no rule of its own. It follows `app_agent_tools`:

1. A built-in's block is read from the wheel's `app.json`, never from the data-home copy.
2. An adopted former built-in's block is read from the installed copy, and a tool counts only when it equals one its wheel copy declares. The installed copy can declare fewer tools, never more.
3. A tool an installed tree adds beyond that is not declared, so it asks.

### Known limit

`_builtin_app_for_agent` maps an agent name to the built-in app that ships an agent of that name. An owner's own agent that has the same name as a built-in app's agent is treated as that app's agent. This is the existing behaviour the `app-own-server` row already relies on. The new row is narrower than that one in practice, because condition 1 must also hold: the run must carry the same app's stamp from the gateway.

## Migration plan

One phase, in [#39500](https://github.com/kirodotdev/KiroCrew/pull/39500).

Exit criteria:

1. `test/test_app_own_agent_tools.py` passes: own agent with own tool is approved; another app's tool, another app's agent, an agent no manifest ships, an unstamped run, an undeclared tool, a disabled app, a `confirm_tools` pattern and a deny rule each ask or deny.
2. `test/test_gate_tiers.py` pins the row's place in the table.
3. On a real gateway, the app's cron on its own agent calls its own tool with no prompt, and the other cases still ask.

## Backward compatibility

Nothing is rejected, renamed or removed. A caller that passes no run stamp gets no grant. The one change: a call that meets all six conditions no longer asks.

## Security considerations

- The grant loosens one approval. Its reach is the tools a shipped first-party manifest declares, which the default agent already calls unasked.
- No fact comes from a name the app, the agent or the model chose. A forged title carries no verified identity and fails condition 3.
- A cron job an agent made with `cron_add` carries no app stamp and fails condition 1.
- An app's cron that names an owner agent fails condition 2 at the gate. Whether such a job may exist at all is #38867; see the last row of Evidence.
- The row runs after every deny row and governance, so it cannot outrank a refusal.

## Alternatives considered

1. **List the tool in the app agent's own `allowedTools`.** `bridges_agents._register_agents` keeps a packaged agent's `allowedTools` after the ceiling filter, so this works with no core change. Rejected: the backend then approves the call itself and the call never reaches the gate. Deny rules, governance and `confirm_tools` are applied once, when the agent file is written, and not on each call. It also needs each app to list each tool in each agent file.
2. **A manifest auto-approval for the app's cron.** #38867 tried a cron `approval_mode: "auto"` in the manifest, then an owner-consent grant family for it, and removed both: it approves every tool call of the run, and reviewers found an app could reach it on an owner agent.
3. **Leave the crons on the default agent.** That keeps today's behaviour, where an app's unattended job runs with the owner's full tool set. #38867 exists to end that.

## Evidence

Run on 2026-11-19 on a throwaway Linux machine (Amazon Linux 2023, Python 3.12), two gateways side by side, each on 127.0.0.1 only, real model turns. Base is `8301e686635`; head is #39500 at `552b1e501d2`. No built-in on main ships agents, so both trees also carried Ops Mission Control's `app.json` and `agents/` from #38867. Every count is read from each gateway's security event log. Full record: [PR comment](https://github.com/kirodotdev/KiroCrew/pull/39500#issuecomment-7019122823).

| Case | Base | Head |
|---|---|---|
| Own agent, own declared tool (app's cron) | asked, expired: 3 of 3 runs, 183 to 184 s | approved, no prompt: 4 of 4 runs |
| Own agent, core tool the app does not declare | not run | asked, expired (1 run, 181 s) |
| Own agent, another app's tool | not run | asked, expired (1 run) |
| Unstamped run on the app's agent | not run | asked, expired (1 run) |
| App's cron on an agent the app does not ship | not run | asked, expired (1 run) |
| App's cron on the name `default` | ran on the owner's `kirocrew` agent, no gate decision (1 run) | the same (1 run) |

The last row is not this grant. `default` resolves to the owner's agent, whose own `allowedTools` approves the call in the backend. Base and head behave the same.

One limit seen in that run: after a second gateway start, Ops Mission Control was adopted onto its pinned repository (`adoption-pins.json`). That tree declares no agents, and 0 of 2 agents registered. On such a host the grant has no agent to apply to until the app's own repository ships the agents and the pin moves.

## Open questions

None. The decision asked of a maintainer is the Summary.
