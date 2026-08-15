"""Coverage for the auto-improvement ``github_repo`` Target Profile.

Everything here is fabricated: no real repository, no ``gh`` CLI, no network, and no
subprocess. The module funnels every external command through one private helper
(``profile._run``), so replacing that helper with a scripted fake exercises the whole
adapter surface -- the ruler, the build gate, the RED/GREEN bug runner, the edit
fence, the isolation recipe, the assembled profile, and the config factory -- while
the process only ever touches ``tmp_path``.

The seams that are deliberately stubbed rather than driven, and why:

* ``profile._run`` -- the sandboxed subprocess chokepoint. Scripted per test so exit
  codes, stdout shapes, timeouts and spawn errors are all reachable.
* ``SuiteRuler._time_once`` -- stubbed for :meth:`measure`, :meth:`baseline_samples`
  and :meth:`measure_canary` because those aggregate WALL-CLOCK values, and a fake
  subprocess returns instantly, so the sign of a real delta would be decided by
  scheduler noise. ``_time_once`` itself is driven through the fake ``_run``.
* ``store.*`` directory helpers -- redirected into ``tmp_path`` so nothing is written
  to the operator's Kiro Crew data home.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
from kiro_crew.apps.builtins.auto_improvement.backend import profile_normalize as PN
from kiro_crew.apps.builtins.auto_improvement.backend import store
from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as gh
from kiro_crew.apps.builtins.auto_improvement.spine.contracts import TRACK_BUG, TRACK_PERF

# ── scaffolding ──────────────────────────────────────────────────────────────


class _Proc:
    """The shape ``profile._run`` returns: a ``CompletedProcess`` read as data."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeRun:
    """A scripted stand-in for ``profile._run`` that records every invocation.

    ``results`` is consumed in order; an ``Exception`` INSTANCE in the script is
    raised instead of returned, which is how the timeout / spawn-error branches are
    reached. The last entry repeats once exhausted so a test only has to script the
    calls it cares about.
    """

    def __init__(self, *results: object) -> None:
        self.results: list[object] = list(results) or [_Proc()]
        self.calls: list[dict[str, object]] = []

    def __call__(self, argv, *, cwd, timeout, env=None):  # noqa: ANN001 - test double
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout, "env": env})
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, BaseException):
            raise result
        return result

    @property
    def argvs(self) -> list[list[str]]:
        return [list(call["argv"]) for call in self.calls]  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Every store path lands in ``tmp_path``; nothing touches the real data home."""
    home = tmp_path / "kirohome"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    data = home / "data"
    data.mkdir(parents=True, exist_ok=True)
    for name in ("data_dir", "workspace_dir"):
        monkeypatch.setattr(store, name, lambda _d=data: _d, raising=True)
    for name in ("pr_queue_dir", "logs_dir", "profiles_dir", "results_dir", "ruler_dir"):
        sub = data / name
        sub.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(store, name, lambda _s=sub: _s, raising=True)
    return home


def _repo(tmp_path: Path, *, name: str = "repo", tests: str | None = "tests") -> Path:
    """A fabricated repo tree: optional test dir plus one source file."""
    root = tmp_path / name
    (root / "src" / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "src" / "pkg" / "mod.py").write_text("x = 1\n", newline="\n")
    if tests:
        (root / tests).mkdir(parents=True, exist_ok=True)
        (root / tests / "test_a.py").write_text("def test_a():\n    pass\n", newline="\n")
    return root


# ── _has_tests / _repo_root ──────────────────────────────────────────────────


@pytest.mark.parametrize("dirname", ["tests", "test"])
def test_has_tests_recognizes_both_suite_dirs(tmp_path, dirname):
    root = tmp_path / dirname[:1] / "r"
    (root / dirname).mkdir(parents=True)
    assert gh._has_tests(root) is True


@pytest.mark.parametrize("pattern", ["test_thing.py", "thing_test.py"])
def test_has_tests_recognizes_tests_beside_the_code(tmp_path, pattern):
    root = tmp_path / pattern.replace(".", "_")
    root.mkdir()
    (root / pattern).write_text("", newline="\n")
    assert gh._has_tests(root) is True


def test_has_tests_false_for_a_bare_tree(tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    (root / "mod.py").write_text("x = 1\n", newline="\n")
    assert gh._has_tests(root) is False


def test_repo_root_returns_the_tree_when_it_holds_the_suite(tmp_path):
    root = _repo(tmp_path, tests="tests")
    assert gh._repo_root(root) == root


def test_repo_root_falls_back_to_parent_for_a_flat_repo(tmp_path):
    """The spine appends ``src`` unconditionally; a flat repo has no such dir."""
    root = _repo(tmp_path, name="flat", tests="test")
    missing = root / "nosrc"
    assert gh._repo_root(missing) == root


def test_repo_root_prefers_the_parent_in_a_src_layout(tmp_path):
    """``<repo>/src`` exists but the suite is at ``<repo>/tests`` -- run at the root."""
    root = _repo(tmp_path, name="srclayout", tests="tests")
    assert gh._repo_root(root / "src") == root


def test_repo_root_keeps_an_existing_tree_when_neither_side_has_tests(tmp_path):
    root = _repo(tmp_path, name="notests", tests=None)
    assert gh._repo_root(root / "src") == root / "src"


def test_repo_root_returns_parent_when_only_the_parent_exists(tmp_path):
    root = _repo(tmp_path, name="ghost", tests=None)
    assert gh._repo_root(root / "src" / "absent") == root / "src"


def test_repo_root_returns_the_tree_when_nothing_exists(tmp_path):
    missing = tmp_path / "gone" / "deeper"
    assert gh._repo_root(missing) == missing


# ── _write_protected_targets ─────────────────────────────────────────────────


def test_write_protected_targets_masks_existing_parent_dirs(tmp_path, monkeypatch):
    """PARENT directories are returned, and only ones that exist."""
    fake_home = tmp_path / "h"
    (fake_home / ".kiro" / "crew").mkdir(parents=True)
    monkeypatch.setattr(gh.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(
        security,
        "write_protected_home_paths",
        lambda: (".kiro/crew/config.json", ".absent/dir/file.json"),
    )
    out = gh._write_protected_targets()
    assert out == (str(fake_home / ".kiro" / "crew"),)


def test_write_protected_targets_fails_soft(monkeypatch):
    """Masking is defense in depth -- an unavailable helper must not refuse the run."""

    def _boom():
        raise RuntimeError("no platform list")

    monkeypatch.setattr(security, "write_protected_home_paths", _boom)
    assert gh._write_protected_targets() == ()


# ── _measure_env ─────────────────────────────────────────────────────────────


def test_measure_env_pins_determinism_and_drops_credentials(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="envrepo")
    monkeypatch.setattr(
        os,
        "environ",
        {
            "PATH": "/usr/bin",
            "HOME": str(tmp_path),
            "AWS_SECRET_ACCESS_KEY": "shhh",
            "GITHUB_TOKEN": "ghp_x",
            "NOT_ALLOWED": "1",
        },
    )
    env = gh._measure_env(root)
    assert env["PYTHONHASHSEED"] == "0"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTEST_ADDOPTS"] == ""
    assert env["PATH"] == "/usr/bin"
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "NOT_ALLOWED" not in env


def test_measure_env_puts_both_run_root_and_src_on_the_path(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="pathrepo")
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin"})
    parts = gh._measure_env(root)["PYTHONPATH"].split(os.pathsep)
    assert parts == [str(root), str(root / "src")]


def test_measure_env_omits_a_missing_src_dir(tmp_path, monkeypatch):
    root = tmp_path / "flatonly"
    (root / "tests").mkdir(parents=True)
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin"})
    assert gh._measure_env(root)["PYTHONPATH"] == str(root)


# ── xdist probing ────────────────────────────────────────────────────────────


def test_xdist_argv_present(monkeypatch):
    monkeypatch.setitem(sys.modules, "xdist", types.ModuleType("xdist"))
    assert gh._xdist_argv() == ("-n", "auto")


def test_xdist_argv_absent_runs_serially(monkeypatch):
    """``None`` in ``sys.modules`` makes ``import xdist`` raise, as an absent plugin does."""
    monkeypatch.setitem(sys.modules, "xdist", None)
    assert gh._xdist_argv() == ()


@pytest.mark.parametrize(
    "blob",
    [
        "unrecognized arguments: -n",
        "error: unrecognized arguments",
        "Different tests were collected",
        "node down",
        "worker crashed",
        "Replacing crashed worker",
    ],
)
def test_looks_like_xdist_failure_matches_every_marker(blob):
    assert gh._looks_like_xdist_failure(blob, "") is True
    assert gh._looks_like_xdist_failure("", blob) is True


def test_looks_like_xdist_failure_false_for_a_test_failure():
    assert gh._looks_like_xdist_failure("1 failed, 2 passed", "") is False


# ── _suite_scope_for_globs ───────────────────────────────────────────────────


def test_suite_scope_empty_without_a_narrowed_allowlist(tmp_path):
    assert gh._suite_scope_for_globs(tmp_path, None) == []
    assert gh._suite_scope_for_globs(tmp_path, []) == []


def test_suite_scope_finds_the_test_dir_at_the_edit_region(tmp_path):
    clone = tmp_path / "mono"
    (clone / "apps" / "one" / "tests").mkdir(parents=True)
    got = gh._suite_scope_for_globs(clone, ["apps/one/**/*.py"])
    assert got == [str(Path("apps") / "one" / "tests")]


def test_suite_scope_walks_up_to_the_nearest_enclosing_test_dir(tmp_path):
    clone = tmp_path / "walkup"
    (clone / "apps" / "one" / "backend").mkdir(parents=True)
    (clone / "apps" / "test").mkdir(parents=True)
    got = gh._suite_scope_for_globs(clone, ["apps/one/backend/*.py"])
    assert got == [str(Path("apps") / "test")]


def test_suite_scope_falls_back_to_the_repo_root_test_dir(tmp_path):
    clone = tmp_path / "roottests"
    (clone / "src" / "pkg").mkdir(parents=True)
    (clone / "tests").mkdir(parents=True)
    assert gh._suite_scope_for_globs(clone, ["src/pkg/*.py"]) == ["tests"]


def test_suite_scope_drops_a_trailing_filename_fragment(tmp_path):
    clone = tmp_path / "fragment"
    (clone / "src" / "test").mkdir(parents=True)
    assert gh._suite_scope_for_globs(clone, ["src/mod_*.py"]) == [str(Path("src") / "test")]


def test_suite_scope_empty_when_globs_name_no_directory(tmp_path):
    assert gh._suite_scope_for_globs(tmp_path, ["*.py", "./*.py"]) == []


def test_suite_scope_with_two_globs_over_the_same_directory(tmp_path):
    """Identical edit dirs exhaust the prefix walk without ever diverging."""
    clone = tmp_path / "sameancestor"
    (clone / "apps" / "one" / "tests").mkdir(parents=True)
    got = gh._suite_scope_for_globs(clone, ["apps/one/*.py", "apps/one/**/*.py"])
    assert got == [str(Path("apps") / "one" / "tests")]


def test_suite_scope_uses_a_partial_common_ancestor(tmp_path):
    """Two sibling edit dirs share only their parent -- that is where the walk starts."""
    clone = tmp_path / "partial"
    (clone / "apps" / "one").mkdir(parents=True)
    (clone / "apps" / "two").mkdir(parents=True)
    (clone / "apps" / "tests").mkdir(parents=True)
    got = gh._suite_scope_for_globs(clone, ["apps/one/*.py", "apps/two/*.py"])
    assert got == [str(Path("apps") / "tests")]


def test_suite_scope_empty_when_globs_share_no_ancestor(tmp_path):
    clone = tmp_path / "disjoint"
    (clone / "alpha").mkdir(parents=True)
    (clone / "beta").mkdir(parents=True)
    (clone / "tests").mkdir(parents=True)
    assert gh._suite_scope_for_globs(clone, ["alpha/*.py", "beta/*.py"]) == []


def test_suite_scope_empty_when_the_repo_has_no_test_dir_at_all(tmp_path):
    clone = tmp_path / "suiteless"
    (clone / "src" / "pkg").mkdir(parents=True)
    assert gh._suite_scope_for_globs(clone, ["src/pkg/*.py"]) == []


# ── _pytest_argv / _time_suite / _collected_count / _failing_nodeids ─────────


def test_pytest_argv_pins_the_running_interpreter_and_overrides_repo_addopts():
    argv = gh._pytest_argv("-q", "--collect-only")
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "pytest"]
    assert "no:cacheprovider" in argv
    assert "--color=no" in argv
    assert argv[argv.index("-o") + 1] == "addopts="
    assert argv[-2:] == ["-q", "--collect-only"]


def test_time_suite_reports_pass_and_a_positive_duration(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="timed")
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(0)))
    seconds, passed = gh._time_suite(root)
    assert passed is True
    assert seconds >= 0.0


def test_time_suite_treats_an_empty_suite_as_not_passed(tmp_path, monkeypatch):
    """pytest exit 5 = nothing collected, which provides no correctness signal."""
    root = _repo(tmp_path, name="empty")
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(5)))
    _seconds, passed = gh._time_suite(root)
    assert passed is False


def test_time_suite_returns_nan_on_a_spawn_failure(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="nanrepo")
    monkeypatch.setattr(gh, "_run", _FakeRun(subprocess.TimeoutExpired("pytest", 1.0)))
    seconds, passed = gh._time_suite(root)
    assert seconds != seconds  # NaN
    assert passed is False


def test_time_suite_forwards_extra_flags(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="extraflags")
    fake = _FakeRun(_Proc(0))
    monkeypatch.setattr(gh, "_run", fake)
    gh._time_suite(root, extra=("--collect-only",))
    assert "--collect-only" in fake.argvs[0]


def test_collected_count_reads_the_summary_line(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="counted")
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(0, "42 tests collected in 0.12s\n")))
    assert gh._collected_count(root) == 42


def test_collected_count_falls_back_to_counting_nodeids(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="nodeids")
    out = "tests/test_a.py::test_one\ntests/test_a.py::test_two\nno colons here\n"
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(0, out)))
    assert gh._collected_count(root) == 2


def test_collected_count_unreadable_is_minus_one(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="unreadable")
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(4, "internal error\n")))
    assert gh._collected_count(root) == -1


def test_collected_count_spawn_failure_is_minus_one(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="countboom")
    monkeypatch.setattr(gh, "_run", _FakeRun(OSError("no exec")))
    assert gh._collected_count(root) == -1


def test_failing_nodeids_parses_failed_and_error_lines():
    out = "FAILED tests/test_a.py::test_one - assert 1 == 2\nERROR tests/test_b.py\nok\n"
    assert gh._failing_nodeids(out) == ["tests/test_a.py::test_one", "tests/test_b.py"]


def test_failing_nodeids_strips_colour_escapes():
    """A nodeid carrying SGR sequences is not a nodeid the gate can re-run."""
    coloured = "FAILED tests/test_a.py::\x1b[1mtest_one\x1b[0m - boom"
    assert gh._failing_nodeids(coloured) == ["tests/test_a.py::test_one"]


def test_failing_nodeids_empty_for_a_green_summary():
    assert gh._failing_nodeids("3 passed in 0.4s") == []
    assert gh._failing_nodeids("") == []


# ── ① SuiteRuler ─────────────────────────────────────────────────────────────


def test_ruler_records_its_measurement_constants():
    ruler = gh.SuiteRuler()
    assert ruler.direction == "minimize"
    assert ruler.unit == gh.UNIT_SECONDS
    assert ruler.measurement_constants["runner"] == "python -m pytest -q"
    assert ruler.measurement_constants["PYTHONHASHSEED"] == "0"
    assert gh.SuiteRuler(benchmark_cmd="  make bench  ").benchmark_cmd == "make bench"


def test_ruler_time_once_runs_a_custom_benchmark_command(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="bench")
    fake = _FakeRun(_Proc(0))
    monkeypatch.setattr(gh, "_run", fake)
    seconds, passed = gh.SuiteRuler(benchmark_cmd="make bench")._time_once(root)
    assert passed is True
    assert seconds >= 0.0
    assert fake.argvs[0] == ["make", "bench"]


def test_ruler_time_once_nan_when_the_benchmark_cannot_run(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="benchboom")
    monkeypatch.setattr(gh, "_run", _FakeRun(OSError("no such command")))
    seconds, passed = gh.SuiteRuler(benchmark_cmd="make bench")._time_once(root)
    assert seconds != seconds
    assert passed is False


def test_ruler_time_once_collect_only_ignores_the_benchmark_command(tmp_path, monkeypatch):
    """The collect-only arm is always pytest -- that is what the canary compares."""
    root = _repo(tmp_path, name="benchcollect")
    fake = _FakeRun(_Proc(0))
    monkeypatch.setattr(gh, "_run", fake)
    gh.SuiteRuler(benchmark_cmd="make bench")._time_once(root, collect_only=True)
    assert "--collect-only" in fake.argvs[0]
    assert fake.argvs[0][0] == sys.executable


def _stub_time_once(monkeypatch, ruler, mapping):
    """Pin ``_time_once`` per arm: ``{(tree, collect_only): (seconds, passed)}``."""

    def _fake(tree, *, collect_only=False):
        return mapping[(Path(tree), collect_only)]

    monkeypatch.setattr(ruler, "_time_once", _fake)


def test_ruler_measure_returns_a_signed_delta_and_stage_breakdown(tmp_path, monkeypatch):
    base, cand = tmp_path / "b", tmp_path / "c"
    ruler = gh.SuiteRuler()
    _stub_time_once(
        monkeypatch,
        ruler,
        {
            (base, False): (10.0, True),
            (base, True): (2.0, True),
            (cand, False): (7.0, True),
            (cand, True): (2.0, True),
        },
    )
    monkeypatch.setattr(gh, "_collected_count", lambda tree: 100)
    m = ruler.measure(base_src=base, cand_src=cand, commit_sha="deadbeefcafe", scenario="")
    assert m.ok is True
    assert m.primary_delta == pytest.approx(-3.0)
    assert m.primary_base == pytest.approx(10.0)
    assert m.primary_cand == pytest.approx(7.0)
    assert m.stages.stages[gh.STAGE_SUITE] == pytest.approx(-3.0)
    assert m.stages.stages[gh.STAGE_COLLECT] == pytest.approx(0.0)
    assert m.guardrails[gh.GUARDRAIL_TESTS_PASS] == 0.0
    assert m.rh_capability_ok is True
    assert m.rh_functional_ok is True
    assert m.secondary["base_tests_collected"] == 100.0
    assert m.note == "sha=deadbeefca scenario=suite tests=100->100"


def test_ruler_measure_flags_a_red_candidate_suite(tmp_path, monkeypatch):
    base, cand = tmp_path / "b2", tmp_path / "c2"
    ruler = gh.SuiteRuler()
    _stub_time_once(
        monkeypatch,
        ruler,
        {
            (base, False): (10.0, True),
            (base, True): (2.0, True),
            (cand, False): (5.0, False),
            (cand, True): (2.0, True),
        },
    )
    monkeypatch.setattr(gh, "_collected_count", lambda tree: 10)
    m = ruler.measure(base_src=base, cand_src=cand, commit_sha="abcdef1234", scenario="suite")
    assert m.guardrails[gh.GUARDRAIL_TESTS_PASS] == 1.0
    assert m.rh_functional_ok is False


def test_ruler_measure_not_ok_when_a_workload_did_not_complete(tmp_path, monkeypatch):
    base, cand = tmp_path / "b3", tmp_path / "c3"
    ruler = gh.SuiteRuler()
    _stub_time_once(
        monkeypatch,
        ruler,
        {
            (base, False): (10.0, True),
            (base, True): (2.0, True),
            (cand, False): (float("nan"), False),
            (cand, True): (2.0, True),
        },
    )
    m = ruler.measure(base_src=base, cand_src=cand, commit_sha="x" * 12, scenario="")
    assert m.ok is False
    assert "did not complete" in m.note


@pytest.mark.parametrize(
    ("base_n", "cand_n", "expected"),
    [
        (100, 100, True),
        (100, 101, True),
        (100, 99, False),  # deleted tests: the top cheat against a wall-clock ruler
        (-1, 100, False),  # unverifiable is indistinguishable from defeated
        (100, -1, False),
    ],
)
def test_ruler_measure_rh_guard_on_collected_test_counts(
    tmp_path, monkeypatch, base_n, cand_n, expected
):
    base, cand = tmp_path / f"b{base_n}{cand_n}", tmp_path / f"c{base_n}{cand_n}"
    ruler = gh.SuiteRuler()
    _stub_time_once(
        monkeypatch,
        ruler,
        {
            (base, False): (10.0, True),
            (base, True): (2.0, True),
            (cand, False): (7.0, True),
            (cand, True): (2.0, True),
        },
    )
    counts = {base: base_n, cand: cand_n}
    monkeypatch.setattr(gh, "_collected_count", lambda tree: counts[Path(tree)])
    m = ruler.measure(base_src=base, cand_src=cand, commit_sha="0123456789", scenario="")
    assert m.rh_capability_ok is expected


def test_ruler_baseline_samples_records_the_median(tmp_path, monkeypatch):
    base = tmp_path / "cal"
    ruler = gh.SuiteRuler()
    samples = iter([3.0, 1.0, 2.0])
    monkeypatch.setattr(ruler, "_time_once", lambda tree, **kw: (next(samples), True))
    out = ruler.baseline_samples(base_src=base, reps=3)
    assert out == [3.0, 1.0, 2.0]
    assert ruler.guardrail_baselines()["suite_wall_seconds"] == pytest.approx(2.0)


def test_ruler_baseline_samples_clamps_reps_to_at_least_two(tmp_path, monkeypatch):
    ruler = gh.SuiteRuler()
    calls: list[int] = []

    def _fake(tree, **kw):
        calls.append(1)
        return 1.0, True

    monkeypatch.setattr(ruler, "_time_once", _fake)
    assert ruler.baseline_samples(base_src=tmp_path / "clamp", reps=0) == [1.0, 1.0]
    assert len(calls) == 2


def test_ruler_baseline_samples_skips_a_failed_rep(tmp_path, monkeypatch):
    ruler = gh.SuiteRuler()
    samples = iter([float("nan"), 4.0])
    monkeypatch.setattr(ruler, "_time_once", lambda tree, **kw: (next(samples), True))
    assert ruler.baseline_samples(base_src=tmp_path / "skip", reps=2) == [4.0]


def test_ruler_baseline_samples_honours_a_stop_click(tmp_path, monkeypatch):
    """A Stop during a 10-rep calibration must take effect between reps."""
    ruler = gh.SuiteRuler()
    ruler.stop_check = lambda: True
    monkeypatch.setattr(ruler, "_time_once", lambda tree, **kw: (1.0, True))
    assert ruler.baseline_samples(base_src=tmp_path / "stopped", reps=10) == []
    assert ruler.guardrail_baselines()["suite_wall_seconds"] == 0.0


def test_ruler_canary_refuses_to_certify_a_custom_benchmark_command(tmp_path):
    m = gh.SuiteRuler(benchmark_cmd="make bench").measure_canary(base_src=tmp_path)
    assert m.ok is False
    assert "mechanically-known win" in m.note


def test_ruler_canary_clears_when_skipping_execution_wins(tmp_path, monkeypatch):
    base = tmp_path / "canary"
    ruler = gh.SuiteRuler()
    monkeypatch.setattr(
        ruler,
        "_time_once",
        lambda tree, *, collect_only=False: (0.5, True) if collect_only else (9.0, True),
    )
    m = ruler.measure_canary(base_src=base)
    assert m.ok is True
    assert m.primary_delta == pytest.approx(-8.5)
    assert m.primary_base == pytest.approx(9.0)
    assert m.stages.stages[gh.STAGE_SUITE] == pytest.approx(-8.5)
    assert m.guardrails[gh.GUARDRAIL_TESTS_PASS] == 0.0
    assert f"{gh._CANARY_REPS} reps" in m.note


def test_ruler_canary_carries_a_red_base_suite_into_the_guardrail(tmp_path, monkeypatch):
    ruler = gh.SuiteRuler()
    monkeypatch.setattr(
        ruler,
        "_time_once",
        lambda tree, *, collect_only=False: (0.5, True) if collect_only else (9.0, False),
    )
    m = ruler.measure_canary(base_src=tmp_path / "redbase")
    assert m.ok is True
    assert m.guardrails[gh.GUARDRAIL_TESTS_PASS] == 1.0


def test_ruler_canary_inconclusive_when_the_suite_is_too_fast(tmp_path, monkeypatch):
    """No win exists to force -- say so rather than return a noise-signed delta."""
    ruler = gh.SuiteRuler()
    monkeypatch.setattr(ruler, "_time_once", lambda tree, **kw: (1.0, True))
    m = ruler.measure_canary(base_src=tmp_path / "tooquick")
    assert m.ok is False
    assert "canary inconclusive" in m.note
    assert m.primary_delta == pytest.approx(0.0)


def test_ruler_canary_not_ok_when_a_rep_did_not_complete(tmp_path, monkeypatch):
    ruler = gh.SuiteRuler()
    monkeypatch.setattr(
        ruler,
        "_time_once",
        lambda tree, *, collect_only=False: (float("nan"), False) if collect_only else (9.0, True),
    )
    m = ruler.measure_canary(base_src=tmp_path / "canaryboom")
    assert m.ok is False
    assert m.note == "canary workload did not complete"


def test_ruler_guardrail_tolerance_for_a_red_suite_is_strictly_zero():
    assert gh.SuiteRuler().guardrail_tolerances() == {gh.GUARDRAIL_TESTS_PASS: 0.0}


# ── ② PytestBuildGate ────────────────────────────────────────────────────────


def test_build_gate_green(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="gategreen")
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(0, "5 passed")))
    res = gh.PytestBuildGate().build_and_test(worktree=root, src=root / "src")
    assert res.passed is True
    assert res.detail == "suite green"


def test_build_gate_red_reports_the_failing_nodeids(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="gatered")
    out = "FAILED tests/test_a.py::test_one - boom\n1 failed, 4 passed in 0.3s\n"
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(1, out)))
    res = gh.PytestBuildGate().build_and_test(worktree=root, src=root / "src")
    assert res.passed is False
    assert res.failing_tests == ["tests/test_a.py::test_one"]
    assert res.detail.startswith("suite red (exit 1)")


def test_build_gate_timeout(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="gatetimeout")
    monkeypatch.setattr(gh, "_run", _FakeRun(subprocess.TimeoutExpired("pytest", 900.0)))
    res = gh.PytestBuildGate().build_and_test(worktree=root, src=root / "src")
    assert res.passed is False
    assert "timed out" in res.detail


def test_build_gate_spawn_error(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="gatespawn")
    monkeypatch.setattr(gh, "_run", _FakeRun(OSError("fork failed")))
    res = gh.PytestBuildGate().build_and_test(worktree=root, src=root / "src")
    assert res.passed is False
    assert "could not run the suite" in res.detail


def test_build_gate_retries_serially_when_xdist_itself_failed(tmp_path, monkeypatch):
    """A broken plugin must read as 'run serially', never as 'suite red'."""
    root = _repo(tmp_path, name="gatexdist")
    monkeypatch.setattr(gh, "_XDIST_ARGV", ("-n", "auto"))
    fake = _FakeRun(_Proc(4, "", "error: unrecognized arguments: -n"), _Proc(0, "5 passed"))
    monkeypatch.setattr(gh, "_run", fake)
    res = gh.PytestBuildGate().build_and_test(worktree=root, src=root / "src")
    assert res.passed is True
    assert "-n" in fake.argvs[0]
    assert "-n" not in fake.argvs[1]


def test_build_gate_serial_retry_can_still_time_out(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="gateretrytimeout")
    monkeypatch.setattr(gh, "_XDIST_ARGV", ("-n", "auto"))
    monkeypatch.setattr(
        gh,
        "_run",
        _FakeRun(_Proc(4, "", "node down"), subprocess.TimeoutExpired("pytest", 900.0)),
    )
    res = gh.PytestBuildGate().build_and_test(worktree=root, src=root / "src")
    assert res.passed is False
    assert "timed out" in res.detail


def test_build_gate_serial_retry_can_still_fail_to_spawn(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="gateretryspawn")
    monkeypatch.setattr(gh, "_XDIST_ARGV", ("-n", "auto"))
    monkeypatch.setattr(
        gh, "_run", _FakeRun(_Proc(4, "", "worker crashed"), OSError("fork failed"))
    )
    res = gh.PytestBuildGate().build_and_test(worktree=root, src=root / "src")
    assert res.passed is False
    assert "could not run the suite" in res.detail


def test_build_gate_appends_a_narrowed_suite_scope(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="gatescope")
    fake = _FakeRun(_Proc(0))
    monkeypatch.setattr(gh, "_run", fake)
    gate = gh.PytestBuildGate(suite_scope=["apps/one/tests"])
    gate.build_and_test(worktree=root, src=root / "missing")
    assert fake.argvs[0][-1] == "apps/one/tests"


# ── ②b PytestBugRunner ──────────────────────────────────────────────────────


@pytest.mark.parametrize(("proc", "expected"), [(_Proc(0), True), (_Proc(1, "SyntaxError"), False)])
def test_bug_runner_import_smoke_compiles_rather_than_imports(
    tmp_path, monkeypatch, proc, expected
):
    root = _repo(tmp_path, name=f"smoke{expected}")
    fake = _FakeRun(proc)
    monkeypatch.setattr(gh, "_run", fake)
    assert gh.PytestBugRunner().build_imports_ok(src=root / "src") is expected
    assert "compileall" in fake.argvs[0]


def test_bug_runner_import_smoke_false_on_spawn_failure(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="smokeboom")
    monkeypatch.setattr(gh, "_run", _FakeRun(OSError("no exec")))
    assert gh.PytestBugRunner().build_imports_ok(src=root / "src") is False


def test_bug_runner_lint_findings_are_keyed_file_plus_code(tmp_path, monkeypatch):
    """Line numbers are dropped on purpose: an edit shifts every later line."""
    root = _repo(tmp_path, name="lintruff")
    out = (
        "src/pkg/mod.py:3:1: F401 `os` imported but unused\n"
        "\x1b[1msrc/pkg/other.py\x1b[0m:9:5: \x1b[31mF841\x1b[0m local unused\n"
        "warning: ruff config is deprecated\n"
        "error: bad config\n"
        "src/pkg/short.py:1:1\n"
        "nonsense\n"
    )
    monkeypatch.setattr(gh.shutil, "which", lambda name: "/usr/bin/ruff")
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(1, out)))
    findings = gh.PytestBugRunner()._lint_findings(root)
    assert findings == {
        "src/pkg/mod.py:F401",
        "src/pkg/other.py:F841",
        "src/pkg/short.py:?",
    }


def test_bug_runner_lint_findings_skips_a_missing_module_and_uses_pyflakes(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="lintflakes")
    fake = _FakeRun(
        _Proc(1, "", "No module named ruff"),
        _Proc(1, "src/pkg/mod.py:3:1: F401 unused\n"),
    )
    monkeypatch.setattr(gh.shutil, "which", lambda name: None)
    monkeypatch.setattr(gh, "_run", fake)
    assert gh.PytestBugRunner()._lint_findings(root) == {"src/pkg/mod.py:F401"}
    assert "ruff" in fake.argvs[0]
    assert "pyflakes" in fake.argvs[1]


def test_bug_runner_lint_findings_none_when_no_linter_runs(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="lintnone")
    monkeypatch.setattr(gh.shutil, "which", lambda name: None)
    monkeypatch.setattr(gh, "_run", _FakeRun(OSError("not installed")))
    assert gh.PytestBugRunner()._lint_findings(root) is None


def test_bug_runner_lint_clean_ignores_pre_existing_violations(tmp_path, monkeypatch):
    runner = gh.PytestBugRunner()
    monkeypatch.setattr(runner, "_lint_findings", lambda tree: {"a.py:F401"})
    assert runner.lint_clean(base_src=tmp_path / "b", cand_src=tmp_path / "c") is True


def test_bug_runner_lint_clean_rejects_a_new_violation(tmp_path, monkeypatch):
    runner = gh.PytestBugRunner()
    found = {tmp_path / "b": {"a.py:F401"}, tmp_path / "c": {"a.py:F401", "b.py:F841"}}
    monkeypatch.setattr(runner, "_lint_findings", lambda tree: found[Path(tree)])
    assert runner.lint_clean(base_src=tmp_path / "b", cand_src=tmp_path / "c") is False


def test_bug_runner_lint_clean_degrades_to_byte_compilation(tmp_path, monkeypatch):
    """No linter installed is a weaker but honest signal, not a fabricated pass."""
    runner = gh.PytestBugRunner()
    monkeypatch.setattr(runner, "_lint_findings", lambda tree: None)
    monkeypatch.setattr(runner, "build_imports_ok", lambda *, src: True)
    assert runner.lint_clean(base_src=tmp_path / "b", cand_src=tmp_path / "c") is True


@pytest.mark.parametrize(
    ("test_path", "proc", "expected"),
    [
        ("tests/test_bug_x.py", _Proc(0), True),
        ("tests/test_bug_x.py", _Proc(5), False),
        ("   ", _Proc(0), False),
        ("", _Proc(0), False),
    ],
)
def test_bug_runner_test_collects(tmp_path, monkeypatch, test_path, proc, expected):
    root = _repo(tmp_path, name=f"collects{abs(hash(test_path)) % 97}")
    monkeypatch.setattr(gh, "_run", _FakeRun(proc))
    assert gh.PytestBugRunner().test_collects(src=root, test_path=test_path) is expected


def test_bug_runner_test_collects_false_on_spawn_failure(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="collectsboom")
    monkeypatch.setattr(gh, "_run", _FakeRun(OSError("no exec")))
    assert gh.PytestBugRunner().test_collects(src=root, test_path="tests/test_x.py") is False


@pytest.mark.parametrize(
    ("proc", "expected"),
    [
        (_Proc(0, "1 passed"), True),
        (_Proc(1, "FAILED tests/test_x.py::test_bug - assert 1 == 2"), False),
        (_Proc(1, "ERROR tests/test_x.py\n1 error"), None),
        (_Proc(1, "!!! 2 errors during collection !!!"), None),
        (_Proc(2, "usage error"), None),
        (_Proc(5, "no tests ran"), None),
    ],
)
def test_bug_runner_run_reproducing_test_is_three_way(tmp_path, monkeypatch, proc, expected):
    """Exit 1 covers BOTH an assertion failure (valid RED) and a collection error."""
    root = _repo(tmp_path, name=f"repro{proc.returncode}{len(proc.stdout)}")
    monkeypatch.setattr(gh, "_run", _FakeRun(proc))
    got = gh.PytestBugRunner().run_reproducing_test(
        src=root, test_id="tests/test_x.py::test_bug", test_only=True
    )
    assert got is expected


@pytest.mark.parametrize("nodeid", ["", "   "])
def test_bug_runner_run_reproducing_test_none_without_a_nodeid(tmp_path, nodeid):
    got = gh.PytestBugRunner().run_reproducing_test(src=tmp_path, test_id=nodeid, test_only=False)
    assert got is None


def test_bug_runner_run_reproducing_test_none_on_spawn_failure(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="reproboom")
    monkeypatch.setattr(gh, "_run", _FakeRun(OSError("no exec")))
    got = gh.PytestBugRunner().run_reproducing_test(src=root, test_id="a::b", test_only=False)
    assert got is None


def test_bug_runner_run_suite_green(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="suitegreen")
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(0, "9 passed")))
    assert gh.PytestBugRunner().run_suite(src=root) == (True, [])


def test_bug_runner_run_suite_red_returns_the_nodeids(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="suitered")
    monkeypatch.setattr(gh, "_XDIST_ARGV", ())
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(1, "FAILED tests/test_a.py::test_one - x")))
    green, failing = gh.PytestBugRunner().run_suite(src=root)
    assert green is False
    assert failing == ["tests/test_a.py::test_one"]


def test_bug_runner_run_suite_unparsed_failure_uses_the_sentinel(tmp_path, monkeypatch):
    """An empty list beside False would let the gate subtract zero and admit a regression."""
    root = _repo(tmp_path, name="suitesentinel")
    monkeypatch.setattr(gh, "_XDIST_ARGV", ())
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(2, "INTERNALERROR")))
    assert gh.PytestBugRunner().run_suite(src=root) == (False, ["<unparsed-suite-failure>"])


def test_bug_runner_run_suite_sentinel_on_spawn_failure(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="suiteboom")
    monkeypatch.setattr(gh, "_run", _FakeRun(OSError("no exec")))
    assert gh.PytestBugRunner().run_suite(src=root) == (False, ["<unparsed-suite-failure>"])


def test_bug_runner_run_suite_retries_serially_after_an_xdist_failure(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="suitexdist")
    monkeypatch.setattr(gh, "_XDIST_ARGV", ("-n", "auto"))
    fake = _FakeRun(_Proc(4, "", "Replacing crashed worker"), _Proc(0, "9 passed"))
    monkeypatch.setattr(gh, "_run", fake)
    assert gh.PytestBugRunner().run_suite(src=root) == (True, [])
    assert "-n" not in fake.argvs[1]


def test_bug_runner_run_suite_sentinel_when_the_serial_retry_cannot_spawn(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="suiteretryboom")
    monkeypatch.setattr(gh, "_XDIST_ARGV", ("-n", "auto"))
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(4, "", "node down"), OSError("no exec")))
    assert gh.PytestBugRunner().run_suite(src=root) == (False, ["<unparsed-suite-failure>"])


def test_bug_runner_run_suite_serial_retry_still_red_yields_a_real_verdict(tmp_path, monkeypatch):
    """After an xdist-side failure the serial retry's own nodeids are what count."""
    root = _repo(tmp_path, name="suiteretryred")
    monkeypatch.setattr(gh, "_XDIST_ARGV", ("-n", "auto"))
    monkeypatch.setattr(
        gh,
        "_run",
        _FakeRun(
            _Proc(4, "", "node down"),
            _Proc(1, "FAILED tests/test_a.py::test_one - boom"),
        ),
    )
    got = gh.PytestBugRunner().run_suite(src=root)
    assert got == (False, ["tests/test_a.py::test_one"])


