"""The owner's consent to send memory to a provider that keeps it elsewhere.

Recorded in the keystone ``memory_provider_consent.json``
(:func:`kiro_crew.config.loader_locations.memory_provider_consent_path`): READONLY in
every sandbox mode, written only by the owner's ``PUT /api/memory/provider``. A grant
names one provider id and the app version the owner agreed to, so an update of the
app asks again. Every read fails soft to no consent.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

logger = logging.getLogger("kiro_crew.memory_provider")

_MAX_GRANTS = 32
_write_lock = threading.Lock()


def _path() -> Any:
    from kiro_crew.config.loader_locations import memory_provider_consent_path

    return memory_provider_consent_path()


def read_grants() -> list[dict[str, Any]]:
    """The grants, for a reader: any failure reads as no consent (the narrow answer)."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return _valid(raw)


def _grants_for_rewrite() -> list[dict[str, Any]]:
    """The grants a rewrite starts from. Only a missing file reads as empty.

    Any other failure (EACCES, EIO, a record that is not JSON) raises, so a transient
    error never publishes an empty record over the owner's other consents.
    """
    try:
        text = _path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return _valid(json.loads(text)) if text.strip() else []


def _valid(raw: Any) -> list[dict[str, Any]]:
    grants = raw.get("grants") if isinstance(raw, dict) else None
    if not isinstance(grants, list):
        return []
    return [
        g
        for g in grants
        if isinstance(g, dict)
        and isinstance(g.get("provider_id"), str)
        and isinstance(g.get("app_version"), str)
    ]


def has_egress_consent(provider: Any) -> bool:
    """Whether the owner agreed to send memory to *provider* at its current version."""
    provider_id = getattr(provider, "provider_id", "")
    version = getattr(provider, "app_version", "")
    return any(
        g["provider_id"] == provider_id and g["app_version"] == version and g.get("egress") is True
        for g in read_grants()
    )


def grant_egress(provider_id: str, app_version: str) -> None:
    """Record the owner's consent. Called only from the owner's dashboard route."""
    with _write_lock:
        grants = [g for g in _grants_for_rewrite() if g["provider_id"] != provider_id]
        grants.append(
            {
                "provider_id": provider_id,
                "app_version": app_version,
                "egress": True,
                "granted_at": time.time(),
            }
        )
        _write({"grants": grants[-_MAX_GRANTS:]})


def revoke_egress(provider_id: str) -> None:
    with _write_lock:
        _write({"grants": [g for g in _grants_for_rewrite() if g["provider_id"] != provider_id]})


def _write(document: dict[str, Any]) -> None:
    """Rewrite the record in place: a running sandbox bind-mounts this inode read-only,
    and a rename over it would detach that seal (``atomic_write.rewrite_in_place``)."""
    from kiro_crew.atomic_write import rewrite_in_place

    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rewrite_in_place(path, json.dumps(document) + "\n", restrict_to_owner=True)
