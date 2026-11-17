"""The K8 duration-upper-bound detector of ``test_flake_pattern_ratchet.py``.

An upper bound on a span read off the REAL clock fails whenever the host is slow
(testing-conventions rule D7). K8 counts every form of one: an ``assert`` comparison
with the span below its limit, literal or not (``elapsed < budget``), the reversed
form (``BUDGET > spent``; ``stamp > time.monotonic() - 5`` bounds an age), and
unittest's ``assertLess`` / ``assertLessEqual`` with the span first or
``assertGreater`` / ``assertGreaterEqual`` with it second. An event loop's ``time()``
is the real clock. It does not count a span off a clock the test drives
(``clock.monotonic() - t0`` where ``clock`` is neither the stdlib ``time`` module nor
an event loop where that function reads it), ordering two stamps (``x > t0``, or a
cutoff stored before the work: ``t0 = time.monotonic() - 5.0``), a remaining-time
check (``deadline - time.monotonic() <= cap``: it only shrinks on a slow host), or a
lower bound. The detector takes the ratchet's parsed module (``tree``, ``asserts``,
``calls``, ``stdlib_of``, ``enclosing_function``); the ratchet owns counting and runs
the cases kept here.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable

#: A CPU clock is a clock too: coverage's tracer multiplies the CPU every traced line
#: costs, so a ``thread_time`` budget reads the shard's instrumentation, not the code.
_CLOCK_CALLS = frozenset(
    {
        "monotonic",
        "perf_counter",
        "time",
        "monotonic_ns",
        "perf_counter_ns",
        "thread_time",
        "thread_time_ns",
        "process_time",
        "process_time_ns",
    }
)
_DELTA_NAMES = re.compile(r"(elapsed|duration|took|delta|wall)", re.IGNORECASE)
_STAMP_NAMES = re.compile(
    r"^(start|started|end|ended|before|after|began|finished|now|then|t\d|t_\w+|\w+_at)$"
)
#: unittest asserts -> the argument the span sits in for it to be an upper bound.
_UNITTEST_UPPER = {
    "assertLess": 0,
    "assertLessEqual": 0,
    "assertGreater": 1,
    "assertGreaterEqual": 1,
}
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
#: An event loop's ``time()`` reads ``time.monotonic``. A loop is a getter's result
#: (``asyncio.get_running_loop().time()``), a name or attribute the file binds from
#: one, or one of these names (``loop``, ``self._loop``, pytest-asyncio's
#: ``event_loop``). A name merely ending in ``_loop`` (``virtual_loop``) is not one.
_LOOP_NAMES = frozenset({"loop", "_loop", "event_loop"})
_LOOP_GETTERS = frozenset({"get_running_loop", "get_event_loop", "new_event_loop"})


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _getter(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _dotted(node.func).rsplit(".", 1)[-1] in _LOOP_GETTERS


def _scope_nodes(scope: ast.AST) -> Iterable[ast.AST]:
    """The nodes of *scope*, not of the functions and classes nested in it."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (*_FUNCS, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


