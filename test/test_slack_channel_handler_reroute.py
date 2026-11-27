"""Ownership re-routing keeps the channel lock on the outer route only."""

from __future__ import annotations

import asyncio

import pytest
import test_slack_channel_handler as _channel
from slack_transport_dispatch_helpers import RecordingSlackClient
from test_slack_channel_handler import A_TS, B_TS, CHAN, _bot_slot, _send, _Sessions, _user_rows

from kiro_crew.slack import channel_handler as ch
from kiro_crew.slack import handler

# Assignment exposes the shared fixtures without redefining imported names.
ds = _channel.ds
channel_bot = _channel.channel_bot
run_chat = _channel.run_chat
bots = _channel.bots
audit = _channel.audit
_store = _channel._store


@pytest.mark.parametrize("relink_during", ["window", "images"])
def test_reroute_to_channel_bot_releases_lock_for_following_message(
    ds, channel_bot, run_chat, monkeypatch, relink_during
):
    slot = _bot_slot(ds)
    other = ds.get_or_create_slot(name="chat-other")
    ds.link_slack(other.key, A_TS, CHAN)
    slack = RecordingSlackClient()
    sessions = _Sessions()

    def _relink():
        assert ch.lock(CHAN).locked()
        assert ds.get_linked_slot(A_TS) is other
        ds.link_slack(slot.key, A_TS, CHAN)

    async def _load_window(target):
        assert target is other
        target._lazy_window = None
        _relink()

    async def _copy_images(state, target, text, *, still_current):
        assert state is ds
        assert still_current()
        if target is other:
            assert relink_during == "images"
            _relink()
            assert not still_current()
            return None
        assert target is slot
        return text

    if relink_during == "window":
        other._lazy_window = object()
        monkeypatch.setattr(handler, "load_window_off_loop", _load_window)
    monkeypatch.setattr(handler, "store_linked_thread_images", _copy_images)

    async def _scenario():
        await asyncio.wait_for(
            _send(slack, sessions, "rerouted", thread_ts=A_TS, msg_ts="1700000100.000200"),
            timeout=30,
        )
        assert not ch.lock(CHAN).locked()
        await asyncio.wait_for(asyncio.gather(*ds._background_tasks), timeout=30)
        await asyncio.wait_for(_send(slack, sessions, "following", msg_ts=B_TS), timeout=30)
        assert not ch.lock(CHAN).locked()
        await asyncio.wait_for(asyncio.gather(*ds._background_tasks), timeout=30)

    asyncio.run(_scenario())

    assert _user_rows(other) == []
    assert _user_rows(slot) == ["rerouted", "following"]
    assert [call.args[1] for call in run_chat.await_args_list] == [slot, slot]
    assert [call.args[2] for call in run_chat.await_args_list] == ["rerouted", "following"]
    assert ds.get_linked_slot(B_TS) is slot
    assert sessions.calls == []
