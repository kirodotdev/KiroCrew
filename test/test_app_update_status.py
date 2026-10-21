"""Behaviour coverage for the cached update status an installed app can read.

``GET /api/apps`` and ``GET /api/apps/{name}`` publish ``update`` on each
installed row, answered from caches alone. The invariants these tests hold:
asking costs no network call; an unconsulted cache stays distinguishable from a
confirmed "nothing newer"; a name the catalog or the seed owns is never
answered by an owner-configured registry; and a version is only repeated when
it is a version, not a sentence.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps import official_catalog, routes, update_status

# A fixed instant, so ``checkedAt`` is asserted against a value this test set
# rather than against whatever the clock said while it ran.
FIXED_MTIME = 1_700_000_000.0
FIXED_ISO = "2023-11-14T22:13:20+00:00"


@pytest.fixture(autouse=True)
def _no_seed(_floor_monkeypatch):
    """The bundled seed names nothing unless a test says otherwise.

    The real seed ships named apps, and every test below wants to decide which
    names are claimed. Patched through the floor's own instance, so a test that
    pins its own seed with ``monkeypatch`` still wins, and undoing that does not
    undo this.
    """
    _floor_monkeypatch.setattr(update_status, "_load_registry_file", lambda: [])


@pytest.fixture(name="no_catalog")
def no_catalog_fixture(monkeypatch):
    """The published catalog knows nothing, so external caches are reached."""
    monkeypatch.setattr(official_catalog, "cached_entries", lambda: ([], None))


def _catalog(monkeypatch, entries: list[dict[str, Any]], mtime: float | None = FIXED_MTIME):
    monkeypatch.setattr(official_catalog, "cached_entries", lambda: (entries, mtime))


def _external(monkeypatch, entries: list[dict[str, Any]], *, mtime: float | None = FIXED_MTIME):
    """Point the one configured registry's cache at *entries*."""
    monkeypatch.setattr(update_status, "_effective_registries", lambda: [object()])
    monkeypatch.setattr(update_status, "_external_registry_cache_identity", lambda reg: "reg-a")
    monkeypatch.setattr(
        update_status, "_read_external_registry_cache", lambda name, ignore_ttl=False: entries
    )
    monkeypatch.setattr(update_status, "_cache_mtime", lambda path: mtime)


def _update(name: str, installed: str) -> dict[str, Any] | None:
    """What the API would put on an installed row: the ``update`` field or None."""
    return update_status.attach_update(name, {"name": name, "version": installed}).get("update")


class TestNothingKnown:
    def test_no_cache_names_the_app(self, monkeypatch, no_catalog):
        _external(monkeypatch, [])
        assert _update("demo", "1.0.0") is None

    def test_a_cache_naming_another_app(self, monkeypatch, no_catalog):
        _external(monkeypatch, [{"name": "other", "version": "9.9.9"}])
        assert _update("demo", "1.0.0") is None


class TestPublishedCatalog:
    def test_a_newer_catalog_version_is_available(self, monkeypatch):
        _catalog(monkeypatch, [{"name": "demo", "version": "1.1.0"}])
        assert _update("demo", "1.0.0") == {
            "available": True,
            "latestVersion": "1.1.0",
            "checkedAt": FIXED_ISO,
        }

    def test_the_same_version_is_not_available(self, monkeypatch):
        _catalog(monkeypatch, [{"name": "demo", "version": "1.0.0"}])
        assert _update("demo", "1.0.0") == {
            "available": False,
            "latestVersion": "1.0.0",
            "checkedAt": FIXED_ISO,
        }

    def test_an_older_catalog_version_is_not_available(self, monkeypatch):
        _catalog(monkeypatch, [{"name": "demo", "version": "0.9.0"}])
        update = _update("demo", "1.0.0")
        assert update is not None
        assert update["available"] is False


class TestPrecedence:
    """App trust is keyed by NAME: a registry never answers for a claimed name."""

    def test_the_catalog_wins_over_a_registry(self, monkeypatch):
        _catalog(monkeypatch, [{"name": "demo", "version": "1.0.0"}])
        _external(monkeypatch, [{"name": "demo", "version": "9.9.9"}])
        assert update_status.published_index()["demo"] == ("1.0.0", FIXED_MTIME)

    def test_a_catalog_claim_without_a_version_still_blocks_a_registry(self, monkeypatch):
        _catalog(monkeypatch, [{"name": "demo"}])
        _external(monkeypatch, [{"name": "demo", "version": "9.9.9"}])
        assert _update("demo", "1.0.0") is None

    def test_a_seed_name_blocks_a_registry_when_the_catalog_cache_is_cold(
        self, monkeypatch, no_catalog
    ):
        """The seed ships in the wheel, so it holds when the catalog cache does not."""
        monkeypatch.setattr(update_status, "_load_registry_file", lambda: [{"name": "demo"}])
        _external(monkeypatch, [{"name": "demo", "version": "9.9.9"}])
        assert _update("demo", "1.0.0") is None

    def test_a_seed_row_never_supplies_a_version_itself(self, monkeypatch, no_catalog):
        """What the wheel shipped is not what is published."""
        monkeypatch.setattr(
            update_status, "_load_registry_file", lambda: [{"name": "demo", "version": "9.9.9"}]
        )
        _external(monkeypatch, [])
        assert _update("demo", "1.0.0") is None

    def test_an_external_app_nothing_else_names_is_answered(self, monkeypatch, no_catalog):
        _external(monkeypatch, [{"name": "demo", "version": "0.3.1"}])
        assert _update("demo", "0.3.0") == {
            "available": True,
            "latestVersion": "0.3.1",
            "checkedAt": FIXED_ISO,
        }


