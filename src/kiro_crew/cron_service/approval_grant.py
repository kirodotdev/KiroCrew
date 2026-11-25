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
:data:`NONCES_FILE`, under the sandbox-hidden :data:`MIGRATION_DIR_LEAF`. A signing
keeps the nonce only while nothing signed changed; any change (auto turned off on
a job that keeps its Bot fields, a new capture) mints a new one, and a grant
turned off or removed loses its nonce, so an earlier or copied signature written
back into the store matches nothing. A job
with no stored nonce never holds. At fire time the grant counts only while that
MAC matches (:func:`holds`), checked against the nonce file as it is then; a
save that finds a job's nonce gone stops trusting the job and signs nothing.

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
import threading
from collections.abc import Callable, Iterable, Mapping, MutableMapping
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


#: The last parse of each nonces file, keyed by the file's stat, so a fire's check
#: costs one ``lstat`` while the file is unchanged. A write replaces the file
#: (``atomic_write``), which gives it a new inode, so the next read parses it.
_NONCE_CACHE: dict[str, tuple[tuple[int, int, int, int], dict[str, str]]] = {}
_NONCE_CACHE_LOCK = threading.Lock()


def _stat_key(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


def read_nonces(store_dir: Path) -> dict[str, str]:
    """The stored nonce of each job, by id: a copy the caller may change. Never raises.

    Opened without following a link, non-blocking and bounded, as
    ``kept_grants._read_bounded`` reads. A file that is absent, unreadable, too
    large or malformed reads as ``{}``, so every grant fails closed (interactive);
    so does an entry that is not 32 lower-case hex characters. An unchanged file
    (same device, inode, mtime and size) is answered from the last parse.
    """
    path = store_dir / MIGRATION_DIR_LEAF / NONCES_FILE
    try:
        info = os.lstat(path)
    except OSError:
        return {}
    if not stat.S_ISREG(info.st_mode):
        return {}
    with _NONCE_CACHE_LOCK:
        cached = _NONCE_CACHE.get(str(path))
    if cached is not None and cached[0] == _stat_key(info):
        return dict(cached[1])
    parsed = _parse_nonces_file(path)
    if parsed is None:
        return {}
    key, nonces = parsed
    with _NONCE_CACHE_LOCK:
        _NONCE_CACHE[str(path)] = (key, nonces)
    return dict(nonces)


def _parse_nonces_file(path: Path) -> tuple[tuple[int, int, int, int], dict[str, str]] | None:
    """The stat key of the file opened and the nonces it holds, or ``None`` when no
    regular file under the bound could be read."""
    from kiro_crew.bounded_read import bounded_read_fd

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_NONCES_BYTES:
            return None
        raw = bounded_read_fd(fd, _MAX_NONCES_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    key = _stat_key(info)
    if len(raw) > _MAX_NONCES_BYTES:
        return key, {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return key, {}
    if not isinstance(data, dict) or data.get("version") != _NONCES_VERSION:
        return key, {}
    nonces = data.get("nonces")
    if not isinstance(nonces, dict):
        return key, {}
    return key, {
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


def publish_with_nonces(
    store_dir: Path,
    nonces: Mapping[str, str],
    stored: Mapping[str, str],
    write_store: Callable[[], None],
) -> None:
    """Write *nonces* (when they differ from *stored*), then call *write_store*.

    A MAC the store write publishes therefore always has its nonce on disk. A nonce
    write that fails raises before the store is touched. When *write_store* raises,
    *stored* is written back first, so the old store keeps the nonces its grants
    were signed with; a restore that also fails logs a warning, and the store
    write's own error is the one raised.
    """
    if nonces == stored:
        write_store()
        return
    write_nonces(store_dir, nonces)
    try:
        write_store()
    except BaseException:
        try:
            write_nonces(store_dir, stored)
        except Exception:
            logger.warning(
                "cron approval grant: the nonces were not put back after a failed store "
                "write; grants signed before it ask for each tool call until re-signed",
                exc_info=True,
            )
        raise


def attach_nonces(jobs: Iterable[Any], store_dir: Path) -> None:
    """Bind each job in *jobs* to *store_dir*'s nonces (``CronJob.grant_dir``), and
    give each that stores a grant the nonce it was loaded with and mark it held
    (``CronJob.grant_held``) when its MAC matches. Reads the nonces file once."""
    jobs = list(jobs)
    nonces = read_nonces(store_dir) if any(carries_grant(job) for job in jobs) else {}
    for job in jobs:
        job.grant_dir = str(store_dir)
        if carries_grant(job):
            job.grant_nonce = nonces.get(job.id, "")
            job.grant_held = holds(job, nonces)


def _current_nonces(obj: Any) -> Mapping[str, str]:
    """The nonces on disk now for a job bound to a store (``CronJob.grant_dir``).

    Never a job's cached ``grant_nonce``: a grant another process revoked must stop
    holding here even while this process has not reloaded the store. A raw record
    comes from the sandbox-writable store, so it has no nonce of its own.
    """
    directory = "" if isinstance(obj, dict) else _value(obj, "grant_dir")
    return read_nonces(Path(directory)) if directory else {}


def mac(obj: Any, nonces: Mapping[str, str] | None = None) -> str | None:
    """The hex MAC of *obj*'s signed fields and its nonce, or ``None`` with no key or nonce.

    The nonce is ``nonces[obj.id]``; without *nonces* it is read from the job's
    store's nonce file now, and a raw record without *nonces* has none.
    """
    key = _key()
    if key is None:
        return None
    if nonces is None:
        nonces = _current_nonces(obj)
    nonce = nonces.get(_value(obj, "id"), "")
    if not _NONCE_SHAPE.fullmatch(nonce):
        return None
    text = json.dumps(
        [*(_signed_value(obj, f) for f in _SIGNED_FIELDS), nonce],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hmac.new(key, text.encode("ascii"), hashlib.sha256).hexdigest()


def holds(obj: Any, nonces: Mapping[str, str] | None = None) -> bool:
    """Whether *obj* stores a grant AND the gateway signed exactly these fields and nonce.

    Without *nonces* a job's nonce is read from its store's nonce file (cached by
    the file's stat), so a revoked grant stops holding at the next check.
    """
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


def _signing_nonce(obj: Any, nonces: MutableMapping[str, str]) -> str:
    """The nonce *obj* is signed with, stored in *nonces*.

    The nonce stored for it is kept only while *obj*'s stored MAC already matches
    its fields under it, so nothing signed changed; any other change gets a new
    nonce, and every earlier MAC stops matching.
    """
    job_id = _value(obj, "id")
    nonce = nonces.get(job_id, "")
    stored = _value(obj, "approval_grant")
    if not (
        _NONCE_SHAPE.fullmatch(nonce)
        and _MAC_SHAPE.fullmatch(stored)
        and hmac.compare_digest(stored, mac(obj, {job_id: nonce}) or "")
    ):
        nonce = secrets.token_hex(16)
    nonces[job_id] = nonce
    return nonce


def sign(job: Any, nonces: MutableMapping[str, str]) -> None:
    """Store the gateway's MAC on *job* (or clear it when it carries no grant).

    *nonces* is the stored nonces (:func:`read_nonces`), updated here: a grant
    whose signed fields did not change keeps its nonce, any other grant gets a new
    one, and a job with no grant loses its nonce. The caller writes *nonces*
    before the store. A process with no persisted key writes "" here and leaves
    *nonces* as they are, so the job fires interactive. Marks *job* as held by
    this process (``CronJob.grant_held``).

    A job signed before (``grant_nonce`` set) whose nonce is gone from *nonces*, or
    differs, had its grant turned off or re-signed by another process: it is no
    longer held, keeps its stored MAC, and gets no nonce, so a save never brings a
    revoked grant back. A fresh grant (``grant_nonce == ""``) gets one.
    """
    if not carries_grant(job):
        nonces.pop(job.id, None)
        job.grant_nonce = ""
        job.approval_grant = ""
    elif _key() is None:
        job.approval_grant = ""
    elif job.grant_nonce and nonces.get(job.id) != job.grant_nonce:
        job.grant_held = False
        return
    else:
        job.grant_nonce = _signing_nonce(job, nonces)
        job.approval_grant = mac(job, nonces) or ""
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
    _signing_nonce(record, nonces)
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

            def _write_store() -> None:
                if signed:
                    atomic_write(path, json.dumps(data, indent=2))

            publish_with_nonces(store_dir, nonces, stored, _write_store)
        return signed
    except Exception:
        logger.warning("Stored cron approval grants were not signed", exc_info=True)
        return []
