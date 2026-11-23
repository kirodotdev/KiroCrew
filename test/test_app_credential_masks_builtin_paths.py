"""The shipped built-ins' manifest paths are built once, and still read fresh.

``credential_masks._builtin_manifests`` runs several times for every sandboxed
spawn. It reuses one parsed ``Path`` per package, but still lists the directory
and reads every manifest on each call, so the declared masks it feeds are
exactly what the files say at that moment.
"""

from __future__ import annotations

import json
import os
import pathlib
from pathlib import Path

import pytest

from kiro_crew.apps import credential_masks


@pytest.fixture(autouse=True)
def _fresh_cache():
    credential_masks.clear_cache()
    yield
    credential_masks.clear_cache()


def _plain_listing(root: Path) -> list[tuple[str, str]]:
    packages = sorted(e.name for e in os.scandir(root) if e.is_dir())
    return [
        (p.replace("_", "-"), str(root / p / "app.json"))
        for p in packages
        if not p.startswith(("_", "."))
    ]


def _ship(root: Path, package: str, masks: list[str]) -> None:
    manifest = root / package / "app.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    name = package.replace("_", "-")
    manifest.write_text(json.dumps({"name": name, "credentialMasks": masks}), encoding="utf-8")


def test_the_listing_matches_the_shipped_tree():
    listed = [(name, str(path)) for name, path in credential_masks._builtin_manifests()]
    assert listed == _plain_listing(credential_masks._builtins_root())
    assert listed, "the shipped tree lists no built-in package"


def test_a_repeat_listing_parses_no_path(monkeypatch):
    credential_masks._builtin_manifests()
    calls = []
    real = pathlib.PurePath._parse_path.__func__

    def _counting(cls, path):
        calls.append(path)
        return real(cls, path)

    monkeypatch.setattr(pathlib.PurePath, "_parse_path", classmethod(_counting))
    for _name, path in credential_masks._builtin_manifests():
        str(path)
    assert calls == []


def test_packages_and_manifests_that_change_are_read_as_they_are_now(tmp_path, monkeypatch):
    root = tmp_path / "builtins"
    monkeypatch.setattr(credential_masks, "_builtins_root", lambda: root)
    _ship(root, "demo_one", ["one.json"])
    assert credential_masks.declared_masks(installed=False) == {"demo-one": ("one.json",)}

    _ship(root, "demo_two", ["two.json"])
    assert credential_masks.declared_masks(installed=False) == {
        "demo-one": ("one.json",),
        "demo-two": ("two.json",),
    }

    # A rewritten manifest with a new size is a new file identity, so it is read again.
    _ship(root, "demo_one", ["one.json", "extra.json"])
    assert credential_masks.declared_masks(installed=False)["demo-one"] == (
        "one.json",
        "extra.json",
    )

    (root / "demo_two" / "app.json").unlink()
    (root / "demo_two").rmdir()
    assert credential_masks.declared_masks(installed=False) == {
        "demo-one": ("one.json", "extra.json")
    }


def test_a_different_root_never_reuses_another_roots_path(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    _ship(first, "demo", ["a.json"])
    _ship(second, "demo", ["b.json"])
    monkeypatch.setattr(credential_masks, "_builtins_root", lambda: first)
    assert credential_masks.declared_masks(installed=False) == {"demo": ("a.json",)}
    monkeypatch.setattr(credential_masks, "_builtins_root", lambda: second)
    assert credential_masks.declared_masks(installed=False) == {"demo": ("b.json",)}
