"""What the grant API hands its callers: the SEL accessor, the revocation sentinel, result records.

``sel`` resolves the Security Event Log singleton at call time so a test can patch
it through the facade; ``POLICY_REVOKED_SOURCE`` is the ``source`` a policy
revocation carries; ``ActivationResult``, ``RenewResult`` and ``OverrideStatus`` are
what ``SafetyOverride`` returns.

:mod:`kiro_crew.safety_override` stays the import path and the patch surface for every
name here (:mod:`kiro_crew._facade_mirror`). This module imports no other part of the
safety override.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from kiro_crew.sel import sel as _get_sel


def sel():  # noqa: ANN201 — thin wrapper kept for test patchability
    """Return the SEL singleton.

    Defined at module level so tests can patch ``kiro_crew.safety_override.sel``.
    """
    return _get_sel()


# The ``source`` sentinel a policy revocation carries into ``deactivate`` and the
# ``on_expired`` callback. Exported as a module constant because the OTHER end of
# the contract lives in ``dashboard/server.py`` (the expiry DM words the notice by
# this value): a bare literal on both ends lets a re-spelling here silently drop
# the policy-specific wording there with every test still green.
POLICY_REVOKED_SOURCE = "policy"


# ─── Result dataclasses ──────────────────────────────────────────────────────


@dataclass
class ActivationResult:
    """Returned by SafetyOverride.activate()."""

    active: bool
    ttl: int
    source: str
    activated_at_iso: str


@dataclass
class RenewResult:
    """Returned by SafetyOverride.renew()."""

    renewed: bool
    ttl: int  # 0 if not renewed
    source: str
    reason: str = ""  # populated on denial


@dataclass
class OverrideStatus:
    """Snapshot returned by SafetyOverride.status()."""

    active: bool
    source: str
    remaining_secs: int
    activation_count: int
    activated_at_iso: Optional[str]  # None when inactive
    expires_at_iso: Optional[str]  # None when inactive
    last_renewed_at_iso: Optional[str]  # None if never renewed
    last_renewed_by: str
    # True when the live grant was DECLARED in config and has no expiry at all.
    # ``remaining_secs`` is -1 and ``expires_at_iso`` is None in that case.
    permanent: bool = False
