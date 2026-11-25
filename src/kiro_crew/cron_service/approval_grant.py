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
The MAC also covers the job's nonce: a random value the gateway keeps per job in
:data:`NONCES_FILE`, under the sandbox-hidden :data:`MIGRATION_DIR_LEAF`. Turning
the grant off deletes the nonce and turning it on again mints a new one, so an
earlier or copied signature written back into the store matches nothing. A job
with no stored nonce never holds. At fire time the grant counts only while that
MAC matches (:func:`holds`).

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
import os
import re
import secrets
import stat
from collections.abc import Iterable, Mapping, MutableMapping
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
#: The gateway's per-job nonces, beside the marker:
#: ``{"version": 1, "nonces": {job_id: 32 lower-case hex}}``.
NONCES_FILE = "nonces.json"
_NONCES_VERSION = 1
_NONCE_SHAPE = re.compile(r"[0-9a-f]{32}")
# shortcut: an arbitrary ceiling, about 20 000 jobs at ~50 bytes each; raise it
# if a store ever holds more grants than that.
_MAX_NONCES_BYTES = 1024 * 1024


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


def read_nonces(store_dir: Path) -> dict[str, str]:
    """The stored nonce of each job, by id. Never raises.

    Opened without following a link, non-blocking and bounded, as
    ``kept_grants._read_bounded`` reads. A file that is absent, unreadable, too
    large or malformed reads as ``{}``, so every grant fails closed (interactive);
    so does an entry that is not 32 lower-case hex characters.
    """
    from kiro_crew.bounded_read import bounded_read_fd

    path = store_dir / MIGRATION_DIR_LEAF / NONCES_FILE
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        if path.is_symlink():
            return {}
        fd = os.open(path, flags)
    except OSError:
        return {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_NONCES_BYTES:
            return {}
        raw = bounded_read_fd(fd, _MAX_NONCES_BYTES + 1)
    except OSError:
        return {}
    finally:
        os.close(fd)
    if len(raw) > _MAX_NONCES_BYTES:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _NONCES_VERSION:
        return {}
    nonces = data.get("nonces")
    if not isinstance(nonces, dict):
        return {}
    return {
        job_id: nonce
        for job_id, nonce in nonces.items()
        if isinstance(job_id, str) and isinstance(nonce, str) and _NONCE_SHAPE.fullmatch(nonce)
    }


def write_nonces(store_dir: Path, nonces: Mapping[str, str]) -> None:
    """Replace the stored nonces with *nonces*, synced. Raises when it cannot write."""
    from kiro_crew.atomic_write import atomic_write, fsync_dir
    from kiro_crew.owner_only_files import mkdirs_owner_only

    directory = store_dir / MIGRATION_DIR_LEAF
    # Every directory this creates is 0700; atomic_write gives a data-home file
    # 0600 by default (owner_only_files).
    mkdirs_owner_only(directory)
    document = {"version": _NONCES_VERSION, "nonces": dict(nonces)}
    atomic_write(directory / NONCES_FILE, json.dumps(document, sort_keys=True), fsync=True)
    fsync_dir(directory, best_effort=True)


def attach_nonces(jobs: Iterable[Any], store_dir: Path) -> None:
    """Give each job in *jobs* that stores a grant its stored nonce, and mark it held
    (``CronJob.grant_held``) when its MAC matches. Reads the nonces file at most once."""
    carrying = [job for job in jobs if carries_grant(job)]
    if not carrying:
        return
    nonces = read_nonces(store_dir)
    for job in carrying:
        job.grant_nonce = nonces.get(job.id, "")
        job.grant_held = holds(job)


def _nonce(obj: Any, nonces: Mapping[str, str] | None) -> str:
    if nonces is not None:
        return nonces.get(_value(obj, "id"), "")
    # A raw record is read from the sandbox-writable store, so its nonce comes
    # only from *nonces*, never from the record.
    return "" if isinstance(obj, dict) else _value(obj, "grant_nonce")


def mac(obj: Any, nonces: Mapping[str, str] | None = None) -> str | None:
    """The hex MAC of *obj*'s signed fields and its nonce, or ``None`` with no key or nonce.

    The nonce is ``nonces[obj.id]`` when *nonces* is given, else a job's own
    ``grant_nonce``; a raw record without *nonces* has none.
    """
    key = _key()
    nonce = _nonce(obj, nonces)
    if key is None or not _NONCE_SHAPE.fullmatch(nonce):
        return None
    text = json.dumps(
        [*(_signed_value(obj, f) for f in _SIGNED_FIELDS), nonce],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hmac.new(key, text.encode("ascii"), hashlib.sha256).hexdigest()


def holds(obj: Any, nonces: Mapping[str, str] | None = None) -> bool:
    """Whether *obj* stores a grant AND the gateway signed exactly these fields and nonce."""
    if not carries_grant(obj):
        return False
    stored = _value(obj, "approval_grant")
    # Any other value (empty, odd length, non-ASCII) was not written here.
    if not _MAC_SHAPE.fullmatch(stored):
        return False
    expected = mac(obj, nonces)
    if expected is None:
        return False
    return hmac.compare_digest(stored, expected)


def _signing_nonce(job_id: str, nonces: MutableMapping[str, str], held: str = "") -> str:
    """The nonce *job_id* is signed with, stored in *nonces*: the one stored for it,
    else *held* (the job's own), else a new one."""
    nonce = nonces.get(job_id) or held or secrets.token_hex(16)
    nonces[job_id] = nonce
    return nonce


def sign(job: Any, nonces: MutableMapping[str, str]) -> None:
    """Store the gateway's MAC on *job* (or clear it when it carries no grant).

    *nonces* is the stored nonces (:func:`read_nonces`), updated here: a grant
    keeps its nonce or gets a new one, and a job with no grant loses its nonce.
    The caller writes *nonces* before the store. A process with no persisted key
    writes "" here and leaves *nonces* as they are, so the job fires interactive.
    Marks *job* as held by this process (``CronJob.grant_held``).
    """
    if not carries_grant(job):
        nonces.pop(job.id, None)
        job.grant_nonce = ""
        job.approval_grant = ""
    elif _key() is None:
        job.approval_grant = ""
    else:
        job.grant_nonce = _signing_nonce(job.id, nonces, job.grant_nonce)
        job.approval_grant = mac(job) or ""
    job.grant_held = True


def sign_record(record: dict[str, Any], nonces: MutableMapping[str, str]) -> bool:
    """Store the gateway's MAC on a raw store *record*; whether it wrote one.

    *nonces* is updated as :func:`sign` updates it.
    """
    job_id = _value(record, "id")
    if not carries_grant(record):
        nonces.pop(job_id, None)
        record["approval_grant"] = ""
        return False
    if not job_id or _key() is None:
        record["approval_grant"] = ""
        return False
    _signing_nonce(job_id, nonces)
    signed = mac(record, nonces)
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
    unsigned and fire interactive (the load warning names them); the nonces are
    written between the two, and a nonce write that fails ends the pass the same
    way, before the store is written. A store that does
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
            nonces = read_nonces(store_dir)
            stored = dict(nonces)
            signed: list[str] = []
            for record in records:
                if not isinstance(record, dict) or not carries_grant(record):
                    continue
                if holds(record, nonces):
                    continue
                if sign_record(record, nonces):
                    signed.append(str(record.get("id") or ""))
            _consume_marker()
            # Each new nonce is stored before a signature that uses it.
            if nonces != stored:
                write_nonces(store_dir, nonces)
            if signed:
                atomic_write(path, json.dumps(data, indent=2))
        return signed
    except Exception:
        logger.warning("Stored cron approval grants were not signed", exc_info=True)
        return []
