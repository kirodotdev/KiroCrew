"""The harness vocabulary is one import path over a family of owner modules.

``kiro_crew.agent_sdk.backends`` defines the backend identifiers and the selectable
registry and re-exports every other vocabulary name from the ``backends_*`` owner that
defines it. Six properties keep that shape honest, each pinned here:

1. **The re-export is whole and is the owner's object.** Every name an owner defines is
   bound on the facade as that same object, so ``vars(backends)`` -- which the
   disposition-row and card tests enumerate -- still sees every set.
2. **The owners are leaves that read only what the facade binds first.** An owner's
   module-scope imports are the standard library and the identifiers the facade defines
   above its owner imports; anything else either re-enters ``KiroCrewConfig.load()``
   or meets a name the facade has not bound yet.
3. **The facade is the only way in.** No product module imports an owner directly, so a
   consumer never depends on which owner holds a name.
4. **A patch on the facade still reaches the code it reached.** Every name the test
   corpus rebinds on the facade is read only by facade code, never by an owner
   function, so a ``monkeypatch.setattr(backends, ...)`` keeps intercepting.
5. **Enum and record classes keep the facade as their module**, the path every caller
   imports them from, so a pickle or a log line names that path.
6. **A review rule that covers the facade covers every owner**, so moving a set into an
   owner never takes it out of the ``harness-parity`` rule's sight.
"""

from __future__ import annotations

import ast
import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.agent_sdk import backends
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# One group per tree-scanning file: the patch-reach guard below reads the whole test tree.
pytestmark = pytest.mark.xdist_group(name="tree_scan_backends_vocabulary_family")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "kiro_crew"
FACADE = Path(backends.__file__)
FACADE_NAME = backends.__name__
OWNER_PREFIX = FACADE_NAME + "_"


def _owner_names(facade_tree: ast.Module) -> list[str]:
    """The owner modules the facade re-exports from, in the order it imports them."""
    return [
        node.module
        for node in facade_tree.body
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(OWNER_PREFIX)
    ]


FACADE_TREE = ast.parse(FACADE.read_text(encoding="utf-8"))
OWNERS = _owner_names(FACADE_TREE)


