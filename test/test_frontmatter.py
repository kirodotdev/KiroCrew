"""Snapshot tests pinning the consolidated frontmatter parser to the
grammars its call sites historically accepted.

The expected values below were captured by running the pre-consolidation
parsers (``SkillsLoader._parse_frontmatter``, ``onboarding_import._frontmatter``,
and ``history._frontmatter_value``) against this corpus. They are the oracle
for the refactor: a change in any expectation means a caller's accepted-input
surface moved, which is a behavior change with its own review — not a
refactor. The skill-provider preview (``dashboard/handlers/discover.py``)
deliberately carries no dialect of its own: it shares SKILL_LOADER so the
preview description matches the installed one (the endpoint-level pin lives
in ``test_skill_discover.py``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from yaml_helpers import load_with

from kiro_crew import history
from kiro_crew.frontmatter import (
    ONBOARDING_IMPORT,
    SKILL_LOADER,
    SKILL_UPDATE,
    STEERING_LOADER,
    FrontmatterDialect,
    _render_frontmatter_value,
    fold_block_scalar,
    frontmatter_value,
    parse_block_scalar_header,
    parse_frontmatter,
    set_frontmatter_fields,
    split_frontmatter,
    split_inline_comment,
)
from kiro_crew.onboarding_import import _column0_activation_declared, _frontmatter
from kiro_crew.skills import SkillsLoader


class _StringScalarLoader(yaml.SafeLoader):
    """The YAML oracle for the block-scalar tests: every scalar stays a ``str``.

    The obvious spelling is ``BaseLoader``, which resolves nothing implicitly and is
    what this reader's ``dict[str, str]`` contract needs -- ``always: true`` must read
    the STRING ``"true"``, not ``True``, or the oracle would disagree with the reader
    about a value neither of them mis-parsed. But BaseLoader is a SIBLING of
    ``SafeLoader``, not a subclass, so ``load_with`` refuses it, and this repo forbids
    ``yaml.load`` call sites outright (``test_yaml_safe_loading.py``): safety has to be
    legible at the call site rather than hidden in a ``Loader=`` argument.

    Emptying the implicit-resolver table gives the same behaviour from a safe base:
    with no implicit resolver to match, every plain scalar falls through to
    ``DEFAULT_SCALAR_TAG`` (``str``), while collections still resolve normally.
    Measured equal to BaseLoader over 348 documents, including the shapes that make
    the difference (``true``, ``yes``, ``123``, ``1.5``, ``null``, ``~``, a date, a
    flow sequence and a flow mapping).
    """


_StringScalarLoader.yaml_implicit_resolvers = {}


# Inputs chosen to hit every axis the four grammars disagree on: opener
# strictness, closer form, indent policy, quote stripping, duplicate-key
# resolution, block-scalar resolution, and line-ending handling.
CORPUS: dict[str, str] = {
    "simple": "---\nname: x\ndescription: hello\n---\nbody\n",
    "space_indented_key": "---\nname: x\n  steps: do x\n---\n",
    "tab_indented_key": "---\nname: x\n\tsteps: tabbed\n---\n",
    "quoted_values": "---\nname: \"quoted\"\ndesc: 'single'\nmulti: \"\"double\"\"\n---\n",
    "mismatched_quotes": "---\nk: \"a'\n---\n",
    "leading_ws_before_opener": "\n  ---\nname: x\n---\n",
    "opener_trailing_junk": "---junk\nname: x\n---\n",
    "opener_junk_with_colon": "---x: y\nname: x\n---\n",
    "no_closer": "---\nname: x\n",
    "closer_trailing_junk": "---\nname: x\n---junk\nbody\n",
    "closer_indented": "---\nname: x\n  ---\nbody\n",
    "duplicate_keys": "---\nk: first\nk: second\n---\n",
    "crlf": "---\r\nname: x\r\n---\r\nbody\r\n",
    "empty_block": "---\n---\nbody\n",
    "colon_in_value": "---\nurl: http://example.com:8080\n---\n",
    "empty_value": "---\nkey:\n---\n",
    "block_scalar_folded": "---\ndescription: >\n  first line\n  second line\n---\n",
    "block_scalar_literal": "---\ndescription: |\n  line one\n  line two\n---\n",
    "block_scalar_chomped": "---\ndescription: >-\n  folded text\nname: x\n---\n",
    "block_scalar_blank_fold": "---\ndescription: >\n  para one\n\n  para two\n---\n",
    "block_scalar_junk_keys": "---\ndescription: |\n  Steps: do x\n  more: prose\n---\n",
    "block_scalar_quoted_inside": "---\nk: |\n  \"quoted\"\n---\n",
    "no_colon_line": "---\nnoise\nname: x\n---\n",
    "four_dash_fences": "----\nkey: v\n----\nbody\n",
    "bare_open_fence": "---",
    "empty_text": "",
    "plain_prose": "no frontmatter here\nkey: value\n",
    "value_whitespace": "---\nk:    padded value   \n---\n",
    "body_padding": "---\nk: v\n---\n\n  body  \n\n",
    # An indented duplicate BEFORE the column-0 key: separates the indent
    # policies from duplicate-key resolution.
    "indented_shadow_before": "---\n  k: shadow\nk: real\n---\n",
    # A resolved scalar followed by a plain duplicate: separates first-wins
    # (scalar survives) from last-wins (plain overwrites).
    "duplicate_scalar_then_plain": "---\nk: |\n  from scalar\nk: plain\n---\n",
    # The reverse — plain first, scalar second — is the one shape where the
    # shared scanner's mechanics differ from history's original (which
    # returned on first match and never consumed the second scalar's lines);
    # pinned to prove the lookup result is unchanged anyway.
    "duplicate_plain_then_scalar": "---\nk: plain\nk: |\n  from scalar\n---\n",
}

# ``SkillsLoader._parse_frontmatter`` reads files with ``Path.read_text``,
# whose universal-newline mode collapses CRLF to LF before parsing — so the
# SKILL_LOADER dialect is exercised on normalized text, like the real caller.
SKILL_LOADER_EXPECTED: dict[str, dict[str, str]] = {
    "bare_open_fence": {},
    "block_scalar_blank_fold": {"description": "para one\npara two\n"},
    "block_scalar_chomped": {"description": "folded text", "name": "x"},
    "block_scalar_folded": {"description": "first line second line\n"},
    "block_scalar_junk_keys": {"description": "Steps: do x\nmore: prose\n"},
    "block_scalar_literal": {"description": "line one\nline two\n"},
    "block_scalar_quoted_inside": {"k": '"quoted"\n'},
    "body_padding": {"k": "v"},
    "closer_indented": {},
    "closer_trailing_junk": {"name": "x"},
    "colon_in_value": {"url": "http://example.com:8080"},
    "crlf": {"name": "x"},
    "duplicate_keys": {"k": "second"},
    "duplicate_plain_then_scalar": {"k": "from scalar\n"},
    "duplicate_scalar_then_plain": {"k": "plain"},
    "empty_block": {},
    "empty_text": {},
    "empty_value": {"key": ""},
    "four_dash_fences": {},
    "indented_shadow_before": {"k": "real"},
    "leading_ws_before_opener": {},
    "mismatched_quotes": {"k": "a"},
    "no_closer": {},
    "no_colon_line": {"name": "x"},
    "opener_junk_with_colon": {},
    "opener_trailing_junk": {},
    "plain_prose": {},
    "quoted_values": {"desc": "single", "multi": "double", "name": "quoted"},
    "simple": {"description": "hello", "name": "x"},
    "space_indented_key": {"name": "x"},
    "tab_indented_key": {"name": "x"},
    "value_whitespace": {"k": "padded value"},
}

ONBOARDING_EXPECTED: dict[str, tuple[dict[str, str], str]] = {
    "bare_open_fence": ({}, "---"),
    # This collapsed map stores a block-scalar indicator verbatim; activation
    # never rides on that, because ``_column0_activation_declared`` treats a
    # bare indicator as activating (fail-closed).
    "block_scalar_blank_fold": ({"description": ">"}, ""),
    "block_scalar_chomped": ({"description": ">-", "name": "x"}, ""),
    "block_scalar_folded": ({"description": ">"}, ""),
    "block_scalar_junk_keys": ({"Steps": "do x", "description": "|", "more": "prose"}, ""),
    "block_scalar_literal": ({"description": "|"}, ""),
    "block_scalar_quoted_inside": ({"k": "|"}, ""),
    "body_padding": ({"k": "v"}, "body"),
    "closer_indented": ({"name": "x"}, "body"),
    # KNOWN DIVERGENCE from SKILL_LOADER: this dialect's closer must be an
    # exact "---" line, so a "---junk" closer means no frontmatter here —
    # while the skills loader parses {"name": "x"} from the same bytes. The
    # activation gate is immune (see TestOnboardingImportDialect::
    # test_closer_divergence_cannot_bypass_the_activation_gate); issue #3231
    # documents the history.
    "closer_trailing_junk": ({}, "---\nname: x\n---junk\nbody\n"),
    "colon_in_value": ({"url": "http://example.com:8080"}, ""),
    "crlf": ({"name": "x"}, "body"),
    "duplicate_keys": ({"k": "second"}, ""),
    "duplicate_plain_then_scalar": ({"k": "|"}, ""),
    "duplicate_scalar_then_plain": ({"k": "plain"}, ""),
    "empty_block": ({}, "body"),
    "empty_text": ({}, ""),
    "empty_value": ({"key": ""}, ""),
    "four_dash_fences": ({}, "----\nkey: v\n----\nbody\n"),
    "indented_shadow_before": ({"k": "real"}, ""),
    "leading_ws_before_opener": ({}, "\n  ---\nname: x\n---\n"),
    "mismatched_quotes": ({"k": "a"}, ""),
    "no_closer": ({}, "---\nname: x\n"),
    "no_colon_line": ({"name": "x"}, ""),
    "opener_junk_with_colon": ({"name": "x"}, ""),
    "opener_trailing_junk": ({"name": "x"}, ""),
    "plain_prose": ({}, "no frontmatter here\nkey: value\n"),
    "quoted_values": ({"desc": "single", "multi": "double", "name": "quoted"}, ""),
    "simple": ({"description": "hello", "name": "x"}, "body"),
    "space_indented_key": ({"name": "x", "steps": "do x"}, ""),
    "tab_indented_key": ({"name": "x", "steps": "tabbed"}, ""),
    "value_whitespace": ({"k": "padded value"}, ""),
}

# Non-empty single-key lookups only; every probed key absent from a case's
# dict was verified to return "" from the pre-consolidation
# ``_frontmatter_value``.
HISTORY_EXPECTED: dict[str, dict[str, str]] = {
    "bare_open_fence": {},
    "block_scalar_blank_fold": {"description": "para one\npara two\n"},
    "block_scalar_chomped": {"description": "folded text", "name": "x"},
    "block_scalar_folded": {"description": "first line second line\n"},
    "block_scalar_junk_keys": {"description": "Steps: do x\nmore: prose\n"},
    "block_scalar_literal": {"description": "line one\nline two\n"},
    "block_scalar_quoted_inside": {"k": '"quoted"\n'},
    "body_padding": {"k": "v"},
    "closer_indented": {},
    "closer_trailing_junk": {"name": "x"},
    "colon_in_value": {"url": "http://example.com:8080"},
    # A deliberate accepted-input widening, not drift: the fence tolerates an
    # optional carriage return before each newline, so a CRLF document reads
    # exactly like its LF twin instead of as "no frontmatter" (the
    # pre-consolidation parser returned {} here).
    "crlf": {"name": "x"},
    "duplicate_keys": {"k": "first"},
    # first_key_wins both ways: the plain value survives a later scalar
    # duplicate (whose lines the shared scanner consumes but the original
    # never even read — the lookup result is identical), and a resolved
    # scalar survives a later plain duplicate.
    "duplicate_plain_then_scalar": {"k": "plain"},
    "duplicate_scalar_then_plain": {"k": "from scalar\n"},
    "empty_block": {},
    "empty_text": {},
    "empty_value": {},
    "four_dash_fences": {},
    "indented_shadow_before": {"k": "real"},
    "leading_ws_before_opener": {"name": "x"},
    "mismatched_quotes": {"k": "\"a'"},
    "no_closer": {},
    "no_colon_line": {"name": "x"},
    "opener_junk_with_colon": {},
    "opener_trailing_junk": {},
    "plain_prose": {},
    "quoted_values": {"multi": '""double""', "name": '"quoted"'},
    "simple": {"description": "hello", "name": "x"},
    "space_indented_key": {"name": "x"},
    "tab_indented_key": {"name": "x"},
    "value_whitespace": {"k": "padded value"},
}

HISTORY_PROBE_KEYS = ("name", "description", "k", "key", "steps", "url", "multi", "more", "Steps", "")


def _write_corpus_file(tmp_path: Path, case_id: str) -> Path:
    path = tmp_path / f"{case_id}.md"
    # newline="" so CRLF corpus bytes reach the parser's read_text unmangled
    # by the platform's default newline translation on write.
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(CORPUS[case_id])
    return path


class TestSkillLoaderDialect:
    """The skills-catalog grammar, exercised through the real caller."""

    @pytest.mark.parametrize("case_id", sorted(CORPUS))
    def test_snapshot(self, case_id: str, tmp_path: Path) -> None:
        path = _write_corpus_file(tmp_path, case_id)
        assert SkillsLoader._parse_frontmatter(path) == SKILL_LOADER_EXPECTED[case_id]

    def test_rejects_indented_keys_including_tabs(self) -> None:
        # The column-0 gate is load-bearing: an indented occurrence belongs to
        # a block scalar, and honoring it broke set_inject_on_trigger.
        text = "---\nname: x\n  inject_on_trigger: false\n\tinject_on_trigger: false\n---\n"
        assert parse_frontmatter(text, SKILL_LOADER) == {"name": "x"}


class TestOnboardingImportDialect:
    """The import screen's collapsed map, exercised through the real caller."""

    @pytest.mark.parametrize("case_id", sorted(CORPUS))
    def test_snapshot(self, case_id: str) -> None:
        assert _frontmatter(CORPUS[case_id]) == ONBOARDING_EXPECTED[case_id]

    @pytest.mark.parametrize(
        "text",
        [
            # Spellings the map has always read — indented, quoted,
            # junk-opener, and whitespace-closed variants. These pin the
            # map's LENIENT axes; completeness of the activation decision is
            # ``_column0_activation_declared``'s job, tested below.
            "---\nalways: true\n---\nbody",
            "---\n  always: 'true'\n---\nbody",
            "---junk\nalways: \"yes\"\n---\nbody",
            "---\ntriggers: a, b\n  ---\nbody",
            "---\n\ttriggers: x\n---\nbody",
        ],
    )
    def test_lenient_map_inputs_still_read(self, text: str) -> None:
        metadata, _ = _frontmatter(text)
        assert ("always" in metadata) or ("triggers" in metadata)

    def test_closer_divergence_cannot_bypass_the_activation_gate(self) -> None:
        # The map's exact-"---" closer misses a "---junk"-closed block that
        # the loader parses (see the KNOWN DIVERGENCE pin above; issue #3231
        # documents the history) — but the activation decision mirrors the
        # loader's region rules, so the divergence cannot re-admit an
        # auto-activating skill.
        text = "---\nalways: true\n---junk\nbody"
        map_metadata, _ = _frontmatter(text)
        assert map_metadata == {}
        assert parse_frontmatter(text, SKILL_LOADER) == {"always": "true"}
        assert _column0_activation_declared(text) is True

    def test_indent_shadow_cannot_bypass_the_activation_gate(self) -> None:
        # The other divergence the separate gate exists for: the map accepts
        # indented keys with last-wins, so indented prose overwrites the real
        # column-0 value — while the loader (and the gate) honor only the
        # column-0 line. A "column-0 beats indented" special case added to
        # the shared scanner would silently erase this divergence; this pin
        # makes that a conscious decision.
        text = "---\nalways: true\n  always: false\n---\nbody"
        map_metadata, _ = _frontmatter(text)
        assert map_metadata == {"always": "false"}
        assert parse_frontmatter(text, SKILL_LOADER) == {"always": "true"}
        assert _column0_activation_declared(text) is True


