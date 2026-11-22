"""Here-document lines, read the way bash reads them, for every shell reader.

bash reads a heredoc body line by line after the line that holds its ``<<``, up to
the first line equal to its tag, and the body's characters open no quote for the
text after it. :func:`heredoc_docs` decides, once, which ``<<`` opens a body and
where each body and its closing line end; the quote walk
(:func:`shell_normalizer_walk._iter_shell_chars`) and
:func:`shell_commands.read_commands` both read their bodies from it. It reads raw
lines only, and never looks for the close of a ``$(...)`` or a backtick.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from .call_views import _CALL_VIEWS
from .shell_normalizer_escapes import _decode_ansi_c_body

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Characters that end the tag word after ``<<`` when they stand outside quotes.
_TAG_ENDS = frozenset(" \t\n;&|()<>`")

#: The memo key under which a gate call keeps its command as submitted.
_SUBMITTED = "heredoc_submitted_text"


def keep_submitted_case(text: str) -> None:
    """Keep *text*, as submitted, for this gate call's :func:`cased_view`."""
    memo = _CALL_VIEWS.get()
    if memo is None or "<<" not in text:
        return
    from .shell_normalizer_walk import _fold_line_continuations  # circular

    forms = memo.setdefault(_SUBMITTED, [])
    for form in dict.fromkeys((text, _fold_line_continuations(text))):
        if len(form.lower()) == len(form):
            forms.append((form, form.lower()))


def cased_view(text: str) -> "str | None":
    """*text* in the case its gate call submitted it in, else None when that is unknown.

    bash compares a body's lines with its tag case-sensitively, and the floors read a
    lower-cased copy of the command, so the tag and the closing lines are read on the
    submitted text at the same offsets. Outside a gate call, and when the command had
    no upper case, *text* is its own case.
    """
    memo = _CALL_VIEWS.get()
    forms = memo.get(_SUBMITTED) if memo is not None else None
    if memo is None or not forms or all(form == low for form, low in forms):
        return text
    key = ("heredoc_cased_view", text)
    if key not in memo:
        memo[key] = None
        for form, low in forms:
            at = low.find(text.lower())
            if at >= 0:
                memo[key] = form[at : at + len(text)]
                break
    return memo[key]


def case_varies(tag: str) -> bool:
    """True if the command this gate call submitted spells *tag* in more than one case.

    A text whose submitted case is unknown (:func:`cased_view`) is compared with its
    tag as it stands, which is exact unless the command holds the tag in two cases.
    """
    memo = _CALL_VIEWS.get()
    word = tag.lower()
    if memo is None or not word:
        return False
    key = ("heredoc_case_varies", word)
    if key not in memo:
        spellings = set()
        for form, low in memo.get(_SUBMITTED, ()):
            at = low.find(word)
            while at >= 0:
                spellings.add(form[at : at + len(word)])
                at = low.find(word, at + 1)
        memo[key] = len(spellings) > 1
    return bool(memo[key])


class HereDocSpan(NamedTuple):
    """One here-document body that a ``<<`` opens, read as bash reads it."""

    #: Offset of the ``<<``.
    opener: int
    #: Offset of the newline that ends the ``<<``'s line; the bodies start after it.
    line: int
    #: Offset of the body's first character.
    start: int
    #: Where the closing line starts, or the end of the text when nothing closes it.
    end: int
    #: Where the closing line ends (its newline), or the end of the text.
    tail: int
    #: The tag was quoted (``<<'EOF'``, ``<<\\EOF``): the body expands nothing.
    quoted: bool


