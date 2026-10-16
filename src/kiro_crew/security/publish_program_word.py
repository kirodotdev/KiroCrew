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
from collections import Counter
from typing import NamedTuple

from .publish_heredoc import (  # noqa: F401 -- also re-exported for callers
    _BULLET_RE,
    _CAT_PIPE_RE,
    _FILE_WRITE_RE,
    _GIT_HOOK_NAMES,
    _GIT_OUTPUT_FILE_OPTION_RE,
    _GIT_TEMPLATE_OPTION_RE,
    _GIT_TRANSPORT_PROGRAM_OPTION_RE,
    _HEREDOC_NESTING_LIMIT,
    _HEREDOC_SINKS,
    _HOOKLESS_SIBLING_RE,
    _HOOKLESS_SINKS,
    _PLAIN_RUNNER_RE,
    _PUSH_WORD_RE,
    _RUNNER_RECIPE_NAMES,
    _SINK_COMPANIONS,
    _SINK_EDITOR_OPTION_RE,
    _SINK_VALUE_OPTIONS,
    _UNREADABLE,
    _UNREADABLE_HEREDOC,
    _changes_mode,
    _Heredoc,
    _heredoc_at,
    _heredoc_bodies_end,
    _heredoc_delimiter,
    _makes_dirs,
    _store_bodies,
    _voids_sinks,
    _writes_file,
)
from .publish_subcommand import _GIT_ARG_FLAGS
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
    _skip_continuations,
    _split_push_command_segments,
    _split_shell_words,
    _word_at,
)

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
#: the lowercase spelling the floor reads, across the GNU, BSD and util-linux
#: spellings (``env -a``/``--argv0``, BSD ``env -P``, ``sudo -R``/``--chroot``).
#: Any other option takes no separate value, so the word after it is still the
#: precommand's operand or program.  A long option is also read by any prefix of
#: one of these, as ``getopt_long`` accepts an unambiguous abbreviation.
_GIT_PRECOMMAND_VALUE_OPTIONS = {
    "chroot": frozenset({"--groups", "--userspec"}),
    "doas": frozenset({"-c", "-u"}),
    "env": frozenset(
        {"-a", "-c", "-l", "-p", "-s", "-u", "--argv0", "--chdir", "--split-string", "--unset"}
    ),
    "exec": frozenset({"-a"}),
    "flock": frozenset(
        {"-c", "-e", "-w", "--command", "--conflict-exit-code", "--timeout", "--wait"}
    ),
    "ionice": frozenset(
        {"-c", "-n", "-p", "-u", "--class", "--classdata", "--pgid", "--pid", "--uid"}
    ),
    "nice": frozenset({"-n", "--adjustment"}),
    "parallel": frozenset({"-j", "-n", "-s", "--jobs"}),
    "stdbuf": frozenset({"-e", "-i", "-o", "--error", "--input", "--output"}),
    "sudo": frozenset(
        "-a -c -d -g -h -p -r -t -u --auth-type --chdir --chroot --close-from --command-timeout"
        " --group --host --login-class --other-user --prompt --role --type --user".split()
    ),
    "time": frozenset({"-f", "-o", "--format", "--output"}),
    "timeout": frozenset({"-k", "-s", "--kill-after", "--signal"}),
    "watch": frozenset({"-n", "-q", "--equexit", "--interval"}),
    "xargs": frozenset(
        "-a -d -e -i -l -n -p -s --arg-file --delimiter --max-args --max-chars --max-procs"
        " --process-slot-var".split()
    ),
}

#: Precommands whose options this table does not model in full (GNU
#: ``parallel`` has hundreds), so the arity of any option is unknown.
_UNMODELLED_OPTION_PRECOMMANDS = frozenset({"parallel"})

