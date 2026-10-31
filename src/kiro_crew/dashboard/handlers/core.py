"""Core handlers — page serving, branding, STT, config, SEL, auth, session workspace."""

from __future__ import annotations

import asyncio
import copy
import difflib
import functools
import importlib as _importlib
import json
import logging
import math
import os
import platform
import re
import shlex
import shutil
import sys as _sys
import unicodedata
from collections.abc import Coroutine, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

import kiro_crew
import kiro_crew._facade_mirror as _facade_mirror
import kiro_crew.config.resolution as _resolution
from kiro_crew import aws_consent, beacon, platform_compat, stt
from kiro_crew.acp_backends import selectable_backend_values, selectable_stored_backend_values
from kiro_crew.apps.manifest import KEBAB_RE
from kiro_crew.computer_use.types import MAX_SCREENSHOT_MAX_PX as _CU_MAX_SCREENSHOT_MAX_PX
from kiro_crew.computer_use.types import MAX_TREE_NODES_LIMIT as _CU_MAX_TREE_NODES_LIMIT
from kiro_crew.computer_use.types import MIN_SCREENSHOT_MAX_PX as _CU_MIN_SCREENSHOT_MAX_PX
from kiro_crew.config.loader import (
    _VALID_STT_PROVIDERS,
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    CHAT_TURN_TIMEOUT_MAX,
    CHAT_TURN_TIMEOUT_MIN,
    COMPLETION_KEEP_CHARS_MIN,
    DEDUP_EVERY_N_SWEEPS_MAX,
    EMBED_RATE_LIMIT_MAX,
    EXTRACTION_POOL_SIZE_MAX,
    EXTRACTION_POOL_SIZE_MIN,
    FOLDER_INGEST_CHUNK_BUDGET_MAX,
    IMPORT_CHUNK_BUDGET_MAX,
    MAX_SUBAGENTS_FIXED_FLOOR,
    MCP_PROBE_TIMEOUT_MAX,
    MCP_PROBE_TIMEOUT_MIN,
    POOL_TTL_SECS_MAX,
    POOL_TTL_SECS_MIN,
    RECENT_TINT_COUNT_MAX,
    RECENT_TINT_COUNT_MIN,
    SESSION_TIMEOUT_MAX,
    SESSION_TIMEOUT_MIN,
    SOFT_STOP_BUDGET_MAX,
    SOFT_STOP_BUDGET_MIN,
    SUBAGENT_AUTO_MAX_CEILING,
    SUBAGENT_MAX_TURNS_CEILING,
    SWEEP_CHUNK_BUDGET_MAX,
    ConfigReadError,
    ConfigWriteRefused,
    KiroCrewConfig,
    _clamp_compact_wait_secs,
    coerce_dict_section,
    config_path,
    update_config_locked,
)
from kiro_crew.config.sections import (
    COMPACT_WAIT_SECS_MAX,
    COMPACT_WAIT_SECS_MIN,
    DECISION_BUCKET_MAX,
    DECISION_BUCKET_MIN,
    DECISION_MODEL_ROUTE_TIERS,
    EXTERNAL_TERMINAL_APPS,
    FOLDER_SORT_MODES,
    JUDGE_PROVIDERS,
    SESSION_SORT_MODES,
    STT_LANGUAGE_AUTO,
    TITLE_REFRESH_EVERY_TURNS_MAX,
    TRANSCRIBE_LANGUAGES_MAX,
    TRANSCRIBE_LANGUAGES_MIN,
    TRANSLUCENT_PANELS_VALUES,
    SttConfig,
    transcribe_language_options,
    transcribe_vocabulary_name,
)
from kiro_crew.config.superseded_defaults import ack_explicit_write
from kiro_crew.context_management import RESULT_FILE_MAX_BYTES
from kiro_crew.dashboard.chat_utils import drained_to_thread, run_config_write
from kiro_crew.dashboard.handlers._shared import (
    _owner_denial_response,
    _pip_install_channel_available,
    guard_owner_surface_routes,
    owner_surface_guard,
    pip_extra_install_command,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.handlers.core_local_token import _unix_peer_is_self
from kiro_crew.dashboard.handlers.core_security import _sel
from kiro_crew.dashboard.origin import check_host, is_direct_local_request
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.stt_stream import _STREAMING_PROVIDERS, PROVIDER_LOCAL
from kiro_crew.dashboard.token_auth import (
    MAX_SESSION_TTL_SECS,
    _ct_eq,
    _unix_request_socket,
    generate_token,
    parse_duration,
)
from kiro_crew.effort import EFFORT_LEVELS
from kiro_crew.executors import discovery_executor
from kiro_crew.external_text import redact_external_text as _redact_external
from kiro_crew.gateway_identity import gateway_id
from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self
from kiro_crew.metrics import provider as _metrics_provider
from kiro_crew.security_posture import build_posture_snapshot_async, posture_counts_async
from kiro_crew.session_workspace import is_valid_id
from kiro_crew.stt import capabilities as stt_capabilities
from kiro_crew.stt import decoder as stt_decoder
from kiro_crew.stt import models as stt_models
from kiro_crew.stt import telemetry as stt_telemetry
from kiro_crew.stt.limits import (
    MAX_IDLE_EVICT_SECS,
    MAX_INTERVAL_MS,
    MIN_IDLE_EVICT_SECS,
    MIN_PARTIAL_INTERVAL_MS,
    MIN_SILENCE_MS,
)
from kiro_crew.transcribe import (
    VOCABULARIES_ACCESS_DENIED,
    VOCABULARIES_LIST_FAILED,
    VOCABULARIES_LIST_PERMISSION,
    VocabularyListError,
    _find_ffmpeg,
    _whisper_language,
    audio_exceeds_secs,
    availability_detail,
    batch_duration_cap_secs,
    ensure_ffmpeg_in_path,
    ffmpeg_source,
    list_custom_vocabularies,
)
from kiro_crew.validation import QUALIFIED_MODEL_PATTERN

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
_DIST_DIR = _STATIC_DIR / "dist"
_DIST_INDEX = _DIST_DIR / "index.html"
# mtime-keyed cache of the SPA shell HTML.  Each request stat()s _DIST_INDEX
# (cheap) and re-reads the file only when its mtime_ns differs from the cached
# key, so a Vite rebuild that rewrites index.html (new hashed asset refs) is
# picked up on the very next request WITHOUT a gateway restart — the cache
# never pins a pre-rebuild shell.  A missing-then-present bundle also self-heals
# because a FileNotFoundError is never cached.  The stat replaces a full
# read_text of the bundle on the hot path, which is the win.
# SECURITY CONTRACT: the cached value must stay ``None`` or equal the static,
# secret-free bundle — never inject per-request/dynamic data.  Pinned by
# test_served_shell_is_auth_independent.
_INDEX_HTML_CACHE: tuple[int, str] | None = None
_SSE_INTERVAL_SECS = 5


# Static, secret-free fallback served when the dashboard's static bundle cannot
# be read. Most commonly this is a stale install after an update: the
# long-running gateway process keeps executing the old install path (it does
# not hot-swap to the freshly-installed version), so it can no longer read
# index.html. It can also mean the web assets were never built (dev /
# first-run). MUST stay static and secret-free -- index() serves it
# UNAUTHENTICATED on the cold-start path (see the SECURITY CONTRACT on index());
# no server/user/session state may be injected.
#
# Marker phrase embedded in the fallback body. Exported so out-of-process
# probes (e.g. `kirocrew token`'s stale-dashboard warning) can detect that
# the gateway is serving the fallback without duplicating the wording.
DASHBOARD_HTML_NOT_FOUND_MARKER = "Dashboard HTML not found"
_DASHBOARD_HTML_NOT_FOUND = (
    f"<h1>{DASHBOARD_HTML_NOT_FOUND_MARKER}</h1>"
    "<p>The gateway is running but could not read the dashboard's"
    " static files.</p>"
    "<p>This most commonly happens after an update leaves a stale install:"
    " the long-running gateway keeps executing the old install path and"
    " cannot read the dashboard bundle (the process does not hot-swap to the"
    " newly-installed version). It can also mean the web assets were never"
    " built (dev / first-run) &mdash; build the frontend and stage it into"
    " the package before starting the gateway.</p>"
    "<p><strong>Try restarting Kiro Crew.</strong> The exact restart step"
    " depends on your environment: if you installed it as a service use"
    " <code>kirocrew restart</code> (systemd / launchd); otherwise"
    " stop the running <code>kirocrew gateway</code> process and start it"
    " again.</p>"
)


# ── Page ──


def _resolve_index_html() -> str:
    """Return the SPA shell HTML, using the mtime-keyed cache.

    Runs entirely in a worker thread (see ``index``): performs the blocking
    ``stat()`` and, only on first load or after a rebuild changed the mtime, the
    blocking ``read_text()``. A ``FileNotFoundError`` returns the static fallback
    and is never cached, so a transiently-absent dist self-heals on the next
    request (e.g. after a dev build). SECURITY CONTRACT: the cached value is
    solely the on-disk bundle — never per-request/dynamic data.
    """
    global _INDEX_HTML_CACHE
    try:
        mtime = _DIST_INDEX.stat().st_mtime_ns
        cached = _INDEX_HTML_CACHE
        if cached is not None and cached[0] == mtime:
            return cached[1]
        html = _DIST_INDEX.read_text(encoding="utf-8")
        _INDEX_HTML_CACHE = (mtime, html)
        return html
    except FileNotFoundError:
        return _DASHBOARD_HTML_NOT_FOUND


async def index(request: web.Request) -> web.Response:
    """Serve the React dashboard SPA shell (``static/dist/index.html``).

    When the built SPA bundle is absent/unreadable, serve the static
    ``_DASHBOARD_HTML_NOT_FOUND`` guidance page (restart/rebuild instructions).
    The React SPA is the only shell; there is no server-rendered HTML fallback,
    which would ship an incomplete ``esc()`` and a permissive inline-script
    surface.

    SECURITY CONTRACT — DO NOT inject server/user/session state into this
    response. The auth middleware serves this handler UNAUTHENTICATED on the
    cold-start path (no/expired token, GET/HEAD), including to remote clients
    in non-local mode, so the SPA can boot and self-refresh. That bypass is
    only safe while the body is a static, secret-free bundle. Inlining
    bootstrap JSON, feature flags, a username, or any per-request state here
    would leak it across the auth boundary. Keep dynamic data behind gated
    ``/api/*`` routes. Pinned by test_served_shell_is_auth_independent.
    """
    # Resolve the shell entirely off the event loop: the stat() + conditional
    # read_text() are the only blocking calls, and even a bare stat() can stall
    # the loop on slow/network-backed storage. Route through the dedicated
    # discovery_executor rather than the shared default thread pool: index() is
    # served UNAUTHENTICATED on the cold-start path, so a remote SPA GET flood on
    # slow storage must not be able to saturate the pool other gateway work
    # (DNS, etc.) depends on. The mtime cache still serves repeat requests
    # without a read.
    loop = asyncio.get_running_loop()
    html = await loop.run_in_executor(discovery_executor(), _resolve_index_html)
    return web.Response(text=html, content_type="text/html")


async def logo(request: web.Request) -> web.StreamResponse:
    """Serve the logo — prefer custom avatar from config, fall back to default."""
    import kiro_crew.dashboard.handlers as _h  # noqa: F811
    from kiro_crew.hooks import validate_file_path  # noqa: F811

    cfg = _h.KiroCrewConfig.load()
    if cfg.dashboard.avatar:
        if _h.is_sensitive_path(cfg.dashboard.avatar):
            return web.Response(status=404)
        validated = validate_file_path(cfg.dashboard.avatar)
        if validated and Path(validated).is_file():
            return web.FileResponse(validated)
    # The DEFAULT logo is channel-aware: nightly builds serve the night-sky
    # variant so the whole in-app surface -- sidebar logo, browser favicon,
    # and native-notification avatar all resolve through /logo.png -- matches
    # the nightly app's Dock/tray identity. Stamp check mirrors the desktop
    # shell's channelForVersion ("-nightly." marks nightly); a user-configured
    # avatar above always wins over channel branding.
    from kiro_crew import __version__

    names = ["kirocrew-logo.png"]
    if "-nightly." in __version__:
        names.insert(0, "kirocrew-logo-nightly.png")
    for name in names:
        path = _h._STATIC_DIR / name
        if path.is_file():
            return web.FileResponse(path)
    return web.Response(status=404)


async def api_branding(request: web.Request) -> web.Response:
    """GET /api/dashboard/branding — bot name and avatar config."""
    cfg = KiroCrewConfig.load()
    # `direct_local` is the canonical client-side origin signal: true only for a
    # direct-local (loopback, no proxy) caller. The dashboard reads it to gate
    # OS-desktop file actions (open/reveal); any future consumer of origin
    # posture should reuse this flag rather than invent a second home.
    return web.json_response(
        {
            "bot_name": cfg.dashboard.bot_name or "Kiro Crew",
            "avatar": "/logo.png",
            "direct_local": is_direct_local_request(request),
        }
    )


async def _liveness_payload(request: web.Request) -> dict[str, object]:
    """Return public liveness plus identity only for direct-local callers.

    Identity requires BOTH gates: a direct-local peer (loopback, no
    forwarding headers) AND a Host header naming a host we serve. The probe
    paths are exempt from the host_validation middleware (orchestrators
    address pods by IP — see origin.PROBE_PATHS), so a DNS-rebound loopback
    request CAN reach this handler with a forged Host; ``check_host`` here
    keeps the exact-version fingerprint off that path. A rebound page then
    learns only ``{"ok": true}`` — indistinguishable from the TCP connect
    succeeding, which it could already observe.

    ``gateway_id`` rides the same gate for the same reason: it is an identity
    fingerprint, so it belongs behind the direct-local check rather than on the
    public probe boundary. A hub reads it through the loopback end of a tunnel it
    just opened, which IS a direct-local request, so the gate does not fence off
    the caller that needs it. It is minted on first read
    (:func:`kiro_crew.gateway_identity.gateway_id`) and reveals nothing about the
    machine: a random id, not a derived one. It is read in a worker thread: the
    first read on a fresh data home mints the file, and ``/api/health`` is the
    most-polled route there is, so a stalled filesystem would otherwise stall the
    loop that answers every other request. Later reads are served from the
    module's cache and the hop costs only a thread round-trip.
    """
    payload: dict[str, object] = {"ok": True}
    if is_direct_local_request(request) and check_host(request):
        # The desktop production/nightly cross-app guard calls over loopback and
        # needs exact identity to decide whether it can reuse the shared port.
        # Anonymous non-loopback probes get only the liveness bit, avoiding an
        # exact-version fingerprint on the public probe boundary.
        payload.update(
            {
                "app": "kirocrew",
                "version": kiro_crew.__version__,
                "gateway_id": await asyncio.to_thread(gateway_id),
            }
        )
        # The off-loop watchdog's sustained-lag reading, for a local probe
        # telling a slow gateway from a dead one.
        from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog

        state = request.app.get("state")
        watchdog = getattr(state, "_loop_watchdog", None)
        if isinstance(watchdog, LoopStallWatchdog):
            payload["loop"] = {
                "lagging": bool(watchdog.lagging),
                "sustained_lag_episodes": int(watchdog.sustained_lag_episodes),
            }
    return payload


async def api_health(request: web.Request) -> web.Response:
    """GET /api/health — liveness, with identity for direct-local callers."""
    return web.json_response(await _liveness_payload(request))


async def api_version(request: web.Request) -> web.Response:
    """GET /api/version — this gateway's exact version, for an authenticated peer.

    Exists because remote execution is fenced by version EQUALITY: a local
    session may only dispatch its turns to a connected crew running the identical
    gateway build, since the two ends exchange a wire vocabulary that is not
    versioned independently. ``/api/health`` cannot answer that question — it
    reveals ``version`` only to a direct-local caller with a served ``Host``, on
    purpose, to keep an exact-version fingerprint off the public probe boundary.

    So this route is NOT public. It is deliberately absent from
    ``token_auth._BYPASS_EXACT`` and from ``origin.PROBE_PATHS``, which means it
    requires the dashboard credential and a served ``Host`` like any other API
    route. A peer reads it over its tunnel with the port-scoped cookie the
    instance manager already mints, exactly as the session-search and
    session-import carriers do — so nothing is exposed to an anonymous caller
    that was not exposed before, and the fingerprint decision at
    :func:`_liveness_payload` stands unchanged.
    """
    return web.json_response({"version": kiro_crew.__version__})


async def api_live(request: web.Request) -> web.Response:
    """GET /api/live — Kubernetes-style liveness alias for /api/health."""
    return web.json_response(await _liveness_payload(request))


async def api_ready(request: web.Request) -> web.Response:
    """GET /api/ready — Kubernetes-style readiness probe.

    Distinct from liveness: the process may be UP (``/api/live`` 200) yet not
    able to serve application traffic. Readiness reflects the observable
    lifecycle state:

    * **Startup** — before the socket binds, connection failure is the external
      not-ready signal. After bind, ``DashboardState.ready`` remains false and
      the probe returns 503 while session restoration, tunnel setup, and other
      pre-ready wiring finishes.
    * **Serving** — the server publishes ``DashboardState.ready = True`` at the
      same final boundary used by the boot-to-ready metric; readiness is then
      200 while required control state is wired and shutdown has not been
      requested. The separately tracked memory preparation task starts at this
      boundary: memory content routes remain fail-closed and agent turns wait
      at admission until it settles.
    * **Shutdown requested** — when SIGTERM/SIGINT or ``POST /api/shutdown``
      sets the process-wide ``shutdown_event``, readiness changes to 503 while
      ``/api/live`` remains 200 until the HTTP server exits. Supervisors that
      poll during this interval can stop routing new work; this endpoint does
      not itself impose or promise a minimum load-balancer drain delay.

    Shutdown takes precedence over subsystem checks. The response carries only
    fixed, low-cardinality booleans/markers — no paths, ids, counts, secrets, or
    user/session content. The probe paths are exempt from the host_validation
    middleware (orchestrators address pods by IP — see origin.PROBE_PATHS), so
    a disallowed-Host request CAN reach this handler; the detail fields
    (startup/shutdown/subsystem markers) are therefore gated on ``check_host``,
    mirroring ``_liveness_payload``. A disallowed-Host caller gets only
    ``{"ready": bool}`` — exactly the bit the status code already tells it.
    """
    # Graceful-shutdown gate: as soon as a stop is requested, stop advertising
    # readiness so traffic drains before the socket closes.
    shutting_down = kiro_crew.shutdown_event.is_set()

    state = request.app.get("state")
    # Boot-wired subsystems this gateway needs before it can serve dashboard
    # traffic. Keys are stable + low-cardinality so the payload leaks nothing.
    checks = {
        "state": state is not None,
        "sessions": getattr(state, "sessions", None) is not None,
    }
    # NOTE: readiness deliberately does NOT wait on the Kiro CLI check. Kiro
    # readiness is not a prerequisite for serving the dashboard — a signed-out
    # user is meant to get in and see the reauthentication banner — and gating
    # this endpoint on it would only delay first paint. (It would also not do
    # what it looks like: the desktop splash polls /api/status and accepts any
    # status < 500, so a 503 here is invisible to it.)
    # Require the literal bool set at the final startup boundary. This stays
    # fail-closed for partial/mocked state objects and cannot become truthy just
    # because the socket is already accepting probe requests.
    startup_complete = getattr(state, "ready", False) is True
    ready = all(checks.values()) and startup_complete and not shutting_down
    payload: dict = {"ready": ready}
    if check_host(request):
        # Diagnostic detail for operators/orchestrators addressing the
        # gateway by an allowed hostname. Withheld from disallowed-Host
        # callers (e.g. a DNS-rebound page reaching the probe exemption).
        payload["startup_complete"] = startup_complete
        payload["checks"] = checks
        if shutting_down:
            payload["shutting_down"] = True
    return web.json_response(payload, status=200 if ready else 503)


#: Accepted shape for ``dashboard.language`` — a conservative BCP-47 subset
#: (``en``, ``zh-CN``, ``pt-BR``, ``zh-Hans-CN``). Deliberately validates SHAPE,
#: not membership in the frontend's shipped-language list: ``""`` and
#: not-yet-shipped tags must stay writable (the SPA's ``resolveLanguage()``
#: falls back to detection for any code it has no catalog for, so a persisted
#: non-catalog value degrades gracefully client-side). Membership IS enforced,
#: but at the point of use: ``context.ui_language_tag`` gates the agent-steer
#: read path on ``_UI_LANGUAGE_CATALOGS`` so a non-catalog tag is never claimed
#: to the model as the UI language. A new backend consumer of
#: ``dashboard.language`` must route through that resolver rather than reading
#: the raw field.
_LANGUAGE_TAG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}$")


