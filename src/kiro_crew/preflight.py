"""Seam-supplied pre-launch checks for CLI commands.

``run_preflight_checks`` runs a sequence of already-resolved callables before
the ``gateway`` and ``token`` commands do any real work.  The public edition
supplies none (``DefaultIdentityProvider.preflight_checks()`` returns ``[]``),
so standalone startup is byte-identical to before; a companion returns its own
checks (e.g. an SSO-session freshness probe that prompts the user to refresh)
from its ``IdentityProvider.preflight_checks()``.

Checks are **callables sourced from the composed PlatformContext only** —
never ``module:function`` strings resolved from config.  An agent-writable
config that imported arbitrary callables at next start would be a code-exec
escalation, so no string-import machinery exists here by design.

A check may print to stderr or prompt the user.  ``SystemExit`` propagates so
a check can abort the launch; every other exception is logged and swallowed
per check so a broken check never blocks startup.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional, Sequence

from kiro_crew.platform import current_context, safe_context_call

logger = logging.getLogger(__name__)


def run_preflight_checks(checks: Optional[Sequence[Callable[[], None]]] = None) -> None:
    """Execute each pre-launch check in *checks*, in order.

    Parameters
    ----------
    checks:
        Already-resolved zero-argument callables.  If ``None``, the list is
        sourced from ``current_context().identity.preflight_checks()`` through
        the canonical ``safe_context_call`` helper (fallback ``[]``), so a
        transient context/adapter failure can never block standalone
        ``gateway``/``token`` startup — while a ``PlatformCompositionError``
        (a non-standalone host that could not compose its companion) still
        propagates fail-closed.

    ``SystemExit`` from a check propagates (a check may abort the launch);
    any other exception is logged and swallowed per check.
    """
    if checks is None:
        no_checks: list[Callable[[], None]] = []
        checks = safe_context_call(
            lambda: list(current_context().identity.preflight_checks()),
            fallback=no_checks,
            log_message="identity.preflight_checks unavailable; skipping preflight",
        )

    for check in checks:
        try:
            check()
        except SystemExit:
            raise  # allow a check to abort the launch
        except Exception:
            name = getattr(check, "__qualname__", repr(check))
            logger.warning("preflight check %s failed", name, exc_info=True)
