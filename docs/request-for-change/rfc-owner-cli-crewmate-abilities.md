---
title: Owner CLI for crewmate abilities and approval mode
status: draft
author: cschnidr
created: 2026-11-03
last-audited: 2026-11-03
audited-at: 34619c898e
doc-pr:
implementation-prs: []
tracking-issues: [26999, 21641, 26898]
supersedes: []
superseded-by: []
---

# RFC: Owner CLI for crewmate abilities and approval mode

- Status: draft. Nothing built. Claims about today's code are verified at
  `34619c898e`; re-verify at implementation time.
- Author: cschnidr
- Related: [rfc-mate-owned-agent-config.md](rfc-mate-owned-agent-config.md)
  (the per-mate file this edits), [rfc-crewmate-share.md](rfc-crewmate-share.md)
  (import, which still refuses MCP servers and skills).

## 1. Summary

Give the owner's terminal the two crewmate edits the dashboard already makes,
through the same handlers and the same owner gate:

```
kirocrew agent abilities apply <mate> <spec.json> [--dry-run]
kirocrew agent abilities remove <mate>
kirocrew agent create|update <mate> --approval-mode normal|trust_reads|trust
```

`apply` replaces what an earlier `apply` of the same spec added (prompt, MCP
servers, skills, resources, tools, allowed tools) instead of merging. The
command mints a short owner session the way `kirocrew app install` does, so
an agent's shell or a sandboxed process is refused exactly as it is today.

This is the first, smallest step of #26999. App-declared crewmates stay a
later RFC.

## 2. Motivation

### Current state

- The crew editor's **Add abilities** and **Remove** call
  `PUT /api/agents/{name}/capabilities` and
  `POST /api/agents/{name}/capabilities/detach`
  (`dashboard/routes/agents.py:47-50`). Both handlers start with
  `require_owner_dashboard_request` (`dashboard/handlers/agent_capabilities.py:56`,
  `:138`), which admits only a dashboard session whose subject is the owner and
  that is not an app token (`dashboard/handlers/source_providers.py:870`).
- The capability state already covers every part a spec carries:
  `CAPABILITY_SECTIONS` is mcpServers, tools, allowedTools, autoApprove, skills,
  prompt, model, resources (`agent_state.py:188`).
- `kirocrew agent update` sets only template, workspace, display name, memory
  store and model (`kirocrew agent update --help`). There is no CLI for
  abilities or for `approval_mode`.
- The CLI already has an owner path to owner-only routes: `kirocrew app
  install|enable|disable` reads the local secret, mints a two-minute session
  at `/api/token/local` and posts the lifecycle action
  (`app_lifecycle_client.py:160-185`). `/api/token/local` refuses a process it
  cannot verify as the local owner, with "from an agent's shell or a sandbox"
  (`dashboard/handlers/core_local_token.py:113-115`).

### Problems

1. **Every spec change is a manual round per mate.** Add abilities only adds
   what is missing (crew-members.md, "Its worker"), so a changed prompt or MCP
   argument needs Remove then Add. A household app with seven crewmates needs
   fourteen clicks for a one-line prompt fix, after every app update.
2. **A forgotten round fails silently.** A mate keeps the old prompt and
   tools with no warning; nothing compares the mate file with the spec it came
   from.
