"""Builtin-skill sync: a failed copy costs only that skill.

The copy is staged in a private sibling and published with one rename, so a
failure leaves nothing at the skill's name, never touches a concurrent save
there, and never removes another sync's stage.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import test_builtin_skill_sync_safety as _sync_safety

from kiro_crew import skills as skills_mod
from kiro_crew.skills import _ensure_builtin_skills

# The packaged-root and skills-base fixtures are shared with the main suite.
builtin_root = _sync_safety.builtin_root
base = _sync_safety.base
_make_skill = _sync_safety._make_skill


class TestFailedCopy:
    def test_failed_copy_costs_only_that_skill(
        self, builtin_root: Path, base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A copy that fails with an ordinary OSError (full disk, permission,
        # over-long path) is logged and skipped; the sync still installs every
        # other packaged skill instead of raising out half-way.
        import errno

        for name in ("alpha", "broken", "omega"):
            _make_skill(builtin_root, name, f"{name} body")
        real_copytree = skills_mod.shutil.copytree

        def _full_disk(src_arg: object, dst_arg: object, **kw: object) -> object:
            if Path(str(src_arg)).name == "broken":
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_copytree(str(src_arg), str(dst_arg), **kw)

        monkeypatch.setattr(skills_mod.shutil, "copytree", _full_disk)
        _ensure_builtin_skills(base)

        assert (base / "alpha" / "SKILL.md").is_file()
        assert (base / "omega" / "SKILL.md").is_file()
        assert not (base / "broken").exists()

    def test_partial_copy_does_not_retire_relocated_flat_skill(
        self, builtin_root: Path, base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A copy that fails after writing SKILL.md must not leave that partial
        # tree at the skill's name: there it would count as an installed
        # relocation target (quarantining the complete flat copy) and read as
        # up to date, so the next sync would never retry the install.
        import errno

        monkeypatch.setattr(skills_mod, "_RELOCATED_SKILLS", {"old-flat": "fam/new"})
        _make_skill(builtin_root / "fam", "new", "new body", {"scripts/run.py": "x"})
        _make_skill(base, "old-flat", "complete old skill")
        real_copytree = skills_mod.shutil.copytree

        def _partial(src_arg: object, dst_arg: object, **kw: object) -> object:
            dst = Path(str(dst_arg))
            dst.mkdir(parents=True)
            (dst / "SKILL.md").write_bytes((Path(str(src_arg)) / "SKILL.md").read_bytes())
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(skills_mod.shutil, "copytree", _partial)
        _ensure_builtin_skills(base)
        _ensure_builtin_skills(base)

        assert (base / "old-flat" / "SKILL.md").is_file()
        assert not (base / "fam" / "new").exists()
        # A copy that fails on every sync leaves nothing behind, hidden or not.
        assert [p.name for p in (base / "fam").iterdir()] == []

        monkeypatch.setattr(skills_mod.shutil, "copytree", real_copytree)
        _ensure_builtin_skills(base)

        assert (base / "fam" / "new" / "scripts" / "run.py").is_file()

    def test_failed_copy_never_touches_the_live_skill_name(
        self, builtin_root: Path, base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A save that lands at the skill's live name while the sync's copy is
        # failing is not this sync's output and must survive the cleanup.
        import errno

        _make_skill(builtin_root, "helper", "packaged", {"scripts/run.py": "x"})

        def _fail_after_save(src_arg: object, dst_arg: object, **kw: object) -> object:
            _make_skill(base, "helper", "USER saved edit")
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(skills_mod.shutil, "copytree", _fail_after_save)
        _ensure_builtin_skills(base)

        assert "USER saved edit" in (base / "helper" / "SKILL.md").read_text(encoding="utf-8")
        assert not list(base.glob(".helper.sync-stage*"))

    def test_rival_sync_stage_is_never_removed(
        self, builtin_root: Path, base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Another process syncing the same home creates its own stage while
        # this sync is choosing one. This sync must neither copy into nor
        # remove that stage, and still installs the skill itself.
        _make_skill(builtin_root, "helper", "packaged")
        rival = base / ".helper.sync-stage"
        real_copytree = skills_mod.shutil.copytree

        def _rival_appears(src_arg: object, dst_arg: object, **kw: object) -> object:
            if not rival.exists():
                rival.mkdir(parents=True)
                (rival / "in-progress.txt").write_text("rival", encoding="utf-8")
            return real_copytree(str(src_arg), str(dst_arg), **kw)

        monkeypatch.setattr(skills_mod.shutil, "copytree", _rival_appears)
        _ensure_builtin_skills(base)

        assert (rival / "in-progress.txt").read_text(encoding="utf-8") == "rival"
        assert "packaged" in (base / "helper" / "SKILL.md").read_text(encoding="utf-8")
