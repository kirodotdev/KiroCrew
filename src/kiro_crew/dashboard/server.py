"""Dashboard aiohttp application factory and startup."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import faulthandler
import logging
import os
import stat
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from aiohttp import web

from kiro_crew import platform_compat, port_resolution
from kiro_crew.apps.backend import start_enabled_app_backends
from kiro_crew.apps.hook_reconcile import init_hook_reconciler, stop_hook_reconciler
from kiro_crew.apps.hooks_integration import (
    init_hooks_system,
    on_gateway_shutdown,
    on_gateway_startup,
)
from kiro_crew.apps.manager import cleanup_migrated_builtin, register_builtin_apps
from kiro_crew.autonudge import get_instance as _autonudge_get
from kiro_crew.autonudge_authz import authorize_and_add_nudge
from kiro_crew.browser_cli import launch as browser_cli_launch
from kiro_crew.browser_cli import snapshots as browser_cli_snapshots
from kiro_crew.browser_cli import token as browser_cli_token
from kiro_crew.browser_cli import view as browser_cli_view
from kiro_crew.channel_transcript_migration import migrate_channel_transcripts
from kiro_crew.config import data_home
from kiro_crew.config.loader import (
    KiroCrewConfig,
    consume_managed_service_launch_environment,
    degraded_config_files,
    load_loop_stall_exit_after,
    refresh_config_meta_stamp,
    refresh_materialized_agents,
    resolve_loop_stall_exit_after,
    tailnet_effective_allowed_logins,
    tailnet_identity_unknown,
)
from kiro_crew.dashboard import (
    cautious_boot,
    channel_slots,
    chat,
    handlers,
    tailnet,
    tailnet_serve,
)
from kiro_crew.dashboard.crash_dump_store import (
    claim_dump_notification,
    dump_age_seconds,
    dump_replay_lines,
    newest_dump_with_stacks,
    open_dump_file,
    rotate_dumps,
    sweep_stale_dumps,
)
from kiro_crew.dashboard.handlers.artifacts import (
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
)
from kiro_crew.dashboard.handlers.feedback import setup_feedback_routes
from kiro_crew.dashboard.handlers.knowledge import setup_knowledge_routes
from kiro_crew.dashboard.handlers.link_meta import setup_link_meta_routes
from kiro_crew.dashboard.handlers.secrets import setup_secrets_routes
from kiro_crew.dashboard.handlers.source_providers import (
    register_status_delta_sink,
    unregister_status_delta_sink,
)
from kiro_crew.dashboard.handlers.weixin_qr import setup_weixin_routes
from kiro_crew.dashboard.handlers.whatsapp_setup import setup_whatsapp_routes
from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog
from kiro_crew.dashboard.origin import (
    AUDIT_CLAIMED_KEY,
    PROBE_PATHS,
    bind_address_for,
    build_allowed_origins,
    check_host,
    check_origin,
    dashboard_socket_path,
    frame_ancestors_value,
    mark_audit_claimed,
    resolve_dashboard_host,
    should_canonicalize_host,
)
from kiro_crew.dashboard.port_reclaim import (
    FOREIGN_HOLDER,
    HEALTHY_PEER,
    RECLAIMED,
    reclaim_stale_gateway_port,
)
from kiro_crew.dashboard.routes import register_all
from kiro_crew.dashboard.slowloris import build_hardened_runner
from kiro_crew.dashboard.state import _DEFAULT_PORT, DashboardState
from kiro_crew.dashboard.token_auth import (
    _cookie_port_from_host,
    _is_spa_shell_request,
    is_csrf_exempt,
    register_app_window_paths,
    token_auth_middleware,
    token_embed_parent_port,
    warm_auth_singletons,
)
from kiro_crew.deploy import _register_core_skills as _register_deploy_skills
from kiro_crew.deploy.handlers import register_routes as _register_deploy_routes
from kiro_crew.executors import subprocess_executor
from kiro_crew.hooks import ScriptHookStore, set_global_hook_store
from kiro_crew.instances import run_marker
from kiro_crew.instances.registry import InstancesRegistry
from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager, TunnelState
from kiro_crew.mcp_gateway.socketsec import chmod_socket_0600
from kiro_crew.metrics.http_metrics import (
    make_route_latency_middleware,
    record_boot_to_ready,
)
from kiro_crew.platform import (
    async_safe_context_call,
    current_context,
    safe_context_call,
)
from kiro_crew.power import SleepInhibitor
from kiro_crew.safety_override import (
    apply_config_duration,
    describe_dropped_grant,
    grant_declared_yolo,
    safety_override,
    take_dropped_grant,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.skill_usage import register_skill_read_observer
from kiro_crew.skills import SkillsLoader, set_pending_consumed_hook, set_pending_staged_hook
from kiro_crew.tunnel.setup import setup_tunnel

if TYPE_CHECKING:
    from kiro_crew.dashboard._types import (  # noqa: F401
        ContextBuilder,
        ConversationLog,
        CronService,
        HistoryConsolidator,
        LessonStore,
        SessionManager,
        SubagentManager,
        TaskRunner,
    )

# aiohttp's static file handler uses its own ``mimetypes.MimeTypes()`` instance
# (``aiohttp.web_fileresponse.CONTENT_TYPES``) which does NOT load the system
# mime.types database.  Font extensions are missing from the built-in Python
# fallback, so aiohttp returns ``application/octet-stream`` for .woff/.woff2/.ttf.
# Register the correct font MIME types into that singleton at import time so ALL
# static routes (including ``/fonts``) serve proper Content-Type headers.
from aiohttp.web_fileresponse import CONTENT_TYPES as _AIOHTTP_CONTENT_TYPES

_AIOHTTP_CONTENT_TYPES.add_type("font/woff", ".woff")
_AIOHTTP_CONTENT_TYPES.add_type("font/woff2", ".woff2")
_AIOHTTP_CONTENT_TYPES.add_type("font/ttf", ".ttf")

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_DIST_DIR = _STATIC_DIR / "dist"

# How often the prevent-sleep poll re-evaluates whether the host should be kept
# awake. It only needs to beat OS idle-sleep timers (minutes), so a coarse
# interval keeps the overhead negligible; a turn shorter than one interval never
# outlasts a sleep timer, so not catching it is harmless.
_PREVENT_SLEEP_POLL_INTERVAL_SECS = 15.0

# How long the speech idle-sweep task waits before importing the recogniser package.
# Its only job is to keep boot clean: the import pulls numpy and the binding, and the
# hook that starts this task runs before either socket binds. Anything past the first
# few seconds of boot works, since the sweep's own interval is a minute.
_STT_SWEEP_BOOT_DELAY_SECS = 30.0


async def _prune_browser_snapshots_loop() -> None:
    """Keep the browser snapshot directory bounded for as long as we run.

    `playwright-cli` writes one snapshot YAML per command and prunes nothing, so
    retention belongs to a long-lived component. It lives here rather than in the
    agent because the agent has no reason to know the policy, and a per-command
    prune would race the CLI daemon writing the next file.

    The first pass is delayed so it never competes with boot work for disk, and the
    interval is coarse because the retention bound is a ceiling, not a deadline.
    """
    await asyncio.sleep(60.0)
    while True:
        try:
            await asyncio.to_thread(browser_cli_snapshots.prune)
        except Exception:
            logger.debug("browser snapshot prune failed", exc_info=True)
        await asyncio.sleep(30 * 60.0)


#: The tailnet publish state is a subprocess round trip (`tailscale serve
#: status`), and the prevent-sleep poll runs every 15s — far too often to spawn a
#: CLI each time. Cached SEPARATELY from the mobile-access card's own reads, which
#: stay live on purpose: a stale awake decision costs at most one window of
#: battery, while a stale card would show the operator the wrong next action.
_TAILNET_AWAKE_TTL_SECS = 60.0

#: ``(monotonic expiry, published)``. Module-level so both server entrypoints
#: share one cache rather than each paying its own subprocess.
_tailnet_awake_cache: tuple[float, bool] = (0.0, False)


async def _tailnet_publish_keeps_awake(port: int) -> bool:
    """Whether serve is currently fronting *port*, TTL-cached. Never raises."""
    global _tailnet_awake_cache
    if not port:
        return False
    now = time.monotonic()
    expiry, cached = _tailnet_awake_cache
    if expiry > now:
        return cached
    try:
        serve = await asyncio.to_thread(tailnet_serve.serve_state, port)
        # ``published is None`` means we could not tell. Treated as NOT published,
        # because the fail-closed direction for this decision is letting the host
        # sleep — an unresolvable probe must not pin a laptop awake indefinitely.
        published = serve.published is True
    except Exception:
        logger.debug("prevent-sleep tailnet probe failed", exc_info=True)
        published = False
    _tailnet_awake_cache = (now + _TAILNET_AWAKE_TTL_SECS, published)
    return published


async def _should_prevent_sleep(state: DashboardState, port: int) -> bool:
    """Whether the host should be kept awake right now.

    Two independent reasons, either sufficient on its own:

    * **A turn is in flight**, and the user opted in via
      ``dashboard.prevent_sleep``. The original reason this poll exists.
    * **The dashboard is published on this machine's tailnet**, and
      ``dashboard.tailscale.keep_awake`` is on. A phone loses the dashboard the
      moment the laptop idles, so publishing is itself the opt-in — an operator
      who put the dashboard on their tailnet asked for it to stay reachable.
      Deliberately NOT also gated on ``dashboard.prevent_sleep``: that switch is
      scoped to in-flight turns, and making someone find it to keep a published
      dashboard alive would be the wrong switch in the wrong place. The escape
      hatch is ``keep_awake``, which turns off the awake half without
      unpublishing.

    Reads config live so either toggle takes effect on the next poll without a
    restart. Fail-closed throughout: any error resolves to "allow sleep", so a
    config or daemon hiccup can never wedge the machine awake.
    """
    try:
        # KiroCrewConfig.load() does a stat and, on a cache miss, a JSON read +
        # schema validation. On a slow home filesystem that is a blocking call,
        # and this runs on the gateway event loop every poll — offload it so a
        # slow read can never stall chat/heartbeat (no-blocking-call-on-event-loop).
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        # Both reads sit INSIDE the guard, and that placement is the actual
        # defence: a config object predating the tailscale section raises on the
        # attribute, and outside the guard that would propagate — a partially
        # formed config wedging a laptop awake, since the poll swallows the error
        # and retries forever. The getattr defaults are belt-and-braces on top.
        tailscale_cfg = getattr(cfg.dashboard, "tailscale", None)
        tailnet_enabled = bool(getattr(tailscale_cfg, "enabled", False))
        tailnet_keep_awake = bool(getattr(tailscale_cfg, "keep_awake", False))
        tailnet_wants_awake = tailnet_enabled and tailnet_keep_awake
        opted_into_turn_wake = bool(getattr(cfg.dashboard, "prevent_sleep", False))
    except Exception:
        logger.debug("prevent-sleep config read failed", exc_info=True)
        return False
    if tailnet_wants_awake and await _tailnet_publish_keeps_awake(port):
        return True
    if not opted_into_turn_wake:
        return False
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return False
    try:
        # In-memory dict scan on the loop thread (no await inside, so no
        # concurrent mutation) — cheap and non-blocking.
        return sessions.any_active_turn()
    except Exception:
        logger.debug("prevent-sleep active-turn check failed", exc_info=True)
        return False


# Strict internal API paths — exact paths that ONLY internal processes
# (mcp-core, doctor, cron) call, never the browser. Access requires loopback
# AND a matching ``X-Internal-Secret`` header; non-loopback is always denied and
# there is no cookie fall-through (see token_auth.token_auth_middleware).
#
# Module-level and shared by BOTH ``start_dashboard`` and ``start_api_server``
# so the two entrypoints can never drift: the ``--slack-only`` headless server
# must gate exactly the same MCP tool routes the dashboard does. A prior drift
# here — headless mounting no token auth at all — was an auth-bypass regression
# of the loopback-bypass fix. Keep this as the single source of truth.
_STRICT_INTERNAL_API_PATHS = frozenset(
    {
        "/api/send-message",
        "/api/delete-message",
        "/api/browser-event",
        "/api/browser/frame",
        "/api/browser/pump-audit",
        # Native browser command channel (agent->Electron). MACHINE endpoints,
        # same trust class as ``/api/browser/frame``: the MCP proxy posts commands
        # and the Electron main process long-polls/returns results, all loopback +
        # internal-secret. No browser calls them, so STRICT (not mixed). Each
        # handler re-asserts loopback because a ``local_only=False`` deployment
        # reclassifies strict paths as mixed.
        "/api/browser/command",
        "/api/browser/command-drain",
        "/api/browser/command-result",
        # Computer use: the ``kirocrew-computer`` stdio shim's forwarding leg.
        # STRICT (not mixed): no browser calls it, and it is the entry point to
        # accessibility reads and input synthesis into the operator's real
        # applications — the one API surface where a cookie fall-through would be
        # a genuinely new attack path rather than a convenience. The Settings pair
        # (``/api/computer-use/config``) is deliberately NOT here: it is browser-
        # called and cookie-authed. Note the prefix-matching in
        # ``token_auth.middleware`` treats ``/api/computer-use/invoke/...`` as
        # strict too, which is correct — nothing else lives under it.
        "/api/computer-use/invoke",
        # Computer use: the live-view (PiP) frame ingress. STRICT for the same
        # reason as ``invoke`` — its body is a frame of the operator's own desktop
        # and its only caller is this gateway's own capture thread, so no browser
        # ever posts to it. The handler re-asserts loopback itself because a
        # ``local_only=False`` deployment reclassifies strict paths as mixed.
        "/api/computer-use/frame",
        "/api/session-keepalive",
        # Session directives: the provider-neutral leg of the directive
        # protocol. STRICT for the same reasons as its sibling above — the
        # only legitimate caller is a Kiro Crew directive tool in an MCP
        # subprocess, and the route's whole point is that the payload arrives
        # somewhere the model's tool result is not trusted. A cookie
        # fall-through would let a browser bearer park a directive against a
        # session it merely has a tab on, bypassing the unix-socket peer check
        # that makes the declared X-Session-Key trustworthy.
        "/api/session-directive",
        # In-app update approval (RFC OQ7 step-up). STRICT: its only legitimate
        # caller is `kirocrew update approve` on the gateway host presenting the
        # trust/-fenced nonce plus X-Local-Secret; no browser ever posts to it —
        # the SPA can only ARM. Keeping it off the cookie fall-through means a
        # dashboard bearer cannot even reach the handler whose refusal is the
        # boundary, and the handler re-asserts host-locality itself because a
        # local_only=False deployment reclassifies strict paths as mixed.
        "/api/update/approve",
        "/api/session-tool-policy",
        # NOTE: "/api/hooks/agent" is deliberately NOT here. It is an inbound
        # webhook for EXTERNAL callers (CI runners, review bots) that hold no
        # dashboard cookie and no gateway IPC secret, so a strict-internal entry
        # denies every real caller with 403 before the handler's own bearer check
        # can run, leaving the webhook token layer unreachable. It lives in
        # token_auth._BYPASS_EXACT_METHODS, scoped to POST, alongside the
        # /api/messaging/teams precedent: a self-authenticating external webhook
        # whose handler (api_hooks_agent -> _verify_hook_token) is the sole auth
        # gate. The POST scope matters — PUT/DELETE on that same literal path
        # match the {hook_id} wildcard of the dashboard-authed CRUD routes.
        "/api/outbox/notify",
        "/api/notifications/agent",  # MCP-only (send_notification tool); no browser caller
        "/api/slack/upload-file",
        "/api/channel/upload-file",
        "/api/slack/pins",
        "/api/slack/reactions",
        "/api/slack-profile",  # MCP-only (slack_profile tool); no browser caller
        "/api/sessions/summarize",  # MCP-only (list_sessions summarize leg); internal-secret, no browser caller
        # MCP-only (session_ledger_read / session_ledger_record tools); no
        # browser caller. Prefix matching covers "/api/session-ledger/record".
        # Without this entry the tools' internal-secret calls fall through to
        # cookie auth and are refused before the handler's own session
        # recognition can run.
        "/api/session-ledger",
        # MCP-only (knowledge_add_document tool); no browser caller — the
        # dashboard ingests via its own cookie-authed knowledge routes. Same
        # wiring class as "/api/notifications/agent" above.
        "/api/knowledge/agent-document",
        "/api/mcp/servers",
        # Session control -- the three routes behind the session_create /
        # session_stop / session_read_message MCP tools.
        # STRICT, not mixed: no browser calls them, and they are the entry point
        # to opening, stopping, and reading ANOTHER live conversation. A cookie
        # fall-through there would be a new authorization path, not a
        # convenience.
        #
        # Every route registered under /api/session-control MUST appear here.
        # An unlisted path falls through to the general branch, which honors only
        # cookie/query tokens, so the MCP caller's X-Internal-Secret is ignored
        # and the handler's own internal_auth re-assert then refuses it -- the
        # tool is unreachable in production while handler-level tests still pass.
        "/api/session-control/create",
        "/api/session-control/stop",
        "/api/session-control/close",
        "/api/session-control/send",
        "/api/session-control/read",
        # MCP-only structured monitor inspection. The caller selects its
        # session identity through X-Session-Key, so cookie authentication can
        # never authorize this leaf.
        "/api/autonudge/session-monitor",
    }
)


#: Statuses the deny-audit boundary treats as a permission decision. Deliberately
#: not "any 4xx": a 404 from routing and a 302 from host canonicalization are
#: outcomes, not refusals. Nothing raises 401 today (``token_auth_middleware``
#: RETURNS its 401/403 and audits each itself), but a barrier that raises one is
#: the same class of event as a raised 403, so it is covered by position too.
_PRE_AUDIT_DENY_STATUSES = frozenset({401, 403})


async def _audit_denied(caller: str, request: web.Request, error: str) -> None:
    """Record a middleware refusal in the SEL, off the event loop, best-effort.

    Shared by every middleware that denies BEFORE ``sel_audit_middleware`` runs
    (that one is registered inner to them, so a bare raise produces a 403 that
    appears nowhere in the audit log). One helper rather than per-site calls
    because both properties below are easy to omit at a new deny site and
    invisible when omitted:

    * OFF THE LOOP — ``log_api_access`` only enqueues, but the first ``sel()``
      of a process CONSTRUCTS the log: trust-dir creation, key validation, and
      on Windows the owner-only DACL on the key file. A fresh
      dashboard whose first state-changing request is cross-origin would run
      that synchronously on the event loop and stall every other request.
    * BEST-EFFORT — a trust root too short to sign the chain makes construction
      raise, and an unguarded write would turn the refusal into a 500: losing
      the denial in order to report it.

    Calling this CLAIMS the request (:func:`origin.mark_audit_claimed`) so the
    deny-audit boundary outer to every barrier does not record the same refusal
    a second time. The claim is set unconditionally, before the write: a write
    that failed here fails identically in the boundary, so a second doomed
    thread hop buys nothing.
    """
    mark_audit_claimed(request)
    try:
        await asyncio.to_thread(
            lambda: sel().log_api_access(
                caller=caller,
                operation=f"{request.method} {request.path}",
                outcome="denied",
                resources=request.path,
                error=error,
            )
        )
    except Exception:
        logger.warning("Failed to log a middleware denial to SEL", exc_info=True)


def _make_deny_audit_middleware(caller: str) -> Callable:
    """Build the audit boundary for refusals raised BEFORE the audit middleware.

    SHARED by BOTH entrypoints (``start_dashboard`` and the ``--slack-only``
    ``start_api_server``) so the two chains can never drift — same rationale as
    :func:`_make_host_validation_middleware`.

    ``sel_audit_middleware`` is registered INNER to the Host, CSRF and token
    barriers, so a refusal one of them raises produces a 403 that the audit
    middleware never observes. The three known sites each call
    :func:`_audit_denied` themselves and a source-string test pins that they keep
    doing so — but a pin only catches what someone remembers to run, and the
    omission is invisible in production: the refusal simply appears nowhere in
    the audit log. That is the deny-or-audit violation the pin exists to paper
    over.

    Registered OUTER to every barrier, this middleware makes the guarantee
    positional. It catches the refusal on its way out and records it unless some
    inner layer already claimed the request, so a future deny site that forgets
    everything is still audited; forgetting now costs the record's reason
    DETAIL, not the record. The per-site calls become enrichment rather than the
    guarantee.

    Its scope is deliberately narrow, so the audit surface is unchanged and no
    refusal is recorded twice:

    * Only a RAISED ``web.HTTPException`` whose status is in
      :data:`_PRE_AUDIT_DENY_STATUSES`. Everything else propagates untouched.
    * Only an UNCLAIMED request (:data:`origin.AUDIT_CLAIMED_KEY`). A layer claims
      when it has written the specific record itself: the two barriers through
      :func:`_audit_denied`, ``sel_audit_middleware`` for the requests it
      actually logs (so its ``outcome="error"`` entry for a handler's 403 is not
      doubled), and the two WebSocket origin refusals that log their own denial.
      All four go through :func:`origin.mark_audit_claimed`. Not claiming is the
      safe direction: the refusal is then recorded here under a generic reason.
      The one refusal that reaches this middleware unclaimed today is
      ``ws.py``'s cross-origin WebSocket 403, which was audited nowhere before.
    * Returned responses are NOT inspected. ``token_auth_middleware`` returns
      its 401/403 rather than raising and audits each with a specific reason
      code, so its records stay single.

    Best-effort and off the loop come from :func:`_audit_denied`; the refusal is
    re-raised unchanged either way, so an audit failure can never convert a 403
    into a 500.

    ``caller`` is only the FALLBACK label. A refusal raised inner to
    ``token_auth_middleware`` carries an authenticated identity on the request by
    the time it reaches here, and recording the static label instead would file an
    app's or a user's refusal under ``dashboard_user`` — the attribution problem
    ``handlers.terminal``'s own deny site avoids by reading
    ``request["user"]``. Note ``request["app"]`` is ``""`` for the dashboard user
    and that emptiness is POSITIVE proof of them (see ``token_auth``), so an empty
    app falls through to the user rather than to the label.
    """

    @web.middleware  # type: ignore[misc]
    async def deny_audit_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        try:
            return await handler(request)  # type: ignore[operator]
        except web.HTTPException as exc:
            if exc.status in _PRE_AUDIT_DENY_STATUSES and not request.get(AUDIT_CLAIMED_KEY):
                # Status and reason only — never the exception body. The record
                # already carries method, path and caller; what a claimed record
                # adds is the deny site's own explanation, which by definition
                # is missing here.
                await _audit_denied(
                    request.get("app") or request.get("user") or caller,
                    request,
                    f"refused with {exc.status} {exc.reason} before the audit middleware",
                )
            raise

    return deny_audit_middleware


def _make_host_validation_middleware(caller: str) -> Callable:
    """Build the DNS-rebinding ``Host``-header barrier middleware.

    SHARED by BOTH entrypoints (``start_dashboard`` and the ``--slack-only``
    ``start_api_server``) so the two chains can never drift — same rationale
    as ``_STRICT_INTERNAL_API_PATHS`` above. In particular this is the SINGLE
    exemption point for ``origin.PROBE_PATHS``: a change to the exemption is
    necessarily a change in both servers, where test_api_health.py pins it
    through a real middleware chain (disallowed-Host probe allowed,
    disallowed-Host non-probe denied).

    Rejects any request whose ``Host`` header does not name a host we serve.
    Runs on EVERY method (GET data-exfil is the rebinding payload) and
    independently of the CSRF Origin check and loopback trust — a rebound
    request is loopback at the socket but forges ``Host``. See
    ``origin.check_host`` for the missing-Host and empty-allowlist
    deny-by-default carve-outs.

    Probe exemption: orchestrator health probes (kubelet, Docker HEALTHCHECK,
    LBs) address the gateway by container/pod IP, which by construction is
    never in the host allowlist. The probe handlers are token-free/secret-free
    and additionally gate their identity fields on ``check_host``, so
    exempting them leaks nothing a rebound page could not already infer from
    a bare TCP connect (see ``origin.PROBE_PATHS``). This is a permanent,
    deliberate carve-out in a security control: treat ANY addition to
    ``PROBE_PATHS`` as a security review.

    ``caller`` labels the SEL audit line (``dashboard_user`` for the full
    dashboard, ``mcp_tool`` for the headless API server).
    """

    @web.middleware  # type: ignore[misc]
    async def host_validation_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        if request.path not in PROBE_PATHS and not check_host(request):
            # SEL audit (security-relevant permission decision): make
            # DNS-rebinding attempts visible in the audit log, mirroring the
            # API-access audit.
            await _audit_denied(
                caller,
                request,
                f"host header not allowed: {request.headers.get('Host', '')[:100]}",
            )
            raise web.HTTPForbidden(
                text="Host header not allowed.",
                content_type="text/plain",
            )
        return await handler(request)  # type: ignore[operator]

    return host_validation_middleware


#: Methods the CSRF barrier skips. A safe method does not mutate state, and
#: GET-based exfiltration is covered by the Host barrier above, which runs on
#: every method.
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _make_csrf_middleware(caller: str) -> Callable:
    """Build the cross-site CSRF barrier middleware.

    SHARED by BOTH entrypoints (``start_dashboard`` and the ``--slack-only``
    ``start_api_server``) so the two chains can never drift — same rationale as
    :func:`_make_host_validation_middleware`. In particular this is the SINGLE
    read point for ``token_auth.CSRF_EXEMPT_EXACT_METHODS``, so an exemption can
    never be granted on one server and withheld on the other.

    Blocks state-mutating requests that a cross-origin page issued. Loopback
    local processes (mcp-core, cron, doctor) send no Origin header and are
    trusted by ``check_origin``; a browser always sends Origin, so a cross-site
    page is rejected here even before token auth runs.

    Webhook exemption: a self-authenticating external webhook is a
    server-to-server caller that sends neither ``Origin`` nor ``Referer``, which
    ``check_origin`` can only accept from a loopback peer — so without the
    exemption the route is unreachable in the topology that exposes the gateway
    directly, with no configuration that fixes it. Those handlers ignore cookies
    and authenticate a bearer credential a browser cannot forge, which is the
    entire threat CSRF addresses; ``token_auth.CSRF_EXEMPT_EXACT_METHODS`` holds
    the full decision, and any addition to it is a security review. The exempted
    request is still audited — ``sel_audit_middleware`` logs every mutating
    ``/api/`` call in both chains — so the carve-out writes no SEL event of its
    own, matching ``PROBE_PATHS`` on the Host barrier.

    ``caller`` labels the SEL audit line (``dashboard_user`` for the full
    dashboard, ``mcp_tool`` for the headless API server).
    """

    @web.middleware  # type: ignore[misc]
    async def csrf_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        guarded = request.method not in _CSRF_SAFE_METHODS and not is_csrf_exempt(
            request.path, request.method
        )
        if guarded and not check_origin(request, require=True, fallback_header="Referer"):
            await _audit_denied(
                caller,
                request,
                "CSRF check failed: origin not allowed: "
                f"{request.headers.get('Origin', '')[:100]}",
            )
            raise web.HTTPForbidden(
                text="CSRF check failed: request origin not allowed.",
                content_type="text/plain",
            )
        return await handler(request)  # type: ignore[operator]

    return csrf_middleware


# Mixed internal API paths — called by BOTH internal processes (loopback +
# ``X-Internal-Secret``) AND the browser (cookie auth), e.g. ``/api/spawn``
# polled by DCV/SSH-forwarded browsers. On non-loopback they perform explicit
# cookie validation (deny-by-default) rather than hard-denying, so forwarded
# browsers don't trip false "session expired" banners. Prefix-matched:
# ``path == p or path.startswith(p + "/")``. Shared by both entrypoints.
_MIXED_INTERNAL_API_PATHS = frozenset(
    {
        # Called by MCP (loopback + secret) AND browser polling
        # (DCV/SSH-forwarded cookie auth).  See token_auth.py.
        "/api/spawn",
        "/api/chat",
        "/api/lessons",
        "/api/crons",  # CLI cron trigger; prefix covers all sub-routes (consistent with spawn/taskrunner)
        "/api/taskrunner",
        "/api/artifacts",
        # The 5 artifact_folder_* MCP tools authenticate via X-Internal-Secret.
        # token_auth prefix-matching is (path == p or path.startswith(p + "/")),
        # so "/api/artifact-folders" is NOT covered by the "/api/artifacts"
        # entry above — without this entry those MCP calls fall through to
        # cookie auth and fail with "Token required".
        "/api/artifact-folders",
        # Provider-routed remote-artifact browse/clone/fork. Same auth model
        # as "/api/artifacts": browser cookie auth + internal-secret callers;
        # prefix covers every /api/remote-artifacts/{provider}/... sub-route.
        "/api/remote-artifacts",
        "/api/workflows",  # DW engine: MCP tools + Workflows tab polling
        "/api/deploy",  # MCP deploy_artifact tool — server enforces preview-only (confirm/override_scan stripped for internal-secret callers)
        # Issue Radar investigation record — the ONE app route reachable with the
        # internal secret, for the ``issue_radar_record_investigation`` MCP tool.
        # An investigating chat agent has no dashboard token (cookies are
        # httpOnly, ``KIROCREW_INTERNAL_SECRET`` is stripped from agent env by
        # ``sandbox._AGENT_DENIED_ENV_KEYS``, and ``.local_secret`` is on the
        # ``security.py`` sensitive-path denylist), so the PUT the Investigate
        # seed prompt asks for would 403 unconditionally and no investigation
        # could record its findings. Deliberately the FULL path, not the
        # ``/api/apps/issue-radar`` prefix: prefix-matching here would also admit
        # the app's GitHub/GitLab WRITE routes (label, close/reopen, comment) to
        # anything holding the internal secret. This route is local-only triage
        # state — no forge write, no shared ledger.
        "/api/apps/issue-radar/investigation",
        # Ops Mission Control agent surface — the routes the app's SOP-driven
        # crons and investigation slots call through the ``ops_mission_control_api``
        # MCP tool (the app's ONLY credentialed agent path; same trust model as
        # ``/api/apps/issue-radar/investigation`` above: agents hold no cookie,
        # no gateway IPC secret, and the CLI credential mint is denied by the
        # builtin ``credential-exfil`` rules — deliberately, see security.py).
        # Enumerated EXACT paths, never the app prefix: prefix-matching
        # ``/api/apps/ops-mission-control`` would also admit provider
        # configuration/secret writes, ``/settings``, the external ``/webhook``
        # ingest and the human-only ``/incident/proposal/decide`` route to
        # anything holding the internal secret. Bare ``/incident`` is excluded
        # for the same reason (this matcher is exact-or-prefix, so admitting it
        # would admit ``/incident/propose`` and ``/incident/proposal/decide``);
        # single-incident reads go through ``/incidents?id=`` instead. The
        # ``/rotation`` and ``/ledger`` entries DO cover their sub-routes
        # (``/rotation/arm``, ``/ledger/contradictions``, ``/ledger/hygiene``)
        # — all agent-surface by design.
        "/api/apps/ops-mission-control/state",
        "/api/apps/ops-mission-control/signals",
        "/api/apps/ops-mission-control/incidents",
        "/api/apps/ops-mission-control/handover",
        "/api/apps/ops-mission-control/rotation",
        "/api/apps/ops-mission-control/ledger",
        "/api/apps/ops-mission-control/dispatch",
        "/api/apps/ops-mission-control/incident/transition",
        "/api/apps/ops-mission-control/incident/claim",
        "/api/apps/ops-mission-control/incident/action",
        # Issue Radar crew ledger — the read leg and the work-item write leg, for
        # the ``issue_radar_crew_read`` / ``issue_radar_crew_record`` MCP tools. A
        # crew agent has no dashboard token (same three reasons as the
        # investigation entry above), and the ledger is the ONLY thing that
        # survives its compaction, its per-turn ceiling and a gateway restart, so
        # without these entries an unattended crew has no memory at all.
        #
        # FULL paths, never the ``/api/apps/issue-radar`` prefix — for the reason
        # spelled out on the investigation entry: prefix-matching there would also
        # admit the app's GitHub/GitLab WRITE routes (label, close/reopen,
        # comment) to anything holding the internal secret.
        #
        # Read this pair as ONE admission, not two. Matching is
        # ``path == p or path.startswith(p + "/")``, so the ``/crew`` entry
        # already covers ``/crew/work`` and EVERY future ``/crew/...`` sub-route:
        # anything added under that segment becomes agent-reachable the moment it
        # is routed, with no further edit here. So a forge-write or destructive
        # route must not live under ``/crew/`` — put it on its own path, or refuse
        # an internal-secret caller at the handler the way
        # ``api_skills_discover_install`` does below.
        "/api/apps/issue-radar/crew",
        # Redundant under the prefix match above; kept explicit so a reader sees
        # both routes the crew tools actually call.
        "/api/apps/issue-radar/crew/work",
        # Registry skill discovery — the READ leg only, for the
        # ``skill_discover`` / ``skill_fetch`` MCP tools. The Skills page calls
        # the same two routes with cookie auth, hence mixed rather than strict.
        #
        # Prefix-matching (path == p or startswith(p + "/")) means the first
        # entry ALSO admits ``/api/skills/-/discover/install`` — a WRITE that
        # fetches third-party files and writes them into the skills dir. That is
        # closed off at the handler instead: ``api_skills_discover_install``
        # refuses an internal-secret caller outright (see its ``internal_auth``
        # guard), so installation stays a deliberate human action in the
        # dashboard. Do not remove that guard to add an install MCP tool without
        # re-reviewing this admission.
        "/api/skills/-/discover",
        # Redundant under the prefix match above, kept explicit so a reader of
        # this list sees both routes the MCP tools actually call.
        "/api/skills/-/discover/preview",
        "/v1/chat/completions",  # OpenAI-compat API
    }
)


# Base Content-Security-Policy applied to all dashboard responses.
# See ``_apply_security_headers`` for the full rationale and the
# instances-mode ``frame-src`` extension.
_BASE_CSP = (
    "default-src 'self'; "
    # https://esm.sh: MCP App (SEP-1865) srcdoc iframes INHERIT this header
    # CSP (a srcdoc document has no HTTP response of its own), and the real
    # excalidraw/pdf MCP apps load their ESM runtime (React, @excalidraw/…)
    # from esm.sh via importmap. Without these allowances the app's module
    # imports are blocked no matter what the per-app srcdoc <meta> CSP says
    # (when two policies apply, the most restrictive wins per directive).
    # Same pattern as the widget CDN allowances (tailwind/jsdelivr/cdnjs).
    # 'wasm-unsafe-eval': the Pierre highlight workers tokenize with the
    # shiki-wasm engine (website/src/pierre/config.ts, PIERRE_REGEX_ENGINE —
    # chosen there because the JS engine has no backtracking ceiling and a
    # pathological grammar match kills the renderer as a cage OOM).
    # WebAssembly.compile/instantiate requires this source expression in the
    # executing context's script-src, and a same-origin worker takes its CSP
    # from its own script RESPONSE — this header — not from the document that
    # spawned it. Without it the tokenizer worker's WASM instantiation is
    # refused and every diff surface dies on first highlight. It permits ONLY
    # WebAssembly compilation, never JS eval ('unsafe-eval' stays out).
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' "
    "https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com "
    "https://esm.sh; "
    # https://fonts.googleapis.com + https://fonts.gstatic.com: index.html loads
    # the UI's two brand faces (Space Grotesk, JetBrains Mono) from Google Fonts.
    # Without these the stylesheet is refused and BOTH families fall through the
    # stack. macOS lands on -apple-system and looks deliberate; Windows has no
    # such entry, so it drops to the generic sans-serif/monospace and the whole
    # dashboard renders in a face the design never targeted (metrics tuned for
    # Space Grotesk/JetBrains Mono then mis-fit, so chrome text also mis-sizes).
    "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net "
    "https://esm.sh https://fonts.googleapis.com; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self' data: https://esm.sh https://fonts.gstatic.com; "
    # Loopback http(s) origins ({connect_src_extra}) mirror the frame-src note
    # below: WebPreviewPanel does not merely FRAME the local dev server, it also
    # polls it with a no-cors `fetch` liveness probe (a cross-origin iframe
    # cannot report that its server died). Framing without connecting made that
    # probe throw on every tick, so two strikes flipped a perfectly healthy
    # preview to "server stopped responding" and unmounted the iframe. The
    # probe is no-cors, so no response data is ever readable — this admits the
    # reachability check only, and to the same origins frame-src already allows.
    "connect-src 'self' ws://localhost:* ws://127.0.0.1:* "
    "https://esm.sh{connect_src_extra}; "
    "media-src 'self' blob:; "
    "worker-src 'self' blob:; "
    # https://*.cloudfront.net: live preview iframes for deployed webapp
    # artifacts (WebAppArtifactCard / WebAppThumb). The artifact-deploy
    # contract only ever produces `<dist-id>.cloudfront.net` URLs; the FE
    # additionally gates on that exact host shape (framablePreviewUrl) so a
    # crafted webapp_metadata URL on any other host is never framed.
    # http://127.0.0.1:* / http://localhost:*: the Web Preview panel
    # (WebPreviewPanel) frames a local dev/static server. Always admitted so
    # the feature works in the packaged dashboard, not only in instances mode.
    # The panel isolates the preview host from the dashboard host
    # (isolatePreviewHost) so host-scoped dashboard cookies are never sent to
    # the framed server. The *.localhost tunnel wildcard stays instances-gated.
    "frame-src 'self' blob: https://*.cloudfront.net{frame_src_extra}; "
    "object-src 'none'; base-uri 'self'; frame-ancestors {frame_ancestors}"
)

# Loopback preview origins — always framable AND connectable (see the
# frame-src / connect-src notes above). Aligned with the URLs
# WebPreviewPanel.normalizeUrl accepts: http+https on every loopback host, so a
# preview never renders blank due to a CSP-blocked frame, nor gets declared
# unreachable due to a CSP-blocked liveness probe.
#
# IPv6 loopback ([::1]) is deliberately OMITTED. A CSP host-source that pairs a
# bracketed IPv6 literal with a wildcard port — `http://[::1]:*` — is invalid
# per the CSP grammar, so Chromium drops that ENTIRE source and logs
# "contains an invalid source: 'http://[::1]:*'". Because the source was being
# dropped anyway, `[::1]:*` never actually admitted anything; removing it is
# behaviour-preserving for IPv4 loopback (127.0.0.1 / localhost / 0.0.0.0, whose
# non-bracketed literals accept a wildcard port) and only silences the console
# error the pet page surfaced. There is no wildcard-port form Chromium accepts
# for a bracketed IPv6 host, so IPv6 loopback preview cannot be expressed here
# without pinning a specific port — which the arbitrary-port preview use case
# rules out.
_LOOPBACK_FRAME_SRC = (
    " http://127.0.0.1:* http://localhost:* http://0.0.0.0:*"
    " https://127.0.0.1:* https://localhost:* https://0.0.0.0:*"
)
# Additional tunnel wildcard, only when the instances feature is enabled.
_INSTANCES_FRAME_SRC_EXTRA = " http://*.localhost:*"

# Permissions-Policy header. Chrome 143+ changed the default policy so
# that clipboard-write is DENIED unless explicitly allowlisted, even in
# secure contexts like http://localhost (crbug.com/414348233). Without
# this header, ``navigator.clipboard.writeText`` fails with a permissions
# policy violation, breaking the "Copy link" button on published
# artifacts. Grant same-origin only; cross-origin remains denied.
_PERMISSIONS_POLICY = "clipboard-write=(self), clipboard-read=(self)"

# /vendor/* is fetched by sandboxed widget/artifact iframes, which are
# null-origin (srcdoc/blob) documents and therefore NON-secure contexts. On the
# default deployment the gateway is plain http on loopback — a "more-private
# address space" under Chrome's Private Network Access policy — which blocks
# the iframe's <script src> for the Tailwind runtime unless the load goes
# through CORS with server approval: the tag carries
# crossorigin="anonymous" (widgetSrcdoc.ts) and this response carries
# Access-Control-Allow-Origin. Verified against real Chromium: with the
# header the runtime loads; without it the load hard-fails (crossorigin
# makes the header MANDATORY, not additive), the runtime never arrives,
# Tailwind-classed widgets render unstyled, and the widget loading overlay
# sits on its hang backstop (blank box), see issue #6181. `*` leaks nothing:
# /vendor/ holds only public, non-secret static JS (already auth-exempt via
# token_auth._BYPASS_PREFIXES) and the response carries no credentials or
# user data.
_VENDOR_PATH_PREFIX = "/vendor/"
_VENDOR_CORS_HEADER_VALUE = "*"
_PNA_REQUEST_HEADER = "Access-Control-Request-Private-Network"
_PNA_RESPONSE_HEADER = "Access-Control-Allow-Private-Network"
# Two hours — Chrome caps preflight cache entries at 7200s, so a larger value
# documents a guarantee the browser does not honour. The vendor files are
# stable, unversioned assets; caching the approval avoids a preflight per
# widget for the cap's duration.
_VENDOR_PREFLIGHT_MAX_AGE_SECS = 7200


async def _vendor_preflight_handler(request: web.Request) -> web.Response:
    """Answer the CORS / Private Network Access preflight for ``/vendor/*``.

    Forward-compat: current Chromium blocks the insecure-initiator load at
    the CORS layer WITHOUT sending a PNA preflight (verified empirically —
    the GET-with-Access-Control-Allow-Origin path above is the live fix).
    Chrome's PNA rollout answers a private-network subresource fetch with a
    preflight OPTIONS carrying ``Access-Control-Request-Private-Network:
    true``; ``add_static`` registers GET/HEAD only, so if/when that ships
    for this initiator class the preflight would 405 and the runtime load
    would fail closed again. The PNA grant header is echoed only when the
    request actually asks for it, per the PNA spec's request/response
    pairing.
    """
    headers = {
        "Access-Control-Allow-Origin": _VENDOR_CORS_HEADER_VALUE,
        "Access-Control-Allow-Methods": "GET, HEAD",
        "Access-Control-Max-Age": str(_VENDOR_PREFLIGHT_MAX_AGE_SECS),
    }
    if request.headers.get(_PNA_REQUEST_HEADER, "").lower() == "true":
        headers[_PNA_RESPONSE_HEADER] = "true"
    return web.Response(status=204, headers=headers)


# Content-hashed build output (Vite emits ``/assets/<name>-<hash>.<ext>``;
# the URL changes whenever the content changes) is safe to cache forever.
# Everything else — index.html, the SPA shell, /api — keeps the no-store
# policy so upgrades are picked up immediately. Without this exemption the
# ~6MB entry bundle is re-downloaded on every page load, and a reload right
# after a gateway restart bets the whole page on that transfer succeeding
# while the gateway is at cold-start peak (the "black screen until hard
# refresh" failure mode). Deliberately excludes /vendor, /fonts and
# /sprites: those use stable, un-hashed filenames.
_IMMUTABLE_PATH_PREFIXES = ("/assets/",)
_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"

# Max size of a single incoming HTTP header field, raised from aiohttp's
# 8190-byte default. Browser cookies are not port-isolated (RFC 6265), so on
# 127.0.0.1 the per-port mc_token_<port>/mc_refresh_<port> cookies of every
# gateway instance pile up in one shared Cookie header. At the 8190 default
# that header crosses the limit after ~16 ports and aiohttp's C parser rejects
# the request with 400 LineTooLong BEFORE any handler runs — so the request
# that would prune the jar can never execute. This headroom lets an oversized
# request reach the handler, which then expires the other-port cookies (see
# refresh_tokens.foreign_port_cookies) so the jar self-trims. 32 KiB stays well
# under a DoS-relevant size while covering ~60 accumulated ports plus other
# request headers.
_MAX_HEADER_FIELD_SIZE = 32 * 1024

# Upper bound on the tunnel teardown at shutdown. The provider behind the
# ``TunnelProvider`` seam may talk to a remote control plane (or supervise a
# child process), so an unbounded await here could hang ``runner.cleanup()``
# forever and wedge the whole gateway exit. 5s is generous for a local
# teardown and still well inside the desktop app's shutdown window.
_TUNNEL_STOP_TIMEOUT_SECS = 5.0


def _extra_frame_ancestors(
    request: "web.Request | None", app: "web.Application | None" = None
) -> list[str]:
    """Exact parent origins (beyond ``'self'``) permitted to frame this dashboard.

    Read from the ``embed_parent_port`` claim of the request's signed token: the
    multi-instance connect flow mints the remote token carrying the *parent*
    (embedding) dashboard's port — its ``KIROCREW_PORT`` — so the embedded remote
    authorizes exactly that loopback parent origin as a CSP frame-ancestor. The
    claim is carried through the link→session token exchange into the session
    cookie (see token_auth_middleware), which also stashes the validated port on
    the request BEFORE it revokes the link nonce. This reader prefers that stashed
    value, then the query token, then the ``mc_token_<port>`` cookie — so it works
    for the first ``?token=`` framed document (whose link nonce is revoked by the
    exchange) AND every subsequent cookie-authenticated framed load. The port is
    expanded to the loopback hosts (the desktop app may load on any of them).
    Exact origins only — **never a wildcard, never a hardcoded port** — and gated
    on a validly-signed token, so a random local page (which has no token) can
    never get its origin into ``frame-ancestors`` (clickjacking, CSE SEC-016).
    Empty (default ``'self'`` + ``X-Frame-Options`` posture) for any request
    without such a token. See docs/system-specs/modules/security.md.
    """
    if request is None:
        return []
    # Prefer the claim the auth middleware validated and stashed on the request:
    # it is set BEFORE the link→session exchange revokes the link nonce, so the
    # first ``?token=`` framed document (whose header the browser enforces) still
    # carries the parent origin. Fall back to the query token, then the
    # ``mc_token_<port>`` session cookie (steady-state cookie-authenticated
    # framed loads), mirroring token_auth_middleware's own extraction.
    port: int | None = None
    stashed = request.get("embed_parent_port")
    if isinstance(stashed, str) and stashed.isdigit():
        _p = int(stashed)
        if 1 <= _p <= 65535:
            port = _p
    if port is None:
        # Prefer the credential token_auth actually VALIDATED (it publishes it
        # as request["auth_token"]): its extraction can adopt the session cookie
        # over an invalid query token, so a fixed query-then-cookie re-derivation
        # could read an unverified value. Fall back to that order only when no
        # credential was published (e.g. a surface that never reached the
        # middleware's authenticated paths).
        published = request.get("auth_token", "")
        token = published if isinstance(published, str) else ""
        if not token:
            token = request.query.get("token") or ""
        if not token:
            port_fallback = app.get("port", _DEFAULT_PORT) if app is not None else _DEFAULT_PORT
            cookie_port = _cookie_port_from_host(request, port_fallback)
            token = request.cookies.get(f"mc_token_{cookie_port}", "")
        port = token_embed_parent_port(token)
    if port is None:
        return []
    # A CSP host-source admits only letters, digits and hyphens in the host, so a
    # bracketed IPv6 literal cannot be expressed: `http://[::1]:<port>` is refused by
    # the browser ("the directive 'frame-ancestors' does not support the source
    # expression") and dropped, so it never granted anything — it only logged a
    # warning on every framed response. There is no valid spelling to substitute,
    # so an IPv6-loopback parent cannot be authorized at all.
    return [f"http://{host}:{port}" for host in ("127.0.0.1", "localhost", "kirocrew.localhost")]


def _apply_security_headers(
    resp: web.StreamResponse,
    app: web.Application,
    path: str = "",
    request: "web.Request | None" = None,
) -> None:
    """Apply cache-control and security headers to a dashboard response.

    Sets four groups of headers (all via ``setdefault`` so handlers keep
    the ability to override):

    1. Cache-Control / Pragma / Expires — prevent Chrome from caching stale
       assets across upgrades. Content-hashed paths (``/assets/``) are the
       exception: their URL *is* the version, so they are served as
       ``immutable`` instead (see ``_IMMUTABLE_PATH_PREFIXES``).
    2. Content-Security-Policy — defense-in-depth against XSS. Primary XSS
       protection is rehypeSanitize (strips script/iframe/form/foreignObject
       at HAST level before rendering). CSP allows ``'unsafe-inline'``
       because widget iframes (blob: sandbox) inherit parent CSP per W3C
       spec — inline scripts in widgets need it. Widget isolation is
       enforced by ``sandbox="allow-scripts"`` (no parent DOM access) +
       widget-level CSP meta (connect-src 'none'). When the instances
       feature is enabled, ``frame-src`` is extended with a loopback
       wildcard so dynamically-connected tunnel ports can be framed.
    3. Permissions-Policy — required by Chrome 143+ to permit
       ``navigator.clipboard.writeText`` even on secure contexts. Without
       an explicit ``clipboard-write=(self)`` grant, the Copy-link button
       on published artifacts fails with a permissions-policy violation
       (crbug.com/414348233).
    """
    # Immutable only on success — during cold-start a request to /assets/*
    # may get 404 (static route not mounted) or 503 (SPA fallback answering).
    # Caching that error with max-age=31536000 would be a permanent black
    # screen, the same bug class sw.js fixes for the cache layer.
    # 206 (range) and 304 (conditional) are also valid static-handler
    # responses for hashed assets: a 304's headers merge into the stored
    # cache entry, so answering it with no-store would degrade the cached
    # immutable bundle.
    status = getattr(resp, "status", None)
    if status in (200, 206, 304) and path.startswith(_IMMUTABLE_PATH_PREFIXES):
        resp.headers.setdefault("Cache-Control", _IMMUTABLE_CACHE_CONTROL)
    else:
        resp.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        resp.headers.setdefault("Pragma", "no-cache")
        resp.headers.setdefault("Expires", "0")

    state = app.get("state")
    instances_mgr = getattr(state, "instances_manager", None) if state else None
    # Loopback preview origins are always framable (Web Preview panel); the
    # *.localhost tunnel wildcard is added only when instances mode is active.
    frame_src_extra = _LOOPBACK_FRAME_SRC + (
        _INSTANCES_FRAME_SRC_EXTRA if instances_mgr is not None else ""
    )
    # frame-ancestors: ``'self'`` plus the EXACT parent origin carried in the
    # request token's embed_parent_port claim (see _extra_frame_ancestors) — never
    # a wildcard, never a hardcoded port. Lets the desktop app frame an embedded
    # instance dashboard across loopback ports, while any local page without a
    # validly-signed token stays blocked (clickjacking).
    extra_ancestors = _extra_frame_ancestors(request, app)
    # Same builder the sandboxed-document responses use. Hand-joining here instead
    # would leave the shell as the one ancestor source nothing validates, which is
    # exactly how an inexpressible entry (a bracketed IPv6 literal) reached a
    # header before and made engines drop the whole directive.
    frame_ancestors = frame_ancestors_value(extra_ancestors)
    resp.headers.setdefault(
        "Content-Security-Policy",
        _BASE_CSP.format(
            connect_src_extra=_LOOPBACK_FRAME_SRC,
            frame_src_extra=frame_src_extra,
            frame_ancestors=frame_ancestors,
        ),
    )
    resp.headers.setdefault("Permissions-Policy", _PERMISSIONS_POLICY)
    # CORS approval for the vendored runtime files fetched by null-origin
    # sandboxed iframes; pairs with the /vendor OPTIONS preflight handler.
    # See _VENDOR_PATH_PREFIX for the full Private-Network-Access rationale.
    if path.startswith(_VENDOR_PATH_PREFIX):
        resp.headers.setdefault("Access-Control-Allow-Origin", _VENDOR_CORS_HEADER_VALUE)
    # Defense-in-depth browser headers (CWE-1021/693/200/319). All via setdefault
    # so a handler can override. The clickjacking control is CSP ``frame-ancestors``
    # above. X-Frame-Options is origin-exact (SAMEORIGIN) and cannot express the
    # allowlist, so we keep it as the legacy backstop ONLY in the default posture
    # (no extra ancestor trusted); when an operator has configured a cross-port
    # embed origin we omit it, otherwise SAMEORIGIN would contradict the CSP and
    # refuse the embed. Browsers honor frame-ancestors over X-Frame-Options when
    # both are present. nosniff blocks MIME-confusion; Referrer-Policy avoids
    # leaking the (token-bearing) dashboard URL cross-origin. HSTS is inert over
    # the default loopback HTTP bind but protects HTTPS tunnel/desktop access, so
    # it is set unconditionally (browsers ignore it on plain HTTP).
    if not extra_ancestors:
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


# URL prefix for app-shipped standalone HTML windows. One namespace keeps app
# window URLs from colliding with the SPA's own routes, and the two path segments
# after it mirror the on-disk `<app>/<name>.html` exactly — see
# `discover_app_window_entries` for what the previous flat scheme cost.
APP_WINDOW_URL_PREFIX = "app-windows"


def discover_app_window_entries(windows_root: Path) -> list[tuple[str, Path]]:
    """Enumerate app window entries as ``(route_path, file)``.

    An app ships standalone HTML windows as ``<windows_root>/<app>/<name>.html``
    and they are served at ``/app-windows/<app>/<name>.html`` — the same two
    segments, so the URL and the file agree by construction.

    An earlier revision served them FLAT at ``/<app>-<name>.html``, which is
    ambiguous the moment either name contains a hyphen: app ``foo`` + window
    ``bar-baz`` and app ``foo-bar`` + window ``baz`` both spell
    ``/foo-bar-baz.html``. That cost two pieces of machinery — a collision
    refusal here, and a middleware in ``vite.config.ts`` that guessed the split
    by trying each hyphen position, which could resolve to the WRONG file rather
    than refuse. Keeping the boundary in the URL deletes the whole class, so
    neither exists any more. The duplicate check below is retained as a cheap
    invariant: with distinct path segments the filesystem cannot produce two
    identical routes, so a hit means the convention changed under us.

    Returned paths come from the enumerated FILES; the request path is never used
    to build a filesystem path, so there is no traversal surface.
    """
    if not windows_root.is_dir():
        return []
    root = windows_root.resolve()
    out: list[tuple[str, Path]] = []
    claimed: dict[str, Path] = {}
    for entry in sorted(windows_root.glob("*/*.html")):
        # Confine the enumerated file to the build tree. The glob cannot walk out
        # on its own, but a symlink planted inside `dist/` could, and this function
        # hands every result to `web.FileResponse` — an unconditional read of
        # whatever the path points at. Resolving and comparing also makes the
        # barrier visible to dataflow analysis, which reported this join as a path
        # injection precisely because the safety was structural rather than stated.
        resolved = entry.resolve()
        if root not in resolved.parents:
            logger.error(
                "App window entry %s resolves outside the build tree (%s) — refusing "
                "to serve it.",
                entry,
                root,
            )
            continue
        route_path = f"/{APP_WINDOW_URL_PREFIX}/{entry.parent.name}/{entry.stem}.html"
        prior = claimed.get(route_path)
        if prior is not None:  # pragma: no cover - unreachable by construction
            logger.error(
                "App window entry %s collides with %s on route %s — refusing to "
                "register the second. Two files cannot share this route, so the "
                "path convention has drifted.",
                entry,
                prior,
                route_path,
            )
            continue
        claimed[route_path] = resolved
        out.append((route_path, resolved))
    return out


