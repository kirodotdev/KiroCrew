"""Grants kept across a gateway exit the user did not choose, and the user-stop mark.

A YOLO grant and a chat's Trust or Trust reads stay on across every exit the user
did not choose: any restart, an update's re-exec, a crash, a watchdog or OOM kill, a
host reboot, a stop the service manager sends on its own. Only a stop the user chose
ends them. This module is the store both kinds share:

* **The user-stop mark.** Every stop the user chooses (``kirocrew stop``, the desktop
  Quit, Ctrl+C on a foreground gateway, ``POST /api/shutdown`` with ``user_stop``)
  calls :func:`mark_user_stop` before the process goes. No other exit writes it.
* **The boot verdict.** :func:`boot_verdict` runs once per process image: with a mark
  present it deletes the mark and every record and answers ``"user_stop"``; without
  one it answers ``"kept"`` and the owners restore what their records hold.
* **The records.** One signed JSON file per kind (:data:`YOLO_RECORD`,
  :data:`CHATS_RECORD`), written by the owner that derives it from live state
  (``kept_yolo``, ``dashboard.chat_trust_restart``).

Everything lives in :data:`KEPT_GRANTS_LEAF` under the data home, a directory the
agent sandbox masks in every mode (``sandbox_leaves._CREW_HIDDEN_LEAVES``) and the
file tools refuse (``security.paths_lists``), so a sandboxed process can neither
write a record nor delete the mark. Each record also carries an HMAC-SHA256 keyed
from the dashboard token-signing key under a label of its own, so a record this
gateway did not write grants nothing: an unsigned, malformed, oversized or tampered
file is deleted, audited and read as absent. Nothing is written while that key is
the in-memory fallback, because no later boot could verify it.

The mark itself is unsigned on purpose: its only effect is to end grants.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.bounded_read import bounded_read_fd
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: The data-home directory holding the mark and the records. Masked by the sandbox.
KEPT_GRANTS_LEAF = "kept-grants"
#: Written by a stop the user chose; consumed by the next boot.
USER_STOP_FILE = "user-stop"
#: The YOLO grant that was live (``kept_yolo``).
YOLO_RECORD = "yolo.json"
#: The chats that held Trust or Trust reads (``dashboard.chat_trust_restart``).
CHATS_RECORD = "chats.json"

#: Boot verdicts.
VERDICT_USER_STOP = "user_stop"
VERDICT_KEPT = "kept"

_ENVELOPE_VERSION = 1
#: A record names a handful of chats or one grant; the bound keeps a file this module
#: did not write from making the boot read unbounded.
_MAX_RECORD_BYTES = 256 * 1024
#: Domain label for the record MAC, so no other signature made with the token-signing
#: key can be replayed as a record.
_MAC_LABEL = b"kirocrew/kept-grants/v1"
_MAC_HEX_LEN = 64

_verdict_lock = threading.Lock()
_verdict: str | None = None
#: The records a user stop cleared at this boot, by name, for their owners' audits.
_cleared: dict[str, dict[str, Any] | None] = {}


def kept_dir(home: Path | None = None) -> Path:
    """The directory under the data home (*home*, else the current one) for the mark and records."""
    return (config_dir() if home is None else home) / KEPT_GRANTS_LEAF


def _audit(operation: str, outcome: str, resources: str) -> None:
    """One SEL event. Never raises: an audit failure must not keep a grant."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="kept_grants",
            operation=operation,
            outcome=outcome,
            source="kept_grants",
            resources=resources,
        )
    except Exception:
        logger.warning("SEL audit failed for %s/%s", operation, outcome, exc_info=True)


def _ensure_dir(home: Path | None = None) -> Path | None:
    """The record directory, created owner-only. ``None`` when it is not a real directory.

    A link at the name is refused rather than followed, so nothing written here can
    land somewhere a sandboxed process could reach.
    """
    path = kept_dir(home)
    try:
        if path.is_symlink():
            logger.warning("kept grants: %s is a link; nothing is kept", path)
            return None
        # Never the parents: a data home that is gone (a removed home, a write landing
        # after its owner went away) must not be recreated just to hold a record.
        path.mkdir(mode=0o700, exist_ok=True)
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):
            logger.warning("kept grants: %s is not a directory; nothing is kept", path)
            return None
        try:
            # Owner-only even if it already existed: the records are signed grants.
            platform_compat.restrict_dir_to_owner(path)
        except OSError:
            pass
        return path
    except OSError:
        logger.warning("kept grants: could not prepare %s", path, exc_info=True)
        return None


