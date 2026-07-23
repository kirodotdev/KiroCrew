"""Regression tests for atomic + locked transcript rewrites (Track A, bug 4).

ConversationLog.mark_consolidated (and the other full-rewrite paths) must:
- write atomically (temp + fsync + os.replace) so a crash can't truncate the
  transcript, and
- serialize behind a per-file lock so a concurrent append is never lost and
  readers never observe a torn file.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from kiro_crew.history import ConversationLog


class TestMarkConsolidatedAtomic:
    def test_preserves_messages_and_updates_offset(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "m1")
        log.append("k", "assistant", "m2")
        log.append("k", "user", "m3")

        log.mark_consolidated("k", 2)

        fresh = ConversationLog(base_dir=tmp_path)
        msgs = fresh._read_messages("k")
        assert [m["content"] for m in msgs] == ["m1", "m2", "m3"]
        assert fresh.get_metadata("k")["last_consolidated"] == 2

    def test_leaves_no_temp_files(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "m1")
        log.mark_consolidated("k", 1)
        assert list(tmp_path.glob("*.tmp")) == []

    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        # No file for this key — must not raise.
        log.mark_consolidated("nope", 0)


class TestMarkConsolidatedOffloaded:
    @pytest.mark.asyncio
    async def test_mark_consolidated_runs_off_loop_thread(self) -> None:
        """mark_consolidated does a synchronous fsync-backed transcript rewrite
        (up to a couple MB) behind the per-file lock. _consolidate runs on the
        gateway event loop, so it MUST offload the rewrite to a worker thread —
        otherwise a slow filesystem freezes the loop (heartbeats, Slack,
        dashboard). Regression guard for the Codex HIGH loop-stall finding."""
        import threading
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.history import HistoryConsolidator

        loop_thread_id = threading.get_ident()
        mark_thread_id: dict[str, int] = {}

        log = MagicMock()
        log.get_unconsolidated.return_value = ([{"role": "user", "content": "hi"}], 1)
        log.get_metadata.return_value = {}
        log.mark_consolidated.side_effect = lambda *a, **k: mark_thread_id.__setitem__(
            "id", threading.get_ident()
        )

        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""
        vector_store = MagicMock()
        vector_store.get_all_semantic.return_value = []

        c = HistoryConsolidator(
            log=log, memory=memory, sessions=None,
            vector_store=vector_store, migrated=True,
        )
        with patch.object(c, "_call_llm", new_callable=AsyncMock) as llm, \
                patch.object(c, "_write_structured_memory"):
            llm.return_value = {"episodic": [{"text": "x" * 20}]}
            await c._consolidate("k", include_history=True)

        assert mark_thread_id.get("id") is not None, "mark_consolidated was not called"
        assert mark_thread_id["id"] != loop_thread_id, (
            "mark_consolidated ran on the event loop thread — its fsync-backed "
            "rewrite must be offloaded via asyncio.to_thread()."
        )


class TestConcurrentAppendVsRewrite:
    def test_no_message_lost_and_never_torn(self, tmp_path: Path) -> None:
        """Hammer append (live session) against mark_consolidated/rewrite
        (compaction) from separate threads. The per-file lock must guarantee
        every appended message survives and the file always stays parseable."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "seed")

        stop = threading.Event()
        appended: list[str] = []
        errors: list[Exception] = []

        def appender() -> None:
            i = 0
            while not stop.is_set():
                content = f"msg-{i}"
                try:
                    log.append("k", "user", content)
                    appended.append(content)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                i += 1

        def consolidator() -> None:
            reader = ConversationLog(base_dir=tmp_path)
            while not stop.is_set():
                try:
                    log.mark_consolidated("k", 1)
                    # Concurrent read must never see a truncated/torn file.
                    path = reader._path("k")
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            json.loads(line)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [
            threading.Thread(target=appender),
            threading.Thread(target=consolidator),
        ]
        for t in threads:
            t.start()
        time.sleep(0.4)
        stop.set()
        for t in threads:
            t.join()

        assert errors == []
        fresh = ConversationLog(base_dir=tmp_path)
        contents = [m["content"] for m in fresh._read_messages("k")]
        for a in appended:
            assert a in contents
