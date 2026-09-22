# Agent Spec Field Reference

The fields Kiro Crew reads or writes on an agent spec, what each one does, and
how that answer changes per backend.

Not a closed list, and deliberately not claimed as one: kiro-cli owns the schema,
Kiro Crew adds fields as features land, and an app or an edition can write one
this page has never seen. To check a field missing here, grep the spelling under
`src/kiro_crew/`; a field kiro-cli accepts but Kiro Crew ignores has no entry
below because there is nothing Crew-side to describe.

**How to read every statement here.** Each one is what the cited symbol does on
`main` today, not a guarantee across versions, backends or spec types. Where a
sentence says "never" or "only", a single named reader backs it and that reader is
cited; where behaviour varies by backend the per-surface table is the answer, and
where it varies by harness [How the other backends consume a
spec](#how-the-other-backends-consume-a-spec) is. This page is a pointer into the
code, not a contract the code is held to — if the two disagree, the code is right
and this page is stale.

This is the field table [Agents & Configuration](agents.md) and the
[agent host contract](../../../docs/system-specs/modules/agent-host-contract.md)
do not have. Read `agents.md` first for how to create, switch and map skills onto
an agent; read the host contract for how each ACP harness differs as a whole.
This page only answers "what does this key do".

Citations name a file and a symbol rather than a line number: line numbers rot on
the next refactor and the repository's own docs lint rejects them.

## The two forms

One spec, two serializations. `src/kiro_crew/agent_spec_format.py` is the single
parser for both, so every scan sees the same dict shape.

| | JSON | Markdown |
|---|---|---|
| Path | `~/.kiro/agents/<name>.json` | `~/.kiro/agents/<name>.md` |
| Fields | the JSON object | YAML frontmatter, JSON-shaped only |
| Prompt | the `prompt` field | the body; frontmatter `prompt` only when the body is empty |
| Twin resolution | wins | dropped, with a gateway warning |

`parse_markdown_spec` normalizes the frontmatter to the JSON shape: a
comma-separated `tools: read, write` becomes `["read", "write"]`, and a value
with no JSON form (`!!binary`, a YAML alias, a non-string key, `.inf`) makes the
file unreadable as a spec. The rest of this page uses the JSON spelling; a
frontmatter key of the same name behaves identically.

## Who reads a spec

This is the axis every per-field answer hangs off, and it is two-dimensional.
`agent.provider` picks the seam; `agent.acp_backend` picks the harness the ACP
seam spawned (`src/kiro_crew/agent_sdk/provider_identity.py`,
`src/kiro_crew/agent_sdk/backend_identity.py`).

| Surface | Config | How the spec reaches it | Consequence |
|---|---|---|---|
| kiro-cli | `provider=acp`, `acp_backend=""` (default) | not at all — kiro-cli opens the file itself, named by `--agent <name>` on the spawn argv (`src/kiro_crew/acp/client.py`, `_spawn`) | the file IS the contract; an unknown key drops the whole spec |
| KAS | `provider=acp`, `acp_backend="kas"` | Crew parses it and projects it onto `_meta.kiro.customAgents` (`src/kiro_crew/acp/kas_agents.py`, `to_client_custom_agent`) | only fields with a wire slot survive |
| Claude Code seam | `provider=claude_code` | not at all — nothing reads the spec | Crew injects the spec's effect into context itself (the `is_cc` branches) |
| claude-agent-acp harness | `acp_backend="claude"` | not as a spec file — Crew projects field by field | `providers/mirrors/claude_code.py` is the per-field ruling; [How the other backends consume a spec](#how-the-other-backends-consume-a-spec) is the table |

Three surfaces, not three backends. `src/kiro_crew/agent_sdk/backends.py` names
eight (`""`, `kas`, `claude`, `codex`, `opencode`, `pi`, `goose`, `deepseek`),
and only `kas` reads the markdown form (`ACP_BACKENDS_MARKDOWN_AGENT_SPECS`).
[How the other backends consume a spec](#how-the-other-backends-consume-a-spec)
below is what happens to the other five.

These two are separate rows because they are separate axes, and only one is
dormant. `provider=claude_code` is dormant in the public build, whose config
schema admits only `acp` (`provider_identity.py`); the `is_cc` branches in
`src/kiro_crew/context.py` are what remain of it, and they are what make the
`resources` field mean two different things (see below). `acp_backend="claude"`
is the harness and is a different matter: `resolve_selected_backend` accepts it,
so the public build can select it, and its field-by-field ruling is recorded in
`src/kiro_crew/providers/mirrors/claude_code.py`. Neither axis implies the other
(`backend_identity.py`).

kiro-cli validates the file with serde `deny_unknown_fields` and silently falls
back to the default agent on any key it does not know — `--agent <name>` resolves
to the default with only a stderr line. That is why Crew's own per-agent
bookkeeping lives in a sidecar instead (`src/kiro_crew/agent_state.py`), and why
you cannot add your own keys to a spec.

## How the other backends consume a spec

The five backends missing from the table above — `codex`, `opencode`, `goose`,
`pi` and `deepseek` — receive **no agent definition in any shape**: no
`--agent`, and no spec written anywhere, so nothing reads a spec AS a spec
there. That is not the same as receiving nothing. On three of them individual
fields still arrive, one at a time, through whichever channel that harness reads
— a `session/new` parameter, a `session/set_config_option` call after it, or one
environment variable. The `acp_backend="claude"` row above is a fourth harness
answered the same way, and `src/kiro_crew/providers/mirrors/` is where all four
answers are recorded.

Four cases, and the difference between them is the whole answer:

- **kiro-cli reads the file itself.** `--agent <name>` on the spawn argv
  (`acp/client.py`, `_spawn`); Crew projects nothing. `registry.py` records it
  as kind `native`.
- **KAS gets a wire projection.** Crew parses the spec and sends it as
  `_meta.kiro.customAgents` (`acp/kas_agents.py`, `to_client_custom_agent`).
  Kind `external`: a real projection down a real channel whose code has not yet
  moved into the mirrors folder.
- **The four MIRRORED harnesses get individual fields, decided one by one.**
  `claude`, `codex`, `opencode` and `goose`. Crew reads the spec and a mirror
  rules on each field. `providers/mirrors/base.py` is the vocabulary: `Concern`
  is the closed list of things a spec expresses, so adding one obliges every
  backend to answer it; `Disposition` is the four answers (`delivered`,
  `translated`, `no-channel`, `withheld`); and `AgentConfigMirror.rulings` is
  abstract, so a new backend cannot inherit silence.
- **`pi` and `deepseek` have no mirror, so no field is DECIDED field by field.**
  What they lose is the projection — the spec's own `mcpServers` and its per-tool
  deny set. Two things still reach them, by channels that are not the mirror and
  not the spec file: `model`, pushed with `session/set_config_option` (both are in
  `agent_sdk/backends.py`, `ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION`), and `prompt`,
  as ordinary context text. They are still in `registry.py`'s `PROJECTIONS`, which
  carries an entry for every backend precisely so absence is a statement rather
  than a lookup miss — `MIRRORS` is the four above, `PROJECTIONS` is all eight.

**Where the mirror runs.** At session establishment, on both ACP seams, over the
shared translation in `acp/session_mcp.py` (`session_mcp_projection`):

| Seam | Entry point | What it does |
|---|---|---|
| the runtime | `acp/runtime.py`, `AcpRuntime._mirrored_session_mcp` | called only when `has_mirror` says so — a synchronous registry read at the call site, not an await that returns nothing, so kiro-cli's own construction gains no step. Runs the projection off the event loop and hands the pooled broker stubs down as `stub_elements` |
| the client | `acp/client.py`, `AcpClient._resolve_session_mcp_servers` | the same projection, resolved on the spawn path because it blocks on the spec read, then cached; `_session_mcp_servers` serves the shared `session/new` call site from that cache |

Both call `AgentConfigMirror.session_projection` rather than `session_params`,
because one spec parse CAN produce two things and only one of them is wire data:
the `mcpServers` array, and the `(server, tool)` pairs the client refuses itself
when the harness asks permission for them (`acp/client.py`,
`_deny_spec_disabled_tool`). Only `codex.py` fills that second half —
`claude_code.py` puts its deny rules in the settings file instead, and
`opencode.py` and `goose.py` return an empty set and withhold the whole server,
which `base.py` calls out on `SessionProjection` itself. A mirrored backend's
broker stubs are placed by the mirror rather than appended afterwards —
`AcpClient._pooled_mcp_servers` is inert for one — so a single withhold rule
covers the whole array instead of an unnarrowed append re-adding what the
projection withheld.

### Per-harness rulings

Derived from each mirror's own `rulings()`, which is the record a test asserts
against. Cells are kept to a phrase; the qualifiers that matter are numbered
underneath.

| Harness (`acp_backend`) | Mirror | Delivered | Translated | Not delivered, with the class |
|---|---|---|---|---|
| `claude` | `claude_code.py` | `mcpServers` [1]; `model`, `availableModels`, `permissions.defaultMode`, in `settings.local.json` [2] | `tools` → the array's allowlist [3]; `disabledTools` → `permissions.deny` rules (`mcp__<server>__<tool>`), so a narrowed server stays MOUNTED | `autoApprove` — gate-preserving; `prompt` — context-instead; `resources` — no-reader [6]; `hooks` — no channel |
| `codex` | `codex.py` | `mcpServers`, narrowed three ways [4]; `model` [5] | `tools` → the array's allowlist [3]; `disabledTools` → the server is withheld whole, except Crew's control plane [7] | `availableModels` — harness-owns-the-vocabulary; `permissions.defaultMode` — fixed-by-governance (`mode=read-only`); `autoApprove` — gate-preserving; `prompt` — context-instead; `resources` — no-reader [6]; `hooks` — no channel |
| `opencode` | `opencode.py` | `mcpServers`, no transport filter; `model` [5] | `tools` → the array's allowlist [3]; `disabledTools` → the server is withheld whole, control plane INCLUDED [7] | as `codex`, with `permissions.defaultMode` fixed at `ask` and read back off the harness's own resolved config |
| `goose` | `goose.py` | `mcpServers`, the one channel measured as a round trip rather than as an accepted element; `model` [5] | `tools` → the array's allowlist [3]; `disabledTools` → the server is withheld whole, control plane included [7] | as `codex`, with `permissions.defaultMode` carried as `GOOSE_MODE` in the child's environment |
| `pi` | none | — | — | no projection at all — kind `no-channel` [8]; `model` and `prompt` still arrive [9] |
| `deepseek` | none | — | — | no projection of the spec — kind `broker-only` [8]; `model` and `prompt` still arrive [9] |

1. Delivered ONLY when Crew authored `<work_dir>/.claude/settings.local.json`.
   That file is this session's permission surface, and a tool Crew cannot gate is
   not handed to the session at all — so a project carrying its own copy gets no
   Crew MCP tools rather than ungoverned ones. The array is otherwise this
   session's entire MCP surface, because the adapter reads no agent file.
2. Written from the SESSION and from Crew's model registry, not copied out of the
   spec. Which is why no mirrored harness honours a spec-REQUESTED permission
   mode: `claude` is delivering the mode the session asked for, not the one the
   agent file did.
3. The allowlist matches on server NAME (`acp/session_mcp.py`,
   `ToolsAllowlist.grants`), so an `@server/tool` grant narrows to one tool on
   kiro-cli and mounts the WHOLE server on every mirror — the tool inventory is
   not knowable without connecting. Every mirror records this residual. A missing
   or non-list `tools` is an EMPTY allowlist, not "no filter".
4. Unadvertised transports dropped (`drop_unadvertised_transports`), narrowed
   third-party servers omitted, and Crew's own control plane rebuilt carrying
   this session's identity — codex-rs `env_clear()`s its stdio children, so
   nothing reaches them by inheritance.
5. Not through the mirror: pushed with `session/set_config_option` after
   `session/new` (on `goose`, from the provider and model selects the harness
   advertises there).
6. Not the mirrors' own wording: three of the four give this field a reason the
   code does not support. See the note below the table, and
   [#12215](https://github.com/kirodotdev/KiroCrew/issues/12215).
7. `codex` is the one harness that keeps a narrowed CONTROL-PLANE server mounted
   and refuses the call instead, at the permission request
   (`AcpClient._deny_spec_disabled_tool`); withholding `kirocrew-core` would
   leave the session unable to report back at all. `opencode` and `goose`
   withhold it too. On `opencode` that is forced — it emits no per-call
   `(server, tool)` identity Crew could match. On `goose` it is CONSERVATIVE
   rather than forced: the pair is on the wire and Crew reads it, but no
   projection yet mounts a narrowed server with its denied tools filtered out.
8. `pi` accepts the array, stores it on its session state and never hands it to
   the pi process, so a projection written into it would report Crew's tools as
   mounted on a session where none can be called. `deepseek` DOES mount stdio
   elements, so its session holds the shared gateway's pooled broker stubs — just
   not the servers its own spec declares, and not its per-tool deny set.
9. "No mirror" is not "nothing from the spec". Both are in
   `agent_sdk/backends.py`'s `ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION`, so the spec's
   `model` reaches them with `session/set_config_option` after `session/new` —
   the same channel `codex`, `opencode` and `goose` use, which is why `model` is
   the one field no mirror needs to carry. And `prompt` arrives as context text on
   every harness alike (`context.py` appends the `[AGENT SYSTEM PROMPT]` block for
   a custom agent with no backend condition on it). What a mirror decides, and
   these two therefore go without, is the MCP surface.

**No mirrored harness READS `resources`, and the two URI schemes under that one
key survive that differently.** Neither is loaded natively: the harness is not
kiro-cli and is handed no spec. A mapped `skill://` still ARRIVES, as
context rather than as config — `context.py`'s `_skills_injection_plan` returns
`bool(globs) or not is_custom`, with no backend condition, so Crew injects the
mapped set on every backend, as a bounded directory plus each `always: true`
body. The `file://` steering
block is the half that reaches nothing: it is gated on `is_cc`, which is
`is_claude_code(provider_type)`, true only for `provider=claude_code` and never for
an `acp_backend`. `claude_code.py`, `codex.py` and `opencode.py` give `withheld`
the reason that STEERING FILES are injected as context text instead, which is the
one thing that does not happen here; `goose.py`'s reason is already right, and says
no channel is advertised for them.
Correcting those three strings, and deciding whether a spec author should be
WARNED rather than left to read this page, is tracked in
[#12215](https://github.com/kirodotdev/KiroCrew/issues/12215). The ruling stays
**no-reader** rather than context-instead because it is a statement about the
harness: what reaches the session on the skill half is Crew's own injected
directory, not the key the mirror declined to project.

Four classes cover the rest, and `no-channel` is not one of them:

- **gate-preserving** — the value would pre-approve a call INSIDE the harness,
  which then never sends `session/request_permission`, so Crew's permission gate,
  its governance ceiling and its SEL audit are all skipped together.
- **context-instead** — the effect already reaches every harness as ordinary
  prompt text in the `[AGENT SYSTEM PROMPT]` block, so a config projection would
  be a second channel for one guarantee. `prompt` is the field this genuinely
  covers: a custom agent's prompt is loaded and appended with no backend gate.
- **harness-owns-the-vocabulary** — the harness advertises its own list on
  `session/new` and that list is the only source of ids it accepts, so Crew
  CAPTURES the advertised set instead of sending one; projecting the spec's would
  offer ids that kill the session.
- **fixed-by-governance** — Crew writes the value
  `src/kiro_crew/agent_sdk/tool_gate.py` demands (`ENFORCED_ROUTINGS`), not the
  one the spec asked for, so an agent file cannot widen the session past the
  boundary that makes the harness offerable.

`no-channel` is the fourth `Disposition` and a different statement, which is why
`hooks` sits in that column without being a withhold: the backend HAS the
capability and this transport cannot carry it. All four harnesses run hooks
natively; what is missing is the delivery path. `Ruling.__post_init__` refuses a
`no-channel` ruling that does not name the channel it would have to travel on,
which is what keeps a gap from reading as a decision.

`providers/mirrors/identity.py` is shared by the mirrors that carry identity ON
an element: `codex.py` and `opencode.py` call it directly, `goose.py` reaches it
through `opencode_projection`, and `claude_code.py` does not use it at all,
because a claude MCP child inherits the adapter's process env and needs no
element-level carry. `control_plane_identity_env` is that carried env, and
`identity_bound_crew_servers` derives which managed servers must not be mounted
at all — mounted without an identity they would answer `not_bound` to every
call.

How far a per-tool MCP restriction survives is declared separately, as
`per_tool_deny` on the projection record (`registry.py`, `PerToolDeny`):
`settings-file` on `claude`, `per-call` on `codex`, `whole-server` on `opencode`
and `goose`. It is a declaration, not a requirement — what it owes you is knowing
which of the three you are getting BEFORE a session runs.

### What this means for a spec author

Three fields travel everywhere: `mcpServers`, `tools` and `model`. Every mirror
rules them delivered or translated, so a spec's server set, which of those
servers mount, and its model pin all still mean something on a mirrored harness.
Everything else degrades, and the four degradations have different shapes, which
is the part worth knowing before you switch:

- **`tools` travels, but only at server granularity.** `@server` behaves
  identically. `@server/tool` does NOT: it narrows to one tool on kiro-cli and
  mounts the whole server everywhere else, so a spec that pins one tool of a
  server widens to that server's siblings on all four mirrors. If that matters,
  the restriction has to be `disabledTools`, not a narrow `tools` ref.
- **A per-tool restriction can cost the whole server, on three harnesses out of
  four.** `per_tool_deny` on the projection record says which of three forms you
  get BEFORE a session runs. `settings-file` on `claude` keeps the server mounted
  and the harness refuses the tool. `whole-server` on `opencode` and `goose` drops
  the server. `per-call` on `codex` reads as the middle ground and is narrower
  than it sounds: it keeps the server mounted and refuses the call for Crew's own
  CONTROL PLANE only, whose tools carry no annotations so codex asks permission
  for every one of them. A narrowed third-party server is withheld whole there
  too, because codex approves a `readOnlyHint` tool internally without asking, so
  there is no permission request for Crew to refuse at. Read per-call as "the
  control plane survives", not "nothing is dropped".
- **`autoApprove` and a spec-written `permissions` block travel nowhere.**
  Deliberately, and this is the one degradation you want: every call reaches
  Crew's gate instead of being pre-approved inside the harness.
- **`hooks` reaches nothing, and `resources` only half-reaches.** `hooks` is
  `no-channel` on all four. Neither URI scheme is loaded natively, but a mapped
  `skill://` still arrives as Crew's injected skill directory, per the note above,
  so an agent whose skills come from its spec keeps them on a mirrored harness. A
  `file://` steering glob is the one that silently reaches nothing
  ([#12215](https://github.com/kirodotdev/KiroCrew/issues/12215)). `prompt`
  survives as context text.

So a spec written for kiro-cli degrades predictably rather than silently, as long
as you read `per_tool_deny`, the `hooks` ruling and the `resources` note first.
The
[agent host contract](../../../docs/system-specs/modules/agent-host-contract.md)
§1 is the same table from the harness's side.

## Field inventory

### Identity

| Field | Type | Effect |
|---|---|---|
| `name` | str | The dispatch name. Outranks the filename: `spec_by_declared_name` resolves a spec whose declared `name` matches even when the file is called something else, which is how a package installs `<package>-<name>.json` and keeps the bare name. Two specs in one directory declaring the same `name` are refused, not arbitrated (`AmbiguousAgentSpecError`). |
| `description` | str | Roster text, and projected on the KAS wire. A non-string reads as absent (`spec_str`) because the agents directory is shared with other tools and a structured value blanked the whole Agent Templates tab. |
| `welcomeMessage` | str | Rendered once into a new chat transcript. Read by `agent_welcome_message` only, which asks `list_agents` which spec is live rather than re-deciding, then truncates at `WELCOME_MESSAGE_MAX_CHARS`, with the ellipsis inside that budget rather than added to it. Whitespace-only collapses to nothing. Best-effort: an unreadable spec and an absent hint are the same answer. |
| `keyboardShortcut` | str | Carried as an ordinary (non-capability) field through fork and publish (`agent_capabilities.py`, `ORDINARY_FIELDS`). Nothing else in this tree reads it. |

### Prompt

| Field | Type | Effect |
|---|---|---|
| `prompt` | str | The system prompt. A literal string, or a `file://` URI. On the markdown form the body wins over a frontmatter `prompt`. |

`file://` resolution is Crew's, in `resolve_prompt`, and applies on the **KAS path
only** — the kiro-cli path never inlines a prompt because kiro-cli reads the file
itself:

- a relative path is anchored to the agents directory, and may not escape it
  via `..`;
- an absolute path (and `~`) is accepted and resolved;
- the resolved path must not be a credential or governance location, `/proc`,
  `/sys` or `/dev` — the content is shipped over the wire, so a spec pointing at
  `/proc/<pid>/environ` would exfiltrate the gateway's environment;
- an empty or unreadable file is an error, not a fallback;
- a missing or blank prompt falls back to `_KAS_FALLBACK_PROMPT`, because KAS
  requires a non-empty prompt where kiro-cli tolerates an empty one. Crew's own
  `kirocrew-lite` ships `"prompt": ""` for exactly this reason.

`systemPrompt` is not a field Kiro Crew reads. Use `prompt`.

*Unverified:* kiro-cli v3's own on-disk loader is sometimes described as
requiring a relative `file://` prompt ref. Nothing in this repository asserts
that, and Crew's own resolver accepts both forms, so treat the restriction as
unconfirmed.

### Tools and permissions

| Field | Type | Effect |
|---|---|---|
| `tools` | list \| `"*"` | What is MOUNTED. `"*"` (the whole value or an entry) means every tool; `@server` mounts an MCP server whole; `@server/tool` mounts one action. Absent or malformed on the KAS path sends an empty allowlist and logs that the agent will run with no tool access (`_project_tools`) — it does not infer a default. `@kirocrew-core` here is what grants Crew's own MCP server. |
| `allowedTools` | list | What is AUTO-APPROVED. Entries are globs, not names: `@srv`, `@srv/`, `@srv/*` all match the whole server (`_canonical_grant_pattern`). This is the ONE path that never reaches Crew's PreToolUse gate, so every writer filters it through the governance ceiling (`_apply_allowed_tools_ceiling`) and the KAS projection re-filters on read (`_ceiling_permitted`), because a file can predate the ceiling that now governs it. |
| `excludedTools` | list | A RESTRICTION, subtracted after `tools`. `spec_grants_tool_search` reads it: a spec that grants `"*"` then excludes `tool_search` grants no loader. Mirrored onto the worker spec alongside the grants, so "superset of what the default grants" cannot quietly become "superset of what it permits". |
| `permissions` | object | KAS's own currency: `{"rules": [{"capability", "match", "effect"}]}`. Crew WRITES this derived from `allowedTools` (`derived_agent_permissions`) and never treats a block in the file as an INPUT to that derivation — it has not passed the governance ceiling, and an auto-approved call skips the deny floor and the audit trail with it. On disk a hand-written block is left untouched and the backend reads it; on the wire it is not forwarded. One path does read it: fork and publish (`agent_capabilities.py`, `_align_permissions`) compares it against a fresh derivation and refuses with `alternate_permissions_require_review` when the two disagree, so a hand-edited block blocks forking that template. `src/kiro_crew/acp/kas_permissions.py` owns the translation, and refuses the shell and filesystem families outright: a tool-name allowlist carries no resource pattern, so the rule it would produce is unscoped. |
| `toolsSettings` | object | Per-tool settings kiro-cli reads. Crew strips exactly two retired keys on every refresh — `execute_bash`/`shell` `deniedCommands` and `autoAllowReadonly` — because denied commands are enforced only at Crew's PreToolUse gate now, and a stale copy in the spec would keep blocking a built-in the user just re-enabled (`_strip_legacy_denied_commands`). Your other keys are preserved. No KAS wire slot. |
| `hooks` | object | Event-keyed hook lists, stored camelCase (`preToolUse`, `postToolUse`, `userPromptSubmit`, `agentSpawn`, `stop`). Crew merges your `kiro_hooks` config and, when autoimport is on, scripts discovered under `~/.kiro/hooks`, capped per event by `_MAX_USER_HOOKS_PER_EVENT` and in total by `_MAX_TOTAL_USER_HOOKS`. No KAS wire slot, so an agent Crew injects over the wire carries NO hooks — `UNSUPPORTED_SPEC_KEYS` drops the key. KAS does run hooks natively, but from its own agent profile on disk, which is not this spec: what is lost is the delivery path, not the feature. |
| `slashCommand` | any | No KAS wire slot. Nothing else in this tree reads it. |
| `toolAliases` | object | Maps a Connections-exposed MCP tool to the short name a `@alias` reference in `tools` / `allowedTools` resolves through. On the default spec's rebuild Crew recomputes it from the connector registry, dropping the pairs its own record proves it wrote and keeping the rest, whose authorship is unproven and therefore yours (`agent.py`, `_reconcile_tool_aliases_from_disk`). It reconciles the path it is given, so a spec that rebuild does not touch keeps whatever it holds. A non-dict value there is replaced rather than merged. |
| `managedToolPolicy` | object | Which of Crew's managed tools an agent may NOT reach. Read per session by the dashboard (`dashboard/handlers/sessions.py`), which treats a non-object as unreadable rather than absent — the operator wrote something and its meaning is unknown. On an app agent this is CONTAINMENT, not preference, so the framework owns it and a rebuild overwrites it (`apps/bridges.py`). |

### MCP servers

| Field | Type | Effect |
|---|---|---|
| `mcpServers` | object | Name → server entry. The keys become the roster's server chips (`_mcp_server_names`). A local entry may carry `command`, `args`, `type`, `env`, `timeout`, `disabled`, `disabledTools` and `autoApprove`. |
| `includeMcpJson` | bool | Whether the backend also loads its global `mcp.json`. Crew's own generated specs pin `false` (shipped in `defaults.json` and re-pinned by `_refresh_dynamic_fields`), so for those a spec's own `mcpServers` is the complete set. Absent, it reads as `true`. Projected on the wire when it is a bool. |

`autoApprove` inside an `mcpServers` entry is the SECOND way a call skips the
gate, and a more direct one: kiro-cli approves an auto-approved MCP tool locally
and emits no permission request at all, so Crew's callback never runs. Crew's own
managed server entries ship without an `autoApprove` key, and Crew's own writers
are barred from adding one. That is a rule on the writers, not a property of the
file: the managed-server refresh preserves user customizations on an existing
entry, so a hand-added `autoApprove` reaches the map. What removes one is
governance — `_strip_ungoverned_auto_approve` drops any the ceiling has not
cleared, and drops any verb no server spec declares unless the operator sets
`mcp.honour_auto_approve`. A verb a managed or edition spec declares is kept on an
ungoverned host. Each withhold is recorded as a `mcp_auto_approve_withheld`
security event.

On the KAS wire, `env` and `headers` are withheld from every entry — `env`
routinely holds tokens, and a remote entry's `headers` can hold a static
`Authorization`. One exception: `KIROCREW_HOME` survives for Crew's own managed
servers, because it pins the data home.

### Model

| Field | Type | Effect |
|---|---|---|
| `model` | str | The pin. `"auto"` means "no pin, defer to the tier below". `spec_model` coerces a non-string to `"auto"` — the same rule the execution path applies — so a foreign `{"id": "..."}` value reads as no pin rather than as a provider-prefixed id kiro-cli would reject. Not projected on the KAS wire. |

Two sidecar values in `~/.kiro/crew/agent_model_state.json` travel with `model`
and must never be written into the spec (`lift_and_strip_bookkeeping` lifts and
strips them): `model_managed`, whether the pin tracks shipped defaults or is
frozen as your explicit pick, and `cc_model`, a per-agent model for the
`claude_code` provider, which cannot pick one from the spec the way kiro-cli
does.

### Resources

| Field | Type | Effect |
|---|---|---|
| `resources` | list | Two unrelated URI schemes in one list. |

`skill://<glob>` maps skills to the agent. `skill_resource_uris` reads them in
order and `expand_skill_uri` turns each into an fnmatch glob over real paths:
`~/...` against your home, `/abs/...` verbatim, and anything else
workspace-relative. A relative glob anchors at the `project_dir` the caller
supplies — the session's own project, on every path that resolves skills for a
prompt — and only with no project supplied does it fall back to three levels above
the spec file (`<project>/.kiro/agents/foo.json` → `<project>`).
`agent_skill_globs` is what the rest of the product asks.

`file://<glob>` is a steering glob, and a narrower mechanism than it looks:
`_load_steering_resources` in `src/kiro_crew/context.py` reads `resources` from
`kirocrew.json` specifically — not from the session's active agent — globs each
pattern against `$HOME`, and admits only `*.md` files that stay under the trust
base and are not sensitive locations.

`_skills_injection_plan` is the single decision for `skill://`, and it reads the
mapping, not the backend:

| Agent | `skill://` mapping | kiro-cli / KAS | Claude Code |
|---|---|---|---|
| `kirocrew` | none | whole catalog, injected by Crew | whole catalog, injected by Crew |
| `kirocrew` | mapped | mapped set only, injected by Crew | mapped set only, injected by Crew |
| custom | none | nothing — the agent brings its own | nothing |
| custom | mapped | mapped set only, injected by Crew | mapped set only, injected by Crew |

What Crew injects is a bounded directory of the mapped set, plus the full body of
every `always: true` skill inside it — a required instruction is never reduced to a
directory entry. The rest load on demand through `skill_search`. It is the same
injection on every backend: on kiro-cli the native launch view carries no
`skill://` resources at all (`acp/skill_projection.py`), so nothing duplicates it
there, and on KAS the wire projection forwards the array while Crew's own directory
stays the bounded one.

The `file://` steering block does NOT follow that shape. It is still gated on the
backend: injected only on the Claude Code backend, and only for the default agent,
because kiro-cli and KAS read `resources` from the spec themselves and injecting
would duplicate what the backend already loaded.

One more skill mapping exists and is edition-specific: a `builder-mcp` server
entry whose `args` carry `--skill-name-filter a,b`. `_extract_skills` unions it
with the `skill://` set for display. It predates `resources` support.

## Per-surface summary

Three columns, for the three surfaces a spec is READ by. The
`acp_backend="claude"` harness is not one of them — its fields are mirrored into
a different file in a different format, and [How the other backends consume a
spec](#how-the-other-backends-consume-a-spec) above is the table for it and for
every other mirrored harness.

| Field | kiro-cli | KAS | CC seam (`provider=claude_code`) |
|---|---|---|---|
| `name` | resolves `--agent` | wire `id` | roster only |
| `description` | roster only | wire field | roster only |
| `prompt` | read from disk | inlined over the wire | Crew sends its own persona prompt |
| `model` | honoured, `"auto"` resolvable | not projected | `cc_model` sidecar instead |
| `tools` | honoured | wire field; absent means NO tools | roster only |
| `allowedTools` | honoured | translated to `permissions` | not read |
| `excludedTools` | honoured | wire field | read by Tool Search only |
| `permissions` | ignored (kiro-cli field set) | Crew-derived only, never forwarded | not read |
| `mcpServers` | honoured | projected, minus `env` / `headers` | session array instead |
| `includeMcpJson` | honoured | wire field | not read |
| `resources` `skill://` | Crew injects the mapped set; the native launch view carries no `skill://` | forwarded on the wire, and Crew injects the mapped set | Crew injects the mapped set |
| `resources` `file://` | loaded natively | loaded natively | Crew injects, from `kirocrew.json` only |
| `hooks` | honoured | dropped from the wire projection; KAS's own on-disk profile is a separate file | Crew's gate, renamed events |
| `toolsSettings` | honoured | no wire slot | not read |
| `toolAliases` | honoured | no wire slot | not read |
| `managedToolPolicy` | Crew-side, per session | Crew-side, per session | Crew-side, per session |
| `welcomeMessage` | Crew-only (chat transcript) | same | same |

## Ownership and refresh

Kiro Crew owns and rewrites these ten filenames in `~/.kiro/agents/`
(`src/kiro_crew/agent_files.py`, `OWNED_KIRO_AGENT_FILES`); this table is that
list's one copy in the docs.

A spec that is neither one of those ten nor generated by an app is yours — which
is still not untouched. The Template pane's PATCH writes `model` and the `skills`
mapping onto an unmanaged template, and only those two (a markdown spec is
refused outright). No other field of yours is written: the `toolAliases`
recompute below runs on the default spec's rebuild, not over your templates.

An APP agent is a third category, not a user spec. The App Kit generates it into
the agents directory and owns the fields that are containment rather than
preference — `tools`, `allowedTools`, `prompt`, `managedToolPolicy`,
`includeMcpJson` and `resources` are regenerated on refresh, because a
user-pinned copy of a generated path keeps pointing at a previous engine root
(`apps/bridges.py`).

Inside an owned spec a rebuild reassembles the file from `defaults.json`, your
`~/.kiro/crew/agent.json` overrides and the live governance ceiling, so a hand
edit to a field the rebuild computes is replaced. Five keys are carried across
rather than recomputed on the fork and publish path — `prompt`, `model`,
`resources`, `tools`, `allowedTools` (`agent_capabilities.py`,
`_maintain_owned`) — and the worker mirror keeps one: an explicit `model` pick is read back off the file and
carried across, because the mirror's fallback carries the shipped sentinel and
would otherwise clobber your pin.

| Spec | Ownership | Refreshed |
|---|---|---|
| `kirocrew.json` | generated | every gateway start, and on `kirocrew setup --agent-only` |
| `kirocrew-lite.json` | generated | every gateway start |
| `kirocrew-worker.json` | DERIVED from `kirocrew.json` | every gateway start, and re-checked before every worker session |
| `kirocrew-conductor.json` | generated | every gateway start |
| `kirocrew-ledger-conductor.json` | generated | every gateway start |
| `kirocrew-pipeline-conductor.json` | generated | every gateway start |
| `kirocrew-security-conductor.json` | generated | every gateway start |
| `kirocrew-knowledge.json` | generated | every gateway start |
| `kirocrew-research.json` | generated | every gateway start |
| `kirocrew-heartbeat.json` | generated | every gateway start |
| an app's generated agent | the App Kit | regenerated on app refresh; containment fields are framework-owned |
| your own `<name>.json` / `<name>.md` | yours | `model` / `skills` via the Template pane and `toolAliases` on rebuild; nothing else. `.md` is read-only to Crew entirely |
| `<project>/.kiro/agents/*` | the checkout's | never rewritten; shadows the user-level spec of the same name on kiro-cli only |

Project shadowing is kiro-cli's rule, not a universal one. kiro-cli resolves
`--agent` against its cwd before the user directory, which is why `list_agents`
lets a project spec win. The KAS projection is handed `kiro_agents_dir()` alone
(`acp/harness/kas.py`), so a project spec of the same name does not shadow
anything there — the user-level spec is what gets projected. The worker spawn
gate refuses outright rather than choosing, when a checkout ships its own
`kirocrew-worker` spec.

The worker spec is the one with a freshness contract, because it is a mirror.
`_write_worker_spec` copies `tools`, `allowedTools`, `excludedTools`,
`mcpServers` and `model` from the default spec **on disk** (not from the template
it was assembled from), adds `@kirocrew-work` plus its two grants, subtracts cron
scheduling and any opt-in server nobody assigned, and derives `permissions` from
the filtered result. `_require_fresh_worker_spec` then runs before every worker
spawn and has no early `return` by design: it re-derives a stale mirror and
refuses the dispatch when it cannot, rather than starting a worker on grants the
default agent no longer has. A project checkout shipping its own
`kirocrew-worker` spec is refused outright — kiro-cli would resolve that file
first, and Crew will neither rewrite a repository's tracked content nor honour
it.

What you may safely hand-edit: a spec you authored yourself, and in an owned or
app-generated one, nothing — change `~/.kiro/crew/agent.json` or the Template pane instead.

## Markdown form and the Template pane

Frontmatter keys map one-to-one onto the JSON fields above; nesting works
(`mcpServers`, `permissions`). What differs is who may write the file.

| Field | Template pane (Agent Capabilities → Agents) |
|---|---|
| `model` | editable |
| `resources` `skill://` entries | editable, via the Skills section |
| `resources` `file://` entries | read-only, preserved across edits |
| `skill://` wildcards and paths outside known skill roots | read-only, preserved |
| everything else | read-only — the pane renders it, the PATCH ignores it |

`PATCH /api/agents/detail/{name}` recognizes exactly two body keys, `model` and
`skills` (`src/kiro_crew/dashboard/handlers/agents.py`, `api_agent_detail`).
`skills` is a computed view of `resources` and is never written back under that
name, because kiro-cli would reject the unknown field and drop the agent. A
markdown spec refuses every non-GET with `409 markdown_spec_readonly`:
serializing a JSON object over it would drop the prompt body and every field the
handler does not model.

See [Agents & Configuration](agents.md) for the rest of the markdown rules — the
fence requirement, the JSON-twin precedence, and which backends run the form.

## Where to look

| Field | Reader |
|---|---|
| both on-disk forms, the parser | `src/kiro_crew/agent_spec_format.py` |
| `name`, `description`, `model`, `welcomeMessage`, the roster | `src/kiro_crew/agent_discovery.py` |
| `resources` `skill://` | `src/kiro_crew/agent_discovery.py` (`skill_resource_uris`, `expand_skill_uri`, `agent_skill_globs`) |
| `resources` `file://`, the injection decision | `src/kiro_crew/context.py` (`_load_steering_resources`, `_skills_injection_plan`) |
| `tools`, `allowedTools`, `excludedTools`, `mcpServers`, `hooks`, `toolsSettings` — writers | `src/kiro_crew/agent.py` |
| owned filenames | `src/kiro_crew/agent_files.py` |
| `model_managed`, `cc_model`, fork lineage | `src/kiro_crew/agent_state.py` |
| the KAS wire projection | `src/kiro_crew/acp/kas_agents.py` |
| the per-harness field rulings, and which backend has a mirror at all | `src/kiro_crew/providers/mirrors/` (`base.py`, `registry.py`, `identity.py`, `claude_code.py`, `codex.py`, `opencode.py`, `goose.py`) |
| where a mirror runs at session establishment | `src/kiro_crew/acp/runtime.py` (`AcpRuntime._mirrored_session_mcp`), `src/kiro_crew/acp/client.py` (`AcpClient._resolve_session_mcp_servers`) |
| `allowedTools` → `permissions` | `src/kiro_crew/acp/kas_permissions.py` |
| `--agent` on the spawn argv, the freshness gate | `src/kiro_crew/acp/client.py` |
| `excludedTools` for Tool Search | `src/kiro_crew/agent_sdk/tool_search.py` |
| the provider and backend axes | `src/kiro_crew/agent_sdk/provider_identity.py`, `src/kiro_crew/agent_sdk/backend_identity.py` |
| Template pane GET / PATCH | `src/kiro_crew/dashboard/handlers/agents.py` |
| fork / publish field handling | `src/kiro_crew/agent_capabilities.py` |

Skill resource mappings define the available set, not startup body injection. Crew
resolves the winning project/global spec for directory, search, list and exact read.
Native CLI execution uses a managed view without skill resources; the original spec
remains unchanged and supplies the mapping. Internal `kirocrew-skill-view-` specs
are omitted from Crew agent discovery. See [context management](../../../docs/architecture/context-management.md).
