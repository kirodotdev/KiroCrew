"""The ``git grep`` pager read of the self-kill floor.

``git grep -O<cmd>`` (and the other places git takes its pager from) runs ``<cmd>``
on the matching files, so a pager that kills the product is a self-kill that no argv
position shows. :func:`_git_grep_pager_commands` returns every pager command a
``git grep`` in a text can run; ``argv_floor_self._is_self_kill`` asks the same kill
check about each one. This module reads the shell reader and the argv rules below it
and imports no other part of the argv floor.
"""

from __future__ import annotations

import shlex
from typing import NamedTuple

from . import argv_rules as _argv_rules
from . import shell_commands as _shell_commands
from . import shell_normalizer as _shell_normalizer
from .paths import MAX_SCANNABLE_COMMAND_CHARS

#: The array expansion one shell text may build for its ``git grep`` arguments, in
#: characters (each word counts its separator too). A command is refused unscanned
#: above :data:`MAX_SCANNABLE_COMMAND_CHARS`, so no honest command needs more than
#: that much; one that would build more is refused rather than expanded.
_EXPANSION_BUDGET = MAX_SCANNABLE_COMMAND_CHARS
#: The array expansion all the ``git grep`` commands of one text may build together,
#: so many commands that each stay under :data:`_EXPANSION_BUDGET` still cost a
#: bounded amount.
_TOTAL_EXPANSION_BUDGET = 16 * MAX_SCANNABLE_COMMAND_CHARS
#: The most significant digits a subscript or a ``GIT_CONFIG_COUNT`` is read with.
#: bash and git hold these in 64 bits, so a longer number names no real index or
#: count, and Python refuses to convert a long enough one. Past it the value is
#: unreadable, and a ``git grep`` in the same text is refused.
_MAX_NUMBER_DIGITS = 19
#: The index :func:`_bash_index` and :func:`_git_count` answer for a number past
#: :data:`_MAX_NUMBER_DIGITS`. No literal subscript reads as negative.
_HUGE = -1
#: The candidate values one variable may carry from conditional assignments, and
#: the combinations one ``git grep`` may be read under. Past either, a ``git grep``
#: that reads such a variable is refused only when one of its candidates or one of
#: the grep's words could reach a pager (:func:`_may_reach_pager`); otherwise the
#: candidates are inert and only the current value is read.
_MAX_ALTERNATIVES = 8
_MAX_WORLDS = 16
#: The separators after which an assignment may not run, or runs in a subshell
#: and does not reach the next command: ``a && b=x``, ``a || b=x``, ``b=x | c``,
#: ``b=x &``.
_CONDITIONAL_SEPARATORS = frozenset({"&&", "||", "|", "&"})
#: What :func:`_git_grep_pager_commands` returns for a text whose pager read would
#: pass :data:`_EXPANSION_BUDGET`, or whose pager is longer than the text it came
#: from: the kill check refuses it instead of reading it.
_PAGER_OVER_BUDGET = "\x00pager-over-budget"

#: git's global options (before the subcommand) whose value is the NEXT word, so that
#: word is skipped rather than read as the subcommand.
_GIT_GLOBAL_VALUE_OPTIONS = frozenset(
    {
        *("-C", "-c", "--git-dir", "--work-tree", "--namespace"),
        *("--super-prefix", "--config-env", "--attr-source"),
    }
)
#: git's global options that, written bare, print or show something and exit
#: without running the subcommand after them.
_GIT_GLOBAL_EXIT_OPTIONS = frozenset({"--exec-path", "--version", "--help", "-v", "-h"})
#: The values git reads as boolean false (case-insensitive; an empty value too).
_GIT_FALSE = frozenset({"false", "no", "off", "0", ""})
#: The values git reads as boolean true (case-insensitive).
_GIT_TRUE = frozenset({"true", "yes", "on", "1"})
#: ``git grep`` short options that take a VALUE (``builtin/grep.c``): a pattern
#: (``-e``), a file (``-f``), a count (``-m``) or a context size (``-A``/``-B``/
#: ``-C``). In a cluster the rest of the word is that value, else it is the next
#: word, so either way it is never a pager. Case-sensitive: ``-E`` and ``-F`` are
#: flags that take none.
_GIT_GREP_VALUE_SHORT = frozenset("efmABC")
#: The long options of the same kind. git also accepts a unique prefix of a long
#: name, so a word is matched as a prefix of one of these.
_GIT_GREP_VALUE_LONG = (
    "--regexp",
    "--file",
    "--max-count",
    "--max-depth",
    "--threads",
    "--context",
    "--after-context",
    "--before-context",
)


#: The other ``git grep`` long options that begin ``--o``. git takes a long option's
#: unique prefix only, so a prefix shared with one of these (``--o``) is ambiguous and
#: git refuses the command instead of running a pager.
_GIT_GREP_OPEN_RIVALS = ("--or", "--only-matching")


