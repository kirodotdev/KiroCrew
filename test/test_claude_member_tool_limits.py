"""A saved member spec's per-tool ``disabledTools`` holds on claude.

claude re-expresses each switched-off tool as a ``permissions.deny`` rule in the
settings Crew governs, spelled the way Claude Code names an MCP tool
(``claude_mcp_names.claude_mcp_tool_name``). The live captures in
``test/fixtures/acp_frames/claude/member-tool-limits*-live.jsonl`` are the evidence
``ACP_BACKENDS_MEMBER_TOOL_LIMITS`` holds claude to; the seed tests below pin that
both settings branches carry the rules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401
from acp_session_mcp_helpers import _by_name, _write_spec

from kiro_crew.acp import session_mcp
from kiro_crew.acp.claude_mcp_names import claude_mcp_tool_name
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_PI,
    ACP_BACKENDS_MEMBER_TOOL_LIMITS,
)
from kiro_crew.agent_capabilities import member_backend_gap

FIXTURES = Path(__file__).parent / "fixtures" / "acp_frames" / "claude"

#: The saved member spec the captures were driven with, minus the probe's argv.
_CAPTURED_SERVERS = {
    "limit-probe": {"command": "python3", "disabledTools": ["probe_hidden"]},
    "dot.probe": {"command": "python3", "disabledTools": ["dot.hidden"]},
}
_HIDDEN = ("mcp__limit-probe__probe_hidden", "mcp__dot_probe__dot_hidden")
_SIBLINGS = ("mcp__limit-probe__probe_echo", "mcp__dot_probe__dot_echo")


def _capture(name: str) -> tuple[dict, list[dict]]:
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0])["_meta"], [json.loads(line) for line in lines[1:]]


def _named(frames: list[dict]) -> tuple[set[str], set[str], set[str]]:
    """Tool names that got a ``tool_call``, a permission request, and a completion."""
    called: dict[str, str] = {}
    asked: set[str] = set()
    completed: set[str] = set()
    for frame in frames:
        if frame.get("method") == "session/request_permission":
            asked.add(frame["params"]["toolCall"]["name"])
        update = (frame.get("params") or {}).get("update") or {}
        if update.get("sessionUpdate") == "tool_call":
            called[update["toolCallId"]] = update["name"]
        elif update.get("status") == "completed" and update.get("toolCallId") in called:
            completed.add(called[update["toolCallId"]])
    return set(called.values()), asked, completed


@pytest.mark.parametrize(
    "fixture", ["member-tool-limits-live.jsonl", "member-tool-limits-inline-live.jsonl"]
)
def test_a_denied_tool_stays_out_while_its_sibling_runs(fixture):
    meta, frames = _capture(fixture)
    assert sorted(meta["deny"]) == sorted(_HIDDEN)
    called, asked, completed = _named(frames)
    for hidden in _HIDDEN:
        assert hidden not in called and hidden not in asked
        assert hidden not in json.dumps(frames), "not even offered to the model"
    for sibling in _SIBLINGS:
        assert sibling in asked, "a sibling still reaches Crew's gate"
        assert sibling in completed
    assert ACP_BACKEND_CLAUDE in ACP_BACKENDS_MEMBER_TOOL_LIMITS


def test_a_rule_with_the_raw_dotted_names_denies_nothing():
    """The reason the rules are folded: Claude Code renames a dotted tool."""
    meta, frames = _capture("member-tool-limits-raw-spelling-live.jsonl")
    assert "mcp__dot.probe__dot.hidden" in meta["deny"]
    _called, asked, completed = _named(frames)
    assert "mcp__dot_probe__dot_hidden" in asked
    assert "mcp__dot_probe__dot_hidden" in completed
    # A name Claude Code keeps as it is was denied by the same spelling.
    assert "mcp__limit-probe__probe_hidden" not in json.dumps(frames)


def test_the_captured_rules_are_what_crew_writes(agents_dir):
    _write_spec(agents_dir, servers=_CAPTURED_SERVERS, tools=["*"], name="probe-member")
    meta, _frames = _capture("member-tool-limits-live.jsonl")
    assert session_mcp.session_mcp_deny_rules("probe-member") == meta["deny"]


@pytest.mark.parametrize(
    ("server", "tool", "name"),
    [
        ("srv", "t", "mcp__srv__t"),
        ("limit-probe", "probe_hidden", "mcp__limit-probe__probe_hidden"),
        ("dot.probe", "dot.hidden", "mcp__dot_probe__dot_hidden"),
        ("my server", "a/b", "mcp__my_server__a_b"),
    ],
)
def test_the_tool_name_is_spelled_as_claude_code_spells_it(server, tool, name):
    assert claude_mcp_tool_name(server, tool) == name


def test_names_that_fold_together_yield_one_rule(agents_dir):
    _write_spec(
        agents_dir,
        servers={
            "dot.probe": {"command": "x", "disabledTools": ["t"]},
            "dot_probe": {"command": "x", "disabledTools": ["t"]},
        },
        tools=["*"],
    )
    assert session_mcp.session_mcp_deny_rules("kirocrew") == ["mcp__dot_probe__t"]


def test_the_start_gate_lets_a_limited_spec_start_on_claude_only_among_mirrors():
    assert member_backend_gap(ACP_BACKEND_CLAUDE, ("srv",)) is None
    assert member_backend_gap(ACP_BACKEND_CODEX, ("srv",)) == "tool_limits_unsupported"
    assert member_backend_gap(ACP_BACKEND_PI, ("srv",)) == "tool_limits_unsupported"


class TestTheSeedCarriesTheRules:
    _SERVERS = {"dot.srv": {"command": "/bin/srv", "disabledTools": ["t"]}}

    def test_the_authored_file(self, tmp_path, agents_dir):
        _write_spec(agents_dir, servers=self._SERVERS, tools=["@dot.srv"], name="member-a")
        client = AcpClient(work_dir=tmp_path, agent="member-a", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert client._claude_settings_authored is True
        data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert data["permissions"]["deny"] == ["mcp__dot_srv__t"]
        assert "dot.srv" in _by_name(client._session_mcp_servers())

    def test_the_inline_settings_beside_a_project_owned_file(self, tmp_path, agents_dir):
        _write_spec(agents_dir, servers=self._SERVERS, tools=["@dot.srv"], name="member-a")
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        original = {"permissions": {"allow": ["mcp__dot_srv__t"]}}
        path.write_text(json.dumps(original), encoding="utf-8")
        client = AcpClient(work_dir=tmp_path, agent="member-a", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert client._claude_settings_authored is False
        assert "dot.srv" in _by_name(client._session_mcp_servers())
        options = client._claude_session_meta()["claudeCode"]["options"]
        assert options["settingSources"] == ["user"]
        assert options["settings"]["permissions"]["deny"] == ["mcp__dot_srv__t"]
        assert "allow" not in options["settings"]["permissions"]
        assert json.loads(path.read_text()) == original

    def test_an_ungoverned_session_gets_no_array(self, tmp_path, agents_dir, monkeypatch):
        """Nothing widens when neither branch can carry the rules."""
        _write_spec(agents_dir, servers=self._SERVERS, tools=["@dot.srv"], name="member-a")
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"permissions": {}}), encoding="utf-8")
        client = AcpClient(
            work_dir=tmp_path,
            agent="member-a",
            acp_backend=ACP_BACKEND_CLAUDE,
            permission_mode="default",
        )
        client._write_claude_local_settings()
        assert client._claude_settings_authored is False
        assert client._session_mcp_servers() == []
