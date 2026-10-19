"""End-to-end Signal channel tests against a FAKE ``signal-cli`` child.

``test/fixtures/channels/signal/fake_signal_cli.py`` speaks newline JSON-RPC on
stdio like the real binary: it answers the readiness probe, emits ``receive``
notifications, and records every request it gets. These tests boot the channel
through the real entry point (``maybe_start_signal``), so the real
``SignalRpcPeer`` framing, ``SignalClient``, ``SignalTransport``,
``SignalDispatcher`` and ``SignalRenderer`` all run; only the agent turn itself
(``drive_turn``) is replaced, by a stand-in that writes an answer through the
turn's own renderer.

Every wait is on a signal the code emits (a counted ``receive`` completion, the
answer's delivery), under ``await_until``'s lost-run bound -- never a sleep.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_signal_dispatch import MirrorStore

from kiro_crew.signal_cli import gateway as gw
from kiro_crew.signal_cli import transport as transport_mod
from kiro_crew.signal_cli.bridge_path import ENV_OVERRIDE
from kiro_crew.signal_cli.client import SignalClient
from kiro_crew.signal_cli.rpc import RpcTransportError
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake binary is a POSIX wrapper")

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "channels" / "signal"
FAKE = FIXTURES / "fake_signal_cli.py"

ACCOUNT = "+447700900001"
OWNER = "+447700900002"
STRANGER = "+447700900009"


def _receive(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["params"]


class _Sessions(MirrorStore):
    """The SessionManager slice the dispatcher touches before ``drive_turn``."""

    def is_busy(self, key: str) -> bool:
        return False

    def max_generation(self, *_args: object, **_kwargs: object) -> int:
        return 0

    def list_sessions(self) -> list[str]:
        return []


class _State:
    def __init__(self) -> None:
        self.signal_connected = False
        self.signal_connect_error = ""
        self.registered: list[Any] = []

    def register_channel_transport(self, transport: Any) -> None:
        self.registered.append(transport)


def _orch(*, allowed: list[str], account: str = ACCOUNT) -> Any:
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    # Duck-typed section: the shape SignalConfig carries.
    object.__setattr__(
        cfg,
        "signal",
        SimpleNamespace(
            enabled=True,
            account=account,
            allowed_numbers=allowed,
            soft_threshold_pct=80,
            hard_threshold_pct=95,
        ),
    )
    return SimpleNamespace(
        _signal_enabled=True,
        _cfg=cfg,
        _approval_mode=None,
        sessions=_Sessions(),
        ctx_builder=object(),
        conv_log=None,
        dashboard_state=_State(),
    )


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Install the fake as the operator-chosen signal-cli, with its I/O files."""
    staged = tmp_path / "signal-cli.staged"
    staged.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n',
        encoding="utf-8",
    )
    staged.chmod(staged.stat().st_mode | stat.S_IXUSR)
    binary = tmp_path / "signal-cli"
    staged.rename(binary)  # appears at its public path already executable
    log = tmp_path / "requests.jsonl"
    notify = tmp_path / "notify.json"
    notify.write_text("[]", encoding="utf-8")
    monkeypatch.setenv(ENV_OVERRIDE, str(binary))
    monkeypatch.setenv("FAKE_SIGNAL_LOG", str(log))
    monkeypatch.setenv("FAKE_SIGNAL_NOTIFY", str(notify))
    monkeypatch.delenv("FAKE_SIGNAL_MODE", raising=False)
    return {"log": log, "notify": notify}


