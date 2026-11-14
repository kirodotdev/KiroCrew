"""Update state for apps installed from the marketplace (``market:<ref>@<n>``).

A refresh asks the listing API, through the ``MarketClient`` Protocol, for the
latest installable version of each recorded listing and keeps the answers in a
cache. ``GET /api/apps`` then stamps ``update`` on a market row from that cache
alone, the same field registry apps carry. The invariants: reading never calls
the client; a failed or offline check leaves no ``update`` (no badge, no error);
and a registry row with the same app name never answers for a market app.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.apps import market_update, official_catalog, update_status
from kiro_crew.apps.marketplace_install import MarketInstallError, ResolvedApp

REF = "acme/demo"
SHA = "a" * 64


class FakeClient:
    """Answers ``resolve_install(ref, None)`` from a table; anything else fails."""

    def __init__(self, latest: dict[str, Any]) -> None:
        self.latest = latest
        self.calls: list[tuple[str, int | None]] = []

    def resolve_install(self, ref: str, version: int | None) -> ResolvedApp:
        self.calls.append((ref, version))
        answer = self.latest[ref]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, ResolvedApp):
            return answer
        return ResolvedApp(
            ref=ref,
            version=answer,
            content_sha256=SHA,
            size=10,
            published_by="someone",
            app_version=f"1.{answer}.0",
        )

    def get_download(self, ref, version):  # pragma: no cover - never reached
        raise AssertionError("an update check must not download")

    def open_download(self, url):  # pragma: no cover - never reached
        raise AssertionError("an update check must not download")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setattr(
        market_update, "_cache_path", lambda: tmp_path / "market-updates.json"
    )
    _floor_monkeypatch.setattr(update_status, "_load_registry_file", lambda: [])
    _floor_monkeypatch.setattr(official_catalog, "cached_entries", lambda: ([], None))


def _row(installed_n: int = 2, name: str = "demo") -> dict[str, Any]:
    return {"name": name, "version": "1.0.0", "source": f"market:{REF}@{installed_n}"}


def _update(row: dict[str, Any]) -> dict[str, Any] | None:
    return update_status.attach_updates([row])[0].get("update")


class TestCheck:
    def test_a_newer_listing_version_is_available(self):
        client = FakeClient({REF: 3})
        market_update.refresh_market_updates(client, [_row(2)])
        assert client.calls == [(REF, None)]
        update = _update(_row(2))
        assert update is not None
        assert update["available"] is True
        assert update["latestVersion"] == "1.3.0"
        assert update["listingVersion"] == 3
        assert isinstance(update["checkedAt"], str)

    def test_the_same_version_is_not_available(self):
        market_update.refresh_market_updates(FakeClient({REF: 2}), [_row(2)])
        assert _update(_row(2))["available"] is False

    def test_the_single_row_route_answers_the_same(self):
        market_update.refresh_market_updates(FakeClient({REF: 5}), [_row(2)])
        detail = update_status.attach_update("demo", _row(2))
        assert detail["update"]["available"] is True

    def test_each_listing_is_asked_once(self):
        client = FakeClient({REF: 3})
        market_update.refresh_market_updates(client, [_row(1, "a"), _row(2, "b"), {"name": "x"}])
        assert client.calls == [(REF, None)]


class TestNoAnswerMeansNoBadge:
    def test_never_checked(self):
        assert _update(_row(2)) is None

    @pytest.mark.parametrize(
        "failure",
        [MarketInstallError("market_api_unreachable", "offline"), RuntimeError("boom")],
    )
    def test_a_failed_check_shows_nothing(self, failure):
        market_update.refresh_market_updates(FakeClient({REF: failure}), [_row(2)])
        assert _update(_row(2)) is None

    def test_a_failed_check_drops_an_earlier_answer(self):
        market_update.refresh_market_updates(FakeClient({REF: 3}), [_row(2)])
        market_update.refresh_market_updates(
            FakeClient({REF: MarketInstallError("market_api_unreachable", "offline")}), [_row(2)]
        )
        assert _update(_row(2)) is None

    def test_an_answer_for_another_listing_is_ignored(self):
        other = ResolvedApp(
            ref="acme/other", version=9, content_sha256=SHA, size=1, published_by="x"
        )
        market_update.refresh_market_updates(FakeClient({REF: other}), [_row(2)])
        assert _update(_row(2)) is None

    def test_an_unreadable_cache_shows_nothing(self, tmp_path):
        (tmp_path / "market-updates.json").write_text("{not json", encoding="utf-8")
        assert _update(_row(2)) is None

    def test_a_malformed_market_source_shows_nothing(self):
        market_update.refresh_market_updates(FakeClient({REF: 3}), [_row(2)])
        assert _update({"name": "demo", "version": "1.0.0", "source": "market:acme/demo"}) is None


class TestRegistryNeverAnswersForAMarketApp:
    def test_a_catalog_row_with_the_same_name_is_ignored(self, monkeypatch):
        monkeypatch.setattr(
            official_catalog,
            "cached_entries",
            lambda: ([{"name": "demo", "version": "9.0.0"}], 1.0),
        )
        assert _update(_row(2)) is None

    def test_the_catalog_still_answers_a_registry_app(self, monkeypatch):
        monkeypatch.setattr(
            official_catalog,
            "cached_entries",
            lambda: ([{"name": "demo", "version": "9.0.0"}], 1.0),
        )
        assert _update({"name": "demo", "version": "1.0.0"})["available"] is True