class TestSkillUpdateDialect:
    """The single-key lookup grammar, exercised through the real caller."""

    @pytest.mark.parametrize("case_id", sorted(CORPUS))
    def test_snapshot(self, case_id: str) -> None:
        text = CORPUS[case_id]
        expected = HISTORY_EXPECTED[case_id]
        for key in HISTORY_PROBE_KEYS:
            assert history._frontmatter_value(text, key) == expected.get(key, "")

    def test_none_and_empty(self) -> None:
        assert history._frontmatter_value(None, "description") == ""
        assert history._frontmatter_value("", "description") == ""


class TestTheLiteralFoldTheSkillEditorSimulates:
    """Pin the literal-scalar fold that ``backendFoldsLiteral`` reproduces.

    ``website/src/components/SkillForm.tsx`` holds a function named
    ``backendFoldsLiteral``. It exists because the editor refuses to restructure a managed
    field whose value this reader and a real YAML parser disagree about -- adopting one
    reading and saving it would silently redefine the file for the code that loads skills.
    Rather than predict agreement from the block-scalar indicator, which was wrong three
    times, it SIMULATES this function for the ``|`` family and compares.

    So this is the backend half of a cross-language invariant, and its scope is every step
    that simulation mirrors -- trailing-blank counting, dedent relative to the first
    non-blank line, join, and chomping -- not the chomping axis alone. A dedent change
    would otherwise leave both this file and the TypeScript tests green (those expectations
    are hardcoded) while the two readers drifted apart, reopening exactly the silent
    corruption the editor's refusal was written to prevent. If any assertion below fails,
    ``backendFoldsLiteral`` has to change with it. See #1825 and #7097.

    What CHANGED in #7097: the fold used to end in ``.strip()``, which ate a leading
    newline and every trailing one. No YAML chomping mode does either, so agreement with a
    parser depended on a block's CONTENT rather than on its header. It no longer does --
    the cases below are the ones that used to diverge, and they now agree, which is why
    :class:`TestBlockScalarsAgreeWithARealYamlParser` can assert agreement wholesale.
    """

    # The bodies the TypeScript simulation test drives through `canEditStructured`, with
    # the value measured from this reader. Held here so a fold change reds the backend
    # too, instead of only the hardcoded frontend expectations that mirror it.
    #
    # Each ends in a newline because a block scalar inside a fence always does: the
    # closing ``---`` is its own line, so the last content line carries a real
    # terminating break and clip chomping keeps exactly one.
    LITERAL_FOLDS = (
        ("plain two lines", ["  one", "  two"], "one\ntwo\n"),
        ("interior blank", ["  one", "", "  two"], "one\n\ntwo\n"),
        ("deeper indent inside", ["  one", "    nested", "  two"], "one\n  nested\ntwo\n"),
        ("leading blank", ["", "  true"], "\ntrue\n"),
    )

    def test_the_literal_fold_the_simulation_mirrors(self):
        for label, body, expected in self.LITERAL_FOLDS:
            assert fold_block_scalar("|", body) == expected, label

    def test_a_leading_blank_line_survives_the_way_a_parser_keeps_it(self):
        # Was `test_a_leading_blank_line_is_lost_where_a_parser_keeps_it`, asserting the
        # loss. A leading break is CONTENT under every chomping mode, so keeping it is
        # what removes the content-dependence from the editor's comparison.
        assert fold_block_scalar("|", ["", "  true"]) == "\ntrue\n"
        assert fold_block_scalar("|", ["  true"]) == "true\n"
        # Strip chomping is how a caller asks for neither break.
        assert fold_block_scalar("|-", ["", "  true"]) == "\ntrue"

    def test_the_keep_forms_preserve_what_keep_chomping_preserves(self):
        # `|+`/`>+` tell YAML to KEEP the trailing blank lines, and now so does this
        # reader. Was `test_the_keep_forms_discard_what_keep_chomping_preserves`.
        for indicator in ("|+", ">+"):
            assert fold_block_scalar(indicator, ["  true", "", ""]) == "true\n\n\n", indicator

    def test_strip_and_clip_chomping_differ_from_keep(self):
        # The three modes are distinguishable on the same body, which is what makes the
        # modifier meaningful rather than decorative. Clip keeps exactly one break.
        assert fold_block_scalar("|-", ["  true", "", ""]) == "true"
        assert fold_block_scalar("|", ["  true", "", ""]) == "true\n"
        assert fold_block_scalar("|+", ["  true", "", ""]) == "true\n\n\n"

    def test_a_block_with_no_content_line_charges_no_break_for_the_header(self):
        # An all-blank block's FIRST blank is the newline that ended the header line, not
        # a content break -- so keep-chomping preserves one fewer than the break count.
        # Measured against the parser; getting this wrong is a silent off-by-one that
        # only shows up on an empty managed field.
        assert fold_block_scalar("|+", []) == ""
        assert fold_block_scalar("|+", [""]) == "\n"
        assert fold_block_scalar("|", [""]) == ""
        assert fold_block_scalar("|-", [""]) == ""


