"""The Job SDK's facade keeps the seams the one module had.

``kiro_crew.apps.job_sdk`` is a plain re-export facade over the
``kiro_crew.apps.job_sdk_<role>`` owners: it imports every name they define, so callers,
tests and documents still reach each one as ``kiro_crew.apps.job_sdk.<name>`` and get the
owner's own object. A re-export is a binding, so a patch through the facade reaches the
facade's own readers and nothing else. These tests pin that this is enough: no owner reads
or binds a name the test trees patch through the facade; the owners are exactly the
``job_sdk_*.py`` siblings and import neither the facade nor each other; the code the facade
keeps stays defined there; the family logs under the facade's name; the exceptions keep
the facade's module; and the record owner keeps the postponed annotations
``JobRun.from_dict`` reads.
"""

from __future__ import annotations

import ast
import functools
import importlib
import importlib.util
import logging
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew.apps import job_sdk

pytestmark = pytest.mark.xdist_group(name="tree_scan_job_sdk_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.apps.job_sdk"
OWNERS = ("kiro_crew.apps.job_sdk_records", "kiro_crew.apps.job_sdk_returns")
APPS_DIR = Path(job_sdk.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()
#: The ``from __future__ import annotations`` feature, as a module binds it.
_POSTPONED = importlib.import_module("__future__").annotations

#: Names the test trees are known to patch through the facade: the reader ``JobStore``
#: reads its records with, the SEL accessor ``JobSDK`` audits through, and the join bound
#: a disable waits for. The scan below must find all of them, so a scanner that stops
#: matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset({"read_bytes_with_retry", "sel", "_CLEANUP_JOIN_SECS"})

#: What the facade defines for itself rather than imports from an owner.
_KEPT = frozenset(
    {
        "logger",
        "_ORIGIN",
        "_PROGRESS_PHRASE",
        "_INTERRUPT_MESSAGE",
        "_WRITE_LOST_BODY_NEVER_RAN",
        "_WRITE_LOST_NEVER_RAN",
        "_WRITE_LOST_OUTCOME_UNKNOWN",
        "_UNKNOWN_PROGRESS_PHRASE",
        "_CLEANUP_JOIN_SECS",
        "_MAX_RECORD_BYTES",
        "_RUNS_DIRNAME",
        "_interrupt_error",
        "JobFn",
        "CleanupResult",
        "_Runner",
        "_Live",
        "JobStore",
        "JobSDK",
        "_SDKS",
        "_SDKS_LOCK",
        "register_sdk",
        "get_sdk",
        "forget_sdk",
        "registered_apps",
        "reconcile_all",
    }
)


def _owners() -> list[ModuleType]:
    return [importlib.import_module(name) for name in OWNERS]


def _source(module: ModuleType) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


def _defined(source: str) -> set[str]:
    """Top-level names *source* binds by definition or assignment (not by import)."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _imports(source: str) -> set[str]:
    """Every module *source* imports, anywhere in its body, relative imports resolved."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = importlib.util.resolve_name("." * node.level + module, "kiro_crew.apps")
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or rel.startswith("test/") or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "job_sdk" not in text:
            continue
        found |= facade_mirror_scan.patched_names(text, {FACADE: FACADE}).get(FACADE, set())
    return frozenset(found)


def test_every_name_patched_through_the_facade_is_read_there_and_by_no_owner() -> None:
    """The reach the unsplit module had. A write to ``job_sdk.<name>`` was what every
    reader of that name read; with plain re-exports that holds only while every reader is
    the facade, so no owner may read the name as a global or bind its own copy."""
    patched = _corpus_patched_names()
    facade_reads = facade_mirror_scan.global_reads(_source(job_sdk), job_sdk.__file__)
    assert sorted(patched - facade_reads) == []
    stray = {
        owner.__name__: sorted(
            patched
            & (facade_mirror_scan.global_reads(_source(owner), owner.__file__) | set(vars(owner)))
        )
        for owner in _owners()
    }
    assert {name: names for name, names in stray.items() if names} == {}


def test_the_reader_scan_sees_a_global_read_in_a_nested_function() -> None:
    """Non-vacuity for the owner half: the scan reaches a method body's globals."""
    planted = "class C:\n    def m(self):\n        return sel().x\n"
    assert "sel" in facade_mirror_scan.global_reads(planted, "<planted>")


def test_the_owners_are_every_job_sdk_sibling_and_import_no_family_member() -> None:
    """``OWNERS`` is every ``job_sdk_*.py`` beside the facade, and none of them imports the
    facade or another owner, anywhere in its body: the facade is the one composition point."""
    siblings = {f"kiro_crew.apps.{path.stem}" for path in APPS_DIR.glob("job_sdk_*.py")}
    assert sorted(siblings) == sorted(OWNERS)
    family = {FACADE, *OWNERS}
    reached = {o.__name__: sorted(_imports(_source(o)) & family) for o in _owners()}
    assert {name: hits for name, hits in reached.items() if hits} == {}


def test_the_import_scan_resolves_relative_imports() -> None:
    """Non-vacuity for the scan above: the spellings an owner could use all resolve."""
    planted = "from . import job_sdk\nfrom .job_sdk_records import JobRun\n"
    assert {FACADE, "kiro_crew.apps.job_sdk_records"} <= _imports(planted)


def test_every_name_an_owner_defines_is_the_facades_object() -> None:
    """Every owner name is bound on the facade, as the owner's own object."""
    wrong = [
        f"{owner.__name__}.{name}"
        for owner in _owners()
        for name in sorted(_defined(_source(owner)) - {"logger"})
        if getattr(job_sdk, name, None) is not getattr(owner, name)
    ]
    assert wrong == []


def test_the_facade_defines_the_code_it_keeps() -> None:
    """``JobSDK``, ``JobStore``, the registry, the interruption message and the tunables
    only they read stay defined in ``job_sdk.py``, and no owner binds any of them."""
    assert sorted(_KEPT - _defined(_source(job_sdk))) == []
    assert {o.__name__: sorted(_KEPT & set(vars(o)) - {"logger"}) for o in _owners()} == {
        name: [] for name in OWNERS
    }


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every owner that logs."""
    assert job_sdk.logger.name == FACADE
    assert job_sdk.logger is logging.getLogger(FACADE)
    assert [o.__name__ for o in _owners() if vars(o).get("logger") is not job_sdk.logger] == []


def test_the_exceptions_keep_the_facade_module() -> None:
    """A traceback or a log line names an exception as ``module.qualname``, so the three
    refusals keep the path callers import them from, and that path resolves them. The
    other moved classes, which nothing names that way, keep their owner's module."""
    for cls in (job_sdk.JobError, job_sdk.UnknownJobKind, job_sdk.JobCancelled):
        assert cls.__module__ == FACADE
        assert getattr(importlib.import_module(cls.__module__), cls.__qualname__) is cls
    for cls in (job_sdk.JobRun, job_sdk.JobHandle):
        assert cls.__module__ == "kiro_crew.apps.job_sdk_records"


def test_the_record_owner_keeps_postponed_annotations() -> None:
    """``JobRun.from_dict`` coerces each field by comparing its annotation to the strings
    ``"bool"``, ``"int"`` and ``"dict[str, str]"``; without postponed annotations those
    are types, every comparison is false, and a record read off disk keeps no field."""
    records = importlib.import_module("kiro_crew.apps.job_sdk_records")
    assert vars(records).get("annotations") is _POSTPONED
    fields = job_sdk.JobRun.__dataclass_fields__
    assert (fields["pid"].type, fields["cancellable"].type) == ("int", "bool")
    assert job_sdk.JobRun.from_dict({"pid": 7, "cancellable": True}).pid == 7


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_job_sdk.py" in walked
