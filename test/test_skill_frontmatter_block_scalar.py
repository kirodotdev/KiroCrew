"""Block-scalar frontmatter values must fold instead of collapsing to ``>``.

Regression tests for https://github.com/kirodotdev/KiroCrew/issues/3182: a
skill whose ``description:`` uses a YAML block scalar (``>`` folded or ``|``
literal) previously stored the indicator character alone, leaving the skill
unroutable (the catalog the router matches on showed ``>`` as the whole
description).

The two invariants the original column-0 rule protected are pinned here too:
an indented ``key: value`` never becomes a top-level field, and a prose line
like ``  Steps: do x`` never invents a ``Steps`` key. Folding strengthens
both — inside a block scalar those lines become part of the block's TEXT.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.dashboard.handlers.discover import _parse_frontmatter as discover_parse
from kiro_crew.skills import SkillsLoader, parse_frontmatter_block


@pytest.fixture(autouse=True)
def _isolate_extra_paths(monkeypatch):
    """Keep loader tests hermetic (same rationale as test_skills.py)."""
    from kiro_crew.platform.defaults import DefaultMcpToolingProvider

    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        classmethod(lambda cls: KiroCrewConfig(skills=SkillsConfig(extra_paths=[]))),
    )
    monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [])


def _skill_file(tmp_path: Path, name: str, content: str) -> Path:
    skill_dir = tmp_path / "skills" / name
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    return path


# Reporter's exact repro shape from issue #3182.
_FOLDED_SKILL = """---
name: my-skill
description: >
  Audit a drafted document against the writing-style rules. Use when a draft
  needs a wording pass, or the user asks for a style check.
---

# my-skill
"""

_FOLDED_DESCRIPTION = (
    "Audit a drafted document against the writing-style rules. Use when a draft "
    "needs a wording pass, or the user asks for a style check."
)


class TestFoldedScalar:
    def test_folded_description_space_joins(self, tmp_path):
        path = _skill_file(tmp_path, "my-skill", _FOLDED_SKILL)
        meta = SkillsLoader._parse_frontmatter(path)
        assert meta["description"] == _FOLDED_DESCRIPTION
        assert meta["description"] != ">"
        assert meta["description"] == meta["description"].strip()
        assert meta["name"] == "my-skill"

    def test_blank_line_becomes_newline(self, tmp_path):
        path = _skill_file(
            tmp_path,
            "para",
            "---\nname: para\ndescription: >\n  First paragraph line one\n"
            "  line two.\n\n  Second paragraph.\n---\nbody\n",
        )
        meta = SkillsLoader._parse_frontmatter(path)
        assert meta["description"] == (
            "First paragraph line one line two.\nSecond paragraph."
        )

    def test_folded_with_chomping_modifier(self, tmp_path):
        path = _skill_file(
            tmp_path,
            "chomp",
            "---\nname: chomp\ndescription: >-\n  Keep this text.\n---\nbody\n",
        )
        meta = SkillsLoader._parse_frontmatter(path)
        assert meta["description"] == "Keep this text."
        assert meta["description"] != ">-"


class TestLiteralScalar:
    def test_literal_preserves_newlines(self, tmp_path):
        path = _skill_file(
            tmp_path,
            "lit",
            "---\nname: lit\ndescription: |\n  line one\n  line two\n---\nbody\n",
        )
        meta = SkillsLoader._parse_frontmatter(path)
        assert meta["description"] == "line one\nline two"
        assert meta["description"] != "|"

    def test_literal_with_chomping_modifier(self, tmp_path):
        path = _skill_file(
            tmp_path,
            "litchomp",
            "---\nname: litchomp\ndescription: |-\n  only line\n---\nbody\n",
        )
        meta = SkillsLoader._parse_frontmatter(path)
        assert meta["description"] == "only line"
        assert meta["description"] != "|-"


class TestInvariantsPreserved:
    """The docstring guards: pinned so a refactor cannot silently undo them."""

    def test_indented_key_value_in_block_is_text_not_field(self, tmp_path):
        path = _skill_file(
            tmp_path,
            "guard",
            "---\nname: guard\ndescription: >\n  Documents the flag\n"
            "  inject_on_trigger: false\n  and more prose.\n---\nbody\n",
        )
        meta = SkillsLoader._parse_frontmatter(path)
        assert "inject_on_trigger" not in meta
        assert "inject_on_trigger: false" in meta["description"]

    def test_prose_steps_line_invents_no_key(self, tmp_path):
        path = _skill_file(
            tmp_path,
            "steps",
            "---\nname: steps\ndescription: >\n  How to do it.\n"
            "  Steps: do x\n---\nbody\n",
        )
        meta = SkillsLoader._parse_frontmatter(path)
        assert "Steps" not in meta
        assert "Steps: do x" in meta["description"]

    def test_indented_line_outside_block_still_ignored(self, tmp_path):
        path = _skill_file(
            tmp_path,
            "stray",
            "---\nname: stray\ndescription: plain\n  orphan: value\n---\nbody\n",
        )
        meta = SkillsLoader._parse_frontmatter(path)
        assert meta == {"name": "stray", "description": "plain"}


class TestSingleLineRegression:
    """Values that are already single-line must parse byte-identically."""

    def test_plain_and_quoted_values_unchanged(self, tmp_path):
        path = _skill_file(
            tmp_path,
            "plain",
            '---\nname: plain\ndescription: "A quoted description"\n'
            "triggers: alpha, beta\nalways: true\n---\nbody\n",
        )
        meta = SkillsLoader._parse_frontmatter(path)
        assert meta == {
            "name": "plain",
            "description": "A quoted description",
            "triggers": "alpha, beta",
            "always": "true",
        }

    def test_indicator_with_no_body_keeps_old_value(self, tmp_path):
        # A bare `>` with no indented content has nothing to fold; the parse
        # must not crash and must keep the pre-fix literal value.
        path = _skill_file(
            tmp_path,
            "bare",
            "---\nname: bare\ndescription: >\nalways: true\n---\nbody\n",
        )
        meta = SkillsLoader._parse_frontmatter(path)
        assert meta["description"] == ">"
        assert meta["always"] == "true"

    def test_indicator_with_whitespace_only_body_is_empty(self, tmp_path):
        # A block whose body was consumed but folds to nothing (whitespace-
        # only) is an EMPTY value, never the indicator character: storing
        # `|-` for it would recreate the corruption this fix removes.
        path = _skill_file(
            tmp_path,
            "wsbody",
            "---\nname: wsbody\ndescription: |-\n   \nalways: true\n---\nbody\n",
        )
        meta = SkillsLoader._parse_frontmatter(path)
        assert meta["description"] == ""
        assert meta["always"] == "true"

    def test_value_starting_with_gt_but_not_indicator(self, tmp_path):
        path = _skill_file(
            tmp_path,
            "gtword",
            "---\nname: gtword\ndescription: >prompt marker\n---\nbody\n",
        )
        meta = SkillsLoader._parse_frontmatter(path)
        assert meta["description"] == ">prompt marker"


class TestDiscoverReader:
    """The dashboard Discover view's reader has the same defect; same fix."""

    def test_folded_description(self):
        meta = discover_parse(_FOLDED_SKILL)
        assert meta["description"] == _FOLDED_DESCRIPTION
        assert meta["description"] != ">"

    def test_single_line_values_byte_identical(self):
        # This reader never stripped quotes; pin that it still does not.
        meta = discover_parse(
            '---\nname: q\ndescription: "quoted stays quoted"\n---\nbody\n'
        )
        assert meta["description"] == '"quoted stays quoted"'
        assert meta["name"] == "q"


