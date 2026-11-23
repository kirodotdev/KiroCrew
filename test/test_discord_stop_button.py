"""The Stop button on a streaming Discord reply.

While a turn streams, the bubble being written carries one ⏹ Stop button
(``x:<turn nonce>``). A press by anyone who could send ``!stop`` there cancels that
turn and clears the presser's own queued messages, also on a channel the governance
ceiling has denied. Once the turn finalizes its nonce is retired, so no button left
anywhere (a missed retirement edit, a restart) can cancel the next turn. Seal and
finalize edits REPLACE the components, so a sealed part and a finished reply never
keep the button and never exceed Discord's five action rows.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

import pytest
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import FakeClient, FakeProvider, _dispatcher, _Ev, _inbound, _origin

import kiro_crew.discord.transport_dispatch as td_mod
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.discord import renderer as discord_renderer
from kiro_crew.discord.client import DiscordInteraction
from kiro_crew.discord.renderer import DiscordRenderer, stop_turn_target
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.testing.wait import async_wait_until, await_until, finished


class _Client(FakeClient):
    """FakeClient plus the follow-up route a Stop press answers privately on."""

    def __init__(self) -> None:
        super().__init__()
        self.application_id = "app1"
        self.api_calls: list[tuple[str, str, Any, str]] = []
        #: Only the edits that LANDED: ``edits`` records failed ones too.
        self.landed_edits: list[tuple[str, Any]] = []

    async def edit_message(
        self, channel_id: str, message_id: str, text: str, *, components: Any = None
    ) -> bool:
        ok = await super().edit_message(channel_id, message_id, text, components=components)
        if ok:
            self.landed_edits.append((message_id, components))
        return ok

    async def api_json(
        self, method: str, path: str, payload: Any, *, destination: str = "", **_: Any
    ) -> Any:
        self.api_calls.append((method, path, payload, destination))
        return object()


class _StreamingProvider(FakeProvider):
    """Streams one chunk, then holds the turn open until cancelled or released."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def stream(self, message: str) -> Any:
        yield _Ev(EVENT_TEXT_CHUNK, text=f"Working on {message[:16]}")
        await await_until(self.release, what="the turn being stopped or released")
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> str:
        self.cancelled += 1
        self.release.set()
        return "acked"


def _stop_ids(components: Any) -> list[str]:
    return [
        button["custom_id"]
        for row in components or []
        for button in row["components"]
        if button["custom_id"].startswith("x:")
    ]


def _press(custom_id: str, *, user: str = "u1", channel: str = "c1") -> DiscordInteraction:
    return DiscordInteraction(
        interaction_id="i-stop",
        interaction_token="tok-stop",
        channel_id=channel,
        user_id=user,
        message_id="m-any",
        custom_id=custom_id,
    )


def _wired(allowed: set[str] | None = None) -> tuple[Any, _Client, Any, list[Any]]:
    """A dispatcher whose sessions hand out a fresh held-open provider per turn."""
    d, _cli, sess = _dispatcher(allowed or {"u1"})
    cli = _Client()
    d.client = cli  # type: ignore[assignment]
    providers: list[_StreamingProvider] = []
    clears: list[Any] = []
    real_release = sess.release

    async def _get_or_create(key: str, **_: Any) -> Any:
        provider = _StreamingProvider()
        providers.append(provider)
        sess._gp = provider
        sess._busy = True
        # The real manager records the claiming task as the turn's holder.
        sess.holder = asyncio.current_task()
        return provider, True, False

    def _release(key: str) -> None:
        sess._busy = False
        real_release(key)

    def _clear_queue(key: str, owned_by: Any = None) -> None:
        clears.append(owned_by)

    sess.get_or_create = _get_or_create
    sess.release = _release
    sess.turn_holder = lambda key: sess.holder if sess._busy else None
    sess.clear_queue = _clear_queue
    sess.clears = clears
    return d, cli, sess, providers


