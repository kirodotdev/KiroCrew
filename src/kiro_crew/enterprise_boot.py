"""Enterprise boot: no kiro login or no governance rules, no Crew.

Design: ``docs/request-for-change/rfc-crew-enterprise-edition.md``, section
"Enterprise gate": "Start | Login and fetch rules. If either fails, exit". In an
enterprise build (``edition.is_enterprise()``) the gateway checks both before its
HTTP server binds, and exits non-zero with a message naming what failed and how
to fix it. It never serves a locked gateway for a failed boot. An ``oss`` build
does nothing here.

* The governance rules load in ``boot_platform``. A
  :class:`~kiro_crew.platform.context.PlatformCompositionError` from it reaches
  :func:`refuse_policy_boot`.
* The login check is :func:`require_kiro_login`, the setup gate's own
  ``--version`` and ``whoami`` probe (``KiroPrerequisiteService.boot_login_status``);
  there is no second login check.

Once the gateway is up, a login that expires closes the enterprise gate
(``kiro_crew.enterprise_gate``); that runtime half is not changed here.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, NoReturn

from kiro_crew import edition

if TYPE_CHECKING:
    from kiro_crew.kiro_prerequisite import KiroPrerequisiteService, PrerequisiteStatus

logger = logging.getLogger(__name__)

#: The exit status of a refused enterprise boot.
EXIT_CODE = 1

_RESTART = "then start Kiro Crew again"


def refuse_boot(failed: str, fix: str) -> NoReturn:
    """Log *failed* and *fix*, print them to stderr, and exit :data:`EXIT_CODE`.

    The desktop app writes the gateway's stderr to its launch log and shows the
    tail of that log in its start-failure window, so the same two lines reach a
    desktop user.
    """
    logger.critical("Enterprise boot refused: %s. %s", failed, fix)
    print(f"❌ Kiro Crew cannot start: {failed}.\n   {fix}", file=sys.stderr, flush=True)
    raise SystemExit(EXIT_CODE)


def refuse_policy_boot(exc: BaseException) -> None:
    """Exit if *exc* is an enterprise build's failed policy load. Else return.

    Returns (so the caller re-raises as before) in an ``oss`` build and for any
    error that is not a ``PlatformCompositionError``.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    if not edition.is_enterprise() or not isinstance(exc, PlatformCompositionError):
        return
    refuse_boot(
        f"the organisation's governance rules or enterprise platform could not be loaded ({exc})",
        "Check that this machine can reach the governance service and that you are "
        f"signed in to Kiro, {_RESTART}.",
    )


async def require_kiro_login(*, assume_ready: bool = False) -> KiroPrerequisiteService | None:
    """In an enterprise build, exit unless a kiro login is verified now.

    Returns the service whose probe saw the login, so the dashboard reuses it
    and its gate starts open on that verdict. Returns ``None`` in an ``oss``
    build, having done nothing. *assume_ready* is ``--test-mode``; the service
    ignores it in an enterprise build.
    """
    if not edition.is_enterprise():
        return None
    from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

    try:
        service = await asyncio.to_thread(KiroPrerequisiteService, assume_ready=assume_ready)
        # ``--version`` and ``whoami`` only: the spec and ``acp`` checks of a
        # full probe run after the bind, so they add nothing before it.
        status = await service.boot_login_status()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Enterprise boot login check raised", exc_info=True)
        refuse_boot(
            f"the Kiro sign-in could not be checked ({type(exc).__name__})",
            f"Run `kiro-cli whoami` to see why, {_RESTART}.",
        )
    if status.authenticated:
        return service
    _refuse_for_status(status)


def _refuse_for_status(status: PrerequisiteStatus) -> NoReturn:
    """Exit naming why *status* has no verified sign-in. Unknown is never "signed out"."""
    sso = status.sso_login_command
    sign_in = f"run `{status.login_command}`" + (f" (organisation SSO: `{sso}`)" if sso else "")
    if status.sandbox_unavailable:
        detail = status.sandbox_detail or "the sandbox could not start"
        refuse_boot(
            f"the Kiro sign-in could not be checked ({detail})",
            f"Fix the sandbox error above, {_RESTART}.",
        )
    if status.probe_timed_out:
        refuse_boot(
            "the Kiro sign-in could not be checked (kiro-cli did not answer in time)",
            f"Check that kiro-cli starts (`kiro-cli --version`), {_RESTART}.",
        )
    if status.probe_error and not status.installed:
        refuse_boot(
            f"kiro-cli did not start ({status.probe_error})",
            f"Reinstall it from {status.docs_url}, {_RESTART}.",
        )
    if status.probe_error:
        refuse_boot(
            f"the Kiro sign-in could not be checked ({status.probe_error})",
            f"Run `kiro-cli whoami` to see why, {_RESTART}.",
        )
    if not status.installed:
        refuse_boot(
            "kiro-cli was not found",
            f"Install it from {status.docs_url}, sign in ({sign_in}), {_RESTART}.",
        )
    refuse_boot("no Kiro sign-in was found", f"Sign in: {sign_in}, {_RESTART}.")
