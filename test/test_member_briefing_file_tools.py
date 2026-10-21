"""A crewmate's briefing instruction follows what its spec can write.

Item 6 of the crewmate protocol tells a member to keep its briefing "with your
file tools". A member whose spec mounts no built-in that writes files -- every
conductor template, by design -- is told instead that it cannot write the file and
to keep its working memory in the DM thread, and its briefing is still read. These
tests pin the predicate (``member_mate_tools.spec_has_file_write`` /
``binding_has_file_write``) and both renderings of the member section.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew import agent, member_mate_tools
from kiro_crew.agent_files import CONDUCTOR_AGENT_FILENAME
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.context_assembly import member as member_ctx
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.members import member_briefing_path

CREW = "code-reviewer"
BRIEFING = "This week: crash issues."

# The briefing read fails CLOSED without O_NOFOLLOW (Windows), and the section then
# renders the platform variant, so a test of the other two variants is POSIX-only.
requires_nofollow = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason="briefing reads fail closed without O_NOFOLLOW",
)


@pytest.fixture(name="agents")
def agents_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty user-level agents directory, shared by the installer and the reader."""
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: agents)
    return agents


def _install_conductor(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Run the real ``kirocrew-conductor`` installer and return the spec it wrote.

    Stubbed as ``test_conductor_agent`` stubs it. The default spec it derives from
    mounts ``fs_write`` and ``code`` on purpose, so what this reads is the
    installer's own tool list, not a fixture that happens to lack a writer.
    """
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )
    monkeypatch.setattr(
        agent,
        "build_agent_config",
        lambda: {
            "name": "kirocrew",
            "prompt": "file://x",
            "mcpServers": {
                "kirocrew-core": {"command": "/resolved/kirocrew", "args": ["mcp-core"]}
            },
            "tools": ["execute_bash", "fs_read", "fs_write", "code", "@kirocrew-core"],
            "allowedTools": ["@kirocrew-core"],
        },
    )
    monkeypatch.setattr(
        agent, "_kirocrew_mcp_invocation", lambda sub: ("/resolved/kirocrew", [sub])
    )
    monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: True)
    assert agent._install_conductor_agent() is True
    path = agent.kiro_agents_dir_path() / CONDUCTOR_AGENT_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def _write_spec(agents: Path, name: str, spec: dict) -> None:
    (agents / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _write_briefing() -> Path:
    path = member_briefing_path(CREW)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BRIEFING, encoding="utf-8")
    return path


def _section(kiro_agent: str) -> str:
    """The member section for one crewmate bound to the spec named *kiro_agent*."""
    cfg = KiroCrewConfig()
    cfg.agents = {CREW: KiroCrewAgentConfig(kiro_agent=kiro_agent, description="Watches issues")}
    with patch("kiro_crew.context.KiroCrewConfig.load", return_value=cfg):
        return member_ctx.build_member_section(CREW)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"tools": ["fs_read", "fs_write"]}, True),
        ({"tools": ["write"]}, True),
        ({"tools": ["code"]}, True),
        ({"tools": ["*"]}, True),
        ({"tools": "*"}, True),
        ({"tools": ["@builtin"]}, True),
        # A spec written in KAS ids: ``fs_write``'s mount family writes too, while
        # ``delete_file`` cannot write a briefing.
        ({"tools": ["str_replace"]}, True),
        ({"tools": ["fs_append"]}, True),
        ({"tools": ["delete_file"]}, False),
        # ``code`` still writes when only ``fs_write`` is excluded.
        ({"tools": ["*"], "excludedTools": ["fs_write"]}, True),
        # An alias excludes the tool it names, and a kiro-cli name its KAS family.
        ({"tools": ["fs_write", "code"], "excludedTools": ["write", "code"]}, False),
        ({"tools": ["str_replace"], "excludedTools": ["fs_write"]}, False),
        ({"tools": ["fs_write"], "excludedTools": ["str_replace"]}, True),
        ({"tools": ["@builtin"], "excludedTools": ["*"]}, False),
        # Only a listed ``"*"`` excludes everything, as ``spec_grants_tool_search``
        # reads it: an ``@builtin`` entry or a bare string withholds no writer.
        ({"tools": ["fs_write"], "excludedTools": ["@builtin"]}, True),
        ({"tools": ["*"], "excludedTools": "*"}, True),
        # The shell and the readers are not file tools.
        ({"tools": ["execute_bash", "shell", "fs_read", "read", "grep", "glob"]}, False),
        ({"tools": ["@kirocrew-core", "@kirocrew-work"]}, False),
        ({"tools": []}, False),
        ({}, False),
        ({"tools": "fs_write"}, False),
        (["fs_write"], False),
    ],
)
def test_spec_has_file_write_reads_tools_then_exclusions(spec: object, expected: bool) -> None:
    assert member_mate_tools.spec_has_file_write(spec) is expected


def test_the_writer_names_follow_governance_and_the_alias_table() -> None:
    """The writers are spelled beside the predicate, so they are joined with their owner.

    Governance's builtin map is where a tool is classed as a file writer, so a
    writer added there must show up here. Every short spelling in the shared alias
    table then counts exactly when the tool it names writes: ``write`` does, and the
    shell and read aliases do not.
    """
    from kiro_crew.platform.governance import BUILTIN_TOOL_SCOPES
    from kiro_crew.platform.tool_names import KIRO_TOOL_ALIASES

    writers = {name for name, scopes in BUILTIN_TOOL_SCOPES.items() if "filesystem.write" in scopes}
    assert member_mate_tools._FILE_WRITE_TOOLS == writers
    assert KIRO_TOOL_ALIASES, "the shared alias table is empty"
    for alias, target in KIRO_TOOL_ALIASES.items():
        has_write = member_mate_tools.spec_has_file_write({"tools": [alias]})
        assert has_write is (target in writers), alias


def test_binding_has_file_write_reads_the_spec_the_member_runs(agents: Path) -> None:
    _write_spec(agents, "reader", {"tools": ["fs_read", "execute_bash"]})
    _write_spec(agents, "editor", {"tools": ["fs_read", "fs_write"]})
    assert member_mate_tools.binding_has_file_write(SimpleNamespace(kiro_agent="reader")) is False
    assert member_mate_tools.binding_has_file_write(SimpleNamespace(kiro_agent="editor")) is True
    # Nothing to read keeps the upkeep wording every crewmate had before the check.
    (agents / "broken.json").write_bytes(b"\xff")
    for unread in ("", "missing", "../reader", "broken", 7):
        binding = SimpleNamespace(kiro_agent=unread)
        assert member_mate_tools.binding_has_file_write(binding) is True, unread
    assert member_mate_tools.binding_has_file_write(SimpleNamespace()) is True


def test_a_row_that_binds_no_template_is_read_as_the_default_one(agents: Path) -> None:
    """``member_template_id`` runs ``kirocrew`` for a blank row, so that spec answers."""
    _write_spec(agents, "kirocrew", {"tools": ["fs_read"], "excludedTools": []})
    for blank in (SimpleNamespace(kiro_agent=""), SimpleNamespace()):
        assert member_mate_tools.binding_has_file_write(blank) is False
    _write_spec(agents, "kirocrew", {"tools": ["fs_read", "fs_write"]})
    assert member_mate_tools.binding_has_file_write(SimpleNamespace(kiro_agent="")) is True


@requires_nofollow
def test_a_conductor_crewmate_is_told_it_cannot_write_its_briefing(
    agents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert member_mate_tools.spec_has_file_write(_install_conductor(monkeypatch)) is False
    path = _write_briefing()
    section = _section("kirocrew-conductor")
    assert member_ctx._MEMBER_BRIEFING_ITEM_NO_FILE_TOOLS in section
    assert member_ctx._MEMBER_BRIEFING_ITEM not in section
    assert "with your file tools" not in section
    # Still read, under a header that disowns the upkeep, names the file the user
    # edits or deletes, and says the note may be out of date.
    header = (
        f"[CURRENT ASSIGNMENT — not yours to maintain, injected from {path}; "
        "only the user edits or deletes it, so it may be out of date]"
    )
    assert f"{header}\n{BRIEFING}" in section
    assert "— yours to maintain" not in section
    assert member_ctx._MEMBER_DASHBOARD_ITEM in section


@requires_nofollow
def test_a_briefing_the_member_cannot_write_is_marked_as_possibly_stale(
    agents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A briefing kept before the member lost its file tools stays injected.

    Nothing the member holds can change it, so it would read as current
    priorities forever. Item 6 says the thread wins where the two disagree and
    sends a stale one to the user, the one person who can edit or delete it.
    """
    _install_conductor(monkeypatch)
    _write_briefing()
    section = _section("kirocrew-conductor")
    item = " ".join(member_ctx._MEMBER_BRIEFING_ITEM_NO_FILE_TOOLS.split())
    assert "possibly out of date" in item and "the thread wins" in item
    assert "tell the user, who can edit or delete the file it names" in item
    assert BRIEFING in section


@requires_nofollow
def test_an_empty_briefing_is_not_an_invitation_to_write_one(
    agents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_conductor(monkeypatch)
    section = _section("kirocrew-conductor")
    assert "(empty — your template cannot write this file; keep your working memory" in section
    assert "write your first briefing" not in section


@requires_nofollow
def test_a_template_that_mounts_fs_write_keeps_the_upkeep_instruction(agents: Path) -> None:
    _write_spec(agents, "writer", {"name": "writer", "tools": ["fs_read", "fs_write"]})
    path = _write_briefing()
    section = _section("writer")
    assert member_ctx._MEMBER_HOW_YOU_WORK in section
    assert member_ctx._MEMBER_BRIEFING_ITEM_NO_FILE_TOOLS not in section
    assert f"[CURRENT ASSIGNMENT — yours to maintain, injected from {path}]\n{BRIEFING}" in section


def test_the_platform_gap_outranks_the_tool_gap(
    agents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where layer 4 is never injected, that is what the member is told."""
    _install_conductor(monkeypatch)
    monkeypatch.setattr("kiro_crew.context.member_briefing_supported", lambda: False)
    section = _section("kirocrew-conductor")
    assert member_ctx._MEMBER_BRIEFING_ITEM_UNAVAILABLE in section
    assert member_ctx._MEMBER_BRIEFING_ITEM_NO_FILE_TOOLS not in section
    assert "[CURRENT ASSIGNMENT — not available on this platform]" in section


def test_the_no_write_item_sends_working_memory_only_to_the_thread() -> None:
    # Whitespace folded, so re-wrapping the item cannot fail this.
    item = " ".join(member_ctx._MEMBER_BRIEFING_ITEM_NO_FILE_TOOLS.split())
    assert "DM thread" in item and "any other means" in item
    # No tool is named for writing the file anyway: no shell, and no mate tool,
    # since none of them writes the briefing.
    assert not any(word in item.lower() for word in ("shell", "bash", "update_self", "@kirocrew"))
