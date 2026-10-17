"""Auto-improvement regressions: what the bug and perf tracks attempt, measure and keep.

A nested target joins its evidence, the repro test directory follows the
repository, the canary and the ruler are proven before the perf track runs, the perf
track keeps its evolutionary head, a configured scope never fails open, and only the
bug track may add tests.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.apps.builtins.auto_improvement.tests.dogfood_learnings_helpers import (  # noqa: F401
    _cwd_is_the_scratch_dir,
)


class TestNestedTargetJoinsItsEvidence:
    """A finding on a DEEPLY NESTED file rendered its diff with no explanation.

    The detail endpoint slugged the FULL target
    (``kiro_crew_apps_builtins_auto_improvement_spine_contracts_py_proposal``) and
    required it to be a substring of the candidate filename — but ``proposer._short``
    builds the cand_id from the file's BASENAME
    (``c6_wide_contracts_py_Proposal_2cbc5716``). The substring test could therefore
    never match for a nested file, so ``candidate`` came back None and the UI showed a
    raw diff with no defect statement, hypothesis, or repro test. The evidence was on
    disk the whole time — only the join was broken.
    """

    def test_a_nested_target_slugs_to_its_basename(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend.progress import _target_slug

        slug = _target_slug(
            "src/kiro_crew/apps/builtins/auto_improvement/spine/contracts.py::Proposal"
        )
        assert slug == "contracts_py_proposal"
        # And it must actually match the cand_id the proposer produces.
        assert slug in "c6_wide_contracts_py_Proposal_2cbc5716".lower()

    def test_the_slug_matches_the_proposers_own_token(self) -> None:
        """Both sides must derive from the basename, or the join silently breaks again."""
        from kiro_crew.apps.builtins.auto_improvement.backend.progress import _target_slug
        from kiro_crew.apps.builtins.auto_improvement.spine.proposer import _short

        for target in (
            "src/a/b/c/mod.py::Klass.method",
            "src/search.py::negamax_root",
            "flat.py::fn",
        ):
            assert _target_slug(target) in _short(target).lower()

    def test_a_shallow_target_still_joins(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend.progress import _target_slug

        assert _target_slug("src/search.py::negamax_root") == "search_py_negamax_root"

    def test_the_slug_is_capped_like_the_cand_id(self) -> None:
        """``_short`` truncates at 48 chars; a longer slug would never be a substring."""
        from kiro_crew.apps.builtins.auto_improvement.backend.progress import _target_slug

        long_target = "src/pkg/" + ("a" * 80) + ".py::Sym"
        assert len(_target_slug(long_target)) <= 48


class TestReproTestDirIsRepoAware:
    """Found by executing docs/system-specs/modules/auto-improvement.md against a SECOND repo (Zedmor/chess_test).

    The authoring prompt and the candidate's declared repro path both hard-coded
    ``test/test_bug_*.py``. chess_test keeps its suite in ``tests/`` (plural), so the
    agent wrote into a directory that does not exist, T2 could never collect it, and
    EVERY candidate failed ``test_invalid`` regardless of fix quality — exactly the
    symptom seen: two candidates, both ``T2: reproducing test does not collect``.

    The edit fence already permitted both spellings, so only the instruction was wrong.
    """

    def _profile(self, clone: Path):
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.profile import (
            build_profile,
        )

        return build_profile({"clone": str(clone), "branch": "main"})

    def test_a_tests_plural_repo_is_detected(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n")
        assert self._profile(tmp_path)._test_dir() == "tests"

    def test_a_test_singular_repo_is_detected(self, tmp_path: Path) -> None:
        (tmp_path / "test").mkdir()
        (tmp_path / "test" / "test_a.py").write_text("def test_a(): pass\n")
        assert self._profile(tmp_path)._test_dir() == "test"

    def test_both_dirs_picks_the_DOMINANT_suite(self, tmp_path: Path) -> None:
        """A repo can have both; writing into the minor one puts the repro test outside
        the suite the gate actually runs (Kiro Crew: 776 under test/ vs 9 under tests/)."""
        (tmp_path / "test").mkdir()
        (tmp_path / "tests").mkdir()
        for i in range(5):
            (tmp_path / "test" / f"test_{i}.py").write_text("def test_x(): pass\n")
        (tmp_path / "tests" / "test_only.py").write_text("def test_x(): pass\n")
        assert self._profile(tmp_path)._test_dir() == "test"

    def test_neither_dir_falls_back_to_test(self, tmp_path: Path) -> None:
        assert self._profile(tmp_path)._test_dir() == "test"

    def test_the_runner_helper_agrees_with_the_profile(self, tmp_path: Path) -> None:
        """Two code paths decide this — the PROMPT (runner) and the DECLARED path
        (profile). If they disagree, the gate collects a file the agent did not write."""
        from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import (
            _repro_test_dir,
        )

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n")
        assert _repro_test_dir(tmp_path) == self._profile(tmp_path)._test_dir()

    def test_the_prompt_names_the_detected_dir(self, tmp_path: Path) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import (
            AgentResult,
            author_bug_fix,
        )
        from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
            TRACK_BUG,
            Candidate,
        )

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n")
        seen: dict = {}

        class _R:
            def run(self, prompt, **kw):
                seen["p"] = prompt
                return AgentResult(ok=True, text="no change")

        author_bug_fix(
            _R(),
            candidate=Candidate(kind=TRACK_BUG, target="src/m.py::f"),
            worktree=tmp_path,
        )
        assert "tests/test_bug_" in seen["p"]
        assert "test/test_bug_<short_slug>" not in seen["p"]


class TestThePerfTrackKeepsItsEvolutionaryHead:
    """The perf filed path deliberately does NOT reset, unlike the bug track's.

    Review asked for a `_reset_provisional(pre_sha)` after the perf filed-PR event, by analogy
    with the bug-track fix. Declined: the perf loop is EVOLUTIONARY — "current best == HEAD" is
    its documented durable state, `base_sha` is re-read from HEAD each cycle, and every
    measurement reads "Δ vs current best". Resetting would make each cycle re-measure against
    the ORIGINAL base, so a second improvement to the same hot path could never register.

    The reported harm is real but is recorded as a known limitation, because the obvious remedy
    is not a safe drop-in: measured, rebuilding a per-winner branch from the remote base when
    two cycles touch the SAME line produced a branch containing NEITHER fix.

    This test pins the DIVERGENCE so a future "consistency" cleanup cannot quietly reset the
    perf path and silently disable cumulative measurement.
    """

    def test_the_perf_filed_path_does_not_reset(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        src = inspect.getsource(Driver._apply_verdict)
        filed_arm = src[
            src.index('"kind": "perf"') : src.index("        else:", src.index('"kind": "perf"'))
        ]
        assert "self._reset_provisional(pre_sha)" not in filed_arm, (
            "the perf filed path now resets — that re-measures every cycle against the "
            "ORIGINAL base and disables the evolutionary 'Δ vs current best' premise. If this "
            "is intended, the module docstring's 'current best == HEAD' claim must change too."
        )

    def test_the_bug_filed_path_does_reset(self) -> None:
        """The two tracks are asymmetric ON PURPOSE — this is the contrast."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        src = inspect.getsource(Driver._apply_bug_winner)
        filed_arm = src[
            src.index("cr_filed={") : src.index("        else:", src.index("cr_filed={"))
        ]
        assert "self._reset_provisional(pre_sha)" in filed_arm


