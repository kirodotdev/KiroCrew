"""``GatewayOrchestrator`` is split across its ``gateway_runtime`` owners by binding.

Each method an owner holds is a function taking ``self`` that the class body binds
under its own name, so the class object every caller, ``patch.object`` and
``__new__`` fixture holds is the one-module class; ``_facade_compose.compose`` runs
those functions on the facade's globals. These tests pin what makes that safe:

* a bound function needs nothing only a class body provides (zero-argument
  ``super()``, ``__class__``, a name-mangled attribute);
* every bound owner member is inventoried, with its kind and signature, in the
  composition contract's ``_BASE_SURFACE``, and a class constant an owner defines is
  the owner's own object;
* every name a test patches on ``kiro_crew.slack.gateway`` is a facade global some
  facade or owner function reads at call time, so the patch reaches the code;
* every name the facade imports is read by a facade or owner function, or is part of
  its frozen public surface: the property flake8's F401 stands in for elsewhere,
  which ``setup.cfg`` turns off for this facade because owners read its imports;
* an owner imports the modules ``check_sync_io_in_async`` gates (``time``, ``os``,
  ``subprocess``...) by name, so a blocking call planted in its coroutines is still
  reported.
"""

from __future__ import annotations

import ast
import dis
import importlib
import importlib.util
import pkgutil
import re
import types
from pathlib import Path

import pytest
from test_campaign_facades_base_public_names import _BASE_PUBLIC_NAMES
from test_slack_gateway_composition_contract import _BASE_SURFACE

import kiro_crew.slack.gateway as gateway
from kiro_crew.slack import gateway_runtime

_FACADE_PATH = Path(gateway.__file__).resolve()
_RUNTIME_DIR = Path(gateway_runtime.__file__).resolve().parent
_TEST_DIR = Path(__file__).resolve().parent
_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "LOAD_NAME"})

#: Class constants an owner defines and the class binds, with that owner.
_BOUND_CLASS_CONSTANTS = {
    "_DEP_INSTALL_TIMEOUT_SECS": "install_repair",
    "_KIRO_CLI_VERSION_TIMEOUT_SECS": "install_repair",
    "_MANDATORY_UPDATE_MAX_DEFER_SECS": "update_schedule",
    "_SLACK_APPLIER_PREFIXES": "channel_boot",
    "_STARTUP_CHILD_REAP_SECS": "install_repair",
    "_UPDATE_BUSY_RETRY_SECS": "update_schedule",
    "_UPDATE_DRAIN_TIMEOUT_SECS": "update_schedule",
}

#: Names a test patches on the facade that no function reads from its globals, each
#: with the reason the patch is still sound.
_PATCHED_WITHOUT_A_GLOBAL_READ = {
    # Imported inside the update paths that use it, never bound on the facade: the
    # one patch is ``create=True`` and asserts the method-local import wins.
    "respawn_executable": "method-local import; the patch creates the name",
}


def _owners() -> list[types.ModuleType]:
    return [
        importlib.import_module(f"{gateway_runtime.__name__}.{info.name}")
        for info in pkgutil.iter_modules([str(_RUNTIME_DIR)])
    ]


def _bound_owner_members() -> dict[str, types.FunctionType]:
    """Class attributes whose function an owner's file defines, by attribute name."""
    owner_files = {owner.__file__ for owner in _owners()}
    found: dict[str, types.FunctionType] = {}
    for name, member in vars(gateway.GatewayOrchestrator).items():
        fn = getattr(member, "__func__", member)
        if isinstance(fn, types.FunctionType) and fn.__code__.co_filename in owner_files:
            found[name] = fn
    return found


# ── a bound function needs no class body ─────────────────────────────────────


def _class_body_needs(tree: ast.AST) -> list[str]:
    """What in *tree* only works inside a class body: zero-argument ``super()``,
    ``__class__``, or a name-mangled ``__attr``."""
    needs: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "super"
            and not node.args
        ):
            needs.append(f"{node.lineno}: super()")
        elif isinstance(node, ast.Name) and node.id == "__class__":
            needs.append(f"{node.lineno}: __class__")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and not node.attr.endswith("__")
        ):
            needs.append(f"{node.lineno}: {node.attr}")
    return needs


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("def f(self):\n    return super().g()\n", True),
        ("def f(self):\n    return __class__\n", True),
        ("def f(self):\n    return self.__secret\n", True),
        ("def f(self):\n    return super(type(self), self).g()\n", False),
        ("def f(self):\n    return self.__dict__, self._private\n", False),
    ],
)
def test_the_class_body_check_sees_each_shape(source: str, flagged: bool) -> None:
    assert bool(_class_body_needs(ast.parse(source))) is flagged


