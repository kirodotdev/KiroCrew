"""A launch added beside an approved one is told apart from a changed one.

The operator approves one agent's launch of a server from the dashboard toggle.
A second agent then declares the same server with its own arguments, and the next
gateway start refuses the name as ``changed_needs_reapproval``. These tests pin,
through the real toggle, rewrite and store, that the refusal record carries the
approved launch unchanged among the live ones (the toggle's display and the
rewrite's display are the same bytes), so ``kirocrew doctor`` names it a new
launch; that an approved launch which really changed keeps "command changed",
even when a redaction tag makes its display read as unchanged; and that one
Approve update still approves every live launch. The dashboard applies
the same rule to the same record (``McpManagement.newLaunch.test.tsx``).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp_launch_approval_helpers import (  # noqa: F401 -- the fixture is requested by name
    _APPROVED_CMD,
    _approval_module,
    _declare,
    _next_boot_rewrite,
    _toggle_on,
    toggle_home_fixture,
)

from kiro_crew.doctor_checks import mcp as doctor_mcp
from kiro_crew.mcp_gateway import rewriter

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True


def _declare_second_agent(agents: Path, args: list[str]) -> None:
    entry = {"command": _APPROVED_CMD, "args": args}
    (agents / "b.json").write_text(
        json.dumps({"name": "b", "tools": ["*"], "mcpServers": {"srv": entry}})
    )


def _pairs(refusal: dict, side: str = "") -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    commands, envs = refusal[f"{side}commands"], refusal[f"{side}envs"]
    assert len(commands) == len(envs)
    return {(tuple(command), tuple(env)) for command, env in zip(commands, envs)}


def _doctor_row(capsys) -> str:
    cfg = SimpleNamespace(mcp_gateway=SimpleNamespace(enabled=True, stub_servers=["srv"]))
    doctor_mcp._doctor_mcp_share_refused(cfg, [])
    return capsys.readouterr().out


async def _refusal_after_next_boot(tmp_path: Path, agents: Path) -> dict:
    mod = _approval_module()
    _target_env, approvals = _next_boot_rewrite(tmp_path, agents)
    assert approvals.refused == {"SRV": mod.REFUSED_CHANGED}
    approvals.full_pass = True
    assert mod.save_pass(approvals)
    return mod.refused_servers()["srv"]


@pytest.mark.asyncio
async def test_a_second_agents_launch_reads_as_new_not_as_a_changed_command(
    tmp_path, toggle_home, capsys
):
    mod = _approval_module()
    assert mod is not None
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200

    _declare_second_agent(toggle_home, ["--second-agent"])
    refusal = await _refusal_after_next_boot(tmp_path, toggle_home)

    assert refusal["approved_commands"] == [[_APPROVED_CMD]]
    assert sorted(refusal["commands"]) == sorted(
        [[_APPROVED_CMD], [_APPROVED_CMD, "--second-agent"]]
    )
    # The toggle and the rewrite display the approved launch as the same bytes.
    assert _pairs(refusal, "approved_") < _pairs(refusal)
    assert "srv (new launch not approved yet)" in _doctor_row(capsys)

    # One Approve update approves both launches, the approved one included.
    status, body = await _toggle_on(expected_launch=refusal["expected_launch"])
    assert status == 200, body
    stored = mod.load_approvals()
    assert stored.stored_refused == {}
    assert stored.admits_command("SRV", rewriter.hash_command(_APPROVED_CMD, []))
    assert stored.admits_command("SRV", rewriter.hash_command(_APPROVED_CMD, ["--second-agent"]))


@pytest.mark.asyncio
async def test_an_approved_launch_that_changed_keeps_command_changed(tmp_path, toggle_home, capsys):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200

    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["--edited"])
    refusal = await _refusal_after_next_boot(tmp_path, toggle_home)

    assert refusal["approved_commands"] == [[_APPROVED_CMD]]
    assert refusal["commands"] == [[_APPROVED_CMD, "--edited"]]
    assert "srv (command changed since it was approved)" in _doctor_row(capsys)


@pytest.mark.asyncio
async def test_an_edit_beside_a_second_agent_keeps_command_changed(tmp_path, toggle_home, capsys):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200

    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["--edited"])
    _declare_second_agent(toggle_home, ["--second-agent"])
    refusal = await _refusal_after_next_boot(tmp_path, toggle_home)

    assert [_APPROVED_CMD] not in refusal["commands"]
    assert "srv (command changed since it was approved)" in _doctor_row(capsys)


@pytest.mark.asyncio
async def test_a_changed_credential_beside_a_second_agent_keeps_command_changed(
    tmp_path, toggle_home, capsys
):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["--token", "ghp_" + "a1B2" * 9])
    assert (await _toggle_on())[0] == 200

    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["--token", "ghp_" + "z9Y8" * 9])
    _declare_second_agent(toggle_home, ["--second-agent"])
    refusal = await _refusal_after_next_boot(tmp_path, toggle_home)

    # Both tokens display as the same tag, so the edited launch reads like the
    # approved one beside the second agent's.
    assert _pairs(refusal, "approved_") < _pairs(refusal)
    assert "[REDACTED: credential]" in refusal["approved_commands"][0][-1]
    assert "srv (command changed since it was approved)" in _doctor_row(capsys)
