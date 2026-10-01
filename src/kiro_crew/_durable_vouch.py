"""A restart-surviving copy of the vouches this gateway committed.

``execution_context._VOUCHED_EXECUTIONS`` is process memory, so a restart (or a
cap eviction) drops every entry while the sessions' durable records survive. A
member DM session re-establishes its entry at its next gate-verified admission
from its key and config alone. A session a member CREATED has an ordinary
``chat-`` key, so nothing about the key names its member, and without a second
source it stays refused until its owner acts -- the nested-conductor stall.

This module is that second source. It keeps one small file per vouched session
in its own top-level ``vouched-executions/`` directory under the data home. It is
NOT under ``trust/``, which sandboxes keep read-write for the SEL log. This leaf is
on ``security._SENSITIVE_HOME_DIRS`` (agent file tools refuse it) AND on
``sandbox._CREW_HIDDEN_LEAVES`` (every sandbox bind-masks it, so a spawned shell
cannot open it either). Only the unsandboxed gateway process reads or writes it.
So a session that rewrites its own transcript record cannot write here, and a
record naming a store this file does not name gets nothing.

The file mirrors the in-memory entry, not the record: it is written exactly where
``bind_session_execution`` vouches, and removed wherever a deliberate
withdrawal happens (a privacy tightening, a selection rollback, an explicit
``clear_session_execution``) and when the session's transcript is deleted. A
capacity eviction does NOT remove it -- eviction drops memory under pressure, not
authority, and a cap-evicted session is exactly one this file must bring back --
and neither does a restart, which is the point. So the population is bounded by
the sessions that still exist, not by every session ever vouched.

One file per key, named by the key's SHA-256, so writers never read-modify-write
a shared object. Every reader is TOTAL: a missing, unreadable or malformed file
reads as "not recorded", the refusing answer. Blocking file IO on small files;
the admission's re-vouch runs it via ``asyncio.to_thread``, while withdrawal
callers (``clear_session_execution`` from the hook runner and the close callback)
run it on the loop, the same as the session-record writes they sit beside. The
two run on different threads, so the conditional withdrawal's compare-and-unlink
and the record write share one process lock: without it a write landing between
the compare and the unlink is deleted as if it were the older record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

DURABLE_VOUCH_DIR_NAME = "vouched-executions"

# Every writer is in the one gateway process (bind on worker threads, withdrawal
# on the loop), so a process lock suffices; no file lock is needed.
_WRITE_LOCK = threading.Lock()


def durable_vouch_path(session_key: str) -> Path:
    """The record's path for *session_key*, inside the keystone-gated root."""
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
    return data_home() / DURABLE_VOUCH_DIR_NAME / f"{digest}.json"


def record_durable_vouch(session_key: str, record: dict[str, Any]) -> None:
    """Persist *record* (an ``ExecutionContext.to_record()``) as vouched.

    Best effort: a failed write leaves the in-memory vouch standing and only loses
    the restart self-heal, which is the fail-closed direction.
    """
    path = durable_vouch_path(session_key)
    try:
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Owner-only: a parents=True mkdir otherwise leaves a default-mode
            # directory. The sandbox precreates it 0o700 as well.
            try:
                platform_compat.restrict_dir_to_owner(path.parent)
            except OSError:
                logger.debug("could not tighten mode on %s", path.parent, exc_info=True)
            atomic_write(
                path, json.dumps({"session_key": session_key, "execution": record}), fsync=True
            )
    except OSError:
        logger.warning("could not persist the vouched identity for %r", session_key, exc_info=True)


def read_durable_vouch(session_key: str) -> dict[str, Any] | None:
    """The vouched record for *session_key*, or None when none is recorded."""
    try:
        raw = json.loads(durable_vouch_path(session_key).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(raw, dict) or raw.get("session_key") != session_key:
        # The digest names the file, the payload names the key: a file whose key
        # differs is not this session's, whatever produced it.
        return None
    record = raw.get("execution")
    return record if isinstance(record, dict) else None


def forget_durable_vouch(session_key: str, *, only_if: dict[str, Any] | None = None) -> None:
    """Remove the record, or only when it still equals *only_if*.

    The compare form mirrors the in-memory compare-and-set withdrawals, so a
    rollback of one publication never erases a newer vouch. The lock spans the
    compare AND the unlink: a write is either seen by the compare or waits.
    """
    with _WRITE_LOCK:
        if only_if is not None and read_durable_vouch(session_key) != only_if:
            return
        try:
            durable_vouch_path(session_key).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "could not withdraw the vouched identity for %r", session_key, exc_info=True
            )