class TestTheBlockScalarHeaderGrammar:
    """The full YAML header grammar is resolved, on the read path as well as the write one.

    Before #7097 the read path matched only the six BARE indicators while the write path
    already matched the explicit-indentation forms. A ``description: |2-`` was therefore
    stored as the literal text ``"|2-"`` -- the header mistaken for the value -- while a
    rewrite of an unrelated line above it correctly treated the indented tail as that
    field's content. One matcher now serves both.
    """

    def test_an_explicit_indentation_indicator_is_resolved(self) -> None:
        text = "---\nname: s\ndescription: |2-\n  body text\n---\n"
        assert parse_frontmatter(text, SKILL_LOADER)["description"] == "body text"

    def test_an_explicit_indicator_preserves_leading_whitespace(self) -> None:
        # The reason the indicator exists, and the construct the skill editor
        # deliberately degrades a managed value to avoid: with the indentation DECLARED,
        # a first line indented past it keeps that extra space as content. Inferring the
        # indent from the first non-blank line cannot express this at all.
        text = "---\nname: s\ndescription: |2-\n    indented first\n  flush second\n---\n"
        assert parse_frontmatter(text, SKILL_LOADER)["description"] == "  indented first\nflush second"

    def test_either_modifier_ordering_is_accepted(self) -> None:
        for header in ("|2-", "|-2"):
            text = f"---\nname: s\ndescription: {header}\n    indented\n---\n"
            assert parse_frontmatter(text, SKILL_LOADER)["description"] == "  indented", header

    def test_a_comment_may_share_the_header_line(self) -> None:
        # YAML allows a comment on the header line; it belongs to the header, not to the
        # value. This reader used to store `"|- # note"` as the whole value.
        text = "---\nname: s\ndescription: |- # note\n  body text\n---\n"
        assert parse_frontmatter(text, SKILL_LOADER)["description"] == "body text"

    def test_a_zero_indicator_is_not_a_block_scalar(self) -> None:
        # YAML forbids an indentation indicator of 0. Accepting it would hand the folder a
        # zero-width indent and turn the whole block into content, so it stays a plain
        # value -- the pre-existing behaviour for anything unrecognized.
        text = "---\nname: s\ndescription: |0\n  body\n---\n"
        assert parse_frontmatter(text, SKILL_LOADER)["description"] == "|0"

    def test_a_hash_without_leading_space_is_not_a_comment(self) -> None:
        # `|-#note` is not a header-plus-comment to YAML, so it is not one here either.
        text = "---\nname: s\ndescription: |-#note\n  body\n---\n"
        assert parse_frontmatter(text, SKILL_LOADER)["description"] == "|-#note"

    def test_the_header_parser_reports_style_chomp_and_indent(self) -> None:
        assert parse_block_scalar_header("|") == ("|", None)
        assert parse_block_scalar_header(">-") == (">-", None)
        assert parse_block_scalar_header("|2-") == ("|-", 2)
        assert parse_block_scalar_header("|-2") == ("|-", 2)
        assert parse_block_scalar_header(">+3") == (">+", 3)
        assert parse_block_scalar_header("|2 # note") == ("|", 2)
        assert parse_block_scalar_header("plain value") is None
        assert parse_block_scalar_header("|0") is None

    def test_read_and_write_share_one_header_grammar(self) -> None:
        # The convergence, stated as behaviour: rewriting an unrelated field must consume
        # the explicit-indicator scalar's tail with its key, leaving no orphan, and the
        # rewritten document must read back with both fields intact.
        text = "---\nname: s\ndescription: |2-\n  body text\nrepo_scope: x\n---\nbody\n"
        out = set_frontmatter_fields(text, {"name": "renamed"}, SKILL_LOADER)
        fields = parse_frontmatter(out, SKILL_LOADER)
        assert fields["name"] == "renamed"
        assert fields["description"] == "body text"
        assert fields["repo_scope"] == "x"

    def test_replacing_the_scalar_itself_leaves_no_orphaned_tail(self) -> None:
        text = "---\nname: s\ndescription: |2-\n  body text\n---\nbody\n"
        out = set_frontmatter_fields(text, {"description": "plain now"}, SKILL_LOADER)
        assert parse_frontmatter(out, SKILL_LOADER)["description"] == "plain now"
        assert "body text" not in out

    def test_the_write_walk_stops_at_the_same_boundary_the_read_does(self) -> None:
        # Raised and self-dropped by Opus on b1c0c5c8c as unreachable; fixed anyway,
        # because it is an asymmetry THIS change introduced. Tightening the read path's
        # collection without the write path's left the writer consuming a less-indented
        # trailing comment as though it were block content, so REPLACING the block's own
        # field deleted the author's note -- exactly the silent loss the walk's plain-value
        # rule already guards against. Reachable through the one production caller
        # (steering's mode edit) even though such a document is contrived.
        for header in ("|-", "|2-"):
            text = f"---\nname: s\ndescription: {header}\n  body\n # note\nrepo_scope: x\n---\nBODY\n"
            out = set_frontmatter_fields(text, {"description": "replaced"}, SKILL_LOADER)
            assert "# note" in out, header
            assert parse_frontmatter(out, SKILL_LOADER)["description"] == "replaced", header
            assert parse_frontmatter(out, SKILL_LOADER)["repo_scope"] == "x", header
        # A comment indented INSIDE the block is still the field's content and still goes
        # with it, so the boundary did not become "never consume a comment".
        text = "---\nname: s\ndescription: |-\n  body\n   # inside\nrepo_scope: x\n---\nBODY\n"
        out = set_frontmatter_fields(text, {"description": "replaced"}, SKILL_LOADER)
        assert "# inside" not in out

    def test_onboarding_import_does_not_resolve_block_scalars(self) -> None:
        # ONBOARDING_IMPORT does not resolve block scalars, so its collapsed map still
        # stores the header verbatim. What must NOT happen is the activation GATE going
        # fail-open with it -- see
        # TestTheActivationGateCoversEverythingTheLoaderResolves.
        text = "---\nalways: |2-\n  true\n---\n"
        assert parse_frontmatter(text, ONBOARDING_IMPORT)["always"] == "|2-"

    def test_a_less_indented_comment_is_not_absorbed_into_the_value(self) -> None:
        # YAML ends a block scalar at the first non-blank line indented less than its
        # content, so a less-indented `#` line is a comment in the surrounding document.
        # Collecting purely on "is it indented" absorbed it -- this read back as
        # `body\n# note`, which is a value the file does not contain. Pre-existing for a
        # bare indicator, and the explicit-indicator support would have widened it.
        for header in ("|-", "|2-", ">-", ">2-"):
            text = f"---\nd: {header}\n  body\n # note\n---\nbody\n"
            assert parse_frontmatter(text, SKILL_LOADER)["d"] == "body", header

    def test_a_comment_indented_past_the_content_is_still_content(self) -> None:
        # The boundary is the indent, not the `#`: a MORE-indented `#` line is inside the
        # block and stays part of the value, which is what YAML does.
        text = "---\nd: |-\n  body\n   # note\n---\nbody\n"
        assert parse_frontmatter(text, SKILL_LOADER)["d"] == "body\n # note"


