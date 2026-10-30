"""Token-based text chunker for the Knowledge Library.

Uses recursive separator splitting (LightRAG style): split by separators in
priority order, enforce token limit per chunk, with overlap for context.
"""

from __future__ import annotations

import re

# Default separators in priority order (try double-newline first, then single, then space)
_SEPARATORS = ["\n\n", "\n", " "]

CHUNK_TOKEN_SIZE = 800
CHUNK_OVERLAP = 200
MAX_CHUNKS_PER_FILE = 50


def _skip_space(text: str, pos: int) -> int:
    """Offset of the first non-whitespace character at or after *pos*."""
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


# Scripts written without spaces between words (Thai, Lao, Myanmar, Khmer, CJK
# punctuation, kana, ideographs and full-width forms). A whole paragraph in one is
# a single space-separated "word", so each of these characters is counted as a
# token of its own. Hangul is left out: Korean separates its words with spaces.
_UNSPACED = re.compile(
    "[\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u3000-\u30ff"
    "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
)


def _char_tokens(ch: str) -> float:
    """Approximate tokens one character adds to a run holding unspaced script."""
    if _UNSPACED.match(ch):
        return 1.0
    return 0.0 if ch.isspace() else 0.25


def _word_count(text: str) -> int:
    """Approximate token count: 1 word ≈ 1.3 tokens, with unspaced runs counted.

    Only a run holding unspaced script is priced per character (other characters
    in it at about 4 per token); every other run is one word, however long.
    """
    words = 0
    extra = 0.0
    for run in text.split():
        unspaced = len(_UNSPACED.findall(run))
        if unspaced:
            extra += unspaced + (len(run) - unspaced) / 4
        else:
            words += 1
    return int(words * 1.3 + extra)


def _tail_within(text: str, budget: int) -> str:
    """The longest suffix of *text* whose characters cost at most *budget* tokens."""
    cost = 0.0
    start = len(text)
    while start > 0:
        step = _char_tokens(text[start - 1])
        if cost + step > budget and start < len(text):
            break
        cost += step
        start -= 1
    return text[start:].lstrip()


def _span(text: str, piece: str, cursor: int) -> tuple[int, int]:
    """Start and end offsets of *piece* in *text*, searching from *cursor*.

    A split piece is a substring of the source unless it was split on words,
    which re-joins them with single spaces, so any whitespace run between two
    words matches. Pieces come out in source order, so the piece is first tried
    where the next text after *cursor* begins (a linear check); only when it is
    not there is the earliest match searched for. Either way a word-split piece
    is never placed on a later line that repeats the same words exactly.
    """
    words = piece.split()
    if not words:
        return cursor, cursor
    start = pos = _skip_space(text, cursor)
    for n, word in enumerate(words):
        if n:
            gap = _skip_space(text, pos)
            if gap == pos:
                break
            pos = gap
        if not text.startswith(word, pos):
            break
        pos += len(word)
    else:
        if pos == len(text) or text[pos].isspace():
            return start, pos
    found = re.compile(r"\s+".join(map(re.escape, words))).search(text, cursor)
    if found is None:
        return cursor, cursor
    return found.start(), found.end()


