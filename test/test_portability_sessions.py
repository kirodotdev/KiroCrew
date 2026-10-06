"""Whole-install export with chat history (``create_export_zip(include_sessions=True)``)."""

from __future__ import annotations

import errno
import io
import json
import os
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from kiro_crew import platform_compat, portability
from kiro_crew.dashboard.handlers._shared import _read_memory_mode
from kiro_crew.history import (
    TRANSCRIPT_HEADER_MAX_BYTES,
    ConversationLog,
    memory_mode_from_header_line,
)
from kiro_crew.portability import (
    _read_first_line_fd,
    _transcript_header_verdict,
    apply_import_zip,
    create_export_zip,
)


def _header(**fields) -> str:
    return json.dumps({"_type": "metadata", "title": "chat", **fields})


def _row(text: str) -> str:
    return json.dumps({"role": "user", "content": text, "ts": "2026-01-01T00:00:00"})


def _write_chat(sessions: Path, stem: str, header: str | None, text: str = "hello") -> Path:
    lines = ([header] if header is not None else []) + [_row(text)]
    path = sessions / f"{stem}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def _write_segment(sessions: Path, stem: str, stamp: str, reason: str, text: str) -> Path:
    archive = sessions / "archive"
    archive.mkdir(exist_ok=True)
    path = archive / f"{stem}__{stamp}.jsonl"
    path.write_text(json.dumps({"_type": "archive", "reason": reason}) + "\n" + _row(text) + "\n")
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A data home whose ``sessions/`` holds one chat of every kind the export judges."""
    mc = tmp_path / "source"
    sessions = mc / "sessions"
    sessions.mkdir(parents=True)
    (mc / "config.json").write_text("{}")

    _write_chat(sessions, "dashboard_kept", _header(memory_mode="persistent"), "kept chat")
    _write_chat(sessions, "dashboard_legacy", None, "no header at all")
    _write_chat(sessions, "dashboard_upper", _header(memory_mode="Persistent"))
    _write_chat(sessions, "dashboard_incog", _header(memory_mode="incognito"), "private")
    _write_chat(sessions, "dashboard_temp", _header(memory_mode="temporary"), "private")
    _write_chat(sessions, "dashboard_unknown", _header(memory_mode="something-new"))
    (sessions / "dashboard_corrupt.jsonl").write_text("{not json\n")

    # Companions of a kept chat and of a withheld one.
    (sessions / ".threads").mkdir()
    (sessions / ".threads" / "dashboard_kept.json").write_text('{"threads": []}')
    (sessions / ".threads" / "dashboard_incog.json").write_text('{"threads": []}')
    (sessions / "dashboard_kept.attachments").mkdir()
    (sessions / "dashboard_kept.attachments" / "img.png").write_bytes(b"\x89PNG kept")
    (sessions / "dashboard_incog.attachments").mkdir()
    (sessions / "dashboard_incog.attachments" / "img.png").write_bytes(b"\x89PNG private")

    # Host state and derived files that never travel.
    (sessions / "dashboard_kept.jsonl.lock").write_text("")
    (sessions / "dashboard_kept.jsonl.tmp").write_text("partial")
    (sessions / "archive").mkdir()
    (sessions / "archive" / "dashboard_kept.1.jsonl").write_text(_row("rotated") + "\n")
    (sessions / ".summaries").mkdir()
    (sessions / ".summaries" / "dashboard_kept.json").write_text("{}")

    monkeypatch.setenv("KIROCREW_HOME", str(mc))
    return mc


def _members(zip_bytes: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return {name.split("/", 1)[1] for name in zf.namelist()}


def _import(zip_bytes: bytes, target: Path, tmp_path: Path, monkeypatch, mode: str = "merge"):
    zip_path = tmp_path / f"import-{mode}-{target.name}.zip"
    zip_path.write_bytes(zip_bytes)
    monkeypatch.setenv("KIROCREW_HOME", str(target))
    return apply_import_zip(zip_path, mode=mode)


def _assert_stem_locked(sessions: Path, stem: str) -> None:
    """An independent descriptor must not acquire the canonical cross-process lock."""
    with platform_compat.open_lock_file(sessions / f"{stem}.jsonl.lock") as fd:
        acquired = platform_compat.try_acquire_lock(fd, exclusive=True)
        if acquired:
            platform_compat.release_lock(fd)
        assert not acquired, f"{stem} must stay locked through the whole chat"


class TestExport:
    def test_lock_covers_header_transcript_and_companions(self, home, monkeypatch):
        sessions = home / "sessions"
        stem = "dashboard_kept"
        segment = _write_segment(sessions, stem, "20260101-000000", "rotate", "older")
        expected = {
            f"sessions/archive/{segment.name}": segment.read_bytes(),
            f"sessions/{stem}.jsonl": (sessions / f"{stem}.jsonl").read_bytes(),
            f"sessions/.threads/{stem}.json": (sessions / ".threads" / f"{stem}.json").read_bytes(),
            f"sessions/{stem}.attachments/img.png": b"\x89PNG kept",
        }
        seen = set()
        read_header = portability._read_first_line_fd
        add = portability._add_from_fd

        def checked_header(fd):
            line = read_header(fd)
            # `write_text` ends lines with "\r\n" on Windows; the reader stops at "\n".
            if (
                line is not None
                and line.rstrip(b"\r") == _header(memory_mode="persistent").encode()
            ):
                _assert_stem_locked(sessions, stem)
                seen.add("header")
            if line is not None and line.rstrip(b"\r") == segment.read_bytes().splitlines()[0]:
                _assert_stem_locked(sessions, stem)
                seen.add("rotation-header")
            return line

        def checked_add(zf, fd, arcname):
            rel = arcname.split("/", 1)[1]
            if rel in expected:
                _assert_stem_locked(sessions, stem)
                seen.add(rel)
            return add(zf, fd, arcname)

        monkeypatch.setattr(portability, "_read_first_line_fd", checked_header)
        monkeypatch.setattr(portability, "_add_from_fd", checked_add)
        zip_bytes, manifest = create_export_zip(include_sessions=True)

        assert seen == {"header", "rotation-header", *expected}
        assert manifest["contents"]["session_count"] == 2
        assert manifest["contents"]["sessions_withheld"] == 5
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                rel = name.split("/", 1)[1]
                if rel in expected:
                    assert zf.read(name) == expected[rel]
        with platform_compat.open_lock_file(sessions / f"{stem}.jsonl.lock") as fd:
            assert platform_compat.try_acquire_lock(fd, exclusive=True)
            platform_compat.release_lock(fd)

    def test_default_export_carries_no_chats(self, home):
        zip_bytes, manifest = create_export_zip()
        assert not any(m.startswith("sessions/") for m in _members(zip_bytes))
        assert "session_count" not in manifest["contents"]
        assert "sessions_withheld" not in manifest["contents"]

    def test_opt_in_export_carries_only_persistent_chats(self, home):
        zip_bytes, manifest = create_export_zip(include_sessions=True)
        sessions = {m for m in _members(zip_bytes) if m.startswith("sessions/")}
        assert sessions == {
            "sessions/dashboard_kept.jsonl",
            "sessions/dashboard_upper.jsonl",
            "sessions/.threads/dashboard_kept.json",
            "sessions/dashboard_kept.attachments/img.png",
        }
        assert manifest["contents"]["session_count"] == 2
        # incognito, temporary, an unrecognised mode, a header that is not JSON, and a
        # transcript with no metadata header at all.
        assert manifest["contents"]["sessions_withheld"] == 5

    def test_only_dashboard_chats_are_candidates(self, home):
        sessions = home / "sessions"
        for stem in ("slack_123", "cron_job", "subagent_x"):
            _write_chat(sessions, stem, _header(memory_mode="persistent"), "not a chat")
            (sessions / ".threads" / f"{stem}.json").write_text('{"threads": []}')
        zip_bytes, manifest = create_export_zip(include_sessions=True)
        members = _members(zip_bytes)
        for stem in ("slack_123", "cron_job", "subagent_x"):
            assert f"sessions/{stem}.jsonl" not in members
            assert f"sessions/.threads/{stem}.json" not in members
        # Not chats for this feature, so neither exported nor counted as withheld.
        assert manifest["contents"]["session_count"] == 2
        assert manifest["contents"]["sessions_withheld"] == 5

    def test_archived_transcript_bytes_are_the_source_bytes(self, home):
        zip_bytes, _ = create_export_zip(include_sessions=True)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            (name,) = [n for n in zf.namelist() if n.endswith("sessions/dashboard_kept.jsonl")]
            assert zf.read(name) == (home / "sessions" / "dashboard_kept.jsonl").read_bytes()

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
    def test_a_linked_transcript_is_not_followed(self, home, tmp_path):
        outside = tmp_path / "outside.jsonl"
        outside.write_text(_header(memory_mode="persistent") + "\n" + _row("outside") + "\n")
        try:
            os.symlink(outside, home / "sessions" / "dashboard_link.jsonl")
        except OSError:
            pytest.skip("cannot create a symlink here")
        zip_bytes, _ = create_export_zip(include_sessions=True)
        assert "sessions/dashboard_link.jsonl" not in _members(zip_bytes)


class TestExportFitsImport:
    """The chats are budgeted against the caps import enforces, newest chats first."""

    @pytest.fixture
    def aged_home(self, tmp_path, monkeypatch):
        mc = tmp_path / "aged"
        sessions = mc / "sessions"
        sessions.mkdir(parents=True)
        (mc / "config.json").write_text("{}")
        monkeypatch.setenv("KIROCREW_HOME", str(mc))
        return mc

    @staticmethod
    def _chat(mc: Path, stem: str, age_days: int, image: bytes) -> None:
        sessions = mc / "sessions"
        transcript = _write_chat(sessions, stem, _header(memory_mode="persistent"), stem)
        (sessions / f"{stem}.attachments").mkdir()
        (sessions / f"{stem}.attachments" / "img.png").write_bytes(image)
        stamp = 1_700_000_000 - age_days * 86_400
        os.utime(transcript, (stamp, stamp))

    @staticmethod
    def _chats_in(zip_bytes: bytes) -> set[str]:
        return {
            m.removeprefix("sessions/").removesuffix(".jsonl")
            for m in _members(zip_bytes)
            if m.startswith("sessions/dashboard_") and m.endswith(".jsonl")
        }

    @staticmethod
    def _passes_import(zip_bytes: bytes, tmp_path: Path) -> tuple[bool, str]:
        path = tmp_path / "budgeted.zip"
        path.write_bytes(zip_bytes)
        ok, error, _ = portability.validate_import_zip(path)
        return ok, error

    def test_member_cap_keeps_the_newest_whole_chats(self, aged_home, tmp_path, monkeypatch):
        for stem, age in (("dashboard_old", 30), ("dashboard_mid", 10), ("dashboard_new", 1)):
            self._chat(aged_home, stem, age, b"\x89PNG")
        baseline, _ = create_export_zip()
        with zipfile.ZipFile(io.BytesIO(baseline)) as zf:
            base_members = len(zf.infolist())
        # Room for four session members: two whole chats of two files each, not three.
        monkeypatch.setattr(portability, "_MAX_IMPORT_MEMBERS", base_members + 4)

        zip_bytes, manifest = create_export_zip(include_sessions=True)

        assert self._passes_import(zip_bytes, tmp_path) == (True, "")
        assert self._chats_in(zip_bytes) == {"dashboard_new", "dashboard_mid"}
        members = _members(zip_bytes)
        assert "sessions/dashboard_old.attachments/img.png" not in members
        assert manifest["contents"]["session_count"] == 2
        assert manifest["contents"]["sessions_skipped_size"] == 1

    def test_byte_cap_skips_a_whole_chat_and_still_fits_a_smaller_older_one(
        self, aged_home, tmp_path, monkeypatch
    ):
        self._chat(aged_home, "dashboard_new", 1, b"\x89PNG")
        self._chat(aged_home, "dashboard_huge", 5, b"\x00" * 200_000)
        self._chat(aged_home, "dashboard_old", 30, b"\x89PNG")
        baseline, _ = create_export_zip()
        with zipfile.ZipFile(io.BytesIO(baseline)) as zf:
            base_bytes = sum(i.file_size for i in zf.infolist())
        monkeypatch.setattr(
            portability,
            "_MAX_IMPORT_UNCOMPRESSED",
            base_bytes + portability._MANIFEST_BASE_RESERVE + 20_000,
        )

        zip_bytes, manifest = create_export_zip(include_sessions=True)

        assert self._passes_import(zip_bytes, tmp_path) == (True, "")
        assert self._chats_in(zip_bytes) == {"dashboard_new", "dashboard_old"}
        # Never a partial chat: none of the skipped chat's files travel.
        assert not any("dashboard_huge" in m for m in _members(zip_bytes))
        assert manifest["contents"]["sessions_skipped_size"] == 1

    @pytest.mark.parametrize("image_name", ["x" * 180, "雪" * 60], ids=["ascii", "escaped-unicode"])
    def test_manifest_byte_cap_keeps_newest_whole_chats_and_restores_epochs(
        self, aged_home, tmp_path, monkeypatch, manual_clock, image_name
    ):
        manual_clock.install(monkeypatch, portability, time=False, datetime=True)
        # Non-session fields are not a fixed-size envelope; JSON escaping counts
        # here as well as in attachment names (including on Windows).
        monkeypatch.setenv("USER", 'owner-"\\雪' * 100)
        for stem, age in (("dashboard_old", 30), ("dashboard_mid", 10), ("dashboard_new", 1)):
            self._chat(aged_home, stem, age, b"\x89PNG")
            image = aged_home / "sessions" / f"{stem}.attachments" / "img.png"
            image = image.rename(image.with_name(f"{image_name}.png"))
            epoch = 1_600_000_000 - age * 86_400
            os.utime(image, (epoch, epoch))
        _, full_manifest = create_export_zip(include_sessions=True)
        expected_mtimes = {
            name: epoch
            for name, epoch in full_manifest["session_mtimes"].items()
            if "dashboard_old" not in name
        }
        expected_manifest = {
            **full_manifest,
            "contents": {
                **full_manifest["contents"],
                "session_count": 2,
                "sessions_skipped_size": 1,
            },
            "session_mtimes": expected_mtimes,
        }
        # Room for the four retained records plus their conservative float reserve,
        # but not the oldest chat. The archive inventory caps remain unchanged.
        cap = len(json.dumps(expected_manifest, indent=2).encode("utf-8")) + 200
        monkeypatch.setattr(portability, "_MAX_SETTINGS_DOCUMENT_BYTES", cap)

        zip_bytes, manifest = create_export_zip(include_sessions=True)

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            manifest_info = next(i for i in zf.infolist() if i.filename.endswith("MANIFEST.json"))
            assert manifest_info.file_size <= cap
            assert len(zf.read(manifest_info)) <= cap
            assert portability._read_session_mtimes(zf) == expected_mtimes
        assert self._passes_import(zip_bytes, tmp_path) == (True, "")
        assert self._chats_in(zip_bytes) == {"dashboard_new", "dashboard_mid"}
        assert not any("dashboard_old" in name for name in _members(zip_bytes))
        assert manifest["contents"]["session_count"] == 2
        assert manifest["contents"]["sessions_skipped_size"] == 1
        assert manifest["session_mtimes"] == expected_mtimes
        assert {name.split("/", 1)[1] for name in expected_mtimes} == {
            name for name in _members(zip_bytes) if name.startswith("sessions/")
        }
        assert len(expected_mtimes) < portability._MAX_IMPORT_MEMBERS
        target = tmp_path / "restored"
        target.mkdir()
        summary = _import(zip_bytes, target, tmp_path, monkeypatch)
        assert summary["sessions_added"] == 2
        for name, epoch in expected_mtimes.items():
            assert (target / name.split("/", 1)[1]).stat().st_mtime == epoch

    def test_nothing_skipped_reports_zero(self, aged_home):
        self._chat(aged_home, "dashboard_new", 1, b"\x89PNG")
        _, manifest = create_export_zip(include_sessions=True)
        assert manifest["contents"]["session_count"] == 1
        assert manifest["contents"]["sessions_skipped_size"] == 0


class TestImport:
    @pytest.mark.parametrize("collision", [True, False], ids=["competing-chat", "new-chat"])
    def test_lock_covers_recheck_and_whole_chat_install(
        self, home, tmp_path, monkeypatch, collision
    ):
        segment = _write_segment(
            home / "sessions", "dashboard_kept", "20260101-000000", "rotate", "older"
        )
        zip_bytes, _ = create_export_zip(include_sessions=True)
        target = tmp_path / "target"
        target.mkdir()
        sessions = target / "sessions"
        sessions.mkdir()
        stem = "dashboard_kept"
        locked_stems = ConversationLog.locked_stems
        copy_file = portability._copy_import_session_fd
        reached_lock = []
        installed = []
        competing_bytes = None

        @contextmanager
        def competing_chat(log, stems):
            nonlocal competing_bytes
            with locked_stems(log, stems):
                if log._dir == sessions and stems == [stem]:
                    reached_lock.append(stem)
                    # The staged archive loses to a writer that finishes before
                    # the destination check in this critical section.
                    if collision:
                        competing_bytes = _write_chat(
                            sessions, stem, _header(), "competing copy"
                        ).read_bytes()
                yield

        def checked_copy(mc, rel, fd, **kwargs):
            copied_stem = "dashboard_upper" if rel.name == "dashboard_upper.jsonl" else stem
            _assert_stem_locked(sessions, copied_stem)
            if len(rel.parts) == 2:
                installed.append(copied_stem)
            return copy_file(mc, rel, fd, **kwargs)

        monkeypatch.setattr(ConversationLog, "locked_stems", competing_chat)
        monkeypatch.setattr(portability, "_copy_import_session_fd", checked_copy)
        summary = _import(zip_bytes, target, tmp_path, monkeypatch)

        assert reached_lock == [stem]
        assert summary["sessions_withheld"] == 0
        sidecar = sessions / ".threads" / f"{stem}.json"
        images = sessions / f"{stem}.attachments"
        if collision:
            assert (sessions / f"{stem}.jsonl").read_bytes() == competing_bytes
            assert not sidecar.exists()
            assert not images.exists()
            assert not (sessions / "archive").exists()
            assert installed == ["dashboard_upper"]
        else:
            assert (sessions / "archive" / segment.name).read_bytes() == segment.read_bytes()
            assert sidecar.read_text() == '{"threads": []}'
            assert (images / "img.png").read_bytes() == b"\x89PNG kept"
            assert set(installed) == {stem, "dashboard_upper"}
        assert summary["sessions_added"] == (1 if collision else 2)
        assert summary["sessions_skipped_existing"] == (1 if collision else 0)
        with platform_compat.open_lock_file(sessions / f"{stem}.jsonl.lock") as fd:
            assert platform_compat.try_acquire_lock(fd, exclusive=True)
            platform_compat.release_lock(fd)

    def test_merge_adds_chats_and_lists_them(self, home, tmp_path, monkeypatch):
        zip_bytes, _ = create_export_zip(include_sessions=True)
        target = tmp_path / "target"
        target.mkdir()

        summary = _import(zip_bytes, target, tmp_path, monkeypatch)

        assert summary["sessions_added"] == 2
        assert summary["sessions_skipped_existing"] == 0
        assert summary["sessions_withheld"] == 0
        sessions = target / "sessions"
        assert (sessions / ".threads" / "dashboard_kept.json").is_file()
        assert (sessions / "dashboard_kept.attachments" / "img.png").read_bytes() == b"\x89PNG kept"
        keys = {s["key"] for s in ConversationLog(base_dir=sessions).list_sessions()}
        assert {"dashboard_kept", "dashboard_upper"} <= keys
        assert "dashboard_legacy" not in keys

    def test_imported_chat_keeps_its_last_activity_time(self, home, tmp_path, monkeypatch):
        source = home / "sessions" / "dashboard_kept.jsonl"
        os.utime(source, (1_700_000_000, 1_700_000_000))
        zip_bytes, _ = create_export_zip(include_sessions=True)
        target = tmp_path / "target"
        target.mkdir()

        _import(zip_bytes, target, tmp_path, monkeypatch)

        # Zip timestamps have two-second resolution.
        imported = (target / "sessions" / "dashboard_kept.jsonl").stat().st_mtime
        assert abs(imported - 1_700_000_000) <= 2

    def test_merge_never_overwrites_an_existing_chat(self, home, tmp_path, monkeypatch):
        zip_bytes, _ = create_export_zip(include_sessions=True)
        target = tmp_path / "target"
        (target / "sessions" / ".threads").mkdir(parents=True)
        mine = _write_chat(target / "sessions", "dashboard_kept", _header(), "this machine's copy")
        mine_threads = target / "sessions" / ".threads" / "dashboard_kept.json"
        mine_threads.write_text('{"mine": true}')
        before = mine.read_bytes()

        summary = _import(zip_bytes, target, tmp_path, monkeypatch)

        assert mine.read_bytes() == before
        assert mine_threads.read_text() == '{"mine": true}'
        assert not (target / "sessions" / "dashboard_kept.attachments").exists()
        assert summary["sessions_added"] == 1
        assert summary["sessions_skipped_existing"] == 1

    def test_reimport_is_a_no_op(self, home, tmp_path, monkeypatch):
        zip_bytes, _ = create_export_zip(include_sessions=True)
        target = tmp_path / "target"
        target.mkdir()
        _import(zip_bytes, target, tmp_path, monkeypatch)

        summary = _import(zip_bytes, target, tmp_path, monkeypatch)

        assert summary["sessions_added"] == 0
        assert summary["sessions_skipped_existing"] == 2

    def test_replace_mode_keeps_existing_chats(self, home, tmp_path, monkeypatch):
        zip_bytes, _ = create_export_zip(include_sessions=True)
        target = tmp_path / "target"
        (target / "sessions").mkdir(parents=True)
        mine = _write_chat(target / "sessions", "dashboard_only_here", _header(), "keep me")

        summary = _import(zip_bytes, target, tmp_path, monkeypatch, mode="replace")

        assert mine.is_file()
        assert summary["sessions_added"] == 2

    def test_archive_without_chats_records_nothing(self, home, tmp_path, monkeypatch):
        zip_bytes, _ = create_export_zip()
        target = tmp_path / "target"
        target.mkdir()

        summary = _import(zip_bytes, target, tmp_path, monkeypatch)

        assert "sessions_added" not in summary
        assert not (target / "sessions").exists()

    def test_import_rejudges_a_hand_built_archive(self, tmp_path, monkeypatch):
        """The archive is not trusted to have applied the export's own rules."""
        top = "kirocrew-export-20260101T000000Z"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"{top}/MANIFEST.json", json.dumps({"version": 1, "contents": {}}))
            zf.writestr(
                f"{top}/sessions/dashboard_ok.jsonl", _header() + "\n" + _row("fine") + "\n"
            )
            zf.writestr(
                f"{top}/sessions/dashboard_private.jsonl",
                _header(memory_mode="incognito") + "\n" + _row("private") + "\n",
            )
            # Legal on every platform's filesystem, but not a name `_safe_key` writes.
            zf.writestr(f"{top}/sessions/dashboard_a b.jsonl", _header() + "\n")
            zf.writestr(f"{top}/sessions/notes.txt", "not a transcript")
            zf.writestr(f"{top}/sessions/dashboard_private.attachments/x.png", b"private")
        target = tmp_path / "target"
        target.mkdir()

        summary = _import(buf.getvalue(), target, tmp_path, monkeypatch)

        installed = {p.name for p in (target / "sessions").iterdir()}
        assert installed == {"dashboard_ok.jsonl", "dashboard_ok.jsonl.lock"}
        assert summary["sessions_added"] == 1
        assert summary["sessions_withheld"] == 1

    def test_import_installs_only_dashboard_chats(self, tmp_path, monkeypatch):
        top = "kirocrew-export-20260101T000000Z"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"{top}/MANIFEST.json", json.dumps({"version": 1, "contents": {}}))
            body = _header() + "\n" + _row("fine") + "\n"
            zf.writestr(f"{top}/sessions/dashboard_ok.jsonl", body)
            for stem in ("slack_123", "cron_job", "subagent_x", "dashboard_"):
                zf.writestr(f"{top}/sessions/{stem}.jsonl", body)
                zf.writestr(f"{top}/sessions/.threads/{stem}.json", '{"threads": []}')
        target = tmp_path / "target"
        target.mkdir()

        summary = _import(buf.getvalue(), target, tmp_path, monkeypatch)

        installed = {p.name for p in (target / "sessions").iterdir() if p.is_file()}
        assert installed == {"dashboard_ok.jsonl", "dashboard_ok.jsonl.lock"}
        assert not (target / "sessions" / ".threads").exists()
        assert summary["sessions_added"] == 1
        assert summary["sessions_withheld"] == 0

    @pytest.mark.parametrize(
        "header",
        [
            '{"_type": "metadata", "n": ' + "1" * 5000 + "}",
            '{"_type": "metadata", "x": ' + "[" * 100_000 + "]" * 100_000 + "}",
        ],
        ids=["integer-past-digit-limit", "nesting-past-recursion-limit"],
    )
    def test_an_unparseable_header_is_withheld_not_raised(self, tmp_path, monkeypatch, header):
        top = "kirocrew-export-20260101T000000Z"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"{top}/MANIFEST.json", json.dumps({"version": 1, "contents": {}}))
            zf.writestr(
                f"{top}/sessions/dashboard_ok.jsonl", _header() + "\n" + _row("fine") + "\n"
            )
            zf.writestr(f"{top}/sessions/dashboard_bad.jsonl", header + "\n")
        target = tmp_path / "target"
        target.mkdir()

        summary = _import(buf.getvalue(), target, tmp_path, monkeypatch)

        assert {p.name for p in (target / "sessions").iterdir()} == {
            "dashboard_ok.jsonl",
            "dashboard_ok.jsonl.lock",
        }
        assert summary["sessions_withheld"] == 1

    @pytest.mark.parametrize("collision", ["top-level-dir", "top-level-file"])
    def test_an_archive_name_cannot_collide_with_the_staging_area(
        self, tmp_path, monkeypatch, collision
    ):
        top = "sessions-stage" if collision == "top-level-dir" else "kirocrew-export-x"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"{top}/MANIFEST.json", json.dumps({"version": 1, "contents": {}}))
            zf.writestr(
                f"{top}/sessions/dashboard_ok.jsonl", _header() + "\n" + _row("fine") + "\n"
            )
            if collision == "top-level-file":
                zf.writestr("sessions-stage", "planted")
        target = tmp_path / "target"
        target.mkdir()

        summary = _import(buf.getvalue(), target, tmp_path, monkeypatch)

        assert (target / "sessions" / "dashboard_ok.jsonl").is_file()
        assert summary["sessions_added"] == 1


