# Claude Code provider — dormant ACP seam

## Public provider boundary

`AgentConfig.provider` admits only the ACP provider, and
`KiroCrewConfig.create_provider_factory()` constructs `AcpProvider`. The public
build still selects between its Kiro and KAS ACP harnesses through
`agent.acp_backend`: `acp_backends.BASELINE_SELECTABLE_BACKENDS` contains those
harnesses, while omitting the Claude backend. `DefaultProviderRegistry` adds no
extra selectable backend.

`acp_backends.resolve_selected_backend()` normalizes an unregistered
`agent.acp_backend` value to the Kiro harness. This boundary is load-bearing:
`AcpProvider` rejects unknown harnesses, so normalization prevents a persisted
or hand-edited value from becoming a startup failure. `TestConfigThreading.test_unselectable_values_degrade_to_the_default`
exercises that path.

## Dormant Claude seam

`acp.client.AcpClient._is_claude` recognizes `ACP_BACKEND_CLAUDE`, and
`AcpClient._spawn` retains its adapter-resolution branch through
`_resolve_claude_acp_bin`. The public factory cannot select that branch because
its backend is absent from the selectable registry. An edition companion makes
the branch reachable only by registering both its provider and the Claude
backend with `acp_backends.register_selectable_backend`; the registration
function documents why either registration alone leaves the harness unreachable.

The public client accepts companion-supplied Claude settings behavior without
owning it. In the Claude branch, `_spawn` calls an optional
`_write_claude_local_settings` hook and forwards `CLAUDE_CONFIG_DIR` from
`extra_env` (`test_spawn_forwards_claude_config_dir_from_extra_env`).
`AcpClient._reset_state` removes the per-work-directory local settings file for
a Claude client. This is load-bearing because no caller retries teardown, so a
session-scoped elevated permission setting must not outlive its client.

## Model registry

`src/kiro_crew/model_registry.json` is the shared model data source for
`model_registry.py` and `website/src/model_registry.json`.
`test_frontend_registry_matches_python_source` compares their parsed JSON, and
`website/src/providers/modelRegistry.ts` imports the frontend copy. The
per-entry `claude_code` provider IDs are registry mappings for the dormant
adapter, not values accepted by `AgentConfig.provider`.

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