class HeadingAwareChunker:
    """Token-based text chunker with recursive separator splitting."""

    def __init__(self, target_size: int = CHUNK_TOKEN_SIZE, overlap: int = CHUNK_OVERLAP):
        self.target_size = target_size
        self.overlap = overlap

    def chunk(self, text: str, source_uri: str | None = None) -> list[dict]:
        """Split text into token-sized chunks using recursive separators."""
        if not text.strip():
            return []

        raw_chunks = self._recursive_split(text, _SEPARATORS)

        # Apply cap
        if len(raw_chunks) > MAX_CHUNKS_PER_FILE:
            raw_chunks = raw_chunks[:MAX_CHUNKS_PER_FILE]

        # Build result with overlap
        result = []
        cursor, line = 0, 1
        for i, content in enumerate(raw_chunks):
            # Locate the chunk in the source: the separator between two chunks
            # may be one newline, a blank line, or a space, so it cannot be assumed.
            at, end = _span(text, content, cursor)
            line_start = line + text.count("\n", cursor, at)
            line_end = line_start + content.count("\n")
            cursor, line = end, line_start + text.count("\n", at, end)

            # Then add overlap (convert token count to word count)
            if i > 0 and self.overlap > 0:
                prev_words = raw_chunks[i - 1].split()
                # Floor at 1: int(self.overlap / 1.3) is 0 for overlap == 1, and
                # prev_words[-0:] is prev_words[0:] — i.e. the ENTIRE previous chunk,
                # which would silently duplicate it into every subsequent chunk.
                overlap_word_count = max(1, int(self.overlap / 1.3))
                overlap_text = " ".join(prev_words[-overlap_word_count:])
                # One unspaced "word" can be the whole previous chunk, so the tail
                # is cut to the overlap budget instead of copying that chunk again.
                if _word_count(overlap_text) > max(self.overlap, int(overlap_word_count * 1.3)):
                    overlap_text = _tail_within(overlap_text, self.overlap)
                content = overlap_text + "\n" + content

            result.append(
                {
                    "content": content,
                    "section_title": None,
                    "chunk_index": i,
                    "line_start": line_start,
                    "line_end": line_end,
                }
            )
        return result

    def _recursive_split(self, text: str, separators: list[str]) -> list[str]:
        """Recursively split text by separators until chunks fit target_size."""
        if _word_count(text) <= self.target_size:
            return [text.strip()] if text.strip() else []

        if not separators:
            # No more separators: group whole words, at most target/1.3 per piece.
            # A run of unspaced script too large for one piece on its own is cut
            # by character cost into slices of the source instead.
            target_words = max(1, int(self.target_size / 1.3))
            chunks = []
            group: list[str] = []
            words = 0
            extra = 0
            for run in text.split():
                run_cost = _word_count(run)
                if run_cost > self.target_size:
                    if group:
                        chunks.append(" ".join(group))
                        group, words, extra = [], 0, 0
                    chunks.extend(self._cut_run(run))
                    continue
                plain = not _UNSPACED.search(run)
                next_words = words + (1 if plain else 0)
                next_extra = extra + (0 if plain else run_cost)
                if group and (
                    next_words > target_words or next_words * 1.3 + next_extra > self.target_size
                ):
                    chunks.append(" ".join(group))
                    group, words, extra = [], 0, 0
                    next_words = 1 if plain else 0
                    next_extra = 0 if plain else run_cost
                group.append(run)
                words, extra = next_words, next_extra
            if group:
                chunks.append(" ".join(group))
            return chunks

        sep = separators[0]
        remaining_seps = separators[1:]
        pieces = text.split(sep)

        # Merge small pieces, split large ones
        chunks = []
        current = ""
        for piece in pieces:
            candidate = (current + sep + piece) if current else piece
            if _word_count(candidate) <= self.target_size:
                current = candidate
            else:
                if current.strip():
                    chunks.append(current.strip())
                # If this single piece exceeds target, recurse with finer separator
                if _word_count(piece) > self.target_size:
                    chunks.extend(self._recursive_split(piece, remaining_seps))
                else:
                    current = piece
                    continue
                current = ""
        if current.strip():
            chunks.append(current.strip())
        return chunks

    def _cut_run(self, run: str) -> list[str]:
        """Cut one separator-free run into contiguous slices under target_size."""
        pieces = []
        start = 0
        cost = 0.0
        for i, ch in enumerate(run):
            step = _char_tokens(ch)
            if cost + step > self.target_size and i > start:
                pieces.append(run[start:i])
                start, cost = i, 0.0
            cost += step
        pieces.append(run[start:])
        return pieces

    def chunk_code(self, text: str, language: str | None = None) -> list[dict]:
        """Split code by function/class boundaries.

        The keyword set is language-generic, not per-``language``: one regex serves
        every extension in ``ingestion.CODE_EXTS``, so a keyword added for one
        language also becomes a boundary for the others that spell it the same way
        (``interface`` in Kotlin/C# is equally a boundary in ``.ts``/``.java``).
        Anything the regex misses is not dropped -- the block merely falls into the
        preceding block and is split on word count.
        """
        lines = text.split("\n")
        boundaries = re.compile(
            r"^\s*(?:def |class |function |fn |fun |func |pub fn |object |interface |"
            r"internal |public |private |protected )"
        )
        blocks: list[tuple[int, list[str]]] = []
        current_start = 1
        current: list[str] = []
        for i, line in enumerate(lines):
            if boundaries.match(line) and current:
                blocks.append((current_start, current))
                current = [line]
                current_start = i + 1
            else:
                current.append(line)
        if current:
            blocks.append((current_start, current))

        # Merge small blocks, split oversized ones
        merged: list[tuple[int, list[str]]] = []
        for start, block in blocks:
            wc = _word_count("\n".join(block))
            if merged and _word_count("\n".join(merged[-1][1])) + wc <= self.target_size:
                merged[-1] = (merged[-1][0], merged[-1][1] + block)
            elif wc <= self.target_size * 2:
                merged.append((start, block))
            else:
                chunk_lines: list[str] = []
                chunk_start = start
                for line in block:
                    chunk_lines.append(line)
                    if _word_count("\n".join(chunk_lines)) >= self.target_size:
                        merged.append((chunk_start, chunk_lines))
                        chunk_start = chunk_start + len(chunk_lines)
                        chunk_lines = []
                if chunk_lines:
                    merged.append((chunk_start, chunk_lines))

        results = [
            {
                "content": "\n".join(block),
                "section_title": None,
                "chunk_index": i,
                "line_start": start,
                "line_end": start + len(block) - 1,
            }
            for i, (start, block) in enumerate(merged)
        ]
        return results[:MAX_CHUNKS_PER_FILE]

    def chunk_markdown(self, text: str) -> list[dict]:
        """Split markdown by headings (##, ###) as natural chunk boundaries."""
        heading_re = re.compile(r"^(#{1,6})\s+(.+)", re.MULTILINE)
        splits = list(heading_re.finditer(text))

        if not splits:
            # No headings — fall back to generic chunking
            return self.chunk(text)

        sections: list[tuple[str | None, int, str]] = []
        for i, match in enumerate(splits):
            title = match.group(2).strip()
            start_pos = match.start()
            end_pos = splits[i + 1].start() if i + 1 < len(splits) else len(text)
            body = text[start_pos:end_pos]
            line_start = text[:start_pos].count("\n") + 1
            sections.append((title, line_start, body))

        # Include any text before the first heading
        if splits[0].start() > 0:
            preamble = text[: splits[0].start()].strip()
            if preamble:
                sections.insert(0, (None, 1, preamble))

        # Merge small sections, split oversized ones
        results: list[dict] = []
        idx = 0
        current_title: str | None = None
        current_body = ""
        current_start = 1
        current_end = 1

        def _section_end_line(line_start: int, body: str) -> int:
            """Last source line the section body's own text occupies.

            A section body is a slice of the source running up to the next
            heading, so it carries the newline separating it from that heading
            and any whitespace-only lines sitting before it. The emitted content
            is stripped, so none of that trailing whitespace is a content line:
            trimming all of it before counting keeps the reported end on the
            section's last line of real text.
            """
            return line_start + body.rstrip().count("\n")

        def _flush() -> None:
            """Emit the accumulated section, sub-splitting it if oversized."""
            nonlocal idx
            if _word_count(current_body) > self.target_size:
                # An oversized body is a single unmerged section, so each sub
                # piece is located in the source from the section's first line.
                cursor, line = 0, current_start
                if current_start > 1:
                    cursor = len(text) - len(text.split("\n", current_start - 1)[-1])
                for sub in self._recursive_split(current_body, _SEPARATORS):
                    at, end = _span(text, sub, cursor)
                    sub_line_start = line + text.count("\n", cursor, at)
                    cursor, line = end, sub_line_start + text.count("\n", at, end)
                    results.append(
                        {
                            "content": sub,
                            "section_title": current_title,
                            "chunk_index": idx,
                            "line_start": sub_line_start,
                            "line_end": sub_line_start + sub.count("\n"),
                        }
                    )
                    idx += 1
            else:
                # line_end tracks the last SOURCE line of the merged sections,
                # not the newline count of the reconstructed body: merging joins
                # bodies with a synthetic "\n" that the source never had, so
                # counting newlines in current_body would inflate the end by one
                # line per merged section and cite past the section's real text.
                results.append(
                    {
                        "content": current_body.strip(),
                        "section_title": current_title,
                        "chunk_index": idx,
                        "line_start": current_start,
                        "line_end": current_end,
                    }
                )
                idx += 1

        for title, line_start, body in sections:
            if not current_body:
                current_title = title
                current_body = body
                current_start = line_start
                current_end = _section_end_line(line_start, body)
            elif _word_count(current_body + body) <= self.target_size:
                current_body += "\n" + body
                current_end = _section_end_line(line_start, body)
                if current_title is None and title is not None:
                    current_title = title
            else:
                _flush()
                current_title = title
                current_body = body
                current_start = line_start
                current_end = _section_end_line(line_start, body)

        if current_body.strip():
            _flush()

        return results[:MAX_CHUNKS_PER_FILE]

    def chunk_slides(self, text: str) -> list[dict]:
        """Split presentation text by slide markers."""
        lines = text.split("\n")
        slides: list[tuple[str | None, int, list[str]]] = []
        current_title: str | None = None
        current_start = 1
        current: list[str] = []
        for i, line in enumerate(lines):
            if line.startswith("## Slide"):
                if current or current_title is not None:
                    slides.append((current_title, current_start, current))
                current_title = line.lstrip("#").lstrip()
                current_start = i + 1
                current = []
            else:
                current.append(line)
        if current or current_title is not None:
            slides.append((current_title, current_start, current))

        results = [
            {
                "content": "\n".join(body).strip(),
                "section_title": title,
                "chunk_index": i,
                "line_start": start,
                "line_end": start + len(body) - 1,
            }
            for i, (title, start, body) in enumerate(slides)
        ]
        return results[:MAX_CHUNKS_PER_FILE]
