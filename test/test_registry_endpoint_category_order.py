"""The registry endpoint carries the published rail order, and never fails for it.

Presentation is an enhancement to a store that already works. So the contract
asserted here is not "the order is present" but "a broken order costs the order
and nothing else" -- the store still answers 200 with its apps.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.apps import routes


class _FakeRequest:
    """Minimal stand-in: handle_registry reads nothing off the request."""


async def _call() -> tuple[int, dict[str, Any]]:
    import json

    resp = await routes.handle_registry(_FakeRequest())  # type: ignore[arg-type]
    return resp.status, json.loads(resp.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _quiet_registry(monkeypatch):
    """Keep the test about the editorial half: the app list is stubbed.

    ``handle_registry`` prefers the published catalog and falls back to the seed,
    so BOTH sources are stubbed: an empty catalog lets the seed stub answer with
    the single demo row the assertions expect.
    """

    async def _apps():
        return [{"name": "demo", "tags": ["git"]}]

    async def _no_catalog():
        return []

    monkeypatch.setattr(routes, "list_registry", _apps)
    monkeypatch.setattr(routes, "list_catalog_apps", _no_catalog)
    monkeypatch.setattr(routes, "get_server_platform", lambda: {"os": "linux", "arch": "x86_64"})


@pytest.mark.asyncio
class TestCategoryOrderOnTheRegistryEndpoint:
    async def test_the_published_order_is_included(self, monkeypatch):
        monkeypatch.setattr(routes, "load_category_order", lambda: ["other", "developer-tools"])
        status, body = await _call()
        assert status == 200
        assert body["categoryOrder"] == ["other", "developer-tools"]

    async def test_an_empty_order_is_still_a_list_not_a_missing_key(self, monkeypatch):
        # The client narrows with `Array.isArray`, so a null would be discarded
        # anyway -- but an absent key would make the field's absence ambiguous
        # between "nothing published" and "an older server".
        monkeypatch.setattr(routes, "load_category_order", lambda: [])
        status, body = await _call()
        assert status == 200
        assert body["categoryOrder"] == []

    async def test_a_raising_loader_costs_the_order_not_the_store(self, monkeypatch):
        def boom():
            raise RuntimeError("editorial exploded")

        monkeypatch.setattr(routes, "load_category_order", boom)
        status, body = await _call()
        assert status == 200, "presentation must never take down the store"
        assert body["categoryOrder"] == []
        assert body["apps"], "the apps must still be served"

    async def test_the_apps_payload_is_unchanged_by_this_addition(self, monkeypatch):
        monkeypatch.setattr(routes, "load_category_order", lambda: ["other"])
        _, body = await _call()
        assert [a["name"] for a in body["apps"]] == ["demo"]
        assert body["serverPlatform"] == {"os": "linux", "arch": "x86_64"}


@pytest.mark.asyncio
class TestEditorialSectionsOnTheRegistryEndpoint:
    async def test_the_published_sections_are_included(self, monkeypatch):
        section = {"type": "spotlight", "appRefs": ["demo"], "title": "Picks"}
        monkeypatch.setattr(routes, "load_sections", lambda: [section])
        status, body = await _call()
        assert status == 200
        assert body["editorialSections"] == [section]

    async def test_no_sections_is_still_a_list(self, monkeypatch):
        monkeypatch.setattr(routes, "load_sections", lambda: [])
        _, body = await _call()
        assert body["editorialSections"] == []

    async def test_a_raising_loader_costs_the_layout_not_the_store(self, monkeypatch):
        def boom():
            raise RuntimeError("editorial exploded")

        monkeypatch.setattr(routes, "load_sections", boom)
        status, body = await _call()
        assert status == 200, "presentation must never take down the store"
        assert body["editorialSections"] == []
        assert body["apps"], "the apps must still be served"

    async def test_one_failing_reader_does_not_take_the_other(self, monkeypatch):
        # The two projections are independent: a broken layout must not cost the
        # rail its published order, and vice versa.
        def boom():
            raise RuntimeError("sections exploded")

        monkeypatch.setattr(routes, "load_sections", boom)
        monkeypatch.setattr(routes, "load_category_order", lambda: ["other"])
        _, body = await _call()
        assert body["categoryOrder"] == ["other"]
        assert body["editorialSections"] == []
