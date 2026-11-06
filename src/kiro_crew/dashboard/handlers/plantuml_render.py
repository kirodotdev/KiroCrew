"""``POST /api/plantuml/svg``: draw a ```plantuml fence with the host's own PlantUML.

The dashboard draws a ```plantuml fence as a diagram only when the gateway host
has a ``plantuml`` command on ``PATH`` (PlantUML runs on Java and lays most
diagrams out with Graphviz). Nothing is installed or downloaded, and the diagram
source never leaves the machine. Without a usable command, or when PlantUML
cannot draw the source, the fence stays the highlighted code block it always was.

The source is model- or document-authored, and PlantUML's preprocessor can read
files (``!include``) and URLs (``!includeurl``) while it draws, which would let a
reply fetch an address it chose the moment it is displayed. So:

* PlantUML runs under its ``SANDBOX`` security profile, which refuses file, URL
  and environment reads. Releases before :data:`_FIRST_SANDBOXED_RELEASE` ignore
  the profile or let a read past it, so an older ``plantuml`` is not run at all
  (its ``-version`` is read once).
* The process runs through the sandbox chokepoint in ``strict`` mode with a
  credential-scrubbed environment, as every agent-influenced spawn does, so the
  credential stores stay masked as a second layer. Where the operator runs
  without an OS sandbox, the agent's own commands are unconfined too.
* The executable goes through :func:`validate_provider_executable`, the same
  provenance check ``file_grep`` applies to ``rg``, so a ``plantuml`` planted in a
  tree the agent writes is not run.
* Only the owner's own dashboard session may render: a non-owner session, an app
  token and an internal caller are refused.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.dashboard.handlers._shared import read_bounded_json, require_owner_dashboard_request
from kiro_crew.github_runner import validate_provider_executable
from kiro_crew.sandbox import SandboxUnavailableError, popen_limited, sandboxed_spawn_argv

logger = logging.getLogger(__name__)

#: The longest one PlantUML run may take. A cold JVM start takes a second or two
#: and a large diagram's Graphviz layout a few more; past this the tree is killed.
_RENDER_TIMEOUT_SECS = 20

#: ``(year, n)`` of PlantUML 1.<year>.<n>: the release with the access-control and
#: SSRF fixes to the profile (CVE-2023-3431, CVE-2023-3432), and the first whose
#: ``SANDBOX`` profile refuses environment reads as well as file and URL includes
#: (checked against the published jars: 1.2023.8 still reads the environment,
#: 1.2020.10 also fetches files and URLs).
_FIRST_SANDBOXED_RELEASE = (2023, 9)

#: What :func:`_render_svg` answers in place of an SVG.
_UNAVAILABLE = "unavailable"
_FAILED = "failed"

#: ``(executable, honours the profile)`` for the last executable checked.
_release_verdict: tuple[str, bool] | None = None


def _plantuml_executable() -> str | None:
    """The vetted ``plantuml`` on ``PATH`` as an absolute path, or ``None``.

    BLOCKING (``which`` and the provenance walk both stat).
    """
    found = shutil.which("plantuml")
    if not found:
        return None
    try:
        return validate_provider_executable(found)
    except ValueError as exc:
        logger.warning("plantuml_render: refusing plantuml at %s: %s", found, exc)
        return None


def _run_plantuml(executable: str, args: list[str], stdin: bytes) -> tuple[int, bytes] | None:
    """One sandboxed PlantUML run: ``(exit code, stdout)``, or ``None`` with no sandbox.

    A run past the deadline is killed with its tree and reported as exit ``-1``.
    BLOCKING: run it in a worker thread.
    """
    env = {**os.environ, "PLANTUML_SECURITY_PROFILE": "SANDBOX"}
    try:
        argv, env, cleanup = sandboxed_spawn_argv([executable, *args], "strict", env=env)
    except SandboxUnavailableError:
        return None
    try:
        proc = popen_limited(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
        try:
            out, _ = proc.communicate(stdin, timeout=_RENDER_TIMEOUT_SECS)
        except subprocess.TimeoutExpired:
            platform_compat.kill_popen_tree(proc)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("plantuml_render: plantuml did not exit after SIGKILL")
            return -1, b""
    except OSError as exc:
        logger.debug("plantuml_render: spawn failed: %s", exc)
        return -1, b""
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass
    return proc.returncode, out


def _honours_profile(executable: str) -> bool:
    """Whether *executable* is a PlantUML release that applies the ``SANDBOX`` profile.

    Read from ``plantuml -version`` (``PlantUML version 1.2026.9 ...``) once per
    executable; anything unreadable counts as too old. BLOCKING.
    """
    global _release_verdict
    if _release_verdict is not None and _release_verdict[0] == executable:
        return _release_verdict[1]
    ran = _run_plantuml(executable, ["-version"], b"")
    if ran is None or ran[0] != 0:
        return False  # no sandbox, or no answer this time: not a verdict about the release
    words = ran[1].decode("utf-8", errors="replace").split()
    parts = words[2].split(".") if words[:2] == ["PlantUML", "version"] and len(words) > 2 else []
    release = (
        (int(parts[1]), int(parts[2]))
        if len(parts) == 3 and all(p.isdigit() for p in parts)
        else None
    )
    honours = release is not None and release >= _FIRST_SANDBOXED_RELEASE
    if not honours:
        logger.info(
            "plantuml_render: not using %s, older than PlantUML 1.2023.9 or unreadable", executable
        )
    _release_verdict = (executable, honours)
    return honours


def _render_svg(source: str) -> tuple[str | None, str]:
    """Draw *source* as SVG: ``(None, svg)``, or ``(_UNAVAILABLE | _FAILED, "")``.

    BLOCKING: run it in a worker thread.
    """
    executable = _plantuml_executable()
    if executable is None or not _honours_profile(executable):
        return _UNAVAILABLE, ""
    ran = _run_plantuml(executable, ["-tsvg", "-pipe", "-charset", "UTF-8"], source.encode("utf-8"))
    if ran is None:
        return _UNAVAILABLE, ""
    # A diagram PlantUML cannot parse exits non-zero with an error picture on
    # stdout; the fence keeps its source view instead.
    if ran[0] != 0:
        return _FAILED, ""
    return None, ran[1].decode("utf-8", errors="replace")


async def api_plantuml_svg(request: web.Request) -> web.Response:
    """POST /api/plantuml/svg ``{"source"}`` -> ``{"svg"}``.

    404 ``plantuml_unavailable`` when the host has no usable renderer, 422
    ``plantuml_failed`` when PlantUML could not draw the source.
    """
    denied = await require_owner_dashboard_request(request, "plantuml_render")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    source = body.get("source")
    if not isinstance(source, str) or not source.strip():
        return web.json_response(
            {"error": "source is required", "code": "source_required"}, status=400
        )
    missing, svg = await asyncio.to_thread(_render_svg, source)
    if missing == _UNAVAILABLE:
        return web.json_response(
            {"error": "PlantUML is not available on this host", "code": "plantuml_unavailable"},
            status=404,
        )
    if missing == _FAILED:
        return web.json_response(
            {"error": "PlantUML could not draw this diagram", "code": "plantuml_failed"},
            status=422,
        )
    return web.json_response({"svg": svg})
