"""The entry-type registry keeps its content, its order and its one import path.

``kiro_crew.crew_log.entry_types`` composes the session table from five family owners
(``entry_types_lifecycle``, ``entry_types_exchange``, ``entry_types_delegated``,
``entry_types_ledgers``, ``entry_types_surfaces``) written over one leaf,
``entry_types_fields``, and stays the module callers, tests and documents import from.

The registry is a schema. The first tests here hold the session table to its owners'
``TYPES`` in owner order, each kind's table to the types its reference page under
``docs/reference/crew-log/`` documents, every lookup to the table's own declaration,
and the rendered reference to one section per declaration in table order. What each
declaration states (fields, required flags, JSON types, enums) is the persisted wire
schema, pinned per type in ``_SCHEMA_DIGESTS``. The rest pin the composition: each type and prefix has one owner,
every sibling declaring ``TYPES`` is composed, an owner never imports the facade, every
public name an owner defines other than its ``TYPES`` is the facade's own object, and
every name the test trees
patch through the facade is one the facade itself defines -- an owner's names are read
once, when it is imported, so a patch through the facade could never reach them.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew.crew_log as crew_log_package
from kiro_crew.crew_log import entry_types
from kiro_crew.crew_log.schema import KIND_CREW, KIND_SESSION
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_entry_types_facade")

FACADE = "kiro_crew.crew_log.entry_types"
LEAF = "kiro_crew.crew_log.entry_types_fields"
CREW_LOG_DIR = Path(entry_types.__file__).resolve().parent

#: The published reference pages for each kind: the declared types are the documented
#: ones, so a type dropped or renamed in the registry is one the reference still names.
_REFERENCE = source_corpus.repo_root() / "docs" / "reference" / "crew-log"
_DOCUMENTED = {
    KIND_SESSION: (
        _REFERENCE / "session-types.md",
        re.compile(r"^### `([a-z]+/[a-z_]+)`\s*$", re.M),
    ),
    KIND_CREW: (_REFERENCE / "crew-types.md", re.compile(r"^## `([a-z]+/[a-z_]+)`\s*$", re.M)),
}


#: Contract: the persisted crew-log wire schema. Each declared type, then the first 32
#: hex digits of the SHA-256 of everything its declaration states (fields, required
#: flags, JSON types, enums). Changing a declaration changes what readers of old and
#: new logs accept, so it edits its own line on purpose; a new type adds a line.
_SCHEMA_DIGESTS: dict[str, str] = {
    "crew/dispatch": "5820b02abe6583c997f2cf40fb1465ff",
    "crew/report": "46b293a4663ad9dc56da3afe016b8844",
    "session/opened": "04ef8a56827e9c9078a86c3d509e96db",
    "session/class": "7524b2522063f07a57b88f8e09af6293",
    "session/closed": "a71c964802e62a75e05ac609e252f45e",
    "session/unrestored": "8e9b3dfe651250654e3a55429d30c595",
    "session/adopted": "525b48ab9bf5d674f4398dcf207417b1",
    "session/released": "f1a9ee739bac4c27677df8d0170d2feb",
    "session/spawned": "7386772d486639f42c99e15c5e0b2a48",
    "turn/started": "2c5cb2c6ff71256f1d43927b6fd6106c",
    "turn/refused": "8933c84e395514b4e49208e44a4545a9",
    "turn/completed": "111392f95544c1173c3bc341bd4db8e0",
    "write/dropped": "b8b88e298f6dc4bd990b0d3faf1b865e",
    "message/received": "cf669c725721589588e02b62e7832828",
    "message/sent": "af47be8f0467c00041545fc6be928627",
    "message/chunk": "c73c95c95944935a63a56f660df57444",
    "message/queued": "6180274d3d30cd59a97b4d0c201a1aed",
    "request/configured": "994852b3f44c8bbf3a64e1dd89d1298d",
    "context/composed": "62ed63072024699617c932b98cfe6cc7",
    "context/measured": "e45ef38bb8c2fb8f5a20289daba2f7e2",
    "step/started": "fab7519e4fc65ffab0a6b4206e48b580",
    "step/completed": "36a041f74224ac4dcfadcc5a861aa0ef",
    "tool/called": "065b59afb41bbc227a40882367dfb6f5",
    "tool/completed": "5b49060395c2e7ff544a9861fd97f035",
    "approval/requested": "69ade2dd5394a25d578f8205938b4179",
    "approval/decided": "ef3d9661321ce2f3c680864495c31f1b",
    "approval/bridged": "5e63753192118e89cdcf0a25a9dbb778",
    "model/selected": "3b6f12f703b6d53c5e20d98c3afccc27",
    "compaction/applied": "59e1ed367b9351f74b6789c68ddec2f6",
    "plan/updated": "7cd018ea037a3ceb9a538bf75dc79821",
    "subagent/spawned": "9a8a6655c888333f1432e016097898e0",
    "subagent/steered": "cbc89e1578be381a9422777246b13456",
    "subagent/dismissed": "cf8c93120ff10fc62430e1d01ff85877",
    "subagent/completed": "2b16183f2a82b7687657477dfaa65398",
    "subagent/failed": "e6a1b015742b2baa6733c7071dc4696e",
    "background/completed": "c2baf07274afb461f17f4254c09cf3a6",
    "ledger/recorded": "ce8a9e0246ca5f62f01fe4d9edaad4b9",
    "object/observed": "3aa509be5a56048bafed2769c376e08f",
    "radar/recorded": "f5da674a52d214bb4b0e46b717ff3042",
    "work/recorded": "fc59f6c109d9eec633bbf769a91fae9a",
    "work/charged": "dfafeefd53bcb426be1351ea831bb481",
    "panel/published": "3bed4520ab9f3b183255124dfa140c1e",
    "dashboard/instance_changed": "e89026634b8f99bc617fc02e0839cecc",
    "dashboard/agentic_value": "9ba35f808be9aa3cd45712079b8a9403",
    "dashboard/agentic_refused": "84c3b73827ca0cc5c8d2125efa82e869",
    "card/proposed": "3aa4b12a8444db31ec2704d9b4206cf1",
    "card/finished": "7b65b135a4f6c271df8f31f42152632b",
    "guide/offered": "721d8590a703806a77e0e40a10ef2048",
    "guide/started": "ebf18278dee992c5eea17a2d0b0341d6",
    "guide/finished": "613fdb310da277c35eea97f7911d901b",
    "dashboard/goal_recorded": "efb93c3415691b9494f59daee2952a64",
    "dashboard/waiting_changed": "c66ee05089867149eb2c7cdfa2e2d8a5",
    "dashboard/custom_value": "081599d743b2ec9d384eb75653350287",
    "dashboard/custom_failed": "144633a23583fc8f2eca57e14a8120b6",
    "dashboard/stale_wake": "f41890e0a445d918431855c7c6f7c123",
}


def _schema_digest(spec: entry_types.EntryType) -> str:
    text = json.dumps(dataclasses.asdict(spec), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def test_each_declaration_keeps_its_recorded_wire_schema() -> None:
    declared = {
        entry_type: _schema_digest(spec)
        for table in entry_types.ENTRY_TYPES.values()
        for entry_type, spec in table.items()
    }
    assert declared, "the registry declares no type"
    changed = sorted(
        t for t in declared.keys() & _SCHEMA_DIGESTS.keys() if declared[t] != _SCHEMA_DIGESTS[t]
    )
    assert changed == [], f"these declarations changed their wire schema: {changed}"
    assert sorted(declared.keys() - _SCHEMA_DIGESTS.keys()) == [], "record each new type's digest"
    assert sorted(_SCHEMA_DIGESTS.keys() - declared.keys()) == [], "a recorded type is gone"


def _documented_types(kind: str) -> list[str]:
    path, heading = _DOCUMENTED[kind]
    return heading.findall(path.read_text(encoding="utf-8"))


def _render_mismatches(kind: str, rendered: str) -> list[str]:
    """Where *rendered* is not one ``## `type``` section per declaration of *kind*, in
    table order, each opening with the declaration's summary."""
    table = entry_types.ENTRY_TYPES.get(kind, {})
    out: list[str] = []
    if not rendered.startswith(f"# Declared `{kind}` crew log entry types\n"):
        out.append("the heading names another kind")
    sections = re.findall(r"^## `([^`]+)`\n\n(.*)$", rendered, re.M)
    if [name for name, _ in sections] != list(table):
        out.append(f"sections {[name for name, _ in sections]} != {list(table)}")
    out.extend(
        f"{name}: summary {summary!r}"
        for name, summary in sections
        if name in table and summary != table[name].summary.splitlines()[0]
    )
    return out