class TestPublishableVersion:
    """A published version is text a registry author controls; it is repeated only as a version."""

    @pytest.mark.parametrize(
        "value", ["1.2.3", "0.0.1", "1.2.3-beta.1", "1.2.3+build.7", "1.2.3-rc.1+sha.abc"]
    )
    def test_a_semver_is_kept(self, value):
        assert update_status.publishable_version(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "9.0.0-SECURITY ALERT: reinstall now",
            "1.0.0 <b>",
            "latest",
            "1.2",
            "v1.2.3",
            "1.2.3-" + "x" * 33,
            "",
            None,
            7,
            ["1.0.0"],
        ],
    )
    def test_anything_else_is_dropped(self, value):
        assert update_status.publishable_version(value) == ""

    def test_a_spoofing_version_produces_no_update_field(self, monkeypatch, no_catalog):
        _external(monkeypatch, [{"name": "demo", "version": "9.0.0-SECURITY ALERT: act now"}])
        assert _update("demo", "1.0.0") is None


class TestConservativeComparison:
    def test_an_app_installed_at_no_version_is_not_told_an_update_is_available(
        self, monkeypatch, no_catalog
    ):
        _external(monkeypatch, [{"name": "demo", "version": "2.0.0"}])
        update = _update("demo", "")
        assert update is not None
        assert update["available"] is False
        assert update["latestVersion"] == "2.0.0"

    def test_a_readable_cache_with_no_mtime_still_reports_the_version(
        self, monkeypatch, no_catalog
    ):
        _external(monkeypatch, [{"name": "demo", "version": "0.4.0"}], mtime=None)
        assert _update("demo", "0.3.0") == {
            "available": True,
            "latestVersion": "0.4.0",
            "checkedAt": None,
        }


class TestNoNetwork:
    """The whole point of reading caches: no registry is reached on this path."""

    def test_nothing_is_fetched(self, monkeypatch, tmp_path):
        def _refuse(*a, **k):
            raise AssertionError("the update status must not reach the network")

        monkeypatch.setattr(official_catalog, "fetch_document", _refuse)
        monkeypatch.setattr(official_catalog, "fetch_inventory_entries", _refuse)
        monkeypatch.setattr(official_catalog, "load_official_catalog", _refuse)
        monkeypatch.setattr(official_catalog, "inventory_for_install", _refuse)
        # A real cache file, so the catalog half runs its own reader rather than a stub.
        cache = tmp_path / "official-catalog.json"
        cache.write_text(
            json.dumps({"schemaVersion": 1, "apps": [{"name": "demo", "version": "2.0.0"}]}),
            encoding="utf-8",
        )
        os.utime(cache, (FIXED_MTIME, FIXED_MTIME))
        monkeypatch.setattr(official_catalog, "_cache_path", lambda: cache)
        monkeypatch.setattr(update_status, "_effective_registries", lambda: [])

        assert _update("demo", "1.0.0") == {
            "available": True,
            "latestVersion": "2.0.0",
            "checkedAt": FIXED_ISO,
        }


class TestCachedEntries:
    """``official_catalog.cached_entries`` reads the cache and only the cache."""

    @pytest.fixture(name="cache_file")
    def cache_file_fixture(self, tmp_path, monkeypatch):
        path = tmp_path / "official-catalog.json"
        monkeypatch.setattr(official_catalog, "_cache_path", lambda: path)
        return path

    def test_a_cold_cache_reads_empty_with_no_timestamp(self, cache_file):
        assert official_catalog.cached_entries() == ([], None)

    def test_a_remembered_failure_reads_empty(self, cache_file):
        cache_file.write_text(json.dumps({official_catalog._FAILED_KEY: 1.0}), encoding="utf-8")
        assert official_catalog.cached_entries() == ([], None)

    def test_corrupt_json_reads_empty(self, cache_file):
        cache_file.write_text("{not json", encoding="utf-8")
        assert official_catalog.cached_entries() == ([], None)

    def test_entries_come_back_with_the_cache_mtime(self, cache_file):
        cache_file.write_text(
            json.dumps({"schemaVersion": 1, "apps": [{"name": "demo", "version": "1.0.0"}]}),
            encoding="utf-8",
        )
        os.utime(cache_file, (FIXED_MTIME, FIXED_MTIME))
        entries, mtime = official_catalog.cached_entries()
        assert [e["name"] for e in entries] == ["demo"]
        assert mtime == FIXED_MTIME

    def test_malformed_members_are_dropped(self, cache_file):
        cache_file.write_text(
            json.dumps({"apps": [{"name": "ok"}, {"noname": 1}, "nope", 7, None]}),
            encoding="utf-8",
        )
        entries, _ = official_catalog.cached_entries()
        assert [e["name"] for e in entries] == ["ok"]

    def test_a_stale_cache_still_answers(self, cache_file):
        """Past ``CACHE_TTL`` the version is still reported, with its real age."""
        cache_file.write_text(
            json.dumps({"apps": [{"name": "demo", "version": "1.0.0"}]}), encoding="utf-8"
        )
        ancient = FIXED_MTIME - official_catalog.CACHE_TTL * 10
        os.utime(cache_file, (ancient, ancient))
        entries, mtime = official_catalog.cached_entries()
        assert [e["name"] for e in entries] == ["demo"]
        assert mtime == ancient


