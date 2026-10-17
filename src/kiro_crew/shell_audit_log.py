"""Bound the bundled shell-audit hook's ``audit.log`` from the gateway side.

The default ``postToolUse`` hook in ``config/defaults.json`` records every
``execute_bash`` call as a single line -- a UTC stamp, then `` BASH: ``, then
the hook-event JSON kiro-cli hands it on stdin (the tool call -- its command
and, on this event, its result) with every CR and LF stripped -- appended to
``${KIROCREW_HOME:-$HOME/.kiro/crew}/audit.log``.
The command is one shell append and bounds nothing, so on a default install
the file only grows: measured at 4.4 MB over about five weeks of ordinary
use, as a single file with no sibling generation.

The bound lives HERE rather than in the hook because the hook is the wrong
place for it in both of its possible shapes. Rotating from inside the shell
command needs a portable size check (``stat`` flags differ between the Linux
and macOS shells the hook runs under) and a roll that is racy under concurrent
appends; routing the hook through a ``kirocrew`` helper instead costs a Python
interpreter start on every shell tool call, through an entry point the hook
has to find on PATH. Either shape changes the shipped command, which the agent
spec rebuild copies into every install. A gateway-side sweep changes nothing
the hook does: the command stays byte-identical, a user who authored their own
hook is untouched, and the one thing the file needed -- a size bound -- is
applied by the process that owns the data home anyway.

:func:`rotate_shell_audit_log` is that sweep step. It reuses
:func:`kiro_crew.jsonl_util.rotate_jsonl_at`, whose contract is exactly what a
file with a foreign writer needs: one ``.1`` generation replacing any older one
(so disk use stays at about twice the cap), a non-blocking try-lock so two
rotators cannot both rotate, a rename rather than a truncate or a copy (so the
hook's ``>>`` append keeps working and no record is lost mid-write), and a
promise never to raise. The session cleanup loop
(:class:`kiro_crew.session_cleanup.SessionCleanup`) calls it on every tick,
the first within ``MAX_TICK_INTERVAL_SECS`` of the loop starting, which is
what bounds the overshoot: the live file can exceed the cap by at most one
tick's worth of shell activity before the next sweep moves it aside, and an
install that already carries an oversized file is bounded by its first tick.
The two-generation bound is a steady-state one: that first rotation moves the
whole oversized file aside as ``.1``, whatever its size, and the next rotation
replaces it, so the bound holds from the second rotation on.

The hook runs inside kiro-cli, whichever process launched it, so the gateway is
not the only process on whose watch the file grows: standalone ``kirocrew chat``
spawns the same agent with the same hook and never starts a cleanup loop. An
install that only ever runs the standalone chat would never rotate the file, so
:func:`kiro_crew.cli_chat._chat` calls this sweep once at every chat start,
before the backend that will append is spawned; on such an install the live
file overshoots the cap by at most one chat session's shell activity. A kiro-cli
started by hand with the agent spec writes the file too; the next gateway tick
or chat start on that data home bounds it.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from kiro_crew.jsonl_util import rotate_jsonl_at

logger = logging.getLogger(__name__)

#: The file the bundled hook appends to, relative to the data home. Spelled
#: once here and once in ``config/defaults.json``; the two must agree.
SHELL_AUDIT_LOG_NAME = "audit.log"

#: Rotate ``audit.log`` aside once it reaches this size. At the measured rate
#: (4.4 MB in five weeks on one install) 8 MiB is about nine and a half weeks
#: of shell history in the live file before the first rotation, and with the
#: one kept generation roughly four months on disk. The steady-state bound on
#: disk is this cap plus one cleanup-tick interval (at most
#: ``SessionCleanup.MAX_TICK_INTERVAL_SECS``) of shell activity of overshoot on
#: the live file, times the two generations kept.
SHELL_AUDIT_LOG_MAX_BYTES = 8 * 1024 * 1024

#: A rotation that cannot complete warns at most once per this many seconds per
#: data home, not once per attempt: the cleanup loop retries every tick (60-300
#: s) for the gateway's whole life, and a permanently blocked ``.1`` slot would
#: otherwise repeat the same line hundreds of times a day. The same interval the
#: cleanup loop uses for its own repeating refusals
#: (``SessionCleanup.PROBE_FAILURE_WARN_INTERVAL_SECS``).
SHELL_AUDIT_LOG_WARN_INTERVAL_SECS = 3600.0

# Monotonic instant of the last WARNING per log path. Keyed by path so two data
# homes (two tests, two pods) never share a throttle window.
_warned_at: dict[Path, float] = {}


def shell_audit_log_path(data_home: Path) -> Path:
    """The audit log's path under *data_home*: the file the hook command names."""
    return data_home / SHELL_AUDIT_LOG_NAME


