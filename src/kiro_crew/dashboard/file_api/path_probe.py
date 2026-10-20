"""The path-probe chokepoint: ``_run_path_probe`` runs a blocking request-path call on a bounded pool, or refuses with a busy answer.

It is the one sanctioned route for the blocking filesystem work of every file route the facade serves.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Callable, NamedTuple

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _CONTROL_CHARS_RE,
        _PATH_PROBE_ADMIT_TIMEOUT_SECS,
        _PATH_PROBE_EXEC_CEILING_SECS,
        _PATH_PROBE_STALL_SECS,
        _PATH_PROBE_STUCK_TO_REFUSE,
        _path_probe_inflight,
        _path_probe_inflight_lock,
        _ProbeT,
        _sel,
        _stall_prefix,
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


def _path_probe_stall_key(args: tuple[object, ...]) -> str:
    """The stall prefix a probe is charged to, or ``""`` for no key.

    The key is the probe's FIRST positional argument when that is a non-empty
    absolute path string: every probe-pool caller that names a request path
    passes it first. A call whose first argument is anything else (a closure, a
    list of paths, a config) gets no key and keeps the plain bounded-pool
    behaviour. So does a relative path: the probe resolves it against a project
    directory, not the gateway's working directory, so ``abspath`` would charge
    the wrong mount.

    Lexical on purpose, like :func:`_stall_prefix` itself: the key is computed
    on the event loop before deciding whether to touch the filesystem at all, so
    it must not stat the mount it may be about to refuse, nor ask the password
    database. Only ``~`` and ``~/...`` are expanded, from the environment's home
    variable; ``~name`` (a user lookup that can block on a directory service)
    gets no key, and the worker expands it as before.

    Under a home directory the key is one component deeper than
    :func:`_stall_prefix`: ``/home/<user>/mnt/x`` keys to ``/home/<user>/mnt``,
    ``C:\\Users\\<user>\\ws\\f`` to ``C:\\Users\\<user>\\ws``. A home holds the
    workspace, the checkout and any FUSE or sshfs mount the user made, so a key
    ending at the home would let two probes stuck on ``~/mnt/x`` refuse every
    file route under the home, the workspace included. A key is a home when its
    last component sits directly in a component spelled ``home`` or ``Users``,
    the containers :func:`_stall_prefix` itself recognises; a UNC share root is
    never one. The home itself, and a path that is not under a home, keep the
    plain :func:`_stall_prefix` key. This applies to this gate only: the
    sensitive-path resolver's cooldown keeps its own key.
    """
    first = args[0] if args else None
    if not isinstance(first, str) or not first:
        return ""
    if first.startswith("~"):
        home = os.environ.get("USERPROFILE" if os.name == "nt" else "HOME", "")
        if not home or (len(first) > 1 and first[1] not in ("/", os.sep)):
            return ""
        first = home + first[1:]
    if not os.path.isabs(first):
        return ""
    try:
        abs_path = os.path.abspath(first)
        base = _stall_prefix(abs_path)
    except ValueError:
        return ""
    drive, rest = os.path.splitdrive(base)
    if drive[:1] in ("\\", "/") and drive[1:2] in ("\\", "/"):
        return base  # a UNC share root is a mount point, never a home
    parts = rest.split(os.sep)
    if len(parts) < 3 or not parts[-1] or parts[-2] not in ("home", "Users"):
        return base
    below = abs_path[len(base) :]
    if not below.startswith(os.sep):
        return base
    child = below.lstrip(os.sep).split(os.sep, 1)[0]
    return base + os.sep + child if child else base


def _path_probe_prefix_wedged(key: str) -> bool:
    """True while ``_PATH_PROBE_STUCK_TO_REFUSE`` probes under *key* have each run
    longer than ``_PATH_PROBE_STALL_SECS``.

    One stuck probe is not enough: a single request through a stray link to a
    dead share costs one worker and refuses nothing, exactly as without this
    check. A second one under the same prefix means callers keep asking for a
    mount that does not answer, and from then on the prefix is refused.

    Reads only the in-flight map, never the filesystem. The answer clears by
    itself the moment a stuck thread returns, so a mount that recovers is
    served again on the next request, with no backoff to wait out.
    """
    now = time.monotonic()
    with _path_probe_inflight_lock:
        starts = _path_probe_inflight.get(key)
        if not starts:
            return False
        stuck = sum(1 for start in starts.values() if now - start >= _PATH_PROBE_STALL_SECS)
        return stuck >= _PATH_PROBE_STUCK_TO_REFUSE


def _tracked_path_probe(key: str, fn: Callable[..., _ProbeT], /, *args: object) -> _ProbeT:
    """Run *fn* on the worker thread, listed in the in-flight map under *key* meanwhile.

    The start time is taken here, on the worker, so time spent queued for a
    worker never counts as time spent wedged.
    """
    token = object()
    with _path_probe_inflight_lock:
        _path_probe_inflight.setdefault(key, {})[token] = time.monotonic()
    try:
        return fn(*args)
    finally:
        with _path_probe_inflight_lock:
            starts = _path_probe_inflight.get(key, {})
            starts.pop(token, None)
            if not starts:
                _path_probe_inflight.pop(key, None)


async def _run_path_probe(
    fn: Callable[..., _ProbeT],
    /,
    *args: object,
    transfer: bool = False,
    stall_path: str | None = None,
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

    A probe also learns which MOUNT is dead. A probe whose first argument names
    a path (:func:`_path_probe_stall_key`) is keyed to that path's stall prefix,
    the same lexical key the sensitive-path resolver's cooldown uses, except
    that under a home directory the key goes one component deeper, so a dead
    mount inside the home (``~/mnt/nas``) does not refuse the workspace beside
    it. While
    ``_PATH_PROBE_STUCK_TO_REFUSE`` probes under the same prefix have run past
    ``_PATH_PROBE_STALL_SECS``, the new one is refused with the same busy answer
    before it takes a worker. So a caller hammering one dead mount stops taking
    workers once two probes there are stuck, rather than retiring one per
    request until the pool is gone, and paths under other prefixes keep being
    served. Probes that arrive together, before any of them is past the budget,
    are still bounded only by the pool. A transfer naming a stuck prefix is refused the same way,
    but a transfer never marks a prefix stuck: it legitimately runs long.

    ``stall_path`` names the path the call touches when it is not the first
    argument: a closure, a config-first helper, a root the caller computed. It
    is keyed by the same lexical rule, and ``""`` means no key. Without it the
    first argument is the key, as before.
    """
    key = _path_probe_stall_key(args if stall_path is None else (stall_path,))
    if key and _path_probe_prefix_wedged(key):
        raise _PathProbeBusy()
    pool = executors.path_transfer_executor() if transfer else executors.path_probe_executor()
    tracked = bool(key) and not transfer
    call_args: tuple[object, ...] = (key, fn, *args) if tracked else args
    call_fn: Callable[..., _ProbeT] = _tracked_path_probe if tracked else fn
    try:
        return await executors.run_in_cron_pool(
            call_fn,
            *call_args,
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
