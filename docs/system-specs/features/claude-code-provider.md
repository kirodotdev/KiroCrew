# Standalone provider — removed

> **This provider no longer exists in KiroCrew.** The public fork drives a single
> backend — `kiro-cli` over the Agent Client Protocol (`agent.provider` is fixed
> to `acp`). The removed standalone provider, the removed Bedrock provider, the
> removed agent-renderer / mirror modules, their config fields, and the dashboard
> provider selector were all removed during de-Amazoning. There is no provider to
> choose.

## What remains (the dormant ACP seam)

`acp/client.py` keeps an inert protocol seam (`ACP_BACKEND_CLAUDE` / the
`_is_claude` branch) so an internal companion package can re-register an
alternate `claude-agent-acp` backend without forking the client. The public core
never selects it: `_resolve_kiro_bin` is the only backend the provider factory
wires, and the dashboard exposes no provider choice. **Do not re-add the
registration glue or a provider selector** — see the repo-root `CLAUDE.md`.

The seam's binary-resolution details (`_resolve_claude_acp_bin`, the per-session
`settings.local.json` permission routing, `CLAUDE_CONFIG_DIR` isolation) are
documented in [`acp-client.md`](../modules/acp-client.md) for the companion that
re-enables it — they are not user-facing in the public build.

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
