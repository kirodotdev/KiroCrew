"""``kiro_crew.agent`` composes its split-out spec code onto one namespace.

The spec-file writes, the fresh-spec build, the launcher, the spec upgrades and lookups,
the shared-home guard, the rebuild with its ceiling projection, and most prompt and grant
text moved out of ``agent.py`` into the ``kiro_crew.agent_parts`` owners.
:func:`kiro_crew._facade_compose.compose` runs every function they define on
``kiro_crew.agent``'s module globals. The facade's own contract
(``test/test_agent_refactor_facade.py``) passes unchanged because of that; these tests pin
what the composition itself promises:

* every owner function runs on the facade's globals, names the facade as its module, and is
  the object the facade binds, except the two functions wrapped where they are defined,
  which the facade wraps again around a rebound copy; every global an owner function reads,
  nested code included, is bound there;
* a ``global`` rebind in moved code lands on the facade, and the re-wrapped rebuild and spec
  lock read the facade's names;
* every name the test trees patch on the facade reaches every owner function that reads it,
  and one patch per function owner is exercised for real;
* an owner imports standard-library modules plainly and the facade only for type checking,
  binds every module-level value it defines to the same object the facade binds, carries no
  agent-SDK boundary edge, binds no logger of its own, and nothing but the facade imports
  one;
* the facade composes through the shared helper, once, and imports nothing its family does
  not account for, which stands in for the F401 check ``setup.cfg`` switches off for it; and
* a fresh ``import kiro_crew.agent`` composes every owner.
"""

from __future__ import annotations

import ast
import builtins
import dis
import functools
import importlib
import importlib.util
import inspect
import json
import pkgutil
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Callable
from unittest import mock

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror, agent, agent_parts
from kiro_crew._facade_compose import compose as facade_compose
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_agent_parts")

FACADE = agent.__name__
PACKAGE = agent_parts.__name__
_PARTS_DIR = Path(agent_parts.__file__).resolve().parent
_SRC = Path(agent.__file__).resolve().parent
_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "LOAD_NAME", "STORE_GLOBAL", "DELETE_GLOBAL"})

#: The owners, by module stem.
_OWNER_STEMS = (
    "kirocrew_launcher",
    "shared_home",
    "spec_build",
    "spec_files",
    "spec_grants",
    "spec_lookup",
    "spec_prompts",
    "spec_rebuild",
    "spec_upgrades",
)

#: Owners that define functions (the other two hold prompt and grant text only).
_FUNCTION_OWNERS = frozenset(_OWNER_STEMS) - {"spec_grants", "spec_prompts"}

#: Names an owner binds to a function the facade wraps again around a rebound copy: a
#: rebind reaches a wrapper, never the function its closure holds.
_REWRAPPED = frozenset({"agents_spec_lock", "install_agent", "rebuild_agent_config"})

#: Names the test trees are known to patch on the facade, at least one per function owner
#: and per kind of seam: an owner function, a name the facade only imports, a rebound
#: global, the facade's ``__file__`` and the override the conftest floor sets. The scan below
#: must find all of them, so a scanner that stops matching goes red instead of passing over
#: nothing.
_KNOWN_SEAMS = frozenset(
    {
        "KIRO_AGENTS_DIR",
        "__file__",
        "_KIROCREW_BIN",
        "_atomic_json_write",
        "_conductor_spec_held",
        "_decline_shared_agent_home",
        "_kirocrew_mcp_invocation",
        "_read_spec_capped",
        "agent_spec_path",
        "ambient_agents_dir",
        "kiro_agents_dir_path",
        "rebuild_agent_config",
        "sel",
    }
)