def _read_bounded(path: Path) -> str | None:
    """The file's text, or ``None`` when there is no regular file under the bound.

    Opened by descriptor, non-blocking and without following a link, so a FIFO or a
    link planted at the name can neither hang the boot read nor redirect it. Windows
    has neither flag, so a link is refused there by the ``is_symlink`` check first.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        if path.is_symlink():
            return None
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RECORD_BYTES:
            return None
        return bounded_read_fd(fd, _MAX_RECORD_BYTES + 1).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _mac_key() -> bytes | None:
    """The record MAC key, or ``None`` when the token-signing key is not persisted."""
    try:
        from kiro_crew.dashboard.token_secret import get_persisted_secret

        secret = get_persisted_secret()
    except Exception:
        logger.debug("kept grants: no persisted signing key", exc_info=True)
        return None
    return hmac.new(secret, _MAC_LABEL, hashlib.sha256).digest()


def _signed_text(kind: str, body: dict[str, Any]) -> str:
    return json.dumps(
        {"version": _ENVELOPE_VERSION, "kind": kind, "body": body},
        sort_keys=True,
        separators=(",", ":"),
    )


def _mac(key: bytes, kind: str, body: dict[str, Any]) -> str:
    return hmac.new(key, _signed_text(kind, body).encode("utf-8"), hashlib.sha256).hexdigest()


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("kept grants: could not remove %s", path, exc_info=True)


def write_record(
    name: str,
    kind: str,
    body: dict[str, Any],
    *,
    home: Path | None = None,
    commit: tuple[threading.Lock, Callable[[], bool]] | None = None,
    on_commit: Callable[[], None] | None = None,
) -> bool:
    """Sign and write the *kind* record *name*. Returns whether it was written.

    *home* pins the data home for a write that runs on another thread later than the
    change it records. Without a persisted signing key the record is removed instead,
    so a later boot finds nothing rather than a record it cannot verify.

    *commit* is ``(lock, still_current)``: the file is written and synced under a
    temporary name first, and put in place only under *lock* and only while
    ``still_current()`` holds, so a revocation that took *lock* after this write
    sampled its body is never undone by it. *on_commit* runs under *lock* right after
    the rename, so a caller's record of what is on disk changes with the file itself.
    *lock* is held for that rename and *on_commit*, never across the write, so a
    thread that takes it waits for one syscall. Never raises.
    """
    try:
        key = _mac_key()
        directory = _ensure_dir(home)
        if directory is None:
            return False
        path = directory / name
        if key is None:
            _remove(path)
            return False
        envelope = {
            "version": _ENVELOPE_VERSION,
            "kind": kind,
            "body": body,
            "mac": _mac(key, kind, body),
        }
        text = json.dumps(envelope, sort_keys=True)
        if commit is None:
            atomic_write(path, text, mode=0o600)
            return True
        return _write_then_commit(path, text, *commit, on_commit=on_commit)
    except Exception:
        logger.warning("kept grants: could not write %s", name, exc_info=True)
        return False


def _write_then_commit(
    path: Path,
    text: str,
    lock: threading.Lock,
    still_current: Callable[[], bool],
    *,
    on_commit: Callable[[], None] | None = None,
) -> bool:
    """Write *text* beside *path*, then rename it over *path* under *lock* if still current."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    _remove(tmp)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        view = memoryview(text.encode("utf-8"))
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    with lock:
        if still_current():
            os.replace(tmp, path)
            if on_commit is not None:
                on_commit()
            return True
    _remove(tmp)
    return False


def remove_record(name: str, *, home: Path | None = None) -> None:
    """Delete record *name* under *home* (else the current data home), if any. Never raises."""
    try:
        path = kept_dir(home) / name
        if path.is_symlink() or path.exists():
            _remove(path)
    except Exception:
        logger.warning("kept grants: could not remove %s", name, exc_info=True)


def _reject(path: Path, name: str, reason: str) -> None:
    logger.warning("kept grants: discarding %s (%s)", name, reason)
    _audit("kept_grants:record_rejected", "denied", f"record:{name}, reason:{reason}")
    _remove(path)


def read_record(name: str, kind: str) -> dict[str, Any] | None:
    """The verified body of the *kind* record *name*, or ``None``.

    Fails closed: a link, a non-regular file, an oversized, unparseable or
    wrong-kind document, a missing or wrong MAC, or no persisted key to check it
    with all read as no record, and a present-but-rejected file is deleted and
    audited. Never raises.
    """
    try:
        path = kept_dir() / name
        if not (path.is_symlink() or path.exists()):
            return None
        text = _read_bounded(path)
        if text is None:
            _reject(path, name, "not_a_regular_file")
            return None
        try:
            envelope = json.loads(text)
        except ValueError:
            _reject(path, name, "unparseable")
            return None
        if not isinstance(envelope, dict) or envelope.get("version") != _ENVELOPE_VERSION:
            _reject(path, name, "wrong_version")
            return None
        body, mac = envelope.get("body"), envelope.get("mac")
        if envelope.get("kind") != kind or not isinstance(body, dict):
            _reject(path, name, "wrong_kind")
            return None
        if not isinstance(mac, str) or len(mac) != _MAC_HEX_LEN or not mac.isascii():
            _reject(path, name, "unsigned")
            return None
        key = _mac_key()
        if key is None:
            _reject(path, name, "no_signing_key")
            return None
        if not hmac.compare_digest(mac, _mac(key, kind, body)):
            _reject(path, name, "bad_signature")
            return None
        return body
    except Exception:
        logger.warning("kept grants: could not read %s", name, exc_info=True)
        return None