def test_bug_runner_run_suite_red_after_a_real_xdist_run(tmp_path, monkeypatch):
    root = _repo(tmp_path, name="suitexdistred")
    monkeypatch.setattr(gh, "_XDIST_ARGV", ("-n", "auto"))
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(1, "FAILED t/test_a.py::test_x - boom")))
    assert gh.PytestBugRunner().run_suite(src=root) == (False, ["t/test_a.py::test_x"])


def test_bug_runner_run_named_tests_returns_only_the_failures(tmp_path, monkeypatch):
    runner = gh.PytestBugRunner()
    verdicts = {"a::one": True, "a::two": False, "a::three": None}
    monkeypatch.setattr(
        runner,
        "run_reproducing_test",
        lambda *, src, test_id, test_only: verdicts[test_id],
    )
    root = _repo(tmp_path, name="named")
    got = runner.run_named_tests(src=root, test_ids=["a::one", "a::two", "a::three"])
    assert got == {"a::two", "a::three"}


@pytest.mark.parametrize("ids", [None, [], ["<unparsed-suite-failure>"], [""]])
def test_bug_runner_run_named_tests_empty_for_nothing_addressable(tmp_path, ids):
    assert gh.PytestBugRunner().run_named_tests(src=tmp_path, test_ids=ids) == set()


