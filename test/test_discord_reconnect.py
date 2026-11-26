"""Discord applies a new bot token and new Gateway intents without a restart.

Three layers, each with its own fakes:

* ``DiscordClient.reconnect`` swaps the token and intents on the live client:
  the old WebSocket, heartbeat and loop task are closed and awaited before a
  fresh IDENTIFY, running turn handlers are left alone, and a token swap
  retires the old HTTP session.
* ``discord.gateway.reconnect_discord`` decides from the saved config and
  credentials: verify the token first, keep the old connection on a rejected
  one, fall back to a channel restart for another bot or a cleared token, and
  stay on DM-only intents when Discord refuses the thread intents (4014).
* The config watcher's channel applier offers Discord a credential-only change
  (and an allow-list change) in place before restarting it, and the settings
  save stops asking for a gateway restart for what now applies live.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from _hot_reload_helpers import change as _change

from kiro_crew.channels import builtin_channel_descriptors
from kiro_crew.config import live
from kiro_crew.config.loader import CRED_DISCORD_BOT_TOKEN, KiroCrewConfig
from kiro_crew.discord import client as client_mod
from kiro_crew.discord import gateway as discord_gateway
from kiro_crew.discord.client import (
    _INTENT_DIRECT_MESSAGES,
    _THREAD_INTENTS,
    DiscordClient,
)
from kiro_crew.discord.client_recheck import SendPermission
from kiro_crew.discord.client_rest import DISCORD_OK
from kiro_crew.slack import gateway as gw
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.testing.wait import await_until

OLD_TOKEN = "old-token-value-for-tests"
NEW_TOKEN = "new-token-value-for-tests"
BOT_ID = "1100000000000000001"
APP_ID = "1100000000000000009"


# ── Fakes ──────────────────────────────────────────────────────────────


class _FakeWs:
    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0
        self.sent: list[dict[str, Any]] = []

    async def close(self, code: int | None = None) -> None:
        self.close_calls += 1
        self.closed = True

    async def send_json(self, obj: dict[str, Any]) -> None:
        self.sent.append(obj)


class _FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body
        self.headers: dict[str, str] = {}

    async def json(self, content_type: Any = None) -> Any:
        return self._body


class _FakeHttp:
    """Records each request's Authorization header and answers from *answers*."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.closed = False
        self.auth: list[str] = []
        self._status = status
        self._body = body if body is not None else {}

    @asynccontextmanager
    async def _answer(self, headers: dict[str, str]):
        self.auth.append(headers.get("Authorization", ""))
        yield _FakeResponse(self._status, self._body)

    def get(self, url: str, *, headers: dict[str, str], **_: Any):
        return self._answer(headers)

    def request(self, method: str, url: str, *, headers: dict[str, str], **_: Any):
        return self._answer(headers)

    async def close(self) -> None:
        self.closed = True


def _client_with_fake_loop(token: str = OLD_TOKEN, *, guild: bool = False):
    """A real client whose Gateway loop is a parked task, so no socket is opened."""
    client = DiscordClient(token=token, enable_guild_threads=guild)
    loops: list[asyncio.Event] = []

    async def _fake_loop() -> None:
        parked = asyncio.Event()
        loops.append(parked)
        await parked.wait()

    client._gateway_loop = _fake_loop  # type: ignore[method-assign]
    return client, loops


# ── DiscordClient.reconnect ────────────────────────────────────────────


