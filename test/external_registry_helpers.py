"""Fixtures shared by the ``test_external_registry*.py`` modules.

``test_external_registry.py`` and its eight ``test_external_registry_<topic>.py`` siblings
import these by name. The two autouse pins apply to every module that imports them, as
they applied to every test of the one module these came from. ``cache_dir`` is declared
with ``name=`` and imported as ``cache_dir_fixture``, so a test's ``cache_dir`` parameter
never shadows an import.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
# flake-ok: pre-existing autouse pin on the shared monkeypatch, moved verbatim; the one monkeypatch.undo() in the test_external_registry*.py modules runs in a finally after its assertions, and only a module reload follows it
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the one monkeypatch.undo() in the test_external_registry*.py modules runs in a finally after its assertions, and only a module reload follows it
def _explicit_registry_execution_admission(monkeypatch):
    """Registry tests exercise admitted installs unless they say otherwise."""
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)


@pytest.fixture(autouse=True)
# flake-ok: pre-existing autouse pin on the shared monkeypatch, moved verbatim; the one monkeypatch.undo() in the test_external_registry*.py modules runs in a finally after its assertions, and only a module reload follows it
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the one monkeypatch.undo() in the test_external_registry*.py modules runs in a finally after its assertions, and only a module reload follows it
def _catalog_absent(monkeypatch):
    """Pin "official catalog reachable, app absent" for the whole module.

    This module is about SEED and EXTERNAL-registry resolution; the official
    catalog is upstream of both in ``_resolve_registry_row``, and its real
    lookup performs a fresh uncached HTTPS fetch on every call.
    Before the suite-wide network guard these tests silently depended on the
    runner's live network being up. A test that wants a different catalog
    answer overrides this by monkeypatching the same seam itself.
    """
    monkeypatch.setattr(
        "kiro_crew.apps.official_catalog.inventory_for_install",
        lambda name: None,
    )


@pytest.fixture(name="cache_dir")
def cache_dir_fixture(tmp_path, monkeypatch):
    """Redirect manifest cache to a temp directory."""
    cache = tmp_path / "cache" / "app-manifests"
    cache.mkdir(parents=True)
    monkeypatch.setattr(
        "kiro_crew.apps.registry._manifest_cache_dir",
        lambda: cache,
    )
    return cache