def _owner_paths() -> dict[str, Path]:
    return {
        f"kiro_crew.crew_log.{path.stem}": path
        for path in sorted(CREW_LOG_DIR.glob("entry_types_*.py"))
    }


def _top_level_names(tree: ast.Module) -> set[str]:
    """Names a module binds by its own statements, not by its imports."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# The tables are the composed owners, the documented types and the rendered reference
# --------------------------------------------------------------------------- #


def test_the_session_table_is_its_owners_types_in_owner_order() -> None:
    owners = entry_types._SESSION_OWNERS
    assert [owner.__name__ for owner in owners] == [
        "kiro_crew.crew_log.entry_types_lifecycle",
        "kiro_crew.crew_log.entry_types_exchange",
        "kiro_crew.crew_log.entry_types_delegated",
        "kiro_crew.crew_log.entry_types_ledgers",
        "kiro_crew.crew_log.entry_types_surfaces",
    ]
    composed = [spec for owner in owners for spec in owner.TYPES]
    assert len(composed) == len(entry_types._SESSION_TYPES)
    assert all(a is b for a, b in zip(composed, entry_types._SESSION_TYPES))
    # The table's insertion order IS the render order: render_markdown walks it.
    assert list(entry_types.SESSION_ENTRY_TYPES) == [s.type for s in entry_types._SESSION_TYPES]
    assert all(entry_types.SESSION_ENTRY_TYPES[s.type] is s for s in entry_types._SESSION_TYPES)


@pytest.mark.parametrize("kind", [KIND_SESSION, KIND_CREW])
def test_each_table_declares_exactly_the_documented_types(kind: str) -> None:
    """The reference page is the published contract for what a log may hold: a type the
    registry drops or renames is one the page still names, and a new type is declared
    and documented together."""
    documented = _documented_types(kind)
    assert documented, f"no type heading parsed from {_DOCUMENTED[kind][0].name}"
    assert sorted(entry_types.ENTRY_TYPES[kind]) == sorted(documented)


def test_every_lookup_answers_with_the_tables_own_declaration() -> None:
    assert list(entry_types.ENTRY_TYPES) == [KIND_SESSION, KIND_CREW]
    assert entry_types.ENTRY_TYPES[KIND_SESSION] is entry_types.SESSION_ENTRY_TYPES
    assert entry_types.ENTRY_TYPES[KIND_CREW] is entry_types.CREW_ENTRY_TYPES
    for kind, table in entry_types.ENTRY_TYPES.items():
        for type_name, spec in table.items():
            assert entry_types.declaration_for(kind, type_name) is spec
    assert entry_types.declaration_for("member", "session/opened") is None


@pytest.mark.parametrize("kind", [KIND_SESSION, KIND_CREW, "member"])
def test_the_reference_tables_render_one_section_per_declaration(kind: str) -> None:
    assert _render_mismatches(kind, entry_types.render_markdown(kind)) == []
    if kind == KIND_SESSION:
        assert entry_types.render_markdown() == entry_types.render_markdown(KIND_SESSION)


def test_the_render_check_refuses_a_reordered_dropped_or_edited_section() -> None:
    """The comparison above is what fails, not a check that could pass anything."""
    rendered = entry_types.render_markdown(KIND_SESSION)
    sections = rendered.split("\n## ")
    swapped = "\n## ".join([sections[0], sections[2], sections[1], *sections[3:]])
    assert _render_mismatches(KIND_SESSION, swapped)
    assert _render_mismatches(KIND_SESSION, "\n## ".join(sections[:-1]))
    first = entry_types._SESSION_TYPES[0]
    assert _render_mismatches(KIND_SESSION, rendered.replace(first.summary, "changed", 1))
    assert _render_mismatches(KIND_CREW, rendered)


# --------------------------------------------------------------------------- #
# The owners
# --------------------------------------------------------------------------- #


def test_each_type_and_each_prefix_has_one_owner() -> None:
    owners = entry_types._SESSION_OWNERS
    types = Counter(spec.type for owner in owners for spec in owner.TYPES)
    assert [name for name, count in types.items() if count != 1] == []
    prefixes: dict[str, set[str]] = {}
    for owner in owners:
        assert owner.TYPES, f"{owner.__name__} declares nothing"
        for spec in owner.TYPES:
            prefixes.setdefault(spec.type.split("/")[0], set()).add(owner.__name__)
    assert {p: o for p, o in prefixes.items() if len(o) != 1} == {}
    assert len(prefixes) >= 20
    assert set(entry_types.CREW_ENTRY_TYPES).isdisjoint(entry_types.SESSION_ENTRY_TYPES)


def test_every_sibling_that_declares_types_is_an_owner() -> None:
    declaring = {
        name for name, path in _owner_paths().items() if "TYPES" in _top_level_names(_parsed(path))
    }
    assert declaring == {owner.__name__ for owner in entry_types._SESSION_OWNERS}
    assert LEAF in _owner_paths()
    assert LEAF not in declaring


def _imported_modules(tree: ast.AST, package: str = "kiro_crew.crew_log") -> set[str]:
    """Every module *tree* imports, anywhere in it, with relative imports resolved."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                base = f"{parent}.{base}" if base else parent
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("kiro_crew."):
                found.add(node.value)
    return found


