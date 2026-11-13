"""A codex chat whose codex-acp lacks ``external-sandbox`` says that shell commands fail.

The condition is the offer ``permission_value_for`` reads, so the notice shows
exactly when the session keeps ``read-only`` (whose own OS sandbox Crew's sandbox
refuses) and stays silent once an adapter offers the mode.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_PREFERRED_PERMISSION_CONFIG,
    KNOWN_LIMITATIONS,
    known_limitation_issue,
    permission_value_for,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.acp import AcpProvider

CODEX = sdk_backends.ACP_BACKEND_CODEX
LIVE_CAPTURE = (
    Path(__file__).resolve().parent / "fixtures" / "acp_frames" / "codex" / "session-live.jsonl"
)


def _mode(*values: str) -> list[dict]:
    return [{"id": "mode", "type": "select", "options": [{"value": v} for v in values]}]


def _live_config_options() -> list[dict]:
    for line in LIVE_CAPTURE.read_text(encoding="utf-8").splitlines():
        result = (json.loads(line).get("result") or {}) if line.strip() else {}
        if result.get("configOptions"):
            return result["configOptions"]
    raise AssertionError("no configOptions in the codex live capture")


def test_an_adapter_without_external_sandbox_gets_the_notice():
    issue = known_limitation_issue(CODEX, _mode("read-only", "agent"))
    assert "git included, fail" in issue
    assert "agentclientprotocol/codex-acp#732" in issue


def test_the_released_adapter_crew_pins_gets_the_notice():
    # codex-acp 2.3.0, the Real Adapter lane's pin, as recorded.
    assert known_limitation_issue(CODEX, _live_config_options())


def test_an_adapter_offering_external_sandbox_is_silent():
    assert known_limitation_issue(CODEX, _mode("read-only", "external-sandbox")) == ""


def test_no_options_and_other_harnesses_are_silent():
    assert known_limitation_issue(CODEX, []) == ""
    assert known_limitation_issue(CODEX, None) == ""
    assert known_limitation_issue(sdk_backends.ACP_BACKEND_CLAUDE, _mode("read-only")) == ""


def test_the_notice_agrees_with_the_permission_write():
    for options in (_mode("read-only"), _mode("read-only", "external-sandbox")):
        keeps_read_only = permission_value_for(CODEX, options) != "external-sandbox"
        assert bool(known_limitation_issue(CODEX, options)) is keeps_read_only


def test_the_codex_entry_names_no_release_until_one_exists():
    (entry,) = [e for e in KNOWN_LIMITATIONS if e.harness == CODEX]
    assert entry.missing_config_value == ACP_BACKEND_PREFERRED_PERMISSION_CONFIG[CODEX]
    assert entry.needs_adapter == sdk_backends.ACP_BACKEND_NODE_ADAPTER_PACKAGES[CODEX]
    # agentclientprotocol/codex-acp#732 is unreleased; set fixed_in when it ships.
    assert entry.fixed_in == ""


def test_the_provider_reads_the_session_options():
    inner = SimpleNamespace(backend=CODEX, acp_config_options=_mode("read-only"))
    assert AcpProvider.known_limitation_notice.fget(SimpleNamespace(_client=inner))


def test_a_new_chat_gets_one_notice_row(monkeypatch):
    rows = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    notice = known_limitation_issue(CODEX, _mode("read-only"))
    chat_runner._notice_steer_upgrade(
        object(), object(), SimpleNamespace(known_limitation_notice=notice)
    )
    assert len(rows) == 1
    assert rows[0][2] == "notice"
    assert "git included" in rows[0][3]
