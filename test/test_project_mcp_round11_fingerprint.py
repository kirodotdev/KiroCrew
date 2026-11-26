"""Every field outside the non-launch set counts toward a project server's approval."""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE

_ENTRY = {"type": "http", "url": "https://example.test/mcp"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    crew = tmp_path / "crew"
    crew.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    return crew


def _write(folder, entry: dict[str, Any]) -> None:
    (folder / CLAUDE_PROJECT_MCP_FILE).write_text(
        json.dumps({"mcpServers": {"remote": entry}}), encoding="utf-8"
    )


def _approved(tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    _write(folder, _ENTRY)
    [server] = approvals.pending_servers(folder)
    approvals.approve(server.folder, server.file, server.name, server.fingerprint)
    assert approvals.pending_servers(folder) == []
    return folder


@pytest.mark.parametrize(
    "extra",
    [
        {"headersHelper": "/bin/sh -c 'print-token'"},
        {"oauth": {"clientId": "abc", "scopes": ["read"]}},
    ],
    ids=["headersHelper", "oauth"],
)
def test_a_field_added_after_approval_holds_the_server_again(home, tmp_path, extra):
    folder = _approved(tmp_path)
    _write(folder, {**_ENTRY, **extra})
    [held] = approvals.pending_servers(folder)
    assert held.name == "remote"
    assert held.status == approvals.STATUS_CHANGED


@pytest.mark.parametrize(
    "extra",
    [{"disabledTools": ["write"]}, {"autoApprove": ["read"]}, {"timeout": 60000}],
    ids=["disabledTools", "autoApprove", "timeout"],
)
def test_a_tool_switch_or_start_budget_keeps_the_approval(home, tmp_path, extra):
    folder = _approved(tmp_path)
    _write(folder, {**_ENTRY, **extra})
    assert approvals.pending_servers(folder) == []


def test_the_card_names_the_added_field(home, tmp_path):
    from kiro_crew.change_card_project_mcp import build_preview, clean_params, server_snapshot

    folder = _approved(tmp_path)
    _write(folder, {**_ENTRY, "headersHelper": "print-token"})
    [held] = approvals.pending_servers(folder)
    params = clean_params(
        {
            "folder": held.folder,
            "file": held.file,
            "server": held.name,
            "fingerprint": held.fingerprint,
        }
    )
    out: dict[str, Any] = {}
    build_preview(params, server_snapshot(params), out)
    rows = {row["label"]: row for row in out["changes"]}
    assert rows["headersHelper"] == {"label": "headersHelper", "after": "print-token"}
    # The launch rows that did not change say nothing new.
    assert "URL" not in rows and "type" not in rows


def test_the_record_keeps_a_digest_of_each_field_never_its_value(home, tmp_path):
    folder = _approved(tmp_path)
    _write(folder, {**_ENTRY, "oauth": {"clientSecret": "s3cr3t-value"}})
    [held] = approvals.pending_servers(folder)
    approvals.approve(held.folder, held.file, held.name, held.fingerprint)
    text = approvals.store_path().read_text(encoding="utf-8")
    assert "s3cr3t-value" not in text
    record = json.loads(text)["folders"][held.folder][CLAUDE_PROJECT_MCP_FILE]["remote"]
    assert set(record["extra"]) == {"oauth"}
