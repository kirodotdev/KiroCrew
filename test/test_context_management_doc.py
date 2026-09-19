"""The context-management guide's SHARES and rules are pinned to the code.

``scripts/docs_lint.py`` gates a doc's SYMBOLS (``dead-identifier``) and its PATHS
(``path-exists``), never what it claims about them. So a doc saying "skills get 15%
of the base" keeps a resolving symbol and a resolving path after someone changes
``_SKILLS_CAP`` to ``_budget(0.10)`` -- lint stays green and the doc confidently
lies, which is worse than saying nothing because a reader has no reason to doubt it.

This module closes that gap for ``docs/architecture/context-management.md``.

It asserts SHARES rather than byte figures, and that is the point rather than an
accident. ``docs/system-specs/common/code-style.md`` owns the rule that context
budgets "are expressed as FRACTIONS of the base, so read them there rather than
quoting a byte figure". The doc therefore quotes ``16%`` plus
``_MEMORY_HISTORY_CAP``, and this test recomputes that percentage from the live
constants (``cap / _CONTEXT_BUDGET_BASE``) and fails when the two disagree.

Pinning the share is strictly stronger than pinning the bytes was: a change to the
base alone moves every byte figure and no share, so the doc stays correct through
it -- which is the convention's own argument. A change to a FRACTION moves the
share, and this test catches that.

The behavioural tables are asserted against the functions that implement them, by
calling them where possible, so a behaviour-preserving rewrite stays green.
"""

import re
from pathlib import Path

import pytest

from kiro_crew import context as ctx
from kiro_crew.config.sections import SkillsConfig
from kiro_crew.members import MEMBER_BRIEFING_MAX_CHARS, MEMBER_RULES_MAX_CHARS
from kiro_crew.session_ledger import _SNAPSHOT_MAX_CHARS
from kiro_crew.trigger_match import MIN_TRIGGER_OVERLAP

DOC = Path(__file__).parent.parent / "docs" / "architecture" / "context-management.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _shares_table(text: str) -> list[str]:
    """Just the rows of the budgets section's tables.

    Scoped deliberately: other tables name these constants too (the "Where to
    look" table names most of them), so reading the first row that merely MENTIONS
    a constant would find the wrong cell.
    """
    start = text.index("### Budgets and caps")
    end = text.index("\n## ", start)
    return [ln for ln in text[start:end].splitlines() if ln.startswith("|")]


def _share_of(text: str, symbol: str) -> float:
    """The percentage stated in the shares-table row naming *symbol*.

    The value cell is read rather than the whole line, so a constant whose own
    name contains digits cannot be mistaken for its share.
    """
    for line in _shares_table(text):
        if f"`{symbol}`" not in line:
            continue
        for cell in (c.strip() for c in line.strip("|").split("|")):
            if f"`{symbol}`" in cell:
                continue
            match = re.search(r"(\d+(?:\.\d+)?)\s*%", cell)
            if match:
                return float(match.group(1))
    raise AssertionError(f"no row of the shares table in {DOC.name} states a % for `{symbol}`")


#: Every ``_budget(fraction)`` cap the doc tabulates, as (constant name, live value).
#: The expected share is DERIVED from the value; a second copy of the fraction here
#: would be the very duplication the convention objects to.
SHARE_ROWS = (
    ("_MEMORY_PREFS_CAP", ctx._MEMORY_PREFS_CAP),
    ("_MEMORY_PROJECTS_CAP", ctx._MEMORY_PROJECTS_CAP),
    ("_MEMORY_HISTORY_CAP", ctx._MEMORY_HISTORY_CAP),
    ("_LESSONS_CAP", ctx._LESSONS_CAP),
    ("_SEMANTIC_MEMORY_CAP", ctx._SEMANTIC_MEMORY_CAP),
    ("_SKILLS_CAP", ctx._SKILLS_CAP),
    ("_STEERING_CAP", ctx._STEERING_CAP),
    ("_PREAMBLE_HEADROOM", ctx._PREAMBLE_HEADROOM),
)


@pytest.mark.parametrize("symbol,live", SHARE_ROWS)
def test_share_table_matches_the_fraction_in_code(doc_text: str, symbol: str, live: int) -> None:
    """The doc's percentage must equal ``cap / base``, recomputed live.

    ``_budget`` truncates, so the recovered fraction is not exact; one decimal
    place is the precision the doc states and is far tighter than any real change
    to a share would be.
    """
    expected = live / ctx._CONTEXT_BUDGET_BASE * 100
    stated = _share_of(doc_text, symbol)
    assert round(stated, 1) == round(expected, 1), (
        f"{DOC.name} states {stated}% for {symbol} but the code computes "
        f"{expected:.2f}%. Update the shares table."
    )


