"""Display-redaction verdicts stored beside each transcript, so a reopen skips the scan.

The slot-detail render runs the display battery (``redact_exfiltration_urls`` then
``redact_credentials``) over every non-user string of a session. The in-memory
verdict cache in ``chat_utils_display`` makes a repeat render in the same process cheap,
but a gateway restart, or the first open of a long session, still pays the whole
scan. This module keeps the verdicts on disk:

* when a slot save commits, the rows it newly wrote are projected the way the
  read path projects them, inside a :func:`session` scope, and every verdict is
  appended to the session's sidecar;
* when a render misses the in-memory cache, the sidecar is read once and its
  verdicts are used; a string still missing is scanned and its verdict appended.

The transcript itself is never touched: verdicts live in ``display-verdicts/``
under the data home, two files per session named after the transcript stem.

Trust. A verdict is only ever looked up by its TAG, a 128-bit slice of
``HMAC(K, "tag" || 0 || rules_version || len(text) || text || 0 || exempt hosts)``. ``K`` is a
subkey of the dashboard token-signing secret, which the agent sandbox masks and
the file tools fence, so a same-uid agent that can write the sidecar still
cannot mint a tag for text it chose: a "clean" tag can only come from this
process having scanned exactly that text under exactly these rules. A stored
redacted output carries a MAC binding it to its tag. Anything unreadable,
malformed, truncated or failing its MAC reads as absent, which means "scan".

Invalidation. The rules version is a digest of the battery's own source files
(``dashboard.display_battery.RULE_SOURCES``) plus the native gate fingerprint,
the Python and Unicode versions and the host's darwin directory id; the exempt
hosts (companion-supplied and per-workspace allowed) are part of every tag. Any
change makes every old tag unreachable, so there is no separate invalidation
step to forget, while a release that leaves the rules alone keeps its verdicts.
The version is also written in each file header so a stale file is truncated on
its next append rather than grown.

Off unless :func:`enable` succeeded (the gateway calls it once at boot). Without
it, and inside an owner-view scope that bypasses the credential pass, nothing is
read or written and the caller falls back to its per-process salt.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import logging
import os
import queue
import stat
import sys
import threading
import unicodedata
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

DIR_NAME = "display-verdicts"
_MAGIC = b"KCDV0002"
_TAG = 16
_VER = 16
_HEADER = len(_MAGIC) + _VER
_CLEAN_SUFFIX = ".dvc"
_REDACTED_SUFFIX = ".dvr"
#: A sidecar over this size is truncated and starts over (its verdicts are only
#: an optimisation; losing them costs one rescan).
MAX_FILE_BYTES = 16 * 1024 * 1024
#: The whole directory over this size is cleared at boot.
MAX_DIR_BYTES = 512 * 1024 * 1024
_REDACTED_MAX_ENTRY = 1024 * 1024
#: Clean tags one sidecar can hold; a render retains no more than this.
_NEW_CLEAN_MAX = (MAX_FILE_BYTES - _HEADER) // _TAG
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _NONBLOCK
#: Process-cache misses a read render scans before it reads the sidecar.
LOAD_AFTER_MISSES = 64
#: Misses after which a render that has missed the process cache more often
#: than it hit (a cold open) reads the sidecar. Parsing costs ~10-20 ns per
#: sidecar byte, a scan ~40 us per string, so a cold open pays it back at once.
COLD_MISSES = 4
#: A sidecar no bigger than this many bytes per miss so far is read too: at
#: ~10-20 ns per byte it costs well under the ~40 us scans already paid.
LOAD_BYTES_PER_MISS = 1024
_SUBKEY_DOMAIN = b"kirocrew/display-verdict/v1"
_HMAC_KEY_MIN_BYTES = 32

CLEAN = object()


class _Persist:
    def __init__(self, key: bytes, version: bytes, root: Path) -> None:
        self.key = key
        self.version = version
        self.root = root
        #: HMAC prefix state: copied per string so the per-call cost is one
        #: update over the text, not a re-key.
        self.mac_base = hmac.new(key, b"tag\0" + version, hashlib.sha256)
        self.locks: dict[str, threading.Lock] = {}
        self.locks_guard = threading.Lock()

    def lock_for(self, name: str) -> threading.Lock:
        with self.locks_guard:
            lock = self.locks.get(name)
            if lock is None:
                if len(self.locks) > 4096:
                    self.locks.clear()
                lock = self.locks[name] = threading.Lock()
            return lock


_persist: _Persist | None = None


# --------------------------------------------------------------------- setup
def rules_version() -> bytes:
    """Every input the battery's output depends on other than the text and hosts.

    The battery's own source files (``display_battery.RULE_SOURCES``), not the
    whole package: a release that leaves the redaction rules alone keeps every
    stored verdict. Raises when the files on disk differ from the code this
    process loaded, so a checkout edited under a running gateway stores nothing.
    """
    from kiro_crew.dashboard import display_battery
    from kiro_crew.security import redaction

    source = display_battery.rule_source_digest()
    if display_battery.LOADED_DIGEST is None or source != display_battery.LOADED_DIGEST:
        raise RuntimeError("redaction sources changed since they were loaded")

    native = b"off"
    # Read with getattr: a build without the native gate has no such attribute.
    gate = getattr(redaction, "_NATIVE_GATE", None)
    if gate is not None:
        # The native gate is a prefilter whose "no credential" answer skips the
        # battery, so its BINARY is a rules input: hash the loaded extension.
        native = getattr(redaction, "_native_gate_fingerprint")().encode()
        mod = sys.modules.get(getattr(gate, "__module__", "") or "")
        path = getattr(mod, "__file__", None)
        if not path:
            raise RuntimeError("native gate loaded from an unknown file")
        with open(path, "rb") as fh:
            native += b":" + hashlib.file_digest(fh, "sha256").digest()
    parts = [
        b"rules=" + source,
        b"native=" + native,
        b"py=" + sys.version.encode("utf-8", "surrogatepass"),
        b"ucd=" + unicodedata.unidata_version.encode(),
        b"darwin=" + str(redaction._host_darwin_user_dir_id()).encode("utf-8", "surrogatepass"),
    ]
    return hashlib.sha256(b"\0".join(parts)).digest()[:_VER]


def _derive_key() -> bytes | None:
    from kiro_crew.dashboard import token_secret

    try:
        raw = token_secret._get_secret()
    except OSError:
        return None
    if len(raw) < _HMAC_KEY_MIN_BYTES:
        return None
    return hmac.new(raw, _SUBKEY_DOMAIN, hashlib.sha256).digest()


_on_enable: list = []


def on_enable(callback) -> None:  # type: ignore[no-untyped-def]
    """Run *callback* once persistence turns on (the cache owner clears its salt-keyed entries)."""
    _on_enable.append(callback)


def enable(data_home: Path, sessions_dir: Path | None = None) -> bool:
    """Turn persistence on for this process. Blocking: call off the event loop.

    Returns False (and stays off) when the key cannot be loaded or the
    directory cannot be made safe. Safe to call twice.
    """
    global _persist
    if _persist is not None:
        return True
    try:
        if sessions_dir is None:
            from kiro_crew.history import _sessions_dir

            sessions_dir = _sessions_dir()
        key = _derive_key()
        if key is None:
            return False
        version = rules_version()
        from kiro_crew.platform_compat import effective_uid

        uid = effective_uid()
        if uid is None:
            # No uid to prove the directory is ours: stay off on this platform.
            return False
        root = data_home / DIR_NAME
        root.mkdir(mode=0o700, exist_ok=True)
        # Everything after the check goes through ONE no-follow descriptor: a
        # path re-resolved after an lstat could follow a directory swapped in
        # between, and the sweep below deletes files.
        dir_fd = _open_root(root)
        if dir_fd is None:
            logger.warning("display-verdicts: %s is not an owned directory; staying off", root)
            return False
        try:
            st = os.fstat(dir_fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
                logger.warning("display-verdicts: %s is not an owned directory; staying off", root)
                return False
            os.fchmod(dir_fd, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
            _sweep_fd(dir_fd, sessions_dir)
        finally:
            os.close(dir_fd)
    except Exception:
        logger.warning("display-verdicts: could not enable", exc_info=True)
        return False
    _persist = _Persist(key, version, root)
    for callback in tuple(_on_enable):
        try:
            callback()
        except Exception:
            logger.debug("display-verdicts: on-enable hook failed", exc_info=True)
    return True


def disable_for_tests() -> None:
    global _persist
    _persist = None


def enabled() -> bool:
    return _persist is not None


def _open_root(root: Path) -> int | None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(root, flags)
    except OSError:
        return None


def _sweep(root: Path, sessions_dir: Path | None) -> None:
    """Drop sidecars whose transcript is gone; clear everything when over budget."""
    dir_fd = _open_root(root)
    if dir_fd is None:
        return
    try:
        _sweep_fd(dir_fd, sessions_dir)
    finally:
        os.close(dir_fd)


def _sweep_fd(dir_fd: int, sessions_dir: Path | None) -> None:
    """:func:`_sweep` on an already opened, validated directory descriptor.

    Both scans and every delete are relative to *dir_fd*, so they act on the
    directory that was checked even if its path now names something else.
    """
    total = 0
    # Streamed: orphans go as they are seen, and nothing per file is kept, so a
    # directory of many tiny sidecars costs no memory to sweep.
    with os.scandir(dir_fd) as it:
        for e in it:
            name = e.name
            if not name.endswith((_CLEAN_SUFFIX, _REDACTED_SUFFIX)):
                continue
            if not e.is_file(follow_symlinks=False):
                continue
            stem = name.rsplit(".", 1)[0]
            if sessions_dir is not None and not (sessions_dir / f"{stem}.jsonl").exists():
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=dir_fd)
                continue
            with contextlib.suppress(OSError):
                total += e.stat(follow_symlinks=False).st_size
    if total <= MAX_DIR_BYTES:
        return
    with os.scandir(dir_fd) as it:
        for e in it:
            if e.name.endswith((_CLEAN_SUFFIX, _REDACTED_SUFFIX)) and e.is_file(
                follow_symlinks=False
            ):
                with contextlib.suppress(OSError):
                    os.unlink(e.name, dir_fd=dir_fd)


# ---------------------------------------------------------------- key / tag
def mac_base() -> hmac.HMAC | None:
    """The keyed MAC prefix to copy for a persistent cache key, or None (use the salt)."""
    p = _persist
    if p is None:
        return None
    from kiro_crew.security.redaction_switch import credential_pass_bypassed

    if credential_pass_bypassed():
        return None
    return p.mac_base


def _out_mac(p: _Persist, tag: bytes, out: bytes) -> bytes:
    return hmac.new(p.key, b"out\0" + tag + out, hashlib.sha256).digest()[:_TAG]


# -------------------------------------------------------------- file access
def _safe_name(history_key: str) -> str | None:
    from kiro_crew.history import _safe_key

    name = _safe_key(history_key)
    if not name or "/" in name or name.startswith("."):
        return None
    return name


def _open_dir(p: _Persist) -> int | None:
    return _open_root(p.root)


def _read_file(dir_fd: int, name: str) -> bytes | None:
    try:
        # O_NONBLOCK: a FIFO planted under the name must not block the open.
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX_FILE_BYTES:
            return None
        chunks = []
        remaining = st.st_size
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 4 * 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _load(p: _Persist, name: str) -> tuple[set[bytes], dict[bytes, str]]:
    clean: set[bytes] = set()
    redacted: dict[bytes, str] = {}
    dir_fd = _open_dir(p)
    if dir_fd is None:
        return clean, redacted
    try:
        data = _read_file(dir_fd, name + _CLEAN_SUFFIX)
        if data and data[: len(_MAGIC)] == _MAGIC and data[len(_MAGIC) : _HEADER] == p.version:
            end = _HEADER + (len(data) - _HEADER) // _TAG * _TAG
            mv = memoryview(data)
            clean = {bytes(mv[i : i + _TAG]) for i in range(_HEADER, end, _TAG)}
        data = _read_file(dir_fd, name + _REDACTED_SUFFIX)
        if data and data[: len(_MAGIC)] == _MAGIC and data[len(_MAGIC) : _HEADER] == p.version:
            i = _HEADER
            n = len(data)
            while i + _TAG + 4 <= n:
                tag = data[i : i + _TAG]
                size = int.from_bytes(data[i + _TAG : i + _TAG + 4], "big")
                j = i + _TAG + 4
                if size > _REDACTED_MAX_ENTRY or j + size + _TAG > n:
                    break
                out = data[j : j + size]
                mac = data[j + size : j + size + _TAG]
                i = j + size + _TAG
                if not hmac.compare_digest(mac, _out_mac(p, tag, out)):
                    continue
                try:
                    redacted[tag] = out.decode("utf-8", "surrogatepass")
                except UnicodeDecodeError:
                    continue
    finally:
        os.close(dir_fd)
    return clean, redacted


def _fit(records: list[bytes], room: int) -> bytes:
    """The leading whole records that fit in *room* bytes, joined."""
    taken = []
    for rec in records:
        if len(rec) > room:
            break
        taken.append(rec)
        room -= len(rec)
    return b"".join(taken)


def _append(p: _Persist, name: str, suffix: str, records: list[bytes]) -> None:
    # Only whole records are written, and never past MAX_FILE_BYTES: a file over
    # the cap is refused by _read_file, which would turn the cache off for that
    # session for good (every reopen would rescan and rewrite it).
    if not records:
        return
    dir_fd = _open_dir(p)
    if dir_fd is None:
        return
    try:
        fname = name + suffix
        with p.lock_for(fname):
            from kiro_crew import platform_compat

            fd = platform_compat.open_create_or_existing(
                fname, os.O_RDWR | _NOFOLLOW | _NONBLOCK, 0o600, dir_fd=dir_fd
            )
            try:
                st = os.fstat(fd)
                # O_NOFOLLOW stops symlinks, not hard links: a sidecar name
                # hard-linked to another file (a transcript) would be truncated
                # below. A sidecar this module created has exactly one link.
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    return
                head = os.pread(fd, _HEADER, 0) if st.st_size >= _HEADER else b""
                total = sum(len(r) for r in records)
                fresh = head != _MAGIC + p.version or st.st_size + total > MAX_FILE_BYTES
                payload = _fit(records, MAX_FILE_BYTES - (_HEADER if fresh else st.st_size))
                if not payload:
                    return
                if fresh:
                    os.ftruncate(fd, 0)
                    os.pwrite(fd, _MAGIC + p.version, 0)
                    size = _HEADER
                else:
                    # A torn record at the tail (a crash mid-append) would misalign
                    # every fixed-size record after it; cut it before appending.
                    size = st.st_size
                    if suffix == _CLEAN_SUFFIX:
                        size = _HEADER + (size - _HEADER) // _TAG * _TAG
                        if size != st.st_size:
                            os.ftruncate(fd, size)
                os.pwrite(fd, payload, size)
            finally:
                os.close(fd)
    except OSError:
        logger.debug("display-verdicts: append failed for %s", name, exc_info=True)
    finally:
        os.close(dir_fd)


# -------------------------------------------------------------------- scope
class _Scope:
    __slots__ = (
        "clean",
        "hits",
        "loaded",
        "misses",
        "name",
        "new_clean",
        "new_red",
        "p",
        "record_all",
        "red_bytes",
        "redacted",
        "size",
    )

    def __init__(self, p: _Persist, name: str, record_all: bool) -> None:
        self.p = p
        self.name = name
        self.loaded = False
        self.misses = 0
        self.hits = 0
        self.size: int | None = None
        self.clean: set[bytes] = set()
        self.redacted: dict[bytes, str] = {}
        self.new_clean: dict[bytes, None] = {}
        self.new_red: dict[bytes, str] = {}
        self.red_bytes = 0
        self.record_all = record_all

    def _ensure(self) -> None:
        if not self.loaded:
            self.loaded = True
            self.clean, self.redacted = _load(self.p, self.name)

    def _sidecar_bytes(self) -> int:
        """Both sidecar files' size, from one stat each, cached for the render."""
        if self.size is None:
            self.size = 0
            dir_fd = _open_dir(self.p)
            if dir_fd is not None:
                try:
                    for suffix in (_CLEAN_SUFFIX, _REDACTED_SUFFIX):
                        with contextlib.suppress(OSError):
                            st = os.stat(self.name + suffix, dir_fd=dir_fd, follow_symlinks=False)
                            self.size += st.st_size
                finally:
                    os.close(dir_fd)
        return self.size

    def lookup(self, tag: bytes) -> object | None:
        if self.record_all:
            # A write scope reads the sidecar only when it was opened to (the
            # first save of this transcript in this process, see project): later
            # saves offer only new rows, and re-reading a growing file per flush
            # would cost more than the scans it saves.
            if not self.loaded:
                return None
        elif not self.loaded:
            # A render that misses the process cache only a few times (a live
            # session's newest rows, after its older rows hit memory) scans
            # those few: parsing a long session's whole sidecar would hold the
            # GIL longer than the scans it saves. A render that misses more
            # than it hits is a cold open (a restart, an evicted session; the
            # hits are strings other sessions share), and a small sidecar is
            # cheaper to read than the scans already paid: read it early.
            self.misses += 1
            cold = self.misses >= COLD_MISSES and (
                self.misses > self.hits
                or self._sidecar_bytes() <= self.misses * LOAD_BYTES_PER_MISS
            )
            if not cold and self.misses <= LOAD_AFTER_MISSES:
                return None
            self._ensure()
        if tag[_TAG:] in self.clean:
            return CLEAN
        return self.redacted.get(tag[:_TAG])

    def note(self, tag: bytes, verdict: object, *, from_memory: bool) -> None:
        """Record a verdict for this session unless the sidecar already holds it.

        *tag* is the whole 32-byte keyed digest. A clean verdict is stored under
        its second half and a redacted one under its first, so a tag copied out
        of the redacted file never names a clean verdict: the other half cannot
        be derived without the key.

        A verdict served from the process cache is recorded only by a WRITE
        scope: a read scope stays off the disk on a fully warm render. Neither
        scope reads the sidecar per flush: a write scope reads it once, only for
        the first save of a transcript in this process (``preload``), and a read
        scope only once it has loaded it for lookups. A repeated tag costs 16 bytes.
        """
        if from_memory and not self.record_all:
            self.hits += 1
            return
        if verdict is CLEAN:
            key = tag[_TAG:]
            if key not in self.clean and len(self.new_clean) < _NEW_CLEAN_MAX:
                self.new_clean[key] = None
        elif isinstance(verdict, str) and self.redacted.get(tag[:_TAG]) != verdict:
            # Retain no more than one sidecar can hold: flush drops the rest
            # anyway, and keeping them would hold every output of a long
            # render in memory until the scope ends.
            size = len(verdict)
            if size > _REDACTED_MAX_ENTRY or tag[:_TAG] in self.new_red:
                return
            if self.red_bytes + size > MAX_FILE_BYTES - _HEADER:
                return
            self.red_bytes += size
            self.new_red[tag[:_TAG]] = verdict

    def flush(self) -> None:
        p = self.p
        if self.loaded:
            # Strings scanned before the sidecar was read (a cold open's first
            # few misses) may already be stored; appending them again would
            # grow the file on every reopen.
            self.new_clean = {t: None for t in self.new_clean if t not in self.clean}
            self.new_red = {t: o for t, o in self.new_red.items() if self.redacted.get(t) != o}
        if self.new_clean:
            _append(p, self.name, _CLEAN_SUFFIX, list(self.new_clean))
        if self.new_red:
            parts = []
            for tag, out in self.new_red.items():
                ob = out.encode("utf-8", "surrogatepass")
                if len(ob) > _REDACTED_MAX_ENTRY:
                    continue
                parts.append(tag + len(ob).to_bytes(4, "big") + ob + _out_mac(p, tag, ob))
            _append(p, self.name, _REDACTED_SUFFIX, parts)


