"""Which hook trigger events may fire a script hook: ``capabilities.script_hooks`` ``events``.

A policy names the events in the row's inner ``events`` ruleset (RFC
``rfc-crew-enterprise-edition``, appendix row "Hook trigger events"), e.g.
``{"capabilities": {"script_hooks": {"enabled": true, "scopes": {"events":
{"mode": "allow", "allow": ["PreToolUse"]}}}}}``. Event names are the
``hooks.HOOK_EVENT_*`` values; the identifier matcher is case-insensitive, so
kiro-cli's ``preToolUse`` reads as ``PreToolUse``.

Each place a hook fires asks :func:`event_refusal` right after the capability
check (``hook_runtime.governance_gate._script_hooks_capability_denied``), which
already decides whether hooks run at all and, in an enterprise build, that only
the policy can turn them on. A list the policy does not name permits every event,
so with no policy nothing changes.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The governed row whose inner ``events`` ruleset names the permitted events.
SCRIPT_HOOKS_SCOPE = "capabilities.script_hooks"


def event_refusal(session_key: str, event: str) -> "str | None":
    """Why the policy refuses a script hook on *event*; ``None`` when it permits.

    Asked on *session_key*, as the capability check is. An empty *event* is not
    asked. An evaluation error refuses in an enterprise build and permits in an
    ``oss`` one, matching the capability check. Blocking: call off the event loop.
    """
    if not event:
        return None
    from kiro_crew.edition_locks import locked_off
    from kiro_crew.platform.context import PlatformCompositionError

    enterprise = locked_off()
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            SCRIPT_HOOKS_SCOPE,
            f"events:{event}",
            session_key=session_key,
            log_warning=False,
            fail_closed=enterprise,
        )
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("hook event governance unavailable", exc_info=True)
        return "script_hooks governance unavailable (fail-closed)" if enterprise else None
    if not getattr(decision, "permitted", False):
        return f"hook event {event!r} refused by the security policy"
    return None
