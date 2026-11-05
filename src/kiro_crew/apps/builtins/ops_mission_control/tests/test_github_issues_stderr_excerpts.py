"""Redact-before-bound and tail anchoring on the GitHub Issues adapter's gh stderr.

A failed ``gh`` prints its diagnosis LAST, so a head cut (``[:N]``) shows startup noise
and drops the reason; the excerpt keeps the tail. Bounding BEFORE redaction can cut a
credential mid-match, leaving a fragment no redaction regex recognises, so the whole
stream is redacted first and a cut can at worst split a redaction marker.

The straddle cases place a fabricated key so the tail window's LEFT edge falls inside
it: a raw slice keeps the key's right half, a slice-then-redact reorder keeps the same
unmatched half, and only redact-then-slice yields a marker. The tail-anchor cases put
one marker on the first line and one on the last of a stream longer than the bound:
exactly the last-line marker must survive. The core's structural scan of the same
module runs in ``test/test_stderr_redact_then_tail.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import kiro_crew.apps.builtins.ops_mission_control.backend.providers.github_issues as github_issues
from kiro_crew.apps.builtins.ops_mission_control.backend.models import ACTION_COMMENT, Signal

# Built at runtime so no credential-shaped literal lands in the repo.
_FAKE_KEY = "AKIA" + "B" * 16  # matches the AWS access-key-id pattern
_MARKER_TAIL = "credential]"  # the redaction marker ends "...: credential]"


def _straddling(bound: int) -> str:
    """A stream whose last *bound* chars begin 4 chars INTO the fake key.

    A raw ``[-bound:]`` keeps ``"B" * 16 + tail``: no ``AKIA`` prefix, so the
    fragment matches nothing and leaks; the same is true after a slice-then-
    redact reorder. Redact-then-slice yields the marker's tail instead.
    """
    tail = "e" * (bound - 16)
    return "p" * 40 + _FAKE_KEY + tail


def _two_markers(bound: int) -> str:
    """A stream longer than *bound* with a distinct marker on its first and last line."""
    return "FIRST-LINE-MARKER\n" + "z" * (bound + 50) + "\nLAST-LINE-MARKER"


def _assert_straddle_redacted(excerpt: str, bound: int) -> None:
    assert _FAKE_KEY not in excerpt
    assert "B" * 16 not in excerpt, excerpt  # the raw right half must not survive
    # Positive proof the payload flowed through redaction (not a vacuous pass on
    # an empty excerpt): the tail window opens INSIDE the replacement marker, so
    # the marker's tail, not the key's, is what survives the cut.
    assert _MARKER_TAIL in excerpt
    assert len(excerpt) <= bound


def _assert_tail_anchored(excerpt: str) -> None:
    assert "LAST-LINE-MARKER" in excerpt
    assert "FIRST-LINE-MARKER" not in excerpt


def _configured_adapter(monkeypatch: pytest.MonkeyPatch) -> github_issues.GitHubIssuesAdapter:
    monkeypatch.setattr(github_issues.GitHubIssuesAdapter, "configured", lambda self: True)
    monkeypatch.setattr(github_issues, "config_value", lambda *_a, **_k: "o/r")
    monkeypatch.setattr(github_issues, "config_list", lambda *_a, **_k: [])
    return github_issues.GitHubIssuesAdapter()


def _signal() -> Signal:
    return Signal(
        id="s1", source="github-issues", title="t", labels={"repo": "o/r", "issue_number": "7"}
    )


class TestGitHubIssuesStderrExcerpts:
    @pytest.mark.asyncio
    async def test_poll_failure_redacts_the_whole_stream_before_the_tail_cut(self, monkeypatch):
        adapter = _configured_adapter(monkeypatch)
        monkeypatch.setattr(
            github_issues, "_run_gh", AsyncMock(return_value=(1, "", _straddling(200)))
        )
        with pytest.raises(RuntimeError) as info:
            await adapter.poll()
        _assert_straddle_redacted(str(info.value).removeprefix("gh issue list failed: "), 200)

    @pytest.mark.asyncio
    async def test_poll_failure_keeps_the_tail_of_gh_stderr(self, monkeypatch):
        adapter = _configured_adapter(monkeypatch)
        monkeypatch.setattr(
            github_issues, "_run_gh", AsyncMock(return_value=(1, "", _two_markers(200)))
        )
        with pytest.raises(RuntimeError) as info:
            await adapter.poll()
        _assert_tail_anchored(str(info.value))

    @pytest.mark.asyncio
    async def test_execute_failure_redacts_the_whole_stream_before_the_tail_cut(self, monkeypatch):
        adapter = _configured_adapter(monkeypatch)
        monkeypatch.setattr(
            github_issues, "_run_gh", AsyncMock(return_value=(1, "", _straddling(200)))
        )
        result = await adapter.execute(_signal(), ACTION_COMMENT, {"note": "n"})
        assert result.ok is False
        _assert_straddle_redacted(result.error, 200)

    @pytest.mark.asyncio
    async def test_execute_failure_keeps_the_tail_of_gh_stderr(self, monkeypatch):
        adapter = _configured_adapter(monkeypatch)
        monkeypatch.setattr(
            github_issues, "_run_gh", AsyncMock(return_value=(1, "", _two_markers(200)))
        )
        result = await adapter.execute(_signal(), ACTION_COMMENT, {"note": "n"})
        _assert_tail_anchored(result.error)
