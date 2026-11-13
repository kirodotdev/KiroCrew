"""A spec's ``toolsSettings`` shell and write allowlists on claude.

kiro-cli refuses every shell command a ``shell.denyByDefault`` spec's
``allowedCommands`` do not match, and every file write a ``fs_write.fallbackAction:
"deny"`` spec's ``allowedPaths`` do not cover. claude reads neither, so the direct
client reads them from the spec it projects (``acp.spec_tools_allowlists``),
stamps them on every permission request its process raises (a native
sub-agent's included), and the gate's
``spec-shell-allowlist`` and ``spec-write-allowlist`` rows refuse the rest. A
scheduled job (``ToolApprovalPolicy.AUTO_APPROVE``, which consults the gate through
``permission_floor.refusal_for``) refuses them too. The settings seed adds
``permissions.ask`` rules so claude asks about those tools at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401

from kiro_crew import permission_floor
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.spec_tools_allowlists import (
    CLAUDE_SHELL_ASK_RULES,
    CLAUDE_WRITE_ASK_RULES,
    claude_ask_rules,
)
from kiro_crew.acp.types import AcpEvent, JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_CREW_ENFORCES_SPEC_TOOLS_SETTINGS

_AGENT = "probe"
_SHELL = {"shell": {"denyByDefault": True, "allowedCommands": ["git status"]}}
_WRITE = {"fs_write": {"fallbackAction": "deny", "allowedPaths": ["docs/**"]}}


@pytest.fixture(autouse=True)
def _ungoverned(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    """No governance profile on the host decides any of these calls."""
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform import governance_profiles as gp

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _write_spec(agents_dir: Path, tools_settings: dict[str, Any] | None) -> None:
    spec: dict[str, Any] = {"name": _AGENT, "prompt": "p", "tools": ["*"], "mcpServers": {}}
    if tools_settings is not None:
        spec["toolsSettings"] = tools_settings
    (agents_dir / f"{_AGENT}.json").write_text(json.dumps(spec), encoding="utf-8")


def _plant_checkout(root: Path) -> Path:
    """A cloned repository shipping a same-named spec that sets no allowlist."""
    checkout = root / "cloned-repo"
    agents = checkout / ".kiro" / "agents"
    agents.mkdir(parents=True)
    spec = {"name": _AGENT, "prompt": "p", "tools": ["*"], "mcpServers": {}}
    (agents / f"{_AGENT}.json").write_text(json.dumps(spec), encoding="utf-8")
    return checkout


def _client(work_dir: Path, backend: str = ACP_BACKEND_CLAUDE) -> AcpClient:
    """A client whose seed and array resolution ran, as the spawn path runs them."""
    client = AcpClient(
        work_dir=work_dir, agent=_AGENT, acp_backend=backend, session_key="cron:probe"
    )
    if backend == ACP_BACKEND_CLAUDE:
        client._write_claude_local_settings()
    client._session_mcp_servers()
    return client


def _request(
    client: AcpClient, call_id: str, kind: str, raw: dict[str, Any], **tool_call: Any
) -> AcpEvent:
    """The permission event *client* builds for one of its session's tool calls."""
    client._tool_call_is_shell[call_id] = kind == "execute"
    client._tool_call_params[call_id] = raw
    msg = JsonRpcMessage(
        id=f"req-{call_id}",
        method="session/request_permission",
        params={
            "toolCall": {
                "toolCallId": call_id,
                "kind": kind,
                "title": kind,
                "rawInput": raw,
                **tool_call,
            },
            "options": [{"optionId": "allow", "kind": "allow_once", "name": "Allow"}],
        },
    )
    event = client._build_permission_event(msg)
    assert event is not None
    return event


def _auto_refusal(event: AcpEvent) -> str | None:
    """What an ``AUTO_APPROVE`` job's gate answers: a reason refuses, ``None`` runs it."""
    return permission_floor.refusal_for(event, session_key="cron:probe", security_only=False)


def _shell(client: AcpClient, command: str, call_id: str = "tc-sh") -> AcpEvent:
    return _request(client, call_id, "execute", {"command": command})


def _write(client: AcpClient, path: Path, call_id: str = "tc-wr") -> AcpEvent:
    return _request(client, call_id, "edit", {"file_path": str(path), "content": "x"})