def _theme_payload(cfg: KiroCrewConfig) -> dict[str, object]:
    """Workspace display preferences shared by the boot + config endpoints.

    One builder for all four response sites so a newly-added preference cannot
    be surfaced by some of them and silently omitted by the rest.
    """
    return {
        "mode": cfg.dashboard.theme_mode or "",
        "color": cfg.dashboard.theme_color or "",
        "language": cfg.dashboard.language or "",
        "translucent_panels": cfg.dashboard.translucent_panels or "",
        "onboarded": cfg.dashboard.onboarded,
        "import_onboarded": cfg.dashboard.import_onboarded,
        "privacy_acked": cfg.dashboard.privacy_acked,
        "crewmates_onboarded": cfg.dashboard.crewmates_onboarded,
    }


async def api_theme_boot(request: web.Request) -> web.Response:
    """GET /api/theme/boot — workspace display config for frontend boot.

    Unauthenticated (same boundary as /api/health) so the SPA can read the
    workspace theme and UI language before the token flow completes. Contains
    no secrets — only workspace-level display preferences and onboarding flags.
    """
    cfg = KiroCrewConfig.load()
    return web.json_response(_theme_payload(cfg))


async def api_theme_config(request: web.Request) -> web.Response:
    """GET/PUT /api/config/theme — read or update workspace display settings.

    GET returns the current config. PUT accepts
    {mode?, color?, language?, translucent_panels?, onboarded?, import_onboarded?,
    privacy_acked?, crewmates_onboarded?} and persists ONLY those ``dashboard.*``
    keys.

    The PUT is a locked delta read-modify-write, never a whole-document
    ``KiroCrewConfig.save()``. A momentarily unreadable ``config.json`` (a torn
    read, a sharing violation, an empty file) makes ``load()`` answer
    pure defaults, and the SPA fires this PUT on its own at boot (the legacy
    theme migration, triggered by the ``onboarded=false`` such a load reports),
    so publishing the loaded snapshot erased every setting in the file. Even a
    healthy snapshot carried load-time coercions back to disk and lost a
    concurrent CLI write. Writing only the requested keys under both config
    locks leaves the rest of the file as it was, and an unreadable file is
    refused (500 ``config_unreadable``) with its bytes untouched.
    """
    if request.method == "GET":
        cfg = KiroCrewConfig.load()
        return web.json_response(_theme_payload(cfg))

    # PUT
    denied = await require_owner_dashboard_request(request, "config.theme.write")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except (LookupError, RecursionError, ValueError):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be an object")

    # Validate the WHOLE body before anything is written, so a 400 is a no-op.
    updates: dict[str, object] = {}
    if "mode" in body:
        mode = body["mode"]
        if mode not in ("", "dark", "light", "system"):
            raise web.HTTPBadRequest(text="mode must be '', 'dark', 'light', or 'system'")
        updates["theme_mode"] = mode
    if "color" in body:
        color = body["color"]
        if not isinstance(color, str) or len(color) > 64:
            raise web.HTTPBadRequest(text="color must be a string (max 64 chars)")
        updates["theme_color"] = color
    if "language" in body:
        language = body["language"]
        # "" is the explicit "follow the browser" sentinel, so it must stay
        # writable — a user returning to Auto has to be able to clear the
        # stored choice.
        if not isinstance(language, str):
            raise web.HTTPBadRequest(text="language must be a string")
        if language and not _LANGUAGE_TAG_RE.match(language):
            raise web.HTTPBadRequest(
                text="language must be '' or a BCP-47 tag (e.g. 'en', 'zh-CN')"
            )
        updates["language"] = language
    # A real bool only: ``bool("false")`` is True, so a string here is a client bug
    # this endpoint must answer 400, not persist inverted.
    if "translucent_panels" in body:
        # Like ``mode``, a string with "" for "never chosen": a bool could not
        # tell a workspace that chose solid from one that predates the key, and
        # that is the case where the browser's own stored choice must keep
        # holding (see ``_translucent_panels_from`` in the loader).
        translucent = body["translucent_panels"]
        if translucent not in TRANSLUCENT_PANELS_VALUES:
            raise web.HTTPBadRequest(text="translucent_panels must be '', 'on', or 'off'")
        updates["translucent_panels"] = translucent
    for flag in ("onboarded", "import_onboarded", "privacy_acked", "crewmates_onboarded"):
        if flag in body:
            if not isinstance(body[flag], bool):
                raise web.HTTPBadRequest(text=f"{flag} must be a boolean")
            updates[flag] = body[flag]

    if updates:

        def _apply_theme(doc: dict) -> dict | None:
            dashboard = coerce_dict_section(doc, "dashboard")
            changed = False
            for key, value in updates.items():
                current = dashboard.get(key)
                # ``type`` too: ``1 == True``, and a hand-written 1 is not the
                # bool the loader reads back.
                if key in dashboard and current == value and type(current) is type(value):
                    continue
                dashboard[key] = value
                changed = True
            return doc if changed else None

        try:
            await run_config_write(update_config_locked, config_path(), mutate=_apply_theme)
        except ConfigReadError:
            logger.warning("theme config PUT: config.json is unreadable", exc_info=True)
            return web.json_response(
                {"error": "failed to read config file", "code": "config_unreadable"}, status=500
            )
        except ConfigWriteRefused as exc:
            return web.json_response(
                {"error": str(exc), "code": "config_write_refused"}, status=400
            )
        except OSError:
            logger.warning("theme config PUT: config.json write failed", exc_info=True)
            return web.json_response(
                {"error": "failed to write config file", "code": "config_write_failed"},
                status=500,
            )

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    return web.json_response(_theme_payload(cfg))


