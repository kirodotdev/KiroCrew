"""An installed app's on-disk state, and the reads that must agree with it.

Where an app lives (``apps_dir``, ``app_dir``, ``app_data_dir``), its ``installed.json``
record (:class:`InstalledApp`), its approved ``permissions.api`` / ``permissions.events``
set, the fencing that withholds grants while an update has its tree moved aside
(``staged_app_grants``), the lifecycle result type (:class:`AppResult`) and the parsed
manifest of an installed app (``get_app_manifest``).

Part of the ``kiro_crew.apps.manager`` family: callers and tests reach every name here
as ``kiro_crew.apps.manager.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.point_grants import point_grant_families
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.owner_only_files import ensure_directory

logger = logging.getLogger("kiro_crew.apps.manager")


APP_MANIFEST_FILENAME = "app.json"

INSTALLED_META_FILENAME = "installed.json"

# The approved ``permissions.api`` / ``permissions.events`` set, beside the
# record but in its own file so only the grant-owning paths ever write it.
APPROVED_GRANTS_FILENAME = "approved-grants.json"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def apps_dir() -> Path:
    """Return the root directory for installed apps: ``~/.kiro/crew/apps/``."""
    return config_dir() / "apps"


def app_dir(name: str) -> Path:
    """Return the directory for a specific installed app."""
    return apps_dir() / name


def app_data_dir(name: str) -> Path:
    """Return the app-scoped data directory: ``~/.kiro/crew/apps/{name}/data/``."""
    d = app_dir(name) / "data"
    ensure_directory(d)  # 0700 in the data home, apps/ and apps/<name>/ included
    return d


# ---------------------------------------------------------------------------
# Installed metadata
# ---------------------------------------------------------------------------

# Valid values for InstalledApp classification fields
_VALID_ORIGIN: frozenset[str] = frozenset({"builtin", "registry", "local", "external"})

_VALID_RESOURCES: frozenset[str] = frozenset({"gateway", "app"})

_VALID_LIFECYCLE: frozenset[str] = frozenset({"gateway", "app", "locked"})


def _read_enabled(data: dict[str, Any]) -> bool:
    """The one reading of ``enabled`` that every app reader shares.

    A JSON boolean is taken as is. A missing key reads as enabled, as before:
    every host writer stores a real bool, so only a hand-made record lacks it.
    Any other value (``"false"``, ``"true"``, ``0``, ``1``, ``null``) reads as
    DISABLED. ``bool()`` made any non-empty string enabled, so ``"false"``
    turned an app on; failing closed means an unclear value never runs an app.
    """
    if "enabled" not in data:
        return True
    value = data["enabled"]
    if isinstance(value, bool):
        return value
    logger.warning(
        "InstalledApp %s has a non-boolean enabled value %r: reading it as disabled",
        data.get("name", ""),
        value,
    )
    return False


@dataclass
class InstalledApp:
    """Metadata persisted in ``installed.json`` for each installed app.

    Three orthogonal classification fields replace the old ``managed`` field:

    ``origin`` — where the app came from (read-only, set at install time):
      - ``"builtin"``: baked into the Kiro Crew dashboard
      - ``"registry"``: installed from the curated app registry
      - ``"local"``: installed from a local directory path
      - ``"external"``: self-registered via SDK / API

    ``resources`` — who manages agent/skill/cron registration:
      - ``"gateway"``: Kiro Crew manages via bridges.py symlinks
      - ``"app"``: the app manages its own resource registration

    ``lifecycle`` — who manages updates and uninstall:
      - ``"gateway"``: Kiro Crew handles updates and uninstall
      - ``"app"``: the app handles its own updates
      - ``"locked"``: cannot be uninstalled (builtin only)
    """

    name: str = ""
    version: str = ""
    displayName: str = ""  # noqa: N815
    enabled: bool = True
    installedAt: str = ""  # noqa: N815
    updatedAt: str = ""  # noqa: N815
    source: str = ""  # concrete provenance: path, URL, "registry:name", "builtin"
    origin: str = "registry"  # builtin | registry | local | external
    resources: str = "gateway"  # gateway | app
    lifecycle: str = "gateway"  # gateway | app | locked
    schemaVersion: int = 2  # noqa: N815  — schema version for future migrations
    migratedTo: str = (
        ""  # noqa: N815  — target standalone app: "registry:{name}" or "standalone:{name}"
    )
    dev: bool = False  # dev mode: no-store UI serving + file-watch live reload
    # Whether this record has already received a default-on PROMOTION (see
    # ``_DEFAULT_ON_BACKFILL``).  Lives on the record rather than in a marker file
    # so it is written by the SAME atomic write that flips ``enabled``: two
    # separate writes have no correct ordering, since whichever goes first leaves
    # a window the other owns (a lost flag re-applies the promotion forever and
    # reverses the user's own disable; a flag that outlives a failed flip skips
    # the app forever and never delivers it).  A record created under the promoted
    # default is born ``True``: a first registration with ``defaultEnabled`` is
    # the promotion being received, so nothing is owed.  Meaningless-but-inert
    # (``False``) for every app that is not a promotion target.
    defaultOnBackfilled: bool = False  # noqa: N815
    # Structured install provenance, recorded for registry installs (see
    # ``set_app_provenance``).  ``source`` alone is a bare ``registry:<name>``
    # marker that re-resolves by name, so a same-named entry from a different
    # registry source could answer for this app; these fields pin WHICH source it
    # actually came from.  ``sourceUrl`` is the presence discriminator: empty
    # means a legacy record installed before provenance was captured (an empty
    # ``sourceRegistry`` is meaningful on its own — it denotes the bundled
    # catalog rather than a configured external registry).
    sourceUrl: str = ""  # noqa: N815  — git URL this app was installed from
    sourceRegistry: str = ""  # noqa: N815  — external registry id; "" = bundled catalog
    sourceCommit: str = ""  # noqa: N815  — commit SHA resolved in the source clone
    sourceSigner: str = ""  # noqa: N815  — verified signer id; "" = no verified signature
    # True while a newly declared session-control grant still needs a user
    # consent moment. Kept separate from ``enabled`` so a normal manual disable
    # never shows the re-consent warning.
    sessionApprovalConsentPending: bool = False  # noqa: N815
    # True once this app's approved-grants file has been written. That file
    # sits in the app tree, which an update swaps out before the new set is
    # written, so a reader can find no file for a moment. A record carrying
    # this flag reads that absence as "nothing approved"; only a record without
    # it (an install from before the set was stored) approves what its
    # manifest declares.
    approvedGrantsStored: bool = False  # noqa: N815

    def validate_fields(self) -> list[str]:
        """Validate classification field values. Returns error list (empty = valid)."""
        errors: list[str] = []
        if self.origin not in _VALID_ORIGIN:
            errors.append(f"invalid origin: {self.origin!r}")
        if self.resources not in _VALID_RESOURCES:
            errors.append(f"invalid resources: {self.resources!r}")
        if self.lifecycle not in _VALID_LIFECYCLE:
            errors.append(f"invalid lifecycle: {self.lifecycle!r}")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v or isinstance(v, (bool, int))}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstalledApp:
        inst = cls(
            name=str(data.get("name", "")),
            version=str(data.get("version", "")),
            displayName=str(data.get("displayName", "")),
            enabled=_read_enabled(data),
            installedAt=str(data.get("installedAt", "")),
            updatedAt=str(data.get("updatedAt", "")),
            source=str(data.get("source", "")),
            origin=str(data.get("origin", "registry")),
            resources=str(data.get("resources", "gateway")),
            lifecycle=str(data.get("lifecycle", "gateway")),
            schemaVersion=int(data.get("schemaVersion", 1)),
            migratedTo=str(data.get("migratedTo", "")),
            dev=bool(data.get("dev", False)),
            defaultOnBackfilled=bool(data.get("defaultOnBackfilled", False)),
            sourceUrl=str(data.get("sourceUrl", "")),
            sourceRegistry=str(data.get("sourceRegistry", "")),
            sourceCommit=str(data.get("sourceCommit", "")),
            sourceSigner=str(data.get("sourceSigner", "")),
            sessionApprovalConsentPending=bool(data.get("sessionApprovalConsentPending", False)),
            approvedGrantsStored=data.get("approvedGrantsStored") is True,
        )
        # Migrate old "managed" field to new classification fields
        if inst.schemaVersion < 2 and "origin" not in data:
            old_managed = data.get("managed", "")
            if old_managed == "self":
                inst.origin = "external"
                inst.resources = "app"
                inst.lifecycle = "app"
            elif old_managed == "builtin":
                inst.origin = "builtin"
                inst.resources = "gateway"
                inst.lifecycle = "locked"
            elif old_managed in ("kirocrew", ""):
                source = data.get("source", "")
                if source.startswith("registry:"):
                    inst.origin = "registry"
                elif source and not source.startswith("builtin"):
                    inst.origin = "local"
                else:
                    inst.origin = "registry"
                inst.resources = "gateway"
                inst.lifecycle = "gateway"
            inst.schemaVersion = 2
        errors = inst.validate_fields()
        if errors:
            logger.warning(
                "InstalledApp %s has invalid fields: %s — using defaults",
                inst.name,
                errors,
            )
            if inst.origin not in _VALID_ORIGIN:
                inst.origin = "registry"
            if inst.resources not in _VALID_RESOURCES:
                inst.resources = "gateway"
            if inst.lifecycle not in _VALID_LIFECYCLE:
                inst.lifecycle = "gateway"
        return inst


# ---------------------------------------------------------------------------
# Record lock: one writer of an app's installed.json at a time
# ---------------------------------------------------------------------------

# Every writer reads the record, changes it and replaces the whole file, so two
# writers that overlap lose one change (a dev-mode save landing after a widening
# update undoes its consent-pending mark). The lock is a SIDECAR outside the app
# directories, never installed.json: the record is replaced via os.replace, so a
# lock on it would sit on an inode the next writer never opens; and update_app
# moves the whole app directory aside, so a lock inside it would too.
_record_lock_state = threading.local()


def _record_lock_path(name: str) -> Path:
    # Outside apps/ so app listings and tree scans never meet a lock file.
    return config_dir() / "app-record-locks" / f"{name}.lock"


@contextmanager
def app_record_lock(name: str) -> Iterator[None]:
    """Hold *name*'s cross-process record lock; reentrant within one thread.

    POSIX ``flock`` / Windows ``msvcrt.locking`` via ``platform_compat.file_lock``,
    bounded and FAIL-CLOSED: a lock that cannot be taken raises, so no writer
    proceeds unserialized. A nested acquire in the same thread (a transaction
    calling ``_write_installed``) reuses the held lock instead of deadlocking on
    a second descriptor.
    """
    held: dict[str, int] = getattr(_record_lock_state, "held", None) or {}
    _record_lock_state.held = held
    if held.get(name):
        held[name] += 1
        try:
            yield
        finally:
            held[name] -= 1
        return
    from kiro_crew import platform_compat

    path = _record_lock_path(name)
    ensure_directory(path.parent)
    fd = platform_compat.open_create_or_existing(path, os.O_RDWR)
    try:
        with platform_compat.file_lock(fd, exclusive=True):
            held[name] = 1
            try:
                yield
            finally:
                held.pop(name, None)
    finally:
        os.close(fd)


def holds_app_record_locks(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Release every record lock *fn* took with :func:`hold_app_record_lock`."""
    import functools
    from contextlib import ExitStack

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        stacks: list[ExitStack] = getattr(_record_lock_state, "stacks", None) or []
        _record_lock_state.stacks = stacks
        with ExitStack() as stack:
            stacks.append(stack)
            try:
                return fn(*args, **kwargs)
            finally:
                stacks.pop()

    return wrapper


