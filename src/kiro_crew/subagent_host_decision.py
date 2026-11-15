"""The spawn-approval answer the HOST gave, with nobody asked.

Deliberately a leaf module -- it imports nothing from ``kiro_crew`` -- so the
channel-neutral spawn-approval seam (:mod:`kiro_crew.messaging.spawn_approval_delivery`)
can return one without an edge to :mod:`kiro_crew.subagent`, which ``messaging``
keeps duck-typed because it reaches ``kiro_crew.slack`` transitively.
:mod:`kiro_crew.subagent` re-exports it, so its callers keep reading it from there.
"""

from __future__ import annotations

from typing import NamedTuple


class HostSpawnDecision(NamedTuple):
    """A spawn-approval answer the HOST gave, with nobody asked.

    Returned by a ``SpawnApprovalCallback`` in place of a bare ``bool`` when
    the gateway callback answered alone (see ``_interactive_approval``'s
    ``on_host_decision``), so the gate records ``by: host`` rather than a person's
    answer. *cause* is a ``DENY_CAUSE_*`` code when one names the host's reason (an
    expired window), ``""`` otherwise.
    """

    approved: bool
    cause: str = ""

    def __bool__(self) -> bool:
        # A tuple is truthy, so a host decline would read as a yes to a plain ``if``.
        return self.approved
