"""Dashboard aiohttp application factory and startup."""

from __future__ import annotations

import asyncio
import errno
import faulthandler
import importlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.apps.backend import start_enabled_app_backends
from kiro_crew.apps.builtins import BUILTIN_NAMES
from kiro_crew.apps.hooks_integration import (
    init_hooks_system,
    on_gateway_shutdown,
    on_gateway_startup,
)
from kiro_crew.apps.manager import cleanup_migrated_builtin, register_builtin_apps
from kiro_crew.autonudge import get_instance as _autonudge_get
from kiro_crew.autonudge_authz import authorize_and_add_nudge
from kiro_crew.browser.setup import migrate_owned_playwright_registration
from kiro_crew.channel_transcript_migration import migrate_channel_transcripts
from kiro_crew.config import data_home
from kiro_crew.config.loader import KiroCrewConfig, refresh_materialized_agents
from kiro_crew.constants import env_flag_enabled
from kiro_crew.dashboard import (
    channel_slots,
    chat,
    handlers,
    handlers_channel,
    handlers_instances,
    handlers_project,
    openai_compat,
    stt_stream,
    tailnet,
    ws,
)
from kiro_crew.dashboard.crash_dump_store import (
    claim_dump_notification,
    dump_age_seconds,
    dump_replay_lines,
    newest_dump_with_stacks,
    open_dump_file,
    rotate_dumps,
)
from kiro_crew.dashboard.handlers.artifacts import (
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
from kiro_crew.dashboard.handlers.auth_refresh import (
    api_auth_logout,
    api_auth_me,
    api_auth_refresh,
)
from kiro_crew.dashboard.handlers.discover import (
    api_skills_discover,
    api_skills_discover_install,
    api_skills_discover_preview,
)
from kiro_crew.dashboard.handlers.knowledge import setup_knowledge_routes
from kiro_crew.dashboard.handlers.link_meta import setup_link_meta_routes
from kiro_crew.dashboard.handlers.mcp_custom import (
    api_mcp_custom_add,
    api_mcp_custom_get,
    api_mcp_custom_update,
)
from kiro_crew.dashboard.handlers.mcp_discover import (
    api_mcp_discover,
    api_mcp_discover_detail,
    api_mcp_discover_install,
)
from kiro_crew.dashboard.handlers.source_providers import (
    api_issue_source,
    api_pull_request_auto_merge,
    api_pull_request_checks,
    api_pull_request_comment,
    api_pull_request_pending_review,
    api_pull_request_ready,
    api_pull_request_reply,
    api_pull_request_resolve,
    api_pull_request_source,
    api_pull_request_status,
    api_pull_request_submit_review,
    api_pull_request_unresolve,
    register_status_delta_sink,
    unregister_status_delta_sink,
)
from kiro_crew.dashboard.handlers.tunnel import api_tunnel_status
from kiro_crew.dashboard.handlers.weixin_qr import setup_weixin_routes
from kiro_crew.dashboard.handlers.worktree import api_worktree_create
from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog
from kiro_crew.dashboard.origin import (
    PROBE_PATHS,
    bind_address_for,
    build_allowed_origins,
    check_host,
    check_origin,
    resolve_dashboard_host,
    should_canonicalize_host,
)
from kiro_crew.dashboard.port_reclaim import (
    FOREIGN_HOLDER,
    HEALTHY_PEER,
    RECLAIMED,
    reclaim_stale_gateway_port,
)
from kiro_crew.dashboard.slowloris import build_hardened_runner
from kiro_crew.dashboard.state import _DEFAULT_PORT, DashboardState
from kiro_crew.dashboard.token_auth import (
    _cookie_port_from_host,
    _is_spa_shell_request,
    register_app_window_paths,
    token_auth_middleware,
    token_embed_parent_port,
    warm_auth_singletons,
)
from kiro_crew.deploy import _register_core_skills as _register_deploy_skills
from kiro_crew.deploy.handlers import register_routes as _register_deploy_routes
from kiro_crew.executors import subprocess_executor
from kiro_crew.hooks import ScriptHookStore, set_global_hook_store
from kiro_crew.instances.registry import InstancesRegistry
from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager, TunnelState
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
    grant_declared_yolo,
    safety_override,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.skills import SkillsLoader, set_pending_staged_hook
from kiro_crew.suggestions import api_suggestions
from kiro_crew.tips import api_tips_feedback, api_tips_next, api_tips_status
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


async def _should_prevent_sleep(state: DashboardState) -> bool:
    """Whether the host should be kept awake right now.

    True only when the user opted in (``dashboard.prevent_sleep``) AND some live
    session has a turn in flight. Reads config live so toggling the flag takes
    effect on the next poll without a restart. Fail-closed: any error resolves to
    "allow sleep" so a config/lookup hiccup can never wedge the machine awake.
    """
    try:
        # KiroCrewConfig.load() does a stat and, on a cache miss, a JSON read +
        # schema validation. On a slow home filesystem that is a blocking call,
        # and this runs on the gateway event loop every poll — offload it so a
        # slow read can never stall chat/heartbeat (no-blocking-call-on-event-loop).
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if not cfg.dashboard.prevent_sleep:
            return False
    except Exception:
        logger.debug("prevent-sleep config read failed", exc_info=True)
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
        "/api/slack/pins",
        "/api/slack/reactions",
        "/api/slack-profile",  # MCP-only (slack_profile tool); no browser caller
        "/api/sessions/summarize",  # MCP-only (list_sessions summarize leg); internal-secret, no browser caller
        "/api/mcp/servers",
    }
)


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
            # API-access audit. Non-critical write (no fail-closed fsync) so
            # it never wedges the event loop.
            sel().log_api_access(
                caller=caller,
                operation=f"{request.method} {request.path}",
                outcome="denied",
                resources=request.path,
                error=f"host header not allowed: {request.headers.get('Host', '')[:100]}",
            )
            raise web.HTTPForbidden(
                text="Host header not allowed.",
                content_type="text/plain",
            )
        return await handler(request)  # type: ignore[operator]

    return host_validation_middleware


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
    "script-src 'self' 'unsafe-inline' "
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
        token = request.query.get("token") or ""
        if not token:
            port_fallback = app.get("port", _DEFAULT_PORT) if app is not None else _DEFAULT_PORT
            cookie_port = _cookie_port_from_host(request, port_fallback)
            token = request.cookies.get(f"mc_token_{cookie_port}", "")
        port = token_embed_parent_port(token)
    if port is None:
        return []
    return [
        f"http://{host}:{port}"
        for host in ("127.0.0.1", "localhost", "[::1]", "kirocrew.localhost")
    ]


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
    frame_ancestors = " ".join(["'self'", *extra_ancestors])
    resp.headers.setdefault(
        "Content-Security-Policy",
        _BASE_CSP.format(
            connect_src_extra=_LOOPBACK_FRAME_SRC,
            frame_src_extra=frame_src_extra,
            frame_ancestors=frame_ancestors,
        ),
    )
    resp.headers.setdefault("Permissions-Policy", _PERMISSIONS_POLICY)
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


def _migrate_playwright_to_proxy() -> None:
    """Converge KiroCrew's own Playwright MCP registration to one canonical server.

    Delegates to :func:`migrate_owned_playwright_registration`, which rewrites a
    legacy or slash-keyed browse entry in ``~/.kiro/settings/mcp.json`` to the
    canonical slash-free alias (``playwright-mcp``) proxy entry and sweeps the
    KiroCrew-generated agent configs so a duplicate proxy entry collapses onto
    the one canonical server. This self-heals an existing machine on a plain
    gateway restart rather than waiting for a full agent rebuild.
    """
    migrate_owned_playwright_registration()


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


