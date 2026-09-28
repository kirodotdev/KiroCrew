"""Tests for kiro_crew.doc_blocks — structured block extraction for docx.

What is pinned here is the block CONTRACT the dashboard renders against
(heading level, list orderedness, table grid, bold/italic runs) and the one
place this module can be wrong in a way plaintext extraction never could:

* the BUDGETS. A structured payload becomes DOM nodes, so an unbounded block
  count is a render-time denial of service, not just a large response.
"""

from __future__ import annotations

import pytest
from ooxml_fixtures import docx_para, docx_styles_xml, docx_table, write_docx

from kiro_crew import doc_blocks
from kiro_crew.doc_blocks import extract_blocks


def _kinds(blocks: list[dict]) -> list[str]:
    return [block["type"] for block in blocks]


# ── docx ──


def test_heading_levels_and_title_come_back_as_headings(tmp_path):
    f = tmp_path / "report.docx"
    write_docx(
        str(f),
        docx_para("Quarterly report", style="Title")
        + docx_para("Overview", style="Heading1")
        + docx_para("Detail", style="heading 3")
        + docx_para("Body text"),
    )
    blocks, truncated = extract_blocks(str(f))
    assert truncated is False
    assert _kinds(blocks) == ["heading", "heading", "heading", "paragraph"]
    # Title is level 1; "heading 3" is the same style as "Heading3" — a document
    # from a non-Word writer spells it with the space.
    assert [b["level"] for b in blocks[:3]] == [1, 1, 3]
    assert blocks[1]["text"] == "Overview"


def test_paragraph_runs_keep_bold_and_italic(tmp_path):
    f = tmp_path / "runs.docx"
    write_docx(str(f), docx_para("plain") + docx_para("strong", bold=True))
    blocks, _ = extract_blocks(str(f))
    assert blocks[0]["runs"] == [{"text": "plain", "bold": False, "italic": False}]
    assert blocks[1]["runs"] == [{"text": "strong", "bold": True, "italic": False}]


def test_bold_toggle_explicitly_off_is_not_bold(tmp_path):
    """``<w:b w:val="0"/>`` cancels bold inherited from the paragraph style."""
    f = tmp_path / "toggle.docx"
    write_docx(
        str(f),
        '<w:p><w:r><w:rPr><w:b w:val="0"/></w:rPr><w:t>not strong</w:t></w:r></w:p>',
    )
    blocks, _ = extract_blocks(str(f))
    assert blocks[0]["runs"][0]["bold"] is False


def test_consecutive_numbered_paragraphs_become_one_ordered_list(tmp_path):
    f = tmp_path / "list.docx"
    write_docx(
        str(f),
        docx_para("first", num_id="1") + docx_para("second", num_id="1") + docx_para("after"),
        numbering={"1": "decimal"},
    )
    blocks, _ = extract_blocks(str(f))
    assert _kinds(blocks) == ["list", "paragraph"]
    assert blocks[0] == {"type": "list", "ordered": True, "items": ["first", "second"]}


def test_bullet_numbering_is_unordered_and_splits_from_a_numbered_run(tmp_path):
    f = tmp_path / "mixed.docx"
    write_docx(
        str(f),
        docx_para("one", num_id="1") + docx_para("dot", num_id="2"),
        numbering={"1": "decimal", "2": "bullet"},
    )
    blocks, _ = extract_blocks(str(f))
    assert [(b["ordered"], b["items"]) for b in blocks] == [(True, ["one"]), (False, ["dot"])]


def test_list_without_a_numbering_part_renders_unordered(tmp_path):
    """No numbering.xml: a bullet in front of numbered text still reads as a
    bullet, where an invented "1." asserts an order the document never had."""
    f = tmp_path / "nonum.docx"
    write_docx(str(f), docx_para("item", num_id="7"))
    blocks, _ = extract_blocks(str(f))
    assert blocks[0]["ordered"] is False


