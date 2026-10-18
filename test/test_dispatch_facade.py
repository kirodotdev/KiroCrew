"""The shared channel turn pipeline facade keeps the patch seams the one module had.

``kiro_crew.messaging.dispatch`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over
the ``kiro_crew.messaging.dispatch_<role>`` owners, and keeps the turn loop itself
(``ChannelTurns``, ``drive_turn``, the compaction retry guard, the hook auto-reply and the
directive consumer). Callers, tests and documents still reach every name as
``kiro_crew.messaging.dispatch.<name>``, and the moved code reads its names as plain module
globals, so a patch through the facade reaches that code only because the mirror writes it
into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this facade: every name the test trees patch through it
reaches every module holding it; the owners are exactly the ``dispatch_*.py`` siblings,
loaded lowest layer first; the turn loop and the re-exported turn-bracket helpers stay bound
on the facade; the names served from outside the family stay visible to type checkers; the
family logs under the facade's name; the classes named in tracebacks and reprs keep the
facade's module; and the facade still runs as ``python -m``.
"""

from __future__ import annotations

import ast
import asyncio
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
from kiro_crew.messaging import dispatch
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_dispatch_facade")

FACADE = "kiro_crew.messaging.dispatch"
OWNER_PREFIX = "kiro_crew.messaging.dispatch_"
MESSAGING_DIR = Path(dispatch.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the pipeline
#: has: collaborators the turn loop calls, functions it imports from owners, and names only
#: an owner reads (``channel_inbound_permitted``, ``spool_refused_turn_sync``). The scan below
#: must find all of them, so a scanner that stops matching goes red instead of passing over
#: nothing.
_KNOWN_SEAMS = frozenset(
    {
        "TurnDriver",
        "run_in_embed_pool",
        "publish_turn_identity",
        "session_store_for_turn",
        "inbound_permitted",
        "channel_inbound_permitted",
        "spool_refused_turn",
        "spool_refused_turn_sync",
        "charge_turn_failure",
        "open_turn_crew_log",
        "conversation_is_muted",
    }
)

#: Moved classes whose name reaches a traceback, a log line or a repr: they keep the module
#: callers import them from.
_NAMED_CLASSES = {
    "Drift": "dispatch_contract",
    "Audience": "dispatch_contract",
    "Verdict": "dispatch_contract",
    "_MonitorGenerationChanged": "dispatch_contract",
    "ToollessTurnUnavailable": "dispatch_tools",
}


def _index() -> _facade_mirror.MirrorIndex:
    return dispatch.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('dispatch_'):]}": path
        for path in sorted(MESSAGING_DIR.glob("dispatch_*.py"))
    }


def _family() -> list[ModuleType]:
    return [dispatch, *(sys.modules[name] for name in _index().owners)]


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
        if "messaging" not in text or "dispatch" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that finds nothing would pass the reach test over nothing."""
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``dispatch.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(dispatch, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(dispatch, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(dispatch, name) is not sentinel:
                stale.append(FACADE)
        restored = [m.__name__ for m in holders if vars(m).get(name) is sentinel]
        if stale or restored:
            missed[name] = stale + restored
    assert missed == {}
    assert _index().divergent() == ()


def test_an_owner_reading_a_name_only_it_binds_sees_the_facade_patch() -> None:
    """``inbound_permitted`` lives in an owner and reads ``channel_inbound_permitted`` as a
    global the facade does not bind; a patch through the facade is the gate it consults."""
    calls: list[str] = []

    async def deny(channel_type: str) -> bool:
        calls.append(channel_type)
        return False

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(dispatch, "channel_inbound_permitted", deny)
        assert "channel_inbound_permitted" not in vars(dispatch)
        assert asyncio.run(dispatch.inbound_permitted("probe")) is False
    assert calls == ["probe"]


def test_the_owners_are_every_dispatch_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``dispatch_*.py`` sibling, and each owner imports only owners
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
    """``dispatch.py`` was a plain module, so no reader ever fetched a name of it through
    ``importlib.import_module``: the facade forwards no legacy table and keeps the default
    ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_facade_keeps_the_turn_loop_and_its_re_exports() -> None:
    """The turn loop and the collaborators only it uses stay defined in ``dispatch.py``, and
    so do the turn-bracket helpers re-exported for their importers."""
    defined_here = {
        "ChannelTurns",
        "drive_turn",
        "_given",
        "_TransientCompactionRetryGuard",
        "hook_auto_reply",
        "build_directive_consumer",
        "_ChannelDirectiveState",
    }
    assert sorted(defined_here - set(vars(dispatch))) == []
    assert sorted(n for n in defined_here if getattr(dispatch, n).__module__ != FACADE) == []
    re_exported = {
        "consume_reinjection",
        "driver_turn_landed",
        "rearm_reinjection",
        "rollback_skill_bodies",
        "stop_reason_landed",
    }
    assert sorted(re_exported - set(vars(dispatch))) == []
    assert sorted((defined_here | re_exported) & set(_index().exports)) == []


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
    tree = ast.parse(Path(dispatch.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """At BASE the module bound these by import, so ``from kiro_crew.messaging.dispatch
    import spool_refused_turn_sync`` type-checked. Now an owner holds them, so the facade
    names each one from its home module under ``TYPE_CHECKING``, and exactly those."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert dispatch.logger.name == FACADE
    loggers = [m.__name__ for m in _family() if "logger" in vars(m)]
    assert len(loggers) > 2, "no owner binds a logger -- the check went blind"
    others = [m for m in loggers if vars(sys.modules[m])["logger"] is not dispatch.logger]
    assert others == []


def test_named_classes_keep_the_facade_module_and_others_their_owner() -> None:
    """A moved exception or enum is named in tracebacks, log lines and reprs, so it keeps
    the module callers import it from; every other moved class names the owner defining it."""
    defined: dict[str, str] = {}
    for owner, path in _owner_paths().items():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ClassDef):
                defined[node.name] = owner
    assert set(_NAMED_CLASSES) <= set(defined)
    for name, owner in _NAMED_CLASSES.items():
        cls = getattr(dispatch, name)
        assert cls is vars(sys.modules[defined[name]])[name], name
        assert defined[name] == f"{OWNER_PREFIX}{owner[len('dispatch_'):]}", name
        assert cls.__module__ == FACADE, name
    others = {
        name: getattr(dispatch, name).__module__
        for name, owner in defined.items()
        if name not in _NAMED_CLASSES and getattr(dispatch, name).__module__ != owner
    }
    assert others == {}
    assert {"ChannelTurn", "Asker", "TurnRecord", "TurnOutcome"} <= set(defined)


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.messaging.dispatch`` imported cleanly before the split; the
    mirror installs on ``__main__`` too, because every member that logs binds the one logger
    by its literal name."""
    env = dict(os.environ)
    src = str(Path(dispatch.__file__).resolve().parents[2])
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
    assert source_corpus.repo_root() / "test" / "test_messaging_dispatch.py" in walked


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert _THIS_FILE.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)