def _grep_arg_pager(word: str) -> "tuple[str, str]":
    """How ``git grep`` parses *word*, as submitted: ``(role, pager)``.

    Roles: ``pager`` with the command git would run as its pager (``""`` for a bare
    ``-O``, which asks for the default pager); ``no_pager`` for
    ``--no-open-files-in-pager`` (or a prefix git takes for it, from ``--no-op``),
    which clears an earlier ``-O``; ``skip_next`` when *word* is an option
    whose value is the FOLLOWING word (so that word is not read as a pager); ``plain``
    otherwise. Only ``-O`` / ``--open-files-in-pager`` carry a pager, and always GLUED
    to the option, so a pager is never a separate word. A short cluster is walked one
    letter at a time, the way git's parse-options walks it: ``O`` is the pager only
    before the first letter that takes a value, since everything after that letter
    is its value.
    """
    if word.startswith("--"):
        name, _sep, value = word.partition("=")
        if not _sep and len(name) >= len("--no-op") and "--no-open-files-in-pager".startswith(name):
            return ("no_pager", "")
        if (
            len(name) > 2
            and "--open-files-in-pager".startswith(name)
            and not any(rival.startswith(name) for rival in _GIT_GREP_OPEN_RIVALS)
        ):
            return ("pager", value)
        if not _sep and len(name) > 2 and any(o.startswith(name) for o in _GIT_GREP_VALUE_LONG):
            return ("skip_next", "")
        return ("plain", "")
    if word.startswith("-") and len(word) > 1:
        j = 1
        while j < len(word) and word[j].isascii() and word[j].isalnum():
            if word[j] == "O":
                return ("pager", word[j + 1 :])
            if word[j] in _GIT_GREP_VALUE_SHORT:
                return ("plain", "") if j + 1 < len(word) else ("skip_next", "")
            j += 1
    return ("plain", "")


class _Expansion(NamedTuple):
    """The words one array expansion gives a ``git grep``."""

    words: "tuple[str, ...]"


def _scan_grep_args(
    args: "list[str | _Expansion]",
    memo: "dict[tuple[int, bool], tuple[tuple[str, ...], bool]] | None" = None,
) -> "tuple[str, list[str]]":
    """``(last real -O pager, inserted pagers)`` a ``git grep`` runs from its own
    arguments *args*.

    Each argument is a real word (a ``str``) or an :class:`_Expansion`, the words an
    array expansion put there. A value-taking option's value is skipped, so
    ``-e '-O<cmd>'`` reads ``-O<cmd>`` as a pattern, not a pager. The LAST ``-O``
    wins, as in git, so ``-O<kill> -Ocat`` runs only ``cat``, and a real
    ``--no-open-files-in-pager`` clears it. An inserted word is read where it stands
    but trusted less, because the array the floor read may not be the one bash
    expands: its value option never swallows the real word after it, and its ``-O``
    neither replaces nor is replaced by a real one. ``--`` is not treated as ending
    option parsing here: the issue names ``-O`` placed after the pathspec, and a word
    that both begins ``-O`` and spells a product kill is that bypass, not a real
    pathspec.

    An expansion object is scanned once per entry state (in *memo*, which a caller
    shares across a text's commands), and the caller hands the same object for
    every reference with the same text, so repeating one array reference costs the
    reference's length, not its length times the array's size.
    """
    last = ""
    inserted: "dict[str, None]" = {}
    if memo is None:
        memo = {}
    # What the previous word left pending: ``any`` (a real value option swallows the
    # next word), ``inserted`` (an inserted one swallows only an inserted word), or
    # nothing.
    pending = ""
    for arg in args:
        if isinstance(arg, str):
            if pending == "any":
                pending = ""
                continue
            role, pager = _grep_arg_pager(arg)
            pending = "any" if role == "skip_next" else ""
            if role == "pager":
                last = pager
            elif role == "no_pager":
                last = ""
            continue
        # Keyed by identity: hashing the words would read the whole array again.
        key = (id(arg), bool(pending))
        if key not in memo:
            memo[key] = _scan_expansion(arg.words, bool(pending))
        pagers, leaves_pending = memo[key]
        inserted.update(dict.fromkeys(pagers))
        pending = "inserted" if leaves_pending else ""
    return last, [command for command in inserted if command.strip()]


def _scan_expansion(words: "tuple[str, ...]", swallowed: bool) -> "tuple[tuple[str, ...], bool]":
    """``(pagers, leaves a value option pending)`` for one expansion's *words*.

    *swallowed*: the word before the expansion was a value option, so the first
    inserted word is its value.
    """
    pagers: "dict[str, None]" = {}
    pending = swallowed
    for word in words:
        if pending:
            pending = False
            continue
        role, pager = _grep_arg_pager(word)
        if role == "skip_next":
            pending = True
        elif role == "pager":
            pagers[pager] = None
    return tuple(pagers), pending


#: The characters a backslash escapes inside double quotes; before any other it is
#: a literal backslash.
_DOUBLE_QUOTE_ESCAPES = frozenset('$`"\\\n')


def _shell_word(word: "_shell_commands.ShellWord") -> "tuple[str, list[int]]":
    """The text the shell passes for *word*, with each character's quoting.

    Quote syntax (an opening or closing quote) is removed, an escape pair gives the
    escaped character (a backslash-newline gives nothing, and inside double quotes a
    backslash before any other character stays), all as the shared quote machine
    marked the steps. The quoting is 0 unquoted, 1 literal (single-quoted or
    escaped), 2 double-quoted.
    """
    chars: "list[str]" = []
    quoting: "list[int]" = []
    for step in word.steps:
        if len(step.text) == 2:
            if step.char == "\n":
                continue
            kept = (
                step.text
                if step.state == 2 and step.char not in _DOUBLE_QUOTE_ESCAPES
                else step.char
            )
            chars.append(kept)
            quoting.extend([1] * len(kept))
        elif step.char in "'\"" and (step.active or step.state == 0):
            continue
        else:
            chars.append(step.char)
            quoting.append(step.state)
    return "".join(chars), quoting


