"""Ratchet: a change may not add a shape that makes a test flaky to any file.

The Determinism contract in ``docs/system-specs/common/testing-conventions.md`` names
the shapes a test must not use. ``semgrep/test-determinism.yaml`` refuses the
commonest of them on a new line (a sleep as a barrier, a ``time`` patch, a bare
``monkeypatch.undo()``, a reload, a naive local-time read, an unseeded draw, an autouse
fixture on the shared ``monkeypatch``, a promise-sleep, ``waitForTimeout``). This module
counts per file only the shapes those patterns miss: patches of ``asyncio.sleep``, the
``datetime`` classes or another module's ``time`` binding, ``sys.modules`` evictions,
raw environment writes, duration bounds, short literal waits, literal listener ports,
crew-log flush ceilings and unpaused removals, and five frontend shapes (promise-sleeps
only in the Electron tests, which the semgrep rule does not read). So no site needs
both ``# nosemgrep`` and ``# flake-ok``. The tree already holds thousands of these, so
each file a change touches is held to its own count at the change's merge-base.

* A file's count may not rise; a new file starts at zero, a deleted file passes, and
  a rename git detects keeps its count. A rise is forgiven only for a site that
  moved: the same class in an identical enclosing function (the same line in
  TypeScript) that left another file in the same change, chains of moves included. A
  function edited in place, or a site a ``flake-ok`` still excuses, has not left. The
  working tree is judged, untracked files included.
* The base and the changed-file set are read through ``scripts/ratchet_scope.py``'s
  shared helpers (``change_merge_base``, ``change_files``, ``show_at``), the same
  module ten other diff-scoped gates read. Precedence: ``FLAKE_RATCHET_BASE`` when
  set; on CI the first parent of the merge commit a pull request, push or merge
  queue run checks out; else the newer of the merge-bases with ``upstream/<base>``
  and ``origin/<base>``, or the local ``<base>`` branch. When none is readable (a
  shallow clone, as CI's backend shards are today), the ratchet warns and compares
  nothing: judging against a stored count would red a change for sites that other,
  already merged changes added.
* A line that keeps a shape on purpose carries ``# flake-ok: <reason>`` (``//
  flake-ok: <reason>`` in TypeScript) with a reason of ten characters or more, words
  not punctuation, on a line of the finding.
* ``python test/test_flake_pattern_ratchet.py`` prints each file's current counts
  as the burn-down view; no committed file holds them.

The Python detectors read the AST, so a shape named in a docstring or a comment is not
counted; the TypeScript and JavaScript ones are line patterns. Each detector has a
positive and a negative case below, and a mutation proof that a detector finding
nothing fails its positive case. Only the files a change touches are read, so the
gate costs a git diff plus those files.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import warnings
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
from source_corpus import repo_files, repo_root
from test_ratchet_scope import (
    _GIT_COMMAND_LINE_CONFIG,
    _fixture_git_env,
    assert_fixture_ignores_ambient_git,
)

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

_RATCHET_SCOPE_SCRIPT = repo_root() / "scripts" / "ratchet_scope.py"
_SPEC = importlib.util.spec_from_file_location("ratchet_scope", _RATCHET_SCOPE_SCRIPT)
assert _SPEC and _SPEC.loader
ratchet_scope = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ratchet_scope)

pytestmark = pytest.mark.xdist_group(name="tree_scan_flake_ratchet")

#: A ``flake-ok`` reason shorter than this is not a reason.
MIN_REASON_CHARS = 10
_FLAKE_OK = re.compile(r"(?:#|//)\s*flake-ok:\s*(?P<reason>\S.*?)\s*(?:\*/)?$")

# ── which files are scanned ───────────────────────────────────────────


def _is_python_test_file(rel: str) -> bool:
    if not rel.endswith(".py"):
        return False
    name = rel.rsplit("/", 1)[-1]
    if name == "conftest.py":
        return True
    if rel.startswith("test/"):
        return True
    if rel.startswith("src/kiro_crew/apps/builtins/"):
        parts = rel.split("/")
        return any(part == "tests" or part.endswith("_tests") for part in parts[:-1])
    return False


_JS_SUFFIXES = (".ts", ".tsx", ".js", ".mjs", ".cjs")


def _is_js_test_file(rel: str) -> bool:
    if not rel.startswith("website/") or not rel.endswith(_JS_SUFFIXES):
        return False
    if "/node_modules/" in rel:
        return False
    name = rel.rsplit("/", 1)[-1]
    if rel.startswith(("website/integration/", "website/playwright/")):
        return True
    if rel.startswith("website/src/"):
        return ".test." in name or "/test/" in rel
    if rel.startswith("website/electron/"):
        return "/test/" in rel
    return False


# ── the flake-ok escape ───────────────────────────────────────────────


def _flake_ok(line: str) -> bool:
    match = _FLAKE_OK.search(line)
    if not match:
        return False
    reason = match.group("reason")
    return len(reason) >= MIN_REASON_CHARS and any(c.isalpha() for c in reason)


# ── Python detectors (AST) ────────────────────────────────────────────

#: Stdlib names whose rebinding changes the clock or the sleep of the whole worker.
_CLOCK_ATTRS = {
    "time": frozenset(
        {
            "time",
            "time_ns",
            "monotonic",
            "monotonic_ns",
            "perf_counter",
            "perf_counter_ns",
            "sleep",
            "localtime",
            "gmtime",
        }
    ),
    "asyncio": frozenset({"sleep"}),
    "datetime": frozenset({"datetime", "date"}),
}
_CLOCK_CALLS = frozenset({"monotonic", "perf_counter", "time", "monotonic_ns", "perf_counter_ns"})
_DELTA_NAMES = re.compile(r"(elapsed|duration|took|delta|wall)", re.IGNORECASE)
_STAMP_NAMES = re.compile(
    r"^(start|started|end|ended|before|after|began|finished|now|then|t\d|t_\w+|\w+_at)$"
)
_TIMEOUT_ERRORS = frozenset({"TimeoutError", "asyncio.TimeoutError", "futures.TimeoutError"})
#: Calls that open a listener: a literal port handed to one is the D9 shape.
_LISTENERS = frozenset(
    {
        "TCPSite",
        "run_app",
        "start_server",
        "create_server",
        "HTTPServer",
        "ThreadingHTTPServer",
        "TCPServer",
        "ThreadingTCPServer",
        "UDPServer",
        "serve",
        "make_server",
    }
)
#: Where a listener call takes its port positionally (0-based).
_LISTENER_PORT_ARG = {"TCPSite": 2, "start_server": 2, "create_server": 2}


def _dotted(node: ast.AST) -> str:
    """``a.b.c`` for a Name/Attribute chain, else ''."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _number(node: ast.AST | None) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            return None
        return float(node.value)
    return None


