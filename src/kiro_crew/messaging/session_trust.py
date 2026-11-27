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
from typing import TYPE_CHECKING, Any, Callable

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

#: Session key -> sender -> the binding that sender's grant was made under: an
#: opaque value the granting channel derives from the session as it runs (which
#: allocation, which agent). The sender's OWN turns on it skip the prompt while the
#: channel reads the same binding back. A narrower grant than the mapping above,
#: for a channel where several people drive one session and one of them may not
#: widen it for the rest: it covers no other sender's turn and never writes the
#: approval policy, so a spawned subagent still asks. In memory only.
_trusted_senders: dict[str, dict[str, object]] = {}


def is_sender_trusted(session_key: str, sender: str, binding: object) -> bool:
    """Whether *sender*'s own turns on *session_key*, read as *binding*, skip the prompt.

    A grant made under another binding is dropped here, not skipped, so it cannot
    come back when a later session happens to read the same.
    """
    granted = _trusted_senders.get(session_key, {}).get(sender) if sender else None
    if granted is None:
        return False
    if binding is None or granted != binding:
        remove_trusted_sender(session_key, sender)
        return False
    return trust_permitted()


def sender_binding(session_key: str, sender: str) -> object | None:
    """The binding *sender*'s grant on *session_key* was made under, or ``None``.

    One read that cannot raise: a policy deny clears every grant from another
    thread (:func:`clear_trusted_sessions`), so a caller must not check for the
    grant and then index the map.
    """
    return _trusted_senders.get(session_key, {}).get(sender)


def add_trusted_sender(session_key: str, sender: str, binding: object) -> None:
    """Grant Trust for *sender*'s own turns on *session_key* while it reads *binding*.

    Raises ``PermissionError`` when a ``permission_mode`` rule denies ``trust``, so
    the caller reports the refusal instead of a grant. No *binding* grants nothing:
    there is no running session to bind to.
    """
    if not (session_key and sender) or binding is None:
        return
    if not trust_permitted():
        logger.info("sender Trust refused by policy on %s", telemetry_channel_of(session_key))
        raise PermissionError("trust is disabled by your organization's policy")
    _trusted_senders.setdefault(session_key, {})[sender] = binding


def remove_trusted_sender(session_key: str, sender: str) -> bool:
    """Drop *sender*'s own grant on *session_key*; whether there was one."""
    senders = _trusted_senders.get(session_key)
    if not senders or senders.pop(sender, None) is None:
        return False
    if not senders:
        _trusted_senders.pop(session_key, None)
    return True


def trusted_senders(session_key: str) -> list[str]:
    """The senders holding a grant on *session_key*, sorted: what a drop's audit names."""
    return sorted(_trusted_senders.get(session_key, ()))


def drop_trusted_senders(session_key: str) -> int:
    """Drop every sender's grant on *session_key*; how many there were."""
    return len(_trusted_senders.pop(session_key, ()))


def keep_trusted_senders(keep: Callable[[str], bool]) -> int:
    """Drop, on every session, the grant of each sender *keep* is false for; how many."""
    dropped = 0
    for session_key, senders in list(_trusted_senders.items()):
        for sender in [s for s in list(senders) if not keep(s)]:
            dropped += remove_trusted_sender(session_key, sender)
    return dropped


def is_session_trusted(session_key: str) -> bool:
    """Whether *session_key* holds per-session Trust the policy still permits."""
    return bool(session_key) and session_key in _trusted_sessions and trust_permitted()


def trust_permitted() -> bool:
    """Whether the ``permission_mode`` policy lets a session hold ``trust``. From memory."""
    from kiro_crew.approval_mode_policy import session_mode_permitted

    return session_mode_permitted("trust")


def add_trusted_session(
    session_key: str,
    sessions: "SessionManager | Any | None" = None,
    strict: bool = False,
) -> None:
    """Grant per-session Trust for *session_key*.

    Two halves, and both are load-bearing. The in-memory grant is what
    ``TurnDriver``'s ``auto_approve_session`` predicate reads. The approval policy
    is what a SUBAGENT reads: a spawned child consults its parent's
    ``approval_policy``, never this mapping, so without the second write a trusted
    session's children still stop to ask.

    The manager is remembered so :func:`clear_trusted_sessions` can undo the second
    half too.

    ``strict=True`` makes a failing policy write undo the in-memory half and
    re-raise instead of logging. It is for a caller that reports the grant back to
    the person who asked for it and must not label a PARTIAL grant as a grant: an
    interactive Trust button whose policy write failed would otherwise say
    "Trusted" while every spawned subagent still stops to ask. The default stays
    best-effort, because a caller with nobody to tell is better off with the half
    it got than with none.
    """
    if not session_key:
        return
    if not trust_permitted():
        # Refused, not recorded: a policy deny of ``trust`` covers this button too.
        # A ``strict`` caller reports the refusal to the person who pressed it.
        logger.info("session Trust refused by policy on %s", telemetry_channel_of(session_key))
        if strict:
            raise PermissionError("trust is disabled by your organization's policy")
        return
    _trusted_sessions[session_key] = sessions
    if sessions is None:
        return
    try:
        sessions.set_approval_policy(session_key, "auto")
    except Exception:
        if strict:
            # Roll the first half back so the refusal the caller reports is true:
            # a key left in the mapping is a live grant no matter what it says.
            _trusted_sessions.pop(session_key, None)
            raise
        # The SURFACE, not the key: a session key carries the peer's platform id, and
        # naming the channel is what makes this line actionable anyway. Same shape
        # `session.py` and `task_executor.py` use for a session's surface.
        logger.warning(
            "Failed to propagate trust approval policy on %s",
            telemetry_channel_of(session_key),
            exc_info=True,
        )


def remove_trusted_session(session_key: str) -> None:
    """Drop ONE session's grant, both halves: the inverse of :func:`add_trusted_session`.

    The policy is reset through the manager the grant was made with. A failing
    policy write raises after the in-memory half is gone: the grant is off for
    the session's own turns either way, and the caller says the sub-agent half
    failed.
    """
    manager = _trusted_sessions.pop(session_key, None)
    if manager is not None:
        manager.set_approval_policy(session_key, "")


def clear_trusted_sessions(keep_policy: "set[str] | None" = None) -> None:
    """Drop every grant, per-sender ones too, INCLUDING the approval policy each one wrote.

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
    # The per-sender grants wrote no policy, so dropping them is the whole revoke.
    _trusted_senders.clear()
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


def _on_trust_denied(denied: frozenset[str]) -> None:
    """A ``permission_mode`` deny of ``trust`` drops every session Trust grant."""
    if "trust" in denied:
        clear_trusted_sessions()


def _register() -> None:
    from kiro_crew.approval_mode_policy import register_deny_hook

    register_deny_hook("messaging", _on_trust_denied)


_register()