def test_a_list_numbered_only_by_its_style_is_still_a_list(tmp_path):
    """Word's own "List Bullet" / "List Number" put the numbering in styles.xml, so
    the paragraph carries nothing but ``w:pStyle``. Reading ``w:numPr`` off the
    paragraph alone renders every such list as ordinary body paragraphs -- which is
    exactly what a real Word / python-docx document produces."""
    f = tmp_path / "styled.docx"
    write_docx(
        str(f),
        docx_para("bullet one", style="ListBullet")
        + docx_para("bullet two", style="ListBullet")
        + docx_para("step one", style="ListNumber"),
        numbering={"1": "bullet", "5": "decimal"},
        styles_xml=docx_styles_xml({"ListBullet": "1", "ListNumber": "5"}),
    )
    blocks, _ = extract_blocks(str(f))
    assert [(b["type"], b["ordered"], b["items"]) for b in blocks] == [
        ("list", False, ["bullet one", "bullet two"]),
        ("list", True, ["step one"]),
    ]


def test_a_paragraph_cancelling_its_style_numbering_is_not_a_list(tmp_path):
    """``w:numId w:val="0"`` is Word cancelling numbering the style would supply;
    honouring the style anyway puts a bullet in front of a plain paragraph."""
    f = tmp_path / "cancelled.docx"
    write_docx(
        str(f),
        '<w:p><w:pPr><w:pStyle w:val="ListBullet"/>'
        '<w:numPr><w:numId w:val="0"/></w:numPr></w:pPr>'
        "<w:r><w:t>not a bullet</w:t></w:r></w:p>",
        numbering={"1": "bullet"},
        styles_xml=docx_styles_xml({"ListBullet": "1"}),
    )
    blocks, _ = extract_blocks(str(f))
    assert _kinds(blocks) == ["paragraph"]


def test_a_style_that_names_no_numbering_leaves_its_paragraphs_alone(tmp_path):
    """The complement: the styles.xml lookup must not turn every styled paragraph
    into a list item."""
    f = tmp_path / "quote.docx"
    write_docx(
        str(f),
        docx_para("just a quotation", style="IntenseQuote"),
        styles_xml=docx_styles_xml({"ListBullet": "1"}),
    )
    blocks, _ = extract_blocks(str(f))
    assert _kinds(blocks) == ["paragraph"]


def test_table_comes_back_as_a_row_major_grid(tmp_path):
    f = tmp_path / "table.docx"
    write_docx(str(f), docx_table([["Region", "Total"], ["EU", "12"]]))
    blocks, _ = extract_blocks(str(f))
    assert blocks == [
        {"type": "table", "rows": [["Region", "Total"], ["EU", "12"]], "truncated_cols": False}
    ]


def test_content_control_contents_are_not_dropped(tmp_path):
    """Template-produced documents wrap sections in ``w:sdt``; walking only the
    body's direct children would silently lose everything inside one."""
    f = tmp_path / "sdt.docx"
    write_docx(
        str(f),
        f"<w:sdt><w:sdtContent>{docx_para('Inside', style='Heading1')}</w:sdtContent></w:sdt>",
    )
    blocks, _ = extract_blocks(str(f))
    assert blocks == [{"type": "heading", "level": 1, "text": "Inside"}]


def test_custom_xml_wrapped_sections_are_not_dropped(tmp_path):
    """``w:customXml`` is the other block-level wrapper a template writes around
    a section -- the sibling of ``w:sdt``, with its children directly inside it
    rather than under a content element. The text preview reaches paragraphs
    inside one; a structured walk that did not would return a non-empty block
    list with those sections silently absent, so no fallback and no marker."""
    f = tmp_path / "customxml.docx"
    write_docx(
        str(f),
        docx_para("Before")
        + f"<w:customXml w:element=\"section\">{docx_para('Wrapped', style='Heading1')}"
        + docx_table([["in", "wrapped"]])
        + "</w:customXml>"
        + docx_para("After"),
    )
    blocks, _ = extract_blocks(str(f))
    assert _kinds(blocks) == ["paragraph", "heading", "table", "paragraph"]
    assert blocks[1]["text"] == "Wrapped"
    assert blocks[2]["rows"] == [["in", "wrapped"]]