def _git_config_value(setting: str) -> "tuple[str, bool, str]":
    """``(key folded, has a value, value)`` of one ``key=value`` config setting."""
    key, has_value, value = setting.partition("=")
    return key.lower(), bool(has_value), value


def _git_count(text: str) -> "int | None":
    """``GIT_CONFIG_COUNT`` read as git reads an unsigned number, else None.

    Leading blanks and a ``+`` are allowed, and a ``k`` / ``m`` / ``g`` suffix scales
    it (``git_parse_unsigned``); leading zeros do not count toward
    :data:`_MAX_NUMBER_DIGITS`, and a longer number gives :data:`_HUGE`.
    """
    body = text.lstrip(" \t\n\v\f\r")
    if body.startswith("+"):
        body = body[1:]
    end = 0
    while end < len(body) and body[end].isascii() and body[end].isdigit():
        end += 1
    digits, unit = body[:end], body[end:].lower()
    if not digits or unit not in ("", "k", "m", "g"):
        return None
    significant = digits.lstrip("0") or "0"
    if len(significant) > _MAX_NUMBER_DIGITS:
        return _HUGE
    return int(significant) * {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30}[unit]


def _env_config_settings(env: "dict[str, str]") -> "list[str] | None":
    """The ``key=value`` settings git reads from *env*'s config variables.

    ``GIT_CONFIG_PARAMETERS`` holds single-quoted ``'key=value'`` or ``'key'='value'``
    entries; ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_<n>`` / ``GIT_CONFIG_VALUE_<n>``
    hold numbered pairs, of which git reads the first COUNT. git applies the
    numbered pairs first and ``GIT_CONFIG_PARAMETERS`` after them, so the later
    one wins, and the list keeps that order. Only the variables present are read,
    so a large COUNT costs nothing; a COUNT git cannot parse gives no pairs, as git
    refuses it too, and one past :data:`_MAX_NUMBER_DIGITS` gives None, which the
    caller refuses.
    """
    settings: "list[str]" = []
    count = _git_count(env["GIT_CONFIG_COUNT"]) if "GIT_CONFIG_COUNT" in env else None
    if count == _HUGE:
        return None
    if count is not None:
        pairs: "list[tuple[int, str]]" = []
        for name, key in env.items():
            index = name[len("GIT_CONFIG_KEY_") :]
            # git builds these names from the numbers 0..COUNT-1, so only a plain
            # decimal without leading zeros is one of them.
            if not (name.startswith("GIT_CONFIG_KEY_") and index.isascii() and index.isdigit()):
                continue
            if len(index) > _MAX_NUMBER_DIGITS or (len(index) > 1 and index[0] == "0"):
                continue
            if int(index) < count and f"GIT_CONFIG_VALUE_{index}" in env:
                pairs.append((int(index), f"{key}={env[f'GIT_CONFIG_VALUE_{index}']}"))
        settings.extend(setting for _index, setting in sorted(pairs))
    parameters = env.get("GIT_CONFIG_PARAMETERS")
    if parameters:
        try:
            settings.extend(shlex.split(parameters))
        except ValueError:
            pass
    return settings


def _is_assignment(word: str) -> bool:
    """True if *word* is a shell ``NAME=value`` assignment word."""
    name, has_value, _value = word.partition("=")
    return (
        bool(has_value)
        and bool(name)
        and name.isascii()
        and not name[0].isdigit()
        and name.replace("_", "a").isalnum()
    )


def _shell_reads_stdin(args: "list[str]") -> bool:
    """True if a shell given *args* reads its script from standard input.

    It does unless a ``-c`` (alone or in a short-option cluster) names the script or
    a word that is not an option names a script file. ``-o`` / ``-O`` / ``+o`` /
    ``+O`` and ``--rcfile`` / ``--init-file`` take the next word as their value;
    after ``-s`` every word is a positional argument, and after ``--`` the next
    word, if any, is the script file.
    """
    k = 0
    while k < len(args):
        arg = args[k]
        if arg == "-s":
            return True
        if arg == "--":
            return k + 1 >= len(args)
        if arg in ("-o", "-O", "+o", "+O", "--rcfile", "--init-file"):
            k += 2
            continue
        if arg.startswith("--"):
            k += 1
            continue
        if arg[:1] in "-+" and len(arg) > 1:
            if arg[0] == "-" and "c" in arg[1:]:
                return False
            if "s" in arg[1:]:
                return True
            k += 1
            continue
        return False  # a script file: the here-document is its input, not its code
    return True


