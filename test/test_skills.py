"""Tests for skills module."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.skill_usage import SkillUsageLedger
from kiro_crew.skills import _NEW_SKILL_BOOST_WINDOW_SECS, _SHORT_DESC_CHARS, SkillsLoader
from kiro_crew.testing.wait import wait_until


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """Release every loader this module builds, with its index and its worker.

    Settling a loader is what starts its ``skill-catalog-refresh`` worker and
    opens its SQLite index, and the tests that only reach a settled loader
    through a shared ``_loader`` helper never close one. Measured over this file
    at ``-n0``: 113 parked workers and 348 descriptors at the end without it, 0
    and 14 with it.
    """


@pytest.fixture(autouse=True)
def _isolate_extra_paths(monkeypatch, tmp_path_factory):
    """SkillsLoader.__init__ reads global config for ``skills.extra_paths`` and
    edition-contributed skill roots via the ``extra_skills()`` seam; isolate both
    so a developer's local ~/.kirocrew extra_paths / composed companion roots
    don't bleed into these hermetic loader tests. Tests that need extra_paths
    pass ``config=``; tests that need edition-root resolution monkeypatch
    ``DefaultMcpToolingProvider.extra_skills``."""
    from kiro_crew.platform.defaults import DefaultMcpToolingProvider

    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        classmethod(lambda cls: KiroCrewConfig(skills=SkillsConfig(extra_paths=[]))),
    )
    monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [])


def _create_skill(skills_dir, name, content):
    """Helper to create a skill directory with SKILL.md."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content)


#: Lost-run bound on one discovery walk of these few-file trees, which takes
#: milliseconds. It only ends a wedged walk, so it sits far above the loader's
#: own two-second cold-start budget and under half the suite's 120 s timeout.
_CATALOG_SETTLE_SECS = 30.0


def _settled(loader: SkillsLoader) -> SkillsLoader:
    """Return *loader* once its catalog lists the skills tree as it is now.

    A fresh skills root has no listed entry to serve, and neither has a root
    just after a mutation, so the loader's next enumeration waits at most
    ``_COLD_CATALOG_WAIT_SECS`` (2 s) for the background walk and then answers
    ``[]`` with the scope marked ``"building"``. That is by design: a live turn
    never stalls on discovery. A test is not a live turn: on a loaded Windows
    runner the walk outlasts the budget, and then a positive assertion reads an
    empty catalog and a negative one passes for the trivial reason.

    ``_iter`` arms the walk without parsing a body or logging anything a test
    counts, and the poll ends when ``catalog_status`` says the list is complete.
    """

    def _listed() -> bool:
        loader._iter()
        return loader.catalog_status() == "complete"

    wait_until(
        _listed,
        timeout=_CATALOG_SETTLE_SECS,
        describe=lambda: f"catalog_status={loader.catalog_status()!r}",
    )
    return loader


class TestNoteToolRead:
    """Only content-delivering reads credit the ledger."""

    def _loader(self, tmp_path, opened=None):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "alpha", "---\nname: alpha\ndescription: A\n---\n# Alpha\n")
        _create_skill(skills_dir, "beta", "---\nname: beta\ndescription: B\n---\n# Beta\n")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        if opened is not None:
            # Registered before the settle: a settle that times out raises, and a
            # loader registered after it would never be closed.
            opened(loader)
        _settled(loader)
        loader._usage = SkillUsageLedger(tmp_path / "skill-usage.json")
        # An armed debounce keeps a credit from starting the background flush
        # thread, which would otherwise write after the test ends.
        loader._usage._last_flush = time.time()
        return loader, skills_dir

    def _read(self, loader, **kw):
        """Resolve then credit, the way the ACP layer does across two events."""
        keys = loader.resolve_tool_read_keys(**kw)
        loader.credit_skill_reads(keys)
        return keys

    def test_read_tool_path_credits_a_hit(self, tmp_path):
        loader, skills_dir = self._loader(tmp_path)
        path = str(skills_dir / "alpha" / "SKILL.md")
        assert self._read(loader, tool_name="fs_read", raw_params={"path": path}) == ["alpha"]
        assert loader._usage.score("alpha")[0] == 1.0
        assert loader._usage.score("beta")[0] == 0.0

    def test_kiro_read_tool_batched_line_read_credits_a_hit(self, tmp_path, opened):
        # kiro-cli's `read` batches its targets under `operations`, and it is
        # how the model loads most skills.
        loader, skills_dir = self._loader(tmp_path, opened)
        params = {
            "operations": [
                {"mode": "Line", "path": "/etc/hosts"},
                {"mode": "Line", "path": str(skills_dir / "alpha" / "SKILL.md"), "limit": 40},
            ]
        }
        assert self._read(loader, tool_name="read", raw_params=params) == ["alpha"]
        assert loader._usage.score("alpha")[0] == 1.0

    def test_kiro_read_tool_non_line_operations_are_not_credited(self, tmp_path, opened):
        # Directory mode lists names and Image mode reads images: neither returns
        # the body, and a write tool with the same shape is not a read at all.
        loader, skills_dir = self._loader(tmp_path, opened)
        path = str(skills_dir / "alpha" / "SKILL.md")
        for tool_name, params in (
            ("read", {"operations": [{"mode": "Directory", "path": path}]}),
            ("read", {"operations": [{"mode": "Image", "image_paths": [path]}]}),
            ("write", {"operations": [{"mode": "Line", "path": path}]}),
        ):
            assert loader.resolve_tool_read_keys(tool_name, params) == [], (tool_name, params)
        assert loader._usage.snapshot() == {}

    def test_shell_cat_credits_a_hit(self, tmp_path):
        loader, skills_dir = self._loader(tmp_path)
        cmd = f"cat {skills_dir / 'beta' / 'SKILL.md'}"
        assert self._read(loader, command=cmd) == ["beta"]
        assert loader._usage.score("beta")[0] == 1.0

    def test_resolution_alone_records_nothing(self, tmp_path):
        # The ACP layer resolves at call time and credits only once the tool
        # reports completion, so resolving must have no side effect.
        loader, skills_dir = self._loader(tmp_path)
        cmd = f"cat {skills_dir / 'alpha' / 'SKILL.md'}"
        assert loader.resolve_tool_read_keys(command=cmd) == ["alpha"]
        assert loader._usage.snapshot() == {}

    def test_one_command_naming_a_file_twice_counts_once(self, tmp_path):
        loader, skills_dir = self._loader(tmp_path)
        p = skills_dir / "alpha" / "SKILL.md"
        assert self._read(loader, command=f"cat {p} && cat {p}") == ["alpha"]
        assert loader._usage.score("alpha")[0] == 1.0

    def test_non_read_shell_verbs_are_not_credited(self, tmp_path):
        # A tool call that merely NAMES a skill is not a delivery. Crediting it
        # would be the same mention-as-use conflation the searches tally avoids:
        # a maintenance session could push an unread skill up the ranking.
        loader, skills_dir = self._loader(tmp_path)
        p = skills_dir / "alpha" / "SKILL.md"
        for cmd in (
            f"rm {p}",
            f"mv {p} /tmp/x",
            f"wc -l {p}",
            f"grep -l foo {p}",
            f"chmod 600 {p}",
            f"cp {p} /tmp/x",
            f"stat {p}",
        ):
            assert loader.resolve_tool_read_keys(command=cmd) == [], cmd
        assert loader._usage.snapshot() == {}

    def test_a_read_verb_in_another_segment_does_not_launder_a_delete(self, tmp_path):
        # `cat` applies to its OWN segment only; without per-segment verb
        # attribution this would credit the deleted skill.
        loader, skills_dir = self._loader(tmp_path)
        p = skills_dir / "alpha" / "SKILL.md"
        assert loader.resolve_tool_read_keys(command=f"cat /etc/hosts && rm {p}") == []

    def test_read_verb_variants_are_credited(self, tmp_path):
        loader, skills_dir = self._loader(tmp_path)
        p = skills_dir / "beta" / "SKILL.md"
        for cmd in (f"head -20 {p}", f"tail -5 {p}", f"/bin/cat {p}", f"LC_ALL=C cat {p}"):
            assert loader.resolve_tool_read_keys(command=cmd) == ["beta"], cmd

    def test_non_read_tool_name_is_not_credited(self, tmp_path):
        # Structured tools are allowlisted: an edit/write tool carrying a path
        # must not be mistaken for a delivery.
        loader, skills_dir = self._loader(tmp_path)
        path = str(skills_dir / "alpha" / "SKILL.md")
        assert loader.resolve_tool_read_keys("fs_write", {"path": path}) == []
        assert loader.resolve_tool_read_keys("grep", {"path": path}) == []
        assert loader.resolve_tool_read_keys("fs_read", {"path": path}) == ["alpha"]

    def test_unrelated_tool_call_records_nothing(self, tmp_path):
        loader, _ = self._loader(tmp_path)
        assert self._read(loader, tool_name="fs_read", raw_params={"path": "/etc/hosts"}) == []
        assert self._read(loader, command="ls -la /tmp") == []
        assert loader._usage.snapshot() == {}

    def test_path_outside_the_skills_tree_is_not_credited(self, tmp_path):
        # A file that merely shares the basename must not be attributed to a skill.
        decoy = tmp_path / "elsewhere"
        decoy.mkdir()
        (decoy / "SKILL.md").write_text("---\nname: fake\n---\n")
        loader, _ = self._loader(tmp_path)
        assert loader.resolve_tool_read_keys("fs_read", {"path": str(decoy / "SKILL.md")}) == []

    def test_malformed_params_do_not_raise(self, tmp_path):
        loader, _ = self._loader(tmp_path)
        assert loader.resolve_tool_read_keys("fs_read", {"path": 42}) == []
        assert loader.resolve_tool_read_keys("fs_read", {"paths": [None, 7]}) == []
        assert loader.resolve_tool_read_keys("fs_read", "not-a-dict") == []  # type: ignore[arg-type]

    def test_read_without_a_ledger_is_a_noop(self, tmp_path):
        loader, skills_dir = self._loader(tmp_path)
        loader._usage = None
        path = str(skills_dir / "alpha" / "SKILL.md")
        assert loader.resolve_tool_read_keys("fs_read", {"path": path}) == []

    def test_symlinked_skill_credits_the_canonical_key(self, tmp_path):
        # Reading through a symlinked skill must credit the same key the budget
        # screen shows, or one file's cost splits across two rows.
        loader, skills_dir = self._loader(tmp_path)
        link_dir = skills_dir / "alpha-alias"
        link_dir.mkdir()
        try:
            (link_dir / "SKILL.md").symlink_to(skills_dir / "alpha" / "SKILL.md")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        loader._invalidate_iter_cache()  # re-walk so the alias is served
        _settled(loader)
        recorded = self._read(
            loader, tool_name="fs_read", raw_params={"path": str(link_dir / "SKILL.md")}
        )
        canonical = loader._served_key_by_realpath()[
            str((skills_dir / "alpha" / "SKILL.md").resolve())
        ]
        assert recorded == [canonical] == ["alpha"]


class TestBoundedSkillReads:
    @pytest.mark.parametrize("extra", [False, True], ids=["local", "extra"])
    def test_complete_body_fits_exact_byte_bound(self, tmp_path, extra):
        root = tmp_path / "installed"
        _create_skill(root, "alpha", "placeholder")
        path = root / "alpha" / "SKILL.md"
        raw = "Instructions café".encode("utf-8")
        path.write_bytes(raw)
        loader = SkillsLoader(
            skills_path=tmp_path / "local" if extra else root,
            install_builtins=False,
            config=KiroCrewConfig(skills=SkillsConfig(extra_paths=[str(root)] if extra else [])),
        )

        assert loader.load_skill("alpha", max_bytes=len(raw)) == raw.decode("utf-8")
        assert loader.load_skill("alpha", max_bytes=len(raw) - 1) is None

    def test_unbounded_global_read_keeps_strict_utf8(self, tmp_path):
        root = tmp_path / "skills"
        _create_skill(root, "alpha", "placeholder")
        (root / "alpha" / "SKILL.md").write_bytes(b"instructions\xff")
        loader = SkillsLoader(skills_path=root, install_builtins=False)

        with pytest.raises(UnicodeDecodeError):
            loader.load_skill("alpha")

    def test_global_link_to_sensitive_directory_is_refused(self, tmp_path, monkeypatch):
        from conftest import make_dir_link
        from kiro_crew.hooks import validate_file_path

        crew_home = tmp_path / "crew"
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        secret_dir = crew_home / ".vault"
        secret_dir.mkdir(parents=True)
        secret = secret_dir / "SKILL.md"
        secret.write_text("credential sentinel", encoding="utf-8")
        assert validate_file_path(str(secret)) is None
        root = tmp_path / "skills"
        root.mkdir()
        make_dir_link(root / "alpha", secret_dir)
        loader = SkillsLoader(skills_path=root, install_builtins=False)

        assert loader.load_skill("alpha", max_bytes=100) is None

    def test_extra_directory_swap_after_validation_is_refused(self, tmp_path, monkeypatch):
        from conftest import make_dir_link
        from kiro_crew import skills

        crew_home = tmp_path / "crew"
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        secret_dir = crew_home / ".vault"
        secret_dir.mkdir(parents=True)
        (secret_dir / "SKILL.md").write_text("credential sentinel", encoding="utf-8")
        extra = tmp_path / "extra"
        _create_skill(extra, "alpha", "safe instructions")
        loader = SkillsLoader(
            skills_path=tmp_path / "local",
            install_builtins=False,
            config=KiroCrewConfig(skills=SkillsConfig(extra_paths=[str(extra)])),
        )
        original_validate = skills.validate_file_path
        swapped = False

        def validate_then_swap(raw):
            nonlocal swapped
            resolved = original_validate(raw)
            assert resolved is not None
            (extra / "alpha").rename(extra / "original")
            make_dir_link(extra / "alpha", secret_dir)
            swapped = True
            return resolved

        monkeypatch.setattr(skills, "validate_file_path", validate_then_swap)

        assert loader.load_skill("alpha", max_bytes=100) is None
        assert swapped

    def test_hardlinked_body_is_refused(self, tmp_path):
        root = tmp_path / "skills"
        _create_skill(root, "alpha", "safe instructions")
        (root / "alias.md").hardlink_to(root / "alpha" / "SKILL.md")
        loader = SkillsLoader(skills_path=root, install_builtins=False)

        assert loader.load_skill("alpha", max_bytes=100) is None

    def test_validated_provider_directory_link_remains_readable(self, tmp_path):
        from conftest import make_dir_link

        provider = tmp_path / "provider"
        _create_skill(provider, "alpha", "safe instructions")
        root = tmp_path / "skills"
        root.mkdir()
        make_dir_link(root / "alpha", provider / "alpha")
        loader = SkillsLoader(skills_path=root, install_builtins=False)

        assert loader.load_skill("alpha", max_bytes=100) == "safe instructions"


