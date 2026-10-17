"""The board template reads exactly the fields the contract type declares.

``mypy src/kiro_crew/`` checks both ends of :func:`build_pipeline_board`: a
:class:`~kiro_crew.work_vocab.WorkBoardView` in, a
:class:`~kiro_crew.pipeline_board_contract.PipelineBoardPanel` out, required keys and
all. What it cannot see is ``kirocrew-pipeline-conductor.html``, so the HTML end needs a gate of its own.
This is that gate, built after ``test_agent_host_contract_parity``: parse the real
artefact, extract the set it actually uses, and assert equality with the set the code
declares.

Scope, stated honestly, because a reader will assume more than is here. This asserts a
field EXISTS on both sides. It does NOT assert the value is right, or that the template
renders it anywhere a person will see, or that the bucket it came from is the one the
contract intends -- ``stats[].v`` holding a made-up number passes. That is the limit of
a text gate: it makes an omission visible, and a reviewer makes it answered.

What it does structurally: strips comments before reading (a key named only in a
comment must not count -- twice tonight a raw grep over commented code produced a wrong
answer), traces each container variable to the island path it opens and REFUSES when one
is renamed, scopes element reads to their own loop body (``s`` is a progress segment in
one loop and a stat row in another), and flattens the TypedDict tree to the same dotted
spelling so the comparison needs no translation layer -- a translation layer is where a
mismatch hides.

Three attacks it must survive, each of which an earlier draft allowed: a field named
only inside a comment counting as read; a field read in TWO places where set semantics
hide that one of them was the comment; and a broken extractor returning an empty set,
which would read as "the contract is over-specified" instead of "the gate is broken".
The last is why :func:`island_keys` refuses rather than returning a partial set, and why
the preconditions below run as their own cases.
"""

from __future__ import annotations

import re
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent_panel
from kiro_crew.pipeline_board_contract import (
    BOARD_CREW_NAME,
    BOARD_TEMPLATE_ID,
    CONTRACT_VERSION,
    PipelineBoardPanel,
)
from kiro_crew.work_vocab import WORK_ITEM_STATES

# ``BOARD_TEMPLATE_ID`` and ``BOARD_CREW_NAME`` are IMPORTED, not restated here. The
# reader (``_panel_record``) branches on that id to decide whose data it rebuilds, so a
# copy in this file would let the gate keep guarding one template while the reader
# rewrote another's -- a gate on the wrong file reads as coverage.
#
# What this file still owns is the assertion that the id is REACHABLE:
# :func:`test_the_board_template_id_is_reachable_through_crew_selection`. An id no crew's
# name slugifies to names a file nobody renders, which every check here would stay green
# over.


def _template_path() -> Path:
    return agent_panel.shipped_templates_dir() / f"{BOARD_TEMPLATE_ID}.html"


# ---------------------------------------------------------------------------
# flatten the contract type
# ---------------------------------------------------------------------------


def _is_typed_dict(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, dict) and hasattr(tp, "__annotations__")


def _list_item_type(tp: Any) -> Any | None:
    """The element type of a ``list[X]`` annotation, else ``None``."""
    if typing.get_origin(tp) is list:
        args = typing.get_args(tp)
        return args[0] if args else None
    return None


def contract_keys(td: Any, prefix: str = "") -> set[str]:
    """Every LEAF field of a TypedDict tree, dotted, ``[]`` marking a list element.

    A field whose type is a nested TypedDict, or a list of one, is structure and
    contributes its children rather than itself -- the same distinction the template
    extractor makes, so the two sets are directly comparable.
    """
    out: set[str] = set()
    hints = typing.get_type_hints(td)
    for name, tp in hints.items():
        path = f"{prefix}{name}"
        if _is_typed_dict(tp):
            out |= contract_keys(tp, f"{path}.")
            continue
        item = _list_item_type(tp)
        if item is not None and _is_typed_dict(item):
            out |= contract_keys(item, f"{path}[].")
            continue
        out.add(path)
    return out


