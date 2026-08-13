"""Core handlers — page serving, branding, STT, config, SEL, auth, session workspace."""

from __future__ import annotations

import asyncio
import copy
import hmac
import importlib.util
import json
import logging
import math
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

import kiro_crew
from kiro_crew import beacon, platform_compat
from kiro_crew.computer_use.types import MAX_SCREENSHOT_MAX_PX as _CU_MAX_SCREENSHOT_MAX_PX
from kiro_crew.computer_use.types import MAX_TREE_NODES_LIMIT as _CU_MAX_TREE_NODES_LIMIT
from kiro_crew.computer_use.types import MIN_SCREENSHOT_MAX_PX as _CU_MIN_SCREENSHOT_MAX_PX
from kiro_crew.config.loader import (
    _VALID_STT_PROVIDERS,
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    MAX_SUBAGENTS_FIXED_FLOOR,
    SUBAGENT_AUTO_MAX_CEILING,
    SUBAGENT_MAX_TURNS_CEILING,
    KiroCrewConfig,
    config_path,
)
from kiro_crew.context_management import RESULT_FILE_MAX_BYTES
from kiro_crew.dashboard.origin import check_host, is_direct_local_request
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.stt_stream import _STREAMING_PROVIDERS
from kiro_crew.dashboard.token_auth import MAX_SESSION_TTL_SECS, generate_token, parse_duration
from kiro_crew.effort import EFFORT_LEVELS
from kiro_crew.executors import discovery_executor
from kiro_crew.metrics import provider as _metrics_provider
from kiro_crew.security_posture import build_posture_snapshot_async, posture_counts_async
from kiro_crew.transcribe import BREW_PATH_DIRS, ensure_ffmpeg_in_path, find_brew, is_available

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
_DIST_DIR = _STATIC_DIR / "dist"
_DIST_INDEX = _DIST_DIR / "index.html"
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
    " <code>kirocrew service restart</code> (systemd / launchd); otherwise"
    " stop the running <code>kirocrew gateway</code> process and start it"
    " again.</p>"
)


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811 — circular import

    return _pkg.sel()


