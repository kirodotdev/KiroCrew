"""Each split facade keeps resolving every public name it bound before its owners moved out.

A facade whose rules moved into owner modules stays the import path callers, tests and
docs use, so a public name it bound before the split -- one it defined, or one it
imported from the rest of ``kiro_crew`` -- is part of its surface even when no in-repo
caller reads it today. Standard-library, typing and third-party imports and private
names are not surface. Each facade's inventory is frozen at its split in its own file,
``campaign_facade_inventories/<facade>.json``, so a split adds a file and edits no shared
one. A name leaves an inventory only when the facade deliberately stops offering it.

Each name a facade binds again by import is listed with the module that holds it, and
a fresh interpreter checks the facade's binding is that module's own object, so no
reload or eviction elsewhere in the suite can decide the result.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import kiro_crew
from kiro_crew.subprocess_utf8 import UTF8_TEXT

#: One ``<facade>.json`` per split facade, named by the facade's dotted module path.
_INVENTORY_DIR = Path(__file__).resolve().parent / "campaign_facade_inventories"
_INVENTORY_KEYS = frozenset({"base_public_names", "rebound"})
#: Optional prose kept beside an inventory, such as why the facade forwards its names.
_NOTE_KEY = "note"


def _entries(directory: Path) -> list[Path]:
    """What *directory* holds, less hidden files a desktop or an editor leaves beside them."""
    return sorted(path for path in directory.iterdir() if not path.name.startswith("."))


def _canonical_text(inventory: dict[str, Any]) -> str:
    """The one spelling of an inventory file: sorted keys and one entry per line."""
    return json.dumps(inventory, indent=1, sort_keys=True) + "\n"


def _file_problems(path: Path) -> list[str]:
    """Why *path* is not one well-formed inventory file; empty when it is one."""
    if path.suffix != ".json" or not path.is_file():
        return [f"{path.name}: only <facade>.json inventory files belong here"]
    try:
        text = path.read_text(encoding="utf-8")
        inventory = json.loads(text)
    except ValueError as exc:
        return [f"{path.name}: not UTF-8 JSON ({exc})"]
    keys = set(inventory) if isinstance(inventory, dict) else set()
    if not _INVENTORY_KEYS <= keys <= _INVENTORY_KEYS | {_NOTE_KEY}:
        return [f"{path.name}: keys must be {sorted(_INVENTORY_KEYS)} and an optional note"]
    problems = []
    names, rebound = inventory["base_public_names"], inventory["rebound"]
    if not (
        isinstance(names, list)
        and all(isinstance(name, str) for name in names)
        and names == sorted(set(names))
    ):
        problems.append(f"{path.name}: base_public_names must be sorted, distinct names")
    if not (isinstance(rebound, dict) and all(isinstance(home, str) for home in rebound.values())):
        problems.append(f"{path.name}: rebound must map each name to the module holding it")
    if not isinstance(inventory.get(_NOTE_KEY, ""), str):
        problems.append(f"{path.name}: the note must be a string")
    if not problems and text != _canonical_text(inventory):
        problems.append(f"{path.name}: write json.dumps(inventory, indent=1, sort_keys=True)")
    return problems


def _load_inventories(directory: Path) -> dict[str, dict[str, Any]]:
    """Each well-formed inventory under *directory*, keyed by the facade its file names.

    A malformed file is left out here and named by the layout tests below, so one bad
    file reports itself instead of stopping this module from collecting.
    """
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in _entries(directory)
        if not _file_problems(path)
    }


_INVENTORIES = _load_inventories(_INVENTORY_DIR)

#: Facade -> the public project names it bound before its split; each must keep resolving.
_BASE_PUBLIC_NAMES: dict[str, frozenset[str]] = {
    facade: frozenset(inventory["base_public_names"]) for facade, inventory in _INVENTORIES.items()
}

#: Facade -> ``{name: module holding it}`` for each base name the facade re-binds by import.
_REBOUND: dict[str, dict[str, str]] = {
    facade: dict(inventory["rebound"]) for facade, inventory in _INVENTORIES.items()
}

#: Facade -> digest of the inventory in its file, pinned for the first ten split facades so a
#: change to one of their files is deliberate. A new facade never adds a row here: it adds its
#: own file. A split that changes one of these inventories updates that facade's row. A blank
#: line between rows lets two splits of neighbouring facades each change their own row.
_MOVE_DIGESTS: dict[str, str] = dict(row.split() for row in """
    kiro_crew.acp.client                                        b0bc1c4a2f46693d

    kiro_crew.apps.builtins.issue_radar.backend.routes          e42a59c7e96875b3

    kiro_crew.apps.builtins.ops_mission_control.backend.routes  4676b23fcf6c0cce

    kiro_crew.autonudge                                         57a726a2ffc0bbaa

    kiro_crew.crew_log.projection                               aa5a632cc0ca0c51

    kiro_crew.cron                                              b6a4481b56885fac

    kiro_crew.mcp_gateway.gatewayd                              290c22d9c2d9df84

    kiro_crew.providers.acp                                     f3d54671b53ceb28

    kiro_crew.sandbox                                           c9e1858832fcd0e4

    kiro_crew.slack.gateway                                     90d815f8e6349017
    """.splitlines() if row.strip())

#: Reads the rebound map from stdin, not argv: one argv string holding every facade's map
#: grows with each split and passes Windows' 32,767-character command-line limit.
_PROBE = """
import importlib, json, sys
rebound = json.loads(sys.stdin.read())
checked, wrong = 0, []
for facade, names in rebound.items():
    module = importlib.import_module(facade)
    for name, home in names.items():
        checked += 1
        if getattr(module, name, None) is not getattr(importlib.import_module(home), name):
            wrong.append(f"{facade}.{name}")
