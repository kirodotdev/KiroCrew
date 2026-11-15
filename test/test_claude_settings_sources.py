"""Tests for :func:`other_sources_define_available_models`.

Every path is passed explicitly (``config_dir``, ``managed_dir``) or set under
``tmp_path``, so no test reads the real home directory or the real managed path.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from pathlib import Path

import pytest

from kiro_crew.acp.claude_settings_sources import (
    family_entry_covers,
    other_sources_define_available_models,
    read_claude_settings_bytes,
    user_models_a_family_entry_covers,
)


def _write(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _managed(tmp_path: Path) -> Path:
    """An empty managed settings directory under *tmp_path*."""
    managed = tmp_path / "managed"
    managed.mkdir(exist_ok=True)
    return managed


def _check(work_dir: Path, config_dir: Path, managed_dir: Path) -> bool:
    return other_sources_define_available_models(
        work_dir, config_dir=config_dir, managed_dir=managed_dir
    )


def test_nothing_exists_is_false(tmp_path):
    assert _check(tmp_path / "work", tmp_path / "user", _managed(tmp_path)) is False


def test_user_settings_with_a_list_is_true(tmp_path):
    user = tmp_path / "user"
    _write(user / "settings.json", {"availableModels": ["opus"]})
    assert _check(tmp_path / "work", user, _managed(tmp_path)) is True


def test_user_settings_with_an_empty_list_is_true(tmp_path):
    user = tmp_path / "user"
    _write(user / "settings.json", {"availableModels": []})
    assert _check(tmp_path / "work", user, _managed(tmp_path)) is True


def test_user_settings_without_the_key_is_false(tmp_path):
    user = tmp_path / "user"
    _write(user / "settings.json", {"model": "opus"})
    assert _check(tmp_path / "work", user, _managed(tmp_path)) is False


def test_project_settings_with_the_key_is_true(tmp_path):
    work = tmp_path / "work"
    _write(work / ".claude" / "settings.json", {"availableModels": ["haiku"]})
    assert _check(work, tmp_path / "user", _managed(tmp_path)) is True


def test_project_local_settings_is_not_a_source(tmp_path):
    """settings.local.json never counts: it is Crew's own seed, or a project's file
    the CLI never loads beside Crew's list."""
    work = tmp_path / "work"
    _write(work / ".claude" / "settings.local.json", {"availableModels": ["haiku"]})
    assert _check(work, tmp_path / "user", _managed(tmp_path)) is False


def test_managed_settings_with_the_key_is_true(tmp_path):
    managed = _managed(tmp_path)
    _write(managed / "managed-settings.json", {"availableModels": ["sonnet"]})
    assert _check(tmp_path / "work", tmp_path / "user", managed) is True


def test_invalid_json_is_false(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "settings.json").write_text('{"availableModels": [', encoding="utf-8")
    assert _check(tmp_path / "work", user, _managed(tmp_path)) is False


def test_a_json_list_is_not_an_object_so_false(tmp_path):
    user = tmp_path / "user"
    _write(user / "settings.json", ["availableModels"])
    assert _check(tmp_path / "work", user, _managed(tmp_path)) is False


def test_utf16le_user_settings_with_the_key_is_true(tmp_path):
    """Claude Code reads a file opening with FF FE as UTF-16LE, so the check does too."""
    user = tmp_path / "user"
    user.mkdir()
    (user / "settings.json").write_bytes(
        b"\xff\xfe" + json.dumps({"availableModels": ["opus"]}).encode("utf-16-le")
    )
    assert _check(tmp_path / "work", user, _managed(tmp_path)) is True


def test_utf16le_user_settings_without_the_key_is_false(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "settings.json").write_bytes(
        b"\xff\xfe" + json.dumps({"model": "opus"}).encode("utf-16-le")
    )
    assert _check(tmp_path / "work", user, _managed(tmp_path)) is False


def test_non_utf8_content_is_false(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "settings.json").write_bytes(b'{"availableModels": "\xff\xfe"}')
    assert _check(tmp_path / "work", user, _managed(tmp_path)) is False


def test_config_dir_defaults_to_claude_config_dir_env(tmp_path, monkeypatch):
    user = tmp_path / "from-env"
    _write(user / "settings.json", {"availableModels": ["opus"]})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(user))
    assert (
        other_sources_define_available_models(tmp_path / "work", managed_dir=_managed(tmp_path))
        is True
    )


def test_empty_claude_config_dir_env_falls_back_to_home(tmp_path, monkeypatch):
    """An empty CLAUDE_CONFIG_DIR is unset, so the default home directory applies."""
    fake_home = tmp_path / "home"
    _write(fake_home / ".claude" / "settings.json", {"availableModels": ["opus"]})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert (
        other_sources_define_available_models(tmp_path / "work", managed_dir=_managed(tmp_path))
        is True
    )