class _Module:
    """One parsed file, with what the detectors share."""

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        #: local name -> stdlib module, for ``import time as _time`` and the like.
        self.modules: dict[str, str] = {name: name for name in ("time", "asyncio", "datetime")}
        self.parents: dict[ast.AST, ast.AST] = {}
        self.calls: list[ast.Call] = []
        self.asserts: list[ast.Assert] = []
        self.writes: list[ast.AST] = []  # Assign, AugAssign, Delete
        # One walk; every detector reads these lists instead of walking again.
        stack: list[ast.AST] = [tree]
        while stack:
            node = stack.pop()
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
                stack.append(child)
            if isinstance(node, ast.Call):
                self.calls.append(node)
            elif isinstance(node, ast.Assert):
                self.asserts.append(node)
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
                self.writes.append(node)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _CLOCK_ATTRS:
                        self.modules[alias.asname or alias.name] = alias.name

    def stdlib_of(self, node: ast.AST) -> str | None:
        """The stdlib module a Name or Attribute names, through this file's aliases."""
        if isinstance(node, ast.Name):
            return self.modules.get(node.id)
        dotted = _dotted(node)
        if dotted.endswith((".time", ".asyncio", ".datetime")):
            # ``<subject>.time``: the module under test's own binding of the stdlib
            # module, which a test patches to change the clock for the whole worker.
            return dotted.rsplit(".", 1)[1]
        return None

    def enclosing_function(self, node: ast.AST):
        current = self.parents.get(node)
        while current is not None and not isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            current = self.parents.get(current)
        return current

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

    def inside_paused(self, node: ast.AST) -> bool:
        current = self.parents.get(node)
        while current is not None:
            if isinstance(current, (ast.With, ast.AsyncWith)):
                for item in current.items:
                    call = item.context_expr
                    if isinstance(call, ast.Call) and _dotted(call.func).endswith("paused"):
                        return True
            current = self.parents.get(current)
        return False


def _patched_target(node: ast.Call) -> tuple[ast.AST | None, str | None]:
    """``(object, attribute)`` a ``setattr`` / ``patch.object`` call patches."""
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        return node.args[0], str(node.args[1].value)
    return (node.args[0] if node.args else None), None


#: What ``kirocrew.test-stdlib-clock-rebound`` (semgrep/test-determinism.yaml) refuses:
#: ``<x>.setattr(time, ...)`` and ``patch.object(time, ...)`` on the stdlib ``time``
#: module named directly (an ``import time as`` alias included), and a ``"time.<attr>"``
#: target string handed to ``<x>.setattr``, ``patch`` or ``mock.patch``. K3 counts the
#: clock and sleep patches that rule's patterns miss, so no site needs both markers.
_TIME_RULE_STRING_CALLS = frozenset({"patch", "mock.patch"})


def _time_rule_refuses(module: _Module, node: ast.Call, target: ast.AST | None) -> bool:
    name = _dotted(node.func)
    via_setattr = isinstance(node.func, ast.Attribute) and node.func.attr == "setattr"
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        return target.value.startswith("time.") and (via_setattr or name in _TIME_RULE_STRING_CALLS)
    return (
        isinstance(target, ast.Name)
        and module.modules.get(target.id) == "time"
        and (via_setattr or name == "patch.object")
    )