async def _live_stop(cli: _Client, after: int = 0) -> str:
    """The Stop custom_id on the first live bubble sent after *after* sends."""
    await async_wait_until(
        lambda: any(_stop_ids(c) for _t, c in cli.sent[after:]),
        describe=lambda: cli.sent,
    )
    return next(_stop_ids(c)[0] for _t, c in cli.sent[after:] if _stop_ids(c))


class TestStopPress:
    @pytest.mark.asyncio
    async def test_a_press_cancels_the_turn_and_clears_only_the_pressers_queue(self) -> None:
        d, cli, sess, providers = _wired()
        turn = asyncio.create_task(d.handle_message(_inbound("hello")))
        custom_id = await _live_stop(cli)

        await d.on_interaction(_press(custom_id))

        assert providers[0].cancelled == 1
        assert any("Stopped" in text for text, _ in cli.sent)
        # The queue clear is scoped to the presser: their entry, not another's.
        (owned_by,) = sess.clears
        assert owned_by(_origin("u1", "c1")) and not owned_by(_origin("u2", "c1"))
        await finished(turn, "the stopped turn")
        # The turn is over: its last edit took the button off and its nonce is gone.
        assert cli.edits[-1][2] == []
        assert stop_turn_target(custom_id[2:]) is None

    @pytest.mark.asyncio
    async def test_a_presser_off_the_roster_stops_nothing(self) -> None:
        d, cli, _sess, providers = _wired({"u1"})
        turn = asyncio.create_task(d.handle_message(_inbound("hello")))
        custom_id = await _live_stop(cli)

        await d.on_interaction(_press(custom_id, user="intruder"))

        assert providers[0].cancelled == 0 and cli.acked == []
        providers[0].release.set()
        await finished(turn, "the released turn")

    @pytest.mark.asyncio
    async def test_a_roster_withdrawal_during_the_ack_still_stops_nothing(self) -> None:
        """The ceiling is the only gate a Stop skips; the roster re-read still applies."""
        d, cli, _sess, providers = _wired({"u1"})
        turn = asyncio.create_task(d.handle_message(_inbound("hello")))
        custom_id = await _live_stop(cli)
        original_ack = cli.ack_component_interaction

        async def _ack_then_revoke(*args: Any, **kwargs: Any) -> None:
            await original_ack(*args, **kwargs)
            d._allowed.discard("u1")

        cli.ack_component_interaction = _ack_then_revoke  # type: ignore[method-assign]
        await d.on_interaction(_press(custom_id))

        assert cli.acked == ["i-stop"] and providers[0].cancelled == 0
        providers[0].release.set()
        await finished(turn, "the released turn")

    @pytest.mark.asyncio
    async def test_a_press_still_stops_on_a_governance_denied_channel(self, monkeypatch) -> None:
        d, cli, _sess, providers = _wired()
        turn = asyncio.create_task(d.handle_message(_inbound("hello")))
        custom_id = await _live_stop(cli)
        from kiro_crew.messaging import dispatch

        async def _denied(_channel_type: str) -> bool:
            return False

        # Denied in both namespaces that read the ceiling, as for `/stop`.
        monkeypatch.setattr(dispatch, "channel_inbound_permitted", _denied)
        monkeypatch.setattr(td_mod, "channel_inbound_permitted", _denied)
        await d.on_interaction(_press(custom_id))

        assert providers[0].cancelled == 1
        await finished(turn, "the stopped turn")


