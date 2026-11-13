"""``kiro_crew.cli_commands`` keeps the patch seams and the surface the one module had.

``kiro_crew.cli_commands`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
``cli_commands_<command>`` owners in its ``_OWNERS``. It keeps the ``tailnet``,
``telemetry``, ``secrets`` and ``pod`` handlers and its whole import block; callers,
tests and documents reach every other handler as ``kiro_crew.cli_commands.<name>``.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules.
These tests pin what is particular to this one: every name the test trees patch through
it is routed to every module holding it, and a live patch of each one that is not a
module object lands there; the owner list, lowest layer first, with no exclusion and no
forwarded table; an owner imports only owners listed before it; the facade's
module-level imports are exactly its import block before the split plus the mirror's
glue, and every import inside one of its functions is read there, which together stand
in for the F401 check ``setup.cfg`` switches off for it; a star import binds what it
bound before; every public name it held still type-checks through it; and the moved
handlers log as, and the moved exception class names, ``kiro_crew.cli_commands``.
"""

from __future__ import annotations

import ast
import functools
import re
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus
import test_campaign_facades_base_public_names as base_names

from kiro_crew import _facade_mirror, cli_commands

pytestmark = pytest.mark.xdist_group(name="tree_scan_cli_commands_split_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = (
    "known_seams",
    "spelling_cases",
    "corpus_gate",
    "reach_routing",
    "reach_scan_can_fail",
)

FACADE = "kiro_crew.cli_commands"
OWNERS = tuple(
    f"kiro_crew.cli_commands_{name}"
    for name in (
        "workspace",
        "memory",
        "gateway_api",
        "app",
        "agent",
        "cron",
        "security",
        "eval",
        "learn",
        "secrets",
    )
)

#: Names the test trees are known to patch through the facade, one or more per kind of
#: seam: an import only owners read (``sel``, ``CronService``), an
#: owner helper another owner function calls (``_internal_secret``, ``_gateway_send``,
#: ``_locked_config_write``, ``_markdown_memory_store``), an owner constant, a module or
#: class rebound on the module (``importlib``, ``Path``), and names only the facade's own
#: handlers read (``beacon``, ``_marker_port``). The scan below must find all of them, so
#: a scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "sel",
        "CronService",
        "config_dir",
        "_gateway_send",
        "_internal_secret",
        "_locked_config_write",
        "_markdown_memory_store",
        "_EXPORT_EVENTS_LIMIT",
        "_interactive_terminal",
        "_memory_verb",
        "importlib",
        "Path",
        "beacon",
        "_marker_port",
    }
)

#: A test file worth parsing spells the facade itself, never only a ``cli_commands_*``
#: sibling.
_SPELLS_THE_FACADE = re.compile(
    r"kiro_crew\.cli_commands(?![\w])"
    r"|from\s+kiro_crew\s+import\s+(?:\([^)]*?\bcli_commands\b(?!_)|[^\n]*?\bcli_commands\b(?!_))"
)

