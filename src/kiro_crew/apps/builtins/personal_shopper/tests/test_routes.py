"""Tests for the Personal Shopper HTTP routes.

The load-bearing ones here pin the boundary between a CLIENT error and a SERVER
error. Every field these handlers read used to go straight into a string or int
operation, so a wrong TYPE — not a wrong value — raised inside the handler and
surfaced as a 500. A 500 tells a caller "the server is broken" and is what gets
paged on; the correct answer to ``{"text": 1}`` is a 400 naming the offending
field, which is what these assert.

They also pin that every error body carries a machine-readable ``code``: the
dashboard renders ``error`` verbatim into a localized UI, so prose alone is
untranslatable by construction.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.personal_shopper.backend import routes as routes_mod
from kiro_crew.apps.builtins.personal_shopper.backend.store import PreferenceStore

_PREFIX = "/api/apps/personal-shopper"


class RoutesTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        # Registered first so it runs last: the store's connection must close
        # before the directory holding its database is removed.
        self.addCleanup(self._rmtree)

        self._store = PreferenceStore(db_path=Path(self._tmp) / "preferences.db")
        self.addCleanup(self._store.close)

        # Bypass the singleton so the test never touches the real data home, and
        # so each test gets an isolated database.
        patcher = mock.patch.object(
            routes_mod, "_get_store", self._fake_get_store
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        # The sites handlers do NOT go through the store: they resolve
        # ``app_data_dir`` themselves, so patching the store alone left them
        # writing sites.json into the OPERATOR'S real ~/.kiro/crew. Redirect the
        # path itself -- a test must not have side effects outside its tmpdir.
        sites_file = Path(self._tmp) / "sites.json"
        sites_patcher = mock.patch.object(
            routes_mod, "_sites_path", lambda: sites_file
        )
        sites_patcher.start()
        self.addCleanup(sites_patcher.stop)

        enabled = mock.patch.object(
            routes_mod, "is_app_enabled", lambda _name: True
        )
        enabled.start()
        self.addCleanup(enabled.stop)

        app = web.Application()
        routes_mod.register_routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    def _rmtree(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def _fake_get_store(self) -> PreferenceStore:
        return self._store


class TestBodyShapeIsAClientError(RoutesTestCase):
    async def test_a_non_object_body_is_a_400_not_a_500(self) -> None:
        """A JSON array parses fine but has no ``.get``.

        Without the object check every handler raises ``AttributeError`` and the
        client sees a 500 for what is purely a malformed request.
        """
        resp = await self.client.post(f"{_PREFIX}/preferences", json=[1, 2, 3])
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["code"], "body_not_object")

    async def test_a_non_string_text_is_a_400_not_a_500(self) -> None:
        """``{"text": 1}`` must never reach ``.strip()``."""
        resp = await self.client.post(f"{_PREFIX}/preferences", json={"text": 1})
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["code"], "invalid_field_type")

    async def test_non_string_tag_elements_are_rejected_at_the_boundary(self) -> None:
        """A non-string tag would otherwise fail deep inside sqlite/FTS."""
        resp = await self.client.post(
            f"{_PREFIX}/preferences", json={"text": "shoe size US 10", "tags": [1]}
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["code"], "invalid_field_type")

    async def test_a_missing_text_names_the_field(self) -> None:
        resp = await self.client.post(f"{_PREFIX}/preferences", json={})
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["code"], "missing_required_field")

    async def test_a_valid_preference_is_created(self) -> None:
        resp = await self.client.post(
            f"{_PREFIX}/preferences", json={"text": "shoe size US 10", "tags": ["body"]}
        )
        self.assertEqual(resp.status, 201)
        self.assertTrue((await resp.json())["id"])


class TestSearchArgumentValidation(RoutesTestCase):
    async def test_a_boolean_top_k_is_rejected(self) -> None:
        """``isinstance(True, int)`` is True in Python.

        Without the explicit bool exclusion ``top_k=true`` silently becomes a
        LIMIT of 1 and the caller gets one result while believing it asked for a
        page of them.
        """
        resp = await self.client.post(
            f"{_PREFIX}/preferences/search", json={"query": "gift", "top_k": True}
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["code"], "invalid_field_type")

    async def test_an_out_of_range_top_k_is_rejected(self) -> None:
        resp = await self.client.post(
            f"{_PREFIX}/preferences/search", json={"query": "gift", "top_k": 0}
        )
        self.assertEqual(resp.status, 400)

    async def test_search_reports_its_ranking_mode(self) -> None:
        """Callers must be able to tell a cosine score from a keyword rank."""
        await self.client.post(f"{_PREFIX}/preferences", json={"text": "likes vinyl"})
        resp = await self.client.post(
            f"{_PREFIX}/preferences/search", json={"query": "vinyl"}
        )
        self.assertEqual(resp.status, 200)
        self.assertIn("semantic", await resp.json())


class TestQueryParamValidation(RoutesTestCase):
    async def test_a_non_numeric_limit_is_a_400_not_a_500(self) -> None:
        """``int("abc")`` raises: an unvalidated query param is still input."""
        resp = await self.client.get(f"{_PREFIX}/history?limit=abc")
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["code"], "invalid_field_type")

    async def test_an_out_of_range_limit_is_rejected(self) -> None:
        resp = await self.client.get(f"{_PREFIX}/history?limit=100000")
        self.assertEqual(resp.status, 400)


class TestFeedbackIsConstrained(RoutesTestCase):
    async def test_an_unknown_feedback_value_is_rejected(self) -> None:
        """Only the three states the UI can render may be persisted.

        Anything else would round-trip to the badge and display as a raw English
        string in every locale.
        """
        created = await self.client.post(
            f"{_PREFIX}/history", json={"problem": "neck aches"}
        )
        history_id = (await created.json())["id"]
        resp = await self.client.put(
            f"{_PREFIX}/history/{history_id}/feedback",
            json={"product": "Monitor arm", "feedback": "maybe"},
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["code"], "invalid_field_type")

    async def test_a_known_feedback_value_is_accepted(self) -> None:
        created = await self.client.post(
            f"{_PREFIX}/history", json={"problem": "neck aches"}
        )
        history_id = (await created.json())["id"]
        resp = await self.client.put(
            f"{_PREFIX}/history/{history_id}/feedback",
            json={"product": "Monitor arm", "feedback": "purchased"},
        )
        self.assertEqual(resp.status, 200)


class TestSitesShapeValidation(RoutesTestCase):
    async def test_sites_must_be_an_array_of_objects(self) -> None:
        resp = await self.client.put(f"{_PREFIX}/sites", json={"sites": ["amazon"]})
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["code"], "invalid_field_type")

    async def test_a_missing_sites_key_is_rejected(self) -> None:
        resp = await self.client.put(f"{_PREFIX}/sites", json={})
        self.assertEqual(resp.status, 400)


class TestReembedIsReachable(RoutesTestCase):
    async def test_reembed_has_a_route(self) -> None:
        """The store drops vectors whenever no model is serving.

        That is the normal state on a fresh install, so without a reachable
        rebuild path those entries keep scoring 0 even after the model lands and
        semantic search stays permanently dormant for them.
        """
        resp = await self.client.post(f"{_PREFIX}/preferences/reembed")
        self.assertEqual(resp.status, 200)
        self.assertIn("reembedded", await resp.json())


class TestDisabledAppIsRefused(unittest.IsolatedAsyncioTestCase):
    async def test_every_route_is_gated_on_the_app_being_enabled(self) -> None:
        with mock.patch.object(routes_mod, "is_app_enabled", lambda _n: False):
            app = web.Application()
            routes_mod.register_routes(app)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get(f"{_PREFIX}/preferences")
                self.assertEqual(resp.status, 403)
                self.assertEqual((await resp.json())["code"], "app_disabled")


class TestNestedRecordFieldsAreValidated(RoutesTestCase):
    """The outer "is it a dict" check is not enough.

    An object-valued ``name`` is itself a dict, so it passed the outer check, got
    persisted, and came back to React -- which throws when handed an object as a
    child. That takes down the whole app page on every later visit, because the
    bad value is stored.
    """

    async def test_an_object_valued_product_name_is_rejected(self) -> None:
        resp = await self.client.post(
            f"{_PREFIX}/history",
            json={"problem": "gift", "products": [{"name": {"nested": "object"}}]},
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["code"], "invalid_field_type")

    async def test_a_non_numeric_product_price_is_rejected(self) -> None:
        """``price`` is formatted through ``fmtCurrency``, so it must be a number."""
        resp = await self.client.post(
            f"{_PREFIX}/history",
            json={"problem": "gift", "products": [{"name": "Book", "price": "forty"}]},
        )
        self.assertEqual(resp.status, 400)

    async def test_a_boolean_product_price_is_rejected(self) -> None:
        """``bool`` is a subclass of ``int``: ``price: true`` would format as 1."""
        resp = await self.client.post(
            f"{_PREFIX}/history",
            json={"problem": "gift", "products": [{"name": "Book", "price": True}]},
        )
        self.assertEqual(resp.status, 400)

    async def test_a_product_without_a_name_is_rejected(self) -> None:
        resp = await self.client.post(
            f"{_PREFIX}/history", json={"problem": "gift", "products": [{"price": 10}]}
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["code"], "missing_required_field")

    async def test_a_well_formed_product_is_accepted(self) -> None:
        resp = await self.client.post(
            f"{_PREFIX}/history",
            json={
                "problem": "gift",
                "products": [{"name": "The Eras Tour Book", "price": 40.0}],
            },
        )
        self.assertEqual(resp.status, 201)

    async def test_an_object_valued_site_name_is_rejected(self) -> None:
        resp = await self.client.put(
            f"{_PREFIX}/sites",
            json={"sites": [{"id": "a", "name": {"x": 1}, "url": "store.example.com"}]},
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["code"], "invalid_field_type")

    async def test_a_non_boolean_enabled_flag_is_rejected(self) -> None:
        resp = await self.client.put(
            f"{_PREFIX}/sites",
            json={
                "sites": [
                    {
                        "id": "a",
                        "name": "Example Store",
                        "url": "store.example.com",
                        "enabled": "yes",
                    }
                ]
            },
        )
        self.assertEqual(resp.status, 400)

    async def test_a_site_missing_its_id_is_rejected(self) -> None:
        resp = await self.client.put(
            f"{_PREFIX}/sites",
            json={"sites": [{"name": "Example Store", "url": "store.example.com"}]},
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["code"], "missing_required_field")

    async def test_a_well_formed_site_is_accepted(self) -> None:
        resp = await self.client.put(
            f"{_PREFIX}/sites",
            json={
                "sites": [
                    {
                        "id": "abc123",
                        "name": "Example Store",
                        "url": "store.example.com",
                        "enabled": True,
                        "loggedIn": False,
                    }
                ]
            },
        )
        self.assertEqual(resp.status, 200)


class TestTheSuiteHasNoSideEffects(RoutesTestCase):
    """A test must not write outside its own temporary directory.

    This is not hypothetical: the sites handlers resolve ``app_data_dir``
    themselves rather than going through the store, so patching the store alone
    left ``PUT /sites`` writing sites.json into the OPERATOR'S real
    ``~/.kiro/crew`` every time the suite ran — silently replacing whatever store
    configuration they had. The fixture now redirects ``_sites_path``; this test
    fails if that ever stops being true.
    """

    async def test_writing_sites_stays_inside_the_tmpdir(self) -> None:
        resp = await self.client.put(
            f"{_PREFIX}/sites",
            json={
                "sites": [
                    {
                        "id": "abc123",
                        "name": "Example Store",
                        "url": "store.example.com",
                        "enabled": True,
                        "loggedIn": False,
                    }
                ]
            },
        )
        self.assertEqual(resp.status, 200)

        written = routes_mod._sites_path()
        self.assertTrue(written.exists(), "the write did not land at all")
        self.assertTrue(
            str(written).startswith(self._tmp),
            f"sites.json escaped the tmpdir and landed at {written}",
        )

    async def test_the_real_data_home_is_never_resolved(self) -> None:
        """The patched path must not be the operator's home under any spelling."""
        resolved = str(routes_mod._sites_path().resolve())
        self.assertNotIn(".kiro/crew/apps", resolved)
        self.assertNotIn(".kirocrew/apps", resolved)


if __name__ == "__main__":
    unittest.main()
