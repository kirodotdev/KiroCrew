"""Per-directory consent for loading a project's own ``.kiro/skills``.

A ``SKILL.md`` is prose, not code, but it enters the agent's context and can
instruct the agent to run anything. Loading one out of whatever repository the
operator happens to open is therefore an execution-adjacent decision: a cloned
repository could ship instructions the operator never read. This module is the
consent record that gates it.

Trust is keyed on the **canonical** project directory (``os.path.realpath``),
because the directory *is* the resource. Keying on any softer identity -- a
display name, a slug, an index entry -- leaves the unkeyed component forgeable:
a second name aliasing one directory would grant itself separate trust, and a
rename would orphan the record.

Storage is ``<data home>/trust/project-skills.json``. That directory is already
a whole-directory entry on the keystone deny list, so the agent's own file tools
can neither read nor write this store; like every other keystone reader, this
module opens the path directly rather than through the agent file gate.

The gate fails **closed** everywhere: an unreadable store, a malformed store, or
an unreadable config all yield "nothing is trusted" rather than a permissive
default. Refusing to load a skill costs the operator a click; loading one they
did not consent to cannot be undone.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Subdirectory of the data home holding trust-root material. Shared with the
#: SEL signing project_key so a single keystone entry covers both.
_TRUST_SUBDIR = "trust"

_STORE_FILENAME = "project-skills.json"

#: Owner-only: a world-readable grant list tells a local attacker which
#: directories are worth planting a SKILL.md in.
_STORE_MODE = 0o600

#: Current on-disk schema version. A store written by a newer build is treated
#: as unreadable (fail closed) rather than guessed at.
_SCHEMA_VERSION = 1

#: Bound the per-decision cost of a pathological store. Mirrors the app-trust
#: reader: truncate to the first N rather than denying outright, since an
#: append-ordered list keeps the operator's real grants at the front.
_MAX_GRANT_ENTRIES = 512

#: Cached ``(stat_signature, frozenset_of_keys)``. The enforcement read happens
#: on the event loop during dashboard listing, so re-parsing the store on every
#: skill would be a syscall per row; a stat signature is one syscall total.
_cache: tuple[tuple[int, int, int], frozenset[str]] | None = None


class TrustStoreUnreadable(RuntimeError):
    """The store exists but cannot be trusted to round-trip.

    Raised only to the WRITE paths, so a grant or revoke refuses instead of
    replacing grants it could not read. Never raised to the enforcement reader,
    which fails closed by granting nothing.
    """


def store_path() -> Path:
    """Absolute path of the grant store."""
    return config_dir() / _TRUST_SUBDIR / _STORE_FILENAME


def canonical_key(project_dir: str | Path | None) -> str | None:
    """Return the canonical trust project_key for *project_dir*, or ``None``.

    ``None`` means "this value cannot identify a project directory", which every
    caller must treat as untrusted. A relative path, a file, a dangling symlink
    and a nonexistent path all land here: a value that cannot name a real
    directory has no business matching a grant.

    Resolution is ``os.path.realpath``, so a symlink cannot alias its way to a
    grant belonging to a different real directory.

    This performs filesystem syscalls. Callers on the event loop should resolve
    once per request and pass the result down rather than calling it per skill.
    """
    if project_dir is None:
        return None
    raw = str(project_dir).strip()
    if not raw:
        return None
    try:
        expanded = os.path.expanduser(raw)
        if not os.path.isabs(expanded):
            return None
        real = os.path.realpath(expanded)
        if not os.path.isdir(real):
            return None
    except (OSError, ValueError):
        return None
    return real


def _project_skills_enabled() -> bool:
    """The operator's hard off switch.

    Independent of any grant: with this false, project skills are impossible
    even for a directory that carries one. Fails closed -- an unreadable config
    disables the feature rather than enabling it.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return bool(KiroCrewConfig.load().skills.project_skills_enabled)
    except Exception as exc:  # noqa: BLE001 - unreadable policy must fail closed
        logger.error(
            "skills.project_skills_enabled unreadable (%s); " "refusing every project-skills grant",
            exc,
        )
        return False


