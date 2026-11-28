"""Effort levels a harness advertised for a model, kept across restarts.

A harness in ``ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION`` states its effort levels
only on ``session/new``, so an empty New Chat draft has nothing to show and the
effort control stays hidden until the first message. This module remembers what a
session of that harness reported, keyed by backend and model id, so a later draft
that would run the same model can show the control at once.

Kept the way the advertised-model cache is (``model_registry.refresh_advertised_models``
and ``persist_advertised_models``): in memory, loaded once at import, and written to
disk off the event loop. The session's own option stays the authority: it overwrites
the record each time it reports, and a draft reads the record only for the exact model
it would serve. A model with no record stays unknown.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

__all__ = ["persist", "recall", "remember"]

_FILE_NAME = "advertised_effort_levels.json"


def _path() -> Path:
    return data_home() / _FILE_NAME


def _load() -> dict[str, dict[str, list[str]]]:
    """The persisted record. A missing or unreadable file reads as empty, never raises."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for backend, models in raw.items():
        if not isinstance(backend, str) or not isinstance(models, dict):
            continue
        out[backend] = {
            m: [lvl for lvl in levels if isinstance(lvl, str)]
            for m, levels in models.items()
            if isinstance(m, str) and isinstance(levels, list)
        }
    return out


_LEVELS: dict[str, dict[str, list[str]]] = _load()


def persist() -> None:
    """Write the in-memory record to disk (best effort, blocking).

    Call it off the event loop. A ``dict(...)`` snapshot keeps a concurrent
    :func:`remember` from changing the record mid-serialize.
    """
    try:
        snapshot = {
            backend: {model: list(levels) for model, levels in dict(models).items()}
            for backend, models in dict(_LEVELS).items()
        }
        atomic_write(_path(), json.dumps(snapshot, sort_keys=True))
    except OSError:  # pragma: no cover - disk full / perms
        logger.debug("advertised effort levels not saved", exc_info=True)


def _persist_off_the_loop() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        persist()
        return
    loop.run_in_executor(None, persist)


def remember(backend: str, model: str, levels: list[str]) -> bool:
    """Record *levels* as what *backend* advertised for *model*; return True when that changed it.

    Call it only for levels a session has reported. An empty *levels* clears the
    record, since a session that reported an option with no level has retracted the
    control. A no-op on no model or ``auto``. The disk write runs off the event loop.
    """
    if not backend or not model or model == "auto":
        return False
    models = _LEVELS.setdefault(backend, {})
    if levels:
        if models.get(model) == levels:
            return False
        models[model] = list(levels)
    elif model in models:
        del models[model]
    else:
        return False
    _persist_off_the_loop()
    return True


def recall(backend: str, model: str | None) -> list[str]:
    """The levels *backend* last advertised for exactly *model*, or ``[]``."""
    if not backend or not model or model == "auto":
        return []
    return list(_LEVELS.get(backend, {}).get(model, []))
