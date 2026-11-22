"""Find hand-spelled forwarding calls that drop a keyword both sides accept.

A function that re-calls itself or a sibling with its own parameters spelled out
by hand (``load_session(self, ..., channel_id=channel_id, require_replay=...)``)
has to be edited twice when the callee grows a keyword: once in the signature and
once at the call. Two pull requests that are each green on the same base break
this silently -- one adds the re-call, the other adds the keyword -- and the
merged tree drops the new keyword on that path.

A **forwarding call** is a call inside function *A* that passes at least
:data:`MIN_FORWARDED` of *A*'s own parameters by the same name (``x=x``) and binds
at least :data:`MIN_BOUND_SHARE` of the callee *B*'s parameters. It **drops** a
keyword when *B* accepts a parameter that *A* also accepts and the call does not
pass it.

The callee is resolved by name: ``foo(...)`` and ``obj.foo(...)`` both look up
``def foo`` across the tree, preferring the caller's own class, then its own
module. A name whose remaining definitions disagree is skipped rather than
guessed. A callee that only takes ``**kwargs`` and hands them on with
``other(**kwargs, ...)`` is seen through to ``other`` (``create_session`` ->
``_create_session_with_harness_state`` -> ``create_session``).

Only module-level functions and methods of module-level classes take part; a
nested def is neither caller nor callee.

Cost. Parsing every file is ~11 s and parsing every file with three ``x=x`` in it
still ~8 s, so the scan parses single DEFS, not files. Each module-level def and
method is cut out of the text by indentation (:func:`_segments`) and parsed alone
when it can matter: as a caller when its own lines hold :data:`MIN_FORWARDED`
``x=x``, as a callee when a caller names it and its parameter list holds every
keyword the call passes (or a ``**``). A segment that does not parse alone (a
column-0 line inside a triple-quoted string ends it early) falls back to parsing
its whole file, so the cut never hides a def. No tree is kept.

Kept free of pytest so the scan can be pointed at any tree (the PR proving it
runs it on a checkout of the code before a fix).
"""

from __future__ import annotations

import ast
import bisect
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

#: Same-name keywords (``x=x``) a call must carry before it counts as forwarding.
MIN_FORWARDED = 3
#: Share of the callee's parameters the call must bind to count as forwarding.
MIN_BOUND_SHARE = 0.5
#: How many ``**kwargs`` hops the resolver follows.
_MAX_HOPS = 3
_RECEIVERS = frozenset({"self", "cls"})
_KEYWORD_VALUE = re.compile(r"=(\w+)")
# The optional ``[...]`` is a PEP 695 type-parameter list (``def f[T](...)``).
_DEF_LINE = re.compile(r"\n( {0,4})(?:async[ \t]+)?def[ \t]+(\w+)[ \t]*(?:\[[^\]\n]*\])?[ \t]*\(")
_DEF_LINE_NAME = re.compile(r"def[ \t]+(\w+)[ \t]*(?:\[[^\]\n]*\])?[ \t]*\(")
#: Starts of column-0 code lines: not blank, not a comment, not a closing bracket.
_COL0_CODE = re.compile(r"\n(?=[^\s#)\]}])")
#: Lines that end a cut def: a def, class or decorator no deeper than it.
_COL0_END = re.compile(r"\n(?:@|class[ \t]|(?:async[ \t]+)?def[ \t])")
_UPTO4_END = re.compile(r"\n {0,4}(?:@|class[ \t]|(?:async[ \t]+)?def[ \t])")
#: Column-0 heads a four-space def can sit under, and how to read one.
_COL0_HEADS = ("\nclass ", "\ndef ", "\nasync def ")
_HEAD_LINE = re.compile(r"class[ \t]+(\w+)|(?:async[ \t]+)?def[ \t]")

Sources = Callable[[], Iterable[tuple[str, str]]]


@dataclass(frozen=True)
class _CallSite:
    name: str
    via_attr: bool
    on_receiver: bool
    n_positional: int
    keywords: tuple[str, ...]
    #: the keywords written ``x=x`` with ``x`` a parameter of the enclosing def
    forwarded: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class _Def:
    rel: str
    cls: str | None
    name: str
    positional: tuple[str, ...]
    keyword_only: tuple[str, ...]
    varkw: str | None
    #: calls that hand ``**<varkw>`` on, for :meth:`_Index.effective_params`
    hand_ons: tuple[_CallSite, ...] = ()

    @property
    def params(self) -> tuple[str, ...]:
        return self.positional + self.keyword_only

    @property
    def bound_receiver(self) -> bool:
        return bool(self.positional) and self.positional[0] in _RECEIVERS

    @property
    def qualname(self) -> str:
        return f"{self.cls}.{self.name}" if self.cls else self.name


