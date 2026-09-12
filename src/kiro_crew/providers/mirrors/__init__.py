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
    SessionProjection,
)
from kiro_crew.providers.mirrors.registry import (
    MIRRORS,
    PROJECTIONS,
    McpProjection,
    ProjectionKind,
    has_mirror,
    mirror_for,
    projection_for,
)

__all__ = [
    "MIRRORS",
    "PROJECTIONS",
    "AgentConfigMirror",
    "Concern",
    "Disposition",
    "McpProjection",
    "ProjectionKind",
    "Ruling",
    "SessionProjection",
    "has_mirror",
    "mirror_for",
    "projection_for",
]
