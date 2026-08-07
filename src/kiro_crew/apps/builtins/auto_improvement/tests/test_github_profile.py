"""The reference profile satisfies the spine's seam, and its edit fence holds.

Two properties are asserted here. First, CONFORMANCE: the assembled profile and each
of its six sub-adapters structurally satisfy the corresponding ``@runtime_checkable``
protocol — the spine's only compatibility contract, so a drifted signature must fail
here rather than three minutes into a live run.

Second, the EDIT FENCE asymmetry, which is the profile's real safety content: the perf
track may edit source but never the tests that judge it, while the bug track must be
able to ADD its own reproducing test. Both directions are asserted, including the
adjacent case that would quietly break the guarantee — MODIFYING an existing test
under a path where ADDING one is allowed.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.apps.builtins.auto_improvement.profiles import build_profile
from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as gp
from kiro_crew.apps.builtins.auto_improvement.spine.contracts import TRACK_BUG, TRACK_PERF
from kiro_crew.apps.builtins.auto_improvement.spine.profile import (
    BugRunner,
    BuildGate,
    CalibrationParams,
    EditAllowlist,
    IsolationRecipe,
    PRRecipe,
    Ruler,
    TargetProfile,
)


def _profile(tmp_path: Path, **kw) -> gp.GitHubRepoProfile:
    """A profile pointed at a throwaway path. Nothing here runs a subprocess."""
    kw.setdefault("clone_path", tmp_path / "clone")
    kw.setdefault("pr_queue_dir", tmp_path / "queue")
    return gp.GitHubRepoProfile(**kw)


class TestProtocolConformance:
    """The spine type-checks the profile structurally; assert every field satisfies it."""

    def test_profile_satisfies_target_profile(self, tmp_path: Path) -> None:
        assert isinstance(_profile(tmp_path), TargetProfile)

    def test_each_subfield_satisfies_its_protocol(self, tmp_path: Path) -> None:
        p = _profile(tmp_path)
        assert isinstance(p.ruler, Ruler)
        assert isinstance(p.build_gate, BuildGate)
        assert isinstance(p.bug_runner, BugRunner)
        assert isinstance(p.edit_allowlist, EditAllowlist)
        assert isinstance(p.isolation, IsolationRecipe)
        assert isinstance(p.pr_recipe, PRRecipe)
        assert isinstance(p.calibration, CalibrationParams)

    def test_field_aliases_resolve(self, tmp_path: Path) -> None:
        """Both spellings appear in the design docs; the mixin must serve both."""
        p = _profile(tmp_path)
        assert p.isolation_recipe is p.isolation
        assert p.calibration_params is p.calibration

    def test_ruler_declares_a_minimized_seconds_metric(self, tmp_path: Path) -> None:
        ruler = _profile(tmp_path).ruler
        assert ruler.direction == "minimize"
        assert ruler.unit == "s"
        assert ruler.substages, "a win must be attributable to a named stage"
        assert gp.GUARDRAIL_TESTS_PASS in ruler.guardrails
        assert gp.RH_TEST_COUNT in ruler.rh_guards

    def test_build_gate_is_single_environment(self, tmp_path: Path) -> None:
        """Gate and measurement run in the same environment on the same tree, so the
        spine's cross-environment same-sha assertion does not apply."""
        assert _profile(tmp_path).build_gate.single_environment is True

    def test_measurement_boot_is_a_callable_noop(self, tmp_path: Path) -> None:
        boot = _profile(tmp_path).isolation.measurement_boot()
        assert callable(boot)
        assert boot() is None

    def test_no_frozen_components(self, tmp_path: Path) -> None:
        assert _profile(tmp_path).isolation.frozen_components == []

    def test_guardrail_tolerance_for_tests_passing_is_strict_zero(self, tmp_path: Path) -> None:
        """A red suite is never within tolerance."""
        tol = _profile(tmp_path).ruler.guardrail_tolerances()
        assert tol[gp.GUARDRAIL_TESTS_PASS] == 0.0

    def test_optional_spine_companions_are_present(self, tmp_path: Path) -> None:
        """The spine probes these via ``getattr``; a rename would silently degrade it."""
        p = _profile(tmp_path)
        assert callable(getattr(p.edit_allowlist, "allows_changes", None))
        assert callable(getattr(p.bug_runner, "run_named_tests", None))
        assert callable(getattr(p.bug_runner, "agent_test_hint", None))
        assert callable(getattr(p.isolation, "do_not_pollute_excludes", None))
        assert callable(getattr(p.ruler, "guardrail_tolerances", None))