class TestBlockScalarsAgreeWithARealYamlParser:
    """Differential pin: for BLOCK SCALARS this reader now agrees with ``yaml``.

    Hand-written expectations pin what someone believed; this pins the language. The
    oracle is ``yaml.BaseLoader`` rather than ``safe_load`` because BaseLoader leaves
    every scalar a ``str``, which is this module's contract -- ``always: true`` must stay
    the string ``"true"``, not become ``True``.

    The oracle is fed ``block + "\\n"``, not ``block``. The fence extractor drops the
    newline before the closing ``---``, but that newline is the last content line's own
    terminator in the document, so a parser given the captured text verbatim is being
    asked about a DIFFERENT document -- one line short of a break. Getting this wrong is
    what made PyYAML and the editor's JavaScript parser look like they disagreed about a
    clip-chomped scalar at the end of a block; reconstituted, they agree.

    Scope is deliberate. Only the block-scalar half is asserted to agree, because the
    plain-scalar half deliberately does NOT (see the module docstring: an unquoted
    ``": "`` inside a value is read here and REFUSED by YAML, and two shipped builtin
    skills depend on that). Asserting agreement wholesale would be asserting the parser
    swap this issue measured and rejected.
    """

    BODIES = (
        ["  one"],
        ["  one", "  two"],
        ["  one", "", "  two"],
        ["  one", "    nested", "  two"],
        ["", "  one"],
        ["", "", "  one"],
        ["  one", ""],
        ["  one", "", ""],
        ["  one", "", "", ""],
        ["  one", "  two", ""],
        ["    deep", "  shallow"],
        ["  a", "", "    ind", "", "  b"],
        # WHITESPACE-ONLY lines, which are not interchangeable with empty ones: after
        # dedent, whitespace beyond the block's indent is CONTENT. Judging a trailing
        # line blank on its raw text dropped it (``|-`` over ``  body`` then three
        # spaces read ``body`` where a parser reads ``body\n ``). The matrix originally
        # used only empty strings, which is why it missed that -- these rows are the
        # gap closing.
        ["  one", "   "],
        ["  one", "  "],
        ["  one", " "],
        ["  one", "   ", "   "],
        ["  one", "   ", ""],
        ["  one", "", "   "],
        ["   ", "  one"],
        ["  one", "   ", "  two"],
        ["\t"],
        # Content lines with TRAILING whitespace, which is a third distinct case again:
        # a folded scalar's trailing spaces are content and the break folds to a space
        # AFTER them, so `>` over `a  ` then `b` is `a   b`. The earlier body set had
        # whitespace-only lines but never a content line followed by spaces, so it could
        # not express that -- and `parts.append(ln.strip())` was deleting the author's
        # trailing text on every folded line.
        ["  one  "],
        ["  one  ", "  two"],
        ["  one", "  two  "],
        ["  one  ", "  two  "],
        ["    deep  ", "  shallow  "],
        ["  one  ", "", "  two  "],
        ["  one  ", "   "],
        ["  a  ", "    ind  ", "  b  "],
        [],
        [""],
        ["", ""],
    )
    # The hand-written bodies above are REGRESSION rows: each group was added after a
    # reviewer found a line shape the set could not express. That happened three rounds
    # running -- whitespace-only trailing lines, then content followed by trailing
    # spaces, then whitespace-only lines extending PAST an explicit indent -- because a
    # hand-enumerated list only covers the shapes someone already thought of, and "0
    # divergences" says nothing about the ones it never emits.
    #
    # So the space is now GENERATED from the line kinds that actually behave
    # differently, and every 1- and 2-line combination of them is checked. New shapes
    # come from naming a kind here, not from waiting for a reviewer to find one.
    LINE_KINDS = (
        "",  # empty
        " ",  # whitespace, short of the indent
        "  ",  # whitespace, exactly the indent
        "   ",  # whitespace, PAST the indent -- content, not a blank line
        "  \t",  # a tab past the indent -- indentation is spaces, so the tab is content
        "\t",  # a bare tab, at column 0
        " # note",  # SHALLOW content, indented 1 -- deeper than the key, shallower than
        # a typical body line. Pairs with the whitespace-only kinds to express the shape
        # that made the reader consume a document comment: a leading all-space line takes
        # part in detecting the indent, so a shallower line after it ENDS the scalar.
        " shallow",  # the same depth without the comment marker
        "  one",  # plain content
        "  one  ",  # content with authored trailing whitespace
        "    deep",  # more-indented content
        "    deep  ",  # more-indented, with trailing whitespace
    )
    # Some defects need THREE lines to show: a tab-content first line, then a
    # more-indented line, then a line back at the real indent. Judging the first line
    # blank let the second set a deeper boundary and truncated the scalar at the third,
    # and no 1- or 2-line body can express that. The full 10^3 space costs ~30s, so the
    # triples run over the kinds that actually discriminate.
    TRIPLE_KINDS = ("", "   ", "  \t", "  one", "    deep")

    @classmethod
    def generated_bodies(cls) -> list[list[str]]:
        bodies: list[list[str]] = [[a] for a in cls.LINE_KINDS]
        bodies += [[a, b] for a in cls.LINE_KINDS for b in cls.LINE_KINDS]
        bodies += [
            [a, b, c]
            for a in cls.TRIPLE_KINDS
            for b in cls.TRIPLE_KINDS
            for c in cls.TRIPLE_KINDS
        ]
        return bodies

    HEADERS = ("|", "|-", "|+", ">", ">-", ">+", "|2", "|2-", "|2+", ">2", ">2-", "|-2")
    # Where the scalar SITS matters as much as its header, and missing this cost a real
    # bug. The fence strips the newline before ``---``, so a scalar that runs to the end of
    # the block ends without a break -- but one followed by another field ends on a real
    # one, and clip chomping keeps it. Reading both positions the same way made
    # ``description: |`` followed by ``name: x`` return ``one`` where a parser returns
    # ``one\n``.
    TAILS = ((), ("zz: tail",))

    def test_every_header_and_body_combination_agrees(self) -> None:
        checked = 0
        bodies = [*self.BODIES, *self.generated_bodies()]
        for tail in self.TAILS:
            for header in self.HEADERS:
                for body in bodies:
                    block = "\n".join([f"d: {header}", *body, *tail])
                    try:
                        expected = load_with(_StringScalarLoader, block + "\n")
                    except yaml.YAMLError:
                        # A shape YAML itself refuses (e.g. an explicit indent wider than
                        # the content) has no oracle to compare against.
                        continue
                    if not isinstance(expected, dict) or "d" not in expected:
                        continue
                    want = expected["d"] or ""
                    got = parse_frontmatter(f"---\n{block}\n---\nbody\n", SKILL_LOADER).get("d")
                    assert got == want, (
                        f"{header} {body!r} tail={tail!r}: yaml={want!r} reader={got!r}"
                    )
                    checked += 1
        # Guard against the loop silently checking nothing if the oracle starts refusing
        # everything -- a green test that asserts nothing is worse than a red one. 5928
        # of 7512 combinations have an oracle to compare against; the rest are shapes
        # YAML itself refuses.
        assert checked > 5800, checked

    def test_a_folded_scalar_keeps_the_trailing_whitespace_its_author_wrote(self) -> None:
        # GPT 5.6 review, head 8dcd6ef26. A folded scalar's trailing spaces are CONTENT,
        # and the line break folds to a space AFTER them, so `>` over `a  ` then `b` is
        # `a   b` -- three spaces, not one. The fold was calling `.strip()` on each line,
        # which deleted the author's trailing text outright.
        assert fold_block_scalar(">", ["  a  ", "  b"]) == "a   b\n"
        assert fold_block_scalar(">", ["  a  "]) == "a  \n"
        assert fold_block_scalar(">-", ["  a  "]) == "a  "
        # A more-indented line keeps both its extra indent AND its trailing spaces.
        assert fold_block_scalar(">", ["  a", "    b  "]) == "a\n  b  \n"
        # The literal family always kept them; assert it so the two stay in step.
        assert fold_block_scalar("|", ["  a  ", "  b"]) == "a  \nb\n"

    def test_an_explicit_indent_makes_a_whitespace_only_block_content(self) -> None:
        # GPT 5.6 review, head c24bf7f53. Under an explicit indicator the HEADER fixes
        # where content starts, so `|2-` over a line of three spaces is one space of
        # CONTENT -- not an empty block. The emptiness check ran BEFORE the dedent, so
        # the line looked blank on its raw text and the whole scalar came back "".
        assert fold_block_scalar("|-", ["   "], indent=2) == " "
        assert fold_block_scalar("|", ["   "], indent=2) == " \n"
        assert fold_block_scalar("|+", ["   "], indent=2) == " \n"
        assert fold_block_scalar("|-", ["   "], indent=1) == "  "
        assert fold_block_scalar(">-", ["   "], indent=2) == " "
        assert fold_block_scalar("|-", ["   ", "   "], indent=2) == " \n "
        # Within or short of the declared indent there really is nothing left.
        assert fold_block_scalar("|-", ["  "], indent=2) == ""
        assert fold_block_scalar("|-", [" "], indent=2) == ""
        assert fold_block_scalar("|-", ["   "], indent=4) == ""
        # Without an indicator the indent comes from the content, so these stay empty.
        assert fold_block_scalar("|-", ["   "]) == ""
        assert fold_block_scalar("|+", ["", ""]) == "\n\n"

    def test_indentation_is_counted_in_spaces_so_a_tab_is_content(self) -> None:
        # YAML indentation is spaces, never tabs, so under `|` a line of two spaces then
        # a tab is two columns of indent followed by a TAB OF CONTENT. Judging it with
        # `strip()` counted the tab as indentation and found no content line at all.
        assert fold_block_scalar("|", ["  \t"]) == "\t\n"
        assert fold_block_scalar("|-", ["  \t"]) == "\t"
        # The same misjudgement truncated a scalar through the COLLECTION boundary: the
        # tab line looked blank, the more-indented line then set a deeper boundary, and
        # the line back at the real indent was read as outside the block. This shape
        # needs all three lines, which is why the generated matrix runs triples.
        text = "---\nd: |\n  \t\n    deep\n  one\nzz: tail\n---\nbody\n"
        assert parse_frontmatter(text, SKILL_LOADER)["d"] == "\t\n  deep\none\n"

    def test_a_rewrite_leaves_a_tab_indented_block_alone(self) -> None:
        # The write path's boundary walk has to agree with the read path's about where
        # that block ends, or replacing an unrelated field moves the author's lines.
        text = "---\nname: before\nd: |\n  \t\n    deep\n  one\nzz: tail\n---\nbody\n"
        out = set_frontmatter_fields(text, {"name": "after"}, SKILL_LOADER)
        assert "d: |\n  \t\n    deep\n  one\nzz: tail" in out
        assert parse_frontmatter(out, SKILL_LOADER)["d"] == "\t\n  deep\none\n"

    def test_whitespace_beyond_the_indent_is_content_not_a_trailing_break(self) -> None:
        # GPT 5.6 review, head b1c0c5c8c. A line holding only whitespace looks blank
        # (`"   ".strip()` is empty) but the whitespace past the block's indent is
        # CONTENT, so it is not a trailing break and chomping must not eat it. Classified
        # on the raw line this returned `body`; classified after dedent it is `body\n `.
        assert fold_block_scalar("|-", ["  body", "   "]) == "body\n "
        assert fold_block_scalar("|", ["  body", "   "]) == "body\n \n"
        assert fold_block_scalar("|+", ["  body", "   "]) == "body\n \n"
        assert fold_block_scalar(">-", ["  body", "   "]) == "body\n "
        # At or inside the indent there is nothing left after dedent, so it IS a break.
        assert fold_block_scalar("|-", ["  body", "  "]) == "body"
        assert fold_block_scalar("|-", ["  body", " "]) == "body"
        # And the same distinction through the public reader, with an explicit indicator.
        text = "---\nd: |2-\n  body\n   \n---\nBODY\n"
        assert parse_frontmatter(text, SKILL_LOADER)["d"] == "body\n "

    def test_a_scalar_reads_the_same_wherever_it_sits_in_the_block(self) -> None:
        # The bug the TAILS axis caught, pinned on its own so a regression names itself
        # rather than surfacing as one row of a 300-case loop. Reading the two positions
        # differently made a clip-chomped scalar followed by a field return `one` while
        # the same scalar at the end of the block returned `one\n` -- a value that
        # depended on which field came next.
        followed = "---\nname: s\ndescription: |\n  one\nrepo_scope: x\n---\nbody\n"
        at_end = "---\nname: s\ndescription: |\n  one\n---\nbody\n"
        assert parse_frontmatter(followed, SKILL_LOADER)["description"] == "one\n"
        assert parse_frontmatter(at_end, SKILL_LOADER)["description"] == "one\n"
        # Strip chomping is the spelling that asks for no trailing break, in BOTH
        # positions -- the modifier decides this, never the field's position.
        for text in (followed.replace("|", "|-"), at_end.replace("|", "|-")):
            assert parse_frontmatter(text, SKILL_LOADER)["description"] == "one"

    def test_a_comment_on_the_header_line_agrees(self) -> None:
        for header in ("|- # note", "| # note", ">- # note", "|2- # note"):
            block = "\n".join([f"d: {header}", "  one", "  two"])
            want = load_with(_StringScalarLoader, block + "\n")["d"]
            got = parse_frontmatter(f"---\n{block}\n---\nbody\n", SKILL_LOADER).get("d")
            assert got == want, f"{header}: yaml={want!r} reader={got!r}"


