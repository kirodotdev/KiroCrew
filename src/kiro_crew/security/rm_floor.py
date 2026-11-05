"""Recursive-force ``rm`` seatbelt behind the two ``local-destructive-rm-rf-*`` rows.

Refuses the common respellings of ``rm -rf /`` and ``rm -rf ~`` the rows miss, nested
shells included. A seatbelt, not a parser: other shapes stay with the rows and the sandbox.
"""

from __future__ import annotations

import re

from ..trust_patterns import ENV_ASSIGNMENT_RE
from . import shell_commands as _commands
from . import shell_normalizer as _shell
from .denied_rules import BUILTIN_DENIED_RULES
from .shell_normalizer_escapes import _ANSI_C_QUOTE_RE

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
_LITERAL_SHELLS = _shell._NESTED_SHELL_PROGRAMS - {"busybox"}  # busybox's next word is the applet
_SUBST = _commands.SUBST  # a substitution: may expand to nothing, unlike a literal ``\0``
#: an unquoted parameter (``$x``, ``${…}``, ``$1``): an unset one expands to nothing
_PARAM_RE = re.compile(r"\$(?:\{[^}]*\}|[A-Za-z_]\w*|[0-9@*#?$!-])")
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
    """``rm`` as its own word in *command*, once ``${X:-…}`` defaults are resolved, or
    once unquoted parameters are dropped (an unset ``$1`` glued to ``rm`` runs ``rm``)."""
    resolved = _shell._resolve_param_defaults(command)
    return any(_RM_WORD_RE.search(text) for text in (command, resolved, _PARAM_RE.sub("", command)))


def _target_kind(word: str) -> "str | None":
    """``root`` / ``home`` when *word* is that dir itself, with an optional ``/`` or ``/*``."""
    base = word[:-2] if word.endswith("/*") else word
    base = base.rstrip("/") if base.strip("/") else ""
    if word.startswith("/") and base == "":
        return "root"
    return "home" if base in _HOME_WORDS else None


def _segments(command: str, track: bool = True) -> "list[str]":
    """*command* as the shared reader's commands, each rendered as one segment string.

    A live redirection starts its own word with its fd prefix and operator kept
    together, and an escaped or single-quoted ``$`` or any quoted ``~`` is a literal
    ``\\0`` so it can never read as ``$HOME`` or ``~``.
    """
    out = []
    # ``&>`` is one redirect in bash but a background ``&`` then ``>`` in dash
    for text in dict.fromkeys((command, command.replace("&>", "& >"))):
        for cmd in _commands.read_commands(text, track, heredocs=True):
            seg: list[str] = []
            for step in cmd.steps:
                if step.redirect:
                    seg.append(" " + _pull_trailing_fd_prefix(seg) + step.text)
                elif step.char in "$~" and (
                    len(step.text) == 2 or step.state == 1 or (step.char == "~" and step.state)
                ):
                    seg.append("\0")
                else:
                    seg.append(step.text)
            out.append("".join(seg))
    return out


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
            bare = _PARAM_RE.sub("", words[0])
            if bare != words[0] and _shell._program_basename(words[0]) != "rm":
                # ``${x}rm`` runs ``rm`` when ``x`` is unset; a word already read
                # as ``rm`` keeps that reading, so this only adds refusals
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


def _after_shell_verb(tokens: "list[str]", programs: "list[str]", index: int) -> bool:
    """True when *tokens[index]* is the argument of ``eval`` / ``source`` / ``.``."""
    before = index - 1
    while before >= 0 and tokens[before] == "--":
        before -= 1
    if before < 0 or programs[before] != tokens[before]:
        return False  # a redirect target named ``eval`` is a file, not the verb
    verbs = _shell._NESTED_SHELL_VERBS
    return tokens[before] in verbs or _shell._program_basename(tokens[before]) in verbs


def _command_lines(text: str) -> "list[str]":
    """*text* without its heredoc bodies (data unless fed to a shell, see
    :func:`_heredoc_payloads`), cut at each live newline, a comment cut off."""
    text = _without_bodies(text)
    lines: list[str] = []
    pos = 0
    while pos <= len(text):
        rest, cut, end = text[pos:], len(text), len(text)
        for step in _shell._iter_shell_chars(rest):
            at = step.offset
            if step.active and step.char == "#" and _shell._opens_comment(rest, at):
                newline = rest.find("\n", at)
                cut, end = pos + at, len(text) if newline < 0 else pos + newline
                break
            if step.active and step.char == "\n":
                cut = end = pos + at
                break
        lines.append(text[pos:cut])
        pos = end + 1
    return lines


def _without_bodies(text: str) -> str:
    """*text* with each heredoc body and the tag line that closed it cut out."""
    commands = _commands.read_commands(text, heredocs=True) if "<<" in text else []
    spans: list[list[int]] = []  # a body nested in another body's substitution merges
    for start, end in sorted((d.start, d.tail) for c in commands for d in c.heredocs):
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    for start, end in reversed(spans):
        text = text[:start] + text[end:]
    return text


