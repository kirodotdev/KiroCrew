"""`list_skills()` carries what the injection-cost control needs.

The Skills page has to answer three things per skill: is it sent in full or as a
pointer, how big is it, and how often does its trigger fire. Cost is size x
matches, so a listing that omits either number leaves the user unable to weigh
the decision the control asks them to make.
"""

from pathlib import Path

import pytest

from kiro_crew.skills import SkillsLoader


def _write(root: Path, name: str, *, inject: str | None = None, body: str = "body") -> Path:
    d = root / name
    d.mkdir(parents=True)
    fm = f"---\nname: {name}\ndescription: d\ntriggers: zebra\n"
    if inject is not None:
        fm += f"inject_on_trigger: {inject}\n"
    (d / "SKILL.md").write_text(fm + f"---\n{body}")
    return d / "SKILL.md"


def _one(loader: SkillsLoader, key: str) -> dict:
    return next(s for s in loader.list_skills() if s["key"] == key)


class TestInjectOnTriggerField:
    def test_absent_field_reports_injecting(self, tmp_path: Path) -> None:
        _write(tmp_path / "skills", "plain")
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert _one(loader, "plain")["inject_on_trigger"] is True

    def test_explicit_false_reports_pointer(self, tmp_path: Path) -> None:
        _write(tmp_path / "skills", "offered", inject="false")
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert _one(loader, "offered")["inject_on_trigger"] is False

    @pytest.mark.parametrize("value", ["true", "yes", "1", "", "garbage"])
    def test_anything_but_false_reports_injecting(self, tmp_path: Path, value: str) -> None:
        """The listing must agree with split_triggered, not guess separately."""
        _write(tmp_path / "skills", "plain", inject=value)
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert _one(loader, "plain")["inject_on_trigger"] is True

    def test_listing_agrees_with_the_runtime_split(self, tmp_path: Path) -> None:
        """Two code paths reading one field is how a UI starts lying."""
        skills = tmp_path / "skills"
        _write(skills, "mandated")
        _write(skills, "offered", inject="false")
        loader = SkillsLoader(skills_path=skills, install_builtins=False)

        bodies, pointers = loader.split_triggered(["mandated", "offered"])

        assert [s["key"] for s in loader.list_skills() if s["inject_on_trigger"]] == bodies
        assert [s["key"] for s in loader.list_skills() if not s["inject_on_trigger"]] == pointers


