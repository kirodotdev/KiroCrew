"""A folder install named like a still-running built-in says so, at install and on its row."""

from __future__ import annotations

import pytest
from builtin_adoption_helpers import (
    NAME,
    PACKAGE,
    install_pins,
    pin_row,
    shipped_manifest,
    write_builtin_record,
    write_tree,
)

from kiro_crew.apps import builtin_adoption_pins as pins_mod
from kiro_crew.apps import execution as exec_mod
from kiro_crew.apps import manager
from kiro_crew.apps.builtin_shadow import shadowed_builtin_notice


@pytest.fixture
def wheel(tmp_path, monkeypatch):
    """The fixture app shipped in a wheel package, with no record yet."""
    root = tmp_path / "wheel"
    write_tree(root / PACKAGE, shipped_manifest())
    monkeypatch.setattr(exec_mod, "_builtin_manifest_sources", lambda: (root.resolve(),))
    install_pins(monkeypatch, tmp_path / "pins.json", [])
    yield
    pins_mod.load_adoption_pins.cache_clear()
    manager.invalidate_orphan_cache()


@pytest.fixture
def pinned(tmp_path, monkeypatch, wheel):
    install_pins(monkeypatch, tmp_path / "pins.json", [pin_row("a" * 40, "b" * 64, "c" * 64)])


def _install_local_build(tmp_path) -> manager.AppResult:
    source = write_tree(tmp_path / "local-build", shipped_manifest())
    result = manager.install_app(str(source))
    assert result.ok, result.error
    return result


def test_local_build_of_a_pinned_builtin_names_the_builtin_and_the_app_store(tmp_path, pinned):
    result = _install_local_build(tmp_path)

    assert f"the built-in {NAME} app still runs" in result.message
    assert "from the App Store" in result.message
    row = manager.get_app(NAME)
    assert row is not None and "still runs" in row["shadowedBuiltin"]
    listed = {app["name"]: app for app in manager.list_apps()}
    assert listed[NAME]["shadowedBuiltin"] == row["shadowedBuiltin"]


def test_unpinned_builtin_notice_has_no_app_store_step(tmp_path, wheel):
    result = _install_local_build(tmp_path)

    assert f"the built-in {NAME} app still runs" in result.message
    assert "App Store" not in result.message


def test_builtin_owned_record_and_unshipped_name_carry_no_notice(tmp_path, pinned):
    write_builtin_record()

    assert shadowed_builtin_notice(NAME, manager._read_installed(NAME)) == ""
    assert "shadowedBuiltin" not in (manager.get_app(NAME) or {})
    assert shadowed_builtin_notice("not-shipped", manager.InstalledApp(name="not-shipped")) == ""