def _heredoc_payloads(command: str) -> "list[str]":
    """Each heredoc body a shell reads as its script (``bash <<EOF``, ``cat <<EOF | sh``),
    and each command read from a substitution in an unquoted body, which bash runs."""
    commands = _commands.read_commands(command, heredocs=True)
    docs = [d for c in commands for d in c.heredocs if not d.quoted]
    out: list[str] = []
    for i, cmd in enumerate(commands):
        if cmd.steps and any(d.start <= cmd.steps[0].start < d.end for d in docs):
            out.append(command[cmd.steps[0].start : cmd.steps[-1].end])
        if cmd.heredocs and (
            _runs_shell(cmd)
            or cmd.separator == "|"
            and i + 1 < len(commands)
            and _runs_shell(commands[i + 1])
            and _forwards_stdin(cmd)
            and not _stdin_redirect(commands[i + 1])[0]  # its own stdin replaces the pipe
        ):
            out.extend(_stdin_body(cmd))
    return out


def _forwards_stdin(cmd: "_commands.ShellCommand") -> bool:
    """*cmd* copies its stdin to its stdout (``cat``, ``tee``): only then does a piped
    shell read its heredoc; ``printf ... <<EOF | bash`` sends printf's output instead."""
    from . import argv_rules  # argv_rules imports this module

    words = _shell._shell_tokens(" ".join(word.raw for word in cmd.argv))
    return (argv_rules._program_args(words) or ("",))[0] in ("cat", "tee")


def _stdin_body(cmd: "_commands.ShellCommand") -> "list[str]":
    """The heredoc body that is *cmd*'s stdin, if its last fd 0 redirection is one."""
    op, index = _stdin_redirect(cmd)
    reads = op.startswith("<<") and not op.startswith("<<<") and index < len(cmd.heredocs)
    return [cmd.heredocs[index].body] if reads else []


def _stdin_redirect(cmd: "_commands.ShellCommand") -> "tuple[str, int]":
    """*cmd*'s last fd 0 redirection operator (``""`` for none) and how many heredocs come
    before it: a later ``</dev/null`` or ``<<`` replaces an earlier one; ``3<<B`` is not fd 0."""
    last, heredocs = ("", 0), 0
    for word, _target in cmd.redirects:
        op = word.raw.lstrip("0123456789")
        if op[:1] == "<" and word.raw[: len(word.raw) - len(op)] in ("", "0"):
            last = (op, heredocs)
        heredocs += op.startswith("<<") and not op.startswith("<<<")
    return last


def _runs_shell(cmd: "_commands.ShellCommand") -> bool:
    """*cmd* is a shell reading its script from stdin (``bash``, ``sudo -u x sh -s``): the
    program behind any wrapper is a shell given no ``-c`` and no script file."""
    from . import argv_rules  # argv_rules imports this module

    words = _shell._shell_tokens(" ".join(word.raw for word in cmd.argv))
    program, args = argv_rules._program_args(words) or ("", [])
    if program == "busybox" and args:  # busybox's next word is the applet
        program, args = _shell._program_basename(args[0]), args[1:]
    if program not in _LITERAL_SHELLS:
        return False
    rest, from_stdin = iter(args), False
    for arg in rest:
        if arg in _SHELL_VALUE_OPTIONS or arg[:1] in "-+" and arg[1:2] != "-" and arg[-1:] in "oO":
            if next(rest, None) == "noexec" and arg[:1] == "-":
                return False  # ``-o noexec`` checks syntax and runs nothing
        elif arg[:1] == "-" and arg[1:2] != "-" and "n" in arg:
            return False  # ``-n`` checks syntax and runs nothing
        elif arg in ("--help", "--version"):
            return False  # prints and exits without reading stdin
        elif _shell._is_shell_command_flag(arg):
            return False  # ``-c`` wins, even after ``-s``: stdin is its data
        elif arg[:1] not in "-+":
            return from_stdin  # after ``-s`` an argument, else a script file
        elif arg[:1] == "-" and arg[1:2] != "-" and "s" in arg:
            from_stdin = True  # ``-s``: the script is stdin unless a later ``-c``
    return True


#: Characters one floor call may read across the command and all its nested payloads:
#: 16 times ``paths.MAX_SCANNABLE_COMMAND_CHARS``, so a capped input ends well under 1 s.
_WALK_BUDGET_CHARS = 16 * 20 * 1024
#: Shell options that take the next word as their value (``-o posix``).
_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})


def _ansi_c_newlines(command: str) -> str:
    """*command* with each ``\\n`` escape in a ``$'…'`` span as a real newline. The
    tokenizer reads that escape as a blank, but a nested shell given it reads a new line."""

    def keep(match: "re.Match[str]") -> str:
        if match.group(1) is None:
            return match.group(0)
        body = re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(0), match.group(1))
        return "$'" + body + "'"

    return _ANSI_C_QUOTE_RE.sub(keep, command)