# ---------------------------------------------------------------------------
# extract what the template reads
# ---------------------------------------------------------------------------


class ExtractionRefused(Exception):
    """The template does not match what the extractor can read.

    Raised rather than returning a partial set. A partial set is indistinguishable from
    a template that genuinely reads fewer fields, and that is the one direction an
    equality assertion cannot catch on its own.
    """


#: ``re.IGNORECASE`` because HTML tag names are case-insensitive: without it ``<SCRIPT>``
#: is not matched, so a second script spelled in upper case is invisible to the
#: one-script precondition below and its reads never enter the extracted set. That is the
#: shape CodeQL names ``py/bad-tag-filter``, and here it would make the equality gate
#: quietly incomplete rather than loudly broken.
_SCRIPT = re.compile(r"<script>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"(?<![:\w])//[^\n]*")
_ITER = re.compile(
    r"\b([A-Za-z_$][\w$]*)\s*\.\s*(?:forEach|filter)\s*\(\s*function\s*\(\s*([A-Za-z_$][\w$]*)\s*\)\s*\{"
)

_ROOT = "d"
_ALIASES: tuple[tuple[str, str], ...] = (
    ("meta", "meta"),
    ("cols", "columns"),
    ("pg", "progress"),
    ("stats", "stats"),
    ("segs", "progress.segments"),
    ("items", "columns[].cards"),
)
_LIST_ALIASES: frozenset[str] = frozenset({"cols", "stats", "segs", "items"})
_ARRAY_BUILTINS: frozenset[str] = frozenset({"forEach", "filter", "length"})
_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("cols", "columns[]"),
    ("items", "columns[].cards[]"),
    ("segs", "progress.segments[]"),
    ("stats", "stats[]"),
)
#: A variable holding ONE element picked out of a list, rather than a loop over it --
#: ``var settled = segs.filter(...)[0]``. Its properties are that element's, so they
#: flatten to the element path. Pinned BY NAME, and renaming the variable in the
#: template is a loud refusal rather than a quietly shorter field set: a picked element
#: is usually the one a headline is built from, so losing it silently would drop exactly
#: the field a reader checks first.
_PICKED: tuple[tuple[str, str], ...] = (("settled", "progress.segments[]"),)


@dataclass
class Island:
    """What one template reads out of its data island."""

    leaves: set[str] = field(default_factory=set)
    containers: set[str] = field(default_factory=set)


def _template_script(html: str) -> str:
    """The template's one inline script, comments removed.

    Comments go FIRST, before any field is read out. The template documents its own
    field names in prose beside the code that reads them, so a raw scan counts a
    removed field that a comment still mentions -- and a comment is exactly where a
    field can be named without being used.
    """
    bodies = [m.group(1) for m in _SCRIPT.finditer(html) if m.group(1).strip()]
    if len(bodies) != 1:
        raise ExtractionRefused(f"expected one non-empty inline script, found {len(bodies)}")
    return strip_comments(bodies[0])


def strip_comments(src: str) -> str:
    """*src* with block and line comments blanked, newlines kept.

    Newlines survive so brace matching and loop-body slicing still line up with the
    original. The line-comment pattern excludes ``://`` so a URL in a string is not
    mistaken for a comment.
    """
    src = _BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), src)
    return _LINE_COMMENT.sub("", src)


def _matching_brace(src: str, open_at: int) -> int:
    if src[open_at] != "{":
        raise ExtractionRefused(f"expected a brace at offset {open_at}")
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ExtractionRefused("unbalanced braces in the template script")


def _reads(src: str, var: str) -> list[str]:
    """Property names read off *var*, as a LIST so two sites of one name are visible."""
    return re.findall(rf"\b{re.escape(var)}\s*\.\s*([A-Za-z_$][\w$]*)", src)