class TestSkillsLoader:
    def test_list_empty(self, tmp_path):
        loader = _settled(SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False))
        assert loader.list_skills() == []

    def test_list_skills(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "weather",
            "---\nname: weather\ndescription: Get weather info\n---\n# Weather\n",
        )
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        skills = loader.list_skills()
        assert len(skills) == 1
        assert skills[0]["name"] == "weather"
        assert skills[0]["description"] == "Get weather info"

    def test_html_shaped_skill_is_skipped_and_warned_once(self, tmp_path, caplog):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "page", "\n  <!DOCTYPE html>\n<html><body>x</body></html>")
        _create_skill(skills_dir, "framed", "---\nname: framed\n---\n<HTML><p>x</p></HTML>")
        (skills_dir / "bom").mkdir()
        (skills_dir / "bom" / "SKILL.md").write_bytes(b"\xef\xbb\xbf<!doctype html><p>x</p>")
        _create_skill(skills_dir, "tagged", "<html-guide> is a markdown skill\n")
        _create_skill(skills_dir, "weather", "---\n_html: body\n---\n# Weather\n")
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        with caplog.at_level("WARNING", logger="kiro_crew.skills"):
            assert sorted(s["key"] for s in loader.list_skills()) == ["tagged", "weather"]
            loader.list_skills()
        warned = [r.getMessage() for r in caplog.records if "body is HTML" in r.getMessage()]
        assert len(warned) == 3
        assert any("page" in m for m in warned) and any("framed" in m for m in warned)
        assert loader.read_scoped_skill("page") is None
        assert loader.load_skill("framed") is None
        assert "Weather" in loader.read_scoped_skill("weather")

    def test_html_skill_in_an_index_warmed_before_the_marker_is_dropped(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import skill_search_index
        from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter

        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "page", "<!doctype html><p>x</p>")
        _create_skill(skills_dir, "notes", "# Notes\nplain markdown\n")
        with monkeypatch.context() as old:
            # Schema 5 and its parser are what shipped before the HTML marker.
            old.setattr(skill_search_index, "_SCHEMA_VERSION", 5)
            old.setattr(
                SkillsLoader,
                "_parse_frontmatter_text",
                staticmethod(lambda content: parse_frontmatter(content, SKILL_LOADER)),
            )
            warm = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
            assert sorted(s["key"] for s in warm.list_skills()) == ["notes", "page"]
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        assert [s["key"] for s in loader.list_skills()] == ["notes"]

    def test_load_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "test", "---\nname: test\n---\n# Test Skill\nDo stuff.")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        content = loader.load_skill("test")
        assert content is not None
        assert "Test Skill" in content

    def test_load_missing_skill(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.load_skill("nonexistent") is None

    def test_always_skills(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "memory",
            "---\nname: memory\ndescription: Memory system\nalways: true\n---\n# Memory\n",
        )
        _create_skill(
            skills_dir,
            "weather",
            "---\nname: weather\ndescription: Weather\n---\n# Weather\n",
        )
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        always = loader.get_always_skills()
        assert "memory" in always
        assert "weather" not in always

    def test_get_context(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "memory",
            "---\nname: memory\ndescription: Memory system\nalways: true\n---\n# Memory\nUse it.",
        )
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        ctx = loader.get_context()
        assert "[Skills:]" in ctx
        assert "Memory" in ctx
        assert "Use it." in ctx

    def test_get_context_empty(self, tmp_path):
        loader = _settled(SkillsLoader(skills_path=tmp_path / "empty", install_builtins=False))
        assert loader.get_context() == ""

    def test_mutators_invalidate_iter_cache(self, tmp_path):
        """create/update/delete must invalidate the TTL'd _iter cache so a
        subsequent list_skills() reflects the change immediately (not after the
        TTL). Each step primes the cache via list_skills() first, mutates, then
        re-lists in the same tick — without invalidation these would be stale."""
        loader = _settled(SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False))
        body = "---\nname: x\ndescription: X\n---\n# X\n"

        # create: prime empty, create, must appear now
        assert loader.list_skills() == []
        assert loader.create_skill("alpha", body) is True
        assert any(s["key"] == "alpha" for s in _settled(loader).list_skills())

        # update: prime, overwrite, new description must appear now
        assert loader.update_skill("alpha", body.replace("X", "Updated")) is True
        assert any(s["description"] == "Updated" for s in _settled(loader).list_skills())

        # delete: prime, remove, must disappear now
        assert loader.delete_skill("alpha") is True
        assert all(s["key"] != "alpha" for s in _settled(loader).list_skills())

    def test_update_reflected_when_mtime_unchanged(self, tmp_path):
        """Deterministic gate for the same-tick frontmatter-cache staleness the
        mutators guard against. list_skills() primes the mtime-keyed frontmatter
        cache; then update_skill overwrites the file and we PIN mtime back to the
        pre-update value (simulating a coarse- or same-tick filesystem write).
        Without _fm_cache invalidation the stale parse ('X') would survive even
        though the file on disk now says 'Updated'. Unlike
        test_mutators_invalidate_iter_cache, this does not depend on the host
        filesystem's mtime granularity."""
        import os

        loader = _settled(SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False))
        body = "---\nname: x\ndescription: X\n---\n# X\n"
        assert loader.create_skill("alpha", body) is True
        # Prime the frontmatter cache with description "X".
        assert any(s["description"] == "X" for s in _settled(loader).list_skills())
        skill_file = tmp_path / "skills" / "alpha" / "SKILL.md"
        pinned = skill_file.stat().st_mtime_ns

        assert loader.update_skill("alpha", body.replace("X", "Updated")) is True
        # Force the mtime back so an mtime-keyed cache cannot tell the file changed.
        os.utime(skill_file, ns=(pinned, pinned))
        assert any(s["description"] == "Updated" for s in _settled(loader).list_skills())

    def test_update_auto_skill_invalidates_cache_same_mtime(self, tmp_path):
        """update_auto_skill (the auto-skill refine path) must invalidate cached
        frontmatter too, or a refined skill's new triggers/description stay stale
        when the rewrite lands in the same mtime tick as the priming read."""
        import os

        from kiro_crew.skills import AutoSkillProvenance

        loader = _settled(SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False))
        prov = AutoSkillProvenance(session_key="s1", created_at="2026-01-01T00:00:00Z")
        name = loader.create_auto_skill(
            slug="alpha",
            description="Original",
            triggers="one, two",
            procedure_md="# Do it",
            provenance=prov,
        )
        assert name is not None
        # Prime the frontmatter cache with the original description.
        assert any(s["description"] == "Original" for s in _settled(loader).list_skills())
        skill_file = tmp_path / "skills" / name / "SKILL.md"
        pinned = skill_file.stat().st_mtime_ns

        assert (
            loader.update_auto_skill(
                name=name,
                description="Refined",
                triggers="three, four",
                procedure_md="# Do it better",
                provenance=prov,
            )
            is True
        )
        os.utime(skill_file, ns=(pinned, pinned))
        assert any(s["description"] == "Refined" for s in _settled(loader).list_skills())


class TestRepoScope:
    """``repo_scope:`` frontmatter mechanically suppresses a skill unless the
    SESSION's active project directory (or an ancestor) contains the named
    relative path — the loader-enforced gate for repo-specific skills with
    destructive instructions (prose scope guards are
    probabilistic; containment must be mechanical before shipping to every
    install).

    The gate is keyed on the project, never on the process working directory:
    this runs in the gateway while it assembles context, so ``Path.cwd()`` is
    the gateway's own directory and answers by install shape rather than by the
    work the session is doing."""

    def _write_skill(self, root: Path, name: str, scope: str | None, always: bool = False) -> None:
        d = root / name
        d.mkdir(parents=True)
        fm = f"---\nname: {name}\ntriggers: zebra quokka\n"
        if scope:
            fm += f"repo_scope: {scope}\n"
        if always:
            fm += "always: true\n"
        (d / "SKILL.md").write_text(fm + "---\nbody")

    def _repo(self, tmp_path: Path, name: str = "checkout") -> Path:
        repo = tmp_path / name
        (repo / "src" / "kiro_crew").mkdir(parents=True)
        # A real checkout carries a .git entry, and the gate reads it as the
        # boundary the ancestor walk stops at, so the fixture needs one to model a
        # repository rather than a bare directory tree.
        (repo / ".git").mkdir()
        return repo

    def _other_repo(self, tmp_path: Path, name: str) -> Path:
        """A project that is some OTHER repository, wherever ``tmp_path`` lives.

        The gate walks a project's ancestors up to the first ``.git``. A bare
        directory under ``tmp_path`` has no boundary of its own, so whether the
        walk finds ``src/kiro_crew`` above it is decided by the host: a harness that
        pins ``TMPDIR`` under this checkout puts the real one in every ancestor
        chain. Giving the project its own ``.git`` models what "outside the repo"
        means to the gate -- a different repository -- and stops the walk there.
        """
        other = tmp_path / name
        other.mkdir()
        (other / ".git").mkdir()
        return other

    def test_scoped_skill_suppressed_without_a_project(self, tmp_path: Path) -> None:
        # No project named at all -> fail closed. An un-scoped surface (eval
        # harness, a session with no project set) never inherits repo rules.
        skills = tmp_path / "skills"
        self._write_skill(skills, "repo-only", "src/kiro_crew")
        loader = SkillsLoader(skills_path=skills, install_builtins=False)
        assert loader.get_triggered_skills("zebra quokka") == []

    def test_scoped_skill_suppressed_outside_repo(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        self._write_skill(skills, "repo-only", "src/kiro_crew")
        loader = SkillsLoader(skills_path=skills, install_builtins=False)
        outside = self._other_repo(tmp_path, "elsewhere")
        assert loader.get_triggered_skills("zebra quokka", project_dir=str(outside)) == []

    def test_scoped_skill_eligible_inside_repo(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        self._write_skill(skills, "repo-only", "src/kiro_crew")
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills, install_builtins=False, config=cfg))
        subdir = self._repo(tmp_path) / "website"
        subdir.mkdir()
        # ancestor of the project dir contains src/kiro_crew
        assert loader.get_triggered_skills("zebra quokka", project_dir=str(subdir)) == ["repo-only"]

    def test_process_cwd_does_not_admit_the_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The regression this gate had: a gateway whose OWN cwd sits inside a
        # scoped checkout admitted the skill into every session, whatever the
        # session was working on. Standing in the repo must decide nothing.
        skills = tmp_path / "skills"
        self._write_skill(skills, "repo-only", "src/kiro_crew")
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills, install_builtins=False, config=cfg))
        monkeypatch.chdir(self._repo(tmp_path))
        other = self._other_repo(tmp_path, "some-rust-project")
        assert loader.get_triggered_skills("zebra quokka") == []
        assert loader.get_triggered_skills("zebra quokka", project_dir=str(other)) == []

    def test_always_path_is_gated_identically(self, tmp_path: Path) -> None:
        # Both injection paths share one helper; a pinned skill must not be a
        # way around the gate.
        skills = tmp_path / "skills"
        self._write_skill(skills, "repo-only", "src/kiro_crew", always=True)
        loader = _settled(SkillsLoader(skills_path=skills, install_builtins=False))
        other = self._other_repo(tmp_path, "some-rust-project")
        assert loader.get_always_skills() == []
        assert loader.get_always_skills(str(other)) == []
        assert loader.get_always_skills(str(self._repo(tmp_path))) == ["repo-only"]

    def test_unscoped_skill_unaffected(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        self._write_skill(skills, "anywhere", None)
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills, install_builtins=False, config=cfg))
        assert loader.get_triggered_skills("zebra quokka") == ["anywhere"]

    def test_traversal_scope_fails_closed(self, tmp_path: Path) -> None:
        # ".." in the scope must never widen the check — fails closed.
        skills = tmp_path / "skills"
        self._write_skill(skills, "sneaky", "../..")
        loader = SkillsLoader(skills_path=skills, install_builtins=False)
        assert loader.get_triggered_skills("zebra quokka", project_dir=str(tmp_path)) == []

    def test_scoped_skill_is_absent_from_the_index_too(self, tmp_path: Path) -> None:
        # Dropping only the BODY leaves the summary line in the skill index, and
        # the index tells the agent to read the full file for anything related -
        # so the repo-specific procedure stays one `cat` away.
        skills = tmp_path / "skills"
        self._write_skill(skills, "repo-only", "src/kiro_crew")
        self._write_skill(skills, "anywhere", None)
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills, install_builtins=False, config=cfg))
        other = self._other_repo(tmp_path, "some-rust-project")
        for block in (
            loader.get_context(project_dir=str(other)),
            loader.get_context(budget=100_000, project_dir=str(other)),
            loader.get_context(),
        ):
            assert "repo-only" not in block
            assert "anywhere" in block
        # ...and present once the project is the scoped tree.
        inside = loader.get_context(project_dir=str(self._repo(tmp_path)))
        assert "repo-only" in inside

    def test_kiro_crew_dev_skills_load_globally_and_say_so(self) -> None:
        # Maintainer decision: the Kiro Crew developer skills load in
        # every session like any other skill, so a Kiro Crew PR written from any
        # folder still gets them. The description is what keeps them out of other
        # repositories, so each one must state that it is for this repository.
        from kiro_crew import skills as skills_mod

        pkg = Path(skills_mod.__file__).parent
        expected = {
            pkg
            / "apps"
            / "builtins"
            / "dev_fleet"
            / "skills"
            / "pod-e2e": "ONLY for developing Kiro Crew itself",
            pkg / "builtin_skills" / "kirocrew-dev" / "kirocrew-prepare-pr": "Kiro Crew repo only",
            pkg
            / "builtin_skills"
            / "kirocrew-dev"
            / "kirocrew-worktree-dev": "Kiro Crew source repo ITSELF",
            pkg / "builtin_skills" / "kirocrew-dev" / "writing-tests": "Kiro Crew repo only",
            pkg / "builtin_skills" / "kirocrew-dev" / "dashboard-template": "Kiro Crew repo only",
        }
        for skill_dir, statement in expected.items():
            head = (skill_dir / "SKILL.md").read_text(encoding="utf-8")[:2048]
            frontmatter = head.split("---", 2)[1]
            assert "repo_scope" not in frontmatter, skill_dir.name
            assert statement in frontmatter, skill_dir.name