#: Value options of ``_GIT_PRECOMMAND_VALUE_OPTIONS`` whose other letter case
#: takes no separate value (``xargs -L 1`` and ``xargs -l``, ``sudo -p PROMPT``
#: and ``sudo -P``, ``flock -E 1`` and ``flock -e``): lowercased, either may be
#: the one written.
_CASE_AMBIGUOUS_VALUE_OPTIONS = {
    "flock": frozenset({"-e"}),
    "sudo": frozenset({"-a", "-h", "-p"}),
    "xargs": frozenset({"-e", "-i", "-l", "-p"}),
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
    closers: list[str] = []
    in_backticks = False
    for position, step in enumerate(steps):
        if step.trailing_escape:
            buf.append(step.text)
            break
        char = step.char
        if char == "`" and len(step.text) == 1 and step.state != 1:
            in_backticks = not in_backticks
        elif step.active and not in_backticks:
            if closers:
                # Only the closer of the innermost opener closes it: a ``)`` inside
                # ``${...}`` or a ``}`` inside an extglob group is text.
                if char == "(" or (char == "{" and _opens_substitution(steps, position)):
                    closers.append(")" if char == "(" else "}")
                elif char == closers[-1]:
                    closers.pop()
            elif char in "({" and _opens_substitution(steps, position):
                closers.append(")" if char == "(" else "}")
            elif char == "(" and _opens_extglob(steps, position, word_start_negation):
                closers.append(")")
            elif (
                char in ";\n()"
                or (char == "|" and not _is_noclobber_pipe(steps, position))
                or (char == "&" and not _is_redirection_amp(steps, position))
            ):
                pieces.append("".join(buf))
                buf = []
                continue
        buf.append(step.text)
    pieces.append("".join(buf))
    return [piece for piece in pieces if piece.strip()]


def _is_noclobber_pipe(steps: list, position: int) -> bool:
    """True if the ``|`` at *position* is the ``>|`` noclobber-override operator.

    A ``|`` the shell reads as part of ``>|`` (``cmd >|/dev/null``) is a
    redirection, not a pipeline boundary, so the program before it and the
    words after it are one command.  Only an active ``>`` immediately before
    makes one; ``||`` and ``|&`` still end a command.
    """
    before = steps[position - 1] if position else None
    return before is not None and before.active and before.char == ">"


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

    An unquoted command substitution or parameter expansion can span several
    words (``$(echo git)`` splits as ``$(echo`` and ``git)``, ``${1:-a b}`` as
    ``${1:-a`` and ``b}``).  The shell word ends at the first word where every
    substitution backtick is paired, every unquoted ``(`` is closed, and every
    ``$(`` / ``${`` opener outside backticks has met its closer.  An unclosed
    span runs to the last word.
    """
    depth = 0
    backticks = 0
    closers: "list[str]" = []
    for index in range(start, len(words)):
        depth += _shell_quote_walk(words[index]).paren_delta
        backticks += _active_backticks(words[index])
        _track_word_closers(closers, words[index], backticks)
        if depth <= 0 and backticks % 2 == 0 and not closers:
            return index
    return len(words) - 1


def _track_word_closers(closers: "list[str]", word: str, backticks_after: int) -> None:
    """Carry *closers*, the expansion closers still expected, across *word*.

    Read the way :func:`_top_level_basename` reads a word: text inside a
    backtick substitution is skipped, and so is single-quoted text, so a quoted
    ``}`` (``${1:-'a }'}``) closes nothing.  *backticks_after* is the running
    count of active backticks once *word* is read, which tells whether the word
    starts inside an open backtick substitution.
    """
    steps = list(_iter_shell_chars(word))
    in_backticks = (backticks_after - _active_backticks(word)) % 2 == 1
    for position, step in enumerate(steps):
        if step.char == "`" and len(step.text) == 1 and step.state != 1:
            in_backticks = not in_backticks
        elif not in_backticks and step.state != 1 and len(step.text) == 1:
            _track_closer(closers, steps, position)


def _top_level_basename(raw: str) -> str:
    """The part of raw shell word *raw* after its last top-level ``/``.

    A ``/`` inside a substitution or a parameter expansion is part of what the
    shell produces, not a path separator of the word, so it does not count.
    """
    closers: "list[str]" = []
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
        elif char == "/" and not closers:
            # Quoted or escaped, a ``/`` is still a literal path separator; the
            # quote it sits in is reopened in front of what follows it.
            last = step.offset + len(step.text) - 1
            reopen = {1: "'", 2: '"'}.get(step.state, "")
        elif step.state != 1 and len(step.text) == 1:
            _track_closer(closers, steps, position)
    # A Windows path separates with ``\``; the program is what follows the last.
    return (reopen + raw[last + 1 :]).rsplit("\\", 1)[-1]


def _track_closer(closers: "list[str]", steps: list, position: int) -> None:
    """Update *closers*, the closers still expected, for the character at *position*.

    A substitution's ``(`` or ``{`` opens one, and so does any ``(`` inside
    one; a character closes only the innermost opener it matches, so a ``)``
    inside ``${...}`` (``${1:-x)/adb}``) or a ``}`` inside ``$(...)`` is text.
    """
    char = steps[position].char
    if char == "(" and (closers or _opens_substitution(steps, position)):
        closers.append(")")
    elif char == "{" and _opens_substitution(steps, position):
        closers.append("}")
    elif closers and char == closers[-1]:
        closers.pop()


#: A word that is wholly unquoted parameter/command expansions (``$name``,
#: ``${...}``, ``$(...)``, backticks), one after another with no literal text.
#: Unquoted, every such word can expand to nothing, so it does not settle
#: program position -- the next word may be the program the shell runs.  Each
#: atom starts with ``$`` or a backtick and a name run (``\w+``) and a single
#: special parameter (``$@``/``$!``/``$$`` ...) are separate, so no ``$`` sits in
#: a repeated class: the alternation cannot backtrack on ``$!$!...``.
_VANISHING_WORD_RE = re.compile(r"\A(?:\$\{[^{}]*\}|\$\([^()]*\)|`[^`]*`|\$\w+|\$[@*#?!$-])+\Z")

#: A double-quoted span of expansions that still produce ZERO words when empty:
#: ``"$@"``, ``"${@}"``, ``"${@:2}"`` and ``"${name[@]}"`` (with optional slice),
#: one or several inside one pair of quotes (``"$@$@"``).  A ``"$x"`` or
#: ``"${a[*]}"`` yields one empty word instead, so is excluded.
_ZERO_WORD_SPAN_RE = re.compile(r'"(?:\$@|\$\{@[^{}]*\}|\$\{[A-Za-z_]\w*\[@\][^{}]*\})+"')

#: What may be left of a word once its zero-word spans and its brace and command
#: expansions are cut: unquoted parameters, each of which can expand to nothing.
_PARAMETERS_ONLY_RE = re.compile(r"(?:\$\w+|\$[@*#?!$-])*")


def _can_vanish(raw: str) -> bool:
    """True if *raw* is an expansion or glob that can produce no word at all.

    An unquoted parameter, brace or command expansion at any nesting
    (``${x:-${y:-}}``), a quoted zero-word positional or array expansion
    (``"$@"``, ``"${@}"``, ``"$@$@"``, ``"${a[@]}"``), and an unquoted glob or
    extglob (which matches nothing under ``nullglob``) each can leave the
    program position to the next word.
    """
    decoded = _decode_shell_quoted_literals(raw)
    if _VANISHING_WORD_RE.match(decoded) or _expansions_only(decoded):
        return True
    return any(char in _active_glob_view(decoded) for char in "*?[") or _has_extglob(decoded)


def _expansions_only(word: str) -> bool:
    """True if *word* is nothing but expansions that can each produce no word.

    Zero-word quoted spans are cut first; what is left must be unquoted
    ``${...}``, ``$(...)`` and backtick expansions and unquoted parameters.  A
    word whose expansions cannot be placed counts as able to vanish.
    """
    rest = _ZERO_WORD_SPAN_RE.sub("", word)
    scanned = _scan_substitutions(rest, 0, "", 0)
    if scanned is None:
        return True
    kept: "list[str]" = []
    pos = 0
    for item in scanned[1]:
        width = 1 if rest[item.start - 1] == "`" else 2
        if item.quoted or rest[item.start - width] in "<>":
            # A quoted expansion is one word; a process substitution is a path.
            return False
        kept.append(rest[pos : item.start - width])
        pos = item.end + 1
    kept.append(rest[pos:])
    return _PARAMETERS_ONLY_RE.fullmatch("".join(kept)) is not None


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
    if "$" in base or "`" in base or _has_extglob(base) or _has_extglob(posix_base):
        return True
    if ("$" in posix_base or "`" in posix_base) and (
        _tail_can_name_git(posix_base) or _dequote_token(base) in _GIT_PROGRAM_NAMES
    ):
        return True
    return any(
        _BRACKET_CLASS_RE.search(_active_glob_view(view))
        or _glob_could_expand_to(_active_glob_view(view), _GIT_PROGRAM_NAMES)
        for view in (base, posix_base)
    )


#: A POSIX bracket expression inside a glob (``[[:lower:]]``, ``[[=i=]]``,
#: ``[[.i.]]``), which the glob translator does not read, so a program word
#: carrying one counts as able to name git.
_BRACKET_CLASS_RE = re.compile(r"\[[^\]]*\[[:=.]")


#: The program names git runs under: ``git``, and ``git.exe`` on Windows.
_GIT_PROGRAM_NAMES = ("git", "git.exe")

#: A parameter expansion's name: a run of word characters or one special parameter.
_EXPANSION_NAME_RE = re.compile(r"\w+|[@*#?!$0-9-]")


def _tail_can_name_git(view: str) -> bool:
    """True unless basename *view* ends in literal text no git program name ends in.

    The text after the last expansion in *view* is the end of whatever name the
    shell builds, so ``"$X\\bin\\docker.exe"`` (read with ``\\`` as an escape)
    can only name a program ending in ``docker.exe``.  The tail is read with
    backslashes kept and dropped, since an unquoted ``\\g`` is ``g``.  A word
    carrying a command substitution, one whose expansions cannot be placed, or a
    tail carrying a glob character counts as able to name git.
    """
    scanned = _scan_substitutions(view, 0, "", 0)
    if scanned is None:
        return True
    start, state = 0, 0
    for item in scanned[1]:
        if item.command:
            return True
        # Past the expansion's closing ``}``, in the quote state it closed in.
        start, state = item.end + 1, 2 if item.quoted else 0
    rest = view[start:]
    tail_at = 0
    for step in _iter_shell_chars(rest, state):
        if step.state != 1 and len(step.text) == 1 and step.char == "$":
            name = _EXPANSION_NAME_RE.match(rest, step.offset + 1)
            tail_at = name.end() if name else step.offset + 1
    tail = rest[tail_at:]
    if any(char in tail for char in "*?["):
        return True
    kept = tail.replace("'", "").replace('"', "")
    return any(
        git.endswith(text) for git in _GIT_PROGRAM_NAMES for text in (kept, _dequote_token(tail))
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
        closers: "list[str]" = []
        cut = None
        for position, step in enumerate(steps):
            char = step.char
            if not step.active:
                continue
            if char in "(){}" and (closers or char in "({"):
                _track_closer(closers, steps, position)
            elif closers or position == 0 or char not in "<>&":
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
    #: after ``push`` is exactly these words.  None when the publish's arguments
    #: cannot be known.
    args: "list[str] | None"
    #: The raw program word, then each validated flag value.
    program: "tuple[str, ...]" = ()


#: The global flags of ``_GIT_ARG_FLAGS`` whose value may be one quoted parameter
#: expansion without moving the push target.  ``-c`` is not one: its value can be
#: ``remote.origin.push=feature:main``, and lowercased it cannot be told from ``-C``.
_TARGET_NEUTRAL_GIT_FLAGS = frozenset({"--git-dir", "--work-tree", "--namespace"})


def _literal_git_prefix(words: "list[str]", start: int, stop: int) -> "list[str] | None":
    """The validated flag values if ``words[start:stop]`` reach git as known words.

    The value of a flag in ``_TARGET_NEUTRAL_GIT_FLAGS`` may be one quoted
    parameter expansion (``--git-dir "$REPO/.git"``): it reaches git as one word
    and does not move the push target.  Returns those values, or None when any
    other word in the span carries an expansion or a glob.
    """
    values: list[str] = []
    index = start
    while index < stop:
        word = words[index]
        value = words[index + 1] if index + 1 < stop else ""
        if _dequote_token(word) in _TARGET_NEUTRAL_GIT_FLAGS and _ONE_WORD_EXPANSION_RE.match(
            value
        ):
            values.append(value)
            index += 2
            continue
        flag, equals, attached = word.partition("=")
        if equals and flag in _TARGET_NEUTRAL_GIT_FLAGS and _ONE_WORD_EXPANSION_RE.match(attached):
            # ``--git-dir="$REPO/.git"`` is the same one-word value, attached.
            values.append(word)
            index += 1
            continue
        if any(char in word for char in "$`*?["):
            return None
        index += 1
    return values


#: The short options ``git push`` takes, with ``-h`` for its usage: on any
#: other letter after a single ``-`` it exits with a usage error before it
#: reads a remote, whatever its other options.
_GIT_PUSH_SHORT_OPTIONS = frozenset("46dfhnoquv")


def _git_push_rejects(args: "list[str]") -> bool:
    """True if git's argv after ``push``, *args*, opens with an unknown short option.

    The word is read lowercased, which is sound because no short option of
    ``git push`` is an upper-case letter.  ``dvc push -r storage`` behind a
    program word that may be git publishes nothing as a git push.
    """
    return (
        bool(args)
        and re.fullmatch(r"-[a-z0-9][\w-]*", args[0]) is not None
        and (args[0][1] not in _GIT_PUSH_SHORT_OPTIONS)
    )


def _command_runtime_publish(command: str) -> "_RuntimePublish | None":
    """The push *command* runs under a runtime-resolved program word, if any.

    A precommand option whose two letter cases differ on taking a value
    (``_CASE_AMBIGUOUS_VALUE_OPTIONS``) is read both ways, since the floor reads
    lowercased text; a publish either reading finds is found, and two readings
    that disagree make it unverifiable.
    """
    ambiguous: "list[bool]" = []
    first = _walk_program_words(command, False, ambiguous)
    if not ambiguous:
        return first
    second = _walk_program_words(command, True, [])
    if first is None or first == second:
        return second
    return first if second is None else first._replace(args=None)


def _walk_program_words(
    command: str, drop_ambiguous: bool, ambiguous: "list[bool]"
) -> "_RuntimePublish | None":
    """One reading of the push *command* runs under a runtime-resolved program word.

    Program position opens at the command's first word, after a word in
    ``_GIT_PROGRAM_LEAD_WORDS``, after the name that follows ``function``, and
    after a precommand's options and positional operands.  Leading
    ``VAR=value`` assignments and redirections keep it open.  Inside a
    precommand a runtime program word that does not run ``push`` is read as an
    operand, since its value may be an option value or another precommand.
    With *drop_ambiguous*, a case-ambiguous option takes no value; reaching a
    precommand that has one appends to *ambiguous*.

    A word is read as the shell hands it over, ANSI-C quoting decoded
    (``$'env'`` is ``env``).  Once a word whose arity cannot be known has been
    read -- a runtime word that may be a dispatcher or an option, an option
    spelled with a glob, a long-option abbreviation, an option of a precommand
    whose options are not modelled -- the program the shell finally runs is not
    known either: a literal word where the walk would stop may instead be an
    option value or operand, so any later runtime word running ``push`` is an
    unverifiable publish (``_later_runtime_publish``).
    """
    words = _split_glued_redirections(_split_shell_words(command))
    positionals: "int | None" = None
    value_options: "frozenset[str]" = frozenset()
    after_option = False
    argv_changed = False
    uncertain = False
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
        word = _dequote_token(_decode_shell_quoted_literals(raw))
        index = end + 1
        if after_option:
            # This word is the value of the previous precommand option, not a
            # program, reserved word or operand of its own -- unless the option
            # takes no value where it runs, when it is the program.
            after_option = False
            if _runs_push_at(words, raw, index, lookahead):
                return _RuntimePublish(None, (raw,))
            if _splits_into_words(raw) or _can_vanish(raw):
                # An unquoted expansion or glob as the value may be several words
                # or none (``env -u $1``, ``$1`` holding ``X -S bash -c``), so the
                # options, program and argv after it are not known.
                argv_changed = uncertain = True
            continue
        if positionals and (word in _GIT_PROGRAM_LEAD_WORDS or word == "function"):
            # An open precommand's operand (``flock function ...``), not shell syntax.
            positionals -= 1
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
                # The program may not be git: ``"$G" -c '<a publish>' push`` runs
                # the ``-c`` payload under a shell, so a prefix word naming
                # ``push`` leaves the argv unknown.
                prefix_names_push = any(
                    "push" in _dequote_token(word) for word in words[index : subcommand[1]]
                )
                known = one_word and values is not None
                known = known and not argv_changed and not prefix_names_push
                written = words[subcommand[1] + 1 :]
                # Only a one-word program word gives git's argv: an unquoted
                # expansion splits into any program first (``bash -c``), so an
                # argv git would reject proves nothing about it.
                if known and not uncertain and _git_push_rejects(written):
                    return None
                return _RuntimePublish(written if known else None, (raw, *(values or ())))
            # The word may vanish, or be a dispatcher (``env``, ``sudo``) running
            # the words after it, with options and operands of its own; inside an
            # open precommand it may be an option, of any arity, rather than the
            # operand it is counted as.  Program position stays open, the argv
            # of a publish after it is not known, and so is which later word is
            # the program.
            argv_changed = uncertain = True
            if positionals:
                positionals -= 1
            continue
        if positionals is not None and word.startswith("-"):
            after_option, known_arity = _option_arity(word, value_options, base_of_precommand)
            uncertain = uncertain or not known_arity
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
                    # ``_option_arity`` above, so leave its result.
                    after_option = True
            continue
        base = _top_level_basename(word)
        if base == "coproc" and index + 1 < len(words) and words[index + 1] == "{":
            # ``coproc NAME { ... }``: the name labels the group, which runs next.
            index += 1
        if base in _GIT_PRECOMMAND_POSITIONALS:
            positionals = _GIT_PRECOMMAND_POSITIONALS[base]
            value_options = _GIT_PRECOMMAND_VALUE_OPTIONS.get(base, frozenset())
            if base in _CASE_AMBIGUOUS_VALUE_OPTIONS:
                ambiguous.append(True)
                if drop_ambiguous:
                    value_options = value_options - _CASE_AMBIGUOUS_VALUE_OPTIONS[base]
            after_option = False
            argv_changed = argv_changed or base in _ARGV_CHANGING_PRECOMMANDS
            base_of_precommand = base
            continue
        if _can_vanish(raw):
            # A word that can expand to nothing fills no operand slot and leaves
            # program position to the next word, inside a precommand or not; it
            # may instead be a program running the words after it.
            argv_changed = uncertain = True
            continue
        if positionals:
            positionals -= 1
            continue
        if base in ("git", "git.exe"):
            redirected = _redirected_git_push(
                words, index, lookahead, raw, argv_changed or uncertain
            )
            if redirected is not None:
                return redirected
        break
    return _later_runtime_publish(words, index, lookahead) if uncertain else None


def _redirected_git_push(
    words: "list[str]",
    after: int,
    lookahead: "dict[int, tuple[str, int] | None]",
    raw: str,
    unknown: bool,
) -> "_RuntimePublish | None":
    """A literal ``git`` program word, ending before *after*, running ``push``
    with a redirection between them (``git </dev/null push origin main``).

    The shell removes the redirection before git runs, but the literal-push
    reading does not place a ``push`` behind one, so this pass judges it: by
    its target when every word up to ``push`` is literal and the argv is known
    (*unknown* is False), else as unverifiable.
    """
    subcommand = _git_subcommand(words, after, lookahead)
    if subcommand is None or subcommand[0] != "push":
        return None
    # The shell consumes each redirection and its target, whatever it expands to,
    # so only the other words up to ``push`` reach git.
    reached: "list[str]" = []
    index = after
    while index <= subcommand[1]:
        past = _after_redirection(words, index)
        if past is None:
            reached.append(words[index])
            index += 1
        else:
            index = past
    if len(reached) == subcommand[1] + 1 - after:
        return None
    values = _literal_git_prefix(reached, 0, len(reached))
    known = values is not None and not unknown
    return _RuntimePublish(words[subcommand[1] + 1 :] if known else None, (raw, *(values or ())))


def _runs_push_at(
    words: "list[str]", raw: str, after: int, lookahead: "dict[int, tuple[str, int] | None]"
) -> bool:
    """True if raw word *raw*, ending before *after*, is a runtime program running ``push``."""
    if not _runtime_program(raw):
        return False
    subcommand = _git_subcommand(words, after, lookahead)
    return subcommand is not None and subcommand[0] == "push"


def _later_runtime_publish(
    words: "list[str]", index: int, lookahead: "dict[int, tuple[str, int] | None]"
) -> "_RuntimePublish | None":
    """An unverifiable publish if a word from *index* on is a runtime program running ``push``.

    Used once the walk cannot tell which word is the program, so every later
    word is read as one it might be.
    """
    while index < len(words):
        after = _after_redirection(words, index)
        if after is not None:
            index = after
            continue
        end = _word_span_end(words, index)
        raw = " ".join(words[index : end + 1])
        index = end + 1
        if _runs_push_at(words, raw, index, lookahead):
            return _RuntimePublish(None, (raw,))
    return None


def _option_arity(
    word: str, value_options: "frozenset[str]", precommand: str
) -> "tuple[bool, bool]":
    """Whether precommand option *word* takes the NEXT word, and whether that is known.

    A short-option cluster is read the way ``getopt`` reads it: the first
    letter that takes a value takes the rest of the cluster, or the next word
    when it is the cluster's last letter (``env -iu NAME``).  A long option
    without ``=`` that is a strict prefix of a value-taking one is read as that
    abbreviation (``env --un NAME``), but may instead be a no-value option of
    the same prefix (``sudo --login``), so its arity is not known.  Neither is
    that of an option carrying a glob character, which may expand to any
    option, nor of any option of an ``_UNMODELLED_OPTION_PRECOMMANDS`` program.
    """
    known = precommand not in _UNMODELLED_OPTION_PRECOMMANDS and not any(
        char in word for char in "*?["
    )
    if word.startswith("--"):
        if "=" in word or word in value_options:
            return word in value_options, known
        abbreviates = len(word) > 2 and any(
            option.startswith(word) for option in value_options if option.startswith("--")
        )
        return abbreviates, known and not abbreviates
    for position, letter in enumerate(word[1:], start=1):
        if f"-{letter}" in value_options:
            return position == len(word) - 1, known
    return False, known


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
    reaches the fallback.  So is a double-quoted ``$(cat <<'TAG' ...)`` whose
    text feeds only a sink or an inert companion (``_prints_into_data``).
    Any other heredoc body is read as command text in
    its own right, since what consumes it may run it; past
    ``_HEREDOC_NESTING_LIMIT`` nested bodies, or at a heredoc delimiter that
    cannot be read, the text is one unverifiable publish.  Line continuations
    are folded after the cut, as a comment ends at its newline even when a
    backslash precedes it.  A word-start ``!(`` is read both as a negated
    subshell and as an extglob group, since ``extglob`` may be on.
    """
    if not _PUSH_WORD_RE.search(
        _dequote_token(_decode_shell_quoted_literals(_fold_line_continuations(text_lower)))
    ):
        return [(text_lower, None)]
    if nesting > _HEREDOC_NESTING_LIMIT:
        return [(text_lower, _RuntimePublish(None))]
    blanked = _blank_substitutions(text_lower)
    if blanked is None:
        return [(text_lower, _RuntimePublish(None))]
    outer, substitutions, printers, plain = blanked
    cut = _executable_text(outer, top=nesting == 0)
    if cut is None:
        return [(text_lower, _RuntimePublish(None))]
    executable, data, bodies, _bullets = cut
    printed = [text_lower[start:end] for start, end in printers]
    if printed and _prints_into_data(plain):
        data = data + printed
    elif printed and _prints_bullets(plain):
        substitutions = substitutions + [_BULLET_RE.sub(r"\1:", body) for body in printed]
    else:
        substitutions = substitutions + printed
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
    #: True when it opens inside double quotes, so its output is never split
    #: into words or globbed.
    quoted: bool = False