def _git_grep_pager_commands(source: str) -> "list[str]":
    """The pager commands every ``git grep`` in *source* can run, for the kill check.

    ``git grep`` runs its pager on the matching files, so a pager that kills the
    product is a self-kill no argv position shows. Parsed the way git parses it, one
    invocation at a time and only from what that simple command itself shows:

    * the last ``-O`` / ``--open-files-in-pager=`` among grep's own arguments, with
      each literal array reference there expanded from the arrays the text's earlier
      commands set (:class:`_TextState`);
    * unless paging is off (``-P`` / ``--no-pager``, or a false ``pager.grep``
      without ``-p``), the ``GIT_PAGER=`` / ``PAGER=`` assignments before ``git``
      and the last ``core.pager`` and ``pager.grep`` git reads, from ``-c``, from
      ``--config-env`` naming a variable assigned before ``git``, or from
      ``GIT_CONFIG_PARAMETERS`` / ``GIT_CONFIG_COUNT`` assignments before ``git``;
    * an ``alias.<name>`` set by ``-c`` on the same command that resolves, alias by
      alias, to ``grep`` makes ``git <name>`` a grep, with the aliases' own words
      read first.

    A ``git grep`` run by ``find -exec`` is read the same way, and so is every
    command of a here-document fed to a shell that reads its script from standard
    input (``bash <<EOF``, ``bash -s <<EOF``, ``bash -o pipefail <<EOF``), at any
    depth: each body is read once, as its own text, with arrays of its own.

    The commands and their words come from the shared reader
    (:func:`shell_commands.read_commands`, over :func:`_iter_shell_chars`) after
    ``$'...'`` is decoded as every floor reader decodes it, so a quoted ``;`` or
    ``|`` inside an argument does not cut one ``git grep`` in two. Each distinct
    command is returned once.
    """
    commands: "dict[str, None]" = {}
    texts = [source]
    while texts:
        text = _shell_normalizer._decode_shell_quoted_literals(texts.pop())
        if "git" not in _unquoted_probe(text):
            continue  # no ``git`` word can be read from it, at any depth
        state = _TextState()
        opener: "tuple[str, bool, bool] | None" = None
        previous = ""
        for command in _shell_commands.read_commands(text, heredocs=True):
            words = [_shell_word(word) for word in command.argv]
            sub = [value for value, _quoting in words]
            before, previous = previous, command.separator
            if opener is not None:
                # The elements of ``name=( ... )``: the reader ends the opener at
                # ``(`` and this command at ``)``.
                state.assign_list(opener[0], words, opener[1], conditional=opener[2])
                opener = None
                continue
            parts = [_assignment_parts(word) for word in sub]
            if sub and all(parts):
                conditional = (
                    before in _CONDITIONAL_SEPARATORS
                    or command.separator in _CONDITIONAL_SEPARATORS - {"&&", "||"}
                )
                for part in parts[:-1]:
                    state.assign_word(*part, conditional=conditional)  # type: ignore[misc]
                name, index, appends, value = parts[-1]  # type: ignore[misc]
                if command.separator == "(" and index is None and not value:
                    opener = (name, appends, conditional)
                else:
                    state.assign_word(name, index, appends, value, conditional=conditional)
                continue
            # The program after any wrapper (``sudo``, ``env``, ``timeout 5`` ...) and
            # ``VAR=x`` words, the same step-over the argv rules use.
            found = _argv_rules._program_args(sub)
            if found is None:
                continue
            start = len(sub) - len(found[1])
            program = found[0].lower()
            if (
                command.heredocs
                and program in _shell_normalizer._NESTED_SHELL_PROGRAMS
                and program != "busybox"
                and _shell_reads_stdin(found[1])
            ):
                texts.extend(doc.body for doc in command.heredocs)
            invocations = [words]
            if program == "find":
                invocations = [words[start + a : start + b] for a, b in _find_exec_spans(found[1])]
            for invocation in invocations:
                for pager in _git_grep_invocation_pagers(invocation, state):
                    # A pager longer than the text it came from was built by
                    # repeating an expansion; checking it would cost more than the
                    # text, at every level, so it is refused instead.
                    if pager == _PAGER_OVER_BUDGET or len(pager) > len(text):
                        return [_PAGER_OVER_BUDGET]
                    commands[pager] = None
    return list(commands)


def _unquoted_probe(text: str) -> str:
    """*text* folded with quoting removed, for the "no ``git`` here" shortcut.

    bash runs ``git`` for ``g''it``, ``\"g\"it``, ``gi\\t``, ``gi\\<newline>t`` and
    ``g$()it``, so the quote characters, backslashes, line continuations and empty
    substitutions are dropped before looking for the name. Dropping more than the
    shell would only lets more texts through to the full read.
    """
    probe = text.replace("\\\n", "")
    for drop in ("$()", "``", "'", '"', "\\"):
        probe = probe.replace(drop, "")
    return probe.lower()


def _find_exec_spans(args: "list[str]") -> "list[tuple[int, int]]":
    """``(start, end)`` in *args* of each ``-exec`` / ``-execdir`` / ``-ok`` /
    ``-okdir`` command of a ``find``, as :func:`argv_rules._find_exec_argvs` reads it."""
    spans: "list[tuple[int, int]]" = []
    for index, token in enumerate(args):
        if token in _argv_rules._FIND_EXEC:
            end = next(
                (j for j in range(index + 1, len(args)) if args[j] in (";", "+", "\\;")),
                len(args),
            )
            if end > index + 1:
                spans.append((index + 1, end))
    return spans


