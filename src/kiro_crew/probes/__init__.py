"""Probes: the domain half of a watch, one module per kind of subject.

A probe answers exactly two questions about one external subject -- *what is it*
(:meth:`~kiro_crew.irq.Probe.identity`) and *what does it look like right now*
(:meth:`~kiro_crew.irq.Probe.observe`) -- and nothing else. Everything generic
lives in :mod:`kiro_crew.irq`: state persistence, per-epoch reset, time-bounded
dedupe, the coalescing window, and the consecutive-failure backstop.

Probes live here rather than beside a driver because a probe outlives its
drivers. The gh-pr probe was written for a script cron and is now also driven
in-process by the auto-nudge scheduler; a probe owned by one driver would have
had to be copied for the second, and two copies of a classifier drift.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.probes.gh_pr import PrWatchProbe

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.irq import Probe

#: The subject kinds a driver can ask for by name.
GH_PR = "gh-pr"


def build(kind: str) -> "Probe | None":
    """Return a fresh probe for *kind*, or ``None`` when nothing observes it.

    ``None`` is a supported answer, not an error: a monitor whose subject has no
    probe must degrade to whatever schedule its driver already had, never to
    silence. The caller decides that; this function only reports capability.

    Deliberately a lazy branch rather than a registration API. There is one kind
    and one caller, so ``register()`` / ``kinds()`` would be an interface with no
    user -- and the shape of a registry is best decided by the second probe's
    real needs rather than guessed before it exists.
    """
    if kind == GH_PR:
        return PrWatchProbe()
    return None