def test_empty_paragraphs_do_not_become_blocks(tmp_path):
    f = tmp_path / "spacing.docx"
    write_docx(str(f), "<w:p/><w:p/>" + docx_para("only real line") + "<w:p/>")
    blocks, _ = extract_blocks(str(f))
    assert _kinds(blocks) == ["paragraph"]


# ── budgets and refusals ──


def test_element_budget_stops_extraction_and_reports_truncation(tmp_path, monkeypatch):
    """A one-run paragraph is two elements: the run and the block."""
    monkeypatch.setattr(doc_blocks, "MAX_NODES", 6)
    f = tmp_path / "long.docx"
    write_docx(str(f), "".join(docx_para(f"line {i}") for i in range(50)))
    blocks, truncated = extract_blocks(str(f))
    assert len(blocks) == 3
    assert truncated is True


def test_run_count_is_bounded_so_a_paragraph_of_tiny_runs_cannot_flood_the_dom(
    tmp_path, monkeypatch
):
    """512,000 one-character runs alternating bold and plain fit the character
    budget as ONE paragraph -- and the browser then has half a million elements
    to lay out. Elements are charged alongside characters, and the paragraph
    that does not fit is refused whole, like any other unit."""
    monkeypatch.setattr(doc_blocks, "MAX_NODES", 100)
    runs = "".join(
        f"<w:r>{'<w:rPr><w:b/></w:rPr>' if i % 2 else ''}<w:t>x</w:t></w:r>" for i in range(1000)
    )
    f = tmp_path / "runs.docx"
    write_docx(str(f), docx_para("first") + f"<w:p>{runs}</w:p>")
    blocks, truncated = extract_blocks(str(f))
    assert truncated is True
    assert [b["type"] for b in blocks] == ["paragraph"]
    assert blocks[0]["runs"][0]["text"] == "first"


def test_character_budget_stops_extraction_and_reports_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr(doc_blocks, "MAX_CHARS", 20)
    f = tmp_path / "wide.docx"
    write_docx(str(f), "".join(docx_para("x" * 30) for _ in range(10)))
    blocks, truncated = extract_blocks(str(f))
    assert truncated is True
    assert len(blocks) < 10


def test_an_oversized_run_is_dropped_whole_and_never_overruns_the_allowance(tmp_path, monkeypatch):
    """A single ``w:t`` can be megabytes on its own. Charging it AFTER appending
    left the payload past MAX_CHARS by that whole run while every counter still
    read as though it had not -- so it is charged first, and refused whole.

    Refused rather than cut to fit: redaction runs on these strings later and
    downstream and matches patterns, so a string cut to length can carry a partial
    secret those patterns miss. Dropping the string covers every secret shape at
    once, and costs only the LAST string, since extraction stops immediately
    after."""
    monkeypatch.setattr(doc_blocks, "MAX_CHARS", 50)
    f = tmp_path / "onebigrun.docx"
    write_docx(str(f), docx_para("x" * 5000))
    blocks, truncated = extract_blocks(str(f))
    assert truncated is True
    body = "".join(run["text"] for b in blocks for run in b.get("runs", []))
    assert body == ""


