"""Tests for kiro_crew.signal_cli.gateway and the client's recovery after an exit.

The happy path and the "not registered" start failure run against the fake
binary in ``test_signal_e2e.py``; these cover the guarded branches and the
reconnect backoff without spawning anything.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.signal_cli import client as client_mod
from kiro_crew.signal_cli import gateway as gw
from kiro_crew.signal_cli.client import SignalClient
from kiro_crew.signal_cli.rpc import RpcTransportError
from kiro_crew.testing.wait import await_until

ACCOUNT = "+447700900001"


def _orch(*, enabled: bool = True, account: str = ACCOUNT, approval: Any = None) -> Any:
    state = SimpleNamespace(signal_connected=True, signal_connect_error="")
    cfg = SimpleNamespace(
        signal=SimpleNamespace(account=account, allowed_numbers=["+447700900002"]),
        agent=SimpleNamespace(approval_mode="auto"),
    )
    return SimpleNamespace(
        _signal_enabled=enabled,
        _cfg=cfg,
        _approval_mode=approval,
        dashboard_state=state,
        sessions=object(),
        ctx_builder=object(),
    )


class TestGuards:
    @pytest.mark.asyncio
    async def test_disabled_starts_nothing(self) -> None:
        orch = _orch(enabled=False)
        assert await gw.maybe_start_signal(orch) is None
        assert orch.dashboard_state.signal_connect_error == ""

    @pytest.mark.asyncio
    async def test_a_missing_account_is_reported_not_raised(self) -> None:
        orch = _orch(account="  ")
        assert await gw.maybe_start_signal(orch) is None
        assert "signal.account" in orch.dashboard_state.signal_connect_error
        assert orch.dashboard_state.signal_connected is False

    @pytest.mark.asyncio
    async def test_an_unexpected_error_is_reported_by_type_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: Any, **_k: Any) -> Any:
            raise ValueError("secret detail")

        monkeypatch.setattr(gw, "build_signal_channel", boom)
        orch = _orch()
        assert await gw.maybe_start_signal(orch) is None
        assert orch.dashboard_state.signal_connect_error == "ValueError"

    def test_approval_mode(self) -> None:
        assert gw._resolve_approval_mode(_orch(approval="yolo")) == APPROVAL_AUTO
        assert gw._resolve_approval_mode(_orch()) == APPROVAL_AUTO
        orch = _orch()
        orch._cfg.agent.approval_mode = "interactive"
        assert gw._resolve_approval_mode(orch) == APPROVAL_INTERACTIVE


class _DeadPeer:
    def __init__(self, tail: str) -> None:
        self.tail = tail
        self.closed = False

    async def settle_stderr(self, timeout: float = 2.0) -> str:
        return self.tail

    async def close(self) -> None:
        self.closed = True


class TestRecovery:
    @pytest.mark.asyncio
    async def test_an_exit_after_start_is_reported_and_retried_with_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delays: list[float] = []
        states: list[tuple[bool, str]] = []
        attempts = {"n": 0}
        replacement = _DeadPeer("")

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        async def fake_open(self: SignalClient) -> Any:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RpcTransportError("signal-cli exited: still broken")
            return replacement

        monkeypatch.setattr(client_mod, "_sleep", fake_sleep)
        monkeypatch.setattr(SignalClient, "_open_peer", fake_open)
        client = SignalClient(ACCOUNT)
        client.on_state_change = lambda ok, err: states.append((ok, err))
        lost = _DeadPeer("Config file is in use by another instance")
        client._peer = lost  # type: ignore[assignment]
        client.ready.set()

        client._on_peer_lost(lost, "bridge exited")  # type: ignore[arg-type]
        assert not client.ready.is_set()
        await await_until(client.ready, what="the client reconnects")

        assert lost.closed
        assert client._peer is replacement
        assert delays == [2.0, 4.0, 8.0]  # waits BEFORE every attempt: no tight loop
        assert (False, "signal-cli exited: Config file is in use by another instance") in states
        assert states[-1] == (True, "")
        await client.close()

    @pytest.mark.asyncio
    async def test_the_backoff_carries_across_quick_exits_and_resets_after_stable_uptime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delays: list[float] = []
        now = [1000.0]

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        async def fake_open(self: SignalClient) -> Any:
            return _DeadPeer("")

        monkeypatch.setattr(client_mod, "_sleep", fake_sleep)
        monkeypatch.setattr(client_mod, "_monotonic", lambda: now[0])
        monkeypatch.setattr(SignalClient, "_open_peer", fake_open)
        client = SignalClient(ACCOUNT)

        async def lose_current_peer() -> None:
            client._on_peer_lost(client._peer, "bridge exited")  # type: ignore[arg-type]
            task = client._reconnect_task
            assert task is not None
            await await_until(task, what="the restart completes")

        client._adopt(_DeadPeer(""))  # type: ignore[arg-type]
        for _ in range(3):
            now[0] += 5.0  # passes the probe, dies seconds later
            await lose_current_peer()
        assert delays == [2.0, 4.0, 8.0]  # not 2, 2, 2
        now[0] += client_mod.STABLE_UPTIME_S
        await lose_current_peer()
        assert delays[-1] == 2.0
        await client.close()

    @pytest.mark.asyncio
    async def test_a_failed_boot_retries_in_the_background_and_reports_each_round(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delays: list[float] = []
        states: list[tuple[bool, str]] = []
        attempts = {"n": 0}

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        async def fake_open(self: SignalClient) -> Any:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RpcTransportError(f"signal-cli exited: attempt {attempts['n']}")
            return _DeadPeer("")

        monkeypatch.setattr(client_mod, "_sleep", fake_sleep)
        monkeypatch.setattr(SignalClient, "_open_peer", fake_open)
        client = SignalClient(ACCOUNT)
        client.on_state_change = lambda ok, err: states.append((ok, err))
        client.start_in_background("signal-cli exited: no network")
        await await_until(client.ready, what="the background start succeeds")
        assert delays == [2.0, 4.0, 8.0]
        assert states == [
            (False, "signal-cli exited: no network"),
            (False, "signal-cli exited: attempt 1"),
            (False, "signal-cli exited: attempt 2"),
            (True, ""),
        ]
        await client.close()

    @pytest.mark.asyncio
    async def test_a_stale_or_closing_peer_does_not_reconnect(self) -> None:
        client = SignalClient(ACCOUNT)
        current = _DeadPeer("")
        client._peer = current  # type: ignore[assignment]
        client._on_peer_lost(_DeadPeer(""), "old")  # type: ignore[arg-type]
        assert client._peer is current and client._reconnect_task is None
        client._closing = True
        client._on_peer_lost(current, "closing")  # type: ignore[arg-type]
        assert client._reconnect_task is None

    def test_the_backoff_is_capped(self) -> None:
        assert client_mod.RECONNECT_MIN_S > 0
        assert client_mod.RECONNECT_MAX_S <= 60.0


class TestConfigCoercion:
    def test_an_unquoted_number_keeps_its_plus(self) -> None:
        from kiro_crew.config.section_builders import _build_signal_config

        cfg = _build_signal_config(
            {
                "account": 447700900001,
                "allowed_numbers": [447700900002, " +447700900003 ", True, None, ""],
            }
        )
        assert cfg.account == "+447700900001"
        assert cfg.allowed_numbers == ["+447700900002", "+447700900003"]

    @pytest.mark.parametrize("value", [None, True, 1.5, [], ""])
    def test_a_non_identifier_account_is_empty(self, value: Any) -> None:
        from kiro_crew.config.section_builders import _build_signal_config

        assert _build_signal_config({"account": value}).account == ""

    @pytest.mark.asyncio
    async def test_boot_warns_about_an_unusable_account_and_entries(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        orch = _orch(account="07700900001")
        orch._cfg.signal.allowed_numbers = ["bob", "+447700900002"]

        async def fail_connect(self: Any) -> None:
            raise RpcTransportError("signal-cli exited: x")

        monkeypatch.setattr("kiro_crew.signal_cli.transport.SignalTransport.connect", fail_connect)
        monkeypatch.setattr(SignalClient, "start_in_background", lambda self, err: None)
        orch.dashboard_state.register_channel_transport = lambda t: None
        with caplog.at_level("WARNING", logger="kiro_crew.signal_cli.transport"):
            client = await gw.maybe_start_signal(orch)
        assert client is not None
        assert "signal.account entry 0770***" in caplog.text
        assert "signal.allowed_numbers entry bob***" in caplog.text
        assert "07700900001" not in caplog.text
        await client.close()