def _git_grep_invocation_pagers(
    words: "list[tuple[str, list[int]]]", state: "_TextState"
) -> "list[str]":
    """The pager commands one simple command's ``git grep`` can run, else ``[]``."""
    sub = [value for value, _quoting in words]
    found = _argv_rules._program_args(sub)
    # The program name is folded: a case-insensitive filesystem runs ``GIT``.
    if found is None or found[0].lower() != "git":
        return []
    i = len(sub) - len(found[1])
    # Every assignment before ``git`` is in its environment, whether it leads the
    # command or follows ``env`` or ``sudo``.
    env = dict(word.partition("=")[::2] for word in sub[: i - 1] if _is_assignment(word))
    env_settings = _env_config_settings(env)
    if env_settings is None:
        return [_PAGER_OVER_BUDGET]  # a GIT_CONFIG_COUNT too long to read
    settings = env_settings
    paging = ""  # the last of -P / --no-pager ("off") and -p / --paginate ("on")
    grep_args: "list[str] | None" = None
    while i < len(sub):
        token = sub[i]
        if token in _GIT_GLOBAL_EXIT_OPTIONS:
            return []  # git runs ``version`` / ``help`` or prints a path, never grep
        if token in ("-P", "--no-pager", "-p", "--paginate"):
            # ``-P`` makes git set ``GIT_PAGER=cat``; ``-p`` pages whatever
            # ``pager.grep`` says.
            paging = "off" if token in ("-P", "--no-pager") else "on"
        elif token.startswith("--config-env="):
            settings.extend(_config_env_setting(token[len("--config-env=") :], env))
        elif token in _GIT_GLOBAL_VALUE_OPTIONS:
            if i + 1 < len(sub) and token == "-c":
                settings.append(sub[i + 1])
            elif i + 1 < len(sub) and token == "--config-env":
                settings.extend(_config_env_setting(sub[i + 1], env))
            i += 1
        elif not token.startswith("-"):
            resolved = _grep_subcommand(token, settings)
            if resolved is not None:
                grep_args, alias_options = resolved
                for option in alias_options:  # an alias's own -p / -P come last
                    paging = "off" if option in ("-P", "--no-pager") else "on"
            i += 1
            break  # the subcommand: a grep, an alias of one, or another
        i += 1
    if grep_args is None:
        return []
    pager_values: "dict[str, tuple[bool, str]]" = {}
    for setting in settings:
        key, has_value, value = _git_config_value(setting)
        if key in ("core.pager", "pager.grep"):
            pager_values[key] = (has_value, value)  # the last one wins, as in git
    grep_paged = paging != "off"
    grep_pager: "str | None" = None
    # ``pager.grep`` is read only when no -p / -P said otherwise.
    if not paging and "pager.grep" in pager_values:
        has_value, value = pager_values["pager.grep"]
        folded = value.strip().lower()
        if has_value and folded in _GIT_FALSE:
            grep_paged = False  # a boolean false: grep's paging is off
        elif has_value and folded not in _GIT_TRUE:
            grep_pager = value  # a command: grep's own pager
    # The one pager git runs (``git_pager``): GIT_PAGER, else a ``pager.grep``
    # command, else core.pager, else PAGER.
    if "GIT_PAGER" in env:
        default_pager = env["GIT_PAGER"]
    elif grep_pager is not None:
        default_pager = grep_pager
    elif "core.pager" in pager_values:
        default_pager = pager_values["core.pager"][1]
    else:
        default_pager = env.get("PAGER", "")
    pagers: "dict[str, None]" = {}
    for world in state.worlds(words[i:]):
        with world:
            state.budget = _EXPANSION_BUDGET  # one command's share
            args: "list[str | _Expansion]" = list(grep_args)
            for value, quoting in words[i:]:
                expansion = state.expansion(value, quoting) if "$" in value else None
                args.append(value if expansion is None else expansion)
            if state.over_budget:
                return [_PAGER_OVER_BUDGET]
            last, inserted = _scan_grep_args(args, state.scans)
        if last.strip():
            # ``-O<cmd>`` names the pager itself, so no configured pager runs.
            pagers[last] = None
        elif grep_paged:
            pagers[default_pager] = None
        pagers.update(dict.fromkeys(inserted))
    if state.over_budget:
        return [_PAGER_OVER_BUDGET]
    return [pager for pager in pagers if pager.strip()]


def _assignment_parts(word: str) -> "tuple[str, int | None, bool, str] | None":
    """``(name, index or None, appends, value)`` of a shell assignment word, else None.

    ``a=x``, ``a+=x``, ``a[3]=x`` and ``a[3]+=x``; an array opener (``a=(`` /
    ``a+=(``) arrives as ``a=`` / ``a+=`` with an empty value.
    """
    head, has_value, value = word.partition("=")
    if not has_value:
        return None
    appends = head.endswith("+")
    if appends:
        head = head[:-1]
    index: "int | None" = None
    if head.endswith("]") and "[" in head:
        head, _sep, subscript = head[:-1].partition("[")
        index = _bash_index(subscript)
        if index is None:
            return None
    if not head or not head.isascii() or head[0].isdigit():
        return None
    if not head.replace("_", "a").isalnum():
        return None
    return head, index, appends, value


def _bash_index(text: str) -> "int | None":
    """The array index bash reads from the literal subscript *text*, else None.

    bash evaluates a subscript as arithmetic, so a leading ``0x`` is hexadecimal and
    a leading ``0`` octal (``010`` is 8). Only those literal forms are read; any
    other expression, or an invalid digit for the base, gives None, and a number
    past :data:`_MAX_NUMBER_DIGITS` gives :data:`_HUGE` without being converted.
    """
    if not text.isascii():
        return None
    if text[:2] in ("0x", "0X"):
        digits, base = text[2:], 16
    elif len(text) > 1 and text[0] == "0":
        digits, base = text[1:], 8
    else:
        digits, base = text, 10
    allowed = "0123456789abcdefABCDEF"[: base + max(0, base - 10)]
    if not digits or any(ch not in allowed for ch in digits):
        return None
    significant = digits.lstrip("0") or "0"
    if len(significant) > _MAX_NUMBER_DIGITS:
        return _HUGE
    return int(significant, base)


