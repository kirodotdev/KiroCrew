"""Session-open MCP mount check.

After ``session/new`` on a session-array harness, a broker stub the harness never
started must be logged at WARNING, written to the SEL as a security event, and
shown on the session's MCP report; a stub that did start must leave all three
silent; and "not seen yet" must never read as "missing".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from acp_frame_replay_harness import read_fixture, replay_frames

from kiro_crew.acp import client_mcp_array
from kiro_crew.acp import mcp_mount_check as mc
from kiro_crew.acp.mcp_session_report import NOT_MOUNTED_REASON, McpSessionReport
from kiro_crew.acp.types import ACP_BACKENDS_SESSION_MCP_ARRAY
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import socketsec, transport
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV, mint_stub_session_token
from kiro_crew.mcp_gateway.mount_query import SessionMounts, query_session_mounts
from kiro_crew.mcp_gateway.session_servers import attach_stub_session_token
from kiro_crew.testing.wait import default_timeout

_DROPPED = (
    Path(__file__).parent / "fixtures" / "acp_frames" / "goose" / "mcp-stdio-dropped-live.jsonl"
)


def _stub(name: str, server: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "command": "/usr/bin/python3",
        "args": ["-m", STUB_MODULE, "--server", server or name, "--agent", "kirocrew"],
        "env": [],
    }


def _array(token: str, *names: str) -> list[dict[str, Any]]:
    return attach_stub_session_token([_stub(n) for n in names], token)


class _FakeSEL:
    events: list[Any] = []

    def log(self, event: Any, **_kw: Any) -> None:
        _FakeSEL.events.append(event)


@pytest.fixture(autouse=True)
def _sel(_floor_monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """A fake SEL and an empty ledger per test, patched on the floor's own stack."""
    import kiro_crew.sel as sel

    _FakeSEL.events = []
    _floor_monkeypatch.setattr(sel, "SecurityEventLog", _FakeSEL)
    ledger = (gw._SESSION_MOUNTS, gw._SESSION_MOUNTS_OVERFLOWED, gw._SESSION_MOUNTS_EVICTED)
    for part in ledger:
        part.clear()
    yield _FakeSEL.events
    for part in ledger:
        part.clear()


def _seen(*names: str, since: float = 0.0):
    async def query(_socket: str, _token: str, _after: float = 0.0) -> SessionMounts:
        return SessionMounts(frozenset(names), since)

    return query


# ── roster ───────────────────────────────────────────────────────────────────


def test_roster_takes_only_tokened_broker_stubs() -> None:
    token = mint_stub_session_token()
    plain = {"name": "direct", "command": "node", "args": ["server.js"], "env": []}
    tokenless = _stub("tokenless")
    other = attach_stub_session_token([_stub("other")], mint_stub_session_token())[0]
    roster = mc.broker_stub_roster([*_array(token, "crew", "files"), plain, tokenless, other])
    assert roster == mc.Roster(token, {"crew": "crew", "files": "files"}, False)


def test_roster_keys_on_the_registered_server_name() -> None:
    token = mint_stub_session_token()
    servers = attach_stub_session_token([_stub("ext-crew", server="crew")], token)
    assert mc.broker_stub_roster(servers) == mc.Roster(token, {"crew": "ext-crew"}, False)


def test_roster_is_bounded_where_it_is_kept() -> None:
    from kiro_crew.mcp_gateway.mount_query import MOUNT_SERVERS_CAP

    token = mint_stub_session_token()
    names = [f"{i}-{'n' * 300}" for i in range(MOUNT_SERVERS_CAP + 1)]
    roster = mc.broker_stub_roster(_array(token, *names))
    assert len(roster.servers) == MOUNT_SERVERS_CAP
    assert roster.overflowed is True
    assert all(len(k) <= 128 and len(v) <= 128 for k, v in roster.servers.items())