def test_bug_runner_agent_test_hint_names_this_interpreter(tmp_path):
    root = _repo(tmp_path, name="hint")
    hint = gh.PytestBugRunner().agent_test_hint(root)
    assert sys.executable in hint
    assert hint.endswith("-m pytest -q <test_path>")


# ── ③ RepoEditAllowlist ─────────────────────────────────────────────────────


def test_allowlist_admits_source_and_refuses_the_tests_of_record():
    fence = gh.RepoEditAllowlist()
    ok, offending = fence.allows(["src/pkg/mod.py"])
    assert (ok, offending) == (True, [])
    ok, offending = fence.allows(["tests/test_core.py"])
    assert ok is False
    assert offending == ["tests/test_core.py"]


@pytest.mark.parametrize(
    "path",
    [
        "setup.py",
        "pyproject.toml",
        "tox.ini",
        "pytest.ini",
        "requirements-dev.txt",
        "uv.lock",
        "Makefile",
        ".github/workflows/ci.yml",
        ".gitlab-ci.yml",
        "Dockerfile.prod",
        "conftest.py",
        "pkg/conftest.py",
        "pkg/test_thing.py",
        "pkg/thing_test.py",
    ],
)
def test_allowlist_refuses_every_always_off_limits_path(path):
    ok, offending = gh.RepoEditAllowlist().allows([path])
    assert ok is False
    assert offending == [path]


