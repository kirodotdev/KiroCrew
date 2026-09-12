"""Every structural remove of a cron row must route through _remove_job_rows.

Removing a cron retires its principal ``cron:<job id>``; the removal and the
release of the jobs that principal owned must land in the SAME save, or every
child of the removed cron strands -- owned by a principal no session can ever
present again, unlistable, unremovable, still firing. A bare
``self._jobs = [j for j in self._jobs if ...]`` filter compiles, passes tests,
and silently skips that cascade.

``_remove_job_rows`` is the only sanctioned spelling: it filters the rows out
and calls ``_release_children_of_removed`` in one place. The AST scan here
flags EVERY write to ``self._jobs`` -- assignment in any form (plain, slice,
subscript, augmented, annotated), ``del``, and mutating method calls -- and
fails unless the enclosing function is on the explicit allowlist, so a rogue
remover cannot skip the cascade in ANY spelling: the direct filter, the
two-step ``keep = [...]; self._jobs = keep`` form, ``self._jobs[:] = [...]``,
a ``.remove()`` loop, ``list(filter(...))``, or a rebuild from an index.
Out of static reach by nature: mutating through a local alias
(``jobs = self._jobs; jobs.remove(...)``) -- nothing in cron.py does this, and
a reviewer seeing an alias of ``self._jobs`` should treat it as the same
violation.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import kiro_crew.cron as cron_module
from kiro_crew.cron import CronService

SANCTIONED_HELPER = "_remove_job_rows"

# Functions allowed to write self._jobs, and why. Adding a name here is a
# review decision: the new writer must either not remove live rows, or route
# removals through _remove_job_rows.
ALLOWED_WRITERS = {
    SANCTIONED_HELPER,  # the one sanctioned structural remover
    "__init__",  # initial empty list, no rows exist yet
    "_sync",  # rebuilds the cache from disk, not a removal decision
    "_load",  # rebuilds from disk; its malformed-entry skip is outside this gate
    "_persist_add_if_absent_locked",  # append-only add
    "_persist_add_locked",  # append-only add
}

_MUTATING_METHODS = {
    "pop",
    "remove",
    "clear",
    "insert",
    "append",
    "extend",
    "sort",
    "reverse",
}


def _is_self_jobs(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "_jobs"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _touches_self_jobs(target: ast.expr) -> bool:
    """True for ``self._jobs`` itself and for ``self._jobs[...]`` subscripts."""
    if isinstance(target, ast.Subscript):
        return _is_self_jobs(target.value)
    return _is_self_jobs(target)


def _jobs_writes_by_function(tree: ast.Module) -> dict[str, list[str]]:
    """Map enclosing function name -> descriptions of self._jobs writes."""
    writes: dict[str, list[str]] = {}

    def record(func_name: str, lineno: int, kind: str) -> None:
        writes.setdefault(func_name, []).append(f"line {lineno}: {kind}")

    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Assign):
                if any(_touches_self_jobs(t) for t in node.targets):
                    record(func.name, node.lineno, "assignment")
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                if _touches_self_jobs(node.target):
                    record(func.name, node.lineno, "assignment")
            elif isinstance(node, ast.Delete):
                if any(_touches_self_jobs(t) for t in node.targets):
                    record(func.name, node.lineno, "del")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in _MUTATING_METHODS and _is_self_jobs(node.func.value):
                    record(func.name, node.lineno, f".{node.func.attr}() call")
    return writes


def test_every_self_jobs_write_is_allowlisted():
    source = inspect.getsource(cron_module)
    tree = ast.parse(source, filename=str(Path(cron_module.__file__)))
    writes = _jobs_writes_by_function(tree)
    assert SANCTIONED_HELPER in writes, (
        "expected the sanctioned helper's own filter assignment to be found; "
        "if it was renamed, update SANCTIONED_HELPER and ALLOWED_WRITERS"
    )
    rogue = {name: sites for name, sites in writes.items() if name not in ALLOWED_WRITERS}
    assert not rogue, (
        f"self._jobs is written outside the allowlist: {rogue}. Dropping a row "
        "from self._jobs without _release_children_of_removed strands every "
        "job the removed cron owned (unlistable, unremovable, still firing) -- "
        f"route structural removals through {SANCTIONED_HELPER}, or if the new "
        "write is provably not a removal, add it to ALLOWED_WRITERS with a "
        "reason."
    )


def test_helper_itself_calls_the_cascade_release():
    source = inspect.getsource(getattr(CronService, SANCTIONED_HELPER))
    tree = ast.parse(source.lstrip())
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "_release_children_of_removed" in calls, (
        f"{SANCTIONED_HELPER} must call _release_children_of_removed so every "
        "routed removal releases the removed jobs' children in the same save"
    )


def test_scan_catches_rogue_remover_spellings():
    """The gate must flag every remover spelling a sixth site could use."""
    spellings = {
        "direct_filter": "self._jobs = [j for j in self._jobs if j.id not in ids]",
        "two_step_local": (
            "keep = [j for j in self._jobs if j.id not in ids]\n" "        self._jobs = keep"
        ),
        "slice_assignment": ("self._jobs[:] = [j for j in self._jobs if j.id not in ids]"),
        "remove_loop": (
            "for j in list(self._jobs):\n"
            "            if j.id in ids:\n"
            "                self._jobs.remove(j)"
        ),
        "filter_call": ("self._jobs = list(filter(lambda j: j.id not in ids, self._jobs))"),
        "rebuild_from_index": "self._jobs = [by_id[i] for i in keep_ids]",
        "del_subscript": "del self._jobs[0]",
        "pop_call": "self._jobs.pop()",
    }
    for name, body in spellings.items():
        src = f"class C:\n    def _rogue_remover(self, ids):\n        {body}\n"
        writes = _jobs_writes_by_function(ast.parse(src))
        assert "_rogue_remover" in writes, f"scan missed rogue remover spelling {name!r}: {body!r}"


def test_remove_job_rows_filters_rows_and_returns_rollback(tmp_path):
    service = CronService(base_dir=tmp_path)
    parent = service.add_job("parent", "run", every_secs=3600)
    child = service.add_job("child", "run", every_secs=3600, session_key=f"cron:{parent.id}")
    bystander = service.add_job("bystander", "run", every_secs=3600, session_key="dashboard:x")

    restore = service._remove_job_rows({parent.id})

    remaining = {j.id for j in service._jobs}
    assert parent.id not in remaining, "removed row must be filtered out"
    assert {child.id, bystander.id} <= remaining, "children are released, never deleted"
    assert child.session_key == "", "child of the removed cron must drop to ownerless"
    assert bystander.session_key == "dashboard:x", "unrelated owners are untouched"
    assert restore == [
        (child, f"cron:{parent.id}")
    ], "rollback list must name each released child with its previous owner"
