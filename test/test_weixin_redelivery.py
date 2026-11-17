"""A redelivered WeChat message runs once.

iLink names a message's server id ``message_id`` (a number); dedup read only
``msg_id`` and ``client_id``, so a redelivered message carrying ``message_id``
alone ran a second turn.

A ``getupdates`` reply whose ``get_updates_buf`` is empty or null keeps the
saved cursor, so the next poll does not restart the sync and redeliver.
"""

from __future__ import annotations

import asyncio

from test_weixin import _make

from kiro_crew.weixin.client import ITEM_TEXT


def _envelope(**ids: object) -> dict:
    return {
        "from_user_id": "userA",
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "once"}}],
        **ids,
    }


def test_a_redelivered_message_id_runs_once(tmp_path) -> None:
    t, _, got = _make(tmp_path)
    asyncio.run(t.receive(_envelope(message_id=7001)))
    asyncio.run(t.receive(_envelope(message_id=7001)))
    assert len(got) == 1


def test_distinct_message_ids_both_run(tmp_path) -> None:
    t, _, got = _make(tmp_path)
    asyncio.run(t.receive(_envelope(message_id=7001)))
    asyncio.run(t.receive(_envelope(message_id=7002)))
    assert len(got) == 2


def test_an_empty_or_null_cursor_keeps_the_saved_one(tmp_path) -> None:
    t, client, _ = _make(tmp_path)
    replies = [
        {"ret": 0, "msgs": [], "get_updates_buf": "c1"},
        {"ret": 0, "msgs": [], "get_updates_buf": ""},
        {"ret": 0, "msgs": [], "get_updates_buf": None},
        {"ret": 0, "msgs": [], "get_updates_buf": "c2"},
    ]
    sent: list[str] = []

    async def poll(buf: str) -> dict:
        sent.append(buf)
        if not replies:
            t._running = False
            return {"ret": 0, "msgs": []}
        return replies.pop(0)

    client.get_updates = poll  # type: ignore[attr-defined]
    t._running = True
    asyncio.run(t._poll_forever())
    assert sent == ["", "c1", "c1", "c1", "c2"]