async def pwa_file(request: web.Request) -> web.StreamResponse:
    """Serve PWA root files (manifest, service worker, icons) from dist/."""
    name = request.match_info["name"]
    path = _DIST_DIR / name
    # Resolve both sides so a symlinked _DIST_DIR (dev-backend.sh points it
    # at KiroCrewWebsite/dist) still passes the traversal guard.
    if path.is_file() and _DIST_DIR.resolve() in path.resolve().parents:
        return web.FileResponse(path)
    raise web.HTTPNotFound()


# This module is the facade of the core dashboard routes: every name is reached through it
# as ``kiro_crew.dashboard.handlers.core.<name>``. A read is served by the owner that binds
# the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner
# holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module, and they
# log under its logger name. The page, asset, health and theme routes, the session
# sub-agent routes, logout, shutdown and the app token stay here; new work goes to the
# owner of its responsibility:
#
#   ``core_security`` -- the SEL accessor, the security event log and posture routes
#   ``core_stt_access`` -- STT error codes, background tasks, refusals and their audit
#   ``core_stt_settings`` -- the STT settings route and the choices it offers
#   ``core_stt_status`` -- STT readiness: status, vocabularies, download, fetch, prewarm
#   ``core_stt_speech`` -- the transcribe route and the transcript polish
#   ``core_config_view`` -- the masked config view and the whole-document read and write
#   ``core_config_models`` -- whether the account can use a pinned model
#   ``core_config_schema`` -- the editable config paths and their rules
#   ``core_config_patch`` -- the config PATCH route
#   ``core_local_token`` -- the local token bootstrap and its same-user transport check
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers.core_security",
    "kiro_crew.dashboard.handlers.core_stt_access",
    "kiro_crew.dashboard.handlers.core_stt_settings",
    "kiro_crew.dashboard.handlers.core_stt_status",
    "kiro_crew.dashboard.handlers.core_stt_speech",
    "kiro_crew.dashboard.handlers.core_config_view",
    "kiro_crew.dashboard.handlers.core_config_models",
    "kiro_crew.dashboard.handlers.core_config_schema",
    "kiro_crew.dashboard.handlers.core_config_patch",
    "kiro_crew.dashboard.handlers.core_local_token",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.core_config_models import (
        _ROLE_PIN_REVALIDATING,
        _ROLE_PIN_REVALIDATION_MAX_RESELECTS,
        _active_advertised_ids,
        _active_provider_and_pin_backend,
        _active_provider_name,
        _advertised_evidence_provider,
        _no_kiro_id_reason,
        _provider_backend,
        _revalidate_role_pin_evidence,
        _validate_role_model,
    )
    from kiro_crew.dashboard.handlers.core_config_patch import (
        _MOVED_CONFIG_FIELDS,
        _beacon_governance_pinned_off,
        _check_default_profile_is_saved,
        _ConfigKeyAlreadySet,
        _keep_omitted_session_environment_keys,
        _keep_vm_profiles,
        _profile_ids,
        _session_environment_key_validators,
        _tailnet_governance_pinned_off,
        _UnknownDefaultProfile,
        api_kirocrew_config_patch,
    )
    from kiro_crew.dashboard.handlers.core_config_schema import (
        _APP_MODEL_SPEC,
        _EDITABLE_CONFIG,
        _MODEL_PIN_PATTERN,
        _acp_backend_patch_values,
        _agent_values,
        _editable_config_spec,
        _preview,
        _selectable_acp_backends,
        _tier,
    )
    from kiro_crew.dashboard.handlers.core_config_view import (
        _AGENT_UNTRUSTED_TEXT_FIELDS,
        _SENSITIVE_MASK,
        _changed_paths_need_restart,
        _hot_apply_after_write,
        _mask_agent_free_text,
        _mask_strings,
        _masked_config_dict,
        api_kirocrew_config,
    )
    from kiro_crew.dashboard.handlers.core_local_token import (
        api_token_local,
    )
    from kiro_crew.dashboard.handlers.core_security import (
        api_security_posture,
        api_security_stats,
        api_sel_events,
        api_sel_verify,
    )
    from kiro_crew.dashboard.handlers.core_stt_access import (
        _CODE_DASHBOARD_USER_REQUIRED,
        _CODE_STT_AUDIO_TOO_LARGE,
        _CODE_STT_DECODER_BUNDLED,
        _CODE_STT_FAILED,
        _CODE_STT_MISSING_AUDIO,
        _CODE_STT_UNAVAILABLE,
        _audit_custom_model_disclosure,
        _audit_stt_custom_model_write_allowed,
        _audit_stt_refusal,
        _deny_app_token,
        _deny_non_owner_stt_custom_model,
        _refuse_app_token,
        _spawn_stt_background,
        _stt_background_tasks,
        _stt_custom_model_refusal,
        _stt_positive_int,
    )
    from kiro_crew.dashboard.handlers.core_stt_settings import (
        _LANGUAGE_IDENTIFYING_PROVIDERS,
        _STT_LANGUAGE_CODES,
        _STT_MODEL_SIZES,
        _STT_THREAD_LADDER,
        _builtin_stt_rows,
        _ffmpeg_install_commands,
        _language_identifying_providers,
        _stt_language_codes,
        _stt_prereq_commands,
        _stt_provider_rows,
        _stt_providers,
        _stt_thread_options,
        _stt_threads_env,
        _transcribe_extra_importable,
        api_stt_config,
    )
    from kiro_crew.dashboard.handlers.core_stt_speech import (
        _POLISH_MAX_CHARS,
        _POLISH_MAX_RATIO,
        _POLISH_MIN_RATIO,
        _POLISH_PROMPT,
        _POLISH_TIMEOUT_SECS,
        _POLISH_UNSPACED_SCRIPTS,
        _polish_is_content,
        _polish_is_ignorable,
        _polish_only_marks_differ,
        _polish_preserves_words,
        _polish_spaces_its_words,
        _polish_split,
        api_stt_polish,
        api_stt_transcribe,
    )
    from kiro_crew.dashboard.handlers.core_stt_status import (
        _STT_VOCABULARIES_TIMEOUT_SECS,
        AUTO_FETCH_AVAILABLE,
        AUTO_FETCH_BUNDLED,
        AUTO_FETCH_UNSUPPORTED,
        _ffmpeg_auto_fetch,
        api_stt_ffmpeg_download,
        api_stt_prepare,
        api_stt_prewarm,
        api_stt_status,
        api_stt_vocabularies,
    )


