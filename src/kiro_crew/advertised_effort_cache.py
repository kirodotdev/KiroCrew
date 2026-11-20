"""Last-seen effort levels a harness advertised for a model, kept across restarts.

A harness in ``ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION`` states its effort levels
only on ``session/new``, so an empty New Chat draft has nothing to show and the
effort control stays hidden until the first message. This module remembers what a
live session of that harness reported, keyed by backend and model id, so a later
draft that would run the same model can show the control at once.

The live session's own option stays the authority: it overwrites the record every time
it reports, and a draft only reads the record for the exact model it would serve. A
model with no record stays unknown, as before.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

__all__ = ["recall", "remember"]

_FILE_NAME = "advertised_effort_levels.json"
_lock = threading.Lock()


def _path() -> Path:
    return data_home() / _FILE_NAME


def _read() -> dict[str, dict[str, list[str]]]:
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


def remember(backend: str, model: str, levels: list[str]) -> None:
    """Record *levels* as what *backend* advertised for *model*; a no-op on no model.

    An empty *levels* clears the record, since a session that now advertises nothing
    has retracted the control. Best effort: a write failure never reaches the caller.
    """
    if not backend or not model or model == "auto":
        return
    with _lock:
        data = _read()
        models = data.setdefault(backend, {})
        if levels:
            if models.get(model) == levels:
                return
            models[model] = list(levels)
        elif model in models:
            del models[model]
        else:
            return
        path = _path()
        tmp_name = ""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{_FILE_NAME}.")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True)
            os.replace(tmp_name, path)
            tmp_name = ""
        except OSError:
            logger.debug("advertised effort levels not saved", exc_info=True)
        finally:
            if tmp_name:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)


def recall(backend: str, model: str | None) -> list[str]:
    """The levels *backend* last advertised for exactly *model*, or ``[]``."""
    if not backend or not model or model == "auto":
        return []
    with _lock:
        return list(_read().get(backend, {}).get(model, []))
