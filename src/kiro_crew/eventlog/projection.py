"""Compatibility import path -- the projection kernel now lives in a package of its own.

The fold contract and the registry that drives it were extracted to
:mod:`kiro_crew.projection` so the crew log's folds can run on the same machinery
instead of a second implementation of the same rules. Nothing moved semantically:
the names below ARE the kernel's, re-exported here so existing importers keep
working.

New code imports :mod:`kiro_crew.projection` directly. The member log's own event
carrier stays in :mod:`kiro_crew.eventlog.types`, which is why the kernel does not
import it and reads an event's ``seq`` through a supplied reader instead.
"""

from __future__ import annotations

from kiro_crew.projection import OnChange, ProjectionDefinition, ProjectionRegistry

__all__ = ["OnChange", "ProjectionDefinition", "ProjectionRegistry"]
