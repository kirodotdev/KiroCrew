"""The policy-distribution facade keeps the patch seams and the surface the one module had.

``kiro_crew.platform.policy_distribution`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over three sibling owners -- ``policy_distribution_source``, ``policy_distribution_cache`` and
``policy_distribution_boot`` -- and keeps the live-refresh path, the refresher thread and the
read-only posture itself. Callers, tests and documents still reach every name as
``kiro_crew.platform.policy_distribution.<name>``, and the moved code reads its names as plain
module globals, so a patch through the facade reaches that code only because the mirror
writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this facade: every name the test trees patch through it
reaches every module holding it; the owners are exactly the three siblings, loaded lowest
layer first; every project import the unsplit module bound stays bound on the facade; the
process state an owner rebinds is read through the facade as that owner holds it; the family
logs under the facade's name; the hand-written ``__all__`` is the one the unsplit module
declared; and the facade still runs as ``python -m``.
"""

from __future__ import annotations

import ast
import functools
import hashlib
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
from kiro_crew.platform import policy_distribution
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_policy_distribution_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate", "owners_in_layer_order")

FACADE = "kiro_crew.platform.policy_distribution"
#: The owners, lowest layer first: each imports only the ones listed before it.
OWNERS = (
    "kiro_crew.platform.policy_distribution_source",
    "kiro_crew.platform.policy_distribution_cache",
    "kiro_crew.platform.policy_distribution_boot",
)
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the tier
#: has: the fetcher registry, a transport primitive, a cache write the boot owner and the
#: facade both call, the fetch slot, the boot path's fetch and validation, the facade's own
#: refresh, and project names the family only imports. The scan below must find all of
#: them, so a scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_FETCHERS",
        "_open",
        "write_cache",
        "_claim_fetch_slot",
        "fetch_once",
        "validate_ceiling",
        "refresh_now",
        "mark_governance_incident",
        "path_writable_by_current_user",
    }
)

#: What the facade itself defines: the refresh vocabulary, the live-refresh path, the
#: refresher thread with its post-install hooks, and the read-only posture.
_KEPT = frozenset(
    {
        "REFRESH_NOT_CONFIGURED",
        "REFRESH_UNCHANGED",
        "REFRESH_APPLIED",
        "REFRESH_REJECTED",
        "REFRESH_UNREACHABLE",
        "RefreshOutcome",
        "_recompose_differs",
        "apply_ceiling",
        "refresh_now",
        "_restore_cache",
        "_refresh_failure",
        "_AUDIT_REASONS",
        "_audit_refresh",
        "_Refresher",
        "_REFRESHER",
        "register_post_install_hook",
        "_run_post_install_hooks",
        "start_refresher",
        "stop_refresher",
        "refresher_running",
        "POSTURE_ERROR_MISCONFIGURED",
        "distribution_posture",
    }
)

#: The unsplit module's hand-written ``__all__``: 42 names, digest of the sorted list.
_BASE_ALL_DIGEST = "56ff599d87d0807f"
_BASE_ALL_SIZE = 42


def _index() -> _facade_mirror.MirrorIndex:
    return policy_distribution.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_path(owner: str) -> Path:
    return Path(policy_distribution.__file__).resolve().parent / f"{owner.rsplit('.', 1)[1]}.py"


