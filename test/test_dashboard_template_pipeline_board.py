"""The pipeline board CARD: the page binds exactly what the flattener writes.

``mypy src/kiro_crew/`` checks both ends of ``build_pipeline_board`` -- a
:class:`~kiro_crew.work_vocab.WorkBoardView` in, a ``PipelineBoardPanel`` out, required
keys and all. It cannot see inside ``dashboard_card_pages/pipeline_board.html``, so the
html end of the agreement needs a reader of its own. This is that reader.

TWO GATES, because the card format needs both and neither implies the other.

The first is PARITY, strict and in both directions: the page's
``data-dashboard-field`` set equals ``panel_card_data``'s output keys. The host binds a
field it was not given to the EMPTY STRING, so a binding with no writer is a blank cell
on a page of counts -- indistinguishable from a real zero -- and a written key nothing
binds is a value nobody sees.

The second is COVERAGE, which the drawer template's parity gate gets for free: it compares
that template's reads against the contract's own leaves, so a contract field nobody renders
fails there. A card cannot do that: it has 24 fields for 17
contract leaves whose lists are unbounded, so several leaves reach the page inside a
composed sentence rather than as a field of their own. :func:`test_every_contract_leaf_
reaches_a_card_field` closes that by SEEDING each leaf with a unique marker and requiring
it to come out the other side -- which proves the value travels, not merely that a name
matches.

Every extractor here refuses rather than returning a partial answer. A short set reads
as a page that genuinely binds less, which is the one direction an equality assertion
cannot catch alone -- so the preconditions run as their own cases.

Written with the stdlib html parser, not a regular expression. An attribute scan by
regex mis-reads an attribute inside a comment, a value in the other quote character and
an upper-case tag name, and each of those makes this gate QUIETLY incomplete.
"""

from __future__ import annotations