#: Every name the facade's runtime import statements bound before its code moved out. They
#: stay bound because the composed functions read them as this module's globals, and callers
#: and tests read and patch them as its attributes.
_BASE_IMPORTS = frozenset("""
    _DASHBOARD_AUTHOR_FILENAME _declared_project_agent_name _HEARTBEAT_AGENT_FILENAME
    _in_ephemeral_tree _in_linked_git_worktree _mc_config_path _read_agent_spec
    _under_system_tmp _valid_override_home AGENT_FILENAME agent_spec_candidates agent_state
    agentcore_posture ambient_agents_dir AmbiguousAgentSpecError Any auto_approve
    conductor_agents config_dir contextlib contextvars current_context
    DASHBOARD_MANAGER_AGENT_NAME datetime default_spec_commit FileTooLargeError
    first_crewmate fork_refresh functools governance_permits guide_platform importlib
    is_markdown_spec is_registered_agent_name is_sensitive_path isolated_agents_dir
    iter_agent_spec_files Iterator json kiro_agents_dir kiro_hooks Literal loads_user_json
    logging managed_mcp mcp_aliases mcp_search_path mcp_sources mkdirs_owner_only ModuleType
    MutableMapping os OWNED_KIRO_AGENT_FILES Path platform_compat project_agent_files
    project_agent_name project_agent_names read_json_or redact redact_log replace_with_retry
    REQUIRED_KIRO_AGENT_FILES resolved_command_casing safe_context_call
    safe_read_file_bytes_nolink SecurityEvent sel service_agents shared_kiro_agents_writable
    shutil skill_resources SKILL_URI_PREFIX spec_path_key stat sys tempfile threading time
    timezone TYPE_CHECKING uuid worker_agent write_text_owner_only
    """.split())

#: The names the facade's composition glue imports.
_GLUE_IMPORTS = frozenset({"_facade_compose", "_facade_rebind"})


class _Reached(BaseException):
    """Raised by a stub to prove the composed code read the patched name.

    ``BaseException``, so an ``except Exception`` in the code under test cannot swallow a
    stub that was reached and make a missed patch read as one that landed.
    """


def _raiser(label: str) -> Callable[..., object]:
    def _stub(*_args: object, **_kwargs: object) -> object:
        raise _Reached(label)

    return _stub


def _owners() -> list[types.ModuleType]:
    """Every owner module, imported through the package the facade composes."""
    return [
        importlib.import_module(f"{PACKAGE}.{info.name}")
        for info in pkgutil.iter_modules([str(_PARTS_DIR)])
    ]


def _owner_paths() -> dict[str, Path]:
    return {path.stem: path for path in sorted(_PARTS_DIR.glob("*.py")) if path.stem != "__init__"}


def _owner_functions() -> list[tuple[str, str, types.FunctionType]]:
    """``(owner stem, name, function)`` for every function an owner's file defines."""
    found = []
    for owner in _owners():
        stem = owner.__name__.rpartition(".")[2]
        for name, value in vars(owner).items():
            if (
                isinstance(value, types.FunctionType)
                and value.__code__.co_filename == owner.__file__
            ):
                found.append((stem, name, value))
    return found


def _global_names(code: types.CodeType):
    """Every global a code object and the code objects nested in it read or write."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in _GLOBAL_OPS:
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _unbound_globals(fn: types.FunctionType) -> list[str]:
    namespace = fn.__globals__
    return sorted(
        {n for n in _global_names(fn.__code__) if n not in namespace and not hasattr(builtins, n)}
    )


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


# ── 1. every owner function runs on the facade's namespace ──────────────────


def test_the_facade_composes_every_module_of_the_package() -> None:
    stems = set(_owner_paths())
    assert set(_OWNER_STEMS) <= stems
    assert sorted(agent._PARTS) == sorted(f"{PACKAGE}.{stem}" for stem in stems)
    assert sorted(o.__name__ for o in _owners()) == sorted(agent._PARTS)


def test_every_owner_function_runs_on_the_facade_globals_as_the_facade_binds_it() -> None:
    functions = _owner_functions()
    assert {stem for stem, _name, _fn in functions} == _FUNCTION_OWNERS
    wrong = [
        f"{stem}.{name}"
        for stem, name, fn in functions
        if fn.__globals__ is not vars(agent)
        or fn.__module__ != FACADE
        or (name not in _REWRAPPED and vars(agent).get(name) is not fn)
    ]
    assert wrong == []


def test_each_rewrapped_function_wraps_a_rebound_copy_of_the_owner_code() -> None:
    """The facade's binding of each wrapped function unwraps to the owner's own code object,
    running on the facade's globals, so nothing a caller reaches runs on an owner's."""
    for name in _REWRAPPED:
        bound = vars(agent)[name]
        homes = [o for o in _owners() if name in vars(o)]
        assert len(homes) == 1, name
        inner = inspect.unwrap(bound)
        assert inner is not bound, name
        assert inner.__globals__ is vars(agent), name
        assert inner.__module__ == FACADE, name
        assert inner.__code__ is inspect.unwrap(vars(homes[0])[name]).__code__, name
    assert agent.install_agent is agent.rebuild_agent_config
    assert agent.rebuild_agent_config.__globals__ is vars(agent)  # the outcome tracker


def test_the_managed_server_table_holds_the_composed_spec_gate() -> None:
    """The facade builds ``_MANAGED_MCP_SERVERS`` after composing, so the gate the table
    captures is the composed function, which reads the facade's names; composed after the
    table, it would hold the owner's original."""
    gate = agent._MANAGED_MCP_SERVERS["kirocrew-computer"]["spec_gate"]
    assert gate is agent._computer_use_spec_gate
    assert gate.__globals__ is vars(agent)


