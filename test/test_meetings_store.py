"""Store-layer tests: the path-containment barrier and the on-disk layout.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

The containment tests are the load-bearing ones. Every path this app builds from
a request goes through ``safe_meeting_id`` + ``contain``, so a gap here is a
traversal or symlink-escape write anywhere in the app.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from meetings_helpers import (  # noqa: F401
    reset_module_state_fixture,
    root_fixture,
)

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store


class TestSafeMeetingId:
    def test_plain_id_passes(self):
        assert store.safe_meeting_id("sprint-standup") == "sprint-standup"

    def test_colons_become_underscores(self):
        # Calendar event ids carry colons and are not legal Windows path chars.
        assert store.safe_meeting_id("i_AAMk:OG:abc") == "i_AAMk_OG_abc"

    @pytest.mark.parametrize(
        "raw",
        [
            "../../etc/passwd",
            "..",
            ".hidden",
            "a/b",
            "a\\b",
            "with space",
            "semi;colon",
            "null\x00byte",
            "",
            "   ",
        ],
    )
    def test_rejects_unsafe(self, raw):
        with pytest.raises(store.MeetingsPathError):
            store.safe_meeting_id(raw)

    @pytest.mark.parametrize("raw", [None, 5, [], {}, True])
    def test_rejects_non_string(self, raw):
        with pytest.raises(store.MeetingsPathError):
            store.safe_meeting_id(raw)

    def test_rejects_overlong(self):
        with pytest.raises(store.MeetingsPathError):
            store.safe_meeting_id("a" * (k.MAX_MEETING_ID_LEN + 1))

    def test_traversal_via_colon_substitution_still_refused(self):
        # ".:./.:./etc" must not become "..\/..\/etc" — the substitution happens
        # BEFORE validation, so the result is still checked.
        with pytest.raises(store.MeetingsPathError):
            store.safe_meeting_id("../:etc")


class TestSafeAgentId:
    def test_slug_passes(self):
        assert store.safe_agent_id("note-taker") == "note-taker"

    @pytest.mark.parametrize(
        "raw", ["../x", "Note-Taker", "note_taker", "-lead", "", "a" * 65, "a/b"]
    )
    def test_rejects_unsafe(self, raw):
        with pytest.raises(store.MeetingsPathError):
            store.safe_agent_id(raw)


class TestContainment:
    def test_inside_root_returns_resolved(self, root: Path):
        target = root / "meetings" / "x" / "note-taker.md"
        assert store.contain(target, operation="test", root=root) == target.resolve()

    def test_outside_root_raises_403(self, root: Path, tmp_path: Path):
        with pytest.raises(store.MeetingsPathError) as excinfo:
            store.contain(tmp_path / "elsewhere.md", operation="test", root=root)
        assert excinfo.value.status == 403

    def test_dotdot_collapsed_then_refused(self, root: Path):
        with pytest.raises(store.MeetingsPathError):
            store.contain(root / ".." / "escape.md", operation="test", root=root)

    @pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
    def test_symlink_out_of_root_refused(self, root: Path, tmp_path: Path):
        # The attack this barrier exists for: a symlink planted INSIDE the data
        # dir pointing out of it. resolve() follows it, so containment fails and
        # the write never lands on the target.
        outside = tmp_path / "outside"
        outside.mkdir()
        link = root / "meetings" / "escape"
        link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(store.MeetingsPathError):
            store.contain(link / "pwned.md", operation="test", root=root)

    def test_meeting_dir_is_contained(self, root: Path):
        mdir = store.meeting_dir("standup", root)
        assert mdir.is_relative_to(root.resolve())
        assert mdir.name == "standup"


class TestConfig:
    def test_defaults_seeded_on_fresh_root(self, tmp_path: Path):
        config = store.read_config(tmp_path / "fresh")
        assert config["stt_provider"] == k.STT_PROVIDER_KIROCREW
        assert config["task_provider"] == k.TASK_PROVIDER_LOCAL
        assert config["calendar"]["provider"] == k.CALENDAR_PROVIDER_NONE
        assert [a["id"] for a in config["meeting_agents"]] == ["note-taker", "sketch-artist"]

    def test_missing_keys_backfilled_from_defaults(self, root: Path):
        store.config_path(root).write_text(json.dumps({"task_provider": "local"}))
        config = store.read_config(root)
        assert config["meeting_agents"]  # backfilled
        assert config["poll_interval_active"] == 5000

    def test_malformed_config_falls_back(self, root: Path):
        store.config_path(root).write_text("{not json")
        assert store.read_config(root)["stt_provider"] == k.STT_PROVIDER_KIROCREW

    def test_non_object_config_falls_back(self, root: Path):
        store.config_path(root).write_text("[1, 2, 3]")
        assert store.read_config(root)["meeting_agents"]

    def test_roundtrip(self, root: Path):
        store.write_config({"task_provider": "local", "meeting_agents": []}, root)
        assert store.read_config(root)["task_provider"] == "local"


class TestEnsureDataDirs:
    def test_creates_subtree_and_seeds(self, tmp_path: Path):
        data = store.ensure_data_dirs(tmp_path / "seeded")
        for name in k.DATA_SUBDIRS:
            assert (data / name).is_dir()
        assert (data / k.DICTIONARY_FILE).is_file()
        assert (data / k.CONFIG_FILE).is_file()

    def test_idempotent_and_non_destructive(self, root: Path):
        store.config_path(root).write_text(json.dumps({"task_provider": "custom"}))
        (root / k.DICTIONARY_FILE).write_text("# mine\n")
        store.ensure_data_dirs(root)
        assert json.loads(store.config_path(root).read_text())["task_provider"] == "custom"
        assert (root / k.DICTIONARY_FILE).read_text() == "# mine\n"


class TestMeetingMeta:
    def test_save_and_load(self, root: Path):
        store.write_meeting_meta("m1", store.new_meeting_meta("m1", "Standup"), root)
        meta = store.read_meeting_meta("m1", root)
        assert meta is not None
        assert meta["title"] == "Standup"
        assert meta["status"] == k.STATUS_IDLE

    def test_missing_returns_none(self, root: Path):
        assert store.read_meeting_meta("nope", root) is None

    def test_list_meetings(self, root: Path):
        for name, title in (("a", "Alpha"), ("b", "Beta")):
            store.write_meeting_meta(name, store.new_meeting_meta(name, title), root)
        titles = {m["title"] for m in store.list_meetings(root)}
        assert titles == {"Alpha", "Beta"}

    def test_list_skips_malformed(self, root: Path):
        store.write_meeting_meta("ok", store.new_meeting_meta("ok", "Fine"), root)
        bad = store.meetings_root(root) / "bad"
        bad.mkdir(parents=True)
        (bad / k.SESSION_META_FILE).write_text("{broken")
        assert [m["title"] for m in store.list_meetings(root)] == ["Fine"]

    def test_list_empty_when_no_meetings(self, tmp_path: Path):
        assert store.list_meetings(tmp_path / "empty") == []

    def test_delete_removes_the_complete_meeting_directory(self, root: Path):
        store.write_meeting_meta("m1", store.new_meeting_meta("m1", "Standup"), root)
        store.write_tasks("m1", [{"id": "t1", "description": "Ship it"}], root)
        store.write_agent_output("m1", {"id": "note-taker"}, "# Notes", root)
        store.append_transcript("m1", "The transcript is durable.", "speech", root)

        assert store.delete_meeting("m1", root) is True
        assert not store.meeting_dir("m1", root).exists()
        assert store.list_meetings(root) == []

    def test_delete_unknown_meeting_returns_false(self, root: Path):
        assert store.delete_meeting("missing", root) is False

    @pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
    def test_delete_refuses_an_in_root_directory_link(self, root: Path):
        store.write_meeting_meta("target", store.new_meeting_meta("target", "Keep"), root)
        (store.meetings_root(root) / "alias").symlink_to(
            store.meeting_dir("target", root), target_is_directory=True
        )

        with pytest.raises(store.MeetingsPathError):
            store.delete_meeting("alias", root)
        assert store.read_meeting_meta("target", root) is not None


class TestTranscript:
    def test_roundtrip_preserves_order_unicode_and_source(self, root: Path):
        first = store.append_transcript("m1", "Buongiorno, 世界", k.TRANSCRIPT_SOURCE_SPEECH, root)
        second = store.append_transcript("m1", "Typed correction", k.TRANSCRIPT_SOURCE_TYPED, root)

        assert first is not None and second is not None
        entries = store.read_transcript("m1", root)
        assert [entry["id"] for entry in entries] == [first["id"], second["id"]]
        assert [entry["text"] for entry in entries] == [
            "Buongiorno, 世界",
            "Typed correction",
        ]
        assert [entry["source"] for entry in entries] == ["speech", "typed"]
        assert all(entry["timestamp"].endswith("Z") for entry in entries)

    def test_missing_file_is_a_legacy_empty_transcript(self, root: Path):
        store.write_meeting_meta("legacy", store.new_meeting_meta("legacy", "Old"), root)
        assert store.read_transcript("legacy", root) == []

    def test_malformed_rows_do_not_hide_valid_speech(self, root: Path):
        entry = store.append_transcript("m1", "kept", k.TRANSCRIPT_SOURCE_SPEECH, root)
        assert entry is not None
        with store.transcript_path("m1", root).open("a", encoding="utf-8") as transcript:
            transcript.write("{unfinished\n")

        assert [row["text"] for row in store.read_transcript("m1", root)] == ["kept"]

    def test_truncated_utf8_tail_does_not_hide_valid_speech(self, root: Path):
        first = store.append_transcript("m1", "kept", k.TRANSCRIPT_SOURCE_SPEECH, root)
        assert first is not None
        with store.transcript_path("m1", root).open("ab") as transcript:
            transcript.write(b'{"text":"\xe2\x82')

        second = store.append_transcript("m1", "recovered", k.TRANSCRIPT_SOURCE_SPEECH, root)

        assert second is not None
        assert [row["text"] for row in store.read_transcript("m1", root)] == [
            "kept",
            "recovered",
        ]

    def test_cursor_reads_only_bytes_appended_after_the_previous_page(self, root: Path):
        first = store.append_transcript("m1", "first", k.TRANSCRIPT_SOURCE_SPEECH, root)
        assert first is not None
        first_page, cursor = store.read_transcript_page("m1", root=root)

        second = store.append_transcript("m1", "second", k.TRANSCRIPT_SOURCE_TYPED, root)
        assert second is not None
        next_page, next_cursor = store.read_transcript_page("m1", cursor, root)

        assert first_page == [first]
        assert next_page == [second]
        assert next_cursor > cursor

    def test_cursor_after_a_crash_tail_recovers_the_next_valid_row(self, root: Path):
        first = store.append_transcript("m1", "first", k.TRANSCRIPT_SOURCE_SPEECH, root)
        assert first is not None
        with store.transcript_path("m1", root).open("ab") as transcript:
            transcript.write(b'{"unfinished"')
        first_page, cursor = store.read_transcript_page("m1", root=root)

        second = store.append_transcript("m1", "second", k.TRANSCRIPT_SOURCE_SPEECH, root)
        assert second is not None
        next_page, _next_cursor = store.read_transcript_page("m1", cursor, root)

        assert first_page == [first]
        assert next_page == [second]

    def test_capacity_is_rejected_without_truncating_existing_rows(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        first = store.append_transcript("m1", "kept", k.TRANSCRIPT_SOURCE_SPEECH, root)
        assert first is not None
        current_size = store.transcript_path("m1", root).stat().st_size
        monkeypatch.setattr(k, "MAX_TRANSCRIPT_BYTES", current_size + 1)

        assert store.append_transcript("m1", "too large", k.TRANSCRIPT_SOURCE_SPEECH, root) is None
        assert store.read_transcript("m1", root) == [first]

    def test_capacity_accounts_for_the_crash_tail_separator(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        first = store.append_transcript("m1", "kept", k.TRANSCRIPT_SOURCE_SPEECH, root)
        assert first is not None
        path = store.transcript_path("m1", root)
        with path.open("ab") as transcript:
            transcript.write(b"{")
        original = path.read_bytes()
        monkeypatch.setattr(k, "MAX_TRANSCRIPT_BYTES", len(original) + 1)

        assert store.append_transcript("m1", "too large", k.TRANSCRIPT_SOURCE_SPEECH, root) is None
        assert path.read_bytes() == original


class TestAgentOutputs:
    def test_filename_by_widget_type(self):
        assert store.agent_output_filename({"id": "note-taker", "widget_type": "markdown"}) == (
            "note-taker.md"
        )
        assert store.agent_output_filename({"id": "sketch-artist", "widget_type": "html"}) == (
            "sketch-artist.html"
        )
        assert store.agent_output_filename({"id": "chatty", "widget_type": "chat"}) is None
        assert store.agent_output_filename({"id": "custom"}) == "custom.md"

    def test_filename_rejects_bad_agent_id(self):
        with pytest.raises(store.MeetingsPathError):
            store.agent_output_filename({"id": "../evil", "widget_type": "markdown"})

    def test_ensure_creates_seeded_markdown(self, root: Path):
        store.meeting_dir("m", root).mkdir(parents=True)
        created = store.ensure_agent_files(
            "m", [{"id": "note-taker", "widget_type": "markdown"}], "Sprint Standup", root
        )
        assert created == ["note-taker.md"]
        assert store.agent_output_path("m", "note-taker.md", root).read_text() == (
            "# Sprint Standup\n\n"
        )

    def test_ensure_creates_empty_html(self, root: Path):
        store.meeting_dir("m", root).mkdir(parents=True)
        store.ensure_agent_files("m", [{"id": "sketch-artist", "widget_type": "html"}], "T", root)
        assert store.agent_output_path("m", "sketch-artist.html", root).read_text() == ""

    def test_ensure_is_idempotent(self, root: Path):
        store.meeting_dir("m", root).mkdir(parents=True)
        agents = [{"id": "note-taker", "widget_type": "markdown"}]
        store.ensure_agent_files("m", agents, "First", root)
        store.agent_output_path("m", "note-taker.md", root).write_text("# Kept\n\nmine")
        store.ensure_agent_files("m", agents, "Second", root)
        assert "mine" in store.agent_output_path("m", "note-taker.md", root).read_text()

    def test_ensure_skips_bad_agent_without_raising(self, root: Path):
        store.meeting_dir("m", root).mkdir(parents=True)
        created = store.ensure_agent_files("m", [{"id": "../evil"}], "T", root)
        assert created == []

    def test_read_outputs_returns_empty_for_missing(self, root: Path):
        store.meeting_dir("m", root).mkdir(parents=True)
        outputs = store.read_agent_outputs("m", [{"id": "note-taker"}], root)
        assert outputs == {"note-taker": ""}

    def test_read_outputs_reads_content(self, root: Path):
        store.meeting_dir("m", root).mkdir(parents=True)
        store.write_agent_output("m", {"id": "note-taker"}, "# Notes\n\nbody", root)
        assert "body" in store.read_agent_outputs("m", [{"id": "note-taker"}], root)["note-taker"]


class TestTasks:
    def test_missing_returns_empty_doc(self, root: Path):
        doc = store.read_tasks("m", root)
        assert doc["tasks"] == []
        assert doc["meeting_id"] == "m"

    def test_roundtrip(self, root: Path):
        store.write_tasks("m", [{"id": "t1", "description": "ship it"}], root)
        assert store.read_tasks("m", root)["tasks"][0]["id"] == "t1"

    def test_non_list_tasks_normalized(self, root: Path):
        store.write_tasks("m", [], root)
        store.tasks_path("m", root).write_text(json.dumps({"tasks": "oops"}))
        assert store.read_tasks("m", root)["tasks"] == []


class TestCalendarCache:
    def test_roundtrip_and_cap(self, root: Path):
        store.write_calendar_cache([{"event_id": str(i)} for i in range(5)], root)
        assert len(store.read_calendar_cache(root)) == 5

    def test_over_cap_truncated(self, root: Path):
        store.write_calendar_cache(
            [{"event_id": str(i)} for i in range(k.MAX_CALENDAR_EVENTS + 10)], root
        )
        assert len(store.read_calendar_cache(root)) == k.MAX_CALENDAR_EVENTS

    def test_malformed_cache_returns_empty(self, root: Path):
        store.calendar_cache_path(root).write_text("{not a list}")
        assert store.read_calendar_cache(root) == []