class TestClientReconnect:
    @pytest.mark.asyncio
    async def test_a_token_swap_closes_the_old_connection_and_identifies_fresh(
        self, monkeypatch
    ) -> None:
        client, loops = _client_with_fake_loop()
        await client.start()
        await asyncio.sleep(0)
        old_task = client._task
        old_ws = _FakeWs()
        client._ws = old_ws
        old_http = _FakeHttp()
        client._session = old_http  # type: ignore[assignment]
        heartbeat = asyncio.create_task(asyncio.Event().wait())
        client._hb_task = heartbeat
        client._session_id, client._seq, client._resume_url = "sess", 41, "wss://resume"

        await client.reconnect(token=NEW_TOKEN)
        await asyncio.sleep(0)

        assert old_ws.close_calls == 1
        assert old_task is not None and old_task.done()
        assert heartbeat.cancelled()
        assert len(loops) == 2, "exactly one new Gateway loop, the old one ended"
        assert client._task is not None and not client._task.done()
        assert client.uses_token(NEW_TOKEN)
        assert (client._session_id, client._seq, client._resume_url) == ("", None, "")
        # The new IDENTIFY carries the new token: a RESUME could not change it.
        ws = _FakeWs()
        await client._identify(ws)
        assert ws.sent[0]["d"]["token"] == NEW_TOKEN
        # The HTTP session holds no credential and stays open for in-flight requests.
        assert client._session is old_http
        assert not old_http.closed
        await client.close()

    @pytest.mark.asyncio
    async def test_an_intents_change_identifies_with_the_new_intents(self) -> None:
        client, loops = _client_with_fake_loop()
        await client.start()
        await asyncio.sleep(0)
        http = _FakeHttp()
        client._session = http  # type: ignore[assignment]
        assert not client.guild_intents

        await client.reconnect(enable_guild_threads=True)
        await asyncio.sleep(0)
        ws = _FakeWs()
        await client._identify(ws)
        assert ws.sent[0]["d"]["intents"] == _INTENT_DIRECT_MESSAGES | _THREAD_INTENTS
        assert client.guild_intents
        # Same token: the HTTP session is kept.
        assert client._session is http

        await client.reconnect(enable_guild_threads=False)
        await asyncio.sleep(0)  # one loop pass: the new loop task starts
        ws = _FakeWs()
        await client._identify(ws)
        assert ws.sent[0]["d"]["intents"] == _INTENT_DIRECT_MESSAGES
        assert len(loops) == 3
        await client.close()

    @pytest.mark.asyncio
    async def test_a_turn_running_across_the_swap_keeps_going_with_the_new_token(
        self, monkeypatch
    ) -> None:
        client, _loops = _client_with_fake_loop()
        await client.start()
        http = _FakeHttp()
        monkeypatch.setattr(client, "_ensure_session", AsyncMock(return_value=http))
        swapped = asyncio.Event()

        async def _turn() -> None:
            await swapped.wait()
            await client._api_request(
                "GET", "/users/@me", timeout=5, build=dict
            )  # flake-ok: fake HTTP, no wait

        turn = asyncio.create_task(_turn())
        client._handler_tasks.add(turn)
        await client.reconnect(token=NEW_TOKEN)
        assert not turn.done(), "the reconnect must not cancel a running turn"
        swapped.set()
        await await_until(turn, what="the turn finishes")
        assert not turn.cancelled()
        assert http.auth == [f"Bot {NEW_TOKEN}"]
        client._handler_tasks.discard(turn)
        await client.close()

    @pytest.mark.asyncio
    async def test_a_request_retried_across_the_swap_sends_the_new_token(self, monkeypatch) -> None:
        """The Authorization header is read per attempt, after each wait."""
        client = DiscordClient(token=OLD_TOKEN)
        answers = [_FakeResponse(429, {"retry_after": 0.5}), _FakeResponse(200, {})]
        sent: list[str] = []

        class _Http(_FakeHttp):
            @asynccontextmanager
            async def _answer(self, headers):
                sent.append(headers.get("Authorization", ""))
                yield answers.pop(0)

        monkeypatch.setattr(client, "_ensure_session", AsyncMock(return_value=_Http()))

        async def _swap_during_the_wait(*_: Any) -> SendPermission:
            client._token = NEW_TOKEN  # what reconnect() does mid-wait
            return SendPermission(True)

        monkeypatch.setattr(client, "_wait_then_revalidate", _swap_during_the_wait)
        result = await client._api_request(
            "GET", "/users/@me", timeout=5, build=dict
        )  # flake-ok: fake HTTP, no wait
        assert result.outcome == DISCORD_OK
        assert sent == [f"Bot {OLD_TOKEN}", f"Bot {NEW_TOKEN}"]

    @pytest.mark.asyncio
    async def test_a_closed_client_is_not_revived(self) -> None:
        client, _loops = _client_with_fake_loop()
        await client.start()
        await client.close()
        with pytest.raises(RuntimeError):
            await client.reconnect(token=NEW_TOKEN)

    @pytest.mark.asyncio
    async def test_a_fatally_closed_client_is_revived_by_a_corrected_token(self) -> None:
        client, loops = _client_with_fake_loop()
        client._closed = True
        client.fatal_error = "gateway close 4004 (check bot token/intents)"
        await client.reconnect(token=NEW_TOKEN)
        await asyncio.sleep(0)  # one loop pass: the new loop task starts
        assert not client._closed and client.fatal_error == ""
        assert len(loops) == 1
        await client.close()

    @pytest.mark.asyncio
    async def test_wait_connected_returns_at_once_on_a_fatal_close(self) -> None:
        client = DiscordClient(token=OLD_TOKEN)
        client.fatal_error = "gateway close 4014 (check bot token/intents)"
        ok = await client.wait_connected(timeout=30.0)  # flake-ok: returns at once
        assert ok is False


