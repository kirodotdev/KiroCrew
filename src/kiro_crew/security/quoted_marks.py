"""Marks the self-protection tokenizer glues onto quoted text before the quotes go.

bash reads a reserved word (``case``, ``esac``, ``in``, the command-position keepers)
only when no character of it is quoted, and a backtick inside single quotes or behind
a backslash is text rather than a command substitution.  The tokenizer drops the
quotes, so the argv walker (``shell_normalizer._SubstitutionDepth``) reading the
de-quoted tokens could not tell ``"case"`` from ``case`` or ``'a`b'`` from ``a`b``:
the first armed a phantom ``case`` pattern and the second opened a substitution no
later token closed, and in both an ssh destination behind the token was never read.

:func:`mask_quoted_reserved_words` runs one quote-state pass over the RAW text and
glues :data:`QUOTED_WORD_MARK` onto a quoted grammar word, and rewrites a single-quoted
or escaped backtick to :data:`QUOTED_TICK_MARK`; the walker then reads an ordinary word,
and every site that compares a token's text drops the marks again.  Both bytes sit
outside the ``\\x00``-``\\x06`` range ``argv_floor._QUOTED_SEP_SENTINELS`` uses for the
same job on the command separators.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

#: Reserved words after which bash still reads the NEXT word in command
#: position.  ``case`` is a reserved word ONLY in command position, so
#: ``if true; then case x in ...`` must arm the pattern rule while
#: ``echo case`` must not -- and the hand-through is INHERITED: ``then`` only
#: passes command position when it stands in command position itself
#: (``echo then case ...`` is three arguments).  Block ENDERS (``fi``,
#: ``done``, ``}``, ``esac``) are deliberately absent: bash refuses a keyword
#: directly after them (``fi case ...`` is a syntax error, measured), so not
#: arming there is exact.  Cross-pinned against
#: ``argv_floor._SHELL_RESERVED_WORDS`` by test, so the two keyword tables
#: cannot drift apart silently.  Defined here so the raw-text closer scan and the
#: walker read the same table (R28 GPT: ``time -p case`` is a case to both).
KEEPS_COMMAND_POSITION = frozenset(
    {"if", "then", "else", "elif", "while", "until", "do", "!", "{", "time", "coproc"}
)

#: Glued onto a token whose QUOTED spelling de-quotes to a walker grammar word.
QUOTED_WORD_MARK = "\x07"
#: Stands in for a single-quoted or escaped backtick.
QUOTED_TICK_MARK = "\x08"
#: Stands in for a QUOTED blank that is text of one word: ``case "x y" in "x y")``
#: names the WORD ``x y`` and the PATTERN ``x y``, and the tokenizer hands the walker
#: ``x y`` with no sign the blank was quoted, so the walker split it and lost the
#: compound (R22 GPT: the mint verb behind the closer minted).
QUOTED_BLANK_MARK = "\x0b"
#: Stand in for a QUOTED ``>`` / ``<``: text to bash, but the tokenizer drops the quote
#: and the argv walks read ``"a>b"`` as a redirection whose target swallows the next
#: word (``ssh -l "x 2>/dev/null; echo root" localhost`` was never host-checked, R23).
QUOTED_GT_MARK = "\x0c"
QUOTED_LT_MARK = "\x0e"
#: A quoted tab or newline is one word's text too (R25 GPT: ``case "x<TAB>y" in``).
QUOTED_TAB_MARK = "\x0f"
QUOTED_NL_MARK = "\x10"
#: What a quoted character is rewritten to, when it is rewritten at all.
_QUOTED_CHAR_MARKS = {
    " ": QUOTED_BLANK_MARK,
    "\t": QUOTED_TAB_MARK,
    "\n": QUOTED_NL_MARK,
    ">": QUOTED_GT_MARK,
    "<": QUOTED_LT_MARK,
}

_QUOTED_CHAR_TABLE = str.maketrans(_QUOTED_CHAR_MARKS)
#: The marks turned back into the text bash hands the program (the grammar-word mark
#: is dropped): every site that compares a token's text reads through this.
UNMARK_TABLE = str.maketrans(
    {QUOTED_WORD_MARK: None, QUOTED_TICK_MARK: "`", **{v: k for k, v in _QUOTED_CHAR_MARKS.items()}}
)
MARKS = frozenset(map(chr, UNMARK_TABLE))


#: Follows a value a resolver spliced into a token, until :func:`settle_splices` reads
#: the word it landed in.
SPLICE_MARK = "\x1d"


def settle_splices(text: str, grammar_words: "frozenset[str]", word_break: "frozenset[str]") -> str:
    """Drop every :data:`SPLICE_MARK` from *text*; a WORD (a run of characters outside
    *word_break*) that carried one and reads as a member of *grammar_words* gets
    :data:`QUOTED_WORD_MARK` appended.  Bash expands a variable AFTER parsing, so an
    expanded ``case`` -- whole (``$a``, R22 Opus) or completed by the text around the
    expansion (``${a}e``, R30 Opus) -- is an ordinary word, never grammar; any other
    spliced value (``${a}sh`` naming ``ssh``) is left exactly as spliced."""
    if SPLICE_MARK not in text:
        return text
    out: list[str] = []
    word: list[str] = []
    spliced = False

    def end_word() -> None:
        nonlocal spliced
        w = "".join(word)
        out.append(w + QUOTED_WORD_MARK if spliced and w in grammar_words else w)
        word.clear()
        spliced = False

    for ch in text:
        if ch == SPLICE_MARK:
            spliced = True
        elif ch in word_break:
            end_word()
            out.append(ch)
        else:
            word.append(ch)
    end_word()
    return "".join(out)


#: Every mark byte the readers glue into text: the quoted-text marks, the splice mark
#: and the walker's own substitution stand-in (``\x1f``, ``shell_normalizer._SUBST_MARK``).
_STRIP_TABLE = str.maketrans("", "", "".join(MARKS) + SPLICE_MARK + "\x1f")


def strip_marks(text: str) -> str:
    """*text* with every mark byte deleted: the marks are control characters no real
    command carries, and one typed into a command would otherwise be read back as the
    separator it stands for (``ssh -i /tmp/k\\x10 localhost``, R31 Opus).  The gate's
    entry runs this once, so a mark below can only be the tokenizer's own."""
    return text.translate(_STRIP_TABLE)


