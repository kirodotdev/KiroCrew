"""The profile registry's single entry point.

``build_profile`` is the only seam the run supervisor uses, and its two documented
properties are structural rather than behavioural: the profile module is imported
LAZILY inside the function (module-scope would drag the whole spine into every gateway
boot), and a missing repository surfaces as ``ValueError`` for the supervisor to turn
into a 409 rather than a crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement import profiles


class TestProfileIds:
    def test_the_reference_profile_is_selectable(self) -> None:
        assert profiles.PROFILE_IDS == ("github-repo",)

    def test_the_registry_exports_only_the_ids_and_the_builder(self) -> None:
        assert profiles.__all__ == ["PROFILE_IDS", "build_profile"]


class TestBuildProfile:
    def test_an_unconfigured_repository_raises_value_error(self) -> None:
        """A user-fixable setup problem, not a crash — the supervisor maps it to 409."""
        with pytest.raises(ValueError, match="no repository configured"):
            profiles.build_profile({})

    def test_a_none_config_is_treated_as_empty_rather_than_crashing(self) -> None:
        with pytest.raises(ValueError, match="no repository configured"):
            profiles.build_profile(None)  # type: ignore[arg-type]

    def test_a_configured_clone_builds_the_github_repo_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = tmp_path / "clone"
        clone.mkdir()
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.auto_improvement.backend.store.data_dir",
            lambda: tmp_path / "data",
        )
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.auto_improvement.backend.store.workspace_dir",
            lambda: tmp_path / "data",
        )
        built = profiles.build_profile({"clone": str(clone), "branch": "feat/x"})
        assert Path(built.clone_path) == clone

    def test_the_profile_module_is_imported_lazily_not_at_module_scope(self) -> None:
        """Gateway boot imports this package; it must not pull in the spine."""
        source = Path(profiles.__file__).read_text(encoding="utf-8")
        lazy_import = "from .github_repo.profile import build_profile as _build_github_repo"
        assert lazy_import in source
        # The lazy import is indented inside the function, never at column 0.
        assert f"\n{lazy_import}" not in source