def _imports_the_facade(source: str) -> bool:
    return FACADE in _imported_modules(ast.parse(source))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import kiro_crew.crew_log.entry_types\n", True),
        ("from kiro_crew.crew_log import entry_types\n", True),
        ("from kiro_crew.crew_log.entry_types import Field\n", True),
        ("from . import entry_types\n", True),
        ("from .entry_types import Field\n", True),
        (
            "def f():\n    import importlib\n    importlib.import_module('kiro_crew.crew_log.entry_types')\n",
            True,
        ),
        ("from kiro_crew.crew_log.entry_types_fields import Field\n", False),
        ("from kiro_crew.crew_log import entry_types_fields\n", False),
        ("x = 'kiro_crew.crew_log.entry_types_ledgers'\n", False),
    ],
)
def test_the_facade_import_detector_answers_both_ways(source: str, expected: bool) -> None:
    assert _imports_the_facade(source) is expected


def test_owners_never_import_the_facade() -> None:
    paths = _owner_paths()
    assert len(paths) >= 6
    assert sorted(n for n, p in paths.items() if _imports_the_facade(p.read_text("utf-8"))) == []


def test_the_leaf_imports_nothing_from_the_project_and_families_import_no_family() -> None:
    paths = _owner_paths()
    leaf_imports = _imported_modules(_parsed(paths[LEAF]))
    assert sorted(m for m in leaf_imports if m.startswith("kiro_crew")) == []
    for name, path in paths.items():
        imported = {m for m in _imported_modules(_parsed(path)) if m in paths or m == FACADE}
        assert imported <= ({LEAF} - {name}), (name, imported)


