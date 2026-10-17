"""Ratchet: how many tests still bet on a fixed short wait.

The single commonest cause of a flaky backend test in this repository: the test
waits a fixed, short time for something the code under test signals -- a 5 s
``asyncio.wait_for``, a ``.result(timeout=5)``, a poll of 200 five-millisecond
turns, a 50 ms module constant -- and a CI runner, slower than the machine the test
was written on, takes longer. The bet loses often enough to red unrelated pull
requests (the tracking issue for this gate lists the fixes it is drawn from).

The discipline, "wait on signals, not clocks": a test awaits the event, future or
state the code emits (``kiro_crew.testing.wait``: ``await_until``,
``async_wait_until``, ``wait_until``, ``LoopHop``). A ceiling exists only to turn a
hang into a failure that names itself, so it is LARGE -- the helpers default to
half the test's own timeout -- and it never decides pass versus fail.

The tree already holds close to three thousand short fixed waits, so this module measures
them file by file and ratchets: :data:`BASELINE_FILE` holds, for every file that
has any, the count that file had when this gate landed. A file's count may go DOWN
in any change and may never go up; a file not listed starts at zero. A new short
wait must await its signal instead, or carry ``# flake-ok: <reason>`` with a real
reason (the marker ``test_flake_pattern_ratchet.py`` honours, so no site needs two).

What counts, in ``test/**/*.py``, as real call expressions (``ast``, so a name in
a docstring or a comment is not a site):

* ``asyncio.wait_for(..., timeout=N)``, ``asyncio.timeout(N)`` and
  ``<future>.result(timeout=N)`` with N below :data:`LOST_RUN_FLOOR_SECS`, 30 s:
  the ceiling D1 names as the one that only names a hang. ``timeout=5`` is the
  commonest value in the tree (over a thousand sites) and the one that lost on CI,
  so a floor that let it through would make raising 2 to 5 the cheapest way past a
  red gate. The off-loop hop onto a loop doing durable work (a SQLite commit, an
  fsync) exceeds 5 s on the Windows runner;
* ``time.sleep(N)`` and ``asyncio.sleep(N)`` with 0 < N below
  :data:`SHORT_POLL_SLEEP_SECS` inside a loop: a poll, whose budget is the loop's
  iteration count times N -- a clock bet in disguise -- or a hand-rolled copy of
  the helper that already exists. ``sleep(0)`` is a loop-turn yield, not a wait,
  and a sleep outside a loop belongs to ``semgrep/test-determinism.yaml`` and
  ``test_flake_pattern_ratchet.py``.

N is a numeric literal, or a module-level constant assigned one: the 50 ms
``_TRANSFER_TIMEOUT`` was a constant. A site under ``pytest.raises(TimeoutError)``
is exercising the timeout path on purpose and is not counted.

Why a committed, per-file baseline when ``test_flake_pattern_ratchet.py`` already
counts short waits: that gate judges a change against its merge-base, which CI's
depth-1 checkout cannot read, so it is a local pre-push gate, and its "short" is
under two seconds. This one holds every file to a number CI can read on any
checkout, and names the file whose count rose. Lowering the thousands already here
is a separate, tracked effort; this gate only stops the number growing.

A moved or renamed file keeps its count: its line moves to the new path, count
unchanged (``--move OLD NEW``). A split file splits its line, the counts summing
to the old one. Neither raises a count, so neither is what the ratchet forbids;
a reviewer sees a moved line as a changed path with the same number.

``python test/test_wait_discipline_ratchet.py`` prints the burn-down view;
``--write-baseline`` rewrites :data:`BASELINE_FILE` with every entry lowered to
its real count and gone files dropped, and never raises or adds an entry;
``--move OLD NEW`` carries a line to a renamed file's path.
"""

from __future__ import annotations

import ast
import functools
import re
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_wait_discipline_ratchet")

TEST_ROOT = Path(__file__).resolve().parent