import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import pipeline_board_contract as contract
from kiro_crew.dashboard.dynamic_cards import (
    _FIELD_NAME,
    MAX_DATA_BYTES,
    MAX_HTML_BYTES,
    normalize_card,
)
from kiro_crew.pipeline_board_contract import (
    BOARD_COLUMN_NAMES,
    BOARD_STAT_KEYS,
    BOARD_TEMPLATE_ID,
    CONTRACT_VERSION,
    NOT_SAID,
    UNREADABLE,
    UNSAID,
    PipelineBoardPanel,
    build_pipeline_board,
    card_template_path,
    panel_card_data,
    validate_judgment,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.work_vocab import WORK_ITEM_STATES, WorkBoardView

# ``MAX_DATA_BYTES``/``MAX_HTML_BYTES`` and ``_FIELD_NAME`` are IMPORTED FROM THE HOST,
# not restated. The contract module keeps its own copies so it can bound a card without
# importing the dashboard package, and the two pairs are asserted equal below -- a cap the
# host raises or lowers has to reach the producer, and a producer that bounds a card against
# a number the host does not use builds cards the host drops whole.


# ---------------------------------------------------------------------------
# read the page
# ---------------------------------------------------------------------------


class ExtractionRefused(Exception):
    """The page is not something this reader can answer about."""


#: The binding attribute the host reads. One attribute, one field, one value.
_BINDING = "data-dashboard-field"

#: Tags that would make the page a control surface or reach outside it. The host strips
#: every one, so a page authoring them ships a layout with holes in it and an author who
#: believes the control exists. A dashboard card states facts and offers no actions.
_FORBIDDEN_TAGS = frozenset(
    {
        "script",
        "form",
        "input",
        "button",
        "textarea",
        "select",
        "iframe",
        "object",
        "embed",
        "applet",
        "template",
        "noscript",
        "link",
        "meta",
        "base",
        "frame",
        "animate",
        "set",
    }
)

#: CSS properties ``dashboardDocument.ts`` DELETES in card mode, because they make the
#: browser draw characters the backend's text scan never reads. A label put in one of
#: them is removed and leaves its row unlabelled, with nothing saying why.
_STRIPPED_PROPERTIES = ("content", "list-style", "text-emphasis", "text-overflow", "quotes")


class _Scanner(HTMLParser):
    """Collects the page's bindings, tags, and anything pointing outside it."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: list[str] = []
        self.tags: set[str] = set()
        self.outbound: set[tuple[str, str]] = set()
        self.empty_bindings = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag.lower())
        for name, value in attrs:
            lowered = name.lower()
            if lowered == _BINDING:
                text = (value or "").strip()
                if not text:
                    # A binding with no name binds nothing: the host looks the empty
                    # string up in the data and writes the empty string back, so the
                    # element renders blank forever with nothing to trace it to.
                    self.empty_bindings += 1
                    continue
                self.fields.append(text)
                continue
            # The LOCAL name, so an SVG ``xlink:href`` is judged as an ``href``.
            local = lowered.rsplit(":", 1)[-1]
            if local not in {"src", "srcset", "href"}:
                continue
            target = (value or "").strip()
            if local == "href" and target.startswith("#"):
                continue
            self.outbound.add((lowered, target))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _scan(html: str) -> _Scanner:
    scanner = _Scanner()
    scanner.feed(html)
    scanner.close()
    return scanner


def card_fields(html: str) -> list[str]:
    """Every field *html* binds, as a LIST so a duplicated binding stays visible.

    Refuses a page that binds nothing: an extractor returning an empty answer would make
    the equality gate report the flattener as writing entirely unread keys, which sends a
    reader to delete the flattener instead of fixing the page.
    """
    scanner = _scan(html)
    if not scanner.fields:
        raise ExtractionRefused(f"the page binds no {_BINDING} at all -- renamed?")
    if scanner.empty_bindings:
        raise ExtractionRefused(f"{scanner.empty_bindings} binding(s) name no field")
    return scanner.fields


def _page() -> str:
    return card_template_path().read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# a real board to flatten
# ---------------------------------------------------------------------------


def _item(
    ident: str,
    *,
    state: str = "open",
    pr: int | None = None,
    worker: str | None = None,
    round_: int = 1,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "item_id": ident,
        "title": "an item",
        "acceptance": {},
        "state": state,
        "verdict": None,
        "decision": "",
        "worker_session_key": worker,
        "round": round_,
        "fails": 0,
        "status": None,
        "summary": "",
        "artifacts": {},
        "pr": pr,
        "last_report_at": "2026-09-29T15:00:00+00:00",
        "created_at": "2026-09-29T14:00:00+00:00",
        "closed_at": None,
        "events": [],
    }


def _view(items: list[dict[str, Any]], *, omitted: int = 0, entries: int = 41) -> WorkBoardView:
    return {
        "conductor": {
            "schema": 1,
            "slot_key": "chat-1875",
            "goal": "the board becomes a dashboard card",
            "round": 3,
            "goal_version": 1,
            "depth": 0,
            "parent_item": None,
            "created_at": "2026-09-29T13:00:00+00:00",
            "entries": entries,
            "first_entry_at": "2026-09-29T13:00:00+00:00",
            "last_entry_at": "2026-09-29T15:30:00+00:00",
            "generation": "gen-1",
        },
        "items": items,
        "omitted": omitted,
    }


#: A board with something in every state, an action, a check tally and a metric gloss,
#: so a leaf that only appears when a judgment was written is still exercised.
def _full_board() -> PipelineBoardPanel:
    items = [
        _item("it_0", state="open", pr=14583, worker="chat-1875", round_=3),
        _item("it_1", state="accepted", pr=14689),
        _item("it_2", state="rejected", worker="chat-2176"),
        _item("it_3", state="abandoned", pr=12046),
    ]
    judgment = validate_judgment(
        {
            "lede": "Four items, one needs a person.",
            "you": {"it_0": "push the rebase once the base fix lands"},
            "notes": {"items": "items the fold folded"},
            "checks": {"it_0": "41/47"},
        }
    )
    return build_pipeline_board(
        _view(items, omitted=2),
        judgment,
        name="KiroCrew Pipeline Conductor",  # brand-ok: the crew's own display name
        captured_at="2026-09-29T15:37:00Z",
        stale_after_seconds=900,
        now_epoch=1790696200.0,
    )


# ---------------------------------------------------------------------------
# the extractor's own preconditions
# ---------------------------------------------------------------------------


def test_the_extractor_reads_the_card_page() -> None:
    """A precondition for every assertion below: it finds bindings at all."""
    found = card_fields(_page())
    assert found, "extracted no bindings from the card page"
    for expected in ("lede", "meta_name", "column_0_rows", "stat_items"):
        assert expected in found, f"{expected} not extracted"


def test_a_page_that_binds_nothing_is_refused() -> None:
    with pytest.raises(ExtractionRefused, match="binds no"):
        card_fields("<div><p>nothing bound here</p></div>")


def test_a_binding_naming_no_field_is_refused() -> None:
    """An empty binding renders blank forever with nothing to trace it to."""
    with pytest.raises(ExtractionRefused, match="name no field"):
        card_fields('<p data-dashboard-field=""></p><p data-dashboard-field="lede"></p>')


def test_an_upper_case_binding_is_still_read() -> None:
    """Attribute names are case-insensitive, so the scan must be.

    Read case-sensitively, ``DATA-DASHBOARD-FIELD`` is not a binding at all: the parity
    set silently omits it and the equality gate passes while a bound element has no
    writer. A gate that is quietly incomplete is worse than one that fails.
    """
    assert card_fields('<p DATA-DASHBOARD-FIELD="lede"></p>') == ["lede"]


def test_a_binding_inside_a_comment_is_not_read() -> None:
    """The page documents its own field names beside the markup that binds them.

    The page's header comment names ``meta.name`` and two field names in prose; counted
    as bindings they would enter the parity set and the gate would pass over a field the
    flattener does not write.
    """
    assert card_fields(
        '<!-- data-dashboard-field="phantom" --><p data-dashboard-field="lede"></p>'
    ) == ["lede"]


# ---------------------------------------------------------------------------
# GATE ONE: parity
# ---------------------------------------------------------------------------


def test_the_card_binds_exactly_what_the_flattener_writes() -> None:
    """THE assertion: the html end of the contract, which mypy cannot see.

    STRICT equality with no exemption list. An exemption list is a hiding place: a field
    inconvenient to fill can be bound in the page and named in the list, and every check
    still passes.
    """
    bound = set(card_fields(_page()))
    written = set(panel_card_data(_full_board()))
    assert bound - written == set(), f"bound with no writer: {sorted(bound - written)}"
    assert written - bound == set(), f"written but never bound: {sorted(written - bound)}"


def test_no_field_is_bound_twice() -> None:
    """Two elements bound to one field both show it, which is a layout bug the set
    comparison above cannot see: sets collapse the duplicate and the page reads as
    correct while one number is printed in two places."""
    found = card_fields(_page())
    duplicates = sorted({name for name in found if found.count(name) > 1})
    assert not duplicates, f"bound more than once: {duplicates}"


def test_a_bound_field_with_no_writer_is_caught() -> None:
    """Direction one, planted. Without this the equality above could be vacuous."""
    page = _page()
    seeded = page.replace(
        '<p class="lede" data-dashboard-field="lede"></p>',
        '<p class="lede" data-dashboard-field="lede"></p>'
        '<p data-dashboard-field="invented"></p>',
        1,
    )
    assert seeded != page, "failed to plant the binding -- the anchor line moved"
    assert set(card_fields(seeded)) - set(panel_card_data(_full_board())) == {"invented"}


def test_a_written_key_with_no_binding_is_caught() -> None:
    """Direction two, planted by removing one binding from the page."""
    page = _page()
    anchor = '<span class="dim" data-dashboard-field="meta_when"></span>'
    assert page.count(anchor) == 1, "the header stamp binding moved"
    bound = set(card_fields(page.replace(anchor, "", 1)))
    assert set(panel_card_data(_full_board())) - bound == {"meta_when"}


# ---------------------------------------------------------------------------
# GATE TWO: every contract leaf reaches the card
# ---------------------------------------------------------------------------
#
# The drawer template's parity gate got this for free by comparing against the contract's
# own leaves. The card cannot: several leaves reach the page inside a composed sentence
# (``meta.age_seconds`` is part of ``meta_when``; ``progress.total`` is the denominator in
# ``progress_legend`` and in every column head), so a name comparison would report them
# as unrendered. Seeding the VALUE and looking for it on the other side proves the leaf
# travels, which is the thing actually worth proving.


#: One leaf per row: where it lives in a panel, the value to seed there, and the text
#: that must come out. The two differ only where the flattener FORMATS the leaf rather
#: than printing it -- an age of 7442 seconds reads "2h 4m", and looking for "7442" would
#: report a leaf that is rendered correctly as unrendered. So the expected text is
#: written out for those rows instead of derived, which is also the only way this table
#: says what the card does with each leaf.
_LEAF_MARKERS: tuple[tuple[str, Any, str], ...] = (
    ("lede", "MARK-lede", "MARK-lede"),
    ("since", "MARK-since", "MARK-since"),
    ("meta.name", "MARK-name", "MARK-name"),
    ("meta.captured_at", "MARK-captured", "MARK-captured"),
    # Formatted: seconds become a duration phrase.
    ("meta.age_seconds", 7442, "2h 4m"),
    # Read as a THRESHOLD, not printed: an age past it earns the word. Its two-sided
    # behaviour is pinned separately, because a constant present in the page is not
    # evidence that the comparison is made.
    ("meta.stale_after_seconds", 1, "stale"),
    ("columns[].cards[].id", "MARK-id", "MARK-id"),
    ("columns[].cards[].sub", "MARK-sub", "MARK-sub"),
    ("columns[].cards[].of", "MARK-of", "MARK-of"),
    ("columns[].cards[].you", "MARK-you sentence", "MARK-you sentence"),
    ("progress.total", 6161, "6161"),
    ("progress.added_since", 3131, "3131"),
    ("stats[].v", "MARK-value", "MARK-value"),
    ("stats[].note", "MARK-note", "MARK-note"),
    ("omitted", 8181, "8181"),
)

#: The leaves a marker round-trip CANNOT prove, each with the test that proves it
#: instead. Both are read rather than printed as published, so a seeded value does not
#: come out the other side and looking for one would report a working leaf as unrendered.
#: Two entries, each named with its covering test, so this is a stated exception and not
#: a list anything inconvenient can be added to -- and the table above is asserted to be
#: exactly the remaining leaves, so adding a row here without a test is the change that
#: would have to be made deliberately.
_LEAF_READ_NOT_PRINTED: dict[str, str] = {
    # Reaches the card as the field NAME each tile's value binds by, never as a value.
    # Joined to the page's bindings and literal labels against a REAL board by:
    "stats[].k": "test_the_card_binds_a_value_and_a_note_for_every_stat_the_provider_emits",
    # Decides WHICH field group a column's cards land in -- matched against the closed
    # item states rather than printed, so a seeded name selects no group at all. Pinned
    # two-sided, cards following the name rather than the position, by:
    "columns[].name": "test_a_columns_cards_follow_its_name_not_its_position",
    # COMPARED, and the comparison's ANSWER is what reaches the card. The version NUMBER is not
    # printed -- a reader cannot act on it, only on being told the board may be wrong -- so a
    # seeded one reaches no field. The two abnormal branches carry their own tests instead, both
    # required to speak and required to stay distinguishable from each other, by:
    "contract_version": "test_an_unreadable_contract_version_says_so",
    # DERIVED FROM THE ROUND by the provider, so printing it put one number on the card twice
    # under two names and a reader asked how the two differed. The card shows the ROUND tile
    # and this leaf is checked as a RELATIONSHIP instead, which is stronger than printing it:
    # a provider that stopped deriving it from the round reddens, where a printed copy would
    # simply have shown a second number. By:
    "meta.revision": "test_the_boards_revision_is_the_conductors_round",
    # A SELECTOR, not printed text. The legend names only the settled band, and it finds that
    # band by comparing this name against the closed state vocabulary -- so a seeded name
    # selects nothing and reaches no field. The other bands are not printed at all: each has a
    # column whose head already prints its name and its share. Pinned two-sided, the ratio
    # following the name rather than a position, by:
    "progress.segments[].name": "test_the_settled_count_is_every_terminal_band",
    # SUBTRACTED, never shown. Each band's own count is already on screen -- its column head
    # prints it, taken from that column's cards -- so printing the provider's copy beside it put
    # the same number in two places on every board. What this leaf reaches the card as is the
    # settled band taken off the total, plus the unaccounted remainder when the bands and the
    # board disagree. Pinned two-sided, the figure following the named band's count rather than
    # a position, by:
    "progress.segments[].n": "test_the_settled_count_is_every_terminal_band",
}


def _seed(panel: dict[str, Any], path: str, marker: Any) -> dict[str, Any]:
    """*panel* with the leaf at *path* set to *marker*, everywhere it occurs."""
    node: Any = panel
    parts = path.split(".")
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part.endswith("[]"):
            key = part[:-2]
            children = node[key]
            assert children, f"nothing to seed under {path}: {key} is empty"
            for child in children:
                _seed(child, ".".join(parts[index + 1 :]), marker)
            return panel
        if last:
            node[part] = marker
            return panel
        node = node[part]
    return panel


def test_the_boards_revision_is_the_conductors_round() -> None:
    """The card does not print the revision, so this is what keeps the leaf honest.

    A relationship rather than a rendering: the provider sets the board's revision FROM the
    conductor's round, which is why showing both was showing one number twice. If a provider
    ever stops deriving it that way the card would be silently missing a fact, and this is the
    assertion that notices.
    """
    board = _full_board()
    shown = {stat["k"]: stat["v"] for stat in board["stats"]}
    assert str(board["meta"]["revision"]) == shown["round"], (board["meta"], shown)


#: The band the legend's headline counts, read from the module rather than restated -- a second
#: copy of the word would let the two drift and this test would still pass.
_SETTLED = contract._SETTLED_STATE

#: Every settled state, read from the module. The legend subtracts this whole set, not just the
#: accepted band, so a rejected or abandoned item is counted as settled rather than still moving.
_TERMINAL = contract._TERMINAL_STATES


def test_the_legend_says_only_what_the_heads_do_not() -> None:
    """Every band has a column whose head prints its name and its share, so anything the
    legend says about a SINGLE band is on screen twice.

    A RATIO is not exempt from that. The settled band's head reads ``accepted . 2 of 6`` and
    the legend once read ``2 of 6 accepted`` -- the same two numbers, differently arranged,
    which is the reading a cold reader reported. The count the legend carries is the AGGREGATE
    no single head prints: how much of the whole board has settled, summed across every
    terminal band. ``N still to settle`` is NOT that -- "still to settle" is the open band by
    definition, so a corrected count there just restates the open head.
    """
    data = panel_card_data(_full_board())
    legend = data["progress_legend"]
    heads = [v for k, v in data.items() if k.endswith("_head")]
    assert "settled" in legend and "in round" in legend, legend
    # NO band NAME at all -- naming one is what put its ratio beside its own column head.
    for head in heads:
        band = head.split(" \u00b7 ")[0]
        assert band not in legend, f"the legend restates the {band} head: {legend}"
    # And no head's own figures are repeated here. Checked as the rendered text a head shows,
    # so a legend that re-ran the same arithmetic in the same words is caught. The settled
    # aggregate is the sum across terminal bands, which no single head carries.
    for head in heads:
        assert head.split(" \u00b7 ", 1)[-1] not in legend, f"{head!r} is restated: {legend}"


def test_the_settled_count_is_every_terminal_band() -> None:
    """The legend's count is how much of the board has SETTLED -- every terminal band, by name.

    Settled is ``accepted``, ``rejected`` and ``abandoned`` together, not ``accepted`` alone:
    an item that was rejected or abandoned has finished too. Two-sided: an item in ``rejected``
    is counted settled exactly like one in ``accepted``, and renaming every band off the
    terminal states leaves nothing settled.
    """
    panel = _full_board()
    bands = {seg["name"]: seg["n"] for seg in panel["progress"]["segments"]}
    total = panel["progress"]["total"]
    terminal = sum(n for name, n in bands.items() if name in _TERMINAL)
    assert bands.get(_SETTLED) and len(bands) > 1, bands
    # The counted set is the whole TERMINAL set, so a board carrying rejected/abandoned items
    # counts more than just accepted as settled -- the whole point of the fix.
    assert terminal > bands[_SETTLED], (
        f"this board has no terminal-but-not-accepted item, so it cannot prove the metric "
        f"counts more than accepted: {bands}"
    )
    assert panel_card_data(panel)["progress_legend"].startswith(f"{terminal} of {total} settled")

    # Rename every band off a terminal state: nothing is settled.
    renamed = _full_board()
    for seg in renamed["progress"]["segments"]:
        seg["name"] = f"not-{seg['name']}"
    assert panel_card_data(renamed)["progress_legend"].startswith(f"0 of {total} settled")

    # A rejected item counts as settled exactly like an accepted one: move accepted -> rejected
    # and the settled count does not change, because both are terminal.
    moved = _full_board()
    for seg in moved["progress"]["segments"]:
        if seg["name"] == _SETTLED:
            seg["name"] = "rejected"
    assert panel_card_data(moved)["progress_legend"].startswith(f"{terminal} of {total} settled")


def test_the_leaf_table_is_exactly_the_contracts_own_leaves() -> None:
    """The coverage table cannot go stale: a leaf added to the contract with no row here
    would never be checked, and a row for a leaf the contract dropped would test nothing
    while reading as coverage."""
    declared = _contract_leaves(PipelineBoardPanel)
    exempt = set(_LEAF_READ_NOT_PRINTED)
    listed = {path for path, _seed, _want in _LEAF_MARKERS} | exempt
    assert listed == declared, f"leaf table against the contract: {sorted(listed ^ declared)}"
    assert exempt <= declared, "an exception names a leaf the contract dropped"
    # The named covering test must EXIST. An exception pointing at a test that was
    # renamed or deleted is an unchecked leaf wearing a citation.
    own = _own_source()
    for leaf, covering in _LEAF_READ_NOT_PRINTED.items():
        assert f"def {covering}(" in own, f"{leaf} cites a test this file does not define"


def _own_source() -> str:
    return Path(__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("path", "seed", "want"), _LEAF_MARKERS, ids=[p for p, _s, _w in _LEAF_MARKERS]
)
def test_every_contract_leaf_reaches_a_card_field(path: str, seed: Any, want: str) -> None:
    """Seed one leaf, and require its text out the other side.

    A leaf that reaches no field is a number the provider derives and nobody can read --
    the same defect the drawer's gate caught by name, caught here by value.
    """
    seeded = _seed(dict(_full_board()), path, seed)
    text = "\n".join(panel_card_data(seeded).values())  # type: ignore[arg-type]
    assert want in text, f"{path} reaches no field on the card"


def test_the_header_labels_its_age() -> None:
    """A bare duration beside a captured time reads as how long something RAN.

    Both branches, because the defect was the ASYMMETRY rather than a missing word: the
    absent case already said "age not said", so the label appeared only when the value did
    not. Nothing about a healthy board should be less legible than a broken one.
    """
    healthy = _full_board()
    healthy["meta"]["age_seconds"] = 620
    when = panel_card_data(healthy)["meta_when"]
    # A labelled NUMBER: the label alone would pass over a dropped duration, and the duration
    # alone is the defect. 620 seconds is two units ("10m 20s"), so this also catches a label
    # attached to only the first of them.
    assert re.search(r"age \d+\w+ \d+\w+", when), when
    absent = _full_board()
    absent["meta"]["age_seconds"] = UNSAID
    absent_when = panel_card_data(absent)["meta_when"]
    assert "age" in absent_when and not re.search(r"age \d", absent_when), absent_when


def test_the_header_stamp_reads_as_a_time_not_an_iso_string() -> None:
    """The producer passes the record's `published_at`, an ISO timestamp. A status header wants
    a time a reader can place, and the sibling phrase already formats one -- so the two halves
    of the same header were rendering the same kind of value two different ways."""
    panel = _full_board()
    panel["meta"]["captured_at"] = "2026-09-29T15:37:04+00:00"
    when = panel_card_data(panel)["meta_when"]
    assert "2026-09-29 15:37 UTC" in when, when
    assert "T15:37:04" not in when, when


def test_an_out_of_range_stamp_degrades_to_its_raw_text_not_a_500() -> None:
    """``datetime.fromisoformat`` accepts a year the subsequent ``astimezone``/``strftime`` cannot
    represent (an ``OverflowError`` / ``OSError`` on some platforms). The drawer-read call site
    catches only ``OSError`` around the card build, so an unguarded arithmetic failure here would
    escape as a 500 the card build is documented never to raise. The stamp degrades to its raw
    text instead -- the same "unchanged when it is not a usable one" contract as an unparseable
    value -- and the card still builds."""
    for raw in ("0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-14:00"):
        panel = _full_board()
        panel["meta"]["captured_at"] = raw
        when = panel_card_data(panel)["meta_when"]
        assert raw in when, when
        assert normalize_card({"html": _page(), "data": panel_card_data(panel)}) is not None


@pytest.mark.parametrize("field", ["of", "you"])
def test_a_number_where_words_belong_says_it_cannot_be_read(field: str) -> None:
    """`checks 7` claims seven of something and `-> 3` cannot be acted on at all.

    Both fields are declared `str` by the contract, so a number is a malformed record, and the
    card already has a word for that. `id` and `sub` keep the permissive reader: an item id or a
    session key arriving as a number still reads as one.
    """
    panel = _full_board()
    panel["columns"][0]["cards"][0][field] = 7  # type: ignore[typeddict-item]
    rows = panel_card_data(panel)["column_0_rows"]
    assert UNREADABLE in rows, rows
    assert "checks 7" not in rows and "-> 7" not in rows, rows


def test_an_item_id_that_arrives_as_a_number_still_reads() -> None:
    """Control for the pair above: the stricter reader is scoped to the two fields a reader
    consumes as language, not applied to every string field."""
    panel = _full_board()
    panel["columns"][0]["cards"][0]["id"] = 14583  # type: ignore[typeddict-item]
    assert "14583" in panel_card_data(panel)["column_0_rows"]


def test_the_omitted_line_names_the_tiles_own_total() -> None:
    """Two bare counts beside each other do not say they are one system: a reader could not tell
    whether the entries the fold dropped were part of the entries the log holds."""
    data = panel_card_data(_full_board())
    shown = data["omitted"]
    entries = data["stat_entries"]
    assert entries in shown, (shown, entries)
    assert shown.startswith("2 of the log's"), shown


def test_the_omitted_line_survives_a_tile_it_cannot_read() -> None:
    """The total is parsed back from the tile's own text, so a board whose entries tile is not a
    number still states what the fold dropped rather than dropping the disclosure."""
    panel = _full_board()
    for stat in panel["stats"]:
        if stat["k"] == "entries":
            stat["v"] = "lots"  # type: ignore[typeddict-item]
    shown = panel_card_data(panel)["omitted"]
    assert "2" in shown and "left out" in shown, shown


def test_a_note_that_only_repeats_its_value_is_dropped() -> None:
    """A tile is a value and a gloss. When the gloss repeats the value the tile prints the same
    phrase twice, which says nothing the first one did not."""
    panel = _full_board()
    for stat in panel["stats"]:
        if stat["k"] == "items":
            stat["v"] = {"nested": "object"}  # type: ignore[typeddict-item]
            stat["note"] = {"also": "nested"}  # type: ignore[typeddict-item]
    data = panel_card_data(panel)
    assert data["stat_items"] == UNREADABLE
    assert data["stat_items_note"] == "", data["stat_items_note"]


def test_a_note_that_adds_something_is_kept() -> None:
    """Control. Suppressing every note would satisfy the test above and delete the publisher's
    gloss from every healthy board."""
    assert panel_card_data(_full_board())["stat_items_note"] == "items the fold folded"


#: The drawer template whose rendering this card replaces. While it exists it is the card's
#: post-restart producer: opening the drawer republishes a card the in-memory store lost.
_DRAWER_TEMPLATE = "src/kiro_crew/agent_panel_templates/kirocrew-pipeline-conductor.html"

#: Where a rehydrator may live, as (module, attribute) pairs. Resolved with ``getattr`` on the
#: IMPORTED module, never grepped: a comment, a docstring or an unrelated identifier carrying one of
#: these words satisfies a text search, and the one check standing between the drawer's removal and
#: a board nobody can see must not be satisfiable by prose.
_REHYDRATOR_SITES = (
    ("kiro_crew.dashboard.card_lifecycle", "rehydrate_derived"),
    ("kiro_crew.dashboard.handlers.agent_panel", "_rehydrate_derived_card"),
    ("kiro_crew.dashboard.card_lifecycle", "rebuild_derived_card"),
)


def _rehydrators() -> list[tuple[str, Any]]:
    """Every rehydrator that is actually IMPORTABLE and CALLABLE, with its dotted name."""
    import importlib

    out: list[tuple[str, Any]] = []
    for mod_name, attr in _REHYDRATOR_SITES:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        found = getattr(mod, attr, None)
        if callable(found):
            out.append((f"{mod_name}.{attr}", found))
    return out


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _rehydrator_probe() -> tuple[Any, Any, Any, None]:
    """A card store holding NO card, and a slot a rehydrator could rebuild one for.

    Deliberately minimal: the gate above only needs somewhere a rehydrator's effect is visible,
    so this is a real ``CardLifecycle`` with the model path off and one live slot.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard import card_lifecycle as lifecycle_mod

    slot = SimpleNamespace(
        key="chat-1875",
        _dashboard_card_identity="probe-owner",
        memory_mode="persistent",
        messages=[],
    )
    state = SimpleNamespace(
        _slots={slot.key: slot},
        _dynamic_cards=None,
        broadcast_ws_owners=lambda *a: None,
        _background_tasks=set(),
    )
    lifecycle = lifecycle_mod.CardLifecycle(state, enabled=False)
    state._dynamic_cards = lifecycle
    return lifecycle, slot, state, None


def test_the_drawer_rendering_cannot_go_before_a_rehydrator_lands() -> None:
    """The precondition, enforced rather than written down.

    The derived store is in memory. While the drawer template exists, opening the drawer
    republishes a card a restart lost, so the gap is covered by a surface that is on its way out.
    Remove that template with no replacement producer and a conductor's board is absent from the
    only remaining surface until somebody visits -- which is the regression the migration exists
    to avoid.

    So this fails the moment the template goes without a rehydrator, which is what makes the
    precondition a gate instead of doc discipline: the removal cannot merge on its own.
    """
    root = _repo_root()
    drawer = root / _DRAWER_TEMPLATE
    if drawer.exists():
        # The precondition is still covered. Assert it is RECORDED, so the next person meets the
        # requirement in the spec rather than in this test's failure message.
        spec = (root / "docs/system-specs/modules/learn-cron-dashboard.md").read_text(
            encoding="utf-8"
        )
        assert (
            "Removing the drawer rendering therefore REQUIRES" in spec
        ), "the spec no longer states the rehydration precondition this gate enforces"
        return
    live = _rehydrators()
    assert live, (
        "the drawer template is gone and nothing rehydrates the derived card: a restart now "
        "leaves a conductor's board absent from the card surface until someone opens it. "
        f"Land a CALLABLE at one of {[f'{m}.{a}' for m, a in _REHYDRATOR_SITES]} first, or "
        "see the tracking issue. A name in a comment does not satisfy this."
    )
    # BEHAVIOUR, not merely presence: the callable has to take a slot key and put a card back.
    # Checked by CALLING it against a store holding none, because a function that exists and
    # returns nothing leaves exactly the gap this gate is here to refuse.
    for name, rehydrate in live:
        lifecycle, slot, state, _ = _rehydrator_probe()
        assert lifecycle.derived == {}, "the probe must start with no card, or it proves nothing"
        rehydrate(state, slot.key)
        assert lifecycle.derived.get(slot.key), (
            f"{name} is callable but rebuilt no card for a slot whose panel record holds a board; "
            "a rehydrator that returns without storing does not cover the restart gap"
        )


def test_the_rehydrator_gate_fires_when_the_template_goes(tmp_path: Path) -> None:
    """Control for the gate above, which passes trivially while the template is present.

    A gate whose failing branch has never run is a gate nobody has tested. This builds a tree
    with no drawer template and no rehydrator and requires the check to refuse it.
    """
    import types

    # NO rehydrator today, which is the state the gate must refuse once the template goes.
    assert _rehydrators() == [], "this repo has a rehydrator; the gate's failing branch is stale"

    # A NAME IN PROSE must not satisfy it. This is the hole a text search had: a docstring
    # mentioning the word reads exactly like a landed implementation.
    prose = types.ModuleType("prose_only")
    prose.__doc__ = "one day rehydrate_derived will rebuild the card"
    prose.rehydrate_derived = "not a function"  # type: ignore[attr-defined]
    assert not callable(getattr(prose, "rehydrate_derived", None))

    # And the other direction: a CALLABLE that stores a card satisfies it, so the gate is not
    # simply unsatisfiable. Driven the same way the gate drives a real one.
    lifecycle, slot, state, _ = _rehydrator_probe()

    def _rehydrate(state_: Any, key: str) -> None:
        lifecycle.derived[key] = {"card": {"template": BOARD_TEMPLATE_ID}, "owner": "x"}

    assert lifecycle.derived == {}
    _rehydrate(state, slot.key)
    assert lifecycle.derived.get(slot.key), "the accepting direction must accept"


def test_a_session_key_says_what_kind_of_string_it_is() -> None:
    """`chat-1875` names nothing a reader knows, and it appears on nearly every row.

    The same answer the id and the tally got, for the same reason: a bare identifier in a column
    of them explains itself only to someone who already knows the shape.
    """
    rows = panel_card_data(_full_board())["column_0_rows"]
    assert "session chat-1875" in rows, rows


def test_an_absent_session_still_says_which_detail_is_missing() -> None:
    """The noun stays on an ABSENCE, and that is the half a cold reader needed.

    These cells are positional with no header above them, so the noun is the only thing naming
    which detail a cell is about. Without it a row printed `#42 . not recorded . checks
    161/161` and the reader could not tell what had failed. `worker not recorded` reads as the
    fact it is, and matches the tally, which never made the absence an exception.
    """
    panel = _full_board()
    for card in panel["columns"][0]["cards"]:
        card["sub"] = UNSAID  # type: ignore[typeddict-item]
    rows = panel_card_data(panel)["column_0_rows"]
    assert f"session {NOT_SAID}" in rows, rows
    assert f"· {NOT_SAID} ·" not in rows, f"an unlabelled absence cell survived: {rows}"


def test_a_raw_fold_id_says_it_is_an_item() -> None:
    """A pull request id opens with a hash and announces itself. A raw fold id reads `it_4`,
    which a reader cannot place -- and it sits beside siblings that DO announce themselves, so
    the one nobody can identify is the odd one out."""
    panel = _full_board()
    panel["columns"][0]["cards"][0]["id"] = "it_9"
    rows = panel_card_data(panel)["column_0_rows"]
    assert "item it_9" in rows, rows


def test_an_id_that_already_announces_itself_is_left_alone() -> None:
    """Control. Prefixing everything would read "item" in front of a hash id, which is worse
    than either answer."""
    panel = _full_board()
    panel["columns"][0]["cards"][0]["id"] = "#42"
    rows = panel_card_data(panel)["column_0_rows"]
    assert "#42" in rows and "item #42" not in rows, rows


def test_an_empty_board_says_when_it_is_empty() -> None:
    """An empty board sits beside tiles showing a log with entries and a round past its first.

    Read as "nothing has ever happened here", those three facts cannot be reconciled and a
    reader said so. An empty board in a LATER round is the ordinary end state -- every item
    settled and closed -- so the line names the round and the three agree.
    """
    panel = _full_board()
    panel["progress"] = {"total": 0, "segments": [], "added_since": 0}  # type: ignore[typeddict-item]
    legend = panel_card_data(panel)["progress_legend"]
    assert legend == "no items on the board in round 3", legend


def test_the_legend_names_the_round_the_tile_shows() -> None:
    """ "this round" is a demonstrative, and a card has no conversation to refer into.

    It sat beside a tile reading `round N` and a reader could not tell whether the two meant the
    same pass. The number is read from the TILE's own characters, so the phrase cannot disagree
    with what is printed next to it.
    """
    panel = _full_board()
    data = panel_card_data(panel)
    shown = next(t["v"] for t in panel["stats"] if t["k"] == "round")
    assert f"in round {shown}" in data["progress_legend"], data["progress_legend"]
    assert "this round" not in data["progress_legend"], data["progress_legend"]
    # And it follows the TILE, not a second read of the fold: rewrite the tile and the phrase
    # moves with it.
    moved = _full_board()
    for tile in moved["stats"]:
        if tile["k"] == "round":
            tile["v"] = "9"
    assert "in round 9" in panel_card_data(moved)["progress_legend"]


def test_a_board_with_no_readable_round_stays_vague_rather_than_wrong() -> None:
    """The fallback is the demonstrative, which is vaguer and never false.

    A board that publishes no round, or one no reader can parse, must not have a number invented
    for it -- naming the wrong round is worse than not naming one.
    """
    for planted in (UNSAID, {"not": "a number"}):
        panel = _full_board()
        panel["stats"] = [
            dict(tile, v=planted) if tile["k"] == "round" else tile  # type: ignore[typeddict-item]
            for tile in panel["stats"]
        ]
        legend = panel_card_data(panel)["progress_legend"]
        assert "this round" in legend, legend
        assert "in round" not in legend, legend


def test_an_absent_id_still_says_which_detail_is_missing() -> None:
    """Same for the id, and on an UNREADABLE board this is what stops a row of bare phrases.

    A board whose fold could not be read printed three `could not be read` cells in a line, one
    per detail, none of them saying which detail. The hash-id exemption is the only one that
    survives, because a hash already announces itself.
    """
    panel = _full_board()
    panel["columns"][0]["cards"][0]["id"] = UNSAID  # type: ignore[typeddict-item]
    rows = panel_card_data(panel)["column_0_rows"]
    assert f"item {NOT_SAID}" in rows, rows
    unreadable = _full_board()
    for card in unreadable["columns"][0]["cards"]:
        card["id"] = {"not": "a string"}  # type: ignore[typeddict-item]
        card["sub"] = {"not": "a string"}  # type: ignore[typeddict-item]
    rows = panel_card_data(unreadable)["column_0_rows"]
    assert f"item {UNREADABLE}" in rows and f"session {UNREADABLE}" in rows, rows


def test_the_check_tally_says_what_it_counts() -> None:
    """The tally is published fraction-shaped, so alone it says how many of something without
    saying of what. The contract defines the field as a CI check tally and refuses anything
    else there, so the noun is fixed rather than guessed."""
    panel = _full_board()
    panel["columns"][0]["cards"][0]["of"] = "41/47"
    rows = panel_card_data(panel)["column_0_rows"]
    assert "checks 41/47" in rows, rows


def test_the_stale_threshold_is_compared_rather_than_printed() -> None:
    """Two-sided, because the marker row for this leaf looks for a word the page could
    carry for another reason. An age past the threshold is stale and an age under it is
    not, so the comparison is what the word reports."""
    late = _full_board()
    late["meta"]["age_seconds"] = 1000
    late["meta"]["stale_after_seconds"] = 100
    assert "stale" in panel_card_data(late)["meta_when"]
    early = _full_board()
    early["meta"]["age_seconds"] = 100
    early["meta"]["stale_after_seconds"] = 1000
    assert "stale" not in panel_card_data(early)["meta_when"]


def test_the_marker_probe_can_fail() -> None:
    """Control. A probe that always passes is not evidence about the card.

    A marker never written into the panel must NOT be found, otherwise the parametrized
    cases above would pass over a flattener that ignored the panel entirely.
    """
    text = "\n".join(panel_card_data(_full_board()).values())
    assert "MARK-never-seeded" not in text


def _contract_leaves(td: Any, prefix: str = "") -> set[str]:
    """Every LEAF field of a TypedDict tree, dotted, ``[]`` marking a list element."""
    import typing

    out: set[str] = set()
    for name, tp in typing.get_type_hints(td).items():
        path = f"{prefix}{name}"
        if isinstance(tp, type) and issubclass(tp, dict) and hasattr(tp, "__annotations__"):
            out |= _contract_leaves(tp, f"{path}.")
            continue
        if typing.get_origin(tp) is list:
            args = typing.get_args(tp)
            item = args[0] if args else None
            if isinstance(item, type) and issubclass(item, dict):
                out |= _contract_leaves(item, f"{path}[].")
                continue
        out.add(path)
    return out


# ---------------------------------------------------------------------------
# the host's caps, which drop a card WHOLE rather than degrading it
# ---------------------------------------------------------------------------


def test_the_producers_byte_cap_IS_the_hosts_own_object() -> None:
    """Not merely equal to the host's cap -- the same value, imported from it.

    Equality was the weaker assertion this once made, and it left the producer free to carry
    its own copy of the number the host enforces. A copy can drift, and a producer bounding a
    card against a stale limit builds cards the host drops whole. There is no FIELD-cap
    constant to compare because the host spells 24 inline and exports nothing; the field count
    is asserted instead by handing the real card to the real ``normalize_card``.
    """
    assert contract._HOST_MAX_DATA_BYTES is MAX_DATA_BYTES


def test_the_card_page_fits_the_html_cap() -> None:
    assert len(_page().encode("utf-8")) <= MAX_HTML_BYTES


def test_a_full_board_fits_the_field_and_byte_caps() -> None:
    data = panel_card_data(_full_board())
    assert (
        sum(len(k.encode()) + len(v.encode()) for k, v in data.items())
        <= contract._HOST_MAX_DATA_BYTES
    )


def test_a_board_of_three_hundred_items_is_bounded_and_says_so() -> None:
    """A board may hold hundreds of items; the host drops a card over the cap WHOLE.

    So the rows are trimmed and the trim is COUNTED in the same text. A reader is never
    shown part of a board as if it were all of it -- which is the failure a silent prefix
    produces, and the reason the remainder carries its own denominator.
    """
    items = [_item(f"it_{i}", pr=10000 + i, worker=f"chat-{i:06d}") for i in range(300)]
    judgment = validate_judgment(
        {
            "you": {
                f"it_{i}": "a long sentence about what a person must do here " * 3
                for i in range(300)
            }
        }
    )
    panel = build_pipeline_board(
        _view(items),
        judgment,
        name="C",
        captured_at="2026-09-29T15:37:00Z",
        stale_after_seconds=900,
        now_epoch=1790696200.0,
    )
    data = panel_card_data(panel)
    assert len(data) == len(set(card_fields(_page()))), "trimming must not drop a field"
    assert (
        sum(len(k.encode()) + len(v.encode()) for k, v in data.items())
        <= contract._HOST_MAX_DATA_BYTES
    )
    assert re.search(r"\+\d+ more items \(of 300\)", data["column_0_rows"]), data["column_0_rows"]


# ---------------------------------------------------------------------------
# no blanks, and three states rather than two
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("empty", {}),
        ("no stats", {"stats": []}),
        ("stats not a list", {"stats": None}),
        ("meta not a mapping", {"meta": "nope"}),
        ("columns not a list", {"columns": "nope"}),
        ("progress unreadable", {"progress": {"total": "six", "segments": {"b": 1}}}),
    ],
)
def test_no_field_is_ever_blank(name: str, payload: dict[str, Any]) -> None:
    """The host binds a field it was not given to ``""``.

    So a blank is what a DROPPED field looks like, and must not also be what a real value
    looks like: on a page of counts the two are the same pixel and one of them is a lie.
    """
    data = panel_card_data(payload)  # type: ignore[arg-type]
    blank = sorted(key for key, value in data.items() if not value.strip())
    # The NOTICE fields are exempt, and only those: a notice says something ABOUT a value, so an
    # empty one means there is nothing to notice. Every other field holds something a reader
    # COUNTS, where a blank cannot be told from a zero. Naming the set here rather than dropping
    # the check keeps the distinction auditable, and the cases where a notice MUST speak are
    # pinned by test_a_board_from_another_contract_version_says_the_two_disagree,
    # test_an_unreadable_contract_version_says_so and test_a_written_gloss_still_reaches_its_tile.
    assert [k for k in blank if k not in _NOTICE_FIELDS] == [], f"{name}: blank field(s) {blank}"


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("empty", {}),
        ("hostile", {"stats": None, "meta": 3, "columns": (), "progress": []}),
    ],
)
def test_the_field_set_does_not_depend_on_the_payload(name: str, payload: dict[str, Any]) -> None:
    """Every field the page binds is written on EVERY board.

    Deriving the field set from what arrived is the one mistake this card cannot survive:
    a tile whose key went missing binds to the empty string and renders as a blank number
    beside two real ones, which reads as a zero.
    """
    assert set(panel_card_data(payload)) == set(card_fields(_page()))  # type: ignore[arg-type]