def test_importing_every_owner_leaves_the_facade_unloaded(tmp_path: Path) -> None:
    """Checked in a fresh interpreter: an import cycle through a producer module (the
    ledger, the work vocabulary, the omission kinds) is invisible to an AST scan."""
    probe = (
        "import importlib, json, sys\n"
        f"for name in {sorted(_owner_paths())!r}:\n"
        "    importlib.import_module(name)\n"
        f"print(json.dumps({FACADE!r} in sys.modules))\n"
    )
    env = dict(os.environ)
    src = str(Path(entry_types.__file__).resolve().parents[2])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    done = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        capture_output=True,
        check=False,
        env=env,
        cwd=tmp_path,
        timeout=60,
        **UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) is False


# --------------------------------------------------------------------------- #
# The facade's surface
# --------------------------------------------------------------------------- #


def _owner_defined() -> dict[str, set[str]]:
    return {name: _top_level_names(_parsed(path)) for name, path in _owner_paths().items()}


def test_every_public_name_an_owner_defines_is_the_facades_own_object() -> None:
    seen: dict[str, str] = {}
    for name, defined in _owner_defined().items():
        owner = importlib.import_module(name)
        for attr in sorted(defined):
            if attr.startswith("_") or attr == "TYPES":
                continue
            assert attr not in seen, f"{attr} is defined by {seen[attr]} and {name}"
            seen[attr] = name
            assert getattr(entry_types, attr, None) is getattr(owner, attr), (name, attr)
    facade_defined = {n for n in _top_level_names(_parsed(Path(entry_types.__file__)))}
    assert sorted(facade_defined & set(seen)) == []
    assert len(seen) >= 50


