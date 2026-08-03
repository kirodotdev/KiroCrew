"""AppStorage — app-scoped persistent key-value storage.

Backed by files in the app's data directory:
``~/.kiro/crew/apps/{app_name}/data/kv/{key}.json``

Keys are validated to prevent path traversal.
Values are JSON-serializable dicts or strings.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


class AppStorage:
    """App-scoped persistent key-value storage.

    File-per-key design:
    - Avoids contention (no single-file bottleneck)
    - Supports large values without loading entire store
    - Atomic writes prevent corruption on crash
    - Key validation rejects path traversal
    """

    def __init__(self, app_name: str, data_dir: Path) -> None:
        self._app_name = app_name
        self._kv_dir = data_dir / "kv"
        self._kv_dir.mkdir(parents=True, exist_ok=True)

    @property
    def app_name(self) -> str:
        return self._app_name

    def get(self, key: str) -> dict[str, Any] | str | None:
        """Read a value by key. Returns None if not found."""
        path = self._key_path(key)
        if not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8")
            return json.loads(content)
        except json.JSONDecodeError:
            return None  # corrupted file
        except OSError as exc:
            logger.warning("AppStorage[%s] read error for key %r: %s", self._app_name, key, exc)
            return None

    @classmethod
    def read_key(cls, data_dir: Path, key: str) -> dict[str, Any] | str | None:
        """Read a single key WITHOUT constructing an AppStorage / creating its kv dir.

        A read-only accessor for callers that must not perform write I/O — e.g.
        the core reflecting an app's persisted state in a GET handler (see
        ``apps/context.py:read_persisted_nav_status``). It keeps the file-per-key
        on-disk layout owned by THIS class instead of duplicated at the call
        site, while avoiding the ``__init__`` ``mkdir`` that the instance path
        performs. Returns None for a missing / corrupt / unreadable / invalid-key
        file; never raises and never creates directories.
        """
        try:
            path = cls._kv_path(data_dir, key)
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # OSError: missing/unreadable file. ValueError: an invalid key
            # (from _kv_path), a JSON parse error (JSONDecodeError), or invalid
            # UTF-8 bytes (UnicodeDecodeError) in a hand-edited/corrupt file.
            return None

    def set(self, key: str, value: dict[str, Any] | str) -> None:
        """Write a value. Atomic write (tmp + rename).

        Always JSON-encodes the value so that round-trip is faithful
        (e.g. the string "42" is stored as '"42"', not raw 42).
        """
        path = self._key_path(key)
        content = json.dumps(value, indent=2)
        atomic_write(path, content)
        sel().log_api_access(
            caller=f"app:{self._app_name}",
            operation="app_storage.set",
            outcome="ok",
            resources=key,
        )

    def delete(self, key: str) -> bool:
        """Delete a key. Returns True if existed."""
        path = self._key_path(key)
        if path.is_file():
            path.unlink()
            sel().log_api_access(
                caller=f"app:{self._app_name}",
                operation="app_storage.delete",
                outcome="ok",
                resources=key,
            )
            return True
        return False

    def list_keys(self) -> list[str]:
        """List all stored keys."""
        if not self._kv_dir.is_dir():
            return []
        return sorted(p.stem for p in self._kv_dir.iterdir() if p.suffix == ".json")

    @staticmethod
    def _kv_path(data_dir: Path, key: str) -> Path:
        """Validate a key and return its file path under ``data_dir/kv``.

        Static so the read-only ``read_key`` accessor can resolve a path WITHOUT
        constructing an AppStorage (whose ``__init__`` creates the kv dir). This
        is the single place that owns the file-per-key on-disk layout.

        Raises ValueError if key contains path traversal / unsafe-prefix chars.
        """
        if not key:
            raise ValueError("Storage key must not be empty")
        if ".." in key or "/" in key or "\\" in key:
            raise ValueError(f"Invalid storage key (path traversal): {key!r}")
        # Additional safety: reject keys that would produce unexpected paths
        if key.startswith(".") or key.startswith("~"):
            raise ValueError(f"Invalid storage key (unsafe prefix): {key!r}")
        return data_dir / "kv" / f"{key}.json"

    def _key_path(self, key: str) -> Path:
        """Validate key and return its file path (see :meth:`_kv_path`)."""
        return self._kv_path(self._kv_dir.parent, key)