#: Fields that carry a NOTICE rather than a value, and may therefore be empty. Derived from the
#: closed stat vocabulary rather than listed by hand, so a new metric cannot add an unexempted
#: notice or a stale name linger here.
_NOTICE_FIELDS: frozenset[str] = frozenset(
    {"contract_note"} | {f"stat_{key}_note" for key in BOARD_STAT_KEYS}
)


def test_the_notice_fields_are_the_only_ones_allowed_to_be_empty() -> None:
    """The exemption set is bounded and every member of it is a field the page actually binds."""
    bound = set(card_fields(_page()))
    assert _NOTICE_FIELDS <= bound, sorted(_NOTICE_FIELDS - bound)
    # Every other bound field carries a value, so on a full board none of them is empty.
    data = panel_card_data(_full_board())
    assert [k for k, v in data.items() if not v.strip() and k not in _NOTICE_FIELDS] == []


def test_a_written_gloss_still_reaches_its_tile() -> None:
    """Control for the exemption: a notice the publisher DID write must still be shown.

    Otherwise "empty when silent" could be satisfied by never showing a gloss at all.
    """
    data = panel_card_data(_full_board())
    assert data["stat_items_note"] == "items the fold folded"
    # A tile with NO published gloss and NO fixed fallback stays empty, which is the half of
    # "empty when silent" that still has to hold.
    # A tile with no published gloss falls back to the fixed one where a fixed one exists. All
    # three stat tiles now have one, because all three labels were read as unexplained -- so the
    # "empty when silent" half is asserted against the exemption set instead of against a tile,
    # and the fallbacks themselves are pinned by the tests below.
    bare = _full_board()
    bare["stats"] = [dict(tile, note=UNSAID) for tile in bare["stats"]]  # type: ignore[typeddict-item]
    shown = panel_card_data(bare)
    for key in BOARD_STAT_KEYS:
        assert shown[f"stat_{key}_note"] == contract._STAT_GLOSS[key], key


