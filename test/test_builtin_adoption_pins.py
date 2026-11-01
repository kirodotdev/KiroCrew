"""The adoption pin table: validation (matrix row 21), the shipped file, no app names in core (row 20)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from builtin_adoption_helpers import NAME, REPOSITORY, install_pins, pin_row

import kiro_crew
from kiro_crew.apps import builtin_adoption_pins as pins_mod
from kiro_crew.apps.builtin_adoption_pins import load_adoption_pins, parse_adoption_pins

_SRC = Path(kiro_crew.__file__).resolve().parent
COMMIT = "a" * 40
TREE = "b" * 64
ARCHIVE = "c" * 64


@pytest.fixture(autouse=True)
def _fresh_cache():
    load_adoption_pins.cache_clear()
    yield
    load_adoption_pins.cache_clear()


def _row(**overrides):
    return pin_row(COMMIT, TREE, ARCHIVE, **overrides)


def _parse(rows):
    return parse_adoption_pins({"schemaVersion": 1, "pins": rows})


def test_a_valid_row_loads_with_a_derived_archive_url():
    pins = _parse([_row()])
    pin = pins[NAME]
    assert pin.repository == REPOSITORY
    assert pin.archive_url == (
        f"https://codeload.github.com/FixtureOrg/kirocrew-app-fixture-former/tar.gz/{COMMIT}"
    )
    assert pin.archive_root == f"kirocrew-app-fixture-former-{COMMIT}"
    assert (pin.legacy_route, pin.legacy_icon) == (f"/{NAME}", "Box")


@pytest.mark.parametrize(
    "overrides",
    [
        {"repository": REPOSITORY + ".git"},
        {"repository": "https://user:pw@github.com/FixtureOrg/repo"},
        {"repository": "https://github.com/FixtureOrg/repo?x=1"},
        {"repository": "https://gitlab.com/FixtureOrg/repo"},
        {"commit": "A" * 40},
        {"commit": "a" * 39},
        {"treeSha256": "z" * 64},
        {"archiveSha256": "c" * 63},
        {"version": "1.0"},
        {"minAppSdkLevel": 0},
        {"minAppSdkLevel": True},
        {"name": "Not_Kebab"},
        {"name": "system"},
        {"legacyNav": {"route": "personal", "icon": "Box"}},
        {"legacyNav": {"route": "/x", "icon": "lowercase"}},
        {"legacyNav": {"route": "/x"}},
        {"archiveUrl": "https://evil.example/archive.tar.gz"},
    ],
)
def test_a_malformed_row_is_dropped(overrides):
    assert _parse([_row(**overrides)]) == {}


def test_a_dropped_row_does_not_drop_its_neighbours():
    other = _row(name="other-app")
    assert set(_parse([_row(commit="bad"), other])) == {"other-app"}


def test_a_duplicated_name_drops_every_row_with_it():
    assert _parse([_row(), _row(commit="d" * 40), _row(name="other-app")]).keys() == {"other-app"}


def test_more_rows_than_the_cap_drop_the_whole_table():
    rows = [_row(name=f"app-{i}") for i in range(pins_mod.MAX_PINS + 1)]
    assert _parse(rows) == {}
    assert len(_parse(rows[:-1])) == pins_mod.MAX_PINS


@pytest.mark.parametrize("document", [[], {"schemaVersion": 2, "pins": []}, {"schemaVersion": 1}])
def test_an_unknown_document_shape_is_an_empty_table(document):
    assert parse_adoption_pins(document) == {}


def test_a_missing_or_unparseable_file_is_an_empty_table(tmp_path, monkeypatch):
    monkeypatch.setattr(pins_mod, "_PINS_PATH", tmp_path / "absent.json")
    assert load_adoption_pins() == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(pins_mod, "_PINS_PATH", bad)
    load_adoption_pins.cache_clear()
    assert load_adoption_pins() == {}


def test_the_table_is_read_once_per_process(tmp_path, monkeypatch):
    path = tmp_path / "pins.json"
    install_pins(monkeypatch, path, [_row()])
    assert pins_mod.adoption_pin_names() == {NAME}
    path.write_text(json.dumps({"schemaVersion": 1, "pins": []}), encoding="utf-8")
    assert pins_mod.adoption_pin(NAME) is not None
    load_adoption_pins.cache_clear()
    assert pins_mod.adoption_pin(NAME) is None


# --------------------------------------------------------------------------- #
# The shipped file and the no-app-names rule (row 20)
# --------------------------------------------------------------------------- #


_SHIPPED_PINS = [
    {
        "name": "personal-shopper",
        "repository": "https://github.com/KiroCrewCaptain/kirocrew-app-personal-shopper",
        "commit": "5ee04d9924519b2a1df19e43715b3127c4c400ef",
        "version": "0.1.0",
        "treeSha256": "586797aa4a619094065f0b0f1360a17df89e3fcf68b19b1a3a1f53489dfe989f",
        "archiveSha256": "5b06ef841a9dd5c38240ff72b1c1178233ccb9c4f7a7a52fbe8be5075590ce2d",
        "minAppSdkLevel": 1,
        "legacyNav": {"route": "/personal-shopper", "icon": "ShoppingBag"},
    },
]


def test_the_shipped_pin_file_validates_and_pins_exactly_the_shipped_table():
    """A pin change edits this table with the row ``adoption_pin.py compute`` printed."""
    document = json.loads((_SRC / "apps" / "adoption-pins.json").read_text(encoding="utf-8"))
    assert document == {"schemaVersion": 1, "pins": _SHIPPED_PINS}
    assert sorted(parse_adoption_pins(document)) == sorted(row["name"] for row in _SHIPPED_PINS)


def test_every_shipped_pin_matches_its_wheel_built_in_version():
    """D12: a first-adoption pin carries the wheel built-in's version (downgrade-safe)."""
    from kiro_crew.apps.execution import _shipped_builtin_apps

    shipped = {}
    for root, name in _shipped_builtin_apps():
        shipped[name] = json.loads((root / "app.json").read_text(encoding="utf-8"))["version"]
    document = json.loads((_SRC / "apps" / "adoption-pins.json").read_text(encoding="utf-8"))
    for pin in parse_adoption_pins(document).values():
        assert pin.name in shipped
        assert pin.version == shipped[pin.name]


