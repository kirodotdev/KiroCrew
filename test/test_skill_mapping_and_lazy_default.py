"""Who supplies a skill: an explicit mapping, or Crew's own discovery.

Two defaults are pinned here.

An agent carrying its own ``skill://`` mapping named the skills it wants, so it
gets their INSTRUCTIONS — not an index telling it to read what it was handed, and
not a pointer at a catalog it is scoped out of.

Every other session gets the bounded usage-ranked index, which is now the default
(``skills.lazy_load``): it carries each skill's path and, when the catalog does not
fit, the families the shown rows leave out. The shorter eight-name entry stays
reachable by setting the flag false.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.skills import (
    _FAMILY_LINE_MAX_LABELS,
    _MAPPED_NOTICE_MAX_BYTES,
    _MAPPED_NOTICE_MAX_NAMES,
    MAPPED_SKILL_BODIES_CAP,
    SkillsLoader,
    _family_line,
)

pytestmark = pytest.mark.xdist_group("skill_mapping_and_lazy_default")

_BODY_MARKER = "FULL INSTRUCTIONS MARKER"


def _skill(root: Path, key: str) -> str:
    path = root / key / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {key}\ndescription: desc for {key}\n---\n# H\n{_BODY_MARKER} {key}\n",
        encoding="utf-8",
    )
    return str(path)


def _loader(tmp_path: Path) -> SkillsLoader:
    return SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)


class TestLazyLoadDefault:
    def test_bounded_index_is_the_default(self):
        assert KiroCrewConfig().skills.lazy_load is True

    def test_default_entry_is_the_ranked_index_with_paths(self, tmp_path):
        skills_dir = tmp_path / "skills"
        for n in range(3):
            _skill(skills_dir, f"web-{n}")
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4000)
        assert "## Available Skills" in text
        assert "## Skill discovery" not in text
        assert str(skills_dir / "web-0" / "SKILL.md") in text

    def test_short_entry_still_reachable_when_the_flag_is_false(self, tmp_path):
        """`discovery_only` is what the false setting selects; it must still work."""
        skills_dir = tmp_path / "skills"
        for n in range(3):
            _skill(skills_dir, f"web-{n}")
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4000, discovery_only=True)
        assert "## Skill discovery" in text
        assert "## Available Skills" not in text


class TestMappedAgentGetsBodies:
    def test_mapped_skills_are_delivered_in_full(self, tmp_path):
        """A named skill's instructions, not a row pointing back at its file."""
        skills_dir = tmp_path / "skills"
        mapped = [_skill(skills_dir, "mapped-one"), _skill(skills_dir, "mapped-two")]
        for n in range(4):
            _skill(skills_dir, f"other-{n}")
        loader = _loader(tmp_path)
        text = loader.get_context(budget=8000, only=mapped)

        assert text.count(_BODY_MARKER) == 2
        assert "mapped-one" in text and "mapped-two" in text
        assert "other-0" not in text

    def test_mapped_agent_gets_no_index_and_no_search_pointer(self, tmp_path):
        """Both discovery surfaces are redundant against an explicit mapping."""
        skills_dir = tmp_path / "skills"
        mapped = [_skill(skills_dir, "mapped-one")]
        _skill(skills_dir, "other-one")
        loader = _loader(tmp_path)

        for discovery_only in (False, True):
            text = loader.get_context(budget=8000, only=mapped, discovery_only=discovery_only)
            assert "## Available Skills" not in text
            assert "## Skill discovery" not in text
            assert "skill_search" not in text
            assert text.count(_BODY_MARKER) == 1

    def test_unmapped_session_still_gets_an_index(self, tmp_path):
        """The mapping rule must not leak into an ordinary session."""
        skills_dir = tmp_path / "skills"
        _skill(skills_dir, "one")
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4000)
        assert "## Available Skills" in text
        assert _BODY_MARKER not in text

    def test_a_mapping_that_matches_nothing_yields_nothing(self, tmp_path):
        """An agent mapped to a deleted skill must not inherit the catalog."""
        skills_dir = tmp_path / "skills"
        _skill(skills_dir, "one")
        loader = _loader(tmp_path)
        assert loader.get_context(budget=4000, only=[str(skills_dir / "gone" / "SKILL.md")]) == ""


