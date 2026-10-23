"""claude and goose below their first steering release get an upgrade notice.

The card's ``mid_turn_steer`` line reads membership, so it says yes for every claude
and goose. That is only true from the release whose steer Crew speaks: below it, the
session still runs and a mid-turn message waits, so a new chat gets one notice row
naming the installed version, the minimum and the upgrade command.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_claude_session import (
    CLAUDE_ACP_STEERING_MIN_VERSION,
    _adapter_version_at_least,
)
from kiro_crew.acp.client_turn_control import GOOSE_STEER_MIN_VERSION, steer_version_issue
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk.backend_cards import card_payload
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.acp import AcpProvider

CLAUDE = sdk_backends.ACP_BACKEND_CLAUDE
GOOSE = sdk_backends.ACP_BACKEND_GOOSE
FRAMES = Path(__file__).resolve().parent / "fixtures" / "acp_frames"


@pytest.mark.parametrize(
    ("backend", "version", "minimum", "fix"),
    [
        (CLAUDE, "0.64.9", "0.65.0", "npm i -g @agentclientprotocol/claude-agent-acp"),
        (CLAUDE, "0.61.0", "0.65.0", "npm i -g @agentclientprotocol/claude-agent-acp"),
        (GOOSE, "1.37.0", "1.38.0", "download_cli.sh | bash"),
    ],
)
def test_below_the_floor_names_installed_minimum_and_fix(backend, version, minimum, fix):
    issue = steer_version_issue(backend, version)
    assert version in issue
    assert f"older than {minimum}" in issue
    assert fix in issue


@pytest.mark.parametrize(
    ("backend", "version"),
    [
        (CLAUDE, "0.65.0"),
        (CLAUDE, "0.93.0"),
        (CLAUDE, "0.65.0+build.7"),
        (GOOSE, "1.38.0"),
        (GOOSE, "1.54.0"),
    ],
)
def test_at_or_above_the_floor_is_not_refused(backend, version):
    assert steer_version_issue(backend, version) == ""


@pytest.mark.parametrize("version", ["", "unknown", "dev"])
def test_an_unknown_version_is_not_refused(version):
    """As for pi: only a version known to be old is refused."""
    assert steer_version_issue(CLAUDE, version) == ""
    assert steer_version_issue(GOOSE, version) == ""


@pytest.mark.parametrize("backend", sorted(set(sdk_backends.ACP_BACKENDS_KNOWN) - {CLAUDE, GOOSE}))
def test_other_harnesses_have_no_steer_floor(backend):
    assert steer_version_issue(backend, "0.0.1") == ""


@pytest.mark.parametrize("version", ["0.64.9", "0.65.0", "0.65.0-rc.1", "0.65.0+build.7", "0.93.0"])
def test_the_notice_agrees_with_the_claude_steer_gate(version):
    """A notice says the message waits exactly when the steer gate says it cannot steer."""
    steers = _adapter_version_at_least(version, CLAUDE_ACP_STEERING_MIN_VERSION)
    assert bool(steer_version_issue(CLAUDE, version)) is not steers


def test_no_committed_handshake_is_refused():
    """Every live claude and goose capture is at or above its floor."""
    seen = 0
    for backend in (CLAUDE, GOOSE):
        for path in sorted((FRAMES / backend).glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                frame = json.loads(line) if line.strip() else {}
                info = (
                    (frame.get("result") or {}).get("agentInfo")
                    if isinstance(frame, dict)
                    else None
                )
                if isinstance(info, dict) and info.get("version"):
                    seen += 1
                    assert steer_version_issue(backend, info["version"]) == "", path
    assert seen


def test_goose_floor_is_one_release_above_the_last_without_steer():
    assert GOOSE_STEER_MIN_VERSION == (1, 38, 0)


@pytest.mark.parametrize("backend", [CLAUDE, GOOSE])
def test_the_card_is_unchanged(backend):
    lines = {line["id"]: line for line in card_payload(backend)["capabilities"]}
    assert lines["mid_turn_steer"]["available"] is True


def _handshake(tmp_path, backend, version):
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)

    async def _send(method, params=None):
        return 1

    async def _wait(req_id, timeout=None):
        return {"protocolVersion": 1, "agentInfo": {"version": version}}

    client._send_request = _send  # type: ignore[method-assign]
    client._wait_for_response = _wait  # type: ignore[method-assign]
    return asyncio.run(client._initialize_session())


@pytest.mark.parametrize(("backend", "version"), [(CLAUDE, "0.64.0"), (GOOSE, "1.37.0")])
def test_the_handshake_does_not_refuse_below_the_floor(tmp_path, backend, version):
    """The session keeps running: the handshake goes on past the version read."""
    try:
        _handshake(tmp_path, backend, version)
    except Exception as exc:  # noqa: BLE001 -- the stub cannot finish a real session
        assert "then start a new chat" not in str(exc)


def _provider_notice(backend, version):
    inner = SimpleNamespace(backend=backend, agent_version=version)
    return AcpProvider.steer_upgrade_notice.fget(
        SimpleNamespace(_client=inner, agent_version=version)
    )


def test_the_provider_reads_the_session_handshake():
    assert "0.65.0" in _provider_notice(CLAUDE, "0.64.0")
    assert _provider_notice(CLAUDE, "0.65.0") == ""
    assert _provider_notice(GOOSE, "") == ""


def _surface(monkeypatch, notice):
    rows = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    chat_runner._notice_steer_upgrade(
        object(), object(), SimpleNamespace(steer_upgrade_notice=notice)
    )
    return rows


def test_a_new_session_below_the_floor_gets_one_notice_row(monkeypatch):
    notice = steer_version_issue(GOOSE, "1.37.0")
    rows = _surface(monkeypatch, notice)
    assert len(rows) == 1
    assert rows[0][2] == "notice"
    assert "1.38.0" in rows[0][3] and "download_cli.sh" in rows[0][3]


@pytest.mark.parametrize("notice", ["", None, MagicMock()])
def test_no_notice_at_or_above_the_floor(monkeypatch, notice):
    assert _surface(monkeypatch, notice) == []