class TestRelocatedSkillCleanup:
    """Skills moved into skills/kirocrew-dev/ must have their old FLAT copies
    removed from loader discovery on existing installs — otherwise an upgrade
    leaves two divergent copies matched nondeterministically by trigger overlap
    (the dual-copy drift this prevents). The flat copy may carry
    USER EDITS (the mtime-preserving sync deliberately protects those), so it
    is never deleted: its SKILL.md is renamed to SKILL.md.pre-relocation —
    undiscoverable, but every byte preserved. Quarantine only happens when the
    nested replacement is verifiably present."""

    def test_flat_copy_quarantined_when_nested_present(self, tmp_path: Path) -> None:
        from kiro_crew.skills import _ensure_builtin_skills

        base = tmp_path / "skills"
        old = base / "prepare-pr"
        old.mkdir(parents=True)
        (old / "SKILL.md").write_text("---\nname: kirocrew-prepare-pr\n---\nUSER-EDITED flat copy")
        (old / "scripts").mkdir()
        (old / "scripts" / "helper.py").write_text("# user script")
        new = base / "kirocrew-dev" / "kirocrew-prepare-pr"
        new.mkdir(parents=True)
        (new / "SKILL.md").write_text("---\nname: kirocrew-prepare-pr\n---\nnested copy")

        _ensure_builtin_skills(base)

        # Not discoverable as a skill...
        assert not (old / "SKILL.md").exists()
        # ...but nothing was deleted: user edits and scripts preserved on disk.
        quarantined = old / "SKILL.md.pre-relocation"
        assert quarantined.read_text(encoding="utf-8").endswith("USER-EDITED flat copy")
        assert (old / "scripts" / "helper.py").exists()
        assert (new / "SKILL.md").exists()

    def test_renamed_nested_copy_quarantined_when_new_name_present(self, tmp_path):
        """An install that already holds ``kirocrew-dev/prepare-pr`` gets the
        renamed ``kirocrew-dev/kirocrew-prepare-pr``; the old nested copy is
        quarantined so the loader never sees two copies of the same skill."""
        from kiro_crew.skills import _ensure_builtin_skills

        base = tmp_path / "skills"
        old = base / "kirocrew-dev" / "prepare-pr"
        old.mkdir(parents=True)
        (old / "SKILL.md").write_text("---\nname: prepare-pr\n---\nold nested copy")
        new = base / "kirocrew-dev" / "kirocrew-prepare-pr"
        new.mkdir(parents=True)
        (new / "SKILL.md").write_text("---\nname: kirocrew-prepare-pr\n---\nrenamed copy")

        _ensure_builtin_skills(base)

        assert not (old / "SKILL.md").exists()
        assert (
            (old / "SKILL.md.pre-relocation")
            .read_text(encoding="utf-8")
            .endswith("old nested copy")
        )
        assert (new / "SKILL.md").exists()

    def test_linked_old_dir_is_never_quarantined_through_the_link(self, tmp_path: Path) -> None:
        """An operator who linked ``kirocrew-dev/prepare-pr`` to an outside
        provider keeps that provider's ``SKILL.md``: the relocation must not
        rename a file through the link."""
        from kiro_crew.skills import _ensure_builtin_skills

        provider = tmp_path / "provider" / "prepare-pr"
        provider.mkdir(parents=True)
        (provider / "SKILL.md").write_text("---\nname: prepare-pr\n---\nprovider copy")
        base = tmp_path / "skills"
        (base / "kirocrew-dev").mkdir(parents=True)
        try:
            (base / "kirocrew-dev" / "prepare-pr").symlink_to(provider, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks unavailable on this host")
        new = base / "kirocrew-dev" / "kirocrew-prepare-pr"
        new.mkdir(parents=True)
        (new / "SKILL.md").write_text("---\nname: kirocrew-prepare-pr\n---\nrenamed copy")

        _ensure_builtin_skills(base)

        assert (provider / "SKILL.md").read_text(encoding="utf-8").endswith("provider copy")
        assert not (provider / "SKILL.md.pre-relocation").exists()

    def test_repeated_migration_never_overwrites_prior_quarantine(self, tmp_path: Path) -> None:
        # HIGH regression (GPT 5.6): a rollback/reinstall can recreate
        # SKILL.md AFTER a prior migration quarantined a user-edited copy.
        # The second migration must NOT os.replace over the first quarantine
        # — both preserved copies must survive under distinct names.
        from kiro_crew.skills import _ensure_builtin_skills

        base = tmp_path / "skills"
        old = base / "prepare-pr"
        old.mkdir(parents=True)
        new = base / "kirocrew-dev" / "kirocrew-prepare-pr"
        new.mkdir(parents=True)
        (new / "SKILL.md").write_text("---\nname: kirocrew-prepare-pr\n---\nnested copy")

        # First migration quarantines the user's original edits.
        (old / "SKILL.md").write_text("---\nname: kirocrew-prepare-pr\n---\nFIRST user edit")
        _ensure_builtin_skills(base)
        first = old / "SKILL.md.pre-relocation"
        assert first.read_text(encoding="utf-8").endswith("FIRST user edit")

        # Rollback recreates SKILL.md with different content; migration re-runs.
        (old / "SKILL.md").write_text("---\nname: kirocrew-prepare-pr\n---\nSECOND rollback copy")
        _ensure_builtin_skills(base)

        # Both preserved copies survive; nothing was overwritten.
        assert first.read_text(encoding="utf-8").endswith("FIRST user edit")
        second = old / "SKILL.md.pre-relocation.2"
        assert second.read_text(encoding="utf-8").endswith("SECOND rollback copy")
        assert not (old / "SKILL.md").exists()

    def test_flat_copy_untouched_when_nested_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fail-safe: if the nested replacement never synced, the flat copy is
        # the ONLY working copy — it must stay discoverable.
        #
        # The empty source dir is what makes "never synced" real. Relying on a
        # relocated skill simply not being packaged would stop testing this the
        # moment that skill ships.
        from kiro_crew import skills as skills_mod
        from kiro_crew.skills import _ensure_builtin_skills

        monkeypatch.setattr(skills_mod, "_BUILTIN_SKILLS_DIR", tmp_path / "no-builtins")

        base = tmp_path / "skills"
        old = base / "babysit"
        old.mkdir(parents=True)
        (old / "SKILL.md").write_text("---\nname: babysit\n---\nonly copy")

        _ensure_builtin_skills(base)

        assert (old / "SKILL.md").exists(), "sole copy must stay discoverable"


class TestTriggeredSkills:
    """Tests for fuzzy trigger matching."""

    def _loader_with_skill(self, tmp_path, triggers, monkeypatch=None):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            f"---\nname: tiny-url\ndescription: Shorten URLs\ntriggers: {triggers}\n---\n# Tiny URL\n",
        )
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))
        if monkeypatch is not None:
            from unittest.mock import MagicMock

            monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())
        return loader

    def test_exact_trigger_match(self, tmp_path, monkeypatch):
        loader = self._loader_with_skill(tmp_path, "tiny url, shorten url", monkeypatch)
        assert "tiny-url" in loader.get_triggered_skills("make a tiny url for me")

    def test_reworded_query_matches(self, tmp_path, monkeypatch):
        """Words present but not contiguous — should still match."""
        loader = self._loader_with_skill(tmp_path, "shorten url", monkeypatch)
        assert "tiny-url" in loader.get_triggered_skills("can you shorten this url please")

    def test_fuzzy_partial_overlap(self, tmp_path, monkeypatch):
        """≥70% word overlap triggers the skill."""
        loader = self._loader_with_skill(tmp_path, "shorten this url", monkeypatch)
        # 2 of 3 words = 66% → no match
        assert "tiny-url" not in loader.get_triggered_skills("shorten this link")
        # 3 of 3 words = 100% → match
        assert "tiny-url" in loader.get_triggered_skills("shorten this url now")

    def test_no_match_unrelated(self, tmp_path, monkeypatch):
        loader = self._loader_with_skill(tmp_path, "tiny url, shorten url", monkeypatch)
        assert loader.get_triggered_skills("check my pipeline health") == []

    def test_always_skills_excluded(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "always-on",
            "---\nname: always-on\nalways: true\ntriggers: hello world\n---\n# Always\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        from unittest.mock import MagicMock

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())
        assert loader.get_triggered_skills("hello world") == []

    def test_case_insensitive(self, tmp_path, monkeypatch):
        loader = self._loader_with_skill(tmp_path, "Tiny URL", monkeypatch)
        assert "tiny-url" in loader.get_triggered_skills("Make a TINY url")

    def test_multiple_triggers_first_wins(self, tmp_path, monkeypatch):
        loader = self._loader_with_skill(tmp_path, "tiny url, shorten url", monkeypatch)
        result = loader.get_triggered_skills("shorten this url for me")
        assert result.count("tiny-url") == 1

    def test_empty_triggers_no_crash(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "no-trigger",
            "---\nname: no-trigger\ndescription: No triggers\n---\n# No Trigger\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        from unittest.mock import MagicMock

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())
        assert loader.get_triggered_skills("anything") == []

    def test_trigger_match_does_not_record_usage(self, tmp_path, monkeypatch):
        """A trigger match must not earn ranking weight in the ledger.

        Only body delivery (the context builder calling _record_use after
        load_skill succeeds) should update the hotness ranking. False-positive
        trigger matches must not drift the top-K toward skills the agent never
        reads.
        """
        loader = self._loader_with_skill(tmp_path, "tiny url", monkeypatch)

        # Confirm the skill triggers on the message
        triggered = loader.get_triggered_skills("make a tiny url for me")
        assert "tiny-url" in triggered

        # The ledger must still be at zero: a match alone is not a delivery
        assert loader._usage is not None
        assert loader._usage.score("tiny-url")[0] == 0.0

        # Simulating body delivery (what context.py does after load_skill)
        # must update the ledger
        loader._record_use("tiny-url")
        assert loader._usage.score("tiny-url")[0] == 1.0


class TestSkillsCRUD:
    def test_create_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        ok = loader.create_skill("my-tool", "---\nname: my-tool\n---\n# My Tool\n")
        assert ok is True
        assert (skills_dir / "my-tool" / "SKILL.md").exists()
        content = loader.load_skill("my-tool")
        assert "My Tool" in content

    def test_create_duplicate_fails(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "existing", "# Existing\n")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        ok = loader.create_skill("existing", "# New\n")
        assert ok is False

    def test_update_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "updatable", "# Old\n")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        ok = loader.update_skill("updatable", "# Updated\nNew content.")
        assert ok is True
        content = loader.load_skill("updatable")
        assert "Updated" in content

    def test_update_missing_fails(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        ok = loader.update_skill("nonexistent", "# Nope\n")
        assert ok is False

    def test_delete_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "deletable", "# Delete me\n")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        assert loader.load_skill("deletable") is not None
        ok = loader.delete_skill("deletable")
        assert ok is True
        assert loader.load_skill("deletable") is None
        assert not (skills_dir / "deletable").exists()

    def test_delete_missing_fails(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        ok = loader.delete_skill("nonexistent")
        assert ok is False

    def test_path_traversal_load(self, tmp_path):
        """Path traversal in skill name must be rejected."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        assert loader.load_skill("../../etc/passwd") is None
        assert loader.load_skill("../secret") is None
        assert loader.load_skill("foo/bar") is None

    def test_path_traversal_create(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        assert loader.create_skill("../escape", "# bad") is False
        assert loader.create_skill("", "# bad") is False
        # Nested paths are now allowed
        assert loader.create_skill("foo/bar", "# nested") is True

    def test_rooted_name_load(self, tmp_path):
        """Rooted names must be rejected: a pathlib join with an absolute
        segment discards the base directory, escaping the skills root."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        # POSIX-absolute (payload scoped under tmp_path so a guard regression
        # is contained rather than acted on against the host)
        assert loader.load_skill(str(tmp_path / "escape")) is None
        assert loader.load_skill("/foo") is None
        # Windows drive-qualified, forward-slash spelling
        assert loader.load_skill("C:/Windows/System32") is None
        # Windows drive-relative (no root, still re-anchors the join)
        assert loader.load_skill("C:evil") is None
        # Forward-slash UNC prefix
        assert loader.load_skill("//server/share/skill") is None
        # Backslash spellings stay rejected by the existing backslash rule
        assert loader.load_skill("C:\\Windows\\evil") is None
        assert loader.load_skill("\\\\server\\share\\skill") is None
        # Dot-only spellings collapse the join back onto the skills root
        assert loader.load_skill(".") is None
        assert loader.load_skill("./") is None
        assert loader.load_skill(".//.") is None

    def test_rooted_name_create_update_delete(self, tmp_path):
        """Skill CRUD must refuse rooted or dot-only names but accept
        relative ones. Absolute payloads live under tmp_path so a guard
        regression surfaces as a containment failure, not a host mutation."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        escape = tmp_path / "escape"
        for rooted in (str(escape), "C:/abs", "C:abs", "//server/share/x", ".", "./"):
            assert loader.create_skill(rooted, "# bad") is False
            assert loader.update_skill(rooted, "# bad") is False
            assert loader.delete_skill(rooted) is False
        assert not escape.exists()
        # delete_skill(".") must not have collapsed onto the skills root
        assert skills_dir.is_dir()
        # Normal relative names keep working: single-segment and nested
        assert loader.create_skill("plain", "# ok") is True
        assert loader.create_skill("nested/child", "# ok") is True

    def test_create_then_list(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        loader.create_skill(
            "alpha",
            "---\nname: alpha\ndescription: Alpha skill\n---\n# Alpha\n",
        )
        loader.create_skill(
            "beta",
            "---\nname: beta\ndescription: Beta skill\n---\n# Beta\n",
        )
        skills = _settled(loader).list_skills()
        names = [s["name"] for s in skills]
        assert "alpha" in names
        assert "beta" in names


class TestTriggerMatching:
    """Tests for word-overlap matching with negative keywords and max_triggered."""

    def test_basic_trigger_match(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "weather",
            "---\nname: weather\ndescription: Get weather info\ntriggers: weather forecast\n---\n",
        )
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        result = loader.get_triggered_skills("what's the weather forecast today")
        assert "weather" in result

    def test_negative_trigger_excludes(self, tmp_path, monkeypatch):
        """Negative trigger !keyword should exclude the skill."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "code-search",
            "---\nname: code-search\ndescription: Search code\ntriggers: search code, !search examples\n---\n",
        )
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        # Positive match without negative words
        assert "code-search" in loader.get_triggered_skills("search code repositories")
        # Negative trigger fires — "search" and "examples" both present
        assert "code-search" not in loader.get_triggered_skills("search for code examples")

    def test_max_triggered_limit(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        for i in range(5):
            _create_skill(
                skills_dir,
                f"skill{i}",
                f"---\nname: skill{i}\ndescription: Skill {i}\ntriggers: test\n---\n",
            )
        # max_triggered is snapshotted at construction, so inject via config=.
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=2))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        result = loader.get_triggered_skills("test")
        assert len(result) == 2

    def test_sort_by_overlap_score(self, tmp_path, monkeypatch):
        """Higher overlap skills should appear first."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        # "good" trigger "alpha beta gamma delta" → 3/4 = 0.75 (above 0.7)
        _create_skill(
            skills_dir,
            "good",
            "---\nname: good\ndescription: Good\ntriggers: alpha beta gamma delta\n---\n",
        )
        # "better" trigger "alpha beta gamma" → 3/3 = 1.0
        _create_skill(
            skills_dir,
            "better",
            "---\nname: better\ndescription: Better\ntriggers: alpha beta gamma\n---\n",
        )
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=5))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        result = loader.get_triggered_skills("alpha beta gamma")
        assert len(result) == 2
        assert result[0] == "better"  # higher overlap first
        assert result[1] == "good"

    def test_always_on_skills_skipped(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir, "always", "---\nname: always\nalways: true\ntriggers: test\n---\n"
        )
        _create_skill(skills_dir, "normal", "---\nname: normal\ntriggers: test\n---\n")
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=5))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        result = loader.get_triggered_skills("test")
        assert "always" not in result
        assert "normal" in result

    def test_multi_word_trigger_phrase(self, tmp_path, monkeypatch):
        """Multi-word trigger phrases should match as a unit."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: Shorten URLs\ntriggers: shorten url, create tiny link, make short url\n---\n",
        )
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        assert "tiny-url" in loader.get_triggered_skills("please shorten this url")
        assert "tiny-url" in loader.get_triggered_skills("create a tiny link for me")
        # Single word "url" alone shouldn't trigger (1/2 = 50% < 70%)
        assert "tiny-url" not in loader.get_triggered_skills("what is a url")


