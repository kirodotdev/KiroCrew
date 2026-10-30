"""KiroCrew kiro-cli agent configuration.

Generates and installs ``kirocrew.json`` into ``~/.kiro/agents/``.

Configuration files (edit these, then ``kirocrew setup --agent-only``):

  ``src/kiro_crew/config/defaults.json``
      Base agent config — tools, model, allowedTools, toolsSettings, etc.

  ``src/kiro_crew/config/prompt.md``
      System prompt.

  ``~/.kiro/crew/agent.json``
      User overrides merged on top of defaults (optional).

  ``~/.kiro/crew/prompt.md``
      User prompt override (optional, takes priority over shipped prompt).

Dynamic fields resolved at install time:
  - ``prompt`` — ``file://`` URI pointing to the prompt file
  - ``mcpServers.kirocrew-cron.command`` — absolute path to ``kirocrew`` binary

This module is the facade of the spec materialization. The code that composes,
writes, upgrades and rebuilds the specs, and most of their prompt and grant text, runs
on this module's globals from the ``kiro_crew.agent_parts`` owners (see that package for
what stays here). The rest is forwarded to owners in ``kiro_crew.agent_materialization``:
hook normalization (``kiro_hooks``), the managed-server policy (``managed_mcp``),
server keys and tool aliases (``mcp_aliases``), the governance ceiling
(``auto_approve``), MCP source projection (``mcp_sources``), the locked
default-spec write (``default_spec_commit``), fork refresh (``fork_refresh``) and
the derived agents (``service_agents``, ``conductor_agents``, ``worker_agent``,
``guide_platform``) and the first crewmate (``first_crewmate``).
Every moved name is re-exported here, so reading or patching
``kiro_crew.agent.<name>`` reaches it.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import importlib
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Iterator, Literal, MutableMapping

from kiro_crew import agent_state, platform_compat
from kiro_crew._facade_compose import compose as _facade_compose
from kiro_crew._facade_compose import rebind as _facade_rebind
from kiro_crew.agent_discovery import (
    SKILL_URI_PREFIX,
    AmbiguousAgentSpecError,
    _declared_project_agent_name,
    _read_agent_spec,
    project_agent_files,
    project_agent_name,
    project_agent_names,
)
from kiro_crew.agent_files import DASHBOARD_MANAGER_AGENT_NAME  # noqa: F401
from kiro_crew.agent_files import (
    AGENT_FILENAME,
)
from kiro_crew.agent_files import DASHBOARD_AUTHOR_AGENT_FILENAME as _DASHBOARD_AUTHOR_FILENAME
from kiro_crew.agent_files import HEARTBEAT_AGENT_FILENAME as _HEARTBEAT_AGENT_FILENAME
from kiro_crew.agent_files import (
    OWNED_KIRO_AGENT_FILES,
    REQUIRED_KIRO_AGENT_FILES,
)
from kiro_crew.agent_parts.kirocrew_launcher import (
    _KIROCREW_BIN,
    _bin_is_usable,
    _interpreter_runnable,
    _kirocrew_bin_subpath,
    _kirocrew_mcp_invocation,
    _launcher_works,
    _migrations_dir,
    _resolve_kirocrew_bin,
    _stale_mcp_purge_marker,
    _through_stable_link,
    ensure_kirocrew_on_path,
    run_first_run_setup,
)
from kiro_crew.agent_parts.shared_home import (
    _PROVENANCE_SPEC_CAP_BYTES,
    _PROVENANCE_TOTAL_BUDGET_BYTES,
    _audit_allowed_shared_write,
    _decline_shared_agent_home,
    _declined_home_warned,
    _existing_specs_are_mine,
    _rebuild_spec_install_admitted,
    _warn_declined_home_once,
)
from kiro_crew.agent_parts.spec_build import (
    _BACKGROUND_CC_MODEL,
    _MAIN_AGENT_NAME,
    _NATIVE_PROMPT_STUB,
    _SUPERSEDED_NATIVE_PROMPT_STUBS,
    _agent_identity_enabled,
    _agentcore_capability_permitted,
    _background_agent_model,
    _background_cc_model,
    _computer_use_spec_gate,
    _deep_merge,
    _extra_mcp_servers,
    _hook_diagnostic,
    _is_managed_prompt_pointer,
    _load_json,
    _project_dir,
    _prompt_path,
    _sel_hook_rejected,
    _sel_hooks_merged,
    _shipped_defaults,
    _shipped_prompt,
    _strip_legacy_denied_commands,
    _user_dir,
    _user_overrides_path,
    _user_prompt_path,
    build_agent_config,
    get_shipped_tools,
    is_managed_prompt,
)
from kiro_crew.agent_parts.spec_files import (
    KIRO_AGENTS_DIR,
    _atomic_json_write,
    _atomic_json_write_unguarded,
    _declined_foreign_spec_write,
    _notify_if_config_write,
    agents_spec_lock,
    kiro_agents_dir_path,
    missing_required_agent_specs,
    present_required_agent_specs,
    write_owner_derived_spec,
)
from kiro_crew.agent_parts.spec_grants import (
    _CONDUCTOR_BASE_WORK_GRANTS,
    _CONDUCTOR_CORE_GRANTS,
    _CONDUCTOR_DASHBOARD_GRANTS,
    _GUIDE_AUTO_GRANTS,
    _GUIDE_SERVER,
    _LEDGER_CONDUCTOR_WORK_GRANTS,
    _MATE_AUTO_GRANTS,
    _MATE_SERVER,
    _MEMBER_DASHBOARD_GRANTS,
    _MEMBER_PANEL_GRANTS,
    _PIPELINE_CONDUCTOR_CORE_GRANTS,
    _PIPELINE_CONDUCTOR_DASHBOARD_GRANTS,
    _SECURITY_CONDUCTOR_DASHBOARD_GRANTS,
    _WORKER_EXCLUDED_GRANTS,
    _WORKER_WORK_GRANTS,
)
from kiro_crew.agent_parts.spec_lookup import (
    _conflicting_spec_for,
    _project_shadow_of,
    _read_spec_capped,
    _spec_path_is_safe,
    _spec_path_shape_refusal,
    agent_spec_path,
    markdown_spec_for_agent,
)
from kiro_crew.agent_parts.spec_prompts import (
    _CONDUCTOR_SYSTEM_PROMPT,
    _CONDUCTOR_WORK_LEDGER_PROTOCOL,
    _DASHBOARD_MANAGER_SYSTEM_PROMPT,
    _PIPELINE_CONDUCTOR_SYSTEM_PROMPT,
    _SECURITY_CONDUCTOR_SYSTEM_PROMPT,
    _TEAM_LEAD_SYSTEM_PROMPT,
    _WORKER_SYSTEM_PROMPT,
    GUEST_AGENT_PROMPT,
)
from kiro_crew.agent_parts.spec_rebuild import (
    _PROFILE_WATCH_RAISE_RETRY_S,
    _PROFILE_WATCH_RETRY_S,
    _REPROJECT_LOCK,
    _answer_generation_after_profile_poll,
    _conductor_installers_held,
    _conductor_spec_held,
    _held_rebuild_would_repeat,
    _hold_cause,
    _install_aim_capabilities,
    _install_heartbeat_agent,
    _invalidate_projection_if_answer_moved,
    _lineage_unreadable_refusal,
    _pending_projection_warned_generation,
    _profile_watch_generation,
    _profile_watch_retry_at,
    _projected_ceiling_generation,
    _rebuild_answer_generation,
    _rebuild_incomplete,
    _reproject_holding_the_lock,
    _spec_unresolvable_refusal,
    _stem_claimant_fork,
    _tracks_rebuild_outcome,
    _watch_clock,
    _write_heartbeat_spec,
    ensure_agent_materialized,
    prime_ceiling_projection,
    rebuild_agent_config,
    rebuild_agent_config_reporting,
    reproject_for_ceiling_change,
    reproject_for_profile_change,
    require_fork_governance,
    retry_held_conductor_specs,
    sync_aim_packages,
)
from kiro_crew.agent_parts.spec_upgrades import (
    _claim_propagated_model,
    _hooks_sanitized_mtimes,
    _load_existing_config,
    _relocated_skill_uri,
    _sanitize_agent_hooks,
    clear_model_pin,
    migrate_agent_specs,
    migrate_relocated_skill_uris,
    repair_agent_configs,
    reset_agent_model,
)
from kiro_crew.agent_spec_format import (
    agent_spec_candidates,
    is_markdown_spec,
    iter_agent_spec_files,
)
from kiro_crew.atomic_write import read_json_or, replace_with_retry
from kiro_crew.config import config_dir
from kiro_crew.config import config_path as _mc_config_path
from kiro_crew.config.paths import (
    _in_ephemeral_tree,
    _in_linked_git_worktree,
    _under_system_tmp,
    _valid_override_home,
    ambient_agents_dir,
    isolated_agents_dir,
    kiro_agents_dir,
    shared_kiro_agents_writable,
)
from kiro_crew.env import (
    mcp_search_path,
    resolved_command_casing,
    saved_command_casing,
    spec_path_key,
)
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.owner_only_files import mkdirs_owner_only, write_text_owner_only
from kiro_crew.platform import (
    current_context,
)
from kiro_crew.platform import redact_log_via_context as redact_log
from kiro_crew.platform import (  # noqa: F401 - module attribute read by the platform wiring probe
    redact_via_context as redact,
)
from kiro_crew.platform import (
    safe_context_call,
)
from kiro_crew.platform.governance import agentcore_posture
from kiro_crew.platform.governance_profiles import governance_permits
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import (  # circular import: sel imports config which imports agent
    SecurityEvent,
    sel,
)
from kiro_crew.user_json import loads_user_json
from kiro_crew.validation import is_registered_agent_name

if TYPE_CHECKING:  # served by ``__getattr__`` at runtime; named here for mypy
    from kiro_crew.agent_materialization.auto_approve import (  # noqa: F401
        _apply_allowed_tools_ceiling,
        _ceiling_filtered_spec,
        _entry_is_the_declared_server,
        _filter_auto_approve,
        _may_auto_approve,
        _seed_kas_permissions,
        _spec_permissions_accepted,
        _strip_ungoverned_auto_approve,
        _write_derived_permissions,
        declared_auto_approve,
        may_skip_gate_now,
        strip_ungoverned_auto_approve,
    )
    from kiro_crew.agent_materialization.conductor_agents import (  # noqa: F401
        _CONDUCTOR_AGENT_FILENAME,
        _LEDGER_CONDUCTOR_AGENT_FILENAME,
        _PIPELINE_CONDUCTOR_AGENT_FILENAME,
        _SECURITY_CONDUCTOR_AGENT_FILENAME,
        DEPRECATED_AGENT_SPECS,
        _conductor_mcp_servers,
        _conductor_spec,
        _install_conductor_agent,
        _install_ledger_conductor_agent,
        _install_pipeline_conductor_agent,
        _install_security_conductor_agent,
    )
    from kiro_crew.agent_materialization.default_spec_commit import (  # noqa: F401
        _apply_operator_oauth_client,
        prune_dangling_tool_refs,
    )
    from kiro_crew.agent_materialization.first_crewmate import (  # noqa: F401
        create_first_crewmate_once,
        first_crewmate_was_created,
    )
    from kiro_crew.agent_materialization.fork_refresh import (  # noqa: F401
        _FORK_REFRESH_WAIT_SECS,
        SharedTemplateOverCeiling,
        _fork_refresh_count_lock,
        _fork_refresh_failed,
        _fork_refresh_lock,
        _fork_refresh_pending,
        _fork_refresh_settled,
        _refresh_forked_templates,
        _refresh_forked_templates_locked,
        fork_origin_is_owned,
        hold_spawn_gate,
        name_has_another_claimant,
        require_shared_template_within_ceiling,
    )
    from kiro_crew.agent_materialization.guide_platform import (  # noqa: F401
        grant_guide_platform_once,
        grant_mate_platform_once,
        mark_guide_platform_granted,
        mark_mate_platform_granted,
    )
    from kiro_crew.agent_materialization.kiro_hooks import (  # noqa: F401
        _CREW_ONLY_HOOK_EVENTS,
        _FILENAME_EVENT_SUFFIXES,
        _HOOK_EVENT_CANONICAL,
        _HOOK_HEADER_RE,
        _HOOK_HEADER_SCAN_LINES,
        _HOOK_SPEC_AUDIT_TAG,
        _HOOK_SUPPRESSED_CONFIRM,
        _HOOK_SUPPRESSED_DISABLED,
        _INTERNAL_HOOK_KEYS,
        _KAS_ACTION_TYPES,
        _KAS_DOCUMENT_FIELD_LIMITS,
        _KAS_DOCUMENT_FIELD_TYPES,
        _KAS_TRIGGER_CANONICAL,
        _KAS_TRIGGER_TO_EVENT,
        _LEGACY_KIROCREW_HOOK_KEYS,
        _MAX_HOOK_DESCRIPTION_LEN,
        _MAX_HOOK_NAME_LEN,
        _MAX_HOOK_PAYLOAD_LEN,
        _MAX_MATCHER_LEN,
        _MAX_SPEC_HOOK_DOCUMENTS,
        _MAX_TOTAL_USER_HOOKS,
        _MAX_USER_HOOKS_PER_EVENT,
        _SAFE_MATCHER_RE,
        _SAFE_PATH_RE,
        _VALID_HOOK_EVENTS,
        _apply_user_kiro_hooks,
        _autoimport_kiro_hooks,
        _event_for_hook_trigger,
        _hook_command_reaches_a_share,
        _hook_document_action,
        _hook_document_from_document,
        _hook_documents_from_array_form,
        _hook_matcher_ok,
        _infer_hook_event,
        _kiro_hooks_only,
        _merge_kiro_hooks,
        _parse_hook_script_headers,
        _resolved_hook_command,
        _validate_hook_command,
        hook_documents_suppressed_commands,
        hook_documents_to_object_form,
        is_unc_shape,
        normalize_spec_hooks,
        unc_probe_allowed,
        unsent_kiro_hook_rows,
    )
    from kiro_crew.agent_materialization.managed_mcp import (  # noqa: F401
        _HOME_DERIVING_ENV_KEYS,
        _LAUNCHER_EXEC_ENV_KEYS,
        _MANAGED_MCP_ENTRY_ITEM_TYPES,
        _MANAGED_MCP_ENTRY_KEYS,
        _MANAGED_MCP_ENTRY_VALUE_TYPES,
        _MCP_REGISTRY_TYPE,
        CU_MCP_SERVER,
        _enforce_managed_mcp_ownership,
        _gated_off_servers,
        _managed_mcp_env,
        _managed_opt_in_entry,
        _mcp_registry_mode,
        _mcp_server_emission_eligible,
        _mcp_spec_gate_open,
        _spec_gate_answers_closed,
        crew_owned_mcp_servers,
        emission_eligible_mcp_servers,
        managed_mcp_spec_entry,
        managed_member_transport,
        sanitize_spec_env,
    )
    from kiro_crew.agent_materialization.mcp_aliases import (  # noqa: F401
        DERIVED_KEY,
        _alias_family_base,
        _apply_connection_tool_aliases,
        _connection_tool_aliases_enabled,
        _durable_tool_aliases,
        _is_alias_family,
        _norm_mcp_spec,
        _normalize_mcp_server_keys,
        _reconcile_tool_aliases_from_disk,
        _set_tool_aliases,
        mcp_server_alias,
        purge_deleted_proxy_from_config,
    )
    from kiro_crew.agent_materialization.mcp_sources import (  # noqa: F401
        _SOURCE_OWNED_MCP_KEYS,
        MCP_PATH_HINT,
        _app_owned_mcp_keys,
        _collect_app_mcp_servers,
        _extra_mcp_scope_globals,
        _merge_source_owned,
        command_is_ours,
        dedup_path,
        describe_search_path,
        emit_env,
        entry_as_enabled,
        gateway_routed_names,
        invalid_disabled_flag,
        kiro_oauth_wire_entry,
        mcp_entries_muted,
        mcp_entry_is_muted,
        record_derived,
        recorded_source,
        source_view,
        warn_invalid_disabled,
        withheld_servers_now_routed,
        without_marker,
    )
    from kiro_crew.agent_materialization.service_agents import (  # noqa: F401
        _DASHBOARD_MANAGER_AGENT_FILENAME,
        _GUEST_AGENT_FILENAME,
        _KNOWLEDGE_AGENT_FILENAME,
        _LITE_AGENT_FILENAME,
        _MATE_CONDUCTOR_AGENT_FILENAME,
        _MATE_WORKER_AGENT_FILENAME,
        _RESEARCH_AGENT_FILENAME,
        _TEAM_LEAD_AGENT_FILENAME,
        _install_dashboard_manager_agent,
        _install_guest_agent,
        _install_knowledge_agent,
        _install_lite_agent_fallback,
        _install_mate_conductor_agent,
        _install_mate_worker_agent,
        _install_research_agent,
        _install_team_lead_agent,
        knowledge_agent_model,
        repair_knowledge_agent_spec,
    )
    from kiro_crew.agent_materialization.skill_resources import (  # noqa: F401
        map_prompt_skills,
        prompt_skill_uris,
    )
    from kiro_crew.agent_materialization.worker_agent import (  # noqa: F401
        _DEFAULT_SPEC_OBSERVATION_ATTEMPTS,
        _WORKER_AGENT_FILENAME,
        _WORKER_MIRRORED_KEYS,
        _WORKER_MIRRORED_SHAPES,
        DerivedSpecSnapshot,
        DerivedSpecStale,
        ForeignAgentSpec,
        _apply_worker_exclusions,
        _canonical_grant_pattern,
        _derived_spec_matches_default,
        _drop_servers,
        _excluded_verb,
        _file_identity,
        _foreign_dashboard_author_spec_reason,
        _foreign_worker_spec_reason,
        _glob_hits,
        _grant_reaches_excluded,
        _install_dashboard_author_agent,
        _install_worker_agent,
        _installed_default_spec,
        _is_confirmed_managed_dashboard_author,
        _pattern_reaches_excluded,
        _refuse_foreign_worker_spec,
        _require_fresh_worker_spec,
        _spec_fingerprint,
        _strip_excluded_auto_approve,
        _whole_server_ref,
        _worker_model_is_user_pinned,
        _worker_unassignable_servers,
        _write_worker_spec,
        default_spec_fingerprint,
        default_spec_identity,
        rederive_worker_agent,
        require_fresh_derived_spec,
        require_unchanged_derived_spec,
    )

logger = logging.getLogger(__name__)

# The ``kiro_crew.agent_parts`` owners' functions run on this module's globals, so a
# patch of ``kiro_crew.agent.<name>`` reaches them as it reached them when they lived here;
# see :mod:`kiro_crew.agent_parts`. Composed before any code below captures an owner
# function (the managed-server table holds one), and the two functions wrapped where they are defined are wrapped again here: a
# rebind reaches a wrapper, never the function its closure holds.
#: The composed owners, in the order this module imports them.
_PARTS = (
    "kiro_crew.agent_parts.kirocrew_launcher",
    "kiro_crew.agent_parts.shared_home",
    "kiro_crew.agent_parts.spec_build",
    "kiro_crew.agent_parts.spec_files",
    "kiro_crew.agent_parts.spec_grants",
    "kiro_crew.agent_parts.spec_lookup",
    "kiro_crew.agent_parts.spec_prompts",
    "kiro_crew.agent_parts.spec_rebuild",
    "kiro_crew.agent_parts.spec_upgrades",
)
_facade_compose(globals(), [sys.modules[part] for part in _PARTS])
agents_spec_lock = contextlib.contextmanager(
    _facade_rebind(getattr(agents_spec_lock, "__wrapped__"), globals())
)
rebuild_agent_config = install_agent = _tracks_rebuild_outcome(
    _facade_rebind(getattr(rebuild_agent_config, "__wrapped__"), globals())
)


class SharedAgentHomeRefused(OSError):
    """A spec write was refused because this instance does not own its directory.

    An ``OSError`` subclass on purpose, and that is the whole design: every
    caller that already treats "the spec could not be written" as a failure
    does so with ``except OSError`` — the dashboard's spec editor and MCP
    toggles, the read-only side spec, the Connections mint — so they report a
    refusal through the arm they already have, with no new contract to learn
    and none to forget. A caller with no arm propagates, which is the correct
    end for the paths that would otherwise record success for a file that was
    never written: a worker mirror stamped as re-derived, an agent name handed
    to kiro-cli whose spec is absent.

    Raised only when :func:`_declined_foreign_spec_write` is true, which needs a
    data home that does not own the shared agents directory. A correctly homed
    instance never reaches it, so this adds no failure mode to one.
    """


_KIRO_MCP_JSON = Path.home() / ".kiro" / "settings" / "mcp.json"
# Well-known Claude Code global MCP config. The core does not read this at
# rebuild/discovery/apply time (OSS is Kiro-only); a companion contributes it as
# a scope via the extra_mcp_scopes() CPP seam. Retained as the canonical path
# constant for that companion and for tests.
_CC_MCP_JSON = Path.home() / ".claude.json"

# Bundled fallback — inside the kiro_crew.config package
_BUNDLED_CFG_DIR: Path = Path(__file__).resolve().parent / "config"


# ---------------------------------------------------------------------------
# Managed MCP servers — single source of truth.
#
# Every server here is dynamically injected into the agent config at install
# time (both fresh and existing configs).  Adding a new managed server =
# one entry here.
#
# An entry may carry a ``spec_gate`` callable: a predicate consulted at spec
# EMISSION time, so a capability that is off (or impossible on this platform)
# costs no backend process rather than merely no tools.  Absent = always
# emitted, which is what the two always-on servers want.
# ---------------------------------------------------------------------------
_MANAGED_MCP_SERVERS: dict[str, dict] = {
    "kirocrew-cron": {"invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-cron")},
    "kirocrew-core": {"invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-core")},
    # Computer use (native desktop GUI automation).  ``spec_gate`` keeps the
    # entry out of the emitted spec unless the platform HAS a supported driver
    # AND the keystone primary enable is on, so kiro-cli never spawns the
    # backend for a feature that is
    # off or unsupported (see _computer_use_spec_gate).  The shim's own empty
    # ``tools/list`` while disabled is retained as defence in depth.
    #
    # DELIBERATELY NO ``autoApprove`` KEY, and none may ever be added: kiro-cli
    # approves an autoApproved MCP tool locally and emits no permission request,
    # so ``hooks.on_tool_call`` — the PreToolUse gate carrying the always-on deny
    # floor, the sensitive-path check and the governance ceiling — is NEVER
    # reached for it. For a tool that can click in an already-authenticated
    # application that would be a complete gate bypass.
    "kirocrew-computer": {
        "invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-computer"),
        "spec_gate": _computer_use_spec_gate,
    },
    # Dashboard control (sidebar folder tree + which sessions sit in it).
    # ``opt_in``: an ASSIGNABLE SET, not an always-on capability. The two loops
    # that write specs skip it, so the default agent's spec carries neither the
    # entry nor an ``@kirocrew-dashboard`` ref in ``tools`` — and kiro-cli loads a
    # server only when something references it, so a default session spends no
    # context on tools it never uses. An agent that should reorganize the
    # dashboard is granted the set in its own spec, and a refresh keeps that
    # grant's command current without ever re-granting it.
    #
    # No ``autoApprove`` key, for the same reason the computer server has none:
    # an autoApproved MCP tool is approved inside kiro-cli and never reaches
    # ``hooks.on_tool_call``, so the deny floor and governance ceiling would be
    # bypassed for tools that write to the user's session layout.
    "kirocrew-dashboard": {
        "invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-dashboard"),
        "opt_in": True,
    },
    # The conductor work ledger (a worker reports status; its conductor reads the
    # record and writes its own fields). ``opt_in`` for the same reason the
    # dashboard set is: almost no session is a conductor or a worker, and for the
    # rest the only reachable answer is ``not_bound`` or ``no_ledger`` — so both
    # spec-writing loops skip it and a session that never references the server
    # spends no context on four schemas it cannot use. The two agents that need it
    # (``kirocrew-worker`` and the two conductors) hand-build the entry, which IS
    # the explicit per-agent assignment an opt-in set requires.
    #
    # No ``autoApprove`` key, and none may ever be added — the same prohibition
    # the two servers above carry, for the same mechanism: kiro-cli approves an
    # autoApproved MCP tool locally and emits no permission request, so
    # ``hooks.on_tool_call`` (the always-on deny floor, the sensitive-path check,
    # the governance ceiling) is NEVER reached for it. A store that writes
    # agent-authored text into a record the user reads and a conductor decides
    # from is not the place to break that. Per-tool grants in ``allowedTools`` are
    # how the two halves get their approvals instead, and those still pass the
    # governance ceiling on the way in.
    "kirocrew-work": {
        "invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-work"),
        "opt_in": True,
    },
    # Read-only reads over the crew log (list the logs, page one, read a fold).
    # ``opt_in`` for the same reason as the two above: the crew log is an optional
    # subsystem behind a flag, and for a session that is not verifying or auditing
    # it the only reachable answer is ``crew_log_disabled`` or its own unit -- so
    # both spec-writing loops skip it and a session that never references the
    # server spends no context on three schemas it has no use for. An agent that
    # should read the log is granted the set in its own spec.
    #
    # No ``autoApprove`` key, and none may ever be added -- the same prohibition
    # the three servers above carry, for the same mechanism: kiro-cli approves an
    # autoApproved MCP tool locally and emits no permission request, so
    # ``hooks.on_tool_call`` (the always-on deny floor, the sensitive-path check,
    # the governance ceiling) is NEVER reached for it. These tools read a record
    # that carries the session's own message bodies; that is not the place to
    # break it.
    "kirocrew-crew-log": {
        "invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-crew-log"),
        "opt_in": True,
    },
    # Debug reads (five questions about a running gateway: which code it is, why a
    # call was refused, the interpreter's threads, the process family, the recorded
    # host series). ``opt_in`` for the same reason as the sets around it — a session
    # that is not debugging a gateway should spend no context on these schemas.
    #
    # No ``autoApprove`` key, and the reason is sharper here than anywhere else on
    # this list: these tools read HOST and CROSS-SESSION state, and an autoApproved
    # MCP tool never reaches ``hooks.on_tool_call``. The wide views are additionally
    # gated in the ROUTE to the owner's own dashboard tab, so the set is safe to
    # grant broadly while remaining narrow in what it will actually answer.
    "kirocrew-debug": {
        "invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-debug"),
        "opt_in": True,
    },
    # Agent panels (an agent publishes DATA describing its own state; the
    # dashboard renders it with a human-authored template in a sandboxed frame).
    # ``opt_in`` for the same reason the dashboard set is: this is an assignable
    # capability for long-running agents, and a default session must spend no
    # context on a tool it will never call.
    #
    # Its own server rather than a tool added to ``kirocrew-dashboard``, because
    # assignment is per server and that set is ratcheted to folder organization
    # plus session control. Publishing a document is neither, and folding it in
    # would widen a set the user granted for something else.
    #
    # No ``autoApprove`` key, for the reason the two sets above have none: an
    # autoApproved MCP tool is approved inside kiro-cli and never reaches
    # ``hooks.on_tool_call``, so the deny floor and governance ceiling would be
    # bypassed -- and this tool's input is derived from text the agent read
    # unattended.
    "kirocrew-panel": {
        "invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-panel"),
        "opt_in": True,
    },
    # The platform guide set: where-is answers (``find_ui``), the packaged docs
    # (``search_docs``), dashboard guides and change cards. A platform capability
    # of every crewmate and every dashboard session, so it is always emitted,
    # like the two servers at the top of this map; its own server so an operator
    # or a policy can withhold the whole set at once. Its tools need a dashboard
    # tab and refuse clearly without one (``mcp_guide``). Which of its tools run
    # without a prompt is :data:`_GUIDE_AUTO_GRANTS`, through ``allowedTools``.
    #
    # No ``autoApprove`` key, and none may ever be added -- the same prohibition
    # every set above carries, for the same mechanism: an autoApproved MCP tool is
    # approved inside kiro-cli and never reaches ``hooks.on_tool_call``, so the
    # deny floor and governance ceiling would be bypassed for a tool that steers
    # what the operator looks at and clicks.
    "kirocrew-guide": {
        "invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-guide"),
    },
    # A crewmate's own record (``mcp_mate``): its name, description and face
    # written at once with an undo for the user, and its goal, model, approval
    # mode and tools asked for through a change card. A platform capability of
    # every crewmate, so always emitted like the guide; its own server so an
    # operator or a policy can withhold the whole set at once, and so the guide
    # set stays what it is (the user's help with the dashboard). Every tool acts
    # on the CALLING crewmate only and refuses off its own dashboard turn
    # (``dashboard.handlers.member_self``). Which tools run without a prompt is
    # :data:`_MATE_AUTO_GRANTS`, through ``allowedTools``.
    #
    # No ``autoApprove`` key, and none may ever be added -- the same prohibition
    # every set above carries, for the same mechanism: an autoApproved MCP tool
    # never reaches ``hooks.on_tool_call``, so the deny floor and governance
    # ceiling would be bypassed for a tool that writes a crewmate's record.
    "kirocrew-mate": {
        "invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-mate"),
    },
    # Mediated secret requests (``mcp_secrets``): one tool,
    # ``call_api_with_secret``, that sends a write to an owner-authorized https
    # origin with a Custom secret the agent never sees. ``opt_in`` like the
    # assignable sets above: the trust boundary is the host endpoint, not the
    # mount, so an agent that is not granted the set loses nothing it could use,
    # and a default session spends no process and no context on it.
    #
    # No ``autoApprove`` key, and none may ever be added: an autoApproved MCP
    # tool is approved inside kiro-cli and never reaches ``hooks.on_tool_call``,
    # and the approval is where the single-use capability the host endpoint
    # demands is minted (``mediated_request_capability``).
    "kirocrew-secrets": {
        "invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-secrets"),
        "opt_in": True,
    },
}


def _all_skill_paths() -> list[str]:
    """Discover all skill directories (AIM, project, user).

    Returns directories containing SKILL.md files from:
    - ``~/.aim/skills`` and ``~/.aim/packages/*/skills`` (AIM-installed)
    - ``KIROCREW_PROJECT_DIR/skills`` (project-level)
    - ``~/.kiro/crew/skills`` (user-created)
    """
    paths: set[str] = set()
    # AIM skills — only known locations, not broad rglob.
    # TODO(aim-governance follow-up): this hardcoded ``~/.aim`` scan should
    # route through the ``McpToolingProvider.extra_skills()`` CPP seam (as the
    # dashboard skills catalog already does) so the agent-config rebuild and the
    # dashboard read the SAME source. Deferred to its own PR because of the
    # security-sensitive symlink-resolution + sensitive-path gating below.
    # OSS-inert today (no ``~/.aim`` tree on a vanilla install).
    aim_dir = Path.home() / ".aim"
    if aim_dir.is_dir():
        aim_skills = aim_dir / "skills"
        if aim_skills.is_dir():
            paths.add(str(aim_skills))
            # Resolve symlinks in local/ so skill loaders whose glob skips
            # symlinks can still find them: resolve each symlink target and
            # add its parent dir (only if named "skills").
            local_dir = aim_skills / "local"
            if local_dir.is_dir():
                for entry in local_dir.iterdir():
                    if entry.is_symlink():
                        try:
                            target = entry.resolve(strict=True)
                            parent = target.parent
                            if (
                                target.is_dir()
                                and parent.name == "skills"
                                and not is_sensitive_path(str(parent))
                            ):
                                paths.add(str(parent))
                            elif target.is_dir() and is_sensitive_path(str(parent)):
                                logger.debug(
                                    "Skipping sensitive path: %s",
                                    parent,
                                )
                                try:
                                    sel().log_api_access(
                                        caller="system",
                                        operation="skill_path_rejected",
                                        outcome="denied",
                                        source="agent",
                                        resources=str(parent),
                                        error="sensitive_path",
                                    )
                                except Exception:
                                    logger.debug(
                                        "Failed to emit SEL audit event for sensitive path rejection: %s",
                                        parent,
                                        exc_info=True,
                                    )
                            elif target.is_dir() and parent.name != "skills":
                                # `--local` skill installs always target a
                                # skills/ directory; non-standard layouts are
                                # intentionally skipped for consistency.
                                logger.debug(
                                    "Skipping symlink %s: parent %r is not 'skills'",
                                    entry.name,
                                    parent.name,
                                )
                        except OSError as exc:
                            logger.debug("Skipping unresolvable symlink %s: %s", entry, exc)
        aim_pkgs = aim_dir / "packages"
        if aim_pkgs.is_dir():
            for pkg in aim_pkgs.iterdir():
                if not pkg.is_dir() or pkg.name.startswith("."):
                    continue
                sd = pkg / "skills"
                if sd.is_dir():
                    paths.add(str(sd))
                # Nested variant: ~/.aim/packages/Pkg-1.0/eventId-XXX/skills/
                # Only load from currentEventId to avoid duplicates across snapshots.
                else:
                    manifest = pkg / ".aim" / ".version-manifest.json"
                    current_event = ""
                    if manifest.is_file():
                        manifest_data = read_json_or(
                            manifest, None, logger=logger, what="AIM version manifest"
                        )
                        if isinstance(manifest_data, dict):
                            current_event = manifest_data.get("currentEventId", "")
                    for sub in pkg.iterdir():
                        if not sub.is_dir() or sub.name.startswith("."):
                            continue
                        if current_event and sub.name != f"eventId-{current_event}":
                            continue
                        ssd = sub / "skills"
                        if ssd.is_dir():
                            paths.add(str(ssd))
    # Project-level skills (legacy ``<project>/skills/``)
    proj = _project_dir()
    if proj:
        sd = proj / "skills"
        if sd.is_dir():
            paths.add(str(sd))
        # Open-standard workspace location: ``<project>/.kiro/skills/`` —
        # what kiro-cli's native ``skill://`` loader scans.  Adding it here
        # so SkillsLoader sees the same set as kiro-cli does.
        kiro_proj = proj / ".kiro" / "skills"
        if kiro_proj.is_dir() and not is_sensitive_path(str(kiro_proj)):
            paths.add(str(kiro_proj))
    # User-created skills (KiroCrew convention)
    user_skills = config_dir() / "skills"
    if user_skills.is_dir():
        paths.add(str(user_skills))
    # Open-standard global location: ``~/.kiro/skills/`` — canonical home for
    # ``cp -r my-skill ~/.kiro/skills/`` installs and AIM-published skills
    # that follow the spec.  See docs/reference/kiro-cli/skills.md.
    kiro_user = Path.home() / ".kiro" / "skills"
    if kiro_user.is_dir() and not is_sensitive_path(str(kiro_user)):
        paths.add(str(kiro_user))
    return sorted(paths)


# Keep old name as alias for backward compat
_aim_skill_paths = _all_skill_paths


# Default hooks directory matches kiro-cli's discovery path.
_DEFAULT_KIRO_HOOKS_DIR = Path.home() / ".kiro" / "hooks"


# --------------------------------------------------------------------------- #
# Compatibility facade. The hook normalization that stood here, and the MCP
# projection, governance and derived-agent installers further down, live in
# ``kiro_crew.agent_materialization``; the table and forwarding type below keep
# every moved name readable and patchable as ``kiro_crew.agent.<name>``. The
# table is resolved (``_EXPORTS``) and consulted (``__getattr__``) only at the end
# of this module, once every name it binds is known, so a partial import of this
# module never forwards.
#
# A re-exported name is ABSENT from this module's own namespace on purpose:
# ``__getattr__`` runs only for a name the module does not hold, so a binding here
# would shadow the owner for every later read, and a patch of
# ``kiro_crew.agent.<name>`` would reach nothing the owner's code reads.
# --------------------------------------------------------------------------- #
#: Owner module -> every name this module re-exports from it, in the order the
#: owners load. A name an owner adds is reachable here only once it is listed.
_EXPORTS_BY_OWNER: dict[str, tuple[str, ...]] = {
    "kiro_crew.agent_materialization.kiro_hooks": (
        "_SAFE_PATH_RE",
        "_SAFE_MATCHER_RE",
        "_MAX_MATCHER_LEN",
        "_validate_hook_command",
        "_INTERNAL_HOOK_KEYS",
        "_VALID_HOOK_EVENTS",
        "_CREW_ONLY_HOOK_EVENTS",
        "_LEGACY_KIROCREW_HOOK_KEYS",
        "_kiro_hooks_only",
        "_MAX_USER_HOOKS_PER_EVENT",
        "_MAX_TOTAL_USER_HOOKS",
        "_HOOK_EVENT_CANONICAL",
        "_KAS_TRIGGER_CANONICAL",
        "_KAS_TRIGGER_TO_EVENT",
        "_KAS_ACTION_TYPES",
        "_HOOK_SPEC_AUDIT_TAG",
        "_MAX_SPEC_HOOK_DOCUMENTS",
        "_MAX_HOOK_NAME_LEN",
        "_MAX_HOOK_DESCRIPTION_LEN",
        "_MAX_HOOK_PAYLOAD_LEN",
        "_KAS_DOCUMENT_FIELD_TYPES",
        "_KAS_DOCUMENT_FIELD_LIMITS",
        "_hook_matcher_ok",
        "_event_for_hook_trigger",
        "_hook_document_action",
        "_hook_document_from_document",
        "_hook_documents_from_array_form",
        "normalize_spec_hooks",
        "_hook_command_reaches_a_share",
        "_resolved_hook_command",
        "_HOOK_SUPPRESSED_DISABLED",
        "_HOOK_SUPPRESSED_CONFIRM",
        "hook_documents_suppressed_commands",
        "hook_documents_to_object_form",
        "unsent_kiro_hook_rows",
        "_FILENAME_EVENT_SUFFIXES",
        "_HOOK_HEADER_SCAN_LINES",
        "_HOOK_HEADER_RE",
        "_parse_hook_script_headers",
        "_infer_hook_event",
        "_autoimport_kiro_hooks",
        "_merge_kiro_hooks",
        "_apply_user_kiro_hooks",
        "is_unc_shape",
        "unc_probe_allowed",
    ),
    "kiro_crew.agent_materialization.managed_mcp": (
        "_MCP_REGISTRY_TYPE",
        "_MANAGED_MCP_ENTRY_KEYS",
        "_MANAGED_MCP_ENTRY_VALUE_TYPES",
        "_MANAGED_MCP_ENTRY_ITEM_TYPES",
        "_HOME_DERIVING_ENV_KEYS",
        "_LAUNCHER_EXEC_ENV_KEYS",
        "_mcp_registry_mode",
        "_managed_mcp_env",
        "_mcp_spec_gate_open",
        "_spec_gate_answers_closed",
        "_mcp_server_emission_eligible",
        "emission_eligible_mcp_servers",
        "crew_owned_mcp_servers",
        "_gated_off_servers",
        "managed_mcp_spec_entry",
        "managed_member_transport",
        "_enforce_managed_mcp_ownership",
        "_managed_opt_in_entry",
        "CU_MCP_SERVER",
        "sanitize_spec_env",
    ),
    "kiro_crew.agent_materialization.mcp_aliases": (
        "_norm_mcp_spec",
        "_alias_family_base",
        "_is_alias_family",
        "_normalize_mcp_server_keys",
        "_connection_tool_aliases_enabled",
        "_apply_connection_tool_aliases",
        "_durable_tool_aliases",
        "_set_tool_aliases",
        "_reconcile_tool_aliases_from_disk",
        "DERIVED_KEY",
        "mcp_server_alias",
        "purge_deleted_proxy_from_config",
    ),
    "kiro_crew.agent_materialization.auto_approve": (
        "_entry_is_the_declared_server",
        "declared_auto_approve",
        "_strip_ungoverned_auto_approve",
        "_write_derived_permissions",
        "_spec_permissions_accepted",
        "_seed_kas_permissions",
        "_may_auto_approve",
        "_apply_allowed_tools_ceiling",
        "_ceiling_filtered_spec",
        "_filter_auto_approve",
        "may_skip_gate_now",
        "strip_ungoverned_auto_approve",
    ),
    "kiro_crew.agent_materialization.skill_resources": (
        "prompt_skill_uris",
        "map_prompt_skills",
    ),
    "kiro_crew.agent_materialization.mcp_sources": (
        "_extra_mcp_scope_globals",
        "_collect_app_mcp_servers",
        "_app_owned_mcp_keys",
        "_SOURCE_OWNED_MCP_KEYS",
        "_merge_source_owned",
        "MCP_PATH_HINT",
        "command_is_ours",
        "dedup_path",
        "describe_search_path",
        "emit_env",
        "entry_as_enabled",
        "gateway_routed_names",
        "invalid_disabled_flag",
        "kiro_oauth_wire_entry",
        "mcp_entries_muted",
        "mcp_entry_is_muted",
        "record_derived",
        "recorded_source",
        "source_view",
        "warn_invalid_disabled",
        "withheld_servers_now_routed",
        "without_marker",
    ),
    "kiro_crew.agent_materialization.default_spec_commit": (
        "_apply_operator_oauth_client",
        "prune_dangling_tool_refs",
    ),
    "kiro_crew.agent_materialization.fork_refresh": (
        "_fork_refresh_lock",
        "_fork_refresh_pending",
        "_fork_refresh_count_lock",
        "_fork_refresh_settled",
        "_fork_refresh_failed",
        "_FORK_REFRESH_WAIT_SECS",
        "_refresh_forked_templates",
        "_refresh_forked_templates_locked",
        "fork_origin_is_owned",
        "require_shared_template_within_ceiling",
        "SharedTemplateOverCeiling",
        "name_has_another_claimant",
        "hold_spawn_gate",
    ),
    "kiro_crew.agent_materialization.service_agents": (
        "_install_guest_agent",
        "_install_lite_agent_fallback",
        "_install_knowledge_agent",
        "knowledge_agent_model",
        "_install_mate_conductor_agent",
        "_install_mate_worker_agent",
        "_install_research_agent",
        "_install_team_lead_agent",
        "_install_dashboard_manager_agent",
        "repair_knowledge_agent_spec",
        "_DASHBOARD_MANAGER_AGENT_FILENAME",
        "_GUEST_AGENT_FILENAME",
        "_KNOWLEDGE_AGENT_FILENAME",
        "_LITE_AGENT_FILENAME",
        "_MATE_CONDUCTOR_AGENT_FILENAME",
        "_MATE_WORKER_AGENT_FILENAME",
        "_RESEARCH_AGENT_FILENAME",
        "_TEAM_LEAD_AGENT_FILENAME",
    ),
    "kiro_crew.agent_materialization.conductor_agents": (
        "_conductor_mcp_servers",
        "_conductor_spec",
        "_install_conductor_agent",
        "DEPRECATED_AGENT_SPECS",
        "_install_ledger_conductor_agent",
        "_install_pipeline_conductor_agent",
        "_install_security_conductor_agent",
        "_CONDUCTOR_AGENT_FILENAME",
        "_LEDGER_CONDUCTOR_AGENT_FILENAME",
        "_PIPELINE_CONDUCTOR_AGENT_FILENAME",
        "_SECURITY_CONDUCTOR_AGENT_FILENAME",
    ),
    "kiro_crew.agent_materialization.worker_agent": (
        "_WORKER_MIRRORED_SHAPES",
        "_WORKER_MIRRORED_KEYS",
        "_canonical_grant_pattern",
        "_whole_server_ref",
        "_pattern_reaches_excluded",
        "_glob_hits",
        "_excluded_verb",
        "_grant_reaches_excluded",
        "_apply_worker_exclusions",
        "_strip_excluded_auto_approve",
        "_worker_unassignable_servers",
        "_drop_servers",
        "_installed_default_spec",
        "_worker_model_is_user_pinned",
        "_install_worker_agent",
        "_write_worker_spec",
        "_DEFAULT_SPEC_OBSERVATION_ATTEMPTS",
        "DerivedSpecSnapshot",
        "DerivedSpecStale",
        "ForeignAgentSpec",
        "_foreign_worker_spec_reason",
        "_foreign_dashboard_author_spec_reason",
        "_is_confirmed_managed_dashboard_author",
        "_refuse_foreign_worker_spec",
        "default_spec_fingerprint",
        "_spec_fingerprint",
        "default_spec_identity",
        "_file_identity",
        "_derived_spec_matches_default",
        "require_fresh_derived_spec",
        "require_unchanged_derived_spec",
        "_require_fresh_worker_spec",
        "rederive_worker_agent",
        "_install_dashboard_author_agent",
        "_WORKER_AGENT_FILENAME",
    ),
    "kiro_crew.agent_materialization.guide_platform": (
        "grant_guide_platform_once",
        "grant_mate_platform_once",
        "mark_guide_platform_granted",
        "mark_mate_platform_granted",
    ),
    "kiro_crew.agent_materialization.first_crewmate": (
        "create_first_crewmate_once",
        "first_crewmate_was_created",
    ),
}


def _index_exports() -> dict[str, str]:
    """Invert :data:`_EXPORTS_BY_OWNER`, refusing a name with two homes."""
    index: dict[str, str] = {}
    for module_name, names in _EXPORTS_BY_OWNER.items():
        for name in names:
            if name in index or name in globals():
                raise RuntimeError(f"agent name {name!r} has two owners")
            index[name] = module_name
    return index


def _owner(name: str) -> ModuleType:
    """Return the module that owns re-exported *name*, resolved on each access.

    ``importlib.import_module`` is the resolution rather than a mapping kept here.
    It answers from :data:`sys.modules`, the one place a module is stored, so a
    purged or replaced owner is seen at once; and it waits on that module's import
    lock while its body is still running, where a bare ``sys.modules`` read would
    hand a thread a half-built owner another thread is still importing.
    """
    return importlib.import_module(_EXPORTS[name])


def _refuse_module_rebind(name: str, current: object) -> None:
    """Refuse to rebind or delete a MODULE through this namespace, loudly.

    Every owner imports its own binding of the modules it uses, so replacing
    ``agent.json`` or ``agent.kiro_hooks`` here would reach one reader and leave the
    others on the real module -- a patch that silently misses. Its attributes are
    shared by every reader, so that is what a test patches.
    """
    label = getattr(current, "__name__", name)
    raise AttributeError(
        f"{__name__}.{name} is the shared module {label!r}; rebinding it here "
        f"would reach only one agent-materialization module. Patch its attributes instead."
    )


class _ReExportModule(ModuleType):
    """Send a write or delete of a re-exported name to the module that owns it.

    Binding it here instead would shadow the owner for every later read, because
    ``__getattr__`` runs only for a name this module does not hold. Forwarded, a
    ``monkeypatch`` or ``mock.patch`` round-trips: ``mock.patch`` restores a name
    this module does not hold by deleting it and setting it back. With
    ``create=True`` it skips the set, which would leave the owner without the name,
    so ``test/test_agent_refactor_create_guard.py`` fails on any such patch.

    A name in :data:`_MODULE_NAMES` is refused both ways, by name: writing anything
    but the module it already holds, or deleting it. Any other name takes any value,
    a module included, and gives it back.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _MODULE_NAMES and value is not getattr(self, name, None):
            _refuse_module_rebind(name, getattr(self, name, None))
        if name in _EXPORTS:
            setattr(_owner(name), name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _MODULE_NAMES:
            _refuse_module_rebind(name, getattr(self, name, None))
        if name in _EXPORTS:
            delattr(_owner(name), name)
        else:
            super().__delattr__(name)


def _refresh_dynamic_fields(
    config: dict, *, gated_off: "frozenset[str] | None" = None, fork: bool = False
) -> None:
    """Update security-critical and dynamic fields in an existing config.

    Called when ``kirocrew.json`` already exists so user customizations are
    preserved while security controls and runtime paths stay current.

    Args:
        gated_off: Managed servers whose ``spec_gate`` is closed. Pass the
            caller's snapshot so one rebuild's emit path and its withhold audit
            agree; omitted, it is evaluated here.
        fork: The config is a crew's private COPY of an owned template
            (see ``agent_state`` fork lineage). The copy exists precisely so
            human edits stop landing on the shared file, so three writes that
            are correct for ``kirocrew.json`` are wrong here and are skipped:
            the unconditional prompt overwrite (only refreshed while the value
            is still the machine-shaped managed ``file://`` pointer or a
            superseded stub spelling, and then to ``_NATIVE_PROMPT_STUB``),
            the legacy
            ``deniedCommands`` strip (on a fork that field IS the user's
            guardrails, not an old build's injection), and the global
            ``agent.model`` propagation (a main-agent setting; stamping it on
            every fork would override the fork's own pin). Everything else —
            managed MCP commands, security hooks, the data-home pin — applies
            identically, which is the whole reason forks are refreshed at all.
    """
    # Prompt field — always refreshed at install time. On the main agent it is
    # ``_NATIVE_PROMPT_STUB`` (see its definition for why the spec prompt is a
    # stub). On a fork the heal rewrites the value to that same stub, but only
    # while the value is positively MANAGED: it equals the current
    # machine-shaped URI, it is a stale spelling of a place the managed
    # prompt has actually LIVED — under a crew data home or inside the
    # installed package (a moved data home / upgraded wheel, the repairs this
    # branch exists for) — or it is a superseded stub spelling an older build
    # wrote. A fork left on the pointer would deliver the persona twice —
    # natively from the file and again via injection; one left on an old stub
    # would keep the weaker system-role framing. Identity comes from those
    # locations and exact spellings, never from the basename alone: the
    # managed file is called ``prompt.md``, the single most natural name for a
    # CUSTOM prompt too, so name matching would silently and irrecoverably
    # rewrite real user references. A custom pointer that goes stale is left
    # alone — not healing preserves the user's path; healing destroys it.
    if not fork:
        config["prompt"] = _NATIVE_PROMPT_STUB
    else:
        current = str(config.get("prompt") or "")
        if _is_managed_prompt_pointer(current) or current in _SUPERSEDED_NATIVE_PROMPT_STUBS:
            config["prompt"] = _NATIVE_PROMPT_STUB

    # Managed MCP servers — ensure present and up-to-date.
    # Only refresh command/args; preserve user customizations (e.g. autoApprove).
    mcp = config.setdefault("mcpServers", {})
    registry_mode = managed_mcp._mcp_registry_mode()
    if gated_off is None:
        gated_off = managed_mcp._gated_off_servers()
    managed_mcp.refresh_managed_servers(mcp, gated_off=gated_off, registry_mode=registry_mode)

    # Edition-contributed MCP servers (PlatformContext).  Seed a missing entry
    # whole.  On an existing entry the edition owns only the invocation
    # (``command``/``args``, which can name a versioned interpreter that a later
    # install deletes), so refresh those and keep every other key the user set
    # (env, autoApprove, disabled, ...).  A non-object entry (null included)
    # occupies the name as the user's and is left alone.  Standalone
    # contributes {} (unchanged).
    for name, extra_spec in _extra_mcp_servers().items():
        if name not in mcp:
            mcp[name] = dict(extra_spec)
            continue
        entry = mcp[name]
        if isinstance(entry, dict):
            for key in ("command", "args"):
                if key in extra_spec:
                    value = extra_spec[key]
                    entry[key] = list(value) if isinstance(value, list) else value

    # Security: hooks always from bundled config.
    # Hard-fail if bundled defaults are missing — deny-by-default.
    bundled = _load_json(_BUNDLED_CFG_DIR / "defaults.json")
    if bundled is None:
        raise RuntimeError(
            "Cannot refresh security fields: bundled defaults.json is missing or unreadable"
        )
    if not isinstance(bundled, dict):
        raise RuntimeError(
            "Cannot refresh security fields: bundled defaults.json is not a JSON object"
        )

    bundled_hooks = bundled.get("hooks")
    if not bundled_hooks:
        raise RuntimeError("Cannot refresh security fields: hooks missing from bundled defaults")
    config["hooks"] = kiro_hooks._kiro_hooks_only(bundled_hooks)

    # Upgrade cleanup: drop the retired deniedCommands/autoAllowReadonly that an
    # older build injected into this existing config, so kiro-cli stops enforcing
    # the stale list ahead of the hooks gate (see _strip_legacy_denied_commands).
    # Not on a fork: there the field is the user's own guardrails.
    if not fork:
        _strip_legacy_denied_commands(config)

    # Merge user-defined kiro_hooks from ~/.kiro/crew/config.json (additive).
    mc_cfg = _load_json(_mc_config_path()) or {}
    kiro_hooks._apply_user_kiro_hooks(config, mc_cfg)

    # Model migration — replace deprecated model names with current equivalents.
    # Uses the canonical map from chat.py plus legacy pre-4.6 models.
    _model_migration = {
        "claude-opus-4.6-1m": "claude-opus-4.6",
        "claude-sonnet-4.6-1m": "claude-sonnet-4.6",
    }
    cur_model = config.get("model", "")
    if cur_model in _model_migration:
        config["model"] = _model_migration[cur_model]

    # Self-heal: lift any stray KiroCrew bookkeeping keys into the sidecar and
    # strip them from the spec so kiro-cli (deny_unknown_fields) accepts it.
    # This is the steady-state safety net that cleans specs polluted by older
    # builds on the next refresh; the one-time migrate_agent_specs() at startup
    # handles the rest of ~/.kiro/agents/.
    name = config.get("name") or _MAIN_AGENT_NAME
    agent_state.lift_and_strip_bookkeeping(config, name)

    # Imported lazily: config.loader imports this module, so a top-level import
    # would close the cycle. Warm by the time this runs (importing agent pulls
    # config.loader in), so the lookup costs nothing on the caller's thread.
    from kiro_crew.config.loader import DEFAULT_MODEL, coerce_config_field, normalize_agent_model

    # Default-model tracking: when the model is managed (not an explicit user
    # pick), re-sync it from the shipped defaults.json so a default bump
    # propagates to existing installs. Agents with no sidecar entry are
    # grandfathered and left untouched (never force-changed).
    #
    # The assignment is unconditional for a managed spec, falling back to the
    # inherit sentinel when the template pins nothing: "track the shipped
    # default" and "pin whatever happens to be in the spec already" are not the
    # same state, and only the sentinel makes a managed spec converge on the
    # same value a clean install writes. It is also what lets the global below
    # return to "auto" — leaving the field alone here would strand a concrete
    # model that this propagation itself wrote, and a spec pin outranks the
    # global in resolve_effective_model, so "auto" would be unreachable from the
    # configuration surface. Writing the sentinel rather than deleting the key
    # is equivalent to the resolver (normalize_agent_model collapses "auto" and
    # an absent key to the same "inherit") and keeps the spec shaped like the
    # shipped template.
    #
    # The read is strict because this answer feeds the write just below. The
    # lenient default folds an unreadable sidecar into ``None``, the same value
    # as "no opinion recorded", so a transient read error would decide the
    # model by accident. An unreadable sidecar HOLDS instead: the model stays
    # as the spec has it, which a managed spec converges from on the next
    # readable pass, so the hold costs one pass and nothing is left unstartable.
    try:
        managed = agent_state.get_model_managed(name, strict=True)
    except (OSError, ValueError):
        logger.warning(
            "Agent state sidecar unreadable; leaving the %s spec's model as it is "
            "rather than re-syncing it from the shipped default on unknown ownership",
            name,
            exc_info=True,
        )
        managed = None
    if managed:
        shipped_model = (_load_json(_shipped_defaults()) or {}).get("model")
        config["model"] = shipped_model or DEFAULT_MODEL

    # config.json agent.model is the user-facing authority (kirocrew config set
    # agent.model). An explicit pick (not the "auto" sentinel) is propagated into
    # the agent file so kiro-cli's --agent startup load matches it; otherwise the
    # stale agent-file model shadows config.json and session/set_model loses the
    # startup race. "auto" defers to managed/shipped resolution above.
    #
    # Read through normalize_agent_model, the resolver's own chokepoint for
    # hand-edited values: it collapses "auto", surrounding whitespace and any
    # non-string to "" (inherit). That keeps this branch's notion of "the global
    # defers" identical to the resolver's, and it is what stops a junk value
    # (` auto `, an int) from reaching a spec kiro-cli validates with
    # deny_unknown_fields — a spec it rejects wholesale, silently falling back to
    # the default agent.
    mc_model = normalize_agent_model(coerce_config_field(mc_cfg, "agent", dict, {}).get("model"))
    if mc_model and not fork:
        config["model"] = mc_model

    # Ensure kiro-cli uses agent-level mcpServers exclusively (not global
    # mcp.json).  Existing configs created before this field was added lack
    # it, causing kiro-cli to fall back to the (possibly empty) global file.
    config.pop("useLegacyMcpJson", None)
    config["includeMcpJson"] = False

    # Seed workspace-relative resources (steering files, AGENTS.md, etc.)
    # only when the user hasn't customized them.  kiro-cli normalizes
    # missing ``resources`` to ``[]`` on read, so existing users created
    # before this field shipped end up with an empty list that prevents
    # ``.kiro/steering/**/*.md`` and friends from auto-loading.  If the user
    # has explicitly listed their own resources, leave them alone.
    bundled_resources = bundled.get("resources")
    if isinstance(bundled_resources, list) and bundled_resources and not config.get("resources"):
        config["resources"] = list(bundled_resources)

    # tools/allowedTools: user-owned and otherwise NOT modified on existing
    # configs.  Narrow exception (ADD-only): ensure the ``tool_search`` built-in
    # grant is present.  kiro-cli only activates MCP Tool Search when the
    # ToolSearch built-in is in the agent's tools list; without the grant, the
    # per-session overlay written for AgentConfig.tool_search (enabled +
    # minPct=0/minTokens=0) is a no-op and full MCP tool specs are sent every
    # turn.  Existing configs created before ``tool_search`` shipped in
    # defaults.json never gain it otherwise, because the tools list is preserved
    # above.  It is a read-only, auto-allowed built-in (permission eval => Allow)
    # so no ``allowedTools`` entry is needed.  Gated on the shipped template
    # actually granting it (so an edition that drops it is respected) and scoped
    # to this single tool; the feature's on/off remains the AgentConfig.tool_search
    # toggle.  Never removes a tool and never reorders the rest.
    tools = config.get("tools")
    if (
        isinstance(tools, list)
        and "tool_search" in (bundled.get("tools") or [])
        and "tool_search" not in tools
    ):
        tools.append("tool_search")


