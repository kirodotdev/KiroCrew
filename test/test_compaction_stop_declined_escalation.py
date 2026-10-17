"""The forcing repeat behind a declined Stop is armed only once its warning lands.

The decline markers and the parked queue stay bounded, the entries a forced Stop
detaches are parked or handed to the successor instead of being dropped, and the
cancelled notice names the user's Stop as the cause of the restart.
"""

from __future__ import annotations

import asyncio

import pytest
from compaction_stop_declined_helpers import _clear_channel_decline_markers  # noqa: F401
from compaction_stop_declined_helpers import KEY, _run_slack_stop, _setup, _slack_orch

from kiro_crew.session_compaction import COMPACT_OUTCOME_CANCELLED


def test_a_parked_entry_past_the_field_bounds_is_refused_not_retained(caplog):
    from unittest.mock import MagicMock

    from kiro_crew import session_lifecycle as sl

    sessions = MagicMock()
    sessions._fold_key = lambda k: k
    big_text = ("t1", "x" * (sl.PARKED_ENTRY_TEXT_MAX_CHARS + 1), {})
    big_kwargs = ("t2", "ok", {"blob": "y" * (sl.PARKED_ENTRY_KWARGS_MAX_CHARS + 1)})
    big_ts = ("t" * (sl.PARKED_ENTRY_TS_MAX_CHARS + 1), "ok", {})
    fine = ("t3", "ok", {})
    with caplog.at_level("WARNING", logger=sl.__name__):
        sl._park_queue(sessions, "k", (big_text, big_kwargs, big_ts, fine))
    assert sl._parked_queue["k"] == [fine]
    assert sessions.clear_queue.call_count == 3, "refused entries have their files unlinked"
    assert sum("field bounds" in r.message for r in caplog.records) == 3


