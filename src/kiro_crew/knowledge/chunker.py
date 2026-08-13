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


def _word_count(text: str) -> int:
    """Approximate token count using word splitting (1 word ≈ 1.3 tokens)."""
    return int(len(text.split()) * 1.3)


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
        cumulative_lines = 1
        for i, content in enumerate(raw_chunks):
            line_start = cumulative_lines
            line_end = line_start + content.count("\n")
            cumulative_lines = line_end + 2  # +2 accounts for separator between chunks (at least \n\n)

            # Then add overlap (convert token count to word count)
            if i > 0 and self.overlap > 0:
                prev_words = raw_chunks[i - 1].split()
                # Floor at 1: int(self.overlap / 1.3) is 0 for overlap == 1, and
                # prev_words[-0:] is prev_words[0:] — i.e. the ENTIRE previous chunk,
                # which would silently duplicate it into every subsequent chunk.
                overlap_word_count = max(1, int(self.overlap / 1.3))
                overlap_words = prev_words[-overlap_word_count:]
                content = " ".join(overlap_words) + "\n" + content

            result.append({
                "content": content,
                "section_title": None,
                "chunk_index": i,
                "line_start": line_start,
                "line_end": line_end,
            })
        return result

    def _recursive_split(self, text: str, separators: list[str]) -> list[str]:
        """Recursively split text by separators until chunks fit target_size."""
        if _word_count(text) <= self.target_size:
            return [text.strip()] if text.strip() else []

        if not separators:
            # No more separators — force split by words
            words = text.split()
            target_words = int(self.target_size / 1.3)
            chunks = []
            for i in range(0, len(words), target_words):
                chunk = " ".join(words[i:i + target_words])
                if chunk.strip():
                    chunks.append(chunk.strip())
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

    def chunk_code(self, text: str, language: str | None = None) -> list[dict]:
        """Split code by function/class boundaries."""
        lines = text.split("\n")
        boundaries = re.compile(
            r"^\s*(?:def |class |function |fn |func |pub fn |public |private |protected )"
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
            preamble = text[:splits[0].start()].strip()
            if preamble:
                sections.insert(0, (None, 1, preamble))

        # Merge small sections, split oversized ones
        results = []
        idx = 0
        current_title: str | None = None
        current_body = ""
        current_start = 1

        for title, line_start, body in sections:
            if not current_body:
                current_title = title
                current_body = body
                current_start = line_start
            elif _word_count(current_body + body) <= self.target_size:
                current_body += "\n" + body
                if current_title is None and title is not None:
                    current_title = title
            else:
                # Flush current
                if _word_count(current_body) > self.target_size:
                    offset = 0
                    for sub in self._recursive_split(current_body, _SEPARATORS):
                        sub_line_start = current_start + offset
                        results.append({
                            "content": sub,
                            "section_title": current_title,
                            "chunk_index": idx,
                            "line_start": sub_line_start,
                            "line_end": sub_line_start + sub.count("\n"),
                        })
                        offset += sub.count("\n") + 1
                        idx += 1
                else:
                    stripped = current_body.strip()
                    results.append({
                        "content": stripped,
                        "section_title": current_title,
                        "chunk_index": idx,
                        "line_start": current_start,
                        "line_end": current_start + stripped.count("\n"),
                    })
                    idx += 1
                current_title = title
                current_body = body
                current_start = line_start

        # Flush last
        if current_body.strip():
            if _word_count(current_body) > self.target_size:
                offset = 0
                for sub in self._recursive_split(current_body, _SEPARATORS):
                    sub_line_start = current_start + offset
                    results.append({
                        "content": sub,
                        "section_title": current_title,
                        "chunk_index": idx,
                        "line_start": sub_line_start,
                        "line_end": sub_line_start + sub.count("\n"),
                    })
                    offset += sub.count("\n") + 1
                    idx += 1
            else:
                stripped = current_body.strip()
                results.append({
                    "content": stripped,
                    "section_title": current_title,
                    "chunk_index": idx,
                    "line_start": current_start,
                    "line_end": current_start + stripped.count("\n"),
                })

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
                current_title = line.lstrip('#').lstrip()
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