# ── Session workspace (Orchestrated Chat) ────────────────────────────


def _invalid_session_path_id(session_id: str, agent_id: str | None = None) -> web.Response | None:
    """400 for a path id ``session_workspace`` would refuse, else ``None``.

    Both ids reach a filesystem path (``sessions/{session_id}/agent-{agent_id}.md``)
    and are validated there by ``_validate_id``, which RAISES. The raise is the
    correct containment behaviour -- it is what stops ``..`` from escaping the
    session root -- but an un-caught one leaves the route answering 500 to what
    is really a malformed request, so it is translated here instead.

    The predicate is imported rather than restated: this must refuse exactly the
    set the path join refuses, no wider (a narrower guard would break the ``:``
    in a real key like ``dashboard:slot-3``) and no narrower (a wider one puts
    the 500 back). Shape follows ``cron_shared.py``'s ``_invalid_path_id_response`` --
    400 with an ``invalid_<name>`` ``code`` -- the contract
    docs/system-specs/common/code-style.md requires of a backend-owned error body.

    ``agent_id`` is checked second because that is the order the sinks validate
    in, so the reported code names the half the caller must actually fix.

    ``is_valid_id`` is imported at module scope per AUTOSDE ``top-level-imports``
    -- there is no cycle, ``session_workspace`` pulling only stdlib and
    ``config.paths``. The three ``list_results`` / ``read_result`` /
    ``result_path`` imports below stay function-local: they are pre-existing, and
    hoisting them would bind the names here at import time, which is exactly what
    the existing tests' ``monkeypatch`` of ``kiro_crew.session_workspace.<fn>``
    relies on NOT happening. That is a separate change with its own test fallout.
    """
    if not is_valid_id(session_id):
        return web.json_response(
            {"error": "invalid session id", "code": "invalid_session_id"}, status=400
        )
    if agent_id is not None and not is_valid_id(agent_id):
        return web.json_response(
            {"error": "invalid agent id", "code": "invalid_agent_id"}, status=400
        )
    return None


