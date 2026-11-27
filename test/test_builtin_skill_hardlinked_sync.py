"""The built-in skill sync on a hardlinking installer's package tree.

uv hardlinks package files out of its cache when the cache shares a filesystem
with the venv, so every file under the installed ``kiro_crew/builtin_skills/``
carries ``st_nlink > 1``. The packaged tree is fingerprinted, its ``SKILL.md``
carried and its currency judged by reading those files, and a hardlinked one is
admitted only when its bytes match the digest the installer's RECORD lists for
that exact path. An installed copy in the skills dir, and any file without a
RECORD row, keeps the plain hardlink refusal.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import pytest

import kiro_crew.skills as skills_mod
from kiro_crew.skill_runtime import authoring
from kiro_crew.skills import _PROVENANCE_MARKER, _ensure_builtin_skills, _skill_tree_fingerprint
from kiro_crew.skills_builtin_provenance import SKILL_INSTALL_IN_SYNC, _skill_currency_state

#: Fixed packaged mtimes (seconds): release two is strictly newer than release
#: one, and a user edit lands between them, so no comparison reads the clock.
_RELEASE_ONE_MTIME = 1_700_000_000
_USER_EDIT_MTIME = 1_700_000_500
_RELEASE_TWO_MTIME = 1_700_001_000

_NAME = "demo"
_SKILL_MD = "---\nname: demo\ndescription: a packaged skill\n---\n# Demo\nstep one\n"


def _record_digest(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _HardlinkedInstall:
    """A site dir whose ``kiro_crew`` package came from a hardlinking installer.

    Every package file shares its inode with a name in the installer's cache,
    and ``kirocrew-0.0.0.dist-info/RECORD`` lists each file's sha256.
    """

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        site = tmp_path / "site"
        self.package = site / "kiro_crew"
        self.builtin_root = self.package / "builtin_skills"
        self.src = self.builtin_root / _NAME
        self.dist_info = site / "kirocrew-0.0.0.dist-info"
        self.dist_info.mkdir(parents=True)
        self.release = 0
        self.files: dict[str, bytes] = {}
        self.install_release(
            {
                "__init__.py": b"",
                f"builtin_skills/{_NAME}/SKILL.md": _SKILL_MD.encode("utf-8"),
                f"builtin_skills/{_NAME}/scripts/run.py": b"print('release one')\n",
            },
            _RELEASE_ONE_MTIME,
        )
        monkeypatch.setattr(skills_mod, "_INSTALLED_PACKAGE_DIR", self.package)
        monkeypatch.setattr(skills_mod, "_package_record_cache", {})
        monkeypatch.setattr(skills_mod, "_BUILTIN_SKILLS_DIR", self.builtin_root)
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        self.base = tmp_path / "skills"
        self.base.mkdir()

    def install_release(self, files: dict[str, bytes], mtime: int) -> None:
        """Lay *files* down the way the installer does: cache first, then a link."""
        self.release += 1
        cache = self.tmp_path / f"installer-cache-{self.release}"
        for rel, data in files.items():
            cached = cache / rel
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
            os.utime(cached, (mtime, mtime))
            target = self.package / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.unlink()
            try:
                os.link(cached, target)
            except OSError as exc:  # pragma: no cover — a filesystem without hardlinks
                pytest.skip(f"hardlinks unavailable here: {exc}")
            self.files[rel] = data
        self.write_record(self.files)

    def write_record(self, recorded: dict[str, bytes]) -> None:
        """Publish a RECORD listing *recorded* as a new file, as a reinstall does."""
        lines = [
            f"kiro_crew/{rel},{_record_digest(data)},{len(data)}" for rel, data in recorded.items()
        ]
        lines.append(f"{self.dist_info.name}/RECORD,,")
        staged = self.dist_info / "RECORD.new"
        staged.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(staged, self.dist_info / "RECORD")

    def release_two(self) -> bytes:
        """Ship new bytes for ``scripts/run.py``; every other file is re-linked unchanged."""
        new_script = b"print('release two')\n"
        files = dict(self.files)
        files[f"builtin_skills/{_NAME}/scripts/run.py"] = new_script
        self.install_release(files, _RELEASE_TWO_MTIME)
        return new_script

    @property
    def dest(self) -> Path:
        return self.base / _NAME


@pytest.fixture()
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _HardlinkedInstall:
    made = _HardlinkedInstall(tmp_path, monkeypatch)
    assert (made.src / "scripts" / "run.py").stat().st_nlink == 2
    assert (made.package / "__init__.py").stat().st_nlink == 2
    return made


def _backups(base: Path) -> list[str]:
    return sorted(entry.name for entry in base.iterdir() if entry.name.endswith(".user-backup"))


class TestAHardlinkedPackageSyncs:
    def test_an_unchanged_install_is_updated_in_place(self, install: _HardlinkedInstall) -> None:
        assert _ensure_builtin_skills(install.base) == []
        assert (install.dest / _PROVENANCE_MARKER).is_file()

        new_script = install.release_two()

        assert _ensure_builtin_skills(install.base) == []
        assert _backups(install.base) == []
        assert (install.dest / "scripts" / "run.py").read_bytes() == new_script

    def test_a_carried_opt_out_survives_the_update(self, install: _HardlinkedInstall) -> None:
        _ensure_builtin_skills(install.base)
        installed = install.dest / "SKILL.md"
        opted = authoring.rewrite_inject_on_trigger(installed.read_text(encoding="utf-8"), False)
        assert opted is not None
        # Bytes, as the toggle writes them: write_text turns every newline into
        # CRLF on Windows, which is a real edit the update must set aside.
        installed.write_bytes(opted.encode("utf-8"))
        os.utime(installed, (_USER_EDIT_MTIME, _USER_EDIT_MTIME))

        new_script = install.release_two()

        assert _ensure_builtin_skills(install.base) == []
        assert _backups(install.base) == []
        assert "inject_on_trigger: false" in installed.read_text(encoding="utf-8")
        assert (install.dest / "scripts" / "run.py").read_bytes() == new_script

    def test_the_doctor_reads_a_fresh_install_as_in_sync(self, install: _HardlinkedInstall) -> None:
        _ensure_builtin_skills(install.base)

        assert _skill_currency_state(install.dest, install.src) == SKILL_INSTALL_IN_SYNC


class TestTheStrictReadStillHolds:
    def test_bytes_that_differ_from_the_record_are_unprovable(
        self, install: _HardlinkedInstall
    ) -> None:
        recorded = dict(install.files)
        recorded[f"builtin_skills/{_NAME}/scripts/run.py"] = b"what the installer wrote\n"
        install.write_record(recorded)

        assert _skill_tree_fingerprint(install.src, assume_owner_rwx_dirs=True) is None

    def test_a_hardlink_in_the_installed_copy_is_unprovable(
        self, install: _HardlinkedInstall, tmp_path: Path
    ) -> None:
        _ensure_builtin_skills(install.base)
        assert _skill_tree_fingerprint(install.dest) is not None
        os.link(install.dest / "scripts" / "run.py", tmp_path / "second-name")

        assert _skill_tree_fingerprint(install.dest) is None

    def test_a_hardlink_under_a_project_root_is_unprovable(
        self, install: _HardlinkedInstall, tmp_path: Path
    ) -> None:
        project_src = tmp_path / "project" / "skills" / _NAME
        project_src.mkdir(parents=True)
        (project_src / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
        assert _skill_tree_fingerprint(project_src, assume_owner_rwx_dirs=True) is not None
        os.link(project_src / "SKILL.md", tmp_path / "second-name")

        assert _skill_tree_fingerprint(project_src, assume_owner_rwx_dirs=True) is None
