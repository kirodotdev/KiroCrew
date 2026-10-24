"""The crewmate custom-agent folder is write-protected like ``~/.kiro/agents``.

``config.paths.mate_agents_dir()`` (``<kiro home>/mates/agents``) holds each
crewmate's own spec and the mate conductor and worker specs. The agent's
file-edit tool must not write there; reads stay allowed, and Crew's own gateway
writers, which do not pass through the tool gate, keep working.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew import security
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig
from kiro_crew.mate_config import write_mate_config
from kiro_crew.security import is_sensitive_path, is_sensitive_write_path


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A throwaway home (``USERPROFILE`` too, for Windows) with no kiro-cli home override.

    The floor's isolated ``KIROCREW_HOME`` stays: the folder does not follow it.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    return tmp_path


def _edit(path: str, kind: str = "edit"):
    return HookManager(HooksConfig.from_dict({})).on_tool_call(
        "Editing", session_key="cli_chat", tool_kind=kind, raw_params={"path": path}
    )


def test_literal_is_the_home_relative_tail_of_mate_agents_dir(home: Path) -> None:
    # Drift guard: the literal is kept only to avoid a config->security import cycle.
    rel = paths_mod.mate_agents_dir().relative_to(home).as_posix()
    assert security.paths._KIRO_MATE_AGENTS_DIR == rel


def test_file_edit_writes_into_the_folder_are_refused(home: Path) -> None:
    mates = paths_mod.mate_agents_dir()
    for target in (mates / "ada.json", mates / "sub" / "deep.json", mates):
        assert is_sensitive_write_path(str(target)) is True, target
    assert is_sensitive_write_path("~/.kiro/mates/agents/ada.json") is True
    decision = _edit(str(mates / "ada.json"))
    assert decision.action == TOOL_DENY
    assert "ada.json" in decision.reason


def test_the_folder_follows_kiro_home(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    moved = tmp_path / "elsewhere"
    monkeypatch.setenv("KIRO_HOME", str(moved))
    assert is_sensitive_write_path(str(moved / "mates" / "agents" / "ada.json")) is True


def test_reads_stay_allowed(home: Path) -> None:
    target = str(paths_mod.mate_agents_dir() / "ada.json")
    assert is_sensitive_path(target) is False
    assert _edit(target, kind="read").action != TOOL_DENY


def test_neighbours_are_not_over_blocked(home: Path) -> None:
    assert is_sensitive_write_path("~/.kiro/mates/agents-backup/x.json") is False
    assert is_sensitive_write_path("~/notes.txt") is False


def test_the_gateway_writer_still_writes_the_folder(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kiro = home / ".kiro"
    user = kiro / "agents"
    user.mkdir(parents=True)
    (user / "builder.json").write_text(
        json.dumps({"name": "builder", "prompt": "hi", "tools": ["fs_read"]}), encoding="utf-8"
    )
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)

    path = write_mate_config("ada", "builder")

    assert path == paths_mod.mate_agents_dir() / "ada.json"
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "ada"
    # The same path the tool gate refuses: the writer does not pass through it.
    assert is_sensitive_write_path(str(path)) is True
