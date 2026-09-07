# Claude Code provider — a selectable ACP harness

## Public provider boundary

`AgentConfig.provider` admits only the ACP provider, and
`KiroCrewConfig.create_provider_factory()` constructs `AcpProvider`. Harness
choice is a separate field, `agent.acp_backend`, and the public build now offers
every harness it knows: `acp_backends.BASELINE_SELECTABLE_BACKENDS` contains
`ACP_BACKEND_KIRO` (the empty string), `ACP_BACKEND_CLAUDE` and
`ACP_BACKEND_KAS` — i.e. all of `ACP_BACKENDS_KNOWN`.
`test_baseline_ships_every_known_backend` pins that equality.

`DefaultProviderRegistry` therefore registers no extra backend: there is nothing
left in `ACP_BACKENDS_KNOWN` to add. `register_selectable_backend` stays because
the `ProviderRegistry` protocol declares the hook and an edition overrides it, but
it is **not** an extension point for a harness the core does not ship: it rejects
any id outside `ACP_BACKENDS_KNOWN`, and every id inside that set is now already
selectable. Adding a genuinely new harness therefore means widening
`ACP_BACKENDS_KNOWN` — a core edit — not just calling the register. What the hook
does buy an edition is reach once the id is known: it lands in the config gate, the
dashboard PATCH allowlist and `GET /api/config/schema` together
(`test_a_registered_backend_reaches_the_allowlist`,
`test_a_registered_backend_reaches_the_schema_endpoint`).

`acp_backends.resolve_selected_backend()` normalizes an `agent.acp_backend` value
this deployment cannot select to the Kiro harness. This boundary is load-bearing:
`AcpProvider` rejects unknown harnesses, so normalization prevents a persisted or
hand-edited value from becoming a startup failure.
`TestConfigRoundTrip.test_unselectable_values_degrade_to_the_default` exercises
that path, and `test_harness_parity.test_unselectable_backend_degrades_to_kiro`
asserts the outcome against the live registry rather than a hardcoded verdict —
which is why `claude` now *survives* that gate instead of degrading.

Selectable is not the same as usable, and it is not the same as permitted.
Whether a *deployment* may pick a registered harness is answered by the
`agent_backend` governance scope narrowing the registry
(`apply_selectable_denials`, floored at `GOVERNANCE_FLOOR_BACKEND` = kiro-cli).
Whether a *machine* can run it is answered by `agent_sdk.probe_backend`.

## The Claude harness

`acp/client.py` owns the whole Claude spawn path, and it is a live path on a
plain public build:

- `AcpClient._is_claude` recognizes `ACP_BACKEND_CLAUDE`, and `AcpClient._spawn`
  takes the adapter branch for it.
- `_resolve_claude_acp_bin()` finds the `claude-agent-acp` Node entry script and
  returns `(argv, searched_path)`; the result is memoized process-wide in
  `_claude_acp_argv_cache`, so the search runs once and the "not found" message
  names exactly the directories that were searched.
- `_resolve_claude_code_executable()` finds the `claude` CLI and `_spawn` exports
  it as `CLAUDE_CODE_EXECUTABLE` when the caller has not set one. The adapter
  forwards it to `@anthropic-ai/claude-agent-sdk` as
  `pathToClaudeCodeExecutable`; without it the SDK fails `session/new` with
  "Claude native binary not found", because it does not search `PATH` for
  `claude` on its own.
- The adapter is a **public** npm package, `CLAUDE_ACP_NPM_PKG =
  "@agentclientprotocol/claude-agent-acp"`. Nothing on this path is
  edition-private.

Availability is therefore a property of the operator's machine, not of the build:
Claude Code needs **two** locally-installed binaries, the `claude-agent-acp`
adapter and the `claude` CLI. `agent_sdk.backend_install._probe_claude` reports
which of the two is absent (`COMPONENT_CLAUDE_ACP_ADAPTER`,
`COMPONENT_CLAUDE_CODE_CLI`) plus the command that installs the adapter, so a
half-install does not read as a total one. A probe that itself fails reads
`UNKNOWN`, never `MISSING`.

