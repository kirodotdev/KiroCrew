"""``scripts/ci/runner_watchdog.py`` stays one script while its code lives in ``runner_watchdog_parts``.

The workflow runs ``runner_watchdog.py`` and the watchdog tests load it by path as
``runner_watchdog`` (``ci_runner_watchdog_helpers.wd``); the stages of a tick live in the
owner modules of ``scripts/ci/runner_watchdog_parts/``. These tests pin what keeps that
invisible to both:

- every name the script bound before the split is still bound on it, and a name an owner
  defines is the owner's own object there;
- every name a test patches on the script is read by the owners as
  ``runner_watchdog.<name>`` inside a function body, so the patch reaches the code that
  reads it. An owner never imports it, assigns it, reads it bare or through another module,
  or reads it while being imported; the one binding allowed is the ``def`` or ``class`` that
  defines a patched function in its owner. The guarded names come from the tests' own patch
  calls, not from a list kept here;
- an owner imports only the owners below it, and only the script's names that exist before
  the script imports its owners;
- the script still runs from a copy of ``scripts/ci`` alone, a subset of the workflow's
  sparse (cone-mode) checkout.
"""

from __future__ import annotations

import ast
import dis
import functools
import importlib
import json
import os
import shutil
import subprocess
import sys
import types
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from ci_runner_watchdog_helpers import ROOT, SCRIPT, FakeApi, _policy, _run, wd

from kiro_crew.subprocess_utf8 import UTF8_TEXT

# This module reads every test file once to find the patch targets; one group per file
# keeps that read on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_runner_watchdog_parts")

FACADE = "runner_watchdog"
PACKAGE = "runner_watchdog_parts"
PARTS_DIR = SCRIPT.parent / PACKAGE
HELPERS_MODULE = "ci_runner_watchdog_helpers"

#: The owners, lowest layer first: an owner imports only the owners before it.
LAYER = ("client", "classify", "heal_safety", "listing", "supersession", "heal", "recovery")

#: The exceptions the script's output names: they stay defined in the script itself.
FACADE_EXCEPTIONS = ("ApiError", "LookupInconclusive")

