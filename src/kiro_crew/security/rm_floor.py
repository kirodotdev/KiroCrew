"""Recursive-force ``rm`` seatbelt behind the two ``local-destructive-rm-rf-*`` rows.

Refuses the common respellings of ``rm -rf /`` and ``rm -rf ~`` the rows miss. A
seatbelt, not a parser: other shell shapes stay with the rows and the OS sandbox.
"""

from __future__ import annotations

import re

from ..trust_patterns import ENV_ASSIGNMENT_RE
from . import shell_normalizer as _shell
from .denied_rules import BUILTIN_DENIED_RULES

_RM_FLOOR_RULE_IDS = {
    "root": "local-destructive-rm-rf-root",
    "home": "local-destructive-rm-rf-home",
}
_RM_FLOOR_BY_KIND: dict[str, tuple[str, str]] = {
    kind: (rule.id, rule.pattern)
    for kind, rule_id in _RM_FLOOR_RULE_IDS.items()
    for rule in BUILTIN_DENIED_RULES
    if rule.id == rule_id
}
_RM_FLOOR_NOTE = "Matched on the rm flags and target, not the pattern text: recursive and force against this directory itself."
_RM_FLOOR_UNREAD_NOTE = "Refused because the rm check could not read this command. Simplify it."
_CAN_FIRE_RE = re.compile(r"r[^\w\s]*m")  # a literal rm, quote splices allowed
_RM_WORD_RE = re.compile(r"(?<![\w.-])r[^\w\s/]*m(?![\w.-])")  # rm as its own word

#: A redirection is one word, not the program: an optional ``{fd}`` or fd number,
#: a redirection operator (``>``, ``>>``, ``<``, ``<>``, ``&>``, ``2>&`` …) and any
#: glued target. bash applies a leading redirection before running the command, so
#: one must be stepped over (with its target) exactly as a ``VAR=val`` assignment is.
#: :func:`shell_normalizer._push_token_redirection` is the shared recognizer that
#: reports ``(is_redirection, consumes_next_word)``; it reads a WHOLE token, so
#: :func:`_segments` keeps a redirection and its fd prefix in one word (below).
_HOME_WORDS = frozenset({"~", "$HOME", "${HOME}"})
_CUT_CHARS = frozenset(";&|\n`()")
_SUBST = "\1"  # a substitution: may expand to nothing, unlike a literal ``\0``
#: A STANDALONE fd prefix (a bare number or ``{name}``) directly before a redirection
#: operator: ``2>`` and ``{fd}>`` redirect fd 2 / a named fd, but the ``2`` in
#: ``/tmp/2>x`` is part of the path, so the prefix counts only at a word boundary.
_FD_PREFIX_RE = re.compile(r"(?:^|(?<=[\s\x00]))(?:\{[A-Za-z_]\w*\}|\d+)\Z")


def _pull_trailing_fd_prefix(seg: "list[str]") -> str:
    """Remove and return a standalone fd prefix at the end of *seg*, else ``""``.

    Keeps ``2>``/``{fd}>`` as one redirection word rather than splitting the fd
    number off as its own word (which would be read as the program).
    """
    joined = "".join(seg)
    match = _FD_PREFIX_RE.search(joined)
    if match is None:
        return ""
    seg[:] = [joined[: match.start()]]
    return joined[match.start() :]


def _can_fire(command: str) -> bool:
    return bool(_CAN_FIRE_RE.search(command))


def _names_rm_word(command: str) -> bool:
    """``rm`` as its own word in *command*, or once ``${X:-…}`` defaults are resolved."""
    resolved = _shell._resolve_param_defaults(command)
    return bool(_RM_WORD_RE.search(command) or _RM_WORD_RE.search(resolved))


def _target_kind(word: str) -> "str | None":
    """``root`` / ``home`` when *word* is that dir itself, with an optional ``/`` or ``/*``."""
    base = word[:-2] if word.endswith("/*") else word
    base = base.rstrip("/") if base.strip("/") else ""
    if word.startswith("/") and base == "":
        return "root"
    return "home" if base in _HOME_WORDS else None