def hold_app_record_lock(name: str) -> None:
    """Take *name*'s record lock until the enclosing ``@holds_app_record_locks`` returns.

    Called right before a writer first reads the record, so the lock spans its
    read, its checks and its write. Outside such a function it raises rather
    than run unserialized.
    """
    stacks = getattr(_record_lock_state, "stacks", None)
    if not stacks:
        raise RuntimeError("hold_app_record_lock needs an @holds_app_record_locks caller")
    stacks[-1].enter_context(app_record_lock(name))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_installed(name: str, *, manifest: AppManifest | None = None) -> InstalledApp | None:
    """Read installed.json for an app, or None if not installed.

    *manifest* is the live manifest the caller already read. A permission
    check passes it so the pending decision uses the same manifest that
    showed the grant, rather than a second read that could differ.
    """
    meta_path = app_dir(name) / INSTALLED_META_FILENAME
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        meta = InstalledApp.from_dict(data)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", meta_path, exc)
        return None
    if _session_approval_consent_unrecorded(name, data, manifest=manifest):
        meta.sessionApprovalConsentPending = True
    return meta


SESSION_APPROVAL_PENDING_KEY = "sessionApprovalConsentPending"


def _session_approval_consent_unrecorded(
    name: str, data: Any, *, manifest: AppManifest | None = None
) -> bool:
    """Whether a record predates the consent marker while its manifest needs one.

    Every record this host writes carries ``sessionApprovalConsentPending``
    (``to_dict`` keeps every bool), so a record without the key was written by a
    host that ignored ``permissions.sessionApproval``. If the live manifest
    declares that grant, nobody ever consented to it. Key absence is only read
    in this fail-closed direction: it makes the grant pending, never approved.
    """
    if isinstance(data, dict) and SESSION_APPROVAL_PENDING_KEY in data:
        return False
    if manifest is None:
        manifest = get_app_manifest(name)
    return bool(manifest and manifest.permissions.sessionApproval)


