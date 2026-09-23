"""Durable provenance for the ``settings.local.json`` seed Crew writes.

Crew seeds ``<work_dir>/.claude/settings.local.json`` for a claude-agent-acp
session and overwrites or removes ONLY the file it owns. Proving that ownership
from per-instance memory alone ("this client created it" plus "the bytes it
wrote") answers the question correctly inside one session and wrongly outside
one: a seed left behind by a session that was killed, or by an earlier app
version, reads as a stranger's file forever after. The writer then
takes its leave-it-alone branch on every subsequent session, so a stale
``availableModels`` allowlist (and a stale ``permissions.defaultMode``, up to an
inherited ``bypassPermissions``) becomes permanent project state that Crew can
neither refresh nor clean up.

This module is the missing half: a small record, under Crew's OWN data home, of
the bytes Crew last wrote to a given settings path. It is a **provenance
credential, not a permission grant** — a record alone proves nothing, because
adoption additionally requires the file on disk to still hash to the recorded
digest. A user-authored file (or a Crew file the user has since edited) never
matches, so it is still left untouched; only Crew's own orphan is recognized and
re-seeded.

Four properties the callers depend on:

* **Nothing is added to the user's project.** The record lives beside Crew's
  other sidecars in ``config_dir()``, keyed by the settings path, so a checked-out
  repository gains no extra file to notice, ignore or commit.
* **Lookups never touch the disk, and no mutation runs on the event loop.**
  Ownership is consulted synchronously from teardown, so the sidecar is read once
  at import and served from memory afterwards. Durable mutations write and are
  therefore blocking; each runs off-loop. :func:`release_local` and
  :func:`unshare_local` are the memory-only counterparts teardown may call
  directly after the off-loop discard handles durable holder withdrawal.
* **Every failure answers "not ours".** An unreadable or corrupt sidecar, a
  missing entry, a malformed entry — all degrade to the pre-existing behaviour of
  leaving the file alone, which is the safe direction.
* **A record is adoptable only once nobody here still holds it.** Ownership is
  scoped to an owner token, so an orphan is adopted by the next session while a
  path a LIVE client in this process is seeding stays that client's own.

The sharer registry (:func:`share` / :func:`unshare` / :func:`has_sharers`)
interleaves with the owner lifecycle in ``acp/client.py``; these are the
invariants both modules hold together, each backed by a review ruling on the
PR that introduced it:

* **Byte-equality is the share boundary.** A session becomes a sharer only
  when the payload it would write is byte-identical to the file on disk.
* **Only the owner writes.** A sharer never writes, claims, or deletes the
  settings file; authorship under a live sharer additionally requires the new
  payload to be byte-identical to the durable digest (:func:`recorded_durable`).
* **A lease is withdrawn only by its own sharer's reset** — or by promotion
  to ownership, the single authorship point.
* **A record a live sharer validated survives every prune** until that
  sharer's reset (:func:`_persist` exempts keys with live sharers).
* **Records are invisible until durable.** :func:`record` publishes in memory
  only after its persist lands, so a rider never trusts bytes whose grant
  could vanish in a crash.
* **Owner teardown keeps the file under live sharers** and hands back only
  its own slot; the next session adopts and repairs the recorded orphan.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir, peek_data_home

logger = logging.getLogger(__name__)

# One entry per settings path Crew currently has a seed at. The sidecar carries
# no format marker, and that is deliberate rather than an omission: adoption is
# decided by the digest alone, so a version number would have no reader, and the
# day a second format exists an ABSENT marker already means "the first one".
#
# Process-wide runtime state, like model_registry._ADVERTISED_MODELS: loaded once
# at import, mutated in memory, persisted on write. Tests isolate it by
# monkeypatching this dict and :func:`_sidecar_path`.
_RECORDS: dict[str, dict[str, Any]] = {}

# Which owner token, if any, is CURRENTLY seeding each path in this process.
# Adoption is for orphans, and only a record with no live holder is one: two
# keyless sessions share the default work_dir (``config_dir() / "workspace"``),
# so without this a second session would recognize the FIRST session's live seed
# as an orphan, re-seed it with its own ``permissions.defaultMode``, and unlink it
# on its own reset -- out from under a session still running against it. This dict
# is the in-process cache; the cross-process authority is the ``holders`` entry
# each registration persists into the sidecar record (see ``_local_holders``),
# whose PID-reuse-safe identities let any process tell a live holder from a
# stale one left by a dead process.
_LIVE: dict[str, str] = {}

# Which owner tokens are CURRENTLY running as SHARED READERS of each path in
# this process. A sharer validated that the file on disk is byte-identical to
# both the durable record and its own rendered payload, delivered its MCP array
# on that basis, and holds no other stake: it may never rewrite or remove the
# file. This registry is what keeps the file's future honest for them -- while
# it is non-empty, the owner's teardown leaves the file in place (see the
# client's settle transaction) and :func:`claim` refuses a new adoption, so no
# Crew session can put DIFFERENT permission bytes at a path a sharer already
# delivered tools against. Like ``_LIVE`` it is the in-process cache: each
# lease is also persisted as a ``holders`` entry on the sidecar record, so a
# DIFFERENT process's :func:`claim` or re-seed sees the live stake and refuses
# too, and a holder whose process is gone reads as stale and reclaimable.
_SHARERS: dict[str, set[str]] = {}

# Serializes the record transaction: mutate ``_RECORDS``, prune, snapshot, publish.
# Without it two seeds running concurrently under ``asyncio.to_thread`` can each
# build a snapshot and publish in the opposite order, so an OLDER snapshot lands
# last and the newer seed's provenance is lost -- the surviving file then reads as
# a stranger's on the next run, which is the whole failure this module removes.
#
# Held across the ``atomic_write``, because the snapshot and its publish are one
# step: releasing between them is exactly the reordering above. Every entry point
# that can reach :func:`_persist` runs OFF the event loop, so no wait on this lock
# is ever a wait the loop takes. Read-only entry points and the explicit local
# teardown halves deliberately do NOT take it -- see :func:`recorded`,
# :func:`release_local`, and :func:`unshare_local`.
#
# This serializes THREADS in one process only. Two Crew PROCESSES (the gateway and
# a concurrent CLI chat) each hold their own ``_LOCK`` and their own process-local
# ``_RECORDS``, so this lock cannot stop them clobbering each other's sidecar. The
# cross-process serialization is :func:`_cross_process_lock`, held by :func:`_persist`.
_LOCK = threading.Lock()

# Serializes a sharer's validate-then-take-lease sequence against an owner
# teardown's move/restore transaction on the SAME path. Sibling sessions share
# one process (the premise of the shared-reader state), so a process-local lock
# is the whole arbitration: with it, either the sharer validates before the
# move (its registration then pins the teardown's post-move barrier) or after
# the transaction settles (its disk check then sees the transaction's outcome,
# never the vacancy in the middle). Reentrant is unnecessary -- neither side
# nests. Keyed process-wide rather than per-path: settle transactions are rare
# (session teardown) and short, and one lock cannot deadlock.
SETTLE_LOCK = threading.Lock()

# Cross-process lock filename, BESIDE the sidecar rather than on it: ``atomic_write``
# publishes by renaming a fresh inode over the sidecar, so a lock held on the
# sidecar's own inode would guard nothing across that rename. Same placement and
# reasoning as ``aws_consent._ConsentLock`` and the ops-mission-control policy store.
_LOCK_FILENAME = ".settings_seeds.lock"
_HOLDER_OWNERS = "owners"
_HOLDER_SHARERS = "sharers"
_HOLDER_KINDS = (_HOLDER_OWNERS, _HOLDER_SHARERS)


def _sidecar_path() -> Path:
    """Path to the seed-provenance sidecar under Crew's data home.

    Resolved per call rather than folded into a module constant, so
    ``KIROCREW_HOME`` and test overrides are honoured on every access instead of
    being frozen at first resolution. Resolved through ``peek_data_home()``, not
    ``config_dir()``: the import-time ``_load()`` only needs to know whether the
    file exists, and importing this module must not create ``~/.kiro/crew`` on
    the host. The one writer, :func:`_persist`, creates the directory itself
    before taking its lock beside the sidecar.
    """
    return peek_data_home() / "settings_seeds.json"


def _key(path: Path | str) -> str:
    """Sidecar key for a settings path.

    Deliberately the path as GIVEN, not ``resolve()``d: resolution follows
    symlinks, and every caller derives this from the same
    ``work_dir / ".claude" / "settings.local.json"`` expression, so the
    unresolved string is both stable across sessions and free of a filesystem
    round-trip on the lookup path.
    """
    return os.fspath(path)


def digest(payload: str) -> str:
    """The digest recorded for *payload* — sha256 of its UTF-8 bytes."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _empty_holders() -> dict[str, dict[str, dict[str, Any]]]:
    return {_HOLDER_OWNERS: {}, _HOLDER_SHARERS: {}}