def test_a_paragraph_past_the_budget_stops_being_built_at_the_boundary(tmp_path, monkeypatch):
    """The runs of one paragraph were all materialized before the budget saw
    them, so a crafted paragraph of millions of runs was millions of dicts
    before its refusal -- the allocation the budget exists to prevent, paid
    in full and then thrown away. Runs are merged as they are walked and the
    walk stops the moment the paragraph is past what can fit."""
    monkeypatch.setattr(doc_blocks, "MAX_NODES", 100)
    seen: list[int] = []
    real = doc_blocks._toggle_on

    def counting(props, tag):
        seen.append(1)
        return real(props, tag)

    monkeypatch.setattr(doc_blocks, "_toggle_on", counting)
    runs = "".join(
        f"<w:r>{'<w:rPr><w:b/></w:rPr>' if i % 2 else ''}<w:t>x</w:t></w:r>" for i in range(5000)
    )
    f = tmp_path / "runs.docx"
    write_docx(str(f), f"<w:p>{runs}</w:p>")
    blocks, truncated = extract_blocks(str(f))
    assert truncated is True and blocks == []
    # Each formatted run costs two toggle lookups; only the runs up to the
    # boundary may have been visited, never all 2,500 formatted ones.
    assert len(seen) < 2 * 300, f"walked {len(seen) // 2} formatted runs past a 100-element budget"


def test_a_paragraph_is_charged_whole_so_a_run_split_secret_cannot_leak_a_prefix(
    tmp_path, monkeypatch
):
    """Word splits a sentence at every formatting change, so a credential that
    crosses a bold boundary arrives as two runs. Charged run by run, the first
    fitted and the second was refused -- and the redactor, which matches whole
    tokens on the joined paragraph, saw a prefix its patterns do not match.
    The paragraph is one unit of refusal: it fits whole or it is dropped whole."""
    # 40 chars of filler fit; the 20-char secret straddles two runs and does not.
    monkeypatch.setattr(doc_blocks, "MAX_CHARS", 50)
    two_runs = (
        "<w:p>"
        "<w:r><w:t>AKIAIOSFOD</w:t></w:r>"
        "<w:r><w:rPr><w:b/></w:rPr><w:t>NN7EXAMPLE</w:t></w:r>"
        "</w:p>"
    )
    f = tmp_path / "split-secret.docx"
    write_docx(str(f), docx_para("x" * 40) + two_runs)
    blocks, truncated = extract_blocks(str(f))
    assert truncated is True
    texts = ["".join(r["text"] for r in b["runs"]) for b in blocks if b["type"] == "paragraph"]
    assert texts == ["x" * 40], "the straddling paragraph must be absent, not present as a prefix"
    assert not any("AKIA" in t for t in texts)


def test_a_refused_paragraph_ends_extraction_so_nothing_renders_past_the_gap(tmp_path, monkeypatch):
    """Refusing a paragraph that does not fit must also spend the rest of the
    allowance. Left positive, a later SHORTER paragraph still fitted and rendered
    after the dropped one -- a document with an omitted middle, which the
    "only the beginning" truncation notice does not describe."""
    monkeypatch.setattr(doc_blocks, "MAX_CHARS", 50)
    f = tmp_path / "gap.docx"
    write_docx(str(f), docx_para("a" * 40) + docx_para("b" * 20) + docx_para("c"))
    blocks, truncated = extract_blocks(str(f))
    assert truncated is True
    texts = ["".join(r["text"] for r in b["runs"]) for b in blocks if b["type"] == "paragraph"]
    assert texts == ["a" * 40], "the short paragraph after the refused one must not render"


def test_a_run_that_fits_is_still_returned_whole(tmp_path, monkeypatch):
    """The complement, so "refuse what does not fit" cannot be satisfied by
    refusing everything."""
    monkeypatch.setattr(doc_blocks, "MAX_CHARS", 50)
    f = tmp_path / "smallrun.docx"
    write_docx(str(f), docx_para("z" * 40))
    blocks, truncated = extract_blocks(str(f))
    assert truncated is False
    body = "".join(run["text"] for b in blocks for run in b.get("runs", []))
    assert body == "z" * 40