class TestCalibrationRefusesABadCanary:
    """`POST /calibrate` used `abs(delta) > band`, which ignored `ok` AND the direction.

    A canary is the one measurement whose sign is known a priori, so direction-blindness here
    defeats the whole point of proving the ruler. Measured against the spine's predicate at
    band=10 (minimize): a REGRESSION of `+25` passed, and a canary with `ok=False` passed —
    both writing `status="calibrated"` to `ruler.json`. The backend now reuses the spine's
    `_canary_clears_band` rather than keeping a second, weaker copy; a duplicate predicate is
    exactly how these drifted apart.

    Raised by the GPT review of this branch.
    """

    @staticmethod
    def _canary(*, ok: bool, delta: float | None):
        from kiro_crew.apps.builtins.auto_improvement.spine.contracts import Measurement

        return Measurement(ok=ok, primary_delta=delta)

    def test_a_regression_is_not_a_cleared_canary(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.preflight import (
            _canary_clears_band,
        )

        # `minimize`: a real win is MORE NEGATIVE. The same magnitude the wrong way round is a
        # regression, and `abs()` could not tell them apart.
        assert _canary_clears_band(
            self._canary(ok=True, delta=-25.0), band=10.0, direction="minimize"
        )
        assert not _canary_clears_band(
            self._canary(ok=True, delta=+25.0), band=10.0, direction="minimize"
        ), "a regression was accepted as a proven ruler"
        # `maximize` is the mirror image.
        assert _canary_clears_band(
            self._canary(ok=True, delta=+25.0), band=10.0, direction="maximize"
        )
        assert not _canary_clears_band(
            self._canary(ok=True, delta=-25.0), band=10.0, direction="maximize"
        )

    def test_a_failed_measurement_is_not_a_cleared_canary(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.preflight import (
            _canary_clears_band,
        )

        assert not _canary_clears_band(
            self._canary(ok=False, delta=-25.0), band=10.0, direction="minimize"
        ), "a FAILED measurement was accepted as a proven ruler"
        assert not _canary_clears_band(
            self._canary(ok=True, delta=None), band=10.0, direction="minimize"
        )

    def test_the_backend_uses_the_spine_predicate(self) -> None:
        """Structural: a second copy of this rule is how the two drifted apart."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import runner as R

        src = inspect.getsource(R.RunSupervisor)
        assert "_canary_clears_band(" in src, "the backend re-derives the canary verdict"
        # Match the ASSIGNMENT, not the bare expression: the comment above the fix quotes the
        # old rule to explain what went wrong, and a substring check found the prose. (Caught
        # by this test failing on my own comment — the same class of false positive as the
        # `recipe.draft(` docstring match earlier in this branch.)
        assert "cleared = abs(observed) > band" not in src, "the direction-blind rule is back"


class TestAnUnprovenRulerHaltsThePerfTrack:
    """`canaryAdvisory` defaulted to True, so a perf run whose canary FAILED still entered
    Phase 2 and could keep + draft a "win" measured by a ruler that was never proven.

    The justification for the loose default — written into this repo's own docs — was that
    strict mode "would halt bug-track runs, which never consult the ruler". That is false:
    `Driver.run` skips Phase-1 preflight ENTIRELY for the bug track
    (``driver.py``: "preflight: skipped for bug track"), so a strict canary cannot reach a
    bug run at all. With the stated cost of strictness non-existent, the measurement-first
    contract (03_metric §7.1: an unproven ruler must HALT) wins, and the flag stays only as
    an explicit operator opt-out. Raised by the GPT review; the decline that preceded it was
    reversed after re-deriving the bug-track claim against the code.
    """

    def test_the_backend_default_is_strict(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import runner as runner_mod

        src = inspect.getsource(runner_mod.RunSupervisor._build_driver_locked)
        assert 'canary_advisory=_as_bool(config.get("canaryAdvisory"), False)' in src, (
            "the backend must default the canary to STRICT; an advisory default lets an "
            "unproven ruler into Phase 2"
        )

    def test_an_operator_can_still_opt_out(self) -> None:
        """Strict by DEFAULT, not mandatory: a target whose suite genuinely cannot force a
        measurable win still needs a way to run the loop."""
        from typing import Any

        from kiro_crew.apps.builtins.auto_improvement.backend.runner import _as_bool

        # Annotated rather than inferred: an empty literal narrows its key type to
        # `Never`, so `{}.get("canaryAdvisory")` is a type error even though it is the
        # exact shape the caller passes (a config dict with the key absent).
        opted_in: dict[str, Any] = {"canaryAdvisory": True}
        absent: dict[str, Any] = {}
        assert _as_bool(opted_in.get("canaryAdvisory"), False) is True
        assert _as_bool(absent.get("canaryAdvisory"), False) is False

    def test_the_bug_track_never_reaches_the_canary(self) -> None:
        """The premise of the reversal. If this ever stops being true, the strict default
        starts halting bug runs and has to be reconsidered — so it is pinned, not assumed.
        """
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        src = inspect.getsource(Driver.run)
        assert "TRACK_BUG" in src and "run_preflight = False" in src, (
            "the bug track no longer skips preflight — the strict canary default now "
            "affects bug runs and its justification must be re-derived"
        )


class TestAConfiguredScopeNeverFailsOpen:
    """The scope refusal was gated on `not _ref_resolves(...)`, so it only fired for a base
    that does not exist. A base that RESOLVES but whose diff fails still left `_scope is None`
    — and `None` means "unscoped", which widens the edit fence from "what this branch changed"
    to the WHOLE REPOSITORY. That is the exact inversion of what setting a scope is for, and
    it is the third variant of this same bug on this branch (unresolvable ref, then
    `scopeDiffBase=HEAD`'s empty diff, now a git error).

    Reproduced on real repos: two clones with UNRELATED histories give
    `git rev-parse --verify <ref>` rc=0 while `git diff <ref>...HEAD` exits 128 with
    "no merge base". So `_ref_resolves` says yes, `scoped_relpaths` returns None, and nothing
    refuses.

    The condition is now simply "a scope was configured but could not be computed" — the
    REASON is irrelevant, because every reason has the same consequence. Raised by the GPT
    review.
    """

    def test_a_resolvable_base_with_no_merge_base_is_refused(self, tmp_path) -> None:
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.profiles import build_profile

        def _git(*args, cwd):
            return subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
            )

        # Two unrelated histories in one clone: `imported` resolves, but shares no ancestor
        # with HEAD, so a three-dot diff against it is a git ERROR rather than an empty set.
        other = tmp_path / "other"
        other.mkdir()
        _git("init", "-q", cwd=other)
        _git("config", "user.email", "t@example.invalid", cwd=other)
        _git("config", "user.name", "T", cwd=other)
        (other / "o.txt").write_text("o\n", encoding="utf-8")
        _git("add", "-A", cwd=other)
        _git("commit", "-qm", "o", cwd=other)

        clone = tmp_path / "clone"
        clone.mkdir()
        _git("init", "-q", cwd=clone)
        _git("config", "user.email", "t@example.invalid", cwd=clone)
        _git("config", "user.name", "T", cwd=clone)
        (clone / "c.txt").write_text("c\n", encoding="utf-8")
        _git("add", "-A", cwd=clone)
        _git("commit", "-qm", "c", cwd=clone)
        _git("fetch", "-q", str(other), "HEAD:imported", cwd=clone)

        # Precondition: the ref resolves, so the OLD guard would have passed it through.
        assert _git("rev-parse", "--verify", "--quiet", "imported", cwd=clone).returncode == 0
        assert (
            _git("diff", "--name-only", "imported...HEAD", cwd=clone).returncode != 0
        ), "the fixture no longer reproduces a resolvable-but-undiffable base"

        import pytest as _pytest

        with _pytest.raises(ValueError, match="scopeDiffBase"):
            build_profile({"clone": str(clone), "branch": "main", "scopeDiffBase": "imported"})

    def test_a_working_scope_is_still_accepted(self, tmp_path) -> None:
        """The refusal must not reject a legitimate scope."""
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.profiles import build_profile

        def _git(*args, cwd=None):
            return subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
            )

        clone = tmp_path / "ok-clone"
        clone.mkdir()
        _git("init", "-q", cwd=clone)
        _git("config", "user.email", "t@example.invalid", cwd=clone)
        _git("config", "user.name", "T", cwd=clone)
        (clone / "a.txt").write_text("a\n", encoding="utf-8")
        _git("add", "-A", cwd=clone)
        _git("commit", "-qm", "a", cwd=clone)
        base = _git("rev-parse", "HEAD", cwd=clone).stdout.strip()
        (clone / "b.txt").write_text("b\n", encoding="utf-8")
        _git("add", "-A", cwd=clone)
        _git("commit", "-qm", "b", cwd=clone)

        prof = build_profile({"clone": str(clone), "branch": "main", "scopeDiffBase": base})
        assert prof is not None, "a valid scope was refused"


class TestOnlyTheBugTrackMayAddTests:
    """`RepoEditAllowlist` documents the asymmetry it exists to enforce — "the perf track may
    not touch `tests/**` AT ALL, because the suite is the ruler's measurement subject and
    editing it is metric gaming the build gate cannot see" — but `__init__` never received the
    track, so the added-reproducing-test carve-out applied to BOTH tracks.

    That is the exact gaming the fence is for. The RH guard compares collected test COUNTS
    (`RH_TEST_COUNT`), so adding one cheap test while an expensive one stops being collected
    keeps the count equal while the measured suite time drops — a "win" that is purely an
    artifact of editing the ruler, drafted as a real perf PR.

    The carve-out is now conditional on `TRACK_BUG`, where it is load-bearing: a bug fix
    without a reproducing test cannot be proven RED→GREEN. Raised by the GPT review.
    """

    def _fence(self, track: str):
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.profile import (
            RepoEditAllowlist,
        )

        return RepoEditAllowlist(track=track)

    def test_the_perf_track_cannot_add_a_test(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.contracts import TRACK_PERF

        fence = self._fence(TRACK_PERF)
        ok, offending = fence.allows_changes([("A", "tests/test_repro_x.py")])
        assert ok is False and offending == ["tests/test_repro_x.py"], (
            "a perf candidate added a test file — the suite is the ruler's own measurement "
            "subject, so this is metric gaming the build gate cannot see"
        )

    def test_the_bug_track_can_still_add_a_test(self) -> None:
        """The carve-out must survive where it is load-bearing."""
        from kiro_crew.apps.builtins.auto_improvement.spine.contracts import TRACK_BUG

        fence = self._fence(TRACK_BUG)
        ok, offending = fence.allows_changes([("A", "tests/test_repro_x.py")])
        assert (
            ok is True and offending == []
        ), "the bug track lost its reproducing-test carve-out; a fix cannot be proven RED→GREEN"

    def test_neither_track_may_MODIFY_an_existing_test(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.contracts import TRACK_BUG, TRACK_PERF

        for track in (TRACK_BUG, TRACK_PERF):
            fence = self._fence(track)
            ok, _offending = fence.allows_changes([("M", "tests/test_core.py")])
            assert ok is False, f"{track} was allowed to modify an existing test"

    def test_the_profile_passes_its_track_to_the_fence(self) -> None:
        """Structural: the fence cannot enforce a track it was never told."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as gp

        src = inspect.getsource(gp.GitHubRepoProfile.__init__)
        assert "RepoEditAllowlist(" in src, "the construction site moved"
        call = src[src.index("RepoEditAllowlist(") :]
        assert "track=" in call[: call.index(")")], (
            "the profile builds its edit fence without a track, so the perf prohibition the "
            "class docstring promises is unenforced"
        )
