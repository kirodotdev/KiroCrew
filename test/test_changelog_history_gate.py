"""The changelog-history gate must protect exactly the shipped sections.

``scripts/check_changelog_history.py`` loads its version folding from
:mod:`kiro_crew.changelog` by path rather than re-implementing it, so there is no
second copy to drift. What still needs pinning is the gate's own judgment: which
headings count as shipped history, and which diffs against them are violations.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from kiro_crew.changelog import base_version

_REPO = Path(__file__).resolve().parents[1]
_GATE = _REPO / "scripts" / "check_changelog_history.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("_changelog_history_gate", _GATE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def test_the_gate_uses_the_renderers_own_folding() -> None:
    """Not a maintained copy -- the renderer's own function, loaded by path.

    The earlier design duplicated the fold regex, which fails in the dangerous
    direction: a spelling the renderer folds but the gate does not makes a
    legitimate release-branch rewrite read as a deleted section. Identity is
    asserted by name and by agreement rather than by ``is``, because the gate
    loads ``changelog.py`` as its own module object (CI checks the tree out
    without installing the package), so the two function objects are distinct
    even though the code is the same.
    """
    assert gate.fold_version.__name__ == base_version.__name__ == "base_version"
    for spelling in (
        "0.3.0",
        "0.3.0-rc.2",
        "0.3.0-insider.11",
        "0.3.0-nightly.20260806t065257",
        "0.3.0rc4",
        "0.3.0.dev20260806065257",
        "0.3.0+local",
        "1.2.3.4",
        "0.3.0rc4junk",
    ):
        assert gate.fold_version(spelling) == base_version(spelling)


@pytest.mark.parametrize(
    "version,shipped",
    [
        ("0.3.0", True),
        ("1.2", True),
        ("1.2.3.4", True),
        ("0.3.0-insider.9", False),
        ("0.3.0-rc.2", False),
        ("0.3.0rc4", False),
        ("0.3.0.dev20260806065257", False),
        ("0.3.0-nightly.20260806t065257", False),
        ("0.3.0+local", False),
        ("Unreleased", False),
        ("", False),
        # A malformed spelling keeps its own identity in the renderer, so it is
        # its own fold and reads as shipped here. That is the safe direction: it
        # cannot collide with the real 0.3.0 (the hazard the renderer documents),
        # and the only consequence is that a junk heading is also protected from
        # silent edits.
        ("0.3.0rc4junk", True),
    ],
)
def test_only_a_bare_release_counts_as_shipped(version: str, shipped: bool) -> None:
    assert gate.is_shipped_heading(version) is shipped


def test_the_gates_own_self_test_passes() -> None:
    """CI runs ``--test`` before enforcing; pin it so a weakened rule fails here."""
    assert gate._self_test() == 0


def test_deleting_a_shipped_section_is_a_violation() -> None:
    base = "# Changelog\n\n## [0.3.0]\n\nd\n\n## [0.2.0]\n\nshipped\n\n"
    head = "# Changelog\n\n## [0.3.0]\n\nd\n\n"
    problems = gate.compare(base, head)
    assert len(problems) == 1
    assert "[0.2.0]" in problems[0] and "GONE" in problems[0]


def test_modifying_a_shipped_section_is_a_violation() -> None:
    base = "# Changelog\n\n## [0.3.0]\n\nd\n\n## [0.2.0]\n\nshipped\n\n"
    head = "# Changelog\n\n## [0.3.0]\n\nd\n\n## [0.2.0]\n\nEDITED\n\n"
    problems = gate.compare(base, head)
    assert len(problems) == 1
    assert "[0.2.0]" in problems[0] and "renders DIFFERENT notes" in problems[0]


def test_rewriting_a_shipped_sections_date_is_a_violation() -> None:
    """The heading is part of the shipped record, not decoration.

    Comparing bodies alone let a shipped release's date be rewritten while the
    gate reported the section intact.
    """
    base = "# Changelog\n\n## [0.3.0]\n\nd\n\n## [0.2.0] — 2026-08-09\n\nshipped\n\n"
    head = "# Changelog\n\n## [0.3.0]\n\nd\n\n## [0.2.0] — 2026-01-01\n\nshipped\n\n"
    problems = gate.compare(base, head)
    assert len(problems) == 1
    assert "[0.2.0]" in problems[0] and "renders DIFFERENT notes" in problems[0]


def test_a_duplicate_release_heading_is_a_violation() -> None:
    """Two sections for one release is a defect, and it can mask an edit.

    Edit the real section, append an untouched copy, and any check that folds
    the two together reports no change while the file carries altered history.
    """
    base = "# Changelog\n\n## [0.3.0]\n\nd\n\n## [0.2.0]\n\nshipped\n\n"
    head = "# Changelog\n\n## [0.3.0]\n\nd\n\n" "## [0.2.0]\n\nEDITED\n\n## [0.2.0]\n\nshipped\n\n"
    problems = gate.compare(base, head)
    # TWO findings, and both are wanted. Folding first-wins, the edited copy sits
    # above the untouched one and therefore becomes what the renderer displays --
    # so the resolved comparison sees changed notes, and the heading count sees the
    # duplicate. Under the earlier last-wins fold only the count fired, which is
    # what let an inverted fold direction look correct.
    assert len(problems) == 2
    assert any("renders DIFFERENT notes" in p for p in problems)
    assert any("documented by 2 headings" in p for p in problems)
    assert all("[0.2.0]" in p for p in problems)


def test_editing_the_newest_shipped_section_is_a_violation() -> None:
    """There is no "most recent section" exemption, and that is the point.

    An earlier version exempted the newest section at base so a release branch
    could redraft its own entry. On the default branch the newest section is the
    LAST SHIPPED release, so that exemption made the section the most users are
    running the only editable one. Prerelease-versus-bare distinguishes a draft
    from history; position in the file does not.
    """
    base = "# Changelog\n\n## [0.3.0]\n\nreleased\n\n## [0.2.0]\n\nshipped\n\n"
    head = "# Changelog\n\n## [0.3.0]\n\nREWRITTEN\n\n## [0.2.0]\n\nshipped\n\n"
    problems = gate.compare(base, head)
    assert len(problems) == 1
    assert "[0.3.0]" in problems[0] and "renders DIFFERENT notes" in problems[0]


def test_a_prerelease_heading_is_a_draft_not_shipped_history() -> None:
    """A release branch may replace its own in-progress entry.

    Nothing shipped is documented under a prerelease heading, so swapping
    ``[0.3.0-insider.9]`` for the written ``[0.3.0]`` is not a deletion.
    """
    base = "# Changelog\n\n## [0.3.0-insider.9]\n\ngenerated\n\n## [0.2.0]\n\nshipped\n\n"
    head = "# Changelog\n\n## [0.3.0] — 2026-08-17\n\nwritten\n\n## [0.2.0]\n\nshipped\n\n"
    assert gate.compare(base, head) == []
    # And it is not parsed as protectable history in the first place.
    assert [v for v, _ in gate.parse_sections(base)] == ["0.2.0"]


def test_the_original_incident_shape_is_caught() -> None:
    """A section replaced rather than prepended: insertions masking deletions."""
    base = (
        "# Changelog\n\n## [Unreleased]\n\npending\n\n"
        "## [0.2.0]\n\na\n\n## [0.1.3]\n\nb\n\n## [0.1.2]\n\nc\n\n"
    )
    head = "# Changelog\n\n## [Unreleased]\n\npending\n\n## [0.3.0-insider.9]\n\ngenerated\n\n"
    problems = gate.compare(base, head)
    assert len(problems) == 3
    assert {"[0.2.0]", "[0.1.3]", "[0.1.2]"} == {p.split(" ")[0] for p in problems}


def test_a_prerelease_heading_cannot_shadow_a_shipped_release() -> None:
    """The third defect class: two headings, one rendered release.

    Adding ``[0.2.0-rc.2]`` beside a shipped ``[0.2.0]`` left the earlier
    filter-then-compare check reporting the file intact, while the renderer folds
    both onto 0.2.0 and keeps one body -- so which notes a user sees depended on
    document order. Caught in BOTH orders, because the harmless-looking order is
    still a file whose rendered history is order-dependent.
    """
    base = "# Changelog\n\n## [0.3.0]\n\nr\n\n## [0.2.0]\n\nshipped\n\n"
    for head in (
        "# Changelog\n\n## [0.3.0]\n\nr\n\n## [0.2.0-rc.2]\n\nDRAFT\n\n## [0.2.0]\n\nshipped\n\n",
        "# Changelog\n\n## [0.3.0]\n\nr\n\n## [0.2.0]\n\nshipped\n\n## [0.2.0-rc.2]\n\nDRAFT\n\n",
    ):
        problems = gate.compare(base, head)
        assert problems, "a draft heading folding onto shipped history must be caught"
        assert any("0.2.0-rc.2" in p or "documented by 2 headings" in p for p in problems)


def test_a_prerelease_for_an_unshipped_release_is_allowed() -> None:
    """Only a collision with SHIPPED history is a defect, not drafting itself."""
    base = "# Changelog\n\n## [0.2.0]\n\nshipped\n\n"
    head = "# Changelog\n\n## [0.4.0-rc.1]\n\ndrafting\n\n## [0.2.0]\n\nshipped\n\n"
    assert gate.compare(base, head) == []


def test_resolve_as_rendered_folds_first_wins_like_the_renderer() -> None:
    """The gate must resolve a release the way the renderer displays it.

    ``build_release_list`` uses ``dict.setdefault``, so the FIRST body in document
    order wins -- which under newest-first ordering is the topmost spelling. This
    test asserted the opposite for several rounds and stayed green, because every
    other rule happened to cover the documents in this suite; the gate was merely
    comparing a section no user is ever shown. So assert the direction explicitly
    AND against the renderer, which is the only party that cannot be wrong here.
    """
    text = "# Changelog\n\n## [0.2.0]\n\ntopmost\n\n## [0.2.0-rc.9]\n\nlower\n\n"
    resolved = gate.resolve_as_rendered(text)["0.2.0"]
    assert "topmost" in resolved
    assert "lower" not in resolved

    rendered = {r.version: r.body for r in gate.render_releases(text, "0.2.0")}
    assert ("topmost" in rendered["0.2.0"]) == ("topmost" in resolved)


def _narrowed_grammar() -> "gate.Grammar":
    """A grammar that requires a date, so a dateless heading stops being seen."""
    return gate.Grammar(
        section_re=re.compile(
            r"^##\s+\[(?P<version>[^\]]+)\]\s*[—–-]\s*" r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})\s*$"
        ),
        h2_re=gate.HEAD_GRAMMAR.h2_re,
        fold=gate.HEAD_GRAMMAR.fold,
    )


def test_a_narrowed_head_grammar_cannot_shrink_the_protected_set() -> None:
    """The base's grammar decides what the base documents, not the head's.

    A PR may legitimately change ``changelog.py``. If it narrows the heading regex,
    parsing base history with the narrowed grammar drops those releases out of the
    protected set — and a deletion in the same PR then passes unseen. That is a
    silent loss of a section a user already installed, so the base ref's own
    renderer is the authority on what the base shipped.
    """
    base = "# Changelog\n\n## [0.2.0] — 2026-08-09\n\nkept\n\n## [0.1.2]\n\nshipped\n\n"
    head = "# Changelog\n\n## [0.2.0] — 2026-08-09\n\nkept\n\n"

    problems = gate.compare(base, head, gate.HEAD_GRAMMAR, _narrowed_grammar())
    assert any("[0.1.2]" in p and "GONE" in p for p in problems)

    # the same document under ONE grammar is exactly the hole: the narrowed grammar
    # never saw [0.1.2] at base, so it had nothing to miss
    narrowed = _narrowed_grammar()
    assert gate.compare(base, head, narrowed, narrowed) == []


def test_a_base_that_parses_to_zero_shipped_releases_fails_closed() -> None:
    """Zero protected sections is only safe when the base really shipped nothing.

    A base carrying version headings that yields no shipped release means the parser
    lost them. Returning "no violations" there silently disables the whole gate, so
    it is reported instead.
    """
    blind = gate.Grammar(
        section_re=re.compile(r"^##\s+NEVER-MATCHES-(?P<version>x)$"),
        h2_re=gate.HEAD_GRAMMAR.h2_re,
        fold=gate.HEAD_GRAMMAR.fold,
    )
    base = "# Changelog\n\n## [0.2.0] — 2026-08-09\n\nshipped\n\n"
    problems = gate.compare(base, "# Changelog\n", blind, gate.HEAD_GRAMMAR)
    assert len(problems) == 1
    assert "NONE parsed as a shipped release" in problems[0]


def test_a_base_with_no_version_headings_is_still_a_clean_no_op() -> None:
    """The fail-closed guard must not fire on a genuinely release-less base."""
    base = "# Changelog\n\n## Unreleased\n\npending\n"
    assert gate.compare(base, "# Changelog\n\n## [0.1.0] — 2026-01-01\n\nfirst\n") == []


def test_the_gate_reads_the_grammar_from_the_renderer_it_is_given() -> None:
    """`grammar_of` takes the heading grammar, never a second copy of it."""
    # Anchored to the repo, never to the process CWD: pytest can be invoked from
    # anywhere and a CWD-relative read would fail with FileNotFoundError.
    renderer = gate._load_renderer_source(
        (_REPO / "src" / "kiro_crew" / "changelog.py").read_text(encoding="utf-8")
    )
    grammar = gate.grammar_of(renderer)
    assert grammar.section_re is renderer._SECTION_RE
    assert grammar.h2_re is renderer._H2_RE
    assert grammar.fold("0.3.0-insider.9") == "0.3.0"


# ── head carries shipped sections and nothing else ────────────────────────


@pytest.mark.parametrize(
    "heading",
    [
        "## [Unreleased]",
        "## Unreleased",
        "## [0.4.0-rc.1]",
        "## [0.4.0-rc.1] — 2026-08-21",
        "## [0.4.0-insider.3]",
        "## [0.4.0-nightly.20260821t000000]",
    ],
)
def test_a_draft_heading_is_refused_at_head(heading: str) -> None:
    """No section may describe software nobody has installed.

    This is what leaves prepending a new section as the only legal changelog
    diff: with no draft section present, a feature PR has nowhere to append a
    per-PR line, and ``compare`` already freezes every released section.
    """
    assert gate.draft_headings(f"# Changelog\n\n{heading}\n\nbody\n"), heading


def test_a_draft_is_found_wherever_it_sits() -> None:
    """Position must not matter -- a draft below shipped history still renders."""
    text = (
        "# Changelog\n\n## [0.3.0] — 2026-08-17\n\nshipped\n\n"
        "## [Unreleased]\n\npending\n\n## [0.2.0] — 2026-08-09\n\nolder\n"
    )
    assert gate.draft_headings(text) == ["Unreleased"]


def test_a_file_of_shipped_sections_only_is_clean() -> None:
    text = "# Changelog\n\n## [0.3.0] — 2026-08-17\n\na\n\n## [0.2.0] — 2026-08-09\n\nb\n"
    assert gate.draft_headings(text) == []


def test_a_level_three_heading_inside_a_section_is_not_a_draft() -> None:
    """The rule reads level-2 headings only; a section's own `###` groups are its body."""
    text = (
        "# Changelog\n\n## [0.3.0] — 2026-08-17\n\n"
        "### Before you upgrade\n\n- a thing\n\n### Notable fixes\n\n- another\n"
    )
    assert gate.draft_headings(text) == []


def test_the_repos_own_changelog_carries_no_draft_section() -> None:
    """The live invariant, not just the diff.

    A diff-based check only fires on the PR that introduces a draft. Asserting
    the file itself means the state cannot persist even if one slipped in.
    """
    text = (_REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert gate.draft_headings(text) == []


def test_compare_still_reads_a_draft_heading_in_HISTORY_without_flagging_it() -> None:
    """The two rules are separate on purpose, and this is the case that needs it.

    ``compare`` answers "did this change rewrite shipped history" and is fed base
    text from refs that legitimately predate the draft-section rule. Folding the
    head-only rule into it would make the immutability verdict depend on whether
    a draft happened to be present in history.
    """
    base = "# Changelog\n\n## [0.3.0-insider.9]\n\ngenerated\n\n## [0.2.0]\n\nshipped\n\n"
    head = "# Changelog\n\n## [0.3.0] — 2026-08-17\n\nwritten\n\n## [0.2.0]\n\nshipped\n\n"
    assert gate.compare(base, head) == []
    assert gate.draft_headings(base) == ["0.3.0-insider.9"]
    assert gate.draft_headings(head) == []
