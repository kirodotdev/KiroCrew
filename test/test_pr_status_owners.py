"""``pr_status.py`` as a facade over two owner modules: its surface, its seams, its run.

``pr_status.py`` loads ``pr_status_issue_links.py`` and ``pr_status_verdict.py`` with its
own no-bytecode ``_load_sibling`` and re-exports every name they define. Each section below
pins one property the arrangement has to keep:

* every name a test replaces on the facade is defined there and read by no owner, so the
  replacement reaches every reader;
* every name the one-file script bound is still bound on the facade, a re-export is the
  owner's own object, and the only definitions in two files are the verdict owner's
  byte-identical twin of the facade's loader;
* one load holds one review contract: the facade's ``_review_contract`` is the one the
  verdict owner loaded;
* an owner imports only the standard library (plus, for the verdict, the contract it
  loads by path), is named under the facade's own package, and is never registered in
  ``sys.modules``;
* a bare copy of the scripts runs under ``-I -S`` and writes no bytecode, and so does the
  readiness sweep's sparse checkout, which holds every file the facade loads.
"""

from __future__ import annotations

import ast
import collections
import functools
import importlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path
from types import ModuleType

import pytest
import source_corpus
import yaml
from prepare_pr_status_helpers import SCRIPT
from skill_script_helpers import load_skill_script

# One walk of the test tree, shared by this module's cases on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_pr_status_owners")

SCRIPTS = SCRIPT.parent
REPO = Path(__file__).resolve().parents[1]
#: The readiness sweep runs ``pr_status.py --disposition-gate`` from a sparse checkout
#: that holds only the files this workflow lists.
SWEEP = REPO / ".github" / "workflows" / "pr-readiness-sweep.yml"
#: Owner stem -> the facade attribute its module is bound to.
OWNERS = {"pr_status_issue_links": "_issue_links", "pr_status_verdict": "_verdict"}
OWNER_PATHS = {stem: SCRIPTS / f"{stem}.py" for stem in OWNERS}
#: The loader units the verdict owner carries as a byte-identical twin of the facade's.
LOADER_TWIN = ("_NoBytecodeSourceLoader", "_load_sibling")
#: A dotted load name OUTSIDE ``kiro_crew``: a ``kiro_crew.`` prefix would make a
#: coverage run start measuring these files from this module's partial use of them.
DOTTED = "s56_probe.pr_status"