def _defined_at_module_scope(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _bound_before_owners(facade_tree: ast.Module) -> set[str]:
    """What the facade binds above its first owner import -- all an owner may import."""
    names: set[str] = set()
    for node in facade_tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(OWNER_PREFIX):
            break
        names |= _defined_at_module_scope(ast.Module(body=[node], type_ignores=[]))
    return names


def _owner_import_problems(source: str, allowed_from_facade: set[str]) -> list[str]:
    """Why an owner's module-scope imports break the leaf or the import order."""
    problems = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in sys.stdlib_module_names:
                    problems.append(f"imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                problems.append(f"imports relatively from {'.' * node.level}{module}")
                continue
            if module == "__future__":
                continue
            if module == FACADE_NAME:
                late = sorted(a.name for a in node.names if a.name not in allowed_from_facade)
                if late:
                    problems.append(f"imports {late} from the facade before it binds them")
            elif module.split(".")[0] not in sys.stdlib_module_names:
                problems.append(f"imports from {module}")
    return problems


def _owner_tree(name: str) -> ast.Module:
    return ast.parse(Path(importlib.import_module(name).__file__).read_text(encoding="utf-8"))


# ── 1. the re-export is whole ───────────────────────────────────────────────


def test_the_facade_re_exports_from_owners() -> None:
    assert len(OWNERS) == len(set(OWNERS)) >= 2, OWNERS


@pytest.mark.parametrize("owner", OWNERS)
def test_every_name_an_owner_defines_is_the_same_object_on_the_facade(owner: str) -> None:
    module = importlib.import_module(owner)
    defined = _defined_at_module_scope(_owner_tree(owner)) - {"logger"}
    assert defined
    missing = sorted(n for n in defined if n not in vars(backends))
    assert not missing, f"{owner} defines names the facade does not bind: {missing}"
    copies = sorted(n for n in defined if vars(backends)[n] is not vars(module)[n])
    assert not copies, f"the facade binds a different object than {owner} for: {copies}"


def test_no_name_is_defined_twice_across_the_family() -> None:
    seen = {name: FACADE_NAME for name in _defined_at_module_scope(FACADE_TREE)}
    clashes = []
    for owner in OWNERS:
        for name in _defined_at_module_scope(_owner_tree(owner)) - {"logger"}:
            if name in seen:
                clashes.append((name, seen[name], owner))
            seen[name] = owner
    assert not clashes


def test_every_backend_identifier_is_defined_in_the_facade() -> None:
    """Owners define sets and tables; an id lives beside ``ACP_BACKENDS_KNOWN``."""
    ids = re.compile(r"ACP_BACKEND_[A-Z]+\Z")
    for owner in OWNERS:
        defined = sorted(n for n in _defined_at_module_scope(_owner_tree(owner)) if ids.match(n))
        tables = {n for n in defined if not isinstance(vars(backends)[n], str)}
        assert sorted(set(defined) - tables) == [], owner
    assert {"ACP_BACKEND_KIRO", "ACP_BACKEND_KAS"} <= _defined_at_module_scope(FACADE_TREE)


# ── 2. the owners are leaves that read only what the facade binds first ────


@pytest.mark.parametrize("owner", OWNERS)
def test_an_owner_imports_only_stdlib_and_the_facade_identifiers(owner: str) -> None:
    source = Path(importlib.import_module(owner).__file__).read_text(encoding="utf-8")
    assert _owner_import_problems(source, _bound_before_owners(FACADE_TREE)) == []


def test_the_owner_import_check_can_fail() -> None:
    early = _bound_before_owners(FACADE_TREE)
    assert "ACP_BACKEND_KIRO" in early and "ACP_BACKENDS_STEER" not in early
    planted = {
        "from kiro_crew.acp.types import CC_PERMISSION_MODE_AUTO\n": "kiro_crew.acp.types",
        "import kiro_crew.config\n": "kiro_crew.config",
        f"from {FACADE_NAME} import ACP_BACKENDS_STEER\n": "ACP_BACKENDS_STEER",
        "from ..config import loader\n": "..config",
        "from .backends import ACP_BACKEND_KIRO\n": ".backends",
    }
    for source, word in planted.items():
        problems = _owner_import_problems(source, early)
        assert problems and word in problems[0], source
    clean = f"import os\nfrom typing import Any\nfrom {FACADE_NAME} import ACP_BACKEND_KIRO\n"
    assert _owner_import_problems(clean, early) == []


def test_an_owner_imported_first_in_a_fresh_interpreter_loads_the_whole_family() -> None:
    """Importing an owner before the facade still binds the facade completely.

    The package init reaches the facade before any owner body runs, and the facade
    imports every owner at its foot; this is the order the owners' top-of-file import
    of the identifiers relies on. Run in a child, because this process has imported the
    family long ago.
    """
    last = OWNERS[-1]
    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(SRC.parent)!r})\n"
        f"import {last}\n"
        f"facade = sys.modules[{FACADE_NAME!r}]\n"
        "print(sorted(n for n in sys.modules if n.startswith(facade.__name__ + '_')))\n"
        "print('acp' if 'kiro_crew.acp' in sys.modules else 'leaf')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        cwd=str(ROOT),
        timeout=120,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    loaded, leaf = result.stdout.strip().splitlines()[-2:]
    assert ast.literal_eval(loaded) == sorted(OWNERS)
    assert leaf == "leaf"


# ── 3. the facade is the only way in ─────────────────────────────────────


def _absolute(node: ast.ImportFrom, package: str) -> str:
    """The module an ``ImportFrom`` names, with a relative one resolved against *package*."""
    if not node.level:
        return node.module or ""
    base = package.split(".")[: len(package.split(".")) - node.level + 1]
    return ".".join([*base, node.module] if node.module else base)


