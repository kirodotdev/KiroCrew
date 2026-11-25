"""The ssh self-target floor's reading of substitution bodies: each one bash runs is judged.

A ``$( )`` or backtick body is a command line bash runs before the outer command, and
the static splice in ``argv_floor_ssh`` reads only its first command (``$(echo hi; ssh
<this host> id)`` splices to ``hi``). So each body bash runs -- unquoted, inside double
quotes, in an unquoted heredoc, nested, or inside a ``bash -c`` / ``sh -c`` / ``eval`` /
here-string payload -- is judged here as a command line of its own, with
``_is_ssh_to_self``. Quote state, comments and heredoc spans come from the shared shell
reader. This reading only adds refusals on top of the line's own verdict.

``kiro_crew.security.argv_floor`` serves every name defined here, and a patch through it
reaches this module (:mod:`kiro_crew._facade_mirror`). This module reads the ssh floor in
``argv_floor_ssh`` and the frame walk in ``argv_floor_self``, both below it.
"""

from __future__ import annotations

import re

from .argv_floor_self import _heredoc_data_view, _quoted_assignment_scripts, _shell_payload_sources
from .argv_floor_ssh import (
    _EMPTY_EXPANSION_RE,
    _HEREDOC_RESET,
    _MAIN_READING,
    _is_ssh_to_self,
    _may_name_ssh_verb,
    _resolve_line_assignments,
    _splice_static_substitutions,
)
from .shell_commands import (
    _heredoc_body_substitutions,
    _without_quoted_heredoc_text,
    read_commands,
)
from .shell_normalizer import (
    _backtick_closer,
    _cased_payload,
    _iter_shell_chars,
    _matching_close_paren,
    _nested_shell_payloads,
    _opens_comment,
    _self_tokens,
)


def _live_substitutions(text: str) -> "list[tuple[int, int, str]]":
    """``(start, end, body)`` of each outermost ``$( )`` / backtick substitution bash
    runs in *text*, in text order; ``start:end`` spans the opener to the closer.

    Unquoted or inside double quotes; never in single quotes, a comment, after an
    escape, as ``$$(`` or in a quoted heredoc. Heredoc bodies are blanked before the
    scan (as ``_double_quoted_substitution_bodies`` does), and the substitutions of an
    unquoted one come from ``shell_commands._heredoc_body_substitutions``, the scan
    ``read_commands`` reads them with.
    """
    subs: list[tuple[int, int, str]] = []
    scan = text
    if "<<" in text:
        for doc in [doc for cmd in read_commands(text, heredocs=True) for doc in cmd.heredocs]:
            blank = re.sub("[^\n]", " ", text[doc.start : doc.end])
            scan = scan[: doc.start] + blank + scan[doc.end :]
            if not doc.quoted:
                subs += [
                    (doc.start + s, doc.start + e, body)
                    for s, e, _at, body in _heredoc_body_substitutions(doc.body)
                ]
    i, state = 0, 0
    while i < len(scan):
        dollars = 0
        # Past the first pass, the scan resumes just after a substitution's closer
        # (or at a comment's newline, which is no ``#``). The closer is part of the
        # word, so a ``#`` right after it (``$(date)#$(...)``) is glued, not a
        # comment: one word character put in front of the rest tells the check so.
        rest = ("_" if i else "") + scan[i:]
        for step in _iter_shell_chars(scan[i:], state):
            at = i + step.offset
            dollars = dollars + 1 if step.state != 1 and step.text == "$" else 0
            if step.state == 0 and step.text == "#" and _opens_comment(rest, step.offset + (i > 0)):
                newline = scan.find("\n", at)
                i, state = (len(scan) if newline < 0 else newline), 0
                break
            if dollars % 2 and scan.startswith("(", at + 1) and not scan.startswith("((", at + 1):
                end, proven = _matching_close_paren(scan[at + 2 :], 0)
                stop = at + 2 + end if proven else len(scan)
                subs.append((at, stop, text[at + 2 : stop - proven]))
                i, state = stop, step.state
                break
            if step.state != 1 and step.text == "`":
                closer = _backtick_closer(scan, at + 1)
                stop = closer + 1 if closer >= 0 else len(scan)
                subs.append((at, stop, text[at + 1 : stop - (closer >= 0)]))
                i, state = stop, step.state
                break
        else:
            break
    return sorted(subs)


