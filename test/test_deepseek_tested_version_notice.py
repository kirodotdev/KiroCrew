"""A dsh outside the release Crew tested gets one warning row; the chat still runs.

dsh-acp's handshake reports ``agentInfo.version`` 0.0.1 on every release, so the
version is read from dsh's own npm manifest at spawn, never from the handshake. The
tested release is the Real Adapter Contract lane's pin in
``test/real_adapters/package.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from acp_deepseek_backend_helpers import (  # noqa: F401 -- fixtures are used by name
    _DeepseekLaunchRig,
    _pinned_kiro_cli_version,
)

from kiro_crew.acp.client_turn_control import (
    DEEPSEEK_DSH_TESTED_RANGE,
    DEEPSEEK_DSH_TESTED_VERSION,
    untested_version_issue,
)
from kiro_crew.acp.harness import deepseek as deepseek_mod
from kiro_crew.agent_sdk.backends import ACP_BACKEND_DEEPSEEK
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.acp import AcpProvider

DEEPSEEK = ACP_BACKEND_DEEPSEEK
LANE_PACKAGE_JSON = Path(__file__).resolve().parent / "real_adapters" / "package.json"
#: What every dsh-acp release reports in its handshake (``lib/index.js``).
HANDSHAKE_VERSION = "0.0.1"


@pytest.mark.parametrize("version", ["0.1.9", "0.3.0", "1.0.0"])
def test_outside_the_tested_release_warns_with_version_range_and_fix(version):
    issue = untested_version_issue(DEEPSEEK, version)
    assert f"deepseek {version} is outside the tested range 0.2.x" in issue
    assert f"npm i -g {deepseek_mod.DEEPSEEK_DSH_NPM_PKG}@{DEEPSEEK_DSH_TESTED_VERSION}" in issue


@pytest.mark.parametrize("version", ["0.2.0-rc.2", "0.2.0", "0.2.5", "", "unknown", None])
def test_a_tested_or_unreadable_version_is_silent(version):
    assert untested_version_issue(DEEPSEEK, version) == ""


def test_the_tested_release_is_the_lane_pin():
    pins = json.loads(LANE_PACKAGE_JSON.read_text(encoding="utf-8"))["dependencies"]
    assert pins[deepseek_mod.DEEPSEEK_DSH_NPM_PKG] == DEEPSEEK_DSH_TESTED_VERSION
    low, high = DEEPSEEK_DSH_TESTED_RANGE
    assert untested_version_issue(DEEPSEEK, ".".join(map(str, low))) == ""
    assert untested_version_issue(DEEPSEEK, ".".join(map(str, high)))


def _notice(client) -> str:
    provider = SimpleNamespace(_client=client, agent_version=client.agent_version)
    return AcpProvider.tested_version_notice.fget(provider)


def test_the_notice_reads_the_spawned_manifest_not_the_handshake(monkeypatch):
    rows = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    client = SimpleNamespace(
        backend=DEEPSEEK, agent_version=HANDSHAKE_VERSION, harness_installed_version="0.3.1"
    )
    chat_runner._notice_steer_upgrade(
        object(), object(), SimpleNamespace(tested_version_notice=_notice(client))
    )
    assert len(rows) == 1
    assert rows[0][2] == "notice"
    assert "deepseek 0.3.1 is outside the tested range 0.2.x" in rows[0][3]


@pytest.mark.parametrize("installed", [DEEPSEEK_DSH_TESTED_VERSION, "", None])
def test_the_handshake_version_alone_never_warns(installed):
    client = SimpleNamespace(backend=DEEPSEEK, agent_version=HANDSHAKE_VERSION)
    if installed is not None:
        client.harness_installed_version = installed
    assert _notice(client) == ""


def _install_dsh(root: Path, version: str) -> Path:
    package = root / "node_modules" / "@deepseek-ai" / "dsh"
    (package / "lib").mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": deepseek_mod.DEEPSEEK_DSH_NPM_PKG, "version": version}),
        encoding="utf-8",
    )
    entry = package / "lib" / "bin.js"
    entry.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    return entry


def test_the_installed_version_is_read_from_dsh_own_manifest(tmp_path):
    entry = _install_dsh(tmp_path, "0.2.0-rc.2")
    assert deepseek_mod._dsh_installed_version(str(entry)) == "0.2.0-rc.2"


def test_a_wrapper_or_missing_binary_reads_no_version(tmp_path):
    wrapper = tmp_path / "bin" / "dsh"
    wrapper.parent.mkdir()
    wrapper.write_text('#!/bin/sh\nexec dsh "$@"\n', encoding="utf-8")
    assert deepseek_mod._dsh_installed_version(str(wrapper)) == ""
    assert deepseek_mod._dsh_installed_version(str(tmp_path / "absent")) == ""


def test_a_per_session_spawn_records_the_installed_version(monkeypatch, tmp_path):
    rig = _DeepseekLaunchRig(monkeypatch, tmp_path)
    seen = []

    def _read(dsh_bin):
        seen.append(dsh_bin)
        return "0.3.1"

    monkeypatch.setattr(deepseek_mod, "_dsh_installed_version", _read)
    rig.run()
    assert seen == [rig.ARGV[0]]
    assert rig.client.harness_installed_version == "0.3.1"