def _window_entry_handler(entry: Path) -> Callable[[web.Request], Awaitable[web.FileResponse]]:
    """A handler that serves ONE enumerated window file.

    A factory rather than the usual default-argument idiom
    (``async def h(req, _file=entry)``). Both avoid the late-binding capture bug
    in a loop, but the default-argument form puts the path in a REQUEST
    HANDLER'S SIGNATURE — so it reads, to a human and to dataflow analysis
    alike, as something a request could supply, and `py/path-injection` flagged
    it as exactly that. Here the path is a closure cell fixed at registration and
    the handler takes only the request, which is what is actually true: these
    routes are built from files enumerated at startup and the request path never
    reaches the filesystem.
    """

    async def _serve(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(entry)

    return _serve


def _register_dist_static_routes(app: web.Application, dist_dir: Path) -> None:
    """Register static routes for the React ``dist/`` build on ``app``.

    Extracted from ``start_dashboard`` so the route wiring (which subdirectories
    of the build get served at which prefix) is unit-testable without standing
    up the full gateway. Each optional subdirectory is mounted only when present.
    """
    app.router.add_static(
        "/assets",
        dist_dir / "assets" if (dist_dir / "assets").is_dir() else dist_dir,
        show_index=False,
        append_version=True,
    )
    if (dist_dir / "sprites").is_dir():
        app.router.add_static("/sprites", dist_dir / "sprites", show_index=False)
    # Self-hosted fonts (AWS Diatype family) live at dist/fonts/ and are
    # referenced by absolute url('/fonts/...') in @font-face. Without this
    # route they fall through to the SPA fallback (index.html), and the
    # browser reports "invalid sfntVersion" trying to parse HTML as a font.
    if (dist_dir / "fonts").is_dir():
        app.router.add_static("/fonts", dist_dir / "fonts", show_index=False)
    # Vendor shims for the app import map (react, react-dom, react/jsx-runtime)
    if (dist_dir / "vendor").is_dir():
        app.router.add_static(
            "/vendor",
            dist_dir / "vendor",
            show_index=False,
            append_version=False,  # stable URLs, no cache-busting
        )
        # PNA/CORS preflight, forward-compat: add_static registers GET/HEAD
        # only, so a private-network preflight OPTIONS would 405 and fail the
        # widget iframe's runtime load closed if Chrome starts sending one for
        # this initiator class (today it blocks at the CORS layer without a
        # preflight — see _vendor_preflight_handler).
        app.router.add_route("OPTIONS", "/vendor/{tail:.*}", _vendor_preflight_handler)
    # App Store brand assets — builtin app icons + hero images live at
    # dist/app-assets/ and are referenced by absolute url('/app-assets/...')
    # from each builtin's app.json (iconUrl / heroImage / heroImageDark).
    # These resolve in the Vite dev server (public/ served at root) but, once
    # the gateway serves the built dist/, they need an explicit mount: without
    # it the request falls through to the SPA fallback (index.html) and the
    # App Store <img> tags try to parse HTML as an image → onError placeholder
    # (generic lucide icon / "KIROCREW" hero). Stable, un-hashed filenames, so
    # no append_version cache-busting.
    if (dist_dir / "app-assets").is_dir():
        app.router.add_static("/app-assets", dist_dir / "app-assets", show_index=False)

    # App window entries — separate Vite bundles an app ships as standalone
    # HTML windows, loaded by a shell window rather than the SPA router. The
    # SOURCE html lives inside the app's own folder (website/src/apps/<app>/
    # <name>.html) so each app stays one self-contained folder, and Vite
    # mirrors that path into dist. Each discovered entry is served at
    # /<app>-<name>.html: a flat, stable url the loading shell can hard-code,
    # independent of where the file sits in dist. (In dev the Vite server
    # answers the same urls via the `app-window-urls` rewrite in
    # vite.config.ts, so one url works against either server.)
    #
    # Routes are registered from the files enumerated HERE, at startup; the
    # request path is never used to build a filesystem path, so there is no
    # traversal surface. The same enumeration feeds the SPA-shell fallback
    # exclusion (token_auth.register_app_window_paths): the fallback answers
    # UNAUTHENTICATED GETs so the token bootstrap can load, and a window entry
    # left inside it would be shadowed by an unauthenticated dashboard shell.
    # Registering both from one loop makes route/exclusion drift impossible.
    #
    # A missing entry is not a small failure: the SPA fallback would answer
    # with the dashboard shell, so the window would open showing a full
    # dashboard instead of its own UI.
    windows_root = dist_dir / "src" / "apps"
    window_paths: list[str] = []
    for route_path, entry in discover_app_window_entries(windows_root):
        app.router.add_get(route_path, _window_entry_handler(entry))
        window_paths.append(route_path)
    register_app_window_paths(window_paths)
    logger.info("Serving React build from %s", dist_dir)


def _precompute_telemetry(state: "DashboardState") -> None:
    """Pre-compute telemetry data (blocking I/O — call before server starts)."""
    from kiro_crew.dashboard.handlers_system import _get_owner_hash, _get_static_system_info

    _log = logging.getLogger(__name__)
    owner_hash = "unknown"
    try:
        owner_hash = _get_owner_hash(state)
    except Exception:
        _log.warning("Failed to pre-compute owner hash", exc_info=True)
    static_info: dict = {}
    try:
        static_info = dict(_get_static_system_info())
    except Exception:
        _log.warning("Failed to pre-compute system info", exc_info=True)

    # Backend telemetry sink (PlatformContext).  The Default TelemetryProvider's
    # record_event is a no-op, so standalone is unchanged; the companion
    # records a gateway-start event.  Best-effort — a telemetry failure never
    # blocks server startup.
    try:
        current_context().telemetry.record_event(
            "gateway_start",
            {
                "owner_id_hash": owner_hash,
                "os_type": static_info.get("os", ""),
                "arch": static_info.get("arch", ""),
            },
        )
    except Exception:
        _log.debug("telemetry.record_event(gateway_start) failed", exc_info=True)


def _deferred_session_control(handler_name: str) -> Callable:
    """Bind a session-control route without importing the subsystem at boot.

    Session control is feature-flagged (``agent.session_control``), and the
    enabled check lives inside the handler -- so a module-level import would be
    an eager import of an optional subsystem whose gate runs after it, which the
    boot-path rule names explicitly. Route registration itself is allowed at
    boot; only the import moves to first request, so an operator who disabled the
    feature never pays for loading it.
    """

    async def _route(request: web.Request) -> web.StreamResponse:
        from kiro_crew.dashboard.handlers import session_control

        handler = getattr(session_control, handler_name)
        return await handler(request)

    _route.__name__ = handler_name
    return _route


def _register_mcp_routes(app: web.Application) -> None:
    """Register API routes used by MCP tools (spawn, lessons, crons, etc.)."""
    app.router.add_post("/api/spawn", handlers.api_spawn)
    app.router.add_post("/api/spawn/lost", handlers.api_spawn_lost)
    app.router.add_post("/api/spawn/mark-collected", handlers.api_spawn_mark_collected)
    # MCP Apps (SEP-1865): embedded app iframe -> gateway tool callback.
    app.router.add_post("/api/mcp-apps/call", handlers.api_mcp_apps_call)
    app.router.add_get("/api/spawn", handlers.api_spawn_list)
    app.router.add_post("/api/spawn/stop-all", handlers.api_spawn_stop_all)
    app.router.add_get("/api/spawn/{agent_id}", handlers.api_spawn_status)
    app.router.add_delete("/api/spawn/{agent_id}", handlers.api_spawn_delete)
    app.router.add_post("/api/spawn/{agent_id}/retry", handlers.api_spawn_retry)
    app.router.add_post("/api/spawn/{agent_id}/continue", handlers.api_spawn_continue)
    app.router.add_post("/api/spawn/{agent_id}/steer", handlers.api_spawn_steer)
    app.router.add_post("/api/spawn/{agent_id}/release", handlers.api_spawn_release)
    app.router.add_delete("/api/spawn", handlers.api_spawn_clear)
    app.router.add_get("/api/lessons", handlers.api_lessons)
    app.router.add_post("/api/lessons", handlers.api_lessons_create)
    app.router.add_delete("/api/lessons", handlers.api_lessons_delete)
    app.router.add_get("/api/session-ledger", handlers.api_session_ledger_get)
    app.router.add_post("/api/session-ledger/record", handlers.api_session_ledger_record)
    app.router.add_get("/api/crons", handlers.api_crons)
    app.router.add_post("/api/crons", handlers.api_crons_create)
    app.router.add_delete("/api/crons", handlers.api_cron_batch_delete)
    app.router.add_get("/api/crons/history", handlers.api_cron_history_all)
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
    # send_notification MCP tool (RFC notification bus Phase 5) — registered
    # here (not the dashboard-only block) so headless --slack-only mode
    # serves it too; it is on _STRICT_INTERNAL_API_PATHS like send-message.
    app.router.add_post("/api/notifications/agent", handlers.api_notification_agent_push)
    # Session control. Registered here so the headless --slack-only server
    # serves the same MCP surface as the dashboard; all three are on
    # _STRICT_INTERNAL_API_PATHS, which test_session_control_routes_are_strict
    # pins by deriving the route set from the router rather than a hand-copied list.
    app.router.add_post(
        "/api/session-control/create", _deferred_session_control("api_session_control_create")
    )
    app.router.add_post(
        "/api/session-control/stop", _deferred_session_control("api_session_control_stop")
    )
    app.router.add_post(
        "/api/session-control/close", _deferred_session_control("api_session_control_close")
    )
    app.router.add_post(
        "/api/session-control/send", _deferred_session_control("api_session_control_send")
    )
    app.router.add_get(
        "/api/session-control/read", _deferred_session_control("api_session_control_read")
    )
    app.router.add_get("/api/browser/install", handlers.api_browser_install_get)
    app.router.add_put("/api/browser/token", handlers.api_browser_token_put)
    app.router.add_post("/api/browser/install", handlers.api_browser_install_start)
    app.router.add_post("/api/browser/engine", handlers.api_browser_engine_install)
    app.router.add_get("/api/browser/view", handlers.api_browser_view_get)
    app.router.add_post("/api/browser/view/start", handlers.api_browser_view_start)
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
        api_autonudge_get,
        api_autonudge_list,
        api_autonudge_start,
        api_autonudge_update,
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
    app.router.add_get("/api/monitors", api_monitors_list)
    app.router.add_post("/api/monitors", api_monitor_create)
    app.router.add_get("/api/monitors/slot/{slot_key}", api_monitor_slot_get)
    app.router.add_patch("/api/monitors/{monitor_id}", api_monitor_update)
    app.router.add_post("/api/monitors/{monitor_id}/stop", api_monitor_stop)
    app.router.add_post("/api/monitors/{monitor_id}/restart", api_monitor_restart)

    # Agent questions. The MCP ask_question tool no longer posts here: it returns
    # a session directive and the dashboard posts a NON-BLOCKING card (see
    # mcp_tools.control.ask_question). This API stays live because the UI reads
    # /pending to rehydrate cards after a reload and answers or dismisses them
    # through the routes below, and POST /api/ask-question still opens a blocking
    # wait for any caller that uses it — so it must not be wrapped in any
    # short-timeout middleware.
    from kiro_crew.dashboard.handlers.ask_question import (
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


def _export_bound_port(runner: web.AppRunner, port: int) -> None:
    """Advertise the actually-bound dashboard port to child processes.

    Sets ``KIROCREW_BOUND_PORT`` in this process's environment once the TCP
    site is listening, so everything the gateway spawns (kiro-cli sessions and
    the MCP stdio servers they start) inherits the port that is really bound
    instead of re-deriving a guess from ``dashboard.url``. A portless URL makes
    ``parse_dashboard_url`` substitute the default port — right for the server
    (it must bind something), wrong for a child aiming a loopback callback at
    a gateway that may be bound elsewhere.

    Deliberately a DISTINCT variable from ``KIROCREW_PORT``: that one means
    "operator-declared port" everywhere else — ``service_environment()`` bakes
    it into persistent unit files, and config code reads it as intent — so
    writing bound truth into it would let a ``--port auto`` ephemeral port be
    frozen into a service install run from a gateway-descended shell, and
    would leak between tests through the process environment.
    ``KIROCREW_BOUND_PORT`` carries ephemeral truth only: consumed by
    ``port_resolution.resolve_client_port`` one step below the operator override,
    never persisted.

    *port* is ``0`` for an OS-assigned ephemeral bind (``--port auto``); the
    real port is then read back from the runner's bound addresses (only the
    TCP site is on the runner when this runs — the unix site is added after).
    Best-effort: when no TCP address is readable the environment is left
    untouched, which is exactly the pre-export behavior.
    """
    bound = _resolved_bound_port(runner, port)
    if bound:
        os.environ["KIROCREW_BOUND_PORT"] = str(bound)
        logger.debug("Exported KIROCREW_BOUND_PORT=%d for child processes", bound)
    else:
        logger.warning(
            "Could not read the bound dashboard port; child processes will "
            "re-derive it from config and the run-marker"
        )


def _resolved_bound_port(runner: web.AppRunner, port: int) -> int:
    """The port actually bound: *port*, or the OS-assigned one when it is ``0``.

    ``0`` means an ephemeral bind (``--port auto``, which ``--test-mode`` also
    implies), so the declared value names no listener and anything keyed by it
    would name the wrong one. Shared by the child-env export and the credential
    publication, which must agree: a credential filed under port ``0`` is
    unreachable for every client, and they would fall back to the shared file --
    which is exactly what the live-sibling guard deliberately leaves pointing at
    the sibling, so the ephemeral gateway would 403 every internal call.

    Returns ``0`` only when no TCP address is readable at all.
    """
    if port:
        return port
    for addr in runner.addresses:
        # TCP socknames are (host, port[, flowinfo, scope_id]) tuples; a
        # unix socket's would be a bare str path.
        if isinstance(addr, (tuple, list)) and len(addr) >= 2 and isinstance(addr[1], int):
            return addr[1]
    return 0


async def _start_site(
    site: web.TCPSite,
    port: int,
    *,
    retries: int = 30,
    delay: float = 0.5,
    reclaim: Callable[[int], Awaitable[str]] | None = None,
) -> None:
    """Start *site*, reclaiming a stale holder / retrying on EADDRINUSE.

    On the first EADDRINUSE we probe *who* holds the port. A previous gateway
    that died uncleanly (force-exit or ``kill -9``) can leave a process holding
    the LISTEN socket that will never release it, so plain waiting cannot
    recover — :func:`reclaim_stale_gateway_port` terminates such a stale holder
    so the subsequent retry rebinds cleanly. A live, responsive gateway or a
    non-KiroCrew process is never touched; those (and any case where the holder
    can't be identified) fall back to a wait-up-to-*retries*×*delay* loop before
    giving up with ``SystemExit(1)``. Non-EADDRINUSE OSErrors are re-raised.
    """
    _reclaim = reclaim if reclaim is not None else reclaim_stale_gateway_port
    last_exc: OSError | None = None
    for attempt in range(retries):
        try:
            await site.start()
            return
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            last_exc = exc
            # release the partially-started site before retrying
            await site.stop()
            if attempt == 0:
                try:
                    outcome = await _reclaim(port)
                except Exception:  # never let a reclaim bug block startup
                    logger.exception(
                        "Port %d reclaim probe failed — falling back to wait/retry.",
                        port,
                    )
                    outcome = ""
                if outcome == RECLAIMED:
                    logger.warning(
                        "Reclaimed port %d from a stale KiroCrew gateway — rebinding.",
                        port,
                    )
                elif outcome not in (HEALTHY_PEER, FOREIGN_HOLDER):
                    # NO_HOLDER / UNAVAILABLE / RECLAIM_FAILED / reclaim error:
                    # nothing safely reclaimable, so wait for a possible graceful
                    # handover. (A healthy peer / foreign holder won't release, so
                    # we skip this misleading "waiting" message for those.)
                    logger.warning(
                        "Port %d in use — waiting up to %.0fs for the previous"
                        " gateway to release it…",
                        port,
                        retries * delay,
                    )
            if attempt < retries - 1:
                await asyncio.sleep(delay)
    logger.error(
        "Port %d still in use after %.0fs — is another KiroCrew gateway running?\n"
        "Stop it with: kirocrew stop  or  sudo systemctl stop kirocrew",
        port,
        retries * delay,
    )
    raise SystemExit(1) from last_exc


def _remove_stale_unix_socket(path: Path) -> None:
    """Best-effort unlink of a leftover unix-socket file before rebind.

    Only a socket inode is removed — anything else at the path is left in
    place (and the subsequent bind fails, degrading to TCP-only). Safe against
    a live sibling instance: the socket name is port-suffixed and the TCP port
    bind (a singleton per port) has already succeeded by the time this runs,
    so an existing file with our port's name can only be stale.
    """
    try:
        st = os.stat(path)
    except OSError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        logger.warning(
            "path %s exists and is not a socket (mode=%o); leaving in place", path, st.st_mode
        )
        return
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("could not remove stale dashboard socket %s: %s", path, exc)


async def _start_unix_site(runner: web.AppRunner, port: int) -> Path | None:
    """Additionally serve the internal API on a unix socket (POSIX only).

    Binds ``dashboard_socket_path(port)`` on the same :class:`web.AppRunner`
    as the TCP site, so both transports serve the identical app + middleware
    chain. The unix transport exists so ``token_auth_middleware`` can
    kernel-verify (``SO_PEERCRED`` + /proc ancestry) the session identity an
    internal caller declares in ``X-Session-Key`` — TCP loopback carries no
    peer credentials.

    Strictly additive: skipped entirely on Windows, and ANY failure (bind
    error, permission problem) logs once and degrades to TCP-only, which is
    exactly today's behavior. The socket file inherits the data home's 0700
    directory gate (created here if missing) and is itself tightened to 0600,
    mirroring ``mcp_gateway/transport`` conventions. Returns the bound path,
    or ``None`` when the transport is unavailable.
    """
    if platform_compat.IS_WINDOWS:
        return None
    try:
        path = dashboard_socket_path(port)
        # Offloaded: directory creation, the stale-socket stat/unlink, and the
        # post-bind chmod are blocking fs I/O (no-blocking-call-on-event-loop).
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            subprocess_executor(), platform_compat.make_owner_only_dir, path.parent
        )
        await loop.run_in_executor(subprocess_executor(), _remove_stale_unix_socket, path)
        unix_site = web.UnixSite(runner, str(path))
        await unix_site.start()
        await loop.run_in_executor(subprocess_executor(), chmod_socket_0600, path)
        logger.info("dashboard internal API also listening on unix socket %s", path)
        return path
    except Exception as exc:
        logger.warning("dashboard unix socket unavailable (%s); internal API stays TCP-only", exc)
        return None


def _register_unix_socket_cleanup(app: web.Application, holder: dict[str, Path | None]) -> None:
    """Register best-effort removal of the unix socket file at shutdown.

    Registered BEFORE ``runner.setup()`` freezes the app's signal lists; the
    socket path only becomes known after the site starts, so it is read from
    *holder* lazily. aiohttp does not unlink a ``UnixSite``'s socket file on
    stop, and while startup self-heals a stale file, a clean shutdown should
    not leave one for clients to trip over (each stale connect costs the
    client a refused-connect before its TCP fallback).
    """

    async def _unlink_unix_socket(app_: web.Application) -> None:
        path = holder.get("path")
        if path is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), _remove_stale_unix_socket, path
            )
        except Exception:  # pragma: no cover — cleanup must never break shutdown
            logger.debug("dashboard unix socket cleanup failed", exc_info=True)

    app.on_cleanup.append(_unlink_unix_socket)