def test_a_row_with_an_oversized_cell_is_dropped_whole(tmp_path, monkeypatch):
    """Charged cell by cell, a refused row was half-present: first cells filled,
    the rest blank, which reads as empty cells rather than a cut table."""
    monkeypatch.setattr(doc_blocks, "MAX_CHARS", 30)
    f = tmp_path / "bigcell.docx"
    write_docx(str(f), docx_table([["a", "b"], ["c", "y" * 4000], ["e", "f"]]))
    blocks, truncated = extract_blocks(str(f))
    assert truncated is True
    assert blocks[0]["rows"] == [["a", "b"]]


def test_a_table_wider_than_the_column_cap_reports_truncation_on_the_table(tmp_path, monkeypatch):
    """Dropping columns silently produced a complete-LOOKING grid with nothing in
    the UI to say the right-hand columns were missing.

    Reported ON THE TABLE, not on the document: the document-level flag renders as
    "only the beginning of this document", which describes running out of budget
    part-way through and sends the reader looking for missing pages instead of
    missing columns."""
    monkeypatch.setattr(doc_blocks, "MAX_TABLE_COLS", 3)
    f = tmp_path / "wide.docx"
    write_docx(str(f), docx_table([[f"c{i}" for i in range(9)]]))
    blocks, truncated = extract_blocks(str(f))
    assert blocks[0]["rows"] == [["c0", "c1", "c2"]]
    assert blocks[0]["truncated_cols"] is True
    assert truncated is False, "a width trim is not the document running out"


def test_many_empty_tables_cannot_build_cells_for_free(tmp_path, monkeypatch):
    """A cell costs an element whether or not it holds text, so a grid of EMPTY
    cells -- which spends no characters at all -- is bounded like any other."""
    monkeypatch.setattr(doc_blocks, "MAX_NODES", 60)
    f = tmp_path / "empty-grids.docx"
    write_docx(str(f), docx_table([["" for _ in range(40)] for _ in range(20)]) * 3)
    blocks, truncated = extract_blocks(str(f))
    assert truncated is True
    cells = sum(len(row) for b in blocks if b["type"] == "table" for row in b["rows"])
    assert cells <= 60


def test_the_element_charge_is_aggregate_across_tables_not_per_table(tmp_path, monkeypatch):
    """The second table inherits what the first spent."""
    monkeypatch.setattr(doc_blocks, "MAX_NODES", 5)
    f = tmp_path / "two.docx"
    write_docx(str(f), docx_table([["a", "b"]]) + docx_table([["c", "d"]]))
    blocks, truncated = extract_blocks(str(f))
    assert truncated is True
    grids = [b["rows"] for b in blocks if b["type"] == "table"]
    # First table: its block and two cells, 3 of 5. The second table's block and
    # first row are one unit of 3, which the remaining 2 do not cover -- so the
    # second table is refused whole, never present and empty.
    assert grids == [[["a", "b"]]]


def test_a_row_whose_cells_are_content_control_wrapped_is_read_not_skipped(tmp_path):
    """A template wraps cells in ``w:sdt``, so a direct-children lookup found none.
    Reading that as "budget spent" ended the whole row loop on the first such row,
    dropping every row after it with nothing marking the gap."""
    wrapped = (
        "<w:tr><w:sdt><w:sdtContent>"
        "<w:tc><w:p><w:r><w:t>wrapped-a</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>wrapped-b</w:t></w:r></w:p></w:tc>"
        "</w:sdtContent></w:sdt></w:tr>"
    )
    f = tmp_path / "sdt-row.docx"
    write_docx(str(f), f"<w:tbl>{wrapped}</w:tbl>")
    blocks, truncated = extract_blocks(str(f))
    assert truncated is False
    assert [b["rows"] for b in blocks if b["type"] == "table"] == [[["wrapped-a", "wrapped-b"]]]