def _raise_runtime_error() -> Path:
    raise RuntimeError("Could not determine home directory")


def test_no_resolvable_home_still_checks_project_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", _raise_runtime_error)
    work = tmp_path / "work"
    _write(work / ".claude" / "settings.json", {"availableModels": ["opus"]})
    assert other_sources_define_available_models(work, managed_dir=_managed(tmp_path)) is True


def test_no_resolvable_home_and_no_project_file_is_false(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", _raise_runtime_error)
    assert (
        other_sources_define_available_models(tmp_path / "work", managed_dir=_managed(tmp_path))
        is False
    )


def test_managed_settings_dir_is_one_path_per_os(monkeypatch):
    from kiro_crew import platform_compat
    from kiro_crew.platform_compat import claude_code_managed_settings_dir

    assert isinstance(claude_code_managed_settings_dir(), pathlib.PurePath)
    monkeypatch.setattr(platform_compat, "IS_MACOS", True)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    assert claude_code_managed_settings_dir() == Path("/Library/Application Support/ClaudeCode")
    monkeypatch.setattr(platform_compat, "IS_MACOS", False)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert claude_code_managed_settings_dir() == Path("C:\\Program Files\\ClaudeCode")
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    assert claude_code_managed_settings_dir() == Path("/etc/claude-code")


def test_managed_dropin_with_the_key_is_true(tmp_path):
    managed = _managed(tmp_path)
    _write(managed / "managed-settings.d" / "50-models.json", {"availableModels": ["opus"]})
    assert _check(tmp_path / "work", tmp_path / "user", managed) is True


def test_managed_dropin_with_a_leading_dot_is_skipped(tmp_path):
    managed = _managed(tmp_path)
    _write(managed / "managed-settings.d" / ".hidden.json", {"availableModels": ["opus"]})
    assert _check(tmp_path / "work", tmp_path / "user", managed) is False


def test_managed_dropin_without_a_json_suffix_is_skipped(tmp_path):
    managed = _managed(tmp_path)
    _write(managed / "managed-settings.d" / "models.txt", {"availableModels": ["opus"]})
    assert _check(tmp_path / "work", tmp_path / "user", managed) is False


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_managed_dropin_that_is_a_symlink_to_json_is_true(tmp_path):
    managed = _managed(tmp_path)
    real = _write(tmp_path / "elsewhere.json", {"availableModels": ["opus"]})
    dropin_dir = managed / "managed-settings.d"
    dropin_dir.mkdir()
    (dropin_dir / "50-link.json").symlink_to(real)
    assert _check(tmp_path / "work", tmp_path / "user", managed) is True


def test_managed_dropin_dir_that_is_a_regular_file_is_false(tmp_path):
    managed = _managed(tmp_path)
    (managed / "managed-settings.d").write_text("not a directory", encoding="utf-8")
    assert _check(tmp_path / "work", tmp_path / "user", managed) is False


def test_read_claude_settings_bytes_raises_for_a_missing_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_claude_settings_bytes(tmp_path / "absent.json", follow_links=False)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_read_claude_settings_bytes_refuses_a_link_when_not_following(tmp_path):
    real = _write(tmp_path / "real.json", {"availableModels": ["opus"]})
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert read_claude_settings_bytes(link, follow_links=False) is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_project_settings_reached_through_a_symlink_is_false(tmp_path):
    """A cloned repository controls .claude/settings.json, so a link there is not followed."""
    real = _write(tmp_path / "elsewhere.json", {"availableModels": ["haiku"]})
    work = tmp_path / "work"
    (work / ".claude").mkdir(parents=True)
    (work / ".claude" / "settings.json").symlink_to(real)
    assert _check(work, tmp_path / "user", _managed(tmp_path)) is False


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_user_settings_reached_through_a_symlink_is_true(tmp_path):
    """A dotfile-managed user settings file is commonly a link, and it is still read."""
    real = _write(tmp_path / "dotfiles" / "claude-settings.json", {"availableModels": ["opus"]})
    user = tmp_path / "user"
    user.mkdir()
    (user / "settings.json").symlink_to(real)
    assert _check(tmp_path / "work", user, _managed(tmp_path)) is True


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_project_settings_that_is_a_fifo_is_false_and_does_not_block(tmp_path):
    work = tmp_path / "work"
    (work / ".claude").mkdir(parents=True)
    os.mkfifo(work / ".claude" / "settings.json")
    assert _check(work, tmp_path / "user", _managed(tmp_path)) is False


def test_oversized_settings_file_is_false(tmp_path):
    """A file past the 1 MiB ceiling is not read, even when its head names the key."""
    user = tmp_path / "user"
    user.mkdir()
    (user / "settings.json").write_text(
        '{"availableModels": ["' + "a" * (1 << 20) + '"]}', encoding="utf-8"
    )
    assert _check(tmp_path / "work", user, _managed(tmp_path)) is False


@pytest.mark.parametrize(
    ("entry", "model_id", "covered"),
    [
        # The SDK's own example: a version prefix allows the id that extends it.
        ("claude-opus-5", "claude-opus-5-5", True),
        ("claude-opus-5[1m]", "claude-opus-5-5[1m]", True),
        ("claude-sonnet-4-5", "claude-sonnet-4-5-20250929", True),
        # The same id is listed already, not covered.
        ("claude-opus-5", "claude-opus-5", False),
        # A longer number is another model, not an extension.
        ("claude-opus-5", "claude-opus-55", False),
        ("claude-opus-5-5", "claude-opus-5", False),
        ("claude-opus-5", "claude-sonnet-5-5", False),
        # A family alias has no version: naming an id beside it changes what it runs.
        ("opus", "opus-5-5", False),
        # The window marker must agree.
        ("claude-opus-5", "claude-opus-5-5[1m]", False),
        ("claude-opus-5[1m]", "claude-opus-5-5", False),
        ("", "claude-opus-5-5", False),
    ],
)
def test_family_entry_covers(entry, model_id, covered):
    assert family_entry_covers(entry, model_id) is covered


def _covered(tmp_path: Path, settings: object, managed: Path | None = None) -> list[str]:
    user = tmp_path / "user"
    _write(user / "settings.json", settings)
    return user_models_a_family_entry_covers(
        config_dir=user, managed_dir=managed or _managed(tmp_path)
    )


_FAMILY_SETTINGS = {
    "availableModels": ["claude-opus-4-8", "claude-opus-5[1m]", "claude-opus-5"],
    "modelOverrides": {
        "claude-opus-5": "provider-opus-5",
        "claude-sonnet-5-5": "provider-sonnet-5-5",
        "claude-opus-5-5[1m]": "provider-opus-5-5[1m]",
        "claude-opus-5-5": "provider-opus-5-5",
    },
}


def test_covered_models_are_the_override_keys_a_listed_prefix_allows(tmp_path):
    # In modelOverrides order; the listed key and the uncovered family are left out.
    assert _covered(tmp_path, _FAMILY_SETTINGS) == ["claude-opus-5-5[1m]", "claude-opus-5-5"]


def test_covered_models_never_name_an_id_the_settings_do_not_hold(tmp_path):
    settings = {"availableModels": ["claude-opus-5"], "modelOverrides": {}}
    assert _covered(tmp_path, settings) == []


def test_covered_models_need_both_keys(tmp_path):
    assert _covered(tmp_path, {"availableModels": ["claude-opus-5"]}) == []
    assert _covered(tmp_path, {"modelOverrides": {"claude-opus-5-5": "x"}}) == []
    assert _covered(tmp_path, {"availableModels": "claude-opus-5", "modelOverrides": []}) == []


def test_covered_models_are_empty_without_a_user_file(tmp_path):
    assert (
        user_models_a_family_entry_covers(
            config_dir=tmp_path / "user", managed_dir=_managed(tmp_path)
        )
        == []
    )


def test_exact_matching_in_managed_settings_covers_nothing(tmp_path):
    """Under ``availableModelsMatch: "exact"`` an entry allows only its own version."""
    managed = _managed(tmp_path)
    _write(managed / "managed-settings.json", {"availableModelsMatch": "exact"})
    assert _covered(tmp_path, _FAMILY_SETTINGS, managed) == []
    _write(managed / "managed-settings.json", {"availableModelsMatch": "prefix"})
    assert _covered(tmp_path, _FAMILY_SETTINGS, managed) == [
        "claude-opus-5-5[1m]",
        "claude-opus-5-5",
    ]


def test_a_family_alias_entry_adds_no_model(tmp_path):
    """Naming a specific id beside ``opus`` changes which model the alias runs."""
    settings = {
        "availableModels": ["opus", "sonnet"],
        "modelOverrides": {"opus-5-5": "provider-opus-5-5", "claude-opus-5-5": "x"},
    }
    assert _covered(tmp_path, settings) == []


def test_an_entry_adds_no_model_with_another_window_marker(tmp_path):
    """The SDK text does not say that ``claude-opus-5`` covers ``claude-opus-5-5[1m]``."""
    settings = {
        "availableModels": ["claude-opus-5"],
        "modelOverrides": {"claude-opus-5-5[1m]": "provider-opus-5-5[1m]"},
    }
    assert _covered(tmp_path, settings) == []