class TestWriterReaderAgreement:
    """Round-trip: what the writer emits, the reader returns unchanged."""

    def test_to_frontmatter_lines_round_trip(self, tmp_path):
        from kiro_crew.skills import AutoSkillProvenance, _build_auto_skill_content

        prov = AutoSkillProvenance(
            session_key="sess-1", created_at=AutoSkillProvenance.now_iso()
        )
        content = _build_auto_skill_content(
            slug="round-trip",
            description="A single line description",
            triggers="alpha, beta",
            procedure_md="# body",
            provenance=prov,
        )
        path = _skill_file(tmp_path, "round-trip", content)
        meta = SkillsLoader._parse_frontmatter(path)
        assert meta["description"] == "A single line description"
        assert meta["triggers"] == "alpha, beta"
        assert meta["session_key"] == "sess-1"
        assert meta["created_at"] == prov.created_at

    def test_set_inject_on_trigger_takes_effect_on_block_scalar_skill(
        self, tmp_path
    ):
        _skill_file(tmp_path, "toggle", _FOLDED_SKILL)
        loader = SkillsLoader(
            skills_path=tmp_path / "skills", install_builtins=False
        )
        assert loader.set_inject_on_trigger("toggle", False) is True
        meta = SkillsLoader._parse_frontmatter(
            tmp_path / "skills" / "toggle" / "SKILL.md"
        )
        # The toggle landed as a real field AND the description survived.
        assert meta["inject_on_trigger"] == "false"
        assert meta["description"] == _FOLDED_DESCRIPTION


