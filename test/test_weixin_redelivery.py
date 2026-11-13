"""A redelivered WeChat message runs once.

iLink names a message's server id ``message_id`` (a number); dedup read only
``msg_id`` and ``client_id``, so a redelivered message carrying ``message_id``
alone ran a second turn.
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
