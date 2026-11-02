"""A goose outside the release Crew tested it on gets one warning row; it still runs.

The tested range is 1.50.x: the release the goose capture README names for every
capture but the effort one, and the VERIFIED RANGE ``agent_sdk/backends.py`` declares.

"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp.client_permissions import GOOSE_VERIFIED_VERSION_PREFIX
from kiro_crew.acp.client_turn_control import (
    GOOSE_TESTED_RANGE,
    GOOSE_TESTED_VERSION,
    untested_version_issue,
)
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.acp import AcpProvider

GOOSE = sdk_backends.ACP_BACKEND_GOOSE
HANDSHAKE = Path(__file__).resolve().parent / "fixtures/acp_frames/goose/handshake-live.jsonl"


@pytest.mark.parametrize("version", ["1.49.9", "1.38.0", "1.51.0", "1.54.0", "2.0.0"])
def test_outside_the_tested_range_warns_with_version_range_and_fix(version):
    issue = untested_version_issue(GOOSE, version)
    assert f"goose {version} is outside the tested range 1.50.x" in issue
    assert f"GOOSE_VERSION={GOOSE_TESTED_VERSION} bash" in issue
    assert "download_cli.sh" in issue


@pytest.mark.parametrize("version", ["1.50.0", "1.50.1", "1.50.9", "1.50.1+build.3"])
def test_the_tested_range_is_silent(version):
    assert untested_version_issue(GOOSE, version) == ""


@pytest.mark.parametrize("version", ["", "unknown", "dev", "1.50", "v1.54.0", None])
def test_an_unreadable_version_is_silent_and_does_not_raise(version):
    assert untested_version_issue(GOOSE, version) == ""


@pytest.mark.parametrize(
    "backend",
    sorted(set(sdk_backends.ACP_BACKENDS_KNOWN) - {GOOSE, sdk_backends.ACP_BACKEND_CODEX}),
)
def test_other_harnesses_get_no_goose_notice(backend):
    assert untested_version_issue(backend, "0.0.1") == ""


def test_the_range_matches_the_capture_and_the_logged_range():
    """The tested release is the one the handshake capture reports, inside the range
    the handshake log line already names."""
    versions = [
        info["version"]
        for line in HANDSHAKE.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for info in [(json.loads(line).get("result") or {}).get("agentInfo")]
        if isinstance(info, dict) and info.get("version")
    ]
    assert versions == [GOOSE_TESTED_VERSION]
    assert untested_version_issue(GOOSE, GOOSE_TESTED_VERSION) == ""
    low, _high = GOOSE_TESTED_RANGE
    assert GOOSE_VERIFIED_VERSION_PREFIX == f"{low[0]}.{low[1]}."


def _provider_notice(backend, version):
    inner = SimpleNamespace(backend=backend, agent_version=version)
    return AcpProvider.tested_version_notice.fget(
        SimpleNamespace(_client=inner, agent_version=version)
    )


def test_the_provider_reads_the_session_handshake():
    assert "1.51.0" in _provider_notice(GOOSE, "1.51.0")
    assert _provider_notice(GOOSE, "1.50.1") == ""
    assert _provider_notice(GOOSE, "") == ""


def test_a_new_session_outside_the_range_gets_one_notice_row(monkeypatch):
    rows = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    notice = untested_version_issue(GOOSE, "1.54.0")
    chat_runner._notice_steer_upgrade(
        object(), object(), SimpleNamespace(tested_version_notice=notice)
    )
    assert len(rows) == 1
    assert rows[0][2] == "notice"
    assert "1.54.0" in rows[0][3] and "1.50.x" in rows[0][3]