3. **Remove leaves residue** (#26898): skill names no current ability set adds
   stay in the mate file and cannot be removed from the editor.
4. **A new mate opens in `trust`** while `approval_mode` is unset
   (`config/sections_agent_fields.py:235` and the seed in crew-mode.md). The
   owner can fix it only in the profile, after the mate exists.
5. **No owner automation.** The owner cannot script "after sync, refresh all
   mates", although the same owner can script app install today.

## 3. Goals

- An owner can refresh every crewmate from its spec in one terminal command.
- Applying the same spec twice changes nothing; applying a changed spec
  replaces exactly what the previous apply set.
- A new crewmate can be created with its approval mode in the same step.
- No new trust: same gate, same governance ceiling, same audit as the editor.

## 4. Non-goals

- Agents or crewmates changing their own or another mate's abilities. The
  owner gate stays.
- App-declared crewmates and install-time cards (#26999 phase 2).
- Changing what Add abilities does in the dashboard.
- Mate-bound crons in app manifests (#26911).

## 5. Design

### 5.1 `kirocrew agent abilities apply <mate> <spec.json>`

1. Read the spec (the custom-agent schema an abilities source already uses).
2. Mint an owner session through `/api/token/local`, reusing the
   `app_lifecycle_client` helper. Refused there means refused here, with the
   same message.
3. `POST /api/agents/{name}/capabilities/preview` with the operations below;
   print the preview (sections, added, replaced, removed). `--dry-run` stops
   here.
4. `PUT /api/agents/{name}/capabilities` with the same operations and the
   preview's revision, so a concurrent editor change is refused, not
   overwritten.

Operations are built from the spec: `set` for every entry in the spec's
prompt, mcpServers, skills/resources, tools, allowedTools and autoApprove;
`remove` for every entry the previous apply of a spec with the same `name`
recorded and the new spec no longer has. The record of "what this spec set"
is a small sidecar next to the mate file, keyed by spec name, written in the
same handler call. Entries the owner added by hand are never touched.

### 5.2 `kirocrew agent abilities remove <mate>`

The CLI twin of the editor's Remove, plus clearing the entries the sidecar
says a spec added. This also covers #26898 for this path.

### 5.3 `--approval-mode`

`kirocrew agent create` and `update` take `--approval-mode
normal|trust_reads|trust`. `POST /api/agents` already accepts `approval_mode`
in the create (crew-mode.md, "a caller that must never leave a new mate in
`trust` pins it there"); `update` goes through the profile's compare-and-set
write.

### 5.4 Output and exit codes

Human-readable diff by default, `--json` for scripts. Exit 0 applied or
nothing to do, 1 refused (gate, governance, conflict), 2 bad spec.

## 6. Migration plan

### Phase 1: `apply --dry-run` and `remove`

PR-sized. Exit criteria:
- From the owner's terminal, `apply --dry-run` prints the same preview the
  editor shows for the same spec.
- From an agent's shell, the command exits 1 with the `/api/token/local`
  refusal and writes nothing.
- `remove` leaves the mate file equal to a mate that never had the spec.

### Phase 2: `apply` with replace semantics

Exit criteria:
- Applying spec A, then A again, leaves the mate file byte-identical.
- Applying A, then A' (prompt changed, one skill dropped), yields the prompt
  of A' and no trace of the dropped skill; a skill the owner added by hand
  survives.
- A concurrent editor save between preview and apply makes `apply` exit 1.

### Phase 3: `--approval-mode` on create and update

Exit criteria:
- `agent create --approval-mode trust_reads` creates a mate that is never
  observed in `trust`.

## 7. Backward compatibility

Additive. Mates edited only through the dashboard behave as today. A mate
without a sidecar is treated as "nothing applied yet"; the first `apply`
behaves like Add abilities plus `set` for entries that differ.

## 8. Security considerations

- The owner gate is unchanged; the CLI uses the existing owner-session mint,
  which already refuses agents and sandboxes.
- Every write goes through `CapabilityService` and the governance ceiling,
  like the editor. A spec cannot lift a mate past policy.
- The same SEL audit records the change, with source `cli`.
- No new endpoint reachable without an owner session.

## 9. Alternatives considered

- **Change cards (`crewmate.capabilities`)**: owner-approved and already
  support `set prompt`, but one card per mate, raised only from an agent turn
  in the dashboard, so not scriptable from a terminal.
- **Edit `~/.kiro/mates/agents/<mate>.json` directly**: the file is sealed
  for agents and bypasses governance, revision checks and audit.
- **App-declared crewmates (#26999)**: the full fix for apps, but larger and
  not useful to owners without an app.

## 10. Open questions

- Should the sidecar live in the mate file's agent_state or as its own file?
- Should `apply` accept a spec name registered by an app
  (`<app>/<spec>`) as well as a path?
- Command name: `kirocrew agent abilities …` fits the existing CLI, where
  `kirocrew agent create|update|list|delete` already manages crew members
  (mates). The dashboard now says "Mates", though, so `agent` reads as
  "agent template" to a new owner. Options: keep `agent`, add a
  `kirocrew mate` alias for the whole group, or put only the new verbs
  under `kirocrew mate abilities …`.
