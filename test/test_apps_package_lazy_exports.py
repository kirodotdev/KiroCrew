"""``kiro_crew.apps`` binds its public names on first access, to the objects their modules hold."""

from __future__ import annotations

import importlib

import pytest

import kiro_crew.apps as apps


@pytest.mark.parametrize("name", sorted(apps._LAZY_EXPORTS))
def test_every_export_is_the_object_its_module_binds(name: str) -> None:
    home = importlib.import_module(apps._LAZY_EXPORTS[name])
    assert getattr(apps, name) is getattr(home, name)
    assert name in apps.__all__


def test_an_unknown_name_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="no_such_export"):
        apps.__getattr__("no_such_export")


def test_dir_lists_every_export() -> None:
    assert set(apps._LAZY_EXPORTS) <= set(dir(apps))


def test_a_submodule_still_imports_from_the_package() -> None:
    from kiro_crew.apps import manifest_names

    assert manifest_names.__name__ == "kiro_crew.apps.manifest_names"
