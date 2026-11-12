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
import math
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple, Sequence

from kiro_crew import member_records
from kiro_crew.apps import credential_masks, protected_app_state
from kiro_crew.apps.execution import APP_NAME_RE
from kiro_crew.apps.host_exec_grants import revoke_host_exec
from kiro_crew.apps.manifest import AppManifest, Permissions
from kiro_crew.apps.point_grants import (
    explicit_only_grant_families,
    grant_source,
    point_grant_families,
)
from kiro_crew.apps.worker_auto_approve import WORKER_AUTO_APPROVE_GRANT_FAMILIES
from kiro_crew.apps.worker_auto_approve import revoke_app as revoke_worker_auto_approve
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.jsonl_util import open_regular_nofollow
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


def _is_sidecar_entry(entry_name: str) -> bool:
    """Whether an ``apps_dir()`` entry is a gateway sidecar rather than an app.

    No admissible app name starts with a dot, and every working path the gateway
    puts beside an app directory does (``.<name>-update-old-<pid>-<hex>``,
    ``.<name>-update-inflight``, ``.<name>-data-tmp``). A retired tree still holds
    the app's previous ``installed.json``, so a walk that reads every entry's
    record lists the app twice. Every walk of ``apps_dir()`` skips these.
    """
    return entry_name.startswith(".")


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


def _read_schema_version(data: dict[str, Any]) -> int:
    """The one reading of ``schemaVersion`` that every app reader shares.

    A missing key reads as 1 so the legacy-migration branch runs. Every host
    writer stores a JSON integer; only a hand-made or corrupted record carries
    another shape. ``int()`` raises on a non-numeric string, a list or ``null``
    (TypeError/ValueError) and on a non-finite float (OverflowError, from a
    ``1e400`` or ``Infinity`` that ``json.loads`` parses to ``inf``), and that
    is the one field in ``from_dict`` whose coercion could escape -- unlike the
    string and boolean fields, which never raise -- so a damaged
    ``schemaVersion`` would crash a reader that another malformed field would
    not. Read an unusable value as 1 (forcing the migration pass), matching how
    ``enabled`` and the origin/lifecycle/resources fields degrade rather than
    raise.
    """
    if "schemaVersion" not in data:
        return 1
    value = data["schemaVersion"]
    if isinstance(value, bool):
        # A JSON bool is an int subclass; it is not a real schema version.
        value = None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        logger.warning(
            "InstalledApp %s has a non-integer schemaVersion %r: reading it as 1",
            data.get("name", ""),
            value,
        )
        return 1