class BraceCloses:
    """Whether a ``${`` met at a given offset of an argv can close: bash refuses a ``${``
    inside ``$( … )`` that is left open or whose ``}`` is not followed by the
    substitution's ``)`` (measured), so one without that shape ahead was quoted text.
    Read per OCCURRENCE from the text ahead of it, paren-aware: the ``)`` after the
    ``}`` must be UNMATCHED (``'${' ) localhost '}' $(:)`` has none, R31 Opus), while
    a ``)`` inside the expansion is its text (``${v:-x)}``).  One pass over the joined
    argv, then a bisect per ``${``."""

    __slots__ = ("_starts", "_brace_at", "_closes")
    #: The last argv built over, and its reader: the argv floors build one walker per
    #: program anchor over the SUFFIX from that anchor, so a suffix of the last argv reads
    #: from the same tables at an offset instead of joining the text again (2,200 anchors
    #: joined 2,200 suffixes: a 27 s stall, R32 GPT).
    _last: "tuple[list[str], BraceCloses] | None" = None

    @classmethod
    def for_suffix(cls, rest: "list[str]") -> "tuple[BraceCloses, int]":
        """The reader for *rest* and the token index *rest* starts at in it."""
        last = cls._last
        if last is not None:
            tokens, built = last
            k = len(tokens) - len(rest)
            if k >= 0 and tokens[k:] == rest:
                return built, k
        built = cls(rest)
        cls._last = (list(rest), built)
        return built, 0

    def __init__(self, tokens: "list[str]") -> None:
        joined = " ".join(tokens)
        self._starts: list[int] = []
        at = 0
        for token in tokens:
            self._starts.append(at)
            at += len(token) + 1
        # unmatched[k]: an unmatched ``)`` stands at or after offset k
        n = len(joined)
        unmatched = [False] * (n + 1)
        match: dict[int, int] = {}
        stack: list[int] = []
        for k, ch in enumerate(joined):
            if ch == "(":
                stack.append(k)
            elif ch == ")" and stack:
                match[stack.pop()] = k
        k = n - 1
        while k >= 0:
            ch = joined[k]
            if ch == ")":
                unmatched[k] = True
            elif ch == "(":
                unmatched[k] = unmatched[match[k] + 1] if k in match else False
            else:
                unmatched[k] = unmatched[k + 1]
            k -= 1
        self._brace_at = [k for k, ch in enumerate(joined) if ch == "}"]
        self._closes = [unmatched[k + 1] for k in self._brace_at]

    def at(self, token_index: int, offset: int) -> bool:
        """For the ``${`` at *offset* of the token at *token_index*."""
        if token_index >= len(self._starts):
            return True
        pos = self._starts[token_index] + offset + 2
        lo, hi = 0, len(self._brace_at)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._brace_at[mid] < pos:
                lo = mid + 1
            else:
                hi = mid
        return lo < len(self._brace_at) and self._closes[lo]