def _compound_element(value: str, quoting: "list[int]") -> "tuple[str | None, str] | None":
    """``(subscript, element)`` of an unquoted ``[subscript]=element`` word in an array
    literal, else None (a plain element)."""
    if not value.startswith("[") or quoting[0] != 0:
        return None
    close = value.find("]=")
    if close < 0 or quoting[close] != 0 or quoting[close + 1] != 0:
        return None
    return value[1:close], value[close + 2 :]


class _TextState:
    """What every ``git grep`` of one shell text shares: the literal arrays its
    commands have set so far (each a map of index to element), each array's
    expansions per version, and the scan of each expansion per entry state.

    The floor reads arrays in its own pass over the text's commands, the way bash
    runs them in order: the last assignment wins, ``a+=(...)`` appends, ``a[N]=x``
    and ``a=([N]=x ...)`` set one index (``N`` read as bash reads a literal
    subscript), and ``a=x`` on an array sets element 0. The expansion it builds is
    held to :data:`_EXPANSION_BUDGET`; past it :attr:`over_budget` is set and the
    text's ``git grep`` is refused."""

    def __init__(self) -> None:
        self.arrays: "dict[str, dict[int, str]]" = {}
        self.versions: "dict[str, int]" = {}
        self.elements: "dict[tuple[str, int, int | str, int], list[str]]" = {}
        self.expansions: "dict[tuple[object, ...], _Expansion | None]" = {}
        self.scans: "dict[tuple[int, bool], tuple[tuple[str, ...], bool]]" = {}
        self.sizes: "dict[tuple[str, int, int | str, int], int]" = {}
        self.budget = _EXPANSION_BUDGET
        self.total_budget = _TOTAL_EXPANSION_BUDGET
        self.over_budget = False
        #: Values a conditional assignment may have left in place: per name, the
        #: earlier ``(elements, version)`` pairs bash may still hold.
        self.alternatives: "dict[str, list[tuple[dict[int, str], int]]]" = {}
        #: Names whose candidates passed :data:`_MAX_ALTERNATIVES` and were
        #: dropped, each with whether a dropped one could reach a pager.
        self.collapsed: "dict[str, bool]" = {}

    def _changed(self, name: str) -> None:
        self.versions[name] = self.versions.get(name, 0) + 1

    def _before_assigning(self, name: str, conditional: bool) -> None:
        """Keep the value *name* has now as a candidate when the assignment may not
        run (*conditional*), so a killing earlier value is still read; an
        unconditional whole assignment drops the candidates."""
        if not conditional:
            return
        if name in self.arrays:
            kept = self.alternatives.setdefault(name, [])
            value = dict(self.arrays[name])
            if any(array == value for array, _version in kept):
                return  # ``rc=1`` after each failed check is one candidate
            kept.append((value, self.versions.get(name, 0)))
            if len(kept) > _MAX_ALTERNATIVES:
                # Only a ``git grep`` that reads *name* pays for this (:func:`_worlds`).
                reaches = any(_array_may_reach_pager(array) for array, _version in kept)
                self.collapsed[name] = self.collapsed.get(name, False) or reaches
                self.alternatives.pop(name)

    def assign_list(
        self,
        name: str,
        words: "list[tuple[str, list[int]]]",
        appends: bool,
        conditional: bool = False,
    ) -> None:
        """``name=(words)``, or ``name+=(words)`` when *appends*. A ``[N]=x`` word
        sets index N and the next plain word follows it; a subscript the floor cannot
        read still puts its element in the array, at the next index."""
        self._before_assigning(name, conditional)
        if not appends and not conditional:
            self.alternatives.pop(name, None)
            self.collapsed.pop(name, None)
        current = self.arrays.setdefault(name, {}) if appends else {}
        index = max(current) + 1 if current else 0
        for value, quoting in words:
            compound = _compound_element(value, quoting)
            if compound is not None:
                subscript, value = compound
                read = _bash_index(subscript or "")
                if read == _HUGE:
                    self.over_budget = True
                    return
                if read is not None:
                    index = read
            current[index] = value
            index += 1
        self.arrays[name] = current
        self._changed(name)

    def assign_word(
        self,
        name: str,
        index: "int | None",
        appends: bool,
        value: str,
        conditional: bool = False,
    ) -> None:
        """``name[index]=value`` (``+=`` appends to that element). Without an index it
        sets element 0, which is what a plain scalar is to bash, so ``"$p"`` reads
        it as it reads an array's first element. A subscript past
        :data:`_MAX_NUMBER_DIGITS` makes the text unreadable."""
        if index == _HUGE:
            self.over_budget = True
            return
        self._before_assigning(name, conditional)
        if index is None:
            index = 0
        current = self.arrays.setdefault(name, {})
        current[index] = current.get(index, "") + value if appends else value
        self._changed(name)

    def selected(self, name: str, selector: "int | str", state: int) -> "list[str]":
        """The words reference ``${name[selector]}`` gives, quoted per *state*."""
        key = (name, self.versions.get(name, 0), selector, state)
        if key not in self.elements:
            array = self.arrays[name]
            if selector in ("@", "*"):
                values = [array[index] for index in sorted(array)]
            else:
                values = [array.get(int(selector), "")]
            if state == 0:
                words = [part for value in values for part in value.split()]
            elif selector == "*":
                words = [" ".join(values)]
            else:
                words = values
            self.elements[key] = words
        return self.elements[key]

    def worlds(self, words: "list[tuple[str, list[int]]]") -> "list[_World]":
        """See :func:`_worlds`."""
        return _worlds(self, words)

    def _size(self, name: str, selector: "int | str", state: int) -> int:
        """The characters ``${name[selector]}`` adds, a separator per word included."""
        key = (name, self.versions.get(name, 0), selector, state)
        if key not in self.sizes:
            words = self.selected(name, selector, state)
            self.sizes[key] = sum(len(word) + 1 for word in words)
        return self.sizes[key]

    def expansion(self, value: str, quoting: "list[int]") -> "_Expansion | None":
        """The words bash makes of *value* by expanding its array references, else
        None when it has none the floor can read. Cached per text and per version of
        every array it names."""
        refs = [ref for ref in _array_refs(value, quoting) if ref[2] in self.arrays]
        if not refs:
            return None
        if any(ref[3] == _HUGE for ref in refs):
            self.over_budget = True  # a subscript too long to read
            return None
        key = (value, tuple(quoting), tuple(self.versions.get(ref[2], 0) for ref in refs))
        if key not in self.expansions:
            # Sized before anything is built, from each selection's cached size, so
            # an expansion past the budget costs its references, not its output.
            size = len(value) + sum(self._size(name, sel, quoting[s]) for s, _e, name, sel in refs)
            if size > self.budget or size > self.total_budget:
                self.over_budget = True
                return None
            self.budget -= size
            self.total_budget -= size
            words = [""]
            at = 0
            for start, end, name, selector in refs:
                elements = self.selected(name, selector, quoting[start])
                words[-1] += value[at:start]
                if elements:
                    words[-1] += elements[0]
                    words.extend(elements[1:])
                at = end
            words[-1] += value[at:]
            self.expansions[key] = _Expansion(tuple(words))
        return self.expansions[key]


