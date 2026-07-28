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
import shlex
import shutil
import subprocess
from pathlib import Path

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

import kiro_crew
from kiro_crew import platform_compat
from kiro_crew.config.loader import (
    _VALID_STT_PROVIDERS,
    MAX_SUBAGENTS_FIXED_FLOOR,
    SUBAGENT_AUTO_MAX_CEILING,
    SUBAGENT_MAX_TURNS_CEILING,
    KiroCrewConfig,
    config_path,
)
from kiro_crew.context_management import RESULT_FILE_MAX_BYTES
from kiro_crew.dashboard.origin import check_host, is_direct_local_request
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.token_auth import MAX_SESSION_TTL_SECS, generate_token, parse_duration
from kiro_crew.security_posture import build_posture_snapshot_async, posture_counts_async
from kiro_crew.transcribe import ensure_ffmpeg_in_path, is_available

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
    "<p><strong>Try restarting KiroCrew.</strong> The exact restart step"
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
    The legacy server-rendered ``dashboard.html`` fallback was removed (an
    XSS follow-up): it shipped an incomplete HTML
    ``esc()`` and a permissive inline-script surface, and existed only as a
    build-time fallback. The React SPA (reviewed clean) is now the only shell.

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
    """GET /api/ready — Kubernetes-style readiness probe (Recommendation #6).

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


async def api_theme_boot(request: web.Request) -> web.Response:
    """GET /api/theme/boot — workspace theme config for frontend boot.

    Unauthenticated (same boundary as /api/health) so the SPA can read the
    workspace theme before the token flow completes. Contains no secrets —
    only the workspace-level theme preferences and onboarding flags.
    """
    cfg = KiroCrewConfig.load()
    return web.json_response(
        {
            "mode": cfg.dashboard.theme_mode or "",
            "color": cfg.dashboard.theme_color or "",
            "onboarded": cfg.dashboard.onboarded,
            "import_onboarded": cfg.dashboard.import_onboarded,
        }
    )


async def api_theme_config(request: web.Request) -> web.Response:
    """GET/PUT /api/config/theme — read or update workspace theme settings.

    GET returns the current theme config. PUT accepts
    {mode?, color?, onboarded?, import_onboarded?} and persists to the workspace
    config file.
    """
    if request.method == "GET":
        cfg = KiroCrewConfig.load()
        return web.json_response(
            {
                "mode": cfg.dashboard.theme_mode or "",
                "color": cfg.dashboard.theme_color or "",
                "onboarded": cfg.dashboard.onboarded,
                "import_onboarded": cfg.dashboard.import_onboarded,
            }
        )

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

        if changed:
            await asyncio.to_thread(cfg.save)

    return web.json_response(
        {
            "mode": cfg.dashboard.theme_mode or "",
            "color": cfg.dashboard.theme_color or "",
            "onboarded": cfg.dashboard.onboarded,
            "import_onboarded": cfg.dashboard.import_onboarded,
        }
    )


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

    ``mlx`` (Whisper on Apple's MLX framework) only runs on Apple Silicon, so
    it is omitted entirely on every other platform rather than being shown as
    an unusable option. This is the single source of truth for which providers
    are advertised (GET) and accepted (PUT).
    """
    providers = list(_VALID_STT_PROVIDERS)
    if not _is_apple_silicon() and "mlx" in providers:
        providers.remove("mlx")
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
            if "transcribe_region" in body and isinstance(body["transcribe_region"], str):
                stt["transcribe_region"] = body["transcribe_region"]
            if "transcribe_profile" in body and isinstance(body["transcribe_profile"], str):
                stt["transcribe_profile"] = body["transcribe_profile"]
            if "language_code" in body and isinstance(body["language_code"], str):
                stt["language_code"] = body["language_code"]
            if "streaming" in body and isinstance(body["streaming"], bool):
                stt["streaming"] = body["streaming"]
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(_atomic_json_write, path, data)
        cfg = KiroCrewConfig.load()

    provider = cfg.stt.provider
    available = is_available(cfg.stt)
    # _stt_prereq_commands probes for a system python/brew via subprocess; run it
    # off the event loop so a slow/again-spawned interpreter check can't stall the
    # gateway (observed as "event-loop heartbeat: lag" on Windows where the probe
    # is heavier). The GET is read-only, so threading it is safe.
    prereqs = await asyncio.to_thread(_stt_prereq_commands, provider)
    return web.json_response(
        {
            "enabled": cfg.stt.enabled,
            "provider": provider,
            "model": cfg.stt.model,
            "mlx_model": cfg.stt.mlx_model,
            "available": available,
            "streaming": cfg.stt.streaming,
            "transcribe_region": cfg.stt.transcribe_region,
            "transcribe_profile": cfg.stt.transcribe_profile,
            "language_code": cfg.stt.language_code,
            "models": _STT_MODEL_SIZES,
            "mlx_models": _STT_MLX_MODELS,
            "providers": _stt_providers(),
            "language_codes": list(_STT_LANGUAGE_CODES),
            "install_step": _stt_install_status["step"],
            "install_detail": _stt_install_status["detail"],
            "install_error": _stt_install_status["error"],
            "prereqs": prereqs,
        }
    )