def island_keys(html: str) -> Island:
    """Every island field *html* reads, dotted, with ``[]`` marking a list element."""
    src = _template_script(html)
    isl = Island()

    if not _reads(src, _ROOT):
        raise ExtractionRefused(f"no reads of the island root '{_ROOT}.' -- renamed?")

    for var, path in _ALIASES:
        if not re.search(rf"\bvar\s+{re.escape(var)}\s*=", src):
            raise ExtractionRefused(f"expected 'var {var} =' opening '{path}' -- renamed?")
        isl.containers.add(path)

    for list_var, elem_path in _ELEMENTS:
        elem_vars = {m.group(2) for m in _ITER.finditer(src) if m.group(1) == list_var}
        bodies: list[str] = []
        for m in _ITER.finditer(src):
            if m.group(1) != list_var:
                continue
            open_at = src.index("{", m.end() - 1)
            bodies.append(src[open_at : _matching_brace(src, open_at)])
        if not bodies:
            raise ExtractionRefused(f"no forEach/filter over '{list_var}' -- restructured?")
        for body in bodies:
            for elem_var in elem_vars:
                for prop in _reads(body, elem_var):
                    isl.leaves.add(f"{elem_path}.{prop}")

    for var, elem_path in _PICKED:
        if not re.search(rf"\bvar\s+{re.escape(var)}\s*=", src):
            raise ExtractionRefused(f"expected 'var {var} =' picking one '{elem_path}'")
        for prop in _reads(src, var):
            isl.leaves.add(f"{elem_path}.{prop}")

    for var, path in _ALIASES:
        props = set(_reads(src, var))
        if var in _LIST_ALIASES:
            stray = props - _ARRAY_BUILTINS
            if stray:
                raise ExtractionRefused(
                    f"'{var}' is the list '{path}'; {sorted(stray)} read off an array"
                )
            continue
        for prop in props:
            isl.leaves.add(f"{path}.{prop}")

    root_containers = {p for _, p in _ALIASES if "." not in p and "[]" not in p}
    for prop in _reads(src, _ROOT):
        if prop not in root_containers:
            isl.leaves.add(prop)

    bare = {path.replace("[]", "") for path in isl.containers}
    isl.leaves = {leaf for leaf in isl.leaves if leaf.replace("[]", "") not in bare}
    return isl


def _read_template() -> set[str]:
    return island_keys(_template_path().read_text(encoding="utf-8")).leaves


# ---------------------------------------------------------------------------
# the extractor's own preconditions
# ---------------------------------------------------------------------------


def test_the_extractor_reads_the_shipped_board_template() -> None:
    """It finds fields at all, in every section, off both a list element and an object.

    A precondition for every assertion below: comparing against an extractor that
    silently returned nothing would read as "the contract is over-specified".
    """
    got = _read_template()
    assert got, "extracted no fields from the shipped board template"
    for expected in ("lede", "meta.revision", "columns[].cards[].id", "stats[].note"):
        assert expected in got, f"{expected} not extracted"


def test_a_field_named_only_in_a_comment_is_not_read() -> None:
    """Attack one. A comment names a field; it must not count as used."""
    html = _template_path().read_text(encoding="utf-8")
    seeded = html.replace(
        "var meta =", "/* k.phantom and d.ghost are mentioned here */ var meta =", 1
    )
    assert seeded != html, "failed to seed the comment"
    leaves = island_keys(seeded).leaves
    assert "columns[].cards[].phantom" not in leaves
    assert "ghost" not in leaves


def test_a_field_both_commented_and_read_still_counts() -> None:
    """Attack two. Stripping comments must not delete a real read of the same name.

    The dangerous version of attack one: blank the comment too eagerly and a field the
    template genuinely reads disappears, which makes the contract look over-specified.
    """
    html = _template_path().read_text(encoding="utf-8")
    seeded = html.replace(
        "put(a, k && k.id, true);",
        "/* k.id is the PR number */ put(a, k && k.id, true);",
        1,
    )
    assert seeded != html, "failed to seed the comment"
    assert "columns[].cards[].id" in island_keys(seeded).leaves


def test_a_second_inline_script_is_refused() -> None:
    html = _template_path().read_text(encoding="utf-8")
    with pytest.raises(ExtractionRefused, match="one non-empty inline script"):
        island_keys(html + "<script>var x = d.smuggled;</script>")


