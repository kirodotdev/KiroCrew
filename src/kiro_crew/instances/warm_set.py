"""Resolve the effective warm-iframe cap for the dashboard's instance panes.

Kept as a pure function in its own module so the policy -- "automatic means as
many crews as could be warm at once" -- is testable without a gateway, a
registry, or a live tunnel, and so the one place that decides it is not buried in
an HTTP handler. ``GET /api/instances`` is the only caller: the resolved integer
travels to the browser as ``warm_set_cap`` and the viewport enforces it, so the
frontend never has to know that an automatic mode exists.
"""

from __future__ import annotations

from kiro_crew.instances.constants import WARM_SET_CAP_AUTO_CEILING


def resolve_warm_set_cap(configured: int, eligible_count: int) -> int:
    """Return the number of instance panes that may stay warm right now.

    *configured* is ``instances.warm_set_cap`` as stored. Any value >= 1 is an
    explicit operator budget and is returned UNCHANGED -- including a value below
    *eligible_count*. Silently widening a deliberately tight cap would defeat the
    only knob that bounds renderer cost, and on a memory-starved machine
    accepting eviction is a legitimate trade.

    ``WARM_SET_CAP_AUTO`` (0, the default) resolves to *eligible_count*: how many
    crews are REGISTERED, not how many are connected this instant. That
    distinction is the whole point of this function. Deriving the cap from live
    connection state made it a race against tunnel startup -- a crew whose tunnel
    came up a moment after the dashboard polled was not counted, so the cap
    landed one short, and the pane that lost was whichever crew connected last.
    It changed on every restart and read as a random crew being broken. A count
    of registered crews cannot race, and it rises on its own when a crew is
    added, so nobody has to remember to widen the cap alongside.

    Clamped to ``WARM_SET_CAP_AUTO_CEILING`` so a large fleet cannot mount an
    unbounded number of dashboard SPAs in one renderer.

    Never returns less than 1. The active pane is always warm, so a cap of 0 is
    one the viewport cannot honour -- it would evict the pane the user is looking
    at. A negative *eligible_count* is read as none registered rather than
    trusted, since it can only arrive from a caller bug.
    """
    if configured >= 1:
        return configured
    return max(1, min(max(eligible_count, 0), WARM_SET_CAP_AUTO_CEILING))
