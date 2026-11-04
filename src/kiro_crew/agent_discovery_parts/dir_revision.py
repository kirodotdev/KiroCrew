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
# The switch the revision answers ``None`` under, for every caller at once.
AGENTS_DIR_MEMO_ENABLED = True
# ``st_ctime_ns`` is creation time on Windows, so entry metadata cannot prove
# that an in-place rewrite did not happen: there every entry is pinned on a
# digest of its bytes, the rule a young entry follows elsewhere.
_PIN_EVERY_ENTRY_ON_BYTES = _WINDOWS
# How many bytes one revision may digest when every entry is pinned on them.
# A directory past it reads uncached.
_AGENTS_DIR_ALL_ENTRIES_MAX_DIGEST_BYTES = 256 * 1024 * 1024
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
    target. On Windows (:data:`_PIN_EVERY_ENTRY_ON_BYTES`) ``st_ctime_ns`` is
    creation time, so entry metadata cannot prove an in-place rewrite did not
    happen; there EVERY entry is pinned on a digest of its bytes, whatever its
    age, with no cap on how many and a total of
    :data:`_AGENTS_DIR_ALL_ENTRIES_MAX_DIGEST_BYTES`, past which the answer is
    ``None``. Its metadata comes from ``os.stat(path, follow_symlinks=False)``,
    because ``DirEntry.stat`` leaves the inode, device and link count at zero
    there. One residual is accepted: a rewrite DURING one read that is undone
    before the read ends, with its mtime set back by hand, leaves the bytes and
    the metadata as they were, so the answer read across it can be stored; it
    takes a deliberate ``utime`` inside the read, which an ordinary edit,
    restore or sync never does. There is no cap on the number of entries: the
    walk is one ``stat`` per spec where the reads it saves are one parse per
    spec, and
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
    pin_all = _PIN_EVERY_ENTRY_ON_BYTES
    max_digests = None if pin_all else _AGENTS_DIR_RACY_MAX_DIGESTS
    max_digest_bytes = (
        _AGENTS_DIR_ALL_ENTRIES_MAX_DIGEST_BYTES if pin_all else _AGENTS_DIR_RACY_MAX_DIGEST_BYTES
    )
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
            path = os.path.join(agents_dir, entry.name)
            try:
                # ``DirEntry.stat`` carries no inode, device or link count on
                # Windows; the digest's identity check needs them.
                st = (
                    os.stat(path, follow_symlinks=False)
                    if pin_all
                    else entry.stat(follow_symlinks=False)
                )
            except OSError:
                entries.append((entry.name, 0, 0, 0, 0, 0, 0, 0, _NO_DIGEST))
                continue
            digest = _NO_DIGEST
            if pin_all or st.st_mtime_ns > cutoff or st.st_ctime_ns > cutoff:
                digests += 1
                if max_digests is not None and digests > max_digests:
                    return None
                hashed = _young_entry_digest(path, st, max_digest_bytes - digest_bytes)
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
      direct-filename fallback would read ``<alias>.json``;
    - a caller that can answer every name from one walk passes ``prefill``, so
      the first read of each other name under a revision is a hit (see
      :meth:`get`).

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
        # dir -> (revision pin, answers, whether a prefill already ran under the pin).
        self._answers: dict[str, tuple[bytes, dict[str, T], bool]] = {}

    def get(
        self,
        agents_dir: Path,
        key: str,
        compute: Callable[[], T],
        prefill: Callable[[], dict[str, T]] | None = None,
    ) -> T:
        """Return the memoized answer for *key* under *agents_dir*, or ``compute()``.

        A *key* that is a skill-view alias name is always computed: every reader
        keyed here is keyed by agent name and may read ``<key>.json`` by
        filename, and the revision does not fingerprint aliases, so an answer
        read from one could outlive the alias it was read from.

        *prefill*, when given, answers many keys from ONE walk of the directory:
        it runs on the first miss under a revision, and the answers it returns
        are stored under the same rules as ``compute``'s (revision equal before
        and after, alias names left out), so each other key's first read is a
        hit instead of another walk. A key it does not answer is computed as
        before. It runs once per revision (two threads missing at once may
        both run it, as they may both compute); an ``OSError`` from it stores
        nothing, and past :data:`_AGENTS_DIR_MEMO_MAX_KEYS` only part of its
        answers is kept (see :meth:`_prefill`). A prefill must answer a key
        exactly as ``compute`` would, and leave out every key it cannot answer
        that way.
        """
        if is_native_skill_alias_name(key):
            return compute()
        dir_key = str(agents_dir)
        revision = agents_dir_revision(agents_dir)
        if revision is None:
            return compute()
        pin = _revision_pin(revision)
        prefilled = False
        with self._lock:
            cached = self._answers.get(dir_key)
            if cached is not None and cached[0] == pin:
                if key in cached[1]:
                    return cached[1][key]
                prefilled = cached[2]
        if prefill is not None and not prefilled:
            found = self._prefill(agents_dir, dir_key, key, revision, pin, prefill)
            if found is not None and key in found:
                return found[key]
        answer = compute()
        if agents_dir_revision(agents_dir) != revision:
            return answer
        with self._lock:
            cached = self._answers.get(dir_key)
            if cached is None or cached[0] != pin:
                cached = (pin, {}, False)
                self._answers[dir_key] = cached
            answers = cached[1]
            if len(answers) >= _AGENTS_DIR_MEMO_MAX_KEYS:
                answers.clear()
            answers[key] = answer
        return answer

    def _prefill(
        self,
        agents_dir: Path,
        dir_key: str,
        key: str,
        revision: AgentsDirRevision,
        pin: bytes,
        prefill: Callable[[], dict[str, T]],
    ) -> dict[str, T] | None:
        """Run *prefill* once under *pin*; its answers, or ``None`` when they are unusable.

        The answers are usable only when the revision is the same after the walk
        as before it, the rule every stored answer follows. They are stored up
        to one below :data:`_AGENTS_DIR_MEMO_MAX_KEYS`: the asked-for *key*
        first, then answers already stored under *pin*, then the rest in walk
        order. On a directory declaring more names than that, the caller's own
        answer is still kept and the names past the cap are read one by one as
        before.
        """
        try:
            answers = prefill()
        except OSError:
            return None
        if agents_dir_revision(agents_dir) != revision:
            return None
        found = {name: a for name, a in answers.items() if not is_native_skill_alias_name(name)}
        with self._lock:
            cached = self._answers.get(dir_key)
            prior = cached[1] if cached is not None and cached[0] == pin else {}
            # One below the cap, so the next one-name store does not clear it.
            room = _AGENTS_DIR_MEMO_MAX_KEYS - 1
            kept: dict[str, T] = {}
            for name in [*([key] if key in found else []), *prior, *found]:
                if len(kept) >= room:
                    break
                if name not in kept:
                    kept[name] = prior[name] if name in prior else found[name]
            self._answers[dir_key] = (pin, kept, True)
        return found
