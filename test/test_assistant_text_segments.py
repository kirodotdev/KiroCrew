"""The open reply segment is built from its chunks without re-copying the prefix.

``turn.assistant_text += chunk`` on an attribute copies the whole segment on every
chunk, so a 1.5 MB reply in 77-character chunks copies about 15 GB and holds the
event loop for seconds. ``_ChatTurn`` keeps the chunks as a list and joins them on
read; these tests count the bytes that join is handed, so the budget is a byte
count, never a duration.
"""

from __future__ import annotations

import pytest
from chat_runner_coverage_helpers import _complete, _drive, _runner_state, _set_stream, _slot

from kiro_crew.acp.types import EVENT_TEXT_CHUNK
from kiro_crew.dashboard.chat_turn import turn_locals
from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn
from kiro_crew.providers.base import LLMEvent


def _joined_bytes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the total length of every list the segment join is handed.

    ``chat_runner`` composes ``_ChatTurn`` onto its own module globals, so the
    patch goes on the namespace the property actually reads, not on ``turn_locals``.
    """
    seen: list[int] = []
    real = turn_locals._join_segments

    def counting(parts: list[str]) -> str:
        seen.append(sum(len(p) for p in parts))
        return real(parts)

    globs = _ChatTurn.assistant_text.fget.__globals__  # type: ignore[attr-defined]
    monkeypatch.setitem(globs, "_join_segments", counting)
    return seen


def _chunks(n: int) -> list[str]:
    return [f"word{i:05d} and more text in this chunk, " for i in range(n)]


def test_ten_thousand_appends_copy_each_byte_once(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _joined_bytes(monkeypatch)
    turn = _ChatTurn.__new__(_ChatTurn)
    turn.assistant_text = ""
    chunks = _chunks(10_000)
    for chunk in chunks:
        assert turn.has_assistant_text is (chunk is not chunks[0])
        turn.append_assistant_text(chunk)

    expected = "".join(chunks)
    assert turn.assistant_text == expected
    assert turn.assistant_text == expected  # a second read joins nothing
    assert seen == [len(expected)]


def test_reads_between_appends_match_string_concatenation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interleaved reads, empty chunks and a reset give exactly what ``+=`` gave."""
    turn = _ChatTurn.__new__(_ChatTurn)
    turn.assistant_text = ""
    base = ""
    for i, chunk in enumerate(["a", "", "bc", "", "def", "g"] * 50):
        turn.append_assistant_text(chunk)
        base += chunk
        if i % 7 == 0:
            assert turn.assistant_text == base
        assert turn.has_assistant_text is bool(base)
    assert turn.assistant_text == base
    turn.assistant_text = "kept tail"
    turn.append_assistant_text("!")
    assert turn.assistant_text == "kept tail!"
    turn.assistant_text = ""
    assert turn.assistant_text == "" and not turn.has_assistant_text


@pytest.mark.asyncio
async def test_a_streamed_turn_joins_its_reply_once(tmp_path, monkeypatch) -> None:
    """Through ``_run_chat``: the text-chunk handler appends rather than rebinding,
    so a 2,000-chunk reply is joined once and saved byte-identical."""
    seen = _joined_bytes(monkeypatch)
    state, client = _runner_state(tmp_path)
    slot = _slot()
    chunks = _chunks(2_000)
    _set_stream(
        client,
        [LLMEvent(kind=EVENT_TEXT_CHUNK, text=chunk) for chunk in chunks] + [_complete()],
    )

    await _drive(state, slot, "hello")

    reply = "".join(chunks)
    saved = [row["content"] for row in slot.messages if row["role"] == "assistant"]
    assert saved and saved[-1].rstrip() == reply.rstrip()
    assert sum(seen) == len(reply), seen