#: An unquoted ``#`` that STARTS a word opens a comment: bash's parser never sees the
#: rest of that line, so a ``)`` or ``;`` inside it closes and separates nothing.  A
#: ``)`` is not in the set: the one closing a substitution ends no word (``$(:)# x``
#: hands ``#`` and ``x`` to the command, measured), and the one closing a subshell is
#: read as the walker reads it -- the deny direction.  The one closing a ``case``
#: PATTERN is an operator, so a ``#`` right behind it IS a comment (``case x in x)# )``
#: + newline drops the ``)``, measured -- R46 GPT): the scan tracks the case grammar
#: (``case`` WORD ``in`` PATTERN ``)`` body ``;;`` … ``esac``) to tell that closer from
#: the other two.  A backtick is a word start only when it OPENS a substitution; the
#: closing one ends no word either (``\`true\`#x; …`` runs the second command,
#: measured -- R38 GPT), so the scan keeps the backtick state.
_COMMENT_WORD_START = frozenset(" \t\n;|&(<>")
_COMMENT_WORD_END = frozenset(" \t\n;|&()<>`")


def strip_comments(steps: "Iterable[Any]") -> str:
    """Join the tokenizer's quote-state *steps* (``.text``, ``.char``, ``.active``) with
    every unquoted comment dropped up to its newline, and every ACTIVE newline rewritten
    to a standalone ``;`` (shlex reads a bare newline as a blank).  Done on the raw text,
    where the newline still stands: once tokenized, a ``;`` typed inside the comment is
    indistinguishable from the newline sentinel (``$(echo # ; )`` + newline + ``:)``,
    R37 GPT), and a commented ``)`` closed the substitution (R35 GPT)."""
    out: list[str] = []
    comment = False
    word_start = True
    in_tick = False
    word: list[str] = []  # the unquoted word being read; a quoted char spoils it
    command_position = True
    cases: list[str] = []  # open ``case`` compounds: "word", "pattern" or "body"
    prev = ""  # the previous ACTIVE char, for ``;;`` / ``;&``
    for step in steps:
        active, ch = step.active, step.char
        if comment:
            if not (active and ch == "\n"):
                continue
            comment = False
        if active and ch == "#" and word_start:
            comment = True
            continue
        out.append(" ; " if active and ch == "\n" else step.text)
        if not active:
            word.append("\x00")  # quoted text: ``"case"`` is an argument, not grammar
            word_start = False
            continue
        if ch in _COMMENT_WORD_END:
            w = "".join(word)
            word.clear()
            if w:
                if cases and cases[-1] == "word":
                    cases[-1] = "pattern" if w == "in" else "word"
                elif cases and cases[-1] == "pattern":
                    if w == "esac":
                        cases.pop()
                elif command_position and w == "case":
                    cases.append("word")
                elif command_position and w == "esac" and cases:
                    cases.pop()
                else:
                    command_position = command_position and (
                        w in KEEPS_COMMAND_POSITION or w.startswith("-")
                    )
            if ch in ";|&\n()":
                command_position = True  # a separator or a paren opens one (as the walker reads)
                if cases and cases[-1] == "body" and prev == ";" and ch in ";&":
                    cases[-1] = "pattern"  # ``;;`` / ``;&`` end the clause
        else:
            word.append(ch)
        if ch == "`":
            in_tick = not in_tick
            word_start = in_tick  # the opener starts a command list; the closer ends a word
        elif ch == ")" and cases and cases[-1] == "pattern":
            cases[-1] = "body"  # a PATTERN's closer: an operator, so ``#`` may follow
            command_position = True
            word_start = True
        else:
            word_start = ch in _COMMENT_WORD_START
        prev = ch
    return "".join(out)


