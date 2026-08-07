"""The profiler DATA PATH — captures actually happen now.

``profile_normalize`` (pstats/.cpuprofile → frame tree) and both endpoints
(``GET /profiles``, ``GET /profile/{fp}``) already existed, but no production code ever
called a ``capture_*``: only the normalizer's own tests did. So ``profiles_dir()`` was
never written outside tests and the profiler views were permanently empty — a shipped
surface with no data path.

The driver now calls ``profile.capture_profile(fp=..., worktree=...)`` per perf
candidate, deliberately AFTER the timed A/B (cProfile's overhead is exactly the
variance the noise band excludes, so profiling a measured arm would corrupt the number
it is meant to explain).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import profile_normalize as PN
from kiro_crew.apps.builtins.auto_improvement.backend import store
from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.profile import (
    build_profile,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A flat repo (no ``src/``) with a real suite, plus an isolated data home."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "home" / "data")
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "m.py").write_text("def work():\n    return sum(i * i for i in range(20000))\n")
    (root / "tests" / "test_m.py").write_text(
        "from m import work\n\n\ndef test_work():\n    assert work() > 0\n"
    )
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.invalid", cwd=root)
    _git("config", "user.name", "T", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "base", cwd=root)
    return root


class TestCaptureProfile:
    def test_a_capture_writes_a_tree_the_endpoints_can_read(self, repo: Path) -> None:
        prof = build_profile({"clone": str(repo), "branch": "main"})
        fp = "a" * 16
        tree = prof.capture_profile(fp=fp, worktree=repo)
        assert tree is not None, "capture produced nothing"
        # The shape the frontend's flame/icicle view consumes.
        for key in ("root", "total", "unit", "scenario", "kind"):
            assert key in tree
        # And the READ side both endpoints use.
        assert fp in [p.get("fp") for p in (PN.list_profiles() or [])]
        assert PN.read_profile(fp) is not None

    def test_both_the_raw_and_normalized_artifacts_land(self, repo: Path) -> None:
        """The raw .pstats is kept beside the .json so a capture stays auditable."""
        prof = build_profile({"clone": str(repo), "branch": "main"})
        prof.capture_profile(fp="b" * 16, worktree=repo)
        suffixes = sorted(p.suffix for p in store.profiles_dir().glob("b*"))
        assert suffixes == [".json", ".pstats"]

    def test_a_src_layout_repo_is_not_double_prefixed(self, tmp_path: Path, repo: Path) -> None:
        """Regression: appending "src" unconditionally produced <wt>/src/src, so pytest
        found nothing to profile and the capture silently returned None."""
        prof = build_profile({"clone": str(repo), "branch": "main"})
        inner = repo / "src"
        inner.mkdir()
        (inner / "pkg.py").write_text("x = 1\n")
        assert prof.capture_profile(fp="c" * 16, worktree=repo) is not None

    def test_an_unsafe_fingerprint_is_refused(self, repo: Path) -> None:
        """The fp becomes a filename; traversal must not escape profiles_dir."""
        prof = build_profile({"clone": str(repo), "branch": "main"})
        assert prof.capture_profile(fp="../../etc/passwd", worktree=repo) is None

    def test_a_missing_subdir_resolves_up_rather_than_failing(self, repo: Path) -> None:
        """``_repo_root`` walks up to the real run root, which is the established
        convention for the spine's unconditional ``<tree>/src`` (see the module
        docstring) — so a path whose leaf does not exist still profiles the repo
        instead of erroring. Never raises either way."""
        prof = build_profile({"clone": str(repo), "branch": "main"})
        assert prof.capture_profile(fp="d" * 16, worktree=repo / "nope") is not None

    def test_an_empty_suite_yields_a_tree_with_no_target_frames(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With nothing to collect, cProfile still records the pytest run itself, so we
        get a VALID but essentially empty tree rather than an exception. Capture is
        observability: it must never raise into the run, and it must not fabricate
        frames that were not measured."""
        # Pin the data home. Unlike its siblings this test does not take the `repo`
        # fixture, so nothing pinned `store.data_dir` and it read the DEVELOPER'S real
        # `~/.kiro/crew/.../config.json` — writing profile artifacts there, and keying the
        # workspace off whatever repo/branch happened to be configured. Order-dependent by
        # construction; it only surfaced when the workspace key started reflecting config.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "home" / "data")
        empty = tmp_path / "elsewhere"
        (empty / "tests").mkdir(parents=True)
        prof = build_profile({"clone": str(empty), "branch": "main"})
        tree = prof.capture_profile(fp="e" * 16, worktree=empty)
        assert tree is not None and "root" in tree
        # No frame from a module under test, because none ran.
        assert not any("m.py" in str(c.get("name", "")) for c in tree["root"].get("children", []))

    def test_profiling_reuses_the_gate_suite_scope(self, repo: Path) -> None:
        """A monorepo must profile its edit region, not the whole repo."""
        prof = build_profile({"clone": str(repo), "branch": "main", "editAllowlist": ["m.py"]})
        assert prof.suite_scope_for_profiling == prof.bug_runner.suite_scope


class TestDriverHook:
    """The call site — absent before, which is why nothing was ever captured."""

    def test_the_driver_calls_capture_after_measuring(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        src = Path(D.__file__).read_text(encoding="utf-8")
        assert "self._capture_profile(prop)" in src
        # It must come AFTER measure(), never inside a timed arm.
        assert src.index("self.measurer.measure(") < src.index("self._capture_profile(prop)")

    def test_a_profile_without_the_hook_is_skipped(self) -> None:
        """getattr-probed, so a profile that offers no profiler is unaffected."""
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        src = Path(D.__file__).read_text(encoding="utf-8")
        assert 'getattr(self.profile, "capture_profile", None)' in src
