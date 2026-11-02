"""A codex-acp outside the major Crew tested it on gets one warning row; it still runs.

The tested major is 2: the Real Adapter Contract lane pins codex-acp 2.3.0 in
``test/real_adapters/package.json``, and every live codex capture reports it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp.client_turn_control import (
    CODEX_ACP_TESTED_RANGE,
    CODEX_ACP_TESTED_VERSION,
    untested_version_issue,
)
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.acp import AcpProvider

CODEX = sdk_backends.ACP_BACKEND_CODEX
PACKAGE = sdk_backends.ACP_BACKEND_NODE_ADAPTER_PACKAGES[CODEX]
REPO = Path(__file__).resolve().parent
LANE_PACKAGE_JSON = REPO / "real_adapters" / "package.json"
LIVE_CAPTURE = REPO / "fixtures" / "acp_frames" / "codex" / "session-live.jsonl"


@pytest.mark.parametrize("version", ["1.11.0", "1.99.9", "3.0.0", "3.1.2", "0.0.0"])
def test_outside_the_tested_major_warns_with_version_range_and_fix(version):
    issue = untested_version_issue(CODEX, version)
    assert f"codex {version} is outside the tested range 2.x" in issue
    assert f"npm i -g {PACKAGE}@2" in issue
    assert "start a new chat" in issue


@pytest.mark.parametrize("version", ["2.0.0", "2.3.0", "2.99.0", "2.3.0+build.1"])
def test_the_tested_major_is_silent(version):
    assert untested_version_issue(CODEX, version) == ""


@pytest.mark.parametrize("version", ["", "unknown", "2.3", "v2.3.0", None])
def test_an_unreadable_version_is_silent_and_does_not_raise(version):
    assert untested_version_issue(CODEX, version) == ""


def test_the_tested_release_is_the_lane_pin_and_the_live_capture():
    pins = json.loads(LANE_PACKAGE_JSON.read_text(encoding="utf-8"))["dependencies"]
    assert pins[PACKAGE] == CODEX_ACP_TESTED_VERSION
    versions = {
        info["version"]
        for line in LIVE_CAPTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for info in [(json.loads(line).get("result") or {}).get("agentInfo")]
        if isinstance(info, dict) and info.get("version")
    }
    assert versions == {CODEX_ACP_TESTED_VERSION}
    low, high = CODEX_ACP_TESTED_RANGE
    assert low == (int(CODEX_ACP_TESTED_VERSION.split(".")[0]), 0, 0)
    assert high == (low[0] + 1, 0, 0)


def test_the_provider_reads_the_session_handshake():
    inner = SimpleNamespace(backend=CODEX, agent_version="3.0.0")
    notice = AcpProvider.tested_version_notice.fget(
        SimpleNamespace(_client=inner, agent_version="3.0.0")
    )
    assert "3.0.0" in notice and "2.x" in notice


def test_a_new_session_outside_the_major_gets_one_notice_row(monkeypatch):
    rows = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    notice = untested_version_issue(CODEX, "1.11.0")
    chat_runner._notice_steer_upgrade(
        object(), object(), SimpleNamespace(tested_version_notice=notice)
    )
    assert len(rows) == 1
    assert rows[0][2] == "notice"
    assert "1.11.0" in rows[0][3] and "2.x" in rows[0][3]
