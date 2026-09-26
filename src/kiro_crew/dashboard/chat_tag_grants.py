"""Agent tag-write grants — protected identity and policy for ``chat_tag``.

A tag's agent-write policy (``add-remove`` | ``add-only`` | ``none``) decides
whether the ``chat_tag`` session directive may mutate that tag on a session.
``tags.json`` is agent-writable, so neither policy nor trusted tag identity can
come from its rows. The authorization record lives instead in
``<data home>/tag-grants/agent-tag-policy.json``, a dedicated leaf masked from
sandboxed processes and fenced from agent file tools and shell. Only the
gateway opens it directly.

Authenticated dashboard create records an identity row for every new tag:
``none``/non-status for a plain label and ``add-remove``/status for a workflow
state. PATCH may change policy or status only when that row already exists; the
dashboard-owner adoption action records a ``none`` row for a legacy tag, and
delete revokes it. Boot seeding reads only code-constant workflow-state ids,
never ``tags.json``. A pre-existing custom tag without a row remains human-only
until explicit dashboard-owner adoption.

Each row records the protected STATUS bit used by set_state eligibility,
mutual-exclusivity stripping, and the no-status-tag-through-add rule. An absent,
unreadable, malformed, or newer-schema store fails closed to
``("none", False)``. A legitimate custom tag whose protected identity is lost
requires explicit owner adoption; honoring a forged row hands the agent a
human-reserved tag.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.board_tag_grammar import DEFAULT_TAG_IDS, is_grantable_tag_id
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

#: Upper bound on the store-key file (``{"key": <64 hex>, "cert": <64 hex>}`` is
#: ~160 bytes); anything larger is not a key this module wrote.
_MAX_STORE_KEY_BYTES = 4096

#: Upper bound on the grants document. ``_MAX_GRANT_ROWS`` rows of the widest
#: legal shape fit in well under this; anything larger is not a store this
#: module wrote and is read as malformed (quarantine) rather than parsed.
_MAX_STORE_BYTES = 2 * 1024 * 1024

#: The policies a store row may carry. ``none`` rows record protected
#: dashboard-create identity without granting write authority; for workflow
#: states they also preserve the STATUS bit used by exclusivity semantics.
#: Absence of a row still resolves to ``("none", False)``, the maximally-closed
#: state, but is not provenance for a later PATCH mint.
_ROW_POLICIES = frozenset({"add-remove", "add-only", "none"})

#: The closed grant grammar lives in the dependency-free
#: :mod:`kiro_crew.board_tag_grammar` (``context.py`` screens the [BOARD] rail
#: with it and must stay dashboard-free); re-exported here for the mint and
#: its callers.
__all__ = ["DEFAULT_TAG_IDS", "is_grantable_tag_id"]


#: Cached ``(stat_signature, {tag_id: (policy, status)})`` — the resolver runs
#: on the event loop inside the chat_tag applier and the per-turn context
#: injection, so re-parsing per call would be a read per tag; a stat signature
#: is one syscall total.
_StoreSignature = tuple[int, int, int, int, int]
_cache: tuple[_StoreSignature, dict[str, tuple[str, bool]]] | None = None

#: Why the store is currently serving ZERO or REDUCED grants for a reason
#: other than a human's decision: unreadable document, store gone, or a boot
#: quarantine that discarded rows this process cannot recover. ``None`` while
#: the installed snapshot came from a healthy read. The ``chat_tag`` applier
#: reads this so an agent's refusal says the store is unavailable instead of
#: ``tag_policy_denied`` -- the latter reads as a deliberate human reservation
#: and sends the operator debugging a "broken" feature from gateway logs. A
#: quarantine REDUCES grants for the life of the process: the re-seeded store
#: is healthy but every custom identity it held is gone until the tag is
#: explicitly adopted, so a rowless refusal keeps naming the store.
_degraded: str | None = None
_quarantined_this_boot = False
#: True once the reseed that followed a boot quarantine was written, read
#: back from disk, and verified under the certified store key. Cleared by
#: every quarantine. While a quarantine stands unrepaired the write gate stays
#: closed: the quarantined rows are never trusted, and there is no verified
#: store for an authenticated write to land in.
_quarantine_repaired = False


def store_degraded() -> str | None:
    """Why grants are unavailable or reduced, or ``None`` when the store is healthy."""
    with _cache_lock:
        if _quarantined_this_boot:
            return "quarantined"
        return _degraded


def store_write_blocked() -> str | None:
    """Why an authenticated write must refuse, or ``None`` when it may land.

    Distinct from :func:`store_degraded`, which keeps naming a boot quarantine
    for the whole process because the grants it REDUCED stay reduced. A write
    asks a narrower question: does the CURRENT installed store verify? A
    missing or unreadable snapshot fails closed, and so does a quarantine
    whose reseed was never read back and verified. Once
    :func:`seed_default_grants` has proven the replacement store on disk, the
    quarantine is exactly what explicit dashboard-owner adoption repairs, and
    refusing that adoption until a restart would leave the one recovery path
    the quarantine points at closed.
    """
    with _cache_lock:
        if _degraded is not None:
            return _degraded
        if _quarantined_this_boot and not _quarantine_repaired:
            return "quarantined"
        return None


def _set_degraded(reason: str | None) -> None:
    global _degraded
    with _cache_lock:
        _degraded = reason


# Serializes snapshot installs (refresh vs authenticated write): see
# _load_rows' install-time signature re-verification.
_cache_lock = threading.Lock()


class GrantStoreTransientReadError(OSError):
    """A store file exists but could not be READ this instant (EIO, EACCES, a
    race with a writer). Distinct from an unreadable DOCUMENT: nothing was
    read, so nothing can be judged forged, and the boot pass must not
    quarantine on it -- it resolves zero grants for this boot and leaves the
    files exactly as they are."""


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
    the only safe answer is the fail-closed one. Custom tag authority is
    restored by explicit dashboard-owner adoption.
    """
    return hmac.new(token_secret._get_secret(), _KEY_CERT_DOMAIN + key, hashlib.sha256).hexdigest()


