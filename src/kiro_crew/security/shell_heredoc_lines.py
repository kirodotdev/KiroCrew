"""Here-document lines, read the way bash reads them, for the shell quote walk.

bash reads a heredoc body line by line after the line that holds its ``<<``, up to
the first line equal to its tag, and the body's characters open no quote for the
text after it. :func:`shell_normalizer_walk._iter_shell_chars` reads each ``<<`` and
each closing line through these helpers. They read raw lines only, and never look
for the close of a ``$(...)`` or a backtick.
"""

from __future__ import annotations

from .call_views import _CALL_VIEWS
from .shell_normalizer_escapes import _decode_ansi_c_body

#: Characters that end the tag word after ``<<`` when they stand outside quotes.
_TAG_ENDS = frozenset(" \t\n;&|()<>`")

#: The unquoted characters outside a body that the walk hands to :class:`HeredocReader`.
READ_CHARS = frozenset("#()<\n")

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


class HeredocReader:
    """The heredocs that one walk of *text* opens, read as bash reads them.

    The walk hands it each unquoted character in :data:`READ_CHARS` outside a body. A
    ``<<`` opens a heredoc unless it is a ``<<<`` here-string, sits in a ``#``
    comment, or is a shift inside ``$((...))`` or ``((...))``, whose nesting is
    carried across lines. The tag and the closing lines are read on
    :func:`cased_view`; when its case is unknown, a ``<<`` whose tag the command
    spells in two cases opens no body. *ends* holds where each body still ahead ends
    its closing line.
    """

    __slots__ = ("text", "bodies", "ends", "tags", "view", "comment", "depth", "pair")

    def __init__(self, text: str, bodies: "list[tuple[int, int]] | None") -> None:
        self.text, self.bodies = text, bodies
        self.ends: "list[int]" = []
        self.tags: "list[tuple[str, bool, bool]]" = []  # opened on the current line
        self.view: "str | None" = ""  # read at the first ``<<``
        self.comment = False
        self.depth = 0  # open parentheses of an arithmetic span
        self.pair = -1  # the second ``(`` of the span's opener

    def read(self, i: int, ch: str) -> None:
        text = self.text
        if ch == "\n":
            self.comment = False
            if self.tags:
                spans = heredoc_bodies(self.view or text, i + 1, self.tags)
                self.ends, self.tags = [end for _start, end in spans], []
                if self.bodies is not None:
                    self.bodies += [span for span in spans if span[1] < len(text)]
        elif self.comment or i == self.pair:
            return
        elif ch == "(" and self.depth:
            self.depth += 1
        elif ch == "(" and text.startswith("(", i + 1):
            self.depth, self.pair = 2, i + 1  # ``((`` or ``$((``
        elif ch == ")":
            self.depth = max(self.depth - 1, 0)
        elif ch == "#" and not self.depth:
            from .shell_normalizer_walk import _opens_comment  # circular

            self.comment = _opens_comment(text, i)
        elif ch == "<" and not self.depth:
            if self.view == "":
                self.view = cased_view(text)
            tag = heredoc_opener(self.view or text, i)
            if tag is not None and (self.view is not None or not case_varies(tag[0])):
                self.tags.append(tag)


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


def heredoc_bodies(
    text: str, at: int, tags: "list[tuple[str, bool, bool]]"
) -> "list[tuple[int, int]]":
    """``(start, end)`` of the bodies of *tags*, read one after another from *at*.

    *end* is where a body's closing line ends. A body that never closes ends at the
    end of *text*, as in bash, and no body after it is read.
    """
    spans: "list[tuple[int, int]]" = []
    for tag in tags:
        line = heredoc_closing_line(text, at, *tag)
        spans.append((at, len(text) if line is None else line[1]))
        if line is None:
            break
        at = line[1] + 1
    return spans


def heredoc_body_spans(text: str) -> "list[tuple[int, int]]":
    """``(start, end)`` of each closed heredoc body that a walk of *text* from offset
    0 meets outside quotes, in order; *end* is where its closing line ends, and the
    walk reads the text from there unquoted."""
    if "<<" not in text:
        return []
    from .shell_normalizer_walk import _iter_shell_chars  # circular: the walk imports this

    spans: "list[tuple[int, int]]" = []
    for _step in _iter_shell_chars(text, bodies=spans):
        pass
    return spans
