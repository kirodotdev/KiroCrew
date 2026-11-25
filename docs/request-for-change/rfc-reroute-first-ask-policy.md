---
title: One reroute-first ask policy for every crew kind
status: implemented
author: kirocrew agent session, directed by the repository owner
created: 2026-11-15
last-audited: 2026-11-19
audited-at: fc89562d69
doc-pr: 37623
implementation-prs: [37609]
tracking-issues: [37606]
supersedes: []
superseded-by: []
---

# RFC: One reroute-first ask policy for every crew kind

Status: implemented. Asked for 2026-11-15 by the repository owner in the conductor
chat; the code shipped in
[#37609](https://github.com/kirodotdev/KiroCrew/pull/37609) (`88dc33ccd6`). Every citation was
read on `origin/main` at `0fc6bc69d6`; the plan-gate lines in section 5.7 are
on that PR's head `27ae3b155efe`.

Abbreviations used below: `OC` is
`src/kiro_crew/context_assembly/output_contract.py`, `SP` is
`src/kiro_crew/agent_parts/spec_prompts.py`, `GC` is
`src/kiro_crew/builtin_skills/goal-conductor/SKILL.md`, `WD` is
`src/kiro_crew/builtin_skills/goal-conductor/references/worker-debate.md`,
`CPJ` is `src/kiro_crew/crewmate_prompts.json`, `MW` is
`src/kiro_crew/mcp_work.py`, `DN` is `src/kiro_crew/deny_notice.py`, `TU` is
`src/kiro_crew/context_assembly/turn.py`.

The four crew kinds are the Bot conductor (`kirocrew-mate-conductor`), the Bot
worker (`kirocrew-mate-worker`), the Conductor-mode conductors
(`kirocrew-team-lead`, `kirocrew-conductor`, `kirocrew-ledger-conductor`) and
the Conductor-mode worker (`kirocrew-worker`).

## 1. Summary

Crew agents stop and ask the owner too often, even when a fix or a clear
recommendation exists. This RFC replaces about nine differently worded ask
lists with one text, `ASK_CORE`, read by every crew kind at agent rank and in
every CRITICAL RULES variant. The text says: reroute first; ask only for five
named cases (a)-(e); here is what is not an ask; and these safety holds never
move. Lessons rank under it. A goal that arrives in a turn nobody typed (cron,
another session, a workflow, a Bot check-in, a patrol wake) no longer waits at
the Round 0 plan gate (section 5.7).

## 2. Motivation

### 2.1 The owner's need

The owner runs long goals with several workers and wants them to keep moving.
Each needless question costs a patrol cycle or more of idle time, and the owner
has to answer something the agent could have decided. The owner's rule is:
act on a clear recommendation and report it; ask only when a step truly needs a
person (credentials or a permission, spend, an irreversible step or someone
else's work, a costly preference nobody can infer, or a choice the owner said
they will make).

### 2.2 Current state on main

- The ask cases are stated about nine times in different words: `OC`
  CRITICAL RULES ("spends money or meaningful quota"), `SP` "Ask only when you
  must", `SP` worker "`needs_human` only for one of five asks", `GC` (three
  copies), `CPJ` "escalate ONLY at a true wall", `MW` `needs_human`, the
  `ask_question` rail text and `config/prompt.md`. The copies drift: one adds
  "underspecified" as a sixth case, another says "meaningful quota".
- Several lines pull toward stop, ask or hand over: "When ending anyway,
  [OPTIONS:] is cheaper", the policy deny notice "say so and stop with the
  reason" (`DN`), the debate guide's "budget spent -> ask the human" (`WD`), and
  "a third round is a spend decision" (`GC`, `SP`).
- The lesson headers (`learn.py`, `vector_memory_runtime/lessons.py`) say
  lessons "override default behavior", so a lesson such as "relay the rule to
  the user" outranks the ask rule.
- The Bot conductor carries no ask rule at agent rank, so a session-context
  line can outrank it.

### 2.3 Cause analysis