def test_a_second_script_in_upper_case_is_refused_too() -> None:
    """Tag names are case-insensitive, so the scan must be.

    Matched case-sensitively, ``<SCRIPT>`` is not a script at all: the one-script
    precondition counts one body, the extractor happily returns a set that omits
    everything the upper-case block reads, and the equality gate passes while a template
    field goes undeclared. A gate that is quietly incomplete is worse than one that
    fails, which is why this is its own case and not a variant of the test above.
    """
    html = _template_path().read_text(encoding="utf-8")
    with pytest.raises(ExtractionRefused, match="one non-empty inline script"):
        island_keys(html + "<SCRIPT>var x = d.smuggled;</SCRIPT>")


def test_a_renamed_container_variable_is_refused() -> None:
    html = _template_path().read_text(encoding="utf-8")
    with pytest.raises(ExtractionRefused, match="opening 'meta'"):
        island_keys(html.replace("var meta =", "var hdr ="))


def test_the_contract_flattener_walks_the_whole_tree() -> None:
    """Nested TypedDicts and lists of them must contribute children, not themselves."""
    keys = contract_keys(PipelineBoardPanel)
    for expected in (
        "meta.revision",
        "columns[].cards[].id",
        "progress.segments[].n",
        "stats[].k",
    ):
        assert expected in keys, f"{expected} missing from the flattened contract"
    for structure in ("meta", "columns", "progress", "stats", "columns[].cards"):
        assert structure not in keys, f"{structure} is structure, not a field"


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_the_board_template_id_is_reachable_through_crew_selection() -> None:
    """THE recurrence guard: this id must be one ``template_for_crew`` can return.

    Naming the file correctly is not the fix on its own: an id no crew slugifies to is
    selected by nothing, so the conductor gets the generic template while the parity gate
    stays green over a file that is never rendered. Asserting the id through the real
    selection function is what makes a rename fail here instead of silently returning the
    conductor to the generic template.

    Goes through :func:`agent_panel.template_for_crew`, not a string comparison, so it
    also covers the slugify step and the "is it actually installed" check.
    """
    assert agent_panel.template_for_crew(BOARD_CREW_NAME) == BOARD_TEMPLATE_ID
    assert BOARD_TEMPLATE_ID != agent_panel.DEFAULT_TEMPLATE_ID, (
        "the contract must not be bound to the generic template: any crew may publish "
        "anything to it, which is the free-form panel this contract replaces"
    )
    assert _template_path().is_file()


def test_the_template_reads_exactly_the_contract_declares() -> None:
    """THE assertion: the HTML end of the contract, which mypy cannot see.

    STRICT equality, with no exemption list. An exemption list is a hiding place: a field
    inconvenient to render can be declared in the type and named in the list, and every
    check still passes. Every field has a real job on the page instead --
    ``contract_version`` discloses a version mismatch and ``omitted`` states how many
    entries the fold dropped -- so the set is exactly equal and there is nowhere to bury
    anything.

    A field added to the template with no home in :class:`PipelineBoardPanel` is the move
    that reintroduces a panel of publisher strings. A field in the type the template never
    reads is a provider deriving something nobody displays.
    """
    read = _read_template()
    declared = contract_keys(PipelineBoardPanel)
    assert read - declared == set(), f"template reads undeclared fields: {sorted(read - declared)}"
    assert (
        declared - read == set()
    ), f"contract declares fields the template never reads: {sorted(declared - read)}"


def test_an_undeclared_template_field_is_caught() -> None:
    """Direction one, inside the CARD loop -- so the scoped extraction does the work."""
    html = _template_path().read_text(encoding="utf-8")
    bogus = html.replace(
        "put(a, k && k.id, true);",
        "put(a, k && k.id, true); put(a, k && k.bogus, true);",
        1,
    )
    assert bogus != html, "failed to inject the bogus field -- the anchor line moved"
    read = island_keys(bogus).leaves
    assert "columns[].cards[].bogus" in read
    assert read - contract_keys(PipelineBoardPanel) == {"columns[].cards[].bogus"}


