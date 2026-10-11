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
but does not run anyone. What ships here closes that gap: a default agent that
owns a goal, does the small focused items with its own hands, and dispatches and
patrols a session for every other one.

**The dispatch surface is mounted, not inherited.** Running a goal as a team needs
`session_create`, `chat_folder_*` and `work_ledger_*`. Those live on
`kirocrew-dashboard` and `kirocrew-work`, which are `opt_in` servers that
`build_agent_config()` skips. A spec that only names them in `tools` mounts
nothing: kiro-cli reports the server as declared but not configured. So the
installer builds both entries with `managed_mcp._managed_opt_in_entry`, the call
the dashboard-manager and worker installers already make. That helper carries the
two fields that fail silently when dropped: `"type": "registry"` and the
`KIROCREW_HOME` pin. The verbs auto-approved on them are the goal conductor's own
tuples, `_CONDUCTOR_DASHBOARD_GRANTS` and `_LEDGER_CONDUCTOR_WORK_GRANTS`, and the
whole `allowedTools` list goes back through the governance ceiling before it is
written. `kirocrew-panel` is not mounted.

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
ceiling, so the lead inherits whatever the ceiling permits. The only additions are
the two dispatch servers above, and their grants pass through the same ceiling
filter, so a ceiling change re-filters them on the next rebuild like every other
grant. No `autoApprove` map is written.

### Overwritten on every boot, and that is the whole design

The spec is rewritten each boot, exactly as `kirocrew-research` and
`kirocrew-knowledge` are. **An operator who wants a lead with different rules
copies it to a different agent name.** A spec this installer wrote on an earlier
boot is replaced in place on the next one.

Because this stem is newly reserved, a one-time migration guards the single case
the long-reserved names cannot hit: on install, a pre-existing file at this path
that this installer did not write (recognised by the `name` field it always sets)
is moved aside once to a timestamped `.bak` and logged, then the managed spec is
written — a migration, not ongoing ownership tracking, with no digest, start gate
or renewal table.

That one line removes a question rather than answering it. An installer that had
to detect and respect a hand-edit needs to decide whether the file on disk is
its own write, which needs a recorded digest, which needs somewhere to record it,
which needs a three-way install outcome, which needs a start-time gate to refuse
a spec whose authorship it cannot establish -- and each of those guards needs to
cover what the next one assumes. None of it is needed here, because nothing asks
whether the file is ours: it is simply re-derived.

This stem is newly reserved, so a first boot can overwrite a file an operator
happened to author under this name. That is the same accepted behaviour
`kirocrew-research` and `kirocrew-knowledge` already carry, and the reason the
one-line "copy it to another name" contract exists: a customized lead belongs
under a name the install sequence does not own.

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
2. `builtin_skills/team-lead/SKILL.md` -- the delta over `goal-conductor`: echo
   the ask, register the work, the do-it-yourself test, dispatch one level deep,
   and how to run the team. `goal-conductor` stays the dispatch and patrol
   procedure.
3. This document.
4. Tests mirroring the ones `kirocrew-research` already has.

## Backwards compatibility

Compatible. One agent spec appears under `~/.kiro/agents/` where none was before,
and one filename joins `OWNED_KIRO_AGENT_FILES` so the boot-time self-heal sweep
covers it. No field, key, type, validator or tool signature changes.

One behaviour is worth stating plainly because it is a contract and not an
accident: a spec this installer wrote at `kirocrew-team-lead.json` is overwritten
on every boot. That name is a shipped name, the same as `kirocrew-research.json`,
and a customized copy belongs under another name. Because this stem is newly
reserved, the one difference from the older names is the one-time migration above:
a pre-existing file at this path that this installer did not write is moved aside
to a timestamped `.bak` on the first install rather than destroyed.

## Open questions

None. The design mirrors a shipped pattern; a question about whether a hand-edit
at a shipped name should be respected is a question about every default agent
rather than about this one.