def _direct_owner_imports(source: str, package: str = "kiro_crew") -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            module = _absolute(node, package)
            if module.startswith(OWNER_PREFIX):
                found.append(module)
            elif module == "kiro_crew.agent_sdk":
                found += [a.name for a in node.names if a.name.startswith("backends_")]
        elif isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.startswith(OWNER_PREFIX)]
    return found


def test_no_product_module_but_the_facade_imports_an_owner() -> None:
    family = {FACADE} | {Path(importlib.import_module(o).__file__) for o in OWNERS}
    offenders = {}
    for path in sorted(SRC.rglob("*.py")):
        if path in family or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "backends_" not in text:
            continue
        package = ".".join(path.relative_to(SRC.parent).with_suffix("").parts[:-1])
        if path.name == "__init__.py":
            package = ".".join(path.relative_to(SRC.parent).parts[:-1])
        hits = _direct_owner_imports(text, package)
        if hits:
            offenders[path.relative_to(SRC).as_posix()] = hits
    assert offenders == {}, "import vocabulary names from kiro_crew.agent_sdk.backends"


def test_the_direct_import_check_can_fail() -> None:
    owner = OWNERS[0]
    assert _direct_owner_imports(f"from {owner} import X\n") == [owner]
    assert _direct_owner_imports(f"import {owner}\n") == [owner]
    short = owner.rsplit(".", 1)[1]
    assert _direct_owner_imports(f"from kiro_crew.agent_sdk import {short}\n") == [short]
    assert _direct_owner_imports(f"from {FACADE_NAME} import X\n") == []
    sibling = "kiro_crew.agent_sdk"
    assert _direct_owner_imports(f"from .{short} import X\n", sibling) == [owner]
    assert _direct_owner_imports(f"from . import {short}\n", sibling) == [short]
    assert _direct_owner_imports(f"from ..{short} import X\n", f"{sibling}.drivers") == [owner]
    assert _direct_owner_imports("from .backends import X\n", sibling) == []


# ── 4. a patch on the facade still reaches the code it reached ──────────────

_PATCH_CALLS = ("setattr", "delattr", "patch.object")


def _facade_aliases(tree: ast.Module) -> set[str]:
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == FACADE_NAME and alias.asname:
                    aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.agent_sdk":
            aliases |= {a.asname or a.name for a in node.names if a.name == "backends"}
    return aliases


def _names_patched_on_the_facade(source: str) -> set[str]:
    """Attribute names a test module rebinds on the facade module object."""
    tree = ast.parse(source)
    aliases = _facade_aliases(tree)
    names = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        func = ast.unparse(node.func)
        target = node.args[0]
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            if target.value.startswith(FACADE_NAME + ".") and func.endswith(
                _PATCH_CALLS + ("patch",)
            ):
                names.add(target.value[len(FACADE_NAME) + 1 :])
            continue
        if not func.endswith(_PATCH_CALLS) or len(node.args) < 2:
            continue
        if ast.unparse(target) in aliases | {FACADE_NAME}:
            attr = node.args[1]
            if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                names.add(attr.value)
    return names


def _globals_read_by_functions(tree: ast.Module) -> set[str]:
    reads = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local = {a.arg for a in ast.walk(node.args) if isinstance(a, ast.arg)}
            local |= {
                n.id
                for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
            }
            reads |= {
                n.id
                for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in local
            }
    return reads


