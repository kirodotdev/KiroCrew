"""HeadingAwareChunker line ranges.

Each chunk's ``line_start``/``line_end`` is stored as the item's ``chunk_range``
and shown to the user as ``(lines X-Y)`` in ``local_knowledge_search``
citations, so it must name the source lines the chunk's text is really on.
"""

from __future__ import annotations

import pytest

from kiro_crew.knowledge.chunker import HeadingAwareChunker


def _source_line_of(text: str, needle: str) -> int:
    return text.count("\n", 0, text.index(needle)) + 1


def _own_first_line(chunk: dict) -> str:
    # chunk() prepends the overlap words as an extra first line from chunk 1 on.
    content = chunk["content"]
    if chunk["chunk_index"] > 0 and "\n" in content:
        content = content.split("\n", 1)[1]
    return content.split("\n", 1)[0]


def _wrong_starts(text: str, chunks: list[dict], *, overlap: bool) -> list[tuple]:
    wrong = []
    for c in chunks:
        first = _own_first_line(c) if overlap else c["content"].split("\n", 1)[0]
        real = _source_line_of(text, first)
        if c["line_start"] != real:
            wrong.append((c["chunk_index"], c["line_start"], real))
    return wrong


def test_plain_text_without_blank_lines_cites_real_lines_at_default_sizes():
    # A 2,000-line log with no blank lines: split on single newlines.
    text = "\n".join(f"2026-10-27 line{i:05d} INFO worker started job alpha" for i in range(2000))
    chunks = HeadingAwareChunker().chunk(text)
    assert len(chunks) > 5
    assert not _wrong_starts(text, chunks, overlap=True)


def test_single_newline_chunks_stay_inside_the_document():
    text = "\n".join(f"word{i} alpha beta gamma delta" for i in range(12))
    chunks = HeadingAwareChunker(target_size=8, overlap=0).chunk(text)
    assert [(c["line_start"], c["line_end"]) for c in chunks] == [(i + 1, i + 1) for i in range(12)]


@pytest.mark.parametrize("gap", [" ", "\t"])
def test_word_split_chunks_cite_the_line_they_are_on(gap):
    # Line 2 is one long run of words, so it is split on words. With tabs the
    # pieces are re-joined with spaces, so they are not substrings of the source.
    text = "intro\n" + gap.join(f"w{j}" for j in range(40)) + "\noutro"
    chunks = HeadingAwareChunker(target_size=10, overlap=0).chunk(text)
    assert [c["line_start"] for c in chunks] == [1] + [2] * (len(chunks) - 2) + [3]


def test_word_split_chunk_is_not_matched_to_a_later_space_separated_copy():
    # Line 1 is tab-separated, so its word-split pieces are not substrings of
    # the source; line 2 repeats the same words space-separated and IS an exact
    # match. The piece must still be cited at line 1, where it comes from.
    words = [f"w{j}" for j in range(40)]
    text = "\t".join(words) + "\n" + " ".join(words)
    chunks = HeadingAwareChunker(target_size=10, overlap=0).chunk(text)
    # Line 1 is force-split into 7-word pieces (6), line 2 merges 8 words (5).
    assert [c["line_start"] for c in chunks] == [1] * 6 + [2] * 5


def test_oversized_markdown_section_sub_chunks_cite_real_lines():
    # One heading over ~20 blank-line-separated paragraphs: the section is
    # oversized and sub-split on paragraph breaks.
    paras = [" ".join(f"p{i}w{j}" for j in range(150)) for i in range(20)]
    text = "# Guide\n\n" + "\n\n".join(paras) + "\n"
    chunks = HeadingAwareChunker().chunk_markdown(text)
    assert len(chunks) > 2
    assert not _wrong_starts(text, chunks, overlap=False)
    for c in chunks:
        assert c["line_end"] == c["line_start"] + c["content"].count("\n")


def test_oversized_section_after_another_section_cites_real_lines():
    paras = [" ".join(f"q{i}w{j}" for j in range(150)) for i in range(10)]
    text = "# Intro\n\nshort intro\n\n## Long\n\n" + "\n\n".join(paras) + "\n"
    chunks = HeadingAwareChunker().chunk_markdown(text)
    assert chunks[0]["line_start"] == 1
    assert not _wrong_starts(text, chunks, overlap=False)


def test_control_blank_line_paragraphs_cite_real_lines():
    paras = [" ".join(f"w{i}x{j}" for j in range(300)) for i in range(8)]
    text = "\n\n".join(paras)
    chunks = HeadingAwareChunker().chunk(text)
    assert not _wrong_starts(text, chunks, overlap=True)
