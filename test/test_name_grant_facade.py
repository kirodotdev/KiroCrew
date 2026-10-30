"""``kiro_crew.name_grant`` keeps the patch seams and the surface the one module had.

``kiro_crew.name_grant`` is a plain facade over three owners, lowest layer first:
``name_grant_refusals`` (the refusal vocabulary and the decline-log dedupe),
``name_grant_tables`` (the fixed name tables) and ``name_grant_lookup`` (how the modelled
shell looks a name up and which file answers it). It keeps the verdicts, the tokenizer, the
pin store and the entry points itself, and re-exports every name an owner defines, so
``vars(name_grant)`` still holds every definition the one module held.

A plain facade reaches moved code only through the names its own code reads: a patch of
``name_grant.<name>`` rebinds this module's global and no owner's. So the property these
tests pin is that no owner function reads a name the test trees patch through the facade
-- every such reader stayed here -- and the scan that proves it can fail and finds the
seams it is known to have. They also pin the owner list and its layering, that the facade
binds each owner-defined name as the owner's own object, that no two owners define one
name, that every public name the module held still type-checks through it, and that an
owner that logs uses the facade's logger.
"""

from __future__ import annotations

import ast
import dis
import functools
import importlib
import sys
import types
from pathlib import Path

import facade_mirror_scan
import pytest
import source_corpus
import test_campaign_facades_base_public_names as base_names

from kiro_crew import _facade_mirror, name_grant

pytestmark = pytest.mark.xdist_group(name="tree_scan_name_grant_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "spelling_cases", "corpus_gate")

FACADE = name_grant.__name__
OWNERS = (
    "kiro_crew.name_grant_refusals",
    "kiro_crew.name_grant_tables",
    "kiro_crew.name_grant_lookup",
)
_SRC = Path(name_grant.__file__).resolve().parent
_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "LOAD_NAME"})

#: Names the test trees are known to patch through the facade, one or more per kind of
#: seam: the agent-path helpers ``_program_refusal`` reads, the environment checks
#: ``environment_refusal`` reads, the pin store, the entry points the surfaces call, and a
#: name patched through the Slack gateway's binding of the module. The scan below must find
#: all of them, so a scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_PINS",
        "_agent_search_path",
        "_agent_writable_roots",
        "_inherited_preload",
        "_path_is_ambiguous",
        "log_decline",
        "pin_human_approval",
        "refusal_for_command_off_loop",
        "refusal_for_event",
    }
)

#: Every name the one-module file defined that an owner holds now, by owner: each stays bound
#: on the facade as the owner's own object (names an owner gains later are not pinned).
_MOVED: dict[str, frozenset[str]] = {
    "kiro_crew.name_grant_refusals": frozenset("""
        AGENT_TREE AMBIGUOUS_ENV AMBIGUOUS_PATH BUILTIN_SHADOWS DISPATCHER EXPANDED
        FILE_ASSOCIATION IDENTITY_CHANGED RELATIVE_PATH Refusal SHADOWED
        UNENUMERABLE UNINSPECTABLE UNKNOWN_COMMAND UNTOKENIZABLE UNWITNESSED
        WINDOWS_UNMODELLED _DECLINE_NOTICES _DECLINE_NOTICE_LIMIT
        _DECLINE_NOTICE_LOCK _PLATFORM_SCOPE_CODES _REFUSAL_LOG_TEXT _notice_digest
        platform_scope_notice should_log_decline windows_environment_refusal
        """.split()),
    "kiro_crew.name_grant_tables": frozenset("""
        MODELLED_WINDOWS_SHELL _BASH_FUNC_KEY_PREFIX _BASH_FUNC_VALUE_PREFIX
        _COMSPEC_SCRIPT_EXTENSIONS _DEFAULT_PATHEXT _DISPATCHERS _ENV_PRELOAD_VARS
        _EXPANDING_CHARS _INERT_BUILTINS _POWERSHELL_CORE_COMMANDS
        _POWERSHELL_DEFAULT_ALIASES _WINDOWS_DISPATCHERS _WINDOWS_EXPANDING_CHARS
        _WINDOWS_INERT_BUILTINS _WINDOWS_RUNNABLE_EXTENSIONS _inherited_preload
        """.split()),
    "kiro_crew.name_grant_lookup": frozenset("""
        _ASSIGN_NAME_RE _COMPLEX_ENV_SHEBANG _DIGEST_CHUNK _EXEC_ENV_PREFIXES
        _EXEC_ENV_SUFFIXES _EXEC_ENV_VARS _INTERPRETER_DEPTH _content_digest
        _decides_execution _identity _is_absolute _is_regular_file
        _is_trusted_system_file _model_basename _model_is_windows _model_normcase
        _model_sep_in _program_key _shebang_interpreter
        _windows_case_insensitive_file _windows_extension_refusal
        _windows_extensions _windows_which
        """.split()),
}