class TestStopWithTheRealSessionManager:
    @pytest.mark.asyncio
    async def test_the_turn_holder_the_press_checks_is_the_streaming_task(self) -> None:
        """The press checks the session manager's own turn holder, so a fake that
        names it could hide a mismatch: run the press against a real manager."""
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        class _Live(_StreamingProvider):
            async def start(self) -> None:
                return None

            async def shutdown(self) -> None:
                return None

            def is_process_alive(self) -> bool:
                return True

            def is_alive(self) -> bool:
                return True

            def context_usage_pct(self) -> float:
                return 0.0

        provider = _Live()
        manager = SessionManager(KiroCrewConfig(), provider_factory=lambda *_a, **_k: provider)
        d, _cli, _sess = _dispatcher({"u1"})
        d.sessions = manager  # type: ignore[assignment]
        cli = _Client()
        d.client = cli  # type: ignore[assignment]
        turn = asyncio.create_task(d.handle_message(_inbound("hello")))
        custom_id = await _live_stop(cli)

        await d.on_interaction(_press(custom_id))

        assert provider.cancelled == 1
        await finished(turn, "the stopped turn")
        await manager.close_all()


class TestStaleStop:
    @pytest.mark.asyncio
    async def test_an_old_button_leaves_the_next_turn_running(self) -> None:
        d, cli, _sess, providers = _wired()
        first = asyncio.create_task(d.handle_message(_inbound("one")))
        old = await _live_stop(cli)
        providers[0].release.set()
        await finished(first, "the first turn")

        sent_before = len(cli.sent)
        second = asyncio.create_task(d.handle_message(_inbound("two")))
        new = await _live_stop(cli, after=sent_before)
        assert new != old

        await d.on_interaction(_press(old))

        assert providers[1].cancelled == 0
        assert not any("Stopped" in text for text, _ in cli.sent)
        # Told privately, on the press's own follow-up route, gated to this channel.
        ((method, path, payload, destination),) = cli.api_calls
        assert (method, path, destination) == ("POST", "/webhooks/app1/tok-stop", "c1")
        assert payload["flags"] == 1 << 6 and "already finished" in payload["content"]
        providers[1].release.set()
        await finished(second, "the second turn")

    @pytest.mark.asyncio
    async def test_a_press_after_a_restart_leaves_the_running_turn_alone(self, monkeypatch) -> None:
        d, cli, _sess, providers = _wired()
        turn = asyncio.create_task(d.handle_message(_inbound("hello")))
        custom_id = await _live_stop(cli)
        # A restart starts with an empty registry.
        monkeypatch.setattr(discord_renderer, "_STOP_TURNS", OrderedDict())

        await d.on_interaction(_press(custom_id))

        assert providers[0].cancelled == 0
        providers[0].release.set()
        await finished(turn, "the released turn")

    @pytest.mark.asyncio
    async def test_a_reset_session_held_by_a_new_turn_is_left_running(self) -> None:
        """A hard reset drops the old session without waiting for its turn, so the
        old renderer has not finalized yet while a new turn holds the same key."""
        d, cli, sess, providers = _wired()
        turn = asyncio.create_task(d.handle_message(_inbound("hello")))
        custom_id = await _live_stop(cli)
        replacement = asyncio.create_task(asyncio.sleep(0))
        sess.holder = replacement

        await d.on_interaction(_press(custom_id))

        assert providers[0].cancelled == 0
        await finished(replacement, "the replacement turn's stand-in task")
        sess.holder = turn
        providers[0].release.set()
        await finished(turn, "the released turn")

    @pytest.mark.asyncio
    async def test_a_message_waiting_behind_the_turn_does_not_disable_its_stop(self) -> None:
        """A second message builds and publishes its renderer before it waits for the
        session, so the session's published renderer is not the running turn's."""
        d, cli, _sess, providers = _wired()
        turn = asyncio.create_task(d.handle_message(_inbound("hello")))
        custom_id = await _live_stop(cli)
        key = d._session_key("u1")
        old = d._active_renderers[key]
        d._active_renderers[key] = DiscordRenderer(cli, "c1", DISCORD_CAPABILITIES)  # type: ignore[arg-type]

        await d.on_interaction(_press(custom_id))

        assert providers[0].cancelled == 1
        d._active_renderers[key] = old
        await finished(turn, "the stopped turn")

    @pytest.mark.asyncio
    async def test_an_old_button_on_a_denied_channel_is_dropped_quietly(
        self, monkeypatch, caplog
    ) -> None:
        """Only a live nonce claims the pure-cancel exemption, so a stale press on a
        denied channel is dropped like any other press there, and the audit log does
        not record a cancellation that never happened."""
        d, cli, _sess, providers = _wired()
        first = asyncio.create_task(d.handle_message(_inbound("one")))
        old = await _live_stop(cli)
        providers[0].release.set()
        await finished(first, "the first turn")
        from kiro_crew.messaging import dispatch

        async def _denied(_channel_type: str) -> bool:
            return False

        monkeypatch.setattr(dispatch, "channel_inbound_permitted", _denied)
        monkeypatch.setattr(td_mod, "channel_inbound_permitted", _denied)

        with caplog.at_level("INFO"):
            await d.on_interaction(_press(old))

        assert cli.api_calls == []
        assert "cancellation allowed" not in caplog.text
        assert "denied by channels governance policy" in caplog.text

    @pytest.mark.asyncio
    async def test_a_nonce_pressed_from_another_channel_stops_nothing(self) -> None:
        d, cli, _sess, providers = _wired()
        turn = asyncio.create_task(d.handle_message(_inbound("hello")))
        custom_id = await _live_stop(cli)

        await d.on_interaction(_press(custom_id, channel="c-other"))

        assert providers[0].cancelled == 0
        providers[0].release.set()
        await finished(turn, "the released turn")


