"""The shared ahead/behind divergence counter (``kiro_crew.git_divergence``).

Four surfaces act on this count and two of them gate hard-to-undo actions
(the CLI hard reset, the unattended auto-apply's check verdict), so the
contract under test here is safety-relevant: the counting details live in
ONE place, and a count that cannot be read is a distinct failure value —
never ``(0, 0)``, which also means "in sync" and would silently turn a
fail-closed gate into a fail-open one.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from kiro_crew import git_divergence
from kiro_crew.git_divergence import (
    DIVERGENCE_TIMEOUT_SEC,
    UNREADABLE_GIT_FAILED,
    UNREADABLE_TIMEOUT,
    UNREADABLE_UNPARSEABLE,
    DivergenceCounts,
    DivergenceUnreadable,
    count_divergence,
    count_divergence_sync,
    divergence_count_args,
    parse_divergence_counts,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


class TestArgs:
    def test_three_dot_range_owns_the_fragile_spelling(self) -> None:
        assert divergence_count_args("@{u}") == [
            "rev-list",
            "--count",
            "--left-right",
            "HEAD...@{u}",
        ]

    def test_upstream_spelling_is_a_caller_choice(self) -> None:
        """Both real spellings stay reachable: the callers genuinely differ."""
        assert divergence_count_args("origin/main")[-1] == "HEAD...origin/main"
        assert divergence_count_args("@{upstream}")[-1] == "HEAD...@{upstream}"


class TestParse:
    def test_left_is_ahead_right_is_behind(self) -> None:
        assert parse_divergence_counts("3\t219\n") == DivergenceCounts(ahead=3, behind=219)

    def test_any_whitespace_split_is_accepted(self) -> None:
        """``--left-right --count`` prints a tab, but the split is not brittle."""
        assert parse_divergence_counts("2 1") == DivergenceCounts(ahead=2, behind=1)

    @pytest.mark.parametrize(
        "junk",
        ["", "garbage\n", "1\n", "1\t2\t3\n", "a\tb\n", "3.5\t2\n", "fatal: bad revision\n"],
    )
    def test_junk_is_none_never_zeros(self, junk: str) -> None:
        assert parse_divergence_counts(junk) is None


class _FakeProc:
    """A scripted ``asyncio`` subprocess: one (returncode, stdout) result."""

    def __init__(self, rc: int, out: bytes, *, hang: bool = False) -> None:
        self.returncode = rc
        self._out = out
        self._hang = hang
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang and not self.killed:
            await asyncio.sleep(30)
        return (self._out, b"")

    def kill(self) -> None:
        self.killed = True


class TestAsyncCount:
    def _patch(self, monkeypatch: pytest.MonkeyPatch, proc: _FakeProc) -> list[tuple[str, ...]]:
        calls: list[tuple[str, ...]] = []

        async def _exec(*args: str, **kwargs: object) -> _FakeProc:
            calls.append(tuple(args))
            return proc

        monkeypatch.setattr(git_divergence.asyncio, "create_subprocess_exec", _exec)
        return calls

    def test_success_returns_the_pair(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch(monkeypatch, _FakeProc(0, b"2\t5\n"))
        result = asyncio.run(count_divergence("/repo", "@{u}"))
        assert result == DivergenceCounts(ahead=2, behind=5)
        assert calls == [("git", "rev-list", "--count", "--left-right", "HEAD...@{u}")]

    def test_nonzero_exit_is_unreadable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, _FakeProc(128, b""))
        result = asyncio.run(count_divergence("/repo", "@{u}"))
        assert isinstance(result, DivergenceUnreadable)
        assert result.reason == UNREADABLE_GIT_FAILED

    def test_unparseable_output_is_unreadable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, _FakeProc(0, b"garbage\n"))
        result = asyncio.run(count_divergence("/repo", "@{u}"))
        assert isinstance(result, DivergenceUnreadable)
        assert result.reason == UNREADABLE_UNPARSEABLE
        assert result.detail == "garbage"

    def test_spawn_failure_is_unreadable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """git missing from PATH (or the repo path gone) is a count failure,
        not a traceback for every caller to degrade through differently."""

        async def _exec(*args: str, **kwargs: object) -> _FakeProc:
            raise FileNotFoundError("git: command not found")

        monkeypatch.setattr(git_divergence.asyncio, "create_subprocess_exec", _exec)
        result = asyncio.run(count_divergence("/repo", "@{u}"))
        assert isinstance(result, DivergenceUnreadable)
        assert result.reason == UNREADABLE_GIT_FAILED
        assert "git" in result.detail

    def test_timeout_kills_the_child_and_is_unreadable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProc(0, b"0\t0\n", hang=True)
        self._patch(monkeypatch, proc)
        result = asyncio.run(count_divergence("/repo", "@{u}", timeout=0.05))
        assert isinstance(result, DivergenceUnreadable)
        assert result.reason == UNREADABLE_TIMEOUT
        assert proc.killed


class TestSyncCount:
    def _patch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        rc: int = 0,
        stdout: str = "",
        stderr: str = "",
        timeout: bool = False,
    ) -> list[list[str]]:
        calls: list[list[str]] = []

        def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(argv))
            if timeout:
                raise subprocess.TimeoutExpired(argv, DIVERGENCE_TIMEOUT_SEC)
            return subprocess.CompletedProcess(argv, rc, stdout, stderr)

        monkeypatch.setattr(subprocess, "run", _run)
        return calls

    def test_success_returns_the_pair(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch(monkeypatch, stdout="0\t7\n")
        result = count_divergence_sync("/repo", "origin/main")
        assert result == DivergenceCounts(ahead=0, behind=7)
        assert calls == [["git", "rev-list", "--count", "--left-right", "HEAD...origin/main"]]

    def test_nonzero_exit_carries_gits_own_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The CLI prints git's message, so the failure must carry it."""
        self._patch(monkeypatch, rc=128, stderr="fatal: bad revision\n")
        result = count_divergence_sync("/repo", "origin/main")
        assert isinstance(result, DivergenceUnreadable)
        assert result.reason == UNREADABLE_GIT_FAILED
        assert result.detail == "fatal: bad revision"

    def test_nonzero_exit_falls_back_to_stdout_detail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch(monkeypatch, rc=1, stdout="something odd\n")
        result = count_divergence_sync("/repo", "origin/main")
        assert isinstance(result, DivergenceUnreadable)
        assert result.detail == "something odd"

    def test_unparseable_output_is_unreadable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, stdout="garbage\n")
        result = count_divergence_sync("/repo", "origin/main")
        assert isinstance(result, DivergenceUnreadable)
        assert result.reason == UNREADABLE_UNPARSEABLE

    def test_spawn_failure_is_unreadable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("git: command not found")

        monkeypatch.setattr(subprocess, "run", _boom)
        result = count_divergence_sync("/repo", "origin/main")
        assert isinstance(result, DivergenceUnreadable)
        assert result.reason == UNREADABLE_GIT_FAILED

    def test_timeout_is_unreadable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, timeout=True)
        result = count_divergence_sync("/repo", "origin/main")
        assert isinstance(result, DivergenceUnreadable)
        assert result.reason == UNREADABLE_TIMEOUT


