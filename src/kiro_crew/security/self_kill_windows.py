"""The Windows by-name kill programs the self-kill floor reads.

``taskkill``, PowerShell's ``Stop-Process`` (and its ``spps`` alias) and ``wmic``
end a process by name, but unlike ``pkill`` each one names its TARGET in one
place. This reader looks only at that place, so the product's name in an
exclusion filter, a later command or a printed message is not a kill:

* ``taskkill``: the ``/im`` operand, or an ``imagename eq <name>`` filter. A ``ne``
  filter spares the name.
* ``Stop-Process``/``spps``: the ``-Name`` operand or, when a pipe feeds it, an
  operand of the ``Get-Process``/``gps`` stage that starts the pipeline. A
  ``-WhatIf`` run ends nothing.
* ``wmic``: an ``=`` or ``like`` comparison in the ``where`` clause of a ``delete``
  or ``terminate``. A comparison after ``not`` spares the name.

The tokenizer splits on whitespace only, so a word glued to ``|`` or ``;``
(``kirocrew|``, ``delete;``) is split here first.

shortcut: an alias (``tk=taskkill``), a backslash path the tokenizer flattens, a
``powershell -c`` payload and a ``Where-Object`` filter that selects the product
are not read; reading them needs a PowerShell tokenizer.

``argv_floor_self`` calls :func:`_windows_self_kill` once per token frame. This
module reads the shell reader and the shared name vocabulary below it, and
nothing else.
"""

from __future__ import annotations

import re

from . import shell_normalizer as _shell_normalizer
from .shell_normalizer import _argv_programs, _data_consumer_exempt, _debracket
from .vocabulary import _SELF_NAME_RE, _WINDOWS_KILL_BY_NAME_PROGRAMS

#: A run of pipe and statement separators glued inside a word.
_GLUED_SEPARATOR_RE = re.compile(r"([|;]+)")
#: PowerShell programs that print their arguments, as ``echo`` does.
_PRINTERS = frozenset({"write-host", "write-output"})
#: The pipeline source whose operands name the processes a piped ``Stop-Process`` ends.
_GET_PROCESS = frozenset({"get-process", "gps"})
#: ``-Name`` and the prefixes PowerShell accepts for it on both cmdlets.
_NAME_FLAGS = frozenset({"-n", "-na", "-nam", "-name"})
_WHATIF = frozenset({"-whatif", "-whatif:$true"})
#: One ``where`` comparison: an optional ``not``, the field, the operator, the value.
_WMIC_COMPARISON_RE = re.compile(
    r"(\bnot\s+)?\b[\w.]+\s*(<>|!=|=|\blike\b)\s*('[^']*'|\"[^\"]*\"|[^\s)]+)"
)


def _is_separator(word: str) -> bool:
    return bool(word) and set(word) <= set("|;&\n")


def _split_glued(tokens: "list[str]") -> "list[str]":
    """*tokens* with each blank-free word glued to ``|`` or ``;`` split around them.

    A word with a blank in it was quoted, and a quoted separator is literal.
    """
    words: list[str] = []
    for token in tokens:
        if (
            _is_separator(token)
            or not _GLUED_SEPARATOR_RE.search(token)
            or any(c.isspace() for c in token)
        ):
            words.append(token)
            continue
        words.extend(part for part in _GLUED_SEPARATOR_RE.split(token) if part)
    return words


def _names_product(word: str) -> bool:
    """True if *word* names the product, read the way the ``pkill`` operands are."""
    n = _shell_normalizer
    views = (_debracket(word), n._normalize_operand(word), n._resolved_word_view(word))
    return any(_SELF_NAME_RE.search(view) for view in views)


def _own_args(words: "list[str]", i: int) -> "list[str]":
    args: list[str] = []
    for word in words[i + 1 :]:
        if _is_separator(word):
            break
        args.append(word)
    return args


def _flag_values(args: "list[str]", flags: "frozenset[str]") -> "list[str]":
    """The values *args* gives any of *flags*, as ``-flag value`` or ``-flag:value``."""
    values = []
    for k, arg in enumerate(args):
        flag, colon, value = arg.partition(":")
        if flag in flags:
            values.append(value if colon else (args[k + 1] if k + 1 < len(args) else ""))
    return values


def _taskkill_targets(args: "list[str]") -> "list[str]":
    """The image names a ``taskkill`` argv ends: ``/im`` operands and ``eq`` filters."""
    targets = []
    for k, arg in enumerate(args[:-1]):
        flag = arg.lstrip("/-")
        if flag == arg:
            continue
        if flag == "im":
            targets.append(args[k + 1])
        elif flag == "fi":
            parts = args[k + 1].split(None, 2)
            if len(parts) == 3 and parts[0] == "imagename" and parts[1] == "eq":
                targets.append(parts[2])
    return targets


def _wmic_kills_product(args: "list[str]") -> bool:
    if not {"delete", "terminate"}.intersection(args):
        return False
    for match in _WMIC_COMPARISON_RE.finditer(" ".join(args)):
        negated, operator, value = match.groups()
        if not negated and operator in ("=", "like") and _names_product(value):
            return True
    return False


def _pipeline_source_names_product(words: "list[str]", pipe_at: int) -> bool:
    """True if the pipeline ending in ``words[pipe_at]`` starts with a product ``Get-Process``.

    Walks back to the separator that starts the pipeline, skipping any inside a
    ``{ }`` script block (``Where-Object { $a; $b }``).
    """
    depth = 0
    start = 0
    for k in range(pipe_at - 1, -1, -1):
        word = words[k]
        depth += word.count("}") - word.count("{")
        if depth <= 0 and _is_separator(word) and word != "|":
            start = k + 1
            break
    if start >= pipe_at:
        return False
    if _shell_normalizer._program_basename(words[start]) not in _GET_PROCESS:
        return False
    for arg in _own_args(words, start):
        # A flag's own ``:value`` is read; a bare ``-ErrorAction`` value cannot name it.
        if _names_product(arg.partition(":")[2] if arg.startswith("-") else arg):
            return True
    return False


def _windows_self_kill(tokens: "list[str]") -> bool:
    """True if a Windows by-name kill in this token frame targets the product."""
    if not any(p in token for token in tokens for p in _WINDOWS_KILL_BY_NAME_PROGRAMS):
        return False
    words = _split_glued(tokens)
    programs = _argv_programs(words)
    disqualified = _shell_normalizer._data_consumer_command_disqualified(words)
    for i, word in enumerate(words):
        program = _shell_normalizer._program_basename(word).removesuffix(".exe")
        if program not in _WINDOWS_KILL_BY_NAME_PROGRAMS:
            continue
        # ``echo taskkill /im <name>`` and ``Write-Host ... taskkill`` print words.
        if _data_consumer_exempt(i, word, programs, words, command_disqualified=disqualified):
            continue
        if i and programs[i] in _PRINTERS and not disqualified:
            continue
        args = _own_args(words, i)
        if program == "taskkill":
            hit = any(_names_product(t) for t in _taskkill_targets(args))
        elif program == "wmic":
            hit = _wmic_kills_product(args)
        elif _WHATIF.intersection(args):
            hit = False
        else:
            hit = any(_names_product(v) for v in _flag_values(args, _NAME_FLAGS)) or (
                i > 0 and words[i - 1] == "|" and _pipeline_source_names_product(words, i - 1)
            )
        if hit:
            return True
    return False