_scope: contextvars.ContextVar[_Scope | None] = contextvars.ContextVar(
    "display_verdict_scope", default=None
)


@contextlib.contextmanager
def session(
    history_key: str | None, *, record_all: bool = False, preload: bool = False
) -> Iterator[None]:
    """Use and extend *history_key*'s stored verdicts for the redactions inside.

    *preload* reads the stored verdicts up front, so a write scope skips the
    strings already judged and its flush appends none of them again.
    """
    p = _persist
    name = _safe_name(history_key) if (p is not None and history_key) else None
    if p is None or name is None:
        yield
        return
    sc = _Scope(p, name, record_all)
    if preload:
        sc._ensure()
    token = _scope.set(sc)
    try:
        yield
    finally:
        _scope.reset(token)
        try:
            sc.flush()
        except Exception:
            logger.debug("display-verdicts: flush failed", exc_info=True)


def active() -> tuple[_Scope | None, hmac.HMAC | None]:
    """``(scope, mac_base())`` for one display redaction, with one bypass check.

    The display path needs both per string; asking the owner-view switch once
    instead of twice is most of what persistence adds to a first open.
    """
    p = _persist
    if p is None:
        return None, None
    from kiro_crew.security.redaction_switch import credential_pass_bypassed

    if credential_pass_bypassed():
        return None, None
    sc = _scope.get()
    if sc is not None and sc.p is not p:
        sc = None
    return sc, p.mac_base