#: How deeply substitutions nested in one another are read.
_SUBSTITUTION_DEPTH_LIMIT = 32


def _blank_substitutions(text: str) -> "tuple[str, list[str], list[tuple[int, int]], str] | None":
    """*text* with each substitution body that could desync it blanked, the
    command bodies, the spans of the heredoc-printer bodies, and *text* with
    each heredoc printer reduced to a plain word.

    Bash reads the body of ``$(...)``, a backtick pair, ``<(...)``,
    ``>(...)`` and ``${...}`` in a quote context of its own, even inside
    double quotes, so a quote, escape, comment or heredoc inside one must not
    reach the reading of the text around it.  A body holding any of those
    (``_DESYNCING_BODY_TEXT``) is replaced by ``_`` (``${...}`` by ``@`` when
    it holds ``@`` or ``*``, which can expand to several words); any other is
    kept, so a program word keeps its spelling.  Each command or process
    substitution body, including one nested in ``${...}``, is returned to be
    read on its own, except a double-quoted ``$(...)`` that
    ``_prints_heredoc_only``: that one is returned apart, and the last text
    has the whole substitution replaced by ``_``, the one word it expands to.
    None when a body's end cannot be found, since then no command after it can
    be placed.
    """
    scanned = _scan_substitutions(text, 0, "", 0)
    if scanned is None:
        return None
    _end, found = scanned
    out: list[str] = []
    plain: list[str] = []
    bodies: list[str] = []
    printers: "list[tuple[int, int]]" = []
    pos = plain_pos = 0
    for item in found:
        body = text[item.start : item.end]
        if not any(token in body for token in _DESYNCING_BODY_TEXT):
            blanked = body
        else:
            blanked = "_" if item.command or not ("@" in body or "*" in body) else "@"
        out.append(text[pos : item.start] + blanked)
        pos = item.end
        if (
            item.command
            and item.quoted
            and text.startswith("$(", item.start - 2)
            and _prints_heredoc_only(body)
        ):
            plain.append(text[plain_pos : item.start - 2] + "_")
            # Past the substitution's closing ``)``.
            plain_pos = item.end + 1
            printers.append((item.start, item.end))
            continue
        plain.append(text[plain_pos : item.start] + blanked)
        plain_pos = item.end
        if item.command:
            bodies.append(body)
        bodies.extend(text[start:end] for start, end in item.inner)
    out.append(text[pos:])
    plain.append(text[plain_pos:])
    return "".join(out), bodies, printers, "".join(plain)