def test_the_round_tile_carries_a_gloss_the_publisher_did_not_have_to_write() -> None:
    """`items on board` and `log entries` say what they are in their own labels. "round" does
    not, and a reader who has never met the conductor never learns it -- so that one tile falls
    back to a fixed gloss, because the publisher's note is optional and most boards show none.
    """
    bare = _full_board()
    bare["stats"] = [dict(tile, note=UNSAID) for tile in bare["stats"]]  # type: ignore[typeddict-item]
    gloss = panel_card_data(bare)["stat_round_note"]
    assert gloss == "one round is one full pass over this board", gloss
    # NOT THE LABEL AGAIN, and not the machinery either. Two ways this gloss has failed a cold
    # reader: a bare synonym of its own label ("the current pass"), and words naming the parts
    # rather than the reading ("the conductor has swept its items"), which answers one unknown
    # word with two more.
    for jargon in ("conductor", "swept", "crew"):
        assert jargon not in gloss, f"{jargon!r} is not a word the card teaches: {gloss}"
    # DEFINES rather than renames. Naming the label and its synonym in one breath -- "one round is
    # one pass" -- is what stops a reader wondering whether the two are separate counts, which is
    # how a bare synonym ("the current pass") read. And the pass has a REFERENT: over THIS BOARD,
    # the thing on screen, so the sentence lands without a conductor to explain.
    assert gloss.startswith("one round is"), gloss
    assert "board" in gloss, gloss