# A ``case … esac`` command substitution whose clauses each print one literal
# is a static ALTERNATION: bash emits exactly one clause's word (or nothing, when
# no pattern matches).  The body class excludes ``$`` and the backtick, so a scan
# started at one opener stops at the next and the pass stays linear; a nested
# substitution therefore never resolves and stays the run-time residual.
CASE_SUBSTITUTION_RE = re.compile(
    r"\$\(\s*case\s+\S+\s+in\s+([^$`]*?)\s*esac\s*\)|`\s*case\s+\S+\s+in\s+([^$`]*?)\s*esac\s*`"
)


def case_clause_bodies(clauses: str) -> "list[str] | None":
    """The command text of every ``PATTERN) body`` clause in *clauses*, or None.

    None when any clause is not that shape: a ``;&``/``;;&`` fall-through (two
    clauses run), a pattern carrying its own ``)``, or no ``)`` at all.  The
    caller decides what each body prints; this pass only cuts the compound into
    its branches, on the raw text, the way the quote and comment passes do.
    """
    if ";&" in clauses:
        return None
    bodies = []
    for clause in clauses.split(";;"):
        if not clause.strip():
            continue
        pattern, closer, body = clause.partition(")")
        if not closer or ")" in body or "(" in pattern.replace("(", "", 1):
            return None
        body = body.strip()
        while body.startswith("#"):  # behind a PATTERN's ``)`` a ``#`` is a comment (R46 GPT)
            body = body.partition("\n")[2].strip()
        bodies.append(body.lstrip(";").strip())  # the newline may already read `` ; ``
    return bodies


def splice_case_alternations(
    text: str, output_of: "Callable[[str], str]", names_self: "Callable[[str], bool]"
) -> str:
    """Splice each ``case`` substitution's self-naming branch into *text*.

    *output_of* gives a clause body's static output (``"\\x00"`` when it has
    none); a compound with a dynamic branch, or none whose output *names_self*,
    is left as written -- the documented run-time residual -- so every splice
    only widens the deny, exactly as brace expansion does.
    """

    def _splice(match: "re.Match[str]") -> str:
        bodies = case_clause_bodies(match.group(1) or match.group(2) or "")
        outs = [output_of(body) for body in bodies or ()]
        if bodies is None or "\x00" in outs:
            return match.group(0)
        return next((out for out in outs if names_self(out)), match.group(0))

    return CASE_SUBSTITUTION_RE.sub(_splice, text)


def unmark(text: str) -> str:
    """*text* with the quoted-text marks turned back into the characters they stand for."""
    return text.translate(UNMARK_TABLE)


def _substitution_closer(text: str, start: int, closer: str) -> int:
    """Index of the ``)`` (or backtick) that closes the substitution opened before
    *start*, or -1.  A quote-aware scan: quoting restarted at *start*, so an inner
    ``"…"`` or ``'…'`` is a real quote (``"$(dirname "$0")/a"``, R25 Opus) and a ``)``
    inside it or inside a nested ``(`` is not the closer.  A ``)`` that closes a
    ``case`` PATTERN is not the closer either; the pass cannot parse that grammar, so
    a candidate is skipped while more ``case`` than ``esac`` words stand in COMMAND
    position so far (``"$(case y in y) :;; esac)"`` runs to its last ``)``; an
    argument spelled ``case`` counts for nothing, R26 GPT).  Command position is
    read as the walker reads it: a separator or a pattern's ``)`` opens it, and a
    keeper (:data:`KEEPS_COMMAND_POSITION`) or an option word holds it only when it
    holds it itself (``time -p case x in x)`` is a case, R28 GPT), and the NAME after
    ``function``/``coproc`` hands it on (``function f case x in x)``, R29 GPT).  When
    that reading finds no closer, the plain paren reading is taken instead.
    """
    return _closer_probe(text, start, closer, case_aware=True)[0]


def _closer_probe(text: str, start: int, closer: str, *, case_aware: bool) -> "tuple[int, bool]":
    """:func:`_substitution_closer`'s reading, plus whether the case-aware scan read
    to the end of the text without a closer.  Once it has, no later opener's can stop
    before the end either (a ``case`` left open is one bash refuses the line for), so
    the caller reads every later opener with the plain paren reading alone: probing a
    run of ``$(case x)`` case-aware per opener was quadratic (R30 Opus)."""
    if case_aware:
        found = _closer_scan(text, start, closer, case_aware=True)
        if found >= 0:
            return found, False
    return _closer_scan(text, start, closer, case_aware=False), case_aware