#: A command substitution body that only prints one heredoc: ``cat`` and one
#: ``<<TAG``, ``<<'TAG'``, ``<<"TAG"`` or ``<<-`` form of any, with only blanks
#: around them once the heredoc body is cut.  A carriage return counts as a
#: blank, so a CRLF text reads the same: bash keeps it in the delimiter word,
#: and so does the delimiter this reading compares lines against.
_HEREDOC_PRINTER_RE = re.compile(
    r"[ \t\r\n]*cat[ \t]+<<-?[ \t]*(?:'[^'\n]+'|\"[^\"\\$`\n]+\"|[a-z_][a-z0-9_]*)"
    r"[ \t\r]*\n[ \t\r\n]*\Z"
)


def _prints_heredoc_only(body: str) -> bool:
    """True if substitution *body* runs only ``cat`` printing one heredoc as written.

    Its output is then the heredoc's text, and nothing in it runs: the
    delimiter is quoted, or the body carries no ``$``, backtick or backslash
    for bash to expand.
    """
    if not body.lstrip(" \t\n").startswith("cat"):
        return False
    cut = _executable_text(body)
    if cut is None:
        return False
    kept, data, bodies, _bullets = cut
    if data or len(bodies) != 1 or _HEREDOC_PRINTER_RE.match(kept) is None:
        return False
    return "'" in kept or '"' in kept or not any(char in bodies[0] for char in "$`\\")