def mark_user_stop(via: str) -> bool:
    """Record that the user chose to stop Kiro Crew, before the process goes.

    *via* names the path for the audit (``cli``, ``desktop``, ``ctrl_c``, ...).
    Returns whether the mark is on disk. Never raises: a stop must go ahead even when
    the mark cannot be written, and the grants are then kept as for any other exit.
    """
    try:
        directory = _ensure_dir()
        if directory is None:
            return False
        payload = json.dumps({"version": 1, "via": str(via)[:32], "at": time.time()})
        atomic_write(directory / USER_STOP_FILE, payload, mode=0o600)
    except Exception:
        logger.warning("kept grants: could not write the user-stop mark", exc_info=True)
        return False
    _audit("kept_grants:user_stop", "recorded", f"via:{str(via)[:32]}")
    return True


def withdraw_user_stop() -> None:
    """Remove a mark whose stop stopped nothing (the CLI found no gateway to stop)."""
    try:
        _remove(kept_dir() / USER_STOP_FILE)
    except Exception:
        logger.debug("kept grants: could not withdraw the user-stop mark", exc_info=True)


def port_in_this_home(port: int) -> bool:
    """Whether the gateway on *port* uses this data home, so a stop's mark reaches it.

    Proven live, never from a file alone: the pid this data home's ``run/`` records
    for *port* must itself hold the port's listener and be this user's gateway
    (``port_resolution._gateway_owns_port``). A credential a crashed gateway left
    behind proves nothing. Never raises; anything unproven counts as another home.
    """
    try:
        from kiro_crew.port_resolution import _gateway_owns_port

        return bool(_gateway_owns_port(int(port)))
    except Exception:
        return False


def run_user_stop(stop: Callable[[], None], *, via: str) -> None:
    """Run *stop*, a stop the user asked for, with the user-stop mark written first.

    The mark goes down before anything is signalled, so a gateway that exits at once
    still finds it at its next boot. A stop that exits non-zero stopped nothing
    (no gateway, a refusal, a denied signal), so its mark is taken back and the next
    boot keeps the grants as after any other exit.
    """
    # A mark already down is an earlier stop's, still unread by a boot: a repeat stop
    # that finds nothing to stop must leave it.
    earlier = (kept_dir() / USER_STOP_FILE).exists()
    marked = mark_user_stop(via)
    try:
        stop()
    except SystemExit as exc:
        if marked and not earlier and exc.code not in (0, None):
            withdraw_user_stop()
        raise


def _mark_via(path: Path) -> str:
    text = _read_bounded(path)
    if text is None:
        return "unknown"
    try:
        data = json.loads(text)
    except ValueError:
        return "unknown"
    via = data.get("via") if isinstance(data, dict) else None
    return via[:32] if isinstance(via, str) and via.isascii() else "unknown"


def boot_verdict() -> str:
    """Whether this boot follows a stop the user chose. Decided once per process image.

    The first call consumes the mark: with one present it deletes the mark and both
    records, audits the clear and answers :data:`VERDICT_USER_STOP`, so nothing is
    restored. Otherwise it answers :data:`VERDICT_KEPT`. An exec starts a new image,
    so an in-place restart decides again. Never raises; a mark that cannot be read is
    still a user stop.
    """
    global _verdict
    with _verdict_lock:
        if _verdict is not None:
            return _verdict
        verdict = VERDICT_KEPT
        try:
            mark = kept_dir() / USER_STOP_FILE
            if mark.is_symlink() or mark.exists():
                via = _mark_via(mark)
                verdict = VERDICT_USER_STOP
                # Read before the delete, so each owner can audit what the stop
                # ended (:func:`cleared_record`); deleted here, so a boot whose owner
                # never runs still leaves nothing for a later boot to restore.
                for name, kind in ((YOLO_RECORD, "yolo"), (CHATS_RECORD, "chats")):
                    _cleared[name] = read_record(name, kind)
                    remove_record(name)
                _remove(mark)
                _audit("kept_grants:cleared", "disabled", f"reason:user_stop, via:{via}")
        except Exception:
            logger.warning("kept grants: could not read the user-stop mark", exc_info=True)
        _verdict = verdict
        return verdict


def cleared_record(name: str) -> dict[str, Any] | None:
    """The verified body a user stop cleared from record *name* at this boot, if any."""
    with _verdict_lock:
        return _cleared.get(name)


def _reset_for_tests() -> None:
    """Forget the boot verdict, so the next call decides again."""
    global _verdict
    with _verdict_lock:
        _verdict = None
        _cleared.clear()
