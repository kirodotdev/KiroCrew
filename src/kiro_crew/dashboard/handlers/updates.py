"""Update check/apply, log level, ring buffer, and SSE stream handlers."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

from kiro_crew import __version__ as _local_version
from kiro_crew import shutdown_event
from kiro_crew.config.loader import KiroCrewConfig, config_path
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

_SSE_INTERVAL_SECS = 5

# ── Update ──

# Cached update check result
_update_info: dict[str, object] = {"available": False, "changes": "", "checked": False}
_UPDATE_CHECK_INTERVAL = 43200  # 12 hours
_last_update_check: float = 0.0


def get_update_info() -> dict[str, object]:
    """Return a copy of the cached update-check state."""
    return dict(_update_info)


async def api_update_check(request: web.Request) -> web.Response:
    """GET /api/update/check — check if git remote has new commits."""
    await _do_update_check()
    cfg = KiroCrewConfig.load()
    return web.json_response({**_update_info, "auto_update": cfg.auto_update})


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse version string to tuple for safe numeric comparison."""
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0,)


async def _do_update_check() -> None:
    """Run git fetch and compare HEAD with remote."""
    global _last_update_check

    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if not proj:
        return
    # Skip when the project dir isn't a git checkout — e.g. a cloud/EC2 install
    # that received its source as a tarball. Without this the update poller
    # spams "git fetch failed: not a git repository" every cycle. exists() (not
    # isdir): in linked worktrees/submodules .git is a file, but git works.
    if not os.path.exists(os.path.join(proj, ".git")):
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "fetch",
            "--quiet",
            cwd=proj,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, fetch_err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
            logger.warning("git fetch timed out")
            return
        if proc.returncode != 0:
            logger.warning(
                "git fetch failed (rc=%s): %s",
                proc.returncode,
                (fetch_err or b"").decode(errors="replace").strip(),
            )
            return

        local = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=proj,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            local_out, _ = await asyncio.wait_for(local.communicate(), timeout=10)
        except asyncio.TimeoutError:
            try:
                local.kill()
            except ProcessLookupError:
                pass
            await local.communicate()
            return
        remote = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "@{u}",
            cwd=proj,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            remote_out, _ = await asyncio.wait_for(remote.communicate(), timeout=10)
        except asyncio.TimeoutError:
            try:
                remote.kill()
            except ProcessLookupError:
                pass
            await remote.communicate()
            return

        local_sha = local_out.decode(errors="replace").strip()
        remote_sha = remote_out.decode(errors="replace").strip()

        # Check version: compare remote (or on-disk if already pulled) vs running
        available = False
        remote_version = ""
        target_sha = remote_sha if local_sha != remote_sha else local_sha
        if local_sha and remote_sha:
            show = await asyncio.create_subprocess_exec(
                "git",
                "show",
                f"{target_sha}:src/kiro_crew/__init__.py",
                cwd=proj,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                show_out, _ = await asyncio.wait_for(show.communicate(), timeout=10)
            except asyncio.TimeoutError:
                try:
                    show.kill()
                except ProcessLookupError:
                    pass
                await show.communicate()
                return
            m = re.search(r'__version__\s*=\s*"(.+?)"', show_out.decode(errors="replace"))
            if m:
                remote_version = m.group(1)
            available = (
                _version_tuple(remote_version) > _version_tuple(_local_version)
                if remote_version
                else False
            )

        changes = ""
        if available:
            diff_base = f"v{_local_version}" if local_sha == remote_sha else local_sha
            diff = await asyncio.create_subprocess_exec(
                "git",
                "diff",
                f"{diff_base}..{target_sha}",
                "--",
                "CHANGELOG.md",
                cwd=proj,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                diff_out, _ = await asyncio.wait_for(diff.communicate(), timeout=10)
            except asyncio.TimeoutError:
                try:
                    diff.kill()
                except ProcessLookupError:
                    pass
                await diff.communicate()
                return
            # Extract added lines from changelog diff
            lines: list[str] = []
            for line in diff_out.decode(errors="replace").splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    lines.append(line[1:])
            changes = "\n".join(lines).strip()

        _update_info["available"] = available
        _update_info["changes"] = changes
        _update_info["remote_version"] = remote_version
        _update_info["checked"] = True
        _last_update_check = time.time()
    except Exception:
        logger.debug("Update check failed", exc_info=True)


async def api_update_auto(request: web.Request) -> web.Response:
    """POST /api/update/auto — toggle auto-update on/off."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    enabled = body.get("enabled", True)
    # Read, modify, write config
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    data["auto_update"] = enabled
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return web.json_response({"ok": True, "auto_update": enabled})


def _changelog_path() -> Path | None:
    """Locate CHANGELOG.md across install layouts.

    1. ``KIROCREW_PROJECT_DIR/CHANGELOG.md`` — dev / git installs.
    2. Bundled ``kiro_crew/CHANGELOG.md`` — pip-wheel installs where
       no source tree is present (copied into the package at build time by
       ``setup.py``'s ``BuildWithFrontend._copy_changelog``).

    Returns the first existing path, or ``None`` if neither is found.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if proj:
        p = Path(proj) / "CHANGELOG.md"
        if p.is_file():
            return p
    # updates.py lives at kiro_crew/dashboard/handlers/ — parents[2] == kiro_crew/
    bundled = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
    if bundled.is_file():
        return bundled
    return None


async def api_changelog(request: web.Request) -> web.Response:
    """GET /api/changelog — read full CHANGELOG.md from project or bundle."""
    path = _changelog_path()
    content = ""
    if path is not None:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            content = ""
    return web.json_response({"content": content})


async def _build_frontend(proj: str, state: DashboardState) -> None:
    """Build the in-tree ``website/`` frontend and stage it into ``static/dist``.

    Delegates to the shared :func:`kiro_crew.frontend.build_frontend_async`
    helper so the build/stage logic is not duplicated across the three update
    paths (CLI ``kirocrew update``, this dashboard endpoint, and the gateway
    auto-update). That helper runs ``npm ci`` (fallback ``npm install``) then
    ``npm run build`` in ``<proj>/website`` and copies ``website/dist`` into
    the served ``src/kiro_crew/static/dist`` — without which the dashboard
    would serve a stale bundle after an update.

    Graceful no-op when there is no ``website/`` directory or ``npm`` is not
    installed (a packaged checkout may ship prebuilt assets). Build warnings
    are surfaced via ``state.push_update_progress`` after credential/URL
    redaction.
    """
    from kiro_crew import frontend

    def _push(step: str, msg: str) -> None:
        msg, _ = redact_credentials(msg)
        msg, _ = redact_exfiltration_urls(msg)
        # frontend emits ("warning", detail); show it as a non-fatal build note.
        state.push_update_progress("building", msg)

    state.push_update_progress("building", "Building frontend (npm)…")
    await frontend.build_frontend_async(proj, push_progress=_push)


async def _venv_pip_install(proj: str, state: DashboardState) -> bool:
    """Run `pip install -e .` to (re)install the package from source.

    Returns ``True`` on success. On failure, pushes an error to ``state`` and
    returns ``False`` — caller should ``return``.
    """
    state.push_update_progress("building", "Installing package (pip)…")
    install = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pip",
        "install",
        "-e",
        ".",
        "--quiet",
        cwd=proj,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(install.communicate(), timeout=120)
    except asyncio.TimeoutError:
        try:
            install.kill()
        except ProcessLookupError:
            pass
        await install.communicate()
        state.push_update_progress("error", "pip install timed out")
        return False
    if install.returncode != 0:
        raw_err = stderr.decode()
        raw_err, _ = redact_credentials(raw_err)
        raw_err, _ = redact_exfiltration_urls(raw_err)
        # Build wheel / native extension errors often have the actionable message
        # mid-stderr after a long traceback, so keep up to 1000 chars after redaction.
        if len(raw_err) > 1000:
            raw_err = raw_err[:1000] + "\n…(truncated)"
        state.push_update_progress("error", f"pip install failed: {raw_err}")
        return False
    return True


async def _restart_gateway(state: DashboardState) -> None:
    """Save state, close sessions, and exec the same Python process."""
    state.push_update_progress("restarting", "Restarting server…")
    exe = sys.executable
    if not os.path.isfile(exe) or not os.access(exe, os.X_OK):
        state.push_update_progress("error", "Cannot restart: invalid Python executable path")
        return
    # circular import: kiro_crew.dashboard.chat imports from
    # kiro_crew.dashboard.handlers (which re-exports this module), so this
    # must stay inline to avoid an import cycle at module load.
    from kiro_crew.dashboard.chat import save_all_slots_to_history
    from kiro_crew.executors import subprocess_executor

    try:
        # Offload the synchronous per-slot save (per-session lock + disk I/O)
        # to the bounded subprocess_executor with a deadline: on the event loop
        # a contended session raises HistoryLockTimeout and a wedged disk would
        # block the restart, so a slot's final save must run off-loop and be
        # time-bounded rather than stall (or silently drop) here.
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), save_all_slots_to_history, state
            ),
            timeout=5.0,
        )
    except Exception:
        logger.debug("History save before restart failed", exc_info=True)
    try:
        await state.sessions.close_all()
    except Exception:
        logger.debug("Session cleanup before restart failed", exc_info=True)
    sys.stdout.flush()
    sys.stderr.flush()
    await asyncio.sleep(0.5)
    os.execv(exe, [exe, "-m", "kiro_crew"] + sys.argv[1:])


