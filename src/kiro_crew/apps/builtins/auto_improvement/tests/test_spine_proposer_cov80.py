"""Proposer worktree machinery — the git plumbing Phase B runs before any gate.

The parts pinned here are the ones a candidate's isolation depends on and that no
other suite reaches: the hardened ``git`` argv (every host-side call over an
agent-writable tree must carry the safe-config overrides), the idempotent
worktree/branch lifecycle, the agent-authoring escalation for candidates whose profile
produces no mechanical seed, and the clean-stop check inside ``fan_out`` — without
which a stop request would still pay for every remaining agent pass in the cycle.

No real git runs: the module-level ``_git`` helper and ``subprocess.run`` are the seams.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine import proposer as P
from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    Candidate,
    Proposal,
    TargetProfile,
)


@dataclass
class FakeCandidate:
    target: str = "src/mod.py::fn"
    signature: str = "sig"
    hypothesis: str = ""
    kind: str = "bug"


class FakeProfile:
    """Profile whose ``propose`` reports whether it seeded an edit itself."""

    def __init__(self, produced: bool = True, *, raises: Exception | None = None) -> None:
        self.produced = produced
        self.raises = raises
        self.calls: list[str] = []

    def propose(self, *, candidate, base_sha, worktree, tier):
        self.calls.append(tier)
        if self.raises is not None:
            raise self.raises
        return self.produced


def _profile(**kw: Any) -> TargetProfile:
    """A ``TargetProfile``-shaped double, cast once at construction.

    The proposer touches only the handful of attributes ``FakeProfile`` defines, so a
    structural double is the right test input; casting here (rather than suppressing
    ``arg-type`` at each call) states that intent in one place and keeps later call
    sites annotation-free. Same convention as ``test_measurer.py`` / ``test_preflight.py``.
    """
    return cast(TargetProfile, FakeProfile(**kw))


def _candidate(**kw: Any) -> Candidate:
    """A ``Candidate``-shaped double, cast once at construction. See ``_profile``."""
    return cast(Candidate, FakeCandidate(**kw))


def _ok(**kw):
    return SimpleNamespace(returncode=0, stdout=kw.get("stdout", ""), stderr="")


def _proposer(tmp_path: Path, **kw) -> P.Proposer:
    return P.Proposer(clone=tmp_path / "clone", worktree_root=tmp_path / "wt", **kw)


class TestHardenedGitArgv:
    def test_every_call_is_pinned_and_carries_the_safe_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pin must be established BEFORE git spawns, and the ``-c`` overrides
        must sit on our own argv where they beat the repo config."""
        pinned: list[Path] = []
        seen: dict[str, object] = {}
        monkeypatch.setattr(P, "require_pinned", lambda cwd: pinned.append(Path(cwd)))

        def _run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["kwargs"] = kwargs
            return _ok()

        monkeypatch.setattr(P.subprocess, "run", _run)

        out = P._git(["status"], tmp_path)

        assert pinned == [tmp_path]
        argv = seen["argv"]
        assert argv[:3] == ["git", "-C", str(tmp_path)]  # type: ignore[index]
        for flag in P._GIT_SAFE_CONFIG:
            assert flag in argv  # type: ignore[operator]
        assert argv[-1] == "status"  # type: ignore[index]
        assert seen["kwargs"]["capture_output"] is True  # type: ignore[index]
        assert out.returncode == 0


