"""A ``deniedCommands`` entry with alternation, as the regexes it is the union of.

KAS has no alternation in a shell glob. kiro-cli's own upgrade of a legacy agent
file splits such an entry into one glob per branch: ``(touch|mkdir) .*`` becomes
``touch *, mkdir *`` and ``rm .*|shred .*`` becomes ``rm *, shred *``. The KAS
projection does the same here, so the deny travels instead of being dropped.

Two constructs are split, and nothing else:

- a top-level ``|``, outside every group and class, into its branches (an empty
  one is dropped: it matches only an empty command line);
- inside a branch, a ``( )`` or ``(?: )`` group holding no other group, no class
  and no repeat after it, into one regex per combination of group branches; a
  group whose whole body is one such group (``((a|b))``) reads as that group.

Each branch keeps its text as written, a space at either end included: kiro-cli
turns ``rm .* | shred .*`` into ``rm * `` and `` shred *``, which refuse neither
``rm x`` nor ``shred x``, as the entry's own regex does not.

Every other entry (a class anywhere, any other nested group, a repeated or flagged group,
unbalanced parentheses, more than :data:`MAX_ALTERNATIVES` results) comes back
whole, or keeps the branch it cannot split whole, so the caller treats it exactly
as before: a branch with no glob is not carried.

A LEAF on purpose: stdlib only, so ``kas_permissions`` imports it.
"""

from __future__ import annotations

#: Most regexes one entry is split into, like ``kas_permissions._MAX_PATTERNS_PER_RULE``.
#: Past it the entry comes back whole and is reported as before.
MAX_ALTERNATIVES = 64

#: What may follow a group and repeat it; such a group is not split.
_REPEATS = frozenset("*+?{")


def top_level_branches(pattern: str) -> list[str]:
    """*pattern*'s branches split at each ``|`` outside every group, in order.

    ``[pattern]`` when it has no such ``|``, holds a class (a ``|`` inside one is a
    literal this scan does not read), or its parentheses do not balance."""
    branches = [""]
    depth = 0
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            branches[-1] += pattern[i : i + 2]
            i += 2
            continue
        if ch == "[":
            return [pattern]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return [pattern]
        elif ch == "|" and depth == 0:
            branches.append("")
            i += 1
            continue
        branches[-1] += ch
        i += 1
    if depth != 0 or len(branches) == 1:
        return [pattern]
    return branches


def deny_alternatives(pattern: str) -> list[str]:
    """The regexes *pattern* is the union of (module docstring), in order.

    ``[pattern]`` when it has nothing the split reads exactly."""
    branches = top_level_branches(pattern)
    if len(branches) == 1:
        return _group_alternatives(pattern)
    out: list[str] = []
    for branch in branches:
        out.extend(_group_alternatives(branch))
        if len(out) > MAX_ALTERNATIVES:
            return [pattern]
    # An empty branch matches only an empty command line, which never runs.
    return [alt for alt in dict.fromkeys(out) if alt]


def _group_alternatives(pattern: str) -> list[str]:
    """The regexes *pattern* is the union of, one per combination of group branches.

    ``[pattern]`` when it has no group the split reads exactly."""
    pieces: list[list[str]] = [[""]]
    i = 0
    split = False
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            pieces[-1] = [p + pattern[i : i + 2] for p in pieces[-1]]
            i += 2
        elif ch in "[|)":
            return [pattern]
        elif ch == "(":
            start = _group_body(pattern, i)
            if start is None:
                return [pattern]
            branches, end = _group_branches(pattern, start)
            # A group whose whole body is one group, ``((a|b))``: the inner one's
            # branches, read up to the outer group's ``)``.
            outer: list[int] = []
            while branches is None and end < len(pattern) and pattern[end] == "(":
                inner = _group_body(pattern, end)
                if inner is None or end != (outer[-1] if outer else start):
                    break
                outer.append(inner)
                branches, end = _group_branches(pattern, inner)
            for _ in outer:
                if branches is None or pattern[end + 1 : end + 2] != ")":
                    branches = None
                    break
                end += 1
            if branches is None or pattern[end + 1 : end + 2] in _REPEATS:
                return [pattern]
            pieces.append(branches)
            pieces.append([""])
            split = True
            i = end + 1
        else:
            pieces[-1] = [p + ch for p in pieces[-1]]
            i += 1
    if not split:
        return [pattern]
    out = [""]
    for choices in pieces:
        out = [head + tail for head in out for tail in choices]
        if len(out) > MAX_ALTERNATIVES:
            return [pattern]
    return list(dict.fromkeys(out))


def _group_body(pattern: str, i: int) -> int | None:
    """Where the body of the group opening at *i* starts: after ``(`` or ``(?:``;
    ``None`` for any other ``(?`` group (a flag or a look-around)."""
    if pattern.startswith("(?:", i):
        return i + 3
    return None if pattern.startswith("(?", i) else i + 1


def _group_branches(pattern: str, start: int) -> tuple[list[str] | None, int]:
    """The branches of the group whose body starts at *start*, and the index of its
    ``)``; ``None`` for a group holding a group or a class, or one never closed."""
    branches = [""]
    i = start
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            branches[-1] += pattern[i : i + 2]
            i += 2
        elif ch in "([":
            return None, i
        elif ch == "|":
            branches.append("")
            i += 1
        elif ch == ")":
            return branches, i
        else:
            branches[-1] += ch
            i += 1
    return None, i
