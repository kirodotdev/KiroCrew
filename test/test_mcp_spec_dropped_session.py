"""A server the spec writer dropped for a missing command is named by the session and doctor.

The spec writer (``mcp_sources.resolve_mcp_servers``) leaves such a server out of
the default agent's spec, so no session array carries it and the array prune never
sees it: on claude, codex, opencode and pi the session said nothing, its MCP report
listed no failure and ``kirocrew doctor`` had no line, while the Connections page
showed the server as an error. These tests pin that the rebuild's drop record
(``mcp_dropped_servers``) reaches each of the three:

1. ``mcp_prune.spec_dropped_servers`` reads it for the agents that carry the
   default agent's servers, minus a name the session's spec still defines.
2. An ``AcpClient`` session on any harness records each drop as failed, with its
   reason, on every attempt's report, and the ref guard does not call it
   unconfigured too.
3. A new chat session gets one warn notice naming it.
4. ``kirocrew doctor`` prints a line for it and counts an issue.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from kiro_crew import mcp_dropped_servers
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.mcp_prune import spec_dropped_servers
from kiro_crew.acp.mcp_session_report import BUCKET_CAP, McpSessionReport
from kiro_crew.acp.types import EVENT_MCP_SERVER_INIT_FAILURE
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.doctor_checks import mcp as doctor_mcp

# abspath gives it a drive on Windows, where a drive-less rooted path is not absolute.
_MISSING = os.path.abspath(os.path.join(os.sep, "nonexistent", "bin", "no-such-server"))


@pytest.fixture
def dropped_store(tmp_path, monkeypatch):
    """The rebuild's drop record, in *tmp_path*, holding ``p-dead``."""
    path = tmp_path / mcp_dropped_servers.STORE_FILENAME
    monkeypatch.setattr(mcp_dropped_servers, "store_path", lambda: path)
    mcp_dropped_servers.record_rebuild_drops({"p-dead": _MISSING})
    return path


# ── The read ─────────────────────────────────────────────────────────────────


def test_the_default_agent_and_the_worker_read_the_drop(dropped_store):
    for agent in ("kirocrew", "kirocrew-worker"):
        [server] = spec_dropped_servers(agent, {"mcpServers": {}})
        assert server.name == "p-dead"
        assert server.reason == (
            "command not found (install it, or fix the command on the MCP Servers page): "
            f"{_MISSING}"
        )


def test_other_agents_and_a_name_the_spec_defines_read_nothing(dropped_store):
    # The record describes the default agent's server set only.
    assert spec_dropped_servers("kirocrew-conductor", None) == ()
    assert spec_dropped_servers(None, None) == ()
    # A spec that still defines the name (a project-level copy) is not missing it.
    assert spec_dropped_servers("kirocrew", {"mcpServers": {"p-dead": {"command": "x"}}}) == ()


def test_no_record_reads_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_dropped_servers, "store_path", lambda: tmp_path / "absent.json")
    assert spec_dropped_servers("kirocrew", None) == ()


# ── The session report ───────────────────────────────────────────────────────


def _client(tmp_path, monkeypatch, backend: str) -> tuple[AcpClient, list[dict]]:
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=backend)
    sent: list[dict] = []

    async def _work_dir():
        return str(tmp_path)

    async def _send(_method, params):
        sent.append(params)
        return len(sent)

    async def _wait(_rid, **_kw):
        return {"sessionId": "s-1"}

    monkeypatch.setattr(client, "_session_work_dir", _work_dir)
    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_wait_for_response", _wait)
    monkeypatch.setattr(client, "_read_mcp_ref_spec", lambda: {"tools": ["@p-dead"]})
    good = {"name": "good", "command": sys.executable, "args": [], "env": [], "type": "stdio"}
    monkeypatch.setattr(client, "_session_mcp_servers", lambda: [good])
    monkeypatch.setattr(client, "_pooled_mcp_servers", lambda: [])
    return client, sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend",
    [ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_OPENCODE, ACP_BACKEND_PI, ACP_BACKEND_KIRO],
)
async def test_every_harness_session_names_the_dropped_server_as_failed(
    tmp_path, monkeypatch, dropped_store, backend
):
    client, sent = _client(tmp_path, monkeypatch, backend)
    # The spawn's off-loop hop: the snapshot, then the record read beside it.
    client._prepare_spawn_workspace()
    client._begin_session_report([])
    client._guard_unresolved_mcp_refs([])

    report = client.mcp_session_report()
    payload = report.payload() or {}
    assert payload["failed"] == ["p-dead"]
    assert payload["failures"]["p-dead"].endswith(f"MCP Servers page): {_MISSING}")
    assert report.crew_left_out == ("p-dead",)
    # Configured, so not also "declared by the agent spec but not configured".
    assert "@p-dead" not in payload["unresolved_refs"]
    assert "failed to start: p-dead (command not found" in report.problem_summary()
    # A session/load that fails falls back to session/new: the drop is on that
    # attempt's report too.
    client._begin_session_report([])
    assert (client.mcp_session_report().payload() or {})["failed"] == ["p-dead"]