def _expands_nothing(command: str) -> bool:
    """*command* is one command and bash hands it on verbatim: it holds a ``\\$``, every
    ``$`` is single-quoted, and there is no ``"``, backtick or ``\\\\``. Read once per call."""
    if "\\$" not in command or any(mark in command for mark in ('"', "`", "\\\\")):
        return False
    if len(_commands.read_commands(command)) != 1:
        return False
    return all(c.state == 1 for c in _shell._iter_shell_chars(command) if c.char == "$")


def _literal_shell_script(tokens: "list[str]") -> "str | None":
    """The script of exactly ``<shell> -c '<script>'`` or ``<shell> <<< '<script>'``
    (``bash``, ``sh`` …, never ``busybox``). Only that script, in a command that
    :func:`_expands_nothing`, keeps its ``\\$`` literal; any other payload (a forwarded
    ``sed …/e``, ``env -S``, ``xargs``) may be unescaped, so it keeps the fail-closed view."""
    if len(tokens) != 3 or _shell._program_basename(tokens[0]) not in _LITERAL_SHELLS:
        return None
    if tokens[1] != "<<<" and not _shell._is_shell_command_flag(tokens[1]):
        return None
    return tokens[2]


def _command_tokens(text: str) -> "list[list[str]]":
    """Dequoted argv of each command the shared reader finds in each line, read every
    way so each reading only adds payloads; a command stopped at ``<<`` runs to the
    line's end (a herestring is a payload)."""
    out: list[list[str]] = []
    for line in _command_lines(text):
        for part in dict.fromkeys((line, _shell._fold_line_continuations(line))):
            for plain in dict.fromkeys((part, _commands.without_substitutions(part))):
                for track in (True, False) if "${" in plain or "$[" in plain else (True,):
                    for cmd in _commands.read_commands(plain, track):
                        words = " ".join(plain[word.start : word.end] for word in cmd.argv)
                        if cmd.separator == "<<":  # the read stops there; the line runs on
                            words = plain[cmd.steps[0].start if cmd.steps else 0 :]
                        out.append(_shell._shell_tokens(words))
                        if "<<<" in plain and (s := cmd.steps):  # its own words, herestring kept
                            out.append(_shell._shell_tokens(plain[s[0].start : s[-1].end]))
    return out


def _rm_wipe_targets_nested(command: str) -> "set[str]":
    """:func:`_rm_wipe_targets` over *command* and each nested-shell payload in it.

    A payload inert in its own command (``echo bash -c '...'``) or an ``eval`` / ``source``
    argument is skipped. A payload is shorter than its parent, so the walk ends.
    """
    kinds: set[str] = set()
    pending, seen = [command], {command}
    verbatim = _expands_nothing(command)
    walked = 0
    while pending:
        source = pending.pop()
        walked += len(source)
        if walked > _WALK_BUDGET_CHARS:  # deep nesting re-reads each suffix: fail closed
            raise RuntimeError(f"rm floor walk passed its budget of {_WALK_BUDGET_CHARS} chars")
        kinds |= _rm_wipe_targets(source)
        # a quoted heredoc body keeps its backslash-newlines, so fold only outside bodies
        folded = _shell._fold_line_continuations(
            _without_bodies(source) if "<<" in source else source
        )
        kinds |= _rm_wipe_targets(folded) if folded != source else set()  # bash folds them
        for payload in _heredoc_payloads(source) if "<<" in source else ():
            if len(payload) < len(source) and payload not in seen:
                seen.add(payload)
                pending.append(payload)
        views = (source, _ansi_c_newlines(source)) if "$'" in source else (source,)
        for tokens in (t for view in dict.fromkeys(views) for t in _command_tokens(view)):
            payloads = _shell._nested_shell_payloads(tokens, allow_join=False)
            if not payloads:
                continue
            script = _literal_shell_script(tokens) if source == command and verbatim else None
            programs = _shell._argv_programs(tokens)
            disqualified = _shell._data_consumer_command_disqualified(tokens)
            joined = "\0".join(tokens)
            for payload in dict.fromkeys(payloads):
                spots = [i for i, token in enumerate(tokens) if token == payload]
                # a skip needs no glued copy elsewhere (``bash<<<'x'``)
                spots = spots if joined.count(payload) == len(spots) else []
                if spots and all(_after_shell_verb(tokens, programs, i) for i in spots):
                    continue  # an ``eval`` / ``source`` argument
                if spots and all(
                    _shell._data_consumer_exempt(
                        i, payload, programs, tokens, command_disqualified=disqualified
                    )
                    for i in spots
                ):
                    continue
                literal = payload == script
                views = (payload,) if literal else (payload, payload.replace("\\$", "$"))
                for view in views:
                    if len(view) < len(source) and view not in seen:
                        seen.add(view)
                        pending.append(view)
    return kinds