#: Every name the facade's import statements bound before its handlers moved out. They
#: stay bound here because callers and tests read and patch them as attributes of this
#: module, and a patch of one reaches every owner holding it.
_BASE_IMPORTS = frozenset("""
    __version__ _AGENT_NAME_RE _JOB_TIMEOUT_SECS _lesson_display_text _lesson_scope
    _lesson_scope_unusable _marker_port _MODEL_NAME_RE _SUBPROC_CLEANUP_ALLOWANCE_SECS
    _TERMINAL_CTRL_RE _time _vet_cron_capability_governance _vet_script_file _vet_shell_command
    app_lifecycle_client argparse AssertionType asyncio beacon build_provider_factory
    BUILTIN_DENIED_RULES BUILTIN_DENY_PATTERNS Callable CHANNEL_ID_RE CHANNEL_MAX_LEN
    check_crew_label coerce_dict_section config_dir config_local_path config_path
    ConfigReadError contextlib ConversationLog convert_plugin_package Counter crew_teams
    CRON_ADD_SCHEMA CronSchedule CronService CronStoreBusy CronStoreUnreadable dataclass
    dataclasses datetime declared_store DEFAULT_MEMORY_STORE deregister_app
    deregister_app_crons_from_service disable_app discard_app_session_pointers enable_app
    EnvStagingRefused EvalRunner external_text_requires_redaction find_plugin_manifest
    format_report format_results format_schedule get_app get_app_manifest get_local_tz
    get_shared_embedder importlib inspect install_app is_claude_code is_sensitive_path
    is_valid_timezone json key_new_crew KiroCrewAgentConfig KiroCrewConfig LessonStore
    LessonWriteOutcome list_apps LLMJudge load_scenario load_scenarios logging
    lookup_cron_folder_id loopback_urlopen make_sync_embed_fn materialize_workspace_dir
    MAX_SHORT_STRING MemberNameError MemberWorkspaceUnavailable memory_store_binding_defect
    memory_store_namespace_lock memory_store_version MemoryStore migrate_env_secrets
    MigrationConflictError model_file_present model_registry named_store_or_empty NoReturn
    normalize_agent_model normalize_app_name normalize_lesson_category os owned_store_path
    parse_dashboard_url parse_time_string Path persist_member_config platform_compat
    PluginImportError provision_member_memory provision_member_workspace queued_wait_text re
    read_config_for_update read_config_text read_local_secret read_manifest_name
    recorded_backend_port redact redact_credentials redact_exfiltration_urls
    redact_log_via_context register_app register_app_crons_with_service
    require_member_memory_creation require_memory_store reset_agent_model resolve_client_port
    resolve_client_port_ex resolve_declared_store resolve_script_path
    retire_unpublished_allocation retire_unpublished_workspace safe_read_file
    safe_terminal_line scaffold_app scan_history scan_memory scope_is_admissible
    scope_selector_is_inadmissible score_by_dimension sel SessionPointerCleanup shutil sqlite3
    stat StdlibSQLiteError store_embedding_space_is_stale sys tailnet tailnet_serve
    TEMPLATE_NAME_RE timedelta timezone traceback trigger_cron_job trust_grant_removal_blocked
    uninstall_app UnknownMemoryStore update_config_locked urllib uuid validate_managed_by
    validate_member_name VectorMemoryStore WORKSPACE_NAME_RE WorkspaceConfig
    WorkspaceDirUnusable ZoneInfo
    """.split())

#: The names the facade's own glue imports: the mirror, and the ``TYPE_CHECKING`` flag
#: its manifest sits under. The owner loop and the class swap use the BASE ``importlib``
#: and ``sys``.
_GLUE_IMPORTS = frozenset({"TYPE_CHECKING", "_facade_mirror"})


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(cli_commands)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _family() -> list[ModuleType]:
    return [cli_commands, *(sys.modules[name] for name in _index().owners)]


def _source(module: ModuleType) -> str:
    assert module.__file__ is not None
    return Path(module.__file__).read_text(encoding="utf-8")


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(_source(module))


def patched_names(source: str) -> set[str]:
    """Every name *source* patches through the facade, read by the guards' own reader."""
    return facade_mirror_scan.patched_names(source, {FACADE: FACADE}).get(FACADE, set())


# --------------------------------------------------------------------------- #
# The patch scan
# --------------------------------------------------------------------------- #