def _register_mcp_routes(app: web.Application) -> None:
    """Register API routes used by MCP tools (spawn, lessons, crons, etc.)."""
    app.router.add_post("/api/spawn", handlers.api_spawn)
    app.router.add_post("/api/spawn/lost", handlers.api_spawn_lost)
    app.router.add_post("/api/spawn/mark-collected", handlers.api_spawn_mark_collected)
    # MCP Apps (SEP-1865): embedded app iframe -> gateway tool callback.
    app.router.add_post("/api/mcp-apps/call", handlers.api_mcp_apps_call)
    app.router.add_get("/api/spawn", handlers.api_spawn_list)
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
    app.router.add_get("/api/crons", handlers.api_crons)
    app.router.add_post("/api/crons", handlers.api_crons_create)
    app.router.add_delete("/api/crons", handlers.api_cron_batch_delete)
    app.router.add_get("/api/crons/history", handlers.api_cron_history_all)
    app.router.add_delete("/api/crons/{job_id}", handlers.api_cron_delete)
    app.router.add_patch("/api/crons/{job_id}", handlers.api_cron_update)
    app.router.add_post("/api/crons/{job_id}/enable", handlers.api_cron_enable)
    app.router.add_post("/api/crons/{job_id}/run", handlers.api_cron_run)
    app.router.add_post("/api/crons/{job_id}/cancel", handlers.api_cron_cancel)
    app.router.add_post("/api/crons/{job_id}/to-chat", handlers.api_cron_to_chat)
    app.router.add_post("/api/crons/{job_id}/ack", handlers.api_cron_ack)
    app.router.add_get("/api/crons/{job_id}/history", handlers.api_cron_history)
    app.router.add_get("/api/crons/{job_id}/history/{run_id}", handlers.api_cron_history_detail)
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
    app.router.add_post("/api/browser-event", handlers.api_browser_event)
    app.router.add_post("/api/browser-auth-retry", handlers.api_browser_auth_retry)
    app.router.add_post("/api/browser/frame", handlers.api_browser_frame)
    app.router.add_post("/api/browser/pump-audit", handlers.api_browser_pump_audit)
    app.router.add_post("/api/browser/command", handlers.api_browser_command)
    app.router.add_post("/api/browser/command-drain", handlers.api_browser_command_drain)
    app.router.add_post("/api/browser/command-result", handlers.api_browser_command_result)
    app.router.add_get("/api/browser/config", handlers.api_browser_config_get)
    app.router.add_put("/api/browser/config", handlers.api_browser_config_save)
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
    )

    app.router.add_get("/api/autonudge", api_autonudge_list)
    app.router.add_post("/api/autonudge", api_autonudge_start)
    app.router.add_get("/api/autonudge/slot/{slot_key}", api_autonudge_get)
    app.router.add_patch("/api/autonudge/{loop_id}", api_autonudge_update)
    app.router.add_delete("/api/autonudge/{loop_id}", api_autonudge_delete)

    # Agent questions — blocking question-card round-trip for the ask_question
    # MCP tool. The POST holds open until the user answers, so it must not be
    # wrapped in any short-timeout middleware.
    from kiro_crew.dashboard.handlers.ask_question import (
        api_ask_question,
        api_ask_question_answer,
        api_ask_question_pending,
    )

    app.router.add_post("/api/ask-question", api_ask_question)
    # Registered before the {ask_id} route so the literal path is not captured
    # as an ask_id.
    app.router.add_get("/api/ask-question/pending", api_ask_question_pending)
    app.router.add_post("/api/ask-question/{ask_id}/answer", api_ask_question_answer)

    # Artifacts — persistent, versioned LLM-generated UI
    app.router.add_get("/api/artifacts", api_artifacts_list)

    # Dynamic Workflows (M6) — author, run, monitor, cancel, rerun
    from kiro_crew.dashboard.handlers.workflows import (
        api_workflow_author,
        api_workflow_run,
        api_workflow_run_cancel,
        api_workflow_run_get,
        api_workflow_run_intent,
        api_workflow_run_rerun,
        api_workflow_runs,
    )

    app.router.add_post("/api/workflows/author", api_workflow_author)
    app.router.add_post("/api/workflows/run", api_workflow_run)
    app.router.add_post("/api/workflows/run_intent", api_workflow_run_intent)
    app.router.add_get("/api/workflows/runs", api_workflow_runs)
    app.router.add_get("/api/workflows/runs/{run_id}", api_workflow_run_get)
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
    app.router.add_get("/api/artifacts/session-docs", api_artifact_session_docs)
    app.router.add_post("/api/artifacts/materialize", api_artifact_materialize)
    app.router.add_get("/api/artifacts/publish-providers", api_artifact_publish_providers)
    app.router.add_get("/api/artifacts/{slug}", api_artifact_detail)
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
            # on Windows an owner-only DACL via icacls (fchmod doesn't exist on
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
        return frozenset(
            k[len("dashboard:"):] for k in data if k.startswith("dashboard:")
        )
    except Exception:
        logger.debug("could not read claimed dashboard slots", exc_info=True)
        return frozenset()


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
    """Best-effort owner Slack DM. No-op if Slack/owner are not configured.

    The shared owner-notification exit point to Slack (currently the
    safety-override-expiry path), so the open_dm → post_message →
    swallow-and-log idiom lives in one place.

    Defense-in-depth: because this is the single exit point to Slack for owner
    notifications and is intended for reuse, ``text`` is passed through
    ``redact_exfiltration_urls()`` then ``redact_credentials()`` (same order as
    the rest of the Slack surface) so a future caller that forwards
    LLM/user-derived content can never leak credentials or exfil URLs, even
    though today's callers only pass static constants.
    """
    slack_client = state.slack_client
    owner_id = state.owner_id
    if not (slack_client and owner_id):
        return
    safe_text, _ = redact_exfiltration_urls(text)
    safe_text, _ = redact_credentials(safe_text)
    try:
        dm_channel = await slack_client.open_dm(owner_id)
        await slack_client.post_message(dm_channel, safe_text)
    except Exception:
        logger.debug("Owner Slack DM skipped", exc_info=True)


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
            ssh_compression=_cfg.instances.ssh_compression,
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