class TestTheRepoSkillFileCorpus:
    """Read every repo-tracked SKILL.md both ways. The corpus pin #7097 asked for.

    The module docstring's cross-language warning used to end "nothing in the build
    enforces it". This is the enforcement: a change to the reader that moves what a
    SHIPPED skill file means fails here, naming the file.
    """

    # Skill files whose frontmatter is NOT valid YAML, and which are read correctly only
    # because SKILL_LOADER is wider than YAML on plain scalars. Both carry an unquoted
    # ``": "`` inside `description`, which a YAML parser rejects document-wide with
    # "mapping values are not allowed here".
    #
    # This set is asserted EXACTLY, in both directions. A new entry means someone shipped
    # a skill that a real YAML parse cannot read -- fine today, but it is the evidence
    # that decides whether the parser swap in #7097 is ever affordable, so it must be
    # visible rather than absorbed. A removed entry means the file was quoted and the set
    # needs updating with it.
    NOT_VALID_YAML = frozenset(
        {
            "src/kiro_crew/builtin_skills/kirocrew-dev/prepare-pr/SKILL.md",
            "src/kiro_crew/builtin_skills/web-verify/SKILL.md",
        }
    )

    @staticmethod
    def _repo_root() -> Path:
        return Path(__file__).resolve().parent.parent

    @classmethod
    def _skill_files(cls) -> list[tuple[str, str]]:
        root = cls._repo_root()
        out: list[tuple[str, str]] = []
        for path in sorted(root.rglob("SKILL.md")):
            rel = path.relative_to(root).as_posix()
            if any(part in ("node_modules", ".git", "dist", "build") for part in path.parts):
                continue
            out.append((rel, path.read_text(encoding="utf-8")))
        return out

    @staticmethod
    def _fence_block(text: str) -> str | None:
        match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        return None if match is None else match.group(1)

    def test_the_corpus_is_actually_populated(self) -> None:
        # Every assertion below is vacuous if the glob stops finding files.
        files = self._skill_files()
        assert len(files) > 40, len(files)
        assert sum(1 for _, text in files if self._fence_block(text) is not None) > 40

    def test_the_named_files_are_the_only_ones_not_valid_yaml(self) -> None:
        offenders = set()
        for rel, text in self._skill_files():
            block = self._fence_block(text)
            if block is None:
                continue
            try:
                load_with(_StringScalarLoader, block + "\n")
            except yaml.YAMLError:
                offenders.add(rel)
        assert offenders == set(self.NOT_VALID_YAML), (
            "the set of shipped skill files a YAML parser cannot read has changed; "
            f"unexpected={sorted(offenders - set(self.NOT_VALID_YAML))} "
            f"fixed={sorted(set(self.NOT_VALID_YAML) - offenders)}"
        )

    def test_every_shipped_block_scalar_reads_the_same_both_ways(self) -> None:
        for rel, text in self._skill_files():
            block = self._fence_block(text)
            if block is None or rel in self.NOT_VALID_YAML:
                continue
            parsed = load_with(_StringScalarLoader, block + "\n")
            if not isinstance(parsed, dict):
                continue
            fields = parse_frontmatter(text, SKILL_LOADER)
            for line in block.split("\n"):
                if ":" not in line or line[:1].isspace():
                    continue
                key, _, raw = line.partition(":")
                key = key.strip()
                if parse_block_scalar_header(raw.strip()) is None:
                    continue
                assert fields[key] == parsed[key], f"{rel}::{key}"


class TestChompingCannotFlipAnActivationFlag:
    """Real chomping must not silently change whether a skill is always-on.

    ``always`` and ``pinned`` are decided by an EXACT string comparison against
    ``"true"`` (``skills.py``, ``skill_budget.py``). Before #7097 the fold ended in
    ``.strip()``, so ``always: |+`` followed by trailing blank lines read ``"true"`` and
    the skill was always-on. Honouring keep-chomping makes that same field read
    ``"true\\n\\n"``, which is not equal to ``"true"`` -- so without normalising at the
    comparison the flag would flip from on to OFF on a file nobody edited.

    The repo already stripped at five of those comparisons (``inject_on_trigger`` in three
    places, ``pinned`` in two) and not at six others -- ``pinned`` was read BOTH ways in
    the same module. This pins the whole class as normalised, so the reader can stay
    YAML-correct without the activation semantics riding on a trailing newline.

    ``repo_scope`` belongs to the same class and was missed on the first sweep, which
    stopped at the boolean flags. Its harm is narrower than a flag's, though, and worth
    stating precisely: ``project_scope_satisfied`` strips its own fragment, so a real path
    carrying a trailing break was always gated correctly. What breaks is the
    WHITESPACE-ONLY value -- ``repo_scope: |`` over a blank line resolves to a break, which
    is truthy, and each of the three gate call sites guards on that truthiness. An
    unstripped site therefore hands the gate whitespace and it refuses, suppressing a skill
    nobody scoped.
    """

    FLAG_BODIES = (
        ("keep chomped with trailing blanks", "|+", ["  true", "", ""]),
        ("clip chomped with a trailing blank", "|", ["  true", ""]),
        ("leading blank line", "|", ["", "  true"]),
        ("plain literal", "|", ["  true"]),
        ("strip chomped", "|-", ["  true", "", ""]),
    )

    def test_every_block_scalar_spelling_of_true_still_reads_as_always_on(self) -> None:
        for label, header, body in self.FLAG_BODIES:
            text = "\n".join(["---", "name: s", f"always: {header}", *body, "---", "", "# Body"])
            value = parse_frontmatter(text, SKILL_LOADER)["always"]
            assert value.strip().lower() == "true", f"{label}: {value!r}"

    def test_a_crlf_document_resolves_exactly_like_its_lf_twin(self) -> None:
        # GPT 5.6 review, head ac5b7940e. Judging "blank" in SPACES made a CRLF blank line
        # -- which is "\r", not "" -- look like a content line at indent 0, so it ENDED the
        # scalar: a steering description was truncated at its first interior blank, and lost
        # whole when the blank came first. YAML normalises line breaks, so the contract is
        # equality with the LF twin, which also removes the stray CR base left in the value.
        for header, body in (
            ("|", ["  one", "", "  two"]),
            ("|", ["", "  one"]),
            ("|-", ["  one", "", "  two"]),
            ("|+", ["  one", ""]),
            (">", ["  one", "", "  two"]),
            ("|2", ["  one", "", "  two"]),
        ):
            frag = "\n".join(["name: s", f"description: {header}", *body, "mode: always"])
            lf = f"---\n{frag}\n---\n\nBody\n"
            crlf = lf.replace("\n", "\r\n")
            want = parse_frontmatter(lf, STEERING_LOADER)["description"]
            got = parse_frontmatter(crlf, STEERING_LOADER)["description"]
            assert got == want, (header, body, got, want)
            assert "\r" not in got, (header, body, got)
        # And the value is what a parser gives, not merely self-consistent.
        crlf = "---\r\nname: s\r\ndescription: |\r\n  one\r\n\r\n  two\r\nmode: always\r\n---\r\n"
        assert parse_frontmatter(crlf, STEERING_LOADER)["description"] == "one\n\ntwo\n"

    def test_a_leading_blank_line_sets_the_block_boundary(self) -> None:
        # GPT 5.6 review, head a337c8182. A leading all-space line takes part in detecting
        # the block's indent, so a SHALLOWER line after it ends the scalar instead of
        # becoming its first content line. Without that floor, `description: |` over three
        # spaces then ` # note` read the note as content where a parser reads an empty
        # scalar plus a document comment -- and rewriting the field then deleted the note.
        doc = "---\nname: s\ndescription: |\n   \n # outer comment\nrepo_scope: x\n---\n\n# B\n"
        assert parse_frontmatter(doc, SKILL_LOADER)["description"] == ""
        # The note is the surrounding document's, so a rewrite of ANY field must keep it.
        for target in ("description", "name", "repo_scope"):
            out = set_frontmatter_fields(doc, {target: "AFTER"}, SKILL_UPDATE)
            assert " # outer comment" in out, (target, out)
        # Contrast, and the reason this is a floor and not a blanket refusal: with NO
        # leading blank the comment IS the first content line, so it is scalar content and
        # a parser agrees. Consuming it there is correct.
        no_blank = "---\nname: s\ndescription: |\n # outer comment\nrepo_scope: x\n---\n\n# B\n"
        assert parse_frontmatter(no_blank, SKILL_LOADER)["description"] == "# outer comment\n"
        # An explicit indicator fixes the indent itself, so the floor must not override it.
        explicit = "---\nname: s\ndescription: |2\n   \n # note\nrepo_scope: x\n---\n\n# B\n"
        assert parse_frontmatter(explicit, SKILL_LOADER)["description"] == " \n"

    def test_a_block_scalar_repo_scope_still_gates_to_the_same_path(self) -> None:
        # First Principles review, heads e5c989b4a and e847966a7. `repo_scope` reaches a
        # gate at three call sites, each guarded on the value's TRUTHINESS, so all three
        # have to agree about what counts as "no scope at all". `repo_scope: |` over a
        # blank line resolves to a break rather than to "", which is truthy -- so an
        # unstripped site hands the gate whitespace and it refuses, suppressing a skill
        # nobody scoped, while a stripped site reads it as unscoped and shows it.
        #
        # A trailing break on a REAL path is deliberately not the claim here:
        # `project_scope_satisfied` strips its own fragment, so `src/x\n` was always gated
        # as `src/x`. Only the whitespace-only case was ever a defect.
        for label, header, body in (
            ("keep chomped", "|+", ["  src/kiro_crew", "", ""]),
            ("clip chomped", "|", ["  src/kiro_crew", ""]),
            ("plain literal", "|", ["  src/kiro_crew"]),
            ("strip chomped", "|-", ["  src/kiro_crew"]),
            ("folded", ">", ["  src/kiro_crew"]),
        ):
            text = "\n".join(["---", "name: s", f"repo_scope: {header}", *body, "---", "", "# B"])
            value = parse_frontmatter(text, SKILL_LOADER)["repo_scope"]
            assert value.strip() == "src/kiro_crew", f"{label}: {value!r}"
        # Not vacuous: at least one spelling really does carry the break, so the
        # normalisation is doing work rather than restating the parser.
        carried = parse_frontmatter(
            "---\nname: s\nrepo_scope: |\n  src/kiro_crew\n---\n\n# B\n", SKILL_LOADER
        )["repo_scope"]
        assert carried == "src/kiro_crew\n", carried
        # The whitespace-only value: truthy as parsed, falsy once normalised, which is
        # what makes the three gate sites agree. Measured spellings -- keep-chomping and
        # an explicit indent both survive as breaks, and so does a tab past the indent.
        # A plain `|` over spaces is NOT one of these: with the indent taken from the
        # content, a whitespace-only block has no content line and resolves to "".
        for label, frag in (
            ("keep chomped blank", "repo_scope: |+\n\n"),
            ("explicit indent, spaces", "repo_scope: |2\n   \n"),
            ("explicit indent, stripped", "repo_scope: |2-\n   \n"),
            ("folded keep chomped", "repo_scope: >+\n\n"),
            ("tab past the indent", "repo_scope: |\n  \t\n"),
        ):
            blank = parse_frontmatter(f"---\nname: s\n{frag}---\n\n# B\n", SKILL_LOADER)[
                "repo_scope"
            ]
            assert blank, f"{label}: must be truthy as parsed, else no inconsistency"
            assert not blank.strip(), f"{label}: {blank!r}"
        # And the falsy-as-parsed spelling, for contrast.
        assert (
            parse_frontmatter("---\nname: s\nrepo_scope: |\n   \n---\n\n# B\n", SKILL_LOADER)[
                "repo_scope"
            ]
            == ""
        )

    def test_every_repo_scope_gate_site_normalises(self) -> None:
        # The defect was a PARTIAL sweep: two of the three call sites stripped and the
        # third consumed an unstripped row, so one document was scoped at one site and
        # unscoped at another. Pin every site that feeds the gate as normalised.
        src = (Path(__file__).resolve().parents[1] / "src/kiro_crew/skills.py").read_text()
        reads = re.findall(r'"repo_scope":\s*meta\.get\("repo_scope"[^\n]*', src)
        reads += re.findall(r'scope\s*=\s*meta\.get\("repo_scope"[^\n]*', src)
        assert reads, "no repo_scope reads found -- the pattern moved, update this test"
        for read in reads:
            assert ".strip()" in read, read
        # And the gate is only ever called with a value that came through one of those.
        calls = re.findall(r"self\._repo_scope_satisfied\(([^,]+),", src)
        assert len(calls) >= 3, calls
        for call in calls:
            assert call.strip() in {"scope", 'str(s["repo_scope"])'}, call

    def test_the_raw_value_really_does_carry_the_breaks(self) -> None:
        # Guard the test above against becoming vacuous: it only proves anything while at
        # least one spelling genuinely carries a trailing break that the comparison has to
        # absorb. If chomping were reverted this assertion fails, not the one above.
        text = "\n".join(["---", "name: s", "always: |+", "  true", "", "", "---", "", "# Body"])
        assert parse_frontmatter(text, SKILL_LOADER)["always"] == "true\n\n\n"


