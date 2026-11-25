"""A scheduled job's auto approval counts only when the gateway signed it.

Two stored fields let a job's fire auto-approve its tool calls: the job's own
``approval_mode == "auto"`` (the owner's switch) and the Bot owner grant
(``owner_member_id`` with ``owner_approval_mode``, see ``owner_grant``). Each
gateway write that sets either one also stores ``approval_grant``: an
HMAC-SHA256 over those approval fields and every field that picks who or what
a fire runs as (:data:`_SIGNED_FIELDS`: ``id``, ``approval_mode``,
``owner_member_id``, ``owner_approval_mode``, ``session_key``, ``agent_id``,
``member_id``, ``memory_store``, ``execution_context``, ``agent_sequence``),
read the same way from a raw store record and from the job decoded from it.
At fire time the grant counts only while that MAC matches (:func:`holds`).

The key comes from the persisted dashboard token-signing key under a label of
its own. That key file is masked in every sandbox, so a process in a sandbox
cannot sign. A record written or edited by hand, or a MAC copied from another
job, therefore matches nothing: the job keeps loading and running, but it fires
interactive (every tool call asks). Nothing is deleted, refused or rewritten on
load; a load in a process that holds the key logs one warning naming those jobs
(``store_sync._load``), and a process without the key says nothing.

Jobs stored before this field existed get their MAC once, at start, from
:func:`sign_existing_grants`. Its marker lives in a directory the sandbox
hides (:data:`MIGRATION_DIR_LEAF`), so the pass cannot be made to run again.

shortcut: the MAC covers who the job runs as and its approval fields, not its
message or command; the job store itself is still sandbox-writable on Linux.
Upgrade when the store moves into a sealed directory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from pathlib import Path
from typing import Any

# The cron channel every cron_service owner logs on.
logger = logging.getLogger("kiro_crew.cron")

_MAC_LABEL = b"kiro-crew cron approval grant v1"

#: The job fields the MAC covers, in signing order: its approval fields and every
#: field that picks who or what a fire runs as.
_SIGNED_FIELDS = (
    "id",
    "approval_mode",
    "owner_member_id",
    "owner_approval_mode",
    "session_key",
    "agent_id",
    "member_id",
    "memory_store",
    "execution_context",
    "agent_sequence",
)

#: A stored grant this module could have written: lower-case hex SHA-256.
_MAC_SHAPE = re.compile(r"[0-9a-f]{64}")

#: The data-home directory holding the one-time signing pass's marker. Hidden in
#: every sandbox (``sandbox_leaves._CREW_HIDDEN_LEAVES``).
MIGRATION_DIR_LEAF = "cron-grants"
#: The marker file :func:`sign_existing_grants` writes once its pass ran.
MIGRATION_MARKER = "signed"


def _key() -> bytes | None:
    """The grant MAC key, or ``None`` when the token-signing key is not persisted."""
    try:
        from kiro_crew.dashboard.token_secret import get_persisted_secret

        secret = get_persisted_secret()
    except Exception:
        logger.debug("cron approval grant: no persisted signing key", exc_info=True)
        return None
    return hmac.new(secret, _MAC_LABEL, hashlib.sha256).digest()


def _raw(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _value(obj: Any, name: str) -> str:
    """Field *name* of a store record (dict) or a ``CronJob``; a non-string reads as ""."""
    value = _raw(obj, name)
    return value if isinstance(value, str) else ""


def _signed_value(obj: Any, name: str) -> Any:
    """Field *name* as the MAC reads it, the same for a raw record and its decoded job.

    ``execution_context`` is the dict or None; ``agent_sequence`` the list of its
    string items (as ``identity._identity_job`` reads it); every other field a str.
    """
    value = _raw(obj, name)
    if name == "execution_context":
        return value if isinstance(value, dict) else None
    if name == "agent_sequence":
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
    return value if isinstance(value, str) else ""


def carries_grant(obj: Any) -> bool:
    """Whether *obj* stores an approval grant: its own ``auto`` or a Bot owner grant."""
    return (
        _value(obj, "approval_mode") == "auto"
        or bool(_value(obj, "owner_member_id"))
        or bool(_value(obj, "owner_approval_mode"))
    )


def mac(obj: Any) -> str | None:
    """The hex MAC of *obj*'s signed fields, or ``None`` when there is no key."""
    key = _key()
    if key is None:
        return None
    text = json.dumps(
        [_signed_value(obj, f) for f in _SIGNED_FIELDS],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hmac.new(key, text.encode("ascii"), hashlib.sha256).hexdigest()


def holds(obj: Any) -> bool:
    """Whether *obj* stores a grant AND the gateway signed exactly these fields."""
    if not carries_grant(obj):
        return False
    stored = _value(obj, "approval_grant")
    # Any other value (empty, odd length, non-ASCII) was not written here.
    if not _MAC_SHAPE.fullmatch(stored):
        return False
    expected = mac(obj)
    if expected is None:
        return False
    return hmac.compare_digest(stored, expected)


def sign(job: Any) -> None:
    """Store the gateway's MAC on *job* (or clear it when it carries no grant).

    Marks *job* as held by this process (``CronJob.grant_held``). A process with
    no persisted key writes "" here, so the job fires interactive.
    """
    job.approval_grant = (mac(job) or "") if carries_grant(job) else ""
    job.grant_held = True


def sign_record(record: dict[str, Any]) -> bool:
    """Store the gateway's MAC on a raw store *record*; whether it wrote one."""
    if not carries_grant(record):
        record["approval_grant"] = ""
        return False
    signed = mac(record)
    record["approval_grant"] = signed or ""
    return bool(signed)


def sign_existing_grants(store_dir: Path) -> list[str]:
    """Sign each stored grant that predates ``approval_grant``, once. Never raises.

    Runs at start, after ``owner_grant.migrate_owner_grants``. Skipped while the
    marker under :data:`MIGRATION_DIR_LEAF` exists. With no persisted key it logs
    and returns without the marker, so the next start tries again. Returns the
    signed ids.

    Under the store lock the marker is written and synced BEFORE any signature
    reaches the store, so a pass can only publish signatures once: a marker write
    that fails leaves the store untouched for the next start to retry. A store
    write that fails after the marker landed is not retried, and those grants stay
    unsigned and fire interactive (the load warning names them). A store that does
    not parse, or holds no ``jobs`` list, also writes the marker and signs nothing.
    """
    from kiro_crew.atomic_write import atomic_write, fsync_dir
    from kiro_crew.cron_service.store import _CRONS_FILE, cron_store_lock
    from kiro_crew.owner_only_files import mkdirs_owner_only

    marker_dir = store_dir / MIGRATION_DIR_LEAF
    marker = marker_dir / MIGRATION_MARKER
    path = store_dir / _CRONS_FILE

    def _consume_marker() -> None:
        # Every directory this creates is 0700; atomic_write gives a data-home
        # file 0600 by default (owner_only_files).
        mkdirs_owner_only(marker_dir)
        atomic_write(marker, "1\n", fsync=True)
        fsync_dir(marker_dir)

    try:
        if marker.exists():
            return []
        if _key() is None:
            logger.warning(
                "Stored cron approval grants were not signed: no persisted signing key; "
                "the next start tries again"
            )
            return []
        if not path.is_file():
            _consume_marker()
            return []
        with cron_store_lock(store_dir):
            # Another process may have run the pass while this one waited.
            if marker.exists():
                return []
            raw = path.read_bytes()
            try:
                data = json.loads(raw)
            except (ValueError, RecursionError):
                data = None
            records = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(records, list):
                # Unreadable: the pass is still spent, so a store that parses
                # later is not signed wholesale.
                _consume_marker()
                return []
            signed: list[str] = []
            for record in records:
                if not isinstance(record, dict) or not carries_grant(record):
                    continue
                if holds(record):
                    continue
                if sign_record(record):
                    signed.append(str(record.get("id") or ""))
            _consume_marker()
            if signed:
                atomic_write(path, json.dumps(data, indent=2))
        return signed
    except Exception:
        logger.warning("Stored cron approval grants were not signed", exc_info=True)
        return []