@pytest.mark.asyncio
async def test_more_stubs_than_the_cap_is_unavailable_not_missing(_sel: list[Any]) -> None:
    from kiro_crew.mcp_gateway.mount_query import MOUNT_SERVERS_CAP

    token = mint_stub_session_token()
    servers = _array(token, *(f"s{i}" for i in range(MOUNT_SERVERS_CAP + 1)))
    report = McpSessionReport()
    report.begin_session(servers)
    got = await mc.run_mount_check(
        report,
        servers,
        socket_path="/s",
        backend="goose",
        session_id="sid",
        opened_at=time.time(),
        window=0,
        query=_seen(),
    )
    assert got == []
    assert report.payload()["failed"] == []
    assert _sel == []


# ── the check ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_stub_is_logged_audited_and_reported(
    caplog: pytest.LogCaptureFixture, _sel: list[Any]
) -> None:
    token = mint_stub_session_token()
    report = McpSessionReport()
    servers = _array(token, "crew", "broken")
    report.begin_session(servers)
    with caplog.at_level(logging.WARNING, logger=mc.__name__):
        got = await mc.run_mount_check(
            report,
            servers,
            socket_path="/s",
            backend="goose",
            session_id="sid-1",
            session_key="dashboard:chat-1",
            opened_at=time.time(),
            window=0,
            query=_seen("crew"),
        )
    assert got == ["broken"]
    payload = report.payload()
    assert payload["failed"] == ["broken"]
    assert payload["failures"]["broken"] == NOT_MOUNTED_REASON
    assert "broken" in report.problem_summary()
    assert any("never reached the MCP broker" in r.getMessage() for r in caplog.records)
    [event] = _sel
    assert event.event_type == "mcp_session_mount_missing"
    assert event.metadata["servers"] == ["broken"]
    assert event.metadata["session_id"] == "sid-1"
    assert event.caller_identity == "dashboard:chat-1"
    # Never the bearer token, an env value or a command line.
    assert token not in repr(event)
    assert "/usr/bin/python3" not in repr(event)


@pytest.mark.asyncio
async def test_every_stub_seen_is_silent(caplog: pytest.LogCaptureFixture, _sel: list[Any]) -> None:
    token = mint_stub_session_token()
    report = McpSessionReport()
    servers = _array(token, "crew", "files")
    report.begin_session(servers)
    with caplog.at_level(logging.WARNING, logger=mc.__name__):
        got = await mc.run_mount_check(
            report,
            servers,
            socket_path="/s",
            backend="goose",
            session_id="sid",
            opened_at=time.time(),
            window=5,
            query=_seen("crew", "files"),
        )
    assert got == []
    assert report.payload()["failed"] == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert _sel == []


@pytest.mark.asyncio
async def test_not_seen_yet_is_pending_not_missing(_sel: list[Any]) -> None:
    token = mint_stub_session_token()
    report = McpSessionReport()
    servers = _array(token, "crew")
    report.begin_session(servers)
    asks: list[float] = []
    second_ask = asyncio.Event()
    release = asyncio.Event()

    async def query(_s: str, _t: str, _after: float = 0.0) -> SessionMounts:
        asks.append(time.monotonic())
        if len(asks) == 1:
            return SessionMounts(frozenset(), 0.0)
        second_ask.set()
        await release.wait()
        return SessionMounts(frozenset({"crew"}), 0.0)

    task = asyncio.create_task(
        mc.run_mount_check(
            report,
            servers,
            socket_path="/s",
            backend="claude",
            session_id="sid",
            opened_at=time.time(),
            window=10,
            interval=0.01,
            query=query,
        )
    )
    waiter = asyncio.create_task(second_ask.wait())
    try:
        # Bounded, and an early exit of the check surfaces here instead of hanging.
        done, _ = await asyncio.wait(
            {task, waiter}, timeout=default_timeout(), return_when=asyncio.FIRST_COMPLETED
        )
        assert waiter in done, "the check never asked a second time"
        assert report.payload()["failed"] == []
        release.set()
        assert await asyncio.wait_for(task, default_timeout()) == []
    finally:
        release.set()
        for pending in (task, waiter):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, waiter, return_exceptions=True)
    assert _sel == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        pytest.param(lambda: _none_query, id="no-answer"),
        pytest.param(lambda: _seen(since=time.time() + 60), id="ledger-newer-than-session"),
    ],
)
async def test_a_broker_that_cannot_answer_proves_nothing(
    query: Any, caplog: pytest.LogCaptureFixture, _sel: list[Any]
) -> None:
    token = mint_stub_session_token()
    report = McpSessionReport()
    servers = _array(token, "crew")
    report.begin_session(servers)
    with caplog.at_level(logging.WARNING, logger=mc.__name__):
        got = await mc.run_mount_check(
            report,
            servers,
            socket_path="/s",
            backend="opencode",
            session_id="sid",
            opened_at=time.time(),
            window=0,
            query=query(),
        )
    assert got == []
    assert report.payload()["failed"] == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert _sel == []


