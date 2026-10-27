"""The case-aware argv window walker of the shell text reader.

:class:`_SubstitutionDepth` reads the self-protection tokenizer's de-quoted tokens as
a stack of command lists, so a separator inside a command substitution, a ``case``
PATTERN's ``)`` and a substitution's own ``)`` are told apart when an argv floor
bounds the window it reads. The quoted-text marks it reads live in ``quoted_marks``.

Imports only ``quoted_marks`` and ``shell_normalizer_escapes``; ``shell_normalizer``
re-exports every name here, and nothing here imports it back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import quoted_marks as _quoted_marks
from .shell_normalizer_escapes import _decode_ansi_c_body

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Reserved words after which bash still reads the NEXT word in command position;
#: the table lives in ``quoted_marks`` (see its comment there), which the raw-text
#: closer scan reads too, so the two readings of command position cannot drift.
_KEEPS_COMMAND_POSITION = _quoted_marks.KEEPS_COMMAND_POSITION

#: The reading position of one :class:`_Frame`.  ARG: ordinary words of a command
#: list.  WORD: the word after ``case``.  IN: ``in`` is the next word.  PATTERN: the
#: tokens up to the pattern's unbalanced ``)``.
_ARG, _WORD, _IN, _PATTERN = 0, 1, 2, 3
#: A char that ends a word in ARG position.  A token boundary is one too, and so is
#: a blank INSIDE a token when a substitution is open: the tokenizer split on
#: unquoted blanks, so one that survived was quoted -- and inside ``"$( … )"`` it is
#: the substitution's own word separator (``"$(case y in y) :;; esac)"`` arrives as
#: ONE token).  At TOP level a quoted blank is text of the one word bash hands over.
_WORD_BREAK = frozenset(";|&()<>`\n \t")
#: Stands in for a command substitution inside the word being read, so the text on
#: either side of ``$( … )`` stays ONE word (``$(x)case`` is not the reserved word)
#: and the caller can still read the top-level text around it (``$( … )token``).
_SUBST_MARK = "\x1f"
#: The quoted-grammar-word and quoted-backtick marks, and why (see ``quoted_marks``).
_QUOTED_WORD_MARK, _QUOTED_TICK_MARK = (
    _quoted_marks.QUOTED_WORD_MARK,
    _quoted_marks.QUOTED_TICK_MARK,
)
_QUOTED_BLANK_MARK, _QUOTED_GT_MARK = _quoted_marks.QUOTED_BLANK_MARK, _quoted_marks.QUOTED_GT_MARK
_QUOTED_LT_MARK, _QUOTED_TAB_MARK = _quoted_marks.QUOTED_LT_MARK, _quoted_marks.QUOTED_TAB_MARK
_QUOTED_NL_MARK = _quoted_marks.QUOTED_NL_MARK
#: After a fresh pattern's ``esac``, one of these (or the token's end) makes it the reserved
#: word; ``|``, ``(``, a word character or a QUOTED blank (``'esac '`` is a pattern) do not.
_ESAC_FOLLOWERS = frozenset(";)&`<>\n \t")
_UNMARK_TABLE, _MARKS, _unmark = (
    _quoted_marks.UNMARK_TABLE,
    _quoted_marks.MARKS,
    _quoted_marks.unmark,
)


#: The walker's structure characters: a token with none is one plain word (fast path).
_SCAN_CHARS = frozenset("$(){}[]`;|&<>#\n \t") | {_SUBST_MARK}


class _Frame:
    """One command list: the argv window itself, or the inside of a ``$( … )`` /
    ``<( … )`` / backtick substitution opened inside it.

    A substitution is a whole command list, so a ``case`` compound INSIDE one is
    read by its own frame -- its pattern ``)`` never closes the substitution, and
    the enclosing frame resumes in the position it left (an argument, a ``case``
    WORD or a PATTERN) once the substitution's own ``)`` pops the frame.
    """

    __slots__ = (
        "kind", "cases", "mode", "command_next", "name_next", "groups", "fresh",
        "pattern_chars", "word",
    )  # fmt: skip

    def __init__(self, kind: str, command_position: bool) -> None:
        self.kind = kind  # "root", "sub" (``$(`` / ``<(`` / ``>(``) or "tick" (backtick)
        self.cases = 0  # ``case`` compounds open in this list
        self.mode = _ARG
        self.command_next = command_position  # the next word is in command position
        self.name_next = False  # the next word is a ``function``/``coproc`` NAME
        self.groups: list[str] = []  # open text groups: "brace" ``${``, "paren", "bracket"
        self.fresh = False  # PATTERN: no pattern character consumed yet
        self.pattern_chars = 0
        self.word: list[str] = []  # the word being read, across tokens

    def arm_pattern(self) -> None:
        self.mode = _PATTERN
        self.fresh = True
        self.pattern_chars = 0
        self.groups = []
        self.command_next = True  # the clause body opens after the pattern's ``)``

    def copy(self) -> "_Frame":
        frame = _Frame(self.kind, self.command_next)
        frame.cases = self.cases
        frame.mode = self.mode
        frame.name_next = self.name_next
        frame.groups = list(self.groups)
        frame.fresh = self.fresh
        frame.pattern_chars = self.pattern_chars
        frame.word = list(self.word)
        return frame


class _SubstitutionDepth:
    """Stateful successor to :func:`_substitution_depth_delta` for argv windows.

    An argv window stops at the first token that ends the argv while NO command
    substitution is open.  The bare counter cannot judge a ``case`` compound: its
    grammar reuses ``)`` to close a PATTERN, so ``x)`` scored as a closer, ``x|y)``
    as a separator, and every clause after ``esac`` fell out of the window
    (``kill $(case x in x) :;; esac; pgrep -f <name>)`` -- measured).

    The walker reads the de-quoted tokens character by character as a STACK of
    :class:`_Frame` command lists: ``$(``, ``<(``, ``>(`` and an opening backtick
    push a frame, the frame's own unbalanced ``)`` (closing backtick) pops it.
    Within a frame ``case … in … esac`` is tracked the way bash reads it: after
    ``in`` and after ``;;`` / ``;&`` / ``;;&`` the tokens up to the unbalanced ``)``
    are the pattern; ``esac`` leaves the innermost compound; nesting is a counter.
    ``case``/``esac`` are reserved words only in COMMAND POSITION -- after a
    separator, an opener, a keeper (:data:`_KEEPS_COMMAND_POSITION`,
    ``function``/``coproc`` + name), a pattern's ``)``, or through an option word;
    never right after an ordinary verb, an assignment or a redirect (bash refuses
    ``>/dev/null case``, measured).  A window's first token is an ARGUMENT unless
    the caller says otherwise.

    Two readings the de-quoted stream cannot make are settled in the DENY
    direction (a misread only ever KEEPS a window open): a ``;;`` where no case is
    open re-opens the compound an earlier quoted ``'esac'`` seemed to close, and a
    ``${`` with no ``}`` + ``)`` ahead is a quoted literal.  The quoted-paren
    limit stands (see :func:`_bare_kill_raw_bodies`).

    ``esac)`` in PATTERN position has two readings: the reserved word closing the
    compound, glued to the ``)`` closing the substitution (``$(case x in x) :;;
    esac)``), or a quoted ``'esac')`` PATTERN whose clause body follows (R12:
    ``$(case x in 'esac') echo hi; :;; esac; :)`` -- the body's ``;`` read as the
    window's end and the verb behind the real closer was never read).  The rest of
    the argv (*rest*) settles it: under the first reading the tokens after the
    closer are top-level text, and bash refuses a ``)`` that closes nothing, a ``;;``
    with no case open and an ``esac`` in command position there -- so a lookahead
    that reads on under the first reading and meets one of those proves the second.
    One lookahead covers every ambiguity up to the event it found (linear overall);
    without *rest* the closer reading stands.

    After each :meth:`feed`, :attr:`words` lists the TOP-LEVEL argument words the
    token completed, with every command substitution cut out (the tokenizer's
    quoted-text marks are kept -- a consumer reads them through ``_unmark`` or
    ``_normalize_operand``, and the ssh floor needs to know a backtick was quoted):
    bash hands
    ``$(case x in x) :;; esac)token`` to the command as ONE word made of the
    substitution's output and ``token``, so a caller that classified only the
    whole glued token (``esac)token``) let the verb through (R11).  A word inside
    a substitution is that command's argv and is never listed.
    """

    __slots__ = (
        "_frames", "_brace_closes", "_after_esac", "_esac_fresh", "_esac_closer", "_data_floor",
        "_rest", "_fed", "_speculating", "_root_event", "_lookahead", "_read", "words",
        "alt_words", "_alt_pending", "_comment",
    )  # fmt: skip

    def __init__(self, command_position: bool = False, rest: "Iterable[str] | None" = None) -> None:
        # An unquoted ``${`` opened inside ``$( … )`` needs a ``}`` AND the substitution's
        # ``)`` after it in *rest*; one without that shape ahead of IT was quoted text
        # (``'sync-${ENV'``, ``'${' x); awk '{print}'``; see ``quoted_marks.BraceCloses``).
        self._rest = None if rest is None else list(rest)
        self._brace_closes: "tuple[_quoted_marks.BraceCloses, int] | None" = (
            None  # on the first ``${``
        )
        self._fed = 0  # index in *rest* of the token being fed
        # A lookahead walker: reads on past window ends and records a ROOT event
        # (a ``)`` closing nothing, a ``;;`` with no case, ``esac`` in command position)
        # instead of ending; never looks ahead itself.
        self._speculating = False
        self._root_event = False
        # The last lookahead: (position asked, event position | None, its offset | None,
        # the offset it started at, the closer reading's words up to the event).
        self._lookahead: "tuple[int, int | None, int | None, int, list[str]] | None" = None
        self._read = 0  # chars of the current token read so far (a speculation's offset)
        # The CLOSER reading's words behind an ``esac)`` the lookahead settled as a
        # pattern: an ENCLOSING clause's ``;;`` fools it (R22 Opus), so consumers read
        # these too -- the deny direction.
        self.alt_words: "list[str]" = []
        self._alt_pending = False  # the closer reading's word is the NEXT token
        # In a substitution a ``#`` word opens a comment to the newline (the tokenizer's
        # standalone ``;``): a ``)`` in it closes nothing (R35 GPT).
        self._comment = False
        self._frames = [_Frame("root", command_position)]
        # The last word was a reserved ``esac``; the last ``)`` popped a frame right
        # behind one (a ``;;`` next: that ``esac`` was a QUOTED pattern, frame open);
        # and that ``esac`` stood in PATTERN position (see the class docstring).
        self._after_esac = self._esac_closer = self._esac_fresh = False
        # Reading a DATA token: the frame count when it began (0 otherwise).  Its
        # top-level separators are text; a ``)`` closes only a frame the token opened.
        self._data_floor = 0
        self.words: list[str] = []

    @property
    def depth(self) -> int:
        """Command substitutions currently open."""
        return len(self._frames) - 1

    @property
    def top_level(self) -> bool:
        """True while no command substitution is open."""
        return len(self._frames) == 1

    @property
    def grammar_next(self) -> bool:
        """True while the next token is ``case`` grammar -- the WORD, ``in`` or a PATTERN --
        which bash never hands to the command as an operand (``*)`` is not a host)."""
        return self._frames[-1].mode != _ARG

    def feed_data(self, token: str) -> bool:
        """A token the caller skips as DATA (a redirection, its target, an inline
        program): never ends the argv and lists no word -- its separator characters
        were quoted text (``> 'a;b'``) -- but a command substitution it opens is
        still a command list this window reads through (``2>$(case … esac; echo
        /dev/null) restart`` left ``restart`` outside the window, R11).  Unless a
        ``case`` WORD or pattern is pending: bash reads the token as that word
        anyway (``case $1 in -c)``)."""
        if self.grammar_next:
            return self.feed(token)
        self.words = []
        self._data_floor = len(self._frames)
        try:
            self._scan(token)
        finally:
            self._data_floor = 0
            self._fed += 1
        return False

    def feed(self, token: str) -> bool:
        """Account for *token*; True if it ends the argv at top level.  One pass over
        the characters, whatever is glued together: no recursion, no re-reading."""
        self.words = []
        self.alt_words = []
        if self._alt_pending:
            self._alt_pending = False
            if len(self._frames) == 2 and token and _SCAN_CHARS.isdisjoint(token):
                self.alt_words.append(token)  # ``esac) <host>``: the closer reading
        frame = self._frames[-1]
        if (
            token
            and len(self._frames) == 1
            and frame.mode == _ARG
            and not frame.word
            and not frame.groups
            and not self._speculating
            and _SCAN_CHARS.isdisjoint(token)
        ):
            # One plain top-level word cannot end the argv: read it whole (R18 GPT).
            self._fed += 1
            self._after_esac = False
            if frame.command_next or frame.name_next or self._data_floor:
                frame.word.append(token)
                self._flush(frame)  # it may be ``case``, a keeper or a function name
                return False
            if token.replace(_QUOTED_WORD_MARK, ""):
                self.words.append(token)  # marks kept: the consumer unmarks (R26)
            return False
        try:
            ended = self._scan(token)
        finally:
            self._fed += 1
        frame = self._frames[-1]
        if ended or len(self._frames) > 1 or frame.mode != _ARG:
            return ended
        # The token-level boundaries of :func:`_ends_argv` that are not separator
        # characters: an EMPTY word and a function-body opener.
        bare = token.rstrip("{")
        return bare in {"", "("} or bare.endswith("()")

    # -- the character walk ---------------------------------------------------

    def _push(self, kind: str) -> None:
        parent = self._frames[-1]
        if parent.mode == _PATTERN:
            parent.fresh = False
            parent.pattern_chars += 2
        else:
            parent.word.append(_SUBST_MARK)  # the word goes on after the substitution
        self._after_esac = self._esac_closer = False
        self._frames.append(_Frame(kind, True))

    def _pop(self) -> None:
        if len(self._frames) > 1:
            self._frames.pop()

    def _scan(self, token: str) -> bool:
        if self._comment:
            self._comment = token != ";"  # the newline sentinel ends the comment
            return False
        i, n = 0, len(token)
        while i < n:
            if self._speculating:
                if self._root_event:
                    # A lookahead has its answer at the first root event: reading on would
                    # cost the rest of the token for nothing, and a glued run of ``esac);;``
                    # asks once per clause (R19 Opus: 2,900 in one token, quadratic).
                    return False
                self._read = i
            frame = self._frames[-1]
            ch = token[i]
            if frame.groups and frame.groups[-1] == "brace":
                # Inside ``${ … }``: text, except a substitution it opens and its ``}``.
                if token.startswith(("$(", "<(", ">("), i):
                    self._push("sub")
                    i += 2
                elif token.startswith("${", i):
                    frame.groups.append("brace")
                    i += 2
                elif ch == "`":
                    self._push("tick")
                    i += 1
                else:
                    if ch == "}":
                        frame.groups.pop()
                    i += 1
                continue
            if frame.mode == _PATTERN:
                i = self._pattern_char(frame, token, i)
                continue
            if token.startswith(("$(", "<(", ">("), i):
                self._push("sub")
                i += 2
            elif token.startswith("${", i):
                if self._brace_closes is None and self._rest is not None:
                    self._brace_closes = _quoted_marks.BraceCloses.for_suffix(self._rest)
                if self._brace_closes is None or self._brace_closes[0].at(
                    self._brace_closes[1] + self._fed, i
                ):
                    frame.groups.append("brace")
                frame.word.append("$")  # ``${x}case`` and a quoted ``'${'`` are words
                i += 2
            elif ch in " \t" and len(self._frames) == 1 and not self._data_floor:
                # A blank inside a token was QUOTED: at top level it is text of ONE argv
                # word (``'psql -h localhost'`` is one ssh word, R17); inside a
                # substitution it is that command list's own separator (branch below).
                frame.word.append(ch)
                i += 1
            elif ch not in _WORD_BREAK:
                # A comment is a ``#`` that STARTS the token (``_ends_argv``): a blank
                # inside a de-quoted token was quoted, so the ``#`` behind it is data
                # (``>'a #' <verb>`` minted on R12).
                if ch == "#" and i == 0 and frame.mode == _ARG and not self._data_floor:
                    if len(self._frames) == 1 and not self._speculating:
                        return True  # a comment: everything after it is prose
                    if len(self._frames) > 1:
                        self._comment = True  # ...to the newline, inside a substitution
                        return False
                frame.word.append(ch)
                i += 1
            elif ch == "&" and ((i and token[i - 1] in "<>") or token.startswith("&>", i)):
                i += 1  # ``2>&1``, ``&>x``: a redirection, not a separator
            elif self._data_floor and len(self._frames) == 1 and ch in ";|&\n()":
                frame.word.append(ch)  # a DATA token's top-level operator was quoted text
                i += 1
            elif self._flush(frame) and frame.mode == _PATTERN:
                continue  # ``in(x)``: the word armed a pattern the ``(`` belongs to
            elif ch in " \t":
                i += 1
            elif ch == "`":
                if frame.kind != "tick":
                    self._push("tick")
                elif len(self._frames) > self._data_floor:
                    self._pop()
                i += 1
            elif ch == ")":
                if frame.groups:  # a "paren" (subshell / function parens)
                    frame.groups.pop()
                elif frame.kind == "sub" and len(self._frames) > self._data_floor:
                    if (
                        self._after_esac
                        and self._esac_fresh
                        and self._quoted_esac_pattern(token, i + 1)
                    ):
                        # ``'esac')``: the pattern, so the compound and the frame
                        # stay open and the clause body opens here.
                        frame.cases += 1
                        frame.command_next = True
                        self._after_esac = False
                        if len(self._frames) == 2 and not self._speculating:
                            hit = self._lookahead
                            if (
                                hit is not None
                                and hit[0] == self._fed
                                and hit[3] == i + 1
                                and hit[4]
                            ):
                                # ALL the closer reading's words to the refusing event:
                                # an enclosing clause's ``;;`` makes it bash's (R28 Opus).
                                self.alt_words.extend(hit[4])
                            else:
                                j = i + 1
                                while j < n and token[j] not in _WORD_BREAK:
                                    j += 1
                                glued = token[i + 1 : j].replace(_SUBST_MARK, "")
                                if glued:
                                    self.alt_words.append(glued)  # the closer reading's word
                                else:
                                    self._alt_pending = j >= n  # ...or the next token is
                    else:
                        self._pop()
                        self._esac_closer = self._after_esac
                elif len(self._frames) == 1:
                    self._root_event = True  # a ``)`` closing nothing: not valid bash
                # A ``)`` inside backticks or at top level closes nothing.
                i += 1
            elif ch == "(":
                frame.groups.append("paren")
                frame.command_next = True  # a subshell opens a command list
                i += 1
            elif ch in "<>":
                i += 1
            else:
                if ch == ";" and token.startswith((";;", ";&"), i):
                    end = 3 if token.startswith(";;&", i) else 2
                    if self._clause_end(frame) and not self._speculating:
                        return True
                    i += end
                    continue
                if ch in "&|" and token.startswith(ch, i + 1):
                    i += 1  # ``&&`` / ``||``
                elif ch == "|" and token.startswith("|&", i):
                    i += 1
                i += 1
                if self._separator(frame, ch, token) and not self._speculating:
                    return True
        frame = self._frames[-1]
        self._flush(frame)
        if len(self._frames) == 1 and frame.mode == _ARG and "brace" in frame.groups:
            # At top level an open ``${`` is the counter's reading: its ``)`` can close
            # nothing there, and a quoted ``'${'`` literal must not swallow the argv.
            frame.groups = [group for group in frame.groups if group != "brace"]
        return False

    def _separator(self, frame: _Frame, ch: str, token: str) -> bool:
        """``;`` ``|`` ``&`` newline in ARG position: the next word is a command.
        True if it ends the window: at top level, in ARG position."""
        if frame.mode != _ARG:
            return False  # a newline sentinel before ``in`` or the first pattern
        self._after_esac = self._esac_closer = False
        frame.command_next = True
        if len(self._frames) > 1:
            return False
        # ``&`` ends the argv only as a token of its own (``_ends_argv``): the
        # window keeps reading through a glued ``x&`` -- the deny direction.
        return ch != "&" or token in {"&", "&&"}

    def _clause_end(self, frame: _Frame) -> bool:
        """``;;`` / ``;&`` / ``;;&``: the next tokens are a PATTERN.  True if it ends
        the window (top level, so the enclosing case is the caller's)."""
        self._after_esac = False
        if frame.cases:
            frame.arm_pattern()
            return len(self._frames) == 1
        if self._esac_closer:
            # ``esac);;``: bash refuses ``;;`` after a closed case, so that ``esac``
            # was a quoted PATTERN and the substitution it seemed to close is open.
            self._esac_closer = False
            self._frames.append(_Frame("sub", True))
            self._frames[-1].cases = 1
            self._frames[-1].arm_pattern()
            return False
        if len(self._frames) == 1:
            self._root_event = True  # ``;;`` with no case open: not valid bash
            return True
        # A ``;;`` with no case open in this substitution: the quoted ``'esac'`` that
        # seemed to close it was a body word.  Re-open -- the deny direction.
        frame.cases = 1
        frame.arm_pattern()
        return False

    def _quoted_esac_pattern(self, token: str, i: int) -> bool:
        """At a ``)`` behind a pattern-position ``esac`` (*token* from *i* is what
        follows it): True if the rest of the argv proves that ``esac`` was a quoted
        PATTERN -- read on under the closer reading, and a ``)`` that closes nothing,
        a ``;;`` with no case open or an ``esac`` in command position at top level is
        something bash refuses there.  The event's position is cached: every
        ambiguity before it reads as the pattern (the deny direction), and one that
        reaches the end with no event settles every later one as the closer.
        """
        if self._speculating or self._rest is None:
            return False
        pos = self._fed
        # Bash refuses an EMPTY clause body before a lone ``;``, ``|``, ``&`` or ``)``
        # (``x) ;`` is a syntax error), so one glued behind the closer settles it.  Only
        # in THIS token: a standalone ``;`` token may be a newline sentinel, and a
        # newline after the pattern's ``)`` is allowed.
        after = token[i:].lstrip(" \t")
        if after and after[0] in ";|&)" and not after.startswith((";;", ";&")):
            return False
        hit = self._lookahead
        # A cached answer holds for a later ambiguity when the event it found is
        # still ahead: in a LATER token, or in this token at or past this offset
        # (``hit[2]`` is the offset the event was found at, None when it was found
        # in a later token or not at all).
        stale = (
            hit is None
            or hit[0] > pos
            or (hit[1] is not None and pos > hit[1])
            or (hit[1] == pos and hit[2] is not None and i > hit[2])
        )
        if stale:
            spec = self._speculation()
            spec._data_floor = self._data_floor
            spec._scan(token[i:])
            spec._data_floor = 0
            found = pos if spec._root_event else None
            offset = i + spec._read if spec._root_event else None
            j = pos + 1
            while found is None and j < len(self._rest):
                spec._scan(self._rest[j])
                if spec._root_event:
                    found = j
                j += 1
            # The closer reading's words are kept when an event refused it: a ``;;``
            # may be an enclosing clause's, making that reading bash's own.
            hit = self._lookahead = (pos, found, offset, i, spec.words if found is not None else [])
        assert hit is not None
        return hit[1] is not None

    def _speculation(self) -> "_SubstitutionDepth":
        """A copy of this walker's frames, reading on under the closer reading."""
        spec = _SubstitutionDepth.__new__(_SubstitutionDepth)
        spec._frames = [frame.copy() for frame in self._frames]
        spec._pop()  # the closer reading
        spec._brace_closes = None  # a partial token: no offsets to read ahead from
        spec._after_esac = spec._esac_fresh = spec._esac_closer = False
        spec._data_floor = 0
        spec._rest = None
        spec._fed = 0
        spec._speculating = True
        spec._root_event = False
        spec._lookahead = None
        spec._read = 0
        spec.words = []
        spec.alt_words = []
        spec._alt_pending = False
        spec._comment = self._comment
        return spec

    def _flush(self, frame: _Frame) -> bool:
        """The word being read is complete: read it in the frame's position.
        Always True, so the caller can chain the position check onto it."""
        if not frame.word:
            return True
        word = "".join(frame.word)
        frame.word = []
        self._after_esac = False
        if frame.mode == _WORD:
            frame.mode = _IN  # the case WORD ended
        elif frame.mode == _IN:
            if word == "in":
                frame.cases += 1
                frame.arm_pattern()
            else:
                frame.mode = _ARG
                frame.command_next = False
        elif frame.command_next and word == "case":
            frame.mode = _WORD
        elif frame.command_next and word == "esac" and frame.cases:
            frame.cases -= 1
            frame.command_next = False
            self._after_esac = True
            self._esac_fresh = False
        elif frame.name_next:
            frame.name_next = False
            frame.command_next = True
        else:
            if len(self._frames) == 1 and not self._data_floor:
                if frame.command_next and word == "esac":
                    self._root_event = True  # ``esac`` with no case open: not valid bash
                text = word.replace(_SUBST_MARK, "")
                if text.replace(_QUOTED_WORD_MARK, ""):
                    self.words.append(text)  # marks kept: the consumer unmarks (R26)
            # A keeper or an option word hands command position on only when it
            # HOLDS it (``time case x in …`` in command position arms a case;
            # ``scp src time case x in localhost:/tmp/`` names four operands, R11).
            held = frame.command_next
            frame.command_next = held and (word in _KEEPS_COMMAND_POSITION or word.startswith("-"))
            frame.name_next = held and word in ("function", "coproc")
        return True

    def _pattern_char(self, frame: _Frame, token: str, i: int) -> int:
        """Consume pattern text at *i*; the index after what was read.

        Tokens are de-quoted.  Bash refuses an unquoted ``)`` as a pattern's FIRST
        character and refuses ``))`` (measured), so a ``)`` there was quoted and is
        text: the terminator is the LAST ``)`` of the run.  A bare ``)`` token is the
        EMPTY pattern (``'')``) and terminates; ``')' )`` and ``'x)'y)`` are the
        documented quoted-paren limit.  A ``$( … )`` or backtick inside the pattern
        is a command list of its own (a frame); inside ``${ … }`` a ``)`` is text,
        and so is one inside a bracket ``[ … ]`` (with a ``]`` still to come): bash
        refuses ``[)]`` and ``[(]`` unquoted (measured), so a ``\\)`` between them
        de-quotes to a bracket whose ``)`` was quoted.  ``[:alpha:]`` inside a
        bracket is a class.  A fresh pattern's ``esac`` followed by a separator,
        a closer, a redirect or the token's end is the reserved word (bash
        refuses ``esac)`` and ``esac|`` unquoted: those are pattern text).
        """
        ch = token[i]
        if frame.fresh:
            if ch in ";\n \t":
                # A frame's newline sentinel, or a QUOTED blank (the tokenizer split on
                # the unquoted ones): bash allows both before a pattern, and a token
                # boundary here is what the multi-token spelling gets for free
                # (``"$(case y in y) :;; esac)"`` is ONE token, R17).
                return i + 1
            if ch == "(" and not token.startswith("$(", i):
                frame.fresh = False
                return i + 1  # the optional ``(`` before the first pattern
            if token.startswith("esac", i) and (
                i + 4 == len(token) or token[i + 4] in _ESAC_FOLLOWERS
            ):
                frame.cases -= 1
                frame.mode = _ARG
                frame.fresh = False
                frame.command_next = False
                self._after_esac = True
                self._esac_fresh = True
                return i + 4
        frame.fresh = False
        if token.startswith(("$(", "<(", ">("), i):
            self._push("sub")
            return i + 2
        if token.startswith("${", i):
            frame.groups.append("brace")
            frame.pattern_chars += 2
            return i + 2
        frame.pattern_chars += 1
        bracket = bool(frame.groups) and frame.groups[-1] == "bracket"
        if ch == "`":
            self._push("tick")
        elif ch == "[" and bracket:
            if (
                token[i + 1 : i + 2] in (":", "=", ".")
                and token.find(token[i + 1] + "]", i + 2) > 0
            ):
                return token.find(token[i + 1] + "]", i + 2) + 2  # ``[:alpha:]``: a class
        elif ch == "[":
            j = i + 1 + (token[i + 1 : i + 2] in ("!", "^"))
            j += token[j : j + 1] == "]"  # a leading ``]`` is text
            if token.find("]", j) > 0:
                frame.groups.append("bracket")
                frame.pattern_chars += j - i - 1
                return j
        elif ch == "(" and not bracket:
            frame.groups.append("paren")
        elif ch == "]":
            if bracket:
                frame.groups.pop()
        elif ch == ")":
            if frame.groups:
                if frame.groups[-1] == "paren":
                    frame.groups.pop()
                return i + 1  # inside a bracket: text
            j = i + 1
            if frame.pattern_chars == 1 and j < len(token):
                return j  # a leading ``)`` with text behind it is quoted
            while j < len(token) and token[j] == ")":
                j += 1  # ``))``: the earlier parens are quoted
            frame.mode = _ARG
            frame.command_next = True  # the clause body opens here
            return j
        return i + 1


#: Every word :class:`_SubstitutionDepth` reads as grammar rather than as an
#: argument: the case compound's own words, the name-taking keepers and the
#: command-position keepers.  A QUOTED spelling of any of them is an ordinary
#: word to bash, so :func:`_mask_quoted_reserved_words` marks it before the
#: tokenizer drops the quotes.  A subset of ``argv_floor._SHELL_RESERVED_WORDS``,
#: pinned by test.
_WALKER_GRAMMAR_WORDS = frozenset({"case", "esac", "in", "function"}) | _KEEPS_COMMAND_POSITION


def _mask_quoted_reserved_words(text: str) -> str:
    """Mark quoted grammar words and quoted backticks in the raw *text*
    (:func:`quoted_marks.mask_quoted_reserved_words` with this walker's vocabulary)."""
    return _quoted_marks.mask_quoted_reserved_words(
        text, _WALKER_GRAMMAR_WORDS, _WORD_BREAK, _decode_ansi_c_body
    )
