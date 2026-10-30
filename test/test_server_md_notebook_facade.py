"""Notes' split ``server`` keeps the patch seams and the per-process state it had as one module.

``kiro_crew.apps.builtins.md_notebook.server`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``server_*`` owners beside it. The gateway runs it, ``syncer`` imports it and the tests
reach every name as ``server.<name>``; the moved code reads its names as plain module globals, so
a patch through the facade reaches that code because the mirror writes it into every module
holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this one:

* its owners are exactly the ``server_*.py`` siblings, each importing only the owners before
  it, and every name it serves from outside the family stays visible to type checkers;
* the per-process state stays in the facade and no owner reads it. The gateway runs ``server``
  as ``__main__``; ``main()`` aliases that running copy under the canonical name before
  ``create_app()`` imports ``syncer``, so ``syncer`` reads the same object and the whole
  backend process shares one facade — one set of locks, caches and token memo;
* a patch through the facade reaches every module holding the name, and its undo restores
  each, for the seams the test trees patch -- most of them through a fixture-unpacked alias
  the shared corpus scan cannot follow;
* the gateway's ``__main__`` launch installs the mirror on the running copy, with the one
  logger named for the facade, and the dotted import resolves to that same copy;
* the derived ``__all__`` keeps the star-import surface, and ``ApiError`` keeps the facade's
  module name.
"""

from __future__ import annotations

import ast
import functools
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew.apps.builtins.md_notebook import server, server_http, server_store
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_server_md_notebook_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("corpus_gate",)

PKG = "kiro_crew.apps.builtins.md_notebook"
FACADE = f"{PKG}.server"
_THIS_FILE = Path(__file__).resolve()
_MISSING = object()

#: Every per-process state object the facade holds: locks, caches, watches, marks, the memo.
STATE = (
    "_caches",
    "_gh_cache",
    "_save_locks",
    "_self_writes",
    "_settings_lock",
    "_vault_write_locks",
    "_vaults_lock",
    "_watches",
)

#: Names the test trees patch through the facade. Most are patched through a ``server_mod``
#: unpacked from a fixture, which the shared corpus scan cannot resolve, so each is exercised
#: here: owner-defined names, names the family imports, a module, the facade's own code.
SEAMS = (
    "FETCH_SETTLE_MS",
    "_HOME",
    "_publish_staged_sync",
    "_reveal_binary",
    "_reveal_folder_sync",
    "_save_note_contents",
    "_write_settings_sync",
    "asyncio",
    "gh_token",
    "json_body",
    "read_note_text",
    "read_settings",
    "read_vaults",
    "rebuild_cache",
    "replace_with_retry",
    "resolve_auth",
    "restrict_to_owner",
    "sel",
    "vault_path",
    "vault_write_lock",
    *STATE,
)

#: Seams the corpus scan must find, so a scan that stops matching goes red instead of letting
#: the routing check below pass over nothing.
KNOWN_SCANNED_SEAMS = frozenset({"_HOME", "resolve_auth", "restrict_to_owner", "sel", "vault_path"})


