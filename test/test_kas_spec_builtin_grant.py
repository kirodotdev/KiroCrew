"""A KAS built-in the agent spec grants in ``allowedTools`` runs without a prompt,
and Crew's own checks still run on it.

The KAS projection keeps the shell and filesystem families at ``ask``
(``kas_permissions.WITHHELD_FROM_AUTO_APPROVE``): an allow rule would make KAS run
the call with no permission request, so the deny floor and the sensitive-path check
would never see it. Each call therefore raises ``session/request_permission``, and
the gate's ``spec-builtin-grant`` tier answers it on the spec's behalf -- after every
deny tier and governance. These tests pin both directions:

* granted and safe -> auto-approved by ``spec-builtin-grant`` (no prompt);
* granted but refused by the deny floor or the sensitive-path check -> denied;

and the plumbing that carries the grant from the projection to the gate: the entry
key, its removal from the wire, the session record, a mode switch, and the handle
stamping only its OWN requests.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew.acp import kas_agents
from kiro_crew.acp._dispatch import attach_kas_custom_agents
from kiro_crew.acp.kas_permissions import (
    CREW_ANSWERED_BUILTINS_KEY,
    WITHHELD_FROM_AUTO_APPROVE,
    crew_answered_builtins,
)
from kiro_crew.acp.types import AcpEvent, JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.hooks import (
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    HookManager,
    HooksConfig,
    hook_gate_kwargs,
)
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import parse_policy

_HOME = Path.home()
#: Spelled in two pieces so this file's own text is not one destructive command line.
_DESTRUCTIVE = "rm " + "-rf /"


@pytest.fixture(autouse=True)
def _no_profiles(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _install_governance(body: dict[str, Any]) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    ctx_mod.set_context(dataclasses.replace(base, governance=parse_policy(body)))


def _shell(command: str) -> AcpEvent:
    return _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": command}
    )


def _read(path: str) -> AcpEvent:
    return _kas_builtin_flow("read_file", raw_input={"path": path})


def _judge(event: AcpEvent, grants: set[str] | frozenset[str], **surface: Any):
    event.spec_granted_builtins = frozenset(grants)
    return HookManager(HooksConfig(**surface.pop("config", {}))).on_tool_call(
        event.title, **hook_gate_kwargs(event), **surface
    )


# ── granted and safe: no prompt ───────────────────────────────────────────────


def test_a_granted_shell_command_is_answered_by_crew_without_a_prompt() -> None:
    """``make build`` is not provably read-only, so without the grant it reaches the
    prompt (``allow``); with the spec's ``execute_bash`` grant Crew approves it."""
    event = _shell("make build")
    assert event.kas_builtin_ids and event.mcp_identity_trusted and event.is_shell
    ungranted = _judge(event, set())
    assert (ungranted.action, ungranted.tier) == (TOOL_ALLOW, "")
    granted = _judge(event, {"execute_bash"})
    assert (granted.action, granted.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


def test_a_granted_edit_is_answered_by_crew_without_a_prompt() -> None:
    event = _kas_builtin_flow(
        "str_replace",
        title="Edit",
        kind="edit",
        raw_input={"path": "/tmp/probe-note.txt", "oldStr": "a", "newStr": "b"},
    )
    assert _judge(event, set()).action == TOOL_ALLOW
    granted = _judge(event, {"fs_write"})
    assert (granted.action, granted.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


def test_a_granted_read_is_answered_by_crew() -> None:
    granted = _judge(_read("/tmp/probe-note.txt"), {"fs_read"})
    assert (granted.action, granted.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


# ── granted but refused: the checks still run ─────────────────────────────────


def test_the_deny_floor_still_refuses_a_granted_shell_command() -> None:
    verdict = _judge(_shell(_DESTRUCTIVE), {"execute_bash"})
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "deny-rules")


def test_the_sensitive_path_check_still_refuses_a_granted_read() -> None:
    verdict = _judge(_read(str(_HOME / ".aws" / "credentials")), {"fs_read"})
    assert verdict.action == TOOL_DENY
    assert verdict.tier in ("param-paths", "sensitive-path")


def test_a_governance_deny_still_refuses_a_granted_call() -> None:
    _install_governance(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "tools": {"mode": "deny", "deny": ["execute_bash"]},
        }
    )
    verdict = _judge(_shell("make build"), {"execute_bash"}, session_key="cli_chat")
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def test_a_ceiling_with_an_opinion_on_the_tool_withholds_the_grant() -> None:
    """A ceiling that speaks about ``commands`` at all keeps a permitted command at
    the prompt: the grant is the ceiling's to give, as on every other grant path."""
    _install_governance(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "commands": {"mode": "deny", "deny": ["*backdoor*"]},
        }
    )
    verdict = _judge(_shell("make build"), {"execute_bash"})
    assert verdict.action == TOOL_ALLOW
    assert verdict.tier != "spec-builtin-grant"