@dataclass(frozen=True)
class Drop:
    """One keyword a forwarding call leaves out."""

    rel: str
    caller: str
    callee: str
    param: str
    line: int

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.rel, self.caller, self.callee, self.param)


#: :attr:`_Segment.cls` for a four-space def whose class the cut cannot name.
_UNKNOWN_CLASS = "?"


@dataclass(frozen=True)
class _Segment:
    #: ``None`` at module level, :data:`_UNKNOWN_CLASS` when the cut cannot tell
    cls: str | None
    name: str
    #: character span of the def, from its ``def`` line (decorators excluded)
    start: int
    end: int
    #: 0-based line number of the ``def`` line
    line: int
    #: where the parameter list opens, for :func:`_header`
    params_at: int


def _same_name_spots(text: str) -> list[int]:
    """Where *text* spells ``x=x``, as the offsets of the ``=``.

    Every forwarding call spells it :data:`MIN_FORWARDED` times. A regex with a
    back-reference costs ~3 s over the tree; matching ``=word`` and comparing the
    text before it costs ~0.1 s.
    """
    spots = []
    for match in _KEYWORD_VALUE.finditer(text):
        word, at = match.group(1), match.start()
        start = at - len(word)
        if (
            start > 0
            and text.startswith(word, start)
            and not (text[start - 1].isalnum() or text[start - 1] == "_")
        ):
            spots.append(at)
    return spots


def _header(text: str, start: int) -> str:
    """The parameter list opening just before *start*, by paren depth.

    A paren inside a string default or a comment can end it early, and a shorter
    header would hide keywords. :func:`_may_bind` therefore keeps any def whose
    header holds a quote or a ``#``.
    """
    depth = 1
    for at in range(start, len(text)):
        char = text[at]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:at]
    return text[start:]


def _may_bind(header: str, sites: list[tuple[str, ...]]) -> bool:
    """Whether a def with *header* can be the callee of one of *sites*.

    A callee binds every keyword a site passes unless it takes ``**kwargs``. A
    header holding a quote or ``#`` may have been cut short (:func:`_header`), so
    it is kept.
    """
    if "**" in header or any(mark in header for mark in "'\"#"):
        return True
    return any(all(kw in header for kw in kws) for kws in sites)


def _segments(text: str, names: frozenset[str] | None) -> Iterator[_Segment]:
    """Module-level defs and methods of module-level classes, cut out of *text*.

    *text* starts with ``"\\n"`` (:func:`_summaries` adds it) so every line start is
    a newline, which the patterns below match at C speed. *names* limits the defs
    cut (``None``: all).

    A def runs from its ``def`` line to the next ``def``, ``class`` or decorator
    line indented no deeper than it. That can run past the def (module code after
    it joins the cut, and parses), but it cannot stop inside the def's own body
    unless such a line sits at that depth inside a string: the cut is then not
    valid Python and the caller reads the file whole.

    A def at four spaces is a method when the nearest ``class`` or ``def`` line at
    column 0 above it is a ``class`` with no other column-0 code between; under a
    ``def`` it is nested and skipped. Anything else (an ``if``, or a column-0 line
    of a string) makes the class :data:`_UNKNOWN_CLASS`, and the caller reads the
    file whole.
    """
    line, counted = 0, 0
    for match in _DEF_LINE.finditer(text):
        name = match.group(2)
        if names is not None and name not in names:
            continue
        start = match.start() + 1
        line += text.count("\n", counted, start)
        counted = start
        indent = len(match.group(1))
        cls = None
        if indent:
            head = max(text.rfind(word, 0, start) for word in _COL0_HEADS)
            found = _HEAD_LINE.match(text, head + 1) if head >= 0 else None
            if found is None or _COL0_CODE.search(text, head + 1, start - 1):
                cls = _UNKNOWN_CLASS
            elif found.group(1) is None:
                continue  # nested in a module-level def
            else:
                cls = found.group(1)
        nxt = (_UPTO4_END if indent else _COL0_END).search(text, match.end())
        end = nxt.start() + 1 if nxt else len(text)
        yield _Segment(cls, name, start, end, line - 1, match.end())