Both operator-facing surfaces read that one verdict:

- `kirocrew doctor` prints Claude Code as an optional backend — present, absent
  with the missing components named, or uncheckable. It is never a hard failure;
  kiro-cli is the floor.
- The dashboard's agent-backend control **hides** a harness the deployment may
  not select instead of dimming it, because under a managed policy there is
  nothing the reader can do about it and advertising a forbidden option is the
  opposite of what a restriction is for. The currently-selected value is always
  kept visible. A rendered-but-disabled row therefore always names something the
  user can act on: install a binary, or restart the gateway. Covered by
  `hides a backend the deployment may not select, rather than dimming it`,
  `keeps the selected backend visible even if it reads as unselectable` and
  `saves the Claude Code selection the shipped build offers`.

### What Crew gates on this harness, and what a pre-approval skips

Start from what is NOT broken, because the difference is narrow and easy to overstate.

**By default, Claude asks and Crew decides.** In `default` permission mode with no
matching rule, every tool call reaches the SDK's `canUseTool` callback — which is
exactly what `claude-agent-acp` turns into ACP `session/request_permission`. That
arrives as a `permission_request` event and runs Crew's own approval path:
`hooks.on_tool_call`, its deny rules, its sensitive-path and write-protected-config
checks, and its SEL decision record. A Claude session is governed like any other on
that path.

**What escapes is a call that was already pre-approved, because it never asks.** The
SDK evaluates permissions in a fixed order and `allow` rules sit at step 5, ahead of
the callback at step 6. Anthropic's documentation states the consequence in bold:
*"Auto-approved tools never reach `canUseTool`."* No callback means no ACP request,
so for that specific call there is nothing for Crew to gate or record. The same holds
for `bypassPermissions` and for `acceptEdits` on the operations it covers.

**Why that matters here rather than being purely the operator's own choice.** Those
rules do not have to come from the operator. The SDK reads `.claude/settings.json`
from the **project directory** — the `project` setting source is enabled for default
options — so a cloned repository can carry allow rules its author wrote. Crew's public
core passes nothing that would change this: no permission mode
(`AcpClient._permission_mode` is stored and never read), no `settingSources`
restriction, no `PreToolUse` hook, and no settings seed.

This is documented, intended Claude Code behaviour, not a defect introduced by making
the harness selectable — the harness was already implemented and reachable by any
edition that registered it. But it means the guarantee differs per harness, so the
dashboard states the difference on the Claude row
(`claude_uses_its_own_permissions`) rather than leaving an operator to discover it
from a shell command that never asked. Kiro CLI and KAS have no equivalent
settings file that can pre-approve past Crew's gate, and deliberately carry no such
line.

Anthropic documents two mechanisms that would close even the pre-approved case — a
`PreToolUse` hook, which runs before every other step and whose deny holds even in
`bypassPermissions` mode, and excluding `project` from `settingSources`, which stops
the untrusted copy being read at all. Whether `claude-agent-acp` forwards either over
ACP is not answered in this repository, and is the prerequisite for Crew gating
*every* Claude tool call rather than every call Claude asks about.

### MCP tools on a Claude session

The `claude-agent-acp` adapter reads **no** agent file: the `mcpServers` array on
`session/new` / `session/load` is the entire MCP surface a Claude session has, and
an empty one means zero Crew tools — `kirocrew-core`, cron and every
user-configured server absent, while the harness itself (prompts, streaming, model
and effort selection, the full `session/request_permission` flow) works. So
`_session_mcp_servers()` fills it, from `acp/session_mcp.py`:

