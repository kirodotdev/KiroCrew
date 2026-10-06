"""The dashboard gateway bootstrap keeps its surface and contracts while its owners move.

``kiro_crew.dashboard.server`` is the gateway's import path and its patch surface.
``start_dashboard`` and ``start_api_server`` stay defined there as the boot
sequencers; the helper families they call, and the boot phases they delegate to,
live in the modules of ``kiro_crew.dashboard.server_runtime``, and
``server_runtime.compose`` runs every function those owners define on the server
module's globals. These tests pin:

* the surface: every name the module bound before the split still resolves on it,
  and the seams other modules import keep their identity;
* the composition: every owner function runs on the server's globals, reads only
  names the server binds, and captures no name a test rebinds on the server;
* the boot: the full middleware order of both entrypoints, their lifecycle hooks,
  the MCP route table and the sequence of server calls a boot and a teardown make;
* the guards: what repository guards read in ``server.py`` by path stays there, and
  every review rule and path filter keyed to the server covers its owners;
* the clauses the bootstrap carries, characterized where the owners hold them.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import dis
import functools
import hashlib
import importlib
import importlib.util
import inspect
import pkgutil
import re
import subprocess
import sys
import textwrap
import threading
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from source_corpus import repo_files_named, repo_root

import kiro_crew.dashboard.server as server
from kiro_crew.dashboard import server_runtime
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_FACADE = server.__name__
_FACADE_PATH = Path(server.__file__).resolve()
_OWNER_PACKAGE = server_runtime.__name__
_OWNER_DIR = Path(server_runtime.__file__).resolve().parent
_SRC = _FACADE_PATH.parents[2]

#: Every module-level name ``server`` bound at the base the split was cut from: what it
#: defined and what it imported, private names included, because tests and other
#: modules read private names off it. A name bound only by ``import <module>`` of a
#: stdlib module is left out: nothing reads ``os`` off the server, and pinning one
#: would fail on the removal of an unused import.
_BASE_NAMES = frozenset("""
        AMBIGUOUS_LOOPBACK_HOSTS APP_WINDOW_URL_PREFIX AUDIT_CLAIMED_KEY Any
        Awaitable Callable DashboardState FOREIGN_HOLDER HEALTHY_PEER
        InstancesRegistry KiroCrewConfig LISTENER_LOST_EXIT_CODE ListenerGuard
        LoopStallWatchdog NO_HOLDER NamedTuple POLICY_REVOKED_SOURCE PROBE_PATHS
        Path PureWindowsPath RECLAIMED SECONDARY_LOOPBACK_FOR STT_PROVIDER_LOCAL
        ScriptHookStore SecondaryLoopback Sequence SkillsLoader SleepInhibitor
        SshTunnelManager TYPE_CHECKING TunnelState _AIOHTTP_CONTENT_TYPES
        _APP_WINDOWS_SUBDIR _BASE_CSP _CREWMATE_PRUNE_GATE_HELD_PREFIXES
        _CREWMATE_PRUNE_GATE_SAFE_METHODS _CREWMATE_PRUNE_GATE_TIMEOUT_S
        _CSRF_SAFE_METHODS _DEFAULT_PORT _DIST_DIR _IMMUTABLE_CACHE_CONTROL
        _IMMUTABLE_PATH_PREFIXES _INSTANCES_FRAME_SRC_EXTRA _LOOPBACK_FRAME_SRC
        _MAX_HEADER_FIELD_SIZE _MIXED_INTERNAL_API_PATHS _NO_STORE_CACHE_CONTROL
        _OWN_HOST_WARM_TASKS _PERMISSIONS_POLICY _PNA_REQUEST_HEADER
        _PNA_RESPONSE_HEADER _PREVENT_SLEEP_POLL_INTERVAL_SECS
        _PRE_AUDIT_DENY_STATUSES _SKILL_APPROVAL_SETTING_URL _STATIC_DIR
        _STRICT_INTERNAL_API_PATHS _STT_PREWARM_BOOT_DELAY_SECS
        _STT_SWEEP_BOOT_DELAY_SECS _TAILNET_AWAKE_TTL_SECS _TIME_WAIT_BUDGET_SECS
        _TUNNEL_STOP_TIMEOUT_SECS _UNATTENDED_EXPIRY_TITLE _VENDOR_CORS_HEADER_VALUE
        _VENDOR_PATH_PREFIX _VENDOR_PREFLIGHT_MAX_AGE_SECS _VIA_PROXY_SUFFIX
        _WORKER_ASSET_MARKER _WORKER_CACHE_CONTROL _apply_security_headers
        _apply_startup_yolo _arm_listener_guard _arm_prevent_sleep_poll
        _arm_secondary_listener_guard _armed_unattended_loops _asset_cache_control
        _audit_denied _autonudge_get _bind_once _claimed_dashboard_slots
        _clear_override_derived_trust _cookie_port_from_host
        _crewmate_prune_gate_holds_path _deferred _deferred_work_ledger
        _dispatch_override_expiry_notification _dispatch_owner_dm _dist_file_handler
        _dm_owner _export_bound_port _extra_frame_ancestors
        _finalize_asset_cache_control _holds_every_loopback_family
        _import_stt_engine _initialize_workflow_service
        _install_asset_cache_control_finalizer _is_anchored _is_spa_shell_request
        _kick_config_watch _kick_connections_warm_scavenge _kick_crewmate_prune
        _kick_deferred_transcript_removal _kick_knowledge_orphan_reclaim
        _kick_local_decision_model _kick_session_search_index
        _kick_workflow_initialization _live_sibling_port _log_prewarm_outcome
        _make_csrf_middleware _make_deny_audit_middleware
        _make_host_validation_middleware _mixed_internal_api_paths
        _note_listener_sidecar _notify_owner_channels _notify_slack_override_expired
        _notify_unattended_expiry _override_expiry_dm_text _own_host_warm_done
        _pending_skill_notification _precompute_telemetry
        _prune_browser_snapshots_loop _reconcile_listener_publication
        _register_browser_install_cleanup _register_browser_view_cleanup
        _register_config_watch _register_connections_warm_lifecycle
        _register_crewmate_prune_gate _register_deploy_routes
        _register_deploy_skills _register_dist_static_routes
        _register_instances_hooks _register_listener_guard_shutdown
        _register_mcp_routes _register_own_host_warm
        _register_prevent_sleep_shutdown _register_stt_hooks
        _register_unix_socket_cleanup _register_workflow_lifecycle
        _remove_stale_unix_socket _republish_listener_sidecar
        _request_listener_lost_exit _reserve_dashboard_port _resolve_dist_file
        _resolved_bound_host _resolved_bound_port _retake_hops_then_revive
        _revive_intended_instances _secondary_listener_given_up _serve_dist_file
        _should_prevent_sleep _start_secondary_loopback_site _start_site
        _start_unix_site _stop_spawned_backends _stt_idle_sweep _stt_startup_prewarm
        _suspend_override_derived_trust _tailnet_awake_cache _tailnet_origin_enabled
        _tailnet_publish_keeps_awake _take_prior_dropped_grant
        _unattended_expiry_text _vendor_preflight_handler _window_entry_handler
        _wire_status_delta_sink _wire_tunnel_shutdown _withdraw_listener_sidecar
        _would_soften_a_strict_path _write_instance_credentials _write_secret_file
        annotations api_artifact_asset api_artifact_comments api_artifact_delete
        api_artifact_delete_comment api_artifact_detail api_artifact_edit_comment
        api_artifact_events api_artifact_folder_create api_artifact_folder_delete
        api_artifact_folder_update api_artifact_folders api_artifact_mark_review
        api_artifact_materialize api_artifact_overwrite_remote
        api_artifact_post_comment api_artifact_publish
        api_artifact_publish_providers api_artifact_pull_latest
        api_artifact_record_event api_artifact_refresh_sharing api_artifact_relocate
        api_artifact_reopen_comment api_artifact_reply_comment
        api_artifact_reprobe_notice api_artifact_resolve_comment
        api_artifact_session_docs api_artifact_set_folder api_artifact_set_pinned
        api_artifact_settle_blank api_artifact_unpublish api_artifact_update
        api_artifact_update_sharing api_artifact_upstream_status
        api_artifact_version_detail api_artifact_versions api_artifacts_create
        api_artifacts_list api_remote_artifact_comments
        api_remote_artifact_delete_comment api_remote_artifact_get
        api_remote_artifact_mark_review api_remote_artifact_post_comment
        api_remote_artifact_reply_comment api_remote_artifacts_browse
        api_remote_artifacts_clone api_remote_artifacts_fork apply_config_duration
        async_safe_context_call attribute_dump audit_actor authorize_and_add_nudge
        await_crewmate_prune_settled bind_address_for browser_cli_launch
        browser_cli_launcher browser_cli_snapshots browser_cli_token
        browser_cli_view build_allowed_origins build_hardened_runner
        build_host_canonical_redirect cautious_boot channel_slots chat check_host
        check_origin chmod_socket_0600 claim_dump_notification
        cleanup_migrated_builtin consume_managed_service_launch_environment
        current_context dashboard_socket_path data_home degraded_config_files
        describe describe_dropped_grant discover_app_window_entries dump_age_seconds
        dump_replay_lines effective_session_key frame_ancestors_value
        grant_declared_yolo handlers import_module init_hook_reconciler
        init_hooks_system internal_path_matches is_csrf_exempt is_proxied_request
        load_loop_stall_exit_after logger make_route_latency_middleware
        mark_audit_claimed migrate_channel_transcripts newest_dump_with_stacks
        on_gateway_shutdown on_gateway_startup open_dump_file platform_compat
        port_resolution prune_synced_crewmates quote reclaim_stale_gateway_port
        record_boot_to_ready redact_credentials redact_exfiltration_urls
        refresh_config_meta_stamp refresh_materialized_agents register_all
        register_app_window_paths register_builtin_apps register_skill_read_observer
        register_status_delta_sink release_site resolve_dashboard_host
        resolve_loop_stall_exit_after rotate_dumps run_marker safe_context_call
        safety_override sel sel_is_warm set_global_hook_store
        set_pending_consumed_hook set_pending_staged_hook setup_feedback_routes
        setup_knowledge_routes setup_link_meta_routes setup_secrets_routes
        setup_spawn_resume_routes setup_tunnel setup_weixin_routes
        setup_whatsapp_routes should_canonicalize_host shutdown_event
        slot_ownership_middleware start_api_server start_dashboard
        start_deferred_app_backends start_enabled_app_backends stop_hook_reconciler
        subprocess_executor sweep_stale_dumps tailnet
        tailnet_effective_allowed_logins tailnet_identity_unknown tailnet_serve
        take_dropped_grant token_auth_middleware token_embed_parent_port
        unregister_status_delta_sink warm_auth_singletons warm_own_host_names
        warm_sel_singleton web wire_session_subagent_probe
    """.split())

#: Each definition moved out of the one-module file and the owner its responsibility
#: puts it in. Adding or removing an owner changes the composition, so the set is
#: spelled out.
_BASE_OWNERS: dict[str, tuple[str, ...]] = {
    "app_platform": (),
    "config_watch": ("_kick_config_watch", "_register_config_watch"),
    "crewmate_prune": tuple("""
        _claimed_dashboard_slots _crewmate_prune_gate_holds_path _kick_crewmate_prune
        _kick_deferred_transcript_removal _register_crewmate_prune_gate
        await_crewmate_prune_settled
        """.split()),
    "diagnostics": ("_precompute_telemetry",),
    "heartbeat": (),
    "listener": tuple("""
        SecondaryLoopback _bind_once _export_bound_port _holds_every_loopback_family
        _register_unix_socket_cleanup _remove_stale_unix_socket _reserve_dashboard_port
        _resolved_bound_host _resolved_bound_port _start_secondary_loopback_site
        _start_site _start_unix_site
        """.split()),
    "listener_claims": tuple("""
        _arm_listener_guard _arm_secondary_listener_guard _live_sibling_port
        _note_listener_sidecar _reconcile_listener_publication
        _register_listener_guard_shutdown _republish_listener_sidecar
        _request_listener_lost_exit _secondary_listener_given_up
        _withdraw_listener_sidecar _write_instance_credentials _write_secret_file
        """.split()),
    "maintenance": tuple("""
        _kick_connections_warm_scavenge _kick_knowledge_orphan_reclaim
        _kick_local_decision_model _kick_session_search_index _own_host_warm_done
        _register_connections_warm_lifecycle _register_own_host_warm
        """.split()),
    "mcp_routes": ("_deferred", "_deferred_work_ledger", "_register_mcp_routes"),
    "middleware_chain": ("_tailnet_origin_enabled",),
    "owner_notices": ("_dispatch_owner_dm", "_dm_owner", "_notify_owner_channels"),
    "prevent_sleep": (
        "_arm_prevent_sleep_poll",
        "_register_prevent_sleep_shutdown",
        "_should_prevent_sleep",
    ),
    "safety_grants": tuple("""
        _apply_startup_yolo _armed_unattended_loops _dispatch_override_expiry_notification
        _notify_slack_override_expired _notify_unattended_expiry _override_expiry_dm_text
        _take_prior_dropped_grant _unattended_expiry_text
        """.split()),
    "security_headers": tuple("""
        _apply_security_headers _asset_cache_control _extra_frame_ancestors
        _finalize_asset_cache_control _install_asset_cache_control_finalizer
        _vendor_preflight_handler
        """.split()),
    "security_middleware": tuple("""
        _audit_denied _make_csrf_middleware _make_deny_audit_middleware
        _make_host_validation_middleware _mixed_internal_api_paths
        _would_soften_a_strict_path audit_actor build_host_canonical_redirect
        """.split()),
    "service_hooks": ("_wire_status_delta_sink",),
    "session_restore": (),
    "skill_learning": ("_pending_skill_notification",),
    "static_assets": tuple("""
        _dist_file_handler _is_anchored _register_dist_static_routes _resolve_dist_file
        _serve_dist_file _window_entry_handler discover_app_window_entries
        """.split()),
    "stt_hooks": tuple("""
        _import_stt_engine _log_prewarm_outcome _register_stt_hooks _stt_idle_sweep
        _stt_startup_prewarm
        """.split()),
    "tunnel": ("_wire_tunnel_shutdown",),
    "workflow_startup": (
        "_initialize_workflow_service",
        "_kick_workflow_initialization",
        "_register_workflow_lifecycle",
    ),
}

#: The boot phases the two entrypoints delegate to, which the one-module file kept
#: inline in their bodies, and the owner each lives in.
_PHASE_OWNERS: dict[str, tuple[str, ...]] = {
    "app_platform": (
        "_reconcile_app_resources",
        "_start_app_backends",
        "_start_bound_port_app_backends",
        "_warm_builtin_app_names",
        "_warm_materialized_agents",
    ),
    "crewmate_prune": ("_converge_channel_transcripts",),
    "diagnostics": ("_register_diag_recorder_shutdown", "_start_diag_recorder"),
    "heartbeat": (
        "_open_loop_watchdog",
        "_register_watchdog_shutdown",
        "_report_prior_crash_dump",
        "_start_loop_heartbeat",
    ),
    "listener": ("_export_reserved_bind_evidence",),
    "middleware_chain": (
        "_dashboard_canonical_redirect",
        "_install_api_middlewares",
        "_install_dashboard_middlewares",
        "_resolve_tailnet_trust",
    ),
    "safety_grants": ("_notify_restart_dropped_grant",),
    "service_hooks": ("_register_kiro_service_shutdown",),
    "session_restore": ("_restore_dashboard_sessions",),
    "skill_learning": ("_auto_create_consolidator", "_register_pending_skill_hooks"),
    "tunnel": ("_start_aea_tunnel",),
}

#: SHA-256 of the sorted ``"<name> <kind> <signature>"`` lines of every name in
#: ``_BASE_OWNERS``, captured from the one-module file before the split: each moved
#: name keeps the kind and signature it had there.
_BASE_SHAPE_DIGEST = "534c66ed75e197e3cff4a5d7488aa541ca7ee0da864fa6cce3e1418926a9063c"

#: Definitions that stay in the server module: the two entrypoints, and the helpers
#: repository guards read in ``server.py`` by path or that own module state there --
#: the tailnet awake cache's reader, the inherited-trust teardown pair, the browser
#: lifecycle trio and the instances hooks with their revive policy.
_FACADE_DEFS = (
    "_prune_browser_snapshots_loop",
    "_tailnet_publish_keeps_awake",
    "_retake_hops_then_revive",
    "_revive_intended_instances",
    "_clear_override_derived_trust",
    "_suspend_override_derived_trust",
    "_register_browser_install_cleanup",
    "_register_browser_view_cleanup",
    "_register_instances_hooks",
    "_dispatch_healthy_boot_marker",
    "start_dashboard",
    "start_api_server",
)

_MOVED = frozenset(name for names in _BASE_OWNERS.values() for name in names)
_PHASES = frozenset(name for names in _PHASE_OWNERS.values() for name in names)


def _owner(stem: str) -> types.ModuleType:
    return importlib.import_module(f"{_OWNER_PACKAGE}.{stem}")


def _owners() -> list[types.ModuleType]:
    return [_owner(info.name) for info in pkgutil.iter_modules([str(_OWNER_DIR)])]


def _owner_sources() -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8") for path in sorted(_OWNER_DIR.glob("[!_]*.py"))
    }


def _owner_functions() -> list[tuple[str, types.FunctionType]]:
    """``(label, function)`` for every function an owner's file defines at top level
    or as a member of a class the owner defines."""
    found: list[tuple[str, types.FunctionType]] = []
    for owner in _owners():
        for name, value in vars(owner).items():
            members = [(name, value)]
            if isinstance(value, type) and value.__module__ == owner.__name__:
                members = [(f"{name}.{k}", v) for k, v in vars(value).items()]
            for label, member in members:
                fn = getattr(member, "__func__", member)
                if isinstance(fn, types.FunctionType) and fn.__code__.co_filename == owner.__file__:
                    found.append((f"{owner.__name__.rsplit('.', 1)[-1]}.{label}", fn))
    return found


def _global_names(code: types.CodeType):
    """Every global a code object and its nested code objects read or write."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in ("LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"):
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _run_child(tmp_path: Path, script: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        capture_output=True,
        timeout=120,
        cwd=str(tmp_path),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


# ── the surface ───────────────────────────────────────────────────────────────


def test_every_name_the_facade_bound_at_the_base_still_resolves() -> None:
    """Tests and other modules read private names off the server as well as public
    ones, so every module-level binding survives the split."""
    assert len(_BASE_NAMES) == 326
    assert sorted(name for name in _BASE_NAMES if not hasattr(server, name)) == []


def test_a_fresh_interpreter_sees_every_base_public_name(tmp_path: Path) -> None:
    """The public names resolve in a process that imports nothing else first, and the
    package's lazy entrypoints are the server's own objects there too."""
    public = sorted(name for name in _BASE_NAMES if not name.startswith("_"))
    assert len(public) > 140
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard as pkg
        import kiro_crew.dashboard.server as server
        missing = [n for n in sys.argv[1:] if not hasattr(server, n)]
        assert missing == [], missing
        assert pkg.start_dashboard is server.start_dashboard
        assert pkg.start_api_server is server.start_api_server
        print("ok")
        """,
        *public,
    )


def _facade_importers() -> dict[str, set[str]]:
    """``{module path: names}`` every source file imports from the server by name."""
    found: dict[str, set[str]] = {}
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        resolved = path.resolve()
        if resolved == _FACADE_PATH or _OWNER_DIR in resolved.parents or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if _FACADE not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module == _FACADE:
                found.setdefault(path.relative_to(_SRC).as_posix(), set()).update(
                    alias.name for alias in node.names
                )
    return found


def test_the_seams_other_modules_import_keep_their_identity() -> None:
    """Production modules import the server's names by name, most of them inside a
    function: the gateway's prune wait, the sandboxed documents' frame ancestors, and
    the package's lazy entrypoints. Each resolves to the composed object, which runs
    on the server's globals, so a patch of the server reaches every importer."""
    from kiro_crew import dashboard as pkg

    importers = _facade_importers()
    imported = {name for names in importers.values() for name in names}
    assert {"await_crewmate_prune_settled", "_extra_frame_ancestors"} <= imported
    assert sorted(name for name in imported if not hasattr(server, name)) == []
    for name in ("await_crewmate_prune_settled", "_extra_frame_ancestors"):
        assert getattr(server, name).__globals__ is vars(server), name
    assert pkg.start_dashboard is server.start_dashboard
    assert pkg.start_api_server is server.start_api_server


def test_the_shared_state_keeps_its_identity() -> None:
    """Module state is the server's: the own-address warm task set a startup hook fills
    is the one object ``test_denied_commands_security`` polls, and the tailnet awake
    cache a reader rebinds with ``global`` is the server's binding."""
    hook = server._register_own_host_warm
    assert hook.__globals__["_OWN_HOST_WARM_TASKS"] is server._OWN_HOST_WARM_TASKS
    assert isinstance(server._OWN_HOST_WARM_TASKS, set)
    assert isinstance(server._MIXED_INTERNAL_API_PATHS, frozenset)
    assert "_tailnet_awake_cache" in vars(server)
    assert all("_tailnet_awake_cache" not in vars(owner) for owner in _owners())


#: Names the moved code imports in its own body, by the module it imports them from. A
#: module-level import of any of them in an owner would put that module on the
#: gateway's boot path or close an import cycle, so each stays where the one-module
#: file had it: inside the function that needs it.
_LAZY_IMPORTS = {
    "kiro_crew": "_hist_mod _rs stt",
    "kiro_crew.agent": "rebuild_agent_config rebuild_agent_config_reporting",
    "kiro_crew.apps.bridges": "reconcile_enabled_app_resources",
    "kiro_crew.apps.dev_mode": "init_dev_mode_watcher stop_dev_mode_watcher",
    "kiro_crew.apps.event_bus": "build_broadcast_fn",
    "kiro_crew.apps.execution": "builtin_app_agents builtin_app_mcp_servers builtin_app_names",
    "kiro_crew.apps.spawn_sdk": "build_spawn_impl",
    "kiro_crew.config": "live",
    "kiro_crew.config.live": "ConfigChange",
    "kiro_crew.config.resolution": "DEGRADED_WHOLE_CONFIG",
    "kiro_crew.connections.warm": "scavenge_warm_mint_artifacts shutdown_warm_mint",
    "kiro_crew.dashboard.chat": "_run_chat",
    "kiro_crew.dashboard.handlers": "wf_handlers work_ledger",
    "kiro_crew.dashboard.handlers.ask_question": (
        "api_ask_question api_ask_question_answer api_ask_question_dismiss "
        "api_ask_question_pending"
    ),
    "kiro_crew.dashboard.handlers.autonudge": (
        "api_autonudge_delete api_autonudge_fire api_autonudge_get api_autonudge_list "
        "api_autonudge_start api_autonudge_update api_monitor_clear api_monitor_create "
        "api_monitor_restart api_monitor_slot_get api_monitor_stop api_monitor_update "
        "api_monitors_list api_session_monitor_get"
    ),
    "kiro_crew.dashboard.handlers.decisions": "resume_local_decision_model",
    "kiro_crew.dashboard.handlers.sandbox_doc": "register_sandbox_doc_routes",
    "kiro_crew.dashboard.handlers.updates": "apply_log_level_from_config",
    "kiro_crew.dashboard.handlers.webapp_preview": "register_webapp_preview_routes",
    "kiro_crew.dashboard.handlers.workflows": (
        "api_workflow_author api_workflow_definition_get api_workflow_definition_run "
        "api_workflow_definition_update api_workflow_definitions "
        "api_workflow_definitions_create api_workflow_run api_workflow_run_cancel "
        "api_workflow_run_get api_workflow_run_intent api_workflow_run_promote "
        "api_workflow_run_rerun api_workflow_runs"
    ),
    "kiro_crew.dashboard.handlers_system": "_get_owner_hash _get_static_system_info",
    "kiro_crew.dashboard.workflow_inject": "inject_bound_workflow_result",
    "kiro_crew.decisions": "local_runtime",
    "kiro_crew.diag.recorder": "_DiagRecorder get_recorder",
    "kiro_crew.history_index_worker": "SessionIndexWorkerSupervisor",
    "kiro_crew.hooks": "set_builtin_app_agents set_builtin_app_mcp_servers set_builtin_app_names",
    "kiro_crew.memory": "MemoryStore",
    "kiro_crew.metrics": "_gauges",
    "kiro_crew.security": "redact_credentials redact_exfiltration_urls",
    "kiro_crew.sel": "sel",
    "kiro_crew.stt": "engine stt_models",
    "kiro_crew.subagent": "_available_memory_gb",
    "kiro_crew.transcribe": "_whisper_language",
    "kiro_crew.workflows.service": "WorkflowService",
}


def test_the_lazy_imports_stay_inside_the_functions_that_need_them() -> None:
    expected = {name: module for module, names in _LAZY_IMPORTS.items() for name in names.split()}
    local: dict[str, set[str]] = {}
    for source in _owner_sources().values():
        tree = ast.parse(source)
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        local.setdefault(alias.asname or alias.name, set()).add(node.module or "")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        local.setdefault(alias.asname or alias.name, set()).add(alias.name)
    assert local == {name: {module} for name, module in expected.items()}
    assert len(expected) == 76


def test_a_star_import_carries_the_moved_public_names(tmp_path: Path) -> None:
    """The server declares no ``__all__``, so every public binding goes out."""
    assert not hasattr(server, "__all__")
    probe = tmp_path / "server_star_probe.py"
    probe.write_text(
        "from kiro_crew.dashboard.server import *  # noqa: F401,F403\n", encoding="utf-8"
    )
    spec = importlib.util.spec_from_file_location("server_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in (
        "audit_actor",
        "build_host_canonical_redirect",
        "discover_app_window_entries",
        "await_crewmate_prune_settled",
        "SecondaryLoopback",
        "start_dashboard",
        "start_api_server",
    ):
        assert getattr(module, name) is getattr(server, name)


# ── the composition ───────────────────────────────────────────────────────────


def test_the_owner_set_is_the_package() -> None:
    names = {info.name for info in pkgutil.iter_modules([str(_OWNER_DIR)])}
    assert names == set(_BASE_OWNERS) == set(_BASE_OWNERS) | set(_PHASE_OWNERS)


def _shape(obj: Any) -> str:
    if inspect.isclass(obj):
        return "class"
    prefix = "async def " if inspect.iscoroutinefunction(obj) else "def "
    return prefix + str(inspect.signature(obj))


def test_every_moved_name_is_one_object_in_its_owner() -> None:
    """The server's binding of a moved name or a boot phase is the owner's object, in
    the owner its responsibility names, and no name is defined by two owners."""
    placed = [
        (owner, name)
        for table in (_BASE_OWNERS, _PHASE_OWNERS)
        for owner, names in table.items()
        for name in names
    ]
    strays = [f"{o}:{n}" for o, n in placed if getattr(server, n) is not vars(_owner(o)).get(n)]
    assert strays == []
    names = [name for _, name in placed]
    assert len(names) == len(set(names)) == 90 + 23


def test_the_moved_names_keep_their_base_shapes() -> None:
    lines = sorted(f"{name} {_shape(getattr(server, name))}" for name in _MOVED)
    assert len(lines) == 90
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    assert digest == _BASE_SHAPE_DIGEST, "\n".join(lines)


def _module_assignments(source: str) -> set[str]:
    names = set()
    for node in ast.parse(source).body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


def test_facade_state_stays_on_the_facade() -> None:
    """Owner functions reach module state by name through the server's namespace, so
    a test that rebinds one there is the binding every function sees -- which holds
    only while no owner keeps a copy."""
    state = _module_assignments(_FACADE_PATH.read_text(encoding="utf-8"))
    assert {"logger", "_tailnet_awake_cache", "_OWN_HOST_WARM_TASKS", "_BASE_CSP"} <= state
    assert {"_STRICT_INTERNAL_API_PATHS", "_MIXED_INTERNAL_API_PATHS"} <= state
    assert {"_CREWMATE_PRUNE_GATE_TIMEOUT_S", "_TIME_WAIT_BUDGET_SECS"} <= state
    owned = {stem: sorted(_module_assignments(source)) for stem, source in _owner_sources().items()}
    assert {stem: names for stem, names in owned.items() if names} == {}
    assert not hasattr(server, "_startup_tasks")


@pytest.mark.parametrize("name", _FACADE_DEFS)
def test_a_facade_definition_stays_in_the_facade_file(name: str) -> None:
    obj = getattr(server, name)
    assert Path(obj.__code__.co_filename).resolve() == _FACADE_PATH
    assert [o.__name__ for o in _owners() if name in vars(o)] == []


def test_every_base_definition_is_in_exactly_one_place() -> None:
    """The server keeps what ``_FACADE_DEFS`` names and the owners hold the rest:
    together they are the one-module file's definitions, each once."""
    defined = {
        node.name
        for node in ast.parse(_FACADE_PATH.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert defined == set(_FACADE_DEFS)
    assert len(defined | _MOVED) == len(defined) + len(_MOVED) == 102


def test_the_owners_log_as_the_facade() -> None:
    """Log capture keyed to ``kiro_crew.dashboard.server`` keeps seeing the moved
    sites: an owner function logs through the server's ``logger``."""
    assert server.logger.name == _FACADE
    readers = [
        label for label, fn in _owner_functions() if "logger" in set(_global_names(fn.__code__))
    ]
    assert len(readers) >= 60
    assert all("logger" not in vars(owner) for owner in _owners())


def test_every_owner_function_runs_on_the_facade_globals() -> None:
    """A patch of ``kiro_crew.dashboard.server.<name>`` reaches an owner function only
    because the function reads the server's globals, not its own."""
    labels = {label for label, _ in _owner_functions()}
    assert len(labels) >= 112
    strays = [
        label
        for label, fn in _owner_functions()
        if fn.__globals__ is not vars(server) or fn.__module__ != _FACADE
    ]
    assert strays == []


def test_the_sweep_reports_a_global_the_facade_does_not_bind() -> None:
    """The name sweep can fail, nested bodies included."""

    def _probe() -> object:
        def _inner() -> object:
            return _absent_from_the_server_namespace  # noqa: F821

        return _inner

    assert "_absent_from_the_server_namespace" in set(_global_names(_probe.__code__))


def test_every_global_an_owner_function_reads_is_bound_on_the_facade() -> None:
    """An owner's own imports are inert for its functions, so a name missing from the
    server surfaces only when its line runs -- often inside an ``except`` that turns
    the NameError into a logged degradation. The sweep makes it a test failure."""
    namespace = vars(server)
    unresolved = sorted(
        (label, name)
        for label, fn in _owner_functions()
        for name in set(_global_names(fn.__code__))
        if name not in namespace and not hasattr(builtins, name)
    )
    assert unresolved == []


@pytest.mark.asyncio
async def test_a_patch_of_the_facade_reaches_an_owner_function(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The contract the rebinding exists for: the build route (static_assets) reads
    the server's resolver, and the tailnet opt-in (middleware_chain) reads the
    server's config class."""
    resolved: list[tuple[Path, str, str]] = []

    def _resolve(dist_dir: Path, subdir: str, tail: str) -> None:
        resolved.append((dist_dir, subdir, tail))
        return None

    monkeypatch.setattr(server, "_resolve_dist_file", _resolve)
    response = await server._serve_dist_file(tmp_path, "assets", "a.js")
    assert response.status == 404
    assert resolved == [(tmp_path, "assets", "a.js")]
    loader = MagicMock()
    loader.load.return_value.dashboard.tailscale.enabled = True
    monkeypatch.setattr(server, "KiroCrewConfig", loader)
    assert server._tailnet_origin_enabled() is True


def test_module_and_qualname_still_name_the_facade() -> None:
    """Reprs and pickling by reference read as before the split: every owner function
    resolves back through its own ``__module__`` and ``__qualname__``. A class keeps
    its owner module, which is where ``inspect`` finds its source."""
    wrong = []
    for label, fn in _owner_functions():
        target: object = sys.modules[fn.__module__]
        for part in fn.__qualname__.split("."):
            target = (
                vars(target).get(part) if isinstance(target, type) else getattr(target, part, None)
            )
            target = getattr(target, "__func__", target)
        if target is not fn:
            wrong.append(label)
    assert wrong == []
    assert server.SecondaryLoopback.__module__ == f"{_OWNER_PACKAGE}.listener"


def test_a_moved_function_reads_its_source_from_its_owner() -> None:
    source = inspect.getsource(server._register_mcp_routes)
    assert source.startswith("def _register_mcp_routes(")
    assert inspect.getsourcefile(server._register_mcp_routes) == _owner("mcp_routes").__file__


def test_the_compose_copy_matches_its_siblings() -> None:
    """The dashboard compositions share one technique; each package keeps its own copy
    so no package imports another's, and the copies stay identical."""

    def body(package: str) -> str:
        text = (_SRC / "kiro_crew/dashboard" / package / "__init__.py").read_text(encoding="utf-8")
        return text[text.index("def compose(") :]

    assert body("server_runtime") == body("agent_admin") == body("file_api")


# ── one edge ──────────────────────────────────────────────────────────────────


def _package_of(path: Path) -> str:
    parts = list(path.resolve().relative_to(_SRC).with_suffix("").parts)
    return ".".join(parts[:-1])


def _import_targets(tree: ast.Module, package: str) -> list[tuple[ast.AST, str]]:
    """``(node, dotted module)`` for every module a tree imports, spelled any way:
    ``import a.b``, ``from a import b``, relative imports resolved against
    *package*, and a string-literal ``import_module(...)`` / ``__import__(...)``."""
    found: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, package)
            found.append((node, base))
            found.extend((node, f"{base}.{alias.name}") for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", ""))
            in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and not node.args[0].value.startswith(".")
        ):
            found.append((node, node.args[0].value))
    return found


def _within(target: str, module: str) -> bool:
    return target == module or target.startswith(f"{module}.")


def _type_checking_nodes(tree: ast.Module) -> set[int]:
    """Nodes under a module-level ``if TYPE_CHECKING:`` body; its ``else`` runs."""
    return {
        id(sub)
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for stmt in node.body
        for sub in ast.walk(stmt)
    }


def _owner_runtime_edges(source: str, package: str) -> list[int]:
    """Lines where an owner imports a project module outside ``TYPE_CHECKING`` at
    module level: the server, a sibling owner, or anything else under ``kiro_crew``."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    module_level = {id(sub) for node in tree.body for sub in ast.walk(node)}
    function_bodies = {
        id(sub)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for sub in ast.walk(node)
    }
    return sorted(
        {
            node.lineno
            for node, target in _import_targets(tree, package)
            if _within(target, "kiro_crew")
            and id(node) not in guarded
            and id(node) in module_level
            and id(node) not in function_bodies
        }
    )


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from . import listener\n", True),
        ("from .listener import _bind_once\n", True),
        ("from .. import server\n", True),
        ("from kiro_crew.dashboard import server\n", True),
        ("import kiro_crew.dashboard.server as srv\n", True),
        ("import importlib\nimportlib.import_module('kiro_crew.dashboard.server')\n", True),
        ("__import__('kiro_crew.dashboard.server_runtime.listener')\n", True),
        ("if TYPE_CHECKING:\n    from kiro_crew.dashboard.server import logger\n", False),
        ("def f():\n    from kiro_crew.config import live\n", False),
        ("import asyncio\nfrom aiohttp import web\n", False),
    ],
)
def test_the_owner_edge_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert bool(_owner_runtime_edges(source, _OWNER_PACKAGE)) is flagged


def test_nothing_but_the_facade_imports_an_owner() -> None:
    """The server module is the one import path and the one patch surface."""
    importers = []
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        resolved = path.resolve()
        if _OWNER_DIR in resolved.parents or resolved == _FACADE_PATH or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "server_runtime" not in text:
            continue
        importers.extend(
            f"{path.relative_to(_SRC)}:{node.lineno}"
            for node, target in _import_targets(ast.parse(text), _package_of(path))
            if _within(target, _OWNER_PACKAGE)
        )
    assert importers == []


def test_an_owner_imports_project_modules_only_for_type_checking() -> None:
    """An owner's module-level imports are stdlib and third-party only, plus its
    ``TYPE_CHECKING`` names from the server. Its project names come from the server's
    globals, so an owner adds nothing to the gateway's boot import graph and cannot
    close a cycle with the server."""
    offenders = {
        stem: lines
        for stem, source in _owner_sources().items()
        if (lines := _owner_runtime_edges(source, _OWNER_PACKAGE))
    }
    assert offenders == {}
    for source in _owner_sources().values():
        tree = ast.parse(source)
        guarded = [
            node
            for node in ast.walk(tree)
            if id(node) in _type_checking_nodes(tree) and isinstance(node, ast.ImportFrom)
        ]
        assert [node.module for node in guarded] in ([], [_FACADE])


def test_a_fresh_facade_import_loads_every_owner(tmp_path: Path) -> None:
    """Importing the server imports every owner with it: none loads lazily on a later
    call, so the import order stays the one the one-module file had."""
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.server
        missing = [n for n in sys.argv[1:]
                   if f"kiro_crew.dashboard.server_runtime.{n}" not in sys.modules]
        assert missing == [], missing
        print("ok")
        """,
        *_BASE_OWNERS,
    )


def test_a_second_facade_import_recomposes_the_owners_onto_it(tmp_path: Path) -> None:
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.server as first
        del sys.modules["kiro_crew.dashboard.server"]
        import kiro_crew.dashboard.server as second
        from kiro_crew.dashboard.server_runtime import listener
        assert second is not first
        assert second._bind_once.__globals__ is vars(second)
        assert listener._bind_once is second._bind_once
        print("ok")
        """,
    )


# ── the patch reach ───────────────────────────────────────────────────────────

_PATCH_CALLS = ("setattr", "patch.object", "delattr")
_MULTIPLE_OPTIONS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})
_FACADE_STRING = re.compile(r"""^kiro_crew\.dashboard\.server\.(\w+)$""")

#: The patches whose attribute the scan cannot resolve from the source, keyed by
#: (test file, enclosing function), with the names each one rebinds: the startup
#: harness replaces every step that reaches outside the process through one loop.
_RESOLVED_DYNAMIC_PATCHES: dict[tuple[str, str], frozenset[str]] = {
    ("test_dashboard_server_startup_coverage.py", "_neutralise_outside_process_work"): frozenset(
        {
            "start_enabled_app_backends",
            "start_deferred_app_backends",
            "register_builtin_apps",
            "cleanup_migrated_builtin",
            "on_gateway_startup",
            "on_gateway_shutdown",
        }
    ),
}


def _facade_aliases(tree: ast.Module) -> set[str]:
    """Every expression spelling a test module binds to the server, to a fixed point."""
    aliases = {_FACADE, f"sys.modules[{_FACADE!r}]", f'sys.modules["{_FACADE}"]'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == _FACADE and a.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.dashboard":
            aliases |= {a.asname or a.name for a in node.names if a.name == "server"}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if not isinstance(target, ast.Name) or target.id in aliases:
                continue
            if ast.unparse(value) in aliases or _imports_the_facade(value):
                aliases.add(target.id)
                changed = True
    return aliases


def _imports_the_facade(node: ast.AST) -> bool:
    """``import_module(<server>)``, or ``__import__(<server>, fromlist=...)`` with a
    non-empty fromlist (which returns the server itself, not its root package)."""
    if not (
        isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == _FACADE
    ):
        return False
    func = ast.unparse(node.func)
    if func.endswith("import_module"):
        return True
    fromlist = {k.arg: k.value for k in node.keywords}.get("fromlist")
    if fromlist is None and len(node.args) >= 4:
        fromlist = node.args[3]
    return (
        func == "__import__" and isinstance(fromlist, (ast.List, ast.Tuple)) and bool(fromlist.elts)
    )


def _parametrized_strings(function: ast.AST) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for decorator in getattr(function, "decorator_list", []):
        if not (
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func).endswith("parametrize")
            and len(decorator.args) >= 2
            and isinstance(decorator.args[0], ast.Constant)
            and isinstance(decorator.args[1], (ast.List, ast.Tuple))
        ):
            continue
        names = [n.strip() for n in str(decorator.args[0].value).split(",")]
        if len(names) == 1:
            values = {e.value for e in decorator.args[1].elts if isinstance(e, ast.Constant)}
            if values and all(isinstance(v, str) for v in values):
                found[names[0]] = values
    return found


def _resolve_name(node: ast.AST, params: dict[str, set[str]]) -> set[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in params:
        return set(params[node.id])
    return None


def _facade_strings(tree: ast.Module, aliases: set[str]) -> set[str]:
    """Every name a test module binds to the server's dotted path, to a fixed point."""
    strings: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if isinstance(target, ast.Name) and target.id not in strings:
                if _string_text(value, strings, aliases) == _FACADE:
                    strings.add(target.id)
                    changed = True
    return strings


def _string_text(node: ast.AST, strings: set[str], aliases: set[str]) -> str | None:
    """The text one piece of a string spells: a constant, a name bound to the server's
    dotted path, or ``<server alias>.__name__``; None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in strings:
        return _FACADE
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and ast.unparse(node.value) in aliases
    ):
        return _FACADE
    return None


def _string_pieces(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.JoinedStr):
        return [v.value if isinstance(v, ast.FormattedValue) else v for v in node.values]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _string_pieces(node.left) + _string_pieces(node.right)
    return [node]


def _resolve_target(
    node: ast.AST, params: dict[str, set[str]], strings: set[str], aliases: set[str]
) -> set[str] | None:
    """The names a string patch target rebinds on the server: an empty set when it
    names another module or an attribute of a server global, None when it names the
    server but the scan cannot tell which attribute."""
    pieces = _string_pieces(node)
    head = ""
    for index, piece in enumerate(pieces):
        text = _string_text(piece, strings, aliases)
        if text is None:
            break
        head += text
    else:
        match = _FACADE_STRING.match(head)
        return {match.group(1)} if match else set()
    if not head.startswith(f"{_FACADE}."):
        return set()
    if head == f"{_FACADE}." and index == len(pieces) - 1:
        return _resolve_name(pieces[index], params)
    return None


def _patched_names_in(text: str) -> tuple[set[str], set[str]]:
    """``(names, dynamic)``: first-level names one test source rebinds on the server,
    and the enclosing functions of each patch whose name the scan cannot resolve --
    which fails the reach test closed unless it is a resolved one."""
    # Every spelling below names the server's dotted path or imports ``server`` from
    # the dashboard package, so a source with neither holds no server patch.
    if "dashboard.server" not in text and not (
        "kiro_crew.dashboard import" in text and re.search(r"\bserver\b", text)
    ):
        return set(), set()
    tree = ast.parse(text)
    aliases = _facade_aliases(tree)
    strings = _facade_strings(tree, aliases)
    found: set[str] = set()
    dynamic: set[str] = set()
    functions = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    seen: set[int] = set()
    scopes = [(tree, {}, "<module>")] + [
        (fn, _parametrized_strings(fn), fn.name) for fn in functions
    ]
    for scope, params, label in reversed(scopes):
        for node in ast.walk(scope):
            if id(node) in seen:
                continue
            seen.add(id(node))
            if isinstance(node, ast.Call):
                func = ast.unparse(node.func)
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                target = node.args[0] if node.args else kw.get("target")
                if target is not None and (
                    ast.unparse(target) in aliases or _imports_the_facade(target)
                ):
                    if func.endswith(_PATCH_CALLS):
                        name = (
                            node.args[1]
                            if len(node.args) >= 2
                            else kw.get("attribute", kw.get("name"))
                        )
                        resolved = _resolve_name(name, params) if name is not None else None
                        if resolved is None:
                            dynamic.add(label)
                        else:
                            found |= resolved
                    elif func.endswith("patch.multiple"):
                        if any(k.arg is None for k in node.keywords):
                            dynamic.add(label)
                        found |= {k for k in kw if k not in _MULTIPLE_OPTIONS}
                elif target is not None and func.split(".")[-1] in ("patch", "setattr", "delattr"):
                    resolved = _resolve_target(target, params, strings, aliases)
                    if resolved is None:
                        dynamic.add(label)
                    else:
                        found |= resolved
            elif isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and ast.unparse(target.value) in aliases:
                        found.add(target.attr)
    found |= set(re.findall(r"""["']kiro_crew\.dashboard\.server\.(\w+)["']""", text))
    return found, dynamic


def _facade_patched_names() -> set[str]:
    root = repo_root()
    here = Path(__file__).resolve()
    found: set[str] = set()
    unresolved = []
    for path in repo_files_named(".py"):
        parts = path.relative_to(root).parts
        in_tests = parts[0] == "test" or (parts[0] == "src" and "tests" in parts)
        if in_tests and path.resolve() != here:
            names, dynamic = _patched_names_in(path.read_text(encoding="utf-8", errors="replace"))
            found |= names
            for label in dynamic:
                key = (path.name, label)
                if key in _RESOLVED_DYNAMIC_PATCHES:
                    found |= _RESOLVED_DYNAMIC_PATCHES[key]
                else:
                    unresolved.append(key)
    assert unresolved == [], "a test patches a server name the scan cannot resolve"
    return found


def _captured_names(source: str) -> set[str]:
    """Names an owner module binds or evaluates when it LOADS, outside
    ``TYPE_CHECKING``: everything a later patch of the server cannot reach."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    found: set[str] = set()

    def loads(node: ast.AST) -> set[str]:
        return {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }

    def visit(statements: list[ast.stmt]) -> None:
        for node in statements:
            if id(node) in guarded:
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                found.update((a.asname or a.name).split(".")[0] for a in node.names)
                found.update(a.name.split(".")[-1] for a in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for part in node.decorator_list + node.args.defaults:
                    found.update(loads(part))
                for part in node.args.kw_defaults:
                    if part is not None:
                        found.update(loads(part))
            elif isinstance(node, ast.ClassDef):
                for part in node.decorator_list + node.bases + [k.value for k in node.keywords]:
                    found.update(loads(part))
                for stmt in node.body:
                    if isinstance(stmt, (ast.AnnAssign, ast.Assign)) and stmt.value is not None:
                        found.update(loads(stmt.value))
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("test", "iter", "items"):
                    value = getattr(node, field, None)
                    if isinstance(value, ast.AST):
                        found.update(loads(value))
                    elif isinstance(value, list):
                        for item in value:
                            found.update(loads(item))
                for block in ("body", "orelse", "finalbody"):
                    visit(getattr(node, block, []))
                for handler in getattr(node, "handlers", []):
                    if handler.type is not None:
                        found.update(loads(handler.type))
                    visit(handler.body)
            elif not (
                node is tree.body[0]
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found.update(loads(node))

    visit(tree.body)
    return found


def test_the_patch_scan_reads_every_spelling() -> None:
    planted = (
        "import importlib, sys\n"
        "import kiro_crew.dashboard.server as srv\n"
        "from kiro_crew.dashboard import server as sv\n"
        "facade = importlib.import_module('kiro_crew.dashboard.server')\n"
        "alias = facade\n"
        "held = sys.modules['kiro_crew.dashboard.server']\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(srv, 'first', 1)\n"
        "    monkeypatch.setattr(sv, 'second', 2)\n"
        "    patch.object(alias, 'third')\n"
        "    sv.fourth = 4\n"
        "    monkeypatch.setattr('kiro_crew.dashboard.server.fifth', 5)\n"
        "    monkeypatch.setattr(sv.web, 'attr', 7)\n"
        "    monkeypatch.setattr(other, 'not_the_facade', 8)\n"
        "    monkeypatch.delattr(sv, 'sixth')\n"
        "    patch.object(target=alias, attribute='seventh')\n"
        "    patch.multiple(held, eighth=1, create=True)\n"
        "    patch('kiro_crew.dashboard.server.KiroCrewConfig.load')\n"
        "@pytest.mark.parametrize('which', ['ninth', 'tenth'])\n"
        "def test_param(which):\n"
        "    patch(f'kiro_crew.dashboard.server.{which}')\n"
        "FACADE = 'kiro_crew.dashboard.server'\n"
        "other = 'kiro_crew.dashboard.state'\n"
        "def test_strings(monkeypatch):\n"
        "    path = FACADE\n"
        "    patch(f'{path}.eleventh')\n"
        "    patch(FACADE + '.twelfth')\n"
        "    monkeypatch.setattr(f'{sv.__name__}.thirteenth', 13)\n"
        "    patch(f'{other}.not_the_facade')\n"
        "    patch(f'{FACADE}_runtime.not_the_facade')\n"
    )
    names, dynamic = _patched_names_in(planted)
    assert names == {
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "eleventh",
        "twelfth",
        "thirteenth",
    }
    assert dynamic == set()
    unresolvable = (
        "from kiro_crew.dashboard import server as sv\n"
        "def _drive(monkeypatch, name):\n"
        "    monkeypatch.setattr(sv, name, 1)\n"
    )
    assert _patched_names_in(unresolvable) == (set(), {"_drive"})


def test_the_capture_scan_flags_what_an_owner_evaluates_when_it_loads() -> None:
    planted = (
        '"""An owner."""\n'
        "from typing import TYPE_CHECKING\n"
        "import asyncio as aio\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.dashboard.server import logger\n"
        "LIMIT = _CAP * 2\n"
        "def f(x=_DEFAULT, *, y=_KW):\n"
        "    return logger, sel\n"
        "class C(_Base):\n"
        "    attr: int = _CLASS_BODY\n"
    )
    captured = _captured_names(planted)
    assert {"aio", "asyncio", "_CAP", "_DEFAULT", "_KW", "_Base", "_CLASS_BODY"} <= captured
    assert {"logger", "sel"} & captured == set()


def test_no_owner_captures_a_name_tests_rebind_on_the_facade() -> None:
    """An owner that imported, defaulted or evaluated a rebound name when it loaded
    would keep that object, and a patch of the server would silently stop applying
    there. An owner may DEFINE one: the server's binding of it is the composed copy,
    and every caller reads it through the server's globals."""
    patched = _facade_patched_names()
    assert {
        "sel",
        "sel_is_warm",
        "data_home",
        "current_context",
        "KiroCrewConfig",
        "DashboardState",
        "_bind_once",
        "_start_site",
        "_reserve_dashboard_port",
        "_start_unix_site",
        "_extra_frame_ancestors",
        "_write_secret_file",
        "logger",
        "start_enabled_app_backends",
    } <= patched
    assert len(patched) >= 50
    for stem, source in _owner_sources().items():
        assert _captured_names(source) & patched == set(), stem
        defined = {name for name in vars(_owner(stem)) if name in patched}
        assert all(getattr(server, name) is vars(_owner(stem))[name] for name in defined), stem


# ── the boot ──────────────────────────────────────────────────────────────────

#: The full middleware chain of each entrypoint, outermost first, by the name of each
#: layer and the factory that built it. The order is a security contract: latency is
#: outermost, the deny-audit boundary is outer to every barrier that can refuse, the
#: SEL request audit is inner to them, the per-slot ownership checkpoint is inner to
#: token auth and the audit record, and the workflow and crewmate-prune gates are
#: appended after the explicit list.
_DASHBOARD_CHAIN = (
    ("route_latency_middleware", "make_route_latency_middleware.<locals>."),
    ("deny_audit_middleware", "_make_deny_audit_middleware.<locals>."),
    ("host_canonical_redirect", "build_host_canonical_redirect.<locals>."),
    ("host_validation_middleware", "_make_host_validation_middleware.<locals>."),
    ("reject_compressed_body_middleware", "reject_compressed_body_middleware"),
    ("no_cache_middleware", "_install_dashboard_middlewares.<locals>."),
    ("csrf_middleware", "_make_csrf_middleware.<locals>."),
    ("middleware", "token_auth_middleware.<locals>."),
    ("sel_audit_middleware", "_install_dashboard_middlewares.<locals>."),
    ("slot_ownership_middleware", "slot_ownership_middleware"),
    ("spa_fallback", "_install_dashboard_middlewares.<locals>."),
    ("_workflow_ready", "_register_workflow_lifecycle.<locals>."),
    ("_crewmate_prune_gate", "_register_crewmate_prune_gate.<locals>."),
)
_API_CHAIN = (
    ("route_latency_middleware", "make_route_latency_middleware.<locals>."),
    ("deny_audit_middleware", "_make_deny_audit_middleware.<locals>."),
    ("host_validation_middleware", "_make_host_validation_middleware.<locals>."),
    ("reject_compressed_body_middleware", "reject_compressed_body_middleware"),
    ("csrf_middleware", "_make_csrf_middleware.<locals>."),
    ("middleware", "token_auth_middleware.<locals>."),
    ("sel_audit_middleware", "_install_api_middlewares.<locals>."),
    ("slot_ownership_middleware", "slot_ownership_middleware"),
    ("_workflow_ready", "_register_workflow_lifecycle.<locals>."),
)

#: The lifecycle hooks the server registers, in registration order, by name; hooks
#: route slices and handler modules register are not the server's and are left out.
_DASHBOARD_HOOKS = {
    "on_startup": (
        "_hooks_startup",
        "_contrib_startup",
        "_stt_startup",
        "_own_host_warm",
        "_instances_startup",
        "_browser_sessions_startup",
    ),
    "on_cleanup": (
        "_tunnel_shutdown",
        "_status_sink_shutdown",
        "_hooks_shutdown",
        "_contrib_shutdown",
        "_watchdog_shutdown",
        "_diag_recorder_shutdown",
        "_prevent_sleep_shutdown",
        "_listener_guard_shutdown",
        "_kiro_prerequisite_shutdown",
        "_kas_login_shutdown",
        "_stt_shutdown",
        "_local_decision_model_shutdown",
        "_config_watch_shutdown",
        "_instances_shutdown",
        "_crew_log_drain",
        "_browser_install_shutdown",
        "_browser_view_shutdown",
        "_connections_warm_shutdown",
        "_workflow_shutdown",
        "_unlink_unix_socket",
    ),
    "on_shutdown": ("_workflow_stop_publication",),
    "on_response_prepare": ("_finalize_asset_cache_control",),
}
_API_HOOKS = {
    "on_startup": ("_stt_startup", "_own_host_warm"),
    "on_cleanup": (
        "_kiro_prerequisite_shutdown",
        "_kas_login_shutdown",
        "_stt_shutdown",
        "_local_decision_model_shutdown",
        "_config_watch_shutdown",
        "_prevent_sleep_shutdown",
        "_listener_guard_shutdown",
        "_browser_install_shutdown",
        "_connections_warm_shutdown",
        "_workflow_shutdown",
        "_unlink_unix_socket",
    ),
    "on_shutdown": ("_workflow_stop_publication",),
    "on_response_prepare": (),
}

#: The server calls each boot makes from its own task, in order, by server global name;
#: the ones its teardown makes; and, as a multiset, the ones made ELSEWHERE -- on a
#: worker thread (``T:``) or by another task on the loop (``S:``) -- over boot and
#: teardown together. Captured from the one-module file and pinned so the split could
#: not reorder a step, drop one or move one across the bind; the boot phases the
#: entrypoints now delegate to are names the base did not have, so only the calls
#: they make count. A boot step added on purpose updates these with it.
_DASHBOARD_BOOT = tuple("""
    consume_managed_service_launch_environment set_pending_staged_hook
    set_pending_consumed_hook set_global_hook_store register_skill_read_observer
    wire_session_subagent_probe _wire_tunnel_shutdown _wire_status_delta_sink
    register_status_delta_sink _precompute_telemetry current_context
    _register_mcp_routes _deferred _deferred setup_spawn_resume_routes
    _deferred_work_ledger _deferred_work_ledger _deferred_work_ledger
    _deferred_work_ledger _deferred_work_ledger _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred
    register_all subprocess_executor subprocess_executor subprocess_executor
    subprocess_executor subprocess_executor subprocess_executor _register_deploy_routes
    setup_knowledge_routes setup_weixin_routes setup_feedback_routes
    setup_secrets_routes setup_whatsapp_routes setup_link_meta_routes bind_address_for
    _reserve_dashboard_port subprocess_executor init_hooks_system safe_context_call
    current_context _register_dist_static_routes _dist_file_handler _dist_file_handler
    _dist_file_handler _dist_file_handler _dist_file_handler discover_app_window_entries
    register_app_window_paths _install_asset_cache_control_finalizer
    tailnet_effective_allowed_logins tailnet_identity_unknown degraded_config_files
    build_allowed_origins _make_host_validation_middleware _make_csrf_middleware
    _make_deny_audit_middleware resolve_dashboard_host build_host_canonical_redirect
    warm_auth_singletons warm_sel_singleton make_route_latency_middleware
    _mixed_internal_api_paths safe_context_call current_context token_auth_middleware
    _register_prevent_sleep_shutdown _register_listener_guard_shutdown
    _register_stt_hooks _register_own_host_warm _register_config_watch
    _register_instances_hooks _register_browser_install_cleanup
    _register_browser_view_cleanup _register_connections_warm_lifecycle
    _register_workflow_lifecycle _register_crewmate_prune_gate
    _register_unix_socket_cleanup build_hardened_runner on_gateway_startup
    init_hook_reconciler async_safe_context_call current_context _arm_listener_guard
    _kick_crewmate_prune subprocess_executor _start_unix_site _resolved_bound_port
    _start_secondary_loopback_site subprocess_executor _note_listener_sidecar
    _reconcile_listener_publication _kick_workflow_initialization
    _kick_connections_warm_scavenge _kick_session_search_index _kick_config_watch
    _kick_local_decision_model _kick_knowledge_orphan_reclaim _kick_owner_only_sweep
    load_loop_stall_exit_after
    _arm_prevent_sleep_poll safety_override safety_override safety_override
    await_crewmate_prune_settled current_context record_boot_to_ready
    _dispatch_healthy_boot_marker
    """.split())
_DASHBOARD_TEARDOWN = tuple("""
    current_context unregister_status_delta_sink stop_hook_reconciler
    on_gateway_shutdown async_safe_context_call current_context
    """.split())
_DASHBOARD_ELSEWHERE = tuple("""
    S:_initialize_workflow_service S:_prune_browser_snapshots_loop S:_stt_idle_sweep
    S:_stt_startup_prewarm T:_apply_startup_yolo T:_claimed_dashboard_slots
    T:_live_sibling_port T:_register_deploy_skills T:_take_prior_dropped_grant
    T:_write_instance_credentials T:_write_secret_file T:_write_secret_file
    T:_write_secret_file T:apply_config_duration T:cleanup_migrated_builtin
    T:cleanup_migrated_builtin T:cleanup_migrated_builtin T:cleanup_migrated_builtin
    T:migrate_channel_transcripts T:newest_dump_with_stacks T:open_dump_file
    T:prune_synced_crewmates T:record_healthy_boot T:refresh_config_meta_stamp
    T:refresh_materialized_agents
    T:register_builtin_apps T:rotate_dumps T:start_deferred_app_backends
    T:start_enabled_app_backends T:sweep_stale_dumps T:take_dropped_grant
    T:tighten_data_home T:warm_own_host_names
    """.split())
_API_BOOT = tuple("""
    set_global_hook_store register_skill_read_observer wire_session_subagent_probe
    _precompute_telemetry current_context tailnet_effective_allowed_logins
    tailnet_identity_unknown degraded_config_files build_allowed_origins
    _make_host_validation_middleware _make_csrf_middleware _make_deny_audit_middleware
    warm_auth_singletons warm_sel_singleton make_route_latency_middleware
    _mixed_internal_api_paths safe_context_call current_context token_auth_middleware
    _register_mcp_routes _deferred _deferred setup_spawn_resume_routes
    _deferred_work_ledger _deferred_work_ledger _deferred_work_ledger
    _deferred_work_ledger _deferred_work_ledger _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred
    _register_deploy_routes _register_stt_hooks _register_own_host_warm
    _register_config_watch _register_prevent_sleep_shutdown
    _register_listener_guard_shutdown _register_browser_install_cleanup
    _register_connections_warm_lifecycle _register_workflow_lifecycle
    _register_unix_socket_cleanup build_hardened_runner bind_address_for _start_site
    _arm_listener_guard _export_bound_port _resolved_bound_port _start_unix_site
    _resolved_bound_port _resolved_bound_host _start_secondary_loopback_site
    subprocess_executor _resolved_bound_host _resolved_bound_host _note_listener_sidecar
    _resolved_bound_host _reconcile_listener_publication _kick_workflow_initialization
    _kick_connections_warm_scavenge _kick_session_search_index _kick_config_watch
    _kick_local_decision_model _kick_owner_only_sweep _arm_prevent_sleep_poll
    record_boot_to_ready _dispatch_healthy_boot_marker
    """.split())
_API_TEARDOWN: tuple[str, ...] = ()
_API_ELSEWHERE = tuple("""
    S:_initialize_workflow_service S:_stt_idle_sweep S:_stt_startup_prewarm
    T:_live_sibling_port T:_write_instance_credentials T:_write_secret_file
    T:_write_secret_file T:_write_secret_file T:record_healthy_boot T:tighten_data_home
    T:warm_own_host_names
    """.split())

#: Done callbacks of tasks the boot creates, whose position depends on when a worker
#: thread finishes rather than on the boot's own order.
_TIMING_DEPENDENT = frozenset({"_own_host_warm_done", "_log_prewarm_outcome"})


def _install_trace(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Wrap every server global that is a project function or a test double, so each
    call records its name: bare from the calling task, ``S:<name>`` from another task
    or a loop callback, ``T:<name>`` from a worker thread."""
    trace: list[str] = []
    own = asyncio.current_task()

    def _label(name: str) -> str:
        if threading.current_thread() is not threading.main_thread():
            return f"T:{name}"
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        return name if task is own else f"S:{name}"

    for name, value in list(vars(server).items()):
        if name.startswith("__") or isinstance(value, (type, types.ModuleType)):
            continue
        if isinstance(value, (MagicMock, AsyncMock)):

            def _double(*a: Any, __name: str = name, __target: Any = value, **k: Any) -> Any:
                trace.append(_label(__name))
                return __target(*a, **k)

            monkeypatch.setattr(server, name, _double)
            continue
        if not isinstance(value, types.FunctionType):
            continue
        if not (getattr(value, "__module__", "") or "").startswith("kiro_crew"):
            continue
        if inspect.iscoroutinefunction(value):

            async def _async(*a: Any, __name: str = name, __fn: Any = value, **k: Any) -> Any:
                trace.append(_label(__name))
                return await __fn(*a, **k)

            wrapped: Any = functools.wraps(value)(_async)
        else:

            def _sync(*a: Any, __name: str = name, __fn: Any = value, **k: Any) -> Any:
                trace.append(_label(__name))
                return __fn(*a, **k)

            wrapped = functools.wraps(value)(_sync)
        monkeypatch.setattr(server, name, wrapped)
    return trace


def _own(trace: list[str]) -> tuple[str, ...]:
    return tuple(t for t in trace if ":" not in t and t not in _PHASES)


def _elsewhere(trace: list[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            t for t in trace if ":" in t and t.split(":", 1)[1] not in _PHASES | _TIMING_DEPENDENT
        )
    )


def _server_hooks(app: web.Application) -> dict[str, tuple[str, ...]]:
    return {
        signal: tuple(
            hook.__name__
            for hook in getattr(app, signal)
            if getattr(hook, "__module__", "") == _FACADE
        )
        for signal in ("on_startup", "on_cleanup", "on_shutdown", "on_response_prepare")
    }


def _chain_matches(app: web.Application, expected: tuple[tuple[str, str], ...]) -> None:
    middlewares = list(app.middlewares)
    assert [mw.__name__ for mw in middlewares] == [name for name, _ in expected]
    for mw, (name, built_by) in zip(middlewares, expected, strict=True):
        assert mw.__qualname__.startswith(built_by), (name, mw.__qualname__)


async def _traced_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[web.AppRunner, Any, list[str], list[str]]:
    """Boot the real dashboard through the startup-coverage harness, traced, with the
    second loopback family left unbound so the boot takes one path on every host."""
    import test_dashboard_server_startup_coverage as harness

    monkeypatch.setattr(server, "_start_secondary_loopback_site", AsyncMock(return_value=None))
    # The crewmate prune is kicked onto a worker thread a few steps before the boot
    # merges channel transcripts, and the merge kicks a deferred removal when the
    # prune has not settled yet. Which branch runs depends on the worker thread's
    # speed, so hold the merge until the prune has settled: the recorded order is
    # then the settled one on every runner, however slow.
    converge = server._converge_channel_transcripts

    @functools.wraps(converge)
    async def _converge_once_settled(state: Any) -> None:
        await asyncio.wait_for(state.crewmate_prune_settled.wait(), timeout=30)
        await converge(state)

    monkeypatch.setattr(server, "_converge_channel_transcripts", _converge_once_settled)
    holder: dict[str, list[str]] = {}
    real = server.start_dashboard

    async def _start(*args: Any, **kwargs: Any) -> Any:
        holder["trace"] = _install_trace(monkeypatch)
        return await real(*args, **kwargs)

    monkeypatch.setattr(server, "start_dashboard", _start)
    runner, state, _spies = await harness._start_dashboard(tmp_path, monkeypatch)
    boot = list(holder["trace"])
    del holder["trace"][:]
    return runner, state, boot, holder["trace"]


async def _traced_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[web.AppRunner, Any, list[str], list[str]]:
    import test_dashboard_server_coverage as harness

    monkeypatch.setattr(server, "_start_unix_site", AsyncMock(return_value=None))
    monkeypatch.setattr(server, "_start_secondary_loopback_site", AsyncMock(return_value=None))
    holder: dict[str, list[str]] = {}
    real = server.start_api_server

    async def _start(*args: Any, **kwargs: Any) -> Any:
        holder["trace"] = _install_trace(monkeypatch)
        return await real(*args, **kwargs)

    monkeypatch.setattr(server, "start_api_server", _start)
    runner, state = await harness._start_api(tmp_path, monkeypatch)
    boot = list(holder["trace"])
    del holder["trace"][:]
    return runner, state, boot, holder["trace"]


@pytest.mark.asyncio
async def test_the_dashboard_boot_keeps_its_chain_hooks_and_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_dashboard_server_startup_coverage as harness

    runner, state, boot, teardown = await _traced_dashboard(tmp_path, monkeypatch)
    try:
        app = runner.app
        _chain_matches(app, _DASHBOARD_CHAIN)
        token_auth = [mw for mw in app.middlewares if getattr(mw, "_is_token_auth", False)]
        assert [mw.__name__ for mw in token_auth] == ["middleware"]
        assert _server_hooks(app) == _DASHBOARD_HOOKS
        assert _own(boot) == _DASHBOARD_BOOT
    finally:
        await runner.cleanup()
        await harness._cancel_stray_tasks()
        harness._release_process_handles(state)
    assert _own(teardown) == _DASHBOARD_TEARDOWN
    assert _elsewhere(boot + teardown) == _DASHBOARD_ELSEWHERE


@pytest.mark.asyncio
async def test_the_headless_boot_keeps_its_chain_hooks_and_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_dashboard_server_startup_coverage as harness

    runner, _state, boot, teardown = await _traced_api(tmp_path, monkeypatch)
    try:
        app = runner.app
        _chain_matches(app, _API_CHAIN)
        assert _server_hooks(app) == _API_HOOKS
        assert _own(boot) == _API_BOOT
    finally:
        await runner.cleanup()
        await harness._cancel_stray_tasks()
    assert _own(teardown) == _API_TEARDOWN
    assert _elsewhere(boot + teardown) == _API_ELSEWHERE


def _routes(app: web.Application) -> list[tuple[str, str, str]]:
    return [
        (route.method, route.resource.canonical, getattr(route.handler, "__name__", ""))
        for route in app.router.routes()
        if route.resource is not None
    ]


#: SHA-256 of the MCP route table's ``"<method> <path> <handler>"`` rows in
#: registration order, and their count. The table is shared by both entrypoints, so a
#: route added to it on purpose updates these with it.
_MCP_TABLE_ROWS = 242
_MCP_TABLE_DIGEST = "af7da1dd963a0a957c39b2ee03d87f10cd0aa18417aead722214fd59a0b0ab25"


def test_the_mcp_route_table_keeps_its_rows_and_order() -> None:
    app = web.Application()
    server._register_mcp_routes(app)
    rows = [" ".join(row) for row in _routes(app)]
    digest = hashlib.sha256("\n".join(rows).encode()).hexdigest()
    assert (len(rows), digest) == (_MCP_TABLE_ROWS, _MCP_TABLE_DIGEST), "\n".join(rows)


def test_a_literal_mcp_route_precedes_the_pattern_that_would_swallow_it() -> None:
    """aiohttp resolves a request in REGISTRATION order, so a literal path registered
    after a pattern route that matches it is unreachable for that method."""
    app = web.Application()
    server._register_mcp_routes(app)
    shadowed = []
    seen: list[tuple[str, Any]] = []
    for route in app.router.routes():
        resource = route.resource
        if resource is None:
            continue
        info = resource.get_info()
        if "path" in info:
            for method, pattern in seen:
                if method in (route.method, "*") and pattern.fullmatch(info["path"]):
                    shadowed.append((route.method, info["path"]))
        elif "pattern" in info:
            seen.append((route.method, info["pattern"]))
    assert shadowed == []


@pytest.mark.asyncio
async def test_both_entrypoints_mount_the_one_mcp_table_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_dashboard_server_startup_coverage as harness

    table = web.Application()
    server._register_mcp_routes(table)
    expected = _routes(table)
    runner, state, _spies = await harness._start_dashboard(tmp_path / "dash", monkeypatch)
    try:
        assert _routes(runner.app)[: len(expected)] == expected
    finally:
        await runner.cleanup()
        await harness._cancel_stray_tasks()
        harness._release_process_handles(state)
    api_runner, _state, _boot, _teardown = await _traced_api(tmp_path / "api", monkeypatch)
    try:
        api_routes = _routes(api_runner.app)
        assert api_routes[: len(expected)] == expected
        probes = [path for _, path, _ in api_routes[len(expected) : len(expected) + 6]]
        assert probes == ["/api/health", "/api/health", "/api/live", "/api/live"] + [
            "/api/ready",
            "/api/ready",
        ]
    finally:
        await api_runner.cleanup()
        await harness._cancel_stray_tasks()


def test_the_build_routes_register_every_prefix_then_the_vendor_preflight(tmp_path: Path) -> None:
    app = web.Application()
    server._register_dist_static_routes(app, tmp_path / "dist")
    rows = [(m, p) for m, p, _ in _routes(app)]
    prefixes = ("assets", "sprites", "fonts", "vendor", "app-assets")
    assert rows == [
        (method, f"/{prefix}/{{tail}}") for prefix in prefixes for method in ("HEAD", "GET")
    ] + [("OPTIONS", "/vendor/{tail}")]


# ── the clauses the bootstrap carries ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "status", "expected"),
    [
        ("/assets/index-abc.js", 200, "public, max-age=31536000, immutable"),
        ("/assets/index-abc.js", 206, "public, max-age=31536000, immutable"),
        ("/assets/index-abc.js", 304, "public, max-age=31536000, immutable"),
        ("/assets/diffWorker-abc.js", 200, "public, max-age=60, stale-if-error=86400"),
        ("/assets/Subset-WORKER.chunk-1.js", 304, "public, max-age=60, stale-if-error=86400"),
        ("/assets/index-abc.js", 404, "no-store, no-cache, must-revalidate, max-age=0"),
        ("/assets/diffWorker-abc.js", 503, "no-store, no-cache, must-revalidate, max-age=0"),
        ("/vendor/react.js", 200, "no-store, no-cache, must-revalidate, max-age=0"),
        ("/api/status", 200, "no-store, no-cache, must-revalidate, max-age=0"),
    ],
)
@pytest.mark.asyncio
async def test_the_asset_cache_policy_by_status_and_filename(
    path: str, status: int, expected: str
) -> None:
    """Hashed chunks are immutable, workers short-lived, everything else and every
    error no-store -- decided early in the header middleware and corrected once the
    status is final."""
    app = web.Application()
    response = web.Response(status=200)
    server._apply_security_headers(response, app, path)
    response.set_status(status)
    request = make_mocked_request("GET", path, app=app)
    await server._finalize_asset_cache_control(request, response)
    early = server._asset_cache_control(path) if status in (200, 206, 304) else None
    if early is not None:
        assert response.headers["Cache-Control"] == early == expected
    else:
        assert response.headers["Cache-Control"] == expected
        assert response.headers["Pragma"] == "no-cache"
        assert response.headers["Expires"] == "0"


@pytest.mark.parametrize(
    ("app_claim", "internal", "proxied", "expected"),
    [
        ("", None, False, "dashboard_user"),
        ("", None, True, "dashboard_user_via_proxy"),
        ("my-app", None, False, "my-app"),
        ("my-app", True, False, "dashboard_user:my-app"),
        ("my-app", True, True, "dashboard_user:my-app_via_proxy"),
    ],
)
def test_the_audited_actor_names_the_app_and_the_transport(
    app_claim: str, internal: bool | None, proxied: bool, expected: str
) -> None:
    headers = {"X-Forwarded-For": "203.0.113.9"} if proxied else {}
    request = make_mocked_request("POST", "/api/x", headers=headers)
    request["app"] = app_claim
    if internal is not None:
        request["internal_auth"] = internal
    assert server.audit_actor(request, "dashboard_user") == expected


class _Watchdog:
    """A loop stall watchdog double recording what the heartbeat asks of it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def claim_lag_enrichment(self, lag: float) -> bool:
        self.calls.append(("claim", threading.current_thread().name))
        return sum(1 for name, _ in self.calls if name == "claim") == 1

    def beat(self) -> None:
        self.calls.append(("beat", threading.current_thread().name))

    def log_lag_enrichment(self, lag: float) -> None:
        self.calls.append(("log", threading.current_thread().name))


@pytest.mark.asyncio
async def test_the_heartbeat_claims_the_capture_before_it_beats_and_logs_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bounded enrichment per lag episode: the capture is claimed before the beat
    that would end the episode, and the collector runs on an executor thread behind a
    shielded, bounded wait, so the loop it watches never waits on it unbounded."""
    real_sleep = asyncio.sleep
    ticks: list[float] = []

    async def _sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        ticks.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    state = MagicMock()
    state.resource_pressure_notifier.maybe_sample = AsyncMock()
    trimmer = MagicMock()
    trimmer.maybe_trim = AsyncMock(return_value=0)
    watchdog = _Watchdog()
    task = server._start_loop_heartbeat(state, watchdog, trimmer)
    try:
        for _ in range(200):
            if sum(1 for name, _ in watchdog.calls if name == "beat") >= 3:
                break
            await real_sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    names = [name for name, _ in watchdog.calls]
    assert names[:4] == ["claim", "beat", "log", "claim"]
    main = threading.main_thread().name
    assert [thread for name, thread in watchdog.calls if name == "log"] != [main]
    assert all(thread == main for name, thread in watchdog.calls if name != "log")
    assert ticks and set(ticks) == {5.0}
    source = inspect.getsource(server._start_loop_heartbeat)
    assert "await asyncio.wait_for(asyncio.shield(capture), 2.0)" in source
    assert "None, _loop_watchdog.log_lag_enrichment, lag" in source


@pytest.mark.asyncio
async def test_the_local_decision_model_release_waits_for_its_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cleanup hook releases the local decision model only once its runtime module
    was imported at all -- a gateway that never ran one does not import it to stop it
    -- and the release waits for the model off the event loop."""
    import kiro_crew.decisions as decisions

    name = "kiro_crew.decisions.local_runtime"
    app = web.Application()
    server._register_stt_hooks(app)
    hook = next(h for h in app.on_cleanup if h.__name__ == "_local_decision_model_shutdown")
    monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delattr(decisions, "local_runtime", raising=False)
    await hook(app)
    assert name not in sys.modules
    calls: list[tuple[dict[str, Any], bool]] = []

    def _deactivate(**kwargs: Any) -> None:
        calls.append((kwargs, threading.current_thread() is threading.main_thread()))

    runtime = types.ModuleType(name)
    runtime.get_runtime = lambda: types.SimpleNamespace(deactivate=_deactivate)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, runtime)
    monkeypatch.setattr(decisions, "local_runtime", runtime, raising=False)
    await hook(app)
    assert calls == [({"wait": True}, False)]


# ── the owners' own behaviour ─────────────────────────────────────────────────


@web.middleware
async def _through(request: web.Request, handler: Any) -> web.StreamResponse:
    return await handler(request)


def _skill_settings() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        auto_create_from_sessions=True,
        auto_refine_on_deviation=False,
        auto_min_tool_calls=3,
        auto_similarity_threshold=0.8,
        approval_required=True,
        max_auto_skills=7,
        stale_after_days=30,
        archive_after_days=90,
        generate_scripts=False,
        judge_model="auto",
    )


def test_a_dashboard_only_launch_builds_its_own_consolidator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handed a conversation log but no consolidator, the dashboard builds one wired
    with the same skill defaults as the CLI; a failure leaves it without one."""
    built: list[dict[str, Any]] = []
    stores: list[MagicMock] = []

    def _consolidator(**kwargs: Any) -> str:
        built.append(kwargs)
        return "consolidator"

    def _store() -> MagicMock:
        stores.append(MagicMock())
        return stores[-1]

    monkeypatch.setattr("kiro_crew.history.HistoryConsolidator", _consolidator)
    monkeypatch.setattr("kiro_crew.memory.MemoryStore", _store)
    monkeypatch.setattr(server, "SkillsLoader", lambda **kwargs: ("loader", kwargs))
    loader = MagicMock()
    loader.load.return_value.skills = _skill_settings()
    monkeypatch.setattr(server, "KiroCrewConfig", loader)
    assert server._auto_create_consolidator("sessions", "lessons", None, "log") == "consolidator"
    stores[0].init.assert_called_once_with()
    assert built[0] == {
        "log": "log",
        "memory": stores[0],
        "sessions": "sessions",
        "lesson_store": "lessons",
        "skills_loader": ("loader", {"install_builtins": False}),
        "auto_skills_enabled": True,
        "auto_refine_enabled": False,
        "auto_min_tool_calls": 3,
        "auto_similarity_threshold": 0.8,
        "approval_required": True,
        "max_auto_skills": 7,
        "stale_after_days": 30,
        "archive_after_days": 90,
        "generate_scripts": False,
        "judge_model": "auto",
    }
    builder = types.SimpleNamespace(memory=MagicMock(), skills="builder-skills")
    assert server._auto_create_consolidator("s", "l", builder, "log") == "consolidator"
    builder.memory.init.assert_not_called()
    assert (built[1]["memory"], built[1]["skills_loader"]) == (builder.memory, "builder-skills")
    loader.load.side_effect = OSError("unreadable")
    assert server._auto_create_consolidator("s", "l", builder, "log") is None


def _skill_hooks(monkeypatch: pytest.MonkeyPatch, state: Any) -> tuple[Any, Any]:
    staged: list[Any] = []
    consumed: list[Any] = []
    monkeypatch.setattr(server, "set_pending_staged_hook", staged.append)
    monkeypatch.setattr(server, "set_pending_consumed_hook", consumed.append)
    server._register_pending_skill_hooks(state)
    return staged[0], consumed[0]


@pytest.mark.asyncio
async def test_a_staged_skill_rings_the_bell_on_the_serving_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate staged on a worker thread is announced on the gateway loop, and a
    consumed one retires its notification there."""
    state = MagicMock()
    state._background_tasks = set()
    state.serving_loop = asyncio.get_running_loop()
    state.resolve_skill_review_notifications = AsyncMock()
    on_staged, on_consumed = _skill_hooks(monkeypatch, state)
    info = {"slug": "tidy-up", "name": "Tidy", "kind": "update", "target": "tidy"}
    await asyncio.to_thread(on_staged, info)
    for _ in range(3):
        await asyncio.sleep(0)
    payload = {"slug": "tidy-up", "candidate_kind": "update", "target": "tidy"}
    title, body, url, actions = server._pending_skill_notification(info)
    state.notify.assert_called_once_with(
        "skills", title, body, meta=payload, url=url, actions=actions
    )
    state.broadcast_ws.assert_called_once_with("skills.pending_changed", payload)
    await asyncio.to_thread(on_consumed, {"slug": "tidy-up", "consumed_at": "t1"})
    await asyncio.to_thread(on_consumed, {"slug": "", "consumed_at": "t2"})
    for _ in range(5):
        await asyncio.sleep(0)
    state.resolve_skill_review_notifications.assert_awaited_once_with("tidy-up", "t1")
    assert state._background_tasks == set()


def test_without_a_serving_loop_the_bell_rings_inline_and_retires_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = MagicMock()
    state.serving_loop = None
    state.notify.side_effect = RuntimeError("feed unavailable")
    on_staged, on_consumed = _skill_hooks(monkeypatch, state)
    on_staged({"slug": "tidy-up", "name": "Tidy"})
    state.notify.assert_called_once()
    state.broadcast_ws.assert_not_called()
    on_consumed({"slug": "tidy-up", "consumed_at": "t1"})
    state.resolve_skill_review_notifications.assert_not_called()

    def _refuse(_hook: Any) -> None:
        raise RuntimeError("skills module unavailable")

    monkeypatch.setattr(server, "set_pending_staged_hook", _refuse)
    server._register_pending_skill_hooks(state)


class _Recorder:
    """A diagnostic recorder double: the source it is given, and its start."""

    instances: list[_Recorder] = []
    refuse_start = False

    def __init__(self) -> None:
        self.sources: dict[str, Any] = {}
        self.loops: list[Any] = []
        _Recorder.instances.append(self)

    def register_source(self, name: str, source: Any) -> None:
        self.sources[name] = source

    def start(self, loop: Any) -> None:
        if _Recorder.refuse_start:
            raise RuntimeError("no disk")
        self.loops.append(loop)


@pytest.mark.asyncio
async def test_the_recorder_row_reads_the_gateway_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each field of the gateway's recorder row comes from the canonical accessor and
    degrades to None on its own; a cap of 0 means unknown, never a cap of zero."""
    monkeypatch.setattr(_Recorder, "instances", [])
    monkeypatch.setattr(_Recorder, "refuse_start", False)
    monkeypatch.setattr("kiro_crew.diag.recorder.Recorder", _Recorder)
    state = types.SimpleNamespace(sessions=types.SimpleNamespace(count=2), subagents=None)
    server._start_diag_recorder(state)
    recorder = _Recorder.instances[0]
    assert recorder.loops == [asyncio.get_running_loop()]
    adaptive = {
        "enabled": True,
        "last": {"action": "hold", "reason": "ok", "signals": ["cpu"], "paused": False},
        "last_sample": {"running": 1, "queued": 4, "loop_lag_ms": 12},
    }
    monkeypatch.setattr("kiro_crew.resource_status.adaptive_state", lambda: adaptive)
    monkeypatch.setattr("kiro_crew.resource_status.adaptive_exec_cap", lambda: 0)
    monkeypatch.setattr("kiro_crew.metrics.inventory_gauges.read_active_monitor_loops", lambda: 3)
    assert recorder.sources["gateway"]() == {
        "sessions": 2,
        "subagents": 0,
        "live_loops": 3,
        "adaptive_cap": None,
        "adaptive_action": "hold",
        "adaptive_reason": "ok",
        "adaptive_signals": ["cpu"],
        "adaptive_paused": False,
        "adaptive_enabled": True,
        "subagents_running": 1,
        "subagents_queued": 4,
        "controller_loop_lag_ms": 12,
    }

    def _broken() -> Any:
        raise RuntimeError("gauge unreadable")

    for target in (
        "kiro_crew.resource_status.adaptive_state",
        "kiro_crew.resource_status.adaptive_exec_cap",
        "kiro_crew.metrics.inventory_gauges.read_active_monitor_loops",
    ):
        monkeypatch.setattr(target, _broken)
    state.sessions = None
    state.subagents = types.SimpleNamespace()
    row = recorder.sources["gateway"]()
    assert {k: row[k] for k in ("sessions", "subagents", "live_loops", "adaptive_cap")} == {
        "sessions": None,
        "subagents": None,
        "live_loops": None,
        "adaptive_cap": None,
    }
    monkeypatch.setattr(_Recorder, "refuse_start", True)
    server._start_diag_recorder(state)


@pytest.mark.asyncio
async def test_the_recorder_stop_is_awaited_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = web.Application()
    server._register_diag_recorder_shutdown(app)
    hook = app.on_cleanup[-1]
    finished: list[str] = []

    async def _finish() -> None:
        finished.append("joined")

    recorder = MagicMock()
    recorder.stop.side_effect = lambda: asyncio.ensure_future(_finish())
    monkeypatch.setattr("kiro_crew.diag.recorder.get_recorder", lambda: recorder)
    await hook(app)
    assert finished == ["joined"]
    recorder.stop.side_effect = RuntimeError("probe thread wedged")
    await hook(app)
    monkeypatch.setattr("kiro_crew.diag.recorder.get_recorder", lambda: None)
    await hook(app)


@pytest.mark.asyncio
async def test_the_watchdog_budget_falls_back_to_the_managed_service_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config failure must not erase the grace a managed service launch asked for:
    the watchdog is armed with the environment's budget and a fresh dump file."""
    steps: list[str] = []
    monkeypatch.setattr(server, "sweep_stale_dumps", lambda: steps.append("sweep"))
    monkeypatch.setattr(server, "rotate_dumps", lambda: steps.append("rotate"))
    monkeypatch.setattr(server, "open_dump_file", lambda: "dump.txt")

    def _unreadable(_environ: Any) -> float:
        raise OSError("config unreadable")

    monkeypatch.setattr(server, "load_loop_stall_exit_after", _unreadable)
    monkeypatch.setattr(server, "resolve_loop_stall_exit_after", lambda *, environ: 40)
    watchdog = MagicMock()
    monkeypatch.setattr(server, "LoopStallWatchdog", watchdog)
    assert await server._open_loop_watchdog({"KIROCREW_MANAGED": "1"}) is watchdog.return_value
    assert steps == ["sweep", "rotate"]
    watchdog.assert_called_once_with(dump_file="dump.txt", exit_after=40.0)


@pytest.mark.asyncio
async def test_a_blocked_loop_is_logged_and_a_dead_heartbeat_reports_itself(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A tick that comes back late logs the lag, a trim worth reporting is logged, a
    capture that fails does not stop the beat, and a heartbeat that dies says so."""
    import logging

    real_sleep = asyncio.sleep

    async def _sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        await real_sleep(0)

    clock = iter([0.0, 10.0, 10.0, 15.5])
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    monkeypatch.setattr(server, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))
    state = MagicMock()
    state.resource_pressure_notifier.maybe_sample = AsyncMock(
        side_effect=[None, RuntimeError("notifier gone")]
    )
    trimmer = MagicMock()
    trimmer.maybe_trim = AsyncMock(
        return_value=server.platform_compat.HEAP_TRIM_LOG_THRESHOLD_BYTES
    )
    watchdog = MagicMock()
    watchdog.claim_lag_enrichment.return_value = True
    watchdog.log_lag_enrichment.side_effect = RuntimeError("no stacks")
    with caplog.at_level(logging.DEBUG, logger=_FACADE):
        task = server._start_loop_heartbeat(state, watchdog, trimmer)
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(3):
            await real_sleep(0)
    messages = [record.getMessage() for record in caplog.records]
    assert "event-loop heartbeat: lag 5.0s (loop was blocked)" in messages
    assert "Gateway heap trim returned 16 MiB to the OS" in messages
    assert "heartbeat lag capture not awaited" in messages
    assert "event-loop heartbeat task exited unexpectedly" in messages
    assert watchdog.beat.call_count == 2


@pytest.mark.asyncio
async def test_a_prior_crash_dump_is_reported_once_with_its_stacks(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    import logging

    dump = tmp_path / "stall.txt"
    claims: list[Path] = []
    monkeypatch.setattr(server, "newest_dump_with_stacks", lambda: dump)
    monkeypatch.setattr(server, "dump_age_seconds", lambda path: 7200.0)
    monkeypatch.setattr(server, "claim_dump_notification", lambda path: claims.append(path) or True)
    monkeypatch.setattr(server, "dump_replay_lines", lambda path: (["frame a", "frame b"], True))
    monkeypatch.setattr(server, "data_home", lambda: tmp_path)
    monkeypatch.setattr(server, "attribute_dump", lambda path, home: ("attributed", path, home))
    monkeypatch.setattr(server, "describe", lambda attribution: ["It was running cron nightly"])
    state = MagicMock()
    with caplog.at_level(logging.WARNING, logger=_FACADE):
        await server._report_prior_crash_dump(state)
    assert claims == [dump]
    replay = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Replaying")]
    assert replay == [
        "Replaying prior crash dump stacks:\nframe a\nframe b\n"
        "  [truncated — full dump at above path]"
    ]
    state.notify.assert_called_once_with(
        "heartbeat",
        "⚠️ Gateway restarted after an event-loop stall",
        "The previous gateway stopped responding and exited 2.0h ago, then restarted. "
        "Work in flight at that moment was interrupted and not saved. "
        f"It was running cron nightly. Thread stacks: {dump}",
        meta={"url": "/settings", "dump": str(dump)},
    )

    def _unattributable(path: Path, home: Path) -> Any:
        raise ValueError("unparseable dump")

    monkeypatch.setattr(server, "attribute_dump", _unattributable)
    state.notify.side_effect = RuntimeError("feed unavailable")
    await server._report_prior_crash_dump(state)
    assert "Thread stacks" in state.notify.call_args.args[2]
    assert "It was running" not in state.notify.call_args.args[2]
    monkeypatch.setattr(server, "claim_dump_notification", lambda path: False)
    state.notify.reset_mock()
    await server._report_prior_crash_dump(state)
    state.notify.assert_not_called()


def _dashboard_chain(**overrides: Any) -> web.Application:
    app = web.Application()
    kwargs: dict[str, Any] = {
        "deny_audit_middleware": _through,
        "host_canonical_redirect": _through,
        "host_validation_middleware": _through,
        "csrf_middleware": _through,
        "internal_secret": "per-boot",
        "port": 25300,
        "local_only": True,
        "tailnet_trust": None,
        "tailnet_host": "",
        "configured_host": "127.0.0.1",
        "dashboard_url": "",
    }
    server._install_dashboard_middlewares(app, **(kwargs | overrides))
    return app


def _layer(app: web.Application, name: str) -> Any:
    return next(mw for mw in app.middlewares if mw.__name__ == name)


def _sel_double(monkeypatch: pytest.MonkeyPatch, *targets: str) -> MagicMock:
    log = MagicMock()
    for target in targets:
        monkeypatch.setattr(target, lambda: log)
    return log


@pytest.mark.asyncio
async def test_the_dashboard_inner_layers_serve_the_shell_and_audit_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A route-less navigation is answered with the SPA shell and an API miss is
    not; a mutating API call is recorded once, with its outcome, and re-raised."""
    shell = web.Response(text="shell")
    monkeypatch.setattr(server.handlers, "index", AsyncMock(return_value=shell))
    app = _dashboard_chain()
    spa = _layer(app, "spa_fallback")

    async def _missing(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPNotFound()

    assert await spa(make_mocked_request("GET", "/crew", app=app), _missing) is shell
    with pytest.raises(web.HTTPNotFound):
        await spa(make_mocked_request("GET", "/api/nothing", app=app), _missing)
    log = _sel_double(monkeypatch, "kiro_crew.sel.sel")
    audit = _layer(app, "sel_audit_middleware")

    async def _created(_request: web.Request) -> web.StreamResponse:
        return web.Response(status=201)

    async def _broken(_request: web.Request) -> web.StreamResponse:
        raise RuntimeError("handler failed")

    await audit(make_mocked_request("POST", "/api/things", app=app), _created)
    with pytest.raises(RuntimeError):
        await audit(make_mocked_request("DELETE", "/api/things", app=app), _broken)
    await audit(make_mocked_request("GET", "/api/things", app=app), _created)
    assert [c.kwargs for c in log.log_api_access.call_args_list] == [
        {
            "caller": "dashboard_user",
            "operation": "POST /api/things",
            "outcome": "ok",
            "resources": "/api/things",
        },
        {
            "caller": "dashboard_user",
            "operation": "DELETE /api/things",
            "outcome": "error",
            "resources": "/api/things",
            "error": "handler failed",
        },
    ]


def test_a_remote_dashboard_url_is_refused_without_token_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dashboard.url`` widens the CSRF origin set, which is safe only behind token
    auth: with it the origin is added, without it the dashboard refuses to start."""
    app = _dashboard_chain(dashboard_url="https://crew.example.test")
    assert {"https://crew.example.test"} <= app["allowed_origins"]
    monkeypatch.setattr(server, "token_auth_middleware", lambda **kwargs: _through)
    with pytest.raises(RuntimeError, match="dashboard_url requires token auth middleware"):
        _dashboard_chain(dashboard_url="https://crew.example.test")


@pytest.mark.asyncio
async def test_the_headless_audit_records_every_api_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = web.Application()
    server._install_api_middlewares(
        app,
        deny_audit_middleware=_through,
        host_validation_middleware=_through,
        csrf_middleware=_through,
        internal_secret="per-boot",
        port=25300,
        local_only=True,
        tailnet_trust=None,
    )
    log = _sel_double(monkeypatch, f"{_FACADE}.sel")
    audit = _layer(app, "sel_audit_middleware")

    async def _broken(_request: web.Request) -> web.StreamResponse:
        raise RuntimeError("tool failed")

    async def _page(_request: web.Request) -> web.StreamResponse:
        return web.Response(text="page")

    with pytest.raises(RuntimeError):
        await audit(make_mocked_request("GET", "/api/spawn", app=app), _broken)
    assert (await audit(make_mocked_request("GET", "/health", app=app), _page)).text == "page"
    assert [c.kwargs for c in log.log_api_access.call_args_list] == [
        {
            "caller": "mcp_tool",
            "operation": "GET /api/spawn",
            "outcome": "error",
            "resources": "/api/spawn",
            "error": "tool failed",
        }
    ]


def test_a_rebound_listener_republishes_the_sidecar_it_owns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rebind re-advertises the claim recorded at publication, with this
    generation's secret; a failed write is logged and leaves the address unnoted."""
    written: list[tuple[Path, str]] = []
    noted: list[tuple[int, str]] = []
    monkeypatch.setattr(
        server,
        "run_marker",
        types.SimpleNamespace(
            listener_secret_path=lambda port, address: tmp_path / f"{port}-{address}",
            note_published_listener=lambda port, address: noted.append((port, address)),
        ),
    )
    monkeypatch.setattr(server, "_write_secret_file", lambda p, s: written.append((p, s)))
    state = types.SimpleNamespace()
    server._republish_listener_sidecar(state, "secondary")
    server._note_listener_sidecar(state, "secondary", 25301, "", "minted")
    assert not hasattr(state, "_listener_sidecars")
    server._note_listener_sidecar(state, "secondary", 25301, "::1", "minted")
    server._note_listener_sidecar(state, "primary", 25301, "127.0.0.1", "")
    server._republish_listener_sidecar(state, "primary")
    server._republish_listener_sidecar(state, "secondary")
    assert (written, noted) == ([(tmp_path / "25301-::1", "minted")], [(25301, "::1")])

    def _refuse(path: Path, secret: str) -> None:
        raise PermissionError("read-only run dir")

    monkeypatch.setattr(server, "_write_secret_file", _refuse)
    server._republish_listener_sidecar(state, "secondary")
    assert noted == [(25301, "::1")]


def test_a_second_listener_that_cannot_withdraw_ends_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the second loopback family degrades to an explicit sign-in only while
    its sidecar was withdrawn; otherwise both guards are asked for the exit."""
    secondary = server.SecondaryLoopback(address="::1", site=MagicMock())
    state = types.SimpleNamespace(_listener_guard=MagicMock(), _secondary_listener_guard=None)
    monkeypatch.setattr(server, "_withdraw_listener_sidecar", lambda s, which: True)
    server._secondary_listener_given_up(state, 25301, secondary, "rebind refused")
    state._listener_guard.request_exit.assert_not_called()
    monkeypatch.setattr(server, "_withdraw_listener_sidecar", lambda s, which: False)
    state._secondary_listener_guard = MagicMock()
    server._secondary_listener_given_up(state, 25301, secondary, "rebind refused")
    state._listener_guard.request_exit.assert_called_once_with("rebind refused")
    state._secondary_listener_guard.request_exit.assert_called_once_with("rebind refused")


@pytest.mark.asyncio
async def test_the_workflow_service_hooks_redact_announce_and_authorize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workflow service's callbacks: a run event is broadcast with its session, a
    finished run's result opens an agent turn whose prompt names the run and its
    status, and a workflow nudge goes through the shared authorization chokepoint."""
    created: dict[str, Any] = {}

    async def _create(**kwargs: Any) -> Any:
        created.update(kwargs)
        return types.SimpleNamespace(timeout_secs=kwargs["timeout_secs"], registry=MagicMock())

    monkeypatch.setattr("kiro_crew.workflows.service.WorkflowService.create", _create)
    slot = MagicMock()
    slot.enqueue_or_run_prompt.return_value = True

    async def _inject(state_: Any, run_id: str, snapshot: dict, *, on_injected: Any) -> None:
        on_injected(slot, snapshot)
        on_injected(None, snapshot)

    monkeypatch.setattr("kiro_crew.dashboard.workflow_inject.inject_bound_workflow_result", _inject)
    loader = MagicMock()
    loader.load.side_effect = OSError("config unreadable")
    monkeypatch.setattr(server, "KiroCrewConfig", loader)
    state = MagicMock()
    state._background_tasks = set()
    state.workflow_startup_stopping = False
    state.sessions.admission_closed = False
    state.task_runner = None
    await server._initialize_workflow_service(state)
    assert state.workflow_startup_status == "ready"
    assert created["timeout_secs"] is None and created["concurrency"] == 4
    state.workflow_service.registry.get.return_value = types.SimpleNamespace(session_key="s1")
    created["on_event"]("run-1", {"kind": "step"})
    state.broadcast_ws.assert_called_once_with(
        "workflow_run_event", {"run_id": "run-1", "session_key": "s1", "kind": "step"}
    )
    state.broadcast_ws.side_effect = RuntimeError("socket gone")
    created["on_event"]("run-1", {"kind": "step"})

    created["on_done"]("run-1", {"name": "nightly", "status": "done"})
    for _ in range(3):
        await asyncio.sleep(0)
    prompt = slot.enqueue_or_run_prompt.call_args.args[0]
    assert prompt.startswith("[Workflow `nightly` finished: done] Its result was just posted")
    state.push_slots_update.assert_called_once_with()
    assert state._background_tasks == set()

    authorize = AsyncMock(return_value=(None, "loop refused", 403))
    monkeypatch.setattr(server, "authorize_and_add_nudge", authorize)
    monkeypatch.setattr(server, "_autonudge_get", lambda: "autonudge")
    refusal = await created["nudge_authorizer"](
        slot_key="slot-1", message="keep going", idle_secs=60, max_cycles=3
    )
    assert refusal == "loop refused"
    authorize.assert_awaited_once_with(
        svc="autonudge",
        state=state,
        slot_key="slot-1",
        message="keep going",
        idle_secs=60,
        max_cycles=3,
        source="workflow",
    )


@pytest.mark.asyncio
async def test_the_optional_post_bind_work_never_fails_the_gateway(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A warm-mint shutdown, a decision-model resume and an own-address warm that
    fail are logged, and a resumed decision model says which one started."""
    import logging

    app = web.Application()
    server._register_connections_warm_lifecycle(app, MagicMock())
    shutdown = next(h for h in app.on_cleanup if h.__name__ == "_connections_warm_shutdown")
    monkeypatch.setattr(
        "kiro_crew.connections.warm.shutdown_warm_mint",
        AsyncMock(side_effect=RuntimeError("mint wedged")),
    )
    state = MagicMock()
    state._background_tasks = set()
    resume = AsyncMock(side_effect=["qwen-small", RuntimeError("no model")])
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.decisions.resume_local_decision_model", resume
    )
    with caplog.at_level(logging.INFO, logger=_FACADE):
        await shutdown(app)
        for _ in range(2):
            server._kick_local_decision_model(state)
            await asyncio.gather(*state._background_tasks)
        cancelled = asyncio.get_running_loop().create_future()
        cancelled.cancel()
        server._OWN_HOST_WARM_TASKS.add(cancelled)
        server._own_host_warm_done(cancelled)
    messages = [record.getMessage() for record in caplog.records]
    assert "Connections warm shutdown failed" in messages
    assert "local decision model: starting qwen-small" in messages
    assert "local decision model: resume at startup failed" in messages
    assert cancelled not in server._OWN_HOST_WARM_TASKS


# ── the guards keep their reach ───────────────────────────────────────────────

#: Constructs repository guards read in ``dashboard/server.py`` by path, with the count
#: each guard asserts: the strict internal-path literal, the single heap-trim
#: maintainer after the serving bind, the diag recorder's off switch, the channel
#: manager's broadcast hand-off, the inherited-trust teardown and its wiring, the
#: crew-log and browser-view drains, both runner constructions, both Kiro
#: prerequisite seeds, both skill-read observers, the migrated-builtin sweep, the
#: browser CLI override, the per-boot secret mint, the dashboard contributor seam and
#: both cron-folder loads. An owner that grew one would move it out of the guard's
#: sight, so each stays here.
_STAYS_IN_THE_FACADE = (
    (r"^_STRICT_INTERNAL_API_PATHS = frozenset\($", 1),
    (r"platform_compat\.HeapTrimMaintainer\(\)", 1),
    (r'os\.environ\.get\("KIROCREW_DIAG_RECORDER", ""\)\.strip\(\)\.lower\(\) in \(', 1),
    (r"broadcast_fn=state\.broadcast_ws", 1),
    (r"clear_trusted_sessions\(keep_policy=standing_trust\)", 1),
    (r"functools\.partial\(_clear_override_derived_trust, state\)$", 1),
    (r"functools\.partial\(_suspend_override_derived_trust, state\)$", 1),
    (r"^    def _on_override_expired\(source: str\) -> None:$", 1),
    (r"app\.on_cleanup\.append\(_crew_log_drain\)", 1),
    (r"app\.on_cleanup\.append\(_browser_view_shutdown\)", 1),
    (r"build_hardened_runner\(app", 2),
    (r"KiroPrerequisiteService,$", 2),
    (r"register_skill_read_observer\(", 2),
    (r"^    from kiro_crew\.apps\.builtins import _MIGRATED_BUILTINS$", 1),
    (r"asyncio\.to_thread\(browser_cli_launch\.cli_env_overrides\)", 1),
    (r"_internal_secret = os\.urandom\(16\)\.hex\(\)$", 2),
    (r"current_context\(\)\.dashboard\.contribute_routes\(app\)", 1),
    (r"await asyncio\.to_thread\(state\.load_cron_folders\)", 2),
)


@pytest.mark.parametrize(("pattern", "count"), _STAYS_IN_THE_FACADE)
def test_a_construct_a_guard_reads_in_the_facade_stays_there(pattern: str, count: int) -> None:
    facade = _FACADE_PATH.read_text(encoding="utf-8")
    assert len(re.findall(pattern, facade, re.MULTILINE)) == count
    holders = [
        stem
        for stem, source in _owner_sources().items()
        if re.search(pattern, source, re.MULTILINE)
    ]
    assert holders == []


def _globstar(pattern: str) -> re.Pattern[str]:
    """An AUTOSDE or path-filter pattern as a regex: ``**/`` spans zero or more
    directories, ``*`` and ``?`` stay inside one path segment."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:[^/]+/)*", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] in "*?":
            out, i = out + ("[^/]*" if pattern[i] == "*" else "[^/]"), i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def _owner_paths() -> list[str]:
    root = repo_root()
    return sorted(path.relative_to(root).as_posix() for path in _OWNER_DIR.glob("*.py"))


def _rules_missing_owners(rules: list[dict]) -> tuple[set[str], dict[str, list[str]]]:
    """``(ids of rules matching the server, {id: owner files those rules miss})``."""
    facade = "src/kiro_crew/dashboard/server.py"
    matched: set[str] = set()
    missing: dict[str, list[str]] = {}
    for rule in rules:
        patterns = [_globstar(p) for p in rule.get("file-patterns", [])]
        if not any(p.match(facade) for p in patterns):
            continue
        matched.add(rule["id"])
        gaps = [o for o in _owner_paths() if not any(p.match(o) for p in patterns)]
        if gaps:
            missing[rule["id"]] = gaps
    return matched, missing


def _autosde_rules() -> list[dict]:
    import yaml

    root = repo_root()
    return [
        rule
        for name in ("AUTOSDE.yaml", "website/AUTOSDE.yaml")
        for rule in yaml.safe_load((root / name).read_text(encoding="utf-8"))["custom-rules"]
    ]


def test_the_globstar_matcher_reads_patterns_as_the_reviewers_do() -> None:
    assert _globstar("src/a/**/*.py").match("src/a/b.py")
    assert _globstar("src/a/**/*.py").match("src/a/x/y/b.py")
    assert not _globstar("src/a/*.py").match("src/a/x/b.py")
    assert _globstar("src/a/**").match("src/a/x/b.py")


def test_every_review_rule_on_the_facade_also_covers_its_owners() -> None:
    """A rule that reviews the server reviews the code composed into it: an owner
    outside its patterns would take moved code out of that rule's sight."""
    rules = _autosde_rules()
    matched, missing = _rules_missing_owners(rules)
    assert "no-new-work-on-gateway-boot-path" in matched
    assert missing == {}
    narrowed = [
        (
            {
                **rule,
                "file-patterns": [p for p in rule["file-patterns"] if "server_runtime" not in p],
            }
            if rule["id"] == "no-new-work-on-gateway-boot-path"
            else rule
        )
        for rule in rules
    ]
    assert "no-new-work-on-gateway-boot-path" in _rules_missing_owners(narrowed)[1]


def test_the_container_smoke_lane_fires_on_the_owners() -> None:
    """The container contract's probe exemption is built by an owner, so the path
    filter that names the server names its owners too."""
    import yaml

    workflow = yaml.safe_load(
        (repo_root() / ".github/workflows/docker-smoke.yml").read_text(encoding="utf-8")
    )
    on = workflow.get("on", workflow.get(True))
    patterns = [_globstar(p) for p in on["pull_request"]["paths"]]
    assert any(p.match("src/kiro_crew/dashboard/server.py") for p in patterns)
    assert [o for o in _owner_paths() if not any(p.match(o) for p in patterns)] == []


def test_the_async_config_dir_guard_scans_every_owner_coroutine() -> None:
    """``test_no_config_dir_in_async`` checks only the files it lists, so an owner that
    defines a coroutine and is missing from the list would take it out of the guard."""
    import test_no_config_dir_in_async as guard

    listed = set(guard._ASYNC_CHECKED_FILES)
    with_coroutines = {
        f"dashboard/server_runtime/{stem}.py"
        for stem, source in _owner_sources().items()
        if any(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(ast.parse(source)))
    }
    assert len(with_coroutines) >= 20
    assert "dashboard/server.py" in listed
    assert with_coroutines - listed == set()


def test_the_redaction_census_classifies_the_owners() -> None:
    """An owner that calls a redactor is classified where the server's entry was."""
    from kiro_crew import security_posture

    rel = {
        f"dashboard/server_runtime/{stem}.py"
        for stem, source in _owner_sources().items()
        if re.search(r"\bStreamRedactor\(|\b\w*redact\w*\(|\.redact\(", source)
    }
    assert rel == {
        "dashboard/server_runtime/owner_notices.py",
        "dashboard/server_runtime/workflow_startup.py",
    }
    assert rel <= security_posture.NON_EGRESS_REDACTION_MODULES
    assert "dashboard/server.py" not in security_posture.NON_EGRESS_REDACTION_MODULES


def test_the_entrypoints_keep_the_cited_line_range() -> None:
    """Request-for-change documents cite ``dashboard/server.py`` by line, and the docs
    linter refuses a citation past the end of every file it can match."""
    assert len(_FACADE_PATH.read_text(encoding="utf-8").splitlines()) >= 1660
