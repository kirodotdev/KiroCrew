"""An unstartable stdio element is left out of goose and deepseek sessions, and named.

goose drops such an element silently and deepseek fails the whole ``session/new``
over it (the two captures under ``test/fixtures/acp_frames``). Crew prunes the
element before the request and records it as a failed server on the session's MCP
report, so goose's session says what it lost and deepseek's session starts.
"""

from __future__ import annotations

import inspect
import os
import stat
import sys

import pytest

import kiro_crew.acp.client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.mcp_session_report import BUCKET_CAP, ERROR_CAP, NAME_CAP
from kiro_crew.acp.mcp_startable import prune_unstartable, unstartable_reason
from kiro_crew.acp_backends import (
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP,
)

# abspath gives it a drive on Windows, where a drive-less rooted path is not absolute.
_MISSING = os.path.abspath(os.path.join(os.sep, "nonexistent-g19", "no-such-mcp-server"))


def _stdio(name: str, command: str, env: list | None = None) -> dict:
    return {"name": name, "command": command, "args": [], "env": env or [], "type": "stdio"}


# ── The check ────────────────────────────────────────────────────────────────


def test_an_absolute_command_that_does_not_exist_is_unstartable():
    assert unstartable_reason(_stdio("bad", _MISSING)) == f"command not found: {_MISSING}"


def test_the_running_interpreter_is_startable():
    assert unstartable_reason(_stdio("ok", sys.executable)) == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX execute bit")
def test_an_absolute_file_without_the_execute_bit_is_unstartable(tmp_path):
    script = tmp_path / "server"
    script.write_text("#!/bin/sh\n")
    script.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert unstartable_reason(_stdio("bad", str(script))).startswith("command not found")


def test_a_bare_name_on_no_path_is_unstartable(monkeypatch):
    monkeypatch.setattr("kiro_crew.acp.mcp_startable.mcp_search_path", lambda _base: "")
    reason = unstartable_reason(_stdio("bad", "no-such-mcp-server-g19"))
    assert reason == "command not found on PATH: no-such-mcp-server-g19"


def test_a_bare_name_on_the_elements_own_path_is_startable(tmp_path, monkeypatch):
    # The element's PATH reaches the shared MCP search path, which is what resolves it.
    searched: list[str] = []

    def _search(declared):
        searched.append(declared)
        return declared

    monkeypatch.setattr("kiro_crew.acp.mcp_startable.mcp_search_path", _search)
    name = "g19-server.exe" if sys.platform == "win32" else "g19-server"
    exe = tmp_path / name
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    element = _stdio("ok", name, env=[{"name": "Path", "value": str(tmp_path)}])
    assert unstartable_reason(element) == ""
    assert searched == [str(tmp_path)]


def test_a_relative_path_and_a_remote_element_are_kept():
    assert unstartable_reason(_stdio("rel", os.path.join(".", "bin", "server"))) == ""
    assert unstartable_reason({"name": "remote", "type": "http", "url": "https://x"}) == ""
    assert unstartable_reason({"name": "no-command"}) == ""


def test_prune_keeps_order_and_names_each_pruned_server():
    good_a = _stdio("a", sys.executable)
    good_b = {"name": "b", "type": "http", "url": "https://x"}
    bad = _stdio("bad", _MISSING)
    kept, pruned = prune_unstartable([good_a, bad, good_b])
    assert kept == [good_a, good_b]
    assert [(p.name, p.reason) for p in pruned] == [("bad", f"command not found: {_MISSING}")]


# ── The session/new path ─────────────────────────────────────────────────────


def test_goose_deepseek_and_copilot_are_the_pruned_backends():
    assert ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP == {
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_COPILOT,
    }


