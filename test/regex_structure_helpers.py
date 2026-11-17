"""Read a regex's backtracking risk off its parse, for tests that must not time a search.

A regex search is one C call, so neither a clock nor an interpreter-instruction count
(``_interpreter_work``) sees its backtracking. These helpers read the pattern's structure
through the stdlib parser instead, so a complexity guard on a pattern fails on the shape
that backtracks, before any hostile input runs (testing-conventions § Determinism 5).

They rely on the private ``re._parser`` / ``re._compiler`` (``sre_parse`` / ``sre_compile``
before Python 3.11), which may change between Python versions.
"""

from __future__ import annotations

import re

try:
    from re import _compiler, _constants, _parser
except ImportError:  # Python 3.10
    import sre_compile as _compiler  # type: ignore[no-redef]
    import sre_constants as _constants  # type: ignore[no-redef]
    import sre_parse as _parser  # type: ignore[no-redef]

_REPEATS = (_constants.MAX_REPEAT, _constants.MIN_REPEAT, _constants.POSSESSIVE_REPEAT)


def _subpatterns(av: object):
    for sub in av if isinstance(av, (list, tuple)) else ():
        for part in sub if isinstance(sub, (list, tuple)) else (sub,):
            if isinstance(part, _parser.SubPattern):
                yield part


def unbounded_repeat_bodies(pattern: "str | _parser.SubPattern") -> list:
    """Every ``*``/``+``/``{n,}`` body in *pattern* (a string or a parsed sub-pattern)."""
    parsed = _parser.parse(pattern) if isinstance(pattern, str) else pattern
    found: list = []

    def walk(node) -> None:
        for op, av in node:
            if op in _REPEATS and av[1] == _constants.MAXREPEAT:
                found.append(av[2])
            for sub in _subpatterns(av):
                walk(sub)

    walk(parsed)
    return found


def run_cost_unproven(pattern: re.Pattern[str], unit: str) -> str | None:
    """Why *pattern* might not scan a long run of *unit* in linear time, or None.

    A run is scanned linearly when no unbounded repeat can consume it, or when a
    match cannot start inside the run and no two repeats that consume it are
    separated only by items that also match it (``\\s*␠\\s*``: the run can be split
    between them in n ways, at each of n positions). Anything outside that model (a
    consuming repeat inside a group) is reported as unproven.
    """
    tree = _parser.parse(pattern.pattern, pattern.flags)
    chars = set(unit)

    def matcher(items):
        return _compiler.compile(_parser.SubPattern(tree.state, list(items)), pattern.flags)

    def matches_some(items) -> bool:
        m = matcher(items)
        return any(m.fullmatch(c) for c in chars)

    def eats(item) -> bool:
        op, av = item
        if op not in _REPEATS or av[1] != _constants.MAXREPEAT:
            return False
        body = matcher(av[2])
        return all(body.fullmatch(c) for c in chars)

    def nested(items):
        for _op, av in items:
            for part in _subpatterns(av):
                yield from part
                yield from nested(part)

    top = list(tree)
    if any(eats(item) for item in nested(top)):
        return f"a run-consuming repeat sits inside a group: {pattern.pattern!r}"
    eating = [i for i, item in enumerate(top) if eats(item)]
    if not eating:
        return None
    if matches_some(top[:1]):
        return f"a match can start at every character of the run: {pattern.pattern!r}"
    for i, j in zip(eating, eating[1:]):
        if all(matches_some([it]) or matcher([it]).fullmatch("") for it in top[i + 1 : j]):
            return f"two repeats can split one run between them: {pattern.pattern!r}"
    return None