def test_the_package_serves_the_facades_objects() -> None:
    forwarded = [n for n, owner in crew_log_package._EXPORTS.items() if owner == "entry_types"]
    assert {"Field", "EntryType", "SESSION_ENTRY_TYPES"} <= set(forwarded)
    for name in forwarded:
        assert getattr(crew_log_package, name) is getattr(entry_types, name), name


# --------------------------------------------------------------------------- #
# Patch reach
# --------------------------------------------------------------------------- #

#: The facade's own spelling, and the package's: a patch of a name the package forwards
#: to the facade lands on the facade, since ``kiro_crew.crew_log`` writes through.
_SPELLINGS = {FACADE: FACADE, "kiro_crew.crew_log": "kiro_crew.crew_log"}
_FORWARDED = frozenset(
    n for n, owner in crew_log_package._EXPORTS.items() if owner == "entry_types"
)


def _names_patched_through_the_facade(source: str) -> set[str]:
    """The names *source* patches on the facade, directly or through a name the package
    forwards to it, as the shared scanner (:mod:`facade_mirror_scan`) reads them."""
    found = facade_mirror_scan.patched_names(source, _SPELLINGS)
    return found.get(FACADE, set()) | (found.get("kiro_crew.crew_log", set()) & _FORWARDED)


def test_the_patch_scan_reads_the_facade_and_the_forwarding_package() -> None:
    """The wiring above; the scanner's own spellings are tested with the scanner."""
    source = (
        "import kiro_crew.crew_log as pkg\n"
        "from kiro_crew.crew_log import entry_types as reg\n"
        "def t(monkeypatch):\n"
        "    monkeypatch.setattr(reg, 'RADAR_PHASES', ())\n"
        "    monkeypatch.setattr('kiro_crew.crew_log.entry_types.PANEL_FOLD_NAME', 'x')\n"
        "    monkeypatch.setattr(pkg, 'Field', 1)\n"
        "    monkeypatch.setattr(pkg, 'CrewLog', 1)\n"
    )
    assert _names_patched_through_the_facade(source) == {
        "RADAR_PHASES",
        "PANEL_FOLD_NAME",
        "Field",
    }


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or rel.startswith("test/") or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> tuple[frozenset[str], int]:
    """Every name the test trees patch through the facade, and how many files were read."""
    found: set[str] = set()
    read = 0
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "crew_log" not in text:
            continue
        read += 1
        found |= _names_patched_through_the_facade(text)
    return frozenset(found), read


def test_every_name_the_tests_patch_through_the_facade_is_the_facades_own() -> None:
    """A name an owner defines is read once, when the owner is imported, so a patch of
    it through the facade could never reach the declaration that read it. Patch the
    facade's own names (the lookup, the validator, the renderer) or build the data."""
    found, read = _corpus_patched_names()
    assert read >= 20, "the scan read almost nothing: the corpus filter stopped matching"
    owner_defined = set().union(*_owner_defined().values())
    assert sorted(found & owner_defined) == []


def test_the_family_modules_hold_nothing_that_runs_after_import() -> None:
    """What keeps a facade patch from missing a reader is that no owner reads a name
    after it is imported: a family module is declarations, so its only functions are
    the builders its declarations call while it loads. The leaf's two records and its
    field builder are the one other exception."""
    allowed = {
        LEAF: {"Field", "EntryType", "_turn"},
        "kiro_crew.crew_log.entry_types_surfaces": {"_conversation_row"},
    }
    for name, path in _owner_paths().items():
        defined = {
            node.name
            for node in ast.walk(_parsed(path))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        assert defined - {"__post_init__", "required_names"} == allowed.get(name, set()), name


def test_the_owners_are_plain_modules() -> None:
    """The facade is a plain module and so is every owner: a read is an attribute
    lookup and a write binds the one module written to."""
    for owner in (entry_types, *entry_types._SESSION_OWNERS):
        assert type(owner) is ModuleType, owner.__name__