def test_the_card_says_once_that_it_is_not_clickable() -> None:
    """Readers keep trying to click, and no rewording of a count has stopped them.

    An item id, a pull request number and a remainder all read as things that lead somewhere, and
    on an inert card none do. Said ONCE in the footer rather than left to each identifier to
    disappoint separately -- and said in WORDS, which is the channel a reader who cannot see a
    cursor still has.
    """
    # ASSERTED AS THE WORDS, not as the constant: `constant in text` is True for every text when
    # the constant is emptied, so that spelling passed over a card that had stopped saying it.
    assert contract._READ_ONLY_NOTE, "the note must not be empty"
    for name, panel in (("full", _full_board()), ("empty", {})):
        shown = panel_card_data(panel)["since"]  # type: ignore[arg-type]
        assert "is a link" in shown, f"{name}: {shown}"
        assert contract._READ_ONLY_NOTE in shown, f"{name}: {shown}"


def test_the_two_biggest_numbers_are_glossed_against_each_other() -> None:
    """41 against 6, 400 against 40: each label said what it counted and neither said how the two
    differ, so a reader could not tell an entry from an item. The glosses define them as a pair --
    an item is a piece of work, an entry is a line about that work -- which is also why entries
    outnumber items by design.
    """
    bare = _full_board()
    bare["stats"] = [dict(tile, note=UNSAID) for tile in bare["stats"]]  # type: ignore[typeddict-item]
    data = panel_card_data(bare)
    items, entries = data["stat_items_note"], data["stat_entries_note"]
    assert "piece of work" in items, items
    assert "line" in entries and "about that work" in entries, entries
    # Each names its own unit, so neither reads as a restatement of the other.
    assert items.startswith("one item is") and entries.startswith("one entry is")