class TestTheActivationGateCoversEverythingTheLoaderResolves:
    """The onboarding import screen must not go fail-OPEN when the loader widens.

    ``_column0_activation_declared`` decides whether an IMPORTED (untrusted) skill
    package declares itself always-on, and refuses it if so. It cannot see the
    continuation lines the loader resolves, so it treats any block-scalar header as
    activating and assumes the worst. That is fail-closed only while its detected set is
    a SUPERSET of what the loader can resolve.

    It used to hold its own list of six bare indicators. Widening the loader to the full
    header grammar without it inverted the gate: ``always: |2-`` over a ``true``
    continuation was NOT detected, was installed verbatim, and was then read by
    ``SkillsLoader`` as ``always == "true"`` -- external content self-activating into
    every session, the exact hazard ``automatic_activation_excluded`` exists to reject.
    Both sides now read the grammar from ``parse_block_scalar_header``.
    """

    SPELLINGS = ("|", "|-", "|+", ">", ">-", ">+", "|2-", "|-2", "|2", "|- # note", "| # note")

    def test_every_header_the_loader_resolves_is_detected_by_the_gate(self) -> None:
        for spelling in self.SPELLINGS:
            text = f"---\nname: s\nalways: {spelling}\n  true\n---\nbody\n"
            resolved = parse_frontmatter(text, SKILL_LOADER).get("always", "")
            activates = resolved.strip().lower() == "true"
            declared = _column0_activation_declared(text)
            # The invariant is one-directional: the gate may be stricter than the loader,
            # never looser. An undetected activation is the fail-open case.
            assert not (activates and not declared), (
                f"always: {spelling} activates (loader reads {resolved!r}) "
                f"but the import gate did not detect it"
            )

    def test_the_gate_is_a_superset_not_an_equality(self) -> None:
        # Being STRICTER is fine and expected: a header whose continuation is not `true`
        # does not activate, yet the gate still refuses it, because it cannot see the
        # continuation. That asymmetry is the fail-closed design, not a bug.
        text = "---\nname: s\nalways: |-\n  false\n---\nbody\n"
        assert parse_frontmatter(text, SKILL_LOADER)["always"] == "false"
        assert _column0_activation_declared(text) is True

    def test_a_plain_truthy_value_is_still_detected(self) -> None:
        for value in ("true", "TRUE", "yes", "1", '"true"'):
            text = f"---\nname: s\nalways: {value}\n---\nbody\n"
            assert _column0_activation_declared(text) is True, value

    def test_a_padded_quoted_value_is_detected(self) -> None:
        # GPT 5.6 review, head 8dcd6ef26. The gate stripped whitespace only OUTSIDE the
        # quotes, so `always: " true "` unquoted to `` true `` and matched no truthy
        # word -- while the loader's consumers compare `.strip().lower() == "true"` and
        # DO activate it. An imported skill spelled that way self-activated past the
        # screen. Whitespace is now stripped after the quotes come off too.
        for value in ('" true "', "' true '", '"  TRUE  "', '"\ttrue\t"'):
            text = f"---\nname: s\nalways: {value}\n---\nbody\n"
            resolved = parse_frontmatter(text, SKILL_LOADER)["always"]
            assert resolved.strip().lower() == "true", value
            assert _column0_activation_declared(text) is True, value

    def test_a_non_header_value_is_not_treated_as_one(self) -> None:
        # The gate must not start refusing ordinary values just because it now reads a
        # wider grammar: an explicit `0` indicator is invalid YAML and a plain word is
        # not a header.
        for value in ("false", "no", "0", "|0", "maybe", "pipe | in prose"):
            text = f"---\nname: s\nalways: {value}\n---\nbody\n"
            assert _column0_activation_declared(text) is False, value


class TestDialectContracts:
    """The three dialects stay distinct — collapsing any two axes silently
    changes some caller's accepted-input surface."""

    def test_presets_are_distinct(self) -> None:
        presets = [SKILL_LOADER, ONBOARDING_IMPORT, SKILL_UPDATE]
        keys = {
            (p.extraction, p.indent_policy, p.strip_quotes, p.first_key_wins,
             p.resolve_block_scalars)
            for p in presets
        }
        assert len(keys) == len(presets)

    def test_presets_are_frozen(self) -> None:
        with pytest.raises(AttributeError):
            SKILL_LOADER.strip_quotes = False  # type: ignore[misc]

    def test_first_key_wins_vs_last(self) -> None:
        text = "---\nk: first\nk: second\n---\n"
        assert frontmatter_value(text, "k", SKILL_UPDATE) == "first"
        assert parse_frontmatter(text, SKILL_LOADER)["k"] == "second"

    def test_quote_stripping_is_per_dialect(self) -> None:
        text = '---\nk: "v"\n---\n'
        assert parse_frontmatter(text, SKILL_LOADER)["k"] == "v"
        assert parse_frontmatter(text, SKILL_UPDATE)["k"] == '"v"'

    def test_block_scalar_resolution_is_per_dialect(self) -> None:
        text = "---\nk: |\n  content\n---\n"
        # Clip chomping keeps the last line's own terminating break; see
        # :func:`fold_block_scalar`.
        assert parse_frontmatter(text, SKILL_LOADER)["k"] == "content\n"
        assert parse_frontmatter(text, SKILL_UPDATE)["k"] == "content\n"
        assert parse_frontmatter(text, ONBOARDING_IMPORT)["k"] == "|"

    def test_quote_strip_never_applies_to_a_resolved_scalar(self) -> None:
        # SKILL_LOADER strips quotes from plain values but a resolved block
        # scalar keeps its content verbatim, quotes included.
        text = '---\nk: |\n  "quoted"\n---\n'
        assert parse_frontmatter(text, SKILL_LOADER)["k"] == '"quoted"\n'

    def test_split_returns_text_unchanged_without_block(self) -> None:
        for dialect in (SKILL_LOADER, ONBOARDING_IMPORT, SKILL_UPDATE):
            assert split_frontmatter("plain prose", dialect) == ({}, "plain prose")

    def test_custom_dialect_axes_compose(self) -> None:
        # The parameterization is real, not four hardcoded paths: a novel
        # combination behaves per its axes.
        dialect = FrontmatterDialect(
            extraction="column0_fence",
            indent_policy="accept_indented",
            strip_quotes=False,
            first_key_wins=True,
        )
        text = "---\nk: 'a'\n  k: b\n---\n"
        assert parse_frontmatter(text, dialect) == {"k": "'a'"}

    def test_unknown_extraction_mode_fails_loud(self) -> None:
        # A new Extraction literal without its own branch must raise, never
        # silently inherit another mode's grammar.
        bogus = FrontmatterDialect(
            extraction="nonsense",  # type: ignore[arg-type]
            indent_policy="accept_indented",
            strip_quotes=False,
        )
        with pytest.raises(ValueError, match="unknown frontmatter extraction mode"):
            parse_frontmatter("---\nk: v\n---\n", bogus)

    def test_line_scan_body_is_the_only_renderable_body(self) -> None:
        # line_scan's body contract: stripped text after the closer line.
        # The two fence modes' remainders are pinned here as NOT renderable:
        # they cut immediately after "---" — mid-line when the closer
        # carries trailing text.
        text = "---\nk: v\n---\nbody\n"
        assert split_frontmatter(text, ONBOARDING_IMPORT)[1] == "body"
        assert split_frontmatter(text, SKILL_LOADER)[1] == "\nbody\n"
        assert split_frontmatter(text, SKILL_UPDATE)[1] == "\nbody\n"
        junk_closer = "---\nk: v\n---junk\nbody\n"
        assert split_frontmatter(junk_closer, SKILL_LOADER)[1] == "junk\nbody\n"
        assert split_frontmatter(junk_closer, SKILL_UPDATE)[1] == "junk\nbody\n"


