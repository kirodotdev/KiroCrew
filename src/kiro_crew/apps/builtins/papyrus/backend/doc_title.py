"""Papyrus — the title a paper's main LaTeX document declares.

The clone handler reads ``\\title{...}`` once and saves it as a cloned paper's
display name. The document comes from a cloned repository, so it is untrusted:
this is a linear scan with no regular expressions, no macro expansion and no
``\\input`` following. Anything it cannot read gives ``""``, and the paper keeps
its folder name. The result still goes through :func:`store.sanitize_title` and
the route's redaction and length cap, like any display name.
"""

from __future__ import annotations

#: The argument of ``\\begin`` that ends the preamble.
_DOCUMENT_ARGUMENT = "{document}"

#: Characters LaTeX escapes with a backslash (``\\&``) that a title shows as-is.
_ESCAPED_TEXT = "#$%&_{}"


def document_title(text: str) -> str:
    """Return the plain-text title the preamble of *text* declares, or ``""``."""
    preamble = _preamble(text)
    start = _title_argument(preamble)
    if start is None:
        return ""
    end = _group_end(preamble, start)
    if end is None:
        return ""
    return _plain_text(preamble[start + 1 : end])


def _preamble(text: str) -> str:
    """*text* up to ``\\begin{document}``, with ``%`` comments removed.

    TeX allows spaces, line breaks and comments between ``\\begin`` and
    ``{document}``, so ``\\begin {document}`` ends the preamble too.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\":
            end = _name_end(text, i + 1)
            if text[i + 1 : end] == "begin" and text.startswith(
                _DOCUMENT_ARGUMENT, _skip_blanks(text, end)
            ):
                break
            out.append(text[i : i + 2])  # an escape such as \% is text, not a comment
            i += 2
        elif char == "%":
            newline = text.find("\n", i)
            if newline == -1:
                break
            i = newline  # the line break stays: it separates words
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _skip_blanks(text: str, start: int) -> int:
    """Index of the first character at or after *start* that is not whitespace or
    part of a ``%`` comment."""
    i = start
    while i < len(text):
        if text[i].isspace():
            i += 1
        elif text[i] == "%":
            newline = text.find("\n", i)
            if newline == -1:
                return len(text)
            i = newline + 1
        else:
            break
    return i


def _title_argument(preamble: str) -> int | None:
    """Index of the ``{`` opening the first ``\\title``'s argument, or None.

    Only the exact command counts, so ``\\titleformat`` does not. The short-title
    form ``\\title[Short]{Full}`` is not read.
    """
    i = 0
    while i < len(preamble):
        if preamble[i] != "\\":
            i += 1
            continue
        end = _name_end(preamble, i + 1)
        if preamble[i + 1 : end] == "title":
            while end < len(preamble) and preamble[end].isspace():
                end += 1
            return end if preamble[end : end + 1] == "{" else None
        i = max(end, i + 2)
    return None


def _name_end(text: str, start: int) -> int:
    """Index just past the command name (ASCII letters) that begins at *start*."""
    end = start
    while end < len(text) and text[end].isascii() and text[end].isalpha():
        end += 1
    return end


def _group_end(text: str, start: int) -> int | None:
    """Index of the ``}`` closing the group *start* opens, or None if it never closes."""
    depth = 0
    i = start
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2  # an escaped brace does not count
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _plain_text(title: str) -> str:
    """Drop ``\\thanks{...}``, turn ``\\\\`` and ``~`` into a space, keep escaped characters,
    and remove every other command and brace, keeping the text between them."""
    out: list[str] = []
    i = 0
    while i < len(title):
        char = title[i]
        if char in "{}":
            i += 1
        elif char != "\\":
            out.append(" " if char == "~" else char)
            i += 1
        elif (end := _name_end(title, i + 1)) > i + 1:
            if title[i + 1 : end] == "thanks":
                note = end
                while note < len(title) and title[note].isspace():
                    note += 1
                if title[note : note + 1] == "{":
                    close = _group_end(title, note)
                    if close is None:
                        break
                    end = close + 1
            i = end
        else:
            symbol = title[i + 1 : i + 2]
            if symbol == "\\" or symbol.isspace():
                out.append(" ")
            elif symbol and symbol in _ESCAPED_TEXT:
                out.append(symbol)
            i += 2
    return " ".join("".join(out).split())
