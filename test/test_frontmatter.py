"""Snapshot tests pinning the consolidated frontmatter parser to the four
grammars its call sites historically accepted.

The expected values below were captured by running the four pre-consolidation
parsers (``SkillsLoader._parse_frontmatter``, ``onboarding_import._frontmatter``,
``history._frontmatter_value``, and the ``discover.py`` handler copy) against
this corpus. They are the oracle for the refactor: a change in any expectation
means a caller's accepted-input surface moved, which is a behavior change with
its own review — not a refactor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import history
from kiro_crew.frontmatter import (
    ONBOARDING_IMPORT,
    PROVIDER_PREVIEW,
    SKILL_LOADER,
    SKILL_UPDATE,
    FrontmatterDialect,
    frontmatter_value,
    parse_frontmatter,
    split_frontmatter,
)
from kiro_crew.onboarding_import import _column0_activation_declared, _frontmatter
from kiro_crew.skills import SkillsLoader

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
    "block_scalar_blank_fold": {"description": "para one\npara two"},
    "block_scalar_chomped": {"description": "folded text", "name": "x"},
    "block_scalar_folded": {"description": "first line second line"},
    "block_scalar_junk_keys": {"description": "Steps: do x\nmore: prose"},
    "block_scalar_literal": {"description": "line one\nline two"},
    "block_scalar_quoted_inside": {"k": '"quoted"'},
    "body_padding": {"k": "v"},
    "closer_indented": {},
    "closer_trailing_junk": {"name": "x"},
    "colon_in_value": {"url": "http://example.com:8080"},
    "crlf": {"name": "x"},
    "duplicate_keys": {"k": "second"},
    "duplicate_plain_then_scalar": {"k": "from scalar"},
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
    "block_scalar_blank_fold": {"description": "para one\npara two"},
    "block_scalar_chomped": {"description": "folded text", "name": "x"},
    "block_scalar_folded": {"description": "first line second line"},
    "block_scalar_junk_keys": {"description": "Steps: do x\nmore: prose"},
    "block_scalar_literal": {"description": "line one\nline two"},
    "block_scalar_quoted_inside": {"k": '"quoted"'},
    "body_padding": {"k": "v"},
    "closer_indented": {},
    "closer_trailing_junk": {"name": "x"},
    "colon_in_value": {"url": "http://example.com:8080"},
    "crlf": {},
    "duplicate_keys": {"k": "first"},
    # first_key_wins both ways: the plain value survives a later scalar
    # duplicate (whose lines the shared scanner consumes but the original
    # never even read — the lookup result is identical), and a resolved
    # scalar survives a later plain duplicate.
    "duplicate_plain_then_scalar": {"k": "plain"},
    "duplicate_scalar_then_plain": {"k": "from scalar"},
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

DISCOVER_EXPECTED: dict[str, dict[str, str]] = {
    "bare_open_fence": {},
    "block_scalar_blank_fold": {"description": ">"},
    "block_scalar_chomped": {"description": ">-", "name": "x"},
    "block_scalar_folded": {"description": ">"},
    "block_scalar_junk_keys": {"description": "|"},
    "block_scalar_literal": {"description": "|"},
    "block_scalar_quoted_inside": {"k": "|"},
    "body_padding": {"k": "v"},
    "closer_indented": {},
    "closer_trailing_junk": {"name": "x"},
    "colon_in_value": {"url": "http://example.com:8080"},
    "crlf": {"name": "x"},
    "duplicate_keys": {"k": "second"},
    "duplicate_plain_then_scalar": {"k": "|"},
    "duplicate_scalar_then_plain": {"k": "plain"},
    "empty_block": {},
    "empty_text": {},
    "empty_value": {"key": ""},
    "four_dash_fences": {"key": "v"},
    "indented_shadow_before": {"k": "real"},
    "leading_ws_before_opener": {},
    "mismatched_quotes": {"k": "\"a'"},
    "no_closer": {},
    "no_colon_line": {"name": "x"},
    "opener_junk_with_colon": {"": "y", "name": "x"},
    "opener_trailing_junk": {"name": "x"},
    "plain_prose": {},
    "quoted_values": {"desc": "'single'", "multi": '""double""', "name": '"quoted"'},
    "simple": {"description": "hello", "name": "x"},
    "space_indented_key": {"name": "x"},
    "tab_indented_key": {"name": "x", "steps": "tabbed"},
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


class TestProviderPreviewDialect:
    @pytest.mark.parametrize("case_id", sorted(CORPUS))
    def test_snapshot(self, case_id: str) -> None:
        assert parse_frontmatter(CORPUS[case_id], PROVIDER_PREVIEW) == DISCOVER_EXPECTED[case_id]


class TestDialectContracts:
    """The four dialects stay distinct — collapsing any two axes silently
    changes some caller's accepted-input surface."""

    def test_presets_are_distinct(self) -> None:
        presets = [SKILL_LOADER, ONBOARDING_IMPORT, SKILL_UPDATE, PROVIDER_PREVIEW]
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
        assert parse_frontmatter(text, PROVIDER_PREVIEW)["k"] == '"v"'

    def test_block_scalar_resolution_is_per_dialect(self) -> None:
        text = "---\nk: |\n  content\n---\n"
        assert parse_frontmatter(text, SKILL_LOADER)["k"] == "content"
        assert parse_frontmatter(text, SKILL_UPDATE)["k"] == "content"
        assert parse_frontmatter(text, PROVIDER_PREVIEW)["k"] == "|"
        assert parse_frontmatter(text, ONBOARDING_IMPORT)["k"] == "|"

    def test_quote_strip_never_applies_to_a_resolved_scalar(self) -> None:
        # SKILL_LOADER strips quotes from plain values but a resolved block
        # scalar keeps its content verbatim, quotes included.
        text = '---\nk: |\n  "quoted"\n---\n'
        assert parse_frontmatter(text, SKILL_LOADER)["k"] == '"quoted"'

    def test_split_returns_text_unchanged_without_block(self) -> None:
        for dialect in (SKILL_LOADER, ONBOARDING_IMPORT, SKILL_UPDATE, PROVIDER_PREVIEW):
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
        # The other three modes' remainders are pinned here as NOT renderable:
        # prefix_find's still begins with the closer token, and the two fence
        # modes cut immediately after "---" — mid-line when the closer
        # carries trailing text.
        text = "---\nk: v\n---\nbody\n"
        assert split_frontmatter(text, ONBOARDING_IMPORT)[1] == "body"
        assert split_frontmatter(text, PROVIDER_PREVIEW)[1].startswith("\n---")
        assert split_frontmatter(text, SKILL_LOADER)[1] == "\nbody\n"
        assert split_frontmatter(text, SKILL_UPDATE)[1] == "\nbody\n"
        junk_closer = "---\nk: v\n---junk\nbody\n"
        assert split_frontmatter(junk_closer, SKILL_LOADER)[1] == "junk\nbody\n"
        assert split_frontmatter(junk_closer, SKILL_UPDATE)[1] == "junk\nbody\n"