def _holder_identity() -> dict[str, Any] | None:
    """This process's PID-reuse-safe identity, or ``None`` when unprovable."""
    pid = os.getpid()
    start_id = platform_compat.get_process_start_id(pid)
    if not isinstance(start_id, str) or not start_id:
        return None
    return {"pid": pid, "start_id": start_id}


def _validated_holders(value: object) -> dict[str, dict[str, dict[str, Any]]] | None:
    """Validate persisted holder groups; digest-only legacy records get empty groups."""
    if value is None:
        return _empty_holders()
    if not isinstance(value, dict):
        return None
    out = _empty_holders()
    for kind in _HOLDER_KINDS:
        group = value.get(kind, {})
        if not isinstance(group, dict):
            return None
        for owner, identity in group.items():
            if not isinstance(owner, str) or not owner or not isinstance(identity, dict):
                return None
            pid, start_id = identity.get("pid"), identity.get("start_id")
            if (
                isinstance(pid, bool)
                or not isinstance(pid, int)
                or pid <= 0
                or not isinstance(start_id, str)
                or not start_id
            ):
                return None
            out[kind][owner] = {"pid": pid, "start_id": start_id}
    return out


def _copy_entry(entry: dict[str, Any]) -> dict[str, Any]:
    holders = _validated_holders(entry.get("holders")) or _empty_holders()
    return {
        "size": entry["size"],
        "sha256": entry["sha256"],
        "holders": {kind: dict(holders[kind]) for kind in _HOLDER_KINDS},
    }


