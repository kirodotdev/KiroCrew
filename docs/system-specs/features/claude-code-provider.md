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

### Known gap: a Claude session has no Crew MCP tools

`AcpClient._claude_session_mcp_servers()` is an overridable seam that **defaults
to returning `[]`**, and the `claude-agent-acp` adapter does not read
`kirocrew.mcp.json` on its own. On a build that does not override that method — the
public core does not — a Claude session starts with **zero MCP tools**. The
harness itself works: prompts, streaming, model and effort selection, and the
full `session/request_permission` flow. What is absent is Crew's own tooling,
`kirocrew-core`, cron, and every user-configured MCP server.

The default is byte-identical for kiro-cli, which receives its servers via
`--agent` and is unaffected. Both call sites carry the gap —
`_new_session_following_substitution` (`session/new`) and the `session/load`
branch — so closing it for the public build means translating
`kirocrew.mcp.json` into that array in one place.

### Companion-owned glue stays out of the core

The public client accepts edition-supplied Claude settings behavior without
owning it, hooked through `getattr` so the core is byte-identical when the hook
is absent:

- `_spawn` calls an optional `_write_claude_local_settings` on the **primary**
  spawn path, not only on the model-substitution retry — a session that skips it
  collapses to the 200K context default.
- `_spawn` merges `extra_env` into the child environment, which is how a
  caller-supplied `CLAUDE_CONFIG_DIR` reaches the adapter
  (`test_spawn_forwards_claude_config_dir_from_extra_env`).
- `AcpClient._reset_state` removes `<work_dir>/.claude/settings.local.json` for a
  Claude client. This is load-bearing because no caller retries teardown, so a
  session-scoped elevated permission setting must not outlive its client.

Standing rule, unchanged by Claude Code becoming selectable: `agent.provider`
stays single-valued and **no provider selector is re-added**. The harness switch
is `agent.acp_backend`, and it is gated in exactly one place
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