def _credential_free_source_metadata(value: str) -> str:
    """Sanitize an explicit remote URI while preserving path/id metadata."""
    candidate = value.strip()
    if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", candidate) is None:
        return value

    # Deferred because ``apps.registry`` imports this module.
    from kiro_crew.apps.registry import _strip_git_target_userinfo

    return _strip_git_target_userinfo(candidate)


def _session_approval_save_rule(
    name: str, meta: InstalledApp, *, session_approval_consent: bool
) -> str:
    """How a save writes *name*'s ``sessionApprovalConsentPending``.

    ``"as_is"`` writes the caller's value, ``"pending"`` writes True and
    ``"unrecorded"`` leaves the key out. One read of the record ON DISK and one
    look for the manifest decide it, so two observations can never disagree.

    When the disk copy has no consent key (it never had a consent moment) or
    already says pending, a save may record "no consent needed" only when the
    owner just consented, or when the live manifest is readable and does not
    declare the grant. A manifest that cannot be read keeps it pending. With no
    manifest at all a pending record stays pending and a key-less one stays
    key-less, so a manifest that returns with the grant still asks for consent.
    So an unreadable or missing manifest, an early save, a stale copy written
    back by a rollback, or two writers overlapping never record a consent that
    was not given.
    """
    if meta.sessionApprovalConsentPending or session_approval_consent:
        return "as_is"
    try:
        raw = json.loads((app_dir(name) / INSTALLED_META_FILENAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "as_is"  # a new record; its writer decides from the manifest
    except (OSError, ValueError):
        return "pending"
    if not isinstance(raw, dict):
        return "pending"
    on_disk = raw.get(SESSION_APPROVAL_PENDING_KEY, None)
    if SESSION_APPROVAL_PENDING_KEY in raw and on_disk is not True:
        return "as_is"
    if not os.path.lexists(app_dir(name) / APP_MANIFEST_FILENAME):
        return "pending" if on_disk is True else "unrecorded"
    manifest = get_app_manifest(name)
    if manifest is None or manifest.permissions.sessionApproval:
        return "pending"
    return "as_is"


def _write_installed(
    name: str, meta: InstalledApp, *, session_approval_consent: bool = False
) -> None:
    """Write credential-free installed.json metadata for an app.

    ``session_approval_consent`` is True only on the write that records the
    owner's consent (``enable_app``). Without it, a record that has no consent
    key on disk keeps ``sessionApprovalConsentPending`` (see
    ``_session_approval_save_rule``).

    A raw clone/source URL is a transport capability, not durable app identity.
    Registry callers already pass credential-free provenance, but the external
    registration API also accepts a free-form ``source`` and direct Python
    callers can supply ``sourceUrl`` independently.  ``sourceUrl`` is always a
    Git coordinate and is sanitized unconditionally.  ``source`` and
    ``sourceRegistry`` are discriminated metadata (path/marker/id OR URL), so
    only an explicit remote URI is sanitized; treating arbitrary ``:...@...:``
    text as SCP would corrupt valid POSIX filenames.

    ``approvedGrantsStored`` is kept set while the app's approved-grants file
    exists, so a writer holding a record read before the set was stored cannot
    clear it.

    The import is deferred because ``apps.registry`` imports this module.
    """
    from kiro_crew.apps.registry import _strip_git_target_userinfo

    # The save rule's on-disk read and the replace run under the record lock,
    # so no other writer can land between them.
    with app_record_lock(name):
        rule = _session_approval_save_rule(
            name, meta, session_approval_consent=session_approval_consent
        )
        credential_free_meta = replace(
            meta,
            approvedGrantsStored=meta.approvedGrantsStored or _approved_grants_path(name).is_file(),
            sessionApprovalConsentPending=meta.sessionApprovalConsentPending or rule == "pending",
            source=_credential_free_source_metadata(str(meta.source or "")),
            sourceUrl=_strip_git_target_userinfo(str(meta.sourceUrl or "")),
            sourceRegistry=_credential_free_source_metadata(str(meta.sourceRegistry or "")),
        )
        body = credential_free_meta.to_dict()
        if rule == "unrecorded":
            body.pop(SESSION_APPROVAL_PENDING_KEY, None)
        meta_path = app_dir(name) / INSTALLED_META_FILENAME
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(meta_path, json.dumps(body, indent=2) + "\n")


# Grant families held back on a widening update. ``sessionApproval`` is a flag
# with its own disable-until-consent path below; these are entry lists. The
# point families (``contributes.linkPreviews`` and the other points, registered in
# ``point_grants``) follow; a stored set from before one existed does not carry it, and a missing family approves what the manifest declares, as a missing
# file does for an install that never stored one.
STAGED_GRANT_FAMILIES = ("api", "events", *point_grant_families())
#: Families every stored set carries; the others are absent from an older file.
_ALWAYS_STORED_FAMILIES = ("api", "events")


def _parse_approved_grants(raw: Any) -> dict[str, list[str]]:
    """Read the parsed contents of an approved-grants file, failing closed.

    Anything but a mapping approves nothing, so a damaged file holds the app to
    none of its declared api/events entries rather than to all of them.
    """
    record = raw if isinstance(raw, dict) else {}
    parsed: dict[str, list[str]] = {}
    for family in STAGED_GRANT_FAMILIES:
        if family not in _ALWAYS_STORED_FAMILIES and isinstance(raw, dict) and family not in record:
            # Written before this family existed: no decision recorded for it.
            continue
        entries = record.get(family)
        parsed[family] = (
            [e for e in entries if isinstance(e, str)] if isinstance(entries, list) else []
        )
    return parsed


def _approved_grants_path(name: str) -> Path:
    return app_dir(name) / APPROVED_GRANTS_FILENAME


def _read_approved_grants(name: str) -> dict[str, list[str]] | None:
    """Read *name*'s approved api/events set, fresh from disk, failing closed.

    ``None`` means no file; ``_effective_approved_grants`` decides what that
    approves for the app's install record. A file that cannot be read or
    parsed approves nothing.
    """
    path = _approved_grants_path(name)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return {family: [] for family in STAGED_GRANT_FAMILIES}
    try:
        return _parse_approved_grants(json.loads(text))
    except json.JSONDecodeError:
        return {family: [] for family in STAGED_GRANT_FAMILIES}


def _effective_approved_grants(name: str, meta: InstalledApp | None) -> dict[str, list[str]] | None:
    """*name*'s approved set as *meta* (its install record) qualifies it.

    A missing file approves what the manifest declares (``None``) only for a
    record that has never stored a set. Once one was stored, a missing file
    approves nothing: it is a file an update's tree swap has not put back yet,
    or one that was lost, and neither is the owner's approval.
    """
    grants = _read_approved_grants(name)
    if grants is None and meta is not None and meta.approvedGrantsStored:
        return {family: [] for family in STAGED_GRANT_FAMILIES}
    return grants


def _write_approved_grants(name: str, grants: dict[str, list[str]] | None) -> None:
    """Atomically store *name*'s approved set; ``None`` removes the file.

    Only the grant-owning paths call this: install, update, registration and
    an owner approval. Every other record write leaves the set alone because it
    never touches this file.
    """
    path = _approved_grants_path(name)
    if grants is None:
        path.unlink(missing_ok=True)
        return
    body = {
        family: list(grants.get(family, ()))
        for family in STAGED_GRANT_FAMILIES
        if family in _ALWAYS_STORED_FAMILIES or family in grants
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(body, indent=2) + "\n")


def _declared_grant_entries(permissions: Any) -> dict[str, list[str]]:
    """``api``/``events`` entries from a manifest's permissions (object or raw dict)."""
    declared: dict[str, list[str]] = {}
    for family in STAGED_GRANT_FAMILIES:
        if isinstance(permissions, dict):
            entries = permissions.get(family)
        else:
            entries = getattr(permissions, family, None)
        declared[family] = (
            [e for e in entries if isinstance(e, str) and e] if isinstance(entries, list) else []
        )
    return declared


def _approved_grants_after_manifest_change(
    *,
    existing_approved: dict[str, list[str]] | None,
    old_permissions: Any,
    new_permissions: Any,
) -> dict[str, list[str]]:
    """Return the approved set to store after an update or re-registration.

    The baseline is what the owner approved. A record from before the field
    approves what the replaced manifest declared; with no readable replaced
    manifest there is no known approval, so the baseline is empty. The result
    keeps only baseline entries the new manifest still declares: an entry the
    new manifest adds is not approved, and one it drops must be approved again
    if a later version brings it back.
    """
    old_declared = _declared_grant_entries(old_permissions) if old_permissions is not None else {}
    if existing_approved is not None:
        # A family the stored set predates approves what the replaced manifest declared.
        baseline = {
            family: (
                existing_approved[family]
                if family in existing_approved
                else old_declared.get(family, [])
            )
            for family in STAGED_GRANT_FAMILIES
        }
    else:
        baseline = old_declared
    declared = _declared_grant_entries(new_permissions)
    return {
        family: [e for e in declared[family] if e in set(baseline.get(family, ()))]
        for family in STAGED_GRANT_FAMILIES
    }


def _held_back_entries(
    approved: dict[str, list[str]] | None, permissions: Any
) -> dict[str, list[str]]:
    """Declared api/events entries the approved set does not cover."""
    declared = _declared_grant_entries(permissions)
    if approved is None:
        return {family: [] for family in STAGED_GRANT_FAMILIES}
    return {
        family: (
            [e for e in declared[family] if e not in set(approved[family])]
            if family in approved
            else []
        )
        for family in STAGED_GRANT_FAMILIES
    }


def _update_retired_tree_prefix(name: str) -> str:
    """Name prefix of the tree ``update_app`` moves *name*'s live tree to."""
    return f".{name}-update-old-"


# Tree-swap fencing for this process. ``update_app`` holds a swap open from
# before it moves an app's tree aside until the moved-aside tree is gone. Each
# swap start and end takes the next value of a process-wide sequence; an app's
# last value is kept in a bounded LRU, and its in-flight count only while one
# is open. A grant read that sees a swap in flight, or a change across its
# reads, grants nothing.
_TREE_SWAP_LAST_CAP = 256

# (swaps in flight, the app's last sequence value, the process sequence)
_SwapState = tuple[int, int | None, int]

_tree_swap_guard = threading.Lock()

_tree_swap_seq = 0

_tree_swaps_active: dict[str, int] = {}

_tree_swap_last: OrderedDict[str, int] = OrderedDict()


def _tree_swap_mark(name: str, delta: int) -> None:
    """Record a swap start (+1) or end (-1) for *name*; caller holds the guard."""
    global _tree_swap_seq
    _tree_swap_seq += 1
    active = _tree_swaps_active.get(name, 0) + delta
    if active > 0:
        _tree_swaps_active[name] = active
    else:
        _tree_swaps_active.pop(name, None)
    _tree_swap_last[name] = _tree_swap_seq
    _tree_swap_last.move_to_end(name)
    while len(_tree_swap_last) > _TREE_SWAP_LAST_CAP:
        _tree_swap_last.popitem(last=False)


@contextmanager
def _tree_swap(name: str) -> Iterator[None]:
    with _tree_swap_guard:
        _tree_swap_mark(name, 1)
    try:
        yield
    finally:
        with _tree_swap_guard:
            _tree_swap_mark(name, -1)


def _tree_swap_state(name: str) -> _SwapState:
    """``(in flight, last sequence value, process sequence)`` for *name*."""
    with _tree_swap_guard:
        return (_tree_swaps_active.get(name, 0), _tree_swap_last.get(name), _tree_swap_seq)


def _tree_swap_spanned(before: _SwapState, after: _SwapState) -> bool:
    """Whether a swap of the app was in flight at, or ran between, two states.

    An app whose last value is absent at both ends may have had it evicted in
    between; that takes at least ``_TREE_SWAP_LAST_CAP`` sequence steps.
    """
    if before[0] or after[0] or before[1] != after[1]:
        return True
    return after[1] is None and after[2] - before[2] >= _TREE_SWAP_LAST_CAP


def _update_marker_dir(name: str) -> Path:
    """Directory that exists while any process's update of *name* is in flight."""
    return app_dir(name).parent / f".{name}-update-inflight"


class _UpdateMarker:
    """One update's token in ``_update_marker_dir``, held across the tree swap.

    Each update adds its own file and, when done, removes it and then the
    directory, which stays while another update still holds a token. A reader
    asks one question of the directory -- does it exist -- so the check is a
    single stat rather than a listing. A crash leaves the directory behind,
    which only withholds grants from an app with nothing else on disk.
    """

    def __init__(self, token: Path) -> None:
        self._token = token

    @classmethod
    def hold(cls, name: str) -> _UpdateMarker:
        root = _update_marker_dir(name)
        token = root / f"{os.getpid()}-{os.urandom(4).hex()}"
        for _attempt in range(8):
            root.mkdir(parents=True, exist_ok=True)
            try:
                token.touch(exist_ok=False)
            except FileNotFoundError:
                # Another update's cleanup removed the directory in between.
                continue
            return cls(token)
        raise OSError(f"could not mark the update of {name!r} in progress")

    def __enter__(self) -> _UpdateMarker:
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            self._token.unlink()
        except OSError:
            logger.warning("Could not remove update marker %s", self._token, exc_info=True)
        try:
            self._token.parent.rmdir()
        except OSError:
            pass  # another update still holds a token, or the directory is gone


def _update_in_progress(name: str) -> bool:
    """Whether an update of *name* (in any process) may have its tree moved aside."""
    try:
        return os.path.lexists(_update_marker_dir(name))
    except (OSError, ValueError):
        return True


def staged_app_grants(
    name: str, family: str, read_declared: Callable[[], Sequence[str] | None]
) -> list[str]:
    """Return *name*'s declared ``permissions.<family>`` entries that are approved.

    The enforcement points (``token_auth._app_api_allowlist``,
    ``ws_event_scope._read_declared_events``, the hook context) grant exactly
    this: declared by the live manifest (*read_declared*, ``None`` when there is
    none) and in the app's approved-grants file, both read fresh. An install
    whose record has never stored the file approves what is declared. An app
    with nothing on disk passes the declared entries unchanged (callers gate on
    installation themselves); an app directory without a readable record, or
    a tree an update has moved aside and not yet replaced, is not evidence of
    approval and grants nothing.
    """
    swap_before = _tree_swap_state(name)
    if swap_before[0]:
        return []
    declared = read_declared()
    if declared is None:
        return []
    meta = _read_installed(name)
    if meta is None:
        # The moved-aside tree first, then the live one: an update that ends
        # between the two checks has put the live tree back by the second.
        if _update_in_progress(name):
            return []
        root = app_dir(name)
        if (root / INSTALLED_META_FILENAME).exists() or (root / APP_MANIFEST_FILENAME).exists():
            return []
        granted = list(declared)
    else:
        grants = _effective_approved_grants(name, meta)
        if grants is None:
            # No set file beside a record that never stored one. The record
            # must still be the one read, with still no file, or an update
            # (in this process or another) moved the tree in between.
            if _read_installed(name) != meta or _approved_grants_path(name).exists():
                return []
            granted = list(declared)
        else:
            if family not in grants:
                # The stored set predates this family: what is declared is approved.
                granted = list(declared)
            else:
                approved = set(grants[family])
                granted = [entry for entry in declared if entry in approved]
    if _tree_swap_spanned(swap_before, _tree_swap_state(name)):
        return []
    return granted


def _approved_grants_row(name: str) -> dict[str, Any]:
    """The ``approvedGrants`` key for an app row, absent when no set is stored."""
    grants = _effective_approved_grants(name, _read_installed(name))
    if grants is None:
        return {}
    # An empty link-preview family says nothing an app row needs: the row's
    # ``linkPreviewGrant`` is where a provider's approved and waiting entries live.
    shown = {
        family: entries
        for family, entries in grants.items()
        if family in _ALWAYS_STORED_FAMILIES or entries
    }
    return {"approvedGrants": shown}


def approved_manifest_permissions(app_info: dict[str, Any]) -> dict[str, Any]:
    """``app_info``'s manifest permissions limited to approved api/events entries.

    For the hook-context builders, which start from a ``get_app``/``list_apps``
    row; the live manifest and record are re-read through ``staged_app_grants``.
    """
    name = str(app_info.get("name", ""))
    manifest = app_info.get("manifest") or {}
    permissions = dict(manifest.get("permissions") or {})
    for family in STAGED_GRANT_FAMILIES:
        if family not in permissions:
            continue

        def _read(family: str = family) -> list[str] | None:
            live = get_app_manifest(name)
            if live is None:
                return None
            return [e for e in getattr(live.permissions, family) if e]

        permissions[family] = staged_app_grants(name, family, _read)
    return permissions


def _pending_session_approval_after_manifest_change(
    *,
    existing_pending: bool,
    requested_session_approval: bool,
    widened_session_approval: bool,
) -> bool:
    if widened_session_approval:
        return True
    if not requested_session_approval:
        return False
    return existing_pending


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class AppResult:
    """Result of an app lifecycle operation."""

    ok: bool = True
    name: str = ""
    message: str = ""
    error: str = ""
    error_code: str = ""  # structured error code for HTTP status mapping
    secret: str = ""
    #: Machine-readable qualifier on a SUCCESSFUL result -- something the caller
    #: must show or act on even though the operation went through (an update that
    #: left the app disabled pending consent). Serialized as ``notice`` so it can
    #: never be mistaken for the failure ``code``.
    notice: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ok": self.ok, "name": self.name}
        if self.message:
            d["message"] = self.message
        if self.notice:
            d["notice"] = self.notice
        if self.error:
            d["error"] = self.error
        # `code` is the repo's wire contract for a machine-readable failure
        # (test_error_code_contract.py); `error` is advisory prose. This field
        # existed but was never serialized, so every structured code set by a
        # caller was silently dropped on the way to the client -- leaving the
        # frontend with untranslatable English prose and no way to tell WHICH
        # failure it was, which is why an execution-policy denial could not be
        # given an actionable affordance.
        if self.error_code:
            d["code"] = self.error_code
        return d


def get_app_manifest(name: str) -> AppManifest | None:
    """Return the parsed manifest for an installed app, or None."""
    manifest_path = app_dir(name) / APP_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        return AppManifest.from_json_file(manifest_path)
    except Exception:
        return None


# Scan and central publication are one ordered operation. Lifecycle changes for
# different apps can finish concurrently; without this lock an older scan can
# publish after a newer scan and resurrect stale keys.
_config_keys_refresh_lock = threading.Lock()


def _scan_installed_app_config_keys(root: Path) -> frozenset[str]:
    if not root.is_dir():
        return frozenset()
    keys: set[str] = set()
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        manifest = get_app_manifest(entry.name)
        try:
            installed = _read_installed(entry.name, manifest=manifest)
        except Exception:
            # InstalledApp.from_dict performs real coercion and can raise for a
            # malformed field. One damaged record must not freeze every other
            # app's last published declarations.
            logger.warning(
                "Skipping malformed installed-app record for %s", entry.name, exc_info=True
            )
            continue
        if installed is None:
            continue
        if manifest is not None and not manifest.config_keys_error:
            keys.update(manifest.config_keys)
    return frozenset(keys)


def refresh_app_config_keys() -> frozenset[str]:
    """Scan installed apps and atomically publish their config declarations.

    Startup and lifecycle callers perform this synchronous filesystem work;
    config validation only reads the published in-memory snapshot.
    """
    from kiro_crew.config.validation import (
        extension_config_keys,
        replace_extension_config_keys,
    )

    with _config_keys_refresh_lock:
        try:
            keys = _scan_installed_app_config_keys(apps_dir())
        except Exception:
            logger.debug("app config-key discovery failed", exc_info=True)
            return extension_config_keys("apps")
        return replace_extension_config_keys("apps", keys)
