"""What the enterprise governance ceiling permits a YOLO grant: its duration members and its mode.

The ``yolo_duration`` scope's ``permanent`` and ``until_shutdown`` members decide
whether a declared grant may stand without expiry and whether an ad-hoc grant may
last until shutdown (``declared_grant_permitted``, ``until_shutdown_permitted``);
the ``approval_modes`` scope decides whether ``yolo`` may be selected at all
(``approval_mode_permitted``). Each question is asked of the HOST profile, fail
closed, through governance imports deferred to the call.

:mod:`kiro_crew.safety_override` stays the import path and the patch surface for every
name here, and this module logs under that name. It imports no other part of the
safety override.
"""

from __future__ import annotations

import logging

# Logs under the facade's name: a record names the import path callers use.
logger = logging.getLogger("kiro_crew.safety_override")


_PERMANENT_MEMBER = "permanent"
_UNTIL_SHUTDOWN_MEMBER = "until_shutdown"
_GOVERNANCE_SCOPE = "yolo_duration"
_APPROVAL_MODES_SCOPE = "approval_modes"
_YOLO_MODE = "yolo"


def _duration_member_permitted(member: str) -> bool:
    """Ask the enterprise ceiling whether a duration member may be selected.

    Evaluated against the HOST profile (these are gateway-level decisions, not
    per-session ones) with ``fail_closed=True``, so a governance-evaluation error
    DENIES the riskier duration rather than silently granting it. With no policy
    configured — the standalone default — an ungoverned scope permits, so a solo
    operator's config is honoured.
    """
    # Deferred import: keeps this module free of a governance/config dependency
    # at import time (it is imported very early by the security/hook layers), so
    # no import cycle is possible regardless of which entrypoint loads first.
    try:
        from kiro_crew.platform.governance_profiles import (
            HOST_SESSION_KEY,
            governance_permits,
        )
    except Exception:
        logger.debug("governance layer unavailable; permitting %s", member, exc_info=True)
        return True
    decision = governance_permits(
        _GOVERNANCE_SCOPE,
        member,
        session_key=HOST_SESSION_KEY,
        fail_closed=True,
    )
    return bool(getattr(decision, "permitted", False))


def declared_grant_permitted() -> bool:
    """True when policy allows a DECLARED grant to persist without expiry.

    ``dangerouslySkipPermissions: true`` is the operator's standing instruction,
    but on a managed fleet an admin must be able to forbid a never-expiring
    grant. Denying the ``permanent`` member of the ``yolo_duration`` scope forces
    a declared grant back onto the ordinary ad-hoc duration.
    """
    return _duration_member_permitted(_PERMANENT_MEMBER)


def until_shutdown_permitted() -> bool:
    """True when policy allows the ad-hoc ``until_shutdown`` duration."""
    return _duration_member_permitted(_UNTIL_SHUTDOWN_MEMBER)


def _non_deniable_approval_modes() -> tuple[str, ...]:
    """Modes the ``approval_modes`` scope may never forbid, read from the catalog.

    Read rather than hardcoded so this function and the parse-time refusal cannot
    drift apart: the catalog entry is the single declaration of what is deniable.
    Falls back to the interactive floor alone if the governance layer is missing,
    which is the safe direction -- it only ever makes this check consult policy for
    MORE modes, never fewer.
    """
    try:
        from kiro_crew.platform.governance import SCOPE_CATALOG

        spec = SCOPE_CATALOG.get(_APPROVAL_MODES_SCOPE)
        return tuple(getattr(spec, "always_permitted", ()) or ("normal",))
    except Exception:
        logger.debug("catalog unavailable; assuming only 'normal' is non-deniable")
        return ("normal",)


def approval_mode_permitted(mode: str) -> bool:
    """True when policy allows the dashboard approval *mode* to be selected.

    Backed by the ``approval_modes`` deny-list scope, e.g.
    ``{"approval_modes": {"mode": "deny", "deny": ["yolo"]}}``.

    A **non-deniable** mode short-circuits to True without consulting governance at
    all. That is what makes "non-deniable" mean the same thing at runtime as it does
    at parse time: the parse-time refusal stops an admin from WRITING such a deny,
    and this stops a governance-evaluation error from producing one anyway. Without
    it the ``fail_closed=True`` below could deny ``trust`` on a resolve error --
    refusing a mode whose enforcement this scope does not even implement, which
    surfaced as an unrelated trust grant silently failing.

    Everything else is evaluated against the HOST profile with ``fail_closed=True``,
    so a governance-evaluation error denies the riskier auto-approve mode rather than
    silently granting it. With no policy configured the scope is ungoverned and
    permits every mode, so a solo operator's picker is unchanged.
    """
    if mode in _non_deniable_approval_modes():
        return True
    try:
        from kiro_crew.platform.governance_profiles import (
            HOST_SESSION_KEY,
            governance_permits,
        )
    except Exception:
        logger.debug("governance layer unavailable; permitting mode %s", mode, exc_info=True)
        return True
    decision = governance_permits(
        _APPROVAL_MODES_SCOPE,
        mode,
        session_key=HOST_SESSION_KEY,
        fail_closed=True,
    )
    return bool(getattr(decision, "permitted", False))
