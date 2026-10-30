"""The cron MCP server's facade keeps the patch seams the one module had.

``kiro_crew.mcp_cron`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
``kiro_crew.mcp_cron_<role>`` owners, and keeps the server itself. The gateway, the CLI,
the tests and the documents still reach every name as ``kiro_crew.mcp_cron.<name>``, and
the moved code reads its names as plain module globals, so a patch through the facade
reaches that code only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules.
These tests pin what is particular to this facade: every name the test trees patch
through it reaches every module holding it; the owners are exactly the top-level
``mcp_cron_*.py`` siblings, loaded lowest layer first; the server itself stays bound on
the facade; every name it serves from outside the family stays visible to type checkers;
the family logs under the facade's name; and the facade still runs as ``python -m``.
"""

from __future__ import annotations

import ast
import functools
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror, mcp_cron
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_mcp_cron_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.mcp_cron"
OWNER_PREFIX = "kiro_crew.mcp_cron_"
SRC_DIR = Path(mcp_cron.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the cron
#: server has: the store class and the config dir the tool bodies read, the SEL factory,
#: the governance and script vets, the caller-identity helpers the facade keeps, the
#: dashboard transport an owner calls, and names the family only imports. The scan below
#: must find all of them, so a scanner that stops matching goes red instead of passing
#: over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "CronService",
        "config_dir",
        "sel",
        "_vet_cron_capability_governance",
        "_vet_command_governance",
        "_audit_fire_time_decision",
        "_vet_script_file",
        "_vet_script_contents",
        "_authz_session_key",
        "_caller_channel_id",
        "_call_tool_locally",
        "_fetch_history_stats",
        "_post",
        "fd_real_path",
        "sensitive_path_refusal",
        "is_sensitive_bash_command",
        "resolve_script_path",
        "get_local_tz",
    }
)

#: The server itself: argument validation, the call path, who is calling and what they
#: may touch, the tool bodies and the stdio entry point. These stay in ``mcp_cron.py``.
_KEPT = frozenset(
    {
        "_sub_floor_timeout_note",
        "_validate_args",
        "_call_tool",
        "_call_tool_locally",
        "_caller_channel_id",
        "_caller_thread_id",
        "_authz_session_key",
        "_deny_channel_agent_cron",
        "_unidentified_caller_refusal",
        "_check_cron_job_ownership",
        "_call_tool_inner",
        "ADVERTISE_CALLER_IDENTITY",
        "run_mcp_server",
    }
)

#: Served names the facade's typing manifest leaves out: ``_call_tool`` binds ``_post``
#: with a function-local import, which a module-level binding would shadow.
_LOCALLY_IMPORTED = frozenset({"_post"})


def _index() -> _facade_mirror.MirrorIndex:
    return mcp_cron.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('mcp_cron_'):]}": path
        for path in sorted(SRC_DIR.glob("mcp_cron_*.py"))
    }


def _family() -> list[ModuleType]:
    return [mcp_cron, *(sys.modules[name] for name in _index().owners)]


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
        if "mcp_cron" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``mcp_cron.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(mcp_cron, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(mcp_cron, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(mcp_cron, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_mcp_cron_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``mcp_cron_*.py`` sibling, and each owner imports only
    owners listed before it -- the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    assert sorted(owners) == sorted(_owner_paths())
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(_owner_paths()[owner].read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    assert late == {}


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``mcp_cron.py`` was a plain module, so no reader fetched a name of it through
    ``importlib.import_module``: the facade forwards no legacy table and keeps the default
    ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_facade_keeps_the_server_itself() -> None:
    """The call path, the caller checks, the tool bodies and the entry point stay bound
    on ``mcp_cron.py``; none of them is served from an owner."""
    assert sorted(_KEPT - set(vars(mcp_cron))) == []
    assert sorted(_KEPT & set(_index().exports)) == []


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family."""
    family = set(_index().owners)
    found: dict[str, tuple[str, str]] = {}
    for path in _owner_paths().values():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if not node.module.startswith("kiro_crew") or node.module in family:
                    continue
                for alias in node.names:
                    if f"{node.module}.{alias.name}" not in family:
                        found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family: name -> (home, original)."""
    family = set(_index().owners)
    tree = ast.parse(Path(mcp_cron.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """At BASE the module bound these by import, so ``from kiro_crew.mcp_cron import
    is_sensitive_path`` type-checked. Now an owner holds them, so the facade names each
    one from its home module under ``TYPE_CHECKING``, and exactly those -- except a name
    its own code imports inside a function, which a module-level binding would shadow."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert sorted(_LOCALLY_IMPORTED - set(expected)) == []
    for name in _LOCALLY_IMPORTED:
        owner = sys.modules[_index().exports[name]]
        assert getattr(mcp_cron, name) is vars(owner)[name]
        assert expected.pop(name) == ("kiro_crew.mcp_core", name)
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert mcp_cron.logger.name == FACADE
    others = [
        m.__name__
        for m in _family()
        if vars(m).get("logger", mcp_cron.logger) is not mcp_cron.logger
    ]
    assert others == []


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.mcp_cron`` imported cleanly before the split; the mirror
    installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(Path(mcp_cron.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    out = subprocess.run(
        [sys.executable, "-m", FACADE],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=env,
        timeout=90,
        **UTF8_TEXT,
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_mcp_cron.py" in walked