class TestSetInjectOnTrigger:
    """The toggle is a targeted frontmatter edit, not a form round-trip."""

    def test_opting_out_writes_the_key(self, tmp_path: Path) -> None:
        _write(tmp_path / "skills", "big")
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        assert loader.set_inject_on_trigger("big", False) is True
        assert _one(loader, "big")["inject_on_trigger"] is False

    def test_opting_back_in_removes_the_key_rather_than_writing_true(self, tmp_path: Path) -> None:
        """Injecting is the default, so absent is the honest way to say it."""
        path = _write(tmp_path / "skills", "big", inject="false")
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        assert loader.set_inject_on_trigger("big", True) is True
        assert "inject_on_trigger" not in path.read_text()
        assert _one(loader, "big")["inject_on_trigger"] is True

    def test_it_preserves_every_other_line_and_the_body(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        d = skills / "scoped"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: scoped\ndescription: d\ntriggers: zebra\n"
            "repo_scope: src/kiro_crew\nalways: false\n---\n# Body\nkeep me\n"
        )
        loader = SkillsLoader(skills_path=skills, install_builtins=False)

        loader.set_inject_on_trigger("scoped", False)

        text = (d / "SKILL.md").read_text()
        assert "repo_scope: src/kiro_crew" in text
        assert "always: false" in text
        assert "# Body\nkeep me" in text

    def test_toggling_twice_leaves_exactly_one_key(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "skills", "big")
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        loader.set_inject_on_trigger("big", False)
        loader.set_inject_on_trigger("big", False)

        assert path.read_text().count("inject_on_trigger") == 1

    def test_the_change_is_visible_to_the_matcher_immediately(self, tmp_path: Path) -> None:
        """A stale frontmatter cache would make the toggle look like a no-op."""
        _write(tmp_path / "skills", "big")
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.split_triggered(["big"]) == (["big"], [])

        loader.set_inject_on_trigger("big", False)

        assert loader.split_triggered(["big"]) == ([], ["big"])

    def test_an_unknown_skill_reports_failure(self, tmp_path: Path) -> None:
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.set_inject_on_trigger("ghost", False) is False

    def test_a_skill_without_frontmatter_reports_failure(self, tmp_path: Path) -> None:
        """Better a failed toggle than a silent no-op the UI shows as applied."""
        skills = tmp_path / "skills"
        d = skills / "bare"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# No frontmatter here\n")
        loader = SkillsLoader(skills_path=skills, install_builtins=False)

        assert loader.set_inject_on_trigger("bare", False) is False
        assert (d / "SKILL.md").read_text() == "# No frontmatter here\n"

    def test_it_refuses_a_skill_outside_the_owned_dir(self, tmp_path: Path) -> None:
        """extra_paths and kiro-cli dirs are not ours to rewrite.

        `_resolve_path` reaches them so the listing can show them; the writer
        must not follow it there. The UI gates on source, but the endpoint is
        reachable directly, so ownership is enforced here.
        """
        owned = tmp_path / "skills"
        owned.mkdir()
        foreign = tmp_path / "elsewhere"
        d = foreign / "borrowed"
        d.mkdir(parents=True)
        original = "---\nname: borrowed\ndescription: d\ntriggers: zebra\n---\nbody"
        (d / "SKILL.md").write_text(original)

        loader = SkillsLoader(skills_path=owned, install_builtins=False)
        loader._extra_paths = [foreign]
        loader._invalidate_iter_cache()
        assert any(s["key"] == "borrowed" for s in loader.list_skills())

        assert loader.set_inject_on_trigger("borrowed", False) is False
        assert (d / "SKILL.md").read_text() == original

    def test_the_listing_marks_a_foreign_skill_unowned(self, tmp_path: Path) -> None:
        """So the UI cannot offer a toggle the writer will refuse.

        A `skills.extra_paths` skill still reports `source: kirocrew`, so source
        alone cannot gate the control. The listing carries the writer's own
        ownership predicate instead, and the two must agree.
        """
        owned = tmp_path / "skills"
        _write(owned, "ours")
        foreign = tmp_path / "elsewhere"
        _write(foreign, "borrowed")

        loader = SkillsLoader(skills_path=owned, install_builtins=False)
        loader._extra_paths = [foreign]
        loader._invalidate_iter_cache()

        assert _one(loader, "ours")["owned"] is True
        assert _one(loader, "borrowed")["owned"] is False
        assert loader.set_inject_on_trigger("borrowed", False) is False
        assert loader.set_inject_on_trigger("ours", False) is True

    def test_it_leaves_an_indented_occurrence_inside_a_block_scalar_alone(
        self, tmp_path: Path
    ) -> None:
        """Only a top-level key is the setting; an indented one is prose.

        A skill whose own description documents the flag would otherwise have
        that sentence deleted by a toggle — the write would silently rewrite
        content while changing a setting.
        """
        skills = tmp_path / "skills"
        d = skills / "documented"
        d.mkdir(parents=True)
        original = (
            "---\n"
            "name: documented\n"
            "description: |\n"
            "  Set this when you must be obeyed:\n"
            "  inject_on_trigger: false\n"
            "triggers: zebra\n"
            "---\n"
            "body"
        )
        (d / "SKILL.md").write_text(original)
        loader = SkillsLoader(skills_path=skills, install_builtins=False)

        assert loader.set_inject_on_trigger("documented", False) is True
        after = (d / "SKILL.md").read_text()
        assert "  inject_on_trigger: false\n" in after
        assert after.count("inject_on_trigger:") == 2
        # And the real setting took effect, not the prose line.
        assert _one(loader, "documented")["inject_on_trigger"] is False

        assert loader.set_inject_on_trigger("documented", True) is True
        assert (d / "SKILL.md").read_text() == original

    def test_an_indented_occurrence_is_not_the_setting(self, tmp_path: Path) -> None:
        """Reader and writer must agree on what counts as the setting.

        The writer leaves an indented `inject_on_trigger:` alone because it is
        prose. If the reader honored it anyway, opting the skill back IN could
        never take effect — the toggle would report success and change nothing.
        """
        skills = tmp_path / "skills"
        d = skills / "prosey"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\n"
            "name: prosey\n"
            "description: |\n"
            "  Documented like so:\n"
            "  inject_on_trigger: false\n"
            "triggers: zebra\n"
            "---\n"
            "body"
        )
        loader = SkillsLoader(skills_path=skills, install_builtins=False)

        # No TOP-LEVEL key, so the skill injects — the prose does not opt it out.
        assert _one(loader, "prosey")["inject_on_trigger"] is True
        assert loader.split_triggered(["prosey"])[0] == ["prosey"]

    @pytest.mark.parametrize("name", ["../escape", "..", "a\\b"])
    def test_it_refuses_an_unsafe_name(self, tmp_path: Path, name: str) -> None:
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.set_inject_on_trigger(name, False) is False