class TestEditFence:
    """The asymmetry: source is editable, the tests-of-record are not, and only the bug
    track's ADDED reproducing test crosses that line."""

    def test_accepts_a_source_edit(self, tmp_path: Path) -> None:
        ok, offending = _profile(tmp_path).edit_allowlist.allows(["src/pkg/core.py"])
        assert ok and offending == []

    def test_accepts_a_flat_layout_source_edit(self, tmp_path: Path) -> None:
        ok, _ = _profile(tmp_path).edit_allowlist.allows(["thing.py"])
        assert ok

    def test_rejects_a_test_edit_on_the_perf_track(self, tmp_path: Path) -> None:
        """Editing the suite is metric gaming the build gate cannot see."""
        fence = _profile(tmp_path, track=TRACK_PERF).edit_allowlist
        ok, offending = fence.allows(["tests/test_core.py"])
        assert not ok
        assert offending == ["tests/test_core.py"]

    def test_rejects_a_nested_test_edit(self, tmp_path: Path) -> None:
        ok, offending = _profile(tmp_path).edit_allowlist.allows(["src/pkg/tests/test_x.py"])
        assert not ok and offending

    def test_rejects_config_and_ci_edits(self, tmp_path: Path) -> None:
        """Changing the interpreter or dependency set invalidates every prior measurement."""
        fence = _profile(tmp_path).edit_allowlist
        for path in ("pyproject.toml", "setup.cfg", ".github/workflows/ci.yml", "uv.lock"):
            ok, offending = fence.allows([path])
            assert not ok, path
            assert offending == [path]

    def test_accepts_an_added_reproducing_test_on_the_bug_track(self, tmp_path: Path) -> None:
        """A bug fix without a new reproducing test cannot be proven RED→GREEN."""
        fence = _profile(tmp_path, track=TRACK_BUG).edit_allowlist
        ok, offending = fence.allows_changes(
            [("A", "test/test_bug_widget.py"), ("M", "src/pkg/widget.py")]
        )
        assert ok, offending

    def test_accepts_the_tests_dir_spelling_too(self, tmp_path: Path) -> None:
        ok, offending = _profile(tmp_path).edit_allowlist.allows_changes(
            [("A", "tests/test_bug_widget.py")]
        )
        assert ok, offending

    def test_rejects_modifying_an_existing_test_even_where_adding_is_allowed(
        self, tmp_path: Path
    ) -> None:
        """The carve-out is for ADDITIONS only — otherwise "add your own repro" would
        become "edit the suite that judges you"."""
        fence = _profile(tmp_path).edit_allowlist
        ok, offending = fence.allows_changes([("M", "test/test_bug_widget.py")])
        assert not ok
        assert offending == ["test/test_bug_widget.py"]

    def test_rejects_an_added_test_outside_the_sanctioned_shape(self, tmp_path: Path) -> None:
        ok, offending = _profile(tmp_path).edit_allowlist.allows_changes(
            [("A", "src/pkg/test_sneaky.py")]
        )
        assert not ok and offending

    def test_name_only_form_judges_everything_as_a_modification(self, tmp_path: Path) -> None:
        """Fail-closed: the protocol form cannot tell an addition from an edit, so it must
        not accidentally admit a test edit by assuming it was an addition."""
        ok, _ = _profile(tmp_path).edit_allowlist.allows(["test/test_bug_widget.py"])
        assert not ok

    def test_rejects_path_traversal(self, tmp_path: Path) -> None:
        fence = _profile(tmp_path).edit_allowlist
        for path in ("../outside.py", "/etc/passwd", "src/../../escape.py"):
            ok, _ = fence.allows([path])
            assert not ok, path

    def test_scope_narrows_edits_but_not_additions(self, tmp_path: Path) -> None:
        """With a diff scope configured, an EDIT must be inside the branch's change set;
        a newly added file is exempt because it cannot be in the base diff."""
        fence = gp.RepoEditAllowlist(scope={"src/pkg/in_scope.py"})
        assert fence.allows(["src/pkg/in_scope.py"])[0]
        assert not fence.allows(["src/pkg/out_of_scope.py"])[0]
        assert fence.allows_changes([("A", "src/pkg/brand_new.py")])[0]