class TestFamiliesInTheIndex:
    def test_truncated_index_names_the_families_it_drops(self, tmp_path):
        """A count says how much is missing; a family says what it is about.

        Every label's number is that family's HIDDEN members, and a family with
        fewer than two hidden is left out: its siblings are already rows in the
        index, so the model has the word for it.
        """
        skills_dir = tmp_path / "skills"
        for n in range(6):
            _skill(skills_dir, f"alpha-{n}")
        for n in range(6):
            _skill(skills_dir, f"beta-{n}")
        loader = _loader(tmp_path)
        text = loader.get_context(budget=1500)

        assert "more skill(s) not shown here" in text
        families = [line for line in text.splitlines() if "Families not shown:" in line]
        assert len(families) == 1
        shown = {
            line[4:].split(":", 1)[0].strip("*")
            for line in text.splitlines()
            if line.startswith("- **")
        }
        for label, total in (("alpha-", 6), ("beta-", 6)):
            hidden = total - sum(1 for name in shown if name.startswith(label))
            if hidden > 1:
                assert f"{label}* ({hidden})" in families[0]
            else:
                assert f"{label}*" not in families[0]

    def test_complete_index_names_no_families(self, tmp_path):
        """Nothing is hidden, so a family line would only repeat the rows."""
        skills_dir = tmp_path / "skills"
        for n in range(3):
            _skill(skills_dir, f"web-{n}")
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4000)
        assert "Families not shown" not in text
        assert "more skill(s) not shown here" not in text

    def test_family_line_is_bounded_by_label_count(self):
        """The line cannot grow with the catalog and crowd out a named skill."""
        skills = [{"key": f"fam{n}-a"} for n in range(20)] + [
            {"key": f"fam{n}-b"} for n in range(20)
        ]
        line = _family_line(skills)
        assert line.count("(2)") == _FAMILY_LINE_MAX_LABELS
        assert line.endswith(f"+{20 - _FAMILY_LINE_MAX_LABELS} more")

    def test_family_line_is_empty_without_a_shared_prefix(self):
        assert _family_line([{"key": "alone"}, {"key": "solo"}]) == ""


class TestMappedBodiesAreBounded:
    """One glob can select any number of skills; the bodies it delivers cannot.

    Mapped bodies land in required content, which no budget clips, so the bound has
    to live here. What the allowance cannot deliver is NAMED with its path, because
    the agent still owns those skills and is still scoped out of the catalog.
    """

    @staticmethod
    def _big(root: Path, key: str, kb: int) -> str:
        path = root / key / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = f"{_BODY_MARKER} {key}\n" + ("Instruction line.\n" * (kb * 55))
        path.write_text(
            f"---\nname: {key}\ndescription: desc for {key}\n---\n{body}", encoding="utf-8"
        )
        return str(path)

    def test_a_wildcard_mapping_stays_inside_the_allowance(self, tmp_path):
        skills_dir = tmp_path / "skills"
        paths = [self._big(skills_dir, f"big-{n:03d}", 11) for n in range(60)]
        catalog = sum(len(Path(p).read_bytes()) for p in paths)
        assert catalog > 3 * MAPPED_SKILL_BODIES_CAP, "fixture must exceed the cap severalfold"

        loader = _loader(tmp_path)
        text = loader.get_context(budget=4950, only=paths)

        bodies = text.count("### Skill: ")
        assert 0 < bodies < 60
        assert len(text) < MAPPED_SKILL_BODIES_CAP + 5_000
        # Still no discovery surface: the notice names them, an index would also
        # advertise the catalog this agent is scoped out of.
        assert "## Available Skills" not in text
        assert "skill_search" not in text
        assert "Mapped skills not included in full" in text

    def test_the_notice_names_the_deferred_skills_with_paths(self, tmp_path):
        skills_dir = tmp_path / "skills"
        paths = [self._big(skills_dir, f"big-{n:03d}", 11) for n in range(60)]
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4950, only=paths)

        delivered = {
            line.split("### Skill: ", 1)[1].strip()
            for line in text.splitlines()
            if line.startswith("### Skill: ")
        }
        notice = next(line for line in text.splitlines() if line.startswith("The mapping selects"))
        named = [key for key in (f"big-{n:03d}" for n in range(60)) if key in notice]
        assert named, "the notice must name what it deferred"
        assert not (set(named) & delivered), "a delivered skill is not also deferred"
        # Each named one carries a path the agent can read.
        for key in named:
            assert str(skills_dir / key / "SKILL.md") in notice
        # Bounded like the family line: names, then a count for the rest.
        assert len(named) <= _MAPPED_NOTICE_MAX_NAMES
        assert "more" in notice

    def test_a_narrow_mapping_is_untouched(self, tmp_path):
        """The case this feature exists for must not pay for the wide one."""
        skills_dir = tmp_path / "skills"
        paths = [self._big(skills_dir, f"big-{n:03d}", 11) for n in range(3)]
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4950, only=paths)

        assert text.count("### Skill: ") == 3
        assert text.count(_BODY_MARKER) == 3
        assert "Mapped skills not included in full" not in text


