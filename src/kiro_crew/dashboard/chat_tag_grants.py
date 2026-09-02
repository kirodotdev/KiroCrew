"""Agent tag-write grants — the protected policy source for ``chat_tag``.

A tag's agent-write policy (``add-remove`` | ``add-only`` | ``none``) decides
whether the ``chat_tag`` session directive may mutate that tag on a session.
Storing that policy as fields on the tag rows in ``tags.json`` left it
agent-writable: ``tags.json`` is an ordinary data-home file, so an agent's own
file tools could forge ``agent``/``status`` fields, restart-persistently
granting itself write access to a human-reserved tag (forgeable-authorization hazard on the
``chat_tag`` PR). Same class of control as ``computer_use.json``: the record IS
the authorization, so it cannot live where the subject of the authorization can
write it.

This module is the protected replacement. Grants live in
``<data home>/tag-grants/agent-tag-policy.json`` — a dedicated leaf that is
masked from sandboxed processes (``sandbox._CREW_HIDDEN_LEAVES``) AND fenced
from the agent file tools and shell (``security._CREW_SECRET_LEAVES``), so
neither a gated tool call nor a spawned script's plain ``open()`` can read or
forge it; only the gateway opens the path, directly.

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

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard import token_secret

logger = logging.getLogger(__name__)

#: A dedicated top-level crew-home leaf, masked HIDDEN by every sandbox mode
#: (``sandbox._CREW_HIDDEN_LEAVES`` + pre-created via
#: ``_CREW_PRECREATE_HIDDEN_DIR_LEAVES``) and fenced from agent file tools by
#: ``security._CREW_SECRET_LEAVES``. NOT ``trust/``: that directory must stay
#: sandbox read-write (``_CREW_SANDBOX_VISIBLE_LEAVES`` — the SEL log appends
#: and ``sel_hmac.key`` reads happen in-sandbox), so an authorization store
#: living there is forgeable by a spawned agent script's plain ``open()``,
#: which never routes through the file-tool gate. Only the GATEWAY process
#: reads or writes grants, so the mask breaks no consumer. Top-level because a
#: mask covers the leaf, not its ancestors.
_STORE_SUBDIR = "tag-grants"

#: There is deliberately NO read of any other location — in particular no
#: migration from ``trust/``. That directory is sandbox read-write, so a file
#: there can only be agent-planted (no shipped build writes grants anywhere
#: but this store), and promoting its bytes into the protected store would
#: hand an in-sandbox forgery real authorization on the next boot. Grants are
#: minted exclusively through the authenticated dashboard CRUD and the
#: trusted-constant seeding below.

_STORE_FILENAME = "agent-tag-policy.json"

#: A store written by a newer build is treated as unreadable (fail closed)
#: rather than guessed at.
_SCHEMA_VERSION = 1

#: Bound the cost of a pathological store. A document with more rows is
#: rejected WHOLE (the reader fails closed; never truncated), and mint
#: refuses new rows at the cap — see _parse_rows and mint_grant.
_MAX_GRANT_ROWS = 4096

#: The policies a store row may carry. ``none`` rows exist to preserve the
#: STATUS bit for human-only workflow-state tags (revoking the row
#: on an ``agent: "none"`` PATCH would erase status identity and let
#: ``set_state`` persist two exclusive workflow states); absence of a row still
#: resolves to ``("none", False)``, the maximally-closed state.
_ROW_POLICIES = frozenset({"add-remove", "add-only", "none"})

#: The ONLY tag ids that may ever carry a grant. Every id the dashboard mints
#: is ``uuid4().hex[:12]`` (chat_tags.create_tag_definition); the built-in
#: workflow states are the fixed code-level set below. Nothing else can be a
#: legitimately granted tag, so a grant on any other id is a planted one:
#: ``tags.json`` is agent-writable and an agent that hand-writes a tag whose
#: NAME reads benign in the dashboard but whose ID spells an instruction would,
#: once a human toggled that tag's status (a routine PATCH that mints a grant),
#: ride the trusted ``[BOARD]`` rail with an id the human never saw. Pinning
#: the grammar at the mint closes that class structurally -- the injection
#: heuristic on the rail becomes redundant instead of the last defense.
#: Extending the defaults is a code change here, by design.
DEFAULT_TAG_IDS = frozenset({"planned", "todo", "implementation", "review", "done"})
_DASHBOARD_TAG_ID_RE = re.compile(r"[0-9a-f]{12}")


def is_grantable_tag_id(tag_id: object) -> bool:
    """True only for an id the dashboard could have minted or a built-in default.

    The grammar is closed on purpose: a slug that is not one of these is an id
    the dashboard never issued, however plausible it reads.
    """
    return isinstance(tag_id, str) and (
        tag_id in DEFAULT_TAG_IDS or _DASHBOARD_TAG_ID_RE.fullmatch(tag_id) is not None
    )


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
    """The grants directory, verified real and restricted to the owner.

    Mirrors ``skill_trust._trust_dir``: ``is_link_or_junction`` (a Windows
    directory junction is not a symlink, so ``is_symlink`` would walk through
    a planted one), then ``make_owner_only_dir`` + ``restrict_dir_to_owner``
    rather than ``mkdir(mode=0o700)`` — POSIX mode bits are a NO-OP on
    Windows, and a permissive data-home DACL would leave THE authorization
    store for ``chat_tag`` forgeable by another local account (Opus review
    finding).
    """
    directory = config_dir() / _STORE_SUBDIR
    if platform_compat.is_link_or_junction(directory):
        logger.error("%s is a link; removing it before writing grant state", directory)
        platform_compat.unlink_link_or_junction(directory)
    platform_compat.make_owner_only_dir(directory)
    platform_compat.restrict_dir_to_owner(directory)
    return directory


def _store_path() -> Path:
    return config_dir() / _STORE_SUBDIR / _STORE_FILENAME


#: Domain separator for the store's provenance MAC, so a signature minted here
#: can never validate as a dashboard auth token or vice versa.
_PROVENANCE_DOMAIN = b"kiro-crew:chat-tag-grants:v1\x00"

#: Domain separator for the store key's CERTIFICATE, distinct from the row
#: MAC's domain so neither value can stand in for the other.
_KEY_CERT_DOMAIN = b"kiro-crew:chat-tag-grants:key-cert:v1\x00"

_STORE_KEY_FILENAME = ".signing-key"


def _store_key_path() -> Path:
    return config_dir() / _STORE_SUBDIR / _STORE_KEY_FILENAME


def _key_cert(key: bytes) -> str:
    """Certify the store key under ``token_signing.key``.

    The certificate is what ties the store's OWN key to a secret that is
    unreadable and unwritable from every agent plane in the builds that ship
    it — a pre-upgrade plant can write both the store and a key file, but it
    cannot mint this value. A token-key rotation therefore breaks the
    certificate and the boot pass quarantines the store: at that moment a
    legitimate store and a planted self-signed one are indistinguishable, so
    the only safe answer is the fail-closed one. Grants are restored by
    authenticated re-minting, the same recovery class as every other effect
    of a token-key reset.
    """
    return hmac.new(token_secret._get_secret(), _KEY_CERT_DOMAIN + key, hashlib.sha256).hexdigest()


def _load_store_key() -> tuple[bytes | None, bool]:
    """Return ``(key, certified)``; ``(None, False)`` when absent/malformed."""
    try:
        raw = json.loads(_store_key_path().read_text(encoding="utf-8"))
        key = bytes.fromhex(raw["key"])
        cert = raw["cert"]
    except Exception:
        return (None, False)
    if len(key) < 32 or not isinstance(cert, str):
        return (None, False)
    try:
        certified = hmac.compare_digest(cert, _key_cert(key))
    except Exception:
        certified = False
    return (key, certified)


def _write_store_key(key: bytes) -> None:
    _store_dir()
    payload = json.dumps({"key": key.hex(), "cert": _key_cert(key)}) + "\n"
    atomic_write(_store_key_path(), payload, restrict_to_owner=True)


def _ensure_store_key() -> bytes:
    """The certified store key, minting one when absent (writer path).

    An EXISTING but uncertified key is refused rather than replaced: only the
    boot pass (:func:`seed_default_grants`) may decide between re-certifying
    it (gateway-observed token-key regeneration) and quarantining it (plant),
    and a writer clobbering it would destroy the evidence that decision
    needs.
    """
    key, certified = _load_store_key()
    if key is not None and certified:
        return key
    if key is not None:
        raise GrantStoreUnreadable("store key present but not certified")
    fresh = secrets.token_bytes(32)
    _write_store_key(fresh)
    return fresh


def _rows_mac(key: bytes, grants: dict[str, Any]) -> str:
    payload = _PROVENANCE_DOMAIN + json.dumps(grants, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _provenance_mac(grants: dict[str, Any]) -> str:
    """Row MAC under the (minted-on-demand) certified store key."""
    return _rows_mac(_ensure_store_key(), grants)


def _rows_verify(key: bytes, raw: dict[str, Any]) -> bool:
    mac = raw.get("provenance")
    grants = raw.get("grants")
    if not isinstance(mac, str) or not isinstance(grants, dict):
        return False
    return hmac.compare_digest(mac, _rows_mac(key, grants))


def _provenance_valid(raw: dict[str, Any]) -> bool:
    """Full chain: certified store key AND rows verifying under it."""
    key, certified = _load_store_key()
    if key is None or not certified:
        return False
    return _rows_verify(key, raw)


def _quarantine_store(path: Path, reason: str) -> None:
    """Move an unverifiable store aside; never delete evidence.

    The rename keeps the bytes for the operator to inspect while making the
    load path start from the fail-closed empty state. Refused outright when
    the parent directory is a link or junction — a planted link would carry
    the rename to an arbitrary target — and the boot pass removes such a link
    via :func:`_store_dir` before calling here. Rename failures fall through:
    the reader already refuses the document, so the store stays inert either
    way.
    """
    if platform_compat.is_link_or_junction(path.parent):
        logger.error("agent-tag-policy store %s: %s; parent is a link, not renaming", path, reason)
        return
    target = path.with_name(f"{path.name}.quarantined-{int(time.time())}")
    try:
        path.rename(target)
        logger.error("agent-tag-policy store %s: %s; quarantined to %s", path, reason, target)
    except OSError:
        logger.error("agent-tag-policy store %s: %s; quarantine rename failed", path, reason)


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
        # that produced it reported success. The reader
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
        # identity. Anything non-boolean fails closed.
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
        # grants until the next write — clear it so
        # every resolve fails closed to ("none", False).
        with _cache_lock:
            _cache = None
        return {}
    with _cache_lock:
        if _cache is not None and _cache[0] == sig:
            return _cache[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not _provenance_valid(raw):
            raise GrantStoreUnreadable("provenance MAC missing or invalid")
        rows = _parse_rows(raw)
    except Exception:
        logger.warning("agent-tag-policy store unreadable; resolving zero grants")
        # Cache the fail-closed empty result FOR THIS SIGNATURE: without it,
        # every resolver call re-reads and re-parses the malformed file — and
        # the off-thread ``refresh_cache`` pre-warm caches nothing, so those
        # rereads land synchronously on the gateway event loop (a
        # finding). A rewrite of the store changes the signature and re-reads.
        rows = {}
    with _cache_lock:
        # Install ONLY if the store still carries the signature this read was
        # taken at: a concurrent authenticated write may have installed a
        # NEWER snapshot while we were parsing, and letting this stale read
        # overwrite it would restore revoked authorization until the next
        # refresh. The writer's install also holds this
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
    into a synchronous read+parse on the gateway event loop (a
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


def has_grant_row(tag_id: str) -> bool:
    """True when the cached snapshot carries a protected row for ``tag_id``.

    Distinct from :func:`resolve_grant`, whose ``("none", False)`` default is
    deliberately indistinguishable from a minted none-row: policy resolution
    must fail closed either way. Existence matters separately at the PATCH
    seam — a tag WITH a protected row can inherit its recorded status bit,
    while a tag WITHOUT one has no protected record to inherit from and the
    caller must state the bit explicitly. Same snapshot discipline as the
    resolver: cache-only, never touches the filesystem, fail-closed (no
    snapshot reads as no row).
    """
    if not tag_id:
        return False
    snapshot = _cache
    if snapshot is None:
        return False
    return tag_id in snapshot[1]


def _read_for_write(path: Path) -> dict[str, Any]:
    """Load the raw document for read-modify-write; refuse when untrustworthy."""
    if not path.exists():
        return {"version": _SCHEMA_VERSION, "grants": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GrantStoreUnreadable(str(exc)) from exc
    if not _provenance_valid(raw):
        raise GrantStoreUnreadable("provenance MAC missing or invalid")
    _parse_rows(raw, allow_oversized=True)  # schema check; raises GrantStoreUnreadable
    return raw


def _write_document(path: Path, document: dict[str, Any]) -> None:
    # Directory via the owner-only helper (never a bare mkdir), and the write
    # with ``restrict_to_owner=True`` — it implies 0o600 on POSIX and applies
    # a real owner-only ACL on Windows, where a ``mode=`` argument is a no-op
    # and would leave this authorization store forgeable by another local
    # account (Opus review finding; mirrors skill_trust's write).
    _store_dir()
    grants = document.get("grants")
    if isinstance(grants, dict):
        document = dict(document)
        document["provenance"] = _provenance_mac(grants)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    atomic_write(path, payload, restrict_to_owner=True)
    # Install the fresh snapshot directly: the resolver is cache-only (it
    # never reloads), so a bare invalidation here would
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
        # parser refuses (never silently drops) would report
        # success for a grant that never takes effect.
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
    legacy fields; that would amount to promoting
    agent-controlled data into authorization (edit the file before the
    upgrade, get a protected grant after it), so file-derived seeding is gone:
    a pre-existing custom grant now requires one authenticated dashboard PATCH
    to re-mint, which is the migration cost of not laundering the file's
    contents into the trust store. Returns True when a store was written;
    never overwrites a store that verifies.

    This is also the store's RECOVERY AUTHORITY, run before any resolver
    traffic. Decisions, in order:

    - The store directory is realized via :func:`_store_dir` FIRST, which
      removes a link or junction planted at the directory name — every read
      and every quarantine rename below happens inside a real directory, so
      a planted link can never carry a rename to an arbitrary target.
    - An UNCERTIFIED store key is quarantined together with the store, and
      trusted constants are reseeded. This covers a token-key rotation too:
      at that moment a legitimate store and a planted self-signed one are
      indistinguishable, so the fail-closed answer is the only safe one, and
      grants are restored by authenticated re-minting.
    - A store whose rows fail verification under the certified key is
      quarantined and reseeded from trusted constants: forged rows stop
      resolving on the very boot that finds them, not when a human notices.
    """
    _store_dir()  # link guard BEFORE any path read or rename
    path = _store_path()
    key, certified = _load_store_key()
    if key is not None and not certified:
        # An uncertified key is quarantined UNCONDITIONALLY. A token-key
        # regeneration also lands here and costs the stored grants — that is
        # deliberate: at that moment a legitimate store and an agent-planted
        # self-signed one are cryptographically indistinguishable (the old
        # token key that could tell them apart is gone), so certifying
        # either certifies both, and a forged authorization blessed once
        # persists forever. The loss fails CLOSED (every tag resolves to
        # ``("none", False)``), default status ids re-seed below on this
        # same boot, and a custom grant is restored by one authenticated
        # dashboard PATCH — the same recovery class as every other effect of
        # a token-key reset, which already invalidates all sessions.
        _quarantine_store(_store_key_path(), "store key not certified at boot")
        _quarantine_store(path, "store present with an uncertified key")
        key = None
    if path.exists():
        if key is not None and certified:
            try:
                raw_doc = json.loads(path.read_text(encoding="utf-8"))
                verified = _rows_verify(key, raw_doc)
            except Exception:
                verified = False
            if verified:
                return False
            _quarantine_store(path, "provenance MAC missing or invalid at boot")
        else:
            _quarantine_store(path, "store present without a certified key")
    if path.exists():
        # Quarantine rename failed; the readers refuse the document, so
        # seeding over it is not required for safety — skip rather than
        # clobber bytes the operator may want to inspect.
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