#: Below this, a ``wait_for`` / ``asyncio.timeout`` / ``Future.result`` ceiling is a
#: bet on the runner's speed, not a lost-run bound (rule D1). The helpers default to
#: half the suite's ``--timeout=120``, twice this.
LOST_RUN_FLOOR_SECS = 30.0

#: A sleep this short inside a loop is a poll; its budget is iterations times N.
SHORT_POLL_SLEEP_SECS = 5.0

#: A ``flake-ok`` reason shorter than this is not a reason (same rule as
#: ``test_flake_pattern_ratchet.py``).
MIN_REASON_CHARS = 10
_FLAKE_OK = re.compile(r"#\s*flake-ok:\s*(?P<reason>\S.*?)\s*$")

#: A file whose text has none of these holds none of the shapes, and is not parsed.
_TRIGGERS = ("wait_for", ".result(", "sleep", "timeout(")

_TIMEOUT_ERRORS = frozenset({"TimeoutError", "asyncio.TimeoutError", "futures.TimeoutError"})
_SLEEP_HOMES = frozenset({"time", "asyncio"})
_LOOPS = (ast.For, ast.AsyncFor, ast.While)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

#: The one file under ``test/`` the scan does not read: this one. Everything else,
#: conftest and helpers included, is held to its count. The shared helpers live in
#: ``src/kiro_crew/testing/wait.py``, outside the scanned tree.
EXCLUDED = frozenset({"test_wait_discipline_ratchet.py"})

#: Per file: the short fixed waits it held when this gate landed, one ``<count>
#: <path relative to test/>`` per line. RATCHET: lower a line (or delete it at
#: zero) when a change replaces a wait with its signal; never raise or add one for
#: a site of your own. A file not listed has a baseline of zero; a renamed file's
#: line moves with it (``--move OLD NEW``), count unchanged.
BASELINE_FILE = TEST_ROOT / "wait_discipline_baseline.txt"

_BASELINE_HEADER = """\
# Short fixed waits per test file when the wait-discipline gate landed.
# Read by test/test_wait_discipline_ratchet.py; one "<count> <path under test/>" per line.
# RATCHET: a count may only go down (delete the line at zero); never raise or add one.
# A renamed file keeps its line under the new path, count unchanged; a split file
# splits its line, the parts summing to the old count.
# `python test/test_wait_discipline_ratchet.py --write-baseline` lowers every line to
# its real count and drops files that are gone; it never raises or adds an entry.
# `... --move OLD NEW` carries a line to a renamed file's path.
"""

MOVE_HINT = (
    "Moved or renamed a test file? Its line moves with it, count unchanged: "
    "`python test/test_wait_discipline_ratchet.py --move OLD NEW`. Split one? Split its "
    "line so the parts sum to the old count."
)

DRIFT_HINT = (
    "Not your sites -- main itself is red here, because a change merged after this "
    "baseline was generated added them? Then the baseline is behind main: raise exactly "
    "those lines to main's real counts in a separate, baseline-only pull request "
    "(`--write-baseline` will not do it; it only lowers), and rebase this one on it."
)

HELPERS_HINT = (
    "Wait on the signal instead: await the event, future or state the code emits with "
    "kiro_crew.testing.wait (await_until for an Event or a future; "
    "async_wait_until / wait_until for a state; LoopHop for an off-loop hop onto the "
    "loop). Their default ceiling is half the test's timeout and only names a hang; a "
    "wait_for that must stay takes a ceiling of 30 s or more for the same reason. A "
    "site that must keep a short wait says why on its line with `# flake-ok: <reason>`. "
    "See docs/system-specs/common/testing-conventions.md, rule D1."
)


@dataclass(frozen=True)
class Site:
    rel: str
    line: int
    kind: str
    seconds: float

    def __str__(self) -> str:
        return f"test/{self.rel}:{self.line}  {self.kind}({self.seconds:g}s)"


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else node.attr
    return ""


def _literal(node: ast.AST | None) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            return None
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _literal(node.operand)
        return None if inner is None else -inner
    return None


