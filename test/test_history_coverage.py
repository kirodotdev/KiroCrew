"""Coverage tests for :mod:`kiro_crew.history`.

Targets whole uncovered helpers and error branches rather than the happy paths
the existing ``test_history*.py`` files already exercise:

- the off-loop persistence wrappers' inline / offloaded failure reporting,
- pure text helpers (``_content_text``, ``transcript_stems``, the tool-call and
  sensitive-path scanners),
- bounded tail readers (``_read_tail_messages``, ``last_message_preview``),
- ``list_sessions`` metadata fallbacks and canonical-key dedupe,
- ``clear_closed``'s compare-and-clear guards,
- the durable consolidation retry accounting (environment failures, bad
  persisted values) and ``HistoryConsolidator``'s ``_note_*`` bookkeeping,
- the structured-memory / lesson writers and the two background-session LLM
  turns (``_dedupe_judge``, ``_merge_skill_update``).

Every test writes only under ``tmp_path``; no network, no subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import history as H
from kiro_crew.history import (
    _CONSOLIDATION_BACKOFF_BASE_SECS,
    _CONSOLIDATION_BACKOFF_MAX_SECS,
    _CONSOLIDATION_MAX_ATTEMPTS,
    AttemptedSpan,
    ConversationLog,
    HistoryConsolidator,
)
from kiro_crew.session import BACKGROUND_KEY
from kiro_crew.vector_memory import SemanticRejectCode


def _write(path: Path, text: str) -> None:
    """Write *text* with LF endings on every platform (Windows would translate)."""
    path.write_text(text, encoding="utf-8", newline="\n")


def _jsonl(*rows: dict) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def _log(tmp_path: Path) -> ConversationLog:
    return ConversationLog(base_dir=tmp_path)


def _consolidator(**kw) -> HistoryConsolidator:
    kw.setdefault("log", MagicMock())
    kw.setdefault("memory", MagicMock())
    return HistoryConsolidator(**kw)


# ── off-loop persistence wrappers ──────────────────────────────────────────


class TestOffLoopWrappers:
    """The three ``*_off_loop`` helpers must swallow-and-log inline failures and
    report offloaded ones through the done-callback."""

    def test_append_off_loop_inline_failure_is_logged(self, caplog) -> None:
        log = MagicMock()
        log.append.side_effect = OSError("disk gone")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            H.append_off_loop(log, "k", "user", "hi", agent="a")
        assert "inline append failed" in caplog.text
        log.append.assert_called_once_with("k", "user", "hi", agent="a")

    def test_append_rows_groups_the_pair_under_one_atomic_hold(self, tmp_path) -> None:
        """A prompt+result pair must not be two independent off-loop writes.

        Dispatched separately, two worker threads can land them out of order or
        land one alone, leaving the replay with a reversed or half-written run
        that no timestamp ordering repairs. The rows go through one
        ``atomic_appends`` hold instead, which is that contract's stated
        companion for a multi-append caller that offloads.
        """
        log = H.ConversationLog(base_dir=tmp_path)
        holds: list[str] = []
        real_atomic = log.atomic_appends

        @contextlib.contextmanager
        def _spy(key: str):
            holds.append(key)
            with real_atomic(key):
                yield

        with patch.object(log, "atomic_appends", _spy):
            H.append_rows_if_absent_off_loop(
                log,
                "cron:abc",
                [
                    ("user", "the prompt", "msg msg-u", "m-1"),
                    ("assistant", "the result", "msg msg-a", "m-2"),
                ],
            )
        assert holds == ["cron:abc"], "the pair must share exactly one atomic hold"
        rows = log.read_messages("cron:abc")
        assert [r["role"] for r in rows] == ["user", "assistant"], "order must be preserved"
        assert [r["meta"]["mid"] for r in rows] == ["m-1", "m-2"]

    def test_append_rows_keeps_per_row_idempotence(self, tmp_path) -> None:
        """A row the slot save already persisted is skipped without dropping its sibling."""
        log = H.ConversationLog(base_dir=tmp_path)
        log.append("cron:abc", "user", "the prompt", mid="m-1")
        H.append_rows_if_absent_off_loop(
            log,
            "cron:abc",
            [
                ("user", "the prompt", "msg msg-u", "m-1"),
                ("assistant", "the result", "msg msg-a", "m-2"),
            ],
        )
        rows = log.read_messages("cron:abc")
        assert [r["role"] for r in rows] == ["user", "assistant"], (
            "the already-present prompt must not be duplicated, and must not "
            "suppress the result"
        )

    def test_append_if_absent_off_loop_inline_failure_is_logged(self, caplog) -> None:
        log = MagicMock()
        log.append_if_absent.side_effect = OSError("disk gone")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            H.append_if_absent_off_loop(log, "k", "assistant", "yo")
        assert "inline append failed" in caplog.text

    def test_update_metadata_off_loop_inline_failure_is_logged(self, caplog) -> None:
        log = MagicMock()
        log.update_metadata.side_effect = OSError("disk gone")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            H.update_metadata_off_loop(log, "k", {"title": "t"})
        assert "inline update failed" in caplog.text

    def test_off_loop_wrappers_run_inline_when_no_loop(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        H.append_off_loop(log, "k", "user", "one")
        H.append_if_absent_off_loop(log, "k", "user", "one")  # idempotent → no dup
        H.append_if_absent_off_loop(log, "k", "assistant", "two")
        H.update_metadata_off_loop(log, "k", {"title": "T"})
        assert [m["content"] for m in log._read_messages("k")] == ["one", "two"]
        assert log.get_metadata("k")["title"] == "T"

    @pytest.mark.asyncio
    async def test_append_off_loop_offloads_and_reports_failure(self, caplog) -> None:
        """On a running loop the write is dispatched to an executor, and the
        done-callback surfaces the exception instead of losing it."""
        done = asyncio.Event()
        log = MagicMock()
        log.append.side_effect = OSError("nope")
        loop = asyncio.get_running_loop()
        real_run_in_executor = loop.run_in_executor
        captured: dict[str, object] = {}

        def _spy(executor, func, *args):
            fut = real_run_in_executor(executor, func, *args)
            captured["fut"] = fut
            fut.add_done_callback(lambda _f: loop.call_soon_threadsafe(done.set))
            return fut

        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            with patch.object(loop, "run_in_executor", _spy):
                H.append_off_loop(log, "k", "user", "hi")
            await asyncio.wait_for(done.wait(), timeout=5)
            await asyncio.sleep(0)
        assert "offloaded append failed" in caplog.text
        assert captured["fut"].done()

    @pytest.mark.asyncio
    async def test_update_metadata_off_loop_offloads_to_executor(
        self, tmp_path: Path
    ) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        loop = asyncio.get_running_loop()
        futures: list[asyncio.Future] = []
        real_run_in_executor = loop.run_in_executor

        def _spy(executor, func, *args):
            fut = real_run_in_executor(executor, func, *args)
            futures.append(fut)
            return fut

        with patch.object(loop, "run_in_executor", _spy):
            H.update_metadata_off_loop(log, "k", {"title": "later"})
        assert futures, "update_metadata_off_loop must dispatch to an executor"
        await asyncio.wait_for(asyncio.shield(futures[0]), timeout=5)
        assert _log(tmp_path).get_metadata("k")["title"] == "later"

    @pytest.mark.asyncio
    async def test_append_if_absent_off_loop_offloads_to_executor(
        self, tmp_path: Path
    ) -> None:
        log = _log(tmp_path)
        loop = asyncio.get_running_loop()
        futures: list[asyncio.Future] = []
        real_run_in_executor = loop.run_in_executor

        def _spy(executor, func, *args):
            fut = real_run_in_executor(executor, func, *args)
            futures.append(fut)
            return fut

        with patch.object(loop, "run_in_executor", _spy):
            H.append_if_absent_off_loop(log, "k", "user", "durable")
        assert futures, "append_if_absent_off_loop must dispatch to an executor"
        await asyncio.wait_for(asyncio.shield(futures[0]), timeout=5)
        assert [m["content"] for m in _log(tmp_path)._read_messages("k")] == ["durable"]


# ── pure helpers ───────────────────────────────────────────────────────────


class TestContentText:
    @pytest.mark.parametrize(
        "content,expected",
        [
            ("  hello  ", "hello"),
            ([], ""),
            (["a", "  b  "], "a b"),
            ([{"type": "text", "text": "block"}], "block"),
            ([{"type": "text", "text": "   "}], ""),
            ([{"type": "image"}, {"text": "second"}], "second"),
            ([{"text": 42}], ""),
            (["x", {"text": "y"}], "x y"),
            (None, ""),
            (17, ""),
            ({"text": "dicts are not lists"}, ""),
        ],
    )
    def test_content_text(self, content, expected) -> None:
        assert ConversationLog._content_text(content) == expected


class TestTranscriptStems:
    def test_plain_key_has_one_stem(self) -> None:
        assert H.transcript_stems("dashboard:chat-1") == (H._safe_key("dashboard:chat-1"),)

    def test_legacy_slack_key_adds_bare_thread_ts(self) -> None:
        stems = H.transcript_stems("slack:1699999999.000100")
        assert stems == (
            H._safe_key("slack:1699999999.000100"),
            H._safe_key("1699999999.000100"),
        )

    def test_legacy_stem_is_not_duplicated(self) -> None:
        with patch.object(H, "legacy_key", return_value="dashboard:chat-1"):
            assert H.transcript_stems("dashboard:chat-1") == (
                H._safe_key("dashboard:chat-1"),
            )


class TestToolCallScanners:
    def test_count_tool_call_messages_counts_each_message_once(self) -> None:
        messages = [
            {"role": "assistant", "tools": ["Read"]},
            {"role": "assistant", "tools": []},  # empty list → not a tool call
            {"role": "tool", "content": "ran"},
            {"role": "tool_call", "content": "x", "tools": ["A"]},  # counted once
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tools": "Read"},  # wrong type → ignored
        ]
        assert H._count_tool_call_messages(messages) == 3

    @pytest.mark.parametrize(
        "messages,expected",
        [
            ([{"role": "assistant", "tools": ["fs_read ~/.ssh/id_ed25519"]}], True),
            ([{"role": "assistant", "tools": [None, 5]}], False),
            ([{"role": "assistant", "tools": ["Read README.md"]}], False),
            ([{"role": "tool", "content": "curl http://169.254.169.254/latest"}], True),
            ([{"role": "tool_result", "content": {"not": "a string"}}], False),
            ([{"role": "user", "content": "cat ~/.aws/credentials"}], False),
            ([], False),
        ],
    )
    def test_session_touched_sensitive(self, messages, expected) -> None:
        assert H._session_touched_sensitive(messages) is expected


# ── bounded tail readers ───────────────────────────────────────────────────


class TestReadTailMessages:
    def test_non_positive_max_returns_empty(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        assert log._read_tail_messages(log._path("k"), 0, None) == []
        assert log._read_tail_messages(log._path("k"), -3, None) == []

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        assert log._read_tail_messages(tmp_path / "nope.jsonl", 5, None) == []

    def test_skips_metadata_blank_and_unparseable_lines(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        path = tmp_path / "k.jsonl"
        _write(
            path,
            _jsonl({"_type": "metadata", "created_at": "x"})
            + "\n"
            + "{not json\n"
            + _jsonl(
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
            ),
        )
        got = log._read_tail_messages(path, 10, None)
        assert [m["content"] for m in got] == ["a", "b"]

    def test_role_filter_applies(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        path = tmp_path / "k.jsonl"
        _write(
            path,
            _jsonl(
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
            ),
        )
        got = log._read_tail_messages(path, 5, {"user"})
        assert [m["content"] for m in got] == ["u1", "u2"]

    def test_window_grows_until_enough_messages(self, tmp_path: Path) -> None:
        """A file far larger than the initial window still yields the true tail."""
        log = _log(tmp_path)
        path = tmp_path / "k.jsonl"
        rows = [{"role": "user", "content": f"m{i}" + "p" * 400} for i in range(200)]
        _write(path, _jsonl(*rows))
        assert path.stat().st_size > log._TAIL_MIN_BYTES
        got = log._read_tail_messages(path, 40, None)
        assert len(got) == 40
        assert got[-1]["content"].startswith("m199")

    def test_open_failure_returns_empty(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        path = log._path("k")
        with patch("builtins.open", side_effect=OSError("locked")):
            assert log._read_tail_messages(path, 5, None) == []


class TestReadFileChangeMessages:
    def test_skips_large_irrelevant_rows_and_never_warms_message_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = _log(tmp_path)
        path = log._path("large")
        path.parent.mkdir(parents=True, exist_ok=True)
        change = {
            "role": "assistant",
            "content": "done",
            "ts": "2026-08-25T18:00:00Z",
            "meta": {"file_changes": [{"path": "/tmp/report.md"}]},
        }
        _write(
            path,
            _jsonl(
                {"_type": "metadata"},
                {"role": "assistant", "content": "x" * 1_000_000},
                change,
            ),
        )

        real_loads = H.json.loads
        parsed_sizes: list[int] = []

        def tracking_loads(value, *args, **kwargs):
            parsed_sizes.append(len(value))
            return real_loads(value, *args, **kwargs)

        monkeypatch.setattr(H.json, "loads", tracking_loads)
        rows = log.read_file_change_messages("large")

        assert rows == [
            {
                "ts": "2026-08-25T18:00:00Z",
                "meta": {"file_changes": [{"path": "/tmp/report.md"}]},
            }
        ]
        assert len(parsed_sizes) == 1
        assert parsed_sizes[0] < 1_000
        assert len(log._msg_cache) == 0

        def unexpected_parse(*_args, **_kwargs):
            pytest.fail("unchanged projection should come from its lightweight cache")

        monkeypatch.setattr(H.json, "loads", unexpected_parse)
        assert log.read_file_change_messages("large") is rows

    def test_changed_file_replaces_cached_projection(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        path = log._path("changed")
        path.parent.mkdir(parents=True, exist_ok=True)
        first = {
            "role": "assistant",
            "ts": "2026-08-25T18:00:00Z",
            "meta": {"file_changes": [{"path": "/tmp/old.md"}]},
        }
        second = {
            "role": "assistant",
            "ts": "2026-08-25T18:01:00Z",
            "meta": {"file_changes": [{"path": "/tmp/new-report.md"}]},
        }
        _write(path, _jsonl({"_type": "metadata"}, first))
        assert log.read_file_change_messages("changed")[0]["meta"] == first["meta"]

        _write(path, _jsonl({"_type": "metadata"}, second))

        assert log.read_file_change_messages("changed") == [
            {"ts": second["ts"], "meta": second["meta"]}
        ]

    def test_session_write_invalidates_cached_projection(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(
            log._path("written"),
            _jsonl(
                {"_type": "metadata"},
                {
                    "role": "assistant",
                    "content": "first",
                    "meta": {"file_changes": [{"path": "/tmp/first.md"}]},
                },
            ),
        )
        assert log.read_file_change_messages("written")
        assert "written" in log._file_change_cache

        log.append("written", "assistant", "no document change")

        assert "written" not in log._file_change_cache


class TestLastMessagePreview:
    def test_missing_file(self, tmp_path: Path) -> None:
        assert _log(tmp_path).last_message_preview("nope") == ""

    def test_metadata_only_file(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(log._path("k"), _jsonl({"_type": "metadata", "created_at": "x"}))
        assert log.last_message_preview("k") == ""

    def test_returns_newest_message(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "first")
        log.append("k", "assistant", "second")
        assert log.last_message_preview("k") == "second"

    def test_structured_content_blocks(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(
            log._path("k"),
            _jsonl(
                {"_type": "metadata"},
                {"role": "assistant", "content": [{"type": "text", "text": "blocky"}]},
            ),
        )
        assert log.last_message_preview("k") == "blocky"

    def test_skips_unparseable_and_empty_content_rows(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(
            log._path("k"),
            _jsonl({"role": "user", "content": "real"})
            + "{ broken\n"
            + "\n"
            + _jsonl({"role": "assistant", "content": ""}),
        )
        assert log.last_message_preview("k") == "real"

    def test_truncates_long_preview_with_ellipsis(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "assistant", "abcdefghij" * 40)
        preview = log.last_message_preview("k")
        assert preview.endswith("…")
        assert len(preview) == log._PREVIEW_MAX_CHARS + 1

    def test_retries_with_wider_window(self, tmp_path: Path) -> None:
        """A single trailing line larger than the first window is only reachable
        on the 16x retry — the first pass discards it as a partial line."""
        log = _log(tmp_path)
        big = "z" * (log._PREVIEW_TAIL_BYTES * 2)
        _write(
            log._path("k"),
            _jsonl({"_type": "metadata"}, {"role": "assistant", "content": big}),
        )
        preview = log.last_message_preview("k")
        assert preview.startswith("z")

    def test_open_failure_returns_empty(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        with patch("builtins.open", side_effect=OSError("locked")):
            assert log.last_message_preview("k") == ""

    def test_unpreviewable_markdown_yields_empty(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "assistant", "text")
        with patch("kiro_crew.history.strip_markdown_preview", return_value=""):
            assert log.last_message_preview("k") == ""


# ── list_sessions ──────────────────────────────────────────────────────────


class TestListSessions:
    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert ConversationLog(base_dir=tmp_path / "absent").list_sessions() == []

    def test_metadata_line_supplies_title_agent_folder(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(
            tmp_path / "s1.jsonl",
            _jsonl(
                {
                    "_type": "metadata",
                    "created_at": "2026-01-01T00:00:00",
                    "title": "Titled",
                    "agent": "kirocrew",
                    "memory_mode": "ephemeral",
                    "folder_id": "f1",
                },
                {"role": "user", "content": "hi"},
            ),
        )
        (row,) = log.list_sessions()
        assert row["title"] == "Titled"
        assert row["agent"] == "kirocrew"
        assert row["memory_mode"] == "ephemeral"
        assert row["folder_id"] == "f1"
        assert row["created"] == "2026-01-01T00:00:00"

    def test_metadata_cache_hit_is_used(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(tmp_path / "s1.jsonl", _jsonl({"_type": "metadata"}))
        mtime = (tmp_path / "s1.jsonl").stat().st_mtime
        log._meta_cache["s1"] = (
            mtime,
            log._cache_gen("s1"),
            {
                "_type": "metadata",
                "created_at": "cached-at",
                "title": "cached",
                "agent": "cached-agent",
                "memory_mode": "ephemeral",
                "folder_id": "cf",
            },
        )
        (row,) = log.list_sessions()
        assert (row["title"], row["agent"], row["created"]) == (
            "cached",
            "cached-agent",
            "cached-at",
        )
        assert row["folder_id"] == "cf"

    def test_first_user_message_is_title_fallback(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(
            tmp_path / "s1.jsonl",
            _jsonl({"_type": "metadata"})
            + "\n"
            + "{ broken\n"
            + _jsonl(
                {"role": "assistant", "content": "ignored"},
                {"role": "user", "content": "from message"},
            ),
        )
        (row,) = log.list_sessions()
        assert row["title"] == "from message"

    def test_message_cache_supplies_title_fallback(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(tmp_path / "s1.jsonl", _jsonl({"_type": "metadata"}))
        mtime = (tmp_path / "s1.jsonl").stat().st_mtime
        log._msg_cache["s1"] = (
            mtime,
            log._cache_gen("s1"),
            [
                {"role": "assistant", "content": "skip"},
                {"role": "user", "content": "cached title"},
            ],
        )
        (row,) = log.list_sessions()
        assert row["title"] == "cached title"

    def test_title_defaults_to_key(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(tmp_path / "s1.jsonl", _jsonl({"_type": "metadata"}))
        (row,) = log.list_sessions()
        assert row["title"] == "s1"
        assert row["memory_mode"] == "persistent"

    def test_stacked_dashboard_prefixes_dedupe_to_newest(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(tmp_path / "dashboard_chat-1.jsonl", _jsonl({"_type": "metadata"}))
        _write(
            tmp_path / "dashboard_dashboard_chat-1.jsonl",
            _jsonl({"_type": "metadata", "title": "newer"}),
        )
        import os as _os

        older = tmp_path / "dashboard_chat-1.jsonl"
        _os.utime(older, (1_600_000_000, 1_600_000_000))
        rows = log.list_sessions()
        assert len(rows) == 1
        assert rows[0]["title"] == "newer"

    def test_stat_failure_skips_file(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(tmp_path / "s1.jsonl", _jsonl({"_type": "metadata"}))
        real_stat = Path.stat

        def _stat(self, *args, **kwargs):
            if self.suffix == ".jsonl":
                raise OSError("gone")
            return real_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", _stat):
            assert log.list_sessions() == []

    def test_symlinks_are_skipped(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(tmp_path / "s1.jsonl", _jsonl({"_type": "metadata"}))
        with patch.object(Path, "is_symlink", return_value=True):
            assert log.list_sessions() == []


# ── clear_closed ───────────────────────────────────────────────────────────


class TestClearClosed:
    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        _log(tmp_path).clear_closed("nope")

    def test_empty_file_is_noop(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(log._path("k"), "")
        log.clear_closed("k")

    def test_unparseable_first_line_is_noop(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(log._path("k"), "{ broken\n")
        log.clear_closed("k")
        assert log._path("k").read_text(encoding="utf-8") == "{ broken\n"

    def test_not_flagged_is_noop(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        before = log._path("k").read_text(encoding="utf-8")
        log.clear_closed("k")
        assert log._path("k").read_text(encoding="utf-8") == before

    def test_first_line_not_metadata_is_noop(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(log._path("k"), _jsonl({"role": "user", "content": "hi"}))
        log.clear_closed("k")
        assert "closed" not in log._path("k").read_text(encoding="utf-8")

    def test_clears_flag_and_stamp(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        log.update_metadata("k", {"closed": True, "closed_at": 100.0})
        log.clear_closed("k")
        meta = _log(tmp_path).get_metadata("k")
        assert "closed" not in meta and "closed_at" not in meta

    def test_compare_and_clear_keeps_newer_close(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        log.update_metadata("k", {"closed": True, "closed_at": 500.0})
        log.clear_closed("k", only_if_closed_before=400.0)
        assert _log(tmp_path).get_metadata("k")["closed"] is True

    def test_compare_and_clear_drops_older_close(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        log.update_metadata("k", {"closed": True, "closed_at": 300.0})
        log.clear_closed("k", only_if_closed_before=400.0)
        assert "closed" not in _log(tmp_path).get_metadata("k")

    def test_unstamped_flag_falls_back_to_mtime(self, tmp_path: Path) -> None:
        """No ``closed_at`` → the file mtime approximates the close instant, so a
        far-future threshold clears and a far-past one keeps."""
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        log.update_metadata("k", {"closed": True})
        log.clear_closed("k", only_if_closed_before=0.0)
        assert _log(tmp_path).get_metadata("k")["closed"] is True
        log.clear_closed("k", only_if_closed_before=4_000_000_000.0)
        assert "closed" not in _log(tmp_path).get_metadata("k")

    def test_unparseable_closed_at_falls_back_to_mtime(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        log.update_metadata("k", {"closed": True, "closed_at": "not-a-float"})
        log.clear_closed("k", only_if_closed_before=4_000_000_000.0)
        assert "closed" not in _log(tmp_path).get_metadata("k")


# ── durable consolidation accounting ───────────────────────────────────────


class TestConsolidationCounts:
    def test_unparseable_offset_is_treated_as_zero(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "a")
        log.append("k", "user", "b")
        log.update_metadata("k", {"last_consolidated": "bogus"})
        assert log.consolidation_counts("k") == (2, 2)

    def test_counts_subtract_offset(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        for i in range(4):
            log.append("k", "user", str(i))
        log.mark_consolidated("k", 3)
        assert log.consolidation_counts("k") == (4, 1)


class TestRecordEnvironmentFailure:
    def test_deleted_session_records_nothing(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        assert log.record_consolidation_environment_failure("gone", 900.0, 86400.0) == (
            0,
            0.0,
        )

    def test_increments_and_widens_wait(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        first, at1 = log.record_consolidation_environment_failure(
            "k", 100.0, 10_000.0, now=1_000.0
        )
        second, at2 = log.record_consolidation_environment_failure(
            "k", 100.0, 10_000.0, now=1_000.0
        )
        assert (first, second) == (1, 2)
        assert at1 == 1_100.0
        assert at2 == 1_200.0
        assert log.get_metadata("k")["consolidation_env_failures"] == 2

    def test_wait_is_capped(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        log.update_metadata("k", {"consolidation_env_failures": 40})
        failures, retry_at = log.record_consolidation_environment_failure(
            "k", 100.0, 500.0, now=0.0
        )
        assert failures == 41
        assert retry_at == 500.0

    def test_unparseable_counter_restarts_at_one(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        log.update_metadata("k", {"consolidation_env_failures": "wat"})
        failures, _ = log.record_consolidation_environment_failure(
            "k", 100.0, 10_000.0, now=0.0
        )
        assert failures == 1


class TestRetryEligible:
    def test_refuses_at_attempt_cap(self) -> None:
        log = MagicMock()
        log.consolidation_retry_state.return_value = (_CONSOLIDATION_MAX_ATTEMPTS, 0.0)
        assert _consolidator(log=log).retry_eligible("k", now=1e12) is False

    def test_refuses_before_retry_at(self) -> None:
        log = MagicMock()
        log.consolidation_retry_state.return_value = (1, 5_000.0)
        c = _consolidator(log=log)
        assert c.retry_eligible("k", now=4_999.0) is False
        assert c.retry_eligible("k", now=5_000.0) is True

    def test_passes_message_count_through(self) -> None:
        log = MagicMock()
        log.consolidation_retry_state.return_value = (0, 0.0)
        assert _consolidator(log=log).retry_eligible("k", message_count=12) is True
        log.consolidation_retry_state.assert_called_once_with("k", 12)


_SPAN = AttemptedSpan(total=10, generation=2, offset=4)


class TestNoteFailedAttempt:
    @pytest.mark.asyncio
    async def test_persist_failure_is_logged_and_swallowed(self, caplog) -> None:
        log = MagicMock()
        log.record_consolidation_failure.side_effect = OSError("no disk")
        c = _consolidator(log=log)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            await c._note_failed_attempt("k", _SPAN, "boom")
        assert "Could not persist consolidation retry state" in caplog.text
        log.mark_consolidated.assert_not_called()

    @pytest.mark.asyncio
    async def test_deleted_session_records_nothing(self) -> None:
        log = MagicMock()
        log.record_consolidation_failure.return_value = (0, 0.0)
        c = _consolidator(log=log)
        await c._note_failed_attempt("k", _SPAN, "boom")
        log.mark_consolidated.assert_not_called()

    @pytest.mark.asyncio
    async def test_below_cap_logs_next_attempt(self, caplog) -> None:
        log = MagicMock()
        log.record_consolidation_failure.return_value = (1, 1e12)
        c = _consolidator(log=log)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            await c._note_failed_attempt("k", _SPAN, "boom")
        assert "Consolidation attempt 1/" in caplog.text
        log.mark_consolidated.assert_not_called()
        assert log.record_consolidation_failure.call_args.args[1:] == (
            _CONSOLIDATION_BACKOFF_BASE_SECS,
            _CONSOLIDATION_BACKOFF_MAX_SECS,
            _SPAN,
        )

    @pytest.mark.asyncio
    async def test_at_cap_abandons_span_with_snapshot_identity(self, caplog) -> None:
        log = MagicMock()
        log.record_consolidation_failure.return_value = (
            _CONSOLIDATION_MAX_ATTEMPTS,
            0.0,
        )
        c = _consolidator(log=log)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            await c._note_failed_attempt("k", _SPAN, "boom")
        assert "Abandoning consolidation" in caplog.text
        log.mark_consolidated.assert_called_once_with("k", _SPAN.total, _SPAN.generation)

    @pytest.mark.asyncio
    async def test_marker_write_failure_at_cap_is_logged(self, caplog) -> None:
        log = MagicMock()
        log.record_consolidation_failure.return_value = (
            _CONSOLIDATION_MAX_ATTEMPTS,
            0.0,
        )
        log.mark_consolidated.side_effect = OSError("no disk")
        c = _consolidator(log=log)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            await c._note_failed_attempt("k", _SPAN, "boom")
        assert "Could not mark abandoned consolidation" in caplog.text


class TestNoteEnvironmentFailure:
    @pytest.mark.asyncio
    async def test_persist_failure_is_logged(self, caplog) -> None:
        log = MagicMock()
        log.record_consolidation_environment_failure.side_effect = OSError("no disk")
        c = _consolidator(log=log)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            await c._note_environment_failure("k", "kiro-cli missing")
        assert "Could not persist consolidation environment backoff" in caplog.text

    @pytest.mark.asyncio
    async def test_deleted_session_is_silent(self, caplog) -> None:
        log = MagicMock()
        log.record_consolidation_environment_failure.return_value = (0, 0.0)
        c = _consolidator(log=log)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            await c._note_environment_failure("k", "gone")
        assert "could not reach the LLM" not in caplog.text

    @pytest.mark.asyncio
    async def test_never_touches_the_attempt_cap(self, caplog) -> None:
        log = MagicMock()
        log.record_consolidation_environment_failure.return_value = (3, 1e12)
        c = _consolidator(log=log)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            await c._note_environment_failure("k", "no session manager")
        assert "environment failure #3" in caplog.text
        log.mark_consolidated.assert_not_called()
        log.record_consolidation_failure.assert_not_called()


# ── structured memory + lesson writers ─────────────────────────────────────


class TestWriteStructuredMemory:
    def test_no_vector_store_is_noop(self) -> None:
        _consolidator()._write_structured_memory({"semantic": [{"key": "a"}]}, "k")

    def test_semantic_write_delete_and_escalation(self, caplog) -> None:
        vs = MagicMock()
        vs.set_semantic.return_value = None
        vs.delete_semantic.return_value = True
        c = _consolidator(vector_store=vs)
        result = {
            "semantic": [
                {"key": "plain", "value": "v", "confidence": 0.5},
                {"key": "explicit", "value": "v2", "confidence": 1.0},
                {"key": "stale", "delete": True},
                {"no_key": "skipped"},
                "not a dict",
            ]
        }
        with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
            c._write_structured_memory(result, "sess")
        sources = {kw["key"]: kw["source"] for _, kw in vs.set_semantic.call_args_list}
        assert sources == {"plain": "consolidation:sess", "explicit": "user_explicit"}
        vs.delete_semantic.assert_called_once_with("stale", "consolidation:sess")
        assert "2 written, 1 deleted" in caplog.text

    def test_semantic_reject_is_refused_not_written(self, caplog) -> None:
        vs = MagicMock()
        vs.set_semantic.return_value = (SemanticRejectCode.ALLOWLIST, "not allowlisted")
        c = _consolidator(vector_store=vs)
        with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
            c._write_structured_memory({"semantic": [{"key": "a", "value": "b"}]}, "k")
        assert "0 written" in caplog.text
        assert "1 refused" in caplog.text

    def test_semantic_is_capped(self) -> None:
        vs = MagicMock()
        vs.set_semantic.return_value = None
        c = _consolidator(vector_store=vs)
        items = [{"key": f"k{i}", "value": "v"} for i in range(200)]
        c._write_structured_memory({"semantic": items}, "k")
        assert vs.set_semantic.call_count == H._MAX_SEMANTIC_PER_CONSOLIDATION

    def test_episodic_write_and_skips(self, caplog) -> None:
        vs = MagicMock()
        vs.write_episodic.return_value = True
        c = _consolidator(vector_store=vs)
        result = {
            "episodic": [
                {"text": "a thing happened", "tags": ["t"], "importance": 0.9},
                {"missing": "text"},
                7,
            ]
        }
        with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
            c._write_structured_memory(result, "sess")
        vs.write_episodic.assert_called_once_with(
            text="a thing happened",
            conversation_id="sess",
            tags=["t"],
            importance=0.9,
            source="consolidation:sess",
        )
        assert "Wrote 1 episodic" in caplog.text

    def test_episodic_failure_is_not_counted(self, caplog) -> None:
        vs = MagicMock()
        vs.write_episodic.return_value = False
        c = _consolidator(vector_store=vs)
        with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
            c._write_structured_memory({"episodic": [{"text": "x"}]}, "k")
        assert "episodic entries from consolidation" not in caplog.text

    def test_episodic_is_capped(self) -> None:
        vs = MagicMock()
        vs.write_episodic.return_value = True
        c = _consolidator(vector_store=vs)
        items = [{"text": f"t{i}"} for i in range(200)]
        c._write_structured_memory({"episodic": items}, "k")
        assert vs.write_episodic.call_count == H._MAX_EPISODIC_PER_CONSOLIDATION

    def test_non_list_sections_are_ignored(self) -> None:
        vs = MagicMock()
        c = _consolidator(vector_store=vs)
        c._write_structured_memory({"semantic": "nope", "episodic": {"a": 1}}, "k")
        vs.set_semantic.assert_not_called()
        vs.write_episodic.assert_not_called()


class TestSaveLessons:
    def test_non_list_is_ignored(self) -> None:
        vs = MagicMock()
        _consolidator(vector_store=vs)._save_lessons({"rule": "x"})
        vs.write_lesson.assert_not_called()

    def test_caps_and_warns(self, caplog) -> None:
        vs = MagicMock()
        vs.write_lesson.return_value = True
        c = _consolidator(vector_store=vs)
        raw = [{"rule": f"r{i}"} for i in range(H._MAX_LESSONS_PER_CONSOLIDATION + 5)]
        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            c._save_lessons(raw)
        assert "capping to" in caplog.text
        assert vs.write_lesson.call_count == H._MAX_LESSONS_PER_CONSOLIDATION

    def test_vector_store_path_skips_lesson_store(self, caplog) -> None:
        vs = MagicMock()
        vs.write_lesson.side_effect = [True, False]
        store = MagicMock()
        c = _consolidator(vector_store=vs, lesson_store=store)
        with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
            c._save_lessons(
                [
                    {"rule": "one", "category": "style", "negative": "not that"},
                    {"rule": "two"},
                    {"no": "rule"},
                ]
            )
        assert vs.write_lesson.call_count == 2
        store.save.assert_not_called()
        assert "Extracted 1 lesson(s) from chat (vector store)" in caplog.text

    def test_vector_store_zero_writes_logs_nothing(self, caplog) -> None:
        vs = MagicMock()
        vs.write_lesson.return_value = False
        with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
            _consolidator(vector_store=vs)._save_lessons([{"rule": "r"}])
        assert "vector store" not in caplog.text

    def test_no_stores_is_noop(self) -> None:
        _consolidator()._save_lessons([{"rule": "r"}])

    def test_lesson_store_fallback_saves_lessons(self, caplog) -> None:
        store = MagicMock()
        c = _consolidator(lesson_store=store)
        with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
            c._save_lessons(
                [
                    {"rule": "always X", "category": "workflow", "negative": "never Y"},
                    {"missing": "rule"},
                    "not a dict",
                ]
            )
        store.save.assert_called_once()
        lesson = store.save.call_args.args[0]
        assert lesson.rule == "always X"
        assert lesson.category == "workflow"
        assert lesson.negative == "never Y"
        assert lesson.ts
        assert "Extracted 1 lesson(s) from chat" in caplog.text

    def test_lesson_store_defaults_category(self) -> None:
        store = MagicMock()
        _consolidator(lesson_store=store)._save_lessons([{"rule": "r"}])
        assert store.save.call_args.args[0].category == "knowledge"


# ── background-session LLM turns ───────────────────────────────────────────


def _fake_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
    sessions.release = MagicMock()
    sessions.recycle_background = AsyncMock(return_value=None)
    return sessions


class TestDedupeJudge:
    @pytest.mark.asyncio
    async def test_no_session_manager_fails_open(self) -> None:
        assert await _consolidator()._dedupe_judge("p") == ""

    @pytest.mark.asyncio
    async def test_returns_text_and_releases_session(self) -> None:
        sessions = _fake_sessions()
        c = _consolidator(sessions=sessions)
        with patch(
            "kiro_crew.history.stream_and_collect", AsyncMock(return_value="DUP")
        ):
            assert await c._dedupe_judge("prompt") == "DUP"
        sessions.release.assert_called_once_with(BACKGROUND_KEY)
        sessions.recycle_background.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_text_becomes_empty_string(self) -> None:
        sessions = _fake_sessions()
        c = _consolidator(sessions=sessions)
        with patch(
            "kiro_crew.history.stream_and_collect", AsyncMock(return_value=None)
        ):
            assert await c._dedupe_judge("prompt") == ""

    @pytest.mark.asyncio
    async def test_llm_error_fails_open_but_still_releases(self) -> None:
        sessions = _fake_sessions()
        c = _consolidator(sessions=sessions)
        with patch(
            "kiro_crew.history.stream_and_collect",
            AsyncMock(side_effect=RuntimeError("provider down")),
        ):
            assert await c._dedupe_judge("prompt") == ""
        sessions.release.assert_called_once_with(BACKGROUND_KEY)

    @pytest.mark.asyncio
    async def test_release_failure_is_swallowed(self) -> None:
        sessions = _fake_sessions()
        sessions.release.side_effect = RuntimeError("already released")
        c = _consolidator(sessions=sessions)
        with patch(
            "kiro_crew.history.stream_and_collect", AsyncMock(return_value="NEW")
        ):
            assert await c._dedupe_judge("prompt") == "NEW"


class TestMergeSkillUpdate:
    @pytest.mark.asyncio
    async def test_no_session_manager_returns_none(self) -> None:
        assert await _consolidator()._merge_skill_update("body", "d", "t", "p") is None

    @pytest.mark.asyncio
    async def test_returns_merged_body_and_prompts_with_all_inputs(self) -> None:
        sessions = _fake_sessions()
        c = _consolidator(sessions=sessions)
        collector = AsyncMock(return_value="## When to use\nmerged\n")
        with patch("kiro_crew.history.stream_and_collect", collector):
            out = await c._merge_skill_update(
                "EXISTING BODY", "the description", "trig1, trig2", "1. do it"
            )
        assert out == "## When to use\nmerged\n"
        prompt = collector.await_args.args[1]
        for needle in ("EXISTING BODY", "the description", "trig1, trig2", "1. do it"):
            assert needle in prompt
        sessions.release.assert_called_once_with(BACKGROUND_KEY)
        sessions.recycle_background.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_text_returns_none(self) -> None:
        sessions = _fake_sessions()
        c = _consolidator(sessions=sessions)
        with patch("kiro_crew.history.stream_and_collect", AsyncMock(return_value="")):
            assert await c._merge_skill_update("b", "d", "t", "p") is None

    @pytest.mark.asyncio
    async def test_llm_error_returns_none_but_still_releases(self) -> None:
        sessions = _fake_sessions()
        c = _consolidator(sessions=sessions)
        with patch(
            "kiro_crew.history.stream_and_collect",
            AsyncMock(side_effect=RuntimeError("provider down")),
        ):
            assert await c._merge_skill_update("b", "d", "t", "p") is None
        sessions.release.assert_called_once_with(BACKGROUND_KEY)

    @pytest.mark.asyncio
    async def test_release_failure_is_swallowed(self) -> None:
        sessions = _fake_sessions()
        sessions.recycle_background.side_effect = RuntimeError("boom")
        c = _consolidator(sessions=sessions)
        with patch("kiro_crew.history.stream_and_collect", AsyncMock(return_value="x")):
            assert await c._merge_skill_update("b", "d", "t", "p") == "x"


# ── compaction rewrite ─────────────────────────────────────────────────────


class TestRewriteSession:
    def test_corrupted_lines_are_archived_not_kept(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "keep")
        log.append("k", "user", "drop")
        with log._path("k").open("a", encoding="utf-8", newline="\n") as f:
            f.write("{ corrupted\n")
        keep = [m for m in log._read_messages("k") if m["content"] == "keep"]
        log.rewrite_session("k", keep)
        assert [m["content"] for m in _log(tmp_path)._read_messages("k")] == ["keep"]
        archived = list((tmp_path / H.ARCHIVE_DIR_NAME).glob("*.jsonl"))
        assert archived, "dropped + corrupted lines must be archived"
        body = archived[0].read_text(encoding="utf-8")
        assert "drop" in body
        assert "{ corrupted" in body

    def test_archive_failure_is_logged_not_raised(self, tmp_path: Path, caplog) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "a")
        log.append("k", "user", "b")
        keep = log._read_messages("k")[:1]
        with patch.object(H, "_archive_lines", side_effect=OSError("read-only")):
            with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
                log.rewrite_session("k", keep)
        assert "Failed to archive dropped lines" in caplog.text
        assert [m["content"] for m in _log(tmp_path)._read_messages("k")] == ["a"]

    def test_unowned_metadata_is_carried_through(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "a")
        log.append("k", "user", "b")
        log.update_metadata("k", {"title": "mine", "agent": "kirocrew"})
        keep = log._read_messages("k")[:1]
        log.rewrite_session("k", keep)
        meta = _log(tmp_path).get_metadata("k")
        assert meta["title"] == "mine"
        assert meta["agent"] == "kirocrew"
        assert meta["compacted_at"]

    def test_rewrite_preserves_mtime(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "a")
        log.append("k", "user", "b")
        import os as _os

        _os.utime(log._path("k"), (1_600_000_000, 1_600_000_000))
        before = log._path("k").stat().st_mtime
        log.rewrite_session("k", log._read_messages("k")[:1])
        assert log._path("k").stat().st_mtime == pytest.approx(before, abs=2)


# ── search-text cache budget ───────────────────────────────────────────────


class TestSearchTextCache:
    def _cache(self, max_bytes: int) -> H._SearchTextCache:
        return H._SearchTextCache(max_bytes, lambda v: int(v), "test")

    def test_retain_drops_dead_keys_and_resets_refusals(self) -> None:
        cache = self._cache(100)
        cache["a"] = 10
        cache["b"] = 20
        cache["huge"] = 1_000  # refused: over budget
        assert cache.refused_since_prune() == 1
        assert cache.retain({"a"}) == 1
        assert "b" not in cache
        assert "a" in cache
        assert cache.refused_since_prune() == 0

    def test_len_pop_clear_and_stats(self) -> None:
        cache = self._cache(100)
        cache["a"] = 10
        cache["b"] = 20
        assert len(cache) == 2
        assert cache.pop("a") == 10
        assert cache.pop("a", 99) == 99
        stats = cache.stats()
        assert stats["entries"] == 1
        assert stats["max_bytes"] == 100
        cache.clear()
        assert len(cache) == 0
        assert cache.stats()["bytes"] == 0


# ── span identity for the attempt cap ──────────────────────────────────────


class TestAttemptsDescribeCurrentSpan:
    def _log(self, tmp_path: Path) -> ConversationLog:
        return _log(tmp_path)

    def test_unstamped_accounting_keeps_the_cap(self, tmp_path: Path) -> None:
        log = self._log(tmp_path)
        assert log._attempts_describe_current_span({}, 99) is True

    def test_generation_mismatch_releases_the_cap(self, tmp_path: Path) -> None:
        log = self._log(tmp_path)
        meta = {"rotation_generation": 3, "consolidation_attempts_generation": 2}
        assert log._attempts_describe_current_span(meta, None) is False

    def test_offset_mismatch_releases_the_cap(self, tmp_path: Path) -> None:
        log = self._log(tmp_path)
        meta = {"last_consolidated": 7, "consolidation_attempts_offset": 4}
        assert log._attempts_describe_current_span(meta, None) is False

    def test_unreadable_stamp_keeps_the_cap(self, tmp_path: Path) -> None:
        log = self._log(tmp_path)
        meta = {"rotation_generation": 1, "consolidation_attempts_generation": "junk"}
        assert log._attempts_describe_current_span(meta, None) is True

    def test_growth_past_attempted_extent_releases_the_cap(self, tmp_path: Path) -> None:
        log = self._log(tmp_path)
        meta = {"consolidation_attempts_count": 10}
        assert log._attempts_describe_current_span(meta, 11) is False
        assert log._attempts_describe_current_span(meta, 10) is True
        # A shrink is a rotation/compaction, not new content — cap holds.
        assert log._attempts_describe_current_span(meta, 4) is True

    def test_unreadable_extent_keeps_the_cap(self, tmp_path: Path) -> None:
        log = self._log(tmp_path)
        meta = {"consolidation_attempts_count": "junk"}
        assert log._attempts_describe_current_span(meta, 11) is True

    def test_missing_count_skips_the_extent_test(self, tmp_path: Path) -> None:
        log = self._log(tmp_path)
        assert log._attempts_describe_current_span(
            {"consolidation_attempts_count": 3}, None
        ) is True


# ── metadata write guards ──────────────────────────────────────────────────


class TestUpdateMetadataLocked:
    def test_unparseable_first_line_is_left_untouched(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(log._path("k"), "{ broken\n")
        log.update_metadata("k", {"title": "T"})
        assert log._path("k").read_text(encoding="utf-8") == "{ broken\n"

    def test_non_metadata_first_line_is_left_untouched(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(log._path("k"), _jsonl({"role": "user", "content": "hi"}))
        log.update_metadata("k", {"title": "T"})
        assert "title" not in log._path("k").read_text(encoding="utf-8")

    def test_upsert_creates_metadata_line(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.update_metadata("fresh", {"agent": "kirocrew"})
        meta = _log(tmp_path).get_metadata("fresh")
        assert meta["agent"] == "kirocrew"
        assert meta["last_consolidated"] == 0

    def test_replace_failure_cleans_up_the_temp_file(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        with patch("os.replace", side_effect=OSError("cross-device")):
            with pytest.raises(OSError):
                log.update_metadata("k", {"title": "T"})
        assert list(tmp_path.glob("*.tmp")) == []

    def test_update_metadata_if_guard_rejects(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        assert log.update_metadata_if("k", {"title": "T"}, lambda m: False) is False
        assert "title" not in log.get_metadata("k")

    def test_update_metadata_if_guard_accepts(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        assert log.update_metadata_if("k", {"tab_id": "t1"}, lambda m: True) is True
        assert _log(tmp_path).get_metadata("k")["tab_id"] == "t1"

    def test_update_metadata_if_fails_closed_on_unreadable_record(
        self, tmp_path: Path
    ) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        with patch.object(log, "_read_metadata_status", return_value=({}, False)):
            assert log.update_metadata_if("k", {"title": "T"}, lambda m: True) is False


# ── delete_session ─────────────────────────────────────────────────────────


class TestDeleteSession:
    def test_missing_session_returns_false(self, tmp_path: Path) -> None:
        assert _log(tmp_path).delete_session("nope") is False

    def test_removes_file_and_summary_sidecar(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        log.set_cached_summary("k", "a summary", 1.0)
        sidecar = log._summary_cache_path("k")
        assert sidecar.exists()
        assert log.delete_session("k") is True
        assert not log._path("k").exists()
        assert not sidecar.exists()

    def test_unlink_failure_returns_false(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        with patch.object(Path, "unlink", side_effect=OSError("busy")):
            assert log.delete_session("k") is False
        assert log._path("k").exists()

    def test_sidecar_unlink_failure_still_deletes(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        real_unlink = Path.unlink

        def _unlink(self, *args, **kwargs):
            if self.suffix == ".json":
                raise OSError("busy")
            return real_unlink(self, *args, **kwargs)

        with patch.object(Path, "unlink", _unlink):
            assert log.delete_session("k") is True

    def test_lock_timeout_refuses_to_delete(self, tmp_path: Path, caplog) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hi")
        with patch.object(log, "_locked", side_effect=H.HistoryLockTimeout("wedged")):
            with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
                assert log.delete_session("k") is False
        assert "lock timeout, not deleting" in caplog.text
        assert log._path("k").exists()


# ── snippet + cross-session reads ──────────────────────────────────────────


class TestContentSnippet:
    def test_empty_query_has_no_snippet(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hello world")
        assert log._content_snippet("k", "   ") == ""

    def test_no_match_has_no_snippet(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hello world")
        assert log._content_snippet("k", "absent") == ""

    def test_read_error_has_no_snippet(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "hello world")
        with patch.object(log, "_snippet_texts", side_effect=OSError("gone")):
            assert log._content_snippet("k", "hello") == ""

    def test_match_is_centered_and_elided(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", ("pad " * 200) + "needle" + (" pad" * 200))
        snippet = log._content_snippet("k", "needle")
        assert "needle" in snippet
        assert snippet.startswith("…")
        assert snippet.endswith("…")

    def test_token_fallback_for_multiword_query(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("k", "user", "alpha then something then beta")
        snippet = log._content_snippet("k", "alpha beta")
        assert snippet  # the phrase is absent, so a token matched


class TestRecentFromSource:
    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert (
            ConversationLog(base_dir=tmp_path / "absent").recent_from_source("slack")
            == []
        )

    def test_collects_and_excludes(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("slack:a", "user", "from a")
        log.append("slack:b", "user", "from b")
        log.append("other:c", "user", "from c")
        got = log.recent_from_source("slack", exclude_key="slack:b")
        assert [m["content"] for m in got] == ["from a"]
        assert all(set(m) == {"role", "content"} for m in got)

    def test_incognito_sessions_are_skipped(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("slack:a", "user", "kept")
        log.append("slack:b", "user", "hidden")
        log.update_metadata("slack:b", {"memory_mode": sorted(H.INCOGNITO_MEMORY_MODES)[0]})
        got = log.recent_from_source("slack")
        assert [m["content"] for m in got] == ["kept"]

    def test_head_read_failure_skips_the_file(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.append("slack:a", "user", "hi")
        with patch("builtins.open", side_effect=OSError("locked")):
            assert log.recent_from_source("slack") == []

    def test_max_messages_trims_to_the_newest(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        for i in range(6):
            log.append("slack:a", "user", f"m{i}")
        got = log.recent_from_source("slack", max_messages=2)
        assert [m["content"] for m in got] == ["m4", "m5"]


# ── skill-text helpers ─────────────────────────────────────────────────────


class TestSkillTextHelpers:
    @pytest.mark.parametrize(
        "text,expected",
        [
            (None, ""),
            ("", ""),
            ("no frontmatter here", ""),
            ("---\nname: thing\ndescription: d\n---\nbody", "d"),
            ("---\nname: thing\n---\nbody", ""),
            ("---\nnovalue\n---\nbody", ""),
        ],
    )
    def test_frontmatter_value(self, text, expected) -> None:
        assert H._frontmatter_value(text, "description") == expected

    @pytest.mark.parametrize(
        "live,candidate,expected",
        [
            ("a, b", "b, c", "a, b, c"),
            ("A", "a", "A"),
            ("", "solo", "solo"),
            ("  spaced   out ", "", "spaced out"),
            (",,", ",", ""),
        ],
    )
    def test_merge_trigger_lists(self, live, candidate, expected) -> None:
        assert H._merge_trigger_lists(live, candidate) == expected

    def test_merge_trigger_lists_caps(self) -> None:
        live = ", ".join(f"t{i}" for i in range(20))
        assert H._merge_trigger_lists(live, "extra", cap=3) == "t0, t1, t2"

    @pytest.mark.parametrize(
        "text,expected",
        [
            (None, ""),
            ("---\nname: x\n---\nbody text", "body text"),
            # The locator tolerates a carriage return before each fence
            # newline, exactly like SKILL_UPDATE's fence: a block the field
            # parser reads must be a block this strips, or the header rides
            # into the update-merge prompt under a re-emitted fence.
            ("---\r\nname: x\r\n---\r\nbody text", "body text"),
            ("no frontmatter", "no frontmatter"),
        ],
    )
    def test_strip_skill_frontmatter(self, text, expected) -> None:
        assert H._strip_skill_frontmatter(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("", ""),
            ("plain body", "plain body"),
            ("```markdown\nfenced\n```", "fenced"),
            ("```\nfenced\n```", "fenced"),
            ("```markdown\nunterminated", "unterminated"),
            ("```", "```"),  # a lone fence has no body line to unwrap
        ],
    )
    def test_strip_code_fence(self, text, expected) -> None:
        assert H._strip_code_fence(text) == expected


class TestReadMessagesSkipsBlankLines:
    def test_blank_lines_between_rows_are_ignored(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write(
            log._path("k"),
            _jsonl({"_type": "metadata"})
            + "\n\n"
            + _jsonl({"role": "user", "content": "only"}),
        )
        assert [m["content"] for m in log._read_messages("k")] == ["only"]


class TestIsIncognitoTranscript:
    """Lock the shared classifier's normalization so call sites can rely on it."""

    @pytest.mark.parametrize("mode", sorted(H.INCOGNITO_MEMORY_MODES))
    def test_private_modes_classify_true(self, mode: str) -> None:
        assert H.is_incognito_transcript(mode) is True

    @pytest.mark.parametrize("mode", ["Incognito", "TEMPORARY", "Temporary"])
    def test_case_insensitive(self, mode: str) -> None:
        """The set holds lowercase members; a hand-edited header is not bound
        by API validation, so comparison must be case-insensitive."""
        assert H.is_incognito_transcript(mode) is True

    @pytest.mark.parametrize("mode", [None, "", "persistent", "unknown", 42, False])
    def test_absent_or_unrecognized_reads_persistent(self, mode: object) -> None:
        """None/absent means a legacy persistent session; junk stays
        not-private — fail-closed callers allowlist BEFORE this predicate."""
        assert H.is_incognito_transcript(mode) is False

    def test_whitespace_not_stripped(self) -> None:
        """`"incognito "` deliberately misses the set: the restricted-session
        write gate normalizes via its own allowlist and denies on None, so
        stripping here would silently change which callers fail closed."""
        assert H.is_incognito_transcript("incognito ") is False