def _index() -> _facade_mirror.MirrorIndex:
    return vars(server)[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    directory = Path(server.__file__ or "").resolve().parent
    return {f"{PKG}.{path.stem}": path for path in sorted(directory.glob("server_*.py"))}


def _global_reads(path: Path) -> set[str]:
    return facade_mirror_scan.global_reads(path.read_text(encoding="utf-8"), str(path))


def test_the_owners_are_every_sibling_module_in_layer_order() -> None:
    """``_OWNERS`` lists every ``server_*.py`` sibling, and each owner imports only owners
    listed before it -- the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    paths = _owner_paths()
    assert sorted(owners) == sorted(paths)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(paths[owner].read_text(encoding="utf-8"))
        imported = {
            f"{PKG}.{node.module}" if node.level else node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    assert late == {}


def _home_imports() -> dict[str, tuple[str, str]]:
    """Names an owner imports from a project module outside the family, with their home."""
    family = set(_index().owners)
    found: dict[str, tuple[str, str]] = {}
    for path in _owner_paths().values():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or node.module == "__future__":
                continue
            home = node.module or ""
            if node.level:
                home = PKG + (f".{node.module}" if node.module else "")
            if not home.startswith("kiro_crew") or home in family:
                continue
            for alias in node.names:
                if f"{home}.{alias.name}" not in family:
                    found.setdefault(alias.asname or alias.name, (home, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family: name -> (home, name)."""
    family = set(_index().owners)
    tree = ast.parse(Path(server.__file__ or "").read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """Before the split the module bound these by import, so ``server.<name>`` type-checked.
    Now only an owner holds them, so the facade names each one from its home module under
    ``TYPE_CHECKING``, and exactly those."""
    served = set(_index().exports)
    defined: set[str] = set()
    for path in _owner_paths().values():
        defined |= _facade_mirror._defined_names(ast.parse(path.read_text(encoding="utf-8")))
    expected = {
        name: home
        for name, home in _home_imports().items()
        if name in served and name not in defined
    }
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_per_process_state_stays_in_the_facade_and_no_owner_reads_it() -> None:
    """Each copy of the facade a process imports keeps its own state only while no owner holds
    or reads it: an owner is imported once, so state there would be one object for both."""
    index = _index()
    owner_reads = {owner: _global_reads(path) for owner, path in _owner_paths().items()}
    facade_reads = _global_reads(Path(server.__file__ or ""))
    misplaced = {}
    for name in STATE:
        holders = [owner for owner in owner_reads if name in vars(importlib.import_module(owner))]
        readers = [owner for owner, reads in owner_reads.items() if name in reads]
        if name not in index.facade_only or holders or readers or name not in facade_reads:
            misplaced[name] = (name in index.facade_only, holders, readers)
    assert misplaced == {}


def test_home_is_served_from_the_store_where_every_reader_lives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_HOME`` is the data-home test override; nothing outside the tests writes it."""
    assert "_HOME" not in vars(server)
    assert _index().holders["_HOME"] == (server_store.__name__,)
    with monkeypatch.context() as patch:
        patch.setattr(server, "_HOME", tmp_path)
        assert server_store._home() == tmp_path
        assert server._clone_root() == tmp_path / "vaults"
    assert server_store._HOME is None


def _test_tree_files() -> list[Path]:
    """The root conftest, ``test/``, and the builtin apps' own test packages."""
    root = source_corpus.repo_root()
    test_root = source_corpus.repo_root() / "test"
    found = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root):
            found.append(path)
        elif rel.startswith("src/kiro_crew/apps/builtins/") and "/tests/" in rel:
            found.append(path)
    return found


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, read by both of the scan's readers."""
    spellings = {FACADE: FACADE, f"{PKG}.syncer.server": FACADE}
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "md_notebook" not in text:
            continue
        found |= facade_mirror_scan.text_patched_names(text, spellings).get(FACADE, set())
        found |= facade_mirror_scan.patched_names(text, {FACADE: FACADE}).get(FACADE, set())
    return frozenset(found)


def test_every_name_the_corpus_patches_through_the_facade_is_routed() -> None:
    index = _index()
    corpus = _corpus_patched_names()
    assert sorted(KNOWN_SCANNED_SEAMS - corpus) == []
    routed = set(index.holders) | set(index.facade_only)
    assert sorted(corpus - routed) == []


def _bindings(members: tuple[str, ...], name: str) -> dict[str, object]:
    """What each member of the family binds *name* to, read off its namespace."""
    return {member: vars(importlib.import_module(member)).get(name, _MISSING) for member in members}


@pytest.mark.parametrize("name", SEAMS)
def test_a_patch_through_the_facade_reaches_every_holder_and_its_undo_restores_each(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    members = (FACADE, *_owner_paths())
    before = _bindings(members, name)
    bound = tuple(member for member, value in before.items() if value is not _MISSING)
    readers = {owner for owner, path in _owner_paths().items() if name in _global_reads(path)} | (
        {FACADE} if name in _global_reads(Path(server.__file__ or "")) else set()
    )
    assert bound and readers <= set(bound), (bound, readers)
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(server, name, sentinel)
        assert getattr(server, name) is sentinel
        assert _bindings(bound, name) == {member: sentinel for member in bound}
    assert _bindings(members, name) == before


#: The gateway's launch (``apps/backend.py``), with the server start replaced by a probe of
#: the module the launch runs as ``__main__`` and the one ``syncer`` imports — which the fix
#: makes the SAME object (``main()`` aliases the running copy under the canonical name before
#: ``create_app()`` imports ``syncer``). ``runpy`` restores ``sys.modules["__main__"]`` to the
#: launcher script after the run, so the shared identity is read off the canonical key.
_LAUNCH = """
import json, runpy, sys
import aiohttp.web
from kiro_crew import _facade_mirror
FACADE = "kiro_crew.apps.builtins.md_notebook.server"
seen = {}
def _run_app(app, **kwargs):
    main = sys.modules["__main__"]
    index = vars(main)[_facade_mirror.INDEX_ATTRIBUTE]
    # divergent() is empty whenever no patch is active (_facade_mirror.py): one
    # object per name on the launched copy. Read here, during the run, while the
    # launched copy IS __main__ — after the run runpy restores __main__ to the
    # launcher's own -c script, whose globals (e.g. json) leak into the facade
    # dict and make a post-run read of divergent() meaningless.
    seen["main"] = [type(main).__name__, list(index.divergent()), main.logger.name]
    # The fix ran in main() before this: the canonical key now IS the running copy.
    seen["one_copy_at_launch"] = main is sys.modules[FACADE]
aiohttp.web.run_app = _run_app
try:
    runpy.run_module(FACADE, run_name="__main__", alter_sys=True)
except SystemExit as exc:
    seen["exit"] = exc.code
from kiro_crew.apps.builtins.md_notebook import server, syncer
seen["canonical"] = [type(server).__name__, server.logger.name]
seen["syncer_reads_canonical"] = syncer.server is server
seen["canonical_is_mirror"] = server is sys.modules[FACADE]
print(json.dumps(seen))
"""


def test_the_gateway_launch_runs_one_copy_with_the_mirror(tmp_path: Path) -> None:
    """Run as ``__main__``, the facade installs the mirror before ``main()`` starts the
    server, and ``main()`` aliases the running copy under the canonical name so ``syncer``
    imports that same object rather than loading a second copy. The launched copy holds one
    mirror per name (``divergent()`` empty, ``logger`` included), and the whole process
    shares one facade — so its locks, caches and token memo are process-wide."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "user")
    env["MD_NOTEBOOK_HOME"] = str(tmp_path / "home")
    env["KIROCREW_HOME"] = str(tmp_path / "crew")
    env["MD_NOTEBOOK_NO_PICKER"] = "1"
    src = str(Path(kiro_crew.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", _LAUNCH],
        capture_output=True,
        check=False,
        env=env,
        cwd=tmp_path,
        timeout=60,
        **UTF8_TEXT,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.splitlines()[-1]) == {
        "main": ["MirroredModule", [], FACADE],
        "one_copy_at_launch": True,
        "exit": 0,
        "canonical": ["MirroredModule", FACADE],
        "syncer_reads_canonical": True,
        "canonical_is_mirror": True,
    }
    assert f" {FACADE} INFO Notes backend starting on 127.0.0.1:" in out.stderr


def test_the_star_import_surface_keeps_every_base_public_name() -> None:
    """The module had no ``__all__`` before the split, so ``from server import *`` carried
    every public global. The derived ``__all__`` keeps that surface: every public name the
    facade binds or serves, the inventory's project names among them."""
    inventory_path = _THIS_FILE.parent / "campaign_facade_inventories" / f"{FACADE}.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    exported = set(server.__all__)
    assert sorted(set(inventory["base_public_names"]) - exported) == []
    assert {"Path", "asyncio", "web"} <= exported
    assert [name for name in server.__all__ if not hasattr(server, name)] == []


def test_api_error_keeps_the_facade_module_name() -> None:
    """A traceback or a pickle names the class ``server.ApiError``, the module callers import."""
    assert server.ApiError is server_http.ApiError
    assert (server.ApiError.__module__, server.ApiError.__qualname__) == (FACADE, "ApiError")
