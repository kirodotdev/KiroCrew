# SPDX-License-Identifier: Apache-2.0
"""Approvals the owner took away from a crewmate's worker, kept beside its file.

The worker base wins for its own approvals (``mate_worker.merge_worker_spec``),
so a removed one is recorded in ``~/.kiro/mates/revoked-approvals.json``
(``{"<file id>": ["@server/tool", ...]}``) and :func:`strip` takes it back out.
An unreadable record raises :class:`RevocationsUnreadable`, never reads empty.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The capability sections whose rows are approvals.
APPROVAL_SECTIONS = ("allowedTools", "autoApprove")


def _path() -> Path:
    from kiro_crew.config import paths as paths_mod

    return paths_mod.mate_agents_dir().parent / "revoked-approvals.json"


class RevocationsUnreadable(Exception):
    """The revoked-approvals record exists but cannot be read as one."""


def _read() -> dict[str, list[str]]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("revoked-approvals.json is unreadable", exc_info=True)
        raise RevocationsUnreadable("revoked-approvals.json is unreadable") from exc
    if not isinstance(raw, dict):
        raise RevocationsUnreadable("revoked-approvals.json is not an object")
    return {
        k: [r for r in v if isinstance(r, str)]
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, list)
    }


def revoked(file_id: str) -> list[str]:
    """The approvals revoked for crewmate file *file_id*, ``[]`` when none. Blocking.

    Raises :class:`RevocationsUnreadable` when the record exists but cannot be read.
    """
    return list(_read().get(file_id, [])) if file_id else []


def record(file_id: str, removed: list[str], added: list[str]) -> None:
    """Note *removed* approvals as revoked and *added* ones as granted again. Blocking.

    Raises :class:`RevocationsUnreadable` rather than overwrite a record it cannot read.
    """
    if not file_id or not (removed or added):
        return
    from kiro_crew.atomic_write import atomic_write

    data = _read()
    current = [r for r in data.get(file_id, []) if r not in set(added)]
    current = list(dict.fromkeys([*current, *removed]))
    if current:
        data[file_id] = current
    else:
        data.pop(file_id, None)
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n", restrict_to_owner=True)


def changed_approvals(
    old_rows: dict[str, dict[str, Any]], new_rows: dict[str, dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """The approvals an edit removes and adds, from the editor's section rows."""
    removed: list[str] = []
    added: list[str] = []
    for section in APPROVAL_SECTIONS:
        before = set(old_rows.get(section, {}))
        after = set(new_rows.get(section, {}))
        removed += sorted(before - after)
        added += sorted(after - before)
    return removed, added


def strip(spec: dict[str, Any], refs: list[str]) -> dict[str, Any]:
    """*spec* with every approval in *refs* taken out of ``allowedTools`` and ``autoApprove``."""
    if not refs:
        return spec
    gone = set(refs)
    allowed = spec.get("allowedTools")
    if isinstance(allowed, list):
        spec["allowedTools"] = [r for r in allowed if r not in gone]
    servers = spec.get("mcpServers")
    if isinstance(servers, dict):
        for name, entry in servers.items():
            approve = entry.get("autoApprove") if isinstance(entry, dict) else None
            if isinstance(approve, list):
                entry["autoApprove"] = [t for t in approve if f"@{name}/{t}" not in gone]
    return spec
