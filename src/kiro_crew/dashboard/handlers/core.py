"""Core handlers — page serving, branding, STT, config, SEL, auth, session workspace."""

from __future__ import annotations

import asyncio
import copy
import hmac
import json
import logging
import math
import os
import platform
import re
import shlex
import shutil
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

import kiro_crew
from kiro_crew import beacon, platform_compat, stt
from kiro_crew.acp_backends import selectable_backend_values
from kiro_crew.computer_use.types import MAX_SCREENSHOT_MAX_PX as _CU_MAX_SCREENSHOT_MAX_PX
from kiro_crew.computer_use.types import MAX_TREE_NODES_LIMIT as _CU_MAX_TREE_NODES_LIMIT
from kiro_crew.computer_use.types import MIN_SCREENSHOT_MAX_PX as _CU_MIN_SCREENSHOT_MAX_PX
from kiro_crew.config.loader import (
    _VALID_STT_PROVIDERS,
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    COMPLETION_KEEP_CHARS_MIN,
    DEDUP_EVERY_N_SWEEPS_MAX,
    EMBED_RATE_LIMIT_MAX,
    EXTRACTION_POOL_SIZE_MAX,
    EXTRACTION_POOL_SIZE_MIN,
    FOLDER_INGEST_CHUNK_BUDGET_MAX,
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
    KiroCrewConfig,
    config_path,
)
from kiro_crew.context_management import RESULT_FILE_MAX_BYTES
from kiro_crew.dashboard.handlers._shared import (
    _pip_install_channel_available,
    pip_extra_install_command,
)
from kiro_crew.dashboard.origin import check_host, is_direct_local_request
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.stt_stream import _STREAMING_PROVIDERS, PROVIDER_LOCAL
from kiro_crew.dashboard.token_auth import MAX_SESSION_TTL_SECS, generate_token, parse_duration
from kiro_crew.effort import EFFORT_LEVELS
from kiro_crew.executors import discovery_executor
from kiro_crew.metrics import provider as _metrics_provider
from kiro_crew.security_posture import build_posture_snapshot_async, posture_counts_async
from kiro_crew.session_workspace import is_valid_id
from kiro_crew.stt import decoder as stt_decoder
from kiro_crew.stt import models as stt_models
from kiro_crew.stt.limits import (
    MAX_IDLE_EVICT_SECS,
    MAX_INTERVAL_MS,
    MIN_IDLE_EVICT_SECS,
    MIN_PARTIAL_INTERVAL_MS,
    MIN_SILENCE_MS,
)
from kiro_crew.transcribe import (
    _find_ffmpeg,
    _whisper_language,
    availability_detail,
    ensure_ffmpeg_in_path,
    ffmpeg_source,
    is_available,
)

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

# Sentinel returned in place of sensitive config values in API responses. Kept
# distinct from "" so the UI can render a "set (hidden)" placeholder.
_SENSITIVE_MASK = "••••••••"


def _masked_config_dict(cfg: KiroCrewConfig) -> dict:
    """Return ``cfg.to_dict()`` with sensitive string values masked.

    Applied ONLY to the GET /api/config/kirocrew response — never to the value
    ``cfg.to_dict()`` / ``cfg.save()`` serialize, since masking there would
    persist the sentinel and destroy the real secret (e.g. ``telegram.bot_token``).
    Safe here because no config write endpoint accepts sensitive fields; if one
    is ever added it MUST treat ``_SENSITIVE_MASK`` as "unchanged" and keep the
    stored value. Sensitivity is schema-driven (``sensitive=True`` field
    metadata), so newly added sensitive fields are masked automatically.
    """
    from kiro_crew.config.schema import JSON_SCHEMA
    from kiro_crew.config.validation import _is_sensitive_path

    masked = copy.deepcopy(cfg.to_dict())

    # Drop unknown/edition-contributed top-level sections (KiroCrewConfig.
    # _extra_sections) from the API response entirely. They exist ONLY for the
    # save() round-trip; the core does not model them, so they are absent from
    # the schema and the sensitivity walk below (which is schema-driven) cannot
    # know which of their values are secrets. Returning them verbatim to the
    # dashboard would leak any credential an edition stored in its own section.
    # to_dict()/save() still carry them — only this browser-facing view omits
    # them. (An edition that needs to surface its config in the dashboard does
    # so through its own masked route, not this core endpoint.)
    for _extra_key in getattr(cfg, "_extra_sections", {}):
        masked.pop(_extra_key, None)

    def _walk(node: object, prefix: str) -> None:
        if isinstance(node, dict):
            for key, val in list(node.items()):
                path = f"{prefix}.{key}" if prefix else key
                if isinstance(val, dict):
                    _walk(val, path)
                elif isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict):
                            _walk(item, path)
                elif isinstance(val, str) and val and _is_sensitive_path(JSON_SCHEMA, path):
                    node[key] = _SENSITIVE_MASK

    _walk(masked, "")
    return masked


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


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811 — circular import

    return _pkg.sel()


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


def _liveness_payload(request: web.Request) -> dict[str, object]:
    """Return public liveness plus identity only for direct-local callers.

    Identity requires BOTH gates: a direct-local peer (loopback, no
    forwarding headers) AND a Host header naming a host we serve. The probe
    paths are exempt from the host_validation middleware (orchestrators
    address pods by IP — see origin.PROBE_PATHS), so a DNS-rebound loopback
    request CAN reach this handler with a forged Host; ``check_host`` here
    keeps the exact-version fingerprint off that path. A rebound page then
    learns only ``{"ok": true}`` — indistinguishable from the TCP connect
    succeeding, which it could already observe.
    """
    payload: dict[str, object] = {"ok": True}
    if is_direct_local_request(request) and check_host(request):
        # The desktop production/nightly cross-app guard calls over loopback and
        # needs exact identity to decide whether it can reuse the shared port.
        # Anonymous non-loopback probes get only the liveness bit, avoiding an
        # exact-version fingerprint on the public probe boundary.
        payload.update({"app": "kirocrew", "version": kiro_crew.__version__})
    return payload


async def api_health(request: web.Request) -> web.Response:
    """GET /api/health — liveness, with identity for direct-local callers."""
    return web.json_response(_liveness_payload(request))


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
    return web.json_response(_liveness_payload(request))


async def api_ready(request: web.Request) -> web.Response:
    """GET /api/ready — Kubernetes-style readiness probe.

    Distinct from liveness: the process may be UP (``/api/live`` 200) yet not
    able to serve application traffic. Readiness reflects the observable
    lifecycle state:

    * **Startup** — before the socket binds, connection failure is the external
      not-ready signal. After bind, ``DashboardState.ready`` remains false and
      the probe returns 503 while session restoration, channel relaunch, tunnel
      setup, and other startup work finish.
    * **Serving** — the server publishes ``DashboardState.ready = True`` at the
      same final boundary used by the boot-to-ready metric; readiness is then
      200 while required state is wired and shutdown has not been requested.
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
#: to the model as the UI language (#1130). A new backend consumer of
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
        "onboarded": cfg.dashboard.onboarded,
        "import_onboarded": cfg.dashboard.import_onboarded,
        "privacy_acked": cfg.dashboard.privacy_acked,
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
    {mode?, color?, language?, onboarded?, import_onboarded?} and persists to
    the workspace config file.
    """
    if request.method == "GET":
        cfg = KiroCrewConfig.load()
        return web.json_response(_theme_payload(cfg))

    # PUT
    body = await request.json()
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be an object")
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    async with _get_config_lock():
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        changed = False
        if "mode" in body:
            mode = body["mode"]
            if mode not in ("", "dark", "light", "system"):
                raise web.HTTPBadRequest(text="mode must be '', 'dark', 'light', or 'system'")
            if cfg.dashboard.theme_mode != mode:
                cfg.dashboard.theme_mode = mode
                changed = True
        if "color" in body:
            color = body["color"]
            if not isinstance(color, str) or len(color) > 64:
                raise web.HTTPBadRequest(text="color must be a string (max 64 chars)")
            if cfg.dashboard.theme_color != color:
                cfg.dashboard.theme_color = color
                changed = True
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
            if cfg.dashboard.language != language:
                cfg.dashboard.language = language
                changed = True
        if "onboarded" in body:
            onboarded = bool(body["onboarded"])
            if cfg.dashboard.onboarded != onboarded:
                cfg.dashboard.onboarded = onboarded
                changed = True
        if "import_onboarded" in body:
            import_onboarded = body["import_onboarded"]
            if not isinstance(import_onboarded, bool):
                raise web.HTTPBadRequest(text="import_onboarded must be a boolean")
            if cfg.dashboard.import_onboarded != import_onboarded:
                cfg.dashboard.import_onboarded = import_onboarded
                changed = True
        if "privacy_acked" in body:
            privacy_acked = body["privacy_acked"]
            if not isinstance(privacy_acked, bool):
                raise web.HTTPBadRequest(text="privacy_acked must be a boolean")
            if cfg.dashboard.privacy_acked != privacy_acked:
                cfg.dashboard.privacy_acked = privacy_acked
                changed = True

        if changed:
            await asyncio.to_thread(cfg.save)

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


# ── STT (Speech-to-Text) ──


#: Speech models accepted on PUT, mapped to their download size in BYTES — the
#: number that actually decides the choice on a laptop. DERIVED from the
#: sha256-pinned catalog rather than restated, so a model the recogniser can fetch
#: cannot be rejected by the API (or the reverse). Bytes rather than a formatted
#: label because the dashboard is translated into 12 languages: a server-side
#: "~148 MB" cannot follow the reader's locale, and the frontend formats it.
_STT_MODEL_SIZES: dict[str, int] = {m.name: m.size_bytes for m in stt_models.CATALOG}


def _stt_providers() -> list[str]:
    """STT provider values offered to the UI.

    ``local`` (the resident whisper.cpp recogniser) and ``transcribe`` (paid AWS)
    run everywhere. ``apple`` (the on-device SpeechAnalyzer framework) needs
    macOS 26 or later plus a Swift toolchain, so it is omitted entirely rather
    than shown as an option that cannot be selected. This is the single source of
    truth for which providers are advertised (GET) and accepted (PUT).
    """
    providers = list(_VALID_STT_PROVIDERS)
    if "apple" in providers:
        from kiro_crew import apple_speech

        if not apple_speech.availability().ok:
            providers.remove("apple")
    return providers


# Common BCP-47 language codes surfaced in the Chat Settings STT picker.
# The handler accepts any string value on PUT — this list only drives the UI
# dropdown. AWS Transcribe supports many more; advanced users can edit
# config.json directly.
_STT_LANGUAGE_CODES: tuple[str, ...] = (
    "en-US",
    "en-GB",
    "fr-FR",
    "de-DE",
    "es-ES",
    "es-US",
    "it-IT",
    "pt-BR",
    "ja-JP",
    "ko-KR",
    "zh-CN",
)


#: Machine-readable reasons on the STT endpoints' non-2xx bodies. The dashboard
#: renders localised text and cannot key off an English sentence, so the prose is
#: advisory and these are the contract. Codes the stt package already owns
#: (``stt_extra_missing``, ``stt_model_missing``, …) are forwarded unchanged.
_CODE_DASHBOARD_USER_REQUIRED = "dashboard_user_required"
_CODE_STT_UNAVAILABLE = "stt_unavailable"
_CODE_STT_MISSING_AUDIO = "stt_missing_audio_field"
_CODE_STT_AUDIO_TOO_LARGE = "stt_audio_too_large"
_CODE_STT_FAILED = "stt_transcription_failed"
#: A decoder fetch asked of a desktop release. Its own payload is the only decoder
#: it will run, so the remedy is reinstalling the app, not a download.
_CODE_STT_DECODER_BUNDLED = "stt_decoder_bundled"