def _renderer(cli: _Client | None = None) -> tuple[DiscordRenderer, _Client]:
    client = cli or _Client()
    r = DiscordRenderer(client, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
    # Bound once the turn holds its session; only such a turn offers a Stop.
    r.bind_context_source(FakeProvider())  # type: ignore[arg-type]
    return r, client


def _carrying_stop(cli: _Client) -> set[str]:
    """Message ids whose LAST confirmed components still show a Stop button."""
    shown: dict[str, Any] = {}
    for index, (_text, components) in enumerate(cli.sent):
        shown[str(101 + index)] = components
    for mid, components in cli.landed_edits:
        if components is not None:
            shown[mid] = components
    for mid, components in cli.component_edits:
        shown[mid] = components
    return {mid for mid, components in shown.items() if _stop_ids(components)}


class TestStopLifecycle:
    @pytest.mark.asyncio
    async def test_only_a_turn_holding_its_session_offers_stop(self) -> None:
        cli = FakeClient()
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        await r.on_text_chunk("a hook auto-reply")
        assert cli.sent and not _stop_ids(cli.sent[0][1])
        await r.on_done()

    @pytest.mark.asyncio
    async def test_live_edits_keep_the_button_and_the_finish_retires_it(self) -> None:
        r, cli = _renderer()
        await r.on_text_chunk("Hello")
        (stop_id,) = _stop_ids(cli.sent[0][1])
        assert stop_turn_target(stop_id[2:]) == ("sk", "chan1", asyncio.current_task())
        r._last_edit = -100.0
        await r.on_text_chunk(" world")
        # A live edit passes no components, so Discord keeps the button.
        assert cli.edits and cli.edits[-1][2] is None
        await r.on_done()
        assert cli.edits[-1][2] == []
        assert stop_turn_target(stop_id[2:]) is None and _carrying_stop(cli) == set()

    @pytest.mark.asyncio
    async def test_a_length_rotation_moves_stop_to_the_new_part(self) -> None:
        r, cli = _renderer()
        await r.on_text_chunk("para one. " * 20)
        (stop_id,) = _stop_ids(cli.sent[0][1])
        first_mid = "101"
        # Past the cap: the open part seals and the remainder opens a new bubble.
        r._last_edit = -100.0
        await r.on_text_chunk("\n\n" + "more words here. " * 200)
        sealed = [components for mid, _t, components in cli.edits if mid == first_mid]
        assert sealed and sealed[-1] == []
        opened = [components for _t, components in cli.sent[1:] if _stop_ids(components)]
        assert opened and _stop_ids(opened[0]) == [stop_id]
        assert len(_carrying_stop(cli)) == 1 and first_mid not in _carrying_stop(cli)
        await r.on_done()
        assert _carrying_stop(cli) == set()

    @pytest.mark.asyncio
    async def test_twenty_five_options_finish_with_five_rows_and_no_stop(self) -> None:
        r, cli = _renderer()
        await r.on_text_chunk("Pick one.")
        assert _stop_ids(cli.sent[0][1])
        labels = " | ".join(f"Choice {i}" for i in range(25))
        await r.on_text_chunk(f"\n\n[OPTIONS: {labels}]")
        await r.on_done()
        final = cli.edits[-1][2]
        assert len(final) == 5 and all(len(row["components"]) == 5 for row in final)
        assert _stop_ids(final) == [] and _carrying_stop(cli) == set()

    @pytest.mark.asyncio
    async def test_the_placeholder_finish_clears_the_button(self) -> None:
        r, cli = _renderer()
        # Only a tool line streams, so the bubble opens with no answer text and
        # on_done takes its placeholder branch.
        await r.on_tool_call("t1", "shell")
        assert _stop_ids(cli.sent[0][1])
        await r.on_done()
        assert cli.edits[-1][0] == "101" and cli.edits[-1][2] == []
        assert _carrying_stop(cli) == set()

    @pytest.mark.asyncio
    async def test_close_on_an_error_clears_the_button_and_retires_the_nonce(self) -> None:
        r, cli = _renderer()
        await r.on_text_chunk("partial answer")
        (stop_id,) = _stop_ids(cli.sent[0][1])
        await r.close()
        assert cli.edits[-1][2] == []
        assert stop_turn_target(stop_id[2:]) is None and _carrying_stop(cli) == set()

    @pytest.mark.asyncio
    async def test_a_failed_seal_edit_clears_the_old_bubble(self) -> None:
        r, cli = _renderer()
        await r.on_text_chunk("an answer")
        (stop_id,) = _stop_ids(cli.sent[0][1])
        cli.edit_ok = False
        await r.on_done()
        assert stop_turn_target(stop_id[2:]) is None
        # The dispatcher always closes the renderer after the turn.
        await r.close()
        # The final text went out as a fresh send; the old bubble lost its button.
        assert ("101", []) in cli.component_edits
        assert _carrying_stop(cli) == set()
        assert stop_turn_target(stop_id[2:]) is None

    @pytest.mark.asyncio
    async def test_a_failed_mid_turn_seal_still_leaves_one_bubble_with_stop(self) -> None:
        r, cli = _renderer()
        await r.on_text_chunk("before the steer")
        cli.edit_ok = False
        # The steer seal's edit fails, so the segment goes out as a fresh send.
        await r.on_steer_consumed()
        r._last_edit = -100.0
        await r.on_text_chunk("after the steer")
        assert len(_carrying_stop(cli)) == 1 and "101" not in _carrying_stop(cli)
        cli.edit_ok = True
        await r.on_done()
        await r.close()
        assert _carrying_stop(cli) == set()

    @pytest.mark.asyncio
    async def test_a_raising_finish_still_retires_the_nonce(self, monkeypatch) -> None:
        r, cli = _renderer()
        await r.on_text_chunk("an answer")
        (stop_id,) = _stop_ids(cli.sent[0][1])

        async def _boom(*_a: Any, **_k: Any) -> bool:
            raise RuntimeError("seal exploded")

        monkeypatch.setattr(r, "_seal_current", _boom)
        # The close() error path: the turn never reached on_done, and its finalize
        # raises.
        with pytest.raises(RuntimeError):
            await r.close()
        assert stop_turn_target(stop_id[2:]) is None
        assert ("101", []) in cli.component_edits
