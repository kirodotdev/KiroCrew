"""The tail-preview reader must not surface a stop_event card's JSON payload.

A Stop press appends the stop card as a ``system`` row whose ``content`` IS the
JSON stop payload (``slot.append("system", stop_msg, stop_msg)`` — see the
``is_stop_event_row`` docstring in ``dashboard/state.py``). The
``TranscriptReadProjection.last_message_info`` tail walk rejected only
``_type == "metadata"`` rows and empty text, so a transcript ending on a stop
card handed the raw ``{"kind": "stop_event", …}`` dict to every preview caller:
the Crew Members roster subtitle (``/api/members`` ``last_message``) and the
session list preview (``last_message_preview``) both rendered computer text
where a sentence belongs.

The skip reuses ``is_stop_event_row`` — whose docstring documents why a fresh
``kind == "stop_event"`` check would match a restored row but never a freshly
stopped one — rather than adding a second discriminator.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew.history import ConversationLog

KEY = "preview-stop"


def _stop_payload(state: str = "stopped") -> str:
    return json.dumps(
        {
            "kind": "stop_event",
            "id": "stop-abc",
            "state": state,
            "outcome": "soft" if state == "stopped" else None,
            "ts_start": "2026-09-09T00:00:00+00:00",
        }
    )


def _log_with_trailing_stop(tmp_path: Path) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path)
    log.append(KEY, "user", "hello world")
    payload = _stop_payload()
    # The durable stop row: content mirrors cls, both carry the JSON payload.
    log.append(KEY, "system", payload, cls=payload)
    return ConversationLog(base_dir=tmp_path)  # fresh: no warm cache


class TestPreviewSkipsStopEventRows:
    def test_last_message_info_skips_a_trailing_stop_card(self, tmp_path: Path) -> None:
        log = _log_with_trailing_stop(tmp_path)
        preview, _ = log.last_message_info(KEY)
        assert preview == "hello world"

    def test_epoch_still_reads_the_newest_row(self, tmp_path: Path) -> None:
        """The skip moves the preview TEXT only, not the recency timestamp.

        ``members.py`` orders roster rows by this epoch ("Order by the newest
        MESSAGE"); returning the preceding row's timestamp would sink a thread
        whose newest event is a stop below threads with genuinely older
        activity. A stop IS activity — the epoch reads the stop row, the text
        reads the newest conversational row.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "user", "hello world")
        path = log._path(KEY)
        old_row = {"role": "user", "content": "hello world", "ts": "2026-09-09T00:00:00+00:00"}
        stop_row = {
            "role": "system",
            "content": _stop_payload(),
            "cls": _stop_payload(),
            "ts": "2026-09-09T05:00:00+00:00",
        }
        path.write_text(json.dumps(old_row) + "\n" + json.dumps(stop_row) + "\n", encoding="utf-8")
        fresh = ConversationLog(base_dir=tmp_path)
        preview, epoch = fresh.last_message_info(KEY)
        assert preview == "hello world"
        # 05:00, the stop row's ts — not 00:00, the previewed row's.
        assert epoch == datetime(2026, 9, 9, 5, 0, tzinfo=timezone.utc).timestamp()

    def test_other_skipped_rows_keep_the_previewed_rows_epoch(self, tmp_path: Path) -> None:
        """The stop-row epoch carry-over is scoped to STOP rows.

        Every other non-previewable row (here a zero-width-space-only quiet
        reply, newer than both) keeps the long-standing contract that the
        timestamp travels with the row the preview came from
        (test_preview_text.py pins the stop-free case) — a quiet cycle is not
        displayable activity and must not reorder the roster.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "user", "seed")
        path = log._path(KEY)
        rows = [
            {"role": "user", "content": "the real answer", "ts": "2026-09-09T00:00:00+00:00"},
            {
                "role": "system",
                "content": _stop_payload(),
                "cls": _stop_payload(),
                "ts": "2026-09-09T05:00:00+00:00",
            },
            {"role": "assistant", "content": "\u200b", "ts": "2026-09-09T09:00:00+00:00"},
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        fresh = ConversationLog(base_dir=tmp_path)
        preview, epoch = fresh.last_message_info(KEY)
        assert preview == "the real answer"
        # The stop row's 05:00 carries (activity); the quiet row's 09:00 does not.
        assert epoch == datetime(2026, 9, 9, 5, 0, tzinfo=timezone.utc).timestamp()

    def test_last_message_preview_rides_the_same_skip(self, tmp_path: Path) -> None:
        """The sibling reader (session rows) delegates to the same walk."""
        log = _log_with_trailing_stop(tmp_path)
        assert log.last_message_preview(KEY) == "hello world"

    def test_meta_kind_carrier_is_also_skipped(self, tmp_path: Path) -> None:
        """A restored row carries the discriminator in ``meta.kind``.

        ``is_stop_event_row`` matches all three carriers; the preview walk must
        inherit that, not re-derive one carrier.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "user", "hello world")
        path = log._path(KEY)
        row = {
            "role": "system",
            "content": _stop_payload(),
            "meta": {"kind": "stop_event"},
            "ts": "2026-09-09T00:00:01+00:00",
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        fresh = ConversationLog(base_dir=tmp_path)
        preview, _ = fresh.last_message_info(KEY)
        assert preview == "hello world"

    def test_stopping_state_row_is_skipped_too(self, tmp_path: Path) -> None:
        """An unresolved (still "stopping") card is no more conversational."""
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "user", "hello world")
        payload = _stop_payload(state="stopping")
        log.append(KEY, "system", payload, cls=payload)
        fresh = ConversationLog(base_dir=tmp_path)
        preview, _ = fresh.last_message_info(KEY)
        assert preview == "hello world"

    def test_ordinary_system_row_still_previews(self, tmp_path: Path) -> None:
        """Only stop cards are skipped — not the whole ``system`` role."""
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "user", "hello world")
        log.append(KEY, "system", "session compacted")
        fresh = ConversationLog(base_dir=tmp_path)
        preview, _ = fresh.last_message_info(KEY)
        assert preview == "session compacted"

    def test_non_dict_meta_row_does_not_crash_the_walk(self, tmp_path: Path) -> None:
        """A corrupt/foreign row with truthy non-dict ``meta`` is data, not a 500.

        The tail walk already tolerates every other malformed shape (unparseable
        lines, non-dict rows, non-string ``cls``); a row whose ``meta`` is a
        string or list must be walked past the same way — the predicate treats
        it as "not a stop card" instead of raising ``AttributeError`` into the
        roster and session-list endpoints.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "user", "hello world")
        path = log._path(KEY)
        rows = [
            {"role": "assistant", "content": "the answer", "ts": "2026-09-09T01:00:00+00:00"},
            {
                "role": "system",
                "content": "note",
                "meta": "corrupt-string-meta",
                "ts": "2026-09-09T02:00:00+00:00",
            },
        ]
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        fresh = ConversationLog(base_dir=tmp_path)
        preview, _ = fresh.last_message_info(KEY)  # must not raise
        assert preview == "note"

    def test_predicate_refuses_non_dict_meta_without_raising(self) -> None:
        """``is_stop_event_row`` answers False for every non-dict meta shape."""
        from kiro_crew.dashboard.state import is_stop_event_row

        for bad_meta in ("corrupt", ["kind", "stop_event"], 7, True):
            row = {"role": "system", "content": "x", "meta": bad_meta}
            assert is_stop_event_row(row) is False
        # A dict meta still matches.
        assert is_stop_event_row({"meta": {"kind": "stop_event"}}) is True

    def test_stop_only_transcript_returns_empty_text_with_real_epoch(self, tmp_path: Path) -> None:
        """Nothing conversational to show is an empty preview, not raw JSON.

        The epoch still reflects the stop row: it is the thread's newest
        activity, and callers fall back to file mtime only when it is 0.
        """
        log = ConversationLog(base_dir=tmp_path)
        payload = _stop_payload()
        log.append(KEY, "system", payload, cls=payload)
        fresh = ConversationLog(base_dir=tmp_path)
        preview, epoch = fresh.last_message_info(KEY)
        assert preview == ""
        assert epoch > 0.0