def _live_sibling_port(own_port: int) -> int | None:
    """A DIFFERENT port in this data home whose gateway is verifiably alive.

    ``None`` when this start is the only live gateway in the home, which is the
    normal single-instance case. Uses the same ownership proof the client port
    discovery already trusts (recorded pid, actually holds the port, same uid,
    argv looks like a gateway), so a stale marker left by a crash does not count
    as a sibling and never blocks a legitimate credential write.

    Blocking (/proc + filesystem); call from the executor, never the loop.
    """
    try:
        for port in run_marker.marker_ports():
            if int(port) == int(own_port):
                continue
            if port_resolution._gateway_owns_port(int(port)):
                return int(port)
    except Exception:
        # Discovery failing must not block startup: fall through to the write.
        # A missed sibling degrades to the pre-existing last-writer-wins
        # behaviour, never to a gateway that cannot start.
        logger.debug("live-sibling discovery failed", exc_info=True)
    return None


def _write_instance_credentials(secret_path: Path, port: int, secret: str) -> None:
    """Publish this gateway's internal-API credential.

    Writes two files with different lifetimes:

    * ``run/gateway-<port>.secret`` -- ALWAYS. Paired with the listener, so a
      client that resolved a port reads the credential of the process that owns
      that port rather than whichever gateway wrote the shared file last.
    * ``.local_secret`` -- only when no other gateway in this data home is
      verifiably alive on a different port. Overwriting it while a sibling is
      serving is the desync this guard exists to prevent: the sibling keeps
      comparing against its own in-memory value, every internal caller then
      sends the newcomer's credential, and the whole internal channel answers
      403 with a bare ``Forbidden`` until one of them restarts. The shared file
      is still written in the single-instance case because pre-per-port clients
      (an older CLI, a cron script from a previous install) read only that path.

    Blocking fs I/O; the caller offloads this whole function.
    """
    _write_secret_file(run_marker.secret_path(int(port)), secret)
    sibling = _live_sibling_port(int(port))
    if sibling is not None:
        logger.warning(
            "Not overwriting %s: another gateway in this data home is live on port %d. "
            "This instance's credential is published as %s; clients that resolve port %d "
            "will authenticate against it.",
            secret_path,
            sibling,
            run_marker.secret_path(int(port)).name,
            port,
        )
        return
    _write_secret_file(secret_path, secret)


