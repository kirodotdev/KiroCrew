"""The chats the OWNER trusted, kept so a gateway restart does not forget them.

"Trust this session" auto-approves every tool call in one chat. The grant is a
person's click and it has no expiry, so a restart nobody asked for -- a crash,
an OOM kill, the loop-stall watchdog -- must not take it away: unattended work in
a trusted chat would otherwise stop on approval prompts nobody is there to
answer. A stop the owner DID ask for clears it (``clear``, called from the
gateway's shutdown path), so a deliberate restart is still a re-consent point.

What is kept, and what is deliberately not:

* Only a grant the owner made from their own dashboard session is written here.
  Trust handed to an app worker, a subagent, a cron run, a crew member or a
  session the agent opened is derived from something else that already ends on
  its own, and persisting it would outlive the thing it came from.
* The value is a set of SESSION keys -- the same ``effective_session_key`` the
  live grant and its revoke address -- and nothing else. Restoring one sets the
  same flag the click sets, so every gate that decides a tool call on the live
  path (deny lists, ``human_only`` cards, the governance ceiling) decides it the
  same way after a restart.

Why a sealed leaf of its own rather than a field on the chat's history line: a
transcript line is ordinary agent-reachable state, so a trust bit read back from
it would let a prompt-injected agent grant itself trust by editing its own
transcript. The ``session-trust`` directory is masked in the sandbox
(``sandbox._CREW_HIDDEN_LEAVES``: no in-sandbox code reads it) and write-protected
at the file-edit gate (``security.paths._CREW_SECRET_LEAVES``, read AND write); only the
gateway reads or writes it.

Every read failure restores NOTHING -- an absent, empty, unreadable, malformed
or wrong-version file all mean "no chat is trusted", the safe direction -- and
the caller is told which case it hit so it can say so to the owner. A revoke that
cannot be written removes the whole file rather than leave a revoked grant on
disk for the next boot to restore.

Every function here reads or writes the disk: call them off the event loop.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard import token_secret

logger = logging.getLogger(__name__)

#: Crew-home directory holding the store. The directory, not the file inside it,
#: is the sandbox mask, because the file is republished by rename.
STORE_DIR = "session-trust"
STORE_LEAF = f"{STORE_DIR}/grants.json"

#: Schema marker. A document without exactly this version trusts no chat. Version 2
#: is the first SIGNED format: a version-1 file was never written by a released
#: gateway, so any found at boot was planted and is refused by this check alone.
_VERSION = 2

#: Domain for the record's MAC key, derived from the dashboard token-signing key
#: (``token_secret._get_secret``) -- the secret ``chat_tag_grants`` certifies its
#: store key under, because it is unreadable and unwritable from every agent plane.
#: NOT the SEL trust root: ``trust/sel_hmac.key`` is read inside the sandbox by
#: design, so a MAC keyed from it is forgeable by anything that can run there.
_SUBKEY_DOMAIN = b"kirocrew.session_trust.grants.v2"


def _signing_root() -> bytes | None:
    """The gateway-only secret records are signed under, or None when unavailable.

    None also when that secret is the ephemeral in-memory fallback rather than
    the key on disk: a record signed under it verifies in no later boot, so
    nothing is saved and the grant stays live-only rather than being published
    as durable and silently dropped by the next crash.
    """
    try:
        secret = token_secret._get_secret()
        persisted = token_secret.secret_is_persisted()
    except Exception:
        logger.warning("session trust: token-signing key unavailable", exc_info=True)
        return None
    if not secret:
        return None
    if not persisted:
        logger.warning("session trust: token-signing key is not persisted; not saving trust")
        return None
    return secret


def _mac(sessions: list[object]) -> str | None:
    """MAC over the exact stored session list, or None with no key to sign by.

    The key is out of every agent's reach, so a record an agent wrote -- through a
    path the fences miss, or on an older build before the leaf was fenced at all --
    carries no MAC that verifies and restores nothing. A token-key rotation
    invalidates every record, which fails closed: trust is simply not restored.
    """
    root = _signing_root()
    if root is None:
        return None
    subkey = hmac.new(root, _SUBKEY_DOMAIN, hashlib.sha256).digest()
    body = json.dumps(sessions, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(subkey, body.encode("utf-8"), hashlib.sha256).hexdigest()


#: Upper bound on remembered chats. The oldest grant drops first when a new one
#: would exceed it, which bounds the file and every read of it.
MAX_SESSIONS = 512

#: Upper bound on one stored session key. A longer key is never stored, so its
#: chat simply is not restored.
MAX_KEY_CHARS = 512

#: Serializes every read-modify-write. Every writer runs in the gateway process.
_LOCK = threading.Lock()

_path_override: Path | None = None


def store_path() -> Path:
    """``<crew home>/`` :data:`STORE_LEAF`, following ``KIROCREW_HOME``."""
    if _path_override is not None:
        return _path_override
    return config_dir() / STORE_LEAF


@dataclass(frozen=True)
class TrustSnapshot:
    """What the store says, and whether it could be read at all.

    ``readable`` is False only when a file exists and could not be trusted
    (unreadable, not JSON, wrong shape or version). An absent file is readable
    and empty: there is simply nothing to restore.
    """

    sessions: tuple[str, ...] = ()
    readable: bool = True

    def holds(self, session_key: str) -> bool:
        return session_key in self.sessions


def valid_key(key: object) -> bool:
    """A key the store will hold: a non-empty printable string within the bound."""
    return (
        isinstance(key, str)
        and 0 < len(key) <= MAX_KEY_CHARS
        and key.isprintable()
        and key == key.strip()
    )


def _parse(raw: object) -> TrustSnapshot:
    if not isinstance(raw, dict) or raw.get("version") != _VERSION:
        return TrustSnapshot(readable=False)
    sessions = raw.get("sessions")
    if not isinstance(sessions, list):
        return TrustSnapshot(readable=False)
    expected = _mac(sessions)
    stated = raw.get("mac")
    if (
        expected is None
        or not isinstance(stated, str)
        # compare_digest raises TypeError on a non-ASCII str; a MAC this module
        # wrote is always lowercase hex, so anything else is simply not one.
        or not stated.isascii()
        or not hmac.compare_digest(expected, stated)
    ):
        # Unsigned, forged, edited, or no trust root to check it against: none of
        # these is a grant the gateway wrote, so none restores.
        return TrustSnapshot(readable=False)
    kept: list[str] = []
    for key in sessions:
        # One off-shape entry does not poison the rest: it is dropped, and every
        # well-formed grant beside it still restores.
        if valid_key(key) and key not in kept:
            kept.append(key)
    return TrustSnapshot(sessions=tuple(kept[-MAX_SESSIONS:]))


#: Ceiling on the record's size. MAX_SESSIONS keys of MAX_KEY_CHARS each, JSON
#: framed, is well under this; anything larger is not a record this module wrote
#: and is refused before it is read into memory.
_MAX_RECORD_BYTES = 2 * 1024 * 1024


def _check_store_dir(directory: Path, *, create: bool) -> None:
    """Raise OSError unless *directory* is a real directory, never a link.

    The record's own name is opened without following a link, but every path
    operation also walks its parent: a ``session-trust`` link planted by an older
    build, before the leaf was sealed, would redirect every read, write and
    remove to wherever it points. FileNotFoundError when it is absent and
    *create* is False, so a missing store still reads as empty.
    """
    if create:
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
    info = os.lstat(directory)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError("session trust store directory is not a real directory")


def _read_record(target: Path) -> str:
    """The record's text: no symlink followed, regular files only, bounded.

    Raises FileNotFoundError when absent and OSError for anything else this
    module did not write (a link -- for the record or its directory -- a
    directory, a FIFO, an oversized file), so a planted giant or a link to
    elsewhere is refused before a byte is buffered.
    """
    _check_store_dir(target.parent, create=False)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(target, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("session trust record is not a regular file")
        if info.st_size > _MAX_RECORD_BYTES:
            raise OSError("session trust record is larger than any record written")
        data = os.read(fd, _MAX_RECORD_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > _MAX_RECORD_BYTES:
        raise OSError("session trust record is larger than any record written")
    return data.decode("utf-8")


def load(path: Path | None = None) -> TrustSnapshot:
    """Read the store. Every failure trusts no chat and reports ``readable=False``."""
    target = path or store_path()
    try:
        text = _read_record(target)
    except FileNotFoundError:
        return TrustSnapshot()
    except (OSError, UnicodeDecodeError):
        logger.warning("session trust store unreadable; restoring trust to no chat")
        return TrustSnapshot(readable=False)
    if not text.strip():
        return TrustSnapshot()
    try:
        raw = json.loads(text)
    except ValueError:
        logger.warning("session trust store is not valid JSON; restoring trust to no chat")
        return TrustSnapshot(readable=False)
    snapshot = _parse(raw)
    if not snapshot.readable:
        logger.warning("session trust store has an unknown shape; restoring trust to no chat")
    return snapshot


def _write(path: Path, sessions: list[str]) -> None:
    mac = _mac(list(sessions))
    if mac is None:
        # Nothing to sign with: an unsigned record would never restore, so it is
        # not written at all, and the caller treats it as a failed save.
        raise OSError("no token-signing key to sign the session trust record")
    _check_store_dir(path.parent, create=True)
    doc = {"version": _VERSION, "sessions": sessions, "mac": mac}
    atomic_write(path, json.dumps(doc, indent=2) + "\n", mode=0o600)


def grant_counting(
    keys: Iterable[str], *, drop: Iterable[str] = (), replace: bool = False
) -> tuple[bool, int]:
    """Remember *keys* as owner-trusted; also say how many old grants the bound dropped.

    Returns ``(saved, dropped)``: ``saved`` is True when the store now holds the keys.
    A store that could not be read is replaced: it restored nothing anyway, and
    a click the owner just made is the freshest statement of what they want.
    *drop* names keys a revoke withdrew but could not remove, and *replace*
    says an all-chats revoke could not clear the store: either way the rewrite
    this grant makes must not carry those grants forward. The count lets the
    caller tell the owner, because a dropped grant is a chat that will come back
    untrusted after a crash.
    """
    wanted = [k for k in dict.fromkeys(keys) if valid_key(k)]
    if not wanted:
        return False, 0
    withdrawn = set(drop)
    path = store_path()
    with _LOCK:
        on_disk = list(load(path).sessions)
        current = [] if replace else [k for k in on_disk if k not in withdrawn]
        merged = [k for k in current if k not in wanted] + wanted
        dropped = max(0, len(merged) - MAX_SESSIONS)
        merged = merged[-MAX_SESSIONS:]
        if merged == on_disk:
            return True, 0
        try:
            _write(path, merged)
        except OSError:
            logger.warning(
                "could not persist session trust; it holds until the next restart",
                exc_info=True,
            )
            return False, 0
    if dropped:
        logger.warning("session trust store full: dropped %d oldest grant(s)", dropped)
    return True, dropped


def revoke(keys: Iterable[str]) -> bool:
    """Forget *keys*. True when no revoked key can come back on the next boot.

    Fails CLOSED: when the rewrite cannot be written the whole file is removed,
    dropping every remembered grant, because a revoked grant left on disk is one
    the next restart would hand back.
    """
    dropped = set(dict.fromkeys(keys))
    path = store_path()
    with _LOCK:
        snapshot = load(path)
        if snapshot.readable and not dropped.intersection(snapshot.sessions):
            return True
        return _rewrite_or_remove(path, [k for k in snapshot.sessions if k not in dropped])


def clear() -> bool:
    """Forget every remembered grant. Same fail-closed contract as :func:`revoke`."""
    path = store_path()
    with _LOCK:
        if not path.exists():
            return True
        return _rewrite_or_remove(path, [])


#: Written by an owner stop whose :func:`clear` failed. Its presence at boot means
#: "the owner stopped me; restore nothing", so deleting it would hand back trust
#: the stop withdrew: it lives in a sealed leaf of its own (hidden in the sandbox
#: and refused at the file-edit gate, like the store), and a different directory
#: from the store so a store directory that refuses writes does not take it down.
MARKER_DIR = "session-trust-stop"
OWNER_STOP_MARKER = f"{MARKER_DIR}/owner-stop"


def _marker_path() -> Path:
    if _path_override is not None:
        return _path_override.parent.parent / OWNER_STOP_MARKER
    return config_dir() / OWNER_STOP_MARKER


def mark_owner_stop() -> bool:
    """Record that an owner stop could not clear the store. True when written."""
    path = _marker_path()
    try:
        _check_store_dir(path.parent, create=True)
        atomic_write(path, "owner-stop\n", mode=0o600)
        return True
    except OSError:
        logger.error("could not record the owner stop for session trust", exc_info=True)
        return False


def owner_stop_marked() -> bool:
    """True when a previous owner stop left :data:`OWNER_STOP_MARKER`. Fails closed.

    Read without following a link, for the marker and for its directory: a
    directory that is a link (even a dangling one), not a directory, or cannot
    be inspected reads as MARKED, since "unmarked" is the answer that hands
    saved trust back. Only a cleanly absent directory or marker reads unmarked.
    """
    directory = _marker_path().parent
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return True
    try:
        os.lstat(_marker_path())
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def withdraw_owner_stop() -> bool:
    """Remove a marker an owner action wrote but did not carry out. True when gone.

    For the CLI restart, which marks the stop before it starts and must take the
    mark back when it refuses or fails and the running gateway keeps serving:
    left behind, it would make the next crash boot discard grants that gateway
    goes on to make.
    """
    try:
        _check_store_dir(_marker_path().parent, create=False)
        os.remove(_marker_path())
    except FileNotFoundError:
        return True
    except OSError:
        logger.warning("could not withdraw the session trust owner-stop marker", exc_info=True)
        return False
    return True


def owner_stop_marked_since(started: float) -> bool:
    """True when the marker was written at or after *started*. Fails closed.

    A marker older than this process is a previous run's stop, left to settle. A
    newer one is a stop aimed at this run -- the CLI marks before it ends the
    gateway from outside -- so it is still in progress, and must not be settled
    by a grant that would then outlive it.
    """
    try:
        return os.lstat(_marker_path()).st_mtime >= started
    except FileNotFoundError:
        return False
    except OSError:
        return True


def settle_owner_stop(active_since: float | None = None) -> bool:
    """Clear the store, then remove the marker -- in that order. Only while marked.

    The marker is what keeps a surviving record from being restored, so it is
    removed only once :func:`clear` has succeeded. True when both are done, or
    when there is no marker to settle (nothing is cleared then); a False leaves
    the marker for the next boot to try again. Serialized, and the marker is
    re-read inside: a settle that runs after another one finished -- a late boot
    restore after a grant settled first -- finds no marker and clears nothing,
    so it cannot erase what was granted in between. With *active_since*, a marker
    written at or after it is a stop still in progress: it is left in place and
    the answer is False.
    """
    with _SETTLE_LOCK:
        if not owner_stop_marked():
            return True
        if active_since is not None and owner_stop_marked_since(active_since):
            return False
        if not clear():
            return False
        try:
            _check_store_dir(_marker_path().parent, create=False)
            os.remove(_marker_path())
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("could not remove the session trust owner-stop marker", exc_info=True)
            return False
        return True


#: Serializes :func:`settle_owner_stop`. Separate from ``_LOCK``, which
#: :func:`clear` takes inside it.
_SETTLE_LOCK = threading.Lock()


def _rewrite_or_remove(path: Path, remaining: list[str]) -> bool:
    """Write *remaining*, or remove the file when that write fails. Hold ``_LOCK``."""
    try:
        _write(path, remaining)
        return True
    except OSError:
        logger.warning("could not rewrite session trust store; removing it", exc_info=True)
    try:
        # Never remove through a planted directory link: that would delete a file
        # elsewhere and report the store gone while the real one stays.
        _check_store_dir(path.parent, create=False)
        os.remove(path)
    except FileNotFoundError:
        return True
    except OSError:
        logger.error(
            "could not remove the session trust store; a revoked grant may be "
            "restored on the next boot",
            exc_info=True,
        )
        return False
    return True
