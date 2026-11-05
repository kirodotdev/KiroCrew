"""A catalog that lists an app with no install source is not an unreachable one.

Both refuse the install the same way; what differs is what the refusal tells the
person. Saying "the catalog could not be reached" when it answered sends them to
retry a network that is fine.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from kiro_crew.apps import registry as reg

URL = "https://github.com/org/dup"


def _catalog(*, reachable: bool, apps: list[dict[str, Any]]):
    def fetch(url, *a, **k):
        if not reachable:
            return None  # what an unreachable catalog actually yields
        return {"schemaVersion": reg.official_catalog.SUPPORTED_SCHEMA_VERSION, "apps": apps}

    return fetch


@pytest.fixture
def no_external(monkeypatch):
    monkeypatch.setattr(
        reg,
        "_read_external_registry_cache",
        lambda *a, **k: [{"name": "dup", "gitUrl": "https://ext.example/x", "repo": "e/x"}],
    )


BUILTIN_ROW = {"name": "dup", "source": {"type": "builtin"}}


@pytest.mark.parametrize("seeded", [False, True])
def test_a_listed_app_without_coordinates_says_so(monkeypatch, caplog, no_external, seeded):
    seed = [{"name": "dup", "gitUrl": URL, "repo": URL, "branch": "main"}] if seeded else []
    monkeypatch.setattr(reg, "_load_registry_file", lambda: seed)
    monkeypatch.setattr(
        reg.official_catalog, "fetch_document", _catalog(reachable=True, apps=[BUILTIN_ROW])
    )

    with caplog.at_level(logging.WARNING):
        row, reason = reg._resolve_registry_row("dup")

    assert row is None, "a curated name must not resolve from the seed or another registry"
    assert "lists the requested app" in reason
    assert "no install source" in reason
    assert "could not be reached" not in reason
    assert "lists the app without install coordinates" in caplog.text
    assert "coordinate lookup is unavailable" not in caplog.text
    assert "dup" not in caplog.text


def test_the_no_source_refusal_is_still_a_catalog_unavailable(monkeypatch):
    """Every `except CatalogUnavailable` caller must keep refusing unchanged."""
    monkeypatch.setattr(
        reg.official_catalog, "fetch_document", _catalog(reachable=True, apps=[BUILTIN_ROW])
    )
    with pytest.raises(reg.official_catalog.CatalogNoInstallSource) as info:
        reg.official_catalog.inventory_for_install("dup")
    assert isinstance(info.value, reg.official_catalog.CatalogUnavailable)


def test_an_unreachable_catalog_keeps_the_unreachable_wording(monkeypatch, caplog, no_external):
    monkeypatch.setattr(reg, "_load_registry_file", lambda: [])
    monkeypatch.setattr(reg.official_catalog, "fetch_document", _catalog(reachable=False, apps=[]))

    with caplog.at_level(logging.WARNING):
        row, reason = reg._resolve_registry_row("dup")

    assert row is None
    assert "could not be reached" in reason
    assert "no install source" not in reason
    assert "coordinate lookup is unavailable" in caplog.text


def test_the_install_path_reports_the_no_source_reason(monkeypatch, no_external):
    monkeypatch.setattr(reg, "_load_registry_file", lambda: [])
    monkeypatch.setattr(
        reg.official_catalog, "fetch_document", _catalog(reachable=True, apps=[BUILTIN_ROW])
    )
    monkeypatch.setattr(reg, "get_app", lambda name: {})

    entry, error = reg._resolve_install_entry("dup")

    assert entry is None
    assert "no install source" in error
    assert "could not be reached" not in error