#: Background model-download and prewarm tasks, held ONLY so the loop keeps a
#: strong reference: a task nobody references can be collected mid-await. Both
#: endpoints answer 202 and let the caller poll ``GET /api/stt/status``, because
#: the whole point of the pair is that a 148 MB fetch is not on the request the
#: user is waiting behind.
_stt_background_tasks: set[asyncio.Task[Any]] = set()


def _spawn_stt_background(coro: Coroutine[Any, Any, Any]) -> None:
    """Run *coro* detached, keeping a reference until it finishes."""
    task = asyncio.create_task(coro)
    _stt_background_tasks.add(task)
    task.add_done_callback(_stt_background_tasks.discard)


def _deny_app_token(request: web.Request, operation: str) -> web.Response | None:
    """Refuse an app token on the dashboard-only STT endpoints. 403 or None.

    ``request["user"]`` is truthy for an app token too, so a cookie check alone
    does not separate a browser from an app that declared this path in its
    manifest's ``permissions.api``. These endpoints start a model download and
    warm a resident model inside the gateway, which is operator setup rather than
    something an app earns by naming a path. The live transcription surfaces
    (``/api/ws/stt``, ``POST /api/stt/transcribe``) are deliberately NOT gated
    this way: shipped apps reach them on an app token.

    An absent ``app`` key is refused along with a non-empty one, so an
    unauthenticated route can only ever fail closed here.
    """
    if request.get("app") == "":
        return None
    # Best-effort: an unwrapped SEL failure here would replace the intended 403
    # with a 500, which is the one outcome a refusal must never turn into.
    try:
        _sel().log_api_access(
            caller=str(request.get("app") or request.get("user") or "unknown"),
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error="dashboard user required",
        )
    except Exception:
        logger.warning("SEL logging failed for %s", operation, exc_info=True)
    return web.json_response(
        {"error": "dashboard user required", "code": _CODE_DASHBOARD_USER_REQUIRED},
        status=403,
    )


def _stt_positive_int(body: dict, key: str, *, minimum: int, maximum: int) -> int | None:
    """*body*'s value for *key* when it is an int inside the range, else None.

    Both ends are required rather than defaulted, because a knob accepted here and
    then clamped by the config loader is worse than one refused: the value the user
    reads back would not be the value in force.

    ``bool`` is excluded explicitly because it subclasses ``int``: without the
    check a client sending a checkbox value would persist ``True`` as ``1``.
    """
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if minimum <= value <= maximum else None


async def api_stt_config(request: web.Request) -> web.Response:
    """GET/PUT /api/config/stt — speech-to-text settings."""
    cfg = KiroCrewConfig.load()
    if request.method == "PUT":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        path = config_path()
        from kiro_crew.agent import _atomic_json_write  # noqa: F811
        from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

        # Serialize the full read-modify-write behind the shared config lock so
        # concurrent PUTs (or another config writer) can't interleave and clobber
        # each other's fields, and write atomically (temp + fsync + os.replace)
        # so a crash mid-write can't leave a corrupt config JSON — matching the
        # established pattern used by the other config handlers in this module.
        async with _get_config_lock():
            try:
                raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
                data = json.loads(raw)
            except FileNotFoundError:
                data = {}
            except Exception:
                # Fail loud on a corrupt config rather than proceeding with {}:
                # an atomic write from a {} base would durably clobber every
                # other user setting with an stt-only file. Matches the sibling
                # config handler in this module, which returns 500 on an
                # unparseable config instead of silently resetting it.
                logger.warning("STT config PUT: config.json is unparseable", exc_info=True)
                return web.json_response({"error": "failed to read config file"}, status=500)
            stt_section = data.setdefault("stt", {})
            if "enabled" in body:
                stt_section["enabled"] = bool(body["enabled"])
            # Guard the type before either membership lookup.  The model catalog
            # is a dict, so a JSON object or array would otherwise raise
            # ``TypeError: unhashable type`` and turn this partial update into a
            # 500.  Wrong-typed fields follow the existing config contract: skip
            # that field while still applying valid siblings.
            if (
                "provider" in body
                and isinstance(body["provider"], str)
                and body["provider"] in _stt_providers()
            ):
                stt_section["provider"] = body["provider"]
            if (
                "model" in body
                and isinstance(body["model"], str)
                and body["model"] in _STT_MODEL_SIZES
            ):
                stt_section["model"] = body["model"]
            if "transcribe_region" in body and isinstance(body["transcribe_region"], str):
                stt_section["transcribe_region"] = body["transcribe_region"]
            if "transcribe_profile" in body and isinstance(body["transcribe_profile"], str):
                stt_section["transcribe_profile"] = body["transcribe_profile"]
            if "language_code" in body and isinstance(body["language_code"], str):
                stt_section["language_code"] = body["language_code"]
            if "streaming" in body and isinstance(body["streaming"], bool):
                stt_section["streaming"] = body["streaming"]
            if "endpointing" in body and isinstance(body["endpointing"], bool):
                stt_section["endpointing"] = body["endpointing"]
            if "dictation_panel" in body and isinstance(body["dictation_panel"], bool):
                stt_section["dictation_panel"] = body["dictation_panel"]
            # Every bound comes from kiro_crew.stt.limits, which is what the
            # recogniser itself reads, and the endpoint accepts exactly the range
            # the config loader will keep. Refusing here rather than storing a
            # value the loader then clamps is the whole point: a setting a user
            # reads back has to be the setting in force. MIN_IDLE_EVICT_SECS is 0
            # and it means "release the model as soon as it goes idle", not
            # "never release".
            silence_ms = _stt_positive_int(
                body, "silence_ms", minimum=MIN_SILENCE_MS, maximum=MAX_INTERVAL_MS
            )
            if silence_ms is not None:
                stt_section["silence_ms"] = silence_ms
            partial_interval = _stt_positive_int(
                body,
                "partial_interval_ms",
                minimum=MIN_PARTIAL_INTERVAL_MS,
                maximum=MAX_INTERVAL_MS,
            )
            if partial_interval is not None:
                stt_section["partial_interval_ms"] = partial_interval
            idle_evict = _stt_positive_int(
                body,
                "idle_evict_secs",
                minimum=MIN_IDLE_EVICT_SECS,
                maximum=MAX_IDLE_EVICT_SECS,
            )
            if idle_evict is not None:
                stt_section["idle_evict_secs"] = idle_evict
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(_atomic_json_write, path, data)
        cfg = KiroCrewConfig.load()

    provider = cfg.stt.provider
    # Every probe below touches the filesystem or imports an optional extra, so
    # none of them belongs on the event loop, and they ride one thread rather than
    # several: _stt_prereq_commands resolves ffmpeg and Homebrew, the
    # install-channel probe reads the PEP 668 marker, and availability_detail
    # imports the recogniser (`local`) or the AWS client (`transcribe`). Leaving
    # the availability probe out of the thread would put the heaviest one of the
    # set outside the thread that exists to hold the lighter ones. Windows was
    # where this first showed up, as "event-loop heartbeat: lag".

    def _prereqs_and_probes() -> tuple[list[str], bool, bool, bool, bool]:
        cmds = _stt_prereq_commands(provider)
        ensure_ffmpeg_in_path()
        # `_find_ffmpeg`, not a bare `which`: the settings panel must report on the
        # binary the transcode path would actually run, and that one ignores PATH.
        no_ffmpeg = _find_ffmpeg() is None
        unsupported = not _transcribe_extra_importable() and not _pip_install_channel_available()
        # The bundled desktop app is the one unsupported cause with different
        # user guidance (no Python environment of the user's own to fix), so
        # the UI needs to distinguish it from the pip-less/PEP 668 causes.
        bundled = platform_compat.is_bundled_interpreter()
        return cmds, no_ffmpeg, unsupported, bundled, is_available(cfg.stt)

    (
        prereqs,
        ffmpeg_missing,
        transcribe_unsupported,
        bundled_app,
        available,
    ) = await asyncio.to_thread(_prereqs_and_probes)
    return web.json_response(
        {
            "enabled": cfg.stt.enabled,
            "provider": provider,
            "model": cfg.stt.model,
            "available": available,
            "streaming": cfg.stt.streaming,
            "endpointing": cfg.stt.endpointing,
            "dictation_panel": cfg.stt.dictation_panel,
            "transcribe_region": cfg.stt.transcribe_region,
            "transcribe_profile": cfg.stt.transcribe_profile,
            "language_code": cfg.stt.language_code,
            "silence_ms": cfg.stt.silence_ms,
            "partial_interval_ms": cfg.stt.partial_interval_ms,
            "idle_evict_secs": cfg.stt.idle_evict_secs,
            # The PUT allowlist, so a picker built from it cannot offer a value
            # this endpoint would reject. Which of them are already on disk is
            # runtime state and belongs to GET /api/stt/status, which is also what
            # a panel polls during a transfer — probing four files on every config
            # read would put that cost on the wrong request.
            "models": _STT_MODEL_SIZES,
            "providers": _stt_providers(),
            # Which of those providers can stream partial results. Served from the
            # backend's own `_STREAMING_PROVIDERS` so the Settings UI gates the
            # streaming controls on a CAPABILITY rather than on a hardcoded provider
            # name — the latter silently hid the toggle when `apple` was added.
            "streaming_providers": list(_STREAMING_PROVIDERS),
            "language_codes": list(_STT_LANGUAGE_CODES),
            "prereqs": prereqs,
            # True when no install channel can make Transcribe's import
            # requirement (`boto3` + `amazon-transcribe`) satisfiable in this
            # process — frozen build, pip-less interpreter, or PEP 668
            # externally-managed python. The Settings page shows an unsupported
            # notice instead of an empty prerequisite panel. Computed in the
            # threaded probe above: find_spec and the marker check touch the
            # filesystem.
            "transcribe_unsupported": transcribe_unsupported,
            "bundled_interpreter": bundled_app,
            # ffmpeg is required to decode the browser's .webm on the batch upload
            # path, but is_available() only logs a warning when it is absent — so
            # availability can read "ready" while an upload would fail. Served
            # separately so the UI can surface the gap even when the provider is
            # otherwise available.
            "ffmpeg_missing": ffmpeg_missing,
        }
    )