class TestAutoSkillProvenance:
    """Tests for the AutoSkillProvenance dataclass frontmatter serialization."""

    def test_now_iso_is_utc(self):
        from kiro_crew.skills import AutoSkillProvenance

        stamp = AutoSkillProvenance.now_iso()
        # ISO 8601 UTC ends with +00:00 when using timezone.utc
        assert "+00:00" in stamp

    def test_frontmatter_lines_minimum(self):
        from kiro_crew.skills import AutoSkillProvenance

        prov = AutoSkillProvenance(
            session_key="dashboard:chat-1", created_at="2026-05-05T11:30:00+00:00"
        )
        lines = prov.to_frontmatter_lines()
        assert "source: auto" in lines
        assert "session_key: dashboard:chat-1" in lines
        assert "created_at: 2026-05-05T11:30:00+00:00" in lines
        # Optional fields omitted when unset
        assert not any(line.startswith("refined_at:") for line in lines)
        assert not any(line.startswith("reuse_count:") for line in lines)

    def test_frontmatter_lines_with_refinement(self):
        from kiro_crew.skills import AutoSkillProvenance

        prov = AutoSkillProvenance(
            session_key="dashboard:chat-2",
            created_at="2026-05-05T11:30:00+00:00",
            refined_at="2026-05-06T09:15:00+00:00",
            reuse_count=3,
        )
        lines = prov.to_frontmatter_lines()
        assert "refined_at: 2026-05-06T09:15:00+00:00" in lines
        assert "reuse_count: 3" in lines


class TestFindSimilar:
    """Tests for SkillsLoader.find_similar description overlap dedup."""

    def test_returns_none_when_no_skills(self, tmp_path):
        loader = _settled(SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False))
        assert loader.find_similar("anything") is None

    def test_returns_none_when_no_overlap(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "weather",
            "---\nname: weather\ndescription: Get weather info for a city\n---\n",
        )
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        assert loader.find_similar("deploy kubernetes service") is None

    def test_detects_near_duplicate(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "ssh-timber",
            "---\nname: ssh-timber\ndescription: SSH chained log search on Timber production hosts\n---\n",
        )
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        match = loader.find_similar(
            "SSH chained log search on Timber production hosts", threshold=0.8
        )
        assert match == "ssh-timber"

    def test_exclude_self_during_refine(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "auto/foo",
            "---\nname: auto/foo\ndescription: One two three four five keywords\n---\n",
        )
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        # Without exclude, we match ourselves
        assert loader.find_similar("one two three four five keywords") == "auto/foo"
        # With exclude, we don't
        assert loader.find_similar("one two three four five keywords", exclude="auto/foo") is None

    def test_threshold_respected(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "alpha",
            "---\nname: alpha\ndescription: one two three four five six seven\n---\n",
        )
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        # 3/10 = 0.3 overlap — below 0.85 threshold, rejected
        assert loader.find_similar("one two three eight nine ten eleven") is None


