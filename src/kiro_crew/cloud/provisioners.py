"""The deployment's remote provisioners, read from the CPP seam.

One read shared by the dashboard's Set-up tab (``dashboard/handlers_cloud.py``)
and ``kirocrew setup``'s cloud step, so the two cannot disagree about whether
this deployment offers the built-in EC2 lane. It is a light leaf, not part of
the handler module, because the wizard reads it right after the operator
answers the previous question: importing the dashboard handler graph there
keeps the terminal silent for seconds on a cold start, and an Enter pressed
into that silence answers the AWS question before it is shown.
"""

from __future__ import annotations


def offered_provisioners() -> list:
    """The deployment's provisioners, from the CPP ``remote_provisioners`` seam.

    ``safe_context_call`` fallback is the built-in descriptor alone: a degraded
    seam read keeps today's AWS tab working rather than emptying it, which is
    the fail-safe direction for a listing (nothing here mints or bills). Rows
    with a blank id or kind are dropped, never shadowed. Synchronous; the
    dashboard calls it via ``_in_executor`` because context resolution can
    touch disk.
    """
    from kiro_crew.platform.context import current_context, safe_context_call
    from kiro_crew.platform.defaults import BUILTIN_REMOTE_PROVISIONER

    rows = safe_context_call(
        lambda: list(current_context().remote_provisioners.provisioners()),
        fallback_factory=lambda: [BUILTIN_REMOTE_PROVISIONER],
        log_message="remote_provisioners.provisioners degraded; offering the built-in lane only",
    )
    return [r for r in rows if getattr(r, "id", "") and getattr(r, "kind", "")]