class _World:
    """One combination of candidate values, in force inside a ``with`` block."""

    def __init__(self, state: "_TextState", choice: "dict[str, tuple[dict[int, str], int]]"):
        self.state = state
        self.choice = choice
        self.saved: "dict[str, tuple[dict[int, str], int]]" = {}

    def __enter__(self) -> None:
        for name, (array, version) in self.choice.items():
            self.saved[name] = (self.state.arrays[name], self.state.versions.get(name, 0))
            self.state.arrays[name] = array
            self.state.versions[name] = version

    def __exit__(self, *_exc: object) -> None:
        for name, (array, version) in self.saved.items():
            self.state.arrays[name] = array
            self.state.versions[name] = version


def _worlds(state: "_TextState", words: "list[tuple[str, list[int]]]") -> "list[_World]":
    """Every combination of candidate values the variables *words* name may hold,
    the current values first.

    Past :data:`_MAX_WORLDS`, or when a name *words* read had candidates dropped,
    the state is over budget only if one of *words* or a candidate of a name they
    read could reach a pager; otherwise no combination can, and the current values
    alone are read."""
    referenced = {
        ref[2] for value, quoting in words if "$" in value for ref in _array_refs(value, quoting)
    }
    names = {name for name in referenced if name in state.alternatives}
    choices: "list[dict[str, tuple[dict[int, str], int]]]" = [{}]
    capped = any(name in state.collapsed for name in referenced)
    for name in sorted(names):
        current = (state.arrays[name], state.versions.get(name, 0))
        options = [current, *state.alternatives[name]]
        choices = [{**choice, name: option} for choice in choices for option in options]
        if len(choices) > _MAX_WORLDS:
            capped = True
            break
    if not capped:
        return [_World(state, choice) for choice in choices]
    if (
        _grep_words_may_reach_pager(words)
        or any(state.collapsed.get(name, False) for name in referenced)
        or any(
            _array_may_reach_pager(array)
            for name in names
            for array, _version in state.alternatives[name]
        )
    ):
        state.over_budget = True
    return [_World(state, {})]


def _may_reach_pager(text: str) -> bool:
    """Whether *text* could carry a ``git grep`` pager: a pager option
    (:func:`_may_carry_pager_option`), or a pager or ``GIT_CONFIG_*`` setting. A
    setting names its value after ``=``, so a bare ``pager`` or ``GIT_CONFIG_COUNT``
    (a search pattern) carries none."""
    folded = text.lower()
    if ("pager" in folded or "git_config" in folded) and "=" in folded:
        return True
    return _may_carry_pager_option(text)


def _may_carry_pager_option(text: str) -> bool:
    """Whether *text* could be, or become, a ``git grep`` pager option: an ``-O`` in
    a short-option cluster, or a long option unless its name is plain text
    (:func:`_is_plain`) that cannot grow into ``--open-files-in-pager``. So ``--or``
    and ``--only-matching`` never count, while ``--o``, ``--op=…`` and a name bash
    may still rewrite (``--o$x``, ``--{open…,x}``, a substitution) always do.

    shortcut: an ``-O`` built from two variables (``$a$b`` with ``a=-``) is not
    seen past the caps; read the expanded word instead if that ever matters."""
    for token in text.split():
        name = token.partition("=")[0]
        if token.startswith("--") and not (
            _is_plain(name) and not (len(name) > 2 and "--open-files-in-pager".startswith(name))
        ):
            return True
        if token.startswith("-") and not token.startswith("--") and "O" in token:
            return True
    return False


#: Characters a word may hold and still be read as written: no expansion, quoting,
#: substitution placeholder or pattern character can be among them.
_PLAIN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.,:=/%+^#"
)


