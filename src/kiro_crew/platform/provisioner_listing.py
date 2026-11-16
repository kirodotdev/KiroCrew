"""The deployment's remote provisioners, read from the CPP ``remote_provisioners`` seam.

One read shared by the dashboard's Set-up tab (``dashboard/handlers_cloud.py``)
and ``kirocrew setup``'s cloud step, so the two cannot disagree about which
lanes a deployment offers. It lives outside the dashboard handler module
because that module pulls in the aiohttp and cloud-engine graph, and the
wizard reads this list in the middle of an interactive prompt sequence.
"""

from __future__ import annotations

from kiro_crew.platform import context
from kiro_crew.platform.defaults import BUILTIN_REMOTE_PROVISIONER


def offered_provisioners() -> list:
    """The deployment's provisioners, from the CPP ``remote_provisioners`` seam.

    ``safe_context_call`` fallback is the built-in descriptor alone: a degraded
    seam read keeps today's AWS tab working rather than emptying it, which is
    the fail-safe direction for a listing (nothing here mints or bills). Rows
    with a blank id or kind are dropped, never shadowed. Synchronous; a handler
    calls it via ``_in_executor`` because context resolution can touch disk.
    ``PlatformCompositionError`` still propagates, per ``safe_context_call``.
    The context is read as ``context.current_context`` at call time, so a
    caller that swaps the composed context sees the listing follow it.
    """
    rows = context.safe_context_call(
        lambda: list(context.current_context().remote_provisioners.provisioners()),
        fallback_factory=lambda: [BUILTIN_REMOTE_PROVISIONER],
        log_message="remote_provisioners.provisioners degraded; offering the built-in lane only",
    )
    return [r for r in rows if getattr(r, "id", "") and getattr(r, "kind", "")]
