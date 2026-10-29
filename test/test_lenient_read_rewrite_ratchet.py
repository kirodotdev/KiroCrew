"""Ratchet: a read that turns a failed open into an empty document must not feed a rewrite.

A LENIENT reader answers ``{}`` / ``[]`` when the file cannot be read::

    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

That is right for a display read and wrong as the base of a whole-file rewrite.
A transient ``EACCES`` or ``EIO`` reads as an empty document, the mutation runs
on it, and the write publishes the emptiness over everything that was on disk.
The write succeeds, so nothing reports the loss.

The strict idiom is a ``*_for_update`` reader where only a
MISSING file reads as empty and any other read error propagates, so the mutation
is abandoned (``mcp_quarantine._load_for_update``,
``config.loader_document.read_config_for_update``,
``aws_control`` ``backup_parts.state._read_state_for_update``,
``ops_mission_control`` ``ledger.read_entries_for_update``). This test stops new
instances of the shape from landing.

What is flagged, found through ``ast``:

* a lenient reader: a function with an ``except`` that catches ``OSError`` (or
  ``IOError``, ``EnvironmentError``, ``PermissionError``) and returns an empty
  ``{}``, ``[]``, ``set()``, ``()``, ``dict()``, ``list()`` or ``tuple()``.
  Catching only ``FileNotFoundError`` is the strict idiom and does not count.
* a rewrite from it: inside one function, a call to a writer (a callee named
  ``write*``, ``save*``, ``persist*``, ``dump`` or their ``_`` / ``atomic_``
  forms) whose arguments use the reader's result, either directly
  (``_write(_read())``) or through a name bound from it
  (``data = _read(); data[k] = v; _write(data)``, ``_write(cursor | new)``).
  A closure sees the names its enclosing function bound, so
  ``state = _read(); def persist(): save(state)`` is reported as ``persist``.

A reader is resolved in its own module (a bare call, or ``self.`` / ``cls.``),
through ``from <module> import <reader>``, or as ``<module>.<reader>()`` for an
imported project module. A call that passes ``strict=True``,
``fail_closed=True``, ``for_update=True`` or ``required=True`` asks the reader
for its strict mode and does not count.

The discriminator is the write, not the read: most lenient readers in ``src/``
are display reads and are fine. ``_KNOWN`` lists the rewrites that already
match, each with what is known about it. The list can only shrink: a fixed site
must be removed from it, and a new one fails until it reads through a strict
reader or is added here with a reason a reviewer can check. A scanner that
silently stops matching reports zero offenders, which reads like success, so it
is also run against planted sources and must still recognise real lenient
readers in ``src/``.
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"

#: Caught by a handler, these mean "a read error other than a missing file".
_READ_ERRORS = frozenset({"OSError", "IOError", "EnvironmentError", "PermissionError"})
#: Keyword flags that ask a dual-mode reader for its strict answer.
_STRICT_FLAGS = frozenset({"strict", "fail_closed", "for_update", "required"})
#: A callee that publishes its argument as file content.
_WRITER = re.compile(r"^_?(?:atomic_)?(?:write|save|persist|dump)(?!s$)")
_EMPTY_CALLS = frozenset({"dict", "list", "set", "tuple"})
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)

_UNAUDITED = "matches the shape; not audited"
_PRESERVES = "an unreadable store is copied aside by _preserve_if_unreadable before the write"

#: ``<path under src>::<qualified function>`` -> what is known about the site.
#: Remove an entry when its site is fixed; ``test_every_known_site_still_matches``
#: fails on a stale one so the list cannot outlive the code.
_KNOWN: dict[str, str] = {
    "kiro_crew/apps/backend_runtime/pidfile.py::_forget_app_pid": _UNAUDITED,
    "kiro_crew/apps/backend_runtime/pidfile.py::_forget_app_pid_if": _UNAUDITED,
    "kiro_crew/apps/backend_runtime/pidfile.py::_record_app_pid": _UNAUDITED,
    "kiro_crew/apps/backend_runtime/stale_reap.py::_reap_stale_app_backends": _UNAUDITED,
    "kiro_crew/apps/builtins/meetings/backend/providers/tasks.py::"
    "LocalTaskProvider._create_locked": _UNAUDITED,
    "kiro_crew/apps/builtins/ops_mission_control/backend/ledger_index.py::import_pending": (
        "union write: an empty base re-checks ids on the next import (see _read_cursor)"
    ),
    "kiro_crew/apps/builtins/pptx_maker/backend/routes.py::_write_deck_root": _UNAUDITED,
    "kiro_crew/apps/builtins/spec_builder/backend/repository.py::_forget_deleted": _UNAUDITED,
    "kiro_crew/apps/dependency_ledger.py::classify_and_clean_for_uninstall": _UNAUDITED,
    "kiro_crew/apps/dependency_ledger.py::record_install": _UNAUDITED,
    "kiro_crew/apps/dependency_ledger.py::record_uninstall": _UNAUDITED,
    "kiro_crew/apps/dev_mode.py::remove_dev_app": _UNAUDITED,
    "kiro_crew/apps/dev_mode.py::set_dev_mode": _UNAUDITED,
    "kiro_crew/autonudge_selfarm.py::forget_self_arm": _UNAUDITED,
    "kiro_crew/autonudge_selfarm.py::record_self_arm": _UNAUDITED,
    "kiro_crew/aws_consent.py::record_grant": _PRESERVES,
    "kiro_crew/aws_consent.py::revoke": _PRESERVES,
    "kiro_crew/builtin_skills/kirocrew-dev/kirocrew-prepare-pr/scripts/push_guard_record.py::"
    "record_built": _UNAUDITED,
    "kiro_crew/connections/mint.py::_forget_mint_spec": _UNAUDITED,
    "kiro_crew/connections/mint.py::_record_mint_spec": _UNAUDITED,
    "kiro_crew/connections/mint.py::_sweep_mint_specs": _UNAUDITED,
    "kiro_crew/dashboard/handlers/mcp_config_entries.py::_replace_kirocrew_spec": _UNAUDITED,
    "kiro_crew/dashboard/handlers/mcp_config_entries.py::_rmw_remove_entry": _UNAUDITED,
    "kiro_crew/dashboard/handlers/mcp_config_entries.py::_set_kirocrew_entry": _UNAUDITED,
    "kiro_crew/dashboard/handlers/mcp_config_entries.py::_set_scope_entry": _UNAUDITED,
    "kiro_crew/dashboard/handlers/mcp_config_entries.py::_set_tool_overrides": _UNAUDITED,
    "kiro_crew/irq.py::run.persist": "load_state documents a lost state as one duplicate wake",
    "kiro_crew/ledger_wake.py::forget_items": "_read_rate documents a lost map as one extra wake",
    "kiro_crew/ledger_wake.py::note_wake": "_read_rate documents a lost map as one extra wake",
    "kiro_crew/mate_tool_access.py::bind_card": _UNAUDITED,
    "kiro_crew/mate_tool_access.py::release_offer": _UNAUDITED,
    "kiro_crew/mate_tool_access.py::reserve_offer": _UNAUDITED,
    "kiro_crew/mate_tool_access.py::settle": _UNAUDITED,
    "kiro_crew/mate_tool_access.py::take_held": _UNAUDITED,
}


def _caught(node: ast.expr | None) -> set[str]:
    if node is None:
        return set()
    if isinstance(node, ast.Tuple):
        return set().union(*(_caught(e) for e in node.elts))
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    return set()


def _is_empty(value: ast.expr) -> bool:
    if isinstance(value, ast.Dict):
        return not value.keys
    if isinstance(value, (ast.List, ast.Set, ast.Tuple)):
        return not value.elts
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        return value.func.id in _EMPTY_CALLS and not value.args and not value.keywords
    return False


class _Function:
    """One function's own nodes (nested functions and classes excluded)."""

    def __init__(self, qual: str, parent: _Function | None) -> None:
        self.qual = qual
        self.parent = parent
        self.nodes: list[ast.AST] = []


