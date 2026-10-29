"""The dashboard gateway bootstrap keeps its surface and contracts while its owners move.

``kiro_crew.dashboard.server`` is the gateway's import path and its patch surface.
``start_dashboard`` stays defined there as the dashboard's boot sequencer;
``start_api_server``, the helper families both entrypoints call, and the boot phases
they delegate to live in the modules of ``kiro_crew.dashboard.server_runtime``, and
``_facade_compose.compose`` runs every function those owners define on the server
module's globals. The server keeps the imports and the module state, with the
internal-path sets and the response-header values defined in two pure-data modules
beside it that only the server imports. The tests of this suite pin:

* the surface: every name the module bound before the split still resolves on it,
  and the seams other modules import keep their identity;
* the composition: every owner function runs on the server's globals, reads only
  names the server binds, and captures no name a test rebinds on the server;
* the boot: the full middleware order of both entrypoints, their lifecycle hooks,
  the MCP route table and the sequence of server calls a boot and a teardown make;
* the guards: what repository guards read by path stays in the file each guard reads,
  and every review rule and path filter keyed to the server covers its owners;
* the clauses the bootstrap carries, characterized where the owners hold them.

This file pins the surface and the composition, with the self-test of the owner
edge check. ``test_dashboard_server_composition_contract_reach.py`` pins the owners'
import edges, the patch reach and the boot, ``..._owners.py`` the clauses and the
owners' own behaviour, and ``..._guards.py`` the guards; the facade names and owner
lookups they share live in ``dashboard_server_composition_contract_helpers.py``.
"""

from __future__ import annotations

import ast
import builtins
import dis
import hashlib
import importlib
import importlib.util
import inspect
import pkgutil
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from dashboard_server_composition_contract_helpers import (
    _BASE_OWNERS,
    _FACADE,
    _FACADE_PATH,
    _OWNER_DIR,
    _OWNER_PACKAGE,
    _PHASE_OWNERS,
    _SRC,
    _import_targets,
    _owner,
    _owner_runtime_edges,
    _owner_sources,
    _run_child,
    _within,
)

import kiro_crew.dashboard.server as server
from kiro_crew._facade_compose import compose as facade_compose
from kiro_crew.dashboard import server_runtime

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

#: SHA-256 of the sorted ``"<name> <kind> <signature>"`` lines of every name in
#: ``_BASE_OWNERS``, captured from the one-module file before the split: each moved
#: name keeps the kind and signature it had there.
_BASE_SHAPE_DIGEST = "534c66ed75e197e3cff4a5d7488aa541ca7ee0da864fa6cce3e1418926a9063c"

#: The definitions of the one-module file that ``_BASE_OWNERS`` does not name: the two
#: entrypoints, and the helpers repository guards read by path or that own module
#: state -- the tailnet awake cache's reader, the inherited-trust teardown pair, the
#: browser lifecycle trio, the instances hooks with their revive policy, the pinned
#: order load and the healthy-boot marker. ``_FACADE_DEF_HOMES`` names the file each
#: lives in.
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
    "_kick_pinned_order_load",
    "start_dashboard",
    "start_api_server",
)

#: The file each of ``_FACADE_DEFS`` lives in: ``server`` is the server module itself,
#: which keeps ``start_dashboard`` because its body holds a baselined blocking-call
#: site (``.github/sync-io-in-async-fs-baseline.txt``) that would be a new offender
#: in any other file; every other stem is a ``server_runtime`` owner. Kept apart from
#: ``_BASE_OWNERS`` and its shape digest: the entrypoints' ``port`` default is
#: ``_DEFAULT_PORT``, which reads ``KIROCREW_PORT`` when ``kiro_crew.dashboard.state``
#: loads, so their signatures depend on the environment the suite runs in.
_FACADE_DEF_HOMES: dict[str, tuple[str, ...]] = {
    "server": ("start_dashboard",),
    "api_entrypoint": ("start_api_server",),
    "browser_lifecycle": (
        "_prune_browser_snapshots_loop",
        "_register_browser_install_cleanup",
        "_register_browser_view_cleanup",
    ),
    "heartbeat": ("_dispatch_healthy_boot_marker",),
    "inherited_trust": ("_clear_override_derived_trust", "_suspend_override_derived_trust"),
    "instances_lifecycle": (
        "_retake_hops_then_revive",
        "_revive_intended_instances",
        "_register_instances_hooks",
    ),
    "maintenance": ("_kick_pinned_order_load",),
    "prevent_sleep": ("_tailnet_publish_keeps_awake",),
}
_FACADE_DEF_HOME = {name: stem for stem, names in _FACADE_DEF_HOMES.items() for name in names}