# ── Page ──


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
    try:
        html = _DIST_INDEX.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = _DASHBOARD_HTML_NOT_FOUND
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
    return web.json_response(
        {
            "bot_name": cfg.dashboard.bot_name or "Kiro Crew",
            "avatar": "/logo.png",
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


_STT_MODEL_SIZES: dict[str, str] = {
    "turbo": "~1.6 GB",
}

# Curated MLX model repos surfaced in the STT picker and accepted on PUT.
# Maps Hugging Face repo -> approximate on-disk download size.
_STT_MLX_MODELS: dict[str, str] = {
    "mlx-community/whisper-large-v3-turbo": "~809 MB",
}

# Curated parakeet-mlx model repos surfaced in the STT picker and accepted on
# PUT. Maps Hugging Face repo -> approximate on-disk download size. Parakeet TDT
# 0.6b v3 is multilingual (25 languages) and streams far faster than Whisper for
# a fraction of the RAM, which is why it is the default.
_STT_PARAKEET_MODELS: dict[str, str] = {
    "mlx-community/parakeet-tdt-0.6b-v3": "~600 MB",
}


def _is_apple_silicon() -> bool:
    """True if running on Apple Silicon hardware.

    ``platform.machine()`` reports ``x86_64`` when the interpreter runs under
    Rosetta 2 — which KiroCrew's bundled Python does — so it cannot be used to
    detect the host CPU. The ``hw.optional.arm64`` sysctl reports the true
    hardware capability regardless of translation.
    """
    if platform.system() != "Darwin":
        return False
    if platform.machine() == "arm64":
        return True
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.optional.arm64"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        return False


def _stt_providers() -> list[str]:
    """STT provider values offered to the UI.

    ``mlx`` (Whisper on Apple's MLX framework) only runs on Apple Silicon, and
    ``apple`` (the on-device SpeechAnalyzer framework) needs macOS 26 or later plus
    a Swift toolchain — both are omitted entirely elsewhere rather than being shown
    as unusable options. ``parakeet`` (NVIDIA Parakeet via parakeet-mlx) is likewise
    Apple-Silicon-only and gated the same way as ``mlx``. This is the single source
    of truth for which providers are advertised (GET) and accepted (PUT).
    """
    providers = list(_VALID_STT_PROVIDERS)
    is_apple_silicon = _is_apple_silicon()
    if not is_apple_silicon and "mlx" in providers:
        providers.remove("mlx")
    # `parakeet` shares the exact same Apple-Silicon gate as `mlx`. Reuse the
    # result computed above rather than calling `_is_apple_silicon()` again —
    # off Apple Silicon (e.g. under Rosetta) that call shells out to `sysctl`
    # synchronously, and `_stt_providers()` runs on the dashboard's event loop
    # (GET/PUT /api/config/stt), so a second call doubles that blocking cost
    # on every request.
    if not is_apple_silicon and "parakeet" in providers:
        providers.remove("parakeet")
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


_stt_install_status: dict[str, str] = {"step": "idle", "detail": "", "error": ""}


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
            stt = data.setdefault("stt", {})
            if "enabled" in body:
                stt["enabled"] = bool(body["enabled"])
            if "provider" in body and body["provider"] in _stt_providers():
                stt["provider"] = body["provider"]
            if "model" in body and body["model"] in _STT_MODEL_SIZES:
                stt["model"] = body["model"]
            if (
                "mlx_model" in body
                and isinstance(body["mlx_model"], str)
                and body["mlx_model"] in _STT_MLX_MODELS
            ):
                stt["mlx_model"] = body["mlx_model"]
            if (
                "parakeet_model" in body
                and isinstance(body["parakeet_model"], str)
                and body["parakeet_model"] in _STT_PARAKEET_MODELS
            ):
                stt["parakeet_model"] = body["parakeet_model"]
            if "transcribe_region" in body and isinstance(body["transcribe_region"], str):
                stt["transcribe_region"] = body["transcribe_region"]
            if "transcribe_profile" in body and isinstance(body["transcribe_profile"], str):
                stt["transcribe_profile"] = body["transcribe_profile"]
            if "language_code" in body and isinstance(body["language_code"], str):
                stt["language_code"] = body["language_code"]
            if "streaming" in body and isinstance(body["streaming"], bool):
                stt["streaming"] = body["streaming"]
            if "endpointing" in body and isinstance(body["endpointing"], bool):
                stt["endpointing"] = body["endpointing"]
            if "dictation_panel" in body and isinstance(body["dictation_panel"], bool):
                stt["dictation_panel"] = body["dictation_panel"]
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(_atomic_json_write, path, data)
        cfg = KiroCrewConfig.load()

    provider = cfg.stt.provider
    available = is_available(cfg.stt)
    # _stt_prereq_commands probes for a system python/brew via subprocess; run it
    # off the event loop so a slow/again-spawned interpreter check can't stall the
    # gateway (observed as "event-loop heartbeat: lag" on Windows where the probe
    # is heavier). The GET is read-only, so threading it is safe. The ffmpeg and
    # install-channel probes ride in the same thread: ensure_ffmpeg_in_path,
    # find_spec and the PEP 668 marker check all touch the filesystem, which
    # does not belong on the loop either.

    def _prereqs_and_probes() -> tuple[list[str], bool, bool, bool]:
        cmds = _stt_prereq_commands(provider)
        ensure_ffmpeg_in_path()
        no_ffmpeg = shutil.which("ffmpeg") is None
        unsupported = not _voice_extra_importable() and not _pip_install_channel_available()
        # The bundled desktop app is the one unsupported cause with different
        # user guidance (no Python environment of the user's own to fix), so
        # the UI needs to distinguish it from the pip-less/PEP 668 causes.
        bundled = platform_compat.is_bundled_interpreter()
        return cmds, no_ffmpeg, unsupported, bundled

    prereqs, ffmpeg_missing, transcribe_unsupported, bundled_app = await asyncio.to_thread(
        _prereqs_and_probes
    )
    return web.json_response(
        {
            "enabled": cfg.stt.enabled,
            "provider": provider,
            "model": cfg.stt.model,
            "mlx_model": cfg.stt.mlx_model,
            "parakeet_model": cfg.stt.parakeet_model,
            "available": available,
            "streaming": cfg.stt.streaming,
            "endpointing": cfg.stt.endpointing,
            "dictation_panel": cfg.stt.dictation_panel,
            "transcribe_region": cfg.stt.transcribe_region,
            "transcribe_profile": cfg.stt.transcribe_profile,
            "language_code": cfg.stt.language_code,
            "models": _STT_MODEL_SIZES,
            "mlx_models": _STT_MLX_MODELS,
            "parakeet_models": _STT_PARAKEET_MODELS,
            "providers": _stt_providers(),
            # Which of those providers can stream partial results. Served from the
            # backend's own `_STREAMING_PROVIDERS` so the Settings UI gates the
            # streaming controls on a CAPABILITY rather than on a hardcoded provider
            # name — the latter silently hid the toggle when `apple` was added.
            "streaming_providers": list(_STREAMING_PROVIDERS),
            "language_codes": list(_STT_LANGUAGE_CODES),
            "install_step": _stt_install_status["step"],
            "install_detail": _stt_install_status["detail"],
            "install_error": _stt_install_status["error"],
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
            # ffmpeg is required to remux the browser's .webm for the
            # non-streaming path, but is_available() only logs a warning when
            # it is absent — so availability can read "ready" while dictation
            # would fail. Served separately so the UI can surface the gap even
            # when the provider is otherwise available.
            "ffmpeg_missing": ffmpeg_missing,
        }
    )


def _voice_extra_importable() -> bool:
    """True when the ``voice`` extra actually imported in this process.

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


def _pip_install_channel_available() -> bool:
    """True when ``<gateway python> -m pip install`` can plausibly succeed.

    Three environments make that command a guaranteed dead end, and surfacing
    it there recreates the press-and-nothing-changes failure this surface
    exists to avoid:

    - the desktop app's bundled interpreter (see
      :func:`platform_compat.is_bundled_interpreter`): pip may exist, but a
      pip install writes into the code-signed bundle — breaking launches and
      updates — and is discarded on every app update;
    - an interpreter without the ``pip`` module (uv tool installs, some
      pipx layouts);
    - a PEP 668 externally-managed interpreter (distro/brew pythons), where
      pip refuses to install. Checked only outside a venv: inside one, pip
      works and deliberately ignores the marker, so a venv returns True.
    """
    if platform_compat.is_bundled_interpreter():
        return False
    if importlib.util.find_spec("pip") is None:
        return False
    # PEP 668 applies to the environment pip would install into. Inside a venv
    # pip deliberately ignores the marker, and `sysconfig.get_path("stdlib")`
    # resolves to the BASE interpreter's directory — where distro/brew pythons
    # place it — so checking it from a venv would misfire on the recommended
    # install layout (venv on a Debian/Ubuntu/Homebrew python).
    if sys.prefix != sys.base_prefix:
        return True
    return not (Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").exists()


def _ffmpeg_install_commands() -> list[str]:
    """Platform command(s) that put ffmpeg on PATH, or ``[]`` when it already is."""
    ensure_ffmpeg_in_path()
    if shutil.which("ffmpeg"):
        return []
    system = platform.system()
    if system == "Darwin":
        return ["brew install ffmpeg"]
    if system == "Windows":
        return ["winget install --id Gyan.FFmpeg"]
    if shutil.which("apt-get"):
        return ["sudo apt-get install -y ffmpeg"]
    # Amazon Linux: no ffmpeg in the distro repos — build minimal ffmpeg from
    # source (the official recommendation).
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    script = os.path.join(proj, "scripts", "build-ffmpeg.sh") if proj else ""
    if script and os.path.isfile(script):
        return [
            "sudo dnf install -y gcc make nasm diffutils 2>/dev/null"
            " || sudo yum install -y gcc make nasm diffutils",
            f"bash {shlex.quote(script)}",
        ]
    return ["echo 'Build ffmpeg from source: https://ffmpeg.org/releases/'"]


def _stt_prereq_commands(provider: str = "whisper") -> list[str]:
    """Return shell commands the user must run manually (need sudo/GUI).

    The ``mlx`` and ``parakeet`` providers have their own lightweight prerequisite
    (``pipx install mlx-whisper`` / ``pipx install parakeet-mlx``) and only need
    ffmpeg beyond that — they do not require the system-python/whisper toolchain.
    """
    if provider == "transcribe":
        # AWS Transcribe's availability is "boto3 + amazon-transcribe importable
        # by THIS gateway process" (see kiro_crew.transcribe.is_available); the
        # optional ``voice`` extra provides both, and there is no separate
        # binary, so the Install button flow does not apply.
        cmds: list[str] = []
        if not _voice_extra_importable():
            if not _pip_install_channel_available():
                # No install channel can make the extra importable in this
                # build/interpreter — the Settings page shows an unsupported
                # notice (`transcribe_unsupported`) instead of a command that
                # cannot succeed.
                return []
            # The command targets the gateway's own interpreter: the import
            # happens in-process, so a system python or ``--user`` install is
            # not importable here.
            if os.name == "nt":
                # POSIX quoting is wrong for Windows shells; ``&`` is
                # PowerShell's call operator for a quoted executable path.
                cmds.append(f'& "{sys.executable}" -m pip install "kirocrew[voice]"')
            else:
                cmds.append(f"{shlex.quote(sys.executable)} -m pip install 'kirocrew[voice]'")
        # The non-streaming path remuxes the browser's .webm through ffmpeg, and
        # is_available() only logs a warning when ffmpeg is absent — this list
        # is the one user-visible surface for that gap.
        cmds.extend(_ffmpeg_install_commands())
        return cmds

    ensure_ffmpeg_in_path()

    system = platform.system()
    cmds = []
    has_ffmpeg = shutil.which("ffmpeg") is not None

    if provider in ("mlx", "parakeet"):
        # mlx/parakeet are only advertised on Apple Silicon (see _stt_providers);
        # on any other platform there are no prerequisites to surface.
        if not _is_apple_silicon():
            return []
        # The Install button (see _build_stt_install_script) installs ffmpeg,
        # pipx, and the mlx-based CLI itself. The only thing it cannot bootstrap
        # non-interactively is Homebrew, so that is the sole manual prereq —
        # listing the others here would duplicate the button. ``find_brew``
        # rather than ``shutil.which``: the desktop app's gateway inherits
        # PATH=/usr/bin:/bin:/usr/sbin:/sbin, so which() would claim Homebrew is
        # missing on every DMG install that has it.
        if not find_brew():
            return [
                '/bin/bash -c "$(curl -fsSL'
                ' https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
            ]
        # ffmpeg is still surfaced when missing: the Install button covers it
        # during setup, but a ready mlx install that later loses ffmpeg has no
        # button — this list is what the ready-state warning renders.
        return _ffmpeg_install_commands()

    has_python = _find_suitable_python() is not None

    if system == "Darwin":
        try:
            subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, timeout=5)
        except Exception:
            cmds.append("sudo xcodebuild -license accept")
        if not find_brew():
            cmds.append(
                '/bin/bash -c "$(curl -fsSL'
                ' https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
            )
        pkgs: list[str] = []
        if not has_python:
            pkgs.append("python@3.12")
        if not has_ffmpeg:
            pkgs.append("ffmpeg")
        if pkgs:
            cmds.append("brew install " + " ".join(pkgs))
    else:
        is_al2023 = _is_al2023()
        if not has_python:
            if shutil.which("apt-get"):
                cmds.append("sudo apt-get install -y python3 python3-pip python3-dev gcc g++")
            elif is_al2023:
                cmds.append(
                    "sudo dnf install -y python3.11 python3.11-pip python3.11-devel gcc gcc-c++"
                )
            else:
                # AL2: python3.7 is too old for whisper; Docker mode handles it
                pass
        if not has_ffmpeg:
            cmds.extend(_ffmpeg_install_commands())
    return cmds


def _is_al2023() -> bool:
    """Return True if running on Amazon Linux 2023."""
    try:
        return "2023" in Path("/etc/system-release").read_text(encoding="utf-8")
    except Exception:
        return False


def _find_suitable_python() -> str | None:
    """Find a non-free-threaded python3 >= 3.10 with pip.

    Delegates interpreter resolution to platform_compat.find_python_interpreter,
    which rejects internal build-system paths and — on Windows — the Microsoft Store alias
    stub (spawning that stub is what prints "Python was not found" and is why
    this probe must never touch it). This caller adds two requirements the shared
    helper does not: the interpreter must NOT be free-threaded (whisper wheels
    are unavailable) and MUST have pip (this is an install target). Those are
    passed as the ``reject`` predicate so the resolver FALLS THROUGH to the next
    candidate when one fails them, rather than giving up: a free-threaded/pip-less
    interpreter winning the name race must not mask a usable later one.
    """

    def _unusable(p: str) -> bool:
        # True => skip this interpreter and keep searching. A probe failure
        # (can't even run it) also counts as unusable.
        try:
            ver = subprocess.check_output(
                [p, "-c", "import sys; print(sys.version)"], timeout=5, text=True
            )
            if "free-threading" in ver:
                return True
            subprocess.check_output(
                [p, "-m", "pip", "--version"],
                timeout=5,
                text=True,
                stderr=subprocess.DEVNULL,
            )
            return False
        except Exception:
            return True

    return platform_compat.find_python_interpreter(reject=_unusable)


async def api_stt_install(request: web.Request) -> web.Response:
    """POST /api/stt/install — install openai-whisper + ffmpeg."""
    global _stt_install_status
    caller = request.get("user", "dashboard")
    if _stt_install_status["step"] not in ("idle", "done", "error"):
        _sel().log_api_access(
            caller=caller,
            operation="stt.install",
            outcome="denied",
            error=f"install already in progress: {_stt_install_status['step']}",
        )
        return web.json_response(
            {"error": f"Install already in progress: {_stt_install_status['step']}"}, status=409
        )

    # Native install via shell script, tailored to the configured provider.
    # Transcribe has no local runtime to install (its requirement is the
    # ``voice`` extra importable by this process, surfaced as a prerequisite
    # command) — reject rather than installing a Whisper runtime, which cannot
    # change Transcribe's availability.
    provider = KiroCrewConfig.load().stt.provider
    if provider == "transcribe":
        _sel().log_api_access(
            caller=caller,
            operation="stt.install",
            outcome="denied",
            error="no local install for provider=transcribe",
        )
        return web.json_response(
            {
                "code": "stt_no_local_install",
                "error": (
                    "AWS Transcribe has no local install;"
                    " run the prerequisite command to add the 'voice' extra instead"
                ),
            },
            status=400,
        )

    _stt_install_status = {"step": "starting", "detail": "", "error": ""}

    _sel().log_api_access(
        caller=caller,
        operation="stt.install",
        outcome="started",
        resources=f"provider={provider}",
    )
    script = _build_stt_install_script(provider)
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        # Read output line by line to update progress
        lines: list[str] = []
        assert proc.stdout is not None
        while True:
            line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=600)
            if not line_bytes:
                break
            line = line_bytes.decode(errors="replace").rstrip()
            lines.append(line)
            # Update status based on output
            if "Xcode" in line or "xcode" in line:
                _stt_install_status = {"step": "installing_xcode", "detail": line, "error": ""}
            elif ("Homebrew" in line or "brew" in line.lower()) and "Installing" in line:
                _stt_install_status = {"step": "installing_brew", "detail": line, "error": ""}
            elif "Installing ffmpeg" in line:
                _stt_install_status = {"step": "installing_ffmpeg", "detail": line, "error": ""}
            elif "Installing openai-whisper" in line:
                _stt_install_status = {"step": "installing_whisper", "detail": line, "error": ""}
            elif "Installing mlx-whisper" in line:
                _stt_install_status = {"step": "installing_mlx", "detail": line, "error": ""}
            elif "Installing parakeet-mlx" in line:
                # Reuse the mlx progress step: same pipx phase and bar position.
                # The detail line carries the accurate "parakeet-mlx" text, so a
                # dedicated step (and its 14-locale i18n key) is not warranted.
                _stt_install_status = {"step": "installing_mlx", "detail": line, "error": ""}
            elif "No suitable python3" in line:
                _stt_install_status = {"step": "installing_python", "detail": line, "error": ""}
            elif "Using:" in line:
                _stt_install_status = {"step": "checking", "detail": line, "error": ""}
            elif "Done." in line:
                _stt_install_status["detail"] = line
            elif line.startswith("ERROR:") or line.startswith("error:"):
                _stt_install_status["detail"] = line

        await proc.wait()
        output = "\n".join(lines[-20:])
        if proc.returncode != 0:
            _stt_install_status = {"step": "error", "detail": "", "error": output[-500:]}
            _sel().log_api_access(
                caller=caller,
                operation="stt.install",
                outcome="failed",
                resources=f"provider={provider}",
                error=output[-500:],
            )
            return web.json_response({"ok": False, "error": output[-500:]}, status=500)

        _stt_install_status = {"step": "done", "detail": "Whisper ready", "error": ""}
        _sel().log_api_access(
            caller=caller,
            operation="stt.install",
            outcome="success",
            resources=f"provider={provider}",
        )
        return web.json_response(
            {
                "ok": True,
                "ffmpeg": (
                    shutil.which("ffmpeg") is not None
                    or os.path.isfile(os.path.expanduser("~/ffmpeg/ffmpeg"))
                ),
            }
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except OSError:
            pass
        _stt_install_status = {"step": "error", "detail": "", "error": "Install timed out (10min)"}
        _sel().log_api_access(
            caller=caller,
            operation="stt.install",
            outcome="failed",
            resources=f"provider={provider}",
            error="install timed out",
        )
        return web.json_response({"ok": False, "error": "Install timed out"}, status=500)
    except FileNotFoundError:
        _stt_install_status = {"step": "error", "detail": "", "error": "bash not found"}
        _sel().log_api_access(
            caller=caller,
            operation="stt.install",
            outcome="failed",
            resources=f"provider={provider}",
            error="bash not found",
        )
        return web.json_response({"ok": False, "error": "bash not found"}, status=500)


def _stt_install_path_prelude() -> str:
    """Shell prelude that makes Homebrew (and pipx's bin dir) reachable.

    The install script runs as ``bash -c`` from the gateway process, which is
    neither a login nor an interactive shell — so the ``eval "$(brew shellenv)"``
    line in the user's ``~/.zprofile`` never executes. On a desktop-app install
    the inherited PATH is launchd's ``/usr/bin:/bin:/usr/sbin:/sbin``, which
    contains no Homebrew prefix at all. Without this prelude the script's first
    ``command -v brew`` check fails on a machine that HAS Homebrew and the whole
    install aborts with "ERROR: Homebrew required".

    Prepends the known prefixes (only those that exist), then defers to
    ``brew shellenv`` for the authoritative prefix once ``brew`` itself resolves.
    """
    dirs = " ".join(shlex.quote(d) for d in BREW_PATH_DIRS)
    return f"""
for _d in {dirs}; do
    case ":$PATH:" in
        *":$_d:"*) ;;
        *) [ -d "$_d" ] && PATH="$_d:$PATH" ;;
    esac
