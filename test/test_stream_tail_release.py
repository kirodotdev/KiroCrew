"""The dashboard shows a reply's last word sooner, and the mask says what it said.

The live stream withholds the trailing run of credential-class characters, so a
credential split across two chunks is never shown in parts. On the dashboard a run
that is one plain word ending a sentence is released with its own chunk
(``StreamRedactor.release_sentence_end``): no later text can make such a word part
of a credential, and text glued to it never shows more than it did. Every other
run, and every other consumer of the stream (the channels), holds as it did.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from channel_turn_fakes import RecordingRenderer
from turn_harness import Do, TurnContext, TurnRecord, TurnScript, run_turn

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN, AcpEvent
from kiro_crew.messaging import TurnDriver
from kiro_crew.security import REDACTED_CREDENTIAL_TAG, StreamRedactor
from kiro_crew.security.redaction_structured import compat

#: The AWS documentation's example access key id, split so no literal sits here.
_HEAD, _TAIL = "AKIA", "IOSFODNN7EXAMPLE"
_KEY = _HEAD + _TAIL
_TAG = REDACTED_CREDENTIAL_TAG
_JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)

#: A first chunk the stream withholds whole, and the chunk that follows it. In the
#: first six the withheld run is a plain word ending a sentence, so it is released
#: before the second chunk arrives; the rest are held as they always were.
_TWO_CHUNKS = (
    ("admin.", "internal:Sup3rS3cretPw@db.example.com "),
    ("settled.", _JWT + " "),
    ("example.", "com/cb?token=abcdef0123456789abcdef0123456789 "),
    ("config.", "password=hunter2Sup3rS3cretValue99 "),
    ("done.", _KEY + " "),
    ("done.", " Next step follows. "),
    ("postgres", "://app:Sup3rS3cretPw@db.example.com/x "),
    ("postgres://app:Sup3rS3cretPw", "@db.example.com/x "),
    (_HEAD, _TAIL + " after."),
    ("token", "=abcdef0123456789abcdef0123456789 "),
    ("Done", ". And more "),
    ("Bearer.", " eyJhbGciOiJIUzI1NiJ9 "),
)


#: A token shape only the structured detectors know: its prefix reads as a plain
#: word ending a sentence. Built here so no token literal sits in this file.
_PREFIXED = ("hvs.", "Ab3-" * 24 + " ok")

#: A two-part run of the link-token shape, built here for the same reason.
_TWO_PART_HEAD = "eyJ" + "a1B2c3D4" * 13
_TWO_PART = _TWO_PART_HEAD + "." + ("Zz9Yy8Xx7W" * 5)[:43]

#: Text that arrives glued to a released word, with no space between them.
_GLUED = (
    _KEY,
    _JWT,
    _TWO_PART,
    "com/cb?token=abcdef0123456789abcdef0123456789",
    "example.com:Sup3rS3cretPw@db.example.com/x",
)


@pytest.fixture
def detectors_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch the structured detectors on, as only code can, for one test."""
    monkeypatch.setattr(compat, "_USE_RULE_TABLE", True)


def _as_today(*chunks: str) -> str:
    """What the stream emits for *chunks* when nothing releases the tail early."""
    redactor = StreamRedactor()
    return "".join(redactor.feed(chunk) for chunk in chunks) + redactor.flush()


def _with_release(*chunks: str) -> str:
    """The same stream with the sentence-end release asked for after every chunk."""
    redactor = StreamRedactor()
    out = "".join(redactor.feed(chunk) + redactor.release_sentence_end() for chunk in chunks)
    return out + redactor.flush()


class TestSentenceEndRelease:
    def test_a_plain_word_ending_a_sentence_is_released_at_once(self) -> None:
        redactor = StreamRedactor()

        assert redactor.feed("All is done.") == "All is "
        assert redactor.release_sentence_end() == "done."
        assert redactor.held == ""

    @pytest.mark.parametrize(
        "tail",
        ["pong", "42", "ready?", "v1.2.", "/etc/hosts.", _HEAD, "Done.x", "postgres://u:pw"],
    )
    def test_any_other_run_stays_withheld(self, tail: str) -> None:
        redactor = StreamRedactor()
        redactor.feed(f"so {tail}")

        assert redactor.release_sentence_end() == ""
        assert redactor.held == tail

    def test_nothing_withheld_releases_nothing(self) -> None:
        redactor = StreamRedactor()
        redactor.feed("all out ")

        assert redactor.release_sentence_end() == ""

    def test_a_stream_that_never_asks_holds_the_run_as_before(self) -> None:
        redactor = StreamRedactor()

        assert redactor.feed("All is done.") == "All is "
        assert redactor.held == "done."
        assert redactor.flush() == "done."

    @pytest.mark.parametrize("head, rest", _TWO_CHUNKS)
    def test_two_chunks_come_out_byte_for_byte_as_today(self, head: str, rest: str) -> None:
        assert _with_release(head, rest) == _as_today(head, rest)

    @pytest.mark.parametrize("word", ["done.", "settled.", "www.", "token.", "it's."])
    @pytest.mark.parametrize("glued", _GLUED)
    def test_text_glued_to_a_released_word_never_shows_more_than_today(
        self, word: str, glued: str
    ) -> None:
        """The release cuts the run, so what follows is read without the word."""
        today, now = _as_today(word, glued), _with_release(word, glued)
        pieces = [glued[at : at + 8] for at in range(len(glued) - 7)]

        assert [piece for piece in pieces if piece in now and piece not in today] == []

    def test_a_two_part_run_glued_to_a_released_word_is_masked_whole(self) -> None:
        """The one known difference: a ``.`` on its left switches that pattern off."""
        assert _TWO_PART_HEAD in _as_today("settled.", _TWO_PART)
        assert _with_release("settled.", _TWO_PART) == f"settled.{_TAG}"

    @pytest.mark.usefixtures("detectors_on")
    def test_a_unit_the_detectors_still_read_is_not_released(self) -> None:
        redactor = StreamRedactor()
        redactor.feed(_PREFIXED[0])

        assert redactor.release_sentence_end() == ""
        assert redactor.held == _PREFIXED[0]

    @pytest.mark.usefixtures("detectors_on")
    @pytest.mark.parametrize("head, rest", (*_TWO_CHUNKS, _PREFIXED))
    def test_two_chunks_come_out_byte_for_byte_with_the_detectors_on(
        self, head: str, rest: str
    ) -> None:
        assert _TAG in _as_today(*_PREFIXED)
        assert _with_release(head, rest) == _as_today(head, rest)

    def test_a_value_being_dropped_is_not_released(self) -> None:
        redactor = StreamRedactor()
        redactor.feed("Authorization: Bearer " + "a" * 5000)
        assert redactor.discarding

        assert redactor.release_sentence_end() == ""
        assert redactor.discarding