def _store_signature(path: Path) -> tuple[int, int, int] | None:
    """Cheap change-detector for the store: ``(mtime_ns, size, inode)``."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _parse_store(text: str) -> frozenset[str]:
    """Parse store *text* into a set of canonical keys, failing closed.

    Every malformed shape yields the empty set: a store we cannot understand
    grants nothing.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("%s: not valid JSON (%s); ignoring every grant", _STORE_FILENAME, exc)
        return frozenset()
    if not isinstance(data, dict):
        logger.error("%s: not a JSON object; ignoring every grant", _STORE_FILENAME)
        return frozenset()
    version = data.get("version")
    if version != _SCHEMA_VERSION:
        logger.error(
            "%s: schema version %r is not %d; ignoring every grant",
            _STORE_FILENAME,
            version,
            _SCHEMA_VERSION,
        )
        return frozenset()
    raw = data.get("granted")
    if not isinstance(raw, list):
        logger.error("%s: 'granted' is not an array; ignoring every grant", _STORE_FILENAME)
        return frozenset()
    if len(raw) > _MAX_GRANT_ENTRIES:
        logger.error(
            "%s: %d entries exceeds the %d cap; considering only the first %d",
            _STORE_FILENAME,
            len(raw),
            _MAX_GRANT_ENTRIES,
            _MAX_GRANT_ENTRIES,
        )
        raw = raw[:_MAX_GRANT_ENTRIES]
    keys: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        # Stored keys are already canonical, but an absolute-path check still
        # applies: a relative entry in a hand-edited store must not match a
        # caller's canonical project_key by accident.
        if isinstance(path, str) and path and os.path.isabs(path):
            keys.add(path)
    return frozenset(keys)


def trusted_keys() -> frozenset[str]:
    """Every canonical directory the operator has granted, or an empty set.

    Result is cached against the store's stat signature, so repeated
    enforcement reads within one listing cost a single ``stat``.
    """
    global _cache
    if not _project_skills_enabled():
        return frozenset()
    path = store_path()
    signature = _store_signature(path)
    if signature is None:
        _cache = None
        return frozenset()
    cached = _cache
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("%s: unreadable (%s); ignoring every grant", _STORE_FILENAME, exc)
        _cache = None
        return frozenset()
    keys = _parse_store(text)
    _cache = (signature, keys)
    return keys


def is_project_trusted(project_dir: str | Path | None) -> bool:
    """Whether *project_dir*'s own ``.kiro/skills`` may be loaded."""
    project_key = canonical_key(project_dir)
    if project_key is None:
        return False
    return project_key in trusted_keys()


def is_key_trusted(project_key: str | None) -> bool:
    """Membership test for an already-canonical project_key.

    Split out so a hot path can resolve the project_key once off the event loop and
    then test membership without further syscalls.
    """
    if not project_key:
        return False
    return project_key in trusted_keys()


def _trust_dir() -> Path:
    """The trust directory, verified to be a real directory.

    A pre-planted link here would redirect the grant write somewhere the agent
    can author, letting it forge a grant for a directory the operator never
    approved. Only the link is removed, never its target.

    ``is_link_or_junction`` rather than ``Path.is_symlink``: on Windows a
    DIRECTORY JUNCTION is not a symlink, so an ``is_symlink`` check would walk
    straight through a planted junction and write the store inside it.
    """
    directory = config_dir() / _TRUST_SUBDIR
    if platform_compat.is_link_or_junction(directory):
        logger.error("%s is a link; removing it before writing trust state", directory)
        platform_compat.unlink_link_or_junction(directory)
    # make_owner_only_dir rather than mkdir(mode=0o700): the mode argument is a
    # POSIX permission bit and is a NO-OP on Windows, so a permissive data-home
    # ACL would leave the grant store replaceable by another local account --
    # which forges project consent that this gate then enforces. Fails loud
    # rather than degrading to a world-readable trust root.
    platform_compat.make_owner_only_dir(directory)
    return directory


@contextmanager
def _locked_store(*, exclusive: bool = True) -> Iterator[None]:
    """Hold a lock on the store for the duration of the block.

    One lock spans an entire read-modify-write so two concurrent grants cannot
    lose an update, and a revoke racing a grant cannot leave a revoked
    directory trusted.
    """
    # Every step here can fail before any store I/O: the trust dir may not be
    # creatable or lockable-down, touch/open fail on a read-only filesystem or on
    # permissions, and the lock call itself can fail. Those are surfaced as
    # TrustStoreUnreadable rather than raw OSError because they mean the same
    # thing the callers already handle -- the store cannot be trusted to
    # round-trip, so a mutator must refuse and a listing must degrade. Left as
    # OSError they escaped as 500s, including from list_trusted_projects, whose
    # contract is explicitly to degrade rather than break a settings page.
    try:
        lock_path = _trust_dir() / (_STORE_FILENAME + ".lock")
        lock_path.touch(exist_ok=True)
        # "r+" not "r": Windows msvcrt.locking needs write access on the fd, and a
        # read-only handle degrades the lock to a silent no-op.
        handle = open(lock_path, "r+")
    except OSError as exc:
        raise TrustStoreUnreadable(f"trust store is not lockable: {exc}") from exc
    try:
        try:
            lock = platform_compat.file_lock(handle.fileno(), exclusive=exclusive)
        except OSError as exc:
            raise TrustStoreUnreadable(f"trust store lock failed: {exc}") from exc
        with lock:
            yield
    finally:
        handle.close()


