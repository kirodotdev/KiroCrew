"""Start identity: whether a process recorded in a generation marker is still that process.

A PID alone cannot answer it. PIDs are reused, so a marker records the process START identity
next to its PID, and a verdict compares the two. Both readers of that verdict act on it
destructively -- ``warm._recorded_runtime_is_dead`` releases a generation tree and
``warm._scavenge_warm_generation_dirs`` removes one -- so the verdict is tri-state and only a
POSITIVE proof of death or reuse reads as ``False``. Every other shape reads as ``None``,
which every caller turns into "keep the tree".

The marker's schema, its writer and its reader stay with the generation lifecycle in the facade;
this module owns only the identity those records carry and the comparison that judges it. Every
platform call goes through the :mod:`kiro_crew.platform_compat` module attribute, so a patch on
``warm.platform_compat`` -- the same module object -- still reaches it.
"""

from __future__ import annotations

import re

from kiro_crew import platform_compat


def _marker_process_start_id(pid: int) -> str | None:
    """Start identity for a persisted ownership marker.

    The identity comes from :func:`platform_compat.get_process_start_id`, the
    routine this repository already uses wherever a start identity is WRITTEN
    DOWN and compared back later (``mcp_gateway.claim``, ``session_pid``,
    ``metrics.sessions``). It answers in-process on every platform it covers --
    procfs field 22 on Linux, ``libproc`` microsecond start on macOS, the
    creation ``FILETIME`` on Windows -- and never emits whitespace or ``:``.

    It declines on other POSIX hosts, and there :func:`platform_compat.process_start_time`
    still answers on Windows only: the process creation ``FILETIME`` read through a
    query-only handle -- a machine integer at 100-ns resolution with no locale or
    timezone in it, the same value :func:`get_process_start_id` returns there.
    That leg is kept exactly as it is.

    What is NOT consulted is that routine's remaining POSIX leg, ``ps
    -o lstart=``: 1-second, locale- and TZ-rendered, and documented as safe
    precisely because "a format or resolution drift can only make the guard
    decline to act". That is a KILL-guard contract, where a mismatch means do
    nothing. Both readers of this value act ON a mismatch instead --
    :func:`~kiro_crew.connections.warm._recorded_runtime_is_dead` releases the tree and
    :func:`~kiro_crew.connections.warm._scavenge_warm_generation_dirs` ``rmtree``s it -- so a drifted render
    deletes the cwd and private agent scope of a process that is still running.
    Two gateways sharing one data home need only differ in ``TZ`` or ``LC_TIME``
    to render the same instant differently, and 1-second granularity cannot
    separate two processes that started in the same second.

    Returns ``None`` where the identity is unknown. Per those routines' own
    contract a ``None`` must NOT be read as a mismatch: callers fall back to
    "unproved", which keeps the tree.
    """
    start_id = platform_compat.get_process_start_id(pid)
    if start_id is not None:
        return start_id
    if not platform_compat.IS_WINDOWS:
        return None  # every remaining POSIX leg is the locale-rendered ``ps`` output
    try:
        return platform_compat.process_start_time(pid)
    except Exception:
        return None


#: Shape of a start identity in the CURRENT representation: the digits of a
#: Linux jiffy count or a Windows creation ``FILETIME``, or macOS's
#: ``"<seconds>.<microseconds>"``. Deliberately an ALLOWLIST of what this build
#: writes rather than a denylist of what older ones did: the retired
#: representation was ``ps -o lstart=``, whose exact text is locale- and
#: TZ-dependent, so no property of it can be relied on.
_CURRENT_START_ID_RE = re.compile(r"\A\d+(?:\.\d+)?\Z")


def _start_ids_comparable(recorded: str, current: str) -> bool:
    """May *recorded* and *current* be compared as the same kind of identity?

    A start identity is only evidence of PID reuse when both sides were
    produced by the same representation. A marker written before this build
    recorded a ``ps``-rendered token, which can never equal the value
    :func:`_marker_process_start_id` reads now -- and every caller acts on a
    mismatch DESTRUCTIVELY (:func:`~kiro_crew.connections.warm._recorded_runtime_is_dead` releases the
    tree; :func:`~kiro_crew.connections.warm._scavenge_warm_generation_dirs` removes it). An overlapping
    restart across that upgrade is exactly the case the scavenger documents a
    live process as protecting.

    So a value that is not in the current representation is "unknown", not
    "different", and the caller keeps the tree. It is NOT converted: the
    timezone and locale its writer rendered it under are not recoverable, and
    guessing them would re-introduce the misjudgement this exists to prevent.
    """
    return bool(_CURRENT_START_ID_RE.match(recorded) and _CURRENT_START_ID_RE.match(current))


def _process_identity_live(pid: int, started: str) -> bool | None:
    """Tri-state PID identity: live, dead/reused, or unprovable.

    ``False`` is proof of death -- the PID is gone, or it names a live process
    whose start identity positively differs from the recorded one. ``None`` is
    every other inconclusive shape (no recorded identity, unreadable current
    identity, unproven liveness), and every caller reads it as "keep the tree".

    "Differs" requires the two values to be the same KIND of identity: a marker
    written before this build recorded a locale-rendered ``ps`` token that can
    never equal what is read now, and calling that a mismatch would delete the
    cwd and private scope of a process still running across the upgrade. See
    :func:`_start_ids_comparable`.
    """
    if pid <= 0 or not started:
        return None
    liveness = platform_compat.pid_liveness(pid)
    if liveness == platform_compat.PID_DEAD:
        return False
    current = _marker_process_start_id(pid)
    if current is None:
        return None
    if current != started:
        if not _start_ids_comparable(started, current):
            return None  # legacy marker: unknown rather than different
        return False
    if liveness in (platform_compat.PID_ALIVE, platform_compat.PID_UNSIGNALABLE):
        return True
    return None
