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

    ``None`` means "cannot prove freshness; do not memoize". Unlike
    :func:`_dir_signature`, whose caches tolerate a same-tick edit, this never
    lets entry metadata miss a rewrite. It parses no spec.

    Each spec entry contributes its name, timestamps, size, identity, mode and
    link count (mode and link count are what the hardened reader judges besides
    the bytes). The tuple includes :func:`spec_cache_generation`, so
    :func:`clear_list_agents_cache`, called by in-process spec writers, moves
    every revision at once.

    An entry whose mtime or ctime is within :data:`_AGENTS_DIR_RACY_WINDOW_NS`
    of the walk is also pinned on a SHA-256 of its bytes (git's racy-stat rule),
    so a same-size rewrite in the same timestamp tick is never served stale.
    Older entries need only metadata: a later write stamps a ctime no POSIX call
    can set back. More than :data:`_AGENTS_DIR_RACY_MAX_DIGESTS` young entries,
    more than :data:`_AGENTS_DIR_RACY_MAX_DIGEST_BYTES` of them, or a young entry
    that cannot be hashed gives ``None``.

    A symlinked spec gives ``None``: its metadata cannot see edits to its target.
    On Windows (:data:`_PIN_EVERY_ENTRY_ON_BYTES`) ``st_ctime_ns`` is creation
    time, so EVERY entry is pinned on its bytes, up to
    :data:`_AGENTS_DIR_ALL_ENTRIES_MAX_DIGEST_BYTES` in total (past that,
    ``None``), and metadata comes from ``os.stat(path, follow_symlinks=False)``
    because ``DirEntry.stat`` leaves inode, device and link count at zero there.
    Accepted residual: a rewrite during one read, undone before it ends with the
    mtime set back by a deliberate ``utime``, can be stored. The entry count is
    uncapped: one ``stat`` per spec saves one parse per spec, and
    :class:`AgentsDirMemo` keeps only a fixed-size digest of the revision.

    Only entries :func:`_iter_spec_entries` yields are fingerprinted, a superset
    of what the scans read, so the revision is never less sensitive than the
    scan. The directory mtime is left out, so a stray file or skill-view alias
    beside the specs does not move the revision; :class:`AgentsDirMemo` never
    stores an answer under an alias name. A failed ``stat`` records zeros (the
    name still counts). An entry of unknown kind, or an unlistable directory,
    gives ``None``: an unlistable directory is not an empty one. A directory that
    does not exist gives the revision of an empty one.
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
    except FileNotFoundError:
        # A directory that does not exist holds no specs, which every reader
        # answers as it does an empty one (the crewmate folder before its first
        # file). Creating it with a spec in it adds an entry, so the revision moves.
        return (), spec_cache_generation()
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
    tool-policy read) each parse every spec to resolve one name, and each keeps
    its own instance so their SEL ``operation`` labels never share an answer.

    - An answer is stored only when the revision before and after ``compute`` is
      equal and not ``None``, so a write during the read is never memoized.
    - An exception from ``compute`` propagates and stores nothing.
    - One answer set per directory, replaced on a new revision and cleared at
      :data:`_AGENTS_DIR_MEMO_MAX_KEYS`; the revision is kept as a fixed-size
      digest (:func:`_revision_pin`), so memory does not grow with spec count.
    - A skill-view alias key is never stored: the revision leaves aliases out,
      and a caller's filename fallback would read ``<alias>.json``.
    - ``prefill`` lets a caller answer every name from one walk (see :meth:`get`).

    :func:`clear_list_agents_cache` moves the spec generation inside every
    revision, so stored answers stop matching without the memo being told.

    The stored object itself is returned, so a caller that may mutate it must
    copy (the KAS projection deep-copies). Two threads missing at once compute
    the same answer redundantly. The lock guards the dict only.
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

        A skill-view alias *key* is always computed: readers may open
        ``<key>.json`` by filename, and the revision does not fingerprint
        aliases, so a stored answer could outlive its alias.

        *prefill* answers many keys from ONE walk. It runs once per revision on
        the first miss, its answers are stored under ``compute``'s rules, and a
        key it omits is computed. An ``OSError`` from it stores nothing; past
        :data:`_AGENTS_DIR_MEMO_MAX_KEYS` only part is kept (see :meth:`_prefill`).
        It must answer each key exactly as ``compute`` would, or leave it out.
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