@pytest.mark.parametrize("path", ["/etc/passwd", "../../escape.py", "pkg/../../out.py", ""])
def test_allowlist_refuses_absolute_and_traversing_paths(path):
    ok, _offending = gh.RepoEditAllowlist().allows([path])
    assert ok is False


@pytest.mark.parametrize(
    "path",
    [
        ".kiro/settings/cli.json",
        ".claude/settings.json",
        ".aiderignore",
        ".DS_Store",
        "src/pkg/__pycache__/mod.cpython-311.pyc",
        "src/pkg/mod.pyc",
        ".pytest_cache/v/cache/lastfailed",
        ".ruff_cache/content",
        ".mypy_cache/3.11/x.json",
    ],
)
def test_allowlist_ignores_agent_tooling_debris(path):
    """Judging these rejected a verified RED-then-GREEN fix on the first live run."""
    fence = gh.RepoEditAllowlist()
    assert fence.is_tooling_artifact(path) is True
    ok, offending = fence.allows([path])
    assert (ok, offending) == (True, [])


def test_allowlist_traversal_is_checked_before_the_artifact_ignore():
    """A crafted ``.kiro/../../etc`` must not slip through by matching an ignore glob."""
    ok, _offending = gh.RepoEditAllowlist().allows([".kiro/../../etc/passwd"])
    assert ok is False