#: Every top-level name ``runner_watchdog.py`` bound before the split, frozen.
BASE_NAMES = frozenset("""
    ACTIONABLE_CANDIDATE_STATUSES Any Api ApiError BRANCH_LISTING_DEPTH
    BRANCH_LISTING_MAX_PAGES CANCELLED_ORPHAN CANDIDATE_STATUSES CODEBUILD_LABEL_PREFIX
    COMPLETED_SAMPLE COMPLETED_SAMPLE_WORKFLOWS Callable DispatchEvidence FAILED_OUTCOMES
    GHOST_AFTER GitHubApi HEALTHY HEAL_EXEMPT HEAL_SAFE_PULL_REQUEST_WORKFLOWS
    HEAL_SAFE_WORKFLOWS HoldCheck Iterable Iterator JOBS_LISTING_MAX_PAGES KICK_EVENT
    LISTING_TRUNCATED_MARKER_ID LIVE_CLASSIFY_READS LIVE_EVIDENCE_RESERVE
    LIVE_EVIDENCE_TOPUP_READS LOOKUP_INCONCLUSIVE LookupInconclusive MAX_RESTORE_DEPTH
    NESTED_RERUN_RESERVE_SECONDS ORPHANED OUTCOME_ABORTED_RATE_LIMITED OUTCOME_CANCEL_FAILED
    OUTCOME_CANCEL_TIMED_OUT OUTCOME_COMPLETED_ON_ITS_OWN OUTCOME_DRY_RUN
    OUTCOME_EVIDENCE_REREAD_DEFERRED OUTCOME_HEALED OUTCOME_HEAL_SAFETY_UNKNOWN
    OUTCOME_HUMAN_REQUIRED OUTCOME_LISTING_TRUNCATED OUTCOME_LOOKUP_FAILED
    OUTCOME_NOT_ATTEMPTED OUTCOME_OWN_RERUN_UNCANCELLED OUTCOME_RECOVERED
    OUTCOME_RERUN_ATTEMPT_LEFT OUTCOME_RERUN_DEFERRED OUTCOME_RERUN_REFUSED
    OUTCOME_RERUN_WITHDRAWN OUTCOME_SUCCESSOR_FINISHED OUTCOME_SUCCESSOR_LOST
    OUTCOME_SUCCESSOR_RESTORED OUTCOME_SUCCESSOR_SUPERSEDED OUTCOME_SUCCESSOR_UNSETTLED
    OUTCOME_SUPERSEDED OWN_QUEUE_DISPATCHING OWN_QUEUE_SATURATED OWN_QUEUE_SILENT
    OrphanedJob PAGE_SIZE POST_RERUN_SETTLE_SECONDS PULL_REQUEST_LISTING_DEPTH Policy
    Protocol RATE_LIMIT_MARKER_ID RATE_LIMIT_RETRY_SECONDS RECOVERY_CLASSIFY_READS
    RECOVERY_LISTING_MAX_PAGES REPO_LISTING_MAX_PAGES REPO_LISTING_MAX_WINDOWS
    REPO_LISTING_RESULT_CEILING RERUN_ENDPOINT RERUN_RESERVE_SECONDS RESTORE_CHAIN_SECONDS
    RESTORE_LEVEL_SECONDS RunVerdict SATURATION_FRACTION SKIPPED_ATTEMPT_CAP SKIPPED_FORK
    SKIPPED_NO_DISPATCH_EVIDENCE SKIPPED_PARTIAL_EVIDENCE SKIPPED_SATURATED
    SKIPPED_SUPERSEDED SKIPPED_YOUNG STAND_DOWN_CANCEL_WAIT_SECONDS SUCCESSOR_SETTLE_SECONDS
    SUPERSEDED_SUCCESSOR_UNIDENTIFIED SUPERSEDED_WITHOUT_SUCCESSOR TICK_ABORTED_RATE_LIMITED
    TICK_LISTING_TRUNCATED Tick WAITING_ON_GROUP WATCHDOG_WORKFLOW WATCHED_WORKFLOWS
    _BRANCH_GROUP_SIGNAL _CONTROL_CHARS _MARKDOWN_SPECIALS _PER_RUN_LABEL_SUFFIX
    _PUBLISH_OR_DEPLOY_PATTERNS _PULL_REQUEST_GROUP_SIGNAL _Pending _ReadsThrough
    _WATCHED_SET _base_verdict _can_carry_a_slow_start
    _current_or_successor_for_pull_request _env_flag _evidence_reserve
    _flag_unclassified_near_expiry _fmt_delta _guard _heal_eligible_shape _is_watched
    _iter_actionable_runs _iter_repo_runs _label _latest_queue _mark_heal_exempt
    _may_hold_jobs _md _md_link _open_pull_request_heads _orphan _orphan_queue _out_of_time
    _pull_request_numbers _rate_limit_hints _rerun _rerun_attempt_may_be_cancelled
    _rerun_cancelled_successor _restore_successor _runs_path _safe_text _status_or_unknown
    _supersession_is_answerable _withdraw _workflow_of _workflow_without_full_line_comments
    _yaml_mapping_entries annotations argparse base64 build_parser classify_cancelled_run
    classify_run current_or_successor_id dataclass datetime dispatch_queue drop_ghost_runs
    field gather_all_candidate_runs heal_exempt_workflows heal_runs heal_safe_declared http
    is_codebuild_job is_current_run is_fork_run json kick_is_redundant
    list_all_candidate_runs list_jobs list_recent_cancelled_runs list_runs
    live_runs_within_read_bound main newest_run_id_for_branch os parse_timestamp re
    recover_cancelled_runs render_summary replace resolve_hold run_watchdog
    sample_completed_runs stand_down supersession_clears_hold sys time timedelta timezone
    top_up_unread_evidence urllib workflow_has_ref_or_pr_concurrency
    workflow_is_heal_safe_at_revision workflow_text_has_publish_or_deploy_step
    workflow_text_is_heal_safe
    """.split())


def _owner_path(owner: str) -> Path:
    return PARTS_DIR / f"{owner}.py"


def _owner_module(owner: str) -> types.ModuleType:
    return sys.modules[f"{PACKAGE}.{owner}"]


def _owner_tree(owner: str) -> ast.Module:
    return ast.parse(_owner_path(owner).read_text(encoding="utf-8"))


def _defined_names(tree: ast.Module) -> list[str]:
    """Names a module's own top-level statements define (not the ones it imports)."""
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


# ── the facade keeps every name, bound to the owner's object ─────────────────


