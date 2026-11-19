"""Ratchet: a new module-level cache in ``src/kiro_crew`` ships its per-test reset.

A module-level dict, set or list a production function fills (a store cache, a
memo) outlives the test that filled it, so the next test on the same xdist worker
inherits it -- D11 in ``docs/system-specs/common/testing-conventions.md``. Two such
leaks in one week reached CI as order-dependent reds: a vault read-audit cache, and
``context._memory_stores``, whose default store aimed later tests' writes at an eval
run's deleted temp directory.

This counts module-level assignments of an empty mutable container (``{}``,
``set()``, ``dict()``, ``OrderedDict()``, ...) to a name containing ``cache``,
``stores`` or ``memo``. Each must be reset by the test floor: its name and its
module's name both appear in ``conftest.py`` or ``test/conftest*.py``. The ones that
had no reset when this gate landed are listed in :data:`BASELINE_FILE`; that list
only shrinks. A cache that cannot leak between tests (filled once from constants,
keyed by something no test changes) says so on its line with
``# process-cache-ok: <reason>``.

``python test/test_process_cache_floor_ratchet.py --write-baseline`` drops lines
that are now reset or gone; it never adds one.
"""

from __future__ import annotations

import ast
import functools
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "kiro_crew"
BASELINE_FILE = Path(__file__).resolve().parent / "process_cache_floor_baseline.txt"
FLOOR_FILES = (REPO_ROOT / "conftest.py", *sorted((REPO_ROOT / "test").glob("conftest*.py")))

_HEADER = """\
# Module-level caches under src/kiro_crew with no per-test reset in the test floor
# when the process-cache ratchet landed. Read by test/test_process_cache_floor_ratchet.py;
# one "<path under src/kiro_crew/>::<name>" per line.
# RATCHET: only delete lines. A new cache gets a reset in test/conftest_resets.py.
"""

_NAME = re.compile(r"cache|stores|memo", re.IGNORECASE)
_OK = re.compile(r"#\s*process-cache-ok:\s*\S.{9,}")
_CONTAINERS = {
    "dict",
    "set",
    "list",
    "OrderedDict",
    "defaultdict",
    "WeakValueDictionary",
    "WeakKeyDictionary",
}


def _is_empty_container(value: ast.expr | None) -> bool:
    if isinstance(value, ast.Dict):
        return not value.keys
    if isinstance(value, (ast.Set, ast.List)):
        return not value.elts
    if isinstance(value, ast.Call):
        func = value.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        return name in _CONTAINERS
    return False


def caches_in(source: str) -> list[tuple[int, str]]:
    """``(line, name)`` for each module-level cache-named mutable container."""
    if not _NAME.search(source):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    out: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if not _is_empty_container(value) or _OK.search(lines[node.lineno - 1]):
            continue
        out.extend(
            (node.lineno, t.id) for t in targets if isinstance(t, ast.Name) and _NAME.search(t.id)
        )
    return out


def _floor_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in FLOOR_FILES)


def _is_reset(rel: str, name: str, floor: str) -> bool:
    module = Path(rel).stem
    return bool(re.search(rf"\b{re.escape(name)}\b", floor)) and bool(
        re.search(rf"\b{re.escape(module)}\b", floor)
    )


@functools.lru_cache(maxsize=1)
def unreset() -> frozenset[str]:
    floor = _floor_text()
    found: set[str] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT).as_posix()
        if "tests" in Path(rel).parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found.update(
            f"{rel}::{name}" for _, name in caches_in(source) if not _is_reset(rel, name, floor)
        )
    return frozenset(found)


def read_baseline(path: Path = BASELINE_FILE) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def test_every_new_module_level_cache_has_a_per_test_reset() -> None:
    new = sorted(unreset() - read_baseline())
    assert new == [], (
        "Module-level caches with no per-test reset in the test floor:\n"
        + "".join(f"  src/kiro_crew/{entry}\n" for entry in new)
        + "Add an autouse reset in test/conftest_resets.py (see "
        "_isolate_context_store_caches), or mark a cache that cannot leak between "
        "tests with `# process-cache-ok: <reason>` on its line."
    )


def test_the_scanner_counts_cache_containers_and_honours_the_excuse() -> None:
    source = "\n".join(
        [
            "_memory_stores: dict[str, object] = {}",
            "_SHIM_CACHE = dict()",
            "_seen_memo = set()",
            "_PLAIN = {}",
            "_FIXED_CACHE = {'a': 1}",
            "_ok_cache = {}  # process-cache-ok: filled once from constants at import",
            "def f():",
            "    _local_cache = {}",
        ]
    )
    assert [name for _, name in caches_in(source + "\n")] == [
        "_memory_stores",
        "_SHIM_CACHE",
        "_seen_memo",
    ]


def _write_baseline() -> int:
    kept = read_baseline() & unreset() if BASELINE_FILE.exists() else set(unreset())
    BASELINE_FILE.write_text(
        _HEADER + "".join(f"{e}\n" for e in sorted(kept)), encoding="utf-8", newline="\n"
    )
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-baseline"]:
        sys.exit(_write_baseline())
    print("\n".join(sorted(unreset())))
