"""Dependency ledger — reference-counted tracking for app dependencies.

Tracks which apps installed which external dependencies (capability-manager
MCP servers, skills, agents) so that uninstall can safely clean up dependencies
that are no longer referenced by any app.

All reads/writes use ``fcntl.flock()`` for concurrency safety, consistent
with KiroCrew's existing file locking patterns.  Read-modify-write cycles
hold a single exclusive lock across the entire operation to prevent lost
updates.

Storage: ``~/.kiro/crew/dependency-ledger.json``
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)


def _ledger_path() -> Path:
    return config_dir() / "dependency-ledger.json"


#: Key namespace for capability-manager-provided dependencies.
CAPABILITY_KEY_PREFIX = "capability"

#: Pre-rename namespace, still read so ledgers written by older builds resolve.
_LEGACY_KEY_PREFIX = "aim"

#: Every capability dependency type a manifest may declare, in emit order.
#: Single source of truth — ``dependencies.py`` derives its installable subset
#: from this rather than re-spelling the tuple.
CAPABILITY_DEP_TYPES = ("mcp", "skills", "agents")

#: Types the ``CapabilityManager`` seam exposes NO install/uninstall op for, so
#: KiroCrew can neither install nor clean them up. Their ledger rows must SURVIVE
#: an uninstall: deleting the row for something we cannot actually remove would
#: leave the installed package on disk with no record that anything referenced it
#: (an untraceable orphan). Ownership is dropped instead, so the dep reads as
#: user-installed and a future uninstall op — or the user — can still reclaim it.
CAPABILITY_UNCLEANABLE_TYPES = ("agents",)


def _is_uncleanable(dep_type: str) -> bool:
    """True when *dep_type* has no cleanup operation, so its ledger row must be
    preserved. Accepts a bare type (``agents``) or any prefixed spelling,
    canonical or legacy, since ledger rows carry whichever was written."""
    base = dep_type.split(".")[-1] if "." in dep_type else dep_type
    return base in CAPABILITY_UNCLEANABLE_TYPES


def capability_dep_key(dep_type: str, dep_id: str) -> str:
    """Ledger key for a capability dependency (``capability/<type>/<id>``)."""
    return f"{CAPABILITY_KEY_PREFIX}/{dep_type}/{dep_id}"


def capability_dep_type(dep_type: str) -> str:
    """Ledger ``type`` value for a capability dependency (``capability.<type>``)."""
    return f"{CAPABILITY_KEY_PREFIX}.{dep_type}"


def canonical_dep_key(dep_key: str) -> str:
    """Normalize a caller-supplied dependency key to the canonical prefix.

    Client-supplied keys (an uninstall request's ``keep_specific``) may carry the
    pre-rename prefix: a dashboard session that loaded its uninstall preview from
    an OLDER build echoes those ids straight back. Classification emits canonical
    ids, so an un-normalized legacy key would silently miss the ``keep`` membership
    test and delete a dependency the user explicitly chose to keep. Idempotent for
    already-canonical keys, and leaves unrelated strings untouched.

    Non-string input is returned unchanged rather than raising: callers sanitize
    at their own boundary, but this is consumed mid-uninstall (after the
    onUninstall script and deregistration have run), so raising here would leave
    an app partially uninstalled instead of failing cleanly.
    """
    if not isinstance(dep_key, str):
        return dep_key
    prefix = f"{_LEGACY_KEY_PREFIX}/"
    if dep_key.startswith(prefix):
        return f"{CAPABILITY_KEY_PREFIX}/{dep_key[len(prefix):]}"
    return dep_key


def declared_capability_keys(deps_data: dict[str, Any]) -> list[str]:
    """Ledger keys for every capability dependency declared in a raw manifest dict.

    Reads the ``capabilities`` wire key, falling back to the deprecated ``aim``
    alias (see :class:`kiro_crew.apps.manifest.Dependencies`).  Shared by the
    uninstall-preview and uninstall paths so both derive identical keys.
    """
    raw = deps_data.get("capabilities")
    if not isinstance(raw, dict):
        raw = deps_data.get("aim")
    if not isinstance(raw, dict):
        return []
    keys: list[str] = []
    for dep_type in CAPABILITY_DEP_TYPES:
        entries = raw.get(dep_type)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            dep_id = entry.get("id") if isinstance(entry, dict) else entry
            if not dep_id:
                continue
            keys.append(capability_dep_key(dep_type, str(dep_id)))
    return keys


def _legacy_key(dep_key: str) -> str | None:
    """The pre-rename spelling of *dep_key*, or ``None`` if it has none.

    Lets a ledger written by an older build keep resolving after the rename
    without a migration pass: lookups fall back to the legacy key, and writes
    reuse whichever spelling already exists.
    """
    if dep_key.startswith(f"{CAPABILITY_KEY_PREFIX}/"):
        return f"{_LEGACY_KEY_PREFIX}/{dep_key[len(CAPABILITY_KEY_PREFIX) + 1:]}"
    return None


def _resolve_key(ledger: dict[str, Any], dep_key: str) -> str:
    """Return the key *dep_key* is actually stored under in *ledger*.

    Prefers the canonical key; falls back to the legacy spelling only when the
    canonical one is absent and the legacy one is present.
    """
    if dep_key in ledger:
        return dep_key
    legacy = _legacy_key(dep_key)
    if legacy is not None and legacy in ledger:
        return legacy
    return dep_key


@dataclass
class LedgerEntry:
    """A single dependency tracked in the ledger."""

    installedBy: list[str] = field(default_factory=list)  # noqa: N815
    installedAt: str = ""  # noqa: N815
    #: ``"capability.mcp"`` | ``"capability.skills"`` | ``"capability.agents"``
    #: (legacy ledgers may carry the pre-rename ``"aim.*"`` spelling).
    type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "installedBy": self.installedBy,
            "installedAt": self.installedAt,
            "type": self.type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEntry:
        return cls(
            installedBy=list(data.get("installedBy", [])),
            installedAt=str(data.get("installedAt", "")),
            type=str(data.get("type", "")),
        )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextmanager
def _locked_ledger(*, exclusive: bool = True) -> Iterator[None]:
    """Acquire a lock on the ledger for the duration of the block.

    Uses the same ``.lock`` sidecar file for both shared and exclusive
    locks so that readers and writers coordinate properly.
    """
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    lock_path.touch(exist_ok=True)
    # "r+" (not "r"): Windows msvcrt.locking requires write access on the fd —
    # a read-only handle fails with EACCES and platform_compat.file_lock
    # swallows it (best-effort), silently degrading this to a no-op.
    with open(lock_path, "r+") as lf:
        with platform_compat.file_lock(lf.fileno(), exclusive=exclusive):
            yield


def _read_ledger_unlocked() -> dict[str, Any]:
    """Read the ledger file without acquiring a lock (caller must hold lock).

    A DISPLAY read: every failure degrades to an empty ledger so a render or a
    lookup never crashes on a document it could not load. See
    :func:`_read_ledger_for_update` for why a mutation may not stand on the
    same answer. An absent file is silent -- that is a ledger with no
    dependencies yet, not a fault; anything else is logged, because the state
    this degrades into looks exactly like health: an empty ledger reads as
    "nothing is tracked", which for the only record that an installed
    dependency is still referenced is the one wrong answer nobody would
    question.
    """
    path = _ledger_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # The file vanished between the is_file() check and the read.
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to read dependency ledger: %s", exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("Dependency ledger root is not a JSON object; treating as empty")
        return {}
    return data


def _read_ledger_for_update() -> dict[str, Any]:
    """The ledger document a read-modify-write is allowed to publish over.

    :func:`_read_ledger_unlocked` is a DISPLAY read: every failure collapses
    to ``{}`` so a render never crashes on a document it could not load. That
    reading is wrong as the base of a mutation, because all three mutations
    below rewrite the WHOLE ledger from what they read -- an empty base there
    does not mean "nothing to carry forward", it means "forget every app's
    refcount". :func:`record_install` would publish a one-entry ledger over
    every other app's references, and
    :func:`classify_and_clean_for_uninstall` would wipe the ledger outright.
    The sidecar lock does not help: it serializes writers, and the loss
    happens inside it.

    Only a MISSING file is a failure where ``{}`` is the truth (nothing has
    been tracked yet). An unreadable one -- a transient EACCES/EIO, a scanner
    holding the handle on Windows -- is a ledger we still have, so the error
    propagates and the mutation is abandoned rather than published over state
    nobody read.

    Corruption propagates too (#7805, mirroring #8084): a document that failed
    to parse carries nothing to merge into, but "cannot merge into" is not
    "safe to destroy" -- a truncated file still holds most of its refcounts
    verbatim. ``UnicodeDecodeError`` is folded into the same refusal: it is a
    ``ValueError`` but NOT a ``json.JSONDecodeError``, so left unwrapped it
    would slip past every corruption clause at the callers. Same for a root
    that parses but is not an object: it carries no refcounts to merge into,
    and normalizing it to ``{}`` would destroy a document nobody could read.

    Plain ``json.JSONDecodeError`` rather than a named error type: no caller
    of the three mutations needs to distinguish corruption from a transient
    read failure -- both refuse -- and no cross-app import is wanted.
    """
    path = _ledger_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # The file vanished between the is_file() check and the read.
        return {}
    except UnicodeDecodeError as exc:
        raise json.JSONDecodeError(
            f"dependency ledger is not valid UTF-8: {exc.reason}",
            exc.object.decode("utf-8", "replace")[:120],
            0,
        ) from exc
    if not isinstance(data, dict):
        raise json.JSONDecodeError(
            "dependency ledger root is not a JSON object", str(data)[:120], 0
        )
    return data


def require_readable_ledger() -> None:
    """Refuse loudly when the ledger cannot be read for a mutation.

    A pre-flight for flows with IRREVERSIBLE steps around the mutation: the
    uninstall handler tears the app down (onUninstall script, backend stop,
    resource deregistration) before :func:`classify_and_clean_for_uninstall`
    reads the ledger, so letting that strict read refuse mid-flow would strand
    a half-uninstalled app, and a retry would rerun the non-idempotent
    teardown. Reading here -- before anything destructive -- costs a handled
    refusal instead. Raises whatever :func:`_read_ledger_for_update` raises
    (``OSError`` for a transient read failure, ``json.JSONDecodeError`` for a
    corrupt one); the mutation still re-checks under the lock, so a ledger
    that goes bad between this call and the mutation refuses rather than
    publishes.
    """
    _read_ledger_for_update()


def _write_ledger_unlocked(data: dict[str, Any]) -> None:
    """Write the ledger file without acquiring a lock (caller must hold lock)."""
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(data, indent=2) + "\n")


def _read_ledger() -> dict[str, Any]:
    """Read the ledger file with a shared lock."""
    with _locked_ledger(exclusive=False):
        return _read_ledger_unlocked()


def record_install(dep_key: str, app_name: str, dep_type: str) -> None:
    """Record that an app installed a dependency.

    If the dependency is already in the ledger (installed by another app),
    appends the current app to ``installedBy`` (no duplicates).
    """
    with _locked_ledger():
        ledger = _read_ledger_for_update()
        # Reuse the legacy spelling when that is where the entry already lives,
        # so an older ledger accrues refcounts in one place instead of splitting
        # across two keys for the same dependency.
        key = _resolve_key(ledger, dep_key)
        entry = ledger.get(key)
        if entry:
            installed_by = entry.get("installedBy", [])
            if app_name not in installed_by:
                installed_by.append(app_name)
                entry["installedBy"] = installed_by
        else:
            ledger[key] = {
                "installedBy": [app_name],
                "installedAt": _now_iso(),
                "type": dep_type,
            }
        _write_ledger_unlocked(ledger)
    logger.debug("Ledger: recorded %s install of %s", app_name, dep_key)


def record_uninstall(dep_key: str, app_name: str) -> None:
    """Remove an app's reference to a dependency.

    If the ``installedBy`` list becomes empty, the entry is deleted entirely.
    """
    with _locked_ledger():
        ledger = _read_ledger_for_update()
        key = _resolve_key(ledger, dep_key)
        entry = ledger.get(key)
        if not entry:
            return
        installed_by = entry.get("installedBy", [])
        if app_name in installed_by:
            installed_by.remove(app_name)
        if not installed_by:
            del ledger[key]
        else:
            entry["installedBy"] = installed_by
        _write_ledger_unlocked(ledger)
    logger.debug("Ledger: recorded %s uninstall of %s", app_name, dep_key)


def get_entry(dep_key: str) -> LedgerEntry | None:
    """Get a single dependency's ledger entry, or None.

    Falls back to the pre-rename key spelling so entries written by an older
    build remain visible.
    """
    ledger = _read_ledger()
    raw = ledger.get(_resolve_key(ledger, dep_key))
    if not raw:
        return None
    return LedgerEntry.from_dict(raw)


def list_by_app(app_name: str) -> list[tuple[str, LedgerEntry]]:
    """List all dependencies installed by a specific app."""
    ledger = _read_ledger()
    result: list[tuple[str, LedgerEntry]] = []
    for key, raw in ledger.items():
        entry = LedgerEntry.from_dict(raw)
        if app_name in entry.installedBy:
            result.append((key, entry))
    return result


def classify_for_uninstall(
    app_name: str,
    declared_deps: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Classify dependencies for uninstall preview (read-only).

    Used by the preview endpoint to show the user what will happen.
    Uses a shared lock — safe for read-only display purposes.

    For the actual uninstall, use :func:`classify_and_clean_for_uninstall`
    which holds an exclusive lock across classify + ledger update.
    """
    ledger = _read_ledger()
    return _classify_deps(app_name, declared_deps, ledger)


def classify_and_clean_for_uninstall(
    app_name: str,
    declared_deps: list[str],
    keep_specific: list[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Classify dependencies and update the ledger atomically.

    Holds an exclusive lock across the entire read → classify → write
    cycle to prevent TOCTOU races when two apps sharing a dependency
    are uninstalled concurrently.

    - Removable deps not in *keep_specific* have their ledger entry deleted,
      EXCEPT uncleanable types (see :data:`CAPABILITY_UNCLEANABLE_TYPES`), which
      only lose this app's ownership — dropping the row for a dependency no
      cleanup op can remove would orphan the installed package untraceably.
    - Shared deps have the current app removed from ``installedBy``.
    - User-installed deps are untouched.

    Returns the same classification dict as :func:`classify_for_uninstall`.
    """
    keep = set(keep_specific or [])
    with _locked_ledger():
        ledger = _read_ledger_for_update()
        result = _classify_deps(app_name, declared_deps, ledger)

        # Update ledger for removable deps
        for dep in result["removable"]:
            dep_key = _resolve_key(ledger, dep["id"])
            uncleanable = _is_uncleanable(str(dep.get("type", "")))
            if dep["id"] in keep or uncleanable:
                # Either the user chose to keep this dep, or nothing can clean it
                # up. Drop this app's ownership so it classifies as
                # "user installed" next time (no orphaned reference).
                entry = ledger.get(dep_key)
                if entry:
                    installed_by = entry.get("installedBy", [])
                    if app_name in installed_by:
                        installed_by.remove(app_name)
                    entry["installedBy"] = installed_by
                    # An emptied row is normally pruned — but for an UNCLEANABLE
                    # type the row is the only remaining record that the package
                    # was installed at all (KiroCrew has no operation to remove
                    # it), so retain it ownerless instead of orphaning the package.
                    if not installed_by and not uncleanable:
                        del ledger[dep_key]
                continue
            ledger.pop(dep_key, None)

        # Update ledger for shared deps (remove this app's reference)
        for dep in result["shared"]:
            dep_key = _resolve_key(ledger, dep["id"])
            entry = ledger.get(dep_key)
            if entry:
                installed_by = entry.get("installedBy", [])
                if app_name in installed_by:
                    installed_by.remove(app_name)
                if not installed_by:
                    del ledger[dep_key]
                else:
                    entry["installedBy"] = installed_by

        _write_ledger_unlocked(ledger)
    return result


def _classify_deps(
    app_name: str,
    declared_deps: list[str],
    ledger: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Pure classification logic (no I/O, no locking)."""
    removable: list[dict[str, Any]] = []
    shared: list[dict[str, Any]] = []
    user_installed: list[dict[str, Any]] = []

    for dep_key in declared_deps:
        raw = ledger.get(_resolve_key(ledger, dep_key))
        if not raw:
            user_installed.append({
                "id": dep_key,
                "type": "",
                "reason": "Installed by user (not tracked)",
            })
            continue
        entry = LedgerEntry.from_dict(raw)
        if app_name not in entry.installedBy:
            user_installed.append({
                "id": dep_key,
                "type": entry.type,
                "reason": "Not installed by this app",
            })
            continue
        others = [a for a in entry.installedBy if a != app_name]
        if others:
            shared.append({
                "id": dep_key,
                "type": entry.type,
                "usedBy": others,
                "reason": f"Also used by {', '.join(others)}",
            })
        else:
            removable.append({
                "id": dep_key,
                "type": entry.type,
                "reason": "Only used by this app",
            })

    return {
        "removable": removable,
        "shared": shared,
        "userInstalled": user_installed,
    }