@pytest.fixture
def extracted_chat(tmp_path):
    snap = tmp_path / "work" / "export"
    sessions = snap / "sessions"
    sessions.mkdir(parents=True)
    stem = "dashboard_one"
    transcript = _write_chat(sessions, stem, _header(), "imported chat")
    (sessions / ".threads").mkdir()
    sidecar = sessions / ".threads" / f"{stem}.json"
    sidecar.write_bytes(b'{"threads": []}')
    (sessions / f"{stem}.attachments").mkdir()
    image = sessions / f"{stem}.attachments" / "image.png"
    image.write_bytes(b"original image")
    segment = _write_segment(sessions, stem, "20260101-000000", "rotate", "older")
    target = tmp_path / "target"
    target.mkdir()
    return (
        snap,
        target,
        {"transcript": transcript, "sidecar": sidecar, "attachment": image, "rotation": segment},
    )


class TestImportPinnedFiles:
    @pytest.mark.parametrize("unpinned", [False, True], ids=["pinned", "unpinned"])
    @pytest.mark.parametrize(
        "swap",
        [
            "attachments-dir",
            "threads-dir",
            "archive-dir",
            "sessions-dir",
            "export-dir",
            "transcript",
            "sidecar",
            "attachment",
            "rotation",
        ],
    )
    def test_source_swap_never_moves_or_installs_outside_bytes(
        self, extracted_chat, tmp_path, monkeypatch, swap, unpinned
    ):
        snap, target, files = extracted_chat
        if unpinned:
            monkeypatch.setattr(portability.pinned_fs, "supports_pinned_walk", lambda: False)
            monkeypatch.setattr(portability.pinned_fs, "supports_pinned_tree_walk", lambda: False)
        directories = {
            "attachments-dir": files["attachment"].parent,
            "threads-dir": files["sidecar"].parent,
            "archive-dir": files["rotation"].parent,
            "sessions-dir": snap / "sessions",
            "export-dir": snap,
        }
        victim = directories.get(swap, files.get(swap))
        assert victim is not None
        outside = tmp_path / "outside"
        directory = swap in directories
        foreign = b"foreign bytes must stay outside"
        if directory:
            outside.mkdir()
            for path in files.values():
                if path.is_relative_to(victim):
                    other = outside / path.relative_to(victim)
                    other.parent.mkdir(parents=True, exist_ok=True)
                    other.write_bytes(foreign)
            outside_files = list(outside.rglob("*"))
            outside_files = [p for p in outside_files if p.is_file()]
        else:
            outside.write_bytes(foreign)
            outside_files = [outside]
        originals = {path: path.read_bytes() for path in files.values()}
        displaced = victim.with_name(victim.name + "-displaced")
        classifier_name = "_real_dir" if swap == "attachments-dir" else "_real_file"
        classifier = getattr(portability, classifier_name)
        trigger = (
            files["attachment"].parent
            if swap == "attachments-dir"
            else files[
                {
                    "threads-dir": "sidecar",
                    "archive-dir": "rotation",
                    "sessions-dir": "transcript",
                    "export-dir": "transcript",
                }.get(swap, swap)
            ]
        )
        swapped = []

        def swap_after_classification(entry):
            accepted = classifier(entry)
            if accepted and not swapped and Path(entry.path) == trigger:
                victim.rename(displaced)
                try:
                    outside_link_target = outside
                    victim.symlink_to(outside_link_target, target_is_directory=directory)
                except (OSError, NotImplementedError):
                    pytest.skip("cannot create a symlink here")
                swapped.append(True)
            return accepted

        monkeypatch.setattr(portability, classifier_name, swap_after_classification)
        summary = {"items": []}
        portability._merge_sessions(snap, target, summary, allow_unpinned=True)

        assert swapped, "the classifier/install interleaving must be exercised"
        assert outside_files
        for path in outside_files:
            assert path.is_file(), "import must not move an outside file"
            assert path.read_bytes() == foreign
        for path in target.rglob("*"):
            if path.is_file():
                assert foreign not in path.read_bytes()
        for path, body in originals.items():
            original = (
                displaced / path.relative_to(victim)
                if directory and path.is_relative_to(victim)
                else (displaced if path == victim else path)
            )
            assert original.read_bytes() == body, "import copies; it never removes its source"
        transcript_refused = swap in {"transcript", "sessions-dir", "export-dir"}
        assert summary["sessions_added"] == (0 if transcript_refused else 1)
        assert summary["sessions_withheld"] == (1 if transcript_refused else 0)

    @pytest.mark.parametrize("kind", ["transcript", "rotation"])
    @pytest.mark.parametrize("unpinned", [False, True], ids=["pinned", "unpinned"])
    def test_header_and_install_share_the_open_descriptor(
        self, extracted_chat, monkeypatch, kind, unpinned
    ):
        snap, target, files = extracted_chat
        if platform_compat.IS_WINDOWS:
            pytest.skip("Windows refuses renaming a file held by the no-reparse reader")
        if unpinned:
            monkeypatch.setattr(portability.pinned_fs, "supports_pinned_walk", lambda: False)
            monkeypatch.setattr(portability.pinned_fs, "supports_pinned_tree_walk", lambda: False)
        source = files[kind]
        expected = source.read_bytes()
        judge_name = "_transcript_header_verdict" if kind == "transcript" else "_is_rotation_header"
        judge = getattr(portability, judge_name)
        changed = []

        def replace_after_judgment(line):
            verdict = judge(line)
            if not changed:
                source.rename(source.with_name(source.name + "-judged"))
                source.write_text(
                    _header(memory_mode="incognito") + "\n" + _row("unjudged replacement")
                )
                changed.append(True)
            return verdict

        monkeypatch.setattr(portability, judge_name, replace_after_judgment)
        summary = {"items": []}
        portability._merge_sessions(snap, target, summary, allow_unpinned=True)

        assert changed
        assert summary["sessions_added"] == 1
        assert (target / source.relative_to(snap)).read_bytes() == expected
        assert b"unjudged replacement" in source.read_bytes()

    @pytest.mark.parametrize("allowed", [False, True])
    def test_unpinned_policy_is_inherited_and_sources_are_kept(
        self, extracted_chat, monkeypatch, allowed
    ):
        snap, target, files = extracted_chat
        monkeypatch.setattr(portability.pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(portability.pinned_fs, "supports_pinned_tree_walk", lambda: False)
        summary = {"items": []}
        if not allowed:
            with pytest.raises(portability.pinned_fs.PinnedPathRefusal):
                portability._merge_sessions(snap, target, summary, allow_unpinned=False)
            assert not list(target.iterdir())
            return
        portability._merge_sessions(snap, target, summary, allow_unpinned=True)
        assert summary["sessions_added"] == 1
        for path in files.values():
            assert (target / path.relative_to(snap)).read_bytes() == path.read_bytes()


_DEEP = '{"_type": "metadata", "x": ' + "[" * 100_000 + "]" * 100_000 + "}"
_HUGE_INT = '{"_type": "metadata", "n": ' + "1" * 5000 + "}"

#: (first line, the mode the shared header parse reads from it).
_HEADER_RULES = [
    (_header(memory_mode="persistent"), "persistent"),
    (_header(), "persistent"),
    (_header(memory_mode=None), "persistent"),
    (_header(memory_mode=" Incognito "), "incognito"),
    (_header(memory_mode="TEMPORARY"), "temporary"),
    (_header(memory_mode="something-new"), None),
    (_header(memory_mode=""), None),
    (_header(memory_mode=1), None),
    (_header(memory_mode=["incognito"]), None),
    (_row("no header at all"), None),
    (json.dumps({"_type": "message", "memory_mode": "persistent"}), None),
    ("[1, 2]", None),
    ("{not json", None),
    (_DEEP, None),
    (_HUGE_INT, None),
]
_HEADER_IDS = [
    "persistent",
    "metadata-without-mode",
    "null-mode",
    "padded-incognito",
    "upper-temporary",
    "unknown-mode",
    "empty-mode",
    "int-mode",
    "list-mode",
    "no-header",
    "non-metadata-object",
    "not-an-object",
    "not-json",
    "deep-nesting",
    "integer-past-digit-limit",
]


class TestSharedHeaderRule:
    """Export, import and the restricted-session gate read a header's mode the same way."""

    @pytest.mark.parametrize(("line", "mode"), _HEADER_RULES, ids=_HEADER_IDS)
    def test_header_parse_rules(self, line, mode):
        assert memory_mode_from_header_line(line.encode()) == mode

    def test_invalid_utf8_is_replaced_not_raised(self):
        line = b'{"_type": "metadata", "title": "\xff", "memory_mode": "persistent"}'
        assert memory_mode_from_header_line(line) == "persistent"

    @pytest.mark.parametrize(("line", "mode"), _HEADER_RULES, ids=_HEADER_IDS)
    def test_gate_and_export_agree(self, tmp_path, line, mode):
        path = tmp_path / "chat.jsonl"
        path.write_text(line + "\n" + _row("body") + "\n")
        assert _read_memory_mode(path) == mode
        verdict = _transcript_header_verdict(line.encode())
        assert (verdict is None) == (mode == "persistent")
        if mode in ("incognito", "temporary"):
            assert verdict == "incognito or temporary chat"
        elif mode is None:
            assert verdict == "unreadable header"

    def test_empty_and_overlong_first_lines(self):
        assert _transcript_header_verdict(b"") == "empty transcript"
        assert _transcript_header_verdict(None) == "unreadable header"

    @pytest.mark.parametrize("over", [False, True], ids=["within-bound", "past-bound"])
    def test_gate_and_export_share_the_header_bound(self, tmp_path, over):
        base = _header(memory_mode="persistent", pad="")
        pad = TRANSCRIPT_HEADER_MAX_BYTES - len(base) + (1 if over else -64)
        line = _header(memory_mode="persistent", pad="x" * pad)
        path = tmp_path / "chat.jsonl"
        path.write_text(line + "\n" + _row("body") + "\n")
        fd = os.open(path, os.O_RDONLY)
        try:
            verdict = _transcript_header_verdict(_read_first_line_fd(fd))
        finally:
            os.close(fd)
        if over:
            assert _read_memory_mode(path) is None
            assert verdict == "unreadable header"
        else:
            assert _read_memory_mode(path) == "persistent"
            assert verdict is None


class TestRetainedRotation:
    @pytest.mark.parametrize("mode", ["merge", "replace"])
    def test_rotate_segments_roundtrip(self, home, tmp_path, monkeypatch, mode):
        sessions = home / "sessions"
        stem = "dashboard_a"
        _write_chat(sessions, stem, _header(), "tail")
        first = _write_segment(sessions, stem, "20260101-000000", "rotate", "head one")
        second = _write_segment(sessions, stem, "20260101-000000-2", "rotate", "head two")
        _write_segment(sessions, stem, "20260101-000000-3", "compact", "discarded")
        _write_segment(sessions, stem, "20260101-000000-4", "foreign-dedup", "discarded")
        _write_segment(sessions, stem, "20260101-000000-5", "rewrite", "discarded")
        # Neither a plain shared prefix nor a delimiter-containing longer stem is ours.
        for other in ("dashboard_ab", "dashboard_a__other"):
            _write_segment(sessions, other, "20260101-000000", "rotate", "not ours")
        zip_bytes, manifest = create_export_zip(include_sessions=True)
        expected = {f"sessions/archive/{p.name}" for p in (first, second)}
        assert {m for m in _members(zip_bytes) if m.startswith("sessions/archive/")} == expected
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if "/sessions/" in name:
                    assert name in manifest["session_mtimes"]
        target = tmp_path / "target"
        target.mkdir()
        _import(zip_bytes, target, tmp_path, monkeypatch, mode=mode)
        archived = target / "sessions" / "archive"
        assert {p.name for p in archived.iterdir()} == {first.name, second.name}
        assert (archived / first.name).read_bytes() == first.read_bytes()
        rows = ConversationLog(base_dir=target / "sessions").read_rotated_messages_chained(stem)
        assert [row["content"] for row in rows] == ["head one", "head two"]

    @pytest.mark.parametrize("kind", ["symlink", "hardlink"])
    def test_linked_rotation_segment_is_not_exported(self, home, tmp_path, kind):
        outside = tmp_path / "outside.jsonl"
        outside.write_text('{"_type":"archive","reason":"rotate"}\n' + _row("outside"))
        segment = home / "sessions" / "archive" / "dashboard_kept__20260101-000000.jsonl"
        try:
            (os.symlink if kind == "symlink" else os.link)(outside, segment)
        except (OSError, NotImplementedError):
            pytest.skip(f"cannot create {kind} on this platform")
        zip_bytes, _ = create_export_zip(include_sessions=True)
        assert f"sessions/archive/{segment.name}" not in _members(zip_bytes)

    @pytest.mark.parametrize("skip", ["existing", "private", "non-dashboard", "missing"])
    def test_skipped_chat_installs_no_segments(self, tmp_path, monkeypatch, skip):
        stem = "slack_123" if skip == "non-dashboard" else "dashboard_a"
        target = tmp_path / "target"
        (target / "sessions").mkdir(parents=True)
        if skip == "existing":
            _write_chat(target / "sessions", stem, _header(), "local copy")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("export/MANIFEST.json", '{"version":1}')
            if skip != "missing":
                header = _header(memory_mode="temporary" if skip == "private" else "persistent")
                zf.writestr(f"export/sessions/{stem}.jsonl", header + "\n" + _row("tail"))
            zf.writestr(
                f"export/sessions/archive/{stem}__20260101-000000.jsonl",
                '{"_type":"archive","reason":"rotate"}\n' + _row("head"),
            )
        _import(buf.getvalue(), target, tmp_path, monkeypatch)
        assert not (target / "sessions" / "archive").exists()

    def test_import_rejudges_segment_headers(self, tmp_path, monkeypatch):
        buf = io.BytesIO()
        headers = [
            '{"_type":"archive","reason":"rotate"}',
            '{"_type":"archive","reason":"compact"}',
            '{"_type":"archive","reason":"foreign-dedup"}',
            '{"_type":"archive","reason":"rewrite"}',
            '{"_type":"metadata","reason":"rotate"}',
            "{not json",
            "[]",
            '{"reason":' + "1" * 5000 + "}",
            "x" * (TRANSCRIPT_HEADER_MAX_BYTES + 1),
        ]
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("export/MANIFEST.json", '{"version":1}')
            zf.writestr("export/sessions/dashboard_a.jsonl", _header() + "\n" + _row("tail"))
            for i, header in enumerate(headers):
                zf.writestr(
                    f"export/sessions/archive/dashboard_a__20260101-000000-{i}.jsonl",
                    header + "\n" + _row("archived"),
                )
            zf.writestr(
                "export/sessions/archive/dashboard_ab__20260101-000000.jsonl",
                headers[0] + "\n" + _row("other"),
            )
        target = tmp_path / "target"
        target.mkdir()
        _import(buf.getvalue(), target, tmp_path, monkeypatch)
        assert {p.name for p in (target / "sessions" / "archive").iterdir()} == {
            "dashboard_a__20260101-000000-0.jsonl"
        }


class TestSessionEpochs:
    def test_import_epoch_zero_reexports_with_clamped_header(self, tmp_path, monkeypatch):
        member = "export/sessions/dashboard_a.jsonl"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("export/MANIFEST.json", json.dumps({"session_mtimes": {member: 0}}))
            zf.writestr(member, _header() + "\n" + _row("tail"))
        target = tmp_path / "target"
        target.mkdir()
        summary = _import(buf.getvalue(), target, tmp_path, monkeypatch)
        assert summary["sessions_added"] == 1
        assert (target / "sessions" / "dashboard_a.jsonl").stat().st_mtime == 0

        zip_bytes, manifest = create_export_zip(include_sessions=True)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            name = next(n for n in zf.namelist() if n.endswith("/dashboard_a.jsonl"))
            assert zf.getinfo(name).date_time == (1980, 1, 1, 0, 0, 0)
            assert manifest["session_mtimes"][name] == 0
            stored_manifest = json.loads(zf.read(f"{name.split('/')[0]}/MANIFEST.json"))
            assert stored_manifest["session_mtimes"][name] == 0

    @pytest.mark.parametrize(
        ("epoch", "expected"),
        [(0, (1980, 1, 1, 0, 0, 0)), (4_400_000_000, (2107, 12, 31, 23, 59, 58))],
        ids=["before-1980", "after-2107"],
    )
    def test_export_clamps_all_session_member_headers(self, home, epoch, expected):
        source = home / "sessions"
        segment = _write_segment(source, "dashboard_kept", "20260101-000000", "rotate", "head")
        paths = [
            source / "dashboard_kept.jsonl",
            source / ".threads" / "dashboard_kept.json",
            source / "dashboard_kept.attachments" / "img.png",
            segment,
        ]
        for path in paths:
            os.utime(path, (epoch, epoch))
            assert path.stat().st_mtime == epoch
        zip_bytes, manifest = create_export_zip(include_sessions=True)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            prefix = zf.namelist()[0].split("/")[0]
            for path in paths:
                name = f"{prefix}/{path.relative_to(home).as_posix()}"
                assert zf.getinfo(name).date_time == expected
                assert zf.read(name) == path.read_bytes()
                assert manifest["session_mtimes"][name] == epoch

    @pytest.mark.parametrize("source_tz", ["UTC", "Pacific/Kiritimati", "America/St_Johns"])
    def test_cross_timezone_preserves_epoch(self, home, tmp_path, monkeypatch, local_tz, source_tz):
        source = home / "sessions"
        segment = _write_segment(source, "dashboard_kept", "20260101-000000", "rotate", "head")
        paths = [
            source / "dashboard_kept.jsonl",
            source / ".threads" / "dashboard_kept.json",
            source / "dashboard_kept.attachments" / "img.png",
            segment,
        ]
        epoch = 1_700_000_000.25
        for path in paths:
            os.utime(path, (epoch, epoch))
        local_tz(source_tz)
        zip_bytes, _ = create_export_zip(include_sessions=True)
        local_tz("Pacific/Kiritimati" if source_tz == "UTC" else "UTC")
        target = tmp_path / "target"
        target.mkdir()
        _import(zip_bytes, target, tmp_path, monkeypatch)
        for path in paths:
            assert (
                target / "sessions" / path.relative_to(source)
            ).stat().st_mtime == pytest.approx(epoch, abs=2, rel=0)

    @pytest.mark.parametrize(
        "record",
        [
            None,
            [],
            {},
            {"other": 123},
            "invalid",
            True,
            "1700000000",
            float("nan"),
            float("inf"),
            10**1000,
            1e30,
            1e100,
        ],
        ids=[
            "missing",
            "list-map",
            "empty-map",
            "missing-member",
            "text-map",
            "bool-epoch",
            "text-epoch",
            "nan-epoch",
            "infinite-epoch",
            "huge-int",
            "overflow-time-t",
            "out-of-range",
        ],
    )
    def test_bad_or_missing_epoch_keeps_extraction_time(self, tmp_path, monkeypatch, record):
        member = "export/sessions/dashboard_a.jsonl"
        manifest = {"version": 1}
        if record is not None:
            manifest["session_mtimes"] = (
                record
                if isinstance(record, (dict, list)) or record == "invalid"
                else {member: record}
            )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("export/MANIFEST.json", json.dumps(manifest))
            zf.writestr(
                zipfile.ZipInfo(member, date_time=(2001, 1, 1, 0, 0, 0)),
                _header() + "\n" + _row("tail"),
            )
        # Observe the extraction's actual timestamp: no assumption about the host clock.
        extract = zipfile.ZipFile.extract
        extracted_times = []

        def checked_extract(zf, info, *args, **kwargs):
            path = extract(zf, info, *args, **kwargs)
            if info.filename == member:
                extracted_times.append(Path(path).stat().st_mtime)
            return path

        monkeypatch.setattr(zipfile.ZipFile, "extract", checked_extract)
        target = tmp_path / "target"
        target.mkdir()
        summary = _import(buf.getvalue(), target, tmp_path, monkeypatch)
        assert summary["sessions_added"] == 1
        assert len(extracted_times) == 1
        assert (target / "sessions" / "dashboard_a.jsonl").stat().st_mtime == pytest.approx(
            extracted_times[0], abs=2, rel=0
        )

    @pytest.mark.parametrize("cap", ["bytes", "records"])
    def test_epoch_records_are_bounded(self, monkeypatch, cap):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "export/MANIFEST.json",
                json.dumps(
                    {"session_mtimes": {"export/sessions/dashboard_a.jsonl": 1_700_000_000}}
                ),
            )
        if cap == "bytes":
            monkeypatch.setattr(portability, "_MAX_SETTINGS_DOCUMENT_BYTES", 1)
        else:
            monkeypatch.setattr(portability, "_MAX_IMPORT_MEMBERS", 0)
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
            assert portability._read_session_mtimes(zf) == {}


