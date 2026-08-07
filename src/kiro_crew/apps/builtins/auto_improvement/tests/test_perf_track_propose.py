"""The perf track can propose at all — the high-severity parity gap.

Upstream shipped ~24 hand-written mechanical perf seeds per target, so its
``propose()`` realized a real diff. The port's target-agnostic profile has no seeds
and returns False for every candidate ("no mechanical seed"), and the proposer only
escalated to the model when ``candidate.kind == TRACK_BUG``. So a TRACK_PERF candidate
produced NO diff, was recorded ``no_defect``, and the loop could never keep, measure,
or file a perf win — the whole track was dead-ended at Phase B.

These pin the escalation (``author_perf_fix``) and the guardrails its prompt must
carry, since the perf gate's failure mode is a plausible-looking "optimization" that
quietly changes behavior or games the ruler.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import (
    AgentResult,
    author_perf_fix,
)
from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    TRACK_BUG,
    TRACK_PERF,
    Candidate,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    root.mkdir()
    (root / "m.py").write_text("def f(xs):\n    return [x for x in xs if x in list(range(100))]\n")
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.invalid", cwd=root)
    _git("config", "user.name", "T", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "base", cwd=root)
    return root


def _candidate() -> Candidate:
    return Candidate(
        kind=TRACK_PERF,
        target="m.py::f",
        signature="rebuilds list(range(100)) for every element",
        hypothesis="hoist the membership set to module level",
    )


class _Editing:
    """Stands in for the model: applies a real behavior-preserving speedup."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.prompt = ""
        self.kwargs: dict = {}

    def run(self, prompt: str, **kw: object) -> AgentResult:
        self.prompt = prompt
        self.kwargs = dict(kw)
        (self.root / "m.py").write_text(
            "_S = frozenset(range(100))\n\n\ndef f(xs):\n    return [x for x in xs if x in _S]\n"
        )
        return AgentResult(ok=True, text="hoisted the set out of the loop")


class _NoEdit:
    """The honest no-win outcome: reports and changes nothing."""

    def run(self, prompt: str, **kw: object) -> AgentResult:
        return AgentResult(ok=True, text="NO WIN FOUND — already optimal")


class TestPerfAuthoring:
    def test_an_edit_is_reported_as_produced(self, worktree: Path) -> None:
        assert (
            author_perf_fix(_Editing(worktree), candidate=_candidate(), worktree=worktree) is True
        )

    def test_no_edit_is_not_reported_as_produced(self, worktree: Path) -> None:
        """The worktree, not the agent's prose, is the source of truth — a no-win must
        not enter the gate as a candidate diff."""
        assert author_perf_fix(_NoEdit(), candidate=_candidate(), worktree=worktree) is False

    def test_a_bounded_exit_still_harvests_the_edit(self, worktree: Path) -> None:
        """timeout / max_turns are EXPECTED outcomes whose finished work is on disk."""

        class _Bounded(_Editing):
            def run(self, prompt: str, **kw: object) -> AgentResult:
                super().run(prompt, **kw)
                return AgentResult(ok=False, error="max_turns (40) reached")

        assert (
            author_perf_fix(_Bounded(worktree), candidate=_candidate(), worktree=worktree) is True
        )

    def test_a_genuine_runner_failure_is_not_harvested(self, worktree: Path) -> None:
        class _Dead:
            def run(self, prompt: str, **kw: object) -> AgentResult:
                return AgentResult(ok=False, error="provider factory unavailable")

        assert author_perf_fix(_Dead(), candidate=_candidate(), worktree=worktree) is False

    def test_the_agent_is_cwd_scoped_to_the_worktree(self, worktree: Path) -> None:
        r = _Editing(worktree)
        author_perf_fix(r, candidate=_candidate(), worktree=worktree)
        assert r.kwargs.get("cwd") == str(worktree)


