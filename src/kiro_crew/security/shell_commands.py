"""A quote-aware reader of a shell command line, one command at a time.

It walks the text with the shared quote machine
(:func:`shell_normalizer._iter_shell_chars`) and keeps the two facts a dequoted
token list drops: which characters were quoted, and where a live control operator
ends a command even when it is glued to a word (``a&&rm``). Each
:class:`ShellCommand` carries its steps (raw text, raw offsets, quote state), the
words built from them, its redirections, and the separator that ended it.

A reader, not a parser: it expands and evaluates nothing, and it stops at a live
``<<`` because a heredoc body is data. A ``$(…)`` or backtick body inside double
quotes is read as commands of its own (listed before the command holding it), and
that command keeps one :data:`SUBST` step in its place.
"""

from __future__ import annotations

import functools
from contextvars import ContextVar
from typing import NamedTuple

from . import shell_normalizer as _shell

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


class ShellCommand(NamedTuple):
    """One command: its steps and the live separator that ended it (``""`` at the end)."""

    steps: "tuple[ShellStep, ...]"
    #: ``;`` ``&`` ``&&`` ``|`` ``||`` ``\\n`` a backtick or paren, ``<<`` where a
    #: heredoc stopped the read, or ``""`` at the end of the text.
    separator: str

    @property
    def words(self) -> "tuple[ShellWord, ...]":
        """Words split at live blanks; a live redirection starts its own word, keeping
        a standalone fd prefix (``2``, ``{fd}``) glued in front of it."""
        words: list[tuple[ShellStep, ...]] = []
        word: list[ShellStep] = []
        for step in self.steps:
            if step.active and step.char in _BLANKS:
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


def _heredoc_tag(text: str, i: int) -> "tuple[str, bool]":
    """The here-document delimiter word at *text[i:]* (after blanks), with quotes and
    escapes removed, and whether any of it was quoted (then the body is not expanded)."""
    while i < len(text) and (text[i] in " \t" or text.startswith("\\\n", i)):
        i += 2 if text[i] == "\\" else 1  # a live ``\<newline>`` joins lines
    tag: list[str] = []
    quoted = False
    while i < len(text) and text[i] not in " \t\n;&|()<>":
        ch = text[i]
        if ch == "$" and text[i + 1 : i + 2] in ("'", '"'):
            i += 1
        elif text.startswith("\\\n", i):
            i += 2
        elif ch in "'\"":
            close = text.find(ch, i + 1)
            close = len(text) if close < 0 else close
            part = text[i + 1 : close]  # single quotes keep ``\<newline>``; double drop it
            tag.append(part.replace("\\\n", "") if ch == '"' else part)
            quoted, i = True, close + 1
        elif ch == "\\":
            tag.append(text[i + 1 : i + 2])
            quoted, i = True, i + 2
        else:
            tag.append(ch)
            i += 1
    return "".join(tag), quoted


def _heredoc_end(text: str, start: int, tag: str, strip_tabs: bool) -> int:
    """Index just past the body line equal to *tag*, or -1 when no line closes it."""
    pos = start
    while True:
        end = text.find("\n", pos)
        line = text[pos : len(text) if end < 0 else end]
        if (line.lstrip("\t") if strip_tabs else line) == tag:
            return len(text) if end < 0 else end + 1
        if end < 0:
            return -1
        pos = end + 1


def _body_commands(body: str, base: int, track: bool) -> "list[ShellCommand]":
    """The commands an expanding here-document body runs: each ``$(…)`` and backtick
    body, read once. Quotes in the body are literal text, so only ``\\`` escapes."""
    out: list[ShellCommand] = []
    i = 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
        elif body.startswith("$(", i):
            close, proven = _shell._matching_close_paren(body[i + 2 :], 0)
            inner = body[i + 2 : i + 2 + (close - 1 if proven else close)]
            out.extend(read_commands(inner, track, base + i + 2, heredoc_bodies=True))
            i += 2 + close
        elif body[i] == "`":
            closer = _shell._backtick_closer(body, i + 1)
            end = len(body) if closer < 0 else closer
            out.extend(read_commands(body[i + 1 : end], track, base + i + 1, heredoc_bodies=True))
            i = end + 1
        else:
            i += 1
    return out


