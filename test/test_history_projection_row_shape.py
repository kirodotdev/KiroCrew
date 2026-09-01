"""A transcript row that is valid JSON but not an object must be skipped.

``history_projection`` reads ``.jsonl`` transcripts line by line, and every
reader already guards the line that will not parse::

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        continue

That guard does not cover a line which parses fine and decodes to something
that is not a mapping -- ``[]``, ``"text"``, ``12``, ``null``. The very next
statement is ``data.get(...)``, so such a row raises ``AttributeError``, which
is not what any of these callers catch: the read is abandoned and every VALID
row after the bad one is lost with it.

The file already treats this as a real hazard. ``read_file_change_messages``
decodes the same rows of the same files and follows its ``except ValueError``
with ``if not isinstance(data, dict)``, and ``_read_metadata_status`` and
``delete_session`` do the same. These tests pin the remaining readers to that
existing in-file contract rather than introducing a new one.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.history import ConversationLog

KEY = "row-shape"

#: Valid JSON, no ``.get``. ``null`` is included because ``json.loads`` turns it
#: into ``None``, which fails in the same place for a different reason.
NON_OBJECT_ROWS = ["[]", '"just a string"', "12", "null", '["a", "b"]']


def _seed(tmp_path: Path, bad_row: str) -> ConversationLog:
    """A transcript with one unusable row BETWEEN two good ones.

    Between, not last: a reader that stops at the bad row still returns ``m1``,
    so only a message written AFTER it can show that the rest of the file was
    abandoned rather than merely truncated.
    """
    log = ConversationLog(base_dir=tmp_path)
    log.append(KEY, "user", "m1")
    path = log._path(KEY)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(bad_row + "\n")
    log.append(KEY, "assistant", "m2")
    return ConversationLog(base_dir=tmp_path)  # fresh: no warm cache


class TestTranscriptReadsSkipNonObjectRows:
    def test_read_messages_keeps_the_rows_after_it(self, tmp_path: Path) -> None:
        for index, row in enumerate(NON_OBJECT_ROWS):
            # One transcript per shape, in its own directory: `enumerate`, not the
            # row text, because several of these are not legal path names.
            log = _seed(tmp_path / f"row{index}", row)
            assert [m["content"] for m in log._read_messages(KEY)] == ["m1", "m2"], row

    def test_recent_keeps_the_rows_after_it(self, tmp_path: Path) -> None:
        # `recent` prefers the tail reader, so this covers a different loop from
        # the one above even though the transcript is the same.
        log = _seed(tmp_path, "[]")
        assert [m["content"] for m in log.recent(KEY, max_messages=10)] == ["m1", "m2"]

    def test_last_message_preview_reads_past_it(self, tmp_path: Path) -> None:
        # The preview scans BACKWARDS from the end, so the bad row sits between
        # the newest message and the reader's starting point.
        log = _seed(tmp_path, "[]")
        assert log.last_message_preview(KEY) == "m2"

    def test_recent_from_source_still_returns_the_session(self, tmp_path: Path) -> None:
        # This loop reads the first five lines looking for an incognito marker.
        # It runs inside `try: ... except OSError`, which an AttributeError walks
        # straight out of, taking the whole cross-session view with it.
        log = _seed(tmp_path, "[]")
        recent = log.recent_from_source(KEY.split("-")[0], max_messages=10)
        assert [m["content"] for m in recent] == ["m1", "m2"]


class TestMetadataWritesRefuseANonObjectFirstLine:
    """Line 0 is the metadata row. When it will not parse, both writers below
    ``return`` and leave the file alone; a non-object line 0 must reach the same
    refusal instead of raising out from under the owner lock."""

    def _seed_bad_metadata(self, tmp_path: Path) -> ConversationLog:
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "user", "m1")
        path = log._path(KEY)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        lines[0] = "[]\n"
        path.write_text("".join(lines), encoding="utf-8")
        return ConversationLog(base_dir=tmp_path)

    def test_update_metadata_is_a_noop(self, tmp_path: Path) -> None:
        log = self._seed_bad_metadata(tmp_path)
        before = log._path(KEY).read_text(encoding="utf-8")
        log.update_metadata(KEY, {"title": "t"})
        assert log._path(KEY).read_text(encoding="utf-8") == before

    def test_clear_closed_is_a_noop(self, tmp_path: Path) -> None:
        log = self._seed_bad_metadata(tmp_path)
        before = log._path(KEY).read_text(encoding="utf-8")
        log.clear_closed(KEY)
        assert log._path(KEY).read_text(encoding="utf-8") == before


class TestOrdinaryTranscriptsAreUnaffected:
    """Positive control. Every assertion above is satisfied by a reader that
    returns nothing at all, so pin that the same calls still work normally."""

    def test_a_clean_transcript_reads_normally(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "user", "m1")
        log.append(KEY, "assistant", "m2")
        fresh = ConversationLog(base_dir=tmp_path)

        assert [m["content"] for m in fresh._read_messages(KEY)] == ["m1", "m2"]
        assert [m["content"] for m in fresh.recent(KEY, max_messages=10)] == ["m1", "m2"]
        assert fresh.last_message_preview(KEY) == "m2"

        fresh.update_metadata(KEY, {"title": "t"})
        assert fresh.get_metadata(KEY)["title"] == "t"

    def test_an_unparseable_row_is_still_skipped(self, tmp_path: Path) -> None:
        # The behaviour that already worked, kept under test beside the new one.
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "user", "m1")
        with log._path(KEY).open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        log.append(KEY, "assistant", "m2")
        fresh = ConversationLog(base_dir=tmp_path)
        assert [m["content"] for m in fresh._read_messages(KEY)] == ["m1", "m2"]


def test_the_transcript_on_disk_really_holds_the_bad_row(tmp_path: Path) -> None:
    """Guards the fixture itself: if `append` ever rewrote the file, the tests
    above would pass without the row they are about ever existing."""
    log = _seed(tmp_path, "[]")
    rows = [json.loads(line) for line in log._path(KEY).read_text(encoding="utf-8").splitlines()]
    assert [] in rows