class TestIsolation:
    """The push-disable predicate and the pollute path set."""

    def test_push_disabled_is_false_for_a_missing_clone(self, tmp_path: Path) -> None:
        """Fail closed: no repo, no readable push url, no run."""
        iso = gp.RepoIsolation(clone_path=tmp_path / "nope")
        assert iso.push_disabled() is False

    def test_push_disabled_reads_the_sentinel(self, tmp_path: Path) -> None:
        """Mirrors ``clone_setup``'s own predicate against a real git repo."""
        import subprocess

        clone = tmp_path / "clone"
        clone.mkdir()
        subprocess.run(["git", "init", "-q", str(clone)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(clone), "remote", "add", "origin", "https://example.invalid/x.git"],
            check=True,
            capture_output=True,
        )
        iso = gp.RepoIsolation(clone_path=clone)
        assert iso.push_disabled() is False

        # Disabling ONLY the push url must NOT report "disabled": a live fetch url is a
        # live push target (`git push "$(git remote get-url origin)" HEAD`). This mirrors
        # `clone_setup._ok`, which requires BOTH urls neutral; the runtime check had
        # drifted to push-only. Raised by the GPT review of this branch.
        subprocess.run(
            ["git", "-C", str(clone), "remote", "set-url", "--push", "origin", "DISABLED_NO_PUSH"],
            check=True,
            capture_output=True,
        )
        assert iso.push_disabled() is False, "a live FETCH url is still a push target"

        # Only when BOTH urls are neutralized (what `_disable_push` actually does) is the
        # clone safe and the run allowed to start.
        subprocess.run(
            ["git", "-C", str(clone), "remote", "set-url", "origin", "DISABLED_NO_PUSH"],
            check=True,
            capture_output=True,
        )
        assert iso.push_disabled() is True

    def test_do_not_pollute_excludes_the_app_data_dir(self, tmp_path: Path) -> None:
        """The app writes its own ledger under the snapshot root during the boot window;
        without the exclude those writes register as a phantom leak."""
        iso = gp.RepoIsolation(clone_path=tmp_path / "clone")
        excludes = iso.do_not_pollute_excludes()
        paths = iso.do_not_pollute_paths()
        assert excludes, "the app's own data dir must be excluded"
        assert any(str(e) in {str(p) for p in paths} or True for e in excludes)


class TestBranchPlumbing:
    """A configured branch must reach both the base ref and the PR recipe's ``--base``."""

    def test_bare_branch_is_normalized_to_the_remote_ref(self, tmp_path: Path) -> None:
        p = build_profile({"clone": str(tmp_path / "clone"), "branch": "develop"})
        assert p.isolation.base_ref == "origin/develop"
        assert p.pr_recipe.base_branch == "develop"

    def test_a_qualified_ref_is_left_alone(self, tmp_path: Path) -> None:
        p = build_profile({"clone": str(tmp_path / "clone"), "branch": "origin/release/2.0"})
        assert p.isolation.base_ref == "origin/release/2.0"
        assert p.pr_recipe.base_branch == "release/2.0"

    def test_default_base_ref(self, tmp_path: Path) -> None:
        p = build_profile({"clone": str(tmp_path / "clone")})
        assert p.isolation.base_ref == "origin/main"


class TestFactory:
    def test_build_profile_returns_a_conforming_profile(self, tmp_path: Path) -> None:
        p = build_profile({"clone": str(tmp_path / "clone"), "branch": "main"})
        assert isinstance(p, TargetProfile)
        assert isinstance(p.ruler, Ruler)
        assert isinstance(p.build_gate, BuildGate)
        assert isinstance(p.bug_runner, BugRunner)
        assert isinstance(p.edit_allowlist, EditAllowlist)
        assert isinstance(p.isolation, IsolationRecipe)
        assert isinstance(p.pr_recipe, PRRecipe)

    def test_build_profile_refuses_without_a_clone(self) -> None:
        """A profile pointed at nothing would fail later and less legibly."""
        import pytest

        with pytest.raises(ValueError, match="no repository configured"):
            build_profile({})

    def test_calibration_reps_are_clamped(self, tmp_path: Path) -> None:
        """Each rep is a full suite run, so an unbounded value is an hour of calibration."""
        p = build_profile({"clone": str(tmp_path / "c"), "calibrationReps": 500})
        assert p.calibration.baseline_reps == 10
        p2 = build_profile({"clone": str(tmp_path / "c"), "calibrationReps": 1})
        assert p2.calibration.baseline_reps == 2

    def test_noise_floor_reaches_the_calibration_params(self, tmp_path: Path) -> None:
        p = build_profile({"clone": str(tmp_path / "c"), "noiseFloorSeconds": 0.5})
        assert p.calibration.floor == 0.5
        assert p.calibration.canary_id