def _k3_stdlib_clock_patch(module: _Module) -> Iterable[ast.AST]:
    """A patch on a stdlib clock or sleep the time rule does not refuse: ``asyncio.sleep``,
    ``datetime.datetime`` / ``date``, the time module reached through another module's
    binding (``<mod>.time``, ``"pkg.mod.time.<attr>"``), or ``mock.patch.object(time, ...)``.
    """
    for node in module.calls:
        name = _dotted(node.func)
        target: ast.AST | None
        if name.endswith(("setattr", "patch.object")) and node.args:
            target, attr = _patched_target(node)
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                stdlib, _, attr = target.value.rpartition(".")
                stdlib = stdlib.rsplit(".", 1)[-1]
            else:
                stdlib = module.stdlib_of(target) if target is not None else None
        elif (
            (name == "patch" or name.endswith(".patch"))
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            target = node.args[0]
            stdlib, _, attr = str(target.value).rpartition(".")
            stdlib = stdlib.rsplit(".", 1)[-1]
        else:
            continue
        if attr in _CLOCK_ATTRS.get(stdlib or "", ()) and not _time_rule_refuses(
            module, node, target
        ):
            yield node


def _restores_sys_modules(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and _dotted(target.value) == "sys.modules":
                    return True
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name.endswith(("setitem", "patch.dict", "delitem")) and node.args:
                if _dotted(node.args[0]) == "sys.modules":
                    return True
    return False


def _k5_sys_modules_eviction(module: _Module) -> Iterable[ast.AST]:
    """``del sys.modules[...]`` / ``sys.modules.pop(...)`` with no restore in the same
    function. ``importlib.reload`` is ``kirocrew.test-in-process-reload``'s, not this."""
    for node in [*module.calls, *module.writes]:
        evicted = False
        if isinstance(node, ast.Delete):
            evicted = any(
                isinstance(t, ast.Subscript) and _dotted(t.value) == "sys.modules"
                for t in node.targets
            )
        elif isinstance(node, ast.Call) and _dotted(node.func) == "sys.modules.pop":
            evicted = True
        if evicted:
            fn = module.enclosing_function(node)
            if fn is None or not _restores_sys_modules(fn):
                yield node


def _k6_raw_environ_write(module: _Module) -> Iterable[ast.AST]:
    for node in module.writes:
        targets: list[ast.AST] = []
        if isinstance(node, (ast.Assign, ast.Delete)):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Subscript) and _dotted(target.value) == "os.environ":
                yield node
                break


def _is_clock_delta(node: ast.AST) -> bool:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
        for side in (node.left, node.right):
            if isinstance(side, ast.Call) and _dotted(side.func).rsplit(".", 1)[-1] in _CLOCK_CALLS:
                return True
        names = [_dotted(side).rsplit(".", 1)[-1] for side in (node.left, node.right)]
        if all(names) and all(_STAMP_NAMES.match(name) for name in names):
            return True
    if isinstance(node, (ast.Name, ast.Attribute)):
        return bool(_DELTA_NAMES.search(_dotted(node)))
    return False


def _k8_duration_bound(module: _Module) -> Iterable[ast.AST]:
    for node in module.asserts:
        if not isinstance(node.test, ast.Compare):
            continue
        compare = node.test
        left = compare.left
        for op, right in zip(compare.ops, compare.comparators):
            if (
                isinstance(op, (ast.Lt, ast.LtE))
                and _is_clock_delta(left)
                and _number(right) is not None
            ):
                yield node
                break
            left = right


def _literal_timeout(call: ast.Call, position: int) -> float | None:
    for keyword in call.keywords:
        if keyword.arg == "timeout":
            return _number(keyword.value)
    if len(call.args) > position:
        return _number(call.args[position])
    return None


def _k9_short_literal_wait(module: _Module) -> Iterable[ast.AST]:
    for node in module.calls:
        name = _dotted(node.func)
        if name.endswith("wait_for"):
            seconds = _literal_timeout(node, 1)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in ("wait", "result", "join"):
            seconds = _literal_timeout(node, 0)
        else:
            continue
        if seconds is None or not 0 < seconds < 2 or module.inside_raises_timeout(node):
            continue
        outer = module.parents.get(node)
        if isinstance(outer, ast.UnaryOp) and isinstance(outer.op, ast.Not):
            continue  # ``assert not event.wait(0.1)``: a short bound weakens a negative, never reds it
        yield node


def _k11_literal_listener_port(module: _Module) -> Iterable[ast.AST]:
    for node in module.calls:
        callee = _dotted(node.func).rsplit(".", 1)[-1]
        if callee == "bind" and node.args and isinstance(node.args[0], ast.Tuple):
            elts = node.args[0].elts
            if len(elts) >= 2 and _number(elts[1]):
                yield node
            continue
        if callee not in _LISTENERS:
            continue
        port = next((kw.value for kw in node.keywords if kw.arg == "port"), None)
        position = _LISTENER_PORT_ARG.get(callee)
        if port is None and position is not None and len(node.args) > position:
            port = node.args[position]
        if port is None and node.args and isinstance(node.args[0], ast.Tuple):
            elts = node.args[0].elts  # HTTPServer(("127.0.0.1", 8080), ...)
            port = elts[1] if len(elts) >= 2 else None
        if _number(port):
            yield node


def _k13_fixed_flush_ceiling(module: _Module) -> Iterable[ast.AST]:
    for node in module.calls:
        if _dotted(node.func).endswith("emit.flush") and _literal_timeout(node, 0) is not None:
            yield node


def _k14_unpaused_crew_log_rmtree(module: _Module, source: str) -> Iterable[ast.AST]:
    for node in module.calls:
        if _dotted(node.func) != "shutil.rmtree":
            continue
        text = ast.get_source_segment(source, node) or ""
        if ("crew_log" in text or "crew-log" in text) and not module.inside_paused(node):
            yield node


#: class -> words one of which every shape of that class contains. A file holding none
#: is not parsed: each set is deliberately broader than its detector, never narrower.
PY_TRIGGERS: dict[str, tuple[str, ...]] = {
    "K3-stdlib-clock-patch": ("setattr", "patch"),
    "K5-sys-modules-eviction": ("modules",),
    "K6-raw-environ-write": ("environ",),
    "K8-duration-upper-bound": ("assert",),
    "K9-short-literal-wait": ("(",),
    "K11-literal-listener-port": ("(",),
    "K13-fixed-flush-ceiling": ("flush",),
    "K14-unpaused-crew-log-rmtree": ("rmtree",),
}

#: class -> detector. K14 also reads the source text.
PY_DETECTORS: dict[str, Callable[..., Iterable[ast.AST]]] = {
    "K3-stdlib-clock-patch": _k3_stdlib_clock_patch,
    "K5-sys-modules-eviction": _k5_sys_modules_eviction,
    "K6-raw-environ-write": _k6_raw_environ_write,
    "K8-duration-upper-bound": _k8_duration_bound,
    "K9-short-literal-wait": _k9_short_literal_wait,
    "K11-literal-listener-port": _k11_literal_listener_port,
    "K13-fixed-flush-ceiling": _k13_fixed_flush_ceiling,
    "K14-unpaused-crew-log-rmtree": _k14_unpaused_crew_log_rmtree,
}


# ── TypeScript / JavaScript detectors (line patterns) ─────────────────

_J1 = re.compile(
    r"new Promise(?:<[^>]*>)?\(\s*\(?\s*(\w+)[^)=]*\)?\s*=>\s*\{?\s*setTimeout\(\s*\1\s*,"
    r"\s*([1-9][\d_]*)"
)
_J3 = re.compile(r"\bDate\.now\(\)|\bnew Date\(\s*\)")
_J4 = re.compile(r"\bif\s*\(\s*!?\s*\(?\s*await\b[^;{]*?\.isVisible\(")
_J5 = re.compile(
    r"expect\([^;]*(?:performance\.now\(\)|Date\.now\(\)|\b\w*(?:elapsed|duration|took)\w*\b)[^;]*\)"
    r"\s*\.\s*toBeLessThan(?:OrEqual)?\(\s*\d",
    re.IGNORECASE,
)
_J6 = re.compile(r"\buseFakeTimers\(")
_STALLS = re.compile(r"\bawait\b|\bwaitFor\b|\bfindBy|\bfindAllBy")


def _j_lines(pattern: re.Pattern[str]) -> Callable[[str, str], list[int]]:
    def detect(rel: str, text: str) -> list[int]:
        return [n for n, line in enumerate(text.splitlines(), 1) if pattern.search(line)]

    return detect


def _j1_electron_promise_sleep(rel: str, text: str) -> list[int]:
    """Promise-sleeps in the Electron tests: ``kirocrew.test-promise-sleep`` reads
    only TypeScript under ``website/src``, ``integration`` and ``playwright``."""
    if not rel.startswith("website/electron/"):
        return []
    return _j_lines(_J1)(rel, text)


def _j3_unpinned_date(rel: str, text: str) -> list[int]:
    if "setSystemTime" in text or "mock.timers" in text:
        return []
    return _j_lines(_J3)(rel, text)


def _j4_visible_branch(rel: str, text: str) -> list[int]:
    if not rel.startswith("website/playwright/"):
        return []
    return _j_lines(_J4)(rel, text)


def _restored_by_a_synchronous_finally(lines: list[str], index: int) -> bool:
    """Whether ``useFakeTimers`` on line *index* opens a ``try`` whose ``finally``
    restores real timers with nothing in between that a fake clock could stall."""
    rest = "\n".join(lines[index : index + 200])
    start = rest.find("try {")
    end = rest.find("finally", start)
    if start < 0 or end < 0:
        return False
    between = rest[:start].split("useFakeTimers", 1)[-1].replace("()", "")
    if between.strip(" \t\n;"):
        return False  # the try is not the next statement
    restore = rest.find("useRealTimers", end)
    return restore >= 0 and not _STALLS.search(rest[start:end])


def _j6_unrestored_fake_timers(rel: str, text: str) -> list[int]:
    if ("afterEach" in text and "useRealTimers" in text) or "onTestFinished" in text:
        return []
    lines = text.splitlines()
    return [
        n
        for n, line in enumerate(lines, 1)
        if _J6.search(line) and not _restored_by_a_synchronous_finally(lines, n - 1)
    ]


JS_DETECTORS: dict[str, Callable[[str, str], list[int]]] = {
    "J1-electron-promise-sleep": _j1_electron_promise_sleep,
    "J3-unpinned-date": _j3_unpinned_date,
    "J4-playwright-visible-branch": _j4_visible_branch,
    "J5-duration-upper-bound": _j_lines(_J5),
    "J6-unrestored-fake-timers": _j6_unrestored_fake_timers,
}


# ── counting ──────────────────────────────────────────────────────────


def _excuse_lines(node: ast.AST) -> range:
    """The lines a ``flake-ok`` marker may sit on to excuse *node*: its own."""
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", start) or start
    return range(start, end + 1)


def _python_findings(source: str) -> Iterable[tuple[str, str, bool]]:
    """``(class, site identity, excused by flake-ok)`` for each finding in a source.

    The identity is the class plus the unparsed text of the enclosing function (the
    finding itself at module level), so a site that MOVES with its function keeps it.
    """
    active = [klass for klass, words in PY_TRIGGERS.items() if any(w in source for w in words)]
    if not active:
        return
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    module = _Module(tree)
    lines = source.splitlines()
    for klass in active:
        detector = PY_DETECTORS[klass]
        found = detector(module, source) if klass.startswith("K14") else detector(module)
        for node in found:
            span = [n for n in _excuse_lines(node) if 0 < n <= len(lines)]
            excused = any(_flake_ok(lines[n - 1]) for n in span)
            yield klass, ast.unparse(module.enclosing_function(node) or node), excused


def _js_findings(rel: str, source: str) -> Iterable[tuple[str, str, bool]]:
    """``(class, the stripped line, excused by flake-ok)`` for each finding in a source."""
    lines = source.splitlines()
    for klass, detector in JS_DETECTORS.items():
        for number in detector(rel, source):
            line = lines[number - 1]
            yield klass, line.strip(), _flake_ok(line)


def _tally(findings: Iterable[tuple[str, str, bool]]) -> tuple[Counter[str], int]:
    counts: Counter[str] = Counter()
    excused = 0
    for klass, _site, ok in findings:
        if ok:
            excused += 1
        else:
            counts[klass] += 1
    return counts, excused


def count_python(source: str) -> tuple[Counter[str], int]:
    """``(class -> count, flake-ok lines)`` for one Python source."""
    return _tally(_python_findings(source))


def count_js(rel: str, source: str) -> tuple[Counter[str], int]:
    return _tally(_js_findings(rel, source))


_Sites = Counter[tuple[str, str]]


def sites(rel: str, source: str) -> tuple[_Sites, _Sites]:
    """``(counted, present)``: ``(class, site identity) -> count`` for one test source,
    without and with the lines a ``flake-ok`` excuses."""
    found = _python_findings(source) if _is_python_test_file(rel) else _js_findings(rel, source)
    counted: _Sites = Counter()
    present: _Sites = Counter()
    for klass, site, ok in found:
        present[(klass, site)] += 1
        if not ok:
            counted[(klass, site)] += 1
    return counted, present


# ── the change in front of the ratchet ────────────────────────────────

#: Env var that forces an explicit base, matching the shared helper.
BASE_ENV = "FLAKE_RATCHET_BASE"


def merge_base(root: Path, *, env: dict[str, str] | None = None) -> tuple[str | None, str]:
    """``(base commit, how it was found)`` for this change against *root*; shared helper.

    Delegates to :func:`ratchet_scope.change_merge_base` so the base precedence
    stays the one every change-shaped gate reads.
    """
    return ratchet_scope.change_merge_base(BASE_ENV, cwd=root, env=env)


def changed_files(root: Path, base: str) -> list[tuple[str | None, str | None]]:
    """``(path at base, path now)`` for each file this change touches against *base*."""
    return ratchet_scope.change_files(base, cwd=root)


def _counted(rel: str) -> bool:
    return _is_python_test_file(rel) or _is_js_test_file(rel)


def _sites_at(root: Path, base: str | None, rel: str | None) -> tuple[_Sites, _Sites]:
    if rel is None or not _counted(rel):
        return Counter(), Counter()
    if base is not None:
        text = ratchet_scope.show_at(base, rel, cwd=root)
    else:
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = None
    return sites(rel, text) if text is not None else (Counter(), Counter())


def _class_total(sites_of_file: _Sites, klass: str) -> int:
    return sum(count for (k, _site), count in sites_of_file.items() if k == klass)


def _moved_into(
    drops: list[int], gone: list[_Sites], arrived: list[_Sites], rises: list[int]
) -> list[int]:
    """How much of each file's rise in one class sites MOVED from other files cover.

    A maximum flow: from each file whose count dropped (up to that drop), along each
    identity that left it, to each file that identity arrived in, and out up to that
    file's rise. A file passes on what it receives, so a chain of moves (A's site to B
    while B's own moves to C) is followed; an identity that left no file, or a file
    whose count did not drop and received nothing (a function edited in place), lends
    nothing. Edmonds-Karp over a graph of a few nodes per changed file.
    """
    files = range(len(drops))
    graph: dict[object, dict[object, int]] = {"source": {}, "sink": {}}

    def edge(head: object, tail: object, capacity: int) -> None:
        graph.setdefault(head, {})
        graph.setdefault(tail, {})
        graph[head][tail] = graph[head].get(tail, 0) + capacity
        graph[tail].setdefault(head, 0)

    for index in files:
        if drops[index] > 0:
            edge("source", ("file", index), drops[index])
        for site, count in sorted(gone[index].items()):
            edge(("file", index), ("site", site), count)
        for site, count in sorted(arrived[index].items()):
            edge(("site", site), ("file", index), count)
        if rises[index] > 0:
            edge(("file", index), "sink", rises[index])
    while True:
        parent: dict[object, object] = {"source": "source"}
        queue = ["source"]
        while queue and "sink" not in parent:
            head = queue.pop(0)
            for tail, room in graph[head].items():
                if room > 0 and tail not in parent:
                    parent[tail] = head
                    queue.append(tail)
        if "sink" not in parent:
            break
        path = ["sink"]
        while path[-1] != "source":
            path.append(parent[path[-1]])
        hops = list(zip(path[1:], path[:-1]))
        pushed = min(graph[head][tail] for head, tail in hops)
        for head, tail in hops:
            graph[head][tail] -= pushed
            graph[tail][head] += pushed
    # What the sink edge still has room for is the part of the rise no move covered.
    return [graph.get(("file", index), {}).get("sink", 0) for index in files]


def change_risen(root: Path, base: str) -> dict[tuple[str, str], tuple[int, int]]:
    """``(class, path) -> (count now, count allowed)`` for every rise in this change.

    Each file is held to its own count per class at *base*; a new file starts at 0.
    A rise is forgiven only for a site that MOVED: the same (class, identity), where
    the identity is the enclosing function's unparsed Python text or the stripped
    JavaScript line, that left another file in the same change (see :func:`_moved_into`).
    A site a ``flake-ok`` still excuses has not left.
    """
    paths: list[str | None] = []
    befores: list[_Sites] = []
    nows: list[_Sites] = []
    gones: list[_Sites] = []
    for old, new in sorted(changed_files(root, base), key=lambda pair: str(pair[1] or pair[0])):
        before, _before_present = _sites_at(root, base, old)
        now, now_present = _sites_at(root, None, new)
        paths.append(new)
        befores.append(before)
        nows.append(now)
        gones.append(before - now_present)
    risen: dict[tuple[str, str], tuple[int, int]] = {}
    for klass in sorted({klass for now in nows for klass, _site in now}):
        was = [_class_total(before, klass) for before in befores]
        count = [_class_total(now, klass) for now in nows]

        def of_class(sites_of_file: _Sites) -> _Sites:
            return Counter({key: n for key, n in sites_of_file.items() if key[0] == klass})

        unforgiven = _moved_into(
            [max(0, w - c) for w, c in zip(was, count)],
            [of_class(gone) for gone in gones],
            [of_class(now - before) for before, now in zip(befores, nows)],
            [max(0, c - w) if path is not None else 0 for path, w, c in zip(paths, was, count)],
        )
        for path, c, excess in zip(paths, count, unforgiven):
            if path is not None and excess > 0:
                risen[(klass, path)] = (c, c - excess)
    return risen


# ── the ratchet ───────────────────────────────────────────────────────


def test_no_changed_file_gains_a_flaky_shape() -> None:
    root = repo_root()
    base, how = merge_base(root)
    if base is None:
        message = f"flake-pattern ratchet compared nothing: {how}"
        if os.environ.get("GITHUB_ACTIONS") == "true":
            _write_job_summary(f"**Warning:** {message}.")
        warnings.warn(message, UserWarning, stacklevel=1)
        return
    risen = change_risen(root, base)
    print(f"compared with {base[:12]}, {how}")
    detail = "\n".join(
        f"  {path}: {klass} {now} > {allowed}"
        for (klass, path), (now, allowed) in sorted(risen.items())
    )
    assert not risen, (
        "this change adds a shape the Determinism contract rules out "
        "(docs/system-specs/common/testing-conventions.md#determinism-contract-read-this-first); "
        f"each count is this file's against {base[:12]}:\n{detail}\nFix the new site, or carry "
        "`# flake-ok: <reason>` on its line when the shape is deliberate."
    )


def _write_job_summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


# ── the detectors find what they say, and only that ───────────────────

#: class -> (snippets the detector must count once each, a near miss it must not count).
PY_CASES: dict[str, tuple[tuple[str, ...], str]] = {
    "K3-stdlib-clock-patch": (
        (
            "import asyncio\ndef test_x(monkeypatch):\n    monkeypatch.setattr(asyncio, 'sleep', f)\n",
            "def test_x():\n    with patch('kiro_crew.providers.acp.asyncio.sleep', new=f):\n"
            "        pass\n",
            "import datetime\ndef test_x(monkeypatch):\n"
            "    monkeypatch.setattr(datetime, 'datetime', Frozen)\n",
            "def test_x(monkeypatch):\n    monkeypatch.setattr(subject.time, 'monotonic', f)\n",
            "def test_x(monkeypatch):\n"
            "    monkeypatch.setattr('kiro_crew.acp.client.time.monotonic', f)\n",
            "import time as _time\nfrom unittest import mock\ndef test_x():\n"
            "    with mock.patch.object(_time, 'time', f):\n        pass\n",
        ),
        # The right seam, a non-clock attribute, and the shapes the time rule refuses.
        "import os\nimport time\nimport time as _t\ndef test_x(monkeypatch):\n"
        "    monkeypatch.setattr(subject, 'time', fake)\n    monkeypatch.setattr(os, 'getcwd', f)\n"
        "    monkeypatch.setattr('time.tzname', t)\n    monkeypatch.setattr(time, 'monotonic', f)\n"
        "    monkeypatch.setattr(_t, 'sleep', f)\n    monkeypatch.setattr('time.time', f)\n"
        "    with patch('time.sleep'), patch.object(time, 'monotonic', f):\n        pass\n",
    ),
    "K5-sys-modules-eviction": (
        (
            "import sys\ndef test_x():\n    del sys.modules['m']\n",
            "import sys\ndef test_x():\n    sys.modules.pop('m', None)\n",
        ),
        # A restore in the same function, and the reload the reload rule refuses.
        "import importlib\nimport sys\ndef test_x(monkeypatch):\n    del sys.modules['m']\n"
        "    sys.modules['m'] = saved\ndef test_y():\n    importlib.reload(mod)\n",
    ),
    "K6-raw-environ-write": (
        ("import os\ndef test_x():\n    os.environ['A'] = '1'\n",),
        "import os\ndef test_x(monkeypatch):\n    monkeypatch.setenv('A', '1')\n    x = os.environ['A']\n",
    ),
    "K8-duration-upper-bound": (
        (
            "import time\ndef test_x():\n    t = time.monotonic()\n    run()\n"
            "    assert time.monotonic() - t < 1.0\n",
            "def test_x():\n    assert (\n        elapsed < 5.0\n    ), 'took too long'\n",
            "def test_x():\n    assert 0.15 < elapsed < 1.0\n",
            "def test_x():\n    assert after - before < 0.05\n",
        ),
        "import time\ndef test_x():\n    t = time.monotonic()\n    run()\n"
        "    assert time.monotonic() - t < LOST_RUN_SECS\n    assert elapsed > 0.1\n",
    ),
    "K9-short-literal-wait": (
        (
            "def test_x():\n    thread.join(0.5)\n",
            "async def test_x():\n    await asyncio.wait_for(f, 1)\n",
        ),
        "import pytest\ndef test_x():\n    thread.join(30)\n    with pytest.raises(TimeoutError):\n"
        "        fut.result(timeout=0.01)\n    assert not stopped.wait(0.1)\n",
    ),
    "K11-literal-listener-port": (
        (
            "def test_x():\n    web.TCPSite(runner, '127.0.0.1', 8765)\n",
            "def test_x():\n    s.bind(('127.0.0.1', 9000))\n",
            "def test_x():\n    HTTPServer(('127.0.0.1', 8080), Handler)\n",
        ),
        "def test_x():\n    web.TCPSite(runner, '127.0.0.1', 0)\n    s.bind(('127.0.0.1', 0))\n"
        "    record = AppProcess(port=9137)\n",
    ),
    "K13-fixed-flush-ceiling": (
        ("def test_x():\n    assert emit.flush(timeout=5.0)\n",),
        "def test_x():\n    assert emit.flush()\n",
    ),
    "K14-unpaused-crew-log-rmtree": (
        (
            "import shutil\ndef test_x():\n    shutil.rmtree(crew_log_path('session', sid).parent)\n",
        ),
        "import shutil\ndef test_x():\n    with eager.paused():\n"
        "        shutil.rmtree(crew_log_path('session', sid).parent)\n",
    ),
}

JS_CASES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "J1-electron-promise-sleep": (
        "website/electron/test/a.test.js",
        (
            "await new Promise((r) => setTimeout(r, 50));\n",
            "await new Promise((r) => { setTimeout(r, 150); });\n",
        ),
        "await new Promise((r) => setTimeout(r, 0));\n",
    ),
    "J3-unpinned-date": (
        "website/src/a.test.tsx",
        ("const now = Date.now();\n",),
        "vi.setSystemTime(new Date('2026-01-01'));\nconst now = Date.now();\n",
    ),
    "J4-playwright-visible-branch": (
        "website/playwright/a.spec.ts",
        ("if (await page.getByRole('dialog').isVisible()) {\n",),
        "await expect(page.getByRole('dialog')).toBeVisible();\n",
    ),
    "J5-duration-upper-bound": (
        "website/src/a.test.ts",
        ("expect(performance.now() - start).toBeLessThan(50);\n",),
        "expect(performance.now() - start).toBeLessThan(BUDGET_MS);\n",
    ),
    "J6-unrestored-fake-timers": (
        "website/src/a.test.ts",
        ("it('x', () => {\n  vi.useFakeTimers();\n});\n",),
        "it('x', () => {\n  vi.useFakeTimers();\n  try {\n    vi.advanceTimersByTime(5);\n"
        "  } finally {\n    vi.useRealTimers();\n  }\n});\n",
    ),
}


