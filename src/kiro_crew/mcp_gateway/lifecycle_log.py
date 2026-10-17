"""Join keys for the MCP backend lifecycle log lines.

The daemon logs each lifecycle step of a backend (spawn, initialize, timeout,
forced exit, recycle, respawn) as its own line. These fields let a reader join
the lines that belong to one backend process and one launch attempt without
parsing the free text:

* ``backend_generation_id`` -- minted once per backend process, at spawn. A
  respawn is a new process, so it gets a new one.
* ``attempt_id`` -- minted per launch attempt: once at spawn, and again when
  the gateway re-sends ``initialize`` to the SAME process (the generation
  stays the same).
* ``event`` / ``reason`` -- bounded names from :data:`EVENTS` and
  :data:`REASONS`. ``reason`` is an outcome, so a line that only starts
  something carries none.

The ids are random hex. They carry no user data, server names, paths or
commands, and nothing reads them back: they exist for the log only.
"""

from __future__ import annotations

import secrets
from typing import Optional

EVENTS = frozenset(
    {
        "initialize_started",
        "initialize_succeeded",
        "initialize_failed",
        "initialize_timed_out",
        "forced_exit",
        "backend_gone",
        "reconnect_succeeded",
    }
)

REASONS = frozenset(
    {
        "success",
        "deadline",
        "process_exit",
        "transport_closed",
        "unknown",
    }
)

# Bytes of randomness per id; 16 hex characters is plenty to keep ids distinct
# inside one log file.
_ID_BYTES = 8


def heartbeat_recycle_reason(state: str, returncode: Optional[int]) -> str:
    """The ``reason`` for a heartbeat recycle of a ``"gone"`` or ``"wedged"`` backend.

    ``"gone"`` is either a reaped process or a ping write that hit a broken
    pipe on a process that has not exited; only the first is ``process_exit``.
    """
    if state == "wedged":
        return "deadline"
    return "process_exit" if returncode is not None else "transport_closed"


def new_lifecycle_id() -> str:
    """A fresh random id for ``backend_generation_id`` or ``attempt_id``."""
    return secrets.token_hex(_ID_BYTES)


def lifecycle_fields(
    event: str,
    reason: Optional[str] = None,
    *,
    generation: str = "",
    attempt: str = "",
) -> str:
    """Render the join fields as ``key=value`` pairs for a log line.

    A name outside the bounded sets is logged as ``unknown`` rather than
    raised: a log line must never change what the daemon does.
    """
    parts = [f"event={event if event in EVENTS else 'unknown'}"]
    if reason is not None:
        parts.append(f"reason={reason if reason in REASONS else 'unknown'}")
    if generation:
        parts.append(f"backend_generation_id={generation}")
    if attempt:
        parts.append(f"attempt_id={attempt}")
    return " ".join(parts)