#: Thread history divides its OWN reference base, not the context budget base, so
#: these two shares need their own recomputation rather than a row in SHARE_ROWS.
HISTORY_SHARE_ROWS = (
    ("_HISTORY_BUDGET_CHARS", ctx._HISTORY_BUDGET_CHARS),
    ("_COMPRESSED_HISTORY_CAP", ctx._COMPRESSED_HISTORY_CAP),
)


@pytest.mark.parametrize("symbol,live", HISTORY_SHARE_ROWS)
def test_history_shares_match_their_own_reference_base(
    doc_text: str, symbol: str, live: int
) -> None:
    """The doc states 21% and 27%; recompute both against _HISTORY_REFERENCE_BASE.

    Stated in prose rather than in the shares table -- thread history is scaled by
    the model window on top of its share -- so the percentage is read from the
    sentence that names the constant instead of from a table cell.
    """
    expected = live / ctx._HISTORY_REFERENCE_BASE * 100
    at = doc_text.index(f"`{symbol}`")
    # The NEAREST percentage before the constant, not the first one in the
    # sentence: both shares live in one sentence, so an earliest-match regex
    # read 21% for the compressed cap. Verified by that exact failure.
    before = [m for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", doc_text[:at])]
    assert before, f"no percentage precedes `{symbol}` in {DOC.name}"
    stated = float(before[-1].group(1))
    assert round(stated, 1) == round(expected, 1), (
        f"{DOC.name} states {stated}% for {symbol} but the code computes "
        f"{expected:.2f}% of _HISTORY_REFERENCE_BASE"
    )


def test_no_byte_literals_in_the_shares_table(doc_text: str) -> None:
    """``code-style.md`` forbids quoting the byte figure for a context budget.

    Pinned so the literals cannot drift back: a later edit that "helpfully" adds
    the computed bytes beside a share reintroduces exactly the staleness the
    convention rules out.
    """
    offenders = [ln for ln in _shares_table(doc_text) if re.search(r"\b\d{1,3},\d{3}\b", ln)]
    assert not offenders, (
        "the shares table quotes a byte figure, which "
        "docs/system-specs/common/code-style.md forbids for context budgets "
        f"(state the fraction instead): {offenders}"
    )


def test_semantic_and_episodic_share_one_row(doc_text: str) -> None:
    """The two caps share one row, which is only honest while they are equal."""
    assert (
        ctx._SEMANTIC_MEMORY_CAP == ctx._EPISODIC_MEMORY_CAP
    ), "the caps diverged, so the doc's shared row is now wrong -- split it"
    assert "`_EPISODIC_MEMORY_CAP`" in doc_text


def test_ledger_snapshot_cap_named_not_quoted(doc_text: str) -> None:
    """The work-ledger paragraph names the bound rather than restating it."""
    assert (
        "_SNAPSHOT_MAX_CHARS" in doc_text
    ), "the work-ledger paragraph must name the constant that bounds the snapshot"
    assert (
        f"{_SNAPSHOT_MAX_CHARS:,}" not in doc_text
    ), "the snapshot cap is quoted as a number; name _SNAPSHOT_MAX_CHARS instead"


def test_embedding_deadline_named(doc_text: str) -> None:
    """A timeout in seconds is not a context budget, so its value may be stated."""
    assert "_PROMPT_BUILD_EMBED_TIMEOUT_SECS" in doc_text
    assert (
        ctx._PROMPT_BUILD_EMBED_TIMEOUT_SECS == 5.0
    ), "the deadline changed; the doc says five seconds"


def test_activity_index_cap_is_named_not_quoted(doc_text: str) -> None:
    """The doc points at the ``cap`` default rather than restating it."""
    import inspect

    from kiro_crew.memory import MemoryStore

    default = inspect.signature(MemoryStore.activity_index).parameters["cap"].default
    assert "`activity_index`" in doc_text
    assert (
        f"{default:,}" not in doc_text
    ), "the activity-index cap is quoted as a number; cite the cap default instead"


def test_protected_ceiling_formula_matches_the_code(doc_text: str) -> None:
    """The ceiling is written as a formula over the base, not over its value."""
    assert ctx._PROTECTED_CONTEXT_CHARS_PER_TOKEN == 4.0
    assert ctx._PROTECTED_CONTEXT_WINDOW_FRACTION == 0.125
    assert ctx._PROTECTED_CONTEXT_FLOOR == ctx._CONTEXT_BUDGET_BASE * 3
    assert "window_tokens × 4.0 × 0.125" in doc_text
    assert (
        "max(3 × the base" in doc_text
    ), "the ceiling must be stated over the base, not over its byte value"


def test_member_caps_and_activity_growth(doc_text: str) -> None:
    """The member caps are named, and the activity log's growth bound is STATED.

    The activity log is backed by the event log, which does not rotate, so a doc
    naming a rotation constant would describe a mechanism this code does not have.
    What a reader needs is the bound that IS offered: per append, not over a lifetime.
    """
    assert MEMBER_BRIEFING_MAX_CHARS == MEMBER_RULES_MAX_CHARS
    assert "`MEMBER_BRIEFING_MAX_CHARS`" in doc_text
    assert "`MEMBER_RULES_MAX_CHARS`" in doc_text
    assert "NO rotation" in doc_text, "the absence of rotation has to be stated, not implied"
    assert "accumulates over a member's lifetime" in doc_text
    assert f"{MEMBER_BRIEFING_MAX_CHARS:,}" not in doc_text


def test_trigger_overlap_threshold(doc_text: str) -> None:
    """A fraction, not a byte budget, so the doc may state its value."""
    assert f"MIN_TRIGGER_OVERLAP = {MIN_TRIGGER_OVERLAP}" in doc_text


def test_stated_config_defaults_are_the_real_defaults(doc_text: str) -> None:
    """The doc's two easiest-to-get-backwards claims are the dataclass defaults."""
    skills = SkillsConfig()
    assert skills.max_triggered == 0, "the doc says per-turn trigger matching is OFF by default"
    assert skills.lazy_load is True, "the ranked directory is the default"
    assert "defaults to **0**" in doc_text
    assert "(default true)" in doc_text


def test_session_sharing_default(doc_text: str) -> None:
    from kiro_crew.config.sections import AgentConfig

    assert AgentConfig().session_sharing is True
    assert "(default **true**)" in doc_text


def test_skill_injection_table_matches_the_plan_function(doc_text: str) -> None:
    """The doc's four-row table IS ``_skills_injection_plan``'s truth table.

    Asserted by CALLING it rather than by reading its source, so a rewrite that
    preserves behaviour keeps passing and one that changes behaviour fails.
    ``agent_skill_globs`` is patched through the module attribute the function
    actually resolves, which is what decides the ``globs`` half.

    This is the ONE place the truth table is executed. The same four rows are
    stated as prose in ``src/kiro_crew/docs/agent-spec-fields.md``, whose own
    module (``test_agent_spec_fields_doc.py``) pins that page's wording and defers
    the matrix to here rather than running a second copy of it.

    Both halves of the return are asserted. A plan answering ``(True, [])`` for a
    mapped agent satisfies every "is it injected" check while handing that agent
    the whole catalog its mapping exists to exclude, which is the one outcome the
    tables' "only the mapped set" wording rules out.
    """
    import kiro_crew.context as ctx_mod

    mapping = ["/root/*/SKILL.md"]
    original = ctx_mod.agent_skill_globs
    try:
        ctx_mod.agent_skill_globs = lambda _agent, **_kwargs: []
        for is_cc in (False, True):
            # row 1: kirocrew, unmapped -> the whole catalog, on both backends
            assert ctx_mod._skills_injection_plan("kirocrew", is_cc=is_cc) == (True, [])
            # row 3: custom, unmapped -> nothing, on either backend
            assert ctx_mod._skills_injection_plan("kirocrew-worker", is_cc=is_cc) == (False, [])
        ctx_mod.agent_skill_globs = lambda _agent, **_kwargs: list(mapping)
        for is_cc in (False, True):
            # rows 2 and 4: the mapped set only, on either backend
            for agent in ("kirocrew", "kirocrew-worker"):
                assert ctx_mod._skills_injection_plan(agent, is_cc=is_cc) == (True, mapping)
    finally:
        ctx_mod.agent_skill_globs = original


def test_worker_spec_does_not_mirror_resources(doc_text: str) -> None:
    """The doc's §4 consequence rests on ``resources`` being unmirrored."""
    from kiro_crew.agent import _WORKER_MIRRORED_SHAPES

    assert "resources" not in _WORKER_MIRRORED_SHAPES, (
        "resources is mirrored onto the worker now, so the doc's "
        "'no skill catalog' consequence for kirocrew-worker is stale"
    )
    assert "**`resources` is not mirrored**" in doc_text


def test_capability_sections_listed_completely(doc_text: str) -> None:
    """§6 lists the per-member permission surface; a new section must appear."""
    from kiro_crew.agent_state import CAPABILITY_SECTIONS

    for section in CAPABILITY_SECTIONS:
        assert f"`{section}`" in doc_text, (
            f"CAPABILITY_SECTIONS gained {section!r}; name it in the §6 "
            "per-member permission row"
        )


def test_every_mark_label_is_named(doc_text: str) -> None:
    """§1 lists the section-timing labels; a new one must be added there."""
    source = (Path(__file__).parent.parent / "src" / "kiro_crew" / "context.py").read_text(
        encoding="utf-8"
    )
    labels = set(re.findall(r'_mark\("([a-z_]+)"\)', source))
    assert labels, "no _mark labels found -- the timing marks moved"
    for label in labels:
        assert (
            f"`{label}`" in doc_text
        ), f"context.py stamps _mark({label!r}) but the doc's timing list omits it"