def test_the_owner_files_are_exactly_the_layer() -> None:
    on_disk = {path.stem for path in PARTS_DIR.glob("*.py")} - {"__init__"}
    assert on_disk == set(LAYER)
    init = ast.parse((PARTS_DIR / "__init__.py").read_text(encoding="utf-8"))
    assert len(init.body) == 1 and ast.get_docstring(init), "the package defines nothing"


def test_every_name_the_script_bound_before_the_split_is_still_bound() -> None:
    assert sorted(name for name in BASE_NAMES if not hasattr(wd, name)) == []


def test_every_owner_name_is_a_base_name_and_the_facade_holds_the_owners_object() -> None:
    for owner in LAYER:
        module = _owner_module(owner)
        defined = _defined_names(_owner_tree(owner))
        assert defined, owner
        assert sorted(set(defined) - BASE_NAMES) == [], owner
        for name in defined:
            assert getattr(wd, name) is getattr(module, name), f"{owner}.{name}"


def test_a_class_keeps_its_owners_module_and_the_named_exceptions_keep_the_scripts() -> None:
    for owner in LAYER:
        module = _owner_module(owner)
        for node in _owner_tree(owner).body:
            if isinstance(node, ast.ClassDef):
                assert getattr(module, node.name).__module__ == module.__name__
    facade_classes = {
        node.name
        for node in ast.parse(SCRIPT.read_text(encoding="utf-8")).body
        if isinstance(node, ast.ClassDef)
    }
    for name in FACADE_EXCEPTIONS:
        assert name in facade_classes
        assert getattr(wd, name).__module__ == wd.__name__
        for owner in LAYER:
            held = getattr(_owner_module(owner), name, None)
            assert held is None or held is getattr(wd, name), f"{owner}.{name}"


# ── import direction ──────────────────────────────────────────────────────────


def _first_owner_import_line(tree: ast.Module) -> int:
    return min(
        node.lineno
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(PACKAGE + ".")
    )


def test_an_owner_imports_only_the_owners_below_it() -> None:
    for index, owner in enumerate(LAYER):
        for node in ast.walk(_owner_tree(owner)):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            for name in modules:
                if name == PACKAGE or name.startswith(PACKAGE + "."):
                    home = name[len(PACKAGE) + 1 :]
                    assert home in LAYER[:index], f"{owner} imports {name}"


def test_an_owner_takes_from_the_script_only_what_exists_before_its_owners_are_imported() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    cut = _first_owner_import_line(tree)
    bound_before = set()
    for node in tree.body:
        if node.lineno >= cut:
            break
        bound_before.update(_defined_names(ast.Module(body=[node], type_ignores=[])))
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound_before.update((a.asname or a.name).split(".")[0] for a in node.names)
    for owner in LAYER:
        for node in _owner_tree(owner).body:
            if isinstance(node, ast.ImportFrom) and node.module == FACADE:
                names = {alias.asname or alias.name for alias in node.names}
                assert sorted(names - bound_before) == [], owner


_COLD_IMPORT = """
import importlib, importlib.util, json, os, sys
script, owners = sys.argv[1], sys.argv[2:]
spec = importlib.util.spec_from_file_location("runner_watchdog", script)
wd = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wd
spec.loader.exec_module(wd)
here = os.path.dirname(os.path.realpath(script))
fresh = []
for owner in owners:
    for name in [n for n in sys.modules if n.split(".")[0] == "runner_watchdog_parts"]:
        del sys.modules[name]
    module = importlib.import_module("runner_watchdog_parts." + owner)
    fresh.append(module.runner_watchdog is wd if hasattr(module, "runner_watchdog") else None)
print(json.dumps({"last": sys.path[-1] == here, "first": sys.path[0] == here, "fresh": fresh}))
"""