def heredoc_docs(
    text: str, start: int = 0, state: int = 0, ansi: bool = False
) -> "tuple[HereDocSpan, ...]":
    """The bodies a read of *text* from *start*, in quote *state*, opens, in order.

    THE one decision of which ``<<`` opens a heredoc: the quote walk
    (:func:`shell_normalizer_walk._iter_shell_chars`) and
    :func:`shell_commands.read_commands` both take their bodies from here. A ``<<``
    opens one when it is live (unquoted, unescaped), is not a ``<<<`` here-string,
    is not in a ``#`` comment, and is not inside a ``${…}`` or ``$[…]`` expansion or
    an arithmetic ``((…))`` / ``$((…))`` span, whose nesting is carried across
    lines. A span that closes as two subshells (``((a) …)``) is no arithmetic. The
    tag and the closing lines are read on :func:`cased_view`; when its case is
    unknown, a ``<<`` whose tag the command spells in two cases opens no body. The
    body text is skipped, and the read goes on unquoted after the closing line.
    """
    memo = _CALL_VIEWS.get()
    source, cuts = (memo or {}).get((_UNFOLDED, text), (text, []))
    docs: "list[HereDocSpan]" = []
    if source.find("<<", start) >= 0:
        for _at in _doc_steps(source, _unfolded_offset(start, cuts), state, ansi, docs):
            pass
    if not cuts:
        return tuple(docs)

    def moved(at: int) -> int:
        return _folded_offset(at, cuts)

    return tuple(
        HereDocSpan(
            moved(d.opener), moved(d.line), moved(d.start), moved(d.end), moved(d.tail), d.quoted
        )
        for d in docs
    )


#: The memo key under which a gate call keeps the text a folded text was folded from.
_UNFOLDED = "heredoc_unfolded_text"


def carry_docs(text: str, folded: str, cuts: "list[tuple[int, int]]") -> None:
    """Have :func:`heredoc_docs` read *folded*, which is *text* with the *cuts*
    (``(start, end)`` of each line continuation) removed, on *text* itself.

    bash removes a continuation while it reads a line, but never inside a body whose
    tag is quoted, so a fold there can make a line that equals the tag (``E\\`` then
    ``OF``) where bash sees none. The bodies are found on the text as written and
    their offsets moved onto the folded text. Kept for the gate call only.
    """
    memo = _CALL_VIEWS.get()
    # shortcut: a text folded twice is read as it stands; carry the first text's
    # cuts too if a second fold ever finds a continuation the first one kept.
    if memo is not None and folded != text and (_UNFOLDED, text) not in memo:
        memo.setdefault((_UNFOLDED, folded), (text, cuts))


def _folded_offset(at: int, cuts: "list[tuple[int, int]]") -> int:
    """Where offset *at* of the unfolded text lands once *cuts* are removed."""
    return at - sum(min(end, at) - start for start, end in cuts if start < at)


def _unfolded_offset(at: int, cuts: "list[tuple[int, int]]") -> int:
    """The offset of the unfolded text that lands on *at* once *cuts* are removed."""
    for start, end in cuts:
        if start > at:
            break
        at += end - start
    return at


def heredoc_stream(
    text: str, start: int, state: int, ansi: bool, docs: "list[HereDocSpan]"
) -> "Iterator[int] | None":
    """:func:`heredoc_docs` read lazily, for a walk: each step's offset is yielded
    before it is read, and *docs* grows with each body whose ``<<`` line ends before
    it. A walk that stops early reads no further, so a line cut into many walks
    (each substitution resumes one) stays linear. None when nothing is left to read.
    """
    if text.find("<<", start) < 0:
        return None
    memo = _CALL_VIEWS.get()
    if memo is not None and (_UNFOLDED, text) in memo:
        docs += heredoc_docs(text, start, state, ansi)  # offsets moved from the unfolded text
        return None
    return _doc_steps(text, start, state, ansi, docs)


