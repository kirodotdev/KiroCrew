"""The path-probe chokepoint: ``_run_path_probe`` runs a blocking request-path call on a bounded pool, or refuses with a busy answer.

It is the one sanctioned route for the blocking filesystem work of every file route the facade serves.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Callable, NamedTuple

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _CONTROL_CHARS_RE,
        _PATH_PROBE_ADMIT_TIMEOUT_SECS,
        _PATH_PROBE_EXEC_CEILING_SECS,
        _ProbeT,
        _sel,
        executors,
    )


def _validate_dashboard_path(raw: str) -> str | None:
    """Validate a file path through hooks.py enforcement layer.

    Refuses a control character in the RAW path first. ``FILE_READ_SCHEMA``
    declares that class but cannot enforce it: ``validate_tool_args`` matches the
    *sanitized* copy of the value, from which ``strip_hidden_unicode`` has already
    removed every control character but CR, LF and TAB, while the raw string is
    what travels on. So the class is unobservable at the schema and has to be
    refused here.

    It is refused HERE rather than inside ``validate_file_path`` because that is a
    shared chokepoint whose other callers deliberately handle such a name -- a
    diagnostic that enumerates an agent-writeable directory reports on a
    control-character-named file and escapes the name for display, and refusing it
    there would suppress that report. The class is a property of what this
    boundary accepts from a caller, not of what a path can be.

    What it buys at this boundary: the raw path is recorded in the request's audit
    entry and echoed in diagnostics, so CR or LF forges a line and ESC or an 8-bit
    C1 (U+009B CSI, U+0085 NEL) is a terminal escape. No file a dashboard caller
    means to open is named with one, so the refusal costs nothing legitimate --
    unlike the punctuation an allowlist omits, which is the defect this gate's
    denylist exists to stop causing.

    Blocking: ``validate_file_path`` canonicalizes with ``realpath`` and, on
    Windows, walks the path's ancestors with one ``lstat`` each. Callers reach it
    through :func:`_probe_request_path` on a worker thread rather than calling it
    from an ``async def`` body.
    """
    from kiro_crew.hooks import validate_file_path  # noqa: F811

    if _CONTROL_CHARS_RE.search(raw):
        return None
    return validate_file_path(raw)


class _PathProbeBusy(Exception):
    """No worker on the chosen pool freed up within the admission window."""


# Named by the path callers import it from: a traceback or an error chain renders a raised
# class as ``module.qualname``, so this module's name must not leak into it.
_PathProbeBusy.__module__ = "kiro_crew.dashboard.handlers.files"


async def _run_path_probe(
    fn: Callable[..., _ProbeT], /, *args: object, transfer: bool = False
) -> _ProbeT:
    """Run a blocking request-path call on a dedicated bounded pool, or refuse.

    The only sanctioned route for filesystem work on a caller-supplied path in
    this module -- never ``asyncio.to_thread``. That is the loop's default
    executor, shared by MCP, crons and the rest of the dashboard: a thread wedged
    in an uninterruptible ``stat`` on a dead mount never returns, so a caller
    repeatedly naming one would retire a shared worker per request until every
    unrelated ``to_thread`` user queued behind them. On its own pool the same
    caller exhausts that pool and nothing else.

    ``transfer`` picks the pool. Probes -- validation and stats, milliseconds
    when healthy -- go to :func:`executors.path_probe_executor`; calls that hold
    a worker for the length of a bounded transfer (the open-and-check envelope's
    full read, the search walk, the browse listings, the document parses) go to
    :func:`executors.path_transfer_executor`, so a burst of large downloads
    cannot starve validation behind them.

    Admission and the refusal are :func:`executors.run_in_cron_pool`'s: it
    submits, waits at most ``_PATH_PROBE_ADMIT_TIMEOUT_SECS`` for a worker to
    CLAIM the call, and if none does it cancels the still-queued call and raises
    ``CronQueueTimeout`` -- which becomes :class:`_PathProbeBusy` here and a 503
    at every endpoint via :func:`_probe_busy_response`. A call a worker claims at
    the deadline is not refused: it is running, and a thread cannot be taken
    back. Capacity accounting is the pool's own worker count, so a client that
    gives up on a wedged call cannot make the pool believe a slot is free while
    the thread is still parked; nothing here has to track that.
    """
    pool = executors.path_transfer_executor() if transfer else executors.path_probe_executor()
    try:
        return await executors.run_in_cron_pool(
            fn,
            *args,
            timeout=_PATH_PROBE_EXEC_CEILING_SECS,
            queue_timeout=_PATH_PROBE_ADMIT_TIMEOUT_SECS,
            executor=pool,
        )
    except executors.CronQueueTimeout:
        raise _PathProbeBusy() from None


def _probe_busy_response(
    *,
    resource: str,
    tool_name: str = "",
    operation: str = "",
    caller: str = "dashboard",
    session_key: str = "dashboard",
    source: str = "",
) -> web.Response:
    """The one answer for a refused probe: 503, coded, audited.

    Pass ``tool_name`` for endpoints that audit through ``log_tool_invocation``
    and ``operation`` for those that use ``log_api_access``, matching whichever
    the endpoint's other outcomes already use. One ``json_response`` site, so the
    error-code contract counts every adopter as one.
    """
    if tool_name:
        _sel().log_tool_invocation(
            session_key=session_key,
            source=source,
            tool_name=tool_name,
            outcome="failure",
            error="path_probe_busy",
            resources=resource,
        )
    else:
        _sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="failure",
            resources=resource,
            error="path_probe_busy",
        )
    return web.json_response(
        {"error": "file system probe capacity exhausted; retry shortly", "code": "path_probe_busy"},
        status=503,
    )


class _PathProbe(NamedTuple):
    """What one off-loop filesystem probe of a request path found.

    ``path`` is the validated canonical path, or ``""`` when validation refused
    it -- which the endpoint answers as "invalid or forbidden path", exactly as
    a ``None`` from :func:`_validate_dashboard_path` did. ``is_file`` and
    ``is_dir`` are the stat answers for that path, both ``False`` on a refusal so
    a caller that only reads them still takes its not-found branch.
    """

    path: str
    is_file: bool
    is_dir: bool


def _probe_request_path(raw: str) -> _PathProbe:
    """Validate and stat a request path -- ONE blocking hop, off the loop.

    Groups the filesystem syscalls an endpoint needs before it can answer:
    ``validate_file_path``'s ``realpath`` plus linked-ancestor walk, and the
    ``isfile`` / ``isdir`` probe. One helper means one pool hop per request, and
    it means these cannot be reintroduced on the event loop a call at a time.

    Blocking by design, and unboundedly so: a path whose mount is unresponsive
    (a disconnected network share, a wedged FUSE filesystem) makes ``realpath``
    and ``stat`` block for however long the kernel takes, and those syscalls are
    uninterruptible. Run on the event loop, ONE such request stalls every
    endpoint in the process -- dashboard, tunnel, MCP and crons alike -- and a
    stall outlasting ``dashboard.loop_stall_exit_after_secs`` makes the loop
    watchdog kill the gateway. Which mount the path lands on is the caller's
    choice, not this process's.

    A ``ValueError`` from a malformed path (an embedded NUL makes ``realpath``
    raise) is deliberately NOT caught: it propagates exactly as it did when this
    ran inline, so no caller's answer for that input changes here.

    This probe is a verdict, not a handle. A caller that goes on to OPEN the path
    must not re-derive the descriptor from this answer in a second hop -- see
    :func:`_read_request_path` for why.

    Always reached through :func:`_run_path_probe`, never ``asyncio.to_thread``:
    a thread wedged in an uninterruptible ``stat`` never returns, so a caller
    repeatedly naming one dead mount would otherwise retire a default-executor
    worker per request until the rest of the gateway starves behind them. The
    dedicated pool caps the wedged threads and then refuses with 503, and no other
    ``to_thread`` user ever queues behind a probe.
    """
    path = _validate_dashboard_path(raw)
    if not path:
        return _PathProbe("", False, False)
    return _PathProbe(path, os.path.isfile(path), os.path.isdir(path))