async def api_session_agents_list(request: web.Request) -> web.Response:
    """GET /api/sessions/{id}/agents — list sub-agent results for a session."""
    session_id = request.match_info["id"]
    if (_e := _invalid_session_path_id(session_id)) is not None:
        return _e
    from kiro_crew.session_workspace import list_results  # noqa: F811

    results = list_results(session_id)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="session.agents.list",
        outcome="ok",
        source="dashboard",
        resources=session_id,
    )
    return web.json_response({"results": results})


async def api_session_agent_result(request: web.Request) -> web.Response:
    """GET /api/sessions/{id}/agents/{agent_id} — read sub-agent result."""
    session_id = request.match_info["id"]
    agent_id = request.match_info["agent_id"]
    if (_e := _invalid_session_path_id(session_id, agent_id)) is not None:
        return _e
    from kiro_crew.session_workspace import read_result  # noqa: F811

    content = read_result(session_id, agent_id)
    if not content:
        return web.json_response({"error": "not found"}, status=404)
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # noqa: F811

    content, _ = redact_exfiltration_urls(content)
    content, _ = redact_credentials(content)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="session.agent.result",
        outcome="ok",
        source="dashboard",
        resources=f"{session_id}/{agent_id}",
    )
    return web.json_response({"agent_id": agent_id, "content": content})


