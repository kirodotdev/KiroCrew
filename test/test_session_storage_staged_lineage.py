"""What the session trash tells the chat-image reap: staged stems, their lineage, and gone batches.

``session_storage_parts.staged_lineage``: the empty handler reads which transcripts a
batch stages before the empty, proves each batch is gone afterwards, and reads every
staged transcript's fork edges so a trashed fork keeps protecting its source's copies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from session_storage_helpers import (  # noqa: F401 -- fixtures are requested by name
    _DAY,
    _NOW,
    _archive_segment,
    _cli_half,
    _fresh_scan_cache,
    _index,
    _transcript,
    stores_fixture,
)

from kiro_crew import session_storage


class TestStagedTranscriptStems:
    """Emptying a batch is a permanent delete of its transcripts, whose chat-image
    copies live outside the batch; the empty handler reads the stems first so it
    can reclaim them, and only transcript records count."""

    def test_reports_transcript_stems_per_batch_and_nothing_else(
        self, stores: tuple[Path, Path]
    ) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=10, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=30, age_days=40)
        _archive_segment(crew_home, "dashboard_chat-1", "20260730-211852", size=40, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=10, age_days=40)
        _transcript(crew_home, "slack_1700.42", size=30, age_days=40)
        one = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index({"aaaa1111": "dashboard_chat-1"}), now=_NOW
        )
        two = session_storage.move_to_trash(
            ["bbbb2222"], reason="manual", index=_index({"bbbb2222": "slack_1700.42"}), now=_NOW
        )

        stems = session_storage.staged_transcript_stems([one.batch_id, two.batch_id])

        assert stems == {
            one.batch_id: {"dashboard_chat-1"},
            two.batch_id: {"slack_1700.42"},
        }

    def test_staged_stems_are_bounded_before_they_are_retained(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same population, same policy as `staged_transcript_lineage`: a manifest
        (agent-writable) that names more transcripts than the shared count bound,
        or a name over the key bound, fails the read; the empty job treats that
        as a failed snapshot and reaps nothing."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=10, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=30, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=10, age_days=40)
        _transcript(crew_home, "slack_1700.42", size=30, age_days=40)
        one = session_storage.move_to_trash(
            ["aaaa1111", "bbbb2222"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1", "bbbb2222": "slack_1700.42"}),
            now=_NOW,
        )
        assert session_storage.staged_transcript_stems([one.batch_id]) == {
            one.batch_id: {"dashboard_chat-1", "slack_1700.42"}
        }
        monkeypatch.setattr(session_storage, "MAX_STAGED_LINEAGE_TRANSCRIPTS", 1)
        with pytest.raises(session_storage.SessionStorageError):
            session_storage.staged_transcript_stems([one.batch_id])

    def test_an_unreadable_batch_contributes_no_stems(self, stores: tuple[Path, Path]) -> None:
        assert session_storage.staged_transcript_stems(["20260101-000000-zzzz"]) == {
            "20260101-000000-zzzz": set()
        }

    def test_the_shared_cap_also_bounds_the_batches_retained(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each named batch is a retained entry even when it holds no stems (an
        empty or unreadable batch), so a request naming more batches than the
        shared cap is refused rather than growing one entry per batch while the
        stem count stays at zero."""
        monkeypatch.setattr(session_storage, "MAX_STAGED_LINEAGE_TRANSCRIPTS", 2)
        ids = [f"20260101-000000-zzz{c}" for c in "abc"]
        assert session_storage.staged_transcript_stems(ids[:2]) == {i: set() for i in ids[:2]}
        assert session_storage.staged_transcript_stems(ids[:2] + ids[:1]) == {
            i: set() for i in ids[:2]
        }
        with pytest.raises(session_storage.SessionStorageError, match="batches named"):
            session_storage.staged_transcript_stems(ids)

    def test_the_manifest_is_streamed_and_a_partial_listing_is_discarded_whole(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The manifest is O(sessions) in an agent-writable tree, so neither
        staged reader materializes its entries list: both consume
        `_stream_manifest_entries`, which yields one record at a time and raises
        `_UnreadableManifest` with the same guards `_read_manifest` applies. A
        record over the cap AFTER valid entries means the listing already seen is
        partial: the stem reader drops it to the empty set (never a subset), and
        the lineage reader refuses the batch outright."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=10, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=30, age_days=40)
        one = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index({"aaaa1111": "dashboard_chat-1"}), now=_NOW
        )
        batch_dir = session_storage.trash_root() / one.batch_id
        manifest = batch_dir / session_storage.MANIFEST_NAME
        good = manifest.read_text()
        header, *entries = good.splitlines()
        assert len(entries) == 1

        # Streamed: the generator holds no list, yields the one entry, then ends.
        stream = session_storage._stream_manifest_entries(batch_dir)
        assert [e["uid"] for e in stream] == ["aaaa1111"]

        # A record over the cap after the valid entry: unreadable as a whole.
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 64)
        manifest.write_text(good + json.dumps({"uid": "x" * 200, "files": []}) + "\n")
        with pytest.raises(session_storage._UnreadableManifest):
            list(session_storage._stream_manifest_entries(batch_dir))
        assert session_storage.staged_transcript_stems([one.batch_id]) == {one.batch_id: set()}
        with pytest.raises(session_storage.SessionStorageError, match="no readable manifest"):
            session_storage.staged_transcript_lineage()

        # A wrong schema header is refused before anything is yielded.
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 8 * 1024 * 1024)
        manifest.write_text(json.dumps({"schema": "not-this"}) + "\n" + "\n".join(entries) + "\n")
        with pytest.raises(session_storage._UnreadableManifest):
            next(session_storage._stream_manifest_entries(batch_dir))
        assert session_storage.staged_transcript_stems([one.batch_id]) == {one.batch_id: set()}

        manifest.write_text(good)
        assert session_storage.staged_transcript_stems([one.batch_id]) == {
            one.batch_id: {"dashboard_chat-1"}
        }

    def test_all_staged_stems_union_every_batch_and_an_empty_trash_is_empty(
        self, stores: tuple[Path, Path]
    ) -> None:
        crew_home, kiro_home = stores
        assert set(session_storage.staged_transcript_lineage()) == set()
        _cli_half(kiro_home, "aaaa1111", log_bytes=10, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=30, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=10, age_days=40)
        _transcript(crew_home, "slack_1700.42", size=30, age_days=40)
        session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index({"aaaa1111": "dashboard_chat-1"}), now=_NOW
        )
        session_storage.move_to_trash(
            ["bbbb2222"], reason="manual", index=_index({"bbbb2222": "slack_1700.42"}), now=_NOW
        )
        assert set(session_storage.staged_transcript_lineage()) == {
            "dashboard_chat-1",
            "slack_1700.42",
        }

    def test_staged_lineage_reads_each_transcripts_fork_edges(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A trashed fork's `forked_from` lives only in the staged file; the reap
        needs it to keep protecting the fork's live source. Three shapes: a
        metadata line with edges, a first line that is a message (no metadata,
        readable, no edges), and junk (unreadable, fails closed)."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=10, age_days=40)
        _transcript(crew_home, "dashboard_fork", size=1, age_days=40)
        meta = {
            "_type": "metadata",
            "forked_from": "dashboard:src",
            "fork_ancestors": ["dashboard:src"],
            "title": "ignored",
        }
        path = crew_home / "sessions" / "dashboard_fork.jsonl"
        path.write_text(json.dumps(meta) + "\n" + json.dumps({"role": "user"}) + "\n")
        os.utime(path, (_NOW - 40 * _DAY, _NOW - 40 * _DAY))
        _cli_half(kiro_home, "bbbb2222", log_bytes=10, age_days=40)
        _transcript(crew_home, "dashboard_plain", size=1, age_days=40)
        plain = crew_home / "sessions" / "dashboard_plain.jsonl"
        plain.write_text(json.dumps({"role": "user", "content": "hi"}) + "\n")
        os.utime(plain, (_NOW - 40 * _DAY, _NOW - 40 * _DAY))
        _cli_half(kiro_home, "cccc3333", log_bytes=10, age_days=40)
        _transcript(crew_home, "dashboard_junk", size=1, age_days=40)
        junk = crew_home / "sessions" / "dashboard_junk.jsonl"
        junk.write_bytes(b"\xff\xfe not json\n")
        os.utime(junk, (_NOW - 40 * _DAY, _NOW - 40 * _DAY))
        for uid, stem in (
            ("aaaa1111", "dashboard_fork"),
            ("bbbb2222", "dashboard_plain"),
            ("cccc3333", "dashboard_junk"),
        ):
            session_storage.move_to_trash(
                [uid], reason="manual", index=_index({uid: stem}), now=_NOW
            )

        lineage = session_storage.staged_transcript_lineage()

        assert lineage == {
            "dashboard_fork": {"forked_from": "dashboard:src", "fork_ancestors": ["dashboard:src"]},
            "dashboard_plain": {},
            "dashboard_junk": None,
        }
        assert set(session_storage.staged_transcript_lineage()) == set(lineage)

    def test_staged_lineage_reads_through_the_link_refusing_chokepoint(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The trash is agent-writable. A staged transcript name swapped for a
        symlink, or for a hardlink to a file outside the batch, is refused by the
        READ itself (`hooks.safe_read_file_bytes_nolink`, pinned to the batch root
        and to `_META_LINE_CAP` bytes), so the record reads as unreadable (`None`)
        and the reap keeps everything. A regular staged file still reads."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=10, age_days=40)
        _transcript(crew_home, "dashboard_fork", size=1, age_days=40)
        meta = {"_type": "metadata", "forked_from": "dashboard:src"}
        path = crew_home / "sessions" / "dashboard_fork.jsonl"
        path.write_text(json.dumps(meta) + "\n")
        os.utime(path, (_NOW - 40 * _DAY, _NOW - 40 * _DAY))
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index({"aaaa1111": "dashboard_fork"}), now=_NOW
        )
        staged = (
            session_storage.trash_root()
            / batch.batch_id
            / session_storage.STAGE_CREW_LEAF
            / "dashboard_fork.jsonl"
        )
        assert staged.is_file()
        assert session_storage.staged_transcript_lineage() == {
            "dashboard_fork": {"forked_from": "dashboard:src"}
        }

        outside = crew_home / "outside.jsonl"
        outside.write_text(
            json.dumps({"_type": "metadata", "forked_from": "dashboard:evil"}) + "\n"
        )
        original = staged.read_bytes()
        staged.unlink()
        staged.symlink_to(outside)
        assert session_storage.staged_transcript_lineage() == {"dashboard_fork": None}

        staged.unlink()
        os.link(outside, staged)
        assert session_storage.staged_transcript_lineage() == {"dashboard_fork": None}

        staged.unlink()
        staged.write_bytes(original)
        assert session_storage.staged_transcript_lineage() == {
            "dashboard_fork": {"forked_from": "dashboard:src"}
        }

    def test_staged_lineage_bounds_what_it_retains(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The trash is agent-writable, so the lineage retained from it is bounded
        at the point of retention, not where it is later walked: an over-long or
        non-string `forked_from`, or an over-bounds chain, reads as unreadable;
        a manifest staging more transcripts than the count bound fails the whole
        read (the reap keeps everything)."""
        from kiro_crew.dashboard import fork_lineage

        crew_home, kiro_home = stores
        shapes = {
            "dashboard_longparent": {
                "_type": "metadata",
                "forked_from": "x" * (fork_lineage.MAX_ANCESTOR_KEY_CHARS + 1),
            },
            "dashboard_nested": {"_type": "metadata", "forked_from": {"deep": ["x"] * 64}},
            "dashboard_bigchain": {
                "_type": "metadata",
                "fork_ancestors": ["dashboard:a"] * fork_lineage.MAX_FORK_ANCESTORS,
            },
            "dashboard_fine": {
                "_type": "metadata",
                "forked_from": "dashboard:src",
                "fork_ancestors": ["dashboard:src"],
                "title": "kept out of the lineage",
            },
        }
        for i, (stem, meta) in enumerate(shapes.items()):
            uid = f"{i:04d}aaaa"
            _cli_half(kiro_home, uid, log_bytes=10, age_days=40)
            _transcript(crew_home, stem, size=1, age_days=40)
            path = crew_home / "sessions" / f"{stem}.jsonl"
            path.write_text(json.dumps(meta) + "\n")
            os.utime(path, (_NOW - 40 * _DAY, _NOW - 40 * _DAY))
            session_storage.move_to_trash(
                [uid], reason="manual", index=_index({uid: stem}), now=_NOW
            )
        assert session_storage.staged_transcript_lineage() == {
            "dashboard_longparent": None,
            "dashboard_nested": None,
            "dashboard_bigchain": None,
            "dashboard_fine": {"forked_from": "dashboard:src", "fork_ancestors": ["dashboard:src"]},
        }
        monkeypatch.setattr(session_storage, "MAX_STAGED_LINEAGE_TRANSCRIPTS", 3)
        with pytest.raises(session_storage.SessionStorageError):
            session_storage.staged_transcript_lineage()

    def test_staged_lineage_treats_conflicting_duplicate_stems_as_unreadable(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A stem can be staged by more than one batch: a stable-stem session is
        trashed, recreated, forked from a different source and trashed again, or
        an agent-written manifest names a stem another batch holds. Two records
        that AGREE keep their lineage; two that DIFFER read as unreadable
        (`None`), and the conflict is sticky, so a later agreeing record cannot
        steer which source images the reap keeps."""
        crew_home, kiro_home = stores

        def trash(uid: str, meta: dict[str, object] | None) -> None:
            _cli_half(kiro_home, uid, log_bytes=10, age_days=40)
            _transcript(crew_home, "dashboard_fork", size=1, age_days=40)
            if meta is not None:
                path = crew_home / "sessions" / "dashboard_fork.jsonl"
                path.write_text(json.dumps(meta) + "\n")
                os.utime(path, (_NOW - 40 * _DAY, _NOW - 40 * _DAY))
            session_storage.move_to_trash(
                [uid], reason="manual", index=_index({uid: "dashboard_fork"}), now=_NOW
            )

        same = {"_type": "metadata", "forked_from": "dashboard:src"}
        trash("aaaa1111", same)
        trash("aaaa2222", same)
        assert session_storage.staged_transcript_lineage() == {
            "dashboard_fork": {"forked_from": "dashboard:src"}
        }

        trash("aaaa3333", {"_type": "metadata", "forked_from": "dashboard:other"})
        assert session_storage.staged_transcript_lineage() == {"dashboard_fork": None}

        # A fourth record agreeing with the first two does not clear the conflict.
        trash("aaaa4444", same)
        assert session_storage.staged_transcript_lineage() == {"dashboard_fork": None}

    def test_staged_lineage_fails_closed_on_a_batch_it_cannot_inspect(
        self, stores: tuple[Path, Path]
    ) -> None:
        """`list_trash` OMITS a batch without a readable manifest (it cannot be
        offered for restore); the lineage reader must not, because the omitted
        batch could hold the transcript whose images the reap is about to
        destroy. Three uninspectable shapes each raise: a batch directory whose
        manifest is missing, a link planted where a batch should be, and a
        manifest that cannot be read. A missing trash root reads as empty."""
        crew_home, kiro_home = stores
        root = session_storage.trash_root()
        assert session_storage.staged_transcript_lineage() == {}  # no trash yet
        _cli_half(kiro_home, "aaaa1111", log_bytes=10, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=30, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index({"aaaa1111": "dashboard_chat-1"}), now=_NOW
        )
        assert set(session_storage.staged_transcript_lineage()) == {"dashboard_chat-1"}

        orphan = root / "20260101-000000-zzzz"
        orphan.mkdir()
        with pytest.raises(session_storage.SessionStorageError):
            session_storage.staged_transcript_lineage()
        orphan.rmdir()
        assert set(session_storage.staged_transcript_lineage()) == {"dashboard_chat-1"}

        link = root / "20260101-000000-yyyy"
        link.symlink_to(root / batch.batch_id)
        with pytest.raises(session_storage.SessionStorageError):
            session_storage.staged_transcript_lineage()
        link.unlink()

        manifest = root / batch.batch_id / session_storage.MANIFEST_NAME
        manifest.write_text("{not json\n")
        with pytest.raises(session_storage.SessionStorageError):
            session_storage.staged_transcript_lineage()


class TestBatchIsGone:
    """Positive absence for the post-empty image reap."""

    def test_only_a_missing_directory_reads_as_gone(self, stores: tuple[Path, Path]) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=10, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=30, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index({"aaaa1111": "dashboard_chat-1"}), now=_NOW
        )
        assert session_storage.batch_is_gone(batch.batch_id) is False
        assert session_storage.batch_is_gone("20260101-000000-zzzz") is True
        # An id the resolver refuses is not proof of anything.
        assert session_storage.batch_is_gone("../escape") is False
        # A link planted where the batch was is not "gone" either.
        real = session_storage.trash_root() / batch.batch_id
        moved = real.with_name(batch.batch_id + "-moved")
        real.rename(moved)
        real.symlink_to(moved)
        assert session_storage.batch_is_gone(batch.batch_id) is False
        real.unlink()
        assert session_storage.batch_is_gone(batch.batch_id) is True
