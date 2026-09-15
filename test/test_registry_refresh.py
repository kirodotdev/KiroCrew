"""The store's manual refresh: ``POST /api/app-store/refresh``.

Three contracts, asserted separately because they fail differently:

1. ``forget_cache()`` clears BOTH things the cache file can hold -- a stale
   document (``CACHE_TTL`` not yet expired) and a ``_fetchFailedAt`` failure
   sentinel (``FAILURE_TTL`` back-off). The sentinel case is the one that
   motivated the feature: a failed fetch overwrites the good cache and the
   store silently degrades to the seed listing with nothing the user can do
   about it from the UI.

2. The refresh handler drops all three published-document caches and never
   fetches: the follow-up ``GET /api/apps/registry`` pays the fetch on the
   exact same code path as a cold start, so refresh cannot behave differently
   from the load it is trying to repair.

3. Refresh is a POST, never a side effect of the listing GET. Deleting caches
   and triggering outbound fetches is a state change, and a state-changing GET
   is reachable by cross-site top-level navigation with a valid SameSite=Lax
   cookie -- exactly the request the CSRF middleware never sees. So the plain
   GET must stay read-only.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from kiro_crew.apps import official_catalog as oc
from kiro_crew.apps import official_category_order as oco
from kiro_crew.apps import official_editorial as oe
from kiro_crew.apps import routes

# ---------------------------------------------------------------------------
# forget_cache() on each document module
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mod", [oc, oco, oe], ids=["catalog", "category-order", "editorial"])
class TestForgetCache:
    def test_drops_a_cached_document(self, mod, monkeypatch, tmp_path):
        path = tmp_path / "doc.json"
        monkeypatch.setattr(mod, "_cache_path", lambda: path)
        path.write_text(json.dumps({"schemaVersion": 1, "apps": []}), encoding="utf-8")
        mod.forget_cache()
        assert not path.exists()

    def test_drops_a_failure_sentinel(self, mod, monkeypatch, tmp_path):
        # The case the feature exists for: the back-off memory must not survive
        # an explicit refresh, or the refresh would answer from the seed for up
        # to FAILURE_TTL and look like it did nothing.
        path = tmp_path / "doc.json"
        monkeypatch.setattr(mod, "_cache_path", lambda: path)
        path.write_text(json.dumps({mod._FAILED_KEY: time.time()}), encoding="utf-8")
        mod.forget_cache()
        assert not path.exists()

    def test_missing_cache_is_a_no_op(self, mod, monkeypatch, tmp_path):
        monkeypatch.setattr(mod, "_cache_path", lambda: tmp_path / "absent.json")
        mod.forget_cache()  # must not raise

    def test_an_unlink_error_never_raises(self, mod, monkeypatch, tmp_path):
        # Same degrade-don't-500 contract as every other disk touch in these
        # modules: a refresh that cannot delete answers like a plain load.
        class _Boom:
            def unlink(self, missing_ok=False):
                raise OSError("locked")

        monkeypatch.setattr(mod, "_cache_path", lambda: _Boom())
        mod.forget_cache()  # must not raise


# ---------------------------------------------------------------------------
# POST /api/app-store/refresh, and the GET staying read-only
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal stand-in: neither handler reads anything off the request."""


@pytest.fixture()
def _stubbed_lists(monkeypatch):
    """Keep the GET test about read-only-ness: every list source is stubbed."""

    async def _apps():
        return [{"name": "demo", "tags": ["git"]}]

    async def _no_catalog():
        return []

    monkeypatch.setattr(routes, "list_registry", _apps)
    monkeypatch.setattr(routes, "list_catalog_apps", _no_catalog)
    monkeypatch.setattr(routes, "load_category_order", lambda: [])
    monkeypatch.setattr(routes, "load_sections", lambda: [])
    monkeypatch.setattr(routes, "get_server_platform", lambda: {"os": "linux", "arch": "x86_64"})


@pytest.fixture()
def _forget_counter(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(routes.official_catalog, "forget_cache", lambda: calls.append("catalog"))
    monkeypatch.setattr(routes, "forget_category_order_cache", lambda: calls.append("order"))
    monkeypatch.setattr(routes, "forget_editorial_cache", lambda: calls.append("editorial"))
    return calls


def _body(resp: Any) -> dict[str, Any]:
    return json.loads(resp.body.decode("utf-8"))


@pytest.mark.asyncio
class TestRegistryRefreshEndpoint:
    async def test_refresh_drops_all_three_document_caches(self, _forget_counter):
        resp = await routes.handle_registry_refresh(_FakeRequest())  # type: ignore[arg-type]
        assert resp.status == 200
        assert _body(resp) == {"ok": True}
        assert sorted(_forget_counter) == ["catalog", "editorial", "order"]

    async def test_the_listing_get_never_drops_a_cache(self, _stubbed_lists, _forget_counter):
        # The read path must stay read-only: a state-changing GET would be
        # reachable by cross-site navigation, behind the CSRF middleware's back.
        resp = await routes.handle_registry(_FakeRequest())  # type: ignore[arg-type]
        assert resp.status == 200
        assert _forget_counter == []
