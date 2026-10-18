"""The routes that drive the host desktop: ``POST /api/reveal``, the native file picker ``POST /api/upload`` and ``POST /api/screenshot``."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _SCREENSHOT_DIR,
        _PathProbeBusy,
        _probe_busy_response,
        _run_path_probe,
        _sel,
        data_home,
        is_direct_local_request,
        is_sensitive_path,
        platform_compat,
        read_bounded_json,
    )


async def api_reveal_path(request: web.Request) -> web.Response:
    """POST /api/reveal — reveal a file/folder in Finder or open with default app."""
    # Default cap: the body is a path and an action flag.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    path = body.get("path", "")
    action = body.get("action", "reveal")  # "reveal" or "open"
    if not path or ".." in Path(path).parts:
        return web.json_response({"error": "invalid path"}, status=400)
    if is_sensitive_path(path):
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="reveal_path",
            outcome="denied",
            error="sensitive_path",
            resources=path,
            metadata={"action": action},
        )
        return web.json_response({"error": "access denied"}, status=403)
    # Gate: only spawn native openers from direct-local requests. Remote/tunneled
    # callers get the copy-to-clipboard fallback — spawning Finder on a machine
    # the user is not looking at is surprising and useless.
    if not is_direct_local_request(request):
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="reveal_path",
            outcome="denied",
            error="remote_request",
            resources=path,
            metadata={"action": action},
        )
        # Degrade to a clipboard copy: `copy` is the path to write. The remote
        # cause is recorded in the SEL audit above (error="remote_request"); the
        # response body carries no path, host, or exception detail beyond `copy`.
        return web.json_response({"ok": True, "copy": path})
    # Every ALLOWED outcome leaves through the single audited return below —
    # including the clipboard answer, which is a granted decision whose host
    # simply had no file manager. An early return here would drop that decision
    # from the SEL log, so the branches record what happened instead of exiting.
    #
    # Both spawns live in platform_compat, which owns the safety properties:
    # absolute trusted launchers rather than bare argv names, a folder rather
    # than the file on the platforms where handing a file to the file manager
    # would launch it, and Windows refused outright for the launch-by-association
    # verb. They answer False both for a host with no launcher and for one that
    # refuses to start, and either way this degrades to the clipboard rather than
    # failing a click in the file viewer.
    if action == "open":

        def _stat_then_launch() -> tuple[bool, bool]:
            """The regular-file check AND the launch, in one worker transaction.

            Both belong off the loop: the stat is unbounded on a caller-supplied
            path, and the launch spawns a process. They must not be SPLIT across
            an ``await``, though. The launcher takes a path, not the descriptor
            this stat looked at, so the two calls are a check-then-use pair; an
            ``await`` between them is a scheduler yield inside that window, which
            is long enough for the path to be replaced with a symlink the
            sensitive-path gate above already refused. The launcher follows it and
            opens the substituted target in the user's default application.

            Keeping them in one transaction holds the window to what it is when
            the two run back-to-back: no suspension point, and the GIL not
            released between them. Closing it entirely needs a launcher that
            takes a descriptor, which no platform's open-by-association verb
            does, so this is the narrow form rather than the closed form.
            """
            if not os.path.isfile(path):
                return (False, False)
            return (True, platform_compat.open_with_default_app(path))

        try:
            is_regular_file, launched = await _run_path_probe(_stat_then_launch)
        except _PathProbeBusy:
            return _probe_busy_response(
                resource=path, tool_name="reveal_path", session_key="api", source="api"
            )
        if not is_regular_file:
            return web.json_response({"error": "not a regular file"}, status=400)
        copied = not launched
    else:
        # Off-loop: the reveal spawns a file-manager process. No stat pairs with
        # it, so there is no check-then-use window to hold here.
        copied = not await asyncio.to_thread(platform_compat.reveal_in_file_manager, path)
    _sel().log_tool_invocation(
        session_key="api",
        source="api",
        tool_name="reveal_path",
        outcome="success",
        resources=path,
        metadata={"action": action},
    )
    # A local grant whose host had no working file manager degrades to the
    # clipboard; `copy` is the path to write.
    if copied:
        return web.json_response({"ok": True, "copy": path})
    return web.json_response({"ok": True})


async def api_upload(request: web.Request) -> web.Response:
    """POST /api/upload — open native file picker and return selected paths.

    The dialog binary is resolved from the fixed system directories rather than
    PATH. A gateway's PATH can lead with an agent-writable directory (a worktree
    venv's ``bin``, ``~/.local/bin``), so a bare argv name lets a planted shim
    run with the gateway's environment and outside the sandbox. ``None`` is a
    refusal, never a fallback to the bare name — that would reinstate the hazard.
    """
    if sys.platform != "darwin":
        return web.json_response({"error": "File picker is only available on macOS"}, status=400)

    osascript = platform_compat.trusted_system_bin("osascript")
    if osascript is None:
        return web.json_response(
            {
                "error": "File picker is unavailable on this system",
                "code": "file_picker_unavailable",
            },
            status=501,
        )

    proc = await asyncio.create_subprocess_exec(
        osascript,
        "-e",
        "set f to choose file with multiple selections allowed\n"
        'set out to ""\n'
        "repeat with p in f\n"
        "  set out to out & POSIX path of p & linefeed\n"
        "end repeat\n"
        "return out",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        return web.json_response({"error": "Finder dialog timed out"}, status=504)
    paths = [ln for ln in stdout.decode("utf-8", errors="replace").strip().splitlines() if ln]

    if not paths:
        return web.json_response({"paths": []})
    return web.json_response({"paths": paths})


def _screenshot_dir() -> Path:
    """Screenshots directory, resolved against the live data home."""
    return _SCREENSHOT_DIR if _SCREENSHOT_DIR is not None else data_home() / "screenshots"


async def api_screenshot(request: web.Request) -> web.Response:
    """POST /api/screenshot — capture screen region and return file path.

    macOS only — uses built-in screencapture. Linux cloud desktops
    (AL2, headless) don't have a display server so this is unavailable.

    The capture binary is resolved from the fixed system directories rather than
    PATH, for the reason :func:`api_upload` states, and an unresolvable one is a
    refusal rather than a bare-name spawn.
    """
    if sys.platform != "darwin":
        return web.json_response({"error": "Screenshot is only available on macOS"}, status=400)

    screencapture = platform_compat.trusted_system_bin("screencapture")
    if screencapture is None:
        return web.json_response(
            {
                "error": "Screenshot is unavailable on this system",
                "code": "screenshot_unavailable",
            },
            status=501,
        )

    screenshot_dir = _screenshot_dir()
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    dest = screenshot_dir / f"screenshot_{ts}.png"

    proc = await asyncio.create_subprocess_exec(
        screencapture,
        "-i",
        str(dest),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=120)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return web.json_response({"error": "screenshot timed out"}, status=504)
    if not dest.exists():
        return web.json_response({"path": ""})  # user cancelled
    return web.json_response({"path": str(dest)})
