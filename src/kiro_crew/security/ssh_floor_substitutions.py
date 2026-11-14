"""Substitution readers for the ssh-to-self floor in ``argv_floor_ssh.py``.

Which backticks bash pairs, the empty spans it splices out, and the commands a
static splice of an ``echo``/``printf`` body would erase.  Quote state,
comments and heredoc bodies come from the shared reader
(:func:`shell_commands.read_commands`), never from a reader of this module's
own.  ``argv_floor_ssh.py`` re-imports every name a caller imports from it.
"""

from __future__ import annotations

import re

from .argv_floor_self import _static_substitution_output
from .shell_commands import SUBST, ShellCommand, read_commands

# A FLAT command substitution -- no nested substitution characters in the
# body -- is the only shape ``_static_substitution_output`` can decide
# statically.  Both spellings are matched; group 1 xor group 2 carries the
# body.
_FLAT_SUBSTITUTION_RE = re.compile(r"\$\(([^()`$]*)\)|`([^`()$]*)`")


# An EMPTY command substitution expands to nothing, so ``s$()sh`` runs ``ssh``
# and ``local$()host`` resolves to ``localhost`` -- the same glue-evasion the
# self-kill floor names for ``p$()kill``, but aimed at the verb gate and at the
# operand.  Collapsing these in the SOURCE text (before the gate and before
# tokenization) makes both the substring gate and every resolved operand read
# the spliced-out spelling.  Only ``$()``/backticks are matched: an empty
# ``${}`` is a bash syntax error, not an expansion, so it never splices.
_EMPTY_EXPANSION_RE = re.compile(r"\$\(\s*\)|`\s*`")


def _live_backticks(text: str, commands: "list[ShellCommand]") -> "list[int]":
    """Offsets of the backticks in *text* that open or close a substitution.

    The shared reader keeps every quoted or escaped character as a step and
    every heredoc body as a span, and steps over a comment.  So a live
    unquoted backtick is a cut the reader left uncovered, with no uncovered
    ``#`` before it on its line; a backtick span inside double quotes is one
    :data:`SUBST` step, whose ends are live.
    """
    covered = bytearray(len(text))
    live: "list[int]" = []
    for command in commands:
        for step in command.steps:
            covered[step.start : step.end] = b"\1" * (step.end - step.start)
            if step.text == SUBST and text.startswith("`", step.start):
                live.append(step.start)
                if step.end - 1 > step.start and text[step.end - 1] == "`":
                    live.append(step.end - 1)
        for doc in command.heredocs:
            end = max(doc.tail, doc.end)
            covered[doc.start : end] = b"\1" * (end - doc.start)
    in_comment = False
    for i, ch in enumerate(text):
        if covered[i] or ch == "\n":
            in_comment = False
        elif ch == "#":
            in_comment = True
        elif ch == "`" and not in_comment:
            live.append(i)
    return sorted(set(live))


def _read(text: str) -> "list[ShellCommand] | None":
    """The shared reader's commands of *text*, or None when it cannot read them.

    shortcut: the reader recurses once per double-quoted substitution level,
    so a line nested about a thousand deep exhausts the Python stack; such a
    line gets only the pattern reading.  Drop this guard when the reader
    walks nested bodies with an explicit stack.
    """
    try:
        return read_commands(text, heredocs=True)
    except RecursionError:
        return None


_PAIRED_BACKTICK_CAP = 64