def _holder_is_live(identity: dict[str, Any]) -> bool:
    """Whether *identity* still names the same process; unknown stays live."""
    pid = identity["pid"]
    if not platform_compat.pid_exists(pid):
        return False
    current = platform_compat.get_process_start_id(pid)
    return current is None or current == identity["start_id"]


def _live_holders(
    entry: dict[str, Any], kind: str | None = None
) -> list[tuple[str, str, dict[str, Any]]]:
    holders = _validated_holders(entry.get("holders")) or _empty_holders()
    kinds = _HOLDER_KINDS if kind is None else (kind,)
    return [
        (holder_kind, owner, identity)
        for holder_kind in kinds
        for owner, identity in holders[holder_kind].items()
        if _holder_is_live(identity)
    ]


def _local_holders(key: str) -> dict[str, dict[str, dict[str, Any]]]:
    """Render local holder caches, degrading to digest-only when identity is unprovable.

    The in-process ``_LIVE`` / ``_SHARERS`` registries still arbitrate sibling
    sessions. Only cross-process holder distinction is unavailable, matching the
    digest-only records that predate persisted holder identities.
    """
    identity = _holder_identity()
    if identity is None:
        return _empty_holders()
    owners = {_LIVE[key]: dict(identity)} if key in _LIVE else {}
    sharers = {owner: dict(identity) for owner in _SHARERS.get(key, set())}
    return {_HOLDER_OWNERS: owners, _HOLDER_SHARERS: sharers}