def _doc_steps(
    text: str, pos: int, state: int, ansi: bool, docs: "list[HereDocSpan]"
) -> "Iterator[int]":
    from .shell_normalizer_walk import _iter_shell_chars, _opens_comment  # circular

    n = len(text)
    view: "str | None" = ""  # read at the first ``<<``
    expansion: "list[str]" = []  # closers of open ``${…}`` / ``$[…]``
    frames: "list[list]" = []  # [kind, depth, held openers, arithmetic?] per paren/backtick
    fresh: "list | None" = None  # the frame the step before opened
    closing = odd_dollar = False
    tags: "list[list]" = []  # [opener, tag, strip, quoted] per ``<<`` on this line
    while pos < n:
        resume, dollars = n, 0  # a live ``$`` run ending here: ``$$`` opens nothing
        for step in _iter_shell_chars(text, state, ansi, pos, plain=True):
            at = step.offset
            yield at
            dollars = dollars + 1 if step.active and step.char == "$" else 0
            if not expansion and step.text != "\\\n":  # bash drops a continuation
                char, opened, fresh = (step.char if step.active else ""), fresh, None
                if closing:  # the ``)`` before closed the arithmetic's own depth
                    closing = False
                    held = {id(tag) for tag in frames.pop()[2]}
                    if char == ")":
                        char = ""  # ``))`` ends it: its ``<<`` were shifts
                        tags = [tag for tag in tags if id(tag) not in held]
                    else:
                        frames.append(["(", 0, [], None])  # ``((a) …)``: two subshells
                top = frames[-1] if frames else ["", 0, [], None]
                if char == "(" and opened is not None:
                    opened[0] = "(("  # ``((`` or ``$((``
                elif char == "(" and (top[0] != "((" or odd_dollar):
                    frames.append(fresh := ["(", 0, [], None])
                elif char == "(":
                    top[1] += 1
                elif char == ")" and top[0] == "((":
                    top[1], closing = top[1] - bool(top[1]), not top[1]
                elif char == ")" and top[0] == "(" or char == "`" and top[0] == "`":
                    frames.pop()
                elif char == "`":
                    frames.append(["`", 0, [], None])
                odd_dollar = dollars % 2 == 1
            live = step.active and not expansion
            if live and text.startswith("<<", at):
                if view == "":
                    view = cased_view(text)
                tag = heredoc_opener(view or text, at)
                if tag is not None and (view is not None or not case_varies(tag[0])):
                    tags.append([at, *tag])
                    if frames and frames[-1][0] == "((":
                        frames[-1][2].append(tags[-1])  # a shift if the span closes first
            if live and step.char == "#" and _opens_comment(text, at):
                newline = text.find("\n", at)  # a comment's quotes and ``<<`` open nothing
                resume, state, ansi = (n if newline < 0 else newline), 0, False
                break
            if dollars % 2 and text.startswith(("${", "$["), at):
                expansion.append("}" if text[at + 1] == "{" else "]")
            elif step.active and expansion and step.char == expansion[-1]:
                expansion.pop()
            if step.active and step.char == "\n":
                for k, frame in enumerate(frames):  # a span still open past its line
                    if frame[2] and frame[3] is None:
                        frame[3] = _closes_as_arithmetic(text, at, frames[k:])
                    if frame[3]:
                        held = {id(tag) for tag in frame[2]}
                        tags = [tag for tag in tags if id(tag) not in held]
                    frame[2] = []
                if tags:
                    resume = _line_bodies(view or text, at, tags, docs)
                    tags, state, ansi = [], 0, False
                    break
        else:
            break
        pos = resume
        if not tags and text.find("<<", pos) < 0:
            break  # no body is left to open


def _closes_as_arithmetic(text: str, at: int, frames: "list[list]") -> bool:
    """True if the ``((`` of ``frames[0]``, still open at *at*, closes as ``))``.

    bash reads a ``((`` / ``$((`` span over as many lines as it takes, up to the
    ``)`` that matches its second ``(``; only when a ``)`` follows that one is it
    arithmetic, and a ``<<`` in it a shift. Otherwise (``((a) …)``, or no close) it
    reads the span again as subshells, and their ``<<`` open heredocs.
    """
    from .shell_normalizer_walk import _iter_shell_chars  # circular

    widths = {"(": 1, "((": 1}
    depth = sum(widths.get(kind, 0) + inner for kind, inner, *_ in frames)
    depth += sum(kind == "((" for kind, *_ in frames[1:])  # their first ``(`` too
    for step in _iter_shell_chars(text, 0, False, at, plain=True):
        if step.active and step.char in "()":
            depth += 1 if step.char == "(" else -1
            if depth == 0:
                return text.startswith(")", step.offset + 1)
    return False


