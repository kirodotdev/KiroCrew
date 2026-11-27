"""A spawn does not wait for an MCP server its parent's session has given up on.

A new sub-agent session holds its first prompt, bounded, until each server its
agent spec declares has reported, so a slow server is in its first turn. A server
that never answers would make every spawn wait that whole bound. The parent's
session on the same host already started the same servers: one it holds as
failed, or still starting long after it began, is not waited for. Every other
server is waited for as before.

The dead server here never answers at all and the slow one reports after a few
quiet reads, on the real dedicated transport and the real shared-runtime handle.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_runtime_helpers import _bounded, _make_runtime, _register

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.types import (
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    METHOD_KAS_MCP_STATUS,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk import mcp_problems

_SPEC = {"mcpServers": {"slow-ok": {"command": "node"}, "dead": {"command": "node"}}}
#: The read on which the slow server reports: after several quiet ones.
_SLOW_READ = 4


def _initialized(server: str) -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "method": "_kiro.dev/mcp/server_initialized",
            "params": {"sessionId": "s", "serverName": server},
        }
    )


def _report(**states: str) -> McpSessionReport:
    """A started report declaring ``_SPEC``, each named server put in its state."""
    report = McpSessionReport()
    report.begin_session([])
    report.record_declared(_SPEC)
    for name, state in states.items():
        if state == "connected":
            report.record_event(EVENT_MCP_SERVER_INITIALIZED, name)
        elif state == "failed":
            report.record_event(EVENT_MCP_SERVER_INIT_FAILURE, name, "timed out")
    return report


def _aged(report: McpSessionReport, secs: float) -> McpSessionReport:
    report.began_at = time.monotonic() - secs
    return report


class _Parent:
    def __init__(self, report: McpSessionReport | None) -> None:
        self._report = report

    def mcp_session_report(self) -> McpSessionReport | None:
        return self._report


# ── The parent's report: what it has given up on ────────────────────────────


def test_a_failed_server_is_given_up_at_once():
    assert _report(**{"slow-ok": "connected", "dead": "failed"}).given_up(90.0) == ("dead",)


def test_a_server_still_starting_is_given_up_only_once_the_session_is_old():
    young = _report(**{"slow-ok": "connected"})
    assert young.given_up(90.0) == ()

    assert _aged(young, 91.0).given_up(90.0) == ("dead",)


def _failed_with(report: McpSessionReport, name: str, reason: str) -> McpSessionReport:
    report.record_event(EVENT_MCP_SERVER_INIT_FAILURE, name, reason)
    return report


def _starting_again(report: McpSessionReport, name: str) -> McpSessionReport:
    """The host's status snapshot lists *name* as connecting: a reset it is running."""
    report.fold_frame(
        JsonRpcMessage(
            method=METHOD_KAS_MCP_STATUS,
            params={"sessionId": "s", "servers": [{"name": name, "status": "connecting"}]},
        )
    )
    return report


_NO_CREDENTIAL = "Unable to locate credentials"
_START_TIMEOUT = "connection timed out after 120000ms"


def test_a_failed_server_the_host_still_retries_is_not_given_up():
    """A server that lacked a credential may come up on the re-attempt."""
    report = _failed_with(_report(**{"slow-ok": "connected"}), "dead", _NO_CREDENTIAL)
    report.set_reattempting({"dead"})

    assert report.given_up(90.0) == ()


def test_a_credential_retry_is_not_given_up_while_it_starts_again():
    report = _failed_with(_aged(_report(**{"slow-ok": "connected"}), 600.0), "dead", _NO_CREDENTIAL)
    report.set_reattempting({"dead"})

    assert _starting_again(report, "dead").given_up(90.0) == ()


@pytest.mark.parametrize("starting_again", [False, True], ids=["failed", "starting-again"])
def test_a_retry_of_a_start_that_timed_out_is_given_up(starting_again):
    """A second start takes as long as the first: longer than a spawn waits for it."""
    report = _failed_with(_aged(_report(**{"slow-ok": "connected"}), 600.0), "dead", _START_TIMEOUT)
    report.set_reattempting({"dead"})
    if starting_again:
        _starting_again(report, "dead")

    assert report.given_up(90.0) == ("dead",)


def test_a_young_session_does_not_give_up_on_a_timed_out_start_it_starts_again():
    """Starting again within the bound is still the bound's to wait for."""
    report = _failed_with(_report(**{"slow-ok": "connected"}), "dead", _START_TIMEOUT)
    report.set_reattempting({"dead"})

    assert _starting_again(report, "dead").given_up(90.0) == ()


