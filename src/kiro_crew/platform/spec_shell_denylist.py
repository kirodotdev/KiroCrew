"""A KAS spec's ``toolsSettings.shell.deniedCommands``, enforced at Crew's own gate.

The KAS projection carries a ``deniedCommands`` entry to the engine only when it
is a literal plus ``.*`` (``kas_permissions._regex_to_glob``); any other entry has
no KAS rule. Crew records every entry instead and its gate's
``spec-shell-denylist`` row refuses a shell call one names, on every approval
policy.

kiro-cli's own reading, which this mirrors: each entry is compiled as
``Regex::new(&format!(r"\\A{dc}\\z"))`` and matched against the command, so an entry
must match the WHOLE command (the same anchoring it gives ``allowedCommands``,
and the one :mod:`kiro_crew.platform.allowed_commands` documents). Crew matches the
same anchored pattern against the whole command AND against every piece the
command chains, pipes, backgrounds or substitutes, so it never refuses less than
kiro-cli does.

Each entry is the user's own pattern, compiled from the spec at runtime. It is
read first by :func:`allowed_commands.parse`, which matches in linear time. Any
other entry is compiled with Python's :mod:`re` exactly as kiro-cli writes it,
``\\A{entry}\\z``, so a top-level ``a|b`` keeps Rust's precedence (starts with
``a``, or ends with ``b``). An entry holding ``|`` always takes that path,
because the linear reader reads a top-level ``a|b`` as exactly ``a`` or exactly
``b``, which is narrower. Python's ``re`` backtracks, so those entries are
matched in a child process killed after :data:`_FALLBACK_TIMEOUT_SECS`. A
timeout, like an entry that compiles in neither engine or uses syntax Python
reads but kiro-cli's Rust engine refuses or reads differently (look-around,
backreferences, possessive or atomic groups, POSIX ``[:class:]``), is UNKNOWN:
kiro-cli would refuse every command for such an entry, so Crew's gate lets no
auto-approve path run the call, and a person decides.

A leaf: stdlib and :mod:`kiro_crew.platform.allowed_commands` only.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any

from kiro_crew.platform import allowed_commands
from kiro_crew.platform.allowed_commands import _MAX_MATCH_STEPS, _match

#: What ends one command and starts another to a shell, plus the substitution and
#: grouping openers. Splitting at every one, quoted or not, only adds pieces, so
#: the refusal reads wider, never narrower.
_BREAKS = ("&&", "||", "$(", ";", "|", "&", "`", "(", ")", "{", "}", "\n", "\r")

#: Bound on the pieces one command is split into; past it only the whole command
#: is matched, as kiro-cli matches it.
_MAX_PIECES = 256

#: Pieces of syntax that Rust's ``regex`` crate refuses or reads differently from
#: Crew's linear reader or Python's ``re``. ``\\<``, ``\\>`` and ``\\b{`` are word
#: boundaries to Rust and literal text to both readers here. An entry holding one
#: is not evaluated by either: Crew asks a person instead. Plain substring tests,
#: never a pattern.
_RUST_INCOMPATIBLE = (
    "(?=",
    "(?!",
    "(?<=",
    "(?<!",
    "(?>",
    "(?P=",
    "[:",
    "*+",
    "++",
    "?+",
    "}+",
    "\\<",
    "\\>",
    "\\b{",
)


def _reads_differently(raw: str) -> bool:
    """Whether *raw* holds syntax no reader here reads as kiro-cli's Rust engine does."""
    if any(piece in raw for piece in _RUST_INCOMPATIBLE):
        return True
    # A backreference, which Rust has no meaning for.
    return any(raw[i] == "\\" and raw[i + 1 : i + 2].isdigit() for i in range(len(raw)))


#: Wall-clock bound on matching an entry's :mod:`re` form against one command.
#: Past it the child is killed and the verdict is unknown.
_FALLBACK_TIMEOUT_SECS = 2.0

#: The child that runs the :mod:`re` matches: reads ``{"patterns", "pieces"}`` as
#: JSON on stdin and prints the index of the first pattern that matches a piece,
#: or ``-1``.
_FALLBACK_WORKER = (
    "import json, re, sys\n"
    "d = json.load(sys.stdin)\n"
    "hit = -1\n"
    "for i, p in enumerate(d['patterns']):\n"
    "    c = re.compile(p)\n"
    "    if any(c.search(x) for x in d['pieces']):\n"
    "        hit = i\n"
    "        break\n"
    "print(hit)\n"
)