def _read_disk_seeds() -> dict[str, dict[str, Any]]:
    """The seeds currently ON DISK, validated. ``{}`` when absent or unreadable.

    Old digest-only records remain readable. New records also carry owner and
    shared-reader identities as PID + process start ID, so a second Crew process
    can distinguish a live lease from a crashed process or a recycled PID.
    """
    out: dict[str, dict[str, Any]] = {}
    try:
        path = _sidecar_path()
        if not path.is_file():
            return out
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):  # pragma: no cover - corrupt/absent sidecar
        logger.debug(
            "seed-provenance sidecar unreadable; seeds will read as unowned", exc_info=True
        )
        return out
    seeds = data.get("seeds") if isinstance(data, dict) else None
    if not isinstance(seeds, dict):
        return out
    for key, entry in seeds.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        size, sha = entry.get("size"), entry.get("sha256")
        holders = _validated_holders(entry.get("holders"))
        if (
            isinstance(size, int)
            and not isinstance(size, bool)
            and size >= 0
            and isinstance(sha, str)
            and sha
            and holders is not None
        ):
            out[key] = {"size": size, "sha256": sha, "holders": holders}
    return out


def _load() -> None:
    """Load the persisted sidecar into ``_RECORDS`` (best-effort).

    Called once at import. A missing file is normal (nothing seeded yet); a
    corrupt one leaves every path looking unowned — the same answer Crew gave
    before this record existed.
    """
    _RECORDS.update(_read_disk_seeds())


_load()


@contextlib.contextmanager
def _cross_process_lock() -> Iterator[None]:
    """Exclusive lock around the reload-merge-publish in :func:`_persist`.

    Without it the gateway and a concurrent CLI chat each publish a process-local
    snapshot and the later writer drops the other's record, leaving that seed
    permanently unadoptable — the stale-state failure this module exists to remove,
    reintroduced for the losing process's work dir. ``atomic_write`` renaming a new
    inode over the sidecar is what makes the LAST writer win, so the fix is to make
    every writer reload-merge-publish while holding this lock.

    FAILS CLOSED via :func:`platform_compat.acquire_lock`: a stuck holder raises
    rather than letting a writer proceed unserialized, and :func:`_persist` reports
    that as a failed persist (``False``) — the same honest "grant not durable"
    outcome a failed write already gives, never a silent lost record.
    """
    lock_file = config_dir() / _LOCK_FILENAME
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        platform_compat.acquire_lock(fd, exclusive=True)
        yield
    finally:
        try:
            platform_compat.release_lock(fd)
        finally:
            os.close(fd)