async def api_stt_status(request: web.Request) -> web.Response:
    """GET /api/stt/status — whether speech recognition can run, and what it needs.

    Distinct from ``GET /api/config/stt``, which serves the operator's settings:
    this is the runtime state a panel polls — the availability reason as a code
    rather than prose, whether the configured model is on disk, whether a model is
    resident right now, and the live progress of a transfer started by
    ``POST /api/stt/prepare``.
    """
    denied = _deny_app_token(request, "stt.status")
    if denied is not None:
        return denied
    cfg = KiroCrewConfig.load()
    model = stt_models.resolve(cfg.stt.model)

    # availability_detail imports the recogniser (or the AWS client), and each
    # is_present stats a model file: none of it belongs on the loop.
    def _probe() -> tuple[stt.Availability, list[dict[str, object]], bool, str | None]:
        # kiro_crew.stt.engine is imported HERE rather than at module scope: it
        # pulls numpy, and this module is imported on the gateway boot path, where
        # a gateway with speech-to-text switched off would otherwise pay for an
        # array library before it binds its socket.
        from kiro_crew.stt import engine as stt_engine

        catalog = [
            {"name": m.name, "size_bytes": m.size_bytes, "present": stt_models.is_present(m)}
            for m in stt_models.CATALOG
        ]
        ensure_ffmpeg_in_path()
        # Resolved on the same thread as the rest: it lists a store directory and,
        # when a candidate is there, hashes up to 80 MB to authenticate it.
        return (
            availability_detail(cfg.stt),
            catalog,
            stt_engine.shared_engine().loaded,
            ffmpeg_source(),
        )

    detail, catalog, engine_loaded, decoder_source = await asyncio.to_thread(_probe)
    present = {str(row["name"]): bool(row["present"]) for row in catalog}
    return web.json_response(
        {
            "provider": cfg.stt.provider,
            "available": detail.ok,
            "code": detail.code,
            "detail": detail.detail,
            "model": model.name,
            "model_present": present.get(model.name, False),
            "model_bytes": model.size_bytes,
            # The whole catalog, in the order the picker offers it (smallest
            # first), with sizes as bytes so the dashboard formats them in the
            # reader's locale. `present` is why this cannot be a static frontend
            # table: it is per-host state that changes as models are fetched.
            "models": catalog,
            # Whether a model is resident in this process, which is what decides
            # between a 30 ms transcription and one that pays a load first.
            "engine_loaded": engine_loaded,
            "download": dict(stt_models.store().status),
            # The decoder every compressed input goes through, and what can be
            # done about it. `source` names WHICH of the three the transcode path
            # would run, because each one is repaired differently; `auto_fetch`
            # says whether this host can be fixed in place at all, so the panel
            # offers a button instead of a shell command; `os`/`arch` are the
            # GATEWAY's, not the browser's, and are what a hand-off to an agent
            # session needs in order to name the right remedy.
            "ffmpeg": {
                "present": decoder_source is not None,
                "source": decoder_source,
                "auto_fetch": _ffmpeg_auto_fetch(),
                "os": platform.system(),
                "arch": platform.machine(),
                "download": dict(stt_decoder.store().status),
            },
        }
    )


async def api_stt_prepare(request: web.Request) -> web.Response:
    """POST /api/stt/prepare — start, or join, the one-time speech-model download.

    Answers 202 immediately with the current transfer state; the caller polls
    ``GET /api/stt/status`` for progress. Concurrent callers share one transfer:
    the model store serialises them behind its own lock, so pressing this twice
    cannot start two downloads of the same file.

    An optional ``{"model": name}`` body fetches a model the operator has not
    saved yet, so the picker can offer the weights BEFORE the selection is
    committed. Only catalog names reach the network: an unknown one resolves to
    the default with a logged reason, the same as the configured value does.
    """
    denied = _deny_app_token(request, "stt.prepare")
    if denied is not None:
        return denied
    cfg = KiroCrewConfig.load()
    try:
        body = await request.json()
    except Exception:
        # No body, or an unparseable one. Both mean "the configured model", which
        # is the only reading that makes this endpoint useful without a client.
        body = {}
    requested = body.get("model") if isinstance(body, dict) else None
    name = requested if isinstance(requested, str) and requested else cfg.stt.model
    model = stt_models.resolve(name)
    if stt_models.store().status.get("step") != "downloading":
        # Skipped while a transfer is already running purely so a polling panel
        # cannot accumulate tasks; the store's lock, not this check, is what makes
        # concurrent callers safe.
        _spawn_stt_background(stt.ensure_model(name))
    return web.json_response(
        {"model": model.name, "download": dict(stt_models.store().status)}, status=202
    )


#: Values of the status endpoint's ``ffmpeg.auto_fetch``. ``bundled`` is not
#: "available on a desktop app": a release carries its own authenticated decoder
#: and repairs itself by being reinstalled, so downloading one there would install
#: a second decoder the bundled resolver refuses to look at by design.
AUTO_FETCH_AVAILABLE = "available"
AUTO_FETCH_UNSUPPORTED = "unsupported"
AUTO_FETCH_BUNDLED = "bundled"


def _ffmpeg_auto_fetch() -> str:
    """Whether this host's decoder can be fetched, and if not, why not."""
    if platform_compat.is_bundled_interpreter():
        return AUTO_FETCH_BUNDLED
    if stt_decoder.artifact_for() is None:
        return AUTO_FETCH_UNSUPPORTED
    return AUTO_FETCH_AVAILABLE


async def api_stt_ffmpeg_download(request: web.Request) -> web.Response:
    """POST /api/stt/ffmpeg/download — start, or join, the decoder fetch.

    Answers 202 with the current transfer state and lets the caller poll
    ``GET /api/stt/status``, for the same reason ``POST /api/stt/prepare`` does: a
    ~30 MB wheel is not something to hold a request open behind. Concurrent
    callers share one transfer through the store's own lock.

    Refused on a bundled interpreter rather than quietly answering 202: a desktop
    release already carries an authenticated decoder, its resolver deliberately
    never looks anywhere else, and so a fetch there would spend the operator's
    bandwidth on a file nothing can use.
    """
    denied = _deny_app_token(request, "stt.ffmpeg_download")
    if denied is not None:
        return denied
    auto_fetch = _ffmpeg_auto_fetch()
    if auto_fetch != AUTO_FETCH_AVAILABLE:
        return web.json_response(
            {
                "error": "no decoder can be fetched for this install",
                "code": (
                    stt_decoder.CODE_UNSUPPORTED
                    if auto_fetch == AUTO_FETCH_UNSUPPORTED
                    else _CODE_STT_DECODER_BUNDLED
                ),
                "auto_fetch": auto_fetch,
            },
            status=409,
        )
    store = stt_decoder.store()
    if store.status.get("stage") != stt_decoder.STAGE_DOWNLOADING:
        # Skipped while a transfer is already running purely so a polling panel
        # cannot accumulate tasks; the store's lock, not this check, is what makes
        # concurrent callers safe.
        _spawn_stt_background(store.ensure())
    return web.json_response({"download": dict(store.status)}, status=202)


async def api_stt_prewarm(request: web.Request) -> web.Response:
    """POST /api/stt/prewarm — load and warm the recogniser ahead of the microphone.

    Fire-and-forget, and called when the user reaches for the mic rather than when
    they release it: a first-ever model load compiles a GPU pipeline (measured at
    7.4 s) and the first decode after any load allocates its graph (154-528 ms), so
    both are paid while the user is still speaking instead of after.
    """
    denied = _deny_app_token(request, "stt.prewarm")
    if denied is not None:
        return denied
    cfg = KiroCrewConfig.load()
    if cfg.stt.provider != PROVIDER_LOCAL:
        # Prewarming is specific to the resident whisper.cpp model. Running it under
        # `apple` or `transcribe` loaded — and on a first run DOWNLOADED — 148 MB of
        # weights the configured provider will never decode with, triggered by
        # nothing more than the user reaching for the microphone.
        return web.json_response(
            {"ok": True, "skipped": "provider_not_local"},
            status=202,
        )
    _spawn_stt_background(
        stt.prewarm(
            model_name=cfg.stt.model,
            language=_whisper_language(cfg.stt.language_code),
        )
    )
    return web.json_response({"ok": True}, status=202)


def _transcribe_extra_importable() -> bool:
    """True when AWS Transcribe's half of the ``voice`` extra imported here.

    Reads ``kiro_crew.transcribe``'s own import outcome (its module-level
    try/except sets ``boto3 = None`` on failure) rather than probing specs: a
    partial installation whose dist-info exists but whose import fails must
    surface the repair command, not suppress it. Runs off the event loop —
    ``transcribe`` is already imported at module load, so this is an attribute
    read, but callers batch it with the other filesystem probes anyway.
    """
    from kiro_crew import transcribe

    if transcribe.boto3 is None:
        return False
    try:
        import amazon_transcribe  # noqa: F401
    except ImportError:
        return False
    return True


def _ffmpeg_install_commands() -> list[str]:
    """System-decoder commands a source install can actually run, else ``[]``.

    An empty list means "there is nothing a terminal can usefully be told here",
    and the Settings page then offers the decoder fetch or a hand-off to an agent
    session instead. It is not the same as "nothing is wrong": ``ffmpeg.present``
    on ``GET /api/stt/status`` is what says whether a decoder exists.

    There is deliberately no fallback command. A distribution with no FFmpeg
    package (Amazon Linux, RHEL without EPEL) and no build script in reach used to
    be handed ``echo 'Build ffmpeg from source: …'``, which a user pasted into a
    terminal and got a URL echoed back at them -- a command whose only effect is to
    print a sentence is a dead end wearing the costume of an instruction.
    """
    ensure_ffmpeg_in_path()
    if _find_ffmpeg():
        return []
    system = platform.system()
    if system == "Darwin":
        return ["brew install ffmpeg"]
    if system == "Windows":
        return ["winget install --id Gyan.FFmpeg"]
    if shutil.which("apt-get"):
        return ["sudo apt-get install -y ffmpeg"]
    # Amazon Linux: no ffmpeg in the distro repos. A source build is the only
    # honest answer, and only when the script is actually present -- naming a path
    # that does not exist is worse than saying nothing.
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    script = os.path.join(proj, "scripts", "build-ffmpeg.sh") if proj else ""
    if script and os.path.isfile(script):
        return [
            "sudo dnf install -y gcc make nasm diffutils 2>/dev/null"
            " || sudo yum install -y gcc make nasm diffutils",
            f"bash {shlex.quote(script)}",
        ]
    return []


def _stt_prereq_commands(provider: str = "local") -> list[str]:
    """Shell commands the user has to run themselves (they need sudo, a GUI, or a shell).

    Deliberately short, and there is no install button behind it any more. Desktop
    releases already include both runtime pieces. A source install may need the
    optional ``voice`` extra plus system ffmpeg for batch WebM/voice-memo input,
    while ``local`` fetches its own model.

    Desktop builds bundle the extra and must never suggest installing a system
    dependency. A source install using Apple's OS recogniser can still use a
    system ffmpeg as a fallback when it did not install the voice extra.

    An empty list means "nothing to do", which is the steady state.
    """
    cmds: list[str] = []
    # Which extra to name depends on the provider, because the two halves are
    # separately installable and an extra resolves ATOMICALLY: advising the full
    # `voice` set to a cloud-only user drags in the local recogniser, which has
    # no wheel on some platforms and fails the whole install.
    extra = ""
    if provider == PROVIDER_LOCAL:
        # Only the missing-extra case is actionable by pip. A platform with no
        # prebuilt wheel needs a C++ toolchain instead, and the availability
        # `detail` on GET /api/stt/status is what says so.
        needs_extra = stt.availability().code == stt.CODE_EXTRA_MISSING
        extra = "voice"
    elif provider == "transcribe":
        needs_extra = not _transcribe_extra_importable()
        extra = "voice-aws"
    else:
        needs_extra = False
    # Suppressed where no pip channel into this interpreter exists — frozen build,
    # code-signed app bundle, pip-less or PEP 668 python. The Settings page shows
    # an unsupported notice there instead of a command that cannot succeed.
    if needs_extra and _pip_install_channel_available():
        command = pip_extra_install_command(extra)
        if command:
            cmds.append(command)
    if not platform_compat.is_bundled_interpreter():
        cmds.extend(_ffmpeg_install_commands())
    return cmds