# ------------------------------------------------------------- write side
_written_guard = threading.Lock()
#: Per transcript, the hashes of the rows already projected at write time, so a
#: save re-serializing an unchanged window does not re-project it. A collision or
#: an eviction only skips or repeats a projection: never a correctness question.
_written: dict[str, set[int]] = {}
_WRITTEN_KEYS_MAX = 256
_written_local = threading.local()


def _line_mark(line: str) -> int:
    """A constant-cost mark for one written row.

    A save re-serializes its whole window, so hashing every line in full would
    add a pass over the window to every flush. Length plus both ends tells a
    new row, a grown streaming row and an edited one apart; a same-length edit
    in the middle reads as "already judged" and is simply scanned at its first
    render, so a collision costs a miss, never a wrong answer.
    """
    return hash((len(line), line[:96], line[-96:]))


def note_written_lines(history_key: str, lines: list[str]) -> None:
    """Called by the save with the window lines it is about to write.

    Stashes the lines this transcript has not had projected yet; the save hands
    them to :func:`take_pending` and :func:`commit_written` once its write
    committed. A save that fails never reaches the commit, so its rows stay
    unprojected and the next save offers them again.
    """
    if _persist is None or not history_key:
        _written_local.pending = None
        return
    with _written_guard:
        seen = _written.get(history_key) or set()
    live = [_line_mark(line) for line in lines]
    pending = [line for line, h in zip(lines, live, strict=True) if h not in seen]
    if sum(len(line) for line in pending) > _QUEUE_MAX_CHARS:
        # The queue would refuse it anyway: those rows are judged on first read.
        pending = []
    _written_local.pending = (history_key, pending, set(live))