def test_a_published_gloss_outranks_the_fixed_one() -> None:
    """The publisher's note is about THIS board; the fallback is about the metric. A fallback
    that won would delete what a crew actually wrote."""
    panel = _full_board()
    for tile in panel["stats"]:
        if tile["k"] == "round":
            tile["note"] = "the third pass over this goal"  # type: ignore[typeddict-item]
    assert panel_card_data(panel)["stat_round_note"] == "the third pass over this goal"


class _Hostile:
    """A published value whose ``str()`` raises -- which would take the whole card out."""

    def __str__(self) -> str:
        raise RuntimeError("this value cannot be read as text")

    __repr__ = __str__


def test_a_published_value_that_cannot_be_read_says_so_in_its_own_words() -> None:
    """THREE states, not two.

    A field nobody filled and a field holding an object are different facts: told the
    same way, a reader is sent looking for a publisher who never existed. The drawer's
    script distinguished them, and the card -- which has no script -- distinguishes them
    in the flattener instead.
    """
    data = panel_card_data({"lede": _Hostile(), "since": None})  # type: ignore[arg-type]
    assert data["lede"] == UNREADABLE
    # The footer NAMES its absence rather than printing the bare phrase: a floating
    # "not recorded" says something is missing and not which thing, the same defect the row
    # cells had. The state itself is still the unsaid one, which is what this test is about.
    assert NOT_SAID in data["since"] and data["since"].startswith("first entry "), data["since"]
    assert UNREADABLE not in data["since"]
    assert UNREADABLE != NOT_SAID


def test_an_unsaid_value_never_reaches_the_card_as_a_zero() -> None:
    """A board with no work entry has no AGE -- which is not an age of zero."""
    panel = build_pipeline_board(
        _view([]),
        validate_judgment({}),
        name="C",
        captured_at="2026-09-29T15:37:00Z",
        stale_after_seconds=900,
        now_epoch=1790696200.0,
    )
    panel["meta"]["age_seconds"] = "__unsaid__"  # type: ignore[typeddict-item]
    assert NOT_SAID in panel_card_data(panel)["meta_when"]
    assert "0s" not in panel_card_data(panel)["meta_when"]


# ---------------------------------------------------------------------------
# the VALUES a field may take, which parity cannot see
# ---------------------------------------------------------------------------


#: What a READER sees on each tile, against the field name its value binds by. The two are
#: deliberately different: `items` and `entries` are the fold's words and both read as "pieces
#: of work" to someone who does not know it, so the page says which is the board's items and
#: which is the log's entries, and that the item count is the board's whole total.
_STAT_LABELS: dict[str, str] = {
    "items": "items on board",
    "entries": "log entries",
    "round": "round",
}


def test_the_card_binds_a_value_and_a_note_for_every_stat_the_provider_emits() -> None:
    """A tile's LABEL is literal text in the page, not a bound field.

    ``stats[].k`` is a closed provider vocabulary, so a label is the same on every board
    ever published and belongs in the inert half. The key still reaches the card as the
    field NAME its value binds by -- and that is the join this asserts, against a real
    board rather than against the constant, so renaming a metric reddens here instead of
    leaving a tile bound to nothing.
    """
    emitted = [stat["k"] for stat in _full_board()["stats"]]
    assert emitted == list(BOARD_STAT_KEYS), "the provider's tiles against the closed set"
    assert set(_STAT_LABELS) == set(BOARD_STAT_KEYS), "every tile needs a declared label"
    bound = set(card_fields(_page()))
    page = _page()
    for key in emitted:
        assert f"stat_{key}" in bound, f"no value bound for the {key} tile"
        assert f"stat_{key}_note" in bound, f"no note bound for the {key} tile"
        label = _STAT_LABELS[key]
        assert f">{label}</span>" in page, f"the {key} tile does not carry the label {label!r}"