class TestEverySkillsDefaultComesFromTheDataclass:
    """A default declared twice can drift, and the drift is silent.

    `config.json` omits any key it predates, so a literal in the loader answers for
    exactly those installs. Pinning the whole section, not one field, is the point:
    the first fix of this shape covered `lazy_load` alone.
    """

    def test_an_empty_config_section_reproduces_the_dataclass(self):
        from kiro_crew.config.loader import _build_skills_config

        built = _build_skills_config({})
        assert built == KiroCrewConfig().skills

    def test_an_explicit_value_still_wins(self):
        from kiro_crew.config.loader import _build_skills_config

        built = _build_skills_config({"lazy_load": False, "max_auto_skills": 7})
        assert built.lazy_load is False
        assert built.max_auto_skills == 7


class TestWhatTheMappingDeliversFirst:
    """Order, framing and the always-rule, each measured on its own.

    Three separate claims the cap has to keep: the operator's declared order decides
    who gets a body, the bound counts what is rendered rather than only the file, and
    a skill delivered by `always: true` is neither charged nor named as deferred.
    """

    @staticmethod
    def _sized(root: Path, key: str, kb: int, *, always: bool = False) -> str:
        path = root / key / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        front = f"---\nname: {key}\ndescription: desc for {key}\n"
        if always:
            front += "always: true\n"
        body = f"{_BODY_MARKER} {key}\n" + ("Instruction line.\n" * (kb * 55))
        path.write_text(f"{front}---\n{body}", encoding="utf-8")
        return str(path)

    def test_declaration_order_decides_who_gets_a_body(self, tmp_path):
        skills_dir = tmp_path / "skills"
        paths = [self._sized(skills_dir, f"pick-{n:02d}", 30) for n in range(8)]
        # Declared back to front: the mapping's own order, not the key order.
        reversed_paths = list(reversed(paths))
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4950, only=reversed_paths)

        delivered = [
            line.split("### Skill: ", 1)[1].strip()
            for line in text.splitlines()
            if line.startswith("### Skill: ")
        ]
        assert 0 < len(delivered) < 8
        # The first declared skills are the ones delivered.
        declared_first = [f"pick-{n:02d}" for n in range(7, -1, -1)][: len(delivered)]
        assert sorted(delivered) == sorted(declared_first)

    def test_the_bound_counts_rendered_framing_not_only_the_file(self, tmp_path):
        """A population of tiny skills with long keys must not overrun the bound."""
        skills_dir = tmp_path / "skills"
        long = "x" * 120
        paths = []
        for n in range(400):
            key = f"{long}-{n:04d}"
            path = skills_dir / key / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"---\nname: {key}\ndescription: d\n---\n{_BODY_MARKER}\n"
                + ("Instruction line.\n" * 55),
                encoding="utf-8",
            )
            paths.append(str(path))
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4950, only=paths)

        files = sum(int(Path(p).stat().st_size) for p in paths)
        assert files > MAPPED_SKILL_BODIES_CAP
        # The whole mapped block, bounded notice included, stays inside the byte
        # allowance it claims. Charging only file sizes overshoots it: measured
        # 101,541 bytes against 91,421 on this fixture, because ~77 headings and
        # separators are free there.
        assert len(text) <= MAPPED_SKILL_BODIES_CAP

    def test_an_always_skill_is_never_named_as_deferred(self, tmp_path):
        """Its body is already in the prompt, so naming it would contradict itself."""
        skills_dir = tmp_path / "skills"
        others = [self._sized(skills_dir, f"other-{n:02d}", 30) for n in range(8)]
        pinned_path = self._sized(skills_dir, "pinned-one", 30, always=True)
        # Declared LAST, so the allowance is already spent when it is reached: it
        # would be deferred and named if the always rule did not exclude it.
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4950, only=[*others, pinned_path])

        assert "### Skill: pinned-one" in text
        notice = [line for line in text.splitlines() if line.startswith("The mapping selects")]
        assert notice, "the fixture must exceed the allowance"
        assert "pinned-one" not in notice[0]