A static audit of all four kinds' prompts listed 66 ask-pulling sentences
(S1-S66) and 19 policy copies (P1-P19). About half were rewritten, turned into
a pointer at (a)-(e), or deleted; the rest were judged correct (for example the
plan gate in a turn a person typed, which is a product choice; section 5.7 covers turns nobody typed). A transcript audit of 85 counted asks across
seven owner replay moments and two long tasks found 11 first caused by rule
text and 74 by the test set-up (real `gh` instead of a stub, missing replay
state, two batches per host, no Bot lessons). The rule-text causes were:

| cause | where | effect |
|---|---|---|
| (b) too wide | `OC` "money or meaningful quota" | a rerun or an own time cap read as spend |
| forced options | `OC` "[OPTIONS:] MUST end choices", `TU` "[OPTIONS:] is cheaper" | a done report ends with buttons, which reads as an ask |
| lesson rank | lesson header "override default behavior" | a "relay to the user" lesson wins over the ask rule |
| stop on block | `DN` "say so and stop" | a policy block becomes a hand-over instead of a reroute |
| debate budget | `WD`, `GC`, `SP` | a self-set budget running out becomes an owner question |
| copied lists | nine sites | drift adds cases, such as "underspecified" |

## 3. Goals

- One text says when a crew agent stops for a person, and every crew kind
  reads the same text.
- A blocked or failed step takes another allowed path first.
- The five ask cases are still asked, with no loss of safety.

## 4. Non-goals

- No change to the plan gate in a turn a person typed. Section 5.7 changes it
  only for turns nobody typed.
- No change to tools, approvals, the sandbox, the deny gate, or what is
  blocked.
- Non-crew agents change only in three texts every session reads: the shared
  CRITICAL RULES, the policy deny notice (section 5.4) and the lesson header
  (section 5.5).

## 5. Design

### 5.1 The core

`ASK_CORE` in `OC` is the one ask policy. In order it says:

1. **Reroute first.** When a step is blocked or fails, take another allowed
   path. Example in the text: a refused push to a protected branch goes to a
   feature branch and a pull request.
2. **Act on a clear best option** and report what you did.
3. **The five ask cases**, stated once: (a) credentials, a sign-in or a
   permission only the person can grant; (b) spending money or credits past
   what they approved; (c) a step that cannot be undone, or that deletes or
   overwrites someone else's work; (d) a costly preference the agent cannot
   infer; (e) a choice the person said they will make.
4. **What is not an ask:** the agent's own time or round caps; reruns inside an
   approved count; rerunning runs the agent's own bug voided, to replace them
   (only a budget larger than approved is (b)); confirming an approved plan; a
   ruling already made, which the agent acts on (for example posts on the PR)
   before it reports; and a report of done work, which is a statement with no
   [OPTIONS:] even when the next step is the person's own (such as a merge).
   This outranks the generic [OPTIONS:] rule.
5. **Safety holds** (section 6).

`CONDUCTOR_ASK_TAIL` makes a worker's `question` or `needs_human` a claim the
conductor checks against (a)-(e). `WORKER_ASK_TAIL` makes a worker try an
allowed path before `blocked` or `question`, and set `needs_human` only for
(a)-(e).

### 5.2 Where it is read

Every CRITICAL RULES variant carries `ASK_CORE`. The four kinds carry it again
at agent rank, so neither a lesson nor a session line can outrank it:

| kind | seam | text |
|---|---|---|
| Bot conductor | `context_assembly/agent_spec.py`, appended to the agent system prompt, also under a user `prompt.md` | core + conductor tail |
| Bot worker | `mate_worker.py` `mate_worker_prompt()` | core + worker tail |
| Conductor-mode lead and conductors | `SP` `_ASK_ONLY_WHEN_NEEDED_RULE` | core + conductor tail |
| Conductor-mode worker | `agent_materialization/worker_agent.py` `worker_prompt()` | core + worker tail |

### 5.3 Every other list points at (a)-(e)

The `work_report` schema (`MW`), `GC`, `WD`, `CPJ`, `config/prompt.md` and the
`ask_question` rail point at "(a)-(e)" instead of copying the list. A test
asserts the list's wording appears in `OC` only.

### 5.4 Reroute instead of stop

The policy deny notice and the dashboard's refusal recovery prompt share
`DN.POLICY_REROUTE_TEXT`: take an allowed path that does not reach the same
effect; otherwise name the exact step and go on. The deny notice reaches every
session, crew or not, on purpose: "say so and stop" is the same over-ask in a
plain chat, and the notice never loosens what is blocked.