- **The materialized kiro agent spec is the source.** `~/.kiro/agents/<name>.json`
  is already the merge point for the dashboard's Kiro Crew scope
  (`~/.kiro/crew/mcp.json`) and kiro's global `mcp.json`, so a user-installed
  server needs nothing CC-specific. There is no second, CC-shaped registry to keep
  in sync, and the spec is read **per spawn** — installing or toggling a server
  takes effect on the next session, with no gateway restart. The read goes through
  `agent_discovery._read_agent_spec`, the repo's one hardened reader, labelled
  `operation="session_mcp_servers"`: the agents directory is user-writable and
  shared with other tools, so a symlink whose resolved target is sensitive is
  refused and audited, and an oversized file is refused at the size cap rather
  than being read into memory mid-spawn.
- **`tools` references decide what mounts.** kiro-cli mounts a server only when
  `tools` names it (`@server` or `@server/tool`); the CC array has no such
  indirection, so the reference is applied during translation. Without that, an
  entry deliberately left unreferenced — the shape every `opt_in` grant uses, and
  what a hand-narrowed spec looks like — would come alive the moment a session ran
  on CC.
- **kiro-cli's registry filter is SYMMETRIC, and only one half of it is
  reproducible here.** Outside registry mode kiro-cli drops the entries that
  *carry* `type: "registry"`, and that half is mirrored exactly: the marker is on
  the entry, so the decision needs nothing else. Inside registry mode kiro-cli
  resolves each marked entry against the **admin's catalog** by map key, drops
  what the catalog omits, and applies the catalog's own command override — and
  none of that is available here, because only kiro-cli fetches the registry URL
  and it persists neither the URL nor the catalog. A marked entry therefore
  cannot be positively *authorized* on this backend, and a `"type": "registry"`
  line is one any user can add to their own spec, so in registry mode marked
  entries are **withheld** rather than launched. The unmarked ones are dropped for
  the reason they always were, which leaves a governed install with nothing from
  the spec at all on this harness.
- **Crew's own control plane is exempt from that withholding**, because it is
  re-derived from `agent.managed_mcp_spec_entry` rather than read from the
  user-editable spec: it is the host's own process, and withholding it would leave
  the session unable to report back to its channel — the exact defect this seam
  exists to fix, on precisely the installs that are most governed. The residual
  difference from kiro-cli is stated for what it is: an administrator who omits
  `kirocrew-core` from the catalog has it dropped there and kept here (one
  host-owned server wider), while every third-party server goes the other way.
