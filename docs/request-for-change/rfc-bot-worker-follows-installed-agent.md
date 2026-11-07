---
title: A Bot's worker follows an installed agent live
status: draft
author: leozhad
created: 2026-11-06
last-audited: 2026-11-06
audited-at: f02c887f9a
doc-pr:
implementation-prs: []
tracking-issues: [31219]
supersedes: []
superseded-by: []
---

# RFC: A Bot's worker follows an installed agent live

- Status: draft. Nothing built; it changes no code. Acceptance needs a
  maintainer, because it asks to change part of a recorded decision (§7). It
  amends §3, §9 and §9a of
  [rfc-mate-owned-agent-config.md](rfc-mate-owned-agent-config.md) in this
  document, so that accepted document is not edited. Claims about main were
  checked at `f02c887f9a`.
- Author: leozhad
- Tracks: [#31219](https://github.com/kirodotdev/KiroCrew/issues/31219).
  Agents with more than 20 hooks or bare sub-agent and skill allows also need
  [#31241](https://github.com/kirodotdev/KiroCrew/pull/31241).

## 1. Summary

A Bot may name one installed user-level agent as its **base agent**. Each time
the Bot's worker starts, Crew reads that agent's current spec and layers it
between the worker base and the Bot's own file. An update or reinstall of the
agent reaches the Bot's next worker with no step by the owner. Behaviour
directions stack: the worker base prompt, then the base agent's current prompt,
then the Bot's own directions. The conductor, the memory store, the workspace
and the governance ceiling do not change.

## 2. Motivation

### 2.1 Today

- A Bot's worker spec is the worker base plus the Bot's file, rebuilt at each
  worker start (`mate_worker.resolve_worker_spec`, through
  `worker_additions` and `merge_worker_spec`).
- Add abilities copies an agent into the Bot's file once
  (`mate_worker_kit._plan`). The user docs say so: "Later edits to that agent
  do not reach the Bot" (`src/kiro_crew/docs/crew-members.md`, "Its worker").
- A plain chat session on the same agent reads `~/.kiro/agents/<agent>.json`
  at each start (`agent_sdk.agent_specs.load_agent_spec` ->
  `acp.kas_agents.load_agent_spec`), so it gets every update.
- A Bot create naming any other template is refused
  (`mate_config.start_template_refusal`).

### 2.2 Problems

Some agents are maintained software, not a starting point. An agent installed
and updated by an external package manager is rewritten in place on each
update: its hooks, resources and server commands point at the new version's
install directory, and servers come and go.

1. **The copy goes stale.** After an update, the Bot's copy still names the old
   version's scripts and servers. When the package manager removes the old
   directory, those hooks and skills break.
2. **Adding it again does not repair it.** The add is additive. A server whose
   command changed keeps the old command ("already present: leave it" in
   `mate_worker_kit._plan`). A replaced resource path stays beside the new
   one. A hook whose command moved is carried a second time beside the old one
   (`mate_spec_policy._layer_hooks` dedupes whole entries), so each update
   grows the hook count toward `spec_hooks._MAX_SPEC_HOOKS`.
3. **Directions are one slot.** Adding an agent with a prompt replaces the
   Bot's own directions (the one `prompt` slot `mate_worker_kit._plan`
   fills), so a Bot cannot keep its own text beside the agent's.
4. **Every update is a manual step**, and nothing tells the owner one is due.

## 3. Goals and non-goals

Goals:

- Opt-in per Bot: at most one base agent, set only by the owner.
- Each worker start reads the base agent's current spec. Nothing is copied.
- Stacked directions with no stale copy of the agent's prompt.
- Nothing from the base agent lifts the worker past POLICY ∩ PROFILE, or
  changes the Bot's memory store, workspace, model or conductor.
- Fail closed when the base agent cannot be read.

Non-goals:

- The conductor. It keeps the stock spec, and nothing from the base agent
  reaches it (the conductor half of the 2026-11-04 decision stands).
- More than one base agent per Bot.
- Changing Add abilities, which stays copy-once, or adding a free-text editor
  for the Bot's own directions.
- Watching the agent file and reloading a running worker. A running worker
  keeps its spec until it ends, as a plain chat session does.
- The hook cap and built-in allow carrying (#31241).

## 4. Design

### 4.1 The field

`base_agent: str = ""` on the Bot's binding (`agents.<name>` in
`config.json`, `KiroCrewAgentConfig`). A valid value names a user-level agent
in `kiro_agents_dir()` that is not a mate service spec
(`agent_files.is_mate_service_agent`), not a Bot file in `mates/agents/`, and
not another crew's private fork (`private_to` in the agent sidecar). Empty
means today's behaviour.

Writers, owner-only like the other binding fields: `PUT /api/agents/<name>`
(`dashboard/agent_admin/crew_update.py`), `kirocrew agent update --base-agent
<agent>` (`""` clears it), and the crew editor (§4.5). The Bot's own
`kirocrew-mate` tools do not offer it, and `config.json` is a READONLY sandbox
leaf, so a worker cannot set its own base agent.

### 4.2 Layering at worker start

When the binding names a base agent, `resolve_worker_spec` reads it with
`agent_specs.load_agent_spec` and layers three inputs:

```
worker base (source)  >  base agent (read now)  >  Bot file (owner additions)
```

- The base agent's additions come from `worker_additions(base, agent_spec)`,
  the function the Bot file already goes through; the Bot file's additions are
  then taken against the worker base plus the base agent.
- **Transports:** the earlier layer wins a server name, as "a base server
  always wins" does today. A stale copy in the Bot's file of a server the base
  agent declares is therefore ignored, and the agent's current command runs.
- **Restrictions join** from every layer: `excludedTools`, per-server
  `disabledTools`, switched-off servers and the MCP page's switch-offs
  (`globally_disabled_refs`). A layer never removes another layer's limit.
- **Hooks, permissions, `toolsSettings`** from the base agent and the file both
  go through `mate_spec_policy.layer_policy`: exact or refused, KAS only.
- **Then, unchanged:** the owner's revoked approvals
  (`mate_approval_revocations.strip`) and the ceiling
  (`sanitize_agent_config_governance`) on the merged spec; the KAS projection
  applies the ceiling again.
- The base agent's `model` and `name` are ignored, as the file's are.

### 4.3 Stacked directions

The worker prompt is the worker base prompt, then the base agent's directions
(its prompt less the worker base text, `mate_worker_kit._directions_of`), then
the Bot's own slot. Setting a base agent never writes the slot. When the slot
equals the base agent's current directions (an earlier Add of the same agent),
the set preview offers to clear it, so the worker does not read it twice.

### 4.4 Failure and change notice

- Base agent missing, unreadable or invalid under §4.1: the worker is refused
  with `WorkerKitUnreadable` naming the agent, as an unreadable Bot file is
  today. Failing open is rejected: running without the agent also drops its
  deny rules and guard hooks.
- Each worker start records a digest of the base agent's spec. When it differs
  from the last start's, the Bot's chat gets one notice naming what changed
  (servers, hooks, permission rules, skills, directions). It informs; it does
  not gate (open question 1).

### 4.5 Surfaces

- Crew editor, **Abilities**: a **Base agent** row with a picker and **Clear**.
  Its preview lists what the agent brings now and which of the file's entries
  it supersedes. **Comes with** shows the base agent's items under its name.
- `GET /api/agents` returns `base_agent`; `kirocrew agent list` shows it.

## 5. What this amends

- **rfc-mate-owned-agent-config §3**, "No live link back to a source: the file
  is a stamp, not a parent." Read as: the file stays a stamp. A Bot may also
  name a base agent, which is read live and never copied or written.
- **§9**, "Shared pointer + per-crewmate overlay", was rejected for three
  reasons. "Two sources of truth": each fact keeps one home, the agent's
  definition in its own spec and the owner's additions in the Bot file. "New
  merge/precedence machinery": the merge exists (`worker_additions`,
  `merge_worker_spec`); the base agent is one more input to it. "Stale until
  rebuild": the worker spec is rebuilt at every worker start. The trade-off §9
  names still holds, by the owner's choice per Bot: that Bot is no longer fully
  standalone.