def _persist(
    keep: str | None = None,
    drop: str | None = None,
    pending: tuple[str, dict[str, Any]] | None = None,
    drop_holder: tuple[str, str, str] | None = None,
    require_digest: tuple[str, int, str] | None = None,
    require_unheld: tuple[str, str] | None = None,
) -> bool:
    """Merge this process's state into the sidecar under the cross-process lock.

    Caller holds :data:`_LOCK`. Holder identities are merged per kind rather
    than allowing one process's snapshot to erase another's lease. A durable
    ownership write refuses a different live owner before merging local state;
    stale identities are discarded only when PID absence or start-ID mismatch
    proves that process incarnation is gone.
    """
    try:
        with _cross_process_lock():
            disk = _read_disk_seeds()
            merged: dict[str, dict[str, Any]] = {}
            for key in set(disk) | set(_RECORDS):
                local = _RECORDS.get(key)
                on_disk = disk.get(key)
                source = on_disk or local
                if source is None:
                    continue
                entry = {
                    "size": source["size"],
                    "sha256": source["sha256"],
                    "holders": _empty_holders(),
                }
                if on_disk is not None:
                    disk_holders = _validated_holders(on_disk.get("holders")) or _empty_holders()
                    for kind in _HOLDER_KINDS:
                        entry["holders"][kind].update(disk_holders[kind])
                local_holders = _local_holders(key)
                if local_holders is None:
                    return False
                for kind in _HOLDER_KINDS:
                    entry["holders"][kind].update(local_holders[kind])
                merged[key] = entry

            if pending is not None:
                key, value = pending
                own_identity = _holder_identity()
                prior = disk.get(key)
                if prior is not None:
                    for _kind, _owner, identity in _live_holders(prior, _HOLDER_OWNERS):
                        if own_identity is None or identity != own_identity:
                            return False
                entry = merged.get(key, _copy_entry(value))
                entry["size"] = value["size"]
                entry["sha256"] = value["sha256"]
                local_holders = _local_holders(key)
                if local_holders is None:
                    return False
                for kind in _HOLDER_KINDS:
                    entry["holders"][kind].update(local_holders[kind])
                merged[key] = entry

            for entry in merged.values():
                holders = _validated_holders(entry.get("holders")) or _empty_holders()
                entry["holders"] = {
                    kind: {
                        owner: identity
                        for owner, identity in holders[kind].items()
                        if _holder_is_live(identity)
                    }
                    for kind in _HOLDER_KINDS
                }

            if require_digest is not None:
                key, size, sha = require_digest
                # The DISK side, not the merge: merged entries fall back to this
                # process's own _RECORDS when the sidecar has no entry, so a
                # record another process revoked would be resurrected from local
                # memory and vouch for bytes nothing durable stands behind. The
                # digest requirement is a claim about the DURABLE record.
                required_entry = disk.get(key)
                if (
                    required_entry is None
                    or required_entry["size"] != size
                    or required_entry["sha256"] != sha
                ):
                    return False

            if require_unheld is not None:
                key, owner = require_unheld
                own_identity = _holder_identity()
                held_entry = merged.get(key)
                if held_entry is not None:
                    for kind, held_owner, identity in _live_holders(held_entry):
                        if not (
                            kind == _HOLDER_OWNERS
                            and held_owner == owner
                            and own_identity is not None
                            and identity == own_identity
                        ):
                            return False

            if drop_holder is not None:
                key, kind, owner = drop_holder
                drop_entry = merged.get(key)
                if drop_entry is not None:
                    drop_entry["holders"][kind].pop(owner, None)
            if drop is not None:
                merged.pop(drop, None)

            for key in [
                candidate
                for candidate, entry in merged.items()
                if candidate != keep and not _live_holders(entry) and not os.path.isfile(candidate)
            ]:
                merged.pop(key, None)
                _RECORDS.pop(key, None)
                _LIVE.pop(key, None)

            snapshot = {"seeds": {key: _copy_entry(value) for key, value in merged.items()}}
            atomic_write(_sidecar_path(), json.dumps(snapshot), mode=0o600)
        return True
    except (OSError, ValueError, TypeError):  # pragma: no cover - disk full / perms
        logger.debug("could not persist seed-provenance sidecar", exc_info=True)
        return False


def claim(path: Path | str, owner: str, *, expect_digest: tuple[int, str] | None) -> bool:
    """Persistently reserve *path* for *owner* unless another live holder exists.

    ``expect_digest`` is the ``(size, sha256)`` the caller VALIDATED the file
    against -- required, because a claim is an authorization to rewrite and
    later delete the bytes at *path*, and the caller's belief about those bytes
    can go stale: another process may have recorded new bytes since this one
    cached the digest, after which the durable record describes different bytes
    than the ones the caller checked. The claim then lands only if the durable entry,
    re-read under the cross-process lock, still records exactly that digest --
    otherwise the caller would be adopting (and eventually deleting) a file the
    record stopped vouching for, such as a user's restoration of older bytes.
    ``None`` is for reserving a pathname with no digest belief (a fresh
    reservation before any record exists), never for adoption.
    """
    key = _key(path)
    require_digest = None
    if expect_digest is not None:
        size, sha = expect_digest
        require_digest = (key, size, sha)
    with _LOCK:
        if _SHARERS.get(key) and _LIVE.get(key) != owner:
            return False
        previous = _LIVE.get(key)
        if previous is not None and previous != owner:
            return False
        _LIVE[key] = owner
        if _persist(keep=key, require_digest=require_digest, require_unheld=(key, owner)):
            return True
        if previous is None:
            _LIVE.pop(key, None)
        else:
            _LIVE[key] = previous
        return False


def release(path: Path | str, owner: str) -> bool:
    """Give up *owner*'s claim. ``True`` only when memory and disk agree."""
    key = _key(path)
    with _LOCK:
        owned = _LIVE.get(key) == owner
        if owned:
            _LIVE.pop(key, None)
        if _persist(keep=key, drop_holder=(key, _HOLDER_OWNERS, owner)):
            return True
        if owned:
            _LIVE[key] = owner
        return False