def _fallback_hit(patterns: list[str], pieces: list[str]) -> int | None:
    """The index of the first of *patterns* that matches one of *pieces*, ``-1``
    for none, or ``None`` when the child did not answer in time.

    shortcut: one child process per shell call that has such an entry (tens of
    milliseconds); a linear-time engine for the full syntax is the upgrade if
    that cost ever shows.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _FALLBACK_WORKER],
            input=json.dumps({"patterns": patterns, "pieces": pieces}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_FALLBACK_TIMEOUT_SECS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    try:
        hit = int(proc.stdout.strip())
    except ValueError:
        return None
    return hit if -1 <= hit < len(patterns) else None


def _python_pattern(raw: str) -> re.Pattern[str] | None:
    """*raw* compiled as kiro-cli anchors it, with Python's :mod:`re`, or ``None``.

    Searched, not fullmatched: ``\\A`` binds to the first top-level alternative
    and ``\\Z`` to the last, as in Rust's ``\\A{raw}\\z``.
    """
    if _reads_differently(raw):
        return None
    # Rust's end-of-text anchor ``\z`` is ``\Z`` in Python.
    body = raw.replace("\\z", "\\Z")
    # Leading inline flags (``(?i)``) stay in front of the anchor: Python refuses
    # global flags anywhere else; they still apply to all of it.
    flags = ""
    while body.startswith("(?") and ")" in body:
        head = body[2 : body.index(")")]
        if not head or not set(head) <= set("imsx"):
            break
        flags += head
        body = body[len(head) + 3 :]
    try:
        return re.compile(f"(?{flags})\\A{body}\\Z" if flags else f"\\A{body}\\Z")
    except (re.error, RecursionError, OverflowError, ValueError):
        return None


def _pieces(command: str) -> list[str]:
    """The whole *command* and every piece of it.

    Past :data:`_MAX_PIECES` pieces only the whole command is returned: that is
    exactly what kiro-cli matches (``\\A{entry}\\z`` against the command), so the
    check never reads less than kiro-cli does, it only skips Crew's extra
    per-piece reading.
    """
    current = [command]
    for brk in _BREAKS:
        current = [part for chunk in current for part in chunk.split(brk)]
        if len(current) > _MAX_PIECES:
            return [command]
    pieces = [command, *(p.strip(" \t") for p in current if p.strip(" \t"))]
    return list(dict.fromkeys(pieces))


def unreadable_entries(entries: Any) -> list[str]:
    """The entries of *entries* the gate can judge on no command, for the notice."""
    if not isinstance(entries, (list, tuple)):
        return [repr(entries)]
    bad: list[str] = []
    for raw in entries:
        if not isinstance(raw, str):
            bad.append(repr(raw))
        elif raw.strip() and (
            _reads_differently(raw)
            or (allowed_commands.parse(raw) is None and not _python_pattern(raw))
        ):
            bad.append(raw)
    return bad


def denylist_verdict(entries: Any, command: Any) -> tuple[str, str]:
    """How the spec's *entries* judge the shell *command*: ``(verdict, entry)``.

    ``("denied", entry)`` when an entry matches the whole command or a piece of
    it; ``("unknown", entry)`` when an entry, or the command, cannot be judged;
    ``("clear", "")`` when no entry is set, or every entry was matched against
    every piece and none matched.
    """
    if not isinstance(entries, (list, tuple)):
        return "unknown", repr(entries)
    if all(isinstance(raw, str) and not raw.strip() for raw in entries):
        return "clear", ""  # no entry to match, so nothing to refuse
    if not isinstance(command, str) or not command:
        return "unknown", "(no command)"
    pieces = _pieces(command)
    budget = [_MAX_MATCH_STEPS]
    unknown = ""
    fallback: list[tuple[str, str]] = []
    for raw in entries:
        if not isinstance(raw, str):
            unknown = unknown or repr(raw)
            continue
        if not raw.strip():
            continue
        if _reads_differently(raw):
            unknown = unknown or raw
            continue
        parsed = None if "|" in raw else allowed_commands.parse(raw)
        if parsed is not None:
            for piece in pieces:
                if _match(parsed, piece, budget):
                    return "denied", raw
                if budget[0] < 0:
                    unknown = unknown or raw
                    break
            continue
        compiled = _python_pattern(raw)
        if compiled is None:
            unknown = unknown or raw
            continue
        fallback.append((raw, compiled.pattern))
    if fallback:
        hit = _fallback_hit([pattern for _, pattern in fallback], pieces)
        if hit is None:
            return "unknown", fallback[0][0]
        if hit >= 0:
            return "denied", fallback[hit][0]
    if unknown:
        return "unknown", unknown
    return "clear", ""


#: Prefixes of the session-start notice keys for ``deniedCommands`` entries:
#: ``acp.kas_shell_denylist.denylist_notice_keys`` writes them and
#: ``dashboard.chat_turn.tool_approval._spec_keys_notice`` reads them.
DENY_ENFORCED_PREFIX = "deniedCommands-enforced:"
DENY_UNREADABLE_PREFIX = "deniedCommands-unreadable:"

#: The ``toolsSettings`` keys kiro-cli reads its shell settings under.
_SHELL_KEYS = ("execute_bash", "shell")


def spec_denied_commands(tools_settings: Any) -> list[Any] | None:
    """Every ``deniedCommands`` entry of both shell keys, as written, or ``None``.

    ``None`` when neither key sets the field. A value that is not a list is kept
    as one entry that is not a string, so the gate cannot read it and keeps the
    call for a person rather than drop it.
    """
    if not isinstance(tools_settings, dict):
        return None
    out: list[Any] = []
    found = False
    for key in _SHELL_KEYS:
        settings = tools_settings.get(key)
        if not isinstance(settings, dict) or "deniedCommands" not in settings:
            continue
        value = settings["deniedCommands"]
        if value is None:
            continue
        found = True
        out.extend(value if isinstance(value, list) else [{"not_a_list": value}])
    return out if found else None
