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

Two modes:

On a **pull request** (when ``GITHUB_BASE_REF`` is set by GitHub Actions):
each file the PR touches is held to its count at the PR's merge-base, and
also to its committed baseline entry.  A PR that adds a site or forgets to
update baseline.txt (e.g. after a rename) is red.  Files the PR does not
touch are not judged: drift in untouched files is caught by the
committed-baseline check on the next main push or merge-queue run.

On **main** (push / merge_group, or ``GITHUB_BASE_REF`` absent): the
committed per-file baseline is the catcher of last resort.  Hard red so
regressions are named immediately; the fix is a rebaseline PR.

Why a committed baseline AT ALL: ``test_flake_pattern_ratchet.py`` judges a change
against its merge-base, which CI's depth-1 checkout cannot read, so it is a local
pre-push gate, and its "short" is under two seconds. This gate holds every file to
a number and names the file whose count rose. Lowering the thousands already here
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
import importlib.util
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_ratchet_scope import _drop_ambient_git_location, _fixture_git_env

# ratchet_scope is a standalone script; load it the same way
# test_flake_pattern_ratchet.py does.
_RATCHET_SCOPE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ratchet_scope.py"
_RS_SPEC = importlib.util.spec_from_file_location("ratchet_scope", _RATCHET_SCOPE_SCRIPT)
assert _RS_SPEC and _RS_SPEC.loader
ratchet_scope = importlib.util.module_from_spec(_RS_SPEC)
_RS_SPEC.loader.exec_module(ratchet_scope)

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
    "Not your sites -- main drifted after a same-file race between two PRs that "
    "both passed PR mode (neither added a net site to files it touched at its "
    "own base)? Then the committed baseline is behind main: raise exactly those "
    "lines in a baseline-only pull request (`--write-baseline` will not do it; "
    "it only lowers), then rebase this PR on it. "
    "See issue #20155 for the residual drift-escape-path investigation."
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
    try:
        return _parse_baseline_src(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        # Re-raise naming the file so the error message matches the old format.
        raise ValueError(str(exc).replace("baseline:", f"{path.name}:")) from exc


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


# ── PR-mode: judge only touched files against their count at the PR base ────


def _pr_mode_base(
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    """The commit this PR is judged against, and how it was found.

    Calls ``ratchet_scope.change_merge_base()`` when ``GITHUB_BASE_REF`` is
    set (CI pull_request event).  ``GITHUB_BASE_REF`` is set by GitHub Actions
    only on pull_request events, so push and merge_group runs fall through to
    the committed baseline path automatically -- no bespoke env var needed.

    *cwd* and *env* exist so tests can run against a scratch repository.
    """
    environ = os.environ if env is None else env
    if not environ.get("GITHUB_BASE_REF", "").strip():
        return None, "GITHUB_BASE_REF not set -- committed-baseline mode"
    # On CI PR, change_merge_base detects the merge commit's first parent and
    # returns it as the base.  After fetch-depth: 2 HEAD^1 is already present.
    return ratchet_scope.change_merge_base(cwd=cwd, env=environ)


def _pr_mode_violations(
    base: str,
    *,
    cwd: Path | None = None,
    root: Path | None = None,
) -> dict[str, tuple[int, int, list[Site]]] | None:
    """Files the PR touches that gained short waits relative to *base*.

    Returns ``{rel: (base_count, now_count, sites_now)}`` for files whose
    count rose.  Files the PR did not touch are not judged.  ``rel`` is
    relative to the test root (``root`` or :data:`TEST_ROOT`).

    *cwd* and *root* exist so tests can point the function at a scratch
    git repository; both default to the real repo root / test root.

    Returns ``None`` when the git diff is unavailable; the caller then
    falls through to committed-baseline mode.  (An empty dict means the
    diff ran but found no violations -- that is a clean pass, not a
    fallback.)
    """
    test_root = root if root is not None else TEST_ROOT
    try:
        pairs = ratchet_scope.change_files(base, cwd=cwd)
    except RuntimeError:
        # Diff unavailable (corrupt index, missing objects) -- return None so
        # the caller falls through to committed-baseline mode rather than
        # silently passing the gate with an empty violation set.
        return None

    # Read committed baseline once.  None means no baseline file exists in
    # this context (e.g. a scratch test repo); in that case the committed-
    # baseline check is skipped.  Non-None means the file exists and its
    # entries constrain every touched file (catches renames without updates).
    _bl = (test_root or TEST_ROOT) / BASELINE_FILE.name
    _committed: dict[str, int] | None = read_baseline(_bl) if _bl.exists() else None

    risen: dict[str, tuple[int, int, list[Site]]] = {}
    for base_path, now_path in pairs:
        if now_path is None:  # deleted file -- nothing to count
            continue
        if not now_path.startswith("test/") or not now_path.endswith(".py"):
            continue
        if Path(now_path).name in EXCLUDED:
            continue

        rel = now_path[len("test/") :]  # relative to test root

        # Count at base.
        # For existing files: read the source at base.
        # For new files (no base_path): use the PR's committed baseline
        # entry as the ceiling.  This lets a file reorganisation (split,
        # copy) pass when the author declares the new file's site count
        # in baseline.txt -- without that entry, any sites in a new file
        # are still violations.
        base_count = 0
        if base_path is not None and base_path.startswith("test/"):
            base_source = ratchet_scope.show_at(base, base_path, cwd=cwd)
            if base_source is not None:
                base_rel = base_path[len("test/") :]
                base_count = len(sites_in(base_source, base_rel))
        else:
            # New file: honour a baseline entry the PR ships for it.
            # When test_root is set use the baseline from that root.
            _bl = (test_root or TEST_ROOT) / BASELINE_FILE.name
            base_count = (read_baseline(_bl) if _bl.exists() else {}).get(rel, 0)

        # Count now (read from the working tree)
        now_abs = test_root / rel
        if not now_abs.exists():
            continue
        try:
            now_source = now_abs.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        now_sites = sites_in(now_source, rel)
        now_count = len(now_sites)

        if now_count > base_count:
            # PR added new sites (primary check).
            risen[rel] = (base_count, now_count, now_sites)
        elif _committed is not None:
            # Also verify the committed baseline covers this file's count.
            # Uses the max of the new and old (rename source) baseline entries
            # so a plain rename without touching baseline.txt is not flagged.
            base_rel = (
                base_path[len("test/") :]
                if base_path is not None and base_path.startswith("test/")
                else None
            )
            committed_limit = max(
                _committed.get(rel, 0),
                _committed.get(base_rel, 0) if base_rel else 0,
            )
            if now_count > committed_limit:
                # Committed baseline is too low (rename removed entry, no new one).
                risen[rel] = (committed_limit, now_count, now_sites)

    return risen


def _main_mode_violations(
    baseline: dict[str, int], counts: "Counter[str]"
) -> dict[str, tuple[int, int]]:
    """Files whose short-wait count exceeds the committed baseline.

    Extracted from the gate so both the gate and its unit tests call the
    same logic rather than re-implementing the comprehension separately.
    """
    return {rel: (baseline.get(rel, 0), n) for rel, n in counts.items() if n > baseline.get(rel, 0)}


def _parse_baseline_src(src: str) -> dict[str, int]:
    """Parse baseline file text and return ``{rel: count}``.

    Shared by :func:`read_baseline` and :func:`_pr_mode_baseline_changed_entries`
    so the two never diverge.  Malformed lines raise ``ValueError``.
    """
    out: dict[str, int] = {}
    for number, raw in enumerate(src.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        count, _, rel = line.partition(" ")
        if not count.isdigit() or not rel or rel in out:
            raise ValueError(f"baseline:{number}: expected '<count> <path>', got {raw!r}")
        out[rel] = int(count)
    return out


def _pr_mode_baseline_changed_entries(
    base: str,
    *,
    cwd: Path | None = None,
    baseline_file: Path | None = None,
    test_root: Path | None = None,
) -> dict[str, tuple[str, int, int]]:
    """Validate BASELINE_FILE edits in this PR against lowered_baseline semantics.

    Returns ``{rel: (kind, a, b)}`` for every violation:
    - ``kind='below_real'``: an entry *a* was set below the real count *b*
      (this would red main on the next push).
    - ``kind='deleted'``: an entry was removed while the file still has sites.

    *cwd*, *baseline_file* and *test_root* are for test use; they default
    to the real repo root, :data:`BASELINE_FILE` and :data:`TEST_ROOT`.

    Empty dict when the baseline file was not changed in this PR.
    """
    try:
        pairs = ratchet_scope.change_files(base, cwd=cwd)
    except RuntimeError:
        return {}

    actual_baseline = baseline_file if baseline_file is not None else BASELINE_FILE
    actual_root = test_root if test_root is not None else TEST_ROOT
    rel_baseline = actual_baseline.relative_to(actual_root.parent).as_posix()
    touched_now = {now for _, now in pairs if now is not None}
    if rel_baseline not in touched_now:
        return {}  # baseline file not changed in this PR

    # Parse the old baseline at base via the shared helper.
    old_baseline: dict[str, int] = {}
    old_src = ratchet_scope.show_at(base, rel_baseline, cwd=cwd)
    if old_src is not None:
        try:
            old_baseline = _parse_baseline_src(old_src)
        except ValueError:
            old_baseline = {}

    new_baseline = read_baseline(actual_baseline)
    counts = _counts(scan(actual_root))
    violations: dict[str, tuple[str, int, int]] = {}

    # 1. Entries this PR CHANGED or ADDED whose new value is below the real
    # count.  Unchanged entries are skipped (their drift predates this PR);
    # new entries (absent from old_baseline) are treated as raised from 0
    # and validated so a hand-written under-count addition is caught.
    for rel, new_val in new_baseline.items():
        old_val = old_baseline.get(rel, 0)  # absent in old = effectively 0
        if new_val == old_val:
            continue  # unchanged; any drift there predates this PR
        real = counts.get(rel, 0)
        if real > new_val:
            violations[rel] = ("below_real", new_val, real)

    # 2. Entries this PR DELETED while the file still has sites.
    # Deleting is equivalent to lowering to zero; committed mode sees real > 0
    # and goes red on the next push.
    for rel in old_baseline:
        if rel not in new_baseline:
            real = counts.get(rel, 0)
            if real > 0 and rel not in violations:
                violations[rel] = ("deleted", 0, real)

    return violations


# ── the gate ─────────────────────────────────────────────────────────────


def test_no_file_gains_a_short_fixed_wait() -> None:
    """A file's short-wait count may only fall, never rise.

    On a **pull request**: each file the PR touches is held to its count at
    the PR's merge-base and to its committed baseline entry (``GITHUB_BASE_REF``
    is set by GitHub Actions on pull_request events).  Files the PR does not
    touch are not judged.  Without ``GITHUB_BASE_REF`` (push, merge_group,
    local runs) the committed-baseline path runs instead.

    On **main** (push, merge_group, or any run where no base is readable):
    falls back to the committed baseline, the burn-down tracker and catcher
    of last resort.  Drift is a hard red: the PR check only judges touched
    files, so merge-order drift in untouched files surfaces here; a
    rebaseline PR clears it.
    """
    base, base_label = _pr_mode_base()
    use_baseline = base is None
    # Announce mode so it appears in the CI log.  warnings.warn(UserWarning)
    # is always visible in pytest's warnings summary regardless of capture
    # settings -- more reliable than STEP_SUMMARY (unwritable on some runners)
    # or stderr ::notice:: (swallowed by pytest -q).
    if base is not None:
        _msg = f"wait-discipline ratchet: measured against PR base {base_label}"
    else:
        _msg = f"wait-discipline ratchet: measured against committed baseline ({base_label})"
    import warnings as _warnings

    _warnings.warn(_msg, UserWarning, stacklevel=1)

    if not use_baseline:
        # PR mode: judge only files this PR touched, vs their count at base.
        risen = _pr_mode_violations(base)
        if risen is None:
            # Diff unavailable (corrupt index or missing objects) -- fall
            # through to committed-baseline mode so the gate never silently
            # passes when it cannot measure.
            use_baseline = True
        elif not risen:
            # No test-file violations.  If the PR also edited BASELINE_FILE,
            # validate those edits against lowered_baseline semantics.
            bv = _pr_mode_baseline_changed_entries(base)
            if bv:
                bv_lines = []
                for rel, (kind, a, b) in sorted(bv.items()):
                    if kind == "deleted":
                        bv_lines.append(
                            f"  test/{rel}: baseline entry deleted but file has {b} site(s) "
                            "(would red main; keep the entry or remove all its sites first)"
                        )
                    else:  # below_real
                        bv_lines.append(
                            f"  test/{rel}: baseline {a} is below real count {b} "
                            f"(would red main; lower to {b} by removing those {b - a} site(s))"
                        )
                raise AssertionError(
                    f"{len(bv)} baseline entry change(s) in this PR "
                    f"violate the lowered_baseline contract:\n"
                    + "\n".join(bv_lines)
                    + f"\n{BASELINE_FILE.name} entries may only go DOWN and "
                    f"never below the real site count.  "
                    f"{HELPERS_HINT}"
                )
            return
        else:
            report = [f"(measured against PR base: {base_label})"]
            for rel, (was, now, sites) in sorted(risen.items()):
                report.append(f"  test/{rel}: {was} -> {now}")
                report.extend(f"      {site}" for site in sites)
            raise AssertionError(
                f"{len(risen)} file(s) the PR touches gained a short fixed wait "
                f"compared to their count at the PR base (a wait_for, "
                f"asyncio.timeout or .result(timeout) under "
                f"{LOST_RUN_FLOOR_SECS:g}s, or a sleep under "
                f"{SHORT_POLL_SLEEP_SECS:g}s inside a loop):\n"
                + "\n".join(report)
                + f"\n{HELPERS_HINT}\n"
                f"The limit for each file is its count at {base_label}; "
                f"it may not rise.  {MOVE_HINT}"
            )

    if use_baseline:
        # Main / fallback mode: committed-baseline check.
        found = _tree_scan()
        baseline = read_baseline()
        counts = _counts(found)
        risen_main = _main_mode_violations(baseline, counts)
        if not risen_main:
            return
        report_main: list[str] = []
        for rel, (was, now) in sorted(risen_main.items()):
            report_main.append(f"  test/{rel}: {was} -> {now}")
            report_main.extend(f"      {site}" for site in found[rel])
        raise AssertionError(
            f"{len(risen_main)} file(s) gained a short fixed wait (a wait_for, "
            f"asyncio.timeout or .result(timeout) under {LOST_RUN_FLOOR_SECS:g}s, "
            f"or a sleep under {SHORT_POLL_SLEEP_SECS:g}s inside a loop):\n"
            + "\n".join(report_main)
            + f"\n{HELPERS_HINT}\n{BASELINE_FILE.name} is a ratchet: a count "
            f"may only go down.  {MOVE_HINT}\n{DRIFT_HINT}"
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
    adds a path the scan never visits is refused rather than quietly unenforced.

    An empty baseline is the goal state, not a broken scan: every short fixed wait
    under ``test/`` has moved to a signal, so the file holds the header alone. The
    positive controls below prove the needles still bite on a planted site."""
    baseline = read_baseline()
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
    """POSITIVE CONTROL on the real tree: the counts are not low because the
    needles match nothing. The tree holds no un-excused short fixed wait, so the
    scan must come back empty AND agree with the baseline; a planted site is
    still named (``test_a_planted_short_wait_is_named``) and every shape is
    still detected on the scratch trees above."""
    found = _tree_scan()
    assert not found, (
        "a short fixed wait is back under test/ with no baseline line: "
        f"{sorted(str(s) for sites in found.values() for s in sites)}. {HELPERS_HINT}"
    )
    assert _counts(found) == Counter(read_baseline())


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


def _git_in(repo: Path, *args: str) -> str:
    """Run git in *repo* using an isolated fixture environment."""
    import subprocess as _sp

    proc = _sp.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env=_fixture_git_env(),
    )
    return proc.stdout.strip()


def test_pr_mode_reds_touched_and_ignores_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR mode: a file the PR touched that gains a wait is flagged;
    an untouched file (already with sites at the base) is not judged.

    The test verifies the result by calling ``_pr_mode_violations`` directly,
    so it fails if that function is made a no-op.
    """
    _drop_ambient_git_location(monkeypatch)
    # -- scratch git repo ------------------------------------------------
    repo = tmp_path / "repo"
    repo.mkdir()
    test_dir = repo / "test"
    test_dir.mkdir()

    _git_in(repo, "init", "-b", "main")
    _git_in(repo, "config", "user.email", "t@test.invalid")
    _git_in(repo, "config", "user.name", "T")

    # Base commit: test_touched.py clean; test_untouched.py already has sites.
    (test_dir / "test_touched.py").write_text(
        "import asyncio\nasync def test_x():\n    pass\n", encoding="utf-8"
    )
    (test_dir / "test_untouched.py").write_text(
        "import asyncio\n"
        "async def test_a():\n    await asyncio.wait_for(f(), timeout=5)\n"
        "async def test_b():\n    await asyncio.wait_for(f(), timeout=5)\n",
        encoding="utf-8",
    )
    _git_in(repo, "add", ".")
    _git_in(repo, "commit", "-m", "base")
    base_sha = _git_in(repo, "rev-parse", "HEAD")

    # Working tree: test_touched.py gains a short wait; test_untouched.py unchanged.
    (test_dir / "test_touched.py").write_text(
        "import asyncio\nasync def test_x():\n" "    await asyncio.wait_for(f(), timeout=1)\n",
        encoding="utf-8",
    )

    # Call the real production function.
    risen = _pr_mode_violations(base_sha, cwd=repo, root=test_dir)

    assert (
        "test_touched.py" in risen
    ), f"touched file that gained a wait should fail PR mode; got {risen}"
    assert risen["test_touched.py"][0] == 0  # base count
    assert risen["test_touched.py"][1] == 1  # now count

    # test_untouched.py was not changed in the working tree, so git diff
    # does not list it -- it must not appear in PR mode violations.
    assert "test_untouched.py" not in risen, "untouched file must not be judged in PR mode"


def test_main_mode_reds_on_drift(tmp_path: Path) -> None:
    """Main mode (fallback): a file above its committed baseline is red.

    Calls ``_main_mode_violations`` -- the extracted helper that both the
    gate and this test use -- so the test fails if that function is replaced
    by a no-op.
    """
    (tmp_path / "test_drifted.py").write_text(
        "import asyncio\n"
        "async def test_a():\n    await asyncio.wait_for(f(), timeout=5)\n"
        "async def test_b():\n    await asyncio.wait_for(f(), timeout=5)\n",
        encoding="utf-8",
    )
    found = scan(tmp_path)
    counts = _counts(found)
    # Committed baseline says 1; the file now has 2.
    baseline = {"test_drifted.py": 1}
    risen = _main_mode_violations(baseline, counts)
    assert "test_drifted.py" in risen, "main mode must red a file above its committed baseline"
    assert risen["test_drifted.py"] == (1, 2), risen


def test_pr_mode_base_on_depth1_merge_commit_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_pr_mode_base returns the merge-base (HEAD^1) when GITHUB_BASE_REF is set.

    On CI the PR merge commit is checked out at depth 2.  _pr_mode_base checks
    GITHUB_BASE_REF then calls change_merge_base(), which detects the merge
    commit shape and returns HEAD^1 as the base.
    """
    _drop_ambient_git_location(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_in(repo, "init", "-b", "main")
    _git_in(repo, "config", "user.email", "t@test.invalid")
    _git_in(repo, "config", "user.name", "T")

    # Base commit (simulates main before the PR).
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git_in(repo, "add", ".")
    _git_in(repo, "commit", "-m", "base")
    base_sha = _git_in(repo, "rev-parse", "HEAD")

    # Feature branch with one commit.
    _git_in(repo, "checkout", "-b", "feat")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git_in(repo, "add", ".")
    _git_in(repo, "commit", "-m", "feat commit")

    # Merge commit (simulates what CI checks out for a PR).
    _git_in(repo, "checkout", "main")
    _git_in(repo, "merge", "--no-ff", "feat", "-m", "Merge PR")
    # HEAD now has 2 parents; HEAD^1 == base_sha.
    assert _git_in(repo, "rev-parse", "HEAD^1") == base_sha

    # _pr_mode_base with GITHUB_BASE_REF set should return HEAD^1 = base_sha.
    base, label = _pr_mode_base(
        cwd=repo,
        env={"GITHUB_BASE_REF": "main"},
    )
    assert (
        base is not None
    ), f"_pr_mode_base returned None; PR mode would not activate on CI (label={label!r})"
    assert base == base_sha, f"Expected HEAD^1={base_sha!r}, got {base!r}"

    # Without GITHUB_BASE_REF, falls through to committed-baseline.
    base_none, label_none = _pr_mode_base(cwd=repo, env={})
    assert base_none is None
    assert "committed-baseline" in label_none


def test_pr_mode_does_not_blame_pr_for_untouched_file_drift(
    tmp_path: Path,
) -> None:
    """PR mode ignores files the PR did not touch, even when they have drift.

    A file that is above its committed baseline (drift on main) is not judged
    in PR mode when the PR never touched it.  Committed-baseline mode (main push
    or merge queue) catches the drift instead.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    test_dir = repo / "test"
    test_dir.mkdir()

    _git_in(repo, "init", "-b", "main")
    _git_in(repo, "config", "user.email", "t@test.invalid")
    _git_in(repo, "config", "user.name", "T")

    # Base commit: both files have sites.
    (test_dir / "test_touched.py").write_text(
        "import asyncio\nasync def test_x():\n    pass\n", encoding="utf-8"
    )
    (test_dir / "test_drifted.py").write_text(
        "import asyncio\n"
        "async def test_a():\n    await asyncio.wait_for(f(), timeout=5)\n"
        "async def test_b():\n    await asyncio.wait_for(f(), timeout=5)\n",
        encoding="utf-8",
    )
    _git_in(repo, "add", ".")
    _git_in(repo, "commit", "-m", "base")
    base_sha = _git_in(repo, "rev-parse", "HEAD")

    # Working tree: test_touched.py gains a site; test_drifted.py unchanged.
    (test_dir / "test_touched.py").write_text(
        "import asyncio\nasync def test_x():\n" "    await asyncio.wait_for(f(), timeout=1)\n",
        encoding="utf-8",
    )

    # PR mode: only files the PR touched are judged.
    risen = _pr_mode_violations(base_sha, cwd=repo, root=test_dir)
    assert risen is not None

    # test_touched.py: PR added a site (0 -> 1) -- correctly flagged.
    assert "test_touched.py" in risen

    # test_drifted.py: not touched by this PR; not flagged in PR mode even
    # though it has sites that would exceed a hypothetical low baseline.
    assert "test_drifted.py" not in risen

    # For contrast: committed-baseline mode WOULD flag test_drifted.py if the
    # baseline said 1 (drift: real 2 > baseline 1).
    baseline_with_drift = {"test_drifted.py": 1}
    counts_now = _counts(scan(test_dir))
    main_risen = _main_mode_violations(baseline_with_drift, counts_now)
    assert "test_drifted.py" in main_risen  # main mode catches what PR mode skips


def test_pr_mode_catches_baseline_entry_lowered_below_real_count(
    tmp_path: Path,
) -> None:
    """PR mode catches a baseline edit that lowers an entry below the real count.

    A PR that edits only the baseline file (not the test file) and lowers a
    count below the real site count passes ``_pr_mode_violations`` (no touched
    test .py) but would red main on the next push.
    ``_pr_mode_baseline_changed_entries`` catches it, and this test calls the
    production function directly so it fails if that function returns ``{}``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    test_dir = repo / "test"
    test_dir.mkdir()

    _git_in(repo, "init", "-b", "main")
    _git_in(repo, "config", "user.email", "t@test.invalid")
    _git_in(repo, "config", "user.name", "T")

    # Base commit: test file has 2 sites; scratch baseline says 2.
    (test_dir / "test_foo.py").write_text(
        "import asyncio\n"
        "async def test_a():\n    await asyncio.wait_for(f(), timeout=5)\n"
        "async def test_b():\n    await asyncio.wait_for(f(), timeout=5)\n",
        encoding="utf-8",
    )
    baseline_txt = test_dir / "wait_discipline_baseline_scratch.txt"
    baseline_txt.write_text("2 test_foo.py\n", encoding="utf-8")
    _git_in(repo, "add", ".")
    _git_in(repo, "commit", "-m", "base")
    base_sha = _git_in(repo, "rev-parse", "HEAD")

    # Working tree: baseline lowered to 1, but test_foo.py still has 2 sites.
    baseline_txt.write_text("1 test_foo.py\n", encoding="utf-8")

    # Call the production function directly via cwd+baseline_file+test_root.
    violations = _pr_mode_baseline_changed_entries(
        base_sha,
        cwd=repo,
        baseline_file=baseline_txt,
        test_root=test_dir,
    )
    assert (
        "test_foo.py" in violations
    ), f"Expected below_real violation for test_foo.py; got {violations}"
    kind, a, b = violations["test_foo.py"]
    assert kind == "below_real", violations
    assert a == 1  # new baseline
    assert b == 2  # real count


def test_pr_mode_base_without_github_base_ref_uses_committed_baseline() -> None:
    """Without GITHUB_BASE_REF, _pr_mode_base returns None (committed-baseline).

    GITHUB_BASE_REF is only set on pull_request CI events; on push,
    merge_group and local runs it is absent, so the gate uses the committed
    baseline for all files.  This test goes red if that guard is removed.
    """
    base, label = _pr_mode_base(env={})
    assert base is None, f"Expected None when GITHUB_BASE_REF absent, got {base!r}"
    assert "committed-baseline" in label


def test_diff_failure_returns_none_not_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_pr_mode_violations returns None on diff failure, not an empty dict.

    None tells the caller to fall through to committed-baseline mode;
    an empty dict would silently pass the gate.  This test goes red if
    the RuntimeError branch returns {} instead of None.
    """
    _drop_ambient_git_location(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_in(repo, "init", "-b", "main")
    _git_in(repo, "config", "user.email", "t@test.invalid")
    _git_in(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git_in(repo, "add", ".")
    _git_in(repo, "commit", "-m", "base")
    # Pass an all-zeros SHA that git cannot resolve.  ratchet_scope.change_files
    # runs `git diff $base` and raises RuntimeError on non-zero exit.  This is
    # portable; deleting .git fails on Windows because git's loose objects are
    # created read-only.
    unresolvable_sha = "0" * 40
    result = _pr_mode_violations(unresolvable_sha, cwd=repo, root=repo)
    assert result is None, (
        "_pr_mode_violations must return None on diff failure, not {} (which would "
        "silently pass the gate)"
    )


def test_deleted_baseline_entry_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting a baseline entry while the file still has sites is flagged.

    This test goes red if the deleted-entry check is removed from
    _pr_mode_baseline_changed_entries.
    """
    _drop_ambient_git_location(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    test_dir = repo / "test"
    test_dir.mkdir()
    _git_in(repo, "init", "-b", "main")
    _git_in(repo, "config", "user.email", "t@test.invalid")
    _git_in(repo, "config", "user.name", "T")
    two_sites = (
        "import asyncio\n"
        "async def test_a():\n    await asyncio.wait_for(f(), timeout=5)\n"
        "async def test_b():\n    await asyncio.wait_for(f(), timeout=5)\n"
    )
    (test_dir / "test_foo.py").write_text(two_sites, encoding="utf-8")
    baseline_txt = test_dir / "wait_discipline_baseline_scratch.txt"
    baseline_txt.write_text("2 test_foo.py\n", encoding="utf-8")
    _git_in(repo, "add", ".")
    _git_in(repo, "commit", "-m", "base")
    base_sha = _git_in(repo, "rev-parse", "HEAD")
    # Working tree: entry deleted while the file still has 2 sites.
    baseline_txt.write_text("", encoding="utf-8")
    violations = _pr_mode_baseline_changed_entries(
        base_sha,
        cwd=repo,
        baseline_file=baseline_txt,
        test_root=test_dir,
    )
    assert "test_foo.py" in violations, (
        "Deleting a baseline entry while the file has sites must be flagged; "
        "the deleted-entry check appears to be missing"
    )
    assert violations["test_foo.py"][0] == "deleted"


def test_new_file_without_base_sites_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new test file the PR adds is judged from a base count of zero.

    If the base_count for a new file were set to a non-zero sentinel the
    violation would be missed.  This test goes red if base_count is not 0.
    """
    _drop_ambient_git_location(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    test_dir = repo / "test"
    test_dir.mkdir()
    _git_in(repo, "init", "-b", "main")
    _git_in(repo, "config", "user.email", "t@test.invalid")
    _git_in(repo, "config", "user.name", "T")
    (test_dir / "placeholder.py").write_text("pass\n", encoding="utf-8")
    _git_in(repo, "add", ".")
    _git_in(repo, "commit", "-m", "base")
    base_sha = _git_in(repo, "rev-parse", "HEAD")
    # Working tree: brand-new test file with one short wait.
    (test_dir / "test_new.py").write_text(
        "import asyncio\nasync def test_x():\n" "    await asyncio.wait_for(f(), timeout=1)\n",
        encoding="utf-8",
    )
    risen = _pr_mode_violations(base_sha, cwd=repo, root=test_dir)
    assert risen is not None
    assert "test_new.py" in risen, (
        "A new file with a short wait must be flagged with base_count=0; "
        "setting base_count to a non-zero sentinel would miss this"
    )
    assert risen["test_new.py"][0] == 0  # base count must be 0


def test_pr_mode_allows_git_detected_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git-detected renames carry their site count and are not flagged.

    When git's rename detection (-M) finds a rename, change_files returns
    (old_path, new_path) as one pair.  _pr_mode_violations reads the base
    count from old_path and the current count from new_path: if sites are
    unchanged the rename produces no violation.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    test_dir = repo / "test"
    test_dir.mkdir()
    _git_in(repo, "init", "-b", "main")
    _git_in(repo, "config", "user.email", "t@test.invalid")
    _git_in(repo, "config", "user.name", "T")
    two_sites = (
        "import asyncio\n"
        "async def test_a():\n    await asyncio.wait_for(f(), timeout=5)\n"
        "async def test_b():\n    await asyncio.wait_for(f(), timeout=5)\n"
    )
    # Base: only test_source.py with 2 sites (test_dest.py does not exist)
    (test_dir / "test_source.py").write_text(two_sites, encoding="utf-8")
    _git_in(repo, "add", ".")
    _git_in(repo, "commit", "-m", "base")
    base_sha = _git_in(repo, "rev-parse", "HEAD")
    # Stage the rename so git's -M detection fires.  change_files compares
    # the index to the base: R100 rename is reported as one pair.
    (test_dir / "test_source.py").unlink()
    (test_dir / "test_dest.py").write_text(two_sites, encoding="utf-8")
    _git_in(repo, "add", "-A")  # stage delete + add so rename is detected
    risen = _pr_mode_violations(base_sha, cwd=repo, root=test_dir)
    assert risen is not None
    # Rename detected: sites unchanged (2 at base, 2 now) -> no violation.
    assert (
        "test_dest.py" not in risen
    ), f"Git-detected rename with unchanged sites must not be flagged; got {risen}"


def test_multiline_wait_for_in_b_not_credited_by_deletion_in_a(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a multi-line wait in A does not credit a different multi-line wait in B.

    When the first source line of a wait-for call is `await asyncio.wait_for(`,
    all such calls share that prefix.  A simple first-line text match would let
    A's deletion of task_a credit B's addition of task_b, silently passing B.
    This test verifies B is still flagged (no credit pool is used).
    """
    _drop_ambient_git_location(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    test_dir = repo / "test"
    test_dir.mkdir()
    _git_in(repo, "init", "-b", "main")
    _git_in(repo, "config", "user.email", "t@test.invalid")
    _git_in(repo, "config", "user.name", "T")
    # Multi-line wait_for calls for task_a (A) and nothing in B.
    multiline_a = (
        "import asyncio\n"
        "async def test_a():\n"
        "    await asyncio.wait_for(\n"
        "        task_a(),\n"
        "        timeout=5,\n"
        "    )\n"
    )
    (test_dir / "test_file_a.py").write_text(multiline_a, encoding="utf-8")
    (test_dir / "test_file_b.py").write_text("pass\n", encoding="utf-8")
    _git_in(repo, "add", ".")
    _git_in(repo, "commit", "-m", "base")
    base_sha = _git_in(repo, "rev-parse", "HEAD")
    # PR: remove A's wait (A goes clean) and add a DIFFERENT wait to B.
    (test_dir / "test_file_a.py").write_text("import asyncio\n", encoding="utf-8")
    multiline_b = (
        "import asyncio\n"
        "async def test_b():\n"
        "    await asyncio.wait_for(\n"
        "        task_b(),\n"  # different call from A's task_a()
        "        timeout=5,\n"
        "    )\n"
    )
    (test_dir / "test_file_b.py").write_text(multiline_b, encoding="utf-8")
    risen = _pr_mode_violations(base_sha, cwd=repo, root=test_dir)
    assert risen is not None
    assert (
        "test_file_b.py" in risen
    ), f"Adding a wait in B must be flagged even if A removed one; got {risen}"


def test_identical_content_rename_is_not_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete rename with identical content is not flagged (git -M).

    Git's rename detection (-M flag in change_files) recognises a 100%
    similar rename and reports it as (old_path, new_path).  The comparison
    uses the old path's base count and the new path's current count: if
    both are the same, no violation is raised.
    """
    _drop_ambient_git_location(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    test_dir = repo / "test"
    test_dir.mkdir()
    _git_in(repo, "init", "-b", "main")
    _git_in(repo, "config", "user.email", "t@test.invalid")
    _git_in(repo, "config", "user.name", "T")
    two_sites = (
        "import asyncio\n"
        "async def test_a():\n    await asyncio.wait_for(f(), timeout=5)\n"
        "async def test_b():\n    await asyncio.wait_for(f(), timeout=5)\n"
    )
    (test_dir / "test_source.py").write_text(two_sites, encoding="utf-8")
    _git_in(repo, "add", ".")
    _git_in(repo, "commit", "-m", "base")
    base_sha = _git_in(repo, "rev-parse", "HEAD")
    (test_dir / "test_source.py").unlink()
    (test_dir / "test_dest.py").write_text(two_sites, encoding="utf-8")
    _git_in(repo, "add", "-A")
    risen = _pr_mode_violations(base_sha, cwd=repo, root=test_dir)
    assert risen is not None
    assert "test_dest.py" not in risen, f"Identical rename must not be flagged; got {risen}"


def test_pr_mode_accepts_split_file_when_baseline_entry_covers_new_file(
    tmp_path: Path,
) -> None:
    """Splitting a test file into two is not flagged when the baseline is updated.

    When file A is split into A (reduced) and B (new), B has base_count=0 in the
    diff (no base path).  If the PR also adds a baseline entry ``N test/B.py``,
    _pr_mode_violations uses N as B's base_count, so B is not a violation as long
    as it has at most N sites.  Without the baseline entry B would always be red.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    test_dir = repo / "test"
    test_dir.mkdir()
    _git_in(repo, "init", "-b", "main")
    _git_in(repo, "config", "user.email", "t@test.invalid")
    _git_in(repo, "config", "user.name", "T")
    three_sites = (
        "import asyncio\n"
        "async def test_a():\n    await asyncio.wait_for(f(), timeout=5)\n"
        "async def test_b():\n    await asyncio.wait_for(f(), timeout=5)\n"
        "async def test_c():\n    await asyncio.wait_for(f(), timeout=5)\n"
    )
    (test_dir / "test_original.py").write_text(three_sites, encoding="utf-8")
    # Baseline entry: 3 sites in original file.
    baseline_txt = test_dir / "wait_discipline_baseline.txt"
    baseline_txt.write_text("3 test_original.py\n", encoding="utf-8")
    _git_in(repo, "add", ".")
    _git_in(repo, "commit", "-m", "base")
    base_sha = _git_in(repo, "rev-parse", "HEAD")
    # PR: split into two files; update baseline to cover both.
    two_sites_a = (
        "import asyncio\n"
        "async def test_a():\n    await asyncio.wait_for(f(), timeout=5)\n"
        "async def test_b():\n    await asyncio.wait_for(f(), timeout=5)\n"
    )
    one_site_b = (
        "import asyncio\n" "async def test_c():\n    await asyncio.wait_for(f(), timeout=5)\n"
    )
    (test_dir / "test_original.py").write_text(two_sites_a, encoding="utf-8")
    (test_dir / "test_split.py").write_text(one_site_b, encoding="utf-8")
    # Baseline updated: original shrinks to 2, split file declared at 1.
    baseline_txt.write_text("2 test_original.py\n1 test_split.py\n", encoding="utf-8")
    risen = _pr_mode_violations(base_sha, cwd=repo, root=test_dir)
    assert risen is not None
    # Original file decreased (3 -> 2): no violation.
    assert "test_original.py" not in risen
    # New split file: 1 site, baseline declares 1 -> no violation.
    assert (
        "test_split.py" not in risen
    ), f"Split file with baseline entry must not be flagged; got {risen}"


def test_pr_mode_catches_rename_without_baseline_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR mode flags a rename whose baseline entry was removed but not re-added.

    When test_a.py (8 sites) is renamed to test_b.py and the PR removes
    the baseline entry for test_a.py but adds none for test_b.py, PR mode
    must flag test_b.py -- otherwise the merge queue fails it after merge.
    """
    _drop_ambient_git_location(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    test_dir = repo / "test"
    test_dir.mkdir()
    _git_in(repo, "init", "-b", "main")
    _git_in(repo, "config", "user.email", "t@test.invalid")
    _git_in(repo, "config", "user.name", "T")
    eight_sites = "\n".join(
        "import asyncio"
        + "\nasync def test_{}():\n    await asyncio.wait_for(f(), timeout=5)".format(i)
        for i in range(8)
    )
    (test_dir / "test_a.py").write_text(eight_sites, encoding="utf-8")
    baseline_txt = test_dir / "wait_discipline_baseline.txt"
    baseline_txt.write_text("8 test_a.py\n", encoding="utf-8")
    _git_in(repo, "add", ".")
    _git_in(repo, "commit", "-m", "base")
    base_sha = _git_in(repo, "rev-parse", "HEAD")
    # PR: rename test_a.py -> test_b.py, remove old baseline entry, forget to add new one.
    _git_in(repo, "mv", str(test_dir / "test_a.py"), str(test_dir / "test_b.py"))
    baseline_txt.write_text("", encoding="utf-8")  # removed old entry, no new one
    _git_in(repo, "add", "-A")
    risen = _pr_mode_violations(base_sha, cwd=repo, root=test_dir)
    assert risen is not None
    assert "test_b.py" in risen, (
        "Renamed file without baseline update must be flagged; "
        f"after merge the queue would fail 'test_b.py: 0 -> 8'; got {risen}"
    )


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