class _Clocks:
    """Which calls in one file read the real clock.

    A name's binding is looked up the way Python does: the enclosing function, the
    functions around it, then the module. So ``clock = time`` or ``from time import
    monotonic as now`` in one test does not reach a ``clock`` fixture in another."""

    def __init__(self, module) -> None:
        self.module = module
        #: scope -> name -> its bindings: a marker (``"time"`` the module, ``"bare"`` a
        #: clock function, ``"param"``, ``"other"``) or the value assigned.
        self.scopes: dict[ast.AST, dict[str, list]] = {}
        #: function -> name -> the values it assigns that name.
        self.assigned: dict[ast.AST | None, dict[str, list[ast.AST]]] = {}
        #: ``self.ev = asyncio.new_event_loop()``: attributes the file binds to a loop.
        self.loop_attrs = {
            target.attr
            for node in module.parents
            if isinstance(node, ast.Assign) and _getter(node.value)
            for target in node.targets
            if isinstance(target, ast.Attribute)
        }

    def _bindings(self, scope: ast.AST) -> dict[str, list]:
        if scope in self.scopes:
            return self.scopes[scope]
        table: dict[str, list] = {}
        if isinstance(scope, _FUNCS):
            args = scope.args
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
                if arg is not None:
                    table.setdefault(arg.arg, []).append("param")
        for node in _scope_nodes(scope):
            if isinstance(node, ast.Import):
                for a in node.names:
                    bound = a.asname or a.name.split(".")[0]
                    table.setdefault(bound, []).append("time" if a.name == "time" else "other")
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    clock = node.module == "time" and a.name in _CLOCK_CALLS
                    table.setdefault(a.asname or a.name, []).append("bare" if clock else "other")
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        table.setdefault(target.id, []).append(node.value or "other")
        self.scopes[scope] = table
        return table

    def resolve(self, name: str, fn: ast.AST | None, seen=frozenset()) -> set[str]:
        """What *name* is bound to where *fn* reads it: ``"time"``, ``"bare"``,
        ``"loop"``, ``"param"``, ``"other"`` (a cycle resolves to ``"other"``)."""
        while True:
            scope = fn if fn is not None else self.module.tree
            bindings = self._bindings(scope).get(name)
            if bindings is not None:
                if (name, scope) in seen:
                    return {"other"}
                seen = seen | {(name, scope)}
                kinds: set[str] = set()
                for b in bindings:
                    kinds |= {b} if isinstance(b, str) else self._value_kind(b, fn, seen)
                return kinds
            if fn is None:
                return {"time"} if name == "time" else set()
            fn = self.module.enclosing_function(fn)

    def _value_kind(self, value: ast.AST, fn, seen) -> set[str]:
        if isinstance(value, ast.Name):
            return self.resolve(value.id, fn, seen)
        if _getter(value):
            return {"loop"}
        if isinstance(value, ast.Attribute):
            if self._is_time(value.value, fn, seen) and value.attr in _CLOCK_CALLS:
                return {"bare"}  # ``now = time.monotonic``
            if self._is_time(value, fn, seen):
                return {"time"}
        return {"other"}

    def _is_time(self, node: ast.AST, fn, seen=frozenset()) -> bool:
        if isinstance(node, ast.Name):
            # *seen* carries the bindings being resolved, so ``x = x.attr`` ends.
            return "time" in self.resolve(node.id, fn, seen)
        return self.module.stdlib_of(node) == "time"

    def _is_loop(self, node: ast.AST, fn) -> bool:
        if _getter(node):
            return True
        if isinstance(node, ast.Name):
            kinds = self.resolve(node.id, fn)
            return "loop" in kinds or (node.id in _LOOP_NAMES and kinds <= {"param"})
        return isinstance(node, ast.Attribute) and (
            node.attr in _LOOP_NAMES or node.attr in self.loop_attrs
        )

    def kind(self, node: ast.AST) -> str | None:
        """``"real"`` or ``"manual"`` for a clock-named call, else None."""
        if not isinstance(node, ast.Call):
            return None
        fn = self.module.enclosing_function(node)
        func = node.func
        if isinstance(func, ast.Name):
            return "real" if "bare" in self.resolve(func.id, fn) else None
        if not isinstance(func, ast.Attribute) or func.attr not in _CLOCK_CALLS:
            return None
        receiver = func.value
        if self._is_time(receiver, fn) or (func.attr == "time" and self._is_loop(receiver, fn)):
            return "real"
        return "manual"

    def values(self, fn: ast.AST | None, name: str) -> list[ast.AST]:
        """The values *fn* assigns to the bare *name*."""
        if fn is None:
            return []
        if fn not in self.assigned:
            table: dict[str, list[ast.AST]] = {}
            for node in ast.walk(fn):
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            table.setdefault(target.id, []).append(node.value)
                elif isinstance(node, ast.NamedExpr):
                    table.setdefault(node.target.id, []).append(node.value)
            self.assigned[fn] = table
        return self.assigned[fn].get(name, [])

    def reads_real(self, fn, value: ast.AST, seen=frozenset()) -> bool:
        """*value* reads the real clock, itself or through the names it reads
        (``before = started`` after ``started = time.monotonic()``)."""
        for node in ast.walk(value):
            if self.kind(node) == "real":
                return True
            if isinstance(node, ast.Name) and node.id not in seen:
                inner = seen | {node.id}
                if any(self.reads_real(fn, v, inner) for v in self.values(fn, node.id)):
                    return True
        return False


