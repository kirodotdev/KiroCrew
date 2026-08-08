"""Atomic, corruption-tolerant JSON file persistence.

Writes go to a temp file in the SAME directory then ``os.replace`` — an
atomic rename on POSIX and Windows — so a crash mid-write never leaves a
half-written store file. A cross-process/thread lock serializes writers.

Reads tolerate a missing file (return the provided default) and a corrupt
file (back it up to ``<name>.corrupt-<ts>`` and return the default) rather
than crashing the gateway — a security tool that dies because one JSON blob
got truncated is worse than one that starts from a re-derivable empty state.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .models import utcnow_iso

_LOCK = threading.RLock()


def read_json(path: Path, default: Any) -> Any:
    """Load JSON from ``path``. Missing -> ``default``. Corrupt -> quarantine
    the bad file and return ``default``."""
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        try:
            ts = utcnow_iso().replace(":", "").replace("-", "")
            path.rename(path.with_suffix(path.suffix + f".corrupt-{ts}"))
        except OSError:
            pass
        return default


def write_json(path: Path, data: Any) -> None:
    """Atomically write ``data`` as pretty JSON to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