def _prints_into_data(plain: str) -> bool:
    """True if every heredoc printer in *plain* feeds only a sink or an inert companion.

    *plain* is the text with each printer reduced to the word it expands to,
    so ``_sinks_hold`` judges the commands it is an argument of.
    """
    cut = _executable_text(plain)
    return cut is not None and _sinks_hold(_fold_line_continuations(cut[0]))


def _data_printer_sources(sources: "list[str]") -> "frozenset[str]":
    """The texts a payload walk lifts out of *sources* for their data heredoc printers.

    A walk takes a ``$(...)`` body alone, or with the rest of the text after it
    when it cannot prove where the body closes.  Lifted out, a printer that
    ``_read_commands`` reads as data in its source has no command around it to
    say its text is only printed into a sink, while the source already reads
    both it and the text after it in place.  Empty when a source cannot be
    read, so every lifted text is then read on its own.
    """
    lifted: "set[str]" = set()
    try:
        for source in sources:
            blanked = _blank_substitutions(source) if "<<" in source and "push" in source else None
            if blanked is None or not blanked[2]:
                continue
            if not (_prints_into_data(blanked[3]) or _prints_bullets(blanked[3])):
                continue
            for start, end in blanked[2]:
                lifted.update((source[start:end], source[start:]))
    except Exception:
        return frozenset()
    return frozenset(lifted)


