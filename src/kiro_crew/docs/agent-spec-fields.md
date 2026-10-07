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
`claude_code.py` puts its deny rules in the settings file instead,
`opencode.py` hands its rules back as `harness_deny_rules` for the client to seed
into the harness's own `permission` config, and `goose.py` returns an empty set and
withholds the whole server, which `base.py` calls out on `SessionProjection` itself. A mirrored backend's
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
| `opencode` | `opencode.py` | `mcpServers`, no transport filter; `model` [5] | `tools` → the array's allowlist [3]; `disabledTools` → a `deny` rule in the seeded `permission` config, server kept mounted; withheld whole only when a lower config source outranks the rule [7]; `hooks` → fired by Crew's turn loop | as `codex` except `hooks`, with `permissions.defaultMode` fixed at `ask` and read back off the harness's own resolved config |
| `goose` | `goose.py` | `mcpServers`, the one channel measured as a round trip rather than as an accepted element; `model` [5] | `tools` → the array's allowlist [3]; `disabledTools` → the server is withheld whole, control plane included [7]; `hooks` → fired by Crew's turn loop | as `codex` except `hooks`, with `permissions.defaultMode` carried as `GOOSE_MODE` in the child's environment |
| `pi` | none | — | — | no projection at all — kind `no-channel` [8]; `model` and `prompt` still arrive [9] |
| `deepseek` | none | — | — | no projection of the spec — kind `broker-only` [8]; `model` and `prompt` still arrive [9] |