def _patched_names_in_the_corpus() -> set[str]:
    names: set[str] = set()
    for path in sorted((ROOT / "test").rglob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if "agent_sdk" in text and "backends" in text:
            names |= _names_patched_on_the_facade(text)
    return names


def test_no_owner_function_reads_a_name_the_corpus_patches_on_the_facade() -> None:
    patched = _patched_names_in_the_corpus()
    assert "_selectable" in patched, "the corpus scan found none of the known facade patches"
    for owner in OWNERS:
        crossing = sorted(_globals_read_by_functions(_owner_tree(owner)) & patched)
        assert not crossing, (
            f"{owner} reads {crossing}, which tests rebind on the facade; a patch there "
            "would no longer reach this code"
        )


def test_the_patch_reach_check_can_fail() -> None:
    test_source = (
        "from kiro_crew.agent_sdk import backends as vocab\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(vocab, 'selectable_backends', lambda: set())\n"
        f"    monkeypatch.setattr('{FACADE_NAME}._baseline', set())\n"
    )
    assert _names_patched_on_the_facade(test_source) == {"selectable_backends", "_baseline"}
    owner_source = "def f(backend):\n    return backend in selectable_backends()\n"
    assert _globals_read_by_functions(ast.parse(owner_source)) & {"selectable_backends"}
    shadowed = "def f(selectable_backends):\n    return selectable_backends()\n"
    assert not _globals_read_by_functions(ast.parse(shadowed)) & {"selectable_backends"}


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    this = Path(__file__).resolve().relative_to(ROOT).as_posix()
    assert this in leaf_test_scope.corpus_gates(ROOT)


# ── 5. enum and record classes keep the facade as their module ─────────────


@pytest.mark.parametrize("name", ["Routing", "Readback", "SelfServedLaunch"])
def test_a_moved_class_keeps_the_facade_module(name: str) -> None:
    cls = getattr(backends, name)
    assert cls.__module__ == FACADE_NAME
    homes = [o for o in OWNERS if name in vars(importlib.import_module(o))]
    assert len(homes) == 1 and getattr(importlib.import_module(homes[0]), name) is cls


def test_each_moved_class_resolves_by_the_module_and_name_it_reports() -> None:
    """``module.qualname`` names the class itself: what a pickle or a log line looks up."""
    for cls in (backends.Routing, backends.Readback, backends.SelfServedLaunch):
        assert getattr(importlib.import_module(cls.__module__), cls.__qualname__) is cls


# ── review rules that cover the facade cover its owners ──────────────────


def _globstar(pattern: str) -> re.Pattern[str]:
    """An AUTOSDE pattern as a regex: ``**/`` spans zero or more directories, ``*``
    and ``?`` stay inside one path segment."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:[^/]+/)*", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] in "*?":
            out, i = out + ("[^/]*" if pattern[i] == "*" else "[^/]"), i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def _rules_missing_owners(rules: list[dict]) -> tuple[set[str], dict[str, list[str]]]:
    """``(ids of rules matching the facade, {id: owner files those rules miss})``."""
    facade_path = FACADE.resolve().relative_to(ROOT).as_posix()
    owners = [
        Path(importlib.import_module(o).__file__).resolve().relative_to(ROOT).as_posix()
        for o in OWNERS
    ]
    matched: set[str] = set()
    missing: dict[str, list[str]] = {}
    for rule in rules:
        patterns = [_globstar(p) for p in rule.get("file-patterns", [])]
        if not any(p.match(facade_path) for p in patterns):
            continue
        matched.add(rule["id"])
        gaps = [o for o in owners if not any(p.match(o) for p in patterns)]
        if gaps:
            missing[rule["id"]] = gaps
    return matched, missing


def test_every_review_rule_on_the_facade_also_covers_its_owners() -> None:
    """A rule that reviews the vocabulary module reviews the sets its owners define: an
    owner outside its patterns would take those definitions out of that rule's sight."""
    import yaml

    rules = [
        rule
        for name in ("AUTOSDE.yaml", "website/AUTOSDE.yaml")
        for rule in yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))["custom-rules"]
    ]
    matched, missing = _rules_missing_owners(rules)
    assert "harness-parity" in matched
    assert missing == {}
    narrowed = [
        (
            {
                **rule,
                "file-patterns": [p for p in rule["file-patterns"] if "backends_" not in p],
            }
            if rule["id"] == "harness-parity"
            else rule
        )
        for rule in rules
    ]
    assert _rules_missing_owners(narrowed)[1].keys() == {"harness-parity"}
