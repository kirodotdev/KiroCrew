"""A claude-agent-acp or pi-acp outside the release Crew tested gets one warning row.

The chat still runs. The tested releases are the Real Adapter Contract lane's pins in
``test/real_adapters/package.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp.client_turn_control import (
    CLAUDE_ACP_TESTED_RANGE,
    CLAUDE_ACP_TESTED_VERSION,
    PI_ACP_TESTED_RANGE,
    PI_ACP_TESTED_VERSION,
    untested_version_issue,
)
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.acp import AcpProvider

CLAUDE = sdk_backends.ACP_BACKEND_CLAUDE
PI = sdk_backends.ACP_BACKEND_PI
PACKAGES = sdk_backends.ACP_BACKEND_NODE_ADAPTER_PACKAGES
REPO = Path(__file__).resolve().parent
LANE_PACKAGE_JSON = REPO / "real_adapters" / "package.json"

#: backend -> (tested version, range label, versions outside, versions inside)
CASES = {
    CLAUDE: (
        CLAUDE_ACP_TESTED_VERSION,
        "0.94.x",
        ["0.93.9", "0.84.0", "0.95.0", "1.0.0"],
        ["0.94.0", "0.94.7"],
    ),
    PI: (
        PI_ACP_TESTED_VERSION,
        "0.0.34",
        ["0.0.33", "0.0.35", "0.1.0", "1.4.0"],
        ["0.0.34", "0.0.34+build.1"],
    ),
}


@pytest.mark.parametrize(
    ("backend", "version"),
    [(b, v) for b, (_, _, outside, _) in CASES.items() for v in outside],
)
def test_outside_the_tested_release_warns_with_version_range_and_fix(backend, version):
    tested, label, _, _ = CASES[backend]
    issue = untested_version_issue(backend, version)
    assert f"{backend} {version} is outside the tested range {label}" in issue
    assert f"npm i -g {PACKAGES[backend]}@{tested}" in issue
    assert "start a new chat" in issue


@pytest.mark.parametrize(
    ("backend", "version"),
    [(b, v) for b, (_, _, _, inside) in CASES.items() for v in inside]
    + [(b, v) for b in CASES for v in ["", "unknown", "0.94", "v0.94.0", None]],
)
def test_a_tested_or_unreadable_version_is_silent_and_does_not_raise(backend, version):
    assert untested_version_issue(backend, version) == ""


@pytest.mark.parametrize(
    ("backend", "tested", "tested_range"),
    [
        (CLAUDE, CLAUDE_ACP_TESTED_VERSION, CLAUDE_ACP_TESTED_RANGE),
        (PI, PI_ACP_TESTED_VERSION, PI_ACP_TESTED_RANGE),
    ],
)
def test_the_tested_release_is_the_lane_pin(backend, tested, tested_range):
    pins = json.loads(LANE_PACKAGE_JSON.read_text(encoding="utf-8"))["dependencies"]
    assert pins[PACKAGES[backend]] == tested, "move the tested release with the lane pin"
    low, high = tested_range
    assert low == tuple(int(part) for part in tested.split("."))
    assert untested_version_issue(backend, tested) == ""
    assert untested_version_issue(backend, ".".join(map(str, high)))


@pytest.mark.parametrize("backend", sorted(CASES))
def test_a_new_session_outside_the_range_gets_one_notice_row(backend, monkeypatch):
    rows = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    inner = SimpleNamespace(backend=backend, agent_version="9.9.9")
    provider = SimpleNamespace(_client=inner, agent_version="9.9.9")
    notice = AcpProvider.tested_version_notice.fget(provider)
    chat_runner._notice_steer_upgrade(
        object(), object(), SimpleNamespace(tested_version_notice=notice)
    )
    assert len(rows) == 1
    assert rows[0][2] == "notice"
    assert "9.9.9" in rows[0][3] and CASES[backend][1] in rows[0][3]