def test_the_sweep_reports_a_global_the_namespace_does_not_bind() -> None:
    module_code = compile(
        "def f():\n    def g():\n        return absent\n    return present, g\n", "probe", "exec"
    )
    code = next(c for c in module_code.co_consts if isinstance(c, types.CodeType))
    probe = types.FunctionType(code, {"__name__": "probe", "present": 1})
    assert _unbound_globals(probe) == ["absent"]


def test_every_global_an_owner_function_reads_is_bound_on_the_facade() -> None:
    """Nested code included: ``_resolve_kirocrew_bin`` defines its ``_usable`` inline."""
    missing = {
        f"{stem}.{name}": names
        for stem, name, fn in _owner_functions()
        if (names := _unbound_globals(fn))
    }
    assert missing == {}


def test_a_global_rebind_in_moved_code_lands_on_the_facade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launcher resolver caches what it finds with ``global _KIROCREW_BIN``; the cache
    lands on the facade, which every reader of the name reads."""
    planted = tmp_path / "planted-kirocrew"
    monkeypatch.setattr(agent, "_KIROCREW_BIN", None)
    monkeypatch.setattr(agent, "_launcher_works", lambda path: path.name == planted.name)
    monkeypatch.setattr(shutil, "which", lambda name: str(planted))
    assert agent._resolve_kirocrew_bin() == str(planted)
    assert agent._KIROCREW_BIN == str(planted)


def test_the_rebuild_tracker_rebinds_its_globals_on_the_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outcome tracker around the rebuild rebinds two globals when the rebuild raises;
    both land on the facade, which the projection code and ``fork_refresh`` read, and the
    re-wrapped rebuild body reads the facade's names."""
    monkeypatch.setattr(agent, "_rebuild_incomplete", False)
    monkeypatch.setattr(agent, "_projected_ceiling_generation", 7)
    monkeypatch.setattr(agent, "_decline_shared_agent_home", _raiser("decline"))
    with pytest.raises(_Reached) as reached:
        agent.rebuild_agent_config()
    assert reached.value.args == ("decline",)
    assert agent._rebuild_incomplete is True
    assert agent._projected_ceiling_generation is None


def test_the_rewrapped_spec_lock_reads_the_facade_logger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = mock.Mock()
    stub.warning.side_effect = _Reached("logger.warning")
    monkeypatch.setattr(agent, "logger", stub)
    with pytest.raises(_Reached):
        with agent.agents_spec_lock(tmp_path / "absent" / "agents"):
            pass


# ── 2. a patch of the facade reaches every owner reader ─────────────────────