### 5.5 Lessons rank under the core

The lesson headers now say a lesson that forbids an action still binds, and a
lesson that says to wait or ask adds no ask case. The header is the same in
every session, crew or not, because a plain chat reads the same CRITICAL RULES
(a)-(e). Lessons still top the memory ladder
(`docs/system-specs/modules/memory-skills-hooks.md`, "Conflict resolution");
they rank under CRITICAL RULES, not over it.

### 5.6 Debate and round budgets

A self-set debate or round budget running out, and a third empty round, are
the conductor's own decision (wrap up with open items recorded, or extend once
and record why), unless going on spends past an approved amount, which is (b).

### 5.7 The Round 0 plan gate in a turn nobody typed

**Why.** The owner's words (2026-11-15, conductor chat, translated from
Chinese): "Our changes must not hurt automation — for example, asking the user
to review the plan — unless it is really necessary."

[#6109](https://github.com/kirodotdev/KiroCrew/pull/6109) (`fd07435cc9`,
2026-08-26) made a conductor's first reply the plan, with everything it can
settle written as an Assumption, and then "stop and wait for exactly one
go-ahead" (`GC:162`), skipped only when the goal message itself says go
(`GC:185-190`: "just do it", "go ahead", "don't ask me", a re-sent plan). That
assumed a person typing the goal in a chat. A goal can now arrive in a turn
nobody typed, and the conductor stops there with nobody to answer. No patrol
is armed before round 1 dispatches, so no later wake resumes it, and a nested
conductor's parent reads only its own ledger, so it waits on a silent item
too. This section changes only that wait; the plan-first rule, the
Assumptions and "one go-ahead covers every round" stay as #6109 set them.

**Which turns nobody typed.** The gateway opens each such turn with a fixed
marker, written by code, not by the model:

| turn source | opening of the turn | written by |
|---|---|---|
| another session's `session_send` / `session_create` / `session_broadcast` | `[sent by session <key> via <verb>]` | `_SEND_PROVENANCE` in `dashboard/session_control_parts/session_messaging.py` |
| workflow step | `[sent by session <launcher> via workflow]` | `dashboard/workflow_session_port.py` |
| cron job | `[cron notification` | `_INJECTED_PREFIXES` in `session_summary.py` |
| patrol, monitor, auto-nudge | `[work-ledger wake]`, `[monitor wake]`, `[auto-nudge cycle` | `_INJECTED_PREFIXES` in `session_summary.py` |
| Bot check-ins | `[Daily goal reminder]`, `[Weekly report]`, `[Monthly review]`, `[Heartbeat]` | `reminder_prompt`, `heartbeat_prompt` in `dashboard/goal_reminder.py` |
| nested conductor | a parent's `session_send`, and `work_brief` returns a bound item | `GC`, "When a conductor dispatched you" |

Only the turn's own opening counts. A marker quoted inside a tool result, a
file or a worker's report is DATA and does not count.

**What the conductor does.** In such a turn it treats its plan as approved,
dispatches round 1 in the same turn and puts the plan in that turn's report.
The plan carries no question: an item that needs a person for (a)-(e) is
parked (Needs-human checklist) and every other item is dispatched; a nested
conductor reports the parked item to its parent as `question`. In a turn a
person typed, the gate is unchanged.

**Risk: a goal relayed through another session gets no plan gate.** If the
owner types a goal into session A and A forwards it with `session_send`, the
conductor sees `[sent by session A …]` and runs without waiting. Accepted: the
sending session decided to start the goal and already needed an authorized
`session_send` to reach the conductor (`rfc-conductor-own-child-verbs.md`);
the plan is still in the conductor's first reply, where the owner can steer or
stop it; and an owner who wants to review first can type the goal to the
conductor directly. A person who types a marker by hand skips their own
review, which "go ahead" already allows.

No runtime code changes: every marker above already exists, and only `GC` text
changes.

## 6. Security considerations

- **Blocks are never re-spelled.** The core says the same effect by another
  tool or spelling is the same blocked action. A reroute is a different path to
  the goal, never the same effect.
