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
    # The ranking is a fourth published document on this handler, and an
    # unstubbed loader would reach for the network on a cold cache. ``None`` is
    # the no-ranking answer, which leaves the rows exactly as this file expects.
    monkeypatch.setattr(routes, "load_ranking_document", lambda: None)


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
class TestInstallRankingOnTheRegistryEndpoint:
    """The ranking is a fourth published document, under the same contract.

    It is presentation like the other two, so a failure costs the ranking and
    nothing else: the store still answers 200 with its apps.
    """

    @staticmethod
    def _document(counts: dict[str, Any]) -> Any:
        from datetime import datetime, timedelta, timezone

        from kiro_crew.apps.catalog_ranking import _parse_document

        # Derived from the clock, never a literal date. The loader refuses a
        # document older than MAX_AGE_WINDOWS x windowDays, so a hardcoded
        # timestamp would pass today and start failing once that many days of
        # real time elapsed -- a test that breaks on an unrelated PR, with no
        # code change.
        generated_at = datetime.now(timezone.utc) - timedelta(hours=1)
        return _parse_document(
            {
                "schemaVersion": 1,
                "generatedAt": generated_at.isoformat().replace("+00:00", "Z"),
                "windowDays": 30,
                "rankings": [
                    {"appSlug": slug, "cumulativeInstalls": n, "windowedInstalls": n}
                    for slug, n in counts.items()
                ],
            }
        )

    async def test_a_ranked_app_is_served_with_its_install_count(self, monkeypatch):
        monkeypatch.setattr(routes, "load_ranking_document", lambda: self._document({"demo": 42}))
        status, body = await _call()
        assert status == 200
        assert body["apps"][0]["installs"] == 42

    async def test_an_unranked_app_is_served_without_an_install_count(self, monkeypatch):
        monkeypatch.setattr(routes, "load_ranking_document", lambda: self._document({"other": 42}))
        status, body = await _call()
        assert status == 200
        assert "installs" not in body["apps"][0]

    async def test_a_withheld_count_is_served_as_no_key_not_as_a_zero(self, monkeypatch):
        # A count hidden under the document's small-count floor arrives as
        # null. Serving it as 0 -- or as any bound -- would hand back the very
        # figure the floor exists to suppress, so the wire format must not be
        # able to express it at all.
        monkeypatch.setattr(routes, "load_ranking_document", lambda: self._document({"demo": None}))
        status, body = await _call()
        assert status == 200
        assert "installs" not in body["apps"][0]
        assert body["apps"][0].get("installs") != 0

    async def test_no_ranking_document_leaves_the_rows_alone(self, monkeypatch):
        # The producer is an infrastructure follow-up, so "no document" is the
        # ordinary case today and must be entirely harmless.
        monkeypatch.setattr(routes, "load_ranking_document", lambda: None)
        status, body = await _call()
        assert status == 200
        assert "installs" not in body["apps"][0]

    async def test_a_raising_loader_costs_the_ranking_not_the_store(self, monkeypatch):
        def boom():
            raise RuntimeError("ranking exploded")

        monkeypatch.setattr(routes, "load_ranking_document", boom)
        status, body = await _call()
        assert status == 200, "presentation must never take down the store"
        assert body["apps"], "the apps must still be served"
        assert "installs" not in body["apps"][0]

    async def test_a_malformed_row_is_skipped_rather_than_crashing_the_store(self, monkeypatch):
        # The rows come from a published catalog fetched over the network, so
        # their SHAPE is as untrusted as their content. A row that is not a
        # dict, or whose name is not a string, must be stepped over: raising
        # here would turn a bad catalog row into a 500 on the whole store.
        async def _ragged():
            return ["not-a-dict", {"name": 7}, {"name": "demo"}]

        monkeypatch.setattr(routes, "list_registry", _ragged)
        monkeypatch.setattr(routes, "load_ranking_document", lambda: self._document({"demo": 9}))
        status, body = await _call()
        assert status == 200
        assert body["apps"][0] == "not-a-dict", "the non-dict row is passed through untouched"
        assert "installs" not in body["apps"][1], "a non-string name resolves no rank"
        assert body["apps"][2]["installs"] == 9, "the well-formed row is still stamped"


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
