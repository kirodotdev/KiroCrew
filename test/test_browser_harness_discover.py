"""The browser order (``browser_harness/discover.py``) per platform, on injected facts."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.browser_harness import discover
from kiro_crew.browser_harness.discover import BrowserChoice, HostFacts, choose_browser

_CHROME = Path("/apps/chrome")
_EDGE = Path("/apps/msedge.exe")
_KIRO = Path("/home/x/.kiro/crew/browser-harness/chromium/v/chrome")
_PROFILE = Path("/home/x/.kiro/crew/browser-profile")


def _choose(cfg: dict | None, platform: str, facts: HostFacts) -> BrowserChoice:
    return choose_browser(cfg, platform, facts=facts, kiro_chromium=_KIRO, kiro_profile=_PROFILE)


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_the_users_chrome_comes_first_on_every_desktop(platform: str) -> None:
    facts = HostFacts(chrome=_CHROME, chrome_profile=Path("/p/chrome"), edge=_EDGE)
    choice = _choose(None, platform, facts)
    assert choice.kind == "user_chrome"
    assert choice.executable == _CHROME
    assert choice.headless is False
    assert choice.user_data_dir == Path("/p/chrome")


def test_edge_is_second_on_windows_only() -> None:
    facts = HostFacts(chrome=None, edge=_EDGE, edge_profile=Path("/p/edge"))
    assert _choose(None, "win32", facts).kind == "edge"
    assert _choose(None, "darwin", facts).kind == "kiro_chromium"
    assert _choose(None, "linux", facts).kind == "kiro_chromium"


def test_no_display_means_kiro_chromium_headless_even_with_chrome_installed() -> None:
    facts = HostFacts(chrome=_CHROME, has_display=False)
    choice = _choose(None, "linux", facts)
    assert choice == BrowserChoice("kiro_chromium", _KIRO, True, _PROFILE)


def test_a_policy_that_turns_debugging_off_skips_the_users_chrome() -> None:
    facts = HostFacts(chrome=_CHROME, policy_off=True)
    assert _choose(None, "darwin", facts).kind == "kiro_chromium"


def test_a_snap_chromium_is_skipped() -> None:
    facts = HostFacts(chrome=Path("/snap/bin/chromium"), chrome_is_snap=True)
    assert _choose(None, "linux", facts).kind == "kiro_chromium"


def test_the_explicit_choices_win_over_the_order() -> None:
    facts = HostFacts(chrome=_CHROME, edge=_EDGE, has_display=True)
    assert _choose({"choice": "headless"}, "darwin", facts).headless is True
    kiro = _choose({"choice": "kiro_chromium"}, "darwin", facts)
    assert kiro.kind == "kiro_chromium" and kiro.headless is False
    assert _choose({"choice": "user_chrome"}, "darwin", facts).kind == "user_chrome"
    # Asked for Chrome on a host without one: the fallback, not an error.
    assert _choose({"choice": "user_chrome"}, "darwin", HostFacts()).kind == "kiro_chromium"


def test_an_unknown_choice_reads_as_auto() -> None:
    facts = HostFacts(chrome=_CHROME)
    assert _choose({"choice": "firefox"}, "darwin", facts).kind == "user_chrome"


def test_probe_host_never_raises_on_an_empty_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(discover, "_SYSTEM_ROOT", tmp_path / "root")
    monkeypatch.setenv("PATH", str(tmp_path))
    facts = discover.probe_host("linux", home=tmp_path, env={"PATH": str(tmp_path)})
    assert facts.chrome is None
    assert facts.has_display is False


def test_probe_host_finds_chrome_and_its_profile_on_macos(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(discover, "_SYSTEM_ROOT", tmp_path / "root")
    app = tmp_path / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    app.parent.mkdir(parents=True)
    app.write_text("", encoding="utf-8")
    profile = tmp_path / "Library/Application Support/Google/Chrome"
    profile.mkdir(parents=True)
    facts = discover.probe_host("darwin", home=tmp_path, env={})
    assert facts.chrome == app
    assert facts.chrome_profile == profile
    assert facts.has_display is True


def test_probe_host_reads_the_windows_install_roots(tmp_path: Path) -> None:
    exe = tmp_path / "pf/Google/Chrome/Application/chrome.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    env = {"PROGRAMFILES": str(tmp_path / "pf"), "LOCALAPPDATA": str(tmp_path / "local")}
    (tmp_path / "local/Google/Chrome/User Data").mkdir(parents=True)
    facts = discover.probe_host("win32", home=tmp_path, env=env)
    assert facts.chrome == exe
    assert facts.chrome_profile == tmp_path / "local/Google/Chrome/User Data"


def test_the_choice_values_match_the_settings_contract() -> None:
    assert discover.CHOICES == ("auto", "user_chrome", "kiro_chromium", "headless")