async def api_stt_transcribe(request: web.Request) -> web.Response:
    """POST /api/stt/transcribe — transcribe one uploaded recording.

    The batch counterpart to ``/api/ws/stt``, for a client that records first and
    uploads afterwards. It accepts every provider: ``local`` shares the resident
    model with live sessions, so a voice memo lands on a model that is already
    loaded.
    """
    import tempfile  # noqa: F811
    import uuid

    from kiro_crew.dashboard import part_stream
    from kiro_crew.transcribe import transcribe_audio  # noqa: F811

    cfg = KiroCrewConfig.load()
    # Off the loop: every provider branch of the probe reaches the filesystem, and
    # `local` and `transcribe` each import an optional extra the first time.
    detail = await asyncio.to_thread(availability_detail, cfg.stt)
    if not detail.ok:
        return web.json_response(
            {
                "error": detail.detail or "STT not available",
                "code": detail.code or _CODE_STT_UNAVAILABLE,
            },
            status=503,
        )

    reader = await request.multipart()
    field = await reader.next()
    if field is None or not hasattr(field, "name") or field.name != "audio":  # type: ignore[union-attr]
        return web.json_response(
            {"error": "missing audio field", "code": _CODE_STT_MISSING_AUDIO}, status=400
        )

    # Use uploaded filename extension (recording.webm / .mp4 / .ogg)
    fname = getattr(field, "filename", None) or "recording.webm"
    ext = os.path.splitext(fname)[1] or ".webm"
    # A fresh unpublished path: stream_part_to_file writes to a sibling temp
    # off the event loop and publishes here atomically, so no exit path (413,
    # backend failure, cancellation) can leave a partial file at this name.
    tmp = os.path.join(tempfile.gettempdir(), f"kc_stt_{uuid.uuid4().hex}{ext}")
    try:
        try:
            await part_stream.stream_part_to_file(
                field,  # type: ignore[arg-type]
                Path(tmp),
                max_bytes=25 * 1024 * 1024,
            )
        except part_stream.PartTooLarge:
            return web.json_response(
                {"error": "audio too large", "code": _CODE_STT_AUDIO_TOO_LARGE}, status=413
            )

        text = await transcribe_audio(tmp)
        if text:
            from kiro_crew.security import (  # noqa: F811
                redact_credentials,
                redact_exfiltration_urls,
            )

            text, _ = redact_exfiltration_urls(text)
            text, _ = redact_credentials(text)
        return web.json_response({"text": text or ""})
    except Exception:
        logger.exception("STT transcribe failed")
        return web.json_response(
            {"error": "transcription failed", "code": _CODE_STT_FAILED}, status=500
        )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── Security Event Log API ──


async def api_sel_events(request: web.Request) -> web.Response:
    """GET /api/sel/events — recent security events."""

    try:
        limit = min(int(request.query.get("limit", "100")), 1000)
    except (TypeError, ValueError):
        limit = 100
    # recent() reads the WHOLE audit-log file with blocking IO: it is one JSONL
    # file pruned by age, so `limit` bounds the rows returned, not the bytes
    # read. Called inline it stalls the whole event loop, so it must be
    # offloaded. Use the DISCOVERY pool, not maintenance_executor: this handler
    # is browser-triggerable, so multiple tabs or pollers could otherwise occupy
    # the workers the orphan-reaping sweeps need to recover from an event-loop
    # wedge.
    # _sel() is called INSIDE the callable, not while building it: the first
    # call constructs the singleton, which reads/creates the HMAC key and scans
    # the log tail. Evaluating it here would leave that IO on the loop.
    events = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), lambda: _sel().recent(limit=limit)
    )
    return web.json_response({"events": events, "count": len(events)})


async def api_sel_verify(request: web.Request) -> web.Response:
    """GET /api/sel/verify — verify HMAC chain integrity.

    ``integrity`` is ``unverifiable`` when the segment dir refused to pin (or
    was swapped mid-verification): the rotated segments were not checked, and
    the endpoint must not answer ``ok`` over the live log alone (#5051
    review). ``detail`` carries the reason and is empty when verifiable.
    """

    # Same offload rationale as api_sel_events, including deferring _sel() into
    # the callable: verify_integrity() reads the whole log file to check the HMAC
    # chain end to end and must not run on the event loop.
    result = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), lambda: _sel().verify_integrity(detailed=True)
    )
    if not result.history_verifiable:
        integrity = "unverifiable"
    elif result.total == result.valid:
        integrity = "ok"
    else:
        integrity = "compromised"
    return web.json_response(
        {
            "total": result.total,
            "valid": result.valid,
            "integrity": integrity,
            "tampered": result.total - result.valid,
            "detail": result.reason,
        }
    )


async def api_security_stats(_request: web.Request) -> web.Response:
    """GET /api/security/stats — live security feature counts.

    Every count is DERIVED from the control it describes (``security_posture``),
    so a pill can never drift from the thing it claims to measure. ``denied_commands``
    is the user/governance-effective count, which the posture registry deliberately
    does not carry: the registry lists the built-in RULE TABLE (what ships), while
    this field reports what is currently enforced after opt-outs and policy pins.

    The dashboard does not call this — Settings → Security reads
    ``/api/security/posture``, which carries these same counts PLUS the items behind
    them. Kept as a stable, narrow counts-only endpoint for external/API callers.
    Uses ``posture_counts_async`` rather than the full snapshot so serving three
    integers does not build (and serialize) the whole ~45 KB item payload.
    """
    denied = 0
    try:
        from kiro_crew.dashboard.handlers.security import build_denied_commands_snapshot_async

        # Offloaded to a thread executor — reads denied_commands.json + walks the
        # governance profile store (blocking FS I/O) off the event loop.
        denied = (await build_denied_commands_snapshot_async())["effective_count"]
    except Exception:
        logger.warning("Failed to load denied commands count", exc_info=True)

    counts = await posture_counts_async()
    return web.json_response(
        {
            "denied_commands": denied,
            "suspicious_patterns": counts.get("suspicious_patterns"),
            "tool_schemas": counts.get("tool_schemas"),
            "redaction_paths": counts.get("redaction_paths"),
        }
    )


async def api_security_posture(_request: web.Request) -> web.Response:
    """GET /api/security/posture — expandable detail behind each posture count.

    Read-only and posture-only: control definitions and derived counts, never
    credential material, governance rule contents, or user data. See
    ``security_posture`` for the disclosure contract.
    """
    return web.json_response(await build_posture_snapshot_async())


# ── KiroCrew Config API ──
# The security-relevant ceilings (SUBAGENT_AUTO_MAX_CEILING,
# SUBAGENT_MAX_TURNS_CEILING) are imported from ``config.loader`` — the single
# source of truth shared by this API-write gate and the loader's load-time
# clamp, so the two cannot drift apart. subagent_auto_max is the security cap
# that bounds max_subagents, so it needs its own hard upper bound to stop a
# caller raising it arbitrarily (e.g. {"subagent_auto_max": 9999}) to bypass
# the concurrency limit.

# Agent settings whose ENFORCED effect is fixed at gateway startup.
# ``SubagentManager`` is constructed with ``max_subagents`` and
# ``subagent_max_turns`` and never re-reads the config afterwards;
# ``max_concurrent`` is stored once with no setter, and ``subagent_auto_max``
# only reaches that enforced value as the ``hard_cap`` inside
# ``compute_max_subagents``, which the same construction calls.
#
# Precisely: persisting one of these does NOT change what the running gateway
# ENFORCES. It is not inert, though — the advisory cap advertised to the model
# re-resolves from config on each read, so after a write the reported cap can
# move while the enforced one stays put. That divergence is pre-existing and
# deliberate (overflow queues, so the advertised number is guidance rather than
# a limit); this constant describes only the enforced side, which is what the
# restart is for.
#
# ``dynamic-subagent-sizing.md`` states the contract this mirrors: "The cap is
# computed once per gateway start. Restart to recompute." The ``restart_required``
# response field is the existing convention for exactly this case — the channel
# config handlers already return it for settings read at boot, and the frontend
# API client already types it.
#
# ``conductor_skill`` is deliberately absent: it is applied inline by this
# handler (the skill file is regenerated/removed in-request), so it takes effect
# immediately and must not raise the restart hint.
_STARTUP_READ_AGENT_KEYS = frozenset(
    {
        "max_subagents",
        "subagent_max_turns",
        "subagent_auto_max",
    }
)