def take_pending() -> tuple[str, list[str], set[int]] | None:
    item = getattr(_written_local, "pending", None)
    _written_local.pending = None
    return item


def commit_written(history_key: str, live: set[int]) -> None:
    """Remember the window rows as projected (only the rows still in the window)."""
    with _written_guard:
        if history_key not in _written and len(_written) >= _WRITTEN_KEYS_MAX:
            _written.pop(next(iter(_written)))
        _written[history_key] = live


#: Bounded hand-off to the one projection worker. A full queue, or a batch that
#: would take the queued rows past ``_QUEUE_MAX_CHARS``, is dropped: those rows
#: are simply judged on their first read instead.
_QUEUE_MAX = 256
_QUEUE_MAX_CHARS = 32 * 1024 * 1024
_queue: queue.Queue[tuple[str, str | None, list[str], int, bool]] = queue.Queue(_QUEUE_MAX)
_queued_chars = 0
_queued_guard = threading.Lock()
_worker: threading.Thread | None = None
_worker_guard = threading.Lock()


def submit_committed(workspace: str | None) -> None:
    """After a save's write committed: queue its new rows for projection.

    Runs in the save's thread right after ``atomic_write``. The projection
    itself runs on a single background worker so the save, and the transcript
    lock it holds, are not lengthened by it.
    """
    global _queued_chars
    item = take_pending()
    if item is None or _persist is None:
        return
    history_key, pending, live = item
    with _written_guard:
        # Nothing projected for this transcript in this process (a restart, an
        # eviction): the window offered is the whole transcript, most of it
        # judged before. The projection reads the stored set first, so it
        # neither re-scans those rows nor appends their tags again.
        first = history_key not in _written
    commit_written(history_key, live)
    if not pending:
        return
    size = sum(len(line) for line in pending)
    with _queued_guard:
        if _queued_chars + size > _QUEUE_MAX_CHARS:
            return
        _queued_chars += size
    _ensure_worker()
    try:
        _queue.put_nowait((history_key, workspace, pending, size, first))
    except queue.Full:
        with _queued_guard:
            _queued_chars -= size


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_guard:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run_worker, name="display-verdicts", daemon=True)
        _worker.start()


def project(
    history_key: str, workspace: str | None, lines: list[str], *, preload: bool = False
) -> None:
    """Judge *lines* (transcript rows as written) the way a render would, and store it.

    *preload* (the first save of this transcript in this process) reads the
    stored verdicts first, so rows judged before a restart are neither scanned
    nor appended again.
    """
    import json

    from kiro_crew.dashboard.chat_utils import _prepare_messages

    rows = []
    for line in lines:
        with contextlib.suppress(ValueError):
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    with session(history_key, record_all=True, preload=preload):
        _prepare_messages(rows, False, live_child="", workspace=workspace)


def _run_worker() -> None:
    global _queued_chars
    while True:
        history_key, workspace, lines, size, first = _queue.get()
        try:
            project(history_key, workspace, lines, preload=first)
        except Exception:
            logger.debug("display-verdicts: projection failed", exc_info=True)
        finally:
            with _queued_guard:
                _queued_chars -= size
            _queue.task_done()


def drain_for_tests(timeout: float = 30.0) -> None:
    """Block until every queued projection ran (tests and benchmarks)."""
    import time

    end = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < end:
        time.sleep(0.01)