def _read_bounded_json(path: Path, limit: int) -> Any:
    """Parse ``path`` as JSON without an unbounded read.

    Both files this module owns live in a leaf that is gateway-only once the
    store exists, but the directory is unmasked until the boot that creates
    it, so a file planted there beforehand must cost the boot at most
    ``limit`` bytes. A non-regular file or one over the limit raises like a
    malformed document does, which the callers already treat as
    quarantine-and-reseed.
    """
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
        raise ValueError(f"{path.name}: not a regular file under {limit} bytes")
    with path.open("rb") as fh:
        return json.loads(fh.read(limit).decode("utf-8"))


def _load_store_key() -> tuple[bytes | None, bool]:
    """Return ``(key, certified)``; ``(None, False)`` when absent/malformed.

    Bounded read (see ``_read_bounded_json``): an oversized or non-regular
    key file is malformed, which quarantines and re-seeds exactly like a bad
    certificate does. A file that exists but cannot be read right now raises
    :class:`GrantStoreTransientReadError` instead -- see the boot pass.
    """
    path = _store_key_path()
    if not path.exists():
        return (None, False)
    try:
        raw = _read_bounded_json(path, _MAX_STORE_KEY_BYTES)
    except OSError as exc:
        # The file is THERE but could not be read this instant (EIO, EACCES,
        # a race with a writer). That is not evidence of a forgery, so it
        # must not become a quarantine: raise, and let the boot pass leave
        # the store untouched for this boot.
        raise GrantStoreTransientReadError(str(exc)) from exc
    except Exception:
        return (None, False)  # read fine, not a key this module wrote: malformed
    try:
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
    global _quarantined_this_boot, _quarantine_repaired
    with _cache_lock:
        _quarantined_this_boot = True
        # A fresh quarantine reopens the question of whether a verified store
        # exists: only ``seed_default_grants``' read-back of its reseed can
        # answer it again.
        _quarantine_repaired = False
    stamp = int(time.time())
    target = path.with_name(f"{path.name}.quarantined-{stamp}")
    # Never overwrite an earlier quarantine: the renamed bytes are the evidence
    # an operator inspects, and a same-second collision would replace them.
    suffix = 1
    while target.exists():
        target = path.with_name(f"{path.name}.quarantined-{stamp}-{suffix}")
        suffix += 1
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
        _set_degraded("missing")
        return {}
    with _cache_lock:
        if _cache is not None and _cache[0] == sig:
            return _cache[1]
    healthy = False
    try:
        raw = _read_bounded_json(path, _MAX_STORE_BYTES)
        if not _provenance_valid(raw):
            raise GrantStoreUnreadable("provenance MAC missing or invalid")
        rows = _parse_rows(raw)
        healthy = True
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
    _set_degraded(None if healthy else "unreadable")
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


def resolve_grant_record(tag_id: str) -> tuple[str, bool, bool]:
    """Resolve ``(policy, status, row_exists)`` from one cached snapshot.

    Policy and provenance are projected together so a writer installing a new
    snapshot between separate lookups cannot combine values from two versions.
    This function never touches the filesystem and fails closed when no row is
    installed.
    """
    if not tag_id:
        return ("none", False, False)
    snapshot = _cache
    if snapshot is None:
        return ("none", False, False)
    row = snapshot[1].get(tag_id)
    if row is None:
        return ("none", False, False)
    return (row[0], row[1], True)


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
    policy, status, _row_exists = resolve_grant_record(tag_id)
    return (policy, status)


def has_grant_row(tag_id: str) -> bool:
    """True when the cached snapshot carries a protected row for ``tag_id``.

    Distinct from :func:`resolve_grant`, whose ``("none", False)`` default is
    deliberately indistinguishable from a minted none-row: policy resolution
    must fail closed either way. Existence identifies dashboard-minted tags at
    the PATCH seam. Same snapshot discipline as the resolver: cache-only,
    never touches the filesystem, fail-closed (no snapshot reads as no row).
    """
    return resolve_grant_record(tag_id)[2]


