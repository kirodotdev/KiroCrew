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
where it varies by harness `src/kiro_crew/providers/mirrors/` is. This page is a
pointer into the code, not a contract the code is held to — if the two disagree,
the code is right and this page is stale.

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
| claude-agent-acp harness | `acp_backend="claude"` | not as a spec file — Crew projects field by field | per `providers/mirrors/claude_code.py`: `mcpServers`, `model`, `availableModels`, `permissions.defaultMode` delivered; `tools`, `disabledTools` translated; `prompt`, `resources`, `autoApprove` withheld; `hooks` has no channel |

Three surfaces, not three backends. `src/kiro_crew/agent_sdk/backends.py` names
eight (`""`, `kas`, `claude`, `codex`, `opencode`, `pi`, `goose`, `deepseek`).
The five not in the table above receive **no agent definition in any shape** —
no `--agent`, and no spec written anywhere — so nothing reads a spec AS a spec
there, and this page's per-surface answers do not describe them.

Individual fields still reach some of them by another channel, which is a
different thing from reading the spec. `codex`, `opencode` and `goose` take
`mcpServers` as the `session/new` array, `tools` translated into the allowlist
deciding which servers enter it, and `model` as a session config option, while
`prompt`, `resources`, `autoApprove` and `availableModels` are withheld with a
reason each. `pi` and `deepseek` get no projection at all.
`src/kiro_crew/providers/mirrors/` holds the per-harness ruling and the
[agent host contract](../../../docs/system-specs/modules/agent-host-contract.md)
§1 is the table. Only `kas` reads the markdown form
(`ACP_BACKENDS_MARKDOWN_AGENT_SPECS`).

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
entry, so a hand-added `autoApprove` can persist. What removes one is governance —
`_strip_ungoverned_auto_approve` drops any the ceiling has not cleared, and the
withhold is recorded as a `mcp_auto_approve_withheld` security event.

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
workspace-relative, anchored three levels above the spec file
(`<project>/.kiro/agents/foo.json` → `<project>`). `agent_skill_globs` is what
the rest of the product asks.

`file://<glob>` is a steering glob, and a narrower mechanism than it looks:
`_load_steering_resources` in `src/kiro_crew/context.py` reads `resources` from
`kirocrew.json` specifically — not from the session's active agent — globs each
pattern against `$HOME`, and admits only `*.md` files that stay under the trust
base and are not sensitive locations.

Whether either is loaded at all depends on the backend, and `_skills_injection_plan`
is the single decision:

| Agent | `skill://` mapping | kiro-cli / KAS | Claude Code |
|---|---|---|---|
| `kirocrew` | none | whole catalog, injected by Crew | whole catalog, injected by Crew |
| `kirocrew` | mapped | nothing injected — kiro-cli loads them natively | mapped set only, injected by Crew |
| custom | none | nothing — the agent brings its own | nothing |
| custom | mapped | nothing injected — kiro-cli loads them natively | mapped set only, injected by Crew |

The `file://` steering block follows the same shape: injected only on the Claude
Code backend, and only for the default agent. On kiro-cli and KAS, injecting
would duplicate what the backend already loaded.

One more skill mapping exists and is edition-specific: a `builder-mcp` server
entry whose `args` carry `--skill-name-filter a,b`. `_extract_skills` unions it
with the `skill://` set for display. It predates `resources` support.

## Per-surface summary

Three columns, for the three surfaces a spec is READ by. The
`acp_backend="claude"` harness is not one of them — its fields are mirrored into
a different file in a different format, and
`src/kiro_crew/providers/mirrors/claude_code.py` is the per-field ruling for it.

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
| `resources` `skill://` | loaded natively | loaded natively | Crew injects the mapped set |
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
| `allowedTools` → `permissions` | `src/kiro_crew/acp/kas_permissions.py` |
| `--agent` on the spawn argv, the freshness gate | `src/kiro_crew/acp/client.py` |
| `excludedTools` for Tool Search | `src/kiro_crew/agent_sdk/tool_search.py` |
| the provider and backend axes | `src/kiro_crew/agent_sdk/provider_identity.py`, `src/kiro_crew/agent_sdk/backend_identity.py` |
| Template pane GET / PATCH | `src/kiro_crew/dashboard/handlers/agents.py` |
| fork / publish field handling | `src/kiro_crew/agent_capabilities.py` |