- **§9a.** The fork, lineage and drift lifecycle is still not used. It defends
  a copy that follows a parent; nothing here is copied, so nothing can drift.
  There is no `forked_from` or `private_to`, no review cycle and no parent-drift
  guard, and the Bot file is still edited in place.

Why "stamp, not a parent" falls short: it assumes the source is a template the
Bot outgrows. For an agent that updates itself, the stamp freezes the paths and
commands the next update moves, and the owner must notice and redo the copy. A
plain chat session on the same agent never has this problem.

## 6. Alternatives considered

| Option | Keeps | Costs |
|---|---|---|
| Copy once, add again after each update (today) | Bot fully standalone | Stale commands and paths stay (§2.2 item 2); hooks pile up; directions replaced; a manual step per update |
| A re-sync job that re-copies on change | The Bot file stays the only input | Needs per-entry provenance to tell copies from the owner's edits, the lineage §9a rejects; writes the owner's file with no card; races running workers |
| A replace-mode "Add again" button | No background writer | The same provenance problem; still manual |
| Live base agent plus the Bot file (this RFC) | Updates land as in a plain chat; one home per fact | Bot not fully standalone; an update can change hooks or grants with no preview (open question 1) |
| Bind the Bot's `kiro_agent` back to the agent | Simplest | Reverses the conductor and worker split and puts user servers on the conductor, which the 2026-11-04 decision refuses |