@pytest.mark.asyncio
async def test_without_a_record_the_report_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_dropped_servers, "store_path", lambda: tmp_path / "absent.json")
    client, _sent = _client(tmp_path, monkeypatch, ACP_BACKEND_KIRO)
    client._prepare_spawn_workspace()
    client._begin_session_report([])
    assert (client.mcp_session_report().payload() or {})["failed"] == []
    assert client.mcp_session_report().crew_left_out == ()


def test_drops_past_the_failed_bucket_cap_are_counted(tmp_path, monkeypatch):
    path = tmp_path / mcp_dropped_servers.STORE_FILENAME
    monkeypatch.setattr(mcp_dropped_servers, "store_path", lambda: path)
    mcp_dropped_servers.record_rebuild_drops({f"d{i:03d}": _MISSING for i in range(BUCKET_CAP + 2)})
    client, _sent = _client(tmp_path, monkeypatch, ACP_BACKEND_OPENCODE)
    client._prepare_spawn_workspace()
    client._begin_session_report([])
    payload = client.mcp_session_report().payload() or {}
    assert len(payload["failed"]) == BUCKET_CAP
    assert payload["pruned_omitted"] == 2


# ── The chat notice ──────────────────────────────────────────────────────────


@pytest.fixture
def notices(monkeypatch):
    seen: list[tuple[str, str, str, dict]] = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls="", **kw: seen.append((role, text, cls, kw)),
    )
    return seen


class _Provider:
    def __init__(self, report: McpSessionReport) -> None:
        self._report = report

    def mcp_session_report(self) -> McpSessionReport:
        return self._report


def test_a_new_session_gets_one_notice_naming_the_left_out_server(notices):
    report = McpSessionReport()
    report.begin_session([])
    report.record_pruned("p-dead", f"command not found: {_MISSING}")

    chat_runner._notice_crew_left_out_mcp(SimpleNamespace(), SimpleNamespace(), _Provider(report))

    [(role, text, cls, kw)] = notices
    assert role == "notice"
    assert cls == "msg msg-warn"
    # The tag that keeps it out of the folded steps (TurnBlock.isAlwaysVisible).
    assert kw["meta"] == {"kind": "mcp_servers_left_out"}
    assert text.startswith("⚠ ")
    assert f"p-dead (command not found: {_MISSING})" in text
    assert "Fix what the reason names" in text


def test_a_pi_registration_refusal_is_named_with_its_own_cause(notices):
    """A refusal that is not a command problem is not blamed on the command."""
    from kiro_crew.acp.pi_mcp_registration import UNREGISTERED_REASONS

    report = McpSessionReport()
    report.begin_session([])
    report.record_pruned("probe", UNREGISTERED_REASONS["mcp_json"])

    chat_runner._notice_crew_left_out_mcp(SimpleNamespace(), SimpleNamespace(), _Provider(report))

    [(_role, text, _cls, _kw)] = notices
    assert "same name" in text
    assert "command" not in text


def test_a_clean_session_or_a_host_failure_gets_no_notice(notices):
    report = McpSessionReport()
    report.begin_session([])
    chat_runner._notice_crew_left_out_mcp(SimpleNamespace(), SimpleNamespace(), _Provider(report))
    # A server the host itself reported as failed is not one Crew left out.
    report.record_event(EVENT_MCP_SERVER_INIT_FAILURE, "slow", "boom")
    assert (report.payload() or {})["failed"] == ["slow"]
    chat_runner._notice_crew_left_out_mcp(SimpleNamespace(), SimpleNamespace(), _Provider(report))
    chat_runner._notice_crew_left_out_mcp(SimpleNamespace(), SimpleNamespace(), object())
    assert notices == []


# ── Doctor ───────────────────────────────────────────────────────────────────


def test_doctor_names_the_dropped_server_and_counts_an_issue(dropped_store, capsys):
    issues: list[str] = []
    doctor_mcp._doctor_dropped_mcp_servers(issues)
    out = capsys.readouterr().out
    assert "'p-dead' left out of the default agent: command not found" in out
    assert issues == ["1 MCP server(s) left out: command not found"]


def test_doctor_is_clean_without_a_drop(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mcp_dropped_servers, "store_path", lambda: tmp_path / "absent.json")
    issues: list[str] = []
    doctor_mcp._doctor_dropped_mcp_servers(issues)
    assert "✅" in capsys.readouterr().out
    assert issues == []
