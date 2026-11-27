"""The record a per-project MCP backend is keyed and confined by.

A leaf, so ``backend`` can name it without importing the daemon owners; the
daemon's ``project_scope`` keeps the records and decides who gets which key.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


class ProjectScopeUnavailable(RuntimeError):
    """No per-project backend can serve this connection; the stub runs its own."""


@dataclass(frozen=True)
class ProjectBinding:
    """The project root and sandbox plan the gateway recorded for one session."""

    root: str
    sandbox_mode: str
    hidden_dirs: tuple[str, ...] = ()
    expose_files: tuple[str, ...] = ()

    def sandbox_hash(self) -> str:
        """Digest of the root and plan; two sessions share a backend only on equality."""
        payload = json.dumps(
            [self.root, self.sandbox_mode, sorted(self.hidden_dirs), sorted(self.expose_files)],
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
