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
    """Stable process identities that bind one shell root to its harness.

    ``hops`` runs from the runtime pid down to the shell's expected parent, each
    entry a ``(pid, start_id)`` pair; every hop after the first is the sole direct
    child of the hop above it.
    """

    hops: tuple[tuple[int, str], ...]

    @property
    def runtime_pid(self) -> int:
        return self.hops[0][0]


class _ShellCleanupMixin:
    """Cleanup authority read off the oracle's tracked shell; see the module docstring."""

    _proc: str
    _darwin: DarwinProcessBackend | None
    _tracked_child: int | None
    _tracked_child_start_id: str | None
    _tracked_tool: ToolCallState | None
    _cleanup_tenancy: Callable[[], int | None] | None
    _sandbox_wrapped_by_crew: Callable[[], bool] | None
    _engine_is_child: Callable[[], bool] | None
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

    def _engine_child_status(self) -> bool | None:
        """Trusted harness fact that the tools run under its sole engine child.

        No probe means no extra hop; a raising or non-boolean probe means the
        caller cannot attest the shape, so cleanup has no authority.
        """
        if self._engine_is_child is None:
            return False
        try:
            engine = self._engine_is_child()
        except Exception:
            logger.debug("liveness: engine child probe failed", exc_info=True)
            return None
        return engine if type(engine) is bool else None

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
        engine = self._engine_child_status()
        if engine is None:
            return None
        # Kiro Crew's Linux namespace launcher sits at runtime_pid with the harness as
        # its one child; a relay harness's engine is in turn its one child and the
        # tools' parent. Each such level is one sole-child hop.
        extra_hops = int(wrapped and self._darwin is None) + int(engine)
        runtime_start = platform_compat.get_process_start_id(runtime_pid)
        if not self._valid_start_id(runtime_start):
            return None
        hops: list[tuple[int, str]] = [(runtime_pid, runtime_start)]
        children = self._direct_children_for_cleanup(runtime_pid)
        for _ in range(extra_hops):
            if children is None or len(children) != 1:
                return None
            pid = children[0]
            start = platform_compat.get_process_start_id(pid)
            if not self._valid_start_id(start):
                return None
            hops.append((pid, start))
            children = self._direct_children_for_cleanup(pid)
        if children is None or root_pid not in children:
            return None
        return _CleanupLineage(tuple(hops))

    def _cleanup_lineage_is_current(self, lineage: _CleanupLineage, root_pid: int) -> bool:
        """Recheck every trusted identity and direct edge in *lineage*."""
        hops = lineage.hops
        for index, (pid, start_id) in enumerate(hops):
            if platform_compat.get_process_start_id(pid) != start_id:
                return False
            children = self._direct_children_for_cleanup(pid)
            if children is None:
                return False
            if index + 1 < len(hops):
                if children != [hops[index + 1][0]]:
                    return False
            elif root_pid not in children:
                return False
        return True

    def stalled_shell_tree(self, runtime_pid: int | None) -> list[tuple[int, str]]:
        """Return the tracked shell subtree as ``(pid, start_id)``, leaf-first.

        This is cleanup authority, not another liveness verdict, so every
        uncertainty fails closed. The tool must have retained exact-command
        attribution on an exclusively single-session runtime. Its root must be
        the direct child of the identity-pinned harness: normally the runtime
        itself, or the sole child of Kiro Crew's Linux namespace launcher; on
        the KAS relay route, the relay's sole engine child below that. The
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
