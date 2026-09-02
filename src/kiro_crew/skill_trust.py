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
from kiro_crew.config.loader import KiroCrewConfig
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
_StoreSignature = tuple[int, int, int, int, int]
#: Cached ``(stat_signature, granted_paths, enforcement_tokens)``. Both sets are
#: derived from one parse -- see :func:`_parse_store` for why they differ.
_cache: tuple[_StoreSignature, frozenset[str], frozenset[str]] | None = None


class TrustStoreUnreadable(RuntimeError):
    """The store exists but cannot be trusted to round-trip.

    Raised only to the WRITE paths, so a grant or revoke refuses instead of
    replacing grants it could not read. Never raised to the enforcement reader,
    which fails closed by granting nothing.
    """


class TrustStoreFull(RuntimeError):
    """A new grant cannot be recorded without discarding an existing one."""


class ReviewedProjectChanged(RuntimeError):
    """The project no longer has the canonical identity the operator reviewed."""


_EXPECTED_KEY_UNSET = object()

_PROJECT_SKILL_TRAVERSAL_SUPPORTED = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.scandir in os.supports_fd
)


def project_skill_traversal_supported() -> bool:
    """Whether project trees can be walked without resolving path components.

    Python exposes the required ``openat``/directory-descriptor primitives on
    POSIX. It does not expose an equivalent handle-relative, no-reparse walk on
    Windows, where a path lookup can initiate SMB authentication before a later
    containment check can reject the target. Unsupported platforms therefore
    fail closed before canonicalizing any project path.
    """
    return _PROJECT_SKILL_TRAVERSAL_SUPPORTED


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
    if not project_skill_traversal_supported() or project_dir is None:
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


#: Separator between a grant's path and its instance identity in the membership
#: token. NUL cannot appear in a path on any supported platform, so a crafted
#: directory name cannot make one half read as the other.
_TOKEN_SEP = "\x00"


def _instance_identity(project_key: str) -> str | None:
    """The directory's non-reusable instance identity, or ``None`` if unreadable.

    A grant is consent for the CONTENT the operator reviewed, but a path is a
    reusable name. Delete the reviewed repository and create a different one at
    the same canonical path -- a routine move on a shared dev host, and the normal
    life of a CI checkout directory -- and a path-only grant is inherited by
    content nobody reviewed, whose ``SKILL.md`` then enters the agent context with
    instruction authority the operator never gave it. Binding an identity the new
    directory cannot forge turns that silent inheritance into a consent re-prompt.

    ``st_dev``/``st_ino`` name the directory INSTANCE rather than its name.
    ``st_birthtime`` is folded in where the platform exposes it (macOS, the BSDs,
    Windows) because an inode number can itself be reused after a delete, which is
    the very case being closed; on Linux, where CPython does not expose a birth
    time, ``dev:ino`` alone still discriminates every recreate that lands on a
    different inode -- which a fresh clone and a moved tree both do.

    Deliberately NOT a content fingerprint of the ``.kiro/skills`` tree, and
    deliberately not ``st_ctime``: both change when the operator edits their own
    skills, so either would re-prompt on ordinary work instead of on a
    substitution, and a consent prompt that fires routinely is one that gets
    clicked through.

    Two residuals, stated rather than implied closed, both specific to a platform
    that exposes no birth time (Linux, where ``dev:ino`` stands alone):

    * The ACCIDENTAL case -- a different repository at a path the operator happens
      to have granted -- is fully closed, because a fresh clone or a moved tree
      lands on a different inode. The ADVERSARIAL case is closed only
      best-effort: a local actor who can already write that path can grind
      create/delete until the filesystem reuses the inode. Narrowing that needs a
      real birth time (``statx`` ``STATX_BTIME``), which CPython does not surface
      on Linux today; folding it in when it does is the fix.
    * ``st_dev`` is not stable across remounts on btrfs subvolumes, NFS, and some
      container bind mounts, so a bound grant can stop matching after a reboot
      with the content unchanged. That direction is fail-closed -- the operator is
      re-asked, never over-trusted -- but it is a re-prompt they did not earn, and
      so the same habituation risk this docstring rejects a content fingerprint
      for. If it shows up in practice the answer is to compare the inode plus a
      content-independent tie-break, not to relax the check.
    """
    try:
        st = os.stat(project_key)
    except OSError:
        return None
    parts = [str(st.st_dev), str(st.st_ino)]
    birth = getattr(st, "st_birthtime_ns", None)
    if birth is None:
        birth = getattr(st, "st_birthtime", None)
    if birth is not None:
        parts.append(str(birth))
    return ":".join(parts)


