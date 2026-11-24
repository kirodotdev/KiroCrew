"""Write a text or markdown artifact as a Word file, one paragraph per source line.

The file transcribes the source: each line is one paragraph, kept exactly as
written (spaces, tabs, list markers, table pipes, inline marks and HTML stay
typed text). For a markdown artifact only headings are styled. They are found
from the Markdown parser's block tokens, so a ``#`` line inside a fenced or
indented code block is not one, and a setext heading (text over ``===`` or
``---``) is styled like an ATX one. A heading nested in a quote or list stays
the line as typed.

The cap counts source lines, and a source line is at most one paragraph, so the
cap and the output cannot disagree.
"""

from __future__ import annotations

import io
from contextlib import suppress
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.owner_only_files import owner_only_opener

#: Largest artifact written, in characters and in source lines. Both are checked
#: before anything is parsed or built.
MAX_CHARS = 1_000_000
MAX_LINES = 20_000


class DocxTooLarge(ValueError):
    """The artifact is over :data:`MAX_CHARS` or :data:`MAX_LINES`."""


def _xml_safe(text: str) -> str:
    """*text* without the characters a Word file cannot carry.

    Keeps tab, newline and carriage return; drops the other C0 controls, lone
    surrogates and U+FFFE/U+FFFF.
    """
    return "".join(
        ch
        for ch in text
        if ch in "\t\n\r"
        or (ch >= " " and not "\ud800" <= ch <= "\udfff" and ch not in "\ufffe\uffff")
    )


def _headings(text: str) -> dict[int, tuple[int, int, str]]:
    """Top-level headings of markdown *text*: first line -> (end line, level, text).

    Read from the block tokens only: the inline pass is turned off, because the
    heading text is the inline token's ``content`` and nothing here needs the
    inline tree.
    """
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark", {"html": True}).disable(["inline", "text_join"])
    tokens = parser.parse(text)
    found: dict[int, tuple[int, int, str]] = {}
    for index, token in enumerate(tokens[:-1]):
        if token.type != "heading_open" or token.level != 0 or not token.map:
            continue
        content = tokens[index + 1].content
        joined = " ".join(part.strip() for part in content.split("\n"))
        found[token.map[0]] = (token.map[1], int(token.tag[1:]), joined)
    return found


def build(text: str, markdown: bool) -> bytes:
    """The Word file for *text*, in memory. Raises :class:`DocxTooLarge` over a cap."""
    if len(text) > MAX_CHARS:
        raise DocxTooLarge("characters")
    text = _xml_safe(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if len(lines) > MAX_LINES:
        raise DocxTooLarge("lines")
    # python-docx is imported here, not at module scope: it costs ~75 ms to import
    # and only a Word export needs it.
    from docx import Document  # type: ignore[import-untyped]

    headings = _headings(text) if markdown else {}
    document: Any = Document()
    row = 0
    while row < len(lines):
        if row in headings:
            end, level, title = headings[row]
            document.add_paragraph(title, style=f"Heading {level}")
            row = max(end, row + 1)
        else:
            document.add_paragraph(lines[row])
            row += 1
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def write_docx(text: str, target: Path, markdown: bool) -> None:
    """Create *target*, owner-only where the filesystem keeps modes, with the Word file for *text*.

    The document is built first, so a build failure leaves no file. The create is
    exclusive: an existing file (or a symlink at the name) is refused with
    ``OSError``. A failure after the create, in the write or in the flush and close
    that end it (a full disk), removes the file this call made, and only that one.
    """
    data = build(text, markdown)
    created = False
    try:
        with open(target, "xb", opener=owner_only_opener) as handle:
            created = True
            # Best effort: a filesystem with no modes or ACLs (FAT, exFAT) refuses the
            # lockdown, and the opener has already asked for 0600 where modes exist.
            with suppress(OSError):
                platform_compat.restrict_to_owner(target)  # lockdown-ok: new file, no content yet
            handle.write(data)
    except BaseException:
        if created:
            with suppress(OSError):
                target.unlink()
        raise
