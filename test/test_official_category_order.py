"""Tests for the category-order consumer.

The shape mirrors ``test_official_catalog.py``: every refusal path is asserted with
the DEFAULT as the expected answer, because "degrade to the built-in order" is the
promise this module makes and an empty list is how it keeps it.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from kiro_crew.apps import official_category_order as co


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Point the cache at a scratch dir so tests never read the real one."""
    monkeypatch.setattr(co, "_cache_path", lambda: tmp_path / "category-order.json")
    return tmp_path


def _doc(categories: Any, version: int | Any = 1) -> dict[str, Any]:
    return {"schemaVersion": version, "categories": categories}


class TestOrdering:
    def test_array_position_is_the_sequence(self):
        doc = _doc(["b", "a", "c"])
        assert co.load_category_order(fetcher=lambda: doc) == ["b", "a", "c"]

    def test_a_duplicate_id_keeps_its_first_position(self):
        # The publish gate rejects duplicates, so this only fires on a document
        # that bypassed it; the curator's first mention is the intentional one.
        doc = _doc(["dup", "other", "dup"])
        assert co.load_category_order(fetcher=lambda: doc) == ["dup", "other"]

    def test_the_live_document_shape_round_trips(self):
        # The six ids the CDN publishes today, in the order it publishes them.
        live = [
            "developer-tools",
            "oncall-ops",
            "productivity",
            "agents-automation",
            "research-writing",
            "other",
        ]
        assert co.load_category_order(fetcher=lambda: _doc(live)) == live


class TestFieldLevelDegradation:
    """A bad entry degrades THAT entry. Nothing here may raise."""

    @pytest.mark.parametrize(
        "entry",
        [None, 5, 1.5, True, False, [], {}, {"id": "nested"}],
        ids=["none", "int", "float", "true", "false", "list", "dict", "old-shape"],
    )
    def test_a_non_string_entry_is_dropped(self, entry):
        # `old-shape` is the pre-split `{"id": ...}` form: a document still in the
        # old shape yields nothing rather than a rail of unusable entries.
        doc = _doc([entry, "good"])
        assert co.load_category_order(fetcher=lambda: doc) == ["good"]

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"], ids=["empty", "spaces", "ws"])
    def test_a_blank_id_is_dropped(self, blank):
        assert co.load_category_order(fetcher=lambda: _doc([blank, "good"])) == ["good"]

    def test_an_id_is_stripped_of_surrounding_whitespace(self):
        assert co.load_category_order(fetcher=lambda: _doc(["  spaced  "])) == ["spaced"]

    def test_an_entirely_old_shape_document_yields_the_default(self):
        # Every entry a dict: nothing is usable, so the rail falls back rather
        # than rendering a partial order.
        old = [{"id": "a", "order": 1}, {"id": "b", "order": 2}]
        assert co.load_category_order(fetcher=lambda: _doc(old)) == []


class TestRefusals:
    @pytest.mark.parametrize(
        "version",
        [None, "1", 1.0, True, 2, 0, -1],
        ids=["none", "str", "float", "true", "2", "0", "neg"],
    )
    def test_an_unsupported_schema_version_refuses_the_document(self, version):
        doc = _doc(["a"], version=version)
        assert co.load_category_order(fetcher=lambda: doc) == []

    @pytest.mark.parametrize(
        "categories", ["nope", 5, None, {}], ids=["str", "int", "none", "dict"]
    )
    def test_a_non_list_categories_field_yields_the_default(self, categories):
        assert co.load_category_order(fetcher=lambda: _doc(categories)) == []

    def test_a_failed_fetch_yields_the_default(self):
        assert co.load_category_order(fetcher=lambda: None) == []

    def test_more_than_the_cap_is_truncated_not_refused(self):
        many = [f"c{i:03d}" for i in range(co.MAX_CATEGORIES + 10)]
        got = co.load_category_order(fetcher=lambda: _doc(many))
        assert len(got) == co.MAX_CATEGORIES