def test_no_bound_owner_function_needs_a_class_body() -> None:
    """Checked on every top-level function an owner defines (the bindable ones); a
    class an owner defines keeps its own body, so its methods may use ``super()``."""
    functions = [
        (path.name, node)
        for path in sorted(_RUNTIME_DIR.glob("*.py"))
        if path.name != "__init__.py"
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert len(functions) >= 50
    offenders = [f"{name}:{need}" for name, node in functions for need in _class_body_needs(node)]
    assert offenders == []


# ── the bound members are inventoried ────────────────────────────────────────


def test_every_bound_owner_member_has_a_base_surface_row() -> None:
    """A method an owner gains is inventoried with the kind and signature it had."""
    inventoried = {name for rows in _BASE_SURFACE.values() for name, _, _ in rows}
    bound = _bound_owner_members()
    assert len(bound) >= 50, sorted(bound)
    assert sorted(set(bound) - inventoried) == []


@pytest.mark.parametrize(("name", "owner"), sorted(_BOUND_CLASS_CONSTANTS.items()))
def test_a_bound_class_constant_is_its_owners_object(name: str, owner: str) -> None:
    module = importlib.import_module(f"{gateway_runtime.__name__}.{owner}")
    assert vars(gateway.GatewayOrchestrator)[name] is vars(module)[name]


# ── a patch of the facade reaches the code ───────────────────────────────────

_FACADE_NAME = gateway.__name__
_PATCH_STRING = re.compile(r"""["']kiro_crew\.slack\.gateway\.([A-Za-z_]\w*)""")


def _constant(node: ast.AST | None) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _is_facade_lookup(node: ast.AST) -> bool:
    """``import_module("kiro_crew.slack.gateway")`` or ``sys.modules["kiro_crew.slack.gateway"]``."""
    if isinstance(node, ast.Call) and node.args and _constant(node.args[0]) == _FACADE_NAME:
        func = node.func
        return (func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")) == (
            "import_module"
        )
    if isinstance(node, ast.Subscript) and _constant(node.slice) == _FACADE_NAME:
        return ast.unparse(node.value).split(".")[-1] == "modules"
    return False


def _facade_aliases(tree: ast.AST) -> set[str]:
    """Every local name a file binds to the facade module."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(a.asname for a in node.names if a.name == _FACADE_NAME and a.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.slack":
            aliases.update(a.asname or a.name for a in node.names if a.name == "gateway")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            if _is_facade_lookup(node.value):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                aliases.update(t.id for t in targets if isinstance(t, ast.Name))
    return aliases


def _patched_names(text: str) -> set[str]:
    """Facade attribute names *text* patches: the dotted string form, and the object
    forms ``setattr``/``patch.object``/``delattr``/``patch.multiple`` on any binding of
    the facade module (an import alias, ``import_module`` or ``sys.modules``)."""
    names = set(_PATCH_STRING.findall(text))
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return names
    aliases = _facade_aliases(tree)

    def is_facade(node: ast.AST) -> bool:
        return (
            (isinstance(node, ast.Name) and node.id in aliases)
            or ast.unparse(node) == _FACADE_NAME
            or _constant(node) == _FACADE_NAME
            or _is_facade_lookup(node)
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args or not is_facade(node.args[0]):
            continue
        func = node.func
        verb = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if verb in ("setattr", "object", "delattr") and len(node.args) >= 2:
            name = _constant(node.args[1])
            if isinstance(name, str):
                names.add(name)
        elif verb == "multiple":
            names.update(k.arg for k in node.keywords if k.arg)
    return names


def _test_patched_names() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(_TEST_DIR.rglob("*.py")):
        if path.resolve() == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "kiro_crew.slack" not in text:
            continue
        for name in _patched_names(text):
            found.setdefault(name, set()).add(path.relative_to(_TEST_DIR).as_posix())
    return found


def _global_reads(code: types.CodeType):
    for instruction in dis.get_instructions(code):
        if instruction.opname in _GLOBAL_OPS:
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_reads(constant)


def _functions_reading_facade_globals():
    """Every function that runs on the facade's globals: facade module functions, the
    methods of the classes it binds (owner functions included), unwrapped."""
    for value in list(vars(gateway).values()):
        members = [value]
        if isinstance(value, type) and value.__module__.startswith(gateway.__name__):
            members = list(vars(value).values())
        for member in members:
            fn = getattr(member, "__func__", member)
            if isinstance(member, property):
                fn = member.fget
            fn = getattr(fn, "__wrapped__", fn)
            if isinstance(fn, types.FunctionType) and fn.__globals__ is vars(gateway):
                yield fn


def _facade_global_reads() -> set[str]:
    return {
        name for fn in _functions_reading_facade_globals() for name in _global_reads(fn.__code__)
    }


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import kiro_crew.slack.gateway as gw\n"
            'patch("kiro_crew.slack.gateway.stream_and_collect")\n'
            'monkeypatch.setattr(gw, "sel", fake)\n'
            'patch.object(gw, "CronService")\n',
            {"stream_and_collect", "sel", "CronService"},
        ),
        (
            "from kiro_crew.slack import events, gateway\n"
            'monkeypatch.setattr(gateway, "sel", fake)\n',
            {"sel"},
        ),
        (
            'from kiro_crew.slack import gateway as g\nmonkeypatch.delattr(g, "logger")\n',
            {"logger"},
        ),
        (
            "import kiro_crew.slack.gateway\n"
            'monkeypatch.setattr(kiro_crew.slack.gateway, "time", fake)\n',
            {"time"},
        ),
        (
            'gw = importlib.import_module("kiro_crew.slack.gateway")\n'
            'mock.patch.object(gw, "sel")\n',
            {"sel"},
        ),
        ('gw = sys.modules["kiro_crew.slack.gateway"]\nsetattr(gw, "sel", fake)\n', {"sel"}),
        (
            "import kiro_crew.slack.gateway as gw\n"
            "patch.multiple(gw, sel=fake, CronService=fake)\n"
            'patch.multiple("kiro_crew.slack.gateway", data_home=fake)\n',
            {"sel", "CronService", "data_home"},
        ),
        # Another module's patch is not the facade's.
        ('from kiro_crew.slack import events\nmonkeypatch.setattr(events, "sel", fake)\n', set()),
    ],
    ids=[
        "dotted-and-alias",
        "multi-name-from",
        "from-as",
        "plain-import",
        "import-module",
        "sys-modules",
        "patch-multiple",
        "other-module",
    ],
)
def test_the_patch_scan_finds_every_spelling(source: str, expected: set[str]) -> None:
    assert _patched_names(source) == expected


def test_the_reach_check_fails_a_name_no_function_reads() -> None:
    reads = _facade_global_reads()
    assert "stream_and_collect" in reads
    assert "_a_name_no_gateway_function_reads" not in reads


def test_every_name_a_test_patches_on_the_facade_reaches_the_code() -> None:
    patched = _test_patched_names()
    assert len(patched) >= 100, sorted(patched)
    reads = _facade_global_reads()
    unreached = sorted(
        f"{name} ({', '.join(sorted(files)[:3])})"
        for name, files in patched.items()
        if name not in _PATCHED_WITHOUT_A_GLOBAL_READ
        and (name not in vars(gateway) or name not in reads)
    )
    assert unreached == []


# ── every facade import is read ──────────────────────────────────────────────


def _imported_names(tree: ast.Module) -> dict[str, int]:
    """Names the facade's import statements bind (``TYPE_CHECKING`` ones included)."""
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*":
                    found[(alias.asname or alias.name).split(".")[0]] = node.lineno
    return found


def _annotation_names(text: str) -> set[str]:
    """Names inside string annotations, which the AST holds as constants."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        annotations = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            every = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            annotations = [a.annotation for a in every if a is not None and a.annotation]
            annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations = [node.annotation]
        for annotation in annotations:
            for sub in ast.walk(annotation) if annotation is not None else ():
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    try:
                        parsed = ast.parse(sub.value, mode="eval")
                    except SyntaxError:
                        continue
                    names |= {n.id for n in ast.walk(parsed) if isinstance(n, ast.Name)}
    return names


def _names_read(text: str) -> set[str]:
    """Every name *text* reads outside its import statements."""
    tree = ast.parse(text)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {
        n.value.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
    }
    return names | _annotation_names(text)


def _unread_imports(facade_text: str, owner_texts: list[str], surface: frozenset[str]) -> list[str]:
    read = set().union(_names_read(facade_text), *(_names_read(t) for t in owner_texts))
    imported = _imported_names(ast.parse(facade_text))
    return sorted(
        f"{name} (line {line})"
        for name, line in imported.items()
        if name not in read and name not in surface and name != "annotations"
    )


def test_the_import_check_fails_an_unread_import() -> None:
    facade = "import json\nimport os\nfrom kiro_crew import x as y\n\n\ndef f():\n    return os\n"
    owner = "def g(self):\n    return y\n"
    assert _unread_imports(facade, [owner], frozenset()) == ["json (line 1)"]
    assert _unread_imports(facade, [owner], frozenset({"json"})) == []


def test_every_facade_import_is_read_by_a_facade_or_owner_function() -> None:
    owner_texts = [
        path.read_text(encoding="utf-8")
        for path in sorted(_RUNTIME_DIR.glob("*.py"))
        if path.name != "__init__.py"
    ]
    surface = _BASE_PUBLIC_NAMES[gateway.__name__]
    assert _unread_imports(_FACADE_PATH.read_text(encoding="utf-8"), owner_texts, surface) == []


# ── the sync-io gate reads every owner coroutine ─────────────────────────────

#: Owners composed before the binding split that take a gated module from the facade
#: rather than importing it, so ``check_sync_io_in_async`` cannot resolve those calls.
#: Shrink-only: converting an owner removes its row.
_FACADE_GATED_MODULE_READS = {
    "cron_dispatch": {"time"},
    "cron_verdict": {"time"},
    "delivery": {"time"},
}


def _sync_io_gate() -> types.ModuleType:
    """``scripts/check_sync_io_in_async.py``, loaded by path (``scripts/`` is no package)."""
    path = _TEST_DIR.parent / "scripts" / "check_sync_io_in_async.py"
    spec = importlib.util.spec_from_file_location("_gateway_sync_io_gate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _facade_imported(tree: ast.AST) -> set[str]:
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == gateway.__name__
        for alias in node.names
    }


def test_no_owner_takes_a_gated_module_from_the_facade() -> None:
    """The gate resolves ``time``, ``os``, ``subprocess``... only from an ``import`` in
    the same file. An owner spells them as ``import <module>`` under TYPE_CHECKING; one
    that took the facade's binding instead would hide its coroutines from the gate."""
    gated = set(_sync_io_gate().MODULE_FUNCS)
    assert {"time", "subprocess", "os"} <= gated
    taken = {
        path.stem: found
        for path in sorted(_RUNTIME_DIR.glob("*.py"))
        if (found := _facade_imported(ast.parse(path.read_text(encoding="utf-8"))) & gated)
    }
    assert taken == _FACADE_GATED_MODULE_READS


def _plant(source: str, statement: str) -> str:
    """*source* with *statement* as the first line of its first top-level coroutine."""
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef))
    first = fn.body[0]
    if isinstance(first, ast.Expr) and isinstance(_constant(first.value), str) and fn.body[1:]:
        first = fn.body[1]  # after the docstring
    lines = source.splitlines(keepends=True)
    indent = " " * first.col_offset
    lines.insert(first.lineno - 1, f"{indent}{statement}\n")
    return "".join(lines)