def test_each_owner_imports_cold_once_the_script_is_loaded_by_path(tmp_path: Path) -> None:
    """A fresh interpreter loads the script the way the tests do, then imports each owner
    again from nothing. The script's own directory is APPENDED to ``sys.path`` for that,
    never put first."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSAFEPATH")}
    out = subprocess.run(
        [sys.executable, "-c", _COLD_IMPORT, str(SCRIPT), *LAYER],
        capture_output=True,
        check=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
        **UTF8_TEXT,
    )
    result = json.loads(out.stdout.splitlines()[-1])
    reads_late = [owner for owner in LAYER if "import runner_watchdog\n" in _owner_text(owner)]
    assert result["last"] and not result["first"], result
    assert result["fresh"] == [True if owner in reads_late else None for owner in LAYER]


def _owner_text(owner: str) -> str:
    return _owner_path(owner).read_text(encoding="utf-8")


# ── the script still runs from a checkout of scripts/ci alone ────────────────


def _sparse_copy(tmp_path: Path) -> Path:
    target = tmp_path / "scripts" / "ci"
    shutil.copytree(SCRIPT.parent, target, ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def _run_sparse(root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    dropped = {"PYTHONPATH", "PYTHONSAFEPATH", "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_REPOSITORY"}
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    # argparse wraps the help to the terminal width and breaks words at hyphens; a fixed
    # width keeps the description on whole words whatever the host exports.
    env["COLUMNS"] = "200"
    return subprocess.run(
        [sys.executable, "-B", "scripts/ci/runner_watchdog.py", *argv],
        capture_output=True,
        cwd=root,
        env=env,
        timeout=60,
        **UTF8_TEXT,
    )


def test_the_script_runs_from_a_checkout_of_scripts_ci_alone(tmp_path: Path) -> None:
    """The workflow checks out ``scripts/ci`` (and, in cone mode, only top-level files
    besides) and runs the script as ``__main__``: the owners' ``import runner_watchdog``
    must find that module, not load a second copy (which would stop on its half-imported
    owners)."""
    root = _sparse_copy(tmp_path)
    shown = _run_sparse(root, "--help")
    assert shown.returncode == 0, shown.stderr
    assert "usage: runner_watchdog.py" in shown.stdout
    # argparse re-wraps the description, so compare without whitespace.
    assert "".join(str(wd.__doc__).splitlines()[0].split()) in "".join(shown.stdout.split())
    refused = _run_sparse(root, "--repo", "example-org/example-repo")
    assert refused.returncode == 2, refused.stderr
    assert "GH_TOKEN or GITHUB_TOKEN is required" in refused.stderr


def test_the_script_aliases_itself_and_appends_its_directory_before_its_owners() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    cut = _first_owner_import_line(tree)
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(ast.Module(body=[n for n in tree.body if n.lineno < cut]))
        if isinstance(node, ast.Call)
    ]
    assert "sys.modules.setdefault" in calls
    assert "sys.path.append" in calls
    for path in [SCRIPT, *(_owner_path(owner) for owner in LAYER)]:
        assert "sys.path.insert" not in path.read_text(encoding="utf-8"), path.name


# ── patch reach: what the tests patch on the script ─────────────────────────


def _facade_aliases(tree: ast.Module, path: Path) -> set[str]:
    """The names a test module binds the script to: ``from ci_runner_watchdog_helpers
    import wd`` (under any alias), ``wd`` itself in the helpers module, and any plain
    ``name = <alias>`` copy of one of those."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == HELPERS_MODULE:
            aliases.update(a.asname or a.name for a in node.names if a.name == "wd")
        if path.stem == HELPERS_MODULE and isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "wd" for t in node.targets):
                aliases.add("wd")
    copies = [
        (t.id, node.value.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
        for t in node.targets
        if isinstance(t, ast.Name)
    ]
    grew = True
    while grew:
        grew = False
        for name, source in copies:
            if source in aliases and name not in aliases:
                aliases.add(name)
                grew = True
    return aliases


def _is_facade(node: ast.expr | None, aliases: set[str]) -> bool:
    """``wd`` (an alias of the script) or ``helpers.wd``."""
    if isinstance(node, ast.Name):
        return node.id in aliases
    return isinstance(node, ast.Attribute) and node.attr == "wd"


def _is_facade_namespace(node: ast.expr | None, aliases: set[str]) -> bool:
    """``wd.__dict__`` or ``vars(wd)``."""
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        return _is_facade(node.value, aliases)
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "vars"
        and len(node.args) == 1
        and _is_facade(node.args[0], aliases)
    )


def _argument(node: ast.Call, index: int, *keywords: str) -> ast.expr | None:
    if len(node.args) > index:
        return node.args[index]
    return next((k.value for k in node.keywords if k.arg in keywords), None)


def _text(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


#: ``mock.patch.multiple`` keywords that configure the patch rather than name a target.
_MULTIPLE_OPTIONS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})


