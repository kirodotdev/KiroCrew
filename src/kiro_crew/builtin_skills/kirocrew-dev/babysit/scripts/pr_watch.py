"""Zero-token PR watch: the cron driver for the packaged gh-pr probe.

Polls one pull request with ``gh`` and stays SILENT while nothing needs a
brain: a pure-watch tick costs no tokens at all. Only an unexpected state
raises a wake, which the gateway delivers into the dashboard session that
armed the cron as a real agent turn -- the woken agent reads its session work
ledger (when available), handles the signal, and goes back to sleep while the
watch keeps running. A terminal state (merged / closed) removes the job.

This file is a DRIVER and holds no judgment of its own. The two halves it
composes both live in the package:

- :mod:`kiro_crew.irq` -- state persistence, per-head reset, time-bounded
  dedupe, the convergence coalescing window, the consecutive-failure backstop.
- :mod:`kiro_crew.probes.gh_pr` -- the only GitHub knowledge: how to observe a
  PR, and what counts as an anomaly. Read that module for the wake reasons
  (``conflict`` / ``red:<name>`` / ``ready``), what is deliberately not watched,
  and the ``ctx.message`` JSON shape.

The probe lives in the package rather than in this file because it now has a
SECOND driver: the auto-nudge scheduler observes with the same probe in-process
via :func:`kiro_crew.irq.poll`. Keeping a copy here would have meant two
classifiers to keep identical, and the one a reader happened to open would
decide what they believed.

Arm it FROM the dashboard session that owns the babysit -- the cron captures
that session as its wake target. The copy-then-register recipe lives in ONE
place, this skill's SKILL.md ("Watch mode"), and is deliberately not repeated
here: it was written out twice, the two spellings diverged, and only one of them
resolved ``KIROCREW_HOME`` -- so the copy a reader happened to follow decided
whether the command worked. Re-copy this file whenever you re-arm: it is a thin
adapter over the installed package, so a stale copy paired with a newer gateway
is the one combination that can fail to import.
"""

from __future__ import annotations

from kiro_crew.irq import run
from kiro_crew.probes.gh_pr import PrWatchProbe

__all__ = ["PrWatchProbe", "watch"]


def watch(ctx) -> None:
    """Cron entry point. Register as ``pr_watch.py:watch``.

    ``coalesce_secs`` reaches the kernel through the probe attribute rather than
    an argument here: it is parsed out of the cron message inside
    :meth:`~kiro_crew.probes.gh_pr.PrWatchProbe.identity`, and the kernel reads
    it after that call so a malformed message is converted to ``Done`` in
    exactly one place.
    """
    run(ctx, PrWatchProbe())