class TestAMappedAgentHasNoDiscoveryRow:
    """Not one row, including for a confined project skill.

    A mapped confined skill arrives as a body from the descriptor-pinned reader, so a
    discovery row for it would hand the model the same skill twice, and any row here
    carries the catalog pointer the agent is scoped out of.
    """

    def test_a_confined_mapped_skill_gets_no_row(self, tmp_path):
        from kiro_crew import skill_trust

        skills_dir = tmp_path / "skills"
        mapped = _skill(skills_dir, "mapped-one")
        project = tmp_path / "proj"
        confined = project / ".kiro" / "skills" / "proj-one" / "SKILL.md"
        confined.parent.mkdir(parents=True, exist_ok=True)
        confined.write_text(
            f"---\nname: proj-one\ndescription: a project skill\n---\n{_BODY_MARKER} proj-one\n",
            encoding="utf-8",
        )
        supported = skill_trust.project_skill_traversal_supported()
        if supported:
            skill_trust.grant_project_trust(project)
        loader = _loader(tmp_path)
        # The confined path is IN the mapping, which is how a broad glob reaches it.
        text = loader.get_context(
            budget=4950, only=[mapped, str(confined)], project_dir=str(project)
        )

        # Asserted on BOTH host classes rather than skipped: a host without confined
        # traversal must fail CLOSED, which is a stronger statement than silence and
        # keeps the no-discovery-row ratchet asserted everywhere.
        assert _BODY_MARKER in text, "the mapped global body must still arrive"
        if not supported:
            assert "proj-one" not in text, "an unsupported host serves no confined body"
        assert "## Available Skills" not in text
        assert "## Skill discovery" not in text
        assert "skill_search" not in text

    def test_the_bound_is_measured_on_the_body_it_delivers(self, tmp_path, monkeypatch):
        """The recorded size is not what reaches the prompt, so it cannot be the bound.

        The size comes from the stat taken while enumerating; the body is read after
        it. Any divergence in that window, an atomic save landing between the two,
        would otherwise admit a body on a measurement that fails to describe it, and
        carry the block past a bound no budget clips. Modelled directly by making the
        delivered body larger than the recorded size, rather than by racing a writer.
        """
        skills_dir = tmp_path / "skills"
        paths = []
        for n in range(12):
            path = skills_dir / f"grow-{n:02d}" / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"---\nname: grow-{n:02d}\ndescription: d\n---\n{_BODY_MARKER}\ntiny {n}\n",
                encoding="utf-8",
            )
            paths.append(str(path))
        loader = _loader(tmp_path)
        assert len(loader.list_skills()) == 12

        def grown(key, *args, **kwargs):
            return f"---\nname: {key}\ndescription: d\n---\n{_BODY_MARKER} {key}\n" + (
                f"Instruction line for {key}.\n" * 900
            )

        monkeypatch.setattr(loader, "load_skill", grown)
        text = loader.get_context(budget=4950, only=paths)

        assert len(text.encode("utf-8")) <= MAPPED_SKILL_BODIES_CAP
        assert "Mapped skills not included in full" in text


class TestTheFamiliesLineNamesTheRowsActuallyOmitted:
    """The admission loop skips a row that does not fit and keeps going.

    So the admitted rows are NOT a prefix of the candidates, and a tail slice by count
    would name skills the reader can already see while hiding ones it cannot.
    """

    def test_an_oversized_early_row_does_not_shift_the_families_line(self, tmp_path):
        skills_dir = tmp_path / "skills"
        # A row carries the skill's PATH, so a deeply nested key makes a row too
        # long to admit while shorter later rows still fit. Nested rather than one
        # long segment: a single name over 255 bytes is not a legal filename.
        # TWO of them, so their family has more than one hidden member and appears
        # on the line: a tail slice by row count would drop it and inflate zzz-*.
        deep = "/".join("deep" for _ in range(90))
        for n in range(2):
            huge = skills_dir / "aaa" / deep / f"long-{n}" / "SKILL.md"
            huge.parent.mkdir(parents=True, exist_ok=True)
            huge.write_text(
                f"---\nname: aaa-long-{n}\ndescription: a long-pathed skill\n---\nbody\n",
                encoding="utf-8",
            )
        for n in range(12):
            _skill(skills_dir, f"zzz-{n:02d}")
        loader = _loader(tmp_path)
        text = loader.get_context(budget=900)

        named = {
            line[4:].split(":", 1)[0].strip("*")
            for line in text.splitlines()
            if line.startswith("- **")
        }
        assert named, "some rows must be admitted"
        assert not [n for n in named if n.startswith("aaa-long")], "long rows cannot fit"
        # The count must include the skipped rows, which a tail slice by row count
        # would have replaced with rows the reader can already see.
        omission = [line for line in text.splitlines() if "more skill(s) not shown here" in line]
        assert omission
        assert int(omission[0].split("and ", 1)[1].split(" more", 1)[0]) == 14 - len(named)
        families = [line for line in text.splitlines() if "Families not shown:" in line]
        assert families, "with two families hidden the line must render"
        # Both long rows live under one namespace, so the line names it with the
        # count a tail slice by row number would have dropped.
        assert "aaa/ (2)" in families[0]
        hidden_zzz = 12 - len([n for n in named if n.startswith("zzz-")])
        if hidden_zzz > 1:
            assert f"zzz-* ({hidden_zzz})" in families[0]


