"""An edition's own featured sections, read from its pinned registry.

WHAT THIS IS. The official ``editorial.json`` can only feature apps in the official
catalog. An edition that pins its own registry (``AppsLoader.default_registries``)
may also want to feature the apps that registry lists. A pinned row that carries
``"editorial": true`` opts its registry in: when that registry's index clone holds
an ``editorial.json`` beside ``app-registry.json``, the document is kept with the
index and its sections are shown ABOVE the official ones.

The same two-owner rule as default installs (``default_installs``): the build opts
a registry in, the registry decides what to feature. An operator-configured
registry can never opt in, and the registry must be in force at the ``owner``
tier, whose index is under change control the edition owns.

The document is the official editorial's shape, read by the same projector
(``official_editorial.project_sections``) with ONE difference: artwork refs are
dropped, because they resolve against the official catalog's origin. A section
from here is drawn with the featured app's own store art instead
(``useAppArt``).

WHAT THIS DOES NOT DO. The document is read from the index clone's working tree
and kept in an on-disk cache beside the index cache, which is agent-writable like
every cache under the data home. That is acceptable for presentation: the worst a
local write achieves is a re-cut featured list, and the dashboard resolves every
``appRef`` against the apps it can already show, so a section can never conjure
an app. Nothing here installs, trusts or runs anything.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.apps.official_editorial import MAX_BYTES, project_sections, supported_version
from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry

logger = logging.getLogger(__name__)

#: The build-row key that opts a pinned registry in.
EDITORIAL_KEY = "editorial"
#: The file read from the registry clone's root.
EDITORIAL_FILE = "editorial.json"


def opted_in_registries() -> list[Any]:
    """Pinned, owner-tier registries whose build row sets ``"editorial": true``."""
    from kiro_crew.apps.default_installs import _allowed_registries

    return _allowed_registries(EDITORIAL_KEY)


def registry_opted_in(reg: Any) -> bool:
    """Whether *reg* is one of :func:`opted_in_registries` (never raises)."""
    from kiro_crew.apps import registry as reg_mod

    try:
        key = reg_mod._registry_identity_key(reg.name or reg.repo)
        return any(
            reg_mod._registry_identity_key(r.name or r.repo) == key for r in opted_in_registries()
        )
    except Exception:  # noqa: BLE001 - presentation: not opted in is the safe answer
        logger.debug("could not tell whether a registry opted into editorial", exc_info=True)
        return False


def read_document(clone_path: Path) -> dict[str, Any] | None:
    """The clone's ``editorial.json`` as a JSON object, or None. Blocking.

    Capped at the official document's byte limit, and refused when it is not a
    regular file inside the clone (a symlink could point anywhere on disk).
    """
    path = clone_path / EDITORIAL_FILE
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        # RecursionError: a deeply nested document is invalid, and it must not
        # escape and cost the registry's index fetch.
        return None
    return data if isinstance(data, dict) else None


def _cache_path(reg: Any) -> Path:
    from kiro_crew.apps.registry_pipeline.caches import (
        _external_registry_cache_identity,
        _external_registry_cache_path,
    )

    index = _external_registry_cache_path(_external_registry_cache_identity(reg))
    return index.with_name(f"{index.stem}.editorial.json")


def write_cache(reg: Any, doc: dict[str, Any] | None) -> None:
    """Keep *doc* with *reg*'s index, or drop the copy when the clone has none. Blocking."""
    path = _cache_path(reg)
    try:
        if doc is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, json.dumps(doc))
    except OSError:
        logger.debug("could not cache a registry's editorial document", exc_info=True)


def _read_cache(reg: Any) -> dict[str, Any] | None:
    path = _cache_path(reg)
    try:
        if not path.is_file():
            return None
        data = json.loads(read_bytes_with_retry(path).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def load_sections() -> list[dict[str, Any]]:
    """Every opted-in registry's blocks, in registry order, or ``[]``. Blocking.

    Read from the copies the index fetch keeps, so this makes no network call:
    the listing that serves these blocks has already fetched (or cached) each
    registry's index. Each block carries ``useAppArt: true`` so the dashboard
    draws the app's own art where an official block would draw editorial art.
    """
    out: list[dict[str, Any]] = []
    for reg in opted_in_registries():
        doc = _read_cache(reg)
        if doc is None or not supported_version(doc):
            continue
        for block in project_sections(doc, artwork=False):
            out.append({**block, "useAppArt": True})
    return out


__all__ = [
    "EDITORIAL_FILE",
    "EDITORIAL_KEY",
    "load_sections",
    "opted_in_registries",
    "read_document",
    "registry_opted_in",
    "write_cache",
]
