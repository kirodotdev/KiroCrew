"""Enterprise boot: no kiro sign-in or no governance rules, no Crew.

Design: ``docs/request-for-change/rfc-crew-enterprise-edition.md``, section
"Enterprise gate" and D4. In an enterprise build (``edition.is_enterprise()``) the
gateway checks both before its HTTP server binds, and exits non-zero with a message
naming what failed and how to fix it. It never serves a locked gateway for a failed
boot. An ``oss`` build does nothing here.

* One sign-in (D4): :func:`kiro_crew.enterprise_sign_in.resolve`, Crew's own vault
  sign-in. The rules fetch reads it first, so the sign-in check and
  the fetch are one step inside ``boot_platform``, before any port binds. A
  :class:`~kiro_crew.platform.context.PlatformCompositionError` from it reaches
  :func:`refuse_policy_boot`, which picks the fix from the recorded cause.
* :func:`require_kiro_login` re-checks the same resolver at gateway start; ``kiro-cli
  whoami`` is not the enterprise gate.

Once the gateway is up, a sign-in that is lost closes the enterprise gate
(``kiro_crew.enterprise_gate``, fed by :class:`~kiro_crew.enterprise_sign_in.SignInLoginSource`).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, NoReturn

from kiro_crew import edition
from kiro_crew import enterprise_sign_in as sign_in

if TYPE_CHECKING:
    from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

logger = logging.getLogger(__name__)

#: The exit status of a refused enterprise boot.
EXIT_CODE = 1

_RESTART = "then start Kiro Crew again"

#: The fix sentence for each refusal cause (``enterprise_sign_in.CAUSE_*``).
_FIX_FOR_CAUSE = {
    sign_in.CAUSE_NO_SIGN_IN: f"Sign in with {{sign_in}}, {_RESTART}.",
    sign_in.CAUSE_SIGN_IN_UNUSABLE: f"Sign in again with {{sign_in}}, {_RESTART}.",
    sign_in.CAUSE_UNREACHABLE: (
        f"Check that this machine can reach the governance service, {_RESTART}."
    ),
    sign_in.CAUSE_REFUSED: (
        f"Sign in again with {{sign_in}} as an account of your organisation, " f"{_RESTART}."
    ),
    sign_in.CAUSE_NOT_ELIGIBLE: (
        "Sign in with an account your organisation manages (Identity Center), using "
        f"{{sign_in}}, {_RESTART}."
    ),
    sign_in.CAUSE_ACCOUNT_CHANGED: (
        "The Kiro account changed while Kiro Crew was running. Sign back in to the "
        f"account Kiro Crew started with, or {_RESTART} as the new account."
    ),
    sign_in.CAUSE_BAD_RULES: (
        "Ask your administrator to check the organisation's Kiro Crew policy, " f"{_RESTART}."
    ),
}

_FIX_UNKNOWN = (
    "Check that this machine can reach the governance service and that you are "
    f"signed in to Kiro ({{sign_in}}), {_RESTART}."
)


def refuse_boot(failed: str, fix: str) -> NoReturn:
    """Log *failed* and *fix*, print them to stderr, and exit :data:`EXIT_CODE`.

    The desktop app writes the gateway's stderr to its launch log and shows the
    tail of that log in its start-failure window, so the same two lines reach a
    desktop user.
    """
    logger.critical("Enterprise boot refused: %s. %s", failed, fix)
    print(f"❌ Kiro Crew cannot start: {failed}.\n   {fix}", file=sys.stderr, flush=True)
    raise SystemExit(EXIT_CODE)


def fix_for(cause: str) -> str:
    """The fix sentence for a refusal *cause*; a generic one when the cause is unknown."""
    # The command names this install's launcher, resolved when it is printed.
    return _FIX_FOR_CAUSE.get(cause, _FIX_UNKNOWN).format(sign_in=sign_in.sign_in_hint())


def refuse_policy_boot(exc: BaseException) -> None:
    """Exit if *exc* is an enterprise build's failed policy load. Else return.

    Returns (so the caller re-raises as before) in an ``oss`` build and for any
    error that is not a ``PlatformCompositionError``. The fix sentence comes from the
    cause the fetch or the account lock recorded.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    if not edition.is_enterprise() or not isinstance(exc, PlatformCompositionError):
        return
    from kiro_crew.platform.enterprise_policy_fetch import last_failure_cause

    refuse_boot(
        f"the organisation's governance rules or enterprise platform could not be loaded ({exc})",
        fix_for(last_failure_cause(str(exc))),
    )


async def require_kiro_login(*, assume_ready: bool = False) -> KiroPrerequisiteService | None:
    """In an enterprise build, exit unless the one enterprise sign-in resolves now.

    Reads :func:`kiro_crew.enterprise_sign_in.resolve` (Crew's own vault sign-in), the
    same identity the rules fetch and the account lock read. Returns
    ``None`` in every build: the caller builds its own setup service, and the gate's
    login source is :class:`~kiro_crew.enterprise_sign_in.SignInLoginSource`.
    *assume_ready* (``--test-mode``) changes nothing here.
    """
    del assume_ready
    if not edition.is_enterprise():
        return None
    try:
        await asyncio.to_thread(sign_in.resolve)
    except asyncio.CancelledError:
        raise
    except sign_in.SignInError as exc:
        refuse_boot(str(exc), fix_for(exc.cause))
    except Exception as exc:
        logger.warning("Enterprise boot sign-in check raised", exc_info=True)
        refuse_boot(
            f"the Kiro sign-in could not be checked ({type(exc).__name__})",
            fix_for(sign_in.CAUSE_SIGN_IN_UNUSABLE),
        )
    return None