class TestGlobMappingsKeepTheirDeclaredPriority:
    """A mapping is written as GLOBS, so ordering has to read the glob that matched.

    An exact-path lookup ranks every glob-matched skill identically, which quietly
    hands the delivery order to directory enumeration: the first-declared glob's
    skills can then lose their bodies to a later one.
    """

    @staticmethod
    def _sized(root: Path, key: str, kb: int) -> str:
        path = root / key / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {key}\ndescription: desc for {key}\n---\n"
            f"{_BODY_MARKER} {key}\n" + ("Instruction line.\n" * (kb * 55)),
            encoding="utf-8",
        )
        return str(path)

    def test_the_first_glob_wins_the_allowance(self, tmp_path):
        skills_dir = tmp_path / "skills"
        # Two families, each far larger than half the allowance, so the cap has to
        # choose between them and the choice is observable.
        for n in range(4):
            self._sized(skills_dir, f"wanted/pick-{n}", 20)
            self._sized(skills_dir, f"spare/pick-{n}", 20)
        loader = _loader(tmp_path)
        globs = [f"{skills_dir}/wanted/*/SKILL.md", f"{skills_dir}/spare/*/SKILL.md"]
        text = loader.get_context(budget=4950, only=globs)

        delivered = [
            line.split("### Skill: ", 1)[1].strip()
            for line in text.splitlines()
            if line.startswith("### Skill: ")
        ]
        assert delivered, "the mapping must deliver something"
        wanted = [key for key in delivered if key.startswith("wanted/")]
        spare = [key for key in delivered if key.startswith("spare/")]
        assert len(wanted) == 4, f"the first glob's skills come first: {delivered}"
        # Anything the allowance could not take belongs to the later glob.
        assert len(spare) < 4
        notice = [line for line in text.splitlines() if line.startswith("The mapping selects")]
        assert notice
        assert "wanted/" not in notice[0]

    def test_the_notice_is_bounded_by_bytes_not_only_by_name_count(self, tmp_path):
        """A retained path has no length bound of its own."""
        skills_dir = tmp_path / "skills"
        deep = "/".join("nested" for _ in range(60))
        paths = [self._sized(skills_dir, f"{deep}/deferred-{n:02d}", 20) for n in range(12)]
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4950, only=paths)

        notice = next(line for line in text.splitlines() if line.startswith("The mapping selects"))
        assert len(notice) < _MAPPED_NOTICE_MAX_BYTES + 500
        # Truncating by bytes must still account for everything it dropped.
        named = len([n for n in range(12) if f"deferred-{n:02d} " in notice])
        assert "more" in notice or named == 0


class TestTheByteCapCountsBytesNotCharacters:
    """A key renders as UTF-8, so the bound has to be measured in UTF-8.

    A CJK key costs up to three bytes a character where `len()` sees one, so a
    character-counted charge lets a population of such keys render past the
    allowance it claims to hold.
    """

    @staticmethod
    def _cjk(root: Path, index: int, body_lines: int) -> str:
        # 80 CJK characters: 240 UTF-8 bytes where `len()` counts 80. Small bodies
        # make that delta the term that decides how many skills are admitted.
        key = "技能目录说明" * 13 + f"-{index:03d}"
        path = root / key / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: s{index}\ndescription: d\n---\n{_BODY_MARKER}\n"
            + ("Instruction.\n" * body_lines),
            encoding="utf-8",
        )
        return str(path)

    def test_a_catalog_of_cjk_keys_stays_inside_the_allowance(self, tmp_path):
        skills_dir = tmp_path / "skills"
        paths = [self._cjk(skills_dir, n, 20) for n in range(500)]
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4950, only=paths)

        assert sum(int(Path(p).stat().st_size) for p in paths) > MAPPED_SKILL_BODIES_CAP
        # Measured as the provider measures it: encoded bytes, not code points.
        assert len(text.encode("utf-8")) <= MAPPED_SKILL_BODIES_CAP