class _Module:
    """One parsed file: parents, module-level numeric constants, import aliases."""

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.parents: dict[ast.AST, ast.AST] = {}
        self.calls: list[ast.Call] = []
        self.constants: dict[str, float] = {}
        #: local name -> stdlib module, for ``import asyncio as aio``.
        self.modules: dict[str, str] = {m: m for m in _SLEEP_HOMES}
        #: local name -> ``module.name``, for ``from asyncio import sleep``.
        self.from_imports: dict[str, str] = {}
        stack: list[ast.AST] = [tree]
        while stack:
            node = stack.pop()
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
                stack.append(child)
            if isinstance(node, ast.Call):
                self.calls.append(node)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _SLEEP_HOMES:
                        self.modules[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module in _SLEEP_HOMES:
                for alias in node.names:
                    self.from_imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        for stmt in tree.body:
            target: ast.AST | None = None
            value: ast.AST | None = None
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target, value = stmt.targets[0], stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                target, value = stmt.target, stmt.value
            if isinstance(target, ast.Name):
                number = _literal(value)
                if number is not None:
                    self.constants[target.id] = number

    def callee(self, call: ast.Call) -> str:
        """``asyncio.wait_for`` however the file spelled it; ``<obj>.result`` for a method."""
        func = call.func
        if isinstance(func, ast.Name):
            return self.from_imports.get(func.id, func.id)
        if isinstance(func, ast.Attribute):
            module = self.modules.get(_dotted(func.value))
            if module is not None:
                return f"{module}.{func.attr}"
            return f"<obj>.{func.attr}"
        return ""

    def seconds(self, node: ast.AST | None) -> float | None:
        """A numeric literal, or a module-level name bound to one."""
        number = _literal(node)
        if number is not None:
            return number
        if isinstance(node, ast.Name):
            return self.constants.get(node.id)
        return None

    def inside_loop(self, node: ast.AST) -> bool:
        """A loop between *node* and its enclosing function, class or lambda."""
        current = self.parents.get(node)
        while current is not None and not isinstance(current, _SCOPES):
            if isinstance(current, _LOOPS):
                return True
            current = self.parents.get(current)
        return False

    def inside_raises_timeout(self, node: ast.AST) -> bool:
        current = self.parents.get(node)
        while current is not None:
            if isinstance(current, (ast.With, ast.AsyncWith)):
                for item in current.items:
                    call = item.context_expr
                    if (
                        isinstance(call, ast.Call)
                        and _dotted(call.func).endswith("raises")
                        and call.args
                        and _dotted(call.args[0]) in _TIMEOUT_ERRORS
                    ):
                        return True
            current = self.parents.get(current)
        return False


def _argument(module: _Module, call: ast.Call, keyword: str, position: int) -> float | None:
    for kw in call.keywords:
        if kw.arg == keyword:
            return module.seconds(kw.value)
    if len(call.args) > position:
        return module.seconds(call.args[position])
    return None


def _short_waits(module: _Module) -> Iterable[tuple[ast.Call, str, float]]:
    """``(call, kind, seconds)`` for every short fixed wait in *module*."""
    for call in module.calls:
        callee = module.callee(call)
        if callee == "asyncio.wait_for":
            seconds = _argument(module, call, "timeout", 1)
            if seconds is not None and seconds < LOST_RUN_FLOOR_SECS:
                if not module.inside_raises_timeout(call):
                    yield call, "wait_for", seconds
        elif callee == "asyncio.timeout":
            seconds = _argument(module, call, "delay", 0)
            if seconds is not None and seconds < LOST_RUN_FLOOR_SECS:
                if not module.inside_raises_timeout(call):
                    yield call, "asyncio.timeout", seconds
        elif callee == "<obj>.result":
            seconds = _argument(module, call, "timeout", 0)
            if seconds is not None and seconds < LOST_RUN_FLOOR_SECS:
                if not module.inside_raises_timeout(call):
                    yield call, "result", seconds
        elif callee in ("time.sleep", "asyncio.sleep"):
            seconds = _argument(module, call, "delay", 0)
            if seconds is not None and 0 < seconds < SHORT_POLL_SLEEP_SECS:
                if module.inside_loop(call):
                    yield call, "sleep-in-loop", seconds


def _flake_ok(line: str) -> bool:
    match = _FLAKE_OK.search(line)
    if not match:
        return False
    reason = match.group("reason")
    return len(reason) >= MIN_REASON_CHARS and bool(re.search(r"[A-Za-z]{3,}", reason))


def sites_in(source: str, rel: str) -> list[Site]:
    """Every counted site in one file's source; ``flake-ok`` lines are excused."""
    if not any(word in source for word in _TRIGGERS):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    module = _Module(tree)
    lines = source.splitlines()
    found: list[Site] = []
    for call, kind, seconds in _short_waits(module):
        start = call.lineno
        end = call.end_lineno or start
        span = range(start, min(end, len(lines)) + 1)
        if any(_flake_ok(lines[n - 1]) for n in span):
            continue
        found.append(Site(rel, start, kind, seconds))
    return sorted(found, key=lambda s: (s.line, s.kind))


def scanned_files(root: Path = TEST_ROOT) -> list[Path]:
    return sorted(
        path for path in root.rglob("*.py") if path.name not in EXCLUDED and path.is_file()
    )


def scan(root: Path = TEST_ROOT) -> dict[str, list[Site]]:
    """rel path -> its sites, for every scanned file that has any."""
    out: dict[str, list[Site]] = {}
    for path in scanned_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found = sites_in(source, rel)
        if found:
            out[rel] = found
    return out


@functools.lru_cache(maxsize=1)
def _tree_scan() -> dict[str, list[Site]]:
    """The real tree, read once per process: the tests below share one scan."""
    return scan()


def _counts(found: dict[str, list[Site]]) -> Counter[str]:
    return Counter({rel: len(sites) for rel, sites in found.items()})


def read_baseline(path: Path = BASELINE_FILE) -> dict[str, int]:
    """``{rel: count}`` from :data:`BASELINE_FILE`; a malformed line is an error here,
    not a silently ignored entry."""
    out: dict[str, int] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        count, _, rel = line.partition(" ")
        if not count.isdigit() or not rel or rel in out:
            raise ValueError(f"{path.name}:{number}: expected '<count> <path>', got {raw!r}")
        out[rel] = int(count)
    return out


def _format_baseline(counts: dict[str, int]) -> str:
    width = max((len(str(n)) for n in counts.values()), default=1)
    body = "".join(f"{n:>{width}} {rel}\n" for rel, n in sorted(counts.items()) if n > 0)
    return _BASELINE_HEADER + body


def lowered_baseline(baseline: dict[str, int], counts: Counter[str]) -> dict[str, int]:
    """Every entry at ``min(baseline, real)``, gone files dropped. Never raises or adds."""
    return {rel: min(was, counts.get(rel, 0)) for rel, was in baseline.items()}


def moved_baseline(baseline: dict[str, int], old: str, new: str) -> dict[str, int]:
    """The entry for ``old`` carried to ``new``, count unchanged: a renamed file.

    Refused when ``old`` has no line (nothing to carry) or ``new`` already has one
    (two files merging add their counts, which is a hand edit a reviewer reads).
    """
    if old not in baseline:
        raise ValueError(f"{old} has no baseline line to move")
    if new in baseline:
        raise ValueError(f"{new} already has a baseline line; merge the counts by hand")
    moved = dict(baseline)
    moved[new] = moved.pop(old)
    return moved


# ── the gate ─────────────────────────────────────────────────────────────


def test_no_file_gains_a_short_fixed_wait() -> None:
    """A file's count of short fixed waits may only fall; a new file starts at zero."""
    found = _tree_scan()
    baseline = read_baseline()
    counts = _counts(found)
    risen = {
        rel: (baseline.get(rel, 0), n) for rel, n in counts.items() if n > baseline.get(rel, 0)
    }
    if not risen:
        return
    report = []
    for rel, (was, now) in sorted(risen.items()):
        report.append(f"  test/{rel}: {was} -> {now}")
        report.extend(f"      {site}" for site in found[rel])
    raise AssertionError(
        f"{len(risen)} file(s) gained a short fixed wait (a wait_for, asyncio.timeout or "
        f".result(timeout) under {LOST_RUN_FLOOR_SECS:g}s, or a sleep under "
        f"{SHORT_POLL_SLEEP_SECS:g}s inside a loop):\n" + "\n".join(report) + "\n"
        f"{HELPERS_HINT}\n{BASELINE_FILE.name} is a ratchet: a count may only go down. "
        f"{MOVE_HINT}\n{DRIFT_HINT}"
    )


def test_the_baseline_is_not_stale() -> None:
    """A baseline left above the real count hands the next regression a free pass.

    Without this, a change that replaces 20 waits in a file but leaves its line
    alone lets the next 20 regressions in that file pass. Lowering the line is the
    ratchet clicking; a line for a file that is gone is deleted.
    """
    baseline = read_baseline()
    counts = _counts(_tree_scan())
    stale = {
        rel: (was, counts.get(rel, 0)) for rel, was in baseline.items() if counts.get(rel, 0) < was
    }
    assert not stale, (
        f"{BASELINE_FILE.name} is above the real count for:\n"
        + "\n".join(
            f"  {was} -> {now} {rel}" + ("  (file gone: delete the line)" if now == 0 else "")
            for rel, (was, now) in sorted(stale.items())
        )
        + "\nLower these lines in this change -- that is the ratchet clicking: "
        "`python test/test_wait_discipline_ratchet.py --write-baseline` does exactly that."
    )


def test_the_baseline_is_well_formed() -> None:
    """Sorted, one line per file, no zero or stray entry, header intact -- so two
    changes lowering different files merge without conflict and a hand edit that
    adds a path the scan never visits is refused rather than quietly unenforced."""
    baseline = read_baseline()
    assert baseline, f"{BASELINE_FILE.name} is empty; the scan is broken or the file was wiped"
    files = {p.relative_to(TEST_ROOT).as_posix() for p in scanned_files()}
    strays = sorted(rel for rel in baseline if rel not in files)
    assert (
        not strays
    ), f"{BASELINE_FILE.name} names files the scan does not read: {strays}. {MOVE_HINT}"
    zeros = sorted(rel for rel, n in baseline.items() if n <= 0)
    assert not zeros, f"{BASELINE_FILE.name} has lines at zero; delete them: {zeros}"
    assert BASELINE_FILE.read_text(encoding="utf-8") == _format_baseline(baseline), (
        f"{BASELINE_FILE.name} is not in canonical form (header, then sorted "
        "'<count> <path>' lines); `python test/test_wait_discipline_ratchet.py "
        "--write-baseline` rewrites it"
    )


# ── positive controls: the detector finds each shape and not its near miss ──

_POSITIVE = {
    "wait_for keyword": (
        "import asyncio\nasync def t():\n    await asyncio.wait_for(f(), timeout=1)\n"
    ),
    "wait_for positional": "import asyncio\nasync def t():\n    await asyncio.wait_for(f(), 29.9)\n",
    "wait_for at five, the value that lost on CI": (
        "import asyncio\nasync def t():\n    await asyncio.wait_for(f(), timeout=5)\n"
    ),
    "wait_for from-import": (
        "from asyncio import wait_for\nasync def t():\n    await wait_for(f(), 2)\n"
    ),
    "asyncio.timeout": (
        "import asyncio\nasync def t():\n    async with asyncio.timeout(3):\n        pass\n"
    ),
    "result": "def t():\n    fut.result(timeout=5)\n",
    "result positional": "def t():\n    fut.result(29)\n",
    "result via constant": "_HOP = 0.05\ndef t():\n    fut.result(timeout=_HOP)\n",
    "wait_for via annotated constant": (
        "import asyncio\nLIMIT: float = 2.0\nasync def t():\n"
        "    await asyncio.wait_for(f(), timeout=LIMIT)\n"
    ),
    "asyncio.sleep in a counted for": (
        "import asyncio\nasync def t():\n    for _ in range(200):\n        if done():\n"
        "            break\n        await asyncio.sleep(0.01)\n"
    ),
    "time.sleep in a while": "import time\ndef t():\n    while not done():\n        time.sleep(0.5)\n",
    "sleep from-import in a loop": (
        "from time import sleep\ndef t():\n    for _ in range(3):\n        sleep(1)\n"
    ),
    "aliased module": (
        "import asyncio as aio\nasync def t():\n    while True:\n        await aio.sleep(0.2)\n"
    ),
    "sleep in an async for": (
        "import asyncio\nasync def t():\n    async for _ in source():\n"
        "        await asyncio.sleep(0.1)\n"
    ),
    "flake-ok without a real reason": (
        "import asyncio\nasync def t():\n"
        "    await asyncio.wait_for(f(), timeout=1)  # flake-ok: short\n"
    ),
}

_NEGATIVE = {
    "wait_for at the floor": (
        "import asyncio\nasync def t():\n    await asyncio.wait_for(f(), timeout=30)\n"
    ),
    "wait_for large": "import asyncio\nasync def t():\n    await asyncio.wait_for(f(), timeout=60)\n",
    "wait_for no literal": (
        "import asyncio\nasync def t():\n    await asyncio.wait_for(f(), timeout=limit)\n"
    ),
    "wait_for in raises": (
        "import asyncio, pytest\nasync def t():\n    with pytest.raises(TimeoutError):\n"
        "        await asyncio.wait_for(f(), timeout=0.1)\n"
    ),
    "wait_for of another object": "async def t():\n    await page.wait_for(selector, timeout=1)\n",
    "result at the floor": "def t():\n    fut.result(timeout=30)\n",
    "result in raises": (
        "import pytest\ndef t():\n    with pytest.raises(TimeoutError):\n"
        "        fut.result(timeout=0.01)\n"
    ),
    "sleep zero in a loop": (
        "import asyncio\nasync def t():\n    for _ in range(3):\n        await asyncio.sleep(0)\n"
    ),
    "sleep outside a loop": "import asyncio\nasync def t():\n    await asyncio.sleep(0.1)\n",
    "sleep at the floor": "import time\ndef t():\n    while True:\n        time.sleep(5)\n",
    "sleep in a nested def inside a loop": (
        "import time\ndef t():\n    for _ in range(3):\n        def inner():\n"
        "            time.sleep(0.1)\n"
    ),
    "flake-ok with a reason": (
        "import asyncio\nasync def t():\n"
        "    await asyncio.wait_for(f(), timeout=1)  # flake-ok: the timeout IS the behaviour\n"
    ),
    "a name in a docstring": (
        'def t():\n    """Calls asyncio.wait_for(f(), timeout=1) and fut.result(timeout=5)."""\n'
    ),
    "a name in a string": "def t():\n    s = 'time.sleep(0.5)'\n",
    "the helper, with a short bound": (
        "from kiro_crew.testing.wait import await_until\nasync def t():\n"
        "    await await_until(ev, timeout=0.1)\n"
    ),
    "asyncio.wait, which never cancels and is a different shape": (
        "import asyncio\nasync def t():\n    await asyncio.wait({f}, timeout=1)\n"
    ),
}


@pytest.mark.parametrize("case", sorted(_POSITIVE))
def test_the_detector_counts_each_shape(case: str) -> None:
    assert len(sites_in(_POSITIVE[case], "x.py")) == 1, case


@pytest.mark.parametrize("case", sorted(_NEGATIVE))
def test_the_detector_ignores_each_near_miss(case: str) -> None:
    assert sites_in(_NEGATIVE[case], "x.py") == [], case


def test_the_scan_matches_known_sites() -> None:
    """POSITIVE CONTROL on the real tree: the counts above are not low because the
    needles match nothing. Every kind the gate names is found somewhere under
    ``test/`` today."""
    found = _tree_scan()
    assert found, "no short fixed wait anywhere under test/; the needles are broken"
    kinds = {site.kind for sites in found.values() for site in sites}
    # asyncio.timeout sites have all been migrated; the gate still detects the shape.
    assert {"wait_for", "result", "sleep-in-loop"} <= kinds, kinds


def test_a_planted_short_wait_is_named(tmp_path: Path) -> None:
    """End to end on a scratch tree: a new file with one short wait rises from zero,
    and the report names the file, the line and the shape."""
    (tmp_path / "test_planted.py").write_text(
        "import asyncio\nasync def test_x():\n    await asyncio.wait_for(f(), timeout=1)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_clean.py").write_text(
        "import asyncio\nasync def test_x():\n    await asyncio.wait_for(f(), timeout=60)\n",
        encoding="utf-8",
    )
    found = scan(tmp_path)
    assert list(found) == ["test_planted.py"]
    assert [str(site) for site in found["test_planted.py"]] == [
        "test/test_planted.py:3  wait_for(1s)"
    ]


def test_the_baseline_reader_refuses_a_malformed_line(tmp_path: Path) -> None:
    bad = tmp_path / "b.txt"
    bad.write_text("# header\n3 test_a.py\nnot a count\n", encoding="utf-8")
    with pytest.raises(ValueError, match="b.txt:3"):
        read_baseline(bad)
    bad.write_text("3 test_a.py\n4 test_a.py\n", encoding="utf-8")
    with pytest.raises(ValueError, match="b.txt:2"):
        read_baseline(bad)


def test_a_moved_file_keeps_its_count() -> None:
    moved = moved_baseline({"old.py": 3, "other.py": 1}, "old.py", "new/path.py")
    assert moved == {"other.py": 1, "new/path.py": 3}
    with pytest.raises(ValueError, match="no baseline line"):
        moved_baseline({"a.py": 1}, "missing.py", "b.py")
    with pytest.raises(ValueError, match="already has"):
        moved_baseline({"a.py": 1, "b.py": 2}, "a.py", "b.py")


def test_lowering_the_baseline_never_raises_or_adds() -> None:
    lowered = lowered_baseline(
        {"a.py": 3, "b.py": 2, "gone.py": 1}, Counter({"a.py": 1, "b.py": 5, "new.py": 4})
    )
    assert lowered == {"a.py": 1, "b.py": 2, "gone.py": 0}
    assert "gone.py" not in _format_baseline(lowered)
    assert "new.py" not in _format_baseline(lowered)


# ── command line ────────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    if "--move" in argv:
        at = argv.index("--move")
        try:
            old, new = argv[at + 1], argv[at + 2]
        except IndexError:
            print("usage: --move OLD NEW  (paths relative to test/)", file=sys.stderr)
            return 64
        BASELINE_FILE.write_text(
            _format_baseline(moved_baseline(read_baseline(), old, new)), encoding="utf-8"
        )
        print(f"moved {old} -> {new} in {BASELINE_FILE}")
        return 0
    found = scan()
    counts = _counts(found)
    if "--write-baseline" in argv:
        baseline = read_baseline() if BASELINE_FILE.exists() else dict(counts)
        BASELINE_FILE.write_text(
            _format_baseline(lowered_baseline(baseline, counts)), encoding="utf-8"
        )
        print(f"wrote {BASELINE_FILE}")
        return 0
    kinds = Counter(site.kind for sites in found.values() for site in sites)
    for rel, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"{n:4d}  test/{rel}")
    print(f"\n{sum(counts.values())} short fixed wait(s) in {len(counts)} file(s): {dict(kinds)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