@pytest.mark.parametrize("klass", sorted(PY_CASES))
def test_each_python_detector_counts_its_shape_and_not_the_near_miss(klass: str) -> None:
    positives, negative = PY_CASES[klass]
    for positive in positives:
        assert count_python(positive)[0][klass] == 1, f"{klass} missed its own shape"
        assert any(w in positive for w in PY_TRIGGERS[klass]), f"{klass}'s trigger skips it"
    assert any(w in negative for w in PY_TRIGGERS[klass]), f"{klass}'s near miss is never read"
    assert count_python(negative)[0][klass] == 0, f"{klass} counted a near miss"


@pytest.mark.parametrize("klass", sorted(JS_CASES))
def test_each_js_detector_counts_its_shape_and_not_the_near_miss(klass: str) -> None:
    rel, positives, negative = JS_CASES[klass]
    for positive in positives:
        assert count_js(rel, positive)[0][klass] == 1, f"{klass} missed its own shape"
    assert count_js(rel, negative)[0][klass] == 0, f"{klass} counted a near miss"


def test_every_detector_has_cases() -> None:
    assert set(PY_CASES) == set(PY_DETECTORS) == set(PY_TRIGGERS)
    assert set(JS_CASES) == set(JS_DETECTORS)


@pytest.mark.parametrize("klass", sorted(PY_CASES))
def test_a_detector_that_finds_nothing_fails_its_positive_case(klass, monkeypatch) -> None:
    """Mutation proof: the positive case is what catches a detector gone blind."""
    monkeypatch.setitem(PY_DETECTORS, klass, lambda *_args: ())
    with pytest.raises(AssertionError, match="missed its own shape"):
        test_each_python_detector_counts_its_shape_and_not_the_near_miss(klass)