async def api_update_apply(request: web.Request) -> web.Response:
    """POST /api/update — git pull, rebuild, restart gateway."""
    state: DashboardState = request.app["state"]

    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if not proj:
        return web.json_response({"error": "KIROCREW_PROJECT_DIR not set"}, status=400)
    # Mirror the _do_update_check git guard: a tarball install (e.g. cloud/EC2)
    # has no .git, so `git pull` cannot update it — fail with a clear message
    # instead of a generic "git pull failed".
    if not os.path.exists(os.path.join(proj, ".git")):
        return web.json_response(
            {"error": "Not a git checkout — update by redeploying (e.g. `kirocrew cloud launch`)"},
            status=409,
        )

    # Signal updating state via SSE
    state.push_refresh("updating")

    # Check for dirty working tree before updating
    dirty = await asyncio.create_subprocess_exec(
        "git",
        "status",
        "--porcelain",
        cwd=proj,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        dirty_out, _ = await asyncio.wait_for(dirty.communicate(), timeout=10)
    except asyncio.TimeoutError:
        try:
            dirty.kill()
        except ProcessLookupError:
            pass
        await dirty.communicate()
        return web.json_response(
            {"error": "Timed out checking working tree status"},
            status=500,
        )
    if dirty_out and dirty_out.strip():
        logger.warning("Update skipped: working tree has uncommitted changes")
        return web.json_response(
            {"error": "Working tree has uncommitted changes — commit or stash first"},
            status=409,
        )

    async def _apply() -> None:
        try:
            # git pull
            state.push_update_progress("pulling", "Pulling latest changes…")
            pull = await asyncio.create_subprocess_exec(
                "git",
                "pull",
                cwd=proj,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(pull.communicate(), timeout=60)
            except asyncio.TimeoutError:
                try:
                    pull.kill()
                except ProcessLookupError:
                    pass
                await pull.communicate()
                state.push_update_progress("error", "git pull timed out")
                return
            if pull.returncode != 0:
                state.push_update_progress("error", "git pull failed")
                return

            # Rebuild the in-tree frontend and stage website/dist into the
            # served static/dist (graceful no-op if no website/ or npm).
            # Done before pip install so the served bundle is refreshed even
            # if the package reinstall later hiccups.
            await _build_frontend(proj, state)

            # Reinstall the package so any new Python deps / entry points land.
            if not await _venv_pip_install(proj, state):
                return

            # Restart: save history + clean up sessions then exec the same process.
            logger.info("Update complete — saving history and cleaning up before restart")
            await _restart_gateway(state)
        except Exception:
            logger.exception("Update failed")
            state.push_update_progress("failed", "Update failed — check logs")
            state.push_refresh("update_failed")

    task = asyncio.create_task(_apply())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return web.json_response({"ok": True, "status": "updating"})


async def api_update_cancel(request: web.Request) -> web.Response:
    """POST /api/update/cancel — dismiss a stuck/failed update overlay."""
    state: DashboardState = request.app["state"]
    state.clear_update_progress()
    state.push_update_progress("failed", "Update cancelled by user")
    # Give clients a moment to receive the failed event, then clear
    await asyncio.sleep(0.2)
    state.clear_update_progress()
    return web.json_response({"ok": True})


async def api_update_simulate(request: web.Request) -> web.Response:
    """POST /api/update/simulate — walk through update steps with delays.

    For local testing only. Cycles through each progress step with a
    configurable delay (default 2s per step).
    """
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Simulate a pre-flight rejection (e.g. dirty working tree)
    if body.get("reject"):
        msg = body.get(
            "reject_message", "Working tree has uncommitted changes — commit or stash first"
        )
        return web.json_response({"error": msg}, status=409)

    delay = body.get("delay", 2)
    fail_at = body.get("fail_at", "")  # optional: step name to fail at

    async def _sim() -> None:
        steps = [
            ("pulling", "Pulling latest changes…"),
            ("building", "Installing package (pip)…"),
            ("building", "Building frontend (npm)…"),
            ("restarting", "Restarting server…"),
        ]
        for step, detail in steps:
            if fail_at and step == fail_at:
                state.push_update_progress("failed", f"Simulated failure at {step}")
                return
            state.push_update_progress(step, detail)
            await asyncio.sleep(delay)
        # Simulate completion — broadcast "done" so frontend clears the overlay
        state.push_update_progress("done", "Update complete")
        state.clear_update_progress()

    task = asyncio.create_task(_sim())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return web.json_response({"ok": True, "status": "simulating"})


# ── Logs SSE ──


_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


async def api_log_level(request: web.Request) -> web.Response:
    """POST /api/logs/level — change the kiro_crew logger level at runtime.

    Also persists the new level to config so it survives restarts.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    level_name = body.get("level", "").upper()
    if level_name not in _LOG_LEVELS:
        return web.json_response({"error": f"invalid level: {level_name}"}, status=400)
    root = logging.getLogger("kiro_crew")
    root.setLevel(_LOG_LEVELS[level_name])
    logger.info("Log level changed to %s via dashboard", level_name)

    # Persist to config so the level survives restarts.
    persisted = False
    try:
        cfg = KiroCrewConfig.load()
        cfg.agent.log_level = level_name
        cfg.save()
        persisted = True
    except Exception:
        logger.warning("Failed to persist log level to config", exc_info=True)

    return web.json_response({"ok": True, "level": level_name, "persisted": persisted})


async def api_log_level_get(request: web.Request) -> web.Response:
    """GET /api/logs/level — current kiro_crew logger level."""
    root = logging.getLogger("kiro_crew")
    return web.json_response({"level": logging.getLevelName(root.level)})


class _QueueLogHandler(logging.Handler):
    """Logging handler that enqueues formatted log entries for SSE delivery."""

    def __init__(self, queue: asyncio.Queue) -> None:  # type: ignore[type-arg]
        super().__init__()
        self._queue: asyncio.Queue[str] = queue  # type: ignore[type-arg]

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            data = json.dumps({"level": record.levelname, "msg": msg})
            self._queue.put_nowait(data)
        except Exception:
            pass


# ── Persistent log ring buffer ──

_LOG_RING_SIZE = 1000
_log_ring: collections.deque[str] = collections.deque(maxlen=_LOG_RING_SIZE)
_log_ring_handler_installed = False
_log_ring_handler: _RingLogHandler | None = None


async def _safe_ws_send(ws: web.WebSocketResponse, msg: str, state: DashboardState) -> None:
    """Send to WS, removing dead subscribers on failure."""
    try:
        await ws.send_str(msg)
    except Exception:
        state._ws_log_subscribers.discard(ws)


class _RingLogHandler(logging.Handler):
    """Always-on handler that keeps the last N log entries in a ring buffer.

    Also pushes log events to WebSocket log subscribers.
    """

    def __init__(
        self,
        ring: collections.deque[str],
        max_size: int = _LOG_RING_SIZE,
    ) -> None:
        super().__init__()
        self._ring = ring
        self._max = max_size
        self._state: DashboardState | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_state(self, state: DashboardState) -> None:
        """Attach DashboardState for WS log broadcasting."""
        self._state = state
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            data = json.dumps({"level": record.levelname, "msg": msg})
            self._ring.append(data)
            # Push to WS log subscribers (thread-safe via call_soon_threadsafe)
            if self._state and self._loop and self._state._ws_log_subscribers:
                ws_msg = json.dumps(
                    {"type": "log", "data": {"level": record.levelname, "msg": msg}}
                )
                for ws in list(self._state._ws_log_subscribers):
                    try:
                        self._loop.call_soon_threadsafe(
                            self._loop.create_task,
                            _safe_ws_send(ws, ws_msg, self._state),
                        )
                    except RuntimeError:
                        pass
        except Exception:
            pass


def install_log_ring_handler() -> _RingLogHandler | None:
    """Install the persistent ring buffer handler (call once at startup)."""
    global _log_ring_handler_installed, _log_ring_handler  # noqa: PLW0603
    if _log_ring_handler_installed:
        return _log_ring_handler
    _log_ring_handler_installed = True
    handler = _RingLogHandler(_log_ring, _LOG_RING_SIZE)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger("kiro_crew").addHandler(handler)
    _log_ring_handler = handler
    return handler


async def api_logs(request: web.Request) -> web.StreamResponse:
    """GET /api/logs — SSE stream of live log entries.

    Query params:
      - ``lines``: max ring-buffer entries to replay on connect (default 200, max 1000).

    On connect, replays the last *lines* log entries from the ring buffer
    so the client sees history even if the Logs page wasn't open.
    """
    try:
        lines_cap = min(max(int(request.query.get("lines", "200")), 1), _LOG_RING_SIZE)
    except (TypeError, ValueError):
        lines_cap = 200
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    try:
        await resp.prepare(request)
    except (ConnectionResetError, ClientConnectionResetError):
        return resp

    # Replay buffered history first (capped by ?lines=N)
    ring_snapshot = list(_log_ring)
    for data in ring_snapshot[-lines_cap:]:
        try:
            await resp.write(f"data: {data}\n\n".encode())
        except (ConnectionResetError, ClientConnectionResetError):
            return resp

    log_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
    handler = _QueueLogHandler(log_queue)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("kiro_crew")
    root.addHandler(handler)
    try:
        while not shutdown_event.is_set():
            # Drain any queued log entries
            while not log_queue.empty():
                try:
                    data = log_queue.get_nowait()
                    await resp.write(f"data: {data}\n\n".encode())
                except asyncio.QueueEmpty:
                    break

            # Wait for new entries or keepalive timeout
            try:
                data = await asyncio.wait_for(log_queue.get(), timeout=30)
                await resp.write(f"data: {data}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, ClientConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        root.removeHandler(handler)
    return resp


# ── Dashboard SSE ──


async def api_stream(request: web.Request) -> web.StreamResponse:
    """SSE endpoint — pushes status + notifications to each connected client.

    Each client gets its own notification queue (broadcast pattern) so
    multiple tabs all receive every notification.  Uses a simple sleep
    loop with short intervals to check for notifications — lightweight
    and avoids future leaks from asyncio.wait().
    """
    state: DashboardState = request.app["state"]
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    try:
        await resp.prepare(request)
    except (ConnectionResetError, ClientConnectionResetError):
        return resp

    # Register a per-client notification queue
    client_q = state.register_sse()
    try:
        while not shutdown_event.is_set():
            # Drain this client's notification/slots queue
            while not client_q.empty():
                try:
                    note = client_q.get_nowait()
                    msg_type = note.get("_type", "")
                    if msg_type == "slots":
                        payload = note["slots"]
                        await resp.write(f"event: slots\ndata: {payload}\n\n".encode())
                    elif msg_type == "slot_title":
                        payload = json.dumps({"key": note["key"], "title": note["title"]})
                        await resp.write(f"event: slot_title\ndata: {payload}\n\n".encode())
                    elif msg_type == "refresh":
                        await resp.write(f"event: refresh\ndata: {note['kinds']}\n\n".encode())
                    elif msg_type == "chat_message":
                        payload = json.dumps(
                            {
                                "slot": note["slot"],
                                "role": note["role"],
                                "content": note["content"],
                                "ts": note.get("ts", ""),
                            }
                        )
                        await resp.write(f"event: chat_message\ndata: {payload}\n\n".encode())
                    else:
                        payload = json.dumps(note)
                        await resp.write(f"event: notification\ndata: {payload}\n\n".encode())
                except asyncio.QueueEmpty:
                    break

            data = json.dumps(
                {
                    **state.status_snapshot(update_available=bool(_update_info.get("available"))),
                    "version": _local_version,
                }
            )
            await resp.write(f"event: dashboard\ndata: {data}\n\n".encode())

            # Sleep in short intervals, wake early if notification arrives
            for _ in range(_SSE_INTERVAL_SECS * 4):
                if shutdown_event.is_set() or not client_q.empty():
                    break
                await asyncio.sleep(0.25)
    except (ConnectionResetError, ClientConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        state.unregister_sse(client_q)
    return resp