def release_local(path: Path | str, owner: str) -> None:
    """Give up *owner*'s in-memory claim on loop-safe teardown paths.

    Durable holder withdrawal belongs to the off-loop discard transaction; this
    half takes no lock and performs no persistence.
    """
    key = _key(path)
    if _LIVE.get(key) == owner:
        _LIVE.pop(key, None)


def held_by_another(path: Path | str, owner: str) -> bool:
    """``True`` when a different live process or session owns *path*."""
    key = _key(path)
    local = _LIVE.get(key)
    if local is not None and local != owner:
        return True
    own_identity = _holder_identity()
    with _cross_process_lock():
        entry = _read_disk_seeds().get(key)
        if entry is None:
            return False
        return any(
            held_owner != owner or own_identity is None or identity != own_identity
            for _kind, held_owner, identity in _live_holders(entry, _HOLDER_OWNERS)
        )


def share(path: Path | str, payload: str, owner: str) -> bool:
    """Persist *owner* as a shared reader iff the durable digest matches *payload*."""
    key = _key(path)
    size = len(payload.encode("utf-8"))
    sha = digest(payload)
    with _LOCK:
        holders = _SHARERS.setdefault(key, set())
        newly_registered = owner not in holders
        holders.add(owner)
        if _persist(keep=key, require_digest=(key, size, sha)):
            _RECORDS[key] = {"size": size, "sha256": sha}
            return True
        if newly_registered:
            holders.discard(owner)
        return False


def unshare(path: Path | str, owner: str) -> bool:
    """Withdraw *owner*'s reader lease. ``True`` only when memory and disk agree."""
    key = _key(path)
    with _LOCK:
        holders = _SHARERS.get(key)
        registered = holders is not None and owner in holders
        if registered and holders is not None:
            holders.discard(owner)
        if _persist(keep=key, drop_holder=(key, _HOLDER_SHARERS, owner)):
            return True
        if registered:
            _SHARERS.setdefault(key, set()).add(owner)
        return False


def unshare_local(path: Path | str, owner: str) -> None:
    """Withdraw *owner*'s in-memory lease on loop-safe teardown paths.

    Durable holder withdrawal belongs to the off-loop discard transaction; this
    half takes no lock and performs no persistence.
    """
    key = _key(path)
    holders = _SHARERS.get(key)
    if holders is not None:
        holders.discard(owner)


def has_durable_record(path: Path | str) -> bool:
    """Whether the durable sidecar names *path*, whichever process wrote it.

    This cross-process existence probe serves callers whose answer must include
    only a record persisted to the shared sidecar: the create-race loser's poll
    ends when the winner's durable record exists, while a process-local cache can
    outlive a record another process removed. Grants nothing. Takes the
    cross-process lock for a bounded sidecar read, so it belongs on off-loop
    paths, same reasoning as :func:`has_sharers`.
    """
    key = _key(path)
    with _cross_process_lock():
        return _read_disk_seeds().get(key) is not None


def recorded_durable(path: Path | str) -> tuple[int, str] | None:
    """The durable ``(size, sha256)`` recorded for *path*, whoever wrote it.

    The cross-process sibling of :func:`recorded`, for callers whose
    authorization must include a record another process persisted. Grants
    nothing. Takes the cross-process lock for a bounded sidecar read, so it
    belongs on off-loop paths, same reasoning as :func:`has_sharers`.
    """
    key = _key(path)
    with _cross_process_lock():
        entry = _read_disk_seeds().get(key)
    if entry is None:
        return None
    size, sha = entry.get("size"), entry.get("sha256")
    if not isinstance(size, int) or not isinstance(sha, str) or not sha:
        return None
    return size, sha


def has_sharers(path: Path | str) -> bool:
    """Whether any live shared reader is registered in this or another process."""
    key = _key(path)
    if _SHARERS.get(key):
        return True
    with _cross_process_lock():
        entry = _read_disk_seeds().get(key)
        return entry is not None and bool(_live_holders(entry, _HOLDER_SHARERS))


