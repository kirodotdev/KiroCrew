"""The reconciler's kill arm against agents an MCP server starts.

A session's ``kiro-cli`` starts its MCP servers with its own environment, so every
server, the workflow runner a server detaches and each agent that runner starts
carry the runtime's ``KIROCREW_SPAWN_INSTANCE``. None of them is in a record, and
the agents have harness argv, so the live-owner spawn-instance condition is the one
thing that keeps them alive while their session's runtime serves. The orphaned
side of the same shape must still be reaped: once the runtime is gone, its
instance shelters nothing.

The roles mirror a tree read off a real pod: the runtime's launcher and its
recorded ``kiro-cli``, an ``mcp start-server`` broker and the server it runs, the
server's detached runner, and the agent the runner started. The systemd user
manager is the subreaper an orphan reparents to. Each role gets a pid no OS can
allocate, because the reconciler skips its own pid and a small literal can be the
pytest worker's. Only the worker is an unrecorded harness, so it is the only
candidate the arm can kill. The broker is the orphan MCP sweep's
to reap, and ``test_orphan_mcp_subtree.py`` covers that.
"""

from __future__ import annotations

import pytest
from runtime_reconcile_helpers import _reconciler, _refusals

from kiro_crew.testing.ids import unallocatable_pids

(
    _USER_MANAGER,
    _LAUNCHER,
    _AGENT,
    _BROKER,
    _SERVER,
    _RUNNER,
    _WORKER,
    _OTHER_RUNTIME,
) = unallocatable_pids(8)

_UNDER_SERVER = {
    _USER_MANAGER: (1, 0),
    _LAUNCHER: (1, 0),
    _AGENT: (_LAUNCHER, 0),
    _BROKER: (_AGENT, 0),
    _SERVER: (_BROKER, 0),
    _RUNNER: (_SERVER, 0),
    _WORKER: (_RUNNER, 0),
}
# The broker and its server have exited, and the detached runner reparented to the
# user manager, so the runtime is not an ancestor of the agent at all.
_SERVER_EXITED = {
    _USER_MANAGER: (1, 0),
    _LAUNCHER: (1, 0),
    _AGENT: (_LAUNCHER, 0),
    _RUNNER: (_USER_MANAGER, 0),
    _WORKER: (_RUNNER, 0),
}
_HARNESSES = {_LAUNCHER, _AGENT, _WORKER}
_SHARES_LIVE = "its spawn instance belongs to a live recorded runtime"


def _instances(table: dict[int, tuple[int, int]], instance: str = "spawn-a") -> dict[int, str]:
    return {pid: instance for pid in table if pid != _USER_MANAGER}


@pytest.mark.parametrize(
    "table", [_UNDER_SERVER, _SERVER_EXITED], ids=["under-server", "server-exited"]
)
def test_an_agent_a_live_sessions_mcp_server_started_is_never_killed(
    table: dict[int, tuple[int, int]],
) -> None:
    """The live runtime's instance shelters the agent, whoever its parent is now.

    Without the condition the agent meets every other one from the second pass on,
    which is the kill that ended workflow agents after 300 s. Six passes, so the
    agent is past the two-pass confirmation and the age floor many times over.
    """
    killed: list[int] = []
    audited: list[tuple[int, str, str]] = []
    rec = _reconciler(
        kernel=set(table) - {_USER_MANAGER},
        recorded={_LAUNCHER, _AGENT},
        managed=_HARNESSES,
        killed=killed,
        audited=audited,
        instances=_instances(table),
        table=table,
    )
    for _ in range(6):
        assert rec.run_once().killed == 0
    assert killed == []
    assert [pid for pid, _outcome, why in audited if why == _SHARES_LIVE] == [_WORKER]


@pytest.mark.parametrize("runtime", ["dead", "absent", "another-live"])
def test_an_orphaned_mcp_broker_tree_is_still_reaped(runtime: str) -> None:
    """With its runtime gone, the broker tree's agent is killed on the second pass.

    A dead runtime whose record is still on file, a record already gone, and a live
    runtime of another spawn are the same case: no live owned pid carries the
    tree's instance, so nothing shelters the agent. The broker, its server and the
    runner are not harnesses and are never signalled here.
    """
    table = {
        _USER_MANAGER: (1, 0),
        _BROKER: (_USER_MANAGER, 0),
        _SERVER: (_BROKER, 0),
        _RUNNER: (_SERVER, 0),
        _WORKER: (_RUNNER, 0),
    }
    instances = _instances(table)
    kernel = {_BROKER, _SERVER, _RUNNER, _WORKER}
    recorded: set[int] = set()
    if runtime == "dead":
        recorded = {_LAUNCHER, _AGENT}
    elif runtime == "another-live":
        table[_OTHER_RUNTIME] = (1, 0)
        instances[_OTHER_RUNTIME] = "spawn-b"
        kernel.add(_OTHER_RUNTIME)
        recorded = {_OTHER_RUNTIME}
    killed: list[int] = []
    audited: list[tuple[int, str, str]] = []
    rec = _reconciler(
        kernel=kernel,
        recorded=recorded,
        alive=kernel,
        managed={_WORKER, _OTHER_RUNTIME},
        killed=killed,
        audited=audited,
        instances=instances,
        table=table,
    )
    assert rec.run_once().killed == 0
    assert rec.run_once().killed == 1
    assert killed == [_WORKER]
    assert _SHARES_LIVE not in _refusals(audited)
    held = {pid for pid, _outcome, why in audited if why == "not a managed agent process"}
    assert held == {_BROKER, _SERVER, _RUNNER}
