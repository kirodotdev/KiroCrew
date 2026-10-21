"""Recursive-force ``rm`` seatbelt behind the two ``local-destructive-rm-rf-*`` rows.

Refuses the common respellings of ``rm -rf /`` and ``rm -rf ~`` the rows miss, also inside a
nested shell's ``-c`` operand. A seatbelt, not a parser: other shell shapes stay
with the rows and the OS sandbox.
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
_CAN_FIRE_RE = re.compile(r"r(?:[^\w\s]|\\\n)*m")  # a literal rm; quote splices, continuations
_EXPANDED_HOME = "\0\x01HOME"  # a home an outer shell expanded; no decoded text holds a NUL
_OUTER_HOME_RE = re.compile(r"\$(?:HOME(?![A-Za-z0-9_])|\{HOME\})")
_HOME_WORDS = frozenset({"~", "$HOME", "${HOME}", _EXPANDED_HOME})
_CUT_CHARS = frozenset(";&|\n`()")
_ANSI_CONTROL_RE = re.compile(r"\\(?:c(\\\\?|.)|(.))", re.DOTALL)
_ANSI_NAMED_CONTROLS = {
    "a": 7,
    "b": 8,
    "e": 27,
    "E": 27,
    "f": 12,
    "n": 10,
    "r": 13,
    "t": 9,
    "v": 11,
}
_VALUE_LONG_OPTS = frozenset({"--rcfile", "--init-file"})
_MAX_SHELL_DEPTH = 8


def _can_fire(command: str) -> bool:
    return bool(_CAN_FIRE_RE.search(command))


def _target_kind(word: str) -> "str | None":
    """``root`` / ``home`` when *word* is that dir itself, with an optional ``/`` or ``/*``."""
    base = word[:-2] if word.endswith("/*") else word
    base = base.rstrip("/") if base.strip("/") else ""
    if word.startswith("/") and base == "":
        return "root"
    return "home" if base in _HOME_WORDS else None


def _ansi_control(match: "re.Match[str]") -> str:
    """A named control escape or ``\\cX`` as the hex escape of the byte bash makes of it."""
    if match[2] is not None:
        named = _ANSI_NAMED_CONTROLS.get(match[2])
        return match[0] if named is None else f"\\x{named:02x}"
    if not match[1].isascii():
        return match[0]
    x = match[1][0]  # ``\\c\\`` and ``\\c\\\\`` are both 0x1c, measured on bash 5.2
    code = 0x7F if x == "?" else ord(x.upper()) & 0x1F
    return f"\\x{code:02x}" if code else match[0]


def _decode_ansi(body: str) -> str:
    """A ``$'...'`` body as bash decodes it, a newline kept wherever bash makes one."""
    # the shared decoder flattens control escapes to a space, which hides a newline
    return _shell._decode_ansi_c_body(_ANSI_CONTROL_RE.sub(_ansi_control, body))


def _bash_words(raw: str) -> "list[str]":
    """*raw* split and quote-removed as bash does: a double quote keeps ``\\`` before most chars."""
    words: list[str] = []
    word: "list[str] | None" = None
    skip, ansi_body = 0, None
    for step in _shell._iter_shell_chars(raw):
        if step.offset < skip:
            continue
        if ansi_body is not None:  # inside ``$'...'``: decoded whole when it closes
            if step.text == "'" and step.state == 0:
                word, ansi_body = [*(word or []), _decode_ansi(ansi_body)], None
            else:
                ansi_body += step.text
            continue
        live = step.text == "$" and step.state != 1
        if live and (home := _OUTER_HOME_RE.match(raw, step.offset)):
            # this shell expands the home here, so no escape one level down can make it literal
            word, skip = [*(word or []), _EXPANDED_HOME], home.end()
            continue
        if len(step.text) == 2 and step.char == "\n":
            continue  # a line continuation joins two lines and starts no word
        if step.active and step.char in " \t":
            if word is not None:
                words.append("".join(word))
            word = None
            continue
        word = [] if word is None else word
        if len(step.text) == 2:  # an escape pair; ``\\``-newline joins the line
            kept = step.state == 2 and step.char not in '$`"\\\n'
            word.append(step.text if kept else step.char.replace("\n", ""))
        elif not (step.char in "'\"" and (step.active or step.state == 0)):
            word.append(step.char)  # a quote that opens or closes is removed
        elif step.ansi and step.state == 1:
            ansi_body = ""  # the ``$`` of ``$'...'`` goes with its quote
            word = word[:-1] if word[-1:] == ["$"] else word
    return words if word is None else [*words, "".join(word)]


def _shell_payload(raw: str) -> "str | None":
    """The ``-c`` operand when *raw* runs a nested shell, read from bash's own words."""
    words = _bash_words(raw)
    while words and ENV_ASSIGNMENT_RE.match(words[0]):
        words = words[1:]
    if not words or _shell._program_basename(words[0]) not in _shell._NESTED_SHELL_PROGRAMS:
        return None
    args, command_mode = iter(words[1:]), False
    for arg in args:
        if arg in ("--", "-"):  # each ends the options; a lone ``+`` is skipped (bash 5.2)
            return next(args, None) if command_mode else None
        if not arg.startswith(("-", "+")):
            return arg if command_mode else None  # the first operand: payload or script
        letters = "" if arg[1:2] == "-" else arg[1:]  # a short cluster such as ``-eo``
        command_mode = command_mode or (arg[0] == "-" and "c" in letters)
        for _ in range(letters.count("o") + letters.count("O") + (arg in _VALUE_LONG_OPTS)):
            next(args, None)  # ``-o pipefail``: each option value is not the payload
    return None


def _segments(command: str) -> "list[tuple[str, str]]":
    """*command* cut at live separators, as (``$``-masked, raw) pairs; stops at a heredoc."""
    out: list[tuple[str, str]] = []
    seg: list[str] = []
    raw: list[str] = []
    pos, state = 0, 0
    while pos < len(command):
        text, resume, resume_state = command[pos:], len(command), 0
        for step in _shell._iter_shell_chars(text, state):
            if step.active and text.startswith("<<", step.offset):
                break  # heredoc bodies are data; the rows and the sandbox own them
            if step.active and step.char == "#" and _shell._opens_comment(text, step.offset):
                newline = text.find("\n", step.offset)  # a comment's quotes open nothing
                resume = len(command) if newline < 0 else pos + newline
                break
            if step.state == 2 and text.startswith("$(", step.offset):
                # bash reads "$(…)" from an unquoted start, then the double quote resumes
                body = text[step.offset + 2 :]
                close, proven = _shell._matching_close_paren(body, 0)
                out.extend(_segments(body[: close - 1 if proven else close]))
                seg.append("\0")
                raw.append("\0")
                resume, resume_state = pos + step.offset + 2 + close, 2
                break
            if step.active and step.char in _CUT_CHARS:
                out.append(("".join(seg), "".join(raw)))
                seg, raw = [], []
                continue
            if len(step.text) == 2 and step.char == "\n" and step.state != 1:
                continue  # a line continuation: bash joins the lines, but not in ``$'...'``
            raw.append(step.text)
            if step.char in "$~" and (
                len(step.text) == 2 or step.state == 1 or (step.char == "~" and step.state)
            ):
                seg.append("\0")  # an escaped, single-quoted $ or any quoted ~ is literal
            else:
                seg.append(step.text)
        pos, state = resume, resume_state
    return [*out, ("".join(seg), "".join(raw))]


def _rm_wipe_targets(command: str, depth: int = 0) -> "set[str]":
    """The targets (``root`` / ``home``) a recursive-force ``rm`` in *command* wipes."""
    if depth > _MAX_SHELL_DEPTH:
        raise RecursionError("rm floor: nested shells too deep")  # the caller fails closed
    kinds: set[str] = set()
    for segment, raw in _segments(command):
        payload = _shell_payload(raw)
        if payload is not None:
            kinds |= _rm_wipe_targets(payload, depth + 1)  # bash re-reads the operand
            continue
        words = _shell._shell_tokens(segment)
        while words and ENV_ASSIGNMENT_RE.match(words[0]):
            words = words[1:]
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
