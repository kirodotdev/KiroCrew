"""Contracts the agent-materialization split must carry across unchanged.

``kiro_crew.agent`` is the import path every caller, test and document uses, and its
implementation now lives in owners under ``kiro_crew.agent_materialization``. These
tests freeze what the pre-split module offered and what it wrote, independently of
where the code lives:

* the SURFACE: every name the module bound, with its kind, signature and value;
* ``toolsSettings.subagent.availableAgents``: a kiro-cli-owned allowlist Crew reads at
  its own spawn gate (``subagent.py``), carried byte-for-byte through every read,
  normalize, rebuild, derived-agent and atomic-write path;
* the tool-less guest spec, including its model fallback and install order;
* the conductor prompts and grant tuples, which are spec bytes too.

``test_agent_refactor_spec_bytes.py`` freezes the whole rebuild's output; this module
pins the individual contracts a reviewer would otherwise have to find in that output.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import os
import re
import types
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent, agent_state, platform_compat
from kiro_crew.agent_files import GUEST_AGENT_FILENAME, LITE_AGENT_FILENAME
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

# ── the surface ─────────────────────────────────────────────────────────────

#: Every name ``kiro_crew.agent`` bound before the split, described the way
#: :func:`_describe` describes it now. A value is compared by the digest of its JSON
#: rendering; a name whose value depends on the host (a home-derived path, the logger)
#: is compared by type only, and process state the module keeps across calls (a
#: warned-once set, a pending count) by name only, because earlier tests set it.
BASE_SURFACE: dict[str, str] = {
    "AGENT_FILENAME": "value str a05e57cdc5411e1d",
    "AmbiguousAgentSpecError": "class AmbiguousAgentSpecError",
    "CU_MCP_SERVER": "value str a7edc1bcf90cb13f",
    "DEPRECATED_AGENT_SPECS": "value dict 3c349bf8ac509967",
    "DERIVED_KEY": "value str 0c56dd87b525dbd3",
    "DerivedSpecSnapshot": "class DerivedSpecSnapshot fields=identity,fingerprint,spec",
    "DerivedSpecStale": "class DerivedSpecStale",
    "FileTooLargeError": "class FileTooLargeError",
    "ForeignAgentSpec": "class ForeignAgentSpec",
    "ForkGovernanceUnresolved": "class ForkGovernanceUnresolved",
    "GUEST_AGENT_PROMPT": "value str 9f07b9e7c179fff5",
    "KIRO_AGENTS_DIR": "value host",
    "MCP_PATH_HINT": "value str 2c0b855124e13185",
    "OWNED_KIRO_AGENT_FILES": "value tuple 56259bfccecde44b",
    "Path": "class Path",
    "REQUIRED_KIRO_AGENT_FILES": "value tuple d3a5d201c05d5c9b",
    "SecurityEvent": "class SecurityEvent",
    "_BACKGROUND_CC_MODEL": "value str e5710d6b1810296f",
    "_BUNDLED_CFG_DIR": "value host",
    "_CC_MCP_JSON": "value host",
    "_CONDUCTOR_AGENT_FILENAME": "value str 923f5ca0627d569f",
    "_CONDUCTOR_CORE_GRANTS": "value tuple 7152a6eb169c7c40",
    "_CONDUCTOR_DASHBOARD_GRANTS": "value tuple 0b859d2b54e84503",
    "_CONDUCTOR_SYSTEM_PROMPT": "value str a8e2f9788539103e",
    "_CREW_ONLY_HOOK_EVENTS": "value frozenset 9d900cfb866f983a",
    "_DEFAULT_KIRO_HOOKS_DIR": "value host",
    "_DEFAULT_SPEC_OBSERVATION_ATTEMPTS": "value int 4e07408562bedb8b",
    "_FILENAME_EVENT_SUFFIXES": "value tuple acdc79623b2f3e65",
    "_FORK_REFRESH_WAIT_SECS": "value float db58b6c40698d737",
    "_GUEST_AGENT_FILENAME": "value str b1766e070226dc68",
    "_HEARTBEAT_AGENT_FILENAME": "value str 42387f9e3eabbf0e",
    "_HEARTBEAT_SYSTEM_PROMPT": "value str 9054896452b91851",
    "_HOME_DERIVING_ENV_KEYS": "value frozenset 816c277fc333b3e0",
    "_HOOK_EVENT_CANONICAL": "value dict a09f334fa63b1a4f",
    "_HOOK_HEADER_RE": "value Pattern 905320879920c153",
    "_HOOK_HEADER_SCAN_LINES": "value int ef2d127de37b942b",
    "_HOOK_SPEC_AUDIT_TAG": "value str 872be442e26c62c8",
    "_HOOK_SUPPRESSED_CONFIRM": "value str 287cc46175706b5e",
    "_HOOK_SUPPRESSED_DISABLED": "value str 3e7d4779ece7800e",
    "_INTERNAL_HOOK_KEYS": "value frozenset 274d5b26dc7c6ca8",
    "_KAS_ACTION_TYPES": "value frozenset 903158b00413cba6",
    "_KAS_DOCUMENT_FIELD_LIMITS": "value tuple b80ab2c94d50d9dc",
    "_KAS_DOCUMENT_FIELD_TYPES": "value tuple 87ad0f01d8e8353c",
    "_KAS_TRIGGER_CANONICAL": "value dict cc814336d72dc4c9",
    "_KAS_TRIGGER_TO_EVENT": "value dict 16182f505da03cae",
    "_KIROCREW_BIN": "value host",
    "_KIRO_MCP_JSON": "value host",
    "_KNOWLEDGE_AGENT_FILENAME": "value str 85e4346dff762496",
    "_KNOWLEDGE_SYSTEM_PROMPT": "value str 23af593eebd8222f",
    "_LAUNCHER_EXEC_ENV_KEYS": "value frozenset 8c69b5e74a77bb5d",
    "_LEDGER_CONDUCTOR_AGENT_FILENAME": "value str a87e496fff7c15e0",
    "_LEDGER_CONDUCTOR_WORK_GRANTS": "value tuple 21f3684b0bc1f923",
    "_LEGACY_KIROCREW_HOOK_KEYS": "value frozenset 41cb57ce40df86b2",
    "_LITE_AGENT_FILENAME": "value str 55e6029e6c753857",
    "_MAIN_AGENT_NAME": "value str 153b868d134ec50e",
    "_MANAGED_MCP_ENTRY_ITEM_TYPES": "value dict b743f9c4b68215c4",
    "_MANAGED_MCP_ENTRY_KEYS": "value frozenset 5b8554ac36b42722",
    "_MANAGED_MCP_ENTRY_VALUE_TYPES": "value dict adfab10cc996c8fe",
    "_MANAGED_MCP_SERVERS": 'value dict {"gates": {"kirocrew-computer": "_computer_use_spec_gate", "kirocrew-core": null, "kirocrew-crew-log": null, "kirocrew-cron": null, "kirocrew-dashboard": null, "kirocrew-debug": null, "kirocrew-guide": null, "kirocrew-panel": null, "kirocrew-work": null}, "opt_in": {"kirocrew-computer": false, "kirocrew-core": false, "kirocrew-crew-log": true, "kirocrew-cron": false, "kirocrew-dashboard": true, "kirocrew-debug": true, "kirocrew-guide": false, "kirocrew-panel": true, "kirocrew-work": true}, "shape": {"kirocrew-computer": ["invocation_fn", "spec_gate"], "kirocrew-core": ["invocation_fn"], "kirocrew-crew-log": ["invocation_fn", "opt_in"], "kirocrew-cron": ["invocation_fn"], "kirocrew-dashboard": ["invocation_fn", "opt_in"], "kirocrew-debug": ["invocation_fn", "opt_in"], "kirocrew-guide": ["invocation_fn"], "kirocrew-panel": ["invocation_fn", "opt_in"], "kirocrew-work": ["invocation_fn", "opt_in"]}}',
    "_MAX_HOOK_DESCRIPTION_LEN": "value int 40510175845988f1",
    "_MAX_HOOK_NAME_LEN": "value int 27badc983df1780b",
    "_MAX_HOOK_PAYLOAD_LEN": "value int 8b926d75599a618e",
    "_MAX_MATCHER_LEN": "value int 27badc983df1780b",
    "_MAX_SPEC_HOOK_DOCUMENTS": "value int 27badc983df1780b",
    "_MAX_TOTAL_USER_HOOKS": "value int f5ca38f748a1d6ea",
    "_MAX_USER_HOOKS_PER_EVENT": "value int 4a44dc15364204a8",
    "_MCP_REGISTRY_TYPE": "value str 06c63899e16a32c5",
    "_MEMBER_DASHBOARD_GRANTS": "value tuple 217848c3e96f9a58",
    "_MEMBER_PANEL_GRANTS": "value tuple c03fa46dc2d0f774",
    "_NATIVE_PROMPT_STUB": "value str ceed0be70c1f6da8",
    "_PIPELINE_CONDUCTOR_AGENT_FILENAME": "value str 3b5e111de6d1d305",
    "_PIPELINE_CONDUCTOR_CORE_GRANTS": "value tuple 1ad9ce711e266082",
    "_PIPELINE_CONDUCTOR_DASHBOARD_GRANTS": "value tuple 3f7fb6d17a7f0ebc",
    "_PIPELINE_CONDUCTOR_SYSTEM_PROMPT": "value str d7010e266b564182",
    "_PROVENANCE_SPEC_CAP_BYTES": "value int 54faea9b3eeffce2",
    "_PROVENANCE_TOTAL_BUDGET_BYTES": "value int 107f41ed92e01eb5",
    "_RESEARCH_AGENT_FILENAME": "value str 5cec351bc572af00",
    "_RESEARCH_SYSTEM_PROMPT": "value str b99ba1f3e040be31",
    "_SAFE_MATCHER_RE": "value Pattern ac8a0d6bfee13169",
    "_SAFE_PATH_RE": "value platform",
    "_SECURITY_CONDUCTOR_AGENT_FILENAME": "value str ea0f45466289114a",
    "_SECURITY_CONDUCTOR_DASHBOARD_GRANTS": "value tuple 4f02f1db82a878ee",
    "_SECURITY_CONDUCTOR_SYSTEM_PROMPT": "value str 667c3be2c962f11a",
    "_SOURCE_OWNED_MCP_KEYS": "value tuple b13c0a56645cc511",
    "_VALID_HOOK_EVENTS": "value frozenset 2b7aa96bff704c33",
    "_WORKER_AGENT_FILENAME": "value str e6d5cee102d8cbdf",
    "_WORKER_EXCLUDED_GRANTS": "value frozenset 1a2756dd420ed333",
    "_WORKER_MIRRORED_KEYS": "value tuple 27234a2a963813c9",
    "_WORKER_MIRRORED_SHAPES": "value dict 245618c04df2519f",
    "_WORKER_SYSTEM_PROMPT": "value str e8e4eb580b4afc01",
    "_WORKER_WORK_GRANTS": "value tuple 2617544a8ba41753",
    "_agent_identity_enabled": "callable _agent_identity_enabled() -> 'bool'",
    "_agentcore_capability_permitted": "callable _agentcore_capability_permitted() -> 'bool'",
    "_aim_skill_paths": "callable _all_skill_paths() -> 'list[str]'",
    "_alias_family_base": "callable _alias_family_base(key: 'str') -> 'str'",
    "_all_skill_paths": "callable _all_skill_paths() -> 'list[str]'",
    "_app_owned_mcp_keys": "callable _app_owned_mcp_keys() -> 'dict[str, bool]'",
    "_apply_allowed_tools_ceiling": "callable _apply_allowed_tools_ceiling(config: 'dict', *, source: 'str') -> 'None'",
    "_apply_connection_tool_aliases": "callable _apply_connection_tool_aliases(config: 'dict', claimed: 'frozenset[tuple[str, str, str]]' = frozenset()) -> 'tuple[str, frozenset[tuple[str, str, str]]] | None'",
    "_apply_operator_oauth_client": "callable _apply_operator_oauth_client(name: 'str', entry: 'dict', *, managed: 'bool') -> 'dict'",
    "_apply_user_kiro_hooks": "callable _apply_user_kiro_hooks(config: 'dict', mc_cfg: 'dict') -> 'None'",
    "_apply_worker_exclusions": "callable _apply_worker_exclusions(granted: 'list[str]', *, template_grants: 'list[str]') -> 'list[str]'",
    "_atomic_json_write": "callable _atomic_json_write(path: 'Path', data: 'dict') -> 'None'",
    "_autoimport_kiro_hooks": "callable _autoimport_kiro_hooks(hooks_dir: 'Path') -> 'dict[str, list[dict[str, str]]]'",
    "_background_agent_model": "callable _background_agent_model() -> 'str'",
    "_background_cc_model": "callable _background_cc_model() -> 'str'",
    "_bin_is_usable": "callable _bin_is_usable(path: 'Path') -> 'bool'",
    "_canonical_grant_pattern": "callable _canonical_grant_pattern(ref: 'str') -> 'str | None'",
    "_ceiling_filtered_spec": "callable _ceiling_filtered_spec(ref: 'str', spec: 'dict[str, Any]', *, audit: 'bool' = True) -> 'dict[str, Any]'",
    "_collect_app_mcp_servers": "callable _collect_app_mcp_servers(*, audit: 'bool' = True) -> 'dict[str, Any]'",
    "_computer_use_spec_gate": "callable _computer_use_spec_gate() -> 'bool'",
    "_conductor_mcp_servers": "callable _conductor_mcp_servers(config: 'dict[str, Any]') -> 'dict[str, Any]'",
    "_conductor_spec": "callable _conductor_spec(*, name: 'str', description: 'str', filename: 'str', source: 'str', clean: 'bool' = False) -> 'dict[str, Any] | None'",
    "_conflicting_spec_for": "callable _conflicting_spec_for(name: 'str', chosen: 'Path', agents_dir: 'Path') -> 'Path | None'",
    "_connection_tool_aliases_enabled": "callable _connection_tool_aliases_enabled() -> 'bool'",
    "_declared_project_agent_name": "callable _declared_project_agent_name(spec: 'Path') -> 'str | None'",
    "_decline_shared_agent_home": "callable _decline_shared_agent_home(*, audit: 'bool' = True) -> 'Path | None'",
    "_declined_home_warned": "value state",
    "_deep_merge": "callable _deep_merge(base: 'dict', override: 'dict') -> 'dict'",
    "_derived_spec_matches_default": "callable _derived_spec_matches_default(agent: 'str') -> 'bool'",
    "_drop_servers": "callable _drop_servers(config: 'dict', servers: 'frozenset[str]') -> 'list[str]'",
    "_durable_tool_aliases": "callable _durable_tool_aliases(path: 'Path') -> 'tuple[bool, object]'",
    "_enforce_managed_mcp_ownership": "callable _enforce_managed_mcp_ownership(entry: 'dict', spec: 'dict', registry_mode: 'bool', *, auto_approve: 'str') -> 'None'",
    "_entry_is_the_declared_server": "callable _entry_is_the_declared_server(entry: 'object', spec: 'Mapping[str, Any]') -> 'bool'",
    "_event_for_hook_trigger": "callable _event_for_hook_trigger(trigger: 'object') -> 'str | None'",
    "_excluded_verb": "callable _excluded_verb(ref: 'str') -> 'str'",
    "_existing_specs_are_mine": "callable _existing_specs_are_mine(target: 'Path', own_home: 'Path | None') -> 'bool | None'",
    "_extra_mcp_scope_globals": "callable _extra_mcp_scope_globals() -> 'list[Path]'",
    "_extra_mcp_servers": "callable _extra_mcp_servers() -> 'dict[str, dict]'",
    "_file_identity": "callable _file_identity(path: 'Path') -> 'str | None'",
    "_filter_auto_approve": "callable _filter_auto_approve(refs: 'tuple[str, ...]', *, source: 'str') -> 'list[str]'",
    "_foreign_worker_spec_reason": "callable _foreign_worker_spec_reason(spec: 'dict[str, Any] | None') -> 'str | None'",
    "_fork_refresh_count_lock": "value lock",
    "_fork_refresh_failed": "value state",
    "_fork_refresh_lock": "value lock",
    "_fork_refresh_pending": "value state",
    "_fork_refresh_settled": "value Event",
    "_gated_off_servers": "callable _gated_off_servers() -> 'frozenset[str]'",
    "_glob_hits": "callable _glob_hits(concrete: 'str', pattern: 'str') -> 'bool'",
    "_grant_reaches_excluded": "callable _grant_reaches_excluded(entry: 'str') -> 'list[str]'",
    "_hook_command_reaches_a_share": "callable _hook_command_reaches_a_share(command: 'str') -> 'bool'",
    "_hook_diagnostic": "callable _hook_diagnostic(value: 'object') -> 'str'",
    "_hook_document_action": "callable _hook_document_action(action: 'object', *, index: 'int') -> 'dict | None'",
    "_hook_document_from_document": "callable _hook_document_from_document(entry: 'object', *, index: 'int') -> 'dict | None'",
    "_hook_documents_from_array_form": "callable _hook_documents_from_array_form(hooks: 'list') -> 'list[dict]'",
    "_hook_matcher_ok": "callable _hook_matcher_ok(matcher: 'object') -> 'bool'",
    "_hooks_sanitized_mtimes": "value state",
    "_in_ephemeral_tree": "callable _in_ephemeral_tree(path: 'Path', env: 'Mapping[str, str] | None' = None) -> 'bool'",
    "_in_linked_git_worktree": "callable _in_linked_git_worktree(path: 'Path') -> 'bool'",
    "_infer_hook_event": "callable _infer_hook_event(script_path: 'Path', event_header: 'str | None') -> 'str | None'",
    "_install_aim_capabilities": "callable _install_aim_capabilities() -> 'None'",
    "_install_conductor_agent": "callable _install_conductor_agent(*, clean: 'bool' = False) -> 'bool'",
    "_install_guest_agent": "callable _install_guest_agent() -> 'None'",
    "_install_heartbeat_agent": "callable _install_heartbeat_agent() -> 'None'",
    "_install_knowledge_agent": "callable _install_knowledge_agent() -> 'None'",
    "_install_ledger_conductor_agent": "callable _install_ledger_conductor_agent(*, clean: 'bool' = False) -> 'bool'",
    "_install_lite_agent_fallback": "callable _install_lite_agent_fallback() -> 'None'",
    "_install_pipeline_conductor_agent": "callable _install_pipeline_conductor_agent(*, clean: 'bool' = False) -> 'bool'",
    "_install_research_agent": "callable _install_research_agent() -> 'None'",
    "_install_security_conductor_agent": "callable _install_security_conductor_agent(*, clean: 'bool' = False) -> 'bool'",
    "_install_worker_agent": "callable _install_worker_agent() -> 'None'",
    "_installed_default_spec": "callable _installed_default_spec() -> 'dict[str, Any] | None'",
    "_interpreter_runnable": "callable _interpreter_runnable(candidate: 'Path') -> 'bool'",
    "_is_alias_family": "callable _is_alias_family(key: 'str', base: 'str') -> 'bool'",
    "_is_managed_prompt_pointer": "callable _is_managed_prompt_pointer(prompt: 'str') -> 'bool'",
    "_kiro_hooks_only": "callable _kiro_hooks_only(hooks: 'dict') -> 'dict'",
    "_kirocrew_bin_subpath": "callable _kirocrew_bin_subpath(root: 'Path') -> 'Path'",
    "_kirocrew_mcp_invocation": "callable _kirocrew_mcp_invocation(subcommand: 'str') -> 'tuple[str, list[str]]'",
    "_launcher_works": "callable _launcher_works(path: 'Path') -> 'bool'",
    "_lineage_unreadable_refusal": "callable _lineage_unreadable_refusal(agent: 'str', exc: 'BaseException') -> 'str'",
    "_load_existing_config": "callable _load_existing_config(path: 'Path', *, gated_off: \"'frozenset[str] | None'\" = None) -> 'tuple[dict, bool]'",
    "_load_json": "callable _load_json(path: 'Path') -> 'dict[str, Any]'",
    "_managed_mcp_env": "callable _managed_mcp_env() -> 'dict[str, str]'",
    "_managed_opt_in_entry": "callable _managed_opt_in_entry(subcommand: 'str') -> 'dict[str, Any]'",
    "_may_auto_approve": "callable _may_auto_approve(ref: 'str') -> 'bool'",
    "_mc_config_path": "callable config_path() -> 'Path'",
    "_mcp_registry_mode": "callable _mcp_registry_mode() -> 'bool'",
    "_mcp_server_emission_eligible": "callable _mcp_server_emission_eligible(name: 'str', spec: 'object', *, gated_off: \"'frozenset[str] | None'\" = None) -> 'bool'",
    "_mcp_spec_gate_open": "callable _mcp_spec_gate_open(name: 'str', spec: 'dict') -> 'bool'",
    "_merge_kiro_hooks": "callable _merge_kiro_hooks(hooks: 'dict', user_hooks: 'dict') -> 'dict'",
    "_merge_source_owned": "callable _merge_source_owned(mcps: 'dict', name: 'str', spec: 'dict', *, stale: 'set[str]') -> 'None'",
    "_migrations_dir": "callable _migrations_dir() -> 'Path'",
    "_norm_mcp_spec": "callable _norm_mcp_spec(spec: 'Any') -> 'Any'",
    "_normalize_mcp_server_keys": "callable _normalize_mcp_server_keys(config: 'dict', *, reserved_keys: 'Collection[str]' = (), removed_grants: 'list[str] | None' = None) -> 'dict[str, str]'",
    "_notify_if_config_write": "callable _notify_if_config_write(path: 'Path') -> 'None'",
    "_parse_hook_script_headers": "callable _parse_hook_script_headers(path: 'Path') -> 'tuple[str | None, str | None]'",
    "_pattern_reaches_excluded": "callable _pattern_reaches_excluded(pattern: 'str') -> 'list[str]'",
    "_pending_projection_warned_generation": "value state",
    "_project_dir": "callable _project_dir() -> 'Path | None'",
    "_project_shadow_of": "callable _project_shadow_of(agent: 'str', work_dir: 'str | Path | None', *, markdown_specs: 'bool' = True, dispatchable_only: 'bool' = False) -> 'Path | None'",
    "_projected_ceiling_generation": "value state",
    "_prompt_path": "callable _prompt_path() -> 'Path'",
    "_read_agent_spec": "callable _read_agent_spec(path: 'Path', *, operation: 'str' = 'list_agents', source: 'str' = 'list_agents') -> 'dict[str, Any] | None'",
    "_read_spec_capped": "callable _read_spec_capped(path: 'Path') -> 'dict | None'",
    "_reconcile_tool_aliases_from_disk": "callable _reconcile_tool_aliases_from_disk(path: 'Path', config: 'dict') -> 'bool'",
    "_refresh_dynamic_fields": "callable _refresh_dynamic_fields(config: 'dict', *, gated_off: \"'frozenset[str] | None'\" = None, fork: 'bool' = False) -> 'None'",
    "_refresh_forked_templates": "callable _refresh_forked_templates(*, gated_off: \"'frozenset[str] | None'\" = None) -> 'None'",
    "_refresh_forked_templates_locked": "callable _refresh_forked_templates_locked(*, gated_off: \"'frozenset[str] | None'\" = None) -> 'None'",
    "_refuse_foreign_worker_spec": "callable _refuse_foreign_worker_spec(path: 'Path', spec: 'dict[str, Any] | None') -> 'None'",
    "_require_fresh_worker_spec": "callable _require_fresh_worker_spec(work_dir: 'str | Path | None') -> 'None'",
    "_resolve_kirocrew_bin": "callable _resolve_kirocrew_bin() -> 'str'",
    "_resolved_hook_command": "callable _resolved_hook_command(command: 'object') -> 'str | None'",
    "_sanitize_agent_hooks": "callable _sanitize_agent_hooks() -> 'None'",
    "_seed_kas_permissions": "callable _seed_kas_permissions(config: 'dict[str, Any]') -> 'None'",
    "_sel_hook_rejected": "callable _sel_hook_rejected(event: 'str', command: 'str', reason: 'str') -> 'None'",
    "_set_tool_aliases": "callable _set_tool_aliases(config: 'dict', aliases: 'object') -> 'None'",
    "_shipped_defaults": "callable _shipped_defaults() -> 'Path'",
    "_shipped_prompt": "callable _shipped_prompt() -> 'Path'",
    "_spec_fingerprint": "callable _spec_fingerprint(spec: 'dict[str, Any] | None') -> 'str | None'",
    "_spec_path_is_safe": "callable _spec_path_is_safe(path: 'Path', agents_dir: 'Path') -> 'bool'",
    "_spec_unresolvable_refusal": "callable _spec_unresolvable_refusal(agent: 'str', exc: 'BaseException') -> 'str'",
    "_stale_mcp_purge_marker": "callable _stale_mcp_purge_marker() -> 'Path'",
    "_stem_claimant_fork": "callable _stem_claimant_fork(agent: 'str') -> 'str | None'",
    "_strip_excluded_auto_approve": "callable _strip_excluded_auto_approve(servers: 'dict[str, Any]') -> 'tuple[dict[str, Any], list[str]]'",
    "_strip_legacy_denied_commands": "callable _strip_legacy_denied_commands(config: 'dict') -> 'None'",
    "_strip_ungoverned_auto_approve": "callable _strip_ungoverned_auto_approve(servers: 'dict[str, Any]') -> 'dict[str, Any]'",
    "_under_system_tmp": "callable _under_system_tmp(path: 'Path') -> 'bool'",
    "_user_dir": "callable _user_dir() -> 'Path'",
    "_user_overrides_path": "callable _user_overrides_path() -> 'Path'",
    "_user_prompt_path": "callable _user_prompt_path() -> 'Path'",
    "_valid_override_home": "callable _valid_override_home() -> 'Path | None'",
    "_validate_hook_command": "callable _validate_hook_command(command: 'str', event: 'str') -> 'str | None'",
    "_warn_declined_home_once": "callable _warn_declined_home_once(arm: 'str', target: 'Path', msg: 'str', *args: 'object') -> 'None'",
    "_whole_server_ref": "callable _whole_server_ref(ref: 'str') -> 'str | None'",
    "_worker_model_is_user_pinned": "callable _worker_model_is_user_pinned() -> 'bool'",
    "_worker_unassignable_servers": "callable _worker_unassignable_servers() -> 'frozenset[str]'",
    "_write_derived_permissions": "callable _write_derived_permissions(config: 'dict[str, Any]', allowed_tools: 'object', agent_filename: 'str') -> 'None'",
    "_write_worker_spec": "callable _write_worker_spec(config: 'dict', path: 'Path', *, template_grants: 'list[str]') -> 'None'",
    "agent_spec_candidates": "callable agent_spec_candidates(directory: 'Path', name: 'str') -> 'list[Path]'",
    "agent_spec_path": "callable agent_spec_path(name: 'str', *, agents_dir: 'Path | None' = None) -> 'Path | None'",
    "agent_state": "module kiro_crew.agent_state",
    "agentcore_posture": "callable agentcore_posture(ceiling: 'Optional[GovernanceCeiling]') -> 'Optional[str]'",
    "agents_spec_lock": "callable agents_spec_lock(agents_dir: 'Path') -> 'Iterator[None]'",
    "ambient_agents_dir": "callable ambient_agents_dir() -> 'Path'",
    "build_agent_config": "callable build_agent_config(*, gated_off: \"'frozenset[str] | None'\" = None) -> 'dict'",
    "clear_model_pin": "callable clear_model_pin(config: 'MutableMapping[str, object]', name: 'str') -> 'None'",
    "command_is_ours": "callable command_is_ours(entry: 'object') -> 'bool'",
    "config_dir": "callable config_dir() -> 'Path'",
    "crew_owned_mcp_servers": "callable crew_owned_mcp_servers() -> 'frozenset[str]'",
    "current_context": "callable current_context() -> 'PlatformContext'",
    "declared_auto_approve": "callable declared_auto_approve(emitted: 'Mapping[str, object]') -> 'dict[str, tuple[str, ...]]'",
    "dedup_path": "callable dedup_path(path: 'str') -> 'str'",
    "default_spec_fingerprint": "callable default_spec_fingerprint() -> 'str | None'",
    "default_spec_identity": "callable default_spec_identity() -> 'str | None'",
    "describe_search_path": "callable describe_search_path(path: 'str') -> 'str'",
    "emission_eligible_mcp_servers": "callable emission_eligible_mcp_servers() -> 'frozenset[str]'",
    "emit_env": "callable emit_env(env: 'dict') -> 'dict'",
    "ensure_agent_materialized": "callable ensure_agent_materialized(agent: 'str | None') -> 'bool'",
    "ensure_kirocrew_on_path": "callable ensure_kirocrew_on_path(bin_dir: 'Path | None' = None, *, claim_existing: 'bool' = False) -> 'str | None'",
    "get_shipped_tools": "callable get_shipped_tools() -> 'dict[str, list[str]]'",
    "governance_permits": "callable governance_permits(scope: 'str', item: 'str', *, session_key: 'str' = '', agent: 'str' = '', app: 'str' = '', log_warning: 'bool' = True, fail_closed: 'bool' = False) -> \"'object'\"",
    "hook_documents_suppressed_commands": "callable hook_documents_suppressed_commands(hooks: 'object') -> 'dict[str, str]'",
    "hook_documents_to_object_form": "callable hook_documents_to_object_form(docs: 'Sequence[dict]') -> 'dict[str, list[dict]]'",
    "install_agent": "callable rebuild_agent_config(*, clean: 'bool' = False, refresh_forks: \"bool | Literal['defer']\" = True, _wrote_out: 'list[bool] | None' = None, _held_out: 'list[bool] | None' = None) -> 'Path'",
    "invalid_disabled_flag": "callable invalid_disabled_flag(spec: 'Any') -> 'tuple[bool, Any]'",
    "is_managed_prompt": "callable is_managed_prompt(prompt: 'str') -> 'bool'",
    "is_markdown_spec": "callable is_markdown_spec(path: 'str | Path') -> 'bool'",
    "is_registered_agent_name": "callable is_registered_agent_name(value: 'object') -> 'bool'",
    "is_sensitive_path": "callable is_sensitive_path(path_str: 'str', base_dir: 'str | None' = None) -> 'bool'",
    "is_unc_shape": "callable is_unc_shape(raw: 'str') -> 'bool'",
    "isolated_agents_dir": "callable isolated_agents_dir(data_home: 'Path') -> 'Path'",
    "iter_agent_spec_files": "callable iter_agent_spec_files(directory: 'Path', *, ordered: 'bool' = True) -> 'list[Path]'",
    "kiro_agents_dir": "callable kiro_agents_dir() -> 'Path'",
    "kiro_agents_dir_path": "callable kiro_agents_dir_path() -> 'Path'",
    "kiro_oauth_wire_entry": "callable kiro_oauth_wire_entry(entry: 'dict[str, Any]', *, store_entry: 'dict[str, Any] | None', server: 'str' = '') -> 'dict[str, Any]'",
    "logger": "value host",
    "managed_mcp_spec_entry": "callable managed_mcp_spec_entry(name: 'str', *, include_opt_in: 'bool' = False) -> 'dict[str, Any] | None'",
    "markdown_spec_for_agent": "callable markdown_spec_for_agent(agent: 'str', work_dir: 'str | Path | None' = None) -> 'Path | None'",
    "may_skip_gate_now": "callable may_skip_gate_now(ref: 'str') -> 'bool'",
    "mcp_entries_muted": "callable mcp_entries_muted(entries: 'Iterable[Any]') -> 'bool'",
    "mcp_entry_is_muted": "callable mcp_entry_is_muted(entry: 'Any') -> 'bool'",
    "mcp_search_path": "callable mcp_search_path(env_path: 'str') -> 'str'",
    "mcp_server_alias": "callable mcp_server_alias(name: 'str') -> 'str'",
    "migrate_agent_specs": "callable migrate_agent_specs() -> 'int'",
    "missing_required_agent_specs": "callable missing_required_agent_specs() -> 'list[str]'",
    "normalize_spec_hooks": "callable normalize_spec_hooks(value: 'object') -> 'list[dict]'",
    "platform_compat": "module kiro_crew.platform_compat",
    "present_required_agent_specs": "callable present_required_agent_specs() -> 'list[tuple[str, Path]]'",
    "prime_ceiling_projection": "callable prime_ceiling_projection() -> 'None'",
    "project_agent_files": "callable project_agent_files(project_dir: 'str | Path | None', include_legacy: 'bool' = False, *, operation: 'str' = 'project_agent_files', source: 'str' = 'project_agent_files') -> 'list[Path]'",
    "project_agent_name": "callable project_agent_name(spec: 'Path') -> 'str'",
    "project_agent_names": "callable project_agent_names(project_dir: 'str | Path | None', *, operation: 'str' = 'project_agent_names', source: 'str' = 'project_agent_names') -> 'frozenset[str]'",
    "prune_dangling_tool_refs": "callable prune_dangling_tool_refs(config: 'dict', *, declared: 'Collection[str]' = (), declared_grants: 'Collection[str] | None' = None) -> 'list[str]'",
    "purge_deleted_proxy_from_config": "callable purge_deleted_proxy_from_config(config: 'dict') -> 'list[str]'",
    "rebuild_agent_config": "callable rebuild_agent_config(*, clean: 'bool' = False, refresh_forks: \"bool | Literal['defer']\" = True, _wrote_out: 'list[bool] | None' = None, _held_out: 'list[bool] | None' = None) -> 'Path'",
    "rebuild_agent_config_reporting": "callable rebuild_agent_config_reporting(*, _held_out: 'list[bool] | None' = None) -> 'tuple[Path, bool]'",
    "record_derived": "callable record_derived(entry: 'dict[str, Any]', command_source: 'tuple[str, str] | None') -> 'dict[str, Any]'",
    "recorded_source": "callable recorded_source(entry: 'object') -> 'tuple[str, str] | None'",
    "redact": "callable redact_via_context(text: 'str') -> 'str'",
    "redact_log": "callable redact_log_via_context(text: 'str') -> 'str'",
    "rederive_worker_agent": "callable rederive_worker_agent(reason: 'str') -> 'bool'",
    "repair_agent_configs": "callable repair_agent_configs() -> 'None'",
    "replace_with_retry": "callable replace_with_retry(src: 'Path | str', dst: 'Path | str') -> 'None'",
    "reproject_for_ceiling_change": "callable reproject_for_ceiling_change() -> 'None'",
    "require_fork_governance": "callable require_fork_governance(agent: 'str | None', project_dir: 'str | Path | None' = None) -> 'None'",
    "require_fresh_derived_spec": "callable require_fresh_derived_spec(agent: 'str | None', work_dir: 'str | Path | None') -> \"'DerivedSpecSnapshot | None'\"",
    "require_unchanged_derived_spec": "callable require_unchanged_derived_spec(snapshot: \"'DerivedSpecSnapshot | None'\", *, agent: 'str | None' = None) -> 'None'",
    "reset_agent_model": "callable reset_agent_model(name: 'str') -> 'tuple[Path, str]'",
    "run_first_run_setup": "callable run_first_run_setup() -> 'None'",
    "safe_context_call": "callable safe_context_call(fn: \"'Callable[[], _T]'\", *, fallback: '_T' = <object object at 0x>, fallback_factory: \"'Optional[Callable[[], _T]]'\" = None, log_message: \"'str | None'\" = None) -> '_T'",
    "safe_read_file_bytes_nolink": "callable safe_read_file_bytes_nolink(raw: 'str', within_root: 'str | None' = None, *, max_bytes: 'int | None' = None, allow_truncate: 'bool' = False, within_root_is_canonical: 'bool' = False, admit_hardlinked: 'Callable[[str, bytes], bool] | None' = None) -> 'bytes | None'",
    "sanitize_spec_env": "callable sanitize_spec_env(pairs: 'Iterable[tuple[str, str]]') -> 'dict[str, str]'",
    "sel": "callable sel() -> 'SecurityEventLog'",
    "shared_kiro_agents_writable": "callable shared_kiro_agents_writable() -> 'bool'",
    "shutil": "module shutil",
    "source_view": "callable source_view(entry: 'object') -> 'dict[str, Any]'",
    "spec_path_key": "callable spec_path_key(env: \"'Mapping[str, object]'\") -> 'str | None'",
    "strip_ungoverned_auto_approve": "callable strip_ungoverned_auto_approve(servers: 'Mapping[str, object]', *, audit: 'bool' = True, third_party: 'Container[str]' = (), honour_owner_written: 'bool' = True) -> 'Dict[str, object]'",
    "sync_aim_packages": "callable sync_aim_packages() -> 'None'",
    "unc_probe_allowed": "callable unc_probe_allowed(raw: 'str') -> 'bool'",
    "warn_invalid_disabled": "callable warn_invalid_disabled(name: 'str', value: 'Any', where: 'str') -> 'None'",
    "without_marker": "callable without_marker(entry: 'object') -> 'dict[str, Any]'",
}

#: Every ``kiro_crew`` collaborator the pre-split module imported, by (module, attribute).
BASE_COLLABORATORS: dict[str, tuple[str, str]] = {
    "AGENT_FILENAME": ("kiro_crew.agent_files", "AGENT_FILENAME"),
    "AmbiguousAgentSpecError": ("kiro_crew.agent_discovery", "AmbiguousAgentSpecError"),
    "CU_MCP_SERVER": ("kiro_crew.platform.governance", "CU_MCP_SERVER"),
    "DERIVED_KEY": ("kiro_crew.mcp_provenance", "DERIVED_KEY"),
    "FileTooLargeError": ("kiro_crew.hooks", "FileTooLargeError"),
    "MCP_PATH_HINT": ("kiro_crew.env", "MCP_PATH_HINT"),
    "OWNED_KIRO_AGENT_FILES": ("kiro_crew.agent_files", "OWNED_KIRO_AGENT_FILES"),
    "REQUIRED_KIRO_AGENT_FILES": ("kiro_crew.agent_files", "REQUIRED_KIRO_AGENT_FILES"),
    "SecurityEvent": ("kiro_crew.sel", "SecurityEvent"),
    "_CONDUCTOR_AGENT_FILENAME": ("kiro_crew.agent_files", "CONDUCTOR_AGENT_FILENAME"),
    "_GUEST_AGENT_FILENAME": ("kiro_crew.agent_files", "GUEST_AGENT_FILENAME"),
    "_HEARTBEAT_AGENT_FILENAME": ("kiro_crew.agent_files", "HEARTBEAT_AGENT_FILENAME"),
    "_KNOWLEDGE_AGENT_FILENAME": ("kiro_crew.agent_files", "KNOWLEDGE_AGENT_FILENAME"),
    "_LEDGER_CONDUCTOR_AGENT_FILENAME": (
        "kiro_crew.agent_files",
        "LEDGER_CONDUCTOR_AGENT_FILENAME",
    ),
    "_LITE_AGENT_FILENAME": ("kiro_crew.agent_files", "LITE_AGENT_FILENAME"),
    "_PIPELINE_CONDUCTOR_AGENT_FILENAME": (
        "kiro_crew.agent_files",
        "PIPELINE_CONDUCTOR_AGENT_FILENAME",
    ),
    "_RESEARCH_AGENT_FILENAME": ("kiro_crew.agent_files", "RESEARCH_AGENT_FILENAME"),
    "_SECURITY_CONDUCTOR_AGENT_FILENAME": (
        "kiro_crew.agent_files",
        "SECURITY_CONDUCTOR_AGENT_FILENAME",
    ),
    "_WORKER_AGENT_FILENAME": ("kiro_crew.agent_files", "WORKER_AGENT_FILENAME"),
    "_declared_project_agent_name": ("kiro_crew.agent_discovery", "_declared_project_agent_name"),
    "_in_ephemeral_tree": ("kiro_crew.config.paths", "_in_ephemeral_tree"),
    "_in_linked_git_worktree": ("kiro_crew.config.paths", "_in_linked_git_worktree"),
    "_mc_config_path": ("kiro_crew.config", "config_path"),
    "_read_agent_spec": ("kiro_crew.agent_discovery", "_read_agent_spec"),
    "_under_system_tmp": ("kiro_crew.config.paths", "_under_system_tmp"),
    "_valid_override_home": ("kiro_crew.config.paths", "_valid_override_home"),
    "agent_spec_candidates": ("kiro_crew.agent_spec_format", "agent_spec_candidates"),
    "agent_state": ("kiro_crew", "agent_state"),
    "agentcore_posture": ("kiro_crew.platform.governance", "agentcore_posture"),
    "ambient_agents_dir": ("kiro_crew.config.paths", "ambient_agents_dir"),
    "command_is_ours": ("kiro_crew.mcp_provenance", "command_is_ours"),
    "config_dir": ("kiro_crew.config", "config_dir"),
    "current_context": ("kiro_crew.platform", "current_context"),
    "dedup_path": ("kiro_crew.env", "dedup_path"),
    "describe_search_path": ("kiro_crew.env", "describe_search_path"),
    "emit_env": ("kiro_crew.env", "emit_env"),
    "governance_permits": ("kiro_crew.platform.governance_profiles", "governance_permits"),
    "invalid_disabled_flag": ("kiro_crew.mcp_cleanup", "invalid_disabled_flag"),
    "is_markdown_spec": ("kiro_crew.agent_spec_format", "is_markdown_spec"),
    "is_registered_agent_name": ("kiro_crew.validation", "is_registered_agent_name"),
    "is_sensitive_path": ("kiro_crew.security", "is_sensitive_path"),
    "is_unc_shape": ("kiro_crew.hooks", "is_unc_shape"),
    "isolated_agents_dir": ("kiro_crew.config.paths", "isolated_agents_dir"),
    "iter_agent_spec_files": ("kiro_crew.agent_spec_format", "iter_agent_spec_files"),
    "kiro_agents_dir": ("kiro_crew.config.paths", "kiro_agents_dir"),
    "kiro_oauth_wire_entry": ("kiro_crew.mcp_utils", "kiro_oauth_wire_entry"),
    "may_skip_gate_now": ("kiro_crew.platform.governance", "may_skip_gate_now"),
    "mcp_entries_muted": ("kiro_crew.mcp_cleanup", "mcp_entries_muted"),
    "mcp_entry_is_muted": ("kiro_crew.mcp_cleanup", "mcp_entry_is_muted"),
    "mcp_search_path": ("kiro_crew.env", "mcp_search_path"),
    "mcp_server_alias": ("kiro_crew.mcp_utils", "mcp_server_alias"),
    "platform_compat": ("kiro_crew", "platform_compat"),
    "project_agent_files": ("kiro_crew.agent_discovery", "project_agent_files"),
    "project_agent_name": ("kiro_crew.agent_discovery", "project_agent_name"),
    "project_agent_names": ("kiro_crew.agent_discovery", "project_agent_names"),
    "prune_dangling_tool_refs": ("kiro_crew.mcp_cleanup", "prune_dangling_tool_refs"),
    "purge_deleted_proxy_from_config": ("kiro_crew.mcp_cleanup", "purge_deleted_proxy_from_config"),
    "record_derived": ("kiro_crew.mcp_provenance", "record_derived"),
    "recorded_source": ("kiro_crew.mcp_provenance", "recorded_source"),
    "redact": ("kiro_crew.platform", "redact_via_context"),
    "redact_log": ("kiro_crew.platform", "redact_log_via_context"),
    "replace_with_retry": ("kiro_crew.atomic_write", "replace_with_retry"),
    "safe_context_call": ("kiro_crew.platform", "safe_context_call"),
    "safe_read_file_bytes_nolink": ("kiro_crew.hooks", "safe_read_file_bytes_nolink"),
    "sanitize_spec_env": ("kiro_crew.env", "sanitize_spec_env"),
    "sel": ("kiro_crew.sel", "sel"),
    "shared_kiro_agents_writable": ("kiro_crew.config.paths", "shared_kiro_agents_writable"),
    "source_view": ("kiro_crew.mcp_provenance", "source_view"),
    "spec_path_key": ("kiro_crew.env", "spec_path_key"),
    "strip_ungoverned_auto_approve": (
        "kiro_crew.platform.governance",
        "strip_ungoverned_auto_approve",
    ),
    "unc_probe_allowed": ("kiro_crew.hooks", "unc_probe_allowed"),
    "warn_invalid_disabled": ("kiro_crew.mcp_cleanup", "warn_invalid_disabled"),
    "without_marker": ("kiro_crew.mcp_provenance", "without_marker"),
}

#: Standard-library and typing names the pre-split module imported for its own use.
#: Nothing reads them through the facade, so they are not part of its surface.
IMPLEMENTATION_IMPORTS: frozenset[str] = frozenset(
    [
        "Any",
        "Collection",
        "Iterator",
        "Literal",
        "Mapping",
        "MutableMapping",
        "NamedTuple",
        "Sequence",
        "contextlib",
        "copy",
        "datetime",
        "fnmatchcase",
        "hashlib",
        "itertools",
        "json",
        "logging",
        "math",
        "os",
        "re",
        "stat",
        "sys",
        "tempfile",
        "threading",
        "timezone",
        "uuid",
    ]
)

_HOST_DEPENDENT = frozenset(
    {
        "_KIRO_MCP_JSON",
        "_CC_MCP_JSON",
        "_DEFAULT_KIRO_HOOKS_DIR",
        "_BUNDLED_CFG_DIR",
        "KIRO_AGENTS_DIR",
        "_KIROCREW_BIN",
        "logger",
    }
)

#: Module state that changes as the module runs, so its value is the test order's.
_RUNTIME_STATE = frozenset(
    {
        "_declined_home_warned",
        "_fork_refresh_failed",
        "_fork_refresh_pending",
        "_hooks_sanitized_mtimes",
        "_pending_projection_warned_generation",
        "_projected_ceiling_generation",
    }
)

#: Values compiled per platform at import, pinned for each platform they are built on:
#: the hook path allowlist admits ``\`` and ``:`` on Windows only.
_PER_PLATFORM: dict[str, dict[str, str]] = {
    "_SAFE_PATH_RE": {
        "posix": "value Pattern 0d1f4584e517d253",
        "windows": "value Pattern 92e93b93e927a8a7",
    },
}


def _platform() -> str:
    return "windows" if platform_compat.IS_WINDOWS else "posix"


_ADDRESS = re.compile(r"at 0x[0-9a-fA-F]+")


def _digest(value: Any) -> str:
    try:
        rendered = json.dumps(
            value,
            sort_keys=True,
            default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else repr(o),
        )
    except Exception:
        rendered = repr(value)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]


def _describe(name: str, value: Any) -> str:
    if isinstance(value, types.ModuleType):
        return f"module {value.__name__}"
    if inspect.isclass(value):
        fields = getattr(value, "_fields", None)
        suffix = f" fields={','.join(fields)}" if fields is not None else ""
        return f"class {value.__qualname__}{suffix}"
    if callable(value) and not isinstance(value, (dict, list, tuple)):
        signature = _ADDRESS.sub("at 0x", str(inspect.signature(value)))
        return f"callable {value.__qualname__}{signature}"
    if name in _HOST_DEPENDENT:
        return "value host"
    if name in _RUNTIME_STATE:
        return "value state"
    if name in _PER_PLATFORM:
        return "value platform"
    if name == "_MANAGED_MCP_SERVERS":
        shape = {
            "gates": {k: getattr(s.get("spec_gate"), "__name__", None) for k, s in value.items()},
            "opt_in": {k: bool(s.get("opt_in")) for k, s in value.items()},
            "shape": {k: sorted(s) for k, s in value.items()},
        }
        return "value dict " + json.dumps(shape, sort_keys=True)
    if _ADDRESS.search(repr(value)):
        # A lock, an event: the identity of a runtime object, not a value.
        return f"value {type(value).__name__}"
    return f"value {type(value).__name__} {_digest(value)}"


def test_every_pre_split_name_resolves_with_its_pre_split_shape() -> None:
    """A name the module offered is still offered, as the same kind of thing.

    Signature and qualified name for a callable, fields for a NamedTuple, and the JSON
    digest for a value: a moved function keeps its name and parameters, a moved
    constant keeps its bytes.
    """
    drifted = {}
    for name, expected in BASE_SURFACE.items():
        assert hasattr(agent, name), f"kiro_crew.agent.{name} no longer resolves"
        actual = _describe(name, getattr(agent, name))
        if name in _PER_PLATFORM:
            actual = _describe_value(getattr(agent, name))
            expected = _PER_PLATFORM[name][_platform()]
        if actual != expected:
            drifted[name] = (expected, actual)
    assert drifted == {}, f"names whose shape changed across the split: {drifted}"


def _describe_value(value: Any) -> str:
    return f"value {type(value).__name__} {_digest(value)}"


def test_the_hook_path_allowlist_keeps_both_platform_branches() -> None:
    """Both branches of the import-time ``_SAFE_PATH_RE`` choice, read off the source
    of the module that defines it, compile to the pre-split patterns, so the Windows
    branch is checked on every host rather than only on a Windows shard."""
    home = getattr(agent, "_EXPORTS", {}).get("_SAFE_PATH_RE", agent.__name__)
    source = Path(importlib.import_module(home).__file__ or "").read_text(encoding="utf-8")
    (choice,) = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.If)
        and ast.unparse(node.test).endswith("IS_WINDOWS")
        and "_SAFE_PATH_RE" in ast.unparse(node)
    ]

    def compiled(body: list[ast.stmt]) -> re.Pattern[str]:
        (assign,) = body
        assert isinstance(assign, ast.Assign) and ast.unparse(assign.targets[0]) == "_SAFE_PATH_RE"
        call = assign.value
        assert isinstance(call, ast.Call) and ast.unparse(call.func) == "re.compile"
        (pattern,) = call.args
        assert isinstance(pattern, ast.Constant) and isinstance(pattern.value, str)
        return re.compile(pattern.value)

    expected = _PER_PLATFORM["_SAFE_PATH_RE"]
    assert _describe_value(compiled(choice.body)) == expected["windows"]
    assert _describe_value(compiled(choice.orelse)) == expected["posix"]


def test_every_collaborator_is_still_the_source_object() -> None:
    """An imported collaborator the facade re-offers is the object its own module holds."""
    for name, (module, attribute) in BASE_COLLABORATORS.items():
        source = getattr(importlib.import_module(module), attribute)
        assert getattr(agent, name) is source, f"kiro_crew.agent.{name} is not {module}.{attribute}"


def test_the_surface_inventory_covers_every_pre_split_binding() -> None:
    """The inventory is a partition: surface, collaborators, or an implementation import."""
    overlap = set(BASE_SURFACE) & IMPLEMENTATION_IMPORTS
    assert overlap == set()
    assert set(BASE_COLLABORATORS) <= set(BASE_SURFACE)


def test_install_agent_is_the_rebuild_itself() -> None:
    """The backward-compatible alias is the same function object, not a wrapper."""
    assert agent.install_agent is agent.rebuild_agent_config


def test_the_legacy_skill_path_alias_is_the_discovery_itself() -> None:
    assert agent._aim_skill_paths is agent._all_skill_paths


# ── toolsSettings.subagent.availableAgents ──────────────────────────────────

#: The shapes a parent spec's ``subagent`` settings arrive in. The admission gate reads
#: them (omitted = allow all; a non-list or a malformed list = a declared, empty
#: allowlist; ``trustedAgents`` is never an allowlist), so every writer must hand each
#: one to disk exactly as it read it.
SUBAGENT_SHAPES: dict[str, dict[str, Any] | None] = {
    "omitted": None,
    "declared_list": {"availableAgents": ["kirocrew-worker", "review-*"]},
    "non_list": {"availableAgents": "kirocrew-*"},
    "malformed_list": {"availableAgents": [1, None, "ok-*", {"x": 1}]},
    "empty_list": {"availableAgents": []},
    "with_trusted": {"availableAgents": ["a-*"], "trustedAgents": ["a-1"]},
    "trusted_only": {"trustedAgents": ["kirocrew-worker"]},
    "unrelated_keys": {"availableAgents": ["x"], "futureKey": {"n": 1}},
}


def _tools_settings(shape: dict[str, Any] | None) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "execute_bash": {"deniedCommands": ["rm"], "autoAllowReadonly": True, "keep": 1},
        "fs_write": {"allowedPaths": ["~/w"]},
    }
    if shape is not None:
        settings["subagent"] = json.loads(json.dumps(shape))
    return settings


def _expected_after_normalize(shape: dict[str, Any] | None) -> dict[str, Any]:
    expected: dict[str, Any] = {"execute_bash": {"keep": 1}, "fs_write": {"allowedPaths": ["~/w"]}}
    if shape is not None:
        expected["subagent"] = json.loads(json.dumps(shape))
    return expected


@pytest.mark.parametrize("shape", sorted(SUBAGENT_SHAPES))
def test_the_legacy_strip_keeps_every_subagent_shape(shape: str) -> None:
    """The one ``toolsSettings`` normalizer removes exactly the two retired keys."""
    config = {"toolsSettings": _tools_settings(SUBAGENT_SHAPES[shape])}
    agent._strip_legacy_denied_commands(config)
    assert config["toolsSettings"] == _expected_after_normalize(SUBAGENT_SHAPES[shape])


@pytest.mark.parametrize("shape", sorted(SUBAGENT_SHAPES))
def test_the_atomic_writer_and_the_capped_reader_round_trip_it(shape: str, tmp_path: Path) -> None:
    """Written through the atomic writer and read back through the hardened reader."""
    spec = {"name": "parent", "toolsSettings": _tools_settings(SUBAGENT_SHAPES[shape])}
    path = tmp_path / "parent.json"
    agent._atomic_json_write(path, spec)
    assert path.read_text(encoding="utf-8") == json.dumps(spec, indent=2) + "\n"
    assert agent._read_spec_capped(path) == spec


@pytest.mark.parametrize("shape", sorted(SUBAGENT_SHAPES))
def test_spec_resolution_hands_back_the_shape_it_found(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", tmp_path)
    spec = {"name": "parent", "toolsSettings": _tools_settings(SUBAGENT_SHAPES[shape])}
    (tmp_path / "parent.json").write_text(json.dumps(spec), encoding="utf-8")
    path = agent.agent_spec_path("parent")
    assert path == tmp_path / "parent.json"
    assert agent._read_spec_capped(path)["toolsSettings"] == spec["toolsSettings"]


class _Rig:
    """A private agents directory with the machine-specific inputs pinned."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.agents = tmp_path / "agents"
        self.agents.mkdir()
        binary = tmp_path / "bin" / "kirocrew"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        self.home = Path(os.environ["KIROCREW_HOME"])
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", self.agents)
        monkeypatch.setattr(agent, "_KIROCREW_BIN", str(binary))
        monkeypatch.setattr(agent, "_KIRO_MCP_JSON", tmp_path / "kiro-global-mcp.json")
        monkeypatch.setattr(agent, "_DEFAULT_KIRO_HOOKS_DIR", tmp_path / "no-hooks")
        monkeypatch.setattr(
            "kiro_crew.apps.bridges._mcp_json_path", lambda: self.agents / "kirocrew.json"
        )
        monkeypatch.setattr(
            "kiro_crew.kiro_cli.installed_kiro_cli_version",
            lambda: SPEC_PERMISSIONS_MIN_VERSION,
        )

    def read(self, filename: str) -> dict[str, Any]:
        return json.loads((self.agents / filename).read_text(encoding="utf-8"))