def test_a_contract_field_the_template_ignores_is_caught() -> None:
    """Direction two, simulated by removing every read of one field from the template.

    The WHOLE line goes, because ``meta.revision`` is read twice on it -- once to test
    for absence and once to render. Removing only the render half leaves the field read,
    which is the correct answer and is pinned separately below.
    """
    html = _template_path().read_text(encoding="utf-8")
    line = '  if (intOf(meta.revision) !== null) bar.appendChild(el("span", "meta", "rev " + intOf(meta.revision)));\n'
    assert html.count(line) == 1, "the revision line moved"
    read = island_keys(html.replace(line, "", 1)).leaves
    assert contract_keys(PipelineBoardPanel) - read == {"meta.revision"}


def test_removing_one_of_two_read_sites_keeps_the_field_read() -> None:
    """Attack three. A field read in two places must survive losing one.

    Set semantics make two sites look like one, so the risk runs the other way: a gate
    that concluded "unread" from one site disappearing would report a live field as
    contract-only and invite someone to delete it from the type.
    """
    html = _template_path().read_text(encoding="utf-8")
    half = html.replace(
        'bar.appendChild(el("span", "meta", "rev " + intOf(meta.revision)));', "", 1
    )
    assert half != html, "failed to remove one read site"
    assert "meta.revision" in island_keys(half).leaves


def test_the_contract_version_is_a_positive_integer() -> None:
    """One per contract type, so a reader can branch on the shape it was handed."""
    assert isinstance(CONTRACT_VERSION, int) and CONTRACT_VERSION >= 1


def test_the_template_reads_the_version_the_contract_writes() -> None:
    """The template's own ``READS_CONTRACT`` must equal :data:`CONTRACT_VERSION`.

    Otherwise the disclosure inverts: the shipped template would declare a mismatch on
    every board the shipped provider publishes, and a real mismatch -- an operator
    override serving an older view -- would be indistinguishable from that noise. Bumping
    the contract means bumping this constant in the same change.
    """
    src = _template_path().read_text(encoding="utf-8")
    found = re.findall(r"\bvar\s+READS_CONTRACT\s*=\s*(\d+)\s*;", src)
    assert len(found) == 1, f"expected one READS_CONTRACT in the template, found {found}"
    assert int(found[0]) == CONTRACT_VERSION


# ---------------------------------------------------------------------------
# the VALUES a field may take, which the equality gate above cannot see
# ---------------------------------------------------------------------------
#
# The gate above proves ``progress.segments[].name`` exists on both sides. It cannot
# prove the template understands the strings that arrive in it, and that is not a
# hypothetical gap: the template shipped a tone map keyed on four invented words while
# the provider emitted the four closed item states, so every band fell to the default
# tone and the headline read "0 of 6 done" on a board with two settled items. Nothing
# was red. These two cases close that class by pinning the template's own vocabulary
# against the one the fold is allowed to produce.


def test_the_templates_segment_tones_are_the_closed_item_states() -> None:
    """The tone map's keys are exactly ``WORK_ITEM_STATES``.

    A key the provider never emits is dead, and a state the map omits renders in the
    default tone -- so both directions are wrong and only equality is right.
    """
    src = _template_path().read_text(encoding="utf-8")
    found = re.findall(r"\bvar\s+NAMES\s*=\s*\{([^}]*)\}\s*;", src)
    assert len(found) == 1, f"expected one NAMES map in the template, found {len(found)}"
    keys = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", found[0]))
    assert keys, "parsed no keys out of the NAMES map -- the probe is broken, not the map"
    assert keys == set(WORK_ITEM_STATES), (
        f"template segment tones {sorted(keys)} against the closed item states "
        f"{sorted(WORK_ITEM_STATES)}"
    )


