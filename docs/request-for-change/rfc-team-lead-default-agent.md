---
title: Ship the team lead as a default agent built like kirocrew-research, not as a crewmate template
status: in-progress
author: Raymond Chen, with kirocrew-worker
created: 2026-10-10
last-audited: 2026-10-10
audited-at: 7612507e11
doc-pr: null
implementation-prs: [18128]
tracking-issues: [18049, 18813]
supersedes: []
superseded-by: []
---

# RFC: The team lead as a default agent

- Status: in-progress. The decision was made by the product owner on 2026-10-10:
  "this shall be a normal custom agent, not a crewmate for now", and "I do need
  it to be found by default; look at the other default-agent mechanism." The
  document and the implementation land together in
  [#18128](https://github.com/kirodotdev/KiroCrew/pull/18128).

## Problem

A goal handed to a person does not decompose itself. The conductor agents in the
tree run a fleet but cannot do an item themselves, and the worker does one item
but does not run anyone. Nothing ships that does both: splits a goal into
work-ledger items, keeps the few small focused ones, dispatches a session for
each of the rest, and patrols that fleet.

The missing piece is a prompt, not a mechanism. Every verb such a lead needs --
`work_ledger_record`, `session_create`, `monitor_start`, `resource_status`,
`work_ledger_read` -- already exists and is already in the default agent's
surface. What is missing is an agent that is told how to use them as a lead, and
a set of rules for the calls a lead makes between items.

## Approach

Ship it the way the other shipped service agents ship. `_install_research_agent`
in `agent_materialization/service_agents.py` is about fifteen lines: it derives
from `build_agent_config()`, swaps in a name, a description and a prompt, drops
the platform guide server, and writes the file. The install sequence calls it on
every boot and the filename sits in `OWNED_KIRO_AGENT_FILES`.

`kirocrew-team-lead` is the same five things: one installer beside research, one
call in the same sequence, one filename in the owned list, one prompt constant
beside `_RESEARCH_SYSTEM_PROMPT`, and the two facade entries every moved name in
`agent.py` carries.

Deriving from `build_agent_config()` is the load-bearing part. That function has
already filtered its tool list and its MCP servers against the governance
ceiling, so the lead inherits whatever the ceiling permits and nothing else. This
change adds no grant, no server and no `autoApprove` of its own, which is why
there is no second surface for a ceiling change to re-filter.

### Overwritten on every boot, and that is the whole design

The spec is rewritten each boot, exactly as `kirocrew-research` and
`kirocrew-knowledge` are. **An operator who wants a lead with different rules
copies it to a different agent name.** A hand-edit at this name is replaced on
the next boot.

That one line removes a question rather than answering it. An installer that had
to detect and respect a hand-edit needs to decide whether the file on disk is
its own write, which needs a recorded digest, which needs somewhere to record it,
which needs a three-way install outcome, which needs a start-time gate to refuse
a spec whose authorship it cannot establish -- and each of those guards needs to
cover what the next one assumes. None of it is needed here, because nothing asks
whether the file is ours.

## Alternatives considered

**A crewmate template with hand-edit detection.** Rejected on the owner's ruling
above, and the implementation history is the argument: the apparatus required to
respect a hand-edit at a shipped name reached roughly 1400 lines of installer and
runtime code and 3300 lines of tests, and produced fifteen review findings, every
one of them a consequence of the same question. Overwriting deletes the question.

**A Markdown agent.** Considered first ("I thought it was just an agent
markdown"), and it cannot derive from `build_agent_config()`. A static document
cannot inherit the governance ceiling, so its tool list would be a second copy
that drifts from the ceiling the moment either changes.

## What ships

1. `_install_team_lead_agent()` in `service_agents.py`, its call in the install
   sequence, `TEAM_LEAD_AGENT_FILENAME` in the owned list, the
   `_TEAM_LEAD_SYSTEM_PROMPT` charter, and the two `agent.py` facade entries.
2. `builtin_skills/team-lead/SKILL.md` -- the lead's procedure, and the five
   team-management rules from [#18813](https://github.com/kirodotdev/KiroCrew/issues/18813).
3. This document.
4. Tests mirroring the ones `kirocrew-research` already has.

### The five team-management rules

The skill's procedure covers running an ITEM. The calls a lead makes about the
TEAM had no answer anywhere in the tree, and each one has a mechanism that could
decide it, so the skill names the mechanism rather than asking for judgement:

| the call | the mechanism that decides it |
|---|---|
| how big a wave may be | `resource_status` before every wave and every reseed wave; queue on `tight`, `critical` or `unknown`; give capacity back with `action=close` plus `session_close`; the ceiling is the server's `MAX_SLOTS_PER_CREATOR` and `MAX_LIVE_SLOTS`, never a count the lead holds |
| what stops a third conducting level | `MAX_DEPTH` in `work_ledger.py` is what refuses; the guard admits a depth that does not yet agree with it ([#18127](https://github.com/kirodotdev/KiroCrew/issues/18127)), so the safe tree is the one already stated and the cap is never asked to be raised |
| whether two lines merge | three readings -- same files, one line down to a single item, handoffs bouncing; merge by `action=close` plus a reseed carrying the artifacts; `session_adopt` is named as what this is NOT, since it moves sessions, sits outside the auto-approved set and is the owner's call |
| whether to add a tracker | a concrete trigger -- the `compact` ledger read comes back cut, items span more than one ledger, or the lead skips items -- answered by ONE worker that writes one summary item and reports, never decides |
| who may write a shared file | one owner per shared file, one integrator per output, and review dispatched to a different child than the author |

A posture of `unknown` is the one worth naming separately: it is the reading that
FAILED, and it takes the same lighter path as `tight`. A probe that could not
answer is exactly where a lead would otherwise decide from feel.

## Backwards compatibility

Compatible. One agent spec appears under `~/.kiro/agents/` where none was before,
and one filename joins `OWNED_KIRO_AGENT_FILES` so the boot-time self-heal sweep
covers it. No field, key, type, validator or tool signature changes.

One behaviour is worth stating plainly because it is a contract and not an
accident: a file at `kirocrew-team-lead.json` is overwritten on every boot. That
name is a shipped name, the same as `kirocrew-research.json`, and a customized
copy belongs under another name.

## Open questions

None. The design mirrors a shipped pattern; a question about whether a hand-edit
at a shipped name should be respected is a question about every default agent
rather than about this one.
