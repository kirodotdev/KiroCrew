"""Argv floor for the two recursive-force ``rm`` catalog rules.

The catalog patterns ``rm -rf /.*`` and ``rm -rf ~.*`` match one contiguous
spelling. A shell runs the same wipe for ``rm -fr /``, ``rm -r -f /``,
``rm --recursive --force /`` and ``rm -rf $HOME``, so this floor reads each ``rm``
command's own words instead: recursive and force in any packing, order or long
form, against the root or the home directory ITSELF.

Scope is deliberately that narrow. A descendant (``rm -fr /tmp/build``,
``rm -rf $HOME/.cache``) is left to the catalog regex exactly as it reads today,
and wrappers or interpreter bodies are left to the regex and the OS sandbox.

Frames come from ``_shell_payload_walk``, so ``bash -c`` and ``$(...)`` bodies are
read as commands of their own. Each frame is split into words on the shared quote
state machine (``_iter_shell_chars``), keeping which characters were quoted: a
quoted ``'~'`` or ``'$HOME'`` is a literal name, not the home directory, and a
quoted ``'/*'`` is not a glob. Comments, heredoc bodies and redirections are not
``rm`` operands. A body only a data consumer receives (``echo bash -c '...'``), or
a substitution inside single quotes, a comment or a quoted heredoc, prints rather
than runs: a frame counts only when some executed frame yields it.
"""

from __future__ import annotations

import os
import posixpath
import re
from typing import NamedTuple

from . import shell_normalizer as _shell
from .denied_rules import BUILTIN_DENIED_RULES

#: The catalog row each wipe target maps to. The floor is gated on that row's
#: pattern, so an operator who disables the row disables the floor with it.
_RM_FLOOR_RULE_IDS: dict[str, str] = {
    "root": "local-destructive-rm-rf-root",
    "home": "local-destructive-rm-rf-home",
}
_RM_FLOOR_BY_KIND: dict[str, tuple[str, str]] = {
    kind: (rule.id, rule.pattern)
    for kind, rule_id in _RM_FLOOR_RULE_IDS.items()
    for rule in BUILTIN_DENIED_RULES
    if rule.id == rule_id
}
_RM_FLOOR_NOTE = (
    "Matched structurally on the command's argv, not by the pattern text above: "
    "rm with recursive and force flags, in any spelling, targets this directory itself."
)

_ROOT_RE = re.compile(r"/+")
# ``${HOME:-x}`` / ``${HOME:?}`` / ``${HOME:=x}`` are HOME whenever it is set;
# ``${HOME:+x}`` is the alternate text instead, so it is not listed.
_HOME_VAR_RE = re.compile(r"\$(?:HOME(?![A-Za-z0-9_])|\{HOME(?::?[-?=][^}]*)?\})")
_OPERATORS = ";&|\n()"
_SUBSTITUTION_OPENERS = ("$(", "<(", ">(", "`")

#: One word: its text with quotes removed, and per character whether the shell
#: would expand it (``u`` unquoted, ``d`` inside double quotes, ``s`` literal).
_Word = tuple[str, str]


class _Scan(NamedTuple):
    words: "list[_Word]"
    #: Offsets (in the decoded text) of ``$`` / `` ` `` / ``<`` / ``>`` that can
    #: open a substitution the shell runs.
    live: "set[int]"
    text: str


def _heredoc_end(source: str, start: int, tags: "list[tuple[str, bool, bool]]") -> int:
    """Offset just past the heredoc bodies that begin at *start*."""
    pos = start
    for tag, strip_tabs, _expands in tags:
        while pos < len(source):
            end = source.find("\n", pos)
            line = source[pos : len(source) if end == -1 else end]
            pos = len(source) if end == -1 else end + 1
            if (line.lstrip("\t") if strip_tabs else line) == tag:
                break
    return pos


