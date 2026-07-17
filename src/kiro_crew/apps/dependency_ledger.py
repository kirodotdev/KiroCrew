"""Dependency ledger — reference-counted tracking for app dependencies.

Tracks which apps installed which external dependencies (AIM MCP servers,
skills, agents) so that uninstall can safely clean up dependencies that
are no longer referenced by any app.

All reads/writes use ``fcntl.flock()`` for concurrency safety, consistent
with KiroCrew's existing file locking patterns.  Read-modify-write cycles
hold a single exclusive lock across the entire operation to prevent lost
updates.

Storage: ``~/.kirocrew/dependency-ledger.json``
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


@dataclass
class LedgerEntry:
    """A single dependency tracked in the ledger."""

    installedBy: list[str] = field(default_factory=list)  # noqa: N815
    installedAt: str = ""  # noqa: N815
    type: str = ""  # "aim.mcp" | "aim.skills" | "aim.agents"

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
    """Read the ledger file without acquiring a lock (caller must hold lock)."""
    path = _ledger_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read dependency ledger: %s", exc)
        return {}


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
        ledger = _read_ledger_unlocked()
        entry = ledger.get(dep_key)
        if entry:
            installed_by = entry.get("installedBy", [])
            if app_name not in installed_by:
                installed_by.append(app_name)
                entry["installedBy"] = installed_by
        else:
            ledger[dep_key] = {
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
        ledger = _read_ledger_unlocked()
        entry = ledger.get(dep_key)
        if not entry:
            return
        installed_by = entry.get("installedBy", [])
        if app_name in installed_by:
            installed_by.remove(app_name)
        if not installed_by:
            del ledger[dep_key]
        else:
            entry["installedBy"] = installed_by
        _write_ledger_unlocked(ledger)
    logger.debug("Ledger: recorded %s uninstall of %s", app_name, dep_key)


def get_entry(dep_key: str) -> LedgerEntry | None:
    """Get a single dependency's ledger entry, or None."""
    ledger = _read_ledger()
    raw = ledger.get(dep_key)
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

    - Removable deps not in *keep_specific* have their ledger entry deleted.
    - Shared deps have the current app removed from ``installedBy``.
    - User-installed deps are untouched.

    Returns the same classification dict as :func:`classify_for_uninstall`.
    """
    keep = set(keep_specific or [])
    with _locked_ledger():
        ledger = _read_ledger_unlocked()
        result = _classify_deps(app_name, declared_deps, ledger)

        # Update ledger for removable deps
        for dep in result["removable"]:
            dep_key = dep["id"]
            if dep_key in keep:
                # User chose to keep this dep — still remove the app's
                # ownership so the dep is classified as "user installed"
                # in future uninstalls (no orphaned reference).
                entry = ledger.get(dep_key)
                if entry:
                    installed_by = entry.get("installedBy", [])
                    if app_name in installed_by:
                        installed_by.remove(app_name)
                    if not installed_by:
                        del ledger[dep_key]
                    else:
                        entry["installedBy"] = installed_by
                continue
            ledger.pop(dep_key, None)

        # Update ledger for shared deps (remove this app's reference)
        for dep in result["shared"]:
            dep_key = dep["id"]
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
        raw = ledger.get(dep_key)
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
