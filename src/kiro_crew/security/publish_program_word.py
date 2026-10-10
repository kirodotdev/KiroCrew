"""Program-position reading of a command line for the git-publish floor.

``_runs_runtime_program_publish`` answers whether a command runs the ``push``
subcommand under a program word the shell resolves at run time -- a basename
carrying an expansion or a command substitution, or an unquoted glob that can
expand to ``git`` -- in any program position of the text: after any separator,
inside compound constructs, subshells and ``case`` arms, behind precommands and
redirections.  ``_runtime_publishes`` reports each one, with its ``push``
arguments when the program word reaches exec as exactly one word (a
double-quoted parameter expansion) followed directly by ``push``: git's argv is
then known and ``argv_floor`` judges its target like a literal git push.  Any
other runtime-program publish is unverifiable.

The program is the word's top-level BASENAME: ``"$SDK/platform-tools/adb"`` runs
``adb`` wherever the directory points, so it is not a runtime-resolved program.

These helpers live apart so that the reading does not count against
``argv_floor``'s per-module liveness cap.

Layer.  This module imports only ``shell_normalizer``; ``argv_floor`` imports
these names.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from .shell_normalizer import (
    _GLOB_CHARS_RE,
    _decode_shell_quoted_literals,
    _dequote_token,
    _fold_line_continuations,
    _glob_could_expand_to,
    _iter_shell_chars,
    _opens_comment,
    _push_token_redirection,
    _shell_quote_walk,
    _split_shell_words,
    _word_at,
)

# Git global flags that consume a separate argument token (appear between
# `git` and the subcommand).
_GIT_ARG_FLAGS = frozenset({"-c", "-C", "--git-dir", "--work-tree", "--namespace"})

#: Words after which the next word is still in program position: the reserved
#: words that open a command list in a compound construct, plus ``!``, which
#: prefixes a pipeline, and ``{``, which opens a group.
_GIT_PROGRAM_LEAD_WORDS = frozenset(
    {"!", "{", "do", "elif", "else", "if", "then", "until", "while"}
)

#: An assignment word before a command, as bash reads one: ``NAME=value``,
#: ``NAME+=value``, and a subscripted ``NAME[...]=value``, which bash rejects
#: and then still runs the command after it.
_ASSIGNMENT_WORD_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")

#: Precommand programs and keywords that run one of their later words as a
#: program, each with the number of positional operands it reads before that
#: program (``timeout 5 <program>``, ``chroot <dir> <program>``).  A literal
#: word right after one of their value-taking options is read as that option's
#: value.
_GIT_PRECOMMAND_POSITIONALS = {
    "builtin": 0,
    "chroot": 1,
    "command": 0,
    "coproc": 0,
    "doas": 0,
    "env": 0,
    "exec": 0,
    "flock": 1,
    "ionice": 0,
    "nice": 0,
    "nohup": 0,
    "parallel": 0,
    "setsid": 0,
    "stdbuf": 0,
    "sudo": 0,
    "taskset": 1,
    "time": 0,
    "timeout": 1,
    "unbuffer": 0,
    "watch": 0,
    "xargs": 0,
}

#: Precommands that do not hand the program the argv written after it: ``xargs``
#: and ``parallel`` add the words they read, and ``watch`` joins its operands
#: into a string for ``sh -c``.  A publish behind one has no known argv.
_ARGV_CHANGING_PRECOMMANDS = frozenset({"parallel", "watch", "xargs"})

#: Precommand options that build the program and its argv out of a string the
#: floor cannot split back into words: ``env -S``/``--split-string`` runs the
#: words of its value, so the tokens written after it belong to that program,
#: not to a known publish.  Keyed by precommand; the short spelling is matched
#: as a letter anywhere in a short-option cluster (``env -iS 'str'``).
_ARGV_RECONSTRUCTING_LONG_OPTIONS = {"env": ("--split-string",)}
_ARGV_RECONSTRUCTING_SHORT_LETTERS = {"env": "s"}


def _reconstructs_argv(precommand: str, word: str) -> bool:
    """True if option *word* of *precommand* rebuilds the argv from a string.

    A long option matches by name, including an attached value (``--split-string=x``)
    and an unambiguous abbreviation (``env --split``); a short option matches when the
    letter sits anywhere in the cluster, since the letter takes effect wherever it falls.
    """
    if word.startswith("--"):
        name = word.split("=", 1)[0]
        return len(name) >= 3 and any(
            full.startswith(name) for full in _ARGV_RECONSTRUCTING_LONG_OPTIONS.get(precommand, ())
        )
    return any(
        letter in word[1:] for letter in _ARGV_RECONSTRUCTING_SHORT_LETTERS.get(precommand, "")
    )


#: The options of each precommand that take their value as the NEXT word, in
#: the lowercase spelling the floor reads.  Any other option takes no separate
#: value, so the word after it is still the precommand's operand or program.
_GIT_PRECOMMAND_VALUE_OPTIONS = {
    "chroot": frozenset({"--groups", "--userspec"}),
    "doas": frozenset({"-c", "-u"}),
    "env": frozenset({"-c", "-s", "-u", "--chdir", "--split-string", "--unset"}),
    "exec": frozenset({"-a"}),
    "flock": frozenset({"-c", "-e", "-w", "--command", "--conflict-exit-code", "--timeout"}),
    "ionice": frozenset({"-c", "-n", "-p", "--class", "--classdata", "--pid"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "parallel": frozenset({"-j", "-n", "-s", "--jobs"}),
    "stdbuf": frozenset({"-e", "-i", "-o", "--error", "--input", "--output"}),
    "sudo": frozenset(
        {
            "-c",
            "-d",
            "-g",
            "-h",
            "-p",
            "-r",
            "-t",
            "-u",
            "--chdir",
            "--close-from",
            "--command-timeout",
            "--group",
            "--host",
            "--other-user",
            "--prompt",
            "--role",
            "--type",
            "--user",
        }
    ),
    "time": frozenset({"-f", "-o", "--format", "--output"}),
    "timeout": frozenset({"-k", "-s", "--kill-after", "--signal"}),
    "watch": frozenset({"-n", "--interval"}),
    "xargs": frozenset(
        {"-a", "-d", "-e", "-i", "-l", "-n", "-p", "-s", "--arg-file", "--delimiter", "--max-args"}
    ),
}


def _opens_substitution(steps: list, position: int) -> bool:
    """True if the ``(`` or ``{`` at *position* opens a substitution or expansion.

    ``$(``, ``${`` and the process substitutions ``<(`` / ``>(``, read in any
    quote state but single quotes: ``"${G}"`` is an expansion too.
    """
    if position == 0:
        return False
    before = steps[position - 1]
    if len(before.text) != 1 or before.state == 1:
        return False
    if before.char == "$":
        return True
    return steps[position].char == "(" and before.char in "<>" and before.active


def _opens_extglob(steps: list, position: int, word_start_negation: bool = False) -> bool:
    """True if the ``(`` at *position* opens an extglob group (``@(i)``, ``*(x)``).

    The group is part of the word, not a subshell.  A ``!(`` that starts a word
    is a negated subshell without ``extglob`` and a group with it, so it opens
    a group only when *word_start_negation* asks for the second reading.
    """
    if position == 0:
        return False
    before = steps[position - 1]
    if not before.active or before.char not in "@!+*?":
        return False
    if before.char != "!" or word_start_negation:
        return True
    return position >= 2 and steps[position - 2].char not in _COMMAND_BREAK


#: Characters that end a word where a command may start.
_COMMAND_BREAK = frozenset(" \t\n;&|()")


def _has_extglob(word: str) -> bool:
    """True if raw *word* carries an unquoted extglob group, ``!(`` included."""
    steps = list(_iter_shell_chars(word))
    return any(
        step.active and step.char == "(" and _opens_extglob(steps, position, True)
        for position, step in enumerate(steps)
    )


def _shell_commands(text: str, word_start_negation: bool = False) -> "list[str]":
    """*text* split into the commands the shell runs, at top level only.

    A command ends at an unquoted ``;``, ``|``, newline, or ``&`` that is not
    part of a redirection (``2>&1``, ``&>log``, ``<&0``), and at a bare ``(`` or
    ``)``, which open and close a subshell or end a ``case`` pattern.  Nothing
    inside a ``$(...)``, ``${...}``, backtick body or extglob group is split:
    a substitution is its own command, which the payload walk judges as a
    source of its own.  *word_start_negation* reads a word-start ``!(`` as an
    extglob group (see ``_opens_extglob``).
    """
    steps = list(_iter_shell_chars(text))
    pieces: list[str] = []
    buf: list[str] = []
    depth = 0
    in_backticks = False
    for position, step in enumerate(steps):
        if step.trailing_escape:
            buf.append(step.text)
            break
        char = step.char
        if char == "`" and len(step.text) == 1 and step.state != 1:
            in_backticks = not in_backticks
        elif step.active and not in_backticks:
            if depth:
                if char in "({":
                    depth += 1 if char == "(" or _opens_substitution(steps, position) else 0
                elif char in ")}":
                    depth -= 1
            elif char in "({" and _opens_substitution(steps, position):
                depth = 1
            elif char == "(" and _opens_extglob(steps, position, word_start_negation):
                depth = 1
            elif char in ";|\n()" or (char == "&" and not _is_redirection_amp(steps, position)):
                pieces.append("".join(buf))
                buf = []
                continue
        buf.append(step.text)
    pieces.append("".join(buf))
    return [piece for piece in pieces if piece.strip()]


def _is_redirection_amp(steps: list, position: int) -> bool:
    """True if the ``&`` at *position* belongs to a redirection operator.

    Only an active ``<`` or ``>`` beside it makes one: an escaped or quoted
    angle bracket is data, and the ``&`` beside it still ends a command.
    """
    before = steps[position - 1] if position else None
    after = steps[position + 1] if position + 1 < len(steps) else None
    if before is not None and before.active and before.char in "<>":
        return True
    return after is not None and after.active and after.char == ">"


def _active_backticks(word: str) -> int:
    """Backticks in *word* that open or close a substitution (not quoted literals)."""
    return sum(
        1
        for step in _iter_shell_chars(word)
        if step.char == "`" and len(step.text) == 1 and step.state != 1
    )


def _word_span_end(words: "list[str]", start: int) -> int:
    """Index of the word that closes the shell word opened at *start*.

    An unquoted command substitution can span several words (``$(echo git)``
    splits as ``$(echo`` and ``git)``).  The shell word ends at the first word
    where every substitution backtick is paired and every unquoted ``(`` is
    closed.  An unclosed span runs to the last word.
    """
    depth = 0
    backticks = 0
    for index in range(start, len(words)):
        depth += _shell_quote_walk(words[index]).paren_delta
        backticks += _active_backticks(words[index])
        if depth <= 0 and backticks % 2 == 0:
            return index
    return len(words) - 1


def _top_level_basename(raw: str) -> str:
    """The part of raw shell word *raw* after its last top-level ``/``.

    A ``/`` inside a substitution or a parameter expansion is part of what the
    shell produces, not a path separator of the word, so it does not count.
    """
    depth = 0
    in_backticks = False
    last = -1
    reopen = ""
    steps = list(_iter_shell_chars(raw))
    for position, step in enumerate(steps):
        char = step.char
        if char == "`" and len(step.text) == 1 and step.state != 1:
            in_backticks = not in_backticks
        elif in_backticks:
            continue
        elif char == "/" and not depth:
            # Quoted or escaped, a ``/`` is still a literal path separator; the
            # quote it sits in is reopened in front of what follows it.
            last = step.offset + len(step.text) - 1
            reopen = {1: "'", 2: '"'}.get(step.state, "")
        elif step.state != 1 and len(step.text) == 1:
            if char in "({" and _opens_substitution(steps, position):
                depth += 1
            elif char in ")}" and depth:
                depth -= 1
    # A Windows path separates with ``\``; the program is what follows the last.
    return (reopen + raw[last + 1 :]).rsplit("\\", 1)[-1]


#: A word that is wholly unquoted parameter/command expansions (``$name``,
#: ``${...}``, ``$(...)``, backticks), one after another with no literal text.
#: Unquoted, every such word can expand to nothing, so it does not settle
#: program position -- the next word may be the program the shell runs.
_VANISHING_WORD_RE = re.compile(r"\A(?:\$\{[^{}]*\}|\$\([^()]*\)|`[^`]*`|\$[\w@*#?!$-]+)+\Z")

#: A quoted expansion that still produces ZERO words when empty: ``"$@"``,
#: ``"${@}"``, ``"${@:2}"`` and ``"${name[@]}"`` (with optional slice), including
#: several concatenated into one word (a ``"$x"`` or ``"${a[*]}"`` yields one
#: empty word instead, so is excluded).
_ZERO_WORD_EXPANSION_RE = re.compile(
    r'\A(?:"\$@"|"\$\{@[^{}]*\}"|"\$\{[A-Za-z_]\w*\[@\][^{}]*\}")+\Z'
)


def _can_vanish(raw: str) -> bool:
    """True if *raw* is an expansion or glob that can produce no word at all.

    An unquoted parameter/command expansion, a quoted zero-word positional or
    array expansion (``"$@"``, ``"${@}"``, ``"${a[@]}"``), and an unquoted glob
    (which matches nothing under ``nullglob``) each can leave the program
    position to the next word.
    """
    decoded = _decode_shell_quoted_literals(raw)
    if _VANISHING_WORD_RE.match(decoded) or _ZERO_WORD_EXPANSION_RE.match(decoded):
        return True
    return any(char in _active_glob_view(decoded) for char in "*?[")


def _runtime_program(raw: str) -> bool:
    """True if raw program word *raw* may name git only once the shell resolves it.

    A basename carrying ``$`` or a backtick is resolved at run time, and so is a
    basename whose unquoted glob characters can expand to ``git``.  Quoting is
    read per character: ``g"i"[t]`` keeps its active ``[t]``.  An ANSI-C quoted
    span is read as the literal it decodes to, so ``$'adb'`` is ``adb``.
    """
    raw = _decode_shell_quoted_literals(raw)
    if _splits_into_words(raw):
        return True
    base = _top_level_basename(raw)
    # A ``\`` is a Windows path separator or a POSIX escape, so both readings count.
    posix_base = _top_level_basename(raw.replace("\\", "\0")).replace("\0", "\\")
    if any("$" in view or "`" in view or _has_extglob(view) for view in (base, posix_base)):
        return True
    return any(
        _glob_could_expand_to(_active_glob_view(view), ("git",)) for view in (base, posix_base)
    )


def _splits_into_words(raw: str) -> bool:
    """True if program word *raw* carries an unquoted expansion anywhere.

    The shell splits an unquoted expansion's value into words, so such a word
    -- even one whose basename is literal (``$G/repo``) -- can become a
    different program and argv.
    """
    return any(
        step.state == 0 and len(step.text) == 1 and step.char in "$`"
        for step in _iter_shell_chars(raw)
    )


def _after_redirection(words: "list[str]", index: int) -> "int | None":
    """Index of the word after the redirection at *index*; None if it is not one.

    The target is consumed whole, including a substitution spanning several
    words (``>$(echo /dev/null)``, ``> $(echo x)``).
    """
    is_redirection, consumes_next = _push_token_redirection(words[index])
    if not is_redirection:
        return None
    end = _word_span_end(words, index)
    if consumes_next and end == index and index + 1 < len(words):
        end = _word_span_end(words, index + 1)
    return end + 1


def _active_glob_view(word: str) -> str:
    """*word* with its quote delimiters removed and quoted glob syntax neutralised.

    An unquoted glob character stays glob syntax.  A quoted or escaped one is a
    literal, which no program name contains, so it becomes NUL: a character
    that matches nothing.
    """
    out: list[str] = []
    previous = 0
    for step in _iter_shell_chars(word):
        delimiter = (
            len(step.text) == 1
            and step.char in "'\""
            and (step.active or (previous != 0 and step.state == 0))
        )
        previous = step.state
        if delimiter:
            continue
        literal_glob = not step.active and _GLOB_CHARS_RE.match(step.char)
        out.append("\0" if literal_glob else step.char)
    return "".join(out)


def _split_glued_redirections(words: "list[str]") -> "list[str]":
    """*words* with each unquoted top-level redirection cut off the word before it.

    ``"$G"</dev/null`` is the word ``"$G"`` and the redirection ``</dev/null``;
    ``push>log`` is ``push`` and ``>log``.  A process substitution (``<(``,
    ``>(``) and anything inside a substitution stay in the word.
    """
    out: list[str] = []
    for word in words:
        if _push_token_redirection(word)[0]:
            # The whole word is a redirection operator and its target (``&>f``,
            # ``{fd}>f``, ``2>f``): it carries no program word and is consumed by
            # ``_after_redirection``.  Splitting it would invent a phantom one.
            out.append(word)
            continue
        steps = list(_iter_shell_chars(word))
        depth = 0
        cut = None
        for position, step in enumerate(steps):
            char = step.char
            if not step.active:
                continue
            if char in "({" and _opens_substitution(steps, position):
                depth += 1
            elif char in ")}" and depth:
                depth -= 1
            elif depth or position == 0 or char not in "<>&":
                continue
            elif char == "&" and not word.startswith(">", step.offset + 1):
                continue
            elif word.startswith("(", step.offset + 1):
                continue
            else:
                cut = step.offset
                break
        if cut is None:
            out.append(word)
            continue
        head = word[:cut]
        # A run of digits before the operator names a descriptor: ``2>log``.
        if head.isdigit():
            out.append(word)
            continue
        out.extend([head, word[cut:]])
    return out


def _git_subcommand(
    words: "list[str]", index: int, cache: "dict[int, tuple[str, int] | None] | None" = None
) -> "tuple[str, int] | None":
    """The literal git subcommand at or after *index*, and its last word's index.

    Skips what git and the shell consume before the subcommand: redirections
    with a separated target, empty words, simple flags, and a flag in
    ``_GIT_ARG_FLAGS`` together with the whole span of its value.  A word is
    read as the shell hands it over, ANSI-C and locale quoting included
    (``$'push'``).  *cache* maps each index already walked to its answer, so
    repeated lookups from one command's words cost one walk in total.
    """
    visited: list[int] = []
    result: "tuple[str, int] | None" = None
    while index < len(words):
        if cache is not None and index in cache:
            result = cache[index]
            break
        visited.append(index)
        after = _after_redirection(words, index)
        if after is not None:
            index = after
            continue
        end = _word_span_end(words, index)
        word = _dequote_token(_decode_shell_quoted_literals(" ".join(words[index : end + 1])))
        if word in _GIT_ARG_FLAGS and end + 1 < len(words):
            index = _word_span_end(words, end + 1) + 1
        elif not word.strip() or word.startswith("-"):
            index = end + 1
        else:
            result = (word, end)
            break
    if cache is not None:
        for walked in visited:
            cache[walked] = result
    return result


#: A program word the shell hands to exec as exactly ONE word: fully double
#: quoted, carrying parameter expansions only.  A command substitution, a
#: backtick, or an ``@`` / ``*`` (``"$@"``, ``"${a[@]}"``, which expand to
#: several words) disqualifies it.  A backslash before an ordinary character
#: is literal inside double quotes (``"$HOME\bin\git.exe"``), so it is kept.
_ONE_WORD_EXPANSION_RE = re.compile(r'\A"(?=[^"]*\$)(?:[^"`\\@*]|\\[^"`\\@*\n$])*"\Z')


class _RuntimePublish(NamedTuple):
    """A ``push`` run under a runtime-resolved program word."""

    #: The raw words after ``push``, when the program word reaches exec as one
    #: word and every word between it and ``push`` is literal, so git's argv
    #: after ``push`` is exactly these words.  None when the publish's
    #: arguments cannot be known.
    args: "list[str] | None"
    #: The raw program word, then each validated flag value.
    program: "tuple[str, ...]" = ()


def _literal_git_prefix(words: "list[str]", start: int, stop: int) -> "list[str] | None":
    """The validated flag values if ``words[start:stop]`` reach git as known words.

    The value of a flag in ``_GIT_ARG_FLAGS`` may be one quoted parameter
    expansion (``-C "$REPO"``): it reaches git as one word and does not move
    the push target.  Returns those values, or None when any other word in the
    span carries an expansion or a glob.
    """
    values: list[str] = []
    index = start
    while index < stop:
        word = words[index]
        value = words[index + 1] if index + 1 < stop else ""
        if _dequote_token(word) in _GIT_ARG_FLAGS and _ONE_WORD_EXPANSION_RE.match(value):
            values.append(value)
            index += 2
            continue
        if any(char in word for char in "$`*?["):
            return None
        index += 1
    return values


def _command_runtime_publish(command: str) -> "_RuntimePublish | None":
    """The push *command* runs under a runtime-resolved program word, if any.

    Program position opens at the command's first word, after a word in
    ``_GIT_PROGRAM_LEAD_WORDS``, after the name that follows ``function``, and
    after a precommand's options and positional operands.  Leading
    ``VAR=value`` assignments and redirections keep it open.  Inside a
    precommand a runtime program word that does not run ``push`` is read as an
    operand, since its value may be an option value or another precommand.
    """
    words = _split_glued_redirections(_split_shell_words(command))
    positionals: "int | None" = None
    value_options: "frozenset[str]" = frozenset()
    after_option = False
    argv_changed = False
    base_of_precommand = ""
    lookahead: "dict[int, tuple[str, int] | None]" = {}
    index = 0
    while index < len(words):
        after = _after_redirection(words, index)
        if after is not None:
            index = after
            continue
        start = index
        end = _word_span_end(words, index)
        raw = " ".join(words[index : end + 1])
        word = _dequote_token(raw)
        index = end + 1
        if after_option:
            # This word is the value of the previous precommand option, not a
            # program, reserved word or operand of its own.
            after_option = False
            continue
        if word in _GIT_PROGRAM_LEAD_WORDS or _ASSIGNMENT_WORD_RE.match(word):
            continue
        if word == "function":
            index += 1
            continue
        if _runtime_program(raw):
            subcommand = _git_subcommand(words, index, lookahead)
            if subcommand is not None and subcommand[0] == "push":
                one_word = start == end and _ONE_WORD_EXPANSION_RE.match(raw) is not None
                values = _literal_git_prefix(words, index, subcommand[1] + 1)
                known = one_word and values is not None and not argv_changed
                args = words[subcommand[1] + 1 :] if known else None
                return _RuntimePublish(args, (raw, *(values or ())))
            if positionals is None:
                if _can_vanish(raw):
                    # An unquoted expansion that can expand to nothing does not
                    # settle program position: the next word may be the program.
                    continue
                return None
            # The word is an operand of an open precommand (an option value was
            # consumed above).  A runtime-resolved operand could be a
            # split-string or other argv-changer, so the argv the program
            # finally runs is unknown.
            argv_changed = True
            if positionals:
                positionals -= 1
            continue
        if positionals is not None and word.startswith("-"):
            after_option = _option_takes_next_word(word, value_options)
            if base_of_precommand == "command" and word.lstrip("-") in ("v", "pv", "vp"):
                # ``command -v`` / ``-V`` only looks the name up; it runs nothing.
                return None
            if _reconstructs_argv(base_of_precommand, word):
                # ``env -S 'bash -c'`` splits its value into the program and
                # argv, so the words after it are that program's, not a known
                # publish's.  The push behind it is unverifiable.
                argv_changed = True
                if word.startswith("--") and "=" not in word:
                    # A detached long split-string option takes its value as the
                    # next word even when the abbreviation is not in the value
                    # table.  A short option's attachment is read by
                    # ``_option_takes_next_word`` above, so leave its result.
                    after_option = True
            continue
        base = _top_level_basename(word)
        if base == "coproc" and index + 1 < len(words) and words[index + 1] == "{":
            # ``coproc NAME { ... }``: the name labels the group, which runs next.
            index += 1
        if base in _GIT_PRECOMMAND_POSITIONALS:
            positionals = _GIT_PRECOMMAND_POSITIONALS[base]
            value_options = _GIT_PRECOMMAND_VALUE_OPTIONS.get(base, frozenset())
            after_option = False
            argv_changed = argv_changed or base in _ARGV_CHANGING_PRECOMMANDS
            base_of_precommand = base
            continue
        if _can_vanish(raw):
            # A word that can expand to nothing fills no operand slot and leaves
            # program position to the next word, inside a precommand or not.
            continue
        if positionals:
            positionals -= 1
            continue
        return None
    return None


def _option_takes_next_word(word: str, value_options: "frozenset[str]") -> bool:
    """True if precommand option *word* takes the NEXT word as its value.

    A short-option cluster is read the way ``getopt`` reads it: the first
    letter that takes a value takes the rest of the cluster, or the next word
    when it is the cluster's last letter (``env -iu NAME``).
    """
    if word.startswith("--"):
        return word in value_options
    for position, letter in enumerate(word[1:], start=1):
        if f"-{letter}" in value_options:
            return position == len(word) - 1
    return False


def _runtime_publishes(text_lower: str) -> "list[_RuntimePublish]":
    """Every push in *text_lower* run under a runtime-resolved program word.

    Line continuations are folded first, as the shell folds them before it
    reads words.
    """
    return [publish for _command, publish in _read_commands(text_lower) if publish is not None]


def _commands_without_runtime_publish(text_lower: str) -> "list[str]":
    """The commands of *text_lower* that run no runtime-program publish."""
    return [command for command, publish in _read_commands(text_lower) if publish is None]


def _read_commands(text_lower: str, nesting: int = 0) -> "list[tuple[str, _RuntimePublish | None]]":
    """Each top-level command of *text_lower* with the runtime publish it runs.

    Comments and heredoc bodies are cut first, so a quote inside one cannot
    reopen the command text around it.  A comment, and the body of a heredoc
    fed to a ``_HEREDOC_SINKS`` command, is returned as a command of its own
    that runs no runtime publish, so a literal publish inside one still
    reaches the fallback.  Any other heredoc body is read as command text in
    its own right, since what consumes it may run it; past
    ``_HEREDOC_NESTING_LIMIT`` nested bodies, or at a heredoc delimiter that
    cannot be read, the text is one unverifiable publish.  Line continuations
    are folded after the cut, as a comment ends at its newline even when a
    backslash precedes it.  A word-start ``!(`` is read both as a negated
    subshell and as an extglob group, since ``extglob`` may be on.
    """
    if "push" not in _dequote_token(
        _decode_shell_quoted_literals(_fold_line_continuations(text_lower))
    ):
        return [(text_lower, None)]
    if nesting > _HEREDOC_NESTING_LIMIT:
        return [(text_lower, _RuntimePublish(None))]
    blanked = _blank_substitutions(text_lower)
    if blanked is None:
        return [(text_lower, _RuntimePublish(None))]
    outer, substitutions = blanked
    cut = _executable_text(outer)
    if cut is None:
        return [(text_lower, _RuntimePublish(None))]
    executable, data, bodies = cut
    folded = _fold_line_continuations(executable)
    pieces = _shell_commands(folded)
    if "!(" in folded:
        seen = set(pieces)
        pieces += [piece for piece in _shell_commands(folded, True) if piece not in seen]
    commands: "list[tuple[str, _RuntimePublish | None]]" = [
        (command, _command_runtime_publish(command)) for command in pieces
    ]
    for body in bodies + substitutions:
        commands += _read_commands(body, nesting + 1)
    return commands + [(span, None) for span in data if span.strip()]


class _Substitution(NamedTuple):
    """A substitution or expansion found in a text, by the span of its body."""

    start: int
    end: int
    #: True for a command or process substitution, whose body is command text.
    command: bool
    #: For a ``${...}`` expansion, the spans of the command substitution
    #: bodies nested in it, at any depth.
    inner: "tuple[tuple[int, int], ...]" = ()


#: How deeply substitutions nested in one another are read.
_SUBSTITUTION_DEPTH_LIMIT = 32


def _blank_substitutions(text: str) -> "tuple[str, list[str]] | None":
    """*text* with each substitution body that could desync it blanked, and the command bodies.

    Bash reads the body of ``$(...)``, a backtick pair, ``<(...)``,
    ``>(...)`` and ``${...}`` in a quote context of its own, even inside
    double quotes, so a quote, escape, comment or heredoc inside one must not
    reach the reading of the text around it.  A body holding any of those
    (``_DESYNCING_BODY_TEXT``) is replaced by ``_`` (``${...}`` by ``@`` when
    it holds ``@`` or ``*``, which can expand to several words); any other is
    kept, so a program word keeps its spelling.  Each command or process
    substitution body, including one nested in ``${...}``, is returned to be
    read on its own.  None when a body's end cannot be found, since then no
    command after it can be placed.
    """
    scanned = _scan_substitutions(text, 0, "", 0)
    if scanned is None:
        return None
    _end, found = scanned
    out: list[str] = []
    bodies: list[str] = []
    pos = 0
    for item in found:
        body = text[item.start : item.end]
        out.append(text[pos : item.start])
        if not any(token in body for token in _DESYNCING_BODY_TEXT):
            out.append(body)
        else:
            out.append("_" if item.command or not ("@" in body or "*" in body) else "@")
        if item.command:
            bodies.append(body)
        bodies.extend(text[start:end] for start, end in item.inner)
        pos = item.end
    out.append(text[pos:])
    return "".join(out), bodies


#: Body text the reading around a substitution would take for its own syntax.
_DESYNCING_BODY_TEXT = ("'", '"', "\\", "`", "#", "<<")


#: Each opener ``_scan_substitutions`` follows, with its closer and whether
#: its body is command text.
_SUBSTITUTION_OPENERS = (
    ("$(", ")", True),
    ("<(", ")", True),
    (">(", ")", True),
    ("${", "}", False),
)


def _at_command_position(text: str, offset: int) -> bool:
    """True if a word at *offset* begins a command rather than being an argument.

    A reserved word like ``case``/``esac`` is a keyword only at the start of a
    command: after a separator (``;`` ``&`` ``|`` newline), an opener (``(`` ``{``
    a backtick, the ``$(`` of a substitution), a ``;;`` case terminator, or a
    command-leading keyword (``do``/``then``/``else``/``elif``).  As an argument
    (``echo case``) it is ordinary data and names nothing.
    """
    index = offset - 1
    while index >= 0 and text[index] in " \t":
        index -= 1
    if index < 0 or text[index] in "(){};&|\n`":
        return True
    end = index + 1
    while index >= 0 and (text[index].isalnum() or text[index] == "_"):
        index -= 1
    return text[index + 1 : end] in {"do", "then", "else", "elif"}


def _scan_substitutions(
    text: str, start: int, closer: str, level: int
) -> "tuple[int, list[_Substitution]] | None":
    """Read *text* from *start* in a fresh quote context up to *closer*.

    Returns the index just past the closer (the end of the text for ``""``)
    and the substitutions found on the way, outermost only; None when the
    closer never comes, the nesting passes ``_SUBSTITUTION_DEPTH_LIMIT``, a
    heredoc in it cannot be read.  A ``case`` inside a ``)``-closed body is
    followed by tracking ``case``/``esac`` nesting, so its pattern parens are not
    read as the body's closer; the body's own close is found after ``esac``.
    """
    if level > _SUBSTITUTION_DEPTH_LIMIT:
        return None
    found: list[_Substitution] = []
    pos = start
    state = 0
    depth = 0
    case_depth = 0
    pending: "list[_Heredoc]" = []
    while True:
        restart = None
        for step in _iter_shell_chars(text[pos:], state):
            offset = pos + step.offset
            char = step.char
            if closer == "`" and len(step.text) == 1 and char == "`" and step.state != 1:
                return offset + 1, found
            if step.active and char == "#" and _opens_comment(text, offset):
                newline = text.find("\n", offset)
                if newline != -1:
                    restart = (newline, 0)
                break
            opened = _opened_substitution(text, offset, step)
            if opened is not None:
                item = _scan_one_substitution(text, offset, opened, level)
                if item is None:
                    return None
                found.append(item)
                restart = (item.end + len(opened[1]), 2 if step.state == 2 else 0)
                break
            if not step.active:
                continue
            if (
                closer == ")"
                and char == "c"
                and _word_at(text, offset, "case")
                and _at_command_position(text, offset)
            ):
                case_depth += 1
                continue
            if (
                closer == ")"
                and char == "e"
                and case_depth
                and _word_at(text, offset, "esac")
                and _at_command_position(text, offset)
            ):
                case_depth -= 1
                continue
            if closer in (")", "}") and char == ("(" if closer == ")" else "{"):
                if closer == ")" and case_depth:
                    # A pattern group inside a ``case`` body, not the body's close.
                    continue
                depth += 1
            elif closer in (")", "}") and char == closer:
                if closer == ")" and case_depth:
                    # A ``case`` pattern terminator, not the body's close.
                    continue
                if depth == 0:
                    return offset + 1, found
                depth -= 1
            heredoc = _heredoc_at(text, offset)
            if heredoc is _UNREADABLE_HEREDOC:
                return None
            if heredoc is not None:
                pending.append(heredoc)
            if char == "\n" and pending:
                body_end = _heredoc_bodies_end(text, offset + 1, pending)
                if body_end == _UNREADABLE:
                    return None
                pending = []
                restart = (body_end, 0)
                break
        if restart is None:
            return (len(text), found) if not closer else None
        pos, state = restart


def _scan_one_substitution(
    text: str, offset: int, opened: "tuple[int, str, bool]", level: int
) -> "_Substitution | None":
    """The substitution whose opener *opened* starts at *offset*; None if unreadable."""
    width, closer, command = opened
    inner = _scan_substitutions(text, offset + width, closer, level + 1)
    if inner is None:
        return None
    inner_end, inner_found = inner
    nested: "list[tuple[int, int]]" = []
    if not command:
        for item in inner_found:
            if item.command:
                nested.append((item.start, item.end))
            nested.extend(item.inner)
    return _Substitution(offset + width, inner_end - len(closer), command, tuple(nested))


def _opened_substitution(text: str, offset: int, step) -> "tuple[int, str, bool] | None":
    """``(opener width, closer, is command text)`` if a substitution opens at *offset*.

    ``$(``, ``${`` and a backtick open one unquoted or inside double quotes;
    ``<(`` and ``>(`` only unquoted.
    """
    if len(step.text) != 1 or step.state == 1:
        return None
    if step.char == "`":
        return 1, "`", True
    for opener, closer, command in _SUBSTITUTION_OPENERS:
        if text.startswith(opener, offset) and (opener[0] == "$" or step.active):
            return 2, closer, command
    return None


#: How many heredoc bodies nested one inside another are read.
_HEREDOC_NESTING_LIMIT = 8

#: Characters that end the word a heredoc's delimiter is read from.
_HEREDOC_TAG_BREAK = frozenset(" \t\n;&|()<>")

#: How far past ``<<`` a heredoc delimiter is read.
_HEREDOC_TAG_WINDOW = 256


class _Heredoc(NamedTuple):
    """A heredoc whose body starts after the next newline."""

    tag: str
    strip_tabs: bool
    #: True when the command it feeds is a ``_HEREDOC_SINKS`` subcommand.
    data: bool = False
    #: True when any part of the delimiter word is quoted or escaped, so the
    #: body is read raw, without line continuations.
    quoted: bool = False


#: A ``<<`` whose delimiter cannot be read, so where its body ends is unknown.
_UNREADABLE_HEREDOC = _Heredoc("", False)

#: What ``_next_cut`` returns for a heredoc whose delimiter cannot be read.
_UNREADABLE = -1

#: Programs that read a heredoc only as data and print none of it back as
#: text another program could run, each with the subcommands that do: the
#: body is a pull request, issue or release text, or a commit, note or tag
#: message.
_HEREDOC_SINKS = {
    "gh": frozenset({"issue", "pr", "release"}),
    "git": frozenset({"commit", "notes", "tag"}),
}

#: Programs that may share a command text with a sink, each with the
#: subcommands it may run (an empty set: any arguments): none of them writes a
#: file, sets configuration, defines anything in the shell, or runs code its
#: arguments carry, so none can arrange for a sink's input to be executed.
_SINK_COMPANIONS = {
    ":": frozenset(),
    "[": frozenset(),
    "cd": frozenset(),
    "echo": frozenset(),
    "git": frozenset({"add", "diff", "log", "rev-parse", "show", "status"}),
    "ls": frozenset(),
    "pwd": frozenset(),
    "sleep": frozenset(),
    "test": frozenset(),
    "true": frozenset(),
}

#: A sink option that hands the body to an editor, which can be any program:
#: ``--edit``, ``--editor``, or a short-option cluster carrying ``e``.
_SINK_EDITOR_OPTION_RE = re.compile(r"--edit(?:or)?\Z|-[a-z]*e[a-z]*\Z")

#: A ``git`` option that writes its output to a file: ``--output=FILE`` and
#: ``--output-directory`` (``git log``/``diff``/``format-patch``).  An inert
#: companion carrying one can plant a hook, filter or config file the sink
#: then runs, so it is not inert.
_GIT_OUTPUT_FILE_OPTION_RE = re.compile(r"--output(?:-directory)?(?:=|\Z)")

#: Text whose presence anywhere outside the data spans voids the sink
#: exemption: an output redirection can write a hook, filter or config file,
#: and a substitution or ``${...}`` expansion can run or assign anything.
_SINK_VOIDING_TEXT = (">", "$(", "`", "<(", "${")


def _executable_text(text: str) -> "tuple[str, list[str], list[str]] | None":
    """*text* without its comments and heredoc bodies, then the data spans, then the bodies.

    A comment opens at an unquoted ``#`` that starts a word and runs to the end
    of the line; the quote state is restarted after it, because a quote inside
    a comment opens nothing.  A heredoc body runs from the newline that ends
    the line carrying its ``<<`` to its delimiter line, or to the end of the
    text when no delimiter line comes, at any depth.  Comments, and the bodies
    of heredocs fed to a ``_HEREDOC_SINKS`` command when ``_sinks_hold`` for
    the text left, are data spans; every
    other body is returned to be read as command text.  None when a heredoc
    delimiter cannot be read, since then no command after it can be placed.
    """
    kept: list[str] = []
    data: list[str] = []
    bodies: list[str] = []
    sink_bodies: list[str] = []
    pos = 0
    while pos < len(text):
        resume = _next_cut(text, pos, kept, (data, bodies, sink_bodies))
        if resume == _UNREADABLE:
            return None
        if resume is None:
            break
        pos = resume
    if sink_bodies:
        holds = _sinks_hold(_fold_line_continuations("".join(kept)))
        (data if holds else bodies).extend(sink_bodies)
    return "".join(kept), data, bodies


def _option_is_or_hides(word: str, pattern: "re.Pattern[str]") -> bool:
    """True if dequoted *word* matches *pattern*, or is an option whose name
    carries an unresolved expansion that could resolve to one.

    ``--out""put`` dequotes to ``--output`` and ``--e""dit`` to ``--edit``, and
    a ``--$opt`` or ``-$x`` could expand to either, so a quoted fragment or an
    expansion in the option name is treated as a match (fail closed).
    """
    decoded = _dequote_token(word)
    if pattern.match(decoded):
        return True
    name = decoded.split("=", 1)[0]
    return name.startswith("-") and ("$" in name or "`" in name)


def _sinks_hold(executable: str) -> bool:
    """True if nothing in *executable* can make a sink run its heredoc.

    Every command must run a sink subcommand with no editor option, or a
    ``_SINK_COMPANIONS`` program (with one of its listed subcommands when it
    lists any), each spelled as the plain word with no assignment before it;
    no function may be defined; and no ``_SINK_VOIDING_TEXT`` may appear.
    Anything else could install a hook, filter, editor, alias or ``PATH`` entry
    that hands the body to a program.
    """
    if any(token in executable for token in _SINK_VOIDING_TEXT):
        return False
    if _FUNCTION_DEFINITION_RE.search(executable):
        return False
    for command in _shell_commands(executable):
        words = _split_shell_words(command)
        index = 0
        while index < len(words):
            after = _after_redirection(words, index)
            if after is None:
                break
            index = after
        if index == len(words):
            continue
        program = words[index]
        if _ASSIGNMENT_WORD_RE.match(program):
            # ``PATH=. git ...`` looks ``git`` up somewhere else.
            return False
        subcommand = words[index + 1] if index + 1 < len(words) else ""
        if subcommand in _HEREDOC_SINKS.get(program, frozenset()):
            if any(
                _option_is_or_hides(word, _SINK_EDITOR_OPTION_RE) for word in words[index + 2 :]
            ):
                return False
            continue
        allowed = _SINK_COMPANIONS.get(program)
        if allowed is None or (allowed and subcommand not in allowed):
            return False
        if program == "git" and any(
            _option_is_or_hides(word, _GIT_OUTPUT_FILE_OPTION_RE) for word in words[index + 1 :]
        ):
            # ``git log --output=.git/hooks/...`` writes a file the sink runs.
            return False
    return True


#: A function definition: ``name()``, ``name ()``, or the ``function`` keyword.
_FUNCTION_DEFINITION_RE = re.compile(r"\(\s*\)|(?<![\w-])function(?![\w-])")


def _next_cut(text: str, pos: int, kept: list, cut: "tuple[list, list, list]") -> "int | None":
    """Walk *text* from *pos* to the next comment or heredoc body and cut it.

    Appends the text before the cut to *kept* and the cut span to the data,
    body or sink-body list of *cut*, and returns where the walk resumes in
    quote state 0; None at the end, ``_UNREADABLE`` at a heredoc whose
    delimiter cannot be read.
    """
    data = cut[0]
    pending: "list[_Heredoc]" = []
    command_start = pos
    sink_at: "dict[int, bool]" = {}
    for step in _iter_shell_chars(text[pos:]):
        if not step.active:
            continue
        offset = pos + step.offset
        char = step.char
        if char == "#" and _opens_comment(text, offset):
            end = text.find("\n", offset)
            end = len(text) if end == -1 else end
            kept.append(text[pos:offset])
            data.append(text[offset:end])
            if not pending or end == len(text):
                return end
            # The comment ends the line carrying the ``<<``; the body follows it.
            body_end = _heredoc_bodies_end(text, end + 1, pending)
            if body_end == _UNREADABLE:
                return _UNREADABLE
            kept.append("\n")
            _store_bodies(text[end + 1 : body_end], pending, cut)
            return body_end
        heredoc = _heredoc_at(text, offset)
        if heredoc is _UNREADABLE_HEREDOC:
            return _UNREADABLE
        if heredoc is not None:
            # The program and subcommand are read once per command, at its first ``<<``.
            if command_start not in sink_at:
                sink_at[command_start] = _feeds_sink(text[command_start:offset])
            pending.append(heredoc._replace(data=sink_at[command_start]))
        if char in ";&|()\n":
            command_start = offset + 1
        if char == "\n" and pending:
            body_end = _heredoc_bodies_end(text, offset + 1, pending)
            if body_end == _UNREADABLE:
                return _UNREADABLE
            kept.append(text[pos : offset + 1])
            _store_bodies(text[offset + 1 : body_end], pending, cut)
            return body_end
    kept.append(text[pos:])
    return None


def _store_bodies(span: str, pending: "list[_Heredoc]", cut: "tuple[list, list, list]") -> None:
    """File the bodies *span* of *pending* heredocs as command text or as sink bodies.

    The span is a sink body only when every heredoc it holds feeds a sink, and
    no unquoted one carries an expansion bash runs while it reads the body.
    """
    _data, bodies, sink_bodies = cut
    expands = any(not heredoc.quoted for heredoc in pending) and any(
        token in span for token in ("$(", "`", "${")
    )
    sink = not expands and all(heredoc.data for heredoc in pending)
    (sink_bodies if sink else bodies).append(span)


def _feeds_sink(prefix: str) -> bool:
    """True if command text *prefix*, up to a ``<<``, runs a ``_HEREDOC_SINKS`` subcommand.

    The program must be the bare word ``gh`` or ``git`` with no assignment
    before it, and its next word one of the sink's subcommands; redirections
    may come first.
    """
    words = _split_shell_words(prefix)
    index = 0
    while index < len(words):
        after = _after_redirection(words, index)
        if after is None:
            break
        index = after
    if index + 1 >= len(words):
        return False
    return words[index + 1] in _HEREDOC_SINKS.get(words[index], frozenset())


def _heredoc_at(text: str, offset: int) -> "_Heredoc | None":
    """The heredoc whose ``<<`` operator starts at *offset*, if one does.

    A delimiter that cannot be read whole -- empty, longer than
    ``_HEREDOC_TAG_WINDOW``, or carrying a substitution bash reads as one word
    with it -- is ``_UNREADABLE_HEREDOC``.
    """
    if not text.startswith("<<", offset) or text.startswith("<<<", offset):
        return None
    if offset and text[offset - 1] == "<":
        return None
    after = offset + 2
    strip_tabs = text.startswith("-", after)
    after += 1 if strip_tabs else 0
    while after < len(text) and text[after] in " \t":
        after += 1
    window = text[after : after + _HEREDOC_TAG_WINDOW + 1]
    end = after
    broke = False
    for step in _iter_shell_chars(window):
        if step.active and step.char in _HEREDOC_TAG_BREAK:
            broke = True
            break
        end = after + step.offset + len(step.text)
    raw_tag = text[after:end]
    if not raw_tag or len(raw_tag) > _HEREDOC_TAG_WINDOW or not (broke or end == len(text)):
        return _UNREADABLE_HEREDOC
    if "$(" in raw_tag or "${" in raw_tag or "`" in raw_tag or text[end : end + 1] == "(":
        return _UNREADABLE_HEREDOC
    tag = _heredoc_delimiter(raw_tag)
    if tag is None:
        return _UNREADABLE_HEREDOC
    quoted = any(char in _fold_line_continuations(raw_tag) for char in "'\"\\")
    return _Heredoc(tag, strip_tabs, quoted=quoted)


def _heredoc_delimiter(raw: str) -> "str | None":
    """The delimiter bash reads from raw heredoc word *raw*: quote removal only.

    An unquoted backslash is removed; inside double quotes one is removed only
    before ``$``, a backtick, ``"``, ``\\`` or a newline.  ``$'...'`` is read
    as its literal text and ``$"..."`` as double quotes.  ``$`` is otherwise
    literal, since bash expands nothing in a delimiter.  None when a ``$'...'``
    span carries an escape, which this reading does not decode.
    """
    out: list[str] = []
    ansi: "list[str] | None" = None
    state = 0
    dollars = 0
    for step in _iter_shell_chars(raw):
        quote_syntax = len(step.text) == 1 and step.char in "'\"" and step.state != state
        state = step.state
        if ansi is not None:
            if "\\" in step.text:
                return None
            if not quote_syntax:
                ansi.append(step.text)
                continue
            out.extend(ansi)
            ansi = None
        elif quote_syntax:
            if step.state != 0 and dollars % 2:
                out.pop()
                ansi = [] if step.ansi else None
        elif len(step.text) == 2:
            keep_both = step.state == 2 and step.char not in '$`"\\\n'
            # A backslash-newline is a line continuation: both characters go.
            out.append("" if step.char == "\n" else step.text if keep_both else step.char)
        else:
            out.append(step.char)
        dollars = dollars + 1 if len(step.text) == 1 and step.char == "$" and not state else 0
    return "".join(out)


def _heredoc_bodies_end(text: str, start: int, pending: "list[_Heredoc]") -> int:
    """Where the bodies of *pending* heredocs, starting at *start*, end.

    Each body ends after the first line that is exactly its delimiter (leading
    tabs removed for ``<<-``).  In the body of an unquoted delimiter a line
    ending in an unescaped backslash continues on the next line before the
    comparison, as bash joins it.  A body whose delimiter line never comes
    runs to the end of *text*, as bash reads it.  ``_UNREADABLE`` for a ``<<-``
    body of an unquoted delimiter that continues a line, whose tab stripping
    this reading does not reproduce.
    """
    pos = start
    for heredoc in pending:
        while True:
            if pos >= len(text):
                return len(text)
            line = ""
            while True:
                newline = text.find("\n", pos)
                line_end = len(text) if newline == -1 else newline
                physical = text[pos:line_end]
                pos = line_end + 1
                trailing = len(physical) - len(physical.rstrip("\\"))
                if heredoc.quoted or newline == -1 or trailing % 2 == 0:
                    line += physical
                    break
                if heredoc.strip_tabs:
                    return _UNREADABLE
                line += physical[:-1]
            if (line.lstrip("\t") if heredoc.strip_tabs else line) == heredoc.tag:
                break
    return min(pos, len(text))


def _without_judged_program_words(command: str, publishes: "list[_RuntimePublish]") -> str:
    """*command* with one occurrence of each judged publish's known words removed.

    A publish whose argv is known is judged by its target, so its program word
    and its validated flag values (each one quoted parameter expansion) do not
    make the command unverifiable.  Only those words go: any other expansion,
    including the same spelling used again elsewhere in the command, still
    counts.  A word carrying a command substitution is kept.
    """
    for publish in publishes:
        if publish.args is None:
            continue
        for raw in publish.program:
            if raw and "$(" not in raw:
                command = command.replace(raw, " ", 1)
    return command


def _runs_runtime_program_publish(text_lower: str) -> bool:
    """True if any command in *text_lower* runs a push under a runtime program word.

    Answers False when the text cannot be read, matching the normalizer pass.
    """
    try:
        return bool(_runtime_publishes(text_lower))
    except Exception:
        return False