@pytest.mark.parametrize("later", ["connected", "credential"])
def test_a_later_report_replaces_the_timed_out_start(later):
    """A server that came up (then dropped), or failed for a credential, is retried for real."""
    report = _failed_with(_aged(_report(**{"slow-ok": "connected"}), 600.0), "dead", _START_TIMEOUT)
    if later == "connected":
        report.record_event(EVENT_MCP_SERVER_INITIALIZED, "dead")
    else:
        _failed_with(report, "dead", _NO_CREDENTIAL)
    report.set_reattempting({"dead"})

    assert _starting_again(report, "dead").given_up(90.0) == ()


def test_a_new_session_forgets_a_timed_out_start():
    report = _failed_with(_report(**{"slow-ok": "connected"}), "dead", _START_TIMEOUT)
    report.begin_session([])
    report.record_declared(_SPEC)
    report.record_event(EVENT_MCP_SERVER_INITIALIZED, "slow-ok")
    report.set_reattempting({"dead"})

    assert _starting_again(_aged(report, 600.0), "dead").given_up(90.0) == ()


def test_a_server_the_host_retries_is_not_given_up_while_it_starts_again():
    report = _aged(_report(**{"slow-ok": "connected"}), 600.0)
    report.set_reattempting({"dead"})

    assert report.given_up(90.0) == ()


def test_a_shared_sub_agent_parent_is_read_from_its_run():
    """A sub-agent on a shared runtime keeps its session on its run, not in the registry."""
    from kiro_crew.subagent_manager import turn_session

    shared = _Parent(_report(**{"dead": "failed"}))
    run = MagicMock(
        id="p1", conversation_key="", done=False, _session_sharing=True, _shared_provider=shared
    )
    manager = MagicMock(_agents={"p1": run})
    manager._sessions.get_provider = MagicMock(return_value=None)

    assert turn_session._parent_provider(manager, "subagent:p1") is shared
    run.done = True
    assert turn_session._parent_provider(manager, "subagent:p1") is None
    assert turn_session._parent_provider(manager, "") is None


def test_a_host_that_reported_nothing_gives_up_on_nothing():
    """Such a host may be one that never reports: its silence says nothing."""
    assert _aged(_report(), 3600.0).given_up(90.0) == ()


def test_a_new_session_starts_the_clock_again():
    report = _aged(_report(**{"slow-ok": "connected"}), 3600.0)
    report.begin_session([])
    report.record_declared(_SPEC)
    report.record_event(EVENT_MCP_SERVER_INITIALIZED, "slow-ok")

    assert report.given_up(90.0) == ()


@pytest.mark.parametrize(
    "provider",
    [None, _Parent(None), MagicMock(), _Parent(MagicMock(given_up=MagicMock(side_effect=OSError)))],
    ids=["no-parent", "no-report", "unknown-provider", "unreadable"],
)
def test_given_up_servers_is_silent_without_a_readable_report(provider):
    assert mcp_problems.given_up_servers(provider) == ()


# ── The two transports wait for every other declared server ─────────────────


def _dedicated(work_dir: Path) -> tuple[AcpClient, dict]:
    client = AcpClient(work_dir=work_dir)
    client._mcp_notifications = []
    client._mcp_report.begin_session([])
    client._mcp_report.record_declared(_SPEC)
    reads = {"n": 0}

    async def slow_then_silent(timeout=2.0):
        # The dead server never answers; the slow one does after quiet reads.
        reads["n"] += 1
        if reads["n"] == _SLOW_READ:
            return _initialized("slow-ok")
        await asyncio.sleep(0)
        return None

    client._read_message = slow_then_silent
    return client, reads


@pytest.mark.asyncio
async def test_the_client_waits_for_the_slow_server_and_not_the_skipped_one(tmp_path):
    client, reads = _dedicated(tmp_path)

    missing = await _bounded(
        client.wait_for_declared_mcp(3600.0, ("dead",)), what="the client wait with a skip"
    )

    assert missing == ("dead",)
    assert reads["n"] == _SLOW_READ, "the wait ends on the slow server's report"
    assert client._mcp_report.declared_unreported() == ("dead",)


@pytest.mark.asyncio
async def test_the_client_does_not_drain_when_every_unreported_server_is_skipped(tmp_path):
    client, reads = _dedicated(tmp_path)

    missing = await _bounded(
        client.wait_for_declared_mcp(3600.0, ("slow-ok", "dead")), what="an all-skipped wait"
    )

    assert set(missing) == {"slow-ok", "dead"}
    assert reads["n"] == 0


@pytest.mark.asyncio
async def test_the_handle_waits_for_the_slow_server_and_not_the_skipped_one():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    report = handle.mcp_session_report()
    report.begin_session([])
    report.record_declared(_SPEC)

    waiter = asyncio.ensure_future(handle.wait_for_declared_mcp(3600.0, ("dead",)))
    frame = JsonRpcMessage.from_dict(
        {
            "method": "_kiro.dev/mcp/server_initialized",
            "params": {"sessionId": "sA", "serverName": "slow-ok"},
        }
    )
    report.fold_frame(frame)
    handle._queue.put_nowait(frame)
    missing = await _bounded(waiter, what="the handle wait with a skip")

    assert missing == ("dead",)
    assert report.declared_unreported() == ("dead",)


