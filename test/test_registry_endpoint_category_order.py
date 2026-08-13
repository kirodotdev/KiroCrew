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
    """Keep the test about the editorial half: the app list is stubbed."""

    async def _apps():
        return [{"name": "demo", "tags": ["git"]}]

    monkeypatch.setattr(routes, "list_registry", _apps)
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