#: How sure K8 is that an expression is a real-clock span. A delta-named attribute
#: (``result.duration``, ``plan.duration_ms``) is often a computed or planned number,
#: so it counts only against a literal limit, as K8 always counted it.
_STRONG, _WEAK = 2, 1
_CONSTANT = re.compile(r"^_*[A-Z][A-Z0-9_]*$")


def _number(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def _not_a_real_stamp(clocks: _Clocks, fn: ast.AST | None, side: ast.AST) -> bool:
    """A stamp the function sets without the real clock, directly or through another
    name (``start = s.index(...)``, ``t0 = clock.monotonic()``), or one read off a
    driven clock (``clock.now``)."""
    if isinstance(side, ast.Name):
        values = clocks.values(fn, side.id)
        return bool(values) and not any(
            clocks.reads_real(fn, value, frozenset({side.id})) for value in values
        )
    return (
        isinstance(side, ast.Attribute)
        and side.attr in _CLOCK_CALLS | {"now"}
        and not clocks._is_time(side.value, fn)
    )


def _real_stamp(clocks: _Clocks, fn: ast.AST | None, side: ast.AST) -> bool:
    """A name every assignment of which reads the real clock, not offset by anything."""
    if not isinstance(side, ast.Name):
        return False
    values = clocks.values(fn, side.id)
    return bool(values) and all(
        not isinstance(value, ast.BinOp) and clocks.reads_real(fn, value, frozenset({side.id}))
        for value in values
    )


def _constant(node: ast.AST) -> bool:
    """A literal, a constant (``_CEILING``, ``reg._RETENTION_DAYS``) or arithmetic of them."""
    if isinstance(node, ast.BinOp):
        return _constant(node.left) and _constant(node.right)
    if isinstance(node, ast.UnaryOp):
        return _constant(node.operand)
    name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
    return _number(node) or bool(_CONSTANT.match(name))


def _cutoff(clocks: _Clocks, node: ast.AST) -> bool:
    """A real clock read minus a constant (``t0 = time.monotonic() - 5.0``): stored, it
    is a cutoff taken before the work, so comparing a later stamp to it only orders."""
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Sub)
        and clocks.kind(node.left) == "real"
        and _constant(node.right)
    )


def _opaque(clocks: _Clocks, node: ast.AST) -> bool:
    """A value K8 cannot see into: a call that is not a clock read, or an await."""
    return isinstance(node, ast.Await) or (isinstance(node, ast.Call) and not clocks.kind(node))


def _by_name(name: str) -> int:
    return 0 if _CONSTANT.match(name) or not _DELTA_NAMES.search(name) else _STRONG