def _scan(source: str) -> _Scan:
    """Split one command line into words and control operators, keeping quote kinds.

    ANSI-C and locale quoting are resolved first by the shared decoder, which
    re-quotes any value that must stay literal. An unquoted ``#`` at a word start
    drops the rest of the line, and a heredoc body is dropped through its tag line;
    an unquoted-tag heredoc body still expands substitutions, a quoted one does not.
    """
    source = _shell._decode_shell_quoted_literals(source)
    words: list[_Word] = []
    live: set[int] = set()
    text: list[str] = []
    kinds: list[str] = []
    started = False
    # (tag, strip tabs, body expands)
    tags: list[tuple[str, bool, bool]] = []
    base = 0

    def _end() -> None:
        nonlocal started
        if started:
            word = ("".join(text), "".join(kinds))
            prev = words[-1] if words else ("", "")
            if prev[0] in ("<<", "<<-") and prev[1] in ("uu", "uuu"):
                tags.append((word[0], prev[0] == "<<-", set(word[1]) <= {"u"}))
            elif word[0].startswith("<<") and word[1][:2] == "uu" and word[0][2:3] != "<":
                marker = _shell._heredoc_marker(word[0])
                if marker:
                    dash = word[0].startswith("<<-")
                    tags.append((marker, dash, set(word[1][3 if dash else 2 :]) <= {"u"}))
            words.append(word)
        text.clear()
        kinds.clear()
        started = False

    while base <= len(source):
        restart = None
        comment = False
        for step in _shell._iter_shell_chars(source[base:]):
            char = step.char
            at = base + step.offset
            if comment:
                if not (step.active and char == "\n"):
                    continue
                comment = False
            if len(step.text) == 1 and step.state != 1 and char in "$`<>":
                live.add(at)
            if len(step.text) == 2 or step.trailing_escape:
                text.append(char)
                kinds.append("s")
                started = True
            elif step.active and char in "'\"":
                started = True
            elif not step.active and step.state == 0 and char in "'\"":
                continue
            elif not step.active:
                text.append(char)
                kinds.append("d" if step.state == 2 else "s")
            elif char == "#" and not started:
                comment = True
                live.discard(at)
            elif char.isspace() and char != "\n":
                _end()
            elif (
                char == "&"
                and source[at + 1 : at + 2] == ">"
                or (char == "&" and text and text[-1] in "<>")
            ):
                text.append(char)
                kinds.append("u")
                started = True
            elif char == "(" and text and text[-1] == "=":
                # ``a=(...)`` is an array assignment, not a subshell.
                text.append(char)
                kinds.append("u")
            elif char in _OPERATORS:
                _end()
                if words and words[-1] == (char, "u") and char in "&|":
                    words[-1] = (char * 2, "uu")
                else:
                    words.append((char, "u"))
                if char == "\n" and tags:
                    restart = _heredoc_end(source, at + 1, tags)
                    if any(expands for _tag, _dash, expands in tags):
                        live |= {i for i in range(at + 1, restart) if source[i] in "$`"}
                    tags.clear()
                    break
            else:
                text.append(char)
                kinds.append("u")
                started = True
        if restart is None:
            break
        base = restart
    _end()
    return _Scan(words, live, source)


def _split_redirect(word: _Word) -> "tuple[_Word | None, bool]":
    """The operand glued before an unquoted redirection, and whether a target follows.

    ``~>/dev/null`` is the operand ``~`` plus a redirection; ``2>x`` is only a
    redirection (a leading fd number is part of the operator).
    """
    text, kinds = word
    cut = next((i for i, (c, k) in enumerate(zip(text, kinds)) if c in "<>" and k == "u"), -1)
    if cut == -1:
        return word, False
    if cut and text[cut - 1] == "&" and kinds[cut - 1] == "u":
        cut -= 1
    head = text[:cut]
    operand = None if not head or head.isdigit() else (head, kinds[:cut])
    return operand, text[cut:].lstrip("<>&|") == ""


def _flag_kinds(token: str) -> "set[str]":
    """The ``recursive`` / ``force`` meanings an ``rm`` option word carries."""
    if token.startswith("--"):
        # GNU accepts any unambiguous prefix of a long option; three characters
        # is the shortest prefix that names only one of these two.
        name = token.split("=", 1)[0]
        if len(name) >= 3 and "--recursive".startswith(name):
            return {"recursive"}
        if len(name) >= 3 and "--force".startswith(name):
            return {"force"}
        return set()
    kinds = set()
    if "r" in token[1:] or "R" in token[1:]:
        kinds.add("recursive")
    if "f" in token[1:]:
        kinds.add("force")
    return kinds


def _is_dir_itself(rest: str) -> bool:
    """True when *rest*, after a directory spelling, still names that directory."""
    return bool(_ROOT_RE.fullmatch(posixpath.normpath("/" + rest)))