class ForkGovernanceUnresolved(RuntimeError):
    """A fork-backed agent may not start: fork governance is not projected."""


_KNOWLEDGE_SYSTEM_PROMPT = (
    "You are a knowledge extraction specialist for KiroCrew's Knowledge Library. "
    "Your job is to analyze documents and extract structured information.\n\n"
    "You ALWAYS output valid JSON. No markdown, no explanation — just the JSON object.\n\n"
    "Be precise with entity names — use canonical forms (e.g., 'DynamoDB' not 'dynamo' or 'DDB').\n"
    "Only extract entities explicitly mentioned in the text, do not infer.\n"
    "Relations must reference entities that appear in your entities list."
)


_RESEARCH_SYSTEM_PROMPT = """# KiroCrew Research Worker

You are `kirocrew-research`, an autonomous research worker. You run ONE research
cycle per turn inside an autonudge loop, then end your turn — the next cycle fires
automatically. The Research Lab app drives you; the nudge names the campaign and dir.

## Per-cycle protocol (strict order)
1. Status check (first action): read `<dir>/status.json`. If status is not
   `running`, stop and end the turn.
2. Brief: read `<dir>/brief.md` for the question, sub-questions, and allowed sources.
3. Guidance: if `<dir>/guidance.txt` exists, read it, incorporate it, then delete it.
4. Orient (compact): skim only the one-line `summary`/`key_insight` of existing
   `findings/cycle_*.json` and the `## Research State` section of `FINDINGS.md` —
   NOT the full findings. Note what's answered, what's weak, and which leads are open.
   RECOVERY: if the dir looks emptier than the conversation implies (e.g. you
   recall completing a cycle but no matching `cycle_*.json` is on disk), a prior
   cycle's write was dropped mid-turn (connection loss / gateway restart). Re-derive
   that lost finding from context and write it to disk THIS cycle under the correct
   `cycle_NNN.json` name — do NOT invent a new naming scheme to "save" the work.
5. Decide direction: choose the single highest-value next step toward the question —
   a sub-question, a follow-up a prior finding surfaced, or shoring up weak evidence.
   Steer toward closing the goal; don't just walk the list.
6. Investigate that one step using one source/tool.
7. Record: write `findings/cycle_NNN.json` where **NNN = the count of existing
   `findings/cycle_*.json` files, zero-padded to 3 digits** (first cycle ->
   `cycle_000.json`, next -> `cycle_001.json`, ...). NEVER reuse or overwrite an
   existing cycle file. The filename pattern is a HARD contract: the Research Lab
   counts findings and detects completion by matching cycle-numbered JSON names
   (`cycle_NNN.json`). A finding written under any other name (e.g. a
   descriptive `01-topic.md`) is INVISIBLE — the campaign will show 0 findings and appear stalled even though
   your work is on disk. When in doubt, match `cycle_NNN.json` exactly. Keys:
   `cycle` (= NNN), `summary, sources_checked, sources_empty, new_findings_count,
   evidence_strength, key_insight, sub_question`; append the cycle to `FINDINGS.md`
   with citations; then rewrite its short `## Research State` (open questions,
   leads, dead-ends, weak spots) for the next cycle.
8. End the turn.

## Evidence strength
- `strong`: corroborated by 2+ independent sources
- `moderate`: a single source
- `weak`: inferred/speculative, no direct source

## Rules
- Be honest about `new_findings_count` (0 if nothing new this cycle).
- Never fabricate sources or findings; cite everything with a URL or path.
- Sources: use `web_search`/`web_fetch` for the public web. The local codebase
  (`grep`/`code`/`fs_read`) and the user's Knowledge Library are first-class
  sources too — search them when the question touches the user's own projects
  or saved documents.
- One cycle = one step. The compact summaries are your memory — do not re-read
  full prior findings.
- If brief.md lists sub-questions, they are the AUTHORITATIVE checklist — answer
  each; do NOT generate your own initial set. If brief.md lists none, derive
  sub-questions yourself from the question and scope. Use FIRST PRINCIPLES to steer
  which open sub-question (or weak-evidence gap) to pursue each cycle. When a
  finding surfaces a genuinely new high-value angle not in the checklist, you MAY
  propose it as an emergent sub-question in `emergent_questions.json`, in the
  shape brief.md gives; the campaign admits it to the checklist.
- Follow brief.md's questions directive: when allowed, you MAY pause with ONE
  high-leverage clarification question — write it to questions.json in the
  shape brief.md gives, and end the turn — when the goal or scope is genuinely ambiguous
  in a way that would materially change your research direction. Keep the bar high:
  proceed on a best-reasoned assumption (and record it) for anything minor or that
  you can resolve yourself.
- If `brief.md` defines a **Definition of Done**, verify against it each cycle using
  your tools (run tests, review code, run the eval) and record
  `verification: {passed: bool, detail: "..."}` in the finding. The campaign
  auto-completes when `passed` is true.
- On the final cycle (`cycle == max_cycles - 1`), write an executive summary +
  recommendation at the TOP of `FINDINGS.md` instead of new research.
"""