def _stt_prereq_commands(provider: str = "whisper") -> list[str]:
    """Return shell commands the user must run manually (need sudo/GUI).

    The ``mlx`` provider has its own lightweight prerequisite (``pipx install
    mlx-whisper``) and only needs ffmpeg beyond that — it does not require the
    system-python/whisper toolchain.
    """
    ensure_ffmpeg_in_path()

    system = platform.system()
    cmds: list[str] = []
    has_ffmpeg = shutil.which("ffmpeg") is not None

    if provider == "mlx":
        # mlx is only advertised on Apple Silicon (see _stt_providers); on any
        # other platform there are no prerequisites to surface.
        if not _is_apple_silicon():
            return []
        # The Install button (see _build_stt_install_script) installs ffmpeg,
        # pipx, and mlx-whisper itself. The only thing it cannot bootstrap
        # non-interactively is Homebrew, so that is the sole manual prereq —
        # listing the others here would duplicate the button.
        if not shutil.which("brew"):
            return [
                '/bin/bash -c "$(curl -fsSL'
                ' https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
            ]
        return []

    has_python = _find_suitable_python() is not None

    if system == "Darwin":
        try:
            subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, timeout=5)
        except Exception:
            cmds.append("sudo xcodebuild -license accept")
        if not shutil.which("brew"):
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
            if shutil.which("apt-get"):
                cmds.append("sudo apt-get install -y ffmpeg")
            else:
                # AL2023/AL2: build minimal ffmpeg from source (official recommendation)
                proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
                script = os.path.join(proj, "scripts", "build-ffmpeg.sh") if proj else ""
                if script and os.path.isfile(script):
                    cmds.append(
                        "sudo dnf install -y gcc make nasm diffutils 2>/dev/null"
                        " || sudo yum install -y gcc make nasm diffutils"
                    )
                    cmds.append(f"bash {shlex.quote(script)}")
                else:
                    cmds.append("echo 'Build ffmpeg from source:" " https://ffmpeg.org/releases/'")
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
    candidate when one fails them, rather than giving up — preserving the
    multi-candidate behavior the pre-shim loop had (a free-threaded/pip-less
    interpreter winning the name race must not mask a usable later one).
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

    _stt_install_status = {"step": "starting", "detail": "", "error": ""}

    # Native install via shell script, tailored to the configured provider.
    provider = KiroCrewConfig.load().stt.provider
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
                "ffmpeg": shutil.which("ffmpeg") is not None
                or os.path.isfile(os.path.expanduser("~/ffmpeg/ffmpeg")),
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