def _drive(tmp_path, monkeypatch, backend: str, *, rolls_back: bool):
    """A client on *backend* whose array holds one good and one unstartable element.

    The fake harness answers the way the captures say the real one does: with
    *rolls_back* (deepseek) an unstartable element in ``mcpServers`` fails the whole
    ``session/new``; without it (goose) the session is created regardless.
    """
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=backend)
    good = _stdio("good", sys.executable)
    bad = _stdio("bad", _MISSING)
    sent: list[dict] = []

    async def _work_dir():
        return str(tmp_path)

    async def _send(_method, params):
        sent.append(params)
        return len(sent)

    async def _wait(_rid, **_kw):
        names = {e.get("name") for e in sent[-1]["mcpServers"]}
        if rolls_back and "bad" in names:
            return {}
        return {"sessionId": "s-1"}

    monkeypatch.setattr(client, "_session_work_dir", _work_dir)
    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_wait_for_response", _wait)
    # Each backend is a mirror: each carries the bad element in its own spec half,
    # and the pooled broker append is empty for them.
    monkeypatch.setattr(client, "_goose_session_mcp_servers", lambda: [good, bad])
    monkeypatch.setattr(client, "_deepseek_session_mcp_servers", lambda: [good, bad])
    monkeypatch.setattr(client, "_copilot_session_mcp_servers", lambda: [good, bad])
    monkeypatch.setattr(client, "_pooled_mcp_servers", lambda: [])
    return client, sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "rolls_back"),
    [(ACP_BACKEND_GOOSE, False), (ACP_BACKEND_DEEPSEEK, True), (ACP_BACKEND_COPILOT, False)],
)
async def test_the_session_starts_without_the_bad_element_and_names_it(
    tmp_path, monkeypatch, backend, rolls_back
):
    client, sent = _drive(tmp_path, monkeypatch, backend, rolls_back=rolls_back)

    resp = await client._new_session_following_substitution()

    assert resp["sessionId"] == "s-1"
    assert [e["name"] for e in sent[-1]["mcpServers"]] == ["good"]
    payload = client.mcp_session_report().payload()
    assert payload["configured"] == ["good"]
    assert payload["failed"] == ["bad"]
    assert payload["failures"]["bad"] == f"command not found: {_MISSING}"
    assert (
        "failed to start: bad (command not found" in client.mcp_session_report().problem_summary()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "rolls_back"),
    [(ACP_BACKEND_GOOSE, False), (ACP_BACKEND_DEEPSEEK, True), (ACP_BACKEND_COPILOT, False)],
)
async def test_a_pruned_server_the_spec_refs_is_failed_not_unresolved(
    tmp_path, monkeypatch, backend, rolls_back
):
    """``tools: ["@bad"]`` on a pruned ``bad`` names it once, as failed.

    The ref guard runs after the prune. Without counting the pruned name as
    configured it would also list ``@bad`` as declared but not configured, which
    points the user at the projection when the fault is the command. A ref that
    names nothing at all is still reported.
    """
    client, _sent = _drive(tmp_path, monkeypatch, backend, rolls_back=rolls_back)
    client._mcp_ref_spec = {
        "tools": ["@good", "@bad", "@ghost"],
        "mcpServers": {"good": {"command": sys.executable}, "bad": {"command": _MISSING}},
    }

    await client._new_session_following_substitution()

    payload = client.mcp_session_report().payload()
    assert payload["failed"] == ["bad"]
    assert payload["unresolved_refs"] == ["@ghost"]
    summary = client.mcp_session_report().problem_summary()
    assert "failed to start: bad (command not found" in summary
    assert "@bad" not in summary


@pytest.mark.asyncio
async def test_the_pruned_server_warning_redacts_a_key_in_the_command(
    tmp_path, monkeypatch, caplog
):
    """A whole CLI line in ``command`` reaches the warning only after redaction."""
    key = "sk-proj-" + "Q7" * 20
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_GOOSE)
    bad = _stdio("bad", f"{_MISSING} --api-key {key}")

    with caplog.at_level("WARNING", logger=client_mod.logger.name):
        kept = await client._prune_unstartable_mcp([bad])

    assert kept == []
    lines = [r.getMessage() for r in caplog.records if "leaving server" in r.getMessage()]
    assert len(lines) == 1
    assert "'bad'" in lines[0]
    assert key not in lines[0]
    assert "\n" not in lines[0]