def _floor_segments(text_lower: str) -> "list[str]":
    """The push segments of *text_lower*, with each line of a data span marked as data.

    The floor splits a text at every newline, so a line of a sink's heredoc
    body, a printed message, or a multi-line quoted argument (``--body "...
    * push the branch"``) reaches it as a command of its own.  Such a line is
    prefixed with ``:`` so no runtime program word is read in it, while a
    literal publish it spells still reaches the literal-push reading.  A quoted
    line is marked only when every segment spelled like it is one, so a command
    line spelled the same elsewhere in the text is still read.  A line of a
    body only written to a file has its leading markdown bullet read as ``:``
    (``_bullet_lines``).
    """
    segments = _split_push_command_segments(text_lower)
    if "push" not in text_lower or "\n" not in text_lower:
        return segments
    try:
        blanked = _blank_substitutions(text_lower)
        cut = _executable_text(blanked[0], top=True) if blanked is not None else None
        wholly, reopened = _quoted_lines(cut[0]) if cut is not None else ([], [])
    except Exception:
        return segments
    if blanked is None or cut is None:
        return segments
    spans = list(cut[1]) if "<<" in text_lower else []
    printed_bullets: "list[str]" = []
    if blanked[2] and _prints_into_data(blanked[3]):
        spans += [text_lower[start:end] for start, end in blanked[2]]
    elif blanked[2] and _prints_bullets(blanked[3]):
        printed_bullets = [
            line
            for start, end in blanked[2]
            for line in text_lower[start:end].split("\n")
            if _BULLET_RE.match(line)
        ]
    lines = {line for span in spans for line in span.split("\n") if line.strip()}
    quoted, counts = Counter(wholly), Counter(segments)
    bullets = Counter(cut[3] + printed_bullets)
    marked: "list[str]" = []
    for segment in segments:
        if segment in lines or (segment.strip() and counts[segment] <= quoted[segment]):
            segment = ": " + segment
        elif segment in bullets and counts[segment] <= bullets[segment]:
            segment = _BULLET_RE.sub(r"\1:", segment, count=1)
        marked.append(segment)
    return marked + reopened


def _quoted_lines(text: str) -> "tuple[list[str], list[str]]":
    """The lines of *text* wholly inside one quoted word, and each line that
    starts inside one, read in the quote it starts in.

    A line starts inside quotes when the newline before it is quoted, and lies
    wholly inside them when the quote stays open to its end or closes with only
    blanks after it: it is part of an argument (a multi-line ``--body`` or
    ``-m`` message), not a command.  The floor's segments restart the quote
    state at every newline, so each line that starts inside quotes is also
    returned behind ``:`` and its opening quote (``echo "a`` / ``b"; "$G" push``
    reads as ``: "b"; "$G" push``), which reads the commands after its close
    as the shell does.  A line that starts inside a substitution, whose body the
    payload walk judges as a source of its own, is neither; a line that only
    reaches into one after its quote closes is still reopened.
    """
    scanned = _scan_substitutions(text, 0, "", 0)
    if scanned is None:
        return [], []
    spans = [(item.start, item.end) for item in scanned[1]]
    wholly: "list[str]" = []
    reopened: "list[str]" = []
    start: "int | None" = None
    opener = ""
    closed = beyond = False
    for step in _iter_shell_chars(text + "\n"):
        if step.char == "\n":
            end = step.offset + len(step.text) - 1
            if start is not None and not any(s <= start <= e for s, e in spans):
                reopened.append(": " + opener + text[start:end])
                if not beyond:
                    wholly.append(text[start:end])
            start = end + 1 if step.state else None
            opener = ("$'" if step.ansi else "'") if step.state == 1 else '"'
            closed = beyond = False
        elif start is not None and not step.state:
            # Past the closing quote, anything but blanks is more shell text.
            beyond = beyond or (closed and not step.char.isspace())
            closed = True
    return wholly, reopened


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
    command-leading keyword (``do``/``then``/``else``/``elif``/``if``/``while``/
    ``until``).  As an argument (``echo case``) it is ordinary data and names
    nothing.
    """
    index = offset - 1
    while index >= 0 and text[index] in " \t":
        index -= 1
    if index < 0 or text[index] in "(){};&|\n`":
        return True
    end = index + 1
    while index >= 0 and (text[index].isalnum() or text[index] == "_"):
        index -= 1
    return text[index + 1 : end] in {"do", "then", "else", "elif", "if", "while", "until"}


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
                found.append(item._replace(quoted=step.state == 2))
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
    ``<(`` and ``>(`` only unquoted.  A line continuation between the two
    characters is removed by the shell before it reads them, so ``$\\<newline>(``
    opens one too, its width counting the continuation.
    """
    if len(step.text) != 1 or step.state == 1:
        return None
    if step.char == "`":
        return 1, "`", True
    for opener, closer, command in _SUBSTITUTION_OPENERS:
        if step.char == opener[0] and (opener[0] == "$" or step.active):
            after = _skip_continuations(text, offset + 1)
            if text.startswith(opener[1], after):
                return after + 1 - offset, closer, command
    return None


