"""``SkillsLoader``'s read side (``kiro_crew.skills``).

This file pins listing, loading and the context block, bounded and link-refusing body
reads, which reads credit the usage ledger, skills from extra paths, and the quarantine
of a relocated builtin skill's old copy. The family's other files are described in
``test_skills.py``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from skills_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _close_skills_loaders,
    _create_skill,
    _isolate_extra_paths,
    _settled,
)

from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.skill_usage import SkillUsageLedger
from kiro_crew.skills import SkillsLoader


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
        # flake-ok: pre-existing real-clock arm of the ledger debounce moved verbatim; it only keeps the background flush from starting during the test
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
