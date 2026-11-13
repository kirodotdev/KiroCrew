"""Signal authority over a stalled shell tool's process tree.

:class:`_ShellCleanupMixin` is the half of :class:`~kiro_crew.acp.liveness.LivenessOracle`
that decides which processes a tool-stall recovery may signal: the exclusive-tenancy
probe, the harness lineage proof (:class:`_CleanupLineage`) and
:meth:`~_ShellCleanupMixin.stalled_shell_tree`, the leaf-first ``(pid, start_id)``
snapshot. The oracle's matcher records the tracked shell and its start identity; every
read here fails closed. :mod:`kiro_crew.acp.liveness` re-exports every name here.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, TypeGuard

from kiro_crew import platform_compat
from kiro_crew.acp.liveness_proc import (
    DarwinProcessBackend,
    strict_descendants,
    strict_direct_children,
)
from kiro_crew.acp.liveness_tool_state import ToolCallState

#: The facade's logger: a record from this module keeps the name ``kiro_crew.acp.liveness``.
logger = logging.getLogger("kiro_crew.acp.liveness")


@dataclass(frozen=True)
class _CleanupLineage:
    """Stable process identities that bind one shell root to its harness."""

    runtime_pid: int
    runtime_start_id: str
    parent_pid: int
    parent_start_id: str


class _ShellCleanupMixin:
    """Cleanup authority read off the oracle's tracked shell; see the module docstring."""

    _proc: str
    _darwin: DarwinProcessBackend | None
    _tracked_child: int | None
    _tracked_child_start_id: str | None
    _tracked_tool: ToolCallState | None
    _cleanup_tenancy: Callable[[], int | None] | None
    _sandbox_wrapped_by_crew: Callable[[], bool] | None
    _cleanup_lineage: _CleanupLineage | None

    def _cleanup_has_exclusive_tenancy(self) -> bool:
        """Whether this runtime positively reports exactly one session."""
        if self._cleanup_tenancy is None:
            return False
        try:
            count = self._cleanup_tenancy()
        except Exception:
            logger.debug("liveness: cleanup tenancy probe failed", exc_info=True)
            return False
        return type(count) is int and count == 1

    def _observe_cleanup_tenancy(self, tool: ToolCallState) -> None:
        """Permanently deny this call's cleanup after any non-exclusive view."""
        if not self._cleanup_has_exclusive_tenancy():
            tool.shell_cleanup_disqualified = True

    def _crew_sandbox_wrap_status(self) -> bool | None:
        """Trusted runtime wrapper fact, or None when the caller cannot attest it."""
        if self._sandbox_wrapped_by_crew is None:
            return None
        try:
            wrapped = self._sandbox_wrapped_by_crew()
        except Exception:
            logger.debug("liveness: sandbox wrap probe failed", exc_info=True)
            return None
        return wrapped if type(wrapped) is bool else None

    def _direct_children_for_cleanup(self, pid: int) -> list[int] | None:
        """A complete direct-child set, never best-effort partial evidence."""
        if self._darwin is not None:
            children = self._darwin.direct_children(pid)
        elif os.path.isdir(self._proc):
            children = strict_direct_children(self._proc, pid)
        else:
            return None
        if children is None or any(child <= 1 for child in children):
            return None
        return children if len(children) == len(set(children)) else None

    @staticmethod
    def _valid_start_id(start_id: object) -> TypeGuard[str]:
        return isinstance(start_id, str) and bool(start_id)

    def _capture_cleanup_lineage(self, runtime_pid: int, root_pid: int) -> _CleanupLineage | None:
        """Bind *root_pid* to the harness that this runtime actually spawned."""
        wrapped = self._crew_sandbox_wrap_status()
        if wrapped is None:
            return None
        runtime_start = platform_compat.get_process_start_id(runtime_pid)
        if not self._valid_start_id(runtime_start):
            return None
        runtime_children = self._direct_children_for_cleanup(runtime_pid)
        if runtime_children is None:
            return None
        if wrapped and self._darwin is None:
            # Kiro Crew's Linux namespace launcher sits at runtime_pid; its one child is the harness.
            if len(runtime_children) != 1:
                return None
            parent_pid = runtime_children[0]
            parent_start = platform_compat.get_process_start_id(parent_pid)
            if not self._valid_start_id(parent_start):
                return None
            parent_children = self._direct_children_for_cleanup(parent_pid)
        else:
            parent_pid = runtime_pid
            parent_start = runtime_start
            parent_children = runtime_children
        if parent_children is None or root_pid not in parent_children:
            return None
        return _CleanupLineage(runtime_pid, runtime_start, parent_pid, parent_start)

    def _cleanup_lineage_is_current(self, lineage: _CleanupLineage, root_pid: int) -> bool:
        """Recheck every trusted identity and direct edge in *lineage*."""
        if platform_compat.get_process_start_id(lineage.runtime_pid) != lineage.runtime_start_id:
            return False
        runtime_children = self._direct_children_for_cleanup(lineage.runtime_pid)
        if runtime_children is None:
            return False
        if lineage.parent_pid != lineage.runtime_pid:
            if runtime_children != [lineage.parent_pid]:
                return False
            if platform_compat.get_process_start_id(lineage.parent_pid) != lineage.parent_start_id:
                return False
            parent_children = self._direct_children_for_cleanup(lineage.parent_pid)
        else:
            parent_children = runtime_children
        return parent_children is not None and root_pid in parent_children

    def stalled_shell_tree(self, runtime_pid: int | None) -> list[tuple[int, str]]:
        """Return the tracked shell subtree as ``(pid, start_id)``, leaf-first.

        This is cleanup authority, not another liveness verdict, so every
        uncertainty fails closed. The tool must have retained exact-command
        attribution on an exclusively single-session runtime. Its root must be
        the direct child of the identity-pinned harness: normally the runtime
        itself, or the sole child of Kiro Crew's Linux namespace launcher. The
        root's subtree is enumerated twice with strict readers; an unreadable
        branch, changed edge, recycled root, or a changed membership refuses
        every target rather than authorizing a partial kill. Each target's
        identity is checked again by the signaller, through a pinned handle.
        """

        def descendants_of(pid: int) -> list[int] | None:
            if self._darwin is not None:
                return self._darwin.descendants(pid)
            if not os.path.isdir(self._proc):
                return None
            listed = strict_descendants(self._proc, pid)
            return None if listed is None else listed[1:]

        tracked_tool = self._tracked_tool
        if tracked_tool is None or tracked_tool.shell_cleanup_disqualified:
            return []
        if not self._cleanup_has_exclusive_tenancy():
            tracked_tool.shell_cleanup_disqualified = True
            return []
        root = self._tracked_child
        root_start = self._tracked_child_start_id
        lineage = self._cleanup_lineage
        if (
            root is None
            or root_start is None
            or lineage is None
            or runtime_pid != lineage.runtime_pid
            or platform_compat.get_process_start_id(root) != root_start
            or not self._cleanup_lineage_is_current(lineage, root)
        ):
            return []

        descendants = descendants_of(root)
        if descendants is None:
            return []
        captured: list[tuple[int, str]] = [(root, root_start)]
        for pid in descendants:
            start_id = platform_compat.get_process_start_id(pid)
            if not self._valid_start_id(start_id):
                return []
            captured.append((pid, start_id))

        descendants_again = descendants_of(root)
        if (
            descendants_again is None
            or set(descendants_again) != set(descendants)
            or platform_compat.get_process_start_id(root) != root_start
            or not self._cleanup_lineage_is_current(lineage, root)
        ):
            return []
        return list(reversed(captured))
