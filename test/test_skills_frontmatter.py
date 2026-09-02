"""Frontmatter block-scalar parsing in ``SkillsLoader._parse_frontmatter``.

A skill's ``description`` drives auto-routing; when it is authored as a YAML
block scalar (``>``/``|``), the parser must resolve the indented continuation
lines into the value instead of storing the indicator character. These tests
lock in both block-scalar forms alongside the existing simple-value behavior.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.skills import SkillsLoader


def _write(tmp_path: Path, frontmatter: str) -> Path:
    path = tmp_path / "SKILL.md"
    path.write_text(f"---\n{frontmatter}\n---\n\n# Body\n", encoding="utf-8")
    return path


class TestSimpleValues:
    """Existing single-line behavior must be preserved."""

    def test_plain_key_value(self, tmp_path):
        meta = SkillsLoader._parse_frontmatter(
            _write(tmp_path, "name: my-skill\ndescription: does a thing")
        )
        assert meta == {"name": "my-skill", "description": "does a thing"}

    def test_quoted_value_unquoted(self, tmp_path):
        meta = SkillsLoader._parse_frontmatter(_write(tmp_path, 'description: "quoted text"'))
        assert meta["description"] == "quoted text"

    def test_indented_key_value_ignored(self, tmp_path):
        # An indented ``key: value`` is prose inside an enclosing scalar, not a
        # field; honoring it invented junk keys and flipped real settings.
        meta = SkillsLoader._parse_frontmatter(
            _write(tmp_path, "name: my-skill\n  inject_on_trigger: false")
        )
        assert meta == {"name": "my-skill"}

    def test_no_frontmatter(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text("# Just a body\n", encoding="utf-8")
        assert SkillsLoader._parse_frontmatter(path) == {}


class TestFoldedBlockScalar:
    def test_folded_joins_with_spaces(self, tmp_path):
        meta = SkillsLoader._parse_frontmatter(
            _write(
                tmp_path,
                "name: my-skill\n"
                "description: >\n"
                "  Render rich HTML inline via mcwidget tags\n"
                "  with theme-aware styling.\n"
                "triggers: widget",
            )
        )
        assert meta["description"] == (
            "Render rich HTML inline via mcwidget tags with theme-aware styling.\n"
        )
        # Surrounding keys still parse normally.
        assert meta["name"] == "my-skill"
        assert meta["triggers"] == "widget"

    def test_folded_with_chomping_strip(self, tmp_path):
        meta = SkillsLoader._parse_frontmatter(
            _write(tmp_path, "description: >-\n  first line\n  second line")
        )
        assert meta["description"] == "first line second line"

    def test_folded_with_chomping_keep(self, tmp_path):
        # Keep preserves the trailing break that strip drops -- the two spellings above
        # and below used to return the SAME string, which is what made the chomping
        # modifier decorative. See #7097.
        meta = SkillsLoader._parse_frontmatter(
            _write(tmp_path, "description: >+\n  first line\n  second line")
        )
        assert meta["description"] == "first line second line\n"

    def test_folded_blank_line_is_paragraph_break(self, tmp_path):
        meta = SkillsLoader._parse_frontmatter(
            _write(tmp_path, "description: >\n  para one\n\n  para two")
        )
        assert meta["description"] == "para one\npara two\n"

    def test_folded_preserves_consecutive_blank_count(self, tmp_path):
        # k blank lines fold to k newlines, not to a single separator.
        meta = SkillsLoader._parse_frontmatter(
            _write(tmp_path, "description: >\n  para one\n\n\n  para two")
        )
        assert meta["description"] == "para one\n\npara two\n"

    def test_folded_keeps_more_indented_line_breaks(self, tmp_path):
        # Breaks adjacent to a more-indented line are not folded, so nested
        # structure (a list inside a folded description) keeps its shape.
        meta = SkillsLoader._parse_frontmatter(
            _write(
                tmp_path,
                "description: >\n"
                "  intro line\n"
                "    - item one\n"
                "    - item two\n"
                "  outro line",
            )
        )
        assert meta["description"] == "intro line\n  - item one\n  - item two\noutro line\n"

    def test_folded_blank_next_to_more_indented_keeps_separator_break(self, tmp_path):
        # Between plain lines the separator break folds away (k blanks -> k
        # newlines); next to a more-indented line it stays literal, so k
        # blanks yield k+1 newlines.
        meta = SkillsLoader._parse_frontmatter(
            _write(
                tmp_path,
                "description: >\n"
                "  plain intro\n"
                "\n"
                "    indented detail\n"
                "\n"
                "  plain outro",
            )
        )
        assert meta["description"] == "plain intro\n\n  indented detail\n\nplain outro\n"

    def test_folded_at_end_of_frontmatter(self, tmp_path):
        meta = SkillsLoader._parse_frontmatter(
            _write(tmp_path, "name: my-skill\ndescription: >\n  trailing block value")
        )
        assert meta["description"] == "trailing block value\n"

    def test_empty_folded_block_is_empty_string(self, tmp_path):
        # An indicator with no continuation lines resolves to "", never ">".
        meta = SkillsLoader._parse_frontmatter(_write(tmp_path, "description: >\nname: x"))
        assert meta["description"] == ""
        assert meta["name"] == "x"


class TestLiteralBlockScalar:
    def test_literal_preserves_newlines(self, tmp_path):
        meta = SkillsLoader._parse_frontmatter(
            _write(tmp_path, "description: |\n  line one\n  line two")
        )
        assert meta["description"] == "line one\nline two\n"

    def test_literal_with_chomping_strip(self, tmp_path):
        meta = SkillsLoader._parse_frontmatter(
            _write(tmp_path, "description: |-\n  line one\n  line two")
        )
        assert meta["description"] == "line one\nline two"

    def test_literal_with_chomping_keep(self, tmp_path):
        # As with the folded pair, keep preserves the trailing break strip drops. The two
        # used to return the same string, which is what made the modifier decorative.
        meta = SkillsLoader._parse_frontmatter(
            _write(tmp_path, "description: |+\n  line one\n  line two")
        )
        assert meta["description"] == "line one\nline two\n"

    def test_literal_keeps_relative_indentation(self, tmp_path):
        meta = SkillsLoader._parse_frontmatter(
            _write(tmp_path, "description: |\n  outer\n    nested detail")
        )
        assert meta["description"] == "outer\n  nested detail\n"

    def test_literal_leading_blank_line_keeps_relative_indentation(self, tmp_path):
        # Indent is derived from the first NON-BLANK line, so a blank line
        # right after the indicator must not flatten nested indentation.
        #
        # The leading break itself is now PRESERVED (#7097): no YAML chomping mode
        # removes a leading break, and the fold's old trailing `.strip()` was what
        # dropped it. The indentation claim this test exists for is unaffected.
        meta = SkillsLoader._parse_frontmatter(
            _write(tmp_path, "description: |\n\n  outer\n    nested detail")
        )
        assert meta["description"] == "\nouter\n  nested detail\n"


class TestBlockScalarBoundaries:
    def test_colon_inside_block_is_not_a_key(self, tmp_path):
        # A continuation line containing ``:`` stays part of the scalar and
        # must not invent a frontmatter field.
        meta = SkillsLoader._parse_frontmatter(
            _write(
                tmp_path,
                "description: >\n  Steps: do x then y\nname: my-skill",
            )
        )
        assert meta["description"] == "Steps: do x then y\n"
        assert "Steps" not in meta
        assert meta["name"] == "my-skill"

    def test_next_unindented_key_ends_block(self, tmp_path):
        meta = SkillsLoader._parse_frontmatter(
            _write(
                tmp_path,
                "description: >\n  the description\nalways: true\ntriggers: a, b",
            )
        )
        assert meta["description"] == "the description\n"
        assert meta["always"] == "true"
        assert meta["triggers"] == "a, b"

    def test_blank_lines_between_block_and_next_key(self, tmp_path):
        meta = SkillsLoader._parse_frontmatter(
            _write(tmp_path, "description: >\n  the description\n\nname: my-skill")
        )
        assert meta["description"] == "the description\n"
        assert meta["name"] == "my-skill"

    def test_indicator_like_prose_value_kept_verbatim(self, tmp_path):
        # Only a bare indicator triggers block collection; a value merely
        # starting with one of the characters is an ordinary scalar.
        meta = SkillsLoader._parse_frontmatter(_write(tmp_path, "description: >prompt marker"))
        assert meta["description"] == ">prompt marker"