@pytest.mark.parametrize("klass", sorted(JS_CASES))
def test_a_js_detector_that_finds_nothing_fails_its_positive_case(klass, monkeypatch) -> None:
    monkeypatch.setitem(JS_DETECTORS, klass, lambda *_args: [])
    with pytest.raises(AssertionError, match="missed its own shape"):
        test_each_js_detector_counts_its_shape_and_not_the_near_miss(klass)


def test_a_flake_ok_line_is_excused_only_with_a_reason() -> None:
    shape = "import os\ndef test_x():\n    os.environ['A'] = '1'{}\n"
    counts, excused = count_python(shape.format("  # flake-ok: the variable is the subject"))
    assert counts["K6-raw-environ-write"] == 0 and excused == 1
    for unreasoned in (
        "  # flake-ok: short",
        "  # flake-ok: ..........",
        "  # flake-ok: 1234567890",
    ):
        counts, excused = count_python(shape.format(unreasoned))
        assert counts["K6-raw-environ-write"] == 1 and excused == 0, unreasoned
    js = "const stamp = Date.now(); // flake-ok: only logged, never compared\n"
    assert count_js("website/src/a.test.ts", js) == (Counter(), 1)


#: class -> the semgrep rule (semgrep/test-determinism.yaml) whose shape family it shares.
_RULE_OF = {
    "K3-stdlib-clock-patch": "kirocrew.test-stdlib-clock-rebound",
    "K5-sys-modules-eviction": "kirocrew.test-in-process-reload",
}
_SEMGREP_MARK = re.compile(r"^\s*# (ruleid|ok): (\S+)\s*$")


