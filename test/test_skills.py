"""Tests for skills module."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.skill_usage import SkillUsageLedger
from kiro_crew.skills import _SHORT_DESC_CHARS, SkillsLoader


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


class TestNoteToolRead:
    """Only content-delivering reads credit the ledger."""

    def _loader(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir, "alpha", "---\nname: alpha\ndescription: A\n---\n# Alpha\n"
        )
        _create_skill(
            skills_dir, "beta", "---\nname: beta\ndescription: B\n---\n# Beta\n"
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        loader._usage = SkillUsageLedger(tmp_path / "skill-usage.json")
        return loader, skills_dir

    def _read(self, loader, **kw):
        """Resolve then credit, the way the ACP layer does across two events."""
        keys = loader.resolve_tool_read_keys(**kw)
        loader.credit_skill_reads(keys)
        return keys

    def test_read_tool_path_credits_a_hit(self, tmp_path):
        loader, skills_dir = self._loader(tmp_path)
        path = str(skills_dir / "alpha" / "SKILL.md")
        assert self._read(loader, tool_name="fs_read", raw_params={"path": path}) == [
            "alpha"
        ]
        assert loader._usage.score("alpha")[0] == 1.0
        assert loader._usage.score("beta")[0] == 0.0

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
        loader._iter_cache = None  # re-walk so the alias is served
        recorded = self._read(
            loader, tool_name="fs_read", raw_params={"path": str(link_dir / "SKILL.md")}
        )
        canonical = loader._served_key_by_realpath()[
            str((skills_dir / "alpha" / "SKILL.md").resolve())
        ]
        assert recorded == [canonical] == ["alpha"]


class TestSkillsLoader:
    def test_list_empty(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.list_skills() == []

    def test_list_skills(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "weather",
            "---\nname: weather\ndescription: Get weather info\n---\n# Weather\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        skills = loader.list_skills()
        assert len(skills) == 1
        assert skills[0]["name"] == "weather"
        assert skills[0]["description"] == "Get weather info"

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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        ctx = loader.get_context()
        assert "[Skills:]" in ctx
        assert "Memory" in ctx
        assert "Use it." in ctx

    def test_get_context_empty(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "empty", install_builtins=False)
        assert loader.get_context() == ""

    def test_mutators_invalidate_iter_cache(self, tmp_path):
        """create/update/delete must invalidate the TTL'd _iter cache so a
        subsequent list_skills() reflects the change immediately (not after the
        TTL). Each step primes the cache via list_skills() first, mutates, then
        re-lists in the same tick — without invalidation these would be stale."""
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        body = "---\nname: x\ndescription: X\n---\n# X\n"

        # create: prime empty, create, must appear now
        assert loader.list_skills() == []
        assert loader.create_skill("alpha", body) is True
        assert any(s["key"] == "alpha" for s in loader.list_skills())

        # update: prime, overwrite, new description must appear now
        assert loader.update_skill("alpha", body.replace("X", "Updated")) is True
        assert any(s["description"] == "Updated" for s in loader.list_skills())

        # delete: prime, remove, must disappear now
        assert loader.delete_skill("alpha") is True
        assert all(s["key"] != "alpha" for s in loader.list_skills())

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

        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        body = "---\nname: x\ndescription: X\n---\n# X\n"
        assert loader.create_skill("alpha", body) is True
        # Prime the frontmatter cache with description "X".
        assert any(s["description"] == "X" for s in loader.list_skills())
        skill_file = tmp_path / "skills" / "alpha" / "SKILL.md"
        pinned = skill_file.stat().st_mtime_ns

        assert loader.update_skill("alpha", body.replace("X", "Updated")) is True
        # Force the mtime back so an mtime-keyed cache cannot tell the file changed.
        os.utime(skill_file, ns=(pinned, pinned))
        assert any(s["description"] == "Updated" for s in loader.list_skills())

    def test_update_auto_skill_invalidates_cache_same_mtime(self, tmp_path):
        """update_auto_skill (the auto-skill refine path) must invalidate cached
        frontmatter too, or a refined skill's new triggers/description stay stale
        when the rewrite lands in the same mtime tick as the priming read."""
        import os

        from kiro_crew.skills import AutoSkillProvenance

        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
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
        assert any(s["description"] == "Original" for s in loader.list_skills())
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
        assert any(s["description"] == "Refined" for s in loader.list_skills())


class TestRepoScope:
    """``repo_scope:`` frontmatter mechanically suppresses a skill unless the
    CWD (or an ancestor) contains the named relative path — the loader-enforced
    gate for repo-specific skills with destructive instructions (PR #353
    arbiter: prose scope guards are probabilistic; containment must be
    mechanical before shipping to every install)."""

    def _write_skill(self, root: Path, name: str, scope: str | None) -> None:
        d = root / name
        d.mkdir(parents=True)
        fm = f"---\nname: {name}\ntriggers: zebra quokka\n"
        if scope:
            fm += f"repo_scope: {scope}\n"
        (d / "SKILL.md").write_text(fm + "---\nbody")

    def test_scoped_skill_suppressed_outside_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skills = tmp_path / "skills"
        self._write_skill(skills, "repo-only", "src/kiro_crew")
        loader = SkillsLoader(skills_path=skills, install_builtins=False)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        monkeypatch.chdir(outside)
        assert loader.get_triggered_skills("zebra quokka") == []

    def test_scoped_skill_eligible_inside_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skills = tmp_path / "skills"
        self._write_skill(skills, "repo-only", "src/kiro_crew")
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = SkillsLoader(skills_path=skills, install_builtins=False, config=cfg)
        repo = tmp_path / "checkout"
        (repo / "src" / "kiro_crew").mkdir(parents=True)
        subdir = repo / "website"
        subdir.mkdir()
        monkeypatch.chdir(subdir)  # ancestor contains src/kiro_crew
        assert loader.get_triggered_skills("zebra quokka") == ["repo-only"]

    def test_unscoped_skill_unaffected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skills = tmp_path / "skills"
        self._write_skill(skills, "anywhere", None)
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = SkillsLoader(skills_path=skills, install_builtins=False, config=cfg)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        monkeypatch.chdir(outside)
        assert loader.get_triggered_skills("zebra quokka") == ["anywhere"]

    def test_traversal_scope_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ".." in the scope must never widen the check — fails closed.
        skills = tmp_path / "skills"
        self._write_skill(skills, "sneaky", "../..")
        loader = SkillsLoader(skills_path=skills, install_builtins=False)
        monkeypatch.chdir(tmp_path)
        assert loader.get_triggered_skills("zebra quokka") == []


class TestRelocatedSkillCleanup:
    """Skills moved into skills/kirocrew-dev/ must have their old FLAT copies
    removed from loader discovery on existing installs — otherwise an upgrade
    leaves two divergent copies matched nondeterministically by trigger overlap
    (the dual-copy drift PR #353's arbiter blocked). The flat copy may carry
    USER EDITS (the mtime-preserving sync deliberately protects those), so it
    is never deleted: its SKILL.md is renamed to SKILL.md.pre-relocation —
    undiscoverable, but every byte preserved. Quarantine only happens when the
    nested replacement is verifiably present."""

    def test_flat_copy_quarantined_when_nested_present(self, tmp_path: Path) -> None:
        from kiro_crew.skills import _ensure_builtin_skills

        base = tmp_path / "skills"
        old = base / "prepare-pr"
        old.mkdir(parents=True)
        (old / "SKILL.md").write_text("---\nname: prepare-pr\n---\nUSER-EDITED flat copy")
        (old / "scripts").mkdir()
        (old / "scripts" / "helper.py").write_text("# user script")
        new = base / "kirocrew-dev" / "prepare-pr"
        new.mkdir(parents=True)
        (new / "SKILL.md").write_text("---\nname: prepare-pr\n---\nnested copy")

        _ensure_builtin_skills(base)

        # No longer discoverable as a skill...
        assert not (old / "SKILL.md").exists()
        # ...but nothing was deleted: user edits and scripts preserved on disk.
        quarantined = old / "SKILL.md.pre-relocation"
        assert quarantined.read_text(encoding="utf-8").endswith("USER-EDITED flat copy")
        assert (old / "scripts" / "helper.py").exists()
        assert (new / "SKILL.md").exists()

    def test_repeated_migration_never_overwrites_prior_quarantine(
        self, tmp_path: Path
    ) -> None:
        # HIGH regression (GPT 5.6): a rollback/reinstall can recreate
        # SKILL.md AFTER a prior migration quarantined a user-edited copy.
        # The second migration must NOT os.replace over the first quarantine
        # — both preserved copies must survive under distinct names.
        from kiro_crew.skills import _ensure_builtin_skills

        base = tmp_path / "skills"
        old = base / "prepare-pr"
        old.mkdir(parents=True)
        new = base / "kirocrew-dev" / "prepare-pr"
        new.mkdir(parents=True)
        (new / "SKILL.md").write_text("---\nname: prepare-pr\n---\nnested copy")

        # First migration quarantines the user's original edits.
        (old / "SKILL.md").write_text("---\nname: prepare-pr\n---\nFIRST user edit")
        _ensure_builtin_skills(base)
        first = old / "SKILL.md.pre-relocation"
        assert first.read_text(encoding="utf-8").endswith("FIRST user edit")

        # Rollback recreates SKILL.md with different content; migration re-runs.
        (old / "SKILL.md").write_text("---\nname: prepare-pr\n---\nSECOND rollback copy")
        _ensure_builtin_skills(base)

        # Both preserved copies survive; nothing was overwritten.
        assert first.read_text(encoding="utf-8").endswith("FIRST user edit")
        second = old / "SKILL.md.pre-relocation.2"
        assert second.read_text(encoding="utf-8").endswith("SECOND rollback copy")
        assert not (old / "SKILL.md").exists()

    def test_flat_copy_untouched_when_nested_missing(self, tmp_path: Path) -> None:
        # Fail-safe: if the nested replacement never synced, the flat copy is
        # the ONLY working copy — it must stay discoverable.
        from kiro_crew.skills import _ensure_builtin_skills

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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg)
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

    def test_create_then_list(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        loader.create_skill(
            "alpha",
            "---\nname: alpha\ndescription: Alpha skill\n---\n# Alpha\n",
        )
        loader.create_skill(
            "beta",
            "---\nname: beta\ndescription: Beta skill\n---\n# Beta\n",
        )
        skills = loader.list_skills()
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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg)

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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg)

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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg)

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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg)

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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg)

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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg)

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
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.find_similar("anything") is None

    def test_returns_none_when_no_overlap(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "weather",
            "---\nname: weather\ndescription: Get weather info for a city\n---\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        assert loader.find_similar("deploy kubernetes service") is None

    def test_detects_near_duplicate(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "ssh-timber",
            "---\nname: ssh-timber\ndescription: SSH chained log search on Timber production hosts\n---\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
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
        content = (tmp_path / "skills" / "auto" / "refine-me" / "SKILL.md").read_text(encoding="utf-8")
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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
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
        auto_only = loader.list_auto_skills()
        assert len(auto_only) == 1
        assert auto_only[0]["key"] == "auto/generated-one"


class TestAutoNameFromTitleTruncation:
    """Regression test for #6: trailing hyphen after truncation would fail regex."""

    def test_trailing_hyphen_stripped_after_truncation(self):
        from kiro_crew.skills import _auto_name_from_title

        # Build a title where the 62-char boundary lands in the middle of a
        # word-separator run ("-") that would otherwise leave a trailing
        # hyphen and silently fail _AUTO_NAME_PATTERN.
        # 60 alphanumerics + 2 non-alphanumerics -> "a" * 60 + "-x"
        # After truncation at [:62], you get "a"*60 + "-x" — 62 chars, still valid.
        # A tricker case: 61 alphanumerics + non-alphanum + alphanum
        # -> "a" * 61 + "-b" -> after re.sub + strip + truncate[:62] ->
        # "a"*61 + "-" which ends in a hyphen.
        title = "a" * 61 + " b"  # Space becomes hyphen during sanitization
        slug = _auto_name_from_title(title)
        # Post-fix: trailing hyphen stripped, slug is "a"*61 -> 61 chars, valid
        assert slug
        assert not slug.endswith("-")
        assert slug == "a" * 61

    def test_normal_title_unaffected(self):
        from kiro_crew.skills import _auto_name_from_title

        assert _auto_name_from_title("Debug Timber logs via SSH") == "debug-timber-logs-via-ssh"

    def test_empty_and_invalid_inputs_still_return_empty(self):
        from kiro_crew.skills import _auto_name_from_title

        assert _auto_name_from_title("") == ""
        assert _auto_name_from_title("!!!") == ""
        # Single character is below the min length (3)
        assert _auto_name_from_title("a") == ""


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
        content = (tmp_path / "skills" / "auto" / "preserved-ts" / "SKILL.md").read_text(encoding="utf-8")
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
        loader = SkillsLoader(
            skills_path=local,
            install_builtins=False,
            config=_cfg_with_extra([str(extra)]),
        )
        names = {s["name"] for s in loader.list_skills()}
        assert "roaring" in names

    def test_local_takes_precedence(self, tmp_path):
        local = tmp_path / "local"
        extra = tmp_path / "extra"
        _create_skill(local, "dup", "---\nname: dup\ndescription: LOCAL\n---\n# Local\n")
        _create_skill(extra, "dup", "---\nname: dup\ndescription: EXTRA\n---\n# Extra\n")
        loader = SkillsLoader(
            skills_path=local,
            install_builtins=False,
            config=_cfg_with_extra([str(extra)]),
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
        loader = SkillsLoader(
            skills_path=local,
            install_builtins=False,
            config=_cfg_with_extra([str(extra)]),
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
        loader = SkillsLoader(
            skills_path=skills_dir,
            install_builtins=False,
            config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)

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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        calls = {"n": 0}
        orig = loader._iter_uncached

        def _counting():
            calls["n"] += 1
            return orig()

        monkeypatch.setattr(loader, "_iter_uncached", _counting)
        for _ in range(5):
            loader.get_triggered_skills("shorten this url")
        # 5 messages, but the underlying walk ran at most once (TTL cache).
        assert calls["n"] <= 1

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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg)
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        triggered = loader.get_triggered_skills("shorten url")
        assert len(triggered) == 2


class TestResolveDollarSkills:
    """$skillname inline trigger resolution (allowlist, all sources)."""

    def _loader(self, tmp_path, **extra):
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
        return SkillsLoader(skills_path=skills_dir, install_builtins=False, **extra)

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

    def test_dedupe_repeated_token(self, tmp_path):
        loader = self._loader(tmp_path)
        out = loader.resolve_dollar_skills("$oncall-handover and again $oncall-handover")
        assert len(out) == 1

    def test_unknown_token_skipped(self, tmp_path):
        loader = self._loader(tmp_path)
        assert loader.resolve_dollar_skills("$does-not-exist hello") == []

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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
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
        loader = SkillsLoader(skills_path=local, install_builtins=False, config=cfg)
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
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False, config=cfg)
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
        monkeypatch.setattr(
            DefaultMcpToolingProvider, "extra_skills", lambda self: [aim_root]
        )
        # No extra_paths configured — resolution must come from the edition root.
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
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
        monkeypatch.setattr(
            DefaultMcpToolingProvider, "extra_skills", lambda self: [aim_root]
        )
        local = tmp_path / "skills"
        _create_skill(local, "grill", "---\nname: grill\n---\nLOCAL GRILL")
        loader = SkillsLoader(skills_path=local, install_builtins=False)
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
        return SkillsLoader(skills_path=skills_dir, install_builtins=False)

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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
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
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        hits = loader.search_skills("deployment")
        assert [h["key"] for h in hits] == ["deployer"]

    def test_search_empty_query(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.search_skills("") == []
        assert loader.search_skills("   ") == []

    def test_search_falls_back_to_body(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "hidden",
            "---\nname: hidden\ndescription: nothing relevant here\n---\n# H\nThis mentions kubernetes internally.",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        hits = loader.search_skills("kubernetes")
        assert [h["key"] for h in hits] == ["hidden"]

    def test_search_does_not_record_usage(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "s1", "---\nname: s1\ndescription: alpha beta\n---\n# S1\n")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        loader.search_skills("alpha")
        assert loader._usage.score("s1")[0] == 0.0  # searching is not using
