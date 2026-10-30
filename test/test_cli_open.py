"""Tests for ``kirocrew open`` (``cli_open.py``), the CLI face of the desktop
app's ``kirocrew://`` URL scheme.

The slot-key grammar lives twice -- here in Python and in
``website/electron/deep-link.js`` for the Electron shell -- because neither side
can import the other. The pin test at the bottom reads the JavaScript source and
asserts the two regexes are byte-identical, so a change to one without the other
goes red here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from kiro_crew import cli_open
from kiro_crew.cli_open import DeepLinkError, build_deep_link, open_url

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEEP_LINK_JS = _REPO_ROOT / "website" / "electron" / "deep-link.js"


class TestBuildDeepLink:
    def test_bare_scheme_when_no_session(self):
        assert build_deep_link() == "kirocrew://"
        # An empty session is "no target", not an error: it opens the app.
        assert build_deep_link(session="") == "kirocrew://"

    def test_session_is_percent_encoded_like_the_dashboard_link(self):
        assert build_deep_link(session="cos-quick-chat") == "kirocrew://chat?sid=cos-quick-chat"
        assert (
            build_deep_link(session="hook:cos:menubar-chat")
            == "kirocrew://chat?sid=hook%3Acos%3Amenubar-chat"
        )

    @pytest.mark.parametrize(
        "session", ["a b", "-leading", "a" * 201, "x/y", "../x", "cos-quick-chat\n"]
    )
    def test_refuses_what_the_desktop_resolver_refuses(self, session):
        with pytest.raises(DeepLinkError):
            build_deep_link(session=session)


class TestOpenUrl:
    def test_macos_and_linux_use_the_shared_launcher_and_wait_for_its_exit(self, monkeypatch):
        launched = []

        def launch(url, *, wait_for_exit=None):
            launched.append((url, wait_for_exit))
            return True

        monkeypatch.setattr(cli_open, "open_with_default_app", launch)
        for platform in ("darwin", "linux"):
            assert open_url("kirocrew://chat?sid=k", platform=platform) == (
                True,
                "open_with_default_app",
            )
        assert launched == [("kirocrew://chat?sid=k", 2.0), ("kirocrew://chat?sid=k", 2.0)]

    def test_no_registered_handler_is_a_failure(self, monkeypatch):
        # The launcher exiting non-zero ("No application knows how to open URL",
        # xdg-open's exit 3/4) is what the shared helper reports as False here.
        monkeypatch.setattr(
            cli_open, "open_with_default_app", lambda url, *, wait_for_exit=None: False
        )
        assert open_url("kirocrew://", platform="linux") == (
            False,
            "no application is registered for kirocrew://",
        )

    def test_windows_uses_startfile_not_the_shared_launcher(self, monkeypatch):
        opened = []
        monkeypatch.setattr(cli_open.os, "startfile", opened.append, raising=False)

        def refuse(url, **_kwargs):
            raise AssertionError("the shared launcher refuses Windows; it must not be called there")

        monkeypatch.setattr(cli_open, "open_with_default_app", refuse)
        assert open_url("kirocrew://", platform="win32") == (True, "os.startfile")
        assert opened == ["kirocrew://"]

    def test_windows_startfile_error_is_a_failure(self, monkeypatch):
        def boom(url):
            raise OSError("no application is associated")

        monkeypatch.setattr(cli_open.os, "startfile", boom, raising=False)
        assert open_url("kirocrew://", platform="win32") == (False, "no application is associated")


class TestCli:
    def _run(self, monkeypatch, tmp_path, capsys, *argv):
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "argv", ["kirocrew", *argv])
        from kiro_crew.cli import main

        with pytest.raises(SystemExit) as exc:
            main()
        out, err = capsys.readouterr()
        return exc.value.code, out, err

    def test_print_writes_the_url_and_opens_nothing(self, monkeypatch, tmp_path, capsys):
        opened = []
        monkeypatch.setattr(cli_open, "open_url", lambda url: opened.append(url) or (True, "stub"))
        code, out, _ = self._run(
            monkeypatch, tmp_path, capsys, "open", "cos-quick-chat-high", "--print"
        )
        assert code == 0
        assert out.strip() == "kirocrew://chat?sid=cos-quick-chat-high"
        assert opened == []

    def test_no_session_prints_the_bare_scheme(self, monkeypatch, tmp_path, capsys):
        code, out, _ = self._run(monkeypatch, tmp_path, capsys, "open", "--print")
        assert code == 0
        assert out.strip() == "kirocrew://"

    def test_invalid_session_exits_2_with_a_reason(self, monkeypatch, tmp_path, capsys):
        code, out, err = self._run(monkeypatch, tmp_path, capsys, "open", "bad key", "--print")
        assert code == 2
        assert out == ""
        assert "session key" in err

    def test_open_hands_the_url_to_the_os_opener(self, monkeypatch, tmp_path, capsys):
        opened = []
        monkeypatch.setattr(cli_open, "open_url", lambda url: opened.append(url) or (True, "stub"))
        code, _, _ = self._run(monkeypatch, tmp_path, capsys, "open", "cos-quick-chat")
        assert code == 0
        assert opened == ["kirocrew://chat?sid=cos-quick-chat"]

    def test_failed_open_exits_1_and_names_the_fallback(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(cli_open, "open_url", lambda url: (False, "no handler"))
        code, _, err = self._run(monkeypatch, tmp_path, capsys, "open")
        assert code == 1
        assert "no handler" in err
        assert "kirocrew token" in err


def _js_regex(name: str) -> str:
    source = _DEEP_LINK_JS.read_text(encoding="utf-8")
    match = re.search(rf"const {name} = /(.+?)/;", source)
    assert match, f"{name} not found in deep-link.js"
    return match.group(1)


class TestGrammarAgreesWithTheElectronResolver:
    """The Python and JavaScript sides accept exactly the same slot keys."""

    def test_slot_key_regex_is_identical(self):
        assert cli_open._SLOT_KEY_RE.pattern == _js_regex("SLOT_KEY_RE")

    def test_scheme_is_identical(self):
        source = _DEEP_LINK_JS.read_text(encoding="utf-8")
        assert f'const SCHEME = "{cli_open.SCHEME}";' in source