def test_a_class_counts_only_what_its_semgrep_rule_misses() -> None:
    """The rule's own fixture, which the SAST job's ``semgrep --test`` checks both ways,
    shows the split: no ``ruleid:`` line is counted (so no site needs both markers), and
    every line the class counts there is an ``ok:`` line the rule is proven to miss."""
    text = (repo_root() / "semgrep-tests" / "test-determinism.py").read_text(encoding="utf-8")
    lines = text.splitlines()
    marks: dict[str, dict[int, str]] = {}
    for index, line in enumerate(lines[:-1]):
        match = _SEMGREP_MARK.match(line)
        if match:
            marks.setdefault(match.group(2), {})[index + 2] = match.group(1)
    module = _Module(ast.parse(text))
    for klass, rule in _RULE_OF.items():
        counted = {node.lineno for node in PY_DETECTORS[klass](module)}
        refused = {n for n, kind in marks.get(rule, {}).items() if kind == "ruleid"}
        missed = {n for n, kind in marks.get(rule, {}).items() if kind == "ok"}
        assert refused, f"{rule} has no refused case in its fixture"
        assert counted and counted <= missed, (klass, sorted(counted), sorted(missed))


def test_the_electron_promise_sleep_is_the_only_one_counted() -> None:
    sleep = "await new Promise((r) => setTimeout(r, 50));\n"
    assert count_js("website/electron/test/a.test.js", sleep)[0]["J1-electron-promise-sleep"] == 1
    for rel in (
        "website/src/a.test.tsx",
        "website/integration/a.test.ts",
        "website/playwright/a.spec.ts",
    ):
        assert count_js(rel, sleep)[0]["J1-electron-promise-sleep"] == 0, rel