class _OneAnswer(_FakeHttp):
    """A fake HTTP session that answers every request with one canned reply."""

    def __init__(self, status: int, body: Any, record: list[str]) -> None:
        super().__init__(status, body)
        self.record = record

    @asynccontextmanager
    async def _answer(self, headers: dict[str, str]):
        self.record.append(self.name)
        response = _FakeResponse(self._status, self._body)
        response.request_info = None  # type: ignore[attr-defined]
        yield response

    name = "http"


class TestRetryAfterSessionRetired:
    @pytest.mark.asyncio
    async def test_a_retry_after_the_swap_takes_the_new_http_session(self, monkeypatch) -> None:
        """The session is read per attempt, so a retry never reuses a closed one."""
        client = DiscordClient(token=OLD_TOKEN)
        used: list[str] = []
        old = _OneAnswer(503, {}, used)
        old.name = "old"
        new = _OneAnswer(200, {}, used)
        new.name = "new"
        sessions = iter([old, new])
        monkeypatch.setattr(
            client, "_ensure_session", AsyncMock(side_effect=lambda: next(sessions))
        )

        async def _backoff(*_: Any) -> SendPermission:
            return SendPermission(True)

        monkeypatch.setattr(client, "_wait_then_revalidate", _backoff)
        result = await client._api_request(
            "PATCH", "/channels/1/messages/2", timeout=5, build=dict
        )  # flake-ok: fake HTTP, no wait
        assert result.outcome == DISCORD_OK
        assert used == ["old", "new"]


class TestVerifyToken:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_a_rate_limit_or_server_error_is_unverifiable_not_rejected(
        self, monkeypatch, status
    ) -> None:
        client = DiscordClient(token=OLD_TOKEN)
        http = _OneAnswer(status, {"message": "try later"}, [])
        monkeypatch.setattr(client, "_ensure_session", AsyncMock(return_value=http))
        with pytest.raises(client_mod.aiohttp.ClientResponseError):
            await client.verify_token(NEW_TOKEN)

    @pytest.mark.asyncio
    async def test_a_forbidden_token_is_rejected(self, monkeypatch) -> None:
        client = DiscordClient(token=OLD_TOKEN)
        http = _OneAnswer(403, {"message": "Forbidden"}, [])
        monkeypatch.setattr(client, "_ensure_session", AsyncMock(return_value=http))
        assert await client.verify_token(NEW_TOKEN) == ("", "Forbidden")

    @pytest.mark.asyncio
    async def test_an_accepted_token_names_its_bot(self, monkeypatch) -> None:
        client = DiscordClient(token=OLD_TOKEN)
        http = _FakeHttp(200, {"id": BOT_ID})
        monkeypatch.setattr(client, "_ensure_session", AsyncMock(return_value=http))
        assert await client.verify_token(NEW_TOKEN) == (BOT_ID, "")
        # Checked with the NEW token, not the one the client runs.
        assert http.auth == [f"Bot {NEW_TOKEN}"]

    @pytest.mark.asyncio
    async def test_a_rejected_token_answers_discords_reason_without_the_token(
        self, monkeypatch
    ) -> None:
        client = DiscordClient(token=OLD_TOKEN)
        http = _FakeHttp(401, {"message": "401: Unauthorized", "code": 0})
        monkeypatch.setattr(client, "_ensure_session", AsyncMock(return_value=http))
        bot_id, reason = await client.verify_token(NEW_TOKEN)
        assert bot_id == ""
        assert reason == "401: Unauthorized"
        assert NEW_TOKEN not in reason


# ── reconnect_discord ──────────────────────────────────────────────────


def _cfg(*, enabled: bool = True, threads: list[str] | None = None) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.discord.enabled = enabled
    cfg.discord.allowed_user_ids = ["42"]
    cfg.discord.allowed_thread_ids = list(threads or [])
    return cfg


def _orch(client: Any) -> SimpleNamespace:
    return SimpleNamespace(
        _channel_handles={"discord": client},
        _channel_restart_lock=asyncio.Lock(),
        dashboard_state=SimpleNamespace(discord_reconnect_error=""),
        _discord_bot_token=OLD_TOKEN,
    )