async def _none_query(_s: str, _t: str, _after: float = 0.0) -> None:
    return None


@pytest.mark.asyncio
async def test_a_new_attempt_discards_the_old_answer(_sel: list[Any]) -> None:
    token = mint_stub_session_token()
    report = McpSessionReport()
    servers = _array(token, "crew")
    report.begin_session(servers)

    async def query(_s: str, _t: str, _after: float = 0.0) -> SessionMounts:
        report.begin_session([])  # a fallback attempt replaced the report
        return SessionMounts(frozenset(), 0.0)

    got = await mc.run_mount_check(
        report,
        servers,
        socket_path="/s",
        backend="goose",
        session_id="sid",
        opened_at=time.time(),
        window=0,
        query=query,
    )
    assert got == []
    assert report.payload()["failed"] == []
    assert _sel == []


@pytest.mark.asyncio
async def test_a_server_that_reported_itself_is_not_called_missing(_sel: list[Any]) -> None:
    token = mint_stub_session_token()
    report = McpSessionReport()
    servers = _array(token, "crew")
    report.begin_session(servers)
    report.record_event("mcp_server_initialized", "crew")
    got = await mc.run_mount_check(
        report,
        servers,
        socket_path="/s",
        backend="claude",
        session_id="sid",
        opened_at=time.time(),
        window=0,
        query=_seen(),
    )
    assert got == []
    assert report.payload()["ready"] == ["crew"]
    assert _sel == []


@pytest.mark.asyncio
async def test_a_long_server_name_matches_the_ledger(sock_path: str, _sel: list[Any]) -> None:
    long_name = "x" * 200
    token = mint_stub_session_token()
    servers = _array(token, long_name)
    report = McpSessionReport()
    report.begin_session(servers)
    async with _Daemon(sock_path) as daemon:
        await daemon.register(long_name, token)
        got = await mc.run_mount_check(
            report,
            servers,
            socket_path=sock_path,
            backend="goose",
            session_id="sid",
            opened_at=time.time(),
            window=0,
        )
    assert got == []
    assert _sel == []


