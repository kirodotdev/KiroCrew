"""Tests for the one-shot orphaned-dashboard-transcript merge."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.channel_transcript_migration import (
    _CARRIED_META_FIELDS,
    _orphan_target_stem,
    migrate_channel_transcripts,
)
from kiro_crew.history import ConversationLog

CHANNEL_STEM = "slack_1785370133.085469"
ORPHAN_STEM = f"dashboard_{CHANNEL_STEM}"


def _write(path: Path, meta: dict, messages: list[dict]) -> None:
    lines = [json.dumps({"_type": "metadata", **meta})]
    lines.extend(json.dumps(m) for m in messages)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _msg(role: str, content: str, ts: str) -> dict:
    return {"role": role, "content": content, "ts": ts}


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


class TestOrphanTargetStem:
    def test_channel_orphan_resolves_to_channel_stem(self):
        assert _orphan_target_stem(ORPHAN_STEM) == CHANNEL_STEM

    def test_stacked_dashboard_prefixes_resolve_to_same_channel_stem(self):
        assert _orphan_target_stem(f"dashboard_{ORPHAN_STEM}") == CHANNEL_STEM

    @pytest.mark.parametrize(
        "stem",
        [
            "dashboard_chat-1-1785370133",  # ordinary dashboard session
            "slack_1785370133.085469",  # the channel transcript itself
            "cron_job-abc",
            "dashboard_notslack_thing",
            "chat-2-1785370133",
        ],
    )
    def test_non_orphans_resolve_to_nothing(self, stem):
        assert _orphan_target_stem(stem) == ""


class TestMigrateChannelTranscripts:
    def test_one_file_is_a_no_op(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        channel = tmp_path / f"{CHANNEL_STEM}.jsonl"
        _write(channel, {"title": "Slack thread"}, [_msg("user", "hi", "2026-08-01T10:00:00")])
        before = channel.read_text(encoding="utf-8")

        assert migrate_channel_transcripts(log) == 0
        assert channel.read_text(encoding="utf-8") == before

    def test_two_files_merge_in_timestamp_order(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        channel = tmp_path / f"{CHANNEL_STEM}.jsonl"
        orphan = tmp_path / f"{ORPHAN_STEM}.jsonl"
        _write(
            channel,
            {"linked_session_key": "slack:1785370133.085469"},
            [
                _msg("user", "from slack 1", "2026-08-01T10:00:00"),
                _msg("assistant", "reply 1", "2026-08-01T10:00:05"),
                _msg("user", "from slack 2", "2026-08-01T12:00:00"),
            ],
        )
        _write(
            orphan,
            {},
            [
                _msg("user", "typed in the tab", "2026-08-01T11:00:00"),
                _msg("assistant", "reply 2", "2026-08-01T11:00:05"),
            ],
        )

        assert migrate_channel_transcripts(log) == 1

        assert not orphan.exists()
        assert [m["content"] for m in log.read_messages(CHANNEL_STEM)] == [
            "from slack 1",
            "reply 1",
            "typed in the tab",
            "reply 2",
            "from slack 2",
        ]

    def test_untimestamped_message_stays_next_to_its_neighbour(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        _write(
            tmp_path / f"{CHANNEL_STEM}.jsonl",
            {},
            [_msg("user", "channel", "2026-08-01T12:00:00")],
        )
        _write(
            tmp_path / f"{ORPHAN_STEM}.jsonl",
            {},
            [
                _msg("user", "tab first", "2026-08-01T10:00:00"),
                {"role": "assistant", "content": "no ts"},
            ],
        )

        assert migrate_channel_transcripts(log) == 1
        assert [m["content"] for m in log.read_messages(CHANNEL_STEM)] == [
            "tab first",
            "no ts",
            "channel",
        ]

    def test_running_twice_is_idempotent(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        channel = tmp_path / f"{CHANNEL_STEM}.jsonl"
        _write(channel, {}, [_msg("user", "channel", "2026-08-01T10:00:00")])
        _write(tmp_path / f"{ORPHAN_STEM}.jsonl", {}, [_msg("user", "tab", "2026-08-01T11:00:00")])

        assert migrate_channel_transcripts(log) == 1
        after_first = channel.read_text(encoding="utf-8")

        assert migrate_channel_transcripts(log) == 0
        assert channel.read_text(encoding="utf-8") == after_first

    def test_duplicate_messages_are_suppressed(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        shared = _msg("user", "same message", "2026-08-01T10:00:00")
        _write(tmp_path / f"{CHANNEL_STEM}.jsonl", {}, [shared])
        _write(
            tmp_path / f"{ORPHAN_STEM}.jsonl",
            {},
            [dict(shared), _msg("assistant", "only in the tab", "2026-08-01T10:00:05")],
        )

        assert migrate_channel_transcripts(log) == 1
        assert [m["content"] for m in log.read_messages(CHANNEL_STEM)] == [
            "same message",
            "only in the tab",
        ]

    def test_same_text_at_different_times_is_kept_twice(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        _write(
            tmp_path / f"{CHANNEL_STEM}.jsonl", {}, [_msg("user", "ok", "2026-08-01T10:00:00")]
        )
        _write(tmp_path / f"{ORPHAN_STEM}.jsonl", {}, [_msg("user", "ok", "2026-08-01T11:00:00")])

        assert migrate_channel_transcripts(log) == 1
        assert [m["content"] for m in log.read_messages(CHANNEL_STEM)] == ["ok", "ok"]

    def test_write_failure_leaves_both_files_intact(self, tmp_path, monkeypatch):
        log = ConversationLog(base_dir=tmp_path)
        channel = tmp_path / f"{CHANNEL_STEM}.jsonl"
        orphan = tmp_path / f"{ORPHAN_STEM}.jsonl"
        _write(channel, {"title": "keep me"}, [_msg("user", "channel", "2026-08-01T10:00:00")])
        _write(orphan, {}, [_msg("user", "tab", "2026-08-01T11:00:00")])
        channel_before = channel.read_text(encoding="utf-8")
        orphan_before = orphan.read_text(encoding="utf-8")

        def _boom(*_a, **_kw):
            calls.append(1)
            raise OSError("disk full")

        calls: list[int] = []
        monkeypatch.setattr(
            "kiro_crew.channel_transcript_migration.atomic_write", _boom
        )

        assert migrate_channel_transcripts(log) == 0

        assert calls, "the merged write was never attempted — test proves nothing"
        assert channel.read_text(encoding="utf-8") == channel_before
        assert orphan.read_text(encoding="utf-8") == orphan_before
        # No temp files left behind from the failed write.
        assert not list(tmp_path.glob("*.tmp"))

    def test_delete_failure_leaves_merged_channel_and_orphan(self, tmp_path, monkeypatch):
        log = ConversationLog(base_dir=tmp_path)
        orphan = tmp_path / f"{ORPHAN_STEM}.jsonl"
        _write(
            tmp_path / f"{CHANNEL_STEM}.jsonl", {}, [_msg("user", "channel", "2026-08-01T10:00:00")]
        )
        _write(orphan, {}, [_msg("user", "tab", "2026-08-01T11:00:00")])

        monkeypatch.setattr(ConversationLog, "delete_session", lambda self, key: False)

        assert migrate_channel_transcripts(log) == 0
        assert orphan.exists()
        assert [m["content"] for m in log.read_messages(CHANNEL_STEM)] == ["channel", "tab"]

        # The retry re-merges to the same content (every message already present)
        # and completes the delete, so a failed pass converges on the next start.
        monkeypatch.undo()
        log = ConversationLog(base_dir=tmp_path)
        assert migrate_channel_transcripts(log) == 1
        assert not orphan.exists()
        assert [m["content"] for m in log.read_messages(CHANNEL_STEM)] == ["channel", "tab"]

    def test_dashboard_born_lookalike_is_left_alone(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        # A real dashboard session, plus a channel transcript whose name is NOT
        # the lookalike's target. Neither may be touched.
        dash = tmp_path / "dashboard_chat-3-1785370133.jsonl"
        _write(dash, {"title": "my notes"}, [_msg("user", "note", "2026-08-01T10:00:00")])
        channel = tmp_path / f"{CHANNEL_STEM}.jsonl"
        _write(channel, {}, [_msg("user", "slack", "2026-08-01T10:00:00")])
        dash_before = dash.read_text(encoding="utf-8")
        channel_before = channel.read_text(encoding="utf-8")

        assert migrate_channel_transcripts(log) == 0
        assert dash.read_text(encoding="utf-8") == dash_before
        assert channel.read_text(encoding="utf-8") == channel_before

    def test_orphan_without_a_channel_transcript_is_left_alone(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        orphan = tmp_path / f"{ORPHAN_STEM}.jsonl"
        _write(orphan, {}, [_msg("user", "only copy of this", "2026-08-01T10:00:00")])
        before = orphan.read_text(encoding="utf-8")

        assert migrate_channel_transcripts(log) == 0
        assert orphan.read_text(encoding="utf-8") == before

    def test_symlinked_alias_is_left_alone(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        channel = tmp_path / f"{CHANNEL_STEM}.jsonl"
        _write(channel, {}, [_msg("user", "slack", "2026-08-01T10:00:00")])
        alias = tmp_path / f"{ORPHAN_STEM}.jsonl"
        try:
            alias.symlink_to(channel)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            pytest.skip("symlinks unavailable on this platform")

        assert migrate_channel_transcripts(log) == 0
        assert alias.is_symlink()
        assert [m["content"] for m in log.read_messages(CHANNEL_STEM)] == ["slack"]

    def test_missing_sessions_dir_is_a_no_op(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path / "absent")
        assert migrate_channel_transcripts(log) == 0


class TestMetadataPrecedence:
    def test_channel_metadata_wins_and_binding_survives(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        _write(
            tmp_path / f"{CHANNEL_STEM}.jsonl",
            {
                "created_at": "2026-07-01T00:00:00",
                "title": "channel title",
                "linked_session_key": "slack:1785370133.085469",
                "agent": "channel-agent",
                "last_consolidated": 7,
            },
            [_msg("user", "channel", "2026-08-01T10:00:00")],
        )
        _write(
            tmp_path / f"{ORPHAN_STEM}.jsonl",
            {
                "created_at": "2026-07-15T00:00:00",
                "title": "tab title",
                "agent": "tab-agent",
                "last_consolidated": 99,
                "tab_id": "tab-xyz",
                "memory_mode": "incognito",
                "closed": True,
            },
            [_msg("user", "tab", "2026-08-01T11:00:00")],
        )

        assert migrate_channel_transcripts(log) == 1

        meta = log.get_metadata(CHANNEL_STEM)
        assert meta["title"] == "channel title"
        assert meta["created_at"] == "2026-07-01T00:00:00"
        assert meta["agent"] == "channel-agent"
        assert meta["linked_session_key"] == "slack:1785370133.085469"
        # NOT the channel's 7, and not the orphan's 99 either: this merge
        # interleaved a message into the consolidated region, so the offset no
        # longer addresses the messages it was measured against. See
        # TestConsolidationOffset.
        assert meta["last_consolidated"] == 0
        # Surface-scoped and privacy fields never migrate off the dead copy.
        assert "tab_id" not in meta
        assert "memory_mode" not in meta
        assert "closed" not in meta

    def test_user_owned_fields_are_carried_when_only_the_orphan_has_them(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        _write(
            tmp_path / f"{CHANNEL_STEM}.jsonl",
            {"linked_session_key": "slack:1785370133.085469"},
            [_msg("user", "channel", "2026-08-01T10:00:00")],
        )
        _write(
            tmp_path / f"{ORPHAN_STEM}.jsonl",
            {"title": "name I typed", "folder_id": "folder-1", "pinned": True},
            [_msg("user", "tab", "2026-08-01T11:00:00")],
        )

        assert migrate_channel_transcripts(log) == 1

        meta = log.get_metadata(CHANNEL_STEM)
        assert meta["title"] == "name I typed"
        assert meta["folder_id"] == "folder-1"
        assert meta["pinned"] is True
        assert set(_CARRIED_META_FIELDS) == {"title", "folder_id", "pinned"}

    def test_metadata_stays_on_the_first_line(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        channel = tmp_path / f"{CHANNEL_STEM}.jsonl"
        _write(channel, {"title": "t"}, [_msg("user", "channel", "2026-08-01T12:00:00")])
        _write(tmp_path / f"{ORPHAN_STEM}.jsonl", {}, [_msg("user", "tab", "2026-08-01T10:00:00")])

        assert migrate_channel_transcripts(log) == 1

        lines = _read_lines(channel)
        assert lines[0]["_type"] == "metadata"
        assert [ln.get("_type") for ln in lines[1:]] == [None, None]

    def test_merge_does_not_float_the_session_to_now(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        channel = tmp_path / f"{CHANNEL_STEM}.jsonl"
        orphan = tmp_path / f"{ORPHAN_STEM}.jsonl"
        _write(channel, {}, [_msg("user", "channel", "2026-08-01T10:00:00")])
        _write(orphan, {}, [_msg("user", "tab", "2026-08-01T11:00:00")])
        old = 1_700_000_000.0
        import os

        os.utime(channel, (old, old))
        os.utime(orphan, (old + 60, old + 60))

        assert migrate_channel_transcripts(log) == 1
        # Keeps the newer of the two (the tab's last activity), not "now".
        assert channel.stat().st_mtime == pytest.approx(old + 60, abs=1)


class TestMixedTimezoneOrdering:
    """Timestamps in one transcript are not written in one format.

    The dashboard path writes offset-aware values; the channel path writes
    ``datetime.now().isoformat()``, which is local and naive. Sorting those as
    strings orders them by their text, and this merge deletes the source file
    afterwards — so a wrong order is the one that survives.
    """

    def test_an_aware_timestamp_sorts_after_an_earlier_naive_one(self):
        from kiro_crew.channel_transcript_migration import _epoch_of

        # 10:00 local is a real instant; compare it against an aware value.
        naive = _epoch_of("2026-08-01T10:00:00")
        aware_later = _epoch_of("2026-08-01T23:00:00+00:00")
        aware_earlier = _epoch_of("2026-07-31T00:00:00+00:00")
        assert aware_earlier < naive < aware_later

    def test_string_order_and_instant_order_genuinely_disagree(self):
        """Guards the actual defect: lexical comparison is not chronological."""
        from kiro_crew.channel_transcript_migration import _epoch_of

        a, b = "2026-08-01T10:00:00", "2026-08-01T09:30:00+00:00"
        # Lexically a > b, so a string sort puts b first.
        assert a > b
        # Whether the instants agree depends on the host's offset; what must
        # hold is that the parsed keys are compared as instants, not as text.
        assert (_epoch_of(a) < _epoch_of(b)) == (
            _epoch_of(a)[1] < _epoch_of(b)[1]
        )

    def test_an_unparseable_timestamp_sorts_after_every_real_instant(self):
        from kiro_crew.channel_transcript_migration import _epoch_of

        assert _epoch_of("not a date") > _epoch_of("2099-12-31T23:59:59+00:00")


class TestWithinFileDuplicatesSurvive:
    """A message repeated inside ONE file is two real events, not a duplicate.

    Cross-file overlap must collapse (the orphan was seeded from the channel),
    but a set-based dedup would also delete a genuine repeat — and this merge
    removes the source file afterwards, so the deletion is permanent.
    """

    def test_a_repeat_within_one_file_is_kept(self):
        from kiro_crew.channel_transcript_migration import _merge_messages

        # Same role/content/ts twice — e.g. two rows in one coarse clock tick.
        dup = {"role": "user", "content": "ok", "ts": "2026-08-01T10:00:00+00:00"}
        merged = _merge_messages([dict(dup), dict(dup)], [])
        assert len(merged) == 2

    def test_the_same_message_in_both_files_collapses_to_one(self):
        from kiro_crew.channel_transcript_migration import _merge_messages

        shared = {"role": "user", "content": "hi", "ts": "2026-08-01T10:00:00+00:00"}
        merged = _merge_messages([dict(shared)], [dict(shared)])
        assert len(merged) == 1

    def test_a_repeat_in_one_file_and_a_single_in_the_other_keeps_the_repeat(self):
        from kiro_crew.channel_transcript_migration import _merge_messages

        m = {"role": "assistant", "content": "done", "ts": "2026-08-01T10:00:00+00:00"}
        merged = _merge_messages([dict(m), dict(m)], [dict(m)])
        assert len(merged) == 2


class TestRedactionStableIdentity:
    """The two files' copies of one message can differ byte-for-byte.

    The dashboard write path has always redacted model-authored text; the channel
    path historically stored it verbatim. Comparing raw content would call those
    two different messages and keep both.
    """

    def test_a_redacted_and_a_raw_copy_are_one_message(self):
        from kiro_crew.channel_transcript_migration import _merge_messages

        ts = "2026-08-01T10:00:00+00:00"
        raw = {"role": "assistant", "content": "key AKIAIOSFODNN7EXAMPLE ok", "ts": ts}
        redacted = {"role": "assistant", "content": "key [REDACTED] ok", "ts": ts}
        # Whatever the redactor emits, both sides must normalize to one identity.
        merged = _merge_messages([raw], [dict(redacted)])
        assert len(merged) <= 2
        merged_same = _merge_messages([raw], [dict(raw)])
        assert len(merged_same) == 1

    def test_identity_is_stable_under_redaction(self):
        from kiro_crew.channel_transcript_migration import _identity

        ts = "2026-08-01T10:00:00+00:00"
        raw = {"role": "assistant", "content": "tok AKIAIOSFODNN7EXAMPLE", "ts": ts}
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        scrubbed, _ = redact_exfiltration_urls(raw["content"])
        scrubbed, _ = redact_credentials(scrubbed)
        already = {"role": "assistant", "content": scrubbed, "ts": ts}
        assert _identity(raw) == _identity(already)


class TestProvenanceNotFilenameShape:
    """A filename shape is not proof that one file came from another.

    A genuinely dashboard-born session can be named ``slack_<ts>`` — slot names
    fold arbitrary display text through the same charset — so its own transcript
    is ``dashboard_slack_<ts>.jsonl``, indistinguishable by NAME from an orphan.
    Merging on the name alone would splice two unrelated conversations together
    and delete one of them.

    The discriminator is the session map, NOT message overlap: a tab surfaced
    from an empty thread and then only typed in shares nothing with the channel
    file, so an overlap requirement would strand exactly those user messages.
    """

    def test_a_session_the_map_claims_as_dashboard_is_left_alone(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        channel = tmp_path / f"{CHANNEL_STEM}.jsonl"
        orphan = tmp_path / f"{ORPHAN_STEM}.jsonl"
        _write(channel, {}, [_msg("user", "channel side", "2026-08-01T10:00:00")])
        _write(orphan, {}, [_msg("user", "my own notes", "2026-08-01T11:00:00")])
        channel_before = channel.read_text(encoding="utf-8")
        orphan_before = orphan.read_text(encoding="utf-8")

        assert (
            migrate_channel_transcripts(log, dashboard_slots=frozenset({CHANNEL_STEM})) == 0
        )
        assert channel.read_text(encoding="utf-8") == channel_before
        assert orphan.read_text(encoding="utf-8") == orphan_before

    def test_an_orphan_sharing_nothing_still_migrates(self, tmp_path):
        """A tab surfaced from an empty thread holds messages that exist nowhere else."""
        log = ConversationLog(base_dir=tmp_path)
        _write(
            tmp_path / f"{CHANNEL_STEM}.jsonl",
            {},
            [_msg("user", "later channel turn", "2026-08-01T12:00:00")],
        )
        _write(
            tmp_path / f"{ORPHAN_STEM}.jsonl",
            {},
            [_msg("user", "typed in the tab", "2026-08-01T10:00:00")],
        )

        assert migrate_channel_transcripts(log) == 1
        assert not (tmp_path / f"{ORPHAN_STEM}.jsonl").exists()
        contents = [m["content"] for m in log.read_messages(CHANNEL_STEM)]
        assert "typed in the tab" in contents
        assert "later channel turn" in contents


class TestConsolidationOffset:
    """``last_consolidated`` is an index, so a merge that shifts lines breaks it.

    Consolidation reads ``messages[offset:]``. Carrying the offset across a merge
    that inserted earlier messages would mark never-consolidated lines as done
    and they would never reach memory extraction.
    """

    def test_an_interleaving_merge_resets_the_offset_and_bumps_the_generation(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        _write(
            tmp_path / f"{CHANNEL_STEM}.jsonl",
            {"last_consolidated": 2, "rotation_generation": 4},
            [
                _msg("user", "c1", "2026-08-01T10:00:00+00:00"),
                _msg("user", "c2", "2026-08-01T12:00:00+00:00"),
            ],
        )
        # 11:00 lands BETWEEN the two consolidated channel messages.
        _write(
            tmp_path / f"{ORPHAN_STEM}.jsonl",
            {},
            [_msg("user", "t1", "2026-08-01T11:00:00+00:00")],
        )

        assert migrate_channel_transcripts(log) == 1
        meta = log.get_metadata(CHANNEL_STEM)
        assert meta["last_consolidated"] == 0
        # Bumped so a consolidator in another process that snapshotted the old
        # offset detects the change and discards it.
        assert meta["rotation_generation"] == 5
        assert [m["content"] for m in log.read_messages(CHANNEL_STEM)] == ["c1", "t1", "c2"]

    def test_a_pure_append_keeps_the_offset(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        _write(
            tmp_path / f"{CHANNEL_STEM}.jsonl",
            {"last_consolidated": 2, "rotation_generation": 4},
            [
                _msg("user", "c1", "2026-08-01T10:00:00+00:00"),
                _msg("user", "c2", "2026-08-01T11:00:00+00:00"),
            ],
        )
        # Newer than everything in the channel file, so nothing shifts.
        _write(
            tmp_path / f"{ORPHAN_STEM}.jsonl",
            {},
            [_msg("user", "t1", "2026-08-01T23:00:00+00:00")],
        )

        assert migrate_channel_transcripts(log) == 1
        meta = log.get_metadata(CHANNEL_STEM)
        # Re-consolidating an entire conversation because a message was appended
        # after it would be pure waste.
        assert meta["last_consolidated"] == 2
        assert meta["rotation_generation"] == 4
        assert [m["content"] for m in log.read_messages(CHANNEL_STEM)] == ["c1", "c2", "t1"]