async def api_session_agent_stream(request: web.Request) -> web.StreamResponse:
    """GET /api/sessions/{id}/agents/{agent_id}/stream — SSE stream of result file."""
    session_id = request.match_info["id"]
    agent_id = request.match_info["agent_id"]
    # Before the ok-record below as well as before prepare(): a refused request
    # is not a stream that happened to be empty, so it must not be audited as
    # one, and once the response is prepared the status is already on the wire.
    if (_e := _invalid_session_path_id(session_id, agent_id)) is not None:
        return _e
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="session.agent.stream",
        outcome="ok",
        source="dashboard",
        resources=f"{session_id}/{agent_id}",
    )
    from kiro_crew.session_workspace import result_path  # noqa: F811

    path = result_path(session_id, agent_id)
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    await resp.prepare(request)

    from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # noqa: F811
    from kiro_crew.session_workspace_tail import ResultTail  # noqa: F811

    # Each poll reads only the bytes appended since the last one, in a thread:
    # re-reading the whole file every second on the loop grows with its square.
    tail = ResultTail(path)

    async def _send(chunk: str) -> None:
        if chunk:
            chunk, _ = redact_exfiltration_urls(chunk)
            chunk, _ = redact_credentials(chunk)
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())

    polls = 1200  # 20 min max
    try:
        for poll in range(polls):
            chunk = await asyncio.to_thread(tail.read_new)
            # Check if the subagent is done.
            state: DashboardState = request.app["state"]
            info = state.subagents.get(agent_id) if state.subagents else None
            done = bool(info and info.done)
            if done or poll == polls - 1:
                # The decoder may still hold a final "\r"; it belongs to this
                # last chunk, as the whole-file read gave it.
                chunk += tail.finish()
            await _send(chunk)
            if done:
                await resp.write(b"event: done\ndata: {}\n\n")
                break
            await asyncio.sleep(1)
    except (ConnectionResetError, ClientConnectionResetError):
        pass
    return resp


