"""Tests for the editorial consumer.

The shape mirrors ``test_official_catalog.py``: every refusal path is asserted
with the DEFAULT as the expected answer, because "degrade to the built-in order"
is the promise this module makes and an empty list is how it keeps it.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from kiro_crew.apps import official_editorial as oe


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Point the cache at a scratch dir so tests never read the real one."""
    monkeypatch.setattr(oe, "_cache_path", lambda: tmp_path / "editorial.json")
    return tmp_path


def _doc(categories: Any, version: int | Any = 1) -> dict[str, Any]:
    return {"schemaVersion": version, "categories": categories}


def _cat(cid: str, order: int) -> dict[str, Any]:
    # `label` is present because the live document carries it; these tests assert
    # it is IGNORED rather than absent.
    return {"id": cid, "label": cid.replace("-", " ").title(), "order": order}


class TestOrdering:
    def test_categories_come_back_in_order_not_document_sequence(self):
        doc = _doc([_cat("b", 20), _cat("a", 10), _cat("c", 30)])
        assert oe.load_category_order(fetcher=lambda: doc) == ["a", "b", "c"]

    def test_a_tie_on_order_falls_back_to_document_position(self):
        # Both order=10: the one written first must stay first, so a duplicated
        # order is stable instead of depending on sort internals.
        doc = _doc([_cat("first", 10), _cat("second", 10)])
        assert oe.load_category_order(fetcher=lambda: doc) == ["first", "second"]

    def test_a_duplicate_id_is_collapsed_to_its_best_position(self):
        doc = _doc([_cat("dup", 30), _cat("other", 20), _cat("dup", 10)])
        assert oe.load_category_order(fetcher=lambda: doc) == ["dup", "other"]


class TestFieldLevelDegradation:
    """A bad field degrades THAT field. Nothing here may raise."""

    @pytest.mark.parametrize(
        "order",
        [None, "10", 1.5, True, False, [], {}],
        ids=["none", "str", "float", "true", "false", "list", "dict"],
    )
    def test_an_order_that_is_not_a_real_int_drops_that_category(self, order):
        # `True`/`False` matter specifically: bool subclasses int, so a loose
        # check would sort True as 1 and place the category first.
        doc = _doc([{"id": "bad", "label": "Bad", "order": order}, _cat("good", 5)])
        assert oe.load_category_order(fetcher=lambda: doc) == ["good"]

    @pytest.mark.parametrize(
        "cid", [None, "", "   ", 5, [], {}], ids=["none", "empty", "blank", "int", "list", "dict"]
    )
    def test_a_missing_or_non_string_id_drops_that_category(self, cid):
        doc = _doc([{"id": cid, "order": 1}, _cat("good", 5)])
        assert oe.load_category_order(fetcher=lambda: doc) == ["good"]

    def test_a_non_dict_item_is_skipped_not_fatal(self):
        doc = _doc(["nope", 5, None, _cat("good", 1)])
        assert oe.load_category_order(fetcher=lambda: doc) == ["good"]

    def test_an_id_is_stripped_of_surrounding_whitespace(self):
        doc = _doc([{"id": "  spaced  ", "order": 1}])
        assert oe.load_category_order(fetcher=lambda: doc) == ["spaced"]

    def test_the_published_label_is_never_returned(self):
        # The rail resolves copy through its own i18n catalog; honouring an
        # English label here would replace localised copy for every user.
        doc = _doc([{"id": "developer-tools", "label": "ZZZ Custom", "order": 1}])
        assert oe.load_category_order(fetcher=lambda: doc) == ["developer-tools"]


class TestRefusals:
    @pytest.mark.parametrize(
        "version",
        [None, "1", 1.0, True, 2, 0, -1],
        ids=["none", "str", "float", "true", "2", "0", "neg"],
    )
    def test_an_unsupported_schema_version_refuses_the_document(self, version):
        doc = _doc([_cat("a", 1)], version=version)
        assert oe.load_category_order(fetcher=lambda: doc) == []

    @pytest.mark.parametrize(
        "categories", ["nope", 5, None, {}], ids=["str", "int", "none", "dict"]
    )
    def test_a_non_list_categories_field_yields_the_default(self, categories):
        assert oe.load_category_order(fetcher=lambda: _doc(categories)) == []

    def test_a_failed_fetch_yields_the_default(self):
        assert oe.load_category_order(fetcher=lambda: None) == []

    def test_more_than_the_cap_is_truncated_not_refused(self):
        many = [_cat(f"c{i:03d}", i) for i in range(oe.MAX_CATEGORIES + 10)]
        got = oe.load_category_order(fetcher=lambda: _doc(many))
        assert len(got) == oe.MAX_CATEGORIES


class TestCaching:
    def test_a_success_is_cached_and_the_fetcher_is_not_called_again(self):
        calls: list[int] = []

        def fetcher():
            calls.append(1)
            return _doc([_cat("a", 1)])

        assert oe.load_category_order(fetcher=fetcher) == ["a"]
        assert oe.load_category_order(fetcher=fetcher) == ["a"]
        assert len(calls) == 1, "the second call must be served from cache"

    def test_a_failure_is_cached_so_the_next_caller_does_not_wait_again(self):
        calls: list[int] = []

        def fetcher():
            calls.append(1)
            return None

        assert oe.load_category_order(fetcher=fetcher) == []
        assert oe.load_category_order(fetcher=fetcher) == []
        assert len(calls) == 1, "a remembered failure must not be retried"

    def test_the_failure_ttl_is_far_shorter_than_the_success_ttl(self):
        # Forgetting too early costs one retry; remembering too long keeps the
        # rail stale after the CDN is back.
        assert oe.FAILURE_TTL < oe.CACHE_TTL / 10

    def test_an_expired_failure_is_retried(self, _isolated_cache, monkeypatch):
        oe._write_failure()
        # Age the cache past FAILURE_TTL without sleeping.
        path = oe._cache_path()
        old = time.time() - (oe.FAILURE_TTL + 5)
        import os

        os.utime(path, (old, old))
        assert oe.load_category_order(fetcher=lambda: _doc([_cat("a", 1)])) == ["a"]

    def test_a_corrupt_cache_file_is_ignored_not_fatal(self, _isolated_cache):
        path = oe._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert oe.load_category_order(fetcher=lambda: _doc([_cat("a", 1)])) == ["a"]

    def test_a_cached_non_dict_is_ignored(self, _isolated_cache):
        path = oe._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(["a", "list"]), encoding="utf-8")
        assert oe.load_category_order(fetcher=lambda: _doc([_cat("a", 1)])) == ["a"]


class TestFetchSeam:
    def test_the_url_is_the_editorial_document_beside_the_registry(self):
        from kiro_crew.apps import official_catalog as oc

        assert oe.OFFICIAL_EDITORIAL_URL.startswith(oc.OFFICIAL_CATALOG_BASE)
        assert oe.OFFICIAL_EDITORIAL_URL.endswith("editorial.json")

    def test_the_download_goes_through_the_shared_fetch_seam(self, monkeypatch):
        # The guards (https-only, refuse redirects, byte cap, exception family)
        # live in that seam; a second copy of the fetch would let them drift.
        seen: list[str] = []

        def fake(url: str):
            seen.append(url)
            return _doc([_cat("a", 1)])

        monkeypatch.setattr(oe, "fetch_document", fake)
        assert oe._download() is not None
        assert seen == [oe.OFFICIAL_EDITORIAL_URL]
