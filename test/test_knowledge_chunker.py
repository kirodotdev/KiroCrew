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


# A ``#`` line inside a fenced code block is a shell or Python comment, not a
# heading: it must not cut the fence open and title the code after itself.
_PARA = " ".join(["word"] * 60)


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_a_hash_line_inside_a_code_fence_is_not_a_heading(fence):
    text = (
        "# Setup\n\n"
        + "\n\n".join([_PARA] * 12)
        + f"\n\n{fence}bash\n# install deps\npip install -e .\n{fence}\n\nAfter the fence.\n"
    )
    chunks = HeadingAwareChunker().chunk_markdown(text)
    assert {c["section_title"] for c in chunks} == {"Setup"}
    fenced = [c for c in chunks if "# install deps" in c["content"]]
    assert len(fenced) == 1 and f"{fence}bash" in fenced[0]["content"]


def test_a_fence_closes_only_on_its_own_character_and_length():
    # A ``~~~`` or a shorter ``` line inside a ```` fence is fence content.
    text = (
        f"# Real\n\n{_PARA}\n\n````md\n```\n~~~\n# not a heading\n````\n\n" f"# After\n\n{_PARA}\n"
    )
    chunks = HeadingAwareChunker(target_size=100).chunk_markdown(text)
    assert [c["section_title"] for c in chunks] == ["Real", "After"]


def test_a_heading_after_a_closed_fence_still_splits():
    text = f"# One\n\n{_PARA}\n\n```\n# code\n```\n\n# Two\n\n{_PARA}\n"
    chunks = HeadingAwareChunker(target_size=100).chunk_markdown(text)
    assert [c["section_title"] for c in chunks] == ["One", "Two"]
    assert chunks[1]["line_start"] == 9


def test_a_document_over_the_chunk_cap_keeps_its_end():
    """Past MAX_CHUNKS_PER_FILE, neighbours merge instead of the tail being dropped."""
    text = "あ" * 600 + "終わりの文"
    chunks = HeadingAwareChunker(target_size=10, overlap=0).chunk(text)
    assert len(chunks) <= chunker_mod.MAX_CHUNKS_PER_FILE
    assert "終わりの文" in chunks[-1]["content"]
    assert "".join(c["content"] for c in chunks) == text
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_a_document_past_the_merge_bound_is_cut_with_a_warning(caplog):
    """Merged chunks stay bounded; what still cannot fit is logged, not dropped silently."""
    pieces = chunker_mod.MAX_CHUNKS_PER_FILE * chunker_mod.MAX_MERGE_WIDTH + 10
    text = "あ" * (10 * pieces)
    with caplog.at_level("WARNING", logger=chunker_mod.__name__):
        chunks = HeadingAwareChunker(target_size=10, overlap=0).chunk(text)
    assert len(chunks) == chunker_mod.MAX_CHUNKS_PER_FILE
    assert max(len(c["content"]) for c in chunks) <= 10 * chunker_mod.MAX_MERGE_WIDTH
    assert "only the first" in caplog.text


def test_slides_over_the_chunk_cap_keep_the_last_slide():
    text = "\n".join(f"## Slide {i}\nbody {i}" for i in range(60))
    chunks = HeadingAwareChunker().chunk_slides(text)
    assert len(chunks) <= chunker_mod.MAX_CHUNKS_PER_FILE
    assert "body 59" in chunks[-1]["content"]
    assert chunks[-1]["section_title"] == "Slide 58"


def _deck_text(bodies: list[str]) -> str:
    # The slide reader joins "## Slide N" blocks with a blank line.
    return "\n\n".join(f"## Slide {n}\n{body}" for n, body in enumerate(bodies, 1))


def test_a_slide_cites_the_last_line_of_its_text():
    text = _deck_text(["first a\nfirst b", "second a\nsecond b\nsecond c"])
    chunks = HeadingAwareChunker().chunk_slides(text)
    assert [(c["line_start"], c["line_end"]) for c in chunks] == [(1, 3), (5, 8)]
    source = text.split("\n")
    for c in chunks:
        assert source[c["line_end"] - 1] == c["content"].split("\n")[-1]