def _owners() -> list[types.ModuleType]:
    return [importlib.import_module(name) for name in OWNERS]


def _tree(module: types.ModuleType) -> ast.Module:
    assert module.__file__ is not None
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def _global_reads(code: types.CodeType):
    """Every global a code object and the code objects nested in it read."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in _GLOBAL_OPS:
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_reads(constant)


def _owner_reads() -> dict[str, set[str]]:
    """Owner -> every global its functions read and its module body reads."""
    reads: dict[str, set[str]] = {}
    for owner in _owners():
        assert owner.__file__ is not None
        source = Path(owner.__file__).read_text(encoding="utf-8")
        names = set(_global_reads(compile(source, owner.__file__, "exec")))
        reads[owner.__name__] = names
    return reads


# --------------------------------------------------------------------------- #
# The patch scan, and no owner reading a patched name
# --------------------------------------------------------------------------- #


@functools.lru_cache(maxsize=1)
def _spellings() -> dict[str, str]:
    """The facade's dotted name, plus ``<module>.name_grant`` for each src module that binds
    it at module level -- another spelling of the same object a test can patch through. A
    split module's facade that serves the name from an owner lists it in its module-level
    ``if TYPE_CHECKING:`` manifest, so that block counts as module level too."""
    spellings = {FACADE: FACADE}
    for path, text in source_corpus.iter_candidate_sources(require_all=("name_grant",)):
        if not path.is_relative_to(_SRC):
            continue
        module = ".".join(path.relative_to(_SRC.parent).with_suffix("").parts)
        if module.endswith(".__init__"):
            module = module.rpartition(".")[0]
        body = ast.parse(text).body
        for node in list(body):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name):
                if node.test.id == "TYPE_CHECKING":
                    body.extend(node.body)
        for node in body:
            if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew" and not node.level:
                for alias in node.names:
                    if alias.name == "name_grant":
                        spellings[f"{module}.{alias.asname or alias.name}"] = FACADE
    return spellings


def patched_names(source: str) -> set[str]:
    return facade_mirror_scan.patched_names(source, _spellings()).get(FACADE, set())


#: Each spelling the patch scan must read, and each it must skip, keyed by case id.
_SPELLING_CASES = {
    "monkeypatch": (
        "from kiro_crew import name_grant\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(name_grant, 'A', 1)\n",
        {"A"},
    ),
    "mock-patch-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.name_grant.B'):\n        pass\n",
        {"B"},
    ),
    "gateway-binding-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.slack.gateway.name_grant.C'):\n        pass\n",
        {"C"},
    ),
    "helper-binding": (
        "from kiro_crew import llm_helpers\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(llm_helpers.name_grant, 'D', 1)\n",
        {"D"},
    ),
    "owner": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.name_grant_lookup.E'):\n        pass\n",
        set(),
    ),
}


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    test_root = root / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root) or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "name_grant" not in text:
            continue
        try:
            found |= patched_names(text)
        except SyntaxError:
            continue
    return frozenset(found)


def _owner_reach_gaps(patched: set[str], reads: dict[str, set[str]]) -> dict[str, list[str]]:
    return {owner: sorted(names & patched) for owner, names in reads.items() if names & patched}


def test_no_owner_reads_a_name_the_tests_patch_through_the_facade() -> None:
    """A patch of ``name_grant.<name>`` rebinds this module's global alone, so a reader of
    that name in an owner would keep the unpatched object and the test would pass while
    exercising nothing. Every reader of a patched name lives here."""
    assert _owner_reach_gaps(set(_corpus_patched_names()), _owner_reads()) == {}


def test_the_reach_check_can_fail() -> None:
    """Plant a patch of a name an owner reads, and the gap shows."""
    reads = _owner_reads()
    owner, names = next((o, n) for o, n in reads.items() if "_model_normcase" in n)
    assert _owner_reach_gaps({"_model_normcase"}, {owner: names}) == {owner: ["_model_normcase"]}


def test_a_facade_patch_reaches_the_facade_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seam a patched name serves: ``environment_refusal`` reads ``_path_is_ambiguous``
    and the tables owner's ``_inherited_preload`` through this module's globals, so a patch
    through the facade decides its verdict."""
    monkeypatch.setattr(name_grant.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(name_grant, "_path_is_ambiguous", lambda: True)
    refusal = name_grant.environment_refusal()
    assert refusal is not None and refusal.code == name_grant.AMBIGUOUS_PATH
    monkeypatch.setattr(name_grant, "_path_is_ambiguous", lambda: False)
    monkeypatch.setattr(name_grant, "_inherited_preload", lambda: "BASH_ENV")
    refusal = name_grant.environment_refusal()
    assert refusal is not None and refusal.code == name_grant.AMBIGUOUS_ENV


# --------------------------------------------------------------------------- #
# The owners and what the facade binds
# --------------------------------------------------------------------------- #


def test_the_owners_are_every_name_grant_sibling() -> None:
    siblings = sorted(f"kiro_crew.{path.stem}" for path in _SRC.glob("name_grant_*.py"))
    assert siblings == sorted(OWNERS)


def _project_imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _layering_gaps(trees: dict[str, ast.Module]) -> dict[str, list[str]]:
    """Owner -> the family modules it imports that it may not: the facade, or an owner
    listed at or after it."""
    gaps: dict[str, list[str]] = {}
    for position, owner in enumerate(OWNERS):
        allowed = set(OWNERS[:position])
        reached = _project_imports(trees[owner]) & {FACADE, *OWNERS}
        bad = sorted(reached - allowed)
        if bad:
            gaps[owner] = bad
    return gaps


def test_each_owner_imports_only_owners_listed_before_it_and_never_the_facade() -> None:
    trees = {owner.__name__: _tree(owner) for owner in _owners()}
    assert _layering_gaps(trees) == {}


def test_the_layering_check_can_fail() -> None:
    trees = {owner.__name__: _tree(owner) for owner in _owners()}
    trees[OWNERS[0]] = ast.parse("from kiro_crew.name_grant_lookup import _identity\n")
    trees[OWNERS[1]] = ast.parse("from kiro_crew import name_grant\n")
    assert _layering_gaps(trees) == {OWNERS[0]: [OWNERS[2]], OWNERS[1]: [FACADE]}


def test_the_facade_binds_every_moved_name_as_the_owners_object() -> None:
    """``name_grant.py`` held these definitions in its own namespace, and still does."""
    assert sum(len(names) for names in _MOVED.values()) >= 65
    unbound = [
        f"{owner}.{name}"
        for owner, names in _MOVED.items()
        for name in sorted(names)
        if vars(name_grant).get(name, object()) is not vars(sys.modules[owner]).get(name)
    ]
    assert unbound == []


def test_no_two_owners_define_the_same_name_and_none_redefines_a_facade_name() -> None:
    homes: dict[str, list[str]] = {}
    for owner in _owners():
        for name in _facade_mirror._defined_names(_tree(owner)):
            homes.setdefault(name, []).append(owner.__name__)
    facade_defined = _facade_mirror._defined_names(_tree(name_grant))
    assert {name: owners for name, owners in homes.items() if len(owners) > 1} == {}
    assert sorted(set(homes) & facade_defined) == []


def _statically_bound(tree: ast.Module) -> set[str]:
    names = set(_facade_mirror._defined_names(tree))
    for stmt in _facade_mirror._module_level_statements(tree):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names |= {alias.asname or alias.name.split(".")[0] for alias in stmt.names}
    return names


def test_every_base_public_name_type_checks_through_the_facade() -> None:
    missing = base_names._BASE_PUBLIC_NAMES[FACADE] - _statically_bound(_tree(name_grant))
    assert sorted(missing) == []


def test_an_owner_that_logs_logs_under_the_facade_name() -> None:
    """Every reader of ``logger`` stayed in the facade, whose records keep the
    ``kiro_crew.name_grant`` name; an owner that reads a logger binds that same one."""
    assert name_grant.logger.name == FACADE
    reads = _owner_reads()
    for owner in _owners():
        if "logger" in reads[owner.__name__]:
            assert vars(owner).get("logger") is name_grant.logger, owner.__name__