def _patch_targets(tree: ast.Module, aliases: set[str]) -> set[str]:
    """Every name a module patches on the script, in each spelling a test can use."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        verb = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        first = _argument(node, 0, "target", "in_dict", "dic")
        if verb in {"setattr", "delattr", "object"} and _is_facade(first, aliases):
            name = _text(_argument(node, 1, "name", "attribute"))
            if name is not None:
                found.add(name)
        elif verb == "multiple" and _is_facade(first, aliases):
            found.update(k.arg for k in node.keywords if k.arg and k.arg not in _MULTIPLE_OPTIONS)
        elif verb in {"setattr", "delattr", "patch"} and _text(first) is not None:
            dotted = str(_text(first))
            if dotted.startswith(FACADE + "."):
                found.add(dotted[len(FACADE) + 1 :].split(".")[0])
        elif verb in {"setitem", "delitem"} and _is_facade_namespace(first, aliases):
            name = _text(_argument(node, 1, "name"))
            if name is not None:
                found.add(name)
        elif verb == "dict" and _is_facade_namespace(first, aliases):
            values = _argument(node, 1, "values")
            if isinstance(values, ast.Dict):
                found.update(n for n in map(_text, values.keys) if n is not None)
            found.update(k.arg for k in node.keywords if k.arg and k.arg not in {"clear"})
    return found


def _patch_targets_in(source: str, path: Path) -> set[str]:
    tree = ast.parse(source)
    return _patch_targets(tree, _facade_aliases(tree, path))


def _may_patch_the_script(text: str) -> bool:
    """Cheap text filter before a parse: the module binds ``wd`` or names the module."""
    return HELPERS_MODULE in text or f'"{FACADE}.' in text or f"'{FACADE}." in text


@functools.cache
def guarded_names() -> frozenset[str]:
    """Every name any test patches on the script, read from the test tree itself."""
    names: set[str] = set()
    for path in sorted((ROOT / "test").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if _may_patch_the_script(text):
            names |= _patch_targets_in(text, path)
    return frozenset(names)


def test_the_corpus_finds_every_patch_spelling_and_nothing_else() -> None:
    planted = """
import ci_runner_watchdog_helpers as helpers
from ci_runner_watchdog_helpers import wd
from ci_runner_watchdog_helpers import wd as watchdog
import unittest.mock as _mock
copy = watchdog
def test(monkeypatch, other):
    monkeypatch.setattr(wd, "A", 1)
    _mock.patch.object(watchdog, "B")
    monkeypatch.setattr("runner_watchdog.C", 1)
    _mock.patch("runner_watchdog.D.attr")
    monkeypatch.delattr(wd, "E")
    _mock.patch.multiple(wd, F=1, G=2, create=True)
    monkeypatch.setitem(vars(wd), "H", 1)
    monkeypatch.setitem(wd.__dict__, "I", 1)
    monkeypatch.setattr(wd, name="J", value=1)
    _mock.patch.object(target=wd, attribute="K")
    _mock.patch.dict(vars(wd), {"L": 1})
    monkeypatch.setattr(helpers.wd, "M", 1)
    monkeypatch.setattr(copy, "N", 1)
    monkeypatch.setattr(other, "NOT_THE_SCRIPT", 1)
    monkeypatch.setattr("elsewhere.NOT_THE_SCRIPT", 1)