def _read_not_before(data: dict[str, Any]) -> float:
    """``appTokensNotBefore`` as a float; a missing or unusable value is 0."""
    value = data.get("appTokensNotBefore", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    # NaN compares false with everything, so it would admit every token; a
    # non-finite cutoff refuses them all instead.
    try:
        cutoff = float(value)
    except OverflowError:  # an int too large for a float
        return math.inf
    return cutoff if math.isfinite(cutoff) else math.inf


@dataclass
class InstalledApp:
    """Metadata persisted in ``installed.json`` for each installed app.

    Three orthogonal classification fields replace the old ``managed`` field:

    ``origin`` — where the app came from (set at install; a registry update rewrites it):
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
    # True once the owner consented to ``permissions.memberThreads``: set only
    # by an enable that recorded their consent while the manifest declared the
    # scope. A record from a host that ignored the scope never carries it, so an
    # app consented there reaches no member thread until the owner re-consents.
    memberThreadsConsented: bool = False  # noqa: N815
    # True once this app's approved set has been stored. Informational: the set
    # lives in the protected store (``apps.protected_app_state``), which alone
    # decides what is approved, because this file is writable from the app's
    # sandbox. The one-time import reads it to keep a record from before sets
    # were stored approving what its manifest declares.
    approvedGrantsStored: bool = False  # noqa: N815
    # Epoch seconds before which this app's tokens are refused. ``_write_installed``
    # moves it forward on every write that leaves the app disabled and on the
    # write that creates the record, so a token minted before a disable or before
    # an uninstall stays dead after the app is enabled or installed again. 0 on a
    # record from before the field existed: no cutoff.
    appTokensNotBefore: float = 0.0  # noqa: N815

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

    def to_row_dict(self) -> dict[str, Any]:
        """``to_dict`` for an API row: without the gateway-internal token cutoff."""
        row = self.to_dict()
        row.pop("appTokensNotBefore", None)
        return row

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
            schemaVersion=_read_schema_version(data),
            migratedTo=str(data.get("migratedTo", "")),
            dev=bool(data.get("dev", False)),
            defaultOnBackfilled=bool(data.get("defaultOnBackfilled", False)),
            sourceUrl=str(data.get("sourceUrl", "")),
            sourceRegistry=str(data.get("sourceRegistry", "")),
            sourceCommit=str(data.get("sourceCommit", "")),
            sourceSigner=str(data.get("sourceSigner", "")),
            sessionApprovalConsentPending=bool(data.get("sessionApprovalConsentPending", False)),
            memberThreadsConsented=data.get(MEMBER_THREADS_CONSENT_KEY) is True,
            approvedGrantsStored=data.get("approvedGrantsStored") is True,
            appTokensNotBefore=_read_not_before(data),
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
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", meta_path, exc)
        return None
    if _session_approval_consent_unrecorded(name, data, manifest=manifest):
        meta.sessionApprovalConsentPending = True
    if meta.enabled and protected_app_state.owner_disabled(name):
        # The owner's disable stands outside the app's tree; this file can be
        # rewritten from the sandbox, so it never turns the app back on.
        meta.enabled = False
    return meta


SESSION_APPROVAL_PENDING_KEY = "sessionApprovalConsentPending"
MEMBER_THREADS_CONSENT_KEY = "memberThreadsConsented"


def _session_approval_consent_unrecorded(
    name: str, data: Any, *, manifest: AppManifest | None = None
) -> bool:
    """Whether a record predates the consent marker while its manifest needs one.

    Every record this host writes carries ``sessionApprovalConsentPending``
    (``to_dict`` keeps every bool), so a record without the key was written by a
    host that ignored ``permissions.sessionApproval``. If the live manifest
    declares that grant, nobody ever consented to it. Key absence is only read
    in this fail-closed direction: it makes the grant pending, never approved.

    The member-thread scope is held to the same rule. A record that holds the
    grant's consent (the key present, not pending) covers
    ``permissions.memberThreads`` only when it also says
    ``memberThreadsConsented``, which an enable records only while the
    manifest declares the scope. Without it -- a record consented on a host that
    ignored the scope, or a manifest that gained the scope outside an update --
    the consent never covered the scope, so the grant reads pending again.
    """
    if isinstance(data, dict) and SESSION_APPROVAL_PENDING_KEY in data:
        if data.get(SESSION_APPROVAL_PENDING_KEY) is True:
            return False
        if data.get(MEMBER_THREADS_CONSENT_KEY) is True:
            return False
        if manifest is None:
            manifest = get_app_manifest(name)
        return bool(
            manifest and manifest.permissions.sessionApproval and manifest.permissions.memberThreads
        )
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


def _enabled_on_disk(meta_path: Path) -> bool:
    """Whether the record on disk reads enabled; an unreadable one reads as enabled.

    Used only to decide whether a write turns the app off, so the doubtful case
    records the disable rather than missing it.
    """
    if not os.path.lexists(meta_path):
        return False
    try:
        # No-follow, non-blocking, regular files only: the app can put a FIFO here.
        raw = member_records._read_regular_no_follow(meta_path)
        data = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (OSError, ValueError):
        return True
    return not isinstance(data, dict) or _read_enabled(data)


#: Largest ``installed.json`` the token checks read; a real record is a few KB.
_RECORD_READ_CAP = 256 * 1024


def _read_record_bytes(path: Path) -> bytes:
    """*path*'s bytes through :func:`open_regular_nofollow`, capped at :data:`_RECORD_READ_CAP`.

    The app can replace its own record with a link, a FIFO or a huge file; each raises
    ``OSError`` here, and absence raises ``FileNotFoundError``.
    """
    with open_regular_nofollow(path, max_bytes=_RECORD_READ_CAP) as fh:
        return fh.read()


def _next_token_cutoff(name: str, meta: InstalledApp) -> float:
    """The ``appTokensNotBefore`` a write of *meta* stores.

    Never earlier than what is on disk, so a writer holding a record read before a
    disable cannot reopen tokens the disable closed. A write that leaves the app
    disabled, or that creates the record, moves it to now.
    """
    meta_path = app_dir(name) / INSTALLED_META_FILENAME
    try:
        on_disk = _read_not_before(json.loads(_read_record_bytes(meta_path).decode("utf-8")))
        exists = True
    except FileNotFoundError:
        on_disk, exists = 0.0, False
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        on_disk, exists = 0.0, True
    cutoff = max(meta.appTokensNotBefore, on_disk)
    if not meta.enabled or not exists:
        cutoff = max(cutoff, time.time())
    return cutoff


def app_token_cutoff(name: str) -> float | None:
    """When *name*'s app tokens start being accepted, or None to refuse them all.

    None when the app is not installed, is disabled (its record says so, or the
    owner's disable stands in ``protected_app_state``), or its record cannot be
    read: an app token is honoured only while the app is on.
    Reads ``installed.json`` alone (no manifest), because the auth middleware
    calls it on every app-token request.
    """
    meta_path = app_dir(name) / INSTALLED_META_FILENAME
    try:
        data = json.loads(_read_record_bytes(meta_path).decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not _read_enabled(data):
        return None
    # The owner's disable stands outside the app's tree, which the app can rewrite.
    if protected_app_state.owner_disabled(name):
        return None
    return _read_not_before(data)


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

    ``approvedGrantsStored`` is kept set while the app's stored set exists, so a
    writer holding a record read before the set was stored cannot clear it. A
    write that turns an enabled record off first records the owner's disable in
    the protected store.

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
            appTokensNotBefore=_next_token_cutoff(name, meta),
            approvedGrantsStored=meta.approvedGrantsStored or _approved_grants_stored(name),
            sessionApprovalConsentPending=meta.sessionApprovalConsentPending or rule == "pending",
            source=_credential_free_source_metadata(str(meta.source or "")),
            sourceUrl=_strip_git_target_userinfo(str(meta.sourceUrl or "")),
            sourceRegistry=_credential_free_source_metadata(str(meta.sourceRegistry or "")),
        )
        body = credential_free_meta.to_dict()
        if rule == "unrecorded":
            body.pop(SESSION_APPROVAL_PENDING_KEY, None)
        meta_path = app_dir(name) / INSTALLED_META_FILENAME
        if not credential_free_meta.enabled and _enabled_on_disk(meta_path):
            # Recorded before the record says disabled, so no read in between
            # sees the app off on disk yet on in the protected store.
            protected_app_state.set_owner_disabled(name, True)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(meta_path, json.dumps(body, indent=2) + "\n")
        # The credential masks the manifest declares must outlive it beside the data
        # (apps.credential_masks). Best-effort here: the update and the uninstall
        # refresh the record again, strictly, before they move the data.
        try:
            credential_masks.remember_declaration(name)
        except OSError:
            logger.warning("could not record %s's credential masks", name, exc_info=True)
        # REVOKE-ONLY: drop any host-exec grant whenever a write leaves the app
        # disabled, so no disable path (disable, widen-until-consent, update) can leave
        # a stale grant that still starts a pod. Deliberately never GRANTS here: this
        # chokepoint runs on the boot re-registration of builtins, which reads back
        # ``installed.json`` -- a file a sandboxed shell can forge -- so minting from
        # ``enabled`` here would let a forged record plus a restart produce a real
        # grant. The grant is written only from the owner-authenticated ``enable_app``.
        # No-op when no grant exists. See ``apps.host_exec_grants``.
        if not credential_free_meta.enabled:
            revoke_host_exec(name)
            # Same revoke-only rule for the app's worker auto-approve grants: one
            # minted while a disable was in flight ends once the record says so.
            revoke_worker_auto_approve(name)


# Grant families held back on a widening update. ``sessionApproval`` is a flag
# with its own disable-until-consent path below; these are entry lists. The
# point families (``contributes.linkPreviews`` and the other points, registered in
# ``point_grants``) follow; a stored set from before one existed does not carry it, and a missing family approves what the manifest declares, as a missing
# file does for an install that never stored one.
STAGED_GRANT_FAMILIES = ("api", "events", *point_grant_families())
#: Families every stored set carries; the others are absent from an older file.
_ALWAYS_STORED_FAMILIES = ("api", "events")
#: Families an older file that lacks them approves nothing in (``point_grants``).
_EXPLICIT_ONLY_FAMILIES = explicit_only_grant_families()


def _parse_approved_grants(raw: Any) -> dict[str, list[str]]:
    """Read the parsed contents of an approved-grants file, failing closed.

    Anything but a mapping approves nothing, so a damaged file holds the app to
    none of its declared api/events entries rather than to all of them.

    An explicit-only family the file lacks reads as empty, so every reader and
    writer (an approval, an update, a re-registration) starts it from nothing.
    """
    record = raw if isinstance(raw, dict) else {}
    parsed: dict[str, list[str]] = {}
    for family in STAGED_GRANT_FAMILIES:
        if (
            family not in _ALWAYS_STORED_FAMILIES
            and family not in _EXPLICIT_ONLY_FAMILIES
            and isinstance(raw, dict)
            and family not in record
        ):
            # Written before this family existed: no decision recorded for it.
            continue
        entries = record.get(family)
        parsed[family] = (
            [e for e in entries if isinstance(e, str)] if isinstance(entries, list) else []
        )
    return parsed


def _approved_grants_path(name: str) -> Path:
    """*name*'s approved set, in the gateway-only store (:mod:`apps.protected_app_state`)."""
    return protected_app_state.grants_path(name)


def _approved_grants_stored(name: str) -> bool:
    try:
        return _approved_grants_path(name).is_file()
    except (OSError, ValueError):
        return False


#: The stored form of "approves what the manifest declares": an install from before
#: approved sets existed, or an external registration from before
#: :data:`_AWAITING_MANIFEST_KEY`. The first read fixes it (:func:`_freeze_declared`).
_APPROVE_DECLARED_KEY = "approveDeclared"
_APPROVE_DECLARED_TEXT = json.dumps({_APPROVE_DECLARED_KEY: True}) + "\n"
#: An external registration that sent no manifest: nothing is approved until the
#: owner's registration route sends the first one, which approves what it declares.
#: Never fixed from the app's own ``app.json``, which an agent can write.
_AWAITING_MANIFEST_KEY = "awaitingManifest"
_AWAITING_MANIFEST_TEXT = json.dumps({_AWAITING_MANIFEST_KEY: True}) + "\n"


def _import_app_tree_grants() -> None:
    """Carry every app's set from its own tree into the protected store, once.

    The app-tree copy (``apps/<name>/approved-grants.json``) is read here and never
    again. A record that never stored a set keeps approving what its manifest
    declares, as it did; one that stored a set and lost the file approves nothing.
    The marker written at the end closes the carry-over, so a file planted in an app
    tree afterwards is never read. Not inside a sandbox, where the store is masked.

    Every writer of the store runs this first, and the whole pass holds
    :func:`protected_app_state.grants_import_lock`, so an owner's approval is never
    overwritten by an importer that read the store before it.
    """
    if member_records._inside_sandbox() or protected_app_state.grants_imported():
        return
    with protected_app_state.grants_import_lock():
        if not protected_app_state.grants_imported():
            _import_app_tree_grants_locked()


def _disabled_before_upgrade(name: str, meta_path: Path) -> bool:
    """Whether the import must record *name* as owner-disabled.

    True for a record reading ``enabled: false``, and for one that cannot be read
    (fail closed: the owner enables it again). A built-in still owed its default-on
    promotion is left to ``backfill_default_on_builtins``, which turns it on.
    """
    try:
        raw = member_records._read_regular_no_follow(meta_path)
        data = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (OSError, ValueError, RecursionError):
        return True
    if not isinstance(data, dict):
        return True
    if _read_enabled(data):
        return False
    # Deferred: manager_builtins imports this module.
    from kiro_crew.apps.manager_builtins import _DEFAULT_ON_BACKFILL

    owed_promotion = (
        name in _DEFAULT_ON_BACKFILL
        and data.get("source") == "builtin"
        and data.get("defaultOnBackfilled") is not True
    )
    return not owed_promotion


def _import_app_tree_grants_locked() -> None:
    root = apps_dir()
    entries = sorted(root.iterdir()) if root.is_dir() else []
    for entry in entries:
        name = entry.name
        if _is_sidecar_entry(name) or not APP_NAME_RE.fullmatch(name) or not entry.is_dir():
            continue
        meta_path = entry / INSTALLED_META_FILENAME
        if not meta_path.is_file():
            continue
        if _disabled_before_upgrade(name, meta_path):
            # A disable from an older build lives only in this app-writable file.
            protected_app_state.set_owner_disabled(name, True)
        if protected_app_state.read_grants_text(name) is not None:
            continue
        # Both files are in the app's tree: read no-follow, non-blocking, regular
        # files only, so a FIFO or a link planted there cannot stall the import.
        legacy_path = entry / APPROVED_GRANTS_FILENAME
        # A set that is lost, unreadable or not a mapping approves nothing in EVERY
        # family: ``{}`` would leave the point families out, and a family left out
        # approves what the manifest declares.
        nothing: dict[str, list[str]] = {family: [] for family in STAGED_GRANT_FAMILIES}
        grants: dict[str, list[str]] | None
        if not os.path.lexists(legacy_path):
            grants = None
        else:
            try:
                raw = member_records._read_regular_no_follow(legacy_path)
                grants = (
                    _parse_approved_grants(json.loads(raw.decode("utf-8")))
                    if raw is not None
                    else nothing
                )
            except (OSError, ValueError, RecursionError):
                grants = nothing
        if grants is None:
            try:
                raw = member_records._read_regular_no_follow(meta_path)
                stored = (
                    json.loads(raw.decode("utf-8")).get("approvedGrantsStored") if raw else True
                )
            except (OSError, ValueError, AttributeError, RecursionError):
                stored = True
            if stored is not True:
                protected_app_state.write_grants_text_locked(name, _APPROVE_DECLARED_TEXT)
                continue
            grants = nothing
        protected_app_state.write_grants_text_locked(name, _approved_grants_text(grants))
    protected_app_state.mark_grants_imported()


def _read_approved_grants(name: str) -> dict[str, list[str]] | None:
    """Read *name*'s approved set from the protected store, failing closed.

    Always a set with every family. A missing, unreadable or unparsable set approves
    nothing. A set stored as "approve what is declared", or one written before a
    family existed, is fixed once to what the manifest declares at this first read
    (:func:`_freeze_declared`), so a later write to the agent-writable manifest
    widens nothing.
    """
    nothing: dict[str, list[str]] = {family: [] for family in STAGED_GRANT_FAMILIES}
    try:
        _import_app_tree_grants()
        text = protected_app_state.read_grants_text(name)
    except (OSError, UnicodeDecodeError, ValueError):
        # A constant message: the name and the error text derive from stored data.
        logger.warning("Failed to read an app's approved set; it approves nothing")
        return nothing
    if text is None:
        # No set in the store: nothing is approved. The app's own tree is never
        # read, so a file written there grants nothing.
        return nothing
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return nothing
    if isinstance(raw, dict) and raw.get(_AWAITING_MANIFEST_KEY) is True:
        return nothing
    parsed = (
        None
        if isinstance(raw, dict) and raw.get(_APPROVE_DECLARED_KEY) is True
        else _parse_approved_grants(raw)
    )
    if parsed is not None and all(family in parsed for family in STAGED_GRANT_FAMILIES):
        return parsed
    return _freeze_declared(name, text, parsed)


def _freeze_declared(
    name: str, text: str, parsed: dict[str, list[str]] | None
) -> dict[str, list[str]]:
    """Store, once, what a set that defers to the manifest approves right now.

    *parsed* is ``None`` for "approve what is declared" (an install from before
    approved sets, carried over by the import); otherwise it lacks the families it
    was written before. Each such family takes what the manifest declares at this
    read, which is what it granted until now, and the result is stored, so a later
    write to the agent-writable ``app.json`` is held back like any other widening.
    Built-ins never get here: they store their shipped set at every start.

    Nothing is stored inside a sandbox, during an update's tree swap, without a
    readable manifest, or when the stored set changed since *text* was read; each
    of those reads approves only what the set itself records.
    """
    base = dict(parsed or {})
    recorded = {family: list(base.get(family, ())) for family in STAGED_GRANT_FAMILIES}
    if member_records._inside_sandbox() or _tree_swap_state(name)[0]:
        return recorded
    manifest = get_app_manifest(name)
    if manifest is None:
        return recorded
    declared = _declared_grant_entries(grant_source(manifest))
    frozen = {
        family: base[family] if family in base else declared[family]
        for family in STAGED_GRANT_FAMILIES
    }
    try:
        # The app's write lock, which every write of its set holds (``write_grants_text``),
        # in any thread or process: nothing an owner stores, and no uninstall's
        # removal, can land between this comparison and this write.
        with protected_app_state.grants_write_lock(name):
            if protected_app_state.read_grants_text(name) != text:
                return recorded
            protected_app_state.write_grants_text_locked(name, _approved_grants_text(frozen))
    except (OSError, ValueError):
        logger.warning("Could not store an app's approved set; it approves what it records")
        return recorded
    return frozen


def _effective_approved_grants(name: str, meta: InstalledApp | None) -> dict[str, list[str]] | None:
    """*name*'s approved set as *meta* (its install record) qualifies it.

    A missing file approves what the manifest declares (``None``) only for a
    record that has never stored a set. Once one was stored, a missing file
    approves nothing: it is a file an update's tree swap has not put back yet,
    or one that was lost, and neither is the owner's approval.
    """
    del meta  # the store alone decides; the record's flag is app-writable
    return _read_approved_grants(name)


def _approved_grants_text(grants: dict[str, list[str]]) -> str:
    body = {
        family: list(grants.get(family, ()))
        for family in STAGED_GRANT_FAMILIES
        if family in _ALWAYS_STORED_FAMILIES or family in grants
    }
    return json.dumps(body, indent=2) + "\n"


def _write_approved_grants(name: str, grants: dict[str, list[str]] | None) -> None:
    """Atomically store *name*'s approved set; ``None`` removes it (nothing approved).

    Only the grant-owning paths call this: install, update, registration and
    an owner approval. Every other record write leaves the set alone because it
    never touches this file.
    """
    _import_app_tree_grants()
    protected_app_state.write_grants_text(
        name, None if grants is None else _approved_grants_text(grants)
    )


def _write_approve_declared(name: str) -> None:
    """Store "approves what the manifest declares" for *name* (the pre-set form)."""
    _import_app_tree_grants()
    protected_app_state.write_grants_text(name, _APPROVE_DECLARED_TEXT)


def _write_awaiting_manifest(name: str) -> None:
    """Store that *name* was registered without a manifest (:data:`_AWAITING_MANIFEST_KEY`)."""
    _import_app_tree_grants()
    protected_app_state.write_grants_text(name, _AWAITING_MANIFEST_TEXT)


def _approved_grants_snapshot(name: str) -> str | None:
    """The stored set's text, for a writer that may have to put it back."""
    _import_app_tree_grants()
    return protected_app_state.read_grants_text(name)


def _restore_approved_grants(name: str, snapshot: str | None) -> None:
    _import_app_tree_grants()
    protected_app_state.write_grants_text(name, snapshot)


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
        # An explicit-only family is never absent here (``_parse_approved_grants``).
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
                token.touch(mode=0o600, exist_ok=False)
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
            if _read_installed(name) != meta:
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
        if family not in permissions or family in WORKER_AUTO_APPROVE_GRANT_FAMILIES:
            # ``permissions.workerAutoApprove`` is a flag, not an entry list: it stays
            # as declared, because every grant re-reads the owner's approval itself
            # (``worker_auto_approve.app_holds_worker_auto_approve``).
            continue

        def _read(family: str = family) -> list[str] | None:
            live = get_app_manifest(name)
            if live is None:
                return None
            return [e for e in getattr(live.permissions, family) if e]

        permissions[family] = staged_app_grants(name, family, _read)
    return permissions


class SessionControlWidening(NamedTuple):
    """What a manifest newly asks of the owner's user-session consent.

    ``keys`` names the manifest keys added, for the SEL row; ``words`` says the
    same in plain words, for the operator-facing message. Both say exactly what
    the owner has not consented to yet, so a row never reports the grant itself
    as added when it was already held and only its member-thread scope is new.
    """

    keys: str
    words: str


_GRANT_WIDENING = SessionControlWidening(
    keys="permissions.sessionApproval",
    words="session approval control",
)
_GRANT_AND_SCOPE_WIDENING = SessionControlWidening(
    keys="permissions.sessionApproval and permissions.memberThreads",
    words="session approval control, including your chats with your Bots",
)
_SCOPE_WIDENING = SessionControlWidening(
    keys="permissions.memberThreads",
    words="session approval control over your chats with your Bots",
)


def _session_control_widening(
    prior: Permissions | None, requested: Permissions
) -> SessionControlWidening | None:
    """What user-session control *requested* adds that the owner has not consented to.

    Consent is captured when the owner enables the app, against the manifest of
    that moment, so any later manifest that reaches further must go back through
    it: the grant itself (``sessionApproval``) newly declared, or its
    member-thread scope (``memberThreads``) newly declared on a held grant.
    *prior* is the manifest consented to, ``None`` for a first install or
    registration. A scope flag without the grant widens nothing, because it
    grants nothing; the grant arriving later is judged then, scope included.
    ``None`` when nothing is added.
    """
    if not requested.sessionApproval:
        return None
    if prior is None or not prior.sessionApproval:
        return _GRANT_AND_SCOPE_WIDENING if requested.memberThreads else _GRANT_WIDENING
    if requested.memberThreads and not prior.memberThreads:
        return _SCOPE_WIDENING
    return None


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
        if _is_sidecar_entry(entry.name) or not entry.is_dir():
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