async def api_logout(request: web.Request) -> web.Response:
    """POST /api/logout — revoke all active dashboard sessions.

    Called by ``kirocrew logout`` CLI. Admits a loopback origin OR a
    kernel-attested same-user AF_UNIX peer (``_unix_peer_is_self``, which the
    owner-only socket transport arrives as, with an empty ``request.remote``),
    plus the local secret — same auth as /api/token/local — to prevent
    unauthorized revocation.
    """
    import kiro_crew.dashboard.handlers as _h  # noqa: F811
    from kiro_crew.dashboard.token_auth import revoke_all_sessions  # noqa: F811

    # Reachable over loopback TCP or the dashboard's AF_UNIX socket. A unix peer
    # has an EMPTY request.remote, so the loopback test alone would 403 the
    # owner-only socket transport the CLI prefers; admit it on a positive kernel
    # same-principal check (_unix_peer_is_self), exactly as /api/token/local does.
    # The X-Local-Secret check below is unchanged on both transports.
    if not _h.is_loopback(request.remote or "") and not _unix_peer_is_self(request):
        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation="logout",
            outcome="denied",
            source="cli",
            resources="non-loopback",
        )
        return web.json_response({"error": "loopback only"}, status=403)

    expected = request.app.get("local_secret", "")
    provided = request.headers.get("X-Local-Secret", "")
    if not expected or not provided or not _ct_eq(expected, provided):
        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation="logout",
            outcome="denied",
            source="cli",
            resources="invalid-secret",
        )
        return web.json_response({"error": "invalid secret"}, status=403)

    # Fail-closed: bump_revocation_gen raises when the persisted counter
    # cannot be read (bumping from an assumed base could persist a LOWER
    # counter, resurrecting revoked sessions after restart) or when the write
    # fails (the counter is left unchanged, so the revocation did not take
    # effect). Report the failure instead of a false success.
    try:
        revoke_all_sessions()
    except OSError:
        logger.warning("logout failed: could not persist session revocation", exc_info=True)
        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation="logout",
            outcome="error",
            source="cli",
            resources="revocation-persist-failed",
        )
        return web.json_response(
            {
                "error": "could not persist session revocation; logout not completed",
                "code": "revocation_persist_failed",
            },
            status=500,
        )
    _sel().log_api_access(
        caller=request.remote or "unknown",
        operation="logout",
        outcome="success",
        source="cli",
        resources="all-sessions-revoked",
    )
    return web.json_response({"ok": True})