def test_removing_the_last_field_keeps_the_body_blank_lines():
    """The fence takes ONE separator newline with it. `lstrip` would eat every
    blank line the document itself opens with — a silent reflow of text this
    writer promises to preserve byte for byte."""
    from kiro_crew.frontmatter import STEERING_LOADER, set_frontmatter_fields

    doc = "---\ninclusion: manual\n---\n\n\n# Title\n\nbody\n"
    out = set_frontmatter_fields(doc, {"inclusion": None}, STEERING_LOADER)
    assert out == "\n\n# Title\n\nbody\n"


def test_removing_the_last_field_on_a_body_with_no_blank_line():
    from kiro_crew.frontmatter import STEERING_LOADER, set_frontmatter_fields

    doc = "---\ninclusion: manual\n---\n# Title\n"
    assert set_frontmatter_fields(doc, {"inclusion": None}, STEERING_LOADER) == "# Title\n"


class TestCrlfSteeringDocuments:
    """A steering file authored on Windows has ``---\r\n``.

    The LF-only fence did not match it at all, so its declaration was invisible
    — the tab reported the default mode — and an edit PREPENDED a second
    front-matter block instead of rewriting the first.
    """

    CRLF = "---\r\ninclusion: manual\r\n---\r\n# Title\r\nbody\r\n"

    def _d(self):
        from kiro_crew.frontmatter import STEERING_LOADER

        return STEERING_LOADER

    def test_the_declaration_is_visible(self):
        from kiro_crew.frontmatter import split_frontmatter

        assert split_frontmatter(self.CRLF, self._d())[0] == {"inclusion": "manual"}

    def test_an_edit_rewrites_rather_than_prepends(self):
        from kiro_crew.frontmatter import set_frontmatter_fields

        out = set_frontmatter_fields(self.CRLF, {"inclusion": "always"}, self._d())
        assert out == "---\r\ninclusion: always\r\n---\r\n# Title\r\nbody\r\n"

    def test_creation_matches_the_document_newline(self):
        """Emitting LF into a CRLF file leaves it mixed — the same class of
        damage as reflowing the body, and as invisible in a diff viewer."""
        from kiro_crew.frontmatter import set_frontmatter_fields

        out = set_frontmatter_fields("# Title\r\nbody\r\n", {"inclusion": "manual"}, self._d())
        assert out == "---\r\ninclusion: manual\r\n---\r\n# Title\r\nbody\r\n"
        assert "\n" not in out.replace("\r\n", "")

    def test_removing_the_last_field_takes_one_crlf(self):
        from kiro_crew.frontmatter import set_frontmatter_fields

        out = set_frontmatter_fields(self.CRLF, {"inclusion": None}, self._d())
        assert out == "# Title\r\nbody\r\n"

    def test_lf_documents_are_unchanged(self):
        from kiro_crew.frontmatter import set_frontmatter_fields

        lf = "---\ninclusion: manual\n---\n# Title\nbody\n"
        out = set_frontmatter_fields(lf, {"inclusion": "always"}, self._d())
        assert out == "---\ninclusion: always\n---\n# Title\nbody\n"
        assert "\r" not in out


class TestEmptyFenceIsStillAFence:
    """An opener immediately followed by a closer (``---\n---``) has no line
    between them, so the previous fence pattern — which required a captured
    content line before the closer — never matched it at all. A mode edit
    then read that as "no frontmatter yet" and PREPENDED a brand-new fence in
    front of the empty one, duplicating the block instead of populating it.
    """

    def test_an_edit_populates_the_empty_block_in_place(self):
        from kiro_crew.frontmatter import STEERING_LOADER, set_frontmatter_fields

        doc = "---\n---\n# Title\nbody\n"
        out = set_frontmatter_fields(doc, {"inclusion": "manual"}, STEERING_LOADER)
        assert out == "---\ninclusion: manual\n---\n# Title\nbody\n"

    def test_an_edit_populates_the_empty_crlf_block_in_place(self):
        from kiro_crew.frontmatter import STEERING_LOADER, set_frontmatter_fields

        doc = "---\r\n---\r\n# Title\r\nbody\r\n"
        out = set_frontmatter_fields(doc, {"inclusion": "manual"}, STEERING_LOADER)
        assert out == "---\r\ninclusion: manual\r\n---\r\n# Title\r\nbody\r\n"

    def test_split_frontmatter_reports_no_fields_and_the_real_body(self):
        from kiro_crew.frontmatter import STEERING_LOADER, split_frontmatter

        fields, body = split_frontmatter("---\n---\n# Title\nbody\n", STEERING_LOADER)
        assert fields == {}
        assert body == "\n# Title\nbody\n"


def test_crlf_block_scalar_survives_a_mode_edit():
    """The folded value's continuation lines are re-joined with the document's
    newline, so a retained CR would be written back as ``\r\r\n``."""
    from kiro_crew.frontmatter import STEERING_LOADER, set_frontmatter_fields

    doc = (
        "---\r\ndescription: >\r\n  folded one\r\n  folded two\r\n"
        "inclusion: manual\r\n---\r\n# T\r\nbody\r\n"
    )
    out = set_frontmatter_fields(doc, {"inclusion": "always"}, STEERING_LOADER)
    assert "\r\r\n" not in out
    assert out.count("\r\n") == out.count("\n")


class TestCrlfSkillDocuments:
    """The skill dialects read a CRLF document without a caller pre-folding.

    Most SKILL.md reads fold CRLF to LF before parsing (``Path.read_text``'s
    universal newlines, ``skills._decode_skill_text``), so those paths never
    met the LF-only fence. Text that arrives VERBATIM did: the Agent SOP
    description reader decodes raw bytes, so a Windows-authored document
    showed no description at all. And ``set_frontmatter_fields`` is public
    API reachable with these dialects: a fence the extraction cannot see
    makes a field edit PREPEND a brand-new block above the existing one — a
    double-fenced file, with the stale fence left sitting in the body —
    instead of rewriting it in place.
    """

    def test_the_declaration_is_visible_to_both_skill_dialects(self) -> None:
        doc = "---\r\nname: x\r\nalways: true\r\ndescription: hello\r\n---\r\nbody\r\n"
        expected = {"name": "x", "always": "true", "description": "hello"}
        assert parse_frontmatter(doc, SKILL_LOADER) == expected
        assert parse_frontmatter(doc, SKILL_UPDATE) == expected

    def test_a_crlf_document_resolves_exactly_like_its_lf_twin(self) -> None:
        # The contract is equality with the LF twin because that is what a
        # YAML reader gives: line breaks are normalised, so a resolved value
        # carries no stray CR. Same header/body matrix as the steering pin in
        # TestTheLiteralFoldTheSkillEditorSimulates, extended over every
        # fence-based dialect.
        for dialect in (SKILL_LOADER, SKILL_UPDATE, STEERING_LOADER):
            for header, body in (
                ("|", ["  one", "", "  two"]),
                ("|", ["", "  one"]),
                ("|-", ["  one", "", "  two"]),
                ("|+", ["  one", ""]),
                (">", ["  one", "", "  two"]),
                ("|2", ["  one", "", "  two"]),
            ):
                frag = "\n".join(["name: s", f"description: {header}", *body, "mode: always"])
                lf = f"---\n{frag}\n---\n\nBody\n"
                crlf = lf.replace("\n", "\r\n")
                want = parse_frontmatter(lf, dialect)
                got = parse_frontmatter(crlf, dialect)
                assert want["name"] == "s", (dialect.extraction, header, body)
                assert got == want, (dialect.extraction, header, body, got, want)
                assert not any("\r" in v for v in got.values()), (dialect.extraction, got)

    def test_an_edit_rewrites_rather_than_prepends(self) -> None:
        # The write-side symptom: with the fence invisible, the writer judged
        # the document as having no frontmatter yet and created a new block
        # ABOVE the existing one instead of editing it in place.
        doc = "---\r\nname: s\r\ndescription: |\r\n  one\r\n---\r\n\r\n# Body\r\n"
        out = set_frontmatter_fields(doc, {"description": "AFTER"}, SKILL_UPDATE)
        assert out == "---\r\nname: s\r\ndescription: AFTER\r\n---\r\n\r\n# Body\r\n"
        assert parse_frontmatter(out, SKILL_UPDATE)["description"] == "AFTER"

    def test_lf_documents_are_unchanged(self) -> None:
        lf = "---\nname: s\ndescription: before\n---\n\n# Body\n"
        out = set_frontmatter_fields(lf, {"description": "AFTER"}, SKILL_UPDATE)
        assert out == "---\nname: s\ndescription: AFTER\n---\n\n# Body\n"
        assert "\r" not in out


class TestInlineComments:
    """An inline ``# ...`` is the author's, and both readers must agree it is.

    YAML starts a comment at a ``#`` preceded by whitespace. Two things went
    wrong without that: the tab read ``manual # rationale`` as the whole string
    and reported an unrecognized mode the agent never saw, and a mode edit
    rebuilt the line and deleted the rationale for good.
    """

    def test_the_steering_dialect_reads_past_an_inline_comment(self) -> None:
        yaml = pytest.importorskip("yaml")
        doc = "---\ninclusion: manual # rationale\n---\nbody\n"
        assert parse_frontmatter(doc, STEERING_LOADER)["inclusion"] == "manual"
        assert yaml.safe_load(doc.split("---")[1])["inclusion"] == "manual"

    def test_other_dialects_are_unchanged(self) -> None:
        # Opt-in per dialect: shortening what the skills catalog already accepts
        # is exactly the silent drift these dialects exist to prevent.
        doc = "---\nname: a # b\n---\nbody\n"
        assert parse_frontmatter(doc, SKILL_LOADER)["name"] == "a # b"

    def test_a_rewrite_keeps_the_comment(self) -> None:
        doc = "---\ninclusion: manual # rationale\n---\nbody\n"
        out = set_frontmatter_fields(doc, {"inclusion": "auto"}, STEERING_LOADER)
        assert "# rationale" in out
        assert parse_frontmatter(out, STEERING_LOADER)["inclusion"] == "auto"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (" manual # note", (" manual", " # note")),
            (" a#b", (" a#b", "")),          # no whitespace before # -> value
            (' "a # b"', (' "a # b"', "")),  # quoted -> content
            (' "x"  # note', (' "x"', "  # note")),
        ],
    )
    def test_the_split_follows_yaml_comment_rules(self, raw: str, expected: tuple) -> None:
        assert split_inline_comment(raw) == expected


