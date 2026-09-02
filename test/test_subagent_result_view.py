"""Tests for the subagent completion-event summary and spawn_status paged reads.

Covers:
- context_management.summarize_result — the summary + result_path body injected
  into the parent when the completion copy dropped content.
- dashboard.handlers.messaging._spawn_result_view — line-oriented offset/limit/
  grep slicing used by the spawn_status offset/grep params.
"""

from __future__ import annotations

from kiro_crew.context_management import summarize_result
from kiro_crew.dashboard.handlers.messaging import _spawn_result_view


class TestSummarizeResult:
    def test_small_result_inlined_whole(self):
        out = summarize_result("short answer", "/tmp/does-not-exist.txt", words=200)
        assert "short answer" in out
        assert "Full transcript:" in out
        assert "/tmp/does-not-exist.txt" in out

    def test_large_result_previews_head_and_tail(self):
        body = " ".join(f"w{i}" for i in range(1000))
        out = summarize_result(body, "/tmp/x.txt", words=20)
        assert "w0" in out  # head preserved
        assert "w999" in out  # tail preserved
        assert "middle truncated" in out
        assert "spawn_status" in out  # steers to on-demand read, not re-run

    def test_size_annotation_when_file_exists(self, tmp_path):
        p = tmp_path / "result.txt"
        p.write_text("x" * 1234, encoding="utf-8")
        out = summarize_result("preview", str(p), words=200)
        assert "1,234 bytes" in out


class TestSpawnResultView:
    @staticmethod
    def _text(n: int) -> str:
        return "\n".join(f"line{i}" for i in range(n))

    def test_offset_limit_slices_lines(self):
        view, meta = _spawn_result_view(self._text(100), offset=10, limit=5, grep="")
        assert view.splitlines() == [f"line{i}" for i in range(10, 15)]
        assert meta["offset"] == 10
        assert meta["returned_lines"] == 5
        assert meta["total_lines"] == 100
        assert meta["has_more"] is True

    def test_limit_zero_returns_to_end(self):
        view, meta = _spawn_result_view(self._text(50), offset=0, limit=0, grep="")
        assert meta["returned_lines"] == 50
        assert meta["has_more"] is False

    def test_grep_filters_matching_lines_case_insensitive(self):
        text = "alpha\nBETA\ngamma\nbeta again\n"
        view, meta = _spawn_result_view(text, offset=0, limit=0, grep="beta")
        assert view.splitlines() == ["BETA", "beta again"]
        assert meta["matched_lines"] == 2
        assert meta["total_lines"] == 4

    def test_grep_then_offset_limit(self):
        text = "\n".join("hit" if i % 2 == 0 else "miss" for i in range(10))
        view, meta = _spawn_result_view(text, offset=2, limit=2, grep="hit")
        assert view.splitlines() == ["hit", "hit"]  # 5 hits, skip 2, take 2
        assert meta["matched_lines"] == 5
        assert meta["offset"] == 2
        assert meta["returned_lines"] == 2
        assert meta["has_more"] is True

    def test_bad_grep_regex_returns_error(self):
        view, meta = _spawn_result_view("a\nb", offset=0, limit=0, grep="(")
        assert view == ""
        assert "grep_error" in meta

    def test_offset_past_end_returns_empty(self):
        view, meta = _spawn_result_view(self._text(5), offset=100, limit=10, grep="")
        assert view == ""
        assert meta["returned_lines"] == 0
        assert meta["has_more"] is False
