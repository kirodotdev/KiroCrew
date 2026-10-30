"""Which spawn route a KAS session takes: kiro-cli's relay or Kiro Crew's own engine.

Choosing KAS means Kiro Crew's own engine and Kiro Crew's own sign-in. The relay
(``kiro-cli acp --agent-engine v3``) is still used where it can run, because a
host that already has a working kiro-cli keeps the engine it was given. Where it
cannot run, the bundle route (:mod:`kiro_crew.acp.kas_bundle`) is used instead,
and the gateway installs the pinned engine if it is missing
(:mod:`kiro_crew.acp.kas_engine_install`). The decision is recorded in
``docs/decisions/`` as "Choosing KAS runs Kiro Crew's own engine and sign-in".

The rule, in order:

1. ``agent.kas_bundle`` on: the bundle route, as configured.
2. ``agent.acp_backend`` stored as the Kiro CLI pick (``"kiro"``): the relay. That
   pick runs on KAS only because kiro-cli itself offers v3 (the decision entry
   "A Kiro CLI harness choice runs on Kiro Crew's KAS path"), so it never moves
   onto an engine kiro-cli does not serve.
3. No kiro-cli resolves: the bundle route.
4. kiro-cli resolves, and is not the operator's own ``KIROCREW_KIRO_BIN`` (that
   binary is never second-guessed), but cannot run a session: kiro-cli's own sign-in is proven
   absent (its store read and empty, no ``KIRO_API_KEY``), and the relay cannot
   use Kiro Crew's sign-in in its place (no vault identity, or no
   ``kiro-cli-chat`` entry that skips the launcher's own sign-in check). The
   bundle route.
5. Otherwise the relay.

The switch is never silent: the harness logs it at each change of route, and the
route of the last spawn is published here for the dashboard and the sign-in
messages. ``kirocrew doctor`` and ``kirocrew kas route`` compute it fresh.

Only proof moves a spawn: a store that cannot be read, or one holding a login this
reader cannot identify, keeps the relay, whose own error then names the cause.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "REASON_CONFIGURED",
    "REASON_KIRO_CLI_LAUNCHER",
    "REASON_KIRO_CLI_SIGNED_OUT",
    "REASON_NO_KIRO_CLI",
    "KasRoute",
    "kiro_cli_signed_out",
    "last_spawn_route",
    "note_spawn_route",
    "relay_unusable_reason",
    "resolve_kas_route",
]

#: ``agent.kas_bundle`` is on.
REASON_CONFIGURED = "configured"
#: No kiro-cli resolves on this host.
REASON_NO_KIRO_CLI = "no_kiro_cli"
#: kiro-cli resolves but is not signed in, and Kiro Crew's sign-in cannot reach
#: the engine through it.
REASON_KIRO_CLI_SIGNED_OUT = "kiro_cli_signed_out"
#: The relay, entered through the kiro-cli launcher on a route that never moves
#: (the operator's ``KIROCREW_KIRO_BIN``, or a stored Kiro CLI pick): only
#: kiro-cli's own sign-in starts it, so its remedy is ``kiro-cli login``.
REASON_KIRO_CLI_LAUNCHER = "kiro_cli_launcher"

_REASON_TEXT = {
    REASON_CONFIGURED: "agent.kas_bundle is on",
    REASON_NO_KIRO_CLI: "kiro-cli is not installed",
    REASON_KIRO_CLI_SIGNED_OUT: "kiro-cli is not signed in",
    REASON_KIRO_CLI_LAUNCHER: "it needs kiro-cli's own sign-in",
}


@dataclass(frozen=True)
class KasRoute:
    """One route decision. ``reason`` is ``""`` on the relay."""

    bundle: bool
    reason: str = ""
    #: On the launcher relay: whether Crew's vault answers the engine's credential
    #: callback too, so either sign-in can be the one that failed.
    crew_auth: bool = False

    @property
    def automatic(self) -> bool:
        """Whether the bundle route was chosen because the relay cannot run."""
        return self.bundle and self.reason in (REASON_NO_KIRO_CLI, REASON_KIRO_CLI_SIGNED_OUT)

    @property
    def name(self) -> str:
        """``"bundle"`` or ``"relay"``, the spelling the dashboard and CLI print."""
        return "bundle" if self.bundle else "relay"

    def describe(self) -> str:
        """One line for a person: which engine runs and, when switched, why."""
        if not self.bundle:
            if self.reason == REASON_KIRO_CLI_LAUNCHER:
                return "KAS runs through kiro-cli's relay, which needs kiro-cli's own sign-in"
            return "KAS runs through kiro-cli's relay"
        why = _REASON_TEXT.get(self.reason, "")
        if self.automatic:
            return f"KAS runs on Kiro Crew's own engine because {why}"
        return (
            f"KAS runs on Kiro Crew's own engine ({why})"
            if why
            else ("KAS runs on Kiro Crew's own engine")
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "route": self.name,
            "automatic": self.automatic,
            "reason": self.reason,
            "summary": self.describe(),
        }


RELAY = KasRoute(bundle=False)


def kiro_cli_signed_out(*, environ: Mapping[str, str], home: Path) -> bool:
    """Whether kiro-cli's own sign-in is PROVEN absent on this host.

    True only when no ``KIRO_API_KEY`` is set (inherited or in the data home's
    ``.env``: the key is a whole kiro-cli login on its own) and kiro-cli's own
    identity store was read and holds no login. Every unknown answers False, so
    a store that will not open, or a login this reader cannot identify, keeps
    the relay. Blocking file IO, SEL-audited by the store reader.
    """
    from kiro_crew.config.loader import CRED_KIRO_API_KEY, read_env_file_credential

    if (environ.get(CRED_KIRO_API_KEY) or "").strip():
        return False
    try:
        if read_env_file_credential(CRED_KIRO_API_KEY):
            return False
        from kiro_crew.kiro_prerequisite import (
            identity_fingerprint,
            identity_store_is_relocated,
            kiro_identity_store_path,
        )

        # A store moved off the fixed anchor is not read here, so an empty
        # anchor proves nothing about the real one.
        if identity_store_is_relocated(sys.platform, home, dict(environ)):
            return False
        path = kiro_identity_store_path(sys.platform, home, dict(environ))
        definitive: list[bool] = []
        fingerprint = identity_fingerprint(path, definitive=definitive)
    except Exception:  # noqa: BLE001 - an unreadable answer proves nothing
        logger.debug("could not read kiro-cli's sign-in for the KAS route", exc_info=True)
        return False
    return fingerprint == "" and definitive == [True]


def relay_unusable_reason(
    *,
    kiro_bin: str | None,
    environ: Mapping[str, str],
    home: Path,
    vault_identity: bool,
    crew_entry_skips_launcher: bool,
) -> str:
    """Why the relay cannot run a session here (rules 3 and 4), or ``""``.

    *crew_entry_skips_launcher* is whether a Crew-owned relay spawn enters
    through ``kiro-cli-chat`` and so skips the launcher's own sign-in check.
    """
    if not kiro_bin:
        return REASON_NO_KIRO_CLI
    if is_operator_override(kiro_bin, environ):
        # The operator named this binary with ``KIROCREW_KIRO_BIN``; it may sign
        # in some other way (a wrapper, a test backend), so it is never second-guessed.
        return ""
    if vault_identity and crew_entry_skips_launcher:
        return ""
    if kiro_cli_signed_out(environ=environ, home=home):
        return REASON_KIRO_CLI_SIGNED_OUT
    return ""


def _stored_as_kiro_pick() -> bool:
    try:
        from kiro_crew.config.loader_document import acp_backend_stored_as_kiro

        return acp_backend_stored_as_kiro()
    except Exception:  # noqa: BLE001 - an unreadable document is no Kiro CLI pick
        return False


def automatic_switch_allowed() -> bool:
    """Rule 2: a stored Kiro CLI pick never leaves kiro-cli. Blocking (reads config)."""
    return not _stored_as_kiro_pick()


def resolve_kas_route(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> KasRoute:
    """The route the next KAS spawn takes, computed fresh. Blocking; never raises.

    The same rule :meth:`kiro_crew.acp.harness.kas.KasHarness.resolve_spawn`
    applies, for the callers that describe a spawn instead of making one
    (``kirocrew doctor``, ``kirocrew kas route``, the install probe). A failed
    kiro-cli lookup answers the relay, whose own error names the failure.

    """
    from kiro_crew.config.loader import KiroCrewConfig

    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    try:
        if KiroCrewConfig.load().agent.kas_bundle:
            return KasRoute(bundle=True, reason=REASON_CONFIGURED)
    except Exception:  # noqa: BLE001 - unreadable config keeps the default route rules
        logger.debug("could not read agent.kas_bundle", exc_info=True)
    if not automatic_switch_allowed():
        return RELAY
    try:
        from kiro_crew.acp.client import _resolve_kiro_bin

        kiro_bin = _resolve_kiro_bin(environ=environ, home=home)
    except Exception:  # noqa: BLE001 - a failed check is not an absent install
        return RELAY
    vault_identity = False
    skips_launcher = False
    if kiro_bin:
        try:
            from kiro_crew.auth.bridge import vault_holds_identity

            vault_identity = vault_holds_identity()
        except Exception:  # noqa: BLE001 - the vault probe never decides on a failure
            vault_identity = False
        skips_launcher = crew_entry_skips_launcher(kiro_bin, environ)
    reason = relay_unusable_reason(
        kiro_bin=kiro_bin,
        environ=environ,
        home=home,
        vault_identity=vault_identity,
        crew_entry_skips_launcher=skips_launcher,
    )
    return KasRoute(bundle=True, reason=reason) if reason else RELAY


def is_operator_override(binary: str, environ: Mapping[str, str]) -> bool:
    """Is *binary* the operator's own ``KIROCREW_KIRO_BIN`` choice?

    Compared as absolute paths, the way the resolver hands the override
    through (``snapshot_trusted_acp_executable`` returns ``abspath`` of the
    candidate). No ``realpath``: the operator named THIS path.
    """
    override = (environ.get("KIROCREW_KIRO_BIN") or "").strip()
    return bool(override) and os.path.abspath(binary) == os.path.abspath(override)


def crew_entry_skips_launcher(kiro_bin: str, environ: Mapping[str, str]) -> bool:
    """Whether a Crew-owned relay spawn of *kiro_bin* enters through ``kiro-cli-chat``.

    True when *kiro_bin* already is the chat binary (the desktop's bundled entry,
    or Windows's one ``kiro-cli.exe``), or when the harness swaps the launcher for
    its ``kiro-cli-chat`` sibling; never for the operator's ``KIROCREW_KIRO_BIN``.
    Blocking (a stat).
    """
    if is_operator_override(kiro_bin, environ):
        return False
    from kiro_crew.kiro_cli import chat_sibling
    from kiro_crew.kiro_cli_bundle import BUNDLED_KIRO_CLI_ENTRY

    # Already the chat binary (the desktop's bundled entry), or Windows, whose one
    # ``kiro-cli.exe`` is the chat binary itself: there is no launcher to skip.
    if sys.platform == "win32" or os.path.basename(kiro_bin) == BUNDLED_KIRO_CLI_ENTRY:
        return True
    return chat_sibling(kiro_bin) is not None


#: The route the last KAS spawn in this process took, ``None`` before the first.
#: One slot: the dashboard and the sign-in messages describe the engine that is
#: running, and only the latest spawn says which one that is.
_LAST_SPAWN_ROUTE: KasRoute | None = None


def note_spawn_route(route: KasRoute) -> None:
    """Record and log the route a KAS spawn took.

    Logged at WARNING when the bundle route was chosen automatically and at INFO
    otherwise, once per change of route, so the switch is in the gateway log
    without repeating at every spawn.
    """
    global _LAST_SPAWN_ROUTE
    if route == _LAST_SPAWN_ROUTE:
        return
    _LAST_SPAWN_ROUTE = route
    level = logging.WARNING if route.automatic else logging.INFO
    logger.log(level, "KAS route: %s (%s)", route.name, route.describe())


def last_spawn_route() -> KasRoute | None:
    """The route of the last KAS spawn in this process, or ``None``. No IO."""
    return _LAST_SPAWN_ROUTE
