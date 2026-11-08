"""How the skills loader serves a granted project's skills (``kiro_crew.skills``).

A grant names one directory, so discovery and every read stay inside it: a skills
root, skill directory or ``SKILL.md`` that is a link is refused, cached paths are
re-checked at read time, metadata reads are confined like body reads, and each
enumerated item carries its vetted root. The file also covers CRLF, undecodable and
oversized ``SKILL.md`` files, the SEL record of the loader's enforcement decision, the
trust directory's owner-only mode, and triggered and indexed project skills reaching
the prompt without a project path, within the section budget and only after a grant.
The ``project`` and ``_isolated_home`` fixtures live in ``skill_trust_store_helpers.py``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from skill_trust_store_helpers import (  # noqa: F401 -- fixtures register by import or by name
    _isolated_home,
    project_fixture,
)

from kiro_crew import platform_compat, skill_trust
from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig

pytestmark = pytest.mark.skipif(
    not skill_trust.project_skill_traversal_supported(),
    reason="project skills require no-follow directory-descriptor traversal",
)


class TestConsentCoversOnlyTheGrantedDirectory:
    """A grant names ONE directory; discovery must not reach outside it."""

    def test_a_symlinked_skills_root_is_refused(self, tmp_path, monkeypatch):
        from kiro_crew.skills import SkillsLoader

        # `.kiro/skills` is a link OUT of the project the operator trusted.
        outside = tmp_path / "outside" / "skills"
        (outside / "smuggled").mkdir(parents=True)
        (outside / "smuggled" / "SKILL.md").write_text(
            "---\nname: smuggled\ndescription: d\n---\n\nSMUGGLED BODY\n", encoding="utf-8"
        )
        project = tmp_path / "proj"
        (project / ".kiro").mkdir(parents=True)
        try:
            (project / ".kiro" / "skills").symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        # Consenting to `project` must not admit bodies stored outside it.
        assert "smuggled" not in {n for n, _, _ in loader._iter(project)}
        assert loader.load_skill("smuggled", project) is None

    def test_a_symlinked_skill_file_inside_a_real_root_is_refused(self, tmp_path):
        """A linked SKILL.md inside a CONTAINED skills root must not enumerate.

        The directory-level check cannot catch this: `evil/` is a real directory
        inside the consented tree, and only the file it holds points out. It is
        not a cosmetic listing bug -- _cached_frontmatter parses the linked file,
        so an attacker-controlled name/description/trigger set would reach
        list_skills and the injected "Available Skills" index, which tells the
        agent to cat that path.
        """
        from kiro_crew.skills import SkillsLoader

        outside = tmp_path / "outside"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text(
            "---\nname: smuggled-file\ndescription: d\n---\n\nSMUGGLED BODY\n",
            encoding="utf-8",
        )
        project = tmp_path / "proj"
        evil = project / ".kiro" / "skills" / "evil"
        evil.mkdir(parents=True)
        try:
            (evil / "SKILL.md").symlink_to(outside / "SKILL.md")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        assert "evil" not in {n for n, _, _ in loader._iter(project)}
        assert loader.load_skill("evil", project) is None

    def test_a_symlinked_subdirectory_inside_a_real_root_is_refused(self, tmp_path):
        """Same escape one level up: the skill DIRECTORY is the link."""
        from kiro_crew.skills import SkillsLoader

        outside = tmp_path / "outside" / "smuggled-dir"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text(
            "---\nname: smuggled-dir\ndescription: d\n---\n\nSMUGGLED BODY\n",
            encoding="utf-8",
        )
        project = tmp_path / "proj"
        skills_root = project / ".kiro" / "skills"
        skills_root.mkdir(parents=True)
        try:
            (skills_root / "smuggled-dir").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        assert "smuggled-dir" not in {n for n, _, _ in loader._iter(project)}

    def test_a_project_directory_link_is_pruned_before_target_resolution(
        self, tmp_path, monkeypatch
    ):
        """Walking a project link must not resolve a target that could be UNC."""
        from kiro_crew import skills as skills_mod
        from kiro_crew.skills import SkillsLoader

        outside = tmp_path / "outside" / "linked"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text("---\nname: linked\n---\nbody\n", encoding="utf-8")
        project = tmp_path / "project"
        skills_root = project / ".kiro" / "skills"
        skills_root.mkdir(parents=True)
        link = skills_root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        skill_trust.grant_project_trust(project)
        realpath = skills_mod.os.path.realpath

        def refuse_link_resolution(path, *args, **kwargs):
            if str(path).startswith(str(link)):
                raise AssertionError("project directory link target was resolved")
            return realpath(path, *args, **kwargs)

        monkeypatch.setattr(skills_mod.os.path, "realpath", refuse_link_resolution)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        assert "linked" not in {name for name, _, _ in loader._iter(project)}

    def test_a_linked_project_skills_root_is_refused_before_resolution(self, tmp_path, monkeypatch):
        """The root link itself may target UNC and must not be probed."""
        from kiro_crew import skills as skills_mod
        from kiro_crew.skills import SkillsLoader

        outside = tmp_path / "outside"
        outside.mkdir()
        project = tmp_path / "project"
        (project / ".kiro").mkdir(parents=True)
        skills_root = project / ".kiro" / "skills"
        try:
            skills_root.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)
        realpath = skills_mod.os.path.realpath

        def refuse_root_resolution(path, *args, **kwargs):
            if str(path) == str(skills_root):
                raise AssertionError("linked skills root was resolved")
            return realpath(path, *args, **kwargs)

        monkeypatch.setattr(skills_mod.os.path, "realpath", refuse_root_resolution)

        assert loader.catalog_project_skills(project) == []

    def test_a_confined_walk_does_not_delegate_link_classification_to_os_walk(
        self, project, monkeypatch
    ):
        """os.walk may follow a Windows reparse target just to classify it."""
        from kiro_crew import skills as skills_mod

        genuine = project / ".kiro" / "skills" / "genuine"
        genuine.mkdir(parents=True, exist_ok=True)
        (genuine / "SKILL.md").write_text("---\nname: genuine\n---\nbody\n", encoding="utf-8")

        def unsafe_walk(*_args, **_kwargs):
            raise AssertionError("confined project traversal delegated to os.walk")

        monkeypatch.setattr(skills_mod.os, "walk", unsafe_walk)
        project_key = skill_trust.canonical_key(project)
        assert project_key is not None

        pairs = skills_mod._iter_skill_files(
            project / ".kiro" / "skills", confine_to=(project_key,)
        )

        assert {name for name, _path in pairs} == {"genuine"}

    def test_a_confined_walk_scans_only_open_directory_descriptors(self, project, monkeypatch):
        """Every traversed component stays pinned to a no-follow handle."""
        from kiro_crew import skills as skills_mod

        genuine = project / ".kiro" / "skills" / "genuine"
        genuine.mkdir(parents=True, exist_ok=True)
        (genuine / "SKILL.md").write_text("---\nname: genuine\n---\nbody\n", encoding="utf-8")
        project_key = skill_trust.canonical_key(project)
        assert project_key is not None

        scandir = skills_mod.os.scandir

        def descriptor_only(target):
            if not isinstance(target, int):
                raise AssertionError("confined project traversal scanned a path")
            return scandir(target)

        monkeypatch.setattr(skills_mod.os, "scandir", descriptor_only)

        pairs = skills_mod._iter_skill_files(
            project / ".kiro" / "skills", confine_to=(project_key,)
        )

        assert {name for name, _path in pairs} == {"genuine"}

    def test_a_confined_walk_requires_the_granted_projects_exact_skills_root(
        self, project, tmp_path, monkeypatch
    ):
        """A caller cannot use confinement mode to open an unrelated tree."""
        from kiro_crew import skills as skills_mod

        project_key = skill_trust.canonical_key(project)
        assert project_key is not None
        unrelated = tmp_path / "unrelated"

        def unsafe_open(*_args, **_kwargs):
            raise AssertionError("a mismatched confined root reached the filesystem")

        monkeypatch.setattr(skills_mod, "_open_project_dir_chain", unsafe_open)

        assert skills_mod._iter_skill_files(unrelated, confine_to=(project_key,)) == []

    def test_a_directory_swapped_for_a_link_between_scan_and_open_is_refused(
        self, project, tmp_path, monkeypatch
    ):
        """O_NOFOLLOW closes the exact race reported by review."""
        from kiro_crew import skills as skills_mod

        category = project / ".kiro" / "skills" / "category"
        genuine = category / "genuine"
        genuine.mkdir(parents=True)
        (genuine / "SKILL.md").write_text("---\nname: genuine\n---\nbody\n", encoding="utf-8")
        outside = tmp_path / "outside"
        smuggled = outside / "smuggled"
        smuggled.mkdir(parents=True)
        (smuggled / "SKILL.md").write_text("---\nname: smuggled\n---\nbody\n", encoding="utf-8")
        replacement = category.with_name("category-before-swap")
        try:
            probe = tmp_path / "link-probe"
            probe.symlink_to(outside, target_is_directory=True)
            probe.unlink()
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        project_key = skill_trust.canonical_key(project)
        assert project_key is not None

        real_open = skills_mod.os.open
        swapped = False

        def swap_before_child_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if path == "category" and dir_fd is not None and not swapped:
                category.rename(replacement)
                category.symlink_to(outside, target_is_directory=True)
                swapped = True
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(skills_mod.os, "open", swap_before_child_open)

        pairs = skills_mod._iter_skill_files(
            project / ".kiro" / "skills", confine_to=(project_key,)
        )

        assert swapped is True
        assert pairs == []

    def test_project_skills_fail_closed_without_nofollow_dirfd_support(
        self, project, tmp_path, monkeypatch
    ):
        """Unsupported platforms never resolve or traverse a project path."""
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "genuine"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: genuine\n---\nbody\n", encoding="utf-8")
        skill_trust.grant_project_trust(project)

        monkeypatch.setattr(skill_trust, "project_skill_traversal_supported", lambda: False)

        # ``skill_trust.os.path`` IS the process-wide ``os.path`` module, so this
        # patch is seen by every caller in the interpreter -- pytest's own tmpdir
        # bookkeeping and any audit hook that canonicalises the paths it records
        # included. A fake that raised for EVERY argument therefore blew up in
        # code that had nothing to do with the property under test (the hygiene
        # probe counted 11 hook failures per run and marked this test
        # under-measured). Refuse only what the test is about: the project tree.
        real_realpath = os.path.realpath
        project_prefix = str(project)

        def unsafe_realpath(path, *args, **kwargs):
            resolved = os.fspath(path)
            if isinstance(resolved, bytes):
                resolved = os.fsdecode(resolved)
            if resolved == project_prefix or resolved.startswith(project_prefix + os.sep):
                raise AssertionError("unsupported project path was resolved")
            return real_realpath(path, *args, **kwargs)

        monkeypatch.setattr(skill_trust.os.path, "realpath", unsafe_realpath)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        assert loader.catalog_project_skills(project) == []
        assert loader.get_triggered_skills("genuine", project) == []

    def test_a_project_may_not_link_into_a_provider_root(self, tmp_path, monkeypatch):
        """A project skills root is confined to the project, not to the provider roots.

        `_trusted_skill_roots()` exists so an APP's registered symlink resolves
        out of the skills dir into the app's own tree. A consented project has no
        such need, and inheriting that allowance would let a repository publish a
        builtin/app SKILL.md under any name it liked -- shadowing by aliasing
        rather than by copying. The GLOBAL tree keeps the allowance, which is what
        the second half of this test holds fixed.
        """
        from kiro_crew import skills as skills_mod
        from kiro_crew.skills import SkillsLoader

        provider = tmp_path / "provider"
        (provider / "vendor-skill").mkdir(parents=True)
        (provider / "vendor-skill" / "SKILL.md").write_text(
            "---\nname: vendor-skill\ndescription: d\n---\n\nVENDOR BODY\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(skills_mod, "_trusted_skill_roots", lambda: (str(provider.resolve()),))

        project = tmp_path / "proj"
        skills_root = project / ".kiro" / "skills"
        skills_root.mkdir(parents=True)
        home_skills = tmp_path / "home-skills"
        home_skills.mkdir()
        try:
            (skills_root / "aliased").symlink_to(
                provider / "vendor-skill", target_is_directory=True
            )
            (home_skills / "aliased-global").symlink_to(
                provider / "vendor-skill", target_is_directory=True
            )
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=home_skills, install_builtins=False)
        names = {n for n, _, _ in loader._iter(project)}

        # The project may not reach a provider root...
        assert "aliased" not in names
        # ...but the global skills tree still may, or every installed app breaks.
        assert "aliased-global" in names

    def test_refusing_a_linked_root_says_so(self, tmp_path, caplog):
        """The refusal is logged, not silent.

        Two layers now refuse a `.kiro/skills` link out of the consented tree:
        this call-site check and the walker's own confinement. The walker prunes
        silently, so without this check an operator who granted trust would see
        an empty skills list and no reason for it.
        """
        import logging

        from kiro_crew.skills import SkillsLoader

        outside = tmp_path / "outside" / "skills"
        (outside / "smuggled").mkdir(parents=True)
        (outside / "smuggled" / "SKILL.md").write_text(
            "---\nname: smuggled\ndescription: d\n---\n\nBODY\n", encoding="utf-8"
        )
        project = tmp_path / "proj"
        (project / ".kiro").mkdir(parents=True)
        try:
            (project / ".kiro" / "skills").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.skills"):
            names = {n for n, _, _ in loader._iter(project)}

        assert "smuggled" not in names
        assert any(
            "Refusing project skills traversal" in r.getMessage() for r in caplog.records
        ), f"no refusal logged; records={[r.getMessage() for r in caplog.records]}"

    def test_a_real_in_project_skills_dir_still_loads(self, tmp_path):
        from kiro_crew.skills import SkillsLoader

        project = tmp_path / "proj2"
        d = project / ".kiro" / "skills" / "genuine"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: genuine\ndescription: d\n---\n\nGENUINE BODY\n", encoding="utf-8"
        )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills2", install_builtins=False)

        assert "genuine" in {n for n, _, _ in loader._iter(project)}
        assert "GENUINE BODY" in (loader.load_skill("genuine", project) or "")

    def test_confined_walk_stops_before_python_recursion_can_fail(self, monkeypatch):
        from kiro_crew import skills as skills_mod

        class FakeEntry:
            name = "nested"

            @staticmethod
            def stat(*, follow_symlinks):
                assert follow_symlinks is False
                return SimpleNamespace(st_mode=stat.S_IFDIR)

        class FakeScanner:
            def __enter__(self):
                return iter([FakeEntry()])

            def __exit__(self, *_args):
                return False

        opened: list[int] = []

        def open_child(_name, _flags, *, dir_fd):
            child_fd = dir_fd + 1
            opened.append(child_fd)
            return child_fd

        # ``skills_mod.os`` is the process-wide os module.  Restore its functions
        # before pytest's own temporary-directory teardown uses them on Python 3.12.
        with monkeypatch.context() as patch:
            patch.setattr(skills_mod.os, "scandir", lambda _fd: FakeScanner())
            patch.setattr(skills_mod.os, "open", open_child)
            patch.setattr(skills_mod.os, "close", lambda _fd: None)

            rows = list(skills_mod._walk_confined_skill_fd(0, Path("/skills")))

        assert len(rows) == 65
        assert len(opened) == 64

    def test_a_triggered_project_skill_survives_the_body_pointer_split(self, tmp_path):
        """A matched project skill must not be dropped by ``split_triggered``.

        ``get_triggered_skills`` is project-aware, so it can return a project
        skill's name; if the split then resolves project-blind it gets ``None``
        and is skipped, contributing neither a body nor a pointer. The match
        would appear to succeed while injecting nothing at all.
        """
        from kiro_crew.skills import SkillsLoader

        project = tmp_path / "proj3"
        d = project / ".kiro" / "skills" / "triggered"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: triggered\ndescription: d\ntriggers: shorten url\n---\n\nBODY\n",
            encoding="utf-8",
        )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills3", install_builtins=False)

        bodies, pointers = loader.split_triggered(["triggered"], project)
        assert "triggered" in bodies + pointers

        # Project-blind matching silently drops the name.
        assert loader.split_triggered(["triggered"]) == ([], [])


class TestCachedPathsCannotEscapeAfterVetting:
    """A vetted path is re-validated at READ time, not trusted from the cache."""

    def test_a_skill_swapped_for_a_link_after_caching_is_refused(self, project, tmp_path):
        """Warm the cache with a legitimate skill, then swap the file for a link.

        `_iter` caches for a TTL, so without a read-time check `load_skill` would
        serve whatever the path names a minute later. The read goes through
        `safe_read_file_bytes_nolink(within_root=...)`, which opens O_NOFOLLOW and
        containment-checks the descriptor it actually read, so the swap is caught
        on the inode rather than on the stale path string.
        """
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "legit"
        skill_dir.mkdir(parents=True)
        good = skill_dir / "SKILL.md"
        good.write_text("---\nname: legit\ndescription: d\n---\n\nGOOD BODY\n", encoding="utf-8")

        outside = tmp_path / "outside.md"
        outside.write_text(
            "---\nname: legit\ndescription: d\n---\n\nSMUGGLED BODY\n", encoding="utf-8"
        )

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        # Warm the cache -- the path is vetted here, while it is still genuine.
        assert "legit" in {n for n, _, _ in loader._iter(project)}
        assert "GOOD BODY" in (loader.load_skill("legit", project) or "")

        # Now swap the vetted file for a link pointing out of the project. The
        # cache still holds the path, so a naive reader would follow it.
        good.unlink()
        try:
            good.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        body = loader.load_skill("legit", project)
        assert (
            body is None or "SMUGGLED BODY" not in body
        ), "a cached path was followed out of the consented project"

    def test_metadata_scans_do_not_stat_a_swapped_project_link(
        self, project, tmp_path, monkeypatch
    ):
        """Metadata reaches the no-link reader before any path-following stat."""
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "legit"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            "---\nname: legit\ndescription: safe\ntriggers: launch\n---\n\nSAFE\n",
            encoding="utf-8",
        )
        outside = tmp_path / "outside.md"
        outside.write_text(
            "---\nname: stolen\ndescription: leaked\ntriggers: launch\n---\n\nOUTSIDE\n",
            encoding="utf-8",
        )

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)
        assert "legit" in {name for name, _, _ in loader._iter(project)}
        initial = next(skill for skill in loader.list_skills(project) if skill["key"] == "legit")
        assert initial["size_bytes"] == skill_file.stat().st_size

        skill_file.unlink()
        try:
            skill_file.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        original_stat = Path.stat

        def guarded_stat(path, *args, **kwargs):
            if path == skill_file:
                raise AssertionError("confined metadata followed the cached path with stat")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", guarded_stat)

        assert loader.get_triggered_skills("launch", project) == []
        row = next(skill for skill in loader.list_skills(project) if skill["key"] == "legit")
        assert row["name"] == "legit"
        assert row["description"] == "legit"

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="POSIX mode bits; the Windows ACL path applies a DACL in-process and is asserted there",
    )
    def test_the_trust_dir_is_owner_only_on_posix(self, project):
        """The directory, not just the store file, must be owner-only.

        A group- or world-writable trust DIRECTORY lets another local account
        replace the store wholesale, which forges consent this gate then
        enforces. On Windows the same lockdown runs as a real owner-only DACL --
        not assertable from this host, so it is not asserted here.
        """
        import os

        skill_trust.grant_project_trust(project)
        mode = os.stat(skill_trust.store_path().parent).st_mode & 0o777
        assert mode == 0o700, oct(mode)

    def test_a_swallowed_directory_lockdown_failure_is_retried_fail_loud(
        self, tmp_path, monkeypatch
    ):
        """Best-effort creation cannot authorize a permissive trust directory."""
        calls: list[Path] = []

        def create_without_lockdown(path):
            Path(path).mkdir(parents=True, exist_ok=True)

        def fail_lockdown(path):
            calls.append(Path(path))
            raise OSError("owner-only ACL unavailable")

        monkeypatch.setattr(platform_compat, "make_owner_only_dir", create_without_lockdown)
        monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", fail_lockdown)

        with pytest.raises(OSError, match="owner-only ACL unavailable"):
            skill_trust._trust_dir()

        assert calls == [skill_trust.config_dir() / skill_trust._TRUST_SUBDIR]


class TestOversizedProjectSkillIsSkipped:
    """An oversized SKILL.md must not abort the chat turn."""

    def test_an_oversized_project_skill_yields_no_body_instead_of_raising(
        self, project, tmp_path, monkeypatch
    ):
        """FileTooLargeError subclasses Exception, not OSError.

        So the reader's own OSError guard misses it, and on the context-assembly
        path nothing catches it until the turn's outermost handler -- the message
        never reaches the model and the slot shows an error card on EVERY turn,
        since a pinned body is re-read each time. The global skills path applies
        no cap at all and cannot abort a turn, so skipping is the closer parity.
        """
        from kiro_crew import hooks
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "huge"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: huge\ndescription: d\n---\n\n" + ("x" * 4096), encoding="utf-8"
        )

        # Shrink the cap rather than writing 50 MiB to disk.
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 128)

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        # No exception, and no body -- the shape every caller already handles.
        assert loader.load_skill("huge", project) is None


class TestEnumeratedMetadataIsAlsoConfined:
    """The metadata read is confined too, not just the body read."""

    def _swapped_project(self, project, tmp_path):
        """A project whose vetted SKILL.md is replaced by a link out of it."""
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "legit"
        skill_dir.mkdir(parents=True)
        good = skill_dir / "SKILL.md"
        good.write_text(
            "---\nname: legit\ndescription: HONEST DESCRIPTION\n---\n\nbody\n",
            encoding="utf-8",
        )
        outside = tmp_path / "outside.md"
        outside.write_text(
            "---\nname: legit\ndescription: SMUGGLED DESCRIPTION\n"
            "always: true\ntriggers: everything\n---\n\nsmuggled\n",
            encoding="utf-8",
        )

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        # Warm the enumeration while the file is still genuine.
        assert "legit" in {n for n, _, _ in loader._iter(project)}
        assert (
            loader._cached_frontmatter(good, within=skill_trust.canonical_key(project)).get(
                "description"
            )
            == "HONEST DESCRIPTION"
        )

        good.unlink()
        try:
            good.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        # Drop only the mtime-keyed metadata cache: the ENUMERATION stays warm,
        # which is precisely the window this test is about.
        loader._fm_cache.clear()
        # The vetted root is now an explicit argument, so hand it back rather than
        # letting a caller default it to None and silently test the unconfined path.
        return loader, good, skill_trust.canonical_key(project)

    def test_a_swapped_file_yields_no_metadata(self, project, tmp_path):
        loader, good, root = self._swapped_project(project, tmp_path)
        meta = loader._cached_frontmatter(good, within=root)
        assert "SMUGGLED DESCRIPTION" not in str(meta), meta
        assert meta == {}, meta

    def test_a_swapped_file_cannot_inject_a_description_or_force_always(self, project, tmp_path):
        """The consequence, not just the mechanism.

        `description` is rendered verbatim into the injected skills index and shown
        in the picker; `always: true` promotes a skill to full-body injection on
        every turn. Both are attacker-chosen if the metadata read is unconfined.
        """
        loader, _good, _root = self._swapped_project(project, tmp_path)

        rows = loader.list_skills(project_dir=str(project))
        blob = str(rows)
        assert "SMUGGLED DESCRIPTION" not in blob, blob
        assert "HONEST DESCRIPTION" not in blob or True  # the row may be dropped entirely

        always = loader.get_always_skills(project_dir=str(project))
        assert "SMUGGLED DESCRIPTION" not in str(always)


class TestConfinementMapSharesTheCacheLifetime:
    """The recorded roots are dropped with the caches they belong to."""

    def test_invalidating_the_caches_clears_the_recorded_roots(self, project, tmp_path):
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "legit"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: legit\ndescription: d\n---\n\nbody\n", encoding="utf-8"
        )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        assert "legit" in {n for n, _, _ in loader._iter(project)}

        loader._invalidate_iter_cache()
        assert loader._iter_cache == {}
        assert not loader._fm_cache


class TestEveryEnumeratedPathHasARecordedRoot:
    """The producer's contract: every enumerated item carries its vetted root.

    This began as "no path escapes the confinement map". The map is gone: the root
    is the third element of the item, so a path cannot arrive without one and a
    lookup cannot miss. What remains checkable -- and what can still go wrong -- is
    whether the producer LABELS each item correctly, which is what these assert.

    Two bugs sat here before the restructure: the root was filed under the
    pre-resolution path string while consumers got the post-resolution one (so the
    lookup missed), and a miss read unconfined rather than refusing.
    """

    def test_every_enumerated_path_has_a_recorded_root(self, project, tmp_path):
        from kiro_crew.skills import SkillsLoader

        # A project skill AND a global one, so both branches are covered.
        proj_skill = project / ".kiro" / "skills" / "from-project"
        proj_skill.mkdir(parents=True)
        (proj_skill / "SKILL.md").write_text(
            "---\nname: from-project\ndescription: d\n---\n\nbody\n", encoding="utf-8"
        )
        home_skills = tmp_path / "home-skills"
        global_skill = home_skills / "from-global"
        global_skill.mkdir(parents=True)
        (global_skill / "SKILL.md").write_text(
            "---\nname: from-global\ndescription: d\n---\n\nbody\n", encoding="utf-8"
        )

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=home_skills, install_builtins=False)

        items = loader._iter(project)
        names = {n for n, _, _ in items}
        assert {"from-project", "from-global"} <= names, names

        project_key = skill_trust.canonical_key(project)
        # Every item carries a root -- three elements, always. A path cannot
        # arrive without one, which is what the old side map allowed.
        assert all(len(item) == 3 for item in items), items
        roots = {n: root for n, _pth, root in items}

        # The label must be RIGHT, not merely present: a project skill is confined
        # to the granted directory...
        assert roots["from-project"] == project_key, roots
        # ...and the operator-installed tree stays unconfined, so an app's
        # registered symlink into its own tree keeps resolving.
        assert roots["from-global"] is None, roots

    def test_an_in_project_symlinked_skill_directory_is_refused(self, project):
        """Project directory links are pruned even when their target stays inside."""
        from kiro_crew.skills import SkillsLoader

        # The real skill lives outside .kiro/skills but inside the project.
        real = project / "shared" / "aliased"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text(
            "---\nname: aliased\ndescription: d\n---\n\nALIASED BODY\n", encoding="utf-8"
        )
        skills_root = project / ".kiro" / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        try:
            (skills_root / "aliased").symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=project / "unused-home-skills", install_builtins=False)

        assert "aliased" not in {n for n, _, _ in loader._iter(project)}
        assert loader.load_skill("aliased", project) is None


class TestOneEnforcementPointForEnumeratedReads:
    """Guard: both readers go through the choke point, and neither reads directly.

    Hardening the body read alone would leave the metadata read of the same cached
    paths unchecked. This fails the build if they drift apart.
    """

    def test_enumerated_readers_route_through_the_choke_point(self):
        import ast
        import pathlib

        src_dir = pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew"
        tree = ast.parse((src_dir / "skills_body_reads.py").read_text(encoding="utf-8"))

        wanted = {"_cached_frontmatter", "load_skill", "_read_enumerated_skill_bytes"}
        found: dict[str, ast.FunctionDef] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in wanted:
                found[node.name] = node

        # Canary: a rename or a moved file must fail loudly, not pass silently.
        assert set(found) == wanted, f"missing {sorted(wanted - set(found))} in skills_body_reads"

        def calls(fn):
            out = []
            for n in ast.walk(fn):
                if isinstance(n, ast.Call):
                    f = n.func
                    if isinstance(f, ast.Attribute):
                        out.append((f.attr, n.lineno))
                    elif isinstance(f, ast.Name):
                        out.append((f.id, n.lineno))
            return out

        offenders: list[str] = []

        for name in ("_cached_frontmatter", "load_skill"):
            names = [c for c, _ in calls(found[name])]
            if "_read_enumerated_skill_bytes" not in names:
                offenders.append(f"{name}() no longer reads through _read_enumerated_skill_bytes")

        # The metadata reader must not read the file itself. load_skill is NOT
        # swept for this: its global-skills and extra-path branches read directly
        # and are legitimately exempt from project confinement.
        for call, lineno in calls(found["_cached_frontmatter"]):
            if call in {"read_text", "read_bytes", "_parse_frontmatter"}:
                offenders.append(f"_cached_frontmatter():{lineno} reads directly via {call}()")

        # The enforcement itself must live in exactly one place.
        choke_names = [c for c, _ in calls(found["_read_enumerated_skill_bytes"])]
        if "safe_read_file_bytes_nolink" not in choke_names:
            offenders.append(
                "_read_enumerated_skill_bytes() no longer calls " "safe_read_file_bytes_nolink"
            )

        assert offenders == [], (
            "enumerated skill reads must all go through "
            "SkillsLoader._read_enumerated_skill_bytes: " + "; ".join(offenders)
        )

    def test_a_confined_refusal_is_not_reopened(self, tmp_path, monkeypatch):
        """A replaced project file must degrade without a second failing open."""
        from kiro_crew import skills as skills_mod
        from kiro_crew.skills import SkillsLoader

        monkeypatch.setattr(skills_mod, "safe_read_file_bytes_nolink", lambda *_a, **_kw: None)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        assert (
            loader._read_enumerated_skill_bytes(tmp_path / "vanished" / "SKILL.md", str(tmp_path))
            is None
        )


class TestCrlfSkillFilesParseLikeTextMode:
    """CRLF frontmatter must parse, because git checks out CRLF on Windows.

    These reads moved from `read_text` (TEXT mode, universal newlines) to bytes so
    containment could be checked on the descriptor actually opened. Bytes reads do
    not fold newlines, so every frontmatter key arrived with a trailing `\r`,
    nothing matched `always` or `pinned`, and skill bodies stopped being injected
    into context on Windows -- while Linux and macOS gates stayed green.

    Written with explicit CRLF bytes rather than a Windows-only skip, so the trap
    is reproducible on any platform.
    """

    def _crlf_project_skill(self, project, tmp_path):
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "crlf"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(
            b"---\r\nname: crlf\r\ndescription: has carriage returns\r\n"
            b"always: true\r\n---\r\n\r\nCRLF BODY\r\n"
        )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)
        return loader, skill_dir / "SKILL.md", skill_trust.canonical_key(project)

    def test_crlf_frontmatter_keys_are_not_left_with_carriage_returns(self, project, tmp_path):
        loader, path, root = self._crlf_project_skill(project, tmp_path)
        meta = loader._cached_frontmatter(path, within=root)

        assert meta.get("name") == "crlf", meta
        assert meta.get("always") == "true", meta
        # The specific corruption: a trailing \r on any value.
        assert not any("\r" in str(v) for v in meta.values()), meta

    def test_a_crlf_skill_is_still_recognised_as_always_on(self, project, tmp_path):
        """The consequence, not just the parse.

        `always: true` is what promotes a skill to full-body injection. With a
        stray \r the value never equals "true", so the body silently stops being
        injected -- which is exactly what the Windows shards reported.
        """
        loader, _path, _root = self._crlf_project_skill(project, tmp_path)
        always = loader.get_always_skills(project_dir=str(project))
        assert "crlf" in str(always), always

    def test_lone_carriage_returns_are_folded_too(self, project, tmp_path):
        """TEXT-mode reads translate a lone \r as well, so the fold must match.

        Without it a classic-Mac-ending SKILL.md parses as a single line and no
        frontmatter key is found at column 0 -- the same silent outcome as the
        CRLF case, reached a different way.
        """
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "crmac"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(
            b"---\rname: crmac\rdescription: lone carriage returns\r---\r\rCR BODY\r"
        )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        meta = loader._cached_frontmatter(
            skill_dir / "SKILL.md", within=skill_trust.canonical_key(project)
        )
        assert meta.get("name") == "crmac", meta
        assert not any("\r" in str(v) for v in meta.values()), meta

    def test_a_crlf_body_loads_without_carriage_returns(self, project, tmp_path):
        loader, _path, _root = self._crlf_project_skill(project, tmp_path)
        body = loader.load_skill("crlf", project) or ""
        assert "CRLF BODY" in body
        assert "\r" not in body


class TestProjectSkillDecodeFailures:
    def test_invalid_utf8_metadata_does_not_abort_project_skill_listing(self, project, tmp_path):
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "invalid-utf8"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(
            b"---\nname: invalid-\xff\ndescription: still listable\n---\nbody\n"
        )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        rows = loader.list_skills(project)

        assert any(row["key"] == "invalid-utf8" for row in rows)


class TestEnforcementIsAudited:
    """Using the grant is recorded, not just giving it."""

    @staticmethod
    def _recorder(monkeypatch):
        from kiro_crew import skills as skills_mod

        calls: list[dict] = []

        class _Sel:
            def log_governance_decision(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(skills_mod, "sel", lambda: _Sel())
        return calls

    def test_admitting_a_trusted_project_is_audited(self, project, tmp_path, monkeypatch):
        from kiro_crew.skills import SkillsLoader

        calls = self._recorder(monkeypatch)
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        loader._iter(project)

        governance = [c for c in calls if c.get("rule") == "project_skills_trust_enforced"]
        assert governance, calls
        assert governance[0]["outcome"] == "allowed"
        assert governance[0]["scope"] == "project_skills"
        # A record, not a gate: an unwritable SEL must not fail a chat turn.
        assert governance[0]["critical"] is False

    def test_withholding_an_untrusted_project_is_audited(self, tmp_path, monkeypatch):
        """The half an operator debugging a dead `$token` actually needs."""
        from kiro_crew.skills import SkillsLoader

        calls = self._recorder(monkeypatch)
        untrusted = tmp_path / "no-grant"
        (untrusted / ".kiro" / "skills").mkdir(parents=True)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        loader._iter(untrusted)

        governance = [c for c in calls if c.get("rule") == "project_skills_trust_enforced"]
        assert governance, calls
        assert governance[0]["outcome"] == "denied"

    def test_the_decision_is_audited_once_not_once_per_message(
        self, project, tmp_path, monkeypatch
    ):
        """This is what makes auditing a per-message path affordable.

        `_trusted_project_key` runs on every message via `get_triggered_skills`. One
        governance event per message would bury the events that matter and add
        hot-path cost on a path that runs on every message, so the record is
        written on first use per (directory, outcome).
        """
        from kiro_crew.skills import SkillsLoader

        calls = self._recorder(monkeypatch)
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        for _ in range(5):
            loader._iter(project)
            loader._iter_cache = {}  # force re-enforcement, not a cache hit

        governance = [c for c in calls if c.get("rule") == "project_skills_trust_enforced"]
        assert len(governance) == 1, (
            f"expected one enforcement record, got {len(governance)} -- the "
            "de-duplication is what keeps this off the per-message cost path"
        )

    def test_an_audit_failure_does_not_break_loading(self, project, tmp_path, monkeypatch):
        """A record must never be able to stop a turn."""
        from kiro_crew import skills as skills_mod
        from kiro_crew.skills import SkillsLoader

        class _Boom:
            def log_governance_decision(self, **_kwargs):
                raise OSError("SEL is read-only")

        monkeypatch.setattr(skills_mod, "sel", lambda: _Boom())
        skill_trust.grant_project_trust(project)
        skill_dir = project / ".kiro" / "skills" / "still-loads"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: still-loads\ndescription: d\n---\n\nBODY\n", encoding="utf-8"
        )
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        assert "still-loads" in {n for n, _, _ in loader._iter(project)}

    def test_a_failed_audit_is_retried_until_it_is_recorded(self, project, tmp_path, monkeypatch):
        """A transient SEL failure must not suppress the enforcement record forever."""
        from kiro_crew import skills as skills_mod
        from kiro_crew.skills import SkillsLoader

        attempts = 0

        class _FlakySel:
            def log_governance_decision(self, **_kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("SEL is temporarily read-only")

        monkeypatch.setattr(skills_mod, "sel", lambda: _FlakySel())
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        for _ in range(3):
            loader._iter(project)
            loader._iter_cache = {}

        assert attempts == 2, (
            "the failed write must retry on the next enforcement, while the "
            "successful write remains de-duplicated"
        )


class TestTriggeredProjectSkillsReachThePrompt:
    """A match must produce a body (or a pointer), not just a log line."""

    def _project_with_triggered_skill(self, project, tmp_path, *, inject: bool):
        from kiro_crew.skills import SkillsLoader

        opt_out = "" if inject else "inject_on_trigger: false\n"
        skill_dir = project / ".kiro" / "skills" / "deploys"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: deploys\ndescription: how we deploy\n"
            f"triggers: deploy release\n{opt_out}---\n\nDEPLOY RUNBOOK BODY\n",
            encoding="utf-8",
        )
        skill_trust.grant_project_trust(project)
        # max_triggered defaults to 0 -- trigger matching is OFF unless the
        # operator enables it, and with 0 the scored list is sliced to nothing.
        # So this feature is only reachable at all once it is switched on.
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = SkillsLoader(
            skills_path=tmp_path / "home-skills", install_builtins=False, config=cfg
        )
        return loader

    def test_a_matched_project_skill_body_is_loadable(self, project, tmp_path):
        """The body half: `load_skill` was called project-blind and returned None."""
        loader = self._project_with_triggered_skill(project, tmp_path, inject=True)

        triggered = loader.get_triggered_skills("please deploy the release", project_dir=project)
        assert "deploys" in triggered, triggered

        enforced, pointer_only = loader.split_triggered(triggered, project)
        assert "deploys" in enforced, (enforced, pointer_only)

        # The step that silently did nothing.
        body = loader.load_skill("deploys", project)
        assert body is not None and "DEPLOY RUNBOOK BODY" in body, body

    def test_a_pointer_only_project_skill_is_forced_into_the_body(self, project, tmp_path):
        """A confined skill must never hand the agent a mutable filesystem pointer."""
        loader = self._project_with_triggered_skill(project, tmp_path, inject=False)

        triggered = loader.get_triggered_skills("please deploy the release", project_dir=project)
        assert "deploys" in triggered, triggered

        enforced, pointer_only = loader.split_triggered(triggered, project)
        assert enforced == ["deploys"]
        assert pointer_only == []
        listed = next(row for row in loader.list_skills(project) if row["key"] == "deploys")
        cataloged = next(row for row in loader.catalog_project_skills(project))
        assert listed["inject_on_trigger"] is True
        assert cataloged["inject_on_trigger"] is True

        # Defense in depth: even a caller that bypasses the split cannot render
        # a project path for the agent to follow outside the confined reader.
        assert loader.trigger_hint(["deploys"], project) == ""

    def test_ownership_never_reads_a_project_skill_unconfined(self, project, tmp_path, monkeypatch):
        """A repository's skill belongs to its repository, so the disabled-app check
        must not read its frontmatter: that read would be unconfined."""
        import kiro_crew.apps.manager as mgr

        loader = self._project_with_triggered_skill(project, tmp_path, inject=True)
        skill_md = project / ".kiro" / "skills" / "deploys" / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8")
        skill_md.write_text(text.replace("name: deploys\n", "name: deploys\napp: papyrus\n"))
        monkeypatch.setattr(mgr, "list_apps", lambda: [{"name": "papyrus", "enabled": False}])
        unconfined = []
        vet = loader._vet_unconfined_path
        monkeypatch.setattr(
            loader, "_vet_unconfined_path", lambda path: unconfined.append(path) or vet(path)
        )

        triggered = loader.get_triggered_skills("please deploy the release", project_dir=project)

        assert (triggered, unconfined) == (["deploys"], [])

    def test_an_untrusted_project_skill_still_contributes_nothing(self, tmp_path):
        """Wiring the project through must not bypass consent."""
        from kiro_crew.skills import SkillsLoader

        untrusted = tmp_path / "no-grant"
        skill_dir = untrusted / ".kiro" / "skills" / "deploys"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: deploys\ndescription: d\ntriggers: deploy release\n---\n\nSECRET\n",
            encoding="utf-8",
        )
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = SkillsLoader(
            skills_path=tmp_path / "home-skills", install_builtins=False, config=cfg
        )

        # Empty because the project is UNTRUSTED, not because triggering is off --
        # the config above rules that explanation out.
        assert loader.get_triggered_skills("please deploy the release", project_dir=untrusted) == []
        assert loader.load_skill("deploys", untrusted) is None
        assert "SECRET" not in loader.trigger_hint(["deploys"], untrusted)


class TestProjectSkillsIndexConfinement:
    """The initial skills context must not hand project paths to the agent."""

    @pytest.mark.parametrize("budget", [None, 100_000])
    def test_on_demand_project_skill_is_injected_without_a_path(self, project, tmp_path, budget):
        from kiro_crew.skills import SkillsLoader

        skill_file = project / ".kiro" / "skills" / "release" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text(
            "---\nname: release\ndescription: release procedure\n---\n\n" "CONFINED RELEASE BODY\n",
            encoding="utf-8",
        )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        context = loader.get_context(budget=budget, project_dir=project)

        assert ("CONFINED RELEASE BODY" in context) is (budget is None)
        assert str(skill_file) not in context
        assert "CONFINED RELEASE BODY" in (
            loader.read_scoped_skill("release", project_dir=project) or ""
        )

    def test_project_bodies_stop_at_the_skills_section_budget(self, project, tmp_path):
        """Many large confined bodies must not be accumulated before truncation."""
        from kiro_crew.skills import SkillsLoader

        for name in ("alpha", "beta", "gamma"):
            skill_file = project / ".kiro" / "skills" / name / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text(
                f"---\nname: {name}\ndescription: d\n---\n\n" + (name * 50),
                encoding="utf-8",
            )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)
        original_load = loader.load_skill
        reads: list[tuple[str, int | None]] = []

        def recording_load(
            name: str, project_dir=None, *, max_bytes: int | None = None
        ) -> str | None:
            reads.append((name, max_bytes))
            return original_load(name, project_dir, max_bytes=max_bytes)

        loader.load_skill = recording_load  # type: ignore[method-assign]

        context = loader.get_context(
            budget=None,
            project_body_budget=400,
            project_dir=project,
        )

        assert len(context) <= 400
        assert reads
        assert all(limit is not None and limit <= 400 for _name, limit in reads)
        assert len(reads) < 3, "the loader kept reading after the section budget was exhausted"