def read_commands(
    command: str,
    track: bool = True,
    _base: int = 0,
    nested: bool = True,
    heredoc_bodies: bool = False,
) -> "list[ShellCommand]":
    """*command* as the commands a shell would run, in reading order.

    *track* follows open ``${…}`` / ``$[…]`` spans (opened by an odd run of live
    ``$``), inside which a ``#`` or ``<<`` is data; a caller that cannot trust that
    tracking reads the text both ways; *nested* False skips the commands of a quoted
    substitution body (its :data:`SUBST` step stays). The list is never empty: the last
    command holds whatever follows the last separator.

    By default the read stops at a live ``<<``. With *heredoc_bodies* it reads ``<<`` /
    ``<<-`` as a redirection, skips each body once its line ends, and resumes after it;
    an unquoted delimiter's body adds the commands of its substitutions. A body no line
    closes is not skipped, so the rest is still read as commands.
    """
    out: list[ShellCommand] = []
    steps: list[ShellStep] = []
    stop = ""
    in_redirect = False
    expansion: list[str] = []  # closers of open ``${…}`` / ``$[…]``
    pos, state = 0, 0
    pending: "list[tuple[str, bool, bool]]" = []  # (tag, strip tabs, expands) per ``<<``
    dash_at = -1  # absolute offset of the ``-`` of ``<<-``, kept out of the words

    def cut(separator: str) -> None:
        nonlocal steps
        if not steps and separator in _DOUBLED and out and out[-1].separator == separator:
            out[-1] = out[-1]._replace(separator=separator * 2)
        else:
            out.append(ShellCommand(tuple(steps), separator))
        steps = []

    while pos < len(command):
        text, resume, resume_state = command[pos:], len(command), 0
        dollars = 0  # live ``$`` run ending here: ``$$`` is the PID, never an opener
        for step in _shell._iter_shell_chars(text, state):
            at = _base + pos + step.offset
            dollars = dollars + 1 if step.active and step.char == "$" else 0
            live_syntax = step.active and not expansion
            near = text[max(step.offset - 1, 0) : step.offset + 3]  # ``<<<`` takes one word
            if pos + step.offset == dash_at:
                continue
            if live_syntax and text.startswith("<<", step.offset) and "<<<" not in near:
                if not heredoc_bodies:
                    stop = "<<"
                    break
                strip = text.startswith("<<-", step.offset)
                dash_at = pos + step.offset + 2 if strip else -1
                tag, quoted = _heredoc_tag(text, step.offset + 2 + strip)
                pending.append((tag, strip, not quoted))
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
                    out.extend(read_commands(inner, track, at + 2, heredoc_bodies=heredoc_bodies))
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
                    out.extend(read_commands(inner, track, at + 1, heredoc_bodies=heredoc_bodies))
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
                if step.char == "\n" and pending:
                    # The bodies start on the next line, in ``<<`` order; skip them all
                    # only when every one is closed, else read on (fail closed).
                    start, bodies = step.offset + 1, []
                    for tag, strip, expands in pending:
                        end = _heredoc_end(text, start, tag, strip)
                        if end < 0:
                            break
                        bodies.append((start, end, expands))
                        start = end
                    if len(bodies) == len(pending):
                        for begin, end, expands in bodies:
                            if expands:
                                body = text[begin:end]
                                out.extend(_body_commands(body, _base + pos + begin, track))
                        pending = []
                        resume = pos + start
                        break
                    pending = []
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


#: One gate call's payloads per frame text, shared by its walks; unset outside a call.
_CALL_VIEWS: "ContextVar[dict[str, list[str]] | None]" = ContextVar("call_views", default=None)


def sharing_views(fn):  # type: ignore[no-untyped-def]  # one view cache per call of fn
    @functools.wraps(fn)
    def run(*args, **kwargs):  # type: ignore[no-untyped-def]
        scope = _CALL_VIEWS.set({})
        try:
            return fn(*args, **kwargs)
        finally:
            _CALL_VIEWS.reset(scope)

    return run


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
    while True:
        steps = _shell._iter_shell_chars(command[pos:])
        opens = (pos + s.offset for s in steps if s.active and s.char in "$<>")
        hit = next((at for at in opens if command.startswith("(", at + 1)), None)
        if hit is None:
            return "".join(out) + command[pos:]
        out.append(command[pos:hit] + "_")
        pos = _shell._matching_close_paren(command, hit + 2)[0]
