"""The ``--dry-run`` smoke path (the spine's own self-test).

``driver._run_dry`` builds a throwaway git repo and drives one full cycle with
:class:`StubProfile`, so it exercises the real worktree/commit/gate plumbing without
a model or a network. It is the cheapest end-to-end signal the spine has.

It was BROKEN and silently so: ``driver`` imports ``StubProfile`` from
``.stub_profile``, but the class lives in ``.profile`` and no ``stub_profile`` module
existed, so ``--dry-run`` raised ImportError on entry. Nothing covered it, so the
whole suite stayed green over a dead feature. These tests keep that from recurring.
"""

from __future__ import annotations

from pathlib import Path


class TestStubProfileImport:
    """The import path ``driver._run_dry`` actually uses."""

    def test_stub_profile_module_is_importable(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.stub_profile import StubProfile

        assert StubProfile.id == "stub"

    def test_it_is_the_same_class_as_in_profile(self) -> None:
        """A shim, not a fork — a second definition would drift from the real one."""
        from kiro_crew.apps.builtins.auto_improvement.spine import profile as P
        from kiro_crew.apps.builtins.auto_improvement.spine import stub_profile as S

        assert S.StubProfile is P.StubProfile

    def test_the_driver_can_resolve_it(self, tmp_path: Path) -> None:
        """Mirrors the exact local import inside ``_run_dry``, which is where the
        ImportError fired."""
        from kiro_crew.apps.builtins.auto_improvement.spine.stub_profile import StubProfile

        prof = StubProfile(clone_path=tmp_path / "clone", queue_dir=tmp_path / "queue")
        # The six TargetProfile fields the spine consumes must all be wired.
        for field in ("ruler", "build_gate", "edit_allowlist", "isolation", "pr_recipe"):
            assert getattr(prof, field) is not None
        assert prof.calibration.baseline_reps == 5

    def test_stub_discovery_yields_one_candidate(self, tmp_path: Path) -> None:
        """A dry-run cycle needs exactly one deterministic candidate to drive."""
        from kiro_crew.apps.builtins.auto_improvement.spine.stub_profile import StubProfile

        prof = StubProfile(clone_path=tmp_path / "clone", queue_dir=tmp_path / "queue")
        res = prof.discover(base_sha="deadbeef", top_k=[], known_loci=[])
        assert len(res.candidates) == 1
        assert res.candidates[0].target == "stub_module.py::stub_symbol"
