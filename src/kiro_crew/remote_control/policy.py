"""Which Kiro sign-ins may hold personal Remote Control, and what the person is told.

Personal Remote Control publishes this Crew to its owner's own Kiro account, so it
needs a sign-in that names a person. Two kinds do not:

* an API key (``KIRO_API_KEY``, or a stored credential whose provider is
  ``ApiKey``) is a bearer with no person behind it;
* an external identity provider sign-in (``auth_method`` or provider
  ``external_idp``) is an organisation's own IdP, which the lease service cannot
  bind to a personal Kiro account.

The rule is an allowlist, not a denylist: a provider this module does not know is
refused as ``unknown`` rather than let through, so a sign-in kind added later stays
off until someone decides it may hold Remote Control.

:func:`allowed` is pure. :func:`current_decision` reads the vault (blocking file IO,
call it off the event loop) and never refreshes a token or touches the network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from kiro_crew.auth.store import KasToken

#: Providers whose sign-in names a person, so personal Remote Control may run.
PERSONAL_PROVIDERS: frozenset[str] = frozenset(
    {"BuilderId", "Google", "Github", "Enterprise", "Internal"}
)

#: Refusal codes. Each one has a sentence in :data:`NOTICES`.
REASON_API_KEY = "api_key"
REASON_EXTERNAL_IDP = "external_idp"
REASON_NOT_SIGNED_IN = "not_signed_in"
REASON_UNKNOWN_SIGN_IN = "unknown_sign_in"
REASON_LOCKED = "locked"

#: What the person reads when Remote Control is refused, keyed by reason code.
NOTICES: dict[str, str] = {
    REASON_API_KEY: (
        "Remote Control is off because Crew is signed in to Kiro with an API key. "
        "Sign in with your own Kiro account to use it."
    ),
    REASON_EXTERNAL_IDP: (
        "Remote Control is off because Crew is signed in to Kiro through an external "
        "identity provider. Sign in with your own Kiro account to use it."
    ),
    REASON_NOT_SIGNED_IN: "Remote Control is off because Crew is not signed in to Kiro.",
    REASON_UNKNOWN_SIGN_IN: (
        "Remote Control is off because Crew does not recognise this kind of Kiro " "sign-in."
    ),
    REASON_LOCKED: "Remote Control is managed by this build and cannot be turned on here.",
}


def notice_for(reason: str) -> str:
    """The sentence shown for *reason*, or ``""`` when nothing is refused."""
    return NOTICES.get(reason, "")


def allowed(token: Optional[KasToken], *, env_api_key: bool = False) -> tuple[bool, str]:
    """Whether *token* may hold personal Remote Control, and the refusal code if not.

    *token* is the vault's resolved credential, or ``None`` when the vault is empty.
    *env_api_key* says ``KIRO_API_KEY`` is set: it is the sign-in only when the
    vault is empty, the same precedence :class:`~kiro_crew.auth.provider.KasAuthProvider`
    applies. Returns ``(True, "")`` or ``(False, <reason code>)``.
    """
    if token is None:
        return (False, REASON_API_KEY) if env_api_key else (False, REASON_NOT_SIGNED_IN)
    provider = token.provider if isinstance(token.provider, str) else ""
    auth_method = token.auth_method if isinstance(token.auth_method, str) else ""
    if auth_method.lower() == "external_idp" or provider == "ExternalIdp":
        return False, REASON_EXTERNAL_IDP
    if provider == "ApiKey" or auth_method.lower() == "api_key":
        return False, REASON_API_KEY
    if provider in PERSONAL_PROVIDERS:
        return True, ""
    return False, REASON_UNKNOWN_SIGN_IN


@dataclass(frozen=True)
class Decision:
    """The sign-in verdict at one moment: ``ok`` or a refusal ``reason``."""

    ok: bool
    reason: str = ""

    @property
    def notice(self) -> str:
        return notice_for(self.reason)


def current_decision() -> Decision:
    """The verdict for this Crew's sign-in now. Blocking file IO; never raises.

    An edition that locks local tunnel enables refuses as ``locked`` before the
    sign-in is read. A vault that cannot be read is refused as ``unknown`` rather
    than read as allowed.
    """
    from kiro_crew.edition_locks import local_enable_locked

    if local_enable_locked("tunnel"):
        return Decision(False, REASON_LOCKED)
    try:
        from kiro_crew.auth.bridge import default_token_store

        token = default_token_store().resolve()
    except Exception:  # noqa: BLE001 - an unreadable vault refuses, it never allows
        return Decision(False, REASON_UNKNOWN_SIGN_IN)
    ok, reason = allowed(token, env_api_key=bool(os.environ.get("KIRO_API_KEY")))
    return Decision(ok, reason)


def current_decision_with_generation() -> tuple[Decision, tuple[str, str]]:
    """:func:`current_decision` and the markers it describes, read as one snapshot.

    A sign-in moves the sign-in marker and then saves its credential, both under the
    store's sign-out lock. Reading the marker and the vault outside that lock could
    pair the new account's marker with the old account's credential, and a caller
    caching the verdict under that marker would keep the old verdict. Read under the
    lock, the pair always describes one account. Blocking file IO; never raises.
    """
    from kiro_crew.edition_locks import local_enable_locked

    if local_enable_locked("tunnel"):
        return Decision(False, REASON_LOCKED), sign_in_generation()
    read: list[tuple[Optional[KasToken], tuple[str, str]]] = []
    try:
        from kiro_crew.auth.bridge import default_token_store

        store = default_token_store()

        def _read() -> None:
            read.append((store.resolve(), (store.signin_generation(), store.signout_generation())))

        store.with_signout_lock(_read)
    except Exception:  # noqa: BLE001 - an unreadable vault refuses, it never allows
        return Decision(False, REASON_UNKNOWN_SIGN_IN), ("", "")
    token, gen = read[0]
    ok, reason = allowed(token, env_api_key=bool(os.environ.get("KIRO_API_KEY")))
    return Decision(ok, reason), gen


def sign_in_generation() -> tuple[str, str]:
    """The vault's (sign-in, sign-out) markers: they move on every account change.

    A token refresh moves neither, so a caller that caches :func:`current_decision`
    keyed on this pair re-reads the vault only when the account may have changed.
    ``("", "")`` when the markers cannot be read.
    """
    try:
        from kiro_crew.auth.bridge import default_token_store

        store = default_token_store()
        return store.signin_generation(), store.signout_generation()
    except Exception:  # noqa: BLE001 - a missing marker reads as never written
        return "", ""
