"""The MCP tool route table both gateway entrypoints mount.

With the deferred binders that register an optional subsystem's route without importing
its module on the gateway boot path.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        api_artifact_asset,
        api_artifact_comments,
        api_artifact_delete,
        api_artifact_delete_comment,
        api_artifact_detail,
        api_artifact_edit_comment,
        api_artifact_events,
        api_artifact_folder_create,
        api_artifact_folder_delete,
        api_artifact_folder_update,
        api_artifact_folders,
        api_artifact_mark_review,
        api_artifact_materialize,
        api_artifact_overwrite_remote,
        api_artifact_post_comment,
        api_artifact_publish,
        api_artifact_publish_providers,
        api_artifact_pull_latest,
        api_artifact_record_event,
        api_artifact_refresh_sharing,
        api_artifact_relocate,
        api_artifact_reopen_comment,
        api_artifact_reply_comment,
        api_artifact_reprobe_notice,
        api_artifact_resolve_comment,
        api_artifact_session_docs,
        api_artifact_set_folder,
        api_artifact_set_pinned,
        api_artifact_settle_blank,
        api_artifact_unpublish,
        api_artifact_update,
        api_artifact_update_sharing,
        api_artifact_upstream_status,
        api_artifact_version_detail,
        api_artifact_versions,
        api_artifacts_create,
        api_artifacts_list,
        api_remote_artifact_comments,
        api_remote_artifact_delete_comment,
        api_remote_artifact_get,
        api_remote_artifact_mark_review,
        api_remote_artifact_post_comment,
        api_remote_artifact_reply_comment,
        api_remote_artifacts_browse,
        api_remote_artifacts_clone,
        api_remote_artifacts_fork,
        handlers,
        logger,
        setup_spawn_resume_routes,
    )


def _deferred(module_name: str, handler_name: str) -> Callable:
    """Bind a route without importing its handler module at gateway boot.

    The boot-path rule forbids an eager import of an OPTIONAL subsystem inside
    ``_register_mcp_routes``: it runs on every gateway launch before the socket
    binds, so an operator who never enables the feature still pays to load it, and
    for a feature-flagged subsystem the import precedes its own gate. Route
    registration at boot is fine -- only the import moves to first request.

    Both original callers wanted exactly this and differed only in which module they
    named, so the module is a parameter rather than a second copy of the closure:

    * ``session_control`` -- feature-flagged (``agent.session_control``), with the
      enabled check inside the handler.
    * ``agent_panel`` -- the crew webview store, whose MCP server ships gated off
      (``opt_in``) and which most installs never publish to.
    * ``mcp_apps`` -- feature-flagged (``mcp_gateway.apps_enabled``); its module
      scope imports the gateway backend, which must never load on dashboard boot.

    ``module_name`` is a submodule of ``kiro_crew.dashboard.handlers``, not a
    dotted path, so this cannot be pointed at an arbitrary module.
    """

    async def _route(request: web.Request) -> web.StreamResponse:
        module = import_module(f"kiro_crew.dashboard.handlers.{module_name}")
        handler = getattr(module, handler_name)
        return await handler(request)

    _route.__name__ = handler_name
    return _route


def _deferred_work_ledger(handler_name: str) -> Callable:
    """Bind a work-ledger route without importing the subsystem at boot.

    Same shape and same reason as :func:`_deferred_session_control`: the four
    handlers belong to ``kirocrew-work``, an opt-in MCP server, so they are an
    optional subsystem and a module-level import would be an eager one. Route
    registration itself is allowed at boot; only the import moves to first request,
    so a session that is neither a conductor nor a worker never pays for loading it.
    """

    async def _route(request: web.Request) -> web.StreamResponse:
        from kiro_crew.dashboard.handlers import work_ledger

        handler = getattr(work_ledger, handler_name)
        return await handler(request)

    _route.__name__ = handler_name
    return _route


def _register_mcp_routes(app: web.Application) -> None:
    """Register API routes used by MCP tools (spawn, lessons, crons, etc.)."""
    app.router.add_post("/api/spawn", handlers.api_spawn)
    app.router.add_post("/api/spawn/lost", handlers.api_spawn_lost)
    app.router.add_post("/api/spawn/mark-collected", handlers.api_spawn_mark_collected)
    # MCP Apps (SEP-1865): embedded app iframe -> gateway tool callback.
    app.router.add_post("/api/mcp-apps/call", _deferred("mcp_apps", "api_mcp_apps_call"))
    app.router.add_post("/api/mcp-apps/message", _deferred("mcp_apps", "api_mcp_apps_message"))
    app.router.add_get("/api/spawn", handlers.api_spawn_list)
    app.router.add_post("/api/spawn/stop-all", handlers.api_spawn_stop_all)
    # Fairness: the resume-hold, lanes and adaptive routes
    # (``handlers/spawn_resume.py``), registered before ``{agent_id}`` so
    # ``/api/spawn/lanes`` and ``/api/spawn/adaptive`` are not read as run ids.
    setup_spawn_resume_routes(app)
    app.router.add_get("/api/spawn/{agent_id}", handlers.api_spawn_status)
    app.router.add_delete("/api/spawn/{agent_id}", handlers.api_spawn_delete)
    app.router.add_post("/api/spawn/{agent_id}/retry", handlers.api_spawn_retry)
    app.router.add_post("/api/spawn/{agent_id}/continue", handlers.api_spawn_continue)
    app.router.add_post("/api/spawn/{agent_id}/steer", handlers.api_spawn_steer)
    app.router.add_post("/api/spawn/{agent_id}/release", handlers.api_spawn_release)
    app.router.add_get("/api/lessons", handlers.api_lessons)
    app.router.add_post("/api/lessons", handlers.api_lessons_create)
    app.router.add_delete("/api/lessons", handlers.api_lessons_delete)
    app.router.add_get("/api/session-ledger", handlers.api_session_ledger_get)
    app.router.add_post("/api/session-ledger/record", handlers.api_session_ledger_record)
    app.router.add_get("/api/work-ledger", _deferred_work_ledger("api_work_ledger_get"))
    app.router.add_post("/api/work-ledger/record", _deferred_work_ledger("api_work_ledger_record"))
    app.router.add_get("/api/work-ledger/brief", _deferred_work_ledger("api_work_brief"))
    app.router.add_post("/api/work-ledger/report", _deferred_work_ledger("api_work_report"))
    app.router.add_post(
        "/api/work-ledger/rebuild", _deferred_work_ledger("api_work_ledger_rebuild")
    )
    # The Crew page's masked read of a conductor's work ledger (RFC Phase 4).
    # Deliberately NOT in ``_STRICT_INTERNAL_API_PATHS``: a browser is its only
    # caller, so it stays on cookie auth — the same split the agent-panel surface
    # draws between its MCP write and its browser read. Rows are masked of
    # ``worker_session_key``; see the module.
    #
    # Spelled "/api/crew-board" and NOT "/api/work-ledger/board" on purpose. That
    # list matches ``path == p or path.startswith(p + "/")`` and already holds
    # "/api/work-ledger" to cover "/record", "/brief" and "/report" — so a path
    # under that prefix would inherit MCP-only auth and 403 every browser call.
    # Keeping it off the prefix means the strict list needs no exception, which is
    # not a mechanism a security matcher should have to grow for a read route.
    app.router.add_get("/api/crew-board", _deferred("work_ledger_board", "api_work_ledger_board"))
    # The action half. Cookie-authed like the read above and for the same reason:
    # its principal is the dashboard owner, who already stops any session from the
    # Stop button. It resolves the worker session key from the store and never
    # returns it, which is what lets the page offer an affordance whose target the
    # masked read deliberately withholds.
    app.router.add_post(
        "/api/crew-board/action",
        _deferred("work_ledger_board", "api_work_ledger_board_action"),
    )
    # The write half of the agent panel surface -- MCP-only, like the ledger
    # above. The READ, "/api/members/{slug}/panel", is registered here too and
    # stays on cookie auth because a browser is its only caller.
    #
    # Registered route-by-route through the deferred binder rather than by
    # calling the module's own `register_agent_panel_routes`: that call would
    # import the module at boot, which is what the boot-path rule forbids for an
    # optional subsystem. The paths are duplicated from that function, and
    # `test_agent_panel_routes` pins both spellings against each other.
    app.router.add_get(
        "/api/agent-panel/templates", _deferred("agent_panel", "api_agent_panel_templates")
    )
    app.router.add_post(
        "/api/agent-panel/publish", _deferred("agent_panel", "api_agent_panel_publish")
    )
    # The dynamic dashboard's agent surface. Under the same prefix, which is what
    # gives it the same auth: `_STRICT_INTERNAL_API_PATHS` lists `/api/agent-panel`
    # and the middleware matches by prefix, so these are MCP-only without a second
    # entry there.
    app.router.add_get(
        "/api/agent-panel/dashboard/fields", _deferred("agent_panel", "api_dashboard_fields")
    )
    app.router.add_post(
        "/api/agent-panel/dashboard/write", _deferred("agent_panel", "api_dashboard_write")
    )
    app.router.add_get(
        "/api/agent-panel/dashboard/templates", _deferred("agent_panel", "api_dashboard_templates")
    )
    app.router.add_post(
        "/api/agent-panel/dashboard/preview", _deferred("agent_panel", "api_dashboard_preview")
    )
    app.router.add_post(
        "/api/agent-panel/dashboard/apply", _deferred("agent_panel", "api_dashboard_apply")
    )
    app.router.add_post(
        "/api/agent-panel/dashboard/rollback", _deferred("agent_panel", "api_dashboard_rollback")
    )
    app.router.add_get("/api/members/{slug}/panel", _deferred("agent_panel", "api_member_panel"))
    # The crewmate's dynamic dashboard. Registered through the deferred binder for the
    # reason the panel's routes are: calling the module's own
    # `register_member_dashboard_routes` would import it at boot, which the boot-path
    # rule forbids for an optional subsystem. The path is duplicated from that
    # function, and `test_member_dashboard_routes` pins both spellings against each
    # other.
    app.router.add_get(
        "/api/members/{slug}/dashboard", _deferred("member_dashboard", "api_member_dashboard")
    )
    app.router.add_get("/api/crons", handlers.api_crons)
    app.router.add_post("/api/crons", handlers.api_crons_create)
    app.router.add_delete("/api/crons", handlers.api_cron_batch_delete)
    app.router.add_get("/api/crons/history", handlers.api_cron_history_all)
    app.router.add_post("/api/crons/tools", handlers.api_cron_tools)
    app.router.add_delete("/api/crons/{job_id}", handlers.api_cron_delete)
    app.router.add_patch("/api/crons/{job_id}", handlers.api_cron_update)
    # Operator-only vault-secret grants. The "/api/crons" prefix above makes
    # this reachable with X-Internal-Secret, so the HANDLER refuses proven
    # internal-secret callers (request["internal_auth"]) — machines request,
    # humans grant. See the handler docstring.
    app.router.add_put("/api/crons/{job_id}/secrets", handlers.api_cron_secret_grant)
    app.router.add_post("/api/crons/{job_id}/enable", handlers.api_cron_enable)
    app.router.add_post("/api/crons/{job_id}/run", handlers.api_cron_run)
    app.router.add_post("/api/crons/{job_id}/cancel", handlers.api_cron_cancel)
    app.router.add_post("/api/crons/{job_id}/to-chat", handlers.api_cron_to_chat)
    app.router.add_post("/api/crons/{job_id}/ack", handlers.api_cron_ack)
    app.router.add_get("/api/crons/{job_id}/history", handlers.api_cron_history)
    app.router.add_get("/api/crons/{job_id}/history/{run_id}", handlers.api_cron_history_detail)
    app.router.add_get("/api/crons/{job_id}/script", handlers.api_cron_script_source)
    app.router.add_get("/api/cron-folders", handlers.api_cron_folders)
    app.router.add_post("/api/cron-folders", handlers.api_cron_folders_create)
    app.router.add_patch("/api/cron-folders/{folder_id}", handlers.api_cron_folders_update)
    app.router.add_delete("/api/cron-folders/{folder_id}", handlers.api_cron_folders_delete)
    app.router.add_get("/api/taskrunner", handlers.api_taskrunner_status)
    app.router.add_post("/api/taskrunner", handlers.api_taskrunner_start)
    app.router.add_post("/api/taskrunner/cancel", handlers.api_taskrunner_cancel)
    app.router.add_post("/api/send-message", handlers.api_send_message)
    app.router.add_post("/api/delete-message", handlers.api_delete_message)
    app.router.add_post("/api/update-message", handlers.api_update_message)
    # send_notification MCP tool (RFC notification bus Phase 5) — registered
    # here (not the dashboard-only block) so headless --slack-only mode
    # serves it too; it is on _STRICT_INTERNAL_API_PATHS like send-message.
    app.router.add_post("/api/notifications/agent", handlers.api_notification_agent_push)
    # Session control. Registered here so the headless --slack-only server
    # serves the same MCP surface as the dashboard; all three are on
    # _STRICT_INTERNAL_API_PATHS, which test_session_control_routes_are_strict
    # pins by deriving the route set from the router rather than a hand-copied list.
    app.router.add_post(
        "/api/session-control/create", _deferred("session_control", "api_session_control_create")
    )
    app.router.add_post(
        "/api/session-control/fork", _deferred("session_control", "api_session_control_fork")
    )
    app.router.add_post(
        "/api/session-control/stop", _deferred("session_control", "api_session_control_stop")
    )
    app.router.add_post(
        "/api/session-control/end-wait",
        _deferred("session_control", "api_session_control_end_wait"),
    )
    app.router.add_post(
        "/api/session-control/retry", _deferred("session_control", "api_session_control_retry")
    )
    app.router.add_post(
        "/api/session-control/set-model",
        _deferred("session_control", "api_session_control_set_model"),
    )
    app.router.add_post(
        "/api/session-control/reload",
        _deferred("session_control", "api_session_control_reload"),
    )
    app.router.add_post(
        "/api/session-control/close", _deferred("session_control", "api_session_control_close")
    )
    app.router.add_post(
        "/api/session-control/revive", _deferred("session_control", "api_session_control_revive")
    )
    app.router.add_post(
        "/api/session-control/send", _deferred("session_control", "api_session_control_send")
    )
    app.router.add_post(
        "/api/session-control/broadcast",
        _deferred("session_control", "api_session_control_broadcast"),
    )
    app.router.add_get(
        "/api/session-control/status",
        _deferred("session_control", "api_session_control_status"),
    )
    app.router.add_post(
        "/api/session-control/adopt", _deferred("session_control", "api_session_control_adopt")
    )
    app.router.add_post(
        "/api/session-control/release",
        _deferred("session_control", "api_session_control_release"),
    )
    app.router.add_get(
        "/api/session-control/read", _deferred("session_control", "api_session_control_read")
    )
    app.router.add_get(
        "/api/session-control/summary",
        _deferred("session_control", "api_session_control_summary"),
    )
    app.router.add_get("/api/browser/install", handlers.api_browser_install_get)
    app.router.add_put("/api/browser/token", handlers.api_browser_token_put)
    app.router.add_post("/api/browser/install", handlers.api_browser_install_start)
    app.router.add_post("/api/browser/engine", handlers.api_browser_engine_install)
    app.router.add_get("/api/browser/view", handlers.api_browser_view_get)
    app.router.add_post("/api/browser/view/start", handlers.api_browser_view_start)
    # Same-origin relay for the CLI browser view: the panel frames this path
    # instead of the raw loopback URL, so the live view is reachable wherever
    # the dashboard is (SSH forward, tunnel) with no second forwarded port.
    # HTTP and WebSocket both. Authenticated by the per-instance capability
    # token embedded in the path (NOT the session cookie: the panel frames it
    # in an opaque-origin sandbox that sends none) — the prefix is on
    # token_auth's bypass list and the handler enforces the token itself. See
    # handlers/browser_view_relay.py for the rewrites and the full posture.
    app.router.add_get("/browser-view", handlers.api_browser_view_relay)
    app.router.add_get("/browser-view/{tail:.*}", handlers.api_browser_view_relay)
    # The Browser panel's address bar on the non-native transport: opens an
    # owner-typed URL in the gateway host's Playwright CLI browser and shows it
    # through the view above. Owner-only (cookie/token) and deliberately NOT on
    # any internal-path list -- the handler refuses internal-secret callers too,
    # because agent browsing must keep going through the shell approval ladder.
    app.router.add_post("/api/browser/open", handlers.api_browser_open)
    # Native browser command channel (agent->Electron). Loopback + internal-secret
    # only; see the _STRICT_INTERNAL_API_PATHS entries and each handler's re-assert.
    app.router.add_post("/api/browser/command", handlers.api_browser_command)
    app.router.add_post("/api/browser/command-drain", handlers.api_browser_command_drain)
    app.router.add_post("/api/browser/command-result", handlers.api_browser_command_result)
    # Distinctive boot marker: this line exists ONLY in the command-bus-gateway
    # build, so its presence in gateway.log proves this worktree's backend is the
    # one actually running (vs a stale / frozen bundled backend).
    logger.debug("browser-cmdbus gateway: /api/browser/command{,-drain,-result} registered")
    # Computer use: the thin ``kirocrew-computer`` stdio shim's only call. Lives
    # HERE (rather than in the dashboard-only block, where the browser-called
    # config pair sits) so the headless ``--slack-only`` server exposes it too —
    # kiro-cli spawns the shim on both entrypoints. It is in
    # ``_STRICT_INTERNAL_API_PATHS``: loopback + ``X-Internal-Secret`` only, no
    # cookie fall-through, because no browser ever calls it.
    app.router.add_post("/api/computer-use/invoke", handlers.api_computer_use_invoke)
    # The live-view (PiP) frame ingress. Registered alongside ``invoke`` (not in
    # the dashboard-only block) because the capture that produces a frame runs on
    # BOTH entrypoints — a ``--slack-only`` gateway drives the desktop too, and its
    # dashboard-less state simply has no owner sockets to deliver to.
    app.router.add_post("/api/computer-use/frame", handlers.api_computer_use_frame)
    app.router.add_post("/api/session-keepalive", handlers.api_session_keepalive)
    app.router.add_post("/api/session-directive", handlers.api_session_directive)
    app.router.add_get("/api/session-tool-policy", handlers.api_session_tool_policy)
    app.router.add_post("/api/slack-profile", handlers.api_slack_profile)
    app.router.add_get("/api/notifications", handlers.api_notifications)
    app.router.add_post("/api/notifications/push", handlers.api_push_notification)
    app.router.add_post("/api/notifications/clear", handlers.api_notifications_clear)

    # Auto-nudge (feature-flagged — returns 503 when KIROCREW_AUTONUDGE unset)
    from kiro_crew.dashboard.handlers.autonudge import (
        api_autonudge_delete,
        api_autonudge_fire,
        api_autonudge_get,
        api_autonudge_list,
        api_autonudge_start,
        api_autonudge_update,
        api_monitor_clear,
        api_monitor_create,
        api_monitor_restart,
        api_monitor_slot_get,
        api_monitor_stop,
        api_monitor_update,
        api_monitors_list,
        api_session_monitor_get,
    )

    app.router.add_get("/api/autonudge", api_autonudge_list)
    app.router.add_get("/api/autonudge/session-monitor", api_session_monitor_get)
    app.router.add_post("/api/autonudge", api_autonudge_start)
    app.router.add_get("/api/autonudge/slot/{slot_key}", api_autonudge_get)
    app.router.add_patch("/api/autonudge/{loop_id}", api_autonudge_update)
    app.router.add_delete("/api/autonudge/{loop_id}", api_autonudge_delete)
    app.router.add_post("/api/autonudge/{loop_id}/fire", api_autonudge_fire)
    app.router.add_get("/api/monitors", api_monitors_list)
    app.router.add_post("/api/monitors", api_monitor_create)
    app.router.add_get("/api/monitors/slot/{slot_key}", api_monitor_slot_get)
    app.router.add_patch("/api/monitors/{monitor_id}", api_monitor_update)
    app.router.add_post("/api/monitors/{monitor_id}/stop", api_monitor_stop)
    app.router.add_post("/api/monitors/{monitor_id}/clear", api_monitor_clear)
    app.router.add_post("/api/monitors/{monitor_id}/restart", api_monitor_restart)

    # Agent questions. The MCP ask_question tool opens a BLOCKING card through
    # /api/agent-ask/* (strict internal paths, the caller's attested session
    # only); with no attested identity or no attached dashboard it shows no card
    # (see mcp_tools.control.ask_question). The UI reads /pending to rehydrate cards
    # after a reload and answers or dismisses them through the routes below, and
    # POST /api/ask-question still opens a blocking wait for any caller that
    # uses it — so none of these may be wrapped in short-timeout middleware.
    from kiro_crew.dashboard.handlers.ask_question import (
        api_agent_ask_open,
        api_agent_ask_wait,
        api_agent_ask_withdraw,
        api_ask_question,
        api_ask_question_answer,
        api_ask_question_dismiss,
        api_ask_question_pending,
    )

    app.router.add_post("/api/ask-question", api_ask_question)
    # Registered before the {ask_id} route so the literal path is not captured
    # as an ask_id.
    app.router.add_get("/api/ask-question/pending", api_ask_question_pending)
    app.router.add_post("/api/ask-question/dismiss", api_ask_question_dismiss)
    app.router.add_post("/api/ask-question/{ask_id}/answer", api_ask_question_answer)
    app.router.add_post("/api/agent-ask/open", api_agent_ask_open)
    app.router.add_post("/api/agent-ask/{ask_id}/wait", api_agent_ask_wait)
    app.router.add_post("/api/agent-ask/{ask_id}/withdraw", api_agent_ask_withdraw)

    # Artifacts — persistent, versioned LLM-generated UI
    app.router.add_get("/api/artifacts", api_artifacts_list)

    # Dynamic Workflows (M6) — author, run, monitor, cancel, rerun
    from kiro_crew.dashboard.handlers.workflows import (
        api_workflow_author,
        api_workflow_definition_get,
        api_workflow_definition_run,
        api_workflow_definition_update,
        api_workflow_definitions,
        api_workflow_definitions_create,
        api_workflow_run,
        api_workflow_run_cancel,
        api_workflow_run_get,
        api_workflow_run_intent,
        api_workflow_run_promote,
        api_workflow_run_rerun,
        api_workflow_runs,
    )

    app.router.add_post("/api/workflows/author", api_workflow_author)
    app.router.add_post("/api/workflows/run", api_workflow_run)
    app.router.add_post("/api/workflows/run_intent", api_workflow_run_intent)
    app.router.add_get("/api/workflows/definitions", api_workflow_definitions)
    app.router.add_post("/api/workflows/definitions", api_workflow_definitions_create)
    app.router.add_post(
        "/api/workflows/definitions/{workflow_ref}/run", api_workflow_definition_run
    )
    app.router.add_get("/api/workflows/definitions/{workflow_ref}", api_workflow_definition_get)
    app.router.add_patch(
        "/api/workflows/definitions/{workflow_ref}", api_workflow_definition_update
    )
    app.router.add_get("/api/workflows/runs", api_workflow_runs)
    app.router.add_get("/api/workflows/runs/{run_id}", api_workflow_run_get)
    app.router.add_post("/api/workflows/runs/{run_id}/promote", api_workflow_run_promote)
    app.router.add_post("/api/workflows/runs/{run_id}/cancel", api_workflow_run_cancel)
    app.router.add_post("/api/workflows/runs/{run_id}/rerun", api_workflow_run_rerun)

    # Artifacts — persistent, versioned LLM-generated UI
    app.router.add_get("/api/artifacts", api_artifacts_list)
    app.router.add_post("/api/artifacts", api_artifacts_create)
    # Static sub-paths MUST precede the ``/{slug}`` dynamic route below, else
    # "session-docs" / "materialize" / "publish-providers" would be captured as
    # a slug (aiohttp matches routes in registration order).
    from kiro_crew.dashboard.handlers.webapp_preview import register_webapp_preview_routes

    register_webapp_preview_routes(app)
    # The document channel artifact and widget frames load from — see
    # handlers/sandbox_doc.py for why a blob: URL was not survivable.
    from kiro_crew.dashboard.handlers.sandbox_doc import register_sandbox_doc_routes

    register_sandbox_doc_routes(app)
    app.router.add_get("/api/artifacts/session-docs", api_artifact_session_docs)
    app.router.add_post("/api/artifacts/materialize", api_artifact_materialize)
    app.router.add_get("/api/artifacts/publish-providers", api_artifact_publish_providers)
    app.router.add_get("/api/artifacts/{slug}", api_artifact_detail)
    app.router.add_get("/api/artifacts/{slug}/asset", api_artifact_asset)
    app.router.add_patch("/api/artifacts/{slug}", api_artifact_update)
    app.router.add_delete("/api/artifacts/{slug}", api_artifact_delete)
    app.router.add_post("/api/artifacts/{slug}/settle", api_artifact_settle_blank)
    app.router.add_get("/api/artifacts/{slug}/versions", api_artifact_versions)
    app.router.add_get("/api/artifacts/{slug}/versions/{version}", api_artifact_version_detail)
    app.router.add_get("/api/artifacts/{slug}/events", api_artifact_events)
    app.router.add_post("/api/artifacts/{slug}/events", api_artifact_record_event)
    # Publishing / sharing
    app.router.add_post("/api/artifacts/{slug}/publish", api_artifact_publish)
    app.router.add_delete("/api/artifacts/{slug}/publish", api_artifact_unpublish)
    app.router.add_post("/api/artifacts/{slug}/publish/refresh", api_artifact_refresh_sharing)
    app.router.add_post("/api/artifacts/{slug}/publish/reprobe-notice", api_artifact_reprobe_notice)
    app.router.add_patch("/api/artifacts/{slug}/sharing", api_artifact_update_sharing)
    app.router.add_patch("/api/artifacts/{slug}/relocate", api_artifact_relocate)
    # Upstream sync (fork/publication lineage) — pull / status / overwrite
    app.router.add_post("/api/artifacts/{slug}/pull-latest", api_artifact_pull_latest)
    app.router.add_get("/api/artifacts/{slug}/upstream-status", api_artifact_upstream_status)
    app.router.add_post("/api/artifacts/{slug}/overwrite-remote", api_artifact_overwrite_remote)
    # Remote artifacts — provider-routed browse / clone / fork. Inert in the
    # public edition (empty provider registry -> 404); a companion registers
    # providers via the CPP publish seam.
    app.router.add_get("/api/remote-artifacts/{provider}/browse", api_remote_artifacts_browse)
    # external_id travels in the JSON body, NOT a path segment: provider-native
    # ids can contain "/" (e.g. nested provider repo paths), which a single
    # {external_id} segment cannot carry — the router decodes a percent-encoded
    # slash before matching and 404s. Body transport is slash-safe.
    app.router.add_post("/api/remote-artifacts/{provider}/clone", api_remote_artifacts_clone)
    app.router.add_post("/api/remote-artifacts/{provider}/fork", api_remote_artifacts_fork)
    # Single remote artifact fetch (content source for the remote-detail view).
    # external_id is a path segment here — browser-only, and the ids that reach
    # this route come from the browse listing (no embedded slash). The more
    # specific {external_id}/comments* routes below still match first.
    app.router.add_get("/api/remote-artifacts/{provider}/{external_id}", api_remote_artifact_get)
    # Per-remote-artifact comments (remote-detail view of a provider-hosted
    # artifact the user has no local copy of). external_id here IS a path segment
    # — these are browser-only, comment ops target a single already-resolved
    # artifact, and the provider ids that reach this route are the browse/detail
    # listing's own ids (no embedded slash). Empty registry -> get_provider raises
    # -> the handlers return a clear error, never a 500.
    app.router.add_get(
        "/api/remote-artifacts/{provider}/{external_id}/comments",
        api_remote_artifact_comments,
    )
    app.router.add_post(
        "/api/remote-artifacts/{provider}/{external_id}/comments",
        api_remote_artifact_post_comment,
    )
    app.router.add_post(
        "/api/remote-artifacts/{provider}/{external_id}/comments/{comment_id}/reply",
        api_remote_artifact_reply_comment,
    )
    app.router.add_post(
        "/api/remote-artifacts/{provider}/{external_id}/comments/{comment_id}/review",
        api_remote_artifact_mark_review,
    )
    app.router.add_delete(
        "/api/remote-artifacts/{provider}/{external_id}/comments/{comment_id}",
        api_remote_artifact_delete_comment,
    )

    # Artifact folders. ``/api/artifact-folders`` (hyphen) never
    # collides with the ``/api/artifacts/{slug}`` dynamic route.
    app.router.add_get("/api/artifact-folders", api_artifact_folders)
    app.router.add_post("/api/artifact-folders", api_artifact_folder_create)
    app.router.add_patch("/api/artifact-folders/{id}", api_artifact_folder_update)
    app.router.add_delete("/api/artifact-folders/{id}", api_artifact_folder_delete)
    app.router.add_patch("/api/artifacts/{slug}/folder", api_artifact_set_folder)
    app.router.add_patch("/api/artifacts/{slug}/pin", api_artifact_set_pinned)
    # Artifact comments (durable local store)
    app.router.add_get("/api/artifacts/{slug}/comments", api_artifact_comments)
    app.router.add_post("/api/artifacts/{slug}/comments", api_artifact_post_comment)
    app.router.add_patch("/api/artifacts/{slug}/comments/{comment_id}", api_artifact_edit_comment)
    app.router.add_post(
        "/api/artifacts/{slug}/comments/{comment_id}/reply", api_artifact_reply_comment
    )
    app.router.add_post(
        "/api/artifacts/{slug}/comments/{comment_id}/review", api_artifact_mark_review
    )
    app.router.add_post(
        "/api/artifacts/{slug}/comments/{comment_id}/resolve", api_artifact_resolve_comment
    )
    app.router.add_post(
        "/api/artifacts/{slug}/comments/{comment_id}/reopen", api_artifact_reopen_comment
    )
    app.router.add_delete(
        "/api/artifacts/{slug}/comments/{comment_id}", api_artifact_delete_comment
    )