def _write_secret_file(secret_path: Path, secret: str) -> None:
    """Write *secret* to *secret_path* with mode 0o600.

    Creates the parent directory if needed. On failure the (possibly
    truncated) file is removed and the original ``OSError`` is re-raised.
    Caller is responsible for any further cleanup (e.g. tearing down the app
    runner). Both blocking steps (``mkdir`` and the ``os.open``/``os.close`` +
    ``restrict_to_owner`` write) live here so the caller can offload the whole
    thing with a single ``run_in_executor`` (no-blocking-call-on-event-loop).
    """
    try:
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(secret_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            # Enforce perms even if the file already exists at looser mode.
            # restrict_to_owner (fail-loud), NOT fchmod_safe: fchmod_safe swallows
            # OSError, which would defeat the cleanup-and-reraise below — a
            # pre-existing file with loose perms would stay loose and the caller
            # never learns. On POSIX this applies chmod 0o600 by path;
            # on Windows an owner-only DACL (fchmod doesn't exist on
            # Windows, where a raw fchmod would be a silent no-op).
            platform_compat.restrict_to_owner(secret_path)
            with os.fdopen(fd, "w") as f:
                fd = -1  # fdopen took ownership; skip the redundant close below
                f.write(secret)
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
    except OSError:
        try:
            secret_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _claimed_dashboard_slots(state: DashboardState) -> frozenset[str]:
    """Slot names the persisted session map holds a ``dashboard:`` session for.

    Read off the live map so the transcript migration can tell a real dashboard
    session from an orphan of a same-named channel session. Blocking (reads the
    map file), so callers on the event loop must offload it.
    """
    try:
        sessions = getattr(state, "sessions", None)
        smap = getattr(sessions, "_session_map", None)
        data = getattr(smap, "_data", None)
        if not isinstance(data, dict):
            return frozenset()
        return frozenset(k[len("dashboard:") :] for k in data if k.startswith("dashboard:"))
    except Exception:
        logger.debug("could not read claimed dashboard slots", exc_info=True)
        return frozenset()


def _take_prior_dropped_grant() -> Any:
    """Consume the PREVIOUS process's safety-override record, if any.

    Run off the event loop (the caller wraps it in ``asyncio.to_thread``): it is a
    file open on a filesystem that may be slow, and nothing about boot should wait
    on it. Ordering against ``_apply_startup_yolo`` does not matter, because the
    record carries the writing pid and this process's own record is never read as
    a dropped one. Never raises: the gateway must not fail to boot over a
    notification, and the grant is off either way.
    """
    try:
        return take_dropped_grant()
    except Exception:
        logger.debug("Could not read the prior safety-override record", exc_info=True)
        return None


def _apply_startup_yolo(state: DashboardState, cfg: Any) -> None:
    """Enable the safety override at startup if the operator declared it.

    ``agent.dangerouslySkipPermissions`` is a STANDING operator instruction, so the grant it creates
    does not expire — it used to lapse after 24h and silently drop the user back
    to prompt-for-everything, which breaks flows driven from Slack/Discord and
    from cron where nobody is watching the dashboard to re-enable it.

    State is in-memory, so the grant is re-established and re-audited on every
    startup rather than persisted. An enterprise policy can forbid a
    never-expiring grant (the ``yolo_duration`` governance scope), in which case
    it falls back to the ad-hoc duration. Picking another approval mode still
    clears it immediately.

    Ad-hoc grants are untouched: Slack, the dashboard picker and the API all
    expire on the single ``agent.yolo_duration`` value (default 6h).
    """
    # Seed the ad-hoc TTL even when yolo is off, so a later dashboard/Slack
    # activation uses the configured duration rather than the built-in default.
    try:
        apply_config_duration()
    except Exception:
        logger.warning("Could not apply the configured YOLO duration", exc_info=True)

    if not cfg.agent.dangerously_skip_permissions:
        return
    try:
        result = grant_declared_yolo()
    except Exception:
        logger.error("Failed to activate safety override from config", exc_info=True)
        return
    if not result.active:
        logger.error("Safety override activation refused (SEL audit failure?)")
        return
    logger.info(
        "Safety override enabled at startup (dangerouslySkipPermissions=true, %s)",
        "no expiry" if result.ttl == 0 else f"expires in {result.ttl}s per policy",
    )


async def _revive_intended_instances(
    registry: InstancesRegistry, manager: SshTunnelManager
) -> None:
    """Auto-reconnect every instance the operator left connected.

    ``was_connected`` is the sticky "connection intent" (set on connect, cleared
    only on explicit disconnect) — so on startup it names exactly the instances
    that had open tunnels when the gateway last stopped. We revive all of them
    so their tabs come back live, rather than reviving only the single
    last-active one (which left every other tab dead until a manual reconnect).

    Instances are revived one at a time so they don't race to bind their
    (mirrored) ports, and each attempt is wrapped so one unreachable host can
    neither abort the rest nor crash startup. A failed revive leaves
    ``was_connected`` true (the connect path never clears it on failure) and
    records a retained error, so its tab persists showing *why* it is down — the
    user re-authenticates in their own environment (SSH agent / SSO /
    whatever the host needs) and clicks Retry from the instance page. We do NOT
    pre-gate on any credential-staleness check: a failed connect simply surfaces
    its error, which is exactly the recovery affordance we want.

    Extracted to module level (rather than an inline closure) so the revive
    policy — which instances are picked and the per-instance failure isolation —
    is unit-testable without standing up the whole app.
    """
    intended = [inst for inst in registry.list() if inst.was_connected]
    if not intended:
        return
    logger.info("Auto-reconnecting %d instance(s) on startup", len(intended))
    for inst in intended:
        try:
            st = await manager.connect(inst.id)
            if st.state == TunnelState.CONNECTED:
                logger.info("Auto-reconnected instance %s", inst.id)
            else:
                logger.warning(
                    "Startup auto-reconnect of %s did not connect (%s): %s",
                    inst.id,
                    st.state.value,
                    st.error,
                )
        except Exception:
            logger.warning("Startup auto-reconnect of %s failed", inst.id, exc_info=True)


def _armed_unattended_loops() -> "list[Any]":
    """Nudge loops still marked active, for the expiry notice only.

    Deliberately a plain ``active`` read rather than a careful liveness test: this
    decides whether to TELL someone, and a false positive costs one redundant
    notice. Nothing is granted on the strength of it, so there is no reason to pay
    for a stop-sentinel stat or to re-derive the loop's bounds — and this runs on
    the event loop, reached from tool-approval paths.
    """
    try:
        svc = _autonudge_get()
        if svc is None:
            return []
        return [lp for lp in svc.list_all() if getattr(lp, "active", False)]
    except Exception:
        logger.debug("could not enumerate nudge loops for the expiry notice", exc_info=True)
        return []


_UNATTENDED_EXPIRY_TITLE = "🔒 Auto-approve expired while an unattended run was in progress"


def _unattended_expiry_text(loop_count: int) -> str:
    """Body shared by the dashboard note and the owner DM, so the two cannot drift.

    Names the remedy as well as the cause: ``agent.yolo_duration`` accepts
    ``until_shutdown``, which has no timed expiry. The cheapest half of this
    problem is that operators do not know that option exists, and the moment it
    would have helped is the moment worth saying so.

    The stall is stated conditionally because global auto-approve is not the only
    path to one: a slot carrying its own trust grant is approved by ``slot._trust``
    independently of the grant, so its cycles keep running after this expiry.
    Claiming the run has stopped would send an operator to rescue a healthy one.
    """
    return (
        f"{loop_count} monitor loop(s) are still running, but auto-approval has "
        f"ended, so any cycle that relied on it now waits on a per-tool approval "
        f"that nobody is there to give. (A session granted its own trust is "
        f"unaffected.) Re-enable auto-approve to resume. For runs meant to go "
        f"unattended overnight, Settings → agent.yolo_duration has an "
        f"'until_shutdown' option that has no timed expiry."
    )


def _notify_unattended_expiry(state: "DashboardState", source: str) -> None:
    """Report an expiry that landed on an unattended run, on BOTH surfaces.

    An ordinary expiry degrades gracefully — the next tool call asks a human, and
    a human is there to answer. This one degrades into nothing: the loop keeps
    waking, dispatches a tool, waits out the approval window with nobody present,
    and accomplishes no work until someone notices.

    Delivered to the dashboard feed AND pushed to the owner's DM, because the
    operator this exists for is by definition not looking at a dashboard. Neither
    delivery is gated behind ``agent.notify_override_expiry``: that switch silences
    a recurring *expiry* notice, while this says a run in flight stopped being able
    to work — a different and stronger fact, and one an operator who muted the
    former did not ask to be uninformed about.
    """
    armed = _armed_unattended_loops()
    if not armed:
        return
    logger.warning(
        "Safety override expired with %d unattended loop(s) still running; "
        "every further cycle will wait on per-tool approval",
        len(armed),
    )
    body = _unattended_expiry_text(len(armed))
    try:
        state.notify(
            "safety_override",
            _UNATTENDED_EXPIRY_TITLE,
            body,
            meta={"loops": len(armed), "source": source},
        )
    except Exception:
        # ERROR, not debug: this notice is the only operator-visible trace that an
        # unattended run stopped working rather than finished. Losing it silently
        # reproduces the failure it exists to explain.
        logger.error("unattended-expiry notification failed", exc_info=True)

    # The push half. Scheduled directly rather than through
    # _dispatch_override_expiry_notification, which applies the recurring-expiry
    # mute this notice deliberately does not inherit.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("no running event loop — unattended-expiry DM skipped")
        return
    task = loop.create_task(_dm_owner(state, f"{_UNATTENDED_EXPIRY_TITLE}\n\n{body}"))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


def _dispatch_override_expiry_notification(state: DashboardState, notify_coro_factory: Any) -> bool:
    """Schedule the Slack override-expiry DM unless disabled via config.

    Gated by ``agent.notify_override_expiry`` (read live so it can be toggled
    without a restart). Returns True if a notification task was scheduled, False
    if skipped — either disabled via config or no running event loop.
    """
    if not KiroCrewConfig.load().agent.notify_override_expiry:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("No running event loop — Slack expiry notification skipped")
        return False
    task = loop.create_task(notify_coro_factory())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return True


async def _dm_owner(state: DashboardState, text: str) -> None:
    """Best-effort owner notification, Slack first then any live channel.

    The shared owner-notification exit point (currently the
    safety-override-expiry path), so the open_dm → post_message →
    swallow-and-log idiom lives in one place.

    **Slack is not the only place an operator lives.** This used to no-op entirely
    without Slack, which made an expiring unattended grant invisible on a
    Teams-only, Discord-only or Telegram-only install — silence about a security
    grant lapsing is the one outcome this notice exists to prevent. So a Slack DM
    is still preferred (it is the owner's direct address), and every registered
    channel transport that advertises a reachable configured target is used as the
    FALLBACK when Slack is absent or could not deliver. Not in addition: an
    operator with Slack should get one notice, not one per channel.

    Defense-in-depth: because this is the single exit point for owner
    notifications and is intended for reuse, ``text`` is passed through
    ``redact_exfiltration_urls()`` then ``redact_credentials()`` (same order as
    the rest of the Slack surface) so a future caller that forwards
    LLM/user-derived content can never leak credentials or exfil URLs, even
    though today's callers only pass static constants.
    """
    safe_text, _ = redact_exfiltration_urls(text)
    safe_text, _ = redact_credentials(safe_text)
    slack_client = state.slack_client
    owner_id = state.owner_id
    if slack_client and owner_id:
        try:
            dm_channel = await slack_client.open_dm(owner_id)
            # PLAIN-CLIENT Slack egress: no governance gate, no per-chunk re-ask. The
            # hardened chain is `dashboard.slack_egress`; adopting it widens refusals.
            await slack_client.post_message(dm_channel, safe_text)
            return
        except Exception:
            logger.debug("Owner Slack DM failed; trying the channel transports", exc_info=True)
    await _notify_owner_channels(state, safe_text)


async def _notify_owner_channels(state: DashboardState, safe_text: str) -> None:
    """Deliver an already-redacted owner notice to a channel that can NAME the owner.

    "Reachable" is the transport's OWN answer (`configured_targets` →
    `resolve_configured_target`), so this reaches only destinations that channel
    already authorized — a Teams DM whose route was learned from an allow-listed
    sender, never an address chosen here. Each channel is independent: one that
    cannot deliver must not stop the next.

    **Exactly one candidate across EVERY channel, or nothing.** This notice carries the
    operator's own security state — an expiring unattended auto-approve grant, for
    instance — and there is no channel-neutral owner identity to check it against: Slack
    has an owner id and is preferred above; nothing else does. An allow-list is a list of
    people permitted to TALK to the agent, not a claim that any of them is the operator.

    So the only sound inference is a counting one, and it has to be counted across the
    whole install rather than per channel. Two channels each holding a DIFFERENT single
    identity is two people, and delivering to both hands one of them the other's security
    state — a per-channel "exactly one target" rule misses that entirely. With exactly one
    reachable person in the whole configuration, that person is the operator; with two or
    more, refuse everybody. Same premise as `/sessions`' owner-only rule.

    Counted over ALL configured targets, not just the reachable ones: a three-person
    allow-list where only one route happens to have been learned is still a guess.

    The false negative is deliberate and is the safe direction: the same human configured
    on two channels reads as two candidates and gets no channel notice. The dashboard feed
    carries the same notice unconditionally, so silence here costs a convenience, while
    misdelivery would cost the operator's security state. Positively binding a channel
    identity to the operator is a per-identity authority model that does not exist yet;
    when it does, this becomes a lookup instead of a count.
    """
    candidates: list[tuple[str, Any, Any]] = []
    for channel_type, transport in list(state.channel_transports.items()):
        try:
            if not transport.capabilities.supports_proactive_send:
                continue
            candidates.extend(
                (channel_type, transport, target) for target in transport.configured_targets()
            )
        except Exception:
            logger.debug("Owner notice enumeration failed for %s", channel_type, exc_info=True)
    if len(candidates) != 1:
        if candidates:
            logger.debug(
                "Owner notice skipped: %d channel targets, none positively the owner",
                len(candidates),
            )
        return
    channel_type, transport, target = candidates[0]
    if not target.available:
        return
    try:
        resolved = await transport.resolve_configured_target(target.target_id)
        if not resolved:
            return
        conversation_id, thread_id = resolved
        await transport.send_message(conversation_id, safe_text, thread_id)
    except Exception:
        logger.debug("Owner notice skipped for %s", channel_type, exc_info=True)


def _dispatch_owner_dm(state: DashboardState, text: str) -> None:
    """Fire-and-forget an owner DM without blocking the caller.

    Schedules :func:`_dm_owner` as a tracked background task so a slow or
    unreachable Slack API never stalls the startup / hot path. No-op if there
    is no running loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("No running event loop — owner DM skipped")
        return
    task = loop.create_task(_dm_owner(state, text))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


def _register_connections_warm_lifecycle(app: web.Application, state: DashboardState) -> None:
    """Retire warm generations on cleanup; startup scavenging is kicked post-bind.

    The import sits inside the hook deliberately, against ``top-level-imports``, because
    ``no-new-work-on-gateway-boot-path`` governs this file and wins: importing
    ``connections.warm`` at module scope would pull its whole dependency graph -- the mint
    table, the provider registry, tool aliases, MCP discovery -- onto the one ordered thread
    between process start and the socket accepting requests. Startup scavenging is NOT an
    ``on_startup`` hook for the same reason: aiohttp runs those inside ``runner.setup()``,
    BEFORE the listener binds, so even a hook that only created the scavenge task put the
    synchronous import in front of the bind. Both entrypoints instead call
    ``_kick_connections_warm_scavenge`` strictly after ``_start_site`` returns.

    The cleanup hook is registered here, before ``runner.setup()`` freezes aiohttp's signal
    lists; it resolves the import only when a gateway is already stopping.
    """

    async def _connections_warm_shutdown(_app: web.Application) -> None:
        try:
            from kiro_crew.connections.warm import shutdown_warm_mint

            await shutdown_warm_mint()
        except Exception:  # noqa: BLE001 — one cleanup hook must not suppress later hooks
            logger.warning("Connections warm shutdown failed", exc_info=True)

    app.on_cleanup.append(_connections_warm_shutdown)


def _kick_connections_warm_scavenge(state: DashboardState) -> None:
    """Start the crash-residue scavenge as a tracked background task, post-bind.

    Called by both gateway entrypoints only after ``_start_site`` has returned, so the
    listener is already accepting requests. The deferred ``connections.warm`` import
    happens INSIDE the worker thread: resolving that dependency graph on the event loop
    would stall in-flight requests just as it would have stalled the bind.
    """

    def _scavenge_in_thread() -> None:
        from kiro_crew.connections.warm import scavenge_warm_mint_artifacts

        scavenge_warm_mint_artifacts()

    async def _connections_warm_scavenge() -> None:
        try:
            await asyncio.to_thread(_scavenge_in_thread)
        except Exception:  # noqa: BLE001 — fail closed by retaining unproved residue
            logger.warning("Connections warm artifact scavenging failed", exc_info=True)

    task = asyncio.create_task(_connections_warm_scavenge())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


def _register_browser_view_cleanup(app: web.Application) -> None:
    """Stop the CLI dashboard process when the gateway shuts down.

    `playwright-cli show` is spawned in its OWN session (``start_new_session``) so
    a browsing view outlives the request that started it. That same detachment
    means an ordinary restart would leave it running while the new gateway loses
    its pid, and the next view request starts a SECOND process tree. Stopping it
    on cleanup makes a restart idempotent.

    Registered BEFORE ``runner.setup()`` freezes the app's signal lists --
    appending later raises ``RuntimeError: Cannot modify frozen list``, which is
    exactly what a first attempt at this hook did.

    Best-effort: a failure to reap a supervised child must never block shutdown.
    """

    async def _browser_view_shutdown(app_: web.Application) -> None:
        try:
            await asyncio.to_thread(browser_cli_view.stop)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("browser view stop failed during shutdown", exc_info=True)

    app.on_cleanup.append(_browser_view_shutdown)


def _register_instances_hooks(app: web.Application, state: DashboardState, port: int) -> None:
    """Register the opt-in Instances (multi-instance) startup/cleanup hooks.

    These MUST be attached before ``runner.setup()`` freezes the app's
    ``on_startup`` / ``on_cleanup`` signal lists. Appending after setup raises
    ``RuntimeError: Cannot modify frozen list`` AND the ``on_startup`` signal
    would have already fired, so a hook added late would never run.

    The registry + SSH tunnel manager are created lazily inside the startup
    hook (which fires during ``runner.setup()``), gated on ``instances.enabled``
    (default off). We then auto-reconnect every instance the operator left
    connected (``was_connected``) via :func:`_revive_intended_instances`, which
    isolates per-instance failures so a down host's tab persists in an error
    state instead of vanishing; the user re-authenticates and retries from the
    instance page.
    """

    async def _instances_startup(app_: web.Application) -> None:
        _cfg = KiroCrewConfig.load()
        if not _cfg.instances.enabled:
            return
        registry = InstancesRegistry()
        manager = SshTunnelManager(
            registry,
            base_port=_cfg.instances.tunnel_base_port,
            connect_timeout_secs=_cfg.instances.connect_timeout_secs,
            ssh_compression=_cfg.instances.ssh_compression,
            mint_timeout_secs=_cfg.instances.mint_timeout_secs,
            max_recovery_attempts=_cfg.instances.max_recovery_attempts,
            recover_backoff_max_secs=_cfg.instances.recover_backoff_max_secs,
            probe_failure_threshold=_cfg.instances.probe_failure_threshold,
            # The port this gateway ACTUALLY bound, not the configured guess:
            # it becomes the CSP frame-ancestor claim in every minted remote
            # token, and a claim that disagrees with the parent's real origin
            # makes the browser refuse to frame the remote pane.
            parent_port=port,
        )
        state.instances_registry = registry
        state.instances_manager = manager
        # First-party cookies: embedded instances load from
        # http://127.0.0.1:<port>, so the hub itself should be reached at
        # http://127.0.0.1:<port> (NOT localhost / kirocrew.localhost) — mixing
        # hosts makes the iframes render logged-out. The dashboard already binds
        # 127.0.0.1; we recommend (not force) the loopback-IP URL here so the
        # existing localhost / Slack-link flows are left untouched.
        logger.info(
            "Instances enabled — open the dashboard at http://127.0.0.1:%d for "
            "embedded instances to share first-party cookies.",
            port,
        )
        # Auto-reconnect intended instances in the BACKGROUND rather than
        # awaiting here. on_startup handlers fire during runner.setup(), BEFORE
        # site.start() binds the HTTP port, so awaiting serial SSH-tunnel
        # connects — each of which can hang for its full timeout when the
        # network/DNS is down — delayed the port bind well past the desktop
        # app's 30s gateway-wait window, producing a spurious "Retry/Quit"
        # dialog and relaunch loop. Firing it as a tracked background task lets
        # the port bind immediately; tunnels reconnect (or surface their error
        # on the instance tab, which persists on failure) without gating
        # startup.
        revive_task = asyncio.create_task(_revive_intended_instances(registry, manager))
        state._background_tasks.add(revive_task)
        revive_task.add_done_callback(state._background_tasks.discard)

    async def _instances_shutdown(app_: web.Application) -> None:
        manager = getattr(state, "instances_manager", None)
        if manager is not None:
            await manager.shutdown()

    app.on_startup.append(_instances_startup)
    app.on_cleanup.append(_instances_shutdown)


def build_host_canonical_redirect(canonical_host: str) -> Any:
    """Build the loopback-host-canonicalization middleware.

    Converges non-canonical loopback aliases (127.0.0.1 / ::1 / localhost) onto
    *canonical_host* with a 302 so the SPA's per-origin localStorage settings
    are not split across hostnames. Only top-level document GET/HEAD navigations
    are redirected (see :func:`should_canonicalize_host`); APIs, WebSockets, and
    sub-resource fetches are untouched. Pass ``canonical_host=""`` (e.g. when not
    local_only) to make the middleware a no-op so reverse-proxy / remote-host
    deployments are never redirected.

    Extracted to a module-level factory (rather than an inline closure) so the
    runtime behavior — the 302, port+path+``?token=`` preservation, and the
    gating — is unit-testable.
    """

    @web.middleware  # type: ignore[misc]
    async def host_canonical_redirect(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        if canonical_host and should_canonicalize_host(
            request.host,
            canonical_host,
            method=request.method,
            sec_fetch_dest=request.headers.get("Sec-Fetch-Dest"),
        ):
            # Preserve port + path + query (including ?token=) — only host changes.
            raise web.HTTPFound(location=str(request.url.with_host(canonical_host)))
        return await handler(request)  # type: ignore[operator]

    return host_canonical_redirect


def _wire_status_delta_sink(app: web.Application, state: DashboardState) -> None:
    """Register the PR status-delta sink and its shutdown cleanup on ``app``.

    Registered once at wiring time (rather than per WS connect) so the sink set
    holds exactly one entry per process; ``push_source_status`` no-ops while no
    owner socket is open. The matching ``on_cleanup`` hook is REQUIRED: the sink
    set is module-global and outlives any single ``DashboardState``, so without
    it, starting/stopping/restarting a dashboard in one process retains every old
    state's bound method — a slow leak plus duplicate dispatch to dead states on
    every later status change.
    """
    register_status_delta_sink(state.push_source_status)

    async def _status_sink_shutdown(_app: web.Application) -> None:
        unregister_status_delta_sink(state.push_source_status)

    app.on_cleanup.append(_status_sink_shutdown)


def _wire_tunnel_shutdown(app: web.Application, state: DashboardState) -> None:
    """Register the tunnel teardown hook on ``app``'s shutdown path.

    Without this the tunnel is started (``tunnel/setup.py`` → ``TunnelManager.start()``)
    and then NEVER stopped: ``TunnelManager.stop()`` had no production caller, so
    whatever the active ``TunnelProvider`` brought up outlived the gateway — even
    on a clean Ctrl+C. A companion provider that supervises a child process
    leaked it (reparented to PID 1) and the next gateway start collided on the
    same tunnel name. The manager is edition-neutral, so stopping it here tears
    down EVERY provider (the public Default's ``stop()`` is a no-op).

    Registered like the other long-lived subsystems (``_watchdog_shutdown``,
    ``_register_instances_hooks``): the hook is appended BEFORE ``runner.setup()``
    freezes the app's signal lists, and reads ``state.tunnel_manager`` lazily —
    the manager is only assigned later, after ``setup_tunnel`` runs, and this
    hook fires at shutdown, long after that assignment. That lazy read is also
    what lets the REGISTRATION sit first in ``start_dashboard``: ``on_cleanup``
    handlers are dispatched in registration order under a hard shutdown
    deadline, so a tunnel hook queued behind the other subsystems can be starved
    (instances cleanup waiting on SSH children that ignore SIGTERM eats the
    deadline, the gateway force-exits, and the tunnel is never stopped).

    Two teardown paths, because a live tunnel does not imply a manager:
    ``setup_tunnel`` builds a ``TunnelManager`` and the hook stops that, but the
    on-demand link path (``slack.use_tunnel_url`` →
    ``current_context().tunnel.ensure_available()`` in ``slack/allowlist.py``)
    provisions and starts a tunnel straight on the provider and never constructs
    a manager. With ``state.tunnel_manager`` still None, bailing out left exactly
    the orphan this hook exists to prevent, so the no-manager path stops
    ``current_context().tunnel`` directly. Only one path runs per shutdown — the
    manager delegates to the same provider — so nothing is stopped twice.

    Failure containment: ``on_cleanup`` handlers run in sequence and a raise
    aborts the remaining ones, so a tunnel teardown must never propagate. BOTH
    paths go through ``_stop_bounded``: the stop is bounded by
    ``_TUNNEL_STOP_TIMEOUT_SECS`` and every exception is logged and swallowed, so
    neither a hanging nor a raising provider — nor a fail-closed
    ``current_context()`` — can block or crash the rest of gateway shutdown.
    ``TunnelManager.stop()`` is itself idempotent (it re-delegates and, on
    failure, simply declines to pin STOPPED) and a provider ``stop()`` is
    expected to be too, so a shutdown path that runs twice is harmless on either
    path.
    """

    async def _stop_bounded(stop: Callable[[], Awaitable[None]], what: str) -> None:
        """Await *stop* under the shared bound, logging and swallowing everything.

        *stop* is INVOKED inside the guard, so a synchronous raise — including a
        fail-closed ``current_context()`` lookup — is contained as well.
        """
        try:
            await asyncio.wait_for(stop(), timeout=_TUNNEL_STOP_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            logger.warning(
                "%s did not finish within %.0fs — continuing shutdown",
                what,
                _TUNNEL_STOP_TIMEOUT_SECS,
            )
        except Exception:
            logger.warning("%s failed during shutdown", what, exc_info=True)

    async def _tunnel_shutdown(_app: web.Application) -> None:
        mgr = getattr(state, "tunnel_manager", None)
        if mgr is not None:
            await _stop_bounded(mgr.stop, "Tunnel stop")
            return
        # No manager, but the provider may still own a running tunnel (the
        # on-demand ``ensure_available()`` path never builds one).
        await _stop_bounded(lambda: current_context().tunnel.stop(), "Tunnel provider stop")

    app.on_cleanup.append(_tunnel_shutdown)


def _register_prevent_sleep_shutdown(app: web.Application, state: DashboardState) -> None:
    """Register the on_cleanup hook that cancels the prevent-sleep poll and
    releases the OS block.

    MUST be called BEFORE ``runner.setup()`` freezes the app's signal lists. The
    inhibitor and task are created after setup (by :func:`_arm_prevent_sleep_poll`)
    and resolved here lazily via ``getattr``. Shared by both ``start_dashboard``
    and the headless ``start_api_server`` (``--slack-only``) so a graceful stop
    never leaves caffeinate / systemd-inhibit / the Windows execution-state
    request dangling, in either mode.
    """

    async def _prevent_sleep_shutdown(app_: web.Application) -> None:
        task = getattr(state, "_prevent_sleep_task", None)
        if task is not None:
            task.cancel()
        inhibitor = getattr(state, "_sleep_inhibitor", None)
        if inhibitor is not None:
            try:
                inhibitor.set_active(False)
            except Exception:
                logger.debug("prevent-sleep release on shutdown failed", exc_info=True)

    app.on_cleanup.append(_prevent_sleep_shutdown)


def _import_stt_engine() -> Any:
    """Import the recogniser module. BLOCKING: 169 ms cold, numpy plus the binding.

    A named module-level function rather than a closure so the call is observable: the
    invariant a test has to pin is *which thread* this runs on, and there is no other
    seam on an `import` statement.
    """
    from kiro_crew.stt import engine

    return engine


async def _stt_idle_sweep() -> None:
    """Release the resident speech model once it has been idle past its window.

    `WhisperEngine.maybe_evict` also runs on the paths that finish a decode, and that
    call can never fire on its own: it runs microseconds after ``_last_used`` was
    stamped. Idleness is by definition a stretch in which none of those paths run, so
    noticing it needs something that runs anyway.

    Two costs are kept off the gateway's loop, and they are separate problems with
    separate fixes:

    * The boot delay keeps the import out of ``runner.setup()``, which runs before
      either socket binds. Importing there delays the moment the dashboard answers,
      for a janitor whose first useful pass is minutes away.
    * `asyncio.to_thread` keeps the import off the LOOP. Sleeping first moved it out
      of boot but left it running inline on the event loop, where a measured 169 ms
      (numpy plus the recogniser binding) stalls every socket and heartbeat the
      gateway is serving at that moment.
    """
    await asyncio.sleep(_STT_SWEEP_BOOT_DELAY_SECS)
    engine = await asyncio.to_thread(_import_stt_engine)
    await engine.idle_sweep_loop()


def _register_stt_hooks(app: web.Application) -> None:
    """Register the STT idle sweep and the model release, for both server modes.

    MUST be called BEFORE ``runner.setup()`` freezes the app's signal lists. Shared by
    ``start_dashboard`` and the headless ``start_api_server`` rather than written out
    in each: the two copies were identical, and an event-loop-blocking import in them
    therefore had to be found and fixed twice.
    """

    async def _stt_startup(app_: web.Application) -> None:
        task = asyncio.create_task(_stt_idle_sweep())
        task.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
        app_["stt_idle_sweep"] = task  # prevent GC

    async def _stt_shutdown(app_: web.Application) -> None:
        sweep = app_.get("stt_idle_sweep")
        if sweep is not None:
            sweep.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep
        # Gated on the engine module having been imported AT ALL, which is the cheap
        # and exact test for "could a model be resident". `stt.close()` resolves
        # through `stt.session`, which imports numpy at module scope and whose
        # `shared_engine()` CREATES an engine if none exists -- so on a gateway that
        # never transcribed anything, closing pulled the recogniser binding and built
        # a WhisperEngine at shutdown purely to release nothing.
        if "kiro_crew.stt.engine" not in sys.modules:
            return
        from kiro_crew import stt

        await stt.close()

    app.on_startup.append(_stt_startup)
    app.on_cleanup.append(_stt_shutdown)


def _arm_prevent_sleep_poll(state: DashboardState, port: int) -> None:
    """Create the sleep inhibitor and start its poll task on the running loop.

    *port* is the port this server actually bound, needed because one of the two
    awake reasons is "``tailscale serve`` is fronting this dashboard" — a question
    that can only be asked about a specific port. It is the bound port rather than
    the configured one for the same reason ``kirocrew tailnet up`` insists on
    evidence: if the configured port was occupied the gateway moved, and asking
    about the wrong port would report someone else's serve mapping as ours.

    Keeps the host awake while any session has a turn in flight, but only when
    the user opted in via ``dashboard.prevent_sleep``. Decoupled from the turn
    paths on purpose: polling the same active-turn signal the shutdown drain
    filters on covers every surface (dashboard, Slack, CLI, task runner, and
    sub-agents running under a parent turn) without threading acquire/release
    through each path.

    MUST be called AFTER ``runner.setup()`` (it needs a running loop), and paired
    with :func:`_register_prevent_sleep_shutdown` (registered before setup) for
    release. Shared by both server entrypoints so headless ``--slack-only`` mode
    keeps the host awake identically to the full dashboard — a long Slack task
    on a laptop is the case this feature exists for.
    """
    inhibitor = SleepInhibitor()
    state._sleep_inhibitor = inhibitor  # prevent GC; released on cleanup

    async def _prevent_sleep_poll() -> None:
        try:
            while True:
                await asyncio.sleep(_PREVENT_SLEEP_POLL_INTERVAL_SECS)
                try:
                    inhibitor.set_active(await _should_prevent_sleep(state, port))
                except Exception:
                    logger.debug("prevent-sleep poll toggle failed", exc_info=True)
        except asyncio.CancelledError:
            # Release the OS block before propagating so a cancel (shutdown)
            # never leaves the machine unable to sleep.
            inhibitor.set_active(False)
            raise

    def _prevent_sleep_done(task: "asyncio.Task") -> None:  # type: ignore[type-arg]
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("prevent-sleep poll task exited unexpectedly", exc_info=exc)

    task = asyncio.create_task(_prevent_sleep_poll())
    task.add_done_callback(_prevent_sleep_done)
    state._prevent_sleep_task = task  # prevent GC; cancelled on cleanup


# Deep link at the approval toggle itself (Settings -> Skills, highlighted), so
# the notification can offer the opt-out at the exact moment the user is being
# asked to review yet another candidate. Same highlight=key:<configKey> format
# the frontend's <SettingRef> builds, consumed by useSettingHighlight.
_SKILL_APPROVAL_SETTING_URL = "/settings/skills?highlight=key:skills.approval_required"


def _pending_skill_notification(info: dict) -> tuple[str, str, str, list[dict[str, str]]]:
    """Build the bell-feed payload for a staged skill candidate.

    Returns ``(title, body, review_url, actions)``. Module-level (rather than
    inline in the staged hook) so the notification CONTENT is unit-testable
    without booting the dashboard app.
    """
    name = str(info.get("name") or info.get("slug") or "skill")
    slug = str(info.get("slug") or "")
    is_update = info.get("kind") == "update"
    target = str(info.get("target") or "")
    description = str(info.get("description") or "").strip()
    triggers = str(info.get("triggers") or "").strip()
    subject = target or name if is_update else name
    title = "Skill update awaiting review" if is_update else "New skill awaiting review"
    # The body LEADS with name + description because the feed row
    # renders only its first ~80 characters, stripped to one line.
    # The title already says a skill is awaiting review, so opening
    # with "was generated from a session and needs your approval"
    # spends exactly the characters that decide whether the reader
    # opens the queue on words they have already read. Identity plus
    # purpose first; the approval sentence still follows for the
    # detail panel, which renders the whole body as markdown.
    head = f"**{subject}**"
    if description:
        head += f" — {description}"
    lines = [head]
    lines.append(
        "\nGenerated from a session. Needs your approval before "
        + ("it takes effect." if is_update else "it can be used.")
    )
    if triggers:
        lines.append(f"\n**Triggers:** {triggers}")
    if info.get("has_scripts"):
        lines.append("\n_Bundles executable scripts — review them before approving._")
    body = "\n".join(lines)
    # Deep-link straight at the candidate, not just the tab: the
    # queue can hold several rows, and "go find it" is the failure
    # mode this notification exists to prevent. quote() keeps a slug
    # from opening a second query parameter -- slugs are validated
    # against a restrictive pattern upstream, but the URL is built
    # here and must not depend on that invariant holding.
    review_url = "/capabilities?tab=skills"
    if slug:
        review_url += f"&review={quote(slug, safe='')}"
    actions = [
        {
            "id": "review-skill",
            "label": "Review update" if is_update else "Review skill",
            "url": review_url,
        },
        # The opt-out shortcut: lands on the approval_required toggle in
        # Settings. Offered on every staged candidate — including
        # script-bearing ones, where it still governs FUTURE prose-only
        # skills (scripts always stage; the setting's own description
        # explains that boundary). The label shares the destination
        # toggle's polarity ("Require approval …" is ON; this stops it),
        # and the trailing ellipsis signals that the button NAVIGATES to a
        # settings page rather than flipping the setting itself —
        # notification actions are navigation-only.
        {
            "id": "auto-approve-skills",
            "label": "Stop requiring skill approval…",
            "url": _SKILL_APPROVAL_SETTING_URL,
        },
    ]
    return title, body, review_url, actions


def _tailnet_origin_enabled() -> bool:
    """Read the live recovery opt-in; callers offload this blocking config read."""

    return bool(KiroCrewConfig.load().dashboard.tailscale.enabled)


async def start_dashboard(
    sessions: SessionManager,
    crons: CronService,
    lessons: LessonStore,
    port: int = _DEFAULT_PORT,
    subagents: SubagentManager | None = None,
    context_builder: ContextBuilder | None = None,
    conversation_log: ConversationLog | None = None,
    consolidator: HistoryConsolidator | None = None,
    task_runner: TaskRunner | None = None,
    slack_connected: bool = False,
    local_only: bool = True,
    configured_host: str = "",
    dashboard_url: str = "",
    slack_client: Any = None,
    owner_id: str = "",
    assume_kiro_ready: bool = False,
) -> tuple[web.AppRunner, DashboardState]:
    """Start the dashboard web server.  Returns ``(runner, state)``."""
    # The generated service marker describes this launch, not every process the
    # dashboard may later spawn. Snapshot it before starting app backends or
    # child terminals, then use only that snapshot to choose the watchdog grace.
    _launch_environment = consume_managed_service_launch_environment()

    # Auto-create consolidator if conversation_log available but no consolidator
    if consolidator is None and conversation_log is not None:
        try:
            from kiro_crew import history as _hist_mod
            from kiro_crew.memory import MemoryStore

            memory = context_builder.memory if context_builder else MemoryStore()
            if not context_builder:
                memory.init()
            # Wire the skills loader + config so a dashboard-only launch honors
            # the same auto-skill defaults as the CLI/gateway entry points —
            # otherwise this fallback silently ran with auto-generation disabled,
            # contradicting the on-by-default config.
            if context_builder is not None:
                _skills = context_builder.skills
            else:
                _skills = SkillsLoader(install_builtins=False)
            _scfg = KiroCrewConfig.load().skills
            consolidator = _hist_mod.HistoryConsolidator(
                log=conversation_log,
                memory=memory,
                sessions=sessions,
                lesson_store=lessons,
                skills_loader=_skills,
                auto_skills_enabled=_scfg.auto_create_from_sessions,
                auto_refine_enabled=_scfg.auto_refine_on_deviation,
                auto_min_tool_calls=_scfg.auto_min_tool_calls,
                auto_similarity_threshold=_scfg.auto_similarity_threshold,
                approval_required=_scfg.approval_required,
                max_auto_skills=_scfg.max_auto_skills,
                stale_after_days=_scfg.stale_after_days,
                archive_after_days=_scfg.archive_after_days,
                generate_scripts=_scfg.generate_scripts,
                judge_model=_scfg.judge_model,
            )
            logger.info("Auto-created HistoryConsolidator for dashboard (skills wired)")
        except Exception:
            logger.debug("Could not create consolidator", exc_info=True)

    state = DashboardState(
        sessions=sessions,
        crons=crons,
        lessons=lessons,
        start_time=time.time(),
        subagents=subagents,
        context_builder=context_builder,
        conversation_log=conversation_log,
        consolidator=consolidator,
        task_runner=task_runner,
        slack_client=slack_client,
        owner_id=owner_id,
    )

    # --- Pending-skill approval notifications ---
    # A staged candidate (new OR update) stays invisible until a human approves
    # it, so raise a bell-feed notification with a deep link to the review queue
    # and broadcast ``skills.pending_changed`` so an open Skills tab refreshes
    # live. The hook is registered at MODULE level in ``skills`` because
    # candidates are staged by whichever loader instance the producer holds
    # (consolidation uses the ContextBuilder's; dashboard requests build their
    # own), so a per-instance callback would miss the consolidation path.
    try:
        # Capture the gateway loop: the hook fires from whatever thread staged
        # the candidate, and consolidation stages from a worker thread
        # (``asyncio.to_thread``). Both notify() and broadcast_ws() ultimately
        # call ``asyncio.ensure_future``, which RAISES off-loop — and
        # ``_send_ws_all`` treats that raise as a dead socket and EVICTS every
        # connected client. Marshal the emit back onto the loop instead.
        def _on_pending_skill_staged(info: dict) -> None:
            try:
                slug = str(info.get("slug") or "")
                is_update = info.get("kind") == "update"
                target = str(info.get("target") or "")
                title, body, review_url, actions = _pending_skill_notification(info)
                payload = {
                    "slug": slug,
                    "candidate_kind": "update" if is_update else "new",
                    "target": target,
                }

                def _emit() -> None:
                    try:
                        state.notify(
                            "skills",
                            title,
                            body,
                            meta=payload,
                            url=review_url,
                            actions=actions,
                        )
                        state.broadcast_ws("skills.pending_changed", payload)
                    except Exception:
                        logger.debug("pending-skill notification failed", exc_info=True)

                loop = state.serving_loop
                if loop is not None and not loop.is_closed():
                    # Safe from the loop thread too — call_soon_threadsafe just
                    # schedules. RuntimeError means the loop is shutting down.
                    try:
                        loop.call_soon_threadsafe(_emit)
                    except RuntimeError:  # pragma: no cover - loop closing
                        pass
                else:
                    _emit()
            except Exception:
                logger.debug("pending-skill notification failed", exc_info=True)

        set_pending_staged_hook(_on_pending_skill_staged)

        def _on_pending_skill_consumed(info: dict) -> None:
            # Counterpart of the staged hook above: when a candidate leaves the
            # queue (approved, dismissed, or TTL-pruned — by ANY loader
            # instance), retire its bell notification instead of leaving an
            # unread row whose deep link now lands on the "no longer awaiting
            # review" banner. Same thread contract as staging: the hook fires
            # from whatever thread consumed the candidate (dashboard handlers
            # run it on an executor), so marshal onto the gateway loop before
            # touching the notification log or the WS fanout.
            try:
                slug = str(info.get("slug") or "")
                consumed_at = str(info.get("consumed_at") or "")
                if not slug or not consumed_at:
                    return

                def _resolve() -> None:
                    try:
                        task = asyncio.ensure_future(
                            state.resolve_skill_review_notifications(slug, consumed_at)
                        )
                        state._background_tasks.add(task)
                        task.add_done_callback(state._background_tasks.discard)
                    except Exception:
                        logger.debug("pending-skill notification resolve failed", exc_info=True)

                loop = state.serving_loop
                if loop is not None and not loop.is_closed():
                    try:
                        loop.call_soon_threadsafe(_resolve)
                    except RuntimeError:  # pragma: no cover - loop closing
                        pass
                # Without a loop there is no serving dashboard (sync/embedded
                # launch): no SSE/WS clients to update and no executor to
                # persist through, so the row is left as-is.
            except Exception:
                logger.debug("pending-skill notification resolve failed", exc_info=True)

        set_pending_consumed_hook(_on_pending_skill_consumed)
    except Exception:
        logger.debug("Could not register pending-skill staged hook", exc_info=True)

    # --- Dynamic Workflows ---
    try:
        from kiro_crew.dashboard.handlers import workflows as wf_handlers
        from kiro_crew.dashboard.workflow_inject import inject_workflow_result
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls
        from kiro_crew.workflows.service import WorkflowService

        def _wf_on_event(run_id: str, event_json: dict) -> None:
            try:
                sess = ""
                svc = getattr(state, "workflow_service", None)
                if svc is not None:
                    h = svc.registry.get(run_id)
                    if h is not None:
                        sess = h.session_key
                safe_event = wf_handlers._redact_obj(event_json)
                state.broadcast_ws(
                    "workflow_run_event",
                    {"run_id": run_id, "session_key": sess, **safe_event},
                )
            except Exception:
                logger.debug("workflow on_event broadcast failed", exc_info=True)

        def _wf_on_done(run_id: str, snapshot: dict) -> None:
            def _auto_turn(slot: Any, snap: dict) -> None:
                try:
                    from kiro_crew.dashboard.chat import _run_chat

                    raw_name = snap.get("name") or snap.get("run_id", run_id)
                    name, _ = redact_exfiltration_urls(str(raw_name))
                    name, _ = redact_credentials(name)
                    status, _ = redact_exfiltration_urls(str(snap.get("status", "")))
                    status, _ = redact_credentials(status)
                    prompt = (
                        f"[Workflow `{name}` finished: {status}] Its result was just "
                        "posted above. The user is waiting on the answer to the "
                        "request that prompted this workflow — find that request "
                        "earlier in this conversation and answer it directly. Your "
                        "final message is the only part of this turn the user is "
                        "guaranteed to see, so make it a standalone deliverable: lead "
                        "with the answer, and keep run mechanics (which agents ran, "
                        "what was verified, what is still uncertain) to a short "
                        "closing note or a collapsed fold. If the workflow failed or "
                        "came back incomplete, say that plainly and state what is "
                        "still unknown."
                    )
                    started = slot.enqueue_or_run_prompt(prompt, _run_chat, state)
                    state.push_slots_update()
                    logger.info(
                        "workflow %s result -> chat slot %s: agent turn %s",
                        run_id,
                        getattr(slot, "key", "?"),
                        "started" if started else "queued",
                    )
                except Exception:
                    logger.warning("workflow %s auto-turn failed", run_id, exc_info=True)

            try:
                inject_workflow_result(state, run_id, snapshot, on_injected=_auto_turn)
            except Exception:
                logger.debug("workflow on_done injection failed", exc_info=True)

        # Workflow agent concurrency stays at this fixed cap ON PURPOSE. Sizing it
        # from resolve_max_subagents() looks tempting (it is the sizing authority
        # in mcp_core / slack gateway / context), but the warm pool keeps a
        # SEPARATE sub-pool per agent/model/CWD identity and its own documented
        # aggregate bound is ``(max_identities + 1) * max_workers`` — 9 * this
        # value (see workflows/agent_pool.py). Feeding an auto-sized cap in here
        # would raise the worst-case resident kiro-cli workers from 9*4=36 to
        # 9*subagent_auto_max=288 and OOM the gateway on a large host. Revisit
        # only once the pool enforces ONE aggregate worker limit.
        _wf_concurrency = 4
        # The run ceiling is unaffected by that and IS config-driven.
        _wf_timeout_secs: int | None = None
        try:
            _wf_timeout_secs = int(KiroCrewConfig.load().agent.workflow_run_timeout_secs)
        except Exception:
            logger.debug("workflow run-ceiling config unavailable; using default", exc_info=True)

        async def _wf_nudge_authorizer(
            *, slot_key: str, message: str, idle_secs: int, max_cycles: int
        ) -> str | None:
            """Route a workflow ``ctx.nudge`` through the SHARED authorize/audit
            chokepoint before arming an AutoNudge loop — same ownership/allowlist
            checks, message limit, and SEL audit as ``POST /api/autonudge`` (so a
            caller-influenced session key can't spoof another session's loop).
            Returns the rejection reason (or None on success) so the workflow
            port can surface the outcome in the run's event stream."""
            _loop, error, _status = await authorize_and_add_nudge(
                svc=_autonudge_get(),
                state=state,
                slot_key=slot_key,
                message=message,
                idle_secs=idle_secs,
                max_cycles=max_cycles,
                source="workflow",
            )
            if error is not None:
                logger.info("workflow ctx.nudge not armed for %s: %s", slot_key, error)
            return error

        state.workflow_service = WorkflowService(
            sessions=sessions,
            on_done=_wf_on_done,
            on_event=_wf_on_event,
            now_fn=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            concurrency=_wf_concurrency,
            nudge_authorizer=_wf_nudge_authorizer,
            timeout_secs=_wf_timeout_secs,
        )
        if state.task_runner is not None:
            state.workflow_service.attach_task_runner(state.task_runner)
            state.task_runner.attach_workflow_service(state.workflow_service)
        logger.info(
            "WorkflowService ready (dynamic workflows, max parallel agents=%s, run ceiling=%ss)",
            _wf_concurrency,
            state.workflow_service.timeout_secs,
        )
    except Exception:
        state.workflow_service = None
        logger.warning("WorkflowService unavailable", exc_info=True)

    # Initialize script hook store
    state._hook_store = ScriptHookStore()
    set_global_hook_store(state._hook_store)

    # Credit the skill-usage ledger for skill bodies the model reads directly
    # (a file-read tool or `cat`), which bypass the loader entirely.
    register_skill_read_observer(state.context_builder)

    # Wire script hooks into subagent tool execution path
    if state.subagents is not None:
        state.subagents.hook_store = state._hook_store

    # Visible notice + pct reset when auto-compaction fires on a dashboard session
    state.wire_session_compact_callback()
    # Visible notice when the watchdog recycles a dashboard session (e.g. RSS)
    state.wire_session_recycle_callback()
    # Visible notice in a channel that just lost its session-resume binding
    state.wire_session_unbind_listener()

    app = web.Application(
        client_max_size=60 * 1024 * 1024
    )  # 60 MB: covers a 50 MB BUFFERED upload + multipart overhead. NOT a
    # ceiling on every upload: aiohttp enforces this in Request.read()/.post(),
    # not on the streaming multipart() reader, so the video path in
    # handlers/files.py streams past it under its own _MAX_VIDEO_UPLOAD_BYTES
    # (pinned by test_streaming_bypasses_the_app_client_max_size). Reading this
    # number as a global request cap is the false invariant to avoid.
    app["state"] = state
    # Bind the serving loop once, here: this runs ON that loop, so every
    # surface that later hands work in from a foreign thread -- slots
    # coalescing, an off-loop websocket send, the log handler's fan-out --
    # resolves the same loop instead of each latching its own copy from
    # whichever thread happens to arrive first.
    state.bind_serving_loop(asyncio.get_running_loop())
    # Voice settings live in slack/handler's module state and are otherwise
    # loaded only on the Slack startup path (set_orch_cfg) — without this a
    # dashboard-only gateway (no Slack tokens) resets TTS to defaults on
    # every restart (see load_voice_reply_config).
    from kiro_crew.slack.handler import load_voice_reply_config

    await asyncio.to_thread(load_voice_reply_config)
    # ── Tunnel teardown (FIRST cleanup hook, deliberately) ───────────────────
    # aiohttp dispatches ``on_cleanup`` in registration order and gateway
    # shutdown has a hard deadline, so this is registered ahead of every other
    # cleanup hook: behind them it can be starved — instances cleanup waiting on
    # SSH children that ignore SIGTERM eats the deadline, the gateway
    # force-exits, and the tunnel is never stopped. Safe this early: the hook
    # only reads ``state.tunnel_manager`` lazily at shutdown, long after
    # ``setup_tunnel`` assigns it further below, and this is still well before
    # ``runner.setup()`` freezes the signal lists. See ``_wire_tunnel_shutdown``.
    _wire_tunnel_shutdown(app, state)
    from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

    app["kiro_prerequisite_service"] = await asyncio.to_thread(
        KiroPrerequisiteService,
        assume_ready=assume_kiro_ready,
    )
    state.kiro_prerequisite_service = app["kiro_prerequisite_service"]
    # Probe Kiro readiness during boot rather than on the dashboard's first
    # status request: the cold probe spawns sandboxed CLI subprocesses and can
    # take seconds, which is what made the first-run setup chrome visible to
    # returning users. Fire-and-forget — a warm-up is never a boot dependency,
    # and the task is cancelled by the service's shutdown hook.
    app["kiro_prerequisite_service"].warm_up()
    state.load_folders()
    # Off-loop: a large cron_folders.json would otherwise block the event
    # loop with synchronous file I/O + JSON parsing during startup.
    await asyncio.to_thread(state.load_cron_folders)
    # Off-loop: a large chat_pins.json must not block the event loop at startup.
    await asyncio.to_thread(state.load_chat_pins)
    # Off-loop: load_tags runs a synchronous save_tags() during load (status
    # back-fill / seed) which fsyncs on the event loop; a large tags.json —
    # including preserved-but-malformed rows (#5792) — must not stall startup.
    await asyncio.to_thread(state.load_tags)
    app["port"] = port
    app["dashboard_url"] = dashboard_url

    # Route pull-request status deltas to owner websockets. Extracted so the
    # register + shutdown-cleanup contract is unit-testable without booting the
    # whole gateway (see test_wire_status_delta_sink_registers_and_cleans_up).
    _wire_status_delta_sink(app, state)

    _precompute_telemetry(state)

    # MCP tool routes (shared with start_api_server)
    _register_mcp_routes(app)

    # Install persistent log ring buffer (captures logs even when Logs page is closed)
    ring_handler = handlers.install_log_ring_handler()
    if ring_handler:
        ring_handler.set_state(state)

    # Page routes
    # The route table lives in ``dashboard/routes/``, one module per section.
    # aiohttp resolves in REGISTRATION order and several routes rely on a literal
    # path preceding a pattern that would swallow it, so ``register_all`` calls the
    # slices in the table's original sequence -- see that package's docstring.
    register_all(app)

    # Register built-in apps (idempotent — surfaces baked-in features in App Store).
    # Runs on the executor: escalation cleanup can traverse/delete legacy app
    # dirs, which must not block the event loop during startup.
    await asyncio.get_running_loop().run_in_executor(subprocess_executor(), register_builtin_apps)

    # Warm the PreToolUse gate's first-party (builtin) app-name set from the
    # shipped manifests, ONCE, on the executor (the discovery walk touches the
    # filesystem and must not run on the event loop). The gate's app-own-server
    # auto-approve then does a pure in-memory membership test with zero I/O; an
    # empty set (should this fail) simply fails closed (owns-server calls prompt).
    async def _warm_builtin_app_names() -> None:
        try:
            from kiro_crew.apps.execution import (
                builtin_app_agents,
                builtin_app_mcp_servers,
                builtin_app_names,
            )
            from kiro_crew.hooks import (
                set_builtin_app_agents,
                set_builtin_app_mcp_servers,
                set_builtin_app_names,
            )

            names = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), builtin_app_names
            )
            set_builtin_app_names(names)
            servers = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), builtin_app_mcp_servers
            )
            set_builtin_app_mcp_servers(servers)
            # Agent → owning app, so a builtin whose UI is not an app iframe
            # (empty Slot._app) can still auto-approve calls to its OWN server.
            agents = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), builtin_app_agents
            )
            set_builtin_app_agents(agents)
        except Exception:  # noqa: BLE001 — a warm failure only costs an extra prompt
            logger.warning("Failed to warm builtin app-name set for the gate", exc_info=True)

    await _warm_builtin_app_names()

    # Prime the materialized-agent snapshot on the executor. The resolver's read
    # path does zero filesystem work, so this boot scan (plus the one
    # `_register_agents` does after it writes) is what keeps the snapshot current
    # without ever scanning on the event loop.
    async def _warm_materialized_agents() -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), refresh_materialized_agents
            )
        except Exception:  # noqa: BLE001 — a warm failure only costs one fallback
            logger.debug("Failed to warm materialized agent names", exc_info=True)

    await _warm_materialized_agents()

    # Reconcile resources (agents / skills / crons / MCP) for every ENABLED app.
    # Registration otherwise happens only in the enable path, so an app that
    # gains agents or skills in a later version never registers them for a user
    # who already enabled it. Runs on the executor: it walks the apps tree and
    # writes into ~/.kiro/agents.
    async def _reconcile_app_resources() -> None:
        from kiro_crew.apps.bridges import reconcile_enabled_app_resources

        try:
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), reconcile_enabled_app_resources
            )
        except Exception as exc:  # noqa: BLE001 — never block gateway startup
            logger.warning("App resource reconcile failed: %s", exc)

    await _reconcile_app_resources()

    # One-time migration: disable stale deploy_web builtin installs (now core module).
    # Idempotent — logs once and silently succeeds if already gone.
    # R34 F1: the cleanup reads/deletes files under the data dir — run it off
    # the event loop so wedged filesystem I/O cannot block gateway startup.
    from kiro_crew.apps.builtins import _MIGRATED_BUILTINS

    def _run_migrated_cleanup() -> None:
        for _migrated in _MIGRATED_BUILTINS:
            try:
                _result = cleanup_migrated_builtin(_migrated)
                if not _result.ok:
                    logger.warning(
                        "migrated builtin cleanup failed for %s: %s", _migrated, _result.error
                    )
                elif _result.message and "cleaned up" in _result.message:
                    logger.info("migrated builtin cleanup: %s — %s", _migrated, _result.message)
            except Exception:  # noqa: BLE001
                logger.debug("migrated builtin cleanup skipped for %s", _migrated)

    await asyncio.to_thread(_run_migrated_cleanup)

    # Core deploy module routes (folded from deploy_web app)
    _register_deploy_routes(app)

    # Core deploy skills — symlink into <home>/skills/ so the agent can load them.
    # Offloaded: copytree/rmtree/stat are blocking filesystem calls.
    await asyncio.to_thread(_register_deploy_skills)

    # Knowledge Library
    setup_knowledge_routes(app)
    setup_weixin_routes(app)
    setup_feedback_routes(app)
    setup_secrets_routes(app)
    setup_whatsapp_routes(app)

    # Link previews (chat unfurl). Route is always registered; the handler gates
    # itself on cfg.dashboard.link_previews, so toggling the feature needs no
    # gateway restart.
    setup_link_meta_routes(app)

    # Start backends for enabled apps on the subprocess_executor bulkhead: the
    # startup stale-reap shells out to `ps` per orphan and may SIGTERM→sleep→
    # SIGKILL for seconds, and start_app_backend blocks on a survival poll — all
    # wedge-prone blocking work that would freeze this event loop if run inline.
    # subprocess_executor (not the default to_thread pool) isolates it so a hung
    # `ps` cannot starve asyncio's default executor (the RFC's bulkhead intent).
    await cautious_boot.pause_before("app backends")
    started_apps = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), start_enabled_app_backends
    )
    if started_apps:
        logger.info("Started %d app backend(s): %s", len(started_apps), ", ".join(started_apps))

    # Both adapters are shared with the enable path (apps/routes.py) so the two
    # entry points cannot drift into giving an app different capabilities.
    from kiro_crew.apps.event_bus import build_broadcast_fn
    from kiro_crew.apps.spawn_sdk import build_spawn_impl

    _app_event_broadcast = build_broadcast_fn(state.broadcast_ws)
    _app_spawn = build_spawn_impl(state.subagents)

    # Initialize App SDK Gateway Hooks system
    init_hooks_system(
        app,
        cron_service=state.crons,
        broadcast_fn=_app_event_broadcast,
        spawn_impl=_app_spawn,
    )

    async def _hooks_startup(app_: web.Application) -> None:
        await on_gateway_startup(
            cron_service=state.crons,
            broadcast_fn=_app_event_broadcast,
            spawn_impl=_app_spawn,
        )
        # App dev-mode live reload: watch dev-flagged apps' ui/ dirs and
        # broadcast app_reload WS events on change (see apps/dev_mode.py).
        from kiro_crew.apps.dev_mode import init_dev_mode_watcher

        await init_dev_mode_watcher(state.broadcast_ws)

        # App hook reconciler: the CLI (`kirocrew app enable/disable/install/
        # uninstall`) mutates apps on disk in a DIFFERENT process and never
        # notifies this gateway, so a CLI reinstall left the old backend.hooks
        # module live, its on_startup task running, and its .app_secret stale
        # (issue #7880). This poll reloads changed hooks in-process — the same
        # "CLI writes disk, gateway reconciles" contract already used for crons
        # and UI files. Started AFTER on_gateway_startup so the boot pass has
        # already recorded its loaded-hook signatures in the shared registry and
        # the reconciler's first tick sees no drift. Synchronous + IO-free, so it
        # adds nothing before the dashboard socket binds.
        init_hook_reconciler(
            cron_service=state.crons,
            broadcast_fn=_app_event_broadcast,
            spawn_impl=_app_spawn,
        )

    app.on_startup.append(_hooks_startup)

    async def _hooks_shutdown(app_: web.Application) -> None:
        # Stop the background pollers BEFORE the gateway hook shutdown sweep.
        # The reconciler and the dev-mode watcher can each LOAD/START app hooks
        # on a tick; if either is still live while on_gateway_shutdown() tears
        # hooks down, a poll landing mid-sweep could re-import a module or spawn
        # an on_startup task AFTER it was torn down, so that app's code would
        # survive an in-process gateway restart. Cancelling them first also stops
        # their module-global tasks from leaking stale gateway service handles /
        # a stale broadcast_ws across the restart. Await cancellation so neither
        # can fire one more tick during the sweep.
        from kiro_crew.apps.dev_mode import stop_dev_mode_watcher

        # on_gateway_shutdown() is the sweep that actually tears down app
        # backends; it MUST run even if stopping a poller hangs (its bounded
        # drain can burn its budget) or raises, otherwise a spawned app backend
        # survives gateway exit. Stop the pollers first (preserving the
        # no-tick-during-sweep ordering) but never let a stop failure abort the
        # sweep: catch and log it, then always run on_gateway_shutdown.
        try:
            await stop_dev_mode_watcher()
            await stop_hook_reconciler()
        except Exception:
            logger.exception(
                "Error stopping background pollers on shutdown; proceeding to the "
                "gateway hook shutdown sweep so app backends are torn down"
            )
        await on_gateway_shutdown()

    app.on_cleanup.append(_hooks_shutdown)

    # Edition-contributed dashboard routes + background services (CPP
    # DashboardContributor seam). The Default contributes nothing, so the public
    # dashboard is unchanged. Routes are mounted HERE — before the SPA static
    # catch-all below and well before ``runner.setup()`` freezes the route table
    # and the on_startup/on_cleanup signal lists (see _register_instances_hooks).
    # Fail-closed: a non-standalone host that cannot compose its companion raises.
    safe_context_call(
        lambda: current_context().dashboard.contribute_routes(app),
        fallback=None,
        log_message="dashboard.contribute_routes failed; no edition routes mounted",
    )

    # The service lifecycle hooks are async; they route through
    # ``async_safe_context_call`` so they share the SAME fail-closed discipline as
    # every sync seam call (re-raise ``PlatformCompositionError`` from a host that
    # could not compose its companion; degrade any other transient service error,
    # logged, rather than bricking the gateway start/stop) — kept in one place so
    # a future fail-closed policy change cannot diverge per hand-written copy.
    async def _contrib_startup(app_: web.Application) -> None:
        await async_safe_context_call(
            lambda: current_context().dashboard.start_services(app_),
            fallback=None,
            log_message="dashboard.start_services failed; no edition services",
        )

    async def _contrib_shutdown(app_: web.Application) -> None:
        await async_safe_context_call(
            lambda: current_context().dashboard.stop_services(app_),
            fallback=None,
            log_message="dashboard.stop_services failed",
        )

    app.on_startup.append(_contrib_startup)
    app.on_cleanup.append(_contrib_shutdown)

    # Static files — prefer React dist/ build, fall back to legacy static/
    if _DIST_DIR.is_dir():
        _register_dist_static_routes(app, _DIST_DIR)
    if _STATIC_DIR.is_dir():
        app.router.add_static(
            "/static",
            _STATIC_DIR,
            show_index=False,
            append_version=True,
        )
    else:
        logger.warning("Static dir not found: %s", _STATIC_DIR)

    # ── Middleware ────────────────────────────────────────────────────────────

    # No-cache: prevents Chrome from caching stale assets
    @web.middleware  # type: ignore[misc]
    async def no_cache_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        resp = await handler(request)  # type: ignore[operator]
        if hasattr(resp, "headers"):
            _apply_security_headers(resp, request.app, request.path, request)
        return resp  # type: ignore[return-value]

    # SPA fallback: serve index.html for client-side React Router paths.
    # Uses the same _is_spa_shell_request predicate as the auth middleware so
    # the two layers never drift. Bare /apps/{name} paths (no sub-path) are
    # treated as SPA navigations and served index.html — this fixes browser
    # refresh on e.g. /apps/code-review-sage which has no server-side route.
    @web.middleware  # type: ignore[misc]
    async def spa_fallback(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        try:
            return await handler(request)  # type: ignore[operator]
        except web.HTTPNotFound:
            if _is_spa_shell_request(request):
                return await handlers.index(request)
            raise

    # SEL: log mutating API operations
    _sel_log_methods = {"POST", "PUT", "DELETE", "PATCH"}

    @web.middleware  # type: ignore[misc]
    async def sel_audit_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        if request.method in _sel_log_methods and request.path.startswith("/api/"):
            # Claim only what this middleware actually records. Its except arm
            # logs a refusal raised below this point, so the boundary must not
            # add a second entry for it — but a request OUTSIDE this branch is
            # logged nowhere here, and claiming it would hand the boundary a
            # promise no one keeps (a cross-origin WebSocket GET refused in its
            # handler would be silently unaudited).
            mark_audit_claimed(request)
            from kiro_crew.sel import sel

            try:
                resp = await handler(request)  # type: ignore[operator]
                sel().log_api_access(
                    caller="dashboard_user",
                    operation=f"{request.method} {request.path}",
                    outcome="ok" if resp.status < 400 else "error",
                    resources=request.path,
                )
                return resp  # type: ignore[return-value]
            except Exception as exc:
                sel().log_api_access(
                    caller="dashboard_user",
                    operation=f"{request.method} {request.path}",
                    outcome="error",
                    resources=request.path,
                    error=str(exc)[:200],
                )
                raise
        return await handler(request)  # type: ignore[operator]

    # Tailnet origin (RFC §4): this machine's own MagicDNS name, so
    # `tailscale serve` works without the operator hand-writing dashboard.url.
    # Off by default; resolved in a thread so the daemon call cannot stall the
    # loop; "" whenever Tailscale is absent, stopped, or produced nothing that
    # validated.
    _cfg = KiroCrewConfig.load()
    _ts_cfg = _cfg.dashboard.tailscale
    _tailnet_host = await tailnet.resolve_tailnet_host(_ts_cfg.enabled)
    # Identity trust (RFC §2–§3.1): validated at config load, governance
    # ceiling applied inside the shared helper — ONE code path for both
    # startup surfaces, so they cannot drift.
    _tailnet_trust = await tailnet.governed_tailnet_trust(
        _ts_cfg.trust_identity,
        tailnet_effective_allowed_logins(_cfg.degraded_sections, _ts_cfg.allowed_logins),
        _ts_cfg.pin_scope,
        # An unreadable tailnet policy resolves allowed_logins to [] and so
        # trust_identity to False, which is "no login restriction". The values
        # alone cannot tell that from "never configured"; degraded_sections can.
        identity_unknown=tailnet_identity_unknown(_cfg.degraded_sections),
        unreadable_files=tuple(degraded_config_files(_cfg.degraded_sections)),
    )
    if _tailnet_host:
        logger.info(
            "tailnet access enabled: trusting origin https://%s (bind and auth unchanged)",
            _tailnet_host,
        )
    # Keep the initial snapshot on both startup surfaces for compatibility.
    # Runtime-aware handlers read the mutable state installed below, which can
    # acquire one validated origin after a Tailscale/Gateway boot race.
    app["tailnet_host"] = _tailnet_host
    app["tailnet_resolved_at"] = int(time.time()) if _tailnet_host else 0
    # The governance-filtered identity-trust value the middleware was built
    # with, for handlers the middleware bypasses (POST /api/auth/refresh must
    # re-bind a rotated access token to the same verified peer identity).
    app["tailnet_trust"] = _tailnet_trust
    app["allowed_origins"] = build_allowed_origins(
        port, local_only, configured_host, tailnet_host=_tailnet_host
    )
    # Exposed to handlers (e.g. knowledge.pick_folder) that only make sense when
    # the browser and gateway are co-located on localhost.
    app["local_only"] = local_only

    # DNS-rebinding defense-in-depth — shared factory (single source of truth
    # for the barrier AND the PROBE_PATHS exemption; see
    # _make_host_validation_middleware).
    host_validation_middleware = _make_host_validation_middleware("dashboard_user")
    # Same factory as the headless server's barrier, so the CSRF exemption set is
    # one decision rather than two (see _make_csrf_middleware).
    csrf_middleware = _make_csrf_middleware("dashboard_user")
    # Audit boundary for refusals raised before sel_audit_middleware runs. Same
    # factory as the headless server's, so the guarantee cannot hold on one
    # entrypoint and not the other (see _make_deny_audit_middleware).
    deny_audit_middleware = _make_deny_audit_middleware("dashboard_user")

    # Generate per-session secret for local app / IPC authentication.
    # NOTE: file write (and parent mkdir) deferred until after port bind
    # succeeds — both live in _write_secret_file, offloaded below — to avoid
    # poisoning the secret file when a second instance fails to start and to
    # keep blocking fs I/O off the event loop.
    _secret_path = data_home() / ".local_secret"
    _internal_secret = os.urandom(16).hex()
    app["local_secret"] = _internal_secret

    # Host canonicalization: converge loopback aliases (127.0.0.1 / localhost /
    # kirocrew.localhost) onto a single origin so the SPA's per-origin
    # localStorage (theme, zoom, layout, notifications, ...) is never split
    # across hostnames. localStorage keys on scheme://host:port, so reaching the
    # dashboard on "localhost" one time and "kirocrew.localhost" the next (e.g.
    # `kirocrew token` historically printed localhost while the gateway
    # auto-opens kirocrew.localhost) lands the browser in a different, empty
    # bucket and all settings appear reset. The canonical host is resolved once
    # at startup (it is stable for the gateway's lifetime). Only top-level
    # document GET/HEAD navigations on a non-canonical loopback alias are
    # redirected (see should_canonicalize_host); APIs, WebSockets, and
    # sub-resource fetches are untouched — once the document settles on the
    # canonical host every later request is already canonical. Disabled unless
    # local_only, so reverse-proxy / remote-host deployments are never affected.
    _canonical_host = resolve_dashboard_host(local_only) if local_only else ""

    host_canonical_redirect = build_host_canonical_redirect(_canonical_host)

    # Warm the auth singletons (signing secret + revoked-nonce store) off the
    # event loop BEFORE building the middleware chain, so no blocking key-file
    # I/O lands on the loop on the first auth op.
    await warm_auth_singletons()

    # Explicit middleware ordering — self-documenting and immune to future insertions
    app.middlewares[:] = [
        # Outermost: privacy-safe per-route latency (rec #1). Times the FULL
        # in-gateway handling (all middleware + handler). Labels are limited to
        # method / bounded route_template / status_class — never a real path,
        # query, id, or body — so it cannot leak content or explode cardinality.
        make_route_latency_middleware(),
        # Outer to every barrier that can refuse, so a pre-audit 403 is recorded
        # by POSITION rather than by each deny site remembering to. Inner to the
        # latency middleware only, which keeps that one's "times the FULL
        # in-gateway handling" contract intact.
        deny_audit_middleware,
        host_canonical_redirect,
        host_validation_middleware,
        no_cache_middleware,
        csrf_middleware,
        token_auth_middleware(
            internal_paths=_STRICT_INTERNAL_API_PATHS,
            mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
            internal_secret=_internal_secret,
            port=port,
            local_only=local_only,
            spa_shell_handler=handlers.index,
            tailnet_trust=_tailnet_trust,
        ),
        sel_audit_middleware,
        spa_fallback,
    ]

    # Verify security invariant: if dashboard_url expands the CSRF origin
    # set for a remote URL, token auth middleware MUST be active.
    if dashboard_url:
        _has_token_auth = any(getattr(mw, "_is_token_auth", False) for mw in app.middlewares)
        if _has_token_auth:
            app["allowed_origins"] = build_allowed_origins(
                port, local_only, configured_host, dashboard_url, tailnet_host=_tailnet_host
            )
            logger.info(
                "dashboard_url=%s: added to CSRF allowed origins (token auth verified)",
                dashboard_url,
            )
        else:
            logger.error(
                "dashboard_url=%s requires token auth — refusing to start without it. "
                "Enable Slack or remove dashboard.url from config.",
                dashboard_url,
            )
            raise RuntimeError("dashboard_url requires token auth middleware")

    # Register only after the final allowed-origin set is selected.  The startup
    # hook schedules a sleeping background task and returns immediately, so this
    # cannot extend listener startup; cleanup owns cancellation before aiohttp
    # freezes the signal lists in runner.setup().
    tailnet.install_tailnet_origin_recovery(
        app,
        enabled=_ts_cfg.enabled,
        initial_host=_tailnet_host,
        load_enabled=_tailnet_origin_enabled,
    )

    # ── Loop stall watchdog shutdown ─────────────────────────────────────────
    # Register the cleanup hook HERE, before ``runner.setup()`` freezes the
    # app's signal lists (appending after setup raises "Cannot modify frozen
    # list"). The watchdog itself is created after ``runner.setup()`` and stored
    # on ``state._loop_watchdog``; this hook only fires at shutdown — long after
    # that assignment — so the lazy ``getattr`` always resolves it.
    async def _watchdog_shutdown(app_: web.Application) -> None:
        wd = getattr(state, "_loop_watchdog", None)
        if wd is not None:
            wd.stop()

    app.on_cleanup.append(_watchdog_shutdown)

    # ── Prevent-sleep inhibitor shutdown ─────────────────────────────────────
    # Registered HERE (before runner.setup freezes the signal lists) for the
    # same reason as the watchdog hook above. The inhibitor + poll task are
    # created after runner.setup by _arm_prevent_sleep_poll and released here.
    _register_prevent_sleep_shutdown(app, state)

    async def _kiro_prerequisite_shutdown(app_: web.Application) -> None:
        await app_["kiro_prerequisite_service"].close()

    app.on_cleanup.append(_kiro_prerequisite_shutdown)

    async def _kas_login_shutdown(app_: web.Application) -> None:
        # Releases the service's aiohttp session IF a KAS request created it. It is
        # lazily built on first use (never at boot), so an app that never served a
        # KAS request has nothing to close.
        service = app_.get("kas_login_service")
        if service is not None:
            await service.close()

    app.on_cleanup.append(_kas_login_shutdown)

    # Releases the resident speech model (148MB default, 1.6GB largest) when idle
    # and at shutdown. Registered here, before runner.setup freezes the signal lists.
    _register_stt_hooks(app)

    # ── Instances (multi-instance management) ────────────────────────────────
    # Register the opt-in instances startup/cleanup hooks HERE, before
    # ``runner.setup()`` freezes the app's signal lists. See
    # ``_register_instances_hooks`` for why ordering matters.
    _register_instances_hooks(app, state, port)
    _register_browser_view_cleanup(app)
    _register_connections_warm_lifecycle(app, state)

    # Unix-socket cleanup hook — registered before runner.setup freezes the
    # signal lists; the path itself only becomes known after the site starts
    # (below), hence the holder indirection.
    _unix_socket_holder: dict[str, Path | None] = {"path": None}
    _register_unix_socket_cleanup(app, _unix_socket_holder)

    # Hardened runner: bounds the request-line/header read time (slowloris /
    # CWE-400) and reaps idle keep-alive connections. See dashboard.slowloris.
    # max_field_size is raised from aiohttp's 8190 default so the accumulated
    # shared per-port cookie jar can't 400 at the parser before a handler
    # prunes it (see refresh_tokens.foreign_port_cookies).
    runner = build_hardened_runner(app, max_field_size=_MAX_HEADER_FIELD_SIZE)
    await runner.setup()
    site = web.TCPSite(runner, bind_address_for(local_only), port)
    await _start_site(site, port)
    # Export the port this gateway ACTUALLY bound so child processes resolve
    # loopback callbacks against the truth, not a re-derived config guess.
    _export_bound_port(runner, port)
    # Additional kernel-verifiable transport for the internal API (POSIX only;
    # degrades to TCP-only on any failure — see _start_unix_site).
    _unix_socket_holder["path"] = await _start_unix_site(runner, port)

    # Port bind succeeded — now safe to write the secret file. Offloaded:
    # _write_secret_file does blocking fs I/O (os.open/os.close, plus the
    # owner-only lockdown on Windows), so it must not run on the
    # event loop (no-blocking-call-on-event-loop). The port is passed so the
    # credential is published per listener, not only into the shared file every
    # gateway in this data home writes (see _write_instance_credentials).
    try:
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            _write_instance_credentials,
            _secret_path,
            _resolved_bound_port(runner, port),
            _internal_secret,
        )
    except OSError:
        await runner.cleanup()
        raise

    # Listener is bound and credentials are published — now kick the warm
    # crash-residue scavenge. Deliberately NOT an on_startup hook: those run
    # inside runner.setup(), before the bind, and the scavenge's deferred
    # import must never sit in front of the listener
    # (no-new-work-on-gateway-boot-path).
    _kick_connections_warm_scavenge(state)

    # Event-loop heartbeat: proves the asyncio loop is live (the off-loop /proc
    # sampler can't — it runs in a subprocess). Sleeps 10s, then logs actual
    # elapsed. If the loop wedges (e.g. a coroutine blocks it), this task can't
    # be scheduled, so the log goes SILENT during the stall and the first tick
    # after recovery reports a lag >> 10s — that gap IS the wedge, measured.
    #
    # The heartbeat also "beats" an off-loop stall watchdog (a daemon thread).
    # The recovery-lag log above only fires if the loop EVER recovers; when it
    # wedges permanently the log just goes silent. The watchdog runs on its own
    # thread — unaffected by a loop thread blocked in a syscall — and dumps all
    # thread stacks via faulthandler once the heartbeat stops beating, so the
    # stuck frame lands in the log automatically instead of leaving us to sample
    # the PID by hand.
    #
    # Crash-dump discoverability: route dumps to a dedicated file under
    # ~/.kiro/crew/logs/crash-dumps/ so they are findable via `kirocrew doctor`
    # and startup warnings, rather than buried in interleaved stderr/journal.
    # Crash-dump hygiene: sweep header-only dumps left by prior sessions that
    # exited without ever wedging (every startup pre-creates one for
    # faulthandler's fd), THEN rotate. Sweeping first keeps empty startup files
    # from aging real stall dumps out of the rotation window.
    await asyncio.to_thread(sweep_stale_dumps)
    await asyncio.to_thread(rotate_dumps)
    _dump_file = await asyncio.to_thread(open_dump_file)
    # exit_after is configurable because the right budget is host-dependent: a
    # gateway doing heavy subprocess work (long builds, test suites, bursts of
    # child reaping) can wedge the loop briefly without being genuinely dead,
    # and a hard-coded 25s turned those into hard exits that lost in-flight
    # work. The default is unchanged; the loader clamps the range.
    try:
        _exit_after = float(load_loop_stall_exit_after(_launch_environment))
    except Exception:
        logger.debug("loop-stall exit budget config unavailable; using default", exc_info=True)
        # Config failure must not erase the managed-service grace that protects
        # the process while its config filesystem is itself under pressure.
        _exit_after = float(resolve_loop_stall_exit_after(environ=_launch_environment))
    _loop_watchdog = LoopStallWatchdog(dump_file=_dump_file, exit_after=_exit_after)
    _heap_trim_maintainer = platform_compat.HeapTrimMaintainer()

    async def _loop_heartbeat() -> None:
        # 5s (not 10s) so the watchdog's armed dump-then-exit timer is re-petted
        # at a finer resolution. The timer fires exit_after seconds after the
        # LAST beat, so the real silence the gateway tolerates before _exit is
        # ``exit_after - (time since last beat)`` — i.e. up to one interval less
        # than exit_after. A 5s interval keeps that worst case at ~20s (vs ~15s
        # at 10s), so genuinely-recoverable 15-20s stalls are less likely to be
        # killed while still landing well under the Electron probe's kill window.
        interval = 5.0
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(interval)
            _loop_watchdog.beat()
            lag = time.monotonic() - t0 - interval
            # Resource-pressure notifications ride the heartbeat cadence
            # rather than owning a task: the notifier self-gates to its own
            # sample interval, never raises, and off-loads its synchronous
            # probe to a worker thread so a slow config filesystem cannot
            # block the loop this heartbeat exists to watch. After the lag
            # read so the await can't register as loop lag.
            await state.resource_pressure_notifier.maybe_sample()
            released = await _heap_trim_maintainer.maybe_trim()
            if released >= platform_compat.HEAP_TRIM_LOG_THRESHOLD_BYTES:
                logger.info(
                    "Gateway heap trim returned %.0f MiB to the OS",
                    released / (1024 * 1024),
                )
            if lag > 1.0:
                logger.warning("event-loop heartbeat: lag %.1fs (loop was blocked)", lag)
            else:
                # Healthy ticks are DEBUG: at the default WARNING level the loop
                # stays silent unless it actually wedges (the tripwire), and we
                # don't emit ~8.6k INFO lines/day when DEBUG is enabled.
                logger.debug("event-loop heartbeat ok (lag %.2fs)", lag)

    def _heartbeat_done(task: "asyncio.Task") -> None:  # type: ignore[type-arg]
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("event-loop heartbeat task exited unexpectedly", exc_info=exc)

    _hb = asyncio.create_task(_loop_heartbeat())
    _hb.add_done_callback(_heartbeat_done)
    state._loop_heartbeat = _hb  # prevent GC

    # ── Prevent-sleep poll ───────────────────────────────────────────────────
    # Keep the host awake while a turn is in flight (opt-in via
    # dashboard.prevent_sleep), or while the dashboard is published on the
    # tailnet (opt-out via dashboard.tailscale.keep_awake). Shared with the
    # headless --slack-only entrypoint.
    _arm_prevent_sleep_poll(state, port)

    # Arm the stall watchdog only when faulthandler is enabled — i.e. under the
    # real gateway entrypoint (see cli `gateway` dispatch). Tests that spin up
    # the dashboard directly don't enable faulthandler, so they don't leak a
    # watchdog thread; the heartbeat still beats it harmlessly.
    if faulthandler.is_enabled():
        _loop_watchdog.start()
    # Stopped on shutdown via the ``_watchdog_shutdown`` on_cleanup hook,
    # which is registered before ``runner.setup()`` freezes the signal lists.
    state._loop_watchdog = _loop_watchdog  # prevent GC; stop on cleanup

    # Surface any prior crash dump from a previous gateway session.
    # The armed dump-then-exit path (exit_after=25s) writes ONLY to the dedicated
    # file — not stderr/journal — because faulthandler.dump_traceback_later targets
    # a single fd. To ensure journal-only operators (containers) still see the stacks,
    # we replay the dump content into the logger on next startup.
    _prior_dump = await asyncio.to_thread(newest_dump_with_stacks)
    if _prior_dump is not None:
        _age_h = await asyncio.to_thread(dump_age_seconds, _prior_dump) / 3600
        if _age_h < 168:  # Only surface dumps less than 7 days old
            logger.warning(
                "⚠️  Prior loop-stall crash dump found: %s (%.1f hours ago). "
                "Run `kirocrew doctor` for details.",
                _prior_dump,
                _age_h,
            )
            # Replay stack content to journal so container/journal-only operators
            # can see it without accessing the file system.
            _replay_lines, _truncated = await asyncio.to_thread(dump_replay_lines, _prior_dump)
            if _replay_lines:
                _replay_body = "\n".join(_replay_lines)
                if _truncated:
                    _replay_body += "\n  [truncated — full dump at above path]"
                logger.warning("Replaying prior crash dump stacks:\n%s", _replay_body)
            # A log line is not enough. This dump means the previous gateway
            # exited by hard-exit: no `finally` ran, nothing was flushed, and any
            # turn in flight lost work that was written but not yet committed.
            # The user needs to know that happened rather than discovering a
            # monitoring loop had silently stopped hours earlier. Claimed once
            # per dump — the dump is re-detected for up to 7 days on every
            # start, so notifying unconditionally would alert every restart.
            if await asyncio.to_thread(claim_dump_notification, _prior_dump):
                try:
                    state.notify(
                        "heartbeat",
                        "⚠️ Gateway restarted after an event-loop stall",
                        (
                            f"The previous gateway stopped responding and exited "
                            f"{_age_h:.1f}h ago, then restarted. Work in flight at "
                            f"that moment was interrupted and not saved. Thread "
                            f"stacks: {_prior_dump}"
                        ),
                        meta={"url": "/settings", "dump": str(_prior_dump)},
                    )
                except Exception:
                    logger.debug("stall-exit notification failed", exc_info=True)

    # Fire background MCP probe at startup (non-blocking). The probe spawns a
    # handshake subprocess per configured MCP server, so under cautious boot it
    # gets its own launch window instead of landing on top of the app backends.
    await cautious_boot.pause_before("MCP server probe")
    asyncio.create_task(handlers._bg_mcp_probe())

    # Refresh config.json's meta stamp when an upgrade left it naming the
    # previous build (#3102). Post-bind and fire-and-forget (never awaited on
    # the boot path), and the file I/O runs in a thread so the version check —
    # one small fixed-path file, O(1), rewrite only on mismatch — never holds
    # the event loop. Two locks cover both writer generations: the refresh
    # itself goes through update_config_locked (sidecar advisory lock), and
    # the loop-side asyncio config lock is held around the off-thread call so
    # the legacy writers that serialize on that lock alone cannot land inside
    # the refresh's read→write window. Best-effort: a stale stamp is a
    # diagnostic blemish, so a failure here is logged and boot proceeds.
    async def _refresh_meta_stamp() -> None:
        try:
            async with handlers._get_config_lock():
                if await asyncio.to_thread(refresh_config_meta_stamp):
                    logger.info("config.json meta stamp refreshed to the running version")
        except Exception:
            logger.debug("config meta stamp refresh failed", exc_info=True)

    _stamp_task = asyncio.create_task(_refresh_meta_stamp())
    state._background_tasks.add(_stamp_task)
    _stamp_task.add_done_callback(state._background_tasks.discard)

    # Start terminal orphan reaper (kills PTYs with no WS past the reaper window)
    _reaper = asyncio.create_task(handlers.reap_orphaned_terminals(app))
    _reaper.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
    state._terminal_reaper = _reaper  # prevent GC

    # Point every `playwright-cli` invocation at the service-owned snapshot
    # directory, and keep that directory bounded.
    #
    # The variable goes on the GATEWAY's own environment because the agent runs the
    # CLI as a shell command in a descendant process: an env var is the only channel
    # that reaches an invocation the gateway never constructs. An invocation that
    # misses it writes into whatever directory the agent happened to be in, where
    # the pruner does not look and files accumulate without bound. The CLI accepts
    # this only as an env var, since `--config` is rejected on the follow-up
    # commands that make up most of a session.
    os.environ.update(browser_cli_snapshots.cli_env_overrides())
    # The optional attach token rides the same channel for the same reason: the
    # agent runs the CLI as a shell command, so only an inherited environment
    # reaches it. Absent by default, in which case this adds nothing.
    os.environ.update(browser_cli_token.cli_env_overrides())
    # Name the engine Kiro Crew actually installs. The CLI's own default is the
    # branded Chrome channel at an OS path the product never provisions, so
    # without this the first browse fails on a host where every readiness signal
    # is honestly green. Same channel and same reason as the two above; defers to
    # an operator who set the variable themselves.
    #
    # Off the event loop: computing the override writes the config file, and this
    # runs on the gateway's startup path.
    os.environ.update(await asyncio.to_thread(browser_cli_launch.cli_env_overrides))
    _snap_pruner = asyncio.create_task(_prune_browser_snapshots_loop())
    _snap_pruner.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
    state._browser_snapshot_pruner = _snap_pruner  # prevent GC

    # Start terminal title poller (pushes live foreground-command / cwd titles)
    _title_poller = asyncio.create_task(handlers.poll_terminal_titles(app))
    _title_poller.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
    state._terminal_title_poller = _title_poller  # prevent GC

    # Start periodic flush loop for crash protection (saves dirty slots every 5s)
    state.start_flush_loop()

    # Restore sessions — always restore foldered/pinned sessions; optionally restore recent ones.
    # NOTE: Even with restore_sessions=false, foldered and pinned sessions are restored
    # so the Explorer tree stays populated.  Users can unpin or remove from folder to dismiss.
    cfg = KiroCrewConfig.load()
    _apply_startup_yolo(state, cfg)

    # Wire safety override expiry notifications
    async def _notify_slack_override_expired() -> None:
        """Post override expiry notice to Slack owner DM."""
        await _dm_owner(
            state,
            "\U0001f512 Safety override expired. Tools now require approval. Reply `/kirocrew yolo` to re-authorize.",
        )

    def _on_override_expired(source: str) -> None:
        """Notify all interfaces when safety override expires."""
        state.broadcast_ws("yolo_expired", {"source": source})
        state.push_slots_update()
        # Slots carrying STANDING trust keep their policy: that is a separate,
        # longer-lived decision than the expiring override, and it is also what must
        # survive the channel-trust revoke below.
        standing_trust: set[str] = set()
        if state.sessions is not None:
            from kiro_crew.dashboard.chat_utils import effective_session_key

            for slot in state._slots.values():
                if slot._trust or slot._trust_reads:
                    # Excluded from the channel-trust revoke below, via the SAME
                    # derivation the reset uses: a channel-born slot's turns run on
                    # the channel's own session key, so a `dashboard:<slot>` spelling
                    # names a key nothing on that path reads.
                    standing_trust.add(effective_session_key(slot))
                else:
                    # The SAME derivation the grant used. A channel-born slot's
                    # turns run on the channel's own session key, which is what
                    # `linked_session_key` holds, so clearing `dashboard:<slot>`
                    # here cleared a key nothing on the channel path ever reads:
                    # the TTL could not expire the grant it had handed out, which
                    # is worse than a missing off-switch because the operator was
                    # told it was time-bounded.
                    state.sessions.set_approval_policy(effective_session_key(slot), "")
        # Slack cleanup — isolated so failures don't block dashboard operations
        try:
            # From `messaging`, not `slack.handler`: the grant is channel-neutral.
            # This revokes the approval_policy half as well as the mapping, which is
            # what a CHANNEL session needs -- the loop just above resets only the
            # dashboard's own slots, and a subagent reads the policy rather than the
            # mapping, so policy left at "auto" outlives the override it belonged to.
            # ``keep_policy`` is what stops this from undoing the preservation above:
            # a Trust press can file a ``dashboard:`` key in the shared grant, and
            # resetting its policy here would revoke standing trust nobody expired.
            from kiro_crew.messaging.session_trust import clear_trusted_sessions

            clear_trusted_sessions(keep_policy=standing_trust)
        except Exception:
            logger.debug("Could not clear trusted sessions", exc_info=True)
        # Slack notification (prevent GC with background_tasks set)
        _dispatch_override_expiry_notification(state, _notify_slack_override_expired)
        # An expiry that lands on an unattended run is the one case that cannot
        # self-report: nobody is present to answer the prompts it produces.
        _notify_unattended_expiry(state, source)

    safety_override().on_expired = _on_override_expired

    # A grant that was live when the process went down is GONE -- grants are
    # in-memory by design and this does not change that. What it changes is that
    # the operator now hears about it. Without this, someone who granted six
    # hours of auto-approval and restarted an hour later got no signal at all:
    # the next unattended run just stopped on a prompt nobody was waiting for.
    #
    # Read OFF the loop and off the boot path: it is a file open on a filesystem
    # that may be slow, and nothing about boot should wait on it (found in
    # review). Safe to run after the startup grant because the record carries the
    # writing pid, so this process's own record is never read as a dropped one.
    #
    # Notice only, never a restored grant, and withheld when auto-approve is live
    # RIGHT NOW: a declared grant that the enterprise ceiling clamps to a timed
    # one is re-established by _apply_startup_yolo above, and telling the operator
    # it is "OFF" while it is on would be worse than saying nothing. A lapsed
    # grant, a config-declared one and an ``until_shutdown`` one are all silent
    # too -- see ``take_dropped_grant``.
    try:
        _dropped_grant = await asyncio.to_thread(_take_prior_dropped_grant)
        if _dropped_grant is not None and not safety_override().is_active():
            state.notify(
                "safety",
                "Auto-approve was dropped by a restart",
                describe_dropped_grant(_dropped_grant),
                meta={
                    "source": _dropped_grant.source,
                    "remaining_secs": _dropped_grant.remaining_secs,
                },
            )
    except Exception:
        # Startup must not fail over a notification. The grant is off either
        # way; the worst case is the operator not being told.
        logger.debug("Could not report a restart-dropped safety override", exc_info=True)

    # Restore exactly the tabs the user had open at last shutdown — these
    # come back regardless of mtime, so long-running tabs don't silently
    # fall off into History on every gateway restart. Closed tabs (meta.closed)
    # are still excluded by the rehydrate guard. restore_open_slots() logs
    # its own info line on success, so no caller-side log here.
    # Awaited (not called bare) so the restore yields to the loop between tabs and
    # the stall watchdog keeps getting its heartbeat — a user with many large tabs
    # would otherwise block here long enough to trip the 25s watchdog and crash-loop the
    # gateway before it finished starting.
    #
    # Both restores run inside suspend_slots_push() so the per-slot broadcasts
    # coalesce into one at the end: get_or_create_slot() pushes the whole slot list
    # on every call, which made bulk restore O(N²) in serialization work for
    # intermediate states no client renders. Reseeding happens inside the block too
    # — it must complete before the single broadcast so clients never see slots
    # under a counter that could still re-mint a colliding index.
    # Converge any leftover copy transcripts BEFORE the restores read them. A
    # channel conversation used to get a second transcript under a derived
    # dashboard key; on an install carrying one, its dashboard-authored turns
    # exist nowhere else, so they must be merged into the channel transcript
    # before a slot is built from it. Idempotent, so it is a cheap no-op on
    # every subsequent boot. Off-loop: it takes the per-session cross-process
    # flock, which must never block the event loop.
    try:
        # Slot names the session map claims as real dashboard sessions, so a
        # dashboard session that merely happens to be named like a channel
        # stem is never mistaken for an orphan of it.
        _claimed = await asyncio.to_thread(_claimed_dashboard_slots, state)
        merged = await asyncio.to_thread(migrate_channel_transcripts, dashboard_slots=_claimed)
        if merged:
            logger.info("Merged %d leftover channel transcript copies", merged)
    except Exception:
        # A failed migration leaves the orphan in place rather than losing
        # messages, so starting up without it is safe.
        logger.warning("channel transcript migration failed", exc_info=True)

    # Session restores spawn a kiro-cli process per restored tab — the last
    # large group of the startup battery, so it too gets a cautious-boot window.
    await cautious_boot.pause_before("session restore")
    with state.suspend_slots_push():
        await chat.restore_open_slots_async(state)
        restored = await chat.restore_recent_sessions_async(
            state,
            cfg.dashboard.restore_window_minutes if cfg.dashboard.restore_sessions else 0,
            folders_only=not cfg.dashboard.restore_sessions,
        )
        if restored:
            logger.info("Restored %d session(s)", restored)

        # Both restore paths above rehydrate tabs under their original
        # "chat-<N>-<ts>" keys but leave _slot_counter at its boot value of 0.
        # Reseed it past the highest restored index so the next new chat can't
        # re-mint a colliding low index (which scrambles the tab -> session map).
        state.reseed_slot_counter()

    # Surface conversations started on Slack/Discord/Teams (etc.) in the chat
    # list. These persist under channel-namespaced keys (``slack:<ts>``), which
    # neither restore path above builds slots for — without this they exist only
    # in the sidebar's collapsed History pane. Runs immediately, then on a timer
    # so a channel conversation started while the dashboard is open still shows
    # up without a restart.
    if cfg.dashboard.surface_channel_sessions:
        _chan_reconciler = asyncio.create_task(
            channel_slots.channel_slot_reconciler(state, cfg.dashboard.restore_window_minutes)
        )
        state._channel_slot_reconciler = _chan_reconciler  # prevent GC

    # Relaunch agents in non-archived channels
    from kiro_crew.channel import ChannelManager, run_channel_agent
    from kiro_crew.dashboard.handlers_channel import _spawn_agent_task

    mgr = ChannelManager(
        broadcast_fn=state.broadcast_ws,
        max_channels=cfg.agent.max_channels,
        max_agents=cfg.agent.max_channel_agents,
    )
    state.channel_manager = mgr
    for ch in mgr._channels.values():
        for agent in ch.members.values():
            agent.state = "pending"
            _spawn_agent_task(
                agent, run_channel_agent(agent, ch, state.sessions, is_yolo=lambda: state._yolo)
            )

    # ── AEA Tunnel ───────────────────────────────────────────────────────────
    _tunnel_enabled = cfg.tunnel.enabled
    # The enable gate is also routed through the active PlatformContext's
    # TunnelProvider.  The Default TunnelProvider.enabled() returns False, so
    # standalone is gated solely by ``cfg.tunnel.enabled`` exactly as before;
    # the companion can additionally enable the tunnel from its provider.
    try:
        _ctx_tunnel_enabled = current_context().tunnel.enabled()
    except Exception:
        logger.debug("tunnel.enabled() lookup failed; using cfg only", exc_info=True)
        _ctx_tunnel_enabled = False
    _tunnel_enabled = _tunnel_enabled or _ctx_tunnel_enabled
    logger.debug("Tunnel config: enabled=%s ctx.enabled=%s", _tunnel_enabled, _ctx_tunnel_enabled)
    if _tunnel_enabled:
        tunnel_mgr = await setup_tunnel(
            middlewares=list(app.middlewares),
            allowed_origins=app["allowed_origins"],
            tunnel_name_mode=cfg.tunnel.name_mode,
            tunnel_name_override=cfg.tunnel.name_override,
            port=port,
            log_api_access=sel().log_api_access,
        )
        if tunnel_mgr:
            state.tunnel_manager = tunnel_mgr

    # Boot-to-ready (rec #1): full dashboard init is complete and the server is
    # about to accept traffic. Privacy-safe — the only labels are the fixed
    # ``server``/``outcome`` enums. Best-effort; never blocks the return.
    # Publish readiness at the exact boundary measured as boot-to-ready.
    state.ready = True
    record_boot_to_ready((time.time() - state.start_time) * 1000.0, server="dashboard")

    return runner, state