def _line_may_name_ssh_verb(text_lower: str) -> bool:
    """The verb gate on the whole line, read both as written and statically spliced:
    the splice erases a list's later commands, and builds a verb glued from parts."""
    raw = _EMPTY_EXPANSION_RE.sub("", text_lower)
    spliced = _splice_static_substitutions(raw, escaped_too=True)
    return any(_may_name_ssh_verb(_resolve_line_assignments(t)) for t in (raw, spliced))


def _substitution_reaches_self(text_lower: str) -> bool:
    """True if a substitution body that bash runs in *text_lower* reaches this host,
    judged as a command line of its own.

    Each body is judged one level at a time: a nested body that the static splice can
    never resolve (one holding ``$``, a backtick or a paren) is replaced by one word,
    and is judged on its own when its turn comes. So each level is read once, and the
    work grows linearly with the nesting depth.
    """
    if not _line_may_name_ssh_verb(text_lower):
        return False
    seen: set[tuple[str, bool]] = set()
    # (text, judge it, allow join): the caller judged the line itself.
    work = [(text_lower, False, True)]
    while work:
        text, judge, allow_join = work.pop()
        live = "$(" in text or "`" in text
        subs = _live_substitutions(text) if live else []
        view = text
        for start, end, body in reversed(subs):
            if any(c in body for c in "$`()"):
                view = view[:start] + "_" + view[end:]
        if judge and _is_ssh_to_self(view):
            return True
        if live:
            # A payload with no substitution opener holds no body; the line's own
            # frame walk already judges the payload itself.
            # A joined frame must not re-join the same words into shrinking suffixes.
            joined_here: set[str] = set()
            nested = _nested_shell_payloads(
                _self_tokens(text), allow_join=allow_join, joined_out=joined_here
            )
            fresh = [p for p in nested if (p, False) not in seen]
            seen.update((p, False) for p in fresh)
            work += [(p, False, p not in joined_here) for p in fresh]
        for _start, _end, body in subs:
            body = _without_quoted_heredoc_text(body)
            if (body, True) not in seen:
                seen.add((body, True))
                work.append((body, True, True))
    return False


# A backslash before a path-name character: on Windows PowerShell runs the line, and
# there it separates path components (``C:\Windows\System32\OpenSSH\ssh.exe``,
# ``.\ssh.exe``) instead of escaping the next character as POSIX shell rules do.
_PATH_SEPARATOR_BACKSLASH_RE = re.compile(r"\\(?=[\w.?*\[-])")
# The word at a command start, past a PowerShell call operator ``&``. A quoted word is
# taken whole (it may hold blanks).
_PROGRAM_WORD_RE = re.compile(r"""[ \t]*(?:&[ \t]*)?('[^']*'|"[^"]*"|[^\s;&|()<>'"]+)""")
_COMMAND_SEPARATORS = frozenset(";&|\n(")


def _with_backslash_path_separators(text: str) -> str:
    """*text* with each backslash before a path-name character read as ``/``, in the
    word where each command starts.

    The tokenizer drops an unquoted backslash and keeps a quoted one, and the program
    basename splits on ``/`` only, so neither reading isolates ``ssh.exe`` at the end of
    a Windows path. A backslash before a quote, a blank, a separator or another
    backslash is left as written, so the quoting the tokenizer reads is unchanged. An
    argument keeps its POSIX reading, so a file operand (``git add tools\\ssh.exe``) is
    not read as a program. A command starts at the line's start or after an unquoted,
    unescaped ``; & | ( newline``, so a separator inside a quoted argument
    (``git commit -m 'a; C:\\x\\ssh.exe'``) starts nothing. One pass, linear in *text*.
    """
    out: list[str] = []
    i, n, quote, at_start = 0, len(text), "", True
    while i < n:
        if at_start:
            at_start = False
            match = _PROGRAM_WORD_RE.match(text, i)
            if match is not None:
                word = match.group(1)
                out.append(text[i : match.start(1)])
                out.append(_PATH_SEPARATOR_BACKSLASH_RE.sub("/", word))
                i = match.end()
                continue
        ch = text[i]
        if quote == '"' and ch == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        if quote:
            quote = "" if ch == quote else quote
        elif ch == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        elif ch in "'\"":
            quote = ch
        elif ch in _COMMAND_SEPARATORS:
            at_start = True
        out.append(ch)
        i += 1
    return "".join(out)


