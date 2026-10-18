"""The governance facade keeps the patch seams and the surface the one module had.

``kiro_crew.platform.governance`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.platform.governance_parts`` owners, and keeps the auto-approve
surface and the two boot checks itself. Callers, tests and documents still reach every
name as ``kiro_crew.platform.governance.<name>``, and the moved code reads its names as
plain module globals, so a patch through the facade reaches that code only because the
mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules.
These tests pin what is particular to this facade: every name the test trees patch
through it reaches every module holding it; the owners are exactly the
``governance_parts`` modules, loaded lowest layer first; the kept code and every project
import the unsplit module bound stay bound on the facade; the tier state an owner
rebinds is read through the facade as that owner holds it; the family logs under the
facade's name; the hand-written ``__all__`` is the one the unsplit module declared; the
trust-root literal the code-review grep looks for sits once in the facade, in its boot
check; and the facade still runs as ``python -m``.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import importlib
import os
import pkgutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.platform import governance, governance_parts
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_governance_parts_facade")

FACADE = "kiro_crew.platform.governance"
PACKAGE = "kiro_crew.platform.governance_parts"
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the
#: governance model has: a path accessor the loader reads, a function the facade's own
#: code calls, functions owners call each other through, a catalog table, the tier
#: state reader, and a name the family only imports. The scan below must find all of
#: them, so a scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_policy_home_path",
        "active_update_pins",
        "may_skip_gate_now",
        "sel",
        "last_composed_ceiling",
        "_declared_auto_approve",
        "_policy_signature_required",
        "resolve",
        "gate_decision",
        "SCOPE_CATALOG",
        "_read_json_file",
    }
)

#: What the facade itself defines: the auto-approve surface and the two boot checks.
_KEPT = frozenset(
    {
        "BUILTIN_TOOL_SCOPES",
        "BUILTIN_TOOL_CAPABILITIES",
        "_capability_restricts",
        "_FLOOR_GATE_SCOPES",
        "_ceiling_mentions_mcp_server",
        "_ruleset_has_an_opinion",
        "may_skip_gate",
        "_declared_auto_approve",
        "_auto_approve_is_honoured",
        "_undeclared_verbs",
        "_audit_auto_approve_honoured",
        "_is_owner_written",
        "_is_honourable_shape",
        "strip_ungoverned_auto_approve",
        "_confirm_tool_patterns",
        "_config_files_state",
        "confirm_tools_unknown",
        "confirm_tools_unknown_now",
        "server_requires_confirmation",
        "_operator_requires_confirmation",
        "may_skip_gate_now",
        "withheld_by_confirm_tools",
        "_policy_may_skip_gate_now",
        "sanitize_agent_config_governance",
        "assert_governance_floor",
        "assert_governance_paths_protected",
    }
)

#: The unsplit module's hand-written ``__all__``: 59 names, digest of the sorted list.
_BASE_ALL_DIGEST = "8fc3ef77954d1350"
_BASE_ALL_SIZE = 59


def _index() -> _facade_mirror.MirrorIndex:
    return governance.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{PACKAGE}.{info.name}": Path(governance_parts.__path__[0]) / f"{info.name}.py"
        for info in pkgutil.iter_modules(governance_parts.__path__)
    }


def _family() -> list[ModuleType]:
    return [governance, *(sys.modules[name] for name in _index().owners)]


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
        if "governance" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that finds nothing would pass the reach test over nothing."""
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``governance.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(governance, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        before = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(governance, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(governance, name) is not sentinel:
                stale.append(FACADE)
        stale += [m.__name__ for m in holders if vars(m).get(name) is not before[m.__name__]]
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_governance_parts_module_in_layer_order() -> None:
    """``_OWNERS`` lists every module of the package, and each owner imports only owners
    listed before it -- the order the facade loads them and a read resolves in."""
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
    """``governance.py`` was a plain module, so no reader fetched a name of it through
    ``importlib.import_module``: no legacy table is forwarded, the default
    ``forward_reads`` stays, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_facade_defines_its_own_surface_and_owners_define_the_rest() -> None:
    """The auto-approve surface and the boot checks are the facade's own functions --
    ``cli_doctor`` and ``agent`` re-export ``may_skip_gate_now`` with the facade as its
    module -- and no owner holds a second definition of any of them."""
    assert sorted(_KEPT - set(vars(governance))) == []
    assert sorted(_KEPT & set(_index().exports)) == []
    for name in _KEPT:
        value = vars(governance)[name]
        if callable(value):
            assert value.__module__ == FACADE, name
    owners_defining = {
        name: owner
        for owner in _index().owners
        for name in _facade_mirror._defined_names(
            ast.parse(_owner_paths()[owner].read_text(encoding="utf-8"))
        )
    }
    assert sorted(_KEPT & set(owners_defining)) == []


def _base_project_imports() -> dict[str, str]:
    """Each name an owner imports from a project module outside the family -> that home."""
    family = set(_index().owners)
    found: dict[str, str] = {}
    for path in _owner_paths().values():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if node.module.startswith("kiro_crew") and node.module not in family:
                    for alias in node.names:
                        found[alias.asname or alias.name] = node.module
    return found


def test_every_project_name_the_family_imports_stays_bound_on_the_facade() -> None:
    """At BASE ``governance.py`` bound each of these by import, so ``from
    kiro_crew.platform.governance import config_dir`` type-checked and resolved. The facade
    still binds every one of them by its own import, as the same object its home holds,
    so none is served from an owner and none needs a second manifest."""
    imported = _base_project_imports()
    assert imported, "no owner imports a project name -- the scan went blind"
    served = sorted(set(imported) & set(_index().exports))
    assert served == []
    wrong = [
        name
        for name, home in sorted(imported.items())
        if vars(governance).get(name) is not getattr(importlib.import_module(home), name)
    ]
    assert wrong == []


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert governance.logger.name == FACADE
    others = [
        m.__name__
        for m in _family()
        if vars(m).get("logger", governance.logger) is not governance.logger
    ]
    assert others == []


def test_the_tier_state_is_read_through_the_facade_as_its_owner_holds_it() -> None:
    """``reset_process_state`` rebinds ``_process_state`` in its one owner; a read through
    the facade sees the state that owner now holds, and the facade binds no copy."""
    tiers = sys.modules[f"{PACKAGE}.tiers"]
    original = governance._process_state
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(governance, "_process_state", original)
        governance.reset_process_state()
        current = vars(tiers)["_process_state"]
        assert current is not original
        assert governance._process_state is current
        assert "_process_state" not in vars(governance)
    assert vars(tiers)["_process_state"] is original
    assert "_process_state" not in vars(governance)


def test_the_facade_declares_the_unsplit_modules_all() -> None:
    """``__all__`` stays the hand-written list ``governance.py`` declared: a star import
    binds those names, every one of which still resolves, and nothing the owners import."""
    names = sorted(governance.__all__)
    digest = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()[:16]
    assert (len(names), digest) == (_BASE_ALL_SIZE, _BASE_ALL_DIGEST)
    assert [name for name in names if not hasattr(governance, name)] == []


def test_the_trust_root_tuple_the_code_review_grep_reads_is_the_facades_boot_check() -> None:
    """``code-review.yml`` greps ``governance.py`` for the denied-commands trust-root entry.
    It appears once there, inside ``assert_governance_paths_protected``, and in no owner,
    so the grep keeps reading the check itself rather than a stray copy."""
    literal = '".kiro/crew/denied_commands.json"'
    text = Path(governance.__file__).read_text(encoding="utf-8")
    assert text.count(literal) == 1
    tree = ast.parse(text)
    check = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "assert_governance_paths_protected"
    )
    assert literal in ast.get_source_segment(text, check)
    for path in _owner_paths().values():
        assert literal not in path.read_text(encoding="utf-8"), path.name


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.platform.governance`` imported cleanly before the split; the
    mirror installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(Path(governance.__file__).resolve().parents[2])
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
    assert source_corpus.repo_root() / "test" / "test_governance_parts_facade.py" in walked


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert _THIS_FILE.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)
