"""``kirocrew doctor`` names each agent spec whose shell or write allowlist a Codex
session does not apply.

Codex is outside ``ACP_BACKENDS_CREW_ENFORCES_SPEC_TOOLS_SETTINGS``: no codex-acp
mode makes every shell command and edit ask Kiro Crew, so neither allowlist
restricts a Codex session. A new chat says so in its session-start notice; this row
covers the sessions that get none, such as scheduled jobs.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import cli_doctor
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS
from kiro_crew.agent_sdk import backends
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX, ACP_BACKEND_KIRO
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.doctor_checks import agents

_ISSUE = "an agent spec sets an allowlist that Codex sessions do not apply"
_SHELL = {"execute_bash": {"denyByDefault": True, "allowedCommands": ["git status"]}}
_WRITE = {"fs_write": {"fallbackAction": "deny", "allowedPaths": ["docs/**"]}}


def _write_spec(directory: Path, name: str, tools_settings: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    spec = {"name": name, "prompt": "p", "toolsSettings": tools_settings}
    (directory / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _cfg(main: str = ACP_BACKEND_CODEX, member: str = ACP_BACKEND_KIRO) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = main
    cfg.agent.member_acp_backend = member
    return cfg


@pytest.fixture()
def agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "agents"
    monkeypatch.setattr(cli_doctor, "_agents_dir", lambda: directory)
    return directory


def _run(cfg: KiroCrewConfig, project: Path | None = None) -> list[str]:
    issues: list[str] = []
    agents._doctor_codex_unenforced_allowlists(cfg, str(project) if project else None, issues)
    return issues


@pytest.mark.parametrize(
    ("tools_settings", "named"),
    [
        (_SHELL, "a shell allowlist"),
        (_WRITE, "a write allowlist"),
        ({**_SHELL, **_WRITE}, "a shell and write allowlist"),
        # Every alias spelling reads the same.
        ({"shell": {"denyByDefault": True}}, "a shell allowlist"),
        ({"executeBash": {"denyByDefault": True}}, "a shell allowlist"),
        ({"fsWrite": {"fallbackAction": "deny"}}, "a write allowlist"),
    ],
)
def test_each_allowlist_is_named_with_its_agent(agents_dir, capsys, tools_settings, named):
    _write_spec(agents_dir, "probe", tools_settings)
    assert _run(_cfg()) == [_ISSUE]
    out = capsys.readouterr().out
    assert f"'probe' sets {named} that Codex sessions do not apply" in out
    assert "kiro-cli, KAS, claude or opencode" in out


@pytest.mark.parametrize(
    "tools_settings",
    [
        None,
        {},
        # A list without its deny-by-default switch restricts nothing anywhere.
        {"execute_bash": {"allowedCommands": ["git status"]}},
        {"fs_write": {"allowedPaths": ["docs/**"]}},
    ],
)
def test_a_spec_without_a_deny_switch_prints_nothing(agents_dir, capsys, tools_settings):
    _write_spec(agents_dir, "plain", tools_settings)
    assert _run(_cfg()) == []
    assert capsys.readouterr().out == ""


def test_a_project_spec_is_read_too(agents_dir, tmp_path, capsys):
    project = tmp_path / "project"
    _write_spec(project / ".kiro" / "agents", "local", _SHELL)
    assert _run(_cfg(), project) == [_ISSUE]
    assert "'local'" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("main", "member", "checked"),
    [
        (ACP_BACKEND_CODEX, ACP_BACKEND_KIRO, True),
        (ACP_BACKEND_KAS, ACP_BACKEND_CODEX, True),
        (ACP_BACKEND_KAS, ACP_BACKEND_KAS, False),
        (ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO, False),
    ],
)
def test_shown_only_when_a_selected_role_runs_codex(agents_dir, capsys, main, member, checked):
    _write_spec(agents_dir, "probe", _SHELL)
    assert _run(_cfg(main, member)) == ([_ISSUE] if checked else [])
    assert ("allowlists:" in capsys.readouterr().out) is checked


def test_silent_once_crew_enforces_the_allowlists_on_codex(agents_dir, capsys, monkeypatch):
    monkeypatch.setattr(
        backends,
        "ACP_BACKENDS_CREW_ENFORCES_SPEC_TOOLS_SETTINGS",
        backends.ACP_BACKENDS_CREW_ENFORCES_SPEC_TOOLS_SETTINGS | {ACP_BACKEND_CODEX},
    )
    _write_spec(agents_dir, "probe", _SHELL)
    assert _run(_cfg()) == []
    assert capsys.readouterr().out == ""


def test_codex_is_not_in_the_enforcing_set():
    """The row's premise: if this fails, Crew now enforces the lists and the row goes silent."""
    assert ACP_BACKEND_CODEX not in backends.ACP_BACKENDS_CREW_ENFORCES_SPEC_TOOLS_SETTINGS


def test_an_unwalkable_spec_tree_prints_nothing(monkeypatch, capsys):
    def boom():
        raise OSError("unreadable")

    monkeypatch.setattr(cli_doctor, "_agents_dir", boom)
    assert _run(_cfg()) == []
    assert capsys.readouterr().out == ""


def test_doctor_runs_the_row():
    """Read from source because running ``_doctor`` reaches the host."""
    src = inspect.getsource(cli_doctor._doctor)
    assert "agents._doctor_codex_unenforced_allowlists(cfg, proj or None, issues)" in src