def _build_stt_install_script(provider: str = "whisper") -> str:
    """Shell script that installs the runtime for the selected STT provider.

    - ``mlx``: installs mlx-whisper via pipx (Apple Silicon only) plus ffmpeg.
    - ``whisper`` (default): installs openai-whisper + ffmpeg via brew or pip.
    """
    if provider == "mlx":
        return r"""
[ -d "$HOME/ffmpeg" ] && export PATH="$HOME/ffmpeg:$PATH"

if ! command -v brew >/dev/null 2>&1; then
    echo "ERROR: Homebrew required. Install from https://brew.sh/"
    exit 1
fi

echo "Installing ffmpeg via brew..."
brew install ffmpeg 2>&1 || true

if ! command -v pipx >/dev/null 2>&1; then
    echo "Installing pipx via brew..."
    brew install pipx 2>&1 || { echo "ERROR: pipx install failed"; exit 1; }
fi

echo "Installing mlx-whisper via pipx..."
pipx install --force mlx-whisper 2>&1 || { echo "ERROR: pipx install mlx-whisper failed"; exit 1; }

echo "Done. mlx_whisper=$(command -v mlx_whisper 2>/dev/null || echo 'check PATH') ffmpeg=$(command -v ffmpeg 2>/dev/null || echo 'MISSING')"
"""
    return r"""
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

echo "Installing openai-whisper..."
"$PY" -m pip install -q --user openai-whisper || { echo "ERROR: pip install openai-whisper failed"; exit 1; }

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
    events = _sel().recent(limit=limit)
    return web.json_response({"events": events, "count": len(events)})


async def api_sel_verify(request: web.Request) -> web.Response:
    """GET /api/sel/verify — verify HMAC chain integrity."""

    total, valid = _sel().verify_integrity()
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

    The dashboard no longer calls this — Settings → Security reads
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


async def api_kirocrew_config(request: web.Request) -> web.Response:
    """GET/PUT /api/config/kirocrew — read or update KiroCrew config."""
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
        return web.json_response({"ok": True})

    cfg = KiroCrewConfig.load()
    return web.json_response(_masked_config_dict(cfg))


# Allowed editable config paths and their validators
def _agent_values() -> set[str]:
    """Return allowed pool_agent values: empty string + all configured agent names."""
    from kiro_crew.config.loader import KiroCrewConfig

    return {"", *KiroCrewConfig.load().agents}


_EDITABLE_CONFIG: dict[str, dict] = {
    "agent.provider": {"type": "enum", "values": ["acp"]},
    "agent.approval_mode": {"type": "enum", "values": ["auto", "interactive"]},
    "agent.sandbox": {"type": "enum", "values": ["auto", "off"]},
    "agent.sandbox_allow_no_isolation": {"type": "bool"},
    "agent.apps_allow_third_party": {"type": "bool"},
    "agent.completion_keep": {"type": "enum", "values": ["head", "tail", "both"]},
    "agent.completion_keep_chars": {"type": "int", "min": 0, "max": RESULT_FILE_MAX_BYTES},
    "agent.soft_stop_budget_secs": {"type": "float", "min": 0.5, "max": 60.0},
    "session.timeout_secs": {"type": "int", "min": 0, "max": 86400},
    "session.autocompact_pct": {"type": "float", "min": 5.0, "max": 90.0},
    "session.pool_size": {"type": "int", "min": 0, "max": 10},
    "session.pool_agent": {"type": "str", "values_fn": _agent_values},
    "session.pool_ttl_secs": {"type": "int", "min": 0, "max": 7200},
    "auto_update": {"type": "bool"},
    "dashboard.mcp_probe_timeout_secs": {"type": "int", "min": 5, "max": 120},
    "dashboard.recent_tint_count": {"type": "int", "min": 0, "max": 10},
    # User profile (onboarding step 2 + Settings > General > About You).
    # Structured slugs, not free text: context.py maps them to prompt-ready
    # descriptions in its [USER PROFILE] block. "" = unspecified/cleared.
    "dashboard.user_role": {
        "type": "enum",
        "values": ["", "developer", "designer", "product-manager", "data-ml", "it-ops", "other"],
    },
    "dashboard.user_technical_level": {
        "type": "enum",
        "values": ["", "codes", "somewhat-technical", "non-technical"],
    },
    # SSO login flags for an edition that supplies a real sso_login_handler.
    # Bounded to a short string here; the companion login handler re-validates
    # each token against its own flag allowlist before spawning the login PTY
    # (defense in depth — this gate only stores the value). Inert in public build.
    "dashboard.sso_login_flags": {"type": "str", "max_len": 256},
    # Instances (multi-instance management). Toggling enabled needs a gateway
    # restart to take effect (the SSH manager + CSP relaxation init at startup),
    # so the Instances settings panel surfaces a "restart required" hint.
    "instances.enabled": {"type": "bool"},
}


async def api_kirocrew_config_patch(request: web.Request) -> web.Response:
    """PATCH /api/config/kirocrew — update a single config field."""
    from kiro_crew.agent import _atomic_json_write  # noqa: F811
    from kiro_crew.config.loader import config_path  # noqa: F811

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
        values_fn = spec.get("values_fn")
        if values_fn and value not in values_fn():
            return _deny(f"invalid value for {path_key}", f"{path_key}={value}")
    else:
        return _deny("unsupported config type", f"{path_key}={value}", 500)

    # Read, update, write
    cfg_path = config_path()
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        except Exception:
            _log_sel("error", f"{path_key}=read_failed")
            return web.json_response({"error": "failed to read config file"}, status=500)

        parts = path_key.split(".")
        if len(parts) == 2:
            section = data.setdefault(parts[0], {})
            if not isinstance(section, dict):
                _log_sel("error", f"{path_key}=section_not_dict")
                return web.json_response(
                    {"error": f"config section '{parts[0]}' is not an object"}, status=500
                )
            section[parts[1]] = value
        else:
            data[parts[0]] = value

        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json_write(cfg_path, data)
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

    return web.json_response(_masked_config_dict(cfg))


# ── Local token bootstrap (Electron / local apps) ─────────────────────


async def api_token_local(request: web.Request) -> web.Response:
    """GET /api/token/local — issue a token for local apps.

    Requires a per-session secret written to ~/.kirocrew/.local_secret at
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
    import asyncio  # noqa: F811

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

    revoke_all_sessions()
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
