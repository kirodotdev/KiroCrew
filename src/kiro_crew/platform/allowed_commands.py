"""kiro-cli ``toolsSettings.shell.allowedCommands`` entries, read and matched safely.

kiro-cli compiles each entry with Rust's ``regex`` crate, anchored at both ends, and
runs a command one matches without a prompt. Crew answers the same grant at its own
gate on KAS, and must neither grant more than kiro-cli would nor let a pattern stall
the gateway. Python's ``re`` does neither safely: it reads some Rust syntax
differently (a POSIX class such as ``[[:alpha:]]``) and backtracks without bound
on nested repetition.

So only the subset whose meaning is the same in both engines, and which matches in
linear time, is accepted: literal characters, a backslash-escaped punctuation
character as a literal, and ``.*`` as "any run of characters". Optional ``^`` /
``\\A`` at the start and ``$`` / ``\\z`` at the end are read as the anchoring
kiro-cli adds anyway. Every other entry is refused (``None``) and its commands keep
prompting. A literal-plus-``.*`` pattern is the shape kiro-cli's own documentation
shows (``git status``, ``npm run .*``).

A LEAF on purpose: stdlib only, so both the KAS projection and the gate import it.
"""

from __future__ import annotations

#: Characters that make a regex more than a literal; ``.`` is accepted only as ``.*``.
_REGEX_METACHARACTERS = frozenset(".^$*+?()[]{}|\\")

#: Bound on one entry, so the record a session carries stays small.
MAX_PATTERN_LENGTH = 1024

#: Characters that make a command more than one plain command: chaining, pipes,
#: redirection, substitution, grouping and expansion. A command holding any of
#: them is never granted by an entry, however wide, so a ``git status .*`` grant
#: cannot carry ``git status; <anything>``.
UNGRANTABLE_COMMAND_CHARS = frozenset(";&|<>$`(){}\n\r")

#: Commands longer than this are not matched against an entry at all.
MAX_COMMAND_LENGTH = 4096


def parse(raw: object) -> tuple[str, ...] | None:
    """*raw* as its literal segments between ``.*`` wildcards, or ``None``.

    ``("git status",)`` is an exact command; ``("npm run ", "")`` is ``npm run .*``;
    ``("", "")`` is ``.*``. ``None`` for anything outside the accepted subset.
    """
    if not isinstance(raw, str):
        return None
    # Read verbatim, as kiro-cli reads it: no stripping, and no character a reader
    # could not see (control characters, any whitespace but the plain space).
    body = raw
    if not body or len(body) > MAX_PATTERN_LENGTH or not _plain_text(body):
        return None
    for prefix in ("\\A", "^"):
        if body.startswith(prefix):
            body = body[len(prefix) :]
            break
    for suffix in ("\\z", "$"):
        if body.endswith(suffix) and not body.endswith("\\" + suffix):
            body = body[: -len(suffix)]
            break
    segments: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if body.startswith(".*", i):
            segments.append("".join(current))
            current = []
            i += 2
        elif ch == "\\":
            nxt = body[i + 1] if i + 1 < len(body) else ""
            if not nxt or nxt.isalnum() or nxt.isspace():
                return None
            current.append(nxt)
            i += 2
        elif ch in _REGEX_METACHARACTERS:
            return None
        else:
            current.append(ch)
            i += 1
    segments.append("".join(current))
    if not any(segments) and len(segments) == 1:
        return None
    return tuple(segments)


def _plain_text(text: str) -> bool:
    """Every character printable, and the plain space the only whitespace."""
    return all(ch == " " or (ch.isprintable() and not ch.isspace()) for ch in text)


def plain_command(command: object) -> bool:
    """Whether *command*, exactly as the host recovered it, may be granted at all.

    Not empty, at most :data:`MAX_COMMAND_LENGTH`, none of
    :data:`UNGRANTABLE_COMMAND_CHARS`, and no control character or whitespace but
    the plain space: a non-breaking space or a tab names a different path to the
    shell while looking the same, so a grant for ``rm scratch`` never covers
    ``rm scratch`` plus an invisible character. Never stripped or rewritten: the
    command matched is the command that runs.
    """
    return (
        isinstance(command, str)
        and bool(command)
        and len(command) <= MAX_COMMAND_LENGTH
        and not UNGRANTABLE_COMMAND_CHARS.intersection(command)
        and _plain_text(command)
    )


def matches(segments: tuple[str, ...], command: str) -> bool:
    """Whether *command* is the whole of what *segments* describe.

    Linear in the command's length times the segment count: the first segment is a
    prefix, the last a suffix, and each middle one is found left to right after the
    previous, which is exactly what a greedy ``.*`` between literals accepts.
    """
    if len(segments) == 1:
        return command == segments[0]
    first, *middle, last = segments
    if not command.startswith(first) or len(command) < len(first) + len(last):
        return False
    if not command.endswith(last):
        return False
    pos = len(first)
    end = len(command) - len(last)
    for seg in middle:
        found = command.find(seg, pos, end)
        if found < 0:
            return False
        pos = found + len(seg)
    return True
