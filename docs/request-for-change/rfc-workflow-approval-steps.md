---
title: Approval steps inside workflows — ctx.approve with a timeout, a default action, a reminder, and owner-bound delivery to the dashboard and channels
status: draft
author: javenciu
created: 2026-11-09
last-audited: 2026-11-09
audited-at: 1e3b894d87
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Approval steps inside workflows

Status: draft. Acceptance requested from a maintainer of the workflow engine (`src/kiro_crew/workflows/`) and from a maintainer of channel approvals (`src/kiro_crew/slack/`, `src/kiro_crew/messaging/`). Every main-side reference below was read at `1e3b894d87`.

## Summary

The workflow DSL already declares `ctx.approve(prompt) -> bool` and an `approval_requested` event, but the gateway never wires the `approve` port, so a script that calls it is rejected before it runs. This RFC wires that port. A step that calls `ctx.approve` pauses the run, shows Approve and Reject to the gateway owner on the dashboard and on one channel (Slack first, other channels through an existing delivery seam), sends one reminder, and resolves to `False` when no one answers before the timeout. A decision is accepted only from the owner's authenticated identity on that surface, counts once, and is written to the security event log (SEL). A link or button value copied out of a message and opened by someone else decides nothing. The whole feature sits behind `agent.workflow_approvals_enabled`, default off.

## 1. Problem

### 1.1 Current state on main

- **The DSL declares the gate.** `WorkflowContext.approve(self, prompt: str) -> bool` is part of the frozen protocol (`src/kiro_crew/workflows/__init__.py` `WorkflowContext.approve`, docstring "Human-in-the-loop gate; resolves to the human's decision"). `test/test_workflows_conformance.py` (`EXPECTED_CTX_METHODS`) pins its signature to `(self, prompt)`.
- **The runner delegates to a port.** `WorkflowRunner` reads `ports.get("approve")` (`src/kiro_crew/workflows/runner.py` `_RunContext.__init__`), and `approve()` raises `RuntimeError("ctx.approve is not available for this run (no approve port wired)")` when the port is absent (`runner.py` `_RunContext.approve`).
- **The service wires no approve port.** The only port the service builds for every run is `ports: dict[str, Any] = {"nudge": _nudge}` (`src/kiro_crew/workflows/service.py` `WorkflowService._runner`); the session ports added after it (`open_session`, `send_to_session`, `set_session_mode`) are conditional and none is `approve`.
- **So validation rejects the call.** `approve` is outside `CORE_CTX_SURFACE` (`src/kiro_crew/workflows/validate.py` `CORE_CTX_SURFACE`), and `check_ctx_surface` (`validate.py` `check_ctx_surface`) refuses any reference to a ctx attribute the runner did not wire. `docs/system-specs/modules/workflows.md` lists `ctx.approve` among the host-wired primitives (line 388).
- **The event exists and nothing emits it.** `approval_requested` is in `EVENT_TYPES` (`workflows/__init__.py` (`EVENT_TYPES`)), its required keys are `("prompt", "approval_id")` (`workflows/events.py` (`REQUIRED_DATA_KEYS`)), and its builder is `events.py` `EventStream.approval_requested`. `grep -rn approval_requested src/kiro_crew` finds only those three sites; workflows.md line 519 says "builder exists, nothing emits it".
- **Waits are bounded by the run ceiling.** `MAX_RUN_TIMEOUT_SECS = 6 * 3600` (`runner.py` `MAX_RUN_TIMEOUT_SECS`), clamped per run by `clamp_run_timeout` (`runner.py` `clamp_run_timeout`) and configured as `agent.workflow_run_timeout_secs` (`src/kiro_crew/config/sections_agent.py` `AgentConfig`, band 60..21600 in `config/loader_builders.py` `_agent_section_fields`). A run that is running when the gateway restarts is demoted to failed (workflows.md, "Resume and restart-subtree").

### 1.2 Approval machinery that already exists elsewhere