def _owners_importing(module: str) -> list[str]:
    owners = []
    for path in sorted(_RUNTIME_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        plain = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        if module in plain and any(isinstance(n, ast.AsyncFunctionDef) for n in tree.body):
            owners.append(path.stem)
    return owners


#: One blocking call per gated module an owner may import, and the family it is.
_PLANTS = {
    "time": ("time.sleep(1)", "sleep"),
    "os": ('os.system("true")', "subprocess"),
    "subprocess": ('subprocess.run(["true"])', "subprocess"),
    "socket": ('socket.create_connection(("localhost", 1))', "http"),
}


@pytest.mark.parametrize(
    ("owner", "module"),
    [(owner, module) for module in sorted(_PLANTS) for owner in _owners_importing(module)],
)
def test_the_sync_io_gate_flags_a_call_planted_in_an_owner_coroutine(
    owner: str, module: str
) -> None:
    gate = _sync_io_gate()
    statement, family = _PLANTS[module]
    source = (_RUNTIME_DIR / f"{owner}.py").read_text(encoding="utf-8")
    planted = _plant(source, statement)
    assert family in {found for _line, found, _why in gate._violations_in_source(planted)}


def test_the_planted_sleep_is_seen_only_through_the_plain_import() -> None:
    """Non-vacuity: the same plant, with ``time`` taken from the facade instead, is
    invisible to the gate, so the test above proves the import spelling matters."""
    gate = _sync_io_gate()
    owner = _owners_importing("time")[0]
    source = (_RUNTIME_DIR / f"{owner}.py").read_text(encoding="utf-8")
    assert "    import time\n" in source
    hidden = source.replace("    import time\n", "    from kiro_crew.slack.gateway import time\n")
    planted = _plant(hidden, "time.sleep(1)")
    assert "sleep" not in {family for _line, family, _why in gate._violations_in_source(planted)}