def _line_bodies(text: str, line: int, tags: "list[list]", docs: "list[HereDocSpan]") -> int:
    """Read the bodies of *tags*, one after another from the line after *line*, into
    *docs*, and return the offset past the last closing line. A body that never
    closes runs to the end of *text*, as in bash."""
    at = line + 1
    for opener, tag, strip, quoted in tags:
        start = min(at, len(text))
        found = heredoc_closing_line(text, start, tag, strip, quoted)
        end, tail = found if found is not None else (len(text), len(text))
        docs.append(HereDocSpan(opener, line, start, end, tail, quoted))
        at = tail + 1
    return min(at, len(text))


def heredoc_opener(text: str, at: int) -> "tuple[str, bool, bool] | None":
    """``(tag, strip tabs, quoted)`` of the heredoc whose ``<<`` starts at *at*, else
    None for a ``<<<`` here-string."""
    if not text.startswith("<<", at) or text.startswith("<<<", at) or text[at - 1 : at] == "<":
        return None
    return heredoc_tag(text, at + 2)


def heredoc_tag(text: str, i: int) -> "tuple[str, bool, bool]":
    """``(tag, strip tabs, quoted)`` of the heredoc whose ``<<`` ends at *i*, with its
    quotes removed as bash removes them."""
    strip = text.startswith("-", i)
    i += strip
    while text[i : i + 1] in (" ", "\t"):
        i += 1
    tag: "list[str]" = []
    quoted = False
    quote = ""
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
            elif quote == '"' and ch == "\\" and text[i + 1 : i + 2] in ('"', "\\", "$", "`"):
                i += 1
                tag.append(text[i])
            else:
                tag.append(ch)
        elif ch in ("'", '"'):
            quote, quoted = ch, True
        elif ch == "$" and text[i + 1 : i + 2] == "'":
            # bash decodes an ANSI-C tag before it compares lines with it:
            # ``$'e\x6ff'`` closes on ``eof``
            end = i + 2
            while end < len(text) and text[end] != "'":
                end += 2 if text[end] == "\\" else 1
            tag.append(_decode_ansi_c_body(text[i + 2 : min(end, len(text))]))
            quoted, i = True, end
        elif ch == "$" and text[i + 1 : i + 2] == '"':
            pass  # ``$"EOF"`` names the tag ``EOF``
        elif text.startswith("\\\n", i):
            i += 1  # bash drops a continuation before it reads the tag word
        elif ch == "\\":
            quoted = True
            i += 1
            tag.append(text[i : i + 1])
        elif ch in _TAG_ENDS:
            break
        else:
            tag.append(ch)
        i += 1
    return "".join(tag), strip, quoted


def heredoc_closing_line(
    text: str, at: int, tag: str, strip: bool, quoted: bool
) -> "tuple[int, int] | None":
    """``(start, end)`` of the closing line of a body that starts at *at*, else None.

    *end* is the offset of the newline after that line, or the end of *text*. In a
    body whose tag is not quoted, bash joins a line that ends in an odd run of
    backslashes to the next one before it compares the line with the tag.
    """
    while at < len(text):
        newline = text.find("\n", at)
        end = len(text) if newline < 0 else newline
        line = text[at:end]
        while not quoted and newline >= 0 and (len(line) - len(line.rstrip("\\"))) % 2:
            newline = text.find("\n", end + 1)
            joined = len(text) if newline < 0 else newline
            line, end = line[:-1] + text[end + 1 : joined], joined
        if (line.lstrip("\t") if strip else line) == tag:
            return at, end
        at = end + 1
    return None


def heredoc_body_spans(text: str) -> "list[tuple[int, int]]":
    """``(start, end)`` of each closed heredoc body that a walk of *text* from offset
    0 meets outside quotes (:func:`heredoc_docs`), in order; *end* is where its
    closing line ends, and the walk reads the text from there unquoted."""
    return [(doc.start, doc.tail) for doc in heredoc_docs(text) if doc.tail < len(text)]