def _executable_text(
    text: str, top: bool = False
) -> "tuple[str, list[str], list[str], list[str]] | None":
    """*text* without its comments and heredoc bodies, then the data spans, the
    bodies, and the body lines whose markdown bullet is read as ``:``.

    A comment opens at an unquoted ``#`` that starts a word and runs to the end
    of the line; the quote state is restarted after it, because a quote inside
    a comment opens nothing.  A heredoc body runs from the newline that ends
    the line carrying its ``<<`` to its delimiter line, or to the end of the
    text when no delimiter line comes, at any depth.  Comments, and the bodies
    of heredocs fed to a ``_HEREDOC_SINKS`` command, or to ``cat`` piped into
    one, when ``_sinks_hold`` for the text left, are data spans.  When *text*
    is a whole command text (*top*), so is a body ``cat`` or ``tee`` only
    writes to a plainly named file (``_writes_file``) when ``_sinks_hold`` for
    the text without those writes: it runs only if a reader of that file was
    set up before the command, and that reader would run whatever any command
    writes there.  Inside a substitution or another body the output the write
    goes to is not known (``bash <(cat <<'EOF' >o ...)`` beside a link of
    ``o`` to standard output), so there it is command text.  Every other body
    is returned to be read as command text; in a sink's body beside only inert
    companions and plain runners (``_bullet_reading``), and in a
    ``_HOOKLESS_SINKS`` body beside a ``_HOOKLESS_SIBLING_RE`` command too, a
    markdown bullet is read as ``:``.  None
    when a heredoc delimiter cannot be read, since then no command after it
    can be placed.
    """
    kept: list[str] = []
    data: list[str] = []
    bodies: list[str] = []
    sink_bodies: list[str] = []
    file_bodies: list[str] = []
    hookless_bodies: list[str] = []
    pos = 0
    while pos < len(text):
        resume = _next_cut(
            text, pos, kept, (data, bodies, sink_bodies, file_bodies, hookless_bodies)
        )
        if resume == _UNREADABLE:
            return None
        if resume is None:
            break
        pos = resume
    executable = "".join(kept)
    folded = _fold_line_continuations(executable)
    bullets: list[str] = []
    for group, hookless in ((sink_bodies, False), (hookless_bodies, True)):
        if not group:
            continue
        if _sinks_hold(folded):
            data.extend(group)
        elif _bullet_reading(folded, hookless):
            # A sink reads its body as a message and runs none of it, and beside
            # only inert companions and plain runners the body runs at most
            # through a hook whose ``*`` is a glob of files set up outside the
            # command: a markdown ``*`` leading a line is then not a program
            # word.  ``gh`` and ``glab`` run no hook on a body at all, so the
            # same holds beside a ``_HOOKLESS_SIBLING_RE`` command.  A command
            # that could redefine the sink or run the body in this shell (a
            # function, alias, ``PATH=``, ``source`` or a substitution) fails
            # ``_bullet_reading``, so the body stays command text and the push
            # in it is read.
            bullets += [
                line for body in group for line in body.split("\n") if _BULLET_RE.match(line)
            ]
            bodies.extend(_BULLET_RE.sub(r"\1:", body) for body in group)
        else:
            bodies.extend(group)
    if file_bodies:
        rest = _fold_line_continuations(_without_file_writes(executable))
        # A file this command WROTE whose basename a sibling then runs is an
        # executable the body is written into (``> ~/.local/bin/sleep; chmod +x
        # ...; sleep``): ``sleep`` reads as an inert companion but runs the
        # installed script, so the body is command text, not data.  Compared by
        # basename, since the write names a path and the run resolves it on PATH.
        installed = _written_basenames(executable)
        rest_commands = _shell_commands(rest)
        runs_installed = installed and any(
            _top_level_basename(_dequote_token(_leading_program(command))) in installed
            for command in rest_commands
        )
        # A file this command writes to a plainly named path whose basename is a
        # git hook name (``.husky/pre-commit`` when ``core.hooksPath`` points
        # there) is run by git when a ``git`` sibling fires that hook, so its body
        # is command text and a bullet in it is a glob program word, read the same
        # as a literal push.  git runs only a file named by one of its own hooks,
        # so the written basename alone names the risk; other names stay data.
        runs_hook = bool(installed & _GIT_HOOK_NAMES) and any(
            _leading_program(command) == "git" for command in rest_commands
        )
        # A file this command writes to a plainly named path whose basename is a
        # name ``make`` reads for its recipes by default (``Makefile`` and its
        # spellings) is a recipe ``make`` runs through the shell when a ``make``
        # sibling that reads its default input fires it, so its body is command
        # text and a bullet in it is a glob program word, read the same as a
        # literal push.  A ``make`` reading another file (``-f``/``--makefile``,
        # excluded by ``_PLAIN_RUNNER_RE``) does not run this body, and no other
        # runner reads a ``Makefile``, so the written basename plus a default-input
        # ``make`` names the risk; other names and runners stay as they were.
        runs_recipe = bool(installed & _RUNNER_RECIPE_NAMES) and any(
            _leading_program(command) == "make" and _PLAIN_RUNNER_RE.match(command)
            for command in rest_commands
        )
        holds = top and _sinks_hold(rest) and not runs_installed
        if (
            not runs_hook
            and not runs_recipe
            and holds
            and not any(
                _leading_program(command) in ("git", "gh", "glab") for command in rest_commands
            )
        ):
            data.extend(file_bodies)
        elif (
            not runs_hook
            and not runs_recipe
            and (holds or (top and not runs_installed and _bullet_reading(rest)))
        ):
            # ``git`` (and ``gh`` or ``glab``, which can push) runs hooks, and an
            # executable hook the repository already has (``.husky/pre-commit``)
            # keeps its mode when rewritten, so the body may run here: only a
            # markdown bullet in it is text.  Beside a plain test or build
            # runner the same holds: the runner's code was on disk before the
            # command, so a reader of the file it runs and a file named ``git``
            # for the bullet ``*`` to expand to are both set up outside it.
            bullets += [
                line for body in file_bodies for line in body.split("\n") if _BULLET_RE.match(line)
            ]
            bodies.extend(_BULLET_RE.sub(r"\1:", body) for body in file_bodies)
        else:
            bodies.extend(file_bodies)
    return executable, data, bodies, bullets


def _leading_program(command: str) -> str:
    """The program word of *command*, past leading redirections and assignments.

    ``</dev/null git commit`` and ``GIT_DIR=. git commit`` both run ``git``, so
    the first bare word is read the way ``_sinks_hold`` reads it, not as the
    redirection or assignment that precedes it.
    """
    words = _split_shell_words(command)
    index = 0
    while index < len(words):
        after = _after_redirection(words, index)
        if after is None:
            break
        index = after
    while index < len(words) and _ASSIGNMENT_WORD_RE.match(words[index]):
        index += 1
    return _dequote_token(words[index]) if index < len(words) else ""


def _bullet_reading(executable: str, hookless: bool = False) -> bool:
    """True if a markdown bullet in a sink's body is not a program word, beside *executable*.

    Every command must be a sink, an inert companion or a ``_PLAIN_RUNNER_RE``
    runner, or with *hookless* (a ``_HOOKLESS_SINKS`` body) a
    ``_HOOKLESS_SIBLING_RE`` command, and nothing may ``_voids_sinks``.  The
    runner's code was on disk before the command, so a hook it plants and a
    file named ``git`` it makes for the bullet ``*`` to expand to are both set
    up outside the command, and would run whatever they were given.
    """
    return _sinks_hold(executable, runners=True, hookless=hookless)


def _prints_bullets(plain: str) -> bool:
    """True if a heredoc printer in *plain* feeds a sink whose bullets are not programs.

    Beside a ``_HOOKLESS_SIBLING_RE`` command that holds only when no ``git``
    sink runs in *plain*, so every printer feeds a ``_HOOKLESS_SINKS`` sink or
    a companion.
    """
    cut = _executable_text(plain)
    if cut is None:
        return False
    folded = _fold_line_continuations(cut[0])
    if _bullet_reading(folded):
        return True
    return not any(
        _feeds_sink(command) == "git" for command in _shell_commands(folded)
    ) and _bullet_reading(folded, hookless=True)


def _written_basenames(executable: str) -> "frozenset[str]":
    """The basenames of files a ``_writes_file`` command in *executable* writes.

    A command that writes a heredoc to a plainly named file installs that file;
    its basename is what a later sibling command would run if it names it. Used
    to keep the body command text when such an install is then executed.
    """
    names: set[str] = set()
    for piece in _shell_commands(executable):
        match = _FILE_WRITE_RE.match(piece)
        if match is None:
            continue
        path = next(group for group in match.groups() if group)
        names.add(re.split(r"[\\/]", path)[-1])
    return frozenset(names)


def _without_file_writes(executable: str) -> str:
    """*executable* with each ``_FILE_WRITE_RE`` command, each ``_changes_mode``
    command and each ``_makes_dirs`` command replaced by ``:``.

    What is left is what ``_sinks_hold`` judges, so the file write's own
    redirection does not void it while any other one still does.  A mode
    change or a directory make runs nothing, and a file a mode change makes
    executable runs only for a command that runs it, which is then left to be
    judged.
    """
    parts: list[str] = []
    cursor = 0
    for piece in _shell_commands(executable):
        at = executable.find(piece, cursor)
        if at < 0:
            return executable
        if _writes_file(piece) or _changes_mode(piece) or _makes_dirs(piece):
            parts.append(executable[cursor:at] + ":")
            cursor = at + len(piece)
    parts.append(executable[cursor:])
    return "".join(parts)