- **Slack tool-approval buttons.** A button carries `registry_key|nonce` (`src/kiro_crew/slack/approval_tokens.py` `build_approval_token`); a token with no nonce is refused (`approval_tokens.py` `split_approval_token`). `_handle_tool_approval` (`src/kiro_crew/slack/interactions_approvals.py` `_handle_tool_approval`) gates approve and trust on `channel_inbound_permitted("slack")` and `channel_refusal(...)`, lets a reject through governance, restricts trust to DMs, and replaces the buttons with an outcome label. Linked dashboard approvals write `sel().log_api_access(operation="slack.interactive.approval_linked", ...)` (`src/kiro_crew/slack/handler_runtime/approvals.py` `_resolve_linked_click`). `is_owner(user_id)` is at `src/kiro_crew/slack/handler_runtime/access.py` `is_owner`.
- **Typed-reply approvals for channels without buttons.** `src/kiro_crew/messaging/approval.py` states the house shape: deny on silence, an answer bound to one request and removed when delivered, authorization decided by the channel that knows its operator.
- **One delivery seam for every channel.** `src/kiro_crew/messaging/spawn_approval_delivery.py` is a process-global registry a channel dispatcher registers a hook into (`register_channel_delivery`, line 68), keyed by channel namespace and resolved from a session key (`resolve_channel_delivery`, line 112). Discord already implements a hook (`src/kiro_crew/discord/transport_dispatch_spawn_approval.py`). The host spawn gate consults it first and falls back to the Slack owner DM and the dashboard (`src/kiro_crew/slack/gateway_runtime/subagent_wiring.py` `_spawn_approve`).
- **Blocking question cards on the dashboard.** `src/kiro_crew/dashboard/handlers/ask_question.py` holds a request open until the user answers, offers `GET /api/ask-question/pending` for rehydration after a reload, refuses app tokens (`_deny_app_token`, line 85) and non-owners (`_deny_non_owner`, line 128). Owner identity on a dashboard request is `is_owner_dashboard_request` (`src/kiro_crew/dashboard/handlers/source_providers.py` `is_owner_dashboard_request`).
- **The SEL.** `SecurityEventLog.log_api_access(caller=, operation=, outcome=, source=, resources=, error=, critical=)` (`src/kiro_crew/sel.py` `SecurityEventLog.log_api_access`). `critical=True` writes synchronously and re-raises a write failure so the caller can refuse the action. The chain is serialized across processes (`src/kiro_crew/sel_chain_lock.py`).
- **A file store discipline to copy.** `src/kiro_crew/webhooks.py` does read-modify-write under an advisory `flock` on a sidecar lock, then an atomic `os.replace` of a 0600 temp file (module docstring, lines 22-23; lock at line 349).

### 1.3 The gap

A workflow whose last step writes somewhere (sends a message, opens a pull request, posts in a channel) has two options today: trust the whole run, or do not run it. TaskRunner has per-step gates (`src/kiro_crew/task_executor.py` (`_HEADLESS_DENY_REASON`), `if task.requires_approval or task.force_approval:`), but they wait with no deadline, default, or reminder. Nothing reaches a person who has walked away from the dashboard.

## 2. Prior art

Behaviors only, from public documentation. No code or text is taken from any of these.

| Product | License | Behavior that informs this design | Source |
|---|---|---|---|
| Zapier Human in the Loop | proprietary | A step sends approve or reject (and optionally edit) by Slack, email, Teams or the app. The author sets a timeout value and unit, picks "skip and continue" or "end run" on timeout, and can send a reminder. | https://help.zapier.com/hc/en-us/articles/38731463206029 |
| Windmill approval steps | AGPL-3.0 core, commercial enterprise edition | A suspend step can need N approvals and is cancelled after a timeout; approval steps take the same retry settings as other steps. | https://www.windmill.dev/docs/flows/flow_approval |
| Trigger.dev waitpoint tokens | Apache-2.0 | A run waits on a token with a timeout; the wait returns a result that says whether it timed out, and the task decides what to do. | https://trigger.dev/docs/wait-for-token |
| n8n human review for AI tools | Sustainable Use License | Review can gate only selected tools of an agent step, with the review sent to a chat channel. | https://docs.n8n.io/advanced-ai/human-in-the-loop-tools/ |
| Activepieces waitpoints | MIT community edition, commercial enterprise edition | Approval barriers persist the paused execution and support quorum and veto. | https://www.activepieces.com/docs/install/architecture/waitpoints |
| OpenClaw mobile apps | not checked in this recon | The Android app marks sessions waiting on an answer or an approval, so a phone shows what is blocked. | https://raw.githubusercontent.com/openclaw/openclaw/HEAD/docs/platforms/android.md |

What carries over: a per-step timeout, a declared default on timeout, one reminder, delivery where the person already is. What this design declines: approve-on-timeout (§6.2), quorum (§6.3), and a decision by link in email (§6.4).

## 3. Goals and non-goals

### Goals

1. `ctx.approve(prompt, ...)` works in a dynamic workflow when the flag is on, and keeps today's validation refusal when it is off.
2. The author sets `timeout_secs`, `on_timeout` and `remind_after_secs`. Silence never means yes.
3. The request shows on the run tree, on the chat run card, as a dashboard notification, and on one channel the owner configured.
4. Only the gateway owner can decide, on any surface. A decision counts once. Every request, reminder, decision, refusal, expiry and cancellation is in the SEL.
5. A forwarded message, a copied button value, or a shared dashboard link decides nothing for the person holding it.