def _reaches_self_over_ssh(text_lower: str, raw_text: "str | None" = None) -> bool:
    """:func:`_is_ssh_to_self` on *text_lower*, on each quoted assignment script in it,
    read whole (the separator mask would turn a script's own separators into data),
    and on each substitution body bash runs in it or in those scripts. Either the
    quote-aware reading or main's splice reading (*_MAIN_READING*) of the line refusing
    refuses, and so does the line read with Windows path backslashes as ``/``
    (:func:`_with_backslash_path_separators`); the bodies are judged once, in the
    quote-aware reading. *raw_text* is the command as submitted, for the wrapper option
    words of the line itself."""
    scripts = _quoted_assignment_scripts(text_lower)
    if _is_ssh_to_self(text_lower, own_line=True, raw_text=raw_text) or any(
        map(_is_ssh_to_self, scripts)
    ):
        return True
    if _substitution_reaches_self(text_lower) or any(map(_substitution_reaches_self, scripts)):
        return True
    # Extra pass, ADDED to the readings above: escape the quote an own-line body
    # leaves open so the text after the closing line reads unquoted, catching a
    # self-ssh a pairing quote would otherwise bury. Run only when a here-document
    # is present; the readings above and the main reading below keep a body's
    # quotes as origin/main read them, so this pass only adds a denial.
    if "<<" in text_lower:
        token = _HEREDOC_RESET.set(True)
        try:
            if _is_ssh_to_self(text_lower, own_line=True, raw_text=raw_text) or any(
                map(_is_ssh_to_self, scripts)
            ):
                return True
        finally:
            _HEREDOC_RESET.reset(token)
    # Extra pass, ADDED to the readings above: a nested shell payload that spans
    # lines is read as a command line of its own. The line's frame walk reads the
    # payload from text whose quoted newlines are masked to data, so a command on a
    # later line of the payload, or one bash runs from a here-document the payload
    # opens, is not seen there. As for a substitution body, the text of a quoted-tag
    # body a bare ``cat`` prints is cut first; the rest is found on the words as
    # written, so a body no program runs stays data. The payloads are found in the
    # self floor's here-document data view, under the guards ``_is_ssh_to_self``
    # takes it with, so a payload written in a body no program runs is not read.
    if "\n" in text_lower:
        cased = raw_text if raw_text is not None and raw_text.lower() == text_lower else None
        view = text_lower
        if (
            cased is not None
            and "\r" not in text_lower
            and not any("<<" in ln and ln.endswith("\\") for ln in text_lower.split("\n"))
        ):
            view = _heredoc_data_view(cased).lower()
        for source in _shell_payload_sources(view):
            if source == view or "\n" not in source:
                continue
            payload_cased = _cased_payload(cased, text_lower, source)
            if payload_cased is not None:
                payload_cased = _without_quoted_heredoc_text(payload_cased)
            payload = _without_quoted_heredoc_text(source)
            if _is_ssh_to_self(payload, raw_text=payload_cased):
                return True
    token = _MAIN_READING.set(True)
    try:
        if _is_ssh_to_self(text_lower, own_line=True, raw_text=raw_text) or any(
            map(_is_ssh_to_self, scripts)
        ):
            return True
    finally:
        _MAIN_READING.reset(token)
    # shortcut: only a command's first word and only the line itself get the path reading,
    # not an argument (``cmd /c C:\\...\\ssh.exe``), a quoted script or a substitution body;
    # widen it if a Windows path there is reported.
    path_text = _with_backslash_path_separators(text_lower)
    if path_text == text_lower:
        return False
    path_raw = None if raw_text is None else _with_backslash_path_separators(raw_text)
    return _is_ssh_to_self(path_text, own_line=True, raw_text=path_raw)
