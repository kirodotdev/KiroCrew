"""Tests for session storage measurement and the trash/restore cycle.

Both stores are addressed through their real resolvers by pointing
``KIROCREW_HOME`` and ``KIRO_HOME`` at temp directories, rather than patching the
module's bound names, so the tests exercise the same path resolution the product
uses and a change to where either store lives fails here.

The invariant these tests exist to protect: a session's halves are always
reclaimed and restored TOGETHER. Half a session is worse than either extreme — it
either lists without resuming, or resumes without history.

This file keeps the trash location, the single manifest read per payload, the
manifest readers and the session lock held across the index purge. The other topics
live in ``test_session_storage_measure.py``, ``_reclaim``, ``_restore``, ``_cotenants``,
``_log_safety``, ``_empty_trash`` and ``_empty_trash_removal``, and the fixtures and
builders they share in ``session_storage_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from session_storage_helpers import (  # noqa: F401 -- fixtures are requested by name
    _NOW,
    _cli_half,
    _fresh_scan_cache,
    _index,
    _transcript,
    stores_fixture,
)

from kiro_crew import session_storage
from kiro_crew.session_storage import SessionStorageError


class TestTrashLocation:
    def test_trash_lives_under_the_data_home(self, stores: tuple[Path, Path]) -> None:
        crew_home, _ = stores
        assert session_storage.trash_root() == crew_home / "trash" / "sessions"

    def test_same_filesystem_is_reported_for_a_default_layout(
        self, stores: tuple[Path, Path]
    ) -> None:
        report = session_storage.measure(_index(), now=_NOW)
        assert report.trash_same_filesystem is True


class TestSingleTrashPass:
    """Each payload build reads each trash manifest exactly once.

    ``list_trash`` is uncached and reads every batch manifest per call, so a
    payload builder that calls it once for the totals (inside ``measure``) and
    again for the wire array pays double I/O — and could ship totals that
    contradict the array beside them if a stage/empty lands between the calls.
    Both builders thread one list through ``measure(batches=...)`` instead,
    mirroring the ``units=`` parameter and the counting test in
    ``test_session_storage_measure.py``.
    """

    @staticmethod
    def _two_batches(kiro_home: Path) -> None:
        """Two batches with DISTINCT created_at stamps, so an ordering assertion
        on the payload array is falsifiable rather than satisfied by any order."""
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=64, age_days=40)
        session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW - 60)
        session_storage.move_to_trash(["bbbb2222"], reason="manual", index=_index(), now=_NOW)

    @staticmethod
    def _counted_manifest_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        """Count via ``_summarize_manifest``: it is ``list_trash``'s
        per-batch cost (the count-only streamed pass), and it is hit through the
        module global, so it sees every ``list_trash`` pass no matter which
        module's imported name made the call."""
        reads: list[Path] = []
        real = session_storage._summarize_manifest

        def counted(batch: Path):
            reads.append(batch)
            return real(batch)

        monkeypatch.setattr(session_storage, "_summarize_manifest", counted)
        return reads

    def test_report_payload_reads_each_manifest_exactly_once(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers import session_storage as handler

        _, kiro_home = stores
        self._two_batches(kiro_home)
        reads = self._counted_manifest_reads(monkeypatch)

        payload = handler._report_payload()

        assert len(reads) == 2, "one payload build must read each batch manifest once"
        assert len(set(reads)) == 2
        # Newest first is list_trash's own contract; asserting it on the wire
        # array proves the hoisted list reaches the payload unreordered. The
        # helper gives the batches distinct stamps so this can actually fail.
        assert [b["created_at"] for b in payload["trash"]["batches"]] == [_NOW, _NOW - 60]
        assert len(payload["trash"]["batches"]) == 2
        assert payload["trash"]["bytes"] == sum(b["bytes"] for b in payload["trash"]["batches"])

    def test_inventory_payload_reads_each_manifest_exactly_once(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers import session_storage as handler

        _, kiro_home = stores
        self._two_batches(kiro_home)
        reads = self._counted_manifest_reads(monkeypatch)

        # Explicit empty stubs, not a MagicMock: a bare mock answers
        # running_session_keys() and list_sessions() via magic-method defaults,
        # which silently weakens the test if either callee grows a real check.
        state = SimpleNamespace(
            running_session_keys=lambda: frozenset(),
            conversation_log=SimpleNamespace(list_sessions=lambda: []),
        )
        payload = handler._inventory_payload(state)

        assert len(reads) == 2, "one payload build must read each batch manifest once"
        assert len(set(reads)) == 2
        assert len(payload["trash"]["batches"]) == 2
        assert payload["trash"]["bytes"] == sum(b["bytes"] for b in payload["trash"]["batches"])

    def test_measure_without_batches_still_enumerates_the_trash(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default path is load-bearing: every existing ``measure(index)``
        caller relies on it enumerating the trash itself."""
        _, kiro_home = stores
        self._two_batches(kiro_home)
        reads = self._counted_manifest_reads(monkeypatch)

        report = session_storage.measure(_index(), now=_NOW)

        assert report.trash_batches == 2
        assert report.trash_bytes > 0
        assert len(reads) == 2, "measure() with no batches= must do its own pass"

    def test_measure_uses_the_handed_over_batches_without_a_second_pass(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, kiro_home = stores
        self._two_batches(kiro_home)
        batches = session_storage.list_trash()
        reads = self._counted_manifest_reads(monkeypatch)

        report = session_storage.measure(_index(), now=_NOW, batches=batches)

        assert report.trash_batches == 2
        assert report.trash_bytes == sum(b.bytes for b in batches)
        assert reads == [], "a handed-over list must not trigger another manifest pass"


class TestManifestReaders:
    """`_read_manifest` streams; `_summarize_manifest` aggregates without a list.

    The core invariant: on every manifest the summary's (header, sessions, bytes)
    are byte-identical to what the full read derives — same skipped-line
    tolerance, same schema rejection, same ``None`` on an unreadable file.
    """

    _BATCH_ID = "20240101T000000-cafe0001"

    def _batch(self, tmp_path: Path, lines: list[str], *, terminated: bool = True) -> Path:
        batch = tmp_path / self._BATCH_ID
        batch.mkdir(parents=True, exist_ok=True)
        (batch / session_storage.MANIFEST_NAME).write_text(
            "\n".join(lines) + ("\n" if terminated else ""), encoding="utf-8"
        )
        return batch

    def _header(self, **overrides: object) -> dict[str, object]:
        header: dict[str, object] = {
            "schema": session_storage.MANIFEST_SCHEMA,
            "batch_id": self._BATCH_ID,
            "created_at": _NOW,
            "reason": "manual",
        }
        header.update(overrides)
        return header

    def _entry(self, uid: str, sizes: list[int]) -> dict[str, object]:
        return {
            "uid": uid,
            "files": [
                {"rel": f"cli/{uid}-{i}.jsonl", "origin": f"/x/{uid}-{i}", "bytes": size}
                for i, size in enumerate(sizes)
            ],
        }

    def test_summary_is_byte_identical_to_the_full_read(self, tmp_path: Path) -> None:
        """Header, count and byte total agree with the entry list on a manifest
        that exercises every tolerated irregularity at once: blank lines, a
        malformed line, a non-dict line, and a truncated final line."""
        entries = [
            self._entry("aaaa1111", [10, 20]),
            self._entry("bbbb2222", [5]),
            self._entry("cccc3333", []),
        ]
        lines = [json.dumps(self._header())]
        lines.append("")
        lines.append(json.dumps(entries[0]))
        lines.append("not json at all {")
        lines.append(json.dumps(entries[1]))
        lines.append(json.dumps([1, 2, 3]))
        lines.append(json.dumps(entries[2]))
        lines.append('{"uid": "dddd444')  # crash mid-append: NO trailing newline
        batch = self._batch(tmp_path, lines, terminated=False)

        parsed = session_storage._read_manifest(batch)
        summary = session_storage._summarize_manifest(batch)

        assert parsed is not None and summary is not None
        header, read_entries = parsed
        assert summary == (
            header,
            len(read_entries),
            sum(session_storage._entry_bytes(e) for e in read_entries),
        )
        # Pin the absolute values too, so both readers cannot be wrong together.
        assert summary[1] == 3
        assert summary[2] == 35

    def test_header_only_manifest_summarizes_to_zero(self, tmp_path: Path) -> None:
        batch = self._batch(tmp_path, [json.dumps(self._header())])
        summary = session_storage._summarize_manifest(batch)
        assert summary is not None
        assert (summary[1], summary[2]) == (0, 0)

    def test_wrong_schema_is_rejected_by_both_readers(self, tmp_path: Path) -> None:
        lines = [json.dumps(self._header(schema=99)), json.dumps(self._entry("aaaa1111", [8]))]
        batch = self._batch(tmp_path, lines)
        assert session_storage._read_manifest(batch) is None
        assert session_storage._summarize_manifest(batch) is None

    def test_unreadable_manifest_returns_none_from_both_readers(self, tmp_path: Path) -> None:
        batch = tmp_path / self._BATCH_ID
        # A directory where the file should be: open() raises an OSError subclass,
        # the same contract read_text() had.
        (batch / session_storage.MANIFEST_NAME).mkdir(parents=True)
        assert session_storage._read_manifest(batch) is None
        assert session_storage._summarize_manifest(batch) is None

    def test_batch_id_disagreement_is_tolerated_by_the_reader(self, tmp_path: Path) -> None:
        """The reader returns the header untouched; refusing a disagreeing batch id
        is list_trash's decision, exercised by TestTrashIsolation."""
        lines = [json.dumps(self._header(batch_id="somebody-else"))]
        batch = self._batch(tmp_path, lines)
        summary = session_storage._summarize_manifest(batch)
        assert summary is not None
        assert summary[0]["batch_id"] == "somebody-else"

    def test_corrupt_lines_are_counted_and_logged_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Mid-file corruption is not silent — one aggregated
        warning per read, counting only genuinely corrupt lines."""
        lines = [
            json.dumps(self._header()),
            "garbage {",
            json.dumps(self._entry("aaaa1111", [4])),
            "more garbage {",
        ]
        batch = self._batch(tmp_path, lines)
        with caplog.at_level("WARNING", logger="kiro_crew.session_storage"):
            summary = session_storage._summarize_manifest(batch)
        assert summary is not None and summary[1] == 1
        warnings = [r for r in caplog.records if "unparseable" in r.getMessage()]
        assert len(warnings) == 1
        assert "skipped 2 unparseable" in warnings[0].getMessage()

    def test_a_trailing_partial_line_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A crash mid-append is EXPECTED and tolerated; warning for it on every
        storage-screen open would be recurring noise about a normal state."""
        lines = [
            json.dumps(self._header()),
            json.dumps(self._entry("aaaa1111", [4])),
            '{"uid": "trunc',
        ]
        batch = self._batch(tmp_path, lines, terminated=False)
        with caplog.at_level("WARNING", logger="kiro_crew.session_storage"):
            summary = session_storage._summarize_manifest(batch)
        assert summary is not None and summary[1] == 1
        assert not [r for r in caplog.records if "unparseable" in r.getMessage()]

    def test_unicode_line_boundaries_split_records_like_splitlines_did(
        self, tmp_path: Path
    ) -> None:
        """The old reader split on str.splitlines boundaries (\\u2028, \\x1c, ...).
        Two records separated by one must still parse as two records, not be
        rejected as one malformed line."""
        entry = self._entry("aaaa1111", [7])
        blob = (
            json.dumps(self._header())
            + "\u2028"
            + json.dumps(entry)
            + "\x1c"
            + json.dumps(self._entry("bbbb2222", [3]))
        )
        batch = self._batch(tmp_path, [blob])
        summary = session_storage._summarize_manifest(batch)
        assert summary is not None
        assert (summary[1], summary[2]) == (2, 10)

    def test_an_oversized_record_aborts_the_batch_not_materialised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The manifest lives in an agent-writable tree: one multi-GB line with no
        newline must not be read whole. Past the cap the batch is treated as
        having no readable manifest — a crafted tail sitting exactly past a
        cap boundary cannot be smuggled in as its own forged record."""
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 256)
        forged_tail = json.dumps(
            {"uid": "evil0000", "files": [{"rel": "r", "origin": "o", "bytes": 999}]}
        )
        # 256 filler chars put the forged record exactly after the first
        # cap-sized read of this single (newline-free) oversized line.
        giant = "x" * 256 + forged_tail
        lines = [
            json.dumps(self._header()),
            giant,
            json.dumps(self._entry("aaaa1111", [5])),
        ]
        batch = self._batch(tmp_path, lines)
        assert session_storage._summarize_manifest(batch) is None
        assert session_storage._read_manifest(batch) is None

    def test_undecodable_bytes_still_propagate(self, tmp_path: Path) -> None:
        """read_text raised UnicodeDecodeError on corrupt bytes and the streaming
        readers must keep that contract rather than silently absorbing it."""
        batch = tmp_path / self._BATCH_ID
        batch.mkdir(parents=True)
        (batch / session_storage.MANIFEST_NAME).write_bytes(b'{"schema": 1}\n\xff\xfe garbage\n')
        with pytest.raises(UnicodeDecodeError):
            session_storage._read_manifest(batch)
        with pytest.raises(UnicodeDecodeError):
            session_storage._summarize_manifest(batch)

    def test_an_oserror_mid_iteration_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """read_text failed as one whole-file operation; the streaming readers must
        map a read error DURING iteration to the same None, not leak it."""
        batch = self._batch(tmp_path, [json.dumps(self._header())])

        class _FailsMidRead:
            def __init__(self) -> None:
                self.calls = 0

            def readline(self, _cap: int) -> str:
                self.calls += 1
                if self.calls == 1:
                    return json.dumps({"schema": session_storage.MANIFEST_SCHEMA}) + "\n"
                raise OSError("device gone")

            def __enter__(self) -> "_FailsMidRead":
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(Path, "open", lambda *a, **k: _FailsMidRead())
        assert session_storage._read_manifest(batch) is None
        assert session_storage._summarize_manifest(batch) is None

    def test_list_trash_never_materialises_the_entry_list(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The memory fix itself: the listing must go through the count-only
        reader. Reverting it to _read_manifest would pass every aggregate check,
        so pin the path by making the full reader unreachable from list_trash."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)

        def _forbidden(batch: Path) -> None:
            raise AssertionError("list_trash must not materialise manifest entries")

        monkeypatch.setattr(session_storage, "_read_manifest", _forbidden)
        listed = session_storage.list_trash()
        assert len(listed) == 1 and listed[0].sessions == 1

    def test_a_cap_boundary_read_does_not_destroy_a_record_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """a cap-sized read that cuts a physical line
        mid-way must not condemn the bounded, individually valid records inside
        it. A 255-char record + \\u2028 + another record on one physical line,
        with the cap at 256, must yield both records."""
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 256)
        pad = "p" * (255 - len('{"uid": "aaaa1111", "files": [], "": ""}'))
        first = '{"uid": "aaaa1111", "files": [], "' + pad + '": ""}'
        assert len(first) == 255
        second = json.dumps(self._entry("bbbb2222", [9]))
        lines = [json.dumps(self._header()), first + "\u2028" + second]
        batch = self._batch(tmp_path, lines)
        summary = session_storage._summarize_manifest(batch)
        assert summary is not None
        assert (summary[1], summary[2]) == (2, 9)

    def test_a_final_line_ended_by_a_unicode_boundary_is_complete_corruption(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """'garbage\\u2028' at EOF IS terminated under
        splitlines semantics — a complete corrupt record that must warn, not a
        crash-partial that hides at debug."""
        blob = json.dumps(self._header()) + "\n" + "garbage {\u2028"
        batch = tmp_path / self._BATCH_ID
        batch.mkdir(parents=True, exist_ok=True)
        (batch / session_storage.MANIFEST_NAME).write_text(blob, encoding="utf-8")
        with caplog.at_level("WARNING", logger="kiro_crew.session_storage"):
            summary = session_storage._summarize_manifest(batch)
        assert summary is not None and summary[1] == 0
        assert [r for r in caplog.records if "unparseable" in r.getMessage()]

    def test_a_record_spanning_many_cap_reads_aborts_without_materialising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A record several multiples of the cap long aborts the read (the batch
        is unreadable) without its bytes ever being concatenated whole."""
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 256)
        lines = [
            json.dumps(self._header()),
            '{"uid": "' + "x" * 1500 + '"}',
            json.dumps(self._entry("aaaa1111", [5])),
        ]
        batch = self._batch(tmp_path, lines)
        assert session_storage._summarize_manifest(batch) is None
        assert session_storage._read_manifest(batch) is None

    def test_even_a_valid_record_longer_than_the_cap_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap is a contract on parsed record size: a syntactically VALID
        record between cap and 2x cap (reachable because the carry-over buffer
        admits up to two cap-sized reads) must abort the read, NOT be silently
        skipped — restore() rewrites the manifest from parsed records, so a
        skipped record's staged files would be orphaned."""
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 256)
        valid_oversized = '{"uid": "bbbb2222", "pad": "' + "y" * 380 + '", "files": []}'
        assert 256 < len(valid_oversized) < 512
        lines = [
            json.dumps(self._header()),
            valid_oversized,
            json.dumps(self._entry("aaaa1111", [5])),
        ]
        batch = self._batch(tmp_path, lines)
        assert session_storage._summarize_manifest(batch) is None
        assert session_storage._read_manifest(batch) is None

    def test_an_oversized_record_makes_restore_refuse_not_orphan(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The blocked data-loss seam, end to end: a manifest with an oversized
        record must make restore() refuse loudly. Skipping the record instead
        would let a partial restore REWRITE the manifest without it, permanently
        orphaning its staged files. The manifest must also be left untouched."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111", "bbbb2222"], reason="manual", index=_index(), now=_NOW
        )
        manifest = session_storage.trash_root() / batch.batch_id / session_storage.MANIFEST_NAME
        lines = manifest.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        inflated_uid = entry["uid"]
        intact_uid = "bbbb2222" if inflated_uid == "aaaa1111" else "aaaa1111"
        entry["pad"] = "y" * 600
        lines[1] = json.dumps(entry)
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        before = manifest.read_bytes()
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 256)

        # Restoring the INTACT session is the dangerous path: with the oversized
        # record merely skipped, this would succeed and rewrite the manifest from
        # the parsed records only — erasing the skipped record's metadata.
        with pytest.raises(SessionStorageError):
            session_storage.restore(batch.batch_id, [intact_uid])

        assert manifest.read_bytes() == before

    def test_an_unterminated_oversized_record_at_eof_still_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An over-cap record with NO final newline must abort like any other
        oversized record, not degrade into a silently dropped 'trailing partial'
        — silent dropping is the exact shape that lets a restore rewrite orphan
        staged files."""
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 256)
        valid_oversized = '{"uid": "bbbb2222", "pad": "' + "y" * 560 + '", "files": []}'
        lines = [json.dumps(self._header()), valid_oversized]
        batch = self._batch(tmp_path, lines, terminated=False)
        assert session_storage._summarize_manifest(batch) is None
        assert session_storage._read_manifest(batch) is None

    def test_an_oversized_sibling_aborts_even_across_a_unicode_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'x'*cap + \\u2028 + valid record on one physical line: the oversized
        piece aborts the whole batch (fail-closed) — the record past the boundary
        is NOT salvaged, because salvaging some records while dropping others is
        exactly the shape that lets a partial restore orphan staged files."""
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 256)
        lines = [
            json.dumps(self._header()),
            "x" * 300 + "\u2028" + json.dumps(self._entry("aaaa1111", [6])),
        ]
        batch = self._batch(tmp_path, lines)
        assert session_storage._summarize_manifest(batch) is None
        assert session_storage._read_manifest(batch) is None

    def test_list_trash_aggregates_match_the_staged_batch(self, stores: tuple[Path, Path]) -> None:
        """End-to-end proof that the count-only path reports the same numbers the
        entry-list path did: the listing agrees with the batch returned by the move."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=100, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=300, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111", "bbbb2222"], reason="manual", index=_index(), now=_NOW
        )

        listed = session_storage.list_trash()

        assert len(listed) == 1
        assert listed[0].sessions == batch.sessions == 2
        assert listed[0].bytes == batch.bytes
        assert listed[0].bytes > 0


class TestReclaimHoldsTheSessionLockAcrossTheIndexPurge:
    """The search index's copy of a session's text must not come back after the move.

    The index keeps that copy; the background indexer rebuilds a row from any
    transcript still in place. A purge that merely PRECEDED the move therefore left a
    window -- the indexer's write lands after it, and the text is in the index once
    the files are gone. Both sides take ``ConversationLog._locked``, so the invariant
    is that the purge runs while the reclaim holds that lock.
    """

    def test_the_purge_runs_while_the_unit_lock_is_held(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        from kiro_crew.history import ConversationLog

        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=2048, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=300, age_days=40)

        exclusive: list[bool] = []
        real_purge = session_storage._purge_search_index

        def probing_purge(stems: list[str]) -> bool:
            # Reentrant for the holding thread, so probe from another one.
            lock_path = str(crew_home / "sessions" / f"{stems[0]}.jsonl")
            lock = ConversationLog._file_locks.get(lock_path)
            if lock is None:
                exclusive.append(False)
                return real_purge(stems)

            def probe() -> None:
                got = lock.acquire(blocking=False)
                exclusive.append(not got)
                if got:
                    lock.release()

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=10)
            return real_purge(stems)

        monkeypatch.setattr(session_storage, "_purge_search_index", probing_purge)

        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        assert batch.sessions == 1
        assert exclusive == [True], "the purge ran without the session lock held"

    def test_a_refused_purge_leaves_the_session_in_place(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=2048, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=300, age_days=40)
        monkeypatch.setattr(session_storage, "_purge_search_index", lambda stems: False)

        with pytest.raises(session_storage.SessionStorageError):
            session_storage.move_to_trash(
                ["aaaa1111"],
                reason="manual",
                index=_index({"aaaa1111": "dashboard_chat-1"}),
                now=_NOW,
            )

        assert (crew_home / "sessions" / "dashboard_chat-1.jsonl").is_file()