@pytest.mark.asyncio
async def test_a_reset_that_raises_after_the_pop_parks_the_detached_queue():
    """The restore has no session to land on once the reset popped it; the
    detached entries are parked for the next start instead of unwinding away."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import session_lifecycle as sl

    sessions = MagicMock()
    bob = ("ts-b", "bob's", {})
    sessions.detach_queue = MagicMock(return_value=(bob,))
    sessions.stop_turn = AsyncMock(side_effect=RuntimeError("shutdown raised after the pop"))
    sessions.has_session = MagicMock(return_value=False)
    with pytest.raises(RuntimeError):
        await sl.force_stop_keeping_others(sessions, "k", lambda _kw: False)
    sessions.restore_queue.assert_not_called()
    assert sl._parked_queue["k"] == [bob]
    # With the session still there, the ordinary restore is taken instead.
    sl._parked_queue.clear()
    sessions.has_session = MagicMock(return_value=True)
    with pytest.raises(RuntimeError):
        await sl.force_stop_keeping_others(sessions, "k", lambda _kw: False)
    sessions.restore_queue.assert_called_once_with("k", (bob,))
    assert "k" not in sl._parked_queue


@pytest.mark.asyncio
async def test_clear_queue_only_unlinks_the_handles_even_when_the_session_is_gone(tmp_path):
    """A hard stop pops the session between the detach and the clear; the
    detached entries were never on the popped queue, so this is the one place
    their files can be unlinked."""
    mgr, key, compact, order, notices = await _setup()
    tmp = tmp_path / "gone.png"
    tmp.write_bytes(b"x")
    assert mgr.enqueue(key, "ts-1", "at press", force=True, image_temp_paths=[str(tmp)])
    taken = mgr.detach_queue(key)
    assert await mgr.stop_turn(KEY, force=True) == "hard"
    mgr.clear_queue(key, only=taken)
    assert not tmp.exists()
    await mgr.close_all()


def test_decline_markers_are_count_bounded_and_the_overflow_is_counted(caplog):
    from kiro_crew import session_lifecycle as sl

    with caplog.at_level("WARNING", logger=sl.__name__):
        for i in range(sl.STOP_DECLINED_MARKERS_MAX + 3):
            sl.note_stop_declined(f"k{i}", "u", now=100.0)
    assert len(sl._stop_declined_markers) == sl.STOP_DECLINED_MARKERS_MAX
    # Oldest evicted, newest kept; the evicted presser's repeat is declined again.
    assert sl.consume_stop_declined("k0", "u", now=110.0) is False
    assert sl.consume_stop_declined(f"k{sl.STOP_DECLINED_MARKERS_MAX + 2}", "u", now=110.0)
    assert sum("evicted" in r.message for r in caplog.records) == 3
    # flake-ok: pre-existing read of the module-wide drop counter, which no test before it in collection order advances, moved verbatim; tracked for a de-flake pass
    assert "3 dropped so far" in caplog.records[-1].message


def test_expired_decline_markers_are_swept_on_the_next_write():
    from kiro_crew import session_lifecycle as sl

    for i in range(10):
        sl.note_stop_declined(f"k{i}", "u", now=100.0)
    sl.note_stop_declined("fresh", "u", now=100.0 + sl.STOP_DECLINED_ESCALATION_SECS)
    assert list(sl._stop_declined_markers) == [("fresh", "u")]


def test_a_decline_marker_key_past_the_string_bound_is_refused_not_retained(caplog):
    from kiro_crew import session_lifecycle as sl

    long_key = "k" * (sl.STOP_DECLINED_KEY_MAX_CHARS + 1)
    with caplog.at_level("WARNING", logger=sl.__name__):
        sl.note_stop_declined(long_key, "u", now=100.0)
        sl.note_stop_declined("k", "u" * (sl.STOP_DECLINED_KEY_MAX_CHARS + 1), now=100.0)
    assert sl._stop_declined_markers == {}
    assert sl.consume_stop_declined(long_key, "u", now=101.0) is False
    assert sum("refused" in r.message for r in caplog.records) == 2


def test_the_escalation_window_is_one_constant_for_dashboard_and_channels():
    from kiro_crew import session_lifecycle as sl
    from kiro_crew.dashboard import slot_projection as sp

    assert sp.STOP_DECLINED_ESCALATION_SECS is sl.STOP_DECLINED_ESCALATION_SECS


@pytest.mark.asyncio
async def test_a_cancelled_successor_handoff_parks_the_entries_before_unwinding():
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import session_lifecycle as sl

    sessions = MagicMock()
    sessions._fold_key = lambda k: k
    sessions.get_or_create = AsyncMock(side_effect=asyncio.CancelledError())
    bob = ("ts-b", "bob's", {})
    with pytest.raises(asyncio.CancelledError):
        await sl.hand_queue_to_successor(sessions, "k", (bob,))
    assert sl._parked_queue["k"] == [bob]


@pytest.mark.asyncio
async def test_clear_queue_only_drops_the_detached_entries():
    mgr, key, compact, order, notices = await _setup()
    assert mgr.enqueue(key, "ts-1", "at press", force=True)
    taken = mgr.detach_queue(key)
    assert mgr.enqueue(key, "ts-2", "admitted later", force=True)
    mgr.restore_queue(key, taken)
    assert [e[0] for e in mgr._sessions[key].queue] == ["ts-1", "ts-2"], "restored ahead"
    mgr.clear_queue(key, only=taken)
    left = [e[0] for e in mgr._sessions[key].queue]
    assert left == ["ts-2"]
    await mgr.close_all()


# -- 4. the notices --


def test_the_cancelled_notice_names_the_users_stop_as_the_cause_of_the_restart():
    from kiro_crew.dashboard.chat_compaction_notice import notice_text
    from kiro_crew.dashboard.state import (
        _AUTO_COMPACT_CANCELLED_NOTICE,
        _AUTO_COMPACT_FAILED_NOTICE,
        _AUTO_RECYCLE_NOTICE,
    )

    dashboard = _AUTO_COMPACT_CANCELLED_NOTICE.format(pct=87)
    assert "Stop" in dashboard
    assert "87% of the context limit" in dashboard, "the percentage names its referent"
    # The only Stop that reaches a compacting session is the forced one, and a
    # forced stop resets the session: the notice says so, and says what the
    # next reply is built from.
    assert dashboard.startswith("⏹ Your forced Stop"), "leads with the actor"
    assert "was restarted" in dashboard, "one verb with the restart notices"
    assert "reset" not in dashboard
    assert "kept running" not in dashboard
    assert "recent excerpt" in dashboard
    assert "didn't succeed" not in dashboard, "nothing failed; the user ended it"
    assert dashboard != _AUTO_COMPACT_FAILED_NOTICE.format(pct=87)
    assert dashboard != _AUTO_RECYCLE_NOTICE.format(pct=87)

    channel = notice_text("slack", 87.0, success=False, outcome=COMPACT_OUTCOME_CANCELLED)
    assert "stop" in channel.lower()
    assert "87% of the context limit" in channel
    assert channel.startswith("Your forced stop"), "leads with the actor"
    assert "was restarted" in channel
    assert "reset" not in channel
    assert "kept running" not in channel
    assert "`!new`" in channel, "the channel's own start-fresh command"
    assert "{" not in channel, "every placeholder filled"
    assert channel != notice_text("slack", 87.0, success=False, outcome="compacted")


# ── the decline is delivered before the escalation is armed ──────────────────


def test_decline_stop_arms_only_after_the_warning_is_delivered():
    """The marker is what makes the next press a hard reset; the sentence is what
    makes that press informed. Send first, arm second."""
    import kiro_crew.session_lifecycle as sl

    order: list[str] = []

    async def _deliver() -> bool:
        order.append("sent")
        assert sl._stop_declined_markers == {}, "nothing armed while the send is in flight"
        return True

    assert asyncio.run(sl.decline_stop("k", "u", _deliver, now=100.0)) is True
    assert order == ["sent"]
    assert sl.consume_stop_declined("k", "u", now=101.0) is True


def test_decline_stop_arms_nothing_when_the_send_raises():
    """A transport error is ordinary, and it makes the retry MORE likely: a user
    who saw no answer presses again within seconds. That retry must be a first
    press, not a silent session reset."""
    import kiro_crew.session_lifecycle as sl

    async def _deliver() -> bool:
        raise RuntimeError("transport down")

    with pytest.raises(RuntimeError):
        asyncio.run(sl.decline_stop("k", "u", _deliver, now=100.0))
    assert sl._stop_declined_markers == {}
    assert sl.consume_stop_declined("k", "u", now=101.0) is False


def test_decline_stop_arms_nothing_when_the_send_reports_it_could_not_run():
    """Some sends are conditional on a client or a message id rather than on an
    exception, and some swallow their own transport error. Those report False."""
    import kiro_crew.session_lifecycle as sl

    async def _no_client() -> bool:
        return False

    assert asyncio.run(sl.decline_stop("k", "u", _no_client, now=100.0)) is False
    assert sl._stop_declined_markers == {}
    assert sl.consume_stop_declined("k", "u", now=101.0) is False


@pytest.mark.parametrize("reported", [None, "", 0, "ts-1", object()])
def test_decline_stop_arms_nothing_unless_delivery_is_reported_as_true(reported):
    """Only ``True`` arms. ``None`` is what a send that swallows its own error
    returns when nothing landed, so reading an unknown value as success would
    reset the session of a user who was never warned; anything that is not the
    explicit yes leaves the next press a first press."""
    import kiro_crew.session_lifecycle as sl

    async def _sent():
        return reported

    assert asyncio.run(sl.decline_stop("k", "u", _sent, now=100.0)) is False
    assert sl._stop_declined_markers == {}
    assert sl.consume_stop_declined("k", "u", now=101.0) is False


def test_the_shared_channel_decline_sends_before_it_arms():
    """``stop_running_turn`` owns the send for exactly this reason: a channel
    that posted the text after the arm could leave the escalation live."""
    from unittest.mock import AsyncMock, MagicMock

    import kiro_crew.session_lifecycle as sl
    from kiro_crew.messaging.commands import STOP_REPLY_COMPACTING, stop_running_turn

    sessions = MagicMock()
    sessions.is_compacting = MagicMock(return_value=True)
    queue = MagicMock()
    queue.lock = asyncio.Lock()
    queue.finish_cancelled_locked = AsyncMock()

    sent: list[str] = []

    async def _deliver(text: str) -> int:
        sent.append(text)
        assert sl._stop_declined_markers == {}, "armed before the user was told"
        return 4242  # what a transport hands back: the id of the message it sent

    reply = asyncio.run(
        stop_running_turn(
            sessions,
            "telegram:1",
            queue=queue,
            surface=MagicMock(label="telegram"),
            owner="u1",
            deliver=_deliver,
        )
    )
    assert reply == STOP_REPLY_COMPACTING
    assert sent == [STOP_REPLY_COMPACTING]
    # flake-ok: pre-existing decline marker armed and read back to back inside the real-clock escalation window, moved verbatim; tracked for a de-flake pass
    assert sl.consume_stop_declined("telegram:1", "u1") is True


def test_the_shared_channel_decline_arms_nothing_when_its_send_fails():
    from unittest.mock import AsyncMock, MagicMock

    import kiro_crew.session_lifecycle as sl
    from kiro_crew.messaging.commands import stop_running_turn

    sessions = MagicMock()
    sessions.is_compacting = MagicMock(return_value=True)
    queue = MagicMock()
    queue.lock = asyncio.Lock()
    queue.finish_cancelled_locked = AsyncMock()

    async def _deliver(_text: str) -> int:
        raise RuntimeError("telegram 502")

    with pytest.raises(RuntimeError):
        asyncio.run(
            stop_running_turn(
                sessions,
                "telegram:1",
                queue=queue,
                surface=MagicMock(label="telegram"),
                owner="u1",
                deliver=_deliver,
            )
        )
    assert sl._stop_declined_markers == {}, "the retry is a first press again"
    assert sessions.note_stop.call_count == 0
    sessions.clear_queue.assert_not_called()


def test_the_shared_channel_stop_delivers_every_reply_itself():
    """The non-decline replies go through the same send, so a channel has one
    path and cannot answer a Stop twice."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.messaging.commands import STOP_REPLY_CANCELLED, stop_running_turn

    sessions = MagicMock()
    sessions.is_compacting = MagicMock(return_value=False)
    sessions.is_busy = MagicMock(return_value=True)
    sessions.get_provider = MagicMock(return_value=MagicMock(cancel=AsyncMock()))
    queue = MagicMock()
    queue.lock = asyncio.Lock()
    queue.finish_cancelled_locked = AsyncMock()
    deliver = AsyncMock()

    reply = asyncio.run(
        stop_running_turn(
            sessions,
            "telegram:1",
            queue=queue,
            surface=MagicMock(label="telegram"),
            owner="u1",
            deliver=deliver,
        )
    )
    assert reply == STOP_REPLY_CANCELLED
    deliver.assert_awaited_once_with(STOP_REPLY_CANCELLED)