class TestAttach:
    def test_the_field_is_omitted_when_nothing_is_known(self, monkeypatch):
        monkeypatch.setattr(update_status, "published_index", lambda: {})
        detail = update_status.attach_update("demo", {"name": "demo", "version": "1.0.0"})
        assert "update" not in detail

    def test_the_rest_of_the_row_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(update_status, "published_index", lambda: {})
        row = {"name": "demo", "version": "1.0.0", "enabled": True, "origin": "builtin"}
        assert update_status.attach_update("demo", dict(row)) == row

    def test_the_rows_own_version_is_what_gets_compared(self, monkeypatch):
        monkeypatch.setattr(
            update_status, "published_index", lambda: {"demo": ("1.1.0", FIXED_MTIME)}
        )
        assert _update("demo", "1.0.0")["available"] is True  # type: ignore[index]
        assert _update("demo", "1.1.0")["available"] is False  # type: ignore[index]

    def test_every_row_is_stamped_from_one_cache_read(self, monkeypatch):
        reads: list[int] = []

        def _entries():
            reads.append(1)
            return (
                [{"name": "alpha", "version": "2.0.0"}, {"name": "beta", "version": "1.0.0"}],
                FIXED_MTIME,
            )

        monkeypatch.setattr(official_catalog, "cached_entries", _entries)
        monkeypatch.setattr(update_status, "_effective_registries", lambda: [])
        rows = [
            {"name": "alpha", "version": "1.0.0"},
            {"name": "beta", "version": "1.0.0"},
            {"name": "gamma", "version": "1.0.0"},
        ]
        update_status.attach_updates(rows)
        assert rows[0]["update"]["available"] is True
        assert rows[1]["update"]["available"] is False
        assert "update" not in rows[2]
        assert len(reads) == 1

    def test_a_row_without_a_name_is_left_untouched(self, monkeypatch, no_catalog):
        monkeypatch.setattr(update_status, "_effective_registries", lambda: [])
        rows = [{"version": "1.0.0"}, {"name": "", "version": "1.0.0"}]
        assert update_status.attach_updates([dict(r) for r in rows]) == rows


class TestBothRoutesAnswerTheSame:
    """A routed page and a side-panel tab read their row from different endpoints.

    Because an absent ``update`` MEANS unknown, a gap on one route would fail
    silently: the app would claim nothing is known about itself there.
    """

    @pytest.fixture(autouse=True)
    def _one_app(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(
            routes, "list_apps", lambda: [{"name": "demo", "version": "1.0.0"}]
        )
        _floor_monkeypatch.setattr(routes, "_stamp_installed_trust_repository", lambda app: app)
        _floor_monkeypatch.setattr(
            update_status, "published_index", lambda: {"demo": ("1.1.0", FIXED_MTIME)}
        )

    def test_the_list_carries_it(self):
        assert routes._listed_apps_with_trust()[0]["update"]["available"] is True

    def test_the_list_and_the_detail_agree(self):
        from_list = routes._listed_apps_with_trust()[0]["update"]
        from_detail = routes._installed_app_detail("demo", {"name": "demo", "version": "1.0.0"})
        assert from_list == from_detail["update"]

    @pytest.mark.asyncio
    async def test_the_detail_route_serves_it(self, monkeypatch):
        monkeypatch.setattr(routes, "get_app", lambda name: {"name": name, "version": "1.0.0"})
        request = make_mocked_request("GET", "/api/apps/demo", match_info={"name": "demo"})
        response = await routes.handle_get_app(request)
        assert response.status == 200
        assert json.loads(response.text)["update"]["latestVersion"] == "1.1.0"

    @pytest.mark.asyncio
    async def test_an_app_that_is_not_installed_still_answers_404(self, monkeypatch):
        monkeypatch.setattr(routes, "get_app", lambda name: None)
        request = make_mocked_request("GET", "/api/apps/nope", match_info={"name": "nope"})
        response = await routes.handle_get_app(request)
        assert response.status == 404