def rotate_shell_audit_log(data_home: Path) -> bool:
    """Rotate ``<data_home>/audit.log`` aside to ``audit.log.1`` once it reaches the cap.

    The cap is :data:`SHELL_AUDIT_LOG_MAX_BYTES`, the one shipped value; no
    caller varies it. Returns ``True`` when the file was moved aside by the
    time this call checked -- by this call or by a concurrent rotator that won
    the lock. Never raises: a fresh install without the file, an unreadable or
    unusable path, or a rotation the primitive could not complete (a blocked
    rename) all answer ``False`` and leave the live file exactly as it was. An
    over-cap file that stays over the cap is logged at WARNING, at most once per
    :data:`SHELL_AUDIT_LOG_WARN_INTERVAL_SECS` per data home, so a rotation that
    stopped working is told apart from a file under the cap without repeating
    the same line on every retry.

    Blocking filesystem work over an agent-writable tree, so callers run it on
    a worker thread, never on the event loop. A file under the cap costs one
    ``stat`` and touches nothing else: the lock file the primitive keeps beside
    the log is created only when a rotation is actually attempted, so a fresh
    data home gains no files from this sweep.
    """
    path = shell_audit_log_path(data_home)
    try:
        size_before = path.stat().st_size
    except (OSError, ValueError):
        return False
    if size_before < SHELL_AUDIT_LOG_MAX_BYTES:
        return False
    rotate_jsonl_at(path, SHELL_AUDIT_LOG_MAX_BYTES)
    try:
        rotated = path.stat().st_size < size_before
    except (OSError, ValueError):
        # The live file is gone: the rename moved it aside and the hook has
        # not appended since. That is a rotation.
        rotated = True
    if rotated:
        logger.info(
            "shell audit log rotated at %d bytes (cap %d); one previous generation kept",
            size_before,
            SHELL_AUDIT_LOG_MAX_BYTES,
        )
    else:
        # The primitive swallows its own failure by contract, so this is the
        # only place a rotation that stopped working can surface. Without it an
        # over-cap file whose rename fails every tick (a directory planted at
        # the ``.1`` slot, a sharing violation that never clears) looks exactly
        # like a file under the cap, and the file the sweep exists to bound
        # grows on with no signal an operator could act on. Throttled to one
        # line per ``SHELL_AUDIT_LOG_WARN_INTERVAL_SECS`` per data home: the
        # caller retries on every tick, and a blocked slot does not clear by
        # itself, so a line per attempt would be the same line hundreds of
        # times a day. The message carries sizes and the fixed file name, not
        # the path: the file lives at one known place under the data home. It
        # names the usual causes, not a determined one -- the primitive reports
        # no reason, only that the file is still over the cap.
        now = time.monotonic()
        last = _warned_at.get(path)
        if last is None or now - last >= SHELL_AUDIT_LOG_WARN_INTERVAL_SECS:
            _warned_at[path] = now
            logger.warning(
                "shell audit log is %d bytes (cap %d) and could not be rotated; "
                "the usual causes are a blocked %s.1 slot or the directory's "
                "permissions (next warning in %d s at the earliest)",
                size_before,
                SHELL_AUDIT_LOG_MAX_BYTES,
                SHELL_AUDIT_LOG_NAME,
                int(SHELL_AUDIT_LOG_WARN_INTERVAL_SECS),
            )
    return rotated
