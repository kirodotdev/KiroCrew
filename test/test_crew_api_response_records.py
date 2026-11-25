"""The record file of a ``POST /v1/responses`` turn: what is kept, for how long, and how
a file that cannot be believed is treated. The clock is the test's own.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard import crew_api_response_records as records
from kiro_crew.dashboard.crew_api_response_records import ResponseStore

START = 1_800_000_000.0
ID = "resp_" + "1" * 32
OTHER = "resp_" + "2" * 32
OUT = "msg_" + "a" * 32
IN = "msg_" + "b" * 32


class _Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


def _response(response_id: str = ID, **changed: Any) -> dict[str, Any]:
    part = {"type": "output_text", "text": "Done.", "annotations": []}
    response = {
        "id": response_id,
        "object": "response",
        "created_at": int(START),
        "status": "completed",
        "model": "writer",
        "output": [
            {
                "type": "message",
                "id": OUT,
                "role": "assistant",
                "status": "completed",
                "content": [part],
            }
        ],
        "error": None,
        "incomplete_details": None,
        "metadata": {},
        "usage": None,
        "crew": {"ignored_parameters": []},
    }
    return {**response, **changed}


def _input(**changed: Any) -> dict[str, Any]:
    item = {
        "type": "message",
        "id": IN,
        "role": "user",
        "status": "completed",
        "content": [{"type": "input_text", "text": "hello"}],
    }
    return {**item, **changed}


def _stream(**changed: Any) -> dict[str, Any]:
    """What is kept of the stream that carried ``Done.`` in two deltas."""
    return {"deltas": ["Do", "ne."], "item_id": OUT, "error": False, **changed}


def _failed(**changed: Any) -> dict[str, Any]:
    """The ``Response`` kept for a stream that closed on an ``error`` event."""
    error = {"code": "server_error", "message": "the turn could not be run"}
    return _response(**{"status": "failed", "output": [], "error": error, **changed})


def _record(response_id: str = ID, **changed: Any) -> dict[str, Any]:
    record = {
        "id": response_id,
        "created_at": int(START),
        "response": _response(response_id),
        "input": [_input()],
        "stream": _stream(),
        "created_by_grant": None,
        "namespace": records.NAMESPACE,
        "slot": "resp-" + "1" * 32,
    }
    return {**record, **changed}


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def store(tmp_path: Path, clock: _Clock) -> ResponseStore:
    return ResponseStore(directory=tmp_path / "kept", clock=clock)


def _path(store: ResponseStore, response_id: str = ID) -> Path:
    assert store.directory is not None
    return store.directory / f"{response_id}.json"


def test_a_record_reads_back_as_it_was_written(store: ResponseStore) -> None:
    store.save(_record())
    assert store.load(ID) == _record()
    assert store.load(OTHER) is None


def test_save_syncs_the_published_record_directory_and_its_parent_before_sweeping(
    store: ResponseStore, monkeypatch
) -> None:
    events: list[tuple[str, Path]] = []
    write = records.atomic_write

    def publish(path: Path, serialized: str, **kwargs: Any) -> None:
        assert kwargs == {"fsync": True, "restrict_to_owner": True}
        write(path, serialized, **kwargs)
        events.append(("published", path))

    def sync(directory: Path) -> None:
        # The name is published and its retention time set before syncing.
        assert store.load(ID) == _record()
        assert _path(store).stat().st_mtime == START
        events.append(("synced", directory))

    def sweep() -> int:
        events.append(("swept", _path(store).parent))
        return 0

    monkeypatch.setattr(records, "atomic_write", publish)
    monkeypatch.setattr(records, "fsync_dir", sync)
    monkeypatch.setattr(store, "sweep", sweep)
    path = _path(store)
    # Also sync the parent when the directory already exists: a prior save may
    # have failed after making it but before syncing that parent.
    for _ in range(2):
        events.clear()
        store.save(_record())
        assert events == [
            ("published", path),
            ("synced", path.parent),
            ("synced", path.parent.parent),
            ("swept", path.parent),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [False, True], ids=["record directory", "its parent"])
async def test_a_directory_sync_failure_keeps_a_readable_record_and_does_not_escape_the_turn(
    store: ResponseStore, monkeypatch, caplog, parent: bool
) -> None:
    from kiro_crew.atomic_write import on_event_loop
    from kiro_crew.dashboard.crew_api_responses import _record as keep

    path = _path(store)
    failed_directory = path.parent.parent if parent else path.parent
    synced: list[Path] = []

    def fail(directory: Path) -> None:
        synced.append(directory)
        if directory == failed_directory:
            raise OSError("directory sync failed")

    monkeypatch.setattr(records, "fsync_dir", fail)
    with pytest.raises(OSError, match="directory sync failed"):
        store.save(_record())
    assert store.load(ID) == _record()
    assert store._swept_at is None

    def fail_off_loop(directory: Path) -> None:
        assert not on_event_loop()
        fail(directory)

    monkeypatch.setattr(records, "fsync_dir", fail_off_loop)
    response = _response()
    await keep(store, _record()["slot"], response, [_input()], _stream())
    expected = [path.parent, path.parent.parent] if parent else [path.parent]
    assert synced == expected * 2
    assert response == _response()
    assert store.load(ID) == _record()
    assert store._swept_at is None
    assert f"Crew API response {ID} was not recorded: OSError" in caplog.text


@pytest.mark.parametrize(
    "record",
    [
        _record(stream=None),
        _record(response=_failed(), stream=_stream(error=True)),
        _record(response=_failed(), stream=_stream(error=True, deltas=[])),
        _record(response=_response(output=[]), stream=_stream(deltas=[])),
        _record(response=_response(status="cancelled")),
        _record(
            response=_response(
                status="incomplete", incomplete_details={"reason": "turn_time_limit"}
            )
        ),
        _record(response=_response(metadata={"k": "v"}, crew={"ignored_parameters": ["user"]})),
        _record(
            input=[
                _input(role=role, id="msg_" + digit * 32)
                for role, digit in (("developer", "1"), ("system", "2"), ("assistant", "3"))
            ]
        ),
        _record(input=[]),
    ],
    ids=[
        "answered as JSON",
        "a stream closed on an error event",
        "a stream closed on an error event before any text",
        "a stream with no reply text",
        "cancelled",
        "cut at the time limit",
        "metadata and ignored fields",
        "every input role",
        "no input item",
    ],
)
def test_every_shape_the_writer_gives_reads_back(store: ResponseStore, record: Any) -> None:
    store.save(record)
    assert store.load(ID) == record


def test_a_field_the_record_does_not_name_never_reaches_the_file(store: ResponseStore) -> None:
    store.save({**_record(), "authorization": "Bearer x"})
    assert set(json.loads(_path(store).read_text(encoding="utf-8"))) == set(records.FIELDS)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_the_file_and_its_directory_are_the_owners_alone(store: ResponseStore) -> None:
    store.save(_record())
    assert stat.S_IMODE(_path(store).stat().st_mode) == 0o600
    assert stat.S_IMODE(_path(store).parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "response_id", ["", "resp_1", "../" + ID, ID + "/x", "resp_" + "A" * 32, ID + ".json"]
)
def test_an_id_of_another_shape_never_reaches_the_disk(tmp_path: Path, response_id: str) -> None:
    store = ResponseStore(directory=tmp_path / "never-made", clock=_Clock())
    assert store.load(response_id) is None
    with pytest.raises(ValueError):
        store.save(_record(response_id))
    assert not (tmp_path / "never-made").exists()


def test_a_record_is_gone_once_its_retention_is_over(store: ResponseStore, clock: _Clock) -> None:
    store.save(_record())
    clock.now = START + records.RETENTION_SECS - 1
    assert store.load(ID) is not None
    clock.now = START + records.RETENTION_SECS
    assert store.load(ID) is None
    assert not _path(store).exists()


def test_a_sweep_removes_only_the_records_whose_time_is_up(
    store: ResponseStore, clock: _Clock
) -> None:
    store.save(_record())
    store.save(_record(OTHER))
    bystander = _path(store).with_name("notes.txt")
    bystander.write_text("not a record", encoding="utf-8")
    old = START - records.RETENTION_SECS
    os.utime(_path(store), (old, old))
    os.utime(bystander, (old, old))
    os.utime(_path(store, OTHER), (START, START))
    # The first save swept; the next sweep waits its hour.
    assert store.sweep() == 0
    assert _path(store).exists()
    clock.now = START + records.SWEEP_EVERY_SECS
    assert store.sweep() == 1
    assert not _path(store).exists()
    assert _path(store, OTHER).exists() and bystander.exists()


def test_writing_a_record_sweeps_the_old_ones(store: ResponseStore, clock: _Clock) -> None:
    store.save(_record())
    old = START - records.RETENTION_SECS
    os.utime(_path(store), (old, old))
    clock.now = START + records.SWEEP_EVERY_SECS
    store.save(_record(OTHER))
    assert not _path(store).exists()
    assert _path(store, OTHER).exists()


@pytest.mark.parametrize(
    "damaged",
    [
        "{",
        "[]",
        json.dumps({"id": ID}),
        json.dumps(_record(OTHER)),
        json.dumps(_record(created_at="soon")),
        json.dumps(_record(response={"id": OTHER})),
        # A shape the writer never gives: each would put a text where no read looks.
        json.dumps(_record(input=[{}])),
        json.dumps(_record(input=[_input(content="a text outside a part")])),
        json.dumps(_record(input=[_input(content=[{"type": "refusal", "text": "x"}])])),
        json.dumps(_record(input=[_input(note="x")])),
        json.dumps(_record(input=[_input(role="tool")])),
        # What is kept of a stream, when it is not what the writer gives.
        json.dumps(_record(stream="x")),
        json.dumps(_record(stream={})),
        json.dumps(_record(stream=_stream(note="x"))),
        json.dumps(_record(stream=_stream(deltas="Done."))),
        json.dumps(_record(stream=_stream(deltas=["Done.", 3]))),
        json.dumps(_record(stream=_stream(deltas=["Done.", ""]))),
        json.dumps(_record(stream=_stream(error="no"))),
        json.dumps(_record(stream=_stream(item_id="a text"))),
        # A stream that does not agree with the Response it ended on.
        json.dumps(_record(stream=_stream(deltas=["Do"]))),
        json.dumps(_record(stream=_stream(deltas=["Done.", "and more"]))),
        json.dumps(_record(stream=_stream(item_id=IN))),
        json.dumps(_record(stream=_stream(error=True))),
        json.dumps(_record(response=_failed(status="completed"), stream=_stream(error=True))),
        json.dumps(_record(response=_failed(error=None), stream=_stream(error=True))),
        json.dumps(_record(response={k: v for k, v in _response().items() if k != "output"})),
        json.dumps(_record(response=_response(output=[{"type": "function_call"}]))),
        json.dumps(_record(response=_response(error={"code": "x", "message": 3}))),
        json.dumps(_record(response=_response(note="x"))),
        json.dumps(_record(response=_response(usage={"total": "x"}))),
        # A fixed field holding anything but its fixed value.
        json.dumps(_record(response=_response(error={"code": "a text", "message": "m"}))),
        json.dumps(_record(response=_response(incomplete_details={"reason": "a text"}))),
        json.dumps(_record(response=_response(status="in_progress"))),
        json.dumps(_record(input=[_input(id="msg_a text")])),
        # Two input items under one id: a page could not be told from the next.
        json.dumps(_record(input=[_input(), _input()])),
        # An output item with an empty text, or with no text part, which no kept turn has.
        json.dumps(_record(stream=None)).replace('"text": "Done."', '"text": ""'),
        json.dumps(
            _record(response=_response(output=[{**_response()["output"][0], "content": []}]))
        ),
        pytest.param("[" * 100_000, id="nested deeper than the parser goes"),
    ],
)
def test_a_file_that_cannot_be_believed_reads_absent_and_is_kept_aside(
    store: ResponseStore, damaged: str
) -> None:
    store.save(_record())
    _path(store).write_text(damaged, encoding="utf-8")
    assert store.load(ID) is None
    assert not _path(store).exists()
    (aside,) = [p for p in _path(store).parent.iterdir() if ".damaged-" in p.name]
    assert aside.read_text(encoding="utf-8") == damaged
    # A second damaged file at the same second keeps the first one too.
    _path(store).write_text(damaged, encoding="utf-8")
    assert store.load(ID) is None
    assert len([p for p in _path(store).parent.iterdir() if ".damaged-" in p.name]) == 2


def _replied(text: str, *, streamed: bool) -> dict[str, Any]:
    """The record of a turn that replied *text*, answered as a stream or as JSON."""
    reply = _response()
    reply["output"][0]["content"][0]["text"] = text
    half = len(text) // 2
    stream = _stream(deltas=[text[:half], text[half:]]) if streamed else None
    return _record(response=reply, stream=stream)


HOW_ANSWERED = pytest.mark.parametrize(
    "streamed", [False, True], ids=["answered as JSON", "answered as a stream"]
)


@HOW_ANSWERED
def test_a_reply_of_exactly_the_cap_is_kept(store: ResponseStore, streamed: bool) -> None:
    record = _replied("x" * records.MAX_REPLY_BYTES, streamed=streamed)
    store.save(record)
    assert store.load(ID) == record


@HOW_ANSWERED
def test_a_reply_one_byte_over_the_cap_is_not_kept(store: ResponseStore, streamed: bool) -> None:
    with pytest.raises(ValueError):
        store.save(_replied("x" * (records.MAX_REPLY_BYTES + 1), streamed=streamed))
    assert not _path(store).exists()


@HOW_ANSWERED
def test_the_cap_counts_the_replys_own_bytes_not_the_files(
    store: ResponseStore, streamed: bool
) -> None:
    # Three bytes each as text; the file spells each in six. The text is what counts.
    fits = "\u4e2d" * (records.MAX_REPLY_BYTES // 3)
    record = _replied(fits, streamed=streamed)
    store.save(record)
    assert _path(store).stat().st_size > records.MAX_REPLY_BYTES
    assert store.load(ID) == record
    with pytest.raises(ValueError):
        store.save(_replied(fits + "\u4e2d", streamed=streamed))


def test_the_text_of_a_stream_closed_on_an_error_event_counts_too(store: ResponseStore) -> None:
    carried = ["x" * records.MAX_REPLY_BYTES]
    record = _record(response=_failed(), stream=_stream(error=True, deltas=carried))
    store.save(record)
    assert store.load(ID) == record
    with pytest.raises(ValueError):
        store.save(_record(response=_failed(), stream=_stream(error=True, deltas=[*carried, "x"])))


def _sent(text: str, ignored: list[str]) -> dict[str, Any]:
    """The record of a turn whose one input item is *text* and which ignored *ignored*."""
    part = {"type": "input_text", "text": text}
    return _record(
        input=[_input(content=[part])],
        response=_response(crew={"ignored_parameters": ignored}),
    )


@pytest.mark.parametrize(
    ("texts", "ignored", "size"),
    [
        (["hello"], [], 5),
        (["hello", "you"], ["user"], 12),
        ([], ["user", "top_p"], 9),
        (["\u4e2d"], [], 3),
        (["\ud800"], [], 3),
    ],
    ids=["the input", "inputs and an ignored name", "ignored names", "UTF-8", "half a pair"],
)
def test_a_request_is_measured_as_it_was_sent(texts: Any, ignored: Any, size: int) -> None:
    assert records.request_bytes(texts, ignored) == size


def test_an_input_that_redaction_made_longer_than_the_request_cap_is_kept(
    store: ResponseStore,
) -> None:
    # The request line is the route's, on the text as sent; save does not count again.
    record = _sent("x" * (records.MAX_REQUEST_BYTES + 1), ["user"])
    store.save(record)
    assert store.load(ID) == record


def test_the_largest_record_the_caps_allow_reads_back(store: ResponseStore) -> None:
    # CJK text: six bytes in the file for each three of the text, twice its counted size.
    reply = _response()
    reply["output"][0]["content"][0]["text"] = "\u4e2d" * (records.MAX_REPLY_BYTES // 3)
    whole = reply["output"][0]["content"][0]["text"]
    record = _record(
        response=reply,
        stream=_stream(deltas=[whole]),
        input=[
            _input(
                content=[
                    {"type": "input_text", "text": "\u4e2d" * (records.MAX_REQUEST_BYTES // 3)}
                ]
            )
        ],
    )
    store.save(record)
    assert records.MAX_REPLY_BYTES * 4 < _path(store).stat().st_size < records.MAX_FILE_BYTES
    assert store.load(ID) == record


def test_no_file_is_written_that_could_not_be_read_back(store: ResponseStore, monkeypatch) -> None:
    monkeypatch.setattr(records, "MAX_FILE_BYTES", len(json.dumps(_record())))
    with pytest.raises(ValueError):
        store.save(_record())
    assert not _path(store).exists()
    monkeypatch.setattr(records, "MAX_FILE_BYTES", len(json.dumps(_record())) + 1)
    store.save(_record())
    assert store.load(ID) == _record()


def test_a_file_that_cannot_be_read_stays_where_it_is(store: ResponseStore, monkeypatch) -> None:
    store.save(_record())
    kept = _path(store).read_bytes()

    def _held(*_args: Any, **_kwargs: Any) -> bytes:
        raise PermissionError("another process holds the file")

    with monkeypatch.context() as patched:
        patched.setattr(records, "read_bytes_with_retry", _held)
        with pytest.raises(PermissionError):
            store.load(ID)
    assert [p.name for p in _path(store).parent.iterdir()] == [_path(store).name]
    assert _path(store).read_bytes() == kept
    assert store.load(ID) == _record()


def test_a_copy_moved_aside_goes_when_its_retention_is_over(
    store: ResponseStore, clock: _Clock
) -> None:
    store.save(_record())
    _path(store).write_text("{", encoding="utf-8")
    old = START - records.RETENTION_SECS
    os.utime(_path(store), (old, old))
    assert store.load(ID) is None
    (aside,) = list(_path(store).parent.iterdir())
    clock.now = START + records.SWEEP_EVERY_SECS
    assert store.sweep() == 1
    assert not aside.exists()


def test_a_file_larger_than_any_record_is_not_believed(store: ResponseStore) -> None:
    store.save(_record())
    _path(store).write_text(" " * records.MAX_FILE_BYTES + json.dumps(_record()), encoding="utf-8")
    assert store.load(ID) is None
    assert not _path(store).exists()


def test_a_damaged_record_moves_through_the_approval_records_set_aside(
    store: ResponseStore, clock: _Clock, monkeypatch
) -> None:
    from kiro_crew.dashboard import crew_api_approval_records

    synced: list[Path] = []
    monkeypatch.setattr(crew_api_approval_records, "fsync_dir", synced.append)
    store.save(_record())
    _path(store).write_text("{", encoding="utf-8")
    old = START - records.RETENTION_SECS
    os.utime(_path(store), (old, old))
    assert store.load(ID) is None
    # The shared helper's name and its directory sync: the move survives a crash.
    (aside,) = list(_path(store).parent.iterdir())
    assert aside.name == f"{ID}.json.damaged-20270115T080000Z"
    assert synced == [_path(store).parent]
    # And the sweep still finds the copy by that name.
    clock.now = START + records.SWEEP_EVERY_SECS
    assert store.sweep() == 1
    assert not aside.exists()
