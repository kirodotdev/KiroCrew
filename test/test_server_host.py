"""The Notes backend's host programs: the file-manager binary, the reveal and the folder chooser.

These units live in ``kiro_crew.apps.builtins.md_notebook.server_host`` and are reached
through ``server``, the backend's facade, as every caller reaches them. The route tests in
``test_md_notebook.py`` stub ``_reveal_binary`` and never run the macOS chooser, so the
platform choice, the refusals and the chooser's output handling are pinned here, each spawn
replaced by a recorder so no file manager or dialog ever opens.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.apps.builtins.md_notebook import git_ops, server


class _Spawn:
    """Records each ``subprocess.run`` call and answers with a fixed result."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    def __call__(self, argv: list[str], **kwargs: Any) -> SimpleNamespace:
        self.calls.append((argv, kwargs))
        return self._result


def _install_spawn(monkeypatch: pytest.MonkeyPatch, spawn: _Spawn) -> None:
    """Route the backend's ``subprocess`` name, in every module holding it, to *spawn*."""
    fake = SimpleNamespace(run=spawn, SubprocessError=subprocess.SubprocessError)
    monkeypatch.setattr(server, "subprocess", fake)


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("darwin", "darwin"), ("linux", "linux"), ("freebsd14", "linux")],
)
def test_the_reveal_binary_is_the_platform_constant_when_it_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, expected: str
) -> None:
    binaries = {"darwin": tmp_path / "open", "linux": tmp_path / "xdg-open"}
    for path in binaries.values():
        path.write_text("", encoding="utf-8")
    monkeypatch.setattr(server, "REVEAL_BINARIES", {k: str(v) for k, v in binaries.items()})
    monkeypatch.setattr(sys, "platform", platform)
    assert server._reveal_binary() == str(binaries[expected])


def test_a_missing_reveal_binary_reads_as_unsupported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server, "REVEAL_BINARIES", {"linux": str(tmp_path / "absent")})
    monkeypatch.setattr(sys, "platform", "linux")
    assert server._reveal_binary() is None


def test_windows_reveals_through_the_shell_api_rather_than_a_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert server._reveal_binary() == "explorer"


def test_the_reveal_refuses_when_no_file_manager_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = _Spawn()
    _install_spawn(monkeypatch, spawn)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(server, "_reveal_binary", lambda: None)
    with pytest.raises(server.ApiError) as excinfo:
        server._reveal_folder_sync("/srv/vault/.trash")
    assert (excinfo.value.status, excinfo.value.code) == (501, "folder_open_unsupported")
    assert spawn.calls == []


def test_the_reveal_runs_the_fixed_binary_with_one_path_and_the_trusted_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn = _Spawn()
    _install_spawn(monkeypatch, spawn)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(server, "_reveal_binary", lambda: "/usr/bin/xdg-open")
    server._reveal_folder_sync("/srv/vault/.trash")
    [(argv, kwargs)] = spawn.calls
    assert argv == ["/usr/bin/xdg-open", "/srv/vault/.trash"]
    assert kwargs["timeout"] == server.REVEAL_TIMEOUT_SEC
    assert kwargs["env"]["PATH"] == server._trusted_env()["PATH"]


def test_a_failed_reveal_is_a_500(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_spawn(monkeypatch, _Spawn(returncode=3))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(server, "_reveal_binary", lambda: "/usr/bin/xdg-open")
    with pytest.raises(server.ApiError) as excinfo:
        server._reveal_folder_sync("/srv/vault/.trash")
    assert (excinfo.value.status, excinfo.value.code) == (500, "folder_open_failed")


@pytest.mark.parametrize(
    ("platform", "suppressed", "expected"),
    [("darwin", False, True), ("darwin", True, False), ("linux", False, False)],
)
def test_the_folder_chooser_is_macos_only_and_suppressible(
    monkeypatch: pytest.MonkeyPatch, platform: str, suppressed: bool, expected: bool
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    if suppressed:
        monkeypatch.setenv("MD_NOTEBOOK_NO_PICKER", "1")
    else:
        monkeypatch.delenv("MD_NOTEBOOK_NO_PICKER", raising=False)
    assert server.pick_folder_supported() is expected


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [("/srv/notes/\n", "/srv/notes"), ("/srv/notes\n", "/srv/notes"), ("\n", None)],
)
def test_the_folder_chooser_returns_the_path_without_its_trailing_slash(
    monkeypatch: pytest.MonkeyPatch, stdout: str, expected: str | None
) -> None:
    spawn = _Spawn(stdout=stdout)
    _install_spawn(monkeypatch, spawn)
    assert server._pick_folder_sync() == expected
    [(argv, kwargs)] = spawn.calls
    assert argv == ["/usr/bin/osascript", "-e", server.PICK_FOLDER_SCRIPT]
    assert kwargs["timeout"] == server.PICK_FOLDER_TIMEOUT_SEC
    assert kwargs["env"]["PATH"] == server._trusted_env()["PATH"]


def test_a_failed_folder_chooser_is_a_500(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_spawn(monkeypatch, _Spawn(returncode=1))
    with pytest.raises(server.ApiError) as excinfo:
        server._pick_folder_sync()
    assert (excinfo.value.status, excinfo.value.code) == (500, "folder_chooser_failed")


def test_the_trusted_env_pins_path_on_posix_and_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/srv/agent-writable/bin:/usr/bin")
    monkeypatch.setenv("DISPLAY", ":0")
    env = server._trusted_env()
    assert env["DISPLAY"] == ":0"
    if os.name == "posix":
        assert env["PATH"] == git_ops.TRUSTED_PATH
    else:
        assert env["PATH"] == "/srv/agent-writable/bin:/usr/bin"
