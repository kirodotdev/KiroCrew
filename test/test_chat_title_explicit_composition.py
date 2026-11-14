"""``kiro_crew.dashboard.chat_title`` composes its explicit-title owner onto one namespace.

:func:`kiro_crew._facade_compose.compose` runs every function
``kiro_crew.dashboard.chat_title_explicit`` defines on ``chat_title``'s globals. These tests
pin that composition:

* every owner function runs on the facade's globals, names the facade as its module, is the
  object the facade (and every old import path) binds, and every global it reads is bound
  there;
* every moved function keeps its kind and signature;
* every name the test trees patch on the facade reaches every owner function that reads it,
  and one such patch is exercised for real;
* the owner imports the facade only for type checking, defines no module-level value and no
  logger, and nothing but the facade imports it;
* the facade composes through the shared helper, once.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import dis
import functools
import inspect
import types
from pathlib import Path

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew._facade_compose import compose as facade_compose
from kiro_crew.dashboard import chat_title, chat_title_explicit

pytestmark = pytest.mark.xdist_group(name="tree_scan_chat_title_explicit")

FACADE = chat_title.__name__
OWNER = chat_title_explicit.__name__
_OWNER_PATH = Path(chat_title_explicit.__file__).resolve()
_SRC = Path(chat_title.__file__).resolve().parents[1]
_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "LOAD_NAME", "STORE_GLOBAL", "DELETE_GLOBAL"})

#: Each moved function's kind and signature in the one-module file.
_BASE_SURFACE: dict[str, tuple[bool, str]] = {
    "generate_manual_title": (True, "(state: 'DashboardState', slot: '_ChatSlot') -> 'str'"),
    "apply_regenerated_title": (
        True,
        "(state: 'DashboardState', slot: '_ChatSlot', title: 'str', *, "
        "still_current: 'Callable[[], bool] | None' = None) -> 'bool'",
    ),
    "api_chat_slot_generate_title": (True, "(request: 'web.Request') -> 'web.Response'"),
    "apply_manual_title": (
        True,
        "(state: 'DashboardState', slot: 'Any', title: 'str', *, route: 'str', "
        "caller: 'str', source: 'str' = '', origin: 'str' = 'user') -> 'bool'",
    ),
    "api_chat_slot_rename": (True, "(request: 'web.Request') -> 'web.Response'"),
}

#: Names the test trees are known to patch on the facade that a moved function reads. The
#: scan below must find them, so a scanner that stops matching goes red.
_KNOWN_SEAMS = frozenset({"_generate_title_via_kiro", "_persist_title"})

#: Modules that import a moved name through the facade's path; each must keep resolving.
_OLD_PATH_IMPORTERS = (
    "kiro_crew.dashboard.channel_titles",
    "kiro_crew.dashboard.session_control_parts.session_titles",
)


def _owner_functions() -> list[types.FunctionType]:
    return [
        value
        for value in vars(chat_title_explicit).values()
        if isinstance(value, types.FunctionType)
        and value.__code__.co_filename == chat_title_explicit.__file__
    ]


def _global_names(code: types.CodeType):
    for instruction in dis.get_instructions(code):
        if instruction.opname in _GLOBAL_OPS:
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _owner_tree() -> ast.Module:
    return ast.parse(_OWNER_PATH.read_text(encoding="utf-8"))


# ── 1. every owner function runs on the facade's namespace ──────────────────


def test_the_owner_defines_exactly_the_moved_functions() -> None:
    assert sorted(fn.__name__ for fn in _owner_functions()) == sorted(_BASE_SURFACE)


def test_every_owner_function_runs_on_the_facade_globals_as_the_facade_binds_it() -> None:
    wrong = [
        fn.__name__
        for fn in _owner_functions()
        if fn.__globals__ is not vars(chat_title)
        or fn.__module__ != FACADE
        or vars(chat_title).get(fn.__name__) is not fn
    ]
    assert wrong == []


def test_every_global_an_owner_function_reads_is_bound_on_the_facade() -> None:
    missing = {
        fn.__name__: names
        for fn in _owner_functions()
        if (
            names := sorted(
                {
                    n
                    for n in _global_names(fn.__code__)
                    if n not in vars(chat_title) and not hasattr(builtins, n)
                }
            )
        )
    }
    assert missing == {}


def test_every_moved_function_keeps_its_base_kind_and_signature() -> None:
    changed = {}
    for name, (is_coroutine, signature) in _BASE_SURFACE.items():
        fn = vars(chat_title_explicit)[name]
        if inspect.iscoroutinefunction(fn) is not is_coroutine or fn.__closure__ is not None:
            changed[name] = "kind"
        elif str(inspect.signature(fn)) != signature:
            changed[name] = str(inspect.signature(fn))
    assert changed == {}
    # The owner spells the default as a literal; it must stay the facade's constant.
    origin = inspect.signature(chat_title.apply_manual_title).parameters["origin"]
    assert origin.default == chat_title._TITLE_ORIGIN_USER


@pytest.mark.parametrize("module_name", _OLD_PATH_IMPORTERS)
def test_an_old_import_path_binds_the_composed_function(module_name: str) -> None:
    module = __import__(module_name, fromlist=["_"])
    bound = {n for n in _BASE_SURFACE if n in vars(module)}
    assert bound
    assert {n for n in bound if vars(module)[n] is not vars(chat_title)[n]} == set()


# ── 2. a patch of the facade reaches every owner reader ─────────────────────


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    root = source_corpus.repo_root()
    spellings = {FACADE: FACADE}
    found: set[str] = set()
    for path in sorted((root / "test").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "chat_title" not in text:
            continue
        try:
            found |= facade_mirror_scan.patched_names(text, spellings).get(FACADE, set())
        except SyntaxError:
            continue
    return frozenset(found)


def test_the_patch_scan_finds_every_known_seam() -> None:
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


def test_every_name_the_tests_patch_on_the_facade_reaches_every_owner_reader() -> None:
    patched = _corpus_patched_names()
    readers = [fn for fn in _owner_functions() if set(_global_names(fn.__code__)) & patched]
    assert readers, "no owner function reads a patched name -- the sweep went blind"
    assert [fn.__name__ for fn in readers if fn.__globals__ is not vars(chat_title)] == []


def test_a_facade_patch_reaches_the_moved_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    persisted: list[str] = []

    async def fake_persist(state, slot, **_kwargs) -> bool:
        persisted.append(slot.title)
        return False

    class _State:
        def push_slot_title(self, *_args, **_kwargs) -> None:
            pass

        def push_slot_patch(self, *_args, **_kwargs) -> None:
            pass

    slot = types.SimpleNamespace(
        key="k", title="Old", _titled=True, _title_origin="auto", _title_epoch=0
    )
    monkeypatch.setattr(chat_title, "_persist_title", fake_persist)
    monkeypatch.setattr(chat_title, "record_manual_rename", lambda *a, **k: None)
    saved = asyncio.run(
        chat_title.apply_manual_title(_State(), slot, "New", route="test", caller="test")
    )
    assert (saved, persisted, slot._title_origin) == (False, ["New"], "user")


# ── 3. what the owner may hold and import ───────────────────────────────────


def test_the_owner_imports_the_facade_only_for_type_checking() -> None:
    bad = []
    for node in _owner_tree().body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            continue
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("kiro_crew"):
            bad.append(f"{node.lineno}: {node.module}")
        elif isinstance(node, ast.Import) and any(
            a.name.startswith("kiro_crew") for a in node.names
        ):
            bad.append(f"{node.lineno}: import")
    assert bad == []


def test_the_owner_defines_no_module_level_value_and_no_logger() -> None:
    found = []
    for index, node in enumerate(_owner_tree().body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            continue
        found.append(f"{node.lineno}: {type(node).__name__}")
    assert found == []
    assert "logger" not in vars(chat_title_explicit)
    assert chat_title.logger.name == FACADE


def test_nothing_but_the_facade_imports_the_owner() -> None:
    importers = []
    for path in sorted(_SRC.rglob("*.py")):
        if path in (_OWNER_PATH, Path(chat_title.__file__).resolve()):
            continue
        text = path.read_text(encoding="utf-8")
        if "chat_title_explicit" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and (
                "chat_title_explicit" in (node.module or "")
                or any(a.name == "chat_title_explicit" for a in node.names)
            ):
                importers.append(str(path.relative_to(_SRC)))
            elif isinstance(node, ast.Import) and any(
                "chat_title_explicit" in a.name for a in node.names
            ):
                importers.append(str(path.relative_to(_SRC)))
    assert importers == []


def test_the_facade_composes_through_the_shared_helper_once() -> None:
    assert chat_title._facade_compose is facade_compose
    source = Path(chat_title.__file__).read_text(encoding="utf-8")
    assert source.count("\n_facade_compose(") == 1
