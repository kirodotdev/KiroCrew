"""What the enterprise governance ceiling permits a YOLO grant: its duration members and its mode.

The ``yolo_duration`` scope's ``permanent`` and ``until_shutdown`` members decide
whether a declared grant may stand without expiry and whether an ad-hoc grant may
last until shutdown (``declared_grant_permitted``, ``until_shutdown_permitted``);
the ``approval_modes`` scope decides whether a mode may be selected at all
(``approval_mode_permitted``). The duration members and ``yolo`` are asked of the
HOST profile, fail closed, through governance imports deferred to the call. ``trust``
and ``trust_reads`` go through ``approval_mode_policy.session_mode_permitted``, which
reads the same scope from the installed policy only.

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


def yolo_ttl_cap() -> int:
    """Seconds an auto-approve grant may last under the policy; 0 means no cap.

    The policy's ``yolo_max_seconds`` (RFC crew-lts-edition, appendix row
    "Auto-approval duration"). With none, an LTS build caps at
    ``governance_parts.baseline.LTS_YOLO_MAX_SECONDS`` and an ``oss`` build
    does not cap, as before. A memory read of the installed ceiling, so it is safe on the event
    loop. Unreadable: the LTS cap there, no cap in ``oss``.
    """
    from kiro_crew.edition import is_lts

    lts = is_lts()
    try:
        from kiro_crew.platform.context import current_context

        cap = getattr(current_context().governance, "yolo_max_seconds", 0)
    except Exception:
        logger.debug("yolo_max_seconds unavailable", exc_info=True)
        cap = 0
    if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
        return cap
    if not lts:
        return 0
    from kiro_crew.platform.governance_parts.baseline import LTS_YOLO_MAX_SECONDS

    return LTS_YOLO_MAX_SECONDS


def declared_grant_permitted() -> bool:
    """True when policy allows a DECLARED grant to persist without expiry.

    ``dangerouslySkipPermissions: true`` is the operator's standing instruction,
    but on a managed fleet an admin must be able to forbid a never-expiring
    grant. Denying the ``permanent`` member of the ``yolo_duration`` scope forces
    a declared grant back onto the ordinary ad-hoc duration, and so does a
    ``yolo_max_seconds`` cap (:func:`yolo_ttl_cap`): no grant may outlast it.
    """
    if yolo_ttl_cap():
        return False
    return _duration_member_permitted(_PERMANENT_MEMBER)


def until_shutdown_permitted() -> bool:
    """True when policy allows the ad-hoc ``until_shutdown`` duration.

    False under a ``yolo_max_seconds`` cap, which no untimed grant can meet.
    """
    if yolo_ttl_cap():
        return False
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

    Backed by the ``approval_modes`` scope, e.g.
    ``{"approval_modes": {"mode": "deny", "deny": ["yolo"]}}``. A mode id Crew does
    not know is refused.

    ``normal`` is the **non-deniable** floor: it short-circuits to True without
    consulting governance at all. That is what makes "non-deniable" mean the same
    thing at runtime as it does at parse time: the parse-time refusal stops an admin
    from WRITING such a deny, and this stops a governance-evaluation error from
    producing one anyway.

    ``trust`` and ``trust_reads`` return
    :func:`kiro_crew.approval_mode_policy.session_mode_permitted`, so this function
    and every place that sets or honours them give one answer: only the installed
    policy's rule narrows them, and with no such rule they stay permitted.

    ``yolo`` is evaluated against the HOST profile with ``fail_closed=True``, so a
    governance-evaluation error denies the riskier auto-approve mode rather than
    silently granting it. With no policy configured the scope is ungoverned and
    permits it, so a solo operator's picker is unchanged.
    """
    from kiro_crew.approval_mode_policy import (
        KNOWN_APPROVAL_MODES,
        SESSION_TRUST_MODES,
        session_mode_permitted,
    )

    if mode not in KNOWN_APPROVAL_MODES:
        return False
    if mode in _non_deniable_approval_modes():
        return True
    if mode in SESSION_TRUST_MODES:
        return session_mode_permitted(mode)
    try:
        from kiro_crew.platform.governance_profiles import (
            HOST_SESSION_KEY,
            governance_permits,
        )
    except Exception:
        # Fail closed, like the evaluation below: a deniable mode is the riskier
        # auto-approve one, and a broken governance layer cannot vouch for it.
        logger.warning("governance layer unavailable; refusing mode %s", mode, exc_info=True)
        return False
    decision = governance_permits(
        _APPROVAL_MODES_SCOPE,
        mode,
        session_key=HOST_SESSION_KEY,
        fail_closed=True,
    )
    return bool(getattr(decision, "permitted", False))