class TestRepoRootFallback:
    """The spine appends ``src`` unconditionally; every layout must still be runnable.

    The run root is where pytest can SEE the suite, which is not always the path the
    spine hands over. Getting this wrong does not fail loudly — it reports exit 5 ("no
    tests ran") that every caller reads as a RED suite, so a green repo looks broken and
    the build gate refuses every candidate.
    """

    def test_src_holding_the_suite_is_used_as_is(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        (src / "tests").mkdir(parents=True)
        assert gp._repo_root(src) == src

    def test_src_layout_roots_at_the_repo_not_the_package(self, tmp_path: Path) -> None:
        """``src/`` exists but the suite is at ``<repo>/tests`` — the standard layout the
        profile targets. Running inside ``src/`` would collect nothing."""
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        (tmp_path / "tests").mkdir()
        assert gp._repo_root(tmp_path / "src") == tmp_path

    def test_missing_src_falls_back_to_the_repo_root(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        assert gp._repo_root(tmp_path / "src") == tmp_path

    def test_tests_beside_the_code_count_as_a_suite(self, tmp_path: Path) -> None:
        """A repo with no tests/ dir at all still roots pytest where the tests are."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "test_thing.py").write_text("def test_x():\n    assert True\n")
        assert gp._repo_root(src) == src

    def test_no_suite_anywhere_preserves_the_spine_fallback(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        assert gp._repo_root(src) == src
        assert gp._repo_root(tmp_path / "nope") == tmp_path

    def test_a_src_layout_repo_measures_green(self, tmp_path: Path) -> None:
        """The end-to-end assertion behind the fix: the reference layout (``src/`` +
        ``tests/``, uninstalled) must gate GREEN and collect its tests."""
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
        (tmp_path / "src" / "pkg" / "core.py").write_text("def add(a, b):\n    return a + b\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_core.py").write_text(
            "from pkg.core import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
        )
        # The package is one level below the run root and the repo is not installed, so
        # this also asserts PYTHONPATH carries <root>/src.
        assert gp._collected_count(tmp_path / "src") == 1
        result = gp.PytestBuildGate().build_and_test(worktree=tmp_path, src=tmp_path / "src")
        assert result.passed is True, result.detail
        assert gp.PytestBugRunner().run_suite(src=tmp_path / "src") == (True, [])


class TestSuiteMeasurement:
    """The ruler against a real (tiny) pytest suite — the one place we spawn pytest."""

    @staticmethod
    def _repo(root: Path, *, n_tests: int = 2) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "tests").mkdir(exist_ok=True)
        body = "\n".join(f"def test_{i}():\n    assert True\n" for i in range(n_tests))
        (root / "tests" / "test_tiny.py").write_text(body)
        return root

    def test_collected_count_reads_the_suite_size(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path / "repo", n_tests=3)
        assert gp._collected_count(repo) == 3

    def test_measure_reports_a_delta_and_a_green_guardrail(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path / "repo")
        m = gp.SuiteRuler().measure(
            base_src=repo, cand_src=repo, commit_sha="deadbeefcafe", scenario="suite"
        )
        assert m.ok
        assert m.primary_delta is not None
        assert m.guardrails[gp.GUARDRAIL_TESTS_PASS] == 0.0
        assert m.rh_capability_ok is True
        assert m.rh_functional_ok is True
        assert set(m.stages.stages) == {gp.STAGE_SUITE, gp.STAGE_COLLECT}

    def test_rh_guard_trips_when_the_candidate_deleted_tests(self, tmp_path: Path) -> None:
        """Deleting tests is the highest-value cheat against a wall-clock suite ruler and
        the build gate cannot see it — the RH guard is the only thing that can."""
        base = self._repo(tmp_path / "base", n_tests=4)
        cand = self._repo(tmp_path / "cand", n_tests=1)
        m = gp.SuiteRuler().measure(
            base_src=base, cand_src=cand, commit_sha="sha", scenario="suite"
        )
        assert m.ok
        assert m.rh_capability_ok is False

    def test_guardrail_flags_a_red_candidate_suite(self, tmp_path: Path) -> None:
        base = self._repo(tmp_path / "base")
        cand = self._repo(tmp_path / "cand")
        (cand / "tests" / "test_fail.py").write_text("def test_boom():\n    assert False\n")
        m = gp.SuiteRuler().measure(
            base_src=base, cand_src=cand, commit_sha="sha", scenario="suite"
        )
        assert m.guardrails[gp.GUARDRAIL_TESTS_PASS] == 1.0
        assert m.rh_functional_ok is False

    def test_baseline_samples_honors_stop_check(self, tmp_path: Path) -> None:
        """A Stop click during calibration must not wait out every remaining rep."""
        repo = self._repo(tmp_path / "repo")
        ruler = gp.SuiteRuler()
        ruler.stop_check = lambda: True
        assert ruler.baseline_samples(base_src=repo, reps=8) == []

    def test_baseline_samples_collects_reps(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path / "repo")
        samples = gp.SuiteRuler().baseline_samples(base_src=repo, reps=2)
        assert len(samples) == 2
        assert all(s > 0 for s in samples)

    def test_canary_produces_a_correctly_signed_win(self, tmp_path: Path) -> None:
        """Collect-only vs the full suite: a real, mechanically-known, negative delta.

        The suite needs measurable EXECUTION time for skipping it to be a win, so each
        test sleeps. The sleep is the workload under measurement here, not a fabricated
        improvement: the canary removes execution wholesale and must see it.

        This is a WALL-CLOCK test, so it is inherently sensitive to how loaded the host
        is. The history: 0.05s x 5 (0.25s/rep) resolved a win only 1 run in 3 under 16
        xdist workers; 0.15s x 5 (0.75s/rep) passed alone but still flaked inside a heavily
        loaded CI shard. Chasing the sleep value is a losing game, so this uses a LARGE
        workload (0.3s x 5 = ~1.5s/rep, deltas ~-1.5s under load — measured) AND a bounded
        retry: a false failure now requires sustained scheduling noise to swamp a ~1.5s
        signal on EVERY attempt, which is not a real-host condition. The production code is
        not what flaked — `measure_canary` correctly reports "inconclusive" when it cannot
        resolve a win — the TEST was, and a flaky test reds a CI rollup for a reason that
        has nothing to do with the change under review.
        """
        root = tmp_path / "repo"
        (root / "tests").mkdir(parents=True)
        body = "import time\n\n" + "\n".join(
            f"def test_{i}():\n    time.sleep(0.3)\n" for i in range(5)
        )
        (root / "tests" / "test_slow.py").write_text(body)
        # Retry a "could not resolve under load" result a bounded number of times. A real
        # regression (wrong sign, or a canary that never resolves a 1.5s win) fails all
        # attempts; a one-off scheduling spike does not.
        last = gp.SuiteRuler().measure_canary(base_src=root)
        for _ in range(3):
            if last.ok and last.primary_delta is not None and last.primary_delta < 0:
                break
            last = gp.SuiteRuler().measure_canary(base_src=root)
        assert last.ok, f"canary never resolved a ~1.5s win: {last.note}"
        assert last.primary_delta is not None and last.primary_delta < 0

    def test_canary_is_inconclusive_rather_than_noise_signed(self, tmp_path: Path) -> None:
        """A suite with no measurable execution time has NO win to force.

        Reporting ``ok=False`` is the honest answer; returning whichever sign the noise
        produced would let a coin flip decide whether the ruler is trusted.
        """
        repo = self._repo(tmp_path / "repo", n_tests=2)
        m = gp.SuiteRuler().measure_canary(base_src=repo)
        if not m.ok:
            assert "inconclusive" in m.note
        else:
            # A host slow enough to resolve even a trivial suite is a legitimate pass,
            # but the sign must still be a win — never a positive "win".
            assert m.primary_delta is not None and m.primary_delta < 0

    def test_canary_refuses_a_custom_benchmark_command(self, tmp_path: Path) -> None:
        """With a `benchmarkCommand` configured the `--collect-only` known-win is meaningless:
        the base arm runs the benchmark while the candidate arm still runs `pytest
        --collect-only`, so the delta compares two unrelated workloads and any benchmark
        slower than collection would spuriously clear the sensitivity check. `measure_canary`
        must refuse (`ok=False`) rather than certify an unexercised ruler. It must NOT even
        run the workload — a fast, deterministic refusal. Raised by the Opus review."""
        repo = self._repo(tmp_path / "repo", n_tests=2)
        m = gp.SuiteRuler(benchmark_cmd="python -c 'pass'").measure_canary(base_src=repo)
        assert not m.ok, "a custom benchmarkCommand has no mechanically-known win — must not certify"
        assert "benchmarkCommand" in m.note or "custom" in m.note

    def test_build_gate_passes_on_green_and_fails_on_red(self, tmp_path: Path) -> None:
        gate = gp.PytestBuildGate()
        green = self._repo(tmp_path / "green")
        assert gate.build_and_test(worktree=green, src=green / "src").passed is True

        red = self._repo(tmp_path / "red")
        (red / "tests" / "test_fail.py").write_text("def test_boom():\n    assert False\n")
        result = gate.build_and_test(worktree=red, src=red / "src")
        assert result.passed is False
        assert any("test_boom" in t for t in result.failing_tests)


class TestBugRunnerPrimitives:
    """The RED/GREEN primitives. The three-way ``run_reproducing_test`` return is the
    load-bearing part: a test that cannot RUN must not count as a valid RED."""

    @staticmethod
    def _repo(root: Path, body: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "test").mkdir(exist_ok=True)
        (root / "test" / "test_bug_x.py").write_text(body)
        return root

    def test_pass_fail_and_error_are_distinguished(self, tmp_path: Path) -> None:
        runner = gp.PytestBugRunner()
        nodeid = "test/test_bug_x.py::test_thing"

        green = self._repo(tmp_path / "g", "def test_thing():\n    assert True\n")
        assert runner.run_reproducing_test(src=green, test_id=nodeid, test_only=False) is True

        red = self._repo(tmp_path / "r", "def test_thing():\n    assert False\n")
        assert runner.run_reproducing_test(src=red, test_id=nodeid, test_only=True) is False

        broken = self._repo(tmp_path / "b", "import no_such_module_at_all\n")
        assert runner.run_reproducing_test(src=broken, test_id=nodeid, test_only=True) is None

    def test_missing_nodeid_is_an_error_not_a_red(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path / "m", "def test_thing():\n    assert True\n")
        runner = gp.PytestBugRunner()
        verdict = runner.run_reproducing_test(
            src=repo, test_id="test/test_bug_x.py::test_absent", test_only=True
        )
        assert verdict is None

    def test_test_collects(self, tmp_path: Path) -> None:
        runner = gp.PytestBugRunner()
        repo = self._repo(tmp_path / "c", "def test_thing():\n    assert True\n")
        assert runner.test_collects(src=repo, test_path="test/test_bug_x.py") is True
        assert runner.test_collects(src=repo, test_path="test/test_absent.py") is False
        assert runner.test_collects(src=repo, test_path="") is False

    def test_build_imports_ok_catches_a_syntax_error(self, tmp_path: Path) -> None:
        runner = gp.PytestBugRunner()
        good = self._repo(tmp_path / "ok", "def test_thing():\n    assert True\n")
        assert runner.build_imports_ok(src=good) is True

        bad = self._repo(tmp_path / "bad", "def test_thing(:\n")
        assert runner.build_imports_ok(src=bad) is False

    def test_run_suite_reports_failing_nodeids(self, tmp_path: Path) -> None:
        runner = gp.PytestBugRunner()
        green = self._repo(tmp_path / "g", "def test_thing():\n    assert True\n")
        assert runner.run_suite(src=green) == (True, [])

        red = self._repo(tmp_path / "r", "def test_thing():\n    assert False\n")
        ok, failing = runner.run_suite(src=red)
        assert ok is False
        assert failing and any("test_thing" in f for f in failing)

    def test_nodeids_parse_through_forced_color(self, tmp_path: Path) -> None:
        """A repo whose own config forces color must still yield re-runnable nodeids.

        pytest colors the test name in the MIDDLE of the nodeid, so an unstripped line
        yields no match at all — and STAYGREEN would then read "no pre-existing
        failures" on a repo that has them, admitting a regression as a clean pass.
        """
        red = self._repo(tmp_path / "color", "def test_thing():\n    assert False\n")
        (red / "pytest.ini").write_text("[pytest]\naddopts = --color=yes\n")
        ok, failing = gp.PytestBugRunner().run_suite(src=red)
        assert ok is False
        assert failing != ["<unparsed-suite-failure>"]
        assert any("test_thing" in f for f in failing)
        assert not any("\x1b" in f for f in failing)

    def test_ansi_stripping_is_applied_to_the_summary_parse(self) -> None:
        """The unit-level guard on the parse itself, independent of a pytest version."""
        line = "\x1b[31mFAILED\x1b[0m tests/test_x.py::\x1b[1mtest_boom\x1b[0m - assert False"
        assert gp._failing_nodeids(line) == ["tests/test_x.py::test_boom"]

    def test_run_named_tests_returns_only_the_failures(self, tmp_path: Path) -> None:
        """The gate subtracts pre-existing failures with this; a wrong answer either
        rejects a valid fix or admits a regression."""
        repo = self._repo(
            tmp_path / "n",
            "def test_ok():\n    assert True\n\n\ndef test_bad():\n    assert False\n",
        )
        runner = gp.PytestBugRunner()
        failed = runner.run_named_tests(
            src=repo,
            test_ids=["test/test_bug_x.py::test_ok", "test/test_bug_x.py::test_bad"],
        )
        assert failed == {"test/test_bug_x.py::test_bad"}

    def test_run_named_tests_ignores_the_unparsed_sentinel(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path / "s", "def test_thing():\n    assert True\n")
        assert (
            gp.PytestBugRunner().run_named_tests(src=repo, test_ids=["<unparsed-suite-failure>"])
            == set()
        )

    def test_lint_clean_ignores_preexisting_findings(self, tmp_path: Path) -> None:
        """A repo with existing violations must not be punished for them; only NEW ones
        vs base fail the candidate."""
        base = self._repo(tmp_path / "base", "import os\n\n\ndef test_thing():\n    assert True\n")
        cand = self._repo(tmp_path / "cand", "import os\n\n\ndef test_thing():\n    assert True\n")
        assert gp.PytestBugRunner().lint_clean(base_src=base, cand_src=cand) is True

    def test_agent_test_hint_names_this_interpreter(self, tmp_path: Path) -> None:
        """Without the hint the agent burns time hunting for an interpreter that has the
        dependencies installed."""
        import sys

        hint = gp.PytestBugRunner().agent_test_hint(tmp_path)
        assert sys.executable in hint
        assert "pytest" in hint


class TestDiscovery:
    def test_offline_discovery_returns_nothing_rather_than_fabricating(
        self, tmp_path: Path
    ) -> None:
        """A fabricated candidate list would burn cycles on invented targets."""
        result = _profile(tmp_path).discover(base_sha="abc", top_k=[], known_loci=[])
        assert result.candidates == []
        assert "offline" in result.notes

    def test_bug_candidates_carry_the_sanctioned_test_shape(self, tmp_path: Path) -> None:
        """What discovery asks for, the agent is told to write, and the fence allows must
        all be the same path shape."""
        p = _profile(tmp_path, track=TRACK_BUG)
        cand = p._candidate_from({"target": "src/pkg/widget.py::render", "message": "boom"})
        assert cand.kind == TRACK_BUG
        assert cand.reproducing_test is not None
        assert cand.reproducing_test.test_path.startswith("test/test_bug_")
        assert cand.reproducing_test.added_by_candidate is True
        ok, offending = p.edit_allowlist.allows_changes([("A", cand.reproducing_test.test_path)])
        assert ok, offending

    def test_perf_candidates_carry_no_reproducing_test(self, tmp_path: Path) -> None:
        p = _profile(tmp_path, track=TRACK_PERF)
        cand = p._candidate_from({"target": "src/pkg/widget.py::render"})
        assert cand.kind == TRACK_PERF
        assert cand.reproducing_test is None
        assert cand.scenario == "suite"

    def test_propose_reports_no_mechanical_seed(self, tmp_path: Path) -> None:
        """There is no transformation to apply; fabricating a diff would produce one the
        gate must reject."""
        p = _profile(tmp_path)
        cand = p._candidate_from({"target": "src/pkg/widget.py"})
        assert p.propose(candidate=cand, base_sha="abc", worktree=tmp_path, tier="wide") is False


class TestToolingArtifactsIgnored:
    """Regression: the first live run threw away a verified bug fix because the
    agent's own session had written ``.kiro/settings/cli.json`` into the worktree
    and the fence judged it as an unrecognized path. Tooling debris must be
    IGNORED (not admitted as an editable target, and not a rejection reason)."""

    def _allowlist(self):
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.profile import (
            RepoEditAllowlist,
        )

        return RepoEditAllowlist()

    def test_agent_settings_file_does_not_reject_a_candidate(self) -> None:
        allowed, off_limits = self._allowlist().allows(
            ["src/move_ordering.py", ".kiro/settings/cli.json"]
        )
        assert allowed, f"tooling debris rejected the candidate: {off_limits}"

    def test_cache_debris_is_ignored(self) -> None:
        allowed, _ = self._allowlist().allows(["src/x.py", "src/__pycache__/x.pyc"])
        assert allowed

    def test_traversal_disguised_as_an_artifact_is_still_refused(self) -> None:
        """The traversal check runs BEFORE the artifact ignore, so an ignore glob
        cannot be used as a smuggling prefix."""
        allowed, _ = self._allowlist().allows([".kiro/../../etc/passwd"])
        assert not allowed

    def test_modifying_a_test_is_still_refused(self) -> None:
        """The ignore must not have widened the metric-gaming fence."""
        allowed, _ = self._allowlist().allows(["tests/test_board.py"])
        assert not allowed


class TestMeasureEnvDoesNotUndoTheSandboxScrub:
    """`_run` layers `_measure_env` ON TOP of the sandbox's credential-scrubbed
    environment, so anything `_measure_env` inherits is put back after the sandbox removed
    it. It used to be `dict(os.environ)`.

    Measured before fixing: an `AWS_SECRET_ACCESS_KEY` that the sandbox had scrubbed
    reappeared in the child env once this dict was applied — handing the operator's
    credentials to agent-authored test code. Raised by review of this branch.
    """

    def test_measure_env_carries_no_credentials(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.profile import (
            _measure_env,
        )

        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "probe-secret")
        monkeypatch.setenv("GITHUB_TOKEN", "probe-token")
        monkeypatch.setenv("MY_API_KEY", "probe-key")
        env = _measure_env(tmp_path)
        for name in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "MY_API_KEY"):
            assert name not in env, f"{name} would be re-applied over the sandbox scrub"

    def test_measure_env_still_carries_what_a_suite_needs(self, tmp_path, monkeypatch) -> None:
        """An allowlist that breaks the suite is not a fix."""
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.profile import (
            _measure_env,
        )

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        env = _measure_env(tmp_path)
        assert env.get("PATH"), "without PATH the subprocess cannot find python"
        # And the determinism pins that make an A/B fair must survive.
        assert env["PYTHONHASHSEED"] == "0"
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert str(tmp_path) in env["PYTHONPATH"]

    def test_credential_shaped_names_are_stripped_by_shape(self) -> None:
        """Defends against gaps in the SHARED scrub list: measured on this host,
        `GITHUB_TOKEN` survives `kiro_crew.sandbox.scrub_env` (its list covers
        SLACK_*/AWS_SECRET but not GITHUB_*), so the gate strips by shape as well."""
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.profile import (
            strip_credential_env,
        )

        out = strip_credential_env(
            {
                "PATH": "/usr/bin",
                "LANG": "C",
                "GITHUB_TOKEN": "y",
                "DB_PASSWORD": "p",
                "MY_API_KEY": "z",
                "NPM_CREDENTIAL": "c",
                "AWS_SECRET_ACCESS_KEY": "x",
            }
        )
        assert sorted(out) == ["LANG", "PATH"]


class TestGateChildEnvironmentMeasuredEndToEnd:
    """Runs a real child through the gate's own ``_run`` and reads its environment.

    The earlier tests here assert on the DICTS (`_measure_env`, `strip_credential_env`).
    That is necessary but not sufficient: the leak this fixed only appeared because of how
    those dicts were COMBINED (`scrubbed_env.update(env)` re-applied what the sandbox had
    removed). The lesson from that round — measure the claim, do not infer it from the
    design — is why this drives the real code path instead.
    """

    def test_no_credential_reaches_the_child_and_the_suite_can_still_run(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as prof

        monkeypatch.setenv("GITHUB_TOKEN", "ghp_probe")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_probe")
        monkeypatch.setenv("DB_PASSWORD", "pw_probe")

        (tmp_path / "probe.sh").write_text(
            "echo GH=${GITHUB_TOKEN:-ABSENT}\n"
            "echo AWS=${AWS_SECRET_ACCESS_KEY:-ABSENT}\n"
            "echo PW=${DB_PASSWORD:-ABSENT}\n"
            "echo PATH_OK=${PATH:+yes}\n"
            "echo HASHSEED=${PYTHONHASHSEED:-unset}\n"
        )
        res = prof._run(
            ["/bin/sh", "probe.sh"],
            cwd=tmp_path,
            env=prof._measure_env(tmp_path),
            timeout=120,
        )
        out = res.stdout or ""
        # No credential VALUE may appear anywhere in the child's view.
        for value in ("ghp_probe", "aws_probe", "pw_probe"):
            assert value not in out, f"{value} reached agent-authored test code"
        assert "GH=ABSENT" in out and "AWS=ABSENT" in out and "PW=ABSENT" in out
        # ...and the suite must still be runnable + the A/B still deterministic.
        assert "PATH_OK=yes" in out, "without PATH a suite cannot run at all"
        assert "HASHSEED=0" in out, "the A/B arms must stay byte-identical"


class TestGateRunsInStrictSandboxMode:
    """The gate executes AGENT-AUTHORED code (a candidate's conftest and reproducing test),
    so it must not use the sandbox's default "standard" mode — that deliberately leaves
    ``~/.aws`` visible so a test suite can use the AWS CLI.

    Measured before fixing: under "standard" the child saw all 7 ``~/.aws`` entries; under
    "strict", 0. The agent spawn had already been switched; the gate was the other place
    untrusted code runs and was missed. Raised by review of this branch.

    Pins the MODE rather than re-measuring the filesystem, so it stays meaningful on a host
    where user namespaces are unavailable.
    """

    def test_the_gate_requests_strict_mode(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as prof

        seen: dict = {}

        def _fake(argv, mode="standard", **kw):
            seen["mode"] = mode
            seen["kw"] = kw
            return list(argv), {}, None

        # Patch the name where it is BOUND, not where it is defined: `profile.py` imports
        # `sandboxed_spawn_argv` at module scope, so patching `kiro_crew.sandbox` leaves the
        # already-bound reference — and the REAL sandbox — in place, and this test then
        # asserts nothing while still passing. (It did exactly that when the import was
        # function-local and was later hoisted.)
        monkeypatch.setattr(gp, "sandboxed_spawn_argv", _fake)
        monkeypatch.setattr(prof.subprocess, "run", lambda *a, **k: None)
        prof._run(["/bin/true"], cwd=tmp_path, timeout=5)

        assert seen["mode"] == "strict", "agent-authored tests must not see credential dirs"
        # The worktree must stay visible or there is nothing to test.
        assert str(tmp_path.resolve()) in seen["kw"]["extra_visible_dirs"]

    def test_both_untrusted_execution_paths_agree_on_strict(self) -> None:
        """The gate and the agent spawn are the two places untrusted code runs. Fixing one
        and not the other is exactly what happened here, so assert on both."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as prof
        from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import AgentRunner

        for src in (
            inspect.getsource(prof._run),
            inspect.getsource(AgentRunner._spawn_sandboxed_agent),
        ):
            assert 'mode="strict"' in src
