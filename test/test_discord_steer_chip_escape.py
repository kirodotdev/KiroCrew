"""The Discord steer chip shows the user's quoted words as typed.

Discord parses markup Telegram does not (spoilers, strikethrough, headings and
subtext at a line start), so the chip escapes it with backslashes. Each case is
checked on the chip LINE that lands in the channel, on both chip routes: the
no-rotation summary (``"> " + words``) and the marker rotation
(``"> ↪ " + words``).
"""

from __future__ import annotations

import pytest
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import FakeClient

from kiro_crew.discord import renderer_text
from kiro_crew.discord.renderer import DiscordRenderer, _neutralize_md, _redact_all
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.telegram.renderer_display import _neutralize_md as telegram_neutralize_md

# The rotation chip prefix: an arrow plus emoji variation selector U+FE0F.
ROTATION_CHIP = "> \u21aa\ufe0f "

# (typed words, escaped chip text)
CASES = [
    ("||secret||", r"\|\|secret\|\|"),
    ("~~gone~~", r"\~\~gone\~\~"),
    ("# Title", r"\# Title"),
    ("-# small", r"\-# small"),
    ("- item", r"\- item"),
    ("plain words here", "plain words here"),
]


def _renderer() -> tuple[DiscordRenderer, FakeClient]:
    cli = FakeClient()
    r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
    return r, cli


def _all_texts(cli: FakeClient) -> list[str]:
    return [t for t, _ in cli.sent] + [t for _, t, _ in cli.edits]


@pytest.mark.parametrize(("typed", "escaped"), CASES)
def test_neutralize_escapes_discord_markup(typed: str, escaped: str) -> None:
    assert _neutralize_md(typed) == escaped


def test_typed_backslash_cannot_cancel_an_escape() -> None:
    # ``\||x||`` unescaped-backslash-first would leave ``\\||`` and reopen a spoiler.
    assert _neutralize_md(r"\||x||") == r"\\\|\|x\|\|"


def test_heading_escape_only_at_line_start() -> None:
    assert _neutralize_md("see # 3 and -# x") == "see # 3 and -# x"


# An escape inserted inside a token would split it before the send-time
# redaction pass, and Discord renders the ``\`` away: the tail would leak.
SECRET_STEERS = [
    "Authorization: Bearer abc~SECRETTOKENVALUEXYZ1234567890",
    "https://x.example/cb?token=ab~SECRETTOKENVALUEXYZ1234567890",
]


@pytest.mark.parametrize("typed", SECRET_STEERS)
def test_steer_redactor_matches_the_send_redactor(typed: str) -> None:
    assert renderer_text._redact_steer(typed) == _redact_all(typed)


@pytest.mark.parametrize("typed", SECRET_STEERS)
def test_credential_is_redacted_before_escaping(typed: str) -> None:
    out = _neutralize_md(typed)
    assert "SECRETTOKENVALUE" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", SECRET_STEERS)
async def test_no_rotation_chip_leaks_no_credential_tail(typed: str) -> None:
    r, cli = _renderer()
    await r.on_turn_start()
    r.note_steer(typed)
    await r.on_text_chunk("answer body")
    await r.on_done()
    assert all("SECRETTOKENVALUE" not in t for t in _all_texts(cli))


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", SECRET_STEERS)
async def test_marker_chip_leaks_no_credential_tail(typed: str) -> None:
    r, cli = _renderer()
    await r.on_turn_start()
    await r.on_text_chunk(f"first part [STEERING steer-ab12: {typed}] second part")
    await r.on_done()
    assert all("SECRETTOKENVALUE" not in t for t in _all_texts(cli))


@pytest.mark.parametrize("typed", [c[0] for c in CASES])
def test_telegram_copy_unchanged(typed: str) -> None:
    # Telegram has no spoiler/strike/heading syntax here: its copy stays a strip.
    assert telegram_neutralize_md(typed) == typed


@pytest.mark.asyncio
@pytest.mark.parametrize(("typed", "escaped"), CASES)
async def test_no_rotation_chip_line_shows_words_as_typed(typed: str, escaped: str) -> None:
    r, cli = _renderer()
    await r.on_turn_start()
    r.note_steer(typed)
    await r.on_text_chunk("answer body")
    await r.on_done()
    lines = cli.final_text().splitlines()
    assert lines[0] == "> " + escaped


@pytest.mark.asyncio
@pytest.mark.parametrize(("typed", "escaped"), CASES)
async def test_marker_chip_line_escapes_once(typed: str, escaped: str) -> None:
    r, cli = _renderer()
    await r.on_turn_start()
    await r.on_text_chunk(f"first part [STEERING steer-ab12: {typed}] second part")
    await r.on_done()
    chip_lines = [
        ln for t in _all_texts(cli) for ln in t.splitlines() if ln.startswith(ROTATION_CHIP)
    ]
    assert chip_lines, _all_texts(cli)
    assert set(chip_lines) == {ROTATION_CHIP + escaped}