**An empty `tools` list is a second, stronger case of the "allowlist" row.** `tools:
[]` reads as "mount nothing" for the MCP array on every mirror, same as any other
allowlist -- but it does NOT, by itself, stop the harness's own native tools
(claude-agent-acp's Bash/Read/Write, for instance): those are outside the array
entirely. Every mirrored backend carries a second signal from the same spec
parse, `SessionMcpProjection.zero_tools` (true only for an explicit `[]`, not a
missing key), but only `opencode` and `goose` close the gap on it:
`AcpClient._deny_zero_tools` refuses EVERY permission request on such a session
-- MCP or native -- and the refusal runs only on `AcpClient` sessions of a
backend that answers `SessionCapabilities.honors_zero_tool_ban` (an enforced
routing, `tool_gate.ENFORCED_ROUTINGS`, plus a mirror). `claude`'s
routing (`Routing.SEEDED_SETTINGS`) is declared but not enforced by this core, so
an inherited `~/.claude` pre-approval can skip `session/request_permission`
entirely; the refusal is not applied there. `codex` is served by `AcpRuntime` /
`AcpSessionHandle` rather than `AcpClient`, which carries `spec_denied_tools`
but no `zero_tools` and no equivalent refusal, so nothing closes the gap on a
codex session either. `pi` and `deepseek` have no mirror, so neither this nor
the ordinary allowlist ever applies to them; an agent spec relying on
`tools: []` for any of claude, codex, pi or deepseek is not enforced at all.

The knowledge and research pool spawns zero-tool agents and does not rely on the
native ban alone. On kiro-cli, `--agent` resolves against the project checkout
before the user directory, so a project-level spec of the same name that carries
tools shadows the installed zero-tool one. At worker start the pool confirms the
ban (`AcpClient.effective_spec_declares_zero_tools`) and refuses to run when it
cannot. With a prepared projection the judged spec is the projected view the harness
consumed through `--agent`; the session prepares the view before the spawn and never
re-reads it, so a spec edit after the harness consumed it cannot change the answer.
The view is the one judged because the skill projection appends the skill-search tool
to a spec that carries `skill://` resources. With no prepared projection (the
`KIROCREW_NATIVE_SKILL_PROJECTION=0` rollback, or an alias that could not be taken)
the harness loads the named spec itself, so the authored spec, resolved project
checkout first (`AcpClient.authored_spec_declares_zero_tools`), is read before the
spawn and again after start, and both reads must declare `tools: []`. That bracket
narrows a change to the authored file but cannot close one made and restored inside
the spawn window, which is why a prepared view is preferred.

1. Delivered ONLY when Crew governs the session's permission surface
   (`_permission_surface_governed` in `acp/client.py`): Crew authored
   `<work_dir>/.claude/settings.local.json`, validated that file's current bytes
   as a byte-identical live sibling seed, or left a project-owned copy out of the
   session's setting sources. That file is this session's permission surface, and
   a tool Crew cannot gate is not handed to the session at all — so a project copy
   Crew neither validated nor excluded gets no Crew MCP tools rather than
   ungoverned ones. The array is otherwise the only
   channel Crew has onto this session's MCP surface, because the adapter reads no
   agent file; what the harness mounts from its own configuration sits beside it.
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
7. `codex` and `opencode` keep a narrowed CONTROL-PLANE server mounted;
   withholding `kirocrew-core` would leave the session unable to report back at
   all. `codex` refuses the call at the permission request
   (`AcpClient._deny_spec_disabled_tool`); `opencode` hides the tool with a
   `deny` rule in its own `permission` config. `goose` withholds it: a per-call
   refusal is reachable, but goose's own `permission.yaml` can pre-approve a tool
   so it never asks, and the agent can write that file mid-session.
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
`hooks` sits in that column on `claude` and `codex` without being a withhold:
the backend HAS the capability and this transport cannot carry it. Both run
hooks natively; what is missing is the delivery path. On `goose` and `opencode`
the ruling is `translated` instead: their session carries no hooks field either,
so Crew's own turn loop fires the field (`ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS`). `Ruling.__post_init__` refuses a
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
`settings-file` on `claude` and `opencode`, `per-call` on `codex`, `whole-server`
on `goose`. It is a declaration, not a requirement — what it owes you is knowing
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
- **A per-tool restriction can cost the whole server on `goose` and `codex`.**
  `per_tool_deny` on the projection record says which of three forms you get
  BEFORE a session runs. `settings-file` on `claude` and `opencode` keeps the
  server mounted and the harness refuses the tool; on `opencode`, a lower config
  source that already names the tool can outrank Crew's rule, and that server is
  withheld. `whole-server` on `goose` drops the server. `per-call` on `codex` is narrower than it sounds: it keeps the server mounted and refuses the call for Crew's own
  CONTROL PLANE only, whose tools carry no annotations so codex asks permission
  for every one of them. A narrowed third-party server is withheld whole there
  too, because codex approves a `readOnlyHint` tool internally without asking, so
  there is no permission request for Crew to refuse at. Read per-call as "the
  control plane survives", not "nothing is dropped".
- **`autoApprove` and a spec-written `permissions` block travel nowhere.**
  Deliberately, and this is the one degradation you want: every call reaches
  Crew's gate instead of being pre-approved inside the harness.
- **`hooks` reaches only two mirrors, and `resources` only half-reaches.**
  `hooks` is `translated` on `goose` and `opencode`, where Crew's turn loop fires
  it, and `no-channel` on `claude` and `codex`. Neither URI scheme is loaded natively, but a mapped
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
| `name` | str | The dispatch name. Outranks the filename: `spec_by_declared_name` resolves a spec whose declared `name` matches even when the file is called something else, which is how a package installs `<package>-<name>.json` and keeps the bare name. Two specs in one directory declaring the same `name` are refused, not arbitrated (`AmbiguousAgentSpecError`, raised by `spec_by_declared_name` and `agent.agent_spec_path` alike). One resolver reads the ambiguity differently: the spawn gate (`agent.require_fork_governance`) asks only whether the name is a private template copy, and since every duplicate declares the binding name, the lineage it reads is the same for all of them -- a verified non-fork is admitted with a warning naming each file, while a private copy with a same-named twin is still refused, because its refresh cannot pick a file to re-filter -- and so is a pair beside a `<name>.json` that declares a different, fork-backed name, since the backend also matches the file stem and could run that fork's file. A package installer that flattens dependencies writes exactly such twins for an agent two packages vend, so refusing them left the agent unstartable with no lasting repair. The tool-policy read (`api_session_tool_policy`) and the KAS projection (`kas_agents.load_agent_spec`) keep refusing the pair. The gate's two refusals also differ in wording: an unreadable `agent_model_state.json` names that file, an unresolvable spec names the agents directory. |
| `description` | str | Roster text, and projected on the KAS wire. A non-string reads as absent (`spec_str`) because the agents directory is shared with other tools and a structured value blanked the whole Agent Templates tab. |
| `welcomeMessage` | str | Rendered once into a new chat transcript, and projected on the KAS wire from the same reading, so the two surfaces cannot disagree. Read by `agent_welcome_message` only, which asks `list_agents` which spec is live rather than re-deciding, then truncates at `WELCOME_MESSAGE_MAX_CHARS`, with the ellipsis inside that budget rather than added to it. Whitespace-only collapses to nothing. Best-effort: an unreadable spec and an absent hint are the same answer. |
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

The managed default (`kirocrew`) spec does NOT point `prompt` at the
operating-contract template. `build_agent_config` and `_refresh_dynamic_fields`
set it to `_NATIVE_PROMPT_STUB` (`agent.py`), a short authority-delegating line.
The kiro-family backends deliver the spec prompt NATIVELY — kiro-cli reads the
file, KAS inlines it over the wire — while `context.py` injects the RESOLVED
operating contract on session start for EVERY backend. A `file://` template here
would deliver the persona twice — once natively (raw, with `{{…}}` placeholders
unresolved) and once through the resolved injection. The stub keeps the injection as the
single source, and being non-empty it satisfies KAS without hitting the
`_KAS_FALLBACK_PROMPT` fallback above. Custom and forked agents that set their
own `prompt` keep it — the stub is the managed default's value only. A fork or
template copy inherits the stub verbatim, and the fork heal in
`_refresh_dynamic_fields` rewrites a fork still carrying the managed `file://`
pointer to the stub. A capability-materialized owned member is the exception:
`agent_capabilities._maintain_owned` snapshots and restores its `prompt` around
that heal, so one still carrying the `file://` pointer keeps delivering the
persona natively — the same double delivery tracked in #13305. The two readers
that must not deliver the managed contract twice (member essentials and the
session-start load in `context.py`) recognise the stub and managed file URIs
through `is_managed_prompt`: essentials omit the contract, and the session-start load
resolves it to the current contract file for ANY spec carrying it — owner template,
fork or template copy alike — so a fork inheriting the managed contract is
delivered exactly once, resolved, via the injection. The stub text is frozen
once shipped: forks carry it verbatim on disk and `is_managed_prompt` matches
by equality, so a respelled stub would leave every existing fork with the old
stub text as a custom persona. An agent whose `prompt` names its OWN persona
file is out of scope: it still receives that persona both natively and through
the injection.

The reader also recognizes an older managed `file://` pointer under a Crew data
home or installed package. A desktop update or fallback gateway can leave that
URI in the saved spec while the running package lives elsewhere. It is still the
product contract, so member essentials must not try to read it as a project
document; the current contract is injected at session start instead. Custom
persona paths outside those managed locations retain their normal handling.

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
| `permissions` | object | KAS's own currency: `{"rules": [{"capability", "match", "effect"}]}`. Crew WRITES this derived from `allowedTools` (`derived_agent_permissions`) **only when the installed kiro-cli accepts the field** — `spec_permissions_supported(installed_kiro_cli_version())`, floor `SPEC_PERMISSIONS_MIN_VERSION` (2.23.0) in `kiro_cli.py`. kiro-cli validates specs with `deny_unknown_fields`, so a release below the floor refuses the WHOLE file and drops every Crew MCP server; an unknown version (no pinned binary, refused or unparseable `--version`) withholds too. The default spec's seed (`_seed_kas_permissions`) preserves an existing block before calling the shared gate (`_write_derived_permissions`), so a block already on disk is never removed by seeding; `kirocrew setup --agent-only --clean` repairs a default spec an older release already refuses. Generated conductor and worker writers use the same gate to replace an inherited value on an accepting release and remove it on an older or unknown one. Fork and publish (`_align_permissions` in `agent_capabilities.py`) route the TARGET block through the same `_write_derived_permissions`, so the published spec carries it only on an accepting kiro-cli and has it removed otherwise; the review of the SOURCE block described below runs whatever the version. `kirocrew doctor` prints the verdict as the KAS block's `auto-approve:` row. A block in the file is not an input to DERIVATION, which reads `allowedTools` and nothing else — but on the wire it is a second input, merged by `merge_user_permissions`: parsed against KAS's own shape and refused whole when it does not fit, `deny`/`ask` relayed unconditionally, `allow` relayed only where the same ceiling permits that capability and that resource, the shell and filesystem families never relayed as an `allow`, and an `allow` the `allowedTools` derivation could have emitted itself not relayed at all when the spec carries such a list — that list is the governed input and is re-derived every projection, so a block that has gone stale against it (including one the seeder wrote and preserved) cannot put a revoked grant back. Every decision, relay and withhold alike, is recorded in the security event log. Without that merge a pure-KAS agent — `permissions` authored, no `allowedTools` — reaches the backend with the field absent, and absent resolves every request to `ask`. On disk the block is left untouched and the backend reads it. One path does read it: fork and publish (`agent_capabilities.py`, `_align_permissions`) compares it against a fresh derivation and refuses with `alternate_permissions_require_review` when the two disagree, so a hand-edited block blocks forking that template. `src/kiro_crew/acp/kas_permissions.py` owns the translation, and refuses the shell and filesystem families outright: a tool-name allowlist carries no resource pattern, so the rule it would produce is unscoped. |
| `toolsSettings` | object | Per-tool settings kiro-cli reads. Crew strips exactly two retired keys on every refresh — `execute_bash`/`shell` `deniedCommands` and `autoAllowReadonly` — because denied commands are enforced only at Crew's PreToolUse gate now, and a stale copy in the spec would keep blocking a built-in the user just re-enabled (`_strip_legacy_denied_commands`). Your other keys are preserved. One key Crew READS: `subagent.availableAgents`, kiro-cli's glob allowlist of what this agent may spawn, is honoured at Crew's own `spawn_run` / `spawn_sub_agents` gate too (those bypass kiro-cli's built-in `subagent` tool) — declared, the target must match a glob; omitted, everything is allowed, as upstream. `subagent.trustedAgents` is a trust grant ("no permission prompts"), not an allowlist, and is not read as one. Details: `docs/system-specs/modules/subagent.md` § Parent agent spec allowlist. No KAS wire slot: a KAS session runs without it, and the chat shows one notice at session start saying so. |
| `hooks` | object | Event-keyed hook lists, stored camelCase (`preToolUse`, `postToolUse`, `userPromptSubmit`, `agentSpawn`, `stop`). Crew merges your `kiro_hooks` config and, when autoimport is on, scripts discovered under `~/.kiro/hooks`, capped per event by `_MAX_USER_HOOKS_PER_EVENT` and in total by `_MAX_TOTAL_USER_HOOKS`. This field is the object form only, which is kiro-cli's own schema. The SOURCE that feeds it, `agent.kiro_hooks` in `~/.kiro/crew/config.json`, additionally accepts a kiro-agent hooks ARRAY — `{name, description?, trigger, matcher?, action, timeout?, enabled?, confirm?}` documents, on the twelve trigger names of kiro-agent's alias table in any of its spellings — and projects it onto this object: only a `command` action on the five triggers kiro-cli names is emitted, so an `agent` action, the other seven triggers and the per-hook `name`/`description`/`timeout` stay in your config without running there, and `enabled: false` or `confirm: true` keeps the hook out of the emission entirely (and out of the autoimport scan). See `steering-and-hooks.md` for that field. No KAS wire slot, so `UNSUPPORTED_SPEC_KEYS` drops the key from the agent Crew injects over the wire. On KAS, Crew's own turn loop fires this field instead (`agent_sdk/spec_hooks.py`, gated on `SessionCapabilities.crew_fires_spec_hooks`), through the same hook-store path as a Hooks-page hook: the `capabilities.script_hooks` gate, the sandboxed spawn, the timeout, and a PreToolUse exit 2 that blocks the tool. A PreToolUse hook runs only on a call that reaches Crew as a permission request, so the KAS projection adds an `ask` rule for every auto-approvable capability that such a hook's matcher covers, this spec's or a Hooks-page one's (`kas_permissions.withhold_hook_gated_auto_approval`). KAS lets `ask` outrank `allow`, so the call comes back as a permission request and the hook sees it. A live session keeps the batch it registered, so when a hook added later covers a capability that batch auto-approves, the next turn resets the session and the claim registers a fresh batch (`spec_hooks.invalidate_stale_kas_session` before the claim, and `spec_hooks.reproject_claimed_session` again under the claimed lease, so a turn that waited behind a busy session is not run on its stale batch). A mode switch mid-turn activates a batch KAS already holds, so when the agent it moves to auto-approves what its PreToolUse hooks cover, the turn is stopped and the next one starts a fresh session projected for that agent (`spec_hooks.refuse_stale_switch`). A switch to one of KAS's own built-in modes, which has no spec on disk, carries no spec hooks: only the Hooks page's hooks are checked, and the refusal runs before the switch is recorded on the chat slot. A process that registers no hook store (the standalone `kirocrew run` task runner) reads the Hooks page's saved hooks from disk for both the projection and the gate (`hooks.persisted_hook_store`), so they still apply there. A `confirm: true` document does not run, since Crew cannot ask for the confirmation, and a new session gets one notice naming how many were skipped. When its own backend is KAS, a subagent or task-runner turn runs its PreToolUse hooks (the Hooks page's and its own agent's spec hooks) on each permission request and refuses the call on a deny, and does not fire them again on the tool-call event (KAS sends that frame first); a subagent passes the spec hooks to PostToolUse too. A tool `matcher` keeps kiro-cli's tool names (`execute_bash`, `fs_write`, and the `shell`/`read`/`write` aliases): on a PreToolUse Crew compares it with the id KAS states for the call and the kiro-cli names that id stands for (`acp/kas_permissions.KAS_TOOL_IDS_BY_KIRO_TOOL`, so `execute_bash` meets KAS's `run_command`), never with the call's title, and the hook's stdin reports that kiro-cli name as `tool_name`. A PreToolUse matcher the table does not know (`use_aws`, `disclose_context`) is kept and warned about: it meets a KAS id it matches as written. A call KAS names no tool for is matched on its title, as a Hooks-page hook is. A PostToolUse still matches the title, since KAS's tool-call frames carry no tool id, and a tool-scoped one is warned about when the spec is read. goose and opencode get the same treatment: their session carries no hooks field either, and every tool call arrives as a permission request, so Crew fires the field there too. A tool matcher meets the tool name the harness states on the call's first `tool_call` frame (goose's `_meta.goose.toolCall.toolName`, opencode's `title`), mapped back to kiro-cli's names (`acp/harness_tool_names.py`, so `execute_bash` meets goose's `shell` and opencode's `bash`). An `mcp__server__tool` matcher meets an MCP call too: goose names the server itself, and opencode's fused `<server>_<tool>` title is split against the servers Crew placed on the session and the ones opencode's own config mounts (which Crew reads back when the session starts, so `mcp__docs.server__lookup` meets a `docs.server` tool even though opencode writes it `docs_server_lookup`; a config with more than 256 such rewritten names, or a name over 256 characters, refuses the session rather than leave one out), and also at every `_` for a server neither names; every split is kept. When the fused title is also one of opencode's own tools (`apply_patch`), that tool's names are kept too and its kiro-cli name stays first, so the hook's stdin still reports `fs_write` as `tool_name`. On these backends and KAS a matcher may also be written `@server/tool`, the form Crew builds for an MCP call from the server name the harness states: one literal server name (no glob, so `@*/tool` is refused) and a tool part in the matcher's own characters, `*` included (`@server/*`). Only this conversion accepts it (`spec_hooks._MCP_MATCHER_RE`); the merge that writes kiro-cli's materialized spec keeps its own rule. On those two backends the spec is read from the checkout's `.kiro/agents` first, then the user level, the same order their MCP projection resolves it in, but a project agent's hooks run only when that checkout is trusted to choose its MCP servers (`acp/session_mcp.py`, `_project_mcp_trusted`), and otherwise the user-level spec of that name supplies the hooks; a project spec that cannot be read blocks every tool call. KAS reads the user level alone. As on KAS, this is the chat, subagent and task-runner turn loops: a channel-agent turn (`channel.py`) runs no script hooks on any backend. claude and codex do not run the field yet: each approves some calls inside the harness without asking, and a PreToolUse hook would be skipped on those. kiro-cli runs the field itself, so Crew never fires it there. |
| `slashCommand` | any | No KAS wire slot: a KAS session runs without it, and the chat shows one notice at session start saying so. Nothing else in this tree reads it. |
| `toolAliases` | object | Maps a Connections-exposed MCP tool to the short name a `@alias` reference in `tools` / `allowedTools` resolves through. On the default spec's rebuild Crew recomputes it from the connector registry, dropping the pairs its own record proves it wrote and keeping the rest, whose authorship is unproven and therefore yours (`agent_materialization/mcp_aliases.py`, `_reconcile_tool_aliases_from_disk`). It reconciles the path it is given, so a spec that rebuild does not touch keeps whatever it holds. A non-dict value there is replaced rather than merged. |
| `managedToolPolicy` | object | Which of Crew's managed tools an agent may NOT reach. Read per session by the dashboard (`dashboard/handlers/sessions.py`), which treats a non-object as unreadable rather than absent — the operator wrote something and its meaning is unknown. On an app agent this is CONTAINMENT, not preference, so the framework owns it and a rebuild overwrites it (`apps/bridges.py`). |

### MCP servers

| Field | Type | Effect |
|---|---|---|
| `mcpServers` | object | Name → server entry. The keys become the roster's server chips (`_mcp_server_names`). A local entry may carry `command`, `args`, `type`, `env`, `timeout`, `disabled`, `disabledTools` and `autoApprove`. |
| `includeMcpJson` | bool | Whether the backend also loads its global `mcp.json`. Crew's own generated specs pin `false` (shipped in `defaults.json` and re-pinned by `_refresh_dynamic_fields`), so for those a spec's own `mcpServers` is the complete set. Absent, kiro-cli reads it as `true` and KAS's own disk schema as `false`, so Crew projects it only when the spec states a bool and synthesizes no default for either host — KAS's wire schema has none, and its tool filter resolves an absent flag to `false` itself. |
| `includePowers` | bool | Whether the backend also unions in its Powers tools. Same shape and same absent-default rule as `includeMcpJson`. It widens which tools are VISIBLE, not which are auto-approved: a tool it reveals still has no `permissions` rule, so KAS resolves the call to `ask`. |

`autoApprove` inside an `mcpServers` entry is the SECOND way a call skips the
gate, and a more direct one: kiro-cli approves an auto-approved MCP tool locally
and emits no permission request at all, so Crew's callback never runs. Crew's own
managed server entries ship without an `autoApprove` key, and Crew's own writers
are barred from adding one. That is a rule on the writers, not a property of the
file: the managed-server refresh preserves user customizations on an existing
entry, so a hand-added `autoApprove` reaches the map and, by default, survives.
What removes one is governance — `_strip_ungoverned_auto_approve` drops any the
ceiling has not cleared, and drops any verb no server spec declares once the
operator sets `mcp.honour_auto_approve` to `false`. A verb a managed or edition
spec declares is kept on an ungoverned host either way. Both decisions are
recorded as security events: `mcp_auto_approve_withheld` for a grant taken away,
`mcp_auto_approve_honoured` for an owner-written one kept.

On the KAS wire, `env` and `headers` are withheld from every entry — `env`
routinely holds tokens, and a remote entry's `headers` can hold a static
`Authorization`. Two `env` exceptions. A value that is exactly `${NAME}`
survives on any server when the edition lists `NAME` through
`McpToolingProvider.kas_relayed_env_references()` (the public build lists none);
KAS resolves it at spawn, so only the name crosses the wire. Any other reference
is withheld, because KAS's own environment holds credentials such as
`KIRO_API_KEY`. And `KIROCREW_HOME` survives for Crew's own managed servers,
because it pins the data home — but only as a literal containing no `${`, since
KAS would expand a reference under that name too. That recorded pin is also
**write provenance**: the shared-home write guard reads it back from every managed
entry to decide whether the shared `~/.kiro/agents` specs belong to this
instance — specs pinned by a different home (or by nobody, or with disagreeing
pins) refuse the rewrite.

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
| `resources` | list | Two unrelated URI schemes and one object form in one list. |

`skill://<glob>` maps skills to the agent. `skill_resource_uris` reads them in
order and `expand_skill_uri` turns each into an fnmatch glob over real paths:
`~/...` against your home, `/abs/...` verbatim, and anything else
workspace-relative. A relative glob anchors at the `project_dir` the caller
supplies — the session's own project, on every path that resolves skills for a
prompt — and only with no project supplied does it fall back to three levels above
the spec file (`<project>/.kiro/agents/foo.json` → `<project>`).
`agent_skill_globs` is what the rest of the product asks.

When a builtin skill moves (`_RELOCATED_SKILLS` in `skills.py`), its old
`SKILL.md` is set aside, so a mapping of the old path would load nothing.
`agent.migrate_relocated_skill_uris` runs on every agent-config rebuild and
rewrites such a `~/` or absolute `skill://` entry in each JSON spec under
`~/.kiro/agents/` to the new path, keeping its place and its `~/` form. It
drops the entry instead when the spec already maps the new path, and acts only
once the new `SKILL.md` exists and the old one is gone. Workspace-relative
entries, wildcards and markdown specs are left alone, and so is an enrolled
member's spec: it is a saved capability generation whose digest the intent
records, so it picks up the new path when the member is re-saved in
Capabilities.

`file://<glob>` is a steering glob, and a narrower mechanism than it looks:
`_load_steering_resources` in `src/kiro_crew/context.py` reads `resources` from
`kirocrew.json` specifically — not from the session's active agent — globs each
pattern against `$HOME`, and admits only `*.md` files that stay under the trust
base and are not sensitive locations.

A knowledge base is the one entry that is an **object**, not a URI: kiro-cli
documents `{"type": "knowledgeBase", "source": "file://./docs", "name": ...}` in
the same list and no string spelling exists for it. This reader checks shape
only — a URI string or an object, the contract kiro-cli itself reports as
`resource must be a string (file:// or skill://) or an object` — and leaves the
keys inside to the backend. Crew never reads the entry: no document, no budget
spent, no path derived, so the admitted roots are unchanged.
`to_client_custom_agent` projects string entries only, so the object never
reaches the KAS wire. `agent_capabilities._rows` still requires strings, because
each entry is a catalog key: a member enrolled in capability management cannot
carry the object until that model represents it.

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
| `prompt` | read from disk | inlined over the wire | Crew injects it at session start; the built-in agent gets the Kiro Crew persona |
| `model` | honoured, `"auto"` resolvable | not projected | `cc_model` sidecar instead |
| `tools` | honoured | wire field; absent means NO tools | roster only |
| `allowedTools` | honoured | translated to `permissions` | not read |
| `excludedTools` | honoured | wire field | read by Tool Search only |
| `permissions` | ignored (kiro-cli field set); refused below 2.23.0, so not written there | Crew-derived, plus the author's own block intersected with the ceiling | not read |
| `mcpServers` | honoured | projected, minus `env` / `headers` | session array instead |
| `includeMcpJson` | honoured; absent reads as `true` | wire field when the spec states a bool; no default synthesized | not read |
| `includePowers` | not read | wire field when the spec states a bool | not read |
| `resources` `skill://` | Crew injects the mapped set; the native launch view carries no `skill://` | forwarded on the wire, and Crew injects the mapped set | Crew injects the mapped set |
| `resources` `file://` | loaded natively | loaded natively | Crew injects, from `kirocrew.json` only |
| `hooks` | honoured | dropped from the wire projection; fired by Crew's turn loop | Crew's gate, renamed events |
| `toolsSettings` | honoured | no wire slot; session-start notice | not read |
| `toolAliases` | honoured | no wire slot | not read |
| `managedToolPolicy` | Crew-side, per session | Crew-side, per session | Crew-side, per session |
| `welcomeMessage` | Crew-only (chat transcript) | chat transcript, and a wire field from the same capped reading | Crew-only (chat transcript) |

## Ownership and refresh

Kiro Crew owns and rewrites these twelve filenames in `~/.kiro/agents/`
(`src/kiro_crew/agent_files.py`, `OWNED_KIRO_AGENT_FILES`); this table is that
list's one copy in the docs.

A spec that is neither one of those twelve nor generated by an app is yours — which
is still not untouched. The Template pane's PATCH writes `model` and the `skills`
mapping onto an unmanaged template, and only those two (a markdown spec is
refused outright). The mapping lands element-wise on the spec as re-read under
the lock: only the `skill://` URIs the request adds, removes or reorders move,
and a reorder stays within the managed `skill://` entries' own slots, so a
`file://` glob you interleaved between two skills keeps its place. No other
field of yours is written: the `toolAliases` recompute below runs on the
default spec's rebuild, not over your templates.

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
| `kirocrew-guest.json` | generated | every gateway start (the tool-less agent a non-operator channel sender talks to) |
| `kirocrew-worker.json` | DERIVED from `kirocrew.json` | every gateway start, and re-checked before every worker session |
| `kirocrew-dashboard-author.json` | generated | every gateway start (if a file of this name already exists that is NOT this installer's own — its bytes do not reproduce the ownership digest the installer recorded in the `agent_model_state.json` sidecar for its last managed write, or its read fails — it is left **untouched** and the install is **skipped** — no backup is written and the agent stays unselectable under this name until you remove or rename the file) |
| `kirocrew-conductor.json` | generated; your `allowedTools` entries are carried forward | every gateway start |
| `kirocrew-ledger-conductor.json` | generated; your `allowedTools` entries are carried forward | every gateway start |
| `kirocrew-pipeline-conductor.json` | generated; your `allowedTools` entries are carried forward | every gateway start |
| `kirocrew-security-conductor.json` | generated; your `allowedTools` entries are carried forward | every gateway start |
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

On the backends that receive MCP servers as a `session/new` array (Claude Code,
Codex, OpenCode, Goose), a project spec never chooses the servers: each one is a
command the adapter launches at session start, so a cloned repository could
otherwise run its own code. Only the switch-off keys of the project spec's
`mcpServers` apply (`disabledTools`, and `disabled` when it mutes), layered onto
the user-level spec of the same name, or kept on a project-only spec whose other
fields still apply (`acp/session_mcp.py`, `_project_mcp_trusted`).

The worker spec is the one with a freshness contract, because it is a mirror.
`_write_worker_spec` copies `tools`, `allowedTools`, `excludedTools`,
`mcpServers` and `model` from the default spec **on disk** (not from the template
it was assembled from), adds `@kirocrew-work` plus its two grants, subtracts cron
scheduling and any opt-in server nobody assigned, and derives `permissions` from
the filtered result through the same version gate the other writers use
(`_write_derived_permissions`; see the `permissions` row). `_require_fresh_worker_spec` then runs before every worker
spawn and has no early `return` by design: it re-derives a stale mirror and
refuses the dispatch when it cannot, rather than starting a worker on grants the
default agent no longer has. Both the write and that freshness check first
attribute the file at the mirror path: a `kirocrew-worker.json` that lacks the
derivation's provenance marks (the declared `name` `kirocrew-worker` and a
reference to the `kirocrew-work` server) is refused with `ForeignAgentSpec`
rather than re-derived, and the dispatch is declined; a file that does not parse
as a spec object is still replaced. A project checkout shipping its own
`kirocrew-worker` spec is refused outright — kiro-cli would resolve that file
first, and Crew will neither rewrite a repository's tracked content nor honour
it.

The four conductor specs are rewritten on every rebuild from their grant tuples,
and one field of yours survives that: `allowedTools`. The installer reads the
spec it is about to replace and carries your entries forward after the shipped
grants, through the same governance ceiling the shipped grants pass
(`agent_materialization/conductor_agents.py`, `_governed_grants`), so an entry
you approved on `kirocrew-conductor.json` is still approved at the next start
while an entry the ceiling forbids is still removed. What tells your entry apart
from the installer's own is the history of every grant any release of Kiro Crew
has ever shipped on that spec (`_SHIPPED_GRANT_HISTORY`, a table in the code
that a test keeps complete): an entry on disk that history names is Crew's and
comes back or goes with the current release — so a grant one release shipped and
the next one stopped shipping is dropped rather than kept as yours — and an entry
no release ever shipped is yours and stays. That holds for any file at the
spec's name, whichever release or install wrote it, so an upgrade, a downgrade
and re-upgrade, or a copied agents directory never costs you your entries. The
one thing it cannot tell apart: a grant you add by hand that Crew once shipped
and has since retired reads as Crew's and is dropped at the next start — named
in the warning, recorded as a revoked auto-approval, and the tool asks instead.
`kirocrew setup --agent-only --clean` drops your entries, as it drops every
customization of `kirocrew.json`. A shipped grant you deleted comes back —
narrow a conductor through the governance ceiling, not by editing the generated
list — and nothing on the list moves silently: one warning in the gateway log
names every entry a rebuild did not carry forward, a drop the ceiling did not
make is recorded as a revoked auto-approval in the security event log, and an
entry of yours that was carried forward is recorded there as a retained one. One
shape of yours is never carried forward: a wildcard (`*`, `fs_*`, `session_*`,
`@*`, `execute_?ash`, a `[...]` class). The ceiling and the always-on PreToolUse
floor judge a tool by its exact name, so the pattern itself would pass them —
while kiro-cli expands it, on the one path that skips the gate, over every
builtin and every verb of every MCP server the spec mounts. The builtins could be
checked one by one; a server's verbs cannot, because kiro-cli discovers them when
it connects, not when the spec is written — so a kept `session_*` or `@*` would
hand back a verb the ceiling withholds by name, and no check at rebuild time could
see it. Every pattern is therefore dropped, named in the warning with the remedy:
approve the tools you want by their exact names (`web_fetch`,
`@kirocrew-dashboard/session_send`), which the gate judges and which stay. A
spec that is present but cannot be read is handled by what the failure is. Bytes
that can never be a spec — a JSON typo in your edit, an `allowedTools` that is
not a list, a file past the read cap — are written over with a fresh spec, with
one warning naming the cause and that nothing on the file was carried forward:
kiro-cli could not have loaded that file either, so keeping it would have left
you without the conductor (a session that names it would run the default agent
instead) and left the ceiling with nothing to re-filter. A spec with a second
hard link on it (a hardlink-based dotfile layout) is written over too, for the
opposite reason: Kiro Crew's hardened reader refuses a multiply-linked file for
good while kiro-cli loads it as it is, so leaving it in place would leave the one
list kiro-cli reads out of the ceiling's reach. The rewrite replaces the name in
the agents directory with a fresh file and never writes through the shared
inode, so your other copy keeps its bytes; to carry your own entries across
rebuilds, keep the spec a single-link regular file. A read that failed for a
reason that may not recur — a permission or I/O error, or a metadata probe that
failed before any byte was read (a stale network-filesystem handle) — is tried
again, three reads a fraction of a second apart, before anything else happens; a
failure that outlasts them leaves your file exactly where it is, with one warning
naming the cause and the attempts: repair it, or run
`kirocrew setup --agent-only --clean` to rebuild it. A spec left that way at
gateway start is retried without waiting for the next ceiling change: by the
first governance poll where central policy distribution is configured, and on
every host by the gateway's hourly maintenance wake, which rewrites it once it
can be read and warns again while it cannot.
(A link planted at the spec's name is written over the same way, with the
warning saying nothing behind it was carried: that is what puts a loadable spec
back.)

What you may safely hand-edit: a spec you authored yourself; a conductor spec's
`allowedTools`; and in any other owned or app-generated field, nothing — change
`~/.kiro/crew/agent.json` or the Template pane instead.

## Markdown form and the Template pane

Frontmatter keys map one-to-one onto the JSON fields above; nesting works
(`mcpServers`, `permissions`). What differs is who may write the file.

| Field | Built from pane (Customize → Crewmates) |
|---|---|
| `model` | editable |
| `resources` `skill://` entries | editable, via the Skills section |
| `resources` `file://` entries | read-only, preserved across edits |
| `skill://` wildcards and paths outside known skill roots | read-only, preserved |
| everything else | read-only — the pane renders it, the PATCH ignores it |

`PATCH /api/agents/detail/{name}` recognizes exactly two body keys, `model` and
`skills` (`src/kiro_crew/dashboard/agent_admin/agent_detail.py`, `api_agent_detail`).
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
| `tools`, `allowedTools`, `excludedTools`, `mcpServers`, `hooks`, `toolsSettings` — the default spec's composition and the rebuild order | `src/kiro_crew/agent.py` (`build_agent_config`, `_refresh_dynamic_fields`, `rebuild_agent_config`), the facade every writer below is reached through |
| `hooks` — normalization and the kiro-cli object form | `src/kiro_crew/agent_materialization/kiro_hooks.py` |
| `mcpServers` — Crew's own managed servers | `src/kiro_crew/agent_materialization/managed_mcp.py` |
| `mcpServers`, `tools`, `allowedTools` — app, global and store servers, and the shared-server sync | `src/kiro_crew/agent_materialization/mcp_sources.py` |
| `mcpServers` keys, `toolAliases` | `src/kiro_crew/agent_materialization/mcp_aliases.py` |
| `allowedTools`, `autoApprove`, `permissions` — the governance ceiling | `src/kiro_crew/agent_materialization/auto_approve.py` |
| the default spec's locked write | `src/kiro_crew/agent_materialization/default_spec_commit.py` |
| derived specs — lite, guest, knowledge, research; the conductors; the worker | `src/kiro_crew/agent_materialization/service_agents.py`, `conductor_agents.py`, `worker_agent.py`; their prompts and grant tuples, and the heartbeat installer, stay in `src/kiro_crew/agent.py` |
| the fork refresh | `src/kiro_crew/agent_materialization/fork_refresh.py` |
| owned filenames | `src/kiro_crew/agent_files.py` |
| `model_managed`, `cc_model`, fork lineage | `src/kiro_crew/agent_state.py` |
| the KAS wire projection | `src/kiro_crew/acp/kas_agents.py` |
| the per-harness field rulings, and which backend has a mirror at all | `src/kiro_crew/providers/mirrors/` (`base.py`, `registry.py`, `identity.py`, `claude_code.py`, `codex.py`, `opencode.py`, `goose.py`) |
| where a mirror runs at session establishment | `src/kiro_crew/acp/runtime.py` (`AcpRuntime._mirrored_session_mcp`), `src/kiro_crew/acp/client.py` (`AcpClient._resolve_session_mcp_servers`) |
| `allowedTools` → `permissions` | `src/kiro_crew/acp/kas_permissions.py` |
| `--agent` on the spawn argv, the freshness gate | `src/kiro_crew/acp/client.py` |
| `excludedTools` for Tool Search | `src/kiro_crew/agent_sdk/tool_search.py` |
| the provider and backend axes | `src/kiro_crew/agent_sdk/provider_identity.py`, `src/kiro_crew/agent_sdk/backend_identity.py` |
| Template pane GET / PATCH | `src/kiro_crew/dashboard/agent_admin/agent_detail.py` |
| fork / publish field handling | `src/kiro_crew/agent_capabilities.py` |

Skill resource mappings define the available set, not startup body injection. Crew
resolves the winning project/global spec for directory, search, list and exact read.
Native CLI execution uses a managed view without skill resources; the original spec
remains unchanged and supplies the mapping. Internal `kirocrew-skill-view-` specs
are omitted from Crew agent discovery, and from the stat-only fingerprints its
roster and spec caches revalidate on, so an alias write cannot invalidate them
(`agent_spec_format.is_native_skill_alias_name` is the one predicate both apply).
See [context management](../../../docs/architecture/context-management.md).