def test_slack_stop_arms_nothing_when_the_decline_post_never_lands():
    """``post_message`` hands back the ts of what landed. A falsy one is a warning
    this member never saw, so their retry stays a first press rather than the
    forcing second press the unseen warning would have promised."""
    from unittest.mock import AsyncMock, MagicMock

    import kiro_crew.session_lifecycle as sl

    sl._stop_declined_markers.clear()
    orch, _task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=True)
    orch.sessions.stop_turn = AsyncMock()
    orch.slack.post_message = AsyncMock(return_value=None)
    _run_slack_stop(orch)
    orch.slack.post_message.assert_awaited()
    assert sl._stop_declined_markers == {}, "the retry is a first press again"
    assert sl.consume_stop_declined("100.0", "U123") is False
    sl._stop_declined_markers.clear()


def test_the_shared_channel_decline_arms_nothing_when_the_send_reports_nothing_landed():
    """A transport whose send swallows its own error hands back no message id. That
    is not a delivered warning, so the escalation stays unarmed."""
    from unittest.mock import AsyncMock, MagicMock

    import kiro_crew.session_lifecycle as sl
    from kiro_crew.messaging.commands import STOP_REPLY_COMPACTING, stop_running_turn

    sl._stop_declined_markers.clear()
    sessions = MagicMock()
    sessions.is_compacting = MagicMock(return_value=True)
    queue = MagicMock()
    queue.lock = asyncio.Lock()
    queue.finish_cancelled_locked = AsyncMock()
    sent: list[str] = []

    async def _deliver(text: str) -> None:
        sent.append(text)  # logged its own failure and returned nothing

    reply = asyncio.run(
        stop_running_turn(
            sessions,
            "telegram:2",
            queue=queue,
            surface=MagicMock(label="telegram"),
            owner="u1",
            deliver=_deliver,
        )
    )
    assert reply == STOP_REPLY_COMPACTING
    assert sent == [STOP_REPLY_COMPACTING]
    assert sl._stop_declined_markers == {}
    assert sl.consume_stop_declined("telegram:2", "u1") is False
    sessions.clear_queue.assert_not_called()
