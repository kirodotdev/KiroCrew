"""Agent-config mirrors: one declared spec projection per backend.

See ``README.md`` in this folder for what a mirror is and how to add one, and
``docs/request-for-change/rfc-agent-config-mirror.md`` for the design.
"""

from __future__ import annotations

from kiro_crew.providers.mirrors.base import (
    AgentConfigMirror,
    Concern,
    Disposition,
    Ruling,
)
from kiro_crew.providers.mirrors.registry import MIRRORS, NO_MIRROR, mirror_for

__all__ = [
    "MIRRORS",
    "NO_MIRROR",
    "AgentConfigMirror",
    "Concern",
    "Disposition",
    "Ruling",
    "mirror_for",
]
