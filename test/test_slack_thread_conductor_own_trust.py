"""A Conductor thread's conductor offers "Trust this thread" on its OWN approval cards.

Its workers' cards already did (``worker_approvals``); the conductor's own card,
posted by the dashboard turn into the linked thread, showed only Approve / Reject.
Now a session that IS a bound Conductor thread's conductor gets the same button,
the same confirmation and the same grant (``set_channel_mode`` on the thread
binding): the click approves the call and puts the conductor and its workers on
Trust. Owner only. Any other thread, a plain DM and a code channel keep their
buttons.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from slack_code_channel_surface_helpers import env_fixture  # noqa: F401
from slack_code_channels_helpers import OWNER
from test_slack_thread_conductor_runtime import (  # noqa: F401
    ANCHOR,
    CONDUCTOR,
    MEMBER,
    PRIVATE,
    REQUEST,
    SHARED,
    TEAM,
    WORKER,
)
from test_slack_thread_conductor_runtime import _click as _worker_click
from test_slack_thread_conductor_runtime import (  # noqa: F401
    _relay,
    thread,
)

from kiro_crew.slack.code_channels import approval_picker
from kiro_crew.slack.code_channels import worker_approvals as wa
from kiro_crew.slack.code_channels.store import ROOM_IM, ROOM_PRIVATE, Binding
from kiro_crew.slack.handler import _build_approval_blocks, post_linked_approval

TRUST = approval_picker.TRUST_CALL_ACTION_ID


def _buttons(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for b in blocks if b.get("type") == "actions" for e in b["elements"]]


def _trust_buttons(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in _buttons(blocks) if b["action_id"] == TRUST]


async def _turn(e: Any, *, slot_key: str = CONDUCTOR, channel: str = PRIVATE, anchor: str = ANCHOR):
    """A real dashboard turn in *slot_key*, linked at *channel*/*anchor*, raising one prompt."""
    from slack_code_channel_turns import prompt_in_channel_session
    from turn_harness import APPROVED

    await prompt_in_channel_session(
        e, slot_key=slot_key, channel=channel, anchor=anchor, answer=APPROVED
    )
    return e.slack.blocks_posts[-1]


async def _own_card(e: Any, anchor: str = ANCHOR) -> tuple[str, asyncio.Future]:
    """The conductor's own card, as the prompt posts it into its thread."""
    fut = asyncio.get_running_loop().create_future()
    e.conductor._approval_futures[REQUEST] = fut
    ts = await post_linked_approval(
        e.slack,
        PRIVATE,
        anchor,
        REQUEST,
        f"dashboard:{CONDUCTOR}",
        "Run shell",
        "npm test",
        slot_key=CONDUCTOR,
        future=fut,
        channel_trust=True,
        thread_trust=True,
    )
    assert ts
    return ts, fut


def _click(user: str, ts: str, anchor: str = ANCHOR, team: str = TEAM) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": team},
        "user": {"id": user},
        "channel": {"id": PRIVATE},
        "message": {"ts": ts, "thread_ts": anchor},
        "container": {"message_ts": ts, "channel_id": PRIVATE, "thread_ts": anchor},
        "actions": [{"action_id": TRUST, "value": REQUEST}],
    }


# ── Which cards carry the button ──────────────────────────────────────────


def test_the_conductors_own_card_offers_trust_this_thread_with_its_confirm(
    thread: Any,  # noqa: F811
) -> None:
    channel, blocks, _text, thread_ts = asyncio.run(_turn(thread))
    assert (channel, thread_ts) == (PRIVATE, ANCHOR)
    trust = _trust_buttons(blocks)
    assert len(trust) == 1
    assert trust[0]["text"]["text"] == approval_picker.TRUST_THREAD_LABEL
    assert trust[0]["confirm"] == approval_picker.TRUST_THREAD_CONFIRM
    assert "Trust this channel" not in str(blocks)
    # Approve stays the one primary button.
    assert [b.get("style") for b in _buttons(blocks)].count("primary") == 1


def test_the_thread_confirm_fits_what_slack_accepts() -> None:
    confirm = approval_picker.TRUST_THREAD_CONFIRM
    for field, limit in {"title": 100, "text": 300, "confirm": 30, "deny": 30}.items():
        assert len(confirm[field]["text"]) <= limit, field


def test_another_thread_of_the_same_channel_keeps_approve_and_reject(
    thread: Any,  # noqa: F811
) -> None:
    _channel, blocks, _text, thread_ts = asyncio.run(_turn(thread, anchor="300.000003"))
    assert thread_ts == "300.000003"
    assert _trust_buttons(blocks) == []


def test_only_the_threads_own_conductor_is_one(thread: Any) -> None:  # noqa: F811
    """A slot at the thread's link that the binding does not name is no conductor
    (the anchor guard keeps a real turn from linking there at all)."""
    from types import SimpleNamespace

    from kiro_crew.slack.code_channels import thread_runtime

    assert thread_runtime.conductor_thread_of(thread.conductor) is not None
    other = SimpleNamespace(key="chat-7-7", _slack_channel=PRIVATE, _slack_thread_ts=ANCHOR)
    assert thread_runtime.conductor_thread_of(other) is None
    elsewhere = SimpleNamespace(key=CONDUCTOR, _slack_channel=PRIVATE, _slack_thread_ts="300.3")
    assert thread_runtime.conductor_thread_of(elsewhere) is None
    unlinked = SimpleNamespace(key=CONDUCTOR, _slack_channel="", _slack_thread_ts="")
    assert thread_runtime.conductor_thread_of(unlinked) is None


def test_an_ended_thread_gets_no_button(thread: Any) -> None:  # noqa: F811
    assert thread.store.end_thread_binding(PRIVATE, ANCHOR).ended_at
    _channel, blocks, _text, _ts = asyncio.run(_turn(thread))
    assert _trust_buttons(blocks) == []


def test_no_button_with_the_thread_switch_off(thread: Any) -> None:  # noqa: F811
    thread.thread_on = False
    _channel, blocks, _text, _ts = asyncio.run(_turn(thread))
    assert _trust_buttons(blocks) == []


def test_a_plain_dm_keeps_todays_buttons(thread: Any) -> None:  # noqa: F811
    _channel, blocks, _text, _ts = asyncio.run(
        _turn(thread, slot_key="chat-3-3", channel="D0DM000001", anchor="400.000004")
    )
    assert _trust_buttons(blocks) == []
    assert "Trust this thread" not in str(blocks)


def test_a_code_channel_top_level_card_keeps_trust_this_channel() -> None:
    from types import SimpleNamespace

    event = SimpleNamespace(request_id=REQUEST, title="Run shell", tool_input="ls", tool_purpose="")
    blocks = _build_approval_blocks(event, is_dm=False, channel_trust=True)  # type: ignore[arg-type]
    trust = _trust_buttons(blocks)
    assert trust[0]["text"]["text"] == approval_picker.TRUST_CALL_LABEL
    assert trust[0]["confirm"] == approval_picker.TRUST_CALL_CONFIRM


# ── The click ─────────────────────────────────────────────────────────────


def test_the_owners_click_approves_the_call_and_trusts_the_thread(
    thread: Any,  # noqa: F811
) -> None:
    async def go() -> tuple[str, asyncio.Future]:
        ts, fut = await _own_card(thread)
        assert await approval_picker.handle_trust_call(thread.orch, _click(OWNER, ts))
        return ts, fut

    ts, fut = asyncio.run(go())
    assert fut.result().startswith("approved")
    assert thread.conductor._trust is True
    assert thread.worker._trust is True  # its worker follows, as from a worker card
    assert [r for r in thread.mode_rows if r["operation"] == "mode_change:trust"]
    warned = [p for p in thread.slack.posts if "*Trust*" in p[1]]
    assert warned and warned[-1][0] == PRIVATE and warned[-1][2] == ANCHOR
    assert "this thread" in warned[-1][1]
    written = [u for u in thread.slack.updates if u[1] == ts]
    assert written and "this thread" in str(written[-1]) and "this channel" not in str(written[-1])


@pytest.mark.parametrize("user", [SHARED, MEMBER])
def test_a_non_owner_click_changes_nothing(thread: Any, user: str) -> None:  # noqa: F811
    async def go() -> asyncio.Future:
        ts, fut = await _own_card(thread)
        assert await approval_picker.handle_trust_call(thread.orch, _click(user, ts))
        return fut

    fut = asyncio.run(go())
    assert not fut.done()
    assert thread.conductor._trust is False and thread.worker._trust is False
    assert thread.mode_rows == []
    assert thread.slack.ephemerals and thread.slack.ephemerals[-1][1] == user


def test_a_card_in_a_thread_not_bound_as_a_conductor_thread_is_refused(
    thread: Any,  # noqa: F811
) -> None:
    async def go() -> asyncio.Future:
        ts, fut = await _own_card(thread, anchor="300.000003")
        assert await approval_picker.handle_trust_call(
            thread.orch, _click(OWNER, ts, anchor="300.000003")
        )
        return fut

    fut = asyncio.run(go())
    assert not fut.done() and thread.conductor._trust is False
    assert thread.slack.ephemerals[-1][2] == approval_picker.NOT_THIS_CARD_TEXT


def test_a_card_naming_another_threads_binding_is_refused(thread: Any) -> None:  # noqa: F811
    """The card's thread is a bound Conductor thread, but of another session."""
    thread.store.put_thread_binding(
        Binding(
            channel_id=PRIVATE,
            team_id=TEAM,
            session_key="chat-8-8",
            anchor_ts="300.000003",
            thread=True,
            room=ROOM_PRIVATE,
        )
    )

    async def go() -> asyncio.Future:
        ts, fut = await _own_card(thread, anchor="300.000003")
        assert await approval_picker.handle_trust_call(
            thread.orch, _click(OWNER, ts, anchor="300.000003")
        )
        return fut

    fut = asyncio.run(go())
    assert not fut.done() and thread.conductor._trust is False


def test_the_click_with_the_switch_off_is_not_ours(thread: Any) -> None:  # noqa: F811
    async def go() -> tuple[bool, asyncio.Future]:
        ts, fut = await _own_card(thread)
        thread.thread_on = False
        return await approval_picker.handle_trust_call(thread.orch, _click(OWNER, ts)), fut

    handled, fut = asyncio.run(go())
    assert handled is False and not fut.done() and thread.conductor._trust is False


def test_a_worker_card_after_the_conductor_trusted_still_answers(thread: Any) -> None:  # noqa: F811
    async def go() -> asyncio.Future:
        ts, _fut = await _own_card(thread)
        assert await approval_picker.handle_trust_call(thread.orch, _click(OWNER, ts))
        card, wfut = await _relay(thread)
        assert card is not None
        assert await wa.handle_click(thread.orch, _worker_click(OWNER, card, wa.TRUST_ACTION_ID))
        return wfut

    wfut = asyncio.run(go())
    assert wfut.result().startswith("approved")
    assert thread.conductor._trust is True and thread.worker._trust is True
    assert (WORKER, REQUEST, True) in thread.resolved or wfut.done()


DM = "D0DM000009"


def test_a_dm_conductor_thread_offers_and_grants_the_thread_trust(
    thread: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """In the app's DM, the conductor's card offers the thread's Trust (it reaches
    the workers), never the unconfirmed per-session "Trust session"."""
    from kiro_crew.slack import handler

    monkeypatch.setattr(handler, "_linked_trust_grantable", lambda *_a: True)
    thread.store.put_thread_binding(
        Binding(
            channel_id=DM,
            team_id=TEAM,
            session_key=CONDUCTOR,
            anchor_ts=ANCHOR,
            thread=True,
            room=ROOM_IM,
        )
    )

    async def go() -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        thread.conductor._approval_futures[REQUEST] = fut
        ts = await post_linked_approval(
            thread.slack,
            DM,
            ANCHOR,
            REQUEST,
            f"dashboard:{CONDUCTOR}",
            "Run shell",
            "npm test",
            slot_key=CONDUCTOR,
            future=fut,
            channel_trust=True,
            thread_trust=True,
        )
        assert ts
        blocks = thread.slack.blocks_posts[-1][1]
        trust = _trust_buttons(blocks)
        assert len(trust) == 1
        assert trust[0]["text"]["text"] == approval_picker.TRUST_THREAD_LABEL
        assert trust[0]["confirm"] == approval_picker.TRUST_THREAD_CONFIRM
        assert "Trust session" not in str(blocks)
        click = _click(OWNER, ts)
        click["channel"] = {"id": DM}
        click["container"]["channel_id"] = DM
        assert await approval_picker.handle_trust_call(thread.orch, click)
        return fut

    fut = asyncio.run(go())
    assert fut.result().startswith("approved")
    assert thread.conductor._trust is True and thread.worker._trust is True