@pytest.mark.asyncio
async def test_pruned_servers_are_bounded_where_kept_and_the_overflow_is_said(
    tmp_path, monkeypatch, caplog
):
    """Past the failed bucket's cap, pruned servers are counted, not kept.

    Every kept name and reason is cut to the report's caps, a refused server
    gets no row in the ref guard's names either, and the overflow is in the
    payload, the summary and one log line.
    """
    long = "n" * (NAME_CAP * 4)
    servers = [_stdio(f"{i:03d}{long}", _MISSING) for i in range(BUCKET_CAP + 1)]
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_GOOSE)

    with caplog.at_level("WARNING", logger=client_mod.logger.name):
        kept = await client._prune_unstartable_mcp(servers)
    client._begin_session_report(kept)

    assert kept == []
    assert len(client._mcp_pruned_names) == BUCKET_CAP
    assert all(len(name) <= NAME_CAP for name in client._mcp_pruned_names)
    payload = client.mcp_session_report().payload()
    assert len(payload["failed"]) == BUCKET_CAP
    assert set(client._mcp_pruned_names) == set(payload["failed"])
    assert all(len(reason) <= ERROR_CAP for reason in payload["failures"].values())
    assert payload["pruned_omitted"] == 1
    assert "(+1 more left out as unstartable, not listed)" in (
        client.mcp_session_report().problem_summary()
    )
    assert any("leaving 1 more unstartable" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_backend_outside_the_set_sends_the_array_untouched(tmp_path, monkeypatch):
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_OPENCODE)
    bad = _stdio("bad", _MISSING)
    sent: list[dict] = []

    async def _work_dir():
        return str(tmp_path)

    async def _send(_method, params):
        sent.append(params)
        return 1

    async def _wait(_rid, **_kw):
        return {"sessionId": "s-1"}

    monkeypatch.setattr(client, "_session_work_dir", _work_dir)
    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_wait_for_response", _wait)
    monkeypatch.setattr(client, "_opencode_session_mcp_servers", lambda: [bad])
    monkeypatch.setattr(client, "_pooled_mcp_servers", lambda: [])

    await client._new_session_following_substitution()

    assert [e["name"] for e in sent[-1]["mcpServers"]] == ["bad"]
    assert client.mcp_session_report().payload()["failed"] == []


def test_every_array_build_is_pruned_before_its_roster_handoff():
    """All three builds -- session/new, its substitution retry, session/load -- prune.

    Held by structure, the way the ref guard's pairing is: each roster hand-off to
    the report must be preceded by a prune of the same params' array, so a fourth
    session-establishment path that forgets it fails here.
    """
    lines = [
        line
        for name in (client_mod.__name__, *client_mod._CLIENT_OWNERS)
        for line in inspect.getsource(sys.modules[name]).splitlines()
    ]
    handoffs = [i for i, ln in enumerate(lines) if "self._begin_session_report(" in ln]
    prunes = [i for i, ln in enumerate(lines) if "await self._prune_unstartable_mcp(" in ln]
    assert len(handoffs) == 3
    assert len(prunes) == 3
    for prune, handoff in zip(prunes, handoffs):
        assert prune < handoff
        assert (
            lines[prune - 1].strip() == "if self.backend in ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP:"
        ), f"a prune is awaited on every backend's path (H13): {lines[prune]!r}"
        target = lines[prune].split("=", 1)[0].strip()
        assert target.endswith('["mcpServers"]'), lines[prune]
        params = target.split("[", 1)[0]
        assert f'{params}.get("mcpServers")' in lines[handoff], (lines[prune], lines[handoff])