def test_allowlist_refuses_a_path_outside_a_narrowed_allowed_set():
    fence = gh.RepoEditAllowlist(allowed=["apps/one/**/*.py"])
    assert fence.allows(["apps/one/backend/server.py"])[0] is True
    assert fence.allows(["apps/two/backend/server.py"])[0] is False


def test_allowlist_name_only_form_judges_everything_as_a_modification():
    """Fail-closed: an unknown change cannot earn the added-test carve-out."""
    fence = gh.RepoEditAllowlist(track=TRACK_BUG)
    ok, offending = fence.allows(["test/test_bug_thing.py"])
    assert ok is False
    assert offending == ["test/test_bug_thing.py"]


@pytest.mark.parametrize(
    "path",
    ["test/test_bug_thing.py", "tests/test_bug_thing.py", "test/test_other.py", "tests/test_x.py"],
)
def test_allowlist_bug_track_may_add_a_reproducing_test(path):
    fence = gh.RepoEditAllowlist(track=TRACK_BUG)
    ok, offending = fence.allows_changes([("A", path)])
    assert (ok, offending) == (True, [])


def test_allowlist_perf_track_may_not_add_a_test_at_all():
    """The suite is the ruler's own measurement subject -- adding to it is gaming."""
    fence = gh.RepoEditAllowlist(track=TRACK_PERF)
    ok, offending = fence.allows_changes([("A", "test/test_bug_thing.py")])
    assert ok is False
    assert offending == ["test/test_bug_thing.py"]