#: Each spelling the patch scan must read, and each it must skip, keyed by case id.
_SPELLING_CASES = {
    "monkeypatch-from-import": (
        "from kiro_crew import cli_commands\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(cli_commands, 'A', 1)\n",
        {"A"},
    ),
    "patch-object-import-as": (
        "import kiro_crew.cli_commands as cc\n"
        "from unittest import mock\n"
        "def t():\n    with mock.patch.object(cc, 'B'):\n        pass\n",
        {"B"},
    ),
    "mock-patch-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.cli_commands.C'):\n        pass\n",
        {"C"},
    ),
    "dotted-setattr": (
        "def t(monkeypatch):\n    monkeypatch.setattr('kiro_crew.cli_commands.D', 1)\n",
        {"D"},
    ),
    "assignment-and-del": (
        "from kiro_crew import cli_commands as m\ndef t():\n    m.E = 1\n    del m.F\n",
        {"E", "F"},
    ),
    # An owner is not the facade, even bound under the facade's name.
    "owner-bound-as-facade-name": (
        "from kiro_crew import cli_commands_cron as cli_commands\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(cli_commands, 'X', 1)\n",
        set(),
    ),
    "owner-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.cli_commands_memory.Y'):\n        pass\n",
        set(),
    ),
    "name-chosen-at-run-time": (
        "from kiro_crew import cli_commands\n"
        "def t(monkeypatch, name):\n    monkeypatch.setattr(cli_commands, name, 1)\n",
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
        if _SPELLS_THE_FACADE.search(text):
            found |= patched_names(text)
    return frozenset(found)


# --------------------------------------------------------------------------- #
# Reach: a patch through the facade lands on every module holding the name
# --------------------------------------------------------------------------- #


def _reach_gaps(names: set[str], index: _facade_mirror.MirrorIndex) -> list[str]:
    """``owner.name`` for each patched name an owner binds that the index does not route."""
    return [
        f"{module.__name__}.{name}"
        for name in sorted(names)
        for module in _family()[1:]
        if name in vars(module) and module.__name__ not in index.holders.get(name, ())
    ]


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(cli_commands, name) or isinstance(getattr(cli_commands, name), ModuleType):
            continue  # a shared module (os, sys, beacon) is one object in every holder
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(cli_commands, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(cli_commands, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_mirror_indexes_exactly_the_owners_with_no_exclusion_and_no_forward() -> None:
    index = _index()
    assert index.owners == OWNERS == cli_commands._OWNERS
    assert index.excluded == ()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports) == cli_commands._EXPORTS
    assert index.divergent() == ()


# --------------------------------------------------------------------------- #
# Layering: owners import the facade never, and other owners only from below
# --------------------------------------------------------------------------- #


def _owner_imports(tree: ast.Module) -> set[str]:
    """The facade and owner modules *tree* imports from, anywhere in it."""
    family = {FACADE, *OWNERS}
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found |= {node.module} & family
            found |= {f"{node.module}.{alias.name}" for alias in node.names} & family
        elif isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names} & family
    return found


def layering_violations(trees: dict[str, ast.Module]) -> dict[str, list[str]]:
    """Owner -> the family modules it imports that are not owners listed before it."""
    out: dict[str, list[str]] = {}
    for position, owner in enumerate(OWNERS):
        allowed = set(OWNERS[:position])
        bad = sorted(_owner_imports(trees[owner]) - allowed)
        if bad:
            out[owner] = bad
    return out


def _owner_trees() -> dict[str, ast.Module]:
    return {owner: _tree(sys.modules[owner]) for owner in OWNERS}


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    trees = _owner_trees()
    assert layering_violations(trees) == {}
    assert any(_owner_imports(tree) for tree in trees.values()), "no cross-owner import found"


def test_the_layering_check_can_fail() -> None:
    trees = _owner_trees()
    trees[OWNERS[0]] = ast.parse(f"from {OWNERS[1]} import x\nimport {FACADE}\n")
    assert layering_violations(trees) == {OWNERS[0]: sorted({FACADE, OWNERS[1]})}


# --------------------------------------------------------------------------- #
# The frozen import inventory: what F401 would otherwise check
# --------------------------------------------------------------------------- #


def runtime_imports(source: str) -> set[str]:
    """Names *source* binds by a module-level import outside ``if TYPE_CHECKING:``."""
    found: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            found |= {alias.asname or alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            found |= {alias.asname or alias.name for alias in node.names}
    return found


def unread_local_imports(source: str) -> list[str]:
    """``function:name`` for each import inside a function that nothing in that function
    (its nested scopes included) reads, and ``<where>:name`` for an import that is neither
    at module level, under ``if TYPE_CHECKING:``, nor inside a function."""
    tree = ast.parse(source)
    typing_only = {
        id(sub)
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for sub in ast.walk(node)
    }
    functions = [
        fn for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    owner_of: dict[int, ast.AST] = {}
    for fn in functions:
        for node in ast.walk(fn):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                owner_of[id(node)] = fn  # the innermost function wins: it is walked last
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)) or id(node) in typing_only:
            continue
        if node in tree.body:
            continue  # module level: runtime_imports() holds those to the inventory
        fn = owner_of.get(id(node))
        names = [(alias.asname or alias.name).split(".")[0] for alias in node.names]
        if fn is None:
            found += [f"<line {node.lineno}>:{name}" for name in names]
            continue
        read = {
            n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        found += [f"{fn.name}:{name}" for name in names if name not in read]
    return sorted(found)


def test_the_facade_imports_exactly_its_frozen_inventory() -> None:
    assert runtime_imports(_source(cli_commands)) == _BASE_IMPORTS | _GLUE_IMPORTS


def test_every_function_local_import_in_the_facade_is_read() -> None:
    source = _source(cli_commands)
    tree = ast.parse(source)
    local = [
        node
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(fn)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert local, "the facade's handlers import nothing locally -- the scan went blind"
    assert unread_local_imports(source) == []


def test_the_local_import_check_reports_a_planted_unread_import() -> None:
    planted = (
        "def f():\n    import json\n    from os import sep\n    return sep\n"
        "def g():\n    import json\n    def h():\n        return json\n    return h\n"
        "try:\n    import uuid\nexcept ImportError:\n    pass\n"
    )
    assert unread_local_imports(planted) == ["<line 11>:uuid", "f:json"]
    source = _source(cli_commands) + "\n\ndef _planted():\n    import json\n    return 1\n"
    assert unread_local_imports(source) == ["_planted:json"]


def test_the_inventory_check_reports_a_planted_and_a_dropped_import() -> None:
    """The F401 stand-in must be able to fail both ways: an import added to the facade
    and an import it stops binding are both a difference from the inventory."""
    source = _source(cli_commands)
    planted = source + "\nfrom kiro_crew.cli_commands_cron import _cron_add\n"
    assert runtime_imports(planted) - (_BASE_IMPORTS | _GLUE_IMPORTS) == {"_cron_add"}
    dropped = source.replace("import uuid\n", "", 1)
    assert (_BASE_IMPORTS | _GLUE_IMPORTS) - runtime_imports(dropped) == {"uuid"}


def test_a_star_import_binds_what_the_unsplit_module_bound() -> None:
    """The unsplit module had no ``__all__``, so a star import bound every public name of
    its namespace: its public imports, ``annotations`` from its ``__future__`` import, and
    its two public functions. The derived ``__all__`` binds exactly those."""
    public_imports = {name for name in _BASE_IMPORTS if not name.startswith("_")}
    defined = {"annotations", "parse_time_selector", "write_eval_artifacts"}
    assert set(cli_commands.__all__) == public_imports | defined


# --------------------------------------------------------------------------- #
# Static visibility
# --------------------------------------------------------------------------- #


def _statically_bound(tree: ast.Module) -> set[str]:
    """Names a type checker sees on the module: every module-level binding, the
    ``TYPE_CHECKING`` imports included."""
    names = set(_facade_mirror._defined_names(tree))
    for stmt in _facade_mirror._module_level_statements(tree):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names |= {alias.asname or alias.name.split(".")[0] for alias in stmt.names}
    return names


def test_every_base_public_name_type_checks_through_the_facade() -> None:
    missing = base_names._BASE_PUBLIC_NAMES[FACADE] - _statically_bound(_tree(cli_commands))
    assert sorted(missing) == []


def test_every_served_name_is_defined_by_the_owner_serving_it() -> None:
    """The facade binds every import it had, so what the mirror serves unbound is only
    the owners' own definitions, each from the owner that defines it."""
    trees = _owner_trees()
    defined = {owner: _facade_mirror._defined_names(tree) for owner, tree in trees.items()}
    exports = _index().exports
    assert exports, "the facade serves nothing -- the scan went blind"
    assert {name: owner for name, owner in exports.items() if name not in defined[owner]} == {}


# --------------------------------------------------------------------------- #
# Names the moved code reports itself under
# --------------------------------------------------------------------------- #


def logger_names(tree: ast.Module) -> list[str]:
    """The argument of every ``logging.getLogger(...)`` call in *tree*, unparsed."""
    return [
        ast.unparse(node.args[0]) if node.args else ""
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "logging.getLogger"
    ]


def test_the_moved_handlers_log_under_the_facade_name() -> None:
    found = [name for tree in _owner_trees().values() for name in logger_names(tree)]
    assert found, "no logger call found in the owners -- the scan went blind"
    assert set(found) == {repr(FACADE)}


def test_the_logger_scan_reads_a_module_relative_name() -> None:
    tree = ast.parse("import logging\nlogging.getLogger(__name__).debug('x')\n")
    assert logger_names(tree) == ["__name__"]


def test_the_lazy_import_ban_names_every_owner() -> None:
    """``import kiro_crew.cli`` must load no owner either: each one loads with the facade,
    so ``test/test_cli_lazy_imports.py`` bans every owner beside it."""
    import test_cli_lazy_imports as lazy

    assert sorted(set(OWNERS) - set(lazy._BANNED_AFTER_CLI_IMPORT)) == []
    assert FACADE in lazy._BANNED_AFTER_CLI_IMPORT


def test_the_moved_exception_class_keeps_the_facade_module_name() -> None:
    """``module.qualname`` is how a traceback names a raised class."""
    assert cli_commands._CliConflict.__module__ == FACADE
    assert "_CliConflict" in vars(sys.modules["kiro_crew.cli_commands_workspace"])