### Non-goals

- Approvals that outlive a gateway restart. Until durable resume (a separate proposal) lands, a restart cancels a pending approval with the run, and the timeout is clamped to the run's remaining ceiling.
- Quorum (N of M approvers), veto, or an "edit before approving" field.
- Per-tool approvals inside an agent step. Agent steps keep `ToolApprovalPolicy.AUTO_APPROVE` (`workflows/agent_exec.py` `agent_fn`).
- Email delivery and Web Push. Both are follow-ups that must meet §5.3 first.
- Changing TaskRunner's step gates.

## 4. Design

### 4.1 DSL

```python
async def approve(
    self,
    prompt: str,
    *,
    timeout_secs: int = 3600,
    on_timeout: str = "reject",      # "reject" | "fail"
    remind_after_secs: int | None = None,
) -> bool
```

- New parameters are keyword-only with defaults, so every existing call shape `ctx.approve("text")` keeps working. The conformance pin at `test/test_workflows_conformance.py` (`EXPECTED_CTX_METHODS`) gains the three `KEYWORD_ONLY` rows.
- `timeout_secs` is clamped into `[60, remaining run ceiling]`. The approval wait counts against the run timeout like any other step.
- `on_timeout="reject"` returns `False`, so the script takes its own "not approved" branch (the Trigger.dev shape). `on_timeout="fail"` ends the run as `run_failed` with `where="approval_timeout"` (Zapier's "end run"). There is no `"approve"`.
- `remind_after_secs` sends one reminder if no decision has arrived by then; it must be less than `timeout_secs` or it is ignored with a `log` event.
- `validate.py` rejects a non-literal `on_timeout` and an unknown literal. `approve` stays in `NULLABLE_CTX_METHODS` (`validate.py` `NULLABLE_CTX_METHODS`) unchanged.
- At most 8 pending approvals per run, so a `ctx.parallel` fan-out cannot flood the owner's DMs. The ninth call raises in the script.

### 4.2 The pending-approval store

New module `src/kiro_crew/workflows/approvals.py`. One JSON file per gateway under the workflows state directory, written with the `webhooks.py` discipline (flock on a sidecar, 0600 temp file, `os.replace`). A row:

| Field | Meaning |
|---|---|
| `approval_id` | `secrets.token_urlsafe(16)`; names the request. Not a credential. |
| `run_id`, `call_index` | The run and the ctx call that is waiting. |
| `owner` | The owner identity captured when the request opens (§5.1). |
| `prompt` | Redacted and clipped text (§5.4). |
| `created_at`, `remind_at`, `expires_at` | UTC ISO timestamps. |
| `state` | `pending`, `approved`, `rejected`, `expired`, `cancelled`. |
| `decided_by`, `decided_via` | The identity and surface of the decision. |
| `nonce_digests` | `{surface: sha256(nonce)}` for each surface the request was delivered to. |

The only transition out of `pending` is a compare-and-set under the lock, so two presses on two surfaces cannot both win. The runner holds an `asyncio.Future` per `approval_id`; the store's winning transition resolves it. On gateway start, every `pending` row is set to `cancelled` with reason `gateway_restarted`, matching the run's own demotion to failed. Rows older than 30 days are pruned on write.

The file persists so the dashboard can rehydrate cards after a reload and so a restart can close what it interrupted. It is not a resume mechanism.

### 4.3 Wiring

- `service.py` adds `ports["approve"]` beside `nudge` (line 799) only when `agent.workflow_approvals_enabled` is true, as a `functools.partial` bound to `run_id`, the run's launching session key, and the owner identity. With the flag off the port is absent and `check_ctx_surface` refuses the script exactly as today.
- The port opens a store row, emits `approval_requested` through the existing builder (`events.py` `EventStream.approval_requested`) with `prompt` and `approval_id`, delivers (§4.4), arms the reminder and timeout timers, and awaits the future.
- A new event type `approval_resolved` with required keys `("approval_id", "decision", "via")` closes the pair, where `decision` is one of `approved`, `rejected`, `expired`, `cancelled`. Adding it to `EVENT_TYPES` is a frozen-contract change and is called out in the PR. Run cancel (`registry.py` `RunRegistry.cancel`) cancels the future and marks the row `cancelled`.

### 4.4 Delivery

Every request goes to the dashboard. It also goes to at most one channel, chosen in this order:

1. The channel of the run's launching session, when that session is channel-born: `channel_namespace_of(session_key)` resolves a hook from a delivery registry shaped like `spawn_approval_delivery.py`. The RFC proposes generalizing that registry to carry a `kind` (`spawn` or `workflow`) rather than adding a second registry, so Discord, Telegram and the other channels under `src/kiro_crew/` implement one hook each.
2. Otherwise the Slack owner DM, when Slack is configured and an owner id is set (the fallback the spawn gate takes when no channel hook answers, `subagent_wiring.py` `_spawn_approve`).
3. Otherwise dashboard only.

**Dashboard.** An Approve / Reject card on the run in `website/src/apps/workflows/WorkflowRunTree.tsx` and on the chat run card, plus a bell notification through the existing notification route. Routes:

- `GET /api/workflows/approvals/pending` returns pending rows for rehydration, owner only.
- `POST /api/workflows/approvals/{approval_id}/decide` with body `{"decision": "approve"|"reject", "nonce": "..."}`, owner only, app tokens refused with the same shape as `ask_question.py:_deny_app_token`. The dashboard receives its nonce over the authenticated websocket broadcast, never in a URL.

**Slack.** An owner DM with the prompt and two buttons, `action_id` `workflow_approve` and `workflow_reject`, whose `value` is `approval_id|nonce` built with `build_approval_token` (`approval_tokens.py` `build_approval_token`). The interaction handler sits beside `_handle_tool_approval` in `interactions_approvals.py` and applies the same gates: governance and channel policy on approve, reject allowed through, approve accepted only in a DM. After a decision from any surface, the posted prompt is updated to the outcome label and its buttons removed. The message also carries a plain dashboard link to the run (`/#/workflows/runs/{run_id}`), which holds no nonce and no `approval_id`.

**Other channels.** A channel with buttons posts its inline keyboard through its hook and reports the press with the pressing user's id. A channel with no buttons uses typed replies only if its transport already binds the sender to the owner (the rule in `messaging/approval.py`); otherwise it gets a notice and the link, and the decision happens on the dashboard.

**Reminder.** At `remind_at`, one reminder goes to the same surfaces: a thread reply under the original owner DM, a fresh bell notification. It reuses the original nonces. There is never a second reminder.

## 5. Trust-boundary analysis

### 5.1 Who may decide

The approver is the gateway owner: `is_owner_dashboard_request(request)` on the dashboard, `is_owner(user_id)` on Slack, and the hook's attested owner on other channels. The identity is checked on the inbound decision itself, not taken from anything the request carries. A press by anyone else is refused, logged with `outcome="denied"` and `error="not_owner"`, and leaves the buttons in place so the owner can still press them, as `_handle_tool_approval` does today. When Shared Crew is on and a collaborator launched the run, the approver is still the owner in the first version; whether the launching principal may approve their own run is open question 1.

### 5.2 Single use

A decision needs three things to match: the inbound identity is the owner, the `nonce` hashes to the digest stored for that surface, and the row is `pending`. The compare-and-set under the store lock means the first matching decision wins and every later one, including a replayed Slack payload or a second tab, gets `already_resolved`. A nonce is bound to one surface, so a Slack button value pasted into the dashboard route fails the digest check even for the owner.

### 5.3 A forwarded link is not a bearer token

- The dashboard deep link carries no secret. Opening it requires the dashboard's own sign-in, and the decide route checks owner identity first.
- The Slack button value is useless outside a Slack interaction payload, and Slack sends the clicking user's id with that payload. Forwarding the DM or sharing the message puts the buttons in front of someone whose id fails `is_owner`.
- No route decides on `GET`, so a link preview, an unfurl or a prefetch cannot approve anything. Slack unfurling is turned off on these messages.
- Any future surface that wants one-tap approval from outside the dashboard (email, Web Push) must still land on an authenticated owner check. A signed URL that approves by itself is out of bounds for this feature.

### 5.4 The prompt is untrusted text

The prompt is built by a script and often holds agent output. Before storage and delivery it passes `redact_credentials` and `redact_exfiltration_urls` (the pair `interactions_approvals.py` already imports), is clipped to 2,000 characters, is rendered as plain text with Slack mrkdwn escaped, and links in it are not unfurled. The SEL row stores `run_id:approval_id` and a SHA-256 of the prompt, not the prompt, because the SEL is served over `/api/sel/events` and is append-only.

### 5.5 Silence and failure

- Timeout resolves to `False` or a failed run. No configuration turns silence into approval.
- The decision is written with `log_api_access(..., critical=True)`. If the SEL write fails, the decision is refused and the row stays `pending`; the owner sees an error and the run keeps waiting until its timeout.
- If every delivery fails, the request still exists on the dashboard, and the run times out on schedule.

### 5.6 SEL operations

| Operation | Caller | Outcome values |
|---|---|---|
| `workflow.approval.request` | `run:<run_id>` | `ok` |
| `workflow.approval.remind` | `run:<run_id>` | `ok` |
| `workflow.approval.decide` | the deciding identity | `allowed` (approved), `rejected`, `denied` (not owner, bad nonce, already resolved, policy) |
| `workflow.approval.expire` | `run:<run_id>` | `expired` |
| `workflow.approval.cancel` | `run:<run_id>` | `cancelled` |

`source` is `dashboard`, `slack` or the channel namespace; `resources` is `<run_id>:<approval_id>`.

## 6. Alternatives considered

1. **Reuse the blocking ask-question card.** `ask_question.py` already holds a request open and rehydrates. It is scoped to a chat slot, it has no channel delivery, and a workflow run started by cron has no slot. The card UI is reused; the route and store are not.
2. **Allow `on_timeout="approve"`.** Zapier's "skip and continue" amounts to this. Rejected: it makes silence a yes, which `messaging/approval.py` rules out for tool approvals, and a run that would proceed anyway does not need the gate.
3. **Quorum and veto.** Useful for teams (Windmill, Activepieces). Kiro Crew is a single-owner gateway, so N-of-M has one possible approver. Revisit with Shared Crew roles.
4. **Approve by signed link** (email or push). One tap from anywhere, but whoever holds the link can approve. Rejected for this feature (§5.3).
5. **A second, workflow-only channel registry.** Simpler to land, but every channel would implement two nearly identical hooks. Generalizing `spawn_approval_delivery.py` costs one parameter.
6. **Keep the approval in memory only.** Smaller, but a dashboard reload loses the card unless the run tree re-derives it from events, and a restart leaves stale buttons in Slack with no record of why they stopped working. The file is small and follows an existing discipline.

## 7. Migration

None. With the flag off, behavior is unchanged: `approve` is not wired and scripts that reference it fail validation as today. With it on, existing one-argument calls get the defaults. No stored data changes shape. The new event type is additive for readers that ignore unknown types; the run tree must render it (PR 3).

## 8. Rollout

`agent.workflow_approvals_enabled: bool = False`, in `sections_agent.py` beside `workflow_run_timeout_secs`. Off means no port, no routes registered, no Slack action handler. The port reads the setting when a run starts, so a change applies to runs started after it and never to a run already waiting. After two releases with no open defects the default can be revisited in a follow-up RFC change.

## 9. PR plan

Each PR is independently mergeable and under about 300 source lines.

1. **Store and port, no surfaces.** `workflows/approvals.py`, the keyword-only DSL parameters, the conformance pin, validation of `on_timeout`, the port wiring behind the flag, `approval_requested` and `approval_resolved` events, timeout and cancel. Tests: single-use compare-and-set under concurrent decide, timeout returns `False`, `on_timeout="fail"` fails the run, cancel resolves the row, restart marks pending rows cancelled, flag off keeps the validation refusal.
2. **Dashboard decide and pending routes, plus SEL.** Owner gate, app-token refusal, nonce digest check, `critical=True` audit and refusal on SEL failure. Tests for non-owner, app token, wrong nonce, replay, SEL failure.
3. **Run-tree and chat-card UI.** Approve / Reject card, pending rehydration, `approval_resolved` rendering, bell notification.
4. **Slack delivery.** Owner DM, buttons, handler with governance gates, DM-only approve, outcome label, one reminder. Tests for non-owner press, forwarded-message press, replayed payload, reject in a channel.
5. **Generalize the channel delivery registry** with a `kind`, and move the Discord hook onto it. Other channels follow one PR each.

Exit criteria for the set: with the flag on, a script calling `ctx.approve("send?", timeout_secs=120, remind_after_secs=60)` shows a card on the dashboard and an owner DM in Slack; a press by the owner on either resolves the step and updates both; a press by another Slack user is refused and logged; with no press the step returns `False` at 120 s; the SEL holds one `request`, one `remind`, and one `decide` or `expire` row.

## 10. Open questions

1. Under Shared Crew, may the principal who launched a run approve its steps, or only the owner?
2. Should the reminder also go to the dashboard notification bell when the owner chose a channel, or only to the channel?
3. Is 8 pending approvals per run the right cap, or should it follow the run's agent cap?
4. When durable resume lands, should a pending approval survive a restart with its original `expires_at`, or restart its timeout?