def _spellings() -> dict[str, str]:
    """The facade's dotted name, plus ``<module>.<alias>`` for each src module that binds it
    at module level -- another spelling of the same object a test can patch through."""
    spellings = {FACADE: FACADE}
    for path, text in source_corpus.iter_candidate_sources(require_all=("agent",)):
        if not path.is_relative_to(_SRC):
            continue
        module = ".".join(path.relative_to(_SRC.parent).with_suffix("").parts)
        for node in ast.parse(text).body:
            if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew" and not node.level:
                for alias in node.names:
                    if alias.name == "agent":
                        spellings[f"{module}.{alias.asname or alias.name}"] = FACADE
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == FACADE and alias.asname:
                        spellings[f"{module}.{alias.asname}"] = FACADE
    return spellings


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
    """Every name the test trees patch on the facade, scanned once per worker."""
    spellings = _spellings()
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if not facade_mirror_scan._mentions(text, spellings):
            continue
        found |= facade_mirror_scan.text_patched_names(text, spellings).get(FACADE, set())
        if facade_mirror_scan.needs_syntax_tree(text, spellings):
            try:
                found |= facade_mirror_scan.patched_names(text, spellings).get(FACADE, set())
            except SyntaxError:
                continue
    return frozenset(found)


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    this_file = Path(__file__).resolve().relative_to(root).as_posix()
    assert this_file in leaf_test_scope.corpus_gates(root)