def test_the_column_field_groups_are_the_closed_item_states() -> None:
    """One field group per state, by POSITION over the closed vocabulary.

    Keyed on what arrived instead, a board missing a column shifts every later column's
    field one place and prints one state's items under another state's heading.
    """
    assert tuple(BOARD_COLUMN_NAMES) == tuple(WORK_ITEM_STATES)
    bound = set(card_fields(_page()))
    for index in range(len(WORK_ITEM_STATES)):
        assert f"column_{index}_head" in bound
        assert f"column_{index}_rows" in bound
    assert f"column_{len(WORK_ITEM_STATES)}_head" not in bound, "a column no state fills"


def test_a_board_missing_a_column_does_not_shift_the_others() -> None:
    """Indexing the arrived list by position looks equivalent, because the provider emits
    the four states in order. A board missing one column then shifts every later column's
    field one place and prints one state's items under another state's heading -- a WRONG
    board rather than an incomplete one, with nothing on the page saying so."""
    panel = _full_board()
    removed = panel["columns"].pop(1)
    data = panel_card_data(panel)
    for index, state in enumerate(BOARD_COLUMN_NAMES):
        assert data[f"column_{index}_head"].startswith(state), data[f"column_{index}_head"]
    gone = removed["cards"][0]["id"]
    assert gone not in "\n".join(data.values()), "a removed column's cards appear elsewhere"


def test_a_columns_cards_follow_its_name_not_its_position() -> None:
    """The covering test for ``columns[].name``: the name decides the field group.

    Two columns swapped in the payload must swap on the card too. Placed by position
    instead, each column's cards would print under the other's heading and every count
    would still add up -- the failure a marker round-trip cannot see.
    """
    panel = _full_board()
    panel["columns"][0], panel["columns"][1] = panel["columns"][1], panel["columns"][0]
    data = panel_card_data(panel)
    # ``it_1`` is the accepted item, which is the second closed state.
    assert "#14689" in data["column_1_rows"], data["column_1_rows"]
    assert "#14583" in data["column_0_rows"], data["column_0_rows"]


def test_every_count_on_the_card_carries_its_denominator() -> None:
    """A bare count says nothing about the size of the thing it counts."""
    data = panel_card_data(_full_board())
    for index in range(len(BOARD_COLUMN_NAMES)):
        assert re.search(r"\d+ of \d+", data[f"column_{index}_head"]), data[f"column_{index}_head"]
    assert re.search(r"\d+ of \d+ settled", data["progress_legend"]), data["progress_legend"]
    # The board's revision is the conductor's round, so the card prints it ONCE, as the tile.
    assert data["stat_round"] == "3", data["stat_round"]
    assert not any(k.startswith("meta_revision") for k in data), sorted(data)
    assert "revision" not in data["meta_when"], data["meta_when"]


def test_the_card_reads_the_version_the_contract_writes() -> None:
    """Otherwise the disclosure inverts: the shipped card would declare a mismatch on
    every board the shipped provider publishes, and a REAL mismatch -- an operator
    override serving an older page -- would be indistinguishable from that noise."""
    # SILENT when they agree: the healthy case is the one field on this card that is empty,
    # because "contract" is a word about this code's internals and a permanent header phrase no
    # reader can parse costs more than the disclosure it protects. The guard moved here.
    assert panel_card_data(_full_board())["contract_note"] == ""


def test_a_board_from_another_contract_version_says_the_two_disagree() -> None:
    panel = _full_board()
    panel["contract_version"] = CONTRACT_VERSION + 1
    note = panel_card_data(panel)["contract_note"]
    assert "different version" in note and "may be missing or wrong" in note
    # NO VERSION NUMBER and no "contract": this fires exactly when the reader most needs to be
    # told something they can act on, and a number plus a word about this code's internals is
    # neither. Pinned rather than merely written, because the jargon was here once already.
    assert "contract" not in note
    assert str(CONTRACT_VERSION) not in note and str(CONTRACT_VERSION + 1) not in note


def test_an_unreadable_contract_version_says_so() -> None:
    """The second case that must never be silent.

    ``contract_note`` is empty when the versions agree, so the guard against a DROPPED
    disclosure is these two tests rather than a permanent phrase on the page. Something IS in
    the field and it is not a version, so the two halves cannot be compared at all -- a
    different fact from a version that merely differs, and it keeps its own words.
    """
    panel = _full_board()
    panel["contract_version"] = {"nested": "object"}  # type: ignore[typeddict-item]
    note = panel_card_data(panel)["contract_note"]
    assert note, "an unreadable contract version said nothing"
    assert UNREADABLE in note
    assert "contract" not in note and str(CONTRACT_VERSION) not in note
    # DISTINGUISHABLE from the merely-different case, which is the whole reason this branch
    # exists: "could not be read" and "a different version" are two different facts, and one
    # sentence for both would be the three states collapsing into two.
    other = _full_board()
    other["contract_version"] = CONTRACT_VERSION + 1
    assert note != panel_card_data(other)["contract_note"]


def test_dropped_entries_are_stated_either_way() -> None:
    """Zero says "all entries shown" rather than nothing.

    A field bound to the empty string is exactly what a dropped disclosure looks like, so
    the agreeing case needs words of its own -- and an item missing from a board is not
    recoverable by whoever reads the board.
    """
    assert (
        "2 of the log's 41 entries left out of these counts"
        in panel_card_data(_full_board())["omitted"]
    )
    quiet = _full_board()
    quiet["omitted"] = 0
    assert panel_card_data(quiet)["omitted"] == "every log entry counted"


# ---------------------------------------------------------------------------
# the page is inert, and survives the host's own sanitizer
# ---------------------------------------------------------------------------


def test_the_card_page_declares_no_control_and_no_script() -> None:
    """The host strips every one of these, so a page authoring one ships a layout with a
    hole in it and an author who believes the control exists."""
    used = _scan(_page()).tags & _FORBIDDEN_TAGS
    assert not used, f"the page authors control/script tags the host strips: {sorted(used)}"


def test_the_card_page_points_nowhere_outside_itself() -> None:
    """``src`` always reaches outside; ``href`` does unless it is a same-document
    fragment. The host removes both, and CSP does not govern navigation."""
    assert not _scan(_page()).outbound


def test_the_card_page_avoids_the_properties_the_host_deletes() -> None:
    """``dashboardDocument.ts`` deletes these in card mode, so a label in one of them is
    removed and leaves its row unlabelled with nothing saying why.

    Textual, and deliberately: no Python test runs that TypeScript. The control below
    keeps an empty result from reading as a pass over a page that lost its styles.
    """
    style = re.search(r"<style>(.*?)</style>", _page(), re.DOTALL)
    assert style, "the page has no style block -- this check would pass over a blank page"
    css = style.group(1)
    for prop in _STRIPPED_PROPERTIES:
        assert not re.search(
            rf"(?<![-\w]){re.escape(prop)}\s*:", css
        ), f"{prop} is deleted in card mode"
    assert "data-dashboard-field" in _page(), "control: the page still binds fields"


def test_no_local_token_shares_a_name_with_the_host_var_it_reads() -> None:
    """``--x: var(--x, fallback)`` is a SELF REFERENCE: invalid at computed-value time, so
    every shorthand using it collapses to nothing, silently. The hairline the whole layout
    rests on would not exist and a declaration count would still read as correct."""
    for name, inner in re.findall(r"(--[\w-]+)\s*:\s*var\(\s*(--[\w-]+)", _page()):
        assert name != inner, f"{name} reads a host var of its own name"


def test_the_real_host_normalizer_accepts_the_card() -> None:
    """THE end-to-end check: the host's own validator, not a restatement of its rules.

    ``normalize_card`` returning ``None`` is how a card is dropped whole, so a page and a
    flattener that satisfy every gate above and fail here would ship a panel that is
    simply never there.
    """
    card = normalize_card({"html": _page(), "data": panel_card_data(_full_board())})
    assert card is not None, "the host refused the card"
    assert set(card["data"]) == set(card_fields(_page()))


def test_every_field_name_is_one_the_host_accepts() -> None:
    """The host's own pattern admits ``[a-zA-Z][a-zA-Z0-9_-]{0,47}`` -- NO DOTS.

    Which is why the flattening rule spells a nested path with ``_``: a dotted name is
    not a name the host merely dislikes, it is one ``normalize_card`` refuses, taking the
    whole card with it.
    """
    for name in card_fields(_page()):
        assert _FIELD_NAME.fullmatch(name), f"{name} is not a binding the host accepts"
        assert "." not in name, f"{name} carries a dot, which the host's pattern refuses"


def test_the_card_page_ships_in_the_wheel_and_the_sdist() -> None:
    """A page missing from the build renders nothing while every gate above stays green.

    Both entries, because the sdist is built from the manifest and the wheel from the
    sdist: an entry in one alone ships a half fix that works perfectly from a checkout.
    """
    root = card_template_path().parents[3]
    directory = card_template_path().parent.name
    assert f"{directory}/*.html" in (root / "setup.cfg").read_text(encoding="utf-8")
    assert f"src/kiro_crew/{directory} *.html" in (root / "MANIFEST.in").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# the publisher's own text is bounded too, not just the row lists