def _index(tree: ast.Module) -> list[_Function]:
    """Every function with a qualified name, methods and nested ones included, in one pass."""
    functions: list[_Function] = []
    stack: list[tuple[ast.AST, _Function | None, str]] = [(tree, None, "")]
    while stack:
        node, owner, prefix = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNCS):
                fn = _Function(f"{prefix}{child.name}", owner)
                functions.append(fn)
                stack.append((child, fn, f"{fn.qual}."))
            elif isinstance(child, ast.ClassDef):
                stack.append((child, owner, f"{prefix}{child.name}."))
            else:
                if owner is not None:
                    owner.nodes.append(child)
                stack.append((child, owner, prefix))
    return functions


def is_lenient_reader(fn: _Function) -> bool:
    """True when ``fn`` answers a non-missing read error with an empty document."""
    for node in fn.nodes:
        if isinstance(node, ast.ExceptHandler) and _caught(node.type) & _READ_ERRORS:
            if any(
                isinstance(sub, ast.Return) and sub.value is not None and _is_empty(sub.value)
                for sub in ast.walk(node)
            ):
                return True
    return False


def _callee(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _module_name(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class _Module:
    def __init__(self, rel: str, text: str | None) -> None:
        path = Path(rel)
        self.name = _module_name(path)
        self.rel = rel
        self.text = text
        self.package = self.name if path.name == "__init__.py" else self.name.rpartition(".")[0]
        self.tree: ast.Module | None = None
        self.functions: list[_Function] = []
        self.readers: set[str] = set()

    def parse(self) -> None:
        if self.tree is None and self.text is not None:
            self.tree = ast.parse(self.text, filename=self.rel)
            self.functions = _index(self.tree)
            self.readers = {
                fn.qual.rpartition(".")[2] for fn in self.functions if is_lenient_reader(fn)
            }


def _imports(module: _Module, modules: dict[str, _Module]) -> tuple[dict[str, str], set[str]]:
    """``(alias -> imported project module, names bound to an imported lenient reader)``."""
    aliases: dict[str, str] = {}
    readers: set[str] = set()
    assert module.tree is not None
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # ``import a.b`` binds ``a``, whose calls are ``a.b.f()``: not resolved.
                if alias.name in modules and (alias.asname or "." not in alias.name):
                    aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = module.package.split(".")
                parts = parts[: len(parts) - node.level + 1]
                base = ".".join([*parts, base] if base else parts)
            for alias in node.names:
                local = alias.asname or alias.name
                full = f"{base}.{alias.name}"
                if full in modules:
                    aliases[local] = full
                elif base in modules and alias.name in modules[base].readers:
                    readers.add(local)
    return aliases, readers


def _scan_module(module: _Module, modules: dict[str, _Module]) -> dict[str, list[int]]:
    aliases, imported = _imports(module, modules)

    def is_reader(call: ast.Call) -> bool:
        if any(
            k.arg in _STRICT_FLAGS and isinstance(k.value, ast.Constant) and k.value.value is True
            for k in call.keywords
        ):
            return False
        func = call.func
        if isinstance(func, ast.Name):
            return func.id in module.readers or func.id in imported
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            owner = func.value.id
            if owner in {"self", "cls"}:
                return func.attr in module.readers
            if owner in aliases:
                return func.attr in modules[aliases[owner]].readers
        return False

    found: dict[str, list[int]] = {}
    bound_by: dict[int, set[str]] = {}
    # Parents are indexed before their nested functions, so a closure sees what its
    # enclosing function bound (``state = load(); def persist(): save(state)``).
    for fn in module.functions:
        bound = set(bound_by.get(id(fn.parent), ())) if fn.parent is not None else set()
        for node in fn.nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
                if is_reader(node.value):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    bound |= {
                        sub.id for t in targets for sub in ast.walk(t) if isinstance(sub, ast.Name)
                    }
        bound_by[id(fn)] = bound
        for node in fn.nodes:
            if not (isinstance(node, ast.Call) and _WRITER.match(_callee(node.func))):
                continue
            args = [*node.args, *(k.value for k in node.keywords)]
            if any(
                (isinstance(sub, ast.Name) and sub.id in bound)
                or (isinstance(sub, ast.Call) and is_reader(sub))
                for arg in args
                for sub in ast.walk(arg)
            ):
                found.setdefault(f"{module.rel}::{fn.qual}", []).append(node.lineno)
    return found


#: A lenient reader needs a handler naming a read error and an empty return, so a
#: module whose text lacks either is not parsed for readers.
_HANDLER_GATE = re.compile(
    r"except\b[^:\n]*\b(?:OSError|IOError|EnvironmentError|PermissionError)\b"
)
_EMPTY_GATE = re.compile(r"return\s*(?:\{\s*\}|\[\s*\]|\(\s*\)|(?:dict|list|set|tuple)\(\s*\))")
#: A module whose text holds no writer call cannot rewrite anything.
_WRITER_GATE = re.compile(r"\b_?(?:atomic_)?(?:write|save|persist|dump)\w*\(")


def rewrites_from_lenient_reads(modules: dict[str, _Module]) -> dict[str, list[int]]:
    """``<rel path>::<function>`` -> lines of each write fed by a lenient read.

    Two passes: modules that can define a reader are parsed first, then only the
    modules that both call a writer and name one of the readers found.
    """
    for module in modules.values():
        text = module.text
        if text is not None and _HANDLER_GATE.search(text) and _EMPTY_GATE.search(text):
            module.parse()
    names = sorted({name for module in modules.values() for name in module.readers})
    if not names:
        return {}
    mentions = re.compile(r"\b(?:" + "|".join(map(re.escape, names)) + r")\b")
    found: dict[str, list[int]] = {}
    for module in modules.values():
        text = module.text
        if text is None or not _WRITER_GATE.search(text) or not mentions.search(text):
            continue
        module.parse()
        found.update(_scan_module(module, modules))
    return {key: sorted(lines) for key, lines in found.items()}


def load_modules(root: Path, sources: dict[str, str] | None = None) -> dict[str, _Module]:
    """``root``'s project modules, or the planted ``{rel path: source}`` map. Parsed lazily."""
    if sources is None:
        sources = {}
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root)
            if "__pycache__" in rel.parts or "tests" in rel.parts:
                continue
            sources[rel.as_posix()] = path.read_text(encoding="utf-8")
    modules = [_Module(rel, text) for rel, text in sources.items()]
    return {module.name: module for module in modules}


@functools.cache
def _src_scan() -> tuple[dict[str, _Module], dict[str, list[int]]]:
    """``src/`` is read once per worker for every tree-wide test."""
    modules = load_modules(_SRC)
    return modules, rewrites_from_lenient_reads(modules)


def test_no_new_rewrite_starts_from_a_lenient_read() -> None:
    new = {key: lines for key, lines in _src_scan()[1].items() if key not in _KNOWN}
    assert not new, (
        "these functions rewrite a file from a reader that turns a failed open into an "
        "empty document, so a transient EACCES/EIO would publish the emptiness over "
        "everything on disk. Read the base through a strict reader instead (only a "
        "missing file reads as empty; any other error propagates and the mutation is "
        "abandoned), or, if leniency is genuinely safe here, add the site to _KNOWN "
        "with the reason:\n"
        + "\n".join(f"  {key}: write at lines {lines}" for key, lines in new.items())
    )


def test_every_known_site_still_matches() -> None:
    stale = sorted(set(_KNOWN) - set(_src_scan()[1]))
    assert not stale, (
        "these _KNOWN entries no longer match (fixed, renamed or moved); remove them so "
        "the list only shrinks:\n" + "\n".join(f"  {key}" for key in stale)
    )


def test_the_scan_still_sees_the_real_lenient_readers() -> None:
    modules, _ = _src_scan()
    for module, reader in (
        ("kiro_crew.apps.builtins.ops_mission_control.backend.ledger", "read_entries"),
        ("kiro_crew.aws_consent", "_read_all"),
        ("kiro_crew.mate_tool_access", "_read"),
    ):
        assert reader in modules[module].readers, f"the scan no longer sees {module}.{reader}"
    for module, reader in (
        ("kiro_crew.apps.builtins.ops_mission_control.backend.ledger", "read_entries_for_update"),
        (
            "kiro_crew.apps.builtins.aws_control.backend.backup_parts.state",
            "_read_state_for_update",
        ),
    ):
        assert reader not in modules[module].readers, f"{module}.{reader} is strict"


_LENIENT = """
import json

def _read():
    try:
        return json.loads(open("f").read())
    except (OSError, json.JSONDecodeError):
        return {}

def _read_list():
    try:
        return json.loads(open("f").read())
    except OSError:
        return list()
"""

_STRICT = """
import json

def _read_for_update():
    try:
        return json.loads(open("f").read())
    except FileNotFoundError:
        return {}
"""


def _planted(body: str, extra: dict[str, str] | None = None) -> dict[str, list[int]]:
    sources = {"pkg/__init__.py": "", "pkg/store.py": _LENIENT + _STRICT + body, **(extra or {})}
    return rewrites_from_lenient_reads(load_modules(_SRC, sources))


def test_a_bound_result_passed_to_a_writer_is_flagged() -> None:
    body = "def put(k, v):\n    data = _read()\n    data[k] = v\n    _write(data)\n"
    found = _planted(body)
    assert list(found) == ["pkg/store.py::put"]
    assert len(found["pkg/store.py::put"]) == 1


def test_a_direct_and_a_derived_argument_are_flagged() -> None:
    body = (
        "def direct():\n    atomic_write(path, json.dumps(_read_list()))\n"
        "def union(new):\n    cursor = _read()\n    _write_cursor(cursor | new)\n"
    )
    assert set(_planted(body)) == {"pkg/store.py::direct", "pkg/store.py::union"}


def test_a_method_reader_and_a_nested_function_are_named_by_qualname() -> None:
    body = (
        "class Store:\n"
        "    def _read(self):\n"
        "        try:\n            return {}\n        except PermissionError:\n            return []\n"
        "    def add(self, x):\n"
        "        def inner():\n"
        "            rows = self._read()\n            save_rows(rows + [x])\n"
        "        inner()\n"
        "def run():\n"
        "    state = _read()\n"
        "    def persist():\n        _save_state(state)\n"
        "    persist()\n"
    )
    assert set(_planted(body)) == {"pkg/store.py::Store.add.inner", "pkg/store.py::run.persist"}


def test_a_reader_imported_from_another_module_is_resolved() -> None:
    # The resolve_conflict shape: the lenient read lives in one module, the
    # rewrite in the module that imports it.
    caller = (
        "from pkg import store\n"
        "from pkg.store import _read as read_doc\n"
        "def resolve_conflict():\n    entries = store._read()\n    store._write_all(entries)\n"
        "def other():\n    _save(read_doc())\n"
    )
    found = _planted("", {"pkg/sync.py": caller})
    assert set(found) == {"pkg/sync.py::resolve_conflict", "pkg/sync.py::other"}


def test_strict_reads_and_display_reads_are_not_flagged() -> None:
    body = (
        "def strict(k):\n    data = _read_for_update()\n    data[k] = 1\n    _write(data)\n"
        "def strict_mode(k):\n    data = _read(strict=True)\n    _write(data)\n"
        "def display():\n    return render(_read())\n"
        "def unrelated():\n    data = _read()\n    _write(compute())\n"
        "def text():\n    log(json.dumps(_read()))\n"
    )
    assert _planted(body) == {}


def test_a_handler_that_reraises_or_catches_only_parse_errors_is_not_lenient() -> None:
    sources = """
def a():
    try:
        return load()
    except OSError:
        raise
def b():
    try:
        return load()
    except ValueError:
        return {}
def c():
    try:
        return load()
    except OSError:
        return {"default": 1}
"""
    assert [fn.qual for fn in _index(ast.parse(sources)) if is_lenient_reader(fn)] == []