done
export PATH
if command -v brew >/dev/null 2>&1; then eval "$(brew shellenv)" 2>/dev/null || true; fi
"""


def _build_stt_install_script(provider: str = "whisper") -> str:
    """Shell script that installs the runtime for the selected STT provider.

    - ``mlx``: installs mlx-whisper via pipx (Apple Silicon only) plus ffmpeg.
    - ``parakeet``: installs parakeet-mlx via pipx (Apple Silicon only) plus ffmpeg.
    - ``whisper`` (default): installs openai-whisper + ffmpeg via brew or pip.

    The pip fallback deliberately targets a SYSTEM python with ``--user`` (never
    the gateway's own venv, which is replaced on every upgrade). ``--user`` lands
    in ``~/.local/bin``, which :func:`kiro_crew.transcribe._find_whisper` probes
    via its ``_WHISPER_SEARCH_PATHS`` (and via ``shutil.which`` when that dir is
    on PATH). It also constrains the resolve so pip can never drop into a source
    build — see the ``BINARY_ONLY`` comment in the script for why an incompatible
    wheel otherwise reports itself as a compiler error.
    """
    prelude = _stt_install_path_prelude()
    if provider in ("mlx", "parakeet"):
        pipx_pkg = "parakeet-mlx" if provider == "parakeet" else "mlx-whisper"
        verify_bin = "parakeet-mlx" if provider == "parakeet" else "mlx_whisper"
        return prelude + rf"""
[ -d "$HOME/ffmpeg" ] && export PATH="$HOME/ffmpeg:$PATH"

if ! command -v brew >/dev/null 2>&1; then
    echo "ERROR: Homebrew required. Install from https://brew.sh/"
    exit 1
fi

echo "Installing ffmpeg via brew..."
brew install ffmpeg 2>&1 || true

if ! command -v pipx >/dev/null 2>&1; then
    echo "Installing pipx via brew..."
    brew install pipx 2>&1 || {{ echo "ERROR: pipx install failed"; exit 1; }}
fi

echo "Installing {pipx_pkg} via pipx..."
pipx install --force {pipx_pkg} 2>&1 || {{ echo "ERROR: pipx install {pipx_pkg} failed"; exit 1; }}

echo "Done. {verify_bin}=$(command -v {verify_bin} 2>/dev/null || echo 'check PATH') ffmpeg=$(command -v ffmpeg 2>/dev/null || echo 'MISSING')"
"""
    return prelude + r"""
# Pick up ffmpeg from ~/ffmpeg if installed there
[ -d "$HOME/ffmpeg" ] && export PATH="$HOME/ffmpeg:$PATH"

# Prefer brew install (avoids externally-managed-environment errors)
if command -v brew >/dev/null 2>&1; then
    echo "Installing openai-whisper via brew..."
    if brew install openai-whisper 2>&1; then
        echo "Installing ffmpeg via brew..."
        brew install ffmpeg 2>&1 || true
        echo "Done. whisper=$(command -v whisper 2>/dev/null || echo 'check PATH') ffmpeg=$(command -v ffmpeg 2>/dev/null || echo 'MISSING')"
        exit 0
    fi
    echo "brew install failed, falling back to pip..."
fi

# Fallback: pip install (AL2023 / systems without brew)
PY=""
for py in python3.11 python3.12 python3 python3.13 python3.10; do
    p=$(command -v "$py" 2>/dev/null) || continue
    "$p" -c "import sys; sys.exit(0 if 'free-threading' not in sys.version else 1)" 2>/dev/null || continue
    "$p" -m pip --version >/dev/null 2>&1 || continue
    PY="$p"; break
done

if [ -z "$PY" ]; then
    echo "ERROR: python3 with pip not found. Install brew first: https://brew.sh/"
    exit 1
fi
echo "Using: $PY ($($PY --version))"

# openai-whisper itself is a pure-Python sdist, but its dependency tree is not:
# numpy / numba / llvmlite / torch / triton / tiktoken all ship COMPILED wheels.
# When pip finds no wheel matching the host it silently falls back to the source
# tarball and starts a compile — which is why a wheel-compatibility problem
# surfaces as a toolchain error ("GCC >= 9.3", "metadata-generation-failed")
# that names numpy and looks unrelated to the missing wheel. Amazon Linux 2 ships
# glibc 2.26, so pip accepts at most manylinux_2_17, while current numpy publishes
# manylinux_2_28 only — the default resolve therefore fetches numpy-2.5.1.tar.gz
# and dies on the system GCC (7.3 on AL2).
#
# --only-binary removes sdists from the candidate set for exactly these packages,
# so the resolver BACKTRACKS to the newest version that does have a compatible
# wheel instead of compiling (verified on glibc 2.26: numpy 2.5.1 -> 2.2.6
# manylinux_2_17, exit 0). Deliberately NO pinned version ceiling: a hardcoded cap
# would rot as hosts and wheel tags move, while letting pip choose the newest
# wheel-compatible release stays correct on both old and current hosts.
BINARY_ONLY="numpy,numba,llvmlite,torch,triton,tiktoken,regex"

# torch's default Linux wheels are the CUDA builds, so a plain resolve drags ~2.5 GB
# of nvidia-* packages onto a machine that has no GPU to use them. --extra-index-url
# would not help: it only ADDS a source, and pip still prefers the higher-versioned
# default build. So the CPU wheel gets its own step from the CPU-only index, and the
# whisper resolve below then sees torch already satisfied and leaves it alone.
# Non-fatal: if the CPU index is unreachable, fall through and let whisper resolve
# torch itself rather than failing an install that would otherwise succeed.
if [ "$(uname -s)" = "Linux" ] && ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "No NVIDIA GPU detected, installing CPU-only torch..."
    "$PY" -m pip install -q --user --only-binary=torch \
        --index-url https://download.pytorch.org/whl/cpu torch 2>&1 \
        || echo "CPU-only torch unavailable; letting openai-whisper resolve torch"
fi

echo "Installing openai-whisper..."
"$PY" -m pip install -q --user --only-binary="$BINARY_ONLY" openai-whisper 2>&1 \
    || { echo "ERROR: pip install openai-whisper failed"; exit 1; }

echo "Done. whisper=$(command -v whisper 2>/dev/null || echo 'check PATH') ffmpeg=$(command -v ffmpeg 2>/dev/null || echo 'MISSING')"
"""


async def api_stt_transcribe(request: web.Request) -> web.Response:
    """POST /api/stt/transcribe — transcribe uploaded audio via whisper."""
    import tempfile  # noqa: F811

    from kiro_crew.transcribe import is_available, transcribe_audio  # noqa: F811

    if not is_available():
        return web.json_response({"error": "STT not available"}, status=503)

    reader = await request.multipart()
    field = await reader.next()
    if field is None or not hasattr(field, "name") or field.name != "audio":  # type: ignore[union-attr]
        return web.json_response({"error": "missing audio field"}, status=400)

    # Use uploaded filename extension (recording.webm / .mp4 / .ogg)
    fname = getattr(field, "filename", None) or "recording.webm"
    ext = os.path.splitext(fname)[1] or ".webm"
    fd, tmp = tempfile.mkstemp(suffix=ext)
    try:
        os.close(fd)
        size = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = await field.read_chunk(8192)  # type: ignore[union-attr]
                if not chunk:
                    break
                size += len(chunk)
                if size > 25 * 1024 * 1024:  # 25 MB cap
                    return web.json_response({"error": "audio too large"}, status=413)
                f.write(chunk)

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
        return web.json_response({"error": "transcription failed"}, status=500)
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
    """GET /api/sel/verify — verify HMAC chain integrity."""

    # Same offload rationale as api_sel_events, including deferring _sel() into
    # the callable: verify_integrity() reads the whole log file to check the HMAC
    # chain end to end and must not run on the event loop.
    total, valid = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), lambda: _sel().verify_integrity()
    )
    return web.json_response(
        {
            "total": total,
            "valid": valid,
            "integrity": "ok" if total == valid else "compromised",
            "tampered": total - valid,
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
        path = config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            _sel().log_api_access(
                caller=caller,
                operation="config.update",
                outcome="error",
                error="config.json is corrupt",
            )
            return web.json_response({"error": "config.json is corrupt"}, status=500)
        if not isinstance(data.get("agent"), dict):
            data["agent"] = {}
        agent = data["agent"]
        # Snapshot the persisted values BEFORE any mutation. The dashboard sends
        # all four settings on every save and enables Save whenever any one is
        # dirty, so "was applied" is not "was changed" -- keying the restart hint
        # off the raw applied list would flag a restart for a conductor-only save.
        # Same truthfulness guard as handlers/messaging.py (see its no-op-save
        # comments) so the flag stays trustworthy enough to act on.
        before = dict(agent)
        # subagent_max_turns keeps the generic 1..N validation; max_subagents is
        # special — 0 is the "auto-size" sentinel and its upper bound is the
        # configured hard cap (dynamic-subagent-sizing.md §5.5/§6).
        limits = {"subagent_max_turns": SUBAGENT_MAX_TURNS_CEILING}
        applied: list[str] = []
        for key, upper in limits.items():
            if key in agent_settings:
                val = agent_settings[key]
                if isinstance(val, bool) or not isinstance(val, int) or val < 1 or val > upper:
                    return _deny(f"{key} must be an integer between 1 and {upper}")
                agent[key] = val
                applied.append(key)
        # Capture the hard cap from the *persisted* config BEFORE applying any
        # subagent_auto_max from this request. max_subagents is bounded by this
        # persisted value only: a same-request raise of subagent_auto_max must NOT
        # widen the bound (deny-by-default — prevents
        # {subagent_auto_max: 9999, max_subagents: 9999} bypass). A higher ceiling
        # only takes effect for max_subagents on a *subsequent* request.
        persisted_hard_cap = agent.get("subagent_auto_max", 16)
        if (
            not isinstance(persisted_hard_cap, int)
            or isinstance(persisted_hard_cap, bool)
            or persisted_hard_cap < 3
        ):
            persisted_hard_cap = 16
        # Clamp to the absolute ceiling even when read from persisted config: a
        # corrupt or hand-edited config (e.g. {"subagent_auto_max": 9999}) must not
        # be trusted to widen the concurrency bound (deny-by-default).
        persisted_hard_cap = min(persisted_hard_cap, SUBAGENT_AUTO_MAX_CEILING)
        # subagent_auto_max is now persistable (so the dashboard can raise/lower the
        # auto-size ceiling), but carries its own absolute upper bound
        # (SUBAGENT_AUTO_MAX_CEILING) so it can never be set arbitrarily high.
        if "subagent_auto_max" in agent_settings:
            val = agent_settings["subagent_auto_max"]
            if (
                isinstance(val, bool)
                or not isinstance(val, int)
                or val < 3
                or val > SUBAGENT_AUTO_MAX_CEILING
            ):
                return _deny(
                    "subagent_auto_max must be an integer between 3 and "
                    f"{SUBAGENT_AUTO_MAX_CEILING}"
                )
            agent["subagent_auto_max"] = val
            applied.append("subagent_auto_max")
        # max_subagents: 0 = auto-size; otherwise a fixed pin in
        # [MAX_SUBAGENTS_FIXED_FLOOR, persisted_hard_cap]. The bound is the
        # persisted ceiling captured above, never this request's value. A pin of
        # 1 or 2 is rejected — it would disable auto-sizing and run below the
        # default (0 is the only way to request the host-safe auto cap).
        if "max_subagents" in agent_settings:
            val = agent_settings["max_subagents"]
            hard_cap = persisted_hard_cap
            if (
                isinstance(val, bool)
                or not isinstance(val, int)
                or (val != 0 and not (MAX_SUBAGENTS_FIXED_FLOOR <= val <= hard_cap))
            ):
                return _deny(
                    f"max_subagents must be 0 (auto) or an integer between "
                    f"{MAX_SUBAGENTS_FIXED_FLOOR} and {hard_cap}"
                )
            agent["max_subagents"] = val
            applied.append("max_subagents")
        # Boolean toggles
        for key in ("conductor_skill",):
            if key in agent_settings:
                val = agent_settings[key]
                if not isinstance(val, bool):
                    return _deny(f"{key} must be a boolean")
                agent[key] = val
                applied.append(key)
        if not applied:
            return _deny("no recognized settings provided")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        _sel().log_api_access(
            caller=caller,
            operation="config.update",
            outcome="ok",
            resources=",".join(applied),
        )
        # Regenerate or clean up conductor skill on toggle.
        if "conductor_skill" in applied:
            if agent.get("conductor_skill"):
                from kiro_crew.dashboard.handlers.agents import _regen_conductor  # noqa: F811

                _regen_conductor()
            else:
                try:
                    from kiro_crew.skills import SkillsLoader  # noqa: F811

                    p = SkillsLoader()._dir / "conductor" / "SKILL.md"
                    if p.exists():
                        p.unlink()
                except Exception:
                    logger.exception("Failed to clean up conductor skill")
        # A startup-read key that was merely re-sent with its existing value did
        # not change the enforced cap, so it must not raise the hint.
        restart_required = any(
            key in _STARTUP_READ_AGENT_KEYS and agent.get(key) != before.get(key) for key in applied
        )
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


def _validate_role_model(value: str, request: web.Request) -> str | None:
    """Reject a per-role model pin the account cannot use; ``None`` = allow.

    ``""`` / ``"auto"`` always allow (they defer to the chat default). Otherwise
    reuse the per-session provider guard (rejects display-only canonical keys for
    the active provider), then — when a live advertised set is known — apply the
    SAME entitlement predicate the session-init withhold uses
    (:func:`model_is_unusable`, #1596) so the picker and the wire cannot disagree.
    No advertised set => accept (entitlement unknowable; don't accuse on no
    evidence), matching that predicate's own conservative default.
    """
    if not value or value == "auto":
        return None
    from kiro_crew.acp.client import model_is_unusable
    from kiro_crew.dashboard.chat_handlers import _model_rejected_reason

    reason = _model_rejected_reason(value)
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


_EDITABLE_CONFIG: dict[str, dict] = {
    "agent.provider": {"type": "enum", "values": ["acp"]},
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
    "agent.completion_keep_chars": {"type": "int", "min": 0, "max": RESULT_FILE_MAX_BYTES},
    "agent.soft_stop_budget_secs": {"type": "float", "min": 0.5, "max": 60.0},
    "session.timeout_secs": {"type": "int", "min": 0, "max": 86400},
    # Range shared with the load-time clamp in config/loader.py — one constant
    # pair, so the write gate and the load path cannot drift (issue #4734).
    "session.autocompact_pct": {
        "type": "float",
        "min": AUTOCOMPACT_PCT_MIN,
        "max": AUTOCOMPACT_PCT_MAX,
    },
    "session.pool_size": {"type": "int", "min": 0, "max": 10},
    "session.pool_agent": {"type": "str", "values_fn": _agent_values},
    "session.pool_ttl_secs": {"type": "int", "min": 0, "max": 7200},
    # Intent-level session summaries in the chat right panel. Only the boolean
    # enable is editable here: it spends tokens on turns the user did not ask to
    # pay for, so it is off by default and the Settings toggle is the single
    # opt-in. The cadence/cap fields (min_user_turns, max_intents, …) stay
    # config-file-only — they are power-user knobs, not first-run choices.
    "session_summary.enabled": {"type": "bool"},
    "auto_update": {"type": "bool"},
    "dashboard.mcp_probe_timeout_secs": {"type": "int", "min": 5, "max": 120},
    "dashboard.recent_tint_count": {"type": "int", "min": 0, "max": 10},
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
    # `_build_recorder` attaches an OTLP reader when `telemetry.otlp_endpoint` is
    # set — so the gate below refuses the ENABLE on a host that configured an
    # endpoint, which is what keeps the switch's local-only promise true for every
    # state it can reach. The endpoint itself stays config-file-only, so a
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
    # Knowledge Library auto-ingest. Chunk budget max mirrors the point past which
    # a single sweep stops being a trickle; dedup cadence max is ~a day of sweeps.
    "knowledge.auto_add_documents": {"type": "bool"},
    "knowledge.auto_register_project_docs": {"type": "bool"},
    "knowledge.auto_ingest_artifacts": {"type": "bool"},
    "knowledge.auto_ingest_chunk_budget": {"type": "int", "min": 0, "max": 10000},
    "knowledge.folder_ingest_chunk_budget": {"type": "int", "min": 0, "max": 10000},
    "knowledge.dedup_every_n_sweeps": {"type": "int", "min": 0, "max": 288},
    "knowledge.sweep_chunk_budget": {"type": "int", "min": 0, "max": 50000},
    "knowledge.max_sources": {"type": "int", "min": 0, "max": 1000},
    "knowledge.embed_rate_limit": {"type": "int", "min": 0, "max": 10000},
    "knowledge.extraction_model": {"type": "str"},
    "knowledge.extraction_pool_size": {"type": "int", "min": 1, "max": 10},
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
        if value not in spec["values"]:
            return _deny(f"invalid value, must be one of {spec['values']}", f"{path_key}={value}")
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
    # `_build_recorder` attaches an OTLP reader whenever `telemetry.otlp_endpoint`
    # is set (see metrics/provider.py), so on a host that already configured an
    # endpoint, enabling collection from the dashboard would start network egress
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
            endpoint = str(getattr(cfg.telemetry, "otlp_endpoint", "") or "")
        except Exception:
            # Unreadable config: fail closed rather than enabling collection whose
            # egress posture cannot be established.
            logger.warning("telemetry config unreadable; refusing to enable", exc_info=True)
            return _deny("could not read the telemetry configuration", f"{path_key}={value}", 409)
        if endpoint.strip():
            return _deny(
                "telemetry.otlp_endpoint is set, so enabling collection here would "
                "also export metrics off this machine. Enable it in the config file "
                "instead, where the endpoint is configured.",
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
    if path_key in ("agent.model", "agent.reasoning_effort") or path_key.startswith(
        "agent.role_efforts."
    ):
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


async def api_session_agents_list(request: web.Request) -> web.Response:
    """GET /api/sessions/{id}/agents — list sub-agent results for a session."""
    session_id = request.match_info["id"]
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
