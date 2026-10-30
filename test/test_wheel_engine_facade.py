"""The update-engine facade keeps the patch seams and the surface the one module had.

``kiro_crew.platform.wheel_engine`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over
four sibling owners -- ``wheel_engine_contract``, ``wheel_engine_layout``,
``wheel_engine_build_child`` and ``wheel_engine_transfer`` -- and keeps the offline trust root,
the manifest signature check that reads it, ``download_verified_wheel`` (whose transfer-owner
download compares the wheel's SHA-256 to the signed digest), the shadow build and promotion,
debris pruning, the update lock and the apply itself. Callers, tests and
``cli.sh``'s drift test still reach every name as ``kiro_crew.platform.wheel_engine.<name>``,
and the moved code reads its names as plain module globals, so a patch through the facade
reaches that code only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this facade: every name the test trees patch through it
reaches every module holding it; the owners are exactly the four siblings, loaded lowest
layer first; every project import the unsplit module bound stays bound on the facade; the
update errors still name the facade as their module, in a traceback too; only the facade
logs; the hand-written ``__all__`` is the one the unsplit module
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
import traceback
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.platform import wheel_engine
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_wheel_engine_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate", "owners_in_layer_order")

FACADE = "kiro_crew.platform.wheel_engine"
#: The owners, lowest layer first: each imports only the ones listed before it.
OWNERS = (
    "kiro_crew.platform.wheel_engine_contract",
    "kiro_crew.platform.wheel_engine_layout",
    "kiro_crew.platform.wheel_engine_build_child",
    "kiro_crew.platform.wheel_engine_transfer",
)
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the engine
#: has: the pinned key, a build step, the spawn seam, the signature check, the staging
#: directory the transfer owner and the facade both read, the manifest fetch, the layout
#: and install-shape probes, the launcher path, and the facade's own apply. The scan below must
#: find all of them, so a scanner that stops matching goes red instead of passing over
#: nothing.
_KNOWN_SEAMS = frozenset(
    {
        "CLI_MANIFEST_PUBLIC_KEY_B64",
        "_run",
        "_spawn_build_child",
        "_verify_signature",
        "_staging_dir",
        "fetch_verified_manifest",
        "managed_venv_layout",
        "running_from_managed_venv",
        "stable_launch_path",
        "apply_wheel_update",
    }
)

#: What the facade itself defines: the trust root with the manifest signature check that
#: reads it, the wheel download entry point, the shadow build and promotion, debris pruning,
#: the update lock and the apply.
_KEPT = frozenset(
    {
        "CLI_MANIFEST_KEY_ID",
        "CLI_MANIFEST_PUBLIC_KEY_B64",
        "_no_duplicates",
        "_openssl_bin",
        "_check_key_fingerprint",
        "_verify_signature",
        "_verify_over_fds",
        "_create_exclusive_nofollow",
        "_verify_over_nofollow_files",
        "parse_and_validate_manifest",
        "fetch_verified_manifest",
        "download_verified_wheel",
        "_owned_siblings",
        "_is_unpromoted_debris",
        "_remove_tombstone",
        "_discard_tree",
        "_release_takes_liveness_lock",
        "_dir_identity",
        "_may_run_without_the_lock",
        "_discard_if_unheld",
        "_require_free_space",
        "build_shadow_venv",
        "_NO_WHEEL_RE",
        "_no_wheel_packages",
        "_no_wheel_message",
        "_PROBE_SOURCE",
        "verify_shadow_venv",
        "promote",
        "repoint_launcher_symlink",
        "_KEEP_PREVIOUS_TREES",
        "_STAGING_STALE_SECS",
        "_pid_alive",
        "_sweep_layout_debris",
        "_completed_trees",
        "_launcher_tree",
        "_pinned_trees",
        "_prune_superseded_trees",
        "_update_lock_path",
        "hold_update_lock",
        "release_update_lock",
        "apply_wheel_update",
        "_check_buildable",
        "_usable_promoted_tree",
        "_repair_dangling_stable_link",
        "_apply_locked",
    }
)

#: The errors a traceback or a log line names, which keep the facade as their module.
_NAMED_BY_THE_FACADE = (
    "WheelUpdateError",
    "WheelUpdateCancelled",
    "WheelUpdateBusy",
    "WheelUpdateNotReady",
    "WheelUpdateSnapshotFailed",
    "WheelUpdateIncompatible",
)

#: The unsplit module's hand-written ``__all__``: 27 names, digest of the sorted list.
_BASE_ALL_DIGEST = "fb5928a2122938f0"
_BASE_ALL_SIZE = 27


def _index() -> _facade_mirror.MirrorIndex:
    return wheel_engine.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_path(owner: str) -> Path:
    return Path(wheel_engine.__file__).resolve().parent / f"{owner.rsplit('.', 1)[1]}.py"


def _family() -> list[ModuleType]:
    return [wheel_engine, *(sys.modules[name] for name in _index().owners)]


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
        if "wheel_engine" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``wheel_engine.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must see
    the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(wheel_engine, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        before = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(wheel_engine, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(wheel_engine, name) is not sentinel:
                stale.append(FACADE)
        stale += [m.__name__ for m in holders if vars(m).get(name) is not before[m.__name__]]
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``wheel_engine.py`` was a plain module, so no reader fetched a name of it through
    ``importlib.import_module``: no legacy table is forwarded, the default ``forward_reads``
    stays, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_facade_defines_its_own_surface_and_owners_define_the_rest() -> None:
    """The shadow build, pruning, the lock and the apply are the facade's own code, and no
    owner holds a second definition of any of them."""
    assert sorted(_KEPT - set(vars(wheel_engine))) == []
    assert sorted(_KEPT & set(_index().exports)) == []
    for name in _KEPT:
        value = vars(wheel_engine)[name]
        if callable(value) and not isinstance(value, type):
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
    """At BASE ``wheel_engine.py`` bound each of these by import, so ``from
    kiro_crew.platform.wheel_engine import data_home`` type-checked and resolved. The facade
    still binds every one of them by its own import, as the same object its home holds, so
    none is served from an owner and none needs a second manifest."""
    imported = _owner_project_imports()
    assert imported, "no owner imports a project name -- the scan went blind"
    served = sorted(set(imported) & set(_index().exports))
    assert served == []
    wrong = [
        name
        for name, home in sorted(imported.items())
        if vars(wheel_engine).get(name) is not getattr(importlib.import_module(home), name)
    ]
    assert wrong == []


def test_the_update_errors_name_the_facade_as_their_module() -> None:
    """A traceback and an error chain render a raised class as ``module.qualname``; the
    update errors still say ``kiro_crew.platform.wheel_engine``, and each is the one class
    object its owner defines. ``ApplyCancel`` is a flag, never raised, and nothing reads its
    module, so it reports the owner that defines it."""
    contract = sys.modules["kiro_crew.platform.wheel_engine_contract"]
    for name in _NAMED_BY_THE_FACADE:
        cls = getattr(wheel_engine, name)
        assert cls is vars(contract)[name], name
        assert cls.__module__ == FACADE, name
    try:
        raise wheel_engine.WheelUpdateError("planted refusal")
    except wheel_engine.WheelUpdateError as exc:
        rendered = traceback.format_exception_only(type(exc), exc)[-1]
    assert rendered.startswith(f"{FACADE}.WheelUpdateError: planted refusal")
    assert wheel_engine.ApplyCancel is vars(contract)["ApplyCancel"]
    assert wheel_engine.ApplyCancel.__module__ == contract.__name__


def test_only_the_facade_logs() -> None:
    """The engine's log lines come from its kept code, under the facade's logger; no owner
    binds a logger of its own."""
    assert wheel_engine.logger.name == FACADE
    assert [m.__name__ for m in _family()[1:] if "logger" in vars(m)] == []


def test_the_facade_declares_the_unsplit_modules_all() -> None:
    """``__all__`` stays the hand-written list ``wheel_engine.py`` declared: a star import
    binds those names, every one of which still resolves, and nothing the owners import."""
    names = sorted(wheel_engine.__all__)
    digest = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()[:16]
    assert (len(names), digest) == (_BASE_ALL_SIZE, _BASE_ALL_DIGEST)
    assert [name for name in names if not hasattr(wheel_engine, name)] == []


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.platform.wheel_engine`` imported cleanly before the split; the
    mirror installs on ``__main__`` too."""
    env = dict(os.environ)
    src = str(Path(wheel_engine.__file__).resolve().parents[2])
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
    assert source_corpus.repo_root() / "test" / "test_wheel_engine_facade.py" in walked