## 7. Decision this needs

`docs/decisions/2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md`
says a change that "binds Ultra Mode or mates back to custom agents needs an
explicit owner decision", and that "A mate copies a custom agent's MCP servers,
skills, tools and prompt once, as its starting point." This RFC keeps the
conductor half and asks to change the second sentence for one case: an owner
may opt a Bot's worker into following one installed agent. If a maintainer
accepts it, the maintainer records a superseding entry; this RFC adds none.

## 8. Migration plan

**Phase 1, backend** (one PR). The field and its validation, the layering, the
fail-closed refusal, the change notice, the API and CLI, and
`docs/system-specs/modules/crew-mode.md` plus `crew-members.md`. Exit criteria:

1. With `base_agent` empty, every worker spec is byte-identical to main's.
2. A test rewrites the base agent's spec between two worker starts; the second
   spec carries the new server command and hook paths, and the Bot file is not
   written.
3. An unreadable base agent refuses the worker.
4. A base agent grant the ceiling withholds is absent from the merged spec.
5. The conductor spec is identical with and without a base agent.

**Phase 2, crew editor** (one PR, after Phase 1). The Base agent row, its
preview and the clear-duplicate-directions offer. Exit: a browser test sets and
clears a base agent and sees the preview rows.

## 9. Backward compatibility

Compatible: a new optional field that defaults to empty, no stored file is
migrated, and Add abilities is unchanged.

## 10. Security considerations

- Same trust as running the agent directly: the owner already lets the package
  manager rewrite that spec, and a plain chat session on it runs each update
  unseen.
- The difference from Add: an update can add a hook, an allow rule or a server
  without a preview. Bounded by the owner's opt-in, the ceiling on the merged
  spec, the owner's revocations, KAS-only policy carrying, the hook cap and the
  change notice.
- Out of the Bot's reach: the field lives in the READONLY `config.json`, and no
  Bot tool sets it.
- The memory store, workspace, sandbox profile and approval mode come from the
  binding and the session, never from the spec.

## 11. Open questions

1. Should an update that adds a hook, an allow rule or a server land at the
   next worker start, or hold the worker until the owner presses a card? Open
   for maintainers.

   **The author's position:** it lands on its own at the next worker start,
   the same way a plain chat session on that agent picks it up today. It
   still runs under the governance ceiling on the merged spec and the owner's
   revoked approvals, and the change notice (§4.4) names what arrived. A card
   would bring back the manual step on every update that this RFC exists to
   remove, for an agent the owner already lets the package manager rewrite.
2. Should the change notice go to the Bot's chat, the notification bell, or
   both?
3. Should setting a base agent also offer to remove Bot-file items an earlier
   Add copied from the same agent, beyond the directions in §4.3?
