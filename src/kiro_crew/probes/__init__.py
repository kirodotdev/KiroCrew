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

from typing import TYPE_CHECKING, Callable

from kiro_crew.probes.gh_pr import PrWatchProbe
from kiro_crew.probes.work_ledger import ACCEPTED_KEY as _WORK_LEDGER_ACCEPTED_KEY
from kiro_crew.probes.work_ledger import WORK_LEDGER_KIND, WorkLedgerProbe

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.irq import Probe

#: The subject kinds a driver can ask for by name.
GH_PR = "gh-pr"
WORK_LEDGER = WORK_LEDGER_KIND

#: Terminal observation keys that mean the subject finished WELL, across every kind.
#: A driver records success or blocked from the probe's own keys -- that is what
#: ``irq.Verdict.keys`` is for -- so the vocabulary must be declared per kind rather
#: than assumed to be the pull-request one. ``merged`` is the gh-pr probe's; a
#: conductor whose every work item was ACCEPTED has succeeded, and testing only for
#: ``merged`` would persist that success as a blocked outcome.
#:
#: Membership is per OUTCOME, not per kind. The work-ledger probe reports a second,
#: equally terminal key for a ledger whose items are all closed with at least one
#: rejected or abandoned, and that key is absent here on purpose: a goal its
#: conductor rejected did not finish well, and a kind-shaped set would record it as
#: though it had.
_TERMINAL_SUCCESS_KEYS = frozenset({"merged", _WORK_LEDGER_ACCEPTED_KEY})


def terminal_succeeded(keys: object) -> bool:
    """Whether a terminal verdict's *keys* say the subject finished well.

    False for an empty or unattributable set, which is the direction
    :class:`~kiro_crew.irq.Verdict` already documents: keys are empty whenever the
    kernel could not attribute the end to a probe observation, and that must read as
    "ended, not necessarily well" rather than as success.
    """
    if not isinstance(keys, (tuple, list, set, frozenset)):
        return False
    return bool(_TERMINAL_SUCCESS_KEYS & {str(key) for key in keys})


def build(
    kind: str,
    *,
    worker_running: Callable[[str], bool] | None = None,
) -> "Probe | None":
    """Return a fresh probe for *kind*, or ``None`` when nothing observes it.

    ``None`` is a supported answer, not an error: a monitor whose subject has no
    probe must degrade to whatever schedule its driver already had, never to
    silence. The caller decides that; this function only reports capability.

    Still a branch rather than a registration API, now with two kinds. A registry
    pays for itself when a kind arrives from OUTSIDE this package -- a plugin, a
    separately shipped app -- because then no single file can name them all. Both
    kinds live here, no caller outside ``kiro_crew`` adds one, and
    ``register()`` / ``kinds()`` would be an interface with one user apiece whose
    only effect is to move this mapping somewhere a reader has to search for.
    Revisit at the third kind, or at the first one shipped from elsewhere.

    ``worker_running`` is consumed only by the work-ledger probe (see
    :mod:`kiro_crew.probes.work_ledger` for why it takes it as a value). It is
    accepted here rather than at the call site so the kind-to-probe mapping stays
    in ONE place: a driver that constructed one probe itself and asked this
    function for the other would be a second, quieter copy of this branch.
    """
    if kind == GH_PR:
        return PrWatchProbe()
    if kind == WORK_LEDGER:
        return WorkLedgerProbe(worker_running=worker_running)
    return None