def test_the_scan_covers_the_test_trees_and_nothing_else() -> None:
    assert _is_python_test_file("test/test_a.py")
    assert _is_python_test_file("src/kiro_crew/apps/builtins/x/tests/test_a.py")
    assert _is_python_test_file(
        "src/kiro_crew/apps/builtins/x/crew/runtime/container_tests/test_a.py"
    )
    assert _is_python_test_file("conftest.py")
    assert not _is_python_test_file("src/kiro_crew/session.py")
    assert _is_js_test_file("website/src/pages/a.test.tsx")
    assert _is_js_test_file("website/playwright/a.spec.ts")
    assert _is_js_test_file("website/electron/test/a.test.js")
    assert not _is_js_test_file("website/src/pages/a.tsx")
    assert not _is_js_test_file("website/node_modules/x/test/a.test.js")


def _run_git(root: Path, *args: str) -> str:
    # The scrubbed fixture env: no inherited location, template hooks or identity.
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=_fixture_git_env(),
    ).stdout.strip()


def _drop_ambient_git(monkeypatch) -> None:
    """``merge_base`` and ``change_risen`` run git with the ambient env: drop the
    location family an exported ``GIT_DIR`` / ``GIT_INDEX_FILE`` would redirect, and
    the ``git -c`` options a hook that ran the suite exports."""
    for name in (*_GIT_LOCATION_VARS, *_GIT_COMMAND_LINE_CONFIG):
        monkeypatch.delenv(name, raising=False)


_SITE = "def test_{name}():\n    os.environ['A'] = '1'\n    assert done()\n"