def _calls(body: list[ast.stmt]) -> Iterable[ast.Call]:
    """Calls in *body*, not entering nested functions, lambdas or classes."""
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _site(call: ast.Call, own: frozenset[str], offset: int) -> _CallSite | None:
    func = call.func
    if isinstance(func, ast.Name):
        name, via_attr, on_receiver = func.id, False, False
    elif isinstance(func, ast.Attribute):
        name, via_attr = func.attr, True
        on_receiver = isinstance(func.value, ast.Name) and func.value.id in _RECEIVERS
    else:
        return None
    return _CallSite(
        name=name,
        via_attr=via_attr,
        on_receiver=on_receiver,
        n_positional=len(call.args),
        keywords=tuple(kw.arg for kw in call.keywords if kw.arg),
        forwarded=tuple(
            kw.arg
            for kw in call.keywords
            if kw.arg in own and isinstance(kw.value, ast.Name) and kw.value.id == kw.arg
        ),
        line=call.lineno + offset,
    )


def _summary(
    rel: str,
    cls: str | None,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    offset: int,
    *,
    as_caller: bool,
) -> tuple[_Def, list[_CallSite]]:
    """*node* as a callee, and its forwarding call sites when *as_caller*."""
    a = node.args
    positional = tuple(p.arg for p in (*a.posonlyargs, *a.args))
    keyword_only = tuple(p.arg for p in a.kwonlyargs)
    varkw = a.kwarg.arg if a.kwarg else None
    own = frozenset(positional + keyword_only) - _RECEIVERS
    as_caller = as_caller and len(own) >= MIN_FORWARDED
    sites: list[_CallSite] = []
    hand_ons: list[_CallSite] = []
    for call in _calls(node.body) if (varkw or as_caller) else ():
        splats = [kw.value for kw in call.keywords if kw.arg is None]
        if varkw and any(isinstance(v, ast.Name) and v.id == varkw for v in splats):
            site = _site(call, own, offset)
            if site:
                hand_ons.append(site)
        if not as_caller or splats or any(isinstance(arg, ast.Starred) for arg in call.args):
            continue  # a splat may carry anything; nothing is spelled by hand
        site = _site(call, own, offset)
        if site and len(site.forwarded) >= MIN_FORWARDED:
            sites.append(site)
    return _Def(rel, cls, node.name, positional, keyword_only, varkw, tuple(hand_ons)), sites


def _whole_file(
    rel: str, text: str
) -> Iterator[tuple[str | None, ast.FunctionDef | ast.AsyncFunctionDef]]:
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError:
        return
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield None, node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield node.name, item


