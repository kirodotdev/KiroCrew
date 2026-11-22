"""A quote-aware reader of a shell command line, one command at a time.

It walks the text with the shared quote machine
(:func:`shell_normalizer._iter_shell_chars`) and keeps the two facts a dequoted
token list drops: which characters were quoted, and where a live control operator
ends a command even when it is glued to a word (``a&&rm``). Each
:class:`ShellCommand` carries its steps (raw text, raw offsets, quote state), the
words built from them, its redirections, and the separator that ended it.

A reader, not a parser: it expands and evaluates nothing. By default it stops at a
live ``<<`` because a heredoc body is data; with ``heredocs=True`` it hands each body
to its command as a :class:`HereDoc` and reads on. A ``$(…)`` or backtick body inside double
quotes is read as commands of its own (listed before the command holding it), and
that command keeps one :data:`SUBST` step in its place.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

from . import shell_normalizer as _shell
from . import shell_normalizer_walk as _walk

# One gate call's payloads per frame text are kept in ``_CALL_VIEWS`` under the plain
# text; ``shell_normalizer._shell_payload_walk`` keeps its frames there under tuple keys.
from .call_views import _CALL_VIEWS, sharing_views  # noqa: F401  (also served to importers)

if TYPE_CHECKING:
    from .shell_heredoc_lines import HereDocSpan

#: The step standing for a substitution read on its own: it may expand to nothing.
SUBST = "\1"
#: Live characters that end a command: ``;`` ``&`` ``|`` newline, a backtick or a paren.
_CUT_CHARS = frozenset(";&|\n`()")
#: Doubled, these end one command as one operator (``&&``, ``||``).
_DOUBLED = frozenset("&|")
_BLANKS = frozenset(" \t")


class ShellStep(NamedTuple):
    """One step of a command: a character, an escape pair, or a :data:`SUBST`."""

    #: Raw offsets in the text handed to :func:`read_commands`.
    start: int
    end: int
    #: The raw text consumed (``SUBST`` for a substitution).
    text: str
    #: The significant character: for an escape pair, the character escaped.
    char: str
    #: Quote state after this step: 0 normal, 1 single-quoted, 2 double-quoted.
    state: int
    #: Live shell syntax: unquoted and unescaped.
    active: bool
    #: The first character of a live redirection operator (``>``, ``2>``'s ``>``).
    redirect: bool


class ShellWord(NamedTuple):
    """One word of a command: the steps between live blanks."""

    steps: "tuple[ShellStep, ...]"

    @property
    def raw(self) -> str:
        return "".join(step.text for step in self.steps)

    @property
    def start(self) -> int:
        return self.steps[0].start

    @property
    def end(self) -> int:
        return self.steps[-1].end

    @property
    def quoted(self) -> "tuple[bool, ...]":
        """Per step: True where it is not live syntax (quoted, escaped or a closing quote)."""
        return tuple(not step.active for step in self.steps)

    @property
    def is_redirect(self) -> bool:
        return any(step.redirect for step in self.steps)

    @property
    def wants_target(self) -> bool:
        """A redirection whose operator ends the word, so the next word is its target."""
        at = next((i for i, step in enumerate(self.steps) if step.redirect), None)
        if at is None:
            return False
        rest = self.steps[at + 1 :]
        return all(step.active and step.char in "<>&|" for step in rest)


class HereDoc(NamedTuple):
    """One here-document body, read with ``heredocs=True``."""

    #: The body text, without its tag line.
    body: str
    #: Raw offsets of the body in the text handed to :func:`read_commands`.
    start: int
    end: int
    #: The tag was quoted (``<<'EOF'``, ``<<\\EOF``): the body expands nothing.
    quoted: bool
    #: Raw offset past the tag line that closed the body; ``end`` when nothing closed it.
    tail: int = -1


class ShellCommand(NamedTuple):
    """One command: its steps and the live separator that ended it (``""`` at the end)."""

    steps: "tuple[ShellStep, ...]"
    #: ``;`` ``&`` ``&&`` ``|`` ``||`` ``\\n`` a backtick or paren, ``<<`` where a
    #: heredoc stopped the read, or ``""`` at the end of the text.
    separator: str
    #: The here-documents this command reads, in order (only with ``heredocs=True``).
    heredocs: "tuple[HereDoc, ...]" = ()

    @property
    def words(self) -> "tuple[ShellWord, ...]":
        """Words split at live blanks and at a ``\\``-newline (bash drops it) before the first
        word; a live redirection starts its own word, keeping a standalone fd prefix glued."""
        words: list[tuple[ShellStep, ...]] = []
        word: list[ShellStep] = []
        for step in self.steps:
            if step.active and step.char in _BLANKS or step.text == "\\\n" and not (words or word):
                if word:
                    words.append(tuple(word))
                word = []
                continue
            if step.redirect and word and not _is_fd_prefix("".join(s.text for s in word)):
                words.append(tuple(word))
                word = []
            word.append(step)
        if word:
            words.append(tuple(word))
        return tuple(ShellWord(steps) for steps in words)

    @property
    def redirects(self) -> "tuple[tuple[ShellWord, ShellWord | None], ...]":
        """Each redirection word with its separate target word, if it takes one."""
        return self._parts()[1]

    @property
    def argv(self) -> "tuple[ShellWord, ...]":
        """The words left once every redirection and its target are stepped over."""
        return self._parts()[0]

    def _parts(
        self,
    ) -> "tuple[tuple[ShellWord, ...], tuple[tuple[ShellWord, ShellWord | None], ...]]":
        argv: list[ShellWord] = []
        redirects: list[tuple[ShellWord, ShellWord | None]] = []
        words = self.words
        i = 0
        while i < len(words):
            if words[i].is_redirect:
                target = words[i + 1] if words[i].wants_target and i + 1 < len(words) else None
                redirects.append((words[i], target))
                i += 2 if target is not None else 1
            else:
                argv.append(words[i])
                i += 1
        return tuple(argv), tuple(redirects)


def _is_fd_prefix(raw: str) -> bool:
    if raw.isdigit():
        return True
    name = raw[1:-1]
    return raw[:1] == "{" and raw[-1:] == "}" and name.isidentifier() and name.isascii()


def read_commands(
    command: str,
    track: bool = True,
    _base: int = 0,
    nested: bool = True,
    heredocs: bool = False,
) -> "list[ShellCommand]":
    """*command* as the commands a shell would run, in reading order.

    *track* follows open ``${…}`` / ``$[…]`` spans (opened by an odd run of live
    ``$``), inside which a ``#`` or ``<<`` is data; a caller that cannot trust that
    tracking reads the text both ways; *nested* False skips the commands of a quoted
    substitution body (its :data:`SUBST` step stays). *heredocs* reads past a live
    ``<<`` instead of stopping: each body goes to its command's :attr:`ShellCommand.heredocs`,
    and the substitutions in an unquoted body are read as commands of their own, listed
    after the line. Which ``<<`` opens a body, and where the body ends, is decided once
    for every reader (``shell_heredoc_lines.heredoc_docs``); a line whose bodies open at
    a ``<<`` this read did not see live is read on as commands. The list is never empty:
    the last command holds whatever follows the last separator.
    """
    out: list[ShellCommand] = []
    steps: list[ShellStep] = []
    stop = ""
    in_redirect = False
    expansion: list[str] = []  # closers of open ``${…}`` / ``$[…]``
    pos, state = 0, 0
    docs: "tuple[HereDocSpan, ...]" = ()
    if heredocs and "<<" in command:
        from .shell_heredoc_lines import heredoc_docs  # lazy: off the MCP listing

        docs = heredoc_docs(command)
    owners: "dict[int, int | None]" = {}  # offset of each live ``<<`` -> its command

    def cut(separator: str) -> None:
        nonlocal steps
        if not steps and separator in _DOUBLED and out and out[-1].separator == separator:
            out[-1] = out[-1]._replace(separator=separator * 2)
        else:
            out.append(ShellCommand(tuple(steps), separator))
        steps = []
        for opener, owner in owners.items():
            owners[opener] = len(out) - 1 if owner is None else owner

    while pos < len(command):
        text, resume, resume_state = command[pos:], len(command), 0
        dollars = 0  # live ``$`` run ending here: ``$$`` is the PID, never an opener
        for step in _shell._iter_shell_chars(text, state):
            at = _base + pos + step.offset
            dollars = dollars + 1 if step.active and step.char == "$" else 0
            live_syntax = step.active and not expansion
            near = text[max(step.offset - 1, 0) : step.offset + 3]  # ``<<<`` takes one word
            if live_syntax and text.startswith("<<", step.offset) and "<<<" not in near:
                if not heredocs:
                    stop = "<<"
                    break
                owners.setdefault(pos + step.offset, None)
            if live_syntax and step.char == "#" and _shell._opens_comment(text, step.offset):
                newline = text.find("\n", step.offset)  # a comment's quotes open nothing
                resume = len(command) if newline < 0 else pos + newline
                break
            if step.state == 2 and text.startswith("$(", step.offset):
                # bash reads "$(…)" from an unquoted start, then the double quote resumes
                body = text[step.offset + 2 :]
                close, proven = _shell._matching_close_paren(body, 0)
                if nested:
                    inner = body[: close - 1 if proven else close]
                    out.extend(read_commands(inner, track, at + 2, heredocs=heredocs))
                end = pos + step.offset + 2 + close
                steps.append(ShellStep(at, _base + end, SUBST, SUBST, 2, False, False))
                in_redirect = False
                resume, resume_state = end, 2
                break
            if step.state == 2 and step.char == "`" and len(step.text) == 1:
                # a backtick body in double quotes is read on its own, then the quote resumes
                closer = _shell._backtick_closer(text, step.offset + 1)
                closed = closer != -1
                end = closer + 1 if closed else len(text)
                if nested:
                    inner = text[step.offset + 1 : end - closed]
                    out.extend(read_commands(inner, track, at + 1, heredocs=heredocs))
                steps.append(ShellStep(at, _base + pos + end, SUBST, SUBST, 2, False, False))
                in_redirect = False
                resume, resume_state = pos + end, 2
                break
            if track and dollars % 2 and text.startswith(("${", "$["), step.offset):
                expansion.append("}" if text[step.offset + 1] == "{" else "]")
            elif step.active and expansion and step.char == expansion[-1]:
                expansion.pop()
            redirect = False
            prev = steps[-1].char if steps else ""
            amp_opens = step.char == "&" and text.startswith(">", step.offset + 1)  # ``&>``
            if step.active and in_redirect and step.char + prev in ("&&", "&>", "&<", "|>"):
                pass  # ``2>&1`` duplicates, ``>|`` clobbers: one operator, no separator
            elif step.active and amp_opens and (steps or not out or out[-1].separator != "&"):
                redirect = in_redirect = True  # ``&>`` / ``&>>``; ``&&>`` stays a separator
            elif step.active and step.char in _CUT_CHARS:
                cut(step.char)
                in_redirect = False
                if step.char == "\n" and owners:  # the bodies start on the next line
                    line = [doc for doc in docs if doc.line == pos + step.offset]
                    known = all(doc.opener in owners for doc in line)
                    seen, owners = owners, {}
                    if line and known:
                        resume = _attach_bodies(command, line, seen, out, track, _base)
                        break
                continue
            elif step.active and step.char in "<>":
                redirect = not in_redirect  # a second operator char (``>>``) continues it
                in_redirect = True
            else:
                in_redirect = False  # past the operator, into the (glued) target or argv
            steps.append(
                ShellStep(
                    at, at + len(step.text), step.text, step.char, step.state, step.active, redirect
                )
            )
        pos, state = resume, resume_state
    out.append(ShellCommand(tuple(steps), stop))
    return out


def _attach_bodies(
    command: str,
    docs: "list[HereDocSpan]",
    owners: "dict[int, int | None]",
    out: "list[ShellCommand]",
    track: bool,
    base: int,
) -> int:
    """Attach each body of one line to the command that holds its ``<<``, read the
    substitutions of an unquoted one, and return the offset past the last closing line."""
    for span in docs:
        owner = owners[span.opener]
        if owner is None:  # never: the newline that ends the line cut its command
            owner = len(out) - 1
        body = command[span.start : span.end]
        doc = HereDoc(body, base + span.start, base + span.end, span.quoted, base + span.tail)
        out[owner] = out[owner]._replace(heredocs=out[owner].heredocs + (doc,))
        if not span.quoted:
            out.extend(_body_substitutions(body, base + span.start, track))
    return min(docs[-1].tail + 1, len(command))


def _body_substitutions(body: str, base: int, track: bool) -> "list[ShellCommand]":
    """The commands of each ``$(…)`` / backtick in an unquoted heredoc body, read once."""
    out: list[ShellCommand] = []
    for _start, _stop, inner_at, inner in _heredoc_body_substitutions(body):
        out.extend(read_commands(inner, track, base + inner_at, heredocs=True))
    return out


def _heredoc_body_substitutions(body: str) -> "list[tuple[int, int, int, str]]":
    """``(start, stop, inner_start, inner)`` of each ``$(…)`` / backtick in an unquoted
    heredoc body, in order; ``start:stop`` spans the opener to the closer. There a quote
    is a plain character and only a backslash escapes. The one scan of such a body: the
    ssh self-target floor (``argv_floor_ssh_bodies``) reads it too."""
    out: list[tuple[int, int, int, str]] = []
    i = 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
        elif body.startswith("$(", i):
            rest = body[i + 2 :]  # the walk starts unquoted inside the substitution
            close, proven = _shell._matching_close_paren(rest, 0)
            out.append((i, i + 2 + close, i + 2, rest[: close - 1 if proven else close]))
            i += 2 + close
        elif body[i] == "`":
            closer = _shell._backtick_closer(body, i + 1)
            end = len(body) if closer < 0 else closer
            out.append((i, end + 1, i + 1, body[i + 1 : end]))
            i = end + 1
        else:
            i += 1
    return out


def pipelines(commands: "list[ShellCommand]") -> "list[list[ShellCommand]]":
    """*commands*, one :func:`read_commands` result, grouped into pipelines: a command joins
    the one before it when that one ended in ``|``, in ``|&`` (read as ``|`` then an empty
    ``&``), or opened the ``( … )`` group the pipe feeds (``| (sh)``)."""
    out: list[list[ShellCommand]] = []
    feeds = False
    for cmd in commands:
        if feeds:
            out[-1].append(cmd)
        else:
            out.append([cmd])
        feeds = cmd.separator == "|" or feeds and cmd.separator in ("&", "(") and not cmd.argv
    return out


def redirect_free_views(command: str) -> "list[str]":
    """Each command with a live redirection before an argv word, without its redirections:
    ``sh -c 2>/dev/null '<script>'`` runs ``<script>``. Read once per frame, without quoted
    substitution bodies (each is a frame of its own), and only when a ``<`` / ``>`` exists."""
    if "<" not in command and ">" not in command:
        return []
    folded = _shell._fold_line_continuations(command) if "\\\n" in command else command
    reads = [(folded, read_commands(folded, nested=False))]
    if any(cmd.separator == "(" for cmd in reads[0][1]):  # ``2>$(x)`` cut its command
        plain = without_substitutions(folded)
        reads.append((plain, read_commands(plain, nested=False)))
    return [
        " ".join(text[word.start : word.end] for word in argv)
        for text, commands in reads
        for argv, redirects in (cmd._parts() for cmd in commands)
        if redirects and argv and argv[-1].start > redirects[0][0].end
    ]


def redirect_hidden_payloads(command: str) -> "list[str]":
    """Each :func:`redirect_free_views` view's payloads, for a walk to add to its frame's."""
    cache = {} if (shared := _CALL_VIEWS.get()) is None else shared
    if command not in cache:
        views, nested = redirect_free_views(command), _shell._nested_shell_payloads
        cache[command] = [
            p for v in views for p in nested(_shell._shell_tokens(v), allow_join=False)
        ]
    return cache[command]


def without_substitutions(command: str) -> str:
    """*command* with each unquoted ``$(…)`` / ``<(…)`` / ``>(…)`` as the word ``_``, which
    :func:`read_commands` would cut (``bash -c 2>$(x) '<script>'``)."""
    out: list[str] = []
    pos = 0
    quote_at: "dict[int, tuple[int, bool]] | None" = None
    while True:
        steps = _shell._iter_shell_chars(command, 0, False, pos)
        opens = (s.offset for s in steps if s.active and s.char in "$<>")
        hit = next((at for at in opens if command.startswith("(", at + 1)), None)
        if hit is None:
            return "".join(out) + command[pos:]
        out.append(command[pos:hit] + "_")
        quote_at = quote_at or _walk._quote_states(command)
        pos = _shell._matching_close_paren(command, hit + 2, quote_at[hit + 2])[0]


def heredoc_text_spans(text: str) -> "list[tuple[int, int]]":
    """Raw ``(start, end)`` of each here-document body :func:`read_commands` finds in *text*."""
    if "<<" not in text:
        return []
    return [
        (doc.start, doc.end) for cmd in read_commands(text, heredocs=True) for doc in cmd.heredocs
    ]


def _double_quoted_substitution_spans(text: str) -> "list[tuple[int, int, bool]]":
    """``(start, end, proven)`` of each ``$(...)`` directly inside double quotes in *text*.

    *start* is the ``$``; *end* is just past the matching ``)`` when *proven*, else the end
    of *text*. A ``)`` in a heredoc body inside the substitution does not end it.

    A heredoc body starts after the newline that ends its ``<<`` line, so when the text
    the close walk read has no newline after its first ``<<`` (none at all, a ``<<<``
    here-string, a ``$(( 1 << n ))`` shift), no body can change that walk and the
    heredoc re-read is skipped. Each body that does hold a heredoc before its close
    still re-reads the rest of *text*, so a line of many such bodies stays quadratic.
    """
    spans: "list[tuple[int, int, bool]]" = []
    i, state = 0, 0
    while i < len(text):
        dollars = 0
        for step in _shell._iter_shell_chars(text[i:], state):
            at = i + step.offset
            dollars = dollars + 1 if step.state == 2 and step.text == "$" else 0
            if dollars % 2 and text.startswith("(", at + 1):
                rest = text[at + 2 :]
                end, proven, reach = _walk._matching_close_paren_reach(rest, 0)
                window = rest if reach is None else rest[:reach]
                k = window.find("<<")
                if k >= 0 and "\n" in window[k:]:
                    for start, stop in heredoc_text_spans(rest):
                        rest = rest[:start] + re.sub("[^\n]", " ", rest[start:stop]) + rest[stop:]
                    end, proven = _shell._matching_close_paren(rest, 0)
                spans.append((at, at + 2 + end if proven else len(text), proven))
                i, state = spans[-1][1], 2
                break
        else:
            break
    return spans


def _proven_body_spans(text: str) -> "dict[int, int]":
    """Start -> end of each proven double-quoted ``$( )`` body outside heredoc text."""
    if "$(" not in text:
        return {}
    docs = heredoc_text_spans(text)
    return {
        at: end
        for at, end, proven in _double_quoted_substitution_spans(text)
        if proven and not any(start <= at < stop for start, stop in docs)
    }


def _double_quoted_substitution_bodies(text: str) -> "list[str]":
    """The body of each ``$(...)`` that sits directly inside double quotes in *text*.

    :func:`_mask_quoted_separators` keeps double-quoted text as data, but bash runs
    such a body as a command line of its own, in a new quote context. Only an
    unpaired ``$`` opens one (``$$`` is the PID, ``\\$`` is text); the body ends at
    its own matching ``)``, or runs to the end of *text* when none is found. A ``)``
    in a heredoc body that :func:`read_commands` finds there does not end it. The
    text that a bare ``cat`` prints from a quoted-tag heredoc is cut out of each
    body (:func:`_without_quoted_heredoc_text`).
    """
    spans = _double_quoted_substitution_spans(text)
    return [_without_quoted_heredoc_text(text[a + 2 : e - (1 if p else 0)]) for a, e, p in spans]


def _without_quoted_heredoc_text(body: str) -> str:
    """*body* with the text of each quoted-tag heredoc that a bare ``cat`` prints cut out.

    Bash expands nothing in such a text, and a ``cat`` that writes to no file and
    pipes to nothing only prints it. Every other heredoc keeps its text, so a shell
    fed one (``bash <<'EOF'``, ``cat <<'EOF' | sh``) is read as before.
    """
    if "<<" not in body:
        return body
    docs = [
        doc
        for cmd in read_commands(body, heredocs=True)
        if [word.raw for word in cmd.argv] == ["cat"]
        and all(word.raw.startswith("<<") for word, _target in cmd.redirects)
        and not cmd.separator.startswith("|")
        for doc in cmd.heredocs
        if doc.quoted
    ]
    cut = len(body)
    for doc in sorted(docs, key=lambda doc: doc.start, reverse=True):
        if doc.end <= cut:
            body, cut = body[: doc.start] + body[doc.end :], doc.start
    return body