class _StubClient(DiscordClient):
    """A real client whose network steps are scripted."""

    def __init__(self, *, guild: bool = False, verify: Any = (BOT_ID, "")) -> None:
        super().__init__(token=OLD_TOKEN, enable_guild_threads=guild)
        self.bot_user_id = BOT_ID
        self.application_id = APP_ID  # READY landed at boot unless a test clears it
        self.verify_result = verify
        self.reconnects: list[dict[str, Any]] = []
        self.connect_results: list[tuple[bool, str]] = []
        self.verified: list[str] = []

    async def verify_token(self, token: str) -> tuple[str, str]:
        self.verified.append(token)
        if isinstance(self.verify_result, BaseException):
            raise self.verify_result
        return self.verify_result

    async def reconnect(self, *, token=None, enable_guild_threads=None) -> None:
        self.reconnects.append({"token": token, "guild": enable_guild_threads})
        if token is not None:
            self._token = token
        if enable_guild_threads is not None:
            self._intents = client_mod._intents_for(enable_guild_threads)
        self.fatal_error = ""

    async def wait_connected(self, timeout: float = 15.0) -> bool:
        ok, fatal = self.connect_results.pop(0) if self.connect_results else (True, "")
        self.fatal_error = fatal
        return ok


def _saved(monkeypatch, cfg: KiroCrewConfig, token: str) -> None:
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    monkeypatch.setattr(
        cfg, "load_credentials", lambda propagate=True: {CRED_DISCORD_BOT_TOKEN: token}
    )


@pytest.fixture
def governance(monkeypatch) -> dict[str, bool]:
    """The connect-time ``channels`` gate, scripted: permits unless told otherwise."""
    answer = {"discord": True}
    monkeypatch.setattr(gw, "_channel_transport_permitted", lambda member: answer[member])
    return answer