def test_allowlist_carve_out_does_not_extend_to_modifying_a_test():
    fence = gh.RepoEditAllowlist(track=TRACK_BUG)
    assert fence.allows_changes([("M", "test/test_bug_thing.py")])[0] is False


def test_allowlist_carve_out_does_not_extend_to_a_rename_into_a_test_path():
    fence = gh.RepoEditAllowlist(track=TRACK_BUG)
    assert fence.allows_changes([("R100", "test/test_bug_thing.py")])[0] is False


def test_allowlist_added_test_outside_the_sanctioned_shape_is_refused():
    fence = gh.RepoEditAllowlist(track=TRACK_BUG)
    assert fence.allows_changes([("A", "spec/test_bug_thing.py")])[0] is False


def test_allowlist_scope_narrows_edits_but_exempts_additions():
    """A new file is never part of the base diff, so scope cannot judge it."""
    fence = gh.RepoEditAllowlist(scope={"src/pkg/mod.py"})
    assert fence.allows_changes([("M", "src/pkg/mod.py")])[0] is True
    assert fence.allows_changes([("M", "src/pkg/other.py")])[0] is False
    assert fence.allows_changes([("A", "src/pkg/other.py")])[0] is True


def test_allowlist_empty_scope_enforces_no_file_may_be_edited():
    """``scopeDiffBase=HEAD`` yields an empty diff -- keep nothing beats edit anything."""
    fence = gh.RepoEditAllowlist(scope=set())
    assert fence.allows_changes([("M", "src/pkg/mod.py")])[0] is False


@pytest.mark.parametrize("status", [None, "", "?"])
def test_allowlist_unknown_status_is_treated_as_a_modification(status):
    fence = gh.RepoEditAllowlist(track=TRACK_BUG)
    assert fence.allows_changes([(status, "test/test_bug_thing.py")])[0] is False


def test_allowlist_empty_change_sets_pass():
    fence = gh.RepoEditAllowlist()
    assert fence.allows([]) == (True, [])
    assert fence.allows_changes([]) == (True, [])
    assert fence.allows(None) == (True, [])
    assert fence.allows_changes(None) == (True, [])


def test_allowlist_reports_every_offending_path_not_just_the_first():
    fence = gh.RepoEditAllowlist()
    ok, offending = fence.allows(["src/pkg/mod.py", "setup.py", "tests/test_a.py"])
    assert ok is False
    assert offending == ["setup.py", "tests/test_a.py"]


# ── ④ RepoIsolation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("push_url", "fetch_url", "expected"),
    [
        ("DISABLED_NO_PUSH", "DISABLED_NO_PUSH", True),
        ("", "", True),
        ("DISABLED_NO_PUSH", "https://github.com/o/r.git", False),
        ("https://github.com/o/r.git", "DISABLED_NO_PUSH", False),
    ],
)
def test_isolation_push_disabled_requires_both_urls_neutral(
    tmp_path, monkeypatch, push_url, fetch_url, expected
):
    """A live FETCH url is a live push target -- checking only the push url was the bug."""
    clone = tmp_path / "clone"
    clone.mkdir()
    fake = _FakeRun(_Proc(0, push_url), _Proc(0, fetch_url))
    monkeypatch.setattr(gh, "_run", fake)
    iso = gh.RepoIsolation(clone_path=clone)
    assert iso.push_disabled() is expected
    assert iso.base_ref == "origin/main"


def test_isolation_push_disabled_fails_closed_on_a_git_error(tmp_path, monkeypatch):
    clone = tmp_path / "clone2"
    clone.mkdir()
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(128, "", "not a git repository")))
    assert gh.RepoIsolation(clone_path=clone, base_ref="").push_disabled() is False


