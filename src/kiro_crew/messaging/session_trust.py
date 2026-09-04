"""Per-session tool Trust — one set, shared by every channel.

"Trust the rest of this session's tools" is a channel-neutral grant: the
``TurnDriver``'s ``auto_approve_session`` predicate is what consumes it, and the
button that writes it is just a widget. It lived in ``slack/handler.py`` only
because Slack's approval prompt was the first to offer one, which meant a second
channel could not read it — ``messaging`` may not import ``slack``.

Distinct from global YOLO (``safety_override``) in scope and in lifetime: this
grant covers ONE session and is held in memory only, so it dies with the process.
It does not weaken the PreToolUse gate — the sensitive-path keystone, the
governance ceiling and the deny-list all run ahead of the approval ladder in
``TurnDriver``, so a hard DENY still refuses a trusted session's tool.

Named ``session_trust`` rather than ``trust`` because "trust" in a messaging
package reads as connection admission — WHICH principals may attach — and that is
a different, operator-owned decision with a different failure mode. This module
answers only "may THIS session's remaining tools skip the prompt", so an
admission roster can keep the shorter name without either being mistaken for the
other.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.messaging.link import telemetry_channel_of

if TYPE_CHECKING:
    from kiro_crew.session import SessionManager

logger = logging.getLogger(__name__)

#: Granted session key -> the ``SessionManager`` the grant was propagated through
#: (``None`` when the caller supplied none). In memory only, deliberately: an ad-hoc
#: auto-approve grant must not survive a restart.
#:
#: A mapping rather than a set because the grant has TWO halves (see
#: :func:`add_trusted_session`) and revocation has to undo both. Remembering the
#: manager here is what makes :func:`clear_trusted_sessions` complete without its
#: caller having to hand one back: a revocation API that needs an argument to be
#: thorough is one that eventually gets called without it, and a PARTIAL revoke is
#: worse than none, because it reports success while leaving subagents trusted.
_trusted_sessions: dict[str, Any] = {}


def is_session_trusted(session_key: str) -> bool:
    """Whether *session_key* has been granted per-session Trust."""
    return bool(session_key) and session_key in _trusted_sessions


def add_trusted_session(session_key: str, sessions: "SessionManager | Any | None" = None) -> None:
    """Grant per-session Trust for *session_key*.

    Two halves, and both are load-bearing. The in-memory grant is what
    ``TurnDriver``'s ``auto_approve_session`` predicate reads. The approval policy
    is what a SUBAGENT reads: a spawned child consults its parent's
    ``approval_policy``, never this mapping, so without the second write a trusted
    session's children still stop to ask.

    The manager is remembered so :func:`clear_trusted_sessions` can undo the second
    half too.
    """
    if not session_key:
        return
    _trusted_sessions[session_key] = sessions
    if sessions is None:
        return
    try:
        sessions.set_approval_policy(session_key, "auto")
    except Exception:
        # The SURFACE, not the key: a session key carries the peer's platform id, and
        # naming the channel is what makes this line actionable anyway. Same shape
        # `session.py` and `task_executor.py` use for a session's surface.
        logger.warning(
            "Failed to propagate trust approval policy on %s",
            telemetry_channel_of(session_key),
            exc_info=True,
        )


def clear_trusted_sessions(keep_policy: "set[str] | None" = None) -> None:
    """Drop every grant, INCLUDING the approval policy each one wrote.

    *keep_policy* names session keys whose approval policy must be left alone. The
    grant itself is always dropped; only the policy write is skipped. It exists for
    one caller: the dashboard's override-expiry, which holds STANDING per-slot trust
    that is a separate, longer-lived decision from an ad-hoc Trust press and is
    deliberately preserved across an expiry. Resetting it here would revoke a grant
    this function never made.

    Supplied by the caller rather than read here, because the slots live in
    ``dashboard`` and this module must not import it. Omitting it errs toward MORE
    revocation, which is the safe direction for a forgotten argument.

    Symmetric with :func:`add_trusted_session` by construction, which is the point.
    Clearing only the in-memory mapping would leave every granted session's policy
    at ``auto``, so a later ``/spawn`` would read a trusted parent and skip the
    approval prompt for a grant that had been revoked. The empty string is the same
    value the dashboard's own untrust toggle writes, so the two revocation paths
    agree.

    Restoring the DEFAULT rather than a remembered previous value is deliberate:
    Trust is only offered from an interactive prompt, so the pre-grant policy was
    the default by construction, and storing one would add a second thing to keep
    correct for no reachable case.

    Best-effort per session and it always empties the mapping: a manager that
    raises must not leave the rest of the grants standing.
    """
    granted = list(_trusted_sessions.items())
    _trusted_sessions.clear()
    preserved = keep_policy or set()
    for session_key, sessions in granted:
        if sessions is None or session_key in preserved:
            continue
        try:
            sessions.set_approval_policy(session_key, "")
        except Exception:
            logger.warning(
                "Failed to revoke trust approval policy on %s",
                telemetry_channel_of(session_key),
                exc_info=True,
            )