class TestRouterSurface:
    """The catalog the router matches on must carry the real description."""

    def test_list_skills_surfaces_folded_description(self, tmp_path):
        _skill_file(tmp_path, "my-skill", _FOLDED_SKILL)
        loader = SkillsLoader(
            skills_path=tmp_path / "skills", install_builtins=False
        )
        rows = loader.list_skills()
        assert len(rows) == 1
        assert rows[0]["description"] == _FOLDED_DESCRIPTION

    def test_get_context_catalog_shows_folded_description(self, tmp_path):
        _skill_file(tmp_path, "my-skill", _FOLDED_SKILL)
        loader = SkillsLoader(
            skills_path=tmp_path / "skills", install_builtins=False
        )
        context = loader.get_context()
        assert "Audit a drafted document against the writing-style rules" in context
        assert "**my-skill**: >" not in context


class TestParseFrontmatterBlockHelper:
    """Direct coverage of the shared helper's edge shapes."""

    def test_indent_indicator_digit_accepted(self):
        meta = parse_frontmatter_block("description: |2\n  text here")
        assert meta["description"] == "text here"

    def test_indent_indicator_digit_honored(self):
        # |2 declares a 2-space block indent, so a 4-space body keeps 2 spaces.
        meta = parse_frontmatter_block("description: |2\n    kept indent")
        assert meta["description"] == "  kept indent"

    def test_deeper_indent_kept_in_literal(self):
        meta = parse_frontmatter_block(
            "description: |\n  outer\n    inner extra indent"
        )
        assert meta["description"] == "outer\n  inner extra indent"

    def test_deeper_indent_stays_literal_in_folded(self):
        # YAML keeps more-indented lines literal inside a `>` block: a nested
        # list must not be space-mashed into the surrounding paragraph.
        meta = parse_frontmatter_block(
            "description: >\n  Use when:\n    - alpha\n    - beta\n  tail line"
        )
        assert meta["description"] == "Use when:\n  - alpha\n  - beta\ntail line"

    def test_block_ends_at_column_zero_key(self):
        meta = parse_frontmatter_block(
            "description: >\n  folded text\nalways: true"
        )
        assert meta["description"] == "folded text"
        assert meta["always"] == "true"

    def test_trailing_blank_lines_chomped(self):
        meta = parse_frontmatter_block(
            "description: >\n  text\n\nalways: true"
        )
        assert meta["description"] == "text"
        assert meta["always"] == "true"

    def test_crlf_input_normalized(self):
        # Provider-fetched content (the Discover path) is not read through
        # Path.read_text, so it can carry CRLF; no \r may leak into values.
        meta = parse_frontmatter_block(
            "name: crlf\r\ndescription: >\r\n  line one\r\n  line two\r\n"
        )
        assert meta["description"] == "line one line two"
        assert "\r" not in meta["description"]
        assert meta["name"] == "crlf"

    def test_tab_indented_key_is_not_a_field(self):
        # Tabs are not legal YAML indentation; a tab-indented `key: value`
        # line is treated as indented block content, never as a field. This
        # deliberately aligns the Discover reader with the loader. The tab
        # itself is content and survives (spaces-only dedent).
        meta = parse_frontmatter_block(
            "name: tabs\ndescription: >\n\ttab-indented body\nalways: true"
        )
        assert meta["description"] == "\ttab-indented body"
        assert meta["always"] == "true"

    def test_mixed_indentation_falls_back_to_lstrip(self):
        # A body line that does not share the first line's exact whitespace
        # prefix is lstripped rather than dropped or kept mis-indented.
        meta = parse_frontmatter_block(
            "description: >\n    four spaces\n  two spaces"
        )
        assert meta["description"] == "four spaces two spaces"

    def test_consecutive_blank_lines_keep_their_count(self):
        # YAML folded: a run of k blank lines between text lines becomes k
        # newlines; a double blank must not collapse into one break.
        meta = parse_frontmatter_block(
            "description: >\n  para one\n\n\n  para two"
        )
        assert meta["description"] == "para one\n\npara two"

    def test_blank_line_next_to_indented_line_keeps_all_newlines(self):
        # No fold happens around a more-indented (literal) line, so a blank
        # run adjacent to one yields k+1 newlines (matches PyYAML:
        # 'para\n\n  indented').
        meta = parse_frontmatter_block(
            "description: >\n  para\n\n    indented"
        )
        assert meta["description"] == "para\n\n  indented"

    def test_comment_after_indicator_still_folds(self):
        # YAML allows a comment after the block header: `> # note` is a valid
        # folded block (verified against PyYAML), so the body must fold.
        meta = parse_frontmatter_block("description: > # note\n  folded text")
        assert meta["description"] == "folded text"

    def test_content_leading_tab_survives_dedent(self):
        # YAML indentation is spaces-only, so a tab after the indent is
        # CONTENT (verified against PyYAML: '|\n  \tcode' -> '\tcode').
        meta = parse_frontmatter_block("description: |\n  \tcode line")
        assert meta["description"] == "\tcode line"
