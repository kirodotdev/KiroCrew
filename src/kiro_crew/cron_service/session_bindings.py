"""Gateway-only records of the chat session each cron job is bound to.

``crons.json`` is intentionally writable inside a script cron's sandbox, so its
``session_key`` cannot authorize access to an existing chat session by itself.
This module mirrors that field in ``cron-session-bindings/`` under the data
home. Every sandbox bind-masks the directory, and agent file tools classify it
as sensitive. A sandboxed ``CronService`` writer therefore reaches only its
private masked directory, while the unsandboxed gateway reads the real record.

There is one file per job id, named by its SHA-256 digest. Writers strictly
remove revoked bindings before saving the cron store, then record new grants
best-effort only after that save succeeds. Revocation never trusts the cron
store's value; it uses job ids and these gateway-only records. A failed store
save after revocation leaves the stored job bound but without a record, so it
falls back to the creator fence until re-adopted. Admission reads are total: a
missing, unreadable or malformed record means no binding. Revocation scans skip
missing and malformed records but raise on other I/O failures. The one exception
is a process the macOS kernel reports as Seatbelt-confined: Seatbelt denies the
directory outright instead of masking it, so a ``PermissionError`` there means
no reachable record, as the empty masked copy means on Linux, and revocation
skips a denied unlink the same way.
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


def _seatbelt_confines_this_process() -> bool:
    """True only on the macOS kernel's verdict that Seatbelt confines this process.

    No environment marker counts: the gateway can inherit ``KIROCREW_SANDBOX_ACTIVE``,
    and this verdict relaxes a strict revocation scan. An unanswerable probe reads
    as not confined.
    """
    from kiro_crew import sandbox_probe

    return sandbox_probe._macos_sandbox_state() is True


def _read_session_binding_record(path: Path) -> tuple[str, str] | None:
    """Return one validated binding record, or ``None`` if missing or malformed."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        raw = json.loads(text)
    except (TypeError, UnicodeError, ValueError, RecursionError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != _RECORD_VERSION:
        return None
    job_id = raw.get("job_id")
    session_key = raw.get("session_key")
    if not isinstance(job_id, str) or not job_id:
        return None
    if not isinstance(session_key, str) or not session_key:
        return None
    return job_id, session_key


def read_session_binding(job_id: str) -> str:
    """Return the recorded session key for *job_id*, or ``""`` on any bad record."""
    if not job_id:
        return ""
    try:
        path = session_binding_path(job_id)
        record = _read_session_binding_record(path)
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError):
        return ""
    if record is None or record[0] != job_id:
        return ""
    return record[1]


def list_session_bindings() -> list[tuple[str, str]]:
    """Return valid records; skip missing/malformed files and raise on I/O failure.

    Under a kernel-confirmed Seatbelt sandbox a denied directory or record is not
    reachable and is skipped; everywhere else it raises.
    """
    try:
        directory = data_home() / SESSION_BINDINGS_DIR_NAME
        paths = tuple(directory.iterdir())
    except FileNotFoundError:
        return []
    except PermissionError:
        if _seatbelt_confines_this_process():
            return []
        raise
    except (TypeError, UnicodeError, ValueError):
        return []
    records: list[tuple[str, str]] = []
    for path in paths:
        try:
            record = _read_session_binding_record(path)
        except PermissionError:
            if _seatbelt_confines_this_process():
                continue
            raise
        if record is None:
            continue
        try:
            expected_path = session_binding_path(record[0])
        except (OSError, TypeError, UnicodeError, ValueError):
            continue
        if path != expected_path:
            continue
        records.append(record)
    return sorted(records)


def revoke_session_bindings(job_ids: Iterable[str]) -> None:
    """Remove every named binding before its cron-store revocation; raise on failure.

    A denied unlink under a kernel-confirmed Seatbelt sandbox is skipped: that
    process cannot reach the gateway's records, as ``list_session_bindings`` states.
    """
    ids = tuple(sorted({job_id for job_id in job_ids if job_id}))
    if not ids:
        return
    with _WRITE_LOCK:
        for job_id in ids:
            try:
                session_binding_path(job_id).unlink()
            except FileNotFoundError:
                pass
            except PermissionError:
                if not _seatbelt_confines_this_process():
                    raise