async def api_kirocrew_config(request: web.Request) -> web.Response:
    """GET/PUT /api/config/kirocrew — read or update KiroCrew config."""
    # Re-imported at call time (not reused from the module-level binding) so a
    # test that redirects ``kiro_crew.config.loader.config_path`` at a temp path
    # is observed by this handler.
    from kiro_crew.config.loader import config_path  # noqa: F811

    if request.method == "PUT":
        caller = request.get("user", "dashboard")

        def _deny(error: str, status: int = 400) -> web.Response:
            _sel().log_api_access(
                caller=caller,
                operation="config.update",
                outcome="denied",
                error=error,
            )
            return web.json_response({"error": error}, status=status)

        try:
            body = await request.json()
        except Exception:
            return _deny("invalid JSON")
        agent_settings = body.get("agent")
        if not isinstance(agent_settings, dict):
            return _deny("agent must be an object")
        cfg_path = config_path()
        # Validate-only (CPU-bound) before acquiring the lock — fail fast on
        # obviously-bad input so the lock hold is as short as possible.
        # The actual read-modify-write is serialised under _get_config_lock and
        # offloaded to a thread so it neither races concurrent writers (lost-write
        # bug) nor blocks the event loop (event-loop-stall bug).  This mirrors the
        # pattern used by the sibling PATCH handler (~line 2031).
        from kiro_crew.config.loader import ConfigReadError, update_config_locked  # noqa: F811
        from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

        # Carry the validation error and result out of the mutate callback.
        # Validation that depends on the *persisted* ceiling (max_subagents bound)
        # runs inside the callback where it can read the current config; the
        # callback also emits the "no recognized settings provided" 400, so no
        # pre-lock key-recognition check is needed.
        _validation_error: list[tuple[str, int]] = []
        _result: dict[str, object] = {}

        def _mutate_config_put(data: dict) -> dict | None:
            if not isinstance(data.get("agent"), dict):
                data["agent"] = {}
            agent = data["agent"]
            # Snapshot BEFORE mutation for the restart-hint truthfulness guard.
            # The dashboard sends all settings on every save so "was applied" !=
            # "was changed" — see the no-op-save comments in messaging.py.
            before = dict(agent)

            limits = {"subagent_max_turns": SUBAGENT_MAX_TURNS_CEILING}
            applied: list[str] = []
            for key, upper in limits.items():
                if key in agent_settings:
                    val = agent_settings[key]
                    if isinstance(val, bool) or not isinstance(val, int) or val < 1 or val > upper:
                        _validation_error.append(
                            (f"{key} must be an integer between 1 and {upper}", 400)
                        )
                        return None
                    agent[key] = val
                    applied.append(key)

            # Capture the hard cap from the *persisted* config BEFORE applying any
            # subagent_auto_max from this request — deny-by-default prevents a
            # same-request ceiling-raise+spend.
            persisted_hard_cap = agent.get("subagent_auto_max", 16)
            if (
                not isinstance(persisted_hard_cap, int)
                or isinstance(persisted_hard_cap, bool)
                or persisted_hard_cap < 3
            ):
                persisted_hard_cap = 16
            persisted_hard_cap = min(persisted_hard_cap, SUBAGENT_AUTO_MAX_CEILING)

            if "subagent_auto_max" in agent_settings:
                val = agent_settings["subagent_auto_max"]
                if (
                    isinstance(val, bool)
                    or not isinstance(val, int)
                    or val < 3
                    or val > SUBAGENT_AUTO_MAX_CEILING
                ):
                    _validation_error.append(
                        (
                            "subagent_auto_max must be an integer between 3 and "
                            f"{SUBAGENT_AUTO_MAX_CEILING}",
                            400,
                        )
                    )
                    return None
                agent["subagent_auto_max"] = val
                applied.append("subagent_auto_max")

            if "max_subagents" in agent_settings:
                val = agent_settings["max_subagents"]
                hard_cap = persisted_hard_cap
                if (
                    isinstance(val, bool)
                    or not isinstance(val, int)
                    or (val != 0 and not (MAX_SUBAGENTS_FIXED_FLOOR <= val <= hard_cap))
                ):
                    _validation_error.append(
                        (
                            f"max_subagents must be 0 (auto) or an integer between "
                            f"{MAX_SUBAGENTS_FIXED_FLOOR} and {hard_cap}",
                            400,
                        )
                    )
                    return None
                agent["max_subagents"] = val
                applied.append("max_subagents")

            for key in ("conductor_skill",):
                if key in agent_settings:
                    val = agent_settings[key]
                    if not isinstance(val, bool):
                        _validation_error.append((f"{key} must be a boolean", 400))
                        return None
                    agent[key] = val
                    applied.append(key)

            if not applied:
                _validation_error.append(("no recognized settings provided", 400))
                return None

            restart_required = any(
                key in _STARTUP_READ_AGENT_KEYS and agent.get(key) != before.get(key)
                for key in applied
            )
            _result["applied"] = applied
            _result["restart_required"] = restart_required
            return data

        try:
            async with _get_config_lock():
                try:
                    # update_config_locked returns the final config dict (after
                    # mutation); use it directly rather than re-reading from disk
                    # (a blocking read on the loop, and it writes the callback's
                    # output verbatim — there is no concurrent merge to observe).
                    final = await asyncio.to_thread(
                        update_config_locked, cfg_path, mutate=_mutate_config_put
                    )
                except ConfigReadError:
                    _sel().log_api_access(
                        caller=caller,
                        operation="config.update",
                        outcome="error",
                        error="config.json is corrupt",
                    )
                    return web.json_response(
                        {"error": "config.json is corrupt", "code": "config_corrupt"},
                        status=500,
                    )

                if _validation_error:
                    msg, status = _validation_error[0]
                    return _deny(msg, status)

                applied: list[str] = _result["applied"]  # type: ignore[assignment]
                agent = final.get("agent") or {}
                _sel().log_api_access(
                    caller=caller,
                    operation="config.update",
                    outcome="ok",
                    resources=",".join(applied),
                )
                # Regenerate or clean up conductor skill on toggle. Held INSIDE
                # the lock so a concurrent enable/disable cannot interleave and
                # leave the persisted flag disagreeing with the skill file on
                # disk (config says enabled while SKILL.md is absent, or vice
                # versa).
                if "conductor_skill" in applied:
                    if agent.get("conductor_skill"):
                        from kiro_crew.dashboard.handlers.agents import (  # noqa: F811
                            _regen_conductor,
                        )

                        _regen_conductor()
                    else:
                        try:
                            from kiro_crew.skills import SkillsLoader  # noqa: F811

                            p = SkillsLoader()._dir / "conductor" / "SKILL.md"
                            if p.exists():
                                p.unlink()
                        except Exception:
                            logger.exception("Failed to clean up conductor skill")
        except OSError:
            _sel().log_api_access(
                caller=caller,
                operation="config.update",
                outcome="error",
                error="config.json write failed",
            )
            return web.json_response(
                {"error": "failed to write config file", "code": "config_write_failed"},
                status=500,
            )

        restart_required: bool = _result["restart_required"]  # type: ignore[assignment]
        return web.json_response({"ok": True, "restart_required": restart_required})

    cfg = KiroCrewConfig.load()
    return web.json_response(_masked_config_dict(cfg))


# Allowed editable config paths and their validators
def _agent_values() -> set[str]:
    """Return allowed pool_agent values: empty string + all configured agent names."""
    from kiro_crew.config.loader import KiroCrewConfig

    return {"", *KiroCrewConfig.load().agents}


def _active_advertised_ids(request: web.Request) -> list[str] | None:
    """Advertised model ids from the first active provider, or None if unknown.

    Uses the shared :func:`advertised_model_ids` shape parser so this
    validation sees exactly what the session-init withhold check sees. Returns
    ``None`` when no session has initialized / nothing was advertised, so callers
    treat entitlement as UNKNOWN rather than denying on no evidence.
    """
    from kiro_crew.acp.client import advertised_model_ids

    try:
        providers = request.app["state"].sessions.active_providers()
    except (KeyError, AttributeError):
        return None
    for provider in providers:
        getter = getattr(provider, "available_models", None)
        if not callable(getter):
            continue
        try:
            ids = advertised_model_ids(getter())
        except Exception:
            continue
        if ids:
            return ids
    return None


def _validate_role_model(
    value: str, request: web.Request, provider: str | None = None
) -> str | None:
    """Reject a per-role model pin the account cannot use; ``None`` = allow.

    ``""`` / ``"auto"`` always allow (they defer to the chat default). Otherwise
    reuse the per-session provider guard (rejects display-only canonical keys for
    the active provider), then — when a live advertised set is known — apply the
    SAME entitlement predicate the session-init withhold uses
    (:func:`model_is_unusable`, #1596) so the picker and the wire cannot disagree.
    No advertised set => accept (entitlement unknowable; don't accuse on no
    evidence), matching that predicate's own conservative default.

    *provider* is forwarded to :func:`_model_rejected_reason` so a caller holding
    an already-loaded config does not pay a second synchronous config read; the
    remaining work is in-memory. Omit it and the provider is resolved there.
    """
    if not value or value == "auto":
        return None
    from kiro_crew.acp.client import model_is_unusable
    from kiro_crew.dashboard.chat_handlers import _model_rejected_reason

    reason = _model_rejected_reason(value, provider=provider)
    if reason:
        return reason
    advertised = _active_advertised_ids(request)
    if advertised is None:
        return None
    if model_is_unusable(value, advertised):
        usable = ", ".join(advertised[:8]) or "auto"
        return f"{value!r} is not available on your account; choose one of: {usable}, or 'auto'."
    return None


# Keys a caller may reasonably try to PATCH that have a dedicated endpoint whose
# side effects the generic config write cannot reproduce. Naming the endpoint turns
# a dead end ("field not editable") into a next step.
_MOVED_CONFIG_FIELDS: dict[str, str] = {
    "agent.apps_allow_third_party": (
        "agent.apps_allow_third_party is not editable here because turning it off "
        "must also stop the third-party app code it was admitting. Use "
        "PUT /api/security/trusted-apps/allow-all, which runs that teardown and "
        "reports anything it could not stop."
    ),
}


def _selectable_acp_backends() -> list[str]:
    """The ``agent.acp_backend`` values this build can actually be switched to.

    A thin alias for ``acp_backends.selectable_backend_values`` so the allowlist
    entry below reads in this module's vocabulary; the answer itself comes from the
    one code owner, which the config load path and the schema endpoint also use —
    three independent derivations is how the old literal list drifted.

    Deployment POLICY needs no second derivation here: the ``agent_backend``
    governance scope is applied once at boot by narrowing the registry itself
    (``agent_backend_governance.narrow_selectable_backends``), so a policy-denied
    harness is already absent from the answer this returns.
    """
    return selectable_backend_values()


