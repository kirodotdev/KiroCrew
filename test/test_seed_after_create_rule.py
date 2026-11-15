"""An agent whose grants cannot seed a first message is told so before it tries.

``session_create``'s ``message`` is refused (``message_needs_send_approval``) for an
agent that auto-approves the create and not ``session_send``. Whether that is true
of an agent is decided by its grants and read by one predicate,
``agent_specs.creates_without_send``: the gate refuses on it, and the spec writer
states it in the prompt of every generated spec it is true of. These tests pin that
the two read the same thing, that the line follows the grants on every shipped
spec, and that the tool, the server instructions, the skills and the docs say it in
the same words.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import agent, mcp_instructions
from kiro_crew.agent_materialization import auto_approve
from kiro_crew.agent_sdk import agent_specs
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.mcp_dashboard_session_tools import _session_tools

RULE = auto_approve.SEED_AFTER_CREATE_RULE
_CREATE = "@kirocrew-dashboard/session_create"
_SEND = "@kirocrew-dashboard/session_send"
_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
_DOCS = Path(__file__).resolve().parents[1] / "docs"

#: The description's length before it stated the rule. It may shrink, never grow:
#: every caller of the dashboard server pays for these characters on every turn.
_DESCRIPTION_CHARS_BEFORE = 800

#: Grant lists, and whether a first message is refused for the agent holding them.
_GRANT_CASES = [
    pytest.param([_CREATE], True, id="create-only"),
    pytest.param([_CREATE, "session_send"], True, id="bare-send-still-prompts"),
    pytest.param([_CREATE, _SEND], False, id="create-and-send"),
    pytest.param([_CREATE, "@kirocrew-dashboard"], False, id="whole-server"),
    pytest.param(["@kirocrew-dashboard/session_*"], False, id="glob-covers-both"),
    pytest.param([_SEND], False, id="send-only"),
    pytest.param([], False, id="neither"),
]


@pytest.fixture(autouse=True)
def _pinned_cli(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """The writer's version gate answers from a constant, not from the test host."""
    _floor_monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )


def _governed(grants: list[str], *, prompt: str = "charter\n", kas_policy: str = "derive") -> str:
    config = {"name": "x", "prompt": prompt, "allowedTools": list(grants)}
    auto_approve.govern_spec(config, agent_filename="x.json", source="test", kas_policy=kas_policy)
    return str(config["prompt"])


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


@pytest.mark.parametrize("grants,refused", _GRANT_CASES)
def test_the_line_follows_the_grants(grants: list[str], refused: bool) -> None:
    assert agent_specs.creates_without_send(grants) is refused
    prompt = _governed(grants)
    assert (RULE in prompt) is refused
    assert prompt == (f"charter\n\n{RULE}\n" if refused else "charter\n")


def test_a_second_write_does_not_repeat_the_line() -> None:
    assert _governed([_CREATE], prompt=_governed([_CREATE])).count(RULE) == 1


def test_a_fork_and_a_prompt_kept_in_a_file_are_left_alone() -> None:
    assert _governed([_CREATE], kas_policy="seed") == "charter\n"
    assert _governed([_CREATE], prompt="file://charter.md") == "file://charter.md"
    assert _governed([_CREATE], prompt="") == ""


def test_the_managed_stub_stays_the_managed_stub() -> None:
    """The managed contract is recognised by equality, so an extended stub would
    read as a custom persona and lose the contract."""
    stub = agent._NATIVE_PROMPT_STUB
    governed = _governed([_CREATE], prompt=stub)
    assert governed == stub
    assert agent.is_managed_prompt(governed)


def test_a_grant_the_ceiling_withholds_takes_the_line_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line is read from the FILTERED grants: a create that prompts is not refused."""
    monkeypatch.setattr(auto_approve, "_may_auto_approve", lambda ref: ref != _CREATE)
    assert RULE not in _governed([_CREATE])


@pytest.mark.parametrize("verdict", [True, False])
def test_the_gate_and_the_writer_ask_the_same_predicate(
    verdict: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swap the predicate's answer and both callers follow it, whatever the grants say."""
    monkeypatch.setattr(agent_specs, "creates_without_send", lambda _grants: verdict)
    assert (RULE in _governed([_SEND])) is verdict

    monkeypatch.setattr(handlers_sc, "_caller_auto_approvals", lambda *_a: {_SEND})
    state = MagicMock()
    state.get_slot.return_value = SimpleNamespace(agent="a", project="", agent_kind="")
    if not verdict:
        asyncio.run(handlers_sc._refuse_unprompted_seed(state, "chat-1"))
        return
    with pytest.raises(sc.SessionControlError) as refused:
        asyncio.run(handlers_sc._refuse_unprompted_seed(state, "chat-1"))
    assert refused.value.code == "message_needs_send_approval"
    assert refused.value.status == 403


def test_every_shipped_spec_carries_the_line_exactly_when_its_grants_cannot_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real rebuild, every spec it writes: the line is there if and only if the
    spec's own grants refuse a first message. A new conductor-like spec is covered
    without a row here."""
    agents = tmp_path / "agents"
    agents.mkdir()
    binary = tmp_path / "bin" / "kirocrew"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    assert os.environ["KIROCREW_HOME"]
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr(agent, "_KIROCREW_BIN", str(binary))
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", tmp_path / "kiro-global-mcp.json")
    monkeypatch.setattr(agent, "_DEFAULT_KIRO_HOOKS_DIR", tmp_path / "no-hooks")
    monkeypatch.setattr("kiro_crew.apps.bridges._mcp_json_path", lambda: agents / "kirocrew.json")

    agent.rebuild_agent_config()

    carrying: set[str] = set()
    for path in sorted(agents.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        refused = agent_specs.creates_without_send(agent_specs.spec_grants(spec, audit=False))
        prompt = spec.get("prompt") or ""
        assert (RULE in prompt) is refused, path.name
        assert prompt.count(RULE) <= 1, path.name
        if refused:
            carrying.add(path.stem)
    assert carrying >= {
        "kirocrew-conductor",
        "kirocrew-ledger-conductor",
        "kirocrew-pipeline-conductor",
        "kirocrew-security-conductor",
        "kirocrew-team-lead",
    }
    assert "kirocrew-worker" not in carrying
    assert "kirocrew" not in carrying


def test_the_description_states_the_rule_once_and_did_not_grow() -> None:
    tool = next(t for t in _session_tools() if t.name == "session_create")
    assert len(tool.description) <= _DESCRIPTION_CHARS_BEFORE
    assert tool.description.count("message_needs_send_approval") == 1
    assert "auto-approves session_create but not session_send" in tool.description
    assert "create, then session_send" in tool.description
    assert "message_needs_send_approval" not in json.dumps(tool.schema)


def test_the_server_instructions_point_at_the_line_instead_of_a_refused_try() -> None:
    text = _flat(mcp_instructions.SESSION_CONTROL)
    assert "unless your prompt says it is refused for this agent" in text
    assert "(on `message_needs_send_approval`" not in text


@pytest.mark.parametrize(
    "path",
    [
        _SRC / "builtin_skills" / "goal-conductor" / "SKILL.md",
        _SRC / "builtin_skills" / "team-lead" / "SKILL.md",
        _SRC / "builtin_skills" / "pipeline-conductor" / "SKILL.md",
        _SRC / "builtin_skills" / "security-conductor" / "SKILL.md",
        _SRC / "docs" / "session-control.md",
        _DOCS / "system-specs" / "modules" / "session-control.md",
    ],
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_the_skills_and_docs_say_it_in_the_same_words(path: Path) -> None:
    assert RULE in _flat(path.read_text(encoding="utf-8"))