def _closer_scan(text: str, start: int, closer: str, *, case_aware: bool) -> int:
    n = len(text)
    depth = 0
    quote: str | None = None
    cases = esacs = 0
    word: list[str] = []
    command_position = True  # the word being read starts a command
    name_next = False  # the word being read is the NAME after ``function``/``coproc``
    ansi = False  # the open single quote is ``$'…'``: a backslash escapes there too
    k = start
    while k < n:
        ch = text[k]
        if quote is not None:
            if ch == quote:
                quote = None
            elif ch == "\\" and (quote == '"' or ansi) and k + 1 < n:
                k += 1
            k += 1
            continue
        if ch in " \t\n;|&()<>`":
            w = "".join(word)
            word.clear()
            if w and name_next:
                # ``function f case …``: the name is read, and a compound follows
                # in command position (R29 GPT), as the walker reads it.
                name_next = False
                command_position = True
            elif w:
                if command_position:
                    cases += w == "case"
                    esacs += w == "esac"
                name_next = command_position and w in ("function", "coproc")
                command_position = command_position and (
                    w in KEEPS_COMMAND_POSITION or w.startswith("-")
                )
            if ch in ";|&\n()":
                command_position = True  # a separator or a pattern's ``)`` opens one
                name_next = False
        if ch == "\\" and k + 1 < n:
            k += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            ansi = ch == "'" and k > 0 and text[k - 1] == "$"  # the main pass reads it so
        elif closer == "`" and ch == "`":
            return k
        elif ch == "(":
            depth += 1
        elif ch == ")" and closer == ")":
            if depth:
                depth -= 1
            elif not case_aware or cases <= esacs:
                return k
        elif ch not in " \t\n;|&<>`":
            word.append(ch)
        k += 1
    return -1