_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
_MARK = "tail_release_test_mark"


def _mark(ctx: TurnContext) -> None:
    """Leave a frame on the wire at this point of the stream."""
    ctx.state.broadcast_ws(_MARK, {})


def _text(text: str) -> AcpEvent:
    return AcpEvent(kind=EVENT_TEXT_CHUNK, text=text)


async def _run(*events: AcpEvent | Do) -> TurnRecord:
    return await run_turn(TurnScript(events=list(events)))


def _before_mark(record: TurnRecord) -> str:
    """The reply text streamed before the mark was left."""
    out = []
    for frame in record.ws_frames:
        if frame.kind == _MARK:
            break
        if frame.kind == "chat_chunk":
            out.append(frame.payload["content"])
    return "".join(out)


def _wire(record: TurnRecord) -> str:
    return "".join(frame.payload["content"] for frame in record.frames("chat_chunk"))


def _saved(record: TurnRecord) -> list[str]:
    return [row["content"] for row in record.rows("assistant")]


class TestTheDashboardTurn:
    @pytest.mark.asyncio
    async def test_a_plain_sentence_end_shows_with_its_own_chunk(self) -> None:
        record = await _run(_text("All is done."), Do(_mark), _text(" More"), _DONE)

        assert _before_mark(record) == "All is done."
        assert _wire(record) == "All is done. More"

    @pytest.mark.asyncio
    async def test_a_reply_that_ends_in_a_plain_sentence_is_whole_before_the_answer(self) -> None:
        record = await _run(_text("All is"), _text(" done."), Do(_mark), _DONE)

        assert _before_mark(record) == "All is done."
        assert _wire(record) == "All is done."
        assert _saved(record) == ["All is done."]

    @pytest.mark.parametrize("tail", ["pong", "ready?", f"key {_HEAD}"])
    @pytest.mark.asyncio
    async def test_any_other_tail_waits_for_the_prompts_answer(self, tail: str) -> None:
        record = await _run(_text(tail), Do(_mark), _DONE)

        assert tail.split()[-1] not in _before_mark(record)
        assert _wire(record) == tail

    @pytest.mark.asyncio
    async def test_a_key_split_across_two_chunks_is_masked_whole(self) -> None:
        record = await _run(_text(f"key {_HEAD}"), _text(_TAIL), Do(_mark), _DONE)

        assert _before_mark(record) == "key "
        assert _wire(record) == f"key {_TAG}"
        assert _saved(record) == [f"key {_TAG}"]

    @pytest.mark.parametrize("head, rest", _TWO_CHUNKS)
    @pytest.mark.asyncio
    async def test_two_chunks_reach_the_wire_as_the_redactor_alone_emits_them(
        self, head: str, rest: str
    ) -> None:
        record = await _run(_text(head), _text(rest), _DONE)

        assert _wire(record) == _as_today(head, rest)


class _Provider:
    """Yields *events*; notes the text the channel had sent when each one was asked for."""

    def __init__(self, events: list[AcpEvent], renderer: RecordingRenderer) -> None:
        self._events, self._renderer = events, renderer
        self.sent_before: list[str] = []

    async def stream(self, message: str):  # noqa: ANN201 - the provider protocol
        for event in self._events:
            self.sent_before.append("".join(self._renderer.shown))
            yield event


class TestTheChannelsHoldAsBefore:
    def test_a_plain_sentence_end_stays_withheld_on_a_channel(self) -> None:
        renderer = RecordingRenderer()
        provider = _Provider([_text("All is done."), _DONE], renderer)

        out = asyncio.run(TurnDriver(provider, renderer).run("hello"))

        assert provider.sent_before == ["", "All is "]
        assert out == "All is done."

    def test_only_the_dashboards_chunk_handler_asks_the_redactor_for_the_release(self) -> None:
        """Slack's stream has a method of the same name; it settles at a tool card with ``flush``."""
        src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        asks = re.compile(r"(?<!self)\.release_sentence_end\(")
        callers = sorted(
            path.relative_to(src).as_posix()
            for path in src.rglob("*.py")
            if asks.search(path.read_text(encoding="utf-8"))
        )

        assert callers == ["dashboard/chat_turn/stream_chunks.py"]