"""
    assert _patch_targets_in(planted, Path("test_planted.py")) == set("ABCDEFGHIJKLMN")
    unrelated = planted.replace(HELPERS_MODULE, "some_other_helpers")
    assert _patch_targets_in(unrelated, Path("test_planted.py")) == set("CDM")


def test_the_corpus_is_not_empty_and_names_only_what_the_script_binds() -> None:
    guarded = guarded_names()
    assert guarded, "no test patches the script: the patch-reach guard would check nothing"
    assert sorted(name for name in guarded if not hasattr(wd, name)) == []


# ── patch reach: how the owners read those names ───────────────────────────


def reach_violations(source: str, guarded: frozenset[str]) -> list[str]:
    """Each way *source*, an owner module, could hold a guarded name a patch misses.

    A guarded name must be read as ``runner_watchdog.<name>`` inside a function body.
    Reading it bare, binding it (an import under any name, an assignment, a parameter),
    reading it through another module (an owner, under any alias), aliasing or rebinding
    the script, or reading it while the module is imported (at module or class level, in a
    default, a decorator or an annotation) keeps an object the patch does not replace.
    """
    tree = ast.parse(source)
    problems: list[str] = []
    # Names bound to a module other than the script: ``import runner_watchdog_parts.x``,
    # ``from runner_watchdog_parts import x`` and any other module import.
    other_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            other_modules.update(
                (a.asname or a.name.split(".")[0]) for a in node.names if a.name != FACADE
            )
        elif isinstance(node, ast.ImportFrom) and node.module == PACKAGE:
            other_modules.update(a.asname or a.name for a in node.names)

    def visit(node: ast.AST, call_time: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            outer = [*args.defaults, *[d for d in args.kw_defaults if d is not None]]
            if not isinstance(node, ast.Lambda):
                outer += node.decorator_list
                outer += [a.annotation for a in (*args.posonlyargs, *args.args) if a.annotation]
                outer += [a.annotation for a in args.kwonlyargs if a.annotation]
                outer += [node.returns] if node.returns else []
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if arg is not None and arg.arg in guarded:
                    problems.append(f"line {node.lineno}: parameter {arg.arg} binds a guarded name")
            for child in outer:
                visit(child, call_time)
            for child in node.body if isinstance(node.body, list) else [node.body]:
                visit(child, True)
            return
        if isinstance(node, ast.Name) and node.id in guarded:
            how = "reads" if isinstance(node.ctx, ast.Load) else "binds"
            problems.append(f"line {node.lineno}: {how} {node.id} bare")
        elif (
            isinstance(node, ast.Name) and node.id == FACADE and not isinstance(node.ctx, ast.Load)
        ):
            problems.append(f"line {node.lineno}: rebinds {FACADE}")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound in guarded or alias.name in guarded:
                    problems.append(f"line {node.lineno}: imports {alias.name} as {bound}")
                if isinstance(node, ast.Import) and alias.name == FACADE and alias.asname:
                    problems.append(f"line {node.lineno}: aliases {FACADE} as {alias.asname}")
        elif isinstance(node, ast.ExceptHandler) and node.name in guarded:
            problems.append(f"line {node.lineno}: binds {node.name}")
        elif isinstance(node, ast.Attribute) and node.attr in guarded:
            owner = node.value.id if isinstance(node.value, ast.Name) else None
            if owner == FACADE and not call_time:
                problems.append(f"line {node.lineno}: reads {FACADE}.{node.attr} at import time")
            elif owner in other_modules:
                problems.append(f"line {node.lineno}: reads {node.attr} through {owner}")
        for child in ast.iter_child_nodes(node):
            visit(child, call_time)

    # A patched function is DEFINED in its owner (a ``def``, which is not a Name) and
    # re-exported by the script: that definition is the one binding it may have, and every
    # caller, the owner's own included, goes through the script.
    for statement in tree.body:
        visit(statement, False)
    return problems


def test_the_reach_guard_catches_each_way_an_owner_could_miss_a_patch() -> None:
    guarded = frozenset({"KNOB", "seam"})
    clean = "import runner_watchdog\n\ndef f():\n    return runner_watchdog.KNOB, runner_watchdog.seam()\n"
    assert reach_violations(clean, guarded) == []
    planted = {
        "a bare read": "def f():\n    return KNOB\n",
        "a bare call between owners": "def f():\n    return seam()\n",
        "a from-import": "from runner_watchdog import KNOB\n",
        "a module-level copy": "import runner_watchdog\nKNOB = runner_watchdog.KNOB\n",
        "a default": "import runner_watchdog\ndef f(x=runner_watchdog.KNOB):\n    return x\n",
        "a decorator": "import runner_watchdog\n@runner_watchdog.seam\ndef f():\n    pass\n",
        "a class-level read": "import runner_watchdog\nclass C:\n    k = runner_watchdog.KNOB\n",
        "an aliased script": "import runner_watchdog as rw\ndef f():\n    return rw.KNOB\n",
        "a rebound script": "import runner_watchdog\nrunner_watchdog = None\n",
        "a parameter": "def f(KNOB):\n    return 1\n",
        "an aliased from-import": "from runner_watchdog import KNOB as _KNOB\n",
        "an aliased owner import": "from runner_watchdog_parts.low import seam as _seam\n",
        "a read through an owner": (
            "from runner_watchdog_parts import low\ndef f():\n    return low.seam()\n"
        ),
        "a read through an aliased owner": (
            "import runner_watchdog_parts.low as low\ndef f():\n    return low.KNOB\n"
        ),
    }
    missed = [label for label, source in planted.items() if not reach_violations(source, guarded)]
    assert missed == []


def test_no_owner_binds_or_reads_a_patched_name_except_through_the_script() -> None:
    guarded = guarded_names()
    found = {
        owner: problems
        for owner in LAYER
        if (problems := reach_violations(_owner_text(owner), guarded))
    }
    assert found == {}


def _code_objects(code: types.CodeType) -> list[types.CodeType]:
    out = [code]
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            out.extend(_code_objects(const))
    return out


def _owner_functions(module: types.ModuleType) -> list[types.FunctionType]:
    functions: list[types.FunctionType] = []
    for value in vars(module).values():
        if isinstance(value, types.FunctionType) and value.__module__ == module.__name__:
            functions.append(value)
        elif isinstance(value, type) and value.__module__ == module.__name__:
            for member in vars(value).values():
                func = getattr(member, "__func__", member)
                if isinstance(func, types.FunctionType):
                    functions.append(func)
                elif isinstance(member, property) and member.fget is not None:
                    functions.append(member.fget)
    return functions


def _late_reads(function: types.FunctionType, guarded: frozenset[str]) -> tuple[set[str], set[str]]:
    """(guarded names read as ``runner_watchdog.<name>``, guarded names read any other way:
    as a global, or as an attribute of a module other than the script)."""
    late: set[str] = set()
    missed: set[str] = set()
    for code in _code_objects(function.__code__):
        previous: dis.Instruction | None = None
        for instruction in dis.get_instructions(code):
            if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"} and instruction.argval in guarded:
                missed.add(str(instruction.argval))
            if (
                instruction.opname == "LOAD_ATTR"
                and instruction.argval in guarded
                and previous is not None
                and previous.opname == "LOAD_GLOBAL"
            ):
                held = function.__globals__.get(str(previous.argval))
                if previous.argval == FACADE:
                    late.add(str(instruction.argval))
                elif isinstance(held, types.ModuleType):
                    missed.add(f"{previous.argval}.{instruction.argval}")
            previous = instruction
    return late, missed


def test_every_loaded_owner_function_reads_a_patched_name_from_the_loaded_script() -> None:
    """The compiled half of the guard, over the modules the tests actually run: no owner
    function loads a guarded name as a global or off another module, and each one that
    reads one through ``runner_watchdog`` holds the very module a test patches."""
    guarded = guarded_names()
    reached: set[str] = set()
    for owner in LAYER:
        for function in _owner_functions(_owner_module(owner)):
            late, missed = _late_reads(function, guarded)
            where = f"{owner}.{function.__qualname__}"
            assert missed == set(), f"{where} reads {sorted(missed)} past the script"
            if late:
                assert function.__globals__[FACADE] is wd, where
                reached |= late
    assert reached, "no owner reads a patched name: the late-bound half checks nothing"


def test_a_patched_knob_reaches_the_owner_that_reads_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """``drop_ghost_runs`` lives in ``listing``; ``GHOST_AFTER`` stays on the script."""
    ghost = _run(10, minutes_ago=5 * 24 * 60, status="queued")
    ghost["run_started_at"] = ghost["created_at"]
    policy = _policy()
    assert wd.drop_ghost_runs([dict(ghost)], policy, lambda _l: None) == []
    monkeypatch.setattr(wd, "GHOST_AFTER", timedelta(days=3650))
    kept = wd.drop_ghost_runs([dict(ghost)], policy, lambda _l: None)
    assert [run["id"] for run in kept] == [10]


def test_a_patched_function_reaches_its_callers_in_the_owners() -> None:
    """``is_current_run`` is defined in ``supersession`` and called from there; the patch
    on the script is what that call reaches."""
    verdict = wd.RunVerdict(
        run_id=1,
        run_attempt=1,
        head_branch="main",
        head_repo="example-org/example-repo",
        event="push",
        status="in_progress",
        url="https://example.invalid/runs/1",
        age=timedelta(minutes=60),
        verdict=wd.ORPHANED,
        workflow="ci.yml",
    )
    spy = mock.Mock(return_value=False)
    with mock.patch.object(wd, "is_current_run", spy):
        assert wd.supersession_clears_hold(FakeApi({}, {}), _policy(), verdict, lambda _l: None)
    assert spy.call_count == 1


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard reads every test module, so a leaf-only diff must still run it."""
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    # corpus_gates() keeps this file because it joins the test directory, here and in
    # guarded_names(); this assertion pins that.
    this_file = (ROOT / "test" / Path(__file__).name).relative_to(ROOT).as_posix()
    assert this_file in leaf_test_scope.corpus_gates(ROOT)