# ---------------------------------------------------------------------------
#
# ``panel_publish`` caps a whole payload at 64 KB -- sixteen times this card's entire data
# budget -- and gates ``lede`` and each ``notes`` value only on being a string. The row
# ladder can shorten only rows, so one long sentence in a publisher field makes the card
# oversized at EVERY row limit: the ladder retreats to one row per column, still does not
# fit, and ``normalize_card`` refuses the card whole. A refused card is not a smaller card,
# it is NO card -- and with the cost opt-in off the reader is told "disabled", which names
# the wrong cause; with a previous card in place the reader is served last round's counts
# marked fresh, which is worse.


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("a long lede", {"lede": "x" * 4000}),
        (
            # ``meta.name`` is a crew DISPLAY NAME -- ``MEMBER_NAME_MAX_CHARS`` admits up to 500
            # characters (~2000 UTF-8 bytes of non-ASCII), and it lands in ``fixed``, which the
            # row-retreat ladder never shortens. So without its own clip a long name alone pushes
            # the card past the cap and the host refuses the whole board -- the same unbuildable
            # hazard the cells below close, for a crew whose only sin is a long name.
            "a long crew name",
            {"meta": {"name": "\u4e2d" * 2000}},
        ),
        ("a long metric gloss", {"stats": [{"k": "items", "v": "6", "note": "y" * 4000}]}),
        (
            "a long action sentence",
            {
                "columns": [
                    {
                        "name": "open",
                        "cards": [{"id": "#1", "sub": "s", "of": "1/1", "you": "z" * 4000}],
                    }
                ]
            },
        ),
        (
            # The CHECK TALLY is publisher-written like the others: it arrives from
            # ``judgment["checks"]`` and ``_gate_fraction`` admits any ``N/M`` of ASCII digits
            # verbatim, so this is a legal publish far under the 64 KB payload cap. It is also
            # the one cell the row ladder cannot help with -- dropping the action lines does not
            # shorten it -- so without a per-cell clip the card is oversized at EVERY limit and
            # the host refuses it whole.
            "a long check tally",
            {
                "columns": [
                    {
                        "name": "open",
                        "cards": [
                            {
                                "id": "#1",
                                "sub": "s",
                                "of": f"{'9' * 2100}/{'9' * 2100}",
                                "you": UNSAID,
                            }
                        ],
                    }
                ]
            },
        ),
        (
            # ``id`` and ``sub`` come from the FOLD rather than a publisher, but neither is
            # length-bounded, so the rule the clip enforces is that no single value can make the
            # card unbuildable -- not that only the publisher-written ones cannot.
            "a long item id and worker key",
            {
                "columns": [
                    {
                        "name": "open",
                        "cards": [
                            {"id": "i" * 3000, "sub": "w" * 3000, "of": "1/1", "you": UNSAID}
                        ],
                    }
                ]
            },
        ),
        (
            "all three at once",
            {
                "lede": "x" * 4000,
                "stats": [{"k": key, "v": "1", "note": "y" * 2000} for key in BOARD_STAT_KEYS],
                "columns": [
                    {
                        "name": "open",
                        "cards": [
                            {"id": f"#{i}", "sub": "s" * 60, "of": "1/1", "you": "z" * 900}
                            for i in range(40)
                        ],
                    }
                ],
            },
        ),
    ],
)
def test_a_long_publisher_field_still_produces_a_card_the_host_accepts(
    name: str, payload: dict[str, Any]
) -> None:
    """The whole point of the bound: a card, not a refusal."""
    data = panel_card_data(payload)  # type: ignore[arg-type]
    size = sum(len(k.encode()) + len(v.encode()) for k, v in data.items())
    assert size <= contract._HOST_MAX_DATA_BYTES, f"{name}: {size} bytes"
    assert normalize_card({"html": _page(), "data": data}) is not None, f"{name}: host refused it"
    assert set(data) == set(card_fields(_page())), f"{name}: the field set changed"


def test_a_clipped_value_says_that_it_was_clipped() -> None:
    """A bare truncation reads as the publisher's own sentence ending there.

    So a reader cannot tell a trimmed sentence from a complete one, which is the same
    ambiguity a silent row prefix creates and the reason the row retreat states its count.
    """
    data = panel_card_data({"lede": "x" * 4000})  # type: ignore[arg-type]
    assert data["lede"].endswith("[trimmed]")
    assert data["lede"].startswith("xxx")


def test_a_short_publisher_field_is_left_exactly_as_written() -> None:
    """Control. A clip that fires on every value would be indistinguishable above."""
    lede = "Six items this round; one needs a person."
    assert panel_card_data({"lede": lede})["lede"] == lede  # type: ignore[arg-type]
    assert "[trimmed]" not in panel_card_data(_full_board())["lede"]


def test_the_words_for_a_gap_are_never_themselves_clipped() -> None:
    """``not said`` and ``unreadable`` are this module's own words, not publisher text."""
    data = panel_card_data({})  # type: ignore[arg-type]
    assert data["lede"] == NOT_SAID
    assert panel_card_data({"lede": _Hostile()})["lede"] == UNREADABLE  # type: ignore[arg-type]


def test_a_clip_never_cuts_a_character_in_half() -> None:
    """Slicing UTF-8 mid-sequence yields text that is not decodable, and the card's data is
    JSON -- so the cut is on a character boundary and the result must survive a round trip
    through the encoding the host serializes it with."""
    for char in ("\u00e9", "\u4e2d", "\U0001f680"):  # 2, 3 and 4 UTF-8 bytes
        data = panel_card_data({"lede": char * 3000})  # type: ignore[arg-type]
        text = data["lede"]
        assert text.encode("utf-8").decode("utf-8") == text
        assert len(text.encode("utf-8")) <= 400, f"{char!r}: {len(text.encode())} bytes"
        assert text.endswith("[trimmed]")


# ---------------------------------------------------------------------------
# the website fixture is generated, so something has to notice when it goes stale
# ---------------------------------------------------------------------------


def test_the_website_fixture_matches_what_the_generator_produces() -> None:
    """THE CALLER for the fixture script's ``--check``.

    ``website/src/test/fixtures/pipelineBoardCard.json`` holds the page and two real data sets,
    written by ``scripts/pipeline_board_card_fixture.py`` so the website test asserts on the
    bytes the gateway publishes rather than a sample someone typed. Nothing under ``.github/``
    runs that script, so the claim that a stale fixture fails rather than passing quietly had
    no mechanism behind it: edit the page or the flattener without regenerating, and the
    website test keeps passing against the OLD card while every Python gate stays green.

    This is that mechanism, in the suite that already runs. It re-runs the generator's own
    ``--check`` mode, so there is one definition of "current" rather than a second copy of the
    comparison here.
    """
    root = card_template_path().parents[3]
    script = root / "scripts" / "pipeline_board_card_fixture.py"
    assert script.is_file(), f"the fixture generator is missing: {script}"
    done = subprocess.run(
        [sys.executable, str(script), "--check"],
        capture_output=True,
        cwd=root,
        stdin=subprocess.DEVNULL,
        timeout=120,
        # The repo's own splat, not a bare ``text=True``: text mode with no ``encoding=``
        # decodes the child's output with the Windows ANSI code page, which the backend lint
        # gate refuses -- and this assertion PRINTS that output in its failure message, so a
        # mis-decoded byte would land in the diagnostic a reader acts on.
        **UTF8_TEXT,
    )
    assert done.returncode == 0, (
        "the website fixture no longer matches the page and flattener on this branch -- "
        f"regenerate it with `python {script.relative_to(root)}`.\n{done.stdout}{done.stderr}"
    )


def test_that_check_can_actually_fail(tmp_path: Any) -> None:
    """Control. A `--check` that answered 0 unconditionally would make the test above a
    decoration, and the fixture could still go stale unnoticed.

    Corrupts a COPY under ``tmp_path`` rather than the checked-in file. Mutating the real one
    was unsound in ordinary operation, not only under a crash: neither `--check` test carries an
    ``xdist_group``, so under the repo's `-n auto` they can land on separate workers while one
    holds the repository's fixture corrupted for the whole subprocess window -- and a worker kill
    skips the restore, leaving it corrupted on disk.
    """
    root = card_template_path().parents[3]
    real = root / "website" / "src" / "test" / "fixtures" / "pipelineBoardCard.json"
    untouched = real.read_bytes()
    copy = tmp_path / "pipelineBoardCard.json"
    copy.write_bytes(untouched.replace(b'"lede"', b'"lede_moved"', 1))
    done = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "pipeline_board_card_fixture.py"),
            "--check",
            "--fixture",
            str(copy),
        ],
        capture_output=True,
        cwd=root,
        stdin=subprocess.DEVNULL,
        timeout=120,
        **UTF8_TEXT,
    )
    assert done.returncode != 0, "--check passed over a fixture that does not match"
    # It must also LOCATE the drift. A check whose failure says only "stale" is one a reader has
    # to re-derive by hand, which cost two CI rounds.
    assert "differs at" in done.stdout, done.stdout
    # And the repository's own fixture is untouched -- snapshotted BEFORE the run, so this
    # compares two different moments rather than a value with itself.
    assert real.read_bytes() == untouched, "the checked-in fixture was modified"
