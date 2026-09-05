"""Agent tag-write grants — the protected policy source for ``chat_tag``.

A tag's agent-write policy (``add-remove`` | ``add-only`` | ``none``) decides
whether the ``chat_tag`` session directive may mutate that tag on a session.
Storing that policy as fields on the tag rows in ``tags.json`` left it
agent-writable: ``tags.json`` is an ordinary data-home file, so an agent's own
file tools could forge ``agent``/``status`` fields, restart-persistently
granting itself write access to a human-reserved tag (GPT review finding on the
``chat_tag`` PR). Same class of control as ``computer_use.json``: the record IS
the authorization, so it cannot live where the subject of the authorization can
write it.

This module is the protected replacement. Grants live in
``<data home>/trust/agent-tag-policy.json`` — the ``trust/`` directory is
already a whole-directory entry on the keystone deny list (shared with the SEL
project key and the skill-trust store), so the agent's file tools and shell can
neither read nor write it; like every other keystone reader, this module opens
the path directly rather than through the agent file gate.

Writers are the authenticated dashboard tag CRUD handlers only (create/update/
delete mint and revoke rows), plus a one-time boot seed that mints rows solely
for the CODE-CONSTANT default workflow-state tag ids — never anything read
from ``tags.json``, whose contents are agent-writable and therefore must not
be promoted into this store. A pre-existing custom grant requires one
authenticated dashboard PATCH after upgrade to re-mint.

Each grant row also records the tag's STATUS bit (is this a workflow-state
tag?). The applier's status semantics — set_state eligibility, the
mutual-exclusivity peer strip, the "no status tags through add" rule — key on
this recorded bit rather than the file's, because a forged ``status`` field
on a granted tag would otherwise re-route those authorization decisions.

The gate fails **closed** everywhere: an unreadable store, a malformed store,
an unknown tag id, or a newer schema all resolve to ``("none", False)`` rather
than a permissive default. Refusing a legitimate grant costs one dashboard
click to re-mint; honoring a forged one hands the agent a human-reserved tag.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: Shared with the SEL project key and the skill-trust store so the existing
#: whole-directory keystone entry covers this store with no security changes.
_TRUST_SUBDIR = "trust"

_STORE_FILENAME = "agent-tag-policy.json"

#: A store written by a newer build is treated as unreadable (fail closed)
#: rather than guessed at.
_SCHEMA_VERSION = 1

#: Bound the cost of a pathological store. A document with more rows is
#: rejected WHOLE (the reader fails closed; never truncated), and mint
#: refuses new rows at the cap — see _parse_rows and mint_grant.
_MAX_GRANT_ROWS = 4096

#: The policies a store row may carry. ``none`` rows exist to preserve the
#: STATUS bit for human-only workflow-state tags (GPT review: revoking the row
#: on an ``agent: "none"`` PATCH would erase status identity and let
#: ``set_state`` persist two exclusive workflow states); absence of a row still
#: resolves to ``("none", False)``, the maximally-closed state.
_ROW_POLICIES = frozenset({"add-remove", "add-only", "none"})

#: Cached ``(stat_signature, {tag_id: (policy, status)})`` — the resolver runs
#: on the event loop inside the chat_tag applier and the per-turn context
#: injection, so re-parsing per call would be a read per tag; a stat signature
#: is one syscall total.
_StoreSignature = tuple[int, int, int, int, int]
_cache: tuple[_StoreSignature, dict[str, tuple[str, bool]]] | None = None
# Serializes snapshot installs (refresh vs authenticated write): see
# _load_rows' install-time signature re-verification.
_cache_lock = threading.Lock()


class GrantStoreUnreadable(RuntimeError):
    """The store exists but cannot be trusted to round-trip.

    Raised only to the WRITE paths, so a mint or revoke refuses instead of
    replacing rows it could not read. Never raised to the resolver, which
    fails closed by granting nothing.
    """


def _store_dir() -> Path:
    """The trust directory, verified real and restricted to the owner.

    Mirrors ``skill_trust._trust_dir``: ``is_link_or_junction`` (a Windows
    directory junction is not a symlink, so ``is_symlink`` would walk through
    a planted one), then ``make_owner_only_dir`` + ``restrict_dir_to_owner``
    rather than ``mkdir(mode=0o700)`` — POSIX mode bits are a NO-OP on
    Windows, and a permissive data-home DACL would leave THE authorization
    store for ``chat_tag`` forgeable by another local account (Opus review
    finding).
    """
    directory = config_dir() / _TRUST_SUBDIR
    if platform_compat.is_link_or_junction(directory):
        logger.error("%s is a link; removing it before writing grant state", directory)
        platform_compat.unlink_link_or_junction(directory)
    platform_compat.make_owner_only_dir(directory)
    platform_compat.restrict_dir_to_owner(directory)
    return directory


def _store_path() -> Path:
    return config_dir() / _TRUST_SUBDIR / _STORE_FILENAME


def _stat_signature(path: Path) -> _StoreSignature | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _parse_rows(raw: Any, *, allow_oversized: bool = False) -> dict[str, tuple[str, bool]]:
    """Parse a loaded store document into ``{tag_id: (policy, status)}``.

    Every malformed row is dropped individually (fail closed per row), so one
    bad entry cannot take the legitimate grants beside it down with it.
    """
    if not isinstance(raw, dict) or raw.get("version") != _SCHEMA_VERSION:
        raise GrantStoreUnreadable("unrecognized schema")
    grants = raw.get("grants")
    if not isinstance(grants, dict):
        raise GrantStoreUnreadable("grants is not an object")
    if len(grants) > _MAX_GRANT_ROWS and not allow_oversized:
        # REJECT rather than truncate: silently dropping rows past the cap
        # would corrupt the effective authorization state while every write
        # that produced it reported success (GPT review finding). The reader
        # path converts this into fail-closed zero grants. Writers pass
        # ``allow_oversized`` so an oversized (hand-grown/corrupt) store can
        # still be REPAIRED through revoke — mint separately refuses to add
        # new rows at or past the cap.
        raise GrantStoreUnreadable(f"more than {_MAX_GRANT_ROWS} grant rows")
    rows: dict[str, tuple[str, bool]] = {}
    for tag_id, row in grants.items():
        if not isinstance(tag_id, str) or not tag_id:
            continue
        if not isinstance(row, dict):
            continue
        policy = row.get("policy")
        if not isinstance(policy, str) or policy not in _ROW_POLICIES:
            continue
        # The status bit must be an ACTUAL boolean: a JSON string "false" is
        # truthy, so coercing would let a malformed row mint workflow-state
        # identity (GPT review finding). Anything non-boolean fails closed.
        status_raw = row.get("status", False)
        rows[tag_id] = (policy, status_raw is True)
    return rows


def _load_rows() -> dict[str, tuple[str, bool]]:
    """Read the store for the resolver: any failure yields ZERO grants."""
    global _cache
    path = _store_path()
    sig = _stat_signature(path)
    if sig is None:
        # The store is GONE (deleted/renamed). The resolver is cache-only, so
        # leaving the old snapshot installed would keep authorizing revoked
        # grants until the next write (GPT review finding) — clear it so
        # every resolve fails closed to ("none", False).
        with _cache_lock:
            _cache = None
        return {}
    with _cache_lock:
        if _cache is not None and _cache[0] == sig:
            return _cache[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = _parse_rows(raw)
    except Exception:
        logger.warning("agent-tag-policy store unreadable; resolving zero grants")
        # Cache the fail-closed empty result FOR THIS SIGNATURE: without it,
        # every resolver call re-reads and re-parses the malformed file — and
        # the off-thread ``refresh_cache`` pre-warm caches nothing, so those
        # rereads land synchronously on the gateway event loop (GPT review
        # finding). A rewrite of the store changes the signature and re-reads.
        rows = {}
    with _cache_lock:
        # Install ONLY if the store still carries the signature this read was
        # taken at: a concurrent authenticated write may have installed a
        # NEWER snapshot while we were parsing, and letting this stale read
        # overwrite it would restore revoked authorization until the next
        # refresh (GPT review finding). The writer's install also holds this
        # lock, so the orderings interleave safely: a writer that lands after
        # our re-stat blocks until our install completes and then installs
        # the fresh snapshot last.
        if _stat_signature(path) == sig:
            _cache = (sig, rows)
    return rows


def refresh_cache() -> None:
    """Read and parse the store off the caller's thread of choice.

    The async call sites (the ``chat_tag`` applier, the per-turn board
    context injection) run this via ``asyncio.to_thread`` BEFORE resolving,
    so the full read+parse never happens on the gateway event loop; the
    subsequent sync :func:`resolve_grant` calls then serve the installed
    snapshot and are entirely filesystem-free (the resolver never stats or
    reads — see its docstring).
    """
    _load_rows()


def resolve_grant(tag_id: str) -> tuple[str, bool]:
    """Resolve ``(policy, status)`` for a tag id from the cached snapshot.

    ``("none", False)`` for an unknown id, an empty id, or when no snapshot
    has been loaded. This function NEVER touches the filesystem: a signature
    miss between an off-thread ``refresh_cache`` and this call must not turn
    into a synchronous read+parse on the gateway event loop (GPT review
    finding) — the resolver serves the immutable snapshot the last refresh
    (or write) installed, and staleness is bounded by the callers'
    refresh-before-resolve discipline. Fail-closed on a missing snapshot.
    """
    if not tag_id:
        return ("none", False)
    snapshot = _cache
    if snapshot is None:
        return ("none", False)
    return snapshot[1].get(tag_id, ("none", False))


def _read_for_write(path: Path) -> dict[str, Any]:
    """Load the raw document for read-modify-write; refuse when untrustworthy."""
    if not path.exists():
        return {"version": _SCHEMA_VERSION, "grants": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GrantStoreUnreadable(str(exc)) from exc
    _parse_rows(raw, allow_oversized=True)  # schema check; raises GrantStoreUnreadable
    return raw


def _write_document(path: Path, document: dict[str, Any]) -> None:
    # Directory via the owner-only helper (never a bare mkdir), and the write
    # with ``restrict_to_owner=True`` — it implies 0o600 on POSIX and applies
    # a real owner-only ACL on Windows, where a ``mode=`` argument is a no-op
    # and would leave this authorization store forgeable by another local
    # account (Opus review finding; mirrors skill_trust's write).
    _store_dir()
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    atomic_write(path, payload, restrict_to_owner=True)
    # Install the fresh snapshot directly: the resolver is cache-only (it
    # never reloads — GPT review finding), so a bare invalidation here would
    # leave every grant resolving ("none", False) until the next off-thread
    # refresh. Writers already run off the event loop via ``asyncio.to_thread``.
    global _cache
    sig = _stat_signature(path)
    if sig is not None:
        with _cache_lock:
            _cache = (sig, _parse_rows(document, allow_oversized=True))


def mint_grant(tag_id: str, *, policy: str, status: bool) -> None:
    """Record (or update) a grant row. Caller is an authenticated dashboard write.

    ``policy`` may be ``"none"``: such a row grants no write authority but
    preserves the tag's recorded STATUS bit, which the applier's workflow
    semantics key on. Use :func:`revoke_grant` only for tag deletion or
    status removal, so a human-only workflow state never loses its identity.
    """
    if policy not in _ROW_POLICIES:
        raise ValueError(f"not a recordable policy: {policy!r}")
    if not tag_id:
        raise ValueError("empty tag id")
    path = _store_path()
    document = _read_for_write(path)
    grants = document.setdefault("grants", {})
    if tag_id not in grants and len(grants) >= _MAX_GRANT_ROWS:
        # Enforce the cap at WRITE time, before persisting: adding a row the
        # parser would refuse (or, previously, silently drop) would report
        # success for a grant that never takes effect (GPT review finding).
        # Updating an EXISTING row is always allowed. The CRUD callers turn
        # this into a 500 and roll their vocabulary write back.
        raise GrantStoreUnreadable(f"grant store is at its {_MAX_GRANT_ROWS}-row cap")
    grants[tag_id] = {"policy": policy, "status": bool(status)}
    _write_document(path, document)


def revoke_grant(tag_id: str) -> None:
    """Remove a grant row; a missing row is already the revoked state."""
    path = _store_path()
    if not path.exists():
        return
    document = _read_for_write(path)
    grants = document.get("grants", {})
    if tag_id in grants:
        del grants[tag_id]
        _write_document(path, document)


def seed_default_grants(default_status_tag_ids: list[str]) -> bool:
    """One-time seed of the store from TRUSTED CODE CONSTANTS only.

    Runs at boot when the store file does not exist. Rows are minted solely
    for the ids passed in — the caller supplies the DEFAULT workflow-state tag
    ids from the code-level seed vocabulary, never anything read from
    ``tags.json``. An earlier revision derived rows from the live vocabulary's
    legacy fields; GPT review correctly flagged that as promoting
    agent-controlled data into authorization (edit the file before the
    upgrade, get a protected grant after it), so file-derived seeding is gone:
    a pre-existing custom grant now requires one authenticated dashboard PATCH
    to re-mint, which is the migration cost of not laundering the file's
    contents into the trust store. Returns True when a store was written;
    never overwrites an existing store.
    """
    path = _store_path()
    if path.exists():
        return False
    grants: dict[str, Any] = {}
    for tag_id in default_status_tag_ids:
        if isinstance(tag_id, str) and tag_id:
            grants[tag_id] = {"policy": "add-remove", "status": True}
    try:
        _write_document(path, {"version": _SCHEMA_VERSION, "grants": grants})
    except Exception:
        # A failed seed leaves no store: every tag resolves to "none" until a
        # dashboard write mints a row — closed, never open.
        logger.warning("agent-tag-policy seed failed; store not written", exc_info=True)
        return False
    return True
