"""HeadingAwareChunker line ranges.

Each chunk's ``line_start``/``line_end`` is stored as the item's ``chunk_range``
and shown to the user as ``(lines X-Y)`` in ``local_knowledge_search``
citations, so it must name the source lines the chunk's text is really on.
"""

from __future__ import annotations

import pytest

from kiro_crew.knowledge import chunker as chunker_mod
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


# ── Text with no spaces between words ──
#
# A paragraph in Japanese, Chinese or Thai is one space-separated "word". Counting
# words made a 48,000-character document one chunk, and the embedder keeps only
# the first ``content_budget`` characters of a chunk, so most of it was never
# searchable.

_JA = "これは日本語の文章です。"  # 12 characters, no spaces
_TH = "ภาษาไทยไม่มีช่องว่าง"  # 20 characters, no spaces


def _own(chunk: dict) -> str:
    """A chunk's own text, without the overlap line chunk() puts in front of it."""
    content = chunk["content"]
    return content.split("\n", 1)[1] if chunk["chunk_index"] > 0 else content


@pytest.mark.parametrize("unit", [_JA, _TH, "这是一个中文句子。"])
def test_an_unspaced_paragraph_is_split_and_keeps_every_character(unit):
    text = unit * 500
    chunks = HeadingAwareChunker().chunk(text)
    assert len(chunks) > 1
    assert "".join(_own(c) for c in chunks) == text
    assert all(len(_own(c)) <= 800 for c in chunks)


def test_the_overlap_of_an_unspaced_chunk_is_a_tail_not_the_whole_chunk():
    chunks = HeadingAwareChunker().chunk(_JA * 500)
    assert len(chunks) > 1
    for prev, cur in zip(chunks, chunks[1:]):
        overlap = cur["content"].split("\n", 1)[0]
        assert 0 < len(overlap) <= 200
        assert _own(prev).endswith(overlap)


def test_unspaced_lines_are_grouped_and_cite_their_own_lines():
    lines = [_JA * 20 + str(i) for i in range(60)]  # about 240 characters each
    text = "\n".join(lines)
    chunks = HeadingAwareChunker().chunk(text)
    assert len(chunks) > 1
    for c in chunks:
        own = _own(c)
        first = own.split("\n", 1)[0]
        assert c["line_start"] == lines.index(first) + 1
        assert c["line_end"] == c["line_start"] + own.count("\n")


def test_an_unspaced_markdown_section_is_sub_split():
    text = "# 見出し\n\n" + _JA * 500 + "\n"
    chunks = HeadingAwareChunker().chunk_markdown(text)
    assert len(chunks) > 1
    assert all(c["section_title"] == "見出し" for c in chunks)
    assert all(len(c["content"]) <= 800 for c in chunks)


# ── Space-separated text keeps the estimate it had before ──
#
# Before unspaced scripts were counted, the estimate was one word per
# space-separated run, however long. Space-separated text, including long URLs,
# hashes and blobs, must still be priced and chunked that way.

_URL = "https://example.com/" + "a1b2c3d4" * 13  # 124 characters, no space
_BLOB = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo" * 400  # 14,000 characters, no space

_SPACED = {
    "english": " ".join(f"word{i}" for i in range(3000)),
    "lines": "\n".join(f"line {i} has a few plain words in it" for i in range(400)),
    "urls": " ".join(f"see {_URL} for step {i}." for i in range(400)),
    "blob": "intro\n" + _BLOB + "\noutro " + " ".join(["tail"] * 1500),
    "korean": " ".join(["한국어는", "띄어쓰기를", "합니다."] * 1000),
}


def _words_estimate(text: str) -> int:
    return int(len(text.split()) * 1.3)


@pytest.mark.parametrize("name", sorted(_SPACED))
def test_space_separated_text_is_priced_one_word_per_run(name):
    text = _SPACED[name]
    assert chunker_mod._word_count(text) == _words_estimate(text)


@pytest.mark.parametrize("name", sorted(_SPACED))
def test_space_separated_text_chunks_as_under_the_word_estimate(name, monkeypatch):
    text = _SPACED[name]
    got = HeadingAwareChunker().chunk(text)
    got_md = HeadingAwareChunker().chunk_markdown("# T\n\n" + text)
    monkeypatch.setattr(chunker_mod, "_word_count", _words_estimate)
    assert got == HeadingAwareChunker().chunk(text)
    assert got_md == HeadingAwareChunker().chunk_markdown("# T\n\n" + text)
    assert len(got) > 1