# ── the gate refuses what the spec forbids ─────────────────────────────────


def test_an_auto_mode_job_refuses_a_command_the_spec_never_allowed(tmp_path, agents_dir) -> None:
    _write_spec(agents_dir, _SHELL)
    reason = _auto_refusal(_shell(_client(tmp_path), "rm notes.txt"))
    assert reason is not None and "denyByDefault" in reason


def test_an_auto_mode_job_still_runs_a_listed_command(tmp_path, agents_dir) -> None:
    _write_spec(agents_dir, _SHELL)
    assert _auto_refusal(_shell(_client(tmp_path), "git status")) is None


def test_an_auto_mode_job_refuses_a_write_outside_the_allowed_paths(tmp_path, agents_dir) -> None:
    _write_spec(agents_dir, _WRITE)
    reason = _auto_refusal(_write(_client(tmp_path), tmp_path / "src" / "a.py"))
    assert reason is not None and "fallbackAction" in reason


def test_an_auto_mode_job_still_writes_inside_the_allowed_paths(tmp_path, agents_dir) -> None:
    _write_spec(agents_dir, _WRITE)
    assert _auto_refusal(_write(_client(tmp_path), tmp_path / "docs" / "a.md")) is None


def test_a_withheld_array_still_refuses(tmp_path, agents_dir) -> None:
    """A session whose settings file Crew does not govern runs without Crew's MCP
    array, but claude's own shell still runs, so the allowlist must still hold."""
    _write_spec(agents_dir, _SHELL)
    client = AcpClient(
        work_dir=tmp_path, agent=_AGENT, acp_backend=ACP_BACKEND_CLAUDE, session_key="cron:probe"
    )
    assert client._session_mcp_servers() == []
    reason = _auto_refusal(_shell(client, "rm notes.txt"))
    assert reason is not None and "denyByDefault" in reason


def test_a_checkout_spec_cannot_drop_the_users_allowlists(tmp_path, agents_dir) -> None:
    """An untrusted checkout's same-named spec yields the user's spec on the
    mirrored read, so both the seeded ask rules and the gate's stamps keep the
    allowlists the user wrote."""
    _write_spec(agents_dir, {**_SHELL, **_WRITE})
    checkout = _plant_checkout(tmp_path)
    client = _client(checkout)
    perms = json.loads(client._render_claude_settings_payload()).get("permissions") or {}
    assert perms.get("ask") == [*CLAUDE_SHELL_ASK_RULES, *CLAUDE_WRITE_ASK_RULES]
    reason = _auto_refusal(_write(client, checkout / "src" / "a.py"))
    assert reason is not None and "fallbackAction" in reason


def test_a_withheld_array_keeps_the_users_allowlists_in_a_checkout(tmp_path, agents_dir) -> None:
    """The withheld path reads the spec again through the same mirrored read."""
    _write_spec(agents_dir, _SHELL)
    checkout = _plant_checkout(tmp_path)
    client = AcpClient(
        work_dir=checkout, agent=_AGENT, acp_backend=ACP_BACKEND_CLAUDE, session_key="cron:probe"
    )
    assert client._session_mcp_servers() == []
    reason = _auto_refusal(_shell(client, "rm notes.txt"))
    assert reason is not None and "denyByDefault" in reason


def _envelope_asks(client: AcpClient) -> list[str]:
    options = client._claude_session_meta()["claudeCode"]["options"]
    return list(((options.get("settings") or {}).get("permissions") or {}).get("ask") or [])


def test_a_declined_project_settings_file_still_gets_the_ask_rules(tmp_path, agents_dir) -> None:
    """A project ``settings.local.json`` Crew does not own, with a hook (so it cannot
    be left out) and a ``Bash`` allow rule, still loads: the ask rules ride the
    session envelope so that allow rule cannot approve a command unasked."""
    _write_spec(agents_dir, {**_SHELL, **_WRITE})
    local = tmp_path / ".claude" / "settings.local.json"
    local.parent.mkdir()
    foreign = {
        "permissions": {"allow": ["Bash"]},
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]},
    }
    local.write_text(json.dumps(foreign), encoding="utf-8")
    client = _client(tmp_path)
    assert json.loads(local.read_text(encoding="utf-8")) == foreign
    assert not client._claude_local_settings_excluded
    assert _envelope_asks(client) == [*CLAUDE_SHELL_ASK_RULES, *CLAUDE_WRITE_ASK_RULES]


