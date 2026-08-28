# Claude Code backend — experimental

> **The standalone Claude Code *provider* is still gone.** What exists is a
> selectable ACP **backend**: `agent.provider` remains fixed to `acp`, and
> `agent.acp_backend = "claude"` points that single provider at
> `claude-agent-acp` instead of `kiro-cli`. There is no second `LLMProvider`, no
> API-key path, and no dashboard provider selector.
>
> Driving a registry adapter at `agent.acp_backend` is a shipped goal rather than
> a divergence. The conditions that remain — provider stays `acp`, no API-key
> path, refuse-unless-routed — are in
> [`docs/task-specs/2026/08/pluggable-acp-backends/README.md`](../../task-specs/2026/08/pluggable-acp-backends/README.md).
> kiro-cli remains the only default and the only non-experimental backend.

The backend is marked `experimental` in the registry and is not the default. Its
capability table, tool-gate routing and sign-in are described in
[`../modules/providers.md`](../modules/providers.md) § "Backend registry" and
[`../modules/security.md`](../modules/security.md) § "ACP backend tool-gate
routing". The operator-facing guide is
[`src/kiro_crew/docs/experimental-acp-adapters.md`](../../../src/kiro_crew/docs/experimental-acp-adapters.md).

What this backend does NOT support, from its descriptor: session sharing and
multiplexed subagents (it runs one process per session on the legacy `AcpClient`
path), MCP Tool Search, agent-profile enforcement (no `set_mode` equivalent, which
is why `acp/spec_agent_guard.py` refuses a shell-withholding custom agent),
subscription reporting, and mid-turn steer. Reasoning effort, slash commands and
per-turn usage are degraded rather than absent.

Kiro Crew stores no credential for this backend: the Claude CLI owns sign-in
entirely, and the descriptor names no credential leaf because there is no
Kiro-Crew-visible token file to protect.

## What remains of the original seam

The protocol machinery this backend rides predates the registry and is unchanged:
`_resolve_claude_acp_bin`'s ladder (explicit override, the vendored copy, mise,
augmented PATH), the incomplete-vendored-copy skip, `CLAUDE_CONFIG_DIR` isolation,
and `CLAUDE_CODE_EXECUTABLE` resolution with its deliberate "leave unset and
surface the adapter's own error" behaviour. Details in
[`acp-client.md`](../modules/acp-client.md).

The one addition is `acp/claude.py`: a permission-mode probe that reads
`permissions.defaultMode` back out of the per-session `settings.local.json`, plus a
conservative seeder that writes `default` only when nothing is configured. In the
public core nothing else writes that file — `_write_claude_local_settings` is
`getattr`-guarded and companion-attached — so without the seeder the adapter falls
back to its own default mode and the routing verdict is correctly INDETERMINATE.

## The seam is described but withheld

`acp/client.py`'s `ACP_BACKEND_CLAUDE` / `_is_claude` protocol seam was built so an
internal companion could re-register an alternate `claude-agent-acp` backend
without forking the client. The seam is reachable: the backend has a registry
descriptor, but `claude` is not in `ACP_BACKENDS_SELECTABLE`. The routing seed
may merge into an operator's existing project settings file, while the current
reset path unlinks that whole file. Selection stays withheld until cleanup can
remove only Kiro Crew-owned state without deleting unrelated settings.

Nothing here asks a reader to re-add a provider *selector*: what is added is a
backend selector, and `agent.provider` still has a single-valued enum.

The seam's binary-resolution details (`_resolve_claude_acp_bin`, the per-session
`settings.local.json` permission routing, `CLAUDE_CONFIG_DIR` isolation) are
documented in [`acp-client.md`](../modules/acp-client.md).

## Model registry

`model_registry.json` (loaded by `model_registry.py`, mirrored to
`website/src/model_registry.json` and guarded by `test/test_model_registry_parity.py`)
remains the single source of truth for model names and context windows. Its
per-entry `providers` map keys models under the canonical `claude_code` namespace
— that is just the canonical key form (the public ACP model-id shape, e.g.
`global.anthropic.claude-opus-4-8[1m]`); it is not a selectable provider.

Canonical keys include `fable-5-1m` (leads the JSON but is **not** default —
Opus 4.8 `opus-4.8-1m` remains the sole `"default": true` entry), and
`available_models()` (`model_registry.py`) is default-first-ordered, so a
non-default entry sitting ahead of Opus in the file cannot change the
`auto`-path pick. Each entry's `aliases` also include the bare, prefix-stripped
provider-id spelling (e.g. `claude-fable-5`) so `from_provider_id` folds bare
ids onto the canonical key.
