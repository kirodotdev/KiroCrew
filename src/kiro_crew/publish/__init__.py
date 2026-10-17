"""Publish destinations that ship with the public edition.

``publish_provider`` defines the destination seam and ``publish_sync`` owns the
provider-agnostic orchestration; this package holds the concrete destinations the
public edition itself registers. Today that is exactly one -- the personal cloud
drive in the operator's own AWS account (:mod:`personal_drive`).

Nothing here is imported by the core at module scope. The public edition's
``PublishRegistry.register_publish_providers`` is the single entry point, so a
build that registers a different destination never loads this code.
"""

from __future__ import annotations

__all__ = ["personal_drive"]