def _span(clocks: _Clocks, fn: ast.AST | None, node: ast.AST, seen=frozenset()) -> int:
    """:data:`_STRONG` for a span read off the real clock or named like one, else
    :data:`_WEAK` (a delta-named attribute) or 0."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Div, ast.FloorDiv)):
        sides = (node.left, node.right) if isinstance(node.op, ast.Mult) else (node.left,)
        return max(_span(clocks, fn, side, seen) for side in sides)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
        # The clock on the left: time since a stamp. On the right it is time remaining.
        kind = clocks.kind(node.left)
        if kind is not None:
            return _STRONG if kind == "real" else 0
        sides = (node.left, node.right)
        names = [_dotted(side).rsplit(".", 1)[-1] for side in sides]
        if not all(names) or not all(_STAMP_NAMES.match(name) for name in names):
            # ``stop - start``, ``end_time - start_time``: any two names the function
            # sets from the real clock. A deadline or cutoff (``time.monotonic() + 5``)
            # is not a stamp, so ``deadline - now`` stays time remaining.
            return _STRONG if all(_real_stamp(clocks, fn, side) for side in sides) else 0
        return 0 if any(_not_a_real_stamp(clocks, fn, side) for side in sides) else _STRONG
    if isinstance(node, ast.Name):
        # A name the function assigns is what it is assigned (``spent = now() - t0``,
        # ``elapsed = clock.monotonic() - t0``); only an unassigned one, or one only a
        # helper returns (``elapsed = run_timed()``), goes by its name.
        values = clocks.values(fn, node.id)
        if not values:
            return _by_name(node.id)
        if node.id in seen:
            return 0
        found = max(
            0 if _cutoff(clocks, value) else _span(clocks, fn, value, seen | {node.id})
            for value in values
        )
        if all(_opaque(clocks, value) for value in values):
            return max(found, _by_name(node.id))
        return found
    if isinstance(node, ast.Attribute) and not _CONSTANT.match(node.attr):
        return _WEAK if _DELTA_NAMES.search(_dotted(node)) else 0
    return 0


def _bounds(clocks: _Clocks, fn: ast.AST | None, span: ast.AST, limit: ast.AST) -> bool:
    found = _span(clocks, fn, span)
    if found and _span(clocks, fn, limit):
        # Span against span (``elapsed >= min_duration``, ``again < waited / 4``): a
        # floor or a ratio of two readings of the same host, not a budget.
        return False
    return found == _STRONG or (found == _WEAK and _number(limit))


def _age_bound(clocks: _Clocks, node: ast.AST) -> bool:
    """The reversed form's right side as written: a real clock read minus a limit
    (``time.monotonic() - 5``), maybe scaled: the clock is read after the work."""
    while isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Div)):
        node = node.left
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Sub)
        and clocks.kind(node.left) == "real"
    )


def _reversed(clocks: _Clocks, fn, limit: ast.AST, span: ast.AST) -> bool:
    """``limit > span``: an age through a clock read, or a span (``BUDGET > spent``).
    A bare stamp (``rt.at > t0``) or a stored cutoff only orders two stamps."""
    return _age_bound(clocks, span) or _bounds(clocks, fn, span, limit)


def k8_duration_bound(module) -> Iterable[ast.AST]:
    clocks = _Clocks(module)
    for node in module.asserts:
        if not isinstance(node.test, ast.Compare):
            continue
        fn = module.enclosing_function(node)
        left = node.test.left
        for op, right in zip(node.test.ops, node.test.comparators):
            if (isinstance(op, (ast.Lt, ast.LtE)) and _bounds(clocks, fn, left, right)) or (
                isinstance(op, (ast.Gt, ast.GtE)) and _reversed(clocks, fn, left, right)
            ):
                yield node
                break
            left = right
    for call in module.calls:
        position = _UNITTEST_UPPER.get(_dotted(call.func).rsplit(".", 1)[-1])
        if position is not None and len(call.args) >= 2:
            span, limit = call.args[position], call.args[1 - position]
            fn = module.enclosing_function(call)
            if _reversed(clocks, fn, limit, span) if position else _bounds(clocks, fn, span, limit):
                yield call


_TIME = "import time\n"
K8_CASES: tuple[tuple[str, ...], str] = (
    (
        _TIME + "def test_x():\n    t = time.monotonic()\n    run()\n"
        "    assert time.monotonic() - t < 1.0\n",
        "def test_x():\n    assert (\n        elapsed < 5.0\n    ), 'took too long'\n",
        "def test_x():\n    assert 0.15 < elapsed < 1.0\n",
        "def test_x():\n    assert after - before < 0.05\n",
        _TIME + "def test_x():\n    start = time.thread_time()\n    run()\n"
        "    assert time.thread_time() - start < 0.5\n",
        # A non-literal limit: a name, a module constant, a product.
        "def test_x(budget):\n    assert elapsed < budget\n",
        _TIME + "def test_x():\n    assert time.monotonic() - started < _CEILING\n",
        "def test_x(x):\n    assert elapsed < x * 1.6\n",
        # The reversed form: an age bounded through a real clock read.
        _TIME + "def test_x():\n    assert stamp > time.monotonic() - 5\n",
        _TIME + "def test_x():\n    assert 2.0 >= (time.perf_counter() - t0) * 1000\n",
        # unittest, the span first (or second for assertGreater).
        "class T(TestCase):\n    def test_x(self):\n        self.assertLess(elapsed, 2.0)\n",
        "class T(TestCase):\n    def test_x(self):\n"
        "        self.assertLessEqual(self.duration, 2.0)\n",
        _TIME + "class T(TestCase):\n    def test_x(self):\n"
        "        self.assertGreater(LIMIT, time.monotonic() - t0)\n",
        # An event loop's time() is the real clock.
        "def test_x(loop):\n    assert loop.time() - t0 < 1.0\n",
        "import asyncio\nasync def test_x():\n    started = asyncio.get_running_loop().time()\n"
        "    elapsed = asyncio.get_running_loop().time() - started\n    assert elapsed < 10.0\n",
        "class T:\n    def test_x(self):\n        assert self._loop.time() - t0 < cap\n",
        # The real clock through an alias, a bare import, a scaled span, a named span.
        "import time as _t\ndef test_x():\n    assert _t.perf_counter() - t0 <= cap\n",
        "from time import monotonic as mono\ndef test_x():\n    assert mono() - t0 < cap\n",
        _TIME + "def test_x():\n    assert (time.monotonic() - t0) * 1000 < budget_ms\n",
        # A self-rebinding (``run = run.__wrapped__``) resolves without recursing.
        "def test_x(run):\n    run = run.__wrapped__\n    elapsed = run()\n    assert elapsed < 5.0\n",
        _TIME + "def test_x():\n    spent = time.monotonic() - t0\n    assert spent < cap\n",
        _TIME + "clock = time\ndef test_x():\n    assert clock.monotonic() - t0 < cap\n",
        _TIME + "def test_x():\n    start = time.monotonic()\n    end = time.monotonic()\n"
        "    assert end - start <= cap\n",
        # A stamp through an alias, against a literal and a non-literal limit.
        _TIME + "def test_x():\n    started = time.monotonic()\n    before = started\n"
        "    after = time.monotonic()\n    assert after - before < 0.05\n",
        _TIME + "def test_x(cap):\n    started = time.monotonic()\n    before = started\n"
        "    after = time.monotonic()\n    assert after - before < cap\n",
        # The reversed form with a span second: proven, or named like one.
        _TIME + "def test_x():\n    spent = time.monotonic() - t0\n    assert BUDGET > spent\n",
        _TIME + "class T(TestCase):\n    def test_x(self):\n"
        "        elapsed = time.monotonic() - t0\n        self.assertGreater(5.0, elapsed)\n",
        "def test_x():\n    assert BUDGET > elapsed\n",
        "class T(TestCase):\n    def test_x(self):\n        self.assertGreaterEqual(LIMIT, took)\n",
        # A clock alias or bare import reaches only its own function.
        _TIME + "def test_x():\n    clock = time\n    assert clock.monotonic() - t0 < cap\n"
        "def test_y(clock):\n    assert clock.monotonic() - t0 < cap\n",
        "def test_x():\n    from time import monotonic as now\n    assert now() - t0 < cap\n"
        "def test_y(now):\n    assert now() - t0 < cap\n",
        # A span only a helper returns goes by its name.
        "def test_x():\n    elapsed = run_timed()\n    assert elapsed < 5.0\n",
        "async def test_x():\n    elapsed = await measure()\n    assert elapsed < budget\n",
        # A loop bound from a getter, or pytest-asyncio's fixture.
        "import asyncio\ndef test_x():\n    ev = asyncio.get_running_loop()\n"
        "    assert ev.time() - t0 < cap\n",
        "import asyncio\nclass T:\n    def setUp(self):\n        self.ev = asyncio.new_event_loop()\n"
        "    def test_x(self):\n        assert self.ev.time() - t0 < cap\n",
        "def test_x(event_loop):\n    assert event_loop.time() - t0 < cap\n",
        # Two stamps named any way, through a named span.
        _TIME + "def test_x():\n    start = time.monotonic()\n    run()\n"
        "    stop = time.monotonic()\n    elapsed = stop - start\n    assert elapsed < 5.0\n",
        _TIME + "def test_x():\n    start_time = time.perf_counter()\n    run()\n"
        "    end_time = time.perf_counter()\n    assert end_time - start_time < cap\n",
    ),
    # A clock the test drives, time remaining, lower bounds, a named
    # span read off a manual clock, a unittest lower bound, and a flake-ok line.
    _TIME + "def test_x(clock, vclock, budget):\n    assert clock.monotonic() - t0 < budget\n"
    "    assert vclock.time() - t0 < 1.0\n"
    "    assert deadline - time.monotonic() <= cap\n    assert elapsed > 0.1\n"
    "    assert 0.1 < time.monotonic() - t0\n    assert stamp < time.monotonic() - 5\n"
    "    assert budget >= 0\ndef test_y(clock):\n    elapsed = clock.monotonic() - t0\n"
    "    assert elapsed < 2.0\n    before = clock.monotonic()\n    after = clock.time()\n"
    "    assert after - before < 0.05\nclass T(TestCase):\n    def test_z(self):\n"
    "        self.assertGreater(elapsed, 0.1)\n        self.assertLess(count, 3)\n"
    "def test_w():\n    assert elapsed < 5.0  # flake-ok: the lost-run bound of this wait\n"
    # A constant, a computed span, a planned duration, string offsets, and time
    # remaining through a delta-named local.
    "def test_v(plan, ctx):\n    assert EXPECTED_DELTA <= flatten(old)\n"
    "    assert plan.duration_ms <= MAX_MS\n    assert deadline.wall <= time.time() + 9\n"
    "    elapsed = (TICKS + 3) * CADENCE\n    assert elapsed < STALL\n"
    "    start = ctx.index('[')\n    end = ctx.index(']', start)\n"
    "    assert end - start <= CAP\n    delta = exp - time.time()\n"
    "    assert delta <= TTL\n    assert clock.now - withdrawn_at <= window\n"
    # The reversed form against a bare name: an ordering check, or a span named like
    # one but not read off the clock where it is compared.
    "def test_u(rt):\n    t0 = time.monotonic() - 5.0\n    assert rt.at > t0\n"
    "    past = time.time() - 500.0\n    assert tracker.last > past\n"
    "    cutoff = time.time() - 86400\n    assert st.st_mtime >= cutoff\n"
    "    cutoff = time.time() - reg._RETENTION_DAYS * 86400\n    assert st.st_mtime > cutoff\n"
    "    started = time.monotonic()\n    assert rt.at > started\n"
    "def test_s(vclock):\n    assert stamp > vclock.time() - 5\n"
    # A clock bound in another function, a fixture shadowing a module alias, a loop
    # named like one but driven, a helper's non-span result, an alias cycle.
    "tm = time\ndef test_r():\n    import time as tick\n    from time import monotonic as now\n"
    "def test_q(tm, tick, now):\n    assert tm.monotonic() - t0 < cap\n"
    "    assert tick.monotonic() - t0 < cap\n    assert now() - t0 < cap\n"
    "def test_p(virtual_loop):\n    assert virtual_loop.time() - t0 < 1.0\n"
    "    spent = run_timed()\n    assert spent < 5.0\n"
    "    before = after\n    after = before\n    assert after - before < 0.05\n"
    # Time remaining between two names off the real clock, one a deadline.
    "def test_o():\n    deadline = time.monotonic() + 5\n    current = time.monotonic()\n"
    "    assert deadline - current <= cap\n"
    # A floor or a ratio between two spans: not a budget.
    "def test_n(min_duration, min_wall):\n    assert elapsed >= min_duration\n"
    "    assert min_wall <= elapsed\n    assert elapsed < first_elapsed / 4\n"
    "    assert time.monotonic() - t0 >= expected_elapsed\n"
    "class T2(TestCase):\n    def test_m(self, min_duration):\n"
    "        self.assertGreaterEqual(elapsed, min_duration)\n",
)
