"""What happens to a Discord message after it is handed to a BUSY resumed dashboard slot.

``test_discord_resumed_busy.py`` pins the routing decision: the resumed branch hands
the message to the slot's own steer path or queue, records the audience fence the
peer-steer path records, confirms in the channel, and keeps the refusal for the
cases the slot cannot take. This module pins the hand-off's later life: the drop
notice a DM gets when the drain drops its queued entry (and the recovery requeues
that keep, or refuse, that address), the channel recipient stamp, the bounded fence
set, the drop notice's send ladder off the loop thread, a requeued channel steer's
provenance, a handler cancelled inside the steer RPC, the channel origin a later
directive is stamped with, a close racing the steer, and the live queue's bound.

The dashboard state is the real ``DashboardState`` with a real slot, as in the routing
module: the hand-off writes the slot's queue, its pending-steer bookkeeping and its
audience fences, which a hand-rolled fake would not shape the way production does.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord_resumed_busy_helpers
import pytest
from chat_test_helpers import _make_state
from discord_resumed_busy_helpers import (
    _REFUSAL,
    _SECRET,
    _TYPED,
    _bound_to_busy_dashboard,
    _busy,
    _sent_texts,
    _steerable,
)
from test_discord_sessions import _message

from kiro_crew.dashboard import channel_handoff as ch
from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.session_directive_apply import DirectiveOutcome
from kiro_crew.history import HUMAN_TURN_META_KEY

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_isolated_home = discord_resumed_busy_helpers._isolated_home


def _discord_transport(*, allowed: set[str] = frozenset({"u1"})) -> SimpleNamespace:
    """A registered Discord transport shaped like the send ladder consults it."""
    return SimpleNamespace(
        channel_type="discord",
        capabilities=SimpleNamespace(
            supports_proactive_send=True, max_message_chars=2000, supports_session_resume=True
        ),
        send_message=AsyncMock(return_value="mid-1"),
        # Discord's own rule for a DM route: the roster is consulted through the
        # principal, never through the DM channel id.
        may_send_to=lambda conversation_id, thread_id=None, principal="": principal in allowed,
    )


async def _settle_background(state) -> None:
    for task in list(getattr(state, "_background_tasks", ())):
        # flake-ok: pre-existing await of the state's background tasks, which finish without outside input; moved verbatim, tracked for a de-flake pass
        await task


@pytest.mark.asyncio
async def test_a_dropped_queued_message_is_reported_back_to_the_dm(tmp_path) -> None:
    """The DM was told "queued"; when the drain drops the entry, the DM is told that too.

    A dashboard sender gets its notice on its own transcript; a channel sender
    reads neither the target's transcript nor the SEL, so the drop is reported into
    the conversation that queued it, through the same governed send ladder every
    proactive channel delivery takes -- with the principal the channel authorized on
    inbound, which is what the recipient check needs and a dashboard key cannot
    supply.
    """
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    transport = _discord_transport()
    state.register_channel_transport(transport)
    await dispatcher.handle_message(_message(_TYPED))
    (entry,) = slot._queue
    stamp = entry["meta"][sc.CHANNEL_RECIPIENT_META_KEY]
    assert stamp == {"channel_type": "discord", "conversation_id": "c1", "principal": "u1"}

    # A constraint newly holds at the drain: the session gains a mirror.
    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    await _settle_background(state)

    assert slot._queue == []
    transport.send_message.assert_awaited_once()
    conversation, notice = transport.send_message.await_args.args
    assert conversation == "c1"
    assert "dropped before it ran" in notice and "will not run" in notice
    assert "send it again" in notice
    assert _SECRET not in notice, "the quoted excerpt takes the egress redaction"
    assert 'Text: "use ' in notice and "for this" in notice, "and still names the message"


@pytest.mark.asyncio
async def test_a_requeued_channel_steer_carries_the_drop_notice_address(tmp_path) -> None:
    """The recipient rides the steer's admission dict, which the requeue copies."""
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    transport = _discord_transport()
    state.register_channel_transport(transport)

    async def _requeue_instead(_msg):
        cr._requeue_unconsumed_steers(state, slot)
        return True

    steer_client.steer = AsyncMock(side_effect=_requeue_instead)
    await dispatcher.handle_message(_message("steer that the turn never took"))
    (entry,) = slot._queue
    assert entry["meta"][sc.CHANNEL_RECIPIENT_META_KEY]["conversation_id"] == "c1"

    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    await _settle_background(state)

    transport.send_message.assert_awaited_once()
    assert "steer that the turn never took" in transport.send_message.await_args.args[1]