async def start_api_server(
    sessions: SessionManager,
    crons: CronService,
    lessons: LessonStore,
    port: int = _DEFAULT_PORT,
    subagents: SubagentManager | None = None,
    task_runner: TaskRunner | None = None,
    slack_client: Any = None,
    owner_id: str = "",
    local_only: bool = True,
    configured_host: str = "",
    assume_kiro_ready: bool = False,
    conversation_log: Any = None,
) -> tuple[web.AppRunner, DashboardState]:
    """Start a minimal API-only server for MCP tool transport (no UI).

    Headless (``--slack-only``) mode. This server exposes the SAME
    state-changing MCP tool routes as the dashboard (``_register_mcp_routes``),
    so it MUST authenticate them at parity with ``start_dashboard``: loopback is
    NOT a trust boundary (local port forwarders and any web page the user opens
    can reach 127.0.0.1), so the internal MCP routes require the
    ``X-Internal-Secret`` machine-to-machine handshake, and state-changing
    requests are guarded against DNS-rebinding (Host) and cross-site browsers
    (Origin). Every in-repo caller (mcp-core, cron) already sends the secret.
    """
    state = DashboardState(
        sessions=sessions,
        crons=crons,
        lessons=lessons,
        start_time=time.time(),
        subagents=subagents,
        task_runner=task_runner,
        slack_client=slack_client,
        owner_id=owner_id,
        # Headless mode has no UI, but it still runs Slack turns -- and anything
        # that reasons about how far a conversation has got reads the transcript
        # through here. Leaving it unset made those readers fall back to their
        # can't-tell branch: an OPTIONS control posted in this mode carried no
        # position and every click on it was honoured, however stale.
        conversation_log=conversation_log,
    )
    state._hook_store = ScriptHookStore()
    set_global_hook_store(state._hook_store)

    # This path builds its state without a context_builder, so the loader is
    # reached through the task runner. Logged on a miss rather than silently
    # recording nothing, since a route that credits no reads is the bias this
    # observer exists to remove.
    if not register_skill_read_observer(state.context_builder, getattr(task_runner, "_ctx", None)):
        logger.info("skill-read observer not registered: no skills loader reachable")

    # Wire script hooks into subagent tool execution path
    if state.subagents is not None:
        state.subagents.hook_store = state._hook_store

    # Visible notice + pct reset when auto-compaction fires on a dashboard session
    state.wire_session_compact_callback()
    # Visible notice when the watchdog recycles a dashboard session (e.g. RSS)
    state.wire_session_recycle_callback()
    # Visible notice in a channel that just lost its session-resume binding
    state.wire_session_unbind_listener()

    app = web.Application(
        client_max_size=60 * 1024 * 1024
    )  # 60 MB: covers a 50 MB BUFFERED upload + multipart overhead. NOT a
    # ceiling on every upload: aiohttp enforces this in Request.read()/.post(),
    # not on the streaming multipart() reader, so the video path in
    # handlers/files.py streams past it under its own _MAX_VIDEO_UPLOAD_BYTES
    # (pinned by test_streaming_bypasses_the_app_client_max_size). Reading this
    # number as a global request cap is the false invariant to avoid.
    app["state"] = state
    # Bind the serving loop once, here: this runs ON that loop, so every
    # surface that later hands work in from a foreign thread -- slots
    # coalescing, an off-loop websocket send, the log handler's fan-out --
    # resolves the same loop instead of each latching its own copy from
    # whichever thread happens to arrive first.
    state.bind_serving_loop(asyncio.get_running_loop())
    # Voice settings live in slack/handler's module state and are otherwise
    # loaded only on the Slack startup path (set_orch_cfg) — without this a
    # dashboard-only gateway (no Slack tokens) resets TTS to defaults on
    # every restart (see load_voice_reply_config).
    from kiro_crew.slack.handler import load_voice_reply_config

    await asyncio.to_thread(load_voice_reply_config)
    from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

    app["kiro_prerequisite_service"] = await asyncio.to_thread(
        KiroPrerequisiteService,
        assume_ready=assume_kiro_ready,
    )
    state.kiro_prerequisite_service = app["kiro_prerequisite_service"]
    # Probe Kiro readiness during boot rather than on the dashboard's first
    # status request: the cold probe spawns sandboxed CLI subprocesses and can
    # take seconds, which is what made the first-run setup chrome visible to
    # returning users. Fire-and-forget — a warm-up is never a boot dependency,
    # and the task is cancelled by the service's shutdown hook.
    app["kiro_prerequisite_service"].warm_up()
    state.load_folders()
    # Off-loop: a large cron_folders.json would otherwise block the event
    # loop with synchronous file I/O + JSON parsing during startup.
    await asyncio.to_thread(state.load_cron_folders)
    # Off-loop: a large chat_pins.json must not block the event loop at startup.
    await asyncio.to_thread(state.load_chat_pins)
    # Off-loop: load_tags runs a synchronous save_tags() during load (status
    # back-fill / seed) which fsyncs on the event loop; a large tags.json —
    # including preserved-but-malformed rows (#5792) — must not stall startup.
    await asyncio.to_thread(state.load_tags)
    app["port"] = port

    _precompute_telemetry(state)

    # ── Auth parity with start_dashboard ─────────────────────────────────────
    # The MCP route surface is identical to the dashboard's, so the middleware
    # chain must be too. Host-allowlist source of truth is shared with the CSRF
    # Origin check via build_allowed_origins/build_allowed_hosts (see origin.py).
    _cfg = KiroCrewConfig.load()
    _ts_cfg = _cfg.dashboard.tailscale
    _tailnet_host = await tailnet.resolve_tailnet_host(_ts_cfg.enabled)
    # Same identity-trust value as start_dashboard, via the same shared helper
    # — the auth surface is identical, so the middleware inputs must be too.
    _tailnet_trust = await tailnet.governed_tailnet_trust(
        _ts_cfg.trust_identity,
        tailnet_effective_allowed_logins(_cfg.degraded_sections, _ts_cfg.allowed_logins),
        _ts_cfg.pin_scope,
        identity_unknown=tailnet_identity_unknown(_cfg.degraded_sections),
        unreadable_files=tuple(degraded_config_files(_cfg.degraded_sections)),
    )
    app["allowed_origins"] = build_allowed_origins(
        port,
        local_only,
        configured_host,
        tailnet_host=_tailnet_host,
    )
    # Stashed for the same reason as in start_dashboard, and set here too even
    # though /api/tailnet/status is registered on the dashboard app: leaving one of
    # the two startup paths without the keys is exactly the class of bug an earlier
    # round of this feature already shipped, and a handler moved into the MCP
    # surface later would silently read "" as "nothing was trusted".
    app["tailnet_host"] = _tailnet_host
    app["tailnet_resolved_at"] = int(time.time()) if _tailnet_host else 0
    # The governance-filtered identity-trust value the middleware was built
    # with, for handlers the middleware bypasses (POST /api/auth/refresh must
    # re-bind a rotated access token to the same verified peer identity).
    app["tailnet_trust"] = _tailnet_trust
    app["local_only"] = local_only
    # Parity with the full dashboard: headless gateways have the same live
    # Origin/Host boundary and must recover the same boot race without restart.
    tailnet.install_tailnet_origin_recovery(
        app,
        enabled=_ts_cfg.enabled,
        initial_host=_tailnet_host,
        load_enabled=_tailnet_origin_enabled,
    )

    # Per-session internal secret for machine-to-machine (mcp-core, cron) auth.
    # Deferred file write (and parent mkdir) until after the port binds (mirrors
    # start_dashboard): both live in _write_secret_file, offloaded below, so a
    # failed second instance never poisons the live gateway's secret file and no
    # blocking fs I/O runs on the event loop.
    _secret_path = data_home() / ".local_secret"
    _internal_secret = os.urandom(16).hex()
    app["local_secret"] = _internal_secret

    # SEL audit middleware — log mutating MCP tool calls
    _sel_methods = {"GET", "POST", "PUT", "PATCH", "DELETE"}

    @web.middleware  # type: ignore[misc]
    async def sel_audit_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        if request.method in _sel_methods and request.path.startswith("/api/"):
            # Claim only what this middleware records — same contract as the
            # dashboard's (see origin.AUDIT_CLAIMED_KEY): its except arm owns a
            # refusal raised below this point, and a request it does not log is
            # left unclaimed so the boundary can record one.
            mark_audit_claimed(request)
            # ``sel`` is imported at module scope (top of file); no in-function
            # import needed (host/csrf middleware below call it unqualified too).
            try:
                resp = await handler(request)  # type: ignore[operator]
                sel().log_api_access(
                    caller="mcp_tool",
                    operation=f"{request.method} {request.path}",
                    outcome="ok" if resp.status < 400 else "error",
                    resources=request.path,
                )
                return resp  # type: ignore[return-value]
            except Exception as exc:
                sel().log_api_access(
                    caller="mcp_tool",
                    operation=f"{request.method} {request.path}",
                    outcome="error",
                    resources=request.path,
                    error=str(exc)[:200],
                )
                raise
        return await handler(request)  # type: ignore[operator]

    # DNS-rebinding defense-in-depth, parity with start_dashboard by
    # construction — the SAME factory builds both barriers, including the
    # orchestrator probe exemption (see _make_host_validation_middleware /
    # origin.PROBE_PATHS): headless gateways are the instances most likely to
    # sit behind an orchestrator addressing them by pod/container IP.
    host_validation_middleware = _make_host_validation_middleware("mcp_tool")
    # Cross-site CSRF barrier at parity with start_dashboard by construction —
    # the SAME factory builds both, including the self-authenticating-webhook
    # exemption (see _make_csrf_middleware).
    csrf_middleware = _make_csrf_middleware("mcp_tool")
    # Audit boundary at parity with start_dashboard by construction — the SAME
    # factory builds both, so a pre-audit refusal cannot be positional on one
    # entrypoint and per-site on the other (see _make_deny_audit_middleware).
    deny_audit_middleware = _make_deny_audit_middleware("mcp_tool")

    # Warm the auth singletons off the event loop before building the chain
    # (parity with start_dashboard) so no blocking key-file I/O hits the loop.
    await warm_auth_singletons()

    # Explicit ordering mirrors start_dashboard: latency → deny-audit → host →
    # csrf → token → audit.
    app.middlewares[:] = [
        # Outermost: privacy-safe, bounded-cardinality per-route latency (rec #1).
        # The MCP routes are registered AFTER this assignment, so the middleware
        # captures its route-template set LAZILY on the first request (by which
        # point every route is registered) — see make_route_latency_middleware.
        make_route_latency_middleware(),
        # Outer to every barrier that can refuse: a pre-audit 403 is recorded by
        # POSITION here, not by each deny site remembering to.
        deny_audit_middleware,
        host_validation_middleware,
        csrf_middleware,
        token_auth_middleware(
            internal_paths=_STRICT_INTERNAL_API_PATHS,
            mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
            internal_secret=_internal_secret,
            port=port,
            local_only=local_only,
            # No SPA shell in headless mode: a no-token request must be denied
            # outright, never served an HTML shell (there is no UI to boot).
            spa_shell_handler=None,
            tailnet_trust=_tailnet_trust,
        ),
        sel_audit_middleware,
    ]

    _register_mcp_routes(app)

    # Probe parity with the full dashboard server. Headless gateways are often
    # the instances most likely to sit behind an orchestrator, so they must
    # expose the same unauthenticated, secret-free liveness/readiness surface.
    app.router.add_get("/api/health", handlers.api_health)
    app.router.add_get("/api/live", handlers.api_live)
    app.router.add_get("/api/ready", handlers.api_ready)

    # R16 F6: Deploy routes must be registered in api-only mode too, otherwise
    # the deploy_artifact MCP tool 404s in Slack-only/headless mode.
    _register_deploy_routes(app)

    async def _kiro_prerequisite_shutdown(app_: web.Application) -> None:
        await app_["kiro_prerequisite_service"].close()

    app.on_cleanup.append(_kiro_prerequisite_shutdown)

    async def _kas_login_shutdown(app_: web.Application) -> None:
        # Releases the service's aiohttp session IF a KAS request created it. It is
        # lazily built on first use (never at boot), so an app that never served a
        # KAS request has nothing to close.
        service = app_.get("kas_login_service")
        if service is not None:
            await service.close()

    app.on_cleanup.append(_kas_login_shutdown)

    # Releases the resident speech model (148MB default, 1.6GB largest) when idle
    # and at shutdown. Registered here, before runner.setup freezes the signal lists.
    _register_stt_hooks(app)

    # Prevent-sleep shutdown hook — registered before runner.setup freezes the
    # signal lists; the poll itself is armed after the port binds (below). This
    # is what makes headless --slack-only keep the host awake during a long
    # Slack task, identically to the full dashboard.
    _register_prevent_sleep_shutdown(app, state)
    _register_connections_warm_lifecycle(app, state)

    # Unix-socket cleanup hook — same holder pattern as start_dashboard,
    # registered before runner.setup freezes the signal lists.
    _unix_socket_holder: dict[str, Path | None] = {"path": None}
    _register_unix_socket_cleanup(app, _unix_socket_holder)

    # Hardened runner: same slowloris / CWE-400 mitigation as start_dashboard,
    # plus the raised max_field_size (see start_dashboard for the cookie-jar
    # rationale).
    runner = build_hardened_runner(app, max_field_size=_MAX_HEADER_FIELD_SIZE)
    await runner.setup()
    # Same bind resolution as start_dashboard: loopback unless the operator
    # widened it (dashboard.url opt-out of local_only, or the KIROCREW_BIND
    # container override honored inside bind_address_for). Without this the
    # documented `gateway --slack-only` container path would silently bind
    # loopback and be unreachable through a published Docker port.
    bind_addr = bind_address_for(local_only)
    site = web.TCPSite(runner, bind_addr, port)
    await _start_site(site, port)
    # Export the actually-bound port for child processes (parity with
    # start_dashboard — headless gateways spawn the same MCP stdio children).
    _export_bound_port(runner, port)
    # Additional kernel-verifiable transport for the internal API (parity with
    # start_dashboard; POSIX only, degrades to TCP-only on any failure).
    _unix_socket_holder["path"] = await _start_unix_site(runner, port)

    # Port bind succeeded — now safe to persist the secret file (parity with
    # start_dashboard: write deferred so a failed bind can't poison it).
    # Offloaded: _write_secret_file does blocking fs I/O (os.open/os.close and,
    # on Windows, the owner-only DACL), so it must not run
    # on the event loop (no-blocking-call-on-event-loop). Same per-listener
    # publication as start_dashboard: both surfaces must pair the credential
    # with the port or a client cannot tell which generation it reached.
    try:
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            _write_instance_credentials,
            _secret_path,
            _resolved_bound_port(runner, port),
            _internal_secret,
        )
    except OSError:
        await runner.cleanup()
        raise

    # Listener is bound — kick the warm crash-residue scavenge (parity with
    # start_dashboard: never an on_startup hook, which would run the deferred
    # import before the bind).
    _kick_connections_warm_scavenge(state)

    logger.info("API-only server listening on %s:%d", bind_addr, port)

    # Arm the prevent-sleep poll now the loop is up and the port is bound
    # (shutdown hook already registered above). Headless --slack-only mode keeps
    # the host awake during a long Slack task exactly as the full dashboard does.
    _arm_prevent_sleep_poll(state, port)

    # Boot-to-ready (rec #1): headless API server is bound and ready. Privacy-safe
    # fixed labels only; best-effort.
    # Publish readiness at the exact boundary measured as boot-to-ready.
    state.ready = True
    record_boot_to_ready((time.time() - state.start_time) * 1000.0, server="api")

    return runner, state
