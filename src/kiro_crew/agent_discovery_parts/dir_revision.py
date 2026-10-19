"""The content revision of an agents directory, and the memo keyed on it.

``agents_dir_revision`` pins every spec entry by its metadata, and an entry too young for
its metadata to be trusted by a digest of its bytes as well; ``AgentsDirMemo`` keeps an
answer derived from the directory for as long as that revision holds.

:mod:`kiro_crew.agent_discovery` stays the import path and the patch surface; see
:mod:`kiro_crew.agent_discovery_parts`.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from pathlib import Path
from typing import Callable, Generic, TypeVar

from kiro_crew.agent_discovery_parts.spec_reads import _WINDOWS, _fence_refuses, _SpecReadRefused
from kiro_crew.agent_discovery_parts.spec_snapshot import _iter_spec_entries, spec_cache_generation
from kiro_crew.agent_spec_format import is_native_skill_alias_name
from kiro_crew.pinned_fs import open_fenced_for_read

# name, mtime, ctime, size, inode, device, mode, link count, content digest.
_SpecStatRevision = tuple[str, int, int, int, int, int, int, int, bytes]
AgentsDirRevision = tuple[tuple[_SpecStatRevision, ...], int]
# ``st_ctime_ns`` is creation time on Windows, so entry metadata cannot prove
# that an in-place rewrite did not happen; the revision is unavailable there.
AGENTS_DIR_MEMO_ENABLED = not _WINDOWS
# Follow the racy-git precedent: metadata younger than this window is untrusted,
# so an entry that young is pinned on a digest of its bytes as well.
_AGENTS_DIR_RACY_WINDOW_NS = 2_000_000_000
# How many young entries, and how many of their bytes, one revision may digest.
# A bulk write past either reads uncached until it is a window old.
_AGENTS_DIR_RACY_MAX_DIGESTS = 64
_AGENTS_DIR_RACY_MAX_DIGEST_BYTES = 8 * 1024 * 1024
# An answer set that reaches this many keys is cleared whole, so a churn of names cannot grow it.
_AGENTS_DIR_MEMO_MAX_KEYS = 256
# The digest slot of an entry old enough for its metadata alone to pin it.
_NO_DIGEST = b""
_DIGEST_CHUNK_BYTES = 64 * 1024


def _young_entry_digest(path: str, st: os.stat_result, limit: int) -> tuple[bytes, int] | None:
    """SHA-256 of the regular file at *path* and the bytes read, or ``None``.

    ``None`` when the entry is not a regular file, cannot be opened or read, is
    not the inode *st* describes, or holds more than *limit* bytes: bytes
    the revision cannot hash are bytes it cannot vouch for. The open is the
    hardened reader's own (:func:`pinned_fs.open_fenced_for_read`), so a link
    at the final component, a hardlinked or non-regular inode and a sensitive
    kernel path are refused here exactly as the spec read refuses them.
    """
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        fd = open_fenced_for_read(
            path,
            fence=lambda fd_real: _fence_refuses(Path(fd_real)),
            refusal=_SpecReadRefused,
        )
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        if (opened.st_ino, opened.st_dev) != (st.st_ino, st.st_dev):
            return None
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, _DIGEST_CHUNK_BYTES)
            if not chunk:
                return digest.digest(), total
            total += len(chunk)
            if total > limit:
                return None
            digest.update(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)


def agents_dir_revision(agents_dir: Path) -> AgentsDirRevision | None:
    """Fingerprint of *agents_dir* strong enough to pin a read answer to.

    ``None`` means "cannot prove freshness; do not memoize". Where
    :func:`_dir_signature` answers every call with a tuple because the catalog
    caches it serves tolerate a same-tick edit, this never lets entry metadata
    miss a rewrite, and it parses no spec either way.

    Each spec entry contributes its name, timestamps, size, identity, mode and
    link count, so an entry added, removed, renamed or re-linked and every
    ordinary in-place edit or metadata change moves the revision; the link
    count and the mode are what the hardened reader judges a file by besides
    its bytes. The in-process spec generation (:func:`spec_cache_generation`)
    is part of the tuple, so :func:`clear_list_agents_cache` -- which the
    in-process spec writers call -- moves every revision at once.

    An entry whose mtime or ctime is within :data:`_AGENTS_DIR_RACY_WINDOW_NS`
    of the walk is pinned on a SHA-256 of its bytes as well (the racy-git rule:
    git re-compares the content of an entry whose stat is too young to trust).
    A same-size rewrite that lands in the same filesystem timestamp tick as the
    previous write leaves the metadata unchanged but not the bytes, so it is
    never served stale, and one fresh write does not withdraw the pin from
    the whole directory: a directory written continuously is pinned between
    writes. An older entry's metadata is enough on its own: any later write
    stamps a ctime no POSIX call can set back, which lands past the window the
    entry was outside of. A walk with more than
    :data:`_AGENTS_DIR_RACY_MAX_DIGESTS` young entries, or more than
    :data:`_AGENTS_DIR_RACY_MAX_DIGEST_BYTES` of them, gives ``None``, and so
    does a young entry whose bytes cannot be hashed (not a regular file,
    unreadable, or replaced while it was read).

    A symlinked spec gives ``None``: its entry metadata cannot see edits to its
    target. On Windows the answer is always ``None``: ``st_ctime_ns`` is
    creation time there, so entry metadata cannot prove an in-place rewrite did
    not happen. There is no cap on the number of entries: the walk is one
    ``stat`` per spec where the reads it saves are one parse per spec, and
    :class:`AgentsDirMemo` retains a fixed-size digest of the revision, never
    the tuple.

    Only the entries :func:`_iter_spec_entries` yields -- a recognised spec
    suffix, not a skill-view alias -- are fingerprinted; that is a superset of
    what the spec scans read (a Markdown spec shadowed by its JSON twin is still
    fingerprinted; an alias is left out by scan and fingerprint alike), so the
    revision can only be more sensitive than the scan, never less. The
    directory's own mtime is not part of it: every file a scan reads is named
    in the tuple, so a stray file or an alias written beside the specs neither
    moves the revision nor withdraws the pin. Nothing read from an alias is
    pinned to it either: :class:`AgentsDirMemo` never stores an answer under
    an alias name. A ``stat`` that fails records
    zeros: the entry is still named, so its appearance and disappearance are
    revisions. An entry whose kind cannot be determined gives ``None``, and so
    does a directory that cannot be listed: an unlistable directory is not an
    empty one.
    """
    if not AGENTS_DIR_MEMO_ENABLED:
        return None
    # Taken before the walk, so an entry written while it runs is young.
    cutoff = time.time_ns() - _AGENTS_DIR_RACY_WINDOW_NS
    digests = 0
    digest_bytes = 0
    entries: list[_SpecStatRevision] = []
    try:
        for entry in _iter_spec_entries(agents_dir):
            try:
                if entry.is_symlink():
                    return None
            except OSError:
                return None
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                entries.append((entry.name, 0, 0, 0, 0, 0, 0, 0, _NO_DIGEST))
                continue
            digest = _NO_DIGEST
            if st.st_mtime_ns > cutoff or st.st_ctime_ns > cutoff:
                digests += 1
                if digests > _AGENTS_DIR_RACY_MAX_DIGESTS:
                    return None
                hashed = _young_entry_digest(
                    os.path.join(agents_dir, entry.name),
                    st,
                    _AGENTS_DIR_RACY_MAX_DIGEST_BYTES - digest_bytes,
                )
                if hashed is None:
                    return None
                digest, read = hashed
                digest_bytes += read
            entries.append(
                (
                    entry.name,
                    st.st_mtime_ns,
                    st.st_ctime_ns,
                    st.st_size,
                    st.st_ino,
                    st.st_dev,
                    st.st_mode,
                    st.st_nlink,
                    digest,
                )
            )
    except OSError:
        return None
    return tuple(sorted(entries)), spec_cache_generation()


def _revision_pin(revision: AgentsDirRevision) -> bytes:
    """A fixed-size digest of *revision*, the form :class:`AgentsDirMemo` retains.

    ``repr`` of a tuple of strings, integers and bytes is deterministic, so two
    equal revisions pin equal; SHA-256 makes two different ones collide only by
    a collision of the hash.
    """
    return hashlib.sha256(repr(revision).encode("utf-8", "backslashreplace")).digest()


T = TypeVar("T")


class AgentsDirMemo(Generic[T]):
    """Answers computed from one walk of an agents directory, pinned to its revision.

    The two ``spec_by_declared_name`` callers (the KAS projection and the
    tool-policy read) each parse every spec in the directory to resolve one
    name, and each keeps its own instance here so their SEL ``operation``
    labels never share an answer. The store and hit rules live once:

    - a revision is taken before ``compute`` and again after it, and the answer
      is stored only when both are equal and not ``None``, so a write landing
      during the read is never memoized under the revision that preceded it;
    - an exception from ``compute`` propagates and nothing is stored;
    - one answer set per directory, replaced whole on a new revision and
      cleared when it reaches :data:`_AGENTS_DIR_MEMO_MAX_KEYS`, so a churn of
      keys cannot grow it;
    - the revision an answer set is pinned to is retained as its fixed-size
      digest (:func:`_revision_pin`), so what the memo holds per directory does
      not grow with the number of specs in it;
    - a key that is a skill-view alias name is computed on every call and never
      stored, because the revision leaves aliases out and a caller's
      direct-filename fallback would read ``<alias>.json``.

    An in-process spec write that calls :func:`clear_list_agents_cache` moves
    the spec generation, which is part of every revision, so the stored answers
    stop matching without the memo being told.

    The answer returned is the stored object itself, so a caller that hands it
    to code which may mutate it must copy (the KAS projection deep-copies; the
    tool-policy read's answer is serialized and never mutated). Two threads
    missing at once compute redundantly and last-write-wins, the same answer
    from the same revision. The lock guards the dict only; callers run on
    worker threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._answers: dict[str, tuple[bytes, dict[str, T]]] = {}

    def get(self, agents_dir: Path, key: str, compute: Callable[[], T]) -> T:
        """Return the memoized answer for *key* under *agents_dir*, or ``compute()``.

        A *key* that is a skill-view alias name is always computed: every reader
        keyed here is keyed by agent name and may read ``<key>.json`` by
        filename, and the revision does not fingerprint aliases, so an answer
        read from one could outlive the alias it was read from.
        """
        if is_native_skill_alias_name(key):
            return compute()
        dir_key = str(agents_dir)
        revision = agents_dir_revision(agents_dir)
        if revision is None:
            return compute()
        pin = _revision_pin(revision)
        with self._lock:
            cached = self._answers.get(dir_key)
            if cached is not None and cached[0] == pin and key in cached[1]:
                return cached[1][key]
        answer = compute()
        if agents_dir_revision(agents_dir) != revision:
            return answer
        with self._lock:
            cached = self._answers.get(dir_key)
            if cached is None or cached[0] != pin:
                cached = (pin, {})
                self._answers[dir_key] = cached
            answers = cached[1]
            if len(answers) >= _AGENTS_DIR_MEMO_MAX_KEYS:
                answers.clear()
            answers[key] = answer
        return answer