_EDITABLE_CONFIG: dict[str, dict] = {
    "agent.provider": {"type": "enum", "values": ["acp"]},
    # Which ACP agent drives a session: "" = kiro-cli, "kas" = kiro-agent.
    # ``values_fn`` rather than a literal, because the set WIDENS after this module
    # is imported: an edition registers a backend from
    # ``ProviderRegistry.register_acp_backends`` at boot, and the old literal left
    # it rejected here with a misleading "invalid value". Resolved per request
    # against the one code owner, so this can no longer drift from what
    # ``AcpProvider`` will actually serve — which is what the parity test used to
    # stand in for.
    "agent.acp_backend": {"type": "enum", "values_fn": _selectable_acp_backends},
    # Default model for new sessions. Membership can NOT be validated against a
    # fixed list: the real vocabulary is whatever the live kiro-cli advertises
    # (/api/models spawns it to find out), and it spans both canonical registry
    # keys ("opus-4.8-1m") and kiro's own ids ("claude-opus-4.8"). So this is a
    # grammar check instead — model-id charset only, no separators or shell
    # metacharacters — and an unknown-but-well-formed id is rejected downstream
    # by kiro itself rather than silently accepted here. "auto"/"" = defer to
    # the agent config / kiro's own default.
    "agent.model": {"type": "str", "max_len": 64, "pattern": r"^[A-Za-z0-9._\-\[\]]*$"},
    # Per-task-class model overrides. Same grammar as agent.model (the real
    # vocabulary is whatever the backend advertises). "" / "auto" defers to the
    # chat default. `validate_fn` additionally rejects a well-formed id the
    # active provider or the account's entitlement cannot honor.
    "agent.role_models.background": {
        "type": "str",
        "max_len": 64,
        "pattern": r"^[A-Za-z0-9._\-\[\]]*$",
        "validate_fn": _validate_role_model,
    },
    "agent.role_models.subagent": {
        "type": "str",
        "max_len": 64,
        "pattern": r"^[A-Za-z0-9._\-\[\]]*$",
        "validate_fn": _validate_role_model,
    },
    # Throttle-exhaustion fallback model. Single value: "auto" (default) defers
    # to the backend's availability-aware routing; a concrete id is tried first
    # with "auto" as the final fallthrough; "" disables the feature. Same
    # grammar + entitlement validation as the role-model pins ("" / "auto"
    # always allow), so the dropdown and the wire cannot disagree.
    "agent.fallback_model": {
        "type": "str",
        "max_len": 64,
        "pattern": r"^[A-Za-z0-9._\-\[\]]*$",
        "validate_fn": _validate_role_model,
    },
    "agent.reasoning_effort": {"type": "enum", "values": ["", *EFFORT_LEVELS]},
    # Per-role reasoning effort, paired with role_models. Same enum as the chat
    # default; "" = inherit. Applies only on reasoning-capable models.
    "agent.role_efforts.background": {"type": "enum", "values": ["", *EFFORT_LEVELS]},
    "agent.role_efforts.subagent": {"type": "enum", "values": ["", *EFFORT_LEVELS]},
    "agent.approval_mode": {"type": "enum", "values": ["auto", "interactive"]},
    # How long an AD-HOC auto-approve grant lasts. Editable from Settings because
    # every value here still ends: the timed ones are capped at the SafetyOverride
    # 24h ceiling and "until_shutdown" dies with the process. The never-expiring
    # DECLARED grant (agent.dangerously_skip_permissions) is deliberately NOT
    # here — it stays config-file-only so it cannot be switched on from the UI.
    "agent.yolo_duration": {
        "type": "enum",
        "values": ["30m", "1h", "6h", "12h", "24h", "until_shutdown"],
    },
    "agent.sandbox": {"type": "enum", "values": ["auto", "off"]},
    "agent.sandbox_allow_no_isolation": {"type": "bool"},
    "agent.completion_keep": {"type": "enum", "values": ["head", "tail", "both"]},
    "agent.completion_keep_chars": {
        "type": "int",
        "min": COMPLETION_KEEP_CHARS_MIN,
        "max": RESULT_FILE_MAX_BYTES,
    },
    "agent.soft_stop_budget_secs": {
        "type": "float",
        "min": SOFT_STOP_BUDGET_MIN,
        "max": SOFT_STOP_BUDGET_MAX,
    },
    "session.timeout_secs": {"type": "int", "min": SESSION_TIMEOUT_MIN, "max": SESSION_TIMEOUT_MAX},
    # Range shared with the load-time clamp in config/loader.py — one constant
    # pair, so the write gate and the load path cannot drift (issue #4734).
    "session.autocompact_pct": {
        "type": "float",
        "min": AUTOCOMPACT_PCT_MIN,
        "max": AUTOCOMPACT_PCT_MAX,
    },
    "session.pool_size": {"type": "int", "min": 0, "max": 10},
    "session.pool_agent": {"type": "str", "values_fn": _agent_values},
    "session.pool_ttl_secs": {"type": "int", "min": POOL_TTL_SECS_MIN, "max": POOL_TTL_SECS_MAX},
    # Intent-level session summaries in the chat right panel. Only the boolean
    # enable is editable here: it spends tokens on turns the user did not ask to
    # pay for, so it is off by default and the Settings toggle is the single
    # opt-in. The cadence/cap fields (min_user_turns, max_intents, …) stay
    # config-file-only — they are power-user knobs, not first-run choices.
    "session_summary.enabled": {"type": "bool"},
    "auto_update": {"type": "bool"},
    "dashboard.mcp_probe_timeout_secs": {
        "type": "int",
        "min": MCP_PROBE_TIMEOUT_MIN,
        "max": MCP_PROBE_TIMEOUT_MAX,
    },
    "dashboard.recent_tint_count": {
        "type": "int",
        "min": RECENT_TINT_COUNT_MIN,
        "max": RECENT_TINT_COUNT_MAX,
    },
    # Per-version snooze/skip verdict for the proactive update popup, written
    # as ONE atomic record: the three fields only mean anything together, so
    # per-field writes would open both a crash window (old verdict paired
    # with a new version) and a two-client interleave that reassembles a
    # verdict nobody expressed. Persisted in gateway config (not browser
    # storage) so the decision holds across browsers and the desktop app's
    # embedded dashboard.
    "dashboard.update_nudge": {
        "type": "dict",
        "keys": {
            "version": {"type": "str", "max_len": 128},
            "snoozed_until": {"type": "float", "min": 0.0, "max": 4102444800.0},
            "skipped": {"type": "bool"},
        },
    },
    # Default shell for the built-in terminal panel (Settings → Display →
    # Terminal). "" = unset, use $SHELL / the platform default. The executable
    # check lives as an off-loop special case in the PATCH handler (a PATH
    # scan must not run inline on the event loop, and validate_fn is called
    # synchronously); the spawn path re-validates at open time and falls back
    # rather than failing, so a stale value can never cost the user their
    # terminal — the save-time check exists to surface a typo immediately in
    # the Settings field.
    "dashboard.terminal.shell": {"type": "str", "max_len": 512},
    # Keep the host awake while the agent is running a task. Gateway-host
    # behavior (not a display pref), read by the prevent-sleep poll in
    # dashboard/server.py; off by default.
    "dashboard.prevent_sleep": {"type": "bool"},
    # User profile (onboarding step 2 + Settings > General > About You).
    # Structured slugs, not free text: context.py maps them to prompt-ready
    # descriptions in its [USER PROFILE] block. "" = unspecified/cleared.
    "dashboard.user_role": {
        "type": "enum",
        "values": ["", "developer", "designer", "product-manager", "data-ml", "it-ops", "other"],
    },
    # The one free-text escape hatch: what the user typed after picking "other".
    # Bounded hard (60 chars) and stripped of prompt-structural characters by
    # context.py before it is quoted into [USER PROFILE] — it is the only value
    # in that block the user authors rather than picks.
    "dashboard.user_role_other": {"type": "str", "max_len": 60},
    "dashboard.user_technical_level": {
        "type": "enum",
        "values": ["", "codes", "somewhat-technical", "non-technical"],
    },
    # Anonymous usage beacon — the in-product opt-out (Settings → Privacy
    # toggle), the GUI twin of `kirocrew telemetry disable`. Only the boolean
    # enable is editable here: beacon_endpoint stays CLI/config-file-only so a
    # dashboard caller cannot redirect the heartbeat to an arbitrary host.
    # Nothing about this key is sensitive to read back, so the masked GET
    # already surfaces it for the toggle's initial state.
    "telemetry.beacon_enabled": {"type": "bool"},
    # Tailnet-derived dashboard origin (RFC §4). Only the boolean enable is
    # editable: there is no companion key here for a hand-written tailnet name,
    # because the name is *derived from the local daemon and validated against the
    # tailnet's own MagicDNS suffix* — accepting one from an API caller would hand
    # the CSRF origin allowlist an attacker-chosen value, which is the whole thing
    # ``tailnet._valid_magicdns_name`` exists to prevent. Enabling takes effect on
    # the next gateway start (the origin set is built once during startup), and an
    # enterprise ceiling can refuse the enabling write outright — see the
    # ``capabilities.tailnet_origin`` gate below.
    "dashboard.tailscale.enabled": {"type": "bool"},
    # Identity trust for tailnet peers (RFC §2–§3.1). Only the boolean opt-in
    # and the pin scope are editable here; ``allowed_logins`` is a list and is
    # deliberately config-file-only — the write surface below has no list type,
    # and the allowlist is the control that decides who gets in, so it should
    # be an explicit file edit rather than an API-reachable value. Loader-side
    # validation keeps every bad combination narrowing-only (trust with an
    # empty allowlist stays off; an unrecognised pin_scope falls back to node).
    "dashboard.tailscale.trust_identity": {"type": "bool"},
    "dashboard.tailscale.pin_scope": {"type": "str", "max_len": 8},
    # Local OTEL metric collection — the Privacy panel's recording switch. Safe
    # to expose where beacon_endpoint is not: turning this on writes JSONL under
    # ~/.kiro/crew/metrics. It is NOT unconditionally local, though —
    # `_build_recorder` attaches an OTLP reader for every destination the active
    # telemetry provider supplies (the default provider supplies one when
    # `telemetry.otlp_endpoint` is set) — so the gate below refuses the ENABLE on a
    # host where egress would start, which is what keeps the switch's local-only
    # promise true for every state it can reach. The endpoint itself stays
    # config-file-only, so a
    # dashboard caller can neither choose a destination nor start sending to one.
    "telemetry.enabled": {"type": "bool"},
    # SSO login flags for an edition that supplies a real sso_login_handler.
    # Bounded to a short string here; the companion login handler re-validates
    # each token against its own flag allowlist before spawning the login PTY
    # (defense in depth — this gate only stores the value). Inert in public build.
    "dashboard.sso_login_flags": {"type": "str", "max_len": 256},
    # Instances (multi-instance management). Toggling enabled needs a gateway
    # restart to take effect (the SSH manager + CSP relaxation init at startup),
    # so the Instances settings panel surfaces a "restart required" hint.
    "instances.enabled": {"type": "bool"},
    # Skills: opt in to automatic skill generation (Settings → Skills). Both
    # default OFF/ON respectively in SkillsConfig; generated candidates still
    # require approval unless approval_required is turned off (scripts always
    # require approval regardless — enforced in the generation path).
    "skills.auto_create_from_sessions": {"type": "bool"},
    "skills.approval_required": {"type": "bool"},
    # Knowledge Library ingestion. Chunk budget max mirrors the point past which
    # a single sweep stops being a trickle; dedup cadence max is ~a day of sweeps.
    "knowledge.auto_add_documents": {"type": "bool"},
    "knowledge.auto_ingest_artifacts": {"type": "bool"},
    "knowledge.folder_ingest_chunk_budget": {
        "type": "int",
        "min": 0,
        "max": FOLDER_INGEST_CHUNK_BUDGET_MAX,
    },
    "knowledge.dedup_every_n_sweeps": {"type": "int", "min": 0, "max": DEDUP_EVERY_N_SWEEPS_MAX},
    "knowledge.sweep_chunk_budget": {"type": "int", "min": 0, "max": SWEEP_CHUNK_BUDGET_MAX},
    "knowledge.embed_rate_limit": {"type": "int", "min": 0, "max": EMBED_RATE_LIMIT_MAX},
    "knowledge.extraction_model": {"type": "str"},
    "knowledge.extraction_effort": {"type": "enum", "values": ["", *EFFORT_LEVELS]},
    "knowledge.fetch_effort": {"type": "enum", "values": ["", *EFFORT_LEVELS]},
    "knowledge.extraction_pool_size": {
        "type": "int",
        "min": EXTRACTION_POOL_SIZE_MIN,
        "max": EXTRACTION_POOL_SIZE_MAX,
    },
    # Computer use — BUDGET KNOBS ONLY. There is deliberately no
    # "computer_use.enabled" key here: the primary enable lives on the keystone
    # ``computer_use.json`` (see config.loader.computer_use_state_path) so the
    # agent cannot reach it, and this generic PATCH route writes config.json.
    # Adding an enable key here would reintroduce exactly the hole the keystone
    # exists to close. The ComputerUsePanel drives these through
    # PUT /api/computer-use/config; they are also exposed here so the command
    # palette's generic config path can reach them. Bounds mirror
    # computer_use.types' *_LIMIT ceilings, which the loader re-clamps at load.
    "computer_use.max_tree_nodes": {
        "type": "int",
        "min": 1,
        "max": _CU_MAX_TREE_NODES_LIMIT,
    },
    "computer_use.screenshot_max_px": {
        "type": "int",
        "min": _CU_MIN_SCREENSHOT_MAX_PX,
        "max": _CU_MAX_SCREENSHOT_MAX_PX,
    },
}


def _beacon_governance_pinned_off() -> bool:
    """Return whether a ceiling pins ``capabilities.telemetry`` off (blocking).

    Delegates to ``beacon.is_governance_pinned_off`` rather than re-resolving, so
    the PATCH gate and the send gate can never disagree about whether a host is
    pinned — two independent resolutions would be two things to keep in sync.

    Runs in a worker thread (see the call site): the resolution reads the
    trust-root policy file and the active profile from disk.

    ``audit_tool``: this is an ENFORCEMENT decision (it refuses the write with a
    403), so it routes through the audited seam and lands a
    ``governance_decision`` SEL record — matching the send gate and both CLI
    refusals. The name is distinct per call site so the trail says which control
    refused. The dashboard route additionally logs its own ``config.patch`` denial
    via ``_log_sel``; that records the API call, while this records the governance
    decision behind it.
    """
    return beacon.is_governance_pinned_off(audit_tool="config_patch_dashboard")