def test_the_spellings_include_a_materialization_owner_binding_of_the_facade() -> None:
    assert "kiro_crew.agent_materialization.worker_agent.agent_mod" in _spellings()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from kiro_crew import agent\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(agent, 'A', 1)\n",
            {"A"},
        ),
        (
            "from unittest.mock import patch\n"
            "def t():\n    with patch('kiro_crew.agent.B'):\n        pass\n",
            {"B"},
        ),
        (
            "from kiro_crew.agent_materialization import worker_agent\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(worker_agent.agent_mod, 'C', 1)\n",
            {"C"},
        ),
        (
            "from unittest.mock import patch\n"
            "def t():\n    with patch('kiro_crew.agent_parts.spec_files.D'):\n        pass\n",
            set(),
        ),
    ],
    ids=["monkeypatch", "mock-patch-string", "owner-binding", "owner-string"],
)
def test_the_patch_scan_reads_every_spelling_and_nothing_else(
    source: str, expected: set[str]
) -> None:
    assert facade_mirror_scan.patched_names(source, _spellings()).get(FACADE, set()) == expected


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that found nothing would pass the reach test over nothing."""
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


def test_every_name_the_tests_patch_on_the_facade_reaches_every_owner_reader() -> None:
    """A patch of the facade reaches owner code only where that code runs on its globals."""
    patched = _corpus_patched_names()
    reading_owners: set[str] = set()
    missed = []
    for stem, name, fn in _owner_functions():
        loaded = set(_global_names(fn.__code__)) & patched
        if loaded:
            reading_owners.add(stem)
        if loaded and fn.__globals__ is not vars(agent):
            missed.append(f"{stem}.{name}: {sorted(loaded)}")
    assert (
        reading_owners == _FUNCTION_OWNERS
    ), "an owner reads no patched name -- the sweep went blind"
    assert missed == []


def test_a_facade_patch_reaches_the_spec_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "_decline_shared_agent_home", _raiser("decline"))
    with pytest.raises(_Reached):
        agent.missing_required_agent_specs()


def test_a_facade_patch_reaches_the_spec_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(agent, "_user_dir", lambda: tmp_path)
    assert agent._user_prompt_path() == tmp_path / "prompt.md"


def test_a_facade_patch_reaches_the_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "_resolve_kirocrew_bin", _raiser("resolve"))
    with pytest.raises(_Reached):
        agent._kirocrew_mcp_invocation("mcp-cron")


def test_a_facade_patch_reaches_the_spec_upgrades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "kiro_agents_dir_path", _raiser("agents dir"))
    with pytest.raises(_Reached):
        agent.migrate_agent_specs()


def test_a_facade_patch_reaches_the_spec_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(agent, "_read_agent_spec", lambda path, **_kw: {"planted": str(path)})
    assert agent._read_spec_capped(tmp_path / "x.json") == {"planted": str(tmp_path / "x.json")}


def test_a_facade_patch_reaches_the_shared_home(monkeypatch: pytest.MonkeyPatch) -> None:
    warned: set[tuple[str, str]] = set()
    stub = mock.Mock()
    monkeypatch.setattr(agent, "_declined_home_warned", warned)
    monkeypatch.setattr(agent, "logger", stub)
    agent._warn_declined_home_once("arm", Path("target"), "declined %s", "home")
    assert warned == {("target", "arm")}
    stub.warning.assert_called_once_with("declined %s", "home")


def test_a_facade_patch_reaches_the_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "_decline_shared_agent_home", lambda **_kw: "declined")
    assert agent.rebuild_agent_config() == "declined"


# ── 3. what an owner may hold and import ────────────────────────────────────


def _runtime_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """``(line, module)`` of each import an owner executes when it is imported.

    Module level only: a function that imported a name inside its own body did so in the
    one-module file too, and that import still runs when the function does.
    """
    found = []
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            pending += node.orelse
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            pending += [n for f in ("body", "orelse", "finalbody") for n in getattr(node, f, [])]
            pending += [n for h in getattr(node, "handlers", []) for n in h.body]
        elif isinstance(node, ast.ImportFrom):
            found.append((node.lineno, "." * node.level + (node.module or "")))
        elif isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
    return sorted(found)


def _bad_owner_imports(tree: ast.Module) -> list[str]:
    """Runtime project imports (a relative one included), and type-only imports from
    anywhere but the facade."""
    bad = [
        f"{line}: {mod}"
        for line, mod in _runtime_imports(tree)
        if mod == "kiro_crew" or mod.startswith(("kiro_crew.", "."))
    ]
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.ImportFrom) and stmt.module != FACADE:
                    bad.append(f"{stmt.lineno}: type-only {stmt.module}")
                elif isinstance(stmt, ast.Import):
                    bad.append(f"{stmt.lineno}: type-only import")
    return bad


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("import os\nfrom pathlib import Path\n", False),
        ("if TYPE_CHECKING:\n    from kiro_crew.agent import sel\n", False),
        ("from kiro_crew.agent import sel\n", True),
        ("from kiro_crew import agent_state\n", True),
        ("import kiro_crew.agent\n", True),
        ("if TYPE_CHECKING:\n    from kiro_crew.sel import sel\n", True),
        ("from .. import agent\n", True),
        ("from . import spec_files\n", True),
    ],
)
def test_the_owner_import_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert bool(_bad_owner_imports(ast.parse(source))) is flagged


def test_an_owner_imports_the_facade_only_for_type_checking_and_no_project_module() -> None:
    bad = {
        stem: found
        for stem, path in _owner_paths().items()
        if (found := _bad_owner_imports(_tree(path)))
    }
    assert bad == {}


def _module_values(tree: ast.Module) -> list[str]:
    """Names a module binds at its top level other than by an import or a ``def``."""
    found = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            found += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.append(node.target.id)
        elif not isinstance(node, (ast.Expr, ast.Import, ast.ImportFrom, ast.FunctionDef, ast.If)):
            found.append(f"{node.lineno}: {type(node).__name__}")
    return found


def test_the_value_scan_can_fail() -> None:
    planted = ast.parse('"""doc"""\nimport os\nLIMIT = 3\nX: int = 4\nclass K: ...\n')
    assert _module_values(planted) == ["LIMIT", "X", "5: ClassDef"]


def test_every_module_level_value_an_owner_defines_is_the_object_the_facade_binds() -> None:
    """Checked in a fresh interpreter, before any test patch or rebuild has rebound one: the
    facade's binding is the owner's object, so a write through the facade is what every
    composed function reads. No owner defines a class: the two exception classes stay on the
    facade, where their ``__module__`` is the one error text and logs render."""
    values = {
        stem: [name for name in _module_values(_tree(path)) if name not in _REWRAPPED]
        for stem, path in _owner_paths().items()
    }
    assert [v for names in values.values() for v in names if ":" in v] == []
    probe = (
        "import importlib, json, sys, kiro_crew.agent as agent\n"
        "values = json.loads(sys.argv[1])\n"
        "print(json.dumps(sorted(\n"
        "    stem + '.' + name for stem, names in values.items() for name in names\n"
        "    if vars(agent).get(name, values) is not getattr(\n"
        f"        importlib.import_module('{PACKAGE}.' + stem), name)\n"
        ")))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe, json.dumps(values)],
        cwd=source_corpus.repo_root(),
        capture_output=True,
        check=False,
        timeout=120,
        **UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "[]"
    assert sum(len(names) for names in values.values()) >= 40


def test_the_package_init_defines_nothing() -> None:
    init = _PARTS_DIR / "__init__.py"
    assert _facade_mirror._defined_names(_tree(init)) == set()
    assert [n for n in _tree(init).body if not isinstance(n, ast.Expr)] == []


def _imports_an_owner(source: str) -> bool:
    """Whether *source* imports the package or one of its modules, in any spelling, absolute
    or relative; a sibling whose name merely contains ``agent_parts`` (``subagent_parts``)
    is not one."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            segments = (node.module or "").split(".")
            if "agent_parts" in segments or any(a.name == "agent_parts" for a in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any("agent_parts" in alias.name.split(".") for alias in node.names):
                return True
    return False


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from kiro_crew.agent_parts import spec_files\n", True),
        ("from kiro_crew.agent_parts.kirocrew_launcher import _resolve_kirocrew_bin\n", True),
        ("import kiro_crew.agent_parts.spec_files\n", True),
        ("from kiro_crew import agent_parts\n", True),
        ("from .agent_parts import spec_build\n", True),
        ("from kiro_crew.agent import rebuild_agent_config\n", False),
        ("from kiro_crew.subagent_parts import manager\n", False),
        ("from . import agent_parts\n", True),
        ("from ..agent_parts.spec_files import kiro_agents_dir_path\n", True),
        ("from .subagent_parts import manager\n", False),
    ],
)
def test_the_importer_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert _imports_an_owner(source) is flagged


