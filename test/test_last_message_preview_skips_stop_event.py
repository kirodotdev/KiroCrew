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

``last_message_info`` also returns a third value, ``newest_is_stop``: True when
the newest real row IS that stop card. The preview text alone reads as ongoing
work on a just-stopped thread ("Running the analysis now."), so the locale-aware
client renders a "Stopped" chip beside it from this locale-independent boolean.
It rides the same predicate as the skip, and is False again once a newer
conversational row lands.
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
        preview, _, stopped = log.last_message_info(KEY)
        assert preview == "hello world"
        # The newest real row IS the stop card, so the flag is set — the client
        # renders a "Stopped" chip beside the conversational preview.
        assert stopped is True

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
        preview, epoch, stopped = fresh.last_message_info(KEY)
        assert preview == "hello world"
        # 05:00, the stop row's ts — not 00:00, the previewed row's.
        assert epoch == datetime(2026, 9, 9, 5, 0, tzinfo=timezone.utc).timestamp()
        assert stopped is True

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
        preview, epoch, stopped = fresh.last_message_info(KEY)
        assert preview == "the real answer"
        # The stop row's 05:00 carries (activity); the quiet row's 09:00 does not.
        assert epoch == datetime(2026, 9, 9, 5, 0, tzinfo=timezone.utc).timestamp()
        # The newest real row is the quiet reply, NOT the stop — so the flag is
        # off. This is the clearing direction: a real row after the stop takes
        # the chip down, even a zero-width quiet one.
        assert stopped is False

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
        preview, _, stopped = fresh.last_message_info(KEY)
        assert preview == "hello world"
        assert stopped is True

    def test_stopping_state_row_is_skipped_too(self, tmp_path: Path) -> None:
        """An unresolved (still "stopping") card is no more conversational."""
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "user", "hello world")
        payload = _stop_payload(state="stopping")
        log.append(KEY, "system", payload, cls=payload)
        fresh = ConversationLog(base_dir=tmp_path)
        preview, _, stopped = fresh.last_message_info(KEY)
        assert preview == "hello world"
        assert stopped is True

    def test_ordinary_system_row_still_previews(self, tmp_path: Path) -> None:
        """Only stop cards are skipped — not the whole ``system`` role."""
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "user", "hello world")
        log.append(KEY, "system", "session compacted")
        fresh = ConversationLog(base_dir=tmp_path)
        preview, _, stopped = fresh.last_message_info(KEY)
        assert preview == "session compacted"
        assert stopped is False

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
        preview, _, stopped = fresh.last_message_info(KEY)  # must not raise
        assert preview == "note"
        # A truthy non-dict meta is "not a stop card", so the newest real row
        # (the note) leaves the flag off — same guard the predicate applies.
        assert stopped is False

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
        preview, epoch, stopped = fresh.last_message_info(KEY)
        assert preview == ""
        assert epoch > 0.0
        # No conversational text, but the newest (only) real row is the stop —
        # the client shows the chip alone over the empty subtitle.
        assert stopped is True


class TestNewestIsStopFlagClears:
    """The newest-event-is-a-stop signal in one place: a stop is the newest
    event until a real message replaces it. This pins BOTH directions on one
    transcript so the clearing rule (the one judgement call) cannot regress:
    the chip appears on the stop, and the next real message takes it down.
    """

    def test_stop_sets_the_flag_then_a_later_message_clears_it(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "assistant", "Running the analysis now.")
        payload = _stop_payload()
        log.append(KEY, "system", payload, cls=payload)

        # Newest event is the stop press: the preview is the last real line
        # (which reads as ongoing work), and the flag is set so the client can
        # say "Stopped" beside it.
        after_stop = ConversationLog(base_dir=tmp_path)
        preview, _, stopped = after_stop.last_message_info(KEY)
        assert preview == "Running the analysis now."
        assert stopped is True

        # The member speaks again. The newest real row is now that message, not
        # the stop — the flag clears and the chip comes down. A bare resume that
        # appended no conversational row would leave the stop newest and the flag
        # set, which is the honest reading and why this is the chosen semantics.
        log.append(KEY, "user", "actually, hold on")
        after_reply = ConversationLog(base_dir=tmp_path)
        preview, _, stopped = after_reply.last_message_info(KEY)
        assert preview == "actually, hold on"
        assert stopped is False