def _collapse_paired_empty_expansions(text: str) -> str:
    """*text* with each empty ``$()`` and each empty backtick PAIR removed.

    Backticks pair left to right, as bash reads them.  ``_EMPTY_EXPANSION_RE``
    cannot tell the gap between two spans (```date` `ssh``) from an empty
    span, so it glues the verb onto ``date``.  This reading is judged BESIDE
    that one, never instead of it, so a line it pairs differently keeps the
    pattern's verdict too.  Backticks bash does not expand (quoted, escaped,
    in a comment or a heredoc body) are collapsed exactly as the pattern does,
    so that text reads the same in both.

    The caller walks this reading as a line only up to ``_PAIRED_BACKTICK_CAP``
    backticks.  shortcut: that second walk is O(verbs x tokens), so a line of
    thousands of spans would cost seconds; past the cap only the pattern
    reading is walked.  Lift the cap when the walk is linear.
    """
    commands = _read(text)
    if commands is None:
        return _EMPTY_EXPANSION_RE.sub("", text)
    ticks = _live_backticks(text, commands)
    live = set(ticks)
    drop: "set[int]" = set()
    for open_at, close_at in zip(ticks[::2], ticks[1::2]):
        if not text[open_at + 1 : close_at].strip():
            drop.update(range(open_at, close_at + 1))
    for match in _EMPTY_EXPANSION_RE.finditer(text):
        if text[match.start()] == "`" and not {match.start(), match.end() - 1} <= live:
            drop.update(range(match.start(), match.end()))
    at = text.find("$(")
    while at >= 0:
        end = at + 2
        while end < len(text) and text[end].isspace():
            end += 1
        if end < len(text) and text[end] == ")":
            drop.update(range(at, end + 1))
        at = text.find("$(", at + 2)
    return "".join(ch for i, ch in enumerate(text) if i not in drop)


_SPLICED_LIST_CHAR_CAP = 2048


def _opens_substitution(text: str, at: int) -> bool:
    """True when the ``$`` at *at* opens ``$(``: an odd run of unescaped ``$``.

    ``$$`` is the shell's PID, so ``$$(`` opens nothing, and ``\\$`` is data.
    """
    run_start = at
    while run_start > 0 and text[run_start - 1] == "$":
        run_start -= 1
    slashes = run_start
    while slashes > 0 and text[slashes - 1] == "\\":
        slashes -= 1
    return (at - run_start) % 2 == 0 and (run_start - slashes) % 2 == 0


def _spliced_command_lists(text: str) -> "list[str]":
    """Each command inside a substitution body that the static splice would erase.

    The splice in :func:`_is_ssh_to_self` reads an ``echo``/``printf`` body
    as its first word, which drops the later commands of a list
    (``$(echo hi; ssh localhost)``) that bash still runs.  The commands are
    the shared reader's, kept only where they lie wholly inside such a body:
    quoted text, a comment and a heredoc body produce no command there, and a
    glued ``hi;echo`` is two.  A body is at most ``_SPLICED_LIST_CHAR_CAP``
    characters, and so is the total of the commands returned.
    """
    if "$(" not in text and "`" not in text:
        return []
    spans: "list[tuple[int, int]]" = []
    for match in _FLAT_SUBSTITUTION_RE.finditer(text):
        group = 1 if match.group(1) is not None else 2
        body = match.group(group)
        # shortcut: a body past the cap, and commands past the total, are left
        # to the splice alone, because the walk is O(verbs x tokens) and padded
        # bodies would cost seconds; lift the caps when the walk is linear.
        if len(body) > _SPLICED_LIST_CHAR_CAP or not any(op in body for op in ";&|\n"):
            continue
        if group == 1 and not _opens_substitution(text, match.start()):
            continue
        if _static_substitution_output(body) != "\x00":
            spans.append((match.start(group), match.end(group)))
    commands = _read(text) if spans else None
    if commands is None:
        return []
    inside: "dict[tuple[int, int], list[str]]" = {span: [] for span in spans}
    for command in commands:
        if not command.steps:
            continue
        start, end = command.steps[0].start, command.steps[-1].end
        span = next((s for s in spans if s[0] <= start and end <= s[1]), None)
        if span is not None:
            inside[span].append("".join(step.text for step in command.steps))
    found: "list[str]" = []
    budget = _SPLICED_LIST_CHAR_CAP
    # A body the reader reads as ONE command (``echo\;echo``, an escaped or
    # quoted operator) is all the splice already read; only a list is erased.
    for words in inside.values():
        if len(words) < 2:
            continue
        budget -= sum(map(len, words))
        if budget < 0:
            break
        found.extend(words)
    return list(dict.fromkeys(found))