class TestCaching:
    def test_interrupted_write_keeps_the_previous_cache(self, monkeypatch):
        path = co._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = _doc(["previous"])
        replacement = _doc(["replacement"])
        path.write_text(json.dumps(previous), encoding="utf-8")

        real_write_text = type(path).write_text

        def interrupted_write(self, data, *args, **kwargs):
            if self == path:
                real_write_text(self, data[:8], *args, **kwargs)
                raise OSError("simulated interrupted cache write")
            return real_write_text(self, data, *args, **kwargs)

        def interrupted_atomic_write(*args, **kwargs):
            raise OSError("simulated interrupted cache write")

        monkeypatch.setattr(type(path), "write_text", interrupted_write)
        monkeypatch.setattr(co, "atomic_write", interrupted_atomic_write, raising=False)

        co._write_cache(replacement)

        assert json.loads(path.read_text(encoding="utf-8")) == previous

    def test_a_success_is_cached_and_the_fetcher_is_not_called_again(self):
        calls: list[int] = []

        def fetcher():
            calls.append(1)
            return _doc(["a"])

        assert co.load_category_order(fetcher=fetcher) == ["a"]
        assert co.load_category_order(fetcher=fetcher) == ["a"]
        assert len(calls) == 1, "the second call must be served from cache"

    def test_a_failure_is_cached_so_the_next_caller_does_not_wait_again(self):
        calls: list[int] = []

        def fetcher():
            calls.append(1)
            return None

        assert co.load_category_order(fetcher=fetcher) == []
        assert co.load_category_order(fetcher=fetcher) == []
        assert len(calls) == 1, "a remembered failure must not be retried"

    def test_the_failure_ttl_is_far_shorter_than_the_success_ttl(self):
        # Forgetting too early costs one retry; remembering too long keeps the
        # rail stale after the CDN is back.
        assert co.FAILURE_TTL < co.CACHE_TTL / 10

    def test_an_expired_failure_is_retried(self, _isolated_cache):
        co._write_failure()
        # Age the cache past FAILURE_TTL without sleeping.
        path = co._cache_path()
        old = time.time() - (co.FAILURE_TTL + 5)
        import os

        os.utime(path, (old, old))
        assert co.load_category_order(fetcher=lambda: _doc(["a"])) == ["a"]

    def test_a_corrupt_cache_file_is_ignored_not_fatal(self, _isolated_cache):
        path = co._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert co.load_category_order(fetcher=lambda: _doc(["a"])) == ["a"]

    def test_a_cached_non_dict_is_ignored(self, _isolated_cache):
        path = co._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(["a", "list"]), encoding="utf-8")
        assert co.load_category_order(fetcher=lambda: _doc(["a"])) == ["a"]


class TestIndependenceFromTheEditorialDocument:
    """The separation is the point of the split, so it is asserted, not assumed."""

    def test_the_two_documents_do_not_share_a_cache_file(self):
        from kiro_crew.apps import official_editorial as oe

        assert co._cache_path() != oe._cache_path()

    def test_an_editorial_version_bump_leaves_the_rail_order_intact(self, monkeypatch):
        # The reason these are two documents: a version this client does not
        # recognise discards the WHOLE document it appears in. Bundled, a bump made
        # for a new featuring shape would also re-sort every category here.
        from kiro_crew.apps import official_editorial as oe

        monkeypatch.setattr(oe, "_cache_path", lambda: co._cache_path().with_name("ed.json"))
        future = {"schemaVersion": co.SUPPORTED_SCHEMA_VERSION + 1, "sections": []}
        assert oe.load_sections(fetcher=lambda: future) == []
        assert co.load_category_order(fetcher=lambda: _doc(["a", "b"])) == ["a", "b"]

    def test_the_editorial_module_no_longer_exposes_a_rail_reader(self):
        from kiro_crew.apps import official_editorial as oe

        assert not hasattr(oe, "load_category_order")


class TestFetchSeam:
    def test_the_url_sits_beside_the_registry(self):
        from kiro_crew.apps import official_catalog as oc

        assert co.OFFICIAL_CATEGORY_ORDER_URL.startswith(oc.OFFICIAL_CATALOG_BASE)
        assert co.OFFICIAL_CATEGORY_ORDER_URL.endswith("category-order.json")

    def test_the_download_goes_through_the_shared_fetch_seam(self, monkeypatch):
        # The guards (https-only, refuse redirects, byte cap, exception family)
        # live in that seam; a second copy of the fetch would let them drift.
        seen: list[str] = []

        def fake(url: str):
            seen.append(url)
            return _doc(["a"])

        monkeypatch.setattr(co, "fetch_document", fake)
        assert co._download() is not None
        assert seen == [co.OFFICIAL_CATEGORY_ORDER_URL]