def _owner_importers(src: Path) -> list[str]:
    return sorted(
        path.relative_to(src.parent).as_posix()
        for path in sorted(src.rglob("*.py"))
        if path.parent.name != "agent_parts"
        and path != src / "agent.py"
        and "agent_parts" in (text := path.read_text(encoding="utf-8"))
        and _imports_an_owner(text)
    )


def test_nothing_but_the_facade_imports_an_owner() -> None:
    assert _owner_importers(_SRC) == []


def test_the_importer_scan_finds_a_planted_importer(tmp_path: Path) -> None:
    src = tmp_path / "kiro_crew"
    (src / "agent_parts").mkdir(parents=True)
    (src / "agent.py").write_text(
        "from kiro_crew.agent_parts import spec_files\n", encoding="utf-8"
    )
    (src / "agent_parts" / "spec_files.py").write_text("import os\n", encoding="utf-8")
    (src / "caller.py").write_text(
        "from kiro_crew.agent_parts.spec_files import kiro_agents_dir_path\n", encoding="utf-8"
    )
    assert _owner_importers(src) == ["kiro_crew/caller.py"]


def _rel(path: Path) -> str:
    """*path* as the boundary gate's own scan names it: relative to the repository."""
    return path.resolve().relative_to(source_corpus.repo_root()).as_posix()


