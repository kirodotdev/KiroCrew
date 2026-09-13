"""Installing packaged skills from a read-only source tree.

``shutil.copytree`` preserves source modes verbatim, so a packaged install
whose source tree is read-only or owner-unsearchable (for example, mode
``0o555`` or ``0o455``) yields a destination copy whose directories reject
file creation by the owning uid. Without repair,
``_register_core_skills`` re-raises the ``PermissionError`` and takes the
gateway down, and the builtin sync installs every skill without provenance.

The install therefore normalizes owner rwx on the destination after
each copytree (``ensure_owner_rwx_dirs``), and the fingerprint machinery
hashes the PACKAGED SOURCE side as the copy will look after that repair
(``assume_owner_rwx_dirs=True``) while hashing the installed side with
its real modes -- so a clean install from a 0o555 source does not read as a
user chmod on the next sync, while ANY later mode change on the installed
copy (adding group-write, removing owner rwx, chmod +x on a file) still
diverges the tree. The deploy-skill refresh removes its own stale managed
copies with ``rmtree_force``, so read-only files inside the copy (preserved
from the source) cannot abort the next startup on Windows either.

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
    # Make the root searchable before collecting descendants, then apply
    # restrictive modes deepest-first so the walk cannot lock itself out.
    current = stat.S_IMODE(os.lstat(root).st_mode)
    os.chmod(root, current | stat.S_IRWXU)
    directories = [Path(dirpath) for dirpath, _d, _f in os.walk(root)]
    for directory in reversed(directories):
        os.chmod(directory, mode)


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
class TestEnsureOwnerRwxDirs:
    def test_adds_owner_rwx_to_0455_dirs_and_leaves_files_alone(
        self, readonly_source: Path
    ) -> None:
        # Keep the platform-only helper local to the behavior it exercises.
        from kiro_crew.platform_compat import ensure_owner_rwx_dirs

        skill = _make_skill_tree(readonly_source, "alpha")
        os.chmod(skill / "SKILL.md", 0o444)
        _chmod_dirs(skill, 0o455)

        ensure_owner_rwx_dirs(skill)

        for dirpath, _d, _f in os.walk(skill):
            mode = stat.S_IMODE(os.lstat(dirpath).st_mode)
            assert mode & stat.S_IRWXU == stat.S_IRWXU, dirpath
            assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0o55, dirpath
        # Group/other bits are preserved; owner rwx is complete.
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

    def test_0455_copy_installs_marker_and_verifies(
        self, wired_source: Path, base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A copytree result preserving 0o455 source dirs is repaired before
        marker creation and matches the source-side normalized fingerprint."""
        import shutil

        src = _make_skill_tree(wired_source, "theta")
        real_copytree = shutil.copytree

        def copytree_with_0455_dirs(
            source: Path, dest: Path, *args: object, **kwargs: object
        ) -> Path:
            copied = Path(real_copytree(source, dest, *args, **kwargs))
            _chmod_dirs(copied, 0o455)
            return copied

        # The wrapper models a source owned by another account: group/other can
        # search its 0o455 dirs, while copytree preserves the owner triad.
        monkeypatch.setattr(skills_mod.shutil, "copytree", copytree_with_0455_dirs)

        _ensure_builtin_skills(base)
        dest = base / "theta"

        assert (dest / _PROVENANCE_MARKER).exists()
        assert _verified_unchanged_fingerprint(dest, src) is not None

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
        """Only owner rwx is normalized: any other directory-mode
        customization (here: restricting group/other access) still reads as
        a user edit."""
        src = _make_skill_tree(wired_source, "delta")
        _ensure_builtin_skills(base)
        dest = base / "delta"
        assert _verified_unchanged_fingerprint(dest, src) is not None

        # Fixture simulating a user chmod on their own installed skill;
        # 0o700 grants nothing to group/other, so the finding is suppressed
        # on the line below (same placement as platform_compat.py).
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501
        os.chmod(dest / "scripts", 0o700)

        assert _verified_unchanged_fingerprint(dest, src) is None

    def test_removing_owner_write_still_diverges(self, wired_source: Path, base: Path) -> None:
        """The normalization is asymmetric: only the packaged-source side is
        hashed with owner rwx. A user who chmods u-w an installed builtin
        (write-protecting it) must diverge the tree, or the next update would
        silently replace their protected copy without a quarantine."""
        src = _make_skill_tree(wired_source, "iota")
        _ensure_builtin_skills(base)
        dest = base / "iota"
        assert _verified_unchanged_fingerprint(dest, src) is not None

        os.chmod(dest / "scripts", 0o555)  # user removes owner write

        assert _verified_unchanged_fingerprint(dest, src) is None

    def test_file_mode_customization_still_diverges(self, wired_source: Path, base: Path) -> None:
        """File modes are never normalized: chmod +x on an installed file is a
        user customization and must diverge the fingerprint."""
        src = _make_skill_tree(wired_source, "epsilon")
        _ensure_builtin_skills(base)
        dest = base / "epsilon"
        assert _verified_unchanged_fingerprint(dest, src) is not None

        # Fixture simulating a user chmod (owner-executable, private);
        # 0o700 grants nothing to group/other, so the finding is suppressed
        # on the line below (same placement as platform_compat.py).
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501
        os.chmod(dest / "scripts" / "run.py", 0o700)

        assert _verified_unchanged_fingerprint(dest, src) is None


