"""The update a person armed, and the error its last boot-time apply left.

A click on Update does not install anything: it ARMS the update here (decision
entry "updates never restart a running gateway"). The apply runs at the next
start, before the gateway serves or restores anything
(:mod:`kiro_crew.update_boot_apply`). The record is a small JSON file in the
data home so it outlives the process that wrote it; nothing here starts a
process, runs a command or restarts anything.

The record holds the armed version, when it was armed, how many boot-time
applies were started for it (a crash loop gives up), and the error a failed
apply left for the dashboard to show.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = "update_next_start.json"

#: Boot-time applies started for one arm before it is given up as failed. A
#: process that dies mid-apply leaves the arm behind, so the next start retries;
#: this bounds that to a few tries.
MAX_BOOT_ATTEMPTS = 3

#: Longest one boot-time apply may run before it counts as failed. The apply
#: child is bounded by it, and a restart waiter extends its wait to it.
APPLY_BOUND_SECS = 900.0

#: Record of the apply running right now, in the data home.
APPLYING_FILE = "update_applying.json"

#: The longest error kept, so a noisy installer cannot grow the file or the frame.
_ERROR_MAX_CHARS = 600

_lock = threading.Lock()


@dataclass(frozen=True)
class NextStartState:
    """What is armed, and what the last boot-time apply said."""

    armed_version: str = ""
    armed_at: float = 0.0
    attempts: int = 0
    pending_apply_error: str = ""
    #: Commits the armed git checkout was behind its upstream. A git update can
    #: change the code without moving ``__version__``, so a positive count marks an
    #: arm whose version equals the running one but still has something to apply.
    armed_behind: int = 0
    #: The version this process ran when the apply failed, so a start on a
    #: different version can tell the error describes another install.
    error_version: str = ""

    @property
    def armed(self) -> bool:
        """Whether an update waits for the next start."""
        return self.armed_at > 0.0


def _state_path() -> Path:
    from kiro_crew.config.paths import data_home

    return data_home() / STATE_FILE


def read() -> NextStartState:
    """The record, or an empty one when it is absent or unreadable."""
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return NextStartState()
    if not isinstance(raw, dict):
        return NextStartState()
    version = raw.get("armed_version")
    armed_at = raw.get("armed_at")
    attempts = raw.get("attempts")
    error = raw.get("pending_apply_error")
    behind = raw.get("armed_behind")
    error_version = raw.get("error_version")
    return NextStartState(
        armed_version=version if isinstance(version, str) else "",
        armed_at=(
            float(armed_at)
            if isinstance(armed_at, (int, float)) and not isinstance(armed_at, bool)
            else 0.0
        ),
        attempts=attempts if isinstance(attempts, int) and not isinstance(attempts, bool) else 0,
        pending_apply_error=error if isinstance(error, str) else "",
        armed_behind=(
            behind if isinstance(behind, int) and not isinstance(behind, bool) and behind > 0 else 0
        ),
        error_version=error_version if isinstance(error_version, str) else "",
    )


def _write(state: NextStartState) -> None:
    from kiro_crew.atomic_write import atomic_write

    payload = {
        "armed_version": state.armed_version,
        "armed_at": state.armed_at,
        "attempts": state.attempts,
        "pending_apply_error": state.pending_apply_error,
        "armed_behind": state.armed_behind,
        "error_version": state.error_version,
    }
    atomic_write(_state_path(), json.dumps(payload, sort_keys=True), restrict_to_owner=True)


def arm(version: str, *, behind: int = 0) -> NextStartState:
    """Arm the update to *version* (``""`` when the newest is not known yet).

    *behind* is the git checkout's commit distance behind its upstream (``0`` on
    every other route). Re-arming the same version keeps its first ``armed_at``.
    Arming clears the last apply's error: the person asked again.
    """
    behind = max(0, behind)
    with _lock:
        old = read()
        if old.armed and old.armed_version == version:
            state = NextStartState(version, old.armed_at, old.attempts, "", behind)
        else:
            state = NextStartState(version, time.time(), 0, "", behind)
        _write(state)
    logger.info("Update armed for the next start: version=%s", version or "-")
    return state


def disarm() -> bool:
    """Drop the arm. ``True`` when one was armed."""
    with _lock:
        old = read()
        if not old.armed:
            return False
        _write(
            NextStartState(
                pending_apply_error=old.pending_apply_error, error_version=old.error_version
            )
        )
    logger.info("Update disarmed: the next start applies nothing")
    return True


def note_boot_attempt() -> NextStartState:
    """Count one boot-time apply against the arm, and return the record."""
    with _lock:
        old = read()
        state = NextStartState(
            old.armed_version, old.armed_at, old.attempts + 1, "", old.armed_behind
        )
        _write(state)
    return state


def finish(*, error: str = "") -> None:
    """End the arm after a boot-time apply: cleared, with *error* kept when it failed."""
    from kiro_crew import __version__

    with _lock:
        _write(
            NextStartState(
                pending_apply_error=error[:_ERROR_MAX_CHARS],
                error_version=__version__ if error else "",
            )
        )


def clear_error() -> None:
    """Forget the last apply's error (the person has seen it)."""
    with _lock:
        old = read()
        if old.pending_apply_error:
            _write(
                NextStartState(old.armed_version, old.armed_at, old.attempts, "", old.armed_behind)
            )


