"""Which hook trigger events may fire a script hook: ``capabilities.script_hooks`` ``events``.

A policy names the events in the row's inner ``events`` ruleset (RFC
``rfc-crew-lts-edition``, appendix row "Hook trigger events"), e.g.
``{"capabilities": {"script_hooks": {"enabled": true, "scopes": {"events":
{"mode": "allow", "allow": ["PreToolUse"]}}}}}``. Event names are the
``hooks.HOOK_EVENT_*`` values; the identifier matcher is case-insensitive, so
kiro-cli's ``preToolUse`` reads as ``PreToolUse``.

Each place a hook fires asks :func:`event_refusal` right after the capability
check (``hook_runtime.governance_gate._script_hooks_capability_denied``), which
already decides whether hooks run at all and, in an LTS build, that only
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
    asked. An evaluation error refuses in an LTS build and permits in an
    ``oss`` one, matching the capability check. Blocking: call off the event loop.
    """
    if not event:
        return None
    from kiro_crew.edition_locks import locked_off
    from kiro_crew.platform.context import PlatformCompositionError

    lts = locked_off()
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            SCRIPT_HOOKS_SCOPE,
            f"events:{event}",
            session_key=session_key,
            log_warning=False,
            fail_closed=lts,
        )
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("hook event governance unavailable", exc_info=True)
        return "script_hooks governance unavailable (fail-closed)" if lts else None
    if not getattr(decision, "permitted", False):
        return f"hook event {event!r} refused by the security policy"
    return None


def hook_policy_name(hook: object) -> str:
    """The name the ``hooks`` ruleset knows *hook* by.

    An agent spec's own hook (``spec_origin`` set, in memory only) is
    ``<agent>/<Event>/<n>``, where ``n`` is the ``#n`` of the hook's display
    name; a ``~/.kiro/hooks`` file's hook reads ``userfile:<stem>`` as its agent.
    A Hooks-page hook is its name, trimmed. The name only decides whether the
    hook runs: a listed hook still meets the shell deny list and the sandbox, so
    a hook that borrows a listed name can do no more than the turn's own shell.
    """
    hook_id = str(getattr(hook, "id", "") or "")
    if getattr(hook, "spec_origin", "") and hook_id.startswith("spec:"):
        # The id is ``spec:<agent>:<Event>:<index>`` (``agent_sdk.spec_hooks._hook``);
        # the agent part may hold ``:``, the event and index never do.
        parts = hook_id[len("spec:") :].rsplit(":", 2)
        if len(parts) == 3:
            agent, event, index = parts
            if agent and event and index.isascii() and index.isdigit():
                return f"{agent}/{event}/{int(index) + 1}"
    return str(getattr(hook, "name", "") or hook_id).strip()


def hook_refusal(session_key: str, hook: object) -> "str | None":
    """Why the policy's ``hooks`` list refuses *hook*; ``None`` when it permits.

    Asked as ``hooks:<name>`` (:func:`hook_policy_name`) on *session_key*, after
    :func:`event_refusal`. A list the policy does not name permits every hook.
    Errors follow :func:`event_refusal`. Blocking: call off the event loop.
    """
    from kiro_crew.edition_locks import locked_off
    from kiro_crew.platform.context import PlatformCompositionError

    name = hook_policy_name(hook)
    lts = locked_off()
    try:
        from kiro_crew.platform.governance_profiles import vet_and_audit

        # Audited both ways: a grant on this list is a permission decision too.
        decision = vet_and_audit(
            SCRIPT_HOOKS_SCOPE,
            f"hooks:{name}",
            session_key=session_key,
            tool_name=f"run_script_hook:{name}",
            log_warning=False,
            fail_closed=lts,
        )
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("hook name governance unavailable", exc_info=True)
        return "script_hooks governance unavailable (fail-closed)" if lts else None
    if not getattr(decision, "permitted", False):
        return f"hook {name!r} is not in the security policy's hooks list"
    return None