def test_isolation_push_disabled_fails_closed_on_a_spawn_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "_run", _FakeRun(OSError("git missing")))
    iso = gh.RepoIsolation(clone_path=tmp_path / "absent", base_ref="origin/dev")
    assert iso.push_disabled() is False
    assert iso.base_ref == "origin/dev"


def test_isolation_do_not_pollute_paths_names_the_data_and_config_homes(tmp_path):
    iso = gh.RepoIsolation(clone_path=tmp_path / "c")
    paths = iso.do_not_pollute_paths()
    assert store.data_dir() in paths
    assert len(paths) == 2
    assert iso.do_not_pollute_excludes() == [store.data_dir()]
    assert iso.frozen_components == []


def test_isolation_do_not_pollute_tolerates_unavailable_roots(tmp_path, monkeypatch):
    def _boom():
        raise RuntimeError("no data dir")

    monkeypatch.setattr(store, "data_dir", _boom)
    from kiro_crew.config import loader as cfg_loader

    monkeypatch.setattr(cfg_loader, "config_dir", _boom)
    iso = gh.RepoIsolation(clone_path=tmp_path / "c")
    assert iso.do_not_pollute_paths() == []
    assert iso.do_not_pollute_excludes() == []


def test_isolation_measurement_boot_is_a_documented_no_op(tmp_path):
    boot = gh.RepoIsolation(clone_path=tmp_path / "c").measurement_boot()
    assert boot() is None


# ── the assembled profile ────────────────────────────────────────────────────


def _profile(tmp_path, monkeypatch, **kwargs) -> gh.GitHubRepoProfile:
    clone = kwargs.pop("clone", None) or _repo(tmp_path, name="profclone")
    monkeypatch.setattr(gh.scope_util, "scoped_relpaths", kwargs.pop("scoped", lambda c, r: None))
    return gh.GitHubRepoProfile(
        clone_path=clone,
        pr_queue_dir=tmp_path / "queue",
        **kwargs,
    )


def test_profile_assembles_all_six_fields(tmp_path, monkeypatch):
    prof = _profile(tmp_path, monkeypatch, user="octocat", baseline_reps=99, noise_floor_s=0.5)
    assert prof.id == "github-repo"
    assert isinstance(prof.ruler, gh.SuiteRuler)
    assert isinstance(prof.build_gate, gh.PytestBuildGate)
    assert isinstance(prof.bug_runner, gh.PytestBugRunner)
    assert isinstance(prof.edit_allowlist, gh.RepoEditAllowlist)
    assert isinstance(prof.isolation, gh.RepoIsolation)
    assert prof.pr_recipe.namespace == "github/octocat"
    assert prof.calibration.baseline_reps == 10  # clamped to 2..10
    assert prof.calibration.floor == pytest.approx(0.5)
    assert prof.calibration.canary_id == "collect_only_vs_full_suite"
    # ProfileFieldAliases: both doc spellings resolve.
    assert prof.isolation_recipe is prof.isolation
    assert prof.calibration_params is prof.calibration


def test_profile_clamps_calibration_reps_up_to_two(tmp_path, monkeypatch):
    assert _profile(tmp_path, monkeypatch, baseline_reps=1).calibration.baseline_reps == 2


def test_profile_refuses_an_uncomputable_scope(tmp_path, monkeypatch):
    """An uncomputable scope silently widens the fence to the whole repository."""
    with pytest.raises(ValueError, match="could not be resolved to a file set"):
        _profile(tmp_path, monkeypatch, scope_base="origin/nope", scoped=lambda c, r: None)


def test_profile_accepts_an_empty_but_successful_scope(tmp_path, monkeypatch):
    prof = _profile(tmp_path, monkeypatch, scope_base="HEAD", scoped=lambda c, r: set())
    assert prof.edit_allowlist.scope == set()
    assert prof.edit_allowlist.allows_changes([("M", "src/pkg/mod.py")])[0] is False


def test_profile_scopes_the_gate_suite_to_a_narrowed_allowlist(tmp_path, monkeypatch, caplog):
    clone = tmp_path / "mono2"
    (clone / "apps" / "one" / "tests").mkdir(parents=True)
    with caplog.at_level("INFO"):
        prof = _profile(tmp_path, monkeypatch, clone=clone, allowed_globs=["apps/one/**/*.py"])
    expected = str(Path("apps") / "one" / "tests")
    assert prof.build_gate.suite_scope == [expected]
    assert prof.bug_runner.suite_scope == [expected]
    assert prof.suite_scope_for_profiling == [expected]
    assert "gate suite scoped to" in caplog.text


def test_profile_leaves_the_gate_unscoped_without_an_allowlist(tmp_path, monkeypatch):
    prof = _profile(tmp_path, monkeypatch)
    assert prof.build_gate.suite_scope == []
    assert prof.suite_scope_for_profiling == []


def test_profile_discover_is_honest_offline(tmp_path, monkeypatch):
    """No runner returns nothing rather than a fabricated candidate list."""
    prof = _profile(tmp_path, monkeypatch)
    res = prof.discover(base_sha="abc", top_k=[], known_loci=[])
    assert res.candidates == []
    assert "offline" in res.notes


def test_profile_discover_maps_agent_surfaces_to_candidates(tmp_path, monkeypatch):
    clone = _repo(tmp_path, name="discclone", tests="tests")
    prof = _profile(tmp_path, monkeypatch, clone=clone, track=TRACK_BUG)
    seen: dict[str, object] = {}

    def _fake_discover(runner, **kwargs):
        seen.update(kwargs)
        return [
            {"target": "src/pkg/mod.py::frob", "hypothesis": "off-by-one", "rule": "B001"},
            {"file": "src/pkg/other.py", "message": "unbounded read"},
        ]

    monkeypatch.setattr(gh.agent_discovery, "discover_surfaces_via_agent", _fake_discover)
    prof._skip_targets = ["src/pkg/done.py"]
    prof._discovery_rotate = 3
    res = prof.discover(base_sha="abc", top_k=[], known_loci=[], agent_runner=object())
    assert len(res.candidates) == 2
    assert res.notes == "2 agent surface(s); scope=repo"
    assert seen["skip_targets"] == ["src/pkg/done.py"]
    assert seen["rotate"] == 3
    assert seen["edit_globs"] is None
    first = res.candidates[0]
    assert first.kind == TRACK_BUG
    assert first.target == "src/pkg/mod.py::frob"
    assert first.reproducing_test.added_by_candidate is True
    assert first.reproducing_test.test_path == "tests/test_bug_src_pkg_mod_py_frob.py"
    assert res.candidates[1].target == "src/pkg/other.py"
    assert res.candidates[1].signature == "unbounded read"


def test_profile_discover_reports_a_diff_scope_in_its_notes(tmp_path, monkeypatch):
    prof = _profile(
        tmp_path, monkeypatch, scope_base="origin/main", scoped=lambda c, r: {"src/pkg/mod.py"}
    )
    monkeypatch.setattr(gh.agent_discovery, "discover_surfaces_via_agent", lambda r, **kw: [])
    res = prof.discover(base_sha="abc", top_k=[], known_loci=[], agent_runner=object())
    assert res.notes == "0 agent surface(s); scope=diff"