def installed_version() -> str | None:
    """The version a start from this install would run, or ``None`` when unknown.

    A managed venv answers with the tree its stable link resolves to
    (``crew-venv-<version>``); any other install with its installed metadata,
    read fresh from disk. A policy-command install that cannot be read answers
    ``None``, and the caller treats that as "not known to differ".
    """
    try:
        from kiro_crew.platform import wheel_engine

        layout = wheel_engine.managed_venv_layout()
        if layout.is_managed_tree(layout.stable_link):
            tree = Path(os.path.realpath(wheel_engine.respawn_executable())).parent.parent
            prefix = f"{layout.legacy.name}-"
            if tree.name.startswith(prefix):
                return tree.name[len(prefix) :]
    except Exception:
        logger.debug("managed venv version unreadable", exc_info=True)
    try:
        from importlib import metadata

        return metadata.version("kirocrew")
    except Exception:
        return None


def status_fields() -> dict[str, object]:
    """The status-frame fields (S3): what is armed and the last apply's error."""
    state = read()
    return {
        "update_armed_version": state.armed_version if state.armed else None,
        "pending_apply_error": state.pending_apply_error or None,
    }


def _applying_path() -> Path:
    from kiro_crew.config.paths import data_home

    return data_home() / APPLYING_FILE


def mark_applying() -> None:
    """Record that this process is applying an update, for a restart waiter to read.

    The gateway binds nothing while it applies, so a waiter probing the port
    sees no answer; this record is how it tells a slow apply from a wedged boot.
    """
    from kiro_crew.atomic_write import atomic_write

    payload = {"pid": os.getpid(), "started": time.time()}
    try:
        atomic_write(_applying_path(), json.dumps(payload), restrict_to_owner=True)
    except OSError:
        logger.debug("could not record the running apply", exc_info=True)


def clear_applying() -> None:
    """Drop the running-apply record."""
    try:
        _applying_path().unlink()
    except OSError:
        pass


def applying_started(pid: int, *, now: float | None = None) -> float | None:
    """When *pid* began applying an update, or ``None`` if it is not applying.

    A record older than the apply bound is stale: the apply child is killed at
    that bound, so a process still marked after it is not applying.
    """
    try:
        raw = json.loads(_applying_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("pid") != pid:
        return None
    started = raw.get("started")
    if not isinstance(started, (int, float)) or isinstance(started, bool):
        return None
    current = time.time() if now is None else now
    if current - started > APPLY_BOUND_SECS + 60.0:
        return None
    return float(started)