@pytest.mark.usefixtures("governance")
class TestReconnectDiscord:
    @pytest.mark.asyncio
    async def test_a_policy_that_denies_discord_gets_a_restart_not_a_reconnect(
        self, monkeypatch, governance
    ) -> None:
        governance["discord"] = False
        client = _StubClient()
        _saved(monkeypatch, _cfg(), NEW_TOKEN)
        assert await discord_gateway.reconnect_discord(_orch(client)) is False
        assert client.reconnects == []

    @pytest.mark.asyncio
    async def test_a_queued_attempt_reads_the_settings_saved_while_it_waited(
        self, monkeypatch
    ) -> None:
        """Remove the last thread, then put it back before the first attempt has read.

        The second attempt must apply the LATEST settings (threads), not be a
        no-op that the first, stale attempt then undoes.
        """
        client = _StubClient(guild=True)
        orch = _orch(client)
        narrowed, widened = _cfg(threads=[]), _cfg(threads=["234567890123456789"])
        current = {"cfg": narrowed}
        monkeypatch.setattr(live, "snapshot", lambda: current["cfg"])
        release_first_read = asyncio.Event()
        first_read_started = asyncio.Event()

        def _creds(propagate: bool = True) -> dict[str, str]:
            return {CRED_DISCORD_BOT_TOKEN: OLD_TOKEN}

        async def _slow_first_read(func, *args, **kwargs):
            if not first_read_started.is_set():
                first_read_started.set()
                await release_first_read.wait()
            return func(*args, **kwargs)

        for cfg in (narrowed, widened):
            monkeypatch.setattr(cfg, "load_credentials", _creds)
        monkeypatch.setattr(discord_gateway.asyncio, "to_thread", _slow_first_read)

        first = asyncio.create_task(discord_gateway.reconnect_discord(orch))
        await await_until(first_read_started, what="the first read starts")
        current["cfg"] = widened
        second = asyncio.create_task(discord_gateway.reconnect_discord(orch))
        await asyncio.sleep(0)
        release_first_read.set()
        await await_until(asyncio.gather(first, second), what="both attempts finish")
        assert client.guild_intents, "the latest saved settings allow threads"

    @pytest.mark.asyncio
    async def test_an_attempt_in_flight_keeps_the_last_outcome_readable(self, monkeypatch) -> None:
        """A save reading the outcome while another save's attempt runs sees the last one."""
        client = _StubClient(guild=False)
        orch = _orch(client)
        orch.dashboard_state.discord_reconnect_error = discord_gateway.INTENTS_REFUSED
        _saved(monkeypatch, _cfg(threads=["234567890123456789"]), OLD_TOKEN)
        entered, release = asyncio.Event(), asyncio.Event()
        plain_reconnect = client.reconnect

        async def _held_reconnect(**kwargs: Any) -> None:
            entered.set()
            await release.wait()
            await plain_reconnect(**kwargs)

        client.reconnect = _held_reconnect  # type: ignore[method-assign]
        refused = (False, "gateway close 4014 (check bot token/intents)")
        client.connect_results = [refused, (True, "")]
        attempt = asyncio.create_task(discord_gateway.reconnect_discord(orch))
        await await_until(entered, what="the reconnect is entered")
        assert orch.dashboard_state.discord_reconnect_error == discord_gateway.INTENTS_REFUSED
        release.set()
        assert await await_until(attempt, what="the attempt finishes") is True
        assert orch.dashboard_state.discord_reconnect_error == discord_gateway.INTENTS_REFUSED

    @pytest.mark.asyncio
    async def test_a_client_a_boot_4014_stopped_is_revived_by_a_save(self, monkeypatch) -> None:
        """The revive also reruns the boot steps that waited for READY and gave up."""
        boot_steps: list[str] = []
        monkeypatch.setattr(
            discord_gateway, "_publish_slash_commands", lambda c: boot_steps.append("menu")
        )
        monkeypatch.setattr(
            discord_gateway,
            "_reopen_owner_dm",
            lambda c, ids: boot_steps.append(f"dm:{sorted(ids)}"),
        )
        client = _StubClient(guild=True)
        client.application_id = ""  # READY never landed at boot
        client.fatal_error = "gateway close 4014 (check bot token/intents)"
        _saved(monkeypatch, _cfg(threads=["234567890123456789"]), OLD_TOKEN)
        assert await discord_gateway.reconnect_discord(_orch(client)) is True
        assert client.reconnects == [{"token": None, "guild": True}]
        assert boot_steps == ["menu", "dm:['42']"]

    @pytest.mark.asyncio
    async def test_a_client_that_reached_ready_does_not_republish(self, monkeypatch) -> None:
        boot_steps: list[str] = []
        monkeypatch.setattr(
            discord_gateway, "_publish_slash_commands", lambda c: boot_steps.append("menu")
        )
        client = _StubClient(guild=False)
        _saved(monkeypatch, _cfg(threads=["234567890123456789"]), OLD_TOKEN)
        assert await discord_gateway.reconnect_discord(_orch(client)) is True
        assert client.reconnects == [{"token": None, "guild": True}]
        assert boot_steps == []

    @pytest.mark.asyncio
    async def test_saving_again_after_a_4014_retries_the_thread_intents(self, monkeypatch) -> None:
        """An unchanged save schedules nothing in the watcher; the save's reconcile retries."""
        client = _StubClient(guild=False)
        orch = _orch(client)
        orch.channel_restarts_settled = AsyncMock()
        _saved(monkeypatch, _cfg(threads=["234567890123456789"]), OLD_TOKEN)

        await discord_gateway.reconcile_after_save(orch)
        orch.channel_restarts_settled.assert_awaited_once()
        assert client.reconnects == [{"token": None, "guild": True}]
        assert orch.dashboard_state.discord_reconnect_error == ""

    @pytest.mark.asyncio
    async def test_a_later_attempt_keeps_reporting_a_refusal_that_still_stands(
        self, monkeypatch
    ) -> None:
        """Another save's reconcile must not wipe a 4014 that is still in force."""
        client = _StubClient(guild=False)
        refused = (False, "gateway close 4014 (check bot token/intents)")
        client.connect_results = [refused, (True, ""), refused, (True, "")]
        orch = _orch(client)
        _saved(monkeypatch, _cfg(threads=["234567890123456789"]), OLD_TOKEN)

        assert await discord_gateway.reconnect_discord(orch) is True
        assert orch.dashboard_state.discord_reconnect_error == discord_gateway.INTENTS_REFUSED
        assert await discord_gateway.reconnect_discord(orch) is True
        assert orch.dashboard_state.discord_reconnect_error == discord_gateway.INTENTS_REFUSED

    @pytest.mark.asyncio
    async def test_a_good_token_for_the_same_bot_reconnects_in_place(
        self, monkeypatch, caplog
    ) -> None:
        caplog.set_level(logging.DEBUG)
        client = _StubClient()
        orch = _orch(client)
        _saved(monkeypatch, _cfg(), NEW_TOKEN)

        assert await discord_gateway.reconnect_discord(orch) is True
        assert client.verified == [NEW_TOKEN]
        assert client.reconnects == [{"token": NEW_TOKEN, "guild": False}]
        assert orch._discord_bot_token == NEW_TOKEN
        assert orch.dashboard_state.discord_reconnect_error == ""
        assert NEW_TOKEN not in caplog.text and OLD_TOKEN not in caplog.text

    @pytest.mark.asyncio
    async def test_a_rejected_token_leaves_the_old_connection_and_says_why(
        self, monkeypatch, caplog
    ) -> None:
        caplog.set_level(logging.DEBUG)
        client = _StubClient(verify=("", "401: Unauthorized"))
        orch = _orch(client)
        _saved(monkeypatch, _cfg(), NEW_TOKEN)

        assert await discord_gateway.reconnect_discord(orch) is True
        assert client.reconnects == [], "the old connection must not be touched"
        assert client.uses_token(OLD_TOKEN)
        error = orch.dashboard_state.discord_reconnect_error
        assert "401: Unauthorized" in error and "still running" in error
        assert NEW_TOKEN not in error and NEW_TOKEN not in caplog.text

    @pytest.mark.asyncio
    async def test_an_unverifiable_token_falls_back_to_a_restart(self, monkeypatch) -> None:
        client = _StubClient(verify=ConnectionError("offline"))
        _saved(monkeypatch, _cfg(), NEW_TOKEN)
        assert await discord_gateway.reconnect_discord(_orch(client)) is False
        assert client.reconnects == []

    @pytest.mark.asyncio
    async def test_a_token_for_another_bot_falls_back_to_a_restart(self, monkeypatch) -> None:
        client = _StubClient(verify=("2200000000000000002", ""))
        _saved(monkeypatch, _cfg(), NEW_TOKEN)
        assert await discord_gateway.reconnect_discord(_orch(client)) is False
        assert client.reconnects == []

    @pytest.mark.asyncio
    async def test_widening_dm_only_to_threads_reidentifies_with_thread_intents(
        self, monkeypatch
    ) -> None:
        client = _StubClient(guild=False)
        _saved(monkeypatch, _cfg(threads=["234567890123456789"]), OLD_TOKEN)

        assert await discord_gateway.reconnect_discord(_orch(client)) is True
        assert client.verified == [], "the token did not change, so it is not re-checked"
        assert client.reconnects == [{"token": None, "guild": True}]

    @pytest.mark.asyncio
    async def test_a_thread_the_bot_opened_keeps_the_thread_intents(self, monkeypatch) -> None:
        """Removing the parent channel keeps a runtime-admitted thread, so its intents stay."""

        class _Transport:
            _allowed_threads = {"345678901234567891"}  # promoted at runtime, not in config

            async def receive(self, inbound: Any) -> None:
                return None

        client = _StubClient(guild=True)
        client.set_message_handler(_Transport().receive)
        _saved(monkeypatch, _cfg(threads=[]), OLD_TOKEN)
        assert await discord_gateway.reconnect_discord(_orch(client)) is True
        assert client.reconnects == [], "a still-admitted thread keeps the thread intents"

    @pytest.mark.asyncio
    async def test_narrowing_back_to_dm_only_drops_the_thread_intents(self, monkeypatch) -> None:
        client = _StubClient(guild=True)
        _saved(monkeypatch, _cfg(threads=[]), OLD_TOKEN)
        assert await discord_gateway.reconnect_discord(_orch(client)) is True
        assert client.reconnects == [{"token": None, "guild": False}]

    @pytest.mark.asyncio
    async def test_nothing_changed_is_a_no_op(self, monkeypatch) -> None:
        client = _StubClient()
        _saved(monkeypatch, _cfg(), OLD_TOKEN)
        assert await discord_gateway.reconnect_discord(_orch(client)) is True
        assert client.reconnects == [] and client.verified == []

    @pytest.mark.asyncio
    async def test_refused_thread_intents_stay_on_dm_only_and_say_so(self, monkeypatch) -> None:
        client = _StubClient(guild=False)
        client.connect_results = [
            (False, "gateway close 4014 (check bot token/intents)"),
            (True, ""),
        ]
        orch = _orch(client)
        _saved(monkeypatch, _cfg(threads=["234567890123456789"]), OLD_TOKEN)

        assert await discord_gateway.reconnect_discord(orch) is True
        assert client.reconnects == [
            {"token": None, "guild": True},
            {"token": None, "guild": False},
        ]
        assert not client.guild_intents
        assert orch.dashboard_state.discord_reconnect_error == discord_gateway.INTENTS_REFUSED

    @pytest.mark.asyncio
    async def test_disabling_discord_or_clearing_the_token_falls_back_to_a_restart(
        self, monkeypatch
    ) -> None:
        client = _StubClient()
        _saved(monkeypatch, _cfg(enabled=False), OLD_TOKEN)
        assert await discord_gateway.reconnect_discord(_orch(client)) is False
        _saved(monkeypatch, _cfg(), "")
        assert await discord_gateway.reconnect_discord(_orch(client)) is False
        assert client.reconnects == []

    @pytest.mark.asyncio
    async def test_no_live_client_falls_back_to_a_restart(self, monkeypatch) -> None:
        _saved(monkeypatch, _cfg(), NEW_TOKEN)
        orch = _orch(None)
        orch._channel_handles = {}
        assert await discord_gateway.reconnect_discord(orch) is False

    @pytest.mark.asyncio
    async def test_a_client_replaced_mid_check_is_not_swapped(self, monkeypatch) -> None:
        client = _StubClient()
        orch = _orch(client)
        _saved(monkeypatch, _cfg(), NEW_TOKEN)

        async def _replaced_meanwhile(token: str) -> tuple[str, str]:
            orch._channel_handles["discord"] = object()
            return BOT_ID, ""

        client.verify_token = _replaced_meanwhile  # type: ignore[method-assign]
        assert await discord_gateway.reconnect_discord(orch) is True
        assert client.reconnects == []


