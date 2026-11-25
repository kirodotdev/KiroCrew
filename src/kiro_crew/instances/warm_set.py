"""Resolve the effective warm-pane cap for the dashboard's remote Crews.

Kept as a pure function in its own module so the policy is testable without a
gateway, a registry, or a live tunnel, and so the one place that decides it is
not buried in an HTTP handler. ``GET /api/instances`` is the only caller: the
resolved integer travels to the browser as ``warm_set_cap`` and the dashboard
keeps that many of the most recently opened Crews warm, SSH and relay together,
so the frontend never has to know that an automatic mode exists.
"""

from __future__ import annotations

from kiro_crew.instances.constants import WARM_SET_CAP_AUTO_CEILING


def resolve_warm_set_cap(configured: int) -> int:
    """Return how many remote Crew panes may stay warm right now.

    *configured* is ``instances.warm_set_cap`` as stored. Any value >= 1 is an
    explicit operator budget and is returned UNCHANGED, above or below the
    automatic value. Silently changing a deliberate cap would defeat the only knob
    that bounds renderer cost.

    ``WARM_SET_CAP_AUTO`` (0, the default) resolves to
    ``WARM_SET_CAP_AUTO_CEILING`` flat. It is not derived from the registry: the
    cap covers relay Crews too, which this registry does not hold, so a count of
    registered SSH crews would leave no room for them. A constant also cannot race
    tunnel startup the way a live connected count did.

    A negative value never reaches here: ``InstancesConfig`` already maps it to
    automatic.
    """
    if configured >= 1:
        return configured
    return WARM_SET_CAP_AUTO_CEILING
