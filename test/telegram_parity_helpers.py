"""The inbound messages, renderer, credential id and snapshot fixture the parity tests share.

``test_telegram_parity.py`` and its ``_commands``, ``_approvals``, ``_privacy``, ``_turns``
and ``_inbound`` siblings import these by name: ``_drop_live_config_snapshot``, an autouse
fixture every module imports so it applies to all their tests, which resets the
process-global config snapshot ``_prime_live`` publishes; ``_msg`` and ``_forum_msg``, a
private-DM and a forum-Topic inbound message past authorization; ``_renderer``, a
``TelegramRenderer`` on a ``FakeClient``, returned with that client;
``_AWS_KEY``, the documented example access-key id, split so the literal never appears
whole; and ``_drain_offset_writes``, which awaits the cursor writes
``_maybe_persist_offset`` fired.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_telegram import FakeClient

from kiro_crew.telegram.renderer import TelegramRenderer
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES, TelegramInboundMessage

# Split so the literal never appears whole in this file.
_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.fixture(autouse=True)
def _drop_live_config_snapshot():
    """Leave no primed config snapshot behind for the next test.

    ``_prime_live`` (and ``_dispatcher``, which calls it) publishes into the
    process-global config watcher, so without this the last test to prime would
    set the live config for every test after it in the same worker.
    """
    yield
    from kiro_crew.config import live

    live.reset_for_tests()


def _msg(text: str, *, user: int = 1, chat: int = 1) -> TelegramInboundMessage:
    """A private-DM inbound message, already past authorization."""
    return TelegramInboundMessage(
        channel_type="telegram",
        user_id=str(user),
        conversation_id=str(chat),
        text=text,
        message_id=7,
        chat_type="private",
    )


def _renderer(**kw: Any) -> tuple[TelegramRenderer, FakeClient]:
    client = FakeClient()
    return (
        TelegramRenderer(client, 42, TELEGRAM_CAPABILITIES, session_key="telegram:1", **kw),
        client,
    )


async def _drain_offset_writes(client: Any) -> None:
    """Await the cursor writes ``_maybe_persist_offset`` fired.

    That call is fire-and-forget: it creates a TRACKED task which hands the file
    operation to a thread, so how long it takes belongs to the thread pool, not to
    any duration a test can name. Waiting a fixed number of milliseconds for it is
    the same bug in test form, and it surfaced on the Windows CI shards -- coarse
    ~15.6ms timer granularity with four pytest shards competing on one runner -- as
    the assertion's ``read_text`` landing BEFORE the write, i.e. FileNotFoundError
    on the cursor file rather than a wrong value.

    Draining the tracked set is exact and finishes the moment the write does. The
    loop re-checks rather than gathering once because a task can be created from the
    done-callback of the one being awaited; the ceiling is a backstop so a future
    regression fails loudly instead of hanging the suite, never a timing assertion.
    """

    async def _drain() -> None:
        while client._handler_tasks:
            await asyncio.gather(*tuple(client._handler_tasks))

    await asyncio.wait_for(_drain(), timeout=30)


def _forum_msg(
    text: str = "hello",
    *,
    reply_to: int = 0,
    mentions: tuple[str, ...] = (),
    has_entities: bool = False,
) -> Any:
    """A message in an allow-listed supergroup forum Topic.

    ``has_entities`` defaults False, which is the SYNTHESIZED shape (no entity
    list), so an existing case keeps exercising the token-matcher fallback. A case
    about what Telegram itself classified passes both.
    """
    from kiro_crew.telegram.transport import TelegramInboundMessage

    return TelegramInboundMessage(
        channel_type="telegram",
        user_id="1",
        conversation_id="-100999",
        text=text,
        message_id=7,
        chat_type="supergroup",
        thread_id="4",
        reply_to_user_id=reply_to,
        mentions=mentions,
        has_entities=has_entities,
    )
