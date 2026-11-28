"""A native KAS sub-agent's call is held to the limits of the agent the session runs.

KAS runs a sub-agent inside its parent's permission rules, and its calls share the
parent's ``sessionId``. The limits only Crew's gate applies (the shell
``denyByDefault`` allowlist, the write ``fallbackAction: "deny"`` allowlist, the
deny record) ride the parent's requests, so the session handle stamps them on the
child's too, with none of the parent's grants.
"""

from __future__ import annotations

from typing import Any

from test_kas_deny_by_default import _SETTINGS, _no_profiles, _shell  # noqa: F401
from test_kas_spec_builtin_grant import _handle, _permission

from kiro_crew import permission_floor
from kiro_crew.acp import kas_agents
from kiro_crew.acp._dispatch import scoped_tool_cache_key
from kiro_crew.acp.types import AcpEvent
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.platform.spec_denied_grant import SpecDeniedGrant

_REFUSED = "mkfifo /tmp/probe-notlisted-fifo"
_ALLOWED = "touch /tmp/probe-allowed-a.txt"


def _session(settings: dict[str, Any] | None):
    """A KAS session handle whose own agent's spec carries *settings*."""
    spec: dict[str, Any] = {"name": "probe", "prompt": "p", "tools": ["execute_bash"]}
    if settings is not None:
        spec["toolsSettings"] = settings
    entry = kas_agents.to_client_custom_agent("probe", spec, "p")
    handle = _handle(ACP_BACKEND_KAS)
    handle.kas_spec_shell_allowlist = kas_agents.projected_shell_allowlist([entry], "probe")
    return handle


def _child_call(handle, command: str) -> AcpEvent:
    """*command* as a native sub-agent's shell call: the id its ``agentSubtaskId``
    frame recorded marks it, under the session's own id."""
    handle._native_child_tool_call_ids.add(scoped_tool_cache_key("own-sid", "tc-1"))
    stamped = handle._build_permission_event(_permission("own-sid"))
    assert stamped is not None and stamped.native_child_call is True
    event = _shell(command)
    event.native_child_call = True
    event.spec_shell_allowlist = stamped.spec_shell_allowlist
    return event


def _auto_refusal(event: AcpEvent) -> str | None:
    """What an auto-approving session (a cron job) answers: whatever the gate does
    not deny runs."""
    return permission_floor.refusal_for(event, session_key="cron:probe", security_only=False)


def test_a_child_call_the_parents_allowlist_does_not_match_is_refused() -> None:
    reason = _auto_refusal(_child_call(_session(_SETTINGS), _REFUSED))
    assert reason is not None and "denyByDefault" in reason


def test_a_child_call_the_parents_allowlist_matches_still_runs() -> None:
    assert _auto_refusal(_child_call(_session(_SETTINGS), _ALLOWED)) is None


def test_a_child_of_an_unrestricted_parent_is_judged_as_before() -> None:
    event = _child_call(_session(None), _REFUSED)
    assert event.spec_shell_allowlist is None
    assert _auto_refusal(event) is None


def test_a_child_call_carries_the_parents_limits_and_none_of_its_grants() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    handle.kas_spec_write_allowlist = ("/w/out/**",)
    handle._bound_cwd = "/w"
    handle.kas_spec_unjudged_denies = ("rm .*",)
    handle.kas_crew_answered_commands = ("ls",)
    handle.kas_spec_denies = SpecDeniedGrant(
        builtins=frozenset({"execute_bash"}),
        commands=("git status",),
        denied_commands=("rm -rf .*",),
        denied_write_paths=("/w/secret/**",),
        cwd="/w",
        web_fetch_blocked=("https://blocked.example/",),
        web_fetch_trusted=("https://trusted.example/",),
    )
    handle._native_child_tool_call_ids.add(scoped_tool_cache_key("own-sid", "tc-1"))

    event = handle._build_permission_event(_permission("own-sid"))

    assert event is not None and event.native_child_call is True
    assert event.spec_write_allowlist == ("/w/out/**",)
    assert event.spec_write_cwd == "/w"
    assert event.spec_unjudged_denies == ("rm .*",)
    assert event.spec_denied_grant == SpecDeniedGrant(
        denied_commands=("rm -rf .*",),
        denied_write_paths=("/w/secret/**",),
        cwd="/w",
        web_fetch_blocked=("https://blocked.example/",),
    )
    assert event.spec_granted_builtins == frozenset()
    assert event.spec_granted_commands == ()
