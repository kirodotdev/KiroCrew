---
title: Ask before an MCP tool its server marks destructive runs, on every approval path
status: draft
author: javenciu
created: 2026-11-09
last-audited: 2026-11-09
audited-at: d85b4051b3
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Ask before an MCP tool its server marks destructive runs, on every approval path

Status: draft. Acceptance requested from a maintainer of the dashboard approval ladder and a maintainer of governance. Main-side references were read at `d85b4051b3`. The prototype is the open pull request [#33661](https://github.com/kirodotdev/KiroCrew/pull/33661), which is not on main.

## Summary

An opt-in setting, `hooks.confirm_destructive_mcp_tools` (default off), makes an MCP tool ask for approval when its server annotated it `destructiveHint: true` and did not also annotate it `readOnlyHint: true`, even when the session would otherwise approve the call without showing a card. The setting can only add a prompt.

Two reviews of the prototype showed that a card-time check on one rung is not enough. Kiro Crew approves an MCP call without a card by several independent paths, and the first prototype covered one. This document lists every path, says which ones the setting covers and why, and recommends one predicate that the dashboard rungs, the static grant writers (through `may_skip_gate_now`) and the KAS projection all read. The setting is read from the live last-valid config snapshot, so a half-saved `config.json` does not turn it off.

## Motivation

### Current state on main

- `_slot_is_trusted` in `src/kiro_crew/dashboard/chat_turn/tool_approval.py` is true when a person clicked "trust this session" (`slot._trust`) or when a scoped safety-override grant is live (`slot._trust_scope`). `_decide_permission_request` in the same module then approves the call with no card. The decision never looks at what the tool does.
- `_native_crew_should_auto_approve` in the same module runs earlier in `_decide_permission_request`. With a native crew subagent active it grants on any of three signals: the `auto_approve_subagent_tools` hook flag, `_slot_is_trusted`, or a live YOLO grant. A card-time check placed only after that rung never runs for those calls.
- Static `allowedTools` entries skip the permission request entirely, so no card-time check can see them. `may_skip_gate_now` in `src/kiro_crew/platform/governance.py` is the entry point every writer of such an entry calls. It combines the governance ceiling and profiles (`_policy_may_skip_gate_now`) with the operator's `hooks.confirm_tools` list (`_operator_requires_confirmation`), and it fails closed when that list cannot be read (`_confirm_tool_patterns` returns `None`, which `_operator_requires_confirmation` answers with True).
- The discovery probe collects each server's tool annotations. `_ProbeResult.tool_annotations` and `_probe_cache` in `src/kiro_crew/mcp_discovery_probe_state.py` hold them, and `_cache_probe` keeps the last good copy when a later probe fails. `shareability.py` under `src/kiro_crew/mcp_gateway/` reads `readOnlyHint` to recommend sharing. Nothing reads `destructiveHint` to decide approval.
- The comment in `_tier_read_only` in `src/kiro_crew/hook_runtime/gate_tiers.py` records that `readOnlyHint` is a manifest claim the gate never receives, so it may never widen approval. The narrowing direction of the same annotations is unused.

### The problem

In Trust mode, a server's `delete_*` and `send_*` tools are approved along with its reads. An operator who wants "Trust mode, except the tools the server itself calls destructive" has no setting. `hooks.confirm_tools` can name tools, but the operator must know every name in advance and keep the list current as servers change.

The prototype's first review round found two ways the setting silently did nothing: the native-crew rung approved before the new check, and the HTTP probe never recorded the destructive set. The second round found two more: a dashboard-enabled server gets a static `@server` entry in `allowedTools` that skips the permission request, and the setting was read with a fresh `KiroCrewConfig.load()` that returns defaults, so the flag, once on, read as off while the file was torn. A fix per finding leaves the next path undiscovered. The inventory below is the exhaustive version.

## Goals

- One definition of "destructive": the last probe of the server saw `destructiveHint` equal to JSON `true` on the tool, and not `readOnlyHint` equal to `true`.
- One predicate that every approval path in the table below reads, so a path cannot be covered by one rung and missed by another.
- A static grant is withheld for a server whose last probe shows a destructive tool, and is restored when the probe or the setting changes.
- The setting is read from the live last-valid snapshot. An unreadable config does not turn it off.
- With the setting off, every decision is the same as on main today.

## Non-goals

- YOLO, the hook flag `auto_approve_subagent_tools`, and the operator's own `hooks.auto_approve_tools` patterns. Each is an explicit grant by the operator and stays one.
- Treating a tool with no annotation as destructive. See [Prior art](#2-prior-art) for why, and `hooks.confirm_tools` for the way to ask about those.
- A security boundary against a hostile server. See [Trust boundary](#4-trust-boundary).
- Changing a default. Turning the setting on by default is for maintainers, later.

## 1. Every path that approves an MCP call without a card

"Covered" means: with the setting on, a call to a destructive-annotated tool on that path shows a card (or, for a static grant, reaches the gate and then a card). The last column says what covers it.

| # | Path | Where it is decided | Covered? | Why, and by what |
|---|---|---|---|---|
| 1 | Dashboard Trust rung | `_decide_permission_request` and `_slot_is_trusted` in `src/kiro_crew/dashboard/chat_turn/tool_approval.py` | Yes | Prototype PR 1. Trust is withheld for a withheld call when YOLO is not active, and the call falls to the card. |
| 2 | Native-crew rung | `_native_crew_should_auto_approve` in the same module | Yes, for the Trust signal only | Prototype PR 1. Session Trust no longer approves a withheld call there. The hook flag and YOLO on the same rung are separate grants and stay (rows 4 and 5). |
| 3 | Static `allowedTools` grants, one row per writer | `may_skip_gate_now` callers, below | Yes, in PR 2 | A static entry skips the permission request, so only the writer can withhold it. All writers already call `may_skip_gate_now`, so one clause there covers all of them. |
| 3a | Dashboard enable of a server | `_sync_mcp_to_agent_unlocked` and `_sync_mcp_to_agent_batch_unlocked` in `src/kiro_crew/dashboard/handlers/mcp_agent_sync.py` | Yes | Writes `@server` into `allowedTools` when `may_skip_gate_now` holds. This is the path the second review found. |
| 3b | Agent materialization | `_may_auto_approve` in `src/kiro_crew/agent_materialization/auto_approve.py`, `_strip_excluded_auto_approve` in `src/kiro_crew/agent_materialization/worker_agent.py` | Yes | Both defer to `may_skip_gate_now` or `strip_ungoverned_auto_approve`. |
| 3c | App agents | `_may_auto_approve` in `src/kiro_crew/apps/bridges_agents.py` | Yes | Defers to `may_skip_gate_now`. |
| 3d | Default Crew servers | `with_default_crew_servers` in `src/kiro_crew/acp/default_crew_servers.py` | Yes | Gates each added server on `may_skip_gate_now`. These are Crew's own control-plane servers, which carry no annotations, so in practice nothing changes. |
| 3e | KAS projection and skill view | `auto_approve_refs` and `grant_in_skill_view` in `src/kiro_crew/acp/kas_auto_approve.py`, `_ceiling_permitted` in `src/kiro_crew/acp/kas_agents.py`, `allowed_tools_to_permissions` in `src/kiro_crew/acp/kas_permissions.py` | Yes | Per-verb `autoApprove` refs and `allowedTools` pass `may_skip_gate_now`. The permissions block is derived only from what survives, so it needs no change of its own. |
| 3f | Doctor auto-fix | `_doctor_mcp_tools` in `src/kiro_crew/cli_doctor_parts/mcp_sections.py` | Yes, with a wording change | Reports a withheld grant. It must say the setting withheld it, not an enterprise ceiling (`withheld_by_confirm_tools` is the precedent). |
| 4 | YOLO | `is_yolo_active` and `yolo_reaches` in `src/kiro_crew/dashboard/state_parts/dashboard_services.py` | No | YOLO is the operator's "approve everything" switch. A setting that quietly narrows it would make YOLO unpredictable. |
| 5 | Subagent auto-approve hook | `_native_crew_should_auto_approve`; the field `auto_approve_subagent_tools` of `HooksConfig` in `src/kiro_crew/hook_runtime/hook_types.py` | No | An explicit operator grant, separate from Trust. See open question 2. |
| 6 | Conductor own-child verbs | `_conductor_own_child_call`, `_CONDUCTOR_OWN_CHILD_VERBS` in `tool_approval.py`; [rfc-conductor-own-child-verbs.md](rfc-conductor-own-child-verbs.md) | Not applicable | The verbs are Crew's own `session_send` and `session_close`. Crew's control-plane tools carry no annotations (see `session_mcp_restricted_servers` in `src/kiro_crew/acp/session_mcp_restricted.py`), so the predicate never matches. |
| 7 | Channel trust and owner YOLO | `_answer_permission_request` in `src/kiro_crew/slack/handler_runtime/permission_ladder.py`; `is_session_trusted` in `src/kiro_crew/messaging/session_trust.py`; the `auto_approve_session` hook of `TurnDriver` in `src/kiro_crew/messaging/driver.py` | Not in PR 1 | These ladders never consult the probe. The predicate is in-process, so they can read it. PR 4 adds it, once open question 3 is decided. |
| 8 | Cron `approval_mode: auto` | `cron_job_auto_permitted` in `src/kiro_crew/cron_service/fields.py` | Not in PR 1 | A job has nobody to answer a card. Withholding would stall the job until its timeout. Open question 3. |
| 9 | Scoped worker grants | the `_trust_scope` arm of `_slot_is_trusted`; `WorkerAutoApprove` in `src/kiro_crew/apps/worker_auto_approve.py` | Yes today, by row 1 | `_slot_is_trusted` returns True for both representations, so withholding Trust withholds a scoped grant too. An unattended worker then waits on a card. Open question 3. |
| 10 | Built-in read-only auto-approve | `_tier_read_only` in `src/kiro_crew/hook_runtime/gate_tiers.py` | Not applicable | Judges built-ins and shell commands. An MCP tool is not provable read-only there. |
| 11 | A runtime that auto-approves `readOnlyHint` tools itself | codex, per `session_mcp_restricted_servers` | Not applicable | The predicate excludes any tool that also says `readOnlyHint: true`, so it asks nothing the runtime already approves by that hint. Crew cannot gate a call the runtime never forwards. |
| 12 | Server never probed, or tool added after the last probe | `_probe_cache` | No | No annotation is known. A boot race (`_bg_mcp_probe` in `src/kiro_crew/dashboard/handlers/mcp_probe.py` has not finished) is a gap the documentation must name. PR 2 closes the grant half when the probe lands. |
| 13 | Server sends no annotations, or omits `destructiveHint` | n/a | No | The setting reads a claim. See [Trust boundary](#4-trust-boundary). |
| 14 | Operator `hooks.auto_approve_tools` patterns | `_tier_operator_grants` in `src/kiro_crew/hook_runtime/gate_tiers.py` | No | Written by the operator for named tools. `hooks.confirm_tools` already overrides it (`_operator_confirms`). Open question 2. |

## 2. Prior art

Behaviors only; no text is copied.

- **MCP specification, tool annotations** ([2025-06-18 schema](https://modelcontextprotocol.io/specification/2025-06-18/schema), MIT-licensed specification repository). `destructiveHint` and `readOnlyHint` are optional booleans. `destructiveHint` is meaningful only when `readOnlyHint` is false. The defaults are `readOnlyHint` false and `destructiveHint` true, so an unannotated tool is destructive on paper. The schema says every annotation is a hint, not a guarantee, and that clients should not base tool-use decisions on annotations from untrusted servers.
- **Claude Code** ([permission modes](https://code.claude.com/docs/en/permission-modes); documentation under Anthropic's terms). Documents which MCP tools prompt in each permission mode. The behavior to note: MCP tools marked `requiresUserInteraction` still prompt in a mode that otherwise approves, so a per-tool prompt survives a permissive mode.
- **Codex** ([approvals and security](https://learn.chatgpt.com/docs/agent-approvals-security); the CLI is Apache-2.0 at [openai/codex](https://github.com/openai/codex)). Documented to ask for approval on app and MCP calls that advertise a destructive annotation. A public report on [laravel/mcp issue 133](https://github.com/laravel/mcp/issues/133) says OpenAI's client treats an unspecified `destructiveHint` as its default `true`, which prompts for tools that are not destructive. That is the cost of treating "no annotation" as destructive, and the reason this design counts only an explicit `true`.
- **Cursor** ([agent security](https://prod.cursor.com/docs/agent/security) and [permissions.json](https://cursor.com/docs/reference/permissions); documentation under Cursor's terms). A connection is approved once, each tool call is approved individually unless allowlisted, and, per user reports on its forum, an auto-run mode bypasses the allowlist prompt. Its allowlist is per tool, not per annotation. That supports a per-tool grant (alternative D) and shows the same gap: a mode that approves everything also approves the destructive call.

## 3. Design

### 3.1 One predicate

Two functions, both with no I/O beyond the live snapshot and the in-process probe cache:

- `destructive_confirmation_enabled()` returns whether the setting is on, read as in 3.3.
- `mcp_call_asks_destructive(server, tool)` returns `destructive_confirmation_enabled()` and the last probe of `server` lists `tool` as destructive. The probe side is the prototype's `destructive_tool_names` and `is_destructive_mcp_tool`, which take only a JSON `true` and exclude a tool that also says `readOnlyHint: true`. A server never probed answers False.

The dashboard Trust rung and the native-crew rung call `mcp_call_asks_destructive` with the event's server and tool. The static-grant side needs a server-level form, because a grant such as `@server` names no tool:

- `server_has_destructive_tool(server)` returns the setting is on and the last probe lists at least one destructive tool for `server`.

### 3.2 Static grants

`may_skip_gate_now(ref)` becomes `_policy_may_skip_gate_now(ref) and not _operator_requires_confirmation(ref) and not _destructive_requires_confirmation(ref)`. `_destructive_requires_confirmation` follows the shape `_operator_requires_confirmation` already has:

- A ref that names a whole server (`@server`) is withheld when `server_has_destructive_tool(server)`. This errs toward asking, as `server_requires_confirmation` does for a `confirm_tools` pattern: the server's harmless tools also ask, and the gate settles each call.
- A ref that names one tool (`@server/tool`) is withheld only when that tool is destructive.
- A wildcard ref (`@*`, `@ser*`) is withheld when any matching probed server has a destructive tool. A ref whose literal prefix cannot begin with `@` covers no MCP call and is left alone, as `_operator_requires_confirmation` does.
- A server never probed is not withheld, since nothing is known. The refresh below withholds it once a probe lands.

`withheld_by_destructive_confirmation(ref)` is added beside `withheld_by_confirm_tools`, so doctor can say which setting withheld a grant.

### 3.3 Reading the setting

The prototype reads the setting with a fresh `KiroCrewConfig.load()` and treats a read error as off. The second review showed why that is wrong: `_keep_last_valid` in `src/kiro_crew/config/live.py` exists because an unparseable `config.json` loads as defaults, and it keeps the previous snapshot's values while the file is torn. `snapshot()` in the same module returns that last-valid copy as a plain attribute read, so it is also safe on the event loop.

The read is:

1. `snapshot()` is not `None`: use its `hooks` section. A tear keeps the last good value.
2. `snapshot()` is `None` (the watcher has not started, as in a short-lived process): use the same file-state check `_confirm_tool_patterns` in `governance.py` uses before it trusts a degraded load. If a config file does not parse and names the key, the answer is "on". Otherwise the loaded value stands.
3. The read raises: on. The predicate only adds prompts, so the safe direction for an unknown is to ask. This is the same direction `confirm_tools_unknown_now` takes.

### 3.4 Refresh

A static grant is written at one moment and read for the life of the spec, so it goes stale when either input changes. Two triggers re-run the existing re-projection:

- The setting changes. `subscribe` in `live.py` takes a path prefix and a callback; it dispatches on the live watcher's change events. The callback calls the re-projection that `reproject_for_ceiling_change` in `src/kiro_crew/agent_parts/spec_rebuild.py` already performs for a ceiling change, so the grants are rewritten with the new answer.
- A probe changes the server's destructive set. `_cache_probe` compares the new set to the cached one and, only when it differs, calls the same re-projection. A probe that changes nothing costs nothing.

Whether `reproject_for_ceiling_change` reaches every writer in rows 3a to 3e is open question 4. Where it does not, PR 2 either extends it or lists the writer that still needs a restart.

### 3.5 What PR 1 keeps

The prototype's gate rungs stay as they are: `_trust_grant_withheld` on the Trust rung, the same check on the Trust signal of the native-crew rung, and the HTTP probe filling `destructive_tools`. PR 1 changes one thing: the setting is read through 3.3 instead of a fresh load.

## 4. Trust boundary

The setting can only add a prompt. No input of this design removes a card that main shows today, so a wrong annotation, a stale cache or a hostile server cannot make a call less safe than it is now.

It is not a boundary. A server that wants its destructive tool to run unprompted omits the hint, or sets `destructiveHint: false`, and the setting does nothing for that tool. The MCP schema says annotations from an untrusted server are not a basis for decisions. This design uses them in the one direction where being fooled costs only a prompt: a hostile server that sets the hint on a harmless tool wastes the operator's click. The documentation should say "defense in depth for servers you already trust enough to run", and point to `hooks.confirm_tools` and the governance ceiling for anything that must hold against the server.

Two narrower points:

- A stale probe. `is_destructive_mcp_tool` reads the cache whatever its age. If a server later drops the hint, the old claim keeps asking until a probe succeeds. That errs toward asking.
- Server names. The cache is keyed by configured server name, so two servers that share a name share an answer. Names are unique in a config, so this is the same limit every name-keyed rule in the gate has.

## 5. Alternatives considered

- **A. Gate rungs only (the first prototype).** Rejected by the reviews of #33661. It leaves row 3: a dashboard-enabled server's `@server` entry skips the permission request, so the setting does nothing for the most common way a server is enabled. The user is told destructive tools ask, and they do not.
- **B. Withhold through `may_skip_gate_now` (recommended, with the rungs).** One clause in the function every writer already calls, so a new writer inherits it. The cost is that a whole server asks when any one of its tools is destructive.
- **C. Express it as `hooks.confirm_tools` patterns.** Write a `@server/tool` pattern for each destructive tool on probe. This reuses the withholding and the gate tier that exist, but it rewrites the operator's own list from a server's claim, mixes machine-written and hand-written entries in one setting, and loses the "off" state: turning the setting off would have to delete only the entries it wrote.
- **D. Per-tool static grants.** Rewrite `@server` into `@server/tool` for each non-destructive tool. A server's reads keep running without a card. It needs a complete, current tool list at write time, a grant per tool in every spec, and a rewrite whenever the server adds a tool, which is how an unlisted destructive tool would run unprompted. Left as a later refinement of B once row 12 is closed.

## 6. Migration

No data migration. The setting is a new key, absent today, so every existing config reads it as off. `config-baseline.json` gains the key and its default when PR 1 lands, as the prototype does. With the setting off, `may_skip_gate_now` and every rung decide as on main.

Turning it on rewrites static grants (3.4). A server the operator enabled before the change loses its `@server` grant in `allowedTools` only if its last probe lists a destructive tool, and gets it back when the setting goes off or the tool stops being annotated.

## 7. Rollout

The setting ships off by default and stays off until maintainers choose otherwise. The documentation entry names what it does not cover: unprobed servers and tools added after the last probe, servers that send no annotations, YOLO, and the paths in rows 5, 7, 8 and 14 until their PRs land. No flag is needed beyond the setting itself, since it only adds prompts.

## 8. PR plan

Each step is independently mergeable, under about 300 added source lines, and leaves the setting off by default.

1. **Gate rungs and the live read.** The prototype ([#33661](https://github.com/kirodotdev/KiroCrew/pull/33661)): the setting, the probe's destructive set for stdio and HTTP servers, the Trust rung, the Trust signal on the native-crew rung, the baseline and the documentation entry. Adds 3.3, replacing the fresh load. Tests: the existing set plus a torn-config test that keeps the flag on.
2. **Static grants.** The 3.2 clause in `may_skip_gate_now` and `withheld_by_destructive_confirmation`, plus the 3.4 refresh on a setting change and on a changed destructive set. Tests: a dashboard enable with a destructive-annotated server writes no `@server` entry in `allowedTools`; flipping the setting restores it.
3. **KAS and doctor.** A test that the KAS permissions block carries no allow rule for a withheld server, and doctor's wording from `withheld_by_destructive_confirmation`. May need no source change beyond the doctor line.
4. **Channels.** The predicate in the Slack ladder (`_answer_permission_request`) and the messaging `TurnDriver` session-Trust hook, with the unattended policy from open question 3.
5. **Optional: per-tool grants** (alternative D), only if maintainers want a server's reads to keep running without a card.

## 9. Open questions for maintainers

1. Should a server with one destructive tool ask for all its tools (3.2 as written), or should PR 2 go straight to per-tool grants (alternative D) at the cost of a longer first PR?
2. Should the setting also withhold `auto_approve_subagent_tools` (row 5) and the operator's `hooks.auto_approve_tools` patterns (row 14)? This RFC leaves both alone because each is an explicit grant, and a pattern the operator wrote can be removed by the same operator.
3. For an unattended session (a cron job in `auto` mode, a scoped worker grant, a Slack session with nobody present), is a card that waits until timeout acceptable, or should the call be refused with a message, or exempted? The prototype's scope review raised this. A refusal is the only choice that never hangs; an exemption makes the setting meaningless for exactly the sessions with no human watching.
4. Does `reproject_for_ceiling_change` reach every writer in rows 3a to 3e, and is a changed destructive set a legitimate trigger for it? If not, which writer needs its own refresh?
5. When the config cannot be read and no snapshot exists, is "on" (3.3, item 3) the right answer for a setting that only adds prompts, or does the gate prefer one rule for every unreadable key?
6. Is a stale cache entry acceptable, or should a destructive claim expire after a number of failed probes?