def _is_plain(text: str) -> bool:
    """Whether bash passes *text* to git exactly as written: every character is in
    :data:`_PLAIN_CHARS`."""
    return all(char in _PLAIN_CHARS for char in text)


def _is_literal(value: str, quoting: "list[int]") -> bool:
    """Whether bash passes *value* to git exactly as written: each character is plain
    (:data:`_PLAIN_CHARS`) or quoted literally (single quotes or a backslash)."""
    if len(quoting) != len(value):
        return _is_plain(value)
    return all(char in _PLAIN_CHARS or quoted == 1 for char, quoted in zip(value, quoting))


def _grep_words_may_reach_pager(words: "list[tuple[str, list[int]]]") -> bool:
    """Whether a ``git grep``'s own words could carry a pager. Among grep's own
    arguments only ``-O`` / ``--open-files-in-pager`` name a pager, so a word bash
    passes as written (:func:`_is_literal`) that spells a setting (``core.pager=x``,
    ``'core\\.pager\\s*='``) is a pattern or a path and only the option test applies.
    Any other word may become anything, so it gets the full :func:`_may_reach_pager`
    test. Every word is read, a value option's value included, since an expansion
    before it can shift which word is the value."""
    return any(
        _may_carry_pager_option(value) if _is_literal(value, quoting) else _may_reach_pager(value)
        for value, quoting in words
    )


def _array_may_reach_pager(array: "dict[int, str]") -> bool:
    """Whether any element of *array* could carry a pager (:func:`_may_reach_pager`)."""
    return any(_may_reach_pager(value) for value in array.values())


def _name_end(value: str, at: int) -> int:
    """The end of the shell variable name that starts at *at* (``at`` when none)."""
    end = at
    while end < len(value) and (
        value[end] == "_" or (value[end].isascii() and value[end].isalnum())
    ):
        end += 1
    if end > at and value[at].isdigit():
        return at
    return end


def _array_refs(value: str, quoting: "list[int]") -> "list[tuple[int, int, str, int | str]]":
    """``(start, end, name, selector)`` of each variable reference in *value* that
    bash expands (not single-quoted or escaped): ``$a`` and ``${a}`` select element
    0, ``${a[N]}`` element N, ``${a[@]}`` / ``${a[*]}`` every element. One pass."""
    refs: "list[tuple[int, int, str, int | str]]" = []
    i = 0
    while i < len(value):
        if value[i] != "$" or quoting[i] == 1:
            i += 1
            continue
        if value[i + 1 : i + 2] != "{":
            end = _name_end(value, i + 1)
            if end > i + 1:
                refs.append((i, end, value[i + 1 : end], 0))
                i = end
            else:
                i += 1
            continue
        end = _name_end(value, i + 2)
        name = value[i + 2 : end]
        selector: "int | str" = 0
        if end < len(value) and value[end] == "[":
            close = end + 1
            while close < len(value) and value[close].isascii() and value[close].isalnum():
                close += 1
            subscript = value[end + 1 : close]
            if not subscript and value[close : close + 1] in ("@", "*"):
                subscript = value[close]
                close += 1
            index = None if subscript in ("@", "*") else _bash_index(subscript)
            if not subscript or (index is None and subscript not in ("@", "*")):
                i += 1
                continue
            if value[close : close + 1] != "]":
                i += 1
                continue
            selector = subscript if index is None else index
            end = close + 1
        if name and value[end : end + 1] == "}":
            refs.append((i, end + 1, name, selector))
            i = end + 1
        else:
            i += 1
    return refs


def _config_env_setting(spec: str, env: "dict[str, str]") -> "list[str]":
    """The ``key=value`` setting ``--config-env=<key>=<var>`` makes, when *env*
    (the assignments before ``git`` on the same command) holds ``<var>``."""
    key, _sep, var = spec.partition("=")
    return [f"{key}={env[var]}"] if var in env else []


def _grep_subcommand(token: str, settings: "list[str]") -> "tuple[list[str], list[str]] | None":
    """``(words before grep's own arguments, paging options)`` when the subcommand
    *token* runs ``git grep``, else None: ``([], [])`` for ``grep``, or what the
    ``alias.<name>`` chain set in *settings* that ends at ``grep`` gives. An alias's
    leading ``-p`` / ``--paginate`` / ``-P`` / ``--no-pager`` are git's own options
    (``handle_alias`` runs them) and come back as the paging options; any other
    leading option changes git's environment, which git refuses in an alias, so it
    is not a grep. Alias names are matched folded, as git folds config keys; an
    alias's words are split as git splits them. A shell alias (``!...``), an
    unparsable one, or a chain that loops is not a grep."""
    aliases: "dict[str, str]" = {}
    for setting in settings:
        key, has_value, value = _git_config_value(setting)
        if has_value and key.startswith("alias."):
            aliases[key[len("alias.") :]] = value  # the last one wins
    prefix: "list[str]" = []
    options: "list[str]" = []
    seen: "set[str]" = set()
    while token != "grep":
        name = token.lower()
        if name in seen or name not in aliases:
            return None
        seen.add(name)
        try:
            alias_words = shlex.split(aliases[name])
        except ValueError:
            return None
        while alias_words and alias_words[0] in ("-p", "--paginate", "-P", "--no-pager"):
            options.append(alias_words.pop(0))
        if not alias_words or alias_words[0].startswith("-"):
            return None
        token, prefix = alias_words[0], alias_words[1:] + prefix
    return prefix, options
