"""Daemon mise discovery never depends on the test host's installed tools."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from kiro_crew import env


@pytest.fixture
def mise_host(monkeypatch, tmp_path):
    """Model only the lookup filesystem; never run a host mise binary."""
    env._mise_bin.cache_clear()
    local = tmp_path / ".local" / "bin" / "mise"
    arm = Path("/opt/homebrew/bin/mise")
    intel = Path("/usr/local/bin/mise")
    files: set[Path] = set()
    executable: set[Path] = set()
    probes: list[Path] = []
    which = Mock(return_value=None)

    class LookupPath(type(tmp_path)):
        @classmethod
        def home(cls):
            return cls(tmp_path)

        def is_file(self):
            probes.append(self)
            return self in files

    real_access = os.access

    def access(path, mode, **kwargs):
        if isinstance(path, LookupPath):
            assert mode == os.X_OK
            return path in executable
        return real_access(path, mode, **kwargs)

    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    monkeypatch.setattr(env.shutil, "which", which)
    monkeypatch.setattr(env.sys, "platform", "darwin")
    monkeypatch.setattr(env, "Path", LookupPath)
    monkeypatch.setattr(env.os, "access", access)
    try:
        yield local, arm, intel, files, executable, probes, which
    finally:
        env._mise_bin.cache_clear()


def test_path_choice_wins_without_fallback_probes(mise_host):
    local, arm, intel, files, executable, probes, which = mise_host
    which.return_value = "/chosen/bin/mise"
    files.update([local, arm, intel])
    executable.update(files)
    assert env._mise_bin() == "/chosen/bin/mise"
    which.assert_called_once_with("mise")
    assert probes == []


@pytest.mark.parametrize("choice", ["local", "arm", "intel", "absent"])
def test_fallback_order(mise_host, choice):
    local, arm, intel, files, executable, probes, _ = mise_host
    candidates = [local, arm, intel]
    index = ["local", "arm", "intel", "absent"].index(choice)
    files.update(candidates[index:])
    executable.update(files)
    expected = str(candidates[index]) if index < len(candidates) else None
    assert env._mise_bin() == expected
    assert probes == candidates[: min(index + 1, len(candidates))]


@pytest.mark.parametrize("rejected", ["not_file", "not_executable"])
def test_unusable_fallback_does_not_hide_next_candidate(mise_host, rejected):
    local, arm, intel, files, executable, probes, _ = mise_host
    files.add(intel)
    executable.add(intel)
    # A directory can pass X_OK, but must not be selected as a binary.
    if rejected == "not_file":
        executable.update([local, arm])
    else:
        files.update([local, arm])
    assert env._mise_bin() == str(intel)
    assert probes == [local, arm, intel]


@pytest.mark.parametrize("platform", ["linux", "win32"])
@pytest.mark.parametrize("local_present", [False, True])
def test_other_platforms_keep_existing_lookup(monkeypatch, mise_host, platform, local_present):
    local, arm, intel, files, executable, probes, _ = mise_host
    monkeypatch.setattr(env.sys, "platform", platform)
    files.update([arm, intel])
    if local_present:
        files.add(local)
    executable.update(files)
    assert env._mise_bin() == (str(local) if local_present else None)
    assert probes == [local]


@pytest.mark.parametrize("install", ["arm", "intel"])
def test_activation_uses_homebrew_binary_from_bare_path(monkeypatch, mise_host, install):
    _, arm, intel, files, executable, _, _ = mise_host
    binary = arm if install == "arm" else intel
    files.add(binary)
    executable.add(binary)
    original_path = os.environ["PATH"]
    resolved_path = os.pathsep.join(["/configured/tools", original_path])
    target = {"PATH": original_path, "KEEP": "unchanged"}
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"PATH": resolved_path, "TOOL_ENV": "ready"}), stderr=""
        )
    )
    monkeypatch.setattr(env.subprocess, "run", run)
    assert env.activate_mise(target) == ["PATH", "TOOL_ENV"]
    run.assert_called_once_with(
        [str(binary), "env", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": original_path, "KEEP": "unchanged"},
        cwd=str(env.Path.home()),
    )
    assert target == {"PATH": resolved_path, "KEEP": "unchanged", "TOOL_ENV": "ready"}
    assert os.environ["PATH"] == original_path
