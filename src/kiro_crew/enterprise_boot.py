"""Enterprise boot: no kiro sign-in or no governance rules, no Crew.

Design: ``docs/request-for-change/rfc-crew-enterprise-edition.md``, section
"Enterprise gate" and D4. In an enterprise build (``edition.is_enterprise()``) the
gateway checks both before its HTTP server binds, and exits non-zero with a message
naming what failed and how to fix it. It never serves a locked gateway for a failed
boot. The one exception is a ``gateway`` start with nobody signed in: it first
serves a sign-in-only app (``kiro_crew.enterprise_signin_gateway``), then boots
again and exits here on any failure. An ``oss`` build does nothing here.

* One sign-in (D4): :func:`kiro_crew.enterprise_sign_in.resolve`, Crew's own vault
  sign-in, or kiro-cli's when the vault holds none. The rules fetch reads it first, so the sign-in check and
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
    sign_in.CAUSE_SIGN_INS_DISAGREE: (
        "Kiro Crew and kiro-cli are signed in to different accounts. Sign one of them "
        f"in to the other's account, or sign one out, {_RESTART}."
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


#: The fix when the KAS relay's kiro-cli is too old to serve the KAS engine.
KAS_FIX_UPDATE_KIRO_CLI = (
    "Run `kiro-cli update` to get a kiro-cli that supports `--agent-engine v3`, " f"{_RESTART}."
)

#: The same, when Kiro Crew's own engine could run here instead (the bundle route).
KAS_FIX_UPDATE_KIRO_CLI_OR_BUNDLE = (
    "Run `kiro-cli update` to get a kiro-cli that supports `--agent-engine v3`, or set "
    f"`agent.kas_bundle` to true so KAS runs on Kiro Crew's own engine, {_RESTART}."
)

#: The fix when the bundle route was chosen automatically (kiro-cli missing or signed
#: out) and its engine cannot run on this machine.
KAS_FIX_ENGINE_UNAVAILABLE = (
    "Install kiro-cli and sign in to it (`kiro-cli login`) so KAS runs through its "
    f"relay, {_RESTART}."
)

#: The same, when ``agent.kas_bundle`` is on: that setting keeps the bundle route
#: whatever kiro-cli can do, so it must be turned off for the relay to be used.
KAS_FIX_ENGINE_UNAVAILABLE_CONFIGURED = (
    "Set `agent.kas_bundle` to false, and install kiro-cli and sign in to it "
    f"(`kiro-cli login`) so KAS runs through its relay, {_RESTART}."
)

#: The fix when the check itself failed, so the cause is not known.
KAS_FIX_UNKNOWN = f"Run `kirocrew doctor` to see why KAS cannot start, {_RESTART}."


def _bundle_can_run() -> bool:
    """Whether Kiro Crew's own engine is installed, or installable on this machine."""
    from kiro_crew.agent_sdk import backend_install, kas_bundle

    return kas_bundle.installed_bundle() is not None or backend_install._engine_installable_here()


def kas_unrunnable(*, kas_route: object = None) -> tuple[str, str] | None:
    """``(failed, fix)`` when KAS is PROVEN unable to start on this host, else ``None``.

    KAS has two routes (``acp/kas_route.py``): Crew's own engine bundle, or
    kiro-cli's ACP relay. This checks the route the next KAS spawn takes, by the same
    rule the spawn applies. On the bundle route KAS can start when the pinned engine
    is installed or this host can run the engine the gateway would install. On the
    relay it can start unless kiro-cli's ``acp --help`` RAN and does not offer the
    engine. An unknown verdict (a failed spawn) is not proof and answers ``None``:
    that spawn's own error then names the cause. The fix names the other route when
    that one could help. Blocking: reads config and the sign-in stores, and spawns
    one bounded ``acp --help``. *kas_route* is a test seam.
    """
    from kiro_crew.agent_sdk import kas_bundle, kas_engine_verdict

    route = kas_bundle.kas_route() if kas_route is None else kas_route
    if getattr(route, "bundle", False):
        if _bundle_can_run():
            return None
        # Not chosen automatically means ``agent.kas_bundle`` keeps this route.
        configured = not getattr(route, "automatic", False)
        return (
            "KAS cannot start: its engine is not installed and this machine cannot run it",
            KAS_FIX_ENGINE_UNAVAILABLE_CONFIGURED if configured else KAS_FIX_ENGINE_UNAVAILABLE,
        )
    offers_kas, reason = kas_engine_verdict()
    if offers_kas is False and reason:
        fix = KAS_FIX_UPDATE_KIRO_CLI_OR_BUNDLE if _bundle_can_run() else KAS_FIX_UPDATE_KIRO_CLI
        return f"KAS cannot start: {reason}", fix
    return None