class TestImportRollback:
    @pytest.mark.parametrize("unpinned", [False, True], ids=["pinned", "unpinned"])
    @pytest.mark.parametrize("kind", ["transcript", "attachment", "rotation"])
    @pytest.mark.parametrize("mid_write", [False, True], ids=["before-copy", "mid-write"])
    def test_failed_chat_rolls_back_and_retry_installs_fully(
        self, extracted_chat, tmp_path, monkeypatch, kind, unpinned, mid_write
    ):
        snap, target, files = extracted_chat
        if unpinned:
            monkeypatch.setattr(portability.pinned_fs, "supports_pinned_walk", lambda: False)
            monkeypatch.setattr(portability.pinned_fs, "supports_pinned_tree_walk", lambda: False)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"{snap.name}/MANIFEST.json", json.dumps({"version": 1, "contents": {}}))
            for path in files.values():
                zf.writestr(f"{snap.name}/{path.relative_to(snap).as_posix()}", path.read_bytes())
            zf.writestr(f"{snap.name}/sessions/dashboard_two.jsonl", _header() + "\n")
        destinations = {key: target / path.relative_to(snap) for key, path in files.items()}
        copy_file = portability.pinned_fs.copy_file_pinned
        rollback = portability._rollback_import_session
        seen = []
        rolled_back = []

        def checked_rollback(mc, parents, created):
            _assert_stem_locked(target / "sessions", "dashboard_one")
            for rel, identity in created.items():
                parent = parents[rel.parent]
                st = (
                    (mc / rel).lstat()
                    if parent is None
                    else os.stat(rel.name, dir_fd=parent, follow_symlinks=False)
                )
                assert identity == (st.st_dev, st.st_ino)
            rolled_back.append(set(created))
            return rollback(mc, parents, created)

        def fail_write(src, dst):
            dst.write(src.read(8))
            dst.flush()
            raise OSError(errno.ENOSPC, "injected disk full")

        def failing_copy(by_name, dst=None, **kwargs):
            if dst is not None and Path(dst) in destinations.values():
                _assert_stem_locked(target / "sessions", "dashboard_one")
                seen.append(Path(dst))
                if Path(dst) == destinations[kind]:
                    if mid_write:
                        with monkeypatch.context() as patch:
                            patch.setattr(portability.pinned_fs.shutil, "copyfileobj", fail_write)
                            return copy_file(by_name, dst, **kwargs)
                    os.close(kwargs["src_fd"])
                    raise OSError(errno.ENOSPC, "injected disk full")
            return copy_file(by_name, dst, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(portability.pinned_fs, "copy_file_pinned", failing_copy)
            patch.setattr(portability, "_rollback_import_session", checked_rollback)
            summary = _import(buf.getvalue(), target, tmp_path, monkeypatch)

        assert len(rolled_back) == 1
        expected_created = set(seen if mid_write else seen[:-1])
        assert rolled_back[0] == {path.relative_to(target) for path in expected_created}
        assert summary["sessions_failed"] == 1
        assert summary["sessions_added"] == 1, "the next chat must still install"
        assert summary["sessions_skipped_existing"] == summary["sessions_withheld"] == 0
        assert summary["refused_merges"] == ["sessions"]
        assert any("dashboard_one.jsonl (failed:" in item for item in summary["items"])
        assert not any(path.exists() for path in destinations.values())
        assert not any("rollback incomplete" in item for item in summary["items"])
        assert seen[0] == destinations["sidecar"]
        if kind == "transcript":
            assert seen[-1] == destinations["transcript"]
            assert set(seen[:-1]) == set(destinations.values()) - {destinations["transcript"]}
        else:
            assert destinations["transcript"] not in seen

        retry = _import(buf.getvalue(), target, tmp_path, monkeypatch)
        assert retry["sessions_added"] == 1
        assert retry["sessions_skipped_existing"] == 1
        assert retry["sessions_failed"] == 0
        for key, path in files.items():
            assert destinations[key].read_bytes() == path.read_bytes()
        again = _import(buf.getvalue(), target, tmp_path, monkeypatch)
        assert again["sessions_added"] == 0
        assert again["sessions_skipped_existing"] == 2

    def test_existing_chat_never_enters_failing_copy(self, extracted_chat, monkeypatch):
        snap, target, files = extracted_chat
        for path in files.values():
            destination = target / path.relative_to(snap)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"existing destination bytes")

        def fail_copy(*args, **kwargs):
            pytest.fail("a pre-existing chat must not enter the copy path")

        monkeypatch.setattr(portability.pinned_fs, "copy_file_pinned", fail_copy)
        summary = {"items": []}
        portability._merge_sessions(snap, target, summary, allow_unpinned=True)
        assert summary["sessions_skipped_existing"] == 1
        assert summary["sessions_added"] == summary["sessions_failed"] == 0
        for path in files.values():
            assert (target / path.relative_to(snap)).read_bytes() == b"existing destination bytes"

    def test_existing_companion_is_not_rolled_back(self, extracted_chat):
        snap, target, files = extracted_chat
        existing = target / files["attachment"].relative_to(snap)
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"pre-existing image")
        summary = {"items": []}
        portability._merge_sessions(snap, target, summary, allow_unpinned=True)
        assert summary["sessions_failed"] == 1
        assert summary["sessions_added"] == summary["sessions_skipped_existing"] == 0
        assert existing.read_bytes() == b"pre-existing image"
        assert not (target / files["sidecar"].relative_to(snap)).exists()
        assert not (target / files["transcript"].relative_to(snap)).exists()

    @pytest.mark.parametrize("unpinned", [False, True], ids=["pinned", "unpinned"])
    @pytest.mark.parametrize("failure", ["replacement", "unlink-error"])
    def test_rollback_reports_survivor_and_continues(
        self, extracted_chat, tmp_path, monkeypatch, unpinned, failure
    ):
        snap, target, files = extracted_chat
        if unpinned:
            monkeypatch.setattr(portability.pinned_fs, "supports_pinned_walk", lambda: False)
            monkeypatch.setattr(portability.pinned_fs, "supports_pinned_tree_walk", lambda: False)
        _write_chat(snap / "sessions", "dashboard_two", _header())
        sidecar = target / files["sidecar"].relative_to(snap)
        transcript = target / files["transcript"].relative_to(snap)
        replacement = tmp_path / "replacement.json"
        replacement.write_bytes(b"replacement must survive")
        copy_file = portability.pinned_fs.copy_file_pinned
        unlink = os.unlink

        def failing_copy(by_name, dst=None, **kwargs):
            if dst == str(transcript):
                os.close(kwargs["src_fd"])
                if failure == "replacement":
                    os.replace(replacement, sidecar)
                raise OSError(errno.ENOSPC, "injected disk full")
            return copy_file(by_name, dst, **kwargs)

        def fail_unlink(path, *args, **kwargs):
            if Path(path).name == sidecar.name:
                raise PermissionError("injected removal failure")
            return unlink(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(portability.pinned_fs, "copy_file_pinned", failing_copy)
            if failure == "unlink-error":
                patch.setattr(os, "unlink", fail_unlink)
            summary = {"items": []}
            portability._merge_sessions(snap, target, summary, allow_unpinned=True)
        expected = (
            b"replacement must survive"
            if failure == "replacement"
            else files["sidecar"].read_bytes()
        )
        assert sidecar.read_bytes() == expected
        assert summary["sessions_failed"] == summary["sessions_added"] == 1
        assert any(
            "rollback incomplete" in item and sidecar.name in item for item in summary["items"]
        )
        assert not transcript.exists()
        assert not (target / files["attachment"].relative_to(snap)).exists()
        assert not (target / files["rotation"].relative_to(snap)).exists()