def record(path: Path | str, payload: str, owner: str) -> bool:
    """Record that *owner* just wrote *payload* to *path*. ``True`` when the DISK agrees.

    Blocking (persists the sidecar) and serialized on :data:`_LOCK`; callers run it
    on the already-off-loop seed path. Re-recording the same bytes still rewrites,
    which is cheap and keeps the sidecar honest about the digest. *owner* is also
    claimed as the path's LIVE holder, so a sibling client in this process reads it
    as somebody's live seed rather than as an orphan.

    The return value is the same contract :func:`forget` carries, and for the same
    reason: a grant is only real once it is on disk. ``False`` means the sidecar
    write did not land, this process's records are exactly what a restart would
    read, and **the caller must not leave a seed behind** — a settings file with no
    durable grant is a ``permissions.defaultMode`` the user never approved that no
    later session is permitted to re-seed or remove, so it outlives every session on
    the host. Withdrawing the seed is the only outcome that stays inside this
    module's invariant, which is why this is not best-effort.

    **The entry reaches ``_RECORDS`` only once the sidecar write has landed.** The
    lock-free readers -- :func:`share` above all -- see ``_RECORDS`` at any instant,
    and an entry published before the persist would let a sibling validate a share
    against a grant that then fails: a governed reader left on a file no later
    session can recognize. Publishing after means a failing persist is invisible --
    on a re-seed the previous durable entry simply stays in place, still naming the
    bytes the caller's pre-write copy holds, so restoring that copy returns the path
    to exactly the recognized state a restart would read.
    """
    key = _key(path)
    with _LOCK:
        # Captured under the lock, before either live registry is touched,
        # because a refused persist must reproduce both prior roles exactly.
        previous_live = _LIVE.get(key)
        sharers = _SHARERS.get(key)
        was_sharer = sharers is not None and owner in sharers
        # ``_LIVE`` is taken up front so the window where the seed exists on disk
        # but its record is still persisting never reads as an ORPHAN to a
        # sibling. Promotion drops this owner's reader role before the same
        # persist, so owner and own-sharer can never coexist durably.
        _LIVE[key] = owner
        if sharers is not None:
            sharers.discard(owner)
        entry = {"size": len(payload.encode("utf-8")), "sha256": digest(payload)}
        if _persist(
            keep=key,
            pending=(key, entry),
            drop_holder=(key, _HOLDER_SHARERS, owner),
        ):
            _RECORDS[key] = entry
            return True
        if previous_live is None:
            _LIVE.pop(key, None)
        else:
            _LIVE[key] = previous_live
        if was_sharer:
            _SHARERS.setdefault(key, set()).add(owner)
        return False


def recorded(path: Path | str, owner: str) -> tuple[int, str] | None:
    """The ``(size, sha256)`` Crew wrote to *path*, as far as *owner* may claim it.

    ``None`` when nothing was recorded, or when a DIFFERENT owner in this process
    is still seeding that path: the record then describes a live session's file,
    not an orphan, and re-seeding it would overwrite that session's permission
    mode and delete its file on this client's reset.

    In-memory only, so this is safe to call from the event loop — and deliberately
    LOCK-FREE for the same reason: :data:`_LOCK` is held across the sidecar write,
    so taking it here would let a worker's disk I/O stall the one loop. Each
    statement below is a single dict read, which is atomic under CPython, so a
    concurrent :func:`record` can only make this return the older or the newer
    entry, never a torn one. The size is returned alongside the digest so a caller
    can reject a mismatched file without reading it, and bound its read to exactly
    the bytes it will hash.
    """
    key = _key(path)
    live = _LIVE.get(key)
    if live is not None and live != owner:
        return None
    entry = _RECORDS.get(key)
    if not entry:
        return None
    size, sha = entry.get("size"), entry.get("sha256")
    if not isinstance(size, int) or not isinstance(sha, str) or not sha:
        return None
    return size, sha


def forget(path: Path | str, owner: str) -> bool:
    """Durably revoke *owner* only when no other live holder protects *path*."""
    key = _key(path)
    with _LOCK:
        previous_live = _LIVE.get(key)
        if previous_live is not None and previous_live != owner:
            return False
        entry = _RECORDS.pop(key, None)
        _LIVE.pop(key, None)
        if _persist(drop=key, require_unheld=(key, owner)):
            return True
        if entry is not None:
            _RECORDS[key] = entry
        if previous_live is not None:
            _LIVE[key] = previous_live
        return False