def _sites(*names: str) -> str:
    return "import os\n" + "".join(_SITE.format(name=name) for name in names)


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A repo whose ``main`` holds a file with one K6 site, checked out on a branch."""
    _drop_ambient_git(monkeypatch)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GITHUB_BASE_REF", "GITHUB_EVENT_NAME", BASE_ENV):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    (root / "test").mkdir(parents=True)
    (root / "test" / "test_a.py").write_text(_sites("a"), encoding="utf-8")
    (root / "test" / "test_b.py").write_text("def test_b():\n    assert True\n", encoding="utf-8")
    _run_git(root, "init", "-q")
    _run_git(root, "checkout", "-q", "-b", "main")
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "-q", "-m", "base")
    _run_git(root, "checkout", "-q", "-b", "change")
    return root


def test_the_git_fixture_ignores_ambient_git_state(request, monkeypatch, tmp_path) -> None:
    assert_fixture_ignores_ambient_git(request, monkeypatch, tmp_path, "git_repo")


def test_the_merge_base_is_found_against_the_local_base_branch(git_repo) -> None:
    base, how = merge_base(git_repo)
    assert base == _run_git(git_repo, "rev-parse", "main") and "main" in how


def test_a_pull_request_merge_commit_is_measured_against_its_first_parent(
    git_repo, monkeypatch
) -> None:
    (git_repo / "test" / "test_b.py").write_text(_sites("b"), encoding="utf-8")
    _run_git(git_repo, "commit", "-q", "-am", "change")
    before = _run_git(git_repo, "rev-parse", "main")
    _run_git(git_repo, "checkout", "-q", "main")
    _run_git(git_repo, "merge", "-q", "--no-ff", "-m", "merge", "change")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    base, how = merge_base(git_repo)
    assert base == before and "first parent" in how
    assert change_risen(git_repo, base) == {("K6-raw-environ-write", "test/test_b.py"): (1, 0)}


def test_a_site_already_on_the_base_does_not_red_a_change_to_another_file(git_repo) -> None:
    (git_repo / "test" / "test_b.py").write_text("def test_b():\n    assert 1\n", encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {}


def test_a_new_site_in_a_changed_file_is_named(git_repo, monkeypatch) -> None:
    import test_flake_pattern_ratchet as module

    (git_repo / "test" / "test_b.py").write_text(_sites("b"), encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {
        ("K6-raw-environ-write", "test/test_b.py"): (1, 0)
    }
    monkeypatch.setattr(module, "repo_root", lambda: git_repo)
    with pytest.raises(AssertionError, match=r"test/test_b\.py: K6-raw-environ-write 1 > 0"):
        module.test_no_changed_file_gains_a_flaky_shape()


def test_a_renamed_or_deleted_file_keeps_the_change_green(git_repo) -> None:
    _run_git(git_repo, "mv", "test/test_a.py", "test/test_c.py")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {}
    _run_git(git_repo, "rm", "-q", "-f", "test/test_c.py")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {}


def test_sites_moved_into_a_new_file_stay_green_up_to_what_left(git_repo) -> None:
    (git_repo / "test" / "test_a.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    (git_repo / "test" / "test_part.py").write_text(_sites("a"), encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {}
    (git_repo / "test" / "test_part.py").write_text(_sites("a", "extra"), encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {
        ("K6-raw-environ-write", "test/test_part.py"): (2, 1)
    }


def test_an_unreadable_merge_base_warns_and_compares_nothing(tmp_path, monkeypatch) -> None:
    import test_flake_pattern_ratchet as module

    _drop_ambient_git(monkeypatch)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    for name in (BASE_ENV, "GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(module, "repo_root", lambda: tmp_path)
    assert merge_base(tmp_path)[0] is None
    with pytest.warns(UserWarning, match="compared nothing"):
        module.test_no_changed_file_gains_a_flaky_shape()


def test_the_ci_warning_goes_to_an_isolated_job_summary(tmp_path, monkeypatch) -> None:
    import test_flake_pattern_ratchet as module

    _drop_ambient_git(monkeypatch)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    monkeypatch.delenv(BASE_ENV, raising=False)
    monkeypatch.setattr(module, "repo_root", lambda: tmp_path)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with pytest.warns(UserWarning, match="compared nothing"):
        module.test_no_changed_file_gains_a_flaky_shape()
    assert "compared nothing" in summary.read_text(encoding="utf-8")


def test_a_forks_stale_origin_loses_to_a_newer_upstream(git_repo) -> None:
    old_main = _run_git(git_repo, "rev-parse", "main")
    _run_git(git_repo, "checkout", "-q", "main")
    (git_repo / "test" / "test_merged.py").write_text(_sites("merged"), encoding="utf-8")
    _run_git(git_repo, "add", "-A")
    _run_git(git_repo, "commit", "-q", "-m", "merged upstream")
    new_main = _run_git(git_repo, "rev-parse", "main")
    _run_git(git_repo, "update-ref", "refs/remotes/origin/main", old_main)
    _run_git(git_repo, "update-ref", "refs/remotes/upstream/main", new_main)
    _run_git(git_repo, "checkout", "-q", "-b", "fork-change")
    base, how = merge_base(git_repo)
    assert base == new_main and "upstream/main" in how
    assert change_risen(git_repo, base) == {}  # the merged site is on the base, not the change


def test_an_explicit_base_wins_over_everything(git_repo, monkeypatch) -> None:
    base = _run_git(git_repo, "rev-parse", "main")
    (git_repo / "test" / "test_b.py").write_text(_sites("b"), encoding="utf-8")
    _run_git(git_repo, "commit", "-q", "-am", "change")
    monkeypatch.setenv(BASE_ENV, "change")
    assert merge_base(git_repo) == (
        _run_git(git_repo, "rev-parse", "change"),
        f"the merge-base with change ({BASE_ENV})",
    )
    monkeypatch.setenv(BASE_ENV, base)
    assert merge_base(git_repo)[0] == base
    monkeypatch.setenv(BASE_ENV, "no-such-ref")
    assert merge_base(git_repo)[0] is None


def test_a_push_is_measured_against_its_first_parent(git_repo, monkeypatch) -> None:
    before = _run_git(git_repo, "rev-parse", "HEAD")
    (git_repo / "test" / "test_b.py").write_text(_sites("b"), encoding="utf-8")
    _run_git(git_repo, "commit", "-q", "-am", "pushed")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    base, how = merge_base(git_repo)
    assert base == before and "push" in how


def test_a_fixed_site_does_not_forgive_a_different_new_one(git_repo) -> None:
    (git_repo / "test" / "test_a.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    (git_repo / "test" / "test_new.py").write_text(_sites("new"), encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {
        ("K6-raw-environ-write", "test/test_new.py"): (1, 0)
    }


def test_an_edited_function_lends_its_old_text_to_no_new_file(git_repo) -> None:
    # test_a keeps its one site but its text changes; a new file carries the OLD text.
    edited = _sites("a").replace("assert done()", "assert done(1)")
    (git_repo / "test" / "test_a.py").write_text(edited, encoding="utf-8")
    (git_repo / "test" / "test_copy.py").write_text(_sites("a"), encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {
        ("K6-raw-environ-write", "test/test_copy.py"): (1, 0)
    }


def test_a_move_beside_an_edit_in_the_same_file_stays_green(git_repo) -> None:
    # test_a.py: test_a is edited in place, test_b moves verbatim to test_y.py.
    (git_repo / "test" / "test_a.py").write_text(_sites("a", "b"), encoding="utf-8")
    _run_git(git_repo, "commit", "-q", "-am", "two sites")
    _run_git(git_repo, "branch", "-f", "main", "HEAD")
    edited = _sites("a").replace("assert done()", "assert done(1)")
    (git_repo / "test" / "test_a.py").write_text(edited, encoding="utf-8")
    (git_repo / "test" / "test_y.py").write_text(_sites("b"), encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {}


def test_a_file_whose_count_dropped_lends_nothing_to_a_copy_of_its_new_text(git_repo) -> None:
    (git_repo / "test" / "test_a.py").write_text(_sites("a", "b"), encoding="utf-8")
    _run_git(git_repo, "commit", "-q", "-am", "two sites")
    _run_git(git_repo, "branch", "-f", "main", "HEAD")
    edited = _sites("a").replace("assert done()", "assert done(1)")
    (git_repo / "test" / "test_a.py").write_text(edited, encoding="utf-8")
    for name in ("test_w.py", "test_y.py", "test_z.py"):
        (git_repo / "test" / name).write_text(edited, encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {
        ("K6-raw-environ-write", f"test/{name}"): (1, 0)
        for name in ("test_w.py", "test_y.py", "test_z.py")
    }


def test_a_chain_of_moves_stays_green(git_repo) -> None:
    # test_a's site moves into test_b while test_b's own site moves on to test_c.
    (git_repo / "test" / "test_b.py").write_text(_sites("b"), encoding="utf-8")
    _run_git(git_repo, "commit", "-q", "-am", "a site in each")
    _run_git(git_repo, "branch", "-f", "main", "HEAD")
    (git_repo / "test" / "test_a.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    (git_repo / "test" / "test_b.py").write_text(_sites("a"), encoding="utf-8")
    (git_repo / "test" / "test_c.py").write_text(_sites("b"), encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {}


@pytest.mark.parametrize("donor", ["test_0.py", "test_d.py"])
def test_the_verdict_does_not_depend_on_which_file_sorts_first(git_repo, donor) -> None:
    # test_a holds a and b; the donor holds another a. a is edited inside test_a, the
    # donor's a moves to test_b and test_a's b moves to test_c: every rise is a move.
    (git_repo / "test" / "test_a.py").write_text(_sites("a", "b"), encoding="utf-8")
    (git_repo / "test" / donor).write_text(_sites("a"), encoding="utf-8")
    _run_git(git_repo, "add", "-A")
    _run_git(git_repo, "commit", "-q", "-m", "two files")
    _run_git(git_repo, "branch", "-f", "main", "HEAD")
    edited = _sites("a").replace("assert done()", "assert done(1)")
    (git_repo / "test" / "test_a.py").write_text(edited, encoding="utf-8")
    (git_repo / "test" / donor).write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    (git_repo / "test" / "test_b.py").write_text(_sites("a"), encoding="utf-8")
    (git_repo / "test" / "test_c.py").write_text(_sites("b"), encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {}


def test_a_site_a_flake_ok_now_excuses_has_not_left(git_repo) -> None:
    marked = _sites("a").replace("= '1'\n", "= '1'  # flake-ok: deliberate raw write here\n")
    (git_repo / "test" / "test_a.py").write_text(marked, encoding="utf-8")
    (git_repo / "test" / "test_copy.py").write_text(_sites("a"), encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {
        ("K6-raw-environ-write", "test/test_copy.py"): (1, 0)
    }


def test_a_site_moved_between_existing_files_stays_green(git_repo) -> None:
    (git_repo / "test" / "test_a.py").write_text(
        "def test_other():\n    assert 1\n", encoding="utf-8"
    )
    moved = "def test_b():\n    assert True\n" + _sites("a").replace("import os\n", "")
    (git_repo / "test" / "test_b.py").write_text("import os\n" + moved, encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {}


def main() -> int:
    """Print current counts per file for burn-down review; no gating."""
    import json

    rows: dict[str, dict[str, int]] = {}
    for path in repo_files():
        rel = path.relative_to(repo_root()).as_posix()
        if not _counted(rel):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        counts, _ok = count_python(source) if _is_python_test_file(rel) else count_js(rel, source)
        for klass, count in counts.items():
            if count:
                rows.setdefault(rel, {})[klass] = count
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