def _main_runs_kas(cfg: object) -> bool:
    """Whether this install's MAIN sessions run on KAS, through the one selection gate.

    Only the main backend decides the gateway start. Absent, it defaults to KAS, the
    enterprise default this check exists for. A policy-allowed Kiro CLI pick keeps the
    start (the decision entry "A Kiro CLI harness choice runs on Kiro Crew's KAS path")
    whatever the member backend says: a session that does run KAS is refused at its own start
    (:func:`refuse_unstartable_kas`). Resolved through ``resolve_selected_backend``,
    because a value the policy denied degrades to the floor (KAS).
    """
    from kiro_crew.acp_backends import ACP_BACKEND_KAS, resolve_selected_backend

    agent = getattr(cfg, "agent", None)
    return resolve_selected_backend(getattr(agent, "acp_backend", None)) == ACP_BACKEND_KAS


#: Why KAS cannot start on this host, recorded at boot when the main sessions do not
#: run KAS, so the start goes on. Any later session whose backend is KAS (a crewmate
#: DM or worker, a ``spawn_run(backend=...)`` override, a main backend switched to KAS)
#: is refused at its own start with this ``(failed, fix)`` by
#: :func:`refuse_unstartable_kas`, instead of spawning a KAS that cannot run. ``None``
#: when KAS was not proven unable to start, or nothing was checked.
_kas_refusal: tuple[str, str] | None = None


def kas_refusal() -> tuple[str, str] | None:
    """The ``(failed, fix)`` recorded for sessions on KAS, or ``None``."""
    return _kas_refusal


def require_kas_harness(cfg: object, *, test_mode: bool = False) -> None:
    """In an enterprise build, exit when the main sessions run KAS and it cannot start.

    RFC ``rfc-crew-enterprise-edition.md`` D6: KAS is the enterprise harness and the
    ``agent_backend`` floor, and a KAS proven unable to start on its route refuses the
    start instead of falling back to the Kiro CLI harness. The start is refused only
    when the MAIN backend runs KAS (:func:`_main_runs_kas`); a check that raises then
    refuses too, like :func:`require_kiro_login`.

    Otherwise the start goes on and the verdict is recorded in :func:`kas_refusal`,
    which refuses each later session on KAS at its own start. A check that raises
    there records nothing: an unknown is not proof.

    The record is cleared first, so each boot judges this host afresh. Skipped in
    ``--test-mode`` (*test_mode*), as the gateway's KAS floor probe is: an offline test
    boot never probes the developer's kiro-cli. Does nothing in an ``oss`` build.
    Blocking: one bounded ``acp --help`` probe (``_KAS_HELP_PROBE_TIMEOUT_SECS``) on
    the relay route.
    """
    global _kas_refusal
    _kas_refusal = None
    if not edition.is_enterprise() or test_mode:
        return
    try:
        main_kas = _main_runs_kas(cfg)
    except Exception as exc:
        logger.warning("Enterprise boot KAS role check raised", exc_info=True)
        refuse_boot(f"KAS could not be checked ({type(exc).__name__})", KAS_FIX_UNKNOWN)
    try:
        refusal = kas_unrunnable()
    except Exception as exc:
        logger.warning("Enterprise boot KAS check raised", exc_info=True)
        if main_kas:
            refuse_boot(f"KAS could not be checked ({type(exc).__name__})", KAS_FIX_UNKNOWN)
        return
    if main_kas:
        if refusal is not None:
            refuse_boot(*refusal)
        return
    _kas_refusal = refusal
    if refusal is not None:
        logger.warning(
            "%s. Kiro Crew starts, because its main sessions do not run on KAS; "
            "sessions that run on KAS are refused until this is fixed. %s",
            refusal[0],
            refusal[1],
        )


def refuse_unstartable_kas() -> None:
    """Raise :class:`~kiro_crew.crewmate_harness.KasUnavailable` when boot recorded that
    KAS cannot start on this host (:func:`kas_refusal`).

    The provider factory calls it for a session whose selected backend is KAS, after
    the selection gate and before any process exists, so the gate keeps its
    never-raises contract (H3) and the Kiro CLI path gains nothing (H13). Same refusal
    code as an unstartable crewmate pin (H17), so every caller that handles that
    refusal handles this one. Nothing falls back to the Kiro CLI harness. In-memory
    only; does nothing in an ``oss`` build or when nothing was recorded.
    """
    if not edition.is_enterprise():
        return
    refusal = _kas_refusal
    if refusal is None:
        return
    from kiro_crew.crewmate_harness import KasUnavailable

    raise KasUnavailable(*refusal)


async def require_kiro_login(*, assume_ready: bool = False) -> KiroPrerequisiteService | None:
    """In an enterprise build, exit unless the one enterprise sign-in resolves now.

    Reads :func:`kiro_crew.enterprise_sign_in.resolve` (Crew's vault, else kiro-cli's), the
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
