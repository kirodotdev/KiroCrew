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

``expired_host_credential`` is the same seam's check before unattended work: the
cron callback asks it before a scheduled agent turn starts, and waits at most
``_CREDENTIAL_FRESHNESS_TIMEOUT_SECS`` for the answer, less when the run's own
budget is shorter.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from kiro_crew.platform import current_context, safe_context_call

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import CredentialFreshness

logger = logging.getLogger(__name__)

#: How long the cron callback waits for the host sign-in reading. A probe that
#: has not answered by then holds nothing, so a wedged probe cannot stall a
#: scheduled run's start or spend its wake budget.
_CREDENTIAL_FRESHNESS_TIMEOUT_SECS = 10.0


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


async def expired_host_credential(
    budget: Optional[float] = None,
) -> Optional["CredentialFreshness"]:
    """The host sign-in reading when it says expired, else ``None``.

    Read from ``current_context().identity.credential_freshness()`` on a worker
    thread, since a companion's probe may run a subprocess. Through
    ``safe_context_call`` (fallback ``None``): a probe that fails holds nothing,
    while a ``PlatformCompositionError`` still propagates fail-closed. A probe
    that does not answer within ``_CREDENTIAL_FRESHNESS_TIMEOUT_SECS``, or within
    *budget* seconds when that is shorter, holds nothing either; its thread is
    left to finish on its own.
    """
    probe = asyncio.to_thread(
        safe_context_call,
        lambda: current_context().identity.credential_freshness(),
        fallback=None,
        log_message="identity.credential_freshness unavailable; not holding the run",
    )
    timeout = _CREDENTIAL_FRESHNESS_TIMEOUT_SECS
    if budget is not None:
        timeout = min(timeout, budget)
    try:
        reading = await asyncio.wait_for(probe, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "the host sign-in check gave no answer within %ss; not holding the run",
            timeout,
        )
        return None
    return reading if reading is not None and reading.expired else None