class TestSidecarSummariesSurviveMtimePreservingRewrites:
    """Housekeeping that CHANGES the transcript must drop the summary sidecars.

    ``get_cached_summary`` / ``get_cached_intent_summary`` validate their
    sidecars against ``session_mtime`` — and ``session_mtime``'s docstring calls
    that "a faithful 'has this conversation changed?' signal". It is not, for
    housekeeping: ``_rewrite_session_locked`` and ``_maybe_rotate`` deliberately
    restore the pre-write mtime (``_restore_mtime``) so they do not float stale
    sessions to the top of ``list_sessions``.

    So a compaction that drops half a transcript leaves the recorded signature
    still matching, and the sidecar describing the PRE-rewrite conversation is
    served as valid. Unlike the in-process caches #4293 guards with a generation
    counter, these are files on disk: the staleness outlives the process and is
    permanent until a genuine ``append`` lands.
    """

    def _seed(self, log, key: str) -> float:
        log.append(key, "user", "first message")
        log.append(key, "assistant", "first reply")
        log.append(key, "user", "second message")
        # Age the transcript BEFORE stamping the sidecars against its mtime.
        # Filesystem mtime resolution is not the clock's: tmpfs and ext4 stamp from
        # the kernel's cached wall time, which advances on a timer tick rather than
        # per write, so these appends and a later one can share a single
        # ``st_mtime_ns`` (measured: 200 rapid writes to /tmp on a 6.12 kernel
        # produced ONE distinct value). Every test in this class turns on telling
        # "the mtime moved" apart from "the mtime was preserved", so that gap has to
        # be REPRESENTABLE. Backdating states the precondition outright; a sleep
        # would express the same thing by guessing how long a tick is.
        now = log.session_mtime(key)
        assert now is not None
        aged = now - 60.0
        os.utime(log._path(key), (aged, aged))
        sig = log.session_mtime(key)
        assert sig == aged
        log.set_cached_summary(key, "describes the PRE-rewrite conversation", sig)
        log.set_cached_intent_summary(key, {"intents": [{"text": "pre-rewrite"}]}, sig)
        assert log.get_cached_summary(key) == "describes the PRE-rewrite conversation"
        return sig

    def test_an_advanced_content_generation_invalidates_both_sidecars(
        self, tmp_path: Path
    ) -> None:
        """The general contract, independent of which writer moved the content.

        ``rotation_generation`` is the module's content-identity counter: it is
        advanced by every write that makes the transcript a DIFFERENT body of
        content — a rotation, the dashboard rewrite save
        (regenerate / rewind / fork, ``chat_persistence``) and a channel
        transcript merge. Those are exactly the writers that also restore the
        pre-write mtime, so the counter is the half of the signature that can
        still tell the sidecars their subject changed.

        Here the metadata is advanced the same way those writers advance it,
        with the mtime restored, so the ONLY thing that moved is the content
        identity.
        """
        log = _log(tmp_path)
        sig = self._seed(log, "k")

        path = log._path("k")
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        meta = json.loads(lines[0])
        meta["rotation_generation"] = int(meta.get("rotation_generation", 0) or 0) + 1
        lines[0] = json.dumps(meta) + chr(10)
        path.write_text("".join(lines), encoding="utf-8")
        H._restore_mtime(path, sig)
        # Every real preserved-mtime writer invalidates inside its locked
        # section; without that the in-process metadata cache would still
        # serve the old generation and this would test nothing.
        log._invalidate_cache("k")

        assert log.session_mtime("k") == sig, (
            "precondition: the mtime must be UNCHANGED — otherwise the old "
            "signature would have caught this on its own and the test proves "
            "nothing about the generation"
        )
        assert log.get_cached_summary("k") is None, (
            "the summary describes a body of content that has been replaced"
        )
        assert log.get_cached_intent_summary("k") is None

        # ...but the payload is still ON DISK and reported as STALE, not gone.
        # ``read_intent_summary`` deliberately prefers a summary marked out of
        # date over an empty panel ("an empty panel reads as 'this feature is
        # broken'"), which is why the signature is widened rather than the
        # sidecars being deleted outright.
        payload, stale = log.read_intent_summary("k")
        assert payload is not None and stale is True

    def test_a_rotation_invalidates_both_sidecars(self, tmp_path: Path) -> None:
        """Rotation archives the oldest messages — same class, same fix."""
        log = _log(tmp_path)
        self._seed(log, "k")

        path = log._path("k")
        prev_mtime = path.stat().st_mtime
        with patch.object(H, "_SESSION_MAX_BYTES", 1):
            log._maybe_rotate(path, "k")

        assert log.get_cached_summary("k") is None
        assert log.get_cached_intent_summary("k") is None
        assert path.stat().st_mtime == prev_mtime

    def test_a_metadata_only_write_keeps_them(self, tmp_path: Path) -> None:
        """The deliberate exclusion, and the reason this is not "invalidate on
        every preserved-mtime write".

        ``mark_consolidated`` rewrites ``lines[0]`` only — the bookkeeping
        offset — and never touches a message. The conversation the summary
        describes is unchanged, so discarding an LLM-generated summary here
        would be a pure cost with no correctness gain.
        """
        log = _log(tmp_path)
        self._seed(log, "k")

        log.mark_consolidated("k", 1)

        assert log.get_cached_summary("k") == "describes the PRE-rewrite conversation"
        assert log.get_cached_intent_summary("k") is not None

    def _advance_generation(self, log, key: str, sig: float) -> None:
        """A rewrite that replaces the content and RESTORES the mtime."""
        path = log._path(key)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        meta = json.loads(lines[0])
        meta["rotation_generation"] = int(meta.get("rotation_generation", 0) or 0) + 1
        lines[0] = json.dumps(meta) + chr(10)
        path.write_text("".join(lines), encoding="utf-8")
        H._restore_mtime(path, sig)
        log._invalidate_cache(key)

    def test_a_rewrite_during_generation_cannot_bless_a_stale_summary(
        self, tmp_path: Path
    ) -> None:
        """The write side of the same hole.

        Summary generation holds no lock while the model call is in flight, so
        the caller captures the signature BEFORE reading the transcript. If the
        setter read the generation at WRITE time, a rewrite landing in that
        window would stamp the NEW content's identity onto the OLD summary — and
        because the rewrite also preserves the mtime, nothing else would ever
        catch it. The summary would then be served as fresh forever.

        The generation therefore comes from the caller, captured with the
        signature, exactly as ``mark_consolidated`` already takes the generation
        its caller snapshotted.
        """
        log = _log(tmp_path)
        sig = self._seed(log, "k")
        generation = log.rotation_generation("k")

        self._advance_generation(log, "k", sig)

        log.set_cached_summary("k", "summarises the REPLACED content", sig, generation)

        assert log.session_mtime("k") == sig, "precondition: the mtime was preserved"
        assert log.get_cached_summary("k") is None, (
            "a summary of superseded content must not be served as current"
        )

    def test_the_intent_setter_refuses_a_summary_the_rewrite_superseded(
        self, tmp_path: Path
    ) -> None:
        """``set_cached_intent_summary`` already refuses on a changed mtime; a
        preserved-mtime rewrite is the case that guard cannot see, so it refuses
        on the generation too rather than storing a known-stale payload."""
        log = _log(tmp_path)
        sig = self._seed(log, "k")
        generation = log.rotation_generation("k")

        self._advance_generation(log, "k", sig)

        stored = log.set_cached_intent_summary(
            "k", {"intents": [{"text": "superseded"}]}, sig, generation
        )

        assert stored is False, "the payload describes content that was replaced"

    def test_a_genuine_append_still_invalidates_by_signature(self, tmp_path: Path) -> None:
        """Control: the existing mtime rule keeps working for ordinary writes."""
        log = _log(tmp_path)
        self._seed(log, "k")

        log.append("k", "user", "a real new message")

        assert log.get_cached_summary("k") is None
