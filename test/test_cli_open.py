"""Tests for ``kirocrew open`` (``cli_open.py``), the CLI face of the desktop
app's ``kirocrew://`` URL scheme.

The slot-key grammar lives twice -- here in Python and in
``website/electron/deep-link.js`` for the Electron shell -- because neither side
can import the other. The pin test at the bottom reads the JavaScript source and
asserts the two regexes are byte-identical, so a change to one without the other
goes red here. The same class pins the resolver's other copies of a Python rule:
the app-name grammar, and the SSH host and crew-name rules its
``kirocrew://remote-crew/add`` shape checks.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from kiro_crew import cli_open
from kiro_crew.apps import manifest_names
from kiro_crew.cli_open import DeepLinkError, build_deep_link, open_url
from kiro_crew.instances import registry as instances_registry
from kiro_crew.instances import validation as instances_validation
from kiro_crew.instances.registry import _ID_RE

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEEP_LINK_JS = _REPO_ROOT / "website" / "electron" / "deep-link.js"
_APP_NAME_SAMPLES = (
    _REPO_ROOT / "website" / "electron" / "test" / "fixtures" / "app-name-samples.json"
)
_SSH_HOST_SAMPLES = (
    _REPO_ROOT / "website" / "electron" / "test" / "fixtures" / "ssh-host-samples.json"
)


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


def _js_int(name: str) -> int:
    source = _DEEP_LINK_JS.read_text(encoding="utf-8")
    match = re.search(rf"const {name} = (\d+);", source)
    assert match, f"{name} not found in deep-link.js"
    return int(match.group(1))


class TestGrammarAgreesWithTheElectronResolver:
    """The Python and JavaScript sides accept exactly the same values."""

    def test_slot_key_regex_is_identical(self):
        assert cli_open._SLOT_KEY_RE.pattern == _js_regex("SLOT_KEY_RE")

    def test_app_name_samples_agree_with_the_manifest_grammar(self):
        # deep-link.js checks ``kirocrew://apps/detail/<name>`` with plain logic
        # rather than a copy of ``KEBAB_RE``; deep-link.test.js runs the same
        # samples through it, so the two grammars cannot drift apart.
        samples = json.loads(_APP_NAME_SAMPLES.read_text(encoding="utf-8"))
        assert samples["valid"] and samples["invalid"]
        for name in samples["valid"]:
            assert manifest_names.KEBAB_RE.fullmatch(name), name
        for name in samples["invalid"]:
            assert not manifest_names.KEBAB_RE.fullmatch(name), name

    def test_app_name_length_cap_is_the_manifest_cap(self):
        source = _DEEP_LINK_JS.read_text(encoding="utf-8")
        match = re.search(r"const APP_NAME_MAX_LENGTH = (\d+);", source)
        assert match, "APP_NAME_MAX_LENGTH not found in deep-link.js"
        assert int(match.group(1)) == manifest_names.APP_NAME_MAX_LENGTH

    def test_scheme_is_identical(self):
        source = _DEEP_LINK_JS.read_text(encoding="utf-8")
        assert f'const SCHEME = "{cli_open.SCHEME}";' in source

    def test_ssh_host_samples_agree_with_validate_ssh_host(self):
        # deep-link.js checks a remote-crew link's host with its own copy of
        # this rule (``isSshHost``); deep-link.test.js runs the same samples
        # through it, so the copy cannot admit a host the gateway refuses.
        samples = json.loads(_SSH_HOST_SAMPLES.read_text(encoding="utf-8"))
        assert samples["valid"] and samples["invalid"]
        for host in samples["valid"]:
            assert instances_validation.validate_ssh_host(host) == host, host
        for host in samples["invalid"]:
            with pytest.raises(instances_validation.SshValidationError):
                instances_validation.validate_ssh_host(host)

    def test_ssh_host_length_cap_is_the_gateway_cap(self):
        assert _js_int("SSH_HOST_MAX_LENGTH") == instances_validation._MAX_HOST_LEN

    def test_crew_name_length_cap_is_the_registry_cap(self):
        assert _js_int("CREW_NAME_MAX_LENGTH") == instances_registry.INSTANCE_NAME_MAX

    def test_crew_id_regex_is_the_registry_id_rule(self):
        # A crew link names a crew by its registry id; a drifted copy would turn a
        # valid crew into an "unknown crew" error. JS `$` stands for Python `\Z`.
        assert _js_regex("CREW_ID_RE") == _ID_RE.pattern.replace("\\Z", "$")
