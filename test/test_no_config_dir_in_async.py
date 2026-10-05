"""No config_dir() inside async functions.

config_dir() performs start-of-process maintenance (mkdir, breadcrumb refresh,
ungated-archive sweep with shutil.rmtree) on every call. Calling it from an
async function runs that maintenance on the event loop. The fix is to use
data_home() which returns the cached path without maintenance.

This guard enforces that no async function in the listed files calls
config_dir() directly, so the fix cannot silently regress.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

# Files the AST guard checks for config_dir() calls inside async functions.
_ASYNC_CHECKED_FILES = [
    "dashboard/handlers/files.py",
    "dashboard/file_api/browse.py",
    "dashboard/file_api/dashboard_config.py",
    "dashboard/file_api/git_panel.py",
    "dashboard/file_api/office_preview.py",
    "dashboard/file_api/path_complete.py",
    "dashboard/file_api/project_dirs.py",
    "dashboard/file_api/project_tree.py",
    "dashboard/file_api/search.py",
    "dashboard/file_api/sheet.py",
    "dashboard/file_api/transfer.py",
    "dashboard/file_api/uploads.py",
    "dashboard/file_api/workspaces.py",
    "dashboard/chat_runner.py",
    "dashboard/chat_turn/acp_recovery.py",
    "dashboard/chat_turn/directives.py",
    "dashboard/chat_turn/mcp_session.py",
    "dashboard/chat_turn/model_fallback.py",
    "dashboard/chat_turn/prompt_assembly.py",
    "dashboard/chat_turn/recovery.py",
    "dashboard/chat_turn/tool_approval.py",
    "dashboard/chat_turn/turn_context.py",
    "dashboard/chat_turn/turn_marker.py",
    "dashboard/handlers/knowledge.py",
    "dashboard/handlers/messaging.py",
    "dashboard/messaging_api/channel_delivery.py",
    "dashboard/messaging_api/discord_settings.py",
    "dashboard/messaging_api/feishu_settings.py",
    "dashboard/messaging_api/imessage_settings.py",
    "dashboard/messaging_api/notifications.py",
    "dashboard/messaging_api/proactive_send.py",
    "dashboard/messaging_api/run_control.py",
    "dashboard/messaging_api/run_views.py",
    "dashboard/messaging_api/slack_settings.py",
    "dashboard/messaging_api/spawn.py",
    "dashboard/messaging_api/teams_settings.py",
    "dashboard/messaging_api/telegram_settings.py",
    "dashboard/messaging_api/webex_settings.py",
    "dashboard/messaging_api/wecom_settings.py",
    "dashboard/server.py",
    "dashboard/server_runtime/app_platform.py",
    "dashboard/server_runtime/config_watch.py",
    "dashboard/server_runtime/crewmate_prune.py",
    "dashboard/server_runtime/diagnostics.py",
    "dashboard/server_runtime/heartbeat.py",
    "dashboard/server_runtime/listener.py",
    "dashboard/server_runtime/listener_claims.py",
    "dashboard/server_runtime/maintenance.py",
    "dashboard/server_runtime/mcp_routes.py",
    "dashboard/server_runtime/middleware_chain.py",
    "dashboard/server_runtime/owner_notices.py",
    "dashboard/server_runtime/prevent_sleep.py",
    "dashboard/server_runtime/safety_grants.py",
    "dashboard/server_runtime/security_headers.py",
    "dashboard/server_runtime/security_middleware.py",
    "dashboard/server_runtime/service_hooks.py",
    "dashboard/server_runtime/session_restore.py",
    "dashboard/server_runtime/static_assets.py",
    "dashboard/server_runtime/stt_hooks.py",
    "dashboard/server_runtime/tunnel.py",
    "dashboard/server_runtime/workflow_startup.py",
    "slack/gateway.py",
    "slack/gateway_runtime/admission.py",
    "slack/gateway_runtime/channel_lifecycle.py",
    "slack/gateway_runtime/cron_dispatch.py",
    "slack/gateway_runtime/delivery.py",
    "slack/gateway_runtime/mcp_broker.py",
    "slack/gateway_runtime/memory_lifecycle.py",
    "slack/interactions.py",
    "weixin/gateway.py",
    "cli_chat.py",
]


class TestNoConfigDirInAsync:
    """config_dir() must not be called inside async functions."""

    def test_update_layout_channel_helpers_never_maintain(self) -> None:
        """The channel read/write must use ``data_home()``, not ``config_dir()``.

        Neither helper is an ``async def``, so the AST walk above cannot see them
        — but both are reached FROM async handlers: ``release_channel()`` from the
        update check and ``set_release_channel()`` from ``POST
        /api/update/channel``. ``config_dir()`` is resolve-and-maintain (breadcrumb
        refresh + a leftover-archive sweep that can ``shutil.rmtree``), so using it
        there would run a destructive sweep on the event loop, reached through an
        indirect call chain.
        """
        tree = ast.parse((SRC / "platform" / "update_layout.py").read_text(encoding="utf-8"))
        # AST, not a substring scan: the module's docstrings NAME config_dir to
        # explain why it is the wrong helper here, and a text match would flag
        # exactly the comment that documents the fix.
        called = {
            getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        imported = {
            alias.name
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom)
            for alias in n.names
        }
        assert "config_dir" not in called and "config_dir" not in imported, (
            "update_layout.py must resolve the data home with data_home(); "
            "config_dir() re-runs start-of-process maintenance on every call and "
            "these helpers are reached from async request handlers (#1057)"
        )
        assert "data_home" in called, "the channel helpers must resolve a path at all"

        """Every async call site must use data_home() instead of config_dir()."""
        offenders: list[str] = []
        for fname in _ASYNC_CHECKED_FILES:
            fp = SRC / fname
            if not fp.exists():
                continue
            tree = ast.parse(fp.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        name = getattr(sub.func, "id", None) or getattr(
                            sub.func, "attr", None
                        )
                        if name == "config_dir":
                            offenders.append(
                                f"{fname}:{sub.lineno} in async {node.name}()"
                            )
        assert not offenders, (
            "config_dir() called inside async function (issue #1057). "
            "Use data_home() instead:\n  " + "\n  ".join(offenders)
        )
