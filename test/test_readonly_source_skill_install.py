"""Installing packaged skills from a read-only source tree.

``shutil.copytree`` preserves source modes verbatim, so a packaged install
whose source tree is read-only (mode ``0o555`` -- a Nix store path, a
read-only mount, any hardened install) yields a destination copy whose
directories reject file creation by the owning uid. Without repair,
``_register_core_skills`` re-raises the ``PermissionError`` and takes the
gateway down, and the builtin sync installs every skill without provenance.

The install therefore normalizes owner-writability on the destination after
each copytree (``ensure_owner_writable_dirs``) and OR-s the owner-write bit
into BOTH sides of the fingerprint comparison, so the install-owned repair
does not read as a user chmod on the next sync -- while any other mode
customization still diverges the tree.

POSIX-only where the tests assert real mode bits: on Windows ``os.chmod``
honours only the read-only flag, so a 0o555 fixture cannot be built there.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from kiro_crew import skills as skills_mod
from kiro_crew.skills import (
    _PROVENANCE_MARKER,
    _ensure_builtin_skills,
    _skill_tree_fingerprint,
    _verified_unchanged_fingerprint,
)

_POSIX_MODES = pytest.mark.skipif(
    sys.platform == "win32", reason="asserts real POSIX mode bits (0o555 fixture)"
)


def _make_skill_tree(root: Path, name: str) -> Path:
    """A packaged skill dir with a nested subdirectory, like real builtins."""
    skill_dir = root / name
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: readonly-source fixture\n---\nbody\n",
        encoding="utf-8",
    )
    (scripts / "run.py").write_text("print('hi')\n", encoding="utf-8")
    return skill_dir


def _chmod_dirs(root: Path, mode: int) -> None:
    for dirpath, _dirnames, _filenames in os.walk(root):
        os.chmod(dirpath, mode)


@pytest.fixture()
def readonly_source(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    """A packaged source root whose directories are 0o555, restored on teardown.

    The restore is the load-bearing half: a 0o555 fixture left behind breaks
    pytest's tmp_path cleanup for the whole session.
    """
    root = tmp_path / "packaged-src"
    root.mkdir()
    request.addfinalizer(lambda: _chmod_dirs(root, 0o755))
    return root


@_POSIX_MODES
class TestEnsureOwnerWritableDirs:
    def test_adds_owner_write_to_every_dir_and_leaves_files_alone(
        self, readonly_source: Path
    ) -> None:
        # Imported lazily so red-before proof runs against main, where the
        # helper does not exist yet: the behavioral tests below must fail on
        # their assertions (PermissionError / missing marker), not at import.
        from kiro_crew.platform_compat import ensure_owner_writable_dirs

        skill = _make_skill_tree(readonly_source, "alpha")
        os.chmod(skill / "SKILL.md", 0o444)
        _chmod_dirs(readonly_source, 0o555)

        ensure_owner_writable_dirs(skill)

        for dirpath, _d, _f in os.walk(skill):
            assert os.lstat(dirpath).st_mode & stat.S_IWUSR, dirpath
        # Group/other bits are preserved -- only owner-write is added.
        assert stat.S_IMODE(os.lstat(skill).st_mode) == 0o755
        # File modes are exactly as shipped: a file-mode customization must
        # still diverge the fingerprint, so the helper never touches files.
        assert stat.S_IMODE(os.lstat(skill / "SKILL.md").st_mode) == 0o444


@_POSIX_MODES
class TestDeployRegisterCoreSkills:
    def test_readonly_source_installs_marker_and_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
    ) -> None:
        """Red-before: on main this raised PermissionError and killed startup."""
        import kiro_crew.deploy as deploy_pkg

        source_root = tmp_path / "deploy-src"
        source_root.mkdir()
        _make_skill_tree(source_root, "artifact-deploy")
        request.addfinalizer(lambda: _chmod_dirs(source_root, 0o755))
        _chmod_dirs(source_root, 0o555)

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(deploy_pkg, "config_dir", lambda: home)
        monkeypatch.setattr(deploy_pkg, "_SKILLS_DIR", source_root)

        deploy_pkg._register_core_skills()  # must not raise

        installed = home / "skills" / "artifact-deploy"
        assert (installed / ".kirocrew-managed").exists()
        assert (installed / "scripts" / "run.py").exists()


@_POSIX_MODES
class TestBuiltinSyncFromReadonlySource:
    @pytest.fixture()
    def base(self, tmp_path: Path) -> Path:
        dest = tmp_path / "installed-skills"
        dest.mkdir()
        return dest

    @pytest.fixture()
    def wired_source(self, readonly_source: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setattr(skills_mod, "_BUILTIN_SKILLS_DIR", readonly_source)
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        return readonly_source

    def test_provenance_marker_written(self, wired_source: Path, base: Path) -> None:
        """Red-before: mkstemp(dir=dest_dir) failed with PermissionError, so
        every skill installed with only a warning and no provenance."""
        _make_skill_tree(wired_source, "beta")
        _chmod_dirs(wired_source, 0o555)

        _ensure_builtin_skills(base)

        assert (base / "beta" / _PROVENANCE_MARKER).exists()
        assert (base / "beta" / "scripts" / "run.py").exists()

    def test_second_sync_does_not_read_install_as_user_customized(
        self, wired_source: Path, base: Path
    ) -> None:
        """The drift regression a naive chmod-only fix introduces: the source
        fingerprint is recorded as the installed state, so adding a write bit
        to the destination alone makes a clean install read as user-edited on
        the very next sync -- which licenses quarantine of an untouched tree."""
        src = _make_skill_tree(wired_source, "gamma")
        _chmod_dirs(wired_source, 0o555)

        _ensure_builtin_skills(base)
        dest = base / "gamma"

        # The freshly installed (normalized 0o555 -> 0o755) copy verifies as
        # the sync's own unchanged install, not as a user chmod.
        assert _verified_unchanged_fingerprint(dest, src) is not None

        # And a real update pass replaces it in place: no user-backup
        # quarantine appears for a tree the sync itself installed.
        _chmod_dirs(wired_source, 0o755)
        (src / "SKILL.md").write_text(
            "---\nname: gamma\ndescription: v2\n---\nv2\n", encoding="utf-8"
        )
        future = os.path.getmtime(src / "SKILL.md") + 120
        os.utime(src / "SKILL.md", (future, future))
        _chmod_dirs(wired_source, 0o555)

        _ensure_builtin_skills(base)

        assert "v2" in (dest / "SKILL.md").read_text(encoding="utf-8")
        leftovers = [p.name for p in base.iterdir() if "user-backup" in p.name]
        assert leftovers == []

    def test_genuine_user_chmod_still_diverges(self, wired_source: Path, base: Path) -> None:
        """Only the owner-write bit is normalized: any other directory-mode
        customization (here: group-write) still reads as a user edit."""
        src = _make_skill_tree(wired_source, "delta")
        _ensure_builtin_skills(base)
        dest = base / "delta"
        assert _verified_unchanged_fingerprint(dest, src) is not None

        os.chmod(dest / "scripts", 0o775)  # user adds group-write

        assert _verified_unchanged_fingerprint(dest, src) is None

    def test_file_mode_customization_still_diverges(self, wired_source: Path, base: Path) -> None:
        """File modes are never normalized: chmod +x on an installed file is a
        user customization and must diverge the fingerprint."""
        src = _make_skill_tree(wired_source, "epsilon")
        _ensure_builtin_skills(base)
        dest = base / "epsilon"
        assert _verified_unchanged_fingerprint(dest, src) is not None

        os.chmod(dest / "scripts" / "run.py", 0o755)

        assert _verified_unchanged_fingerprint(dest, src) is None


@_POSIX_MODES
def test_fingerprint_equal_across_owner_write_normalization(tmp_path: Path) -> None:
    """A 0o555 source and its normalized 0o755 copy fingerprint identically,
    which is what lets the sync record the SOURCE fingerprint (immutable while
    the sync runs) and still recognise the normalized destination as its own."""
    import shutil

    a = _make_skill_tree(tmp_path, "zeta-a")
    b = tmp_path / "zeta-b"
    shutil.copytree(a, b)  # byte-identical content, writable modes
    try:
        _chmod_dirs(a, 0o555)
        assert _skill_tree_fingerprint(a) == _skill_tree_fingerprint(b)
    finally:
        _chmod_dirs(a, 0o755)