def _segments(command: str, track: bool = True) -> "list[str]":
    """*command* cut at separators the shared quote machine marks live; stops at a heredoc."""
    out: list[str] = []
    seg: list[str] = []
    in_redirect = False
    #: closers of open ``${…}`` / ``$[…]``: a ``#`` or ``<<`` there is data, so
    #: neither a comment nor a heredoc starts until they close
    expansion: list[str] = []
    pos, state = 0, 0
    while pos < len(command):
        text, resume, resume_state = command[pos:], len(command), 0
        dollars = 0  # live ``$`` run ending here: ``$$`` is the PID, never an opener
        for step in _shell._iter_shell_chars(text, state):
            dollars = dollars + 1 if step.active and step.char == "$" else 0
            live_syntax = step.active and not expansion
            if live_syntax and text.startswith("<<", step.offset):
                break  # heredoc bodies are data; the rows and the sandbox own them
            if live_syntax and step.char == "#" and _shell._opens_comment(text, step.offset):
                newline = text.find("\n", step.offset)  # a comment's quotes open nothing
                resume = len(command) if newline < 0 else pos + newline
                break
            if step.state == 2 and text.startswith("$(", step.offset):
                # bash reads "$(…)" from an unquoted start, then the double quote resumes
                body = text[step.offset + 2 :]
                close, proven = _shell._matching_close_paren(body, 0)
                out.extend(_segments(body[: close - 1 if proven else close], track))
                seg.append(_SUBST)
                in_redirect = False
                resume, resume_state = pos + step.offset + 2 + close, 2
                break
            if step.state == 2 and step.char == "`" and len(step.text) == 1:
                # a backtick body in double quotes is read on its own, then the quote resumes
                closer = _shell._backtick_closer(text, step.offset + 1)
                closed = closer != -1
                end = closer + 1 if closed else len(text)
                out.extend(_segments(text[step.offset + 1 : end - closed], track))
                seg.append(_SUBST)
                in_redirect = False
                resume, resume_state = pos + end, 2
                break
            if track and dollars % 2 and text.startswith(("${", "$["), step.offset):
                expansion.append("}" if text[step.offset + 1] == "{" else "]")
            elif step.active and expansion and step.char == expansion[-1]:
                expansion.pop()
            if step.active and in_redirect and step.char == "&":
                seg.append(step.text)  # fd-dup operator (``2>&1``), not a command separator
            elif step.active and step.char in _CUT_CHARS:
                out.append("".join(seg))
                seg = []
                in_redirect = False
            elif step.active and step.char in "<>":
                if in_redirect:
                    seg.append(step.text)  # a second operator char (``>>``, ``<>``)
                else:
                    # a live redirection starts its own word; keep its fd prefix and
                    # operator together so the whole redirection is one token
                    seg.append(" " + _pull_trailing_fd_prefix(seg) + step.text)
                    in_redirect = True
            elif step.char in "$~" and (
                len(step.text) == 2 or step.state == 1 or (step.char == "~" and step.state)
            ):
                seg.append("\0")  # an escaped, single-quoted $ or any quoted ~ is literal
                in_redirect = False
            else:
                seg.append(step.text)
                in_redirect = False  # past the operator, into the (glued) target or argv
        pos, state = resume, resume_state
    return [*out, "".join(seg)]


def _rm_wipe_targets(command: str) -> "set[str]":
    """The targets (``root`` / ``home``) a recursive-force ``rm`` in *command* wipes."""
    kinds: set[str] = set()
    # read with and without ``${…}`` tracking: the untracked reading is the
    # base one, so tracking can only add refusals
    for segment in [*_segments(command), *_segments(command, track=False)]:
        words = _shell._shell_tokens(segment)
        while words:
            if ENV_ASSIGNMENT_RE.match(words[0]) or words[0] in _shell._KEEPS_COMMAND_POSITION:
                # ``then`` / ``do`` / ``!`` / ``time`` … leave the next word the program
                words = words[2:] if words[0] == "time" and words[1:2] == ["-p"] else words[1:]
                continue
            is_redirection, consumes_next = _shell._push_token_redirection(words[0])
            if is_redirection:
                # a leading redirection runs before the command; drop its word, and
                # its separate target word too when the operator did not glue one on
                words = words[1:]
                if consumes_next and words:
                    words = words[1:]
                continue
            if _SUBST in words[0]:
                # a substitution in command position may expand to nothing
                bare = words[0].replace(_SUBST, "")
                words = [bare, *words[1:]] if bare else words[1:]
                continue
            break
        if not words or _shell._program_basename(words[0]) != "rm":
            continue
        recursive = force = done = False
        operands = []
        for arg in words[1:]:
            if done or not arg.startswith("-") or arg == "-":
                operands.append(arg)
            elif arg == "--":
                done = True
            elif arg.startswith("--"):
                recursive = recursive or arg == "--recursive"
                force = force or arg == "--force"
            else:
                recursive = recursive or "r" in arg or "R" in arg
                force = force or "f" in arg
        if recursive and force:
            kinds |= {kind for arg in operands if (kind := _target_kind(arg))}
    return kinds
