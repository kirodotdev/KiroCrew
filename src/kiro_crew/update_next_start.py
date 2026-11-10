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
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = "update_next_start.json"

#: Boot-time applies started for one arm before it is given up as failed. A
#: process that dies mid-apply leaves the arm behind, so the next start retries;
#: this bounds that to a few tries.
MAX_BOOT_ATTEMPTS = 3

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
    return NextStartState(
        armed_version=version if isinstance(version, str) else "",
        armed_at=(
            float(armed_at)
            if isinstance(armed_at, (int, float)) and not isinstance(armed_at, bool)
            else 0.0
        ),
        attempts=attempts if isinstance(attempts, int) and not isinstance(attempts, bool) else 0,
        pending_apply_error=error if isinstance(error, str) else "",
    )


def _write(state: NextStartState) -> None:
    from kiro_crew.atomic_write import atomic_write

    global _differs_memo
    _differs_memo = None

    payload = {
        "armed_version": state.armed_version,
        "armed_at": state.armed_at,
        "attempts": state.attempts,
        "pending_apply_error": state.pending_apply_error,
    }
    atomic_write(_state_path(), json.dumps(payload, sort_keys=True), restrict_to_owner=True)


def arm(version: str) -> NextStartState:
    """Arm the update to *version* (``""`` when the newest is not known yet).

    Re-arming the same version keeps its first ``armed_at``. Arming clears the
    last apply's error: the person asked again.
    """
    with _lock:
        old = read()
        if old.armed and old.armed_version == version:
            state = NextStartState(version, old.armed_at, old.attempts, "")
        else:
            state = NextStartState(version, time.time(), 0, "")
        _write(state)
    logger.info("Update armed for the next start: version=%s", version or "-")
    return state


def disarm() -> bool:
    """Drop the arm. ``True`` when one was armed."""
    with _lock:
        old = read()
        if not old.armed:
            return False
        _write(NextStartState(pending_apply_error=old.pending_apply_error))
    logger.info("Update disarmed: the next start applies nothing")
    return True


def note_boot_attempt() -> NextStartState:
    """Count one boot-time apply against the arm, and return the record."""
    with _lock:
        old = read()
        state = NextStartState(old.armed_version, old.armed_at, old.attempts + 1, "")
        _write(state)
    return state


def finish(*, error: str = "") -> None:
    """End the arm after a boot-time apply: cleared, with *error* kept when it failed."""
    with _lock:
        _write(NextStartState(pending_apply_error=error[:_ERROR_MAX_CHARS]))


def clear_error() -> None:
    """Forget the last apply's error (the person has seen it)."""
    with _lock:
        old = read()
        if old.pending_apply_error:
            _write(NextStartState(old.armed_version, old.armed_at, old.attempts, ""))


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


def installed_differs_from_running() -> bool:
    """Whether the installed version is not the one this process runs."""
    from kiro_crew import __version__

    installed = installed_version()
    return bool(installed) and installed != __version__


_DIFFERS_TTL_SECS = 5.0
_differs_memo: tuple[float, bool] | None = None


def _differs_cached() -> bool:
    """:func:`installed_differs_from_running`, memoized for a few seconds.

    The status frame is built on every poll; the answer reads the disk.
    """
    global _differs_memo
    now = time.monotonic()
    memo = _differs_memo
    if memo is not None and now - memo[0] < _DIFFERS_TTL_SECS:
        return memo[1]
    answer = installed_differs_from_running()
    _differs_memo = (now, answer)
    return answer


def status_fields() -> dict[str, object]:
    """The status-frame fields (S3): what is armed, whether the install moved, the last error."""
    state = read()
    armed_at = (
        datetime.fromtimestamp(state.armed_at, tz=timezone.utc).isoformat() if state.armed else None
    )
    return {
        "update_armed_version": state.armed_version if state.armed else None,
        "update_armed_at": armed_at,
        "installed_differs_from_running": _differs_cached(),
        "pending_apply_error": state.pending_apply_error or None,
    }