class TestSizeAndDeliveries:
    def test_size_is_the_skill_md_byte_length(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "skills", "sized", body="x" * 500)
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert _one(loader, "sized")["size_bytes"] == path.stat().st_size

    def test_deliveries_is_none_without_a_ledger_entry(self, tmp_path: Path) -> None:
        """None is not zero — an entry can also age out of the window."""
        _write(tmp_path / "skills", "fresh")
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert _one(loader, "fresh")["deliveries"] is None

    def test_a_trigger_match_alone_records_nothing(self, tmp_path: Path) -> None:
        """The ledger counts bodies that reached a prompt, not matches.

        Matching is upstream of the delivery decision, so a match on its own —
        including every false positive — must leave the count untouched.
        """
        _write(tmp_path / "skills", "hot")
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        for _ in range(3):
            loader.get_triggered_skills("zebra")

        assert _one(loader, "hot")["deliveries"] is None

    def test_deliveries_counts_bodies_that_reached_the_prompt(self, tmp_path: Path) -> None:
        _write(tmp_path / "skills", "hot")
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        for _ in range(3):
            loader._record_use("hot")

        assert _one(loader, "hot")["deliveries"] == 3

    def test_an_opted_out_skill_stops_accruing(self, tmp_path: Path) -> None:
        """The figure freezes on opt-out, which the UI has to say out loud.

        Only delivery records, and a pointer delivers no body, so a user
        evaluating an opted-out skill is looking at history — not at whether the
        agent still wants it.
        """
        _write(tmp_path / "skills", "hot")
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        loader._record_use("hot")
        assert _one(loader, "hot")["deliveries"] == 1

        loader.set_inject_on_trigger("hot", False)
        for _ in range(5):
            loader.get_triggered_skills("zebra")

        assert _one(loader, "hot")["deliveries"] == 1

    def test_an_unreadable_ledger_does_not_break_the_listing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path / "skills", "plain")
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        class _Broken:
            def score(self, key: str, **kw: object) -> tuple[float, float]:
                raise RuntimeError("ledger on fire")

        monkeypatch.setattr(loader, "_usage", _Broken())

        row = _one(loader, "plain")
        assert row["deliveries"] is None
        assert row["size_bytes"] > 0


class TestListingStaysCheapOnTheEventLoop:
    """`list_skills()` also feeds the session-start skill index (`get_context`),
    which is assembled on the event loop. Adding `size_bytes` must not add a
    filesystem round-trip per skill: the size comes from the same stat the
    frontmatter cache already needed for its mtime.
    """

    def test_it_stats_no_more_than_the_pre_size_listing_did(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skills = tmp_path / "skills"
        for i in range(5):
            _write(skills, f"s{i}")
        loader = SkillsLoader(skills_path=skills, install_builtins=False)

        calls: list[str] = []
        real_stat = Path.stat

        def counting_stat(self: Path, *a: object, **kw: object) -> object:
            if self.name == "SKILL.md":
                calls.append(str(self))
            return real_stat(self, *a, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", counting_stat)

        # Control: the work this path did BEFORE size_bytes existed — walk the
        # tree and parse each skill's frontmatter through the mtime cache.
        loader._invalidate_iter_cache()
        calls.clear()
        for _, skill_file in loader._iter():
            loader._cached_frontmatter(skill_file)
        control = len(calls)

        loader._invalidate_iter_cache()
        calls.clear()
        rows = loader.list_skills()

        assert len(rows) == 5
        assert all(r["size_bytes"] > 0 for r in rows)
        assert len(calls) <= control, "size_bytes added a stat per skill"

    def test_a_supplied_mtime_skips_the_frontmatter_cache_stat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mechanism the assertion above depends on."""
        path = _write(tmp_path / "skills", "one")
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        mtime = path.stat().st_mtime

        calls: list[str] = []
        real_stat = Path.stat

        def counting_stat(self: Path, *a: object, **kw: object) -> object:
            calls.append(str(self))
            return real_stat(self, *a, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", counting_stat)
        meta = loader._cached_frontmatter(path, mtime=mtime)

        assert meta["name"] == "one"
        assert calls == []