def test_a_slide_followed_by_another_keeps_the_range_it_had():
    chunks = HeadingAwareChunker().chunk_slides(_deck_text(["a\nb", "c"]))
    assert (chunks[0]["line_start"], chunks[0]["line_end"]) == (1, 3)


def test_a_slide_with_no_text_ends_on_its_own_marker():
    chunks = HeadingAwareChunker().chunk_slides("## Slide 1\n## Slide 2\nbody")
    assert [(c["line_start"], c["line_end"]) for c in chunks] == [(1, 1), (2, 3)]


def test_text_before_the_first_slide_ends_on_its_last_text_line():
    chunks = HeadingAwareChunker().chunk_slides("intro\nmore\n\n## Slide 1\nbody")
    assert [(c["line_start"], c["line_end"]) for c in chunks] == [(1, 2), (4, 5)]


@pytest.mark.parametrize("heading", ["#", "# ", "##\t"])
def test_an_empty_heading_does_not_take_the_next_line_as_its_title(heading):
    text = f"{heading}\nFirst paragraph under it\n"
    chunks = HeadingAwareChunker().chunk_markdown(text)
    assert [c["section_title"] for c in chunks] == [None]
    assert "First paragraph under it" in chunks[0]["content"]


def test_a_hash_tag_line_stays_body_text():
    text = "# Notes\n#tag\nmore text\n####### seven\n"
    chunks = HeadingAwareChunker().chunk_markdown(text)
    assert [c["section_title"] for c in chunks] == ["Notes"]
    assert "#tag" in chunks[0]["content"]
    assert "####### seven" in chunks[0]["content"]


class _CopyCounter(str):
    """A str that counts the characters its slices and splits copy."""

    def __new__(cls, value: str, counter: list[int]):
        self = super().__new__(cls, value)
        self.counter = counter
        return self

    def __getitem__(self, key):
        out = str.__getitem__(self, key)
        if isinstance(key, slice):
            self.counter[0] += len(out)
        return out

    def split(self, *args, **kwargs):
        self.counter[0] += len(self)
        return str.split(self, *args, **kwargs)


def test_a_heading_dense_document_is_not_copied_once_per_heading():
    """Each section's first line comes from the pass that found it, not a prefix slice.

    Counting copied characters, rather than timing, keeps the check
    deterministic: a prefix slice per heading copies about half the document
    each time, so the total grows with the square of the heading count.
    """
    doc = "".join(f"## h{i}\nbody line {i}\n" for i in range(2000))
    counter = [0]
    chunks = HeadingAwareChunker().chunk_markdown(_CopyCounter(doc, counter))
    assert chunks
    assert counter[0] <= 8 * len(doc)


_OVERSIZED = (
    "## Small\nfew words\n## Big\n"
    + "alpha beta gamma delta epsilon.\n\n" * 6
    + "## Tail\nlast words\n"
)

_FIXED_DOCUMENTS = {
    "fenced": ("intro\n# Top\n```\n# not a heading\n```\n## Sub\nbody\n", 800),
    "empty headings": ("#\nfirst\n## \nsecond\n## Named\nthird\n", 3),
    "tags": ("# Notes\n#tag\nmore text\n####### seven\n## Next\nend\n", 6),
    "preamble": ("opening line\nsecond line\n## H1\nbody one\n## H2\nbody two\n", 5),
    "oversized": (_OVERSIZED, 12),
    "oversized after a preamble": ("lead in\n\n" + _OVERSIZED, 12),
    "over the chunk cap": ("".join(f"## h{i}\nbody {i}\n" for i in range(200)), 3),
}