#: Every module-level name ``pr_status.py`` defined as one file, before its split: each
#: ``def``, ``class`` and assigned name, imports excluded. A caller reads these off the
#: module, so each must still be there.
BASE_NAMES = (
    "BLOCKING_LINE_RE",
    "BLOCK_MERGE_RE",
    "BODY_SHA_RE",
    "CTX_PASS",
    "CTX_RUNNING",
    "DEFAULT_MARKER_AUTHORS",
    "DEFAULT_MARKER_BINDINGS",
    "DEFAULT_READINESS_CONTEXT",
    "DISPOSITION_BULLET_RE",
    "DISPOSITION_MARKER_RE",
    "DISPOSITION_PREFIX",
    "FINDING_RE",
    "PASS_CONCLUSIONS",
    "REVIEWED_STAMP_RE",
    "ROLLUP_HEAD_MOVED_NOTICE",
    "ROLLUP_QUERY",
    "ROLLUP_UNAVAILABLE_NOTICE",
    "SPAN_CLAIM_RE",
    "VERDICT_LINE_RE",
    "WHOLE_DESIGN_LANES",
    "_BARE_REF_RE",
    "_BOOL_FLAGS",
    "_CLOSING_KW_RE",
    "_CLOSING_REF",
    "_CLOSING_REF_RE",
    "_CLOSING_VERB",
    "_COMMENT_KEY_RE",
    "_CTRL_RE",
    "_DECLARING_LINE_START_RE",
    "_DECLARING_REF_RE",
    "_DECLARING_VERB",
    "_FENCE_OPEN_RE",
    "_GATE_CALL_TIMEOUT_SECS",
    "_GATE_TOTAL_BUDGET_SECS",
    "_GITHUB_ISSUE_NUMBER_MAX",
    "_GITHUB_ISSUE_NUMBER_MAX_TEXT",
    "_ISSUE_TARGET",
    "_MAX_COMMENT_PAGES",
    "_MAX_ROLLUP_PAGES",
    "_MAX_THREAD_PAGES",
    "_NoBytecodeSourceLoader",
    "_REPO_SLUG",
    "_VALUE_FLAGS",
    "_declared_closing_numbers",
    "_displaced",
    "_fence_opening",
    "_flag_value",
    "_green_age",
    "_host_id",
    "_host_ref_repo_key",
    "_is_closing_fence",
    "_issue_number_sort_key",
    "_load_sibling",
    "_mask_html_comments",
    "_mask_inline_code",
    "_normalize_issue_number",
    "_normalize_repo_key",
    "_review_contract",
    "_undeclared_closing_numbers",
    "_visible_markdown_prose",
    "author_is_repo_writer",
    "author_write_verdict",
    "bounded_run",
    "build_report",
    "classify_check",
    "closing_link_reason",
    "collapse_superseded",
    "comment_key",
    "decide",
    "declared_issue_numbers",
    "design_lane_punchlines",
    "design_lane_verdicts",
    "detect_repo",
    "disposition_gate",
    "disposition_violations",
    "err",
    "evaluate_reviewer_markers",
    "extract_design_items",
    "extract_findings",
    "failing_check_identity",
    "fetch_bot_comments",
    "fetch_check_rollup",
    "fetch_disposition_comments",
    "fetch_issue_comments",
    "flatten_rollup_row",
    "green_age_line",
    "head_run_check_enabled",
    "head_run_exists",
    "human_override_actors",
    "is_trusted_bot_comment",
    "main",
    "parse_disposition_record",
    "positional_args",
    "probe_green_age",
    "redact",
    "resolve_marker_authors",
    "resolve_marker_bindings",
    "resolve_readiness_context",
    "resolve_reviewers",
    "reviewer_round_settled",
    "run",
    "sanitize",
    "select_bot_comments",
    "sha_matches",
    "span_hash",
    "superseded_key",
    "superseded_verdicts",
    "supersession_gate",
    "unresolved_thread_count",
    "writer_disposition_records",
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _module_level_bindings(tree: ast.Module) -> list[tuple[str, ast.stmt]]:
    """Every name a module binds at top level: a ``def`` or ``class``, and each plain
    name an assignment targets."""
    bound: list[tuple[str, ast.stmt]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            bound += [(t.id, node) for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.append((node.target.id, node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.append((node.name, node))
    return bound


def _reexport_source(node: ast.stmt) -> str | None:
    """The owner alias a ``NAME = _owner.NAME`` binding reads from, or None.

    Only an OWNER's alias counts: the facade's ``NAME = _review_contract.NAME`` lines
    are the one-file script's own aliases of the contract, defined here as before.
    """
    if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
        return None
    target, value = node.targets[0], node.value
    if (
        isinstance(target, ast.Name)
        and isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.attr == target.id
        and value.value.id in OWNERS.values()
    ):
        return value.value.id
    return None


def _owner_defined(stem: str) -> set[str]:
    return {name for name, _node in _module_level_bindings(_tree(OWNER_PATHS[stem]))}


def _facade_defined() -> set[str]:
    """Names the facade defines itself: every binding that is not a re-export."""
    return {
        name
        for name, node in _module_level_bindings(_tree(SCRIPT))
        if _reexport_source(node) is None
    }


@pytest.fixture(name="facade")
def facade_fixture() -> ModuleType:
    return load_skill_script("pr_status_owners_facade", SCRIPT)


# --------------------------------------------------------------------------
# Patch reach: a replaced name is the facade's own and read by no owner
# --------------------------------------------------------------------------

_PATCH_CALLS = frozenset({"setattr", "delattr", "object"})
_IMPORTED_MODULE = re.compile(r"^[ \t]*(?:from[ \t]+(\w+)[ \t]+import|import[ \t]+(\w+))", re.M)


def patched_names_in(source: str) -> set[str]:
    """Names *source* replaces on a module held in a plain variable.

    ``monkeypatch.setattr(mod, "X", ...)``, ``monkeypatch.delattr(mod, "X")``,
    ``mock.patch.object(mod, "X", ...)``, ``monkeypatch.setitem(vars(mod), "X", ...)``
    and a plain ``mod.X = ...``. A first argument that is itself an attribute
    (``mod.os``) replaces something on ANOTHER module, which every reader sees, so it
    is not a facade seam and is left out.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            found |= {
                target.attr
                for target in node.targets
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
            }
            continue
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        target = node.args[0]
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "setitem"
            and isinstance(target, ast.Call)
            and getattr(target.func, "id", None) == "vars"
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            found.add(node.args[1].value)
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        target, attr = node.args[0], node.args[1]
        if (
            name in _PATCH_CALLS
            and isinstance(target, ast.Name)
            and isinstance(attr, ast.Constant)
            and isinstance(attr.value, str)
        ):
            found.add(attr.value)
    return found


def _test_tree_files() -> list[Path]:
    tree_root = source_corpus.repo_root() / "test"
    return [
        path for path in source_corpus.repo_files_named(".py") if path.is_relative_to(tree_root)
    ]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def names_the_script(source: str, filename: str) -> bool:
    """Whether *source* spells the script's file as a string of its own: ``"pr_status.py"``
    or a path ending in it, as a loader writes it -- never a mention inside prose."""
    if filename not in source:
        return False
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return False
    for token in tokens:
        if token.type != tokenize.STRING:
            continue
        try:
            value = ast.literal_eval(token.string)
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, str) and (value == filename or value.endswith("/" + filename)):
            return True
    return False


def loading_files(texts: dict[Path, str], filename: str) -> set[Path]:
    """The files that load the script: each that names its file, and each that imports
    a module that does -- most suites reach it only through a helper module."""
    naming = {path for path, text in texts.items() if names_the_script(text, filename)}
    stems = {path.stem for path in naming}
    loading = set(naming)
    for path, text in texts.items():
        if path not in naming:
            imported = {a or b for a, b in _IMPORTED_MODULE.findall(text)}
            if imported & stems:
                loading.add(path)
    return loading


@functools.lru_cache(maxsize=1)
def _corpus() -> tuple[frozenset[Path], dict[str, frozenset[Path]]]:
    """The test files that load the script, and what each replaces on it.

    Read from the tests themselves, so a case added anywhere in the tree is covered
    without being registered here. A replaced attribute counts when the loaded facade
    has an attribute of that name -- a module-valued one included -- and the same
    spelling on an unrelated module is the safe over-inclusion, never a miss.
    """
    texts = {path: _read(path) for path in _test_tree_files()}
    scanned = loading_files(texts, "pr_status.py")
    facade = load_skill_script("pr_status_owners_corpus", SCRIPT)
    patches: dict[str, set[Path]] = collections.defaultdict(set)
    for path in scanned:
        for name in patched_names_in(_read(path)):
            if hasattr(facade, name):
                patches[name].add(path)
    return frozenset(scanned), {name: frozenset(paths) for name, paths in patches.items()}


def _reexported() -> set[str]:
    """Names the facade binds as ``NAME = _owner.NAME``."""
    return {
        name
        for name, node in _module_level_bindings(_tree(SCRIPT))
        if _reexport_source(node) is not None
    }


def test_the_corpus_scan_finds_the_seams_the_suite_is_known_to_patch():
    """Non-vacuity: the derivation sees, in the suite itself, replacements it makes
    today -- the gh runner every report test fakes, the thread counter, the gate's
    bounded runner and the green-age probe. This file's own text does not count."""
    this_file = Path(__file__).resolve()
    _scanned, patches = _corpus()
    for name in ("run", "unresolved_thread_count", "bounded_run", "probe_green_age"):
        assert patches.get(name, frozenset()) - {this_file}, name


def test_the_corpus_reads_the_shared_helper_and_every_suite_importing_it():
    """The suites load the script through ``prepare_pr_status_helpers.py``; the scan reads
    the helper and every suite that imports it, whether or not the suite spells the path."""
    scanned, _patches = _corpus()
    names = {path.name for path in scanned}
    assert "prepare_pr_status_helpers.py" in names, sorted(names)
    importers = {
        path.name
        for path in _test_tree_files()
        if "prepare_pr_status_helpers" in {a or b for a, b in _IMPORTED_MODULE.findall(_read(path))}
    }
    assert importers and importers <= names, sorted(importers - names)


def test_the_loader_scan_follows_one_import_and_ignores_prose():
    """On a planted corpus: a helper that spells the path, a suite that only imports it,
    and a suite that merely mentions the file in a docstring."""
    texts = {
        Path("helpers.py"): 'SCRIPT = ROOT / "scripts" / "pr_status.py"',
        Path("test_a.py"): "from helpers import SCRIPT",
        Path("test_b.py"): '"""Pins ``pr_status.py`` and its report."""',
        Path("test_c.py"): 'SCRIPT = "src/x/pr_status.py"',
    }
    assert loading_files(texts, "pr_status.py") == {
        Path("helpers.py"),
        Path("test_a.py"),
        Path("test_c.py"),
    }


def test_the_patch_reader_recognises_each_spelling_and_skips_another_modules_attribute():
    planted = (
        'monkeypatch.setattr(mod, "_a", 1)\n'
        "monkeypatch.setattr(\n    mod,\n    '_b',\n    2,\n)\n"
        'monkeypatch.delattr(status, "_c")\n'
        'mock.patch.object(status, "_d", 3)\n'
        'monkeypatch.setattr(mod.os, "readlink", 4)\n'
        'monkeypatch.setattr("pkg.mod._e", 5)\n'
        'monkeypatch.setitem(vars(mod), "_f", 6)\n'
        "mod._g = 7\n"
        "mod.os.environ = {}\n"
    )
    assert patched_names_in(planted) == {"_a", "_b", "_c", "_d", "_f", "_g"}


def test_no_patched_name_is_a_re_export_or_read_by_an_owner():
    """A replacement rebinds the facade's own global, and only code reading THAT global
    sees it. A re-exported name leaves the owner's original in force for every owner
    caller, and an owner that reads the name -- its own import of a module included --
    never sees the replacement, so both are refused."""
    patched = set(_corpus()[1])
    reexported = sorted(patched & _reexported())
    assert not reexported, f"replaced on the facade but defined in an owner: {reexported}"
    for stem, path in OWNER_PATHS.items():
        read = set()
        for name, node in _units(_tree(path)):
            if name not in _IMPORT_TIME_ONLY:
                read |= {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        assert read, f"{stem}: read no names, so this guard proves nothing"
        assert not patched & read, f"{stem} reads a replaced name: {sorted(patched & read)}"


#: The verdict owner's loader prelude runs once, while the facade loads it, before any
#: test holds the module to replace a name on: the loader twin and the contract load.
_IMPORT_TIME_ONLY = frozenset({*LOADER_TWIN, "_review_contract"})


def _units(tree: ast.Module) -> list[tuple[str, ast.stmt]]:
    """Each top-level statement with the name it binds ("" for one that binds none)."""
    out = []
    for node in tree.body:
        names = [n for n, bound in _module_level_bindings(ast.Module(body=[node], type_ignores=[]))]
        out.append((names[0] if names else "", node))
    return out


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(monkeypatch):
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    this_file = (root / "test" / Path(__file__).name).relative_to(root).as_posix()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert this_file in leaf_test_scope.corpus_gates(root)


def test_a_replaced_runner_reaches_the_rollup_read_the_facade_keeps(facade):
    """The behaviour the placement buys: the facade's own ``fetch_check_rollup`` reads
    the facade's ``run``, so replacing it there decides what the read sees."""
    calls = []

    def fake_run(args):
        calls.append(args)
        return 1, "", "no"

    facade.run = fake_run
    rows, notice = facade.fetch_check_rollup("example/repo", 42, "f" * 40)
    assert calls and rows == [] and notice == facade.ROLLUP_UNAVAILABLE_NOTICE


# --------------------------------------------------------------------------
# Surface: the one file's names, re-exports that are the owner's objects
# --------------------------------------------------------------------------


def test_every_name_the_one_file_script_bound_is_still_bound_on_the_facade(facade):
    missing = [name for name in BASE_NAMES if not hasattr(facade, name)]
    assert not missing, missing


def test_every_one_file_name_is_defined_in_exactly_one_file():
    """Nothing was dropped in the move and nothing was copied: each name the one file
    defined is defined once, in the facade or in one owner. The verdict owner's loader
    twin is the one deliberate copy, and the next test pins it byte-identical. Names
    added later are free to land in any file."""
    verdict = _owner_defined("pr_status_verdict") - set(LOADER_TWIN)
    defined = collections.Counter(
        [*_facade_defined(), *_owner_defined("pr_status_issue_links"), *verdict]
    )
    wrong = {name: defined[name] for name in BASE_NAMES if defined[name] != 1}
    assert not wrong, wrong


def test_the_verdict_owners_loader_is_a_byte_identical_twin_of_the_facades():
    """The verdict owner loads the review contract the way the facade loads its
    siblings. Two copies of one loader may only ever be the same text."""
    facade_source = SCRIPT.read_text(encoding="utf-8")
    owner_source = OWNER_PATHS["pr_status_verdict"].read_text(encoding="utf-8")
    facade_nodes = dict(_module_level_bindings(ast.parse(facade_source)))
    owner_nodes = dict(_module_level_bindings(ast.parse(owner_source)))
    for name in LOADER_TWIN:
        assert ast.get_source_segment(owner_source, owner_nodes[name]) == ast.get_source_segment(
            facade_source, facade_nodes[name]
        ), name


def test_every_moved_name_is_re_exported_as_the_owners_own_object(facade):
    """A caller reads a moved name off the facade, so the facade's binding must be the
    owner's object itself, not a copy the owner's own callers would not see."""
    for stem, alias in OWNERS.items():
        owner = getattr(facade, alias)
        for name in sorted((_owner_defined(stem) & set(BASE_NAMES)) - set(LOADER_TWIN)):
            if name == "_review_contract":
                continue
            assert vars(facade)[name] is vars(owner)[name], (stem, name)


def test_the_only_name_bound_in_two_files_is_a_re_export_or_the_loader_twin():
    """Across the three files a name may be bound twice in two ways only: an owner
    defines it and the facade binds ``NAME = <that owner's alias>.NAME``, or it is one
    of the verdict owner's loader units."""
    by_alias = {alias: stem for stem, alias in OWNERS.items()}
    owners_of: dict[str, list[str]] = collections.defaultdict(list)
    for stem in OWNERS:
        for name in _owner_defined(stem):
            owners_of[name].append(stem)
    assert not {n: s for n, s in owners_of.items() if len(s) > 1}, "defined in both owners"
    for stem, path in OWNER_PATHS.items():
        counts = collections.Counter(name for name, _node in _module_level_bindings(_tree(path)))
        assert counts, f"{stem}: parsed no definitions, so this guard proves nothing"
        assert not {n: c for n, c in counts.items() if c > 1}, stem
    facade_bindings = _module_level_bindings(_tree(SCRIPT))
    seen = collections.Counter(name for name, _node in facade_bindings)
    assert not {n: c for n, c in seen.items() if c > 1}, "bound twice in the facade"
    for name, node in facade_bindings:
        alias = _reexport_source(node)
        if name in owners_of and name not in LOADER_TWIN:
            assert alias is not None and by_alias.get(alias) == owners_of[name][0], name
        else:
            assert alias is None, f"{name} re-exports a name no owner defines"


def test_one_load_holds_one_review_contract(facade):
    """The facade takes the contract the verdict owner loaded, so ``decide`` and the
    facade's own readers answer from the same module and its aliases are its objects."""
    contract = facade._review_contract
    assert contract is facade._verdict._review_contract
    assert Path(contract.__file__).resolve() == (SCRIPTS / "_review_contract.py").resolve()
    assert contract.__name__ == "_prepare_pr_review_contract"
    assert facade.REVIEWED_STAMP_RE is contract.REVIEWED_STAMP_RE


# --------------------------------------------------------------------------
# Loading: standard library only, named after the facade, never registered
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stem", sorted(OWNERS))
def test_an_owner_imports_only_the_standard_library(stem):
    """The script is run by a bare interpreter, so an owner can lean on nothing the
    package provides. The contract the verdict owner needs is loaded by path, never
    imported, which keeps it off ``sys.path`` too."""
    imported = set()
    for node in ast.walk(_tree(OWNER_PATHS[stem])):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{stem}: relative import"
            imported.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
    assert imported, f"{stem}: found no imports, so this guard proves nothing"
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, sorted(imported)


def test_a_dotted_load_names_the_owners_under_the_same_package_and_registers_nothing():
    """The name is what a coverage run attributes executed lines by, so a dotted harness
    gets its owners under its own prefix."""
    facade = load_skill_script(DOTTED, SCRIPT)
    package = DOTTED.rpartition(".")[0]
    for stem, alias in OWNERS.items():
        owner = getattr(facade, alias)
        assert owner.__name__ == f"{package}.{stem}"
        assert Path(owner.__file__) == OWNER_PATHS[stem]
        assert owner.__name__ not in sys.modules and stem not in sys.modules


def _copy_scripts(tmp_path: Path) -> Path:
    """Every script of the skill, the way the installed skill holds them."""
    scripts = tmp_path / "skills" / "kirocrew-prepare-pr" / "scripts"
    scripts.mkdir(parents=True)
    for path in SCRIPTS.glob("*.py"):
        (scripts / path.name).write_bytes(path.read_bytes())
    return scripts


def test_a_bare_load_names_the_owners_by_their_bare_stem(tmp_path):
    """Loaded from a COPY: coverage decides once per file whether to measure it, so a
    bare-name load of the real files could leave them unmeasured for this worker."""
    facade = load_skill_script("pr_status", _copy_scripts(tmp_path) / "pr_status.py")
    assert {getattr(facade, alias).__name__ for alias in OWNERS.values()} == set(OWNERS)
    assert not set(OWNERS) & set(sys.modules)


def test_loading_the_owners_writes_no_bytecode(tmp_path, monkeypatch):
    """The facade goes through the ORDINARY loader with writing on, so only the
    facade's own loader decides whether an owner leaves bytecode behind."""
    scripts = _copy_scripts(tmp_path)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr(sys, "pycache_prefix", None)
    spec = importlib.util.spec_from_file_location("pr_status_copy", scripts / "pr_status.py")
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    cached = sorted(p.name for p in (scripts / "__pycache__").glob("*"))
    # The control: the facade itself was cached, so the owners' absence below is the
    # facade's loader at work.
    assert [name for name in cached if name.startswith("pr_status.")], cached
    assert not [name for name in cached if name.startswith(tuple(OWNERS))], cached
    assert not [name for name in cached if name.startswith("_review_contract")], cached


def _run_gate(script: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    """``script --disposition-gate`` under ``-I -S`` with an empty ``PATH``. The gate mode
    answers without the network: its arguments are checked first."""
    empty = cwd / "empty-path"
    empty.mkdir(exist_ok=True)
    env = {"PATH": str(empty)}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return subprocess.run(
        [sys.executable, "-I", "-S", str(script), "--disposition-gate"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(cwd),
        timeout=60,
        check=False,
    )


def _assert_the_gate_answered(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["ok"] is False and "--repo, --pr and --head" in report["error"], report


def test_a_bare_copy_runs_as_a_script_without_the_package_and_writes_no_bytecode(tmp_path):
    """The installed shape: the skill directory synced out of the package and run by an
    interpreter with no site directory and no environment-driven import path. ``-I``
    leaves the script's own directory off ``sys.path``, so the owners are reachable
    only through the facade's by-path load."""
    scripts = _copy_scripts(tmp_path)
    _assert_the_gate_answered(_run_gate(scripts / "pr_status.py", tmp_path))
    assert not list(scripts.rglob("__pycache__")), sorted(scripts.rglob("*"))


def _sweep_checkout() -> list[str]:
    """The repository paths the readiness sweep's sparse checkout holds."""
    steps = yaml.safe_load(SWEEP.read_text(encoding="utf-8"))["jobs"]["sweep"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
    lines = str(checkout["with"]["sparse-checkout"]).splitlines()
    return [line.strip() for line in lines if line.strip()]


def _sparse_tree(root: Path, paths: list[str]) -> Path:
    """The files ``paths`` names, copied under ``root`` at their repository paths; returns
    the scripts directory inside it."""
    for path in paths:
        target = root.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(REPO.joinpath(*path.split("/")).read_bytes())
    return root.joinpath(*SCRIPTS.relative_to(REPO).parts)


def test_the_readiness_sweeps_sparse_checkout_holds_every_file_the_facade_loads(tmp_path):
    """The sweep runs the gate from a checkout of ONLY the files its workflow lists, so an
    owner the facade loads but the list leaves out fails in the sweep and nowhere else.
    Run from that file set, the gate answers. The control drops each file the facade
    loads today, one at a time, and the same run then fails on that file, so the green
    run is the list at work."""
    paths = _sweep_checkout()
    _assert_the_gate_answered(
        _run_gate(_sparse_tree(tmp_path / "sweep", paths) / "pr_status.py", tmp_path)
    )
    prefix = SCRIPTS.relative_to(REPO).as_posix() + "/"
    loaded = ["_review_contract.py", "green_age.py", *(f"{stem}.py" for stem in OWNERS)]
    assert {prefix + name for name in loaded} <= set(paths), paths
    for name in loaded:
        kept = [path for path in paths if path != prefix + name]
        result = _run_gate(
            _sparse_tree(tmp_path / f"without-{name}", kept) / "pr_status.py", tmp_path
        )
        assert result.returncode != 0 and name in result.stderr, (name, result.stderr)
