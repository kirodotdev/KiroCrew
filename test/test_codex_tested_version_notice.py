"""codex-acp outside the tested major gets a notice, never a refusal.

The Real Adapter lane measures one codex-acp release. A new chat on another major
still runs, and gets one notice row naming the installed version, the tested line
and the command that installs it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp.client_turn_control import CODEX_ACP_TESTED_MAJOR, untested_version_issue
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.acp import AcpProvider

CODEX = sdk_backends.ACP_BACKEND_CODEX
PINS = Path(__file__).resolve().parent / "real_adapters" / "package.json"
FIX = f"npm i -g @agentclientprotocol/codex-acp@{CODEX_ACP_TESTED_MAJOR}"


@pytest.mark.parametrize("version", ["1.11.0", "3.0.0", "10.2.1"])
def test_outside_the_tested_line_warns(version):
    issue = untested_version_issue(CODEX, version)
    assert version in issue
    assert f"tested range {CODEX_ACP_TESTED_MAJOR}.x," in issue
    assert FIX in issue


@pytest.mark.parametrize("version", ["2.0.0", "2.3.0", "2.3.0-rc.1", "2.99.4+build.7"])
def test_inside_the_tested_line_is_silent(version):
    assert untested_version_issue(CODEX, version) == ""


@pytest.mark.parametrize("version", ["", "unknown", "v", "2", None])
def test_an_unreadable_version_is_silent_and_does_not_raise(version):
    assert untested_version_issue(CODEX, version) == ""


def test_the_tested_major_is_the_lane_pin():
    pin = json.loads(PINS.read_text(encoding="utf-8"))["dependencies"]
    assert pin["@agentclientprotocol/codex-acp"].split(".")[0] == str(CODEX_ACP_TESTED_MAJOR)


def test_goose_keeps_its_minor_line_label():
    assert "tested range 1.50.x," in untested_version_issue(
        sdk_backends.ACP_BACKEND_GOOSE, "1.49.0"
    )


def test_the_provider_reads_the_codex_handshake():
    def notice(version):
        inner = SimpleNamespace(backend=CODEX, agent_version=version)
        return AcpProvider.tested_version_notice.fget(
            SimpleNamespace(_client=inner, agent_version=version)
        )

    assert "3.0.0" in notice("3.0.0")
    assert notice("2.3.0") == ""
    assert notice("") == ""


def test_a_new_codex_session_outside_the_line_gets_one_notice_row(monkeypatch):
    rows = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    client = SimpleNamespace(tested_version_notice=untested_version_issue(CODEX, "3.1.0"))
    chat_runner._notice_steer_upgrade(object(), object(), client)
    assert len(rows) == 1
    assert rows[0][2] == "notice"
    assert "3.1.0" in rows[0][3] and FIX in rows[0][3]
