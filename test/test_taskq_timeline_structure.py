"""Task timeline structure: what ``taskq/timeline.py`` may import, call and be called by.

The behaviour of the view is pinned in ``test_taskq_timeline.py``. These tests read the
source tree instead: the module is a read-only leaf under ``taskq`` with no scrubber of
its own, it reaches the crew log only through its read surface, and the task route is
its one caller.
"""

from __future__ import annotations

import ast

import pytest
import source_corpus

from kiro_crew.dashboard.handlers import tasks as tasks_mod

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_taskq_timeline_structure")

_TIMELINE = "taskq/timeline.py"
_RENDER_CALLERS = {"dashboard/handlers/tasks.py"}
#: Modules timeline.py may import at module level.
_ALLOWED_IMPORTS = {
    "__future__",
    "json",
    "logging",
    "math",
    "collections",
    "collections.abc",
    "dataclasses",
    "datetime",
    "typing",
    "kiro_crew.taskq",
    "kiro_crew.taskq.adapters.runner",
}
#: Calls that would write, mutate or emit through the task store, a file or the crew
#: log. ``append`` and ``replace`` are not listed: here they are a list's and a
#: datetime's methods, and the crew log's own ``append`` is kept out by the import
#: allowlist in ``test_the_crew_log_is_reached_only_through_its_read_surface``.
_MUTATING_CALLS = {
    "write",
    "write_text",
    "write_bytes",
    "unlink",
    "rename",
    "execute",
    "executemany",
    "commit",
    "accept",
    "claim",
    "transition",
    "cancel",
    "reconcile_unknown",
    "defer",
    "wake_wait",
    "record_progress",
    "open",
    "emit",
    "dump",
    "remove",
    "rmtree",
}


def _timeline_tree() -> ast.Module:
    return ast.parse((source_corpus.src_root() / _TIMELINE).read_text(encoding="utf-8"))


def test_the_timeline_is_a_read_only_leaf_with_no_scrubber_of_its_own() -> None:
    tree = _timeline_tree()
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module != "":
            imported.add(node.module)
        elif isinstance(node, ast.If):  # the TYPE_CHECKING block
            for inner in node.body:
                if isinstance(inner, ast.ImportFrom) and inner.module:
                    imported.add(inner.module)
    extra = imported - _ALLOWED_IMPORTS - {"kiro_crew.taskq.store"}
    assert not extra, f"timeline.py imports {sorted(extra)}; it is a leaf under taskq"
    names = {
        n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not {n for n in names if any(w in n.lower() for w in ("scrub", "redact", "mask"))}, names
    source = (source_corpus.src_root() / _TIMELINE).read_text(encoding="utf-8")
    assert "kiro_crew.security" not in source and "redact_and_truncate" not in source


def test_the_timeline_makes_no_mutating_call_and_never_opens_a_file() -> None:
    calls = {
        getattr(n.func, "attr", getattr(n.func, "id", ""))
        for n in ast.walk(_timeline_tree())
        if isinstance(n, ast.Call)
    }
    assert not (calls & _MUTATING_CALLS), sorted(calls & _MUTATING_CALLS)


def test_the_crew_log_is_reached_only_through_its_read_surface() -> None:
    tree = _timeline_tree()
    pulled: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "kiro_crew.crew_log"
        ):
            pulled |= {f"{node.module}.{alias.name}" for alias in node.names}
    assert pulled == {
        "kiro_crew.crew_log.read.read_page",
        "kiro_crew.crew_log.resolve.child_record",
    }, sorted(pulled)


def test_timeline_py_is_the_only_reader_added_and_the_route_the_only_caller() -> None:
    root = source_corpus.src_root()
    renderers: set[str] = set()
    readers: set[str] = set()
    sites = 0
    for path, _text, tree in source_corpus.parsed_candidates(require_all=("timeline",)):
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name == "render_timeline":
                renderers.add(rel)
                sites += 1
            if name == "read_child_entries":
                readers.add(rel)
    assert renderers == _RENDER_CALLERS, f"render_timeline is called from {sorted(renderers)}"
    assert sites == 1, f"render_timeline has {sites} call expressions in src/; the contract is 1"
    assert readers <= {
        _TIMELINE
    }, f"a second reader of a child's crew-log entries: {sorted(readers)}"
    assert (root / _TIMELINE).is_file()


def test_the_task_events_tail_is_the_detail_routes_bound() -> None:
    source = (source_corpus.src_root() / "dashboard/handlers/tasks.py").read_text(encoding="utf-8")
    assert "events_limit=_EVENTS_TAIL" in source
    assert tasks_mod._EVENTS_TAIL == 200