def _family() -> list[ModuleType]:
    return [policy_distribution, *(sys.modules[name] for name in _index().owners)]


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
        if "policy_distribution" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``policy_distribution.<name>`` was what
    every reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(policy_distribution, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        before = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(policy_distribution, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(policy_distribution, name) is not sentinel:
                stale.append(FACADE)
        stale += [m.__name__ for m in holders if vars(m).get(name) is not before[m.__name__]]
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``policy_distribution.py`` was a plain module, so no reader fetched a name of it
    through ``importlib.import_module``: no legacy table is forwarded, the default
    ``forward_reads`` stays, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_facade_defines_its_own_surface_and_owners_define_the_rest() -> None:
    """The live-refresh path, the refresher and the posture are the facade's own code, and
    no owner holds a second definition of any of them."""
    assert sorted(_KEPT - set(vars(policy_distribution))) == []
    assert sorted(_KEPT & set(_index().exports)) == []
    for name in _KEPT:
        value = vars(policy_distribution)[name]
        if callable(value):
            assert value.__module__ == FACADE, name
    owners_defining = {
        name: owner
        for owner in _index().owners
        for name in _facade_mirror._defined_names(
            ast.parse(_owner_path(owner).read_text(encoding="utf-8"))
        )
    }
    assert sorted(_KEPT & set(owners_defining)) == []


def _owner_project_imports() -> dict[str, str]:
    """Each name an owner imports from a project module outside the family -> that home."""
    found: dict[str, str] = {}
    for owner in OWNERS:
        for node in ast.parse(_owner_path(owner).read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if node.module.startswith("kiro_crew") and node.module not in OWNERS:
                    for alias in node.names:
                        found[alias.asname or alias.name] = node.module
    return found


def test_every_project_name_the_family_imports_stays_bound_on_the_facade() -> None:
    """At BASE ``policy_distribution.py`` bound each of these by import, so ``from
    kiro_crew.platform.policy_distribution import config_dir`` type-checked and resolved. The
    facade still binds every one of them by its own import, as the same object its home holds,
    so none is served from an owner and none needs a second manifest."""
    imported = _owner_project_imports()
    assert imported, "no owner imports a project name -- the scan went blind"
    served = sorted(set(imported) & set(_index().exports))
    assert served == []
    wrong = [
        name
        for name, home in sorted(imported.items())
        if vars(policy_distribution).get(name) is not getattr(importlib.import_module(home), name)
    ]
    assert wrong == []


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs: the
    tier's log-capture tests filter on that name."""
    assert policy_distribution.logger.name == FACADE
    others = [
        m.__name__
        for m in _family()
        if vars(m).get("logger", policy_distribution.logger) is not policy_distribution.logger
    ]
    assert others == []


def test_the_process_state_is_read_through_the_facade_as_its_owner_holds_it() -> None:
    """``reset_process_state`` rebinds the installed-document digest and the cache-directory
    memo in their one owner; a read through the facade sees what that owner now holds, and
    the facade binds no copy of either."""
    cache = sys.modules["kiro_crew.platform.policy_distribution_cache"]
    names = ("_installed_body_digest", "_cache_dir_ready")
    original = {name: vars(cache)[name] for name in names}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(policy_distribution, "_installed_body_digest", "planted-digest")
        patch.setattr(policy_distribution, "_cache_dir_ready", Path("planted-dir"))
        assert vars(cache)["_installed_body_digest"] == "planted-digest"
        policy_distribution.reset_process_state()
        assert vars(cache)["_installed_body_digest"] == ""
        assert vars(cache)["_cache_dir_ready"] is None
        for name in names:
            assert getattr(policy_distribution, name) is vars(cache)[name]
            assert name not in vars(policy_distribution)
    assert {name: vars(cache)[name] for name in names} == original
    assert not set(names) & set(vars(policy_distribution))


def test_the_facade_declares_the_unsplit_modules_all() -> None:
    """``__all__`` stays the hand-written list ``policy_distribution.py`` declared: a star
    import binds those names, every one of which still resolves, and nothing the owners
    import."""
    names = sorted(policy_distribution.__all__)
    digest = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()[:16]
    assert (len(names), digest) == (_BASE_ALL_SIZE, _BASE_ALL_DIGEST)
    assert [name for name in names if not hasattr(policy_distribution, name)] == []


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.platform.policy_distribution`` imported cleanly before the split;
    the mirror installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(Path(policy_distribution.__file__).resolve().parents[2])
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
    assert source_corpus.repo_root() / "test" / "test_policy_distribution_facade.py" in walked
