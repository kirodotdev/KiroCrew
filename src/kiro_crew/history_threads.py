"""The reply-thread sidecar beside a transcript.

Its location, the bounded and normalized read, and the locked append that refuses a
missing, replaced or unflushed parent.

Each function here whose first parameter is ``self`` is a ``ConversationLog`` method:
the class binds it by name, so it reads and writes the log's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: none.

Instance fields read: ``_dir``.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, atomic_write_at
from kiro_crew.history_files import _safe_key
from kiro_crew.owner_only_files import OWNER_ONLY_FILE_MODE, mkdirs_owner_only

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog


class ThreadStoreUnreadable(ValueError):
    """A reply-thread sidecar holds bytes that are not a thread map.

    Raised by :meth:`ConversationLog.read_threads` instead of reading the file
    as empty, so a writer never replaces damaged data with an empty map.
    """


#: Directory beside the transcripts holding one reply-thread sidecar per
#: session (``dashboard/chat_threads.py``). The ONE spelling of the location:
#: :meth:`ConversationLog.threads_sidecar_path` and Session Storage's
#: "what files is this session made of" both derive it from here, so a
#: reclaim can never leave a session's replies behind in the live store.
THREADS_DIR_NAME = ".threads"
THREADS_SIDECAR_SUFFIX = ".json"


#: The reply-row schema the thread sidecar holds; :meth:`ConversationLog.read_threads`
#: keeps these keys and no other.
THREAD_REPLY_FIELDS: tuple[str, ...] = ("id", "role", "content", "ts")

#: What :meth:`ConversationLog.read_threads` RETAINS of a sidecar, whatever the
#: file holds. The writer (``dashboard/chat_threads.py``) never exceeds these --
#: it clips an answer at 64 000 characters plus a marker and admits 500 replies
#: per thread, 5 000 per sidecar -- so a sidecar it wrote is read back whole;
#: a larger one (another writer under the data home) is cut to the same shape
#: at the point of retention, never carried into memory as written.
THREAD_REPLY_CONTENT_MAX_CHARS = 65_536
#: A thread key is a row id as :func:`mint_row_mid` spells it, and nothing else:
#: the summary route returns the keys as they are, so a key is never allowed a
#: shape that could carry prose.
THREAD_MID_RE = re.compile(r"^m-[0-9a-f]{16}$")
THREADS_MAX_REPLIES_PER_THREAD = 500
THREADS_MAX_REPLIES_PER_SIDECAR = 5_000
#: The sidecar FILE's ceiling, checked on its size before a byte of it is read
#: and on the document before it is written, so the row bounds above cannot be
#: reached through a file that is already too large to parse. The writer
#: answers ``sidecar_full`` at it, exactly as at the row cap; the reader
#: refuses a bigger file (or one that is not a regular file) as unreadable.
THREADS_SIDECAR_MAX_BYTES = 64 * 1024 * 1024
#: The writer's own spellings of a reply's metadata: ``id`` is a uuid4 hex, ``ts``
#: an ISO-8601 instant (or empty), ``role`` one of the two speakers. A row whose
#: metadata is anything else is not a reply the store wrote and is dropped at the
#: read, so those fields reach the dashboard only in shapes that cannot carry
#: prose -- ``content`` is the one free-text field, and it is redacted on the
#: way out.
THREAD_REPLY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_THREAD_REPLY_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$"
)
_THREAD_REPLY_ROLES = frozenset({"user", "assistant"})


def _thread_reply_row(raw: dict) -> dict[str, Any] | None:
    """*raw* reduced to :data:`THREAD_REPLY_FIELDS`, or ``None`` when a field is
    missing, not a string, or -- for ``id``, ``role`` and ``ts`` -- not in the
    shape the writer produces. ``content`` is the only free-text field; it is
    cut at :data:`THREAD_REPLY_CONTENT_MAX_CHARS`."""
    row: dict[str, Any] = {}
    for key in THREAD_REPLY_FIELDS:
        value = raw.get(key, "" if key == "ts" else None)
        if not isinstance(value, str):
            return None
        row[key] = value
    if not THREAD_REPLY_ID_RE.match(row["id"]) or row["role"] not in _THREAD_REPLY_ROLES:
        return None
    if row["ts"] and not _THREAD_REPLY_TS_RE.match(row["ts"]):
        return None
    row["content"] = row["content"][:THREAD_REPLY_CONTENT_MAX_CHARS]
    return row


def _write_thread_sidecar(path: Path, document: str) -> None:
    """Replace the sidecar at *path* without following a link anywhere in it.

    The ``.threads`` directory is code-created beside the transcripts; a LINK at
    that name (planted under the data home) would carry the write outside the
    session store, so the directory is refused unless it is a real directory,
    and on POSIX it is then pinned by descriptor and the leaf replaced relative
    to it (:func:`atomic_write_at`, ``O_NOFOLLOW`` on the temporary), so neither
    a swapped parent nor a link at the leaf can redirect the bytes. Windows has
    no descriptor-relative rename; there the directory check is the guard, as
    Session Storage's opener degrades.
    """
    parent = path.parent
    mkdirs_owner_only(parent)
    if platform_compat.is_link_or_junction(parent) or not stat.S_ISDIR(os.lstat(parent).st_mode):
        raise ThreadStoreUnreadable(f"thread sidecar directory is not a directory: {parent}")
    if not platform_compat.IS_POSIX:
        if os.path.islink(path):
            raise ThreadStoreUnreadable(f"thread sidecar is a link: {path}")
        atomic_write(path, document)
        return
    dir_fd = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        atomic_write_at(dir_fd, path.name, document, mode=OWNER_ONLY_FILE_MODE)
    finally:
        os.close(dir_fd)


def threads_sidecar_for_stem(sessions_dir: Path, stem: str) -> Path:
    """The reply-thread sidecar of the transcript ``<sessions_dir>/<stem>.jsonl``."""
    return sessions_dir / THREADS_DIR_NAME / f"{stem}{THREADS_SIDECAR_SUFFIX}"


def threads_sidecar_path(self: ConversationLog, key: str) -> Path:
    """Sidecar path for a session's reply threads (``dashboard/chat_threads.py``).

    A third sidecar beside the summary caches, for the same reason each of
    those is its own file: the thread store has its own writer (a reply
    landing) and no mtime contract with the transcript -- a reply must
    survive every later append to the main chat, so it is never invalidated
    by the session file's signature. Public because the thread store lives
    outside this module; the transcript delete removes it with the others,
    and Session Storage moves it with the transcript on reclaim.
    """
    return threads_sidecar_for_stem(self._dir, _safe_key(key))


def read_threads(self: ConversationLog, key: str) -> dict[str, list[dict[str, Any]]]:
    """The reply-thread map of *key*'s sidecar (``{mid: [reply, ...]}``).

    A missing sidecar reads as empty. Unreadable bytes -- torn JSON, a wrong
    shape -- raise :class:`ThreadStoreUnreadable` instead of reading as empty,
    because the one caller that writes would otherwise replace the damaged
    file with an empty map and lose every reply it held. Rows and threads of
    the wrong shape are dropped individually; only the document as a whole
    refuses. Each retained row is NORMALIZED to the reply schema -- ``id``,
    ``role``, ``content``, ``ts``, all strings -- and nothing else: the file
    sits beside the transcript under the data home, so a field an agent or
    an older writer put there must never reach the dashboard through the
    detail response's spread. A row missing ``id``, ``role`` or ``content``
    is dropped, as is one whose ``id``, ``role`` or ``ts`` is not in the
    writer's own shape (uuid hex, one of the two speakers, ISO-8601), so
    only ``content`` can carry prose and it is redacted at the boundary.
    The content is cut at :data:`THREAD_REPLY_CONTENT_MAX_CHARS`, a thread
    keeps its NEWEST :data:`THREADS_MAX_REPLIES_PER_THREAD` rows (a key left
    with none is dropped, so keys alone cannot grow the map)
    and the map stops at :data:`THREADS_MAX_REPLIES_PER_SIDECAR` rows in file
    order, so what the file holds never decides what the gateway holds --
    and the file is opened ONCE, without following a link, and sized on
    that descriptor before it is read: over :data:`THREADS_SIDECAR_MAX_BYTES`,
    or not a regular file, is refused, and the read is bounded to the ceiling
    so a file swapped under the open cannot grow past it either.
    """
    path = self.threads_sidecar_path(key)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and os.path.islink(path):
        # Windows has no O_NOFOLLOW: a link at the name is refused by a
        # pre-check instead (degraded, as Session Storage's opener degrades;
        # creating a link there needs a privilege this model does not hand out).
        raise ThreadStoreUnreadable(f"thread sidecar is a link: {path}")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        # ELOOP for a link at the name, EACCES, ENOTDIR: all "not this store".
        raise ThreadStoreUnreadable(f"thread sidecar unreadable: {path}") from exc
    try:
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ThreadStoreUnreadable(f"thread sidecar is not a regular file: {path}")
            if info.st_size > THREADS_SIDECAR_MAX_BYTES:
                raise ThreadStoreUnreadable(
                    f"thread sidecar over {THREADS_SIDECAR_MAX_BYTES} bytes: {path}"
                )
            data = fh.read(THREADS_SIDECAR_MAX_BYTES + 1)
    except OSError as exc:
        raise ThreadStoreUnreadable(f"thread sidecar unreadable: {path}") from exc
    if len(data) > THREADS_SIDECAR_MAX_BYTES:
        raise ThreadStoreUnreadable(
            f"thread sidecar over {THREADS_SIDECAR_MAX_BYTES} bytes: {path}"
        )
    try:
        raw = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        # A document nested past the interpreter's depth is as unreadable as
        # one that does not parse: refused, never a 500.
        raise ThreadStoreUnreadable(f"thread sidecar unreadable: {path}") from exc
    threads = raw.get("threads") if isinstance(raw, dict) else None
    if not isinstance(threads, dict):
        raise ThreadStoreUnreadable(f"thread sidecar has no threads map: {path}")
    out: dict[str, list[dict[str, Any]]] = {}
    budget = THREADS_MAX_REPLIES_PER_SIDECAR
    for mid, replies in threads.items():
        if budget <= 0:
            break
        if not isinstance(mid, str) or not THREAD_MID_RE.match(mid):
            continue
        if not isinstance(replies, list):
            continue
        rows = [_thread_reply_row(r) for r in replies if isinstance(r, dict)]
        kept = [r for r in rows if r is not None][-THREADS_MAX_REPLIES_PER_THREAD:]
        kept = kept[: min(len(kept), budget)]
        if not kept:
            # A key with no rows is not a thread; retaining it would let a
            # map of empty lists grow past the row budget by keys alone.
            continue
        budget -= len(kept)
        out[mid] = kept
    return out


def thread_transcript_identity(self: ConversationLog, key: str) -> str | None:
    """The transcript's ``created_at`` metadata, or ``None`` when absent.

    The identity :meth:`append_thread_reply` checks a reply against; read at
    admission, before any await, and handed back at the write.
    """
    created = self.get_metadata(key).get("created_at")
    return created if isinstance(created, str) and created else None


def append_thread_reply(
    self: ConversationLog,
    key: str,
    mid: str,
    reply: dict[str, Any],
    *,
    max_replies: int,
    max_total: int,
    expected_created_at: str | None = None,
) -> str:
    """Append *reply* to the thread on *mid* in *key*'s sidecar.

    Returns ``"ok"``, ``"duplicate"`` (the thread already holds a reply with
    this ``id`` -- a client re-sending a reply whose acceptance it never saw;
    nothing is written), ``"full"`` (the thread already holds *max_replies*),
    ``"sidecar_full"`` (the sidecar already holds *max_total* replies across
    every thread, or the document with this reply would pass
    :data:`THREADS_SIDECAR_MAX_BYTES` -- the whole-file bounds, since the
    panel reads the file whole and a per-thread cap alone leaves it
    unbounded in the number of threads), ``"missing"`` (no transcript for *key*), or ``"replaced"``
    (the transcript is not the one the reply was admitted against), or
    ``"unflushed"`` (the transcript holds no row with ``meta.mid == mid``).
    Every check runs under the lock so nothing can change between it and
    the write. The identity is the metadata line's ``created_at`` -- minted
    when a transcript is created, carried through verbatim by a rewrite --
    so a member chat deleted and recreated under its deterministic key
    while a turn was in flight is told apart from the chat the reply
    belongs to, exactly as ``chat_persistence`` tells "deleted and
    recreated" apart. Callers capture it with
    :meth:`thread_transcript_identity` at admission and pass it back here.
    The parent's ``meta.mid`` must ALWAYS be on disk: a thread is durable
    only through the row it hangs off, and a parent that exists only in the
    slot's memory window (a reply the slot has not flushed yet) would leave
    the thread unreachable if the process died before the flush -- so such
    a reply is refused as ``"unflushed"`` and the caller says "try again in
    a moment". The same check is what tells a replacement apart for a
    legacy transcript with no ``created_at`` (a replacement never carries
    the old chat's message ids). The read-modify-write runs
    under :meth:`_locked` -- the same lock
    :meth:`delete_session` unlinks the sidecar under -- and refuses when the
    transcript is gone, for the reason :meth:`set_cached_intent_summary`
    gives: a turn holds no lock while its model call is in flight, and an
    unconditional write landing after a delete would recreate the sidecar
    and resurrect a conversation the user was told is gone. Raises
    :class:`ThreadStoreUnreadable` on a damaged sidecar (never overwritten)
    and :class:`HistoryLockTimeout` when the lock cannot be taken. Blocking;
    callers run it off the event loop.
    """
    with self._locked(key):
        if not self._path(key).exists():
            return "missing"
        if expected_created_at is not None:
            current = self.thread_transcript_identity(key)
            if current is not None and current != expected_created_at:
                return "replaced"
        if not self._transcript_holds_mid(key, mid):
            return "replaced" if expected_created_at is None else "unflushed"
        threads = self.read_threads(key)
        if sum(len(r) for r in threads.values()) >= max_total:
            return "sidecar_full"
        replies = threads.setdefault(mid, [])
        if any(r.get("id") == reply.get("id") for r in replies):
            return "duplicate"
        if len(replies) >= max_replies:
            return "full"
        replies.append(reply)
        document = json.dumps({"version": 1, "threads": threads})
        if len(document.encode("utf-8")) > THREADS_SIDECAR_MAX_BYTES:
            return "sidecar_full"
        _write_thread_sidecar(self.threads_sidecar_path(key), document)
        return "ok"


def _transcript_holds_mid(self: ConversationLog, key: str, mid: str) -> bool:
    """Whether *key*'s transcript on disk has a row with ``meta.mid == mid``.

    Read through the chained projection, the same corpus the thread routes
    look a parent up in. Called under :meth:`_locked` by
    :meth:`append_thread_reply`; the projection takes only its own in-process
    index lock, so this is the read-inside-the-lock pattern the metadata
    rewrites already use.
    """
    for row in self.read_messages_chained(key):
        meta = row.get("meta") if isinstance(row, dict) else None
        if isinstance(meta, dict) and meta.get("mid") == mid:
            return True
    return False
