"""Skills Kiro cloud syncs to this machine: the organization's and the user's own.

KAS (Kiro's agent engine) pulls a signed-in account's CloudConfig into a local
replica at ``<kiro home>/cloud-cache/resolved/config`` and loads
``<layer>/skills/<name>/SKILL.md`` from it into every session, beside
``~/.kiro/skills``. Measured on KAS 3.3.0: the ``organization`` layer is read only
while ``state.json`` lists it under ``layers``, the ``user`` layer always.

Crew reads these trees for two things only: the Skills page lists them (read-only),
and a KAS session's skill scope keeps them in scope where it keeps ``~/.kiro/skills``
(:mod:`kiro_crew.acp.kas_skill_scope`). Crew never loads their bodies into a prompt
and never hands their paths to an agent: KAS loads them, and enforces the
organization's ``skill`` policy on them itself (RFC ``rfc-crew-lts-edition`` D8).
Only ``<layer>/skills/*/SKILL.md`` is read; the replica's other config types (MCP
servers, hooks, agents, steering) are never touched here.

A replica KAS is writing (its sync lock is held) or whose ``state.json`` cannot be
read answers "no skills", never an error: the tree may be half-written.

shortcut: the replica layout is KAS's undocumented on-disk format, read as KAS 3.3.0
writes it; replace with a CloudConfig read API when Kiro publishes one.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: ``(replica layer, Skills page source)``, in the order the page lists them.
REPLICA_LAYERS = (("organization", "kiro-org"), ("user", "kiro-cloud"))
#: The Skills page key prefixes of the replica's skills; read-only, like ``kiro-user/``.
REPLICA_KEY_PREFIXES = tuple(f"{source}/" for _layer, source in REPLICA_LAYERS)

#: KAS holds its sync lock as a ``.sync.lock.lock`` directory (proper-lockfile) and
#: refreshes its mtime while it holds it; a lock older than this is a dead one,
#: which proper-lockfile itself treats as stale.
_STALE_LOCK_SECS = 10.0

#: The largest ``state.json`` read; a larger one is treated as unreadable.
_MAX_STATE_BYTES = 32 * 1024 * 1024


def replica_dir() -> Path:
    """The replica's root, under :func:`kiro_crew.config.paths.kiro_home`."""
    from kiro_crew.config import paths

    return paths.kiro_home() / "cloud-cache" / "resolved" / "config"


def _sync_in_progress(root: Path) -> bool:
    """Whether KAS holds the replica's sync lock now."""
    try:
        st = (root / ".sync.lock.lock").stat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return time.time() - st.st_mtime < _STALE_LOCK_SECS


def _listed_layers(root: Path) -> set[str] | None:
    """The layer scopes ``state.json`` lists, or ``None`` when it cannot be read."""
    from kiro_crew.bounded_read import bounded_read

    try:
        with (root / "state.json").open("rb") as f:
            raw = bounded_read(f, _MAX_STATE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_STATE_BYTES:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    layers = data.get("layers")
    if not isinstance(layers, list):
        return set()
    return {
        layer["scope"]
        for layer in layers
        if isinstance(layer, dict) and isinstance(layer.get("scope"), str)
    }


def replica_skill_roots() -> list[tuple[str, Path]]:
    """``(source, skills tree)`` for each replica skills tree KAS loads now.

    ``[]`` when there is no replica, KAS is syncing it, or ``state.json`` is
    unreadable.
    """
    from kiro_crew.security import is_sensitive_path

    root = replica_dir()
    listed = None
    try:
        if (root / "state.json").is_file() and not _sync_in_progress(root):
            listed = _listed_layers(root)
        if listed is None:
            return []
        out: list[tuple[str, Path]] = []
        for layer, source in REPLICA_LAYERS:
            if layer != "user" and layer not in listed:
                continue
            tree = root / layer / "skills"
            if tree.is_dir() and not is_sensitive_path(str(tree)):
                out.append((source, tree))
    except OSError:  # a replica this user cannot search
        logger.debug("Kiro cloud replica is unreadable; listing no cloud skills", exc_info=True)
        return []
    return out


def replica_skill_globs() -> list[str]:
    """The ``SKILL.md`` globs of :func:`replica_skill_roots`."""
    return [str(tree / "*" / "SKILL.md") for _source, tree in replica_skill_roots()]
