"""Ledgers — shared markdown todo/scratch lists attachable to chat sessions.

A ledger is a first-class entity (id, title, markdown content, monotonic
version) stored as a sidecar markdown file under ``config_dir()/ledgers/``
plus a small JSON registry carrying metadata. Sessions reference a ledger
via ``_ChatSlot.ledger_id`` (the slot's *pinned* ledger); many slots may pin
the same ledger.

Concurrency model: every content write is an optimistic compare-and-swap —
callers supply the ``base_version`` they edited from and the write is
rejected when it no longer matches, so a concurrent edit from another
session can never be silently overwritten. Checkbox toggles are line-level
atomic operations guarded by the expected line text.

All methods are synchronous filesystem operations; HTTP handlers MUST call
them off the event loop (``run_in_executor``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

_LEDGERS_DIRNAME = "ledgers"
_REGISTRY_FILE = "registry.json"
_LEDGER_ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_TITLE_LEN = 500
MAX_CONTENT_LEN = 50_000
_DEFAULT_TITLE = "Untitled ledger"
# `- [ ]` / `- [x]` checklist items, tolerant of indentation and `*` bullets.
_CHECKBOX_RE = re.compile(r"^(\s*[-*] \[)( |x|X)(\] ?)(.*)$")


class LedgerConflictError(Exception):
    """Raised when a compare-and-swap write loses the race.

    Carries the current server-side state so callers can return it to the
    client for merge/resolution without a second read.
    """

    def __init__(self, current: dict[str, Any]):
        super().__init__("version_conflict")
        self.current = current


class LedgerNotFoundError(KeyError):
    """Raised when the ledger id is unknown to the registry."""


@dataclass
class Ledger:
    """Registry entry for one ledger (content lives in its own file)."""

    id: str
    title: str
    version: int
    created_at: float
    updated_at: float

    def to_meta(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def checklist_progress(content: str) -> dict[str, int]:
    """Count ``- [ ]`` / ``- [x]`` items → ``{"done": n, "total": m}``."""
    done = 0
    total = 0
    for line in content.splitlines():
        m = _CHECKBOX_RE.match(line)
        if m:
            total += 1
            if m.group(2).lower() == "x":
                done += 1
    return {"done": done, "total": total}


@dataclass
class LedgerStore:
    """Filesystem-backed ledger storage with CAS content writes."""

    root: Path = field(default_factory=lambda: config_dir() / _LEDGERS_DIRNAME)

    # ── internals ──────────────────────────────────────────────────────

    def _registry_path(self) -> Path:
        return self.root / _REGISTRY_FILE

    def _content_path(self, ledger_id: str) -> Path:
        """Resolve a ledger id to its content file, containment-checked.

        Ids are server-generated, but the id also transits the HTTP API, so
        the path is re-validated here (strict grammar + resolved-path
        containment) as defense-in-depth against traversal.
        """
        if not _LEDGER_ID_RE.match(ledger_id):
            raise LedgerNotFoundError(ledger_id)
        path = (self.root / f"{ledger_id}.md").resolve()
        root = self.root.resolve()
        if not path.is_relative_to(root):  # pragma: no cover - grammar already forbids
            raise LedgerNotFoundError(ledger_id)
        return path

    def _load_registry(self) -> dict[str, Ledger]:
        path = self._registry_path()
        try:
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                out: dict[str, Ledger] = {}
                for item in raw:
                    if isinstance(item, dict) and _LEDGER_ID_RE.match(str(item.get("id", ""))):
                        out[item["id"]] = Ledger(
                            id=item["id"],
                            title=str(item.get("title") or _DEFAULT_TITLE)[:MAX_TITLE_LEN],
                            version=int(item.get("version") or 1),
                            created_at=float(item.get("created_at") or 0.0),
                            updated_at=float(item.get("updated_at") or 0.0),
                        )
                return out
        except Exception:
            logger.warning("Failed to load ledger registry", exc_info=True)
        return {}

    def _save_registry(self, registry: dict[str, Ledger]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = [ledger.to_meta() for ledger in registry.values()]
        self._atomic_write(self._registry_path(), json.dumps(data, indent=2))

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def _read_content(self, ledger_id: str) -> str:
        path = self._content_path(ledger_id)
        try:
            if path.exists():
                return path.read_text(encoding="utf-8")
        except Exception:
            logger.warning("Failed to read ledger content %s", ledger_id, exc_info=True)
        return ""

    # ── public API ─────────────────────────────────────────────────────

    def list(self) -> list[dict[str, Any]]:
        """All ledgers (meta + checklist progress), newest-updated first."""
        registry = self._load_registry()
        out = []
        for ledger in sorted(registry.values(), key=lambda l: l.updated_at, reverse=True):
            meta = ledger.to_meta()
            meta["progress"] = checklist_progress(self._read_content(ledger.id))
            out.append(meta)
        return out

    def create(self, title: str = "") -> dict[str, Any]:
        registry = self._load_registry()
        now = time.time()
        ledger = Ledger(
            id=uuid.uuid4().hex[:12],
            title=(title.strip() or _DEFAULT_TITLE)[:MAX_TITLE_LEN],
            version=1,
            created_at=now,
            updated_at=now,
        )
        registry[ledger.id] = ledger
        self.root.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self._content_path(ledger.id), "")
        self._save_registry(registry)
        meta = ledger.to_meta()
        meta["content"] = ""
        return meta

    def get(self, ledger_id: str) -> dict[str, Any]:
        registry = self._load_registry()
        ledger = registry.get(ledger_id)
        if not ledger:
            raise LedgerNotFoundError(ledger_id)
        meta = ledger.to_meta()
        meta["content"] = self._read_content(ledger_id)
        return meta

    def update(
        self,
        ledger_id: str,
        *,
        content: str | None = None,
        base_version: int | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Rename and/or CAS-write content. Content writes require base_version."""
        registry = self._load_registry()
        ledger = registry.get(ledger_id)
        if not ledger:
            raise LedgerNotFoundError(ledger_id)
        if content is not None:
            if base_version is None or int(base_version) != ledger.version:
                raise LedgerConflictError(
                    {"content": self._read_content(ledger_id), "version": ledger.version}
                )
            self._atomic_write(self._content_path(ledger_id), content[:MAX_CONTENT_LEN])
            ledger.version += 1
        if title is not None and title.strip():
            ledger.title = title.strip()[:MAX_TITLE_LEN]
        ledger.updated_at = time.time()
        self._save_registry(registry)
        return ledger.to_meta()

    def toggle(self, ledger_id: str, line: int, expected: str) -> dict[str, Any]:
        """Atomically flip one checkbox line, guarded by its expected text.

        Line-level CAS: concurrent toggles of *different* lines both succeed
        regardless of interleaving; a toggle whose expected text no longer
        matches (the line changed underneath) conflicts.
        """
        registry = self._load_registry()
        ledger = registry.get(ledger_id)
        if not ledger:
            raise LedgerNotFoundError(ledger_id)
        content = self._read_content(ledger_id)
        lines = content.splitlines()
        current_state = {"content": content, "version": ledger.version}
        if line < 0 or line >= len(lines) or lines[line] != expected:
            raise LedgerConflictError(current_state)
        m = _CHECKBOX_RE.match(lines[line])
        if not m:
            raise LedgerConflictError(current_state)
        flipped = " " if m.group(2).lower() == "x" else "x"
        lines[line] = f"{m.group(1)}{flipped}{m.group(3)}{m.group(4)}"
        new_content = "\n".join(lines) + ("\n" if content.endswith("\n") else "")
        self._atomic_write(self._content_path(ledger_id), new_content)
        ledger.version += 1
        ledger.updated_at = time.time()
        self._save_registry(registry)
        return {"version": ledger.version, "content": new_content}

    def delete(self, ledger_id: str) -> None:
        registry = self._load_registry()
        if ledger_id not in registry:
            raise LedgerNotFoundError(ledger_id)
        path = self._content_path(ledger_id)
        registry.pop(ledger_id)
        self._save_registry(registry)
        try:
            path.unlink(missing_ok=True)
        except Exception:
            logger.warning("Failed to remove ledger content %s", ledger_id, exc_info=True)
