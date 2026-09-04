"""Regression tests for issue #1948 — channel-session timestamps in UTC.

A Slack/channel conversation persists to a session transcript whose first line
is a metadata record carrying ``created_at`` (and, later, ``updated_at`` /
``compacted_at`` / ``rotated_at``). The dashboard renders a session's
``created_at`` as a local-time timestamp, and the frontend does that conversion
by parsing the stored string as an *instant*: an offset-aware value
(``…-04:00`` / ``…+00:00``) is converted to the viewer's zone, but a naive value
(``2026-08-07T03:35:00`` with no offset) is read verbatim, so a message the user
sent at 11:35 PM EDT showed as 03:35 AM — its UTC wall clock.

The message ROWS were already offset-aware (``monotonic_transcript_ts``); these
tests lock in that the metadata timestamps are too, so the whole transcript
speaks one unambiguous format regardless of the host timezone.

Deliberately timezone-independent: the guarantee asserted is "the stored string
carries an offset", which holds on any host (including a UTC CI runner, where
the offset is simply ``+00:00``). A naive value has ``tzinfo is None`` and fails
here — which is exactly the regression.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kiro_crew.history import ConversationLog, metadata_now_iso

CHANNEL_KEY = "slack:1785370133.085469"


def _assert_offset_aware(value: str) -> datetime:
    """Parse *value* and assert it is an unambiguous, offset-aware instant."""
    assert isinstance(value, str) and value, f"missing timestamp: {value!r}"
    parsed = datetime.fromisoformat(value)
    # The crux of #1948: a naive string (no offset) parses to tzinfo=None and
    # the frontend renders it verbatim as if UTC. An offset-aware string carries
    # the information needed to convert to the viewer's local zone.
    assert parsed.tzinfo is not None, f"timestamp is naive (no offset): {value!r}"
    assert parsed.utcoffset() is not None
    # Coherence check: it represents roughly now, not a parsing artefact.
    now = datetime.now(timezone.utc)
    assert abs(parsed - now) < timedelta(minutes=5), f"timestamp not ~now: {value!r}"
    return parsed


class TestChannelTranscriptTimezone:
    def test_metadata_now_iso_is_offset_aware(self):
        """The metadata stamp helper always records an offset."""
        _assert_offset_aware(metadata_now_iso())

    def test_channel_session_created_at_is_offset_aware(self, tmp_path):
        """A freshly created channel session's created_at carries an offset."""
        log = ConversationLog(base_dir=tmp_path)
        log.append(CHANNEL_KEY, "user", "sent at 11:35 PM local")

        created_at = log.get_metadata(CHANNEL_KEY).get("created_at")
        _assert_offset_aware(created_at)

    def test_whole_transcript_speaks_one_format(self, tmp_path):
        """Metadata line AND message rows are all offset-aware.

        This is what makes the dashboard render every timestamp in the viewer's
        zone: the reader never has to guess a timezone for any line in the file.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append(CHANNEL_KEY, "user", "hello from slack")
        log.append(CHANNEL_KEY, "assistant", "hi there")

        meta = log.get_metadata(CHANNEL_KEY)
        created = _assert_offset_aware(meta["created_at"])

        rows = log.read_messages(CHANNEL_KEY)
        assert rows, "expected message rows"
        row_instants = [_assert_offset_aware(m["ts"]) for m in rows]

        # Ordered as written, and the metadata (session creation) is not after
        # the first row — proves the two format families are comparable as
        # instants rather than as raw strings.
        assert row_instants == sorted(row_instants)
        assert created <= row_instants[0] + timedelta(seconds=1)

    def test_update_metadata_created_at_is_offset_aware(self, tmp_path):
        """update_metadata's create path also stamps an offset-aware created_at."""
        log = ConversationLog(base_dir=tmp_path)
        log.update_metadata(CHANNEL_KEY, {"agent": "kirocrew"})

        _assert_offset_aware(log.get_metadata(CHANNEL_KEY).get("created_at"))