def _read_for_write(path: Path) -> dict[str, Any]:
    """Load the raw document for read-modify-write; refuse when untrustworthy."""
    if not path.exists():
        return {"version": _SCHEMA_VERSION, "grants": {}}
    try:
        raw = _read_bounded_json(path, _MAX_STORE_BYTES)
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
    global _cache, _degraded
    sig = _stat_signature(path)
    if sig is not None:
        with _cache_lock:
            _cache = (sig, _parse_rows(document, allow_oversized=True))
            # A document this process just wrote and re-parsed is a healthy
            # store; a "missing"/"unreadable" flag from an earlier read is over.
            _degraded = None


def mint_grant(tag_id: str, *, policy: str, status: bool) -> None:
    """Record (or update) a grant row. Caller is an authenticated dashboard write.

    ``policy`` may be ``"none"``: such a row grants no write authority but
    records dashboard provenance and the protected STATUS bit. Use
    :func:`revoke_grant` only for tag deletion; ordinary PATCH transitions keep
    the identity row.
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
    ids from the code-level seed vocabulary. ``tags.json`` is never an identity
    source because promoting its rows would turn agent-controlled data into
    authorization. A pre-existing custom tag therefore requires explicit
    dashboard-owner adoption, which is the migration cost of not laundering the
    file's contents into the trust store. Returns True when a store was written;
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
      custom tag identity is restored only by explicit dashboard-owner adoption.
    - A store whose rows fail verification under the certified key is
      quarantined and reseeded from trusted constants: forged rows stop
      resolving on the very boot that finds them, not when a human notices.
    - The reseed is READ BACK and verified before authenticated writes may
      land on it (:func:`_mark_reseed_verified`). The quarantine keeps
      REDUCING grants for the process (:func:`store_degraded`), but the
      dashboard-owner adoption that restores a custom tag's identity works on
      this same boot rather than after a restart (:func:`store_write_blocked`).
    """
    _store_dir()  # link guard BEFORE any path read or rename
    path = _store_path()
    try:
        key, certified = _load_store_key()
    except GrantStoreTransientReadError:
        # Fail closed WITHOUT mutation: the key could not be read, which is
        # not evidence about its contents. Quarantining here would turn a
        # transient I/O error into the permanent loss of every custom identity.
        logger.warning("agent-tag-policy store key unreadable at boot; leaving store untouched")
        return False
    if key is not None and not certified:
        # An uncertified key is quarantined UNCONDITIONALLY. A token-key
        # regeneration also lands here and costs the stored grants — that is
        # deliberate: at that moment a legitimate store and an agent-planted
        # self-signed one are cryptographically indistinguishable (the old
        # token key that could tell them apart is gone), so certifying
        # either certifies both, and a forged authorization blessed once
        # persists forever. The loss fails CLOSED (every tag resolves to
        # ``("none", False)``), default status ids re-seed below on this
        # same boot. A custom tag whose identity was quarantined requires
        # explicit dashboard-owner adoption; PATCH cannot recreate provenance.
        _quarantine_store(_store_key_path(), "store key not certified at boot")
        _quarantine_store(path, "store present with an uncertified key")
        key = None
    if path.exists():
        if key is not None and certified:
            try:
                raw_doc = _read_bounded_json(path, _MAX_STORE_BYTES)
            except OSError:
                # Same rule as the key: a document that exists but could not
                # be read is not a forgery. Zero grants this boot, no rename.
                logger.warning("agent-tag-policy store unreadable at boot; leaving store untouched")
                return False
            except Exception:
                # Read fine, but not a document this module wrote (oversized,
                # not JSON): that IS evidence, and it is quarantined below.
                verified = False
            else:
                try:
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
    _mark_reseed_verified(path)
    return True


def _mark_reseed_verified(path: Path) -> None:
    """Reopen the write gate only for a reseed that verifies FROM DISK.

    ``_write_document`` installs the snapshot from the document in memory; a
    quarantine is repaired by what actually reached the file. The document is
    read back through the same bounded reader, provenance chain and parser
    the resolver trusts, and the installed snapshot must carry that file's
    signature. A reseed that does not verify is uninstalled so the resolver
    serves zero grants for it, and the quarantine stays unrepaired -- an
    authenticated write then refuses instead of landing on bytes nobody has
    checked.
    """
    global _cache, _degraded, _quarantine_repaired
    try:
        raw = _read_bounded_json(path, _MAX_STORE_BYTES)
        if not _provenance_valid(raw):
            raise GrantStoreUnreadable("provenance MAC missing or invalid")
        _parse_rows(raw)
        sig = _stat_signature(path)
        with _cache_lock:
            if sig is None or _cache is None or _cache[0] != sig:
                raise GrantStoreUnreadable("installed snapshot is not the written store")
            _degraded = None
            _quarantine_repaired = True
    except Exception:
        logger.error("agent-tag-policy reseed does not verify; store not trusted", exc_info=True)
        with _cache_lock:
            _cache = None
            _degraded = "unreadable"


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