# (chunk count, first three, last three) of (section_title, line_start,
# line_end, len(content)), taken from main before the change: nothing may move.
_FIXED_EXPECTED: dict[str, tuple] = {
    "empty headings": (
        3,
        [(None, 1, 2, 7), (None, 3, 4, 10), ("Named", 5, 6, 14)],
        [(None, 1, 2, 7), (None, 3, 4, 10), ("Named", 5, 6, 14)],
    ),
    "fenced": (1, [("Top", 1, 7, 48)], [("Top", 1, 7, 48)]),
    "over the chunk cap": (
        50,
        [("h0", 1, 4, 25), ("h2", 5, 8, 25), ("h4", 9, 12, 25)],
        [("h94", 189, 192, 29), ("h96", 193, 196, 29), ("h98", 197, 200, 29)],
    ),
    "oversized": (
        8,
        [("Small", 1, 2, 18), ("Big", 3, 4, 38), ("Big", 6, 6, 31)],
        [("Big", 12, 12, 31), ("Big", 14, 14, 31), ("Tail", 16, 17, 18)],
    ),
    "oversized after a preamble": (
        8,
        [("Small", 1, 4, 26), ("Big", 5, 6, 38), ("Big", 8, 8, 31)],
        [("Big", 14, 14, 31), ("Big", 16, 16, 31), ("Tail", 18, 19, 18)],
    ),
    "preamble": (
        3,
        [(None, 1, 2, 24), ("H1", 3, 4, 14), ("H2", 5, 6, 14)],
        [(None, 1, 2, 24), ("H1", 3, 4, 14), ("H2", 5, 6, 14)],
    ),
    "tags": (
        3,
        [("Notes", 1, 3, 22), ("Notes", 4, 4, 13), ("Next", 5, 6, 11)],
        [("Notes", 1, 3, 22), ("Notes", 4, 4, 13), ("Next", 5, 6, 11)],
    ),
}


@pytest.mark.parametrize("name", sorted(_FIXED_DOCUMENTS))
def test_chunk_lists_of_fixed_documents_are_unchanged(name):
    text, target = _FIXED_DOCUMENTS[name]
    chunks = HeadingAwareChunker(target_size=target).chunk_markdown(text)
    got = [(c["section_title"], c["line_start"], c["line_end"], len(c["content"])) for c in chunks]
    assert (len(got), got[:3], got[-3:]) == _FIXED_EXPECTED[name]


_PREAMBLE_DOC = "\n\n\nintro line one\nintro line two\n# Heading\nbody a b c d e f g\n"


def test_opening_text_after_blank_lines_cites_the_line_it_is_on():
    chunks = HeadingAwareChunker().chunk_markdown(_PREAMBLE_DOC)
    assert chunks[0]["content"].startswith("intro line one")
    assert chunks[0]["line_start"] == _source_line_of(_PREAMBLE_DOC, "intro line one") == 4
    assert chunks[0]["line_end"] == _source_line_of(_PREAMBLE_DOC, "body a b")


def test_opening_text_alone_in_a_chunk_cites_its_own_lines():
    chunks = HeadingAwareChunker(target_size=5).chunk_markdown(_PREAMBLE_DOC)
    assert [(c["section_title"], c["line_start"]) for c in chunks][:1] == [(None, 4)]


def test_opening_text_that_is_a_chunk_by_itself_ends_on_its_own_last_line():
    chunks = HeadingAwareChunker(target_size=10).chunk_markdown(_PREAMBLE_DOC)
    assert [(c["section_title"], c["line_start"], c["line_end"]) for c in chunks][:1] == [
        (None, 4, 5)
    ]


def test_opening_text_with_no_blank_lines_before_it_starts_on_line_one():
    chunks = HeadingAwareChunker().chunk_markdown("intro line one\n# Heading\nbody\n")
    assert chunks[0]["line_start"] == 1


def test_a_markdown_file_with_no_opening_text_starts_on_its_first_heading():
    chunks = HeadingAwareChunker().chunk_markdown("\n\n# Heading\nbody a b c\n")
    assert chunks[0]["line_start"] == 3
