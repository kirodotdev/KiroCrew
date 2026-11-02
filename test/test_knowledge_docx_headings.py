"""DOCX headings must reach the markdown chunker as headings."""

from pathlib import Path

import pytest

from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.readers import FileReader

docx = pytest.importorskip("docx")


def test_word_heading_7_to_9_stay_markdown_headings(tmp_path: Path) -> None:
    # Markdown has six heading levels. A '#######' line is body text to the
    # chunker, so a Heading 7-9 section would fold into its parent.
    doc = docx.Document()
    doc.add_heading("Guide", level=1)
    doc.add_paragraph("intro")
    for level in (6, 7, 8, 9):
        doc.add_heading(f"Level {level}", level=level)
        doc.add_paragraph(f"body {level}")
    path = tmp_path / "deep.docx"
    doc.save(path)

    text, _meta = FileReader().read(str(path))

    assert "#######" not in text
    assert "###### Level 9" in text.splitlines()
    chunks = HeadingAwareChunker(target_size=6).chunk_markdown(text)
    assert [c["section_title"] for c in chunks] == [
        "Guide",
        "Level 6",
        "Level 7",
        "Level 8",
        "Level 9",
    ]