def _dying_turn(state, monkeypatch) -> None:
    """The slot's next turn dies before any output, and nothing drains after it."""
    from kiro_crew.acp.client import AcpError

    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    acp = MagicMock()
    acp.context_usage_pct = MagicMock(return_value=10.0)

    async def _die(_msg):
        raise AcpError("ACP process exited (code=-15)")
        yield  # an async generator

    acp.stream = _die
    acp.stream_command = _die
    acp.shutdown = AsyncMock()
    state.sessions.get_or_create = AsyncMock(return_value=(acp, True, False))
    state.sessions.reset = AsyncMock()

    async def _no_drain(_state, _slot, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(cr, "_start_next_queued_turn", _no_drain)


async def _replay_queued_dm_through_recovery(
    tmp_path, monkeypatch, *, hand_row: bool, run_prefix: str = ""
):
    """Queue a DM message on the busy slot, drain it, and let its turn die.

    The drain's hand-over is reproduced: the entry leaves the queue and becomes
    the turn's row with the entry's meta, under the channel's provenance. With
    *hand_row* False the runner gets no row and finds the triggering row by
    scanning the transcript instead. A *run_prefix* is runner text in front of
    the row's words, the shape the turn's text has once the runner prepends
    context or failure notes before streaming. Returns the state, slot,
    transport, text and the recovery's requeued entry.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    transport = _discord_transport()
    state.register_channel_transport(transport)
    text = "queued, replayed, then dropped"
    await dispatcher.handle_message(_message(text))
    (entry,) = slot._queue
    slot.queue_remove_by_id(entry["id"])
    slot.task = None
    row = slot.append("user", text, "msg msg-user", meta=dict(entry["meta"]))
    _dying_turn(state, monkeypatch)

    await cr._run_chat(
        state,
        slot,
        run_prefix + text,
        _directive_user_origin=True,
        _directive_channel_origin=True,
        _commands_off=True,
        _current_message=row if hand_row else None,
    )

    (requeued,) = [q for q in slot._queue if text in (q.get("content") or "")]
    return state, slot, transport, text, requeued


@pytest.mark.asyncio
async def test_a_recovery_replayed_channel_entry_keeps_the_drop_notice_address(
    tmp_path, monkeypatch
) -> None:
    """A queued DM message whose turn dies before output is requeued verbatim by
    the runner's recovery. The requeued entry still names the DM, so when the
    retry's drain drops it the DM is told, exactly as for the first queueing."""
    state, slot, transport, text, requeued = await _replay_queued_dm_through_recovery(
        tmp_path, monkeypatch, hand_row=True
    )
    assert (requeued.get("meta") or {}).get(sc.CHANNEL_RECIPIENT_META_KEY) == {
        "channel_type": "discord",
        "conversation_id": "c1",
        "principal": "u1",
    }

    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    await _settle_background(state)

    assert [q for q in slot._queue if q.get("content") == text] == []
    transport.send_message.assert_awaited_once()
    conversation, notice = transport.send_message.await_args.args
    assert conversation == "c1"
    assert "dropped before it ran" in notice and text in notice


@pytest.mark.asyncio
async def test_a_recovery_takes_no_drop_notice_address_from_a_scanned_row(
    tmp_path, monkeypatch
) -> None:
    """A row the runner finds by scanning the transcript may have been restored
    from disk, so it is never the source of a write target: the requeue carries
    no recipient, and a later drop reports nothing to any channel."""
    state, slot, transport, text, requeued = await _replay_queued_dm_through_recovery(
        tmp_path, monkeypatch, hand_row=False
    )
    assert sc.CHANNEL_RECIPIENT_META_KEY not in (requeued.get("meta") or {})

    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    await _settle_background(state)

    transport.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_recovery_of_runner_prefixed_text_takes_no_drop_notice_address(
    tmp_path, monkeypatch
) -> None:
    """The runner can put its own text in front of the turn's words before it
    streams, and recovery requeues that longer text. That text is not the DM's
    own words, so the requeue names no recipient and a drop quotes nothing to
    the DM."""
    note = "[a background task failed; read its result from disk]\n\n"
    state, slot, transport, text, requeued = await _replay_queued_dm_through_recovery(
        tmp_path, monkeypatch, hand_row=True, run_prefix=note
    )
    assert note in requeued["content"] and requeued["content"] != text
    assert sc.CHANNEL_RECIPIENT_META_KEY not in (requeued.get("meta") or {})

    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    await _settle_background(state)

    transport.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_auth_retry_of_changed_text_takes_no_address_from_the_row_meta(
    tmp_path, monkeypatch
) -> None:
    """An auth-retry requeue hands the row's whole meta to the recovery as
    extra meta. When the turn's text differs from the row's, the addresses in
    that meta are not carried: only the row comparison may write them."""
    from kiro_crew.acp.client import AcpAuthRequired

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    dm = sc.channel_recipient_meta("discord", "c1", "u1")
    text = "a completion the DM's session queued"
    row = slot.append("subagent", text, "msg msg-subagent", meta=dict(dm))
    _dying_turn(state, monkeypatch)

    async def _needs_login(_msg):
        raise AcpAuthRequired("login required")
        yield  # an async generator

    acp, _, _ = await state.sessions.get_or_create()
    acp.stream = _needs_login
    acp.stream_command = _needs_login

    prefixed = "[runner note]\n\n" + text
    await cr._run_chat(state, slot, prefixed, _turn_actor="subagent", _current_message=row)

    requeued = [q for q in slot._queue if q.get("content") == prefixed]
    assert requeued, f"expected the auth-retry requeue, queue={slot._queue}"
    assert sc.CHANNEL_RECIPIENT_META_KEY not in (requeued[0].get("meta") or {})


@pytest.mark.asyncio
async def test_a_recovery_replayed_cross_session_delivery_keeps_its_sender(
    tmp_path, monkeypatch
) -> None:
    """The sending-session stamp is the other drop-notice address on a queued
    entry, and a verbatim recovery requeue keeps it the same way."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    sender = state.get_or_create_slot("sender-1")
    slot = state.get_or_create_slot("target-1")
    origin = sc.send_origin_meta(state, sender.key)
    assert origin, "the sender has a tab identity to stamp"
    text = "a delivery from another session"
    row = slot.append("user", text, "msg msg-user", meta=dict(origin))
    _dying_turn(state, monkeypatch)

    await cr._run_chat(state, slot, text, _current_message=row)

    (requeued,) = [q for q in slot._queue if q.get("content") == text]
    assert (requeued.get("meta") or {}).get(sc.SEND_ORIGIN_META_KEY) == origin[
        sc.SEND_ORIGIN_META_KEY
    ]


@pytest.mark.parametrize("second_sender", ["dashboard", "same-dm"])
@pytest.mark.asyncio
async def test_a_merged_row_keeps_an_address_only_its_every_entry_shares(
    tmp_path, monkeypatch, second_sender
) -> None:
    """A merged row's text is every entry's, and a drop notice quotes that text
    to the row's address. So a DM entry merged with a dashboard entry leaves the
    row with no address, while two entries from the same DM keep it."""
    from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.subagents = None
    slot = state.get_or_create_slot("busy-chat")
    dm = sc.channel_recipient_meta("discord", "c1", "u1")[sc.CHANNEL_RECIPIENT_META_KEY]

    queue_for_next_turn(state, slot, "from the DM", channel_recipient=dm)
    queue_for_next_turn(
        state,
        slot,
        "second message",
        channel_recipient=dm if second_sender == "same-dm" else None,
    )

    cfg = MagicMock()
    cfg.load.return_value.dashboard.merge_queued_messages = True
    monkeypatch.setattr(cr, "KiroCrewConfig", cfg)
    with (
        patch.object(cr, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(cr, "_run_chat", return_value=MagicMock()),
    ):
        assert await cr._start_next_queued_turn(state, slot) is True

    row = [m for m in slot.messages if m.get("role") in ("user", "inject")][-1]
    assert "2 queued messages merged" in row.get("content", "")
    kept = (row.get("meta") or {}).get(sc.CHANNEL_RECIPIENT_META_KEY)
    assert kept == (dm if second_sender == "same-dm" else None)


@pytest.mark.asyncio
async def test_a_dm_entry_queued_beside_a_dashboard_entry_is_told_of_its_own_drop(
    tmp_path, monkeypatch
) -> None:
    """With merging on, a DM entry queued next to a dashboard entry is still told
    when the drain drops it: the drop is decided per entry, before any merge, and
    the notice quotes only the DM's own words."""
    from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    transport = _discord_transport()
    state.register_channel_transport(transport)
    await dispatcher.handle_message(_message("the DM's own words"))
    queue_for_next_turn(state, slot, "the dashboard's words")
    assert len(slot._queue) == 2

    cfg = MagicMock()
    cfg.load.return_value.dashboard.merge_queued_messages = True
    monkeypatch.setattr(cr, "KiroCrewConfig", cfg)
    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    with (
        patch.object(cr, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(cr, "_run_chat", return_value=MagicMock()),
    ):
        await cr._start_next_queued_turn(state, slot)
    await _settle_background(state)

    transport.send_message.assert_awaited_once()
    conversation, notice = transport.send_message.await_args.args
    assert conversation == "c1"
    assert "dropped before it ran" in notice and "the DM's own words" in notice
    assert "the dashboard's words" not in notice


@pytest.mark.asyncio
async def test_a_revoked_recipient_gets_no_drop_notice(tmp_path) -> None:
    """The ladder's recipient re-check stands between the stamp and the send."""
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    transport = _discord_transport(allowed=set())
    state.register_channel_transport(transport)
    await dispatcher.handle_message(_message("queued, then revoked"))

    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    await _settle_background(state)

    assert slot._queue == [], "the drop itself is the authorization decision and stands"
    transport.send_message.assert_not_awaited()


def _two_roster_transport(*, allowed_threads: set[str], allowed: set[str]) -> SimpleNamespace:
    """A Discord transport with the REAL two-arm recipient rule: a thread route
    answers from the thread roster, anything else from the user roster via the
    principal -- and a thread not on its roster falls through to that DM arm."""
    return SimpleNamespace(
        channel_type="discord",
        capabilities=SimpleNamespace(
            supports_proactive_send=True, max_message_chars=2000, supports_session_resume=True
        ),
        send_message=AsyncMock(return_value="mid-1"),
        may_send_to=lambda conversation_id, thread_id=None, principal="": (
            conversation_id in allowed_threads or (bool(principal) and principal in allowed)
        ),
    )


@pytest.mark.asyncio
async def test_a_thread_revoked_before_the_drain_gets_no_drop_notice(tmp_path) -> None:
    """Red-first: a thread route's drop-notice address carries no principal.

    ``may_send_to`` answers a thread from the thread roster and falls through to
    the DM arm only when the thread is not on it. Stamping the user id as the
    principal made a thread the operator removed from ``allowed_thread_ids`` pass
    that DM arm, so the notice -- excerpt included -- posted into the revoked
    thread. Only a DM route supplies an identity.
    """
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    allowed_threads = {"t9"}
    transport = _two_roster_transport(allowed_threads=allowed_threads, allowed={"u1"})
    state.register_channel_transport(transport)

    msg = _message("posted from the thread", channel_id="t9", thread_id="t9")
    await dispatcher._handle_resumed_busy("dashboard:chat-1", msg, msg.text, "queue")
    (entry,) = slot._queue
    stamp = entry["meta"][sc.CHANNEL_RECIPIENT_META_KEY]
    assert stamp == {"channel_type": "discord", "conversation_id": "t9", "principal": ""}

    # The operator removes the thread from the roster before the drain runs.
    allowed_threads.clear()
    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    await _settle_background(state)

    assert slot._queue == []
    transport.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_allowed_thread_still_gets_its_drop_notice(tmp_path) -> None:
    """Counterfactual: a thread still on the roster is answered from that roster."""
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    transport = _two_roster_transport(allowed_threads={"t9"}, allowed={"u1"})
    state.register_channel_transport(transport)

    msg = _message("posted from the thread", channel_id="t9", thread_id="t9")
    await dispatcher._handle_resumed_busy("dashboard:chat-1", msg, msg.text, "queue")
    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    await _settle_background(state)

    transport.send_message.assert_awaited_once()
    assert transport.send_message.await_args.args[0] == "t9"


def test_the_channel_recipient_stamp_does_not_survive_a_restore(tmp_path) -> None:
    """It names a write target on a network surface, so an edited line cannot
    make the drain post the entry's text into a conversation of its choosing."""
    from kiro_crew.dashboard.slot_queue_repository import sanitize_restored_queue

    raw = [
        {
            "id": "q1",
            "content": "hello",
            "meta": {
                sc.CHANNEL_RECIPIENT_META_KEY: {
                    "channel_type": "discord",
                    "conversation_id": "c-anyone",
                    "principal": "u1",
                },
                "sendId": "s-1",
            },
        }
    ]
    (restored,) = sanitize_restored_queue(raw)
    assert sc.CHANNEL_RECIPIENT_META_KEY not in restored["meta"]
    assert restored["meta"]["sendId"] == "s-1"


@pytest.mark.parametrize(
    "meta",
    [
        None,
        {},
        {sc.CHANNEL_RECIPIENT_META_KEY: 17},
        {sc.CHANNEL_RECIPIENT_META_KEY: {"channel_type": "discord"}},
        {sc.CHANNEL_RECIPIENT_META_KEY: {"channel_type": "", "conversation_id": "c1"}},
        {sc.CHANNEL_RECIPIENT_META_KEY: {"channel_type": "discord", "conversation_id": 5}},
        {
            sc.CHANNEL_RECIPIENT_META_KEY: {
                "channel_type": "discord",
                "conversation_id": "c1",
                "principal": 3,
            }
        },
    ],
)
def test_channel_recipient_readers_read_junk_as_no_recipient(meta) -> None:
    assert sc.channel_recipient_of(meta) is None


def test_channel_recipient_meta_stamps_a_whole_address_or_nothing() -> None:
    assert sc.channel_recipient_meta("", "c1", "u1") == {}
    assert sc.channel_recipient_meta("discord", "", "u1") == {}
    stamp = sc.channel_recipient_meta("discord", "c1", "")
    assert sc.channel_recipient_of(stamp) == ("discord", "c1", "")


@pytest.mark.asyncio
async def test_repeated_handoffs_under_one_audience_record_one_fence(tmp_path) -> None:
    """The fence set is bounded: keyed by audience, not by message.

    A long dashboard turn can take many Discord steers. Each landed steer's
    bookkeeping clears when the turn consumes it, but its fence is retained for
    the turn -- so a per-message record would grow without bound. One record per
    audience per turn: re-recording the same audience is a no-op, and the turn's
    teardown clears the set.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    for n in range(5):
        await dispatcher.handle_message(_message(f"steer number {n}"))

    assert steer_client.steer.await_count == 5
    assert len(slot._steer_audience_fences) == 1
    (admission,) = slot._steer_audience_fences.values()
    assert (
        admission[sc.QUEUED_CONTAINMENT_META_KEY]
        == sc.containment_meta(state, slot)[sc.QUEUED_CONTAINMENT_META_KEY]
    )
    assert cr.cross_surface_withheld(state, slot) is False


@pytest.mark.asyncio
async def test_a_changed_audience_records_its_own_fence(tmp_path) -> None:
    """The key is the audience, so a containment change between two steers is two
    records -- and the publisher then judges against the one that newly holds."""
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    await dispatcher.handle_message(_message("before the mirror"))
    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    await dispatcher.handle_message(_message("after the mirror"))

    assert steer_client.steer.await_count == 2
    assert len(slot._steer_audience_fences) == 2
    assert (
        cr.cross_surface_withheld(state, slot) is True
    ), "the first admission never saw the mirror"


@pytest.mark.asyncio
async def test_a_full_fence_set_queues_the_handoff_and_keeps_every_fence(tmp_path) -> None:
    """At the fence cap a new audience is neither recorded over a fence nor refused.

    A fence dropped is a cross-surface leg published that should have been
    withheld, so nothing is evicted; and the queue arm records no fence and is
    already where a declined steer goes, so the text takes it instead of a refusal
    that tells the author to wait.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    from kiro_crew.dashboard.chat_delivery import MAX_PENDING_STEERS

    # MAX_PENDING_STEERS distinct audiences already recorded this turn; the live
    # audience is none of them, so this message names a new one.
    recorded = {f"peer-{n}": {"marker": n} for n in range(MAX_PENDING_STEERS)}
    slot._steer_audience_fences.update(recorded)

    await dispatcher.handle_message(_message("one more thing"))

    steer_client.steer.assert_not_awaited()
    assert slot._steer_audience_fences == recorded, "every fence intact, none replaced or added"
    assert [q["content"] for q in slot._queue] == ["one more thing"]
    assert enqueued == []
    assert any("Queued for that session" in t for t in _sent_texts(client)), _sent_texts(client)
    assert not any(_REFUSAL in t or "Steering" in t for t in _sent_texts(client))


@pytest.mark.asyncio
async def test_the_drop_notice_resolves_the_send_ladder_off_the_loop_thread(
    tmp_path, monkeypatch
) -> None:
    """The drain is synchronous on the gateway loop; the ladder's governance vet is
    the call every other async caller offloads. So the resolve runs inside the
    scheduled task, on a worker thread, and the drop path itself never calls it.
    """
    import threading

    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    transport = _discord_transport()
    state.register_channel_transport(transport)
    await dispatcher.handle_message(_message("queued, then dropped"))

    resolver_threads: list[int] = []
    real_resolve = cr._resolve_channel_target

    def _observing_resolve(*args, **kwargs):
        resolver_threads.append(threading.get_ident())
        return real_resolve(*args, **kwargs)

    monkeypatch.setattr(cr, "_resolve_channel_target", _observing_resolve)
    loop_thread = threading.get_ident()

    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    assert resolver_threads == [], "the drop path itself must not run the ladder"
    await _settle_background(state)

    assert len(resolver_threads) == 1 and resolver_threads[0] != loop_thread
    transport.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_channel_notifier_reports_scheduling_not_delivery(tmp_path) -> None:
    """True once a stamp is present and the task exists; False for no stamp."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    assert (
        sc.notify_channel_recipient_dropped(
            state, entry_meta={}, target_slot=slot, text="x", constraints=["mirrored"]
        )
        is False
    )
    stamped = sc.channel_recipient_meta("discord", "c1", "u1")
    assert (
        sc.notify_channel_recipient_dropped(
            state, entry_meta=stamped, target_slot=slot, text="x", constraints=["mirrored"]
        )
        is True
    )
    # No transport is registered, so the task resolves nothing and sends nothing;
    # it still has to finish cleanly.
    await _settle_background(state)


@pytest.mark.asyncio
async def test_a_requeued_channel_steer_keeps_its_channel_provenance(tmp_path) -> None:
    """A steer the turn never consumed re-enters the queue as a channel message.

    ``_requeue_unconsumed_steers`` derives ``directive_user_origin`` from the
    per-steer map; the channel mark rides a sibling map so a requeued Discord
    steer runs its own turn under the same channel authority a queued one does.
    """
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    text = "steer that never landed"
    slot._pending_steers.append(text)
    slot._steer_user_origin[text] = True
    slot._steer_channel_origin[text] = True
    slot._steer_admissions[text] = sc.containment_meta(state, slot)

    cr._requeue_unconsumed_steers(state, slot)

    (entry,) = slot._queue
    assert entry["content"] == text
    assert entry.get("_directive_user_origin") is True
    assert entry.get("_directive_channel_origin") is True
    assert slot._steer_channel_origin == {}, "popped in lockstep with the other maps"


@pytest.mark.parametrize("channel", [True, False])
@pytest.mark.asyncio
async def test_a_requeued_channel_steer_is_turn_content_never_a_dashboard_command(
    tmp_path, channel
) -> None:
    """A channel steer the turn never consumed re-enters the queue stamped as content,
    exactly like a queued hand-off entry; a composer steer carries no such mark."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    text = "/clear"
    slot._pending_steers.append(text)
    slot._steer_user_origin[text] = True
    slot._steer_channel_origin[text] = channel
    slot._steer_admissions[text] = sc.containment_meta(state, slot)

    cr._requeue_unconsumed_steers(state, slot)

    (entry,) = slot._queue
    assert entry["content"] == text
    assert entry["meta"].get(cr.COMMANDS_OFF_META_KEY) is (True if channel else None)


async def _handler_cancelled_inside_the_steer_rpc(tmp_path, *, accepted: bool):
    """Run the DM through the dispatcher and cancel the handler while the RPC is suspended.

    Discord's transport close cancels its handler tasks as an ordinary path
    (``client.py`` gathers them on close), so this is a cancellation the hand-off
    meets in production, not a crash. The RPC is held on an event, the handler is
    cancelled while it waits, and only then does the client answer *accepted*.
    Returns everything the assertions read once the loop has run on.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    rpc_entered = asyncio.Event()
    release_rpc = asyncio.Event()

    async def _answer_after_release(_msg):
        rpc_entered.set()
        # flake-ok: the fake steer RPC parks here until the test sets release_rpc; moved verbatim
        await release_rpc.wait()
        return accepted

    steer_client.steer = AsyncMock(side_effect=_answer_after_release)

    handler = asyncio.ensure_future(dispatcher.handle_message(_message("keep the old name")))
    # flake-ok: pre-existing rendezvous on the fake RPC's entry, which the handler reaches without outside input; moved verbatim, tracked for a de-flake pass
    await rpc_entered.wait()
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        # flake-ok: the handler is cancelled just before the pytest.raises block, so this await unwinds at once; moved verbatim
        await handler
    release_rpc.set()
    # The hand-off task has no await left after the RPC; a few iterations let it finish.
    for _ in range(10):
        # flake-ok: pre-existing ten bounded zero-delay yields let the shielded hand-off task finish; moved verbatim, tracked for a de-flake pass
        await asyncio.sleep(0)
    return client, slot, enqueued


@pytest.mark.asyncio
async def test_a_cancelled_handler_still_reconciles_an_accepted_steer(tmp_path) -> None:
    """The hand-off finishes without its caller: row written, bookkeeping released.

    Awaited inline, the handler's cancellation lands inside the steer RPC: the
    client has the text, and everything behind the RPC -- the transcript row, the
    pop of the per-steer maps -- is skipped, so the text runs with no row while
    the maps keep its entry for the slot's lifetime (the ``steering_consumed``
    settle only removes the pending entry and patches an existing row). Shielded
    and strongly held, the hand-off task completes the reconciliation; the caller
    unwinds without the outcome, so no confirmation reaches the closing DM.
    """
    client, slot, enqueued = await _handler_cancelled_inside_the_steer_rpc(tmp_path, accepted=True)

    steer_row = next((m for m in slot.messages if m.get("meta", {}).get("steer")), None)
    assert steer_row is not None, "the accepted steer has its transcript row"
    assert steer_row["content"] == "keep the old name"
    assert steer_row["meta"].get(HUMAN_TURN_META_KEY) is True
    assert slot._steer_delivery_ids == {} and slot._steer_user_origin == {}
    assert slot._steer_channel_origin == {} and slot._steer_admissions == {}
    assert slot._pending_steers == ["keep the old name"], "delivered and live: the turn consumes it"
    assert len(slot._steer_audience_fences) == 1, "the audience's record stays for the turn"
    assert slot._queue == [] and enqueued == []
    assert ch._HANDOFFS_IN_FLIGHT == set(), "the strong reference is released on completion"
    assert not any("Steering" in t or "Queued" in t for t in _sent_texts(client))


@pytest.mark.asyncio
async def test_a_cancelled_handler_still_takes_the_queue_fallback_for_a_declined_steer(
    tmp_path,
) -> None:
    """The other half of the reconciliation survives the cancellation too.

    A steer the client declines is unwound and the text falls through to the
    slot's queue. Inline, the cancelled RPC leaves the optimistic registration
    standing and the text nowhere: a pending entry the teardown requeues onto a
    queue the DM was never told about, or discards on a hard kill.
    """
    client, slot, enqueued = await _handler_cancelled_inside_the_steer_rpc(tmp_path, accepted=False)

    assert [q["content"] for q in slot._queue] == ["keep the old name"]
    assert slot._pending_steers == [] and slot._steer_delivery_ids == {}
    assert slot._steer_user_origin == {} and slot._steer_channel_origin == {}
    assert enqueued == [], "the Discord-side queue stays out of it"
    assert ch._HANDOFFS_IN_FLIGHT == set()
    assert not any("Queued" in t for t in _sent_texts(client)), "no live transport to confirm to"


async def _turn_emits_monitor_watch_after_a_steer(tmp_path, monkeypatch, *, channel: bool):
    """Drive a real dashboard-origin turn that takes a steer, then emits ``monitor_watch``.

    The harness is ``test_acp_tool_identity``'s: the real ``_run_chat`` loop over a
    fake ACP client streaming ``AcpEvent``s, with ``apply_session_directive``
    replaced by a spy. Between the directive tool's call and its result the turn
    takes a steer -- a Discord human's through the hand-off (*channel*), or the
    composer's own (the counterfactual) -- so the directive the model emits next
    is one the steer could have shaped. Returns the spy and the slot.
    """
    from test_acp_tool_identity import _stub_state

    from kiro_crew import session_directive
    from kiro_crew.acp.types import (
        EVENT_COMPLETE,
        EVENT_TEXT_CHUNK,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        AcpEvent,
    )
    from kiro_crew.dashboard.chat_delivery import steer_into_running_turn

    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    args = {
        "kind": "github_pull_request",
        "target": "https://github.com/example/repo/pull/1",
        "objective": "review_ready",
    }
    marker = session_directive.encode("monitor_watch", args, "watching")
    outcomes: list = []

    async def _stream(_msg):
        if outcomes:
            # The steer the fake client never echoes as consumed is requeued at the
            # turn's end and runs as its own turn through this same stream: that
            # turn emits no directive, so the one directive under test is the
            # running dashboard turn's.
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="done")
            yield AcpEvent(kind=EVENT_COMPLETE)
            return
        yield AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id="tc-watch",
            title="Watching the pull request",
            tool_name="monitor_watch",
            mcp_server_name="kirocrew-core",
        )
        # The turn is running (its task is live) while the steer is admitted; the
        # harness awaits ``_run_chat`` directly, so the liveness the gate reads is
        # expressed by a task the way every other test here expresses it.
        _busy(slot)
        try:
            if channel:
                outcomes.append(
                    await ch.hand_to_resumed_slot(
                        state,
                        "dashboard:chat-1",
                        "watch that PR for me",
                        mode="steer",
                        has_attachments=False,
                        channel_type="discord",
                        conversation_id="c1",
                        principal="u1",
                    )
                )
            else:
                outcomes.append(
                    await steer_into_running_turn(
                        state, slot, "watch that PR for me", user_origin=True
                    )
                )
        finally:
            slot.task = None
        yield AcpEvent(
            kind=EVENT_TOOL_RESULT, tool_call_id="tc-watch", tool_output=marker, tool_final=True
        )
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")
        yield AcpEvent(kind=EVENT_COMPLETE)

    inner = _steerable()
    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = inner  # the inner ACP client the turn publishes on the slot
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    spy = AsyncMock(return_value=DirectiveOutcome("[applied]"))
    monkeypatch.setattr(cr, "apply_session_directive_outcome", spy)

    await cr._run_chat(state, slot, "go", _directive_user_origin=True)
    task = getattr(slot, "task", None)
    if task is not None:
        # flake-ok: pre-existing await of the turn task this helper's own _run_chat started; moved verbatim, tracked for a de-flake pass
        await task
    assert inner.steer.await_count == 1, (inner.steer.await_count, outcomes)
    inner.steer.assert_awaited_with("watch that PR for me")
    return spy, slot, outcomes


@pytest.mark.asyncio
async def test_a_directive_after_a_channel_steer_is_stamped_channel_origin(tmp_path, monkeypatch):
    """A Discord steer into a dashboard-origin turn narrows that turn's directives.

    The turn's ``_directive_channel_origin`` is its opener's (False here) and every
    directive it applies is stamped with it; a channel human's text injected
    mid-turn can shape the ``monitor_watch`` the model emits next, so unchanged the
    stamp would hand a channel-shaped input the dashboard's authority. The steer's
    admission sets the slot's ``_turn_channel_narrowed``, and the directive is
    filed as channel-created -- the authority a channel-origin turn's would carry.
    """
    spy, slot, outcomes = await _turn_emits_monitor_watch_after_a_steer(
        tmp_path, monkeypatch, channel=True
    )

    assert outcomes and outcomes[0].kind == ch.HANDOFF_STEERED
    spy.assert_called_once()
    assert spy.call_args.args[3] == "monitor_watch"
    assert spy.call_args.kwargs["producer_is_channel"] is True
    assert spy.call_args.kwargs["producer_is_user_facing"] is True
    assert slot._turn_channel_narrowed is False, "the narrowing ends with the turn"


@pytest.mark.asyncio
async def test_the_composers_own_steer_does_not_narrow_the_turn(tmp_path, monkeypatch):
    """Counterfactual: the session's own human steering from the composer changes
    nothing -- the directive keeps the dashboard-origin stamp the turn opened with."""
    spy, slot, outcomes = await _turn_emits_monitor_watch_after_a_steer(
        tmp_path, monkeypatch, channel=False
    )

    assert outcomes == ["steered"]
    spy.assert_called_once()
    assert spy.call_args.kwargs["producer_is_channel"] is False
    assert slot._turn_channel_narrowed is False


@pytest.mark.asyncio
async def test_a_close_during_the_steer_reports_the_text_queued_for_the_next_resume(
    tmp_path,
) -> None:
    """The tab is closed while the RPC is suspended; the text is NOT lost.

    ``_close_slot`` pops the slot and cancels its turn, whose teardown requeues the
    pending steer onto the popped object's queue, and the close's save archives
    that queue as ``queued_prompts``, drained when the session is next resumed.
    Reporting "NOT delivered" here makes the human resend and run the text twice;
    the outcome is read from the ARCHIVED record -- the persistence witness says
    the copy on disk carries the entry -- and worded as what it is.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    from kiro_crew.dashboard.slot_queue_repository import queue_persist_signature

    async def _close_then_accept(_msg):
        state._slots.pop("chat-1")
        cr._requeue_unconsumed_steers(state, slot)
        # The close's save archived the popped slot with that queue: the
        # persistence witness now matches the live queue.
        slot._queue_persisted_sig = queue_persist_signature(slot.durable_queue_entries())
        return True

    steer_client.steer = AsyncMock(side_effect=_close_then_accept)

    await dispatcher.handle_message(_message("finish the rename"))

    assert [q["content"] for q in slot._queue] == ["finish the rename"], "the archived queue"
    assert slot.queue_persist_pending is False
    assert state.get_slot("chat-1") is None
    texts = _sent_texts(client)
    assert not any("changed while your message was in flight" in t for t in texts), texts
    assert not any(_REFUSAL in t for t in texts), texts
    assert any("next resumed" in t for t in texts), texts
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_close_before_the_teardown_requeues_reports_not_yet_saved(tmp_path) -> None:
    """The RPC returns between the pop and the cancelled turn's teardown.

    The client accepted the text and the steer persisted its row -- in memory, on
    a popped object the close's save may or may not still write. Only the archive
    can promise the text survives, so the outcome is the honest one: not yet
    saved, watch before resending. Neither "Steering" nor "Queued" is true.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _close_then_accept(_msg):
        state._slots.pop("chat-1")
        return True

    steer_client.steer = AsyncMock(side_effect=_close_then_accept)

    await dispatcher.handle_message(_message("finish the rename"))

    steer_row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
    assert steer_row["meta"].get("steer_delivery_id"), "the row names its delivery"
    texts = _sent_texts(client)
    assert not any("Steering" in t or "Queued" in t or "Delivered" in t for t in texts), texts
    assert any("had not been saved with it yet" in t for t in texts), texts


@pytest.mark.asyncio
async def test_a_close_that_archived_before_the_requeue_does_not_report_queued(tmp_path) -> None:
    """Red-first for the archive-before-cleanup race.

    The close's teardown wait is bounded; a cleanup that outlasts it requeues the
    steer onto an object the archive already left behind, and nothing revisits a
    popped object. Here the registration is still only in memory (a Stop bumped
    the generation during the RPC, so the steer reports "requeued" by prediction)
    and the persistence witness says the archive lacks it: "queued" would name a
    message the archive does not carry.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _close_and_stop_then_accept(_msg):
        state._slots.pop("chat-1")
        slot._stop_generation += 1
        return True

    steer_client.steer = AsyncMock(side_effect=_close_and_stop_then_accept)

    await dispatcher.handle_message(_message("finish the rename"))

    assert slot._pending_steers == ["finish the rename"], "registered in memory only"
    assert slot._queue == [], "no requeue happened, so nothing is archived"
    texts = _sent_texts(client)
    assert not any("Queued" in t or "next resumed" in t for t in texts), texts
    assert any("had not been saved with it yet" in t for t in texts), texts
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_requeue_the_archive_missed_is_not_reported_queued(tmp_path) -> None:
    """The requeued entry sits on the popped object but the archive was written without it."""
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _archive_then_requeue(_msg):
        state._slots.pop("chat-1")
        # The close's save ran first (the queue archived without this entry) and
        # the cancelled turn's teardown requeued afterwards.
        cr._requeue_unconsumed_steers(state, slot)
        return True

    steer_client.steer = AsyncMock(side_effect=_archive_then_requeue)

    await dispatcher.handle_message(_message("finish the rename"))

    assert [q["content"] for q in slot._queue] == ["finish the rename"]
    assert slot.queue_persist_pending is True, "the copy on disk lacks the entry"
    texts = _sent_texts(client)
    assert not any("Queued" in t for t in texts), texts
    assert any("had not been saved with it yet" in t for t in texts), texts


@pytest.mark.asyncio
async def test_a_successor_holding_the_text_in_its_queue_reports_queued(tmp_path) -> None:
    """A slot recreated under the same key that carries the text in its queue.

    The successor's queue is one a drain reaches, so the text runs there; the
    ordinary queued confirmation is right, and nothing is appended a second time.
    """
    steer_client = _steerable(accepted=False)
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _replace_and_carry(_msg):
        # The restored entry carries the steer's delivery id in its durable meta,
        # the way a teardown requeue writes it and a resume restores it.
        did = slot._steer_delivery_ids["finish the rename"]
        state._slots.pop("chat-1")
        fresh = _busy(state.get_or_create_slot("chat-1"))
        fresh.queue_append("finish the rename", meta={"steer_delivery_id": did})
        return False

    steer_client.steer = AsyncMock(side_effect=_replace_and_carry)

    await dispatcher.handle_message(_message("finish the rename"))

    fresh = state.get_slot("chat-1")
    assert fresh is not slot
    assert [q["content"] for q in fresh._queue] == ["finish the rename"], "once, on the live object"
    assert slot._queue == [], "never appended to the detached object"
    texts = _sent_texts(client)
    assert any("Queued for that session" in t and "next resumed" not in t for t in texts), texts
    assert not any("changed while your message was in flight" in t for t in texts), texts
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_successor_that_ran_the_text_reports_delivered(tmp_path) -> None:
    """The successor's transcript names this delivery: the text ran there as its own turn."""
    steer_client = _steerable(accepted=False)
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _replace_and_run(_msg):
        # The drained row unions the entry's meta, delivery id included.
        did = slot._steer_delivery_ids["finish the rename"]
        state._slots.pop("chat-1")
        fresh = _busy(state.get_or_create_slot("chat-1"))
        fresh.append("user", "finish the rename", "msg msg-u", meta={"steer_delivery_ids": [did]})
        return False

    steer_client.steer = AsyncMock(side_effect=_replace_and_run)

    await dispatcher.handle_message(_message("finish the rename"))

    fresh = state.get_slot("chat-1")
    assert fresh._queue == [] and slot._queue == []
    texts = _sent_texts(client)
    assert any("Delivered to that session" in t for t in texts), texts
    assert not any("changed while your message was in flight" in t for t in texts), texts


@pytest.mark.asyncio
async def test_a_close_during_a_declined_steer_still_refuses(tmp_path) -> None:
    """Counterfactual: nothing holds the text, so the refusal stands.

    A declined steer's registration is unwound and the session is closed with no
    successor: no queue the close archives carries the text and no turn ran it.
    Refusing is the truthful answer, and the fallback never appends to the
    detached object.
    """
    steer_client = _steerable(accepted=False)
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _close_then_decline(_msg):
        state._slots.pop("chat-1")
        return False

    steer_client.steer = AsyncMock(side_effect=_close_then_decline)

    await dispatcher.handle_message(_message("finish the rename"))

    assert slot._queue == [] and slot._pending_steers == []
    texts = _sent_texts(client)
    assert any("changed while your message was in flight" in t for t in texts), texts
    assert not any("Queued" in t or "Delivered" in t for t in texts), texts
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_stranded_message_whose_text_recurs_in_the_successor_transcript_is_still_refused(
    tmp_path,
) -> None:
    """Reconciliation is by delivery id, never by content.

    A slot recreated under the same key restores its whole prior transcript. A
    short message that recurs there ("continue") must not prove a delivery that
    never happened: the declined steer's registration is unwound, no record
    carries this delivery's id, and the author is told so rather than left
    waiting for a reply that never comes.
    """
    steer_client = _steerable(accepted=False)
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _replace_with_history(_msg):
        state._slots.pop("chat-1")
        fresh = _busy(state.get_or_create_slot("chat-1"))
        # The restored transcript: the same words, from an earlier turn.
        fresh.append("user", "continue", "msg msg-u")
        fresh.append("assistant", "Continuing.", "msg msg-a")
        return False

    steer_client.steer = AsyncMock(side_effect=_replace_with_history)

    await dispatcher.handle_message(_message("continue"))

    fresh = state.get_slot("chat-1")
    assert fresh._queue == [] and slot._queue == []
    texts = _sent_texts(client)
    assert any("changed while your message was in flight" in t for t in texts), texts
    assert not any("Delivered" in t or "Queued" in t for t in texts), texts
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_full_live_queue_refuses_the_enqueue(tmp_path) -> None:
    """The slot's live queue is bounded; the hand-off refuses at the bound.

    Every producer that appends to the live queue guards ``MAX_LIVE_QUEUE_ENTRIES``
    itself. Appending past it, or evicting a waiting entry (someone's text with no
    other copy), are both wrong; the channel human still holds the text and is
    told why it was not added.
    """
    from kiro_crew.dashboard.slot_queue_repository import MAX_LIVE_QUEUE_ENTRIES

    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    for n in range(MAX_LIVE_QUEUE_ENTRIES):
        slot.queue_append(f"waiting {n}")

    await dispatcher.handle_message(_message("one more"))

    assert len(slot._queue) == MAX_LIVE_QUEUE_ENTRIES, "nothing appended past the bound"
    assert not any(q["content"] == "one more" for q in slot._queue)
    texts = _sent_texts(client)
    assert any("queue is full" in t and "NOT added" in t for t in texts), texts
    assert not any("Queued for that session" in t for t in texts), texts
    assert enqueued == []


@pytest.mark.asyncio
async def test_the_fence_cap_fallthrough_still_meets_the_queue_bound(tmp_path) -> None:
    """At the fence cap the queue arm is the fallback, and its bound is the one refusal left."""
    from kiro_crew.dashboard.chat_delivery import MAX_PENDING_STEERS
    from kiro_crew.dashboard.slot_queue_repository import MAX_LIVE_QUEUE_ENTRIES

    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    recorded = {f"peer-{n}": {"marker": n} for n in range(MAX_PENDING_STEERS)}
    slot._steer_audience_fences.update(recorded)
    for n in range(MAX_LIVE_QUEUE_ENTRIES):
        slot.queue_append(f"waiting {n}")

    await dispatcher.handle_message(_message("one more"))

    steer_client.steer.assert_not_awaited()
    assert slot._steer_audience_fences == recorded
    assert len(slot._queue) == MAX_LIVE_QUEUE_ENTRIES
    assert any("queue is full" in t for t in _sent_texts(client)), _sent_texts(client)
