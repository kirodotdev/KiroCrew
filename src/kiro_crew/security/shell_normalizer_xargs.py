"""Shell text reader: the lexical reading of an ``xargs`` pipeline.

What the xargs rebuild in :mod:`kiro_crew.security.shell_normalizer` needs from the token
stream before it attributes anything: the standalone command separators and the test for a
top-level command boundary, the flattening of a word into the plain operand words a
rebuilt argv takes, ``xargs``' own value-taking options and the skip past them, the
``xargs`` argv bounded to its stage, the split of a line into commands of pipeline stages,
and the test for a spelling the quote-stripped rebuild cannot resolve. No program
attribution happens here; ``shell_normalizer`` reads it and re-exports every name.
"""

from __future__ import annotations

import re

#: Standalone shell tokens that end one command and begin another at the top level.
_COMMAND_SEPARATOR_TOKENS = frozenset({"&", "&&", "||", ";", ";;", "\n"})
#: Shell control characters a producer word must not carry into the rebuilt argv.
_SHELL_CONTROL_CHARS_RE = re.compile(r"[;&|]")


def _flatten_operand_words(token: str) -> "list[str]":
    """The operand words *token* contributes to a rebuilt argv.

    The rebuilt command is re-tokenized and walked by the argv floors, so a shell
    control character surviving on a word (``x;`` from ``echo x; echo <name>``) would
    end the rebuilt kill argv before the name and let the self-kill through.  Every
    ``;`` ``&`` ``|`` is turned into a space -- anywhere in the word, not only at the
    end -- so the result is a flat list of ordinary words, and option flags are dropped.
    """
    return [
        word for word in _SHELL_CONTROL_CHARS_RE.sub(" ", token).split() if not word.startswith("-")
    ]


def _is_top_level_command_boundary(tokens: "list[str]", index: int) -> bool:
    """True if ``tokens[index]`` ends one top-level command and begins another.

    A standalone separator (``;`` ``&`` ``&&`` ``||`` a newline) always is one.  A word
    with a trailing ``;`` or ``&`` glued on (``cat;`` in ``echo x | cat; echo <name> |
    xargs``) is one too -- UNLESS the next token is a pipe, where the ``;`` is a quoted
    operand of the stage feeding that pipe (``echo '<name>;' | xargs``) rather than a
    separator.  A token carrying a ``|`` is never a boundary: it is a pipe (``|``,
    ``|&``) or the OR operator ``||`` (already matched as standalone), and a pipe keeps
    one pipeline together.
    """
    token = tokens[index]
    if token in _COMMAND_SEPARATOR_TOKENS:
        return True
    if "|" in token:
        return False
    if token.endswith(";") or token.endswith("&"):
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        return "|" not in following
    return False


#: ``xargs`` options that take a value in a SEPARATE following token, so that value is
#: part of the invocation and not the command ``xargs`` runs.  A ``;`` as a ``-d`` value
#: (``xargs -d ';' pkill``) would otherwise read as a command separator and hide ``pkill``.
_XARGS_VALUE_OPTIONS = frozenset(
    {
        "-a",
        "-d",
        "-e",
        "-n",
        "-p",
        "-s",
        "--arg-file",
        "--delimiter",
        "--max-args",
        "--max-procs",
        "--max-chars",
    }
)


def _skip_xargs_options(words: "list[str]", start: int) -> int:
    """Index of the first word after ``xargs``' own options, skipping the value of an
    option given in a separate token (``-d`` ``;``) so the value is not read as the
    command or as a separator."""
    index = start
    while index < len(words) and words[index].startswith("-"):
        if words[index] in _XARGS_VALUE_OPTIONS and index + 1 < len(words):
            index += 2
        else:
            index += 1
    return index


def _xargs_stage_command(tokens: "list[str]", start: int) -> "list[str]":
    """The ``xargs`` argv words from *start* to the end of its own stage.

    ``xargs``' own options (and a value given in a separate token) are skipped first, so
    ``xargs -d ';' pkill`` reads ``pkill`` rather than stopping at the ``;`` delimiter.
    The stage ends at the next pipe or top-level command boundary, so a command chained
    after the kill (``echo node | xargs killall && echo <name>``) is NOT folded into the
    kill's arguments.  A trailing control operator glued to the last word (``pkill;``) is
    flattened so the kill program is still read, then the stage ends.
    """
    command: list[str] = []
    for index in range(_skip_xargs_options(tokens, start), len(tokens)):
        token = tokens[index]
        boundary = _is_top_level_command_boundary(tokens, index) or "|" in token
        command.extend(_flatten_operand_words(token))
        if boundary:
            break
    return command


def _split_into_pipeline_stages(tokens: "list[str]") -> "list[list[list[str]]]":
    """Split *tokens* into top-level commands, each a list of pipeline stages, each a
    list of operand words.

    Group punctuation (``(`` ``{`` ``)`` ``}``) is treated as TRANSPARENT -- stripped
    from each word -- rather than tracked as depth.  Once quoting is stripped a group
    character cannot be told from a quoted one, so counting it corrupts the parse
    (``echo '(' | ...`` would inflate a depth); dropping it instead keeps the words that
    feed each stage visible, which is all the rebuild needs, and never hides a stage.
    A ``|`` starts a new stage; a top-level command separator starts a new command.
    """
    commands: list[list[list[str]]] = []
    stages: list[list[str]] = [[]]
    for index, token in enumerate(tokens):
        if _is_top_level_command_boundary(tokens, index):
            commands.append(stages)
            stages = [[]]
            continue
        if "|" in token:  # a pipe (``|`` / ``|&``) starts the next stage
            stages.append([])
            continue
        word = token.strip("(){}")
        if word:
            stages[-1].append(word)
    commands.append(stages)
    return commands


#: xargs options whose VALUE can be a separator character, so a following ``;`` / ``&``
#: is the option's data rather than a command boundary -- a spelling the exact rebuild
#: cannot resolve once quoting is gone.
#: Lower-case because the command is lower-cased before tokenizing (`-E` arrives `-e`).
_XARGS_AMBIGUOUS_OPTIONS = frozenset({"-d", "--delimiter", "-e", "--eof", "-i", "--replace"})


def _xargs_line_has_ambiguous_construct(tokens: "list[str]") -> bool:
    """True if *tokens* carry a spelling the exact rebuild cannot resolve once quoting
    is stripped: a group ``( ... )`` / ``{ ... }`` or substitution ``$( ... )`` /
    backticks (a paren, brace or backtick character), a here-string / heredoc (``<<``),
    an xargs delimiter/replace option, or a separator glued inside a word (a quoted
    ``;`` / ``&`` / ``|`` such as ``'<name>;'`` or ``[;]*<name>``).  A CLEAN standalone
    separator or pipe token is not ambiguous.
    """
    for token in tokens:
        if any(char in token for char in "(){}`"):
            return True
        if "<<" in token:
            return True
        if token in _XARGS_AMBIGUOUS_OPTIONS:
            return True
        if token in _COMMAND_SEPARATOR_TOKENS or token in {"|", "|&"}:
            continue
        if ";" in token or "&" in token or "|" in token:
            return True
    return False
