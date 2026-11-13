---
title: One work_dispatch call creates, opens, binds and seeds a work item
status: accepted
author: kirocrew agent session, directed by zejiangg
created: 2026-10-26
last-audited: 2026-10-28
audited-at: 47c71cbc9e
doc-pr: null
implementation-prs: []
tracking-issues: [24226]
supersedes: []
superseded-by: []
---

# RFC: One `work_dispatch` call creates, opens, binds and seeds a work item

Status: accepted. [#24231](https://github.com/kirodotdev/KiroCrew/pull/24231) was approved and merged by chenmingwei23, and its amendment [#25380](https://github.com/kirodotdev/KiroCrew/pull/25380) was merged by iamwhatever; acceptance recorded by the maintainer iamwhatever, who owns the conductor patrol. Nothing built. This answers open question Q1 of [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md) ("Should `session_create` create and bind the item in one call?"). Tracks [#24226](https://github.com/kirodotdev/KiroCrew/issues/24226). Every code reference below was read at `9d31193afe`.

## Summary

Add one tool, `work_dispatch`, on the `kirocrew-dashboard` server beside `session_create` and `session_send`. One call does what a conductor does today in four: `work_ledger_record action=create`, `session_create`, `work_ledger_record action=bind`, `session_send`. It returns the real `item_id` and worker session key, or names the step that failed and what it cleaned up. It answers only a caller that already owns a work ledger (the 2026-10-28 amendment; originally, a caller whose agent mounts `@kirocrew-work`), and it is not auto-approved: a conductor prompts once per item, as it prompts for `session_send` today. The four-step path stays as the documented fallback. `session_create`, `session_send` and the `kirocrew-work` server do not change.

## Motivation

The dispatch procedure in [`goal-conductor/SKILL.md`](../../src/kiro_crew/builtin_skills/goal-conductor/SKILL.md) (lines 170 to 186) and in the conductor prompts in [`spec_prompts.py`](../../src/kiro_crew/agent_parts/spec_prompts.py) (lines 87 and 410) is four calls, each needing the previous call's result:

```mermaid
sequenceDiagram
    participant C as Conductor
    participant L as Work ledger
    participant W as Worker session
    C->>L: work_ledger_record create
    L-->>C: item_id
    C->>W: session_create
    W-->>C: session key
    C->>L: work_ledger_record bind(item_id, key)
    C->>W: session_send(key, seed)
```

Models often issue all four in one parallel batch. Then `bind` and `session_send` carry ids the model invented before the earlier replies came back. What stops the damage today, and what does not:

- `bind` refuses a key that names no open session (`unknown_worker_session`, `_refuse_unowned_worker` in [`work_ledger_conductor.py`](../../src/kiro_crew/dashboard/handlers/work_ledger_conductor.py) lines 559 to 650) and a session this conductor did not create (`worker_not_owned`, line 616). An invented key is refused. Item ids are random (`mint_item_id` in [`work_ledger_store.py`](../../src/kiro_crew/work_ledger_store.py) uses `secrets`), so an invented `item_id` is refused too.
- `session_send` has no binding admission gate. It reads a ledger hand-back through `read_send_handback` ([`session_control.py`](../../src/kiro_crew/dashboard/handlers/session_control.py) line 766) but never refuses because its target is unbound. A send that runs before, or without, a successful `bind` seeds a worker that runs unbound: its first `work_brief` answers `not_bound`, and its reports have nowhere to land. The parent RFC's §Binding lifecycle names this as the failure to avoid.

The skill already says "in exactly this order". A prompt cannot enforce ordering on a parallel batch, so the rule does not hold.

## Goals

- One conductor call dispatches one item, with no id the model has to carry between calls.
- The tool sends the seed only after `bind` returned success.
- Any failure names the step (`create`, `session_create`, `bind`, `seed`), the route's refusal code, and what the tool cleaned up.
- No new authority and no new unattended path: each step runs through the route the four-call path uses, as the same caller; only a caller that already owns a work ledger can use it (the 2026-10-28 amendment); and it is not auto-approved.

## Non-goals

- Changing `session_create`, `session_send`, `work_ledger_record`, or anything on `kirocrew-work`. The 2026-10-28 amendment makes one exception: the record route handles a create marked `dispatch`. The `work_ledger_record` tool, its schema and `kirocrew-work` stay unchanged.
- Auto-approving the tool (see Open questions).
- Batch dispatch of several items in one call. A round is a loop of `work_dispatch` calls.
- Removing the four-step path. A rebind onto an existing session (`goal-conductor/SKILL.md` line 463) and debugging still need it.
- Crew-member DM dispatch. Member specs mount `@kirocrew-dashboard` without `@kirocrew-work` and auto-approve `session_create` and `session_send` (`_MEMBER_DASHBOARD_GRANTS`, [`spec_grants.py`](../../src/kiro_crew/agent_parts/spec_grants.py) lines 298 to 303). They keep a ledger-free create-and-send, and `work_dispatch` refuses them: since the 2026-10-28 amendment, because they own no ledger (`no_ledger`).

## Design

### Placement

`work_dispatch` is a `kirocrew-dashboard` tool, defined in [`mcp_dashboard_session_tools.py`](../../src/kiro_crew/mcp_dashboard_session_tools.py) with its runner beside `_run_session_create` in [`mcp_dashboard_session_runs.py`](../../src/kiro_crew/mcp_dashboard_session_runs.py). It cannot live on `kirocrew-work`, for two reasons:

- `kirocrew-work`'s charter is that nothing on it reaches `enqueue_or_run_prompt` ([`mcp_work.py`](../../src/kiro_crew/mcp_work.py) lines 8 to 15; parent RFC §Non-goals "Not delivery"). A seed is a prompt.
- Every `kirocrew-worker` mounts `@kirocrew-work` ([`worker_agent.py`](../../src/kiro_crew/agent_materialization/worker_agent.py) line 821), and `kirocrew-dashboard` is kept off workers so that a worker cannot reach `session_send` by construction (same file, lines 412 to 418). On `kirocrew-work`, a worker would gain a verb that opens and seeds sessions.

Placement on `kirocrew-dashboard` must not hand the ledger to sessions that never had it. `/api/work-ledger/record` checks the caller's session key, not which server sent the request ([`work_ledger_caller.py`](../../src/kiro_crew/dashboard/handlers/work_ledger_caller.py) lines 51 to 95), and `action=create` opens a ledger for any such caller. So `work_dispatch` first checks that the caller's agent spec mounts `@kirocrew-work`, and refuses with `work_ledger_not_mounted` otherwise. The tool then reaches exactly the sessions that already hold `session_create`, `session_send` and `work_ledger_record`. The 2026-10-28 amendment below moves this check into the record route and replaces the spec read.

### Steps

The runner calls the existing gateway routes in order, as the caller, and reads their JSON replies (not the rendered text `_run_session_create` returns, so the folder walk and create call are factored into a helper that returns the structured response):

| Step | Route | On refusal |
|---|---|---|
| 1 create | `POST /api/work-ledger/record` `action=create` | stop; nothing to clean up |
| 2 open | the `session_create` folder walk plus `POST /api/session-control/create` | close the step-1 item (`action=close`, `state=abandoned`) |
| 3 bind | `POST /api/work-ledger/record` `action=bind` | close the step-2 session (`POST /api/session-control/close`), then the item |
| 4 seed | `POST /api/session-control/send` | no cleanup: the item is bound and the session exists |

Inputs: `title`, `acceptance` (as `action=create` takes them), `agent`, optional `agent_kind`, `folder`, `session_title` (defaults to `title`), `seed`, and the optional `model` / `reasoning_effort` that `session_create` takes. No input names a session: the seed target and cleanup target are only ever the `target` key step 2's reply returned. Output: `item_id`, `worker_session_key`, and on failure `failed_step`, the refusal code, and one outcome per cleanup action, with the ids of anything that survived.

Cleanup is best effort. A refused cleanup is reported, not retried. Folders the step-2 walk created are left in place, as `session_create` leaves them today when a later call fails.

A transport error after a request was sent leaves that step's outcome unknown, and the reply says so instead of calling it a refusal: `outcome_unknown` with the step name and whatever ids the tool already holds. After a lost step-1 or step-2 reply the tool has no id for what may have been created, so it sends nothing, cleans up only what it holds, and tells the conductor to read the board and sidebar. After a lost step-3 reply it sends nothing and cleans up as for a refused bind. After a lost step-4 reply it tells the conductor to check the session with `session_read_message` before resending, because a blind resend can seed twice.

### What is guaranteed

**The tool never seeds a session whose `bind` did not succeed.** A session that `session_create` opened is empty and idle until something sends to it. Only step 4 sends, and only after step 3 succeeded. A crash or refused cleanup after step 2 leaves at worst an idle, unseeded session and an open unbound item, both visible and neither running.

**What it does not guarantee.** A person can still act in the gap between step 3 and step 4: send into the new tab, or close the item from the Crew page. The conductor cannot, because the `item_id` and key are not returned to it until the tool finishes, and the conductor is its items' only writer. A person closing the item in that gap gets a seeded worker whose reports are refused `item_closed`, which the board shows. Closing that gap needs a binding admission check inside `session_send`, which is the ledger coupling Q1 avoids.

**Exact target.** Steps 3 and 4 and the cleanup pass the slot key step 2 returned. `_resolve_slot` in [`target_authorization.py`](../../src/kiro_crew/dashboard/session_control_parts/target_authorization.py) (line 553) also matches stems and titles, but refuses a string that matches two different slots, so a key that collides with another slot's title is a refusal rather than a wrong target. The tool runs only on approval, so a person sees the call before it acts.

### Why the MCP process orchestrates, not a new gateway route

Reusing the four routes keeps every existing check on its own path: the create rate limit, the goal's open-item cap (`goal_concurrency_cap_reached`, [`work_ledger_writes.py`](../../src/kiro_crew/work_ledger_writes.py) line 551), folder preflight, `bind`'s ownership and workspace fences, and the session-control audit records. One in-process gateway route would have to re-run each of those or call into handlers it does not own.

### Prompt and skill

`goal-conductor/SKILL.md` "Dispatch a round" and the conductor prompts in `spec_prompts.py` name `work_dispatch` as the dispatch path. The four steps stay in the skill as the fallback, with the rule that they run one call at a time.

## Migration plan

One implementation PR, under 600 lines: the tool, its input schema in the validation module, the structured create helper, the prompt and skill text, and tests.

Exit criteria:

- A refused `create` (for example `goal_concurrency_cap_reached`) opens no session and sends nothing.
- A refused `session_create` sends nothing, and the step-1 item is closed.
- A refused `bind` sends nothing, and the step-2 session and step-1 item are closed.
- A refused cleanup is reported with the surviving ids, and nothing was sent.
- A lost reply at any step is reported as `outcome_unknown` for that step, never as a refusal, and nothing is sent after a lost step 1, 2 or 3.
- A refused or lost seed returns both ids.
- A caller whose spec does not mount `@kirocrew-work` is refused `work_ledger_not_mounted` before step 1. Superseded by the 2026-10-28 amendment: a caller that owns no ledger is refused by the record route at step 1, with nothing opened.
- Each failure reply names the failed step and the route's refusal code.
- The happy path returns the `item_id` and session key the routes returned.
- No `kirocrew-worker` spec can reach the tool.
- Tests synchronize on events, never on fixed sleeps.

## Backward compatibility

Additive. A conductor that never calls `work_dispatch` behaves as today. No grant is added. The conductor specs withhold `session_send` (`spec_grants.py` lines 205 to 207), so for them the new tool costs the same one prompt per item. Member specs are refused the tool (Non-goals), since the 2026-10-28 amendment because they own no ledger.

## Security considerations

- No new authority: each step is an existing route called with the caller's own session key, so a refusal the caller would get calling the step directly it gets here. A session that mounts only `kirocrew-dashboard` gains no ledger: since the 2026-10-28 amendment, a `dispatch` create never opens one.
- No new unattended path: the tool is not auto-approved. A conductor prompts once per item, as it prompts once per `session_send` today, and the person reads the seed text in that prompt. `spec_grants.py` lines 205 to 207 give the reason that prompt exists: nothing bounds what `session_send` sends.
- Workers cannot reach it, because they do not mount `kirocrew-dashboard`.
- The seed target is produced by step 2, never chosen by the model.
- `kirocrew-dashboard` already carries the channel-agent block on its session-control tools; the new tool joins that list.

## Alternatives considered

- **Put it on `kirocrew-work`.** Rejected: that server must never deliver a prompt, and every worker mounts it (Placement above).
- **Fuse into `session_create(work_item=...)`**, Q1's original shape. Rejected: it couples `session_create` to the ledger for every caller, which the parent RFC names as the cost.
- **A `dispatch` action on `work_ledger_record`.** Rejected: a ledger verb on `kirocrew-work` that opens and seeds a session hits both Placement problems.
- **Refuse `session_send` to an unbound worker of a conductor.** `session_send` already reads the ledger for hand-backs, so the read is not new. What is new is a refusal: every send would have to decide whether its target is a worker-in-waiting, which a person's send into that tab, a member's send and a rebind all hit. It also leaves the model carrying ids across four calls, so a wrong-but-valid `item_id` from another of its own items still binds.
- **Prompt rule only.** Already in place; does not hold under parallel tool calls.

## Amendment 2026-10-28: the record route enforces the access check

Read at `47c71cbc9e`. This replaces the `@kirocrew-work` spec check wherever the RFC names it: Summary, Goals, Placement, Non-goals, Backward compatibility, Security considerations and the exit criteria. Each of those now points here. Everything else stands.

### Why a check in the tool cannot hold

The implementation PR tried three answers inside the tool to "may this caller hold a ledger". Review found each one wrong on some backend:

| Source the tool read | Breaks on | Why |
|---|---|---|
| The agent spec on disk | every backend | launch also applies `tools`, a global mute, `disabled` and the project-or-user spec choice; each one copied was one more review finding |
| The launch `mcpServers` array (`session_mcp_projection`) | kiro, KAS | the array exists only for `ACP_BACKENDS_SESSION_MCP_ARRAY` ([`backends_mcp.py`](../../src/kiro_crew/agent_sdk/backends_mcp.py) line 142); kiro and KAS mount their own servers |
| The session's MCP report, `mcp_report.ready` | the 7 array backends | `run_mount_check` records missing servers only, never ready ones ([`mcp_mount_check.py`](../../src/kiro_crew/acp/mcp_mount_check.py) lines 206 to 238) |

### The contract: `work_dispatch` never opens a ledger

The record route authenticates the caller's session, not which MCP server sent the request or what that session mounts ([`work_ledger_caller.py`](../../src/kiro_crew/dashboard/handlers/work_ledger_caller.py) lines 51 to 95). So the route cannot prove a mount either. What it can enforce is that `work_dispatch` never opens a ledger:

- `work_dispatch` sends its step-1 `action=create` with `dispatch: true`.
- The record route reads and removes `dispatch` from the body before it validates the rest against `WORK_LEDGER_RECORD_SCHEMA`. That schema is also the `work_ledger_record` tool's input schema, and it refuses unknown fields, so it stays unchanged and the tool cannot carry the field.
- A `dispatch` create is a write on an existing ledger. The route runs `_own_ledger` ([`work_ledger_caller.py`](../../src/kiro_crew/dashboard/handlers/work_ledger_caller.py) line 297) for the verified caller key instead of bootstrapping, and refuses `no_ledger` when the caller owns none. Nothing is written, so nothing needs cleaning up.
- That refusal names one remedy: open the ledger with `work_ledger_record action=goal`, which the goal-conductor skill's Round 0 already does. Unlike `_own_ledger`'s usual text, it does not suggest `action=create`.
- The tool reads no spec, no launch array and no MCP report.

`bind` already requires an existing ledger through the same `_own_ledger` call (the `else` branch at [`work_ledger_conductor.py`](../../src/kiro_crew/dashboard/handlers/work_ledger_conductor.py) lines 251 to 256), so it needs no change.

### Why that keeps a ledger from a session without `kirocrew-work`

A ledger is opened only by `_bootstrap`, which is reached only from the record route's `goal` and unmarked `create` (same lines). The ledger header is also rewritten by `/api/work-ledger/rebuild` ([`work_ledger_conductor.py`](../../src/kiro_crew/dashboard/handlers/work_ledger_conductor.py) line 486), from the crew log. Among the tools, only `kirocrew-work`'s `work_ledger_record` and `work_ledger_rebuild` send those requests. `work_dispatch` is the one other tool that reaches the record route, and it always marks its create. So no tool lets a session without `kirocrew-work` open a ledger.

`dispatch: true` comes from the request body, and that is safe because it can only add a refusal. A request without it takes today's path. `kirocrew-work` cannot send it: `_RECORD_FIELDS` in [`mcp_work.py`](../../src/kiro_crew/mcp_work.py) does not list it, and a test pins that. The `X-Internal-Caller` header is not used, because its trust model is attribution that grants nothing ([`token_auth_callers.py`](../../src/kiro_crew/dashboard/token_auth_callers.py) lines 774 to 812).

What the route does not decide: a request that reaches it outside any tool, with the internal secret and a session key, is judged as it is today. This amendment adds no such path.

### What it costs

A conductor must open its ledger before its first `work_dispatch`. A session that mounts `kirocrew-work` does that with one auto-approved call (`work_ledger_record` is in `_CONDUCTOR_BASE_WORK_GRANTS`). A session without `kirocrew-work` cannot open one, so it is refused at step 1.

The check is ledger ownership, not a live mount. A session that opened a ledger and later lost `kirocrew-work` keeps `work_dispatch`. That can happen through a spec edit, `disabled`, a global mute or a `tools` change. This grants no new authority: that session already held the ledger and `session_create` and `session_send`. It does mean it can dispatch items whose reports it can no longer read until the mount is back.

### Exit criteria, replacing the spec-check one

- A `dispatch` create from a caller with no ledger is refused `no_ledger` by the real record route, and no ledger, item, session or seed is made.
- A `dispatch` create from a caller with a ledger goes through the real record route and store. The rule reads nothing about the backend, so one test covers kiro, KAS and the array backends alike.
- A create without `dispatch` still bootstraps a ledger, as today.
- `_RECORD_FIELDS` does not list `dispatch`, and `WORK_LEDGER_RECORD_SCHEMA` does not accept it.
- The `dispatch` refusal names `action=goal` and not `action=create`.
- Each test fails with the route rule removed.

## Open questions

- Q1. Should conductors later auto-approve `work_dispatch`? Not proposed here. A grant would remove the one point where a person reads the seed, and the child inherits the caller's trust posture at creation. A board with no recorded goal has no goal concurrency cap (`work_ledger_writes.py` lines 541 to 545), only the per-conductor ceiling of 32 open items (`MAX_ITEMS_PER_CONDUCTOR`, [`work_ledger_records.py`](../../src/kiro_crew/work_ledger_records.py) line 132) and the create rate limit. A grant needs its own decision against those costs.
