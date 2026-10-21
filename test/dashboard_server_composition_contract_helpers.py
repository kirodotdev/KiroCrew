"""The server facade names and owner lookups the dashboard server contract tests share.

``_FACADE``, ``_FACADE_PATH``, ``_OWNER_PACKAGE``, ``_OWNER_DIR`` and ``_SRC`` name the
server module, its file, the owner package, its directory and the source root;
``_BASE_OWNERS`` and ``_PHASE_OWNERS`` record which owner holds each moved name and boot
phase; ``_owner`` and ``_owner_sources`` look owners up by stem; ``_run_child`` runs a
fresh interpreter; and ``_owner_runtime_edges``, with ``_import_targets``, ``_within``
and ``_type_checking_nodes``, finds an owner's import edges outside ``TYPE_CHECKING``.
``test_dashboard_server_composition_contract.py`` and its ``_reach``, ``_owners`` and
``_guards`` siblings import what they use of these by name.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import kiro_crew.dashboard.server as server
from kiro_crew.dashboard import server_runtime
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_FACADE = server.__name__
_FACADE_PATH = Path(server.__file__).resolve()
_OWNER_PACKAGE = server_runtime.__name__
_OWNER_DIR = Path(server_runtime.__file__).resolve().parent
_SRC = _FACADE_PATH.parents[2]


#: Each definition moved out of the one-module file and the owner its responsibility
#: puts it in. Adding or removing an owner changes the composition, so the set is
#: spelled out.
_BASE_OWNERS: dict[str, tuple[str, ...]] = {
    "api_entrypoint": (),
    "app_platform": (),
    "browser_lifecycle": (),
    "config_watch": ("_kick_config_watch", "_register_config_watch"),
    "crewmate_prune": tuple("""
        _claimed_dashboard_slots _crewmate_prune_gate_holds_path _kick_crewmate_prune
        _kick_deferred_transcript_removal _register_crewmate_prune_gate
        await_crewmate_prune_settled
        """.split()),
    "diagnostics": ("_precompute_telemetry",),
    "heartbeat": (),
    "inherited_trust": (),
    "instances_lifecycle": (),
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


def _owner(stem: str) -> types.ModuleType:
    return importlib.import_module(f"{_OWNER_PACKAGE}.{stem}")


def _owner_sources() -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8") for path in sorted(_OWNER_DIR.glob("[!_]*.py"))
    }


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