# ── A spawn's first prompt ──────────────────────────────────────────────────


@pytest.fixture()
def spawn_facade(monkeypatch):
    """The session re-checks ``acquire_session`` reads off ``kiro_crew.subagent``, inert."""
    import kiro_crew.subagent as sa

    async def keep(client, *_a, **_k):
        return client

    async def reproject(_sessions, _key, _agent, claimed, _claim):
        return claimed

    monkeypatch.setattr(sa, "replace_stale_shared_session", keep)
    monkeypatch.setattr(sa, "invalidate_stale_kas_session", AsyncMock())
    monkeypatch.setattr(sa, "reproject_claimed_session", reproject)


async def _acquire(client, parent, *, shared: bool, parent_key: str = "parent") -> None:
    from kiro_crew.subagent_manager import turn_session

    manager = MagicMock()
    manager._create_shared_session = AsyncMock(return_value=client)
    manager._ensure_dedicated_start_priced = AsyncMock()
    manager._is_cc_provider = MagicMock(return_value=False)
    manager._sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    manager._sessions.get_provider = MagicMock(
        side_effect=lambda key: parent if key == "parent" else None
    )
    await turn_session.acquire_session(
        MagicMock(_manager=manager),
        MagicMock(id="sub1", parent_session_key=parent_key),
        "subagent:sub1",
        "kirocrew",
        parent_policy="auto",
        extra_kwargs={},
        use_session_sharing=shared,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [True, False], ids=["shared-runtime", "dedicated"])
@pytest.mark.parametrize(
    "parent_report",
    [
        lambda: _report(**{"slow-ok": "connected", "dead": "failed"}),
        lambda: _aged(_report(**{"slow-ok": "connected"}), 600.0),
    ],
    ids=["parent-failed-it", "parent-hung-on-it"],
)
async def test_a_spawn_with_a_dead_server_prompts_once_the_slow_one_reports(
    spawn_facade, shared, parent_report, tmp_path
):
    """The 30 s bound is untouched; the wait ends on the slow server, not at it."""
    client, reads = _dedicated(tmp_path)

    await _bounded(
        _acquire(client, _Parent(parent_report()), shared=shared), what="the spawn's wait"
    )

    assert reads["n"] == _SLOW_READ
    assert client._mcp_report.declared_not_ready() == ("dead",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent", "parent_key"),
    [
        # Built when the test runs: a report built at collection would age past the bound.
        (lambda: None, ""),
        (lambda: _Parent(_report(**{"slow-ok": "connected"})), "parent"),
        (lambda: _Parent(_report(**{"slow-ok": "connected", "dead": "connected"})), "parent"),
    ],
    ids=["no-parent-cli-spawn", "parent-still-starting-it", "parent-connected-it"],
)
async def test_without_the_parents_evidence_a_spawn_waits_for_every_declared_server(
    spawn_facade, monkeypatch, parent, parent_key, tmp_path
):
    from kiro_crew.subagent_manager import turn_session

    seen: list[tuple] = []

    async def waited(provider, timeout, *, seen_by=None):
        seen.append(mcp_problems.given_up_servers(seen_by))

    monkeypatch.setattr(turn_session, "wait_for_declared_mcp", waited)
    client, _ = _dedicated(tmp_path)

    await _acquire(client, parent(), shared=False, parent_key=parent_key)

    assert seen == [()], "nothing is skipped, so the dead server is waited for to the bound"


@pytest.mark.asyncio
async def test_a_spawn_whose_declared_servers_all_died_does_not_wait(tmp_path):
    client, reads = _dedicated(tmp_path)
    parent = _Parent(_report(**{"slow-ok": "failed", "dead": "failed"}))

    await _bounded(_acquire(client, parent, shared=False), what="an all-dead spawn")

    assert reads["n"] == 0


@pytest.mark.asyncio
async def test_a_server_that_fails_fast_in_the_child_ends_the_wait_without_the_parent(tmp_path):
    """Failing is a report; only hanging costs the bound."""
    client = AcpClient(work_dir=tmp_path)
    client._mcp_notifications = []
    client._mcp_report.begin_session([])
    client._mcp_report.record_declared(_SPEC)
    frames = iter(
        [
            _initialized("slow-ok"),
            JsonRpcMessage.from_dict(
                {
                    "method": "_kiro.dev/mcp/server_init_failure",
                    "params": {"sessionId": "s", "serverName": "dead", "error": "ENOENT"},
                }
            ),
        ]
    )

    async def read(timeout=2.0):
        return next(frames, None)

    client._read_message = read

    await _bounded(_acquire(client, None, shared=False, parent_key=""), what="a fast failure")

    assert client._mcp_report.declared_not_ready() == ("dead",)
    assert client._mcp_report.declared_unreported() == ()
