"""Non-chat HTTP handlers — status, system, cron, lessons, spawn, logs, SSE.

System metrics (CPU, memory, network, disk) are in ``handlers_system.py``.
This module re-exports ``api_status`` and ``api_system`` for backward compat.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

# Imports accessed by submodules via late-binding (_h.X pattern)
from kiro_crew.config.loader import KiroCrewConfig, config_dir, config_path  # noqa: F401
from kiro_crew.dashboard.handlers_system import (  # noqa: F401
    api_compliance_yolo_status,
    api_governance_channels,
    api_sso_ttl,
    api_status,
    api_system,
)
from kiro_crew.dashboard.origin import is_loopback  # noqa: F401
from kiro_crew.security import (  # noqa: F401
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.session import _sync_kill_provider  # noqa: F401


def sel():
    """Dynamic sel() that always resolves from kiro_crew.sel for test patching."""
    from kiro_crew.sel import sel as _s

    return _s()


logger = logging.getLogger(__name__)


# ── Cron & Lessons (extracted to handlers/cron.py) ──
from kiro_crew.dashboard.cron_inject import inject_cron_result_to_dashboard  # noqa: E402, F401

# ── Memory (extracted to handlers/memory.py) ──
from kiro_crew.dashboard.handlers._shared import (  # noqa: E402, F401
    _blocks_reads_session,
    _get_active_workspace,
    _get_lessons,
    _get_memory,
    _get_skills,
    _is_restricted_session,
    _resolve_aim_skill_path,
)

# ── Agents (extracted to handlers/agents.py) ──
from kiro_crew.dashboard.handlers.agents import (  # noqa: E402, F401
    _auto_install_agent,
    _find_agent_config,
    _get_config_lock,
    _installed_agent_config,
    api_agent_config,
    api_agent_detail,
    api_agents_installed,
    api_capability_agents_install,
    api_capability_agents_list,
    api_capability_agents_uninstall,
    api_capability_mcp_install,
    api_capability_mcp_list,
    api_capability_mcp_registry,
    api_capability_mcp_uninstall,
    api_capability_plugins_list,
    api_capability_plugins_sync,
    api_capability_skills_install,
    api_capability_skills_list,
    api_capability_skills_uninstall,
    api_config_schema,
    api_default_agent,
    api_effort_levels,
    api_kirocrew_agent_delete,
    api_kirocrew_agent_resolved_model,
    api_kirocrew_agent_update,
    api_kirocrew_agents,
    api_kirocrew_agents_create,
    api_kirocrew_agents_sync,
    api_models,
    api_slash_commands,
)

# ── Connections OAuth relay (handlers/connections.py) ──
from kiro_crew.dashboard.handlers.connections import api_mcp_oauth_relay  # noqa: E402, F401
from kiro_crew.dashboard.handlers.cron import (  # noqa: E402, F401
    api_cron_ack,
    api_cron_batch_delete,
    api_cron_cancel,
    api_cron_delete,
    api_cron_enable,
    api_cron_folders,
    api_cron_folders_create,
    api_cron_folders_delete,
    api_cron_folders_update,
    api_cron_history,
    api_cron_history_all,
    api_cron_history_detail,
    api_cron_run,
    api_cron_to_chat,
    api_cron_update,
    api_crons,
    api_crons_create,
    api_lessons,
    api_lessons_create,
    api_lessons_delete,
)

# ── Diagnostics / Report a Problem (handlers/diagnostics.py) ──
from kiro_crew.dashboard.handlers.diagnostics import (  # noqa: E402, F401
    api_diagnostics_collect,
    api_diagnostics_download,
)

# ── Files & Workspaces (extracted to handlers/files.py) ──
from kiro_crew.dashboard.handlers.files import (  # noqa: E402, F401
    _validate_dashboard_path,
    _write_file_restricted,
    api_browse_dirs,
    api_browse_files,
    api_dashboard_config,
    api_file_diff,
    api_file_download,
    api_file_raw,
    api_file_read,
    api_file_search,
    api_file_watch,
    api_file_write,
    api_outbox_download,
    api_outbox_list,
    api_outbox_notify,
    api_project_git,
    api_reveal_path,
    api_screenshot,
    api_slack_upload_file,
    api_upload,
    api_upload_file,
    api_workspaces,
    api_workspaces_create,
    api_workspaces_delete,
    api_workspaces_update,
)

# ── Hooks (extracted to handlers/hooks.py) ──
from kiro_crew.dashboard.handlers.hooks import (  # noqa: E402, F401
    _get_hook_store,
    _load_hook_context,
    _run_hook_agent,
    _run_hook_inner,
    _verify_hook_token,
    api_hook_detail,
    api_hook_test,
    api_hook_toggle,
    api_hooks,
    api_hooks_agent,
    api_hooks_create,
    api_kiro_hooks,
    api_webhook_context_delete,
    api_webhook_test,
    api_webhook_token_create,
    api_webhook_token_delete,
    api_webhooks,
    api_webhooks_switch,
)
from kiro_crew.dashboard.handlers.kiro_prerequisite import (  # noqa: E402, F401
    api_kiro_prerequisite_repair_specs,
    api_kiro_prerequisite_status,
)
from kiro_crew.dashboard.handlers.mcp import (  # noqa: E402, F401
    _bg_mcp_probe,
    _sync_mcp_to_agent,
    api_mcp_active,
    api_mcp_apply,
    api_mcp_gateway_enable,
    api_mcp_gateway_metrics,
    api_mcp_gateway_servers,
    api_mcp_gateway_set_poolable,
    api_mcp_gateway_status,
    api_mcp_global_scopes,
    api_mcp_probe,
    api_mcp_probe_cached,
    api_mcp_remove,
    api_mcp_server_detail,
    api_mcp_servers,
    api_mcp_sync,
    api_mcp_toggle,
    api_mcp_toggle_all,
    api_mcp_toggle_tool,
)
from kiro_crew.dashboard.handlers.mcp_apps import (  # noqa: E402, F401
    api_mcp_apps_call,
)
from kiro_crew.dashboard.handlers.memory import (  # noqa: E402, F401
    _get_vector_store,
    _redact_memory_field,
    _set_migrated,
    api_memory_consolidate,
    api_memory_context_preview,
    api_memory_disable_embeddings,
    api_memory_embedding_model,
    api_memory_embedding_status,
    api_memory_enable_embeddings,
    api_memory_episodic_delete,
    api_memory_episodic_list,
    api_memory_episodic_search,
    api_memory_events,
    api_memory_graph,
    api_memory_history,
    api_memory_import,
    api_memory_migrate,
    api_memory_observability,
    api_memory_preferences,
    api_memory_projects,
    api_memory_promote,
    api_memory_semantic,
    api_memory_semantic_delete,
    api_memory_semantic_write,
    api_memory_settings,
    api_memory_stats,
)

# ── Messaging (extracted to handlers/messaging.py) ──
from kiro_crew.dashboard.handlers.messaging import (  # noqa: E402, F401
    _redact,
    _resolve_session_target,
    _sanitize_blocks,
    api_browser_auth_retry,
    api_browser_command,
    api_browser_command_drain,
    api_browser_command_result,
    api_browser_config_get,
    api_browser_config_save,
    api_browser_event,
    api_browser_frame,
    api_browser_pump_audit,
    api_delete_message,
    api_discord_config_get,
    api_discord_config_save,
    api_notification_ack,
    api_notification_agent_push,
    api_notification_channel_settings,
    api_notification_channels,
    api_notification_delete,
    api_notification_unack,
    api_notifications,
    api_notifications_ack_all,
    api_notifications_clear,
    api_send_message,
    api_slack_config_get,
    api_slack_config_save,
    api_slack_manifest,
    api_slack_pins,
    api_slack_profile,
    api_slack_reactions,
    api_spawn,
    api_spawn_clear,
    api_spawn_continue,
    api_spawn_delete,
    api_spawn_list,
    api_spawn_lost,
    api_spawn_mark_collected,
    api_spawn_release,
    api_spawn_retry,
    api_spawn_status,
    api_spawn_steer,
    api_teams_activity,
    api_teams_config_get,
    api_teams_config_save,
    api_telegram_config_get,
    api_telegram_config_save,
    api_webex_config_get,
    api_webex_config_save,
    api_wecom_config_get,
    api_wecom_config_save,
)
from kiro_crew.dashboard.handlers.prompts import (  # noqa: E402, F401
    MAX_PROMPT_BYTES,
    _extract_sop_description,
    _find_prompt,
    _redact_prompt,
    api_prompt_detail,
    api_prompts,
    api_skill_detail,
    api_skill_file,
    api_skill_inject_on_trigger,
    api_skill_pending_approve,
    api_skill_pending_detail,
    api_skill_pending_dismiss,
    api_skill_pin,
    api_skill_tree,
    api_skills,
    api_skills_create,
    api_skills_pending,
)

# ── Sessions (extracted to handlers/sessions.py) ──
from kiro_crew.dashboard.handlers.sessions import (  # noqa: E402, F401
    _SHUTDOWN_TIMEOUT_SECS,
    _fetch_usage_bg,
    _parse_usage,
    _remove_slot_for_history_key,
    _reset_all_sessions,
    api_approval_resolve,
    api_approvals,
    api_session_archive_list,
    api_session_archive_read,
    api_session_delete,
    api_session_detail,
    api_session_keepalive,
    api_session_tool_policy,
    api_sessions,
    api_sessions_clear,
    api_sessions_context,
    api_sessions_health,
    api_sessions_memory,
    api_sessions_restart,
    api_sessions_search,
    api_sessions_summarize,
    api_sessions_usage,
)

# ── Side conversation (extracted to handlers/side.py) ──
from kiro_crew.dashboard.handlers.side import (  # noqa: E402, F401
    api_side_close,
    api_side_open,
    api_side_turn,
)

# ── Skill context budget (extracted to handlers/skill_budget.py) ──
from kiro_crew.dashboard.handlers.skill_budget import (  # noqa: E402, F401
    api_skills_budget,
)
from kiro_crew.dashboard.handlers.sso_login import (  # noqa: E402, F401
    api_sso_login_ws,
)
from kiro_crew.dashboard.handlers.steering import (  # noqa: E402, F401
    STEERING_FILE_MAX_BYTES,
    api_steering,
    api_steering_create,
    api_steering_detail,
    list_steering_blocking,
    resolve_steering_file,
    steering_roots,
)
from kiro_crew.dashboard.handlers.tailnet import (  # noqa: E402, F401
    api_tailnet_status,
)

# ── Task Runner (extracted to handlers/taskrunner.py) ──
from kiro_crew.dashboard.handlers.taskrunner import (  # noqa: E402, F401
    _run_refine,
    api_taskrunner_cancel,
    api_taskrunner_delete,
    api_taskrunner_execute_plan,
    api_taskrunner_export_yaml,
    api_taskrunner_from_chat,
    api_taskrunner_pause,
    api_taskrunner_plan,
    api_taskrunner_plan_cancel,
    api_taskrunner_plan_context,
    api_taskrunner_refine,
    api_taskrunner_refine_answer,
    api_taskrunner_refine_cancel,
    api_taskrunner_refine_status,
    api_taskrunner_rename,
    api_taskrunner_retry,
    api_taskrunner_start,
    api_taskrunner_status,
    api_taskrunner_to_chat,
    api_taskrunner_update_plan,
    api_taskrunner_update_task,
)
from kiro_crew.dashboard.handlers.telemetry import (  # noqa: E402, F401
    api_beacon_status,
    api_context_trace,
    api_telemetry_startup,
)
from kiro_crew.dashboard.handlers.terminal import (  # noqa: E402, F401
    api_terminal_complete,
    api_terminal_create,
    api_terminal_delete,
    api_terminal_list,
    api_terminal_redact,
    api_terminal_ws,
    poll_terminal_titles,
    reap_orphaned_terminals,
)

# ── Themes: HTTP handlers (extracted to handlers/themes.py) ──
from kiro_crew.dashboard.handlers.themes import (  # noqa: E402, F401
    api_theme_asset,
    api_theme_detail,
    api_theme_overlay,
    api_theme_topbar,
    api_themes,
    api_themes_create,
    api_themes_install,
)

# ── Updates & Logs (extracted to handlers/updates.py) ──
# NOTE: api_stream passes update_available= to status_snapshot (see updates.py)
from kiro_crew.dashboard.handlers.updates import (  # noqa: E402, F401
    _UPDATE_CHECK_INTERVAL,
    _do_update_check,
    _log_ring,
    _QueueLogHandler,
    _RingLogHandler,
    _update_info,
    _version_key,
    api_changelog,
    api_log_level,
    api_log_level_get,
    api_logs,
    api_stream,
    api_update_apply,
    api_update_auto,
    api_update_cancel,
    api_update_check,
    api_update_simulate,
    get_update_info,
    install_log_ring_handler,
)
from kiro_crew.dashboard.handlers.usage import (  # noqa: E402, F401
    api_kiro_usage,
    api_usage,
)

# ── Themes: validation/parsing core (extracted to theme_validate.py) ──
from kiro_crew.dashboard.theme_validate import (  # noqa: E402, F401
    _CSS_VALUE_ALLOWED_RE,
    _THEME_CSS_VARS_SET,
    _sanitize_css_value,
    _slugify_theme_name,
    _strip_to_allowed_vars,
    _validate_theme_data,
)

# ── Prompts & Skills (extracted to handlers/prompts.py) ──


_PROMPT_CACHE_TTL = 5.0  # seconds
_prompt_cache: list[dict[str, Any]] | None = None
_prompt_cache_ts: float = 0


def _list_aim_prompts() -> list[dict[str, Any]]:
    """Discover agent SOPs from edition-contributed prompt roots and user prompts.

    Edition SOP roots come from ``PromptSourceProvider.prompt_source_roots()`` (CPP
    seam; public Default ``[]``), read fail-closed through ``safe_context_call``.
    Each root is walked generically (``rglob('*.sop.md')``) — no ``~/.aim``
    package layout or eventId resolution — and every SOP is emitted with
    ``source: "package"``. User-authored prompts under ``~/.kiro/prompts`` are
    still discovered (``source: "global"``/``"local"``).
    """
    global _prompt_cache, _prompt_cache_ts  # noqa: PLW0603
    now = time.monotonic()
    if _prompt_cache is not None and now - _prompt_cache_ts < _PROMPT_CACHE_TTL:
        return [dict(p) for p in _prompt_cache]

    result: list[dict[str, Any]] = []

    # Edition-contributed prompt/SOP roots (CPP seam). Deferred import (sel.py
    # pattern) so this package never imports the platform package at module load.
    from kiro_crew.platform.context import current_context, safe_context_call

    roots: list[Path] = safe_context_call(
        lambda: list(current_context().prompt_sources.prompt_source_roots()),
        fallback_factory=list,
        log_message="prompt_source_roots lookup failed; using none",
    )
    for root in roots:
        root = Path(root)
        try:
            if not root.is_dir():
                continue
            sop_files = sorted(root.rglob("*.sop.md"))
        except OSError:
            logger.debug("Skipping unreadable prompt root: %s", root)
            continue
        for sop_file in sop_files:
            try:
                resolved = str(sop_file.resolve())
            except OSError:
                continue
            if is_sensitive_path(resolved):
                logger.debug("Skipping sensitive path: %s", sop_file)
                continue
            name = sop_file.stem.removesuffix(".sop")
            result.append({
                "name": name,
                "fullName": f"agent-sop:{name}",
                "description": _extract_sop_description(sop_file),
                "path": resolved,
                "package": root.name,
                "source": "package",
            })

    # Also scan ~/.kiro/prompts/ for user-created prompts
    home = Path.home()
    prompt_dirs: list[tuple[Path, str]] = [(home / ".kiro" / "prompts", "global")]
    from kiro_crew.agent import _project_dir

    proj = _project_dir()
    if proj:
        prompt_dirs.append((proj / ".kiro" / "prompts", "local"))
    for prompts_dir, src in prompt_dirs:
        if not prompts_dir.is_dir():
            continue
        for f in sorted(prompts_dir.glob("*.md")):
            result.append(
                {
                    "name": f.stem,
                    "fullName": f.stem,
                    "description": _extract_sop_description(f),
                    "path": str(f),
                    "package": "",
                    "source": src,
                }
            )
    _prompt_cache = result
    _prompt_cache_ts = now
    return [dict(p) for p in result]


# Computer use — the Settings config pair (browser, cookie-authed) plus the two
# loopback legs: ``invoke`` (the ``kirocrew-computer`` MCP shim's forward) and
# ``frame`` (the live-view PiP mirror of an already-captured screenshot).
from kiro_crew.dashboard.handlers.computer_use import (  # noqa: E402, F401
    api_computer_use_config_get,
    api_computer_use_config_save,
    api_computer_use_frame,
    api_computer_use_invoke,
)

# ── Core (extracted to handlers/core.py) ──
from kiro_crew.dashboard.handlers.core import (  # noqa: E402, F401
    _DIST_DIR,
    _STATIC_DIR,
    _build_stt_install_script,
    _find_suitable_python,
    _is_al2023,
    _stt_prereq_commands,
    api_app_token,
    api_branding,
    api_health,
    api_kirocrew_config,
    api_kirocrew_config_patch,
    api_live,
    api_logout,
    api_ready,
    api_security_posture,
    api_security_stats,
    api_sel_events,
    api_sel_verify,
    api_session_agent_result,
    api_session_agent_stream,
    api_session_agents_list,
    api_shutdown,
    api_stt_config,
    api_stt_install,
    api_stt_transcribe,
    api_theme_boot,
    api_theme_config,
    api_token_local,
    index,
    logo,
    pwa_file,
)
from kiro_crew.dashboard.handlers.notifications_push import (  # noqa: E402, F401
    api_push_notification,
)
from kiro_crew.dashboard.handlers.onboarding_import import (  # noqa: E402, F401
    api_onboarding_import_apply,
    api_onboarding_import_scan,
    api_onboarding_import_state,
)
from kiro_crew.dashboard.handlers.optimizer import (  # noqa: E402, F401
    handle_optimize,
)

# ── Portability (export/import as zip) ──
from kiro_crew.dashboard.handlers.portability import (  # noqa: E402, F401
    api_portability_export,
    api_portability_import,
    api_portability_preview,
)
from kiro_crew.dashboard.handlers.security import (  # noqa: E402, F401
    api_denied_command_builtin_toggle,
    api_denied_command_user_add,
    api_denied_command_user_delete,
    api_denied_command_user_toggle,
    api_denied_commands_disable_all,
    api_denied_commands_list,
    api_governance_policy,
    api_trusted_app_grant,
    api_trusted_app_revoke,
    api_trusted_apps_allow_all,
    api_trusted_apps_list,
)
