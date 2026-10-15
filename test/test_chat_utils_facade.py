"""The dashboard chat helpers' facade keeps the patch seams the one module had.

``kiro_crew.dashboard.chat_utils`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.dashboard.chat_utils_<role>`` owners. Callers, tests and documents
still reach every name as ``kiro_crew.dashboard.chat_utils.<name>``, and the moved code
reads its names as plain module globals, so a patch through the facade reaches that code
only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules
(one object per name, the manifest, owners never importing their facade, no shared
global rebound). These tests pin what is particular to this facade: every name the test
trees patch through it reaches every module holding it; the owners are exactly the
``chat_utils_*.py`` siblings, loaded lowest layer first; the code the facade keeps for
itself stays bound there; the family logs under the facade's name; the moved enums keep
the facade's module; and the facade still runs as ``python -m``.
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
from kiro_crew.dashboard import chat_utils
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_chat_utils_facade")

FACADE = "kiro_crew.dashboard.chat_utils"
OWNER_PREFIX = "kiro_crew.dashboard.chat_utils_"
DASHBOARD_DIR = Path(chat_utils.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the
#: helpers have: a table the facade keeps, an owner's cache state, functions several
#: owners call, and names the family only imports. The scan below must find all of them,
#: so a scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_DEPRECATED_MODEL_MAP",
        "_DISPLAY_CLEAN_GENERATION",
        "_DISPLAY_REDACTION_SALT",
        "_display_redaction_cache_key",
        "_installed_theme_persona",
        "dashboard_slot_key",
        "effective_session_key",
        "run_config_write",
        "subagents_attached_async",
        "allowed_hosts_for",
        "redact_credentials",
        "sel",
        "set_dashboard_surfaced",
        "tighten_live_session_execution",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return chat_utils.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('chat_utils_'):]}": path
        for path in sorted(DASHBOARD_DIR.glob("chat_utils_*.py"))
    }


def _family() -> list[ModuleType]:
    return [chat_utils, *(sys.modules[name] for name in _index().owners)]


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
        if "chat_utils" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that finds nothing would pass the reach test over nothing."""
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``chat_utils.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(chat_utils, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(chat_utils, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(chat_utils, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_chat_utils_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``chat_utils_*.py`` sibling, and each owner imports only
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
    """``chat_utils.py`` was a plain module, so no reader ever fetched a name of it through
    ``importlib.import_module``: the facade forwards no legacy table and keeps the default
    ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_facade_binds_the_code_it_keeps_for_itself() -> None:
    """What stays in ``chat_utils.py`` stays bound there, not served from an owner: the
    deprecated model aliases, whose literals a review grep judges on added lines; the
    three tool-event writers annotated with the one baselined ``LLMEvent`` import; the
    transient-notice tokens two parity tests read from this file's text."""
    kept = {
        "_DEPRECATED_MODEL_MAP",
        "_normalize_model",
        "is_deprecated_model",
        "_tool_identity_fields",
        "_broadcast_auto_tool",
        "_broadcast_compaction_result",
        "_build_stream_chunk",
        "history_corpus_unreadable",
        "TRANSIENT_NOTICE_META_KEY",
        "TRANSIENT_NOTICE_RETRYING",
        "TRANSIENT_NOTICE_RESUMING",
        "TRANSIENT_NOTICE_GIVE_UP",
    }
    index = _index()
    assert sorted(kept - set(vars(chat_utils))) == []
    assert sorted(kept & set(index.exports)) == []


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
    tree = ast.parse(Path(chat_utils.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """At BASE the module bound these by import, so ``from kiro_crew.dashboard.chat_utils
    import sel`` type-checked. Now an owner holds them, so the facade names each one from
    its home module under ``TYPE_CHECKING``; besides those it names only ``LLMEvent``, which
    its own annotations read."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    own = {"LLMEvent": ("kiro_crew.providers.base", "LLMEvent")}
    assert _home_manifest() == {**expected, **own}


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert chat_utils.logger.name == FACADE
    others = [
        m.__name__
        for m in _family()
        if vars(m).get("logger", chat_utils.logger) is not chat_utils.logger
    ]
    assert others == []


def test_the_moved_enums_keep_the_facade_module() -> None:
    """``ResetCause`` and ``RecoveryPayload`` are named by the path callers import them
    from, and that path still resolves them."""
    for cls in (chat_utils.ResetCause, chat_utils.RecoveryPayload):
        assert cls.__module__ == FACADE
        assert getattr(importlib.import_module(cls.__module__), cls.__qualname__) is cls


def test_a_read_through_the_facade_follows_a_rebound_owner_global() -> None:
    """Clearing the display-redaction cache rebinds the clean-verdict sets in their one
    owner; a read through the facade returns that owner's current binding, and the facade
    binds no copy of its own that could go stale."""
    display = sys.modules[f"{OWNER_PREFIX}display"]
    chat_utils._clear_display_redaction_cache()
    assert chat_utils._display_clean_young is vars(display)["_display_clean_young"]
    assert chat_utils._display_clean_old is vars(display)["_display_clean_old"]
    assert "_display_clean_young" not in vars(chat_utils)
    assert chat_utils._display_redaction_cache_info() == (0, 0)


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.dashboard.chat_utils`` imported cleanly before the split; the
    mirror installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(Path(chat_utils.__file__).resolve().parents[2])
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
    assert source_corpus.repo_root() / "test" / "test_chat_utils.py" in walked


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert _THIS_FILE.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)
