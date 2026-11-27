"""Subshell-scope body extraction for the credential-mint reader.

A ``( ... )`` subshell is read in a child shell, so an assignment and the use it feeds
inside the SAME scope (``( T=$(...); kirocrew $T )``) pair there and nowhere else. This
module finds each such scope's body in one pass; the payload walk reads each body as its
own frame.

A scope nested in another is ELIDED from the enclosing body (it is emitted as its own
frame) so the total body text stays linear in the input. A use of an OUTER-scope variable
(``( T=$(...); ( kirocrew $T ) )``) still resolves because each emitted body also reports
the enclosing-scope assignments its own text references (transitive over aliases); the walk
resolves those into the frame's seed bindings. Only the referenced assignments are reported,
so the per-frame cost is the body's own references, not the ancestor count.

Split out of ``shell_normalizer_walk`` to keep that module under its line cap; it reads
``shell_normalizer_walk`` attributes through the ``_walk`` alias so a test that patches
``_redir._SUBST_ASSIGN_RE`` still measures the scan.
"""

from __future__ import annotations

import re

from . import shell_normalizer_redirects as _redir
from . import shell_normalizer_walk as _walk

# An assignment at a command start, any value kind (literal, alias, substitution): it carries
# an enclosing scope's binding into an elided inner scope that references it.
_ANY_ASSIGN_RE = re.compile(r"(?:(?<=[(;&|\n])|\A)[ \t]*([A-Za-z_]\w*)=")
_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_]\w*)")


def _assign_value_end(
    command: str, eq: int, quote_at: "dict[int, tuple[int, bool]] | None" = None
) -> int:
    """End offset of the one-word value of an assignment whose ``=`` is at *eq*, stepping
    over ``$(...)`` / backtick / quoted spans so a separator inside them does not end it.

    *quote_at* is :func:`_walk._quote_states` of *command*: each ``$(`` close walk then
    starts at its own opener instead of re-reading the command from offset 0."""
    j, n = eq + 1, len(command)
    while j < n:
        c = command[j]
        if c in " \t\n;&|)":
            break
        if c == "'":
            k = command.find("'", j + 1)
            j = k + 1 if k >= 0 else n
        elif c == '"':
            k = j + 1
            while k < n and command[k] != '"':
                k += 2 if command[k] == "\\" else 1
            j = k + 1
        elif c == "`":
            k = command.find("`", j + 1)
            j = k + 1 if k >= 0 else n
        elif command[j : j + 2] == "$(":
            state = quote_at.get(j + 2) if quote_at is not None else None
            j, _ = _walk._matching_close_paren(command, j + 2, state)
        else:
            j += 1
    return j


_ABSENT = object()


def _subshell_bodies(command: str) -> "list[tuple[str, int, list[str]]]":
    """For each ``( ... )`` subshell at a command start with a substitution-valued assignment
    at ITS OWN level: ``(body, raw_len, carried)``. ``body`` is the scope's own source with
    nested own-assignment scopes elided to ``:`` (each emitted separately); ``raw_len`` is the
    scope's raw source span (the walk's re-descent bound); ``carried`` is the enclosing-scope
    assignment texts this body references, transitively over aliases, which the walk resolves
    into the frame's seed bindings. One pass over assignment offsets.

    ``acc`` tracks every assignment of a still-open enclosing scope (and the top level) by
    reference, so building ``carried`` costs the body's references, not the ancestor count. A
    real subshell's own assignments are undone on its ``)`` (they do not leak to a sibling or
    the parent); an assignment reached after a scope closes is simply not in ``acc`` yet."""
    bodies: "list[tuple[str, int, list[str]]]" = []
    marks = {m.start(): m for m in _redir._SUBST_ASSIGN_RE.finditer(command)}
    if "(" not in command or not marks:
        return bodies
    quote_at = _walk._quote_states(command)
    assign_at = {
        m.start(1): (m.group(1), _assign_value_end(command, m.end() - 1, quote_at))
        for m in _ANY_ASSIGN_RE.finditer(command)
    }
    acc: "dict[str, str]" = {}
    # Each real-subshell entry is ``[start, own, elide, undo, own_vars]`` (``None`` for any
    # other paren): ``elide`` collects nested own-assignment scope spans to cut from the body,
    # ``undo`` restores ``acc`` on close, ``own_vars`` excludes this scope's own names.
    stack: "list[list | None]" = []
    for step in _walk._iter_shell_chars(command):
        at = step.offset
        if not step.active:
            continue
        if at in assign_at:
            var, end = assign_at[at]
            if stack and stack[-1] is not None:
                stack[-1][3].append((var, acc.get(var, _ABSENT)))
                stack[-1][4].add(var)
            acc[var] = command[at:end]
        if at in marks and stack and stack[-1] is not None:
            stack[-1][1] = True
        if step.char == "(":
            sub = (
                not (at and command[at - 1] in "$<>(")
                and command[at + 1 : at + 2] != "("
                and _redir._at_command_start(command, at)
            )
            stack.append([at, False, [], [], set()] if sub else None)
        elif step.char == ")" and stack:
            frame = stack.pop()
            if frame is None:
                continue
            start, own, elide, undo, own_vars = frame
            if own:
                body = _elide(command, start + 1, at, elide)
                bodies.append((body, at - (start + 1), _carried(body, acc, own_vars)))
                if stack and stack[-1] is not None:
                    stack[-1][2].append((start, at + 1))  # parent cuts this whole scope
            elif stack and stack[-1] is not None:
                stack[-1][2].extend(elide)  # a non-emitted scope bubbles its cuts up
            for var, prior in reversed(undo):  # drop this scope's own binds from acc
                acc.pop(var, None) if prior is _ABSENT else acc.__setitem__(var, prior)
    return bodies


def _elide(command: str, lo: int, hi: int, spans: "list[tuple[int, int]]") -> str:
    """``command[lo:hi]`` with each nested own-assignment scope span replaced by a null
    command (it is read as its own frame), so this body's text is its own level only."""
    parts: "list[str]" = []
    pos = lo
    for s, e in sorted(spans):
        parts.append(command[pos:s])
        parts.append(" : ")
        pos = e
    parts.append(command[pos:hi])
    return "".join(parts)


def _carried(body: str, acc: "dict[str, str]", own_vars: "set[str]") -> "list[str]":
    """The enclosing-scope assignment texts *body* references (transitively over aliases),
    from *acc*. A name assigned at this scope's own level (*own_vars*) is in *body* already, so
    it is not carried; the cost is the body's references, not the size of *acc*."""
    closure: "set[str]" = set()
    queue = [m.group(1) for m in _VAR_REF_RE.finditer(body)]
    while queue:
        var = queue.pop()
        if var in closure or var in own_vars or var not in acc:
            continue
        closure.add(var)
        queue += [m.group(1) for m in _VAR_REF_RE.finditer(acc[var])]
    # ``acc`` is insertion-ordered, i.e. definition order, so an alias follows its referent.
    return [text for var, text in acc.items() if var in closure]