# ── The config watcher's channel applier ──────────────────────────────


def _make_orchestrator() -> GatewayOrchestrator:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch._channel_transports_started = True
    return orch


def _with_discord_reconnect(monkeypatch, outcome: Any) -> list[str]:
    """Give Discord's descriptor a scripted reconnect; return the calls it got."""
    calls: list[str] = []

    async def _reconnect(orch: Any) -> bool:
        calls.append("reconnect")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    descs = tuple(
        dataclasses.replace(d, reconnect=_reconnect) if d.channel_type == "discord" else d
        for d in builtin_channel_descriptors()
    )
    monkeypatch.setattr(gw, "builtin_channel_descriptors", lambda: descs)
    return calls


def _record_restarts(orch: GatewayOrchestrator) -> tuple[list[str], list[str]]:
    restarts: list[str] = []
    closes: list[str] = []

    async def _restart(channel_type: str, *, cfg: Any = None) -> None:
        restarts.append(channel_type)

    async def _close(channel_type: str) -> None:
        closes.append(channel_type)

    orch.restart_channel = _restart  # type: ignore[method-assign]
    orch._close_channel_locked = _close  # type: ignore[method-assign]
    return restarts, closes


async def _apply(orch: GatewayOrchestrator, change: Any) -> None:
    await orch._on_channel_config_change(change)
    await orch.channel_restarts_settled()