class TestIsAutoGenerated:
    """Tests for the auto/<name> namespace check."""

    def test_true_for_auto_prefix(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.is_auto_generated("auto/foo") is True
        assert loader.is_auto_generated("auto/debug-timber-logs") is True

    def test_false_for_manual(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.is_auto_generated("ssh-timber") is False
        assert loader.is_auto_generated("utils/tiny-url") is False

    def test_false_for_unsafe_name(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        # Path traversal defence — safe_name check kicks in first
        assert loader.is_auto_generated("../auto/foo") is False
        assert loader.is_auto_generated("auto/..\\bar") is False


class TestCreateAutoSkill:
    """Tests for SkillsLoader.create_auto_skill."""

    def _make_provenance(self):
        from kiro_crew.skills import AutoSkillProvenance

        return AutoSkillProvenance(
            session_key="dashboard:chat-1",
            created_at="2026-05-05T11:30:00+00:00",
        )

    def test_creates_skill_under_auto_namespace(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        name = loader.create_auto_skill(
            "debug-timber",
            description="Debug Timber log searches",
            triggers="timber, debug, log search",
            procedure_md="## When\nSSH chain patterns\n",
            provenance=self._make_provenance(),
        )
        assert name == "auto/debug-timber"
        skill_file = tmp_path / "skills" / "auto" / "debug-timber" / "SKILL.md"
        assert skill_file.exists()
        content = skill_file.read_text(encoding="utf-8")
        assert "name: auto/debug-timber" in content
        assert "source: auto" in content
        assert "session_key: dashboard:chat-1" in content
        assert "created_at: 2026-05-05T11:30:00+00:00" in content
        assert "triggers: timber, debug, log search" in content
        assert "SSH chain patterns" in content

    def test_rejects_invalid_slug(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        # Too short
        assert (
            loader.create_auto_skill(
                "ab",
                description="desc",
                triggers="",
                procedure_md="body",
                provenance=self._make_provenance(),
            )
            is None
        )
        # Spaces
        assert (
            loader.create_auto_skill(
                "bad name",
                description="desc",
                triggers="",
                procedure_md="body",
                provenance=self._make_provenance(),
            )
            is None
        )
        # Leading hyphen
        assert (
            loader.create_auto_skill(
                "-bad",
                description="desc",
                triggers="",
                procedure_md="body",
                provenance=self._make_provenance(),
            )
            is None
        )
        # Path traversal
        assert (
            loader.create_auto_skill(
                "../evil",
                description="desc",
                triggers="",
                procedure_md="body",
                provenance=self._make_provenance(),
            )
            is None
        )

    def test_rejects_oversized_procedure(self, tmp_path):
        from kiro_crew.skills import AUTO_SKILL_MAX_PROCEDURE_CHARS

        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        huge = "x" * (AUTO_SKILL_MAX_PROCEDURE_CHARS + 1)
        assert (
            loader.create_auto_skill(
                "test-skill",
                description="desc",
                triggers="",
                procedure_md=huge,
                provenance=self._make_provenance(),
            )
            is None
        )

    def test_refuses_duplicate(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert (
            loader.create_auto_skill(
                "duplicate-name",
                description="desc",
                triggers="",
                procedure_md="body",
                provenance=self._make_provenance(),
            )
            == "auto/duplicate-name"
        )
        # Second call with same slug is rejected
        assert (
            loader.create_auto_skill(
                "duplicate-name",
                description="different",
                triggers="",
                procedure_md="different body",
                provenance=self._make_provenance(),
            )
            is None
        )


class TestUpdateAutoSkill:
    """Tests for SkillsLoader.update_auto_skill (refine path)."""

    def _make_provenance(self, refined_at=""):
        from kiro_crew.skills import AutoSkillProvenance

        return AutoSkillProvenance(
            session_key="dashboard:chat-2",
            created_at="2026-05-05T11:30:00+00:00",
            refined_at=refined_at,
        )

    def test_refuses_manual_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "manual-skill",
            "---\nname: manual-skill\ndescription: hand-crafted\n---\n# Manual\nHand-authored content.\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        # Refuse to update a hand-authored skill even if the caller asks
        assert (
            loader.update_auto_skill(
                "manual-skill",
                description="trying to overwrite",
                triggers="",
                procedure_md="new body",
                provenance=self._make_provenance(),
            )
            is False
        )
        # Original content untouched
        content = (skills_dir / "manual-skill" / "SKILL.md").read_text(encoding="utf-8")
        assert "Hand-authored content" in content

    def test_updates_auto_skill(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        # Create then refine
        loader.create_auto_skill(
            "refine-me",
            description="original desc",
            triggers="",
            procedure_md="## Step 1\nrun X\n",
            provenance=self._make_provenance(),
        )
        ok = loader.update_auto_skill(
            "auto/refine-me",
            description="refined desc",
            triggers="refine, me",
            procedure_md="## Step 1\nrun Y (better)\n",
            provenance=self._make_provenance(refined_at="2026-05-06T09:00:00+00:00"),
        )
        assert ok is True
        content = (tmp_path / "skills" / "auto" / "refine-me" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "refined desc" in content
        assert "run Y (better)" in content
        assert "refined_at: 2026-05-06T09:00:00+00:00" in content

    def test_returns_false_for_missing(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert (
            loader.update_auto_skill(
                "auto/does-not-exist",
                description="x",
                triggers="",
                procedure_md="body",
                provenance=self._make_provenance(),
            )
            is False
        )


class TestListAutoSkills:
    """Tests for list_auto_skills filtering."""

    def test_filters_to_auto_namespace_only(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "manual/one",
            "---\nname: manual/one\ndescription: manual\n---\n",
        )
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        loader.create_auto_skill(
            "generated-one",
            description="auto",
            triggers="",
            procedure_md="body",
            provenance=(
                __import__(
                    "kiro_crew.skills", fromlist=["AutoSkillProvenance"]
                ).AutoSkillProvenance(
                    session_key="x",
                    created_at="2026-05-05T11:30:00+00:00",
                )
            ),
        )
        auto_only = _settled(loader).list_auto_skills()
        assert len(auto_only) == 1
        assert auto_only[0]["key"] == "auto/generated-one"


class TestUpdateAutoSkillPreservesCreatedAt:
    """Regression test for #5: refine must not clobber created_at."""

    def test_created_at_preserved_across_refine(self, tmp_path):
        from kiro_crew.skills import AutoSkillProvenance

        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        original = AutoSkillProvenance(
            session_key="dashboard:chat-1",
            created_at="2026-05-05T11:00:00+00:00",
        )
        loader.create_auto_skill(
            "preserved-ts",
            description="initial",
            triggers="",
            procedure_md="v1",
            provenance=original,
        )
        # Caller passes a fresh provenance with a new created_at — the
        # update path should ignore that and preserve the original.
        bogus_new = AutoSkillProvenance(
            session_key="dashboard:chat-2",
            created_at="2026-05-06T12:00:00+00:00",  # WRONG created_at
            refined_at="2026-05-06T12:00:00+00:00",
        )
        ok = loader.update_auto_skill(
            "auto/preserved-ts",
            description="refined",
            triggers="",
            procedure_md="v2",
            provenance=bogus_new,
        )
        assert ok is True
        content = (tmp_path / "skills" / "auto" / "preserved-ts" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        # Original created_at must survive
        assert "created_at: 2026-05-05T11:00:00+00:00" in content
        # New refined_at was honored
        assert "refined_at: 2026-05-06T12:00:00+00:00" in content
        # Session key from the refine provenance was honored (provenance
        # fields other than created_at update normally).
        assert "session_key: dashboard:chat-2" in content


def _cfg_with_extra(paths):
    """Build a config with skills.extra_paths set (isolated, no disk read)."""
    return KiroCrewConfig(skills=SkillsConfig(extra_paths=paths))


class TestSkillsLoaderExtraPaths:
    def test_extra_path_skill_listed(self, tmp_path):
        local = tmp_path / "local"
        local.mkdir()
        extra = tmp_path / "extra"
        _create_skill(
            extra, "roaring", "---\nname: roaring\ndescription: Roaring ops\n---\n# Roaring\n"
        )
        loader = _settled(
            SkillsLoader(
                skills_path=local,
                install_builtins=False,
                config=_cfg_with_extra([str(extra)]),
            )
        )
        names = {s["name"] for s in loader.list_skills()}
        assert "roaring" in names

    def test_local_takes_precedence(self, tmp_path):
        local = tmp_path / "local"
        extra = tmp_path / "extra"
        _create_skill(local, "dup", "---\nname: dup\ndescription: LOCAL\n---\n# Local\n")
        _create_skill(extra, "dup", "---\nname: dup\ndescription: EXTRA\n---\n# Extra\n")
        loader = _settled(
            SkillsLoader(
                skills_path=local,
                install_builtins=False,
                config=_cfg_with_extra([str(extra)]),
            )
        )
        dup = [s for s in loader.list_skills() if s["name"] == "dup"]
        assert len(dup) == 1
        assert dup[0]["description"] == "LOCAL"

    def test_load_skill_from_extra_path(self, tmp_path):
        local = tmp_path / "local"
        local.mkdir()
        extra = tmp_path / "extra"
        _create_skill(extra, "contributing", "---\nname: contributing\n---\n# Contributing\nGuide.")
        loader = SkillsLoader(
            skills_path=local,
            install_builtins=False,
            config=_cfg_with_extra([str(extra)]),
        )
        content = loader.load_skill("contributing")
        assert content is not None
        assert "Contributing" in content

    def test_local_skill_shadows_extra_on_load(self, tmp_path):
        local = tmp_path / "local"
        extra = tmp_path / "extra"
        _create_skill(local, "dup", "---\nname: dup\n---\n# LocalBody\n")
        _create_skill(extra, "dup", "---\nname: dup\n---\n# ExtraBody\n")
        loader = SkillsLoader(
            skills_path=local,
            install_builtins=False,
            config=_cfg_with_extra([str(extra)]),
        )
        assert "LocalBody" in loader.load_skill("dup")

    def test_identical_copy_at_different_depth_deduped_in_context(self, tmp_path):
        """A physical copy of a skill nested one level deeper in another root
        (a package tree plus a flat mirror of it) yields ONE index line, not two.

        The per-key shadowing in enumeration cannot catch this: the copies have
        different dir-relative keys (``pkg/tool`` vs ``tool``), so both survive
        to ``list_skills()`` and, before the fingerprint dedup, both rendered
        identical summary lines differing only in path.
        """
        body = "---\nname: tool\ndescription: One tool\n---\n# Tool\nSame bytes.\n"
        local = tmp_path / "local"
        local.mkdir()
        extra = tmp_path / "extra"
        _create_skill(extra, "tool", body)
        _create_skill(extra, "mirror-pkg/tool", body)
        loader = _settled(
            SkillsLoader(
                skills_path=local,
                install_builtins=False,
                config=_cfg_with_extra([str(extra)]),
            )
        )
        # Both copies are enumerated (different keys) ...
        keys = {s["key"] for s in loader.list_skills()}
        assert {"tool", "mirror-pkg/tool"} <= keys
        # ... but the injected index carries exactly one line for the skill,
        # on the legacy full-dump path and the budgeted lazy path alike.
        legacy = loader.get_context()
        assert legacy.count("**tool**") == 1
        lazy = loader.get_context(budget=10_000)
        assert lazy.count("**tool**") == 1

    def test_same_name_different_content_not_deduped(self, tmp_path):
        """Same frontmatter name with different content is a legitimate
        collision, not a mirror copy — both index lines must survive."""
        local = tmp_path / "local"
        local.mkdir()
        extra = tmp_path / "extra"
        _create_skill(extra, "tool", "---\nname: tool\ndescription: Original\n---\n# A\n")
        _create_skill(
            extra, "fork-pkg/tool", "---\nname: tool\ndescription: Forked variant\n---\n# B\n"
        )
        loader = _settled(
            SkillsLoader(
                skills_path=local,
                install_builtins=False,
                config=_cfg_with_extra([str(extra)]),
            )
        )
        legacy = loader.get_context()
        assert legacy.count("**tool**") == 2
        assert "Original" in legacy and "Forked variant" in legacy

    def test_equal_metadata_different_bytes_not_deduped(self, tmp_path):
        """Equal (name, description, size) with DIFFERENT bodies is a metadata
        coincidence, not a copy — content verification must keep both.

        The pinned path injects full bodies, so dropping a same-size row on
        metadata alone would silently lose one skill's instructions. The two
        bodies below are the same byte length but different procedures.
        """
        local = tmp_path / "local"
        local.mkdir()
        extra = tmp_path / "extra"
        head = "---\nname: tool\ndescription: One tool\nalways: true\n---\n"
        _create_skill(extra, "tool", head + "# Tool\nStep: run AAAA.\n")
        _create_skill(extra, "mirror-pkg/tool", head + "# Tool\nStep: run BBBB.\n")
        loader = _settled(
            SkillsLoader(
                skills_path=local,
                install_builtins=False,
                config=_cfg_with_extra([str(extra)]),
            )
        )
        rows = [s for s in loader.list_skills() if s["name"] == "tool"]
        assert rows[0]["size_bytes"] == rows[1]["size_bytes"]  # fingerprints collide
        legacy = loader.get_context()
        assert "run AAAA" in legacy and "run BBBB" in legacy  # both bodies survive

    def test_nonexistent_extra_path_ignored(self, tmp_path):
        local = tmp_path / "local"
        local.mkdir()
        loader = SkillsLoader(
            skills_path=local,
            install_builtins=False,
            config=_cfg_with_extra([str(tmp_path / "missing")]),
        )
        assert loader._extra_paths == []

    def test_sensitive_extra_path_skipped(self, tmp_path, monkeypatch):
        extra = tmp_path / "extra"
        extra.mkdir()
        monkeypatch.setattr("kiro_crew.skills.is_sensitive_path", lambda p: True)
        loader = SkillsLoader(
            skills_path=tmp_path / "local",
            install_builtins=False,
            config=_cfg_with_extra([str(extra)]),
        )
        assert loader._extra_paths == []

    def test_sensitive_skill_file_skipped(self, tmp_path, monkeypatch):
        # Extra dir is allowed, but an individual SKILL.md is flagged sensitive:
        # it must be excluded from listing AND refused on load.
        local = tmp_path / "local"
        local.mkdir()
        extra = tmp_path / "extra"
        _create_skill(extra, "x", "---\nname: x\n---\n# X\n")
        # Both listing (_iter) and load_skill route extra-path reads through
        # validate_file_path — flagging it there blocks both.
        monkeypatch.setattr(
            "kiro_crew.skills.validate_file_path",
            lambda p: None if str(p).endswith("SKILL.md") else p,
        )
        loader = _settled(
            SkillsLoader(
                skills_path=local,
                install_builtins=False,
                config=_cfg_with_extra([str(extra)]),
            )
        )
        assert "x" not in {s["name"] for s in loader.list_skills()}
        assert loader.load_skill("x") is None


class TestTriggerPerformance:
    """Per-message cost reductions in get_triggered_skills (perf)."""

    def test_single_audit_event_for_triggered_set(self, tmp_path, monkeypatch):
        """One SEL event per call (for the matched set), not one per skill."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        # Several skills; only one will match the query.
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n",
        )
        _create_skill(
            skills_dir,
            "weather",
            "---\nname: weather\ndescription: d\ntriggers: weather forecast\n---\n# x\n",
        )
        _create_skill(
            skills_dir,
            "pipeline",
            "---\nname: pipeline\ndescription: d\ntriggers: pipeline health\n---\n# x\n",
        )
        loader = _settled(
            SkillsLoader(
                skills_path=skills_dir,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: fake_sel)
        triggered = loader.get_triggered_skills("please shorten this url")

        assert "tiny-url" in triggered
        # Exactly ONE audit event, regardless of how many skills were scanned.
        assert fake_sel.log_tool_invocation.call_count == 1

    def test_no_audit_event_when_nothing_triggers(self, tmp_path, monkeypatch):
        """The common case (no match) writes zero SEL events."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: fake_sel)
        assert loader.get_triggered_skills("hello there friend") == []
        assert fake_sel.log_tool_invocation.call_count == 0

    @pytest.mark.parametrize(
        "triggers",
        [
            "shorten url, !test",  # positive first
            "!test, shorten url",  # negative first — must be order-independent
        ],
    )
    def test_negative_trigger_exclusion_is_audited(self, tmp_path, monkeypatch, triggers):
        """A negative trigger that excludes an otherwise-matching skill is a
        permission DENY and must still emit an audit event (with the negated
        skill in metadata), regardless of trigger order in the frontmatter."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            f"---\nname: tiny-url\ndescription: d\ntriggers: {triggers}\n---\n# x\n",
        )
        # A positive cap: the denial is a DENY only where the match could have
        # been a grant. At the shipped cap of 0 the same veto is not audited
        # (see test_zero_cap_writes_no_denied_audit_row).
        loader = _settled(
            SkillsLoader(
                skills_path=skills_dir,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: fake_sel)
        # "shorten url" matches the positive trigger, but "test" fires the
        # negative trigger → excluded.
        triggered = loader.get_triggered_skills("shorten url for this test")

        assert triggered == []  # excluded by negative trigger
        # The denial is audited (one event), with the skill named in metadata —
        # even when "!test" is listed before "shorten url".
        assert fake_sel.log_tool_invocation.call_count == 1
        _, kwargs = fake_sel.log_tool_invocation.call_args
        assert kwargs["outcome"] == "denied"
        assert "tiny-url" in kwargs["metadata"]["negated"]

    def test_iter_result_is_cached(self, tmp_path, monkeypatch):
        """The skill-file walk is cached, not re-run on every call."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n",
        )
        # A positive cap so the scan actually runs; at 0 there is no walk to cache.
        loader = SkillsLoader(
            skills_path=skills_dir,
            install_builtins=False,
            config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
        )
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        calls = {"n": 0}
        orig = loader._iter_uncached

        def _counting(project_key=None):
            calls["n"] += 1
            return orig(project_key)

        monkeypatch.setattr(loader, "_iter_uncached", _counting)
        # Settled only once the counter is in place, so the first walk is counted:
        # exactly one, and the five messages after it must add none.
        _settled(loader)
        for _ in range(5):
            loader.get_triggered_skills("shorten this url")
        assert calls["n"] == 1

    def test_config_not_loaded_per_message(self, tmp_path, monkeypatch):
        """get_triggered_skills must not re-load config on every message — the
        trigger cap is snapshotted at construction (max_triggered hoist)."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n",
        )
        loader = _settled(
            SkillsLoader(
                skills_path=skills_dir,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        calls = {"n": 0}
        real_load = KiroCrewConfig.load

        def _counting_load():
            calls["n"] += 1
            return real_load()

        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", _counting_load)
        for _ in range(5):
            loader.get_triggered_skills("shorten this url")
        # Zero config loads across 5 messages — the cap was snapshotted in __init__.
        assert calls["n"] == 0

    def test_max_triggered_snapshot_caps_results(self, tmp_path, monkeypatch):
        """The snapshotted max_triggered still bounds the result count."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        for i in range(5):
            _create_skill(
                skills_dir,
                f"s{i}",
                f"---\nname: s{i}\ndescription: d\ntriggers: shorten url\n---\n# x\n",
            )
        cfg = KiroCrewConfig()
        cfg.skills.max_triggered = 2
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        triggered = loader.get_triggered_skills("shorten url")
        assert len(triggered) == 2

    def test_zero_cap_skips_the_scan_entirely(self, tmp_path, monkeypatch):
        """At the shipped default (``max_triggered = 0``) the matcher is off: no
        skill is walked, read or scored, because every score would be sliced away.
        The per-message cost is one cap read, and the result is the same ``[]``."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n",
        )
        loader = SkillsLoader(
            skills_path=skills_dir,
            install_builtins=False,
            config=KiroCrewConfig(skills=SkillsConfig(max_triggered=0)),
        )
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: fake_sel)
        walk = MagicMock(return_value=[])
        monkeypatch.setattr(loader, "_iter_visible", walk)
        read = MagicMock(return_value={})
        monkeypatch.setattr(loader, "_cached_frontmatter", read)

        assert loader.get_triggered_skills("shorten this url") == []
        walk.assert_not_called()
        read.assert_not_called()
        assert fake_sel.log_tool_invocation.call_count == 0

    def test_zero_cap_writes_no_denied_audit_row(self, tmp_path, monkeypatch):
        """A ``!`` veto at cap 0 excludes nothing that could have been injected, so
        it is not a permission DENY and writes no ``skill_trigger`` row -- unlike
        the same veto under a positive cap (test_negative_trigger_exclusion_is_audited)."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url, !test\n---\n# x\n",
        )
        loader = SkillsLoader(
            skills_path=skills_dir,
            install_builtins=False,
            config=KiroCrewConfig(skills=SkillsConfig(max_triggered=0)),
        )
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: fake_sel)

        assert loader.get_triggered_skills("shorten url for this test") == []
        assert fake_sel.log_tool_invocation.call_count == 0

    def test_zero_cap_still_consults_select(self, tmp_path, monkeypatch):
        """The zero-cap skip belongs to the matcher, not to ``select``: a
        selection point still runs, owns its own zero-cap refusal, and a pick it
        returns is injected and audited as a selection."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n",
        )
        loader = _settled(
            SkillsLoader(
                skills_path=skills_dir,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=0)),
            )
        )
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: fake_sel)
        select = MagicMock(return_value=["tiny-url"])

        assert loader.get_triggered_skills("hello there friend", select=select) == ["tiny-url"]
        select.assert_called_once_with()
        assert fake_sel.log_tool_invocation.call_count == 1
        _, kwargs = fake_sel.log_tool_invocation.call_args
        assert kwargs["outcome"] == "triggered"
        assert kwargs["metadata"]["selected"] == "true"
        assert kwargs["metadata"]["skills"] == "tiny-url"

    def test_zero_cap_is_read_live_so_raising_it_turns_the_scan_back_on(
        self, tmp_path, monkeypatch
    ):
        """The skip reads the cap from the live snapshot, so ``kirocrew config set
        skills.max_triggered 3`` re-enables the scan for the very next message
        without rebuilding the loader."""
        from unittest.mock import MagicMock

        from kiro_crew.config import live

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n",
        )
        loader = _settled(
            SkillsLoader(
                skills_path=skills_dir,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=0)),
            )
        )
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        assert loader.get_triggered_skills("shorten this url") == []
        cfg = KiroCrewConfig()
        cfg.skills.max_triggered = 3
        live.watch().prime(cfg)
        assert loader.get_triggered_skills("shorten this url") == ["tiny-url"]


class TestResolveDollarSkills:
    """$skillname inline trigger resolution (allowlist, all sources)."""

    def _loader(self, tmp_path, opened=None, **extra):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "oncall-handover",
            "---\nname: oncall-handover\ndescription: Handover\n---\n# Handover\nBody A.",
        )
        _create_skill(
            skills_dir,
            "nested/ticket-pull",
            "---\nname: nested/ticket-pull\ndescription: Pull\n---\n# Pull\nBody B.",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False, **extra)
        if opened is not None:
            # Registered before the settle, so a settle that raises still closes it.
            opened(loader)
        return _settled(loader)

    def test_basic_match_leaves_token_resolves_body(self, tmp_path):
        loader = self._loader(tmp_path)
        out = loader.resolve_dollar_skills("please $oncall-handover now")
        assert len(out) == 1
        token, name, body = out[0]
        assert token == "oncall-handover"
        assert name == "oncall-handover"
        assert "Body A." in body
        # frontmatter stripped
        assert "description:" not in body

    def test_nested_key_resolved_by_leaf(self, tmp_path):
        loader = self._loader(tmp_path)
        out = loader.resolve_dollar_skills("run $ticket-pull")
        assert len(out) == 1
        assert out[0][1] == "nested/ticket-pull"

    def test_multiple_tokens_anywhere(self, tmp_path):
        loader = self._loader(tmp_path)
        out = loader.resolve_dollar_skills("start $oncall-handover then $ticket-pull")
        names = [n for _t, n, _b in out]
        assert names == ["oncall-handover", "nested/ticket-pull"]

    def test_nested_key_uses_forward_slash(self, tmp_path, opened):
        """The enumerated key of a nested skill is forward-slash-separated.

        Discriminator for a Windows-only failure of ``test_multiple_tokens_anywhere``
        seen on a CodeBuild container: the resolver leaf-matches ``$ticket-pull``
        against ``key.rsplit("/", 1)[-1]``, so a key built with the OS separator
        (``nested\\ticket-pull`` on Windows) would leaf to the whole string and match
        nothing — an identical-looking assertion failure, but with the bug in NAMING
        rather than enumeration or the read gate. This pins the key exactly, so a CI
        run answers which mechanism is at fault instead of leaving it a coin flip: if
        BOTH this and ``test_multiple_tokens_anywhere`` fail, the key is misnamed; if
        this passes while the other fails, naming is eliminated. The loader is
        wrapped in ``opened`` so its catalog-refresh thread and SQLite descriptors
        are released at teardown.
        """
        loader = self._loader(tmp_path, opened=opened)
        keys = [s["key"] for s in loader.scoped_skills()]
        assert "nested/ticket-pull" in keys
        assert "nested\\ticket-pull" not in keys

    def test_two_flat_siblings_both_enumerate_and_resolve(self, tmp_path, opened):
        """Two flat sibling skills both enumerate and both resolve — a count pin.

        Discriminator for a Windows-only second-token drop seen on a CodeBuild
        container: resolving ``$one $two`` returns only one skill even though both
        are flat, top-level, and carry no path separator. That rules out the
        nested-key/separator theory (there is no separator here) and localizes the
        drop to the count, not the name. This pins BOTH stages so a CI run says
        which stage drops the sibling:

        * If ``scoped_skills`` returns one key, the WALK (``_iter_skill_files``)
          drops a distinct sibling on that filesystem — its only sibling-dropping
          site is the ``seen_real`` dedup keyed on ``os.path.realpath``.
        * If ``scoped_skills`` returns both keys but ``resolve_dollar_skills``
          returns one, the drop is in the resolver's per-token loop.

        Kept minimal and platform-neutral: the assertion holds on every platform,
        so a red is a real defect on the host that produced it, not a POSIX-only
        expectation. The loader is wrapped in ``opened`` so its catalog-refresh
        thread and SQLite descriptors are closed at teardown — a measurement test
        must not itself leak the resource that makes the suite flaky.
        """
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "one", "---\nname: one\ndescription: A\n---\n# One\nBody one.")
        _create_skill(skills_dir, "two", "---\nname: two\ndescription: B\n---\n# Two\nBody two.")
        loader = _settled(opened(SkillsLoader(skills_path=skills_dir, install_builtins=False)))

        keys = sorted(s["key"] for s in loader.scoped_skills())
        assert keys == ["one", "two"]

        out = loader.resolve_dollar_skills("use $one and $two")
        assert [n for _t, n, _b in out] == ["one", "two"]

    def test_nested_skill_enumerates_beside_flat_under_plain_and_symlinked_base(self, tmp_path):
        """A nested skill enumerates alongside a flat one — even via a symlinked base.

        Regression guard for the Windows-only report where the NESTED skill
        (``nested/ticket-pull``) vanished from enumeration while the depth-1 flat
        skill survived. The catalog walk resolves each node through
        ``os.path.realpath`` and admits a SKILL.md whose resolved path lands
        under the base; this pins that a nested descendant and a flat sibling
        both enumerate, so a regression that drops the nested subtree is caught.

        The symlinked-base half matters and a naive fix fails it: a lexical-only
        containment shortcut would drop a skill reached through a symlinked base,
        because the symlinked spelling is not lexically under the base's
        realpath. Enumeration is deterministic here, so the assertion holds on
        every platform; ``make_dir_link`` keeps the symlinked-base path exercised
        on Windows (a junction) instead of skipping there.
        """
        from kiro_crew.skills import _iter_skill_files

        real_base = tmp_path / "real_skills"
        _create_skill(real_base, "flat-skill", "---\nname: flat-skill\n---\n# Flat")
        _create_skill(real_base, "nested/deep-skill", "---\nname: nested/deep-skill\n---\n# Deep")

        # Plain base: both enumerate, nested key is forward-slash separated.
        names = sorted(name for name, _path in _iter_skill_files(real_base))
        assert names == ["flat-skill", "nested/deep-skill"]

        # Symlinked base: the nested skill must still enumerate. A lexical-only
        # containment shortcut would drop it here because the symlinked spelling
        # is not lexically under the base's realpath. make_dir_link (a junction
        # on Windows, a dir symlink on POSIX) keeps this exercised on the
        # platform the drop was found on, instead of skipping there.
        from conftest import make_dir_link

        link_base = tmp_path / "linked_skills"
        make_dir_link(link_base, real_base)
        via_link = sorted(name for name, _path in _iter_skill_files(link_base))
        assert via_link == ["flat-skill", "nested/deep-skill"]

    def test_skill_under_symlinked_intermediate_ancestor_is_resolved_fresh(self, tmp_path):
        """A skill whose ancestor is a link enumerates with its resolved path under the tree.

        The catalog walk resolves each node through ``os.path.realpath``, so a
        link/junction ancestor is followed to its target and the node is admitted
        only when the resolved SKILL.md lands under the base. Here an intermediate
        directory in the tree is a symlink to a sibling holding the skill; the
        skill must still be found and its resolved SKILL.md must land under the
        tree — proving a link ancestor is resolved and contained, not dropped.
        """
        from kiro_crew.skills import _iter_skill_files

        base = tmp_path / "skills"
        base.mkdir(parents=True, exist_ok=True)
        _create_skill(base, "plain", "---\nname: plain\n---\n# Plain")
        # Target lives INSIDE the base tree (so it is contained), and an
        # intermediate name links to it — the link node resolves to a path that
        # still lands under the base.
        _create_skill(base, "real/linked-child", "---\nname: linked-child\n---\n# Linked")
        # make_dir_link: a junction on Windows, a dir symlink on POSIX — keeps
        # the link-ancestor path exercised on Windows instead of skipping there.
        from conftest import make_dir_link

        make_dir_link(base / "via", base / "real")

        names = sorted(name for name, _path in _iter_skill_files(base))
        # The plain skill and the skill reached through the linked intermediate
        # both enumerate; the link node resolves to a contained path.
        assert "plain" in names
        assert "real/linked-child" in names

    def test_skill_under_ancestor_relocated_out_of_root_is_excluded(self, tmp_path):
        """A skill reachable only through an out-of-root ancestor is excluded.

        The catalog walk resolves each node through ``os.path.realpath`` and
        admits a SKILL.md only when its resolved path is under the allowed roots.
        An ancestor inside the base can link to a directory outside the roots;
        a skill reached only through that out-of-root ancestor resolves outside
        the roots and must not enumerate, while an in-tree sibling still does.
        """
        from conftest import make_dir_link
        from kiro_crew.skills import _iter_skill_files

        base = tmp_path / "skills"
        base.mkdir(parents=True, exist_ok=True)
        _create_skill(base, "plain", "---\nname: plain\n---\n# Plain")
        # A skill lives under outside/real/leaf, entirely outside the base tree.
        outside = tmp_path / "outside"
        _create_skill(outside, "real/leaf", "---\nname: leaf\n---\n# Leaf")
        # An ancestor name inside the base links to the out-of-root directory:
        # the only path to the leaf runs through a link whose resolved target
        # is outside the roots.
        make_dir_link(base / "aliased", outside / "real")

        names = sorted(name for name, _path in _iter_skill_files(base))
        # The in-tree plain skill enumerates; the leaf reachable only through
        # the out-of-root ancestor resolves outside the roots and is excluded.
        assert "plain" in names
        assert "leaf" not in names
        assert "real/leaf" not in names

    def test_dedupe_repeated_token(self, tmp_path):
        loader = self._loader(tmp_path)
        out = loader.resolve_dollar_skills("$oncall-handover and again $oncall-handover")
        assert len(out) == 1

    def test_unknown_token_skipped(self, tmp_path):
        loader = self._loader(tmp_path)
        assert loader.resolve_dollar_skills("$does-not-exist hello") == []

    def test_renamed_builtin_answers_to_its_old_name(self, tmp_path):
        """A saved hook or AGENTS.md line still says `$prepare-pr` (or the old
        nested key) after the skill became `kirocrew-dev/kirocrew-prepare-pr`."""
        _create_skill(
            tmp_path / "skills",
            "kirocrew-dev/kirocrew-prepare-pr",
            "---\nname: kirocrew-prepare-pr\ndescription: PR loop\n---\n# PR\nBody P.",
        )
        loader = self._loader(tmp_path)
        for text in ("run $prepare-pr", "run $kirocrew-dev/prepare-pr"):
            out = loader.resolve_dollar_skills(text)
            assert [name for _, name, _ in out] == ["kirocrew-dev/kirocrew-prepare-pr"], text
            assert "Body P." in out[0][2]

    def test_installed_skill_keeps_its_name_over_a_relocation_alias(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "prepare-pr", "---\nname: prepare-pr\n---\nUser's own.")
        _create_skill(
            skills_dir,
            "kirocrew-dev/kirocrew-prepare-pr",
            "---\nname: kirocrew-prepare-pr\n---\nBuilt-in.",
        )
        loader = self._loader(tmp_path)
        assert [name for _, name, _ in loader.resolve_dollar_skills("$prepare-pr")] == [
            "prepare-pr"
        ]

    def test_ambiguous_old_name_does_not_fall_through_to_the_relocation_alias(self, tmp_path):
        """Two user skills share the `prepare-pr` leaf: the token stays
        unresolved, as before the alias existed, instead of picking the
        renamed built-in."""
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "team-a/prepare-pr", "---\nname: a\n---\nA.")
        _create_skill(skills_dir, "team-b/prepare-pr", "---\nname: b\n---\nB.")
        _create_skill(
            skills_dir,
            "kirocrew-dev/kirocrew-prepare-pr",
            "---\nname: kirocrew-prepare-pr\n---\nBuilt-in.",
        )
        loader = self._loader(tmp_path)
        assert loader.resolve_dollar_skills("$prepare-pr") == []

    def test_no_dollar_returns_empty(self, tmp_path):
        loader = self._loader(tmp_path)
        assert loader.resolve_dollar_skills("plain message no trigger") == []

    def test_path_traversal_token_matches_nothing(self, tmp_path):
        loader = self._loader(tmp_path)
        # allowlist-only: no path is constructed from the token
        assert loader.resolve_dollar_skills("$../../etc/passwd") == []

    def test_uppercase_midtoken_truncates_match(self, tmp_path):
        loader = self._loader(tmp_path)
        # The $ charset is [a-z0-9/_-]: an uppercase letter ENDS the token. So
        # "$oncall-Handover" tokenizes as "oncall-" (trailing hyphen, no skill),
        # not "oncall-handover" — hence no match. (This is about charset
        # boundaries, NOT case-insensitive matching; see
        # test_leaf_match_is_case_insensitive for that.)
        out = loader.resolve_dollar_skills("$oncall-Handover")
        assert out == []

    def test_leaf_match_is_case_insensitive(self, tmp_path):
        # A skill whose leaf has uppercase letters is still matched by a
        # lowercase $token, because the leaf→name map lowercases both sides.
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "MyHandover",
            "---\nname: MyHandover\ndescription: d\n---\n# Body",
        )
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        out = loader.resolve_dollar_skills("$myhandover")
        assert len(out) == 1
        assert out[0][1].rsplit("/", 1)[-1].lower() == "myhandover"

    def test_uppercase_env_like_token_ignored(self, tmp_path):
        loader = self._loader(tmp_path)
        # $PATH / $5 must not match skill slugs
        assert loader.resolve_dollar_skills("echo $PATH and $5") == []

    def test_cap_enforced(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / "skills"
        for i in range(8):
            _create_skill(
                skills_dir,
                f"skill{i}",
                f"---\nname: skill{i}\ndescription: d{i}\n---\n# S{i}\nbody{i}",
            )
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        monkeypatch.setattr("kiro_crew.skills._MAX_DOLLAR_SKILLS", 5)
        msg = " ".join(f"$skill{i}" for i in range(8))
        out = loader.resolve_dollar_skills(msg)
        assert len(out) == 5

    def test_local_precedence_over_aim_extra_path(self, tmp_path):
        # Same leaf in local skills dir and an AIM-style extra path → local wins.
        local = tmp_path / "skills"
        _create_skill(local, "oncall-handover", "---\nname: oncall-handover\n---\nLOCAL")
        aim = tmp_path / "aim"
        _create_skill(
            aim,
            "WorkforceEmploymentKnowledgeBase/oncall-handover",
            "---\nname: WFE/oncall-handover\n---\nAIM",
        )
        cfg = KiroCrewConfig(skills=SkillsConfig(extra_paths=[str(aim)]))
        loader = _settled(SkillsLoader(skills_path=local, install_builtins=False, config=cfg))
        out = loader.resolve_dollar_skills("$oncall-handover")
        assert len(out) == 1
        assert "LOCAL" in out[0][2]

    def test_aim_only_skill_resolves(self, tmp_path):
        aim = tmp_path / "aim"
        _create_skill(
            aim,
            "WorkforceEmploymentKnowledgeBase/alarm-investigation",
            "---\nname: WFE/alarm-investigation\n---\nAIM ALARM",
        )
        cfg = KiroCrewConfig(skills=SkillsConfig(extra_paths=[str(aim)]))
        loader = _settled(
            SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False, config=cfg)
        )
        out = loader.resolve_dollar_skills("check $alarm-investigation")
        assert len(out) == 1
        assert out[0][1] == "WorkforceEmploymentKnowledgeBase/alarm-investigation"
        assert "AIM ALARM" in out[0][2]

    def test_implicit_aim_skills_root_resolves(self, tmp_path, monkeypatch):
        # An edition-contributed skill root (via the extra_skills() seam) must
        # resolve via $leaf WITHOUT being in config extra_paths — the loader
        # appends edition roots so the $skill resolver matches what the
        # /api/skills picker offers (frontend/backend parity).
        from kiro_crew.platform.defaults import DefaultMcpToolingProvider

        aim_root = tmp_path / "aim_skills"
        _create_skill(
            aim_root,
            "HoangvpPrivatePackage/personal-kb-sync",
            "---\nname: personal-kb-sync\ndescription: Sync\n---\n# Sync\nAIM KB BODY",
        )
        monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [aim_root])
        # No extra_paths configured — resolution must come from the edition root.
        loader = _settled(SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False))
        out = loader.resolve_dollar_skills("run $personal-kb-sync")
        assert len(out) == 1
        assert out[0][1] == "HoangvpPrivatePackage/personal-kb-sync"
        assert "AIM KB BODY" in out[0][2]

    def test_local_skill_wins_over_aim_root_on_leaf_collision(self, tmp_path, monkeypatch):
        # Local skills dir and an edition root both have a `grill` leaf → local
        # wins (edition roots are appended last, _iter dedupes by first-seen).
        from kiro_crew.platform.defaults import DefaultMcpToolingProvider

        aim_root = tmp_path / "aim_skills"
        _create_skill(aim_root, "SomePkg/grill", "---\nname: grill\n---\nAIM GRILL")
        monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [aim_root])
        local = tmp_path / "skills"
        _create_skill(local, "grill", "---\nname: grill\n---\nLOCAL GRILL")
        loader = _settled(SkillsLoader(skills_path=local, install_builtins=False))
        out = loader.resolve_dollar_skills("$grill")
        assert len(out) == 1
        assert "LOCAL GRILL" in out[0][2]

    def test_has_dollar_candidate(self, tmp_path):
        loader = self._loader(tmp_path)
        # real $skill-shaped tokens
        assert loader.has_dollar_candidate("run $oncall-handover")
        assert loader.has_dollar_candidate("$does-not-exist still counts")
        assert loader.has_dollar_candidate("$a/b nested")
        # incidental $ that the lowercase-led charset rejects
        assert not loader.has_dollar_candidate("it costs $5")
        assert not loader.has_dollar_candidate("price $42 today")
        assert not loader.has_dollar_candidate("echo $PATH")
        assert not loader.has_dollar_candidate("bare $ sign")
        assert not loader.has_dollar_candidate("no dollar here")
        assert not loader.has_dollar_candidate("")


class TestLazyLoadContext:
    """Lazy-load skill injection: pinned core + usage-ranked top-K + tail pointer."""

    def _make(self, tmp_path, n_on_demand=6, always=False):
        skills_dir = tmp_path / "skills"
        if always:
            _create_skill(
                skills_dir,
                "core-pinned",
                "---\nname: core-pinned\ndescription: core\nalways: true\n---\n# CorePinned\nAlways here.",
            )
        for i in range(n_on_demand):
            # Description is sized off the cap so it always exceeds it — a fixed
            # repetition count silently stops testing truncation if the cap rises.
            verbose = ("word%d " % i) * (_SHORT_DESC_CHARS // 4)
            _create_skill(
                skills_dir,
                f"od{i}",
                f"---\nname: od{i}\ndescription: {verbose}\n---\n# OD{i}\nBody {i}.",
            )
        return _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))

    def test_pinned_always_full_content(self, tmp_path):
        loader = self._make(tmp_path, n_on_demand=2, always=True)
        # Pinned (always:true) skills get full content in BOTH the legacy
        # (budget=None) and the lazy-load top-K (integer budget) paths.
        for budget in (None, 100_000):
            ctx = loader.get_context(budget=budget)
            assert "### Skill: core-pinned" in ctx, f"budget={budget}"
            assert "Always here." in ctx, f"budget={budget}"  # full body of pinned

    def test_budget_bounds_block_and_adds_tail_pointer(self, tmp_path):
        loader = self._make(tmp_path, n_on_demand=30)
        budget = 1200
        ctx = loader.get_context(budget=budget)
        # Regression (footer-truncation bug): get_context reserves room for the
        # footer + wrapper, so the FINAL block stays within budget and the
        # caller's backstop never chops the "...N more / skill_search" footer.
        assert len(ctx) <= budget
        # The tail that didn't fit is discoverable via skill_search.
        assert "more skill(s) not shown" in ctx
        assert "skill_search" in ctx

    def test_unbounded_shows_all(self, tmp_path):
        loader = self._make(tmp_path, n_on_demand=5)
        ctx = loader.get_context(budget=None)
        for i in range(5):
            assert f"**od{i}**" in ctx
        assert "more skill(s) not shown" not in ctx

    def test_ranking_prefers_used_skills(self, tmp_path):
        loader = self._make(tmp_path, n_on_demand=4)
        # Mark od3 as heavily used — it should sort to the front of the block.
        for _ in range(5):
            loader._usage.record("od3")
        # Ranking applies only on the opt-in (integer-budget) path; a large
        # budget shows all skills AND exercises the usage ordering.
        ctx = loader.get_context(budget=100_000)
        assert ctx.index("**od3**") < ctx.index("**od0**")

    @staticmethod
    def _shipped_and_user(tmp_path, n_shipped=12, n_user=1, hot=None, shipped_hits=1):
        """A skills dir of *n_shipped* shipped skills plus *n_user* cold user skills.

        Shipped is marked the way the builtin sync marks every copy it installs.
        User skills are aged past the new-skill boost window and never used, so
        rank alone puts every one of them last; each shipped skill carries
        *shipped_hits* hits, and *hot* (a shipped index) carries several more.
        """
        import os
        import time

        skills_dir = tmp_path / "skills"
        stale = time.time() - 2 * _NEW_SKILL_BOOST_WINDOW_SECS
        for i in range(n_user):
            _create_skill(
                skills_dir,
                f"zz-mine{i:02}",
                f"---\nname: zz-mine{i:02}\ndescription: my own procedure {i}\n---\n# Mine\n",
            )
            os.utime(skills_dir / f"zz-mine{i:02}" / "SKILL.md", (stale, stale))
        for i in range(n_shipped):
            _create_skill(
                skills_dir,
                f"shipped{i:02}",
                f"---\nname: shipped{i:02}\ndescription: shipped {i}\n---\n# S\n",
            )
            (skills_dir / f"shipped{i:02}" / ".builtin-skill-provenance").write_text("2:x")
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        for i in range(n_shipped):
            for _ in range(shipped_hits):
                loader._usage.record(f"shipped{i:02}")
        for _ in range(5 if hot is not None else 0):
            loader._usage.record(f"shipped{hot:02}")
        return loader

    @staticmethod
    def _pointer_names(loader) -> list[str]:
        text = loader.get_context(budget=100_000, discovery_only=True)
        return [line[2:].split(":", 1)[0] for line in text.splitlines() if line.startswith("- ")]

    def test_pointer_names_a_cold_user_skill_ahead_of_hot_shipped_ones(self, tmp_path):
        # A new user has no usage history, so the eight pointer names went to
        # shipped skills and the skill they wrote was never named at all.
        loader = self._shipped_and_user(tmp_path)
        names = self._pointer_names(loader)
        assert names[0] == "zz-mine00"
        # The cap is still eight names in total.
        assert len(names) == 8

    def test_pointer_names_every_user_skill_on_a_zero_usage_install(self, tmp_path):
        # Three user skills, sixty-odd shipped ones, an empty ledger: all three
        # user skills are named, and shipped skills fill the remaining slots.
        loader = self._shipped_and_user(tmp_path, n_shipped=60, n_user=3, shipped_hits=0)
        names = self._pointer_names(loader)
        assert names[:3] == ["zz-mine00", "zz-mine01", "zz-mine02"]
        assert len(names) == 8
        assert all(n.startswith("shipped") for n in names[3:])

    def test_pointer_keeps_a_hot_shipped_skill_under_a_large_user_tree(self, tmp_path):
        # The mirror case: ten user skills must not evict a shipped skill the
        # user actually relies on. At most six of the eight names are the user's
        # on provenance alone; the rest go to rank, which the hot skill wins.
        loader = self._shipped_and_user(tmp_path, n_shipped=12, n_user=10, hot=5)
        names = self._pointer_names(loader)
        assert len(names) == 8
        assert "shipped05" in names
        assert sum(n.startswith("zz-mine") for n in names) == 6

    def test_user_first_order_is_quota_then_rank_then_user_tail(self, tmp_path):
        loader = self._make(tmp_path, n_on_demand=0)
        loader._is_user_authored = lambda s: s["user"]  # type: ignore[method-assign]

        def row(name, user):
            return {"key": name, "user": user}

        # Rank order in: shipped and user rows interleaved, nine user rows.
        ranked = [
            row("s0", False),
            row("u0", True),
            row("u1", True),
            row("s1", False),
            *(row(f"u{i}", True) for i in range(2, 8)),
            row("s2", False),
            row("s3", False),
            row("u8", True),
        ]
        out = [r["key"] for r in loader._user_first(ranked)]
        # Six user rows lead; the two remaining head slots go to the best-ranked
        # of what is left (s0, s1 outrank the overflow user rows u6, u7); after
        # the head, the overflow user rows precede the shipped tail.
        assert out == ["u0", "u1", "u2", "u3", "u4", "u5", "s0", "s1", "u6", "u7", "u8", "s2", "s3"]

    def test_index_admits_a_cold_user_skill_before_hot_shipped_ones(self, tmp_path):
        loader = self._shipped_and_user(tmp_path)
        full = loader.get_context(budget=100_000)
        assert full.index("**zz-mine00**") < full.index("**shipped00**")
        # A budget with room for only a few rows still spends it on the user's
        # skill first; the shipped tail goes to the omission footer.
        tight = loader.get_context(budget=1200)
        assert "**zz-mine00**" in tight
        assert "more skill(s) not shown" in tight

    def test_user_authored_classification(self, tmp_path, monkeypatch):
        import kiro_crew.skills as skills_mod

        skills_dir = tmp_path / "skills"
        provider = tmp_path / "provider" / "app-skill"
        provider.mkdir(parents=True)
        (provider / "SKILL.md").write_text("---\nname: app-skill\ndescription: d\n---\n")
        from conftest import make_dir_link

        skills_dir.mkdir()
        make_dir_link(skills_dir / "app-skill", provider)
        _create_skill(skills_dir, "marked", "---\nname: marked\ndescription: d\n---\n")
        (skills_dir / "marked" / ".builtin-skill-provenance").write_text("2:x")
        _create_skill(skills_dir, "packaged", "---\nname: packaged\ndescription: d\n---\n")
        _create_skill(skills_dir, "team/mine", "---\nname: mine\ndescription: d\n---\n")
        monkeypatch.setattr(
            skills_mod, "_trusted_skill_roots", lambda: (str((tmp_path / "provider").resolve()),)
        )
        monkeypatch.setattr(skills_mod, "_packaged_skill_names", lambda: frozenset({"packaged"}))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        verdict = {str(r["key"]): loader._is_user_authored(r) for r in loader.scoped_skills()}
        assert verdict == {
            "app-skill": False,
            "marked": False,
            "packaged": False,
            # A nested key is not an app namespace: the user's own tree.
            "team/mine": True,
        }
        # A confined project row is the user's without touching its path.
        assert loader._is_user_authored({"key": "x", "path": "/nonexistent", "confine_root": "/p"})

    def test_short_desc_truncated(self, tmp_path):
        loader = self._make(tmp_path, n_on_demand=1)
        # Description truncation applies only on the opt-in (integer-budget) path.
        ctx = loader.get_context(budget=100_000)
        # The verbose description is truncated with an ellipsis.
        assert "..." in ctx

    def test_short_desc_cuts_on_a_word_boundary(self, tmp_path):
        loader = self._make(tmp_path, n_on_demand=1)
        # A space falls in the last fifth of the budget, so the cut lands there
        # and the line does not end mid-word.
        desc = "alpha " * 200
        out = loader._short_desc(desc)
        assert out.endswith("alpha...")
        assert len(out) <= _SHORT_DESC_CHARS + len("...")

    def test_short_desc_hard_cuts_a_single_long_token(self, tmp_path):
        loader = self._make(tmp_path, n_on_demand=1)
        # No word boundary to cut on -- fall back to a hard cut rather than
        # returning the whole token or an empty string.
        out = loader._short_desc("x" * (_SHORT_DESC_CHARS + 50))
        assert out == "x" * _SHORT_DESC_CHARS + "..."

    def test_short_desc_leaves_a_description_under_the_cap_alone(self, tmp_path):
        loader = self._make(tmp_path, n_on_demand=1)
        # The cap is a guardrail: a typical-length description is untouched.
        desc = "Drive a change to a review-ready pull request."
        assert loader._short_desc(desc) == desc

    def test_budget_none_is_legacy_full_dump(self, tmp_path):
        # Opt-in OFF (budget=None): legacy block — old header, every skill shown,
        # no top-K / tail-pointer, no skill_search.
        loader = self._make(tmp_path, n_on_demand=3)
        ctx = loader.get_context(budget=None)
        assert "If a user request relates to any skill below" in ctx
        assert "The most-used skills are listed below" not in ctx
        assert "more skill(s) not shown" not in ctx
        assert "skill_search" not in ctx
        for i in range(3):
            assert f"**od{i}**" in ctx

    def test_skills_config_max_triggered_default_is_zero(self):
        assert SkillsConfig().max_triggered == 0

    def test_budget_none_truncates_long_description(self, tmp_path):
        # On the budget=None (legacy) path, an over-cap description is truncated.
        skills_dir = tmp_path / "skills"
        long_desc = "x" * (_SHORT_DESC_CHARS + 40)
        _create_skill(
            skills_dir,
            "longdesc",
            f"---\nname: longdesc\ndescription: {long_desc}\n---\n# LD\nBody.",
        )
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        ctx = loader.get_context(budget=None)
        # The full description should NOT appear verbatim.
        assert long_desc not in ctx
        # Truncated with ellipsis.
        assert "..." in ctx or "…" in ctx


class TestSearchSkills:
    def test_search_matches_by_description(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "deployer",
            "---\nname: deployer\ndescription: fix a broken deployment pipeline\n---\n# D\n",
        )
        _create_skill(
            skills_dir,
            "weather",
            "---\nname: weather\ndescription: get the forecast\n---\n# W\n",
        )
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        hits = loader.search_skills("deployment")
        assert [h["key"] for h in hits] == ["deployer"]

    def test_search_empty_query(self, tmp_path):
        loader = _settled(SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False))
        assert loader.search_skills("") == []
        assert loader.search_skills("   ") == []

    def test_search_falls_back_to_body(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "hidden",
            "---\nname: hidden\ndescription: nothing relevant here\n---\n# H\nThis mentions kubernetes internally.",
        )
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        hits = loader.search_skills("kubernetes")
        assert [h["key"] for h in hits] == ["hidden"]

    def test_search_does_not_record_usage(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "s1", "---\nname: s1\ndescription: alpha beta\n---\n# S1\n")
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False))
        loader.search_skills("alpha")
        assert loader._usage.score("s1")[0] == 0.0  # searching is not using


class TestDisabledAppSkillsAreNotTriggered:
    """Disabling an app must actually stop its bundled skills loading.

    The skill tree an app bundles under ``skills/<app>/`` was never gated on the
    app's enabled state, so a disabled app's skills stayed in the matching index
    and kept firing into every turn's context on generic trigger words -- burning
    tokens and polluting the prompt for an app the user explicitly opted out of,
    with no visible reason.
    """

    @staticmethod
    def _write_app_skill(skills: Path, app: str, name: str, triggers: str) -> None:
        d = skills / app / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\ntriggers: {triggers}\n---\n\nbody\n"
        )

    @staticmethod
    def _apps(monkeypatch, **enabled: bool) -> None:
        """Stub the app registry: name -> enabled."""
        import kiro_crew.apps.manager as mgr

        monkeypatch.setattr(
            mgr,
            "list_apps",
            lambda: [{"name": n, "enabled": e} for n, e in enabled.items()],
        )

    def test_a_disabled_apps_skill_does_not_trigger(self, tmp_path, monkeypatch):
        skills = tmp_path / "skills"
        self._write_app_skill(skills, "deploy_web", "artifact-deploy", "deploy")
        self._apps(monkeypatch, deploy_web=False)
        loader = _settled(
            SkillsLoader(
                skills_path=skills,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        assert loader.get_triggered_skills("please deploy this") == []

    def test_an_enabled_apps_skill_still_triggers(self, tmp_path, monkeypatch):
        skills = tmp_path / "skills"
        self._write_app_skill(skills, "deploy_web", "artifact-deploy", "deploy")
        self._apps(monkeypatch, deploy_web=True)
        loader = _settled(
            SkillsLoader(
                skills_path=skills,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        assert loader.get_triggered_skills("please deploy this") == ["deploy_web/artifact-deploy"]

    def test_only_the_disabled_apps_skill_is_withheld(self, tmp_path, monkeypatch):
        """One disabled app must not silence a sibling app's skill."""
        skills = tmp_path / "skills"
        self._write_app_skill(skills, "deploy_web", "artifact-deploy", "deploy")
        self._write_app_skill(skills, "ops_mc", "ops-mission-control", "deploy")
        self._apps(monkeypatch, deploy_web=False, ops_mc=True)
        loader = _settled(
            SkillsLoader(
                skills_path=skills,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        assert loader.get_triggered_skills("please deploy this") == ["ops_mc/ops-mission-control"]

    def test_a_plain_user_skill_is_never_gated(self, tmp_path, monkeypatch):
        """A skill owned by no app has no enablement to consult."""
        skills = tmp_path / "skills"
        d = skills / "my-notes"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: my-notes\ndescription: d\ntriggers: deploy\n---\n\nb\n"
        )
        self._apps(monkeypatch, deploy_web=False)
        loader = _settled(
            SkillsLoader(
                skills_path=skills,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        assert loader.get_triggered_skills("please deploy this") == ["my-notes"]

    def test_an_unreadable_app_registry_hides_nothing(self, tmp_path, monkeypatch):
        """Fail OPEN here, deliberately: a transient read error must not silently
        strip an enabled app's skills out of context."""
        import kiro_crew.apps.manager as mgr

        skills = tmp_path / "skills"
        self._write_app_skill(skills, "deploy_web", "artifact-deploy", "deploy")

        def _boom():
            raise OSError("installed.json unreadable")

        monkeypatch.setattr(mgr, "list_apps", _boom)
        loader = _settled(
            SkillsLoader(
                skills_path=skills,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        assert loader.get_triggered_skills("please deploy this") == ["deploy_web/artifact-deploy"]

    @staticmethod
    def _link_shipped_builtin_skill(skills: Path) -> str:
        """Flat-link a REAL shipped builtin's skill into *skills*, as bridges does.

        Uses ``auto-improvement``'s ``ai-discover``: the flat name sorts before
        the app name, so the flat registration is the one ``_iter_skill_files``'s
        ``seen_real`` dedup keeps -- and its realpath resolves into the PACKAGE
        tree (``apps/builtins/auto_improvement/skills/ai-discover``), not the
        data-home apps root. Returns the app's manifest name.
        """
        import kiro_crew.skills as skills_mod

        target = (
            Path(skills_mod.__file__).parent
            / "apps"
            / "builtins"
            / "auto_improvement"
            / "skills"
            / "ai-discover"
        )
        skills.mkdir(parents=True, exist_ok=True)
        try:
            (skills / "ai-discover").symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable on this platform")
        return "auto-improvement"

    def test_a_disabled_builtins_package_tree_skill_is_gated(self, tmp_path, monkeypatch):
        """A shipped builtin registers its skills
        straight out of the package tree, and the flat link -- whose name says
        nothing -- can be the registration the walk keeps. Ownership must resolve
        through the builtin's manifest, not only the data-home apps root."""
        skills = tmp_path / "skills"
        app = self._link_shipped_builtin_skill(skills)
        self._apps(monkeypatch, **{app: False})
        loader = _settled(
            SkillsLoader(
                skills_path=skills,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        assert loader.get_triggered_skills("find hotspots for me") == []

    def test_an_enabled_builtins_package_tree_skill_still_triggers(self, tmp_path, monkeypatch):
        """The gate's other direction: enabling the builtin restores matching."""
        skills = tmp_path / "skills"
        app = self._link_shipped_builtin_skill(skills)
        self._apps(monkeypatch, **{app: True})
        loader = _settled(
            SkillsLoader(
                skills_path=skills,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        assert loader.get_triggered_skills("find hotspots for me") == ["ai-discover"]

    def test_disabled_app_skills_excluded_from_list_skills_and_context(self, tmp_path, monkeypatch):
        """Disabled app skills must not appear in list_skills or get_context index."""
        skills = tmp_path / "skills"
        self._write_app_skill(skills, "deploy_web", "artifact-deploy", "deploy")
        self._write_app_skill(skills, "ops_mc", "ops-mission-control", "deploy")
        self._apps(monkeypatch, deploy_web=False, ops_mc=True)
        loader = _settled(
            SkillsLoader(
                skills_path=skills,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        listed_keys = [s["key"] for s in loader.list_skills()]
        assert "deploy_web/artifact-deploy" not in listed_keys
        assert "ops_mc/ops-mission-control" in listed_keys

        context = loader.get_context(budget=10000)
        assert "artifact-deploy" not in context
        assert "ops-mission-control" in context

    def test_disabled_app_always_skill_is_excluded(self, tmp_path, monkeypatch):
        """Disabled app skills with always: true must not be injected."""
        skills = tmp_path / "skills"
        d = skills / "deploy_web" / "always-helper"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: always-helper\nalways: true\n---\n\nBody of always helper\n"
        )
        self._apps(monkeypatch, deploy_web=False)
        loader = _settled(
            SkillsLoader(
                skills_path=skills,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        assert loader.get_always_skills() == []

        loader._invalidate_iter_cache()
        self._apps(monkeypatch, deploy_web=True)
        assert _settled(loader).get_always_skills() == ["deploy_web/always-helper"]

    def test_disabled_app_dollar_skill_is_not_resolved(self, tmp_path, monkeypatch):
        """Explicit $skill invocation must not resolve a disabled app's skill."""
        skills = tmp_path / "skills"
        self._write_app_skill(skills, "deploy_web", "artifact-deploy", "deploy")
        self._apps(monkeypatch, deploy_web=False)
        loader = _settled(
            SkillsLoader(
                skills_path=skills,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        assert loader.resolve_dollar_skills("run $artifact-deploy please") == []

        loader._invalidate_iter_cache()
        self._apps(monkeypatch, deploy_web=True)
        res = _settled(loader).resolve_dollar_skills("run $artifact-deploy please")
        assert len(res) == 1
        assert res[0][1] == "deploy_web/artifact-deploy"

    def test_disabled_app_skill_is_excluded_from_search(self, tmp_path, monkeypatch):
        """skill_search must not return results from disabled apps."""
        skills = tmp_path / "skills"
        self._write_app_skill(skills, "deploy_web", "artifact-deploy", "deploy")
        self._apps(monkeypatch, deploy_web=False)
        loader = _settled(
            SkillsLoader(
                skills_path=skills,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        assert loader.search_skills("artifact-deploy") == []

        loader._invalidate_iter_cache()
        self._apps(monkeypatch, deploy_web=True)
        results = _settled(loader).search_skills("artifact-deploy")
        assert len(results) == 1
        assert results[0]["key"] == "deploy_web/artifact-deploy"


class TestStripFrontmatterCloserParity:
    """strip_frontmatter's closer must accept everything the display
    parser's ``column0_fence`` grammar accepts. A closer the parser tolerates
    but the stripper rejects shows parsed metadata in the UI while the whole
    frontmatter block leaks to the model."""

    BODY = "# Heading\n\nInstruction text."

    def _doc(self, closer: str) -> str:
        return f"---\ndescription: test skill\n{closer}\n{self.BODY}"

    def test_strict_closer_still_strips(self) -> None:
        out = SkillsLoader.strip_frontmatter(self._doc("---"))
        assert out == self.BODY
        assert "description:" not in out

    @pytest.mark.parametrize("closer", ["--- ", "---junk", "--- comment text"])
    def test_lenient_closer_strips_whatever_the_parser_parses(self, closer: str) -> None:
        """The invariant itself: for the same document, the display parser
        reads the fields AND the stripper removes the block."""
        from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter

        doc = self._doc(closer)
        fields = parse_frontmatter(doc, SKILL_LOADER)
        assert fields.get("description") == "test skill", (
            "premise broken: the display parser no longer tolerates this "
            "closer — re-check _COLUMN0_BLOCK_RE before touching the stripper"
        )
        out = SkillsLoader.strip_frontmatter(doc)
        assert "description:" not in out, f"frontmatter leaked past closer {closer!r}"
        assert out == self.BODY

    def test_closer_at_eof_without_newline_strips(self) -> None:
        out = SkillsLoader.strip_frontmatter("---\ndescription: test skill\n---")
        assert out == ""

    def test_crlf_fences_strip_whatever_the_parser_parses(self) -> None:
        """Line-ending side of the same invariant: the display dialect
        tolerates a carriage return before each fence newline, so the
        stripper must too — a CRLF document whose fields render in the UI
        must not leak its whole block to the model."""
        from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter

        doc = f"---\r\ndescription: test skill\r\n---\r\n{self.BODY}"
        fields = parse_frontmatter(doc, SKILL_LOADER)
        assert fields.get("description") == "test skill", (
            "premise broken: the display parser no longer tolerates a CRLF "
            "fence — re-check _COLUMN0_BLOCK_RE before touching the stripper"
        )
        out = SkillsLoader.strip_frontmatter(doc)
        assert "description:" not in out, "frontmatter leaked past a CRLF fence"
        assert out == self.BODY

    def test_no_closer_leaves_content_unchanged(self) -> None:
        doc = "---\ndescription: dangling opener, no closer"
        assert SkillsLoader.strip_frontmatter(doc) == doc

    def test_plain_markdown_untouched(self) -> None:
        assert SkillsLoader.strip_frontmatter(self.BODY) == self.BODY

    def test_dashes_inside_body_do_not_end_early(self) -> None:
        """A ``---`` ruler AFTER the real closer stays in the body."""
        doc = f"---\ndescription: test skill\n---\nintro\n---\n{self.BODY}"
        out = SkillsLoader.strip_frontmatter(doc)
        assert out.startswith("intro")
        assert "---" in out

    def test_leading_whitespace_opener_parity_both_sides_reject(self) -> None:
        """Opener-side guard: today BOTH the display dialect (column0_fence)
        and the stripper reject a leading-whitespace opener, so nothing
        parses and nothing strips — parity holds in the reject direction.
        If the display dialect ever goes lenient on the opener (e.g.
        switching SKILL_LOADER to leading_ws_fence), the premise assertion
        here goes red, forcing the stripper's opener to be revisited in the
        same change instead of silently reopening the leak."""
        from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter

        doc = f" ---\ndescription: test skill\n---\n{self.BODY}"
        fields = parse_frontmatter(doc, SKILL_LOADER)
        assert not fields.get("description"), (
            "premise broken: the display dialect now tolerates a "
            "leading-whitespace opener — strip_frontmatter's opener "
            "(startswith('---')) must be widened in the same change"
        )
        assert SkillsLoader.strip_frontmatter(doc) == doc