class TestExplicitIndentBlockScalars:
    """``|2``, ``>2-`` and friends are valid YAML block-scalar headers this
    module's READER still does not fold (a documented, pre-existing limit).
    The WRITE path must still know they are one, though: otherwise a `#`-shaped
    continuation line reads as an ordinary YAML comment — the rule a plain
    scalar's tail follows — and is left orphaned by a rewrite of the key above
    it, detaching the author's content from the field it was written under.
    """

    @pytest.mark.parametrize("header", ["|2", ">2-", "|2+", ">3", "|-2", "|+2", ">-2"])
    def test_a_comment_shaped_line_is_still_consumed(self, header: str) -> None:
        yaml = pytest.importorskip("yaml")
        doc = f"---\ninclusion: {header}\n    manual\n    # note\nname: x\n---\nbody\n"
        out = set_frontmatter_fields(doc, {"inclusion": "auto"}, STEERING_LOADER)
        block = out.split("---")[1]
        assert "# note" not in block
        assert yaml.safe_load(block) == {"inclusion": "auto", "name": "x"}


class TestMultilineFieldsAreReplacedWhole:
    """A rewritten field must not leave its continuation lines behind.

    The write follows YAML's reading, not this module's. Under
    ``reject_indented`` an indented line is prose to the parser here, so
    ``inclusion:`` above an indented ``manual`` reads as an EMPTY inclusion —
    while a YAML reader folds the two together. An orphan left behind makes the
    document and the dashboard disagree, silently, about the declared mode.
    """

    def test_a_plain_multiline_value_is_replaced_whole(self) -> None:
        yaml = pytest.importorskip("yaml")
        doc = "---\ninclusion:\n  manual\n---\n# T\nbody\n"
        assert yaml.safe_load(doc.split("---")[1]) == {"inclusion": "manual"}
        out = set_frontmatter_fields(doc, {"inclusion": "auto"}, STEERING_LOADER)
        # Both readers, because the bug was that they diverged.
        assert yaml.safe_load(out.split("---")[1]) == {"inclusion": "auto"}
        assert parse_frontmatter(out, STEERING_LOADER) == {"inclusion": "auto"}
        assert "manual" not in out.split("---")[1]

    def test_removing_a_multiline_value_takes_its_continuation(self) -> None:
        yaml = pytest.importorskip("yaml")
        doc = "---\nname: x\ninclusion:\n  manual\n---\nbody\n"
        out = set_frontmatter_fields(doc, {"inclusion": None}, STEERING_LOADER)
        assert yaml.safe_load(out.split("---")[1]) == {"name": "x"}

    def test_an_indented_comment_is_not_consumed(self) -> None:
        # YAML reads an indented ``#`` line as a COMMENT, not as part of the
        # scalar, so consuming it would delete the author's own note from their
        # document during an unrelated mode edit.
        doc = "---\ninclusion: manual\n  # keep me\nname: x\n---\nbody\n"
        out = set_frontmatter_fields(doc, {"inclusion": "auto"}, STEERING_LOADER)
        assert "# keep me" in out

    def test_a_hash_inside_a_block_scalar_is_content_and_is_consumed(self) -> None:
        # The exception is scoped: inside a block scalar the same line is
        # CONTENT to YAML, so it belongs to the value being replaced.
        doc = "---\ndesc: |\n  a\n  # content\n  b\nname: x\n---\nbody\n"
        out = set_frontmatter_fields(doc, {"desc": "short"}, STEERING_LOADER)
        assert "# content" not in out.split("---")[1]

    def test_a_blank_line_does_not_end_a_plain_scalar(self) -> None:
        """What FOLLOWS the blank decides, not the blank itself.

        YAML keeps folding a plain scalar while an indented line follows, so
        ``a`` + blank + ``  b`` is ONE value. Stopping at the blank left the tail
        attached to a replaced key and the stored mode stopped matching the one
        the author picked.
        """
        yaml = pytest.importorskip("yaml")
        doc = "---\ninclusion:\n  manual\n\n  more\nname: x\n---\nbody\n"
        assert yaml.safe_load(doc.split("---")[1])["inclusion"] == "manual\nmore"
        out = set_frontmatter_fields(doc, {"inclusion": "auto"}, STEERING_LOADER)
        assert yaml.safe_load(out.split("---")[1]) == {"inclusion": "auto", "name": "x"}

    def test_a_blank_before_a_column_zero_key_ends_it(self) -> None:
        doc = "---\ninclusion:\n  manual\n\nname: x\n---\nbody\n"
        out = set_frontmatter_fields(doc, {"inclusion": "auto"}, STEERING_LOADER)
        assert "name: x" in out
        assert parse_frontmatter(out, STEERING_LOADER)["name"] == "x"

    def test_a_blank_before_an_indented_comment_ends_it(self) -> None:
        # A comment stays a comment across a blank line, so it is not swept up
        # with the value being replaced.
        doc = "---\ninclusion:\n  manual\n\n  # note\nname: x\n---\nbody\n"
        out = set_frontmatter_fields(doc, {"inclusion": "auto"}, STEERING_LOADER)
        assert "# note" in out


class TestWrittenValuesStayLoadableYaml:
    """A written value must come back identical from a real YAML reader.

    This module's own parser is line-wise and forgiving, so a round-trip through
    it cannot catch what matters: the document is written for kiro-cli, which
    loads it as YAML. There a bare scalar is retyped (``true`` -> bool, ``123``
    -> int), re-cut (``a # b`` -> ``a``), or refused outright (a leading ``*``
    opens an alias). Each case leaves the author's pattern silently not what they
    typed — or the whole file unparseable for its only real consumer.
    """

    ROUND_TRIP = [
        # Leading indicators: alias, flow collection, tag, anchor, directive.
        "*.ts", "[abc].ts", "{a,b}.ts", "!x.ts", "&y.ts", "@x.ts", "%x.ts", "`x.ts",
        # Resolver keywords and numbers — a bare one stops being a string.
        "true", "false", "no", "on", "off", "null", "~", "123", "1.5",
        # An unquoted ``#`` after a space opens a comment and truncates the value.
        "src # old/*.ts",
        # Ordinary, and deliberately boring: these must not regress.
        "src/**/*.ts", "?.ts", "-x.ts", "a: b", "  padded  ", "", "it's ok", "日本語/*.ts",
    ]

    @pytest.mark.parametrize("value", ROUND_TRIP)
    def test_value_survives_yaml_and_our_own_reader(self, value: str) -> None:
        yaml = pytest.importorskip("yaml")
        doc = set_frontmatter_fields(
            "---\ninclusion: always\n---\n\nbody\n",
            {"inclusion": "fileMatch", "fileMatchPattern": value},
            STEERING_LOADER,
        )
        assert yaml.safe_load(doc.split("---")[1])["fileMatchPattern"] == value
        assert parse_frontmatter(doc, STEERING_LOADER)["fileMatchPattern"] == value

    @pytest.mark.parametrize("mode", ["always", "fileMatch", "manual", "auto"])
    def test_the_mode_vocabulary_stays_unquoted(self, mode: str) -> None:
        # Quoting is not free: it rewrites a line in the author's own document.
        # The closed mode vocabulary is plain under every resolver, so it stays bare.
        assert _render_frontmatter_value(mode) == mode

    # Two different failures, and the quiet one is worse. A C0 control, DEL or a
    # C1 control makes the document unloadable outright; NEL and the line and
    # paragraph separators are LINE BREAKS to a YAML reader, so the file still
    # parses and the author's pattern comes back as something they never wrote.
    @pytest.mark.parametrize(
        "value",
        ["a\x00b", "a\x01b", "a\x0bb", "a\x0cb", "a\x1bb", "a\x7fb",
         "a\x85b", "a\x9fb", "a\u2028b", "a\u2029b"],
        ids=["nul", "soh", "vt", "ff", "esc", "del", "nel", "c1", "ls", "ps"],
    )
    def test_a_control_character_is_refused(self, value: str) -> None:
        with pytest.raises(ValueError):
            set_frontmatter_fields("---\na: b\n---\n", {"fileMatchPattern": value}, STEERING_LOADER)

    @pytest.mark.parametrize("escape", ['"\\ud800"', '"\\udfff"', '"a\\udc00b"'])
    def test_a_lone_surrogate_is_refused(self, escape: str) -> None:
        """Refused for a third reason: it is not encodable as UTF-8 at all.

        A lone surrogate never reaches a YAML reader — it raises
        ``UnicodeEncodeError`` at the first ``.encode()`` on the write path, so a
        malformed request would answer 500 instead of a refusal. JSON hands one
        over willingly, which is how the API can be given a character no editor
        can type.
        """
        value = json.loads(escape)
        with pytest.raises(ValueError):
            set_frontmatter_fields("---\na: b\n---\n", {"fileMatchPattern": value}, STEERING_LOADER)

    @pytest.mark.parametrize("value", ["a\tb", "src/**/*.ts", "日本語/*.ts"])
    def test_tab_and_non_ascii_still_round_trip(self, value: str) -> None:
        # TAB is the one control YAML allows, and the refusal above must not
        # widen into "anything unusual", which would reject ordinary globs.
        yaml = pytest.importorskip("yaml")
        doc = set_frontmatter_fields(
            "---\ninclusion: always\n---\n\nbody\n",
            {"fileMatchPattern": value},
            STEERING_LOADER,
        )
        assert yaml.safe_load(doc.split("---")[1])["fileMatchPattern"] == value
        assert parse_frontmatter(doc, STEERING_LOADER)["fileMatchPattern"] == value

    @pytest.mark.parametrize("value", ['a"b', "a\\b", "'quoted'", "trailing'"])
    def test_unspellable_values_are_refused_not_mangled(self, value: str) -> None:
        # This writer emits no escape sequences and its reader understands none,
        # so there is no spelling both agree on. Refusing beats writing a document
        # that loads as something else.
        with pytest.raises(ValueError):
            set_frontmatter_fields("---\na: b\n---\n", {"fileMatchPattern": value}, STEERING_LOADER)