def _tailnet_governance_pinned_off() -> bool:
    """Return whether a ceiling pins ``capabilities.tailnet_origin`` off (blocking).

    The tailnet twin of :func:`_beacon_governance_pinned_off`, and delegating for
    the same reason: ``tailnet.is_governance_pinned_off`` is the one resolution, so
    the PATCH gate, the startup derivation gate and the CLI gate cannot disagree
    about whether a host is pinned.

    Runs in a worker thread (see the call site): the resolution reads the
    trust-root policy file and the active profile from disk.

    ``audit_tool``: this is an ENFORCEMENT decision (it refuses the write with a
    403), so it routes through the audited seam and lands a
    ``governance_decision`` SEL record. The name is distinct per call site so the
    trail says which control refused; the route additionally logs its own
    ``config.patch`` denial via ``_log_sel``, which records the API call while
    this records the governance decision behind it.
    """
    from kiro_crew.dashboard import tailnet  # noqa: F811 - local: keeps the import edge lazy

    return tailnet.is_governance_pinned_off(audit_tool="config_patch_dashboard_tailnet")


async def api_kirocrew_config_patch(request: web.Request) -> web.Response:
    """PATCH /api/config/kirocrew — update a single config field."""
    from kiro_crew.config.loader import ConfigReadError, config_path, update_config_locked

    caller = request.get("user")
    if not caller:
        logger.warning(
            "config.patch called without authenticated user; falling back to 'dashboard'"
        )
        caller = "dashboard"

    def _log_sel(outcome: str, resources: str) -> None:
        _sel().log_api_access(
            caller=caller,
            operation="config.patch",
            outcome=outcome,
            source="dashboard",
            resources=resources,
        )

    def _deny(msg: str, resources: str = "", status: int = 400) -> web.Response:
        _log_sel("denied", resources or msg)
        return web.json_response({"error": msg}, status=status)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON", "invalid JSON body")

    path_key = body.get("path", "")
    value = body.get("value")
    spec = _EDITABLE_CONFIG.get(path_key)
    if not spec:
        # `agent.apps_allow_third_party` was deliberately REMOVED from the editable
        # set. It is not an ordinary preference: turning it off has to stop the code
        # it was admitting, which means a teardown sweep (shutdown hooks, backend
        # processes, cron deregistration) that this generic read-modify-write knows
        # nothing about. A plain PATCH here would flip the flag and leave every app
        # it admitted still executing — trust withdrawn on paper only. The dedicated
        # endpoint owns that sequencing, so point the caller at it instead of
        # silently accepting a write that cannot honour the setting's meaning.
        if path_key in _MOVED_CONFIG_FIELDS:
            return _deny(_MOVED_CONFIG_FIELDS[path_key], f"{path_key}={value}")
        return _deny(f"field not editable: {path_key}", f"{path_key}={value}")

    # Validate value
    if spec["type"] == "enum":
        # ``values_fn`` (the same hook the ``str`` branch already carries) is for an
        # enum whose membership is not knowable at import: it can widen after boot
        # when an edition registers a backend. A static ``values`` list would be
        # read before that happened.
        allowed = list(spec["values_fn"]()) if "values_fn" in spec else spec["values"]
        if value not in allowed:
            return _deny(f"invalid value, must be one of {allowed}", f"{path_key}={value}")
    elif spec["type"] == "int":
        try:
            value = int(value)
        except (TypeError, ValueError):
            return _deny("must be an integer", f"{path_key}={value}")
        lo, hi = spec.get("min", 0), spec.get("max", 999999)
        if value < lo or value > hi:
            return _deny(f"must be between {lo} and {hi}", f"{path_key}={value}")
    elif spec["type"] == "bool":
        if not isinstance(value, bool):
            return _deny("must be a boolean", f"{path_key}={value}")
    elif spec["type"] == "float":
        try:
            value = float(value)
        except (TypeError, ValueError):
            return _deny("must be a number", f"{path_key}={value}")
        if not math.isfinite(value):
            return _deny("must be a finite number", f"{path_key}={value}")
        lo, hi = spec.get("min", 0.0), spec.get("max", 999999.0)
        if value < lo or value > hi:
            return _deny(f"must be between {lo} and {hi}", f"{path_key}={value}")
    elif spec["type"] == "str":
        if not isinstance(value, str):
            return _deny("must be a string", f"{path_key}={value}")
        max_len = spec.get("max_len", 256)
        if len(value) > max_len:
            return _deny(f"must be at most {max_len} characters", f"{path_key}={value}")
        if "values" in spec and value not in spec["values"]:
            return _deny(f"invalid value, must be one of {spec['values']}", f"{path_key}={value}")
        pattern = spec.get("pattern")
        if pattern and not re.fullmatch(pattern, value):
            return _deny(f"invalid value for {path_key}", f"{path_key}={value}")
        values_fn = spec.get("values_fn")
        if values_fn and value not in values_fn():
            return _deny(f"invalid value for {path_key}", f"{path_key}={value}")
        validate_fn = spec.get("validate_fn")
        if validate_fn:
            reason = validate_fn(value, request)
            if reason:
                return _deny(reason, f"{path_key}={value}")
    elif spec["type"] == "dict":
        # One-level record written ATOMICALLY as a single value, for settings
        # where multiple scalar fields form one verdict and a partial write is
        # itself the bug (e.g. the update popup's version+snooze+skip record).
        # Strict by design: every declared key present, no undeclared keys,
        # each value validated against its scalar subspec — so this cannot
        # become a generic JSON passthrough.
        if not isinstance(value, dict):
            return _deny("must be an object", f"{path_key}={value}")
        keys_spec = spec["keys"]
        unknown = set(value) - set(keys_spec)
        if unknown:
            return _deny(f"unknown key(s): {sorted(unknown)}", f"{path_key}={value}")
        missing = set(keys_spec) - set(value)
        if missing:
            return _deny(f"missing key(s): {sorted(missing)}", f"{path_key}={value}")
        validated: dict = {}
        for sub_key, sub_spec in keys_spec.items():
            sub_val = value[sub_key]
            if sub_spec["type"] == "str":
                if not isinstance(sub_val, str):
                    return _deny(f"{sub_key} must be a string", f"{path_key}={value}")
                if len(sub_val) > sub_spec.get("max_len", 256):
                    return _deny(
                        f"{sub_key} must be at most {sub_spec.get('max_len', 256)} characters",
                        f"{path_key}={value}",
                    )
            elif sub_spec["type"] == "bool":
                if not isinstance(sub_val, bool):
                    return _deny(f"{sub_key} must be a boolean", f"{path_key}={value}")
            elif sub_spec["type"] == "float":
                # bool is an int subclass; refuse it before coercion so
                # `true` cannot silently store 1.0.
                if isinstance(sub_val, bool):
                    return _deny(f"{sub_key} must be a number", f"{path_key}={value}")
                try:
                    sub_val = float(sub_val)
                except (TypeError, ValueError):
                    return _deny(f"{sub_key} must be a number", f"{path_key}={value}")
                if not math.isfinite(sub_val):
                    return _deny(f"{sub_key} must be a finite number", f"{path_key}={value}")
                lo, hi = sub_spec.get("min", 0.0), sub_spec.get("max", 999999.0)
                if sub_val < lo or sub_val > hi:
                    return _deny(f"{sub_key} must be between {lo} and {hi}", f"{path_key}={value}")
            else:
                return _deny("unsupported config type", f"{path_key}={value}", 500)
            validated[sub_key] = sub_val
        value = validated
    else:
        return _deny("unsupported config type", f"{path_key}={value}", 500)

    # The terminal's default shell must name a program that exists — "" clears
    # the setting (restores the $SHELL / platform default). shutil.which stats
    # every PATH entry, so the probe runs off-loop (same rationale as the
    # governance reads below); the spawn path re-validates at open time and
    # falls back regardless, so this gate is a UX surface, not the safety
    # boundary — it exists to refuse a typo visibly at save time instead of
    # letting it be discovered as a silently different shell on the next
    # terminal open. The body carries a machine-readable `code` (the AGENTS
    # contract for new non-2xx JSON): the Settings field maps it to a catalog
    # key, since rendering this English sentence verbatim would ship an
    # untranslated string into a 12-language dashboard.
    if path_key == "dashboard.terminal.shell" and value.strip():
        resolved = await asyncio.to_thread(shutil.which, value.strip())
        if not resolved:
            _log_sel("denied", f"{path_key}={value}")
            return web.json_response(
                {
                    "error": (
                        "must be an executable shell (an absolute path or a "
                        "command on PATH); leave empty to use the system default"
                    ),
                    "code": "shell_not_executable",
                },
                status=400,
            )

    # ── Governance: refuse a write an enterprise ceiling has pinned ──
    # Only re-ENABLING is refused. Writing `false` is always allowed even under a
    # ceiling that already forbids the beacon: the ceiling is a floor on privacy,
    # so a narrower local choice composes with it (tightest-wins), and refusing it
    # would leave a user unable to record the stricter preference they already have
    # in effect — which would also strand them if the policy were later lifted.
    #
    # The 403 exists so a pinned host cannot be left storing `true` behind a toggle
    # that does nothing: `should_send` already blocks the egress, so without this
    # the config file and the UI would both claim "on" while nothing is ever sent.
    if path_key == "telemetry.beacon_enabled" and value is True:
        # to_thread: resolving the ceiling reads the trust-root policy file and
        # the active profile from disk, which must not block the event loop.
        pinned = await asyncio.to_thread(_beacon_governance_pinned_off)
        if pinned:
            return _deny(
                "telemetry is disabled by your administrator's security policy",
                f"{path_key}={value}",
                403,
            )

    # Local metric collection is offered as local-only ("Nothing is exported"), and
    # that promise has to hold for every state this route can reach. It would not:
    # `_build_recorder` attaches an OTLP reader for every destination the active
    # telemetry provider supplies (see metrics/provider.py), so on a host where
    # egress is configured — through `telemetry.otlp_endpoint` for the default
    # provider, or an edition's own collector — enabling collection from the
    # dashboard would start network egress
    # under a switch that says it does not. Refuse the ENABLE there and let the
    # config file — which is where the endpoint was chosen — be where that decision
    # is made. Disabling stays writable for the same reason as the beacon above: a
    # narrower local choice always composes.
    if path_key == "telemetry.enabled" and value is True:
        try:
            # to_thread: a config load is a fingerprint-cache hit in the steady
            # state, but a full read plus schema validation (~14ms) when the file
            # changed — and this handler runs on the event loop.
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
            # Resolved posture, not the raw endpoint string: the DEFAULT provider
            # derives its one destination from telemetry.otlp_endpoint, but an
            # edition may supply its own collector with that key empty. Asking the
            # same resolver _build_recorder uses is what keeps this refusal and
            # the actual egress from disagreeing. It RAISES when posture cannot be
            # established, which the handler below turns into a refusal: reading a
            # transient provider error as "no egress" would permit an enable that
            # the recovered provider then turns into egress.
            egress = await asyncio.to_thread(_metrics_provider.otlp_egress_active, cfg.telemetry)
        except Exception:
            # Unreadable config, or egress posture that could not be resolved:
            # fail closed rather than enabling collection whose egress posture
            # cannot be established.
            logger.warning(
                "telemetry config or egress posture unreadable; refusing to enable",
                exc_info=True,
            )
            return _deny(
                "could not establish the telemetry egress posture",
                f"{path_key}={value}",
                409,
            )
        if egress:
            return _deny(
                "this host is configured to export metrics off the machine, so "
                "enabling collection here would also start that export. Enable it "
                "in the config file instead, where the destination is configured.",
                f"{path_key}={value}",
                409,
            )

    # Same rule, same direction, for the tailnet origin derivation. `false` stays
    # writable under a ceiling that already forbids it, for the same reason as
    # above: the ceiling is a floor, a narrower local choice composes with it, and
    # refusing the write would strand the user if the policy were later lifted.
    # The 403 exists so a pinned host cannot store `true` behind a control that
    # does nothing — `resolve_tailnet_host` already refuses to derive, so without
    # this the config file and the card would both claim "on" while no origin is
    # ever added.
    if (
        path_key in ("dashboard.tailscale.enabled", "dashboard.tailscale.trust_identity")
        and value is True
    ):
        pinned = await asyncio.to_thread(_tailnet_governance_pinned_off)
        if pinned:
            return _deny(
                "tailnet access is disabled by your administrator's security policy",
                f"{path_key}={value}",
                403,
            )

    # Read, update, write — serialized across processes via update_config_locked.
    cfg_path = config_path()
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        parts = path_key.split(".")

        def _mutate_config_patch(data: dict) -> dict | None:
            """Apply a single dotted-key assignment to the raw config dict."""
            # Walk (creating) intermediate objects, then set the leaf. Handles
            # arbitrary depth uniformly — 1-level ("auto_update"), 2-level
            # ("agent.model"), and 3-level ("agent.role_models.background") —
            # instead of special-cases that would clobber a whole section for a
            # 3-level key.
            section = data
            for part in parts[:-1]:
                nxt = section.setdefault(part, {})
                if not isinstance(nxt, dict):
                    raise ValueError(f"config section '{part}' is not an object")
                section = nxt
            section[parts[-1]] = value
            return data

        try:
            await asyncio.to_thread(update_config_locked, cfg_path, mutate=_mutate_config_patch)
        except ConfigReadError:
            _log_sel("error", f"{path_key}=read_failed")
            return web.json_response({"error": "failed to read config file"}, status=500)
        except ValueError as exc:
            _log_sel("error", f"{path_key}=section_not_dict")
            return web.json_response({"error": str(exc)}, status=500)
        except OSError:
            _log_sel("error", f"{path_key}=write_failed")
            return web.json_response({"error": "failed to write config file"}, status=500)

    _log_sel("success", f"{path_key}={value}")

    cfg = KiroCrewConfig.load()

    # If provider changed, reload the factory so new sessions use the new provider
    if path_key == "agent.provider":
        state: DashboardState = request.app["state"]
        # Refresh agent artifacts so the target provider is immediately usable.
        # For claude_code this (re)writes ~/.claude/agents/kirocrew.mcp.json —
        # the MCP registry the claude-agent-acp backend reads at session/new —
        # picking up any servers installed while on kiro. Best-effort: a failure
        # here must not block the provider switch (gateway boot also rebuilds).
        try:
            from kiro_crew.agent import rebuild_agent_config  # noqa: F811  circular import

            await asyncio.to_thread(rebuild_agent_config)
        except Exception:
            logger.warning("Agent config rebuild after provider switch failed", exc_info=True)
        await state.sessions.reload_provider_factory()
        # Clear model on all slots — aliases are provider-specific
        for slot in state._slots.values():
            if slot.model:
                slot.model = ""
                # Deliberate model change: bump the pick generation so the
                # fallback restore probe drops any sticky state instead of
                # restoring a model id from the previous provider.
                slot._model_pick_gen += 1
        state.push_slots_update()
        logger.info(
            "Provider switched to %s — config rebuilt, factory reloaded, slot models cleared", value
        )

    # The default model and default reasoning effort are captured when the
    # provider factory is built (at gateway startup), so a config write alone
    # would not reach new sessions until a restart. refresh_defaults() rebuilds
    # the factory and drains the warm pool WITHOUT touching live sessions —
    # reload_provider_factory() must NOT be used here: it clears _sessions and
    # shuts every provider down, which is correct for a provider switch but
    # would kill in-flight turns just because a default changed.
    if path_key in (
        "agent.model",
        "agent.reasoning_effort",
        # The ACP backend is captured when the provider factory is built, and a
        # pre-warmed kiro-cli process must not serve a session that asked for
        # KAS — refresh_defaults() rebuilds the factory and drains the pool.
        # NOT reload_provider_factory(): switching the default backend must not
        # kill in-flight turns on live sessions, which keep the backend they
        # were started on.
        "agent.acp_backend",
    ) or path_key.startswith("agent.role_efforts."):
        state = request.app["state"]
        await state.sessions.refresh_defaults()
        logger.info("%s set to %r — session defaults refreshed", path_key, value)

    # The background role model is baked into the lite / heartbeat kiro specs at
    # agent-build time, so a change must rewrite them to take effect without a
    # restart. The subagent role is read live at spawn (_subagent_default_model),
    # so it needs no rebuild. Chat-default inheritance for both roles is picked
    # up by the refresh_defaults above when agent.model changes.
    if path_key == "agent.role_models.background":
        try:
            from kiro_crew.agent import rebuild_agent_config

            await asyncio.to_thread(rebuild_agent_config)
            logger.info(
                "agent.role_models.background set to %r — background agent specs rebuilt", value
            )
        except Exception:
            logger.warning("background-model rebuild failed", exc_info=True)

    # If completion-keep mode or budget changed, propagate to the live
    # SubagentManager so the next subagent to complete uses the new value.
    # Without this the manager keeps the values it cached at gateway
    # startup and the Settings UI change would only take effect after a
    # gateway restart.
    if path_key in ("agent.completion_keep", "agent.completion_keep_chars"):
        state = request.app["state"]
        if state.subagents is not None:
            state.subagents.update_completion_keep(
                cfg.agent.completion_keep,
                cfg.agent.completion_keep_chars,
            )
            logger.info(
                "completion_keep hot-reloaded: mode=%s chars=%d",
                cfg.agent.completion_keep,
                cfg.agent.completion_keep_chars,
            )

    # The metrics recorder is built once per process and memoized, so a config
    # write alone would leave the Telemetry panel reporting "on" while every
    # metric call site stayed a no-op. Dropping the cached recorder makes the next
    # get_recorder() rebuild from the value just written — collection starts (or
    # stops, flushing what it had) without a restart. This reaches the gateway
    # process, which is where the session/turn/HTTP metrics are recorded; other
    # kirocrew processes pick the value up when they next start.
    if path_key == "telemetry.enabled":
        try:
            # to_thread: shutdown() flushes the exporter and joins the reader
            # thread, both of which block.
            await asyncio.to_thread(_metrics_provider.shutdown)
            logger.info("telemetry.enabled set to %r — metrics recorder rebuilt", value)
        except Exception:
            logger.warning("metrics recorder reset after telemetry toggle failed", exc_info=True)

    return web.json_response(_masked_config_dict(cfg))


