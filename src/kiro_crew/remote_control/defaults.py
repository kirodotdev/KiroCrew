"""The rule that decides whether this Crew hosts Remote Control.

A leaf module with no imports, so the config schema and ``edition_locks`` can read
it without pulling in the rest of the package (or ``kiro_crew.relay_client``).

Two things turn hosting on. ``tunnel.relay_enabled`` true is the personal switch,
off by default (:data:`REMOTE_CONTROL_ON_BY_DEFAULT`): a Kiro sign-in alone never
starts hosting. A lease credential named in ``tunnel.relay_lease_credential`` (a
team Crew's key or a workload identity) is explicit host setup, so it starts
hosting too, unless Remote Control was turned off, which writes
``tunnel.relay_turned_off``. The stored ``relay_enabled`` false every
``config.json`` carries cannot be told from a choice, so the off command writes
its own marker. The rule is computed at each load and never written to disk.
"""

from __future__ import annotations

#: Whether ``tunnel.relay_enabled`` is on when ``config.json`` does not set it.
REMOTE_CONTROL_ON_BY_DEFAULT: bool = False

#: The ``tunnel.relay_lease_credential`` value that leases with the Crew's own Kiro
#: sign-in (a personal Crew) instead of a file or a stored secret.
SIGN_IN_CREDENTIAL = "kiro-sign-in"


def credential_set(ref: object) -> bool:
    """Whether *ref* names a host credential: any text but empty or the sign-in.

    A malformed reference counts, so the host starts and its status names the
    problem instead of staying off with no word.
    """
    if not isinstance(ref, str):
        return False
    ref = ref.strip()
    return bool(ref) and ref != SIGN_IN_CREDENTIAL


def hosting_requested(tunnel: object) -> bool:
    """Whether the ``tunnel`` section asks this Crew to host Remote Control.

    ``is True``: a stand-in config object answers any attribute truthily.
    """
    if getattr(tunnel, "relay_enabled", False) is True:
        return True
    if getattr(tunnel, "relay_turned_off", False) is True:
        return False
    return credential_set(getattr(tunnel, "relay_lease_credential", ""))