def test_the_templates_settled_state_is_one_the_fold_can_emit() -> None:
    """The headline counts a state that actually arrives.

    ``SETTLED`` picks the segment the "N of TOTAL" line is built from. Pointed at a
    string no board ever carries, that line reads zero forever -- the most-read number
    on the panel, wrong in the direction that looks like bad news rather than a bug.
    """
    src = _template_path().read_text(encoding="utf-8")
    found = re.findall(r'\bvar\s+SETTLED\s*=\s*"([^"]+)"\s*;', src)
    assert len(found) == 1, f"expected one SETTLED in the template, found {found}"
    assert (
        found[0] in WORK_ITEM_STATES
    ), f"the headline counts {found[0]!r}, which is not one of {sorted(WORK_ITEM_STATES)}"


def test_no_segment_field_is_coerced_with_a_raw_string_call() -> None:
    """Segment values are normalised once, never coerced where they are read.

    ``String(x)`` throws a ``TypeError`` on an object whose ``toString`` and ``valueOf``
    are not callable, and that aborts the whole script -- leaving a half-drawn panel,
    the one state a status board must not reach because a reader cannot tell it from an
    empty one. Such a value is reachable: a record stored in the free shape before this
    contract existed passes through the reader untouched whenever its crew has no work
    fold. The template therefore maps each segment through ``textOf``/``intOf`` once and
    reads plain properties afterwards.

    Textual, and deliberately so: the hazard IS the spelling ``String(s.`` at a read
    site, and no Python test can execute this template's JavaScript. The control below
    keeps an empty result from reading as a pass.
    """
    src = _template_path().read_text(encoding="utf-8")
    offenders = re.findall(r"String\(\s*s\s*\.", src)
    assert not offenders, f"{len(offenders)} raw coercion(s) of a segment field remain"
    # Control: the normalising map must be there, otherwise "no offenders" is satisfied
    # by a template that stopped reading segments at all.
    assert re.search(r"\.map\(function\(s\)\{\s*return\s*\{\s*name:\s*textOf\(s\.name\)", src), (
        "the segment-normalising map is gone; this check would pass over a template "
        "that no longer reads segments"
    )


def test_an_empty_board_reads_as_empty_not_as_undrawable_progress() -> None:
    """A conductor with a goal and no items folds a well-formed progress block -- total
    0, every band 0 -- which draws no track. That is an empty board, and the template
    routes ``total === 0`` to an empty reading BEFORE the arm that reports progress that
    cannot be drawn. Without that arm the empty board falls through to the alarm and
    accuses a board that is simply empty and correct.

    Textual, like the vocabulary pins above: no Python test executes this template's
    JavaScript. The control below keeps a moved alarm string from reading as a pass.
    """
    src = _template_path().read_text(encoding="utf-8")
    empty_arm = re.search(r"else if\s*\(\s*total === 0\s*\)", src)
    assert empty_arm, "no 'else if (total === 0)' arm: an empty board is not distinguished"
    assert "No items yet." in src, "the empty-board arm renders no empty reading"
    alarm = "Progress published, but it cannot be drawn."
    assert (
        src.count(alarm) == 1
    ), f"expected one undrawable-progress alarm, found {src.count(alarm)}"
    assert empty_arm.start() < src.index(alarm), (
        "the 'total === 0' arm must come before the undrawable-progress alarm, so an "
        "empty board is caught before it"
    )


def test_the_undrawable_progress_alarm_is_kept_for_the_malformed_case() -> None:
    """Control for the pin above: the malformed case keeps its own words.

    The fix distinguishes an empty board from progress that cannot be drawn; it does not
    delete the undrawable arm. This holds the alarm string and its ``held(d.progress)``
    guard in place, so the pin above cannot be satisfied by removing the alarm instead of
    adding the empty arm.
    """
    src = _template_path().read_text(encoding="utf-8")
    assert "Progress published, but it cannot be drawn." in src
    assert re.search(
        r"else if\s*\(\s*held\(d\.progress\)\s*\)", src
    ), "the undrawable-progress arm must stay guarded by held(d.progress)"