def seed_status_identity_rows(default_status_tag_ids: list[str]) -> bool:
    """Ensure every DEFAULT workflow-state id carries a status-identity row.

    Upgraded installs predate the store, and without a row the applier reads
    a default status tag as non-status: ``set_state`` then skips exclusive-
    peer stripping and two mutually exclusive workflow states persist. Rows
    are minted as ``{"policy": "none", "status": True}``: the identity bit
    CONSTRAINS (peer exclusivity) and grants no agent write authority, so a
    tag id restored into agent-writable tags.json inherits nothing, which is
    what keeps this safe where whole-grant upgrade seeding is not. Ids come
    from TRUSTED CODE CONSTANTS only; existing rows are never touched.
    Returns True when the document was modified.
    """
    ids = [t for t in default_status_tag_ids if isinstance(t, str) and t]
    if not ids:
        return False
    path = _store_path()
    try:
        document = _read_for_write(path)
    except GrantStoreUnreadable:
        # An unreadable store fails closed everywhere else; seeding must not
        # be the write that papers over it.
        logger.warning("status-identity seed skipped: store unreadable", exc_info=True)
        return False
    grants = document.setdefault("grants", {})
    missing = [t for t in ids if t not in grants]
    if not missing or len(grants) + len(missing) > _MAX_GRANT_ROWS:
        return False
    for tag_id in missing:
        grants[tag_id] = {"policy": "none", "status": True}
    try:
        _write_document(path, document)
    except Exception:
        logger.warning("status-identity seed failed; store unchanged", exc_info=True)
        return False
    return True
