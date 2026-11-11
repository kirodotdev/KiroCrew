"""Gateway-only records of the chat session each cron job is bound to.

``crons.json`` is intentionally writable inside a script cron's sandbox, so its
``session_key`` cannot authorize access to an existing chat session by itself.
This module mirrors that field in ``cron-session-bindings/`` under the data
home. Every sandbox bind-masks the directory, and agent file tools classify it
as sensitive. A sandboxed ``CronService`` writer therefore reaches only its
private masked directory, while the unsandboxed gateway reads the real record.

There is one file per job id, named by its SHA-256 digest. Writers strictly
remove revoked bindings before saving the cron store, then record new grants
best-effort only after that save succeeds. A failed store save after revocation
leaves the stored job bound but without a record, so it falls back to the creator
fence until re-adopted. Every reader is total: a missing, unreadable or malformed
record means no binding.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Iterable
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

logger = logging.getLogger("kiro_crew.cron")

SESSION_BINDINGS_DIR_NAME = "cron-session-bindings"
_RECORD_VERSION = 1
_WRITE_LOCK = threading.Lock()


def session_binding_path(job_id: str) -> Path:
    """Return the gateway-only binding record path for *job_id*."""
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    return data_home() / SESSION_BINDINGS_DIR_NAME / f"{digest}.json"


def record_session_binding(job_id: str, session_key: str) -> None:
    """Record a non-empty *session_key* grant for *job_id*, best-effort."""
    if not job_id or not session_key:
        return
    try:
        path = session_binding_path(job_id)
        payload = json.dumps(
            {"version": _RECORD_VERSION, "job_id": job_id, "session_key": session_key},
            separators=(",", ":"),
        )
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                platform_compat.restrict_dir_to_owner(path.parent)
            except OSError:
                logger.debug(
                    "could not tighten mode on cron session binding directory", exc_info=True
                )
            atomic_write(path, payload, fsync=True, restrict_to_owner=True)
    except (OSError, TypeError, UnicodeError, ValueError):
        logger.warning(
            "could not persist the session binding for cron job %r", job_id, exc_info=True
        )


def read_session_binding(job_id: str) -> str:
    """Return the recorded session key for *job_id*, or ``""`` on any bad record."""
    if not job_id:
        return ""
    try:
        raw = json.loads(session_binding_path(job_id).read_text(encoding="utf-8"))
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError):
        return ""
    if not isinstance(raw, dict):
        return ""
    if raw.get("version") != _RECORD_VERSION or raw.get("job_id") != job_id:
        return ""
    session_key = raw.get("session_key")
    return session_key if isinstance(session_key, str) and session_key else ""


def revoke_session_bindings(job_ids: Iterable[str]) -> None:
    """Remove every named binding before its cron-store revocation; raise on failure."""
    ids = tuple(sorted({job_id for job_id in job_ids if job_id}))
    if not ids:
        return
    with _WRITE_LOCK:
        for job_id in ids:
            try:
                session_binding_path(job_id).unlink()
            except FileNotFoundError:
                pass