_HEARTBEAT_SYSTEM_PROMPT = """# KiroCrew Heartbeat Worker

You are `kirocrew-heartbeat`, an unattended polling worker that runs one task
per heartbeat cycle. You are dispatched by HeartbeatService when a task line in
`HEARTBEAT.md` is due to run; the gateway delivers your response text directly
to the user as a notification (no `send_message` call required, no chat panel
to write to).

## Charter

- **Observe and report only.** Heartbeat tasks watch for a condition (a build
  status, a file change, an external page state). When you see it, report.
  When you don't, respond with `HEARTBEAT_KEEP` so the task stays armed for the
  next cycle.
- **No write actions.** Tool approval is gated at the gateway by
  `_heartbeat_approval` (a read-only allowlist). Any write tool you try will
  be rejected and audited; do not waste a turn attempting one. If a task
  asks you to "fix" or "update" something, treat it as "observe and notify
  the user so they can fix" — never the action itself.
  - **Translate write→read; never call the write tool.** A task line may
    literally instruct you to `spawn_run` a subagent, `send_message`, write a
    file, or `cron_add` — these (and every other write tool) are blocked here.
    Do the equivalent read yourself with your allowed tools and put the result
    in your response text, which is auto-delivered as the notification. You do
    NOT need — and must not attempt — `spawn_run` or `send_message` to report:
    your response IS the message. Attempting a blocked tool just burns the
    cycle and emits a `denied` audit event.
  - **Drop tasks that truly need a write tool.** If a task cannot be done
    read-only (it fundamentally requires an action you can't take), report that
    limitation to the user once and OMIT `HEARTBEAT_KEEP` so the task is dropped
    — do not re-arm it to fail the same way every cycle.
- **Your response IS the notification.** Whatever you write becomes the
  message the user sees, routed per the task's `<!-- deliver:... -->` tag or,
  when untagged, the `heartbeat.default_deliver` config (default `slack` = Slack
  DM + dashboard bell; `dashboard` = dashboard bell only). Report only when there
  is a real signal — a failure, a blocked CR, an item needing action. For a
  routine "nothing to do" completion, keep your response minimal. There is no
  transcript to scroll; be concise (a sentence or two for a status check, a short
  bulleted summary for a comment dump). Keep it scannable.
- **HEARTBEAT_KEEP semantics.** Include the literal token `HEARTBEAT_KEEP`
  anywhere in your response when the task is NOT done (so it retries next
  cycle). Omit the token when the task is fully complete (so it is dropped
  from the file).

## Tools

You have a curated read-only toolset (codebase search, knowledge-base query,
and side-effect-free kirocrew-core reads). Anything outside that list is
rejected. If you find yourself wanting a tool that isn't available, say so in
the response — the operator will add it after observing the SEL `denied` event.
"""