def _boundary_gate() -> types.ModuleType:
    root = source_corpus.repo_root()
    spec = importlib.util.spec_from_file_location(
        "_s35_agent_sdk_boundary", root / "scripts" / "check_agent_sdk_boundary.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_owner_and_not_the_facade_carries_an_agent_sdk_boundary_edge() -> None:
    gate = _boundary_gate()
    paths = [*sorted(_PARTS_DIR.glob("*.py")), Path(agent.__file__)]
    edges = {
        path.name: len(gate._violations_in_source(_rel(path), path.read_text(encoding="utf-8")))
        for path in paths
    }
    assert edges == dict.fromkeys(edges, 0)


@pytest.mark.parametrize(
    "source", ["from ..acp.client import KIRO_CLI_BIN\n", "from kiro_crew.acp import types\n"]
)
def test_the_boundary_check_sees_an_owner_import_of_the_acp_layer(source: str) -> None:
    """The gate resolves a relative import against the repository path it is given."""
    gate = _boundary_gate()
    assert gate._violations_in_source(_rel(_PARTS_DIR / "spec_files.py"), source) != {}


def test_the_owners_log_through_the_facade_logger() -> None:
    assert agent.logger.name == FACADE
    assert [o.__name__ for o in _owners() if "logger" in vars(o)] == []
    assert any("logger" in set(_global_names(fn.__code__)) for _s, _n, fn in _owner_functions())


# ── 4. the composition and the facade's imports ─────────────────────────────


def test_the_facade_composes_through_the_shared_helper_once() -> None:
    assert agent._facade_compose is facade_compose
    source = Path(agent.__file__).read_text(encoding="utf-8")
    assert source.count("\n_facade_compose(") == 1


def unexpected_imports(source: str, owner_names: dict[str, set[str]], read: set[str]) -> list[str]:
    """Names *source* binds by a runtime import that nothing accounts for: the F401 check
    ``setup.cfg`` switches off for this module, kept as a test.

    A name is accounted for when the module bound it before its code moved out (BASE), the
    composition glue imports it, an owner module defines it (for an import from that owner),
    or the family reads it: the facade's own code or a composed owner function. Imports under
    ``if TYPE_CHECKING:`` bind nothing at run time and are the facade contract's business.
    """
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            continue
        if isinstance(node, ast.Import):
            bound = [(alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            bound = [(alias.asname or alias.name, node.module) for alias in node.names]
        else:
            continue
        for name, module in bound:
            if module in owner_names:
                if name not in owner_names[module]:
                    found.append(f"{module}.{name}")
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS | read:
                found.append(name)
    return found


def _owner_names() -> dict[str, set[str]]:
    return {
        f"{PACKAGE}.{stem}": _facade_mirror._defined_names(_tree(path))
        for stem, path in _owner_paths().items()
    }


def _family_reads() -> set[str]:
    """Every global the facade's code and the composed owner functions read."""
    facade = Path(agent.__file__)
    read = set(_global_names(compile(facade.read_text(encoding="utf-8"), str(facade), "exec")))
    read |= {n for _s, _n, fn in _owner_functions() for n in _global_names(fn.__code__)}
    return read


def test_the_facade_imports_nothing_the_family_does_not_account_for() -> None:
    source = Path(agent.__file__).read_text(encoding="utf-8")
    assert unexpected_imports(source, _owner_names(), _family_reads()) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: an import nothing reads, and an owner import
    of a name that owner does not define, are both reported."""
    planted = (
        Path(agent.__file__).read_text(encoding="utf-8")
        + "\nimport pickle\nfrom kiro_crew.agent_parts.spec_lookup import _atomic_json_write\n"
    )
    assert unexpected_imports(planted, _owner_names(), _family_reads()) == [
        "pickle",
        "kiro_crew.agent_parts.spec_lookup._atomic_json_write",
    ]


def test_a_fresh_import_composes_every_owner() -> None:
    probe = (
        "import sys, kiro_crew.agent as agent\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('kiro_crew.agent_parts.'))\n"
        "composed = all(getattr(agent, n).__globals__ is vars(agent)\n"
        "    for m in loaded for n, v in vars(sys.modules[m]).items()\n"
        "    if type(v).__name__ == 'function'\n"
        "    and v.__code__.co_filename == sys.modules[m].__file__)\n"
        "print(repr((loaded, composed)))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=source_corpus.repo_root(),
        capture_output=True,
        check=False,
        timeout=120,
        **UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr
    loaded, composed = ast.literal_eval(done.stdout.strip().splitlines()[-1])
    assert loaded == sorted(f"{PACKAGE}.{stem}" for stem in _OWNER_STEMS)
    assert composed is True
