"""A metadata update copies the rows in chunks instead of reading the transcript.

The update changes line 0 only. Reading the whole file, splitting it and joining
it again held several copies of a long transcript at once, on every lease claim
and execution binding. The chunked copy must write the same bytes, keep the
stored message count, and hand every file it cannot copy exactly (a ``\\r`` or a
damaged row) to the whole-file path, which behaves as before.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import history_projection
from kiro_crew.history import ConversationLog
from kiro_crew.transcript_count import count_and_store, stored_count

KEY = "dashboard:streamed-metadata"


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    return ConversationLog(base_dir=tmp_path / "sessions")


def _transcript(log: ConversationLog, rows: list[bytes]):
    path = log._path(KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = json.dumps({"_type": "metadata", "created_at": "2026-11-01T00:00:00+00:00"})
    path.write_bytes(meta.encode() + b"\n" + b"".join(rows))
    return path


def _whole_file_reads(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = history_projection._split_transcript_bytes

    def spy(data):
        calls.append(len(data))
        return real(data)

    monkeypatch.setattr(history_projection, "_split_transcript_bytes", spy)
    return calls


def test_rows_are_copied_byte_for_byte_without_a_whole_file_read(log, monkeypatch):
    # Rows larger than one copy chunk, with escaped and raw non-ASCII text.
    big = json.dumps({"role": "user", "content": "x" * (3 << 20)}).encode() + b"\n"
    raw = '{"role": "assistant", "content": "内存 ok"}\n'.encode()
    path = _transcript(log, [big, raw, big])
    rows_before = path.read_bytes().split(b"\n", 1)[1]
    reads = _whole_file_reads(monkeypatch)

    assert log.update_metadata_if(KEY, {"title": "t1"}, lambda m: True)

    data = path.read_bytes()
    head, rows_after = data.split(b"\n", 1)
    assert rows_after == rows_before
    assert json.loads(head)["title"] == "t1"
    assert json.loads(head)["created_at"] == "2026-11-01T00:00:00+00:00"
    assert reads == []


def test_stored_message_count_survives_the_update(log):
    row = b'{"role": "user", "content": "hi"}\n'
    path = _transcript(log, [row] * 5)
    assert count_and_store(path) == 5

    assert log.update_metadata_if(KEY, {"title": "t2"}, lambda m: True)

    assert stored_count(path) == 5


def test_a_carriage_return_takes_the_whole_file_path(log, monkeypatch):
    path = _transcript(log, [b'{"role": "user", "content": "a"}\r\n', b'{"role": "user"}\n'])
    reads = _whole_file_reads(monkeypatch)

    assert log.update_metadata_if(KEY, {"title": "t3"}, lambda m: True)

    assert reads  # the whole-file path ran, and folded the line ending as before
    assert b"\r" not in path.read_bytes()
    assert not list(path.parent.glob("*.tmp"))


def test_a_row_that_is_not_utf8_still_refuses_the_write(log):
    path = _transcript(log, [b'{"role": "user"}\n', b"\xff\xfe broken\n"])
    before = path.read_bytes()

    with pytest.raises(UnicodeDecodeError):
        log.update_metadata_if(KEY, {"title": "t4"}, lambda m: True)

    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


def test_no_handle_on_the_transcript_is_open_when_it_is_renamed(log, monkeypatch):
    # Windows os.replace cannot swap a file another handle holds, so every
    # handle the streaming copy opened on the transcript must be closed by the
    # time atomic_write publishes, which it does right after on_staged returns.
    from kiro_crew import history_rewrite

    path = _transcript(log, [b'{"role": "user", "content": "hi"}\n'] * 3)
    opened: list = []

    def tracking_open(file, *args, **kwargs):
        handle = open(file, *args, **kwargs)  # noqa: SIM115 - the caller closes it
        opened.append((file, handle))
        return handle

    real_atomic_write = history_rewrite.atomic_write
    published: list[bool] = []

    def checking_atomic_write(target, content, **kwargs):
        on_staged = kwargs["on_staged"]

        def staged_then_check(fd):
            on_staged(fd)
            alive = [f for f, h in opened if str(f) == str(target) and not h.closed]
            assert alive == []
            published.append(True)

        kwargs["on_staged"] = staged_then_check
        return real_atomic_write(target, content, **kwargs)

    monkeypatch.setattr(history_rewrite, "open", tracking_open, raising=False)
    monkeypatch.setattr(history_rewrite, "atomic_write", checking_atomic_write)

    assert log.update_metadata_if(KEY, {"title": "t5"}, lambda m: True)

    assert published == [True]
    assert opened  # the streaming copy ran and its opens were seen
    assert json.loads(path.read_bytes().split(b"\n", 1)[0])["title"] == "t5"