def mask_quoted_reserved_words(
    text: str,
    grammar_words: "frozenset[str]",
    word_break: "frozenset[str]",
    decode_ansi_c: "Callable[[str], str]",
) -> str:
    """Mark quoted grammar words and quoted backticks in the raw *text*.

    A word runs to the next UNQUOTED character of *word_break*; its de-quoted text
    is what bash would hand the command, and ``quoted`` records whether any character
    of it was inside quotes or behind a backslash.  A word that de-quotes to a member
    of *grammar_words* with ``quoted`` set gets :data:`QUOTED_WORD_MARK` appended after
    its last character: the tokenizer glues adjacent text into one token, so
    ``"case"`` arrives as ``case`` + mark and the walker reads an argument.

    Quote rules are bash's: a single quote takes no escapes; inside double quotes a
    backslash escapes only a double quote, a backslash, ``$`` or a backtick, and a
    backtick there still substitutes; outside quotes a backslash escapes the next
    character (a backslash-newline is a line continuation, neither text nor a quote).
    ``$'…'``/``$"…"`` open a quote with the ``$`` belonging to the opener, not to the
    word's text; the body of ``$'…'`` is compared DECODED (*decode_ansi_c*, the
    tokenizer's own decoder), since ``$'\\x63ase'`` is the word ``case`` to bash and to
    the tokenizer alike.  The function is the identity on every command with neither
    a quoted grammar word nor a single-quoted or escaped backtick.

    A blank inside quotes becomes :data:`QUOTED_BLANK_MARK` (text of one word); a
    substitution met inside double quotes restarts quoting, so ITS blanks stay the
    separators of that command list (``"$(case y in y) :;; esac)"`` is one token).
    """
    out: list[str] = []
    word: list[str] = []  # de-quoted text of the word being read
    quoted = False
    quote: str | None = None
    ansi = False  # the open quote is ``$'…'``: its body decodes
    # Quoting RESTARTS inside a command substitution: in ``"$('case' x in y)"`` the
    # single quotes are real quotes to bash (R23 GPT), so a ``$(`` or backtick met
    # inside double quotes pushes the enclosing quote here and reads its body unquoted;
    # the substitution's own closer (a ``)`` at its paren depth, or the backtick) pops it.
    # (enclosing quote, index of the closer that ends the restarted substitution): the
    # raw pass cannot parse the case grammar whose ``)`` is not a closer, so a ``$(``
    # runs to the LAST ``)`` before the enclosing quote closes; a backtick to the next.
    enclosing: list[tuple[str, int]] = []
    # An opener inside double quotes with NO closer ahead is one bash refuses the whole
    # line for, so every later opener is text as well and is never probed: probing each
    # of a run of ``$(`` to the end of the text is quadratic in the command's length,
    # and a 20 KB run held the synchronous gate for minutes (R27 Opus).
    refused = False
    case_aware = True  # off once a case-aware probe has read to the end (R30 Opus)
    i = 0
    n = len(text)

    def end_word() -> None:
        nonlocal quoted
        if quoted and "".join(word) in grammar_words:
            out.append(QUOTED_WORD_MARK)
        word.clear()
        quoted = False

    while i < n:
        ch = text[i]
        if quote is None:
            if enclosing and i >= enclosing[-1][1]:
                # ``>=``: a body scan that stepped past the recorded closer (an ANSI-C
                # body read as one span) must still pop the enclosing quote (R32 Opus).
                # The restarted substitution closes: back inside the enclosing quote.
                end_word()
                out.append(ch)
                i += 1
                quote = enclosing.pop()[0]
                continue
            if ch in word_break:
                end_word()
                out.append(ch)
                i += 1
                continue
            if ch == "\\" and i + 1 < n:
                nxt = text[i + 1]
                out.append(ch)
                if nxt == "\n":
                    out.append(nxt)  # a line continuation: neither text nor a quote
                else:
                    out.append(QUOTED_TICK_MARK if nxt == "`" else _QUOTED_CHAR_MARKS.get(nxt, nxt))
                if nxt != "\n":
                    word.append(nxt)
                    quoted = True
                i += 2
                continue
            if ch == "$" and i + 1 < n and text[i + 1] in ("'", '"'):
                out.append(ch)  # ANSI-C / locale quoting: the ``$`` is the opener's
                i += 1
                ch = text[i]
                ansi = ch == "'"
            if ch in ("'", '"'):
                quote = ch
                quoted = True
                out.append(ch)
                i += 1
                if ansi:
                    # ``$'…'``: copy the raw body through (marking a backtick inside
                    # it, text to bash), and add the DECODED body to the word.
                    j = i
                    while j < n and text[j] != "'":
                        j += 2 if text[j] == "\\" and j + 1 < n else 1
                    decoded = decode_ansi_c(text[i:j])
                    # Emit the DECODED body re-escaped for the tokenizer's own decoder,
                    # with every backtick it decodes to marked: ``$'\\x60'`` is a
                    # backtick to bash and text, not a substitution (R21 GPT).
                    out.append(
                        decoded.replace("\\", "\\\\")
                        .replace("'", "\\'")
                        .replace("`", QUOTED_TICK_MARK)
                        .translate(_QUOTED_CHAR_TABLE)
                    )
                    word.extend(decoded)
                    i = j
                    quote = None if i >= n else quote
                    ansi = False
                    if i < n:
                        out.append(text[i])  # the closing quote
                        i += 1
                        quote = None
                continue
            word.append(ch)
            out.append(ch)
            i += 1
            continue
        if ch == quote:
            quote = None
            out.append(ch)
            i += 1
            continue
        if quote == '"' and not refused and (text.startswith("$(", i) or ch == "`"):
            # A substitution opens inside double quotes: its body is read UNQUOTED up
            # to its closer, then the quote resumes.
            end_word()
            j = i + (2 if ch == "$" else 1)
            closer, exhausted = _closer_probe(
                text, j, ")" if ch == "$" else "`", case_aware=case_aware
            )
            case_aware = case_aware and not exhausted
            if closer < 0:
                # No closer ahead: bash would refuse the line.  Read the opener as text
                # so the enclosing quote still closes where it does (R25 Opus), and
                # every later opener as text without a probe (R27 Opus).
                refused = True
                word.extend(text[i:j])
                out.append(text[i:j])
                i = j
                continue
            out.append(text[i:j])
            enclosing.append((quote, closer))
            quote = None
            i = j
            continue
        if quote == '"' and ch == "\\" and i + 1 < n and text[i + 1] in '"\\$`':
            out.append(ch)
            out.append(
                QUOTED_TICK_MARK if text[i + 1] == "`" else text[i + 1]
            )  # an escaped one is text
            word.append(text[i + 1])
            i += 2
            continue
        word.append(ch)
        if ch == "`" and quote == "'":
            out.append(QUOTED_TICK_MARK)
        else:
            out.append(_QUOTED_CHAR_MARKS.get(ch, ch))
        i += 1
    end_word()
    return "".join(out)
