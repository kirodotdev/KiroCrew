"""Session records a host keeps after a session ends, until Crew deletes them.

On the ``kas`` backend, a task-runner session and a reused workflow worker's
previous conversation end with ``session/close`` instead of
``_kiro/session/delete`` when ``session.kas_delete_delay_secs`` is above zero
(``AcpSessionHandle.destroy``). The close frees the session from the engine's
memory as the delete does and leaves its record on disk, so a tool that reads the
engine's session store still finds a finished step for that long. Something still
has to delete the record, or it stays for good, and that is this module: one file
per released session, saying which session and when its record is due, and a
sweep on the session cleanup loop that deletes the due records.

The file is written BEFORE the close is sent. A gateway that exits before the
sweep, by a restart or a forced exit, then leaves an entry the next gateway's
sweep still finds, never a record nothing will delete.

The sweep is a hook of the session cleanup loop, and that loop otherwise starts
only with the first ordinary session allocation. So the task runner's opener
starts it when it sets a delay (``open_task_session``), and
``SessionManager.start_pool`` starts it when entries wait from an earlier
process. A workflow worker's session comes from ``get_or_create``, which starts
it already.

One file per session, named for the session, so two releases never contend over a
shared file and the sweep is one directory listing. A top-level leaf of the data
home, NOT under ``trust``: an entry authorizes the gateway to delete the record it
names, so an agent able to write one could have any conversation the engine holds
deleted. The leaf is registered in the same four places as ``panel-dismissals``,
for the reasons ``subagent_persistence._panel_dismissals_dir`` gives:
``security/paths._CREW_SECRET_LEAVES`` and, in ``sandbox``,
``_CREW_HIDDEN_LEAVES``, ``_CREW_PRECREATE_HIDDEN_DIR_LEAVES`` and
``_CREW_NO_ALIAS_LEAVES``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

_RELEASED_SESSIONS_LEAF = "released-sessions"

#: A session id spelled as a file name. KAS ids are ``sess_`` and a UUID; the rule
#: is about the FILE the id names, so ``/``, ``..`` and a leading dot never reach
#: the path.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

#: How many records one sweep deletes. Each delete's response wait uses the teardown
#: timeout, and the sweep shares its cleanup tick with the other hooks, so a backlog
#: drains over several ticks instead of holding one.
_SWEEP_BATCH = 50

#: Seconds of deletes after which one sweep starts no further delete; the rest wait
#: for the next tick. The hooks after this one in the cleanup tick wait for it, so an
#: engine that stops answering costs a tick about this budget, not one teardown timeout
#: per record.
#: The check runs before each delete, once the background session is acquired, so the
#: delete in flight and that session's own setup and teardown come on top. A delete
#: the engine answers with an error does not stop the rest of the batch.
_SWEEP_BUDGET_SECS = 15.0

#: How long past its due time an entry released on a backend that is not selected
#: waits before the sweep warns about it. A short switch away and back deletes it
#: as usual; an entry this old most likely belongs to a backend the operator left.
_STRANDED_GRACE_SECS = 7 * 86400

#: The sweep budget's clock, a module binding a test replaces.
_monotonic = time.monotonic

#: Whether this process has warned about entries waiting on a backend that is not
#: selected. Once per process, because the sweep runs on every cleanup tick.
_stranded_warned = False


@dataclass(frozen=True)
class ReleasedSession:
    """A released session whose record waits for deletion."""

    session_id: str
    backend: str
    delete_at: float


def released_sessions_dir() -> Path:
    """Gateway-owned home of the pending deletes, a direct child of the data home."""
    return data_home() / _RELEASED_SESSIONS_LEAF


def _record_path(session_id: str) -> Path:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"Invalid session id for a released-session record: {session_id!r}")
    return released_sessions_dir() / f"{session_id}.json"


def schedule(session_id: str, *, backend: str, delay_secs: float, now: float | None = None) -> None:
    """Record that a session's record is to be deleted ``delay_secs`` from now.

    Raises ``OSError`` when the record cannot be stored and ``ValueError`` for an
    id that is not a safe file name. Either way the caller deletes the session's
    record at once, as a session with no delay is deleted.
    """
    path = _record_path(session_id)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    platform_compat.make_owner_only_dir(directory)
    platform_compat.restrict_dir_to_owner(directory)
    released_at = time.time() if now is None else now
    # ``atomic_write`` applies the owner-only lockdown to the temp file before any
    # content reaches it, so the entry never exists in a readable file.
    atomic_write(
        path,
        json.dumps(
            {
                "version": 1,
                "session_id": session_id,
                "backend": backend,
                "released_at": released_at,
                "delete_at": released_at + max(0.0, float(delay_secs)),
            }
        ),
        restrict_to_owner=True,
    )


def forget(session_id: str, *, delete_at: float | None = None) -> None:
    """Drop a session's entry, once its record is deleted or is to stay.

    With ``delete_at``, the entry is dropped only if it still records that due time
    when checked just before the unlink, so a sweep that acted on one reading does
    not drop an entry written again with another due time before that check. The
    check and the unlink are two steps, not one atomic operation.
    """
    try:
        path = _record_path(session_id)
    except ValueError:
        return
    try:
        if delete_at is not None:
            try:
                stored = json.loads(path.read_text(encoding="utf-8")).get("delete_at")
            except FileNotFoundError:
                return
            except (ValueError, AttributeError):
                stored = None
            if stored != delete_at:
                return
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning(
            "released sessions: could not drop the entry of %s", session_id, exc_info=True
        )


def pending() -> bool:
    """Whether any entry is waiting, so a new process starts the loop that sweeps them."""
    try:
        return any(released_sessions_dir().glob("*.json"))
    except OSError:
        return False


def due(now: float | None = None) -> list[ReleasedSession]:
    """The entries whose record is due for deletion, earliest first.

    An entry that cannot be read is skipped and kept: it names nothing this sweep
    can act on, and removing it would hide that from whoever reads the warning.
    """
    at = time.time() if now is None else now
    try:
        paths = sorted(released_sessions_dir().glob("*.json"))
    except OSError:
        logger.warning("released sessions: the directory cannot be listed", exc_info=True)
        return []
    found: list[ReleasedSession] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = ReleasedSession(
                session_id=str(data["session_id"]),
                backend=str(data["backend"]),
                delete_at=float(data["delete_at"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("released sessions: unreadable entry %s", path.name, exc_info=True)
            continue
        # The file name is the key a later ``forget`` removes, so an entry whose
        # content names another session is not one this sweep may act on.
        if path.name != f"{entry.session_id}.json":
            logger.warning("released sessions: entry %s names another session", path.name)
            continue
        if entry.delete_at <= at:
            found.append(entry)
    found.sort(key=lambda e: e.delete_at)
    return found


async def sweep(sessions: Any, backend: str) -> int:
    """Delete the records that are due, through the shared background runtime.

    ``sessions`` is duck-typed (a ``SessionManager``-like object exposing
    ``get_bg_session()``), as in ``llm_helpers``. The delete is sent on the
    background runtime because the process that released a session has usually
    exited by the time its record is due, and the engine finds a record by its id
    from any process. Only entries released on ``backend``, the one that runtime
    serves, are sent; the rest wait, so a backend switch leaves them for the next
    time it is selected rather than sending one host's verb to another, and the
    first sweep to find one more than ``_STRANDED_GRACE_SECS`` past due says so in
    the log. No further delete starts once ``_SWEEP_BUDGET_SECS`` of deletes have
    passed, and the rest wait for the next tick. Returns how many records were
    deleted.
    """
    found = await asyncio.to_thread(due)
    _warn_if_stranded(found, backend)
    entries = [e for e in found if e.backend == backend][:_SWEEP_BATCH]
    if not entries:
        return 0
    handle = await sessions.get_bg_session()
    deleted = 0
    try:
        # A provider-shaped background session has no runtime to send the verb on.
        runtime = getattr(handle, "_runtime", None)
        if runtime is None or runtime.acp_backend != backend:
            return 0
        started = _monotonic()
        for entry in entries:
            if _monotonic() - started >= _SWEEP_BUDGET_SECS:
                break
            if await runtime.delete_session_record(entry.session_id):
                await asyncio.to_thread(forget, entry.session_id, delete_at=entry.delete_at)
                deleted += 1
    finally:
        await handle.destroy()
    if deleted:
        logger.info("released sessions: deleted %d record(s) that were due", deleted)
    return deleted


async def sweep_on_tick(sessions: Any) -> None:
    """The ``released_sessions`` cleanup hook: one :func:`sweep` per cleanup tick.

    The delete waits for this tick rather than a timer of its own, so an entry left
    by a gateway that exited is found by the next gateway the same way. Nothing is
    sent while admission is closed for a shutdown or an update
    (``sessions.admission_closed``); the entries wait for a later tick. The backend
    is the one ``agent.acp_backend`` names as the tick runs. A failed sweep is
    logged and tried again on the next tick, as the other hooks' failures are.
    """
    if sessions.admission_closed:
        return
    try:
        await sweep(sessions, sessions._cfg.agent.acp_backend)
    except Exception:
        logger.warning("released-session sweep failed; will retry next tick", exc_info=True)


def _warn_if_stranded(found: list[ReleasedSession], backend: str) -> None:
    """Warn once per process about records that wait on a backend that is not selected."""
    global _stranded_warned
    if _stranded_warned:
        return
    cutoff = time.time() - _STRANDED_GRACE_SECS
    stranded: dict[str, int] = {}
    for entry in found:
        if entry.backend != backend and entry.delete_at < cutoff:
            stranded[entry.backend] = stranded.get(entry.backend, 0) + 1
    if not stranded:
        return
    _stranded_warned = True
    for released_on, count in sorted(stranded.items()):
        logger.warning(
            "released sessions: %d record(s) released on the %s backend have been due "
            "for over %d days, and are deleted only while that backend is selected. "
            "To delete them, select it again (`kirocrew config set agent.acp_backend %s`, "
            "then `kirocrew restart`) and keep it selected until they are deleted: each "
            "session cleanup tick deletes up to %d and logs how many.",
            count,
            released_on,
            _STRANDED_GRACE_SECS // 86400,
            released_on,
            _SWEEP_BATCH,
        )