def test_profile_discover_steers_reads_with_a_narrowed_allowlist(tmp_path, monkeypatch):
    clone = tmp_path / "mono3"
    (clone / "apps" / "one" / "tests").mkdir(parents=True)
    prof = _profile(tmp_path, monkeypatch, clone=clone, allowed_globs=["apps/one/**/*.py"])
    seen: dict[str, object] = {}

    def _fake_discover(runner, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(gh.agent_discovery, "discover_surfaces_via_agent", _fake_discover)
    prof.discover(base_sha="abc", top_k=[], known_loci=[], agent_runner=object())
    assert seen["edit_globs"] == ["apps/one/**/*.py"]


def test_profile_perf_candidate_carries_no_reproducing_test(tmp_path, monkeypatch):
    prof = _profile(tmp_path, monkeypatch, track=TRACK_PERF)
    cand = prof._candidate_from({"target": "src/pkg/mod.py", "message": "hot loop", "rule": "P1"})
    assert cand.kind == TRACK_PERF
    assert cand.scenario == "suite"
    assert cand.evidence == "P1"
    assert cand.reproducing_test is None


def test_profile_bug_candidate_slug_survives_an_unnamed_surface(tmp_path, monkeypatch):
    clone = _repo(tmp_path, name="slugclone", tests="test")
    prof = _profile(tmp_path, monkeypatch, clone=clone, track=TRACK_BUG)
    cand = prof._candidate_from({"target": "!!!"})
    assert cand.reproducing_test.test_path == "test/test_bug_surface.py"


@pytest.mark.parametrize(
    ("layout", "expected"),
    [({"test": 3, "tests": 1}, "test"), ({"tests": 2}, "tests"), ({}, "test")],
)
def test_profile_test_dir_follows_where_the_tests_really_are(
    tmp_path, monkeypatch, layout, expected
):
    """A repo can have BOTH; writing into the minor one lands outside the gated suite."""
    clone = tmp_path / f"layout_{expected}_{len(layout)}"
    clone.mkdir()
    for name, count in layout.items():
        (clone / name).mkdir()
        for i in range(count):
            (clone / name / f"test_{i}.py").write_text("", newline="\n")
    prof = _profile(tmp_path, monkeypatch, clone=clone)
    assert prof._test_dir() == expected


def test_profile_propose_never_fabricates_a_mechanical_edit(tmp_path, monkeypatch):
    prof = _profile(tmp_path, monkeypatch)
    from kiro_crew.apps.builtins.auto_improvement.spine.contracts import Candidate

    got = prof.propose(
        candidate=Candidate(kind=TRACK_BUG, target="src/pkg/mod.py"),
        base_sha="abc",
        worktree=tmp_path / "wt",
        tier="T1",
    )
    assert got is False


def test_profile_capture_profile_normalizes_a_pstats_artifact(tmp_path, monkeypatch):
    clone = _repo(tmp_path, name="profcap", tests="tests")
    prof = _profile(tmp_path, monkeypatch, clone=clone)
    raw_dir = store.profiles_dir()

    def _fake_run(argv, *, cwd, timeout, env=None):
        Path(argv[argv.index("-o") + 1]).write_text("pstats", newline="\n")
        return _Proc(0)

    monkeypatch.setattr(gh, "_run", _fake_run)
    monkeypatch.setattr(PN, "capture_profile", lambda fp, raw, *, scenario: {"fp": fp, "hot": []})
    got = prof.capture_profile(fp="abc123", worktree=clone)
    assert got == {"fp": "abc123", "hot": []}
    assert (raw_dir / "abc123.pstats").is_file()


def test_profile_capture_profile_none_when_no_artifact_was_written(tmp_path, monkeypatch):
    clone = _repo(tmp_path, name="profcapempty", tests="tests")
    prof = _profile(tmp_path, monkeypatch, clone=clone)
    monkeypatch.setattr(gh, "_run", _FakeRun(_Proc(1, "pytest failed")))
    assert prof.capture_profile(fp="def456", worktree=clone) is None


def test_profile_capture_profile_never_fails_a_run(tmp_path, monkeypatch):
    """Profiling is observability: an error must not block a measured candidate."""
    clone = _repo(tmp_path, name="profcapboom", tests="tests")
    prof = _profile(tmp_path, monkeypatch, clone=clone)
    monkeypatch.setattr(gh, "_run", _FakeRun(OSError("no exec")))
    assert prof.capture_profile(fp="ghi789", worktree=clone) is None


def test_profile_capture_profile_does_not_double_the_src_segment(tmp_path, monkeypatch):
    """Appending ``src`` unconditionally produced ``<wt>/src/src`` and profiled nothing."""
    clone = _repo(tmp_path, name="profcaproot", tests="tests")
    prof = _profile(tmp_path, monkeypatch, clone=clone)
    fake = _FakeRun(_Proc(0))
    monkeypatch.setattr(gh, "_run", fake)
    monkeypatch.setattr(PN, "capture_profile", lambda fp, raw, *, scenario: None)
    prof.capture_profile(fp="jkl012", worktree=clone)
    assert os.path.realpath(str(fake.calls[0]["cwd"])) == os.path.realpath(str(clone))
    assert "-n" not in fake.argvs[0]
    assert "cProfile" in fake.argvs[0]


# ── the factory ──────────────────────────────────────────────────────────────


def test_resolve_origin_url_delegates_to_the_validating_helper(monkeypatch):
    monkeypatch.setattr(
        clone_setup, "resolve_origin_url", lambda cfg: f"validated:{cfg.get('origin_url')}"
    )
    assert gh._resolve_origin_url({"origin_url": "u"}) == "validated:u"


def test_build_profile_refuses_without_a_configured_clone():
    with pytest.raises(ValueError, match="no repository configured"):
        gh.build_profile({})
    with pytest.raises(ValueError, match="no repository configured"):
        gh.build_profile({"clone": "   "})


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("", "origin/main"),
        ("dev", "origin/dev"),
        ("origin/dev", "origin/dev"),
        ("   ", "origin/main"),
    ],
)
def test_build_profile_normalizes_the_base_ref(tmp_path, monkeypatch, branch, expected):
    clone = _repo(tmp_path, name=f"factory_{expected.replace('/', '_')}")
    monkeypatch.setattr(gh.scope_util, "scoped_relpaths", lambda c, r: None)
    monkeypatch.setattr(gh, "_resolve_origin_url", lambda cfg: "https://github.com/o/r.git")
    prof = gh.build_profile({"clone": str(clone), "branch": branch})
    assert prof.isolation.base_ref == expected
    assert prof.pr_recipe.base_ref == expected


def test_build_profile_reads_every_config_key(tmp_path, monkeypatch):
    clone = _repo(tmp_path, name="factoryfull")
    monkeypatch.setattr(gh.scope_util, "scoped_relpaths", lambda c, r: {"src/pkg/mod.py"})
    monkeypatch.setattr(gh, "_resolve_origin_url", lambda cfg: "https://github.com/o/r.git")
    prof = gh.build_profile(
        {
            "clone": str(clone),
            "branch": "main",
            "prUser": "octocat",
            "track": TRACK_PERF,
            "benchmarkCommand": "make bench",
            "scopeDiffBase": "origin/main",
            "editAllowlist": ["src/**/*.py"],
            "calibrationReps": 7,
            "noiseFloorSeconds": 0.75,
        }
    )
    assert prof.track == TRACK_PERF
    assert prof.ruler.benchmark_cmd == "make bench"
    assert prof.scope_base == "origin/main"
    assert prof.edit_allowlist.allowed == ["src/**/*.py"]
    assert prof.edit_allowlist.track == TRACK_PERF
    assert prof.calibration.baseline_reps == 7
    assert prof.calibration.floor == pytest.approx(0.75)
    assert prof.pr_recipe.fetch_url == "https://github.com/o/r.git"
    assert prof.pr_recipe.user == "octocat"


@pytest.mark.parametrize("value", [[], None, "not-a-list", {}])
def test_build_profile_falls_back_to_the_repo_wide_edit_default(tmp_path, monkeypatch, value):
    """Only a genuinely NARROWED allowlist steers discovery reads."""
    clone = _repo(tmp_path, name=f"factorydefault{abs(hash(str(value))) % 89}")
    monkeypatch.setattr(gh.scope_util, "scoped_relpaths", lambda c, r: None)
    monkeypatch.setattr(gh, "_resolve_origin_url", lambda cfg: "")
    prof = gh.build_profile({"clone": str(clone), "editAllowlist": value})
    assert prof._user_edit_globs is None
    assert "src/**/*.py" in prof.edit_allowlist.allowed
    assert prof.pr_recipe.fetch_url is None
