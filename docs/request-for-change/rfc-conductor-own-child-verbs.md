---
title: A conductor sends to and closes its own children, and reports on its own item, without an approval prompt
status: implemented
author: kirocrew-worker, directed by iamwhatever
created: 2026-11-08
last-audited: 2026-11-08
audited-at: e48fe0ec03
doc-pr: 33043
implementation-prs: [33373]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: A conductor sends to and closes its own children, and reports on its own item, without an approval prompt

Status: implemented by
[#33373](https://github.com/kirodotdev/KiroCrew/pull/33373), after acceptance in
[#33417](https://github.com/kirodotdev/KiroCrew/pull/33417), as proposed:
default on (Q1) and the four-step fallback's seed unprompted (Q2). It moves a trust
boundary, so it is RFC-first under
[CONTRIBUTING.md](../../CONTRIBUTING.md#pull-request-workflow). **The security
trade-off in section 6 is the repository owner's (iamwhatever) and the security
reviewer's decision**, not the author's. Every code reference below was read at
`43ac0cd051`. Paths are under `src/kiro_crew/` unless they start with `docs/`.

## 1. Summary

Under the Normal approval mode, every `session_send` and `session_close` that
`kirocrew-conductor` or `kirocrew-team-lead` makes prompts the person, including
when the target is a worker the conductor itself just created. An unattended
Ultra Mode or Bot conductor therefore cannot answer a worker's question or close
a finished one.

Bots already work without these prompts: a crew member's `session_send`,
`session_stop` and `session_close` are auto-approved under the `created_by`
ownership fence. This RFC gives the team lead and the root conductor the same
footing for `session_send` and `session_close`: auto-approved **only when the
target is a session the caller created**, decided per call at approval time from
the gateway-vouched creation edge. A send or close aimed anywhere else keeps
prompting exactly as today. A config switch turns the exemption off.

It also proposes one more grant (section 5.5): `work_report`, for a
conductor or team lead that another conductor dispatched. Every worker
already holds it; a nested conductor still prompts on it at each round
boundary. That reverses a gate [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md)
records, so it is decided here and not in a code PR.

`work_dispatch` is not decided here. It stays open as Q1 of
[rfc-work-dispatch.md](rfc-work-dispatch.md) (section 4).

## 2. Motivation

### 2.1 Current state

- The withholding comment above `_CONDUCTOR_DASHBOARD_GRANTS` in
  `agent_parts/spec_grants.py` keeps `session_send` from the conductor on
  purpose: it "Runs text as another session's user-role turn under that
  target's own grants ... nothing bounds WHAT is sent". The same comment keeps
  `work_dispatch` back on that reason and names granting it as
  rfc-work-dispatch.md's open question Q1. `session_close` is not in
  `_CONDUCTOR_DASHBOARD_GRANTS` either, so it prompts.
- **Precedent: Bots.** `_MEMBER_DASHBOARD_GRANTS` in the same file grants a
  crew member `session_send`, `session_broadcast`, `session_stop`,
  `session_retry` and `session_close`. Its comment gives the reason: a
  member's writes are server-bounded by the `created_by` ownership fence in
  `authorize_target`, so "the worst case of an auto-approved write is confined
  to worker sessions the member opened". On KAS the same set reaches a Bot's
  agent: `to_client_custom_agent` in `acp/kas_agents.py` merges
  `_MEMBER_DASHBOARD_GRANTS` into the agent's `allowedTools` when
  `member_dispatch` is on. `work_dispatch` is in no grant set, for members or
  conductors.
- The same comment says "The conductor has no such ownership fence". The
  conductor is creator-fenced server-side only while `agent.session_control`
  is off (`_conductor_fenced` in
  `dashboard/session_control_parts/caller_access.py`). With the switch on, it
  keeps the owner's reach. `CONDUCTOR_AGENTS` in the same file names
  `kirocrew-conductor` and `kirocrew-ledger-conductor`; it does not name
  `kirocrew-team-lead`, which is what Ultra Mode runs on
  (`DEFAULT_CREW_MODE_AGENT` in `crew_mode.py`).
- An approval-time exemption for a conductor's own workers already exists for
  reads: `_CREW_ROOT_OWN_WORKER_VERBS` (`dashboard/chat_turn/tool_approval.py`)
  holds `session_status` and `session_read_message`, and
  `_crew_root_own_worker_call` approves one only when the verified `target`
  argument is a live session whose gateway-vouched creator
  (`_vouched_creator_of`) is the caller. It is inert while
  `crew_mode.APPROVAL_ROUTING_LIVE` is `False`, which it is on main. Its one
  call site is `_decide_permission_request`, ahead of the Trust branch.

- **A nested conductor's own report.** `_LEDGER_CONDUCTOR_WORK_GRANTS` in
  `agent_parts/spec_grants.py` is the goal conductor's work-ledger grant set;
  `_conductor_spec` ships it to `kirocrew-conductor` and its alias, and the
  team lead mounts the same tuple (`agent_materialization/service_agents.py`).
  It grants `work_brief`, the worker verb that reads the caller's own bound
  item, and its comment keeps `work_report` gated: "it WRITES into the
  parent's record, across a dispatch relationship". `_WORKER_WORK_GRANTS` in
  the same file grants `work_brief` and `work_report` to every worker, with
  the reason "A worker that must ask permission to say it is blocked will not
  say it". [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md)
  records the conductor's omission in its Agent spec changes section and in
  its Phase 2.5 exit criterion ("still omits `work_report`").

### 2.2 The problem

The goal-conductor skill and the team-lead skill drive a loop of dispatch,
patrol, answer and close. A read of every place an Ultra Mode run stops
(goal-conductor skill, team-lead skill, the grant lists and the approval
decider) found that every answer and close is an approval stop with no person
truly needed when the target is the conductor's own child. Two more stops
follow from it: a draft PR is fixed by telling the worker to run
`gh pr ready`, and a ledger whose items all wait on an `approval` holds the
patrol (the work-ledger person-wait hold).

A conductor or team lead that another conductor dispatched is a ledger item
like any worker. Its skill tells it to report at each round boundary, and each
of those reports is an approval prompt in a child session nobody watches.
Until it is answered the parent's patrol does not wake, because a
`work_report` is the only thing that wakes it.

### 2.3 What people do today: Trust

Picking Trust on the conductor chat removes these prompts today, but it is
wider than the problem and does not last:

| Trust today | Detail |
|---|---|
| every tool, every argument | the Trust/YOLO branch of `_decide_permission_request` reads no tool name or argument |
| the whole child tree | children inherit Trust at birth (`inherit_posture` in `dashboard/approval_inheritance.py`, called by `create_session`) and follow later raises (`_apply_inherited_postures` in `dashboard/chat_api/slot_approval.py`) |
| any target | a Trust-approved `session_send` may reach any session the conductor's server-side reach allows, not only its children |
| lost on restart | Trust is in memory only; `create_session` notes that a restart returns the child to interactive along with its creator |

## 3. Goals

- G1. `session_send` and `session_close` from `kirocrew-conductor` or
  `kirocrew-team-lead` to a session that caller created run without an
  approval prompt under Normal and Reads, as a Bot's do today.
- G2. The same verbs aimed at any other session prompt exactly as today.
- G3. Nothing else is opened: no other tool, no other agent, and no change to
  the child's own approval mode.
- G4. The exemption survives a gateway restart when the creation edge is still
  vouched, and fails closed (prompts) when it is not.
- G5. An operator can turn it off with one config key.
- G6. A conductor or team lead dispatched as a ledger item calls
  `work_report` on its own item without a prompt, as a worker does.

## 4. Non-goals and not in scope

- **`work_dispatch`.** Granted to nobody today. Whether a conductor may
  auto-approve it is Q1 of [rfc-work-dispatch.md](rfc-work-dispatch.md), named
  by the withholding comment in `agent_parts/spec_grants.py`. This RFC does not
  decide it; `work_dispatch` keeps prompting. Section 10 Q2 records how this
  RFC touches the four-step fallback.
- **Not in scope: worker shell and file-write prompts** (a worker's own
  `execute_bash`, `fs_write`, `gh`, `git` under Normal, in `kirocrew-worker`
  and `kirocrew-mate-worker`). They are a separate, larger trust question, for
  a later RFC. Only Trust inheritance covers them today.
- `session_broadcast`, `session_stop`, `session_retry`, `session_continue`.
  They stay withheld from the conductor; each can earn its own argument later.
- Running `accept_eval.py` / `patrol_budget.py` without a shell. Separate
  change.
- Remote (mesh) children and the claude backend. The claude backend is already
  excluded from the own-worker exemption because its permission requests do not
  say which session sent them (the `_claude_backend` guard in
  `_decide_permission_request`); that stays.
- Any change to the server-side reach of the conductor, the
  `agent.session_control` switch, or the member grant set.

## 5. Design

### 5.1 Where the decision is made

At approval time, in the gateway's permission decider, by widening the
existing own-worker exemption. Not as an `allowedTools` grant in
`_CONDUCTOR_DASHBOARD_GRANTS`, which is how Bots get theirs. The difference is
the server fence: a Bot is always creator-fenced by `authorize_target`, so a
name grant is safe for it. The conductor is fenced only while
`agent.session_control` is off; with it on, a name grant would auto-approve a
send to any same-workspace session. The approval-time check reads the verified
target and approves only the caller's own children, so the conductor ends up
with the Bot's reach for auto-approved calls without narrowing what a person
can still approve by hand.

### 5.2 The rule

A permission request is auto-approved when all of these hold, and prompts
otherwise:

1. The calling slot's agent is `kirocrew-conductor` or `kirocrew-team-lead`.
   Other conductor agents (`kirocrew-ledger-conductor`, pipeline, security,
   folder) keep prompting until named.
2. The tool is `session_send` or `session_close` on `kirocrew-dashboard`.
3. The request carries a verified `kirocrew-dashboard` identity and verified
   raw arguments, and is not a subagent's call (the same checks
   `_crew_root_own_worker_call` makes).
4. The `target` argument is the exact key (not a title) of a live session
   other than the caller, and `_vouched_creator_of(target)` equals the caller's
   key. This is the `created_by` fence Bots are held to
   (`_caller_is_ownership_fenced`), read from gateway-vouched lineage only: the
   settled crew-log tree, or the birth stamp only when `_lineage_minted` says
   this process stamped it. A `created_by` read back from transcript metadata
   never counts (`_vouched_creator_of`'s docstring).
5. `agent.conductor_own_child_verbs` is true (section 5.4).
6. The deny-rule card and PreToolUse hooks still run first, as they do for
   every auto-approve rung (the `TOOL_DENY` card and `_reject_hook_blocked`
   in `dashboard/chat_turn/tool_approval.py`).

The rule does not depend on `crew_mode.APPROVAL_ROUTING_LIVE`. That flag gates
the read exemption for Crew Mode roots; this exemption is keyed on the caller's
agent instead, so it works for a Bot conductor in an ordinary tab and for the
Ultra Mode team lead.

### 5.3 What the child does with the text

Nothing changes on the child side. The text becomes the child's user-role turn
under the child's own grants and the child's own approval mode. A child born
from a Normal conductor is Normal and prompts for its own shell, file writes
and pushes. The exemption skips one person-read of the text, not any check the
child makes.

### 5.4 Rollout default

Default on, with `agent.conductor_own_child_verbs` (boolean, default `true`)
to turn it off. Reasoning: the exemption only covers the caller's own
children, Bots already run this way, the loop it unblocks is the documented
job of both agents, and the alternative people use today (Trust) is wider. An
operator who wants a person to read every send sets it to `false` and gets
today's behaviour. The key is a new config key, so the code PR adds it to the
config schema, the settings page and `config/defaults.json` together.

Every exemption decision writes the same auto-approved audit row the existing
rungs write, with `metadata.reason = "conductor_own_child"`, so a reviewer can
list after the fact what was sent without a prompt.

### 5.5 A nested conductor's own `work_report`

Add `@kirocrew-work/work_report` to `_LEDGER_CONDUCTOR_WORK_GRANTS`, and the
same ref to the goal conductor's KAS permission rule in
`agent_materialization/conductor_agents.py`. One tuple entry; the team lead
picks it up through the shared tuple. The pipeline and security conductors
ship only `_CONDUCTOR_BASE_WORK_GRANTS` and keep it gated.

This is a plain name grant, not an approval-time check like section 5.2,
because the server already fences it: `work_report` has no `item_id`
parameter and reaches only the item bound to the caller's own session
([rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md), Security
considerations), and a root conductor with no binding gets `not_bound`. It
cannot write `verdict`, `state` or `acceptance`. It is not covered by
`agent.conductor_own_child_verbs`: the grant is the worker's grant, already on
by default for every worker.

This amends the work-ledger RFC's Agent spec changes section and its Phase 2.5
exit criterion in this document, so that accepted document is not edited.
When this lands, the code PR updates the `_CONDUCTOR_BASE_WORK_GRANTS` comment
that says `work_report` stays gated.

The follow-up code is ready as a patch outside the repository
(`a11-work-report-grant.patch`, held by the owner's session): it adds the ref
to `_LEDGER_CONDUCTOR_WORK_GRANTS` and `_HISTORY_WORK_VERBS`, rewrites the two
gating comments, and flips the assertions in `test/test_conductor_agent.py`,
`test/test_conductor_base_work_ledger.py` and `test/test_worker_agent.py`.
It ships with, or right after, the section 5.2 code PR.

## 6. Threat model and security considerations

**The security trade-off below is the repository owner's and the security
reviewer's decision.**

### 6.1 The threat

A conductor reads untrusted text: issue bodies, PR descriptions, review
comments, CI logs, web pages. Prompt injection in that text can steer what the
conductor writes. Today the approval prompt on `session_send` is the point
where a person reads that text before it runs as a worker's user-role turn.
With this RFC, an injected instruction can reach a child's turn with nobody
reading it. This is the same exposure a Bot has today.

### 6.2 What still stops it

| Layer | What it bounds | Where |
|---|---|---|
| approval-time target fence | the exemption reaches only sessions the caller created, by vouched lineage; any other target prompts | `_vouched_creator_of`, proposed widening of `_crew_root_own_worker_call` |
| server-side target authorization | `authorize_target` still refuses cross-workspace, channel-mirrored, self and out-of-reach targets on every call | `authorize_target` |
| governance deny rules | a deny rule goes to a card in every mode | `TOOL_DENY` branch of `_decide_permission_request` |
| PreToolUse hooks | an operator hook can block the call | `_reject_hook_blocked` |
| the child's own approval mode | the child's shell, writes and pushes prompt unless the person gave the parent Trust | `inherit_posture` |
| `work_dispatch` still prompts | the one-call create-and-seed path keeps its approval | section 4 |
| close is archival | a closed session is reopenable from History, never deleted | `_MEMBER_DASHBOARD_GRANTS` comment |

### 6.3 What it does not stop

- An injected message that asks the worker to do something its own grants
  allow without a prompt (for example, read files or post a comment with a
  granted tool). This is the residual risk the owner and security reviewer are
  asked to accept. Bots carry it today.
- A worker the person put on Trust by hand, or a child of a Trust parent. That
  is today's Trust risk, unchanged by this RFC.

### 6.4 Why this is narrower than Trust

| | Trust | This RFC |
|---|---|---|
| tools | every tool | two verbs |
| targets | anything the conductor can reach | only its own children |
| child posture | child inherits Trust | child keeps its own mode |
| restart | lost | kept while lineage is vouched; else prompts |
| who | any agent in the chat | two named agents |
| off switch | pick Normal per chat | one config key |

### 6.5 The nested `work_report`

The gate exists because the report crosses a dispatch relationship: the child
writes into its parent's record. An injected nested conductor can therefore
write a misleading `summary`, `artifacts` or `status` on its own item. That is
the exposure every worker has today, bounded the same way: one item, no
conductor-owned field, capped sizes, and `done` is a claim the parent's
evaluator checks. The text is a JSON field, never the parent's prompt
(rfc-conductor-work-ledger.md, "A worker's text becomes the conductor's
prompt").

## 7. Alternatives considered

- **Keep Trust as the answer.** Zero code. Rejected as the default path: it
  opens every tool to the whole child tree, has no target fence, and is lost on
  restart (section 2.3). It stays available for people who want it.
- **Content checks on sends.** Scan the text for injection markers or
  known-bad instructions before auto-approving. Rejected: there is no reliable
  classifier for prompt injection, a check gives false confidence, and it adds
  a model call or a pattern list to every send. The fence plus the child's own
  approval mode bound the damage without judging the text.
- **Copy the Bot's `allowedTools` grant and always fence the conductor
  server-side.** Rejected: it narrows what a person can do through their own
  conductor with `agent.session_control` on, which is a product change outside
  this goal.
- **Status quo.** Unattended conductors keep stalling on every answer and
  close. Rejected as the default; it is what
  `agent.conductor_own_child_verbs = false` gives.

## 8. Migration plan

One code PR, after this RFC is accepted.

Exit criteria (tests in that PR):

1. Normal mode, `kirocrew-conductor` caller, `session_send` to a child it
   created (vouched lineage): approved with no card, audit row
   `reason=conductor_own_child`.
2. Same, `kirocrew-team-lead` caller: approved.
3. `session_send` to a session the caller did not create (a person's tab, a
   sibling conductor's worker): card shown.
4. `session_send` by title instead of key: card shown.
5. Child whose `created_by` comes only from saved transcript metadata (not
   minted this process, lineage not settled): card shown. Same child with
   settled crew-log lineage after restart: approved.
6. `session_close` to own child: approved; to another session: card.
7. `work_dispatch` from either agent: card, unchanged.
8. Caller agent `kirocrew-ledger-conductor` or plain `kirocrew`: card for both
   verbs.
9. A governance deny rule matching the call: deny card, not auto-approved.
   A PreToolUse hook blocking the call: blocked.
10. Subagent call, unverified identity, or unverified arguments: card.
11. claude backend: card.
12. `agent.conductor_own_child_verbs = false`: card for every case above.
13. The child's own approval mode is unchanged by an auto-approved send (a
    Normal child still prompts for its own `execute_bash`).

Exit criteria for the `work_report` grant (section 5.5):

14. `kirocrew-conductor`, its alias and `kirocrew-team-lead` carry
    `@kirocrew-work/work_report` in `allowedTools` and in the KAS rule.
15. The pipeline and security conductors do not.
16. No conductor spec grants the whole `@kirocrew-work` server or a
    `kirocrew-work/*` wildcard.

The same PR updates the withholding comment in `agent_parts/spec_grants.py`,
the `_CREW_ROOT_OWN_WORKER_VERBS` comment in `dashboard/chat_turn/tool_approval.py`,
the goal-conductor and team-lead skills where they say a send prompts, and
flips this document to `implemented`.

## 9. Backward compatibility

No persisted format changes. The new config key defaults on; an existing
config without it reads as `true`. Setting it to `false` restores today's
behaviour exactly. A `git revert` of the code PR fully undoes it.

## 10. Open questions

- Q1. Default on or default off? This RFC proposes on (section 5.4). Owner and
  security reviewer decide.
- Q2. The first send into a fresh child. `session_create` is already granted
  to the conductor, so with this RFC the four-step dispatch fallback
  (`session_create`, then `session_send` of the seed) runs without a prompt
  even while `work_dispatch` still prompts. This RFC proposes accepting that:
  a seed is the same kind of text as a later answer, and reading only the
  first message does not bound injection in the rest. The alternative is to
  exempt only sends into a child that has already had a turn. Owner and
  security reviewer decide, alongside rfc-work-dispatch.md Q1.
- Q3. Should `kirocrew-ledger-conductor` and the other conductor agents join
  later? Not proposed here.
- Q4. Should `session_stop` and `session_retry` follow under the same fence, as
  Bots have them? Not proposed here.
