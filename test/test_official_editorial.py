"""Tests for the editorial consumer's fetch, cache and schema gate.

The projection of individual sections is covered in ``test_editorial_sections.py``;
what is asserted here is the document plumbing around it. Every refusal path is
asserted with the DEFAULT as the expected answer, because "degrade to the derived
featured pick" is the promise this module makes and an empty list is how it keeps
it.
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


def _app(ref: str = "a") -> dict[str, Any]:
    """One well-formed ``full`` block. The base landed the form/items shape
    (sections are blocks, not bare cards) while this branch was in flight, so a
    bare ``{"type": "app"}`` section is now silently dropped by the reader --
    these tests only count sections, and every count would read 0."""
    return {"form": "full", "items": [{"type": "app", "appRef": ref}]}


def _doc(sections: Any, version: int | Any = 1) -> dict[str, Any]:
    return {"schemaVersion": version, "sections": sections}


class TestRefusals:
    @pytest.mark.parametrize(
        "version",
        [None, "1", 1.0, True, 2, 0, -1],
        ids=["none", "str", "float", "true", "2", "0", "neg"],
    )
    def test_an_unsupported_schema_version_refuses_the_document(self, version):
        doc = _doc([_app()], version=version)
        assert oe.load_sections(fetcher=lambda: doc) == []

    def test_a_failed_fetch_yields_the_default(self):
        assert oe.load_sections(fetcher=lambda: None) == []


class TestCaching:
    def test_interrupted_write_keeps_the_previous_cache(self, monkeypatch):
        path = oe._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = _doc([_app("previous")])
        replacement = _doc([_app("replacement")])
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
        monkeypatch.setattr(oe, "atomic_write", interrupted_atomic_write, raising=False)

        oe._write_cache(replacement)

        assert json.loads(path.read_text(encoding="utf-8")) == previous

    def test_a_success_is_cached_and_the_fetcher_is_not_called_again(self):
        calls: list[int] = []

        def fetcher():
            calls.append(1)
            return _doc([_app()])

        assert len(oe.load_sections(fetcher=fetcher)) == 1
        assert len(oe.load_sections(fetcher=fetcher)) == 1
        assert len(calls) == 1, "the second call must be served from cache"

    def test_a_failure_is_cached_so_the_next_caller_does_not_wait_again(self):
        calls: list[int] = []

        def fetcher():
            calls.append(1)
            return None

        assert oe.load_sections(fetcher=fetcher) == []
        assert oe.load_sections(fetcher=fetcher) == []
        assert len(calls) == 1, "a remembered failure must not be retried"

    def test_the_failure_ttl_is_far_shorter_than_the_success_ttl(self):
        # Forgetting too early costs one retry; remembering too long keeps the
        # featured list stale after the CDN is back.
        assert oe.FAILURE_TTL < oe.CACHE_TTL / 10

    def test_an_expired_failure_is_retried(self, _isolated_cache):
        oe._write_failure()
        # Age the cache past FAILURE_TTL without sleeping.
        path = oe._cache_path()
        old = time.time() - (oe.FAILURE_TTL + 5)
        import os

        os.utime(path, (old, old))
        assert len(oe.load_sections(fetcher=lambda: _doc([_app()]))) == 1

    def test_a_corrupt_cache_file_is_ignored_not_fatal(self, _isolated_cache):
        path = oe._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert len(oe.load_sections(fetcher=lambda: _doc([_app()]))) == 1

    def test_a_cached_non_dict_is_ignored(self, _isolated_cache):
        path = oe._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(["a", "list"]), encoding="utf-8")
        assert len(oe.load_sections(fetcher=lambda: _doc([_app()]))) == 1


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
            return _doc([_app()])

        monkeypatch.setattr(oe, "fetch_document", fake)
        assert oe._download() is not None
        assert seen == [oe.OFFICIAL_EDITORIAL_URL]

    def test_a_published_categories_key_is_not_read_here(self):
        # The rail's order moved to its own document. A stale editorial document
        # that still carries the key must not resurrect a second reader for it.
        doc = {"schemaVersion": 1, "categories": ["a", "b"], "sections": [_app()]}
        assert len(oe.load_sections(fetcher=lambda: doc)) == 1
        assert not hasattr(oe, "load_category_order")
