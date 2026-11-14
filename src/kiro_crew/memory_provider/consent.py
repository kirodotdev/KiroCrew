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
import os
import tempfile
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
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
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
        grants = [g for g in read_grants() if g["provider_id"] != provider_id]
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
        _write({"grants": [g for g in read_grants() if g["provider_id"] != provider_id]})


def _write(document: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".memory_provider_consent.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