def _folded(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/")).casefold()


def _target_kind(word: _Word, homes: "frozenset[str]") -> "str | None":
    """``"root"`` or ``"home"`` when *word* names that directory or the glob over it."""
    text, kinds = word
    if text.endswith("/*") and kinds[-1] == "u":
        text, kinds = text[:-1], kinds[:-1]
    if not text:
        return None
    if text.startswith("/") and _ROOT_RE.fullmatch(posixpath.normpath(text)):
        return "root"
    if text.startswith("~") and kinds[0] == "u" and (len(text) == 1 or text[1] == "/"):
        return "home" if _is_dir_itself(text[1:]) else None
    match = _HOME_VAR_RE.match(text)
    # The whole reference must come from ONE quoting run: ``"$HO"ME`` is the
    # variable ``HO`` followed by literal text, not ``$HOME``.
    if match and len(set(kinds[: match.end()])) == 1 and kinds[0] != "s":
        return "home" if _is_dir_itself(text[match.end() :]) else None
    # A nested frame's text arrives with ``$HOME`` already expanded by the walk,
    # and on Windows the word split has read that path's backslashes as escapes.
    if _folded(text) in homes:
        return "home"
    return None


def _frame_targets(scan: _Scan, homes: "frozenset[str]") -> "set[str]":
    """Which of root / home a recursive-force ``rm`` in one command line deletes."""
    words = scan.words
    ends = [
        (_shell._ends_argv(text) or text == ")") and set(kinds) <= {"u"} for text, kinds in words
    ]
    # A quoted word that merely LOOKS like an operator is data, so it is masked
    # before program attribution reads it.
    programs = _shell._argv_programs(
        [
            text if ends[i] or not _shell._ends_argv(text) else "_"
            for i, (text, _kinds) in enumerate(words)
        ]
    )
    found: set[str] = set()
    flags: set[str] = set()
    targets: set[str] = set()
    options_done = False
    skip_next = False
    for index, word in enumerate(words):
        is_rm_argument = (
            index > 0
            and programs[index].lower() == "rm"
            and programs[index - 1].lower() == "rm"
            and not ends[index]
        )
        if is_rm_argument and skip_next:
            skip_next = False
        elif is_rm_argument:
            operand, skip_next = _split_redirect(word)
            text = operand[0] if operand else ""
            if operand is None:
                pass
            elif not options_done and text == "--":
                options_done = True
            elif not options_done and text.startswith("-") and len(text) > 1:
                flags |= _flag_kinds(text)
            else:
                target = _target_kind(operand, homes)
                if target:
                    targets.add(target)
        if ends[index] or index + 1 == len(words):
            if {"recursive", "force"} <= flags:
                found |= targets
            flags, targets, options_done, skip_next = set(), set(), False, False
    return found


def _live_substitution_bodies(scan: _Scan, source: str) -> "set[str]":
    """Substitution bodies of *source* that run: one with at least one live opener."""
    text = scan.text
    bodies: set[str] = set()
    for body in _shell._substitution_bodies(_shell._fold_line_continuations(source)):
        spots = []
        start = text.find(body)
        while start != -1:
            spots += [
                start - len(o) for o in _SUBSTITUTION_OPENERS if text.startswith(o, start - len(o))
            ]
            start = text.find(body, start + 1)
        # A body not found as written cannot be proven inert, so it is kept.
        if not spots or any(spot in scan.live for spot in spots):
            bodies.add(_shell._decode_printf_escapes(body))
    return bodies


def _executed_children(scan: _Scan, source: str, tokens: "list[str]") -> "set[str]":
    """The bodies this frame RUNS: every payload except one only a data consumer receives."""
    programs = _shell._argv_programs(tokens)
    disqualified = _shell._data_consumer_command_disqualified(tokens)
    positions: dict[str, list[int]] = {}
    for index, token in enumerate(tokens):
        positions.setdefault(token, []).append(index)
    children: set[str] = set()
    for payload in _shell._nested_shell_payloads(tokens):
        occurrences = positions.get(payload, [])
        if occurrences and all(
            _shell._data_consumer_exempt(
                i, payload, programs, tokens, command_disqualified=disqualified
            )
            for i in occurrences
        ):
            continue
        children.add(_shell._decode_printf_escapes(payload))
    return children | _live_substitution_bodies(scan, source)


def _rm_wipe_targets(text: str) -> "set[str]":
    """Which of root / home a recursive-force ``rm`` in *text* deletes.

    *text* is the command as submitted: ``$HOME`` is case-sensitive, so a
    lowercased copy would read an unrelated ``$home`` variable as home.
    """
    home = os.path.expanduser("~")
    homes = frozenset({_folded(home), _folded(home.replace("\\", ""))}) - {"."}
    frames = _shell._shell_payload_walk(text)
    scans = [_scan(source) for source, _tokens in frames]
    hits = [_frame_targets(scan, homes) for scan in scans]
    if not any(hits):
        return set()
    # A frame counts only when an executed frame yields it. The walk keeps one
    # frame per distinct text, so a printed copy and a run copy of the same body
    # share a frame; reachability through any executed occurrence keeps it.
    executed = {frames[0][0]}
    expanded: set[str] = set()
    changed = True
    while changed:
        changed = False
        for (source, tokens), scan in zip(frames, scans):
            if source in executed and source not in expanded:
                expanded.add(source)
                executed |= _executed_children(scan, source, tokens)
                changed = True
    found: set[str] = set()
    for (source, _tokens), targets in zip(frames, hits):
        if source in executed:
            found |= targets
    return found