@pytest.fixture
def receipts(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Count completed ``SignalTransport.receive`` calls, so a test can wait for N."""
    box: dict[str, Any] = {"done": 0, "target": 0, "event": None}
    original = transport_mod.SignalTransport.receive

    async def counting(self: Any, raw: Any) -> None:
        try:
            await original(self, raw)
        finally:
            box["done"] += 1
            if box["event"] is not None and box["done"] >= box["target"]:
                box["event"].set()

    monkeypatch.setattr(transport_mod.SignalTransport, "receive", counting)
    return box


@pytest.fixture
def turns(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace the agent turn with one that answers through the turn's renderer."""
    seen: list[Any] = []

    async def _yes(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def fake_drive_turn(turn: Any, **_kwargs: Any) -> None:
        seen.append(turn)
        renderer = turn.renderer
        await renderer.on_turn_start()
        await renderer.on_text_chunk(f"**You said:** {turn.user_text}")
        await renderer.on_done("end_turn")
        await renderer.close()

    monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.inbound_permitted", _yes)
    monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.drive_turn", fake_drive_turn)
    return seen


def _requests(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def _sends(log: Path) -> list[dict[str, Any]]:
    return [r["params"] for r in _requests(log) if r.get("method") == "send"]


async def _boot(
    orch: Any, receipts: dict[str, Any], expected: int, *, timeout: float | None = None
) -> Any:
    """Boot the channel and wait for *expected* receives; the caller closes the client.

    A wait that fails closes the client (and so its signal-cli child) itself,
    because the caller's ``try``/``finally`` has not been entered yet.
    """
    receipts["event"] = asyncio.Event()
    receipts["target"] = expected
    client = await gw.maybe_start_signal(orch)
    assert client is not None, orch.dashboard_state.signal_connect_error
    try:
        await await_until(
            receipts["event"], timeout=timeout, what=f"{expected} receive notification(s) handled"
        )
    except BaseException:
        await client.close()
        raise
    return client


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every signal-cli child the client spawns, as its ``asyncio`` process."""
    from kiro_crew.signal_cli import client as client_mod

    spawned: list[Any] = []

    class _RecordingPeer(client_mod.SignalRpcPeer):
        async def start(self) -> None:
            await super().start()
            spawned.append(self._proc)

    monkeypatch.setattr(client_mod, "SignalRpcPeer", _RecordingPeer)
    return spawned


async def _reap(children: list[Any]) -> None:
    """Kill any child the code under test left running, so a red run leaks nothing."""
    for proc in children:
        if proc.returncode is None:
            proc.kill()
            await await_until(asyncio.ensure_future(proc.wait()), what="the leaked child exits")


def _probed(log: Path, count: int) -> Any:
    def _seen() -> bool:
        return sum(r.get("method") == "version" for r in _requests(log)) >= count

    return _seen


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_a_dm_from_an_allowed_sender_is_answered_to_that_sender(
        self, fake_cli: dict[str, Path], receipts: dict[str, Any], turns: list[Any]
    ) -> None:
        fake_cli["notify"].write_text(json.dumps([_receive("receive_dm")]), encoding="utf-8")
        orch = _orch(allowed=[OWNER])
        client = await _boot(orch, receipts, 1)
        try:
            assert orch.dashboard_state.signal_connected is True
            assert orch.dashboard_state.signal_connect_error == ""
            assert [t.channel_type for t in orch.dashboard_state.registered] == ["signal"]
            assert len(turns) == 1 and turns[0].session_key.startswith("signal:")
            methods = [r["method"] for r in _requests(fake_cli["log"])]
            assert methods[0] == "version"
            assert _sends(fake_cli["log"]) == [
                {"recipient": [OWNER], "message": "You said: what's on my calendar?"}
            ]
            # The receipt names the message being answered, by its timestamp.
            receipt = [
                r["params"] for r in _requests(fake_cli["log"]) if r["method"] == "sendReceipt"
            ]
            assert receipt == [
                {"recipient": OWNER, "targetTimestamp": [1700000000100], "type": "read"}
            ]
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_note_to_self_is_answered_in_note_to_self(
        self, fake_cli: dict[str, Path], receipts: dict[str, Any], turns: list[Any]
    ) -> None:
        fake_cli["notify"].write_text(
            json.dumps([_receive("receive_note_to_self")]), encoding="utf-8"
        )
        orch = _orch(allowed=[ACCOUNT])
        client = await _boot(orch, receipts, 1)
        try:
            assert _sends(fake_cli["log"]) == [
                {"message": "You said: remind me to call mum", "noteToSelf": True}
            ]
            # No typing indicator or read receipt is sent to oneself; the status
            # reactions go on the owner's own message, in Note to Self.
            methods = {r["method"] for r in _requests(fake_cli["log"])}
            assert methods == {"version", "send", "sendReaction"}
            reactions = [
                r["params"] for r in _requests(fake_cli["log"]) if r["method"] == "sendReaction"
            ]
            assert [r["emoji"] for r in reactions] == [
                "\N{HOURGLASS WITH FLOWING SAND}",
                "\N{WHITE HEAVY CHECK MARK}",
            ]
            assert all(
                r["noteToSelf"] is True
                and r["targetAuthor"] == ACCOUNT
                and r["targetTimestamp"] == 1700000000200
                and "recipient" not in r
                for r in reactions
            )
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_groups_strangers_and_syncs_to_others_are_refused_silently(
        self, fake_cli: dict[str, Path], receipts: dict[str, Any], turns: list[Any]
    ) -> None:
        stranger = _receive("receive_dm")
        stranger["envelope"]["source"] = STRANGER
        stranger["envelope"]["sourceNumber"] = STRANGER
        stranger["envelope"]["sourceUuid"] = "cccccccc-3333-4333-8333-333333333333"
        stranger["envelope"]["timestamp"] = stranger["envelope"]["dataMessage"]["timestamp"] = 5
        fake_cli["notify"].write_text(
            json.dumps(
                [
                    _receive("receive_group"),
                    stranger,
                    _receive("receive_sync_to_other"),
                    _receive("receive_receipt"),
                ]
            ),
            encoding="utf-8",
        )
        orch = _orch(allowed=[OWNER, ACCOUNT])
        client = await _boot(orch, receipts, 4)
        try:
            assert turns == []
            assert [r["method"] for r in _requests(fake_cli["log"])] == ["version"]
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_a_failed_boot_wait_still_closes_the_child(
        self,
        fake_cli: dict[str, Path],
        receipts: dict[str, Any],
        turns: list[Any],
        children: list[Any],
    ) -> None:
        # Nothing is queued, so the receive the wait counts on never comes: the
        # child must be closed anyway, or it outlives the failed test.
        try:
            with pytest.raises(AssertionError, match="receive notification"):
                await _boot(_orch(allowed=[OWNER]), receipts, 1, timeout=0)
            assert len(children) == 1
            assert children[0].returncode is not None, "the child outlived the failed wait"
        finally:
            await _reap(children)

    @pytest.mark.asyncio
    async def test_the_transport_delivers_a_proactive_send(
        self, fake_cli: dict[str, Path], receipts: dict[str, Any], turns: list[Any]
    ) -> None:
        # The generic outbound path (send_message MCP tool, notifications) goes
        # through the registered transport's may_send_to + send_message.
        orch = _orch(allowed=[OWNER])
        client = await gw.maybe_start_signal(orch)
        assert client is not None
        try:
            transport = orch.dashboard_state.registered[0]
            assert transport.may_send_to(OWNER) and not transport.may_send_to(STRANGER)
            mid = await transport.send_message(OWNER, "cron finished")
            assert mid.isdigit()
            assert _sends(fake_cli["log"]) == [{"recipient": [OWNER], "message": "cron finished"}]
        finally:
            await client.close()


class TestStartFailure:
    @pytest.mark.asyncio
    async def test_an_unregistered_account_reports_signal_clis_own_reason_and_retries(
        self, fake_cli: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_SIGNAL_MODE", "unregistered")
        orch = _orch(allowed=[OWNER])
        client = await gw.maybe_start_signal(orch)
        assert client is not None
        try:
            state = orch.dashboard_state
            assert state.signal_connected is False
            assert (
                state.signal_connect_error
                == f"signal-cli exited: User {ACCOUNT} is not registered."
            )
            # Registered anyway, so a send reaches Signal once it comes up.
            assert [t.channel_type for t in state.registered] == ["signal"]
            assert client._reconnect_task is not None
        finally:
            await client.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("account", ["+44 7700 900001", "0044 7700 900001", 447700900001])
    async def test_a_formatted_account_reaches_signal_cli_as_bare_e164(
        self, fake_cli: dict[str, Path], monkeypatch: pytest.MonkeyPatch, account: Any
    ) -> None:
        # The fake names the -a it was given in its "not registered" line, so
        # the reason quotes exactly what reached signal-cli's argv.
        monkeypatch.setenv("FAKE_SIGNAL_MODE", "unregistered")
        orch = _orch(allowed=[OWNER], account=account)
        client = await gw.maybe_start_signal(orch)
        assert client is not None
        try:
            assert client.account == ACCOUNT
            assert orch.dashboard_state.signal_connect_error == (
                f"signal-cli exited: User {ACCOUNT} is not registered."
            )
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_a_failed_boot_comes_up_by_itself_and_then_delivers(
        self, fake_cli: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.signal_cli import client as client_mod

        delays: list[float] = []
        released = asyncio.Event()

        async def gated_sleep(delay: float) -> None:
            # The backoff waits on the test, not the wall clock.
            delays.append(delay)
            await released.wait()

        monkeypatch.setattr(client_mod, "_sleep", gated_sleep)
        monkeypatch.setenv("FAKE_SIGNAL_MODE", "unregistered")
        orch = _orch(allowed=[OWNER])
        client = await gw.maybe_start_signal(orch)
        assert client is not None
        try:
            transport = orch.dashboard_state.registered[0]
            with pytest.raises(RpcTransportError):
                await transport.send_message(OWNER, "too early")
            # signal-cli becomes usable (the account got linked), then the
            # backoff's wait ends.
            monkeypatch.delenv("FAKE_SIGNAL_MODE")
            released.set()
            await await_until(client.ready, what="the background retry connects")
            assert delays == [2.0]
            assert orch.dashboard_state.signal_connected is True
            assert orch.dashboard_state.signal_connect_error == ""
            await transport.send_message(OWNER, "now it works")
            assert _sends(fake_cli["log"]) == [{"recipient": [OWNER], "message": "now it works"}]
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_a_start_cancelled_during_the_probe_leaves_no_child_running(
        self, fake_cli: dict[str, Path], monkeypatch: pytest.MonkeyPatch, children: list[Any]
    ) -> None:
        # signal-cli is up but has not answered `version` yet (a slow JVM) when
        # the gateway shuts down: the half-started child must not outlive it.
        monkeypatch.setenv("FAKE_SIGNAL_MODE", "silent")
        client = SignalClient(ACCOUNT)
        start = asyncio.ensure_future(client.start())
        try:
            await async_wait_until(
                _probed(fake_cli["log"], 1), off_loop=True, describe=lambda: children
            )
            start.cancel()
            with pytest.raises(asyncio.CancelledError):
                await await_until(start, what="the cancelled start returns")
            assert len(children) == 1
            assert children[0].returncode is not None, "the probed child is still running"
            assert client._peer is None
        finally:
            await client.close()
            await _reap(children)

    @pytest.mark.asyncio
    async def test_closing_during_a_reconnect_probe_leaves_no_child_running(
        self, fake_cli: dict[str, Path], monkeypatch: pytest.MonkeyPatch, children: list[Any]
    ) -> None:
        from kiro_crew.signal_cli import client as client_mod

        released = asyncio.Event()

        async def gated_sleep(_delay: float) -> None:
            await released.wait()

        monkeypatch.setattr(client_mod, "_sleep", gated_sleep)
        monkeypatch.setenv("FAKE_SIGNAL_MODE", "unregistered")
        orch = _orch(allowed=[OWNER])
        client = await gw.maybe_start_signal(orch)
        assert client is not None
        try:
            assert client._reconnect_task is not None
            # The retry spawns a child that hangs in its probe; the gateway
            # closes the channel then, cancelling the backoff loop mid-probe.
            monkeypatch.setenv("FAKE_SIGNAL_MODE", "silent")
            released.set()
            await async_wait_until(
                _probed(fake_cli["log"], 1), off_loop=True, describe=lambda: children
            )
            await await_until(asyncio.ensure_future(client.close()), what="close returns")
            assert len(children) == 2
            assert [p.returncode is not None for p in children] == [True, True]
            assert client._reconnect_task is None and client._peer is None
        finally:
            await client.close()
            await _reap(children)

    @pytest.mark.asyncio
    async def test_a_missing_binary_is_reported_and_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "absent"))
        orch = _orch(allowed=[OWNER])
        client = await gw.maybe_start_signal(orch)
        assert client is not None
        try:
            assert "signal-cli is not installed" in orch.dashboard_state.signal_connect_error
        finally:
            await client.close()
