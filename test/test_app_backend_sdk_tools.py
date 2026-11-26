"""The backend SDK's tool lookups and bind-address predicate (seam level 18).

What is pinned: each name is the gateway's own lookup, read at call time, published at
level 18; the template values are the renderer's own, without the two that name one
app; and the process probes that would answer about another process
(``find_port_listeners``, ``get_ppid``, ``kill_process_tree``) are not offered.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from kiro_crew import env, platform_compat
from kiro_crew.apps import agent_template_values
from kiro_crew.apps import backend_sdk as sdk
from kiro_crew.apps.backend_sdk import process as sdk_process
from kiro_crew.apps.backend_sdk import tools as sdk_tools

_LEVEL_18_NAMES = (
    "address_covers_loopback",
    "agent_template_host_values",
    "nvm_version_bin_dirs",
    "trusted_git_bin",
    "trusted_system_bin",
    "uv_executable",
)


def test_every_name_is_exported_at_seam_level_eighteen() -> None:
    for name in _LEVEL_18_NAMES:
        assert name in sdk.__all__ and name in dir(sdk), name
        assert sdk._EXPORT_LEVELS[name] == 18 <= sdk.SDK_LEVEL, name
        assert callable(getattr(sdk, name)), name
    routed = {name for name, module in sdk._LAZY_EXPORTS.items() if module == "tools"}
    assert routed == set(sdk_tools.__all__)
    assert "address_covers_loopback" in sdk_process.__all__


@pytest.mark.parametrize(
    "dropped",
    [
        "find_port_listeners",
        "get_ppid",
        "kill_process_tree",
        "kill_process_tree_async",
        "loopback_owner_pids",
    ],
)
def test_a_probe_that_answers_about_another_process_is_not_offered(dropped) -> None:
    assert dropped not in sdk.__all__
    with pytest.raises(AttributeError):
        getattr(sdk, dropped)


def test_trusted_git_bin_is_the_gateway_lookup_read_at_call_time(monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_git_bin", lambda: "/usr/bin/git")
    assert sdk.trusted_git_bin() == "/usr/bin/git"
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_git_bin", lambda: None)
    assert sdk.trusted_git_bin() is None
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_git_bin", lambda: "")
    assert sdk.trusted_git_bin() is None


def test_trusted_system_bin_passes_the_name_and_reads_no_path(monkeypatch) -> None:
    asked: list[str] = []

    def lookup(name: str) -> str | None:
        asked.append(name)
        return "/usr/sbin/lsof" if name == "lsof" else None

    monkeypatch.setattr("kiro_crew.platform_compat.trusted_system_bin", lookup)
    assert sdk.trusted_system_bin("lsof") == "/usr/sbin/lsof"
    assert sdk.trusted_system_bin("osascript") is None
    assert asked == ["lsof", "osascript"]


def test_trusted_system_bin_does_not_answer_with_a_tool_planted_on_path(
    monkeypatch, tmp_path: Path
) -> None:
    # An ``.exe`` on Windows, where PATHEXT finds it and any existing file is
    # executable; a script with the executable bit elsewhere.
    name = "kirocrew-sdk-planted-tool"
    planted = tmp_path / (f"{name}.exe" if platform_compat.IS_WINDOWS else name)
    planted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    planted.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    assert shutil.which(name) is not None, "the planted tool is on PATH"
    assert sdk.trusted_system_bin(name) is None


def test_uv_executable_is_the_gateway_resolver(monkeypatch) -> None:
    monkeypatch.setattr(env, "resolve_uv", lambda: "/opt/crew/bin/uv")
    assert sdk.uv_executable() == "/opt/crew/bin/uv"
    monkeypatch.setattr(env, "resolve_uv", lambda: None)
    assert sdk.uv_executable() is None


def test_nvm_version_bin_dirs_is_the_gateway_scan(monkeypatch) -> None:
    dirs = ["/h/.nvm/versions/node/v22.1.0/bin", "/h/.nvm/versions/node/v20.9.0/bin"]
    monkeypatch.setattr(env, "nvm_version_bin_dirs", lambda: list(dirs))
    assert sdk.nvm_version_bin_dirs() == tuple(dirs)


def test_template_host_values_are_the_renderers_without_the_app_placeholders(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(agent_template_values, "resolve_uv", lambda: "/opt/crew/bin/uv")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(agent_template_values.shutil, "which", lambda name: "/usr/bin/soffice")
    values = sdk.agent_template_host_values()
    assert values == {
        agent_template_values.PATH_SEP: os.pathsep,
        agent_template_values.UV_BIN: "/opt/crew/bin/uv",
        agent_template_values.HOST_PATH: "/usr/bin",
    }
    assert agent_template_values.APP_ROOT not in values
    assert agent_template_values.APP_DATA not in values
    rendered = agent_template_values.host_placeholder_values("demo-app", tmp_path)
    host_only = {
        key: value
        for key, value in rendered.items()
        if key not in (agent_template_values.APP_ROOT, agent_template_values.APP_DATA)
    }
    assert host_only == values


def test_template_host_values_leave_out_what_the_host_cannot_compute(monkeypatch) -> None:
    monkeypatch.setattr(agent_template_values, "resolve_uv", lambda: None)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(agent_template_values.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        agent_template_values.soffice_discovery, "windows_install_path", lambda: None
    )
    assert sdk.agent_template_host_values() == {agent_template_values.PATH_SEP: os.pathsep}


@pytest.mark.parametrize(
    ("address", "covers"),
    [
        ("127.0.0.1", True),
        ("::ffff:127.0.0.1", True),
        ("[::ffff:127.0.0.1]", True),
        ("0.0.0.0", True),
        ("*", True),
        ("::", True),
        ("::1", False),
        ("10.0.0.5", False),
        ("", False),
    ],
)
def test_address_covers_loopback_is_the_gateway_predicate(address, covers) -> None:
    assert sdk.address_covers_loopback(address) is covers
    assert covers is bool(platform_compat.address_covers_loopback(address))


def test_address_covers_loopback_delegates_at_call_time(monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.platform_compat.address_covers_loopback", lambda a: a == "x")
    assert sdk.address_covers_loopback("x") is True
    assert sdk.address_covers_loopback("127.0.0.1") is False
