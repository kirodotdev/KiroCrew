"""`kirocrew app enable` never gives session-approval consent; the dashboard does.

An app whose manifest declares ``permissions.sessionApproval`` is installed with
``sessionApprovalConsentPending``, and ``enable_app`` refuses it without consent
(``session_approval_consent_required``). The CLI has no
consent flag and no prompt, because an agent with a shell could use either. It
prints what the app asks for and the dashboard link, exits 1, and leaves the app
off, on both the file-only path and the running-gateway path. It never sends
``sessionApprovalConsent``.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.error

import pytest

from kiro_crew import app_lifecycle_client
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, _read_installed, install_app
from kiro_crew.cli_commands import _handle_app
from kiro_crew.cli_parser_agent import register_app_parser

APP = "chat-keypad"
LINK_TAIL = f":4321/apps/detail/{APP}"
_REAL_TOGGLE = app_lifecycle_client.toggle_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    register_app_parser(parser.add_subparsers(dest="command"))
    return parser


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIROCREW_PORT", "4321")
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    import kiro_crew.apps.bridges as bridges_mod

    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)
    # File-only path unless a test fakes a gateway.
    monkeypatch.setattr(app_lifecycle_client, "toggle_app", lambda *a, **k: None)
    return home


def _install(tmp_path, *, session_approval: bool) -> None:
    src = tmp_path / "source" / APP
    src.mkdir(parents=True)
    manifest = {
        "name": APP,
        "version": "1.0.0",
        "displayName": "Chat Keypad",
        "description": "test",
        "author": "tester",
        "permissions": {"sessionApproval": session_approval, "storage": True},
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    assert install_app(src).ok
    meta = _read_installed(APP)
    assert meta is not None and meta.sessionApprovalConsentPending is session_approval


def _fake_terminal_answering_yes(monkeypatch) -> list[str]:
    """A terminal on both ends whose every prompt is answered ``y``."""
    prompts: list[str] = []
    monkeypatch.setattr(sys, "stdin", _Tty("y\n" * 5))
    monkeypatch.setattr(sys, "stdout", _Tty())

    def answer_yes(prompt: str = "") -> str:
        prompts.append(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", answer_yes)
    return prompts


def _enable(*extra: str) -> None:
    _handle_app(build_parser().parse_args(["app", "enable", APP, *extra]))


def _state() -> tuple[bool, bool]:
    meta = _read_installed(APP)
    assert meta is not None
    return meta.enabled, meta.sessionApprovalConsentPending


def _assert_refusal(err: str) -> None:
    assert f"{APP} asks to control your chats. Nothing was enabled." in err
    assert "approve or deny tool prompts" in err
    assert "Storage:" in err
    assert LINK_TAIL in err
    assert "--allow-chat-control" not in err


def test_file_only_enable_of_pending_app_stays_off_and_prints_link(
    tmp_path, app_env, monkeypatch, capsys
):
    _install(tmp_path, session_approval=True)
    with pytest.raises(SystemExit) as exc:
        _enable()
    assert exc.value.code == 1
    _assert_refusal(capsys.readouterr().err)
    assert _state() == (False, True)


def test_terminal_answering_yes_still_enables_nothing(tmp_path, app_env, monkeypatch, capsys):
    _install(tmp_path, session_approval=True)
    prompts = _fake_terminal_answering_yes(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _enable()
    assert exc.value.code == 1
    assert prompts == []
    _assert_refusal(capsys.readouterr().err)
    assert _state() == (False, True)


def test_cli_has_no_consent_flag(app_env, capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["app", "enable", APP, "--allow-chat-control"])
    assert "unrecognized arguments" in capsys.readouterr().err


def test_not_pending_app_enables_unchanged(tmp_path, app_env, monkeypatch, capsys):
    _install(tmp_path, session_approval=False)
    _enable()
    assert "Recorded chat-keypad as enabled" in capsys.readouterr().out
    assert _state() == (True, False)


def _gateway_refusing_consent(sent: list[object]):
    """A gateway that answers ``enable`` like routes_enable does for a pending app."""

    def fake_post(route, action, subject, *, payload):
        sent.append(payload)
        body = json.dumps(
            {
                "ok": False,
                "name": APP,
                "error": "session approval consent must be confirmed from a disclosure surface",
                "code": "session_approval_consent_required",
            }
        ).encode()
        raise app_lifecycle_client._gateway_error(
            urllib.error.HTTPError(route, 400, "Bad Request", {}, io.BytesIO(body))  # type: ignore[arg-type]
        )

    return fake_post


def test_gateway_enable_of_pending_app_prints_link_and_sends_no_consent(
    tmp_path, app_env, monkeypatch, capsys
):
    _install(tmp_path, session_approval=True)
    sent: list[object] = []
    monkeypatch.setattr(app_lifecycle_client, "toggle_app", _REAL_TOGGLE)
    monkeypatch.setattr(
        app_lifecycle_client, "_post_lifecycle_action", _gateway_refusing_consent(sent)
    )
    prompts = _fake_terminal_answering_yes(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _enable()
    assert exc.value.code == 1
    # One request, with no body at all: the CLI never sends sessionApprovalConsent.
    assert sent == [None]
    assert prompts == []
    err = capsys.readouterr().err
    _assert_refusal(err)
    assert "gateway refused" not in err
    assert _state() == (False, True)


def test_gateway_error_keeps_the_refusal_code():
    body = json.dumps({"error": "nope", "code": "session_approval_consent_required"}).encode()
    exc = app_lifecycle_client._gateway_error(
        urllib.error.HTTPError("/x", 400, "Bad Request", {}, io.BytesIO(body))  # type: ignore[arg-type]
    )
    assert exc.code == "session_approval_consent_required"
    assert str(exc) == "nope"