#: The pure-data modules beside the server, by dotted name, and every name each binds.
#: The server imports all of them and nothing else imports either module, so the
#: server's binding is the one its owners read and the one a test rebinds.
_DATA_MODULES: dict[str, tuple[str, ...]] = {
    "kiro_crew.dashboard.server_internal_paths": (
        "_CSRF_SAFE_METHODS",
        "_MIXED_INTERNAL_API_PATHS",
        "_PRE_AUDIT_DENY_STATUSES",
        "_STRICT_INTERNAL_API_PATHS",
        "_VIA_PROXY_SUFFIX",
    ),
    "kiro_crew.dashboard.server_response_headers": (
        "_IMMUTABLE_CACHE_CONTROL",
        "_IMMUTABLE_PATH_PREFIXES",
        "_NO_STORE_CACHE_CONTROL",
        "_PERMISSIONS_POLICY",
        "_PNA_REQUEST_HEADER",
        "_PNA_RESPONSE_HEADER",
        "_VENDOR_CORS_HEADER_VALUE",
        "_VENDOR_PATH_PREFIX",
        "_VENDOR_PREFLIGHT_MAX_AGE_SECS",
        "_WORKER_ASSET_MARKER",
        "_WORKER_CACHE_CONTROL",
    ),
}

_MOVED = frozenset(name for names in _BASE_OWNERS.values() for name in names)


def _owners() -> list[types.ModuleType]:
    return [_owner(info.name) for info in pkgutil.iter_modules([str(_OWNER_DIR)])]


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
    is the one object ``test_denied_commands_security_own_host`` polls, and the tailnet awake
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
    "kiro_crew": "_hist_mod _rs model_registry stt",
    "kiro_crew.agent": "rebuild_agent_config rebuild_agent_config_reporting",
    "kiro_crew.change_card_catalog": "HOOKED_ROUTES",
    "kiro_crew.apps.bridges": "reconcile_enabled_app_resources",
    "kiro_crew.apps.default_installs": "reconcile_default_installs",
    "kiro_crew.apps.dev_mode": "init_dev_mode_watcher stop_dev_mode_watcher",
    "kiro_crew.apps.event_bus": "build_broadcast_fn",
    "kiro_crew.apps.execution": "builtin_app_agents builtin_app_mcp_servers builtin_app_names",
    "kiro_crew.apps.spawn_sdk": "build_spawn_impl",
    "kiro_crew.config": "live",
    "kiro_crew.config.live": "ConfigChange",
    "kiro_crew.config.resolution": "DEGRADED_WHOLE_CONFIG",
    "kiro_crew.connections.warm": "scavenge_warm_mint_artifacts shutdown_warm_mint",
    "kiro_crew.crew_log": "crew_log_emit",
    "kiro_crew.edition_locks": "local_enable_locked",
    "kiro_crew.dashboard": "crew_mode_migration display_verdicts",
    "kiro_crew.dashboard.chat": "_run_chat",
    "kiro_crew.dashboard.enterprise_gate_middleware": (
        "enterprise_gate_middleware_for install_enterprise_gate"
    ),
    "kiro_crew.enterprise_boot": "require_kiro_login",
    "kiro_crew.dashboard.handlers": "wf_handlers work_ledger",
    "kiro_crew.dashboard.handlers.ask_question": (
        "api_agent_ask_open api_agent_ask_wait api_agent_ask_withdraw "
        "api_ask_question api_ask_question_answer api_ask_question_dismiss "
        "api_ask_question_pending"
    ),
    "kiro_crew.dashboard.handlers.autonudge": (
        "api_autonudge_delete api_autonudge_fire api_autonudge_get api_autonudge_list "
        "api_autonudge_start api_autonudge_update api_monitor_clear api_monitor_create "
        "api_monitor_restart api_monitor_slot_get api_monitor_stop api_monitor_update "
        "api_monitors_list api_session_monitor_get"
    ),
    "kiro_crew.dashboard.handlers.change_cards": "change_card_middleware",
    "kiro_crew.dashboard.handlers.cron_board": "wire_app_cron_folder_creator",
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
    "kiro_crew.dashboard.slot_queue_repository": "drain_restored_parked_sends",
    "kiro_crew.dashboard.workflow_inject": "inject_bound_workflow_result",
    "kiro_crew.dashboard.workflow_session_port": "WorkflowSessionPort",
    "kiro_crew.decisions": "impl_jev local_runtime",
    "kiro_crew.diag.recorder": "_DiagRecorder get_recorder",
    "kiro_crew.memory_startup": "memory_prepared memory_startup_preparing",
    "kiro_crew.memory_startup_prebuild": "prebuild_startup_reads",
    "kiro_crew.history_index_worker": "SessionIndexWorkerSupervisor",
    "kiro_crew.kiro_prerequisite": "KiroPrerequisiteService register_turn_auth_service",
    "kiro_crew.hooks": "set_builtin_app_agents set_builtin_app_mcp_servers set_builtin_app_names",
    "kiro_crew.memory": "MemoryStore",
    "kiro_crew.messaging.session_trust": "clear_trusted_sessions",
    "kiro_crew.metrics": "_gauges",
    "kiro_crew.metrics.crew_log_export": "install_otel_crew_log_subscriber",
    "kiro_crew.page_cache": "read_now",
    "kiro_crew.security": "redact_credentials redact_exfiltration_urls",
    "kiro_crew.sel": "sel",
    "kiro_crew.slack.handler": "load_voice_reply_config",
    "kiro_crew.stt": "engine stt_models",
    "kiro_crew.shared_crew.wiring": "install prepare_gateway",
    "kiro_crew.shared_crew.host_hooks": "mark_sessions_restored",
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
    assert len(expected) == 106


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
    assert set(_FACADE_DEF_HOMES) - {"server"} <= names


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


