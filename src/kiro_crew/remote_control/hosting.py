"""Whether this Crew hosts through the Kiro relay: the one rule every reader asks.

``tunnel.relay_enabled`` has three stored values:

* ``true`` -- turned on (``kirocrew remote-control on``): host.
* ``false`` -- turned off, or stored by a build before this rule: never host.
* unset (``null`` or absent) -- nobody chose: a personal sign-in hosts, and so
  does a Crew whose credential is itself the owner's choice to host, a team
  Crew's key or a workload identity (:func:`hosts_by_default`).

So a stored value always wins: an install that stores ``false`` (an ``off``, a pod
seed, or a full save by an older build, whose default was ``false``) stays off, and
nothing migrates it. An install that stores nothing hosts after the upgrade on a
personal sign-in or a team key; ``kirocrew remote-control off`` stores ``false``.

A leaf module: it reads the config object it is handed and the process
environment, and imports nothing from the package, so
``edition_locks.relay_selected`` and the config schema can call it with no I/O.
"""

from __future__ import annotations

import os
from typing import Any

#: The ``tunnel.relay_lease_credential`` value that means the Crew's own Kiro
#: sign-in (``relay_client.lease.SIGN_IN_CREDENTIAL``; a test pins them equal).
SIGN_IN_CREDENTIAL = "kiro-sign-in"
#: The ``tunnel.relay_lease_token_type`` of a team Crew's key
#: (``relay_client.lease.TOKEN_TYPE_API_KEY``; a test pins them equal).
TOKEN_TYPE_API_KEY = "API_KEY"
#: The deployment's switch for the Crew's own workload identity
#: (``workload_identity.ENV_MODE``; a test pins them equal). Read here only to
#: decide the default; how a workload identity signs in is ``workload_identity``'s.
WORKLOAD_IDENTITY_ENV = "KIROCREW_WORKLOAD_IDENTITY"


def _text(tunnel: Any, name: str) -> str:
    value = getattr(tunnel, name, "")
    return value.strip() if isinstance(value, str) else ""


def workload_identity_named() -> bool:
    """Whether the deployment names a workload identity for this Crew.

    Any non-empty value counts, as a misconfigured one does for its sign-in: the
    Crew never falls back to a person.
    """
    return bool((os.environ.get(WORKLOAD_IDENTITY_ENV) or "").strip())


def hosts_by_default(tunnel: Any) -> bool:
    """Whether a Crew whose ``relay_enabled`` is unset hosts.

    A personal sign-in hosts: ``tunnel.relay_lease_credential`` empty or
    ``kiro-sign-in``, with no ``relay_lease_token_type`` and no workload identity
    named. So does a credential that names a team key (``relay_lease_token_type``
    is ``API_KEY``), or any named credential when the deployment names a workload
    identity: setting such a credential is the owner choosing to host. An empty
    or ``kiro-sign-in`` credential with a token type or a workload identity, and a
    named credential with any other token type, do not host by default.
    """
    credential = _text(tunnel, "relay_lease_credential")
    if not credential or credential == SIGN_IN_CREDENTIAL:
        return personal_sign_in(tunnel)
    if _text(tunnel, "relay_lease_token_type") == TOKEN_TYPE_API_KEY:
        return True
    return workload_identity_named()


def personal_sign_in(tunnel: Any) -> bool:
    """Whether the Crew leases on its own Kiro sign-in: a personal Crew.

    ``relay_lease_credential`` empty or ``kiro-sign-in``, no ``relay_lease_token_type``
    and no workload identity named.
    """
    credential = _text(tunnel, "relay_lease_credential")
    if credential and credential != SIGN_IN_CREDENTIAL:
        return False
    return not _text(tunnel, "relay_lease_token_type") and not workload_identity_named()


def is_unset(tunnel: Any) -> bool:
    """Whether nobody chose: ``relay_enabled`` is unset, so the default decides."""
    return getattr(tunnel, "relay_enabled", False) is None


def relay_requested(tunnel: Any) -> bool:
    """Whether config asks this Crew to host through the relay.

    Only the boolean ``True`` turns it on, and only ``None`` lets the default
    decide: ``False``, a string or a number left by a hand edit, and a stand-in
    object's attribute all read as off.
    """
    value = getattr(tunnel, "relay_enabled", False)
    if value is True:
        return True
    if value is None:
        return hosts_by_default(tunnel)
    return False


def hosting_by_default(tunnel: Any) -> bool:
    """Whether this Crew hosts only because of the default, which nobody chose."""
    return is_unset(tunnel) and hosts_by_default(tunnel)


def personal_by_default(tunnel: Any) -> bool:
    """Whether a personal Crew hosts only because of the default.

    Such a Crew stays quiet while it cannot host yet (no sign-in, a sign-in that
    cannot own a personal Crew, an organization with remote access off, a bind the
    relay cannot reach): the tunnel status names the reason, and the dashboard
    shows nothing for it. Someone who ran ``on`` gets today's errors.
    """
    return is_unset(tunnel) and personal_sign_in(tunnel)
