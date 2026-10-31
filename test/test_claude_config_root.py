"""Tests for the isolated Claude Code config root.

Every test fakes ``Path.home`` to ``tmp_path`` and clears both config-dir
environment variables, so none reads the real home directory or the operator's
real ``CLAUDE_CONFIG_DIR``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.acp.harness import claude_config_root


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_config_root.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("KIROCREW_CLAUDE_CONFIG_DIR", raising=False)
    return tmp_path


def _write_user_settings(home: Path, payload: dict) -> Path:
    source = home / ".claude"
    source.mkdir(parents=True, exist_ok=True)
    path = source / "settings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_default_root_is_dot_claudecrew(_isolated_home):
    assert claude_config_root.isolated_config_root() == _isolated_home / ".claudecrew"


def test_location_override_is_honored(_isolated_home, monkeypatch):
    monkeypatch.setenv("KIROCREW_CLAUDE_CONFIG_DIR", str(_isolated_home / "elsewhere"))
    assert claude_config_root.isolated_config_root() == _isolated_home / "elsewhere"


def test_operator_config_dir_is_left_untouched(_isolated_home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/operator/own/claude")
    env = dict(CLAUDE_CONFIG_DIR="/operator/own/claude")
    claude_config_root.apply_to_environ(env)
    assert env["CLAUDE_CONFIG_DIR"] == "/operator/own/claude"
    # Nothing is seeded when the operator opted out of isolation.
    assert not (_isolated_home / ".claudecrew").exists()


def test_apply_sets_config_dir_and_strips_permissions(_isolated_home):
    _write_user_settings(
        _isolated_home,
        {
            "model": "sonnet",
            "env": {"FOO": "bar"},
            "permissions": {"allow": ["Bash"]},
            "defaultMode": "acceptEdits",
        },
    )
    env: dict[str, str] = {}
    claude_config_root.apply_to_environ(env)
    root = _isolated_home / ".claudecrew"
    assert env["CLAUDE_CONFIG_DIR"] == str(root)
    seeded = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    # The whole point: no inherited permission channel carries across.
    assert "permissions" not in seeded
    assert "defaultMode" not in seeded
    # Non-permission preferences DO carry.
    assert seeded["model"] == "sonnet"
    assert seeded["env"] == {"FOO": "bar"}


def test_existing_root_settings_are_not_clobbered(_isolated_home):
    root = _isolated_home / ".claudecrew"
    root.mkdir(mode=0o700)
    (root / "settings.json").write_text('{"model": "opus"}', encoding="utf-8")
    _write_user_settings(_isolated_home, {"model": "sonnet", "permissions": {"allow": ["Bash"]}})
    claude_config_root.ensure_isolated_config_root()
    kept = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    assert kept == {"model": "opus"}


def test_no_user_settings_yields_empty_permission_free_seed(_isolated_home):
    claude_config_root.ensure_isolated_config_root()
    seeded = json.loads((_isolated_home / ".claudecrew" / "settings.json").read_text("utf-8"))
    assert seeded == {}


def test_credentials_copied_off_darwin(_isolated_home, monkeypatch):
    monkeypatch.setattr(claude_config_root.sys, "platform", "linux")
    source = _isolated_home / ".claude"
    source.mkdir(parents=True)
    (source / ".credentials.json").write_text('{"token": "secret"}', encoding="utf-8")
    claude_config_root.ensure_isolated_config_root()
    copied = _isolated_home / ".claudecrew" / ".credentials.json"
    assert json.loads(copied.read_text(encoding="utf-8")) == {"token": "secret"}


def test_credentials_not_copied_on_darwin(_isolated_home, monkeypatch):
    monkeypatch.setattr(claude_config_root.sys, "platform", "darwin")
    source = _isolated_home / ".claude"
    source.mkdir(parents=True)
    (source / ".credentials.json").write_text('{"token": "secret"}', encoding="utf-8")
    claude_config_root.ensure_isolated_config_root()
    assert not (_isolated_home / ".claudecrew" / ".credentials.json").exists()


def test_existing_credentials_not_clobbered_off_darwin(_isolated_home, monkeypatch):
    monkeypatch.setattr(claude_config_root.sys, "platform", "linux")
    root = _isolated_home / ".claudecrew"
    root.mkdir(mode=0o700)
    (root / ".credentials.json").write_text('{"token": "kept"}', encoding="utf-8")
    source = _isolated_home / ".claude"
    source.mkdir(parents=True)
    (source / ".credentials.json").write_text('{"token": "new"}', encoding="utf-8")
    claude_config_root.ensure_isolated_config_root()
    kept = json.loads((root / ".credentials.json").read_text(encoding="utf-8"))
    assert kept == {"token": "kept"}