#: SHA-256 of the same ``"<name> <kind> <signature>"`` lines for the helpers of
#: ``_FACADE_DEFS``, captured from the server module before they moved to their homes.
#: The two entrypoints are left out: their ``port`` default reads ``KIROCREW_PORT``.
_FACADE_HELPER_SHAPE_DIGEST = "54f69707c825bcf977e7e0ddde37d4469664ada8a855738842d4af0d75a49aee"


def test_the_moved_facade_helpers_keep_their_base_shapes() -> None:
    helpers = set(_FACADE_DEFS) - {"start_dashboard", "start_api_server"}
    lines = sorted(f"{name} {_shape(getattr(server, name))}" for name in helpers)
    assert len(lines) == 11
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    assert digest == _FACADE_HELPER_SHAPE_DIGEST, "\n".join(lines)


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
    only while no owner keeps a copy. The internal-path sets are defined in a
    pure-data module the server alone imports; their own test below pins that."""
    state = _module_assignments(_FACADE_PATH.read_text(encoding="utf-8"))
    assert {"logger", "_tailnet_awake_cache", "_OWN_HOST_WARM_TASKS", "_BASE_CSP"} <= state
    assert {"_STRICT_INTERNAL_API_PATHS", "_MIXED_INTERNAL_API_PATHS"} <= set(
        _DATA_MODULES["kiro_crew.dashboard.server_internal_paths"]
    )
    assert {"_CREWMATE_PRUNE_GATE_TIMEOUT_S", "_TIME_WAIT_BUDGET_SECS"} <= state
    owned = {stem: sorted(_module_assignments(source)) for stem, source in _owner_sources().items()}
    assert {stem: names for stem, names in owned.items() if names} == {}
    assert not hasattr(server, "_startup_tasks")


def _module_bindings(tree: ast.Module) -> set[str]:
    """Every name a module binds at top level: assignments and imports, the
    ``__future__`` import excepted."""
    names = _module_assignments(ast.unparse(tree))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module != "__future__":
            names |= {alias.asname or alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            names |= {alias.asname or alias.name.split(".")[0] for alias in node.names}
    return names


@pytest.mark.parametrize("module", sorted(_DATA_MODULES))
def test_a_data_module_holds_only_the_values_the_facade_rebinds(module: str) -> None:
    """Each name is bound on the server as the data module's own object, the module
    defines nothing but those names, and no owner binds one: a test that rebinds the
    server's name is the binding every owner function reads."""
    names = set(_DATA_MODULES[module])
    data = importlib.import_module(module)
    for name in sorted(names):
        assert getattr(server, name) is getattr(data, name), name
    tree = ast.parse(Path(data.__file__).read_text(encoding="utf-8"))
    code = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    assert [type(node).__name__ for node in ast.walk(tree) if isinstance(node, code)] == []
    assert _module_bindings(tree) == names
    assert {
        o.__name__: sorted(names & set(vars(o))) for o in _owners() if names & set(vars(o))
    } == {}


def _data_module_importers(sources: dict[str, str]) -> dict[str, list[str]]:
    """``{relpath: [data module, ...]}`` for every source that imports a data module,
    spelled any way: ``import``, ``from ... import``, relative, ``import_module``."""
    found: dict[str, list[str]] = {}
    for rel, text in sources.items():
        if "server_internal_paths" not in text and "server_response_headers" not in text:
            continue
        package = ".".join(Path(rel).with_suffix("").parts[:-1])
        hits = sorted(
            {
                module
                for _node, target in _import_targets(ast.parse(text), package)
                for module in _DATA_MODULES
                if _within(target, module)
            }
        )
        if hits:
            found[rel] = hits
    return found