def test_the_package_ships_the_pin_file():
    setup = (_SRC.parents[1] / "setup.cfg").read_text(encoding="utf-8")
    assert "apps/adoption-pins.json" in setup


_ADOPTION_MODULES = (
    "apps/builtin_adoption.py",
    "apps/builtin_adoption_apply.py",
    "apps/builtin_adoption_checks.py",
    "apps/builtin_adoption_fetch.py",
    "apps/builtin_adoption_markers.py",
    "apps/builtin_adoption_pins.py",
    "apps/builtin_adoption_state.py",
    "apps/manager_adopt.py",
    "apps/agent_config_writer.py",
)


def _builtin_identifiers() -> set[str]:
    from kiro_crew.apps.builtins import BUILTIN_NAMES
    from kiro_crew.apps.execution import _shipped_builtin_apps

    names = set(BUILTIN_NAMES)
    for root, name in _shipped_builtin_apps():
        names |= {name, root.name}
    return names


def _string_literals(source: str) -> set[str]:
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


@pytest.mark.parametrize("rel", _ADOPTION_MODULES)
def test_no_adoption_module_names_a_shipped_built_in(rel):
    builtins = _builtin_identifiers()
    assert builtins, "the census found no shipped built-in"
    literals = _string_literals((_SRC / rel).read_text(encoding="utf-8"))
    assert sorted(literals & builtins) == []


def test_the_literal_scan_finds_a_planted_name():
    some = sorted(_builtin_identifiers())[0]
    assert some in _string_literals(f"X = {some!r}\n")