def _arm_prevent_sleep_poll(state: DashboardState) -> None:
    """Create the sleep inhibitor and start its poll task on the running loop.

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
                    inhibitor.set_active(await _should_prevent_sleep(state))
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
        try:
            _gw_loop: "asyncio.AbstractEventLoop | None" = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - sync/embedded launch
            _gw_loop = None

        def _on_pending_skill_staged(info: dict) -> None:
            try:
                name = str(info.get("name") or info.get("slug") or "skill")
                slug = str(info.get("slug") or "")
                is_update = info.get("kind") == "update"
                target = str(info.get("target") or "")
                description = str(info.get("description") or "").strip()
                triggers = str(info.get("triggers") or "").strip()
                subject = target or name if is_update else name
                title = (
                    "Skill update awaiting review"
                    if is_update
                    else "New skill awaiting review"
                )
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
                    lines.append(
                        "\n_Bundles executable scripts — review them before approving._"
                    )
                body = "\n".join(lines)
                payload = {
                    "slug": slug,
                    "candidate_kind": "update" if is_update else "new",
                    "target": target,
                }
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
                        "label": "Review" if is_update else "Review skill",
                        "url": review_url,
                    }
                ]

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

                if _gw_loop is not None and not _gw_loop.is_closed():
                    # Safe from the loop thread too — call_soon_threadsafe just
                    # schedules. RuntimeError means the loop is shutting down.
                    try:
                        _gw_loop.call_soon_threadsafe(_emit)
                    except RuntimeError:  # pragma: no cover - loop closing
                        pass
                else:
                    _emit()
            except Exception:
                logger.debug("pending-skill notification failed", exc_info=True)

        set_pending_staged_hook(_on_pending_skill_staged)
    except Exception:
        logger.debug("Could not register pending-skill staged hook", exc_info=True)

    # --- Dynamic Workflows (M6) ---
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

    # Wire script hooks into subagent tool execution path
    if state.subagents is not None:
        state.subagents.hook_store = state._hook_store

    # Visible notice + pct reset when auto-compaction fires on a dashboard session
    state.wire_session_compact_callback()
    # Visible notice when the watchdog recycles a dashboard session (e.g. RSS)
    state.wire_session_recycle_callback()

    app = web.Application(
        client_max_size=60 * 1024 * 1024
    )  # 60 MB: covers 50 MB upload + multipart overhead
    app["state"] = state
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
    state.load_tags()
    app["port"] = port

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
    app.router.add_get("/", handlers.index)
    app.router.add_get("/logo.png", handlers.logo)
    app.router.add_get(
        "/{name:manifest\\.json|sw\\.js|icon-\\d+\\.png|pcm-worklet\\.js}", handlers.pwa_file
    )

    # WebSocket (multiplexed real-time events)
    app.router.add_get("/api/ws", ws.api_ws)

    # Status / system
    app.router.add_get("/api/status", handlers.api_status)
    app.router.add_get("/api/system", handlers.api_system)
    app.router.add_get("/api/stream", handlers.api_stream)
    app.router.add_get("/api/sso-ttl", handlers.api_sso_ttl)
    app.router.add_get("/api/dashboard/branding", handlers.api_branding)
    app.router.add_get("/api/health", handlers.api_health)
    app.router.add_get("/api/live", handlers.api_live)
    app.router.add_get("/api/ready", handlers.api_ready)
    app.router.add_get("/api/theme/boot", handlers.api_theme_boot)
    app.router.add_get("/api/admin/compliance/yolo-status", handlers.api_compliance_yolo_status)
    app.router.add_get(
        "/api/kiro-prerequisite",
        handlers.api_kiro_prerequisite_status,
    )
    # POST, not a flag on the status GET: csrf_middleware skips check_origin for
    # safe methods and sel_audit_middleware logs only mutating ones, so a spec
    # rewrite reached from the GET would be cross-site triggerable and unaudited.
    app.router.add_post(
        "/api/kiro-prerequisite/repair-specs",
        handlers.api_kiro_prerequisite_repair_specs,
    )
    app.router.add_get("/api/governance/channels", handlers.api_governance_channels)

    # Suggestions (pre-computed contextual prompts)
    app.router.add_get("/api/suggestions", api_suggestions)

    # Tips (feature discovery)
    app.router.add_get("/api/tips/next", api_tips_next)
    app.router.add_get("/api/tips/status", api_tips_status)
    app.router.add_post("/api/tips/feedback", api_tips_feedback)

    # Memory
    app.router.add_get("/api/memory/preferences", handlers.api_memory_preferences)
    app.router.add_put("/api/memory/preferences", handlers.api_memory_preferences)
    app.router.add_get("/api/memory/projects", handlers.api_memory_projects)
    app.router.add_put("/api/memory/projects", handlers.api_memory_projects)
    app.router.add_get("/api/memory/history", handlers.api_memory_history)
    app.router.add_put("/api/memory/history", handlers.api_memory_history)
    app.router.add_get("/api/memory/settings", handlers.api_memory_settings)
    app.router.add_put("/api/memory/settings", handlers.api_memory_settings)

    # STT (Speech-to-Text)
    app.router.add_get("/api/config/stt", handlers.api_stt_config)
    app.router.add_put("/api/config/stt", handlers.api_stt_config)
    app.router.add_post("/api/stt/install", handlers.api_stt_install)
    app.router.add_post("/api/stt/transcribe", handlers.api_stt_transcribe)
    app.router.add_get("/api/ws/stt", stt_stream.api_ws_stt)

    # Vector Memory (Semantic)
    app.router.add_get("/api/memory/semantic", handlers.api_memory_semantic)
    app.router.add_put("/api/memory/semantic", handlers.api_memory_semantic_write)
    app.router.add_delete("/api/memory/semantic/{key:.+}", handlers.api_memory_semantic_delete)
    app.router.add_get("/api/memory/events", handlers.api_memory_events)
    app.router.add_get("/api/memory/embedding-status", handlers.api_memory_embedding_status)
    app.router.add_post("/api/memory/enable-embeddings", handlers.api_memory_enable_embeddings)
    app.router.add_post("/api/memory/embedding-model", handlers.api_memory_embedding_model)
    app.router.add_post("/api/memory/disable-embeddings", handlers.api_memory_disable_embeddings)
    app.router.add_get("/api/memory/episodic/search", handlers.api_memory_episodic_search)
    app.router.add_get("/api/memory/episodic", handlers.api_memory_episodic_list)
    app.router.add_delete("/api/memory/episodic/{id}", handlers.api_memory_episodic_delete)
    app.router.add_get("/api/memory/stats", handlers.api_memory_stats)
    app.router.add_post("/api/memory/migrate", handlers.api_memory_migrate)
    app.router.add_post("/api/memory/import", handlers.api_memory_import)
    app.router.add_get("/api/memory/context-preview", handlers.api_memory_context_preview)
    app.router.add_post("/api/memory/consolidate", handlers.api_memory_consolidate)
    app.router.add_get("/api/session/archive", handlers.api_session_archive_list)
    app.router.add_get("/api/session/archive/{name}", handlers.api_session_archive_read)
    app.router.add_get("/api/memory/observability", handlers.api_memory_observability)
    app.router.add_get("/api/memory/graph", handlers.api_memory_graph)
    app.router.add_post("/api/memory/promote", handlers.api_memory_promote)

    # Crons, lessons, spawn, taskrunner, send-message, notifications
    # are registered via _register_mcp_routes() above.

    # Slack settings (dashboard-only, NOT in _register_mcp_routes: that set is
    # also mounted on the token-less API-only server, and these endpoints
    # write credentials / expose config state, so they must sit behind the
    # dashboard's token auth in addition to the direct-local write gate).
    app.router.add_get("/api/slack/config", handlers.api_slack_config_get)
    app.router.add_put("/api/slack/config", handlers.api_slack_config_save)
    app.router.add_get("/api/slack/manifest", handlers.api_slack_manifest)
    app.router.add_get("/api/discord/config", handlers.api_discord_config_get)
    app.router.add_put("/api/discord/config", handlers.api_discord_config_save)
    app.router.add_get("/api/telegram/config", handlers.api_telegram_config_get)
    app.router.add_put("/api/telegram/config", handlers.api_telegram_config_save)
    app.router.add_get("/api/webex/config", handlers.api_webex_config_get)
    app.router.add_put("/api/webex/config", handlers.api_webex_config_save)
    app.router.add_get("/api/wecom/config", handlers.api_wecom_config_get)
    app.router.add_put("/api/wecom/config", handlers.api_wecom_config_save)
    # Microsoft Teams: inbound Bot Framework webhook (self-authenticating via
    # JWT; exempt from the cookie gate) + read-only status for the settings UI.
    app.router.add_post("/api/messaging/teams", handlers.api_teams_activity)
    app.router.add_get("/api/teams/config", handlers.api_teams_config_get)
    app.router.add_put("/api/teams/config", handlers.api_teams_config_save)

    # Script Hooks
    app.router.add_get("/api/hooks", handlers.api_hooks)
    app.router.add_get("/api/kiro-hooks", handlers.api_kiro_hooks)
    app.router.add_post("/api/hooks", handlers.api_hooks_create)
    app.router.add_put("/api/hooks/{hook_id}", handlers.api_hook_detail)
    app.router.add_delete("/api/hooks/{hook_id}", handlers.api_hook_detail)
    app.router.add_post("/api/hooks/{hook_id}/toggle", handlers.api_hook_toggle)
    app.router.add_post("/api/hooks/{hook_id}/test", handlers.api_hook_test)

    # Inbound webhook management (dashboard-authed — the webhook token itself
    # only ever authenticates POST /api/hooks/agent, never these).
    app.router.add_get("/api/webhooks", handlers.api_webhooks)
    app.router.add_post("/api/webhooks/tokens", handlers.api_webhook_token_create)
    app.router.add_delete("/api/webhooks/tokens/{token_id}", handlers.api_webhook_token_delete)
    app.router.add_delete("/api/webhooks/contexts/{hook_id}", handlers.api_webhook_context_delete)
    app.router.add_post("/api/webhooks/test", handlers.api_webhook_test)
    app.router.add_post("/api/webhooks/switch", handlers.api_webhooks_switch)

    # Prompts (Agent SOPs)
    app.router.add_get("/api/prompts", handlers.api_prompts)
    app.router.add_get("/api/prompts/{name:.+}", handlers.api_prompt_detail)

    # Skills (CRUD + directory browser).  The browser routes use a ``/-/``
    # separator (GitLab-style) before tree/file so they can't collide with a
    # nested skill whose own last path segment is literally ``tree`` or
    # ``file`` (e.g. ``utils/tree`` → ``GET /api/skills/utils/tree`` is the
    # detail endpoint, not the browser).  They're still registered before the
    # catch-all {name:.+} so aiohttp reaches them first.
    app.router.add_get("/api/skills", handlers.api_skills)
    app.router.add_post("/api/skills", handlers.api_skills_create)
    # Multi-provider skill discovery (skills.sh REST browser)
    app.router.add_get("/api/skills/-/discover", api_skills_discover)
    app.router.add_get("/api/skills/-/discover/preview", api_skills_discover_preview)
    app.router.add_post("/api/skills/-/discover/install", api_skills_discover_install)
    # Auto-skill pending-approval queue + pin (v2). Registered before the
    # catch-all {name:.+} so the ``-`` sentinel paths resolve first.
    app.router.add_get("/api/skills/-/pending", handlers.api_skills_pending)
    app.router.add_get("/api/skills/-/pending/{slug}", handlers.api_skill_pending_detail)
    app.router.add_post("/api/skills/-/pending/{slug}/approve", handlers.api_skill_pending_approve)
    app.router.add_post("/api/skills/-/pending/{slug}/dismiss", handlers.api_skill_pending_dismiss)
    app.router.add_post("/api/skills/-/pin", handlers.api_skill_pin)
    app.router.add_post(
        "/api/skills/-/inject-on-trigger", handlers.api_skill_inject_on_trigger
    )
    # Skill context budget (read-only cost analysis with alias folding).
    app.router.add_get("/api/skills/-/budget", handlers.api_skills_budget)
    app.router.add_get("/api/skills/{name:.+}/-/tree", handlers.api_skill_tree)
    app.router.add_get("/api/skills/{name:.+}/-/file", handlers.api_skill_file)
    app.router.add_get("/api/skills/{name:.+}", handlers.api_skill_detail)
    app.router.add_put("/api/skills/{name:.+}", handlers.api_skill_detail)
    app.router.add_delete("/api/skills/{name:.+}", handlers.api_skill_detail)

    # Kiro steering files (~/.kiro/steering + <project>/.kiro/steering).  Plain
    # markdown documents, so no tree browser — the key is ``<source>/<relpath>``
    # and the fixed list/create route is registered before the catch-all
    # {key:.+} detail routes so aiohttp reaches it first.
    app.router.add_get("/api/steering", handlers.api_steering)
    app.router.add_post("/api/steering", handlers.api_steering_create)
    app.router.add_get("/api/steering/{key:.+}", handlers.api_steering_detail)
    app.router.add_put("/api/steering/{key:.+}", handlers.api_steering_detail)
    app.router.add_delete("/api/steering/{key:.+}", handlers.api_steering_detail)

    # Custom Themes (CRUD)
    app.router.add_get("/api/themes", handlers.api_themes)
    app.router.add_post("/api/themes", handlers.api_themes_create)
    app.router.add_post("/api/themes/install", handlers.api_themes_install)
    app.router.add_get("/api/themes/{slug}", handlers.api_theme_detail)
    app.router.add_put("/api/themes/{slug}", handlers.api_theme_detail)
    app.router.add_delete("/api/themes/{slug}", handlers.api_theme_detail)
    # Installed-theme asset serving (L1/L2)
    app.router.add_get("/api/theme/{slug}/assets/{path:.+}", handlers.api_theme_asset)
    app.router.add_get("/api/theme/{slug}/overlay/{id}", handlers.api_theme_overlay)
    app.router.add_get("/api/theme/{slug}/topbar/{mode}", handlers.api_theme_topbar)

    # Agent config
    app.router.add_get("/api/agent/config", handlers.api_agent_config)
    app.router.add_put("/api/agent/config", handlers.api_agent_config)
    app.router.add_get("/api/config/default-agent", handlers.api_default_agent)
    app.router.add_put("/api/config/default-agent", handlers.api_default_agent)
    app.router.add_get("/api/config/schema", handlers.api_config_schema)
    app.router.add_get("/api/config/kirocrew", handlers.api_kirocrew_config)
    app.router.add_put("/api/config/kirocrew", handlers.api_kirocrew_config)
    app.router.add_patch("/api/config/kirocrew", handlers.api_kirocrew_config_patch)
    app.router.add_get("/api/config/theme", handlers.api_theme_config)
    app.router.add_put("/api/config/theme", handlers.api_theme_config)
    app.router.add_get(
        "/api/onboarding/import/scan",
        handlers.api_onboarding_import_scan,
    )
    app.router.add_post(
        "/api/onboarding/import/apply",
        handlers.api_onboarding_import_apply,
    )
    app.router.add_put(
        "/api/onboarding/import/state",
        handlers.api_onboarding_import_state,
    )
    app.router.add_get("/api/dashboard/config", handlers.api_dashboard_config)
    app.router.add_put("/api/dashboard/config", handlers.api_dashboard_config)

    # MCP servers
    app.router.add_get("/api/mcp", handlers.api_mcp_servers)
    app.router.add_get("/api/mcp/scopes", handlers.api_mcp_global_scopes)
    app.router.add_get("/api/mcp/active", handlers.api_mcp_active)
    # Multi-provider MCP discovery (official registry + optional edition capability provider)
    app.router.add_get("/api/mcp/discover", api_mcp_discover)
    app.router.add_get("/api/mcp/discover/detail", api_mcp_discover_detail)
    app.router.add_post("/api/mcp/discover/install", api_mcp_discover_install)
    # Manual MCP server management (Add Custom modal + per-server JSON edit)
    app.router.add_post("/api/mcp/custom", api_mcp_custom_add)
    app.router.add_get("/api/mcp/custom/{name}", api_mcp_custom_get)
    app.router.add_put("/api/mcp/custom/{name}", api_mcp_custom_update)
    app.router.add_post("/api/mcp/probe", handlers.api_mcp_probe)
    app.router.add_get("/api/mcp/probe", handlers.api_mcp_probe_cached)
    app.router.add_post("/api/mcp/sync", handlers.api_mcp_sync)
    app.router.add_post("/api/mcp/apply", handlers.api_mcp_apply)
    app.router.add_post("/api/mcp/toggle", handlers.api_mcp_toggle)
    app.router.add_post("/api/mcp/toggle-tool", handlers.api_mcp_toggle_tool)
    app.router.add_post("/api/mcp/toggle-all", handlers.api_mcp_toggle_all)
    app.router.add_post("/api/mcp/remove", handlers.api_mcp_remove)
    app.router.add_post("/api/mcp/oauth/relay", handlers.api_mcp_oauth_relay)
    # REST-style MCP server registration (App Kit)
    app.router.add_put("/api/mcp/servers/{name}", handlers.api_mcp_server_detail)
    app.router.add_delete("/api/mcp/servers/{name}", handlers.api_mcp_server_detail)
    # Shared MCP gateway (pool)
    app.router.add_get("/api/mcp-gateway/status", handlers.api_mcp_gateway_status)
    app.router.add_post("/api/mcp-gateway/enable", handlers.api_mcp_gateway_enable)
    app.router.add_get("/api/mcp-gateway/metrics", handlers.api_mcp_gateway_metrics)
    app.router.add_get("/api/mcp-gateway/servers", handlers.api_mcp_gateway_servers)
    app.router.add_post("/api/mcp-gateway/servers/poolable", handlers.api_mcp_gateway_set_poolable)
    # AIM integration
    app.router.add_get("/api/capability/mcp", handlers.api_capability_mcp_list)
    app.router.add_post("/api/capability/mcp/install", handlers.api_capability_mcp_install)
    app.router.add_post("/api/capability/mcp/uninstall", handlers.api_capability_mcp_uninstall)
    app.router.add_get("/api/capability/skills", handlers.api_capability_skills_list)
    app.router.add_post("/api/capability/skills/install", handlers.api_capability_skills_install)
    app.router.add_post(
        "/api/capability/skills/uninstall", handlers.api_capability_skills_uninstall
    )

    # Chat
    app.router.add_post("/api/chat", chat.api_chat)
    app.router.add_post("/api/source/pull-request", api_pull_request_source)
    app.router.add_post("/api/source/pull-request/checks", api_pull_request_checks)
    app.router.add_post("/api/source/pull-request/status", api_pull_request_status)
    app.router.add_post("/api/source/pull-request/resolve", api_pull_request_resolve)
    app.router.add_post("/api/source/pull-request/unresolve", api_pull_request_unresolve)
    app.router.add_post("/api/source/pull-request/reply", api_pull_request_reply)
    app.router.add_post("/api/source/pull-request/comment", api_pull_request_comment)
    app.router.add_post("/api/source/pull-request/auto-merge", api_pull_request_auto_merge)
    app.router.add_post("/api/source/pull-request/ready", api_pull_request_ready)
    app.router.add_post(
        "/api/source/pull-request/pending-review", api_pull_request_pending_review
    )
    app.router.add_post(
        "/api/source/pull-request/submit-review", api_pull_request_submit_review
    )
    app.router.add_post("/api/source/issue", api_issue_source)
    app.router.add_get("/api/chat/slots", chat.api_chat_slots)
    app.router.add_post("/api/chat/slots", chat.api_chat_slot_create)
    app.router.add_post("/api/chat/slots/cleanup", chat.api_chat_slots_cleanup)
    app.router.add_post("/api/chat/slots/model", chat.api_chat_slots_model)
    app.router.add_get("/api/chat/slots/{slot}", chat.api_chat_slot_detail)
    app.router.add_post("/api/chat/slots/{slot}/stop", chat.api_chat_slot_stop)
    app.router.add_post("/api/chat/slots/{slot}/interrupt", chat.api_chat_slot_interrupt)
    # Deliberately NOT /resume — that path is already taken by "open a history
    # session into a tab" (api_chat_slot_resume) and means something else.
    app.router.add_post("/api/chat/slots/{slot}/continue", chat.api_chat_slot_continue)
    app.router.add_delete(
        "/api/chat/slots/{slot}/queue/{queue_id}", chat.api_chat_slot_queue_cancel
    )
    app.router.add_patch("/api/chat/slots/{slot}/queue/{queue_id}", chat.api_chat_slot_queue_edit)
    app.router.add_put("/api/chat/slots/{slot}/queue/order", chat.api_chat_slot_queue_reorder)
    app.router.add_delete("/api/chat/slots/{slot}", chat.api_chat_slot_delete)
    app.router.add_post("/api/chat/slots/{slot}/agent", chat.api_chat_slot_agent)

    # Optimizer
    app.router.add_post("/api/optimizer/optimize", handlers.handle_optimize)
    app.router.add_post("/api/chat/slots/{slot}/model", chat.api_chat_slot_model)
    app.router.add_post(
        "/api/chat/slots/{slot}/reasoning-effort", chat.api_chat_slot_reasoning_effort
    )
    app.router.add_post("/api/chat/slots/{slot}/workspace", chat.api_chat_slot_workspace)
    app.router.add_post("/api/chat/slots/{slot}/project", chat.api_chat_slot_project)
    # Follow-up suggestion card (suggest_followup MCP tool -> card below composer)
    app.router.add_post("/api/chat/slots/{slot}/followup", chat.api_chat_slot_followup)
    app.router.add_post("/api/worktree/create", api_worktree_create)
    app.router.add_get("/api/recent-projects", chat.api_recent_projects)
    app.router.add_patch("/api/chat/slots/{slot}/color", chat.api_chat_slot_color)
    # Context injection (App Kit — silent background context)
    app.router.add_post("/api/chat/slots/{slot}/context", chat.api_chat_slot_context)
    app.router.add_post("/api/chat/slots/{slot}/fork", chat.api_chat_slot_fork)
    app.router.add_post("/api/chat/slots/{slot}/side/open", handlers.api_side_open)
    app.router.add_post("/api/chat/slots/{slot}/side/turn", handlers.api_side_turn)
    app.router.add_post("/api/chat/slots/{slot}/side/close", handlers.api_side_close)
    # Workspaces
    app.router.add_get("/api/workspaces", handlers.api_workspaces)
    app.router.add_post("/api/workspaces", handlers.api_workspaces_create)
    app.router.add_put("/api/workspaces/{name}", handlers.api_workspaces_update)
    app.router.add_delete("/api/workspaces/{name}", handlers.api_workspaces_delete)
    # Agents
    app.router.add_get("/api/agents/installed", handlers.api_agents_installed)
    app.router.add_get("/api/models", handlers.api_models)
    app.router.add_get("/api/effort-levels", handlers.api_effort_levels)
    app.router.add_get("/api/slash-commands", handlers.api_slash_commands)
    app.router.add_get("/api/agents/detail/{name}", handlers.api_agent_detail)
    app.router.add_patch("/api/agents/detail/{name}", handlers.api_agent_detail)
    app.router.add_delete("/api/agents/detail/{name}", handlers.api_agent_detail)
    # KiroCrew Agent CRUD
    app.router.add_get("/api/agents", handlers.api_kirocrew_agents)
    app.router.add_get(
        "/api/agents/resolved-model", handlers.api_kirocrew_agent_resolved_model
    )
    app.router.add_post("/api/agents", handlers.api_kirocrew_agents_create)
    app.router.add_post("/api/agents/sync", handlers.api_kirocrew_agents_sync)
    app.router.add_put("/api/agents/{name}", handlers.api_kirocrew_agent_update)
    app.router.add_delete("/api/agents/{name}", handlers.api_kirocrew_agent_delete)
    # Edition capability agents
    app.router.add_get("/api/capability/agents", handlers.api_capability_agents_list)
    app.router.add_post("/api/capability/agents/install", handlers.api_capability_agents_install)
    app.router.add_post(
        "/api/capability/agents/uninstall", handlers.api_capability_agents_uninstall
    )
    # Edition capability plugins (agent-client integrations + drift reconcile)
    app.router.add_get("/api/capability/plugins", handlers.api_capability_plugins_list)
    app.router.add_post("/api/capability/plugins/sync", handlers.api_capability_plugins_sync)
    # Session workspace (Orchestrated Chat)
    app.router.add_get("/api/sessions/{id}/agents", handlers.api_session_agents_list)
    app.router.add_get("/api/sessions/{id}/agents/{agent_id}", handlers.api_session_agent_result)
    app.router.add_get(
        "/api/sessions/{id}/agents/{agent_id}/stream", handlers.api_session_agent_stream
    )
    app.router.add_get("/api/capability/mcp/registry", handlers.api_capability_mcp_registry)
    app.router.add_post("/api/chat/slots/{slot}/resume", chat.api_chat_slot_resume)
    app.router.add_post("/api/chat/slots/{slot}/approve", chat.api_chat_slot_approve)
    app.router.add_post("/api/chat/slots/{slot}/plan-action", chat.api_chat_plan_action)
    app.router.add_post("/api/chat/mode", chat.api_chat_mode)
    app.router.add_post("/api/chat/nav/resolve-links", chat.api_chat_nav_resolve_links)
    app.router.add_post("/api/chat/slots/{slot}/generate-title", chat.api_chat_slot_generate_title)
    app.router.add_patch("/api/chat/slots/{slot}/title", chat.api_chat_slot_rename)
    app.router.add_post("/api/chat/slots/{slot}/regenerate", chat.api_chat_slot_regenerate)
    app.router.add_post("/api/chat/slots/{slot}/switch-variant", chat.api_chat_slot_switch_variant)
    app.router.add_post("/api/chat/slots/{slot}/edit-resend", chat.api_chat_slot_edit_resend)
    app.router.add_post("/api/chat/slots/{slot}/rewind", chat.api_chat_slot_rewind)
    # Folders
    app.router.add_get("/api/chat/folders", chat.api_chat_folders)
    app.router.add_post("/api/chat/folders", chat.api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", chat.api_chat_folder_update)
    app.router.add_delete("/api/chat/folders/{id}", chat.api_chat_folder_delete)
    app.router.add_patch("/api/chat/slots/{slot}/folder", chat.api_chat_slot_folder)
    app.router.add_patch("/api/chat/slots/{slot}/pin", chat.api_chat_slot_pin)
    app.router.add_patch("/api/chat/slots/{slot}/mode", chat.api_chat_slot_mode)
    # Tags
    app.router.add_get("/api/chat/tags", chat.api_chat_tags)
    app.router.add_post("/api/chat/tags", chat.api_chat_tag_create)
    app.router.add_patch("/api/chat/tags/{id}", chat.api_chat_tag_update)
    app.router.add_delete("/api/chat/tags/{id}", chat.api_chat_tag_delete)
    app.router.add_put("/api/chat/slots/{slot}/tags", chat.api_chat_slot_tags)
    app.router.add_post("/api/chat/slots/{slot}/drop", chat.api_chat_slot_drop)
    app.router.add_get("/api/chat/tag-columns", chat.api_chat_tag_columns)
    app.router.add_post("/api/chat/tag-columns", chat.api_chat_tag_column_create)
    app.router.add_put("/api/chat/tag-columns/order", chat.api_chat_tag_columns_reorder)
    app.router.add_patch("/api/chat/tag-columns/{id}", chat.api_chat_tag_column_update)
    app.router.add_delete("/api/chat/tag-columns/{id}", chat.api_chat_tag_column_delete)
    app.router.add_post("/api/voice/synthesize", chat.api_voice_synthesize)
    app.router.add_get("/api/voice/config", chat.api_voice_config)
    app.router.add_put("/api/voice/config", chat.api_voice_config)
    app.router.add_get("/api/voice/voices", chat.api_voice_voices)
    app.router.add_post("/api/chat/slots/{slot}/handoff", chat.api_chat_slot_handoff)
    app.router.add_get("/api/handoff-channels", chat.api_handoff_channels)
    app.router.add_post("/api/chat/slots/{slot}/slack-link", chat.api_chat_slot_slack_link)
    app.router.add_post("/api/chat/slots/{slot}/slack-unlink", chat.api_chat_slot_slack_unlink)
    app.router.add_post("/api/chat/slots/{slot}/mirror-link", chat.api_chat_slot_mirror_link)
    app.router.add_post("/api/chat/slots/{slot}/mirror-unlink", chat.api_chat_slot_mirror_unlink)
    app.router.add_get("/api/chat/channel-targets", chat.api_channel_targets)
    app.router.add_get("/api/slack/channels", chat.api_slack_channels)

    # OpenAI-compatible API
    app.router.add_post("/v1/chat/completions", openai_compat.api_completions)

    # Task runner (MCP routes via _register_mcp_routes; dashboard-only routes below)
    app.router.add_post("/api/taskrunner/plan", handlers.api_taskrunner_plan)
    app.router.add_post("/api/taskrunner/plan/cancel", handlers.api_taskrunner_plan_cancel)
    app.router.add_post("/api/taskrunner/from-chat", handlers.api_taskrunner_from_chat)
    app.router.add_delete("/api/taskrunner/{task_id}", handlers.api_taskrunner_delete)
    app.router.add_patch("/api/taskrunner/{task_id}/name", handlers.api_taskrunner_rename)
    app.router.add_patch(
        "/api/taskrunner/{task_id}/tasks/{index}", handlers.api_taskrunner_update_task
    )
    app.router.add_post("/api/taskrunner/{task_id}/retry", handlers.api_taskrunner_retry)
    app.router.add_post("/api/taskrunner/{task_id}/pause", handlers.api_taskrunner_pause)
    app.router.add_post("/api/taskrunner/{task_id}/to-chat", handlers.api_taskrunner_to_chat)
    app.router.add_get(
        "/api/taskrunner/{task_id}/plan-context", handlers.api_taskrunner_plan_context
    )
    app.router.add_get("/api/taskrunner/{task_id}/plan.yaml", handlers.api_taskrunner_export_yaml)
    app.router.add_put("/api/taskrunner/{task_id}/plan", handlers.api_taskrunner_update_plan)
    app.router.add_post("/api/taskrunner/{task_id}/execute", handlers.api_taskrunner_execute_plan)
    app.router.add_post("/api/reveal", handlers.api_reveal_path)
    app.router.add_get("/api/file-read", handlers.api_file_read)
    app.router.add_get("/api/file-download", handlers.api_file_download)
    app.router.add_get("/api/file-raw", handlers.api_file_raw)
    app.router.add_get("/api/file-watch", handlers.api_file_watch)
    app.router.add_post("/api/file-write", handlers.api_file_write)
    app.router.add_get("/api/file-diff", handlers.api_file_diff)
    app.router.add_get("/api/file-search", handlers.api_file_search)
    app.router.add_get("/api/browse-dirs", handlers.api_browse_dirs)
    app.router.add_get("/api/browse-files", handlers.api_browse_files)
    app.router.add_get("/api/project/git", handlers.api_project_git)
    app.router.add_post("/api/upload", handlers.api_upload)
    app.router.add_post("/api/upload/file", handlers.api_upload_file)
    app.router.add_post("/api/slack/upload-file", handlers.api_slack_upload_file)
    app.router.add_post("/api/slack/pins", handlers.api_slack_pins)
    app.router.add_post("/api/slack/reactions", handlers.api_slack_reactions)
    app.router.add_post("/api/chat/slots/{name}/slack-link", chat.api_chat_slot_slack_link)
    app.router.add_post("/api/chat/slots/{name}/slack-unlink", chat.api_chat_slot_slack_unlink)
    app.router.add_post("/api/chat/slots/{name}/mirror-link", chat.api_chat_slot_mirror_link)
    app.router.add_post("/api/chat/slots/{name}/mirror-unlink", chat.api_chat_slot_mirror_unlink)
    app.router.add_get("/api/slack/channels", chat.api_slack_channels)
    app.router.add_post("/api/outbox/notify", handlers.api_outbox_notify)
    app.router.add_get("/api/outbox", handlers.api_outbox_list)
    app.router.add_get("/api/outbox/{filename}", handlers.api_outbox_download)
    app.router.add_post("/api/screenshot", handlers.api_screenshot)

    # Diagnostics / "Report a Problem" (redacted support bundle)
    app.router.add_post("/api/diagnostics/collect", handlers.api_diagnostics_collect)
    app.router.add_get(
        "/api/diagnostics/download/{filename}", handlers.api_diagnostics_download
    )

    # Portability (export/import config+memory as zip)
    app.router.add_get("/api/portability/export", handlers.api_portability_export)
    app.router.add_post("/api/portability/import", handlers.api_portability_import)
    app.router.add_post("/api/portability/preview", handlers.api_portability_preview)

    # SSO login WS: an edition may supply the real login handler (CPP
    # DashboardContributor.sso_login_handler); the public Default returns None so the
    # built-in stub stays bound. Fail-closed via the canonical safe_context_call.
    _sso_login_handler = (
        safe_context_call(
            lambda: current_context().dashboard.sso_login_handler(),
            fallback=None,
            log_message="dashboard.sso_login_handler lookup failed; using built-in stub",
        )
        or handlers.api_sso_login_ws
    )
    app.router.add_get("/api/sso-login", _sso_login_handler)
    # Terminal (CLI panel)
    app.router.add_get("/api/ws/terminal/{session_id}", handlers.api_terminal_ws)
    app.router.add_post("/api/terminal/sessions", handlers.api_terminal_create)
    app.router.add_get("/api/terminal/sessions", handlers.api_terminal_list)
    app.router.add_post("/api/terminal/redact", handlers.api_terminal_redact)
    app.router.add_post("/api/terminal/complete", handlers.api_terminal_complete)
    app.router.add_delete("/api/terminal/sessions/{session_id}", handlers.api_terminal_delete)
    app.router.add_get("/api/taskrunner/refine", handlers.api_taskrunner_refine_status)
    app.router.add_post("/api/taskrunner/refine", handlers.api_taskrunner_refine)
    app.router.add_post("/api/taskrunner/refine/cancel", handlers.api_taskrunner_refine_cancel)
    app.router.add_post("/api/taskrunner/refine/answer", handlers.api_taskrunner_refine_answer)

    # Projects
    app.router.add_get("/api/projects", handlers_project.api_projects_list)
    app.router.add_get("/api/projects/{id}", handlers_project.api_project_get)
    app.router.add_post("/api/projects", handlers_project.api_project_create)
    app.router.add_put("/api/projects/{id}", handlers_project.api_project_update)
    app.router.add_delete("/api/projects/{id}", handlers_project.api_project_delete)
    app.router.add_get("/api/activities", handlers_project.api_activities_list)
    app.router.add_post("/api/comments", handlers_project.api_comment_add)
    app.router.add_get("/api/comments", handlers_project.api_comments_list)
    app.router.add_delete("/api/comments/{id}", handlers_project.api_comment_delete)

    # Channels
    app.router.add_get("/api/channels/presets", handlers_channel.api_channel_presets)
    app.router.add_get("/api/channels", handlers_channel.api_channels_list)
    app.router.add_post("/api/channels", handlers_channel.api_channel_create)
    app.router.add_get("/api/channels/{id}", handlers_channel.api_channel_get)
    app.router.add_delete("/api/channels/{id}", handlers_channel.api_channel_close)
    app.router.add_post(
        "/api/channels/{id}/clear-context", handlers_channel.api_channel_clear_context
    )
    app.router.add_post("/api/channels/{id}/messages", handlers_channel.api_channel_post)
    app.router.add_post("/api/channels/{id}/agents", handlers_channel.api_channel_add_agent)
    app.router.add_patch(
        "/api/channels/{id}/agents/{aid}", handlers_channel.api_channel_update_agent
    )
    app.router.add_delete(
        "/api/channels/{id}/agents/{aid}", handlers_channel.api_channel_dismiss_agent
    )
    app.router.add_post(
        "/api/channels/{id}/agents/{aid}/wake", handlers_channel.api_channel_wake_agent
    )
    app.router.add_post(
        "/api/channels/{id}/agents/{aid}/approve", handlers_channel.api_channel_approve_agent
    )

    # OAuth-style refresh tokens for dashboard auth. POST /api/auth/refresh and
    # POST /api/auth/logout self-authenticate via the refresh cookie (the
    # token_auth middleware exempts them); GET /api/auth/me is gated by the
    # standard access-cookie auth.
    app.router.add_get("/api/auth/me", api_auth_me)
    app.router.add_post("/api/auth/refresh", api_auth_refresh)
    app.router.add_post("/api/auth/logout", api_auth_logout)

    # Instances (multi-instance management) — owner-only, gated by instances.enabled
    app.router.add_get("/api/instances", handlers_instances.api_instances_list)
    app.router.add_post("/api/instances", handlers_instances.api_instances_add)
    app.router.add_patch("/api/instances/{id}", handlers_instances.api_instances_update)
    app.router.add_delete("/api/instances/{id}", handlers_instances.api_instances_remove)
    app.router.add_get("/api/instances/{id}/status", handlers_instances.api_instances_status)
    app.router.add_post("/api/instances/{id}/connect", handlers_instances.api_instances_connect)
    app.router.add_post(
        "/api/instances/{id}/refresh-token", handlers_instances.api_instances_refresh_token
    )
    app.router.add_post(
        "/api/instances/{id}/disconnect", handlers_instances.api_instances_disconnect
    )
    app.router.add_post("/api/instances/{id}/restart", handlers_instances.api_instances_restart)

    # Misc (notifications GET/clear and send-message via _register_mcp_routes)
    app.router.add_get("/api/notifications", handlers.api_notifications)
    app.router.add_delete("/api/notifications", handlers.api_notification_delete)
    app.router.add_post("/api/notifications/ack", handlers.api_notification_ack)
    app.router.add_post("/api/notifications/unack", handlers.api_notification_unack)
    app.router.add_post("/api/notifications/ack-all", handlers.api_notifications_ack_all)
    app.router.add_get("/api/notifications/channels", handlers.api_notification_channels)
    app.router.add_put(
        "/api/notifications/channels/settings", handlers.api_notification_channel_settings
    )
    app.router.add_get("/api/update/check", handlers.api_update_check)
    app.router.add_get("/api/changelog", handlers.api_changelog)
    app.router.add_post("/api/update", handlers.api_update_apply)
    app.router.add_post("/api/update/auto", handlers.api_update_auto)
    app.router.add_post("/api/update/cancel", handlers.api_update_cancel)
    # Only expose the simulation endpoint in dev/debug environments
    _is_dev_env = os.environ.get("KIROCREW_HOME", "").endswith("-dev")
    if _is_dev_env or env_flag_enabled("KIROCREW_DEV_MODE"):
        app.router.add_post("/api/update/simulate", handlers.api_update_simulate)
    app.router.add_get("/api/sessions", handlers.api_sessions)
    app.router.add_delete("/api/sessions", handlers.api_sessions_clear)
    app.router.add_get("/api/sessions/context", handlers.api_sessions_context)
    app.router.add_get("/api/sessions/memory", handlers.api_sessions_memory)
    app.router.add_get("/api/sessions/health", handlers.api_sessions_health)
    app.router.add_get("/api/sessions/usage", handlers.api_sessions_usage)
    app.router.add_get("/api/usage/kiro", handlers.api_kiro_usage)
    app.router.add_get("/api/usage", handlers.api_usage)
    app.router.add_get("/api/telemetry/startup", handlers.api_telemetry_startup)
    app.router.add_get("/api/telemetry/context-trace", handlers.api_context_trace)
    app.router.add_get("/api/telemetry/beacon", handlers.api_beacon_status)
    app.router.add_get("/api/tailnet/status", handlers.api_tailnet_status)
    app.router.add_post("/api/sessions/restart", handlers.api_sessions_restart)
    # NOTE: /search must be registered before /{key} to avoid the path param catching "search"
    app.router.add_get("/api/sessions/search", handlers.api_sessions_search)
    app.router.add_post("/api/sessions/summarize", handlers.api_sessions_summarize)
    app.router.add_get("/api/sessions/{key}", handlers.api_session_detail)
    app.router.add_delete("/api/sessions/{key}", handlers.api_session_delete)
    app.router.add_get("/api/logs", handlers.api_logs)
    app.router.add_get("/api/logs/level", handlers.api_log_level_get)
    app.router.add_post("/api/logs/level", handlers.api_log_level)
    app.router.add_get("/api/sel/events", handlers.api_sel_events)
    app.router.add_get("/api/sel/verify", handlers.api_sel_verify)
    app.router.add_get("/api/security/stats", handlers.api_security_stats)
    app.router.add_get("/api/security/posture", handlers.api_security_posture)
    app.router.add_get("/api/security/denied-commands", handlers.api_denied_commands_list)
    app.router.add_patch(
        "/api/security/denied-commands/disable-all", handlers.api_denied_commands_disable_all
    )
    app.router.add_patch(
        "/api/security/denied-commands/builtins/{id}", handlers.api_denied_command_builtin_toggle
    )
    app.router.add_post("/api/security/denied-commands/user", handlers.api_denied_command_user_add)
    app.router.add_patch(
        "/api/security/denied-commands/user/{id}", handlers.api_denied_command_user_toggle
    )
    app.router.add_delete(
        "/api/security/denied-commands/user/{id}", handlers.api_denied_command_user_delete
    )
    # Per-app third-party execution grants (Settings > Security opt-IN). The
    # blanket flag is a PUT on a fixed sub-path; grant/revoke are POST/DELETE on
    # {name}, so the two never collide on method+path.
    app.router.add_get("/api/security/trusted-apps", handlers.api_trusted_apps_list)
    app.router.add_put("/api/security/trusted-apps/allow-all", handlers.api_trusted_apps_allow_all)
    app.router.add_post("/api/security/trusted-apps/{name}", handlers.api_trusted_app_grant)
    app.router.add_delete("/api/security/trusted-apps/{name}", handlers.api_trusted_app_revoke)
    # Read-only governance policy viewer — effective Level-1 ∩ Level-2 ceiling
    # across every governed scope (no write path; the ceiling is file-authored).
    app.router.add_get("/api/governance/policy", handlers.api_governance_policy)

    # Computer use (Settings > Computer Use). Browser-called and cookie-authed,
    # like the browser-config pair — deliberately NOT in
    # ``_STRICT_INTERNAL_API_PATHS``. The machine-only ``invoke`` leg IS in that
    # set and is registered in ``_register_mcp_routes``.
    app.router.add_get("/api/computer-use/config", handlers.api_computer_use_config_get)
    app.router.add_put("/api/computer-use/config", handlers.api_computer_use_config_save)
    app.router.add_get("/api/approvals", handlers.api_approvals)
    app.router.add_post("/api/approvals/{id}/{action}", handlers.api_approval_resolve)

    # Local token bootstrap (file-based secret auth in handler, bypasses middleware)
    app.router.add_get("/api/token/local", handlers.api_token_local)

    # Tunnel status
    app.router.add_get("/api/tunnel/status", api_tunnel_status)

    # Session revocation (called by `kirocrew logout` CLI)
    app.router.add_post("/api/logout", handlers.api_logout)
    app.router.add_post("/api/shutdown", handlers.api_shutdown)

    # Webhook hooks (external triggers)
    app.router.add_post("/api/hooks/agent", handlers.api_hooks_agent)

    # App Platform
    from kiro_crew.apps.routes import register_app_routes

    register_app_routes(app)

    # Built-in app routes — register at startup (handlers check enabled state)
    for _builtin_name in BUILTIN_NAMES:
        try:
            _mod = importlib.import_module(f"kiro_crew.apps.builtins.{_builtin_name}")
            if hasattr(_mod, "register_routes"):
                _mod.register_routes(app)
        except ModuleNotFoundError as exc:
            if exc.name != f"kiro_crew.apps.builtins.{_builtin_name}":
                raise

    # App token exchange (App Kit §5.1 — must be before auth middleware bypass)
    app.router.add_post("/api/apps/{name}/token", handlers.api_app_token)

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

    app.on_startup.append(_hooks_startup)

    async def _ensure_playwright(app_: web.Application) -> None:
        """Migrate any existing Playwright MCP config to the proxy (background task).

        The OSS build ships no bundled Playwright MCP installer, so there is no
        unconditional install step — we only migrate pre-existing mcp.json entries.
        """

        async def _bg_migrate() -> None:
            try:
                await asyncio.to_thread(_migrate_playwright_to_proxy)
            except Exception as exc:
                logger.debug("Playwright proxy migration skipped: %s", exc)

        task = asyncio.create_task(_bg_migrate())
        app_.setdefault("_bg_tasks", set()).add(task)
        task.add_done_callback(lambda t: app_.get("_bg_tasks", set()).discard(t))

    app.on_startup.append(_ensure_playwright)

    async def _hooks_shutdown(app_: web.Application) -> None:
        await on_gateway_shutdown()
        # Cancel the app dev-mode watcher started in _hooks_startup so an
        # in-process gateway restart does not leak the module-global task (which
        # holds a stale broadcast_ws targeting dead clients). Await cancellation.
        from kiro_crew.apps.dev_mode import stop_dev_mode_watcher

        await stop_dev_mode_watcher()

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

    # CSRF: block state-mutating requests from cross-origin pages
    _safe_methods = {"GET", "HEAD", "OPTIONS"}

    # SEL: log mutating API operations
    _sel_log_methods = {"POST", "PUT", "DELETE", "PATCH"}

    @web.middleware  # type: ignore[misc]
    async def sel_audit_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        if request.method in _sel_log_methods and request.path.startswith("/api/"):
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
    _tailnet_host = await tailnet.resolve_tailnet_host(
        KiroCrewConfig.load().dashboard.tailscale.enabled
    )
    if _tailnet_host:
        logger.info(
            "tailnet access enabled: trusting origin https://%s (bind and auth unchanged)",
            _tailnet_host,
        )
    # Stashed on the app, not left a local, because GET /api/tailnet/status must
    # report the value the running origin set was actually built from rather than
    # re-probe the daemon (see handlers/tailnet.py). ``tailnet_resolved_at`` is
    # stamped unconditionally — it timestamps the resolution ATTEMPT, so an
    # "unresolved" card can say when we last looked; ``0`` means the derivation
    # never ran (feature off, or pinned). Both start-up paths set both keys: only
    # one of them serves this route today, but an earlier round of this feature
    # already shipped a bug from touching one startup site and not the other.
    app["tailnet_host"] = _tailnet_host
    app["tailnet_resolved_at"] = int(time.time()) if _tailnet_host else 0
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

    @web.middleware  # type: ignore[misc]
    async def csrf_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        if request.method not in _safe_methods:
            if not check_origin(request, require=True, fallback_header="Referer"):
                raise web.HTTPForbidden(
                    text="CSRF check failed: request origin not allowed.",
                    content_type="text/plain",
                )
        return await handler(request)  # type: ignore[operator]

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
    # I/O (or Windows icacls subprocess) lands on the loop on the first auth op.
    await warm_auth_singletons()

    # Explicit middleware ordering — self-documenting and immune to future insertions
    app.middlewares[:] = [
        # Outermost: privacy-safe per-route latency (rec #1). Times the FULL
        # in-gateway handling (all middleware + handler). Labels are limited to
        # method / bounded route_template / status_class — never a real path,
        # query, id, or body — so it cannot leak content or explode cardinality.
        make_route_latency_middleware(),
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

    # ── Instances (multi-instance management) ────────────────────────────────
    # Register the opt-in instances startup/cleanup hooks HERE, before
    # ``runner.setup()`` freezes the app's signal lists. See
    # ``_register_instances_hooks`` for why ordering matters.
    _register_instances_hooks(app, state, port)

    # Hardened runner: bounds the request-line/header read time (slowloris /
    # CWE-400) and reaps idle keep-alive connections. See dashboard.slowloris.
    # max_field_size is raised from aiohttp's 8190 default so the accumulated
    # shared per-port cookie jar can't 400 at the parser before a handler
    # prunes it (see refresh_tokens.foreign_port_cookies).
    runner = build_hardened_runner(app, max_field_size=_MAX_HEADER_FIELD_SIZE)
    await runner.setup()
    site = web.TCPSite(runner, bind_address_for(local_only), port)
    await _start_site(site, port)

    # Port bind succeeded — now safe to write the secret file. Offloaded:
    # _write_secret_file does blocking fs I/O (os.open/os.close and, on Windows,
    # an icacls subprocess via restrict_to_owner), so it must not run on the
    # event loop (no-blocking-call-on-event-loop).
    try:
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _write_secret_file, _secret_path, _internal_secret
        )
    except OSError:
        await runner.cleanup()
        raise

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
    await asyncio.to_thread(rotate_dumps)
    _dump_file = await asyncio.to_thread(open_dump_file)
    # exit_after is configurable because the right budget is host-dependent: a
    # gateway doing heavy subprocess work (long builds, test suites, bursts of
    # child reaping) can wedge the loop briefly without being genuinely dead,
    # and a hard-coded 25s turned those into hard exits that lost in-flight
    # work. The default is unchanged; the loader clamps the range.
    try:
        _exit_after = float(KiroCrewConfig.load().dashboard.loop_stall_exit_after_secs)
    except Exception:
        logger.debug("loop-stall exit budget config unavailable; using default", exc_info=True)
        _exit_after = 25.0
    _loop_watchdog = LoopStallWatchdog(dump_file=_dump_file, exit_after=_exit_after)

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
    # dashboard.prevent_sleep). Shared with the headless --slack-only entrypoint.
    _arm_prevent_sleep_poll(state)

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

    # Fire background MCP probe at startup (non-blocking)
    asyncio.create_task(handlers._bg_mcp_probe())

    # Start terminal orphan reaper (kills PTYs with no WS past the reaper window)
    _reaper = asyncio.create_task(handlers.reap_orphaned_terminals(app))
    _reaper.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
    state._terminal_reaper = _reaper  # prevent GC

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
        if state.sessions is not None:
            for slot in state._slots.values():
                if not slot._trust and not slot._trust_reads:
                    state.sessions.set_approval_policy(f"dashboard:{slot.key}", "")
        # Slack cleanup — isolated so failures don't block dashboard operations
        try:
            from kiro_crew.slack.handler import (
                _trusted_sessions,  # circular import: server.py is imported by slack/gateway.py which imports handler.py
            )

            _trusted_sessions.clear()
        except Exception:
            logger.debug("Could not clear trusted sessions", exc_info=True)
        # Slack notification (prevent GC with background_tasks set)
        _dispatch_override_expiry_notification(state, _notify_slack_override_expired)

    safety_override().on_expired = _on_override_expired

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
        merged = await asyncio.to_thread(
            migrate_channel_transcripts, dashboard_slots=_claimed
        )
        if merged:
            logger.info("Merged %d leftover channel transcript copies", merged)
    except Exception:
        # A failed migration leaves the orphan in place rather than losing
        # messages, so starting up without it is safe.
        logger.warning("channel transcript migration failed", exc_info=True)

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
    )
    state._hook_store = ScriptHookStore()
    set_global_hook_store(state._hook_store)

    # Wire script hooks into subagent tool execution path
    if state.subagents is not None:
        state.subagents.hook_store = state._hook_store

    # Visible notice + pct reset when auto-compaction fires on a dashboard session
    state.wire_session_compact_callback()
    # Visible notice when the watchdog recycles a dashboard session (e.g. RSS)
    state.wire_session_recycle_callback()

    app = web.Application(
        client_max_size=60 * 1024 * 1024
    )  # 60 MB: covers 50 MB upload + multipart overhead
    app["state"] = state
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
    state.load_tags()
    app["port"] = port

    _precompute_telemetry(state)

    # ── Auth parity with start_dashboard ─────────────────────────────────────
    # The MCP route surface is identical to the dashboard's, so the middleware
    # chain must be too. Host-allowlist source of truth is shared with the CSRF
    # Origin check via build_allowed_origins/build_allowed_hosts (see origin.py).
    _tailnet_host = await tailnet.resolve_tailnet_host(
        KiroCrewConfig.load().dashboard.tailscale.enabled
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
    app["local_only"] = local_only

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
    _safe_methods = {"GET", "HEAD", "OPTIONS"}

    @web.middleware  # type: ignore[misc]
    async def sel_audit_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        if request.method in _sel_methods and request.path.startswith("/api/"):
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

    @web.middleware  # type: ignore[misc]
    async def csrf_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        # Cross-site CSRF barrier on state-changing routes, parity with
        # start_dashboard. Loopback local processes (mcp-core, cron) send no
        # Origin header and are trusted by check_origin; a browser always sends
        # Origin, so a cross-site page is rejected here even before token auth.
        if request.method not in _safe_methods:
            if not check_origin(request, require=True, fallback_header="Referer"):
                # Audit the CSRF denial (security-relevant permission decision),
                # mirroring host_validation_middleware.
                sel().log_api_access(
                    caller="mcp_tool",
                    operation=f"{request.method} {request.path}",
                    outcome="denied",
                    resources=request.path,
                    error=f"CSRF check failed: origin not allowed: {request.headers.get('Origin', '')[:100]}",
                )
                raise web.HTTPForbidden(
                    text="CSRF check failed: request origin not allowed.",
                    content_type="text/plain",
                )
        return await handler(request)  # type: ignore[operator]

    # Warm the auth singletons off the event loop before building the chain
    # (parity with start_dashboard) so no blocking key-file I/O hits the loop.
    await warm_auth_singletons()

    # Explicit ordering mirrors start_dashboard: latency → host → csrf → token → audit.
    app.middlewares[:] = [
        # Outermost: privacy-safe, bounded-cardinality per-route latency (rec #1).
        # The MCP routes are registered AFTER this assignment, so the middleware
        # captures its route-template set LAZILY on the first request (by which
        # point every route is registered) — see make_route_latency_middleware.
        make_route_latency_middleware(),
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

    # Prevent-sleep shutdown hook — registered before runner.setup freezes the
    # signal lists; the poll itself is armed after the port binds (below). This
    # is what makes headless --slack-only keep the host awake during a long
    # Slack task, identically to the full dashboard.
    _register_prevent_sleep_shutdown(app, state)

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

    # Port bind succeeded — now safe to persist the secret file (parity with
    # start_dashboard: write deferred so a failed bind can't poison it).
    # Offloaded: _write_secret_file does blocking fs I/O (os.open/os.close and,
    # on Windows, an icacls subprocess via restrict_to_owner), so it must not run
    # on the event loop (no-blocking-call-on-event-loop).
    try:
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _write_secret_file, _secret_path, _internal_secret
        )
    except OSError:
        await runner.cleanup()
        raise

    logger.info("API-only server listening on %s:%d", bind_addr, port)

    # Arm the prevent-sleep poll now the loop is up and the port is bound
    # (shutdown hook already registered above). Headless --slack-only mode keeps
    # the host awake during a long Slack task exactly as the full dashboard does.
    _arm_prevent_sleep_poll(state)

    # Boot-to-ready (rec #1): headless API server is bound and ready. Privacy-safe
    # fixed labels only; best-effort.
    # Publish readiness at the exact boundary measured as boot-to-ready.
    state.ready = True
    record_boot_to_ready((time.time() - state.start_time) * 1000.0, server="api")

    return runner, state