def test_only_the_facade_imports_a_data_module() -> None:
    sources = {
        path.relative_to(_SRC).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((_SRC / "kiro_crew").rglob("*.py"))
        if "_vendor" not in path.parts
    }
    assert _data_module_importers(sources) == {
        _FACADE_PATH.relative_to(_SRC).as_posix(): sorted(_DATA_MODULES)
    }


def test_the_data_module_importer_scan_sees_every_spelling() -> None:
    planted = {
        "kiro_crew/dashboard/a.py": "from kiro_crew.dashboard import server_internal_paths\n",
        "kiro_crew/dashboard/b.py": (
            "import kiro_crew.dashboard.server_response_headers as headers\n"
        ),
        "kiro_crew/dashboard/c.py": (
            "from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS\n"
        ),
        "kiro_crew/dashboard/d.py": "from .server_response_headers import _PERMISSIONS_POLICY\n",
        "kiro_crew/dashboard/e.py": "from . import server_internal_paths\n",
        "kiro_crew/x/f.py": (
            "import importlib\n"
            "importlib.import_module('kiro_crew.dashboard.server_response_headers')\n"
        ),
        "kiro_crew/x/g.py": "NAME = 'server_internal_paths'\nimport os\n",
    }
    assert _data_module_importers(planted) == {
        "kiro_crew/dashboard/a.py": ["kiro_crew.dashboard.server_internal_paths"],
        "kiro_crew/dashboard/b.py": ["kiro_crew.dashboard.server_response_headers"],
        "kiro_crew/dashboard/c.py": ["kiro_crew.dashboard.server_internal_paths"],
        "kiro_crew/dashboard/d.py": ["kiro_crew.dashboard.server_response_headers"],
        "kiro_crew/dashboard/e.py": ["kiro_crew.dashboard.server_internal_paths"],
        "kiro_crew/x/f.py": ["kiro_crew.dashboard.server_response_headers"],
    }


def test_the_facade_definitions_each_have_one_home() -> None:
    assert sorted(_FACADE_DEF_HOME) == sorted(_FACADE_DEFS)
    assert len(_FACADE_DEF_HOME) == sum(map(len, _FACADE_DEF_HOMES.values())) == 13


@pytest.mark.parametrize("name", _FACADE_DEFS)
def test_a_facade_definition_lives_in_its_home_file(name: str) -> None:
    """``start_dashboard`` is defined in the server file and in no owner; each other
    definition in its owner's file, held by that owner alone, as the server's object."""
    stem = _FACADE_DEF_HOME[name]
    obj = getattr(server, name)
    holders = [o.__name__ for o in _owners() if name in vars(o)]
    if stem == "server":
        assert Path(obj.__code__.co_filename).resolve() == _FACADE_PATH
        assert holders == []
        return
    home = _owner(stem)
    assert Path(obj.__code__.co_filename).resolve() == Path(home.__file__).resolve()
    assert holders == [home.__name__]
    assert obj is vars(home)[name]


def test_every_base_definition_is_in_exactly_one_place() -> None:
    """The server defines what ``_FACADE_DEF_HOMES`` keeps in it and the owners hold
    the rest: together they are the one-module file's definitions, each once, each
    in the file its table names."""
    defined = {
        node.name
        for node in ast.parse(_FACADE_PATH.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert defined == set(_FACADE_DEF_HOMES["server"])
    assert len(set(_FACADE_DEFS) | _MOVED) == len(_FACADE_DEFS) + len(_MOVED) == 103
    places: dict[str, list[str]] = {name: ["server"] for name in defined}
    for path in sorted(_OWNER_DIR.glob("[!_]*.py")):
        for top in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                places.setdefault(top.name, []).append(path.stem)
    homes = {name: [_FACADE_DEF_HOME[name]] for name in _FACADE_DEFS}
    homes.update({name: [stem] for stem, names in _BASE_OWNERS.items() for name in names})
    assert {name: places.get(name, []) for name in homes} == homes


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


def test_the_facade_composes_through_the_shared_helper() -> None:
    """The facade composes its owners with the one shared helper,
    ``kiro_crew._facade_compose.compose``, calling it exactly once, and the owner
    package keeps no copy of its own: its ``__init__`` defines no function."""
    assert server._facade_compose is facade_compose
    source = Path(server.__file__).read_text(encoding="utf-8")
    assert source.count("\n_facade_compose(\n") == 1
    package = ast.parse(Path(server_runtime.__file__).read_text(encoding="utf-8"))
    defined = [
        node.name
        for node in package.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []


# ── one edge ──────────────────────────────────────────────────────────────────


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
