"""The record a per-project MCP backend is keyed and confined by.

A leaf, so ``backend`` can name it without importing the daemon owners; the
daemon's ``project_scope`` keeps the records and decides who gets which key.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


class ProjectScopeUnavailable(RuntimeError):
    """No per-project backend can serve this connection; the stub runs its own."""


@dataclass(frozen=True)
class ProjectBinding:
    """The project root and sandbox plan the gateway recorded for one session."""

    root: str
    sandbox_mode: str
    hidden_dirs: tuple[str, ...] = ()
    expose_files: tuple[str, ...] = ()
    #: The operator's read-only paths and exceptions the session's wrap applied.
    readonly_paths: tuple[str, ...] = ()
    readonly_exceptions: tuple[str, ...] = ()
    #: The env the session's launch added over the gateway env, as sorted pairs.
    #: Values stay in memory: never in ``repr``, a log or an audit row.
    env_layer: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def sandbox_hash(self) -> str:
        """Digest of the root and plan; two sessions share a backend only on equality."""
        payload = json.dumps(
            [
                self.root,
                self.sandbox_mode,
                sorted(self.hidden_dirs),
                sorted(self.expose_files),
                sorted(self.readonly_paths),
                sorted(self.readonly_exceptions),
                hashlib.sha256(
                    json.dumps(sorted(self.env_layer), separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