@pytest.mark.parametrize("shape", sorted(SUBAGENT_SHAPES))
def test_a_rebuild_over_an_existing_spec_keeps_the_shape(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh path owns hooks and managed servers, never the user's settings."""
    rig = _Rig(tmp_path, monkeypatch)
    spec = {"name": "kirocrew", "toolsSettings": _tools_settings(SUBAGENT_SHAPES[shape])}
    (rig.agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
    agent.rebuild_agent_config()
    assert rig.read("kirocrew.json")["toolsSettings"] == _expected_after_normalize(
        SUBAGENT_SHAPES[shape]
    )


@pytest.mark.parametrize("shape", sorted(SUBAGENT_SHAPES))
def test_derived_specs_inherit_the_shape_from_the_template(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``agent.json`` overrides reach every spec derived from ``build_agent_config``.

    The conductors and the research agent start from ``build_agent_config``, so an
    operator's ``subagent`` settings land on them as written. The worker starts from
    the same template and mirrors only ``_WORKER_MIRRORED_SHAPES`` from the default
    spec on disk, which does not include ``toolsSettings`` -- so its settings are the
    template's, whatever the default spec on disk carries.
    """
    rig = _Rig(tmp_path, monkeypatch)
    override = {"toolsSettings": {"subagent": SUBAGENT_SHAPES[shape] or {}}}
    rig.home.mkdir(parents=True, exist_ok=True)
    (rig.home / "agent.json").write_text(json.dumps(override), encoding="utf-8")
    on_disk = {"name": "kirocrew", "toolsSettings": {"subagent": {"availableAgents": ["disk"]}}}
    (rig.agents / "kirocrew.json").write_text(json.dumps(on_disk), encoding="utf-8")
    built = agent.build_agent_config()
    assert built["toolsSettings"]["subagent"] == (SUBAGENT_SHAPES[shape] or {})
    agent.rebuild_agent_config()
    for derived in (
        "kirocrew-conductor.json",
        "kirocrew-ledger-conductor.json",
        "kirocrew-pipeline-conductor.json",
        "kirocrew-security-conductor.json",
        "kirocrew-research.json",
        "kirocrew-worker.json",
    ):
        assert rig.read(derived)["toolsSettings"] == built["toolsSettings"], derived
    assert rig.read("kirocrew.json")["toolsSettings"]["subagent"] == {"availableAgents": ["disk"]}


@pytest.mark.parametrize("shape", sorted(SUBAGENT_SHAPES))
def test_a_fork_refresh_keeps_the_shape(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private template copy keeps its settings through the governance refresh."""
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

    rig = _Rig(tmp_path, monkeypatch)
    cfg = KiroCrewConfig()
    cfg.agents = {"my-crew": KiroCrewAgentConfig(kiro_agent="my-crew")}
    cfg.save()
    settings = _tools_settings(SUBAGENT_SHAPES[shape])
    fork = {"name": "my-crew", "toolsSettings": settings, "mcpServers": {}}
    (rig.agents / "my-crew.json").write_text(json.dumps(fork), encoding="utf-8")
    agent_state.set_fork_info("my-crew", forked_from="kirocrew", private_to="my-crew")
    agent._refresh_forked_templates(gated_off=frozenset())
    assert rig.read("my-crew.json")["toolsSettings"] == settings
    assert "my-crew" not in agent._fork_refresh_failed


# ── the guest agent ─────────────────────────────────────────────────────────


def _guest_bytes(model: str) -> str:
    spec = {
        "name": "kirocrew-guest",
        "model": model,
        "tools": [],
        "mcpServers": {},
        "includeMcpJson": False,
        "prompt": agent.GUEST_AGENT_PROMPT,
    }
    return json.dumps(spec, indent=2) + "\n"


def test_the_guest_prompt_is_frozen() -> None:
    assert agent.GUEST_AGENT_PROMPT == (
        "You are answering a guest: a person the operator allowed to message this "
        "account, not the operator. Reply to what they ask, briefly and helpfully, "
        "from the conversation alone. You have no tools: you cannot run commands, read "
        "or write files, browse, or act on anything, so never claim to have done so. "
        "If a request needs any of that, say the account owner has to do it."
    )


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("kiro-model-x", "kiro-model-x"), ("", "auto")],
)
def test_the_guest_spec_follows_the_operator_model(
    configured: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", tmp_path)
    cfg = KiroCrewConfig()
    cfg.agent.model = configured
    cfg.save()
    agent._install_guest_agent()
    assert (tmp_path / GUEST_AGENT_FILENAME).read_text(encoding="utf-8") == _guest_bytes(expected)


def test_an_unreadable_config_gives_the_guest_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.config import loader

    def _broken() -> None:
        raise RuntimeError("unreadable")

    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", tmp_path)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(_broken))
    agent._install_guest_agent()
    assert (tmp_path / GUEST_AGENT_FILENAME).read_text(encoding="utf-8") == _guest_bytes("auto")


def test_the_background_install_writes_lite_then_guest(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(agent, "_install_lite_agent_fallback", lambda: order.append("lite"))
    monkeypatch.setattr(agent, "_install_guest_agent", lambda: order.append("guest"))
    agent._install_aim_capabilities()
    assert order == ["lite", "guest"]


def test_the_lite_spec_is_the_bare_background_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", tmp_path)
    monkeypatch.setattr(agent, "_background_agent_model", lambda: "auto")
    agent._install_lite_agent_fallback()
    assert json.loads((tmp_path / LITE_AGENT_FILENAME).read_text(encoding="utf-8")) == {
        "name": "kirocrew-lite",
        "model": "auto",
        "tools": [],
        "mcpServers": {},
        "prompt": "",
    }
    assert agent_state.get_cc_model("kirocrew-lite") == agent._BACKGROUND_CC_MODEL


# ── prompts and grants are spec bytes ───────────────────────────────────────

_PATROL_RECEIPT = (
    "*requested* confirms receipt only — do not retry it in the same turn.\n"
    "Confirm activation from the gateway arm notice or `monitor_inspect` on a later turn."
)


@pytest.mark.parametrize(
    "prompt",
    [
        "_CONDUCTOR_SYSTEM_PROMPT",
        "_PIPELINE_CONDUCTOR_SYSTEM_PROMPT",
        "_SECURITY_CONDUCTOR_SYSTEM_PROMPT",
    ],
)
def test_every_conductor_prompt_carries_the_patrol_receipt_rule(prompt: str) -> None:
    """The monitor-arm wording, line break included, in each conductor charter."""
    assert _PATROL_RECEIPT in getattr(agent, prompt)


def test_the_member_grants_extend_the_conductor_grants_in_order() -> None:
    assert agent._CONDUCTOR_DASHBOARD_GRANTS == (
        "@kirocrew-dashboard/chat_folder_tree",
        "@kirocrew-dashboard/chat_folder_create",
        "@kirocrew-dashboard/chat_folder_file_self",
        "@kirocrew-dashboard/session_create",
        "@kirocrew-dashboard/session_read_message",
        "@kirocrew-dashboard/session_status",
    )
    assert agent._MEMBER_DASHBOARD_GRANTS == agent._CONDUCTOR_DASHBOARD_GRANTS + (
        "@kirocrew-dashboard/session_send",
        "@kirocrew-dashboard/session_broadcast",
        "@kirocrew-dashboard/session_stop",
        "@kirocrew-dashboard/session_retry",
    )
    assert agent._MEMBER_PANEL_GRANTS == (
        "@kirocrew-panel/panel_templates",
        "@kirocrew-panel/panel_publish",
        "@kirocrew-panel/dashboard_fields",
        "@kirocrew-panel/dashboard_write",
        "@kirocrew-panel/dashboard_templates",
        "@kirocrew-panel/dashboard_preview",
        "@kirocrew-panel/dashboard_apply",
        "@kirocrew-panel/dashboard_rollback",
    )


def test_the_security_grants_extend_the_pipeline_grants() -> None:
    assert agent._PIPELINE_CONDUCTOR_DASHBOARD_GRANTS == (
        "@kirocrew-dashboard/chat_folder_tree",
        "@kirocrew-dashboard/chat_folder_create",
        "@kirocrew-dashboard/session_create",
        "@kirocrew-dashboard/session_read_message",
        "@kirocrew-dashboard/session_status",
    )
    assert agent._SECURITY_CONDUCTOR_DASHBOARD_GRANTS == (
        agent._PIPELINE_CONDUCTOR_DASHBOARD_GRANTS + ("@kirocrew-dashboard/chat_folder_file_self",)
    )


def test_no_conductor_is_granted_a_fleet_write_or_a_model_switch() -> None:
    conductor_tuples = (
        agent._CONDUCTOR_DASHBOARD_GRANTS,
        agent._PIPELINE_CONDUCTOR_DASHBOARD_GRANTS,
        agent._SECURITY_CONDUCTOR_DASHBOARD_GRANTS,
    )
    for grants in conductor_tuples:
        for verb in (
            "session_broadcast",
            "session_send",
            "session_stop",
            "session_retry",
            "session_set_model",
        ):
            assert f"@kirocrew-dashboard/{verb}" not in grants
    every = conductor_tuples + (agent._MEMBER_DASHBOARD_GRANTS, agent._MEMBER_PANEL_GRANTS)
    assert all("@kirocrew-dashboard/session_set_model" not in grants for grants in every)
    assert all("@kirocrew-dashboard/session_reload" not in grants for grants in every)