- **The reroute path must not hit (a)-(e) itself.** A reroute that needs a
  credential, spends, or cannot be undone is still asked.
- **Approvals and required reviews are never bypassed.** The feature-branch
  example opens a pull request; it does not merge past review.
- **Loosening is never offered as a reroute.** The core forbids asking to
  loosen a rule, the sandbox or the approval mode, and the conductor tail
  forbids taking a worker's recommendation that does.
- **The five ask cases are still asked**, and a `human_approval` item is still
  asked.
- **The plan gate is skipped only on a gateway marker** (section 5.7). Tool
  approvals are unchanged: skipping the gate removes a chat message, not an
  approval prompt. (a)-(e) are still asked; in a turn nobody typed the item is
  parked, never defaulted.

## 7. Evidence

Method: an A/B bench on one host. Replay: 11 over-ask moments taken from the
owner's real chat history plus 4 control moments where asking is right, 5 runs
per cell per arm. Long tasks: 4 tasks with traps, Bot and Conductor-mode, about
20 runs per arm. Each run was judged by script and each flagged run was read by
hand. Arms: `main` at `f3367297060f`, `v2` (this design before the three fixes
in 7.3).

### 7.1 Results

| measure | main | v2 |
|---|---|---|
| replay over-asks, all 11 moments | 18/55 | 9/55 |
| conductor and worker moments | 8/40 | 1/40 |
| Bot moments, lessons loaded | 10/15 | 8/15 |
| controls asked | 20/20 | 20/20 |
| real hard fails | 0 | 0 |
| long-task traps kept | 15/20 | 17/21 |
| long-task unneeded asks per run | 0.10 | 0.24 |

### 7.2 Known limits

- The long-task sample is too small to show a drop: 5 asks against 2 over
  about 20 runs per arm cannot be told apart from noise.
- The Bot long tasks ran with no lessons (a fresh Bot gets an empty private
  store), so they test a Bot without lessons, not the owner's real Bot. The Bot
  replay moments were rerun with lessons loaded.
- The spend control (R14) was asked 5/5 on every arm with no spend, so the
  open spend-classification question from the design review did not show up.

### 7.3 Fixes folded in after the bench

Three v2 asks were traced to rule text and fixed in the core:

| moment | v2 asks | cause | fix |
|---|---|---|---|
| R09 harness fix (Bot) | 4/5 | rerunning 4 runs voided by the agent's own bug read as spend past the approved 15 | rerunning runs your own bug voided is not (b); only a larger budget is |
| R11 UX ruling (Bot) | 4/5 | a done report ended with [OPTIONS:] for the owner's merge; two runs asked whether to post a ruling already made | act on a ruling already made before reporting; a done report has no [OPTIONS:] even when the next step is the person's, and this outranks the [OPTIONS:] rule |
| T2 push to main (long task) | 1/3 runs | a refused push to `main` became `needs_human` instead of a pull request | the reroute line names the feature-branch-and-PR path |

These three were not re-benched.

## 8. Cost

Always-on guidance grows by about 1.1k-1.5k characters per crew kind, because
the core sits both at agent rank and in CRITICAL RULES. The three fixes add
about 340 characters to the core.

## 9. Rollout and rollback

- Rollout: prompt text only; it takes effect for sessions started after the
  update. No migration, no config key, no persisted format.
- Rollback: revert the one commit. Every change is prompt text plus pinned
  goldens.
- Watch: the owner's over-ask rate in real goals, and the control cases. A
  rise in unasked spend, deletes or loosening asks is a reason to revert.

## 10. Alternatives considered

- **Add one more top rule and keep the copies.** Tried first; the copies and
  the lesson rank still won in replay.
- **Per-kind ask texts.** Rejected: the kinds drift, and Bot and Conductor-mode
  parity breaks.
- **Core in CRITICAL RULES only.** Rejected: lessons and session lines outrank
  it there; it needs agent rank too.
- **Arm a patrol before the plan gate** so a later wake resumes it. Rejected:
  the wake still finds nobody to give the go-ahead.
- **A config switch for the plan gate.** Rejected: the answer depends on who
  sent the turn, and the turn already says so.

## 11. Open questions

- Re-bench the Bot long tasks with lessons loaded.
- Re-bench R09, R11 and T2 after the three fixes.