async def api_shutdown(request: web.Request) -> web.Response:
    """POST /api/shutdown — gracefully stop the gateway process.

    Sets the process-wide ``shutdown_event``, which is the same trigger the
    SIGTERM/SIGINT handler uses: it unblocks the gateway run loop, runs the
    graceful ``_shutdown()`` sequence (flushes session/memory/cron state,
    cleans up the dashboard runner), kills orphaned kiro-cli subprocesses, and
    exits the process.

    Intended for the desktop app to call before installing an auto-update, so
    the Squirrel bundle swap never races a live gateway. Requires loopback (or a
    kernel-attested same-user AF_UNIX peer, ``_unix_peer_is_self``, which is how
    ``kirocrew stop``'s owner-verified socket send arrives, with an empty
    ``request.remote``) plus the local secret (same auth as
    ``/api/token/local`` and ``/api/logout``) so a web page cannot trigger a
    shutdown.
    """
    import kiro_crew.dashboard.handlers as _h  # noqa: F811
    from kiro_crew import shutdown_event  # noqa: F811

    if not _h.is_loopback(request.remote or "") and not _unix_peer_is_self(request):
        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation="shutdown",
            outcome="denied",
            source="local-app",
            resources="non-loopback",
        )
        return web.json_response({"error": "loopback only"}, status=403)

    expected = request.app.get("local_secret", "")
    provided = request.headers.get("X-Local-Secret", "")
    if not expected or not provided or not _ct_eq(expected, provided):
        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation="shutdown",
            outcome="denied",
            source="local-app",
            resources="invalid-secret",
        )
        return web.json_response({"error": "invalid secret"}, status=403)

    _sel().log_api_access(
        caller=request.remote or "unknown",
        operation="shutdown",
        outcome="success",
        source="local-app",
        resources="gateway",
    )
    logger.info("shutdown requested via /api/shutdown — triggering graceful stop")

    # Fire the shutdown only AFTER this 200 has flushed to the client, so the
    # desktop app receives a definitive ack before the gateway tears down.
    asyncio.get_running_loop().call_later(0.25, shutdown_event.set)
    return web.json_response({"ok": True, "shutting_down": True})


async def api_app_token(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/token — exchange app secret for app-scoped token.

    Apps authenticate by presenting their per-app secret (stored on disk
    at install time) via the ``X-App-Secret`` header.  On success, returns
    an HMAC token with ``app=<name>`` in the payload so downstream
    middleware can extract the verified app identity.
    """
    from kiro_crew.dashboard.token_auth import generate_token, validate_app_secret  # noqa: F811
    from kiro_crew.sel import sel

    app_name = request.match_info["name"]
    provided_secret = request.headers.get("X-App-Secret", "")
    if not provided_secret:
        sel().log_api_access(
            caller=app_name,
            operation="app_token_exchange",
            outcome="denied",
            source="app_auth",
            error="missing X-App-Secret header",
        )
        return web.json_response({"error": "missing X-App-Secret header"}, status=403)

    if not validate_app_secret(app_name, provided_secret):
        sel().log_api_access(
            caller=app_name,
            operation="app_token_exchange",
            outcome="denied",
            source="app_auth",
            error="invalid secret",
        )
        return web.json_response({"error": "invalid secret"}, status=403)

    token = generate_token(app_name, app=app_name)
    sel().log_api_access(
        caller=app_name,
        operation="app_token_exchange",
        outcome="granted",
        source="app_auth",
    )
    return web.json_response({"token": token})


# The session sub-agent routes are owner surfaces under their own audit labels;
# any other ``api_session_agent*`` handler is refused to private members under
# its name.
guard_owner_surface_routes(
    globals(),
    prefix="api_session_agent",
    member_scoped=frozenset(),
    resource_scoped={
        "api_session_agents_list": owner_surface_guard("session.agents.list"),
        "api_session_agent_result": owner_surface_guard("session.agent.result"),
        "api_session_agent_stream": owner_surface_guard("session.agent.stream"),
    },
)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
