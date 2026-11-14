"""A dangling spec link refuses only the agent whose file name it carries.

Real files, real dangling symlinks and the real policy read: nothing is mocked.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from kiro_crew.dashboard.handlers import sessions as sessions_mod
from kiro_crew.dashboard.handlers.sessions_parts import tool_policy

_read = sessions_mod._read_managed_tool_policy_sync
_refused = sessions_mod.ManagedToolPolicyUnreadable


def _dangle(agents_dir: Path, link: str) -> Path:
    path = agents_dir / link
    os.symlink(agents_dir.parent / "gone" / link, path)
    return path


def _fresh_gateway_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_policy, "_UNREADABLE_SPEC_WARNED", set())
    monkeypatch.setattr(tool_policy, "_TOOL_POLICY_MEMO", tool_policy.AgentsDirMemo())


@pytest.fixture
def agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _fresh_gateway_state(monkeypatch)
    home = tmp_path / "agents"
    home.mkdir()
    (home / "b.json").write_text(json.dumps({"name": "b", "managedToolPolicy": {"exclude": ["x"]}}))
    _dangle(home, "a.json")
    return home


def test_the_agent_whose_spec_dangles_is_refused_and_another_is_allowed(agents_dir: Path) -> None:
    with pytest.raises(_refused) as refused:
        _read(agents_dir, "a")
    assert "a.json" in str(refused.value)
    assert _read(agents_dir, "b") == {"exclude": ["x"]}


def test_an_agent_with_no_spec_of_its_own_is_refused_by_a_link_of_unknown_owner(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(_refused):
        _read(agents_dir, "no-spec-agent")
    monkeypatch.setattr(tool_policy, "_DANGLING_LINK_OF_UNKNOWN_OWNER_REFUSES", False)
    assert _read(agents_dir, "no-spec-agent") is None


def test_the_same_holds_after_a_gateway_restart(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(_refused):
        _read(agents_dir, "a")
    _fresh_gateway_state(monkeypatch)
    with pytest.raises(_refused):
        _read(agents_dir, "a")
    assert _read(agents_dir, "b") == {"exclude": ["x"]}


def test_repointing_the_link_to_a_valid_target_resolves_the_agent(agents_dir: Path) -> None:
    with pytest.raises(_refused):
        _read(agents_dir, "a")
    target = agents_dir.parent / "real-a.json"
    target.write_text(json.dumps({"name": "a", "managedToolPolicy": {"exclude": ["y"]}}))
    (agents_dir / "a.json").unlink()
    os.symlink(target, agents_dir / "a.json")
    assert _read(agents_dir, "a") == {"exclude": ["y"]}


def test_a_namespaced_dangling_link_refuses_the_agent_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fresh_gateway_state(monkeypatch)
    monkeypatch.setattr(tool_policy, "_DANGLING_LINK_OF_UNKNOWN_OWNER_REFUSES", False)
    _dangle(tmp_path, "pkg-reviewer.json")
    assert _read(tmp_path, "other") is None
    with pytest.raises(_refused) as refused:
        _read(tmp_path, "reviewer")
    assert "pkg-reviewer.json" in str(refused.value)


def test_a_skipped_dangling_link_is_logged_once_with_its_path(
    agents_dir: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool_policy, "_DANGLING_LINK_OF_UNKNOWN_OWNER_REFUSES", False)
    with caplog.at_level(logging.WARNING, logger=tool_policy.logger.name):
        for _ in range(3):
            _read(agents_dir, "no-spec-agent")
            _read(agents_dir, "other-agent")
    assert len([r for r in caplog.records if "a.json" in r.getMessage()]) == 1


def test_a_dangling_link_in_another_case_refuses_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fresh_gateway_state(monkeypatch)
    _dangle(tmp_path, "Helper.json")
    with pytest.raises(_refused):
        _read(tmp_path, "helper")


def test_a_link_to_an_unparseable_file_still_refuses_every_spec_less_agent(
    agents_dir: Path,
) -> None:
    (agents_dir / "garbled-target.txt").write_text("{not json")
    os.symlink(agents_dir / "garbled-target.txt", agents_dir / "alias.json")
    with pytest.raises(_refused):
        _read(agents_dir, "no-spec-agent")


def test_a_broken_regular_spec_still_refuses_every_spec_less_agent(agents_dir: Path) -> None:
    (agents_dir / "garbled.json").write_text("{not json")
    with pytest.raises(_refused):
        _read(agents_dir, "no-spec-agent")


def test_a_spec_filed_under_another_name_refuses_only_agents_without_a_spec(
    agents_dir: Path,
) -> None:
    # reviewer-v2.json declares "reviewer" but dangles, so only its file name is known.
    _dangle(agents_dir, "reviewer-v2.json")
    with pytest.raises(_refused):
        _read(agents_dir, "reviewer")
    assert _read(agents_dir, "b") == {"exclude": ["x"]}


def test_a_spec_filed_under_another_name_is_skipped_when_the_branch_is_flipped(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dangle(agents_dir, "reviewer-v2.json")
    monkeypatch.setattr(tool_policy, "_DANGLING_LINK_OF_UNKNOWN_OWNER_REFUSES", False)
    assert _read(agents_dir, "reviewer") is None