def _option_is_or_hides(word: str, pattern: "re.Pattern[str]") -> bool:
    """True if dequoted *word* matches *pattern*, or is an option whose name
    carries an unresolved expansion that could resolve to one.

    ``--out""put`` dequotes to ``--output`` and ``--e""dit`` to ``--edit``, an
    ANSI-C ``$'--edit'`` decodes to it, and a ``--$opt``, ``-$x`` or ``$opt``
    could expand to either, so a quoted fragment or an expansion that can name
    an option is treated as a match (fail closed).  So is an unquoted glob
    that can expand to a name starting with ``-`` (``--[e]dit``, ``*``), since
    a file of that name makes it the option.
    """
    decoded = _dequote_token(_decode_shell_quoted_literals(word))
    if pattern.match(decoded):
        return True
    globbed = _has_extglob(word) or any(
        step.active and step.char in "*?[" for step in _iter_shell_chars(word)
    )
    if globbed and (decoded.startswith("-") or decoded[:1] in "*?[@!+"):
        return True
    name = decoded.split("=", 1)[0]
    return (name.startswith("-") or name[:1] in ("$", "`")) and ("$" in name or "`" in name)


def _sinks_hold(executable: str, runners: bool = False, hookless: bool = False) -> bool:
    """True if nothing in *executable* can make a sink run its heredoc.

    Every command must run a sink subcommand with no editor option, or a
    ``_SINK_COMPANIONS`` program (with one of its listed subcommands when it
    lists any), each spelled as the plain word with no assignment before it;
    and nothing may ``_voids_sinks``.  Anything else could install a hook,
    filter, editor, alias or ``PATH`` entry that hands the body to a program.
    The word after a sink's value-taking option (``_SINK_VALUE_OPTIONS``) is
    its value, so it names no editor unless an unquoted expansion splits it.
    With *runners*, a ``_PLAIN_RUNNER_RE`` command counts too, and with
    *hookless* as well, a ``_HOOKLESS_SIBLING_RE`` one.
    """
    if _voids_sinks(executable):
        return False
    for command in _shell_commands(executable):
        if runners and _PLAIN_RUNNER_RE.match(command):
            continue
        if runners and hookless and _HOOKLESS_SIBLING_RE.match(command):
            continue
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
            if not _sink_options_hold(program, words[index + 2 :]):
                return False
            continue
        allowed = _SINK_COMPANIONS.get(program)
        if allowed is None or (allowed and subcommand not in allowed):
            return False
        if program == "git" and any(
            _option_is_or_hides(word, _GIT_OUTPUT_FILE_OPTION_RE)
            or _option_is_or_hides(word, _GIT_TRANSPORT_PROGRAM_OPTION_RE)
            for word in words[index + 1 :]
        ):
            # ``git log --output=.git/hooks/...`` writes a file the sink runs, and
            # ``git fetch --upload-pack='<cmd>'`` runs a program that can.
            return False
    return True


def _sink_options_hold(program: str, words: "list[str]") -> bool:
    """True if no word of a sink's argument *words* is or can be an editor option.

    For ``git`` a template option counts too.  The word after a value-taking
    option (``_SINK_VALUE_OPTIONS``) is that option's value and is not read,
    unless an unquoted expansion can split it into more words.
    """
    values = _SINK_VALUE_OPTIONS.get(program, frozenset())
    value_next = False
    for word in words:
        if value_next and not (_splits_into_words(word) or _can_vanish(word)):
            value_next = False
            continue
        value_next = word in values
        if _option_is_or_hides(word, _SINK_EDITOR_OPTION_RE) or (
            program == "git" and _option_is_or_hides(word, _GIT_TEMPLATE_OPTION_RE)
        ):
            return False
    return True


def _next_cut(text: str, pos: int, kept: list, cut: "tuple[list, ...]") -> "int | None":
    """Walk *text* from *pos* to the next comment or heredoc body and cut it.

    Appends the text before the cut to *kept* and the cut span to the data,
    body, sink-body, file-body or hookless-sink-body list of *cut*, and returns
    where the walk resumes in quote state 0; None at the end, ``_UNREADABLE``
    at a heredoc whose delimiter cannot be read.
    """
    data = cut[0]
    pending: "list[_Heredoc]" = []
    command_start = pos
    sink_at: "dict[int, tuple[str, bool]]" = {}
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
            spans: "list[tuple[int, int]]" = []
            body_end = _heredoc_bodies_end(text, end + 1, pending, spans)
            if body_end == _UNREADABLE:
                return _UNREADABLE
            kept.append("\n")
            _store_bodies(text, spans, pending, cut)
            return body_end
        heredoc = _heredoc_at(text, offset)
        if heredoc is _UNREADABLE_HEREDOC:
            return _UNREADABLE
        if heredoc is not None:
            # The program and subcommand are read once per command, at its first ``<<``.
            if command_start not in sink_at:
                line_end = text.find("\n", offset)
                line = text[command_start : None if line_end == -1 else line_end]
                rest = _shell_commands(line)
                writes_file = bool(rest) and _writes_file(rest[0])
                sink = _feeds_sink(text[command_start:offset])
                piped = _CAT_PIPE_RE.match(line)
                if piped is not None:
                    # ``cat <<'EOF' | gh pr create --body-file -`` hands the body to the sink.
                    sink = _feeds_sink(line[piped.end() :])
                sink_at[command_start] = (sink, writes_file)
            sink, writes_file = sink_at[command_start]
            pending.append(
                heredoc._replace(
                    data=bool(sink), file=writes_file, hookless=sink in _HOOKLESS_SINKS
                )
            )
        if char in ";&|()\n":
            command_start = offset + 1
        if char == "\n" and pending:
            spans = []
            body_end = _heredoc_bodies_end(text, offset + 1, pending, spans)
            if body_end == _UNREADABLE:
                return _UNREADABLE
            kept.append(text[pos : offset + 1])
            _store_bodies(text, spans, pending, cut)
            return body_end
    kept.append(text[pos:])
    return None


def _feeds_sink(prefix: str) -> str:
    """The ``_HEREDOC_SINKS`` program command text *prefix*, up to a ``<<``, runs
    a sink subcommand of; empty when it runs none.

    The program must be the bare word ``gh``, ``git`` or ``glab`` with no
    assignment before it, and its next word one of the sink's subcommands;
    redirections may come first.
    """
    words = _split_shell_words(prefix)
    index = 0
    while index < len(words):
        after = _after_redirection(words, index)
        if after is None:
            break
        index = after
    if index + 1 >= len(words):
        return ""
    program = words[index]
    return program if words[index + 1] in _HEREDOC_SINKS.get(program, frozenset()) else ""


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