def _parse_segment(
    rel: str, seg: _Segment, text: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    body = text[seg.start : seg.end]
    wrapped = f"class _:\n{body}" if seg.cls else body
    try:
        tree = ast.parse(wrapped, filename=rel)
    except SyntaxError:
        return None
    if not tree.body:
        return None
    node = tree.body[0].body[0] if seg.cls else tree.body[0]  # type: ignore[attr-defined]
    return node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def _summaries(
    rel: str,
    text: str,
    pick: Callable[[_Segment, str, str], bool],
    *,
    as_caller: bool,
    names: frozenset[str] | None = None,
) -> Iterator[tuple[_Def, list[_CallSite]]]:
    """The defs of *text* that *pick* keeps, parsed one segment at a time."""
    padded = "\n" + text
    picked = [
        seg for seg in _segments(padded, names) if pick(seg, padded, padded[seg.start : seg.end])
    ]
    if not picked:
        return
    nodes = []
    for seg in picked:
        node = None if seg.cls == _UNKNOWN_CLASS else _parse_segment(rel, seg, padded)
        if node is None or node.name != seg.name:
            # The cut cannot stand for this def: read the whole file once instead.
            # Every module-level def and method of a picked name is summarised, so
            # one wrong cut costs a parse, never a def.
            wanted = {s.name for s in picked}
            for cls, whole in _whole_file(rel, text):
                if whole.name in wanted:
                    yield _summary(rel, cls, whole, 0, as_caller=as_caller)
            return
        offset = seg.line - (0 if seg.cls is None else 1)
        nodes.append((seg.cls, node, offset))
    for cls, node, offset in nodes:
        yield _summary(rel, cls, node, offset, as_caller=as_caller)


class _Index:
    def __init__(self) -> None:
        self.by_name: dict[str, list[_Def]] = defaultdict(list)

    def resolve(self, caller: _Def, site: _CallSite) -> _Def | None:
        passed = set(site.keywords)
        found = [
            d
            for d in self.by_name.get(site.name, ())
            if (d.varkw or passed <= set(d.params))
            and (not site.via_attr or d.bound_receiver or d.cls)
        ]
        for narrow in (
            lambda d: site.on_receiver
            and d.cls is not None
            and (d.cls, d.rel) == (caller.cls, caller.rel),
            lambda d: d.rel == caller.rel,
        ):
            near = [d for d in found if narrow(d)]
            if near:
                found = near
                break
        if len({(d.params, d.varkw) for d in found}) != 1:
            return None
        return found[0]

    def effective_params(self, callee: _Def, hops: int = 0) -> tuple[str, ...]:
        """*callee*'s parameters, seen through a ``**kwargs`` hand-on."""
        params = callee.params
        if callee.varkw is None or hops >= _MAX_HOPS:
            return params
        for site in callee.hand_ons:
            onward = self.resolve(callee, site)
            if onward is None:
                continue
            rest = self.effective_params(onward, hops + 1)
            if site.via_attr and onward.bound_receiver:
                rest = rest[1:]
            return params + tuple(p for p in rest if p not in site.keywords and p not in params)
        return params


def _drops(index: _Index, caller: _Def, sites: list[_CallSite]) -> Iterable[Drop]:
    own = set(caller.params) - _RECEIVERS
    for site in sites:
        callee = index.resolve(caller, site)
        if callee is None:
            continue
        params = index.effective_params(callee)
        if site.via_attr and callee.bound_receiver:
            params = params[1:]
        passed = set(params[: site.n_positional]) | set(site.keywords)
        bindable = [p for p in params if p not in _RECEIVERS]
        if not bindable or len(passed - _RECEIVERS) < MIN_BOUND_SHARE * len(bindable):
            continue
        for param in bindable:
            if param in own and param not in passed:
                yield Drop(caller.rel, caller.qualname, callee.name, param, site.line)


def scan(sources: Sources) -> list[Drop]:
    """Every dropped keyword in the tree *sources* streams as ``(relative path, text)``.

    *sources* is called once per pass, so it must stream afresh each time.
    """
    callers: list[tuple[_Def, list[_CallSite]]] = []
    defined: dict[str, frozenset[str]] = {}
    for rel, text in sources():
        defined[rel] = frozenset(_DEF_LINE_NAME.findall(text))
        spots = _same_name_spots(text)
        if len(spots) < MIN_FORWARDED:
            continue

        def holds_three(seg: _Segment, _text: str, _body: str, spots: list[int] = spots) -> bool:
            # ``seg`` is in the padded text, one character ahead of ``spots``.
            inside = bisect.bisect_left(spots, seg.end - 1) - bisect.bisect_left(
                spots, seg.start - 1
            )
            return inside >= MIN_FORWARDED

        callers.extend((d, s) for d, s in _summaries(rel, text, holds_three, as_caller=True) if s)

    # A callee binds every keyword a site passes unless it takes ``**kwargs``.
    need: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for _, sites in callers:
        for site in sites:
            need[site.name].append(site.keywords)
    index = _Index()
    seen: set[str] = set()
    for _ in range(_MAX_HOPS + 1):
        if not need:
            break
        wanted = need

        def pick(seg: _Segment, text: str, _body: str) -> bool:
            kws = wanted.get(seg.name)
            if kws is None:
                return False
            return _may_bind(_header(text, seg.params_at), kws)

        found: list[_Def] = []
        names = frozenset(wanted)
        for rel, text in sources():
            if defined.get(rel, names) & names:
                found.extend(
                    d for d, _ in _summaries(rel, text, pick, as_caller=False, names=names)
                )
        for d in found:
            index.by_name[d.name].append(d)
        seen |= need.keys()
        need = defaultdict(list)
        for d in found:
            for site in d.hand_ons:
                if site.name not in seen:
                    need[site.name].append(())

    drops = [drop for caller, sites in callers for drop in _drops(index, caller, sites)]
    return sorted(drops, key=lambda d: (d.rel, d.line, d.param))