def test_confirm_tools_withholds_the_grant() -> None:
    verdict = _judge(_shell("make build"), {"execute_bash"}, config={"confirm_tools": ["*"]})
    assert verdict.action == TOOL_ALLOW


# ── what the grant never covers ───────────────────────────────────────────────


def test_a_write_grant_does_not_approve_delete_file() -> None:
    """``delete_file`` is governed under ``fs_write`` but never mounted by it."""
    event = _kas_builtin_flow(
        "delete_file", title="Delete", kind="delete", raw_input={"path": "/tmp/probe-note.txt"}
    )
    assert _judge(event, {"fs_write"}).tier != "spec-builtin-grant"


def test_a_powershell_grant_answers_a_kas_shell_call() -> None:
    """Both KAS shell tools reach the permission frame as ``run_command``."""
    granted = _judge(_shell("make build"), {"execute_pwsh"})
    assert (granted.action, granted.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")
    assert _judge(_shell(_DESTRUCTIVE), {"execute_pwsh"}).action == TOOL_DENY


def _facts(event: AcpEvent, grants: set[str], **surface: Any):
    from kiro_crew.hooks import GateFacts, ToolCall

    event.spec_granted_builtins = frozenset(grants)
    return GateFacts(
        HookManager(),
        ToolCall.from_event(event),
        session_key="",
        agent="",
        app="",
        classifier_only=bool(surface.get("classifier_only", False)),
    )


def test_the_shared_helper_takes_an_allow_predicate_that_only_narrows() -> None:
    """``kiro_crew.hooks.grant_spec_builtin`` is the helper another grant reuses:
    its ``allow`` predicate is asked last, with the facts and the call's names, and
    can only withhold."""
    from kiro_crew.hooks import grant_spec_builtin

    seen: list[tuple[str, ...]] = []

    def allow_all(facts: Any, names: tuple[str, ...]) -> bool:
        seen.append(names)
        return True

    facts = _facts(_shell("make build"), {"execute_bash"})
    assert grant_spec_builtin(facts).action == TOOL_AUTO_APPROVE
    assert grant_spec_builtin(facts, allow=allow_all).action == TOOL_AUTO_APPROVE
    assert seen == [("run_command", "execute_bash", "execute_pwsh")]
    assert grant_spec_builtin(facts, allow=lambda facts, names: False) is None
    # The predicate never widens: an ungranted call gets no grant and is not asked.
    seen.clear()
    assert grant_spec_builtin(_facts(_shell("make build"), set()), allow=allow_all) is None
    assert seen == []
    unattended = _facts(_shell("make build"), {"execute_bash"}, classifier_only=True)
    assert grant_spec_builtin(unattended, allow=allow_all) is None


def test_a_grant_for_another_tool_approves_nothing() -> None:
    assert _judge(_shell("make build"), {"fs_read"}).action == TOOL_ALLOW


def test_an_unattended_read_only_surface_ignores_the_grant() -> None:
    verdict = _judge(_shell("make build"), {"execute_bash"}, classifier_only=True)
    assert verdict.action != TOOL_AUTO_APPROVE


@pytest.mark.parametrize(
    "change",
    [
        {"mcp_identity_trusted": False},
        {"kas_builtin_ids": False},
        {"mcp_server_name": "ops"},
        {"tool_name": ""},
    ],
    ids=["untrusted-identity", "not-kas", "mcp-served", "no-identity"],
)
def test_only_a_host_identified_kas_builtin_is_granted(change: dict[str, Any]) -> None:
    event = _shell("make build")
    for name, value in change.items():
        setattr(event, name, value)
    assert _judge(event, {"execute_bash"}).tier != "spec-builtin-grant"


# ── the projection ────────────────────────────────────────────────────────────


def _spec(**extra: Any) -> dict[str, Any]:
    return {"name": "a", "prompt": "p", **extra}


def test_the_projection_records_the_grants_and_relays_no_allow_rule() -> None:
    spec = _spec(
        tools=["*"], allowedTools=["fs_read", "shell", "grep", "glob", "code", "web_fetch"]
    )
    out = kas_agents.to_client_custom_agent("a", spec, "p")
    assert out[CREW_ANSWERED_BUILTINS_KEY] == ["code", "execute_bash", "fs_read", "glob", "grep"]
    rules = out.get("permissions", {}).get("rules", [])
    assert not [r for r in rules if r["capability"] in ("shell", "fs_read", "fs_write")]


def test_no_grant_means_no_key() -> None:
    out = kas_agents.to_client_custom_agent("a", _spec(allowedTools=["web_fetch"]), "p")
    assert CREW_ANSWERED_BUILTINS_KEY not in out


def test_a_ceiling_with_an_opinion_withholds_the_recorded_grant() -> None:
    _install_governance(
        {"version": 1, "boot": {"fail_closed": True}, "commands": {"mode": "deny", "deny": ["x"]}}
    )
    out = kas_agents.to_client_custom_agent("a", _spec(allowedTools=["shell", "fs_read"]), "p")
    assert out[CREW_ANSWERED_BUILTINS_KEY] == ["fs_read"]


@pytest.mark.parametrize(
    ("rule", "kept"),
    [
        ({"capability": "fs_write", "effect": "ask"}, ["execute_bash", "fs_read"]),
        (
            {"capability": "fs_write", "effect": "ask", "match": ["docs/**"]},
            ["execute_bash", "fs_read"],
        ),
        ({"capability": "shell", "effect": "deny"}, ["fs_read", "fs_write"]),
        ({"capability": "filesystem", "effect": "ask"}, ["execute_bash"]),
        ({"capability": "builtin", "effect": "ask"}, []),
    ],
    ids=["ask", "scoped-ask", "deny", "meta-filesystem", "meta-builtin"],
)
def test_an_authored_ask_or_deny_keeps_its_built_ins_at_the_prompt(
    rule: dict[str, Any], kept: list[str]
) -> None:
    """The spec's own ``ask`` reaches Crew as a permission request; answering it
    with the ``allowedTools`` grant would override the author. Any ``ask`` or
    ``deny`` on a capability, whatever its scope, withholds the grants under it."""
    spec = _spec(allowedTools=["fs_read", "fs_write", "shell"], permissions={"rules": [rule]})
    out = kas_agents.to_client_custom_agent("a", spec, "p")
    assert out.get(CREW_ANSWERED_BUILTINS_KEY, []) == kept


@pytest.mark.parametrize(
    ("extra", "kept"),
    [
        (
            {
                "permissions": {
                    "policies": ["dev-shell"],
                    "rules": [{"capability": "shell", "effect": "ask"}],
                }
            },
            [],
        ),
        ({"permissions": {"rules": "not-a-list"}}, []),
        ({"permissions": {"rules": [], "extra": 1}}, []),
        ({"permissions": ["not", "an", "object"]}, []),
        (
            {
                "permissions": {
                    "rules": [{"capability": "web_fetch", "effect": "allow"}] * 300
                    + [{"capability": "shell", "effect": "ask"}]
                }
            },
            ["fs_read"],
        ),
        ({"toolsSettings": {"execute_bash": {"allowedCommands": ["git status"]}}}, ["fs_read"]),
        ({"toolsSettings": {"shell": {"deniedCommands": ["rm .*"]}}}, ["fs_read"]),
        ({"toolsSettings": {"fs_read": {"allowedPaths": ["docs/**"]}}}, ["execute_bash"]),
        ({"toolsSettings": {"@srv": {"anything": True}}}, ["execute_bash", "fs_read"]),
        ({"toolsSettings": "nonsense"}, []),
    ],
    ids=[
        "refused-bundle-with-ask",
        "malformed-rules",
        "unknown-key",
        "not-an-object",
        "oversized-with-ask",
        "tools-settings-shell",
        "tools-settings-shell-alias",
        "tools-settings-read",
        "tools-settings-mcp-only",
        "tools-settings-malformed",
    ],
)
def test_a_restriction_a_merge_drops_still_keeps_its_built_ins_at_the_prompt(
    extra: dict[str, Any], kept: list[str]
) -> None:
    """A merge refuses a bundle-bearing, malformed or oversized ``permissions`` block
    and a translation may not carry a ``toolsSettings`` entry; the author's
    restriction must still keep the grant from answering the call."""
    spec = _spec(allowedTools=["fs_read", "shell"], **extra)
    out = kas_agents.to_client_custom_agent("a", spec, "p")
    assert out.get(CREW_ANSWERED_BUILTINS_KEY, []) == kept


def test_an_authored_allow_withholds_nothing() -> None:
    spec = _spec(
        allowedTools=["fs_read"],
        permissions={"rules": [{"capability": "web_fetch", "effect": "allow"}]},
    )
    out = kas_agents.to_client_custom_agent("a", spec, "p")
    assert out[CREW_ANSWERED_BUILTINS_KEY] == ["fs_read"]


def test_withhold_answered_reads_every_withheld_name() -> None:
    from kiro_crew.acp.kas_permissions import withhold_answered_under_authored_rules

    assert withhold_answered_under_authored_rules(WITHHELD_FROM_AUTO_APPROVE, None) == (
        WITHHELD_FROM_AUTO_APPROVE
    ), "every withheld name needs a capability row, or it is never answered"
    code_only = withhold_answered_under_authored_rules(
        {"code"}, {"rules": [{"capability": "shell", "effect": "ask"}]}
    )
    assert code_only == frozenset()


def test_a_profile_on_another_scope_does_not_withhold_the_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile that governs only ``network.egress`` says nothing about a shell
    command: the projection still records the grant and the gate still answers it.
    A profile that governs ``commands`` withholds both."""
    from kiro_crew.platform import governance as gov

    egress = gov.Profile(
        name="egress",
        controls={
            "network.egress": gov.ScopedRuleset(mode="deny", allow=(), deny=("evil.example",))
        },
    )
    commands = gov.Profile(
        name="commands",
        controls={"commands": gov.ScopedRuleset(mode="deny", allow=(), deny=("*backdoor*",))},
    )
    monkeypatch.setattr(gp._STORE, "resolved", lambda: True)
    monkeypatch.setattr(gp._STORE, "all_profiles", lambda: [egress])
    out = kas_agents.to_client_custom_agent("a", _spec(allowedTools=["shell"]), "p")
    assert out[CREW_ANSWERED_BUILTINS_KEY] == ["execute_bash"]
    assert _judge(_shell("make build"), {"execute_bash"}).tier == "spec-builtin-grant"

    monkeypatch.setattr(gp._STORE, "all_profiles", lambda: [egress, commands])
    out = kas_agents.to_client_custom_agent("a", _spec(allowedTools=["shell"]), "p")
    assert CREW_ANSWERED_BUILTINS_KEY not in out
    assert _judge(_shell("make build"), {"execute_bash"}).tier != "spec-builtin-grant"


def test_an_unresolved_profile_store_withholds_the_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gp._STORE, "resolved", lambda: False)
    assert _judge(_shell("make build"), {"execute_bash"}).tier != "spec-builtin-grant"


def test_the_crew_only_key_never_reaches_the_wire() -> None:
    entry = kas_agents.to_client_custom_agent("a", _spec(allowedTools=["fs_read"]), "p")
    params: dict[str, Any] = {}
    attach_kas_custom_agents(params, [entry])
    (wire,) = params["_meta"]["kiro"]["customAgents"]
    assert CREW_ANSWERED_BUILTINS_KEY not in wire
    assert CREW_ANSWERED_BUILTINS_KEY in entry, "the caller's list is not mutated"


def test_crew_answered_builtins_reads_only_withheld_names() -> None:
    assert crew_answered_builtins(["shell", "read", "write", "web_fetch", "@srv"]) == {
        "execute_bash",
        "fs_read",
        "fs_write",
    }
    assert crew_answered_builtins(None) == frozenset()
    # kiro-cli's short spellings fold to the name a rule is written in.
    assert crew_answered_builtins(sorted(WITHHELD_FROM_AUTO_APPROVE)) == (
        WITHHELD_FROM_AUTO_APPROVE - {"read", "write"}
    )


def test_the_session_record_follows_the_active_agent_and_a_mode_switch() -> None:
    batch = [
        {"id": "vibe", "prompt": "p", CREW_ANSWERED_BUILTINS_KEY: ["fs_read"]},
        {"id": "other", "prompt": "p"},
    ]
    assert kas_agents.projected_crew_answered(batch, "vibe") == {"fs_read"}
    assert kas_agents.projected_crew_answered(batch, "missing") == frozenset()
    from kiro_crew.agent_files import kas_wire_agent_id

    assert kas_agents.switched_crew_answered(batch, kas_wire_agent_id("vibe")) == {"fs_read"}
    assert kas_agents.switched_crew_answered(batch, "other") == frozenset()
    if kas_wire_agent_id("vibe") != "vibe":
        # The engine's own built-in mode of the same name inherits nothing.
        assert kas_agents.switched_crew_answered(batch, "vibe") == frozenset()


# ── the session handle stamps only its own requests ───────────────────────────


def _handle(backend: str):
    from kiro_crew.acp.session_handle import AcpSessionHandle

    rt = MagicMock()
    rt.pid = None
    rt.acp_backend = backend
    rt._remote_workspace = None
    handle = AcpSessionHandle("own-sid", asyncio.Queue(), rt)
    handle.kas_crew_answered = frozenset({"execute_bash"})
    return handle


def _permission(session_id: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        id=7,
        method="session/request_permission",
        params={
            "sessionId": session_id,
            "toolCall": {"toolCallId": "tc-1", "status": "pending", "title": "Run Command"},
            "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
            "_meta": {"kiro": {"toolId": "run_command"}},
        },
    )


def test_the_handle_stamps_its_own_kas_request() -> None:
    event = _handle(ACP_BACKEND_KAS)._build_permission_event(_permission("own-sid"))
    assert event is not None and event.spec_granted_builtins == {"execute_bash"}


def test_a_native_kas_child_tool_call_carries_no_grant() -> None:
    """A native KAS sub-agent's tool call shares the parent's sessionId; the id its
    ``agentSubtaskId`` frame recorded marks it, and it runs another agent's spec."""
    from kiro_crew.acp._dispatch import scoped_tool_cache_key

    handle = _handle(ACP_BACKEND_KAS)
    handle._native_child_tool_call_ids.add(scoped_tool_cache_key("own-sid", "tc-1"))
    event = handle._build_permission_event(_permission("own-sid"))
    assert event is not None and event.spec_granted_builtins == frozenset()


def test_a_child_session_request_carries_no_grant() -> None:
    event = _handle(ACP_BACKEND_KAS)._build_permission_event(_permission("child-sid"))
    assert event is not None and event.sub_session_id == "child-sid"
    assert event.spec_granted_builtins == frozenset()


def test_another_backend_carries_no_grant() -> None:
    event = _handle("")._build_permission_event(_permission("own-sid"))
    assert event is not None and event.spec_granted_builtins == frozenset()