class TestChannelApplier:
    def test_discord_declares_its_reconnect(self) -> None:
        desc = next(d for d in builtin_channel_descriptors() if d.channel_type == "discord")
        assert desc.reconnect is discord_gateway.reconnect_discord
        assert desc.reconnect_keys == frozenset({"allowed_thread_ids", "allowed_channel_ids"})

    @pytest.mark.asyncio
    async def test_a_token_change_reconnects_in_place_without_closing(self, monkeypatch) -> None:
        calls = _with_discord_reconnect(monkeypatch, True)
        orch = _make_orchestrator()
        restarts, closes = _record_restarts(orch)
        await _apply(orch, _change(KiroCrewConfig(), f"credentials.{CRED_DISCORD_BOT_TOKEN}"))
        assert calls == ["reconnect"]
        assert restarts == [] and closes == []

    @pytest.mark.parametrize("outcome", [False, RuntimeError("boom")], ids=["declined", "raised"])
    @pytest.mark.asyncio
    async def test_a_failed_reconnect_falls_back_to_a_restart(self, monkeypatch, outcome) -> None:
        calls = _with_discord_reconnect(monkeypatch, outcome)
        orch = _make_orchestrator()
        restarts, _closes = _record_restarts(orch)
        await _apply(orch, _change(KiroCrewConfig(), f"credentials.{CRED_DISCORD_BOT_TOKEN}"))
        assert calls == ["reconnect"]
        assert restarts == ["discord"]

    @pytest.mark.asyncio
    async def test_an_allow_list_change_is_offered_to_the_reconnect(self, monkeypatch) -> None:
        calls = _with_discord_reconnect(monkeypatch, True)
        orch = _make_orchestrator()
        restarts, closes = _record_restarts(orch)
        await _apply(orch, _change(KiroCrewConfig(), "discord.allowed_thread_ids"))
        assert calls == ["reconnect"]
        assert restarts == [] and closes == []

    @pytest.mark.asyncio
    async def test_a_declined_allow_list_reconnect_still_restarts_nothing(
        self, monkeypatch
    ) -> None:
        calls = _with_discord_reconnect(monkeypatch, False)
        orch = _make_orchestrator()
        restarts, closes = _record_restarts(orch)
        await _apply(orch, _change(KiroCrewConfig(), "discord.allowed_channel_ids"))
        assert calls == ["reconnect"]
        assert restarts == [] and closes == []

    @pytest.mark.asyncio
    async def test_disabling_discord_closes_and_restarts_without_a_reconnect(
        self, monkeypatch
    ) -> None:
        calls = _with_discord_reconnect(monkeypatch, True)
        orch = _make_orchestrator()
        restarts, closes = _record_restarts(orch)
        await _apply(
            orch,
            _change(KiroCrewConfig(), "discord.enabled", f"credentials.{CRED_DISCORD_BOT_TOKEN}"),
        )
        assert calls == []
        assert closes == ["discord"] and restarts == ["discord"]

    @pytest.mark.asyncio
    async def test_another_channels_token_change_still_restarts_it(self, monkeypatch) -> None:
        _with_discord_reconnect(monkeypatch, True)
        orch = _make_orchestrator()
        restarts, closes = _record_restarts(orch)
        await _apply(orch, _change(KiroCrewConfig(), "credentials.TELEGRAM_BOT_TOKEN"))
        assert closes == ["telegram"] and restarts == ["telegram"]