def _binding_token(project_key: str, identity: str) -> str:
    """The enforcement-set membership token for an identity-bound grant."""
    return f"{project_key}{_TOKEN_SEP}{identity}"


def _project_skills_enabled() -> bool:
    """The operator's hard off switch.

    Independent of any grant: with this false, project skills are impossible
    even for a directory that carries one. Fails closed -- an unreadable config
    disables the feature rather than enabling it.
    """
    try:
        return bool(KiroCrewConfig.load().skills.project_skills_enabled)
    except Exception as exc:  # noqa: BLE001 - unreadable policy must fail closed
        logger.error(
            "skills.project_skills_enabled unreadable (%s); " "refusing every project-skills grant",
            exc,
        )
        return False


def _store_signature(path: Path) -> _StoreSignature | None:
    """Detect content, identity, and permission-state changes to the store."""
    try:
        st = path.stat()
    except OSError:
        return None
    # chmod/setfacl changes ctime without changing content metadata; st_mode is
    # included as a direct guard even on filesystems with coarse ctime. A cached
    # grant must never bypass the unreadable-store fail-closed path after access
    # to the store has been withdrawn.
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino, st.st_mode)


def _parse_store(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """Parse store *text* into ``(granted_paths, enforcement_tokens)``, failing closed.

    Two sets, because they answer two different questions.

    ``granted_paths`` is what the operator granted, for display and inspection,
    and includes every well-formed row. ``enforcement_tokens`` is what a caller has
    to MATCH, and includes ONLY identity-bound rows: a row written before grants
    carried an identity grants nothing until it is re-granted, because the whole
    point of the binding is that a path alone cannot establish which directory the
    operator reviewed -- and that is no less true of a row this build inherited
    than of one it wrote.

    Keeping the two apart is what makes that fail-closed without being
    destructive: the row stays listed, so the operator SEES the grant and can
    re-grant or revoke it, rather than having it silently deleted or silently
    honored. :func:`list_trusted_projects` reports ``bound`` for exactly this.

    Every malformed shape yields empty sets: a store we cannot understand grants
    nothing.
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        logger.error("%s: not valid JSON (%s); ignoring every grant", _STORE_FILENAME, exc)
        return frozenset(), frozenset()
    if not isinstance(data, dict):
        logger.error("%s: not a JSON object; ignoring every grant", _STORE_FILENAME)
        return frozenset(), frozenset()
    version = data.get("version")
    if version != _SCHEMA_VERSION:
        logger.error(
            "%s: schema version %r is not %d; ignoring every grant",
            _STORE_FILENAME,
            version,
            _SCHEMA_VERSION,
        )
        return frozenset(), frozenset()
    raw = data.get("granted")
    if not isinstance(raw, list):
        logger.error("%s: 'granted' is not an array; ignoring every grant", _STORE_FILENAME)
        return frozenset(), frozenset()
    if len(raw) > _MAX_GRANT_ENTRIES:
        logger.error(
            "%s: %d entries exceeds the %d cap; considering only the first %d",
            _STORE_FILENAME,
            len(raw),
            _MAX_GRANT_ENTRIES,
            _MAX_GRANT_ENTRIES,
        )
        raw = raw[:_MAX_GRANT_ENTRIES]
    paths: set[str] = set()
    tokens: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        # Stored keys are already canonical, but an absolute-path check still
        # applies: a relative entry in a hand-edited store must not match a
        # caller's canonical project_key by accident.
        if not (isinstance(path, str) and path and os.path.isabs(path)):
            continue
        paths.add(path)
        identity = entry.get("identity")
        if isinstance(identity, str) and identity:
            # Identity-bound: ONLY the directory instance the operator reviewed
            # matches. The bare path is deliberately NOT also a token -- adding it
            # would make the binding decorative.
            tokens.add(_binding_token(path, identity))
        # A row with no identity contributes NO token: it is listed but not
        # enforced. It cannot say WHICH directory was reviewed, so honoring it
        # would leave open exactly the replacement path this binding closes.
    return frozenset(paths), frozenset(tokens)


def _read_store() -> tuple[frozenset[str], frozenset[str]]:
    """``(granted_paths, enforcement_tokens)`` from the store, or two empty sets.

    Result is cached against the store's stat signature, so repeated enforcement
    reads within one listing cost a single ``stat``.
    """
    global _cache
    if not _project_skills_enabled():
        return frozenset(), frozenset()
    path = store_path()
    signature = _store_signature(path)
    if signature is None:
        _cache = None
        return frozenset(), frozenset()
    cached = _cache
    if cached is not None and cached[0] == signature:
        return cached[1], cached[2]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("%s: unreadable (%s); ignoring every grant", _STORE_FILENAME, exc)
        _cache = None
        return frozenset(), frozenset()
    paths, tokens = _parse_store(text)
    _cache = (signature, paths, tokens)
    return paths, tokens


def trusted_keys() -> frozenset[str]:
    """Every canonical directory the operator has granted, or an empty set.

    The granted PATHS, for display and inspection. Deliberately NOT the
    enforcement oracle: a grant is bound to the directory INSTANCE the operator
    reviewed, so membership here does not by itself mean a directory may be
    loaded. Enforcement goes through :func:`is_key_trusted`, which compares that
    binding -- see :func:`_instance_identity` for what a path-only check lets
    through.
    """
    return _read_store()[0]


def is_project_trusted(project_dir: str | Path | None) -> bool:
    """Whether *project_dir*'s own ``.kiro/skills`` may be loaded."""
    project_key = canonical_key(project_dir)
    if project_key is None:
        return False
    return is_key_trusted(project_key)


def is_key_trusted(project_key: str | None) -> bool:
    """Membership test for an already-canonical project_key.

    Split out so a hot path can resolve the project_key once off the event loop and
    then test membership without re-reading the store.

    Costs one ``stat`` of *project_key* on top of the store's cached signature
    read. That is the price of the binding: a grant names the directory INSTANCE
    the operator reviewed, not just its name, so the instance has to be read to be
    compared -- see :func:`_instance_identity` for what a path-only grant lets
    through. One extra ``stat`` per key derivation, not per skill; the caller in
    ``skills.py`` already pays a ``realpath`` beside it.

    Fails CLOSED on a row with no recorded identity (one written before grants
    carried one) and on a directory that can no longer be read. Neither can
    establish WHICH directory the operator reviewed, and an unenforced row is not a
    deleted one: it stays listed and is reported unbound, so the operator is
    re-ASKED rather than silently un-consented. See :func:`_parse_store`.
    """
    if not project_key:
        return False
    granted = _read_store()[1]
    if not granted:
        return False
    identity = _instance_identity(project_key)
    if identity is None:
        return False
    return _binding_token(project_key, identity) in granted


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
    # which forges project consent that this gate then enforces. Its tightening
    # step is deliberately best-effort for general callers, so retry with the
    # fail-loud primitive before treating this security boundary as usable.
    platform_compat.make_owner_only_dir(directory)
    platform_compat.restrict_dir_to_owner(directory)
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
    except ValueError as exc:
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
    if any(not isinstance(entry, dict) for entry in raw):
        raise TrustStoreUnreadable(
            f"{_STORE_FILENAME} contains a non-object grant; refusing to overwrite it"
        )
    return raw


def _write_entries_unlocked(entries: list[dict[str, Any]]) -> None:
    """Persist grant *entries*. Caller must hold the lock."""
    global _cache
    payload = {"version": _SCHEMA_VERSION, "granted": entries}
    try:
        _trust_dir()
        atomic_write(
            store_path(),
            json.dumps(payload, indent=2) + "\n",
            # restrict_to_owner rather than a POSIX mode: the mode bits are ignored
            # on Windows, where this helper applies a real owner-only ACL instead.
            # It implies 0o600 on POSIX, so passing both would be refused.
            restrict_to_owner=True,
        )
    except OSError as exc:
        raise TrustStoreUnreadable(f"{_STORE_FILENAME} is not writable: {exc}") from exc
    # The next read re-stats and re-parses rather than trusting a value this
    # process cached before the write.
    _cache = None


def grant_project_trust(
    project_dir: str | Path,
    *,
    expected_key: object = _EXPECTED_KEY_UNSET,
    session_key: str = "",
) -> str:
    """Record consent for *project_dir* and return its canonical project_key.

    Raises ``ValueError`` when *project_dir* cannot name a real directory, so a
    caller cannot bank a grant against a path that will never match.

    When *expected_key* is supplied, it is the opaque canonical identity shown
    to the operator. The one resolution performed here is compared and then
    persisted verbatim. Keeping both operations in this primitive prevents the
    canonical directory name itself from being replaced between a handler-side
    check and a second resolution here. Internal callers that are not recording
    an interactive review may omit the confirmation.

    The grant is audited with ``critical=True``: this is a one-time human
    security decision, and an audit that cannot be written must refuse it
    rather than record consent nowhere.
    """
    project_key = canonical_key(project_dir)
    if project_key is None:
        raise ValueError(f"not an existing absolute directory: {project_dir!r}")
    if expected_key is not _EXPECTED_KEY_UNSET and (
        not isinstance(expected_key, str) or expected_key != project_key
    ):
        raise ReviewedProjectChanged(str(expected_key or ""))
    # Read before the lock so an unreadable directory refuses without taking it.
    # A grant that cannot be bound is not recorded unbound: that would write
    # exactly the path-only row this binding exists to stop producing.
    identity = _instance_identity(project_key)
    if identity is None:
        raise ValueError(f"not a readable directory: {project_dir!r}")
    with _locked_store():
        # Read BEFORE auditing: an unreadable store refuses here, and auditing
        # first would leave an "allowed" record for consent that never landed.
        entries = _read_entries_unlocked()
        for entry in entries:
            if entry.get("path") != project_key:
                continue
            if entry.get("identity") == identity:
                return project_key
            # Same path, a different directory instance -- or a legacy row with
            # no identity at all. The operator is granting THIS content, so
            # rebind rather than short-circuit: returning early here would
            # leave the new tree untrusted even after an explicit re-grant,
            # with no surface saying why.
            entries = [e for e in entries if e.get("path") != project_key]
            break
        if len(entries) >= _MAX_GRANT_ENTRIES:
            raise TrustStoreFull(
                f"project-skills trust store is full ({_MAX_GRANT_ENTRIES} grants); "
                "revoke an existing grant before adding another"
            )
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
        entries.append(
            {
                "path": project_key,
                "identity": identity,
                "granted_at": int(time.time()),
            }
        )
        _write_entries_unlocked(entries)
    return project_key


def revoke_project_trust(project_dir: str | Path, *, session_key: str = "") -> bool:
    """Withdraw consent for *project_dir*. Returns whether a grant was removed.

    Revocation deliberately does **not** require the directory to still exist:
    an operator must be able to withdraw trust from a path they have already
    deleted or moved, so this matches on the stored string as well as on the
    canonical project_key.
    """
    raw = str(project_dir).strip()
    expanded = os.path.expanduser(raw)
    project_key: str | None = None
    removed = False

    # Match the stored identity before interpreting request text as a filesystem
    # path. On Windows, resolving an unmatched UNC/device path can initiate SMB
    # authentication to an attacker-controlled host. An exact stored key needs
    # no resolution at all, which also keeps vanished network grants revocable.
    with _locked_store():
        entries = _read_entries_unlocked()
        exact_candidates = {candidate for candidate in (raw, expanded) if candidate}
        kept = [e for e in entries if e.get("path") not in exact_candidates]
        if len(kept) != len(entries):
            _write_entries_unlocked(kept)
            removed = True

    normalized = raw.replace("\\", "/")
    if not removed and not normalized.startswith("//"):
        project_key = canonical_key(project_dir)
        if project_key:
            with _locked_store():
                entries = _read_entries_unlocked()
                kept = [e for e in entries if e.get("path") != project_key]
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
                "SEL audit failed for project-skills revoke; "
                "trust IS revoked and the store write stands",
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
        identity = entry.get("identity")
        rows.append(
            {
                "path": path,
                "granted_at": _as_epoch(entry.get("granted_at")),
                "exists": os.path.isdir(path),
                # Whether this row is bound to the directory INSTANCE reviewed,
                # or is a pre-binding row still matching on the path alone.
                # Reported rather than inferred so an operator can see which of
                # their grants a same-path replacement would still inherit.
                "bound": bool(isinstance(identity, str) and identity),
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