def test_the_envelope_asks_nothing_without_an_allowlist(tmp_path, agents_dir) -> None:
    _write_spec(agents_dir, None)
    assert _envelope_asks(_client(tmp_path)) == []


def test_a_spec_with_no_allowlist_stamps_nothing(tmp_path, agents_dir) -> None:
    _write_spec(agents_dir, None)
    client = _client(tmp_path)
    event = _shell(client, "rm notes.txt")
    assert event.spec_shell_allowlist is None and event.spec_write_allowlist is None
    assert _auto_refusal(_write(client, tmp_path / "src" / "a.py")) is None


def test_a_sub_agent_request_carries_the_parents_allowlists(tmp_path, agents_dir) -> None:
    """A claude Task/Agent child runs in the same process under the same spec, with
    no Crew spec of its own, so delegating must not step around the allowlists."""
    _write_spec(agents_dir, {**_SHELL, **_WRITE})
    client = _client(tmp_path)
    event = _request(
        client,
        "tc-child",
        "execute",
        {"command": "rm notes.txt"},
        _meta={"claudeCode": {"parentToolUseId": "toolu_parent"}},
    )
    assert event.native_child_call is True
    assert client._spec_shell_allowlist is not None
    assert client._spec_write_allowlist is not None
    assert event.spec_shell_allowlist == client._spec_shell_allowlist
    assert event.spec_write_allowlist == client._spec_write_allowlist
    reason = _auto_refusal(event)
    assert reason is not None and "denyByDefault" in reason


def test_kiro_cli_requests_are_never_stamped(tmp_path, agents_dir) -> None:
    """kiro-cli enforces toolsSettings itself; Crew's narrower subset must not
    refuse what it allows (H13: the kiro path gains no step)."""
    assert ACP_BACKEND_KIRO not in ACP_BACKENDS_CREW_ENFORCES_SPEC_TOOLS_SETTINGS
    _write_spec(agents_dir, {**_SHELL, **_WRITE})
    client = _client(tmp_path, backend=ACP_BACKEND_KIRO)
    assert client._spec_shell_allowlist is None and client._spec_write_allowlist is None
    event = _shell(client, "rm notes.txt")
    assert event.spec_shell_allowlist is None and event.spec_write_allowlist is None


# ── the seed makes those tools ask ────────────────────────────────────────


def _seeded_permissions(tmp_path: Path) -> dict[str, Any]:
    payload = json.loads(_client(tmp_path)._render_claude_settings_payload())
    return payload.get("permissions") or {}


@pytest.mark.parametrize(
    "tools_settings, expected",
    [
        (_SHELL, list(CLAUDE_SHELL_ASK_RULES)),
        (_WRITE, list(CLAUDE_WRITE_ASK_RULES)),
        ({**_SHELL, **_WRITE}, [*CLAUDE_SHELL_ASK_RULES, *CLAUDE_WRITE_ASK_RULES]),
    ],
)
def test_the_seed_asks_about_the_allowlisted_tools(
    tmp_path, agents_dir, tools_settings, expected
) -> None:
    """claude approves a read-only command or an ``acceptEdits`` edit on its own;
    an ``ask`` rule outranks that, so the call reaches the gate."""
    _write_spec(agents_dir, tools_settings)
    assert _seeded_permissions(tmp_path).get("ask") == expected


def test_the_seed_asks_nothing_without_an_allowlist(tmp_path, agents_dir) -> None:
    _write_spec(agents_dir, {"shell": {"allowedCommands": ["git status"]}})
    assert "ask" not in _seeded_permissions(tmp_path)


def test_a_spec_that_grants_the_shell_and_denies_nothing_lifts_deny_by_default() -> None:
    """As on kiro-cli: an agent whose ``allowedTools`` names the shell runs any
    command its ``deniedCommands`` do not deny, so there is no allowlist to ask for."""
    spec = {"allowedTools": ["execute_bash"], "toolsSettings": _SHELL}
    assert claude_ask_rules(spec) == []