class TestFailureIsNeverInSync:
    """The single most important property of the shared counter.

    ``(0, 0)`` means "in sync". Two of the callers gate hard-to-undo actions
    on that answer, so a failure that surfaced as a zero pair would convert
    their fail-closed refusal into a silent go-ahead. Every failure mode must
    therefore come back as :class:`DivergenceUnreadable` — a type with no
    ``ahead``/``behind`` to read — and never as a counts value.
    """

    def test_no_async_failure_mode_reads_as_in_sync(self, monkeypatch: pytest.MonkeyPatch) -> None:
        failures = [
            _FakeProc(128, b""),  # git failed
            _FakeProc(0, b""),  # empty output
            _FakeProc(0, b"garbage\n"),  # unparseable
            _FakeProc(0, b"0\t0\n", hang=True),  # timeout (even with clean output)
        ]
        for proc in failures:

            async def _exec(*args: str, _proc: _FakeProc = proc, **kwargs: object) -> _FakeProc:
                return _proc

            monkeypatch.setattr(git_divergence.asyncio, "create_subprocess_exec", _exec)
            result = asyncio.run(count_divergence("/repo", "@{u}", timeout=0.05))
            assert isinstance(result, DivergenceUnreadable)
            assert not isinstance(result, DivergenceCounts)
            assert not hasattr(result, "ahead")

    def test_no_sync_failure_mode_reads_as_in_sync(self, monkeypatch: pytest.MonkeyPatch) -> None:
        shapes: list[dict[str, object]] = [
            {"rc": 128, "stdout": ""},
            {"rc": 0, "stdout": ""},
            {"rc": 0, "stdout": "garbage\n"},
        ]
        for shape in shapes:

            def _run(
                argv: list[str], _shape: dict[str, object] = shape, **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    argv, int(_shape["rc"]), str(_shape["stdout"]), ""
                )

            monkeypatch.setattr(subprocess, "run", _run)
            result = count_divergence_sync("/repo", "origin/main")
            assert isinstance(result, DivergenceUnreadable)
            assert not hasattr(result, "ahead")

        def _boom(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, DIVERGENCE_TIMEOUT_SEC)

        monkeypatch.setattr(subprocess, "run", _boom)
        result = count_divergence_sync("/repo", "origin/main")
        assert isinstance(result, DivergenceUnreadable)
        assert not hasattr(result, "ahead")


class TestNoHandRolledCopies:
    """A future consumer must go through the shared counter, not re-roll it.

    The tripwire greps the one spelling the helper owns — ``--left-right`` —
    which is what every historical copy used and what a copy-paste of any of
    them reintroduces. A from-scratch reimplementation via two one-directional
    ``rev-list --count`` calls or ``status -sb`` parsing is outside its reach
    on purpose: widening the pattern would flag legitimate one-directional
    counts (the doctor's behind-only readout) and grow the allowlist, and a
    guard with a broad allowlist reads as protection while permitting the
    regression. That class stays a review concern.
    """

    # The ONLY files allowed to spell ``--left-right`` by hand. Keep this list
    # explicit and short: a broad allowlist reads as protection while
    # permitting the regression this guard exists to catch.
    _ALLOWED = {
        # The owner.
        Path("src/kiro_crew/git_divergence.py"),
        # A standalone skill script shipped to users' machines; it must stay
        # dependency-free, so it cannot import the shared module.
        Path("src/kiro_crew/builtin_skills/kirocrew-dev/prepare-pr/scripts/preflight.py"),
    }

    def test_no_new_hand_rolled_divergence_count(self) -> None:
        offenders: list[str] = []
        for path in sorted((_REPO_ROOT / "src" / "kiro_crew").rglob("*.py")):
            rel = path.relative_to(_REPO_ROOT)
            if rel in self._ALLOWED:
                continue
            if "--left-right" in path.read_text(encoding="utf-8"):
                offenders.append(str(rel))
        assert not offenders, (
            "Hand-rolled ahead/behind divergence count outside the shared "
            "helper. Route the counting through kiro_crew.git_divergence "
            "(count_divergence / count_divergence_sync, or "
            "divergence_count_args + parse_divergence_counts for a caller "
            "with its own hardened spawn path) instead of re-rolling "
            "`rev-list --left-right`:\n  " + "\n  ".join(offenders)
        )
