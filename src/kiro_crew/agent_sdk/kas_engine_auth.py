"""Edition hook: how Crew's own KAS engine bundle gets its credential.

By default Crew is the engine's auth owner: the bundle argv carries
``KAS_BUNDLE_AUTH_ARG`` and Crew answers the engine's credential callback
(``host_auth=True``). An edition that runs Crew on a host which hands the engine a
credential of its own answers one through the platform seam
(``DashboardContributor.kas_engine_auth()``). While the ``crew_client`` feature
preview is on and it answers an :class:`EngineAuth`, a bundle spawn:

- runs :meth:`EngineAuth.preflight` before any install, so a refused spawn starts
  no engine;
- replaces ``KAS_BUNDLE_AUTH_ARG`` with :meth:`EngineAuth.engine_args`;
- answers no credential callback (``host_auth=False``) and strips Crew's own API
  key from the engine's environment, then lets :meth:`EngineAuth.scrub_env` drop
  more;
- is refused on the kiro-cli relay route, which has no such mode;
- reports a refused start with :meth:`EngineAuth.start_refusal` instead of a
  sign-in prompt.

Off, or with no provider, or when the provider answers None, nothing here changes
a spawn: Crew behaves as on ``main``. Crew holds no flag spelling, host or token
of any provider; the edition supplies all of them.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Protocol

logger = logging.getLogger(__name__)


class EngineAuthRefused(Exception):
    """A spawn the provider refuses. The message is shown to the user as written."""


class EngineAuth(Protocol):
    """One spawn's engine credential, as an edition's provider answers it."""

    @property
    def relay_refusal(self) -> str:
        """Why the kiro-cli relay route cannot run under this credential."""

    def preflight(self) -> None:
        """Raise :class:`EngineAuthRefused` when the spawn cannot start. Blocking."""

    def engine_args(self) -> list[str]:
        """The engine's auth arguments, in place of ``KAS_BUNDLE_AUTH_ARG``."""

    def scrub_env(self, env: MutableMapping[str, str]) -> None:
        """Drop from the engine's *env* what it must not see."""

    def start_refusal(self, text: str, code: object) -> str:
        """The user's text for a start the engine refused, or ``""`` when not ours."""


#: The credential the last KAS spawn on this process ran under, set by the harness at
#: each spawn and read on the loop, where config is never read.
_last_spawn: EngineAuth | None = None


def edition_engine_auth() -> EngineAuth | None:
    """The edition's ``DashboardContributor.kas_engine_auth()``; None by Default.

    Blocking (an edition may read config); call off the loop. Never raises: a
    contributor that fails is logged and read as None, the default route.
    """
    from kiro_crew.platform import safe_context_call
    from kiro_crew.platform.context import installed_context

    ctx = installed_context()
    if ctx is None:
        return None
    auth = safe_context_call(
        lambda: ctx.dashboard.kas_engine_auth(),
        fallback=None,
        log_message="dashboard.kas_engine_auth failed; using Crew's sign-in",
    )
    return auth if auth is not None else None


def active_engine_auth() -> EngineAuth | None:
    """The credential a new KAS spawn runs under, while the ``crew_client`` preview is on.

    The edition's (:func:`edition_engine_auth`) first; else Crew's own machine mode
    (``agent_sdk/kas_machine.py``), which answers None unless ``agent.kas_auth`` is
    ``machine``. None while the preview is off. Blocking; call off the loop. Never
    raises: a provider that fails reads as None, Crew's own sign-in.
    """
    from kiro_crew.crew_client_preview import preview_on

    if not preview_on():
        return None
    auth = edition_engine_auth()
    if auth is not None:
        return auth
    from kiro_crew.agent_sdk.kas_machine import builtin_engine_auth

    try:
        return builtin_engine_auth()
    except Exception:  # noqa: BLE001 -- a broken config keeps the default route
        logger.warning("KAS machine mode unreadable; using Crew's sign-in", exc_info=True)
        return None


def cold_spawn_refused(auth: EngineAuth | None, config: object = None) -> bool:
    """Whether a NEW KAS spawn would refuse before it starts, as ``KasHarness`` does.

    The turn gate's mirror of the spawn's own refusals, so a destructive rerun is
    refused before history is cut: with no engine credential, a stored sign-in on an
    edition's own start URL (no local agent may run on it); with one, a configured
    route that is not Crew's engine bundle (the relay takes no engine credential).
    *config* is the applied configuration when the caller has it. Blocking; never
    raises (an unreadable configuration refuses).
    """
    try:
        if auth is None:
            from kiro_crew.auth.login.endpoints import stored_sign_in_registered

            return stored_sign_in_registered()
        bundle = getattr(getattr(config, "agent", None), "kas_bundle", None)
        if not isinstance(bundle, bool):
            from kiro_crew.config.loader import KiroCrewConfig

            bundle = bool(KiroCrewConfig.load().agent.kas_bundle)
        return not bundle
    except Exception:  # noqa: BLE001 -- unknown means refuse the rerun, never cut history
        logger.debug("KAS spawn refusal unreadable; refusing the turn", exc_info=True)
        return True


def turn_ready(auth: EngineAuth) -> bool:
    """Whether a turn can run under *auth*: its preflight passes. Blocking; never raises."""
    try:
        auth.preflight()
    except EngineAuthRefused:
        return False
    except Exception:  # noqa: BLE001 -- an edition bug is not ready, never a crash
        logger.warning("KAS engine credential preflight failed", exc_info=True)
        return False
    return True


def with_engine_args(argv: list[str], auth: EngineAuth) -> list[str]:
    """*argv* with ``KAS_BUNDLE_AUTH_ARG`` replaced by the provider's arguments."""
    from kiro_crew.acp.kas_transport import KAS_BUNDLE_AUTH_ARG

    if KAS_BUNDLE_AUTH_ARG not in argv:
        raise ValueError("the bundle argv carries no auth argument to replace")
    at = argv.index(KAS_BUNDLE_AUTH_ARG)
    return [*argv[:at], *auth.engine_args(), *argv[at + 1 :]]


def note_spawn(auth: EngineAuth | None) -> None:
    """Record the credential the spawn now starting runs under."""
    global _last_spawn
    _last_spawn = auth


def start_refusal(backend: str, text: str, code: object) -> str:
    """The provider's text for a refused KAS start; ``""`` on every other start."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    auth = _last_spawn
    if backend == ACP_BACKEND_KAS and auth is not None:
        try:
            return auth.start_refusal(text, code) or ""
        except Exception:  # noqa: BLE001 -- the generic sign-in text still answers
            logger.warning("KAS engine credential provider could not word a refusal", exc_info=True)
    return ""