def test_a_row_wrapped_in_a_content_control_is_read(tmp_path):
    """A repeating-section content control wraps whole ROWS in ``w:sdt``. The cell
    walk and the body walk both descended those; the row walk used ``findall`` and
    saw no rows at all, emitting an empty table with nothing marking the loss."""
    wrapped_rows = (
        "<w:sdt><w:sdtContent>"
        "<w:tr><w:tc><w:p><w:r><w:t>r1</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>r2</w:t></w:r></w:p></w:tc></w:tr>"
        "</w:sdtContent></w:sdt>"
    )
    f = tmp_path / "sdt-rows.docx"
    write_docx(str(f), f"<w:tbl>{wrapped_rows}</w:tbl>")
    blocks, truncated = extract_blocks(str(f))
    assert truncated is False
    assert [b["rows"] for b in blocks if b["type"] == "table"] == [[["r1"], ["r2"]]]


def test_a_cell_less_row_does_not_end_the_table(tmp_path):
    """The row-loop regression in its own right: a genuinely empty ``w:tr`` is
    skipped, and the rows AFTER it still arrive."""
    empty_row = "<w:tr></w:tr>"
    f = tmp_path / "gap-row.docx"
    write_docx(
        str(f),
        "<w:tbl>"
        + "<w:tr><w:tc><w:p><w:r><w:t>first</w:t></w:r></w:p></w:tc></w:tr>"
        + empty_row
        + "<w:tr><w:tc><w:p><w:r><w:t>last</w:t></w:r></w:p></w:tc></w:tr>"
        + "</w:tbl>",
    )
    blocks, truncated = extract_blocks(str(f))
    assert truncated is False
    assert [b["rows"] for b in blocks if b["type"] == "table"] == [[["first"], ["last"]]]


def test_a_nested_table_does_not_add_columns_to_the_outer_row(tmp_path):
    """``_row_cells`` descends content controls but NOT arbitrary depth: an inner
    table's cells belong to the inner table, and its text rides along on the outer
    cell's own descendant walk rather than inventing outer columns."""
    inner = "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>inner</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    f = tmp_path / "nested.docx"
    write_docx(
        str(f),
        f"<w:tbl><w:tr><w:tc><w:p><w:r><w:t>outer</w:t></w:r></w:p>{inner}</w:tc></w:tr></w:tbl>",
    )
    blocks, _ = extract_blocks(str(f))
    grids = [b["rows"] for b in blocks if b["type"] == "table"]
    assert len(grids[0][0]) == 1, "outer row must have exactly its own one cell"
    assert "inner" in grids[0][0][0]


def test_a_refused_row_ends_extraction_so_nothing_renders_past_the_table(tmp_path, monkeypatch):
    """A per-table row ceiling refused a row WITHOUT spending the allowance, so
    the paragraphs after a long table still rendered: a 201-row table followed by
    text showed rows 1-200, then the text, as one continuous document with the
    omission unmarked. There is no row ceiling now; a row that does not fit the
    shared budget is refused like any other unit, and that refusal is the end."""
    monkeypatch.setattr(doc_blocks, "MAX_NODES", 4)
    f = tmp_path / "over.docx"
    write_docx(str(f), docx_table([["a"], ["b"], ["c"], ["d"], ["e"]]) + docx_para("after"))
    blocks, truncated = extract_blocks(str(f))
    assert truncated is True
    assert [b["type"] for b in blocks] == ["table"], "nothing may render past a refused row"
    assert blocks[0]["rows"] == [["a"], ["b"], ["c"]]


def test_a_list_that_spends_the_last_of_the_budget_is_present_and_cut_not_dropped(
    tmp_path, monkeypatch
):
    """Emitting the list block at flush, AFTER its items had each been
    charged -- so a list that ran the allowance out was refused as a block and
    every item already paid for vanished with it. The block precedes its
    items, as a table's precedes its rows."""
    monkeypatch.setattr(doc_blocks, "MAX_NODES", 6)
    f = tmp_path / "longlist.docx"
    write_docx(
        str(f),
        docx_para("first") + "".join(docx_para(f"item {i}", num_id="1") for i in range(10)),
    )
    blocks, truncated = extract_blocks(str(f))
    assert truncated is True
    assert _kinds(blocks) == ["paragraph", "list"]
    assert blocks[1]["items"][0] == "item 0"


