"""Tests for what conversation history keeps and drops.

Covers the archives rotation and ``rewrite_session`` write (only the dropped
messages), archive cleanup and its retention config, the ``/api/session/archive``
endpoints, and ``delete_session`` with its summary sidecar and pinned-session
guard.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.history import ConversationLog


class TestArchive:
    def test_rotate_archives_dropped_lines(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 100)
        monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 3)
        log = ConversationLog(base_dir=tmp_path)
        for i in range(20):
            log.append("t1", "user", f"message number {i} with enough text to exceed limits")
        archives = list((tmp_path / "archive").glob("t1__*.jsonl"))
        assert len(archives) >= 1
        content = archives[0].read_text(encoding="utf-8")
        header = json.loads(content.splitlines()[0])
        assert header["_type"] == "archive"
        assert header["reason"] == "rotate"
        assert header["count"] > 0

    def test_rewrite_session_archives_existing(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "original msg 1")
        log.append("t1", "assistant", "original msg 2")
        log.rewrite_session("t1", [{"role": "user", "content": "new", "ts": "x"}])
        archives = list((tmp_path / "archive").glob("t1__*.jsonl"))
        assert len(archives) == 1
        content = archives[0].read_text(encoding="utf-8")
        assert "original msg 1" in content
        assert "original msg 2" in content
        header = json.loads(content.splitlines()[0])
        assert header["reason"] == "compact"

    def test_cleanup_old_archives(self, tmp_path):
        import os
        import time

        import kiro_crew.history as history_mod
        from kiro_crew.history import _cleanup_old_archives

        history_mod._last_cleanup = 0.0  # reset rate-limit so cleanup actually runs
        adir = tmp_path / "archive"
        adir.mkdir()
        old = adir / "old__20200101-000000.jsonl"
        old.write_text("{}\n")
        new = adir / "new__20990101-000000.jsonl"
        new.write_text("{}\n")
        # Backdate old file by 10 days
        # flake-ok: pre-existing real-clock backdating of the archive's mtime by ten days, moved verbatim; tracked for a de-flake pass
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old, (ten_days_ago, ten_days_ago))
        removed = _cleanup_old_archives(retention_days=7, base=tmp_path)
        assert removed == 1
        assert not old.exists()
        assert new.exists()

    def test_archive_empty_lines_noop(self, tmp_path):
        from kiro_crew.history import _archive_lines

        result = _archive_lines("k", [], reason="rotate", base=tmp_path)
        assert result is None
        assert not (tmp_path / "archive").exists()

    def test_same_second_conflict_suffixes_filename(self, tmp_path):
        """Multiple archives for same key in same second must not clobber each other."""
        from kiro_crew.history import _archive_lines

        p1 = _archive_lines("k", ["line1\n"], reason="rotate", base=tmp_path)
        p2 = _archive_lines("k", ["line2\n"], reason="rotate", base=tmp_path)
        p3 = _archive_lines("k", ["line3\n"], reason="rotate", base=tmp_path)
        assert len({p1, p2, p3}) == 3
        assert p1.exists() and p2.exists() and p3.exists()
        assert "line1" in p1.read_text(encoding="utf-8")
        assert "line2" in p2.read_text(encoding="utf-8")
        assert "line3" in p3.read_text(encoding="utf-8")

    def test_cleanup_old_archives_noop_when_dir_missing(self, tmp_path):
        import kiro_crew.history as history_mod
        from kiro_crew.history import _cleanup_old_archives

        history_mod._last_cleanup = 0.0
        removed = _cleanup_old_archives(retention_days=7, base=tmp_path)
        assert removed == 0

    def test_cleanup_disabled_when_retention_negative(self, tmp_path):
        """retention_days < 0 disables cleanup — old files are kept."""
        import os
        import time

        import kiro_crew.history as history_mod
        from kiro_crew.history import _cleanup_old_archives

        history_mod._last_cleanup = 0.0
        adir = tmp_path / "archive"
        adir.mkdir()
        old = adir / "old__20200101-000000.jsonl"
        old.write_text("{}\n")
        # flake-ok: pre-existing real-clock backdating of the archive's mtime by ten days, moved verbatim; tracked for a de-flake pass
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old, (ten_days_ago, ten_days_ago))
        removed = _cleanup_old_archives(retention_days=-1, base=tmp_path)
        assert removed == 0
        assert old.exists()

    def test_cleanup_resolves_retention_from_config(self, tmp_path, monkeypatch):
        """retention_days=None resolves the window from config."""
        import os
        import time

        import kiro_crew.history as history_mod
        from kiro_crew.history import _cleanup_old_archives

        monkeypatch.setattr(history_mod, "_resolve_retention_days", lambda: 7)
        history_mod._last_cleanup = 0.0
        adir = tmp_path / "archive"
        adir.mkdir()
        old = adir / "old__20200101-000000.jsonl"
        old.write_text("{}\n")
        # flake-ok: pre-existing real-clock backdating of the archive's mtime by ten days, moved verbatim; tracked for a de-flake pass
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old, (ten_days_ago, ten_days_ago))
        removed = _cleanup_old_archives(base=tmp_path)
        assert removed == 1
        assert not old.exists()

    def test_cleanup_throttled_skips_config_load(self, tmp_path, monkeypatch):
        """A rate-limited call must NOT resolve retention from config (Bug #6).

        Config resolution (KiroCrewConfig.load — a disk read + parse) is
        expensive and runs on every archive write via _archive_lines. The
        throttle guard must short-circuit BEFORE that read so the common
        once-per-hour-already-ran path stays cheap.
        """
        import time

        import kiro_crew.history as history_mod
        from kiro_crew.history import _cleanup_old_archives

        def _boom() -> int:
            raise AssertionError("config must not be loaded on a throttled call")

        monkeypatch.setattr(history_mod, "_resolve_retention_days", _boom)
        # Simulate a cleanup that ran moments ago → within the 1h window.
        # flake-ok: pre-existing real-clock cleanup stamp inside the one-hour throttle window, moved verbatim; tracked for a de-flake pass
        history_mod._last_cleanup = time.time()
        removed = _cleanup_old_archives(base=tmp_path)
        assert removed == 0

    def test_cleanup_throttled_explicit_negative_skips_config_load(self, tmp_path, monkeypatch):
        """Explicit negative disables without touching config, even when throttled."""
        import time

        import kiro_crew.history as history_mod
        from kiro_crew.history import _cleanup_old_archives

        def _boom() -> int:
            raise AssertionError("config must not be loaded for explicit negative")

        monkeypatch.setattr(history_mod, "_resolve_retention_days", _boom)
        # flake-ok: pre-existing real-clock cleanup stamp inside the one-hour throttle window, moved verbatim; tracked for a de-flake pass
        history_mod._last_cleanup = time.time()
        removed = _cleanup_old_archives(retention_days=-1, base=tmp_path)
        assert removed == 0

    def test_cleanup_config_disabled_stamps_window_to_throttle_next_call(
        self, tmp_path, monkeypatch
    ):
        """A config-resolved 'disabled' must still stamp _last_cleanup (Bug #6).

        If the throttle window is not stamped when retention resolves negative,
        every subsequent archive write re-runs the expensive config load. The
        first call should resolve config once; the immediate next call must be
        throttled and NOT resolve config again.
        """
        import kiro_crew.history as history_mod
        from kiro_crew.history import _cleanup_old_archives

        calls = {"n": 0}

        def _disabled() -> int:
            calls["n"] += 1
            return -1  # cleanup disabled via config

        monkeypatch.setattr(history_mod, "_resolve_retention_days", _disabled)
        history_mod._last_cleanup = 0.0  # force first call past the throttle
        assert _cleanup_old_archives(base=tmp_path) == 0
        assert calls["n"] == 1  # config resolved once
        # Immediate second call must be throttled → config NOT resolved again.
        assert _cleanup_old_archives(base=tmp_path) == 0
        assert calls["n"] == 1

    def test_safe_key_sanitizes_unsafe_chars(self, tmp_path):
        """Keys with slashes/colons must be sanitized into safe filenames."""
        from kiro_crew.history import _archive_lines, _safe_key

        assert _safe_key("slack:C123/456") == "slack_C123_456"
        p = _archive_lines("slack:C123/456", ["x\n"], reason="rotate", base=tmp_path)
        assert p is not None
        assert "/" not in p.name and ":" not in p.name
        assert p.name.startswith("slack_C123_456__")

    def test_multiple_rotations_produce_multiple_archives(self, tmp_path, monkeypatch):
        """A session that keeps growing across multiple rotate cycles produces multiple archive files."""
        monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 200)
        monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 2)
        log = ConversationLog(base_dir=tmp_path)
        for _ in range(3):
            # Each round writes enough to trigger a rotate
            for i in range(20):
                log.append("loop", "user", f"msg {i} " + "x" * 50)
        archives = list((tmp_path / "archive").glob("loop__*.jsonl"))
        assert len(archives) >= 2, f"expected multiple archives, got {len(archives)}"

    def test_archive_header_is_valid_json_metadata_line(self, tmp_path):
        """First line of archive is a JSON metadata row; remaining lines are original message jsonl."""
        from kiro_crew.history import _archive_lines

        p = _archive_lines(
            "k",
            ['{"role":"user","content":"a"}\n', '{"role":"assistant","content":"b"}\n'],
            reason="rotate",
            base=tmp_path,
        )
        lines = p.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        assert header == {
            "_type": "archive",
            "reason": "rotate",
            "archived_at": header["archived_at"],
            "count": 2,
        }
        assert json.loads(lines[1])["role"] == "user"
        assert json.loads(lines[2])["role"] == "assistant"


class TestArchiveDashboardAPI:
    """HTTP-level tests for /api/session/archive endpoints."""

    @staticmethod
    def _make_app():
        import pytest

        pytest.importorskip("aiohttp")
        from aiohttp import web

        from kiro_crew.dashboard.handlers import (
            api_session_archive_list,
            api_session_archive_read,
        )

        app = web.Application()
        app.router.add_get("/api/session/archive", api_session_archive_list)
        app.router.add_get("/api/session/archive/{name}", api_session_archive_read)
        # Handler resolves archive dir via _sessions_dir(); tests monkeypatch that.
        return app

    @pytest.fixture
    def archive_dir(self, tmp_path, monkeypatch):
        """Create an archive dir seeded with fake archive files and wire _sessions_dir()."""
        import os
        import time

        import kiro_crew.history as history_mod

        sessions = tmp_path / "sessions"
        archive = sessions / "archive"
        archive.mkdir(parents=True)
        # flake-ok: pre-existing real-clock base for the archives' relative mtimes, moved verbatim; tracked for a de-flake pass
        now = time.time()
        # Oldest mtime
        (archive / "a__20260101-000000.jsonl").write_text(
            '{"_type":"archive","reason":"rotate","count":1}\n{"role":"user","content":"x"}\n'
        )
        os.utime(archive / "a__20260101-000000.jsonl", (now - 300, now - 300))
        # Newest mtime (should sort first)
        (archive / "b__20260102-000000.jsonl").write_text(
            '{"_type":"archive","reason":"compact","count":1}\n{"role":"user","content":"y"}\n'
        )
        os.utime(archive / "b__20260102-000000.jsonl", (now, now))
        # Middle mtime
        (archive / "a__20260103-000000.jsonl").write_text(
            '{"_type":"archive","reason":"rotate","count":1}\n{"role":"user","content":"z"}\n'
        )
        os.utime(archive / "a__20260103-000000.jsonl", (now - 100, now - 100))
        monkeypatch.setattr(history_mod, "_sessions_dir", lambda: sessions)
        return archive

    @pytest.mark.asyncio
    async def test_list_returns_all_archives(self, archive_dir):
        from aiohttp.test_utils import TestClient, TestServer

        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/session/archive")
            assert resp.status == 200
            data = await resp.json()
            assert len(data["archives"]) == 3
            # Sorted newest first by mtime (not filename)
            assert data["archives"][0]["name"] == "b__20260102-000000.jsonl"
            assert data["archives"][1]["name"] == "a__20260103-000000.jsonl"
            assert data["archives"][2]["name"] == "a__20260101-000000.jsonl"
            assert set(e["key"] for e in data["archives"]) == {"a", "b"}

    @pytest.mark.asyncio
    async def test_list_key_prefix_filter(self, archive_dir):
        from aiohttp.test_utils import TestClient, TestServer

        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/session/archive?key=a")
            data = await resp.json()
            assert len(data["archives"]) == 2
            assert all(e["key"] == "a" for e in data["archives"])

    @pytest.mark.asyncio
    async def test_list_empty_when_no_archive_dir(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer

        import kiro_crew.history as history_mod

        sessions = tmp_path / "sessions"
        sessions.mkdir()
        monkeypatch.setattr(history_mod, "_sessions_dir", lambda: sessions)
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/session/archive")
            assert resp.status == 200
            data = await resp.json()
            assert data["archives"] == []

    @pytest.mark.asyncio
    async def test_read_returns_archive_content(self, archive_dir):
        from aiohttp.test_utils import TestClient, TestServer

        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/session/archive/a__20260101-000000.jsonl")
            assert resp.status == 200
            body = await resp.text()
            assert "x" in body and "archive" in body

    @pytest.mark.asyncio
    async def test_read_rejects_path_traversal(self, archive_dir):
        from aiohttp.test_utils import TestClient, TestServer

        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # Names with '..' must be rejected by the handler's canonical path check.
            # '..' alone (no slash) reaches the handler; canonical-resolve check catches it.
            # URL-encoded slashes ('..%2Fetc.jsonl') may be rejected by the router (404)
            # or by the handler (400) depending on aiohttp version — both are acceptable.
            for bad, expected in [
                ("..", (400, 404)),  # missing .jsonl → 400, or no match → 404
                ("...jsonl", (400, 404)),  # may resolve inside dir → 404, or caught → 400
                ("..%2Fetc.jsonl", (400, 403, 404)),
                ("..%2F..%2Fetc.jsonl", (400, 403, 404)),
            ]:
                resp = await client.get(f"/api/session/archive/{bad}")
                assert resp.status in expected, f"{bad} returned {resp.status}"

    @pytest.mark.asyncio
    async def test_read_rejects_non_jsonl_extension(self, archive_dir):
        from aiohttp.test_utils import TestClient, TestServer

        # Put a forbidden file alongside archives
        (archive_dir / "secret.txt").write_text("SHOULD NOT BE READABLE")
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/session/archive/secret.txt")
            assert resp.status in (400, 403, 404)

    @pytest.mark.asyncio
    async def test_read_missing_archive_returns_404(self, archive_dir):
        from aiohttp.test_utils import TestClient, TestServer

        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/session/archive/nonexistent.20260101-000000.jsonl")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_read_redacts_credentials_and_urls(self, archive_dir):
        """Archived content is redacted (credentials + exfiltration URLs) before being served."""
        from aiohttp.test_utils import TestClient, TestServer

        # Write an archive containing a fake AWS access key
        leaky = archive_dir / "leak__20260104-000000.jsonl"
        leaky.write_text(
            '{"_type":"archive","reason":"rotate","count":1}\n'
            '{"role":"user","content":"here is AKIAIOSFODNN7EXAMPLE my key"}\n'
        )
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/session/archive/leak__20260104-000000.jsonl")
            assert resp.status == 200
            body = await resp.text()
            # Raw credential must not appear in the response
            assert "AKIAIOSFODNN7EXAMPLE" not in body


class TestArchiveOnlyDropped:
    """rewrite_session must archive only the messages being dropped, not kept ones."""

    def test_rewrite_archives_only_dropped_messages(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "A")
        log.append("t1", "assistant", "B")
        log.append("t1", "user", "C")
        # Read back the three message lines so we can feed them exactly to rewrite_session
        from kiro_crew.history import _safe_key

        path = tmp_path / f"{_safe_key('t1')}.jsonl"
        lines = [
            ln for ln in path.read_text(encoding="utf-8").splitlines() if ln and '"_type"' not in ln
        ]
        assert len(lines) == 3
        kept = [json.loads(lines[1]), json.loads(lines[2])]  # B, C
        log.rewrite_session("t1", kept)
        archives = list((tmp_path / "archive").glob("t1__*.jsonl"))
        assert len(archives) == 1
        archived = archives[0].read_text(encoding="utf-8")
        # Only the dropped message A should be in the archive (not B or C).
        assert '"content": "A"' in archived
        assert '"content": "B"' not in archived
        assert '"content": "C"' not in archived
        header = json.loads(archived.splitlines()[0])
        assert header["count"] == 1


class TestDeleteSessionSummarySidecar:
    def test_delete_session_removes_summary_sidecar(self, tmp_path):
        # delete_session is a *permanent* removal — the derived one-line summary
        # sidecar must not survive the session it describes (Arbiter data-lifecycle).
        log = ConversationLog(base_dir=tmp_path)
        log.append("thread-sum", "user", "tune the redis timeout")
        sig = log.session_mtime("thread-sum")
        assert sig is not None
        log.set_cached_summary("thread-sum", "Tuning redis timeout", sig)
        sidecar = log._summary_cache_path("thread-sum")
        assert sidecar.exists()

        assert log.delete_session("thread-sum") is True
        assert not sidecar.exists()
        assert log.get_cached_summary("thread-sum") is None

    def test_delete_session_without_summary_is_fine(self, tmp_path):
        # No sidecar present → delete still succeeds, no error.
        log = ConversationLog(base_dir=tmp_path)
        log.append("thread-nosum", "user", "hello")
        assert log.delete_session("thread-nosum") is True
        assert not log._summary_cache_path("thread-nosum").exists()

    def test_delete_session_skip_pinned_protects_pinned_sessions(self, tmp_path):
        """skip_pinned=True returns None for pinned sessions under the REAL lock.

        Regression test for the layering fix: the pin-check-and-delete invariant
        now lives in ConversationLog.delete_session rather than in a handler closure.
        This test exercises the real _locked codepath, NOT a mocked context manager,
        so a regression that breaks lock reentrancy will actually fail.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("sess-pinned", "user", "important")
        log.update_metadata("sess-pinned", {"pinned": True})
        log.append("sess-unpinned", "user", "ephemeral")

        # Pinned session: skip_pinned=True returns None, file survives
        assert log.delete_session("sess-pinned", skip_pinned=True) is None
        assert log._path("sess-pinned").exists()

        # Unpinned session: skip_pinned=True returns True, file is deleted
        assert log.delete_session("sess-unpinned", skip_pinned=True) is True
        assert not log._path("sess-unpinned").exists()

    def test_delete_session_skip_pinned_skips_unreadable_metadata(self, tmp_path):
        """skip_pinned=True returns None when get_metadata_status returns unreadable.

        Simulates a Windows indexer/AV hold that makes the metadata transiently
        unreadable. The session is skipped (not deleted blind) and can be retried.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("sess-transient", "user", "might be locked")

        # Patch get_metadata_status to simulate transient unreadability
        original = log.get_metadata_status

        def _unreadable(key):
            if key == "sess-transient":
                return {}, False  # readable=False
            return original(key)

        log.get_metadata_status = _unreadable

        # skip_pinned=True returns None, file survives
        assert log.delete_session("sess-transient", skip_pinned=True) is None
        assert log._path("sess-transient").exists()

    def test_delete_session_skip_pinned_logs_on_exception(self, tmp_path, caplog):
        """skip_pinned=True logs and returns None when get_metadata_status raises.

        Corrupt metadata or permanent I/O failure should be diagnosable via logs.
        """
        import logging

        log = ConversationLog(base_dir=tmp_path)
        log.append("sess-corrupt", "user", "bad metadata")

        # Patch to simulate corrupt metadata raising
        def _corrupt_meta(key):
            if key == "sess-corrupt":
                raise ValueError("corrupt JSON")
            return log.get_metadata(key), True

        log.get_metadata_status = _corrupt_meta

        with caplog.at_level(logging.WARNING):
            result = log.delete_session("sess-corrupt", skip_pinned=True)

        assert result is None
        assert log._path("sess-corrupt").exists()
        assert "unexpected error reading metadata" in caplog.text
        assert "sess-corrupt" in caplog.text

    def test_delete_session_skip_pinned_false_deletes_pinned(self, tmp_path):
        """skip_pinned=False (default) deletes even pinned sessions.

        Ensures the default behavior is unchanged for callers that don't use skip_pinned.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("sess-pinned-force", "user", "pinned but forced")
        log.update_metadata("sess-pinned-force", {"pinned": True})

        # Without skip_pinned, pinned sessions ARE deleted
        assert log.delete_session("sess-pinned-force") is True
        assert not log._path("sess-pinned-force").exists()
