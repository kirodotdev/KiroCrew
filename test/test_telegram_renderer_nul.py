"""A NUL byte in a reply must not break the Telegram renderer.

``_md_to_telegram_html`` and ``_strip_hr`` hold code spans and fences behind
``NUL<index>NUL`` placeholders. Reply text that already carried that shape (a tool
output with raw NUL bytes the agent echoed) was read back as a placeholder: an
index past the stash raised ``IndexError``, and an index inside it pulled a code
span into the wrong place.
"""

from __future__ import annotations

import asyncio

from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import FakeClient
from telegram_parity_helpers import _AWS_KEY

from kiro_crew.telegram.renderer import (
    TelegramRenderer,
    _md_to_telegram_html,
    md_to_telegram_html_safe,
)
from kiro_crew.telegram.renderer_display import _strip_hr
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

NUL = "\x00"


def test_a_nul_index_past_the_stash_renders_instead_of_raising() -> None:
    assert _md_to_telegram_html(f"out: {NUL}5{NUL} done") == "out: 5 done"


def test_a_nul_index_inside_the_stash_does_not_copy_a_code_span() -> None:
    assert _md_to_telegram_html(f"use `ls` then {NUL}0{NUL} ok") == (
        "use <code>ls</code> then 0 ok"
    )


def test_strip_hr_keeps_a_nul_shaped_run_as_plain_text() -> None:
    assert _strip_hr(f"bytes: {NUL}H3{NUL} done") == "bytes: H3 done"


def test_a_streamed_reply_with_nul_bytes_still_seals() -> None:
    cli = FakeClient()
    r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

    async def _go() -> None:
        await r.on_turn_start()
        await r.on_text_chunk(f"bytes: {NUL}H3{NUL} and {NUL}7{NUL} done")
        await r._stream_live(force=True)
        await r._seal_current()

    asyncio.run(_go())
    shown = [text for text, _ in cli.sent] + [text for _, text, _ in cli.edits]
    assert shown, "the reply reached the chat"
    assert all(NUL not in text for text in shown)
    assert "H3 and 7 done" in shown[-1]


def test_a_credential_split_by_a_nul_is_redacted_before_the_nul_goes() -> None:
    # Dropping NUL rejoins the two halves, so it has to happen BEFORE the
    # credential scan, which cannot see a key a NUL cuts in two.
    out = md_to_telegram_html_safe(f"key {_AWS_KEY[:4]}{NUL}{_AWS_KEY[4:]} end")
    assert "[REDACTED" in out
    assert _AWS_KEY not in out and _AWS_KEY[4:] not in out


def test_a_streamed_credential_split_by_a_nul_is_redacted() -> None:
    cli = FakeClient()
    r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

    async def _go() -> None:
        await r.on_turn_start()
        await r.on_text_chunk(f"key {_AWS_KEY[:4]}{NUL}{_AWS_KEY[4:]} end")
        await r._stream_live(force=True)
        await r._seal_current()

    asyncio.run(_go())
    shown = [text for text, _ in cli.sent] + [text for _, text, _ in cli.edits]
    assert shown
    assert all(_AWS_KEY not in text for text in shown)