def test_a_list_whose_first_item_does_not_fit_is_not_emitted_empty(tmp_path, monkeypatch):
    """Charged apart, the list's block fitted where its first item did not, and
    the document rendered as one EMPTY list -- which the viewer takes as
    "structure present" and so shows neither items nor the plain-text fallback.
    The block and its first item are one unit: both or neither."""
    monkeypatch.setattr(doc_blocks, "MAX_NODES", 2)
    f = tmp_path / "one-item.docx"
    write_docx(str(f), docx_para("only item", num_id="1"))
    blocks, truncated = extract_blocks(str(f))
    assert truncated is True
    assert blocks == [], "an empty list must not be the whole preview"


def test_a_table_whose_first_row_does_not_fit_is_not_emitted_empty(tmp_path, monkeypatch):
    """The table shape of the same rule: block and first row are one unit."""
    monkeypatch.setattr(doc_blocks, "MAX_NODES", 1)
    f = tmp_path / "one-cell.docx"
    write_docx(str(f), docx_table([["a"]]))
    blocks, truncated = extract_blocks(str(f))
    assert truncated is True
    assert blocks == [], "an empty table must not be the whole preview"


def test_a_table_that_exactly_fits_is_not_reported_truncated(tmp_path, monkeypatch):
    """The block and three one-cell rows: four elements, four allowed."""
    monkeypatch.setattr(doc_blocks, "MAX_NODES", 4)
    f = tmp_path / "exact.docx"
    write_docx(str(f), docx_table([["a"], ["b"], ["c"]]))
    blocks, truncated = extract_blocks(str(f))
    assert truncated is False
    assert blocks[0]["rows"] == [["a"], ["b"], ["c"]]


def test_a_table_within_the_column_cap_is_not_reported_truncated(tmp_path):
    f = tmp_path / "narrow.docx"
    write_docx(str(f), docx_table([["a", "b"], ["c", "d"]]))
    blocks, truncated = extract_blocks(str(f))
    assert truncated is False
    assert blocks[0]["truncated_cols"] is False


def test_a_short_document_is_not_reported_truncated(tmp_path):
    f = tmp_path / "short.docx"
    write_docx(str(f), docx_para("done"))
    assert extract_blocks(str(f))[1] is False


@pytest.mark.parametrize("name", ["notes.txt", "book.epub", "sheet.xlsx", "old.doc"])
def test_unsupported_extensions_yield_no_blocks(tmp_path, name):
    f = tmp_path / name
    f.write_bytes(b"whatever")
    assert extract_blocks(str(f)) == ([], False)


def test_a_non_zip_file_named_docx_degrades_instead_of_raising(tmp_path):
    f = tmp_path / "fake.docx"
    f.write_bytes(b"not a zip at all")
    assert extract_blocks(str(f)) == ([], False)


def test_sensitive_paths_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(doc_blocks, "is_sensitive_path", lambda path: True)
    f = tmp_path / "secret.docx"
    write_docx(str(f), docx_para("body"))
    assert extract_blocks(str(f)) == ([], False)


def test_extraction_reads_through_a_supplied_handle(tmp_path):
    """The endpoint stat-gates the file then parses the SAME handle, so the bytes
    parsed are the bytes measured."""
    f = tmp_path / "handle.docx"
    write_docx(str(f), docx_para("Heading", style="Heading1"))
    with open(f, "rb") as fobj:
        blocks, _ = extract_blocks(str(f), fileobj=fobj)
    assert blocks == [{"type": "heading", "level": 1, "text": "Heading"}]
