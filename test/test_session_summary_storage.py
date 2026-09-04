"""Tests for the intent-summary sidecar cache in ConversationLog.

The load-bearing invariant is that writing a summary must not touch the session
JSONL. The transcript's mtime is the cache-validity signature for every derived
artifact and the sort key for the sessions list, so a summary write that advanced
it would invalidate unrelated caches and reorder the user's session list.
"""

from __future__ import annotations

import json

import pytest
from chat_test_helpers import move_transcript_past

from kiro_crew.history import ConversationLog


@pytest.fixture
def log(tmp_path):
    return ConversationLog(base_dir=tmp_path)


def _payload(title="do the thing"):
    return {
        "intents": [
            {
                "title": title,
                "ranges": [[1, 2]],
                "status": "active",
                "state": "in-progress",
                "last_touched_turn": 2,
            }
        ],
        "constraints": ["build with the flag set"],
    }


class TestIntentSummarySidecar:
    def test_round_trips(self, log):
        log.append("s1", "user", "hello")
        sig = log.session_mtime("s1")
        log.set_cached_intent_summary("s1", _payload(), sig)
        got = log.get_cached_intent_summary("s1")
        assert got is not None
        assert got["intents"][0]["title"] == "do the thing"
        assert got["constraints"] == ["build with the flag set"]

    def test_missing_cache_returns_none(self, log):
        log.append("s1", "user", "hello")
        assert log.get_cached_intent_summary("s1") is None

    def test_a_new_message_invalidates_the_cache(self, log):
        log.append("s1", "user", "hello")
        sig = log.session_mtime("s1")
        log.set_cached_intent_summary("s1", _payload(), sig)
        assert log.get_cached_intent_summary("s1") is not None
        log.append("s1", "user", "another turn")
        move_transcript_past(log, "s1", sig)  # don't rely on the OS tick
        assert log.get_cached_intent_summary("s1") is None

    def test_a_stale_signature_is_rejected(self, log):
        log.append("s1", "user", "hello")
        log.set_cached_intent_summary("s1", _payload(), 1.0)
        assert log.get_cached_intent_summary("s1") is None

    def test_corrupt_sidecar_degrades_to_a_miss(self, log):
        log.append("s1", "user", "hello")
        path = log._intent_summary_cache_path("s1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert log.get_cached_intent_summary("s1") is None

    def test_payload_without_intents_is_rejected(self, log):
        log.append("s1", "user", "hello")
        sig = log.session_mtime("s1")
        path = log._intent_summary_cache_path("s1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sig": sig, "constraints": []}), encoding="utf-8")
        assert log.get_cached_intent_summary("s1") is None

    def test_uses_a_separate_file_from_the_one_line_summary(self, log):
        """Independent writers must not share a file, or they clobber each other."""
        log.append("s1", "user", "hello")
        sig = log.session_mtime("s1")
        log.set_cached_summary("s1", "a one-line summary", sig)
        log.set_cached_intent_summary("s1", _payload(), sig)
        assert log.get_cached_summary("s1") == "a one-line summary"
        assert log.get_cached_intent_summary("s1") is not None
        assert log._summary_cache_path("s1") != log._intent_summary_cache_path("s1")

    def test_writing_one_does_not_clobber_the_other(self, log):
        log.append("s1", "user", "hello")
        sig = log.session_mtime("s1")
        log.set_cached_intent_summary("s1", _payload(), sig)
        log.set_cached_summary("s1", "one-liner", sig)
        assert log.get_cached_intent_summary("s1") is not None

    def test_sessions_are_isolated_from_each_other(self, log):
        log.append("s1", "user", "hello")
        log.append("s2", "user", "hello")
        log.set_cached_intent_summary("s1", _payload("first"), log.session_mtime("s1"))
        assert log.get_cached_intent_summary("s2") is None
        assert log.get_cached_intent_summary("s1")["intents"][0]["title"] == "first"

    def test_a_key_needing_folding_is_handled(self, log):
        key = "dashboard/chat:weird key"
        log.append(key, "user", "hello")
        log.set_cached_intent_summary(key, _payload(), log.session_mtime(key))
        assert log.get_cached_intent_summary(key) is not None


class TestTranscriptIsNeverTouched:
    def test_writing_a_summary_does_not_change_the_session_file(self, log):
        log.append("s1", "user", "hello")
        path = log._path("s1")
        before_bytes = path.read_bytes()
        before_mtime = log.session_mtime("s1")

        log.set_cached_intent_summary("s1", _payload(), before_mtime)

        assert path.read_bytes() == before_bytes
        assert log.session_mtime("s1") == before_mtime

    def test_reading_a_summary_does_not_change_the_session_file(self, log):
        log.append("s1", "user", "hello")
        sig = log.session_mtime("s1")
        log.set_cached_intent_summary("s1", _payload(), sig)
        log.get_cached_intent_summary("s1")
        assert log.session_mtime("s1") == sig

    def test_the_sidecar_lives_outside_the_sessions_transcript_names(self, log):
        log.append("s1", "user", "hello")
        sidecar = log._intent_summary_cache_path("s1")
        assert sidecar != log._path("s1")
        assert sidecar.parent.name == ".intents"


class TestDeleteSessionReapsTheSidecar:
    def test_delete_removes_the_intent_sidecar(self, log):
        log.append("s1", "user", "hello")
        log.set_cached_intent_summary("s1", _payload(), log.session_mtime("s1"))
        sidecar = log._intent_summary_cache_path("s1")
        assert sidecar.exists()

        log.delete_session("s1")

        assert not sidecar.exists()

    def test_delete_still_succeeds_when_no_sidecar_exists(self, log):
        log.append("s1", "user", "hello")
        assert log.delete_session("s1") is True


class TestWriteGuard:
    """The write is refused unless the transcript still exists with the same
    signature the generation started from -- a permanent delete completing
    while a model call is in flight must stay deleted."""

    def test_a_successful_write_reports_true(self, log):
        log.append("s1", "user", "hello")
        assert log.set_cached_intent_summary("s1", _payload(), log.session_mtime("s1")) is True

    def test_a_delete_during_generation_is_not_resurrected(self, log):
        log.append("s1", "user", "hello")
        sig = log.session_mtime("s1")  # generation captured this, then awaited
        log.delete_session("s1")  # permanent delete lands mid-flight

        assert log.set_cached_intent_summary("s1", _payload(), sig) is False
        assert not log._intent_summary_cache_path("s1").exists()

    def test_an_append_during_generation_refuses_the_stale_payload(self, log):
        log.append("s1", "user", "hello")
        sig = log.session_mtime("s1")
        log.append("s1", "user", "a newer turn")  # transcript moved on
        move_transcript_past(log, "s1", sig)  # don't rely on the OS tick

        assert log.set_cached_intent_summary("s1", _payload(), sig) is False
        assert log.get_cached_intent_summary("s1") is None
