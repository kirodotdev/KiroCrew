"""The dashboard token-auth facade keeps the patch seams the one module had.

``kiro_crew.dashboard.token_auth`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.dashboard.token_auth_<role>`` owners, and keeps the middleware
itself. Callers, tests and documents still reach every name as
``kiro_crew.dashboard.token_auth.<name>``, and the moved code reads its names as plain
module globals, so a patch through the facade reaches that code only because the mirror
writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules.
These tests pin what is particular to this facade: every name the test trees patch
through it reaches every module holding it; the owners are exactly the
``token_auth_*.py`` siblings, loaded lowest layer first; the middleware and the
re-exported secret and revocation names stay bound on the facade; the singletons an
owner rebinds are read through the facade as the owner holds them; the family logs
under the facade's name; and the facade still runs as ``python -m``.
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

from kiro_crew import _facade_mirror
from kiro_crew.dashboard import token_auth
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_token_auth_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.dashboard.token_auth"
OWNER_PREFIX = "kiro_crew.dashboard.token_auth_"
DASHBOARD_DIR = Path(token_auth.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam token
#: auth has: the token state and the singletons owners rebind, functions the middleware
#: calls, a module the whole family reads, and names the family only imports. The scan
#: below must find all of them, so a scanner that stops matching goes red instead of
#: passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_state",
        "_revoked_store_singleton",
        "_app_perms_cache",
        "_APP_WINDOW_EXCLUDED_PATHS",
        "_app_api_allowlist",
        "generate_token",
        "revoke_all_sessions",
        "time",
        "_get_secret",
        "_sel_fn",
        "is_loopback",
        "request_is_unix_socket",
        "session_keys_bound_to_pid",
        "verify_session_token",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return token_auth.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('token_auth_'):]}": path
        for path in sorted(DASHBOARD_DIR.glob("token_auth_*.py"))
    }


def _family() -> list[ModuleType]:
    return [token_auth, *(sys.modules[name] for name in _index().owners)]


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
        if "token_auth" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``token_auth.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(token_auth, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(token_auth, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(token_auth, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_token_auth_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``token_auth_*.py`` sibling, and each owner imports only
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
    """``token_auth.py`` was a plain module, so no reader ever fetched a name of it through
    ``importlib.import_module``: the facade forwards no legacy table and keeps the default
    ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_facade_binds_the_middleware_and_its_re_exports() -> None:
    """The middleware stays in ``token_auth.py``, and so do the secret and revocation
    re-exports callers import from it, the four no family member reads included."""
    kept = {
        "token_auth_middleware",
        "internal_path_matches",
        "_SECRET_KEY_FILE",
        "_get_secret",
        "_load_or_create_secret",
        "_REVOCATION_FILE",
        "_load_revocation_gen",
        "bump_revocation_gen",
        "current_revocation_gen",
        "current_revocation_gen_or_none",
    }
    assert sorted(kept - set(vars(token_auth))) == []
    assert sorted(kept & set(_index().exports)) == []


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
    tree = ast.parse(Path(token_auth.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """At BASE the module bound these by import, so ``from kiro_crew.dashboard.token_auth
    import check_peer_is_self`` type-checked. Now an owner holds them, so the facade names
    each one from its home module under ``TYPE_CHECKING``, and exactly those."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert token_auth.logger.name == FACADE
    others = [
        m.__name__
        for m in _family()
        if vars(m).get("logger", token_auth.logger) is not token_auth.logger
    ]
    assert others == []


def test_a_facade_patch_of_the_revoked_store_is_what_the_store_getter_returns() -> None:
    """``_get_revoked_store`` rebinds its singleton in its one owner; a store patched in
    through the facade is the one it hands back, and the undo leaves the facade binding
    no copy of its own."""
    store = object()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(token_auth, "_revoked_store_singleton", store)
        assert token_auth._get_revoked_store() is store
    assert "_revoked_store_singleton" not in vars(token_auth)


def test_a_registered_app_window_path_is_read_through_the_facade() -> None:
    """``register_app_window_paths`` rebinds the excluded-path set in its one owner, and a
    read through the facade sees the set that owner now holds."""
    paths = sys.modules[f"{OWNER_PREFIX}paths"]
    prior = token_auth._APP_WINDOW_EXCLUDED_PATHS
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(token_auth, "_APP_WINDOW_EXCLUDED_PATHS", prior)
        token_auth.register_app_window_paths(["/app-windows/probe/main.html"])
        current = vars(paths)["_APP_WINDOW_EXCLUDED_PATHS"]
        assert token_auth._APP_WINDOW_EXCLUDED_PATHS is current
        assert current == frozenset({"/app-windows/probe/main.html"})
    assert vars(paths)["_APP_WINDOW_EXCLUDED_PATHS"] is prior
    assert "_APP_WINDOW_EXCLUDED_PATHS" not in vars(token_auth)


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.dashboard.token_auth`` imported cleanly before the split; the
    mirror installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(Path(token_auth.__file__).resolve().parents[2])
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
    assert source_corpus.repo_root() / "test" / "test_token_auth.py" in walked