- **A server the MCP gateway brokers yields to its stub.** The pooled broker
  emits a stub under the SAME name as the agent-spec entry it rewrites, and the
  caller appends those stubs to this array. Emitting both would put two elements
  with one `name` in a single array: either the raw entry shadows the stub and
  the session bypasses the broker, or both register and every pooled backend runs
  twice (#927). The client resolves `injection_server_names` for its overlay and
  passes the set down; an unreadable overlay degrades to "no stubs", never to a
  session with no servers.
- **Crew's control plane is re-derived, not copied.** `kirocrew-core` and
  `kirocrew-cron` come from `agent.managed_mcp_spec_entry`, so a stale hand-edited
  command in the spec cannot cost a session the tools it needs to report back at
  all. A missing or malformed spec degrades to the control plane alone; nothing in
  the path raises, because a bad element fails the whole `session/new`.
- **`autoApprove` is not translated.** Its nearest CC equivalent is a
  `permissions.allow` entry, and a pre-approved tool is one Claude never asks
  about — so the call would never reach the host gate that carries the deny floor,
  the sensitive-path check and the governance ceiling. Every MCP call on this
  backend stays gated. `timeout` is kiro-only and dropped.
- **`disabledTools` becomes a `permissions.deny` rule.** It cannot ride along in
  an array element either, but it is a RESTRICTION: dropping it while forwarding
  the server it narrows would silently widen the session's tool surface, which is
  what the dashboard writes that key to prevent. `session_mcp_deny_rules` turns
  each entry into `mcp__<server>__<tool>` and the settings writer merges those
  into `permissions.deny`, which CC evaluates ahead of every allow rule and of the
  host callback. One asymmetry remains open: a `@server/tool` reference grants ONE
  tool on kiro-cli while the array mounts the whole server here, and the set to
  deny is not knowable without connecting to the server — those extra tools still
  reach the host permission gate, so the surface is wider, not ungated.

Both call sites are fed from the one function —
`_new_session_following_substitution` (`session/new`) and the `session/load`
branch — and the array is ordered by server name so the two are comparable.

Those call sites are **synchronous**, and that is a harness-parity constraint
rather than a style choice. The translation reads disk, but it runs once per
spawn in `_resolve_session_mcp_servers` (off the loop, from the adapter-only
branch of `_spawn`) and lands in `_session_mcp_cache`; `_session_mcp_servers` only
hands the cached list out. Awaiting an executor hop at the shared site would put a
new scheduling and failure point on **every** backend's construction path,
kiro-cli included — and the kiro path is not allowed to change in service of an
adapter (`AUTOSDE.yaml` `harness-parity` H13). The capability set is still what
decides: the set check comes first, so a harness outside it returns `[]` before
the cache is ever consulted, and a set member with a cold cache resolves inline
rather than coming up with no tools. `_reset_state` clears the cache, which is
what keeps the per-spawn freshness promise.

The kiro-cli path is unchanged: it receives the same servers via `--agent`, and a
duplicate array here would shadow the spec's own entries, so that backend passes
none.

### Session-scoped Claude settings

`_write_claude_local_settings` writes `<work_dir>/.claude/settings.local.json`
before the primary spawn (not only on the model-substitution retry) with four
things: `permissions.defaultMode` when the session asked for one, the
`permissions.deny` rules derived from the spec's `disabledTools`,
`availableModels` from the registry, and `model` when the session pinned one. The
allowlist is not cosmetic — without it the adapter can collapse a versioned `[1m]`
id back to the 200K window.

**Crew CREATES this file or it leaves the path entirely alone.** That single
ownership rule is what the whole seam rests on. `work_dir` is routinely a
checked-out project the user also drives with `claude` by hand, so a
`settings.local.json` already sitting there belongs to someone else — either the
user, or a live sibling session (`work_dir` is caller-supplied and every keyless
client shares one default). Crew authored neither, so it reads neither, merges into
neither, rewrites neither and deletes neither. `AcpClient._reset_state` removes only
a file this session itself created, which is also what keeps a permission mode from
outliving its session and an inherited `bypassPermissions` from surviving a crash.

The create is `O_EXCL` + `O_NOFOLLOW` at `0o600`, so the existence check is only a
fast path: two sessions sharing a `work_dir` can both pass it, and the loser of the
race declines rather than clobbering the winner. Nothing on the teardown path writes
a file, so reset is a single `unlink` — the same operation the pre-change code
performed at that point.

What this deliberately does NOT do is preserve and restore a user's own file.
Doing that means reading and rewriting a path a checked-out repository controls,
which is how a snapshot read, a cross-session ownership registry, an ACL-preserving
restore write and a foreign-edit comparison all become necessary — and each of those
is a place to get it wrong on someone's project state. Refusing the path is the
invariant that removes all of them at once.

The cost is disclosed rather than hidden, and it is a REDUCTION in what Crew
applies, never a widening of what the session can do:

- A project that already has its own `settings.local.json` gets no seed, so that
  session runs without the `availableModels` allowlist (a versioned `[1m]` id may
  collapse to 200K) and without the `permissions.deny` rules derived from
  `disabledTools`.
- An inherited `bypassPermissions` in such a file is **not** stripped. Crew used to
  strip it; stripping required rewriting the user's file, which is exactly the
  machinery this rule removes. A tool call still reaches Crew's `canUseTool` gate
  unless that file pre-approves it — the same boundary the inherited-`~/.claude`
  gap below already documents, arriving through the project file instead.
- **That session also gets no `mcpServers` array at all**, and this is what keeps
  the sentence above true. The array is the session's whole MCP surface, so
  delivering it here would hand `spawn_run`, `cron_add`, `send_message` and every
  configured server into a permission surface Crew does not control — a
  `permissions.allow` entry in the project's own file pre-approves the tool, no
  `session/request_permission` is ever sent, and Crew sees the `tool_call`
  notification too late to withhold it. That would be a widening, not a
  reduction. So the seed and the array travel together: Crew delivers tools only
  where it authored the file that governs their use, and
  `ClaudeCodeMirror.session_params` fails closed on that precondition rather than
  taking it on trust from its caller.

A **symlink** at either component (the file or the `.claude` directory), or a
sensitive resolved target, is REFUSED rather than followed. Since Crew never reads
or rewrites an existing file, the exposure is the *create*: a dangling link is
absent to `exists()` yet writing it materializes Crew's settings at the link's
target, and a symlinked `.claude` directory puts the whole write somewhere the
project does not own. A repository can ship
`.claude/settings.local.json -> ~/.aws/credentials`, so this is attacker-influenced
input. Refusing costs that session the seed, which the caller logs as a warning
naming what is lost.

`_spawn` also merges `extra_env` into the child environment, which is how a
caller-supplied `CLAUDE_CONFIG_DIR` reaches the adapter
(`test_spawn_forwards_claude_config_dir_from_extra_env`). The public core does not
set that variable itself: an isolated CC config root (seeding a Crew-owned
directory from the user's `~/.claude`, keeping credentials and models while
stripping inherited `permissions` that would pre-approve past Crew's gate) is
**not implemented here** — see the known gap below.

### Known gap: the user's global `~/.claude` is inherited

With no `CLAUDE_CONFIG_DIR` set by the core, the adapter and the SDK read the
user's real `~/.claude`. Project-scope `settings.local.json` outranks it for
`defaultMode`, but `permissions.allow` entries **merge** rather than being
overridden — so a user whose global settings pre-approve a tool family gets those
calls auto-approved by Claude's own engine, which never calls `canUseTool` and so
never reaches Crew's gate. Crew does not close this from the project file either:
the seam above declines a `settings.local.json` it did not author, so an
`allow` entry in a user's own project file is left in place exactly as one in
`~/.claude` is. Both are the same hazard the "no gate on pre-approved calls"
section above describes, arriving through inherited config. Closing it means an
isolated config root, which is a separate change: it has to carry credentials
across (or CC cannot authenticate at all) while dropping exactly the `permissions`
keys that bypass the gate.

### Standing rule

Unchanged by Claude Code becoming selectable: `agent.provider` stays
single-valued and **no provider selector is re-added**. The harness switch is
`agent.acp_backend`, and it is gated in exactly one place
(`resolve_selected_backend`).

## Model registry

`src/kiro_crew/model_registry.json` is the shared model data source for
`model_registry.py` and `website/src/model_registry.json`.
`test_frontend_registry_matches_python_source` compares their parsed JSON, and
`website/src/providers/modelRegistry.ts` imports the frontend copy. The
per-entry `claude_code` provider IDs are registry mappings for the adapter's
advertised ids, not values accepted by `AgentConfig.provider`.

`model_registry._build_indices` indexes canonical keys, provider IDs, and
aliases. `from_provider_id` uses that index to recover a canonical key from an
advertised adapter ID. `TestModelRegistry.test_bare_advertised_ids_fold_to_canonical_key`
pins the bare-ID case.

`model_registry.available_models` and `display_list` sort default entries first
rather than trusting JSON object order. This is load-bearing because the adapter
uses the resulting allowlist when an automatic selection omits an explicit
model. `TestModelRegistry.test_fable_5_not_default` and
`TestModelRegistry.test_available_models_is_default_first` pin the default and
ordering behavior.