class TestWorktreeLifecycle:
    def test_a_new_worktree_clears_any_orphan_before_adding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crashed prior cycle leaves a worktree and branch behind; the add would
        fail on both, so the removal is idempotent cleanup, not optional."""
        calls: list[list[str]] = []

        def _git_spy(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
            calls.append(list(args))
            return _ok()

        monkeypatch.setattr(P, "_git", _git_spy)

        wt, branch = _proposer(tmp_path)._new_worktree("c1_wide_mod_abcd1234", "base0")

        assert wt == tmp_path / "wt" / "c1_wide_mod_abcd1234"
        assert branch == "cand/c1_wide_mod_abcd1234"
        assert calls[0][:2] == ["worktree", "remove"]
        assert calls[1][:2] == ["branch", "-D"]
        assert calls[2][:2] == ["worktree", "add"]
        assert calls[2][-1] == "base0"
        assert (tmp_path / "wt").is_dir()

    def test_a_failed_worktree_add_raises_with_gits_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _git(args, cwd):
            if args[:2] == ["worktree", "add"]:
                return SimpleNamespace(returncode=128, stdout="", stderr=" fatal: bad object \n")
            return _ok()

        monkeypatch.setattr(P, "_git", _git)
        with pytest.raises(RuntimeError, match="fatal: bad object"):
            _proposer(tmp_path)._new_worktree("c1_wide_x_0000", "nosuchsha")

    def test_teardown_drops_both_the_worktree_and_its_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Leaving either behind collides with the next cycle's cand_id."""
        calls: list[list[str]] = []

        def _git_spy(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
            calls.append(list(args))
            return _ok()

        monkeypatch.setattr(P, "_git", _git_spy)
        proposal = Proposal(
            cand_id="c1_wide_x_0000",
            candidate=_candidate(),
            worktree=tmp_path / "wt" / "c1_wide_x_0000",
            branch="cand/c1_wide_x_0000",
            description="d",
        )

        _proposer(tmp_path).teardown(proposal)

        assert calls == [
            ["worktree", "remove", "--force", str(proposal.worktree)],
            ["branch", "-D", "cand/c1_wide_x_0000"],
        ]


class TestCapturedDiff:
    def test_it_stages_untracked_files_and_excludes_tooling_artifacts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bug fix's reproducing test is always a NEW file, so the diff has to run
        against a staged index; ``uv.lock`` and agent settings must be excluded or the
        downstream ``git apply`` onto the clone collides."""
        pinned: list[Path] = []
        argvs: list[list[str]] = []
        monkeypatch.setattr(P, "require_pinned", lambda cwd: pinned.append(Path(cwd)))

        def _run(argv, **kwargs):
            argvs.append(list(argv))
            return _ok(stdout="diff --git a/x b/x\n")

        monkeypatch.setattr(P.subprocess, "run", _run)
        wt = tmp_path / "wt" / "c1"

        diff = _proposer(tmp_path)._capture_diff(wt, "base0")

        assert diff == "diff --git a/x b/x\n"
        assert pinned == [wt]
        assert argvs[0][-2:] == ["add", "-A"]
        assert "--cached" in argvs[1]
        assert ":(exclude)uv.lock" in argvs[1]
        assert ":(exclude).kiro/**" in argvs[1]


class TestAgentAuthoringEscalation:
    def _wire(self, monkeypatch: pytest.MonkeyPatch, authored: dict) -> None:
        monkeypatch.setattr(
            P.Proposer,
            "_new_worktree",
            lambda self, cand_id, base_sha: (self.worktree_root / cand_id, f"cand/{cand_id}"),
        )
        monkeypatch.setattr(P.Proposer, "_capture_diff", lambda self, wt, base: "diff --git\n+x\n")
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as AR

        def _bug(runner, *, candidate, worktree, test_cmd_hint=None):
            authored["bug"] = {"runner": runner, "hint": test_cmd_hint}
            return True

        def _perf(runner, *, candidate, worktree, test_cmd_hint=None):
            authored["perf"] = {"runner": runner, "hint": test_cmd_hint}
            return True

        monkeypatch.setattr(AR, "author_bug_fix", _bug)
        monkeypatch.setattr(AR, "author_perf_fix", _perf)

    def test_a_bug_candidate_with_no_seed_is_authored_by_the_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``propose`` returning False means "awaiting the agent edit", not "no defect"
        — with a runner wired the fix is authored and the diff is captured."""
        authored: dict = {}
        self._wire(monkeypatch, authored)
        runner = object()
        profile = _profile(produced=False)
        profile.bug_runner = SimpleNamespace(agent_test_hint=lambda wt: "pytest -q")  # type: ignore[attr-defined]

        out = _proposer(tmp_path, agent_runner=runner).propose_one(
            profile=profile,
            candidate=_candidate(kind="bug"),
            base_sha="base0",
            cycle=2,
            tier="wide",
        )

        assert authored["bug"]["runner"] is runner
        assert authored["bug"]["hint"] == "pytest -q"  # the gate's known-good command
        assert "perf" not in authored
        assert out.skipped is False
        assert out.diff.startswith("diff --git")

    def test_a_perf_candidate_routes_to_the_perf_author_with_no_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A runner exposing no ``agent_test_hint`` must leave the hint empty rather
        than blow up — and the perf track must not be dispatched to the bug author."""
        authored: dict = {}
        self._wire(monkeypatch, authored)

        out = _proposer(tmp_path, agent_runner=object()).propose_one(
            profile=_profile(produced=False),
            candidate=_candidate(kind="perf"),
            base_sha="base0",
            cycle=2,
            tier="deep",
        )

        assert authored["perf"]["hint"] is None
        assert "bug" not in authored
        assert out.tier == "deep"
        assert out.skipped is False

    def test_offline_a_seedless_candidate_is_no_defect_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no runner wired the spine must skip honestly rather than fabricate."""
        authored: dict = {}
        self._wire(monkeypatch, authored)

        out = _proposer(tmp_path).propose_one(
            profile=_profile(produced=False),
            candidate=_candidate(),
            base_sha="base0",
            cycle=1,
            tier="wide",
        )

        assert authored == {}
        assert out.skipped is True
        assert out.skip_reason == "no diff produced"
        assert out.diff == ""


class TestFanOutStopCheck:
    def test_a_stop_request_aborts_before_the_next_agent_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cycle can spawn N multi-minute agent calls; a stop must land inside the
        fan-out, not only between cycles."""
        seen: list[str] = []

        def _propose_spy(
            self: object,
            *,
            profile: object,
            candidate: Candidate,
            base_sha: str,
            cycle: int,
            tier: str,
        ) -> Proposal:
            seen.append(f"{tier}:{candidate.target}")
            return Proposal(
                cand_id=candidate.target,
                candidate=candidate,
                worktree=tmp_path,
                branch="b",
                description="d",
            )

        monkeypatch.setattr(P.Proposer, "propose_one", _propose_spy)
        cands = [_candidate(target=f"src/m{i}.py::f") for i in range(4)]
        stop_after = {"n": 2}

        def _stop() -> bool:
            if stop_after["n"] <= 0:
                return True
            stop_after["n"] -= 1
            return False

        proposals = _proposer(tmp_path, wide=3, deep=1).fan_out(
            profile=_profile(),
            candidates=cands,
            base_sha="base0",
            cycle=1,
            stop_check=_stop,
        )

        # Two wide proposals ran, then the stop broke the wide loop; the deep loop's
        # own check then broke immediately, so no deep proposal was paid for.
        assert seen == ["wide:src/m0.py::f", "wide:src/m1.py::f"]
        assert len(proposals) == 2

    def test_without_a_stop_check_wide_and_deep_slices_stay_disjoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        def _propose_spy(
            self: object,
            *,
            profile: object,
            candidate: Candidate,
            base_sha: str,
            cycle: int,
            tier: str,
        ) -> Proposal:
            seen.append(f"{tier}:{candidate.target}")
            return Proposal(
                cand_id=candidate.target,
                candidate=candidate,
                worktree=tmp_path,
                branch="b",
                description="d",
            )

        monkeypatch.setattr(P.Proposer, "propose_one", _propose_spy)
        cands = [_candidate(target=f"src/m{i}.py::f") for i in range(3)]

        _proposer(tmp_path, wide=2, deep=1).fan_out(
            profile=_profile(), candidates=cands, base_sha="base0", cycle=1
        )

        assert seen == ["wide:src/m0.py::f", "wide:src/m1.py::f", "deep:src/m2.py::f"]


class TestProposeErrors:
    def test_a_raising_profile_is_recorded_as_an_error_not_a_no_defect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            P.Proposer,
            "_new_worktree",
            lambda self, cand_id, base_sha: (self.worktree_root / cand_id, f"cand/{cand_id}"),
        )
        out = _proposer(tmp_path).propose_one(
            profile=_profile(raises=RuntimeError("boom")),
            candidate=_candidate(),
            base_sha="base0",
            cycle=1,
            tier="wide",
        )
        assert out.skipped is True
        assert "RuntimeError: boom" in out.skip_reason
        assert out.skip_status == "error"


class TestCandIdTokens:
    def test_distinct_loci_sharing_a_basename_get_distinct_worktree_names(self) -> None:
        """Colliding cand_ids would make the idempotent removal wipe a live worktree."""
        a, b = "a/util.py::f", "b/util.py::f"
        assert P._short(a) == P._short(b) == "util_py_f"
        assert P._disambig(a) != P._disambig(b)
        assert len(P._disambig(a)) == 8

    def test_the_short_token_is_filesystem_safe_and_bounded(self) -> None:
        token = P._short("src/very/deep/" + "n" * 200 + ".py::fn")
        assert len(token) == 48
        assert all(ch.isalnum() or ch in "_-" for ch in token)

    def test_an_empty_locus_still_yields_a_usable_token(self) -> None:
        assert P._short("") == "cand"