# ── The settings save ─────────────────────────────────────────────────

VALID_TOKEN = ".".join(
    ["MTA5OTk5OTk5OTk5OTk5OTk5OQ", "GhIjKl", "MnOpQrStUvWxYz0123456789_-AbCdEfGhIj"]
)


def _put(monkeypatch, tmp_path: Path, body: dict, *, watcher: bool, state: Any = None):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import kiro_crew.config.loader as loader
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    if not env.exists():
        env.write_text("", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    monkeypatch.setattr(mod, "_validate_discord_token", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_hot_apply_after_write", AsyncMock())
    monkeypatch.setattr(live.ConfigWatch, "started", property(lambda self: watcher))

    async def _run():
        app = web.Application()
        if state is not None:
            app["state"] = state
        app.router.add_put("/api/discord/config", mod.api_discord_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/discord/config", json=body)
            return resp.status, await resp.json()

    return asyncio.run(_run())


class TestSettingsSave:
    def test_a_token_save_needs_no_restart_while_the_watcher_runs(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.delenv(CRED_DISCORD_BOT_TOKEN, raising=False)
        status, body = _put(monkeypatch, tmp_path, {"bot_token": VALID_TOKEN}, watcher=True)
        assert status == 200
        assert body["restart_required"] is False
        assert body["reconnect_error"] == ""

    def test_a_token_save_still_needs_a_restart_with_no_watcher(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.delenv(CRED_DISCORD_BOT_TOKEN, raising=False)
        status, body = _put(monkeypatch, tmp_path, {"bot_token": VALID_TOKEN}, watcher=False)
        assert status == 200
        assert body["restart_required"] is True

    def test_the_save_answers_with_what_the_reconnect_could_not_apply(
        self, tmp_path, monkeypatch
    ) -> None:
        state = SimpleNamespace(discord_reconnect_error="")
        settled: list[bool] = []

        async def _reconcile() -> None:
            # What the save's reconcile records: the latest attempt's outcome.
            settled.append(True)
            state.discord_reconnect_error = discord_gateway.INTENTS_REFUSED

        state.discord_reconcile = _reconcile
        status, body = _put(
            monkeypatch,
            tmp_path,
            {"allowed_thread_ids": ["234567890123456789"]},
            watcher=True,
            state=state,
        )
        assert status == 200
        assert settled == [True]
        assert body["reconnect_error"] == discord_gateway.INTENTS_REFUSED
        assert body["restart_required"] is False
        assert VALID_TOKEN not in json.dumps(body)