# ── Local token bootstrap (Electron / local apps) ─────────────────────


async def api_token_local(request: web.Request) -> web.Response:
    """GET /api/token/local — issue a token for local apps.

    Requires a per-session secret written to ~/.kiro/crew/.local_secret at
    gateway startup. Only processes on the same machine can read the file.
    Secret passed via ``X-Local-Secret`` header (not query string, to avoid
    leaking in logs).
    """
    import kiro_crew.dashboard.handlers as _h  # noqa: F811

    if not _h.is_loopback(request.remote or ""):
        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation="token.local",
            outcome="denied",
            source="local-bootstrap",
            resources="non-loopback",
        )
        return web.json_response({"error": "loopback only"}, status=403)

    expected = request.app.get("local_secret", "")
    if not expected:
        return web.json_response({"error": "not available"}, status=503)
    provided = request.headers.get("X-Local-Secret", "")
    if not provided or not hmac.compare_digest(expected, provided):
        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation="token.local",
            outcome="denied",
            source="local-bootstrap",
            resources="invalid-secret",
        )
        return web.json_response({"error": "invalid secret"}, status=403)
    ttl = MAX_SESSION_TTL_SECS
    ttl_param = request.query.get("ttl", "")
    if ttl_param:
        parsed = parse_duration(ttl_param)
        if parsed:
            ttl = parsed
    state = request.app.get("state")
    owner_id = str(getattr(state, "owner_id", "") or "")
    # Optional multi-instance embed claim: the parent (embedding) dashboard's
    # port, so the embedded remote can authorize exactly that loopback parent
    # origin in CSP frame-ancestors (see server._extra_frame_ancestors). Minted
    # only via this local-secret-gated endpoint; validated as a loopback port.
    extra: dict[str, str] = {}
    epp = request.query.get("embed_parent_port", "")
    if epp.isdigit() and 1 <= int(epp) <= 65535:
        extra["embed_parent_port"] = str(int(epp))
    token = generate_token(owner_id or "local-app", ttl_seconds=ttl, extra=extra or None)
    _sel().log_api_access(
        caller=request.remote or "unknown",
        operation="token.local",
        outcome="success",
        source="local-bootstrap",
        resources="token-issued",
    )
    return web.json_response({"token": token, "expires_in": ttl})


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
    the 500 back). Shape follows ``cron.py``'s ``_invalid_path_id_response`` --
    400 with an ``invalid_<name>`` ``code`` -- which is the contract #6301 names
    and which AGENTS.md's code-field rule requires.

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

    last_pos = 0
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # noqa: F811

    for _ in range(1200):  # 20 min max
        try:
            if path.exists():
                content = path.read_text(encoding="utf-8")
                if len(content) > last_pos:
                    chunk = content[last_pos:]
                    last_pos = len(content)
                    chunk, _ = redact_exfiltration_urls(chunk)
                    chunk, _ = redact_credentials(chunk)
                    await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
            # Check if the subagent is done.
            state: DashboardState = request.app["state"]
            if state.subagents:
                info = state.subagents.get(agent_id)
                if info and info.done:
                    await resp.write(b"event: done\ndata: {}\n\n")
                    break
        except (ConnectionResetError, ClientConnectionResetError):
            break
        await asyncio.sleep(1)
    return resp


async def api_logout(request: web.Request) -> web.Response:
    """POST /api/logout — revoke all active dashboard sessions.

    Called by ``kirocrew logout`` CLI. Requires loopback + local secret
    (same auth as /api/token/local) to prevent unauthorized revocation.
    """
    import kiro_crew.dashboard.handlers as _h  # noqa: F811
    from kiro_crew.dashboard.token_auth import revoke_all_sessions  # noqa: F811

    if not _h.is_loopback(request.remote or ""):
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
    if not expected or not provided or not hmac.compare_digest(expected, provided):
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
    the Squirrel bundle swap never races a live gateway. Requires loopback +
    the local secret (same auth as ``/api/token/local`` and ``/api/logout``)
    so a web page cannot trigger a shutdown.
    """
    import kiro_crew.dashboard.handlers as _h  # noqa: F811
    from kiro_crew import shutdown_event  # noqa: F811

    if not _h.is_loopback(request.remote or ""):
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
    if not expected or not provided or not hmac.compare_digest(expected, provided):
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
    from kiro_crew.dashboard.token_auth import generate_token, validate_app_secret
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
