"""The gateway manager's failure paths: what it does when the daemon, the transport or the OS lets it down.

The lifecycle, repair and liveness suites walk the happy paths and the election. This module covers the
guards around them: the three control-frame clients' transport failures, the cleanup after a start that
cannot confirm its daemon, the optional daemon flags, the host-memory sample and the SIGTERM to SIGKILL
escalation. Only the transport hop, the process launcher or a collaborator is doubled; every test asserts
what the manager returns, logs or does next.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew.code_fingerprint import code_fingerprint
from kiro_crew.mcp_gateway import manager as mgr
from kiro_crew.testing.ids import UNALLOCATABLE_PID


def _manager(tmp_path, **spec):
    return mgr.GatewayManager(mgr.GatewaySpec(socket_path=tmp_path / "gw.sock", **spec))


def _double_transport(
    monkeypatch, *, reply=None, connect_error=None, drain_error=None, close_error=None
):
    """Point ``transport.connect`` at an in-process pair (a real ``StreamReader``); return the writer."""
    writer = SimpleNamespace(
        write=Mock(),
        close=Mock(),
        drain=AsyncMock(side_effect=drain_error),
        wait_closed=AsyncMock(side_effect=close_error),
    )

    async def connect(_path, **_kwargs):
        if connect_error is not None:
            raise connect_error
        reader = asyncio.StreamReader()
        if reply is not None:
            reader.feed_data(reply)
        reader.feed_eof()
        return reader, writer

    monkeypatch.setattr(mgr.transport, "connect", connect)
    return writer


# (client call, a well-formed reply to it, what it returns for that reply, what it returns when nothing answers)
_CLIENTS = [
    pytest.param(lambda m: m._ping_raw(), b'{"type":"pong"}\n', {"type": "pong"}, None, id="ping"),
    pytest.param(lambda m: m.stats(), b'{"type":"stats"}\n', {"type": "stats"}, {}, id="stats"),
    pytest.param(
        lambda m: m._control_roundtrip({"type": "x"}),
        b'{"type":"ok"}\n',
        {"type": "ok"},
        None,
        id="control",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("call", "reply", "answer", "silent"), _CLIENTS)
@pytest.mark.parametrize(
    "close_error", [None, RuntimeError("already closed")], ids=["closes", "close-raises"]
)
async def test_a_well_formed_reply_is_returned_and_the_connection_closed(
    monkeypatch, tmp_path, call, reply, answer, silent, close_error
):
    writer = _double_transport(monkeypatch, reply=reply, close_error=close_error)

    assert await call(_manager(tmp_path)) == answer
    writer.close.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(("call", "reply", "answer", "silent"), _CLIENTS)
@pytest.mark.parametrize(
    "double",
    [
        pytest.param({"connect_error": ConnectionRefusedError("refused")}, id="connect-refused"),
        pytest.param({"connect_error": asyncio.TimeoutError()}, id="connect-times-out"),
        pytest.param({"drain_error": ConnectionResetError("reset")}, id="reset-while-sending"),
        pytest.param({"drain_error": asyncio.TimeoutError()}, id="send-times-out"),
        pytest.param({"reply": b'{"type":"pong"'}, id="cut-before-the-newline"),
        pytest.param({"reply": b"\xff\xfe\n"}, id="not-utf8"),
        pytest.param({"reply": b"not json\n"}, id="not-json"),
        pytest.param({"reply": b"[1]\n"}, id="not-an-object"),
    ],
)
async def test_a_daemon_that_cannot_answer_gives_each_client_its_empty_value(
    monkeypatch, tmp_path, double, call, reply, answer, silent
):
    _double_transport(monkeypatch, **double)

    assert await call(_manager(tmp_path)) == silent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "snapshot"),
    [
        (b'{"type":"stats","backends":2}\n', {"type": "stats", "backends": 2}),
        (b'{"type":"pong"}\n', {}),
    ],
    ids=["stats-frame", "any-other-frame"],
)
async def test_stats_returns_only_a_stats_frame(monkeypatch, tmp_path, reply, snapshot):
    writer = _double_transport(monkeypatch, reply=reply)

    assert await _manager(tmp_path).stats() == snapshot
    writer.write.assert_called_once_with(b'{"type":"stats"}\n')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "alive"), [(b'{"type":"pong"}\n', True), (b'{"type":"nope"}\n', False), (None, False)]
)
async def test_ping_is_true_only_when_the_daemon_answers_a_pong(
    monkeypatch, tmp_path, reply, alive
):
    _double_transport(monkeypatch, reply=reply)

    assert await _manager(tmp_path).ping() is alive


# ── start(): the guards, and the two ways a daemon this manager did not spawn ends up adopted ──


@pytest.mark.asyncio
async def test_start_is_a_no_op_while_the_daemon_runs_or_is_adopted(monkeypatch, tmp_path):
    m = _manager(tmp_path)
    looked = AsyncMock(return_value=None)
    monkeypatch.setattr(m, "_ping_payload", looked)
    assert m.socket_path == tmp_path / "gw.sock"

    m._process = SimpleNamespace(returncode=None)
    assert m.is_running is True
    assert await m.start() is True

    m._process, m._adopted = None, True
    assert await m.start() is True
    looked.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stale_incumbent_that_will_not_yield_is_adopted_once_the_ask_budget_is_spent(
    monkeypatch, tmp_path
):
    m = _manager(tmp_path)
    m._stand_downs_issued = mgr._MAX_STAND_DOWN_REQUESTS
    stale = {"type": "pong", "fingerprint": "an-older-revision"}
    monkeypatch.setattr(m, "_ping_payload", AsyncMock(return_value=stale))
    monkeypatch.setattr(m, "_run_watchdog", AsyncMock())

    assert await m.start() is True
    assert (m._adopted, m._process) == (True, None)
    assert m._stand_downs_issued == mgr._MAX_STAND_DOWN_REQUESTS, "no further ask is made"
    await m.shutdown()


@pytest.mark.asyncio
async def test_a_spawn_that_lost_the_lock_to_a_fit_daemon_adopts_that_daemon(monkeypatch, tmp_path):
    m = _manager(tmp_path)
    fit = {
        "type": "pong",
        "fingerprint": code_fingerprint(),
    }  # our daemon exited rc=0; this one answered
    spawn = AsyncMock(return_value=fit)
    monkeypatch.setattr(m, "_ping_payload", AsyncMock(return_value=None))
    monkeypatch.setattr(m, "_spawn_and_confirm", spawn)
    monkeypatch.setattr(m, "_run_watchdog", AsyncMock())

    assert await m.start() is True
    assert m._adopted is True
    spawn.assert_awaited_once()
    await m.shutdown()


# ── _spawn_and_confirm(): a daemon that cannot be confirmed is terminated, not leaked ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stopping", "socket_up", "waits", "pings"),
    [
        pytest.param(True, True, 0, 0, id="shutdown-raced-the-spawn"),
        pytest.param(False, False, 1, 0, id="socket-never-appears"),
        pytest.param(False, True, 1, 1, id="daemon-never-pongs"),
    ],
)
async def test_a_start_that_cannot_confirm_its_daemon_terminates_it(
    monkeypatch, tmp_path, stopping, socket_up, waits, pings
):
    m = _manager(tmp_path)

    async def spawn_once():
        m._stopping = stopping

    terminate, waited, pinged = (
        AsyncMock(),
        AsyncMock(return_value=socket_up),
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(mgr.transport, "prepare_dir", lambda _path: None)
    monkeypatch.setattr(m, "_clear_stale_socket", AsyncMock())
    monkeypatch.setattr(m, "_spawn_once", spawn_once)
    monkeypatch.setattr(m, "_wait_for_socket", waited)
    monkeypatch.setattr(m, "_ping_with_escalation", pinged)
    monkeypatch.setattr(m, "_terminate_process", terminate)

    assert await m._spawn_and_confirm() is None
    terminate.assert_awaited_once_with(grace_secs=mgr._SHUTDOWN_GRACE_SECS)
    assert (waited.await_count, pinged.await_count) == (waits, pings)


# ── _spawn_once(): the daemon's argv, and the log it writes to ──


async def _spawn_argv(monkeypatch, tmp_path, *, watch=(), **spec):
    """Run ``_spawn_once`` against a fake launcher; return the argv it launched and the manager."""
    launched = []

    async def launch(*argv, **_kwargs):
        launched.append(list(argv))
        return SimpleNamespace(returncode=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    m = _manager(tmp_path, **spec)
    monkeypatch.setattr(m, "_gatewayd_log_path", lambda: tmp_path / "logs" / "mcp-gatewayd.stdout")
    monkeypatch.setattr(m, "_host_available_mb", lambda: 2048.0)
    monkeypatch.setattr(m, "_credential_watch_paths", lambda: list(watch))
    await m._spawn_once()
    return launched[0], m


def _flag_values(argv, flag):
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == flag]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prewarm", "watch"),
    [
        pytest.param(0, [], id="defaults"),
        pytest.param(3, [Path("/run/a"), Path("/run/b")], id="configured"),
    ],
)
async def test_optional_daemon_flags_appear_only_when_configured(
    monkeypatch, tmp_path, prewarm, watch
):
    argv, _m = await _spawn_argv(monkeypatch, tmp_path, watch=watch, prewarm_count=prewarm)

    assert _flag_values(argv, "--prewarm-count") == ([str(prewarm)] if prewarm else [])
    assert _flag_values(argv, "--credential-watch-path") == [str(p) for p in watch]
    assert _flag_values(argv, "--host-available-mb") == ["2048.0"]


@pytest.mark.asyncio
async def test_a_log_that_cannot_be_locked_down_is_warned_about_and_the_spawn_goes_on(
    monkeypatch, tmp_path, caplog
):
    real_chmod = Path.chmod

    def chmod(self, mode, **kwargs):
        if self == tmp_path / "logs":
            raise PermissionError("not ours to change")
        real_chmod(self, mode, **kwargs)

    def deny(_path):
        raise OSError("acl refused")

    monkeypatch.setattr(Path, "chmod", chmod)
    monkeypatch.setattr(mgr.platform_compat, "restrict_to_owner", deny)

    with caplog.at_level(logging.WARNING, logger=mgr.logger.name):
        _argv, m = await _spawn_argv(monkeypatch, tmp_path)

    assert m.is_running is True
    assert "could not restrict gatewayd log" in caplog.text
    assert "acl refused" in caplog.text


@pytest.mark.parametrize(
    ("sample", "expected"), [(2.0, 2048.0), (-1.0, -1.0), (None, -1.0), (OSError("no /proc"), -1.0)]
)
def test_host_memory_is_reported_in_mib_and_anything_unusable_is_minus_one(
    monkeypatch, sample, expected
):
    def probe():
        if isinstance(sample, Exception):
            raise sample
        return SimpleNamespace(available_gb=sample)

    monkeypatch.setattr("kiro_crew.resource_status.probe", probe)

    assert mgr.GatewayManager._host_available_mb() == expected


# ── _request_stand_down(): what the frame claims, and what a spent budget decides ──


@pytest.mark.asyncio
async def test_a_stand_down_for_an_orphaned_daemon_says_so(monkeypatch, tmp_path):
    m = _manager(tmp_path)
    roundtrip = AsyncMock(return_value=None)
    monkeypatch.setattr(m, "_control_roundtrip", roundtrip)

    assert await m._request_stand_down(["CORE", "AWS"], orphaned=True) == mgr._REFUSED
    roundtrip.assert_awaited_once_with(
        {"type": "stand-down", "need": ["AWS", "CORE"], "orphaned": True}
    )
    assert m._stand_downs_issued == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("lock_free", "outcome"), [(True, mgr._RELEASED), (False, mgr._DRAINING)])
async def test_a_spent_stand_down_budget_is_judged_by_one_last_lock_check(
    monkeypatch, tmp_path, lock_free, outcome
):
    m = _manager(tmp_path)
    monkeypatch.setattr(m, "_control_roundtrip", AsyncMock(return_value={"type": "standing-down"}))
    monkeypatch.setattr(mgr, "_SHUTDOWN_GRACE_SECS", 0.0)
    checked = Mock(return_value=lock_free)
    monkeypatch.setattr(mgr.transport, "singleton_lock_free", checked)

    assert await m._request_stand_down(["CORE"]) == outcome
    checked.assert_called_once_with(m.socket_path)  # the budget was spent already: no polling


# ── _terminate_process(): SIGTERM, then SIGKILL, then the backends it orphaned ──


def _proc(waits, term_error=None, kill_error=None):
    """A daemon handle whose successive ``wait()`` calls either return or run out of time."""
    outcomes = iter(waits)

    async def wait():
        if next(outcomes) == "timeout":
            raise asyncio.TimeoutError
        return 0

    return SimpleNamespace(
        pid=UNALLOCATABLE_PID,
        returncode=None,
        wait=wait,
        send_signal=Mock(side_effect=term_error),
        kill=Mock(side_effect=kill_error),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("waits", "term_error", "kill_error", "killed", "reaped", "gave_up"),
    [
        pytest.param([], ProcessLookupError(), None, False, False, False, id="gone-at-sigterm"),
        pytest.param(["timeout", "exit"], None, None, True, True, False, id="sigkill-lands"),
        pytest.param(["timeout", "timeout"], None, None, True, True, True, id="sigkill-unheard"),
        pytest.param(
            ["timeout"], None, ProcessLookupError(), True, False, False, id="gone-at-sigkill"
        ),
    ],
)
async def test_terminate_escalates_to_sigkill_and_reaps_only_what_a_sigkill_orphans(
    monkeypatch, tmp_path, caplog, waits, term_error, kill_error, killed, reaped, gave_up
):
    m = _manager(tmp_path)
    proc, reap = _proc(waits, term_error, kill_error), AsyncMock()
    monkeypatch.setattr(m, "_reap_orphaned_backends", reap)
    m._process = proc

    with caplog.at_level(logging.WARNING, logger=mgr.logger.name):
        await m._terminate_process(grace_secs=1.0)

    assert m._process is None
    assert proc.kill.called is killed
    assert reap.await_count == int(reaped)
    assert ("SIGKILL also timed out" in caplog.text) is gave_up