def _read_entries_unlocked() -> list[dict[str, Any]]:
    """Read raw grant entries. Caller must hold the lock.

    Returns ``[]`` ONLY for a store that does not exist yet. Anything else that
    cannot be round-tripped -- an unreadable file, malformed JSON, a non-object
    document, or a schema version this build does not know -- raises
    ``TrustStoreUnreadable``.

    The distinction is load-bearing for the MUTATORS. "Absent" means there are
    no grants and a write is safe; "unreadable" means there may be grants this
    build cannot see, so appending to an empty list and writing it back would
    silently destroy every one of them. The enforcement reader still fails
    CLOSED on the same conditions (it grants nothing); only the write paths need
    to refuse rather than overwrite.
    """
    path = store_path()
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TrustStoreUnreadable(f"{_STORE_FILENAME} is unreadable: {exc}") from exc
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise TrustStoreUnreadable(f"{_STORE_FILENAME} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TrustStoreUnreadable(f"{_STORE_FILENAME} is not a JSON object")
    version = data.get("version")
    if version != _SCHEMA_VERSION:
        raise TrustStoreUnreadable(
            f"{_STORE_FILENAME} schema version {version!r} is not {_SCHEMA_VERSION}; "
            "refusing to overwrite a store this build cannot read"
        )
    raw = data.get("granted")
    if not isinstance(raw, list):
        raise TrustStoreUnreadable(f"{_STORE_FILENAME} 'granted' is not an array")
    return [entry for entry in raw if isinstance(entry, dict)]


def _write_entries_unlocked(entries: list[dict[str, Any]]) -> None:
    """Persist grant *entries*. Caller must hold the lock."""
    global _cache
    _trust_dir()
    payload = {"version": _SCHEMA_VERSION, "granted": entries}
    atomic_write(
        store_path(),
        json.dumps(payload, indent=2) + "\n",
        # restrict_to_owner rather than a POSIX mode: the mode bits are ignored
        # on Windows, where this helper applies a real owner-only ACL instead.
        # It implies 0o600 on POSIX, so passing both would be refused.
        restrict_to_owner=True,
    )
    # The next read re-stats and re-parses rather than trusting a value this
    # process cached before the write.
    _cache = None


def grant_project_trust(project_dir: str | Path, *, session_key: str = "") -> str:
    """Record consent for *project_dir* and return its canonical project_key.

    Raises ``ValueError`` when *project_dir* cannot name a real directory, so a
    caller cannot bank a grant against a path that will never match.

    The grant is audited with ``critical=True``: this is a one-time human
    security decision, and an audit that cannot be written must refuse it
    rather than record consent nowhere.
    """
    project_key = canonical_key(project_dir)
    if project_key is None:
        raise ValueError(f"not an existing absolute directory: {project_dir!r}")
    with _locked_store():
        # Read BEFORE auditing: an unreadable store refuses here, and auditing
        # first would leave an "allowed" record for consent that never landed.
        entries = _read_entries_unlocked()
        for entry in entries:
            if entry.get("path") == project_key:
                return project_key
        # Audited BEFORE the write, with critical=True: this is a one-time human
        # security decision, and an audit that cannot be written must refuse it
        # rather than record consent nowhere.
        sel().log_governance_decision(
            session_key=session_key,
            tool_name="skill_trust",
            scope="project_skills",
            item=project_key,
            outcome="allowed",
            rule="operator_granted_project_skills",
            reason="operator granted project-skills trust for this directory",
            critical=True,
        )
        entries.append({"path": project_key, "granted_at": int(time.time())})
        if len(entries) > _MAX_GRANT_ENTRIES:
            # Keep the newest grant rather than silently dropping the click the
            # operator just made; the oldest entry is the one they care least
            # about.
            entries = entries[-_MAX_GRANT_ENTRIES:]
        _write_entries_unlocked(entries)
    return project_key


def revoke_project_trust(project_dir: str | Path, *, session_key: str = "") -> bool:
    """Withdraw consent for *project_dir*. Returns whether a grant was removed.

    Revocation deliberately does **not** require the directory to still exist:
    an operator must be able to withdraw trust from a path they have already
    deleted or moved, so this matches on the stored string as well as on the
    canonical project_key.
    """
    project_key = canonical_key(project_dir)
    raw = str(project_dir).strip()
    candidates = {c for c in (project_key, raw, os.path.expanduser(raw)) if c}
    removed = False
    with _locked_store():
        entries = _read_entries_unlocked()
        kept = [e for e in entries if e.get("path") not in candidates]
        if len(kept) != len(entries):
            _write_entries_unlocked(kept)
            removed = True
    if removed:
        # Audited AFTER the write, and OUTSIDE the lock, and deliberately so --
        # the reverse of the grant path. A revoke is a DE-ESCALATION: refusing it
        # because the audit could not be written would leave trust IN PLACE and
        # the project's skills still loading, which is worse than an unrecorded
        # revoke, and would let anyone able to make the SEL unwritable veto every
        # revocation. Same rule, and nearly the same words, as
        # safety_override.deactivate. Fail closed on escalation, fail open on
        # de-escalation.
        #
        # Containing the failure is the part that was actually broken: the
        # OSError from a critical audit used to escape to aiohttp as a 500, so the
        # operator was told the revoke failed when it had durably succeeded -- and
        # a retry returned removed=False, skipping the audit and losing the record
        # permanently. critical=True stays INSIDE the try because it flushes the
        # chain and writes synchronously, making the record more likely to land;
        # the except is what keeps it from reaching the caller. Outside the lock
        # because holding an flock across synchronous SEL I/O plus a backlog
        # flush is the rule violation safety_override names.
        try:
            sel().log_governance_decision(
                session_key=session_key,
                tool_name="skill_trust",
                scope="project_skills",
                item=project_key or raw,
                outcome="denied",
                rule="operator_revoked_project_skills",
                reason="operator revoked project-skills trust for this directory",
                critical=True,
            )
        except Exception:  # noqa: BLE001 — an unaudited revoke beats a blocked one
            logger.error(
                "SEL audit failed for project-skills revoke of %s; "
                "trust IS revoked and the store write stands",
                project_key or raw,
                exc_info=True,
            )
    return removed


def _as_epoch(value: Any) -> int:
    """Coerce a stored ``granted_at`` to a sortable epoch-seconds int.

    A hand-edited store can carry a string (or anything else) here, and Python
    refuses to order a str against an int -- which would turn the listing
    endpoint into a 500 rather than showing the operator their own grants.

    Returns an ``int`` because that is what the store writes and what the API
    reports; normalizing to ``float`` here would silently change ``granted_at``
    on the wire from ``1787261990`` to ``1787261990.0``. A bool is not a
    timestamp (and ``isinstance(True, int)`` is True), so it is rejected first.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def list_trusted_projects() -> list[dict[str, Any]]:
    """Every stored grant, newest first, for display.

    Reports the raw stored rows rather than the enforced set so a UI can show
    a grant whose directory has since disappeared -- otherwise a stale entry
    would be invisible and un-revokable.

    An unreadable store yields an empty list rather than raising: listing
    destroys nothing, so a read failure must not turn a settings page into a
    500. The mutators are where refusing matters.
    """
    try:
        with _locked_store(exclusive=False):
            entries = _read_entries_unlocked()
    except TrustStoreUnreadable as exc:
        logger.error("%s; listing no grants", exc)
        return []
    rows: list[dict[str, Any]] = []
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        rows.append(
            {
                "path": path,
                "granted_at": _as_epoch(entry.get("granted_at")),
                "exists": os.path.isdir(path),
            }
        )
    # Rows carry an already-normalized int, so the sort cannot meet a string
    # here. Sorting on the emitted value keeps ONE normalization point rather
    # than a second, unpinnable copy of the same coercion.
    rows.sort(key=lambda r: r["granted_at"], reverse=True)
    return rows


def reset_cache_for_tests() -> None:
    """Drop the memoized enforcement read.

    The cache keys on a stat signature, and a test that writes a store twice
    within the same filesystem timestamp granularity can otherwise observe the
    first value.
    """
    global _cache
    _cache = None