print(json.dumps({"checked": checked, "wrong": wrong}))
"""


@pytest.mark.parametrize("facade", sorted(_BASE_PUBLIC_NAMES))
def test_every_base_public_name_resolves_on_its_facade(facade: str) -> None:
    module = importlib.import_module(facade)
    assert sorted(name for name in _BASE_PUBLIC_NAMES[facade] if not hasattr(module, name)) == []


def test_every_rebound_name_is_a_base_public_name_of_its_facade() -> None:
    assert set(_REBOUND) == set(_BASE_PUBLIC_NAMES)
    for facade, names in _REBOUND.items():
        assert names and set(names) <= _BASE_PUBLIC_NAMES[facade], facade


def test_a_rebound_name_is_the_object_its_home_module_holds(tmp_path: Path) -> None:
    """Checked in a fresh interpreter, so no module another test reloaded or evicted
    can make a copy look like the original, or the original look like a copy. The child
    runs in ``tmp_path``, so anything an import writes by a relative path stays there."""
    env = dict(os.environ)
    src = str(Path(kiro_crew.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    command = [sys.executable, "-c", _PROBE]
    assert sum(map(len, command)) < 8192, "the probe's command line must not carry the inventories"
    out = subprocess.run(
        command,
        input=json.dumps(_REBOUND),
        capture_output=True,
        check=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        **UTF8_TEXT,
    )
    result = json.loads(out.stdout.splitlines()[-1])
    assert result == {"checked": sum(map(len, _REBOUND.values())), "wrong": []}, out.stderr


def _inventory_digest(inventory: dict[str, Any]) -> str:
    """A short digest of the frozen names and homes; the note is prose and stays out."""
    frozen = {key: inventory[key] for key in _INVENTORY_KEYS}
    return hashlib.sha256(json.dumps(frozen, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _layout_problems(directory: Path) -> list[str]:
    """Each entry under *directory* the loader does not read, or that names no facade."""
    loaded = _load_inventories(directory)
    problems = [
        f"{path.name}: not loaded"
        for path in _entries(directory)
        if path.name not in {f"{facade}.json" for facade in loaded}
    ]
    for facade in sorted(loaded):
        try:
            importlib.import_module(facade)
        except ImportError as exc:
            problems.append(f"{facade}.json: names no importable facade ({exc})")
    return problems


def _shape_problems(directory: Path) -> list[str]:
    return [problem for path in _entries(directory) for problem in _file_problems(path)]


def _digest_mismatches(inventories: dict[str, dict[str, Any]]) -> list[str]:
    return [
        facade
        for facade, digest in sorted(_MOVE_DIGESTS.items())
        if facade not in inventories or _inventory_digest(inventories[facade]) != digest
    ]


def test_every_inventory_file_is_loaded_and_names_a_facade_that_imports() -> None:
    assert _layout_problems(_INVENTORY_DIR) == []


def test_every_inventory_file_holds_exactly_the_inventory_keys() -> None:
    assert _shape_problems(_INVENTORY_DIR) == []


def test_every_pinned_inventory_keeps_its_digest() -> None:
    assert _digest_mismatches(_INVENTORIES) == []


_PLANTED = {"base_public_names": ["x"], "rebound": {"x": "kiro_crew"}}

#: A bogus file to plant, its contents, and words from the problem it must be reported with.
_PLANTED_FILES: dict[str, tuple[str | bytes, str]] = {
    "README.md": ("Inventories.\n", "only <facade>.json"),
    "planted.broken.json": ("{\n", "not UTF-8 JSON"),
    "planted.json.orig": (_canonical_text(_PLANTED), "only <facade>.json"),
    "planted.utf16.json": (_canonical_text(_PLANTED).encode("utf-16"), "not UTF-8 JSON"),
    "planted.extra.json": (_canonical_text({**_PLANTED, "owners": []}), "keys must be"),
    "planted.homeless.json": (_canonical_text({**_PLANTED, "rebound": {"x": 1}}), "holding it"),
    "planted.no_such_facade.json": (_canonical_text(_PLANTED), "names no importable facade"),
    "planted.noted.json": (_canonical_text({**_PLANTED, _NOTE_KEY: 1}), "must be a string"),
    "planted.partial.json": (_canonical_text({"base_public_names": ["x"]}), "keys must be"),
    "planted.unsorted.json": (
        _canonical_text({**_PLANTED, "base_public_names": ["y", "x"]}),
        "sorted, distinct",
    ),
    "planted.untidy.json": (json.dumps(_PLANTED) + "\n", "indent=1"),
}


@pytest.mark.parametrize("name", sorted(_PLANTED_FILES))
def test_a_planted_bogus_inventory_file_goes_red(tmp_path: Path, name: str) -> None:
    text, reason = _PLANTED_FILES[name]
    directory = tmp_path / "inventories"
    shutil.copytree(_INVENTORY_DIR, directory)
    assert _layout_problems(directory) == []
    (directory / name).write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    reported = _layout_problems(directory) + _shape_problems(directory)
    assert {problem.split(":")[0] for problem in reported} == {name}
    assert any(reason in problem for problem in reported), reported


def test_a_hidden_file_beside_the_inventories_is_not_read(tmp_path: Path) -> None:
    directory = tmp_path / "inventories"
    shutil.copytree(_INVENTORY_DIR, directory)
    (directory / ".DS_Store").write_bytes(b"\0")
    (directory / ".planted.json").write_text(_canonical_text(_PLANTED), encoding="utf-8")
    assert _layout_problems(directory) == []
    assert _load_inventories(directory) == _INVENTORIES


def test_a_changed_or_missing_frozen_inventory_misses_its_digest() -> None:
    facade = min(_MOVE_DIGESTS)
    inventory = _INVENTORIES[facade]
    shrunk = {**inventory, "base_public_names": inventory["base_public_names"][1:]}
    rehomed = {**inventory, "rebound": {name: "kiro_crew" for name in inventory["rebound"]}}
    noted = {**inventory, _NOTE_KEY: "Prose stays out of the digest."}
    assert _digest_mismatches({**_INVENTORIES, facade: shrunk}) == [facade]
    assert _digest_mismatches({**_INVENTORIES, facade: rehomed}) == [facade]
    assert _digest_mismatches({k: v for k, v in _INVENTORIES.items() if k != facade}) == [facade]
    assert _digest_mismatches({**_INVENTORIES, facade: noted}) == []