class TestDeployManagedReplacement:
    def _install_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        import kiro_crew.deploy as deploy_pkg

        source_root = tmp_path / "deploy-src"
        if not source_root.exists():
            source_root.mkdir()
            _make_skill_tree(source_root, "artifact-deploy")
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        monkeypatch.setattr(deploy_pkg, "config_dir", lambda: home)
        monkeypatch.setattr(deploy_pkg, "_SKILLS_DIR", source_root)
        deploy_pkg._register_core_skills()
        return home / "skills" / "artifact-deploy"

    def test_replacing_a_managed_copy_with_readonly_files_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A managed copy carrying read-only files (preserved from a read-only
        source) is removed via rmtree_force on refresh, so the startup path
        does not depend on plain rmtree's mode handling."""
        import kiro_crew.deploy as deploy_pkg

        installed = self._install_once(tmp_path, monkeypatch)
        os.chmod(installed / "SKILL.md", 0o444)
        # Force a refresh by making the source newer than the copy.
        src_md = tmp_path / "deploy-src" / "artifact-deploy" / "SKILL.md"
        future = os.path.getmtime(src_md) + 120
        os.utime(src_md, (future, future))

        deploy_pkg._register_core_skills()  # must not raise

        assert (installed / ".kirocrew-managed").exists()

    def test_a_surviving_managed_copy_still_fails_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rmtree_force never raises; a tree that survives it is a genuinely
        failed install and must keep the fail-loud contract."""
        import kiro_crew.deploy as deploy_pkg

        self._install_once(tmp_path, monkeypatch)
        monkeypatch.setattr(deploy_pkg, "rmtree_force", lambda _p: False)

        with pytest.raises(OSError, match="could not remove"):
            deploy_pkg._register_core_skills()


@_POSIX_MODES
def test_source_fingerprint_predicts_the_normalized_copy(tmp_path: Path) -> None:
    """Hashing a 0o555 source with ``assume_owner_rwx_dirs=True`` equals
    hashing its normalized 0o755 copy with real modes -- which lets the
    sync record the SOURCE fingerprint (immutable while the sync runs) and
    still recognise the normalized destination as its own. Without the flag
    the two differ: the installed side is always hashed with real modes, so a
    user chmod on the copy diverges."""
    import shutil

    a = _make_skill_tree(tmp_path, "zeta-a")
    b = tmp_path / "zeta-b"
    shutil.copytree(a, b)  # byte-identical content, writable modes
    try:
        _chmod_dirs(a, 0o555)
        assert _skill_tree_fingerprint(a, assume_owner_rwx_dirs=True) == _skill_tree_fingerprint(b)
        assert _skill_tree_fingerprint(a) != _skill_tree_fingerprint(b)
    finally:
        _chmod_dirs(a, 0o755)