@pytest.mark.asyncio
async def test_a_full_failed_bucket_still_logs_and_audits(
    caplog: pytest.LogCaptureFixture, _sel: list[Any]
) -> None:
    from kiro_crew.acp.mcp_session_report import BUCKET_CAP

    token = mint_stub_session_token()
    servers = _array(token, "crew")
    report = McpSessionReport()
    report.begin_session(servers)
    for i in range(BUCKET_CAP):
        report.record_pruned(f"other{i}", "boom")
    with caplog.at_level(logging.WARNING, logger=mc.__name__):
        got = await mc.run_mount_check(
            report,
            servers,
            socket_path="/s",
            backend="goose",
            session_id="sid",
            opened_at=time.time(),
            window=0,
            query=_seen(),
        )
    assert got == ["crew"]
    assert [e.metadata["servers"] for e in _sel] == [["crew"]]
    assert any("crew" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_session_closed_inside_the_window_gets_nothing(_sel: list[Any]) -> None:
    token = mint_stub_session_token()
    servers = _array(token, "crew")
    report = McpSessionReport()
    report.begin_session(servers)
    got = await mc.run_mount_check(
        report,
        servers,
        socket_path="/s",
        backend="goose",
        session_id="sid",
        opened_at=time.time(),
        window=0,
        query=_seen(),
        alive=lambda: False,
    )
    assert got == []
    assert report.payload()["failed"] == []
    assert _sel == []


def test_runtime_passes_send_time_and_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.acp import runtime as rt

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(rt, "schedule_mount_check", lambda r, s, **kw: calls.append(kw))
    fake = SimpleNamespace(
        _acp_backend="codex", _mcp_gateway_socket="/s", _session_queues={"sid": object()}
    )
    handle = SimpleNamespace(mcp_session_report=McpSessionReport)
    rt.AcpRuntime._start_mount_check(fake, handle, [], "sid", "k", 12.5)
    [kw] = calls
    assert kw["opened_at"] == 12.5
    assert kw["alive"]() is True
    fake._session_queues.clear()
    assert kw["alive"]() is False


# ── the daemon ledger ──────────────────────────────────────────────────────


def test_ledger_records_a_register_even_when_it_is_refused() -> None:
    token = mint_stub_session_token()
    gw._record_session_mount(
        {"type": "register", "server_name": "crew", "stub_session_token": token}
    )
    reply = gw._session_mounts_reply({"type": "session-mounts", "stub_session_token": token})
    assert reply["type"] == "session-mounts"
    assert reply["servers"] == ["crew"]
    assert reply["since"] == gw._MOUNT_LEDGER_SINCE
    # Held by digest: the bearer token itself is not kept.
    assert token not in gw._SESSION_MOUNTS
    assert gw._session_mounts(mint_stub_session_token()) == []
    assert gw._session_mounts_reply({"type": "session-mounts"})["type"] == (
        "session-mounts-rejected"
    )


def test_ledger_is_bounded() -> None:
    token = mint_stub_session_token()
    for i in range(gw._MAX_MOUNTS_PER_SESSION + 5):
        gw._record_session_mount({"server_name": f"s{i}", "stub_session_token": token})
    assert len(gw._session_mounts(token)) == gw._MAX_MOUNTS_PER_SESSION
    # A list that dropped a name cannot prove absence, and the reply says so.
    assert gw._session_mounts_reply({"stub_session_token": token})["complete"] is False
    for _ in range(gw._MAX_MOUNT_SESSIONS + 5):
        gw._record_session_mount(
            {"server_name": "x", "stub_session_token": mint_stub_session_token()}
        )
    assert len(gw._SESSION_MOUNTS) == gw._MAX_MOUNT_SESSIONS
    # The overflow mark leaves with its evicted session.
    assert gw._SESSION_MOUNTS_OVERFLOWED == set()


@pytest.mark.asyncio
async def test_an_overflowed_ledger_is_unavailable_not_missing(
    sock_path: str, _sel: list[Any]
) -> None:
    """65 stubs, all started: the 65th is past the ledger cap, never called missing."""
    token = mint_stub_session_token()
    names = [f"s{i}" for i in range(gw._MAX_MOUNTS_PER_SESSION + 1)]
    servers = _array(token, *names)
    for name in names:
        gw._record_session_mount({"server_name": name, "stub_session_token": token})
    report = McpSessionReport()
    report.begin_session(servers)
    async with _Daemon(sock_path):
        got = await mc.run_mount_check(
            report,
            servers,
            socket_path=sock_path,
            backend="goose",
            session_id="sid",
            opened_at=time.time(),
            window=0,
        )
    assert got == []
    assert report.payload()["failed"] == []
    assert _sel == []


@pytest.mark.asyncio
async def test_a_stub_from_before_a_respawn_does_not_count(sock_path: str, _sel: list[Any]) -> None:
    """The token survives a respawn; only this process's registrations prove a start."""
    token = mint_stub_session_token()
    servers = _array(token, "crew")
    gw._record_session_mount({"server_name": "crew", "stub_session_token": token})
    # The first process registered it a minute ago; the respawned one never does.
    gw._SESSION_MOUNTS[gw._mount_key(token)]["crew"] = time.time() - 60
    report = McpSessionReport()
    report.begin_session(servers)
    async with _Daemon(sock_path):
        got = await mc.run_mount_check(
            report,
            servers,
            socket_path=sock_path,
            backend="goose",
            session_id="sid",
            opened_at=time.time(),
            window=0,
        )
    assert got == ["crew"]
    assert [e.metadata["servers"] for e in _sel] == [["crew"]]


@pytest.mark.asyncio
async def test_a_full_ledger_still_proves_a_new_session_drop(
    sock_path: str, _sel: list[Any]
) -> None:
    """Steady state on a long-lived daemon: other sessions' evictions do not blind this one."""
    for _ in range(gw._MAX_MOUNT_SESSIONS + 10):
        gw._record_session_mount(
            {"server_name": "x", "stub_session_token": mint_stub_session_token()}
        )
    token = mint_stub_session_token()
    servers = _array(token, "crew", "files")
    report = McpSessionReport()
    report.begin_session(servers)
    opened = time.time()
    async with _Daemon(sock_path) as daemon:
        await daemon.register("crew", token)  # evicts the oldest OTHER session
        got = await mc.run_mount_check(
            report,
            servers,
            socket_path=sock_path,
            backend="goose",
            session_id="sid",
            opened_at=opened,
            window=0,
        )
    assert got == ["files"]
    assert [e.metadata["servers"] for e in _sel] == [["files"]]


@pytest.mark.asyncio
async def test_an_eviction_after_open_is_unavailable_not_missing(
    sock_path: str, _sel: list[Any]
) -> None:
    """512 other sessions push this one's started stub out of the ledger."""
    token = mint_stub_session_token()
    servers = _array(token, "crew", "files")
    report = McpSessionReport()
    report.begin_session(servers)
    opened = time.time()
    gw._record_session_mount({"server_name": "crew", "stub_session_token": token})
    for _ in range(gw._MAX_MOUNT_SESSIONS):
        gw._record_session_mount(
            {"server_name": "x", "stub_session_token": mint_stub_session_token()}
        )
    assert gw._session_mounts(token) == []  # evicted
    async with _Daemon(sock_path):
        got = await mc.run_mount_check(
            report,
            servers,
            socket_path=sock_path,
            backend="goose",
            session_id="sid",
            opened_at=opened,
            window=0,
        )
    assert got == []
    assert report.payload()["failed"] == []
    assert _sel == []


# ── end to end over a real daemon socket ────────────────────────────────────


class _Daemon:
    """``gatewayd._handle_connection`` serving a temporary owner-only socket."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.server: Any = None

    async def __aenter__(self) -> "_Daemon":
        async def handle(reader: Any, writer: Any) -> None:
            try:
                await gw._handle_connection(
                    reader,
                    writer,
                    pool=None,
                    resolver=object(),
                    socket_path=Path(self.path),
                    hot_keys=None,
                )
            finally:
                writer.close()

        self.server = await asyncio.start_unix_server(handle, path=self.path)
        os.chmod(self.path, 0o600)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def register(self, server: str, token: str) -> None:
        """What a stub sends first when its harness starts it."""
        reader, writer = await transport.connect(self.path)
        frame = {
            "type": "register",
            "stub_uuid": str(uuid.uuid4()),
            "server_name": server,
            "stub_session_token": token,
        }
        writer.write((json.dumps(frame) + "\n").encode())
        await writer.drain()
        await reader.readline()
        writer.close()


@pytest.fixture
def sock_path(monkeypatch: pytest.MonkeyPatch) -> Any:
    if not hasattr(asyncio, "start_unix_server"):
        pytest.skip("unix sockets only")
    monkeypatch.setattr(socketsec, "PEER_IDENTITY_SUPPORTED", True)
    monkeypatch.setattr(socketsec, "check_peer_is_self", lambda _w: socketsec.PeerCredResult.MATCH)
    with tempfile.TemporaryDirectory(prefix="mc-") as d:
        yield os.path.join(d, "g.sock")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_SESSION_MCP_ARRAY))
async def test_unstartable_element_fires_and_startable_one_is_silent(
    backend: str, sock_path: str, _sel: list[Any]
) -> None:
    token = mint_stub_session_token()
    servers = _array(token, "crew", "cannot-start")
    report = McpSessionReport()
    report.begin_session(servers)
    opened = time.time()
    async with _Daemon(sock_path) as daemon:
        await daemon.register("crew", token)  # the harness started this one
        got = await mc.run_mount_check(
            report,
            servers,
            socket_path=sock_path,
            backend=backend,
            session_id="sid",
            opened_at=opened,
            window=0.2,
            interval=0.05,
        )
    assert got == ["cannot-start"]
    assert report.payload()["failed"] == ["cannot-start"]
    assert [e.metadata["servers"] for e in _sel] == [["cannot-start"]]


@pytest.mark.asyncio
async def test_query_answers_none_without_a_daemon(tmp_path: Path) -> None:
    assert await query_session_mounts(str(tmp_path / "absent.sock"), "t") is None


@pytest.mark.asyncio
async def test_goose_dropped_capture_replays_into_the_observation(
    sock_path: str, caplog: pytest.LogCaptureFixture, _sel: list[Any]
) -> None:
    """The live goose capture: session/new succeeds with the element dropped."""
    _meta, frames = read_fixture(_DROPPED)
    [created] = [e for e in replay_frames(frames) if e.get("session_id")]
    session_id = created["session_id"]
    token = mint_stub_session_token()
    servers = _array(token, "crew-probe")
    report = McpSessionReport()
    report.begin_session(servers)
    async with _Daemon(sock_path):
        with caplog.at_level(logging.WARNING, logger=mc.__name__):
            await mc.run_mount_check(
                report,
                servers,
                socket_path=sock_path,
                backend="goose",
                session_id=session_id,
                opened_at=time.time(),
                window=0.1,
                interval=0.05,
            )
    [event] = _sel
    assert event.metadata == {
        "backend": "goose",
        "session_id": "goose-session-1",
        "servers": ["crew-probe"],
        "window_secs": mc.MOUNT_WINDOW_SECS,
        "mechanism": "gatewayd_session_mounts",
    }
    assert report.payload()["failed"] == ["crew-probe"]
    assert any("goose-session-1" in r.getMessage() for r in caplog.records)


# ── wiring ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", ["kiro", *sorted(ACP_BACKENDS_SESSION_MCP_ARRAY)])
def test_client_starts_the_check_only_for_session_array_backends(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        client_mcp_array, "schedule_mount_check", lambda r, s, **kw: calls.append(kw)
    )
    fake = SimpleNamespace(
        backend=backend,
        _session_id="sid",
        _mcp_report=McpSessionReport(),
        _mcp_mount_servers=[],
        _mcp_mount_opened_at=1.0,
        _mcp_gateway_socket="/s",
        _session_key="k",
    )
    client_mcp_array._start_mount_check(fake)
    assert bool(calls) == (backend in ACP_BACKENDS_SESSION_MCP_ARRAY)


def test_stub_token_env_name_is_the_one_stubs_read() -> None:
    assert mc.STUB_SESSION_TOKEN_ENV == STUB_SESSION_TOKEN_ENV
