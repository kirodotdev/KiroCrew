"""The Settings > Security handlers' facade keeps the patch seams the one module had.

``kiro_crew.dashboard.handlers.security`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.dashboard.handlers.security_<role>`` owners. Callers, tests and documents
still reach every name as ``kiro_crew.dashboard.handlers.security.<name>``, and the moved code
reads its names as plain module globals, so a patch through the facade reaches that code only
because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules (one
object per name, the manifest, owners never importing their facade, no shared global
rebound). These tests pin what is particular to this facade: every name the test trees patch
through it reaches every module holding it; the owners are exactly the ``security_*.py``
siblings, loaded lowest layer first; the denied-commands code the facade keeps stays bound
there; the family logs under the facade's name; the moved exceptions keep the facade's
module; and the facade still runs as ``python -m``.
"""

from __future__ import annotations

import ast
import functools
import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import security
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_security_handlers_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.dashboard.handlers.security"
OWNER_PREFIX = "kiro_crew.dashboard.handlers.security_"
HANDLERS_DIR = Path(security.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the
#: handlers have: the SEL every endpoint audits through, the trusted-app config write and
#: the app lifecycle calls its routes make, the registry cache and pin reads, the governance
#: scope lookup, and the denied-commands snapshot the facade keeps. The scan below must find
#: all of them, so a scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_sel",
        "_mutate_agent_config",
        "teardown_app_runtime",
        "get_app",
        "disable_app",
        "get_registry_app",
        "_expire_registry_index_cache",
        "_matching_operator_registry",
        "_pinned_registries",
        "resolve_active_scope",
        "build_denied_commands_snapshot_async",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return security.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('security_'):]}": path
        for path in sorted(HANDLERS_DIR.glob("security_*.py"))
    }


def _family() -> list[ModuleType]:
    return [security, *(sys.modules[name] for name in _index().owners)]


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
        if "handlers" not in text or "security" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``security.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(security, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(security, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(security, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_security_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``security_*.py`` sibling, and each owner imports only
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
    """No reader fetches a name of ``security.py`` through ``importlib.import_module``, so the
    facade forwards no legacy table and keeps the default ``forward_reads``, and every
    served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_facade_binds_the_code_it_keeps_for_itself() -> None:
    """The denied-commands surface stays in ``security.py``, bound there and not served
    from an owner: ``code-review.yml``'s ``bool()`` opt-out tripwire reads the
    ``denied_commands.json`` parsing in this file by path, and the denied-commands tests
    import these names from it."""
    kept = {
        "_MAX_PATTERN_LEN",
        "_MAX_NOTE_LEN",
        "_read_denied_data",
        "_read_denied_strict",
        "_denied_state",
        "_user_rule_ids",
        "_user_rule_ids_async",
        "_pinned_ids_for_snapshot_async",
        "build_denied_commands_snapshot",
        "count_effective_denied_commands",
        "build_denied_commands_snapshot_async",
        "_snapshot_response",
        "_write_denied_state",
        "_reload_live_hooks",
        "_apply_mutation",
        "api_denied_commands_list",
        "api_denied_command_builtin_toggle",
        "api_denied_commands_disable_all",
        "api_denied_command_user_add",
        "api_denied_command_user_toggle",
        "api_denied_command_user_delete",
    }
    index = _index()
    assert sorted(kept - set(vars(security))) == []
    assert sorted(kept & set(index.exports)) == []
    for name in kept:
        obj = getattr(security, name)
        code = getattr(obj, "__code__", None)
        if code is not None:
            assert Path(code.co_filename).resolve() == Path(security.__file__).resolve(), name


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
    tree = ast.parse(Path(security.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """An owner binds these by import, so the facade names each one from its home module
    under ``TYPE_CHECKING`` and ``from kiro_crew.dashboard.handlers.security import
    get_app`` type-checks."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert security.logger.name == FACADE
    others = [
        m.__name__
        for m in _family()
        if vars(m).get("logger", security.logger) is not security.logger
    ]
    assert others == []


def test_the_moved_exceptions_keep_the_facade_module() -> None:
    """``ConfigCorruptError`` and ``TrustSettingOverlayOwned`` are named in tracebacks and
    logs by the path callers import them from, and that path still resolves them."""
    for cls in (security.ConfigCorruptError, security.TrustSettingOverlayOwned):
        assert cls.__module__ == FACADE
        assert getattr(importlib.import_module(cls.__module__), cls.__qualname__) is cls


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.dashboard.handlers.security`` imports cleanly: the mirror
    installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(Path(security.__file__).resolve().parents[3])
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
    assert source_corpus.repo_root() / "test" / "test_trusted_apps_api.py" in walked