class TestPerfPromptGuardrails:
    """The perf gate's danger is a plausible edit that changes behavior or games the
    ruler, so these constraints must actually reach the model."""

    def _prompt(self, worktree: Path, test_cmd_hint: str | None = None) -> str:
        r = _Editing(worktree)
        author_perf_fix(r, candidate=_candidate(), worktree=worktree, test_cmd_hint=test_cmd_hint)
        return r.prompt

    @pytest.mark.parametrize(
        "needle",
        [
            "behavior-preserving",  # the core contract
            "RULER",  # editing the suite is metric gaming
            "reward-hack",  # test-count guard
            "noise band",  # the model does not decide the win
            "NO WIN FOUND",  # the honest exit
            "MINIMAL",  # reviewable diff
        ],
    )
    def test_prompt_states_the_constraint(self, worktree: Path, needle: str) -> None:
        assert needle in self._prompt(worktree)

    def test_the_candidate_context_is_included(self, worktree: Path) -> None:
        p = self._prompt(worktree)
        assert "m.py::f" in p
        assert "hoist the membership set to module level" in p

    def test_the_test_command_hint_is_passed_through(self, worktree: Path) -> None:
        """Without it the agent burns ~20 min rediscovering the interpreter."""
        p = self._prompt(worktree, test_cmd_hint="python -m pytest -q -o addopts=")
        assert "python -m pytest -q -o addopts=" in p

    def test_no_hint_omits_the_block_rather_than_printing_none(self, worktree: Path) -> None:
        assert "None" not in self._prompt(worktree).split("HOW TO RUN")[0]


class TestProposerDispatch:
    """The guard that dead-ended the track: escalation must cover BOTH kinds."""

    def test_perf_and_bug_select_different_authors(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine import proposer as P

        src = Path(P.__file__).read_text(encoding="utf-8")
        # The dispatch is by track, and no longer gated on TRACK_BUG alone.
        assert "author_bug_fix if candidate.kind == TRACK_BUG else author_perf_fix" in src
        assert "if not produced and candidate.kind == TRACK_BUG" not in src

    def test_both_authors_share_the_same_call_contract(self) -> None:
        """The proposer calls one or the other with identical kwargs, so their
        signatures must not drift."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import (
            author_bug_fix,
        )

        bug = inspect.signature(author_bug_fix).parameters
        perf = inspect.signature(author_perf_fix).parameters
        assert set(bug) == set(perf) == {"runner", "candidate", "worktree", "test_cmd_hint"}

    def test_track_constants_are_distinct(self) -> None:
        assert TRACK_PERF != TRACK_BUG


class TestLintSelfCheckInPrompts:
    """T1 rejects a candidate for ANY new lint finding its diff adds — including one in
    the test the agent just wrote. Observed live: a correct, fully RED→GREEN-verified fix
    to ``gate.py`` was thrown away for a single unused ``import pytest``. Both authoring
    prompts must therefore tell the agent to lint its own diff before finishing."""

    @staticmethod
    def _sources() -> list[tuple[str, str]]:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import (
            author_bug_fix,
        )

        return [(fn.__name__, inspect.getsource(fn)) for fn in (author_bug_fix, author_perf_fix)]

    def test_both_prompts_ask_the_agent_to_lint_its_diff(self) -> None:
        for name, src in self._sources():
            assert "ruff check" in src, f"{name} does not tell the agent to lint"

    def test_both_prompts_forbid_silencing_with_noqa(self) -> None:
        """Suppressing a finding instead of fixing it games the gate."""
        for name, src in self._sources():
            assert "noqa" in src, f"{name} does not forbid noqa-silencing"

    def test_both_prompts_scope_the_fix_to_the_agents_own_findings(self) -> None:
        """The repo's pre-existing findings are not the candidate's to fix — T1 compares
        SETS, so touching them widens the diff for no gate benefit."""
        for name, src in self._sources():
            assert "pre-existing" in src, f"{name} does not scope lint fixing"
