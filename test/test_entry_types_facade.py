"""The entry-type registry keeps its content, its order and its one import path.

``kiro_crew.crew_log.entry_types`` composes the session table from five family owners
(``entry_types_lifecycle``, ``entry_types_exchange``, ``entry_types_delegated``,
``entry_types_ledgers``, ``entry_types_surfaces``) written over one leaf,
``entry_types_fields``, and stays the module callers, tests and documents import from.

The registry is a schema: what each declaration says and the order the tables render
in are its contract. So the first tests here hold the composed tables, every lookup and
the rendered reference tables equal to the registry as it stood when it was split into
owners, by digest. A declaration changed on purpose changes its row; the failure prints
the rows to paste. The rest pin the composition: each type and prefix has one owner,
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

#: Each kind's declarations when the registry was split into owners: type, then the
#: first 32 hex digits of the SHA-256 of ``_canonical(declaration)``, in table order.
_GOLDEN_TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    KIND_SESSION: (
        ("session/opened", "04ef8a56827e9c9078a86c3d509e96db"),
        ("session/class", "7524b2522063f07a57b88f8e09af6293"),
        ("session/closed", "a71c964802e62a75e05ac609e252f45e"),
        ("session/unrestored", "8e9b3dfe651250654e3a55429d30c595"),
        ("session/adopted", "525b48ab9bf5d674f4398dcf207417b1"),
        ("session/released", "f1a9ee739bac4c27677df8d0170d2feb"),
        ("session/spawned", "7386772d486639f42c99e15c5e0b2a48"),
        ("turn/started", "4da5599f3789b19155182a2d01cd37da"),
        ("turn/refused", "259f77ef9760e46fb840162b57a263d6"),
        ("turn/completed", "111392f95544c1173c3bc341bd4db8e0"),
        ("write/dropped", "b8b88e298f6dc4bd990b0d3faf1b865e"),
        ("message/received", "cf669c725721589588e02b62e7832828"),
        ("message/sent", "af47be8f0467c00041545fc6be928627"),
        ("message/chunk", "c73c95c95944935a63a56f660df57444"),
        ("message/queued", "6180274d3d30cd59a97b4d0c201a1aed"),
        ("request/configured", "994852b3f44c8bbf3a64e1dd89d1298d"),
        ("context/composed", "62ed63072024699617c932b98cfe6cc7"),
        ("context/measured", "e45ef38bb8c2fb8f5a20289daba2f7e2"),
        ("step/started", "fab7519e4fc65ffab0a6b4206e48b580"),
        ("step/completed", "36a041f74224ac4dcfadcc5a861aa0ef"),
        ("tool/called", "065b59afb41bbc227a40882367dfb6f5"),
        ("tool/completed", "5b49060395c2e7ff544a9861fd97f035"),
        ("approval/requested", "69ade2dd5394a25d578f8205938b4179"),
        ("approval/decided", "ef3d9661321ce2f3c680864495c31f1b"),
        ("model/selected", "3b6f12f703b6d53c5e20d98c3afccc27"),
        ("compaction/applied", "59e1ed367b9351f74b6789c68ddec2f6"),
        ("plan/updated", "7cd018ea037a3ceb9a538bf75dc79821"),
        ("subagent/spawned", "9a8a6655c888333f1432e016097898e0"),
        ("subagent/steered", "cbc89e1578be381a9422777246b13456"),
        ("subagent/dismissed", "cf8c93120ff10fc62430e1d01ff85877"),
        ("subagent/completed", "3d9c7a881ccb9cc2d74d40d131e8d84e"),
        ("subagent/failed", "36bcc7437227a19656db9cfeff419bca"),
        ("background/completed", "c2baf07274afb461f17f4254c09cf3a6"),
        ("ledger/recorded", "ce8a9e0246ca5f62f01fe4d9edaad4b9"),
        ("object/observed", "e7708ecf53c90b1b1e5e5340c59a97e0"),
        ("radar/recorded", "f5da674a52d214bb4b0e46b717ff3042"),
        ("work/recorded", "cc70cbd04f19d8f6cf8b6c3ada680d8a"),
        ("work/charged", "dfafeefd53bcb426be1351ea831bb481"),
        ("panel/published", "3bed4520ab9f3b183255124dfa140c1e"),
        ("dashboard/instance_changed", "f8187ea96d4c04bc538184bbbbffaca2"),
        ("dashboard/agentic_value", "5fa08587836bf07bca2f89201f0ffc28"),
        ("dashboard/agentic_refused", "8a1ec793a0785956734a05b7783512c8"),
        ("card/proposed", "3aa4b12a8444db31ec2704d9b4206cf1"),
        ("card/finished", "7b65b135a4f6c271df8f31f42152632b"),
        ("guide/offered", "721d8590a703806a77e0e40a10ef2048"),
        ("guide/started", "ebf18278dee992c5eea17a2d0b0341d6"),
        ("guide/finished", "613fdb310da277c35eea97f7911d901b"),
        ("dashboard/goal_recorded", "d9a0196325963ff3db8e6fbdf7c84004"),
        ("dashboard/waiting_changed", "cb4f1800ba39b67fd925d785de126b7b"),
        ("dashboard/custom_value", "5d84827cd8f329bb4a9be149447dce62"),
    ),
    KIND_CREW: (
        ("crew/dispatch", "5820b02abe6583c997f2cf40fb1465ff"),
        ("crew/report", "46b293a4663ad9dc56da3afe016b8844"),
    ),
}

#: SHA-256 of ``render_markdown(kind)`` when the registry was split into owners. The
#: member kind declares nothing here, so its digest is of the heading alone.
_GOLDEN_RENDER: dict[str, str] = {
    KIND_SESSION: "38bed9911f5f1c590a7056e3777f8258213cf62a1a3b85a442125990715774e5",
    KIND_CREW: "b6938d4f70730ae5ffce4ec6983a609443976a9fca7b013c04152e426dd8be8e",
    "member": "5cb4ea02ed8121eb6b285806b4723251aed568df0aa54b9ac8505c9ad389fa8d",
}


def _canonical(spec: entry_types.EntryType) -> str:
    """Everything a declaration states, as one stable string."""
    return json.dumps(dataclasses.asdict(spec), sort_keys=True, separators=(",", ":"))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rows(specs) -> tuple[tuple[str, str], ...]:
    return tuple((spec.type, _digest(_canonical(spec))[:32]) for spec in specs)


def _golden_mismatches(kind: str, specs) -> list[str]:
    """Where *specs* differ from the golden table for *kind*, row by row."""
    got = _rows(specs)
    want = _GOLDEN_TABLES[kind]
    out = [f"{i}: {g} != {w}" for i, (g, w) in enumerate(zip(got, want)) if g != w]
    if len(got) != len(want):
        out.append(f"length {len(got)} != {len(want)}")
    return out


def _paste(kind: str, specs) -> str:
    return "\n".join(f'        ("{t}", "{h}"),' for t, h in _rows(specs)) + f"\n  ({kind})"


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
# The tables equal the registry at the split
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
def test_each_table_equals_the_registry_at_the_split(kind: str) -> None:
    table = entry_types.ENTRY_TYPES[kind]
    assert _golden_mismatches(kind, table.values()) == [], (
        "a declaration differs from the registry at the split; if the change is meant, "
        "replace the rows with:\n" + _paste(kind, table.values())
    )


def test_every_lookup_answers_with_the_tables_own_declaration() -> None:
    assert list(entry_types.ENTRY_TYPES) == [KIND_SESSION, KIND_CREW]
    assert entry_types.ENTRY_TYPES[KIND_SESSION] is entry_types.SESSION_ENTRY_TYPES
    assert entry_types.ENTRY_TYPES[KIND_CREW] is entry_types.CREW_ENTRY_TYPES
    for kind, table in entry_types.ENTRY_TYPES.items():
        for type_name, spec in table.items():
            assert entry_types.declaration_for(kind, type_name) is spec
    assert entry_types.declaration_for("member", "session/opened") is None


@pytest.mark.parametrize("kind", sorted(_GOLDEN_RENDER))
def test_the_reference_tables_render_as_they_did_at_the_split(kind: str) -> None:
    assert _digest(entry_types.render_markdown(kind)) == _GOLDEN_RENDER[kind]
    if kind == KIND_SESSION:
        assert entry_types.render_markdown() == entry_types.render_markdown(KIND_SESSION)


def test_the_golden_check_refuses_a_reordered_or_edited_table() -> None:
    """The comparison above is what fails, not a check that could pass anything."""
    owners = list(entry_types._SESSION_OWNERS)
    owners[1], owners[3] = owners[3], owners[1]
    swapped = [spec for owner in owners for spec in owner.TYPES]
    assert _golden_mismatches(KIND_SESSION, swapped)
    edited = list(entry_types._SESSION_TYPES)
    edited[0] = dataclasses.replace(edited[0], summary=edited[0].summary + " ")
    assert _golden_mismatches(KIND_SESSION, edited) == [
        f"0: {_rows(edited[:1])[0]} != {_GOLDEN_TABLES[KIND_SESSION][0]}"
    ]
    assert _golden_mismatches(KIND_SESSION, entry_types._SESSION_TYPES[:-1])


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
