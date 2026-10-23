"""Writing skills with ``SkillsLoader`` (``kiro_crew.skills``).

This file pins create, update and delete with their name-safety refusals, the provenance
frontmatter of an auto skill, and refining and listing auto skills while keeping
``created_at``. Near-duplicate detection, the ``auto/`` namespace check and
``create_auto_skill`` are pinned in ``test_skills.py``, which also describes the
family's other files.
"""

from __future__ import annotations

from skills_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _close_skills_loaders,
    _create_skill,
    _isolate_extra_paths,
    _settled,
)

from kiro_crew.skills import SkillsLoader


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
