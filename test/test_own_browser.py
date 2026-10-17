"""Which browser the agent is told to drive: the user's own Chrome first, headless last.

Two halves. The probes and the ladder in ``browser_cli.own_browser`` decide the
instruction from host facts, each pinned on a temporary tree so the test host's
own Chrome never leaks in. The guidance pin holds every shipped agent-facing
attach command to :data:`own_browser.ATTACH_COMMAND`'s override: without
``PLAYWRIGHT_MCP_ISOLATED=false`` an operator config with ``browser.isolated``
turns the attach into a throwaway browser that still prints "attached".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.browser_cli import own_browser
from kiro_crew.browser_cli.token import TOKEN_ENV

SRC = Path(own_browser.__file__).resolve().parents[1]


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    system_root = tmp_path / "root"
    system_root.mkdir()
    monkeypatch.setattr(own_browser, "_ROOT", system_root)
    monkeypatch.setattr(own_browser.shutil, "which", lambda _name: None)
    return system_root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def _mac_chrome(root: Path) -> None:
    (root / "Applications" / "Google Chrome.app").mkdir(parents=True)


def _mac_extension(home: Path, profile: str = "Default") -> None:
    data = home / "Library" / "Application Support" / "Google" / "Chrome"
    (data / profile / "Extensions" / own_browser.EXTENSION_ID).mkdir(parents=True)


def test_no_chrome_points_at_headless(root: Path, home: Path) -> None:
    step = own_browser.next_step(home, {}, "darwin")
    assert "No Google Chrome" in step
    assert "playwright-cli open <url>" in step
    assert "attach" not in step


def test_chrome_without_extension_asks_the_user_to_add_it(root: Path, home: Path) -> None:
    _mac_chrome(root)
    step = own_browser.next_step(home, {}, "darwin")
    assert own_browser.EXTENSION_URL in step
    assert own_browser.ATTACH_COMMAND in step


def test_extension_in_a_non_default_profile_counts(root: Path, home: Path) -> None:
    _mac_chrome(root)
    _mac_extension(home, "Profile 3")
    assert own_browser.extension_installed(home, {}, "darwin")


def test_extension_without_token_attaches_and_names_settings(root: Path, home: Path) -> None:
    _mac_chrome(root)
    _mac_extension(home)
    step = own_browser.next_step(home, {}, "darwin")
    assert step.index(own_browser.ATTACH_COMMAND) < step.index("headless")
    assert "Settings -> Browser" in step
    assert "approve" in step


def test_extension_with_token_attaches_without_asking(root: Path, home: Path) -> None:
    _mac_chrome(root)
    _mac_extension(home)
    step = own_browser.next_step(home, {TOKEN_ENV: "abc"}, "darwin")
    assert own_browser.ATTACH_COMMAND in step
    assert "Settings -> Browser" not in step


def test_user_chrome_install_counts_on_macos(root: Path, home: Path) -> None:
    (home / "Applications" / "Google Chrome.app").mkdir(parents=True)
    assert own_browser.chrome_installed(home, {}, "darwin")


def test_windows_reads_the_install_and_profile_roots_from_env(root: Path, tmp_path: Path) -> None:
    local = tmp_path / "local"
    (local / "Google" / "Chrome" / "Application").mkdir(parents=True)
    (local / "Google" / "Chrome" / "Application" / "chrome.exe").write_text("")
    profile = local / "Google" / "Chrome" / "User Data" / "Default"
    (profile / "Extensions" / own_browser.EXTENSION_ID).mkdir(parents=True)
    env = {"LOCALAPPDATA": str(local)}
    assert own_browser.chrome_installed(tmp_path, env, "win32")
    assert own_browser.extension_installed(tmp_path, env, "win32")
    assert not own_browser.extension_installed(tmp_path, {}, "win32")
    step = own_browser.next_step(tmp_path, env, "win32")
    assert own_browser.WINDOWS_ATTACH_COMMAND in step
    assert own_browser.ATTACH_COMMAND not in step


def test_attach_step_names_the_remote_gateway_exception(root: Path, home: Path) -> None:
    _mac_chrome(root)
    _mac_extension(home)
    step = own_browser.next_step(home, {TOKEN_ENV: "abc"}, "darwin")
    assert "another machine" in step
    assert "about:blank" in step


def test_linux_finds_chrome_on_path(
    root: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not own_browser.chrome_installed(home, {}, "linux")
    monkeypatch.setattr(
        own_browser.shutil,
        "which",
        lambda name: "/usr/bin/google-chrome" if name == "google-chrome" else None,
    )
    assert own_browser.chrome_installed(home, {}, "linux")


def test_unknown_os_has_no_chrome(root: Path, home: Path) -> None:
    assert not own_browser.chrome_installed(home, {}, "sunos5")
    assert not own_browser.extension_installed(home, {}, "sunos5")


def _guidance_files() -> list[Path]:
    return [SRC / "config" / "prompt.md", *sorted((SRC / "builtin_skills").rglob("SKILL.md"))]


def test_every_shipped_attach_command_carries_the_isolated_override() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in _guidance_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "playwright-cli attach --extension" in line and "PLAYWRIGHT_MCP_ISOLATED" not in line
    ]
    assert not offenders, offenders


def test_the_guidance_pin_sees_the_attach_commands() -> None:
    hits = sum(
        path.read_text(encoding="utf-8").count(own_browser.ATTACH_COMMAND)
        for path in _guidance_files()
    )
    assert hits >= 3
