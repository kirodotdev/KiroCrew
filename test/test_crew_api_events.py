"""``GET /v1/crew/events`` and what the gateway keeps of a conversation's event stream.

The route runs around the same stand-in for ``_run_chat`` as ``test_crew_api_responses.py``.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_crew_api_conversations import (
    NOT_IDS,
    UNKNOWN,
    _Clock,
    _conversations,
    _create,
    _refusal,
)
from test_crew_api_responses import AS_OWNER, ASK, CALLERS, SPEC_DIR, _ask, _valid
from test_crew_api_responses import client as _responses_client

from kiro_crew.dashboard import crew_api_conversations as conversations
from kiro_crew.dashboard import crew_api_event_records as event_records
from kiro_crew.dashboard import crew_api_events as events
from kiro_crew.dashboard.crew_api_conversations import OWNER, ConversationStore
from kiro_crew.dashboard.crew_api_event_records import EventLog, Resync
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout

client = _responses_client  # the route fixture of the POST /v1/responses tests


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


PATH = "/v1/crew/events"
ITEMS = "/v1/conversations/{}/items"
OPERATION = yaml.safe_load((SPEC_DIR / "crew-public.yaml").read_text(encoding="utf-8"))["paths"][
    PATH
]["get"]
CONV = "conv_" + "1" * 32
RESP = "resp_" + "2" * 32
QUOTA = ["X-Crew-Quota-Limit", "X-Crew-Quota-Remaining", "X-Crew-Quota-Reset"]


@pytest.fixture
def log(tmp_path) -> EventLog:
    return EventLog(directory=tmp_path / "events", clock=_Clock())


def _seqs(page: dict[str, Any]) -> list[int]:
    return [event["seq"] for event in page["events"]]


def _types(page: dict[str, Any]) -> list[str]:
    return [event["type"] for event in page["events"]]


async def _get(client, headers: dict[str, str] | None = None, **query: Any):
    response = await client.get(
        PATH,
        params={name: str(value) for name, value in query.items()},
        headers=AS_OWNER if headers is None else headers,
    )
    return response, await response.json()


def _documented(response) -> None:
    assert str(response.status) in OPERATION["responses"], response.status
    assert response.headers["Crew-API-Version"] == "1.0"
    assert response.headers["X-Request-Id"]


def _fail_writes_of(monkeypatch, log: EventLog, suffixes: tuple[str, ...]) -> None:
    """Make every write of a file whose name ends in one of *suffixes* fail."""
    write = log._write

    def _write(path: Path, text: str) -> None:
        if path.name.endswith(suffixes):
            raise OSError("disk full")
        write(path, text)

    monkeypatch.setattr(log, "_write", _write)


# ── the stream's record ──────────────────────────────────────────────────────


def test_events_are_numbered_from_one_in_the_order_written(log) -> None:
    log.append(CONV, event_records.TURN_COMPLETED, RESP)
    log.append(CONV, event_records.MESSAGE_CREATED, RESP, item_id="msg_1")
    log.append(CONV, event_records.TURN_COMPLETED, RESP, result="completed")
    page = log.events_after(CONV, 0, 100)
    assert _seqs(page) == [1, 2, 3]
    assert (page["latest_seq"], page["oldest_seq"], page["has_more"]) == (3, 1, False)
    assert page["events"][1] == {
        "seq": 2,
        "event_id": page["events"][1]["event_id"],
        "type": "message.created",
        "conversation": CONV,
        "response": RESP,
        "created_at": 1_800_000_000,
        "data": {"item_id": "msg_1"},
    }
    assert len({event["event_id"] for event in page["events"]}) == 3
    _valid("EventPage", page)
    # One line per event in the stream's own file, and nothing else in the directory.
    assert [p.name for p in log.directory.iterdir()] == [CONV + ".jsonl"]
    assert len((log.directory / (CONV + ".jsonl")).read_text(encoding="utf-8").splitlines()) == 3


def test_a_page_is_what_comes_after_the_cursor(log) -> None:
    for _ in range(5):
        log.append(CONV, event_records.TURN_COMPLETED, RESP)
    page = log.events_after(CONV, 1, 2)
    assert (_seqs(page), page["has_more"], page["latest_seq"]) == ([2, 3], True, 5)
    page = log.events_after(CONV, 3, 2)
    assert (_seqs(page), page["has_more"]) == ([4, 5], False)
    assert log.events_after(CONV, 5, 2)["events"] == []
    # No cursor: nothing that was already written, and where the stream is now.
    assert log.events_after(CONV, None, 2) == {
        "events": [],
        "latest_seq": 5,
        "oldest_seq": 1,
        "has_more": False,
    }
    assert log.events_after(CONV, 0, 0) == {
        "events": [],
        "latest_seq": 5,
        "oldest_seq": 1,
        "has_more": True,
    }


def test_a_stream_with_no_event_reads_empty(log) -> None:
    assert log.events_after(CONV, 0, 100) == {
        "events": [],
        "latest_seq": 0,
        "oldest_seq": 0,
        "has_more": False,
    }


def test_a_cursor_past_the_newest_event_must_resync(log) -> None:
    log.append(CONV, event_records.TURN_COMPLETED, RESP)
    with pytest.raises(Resync) as raised:
        log.events_after(CONV, 2, 100)
    assert (raised.value.oldest_seq, raised.value.latest_seq) == (1, 1)


def _resync(reader: EventLog, cursor: int) -> tuple[int, int]:
    """The ``(oldest_seq, latest_seq)`` a reader at *cursor* is told to resync with."""
    with pytest.raises(Resync) as raised:
        reader.events_after(CONV, cursor, 100)
    return raised.value.oldest_seq, raised.value.latest_seq


def _lines(log: EventLog) -> list[dict[str, Any]]:
    text = (log.directory / (CONV + ".jsonl")).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _lose_an_event(log: EventLog, monkeypatch, suffixes: tuple[str, ...] = (".jsonl",)) -> None:
    with monkeypatch.context() as patched:
        _fail_writes_of(patched, log, suffixes)
        log.append(CONV, event_records.TURN_COMPLETED, RESP, result="completed")  # never raises


def test_an_event_that_was_not_written_breaks_the_stream_until_it_reads_whole(
    log, monkeypatch
) -> None:
    log.append(CONV, event_records.TURN_COMPLETED, RESP)
    marker = log.directory / (CONV + ".gap")
    with monkeypatch.context() as patched:
        _fail_writes_of(patched, log, (".jsonl",))
        log.append(CONV, event_records.TURN_COMPLETED, RESP, result="completed")
        assert marker.read_bytes() == b"1\n"
        # Broken, and it cannot be mended yet: every cursor is told to resync, and
        # no number that is not a line of the file is given out.
        for cursor in (0, 1):
            assert _resync(log, cursor) == (1, 1)
        assert log.events_after(CONV, None, 100)["latest_seq"] == 1
    # The next process finds the stream whole: the gap becomes a line of the file.
    restarted = EventLog(directory=log.directory)
    assert _resync(restarted, 1) == (2, 2)
    assert not marker.exists()
    assert [sorted(line) for line in _lines(log)][1] == ["gap"] and _lines(log)[1] == {"gap": 2}
    assert restarted.events_after(CONV, 2, 100)["events"] == []
    restarted.append(CONV, event_records.TURN_COMPLETED, RESP)
    for reader in (restarted, EventLog(directory=log.directory)):
        assert _seqs(reader.events_after(CONV, 2, 100)) == [3]
        for cursor in (0, 1):
            assert _resync(reader, cursor) == (3, 3)


def test_a_second_loss_is_numbered_above_the_gap_a_reader_already_holds(log, monkeypatch) -> None:
    """Every step in a new process, so nothing is carried in memory."""
    log.append(CONV, event_records.TURN_COMPLETED, RESP)
    _lose_an_event(log, monkeypatch)
    # A reader resyncs and now holds 2, the gap's own number.
    assert _resync(EventLog(directory=log.directory), 1) == (2, 2)
    _lose_an_event(EventLog(directory=log.directory), monkeypatch)
    EventLog(directory=log.directory).append(CONV, event_records.TURN_COMPLETED, RESP)
    assert [line.get("gap", line.get("seq")) for line in _lines(log)] == [1, 2, 3, 4]
    reader = EventLog(directory=log.directory)
    assert _resync(reader, 2) == (4, 4)
    assert _seqs(reader.events_after(CONV, 3, 100)) == [4]


def test_a_stream_that_cannot_be_read_after_a_restart_gives_no_number_again(
    log, monkeypatch
) -> None:
    for _ in range(4):
        log.append(CONV, event_records.TURN_COMPLETED, RESP)
    restarted = EventLog(directory=log.directory)
    read = event_records.read_bytes_with_retry
    error = OSError("stream read failed")

    def _fail_stream(path: Path, **kwargs: Any) -> bytes:
        if path.suffix == ".jsonl":
            raise error
        return read(path, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(event_records, "read_bytes_with_retry", _fail_stream)
        restarted.append(CONV, event_records.TURN_COMPLETED, RESP)  # never raises
        assert (log.directory / (CONV + ".gap")).exists()
        # Unreadable: no page is served, and no number is made up.
        with pytest.raises(OSError) as raised:
            restarted.events_after(CONV, 4, 100)
        assert raised.value is error
    restarted.append(CONV, event_records.TURN_COMPLETED, RESP)
    for reader in (restarted, EventLog(directory=log.directory)):
        assert _resync(reader, 4) == (6, 6)
        assert _seqs(reader.events_after(CONV, 5, 100)) == [6]


def test_a_broken_stream_whose_marker_was_not_written_is_broken_in_memory_only(
    log, monkeypatch
) -> None:
    log.append(CONV, event_records.TURN_COMPLETED, RESP)
    _lose_an_event(log, monkeypatch, (".jsonl", ".gap", ".open"))
    assert [p.name for p in log.directory.iterdir()] == [CONV + ".jsonl"]
    # The limit: another process has nothing that tells it of the missing event.
    assert EventLog(directory=log.directory).events_after(CONV, 1, 100)["events"] == []
    assert _resync(log, 1) == (2, 2)
    assert _lines(log)[-1] == {"gap": 2}


def test_a_marker_that_cannot_be_looked_at_counts_as_there(log, monkeypatch) -> None:
    log.append(CONV, event_records.TURN_COMPLETED, RESP)
    stat = os.stat

    def _refused(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if str(path).endswith(".gap"):
            raise PermissionError("no")
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(event_records.os, "stat", _refused)
    assert _resync(log, 1) == (2, 2)


def test_a_marker_that_cannot_be_removed_adds_no_gap_until_the_next_loss(log, monkeypatch) -> None:
    log.append(CONV, event_records.TURN_COMPLETED, RESP)
    _lose_an_event(log, monkeypatch)
    unlink = Path.unlink

    def _stuck(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.suffix == ".gap":
            raise PermissionError("no")
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _stuck)
    assert _resync(log, 1) == (2, 2)
    # Mended once: later reads and writes add no gap line for the marker left behind.
    assert log.events_after(CONV, 2, 100)["events"] == []
    log.append(CONV, event_records.TURN_COMPLETED, RESP)
    assert [line.get("gap", line.get("seq")) for line in _lines(log)] == [1, 2, 3]
    assert _seqs(log.events_after(CONV, 2, 100)) == [3]
    # A new loss breaks it again; another process reads the marker as one more loss.
    _lose_an_event(log, monkeypatch)
    assert _resync(log, 3) == (4, 4)
    assert _resync(EventLog(directory=log.directory), 4) == (5, 5)


def _foreign_line() -> str:
    line = {
        "seq": 4,
        "event_id": "evt_1",
        "type": "turn.started",
        "conversation": "conv_" + "9" * 32,
        "response": RESP,
        "created_at": 1,
        "data": {},
    }
    return json.dumps(line) + "\n"


@pytest.mark.parametrize(
    "damage",
    [
        lambda kept: kept + "not json\n",
        lambda kept: kept + "{",
        lambda kept: "not json\n" + kept,
        lambda kept: kept + json.dumps({"seq": 4}) + "\n",
        lambda kept: kept + kept,
        lambda kept: kept + json.dumps({"gap": 3}) + "\n",
        lambda kept: kept + "\n",
    ],
    ids=["garbage-last", "cut-line", "garbage-first", "bare-seq", "repeated", "low-gap", "blank"],
)
def test_a_stream_that_cannot_be_believed_becomes_one_gap_above_what_it_names(log, damage) -> None:
    for _ in range(3):
        log.append(CONV, event_records.TURN_COMPLETED, RESP)
    path = log.directory / (CONV + ".jsonl")
    path.write_text(damage(path.read_text(encoding="utf-8")), encoding="utf-8")
    named = event_records._highest_number_named(path)
    assert named >= 3
    restarted = EventLog(directory=log.directory)
    # Every number a reader may hold is at most what the file names, so each resyncs.
    for cursor in (0, 3, named):
        assert _resync(restarted, cursor) == (named + 1, named + 1)
    assert _lines(log) == [{"gap": named + 1}]
    assert [p.name for p in log.directory.iterdir()] == [CONV + ".jsonl"]
    restarted.append(CONV, event_records.TURN_COMPLETED, RESP)
    assert _seqs(EventLog(directory=log.directory).events_after(CONV, named + 1, 100)) == [
        named + 2
    ]


def test_a_line_of_another_conversation_makes_the_stream_unbelievable(log) -> None:
    for _ in range(3):
        log.append(CONV, event_records.TURN_COMPLETED, RESP)
    path = log.directory / (CONV + ".jsonl")
    path.write_text(path.read_text(encoding="utf-8") + _foreign_line(), encoding="utf-8")
    assert _resync(EventLog(directory=log.directory), 3) == (5, 5)


def test_a_stream_file_longer_than_one_is_written_is_not_read_as_whole(log, monkeypatch) -> None:
    for _ in range(4):
        log.append(CONV, event_records.TURN_COMPLETED, RESP)
    path = log.directory / (CONV + ".jsonl")
    whole = path.read_bytes()
    # The cap falls exactly at the end of the third line: a prefix that parses.
    monkeypatch.setattr(
        event_records, "MAX_FILE_BYTES", whole.index(b"\n", whole.rindex(b'"seq": 3')) + 1
    )
    restarted = EventLog(directory=log.directory)
    # The fourth line is past the cap, and it is still found: the gap is above it.
    assert _resync(restarted, 3) == (5, 5)
    assert _lines(log) == [{"gap": 5}]
    assert _resync(restarted, 4) == (5, 5)
    assert restarted.events_after(CONV, 5, 100)["events"] == []


def test_the_highest_number_is_looked_for_to_the_end_of_a_file_of_any_length(
    log, monkeypatch
) -> None:
    for _ in range(3):
        log.append(CONV, event_records.TURN_COMPLETED, RESP)
    path = log.directory / (CONV + ".jsonl")
    monkeypatch.setattr(event_records, "MAX_FILE_BYTES", 2048)
    monkeypatch.setattr(event_records, "_LINE_MAX", 256)
    monkeypatch.setattr(event_records, "_CHUNK", 16)
    with open(path, "ab") as grown:
        grown.write(b"garbage\n" * 1024)  # four times the cap
        grown.write(b'{"gap": 900}\r\n')
        # Longer than a line is ever written: no reader was given its number.
        grown.write(b'{"seq": 5000, "pad": "' + b"x" * 600 + b'"}\n')
        grown.write(b'{"seq": 950}')  # the last line, with no line end
    assert path.stat().st_size > 4 * 2048
    asked: list[int | None] = []
    read = event_records.read_bytes_with_retry

    def _bounded(target: Path, *, max_bytes: int | None = None) -> bytes:
        asked.append(max_bytes)
        return read(target, max_bytes=max_bytes)

    class _Pieces:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        def __enter__(self) -> _Pieces:
            return self

        def __exit__(self, *_exc: Any) -> None:
            self._stream.close()

        def read(self, size: int) -> bytes:
            assert size == 16, "the file is read a piece at a time"
            return self._stream.read(size)

    monkeypatch.setattr(event_records, "read_bytes_with_retry", _bounded)
    monkeypatch.setattr(
        event_records, "open", lambda target, mode: _Pieces(open(target, mode)), raising=False
    )
    restarted = EventLog(directory=log.directory)
    assert _resync(restarted, 3) == (951, 951)
    # Never the whole file at once: the first read is capped, the second is in pieces.
    assert asked[0] == 2049
    assert _lines(log) == [{"gap": 951}]


def test_a_line_longer_than_one_is_ever_written_is_not_written(log, monkeypatch) -> None:
    log.append(CONV, event_records.TURN_COMPLETED, RESP)
    monkeypatch.setattr(event_records, "_LINE_MAX", 256)
    log.append(CONV, event_records.TURN_COMPLETED, RESP, result="x" * 300)  # never raises
    assert [line.get("seq") for line in _lines(log)] == [1]
    assert _resync(log, 1) == (2, 2)


def test_an_unbelievable_stream_that_cannot_be_replaced_stays_and_is_not_served(
    log, monkeypatch
) -> None:
    log.append(CONV, event_records.TURN_COMPLETED, RESP)
    path = log.directory / (CONV + ".jsonl")
    damaged = path.read_bytes() + b"not json\n"
    path.write_bytes(damaged)
    with monkeypatch.context() as patched:
        _fail_writes_of(patched, log, (".jsonl",))
        with pytest.raises(OSError):
            log.events_after(CONV, 1, 100)
        assert path.read_bytes() == damaged
    assert _resync(log, 1) == (2, 2)


def test_a_stream_that_names_no_number_becomes_gap_one(log) -> None:
    """The limit: nothing in such a file says what its readers hold."""
    (log.directory).mkdir(parents=True)
    (log.directory / (CONV + ".jsonl")).write_text("garbage\n", encoding="utf-8")
    assert _resync(log, 0) == (1, 1)
    assert _resync(log, 2) == (1, 1)
    assert log.events_after(CONV, 1, 100)["events"] == []


@pytest.mark.parametrize("suffix", [".jsonl", ".gap"])
def test_stream_and_marker_writes_use_lf_only(log, monkeypatch, suffix) -> None:
    write = event_records.atomic_write
    calls: list[dict[str, Any]] = []

    def _capture(path: Path, text: str, **kwargs: Any) -> None:
        calls.append(kwargs)
        write(path, text, **kwargs)

    monkeypatch.setattr(event_records, "atomic_write", _capture)
    path = log.directory / (CONV + suffix)
    log._write(path, "1\n2\n")
    assert path.read_bytes() == b"1\n2\n" and b"\r" not in path.read_bytes()
    assert calls == [{"fsync": True, "restrict_to_owner": True, "newline": "\n"}]


def test_a_stream_with_crlf_line_ends_reads_and_is_written_back_with_lf(log) -> None:
    for _ in range(2):
        log.append(CONV, event_records.TURN_COMPLETED, RESP)
    path = log.directory / (CONV + ".jsonl")
    # What a build that translated newlines left on a Windows disk.
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert _seqs(log.events_after(CONV, 0, 100)) == [1, 2]
    log.append(CONV, event_records.TURN_COMPLETED, RESP)
    assert b"\r" not in path.read_bytes()
    assert _seqs(log.events_after(CONV, 0, 100)) == [1, 2, 3]
    assert not (log.directory / (CONV + ".gap")).exists()


def test_the_record_files_are_written_without_newline_translation(tmp_path, monkeypatch) -> None:
    """The size each store checks is the size of the file: no write adds a byte."""
    from kiro_crew.dashboard import crew_api_approval_records as approval_records

    newlines: list[Any] = []
    for module in (conversations, approval_records):
        write = module.atomic_write

        def _capture(path: Path, text: str, _write: Any = write, **kwargs: Any) -> None:
            newlines.append(kwargs.get("newline"))
            _write(path, text, **kwargs)

        monkeypatch.setattr(module, "atomic_write", _capture)
    made = ConversationStore(directory=tmp_path / "conversations").create({}, OWNER)
    approval_records.save(tmp_path / "approvals" / "approvals.json", [])
    assert newlines == ["\n", "\n"]
    assert b"\r" not in (tmp_path / "conversations" / (made["id"] + ".json")).read_bytes()
    # The response store's own test pins the same argument on its write.


def test_the_stream_size_guard_counts_encoded_bytes(log, monkeypatch) -> None:
    dumps = json.dumps
    texts: list[str] = []

    def _unicode(event: Any, **options: Any) -> str:
        if options or not (isinstance(event, dict) and event.get("conversation") == CONV):
            # json is the whole process's: any call but the stream's own goes on as it was.
            return dumps(event, **options)
        text = dumps(event, ensure_ascii=False)
        texts.append(text + "\n")
        monkeypatch.setattr(event_records, "MAX_FILE_BYTES", len(text) + 1)
        return text

    monkeypatch.setattr(event_records.json, "dumps", _unicode)
    log.append(CONV, event_records.MESSAGE_CREATED, RESP, item_id="消息")
    assert len(texts) == 1
    assert len(texts[0]) <= event_records.MAX_FILE_BYTES < len(texts[0].encode("utf-8"))
    assert not log._path(CONV).exists()
    with pytest.raises(Resync):
        log.events_after(CONV, 0, 100)


def test_a_stream_too_large_to_read_back_is_not_written(log, monkeypatch) -> None:
    log.append(CONV, event_records.TURN_COMPLETED, RESP)
    size = (log.directory / (CONV + ".jsonl")).stat().st_size
    monkeypatch.setattr(event_records, "MAX_FILE_BYTES", size + 10)
    log.append(CONV, event_records.TURN_COMPLETED, RESP)
    assert (log.directory / (CONV + ".jsonl")).stat().st_size == size
    with pytest.raises(Resync):
        log.events_after(CONV, 1, 100)


def test_removing_a_stream_removes_its_gap_marker_too(log, monkeypatch) -> None:
    other = "conv_" + "3" * 32
    log.append(other, event_records.TURN_COMPLETED, RESP)
    log.append(CONV, event_records.TURN_COMPLETED, RESP)
    with monkeypatch.context() as patched:
        _fail_writes_of(patched, log, (".jsonl",))
        log.append(CONV, event_records.TURN_COMPLETED, RESP)
    log.remove(CONV)
    assert [p.name for p in log.directory.iterdir()] == [other + ".jsonl"]
    assert log.events_after(CONV, 0, 100)["latest_seq"] == 0
    log.remove(CONV)  # nothing left to remove


def test_streams_of_conversations_that_are_gone_are_removed(log) -> None:
    kept, gone = CONV, "conv_" + "3" * 32
    log.append(kept, event_records.TURN_COMPLETED, RESP)
    log.append(gone, event_records.TURN_COMPLETED, RESP)
    (log.directory / "notes.txt").write_text("not a stream", encoding="utf-8")
    assert log.remove_without(lambda conversation_id: conversation_id == kept) == 1
    assert sorted(p.name for p in log.directory.iterdir()) == [kept + ".jsonl", "notes.txt"]


def test_a_conversation_whose_time_is_up_takes_its_stream_with_it(tmp_path) -> None:
    clock = _Clock()
    store = ConversationStore(directory=tmp_path / "conversations", clock=clock)
    read_again, swept = store.create({}, OWNER)["id"], store.create({}, OWNER)["id"]
    for conversation_id in (read_again, swept):
        store.events.append(conversation_id, event_records.TURN_COMPLETED, RESP)
    clock.now += conversations.RETENTION_SECS
    assert store.load(read_again, OWNER) is None
    assert [p.name for p in store.events.directory.iterdir()] == [swept + ".jsonl"]
    # The sweep, which reads no record, takes the stream of the one nobody read again.
    store._swept_at = None
    store.sweep()
    assert list(store.events.directory.iterdir()) == []


def test_the_streams_live_in_the_directory_no_sandbox_reads(tmp_path) -> None:
    from kiro_crew import member_records

    store = ConversationStore()
    store.events.append(CONV, event_records.TURN_COMPLETED, RESP)
    directory = tmp_path / member_records.MEMBER_RECORDS_LEAF / "crew-api-events"
    assert [p.name for p in directory.iterdir()] == [CONV + ".jsonl"]
    assert event_records.EVENTS_KIND in member_records.RECORD_KINDS


# ── the route: what a turn leaves in its conversation's stream ───────────────


@pytest.mark.asyncio
async def test_a_turn_is_announced_then_its_items_then_its_end(client) -> None:
    _response, made = await _create(client)
    _response, turn = await _ask(client, {**ASK, "conversation": made["id"]})
    response, page = await _get(client, conversation=made["id"], since=0)
    assert response.status == 200, page
    _documented(response)
    _valid("EventPage", page)
    assert _types(page) == ["turn.started", "message.created", "message.created", "turn.completed"]
    assert _seqs(page) == [1, 2, 3, 4]
    assert (page["latest_seq"], page["has_more"]) == (4, False)
    for event in page["events"]:
        assert (event["conversation"], event["response"]) == (made["id"], turn["id"])
    assert page["events"][0]["data"] == {}
    assert page["events"][3]["data"] == {"result": "completed"}
    # The messages are named by the ids the history serves them under, in its order.
    history = await client.get(ITEMS.format(made["id"]), params={"order": "asc"}, headers=AS_OWNER)
    listed = [item["id"] for item in (await history.json())["data"]]
    assert [event["data"] for event in page["events"][1:3]] == [{"item_id": i} for i in listed]
    for sent in QUOTA:
        assert sent not in response.headers


@pytest.mark.asyncio
async def test_a_turn_that_starts_its_own_conversation_is_in_its_stream(client) -> None:
    _response, turn = await _ask(client)
    _response, page = await _get(client, conversation=turn["conversation"]["id"], since=0)
    assert _types(page)[0] == "turn.started" and _types(page)[-1] == "turn.completed"
    assert {event["response"] for event in page["events"]} == {turn["id"]}


@pytest.mark.asyncio
async def test_a_turn_that_failed_ends_failed(client) -> None:
    _response, made = await _create(client)
    client.turn.rows = [("error", "the model is unavailable", None)]
    _response, turn = await _ask(client, {**ASK, "conversation": made["id"]})
    assert turn["status"] == "failed"
    _response, page = await _get(client, conversation=made["id"], since=0)
    assert _types(page) == ["turn.started", "message.created", "turn.completed"]
    assert page["events"][-1]["data"] == {"result": "failed"}


@pytest.mark.asyncio
async def test_a_running_turn_is_started_and_not_yet_completed(client) -> None:
    _response, made = await _create(client)
    client.turn.release = asyncio.Event()
    running = asyncio.ensure_future(_ask(client, {**ASK, "conversation": made["id"]}))
    try:
        await await_until(client.turn.started, what="the turn to start")

        async def _started() -> bool:
            return _types((await _get(client, conversation=made["id"], since=0))[1]) == [
                "turn.started"
            ]

        await async_wait_until(_started)
    finally:
        client.turn.release.set()
        await await_until(running, what="the turn's answer")
    _response, page = await _get(client, conversation=made["id"], since=1)
    assert _types(page)[-1] == "turn.completed"


@pytest.mark.asyncio
async def test_a_handler_that_ends_without_an_answer_still_ends_the_turn_in_the_stream(
    client, monkeypatch
) -> None:
    _response, made = await _create(client)

    async def _raises(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the answer could not be made")

    from kiro_crew.dashboard import crew_api_responses as responses

    monkeypatch.setattr(responses, "_answer", _raises)
    response = await client.post(
        "/v1/responses", json={**ASK, "conversation": made["id"]}, headers=AS_OWNER
    )
    assert response.status == 500

    async def _ended() -> bool:
        page = (await _get(client, conversation=made["id"], since=0))[1]
        return _types(page) == ["turn.started", "turn.completed"]

    await async_wait_until(_ended)
    _response, page = await _get(client, conversation=made["id"], since=0)
    assert page["events"][-1]["data"] == {"result": "failed"}


@pytest.mark.asyncio
async def test_a_turns_end_is_written_after_its_start_though_the_handler_is_cancelled(
    log, monkeypatch
) -> None:
    from kiro_crew.dashboard import crew_api_responses as responses

    append = log.append
    writing, let_on = threading.Event(), threading.Event()

    def _slow_start(conversation_id: str, kind: str, response_id: str, **data: Any) -> None:
        if kind == event_records.TURN_STARTED:
            writing.set()
            assert let_on.wait(default_timeout()), "the start was never let on"
        append(conversation_id, kind, response_id, **data)

    monkeypatch.setattr(log, "append", _slow_start)
    turn = responses._TurnStream(log, CONV, RESP)
    started = turn.started()
    await async_wait_until(writing.is_set)
    # The handler asks for the end while the start is still being written, and is
    # cancelled while it waits for it.
    ended = turn.ended_with(["msg_1"], "cancelled")
    handler = asyncio.ensure_future(asyncio.shield(ended))
    await asyncio.sleep(0)
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    assert turn.ended and not ended.done()
    let_on.set()
    await await_until(started, what="the start to be written")
    await await_until(ended, what="the end to be written")
    assert _types(log.events_after(CONV, 0, 100)) == [
        "turn.started",
        "message.created",
        "turn.completed",
    ]


@pytest.mark.asyncio
async def test_an_event_that_cannot_be_written_never_changes_the_turn(client, monkeypatch) -> None:
    _response, made = await _create(client)
    _response, _first = await _ask(client, {**ASK, "conversation": made["id"]})
    with monkeypatch.context() as patched:
        _fail_writes_of(patched, _conversations(client).events, (".jsonl",))
        response, turn = await _ask(client, {**ASK, "conversation": made["id"]})
        assert (response.status, turn["status"]) == (200, "completed")
        record = _conversations(client).load(made["id"], OWNER)
        assert record["turns"][-1] == turn["id"]
        # While the stream is broken, a reader is told to resync whatever it holds.
        for cursor in (0, 4):
            response, body = await _get(client, conversation=made["id"], since=cursor)
            error = _refusal(response, body, 410, "resync_required")
            assert (error["oldest_seq"], error["latest_seq"]) == (1, 4)
    # Whole again: a reader that had the first turn's events learns it missed some.
    response, body = await _get(client, conversation=made["id"], since=4)
    error = _refusal(response, body, 410, "resync_required")
    assert error["type"] == "resync_required_error"
    assert (error["oldest_seq"], error["latest_seq"]) == (5, 5)
    _documented(response)
    response, page = await _get(client, conversation=made["id"], since=5)
    assert (response.status, page["events"]) == (200, [])


@pytest.mark.asyncio
async def test_no_text_of_a_turn_reaches_the_stream(client) -> None:
    asked, replied, tagged = "QUESTION-7f3a91c2", "REPLY-b84e06d5", "META-19c7e2aa"
    _response, made = await _create(client, {"metadata": {"note": tagged}})
    client.turn.rows = [("chunk", f"here is {replied}", None)]
    body = {**ASK, "input": f"tell me {asked}", "instructions": f"mind {tagged}"}
    _response, turn = await _ask(client, {**body, "conversation": made["id"]})
    assert replied in json.dumps(turn)
    # The history holds both texts; the stream's files hold neither.
    history = await client.get(ITEMS.format(made["id"]), headers=AS_OWNER)
    assert asked in await history.text() and replied in await history.text()
    directory = _conversations(client).events.directory
    files = [path for path in directory.rglob("*") if path.is_file()]
    assert [path.name for path in files] == [made["id"] + ".jsonl"]
    for path in files:
        kept = path.read_text(encoding="utf-8")
        for text in (asked, replied, tagged, "tell me", "here is"):
            assert text not in kept
    _response, page = await _get(client, conversation=made["id"], since=0)
    for text in (asked, replied, tagged):
        assert text not in json.dumps(page)


# ── the route: the cursor ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_cursor_pages_through_the_stream(client) -> None:
    _response, made = await _create(client)
    await _ask(client, {**ASK, "conversation": made["id"]})
    await _ask(client, {**ASK, "conversation": made["id"]})
    seen: list[int] = []
    cursor = 0
    while True:
        _response, page = await _get(client, conversation=made["id"], since=cursor, limit=3)
        _valid("EventPage", page)
        seen += _seqs(page)
        if not page["has_more"]:
            break
        cursor = seen[-1]
    assert seen == list(range(1, 9))
    response, page = await _get(client, conversation=made["id"], since=0, limit=0)
    assert (page["events"], page["latest_seq"]) == ([], 8)
    # No cursor: only what comes after this moment, which is nothing yet.
    response, page = await _get(client, conversation=made["id"])
    assert (response.status, page["events"], page["latest_seq"]) == (200, [], 8)


@pytest.mark.asyncio
async def test_a_cursor_past_the_newest_event_is_told_to_resync(client) -> None:
    _response, made = await _create(client)
    await _ask(client, {**ASK, "conversation": made["id"]})
    response, body = await _get(client, conversation=made["id"], since=5)
    error = _refusal(response, body, 410, "resync_required")
    assert (error["type"], error["oldest_seq"], error["latest_seq"]) == (
        "resync_required_error",
        1,
        4,
    )
    _documented(response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        {"since": "-1"},
        {"since": "x"},
        {"since": "1.5"},
        {"limit": "1001"},
        {"limit": "-1"},
        {"wait": "31"},
        {"wait": "x"},
    ],
)
async def test_a_parameter_outside_what_the_contract_allows_is_refused(client, query) -> None:
    _response, made = await _create(client)
    response, body = await _get(client, conversation=made["id"], **query)
    _refusal(response, body, 400, "invalid_request")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "headers"),
    [
        ({}, {}),
        ({"since": "0"}, {}),
        ({"conversation": UNKNOWN}, {"Accept": "text/event-stream"}),
    ],
)
async def test_the_parts_not_built_yet_are_a_422(client, query, headers) -> None:
    response, body = await _get(client, headers={**AS_OWNER, **headers}, **query)
    error = _refusal(response, body, 422, "capability_unavailable")
    assert error["type"] == "capability_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [UNKNOWN, *NOT_IDS])
async def test_a_conversation_that_does_not_exist_has_no_stream(client, name) -> None:
    response, body = await _get(client, conversation=name, since=0, wait=30)
    _refusal(response, body, 404, "not_found")
    _documented(response)
    assert not _conversations(client).events._waiting


@pytest.mark.asyncio
async def test_a_deleted_conversation_has_no_stream_and_no_file(client) -> None:
    _response, made = await _create(client)
    await _ask(client, {**ASK, "conversation": made["id"]})
    directory = _conversations(client).events.directory
    assert [p.name for p in directory.iterdir()] == [made["id"] + ".jsonl"]
    response = await client.delete(f"/v1/conversations/{made['id']}", headers=AS_OWNER)
    assert response.status == 200
    assert list(directory.iterdir()) == []
    response, body = await _get(client, conversation=made["id"], since=0)
    _refusal(response, body, 404, "not_found")


@pytest.mark.asyncio
async def test_a_stream_that_cannot_be_read_is_unavailable(client, monkeypatch) -> None:
    _response, made = await _create(client)

    def _unreadable(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("no")

    monkeypatch.setattr(_conversations(client).events, "events_after", _unreadable)
    response, body = await _get(client, conversation=made["id"], since=0)
    _refusal(response, body, 503, "instance_unavailable")
    assert not _conversations(client).events._waiting


# ── the route: a request that waits ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_waiting_request_is_answered_at_the_next_event(client) -> None:
    _response, made = await _create(client)
    log = _conversations(client).events
    waiting = asyncio.ensure_future(_get(client, conversation=made["id"], wait=30))

    async def _held() -> bool:
        return log.waits == 1

    await async_wait_until(_held)
    client.turn.release = asyncio.Event()
    running = asyncio.ensure_future(_ask(client, {**ASK, "conversation": made["id"]}))
    try:
        response, page = await await_until(waiting, what="the waiting request's answer")
    finally:
        client.turn.release.set()
        await await_until(running, what="the turn's answer")
    assert response.status == 200
    assert _types(page) == ["turn.started"]
    assert log.waits == 0 and not log._waiting


@pytest.mark.asyncio
async def test_a_request_with_a_cursor_waits_only_when_nothing_is_after_it(client) -> None:
    _response, made = await _create(client)
    await _ask(client, {**ASK, "conversation": made["id"]})
    log = _conversations(client).events
    # Something to answer: no wait, whatever ``wait`` says.
    response, page = await _get(client, conversation=made["id"], since=0, wait=30)
    assert (response.status, _seqs(page)) == (200, [1, 2, 3, 4])
    waiting = asyncio.ensure_future(_get(client, conversation=made["id"], since=4, wait=30))

    async def _held() -> bool:
        return log.waits == 1

    await async_wait_until(_held)
    await _ask(client, {**ASK, "conversation": made["id"]})
    response, page = await await_until(waiting, what="the waiting request's answer")
    assert _seqs(page)[0] == 5


@pytest.mark.asyncio
async def test_a_wait_that_ends_with_no_event_answers_an_empty_page(client, monkeypatch) -> None:
    _response, made = await _create(client)
    waited: list[float] = []
    wait_for = asyncio.wait_for

    async def _no_event(awaitable: Any, timeout: float | None = None) -> Any:
        if timeout != 30:
            # asyncio is the whole process's: any wait but the route's goes on as it was.
            return await wait_for(awaitable, timeout)
        waited.append(timeout)
        # The wait's own time is asyncio's; here it is over at once.
        await wait_for(awaitable, 0)

    monkeypatch.setattr(events.asyncio, "wait_for", _no_event)
    response, page = await _get(client, conversation=made["id"], since=0, wait=30)
    assert waited == [30]
    assert (response.status, page) == (
        200,
        {"events": [], "latest_seq": 0, "oldest_seq": 0, "has_more": False},
    )
    assert _conversations(client).events.waits == 0


@pytest.mark.asyncio
async def test_no_more_requests_wait_than_the_ceiling(client, monkeypatch) -> None:
    assert events.MAX_WAITING == 10
    monkeypatch.setattr(events, "MAX_WAITING", 2)
    _response, made = await _create(client)
    log = _conversations(client).events
    held = [
        asyncio.ensure_future(_get(client, conversation=made["id"], since=0, wait=30))
        for _ in range(2)
    ]

    async def _both() -> bool:
        return log.waits == 2

    await async_wait_until(_both)
    # One more is refused at once; a request that does not wait is still served.
    response, body = await _get(client, conversation=made["id"], since=0, wait=30)
    error = _refusal(response, body, 429, "concurrency_limit")
    assert (error["type"], error["limit"]) == ("rate_limit_error", "event_streams")
    assert response.headers["Retry-After"] == "1"
    _documented(response)
    response, page = await _get(client, conversation=made["id"], since=0)
    assert response.status == 200
    # Closing the gateway ends the waits, and none begins afterwards.
    assert events.close_event_waits in client.app.on_shutdown
    await events.close_event_waits(client.app)
    for request in held:
        response, page = await await_until(request, what="a held request's answer")
        assert (response.status, page["events"]) == (200, [])
    response, page = await _get(client, conversation=made["id"], since=0, wait=30)
    assert (response.status, page["events"]) == (200, [])
    assert log.waits == 0 and not log._waiting


@pytest.mark.asyncio
async def test_a_waiting_request_on_a_conversation_that_is_deleted_is_not_found(client) -> None:
    _response, made = await _create(client)
    log = _conversations(client).events
    waiting = asyncio.ensure_future(_get(client, conversation=made["id"], since=0, wait=30))

    async def _held() -> bool:
        return log.waits == 1

    await async_wait_until(_held)
    response = await client.delete(f"/v1/conversations/{made['id']}", headers=AS_OWNER)
    assert response.status == 200
    response, body = await await_until(waiting, what="the waiting request's answer")
    _refusal(response, body, 404, "not_found")


# ── the route: who is served ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", CALLERS)
async def test_only_the_machine_owner_reads_events_and_nobody_else_waits(
    client, monkeypatch, caller
) -> None:
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    _response, made = await _create(client)
    await _ask(client, {**ASK, "conversation": made["id"]})
    store = _conversations(client)

    def _no_read(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a refused caller must not reach the records or wait")

    monkeypatch.setattr(store, "load", _no_read)
    monkeypatch.setattr(store.events, "events_after", _no_read)
    monkeypatch.setattr(store.events, "listen", _no_read)
    answers = []
    for name in (made["id"], UNKNOWN):
        response, body = await _get(client, headers=caller, conversation=name, since=0, wait=30)
        _refusal(response, body, 403, "owner_only")
        _documented(response)
        body["error"].pop("request_id")
        answers.append(body)
    # A conversation that exists and one that does not read alike to such a caller.
    assert answers[0] == answers[1]
    assert store.events.waits == 0


# ── the contract ─────────────────────────────────────────────────────────────


def test_the_spec_says_what_is_served_and_what_is_not() -> None:
    assert OPERATION["x-crew-status"] == "available"
    assert OPERATION["x-crew-available-to"] == "owner"
    assert OPERATION["x-crew-stability"] == "preview"
    assert OPERATION["x-crew-not-served-yet"] == {
        "request-headers": ["Last-Event-ID"],
        "response-headers": QUOTA,
    }
    # Every event type the route writes is one the contract names.
    named = OPERATION["responses"]["200"]["content"]["text/event-stream"]["x-crew-sse-events"]
    assert set(event_records.TYPES) <= set(named)
    for status in ("400", "410", "422", "429"):
        assert status in OPERATION["responses"]


# ── what survives a process that dies, and a power loss ──────────────────────


class _Died(BaseException):
    """The process ends here."""


def _names(log: EventLog) -> list[str]:
    return sorted(p.name for p in log.directory.iterdir())


def _watch_the_disk(monkeypatch) -> list[str]:
    """Every file written and every directory synced by the event records, in order."""
    seen: list[str] = []
    write, sync = event_records.atomic_write, event_records.fsync_dir

    def _atomic_write(path: Path, text: str, **kwargs: Any) -> None:
        assert kwargs["fsync"] is True
        seen.append("write " + path.suffix)
        write(path, text, **kwargs)

    def _fsync_dir(path: Path) -> None:
        seen.append("sync " + path.name)
        sync(path)

    monkeypatch.setattr(event_records, "atomic_write", _atomic_write)
    monkeypatch.setattr(event_records, "fsync_dir", _fsync_dir)
    return seen


def test_a_turn_costs_one_open_marker_and_each_write_is_synced_to_its_directories(
    log, monkeypatch
) -> None:
    seen = _watch_the_disk(monkeypatch)
    synced = ["sync events", "sync " + log.directory.parent.name]
    log.append(CONV, event_records.TURN_STARTED, RESP)
    # The marker is on disk, directory and all, before the turn's first event.
    assert seen == ["write .open", *synced, "write .jsonl", *synced]
    assert _names(log) == [CONV + ".jsonl", CONV + ".open"]
    del seen[:]
    log.append(CONV, event_records.MESSAGE_CREATED, RESP, item_id="msg_1")
    log.append(CONV, event_records.MESSAGE_CREATED, RESP, item_id="msg_2")
    log.append(CONV, event_records.TURN_COMPLETED, RESP, result="completed")
    # No marker for the turn's other events, and none left when it ended.
    assert seen == ["write .jsonl", *synced] * 3
    assert _names(log) == [CONV + ".jsonl"]
    assert _seqs(EventLog(directory=log.directory).events_after(CONV, 0, 100)) == [1, 2, 3, 4]


def test_a_process_that_dies_with_a_turn_open_leaves_the_stream_broken(log) -> None:
    log.append(CONV, event_records.TURN_STARTED, RESP)
    # The process that has the turn open reads its own stream as whole.
    assert log.events_after(CONV, 1, 100)["events"] == []
    restarted = EventLog(directory=log.directory)
    assert _resync(restarted, 1) == (2, 2)
    assert _lines(log)[-1] == {"gap": 2}
    assert _names(log) == [CONV + ".jsonl"]
    assert restarted.events_after(CONV, 2, 100)["events"] == []


@pytest.mark.parametrize("read_first", [True, False], ids=["read", "write"])
def test_a_process_that_dies_after_a_failed_write_and_before_marking_it_is_not_forgotten(
    log, monkeypatch, read_first
) -> None:
    log.append(CONV, event_records.TURN_STARTED, RESP)

    def _dies(_conversation_id: str) -> None:
        raise _Died()

    with monkeypatch.context() as patched:
        _fail_writes_of(patched, log, (".jsonl",))
        patched.setattr(log, "_break", _dies)
        with pytest.raises(_Died):
            log.append(CONV, event_records.MESSAGE_CREATED, RESP, item_id="msg_1")
    # Nothing recorded the loss after it happened; the open marker was there before.
    assert _names(log) == [CONV + ".jsonl", CONV + ".open"]
    restarted = EventLog(directory=log.directory)
    if read_first:
        assert _resync(restarted, 1) == (2, 2)
    restarted.append(CONV, event_records.TURN_COMPLETED, RESP, result="failed")
    assert [line.get("gap", line.get("seq")) for line in _lines(log)] == [1, 2, 3]
    assert _resync(EventLog(directory=log.directory), 1) == (3, 3)


def test_an_open_marker_that_cannot_be_written_lets_no_event_be_written(log, monkeypatch) -> None:
    with monkeypatch.context() as patched:
        _fail_writes_of(patched, log, (".open",))
        log.append(CONV, event_records.TURN_STARTED, RESP)  # never raises
        assert _names(log) == [CONV + ".gap"]
    # The next event of the turn asks for the marker again.
    log.append(CONV, event_records.MESSAGE_CREATED, RESP, item_id="msg_1")
    assert _names(log) == [CONV + ".jsonl", CONV + ".open"]
    assert [line.get("gap", line.get("seq")) for line in _lines(log)] == [1, 2]
    assert _resync(log, 0) == (2, 2)


def test_the_open_marker_stays_until_the_last_open_turn_of_the_conversation_ends(log) -> None:
    other = "resp_" + "4" * 32
    log.append(CONV, event_records.TURN_STARTED, RESP)
    log.append(CONV, event_records.TURN_STARTED, other)
    log.append(CONV, event_records.TURN_COMPLETED, RESP, result="completed")
    assert (log.directory / (CONV + ".open")).exists()
    log.append(CONV, event_records.TURN_COMPLETED, other, result="completed")
    assert _names(log) == [CONV + ".jsonl"]


def test_a_turn_whose_end_was_not_written_leaves_its_open_marker_for_the_mending(
    log, monkeypatch
) -> None:
    log.append(CONV, event_records.TURN_STARTED, RESP)
    _lose_an_event(log, monkeypatch)
    assert _names(log) == [CONV + ".gap", CONV + ".jsonl", CONV + ".open"]
    assert _resync(log, 1) == (2, 2)
    assert _names(log) == [CONV + ".jsonl"]


def test_a_directory_that_cannot_be_synced_leaves_the_stream_broken(log, monkeypatch) -> None:
    log.append(CONV, event_records.TURN_STARTED, RESP)

    def _device_error(_path: Path) -> None:
        raise OSError(errno.EIO, "the device did not take the write")

    with monkeypatch.context() as patched:
        patched.setattr(event_records, "fsync_dir", _device_error)
        log.append(CONV, event_records.MESSAGE_CREATED, RESP, item_id="msg_1")  # never raises
        # The second event may not survive a power loss: no cursor is continued
        # from, and the mending is not taken as done while it cannot be synced.
        with pytest.raises(Resync):
            log.events_after(CONV, 1, 100)
    # Synced again: a reader that was given the second event is told of the gap.
    assert _resync(log, 2) == (4, 4)
    assert [line.get("gap", line.get("seq")) for line in _lines(log)] == [1, 2, 3, 4]


def test_removing_a_stream_removes_its_open_marker_too(log) -> None:
    log.append(CONV, event_records.TURN_STARTED, RESP)
    log.remove(CONV)
    assert _names(log) == []
    # A turn still open in the removed conversation asks for the marker again.
    log.append(CONV, event_records.MESSAGE_CREATED, RESP, item_id="msg_1")
    assert _names(log) == [CONV + ".jsonl", CONV + ".open"]


@pytest.mark.asyncio
async def test_a_turn_end_that_the_stopping_gateway_refuses_leaves_the_stream_broken(
    client, monkeypatch, caplog
) -> None:
    from kiro_crew.dashboard import crew_api_responses as responses

    _response, made = await _create(client)

    async def _raises(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("the answer could not be made")

    def _stopping(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("cannot schedule new futures after shutdown")

    with monkeypatch.context() as patched:
        patched.setattr(responses, "_answer", _raises)
        patched.setattr(responses._TurnStream, "ended_with", _stopping)
        response = await client.post(
            "/v1/responses", json={**ASK, "conversation": made["id"]}, headers=AS_OWNER
        )
        assert response.status == 500
    assert "its end was not written to the event stream" in caplog.text
    # The handler still let go of the session: the next turn runs.
    response, turn = await _ask(client, {**ASK, "conversation": made["id"]})
    assert (response.status, turn["status"]) == (200, "completed")
    # The process that takes over reads the first turn's missing end as a gap.
    directory = _conversations(client).events.directory
    assert (directory / (made["id"] + ".open")).exists()
    with pytest.raises(Resync):
        EventLog(directory=directory).events_after(made["id"], 1, 100)