# --------------------------------------------------------------------------- #
# The compatibility facade's resolution (the table and forwarding type sit above
# ``_NATIVE_PROMPT_STUB``). It runs here, after every name this module binds.
# --------------------------------------------------------------------------- #
#: Re-exported name -> the dotted NAME of its owner, never the module object: the
#: owner is read from :data:`sys.modules` on each use, so a module purged and
#: imported again is seen at once instead of this table forwarding to the old copy.
_EXPORTS: dict[str, str] = _index_exports()


# Hidden from type checkers: mypy types every unknown attribute of a module that
# defines ``__getattr__`` as ``Any``, so a mistyped or removed ``agent.<name>`` would
# type-check. mypy sees the re-exports through the ``TYPE_CHECKING`` imports instead.
if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Read a re-exported name from the module that owns it (:pep:`562`)."""
        if name not in _EXPORTS:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(_owner(name), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


# Every owner is imported here, once this module has bound all of its own names: an
# owner reads this module's names as ``agent_mod.<name>`` at import and at call time,
# so it cannot load before them. Loading them now rather than on first use takes
# each owner's module-level bindings when this module is imported, as they were when
# the materialization lived in one module; on first use that moment could fall inside
# a test's patch of a source module, and the owner would keep the patched value for
# the rest of the process. This module's own code calls the owners through these
# bindings.
from kiro_crew.agent_materialization import (  # noqa: E402, F401 -- the owners read the names bound above
    auto_approve,
    conductor_agents,
    default_spec_commit,
    first_crewmate,
    fork_refresh,
    guide_platform,
    kiro_hooks,
    managed_mcp,
    mcp_aliases,
    mcp_sources,
    service_agents,
    skill_resources,
    worker_agent,
)

#: The names, here or on an owner, that are bound to a MODULE once every owner has
#: loaded. Fixed by name rather than judged by the value a name holds at the moment
#: of a write, so a function patched with a module stub is still undone, and a
#: module name stays refused whatever it was last set to.
_MODULE_NAMES = frozenset(
    name
    for name, value in [
        *globals().items(),
        *((name, getattr(_owner(name), name)) for name in _EXPORTS),
    ]
    if isinstance(value, ModuleType) and not name.startswith("__")
)

# Installed once this module's own names are bound and its owners have loaded, so
# the forwarding is live for every caller but never runs during either.
sys.modules[__name__].__class__ = _ReExportModule

# ``from kiro_crew.agent import *`` consults this list and never reaches
# ``__getattr__``, so without it a star import would carry only the names this
# module binds itself. It is DERIVED from the two authorities -- what this module
# binds and the re-export table -- so it is not a third list to keep in step.
__all__ = sorted(name for name in set(globals()) | set(_EXPORTS) if not name.startswith("_"))
