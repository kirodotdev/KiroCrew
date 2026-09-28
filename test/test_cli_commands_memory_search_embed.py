"""`kirocrew memory search` must embed the query, not keyword-match it.

``VectorMemoryStore.search_episodic`` falls back to FTS5 whenever
``query_embedding`` is None and does not auto-embed (only ``get_episodic_context``
does), and nothing repairs an un-embedded QUERY the way the gateway's boot sweep
repairs an un-embedded row. The CLI therefore has to produce its own query
vector, or ``--layer all`` prints two labelled sections that are both keyword
hits over different corpora — while the two are documented as answering
different questions ("where did I write this word" versus "what does this mean
like").

Every test here drives ``_memory_cmd`` directly with an ``argparse.Namespace``,
the style of ``test_cli_commands_coverage.py``, and stands in for the embedding
backend rather than loading the 610MB GGUF.
"""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import cli_commands as cc
from kiro_crew.config.loader import KiroCrewConfig

_QUERY_VECTOR = [0.25, 0.5, 0.75, 1.0]


def _search_args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "mem_action": "search",
        "query": "how do I ship a release",
        "layer": "vector",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class _RecordingStore:
    """Stands in for ``VectorMemoryStore`` with the surface search touches."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.kwargs: dict[str, Any] = {}
        self.embed_fn: Any = None
        self.closed = False

    def init(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def recorded_embedding_space(self) -> str:
        return "sig-of-the-active-model"

    def _try_embed(self, text: str) -> list[float] | None:
        # Mirrors the real method: routes through whatever ``embed_fn`` is bound.
        return self.embed_fn(text) if self.embed_fn is not None else None

    def search_episodic(self, **kwargs: Any) -> list:
        self.kwargs = kwargs
        return [{"text": "cut the release from mainline", "importance": 0.8, "tags": "[]"}]


class _ReadyEmbedder:
    """Backend whose callable only produces vectors after ``wait_ready``.

    That is the shipped behaviour, and the trap in this fix: ``embed()`` never
    blocks on the GGUF load, so the FIRST call in a fresh process returns None
    and a naive wiring leaves the invocation the user typed on keyword search.
    """

    def __init__(self, *, loads: bool = True) -> None:
        self.loads = loads
        self.ready = False
        self.waited_with: float | None = None

    def wait_ready(self, timeout: float | None = None) -> bool:
        self.waited_with = timeout
        self.ready = self.loads
        return self.ready

    def is_ready(self) -> bool:
        return self.ready

    def embed(self, text: str) -> list[float] | None:
        return list(_QUERY_VECTOR) if self.ready else None


class _Harness:
    """Patches the store, the config load and the embedding module seams."""

    def __init__(
        self,
        *,
        model_present: bool = True,
        stale_space: bool = False,
        embedder: _ReadyEmbedder | None = None,
    ) -> None:
        self.store = _RecordingStore()
        self.embedder = embedder if embedder is not None else _ReadyEmbedder()
        self.embed_fn_builds = 0
        self.embedder_lookups = 0
        self._patches = [
            patch.object(cc, "VectorMemoryStore", lambda *a, **k: self.store),
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch.object(cc, "model_file_present", lambda *a, **k: model_present),
            patch.object(cc, "store_embedding_space_is_stale", lambda _s: stale_space),
            patch.object(cc, "get_shared_embedder", self._get_embedder),
            patch.object(cc, "make_sync_embed_fn", self._make_embed_fn),
        ]

    def _get_embedder(self) -> _ReadyEmbedder:
        self.embedder_lookups += 1
        return self.embedder

    def _make_embed_fn(self):  # type: ignore[no-untyped-def]
        self.embed_fn_builds += 1
        return self.embedder.embed

    def __enter__(self) -> _Harness:
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


class TestMemorySearchQueryEmbedding:
    def test_search_hands_search_episodic_a_query_vector(self) -> None:
        """The whole point: semantic recall, not a 0.4 FTS5 score."""
        with _Harness() as h:
            cc._memory_cmd(_search_args())
        assert h.store.kwargs["query_embedding"] == _QUERY_VECTOR
        assert h.store.kwargs["query_text"] == "how do I ship a release"

    def test_the_invocation_the_user_typed_is_not_left_on_keyword(self) -> None:
        """A fresh process must BLOCK on the load once, not return None.

        ``make_sync_embed_fn()``'s callable kicks the background GGUF load and
        returns None until the model is resident, so wiring it without waiting
        would produce a None vector on exactly the call being made.
        """
        embedder = _ReadyEmbedder()
        with _Harness(embedder=embedder) as h:
            cc._memory_cmd(_search_args())
        assert embedder.waited_with == cc._SEARCH_MODEL_LOAD_TIMEOUT_SECS
        assert h.store.kwargs["query_embedding"] is not None

    def test_absent_model_degrades_to_keyword_without_kicking_a_download(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A one-shot CLI must not start a 610MB download it abandons at exit."""
        with _Harness(model_present=False) as h:
            cc._memory_cmd(_search_args())
        assert h.store.kwargs["query_embedding"] is None
        assert h.embedder_lookups == 0 and h.embed_fn_builds == 0
        assert cc._SEARCH_KEYWORD_ONLY_NO_MODEL in capsys.readouterr().err

    def test_stale_vector_space_degrades_to_keyword(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A new-model query vector must not be scored against old-model vectors."""
        with _Harness(stale_space=True) as h:
            cc._memory_cmd(_search_args())
        assert h.store.kwargs["query_embedding"] is None
        assert h.store.embed_fn is None
        assert cc._SEARCH_KEYWORD_ONLY_STALE_SPACE in capsys.readouterr().err

    def test_model_that_never_loads_degrades_to_keyword(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _Harness(embedder=_ReadyEmbedder(loads=False)) as h:
            cc._memory_cmd(_search_args())
        assert h.store.kwargs["query_embedding"] is None
        assert cc._SEARCH_KEYWORD_ONLY_NOT_READY in capsys.readouterr().err

    def test_empty_query_never_touches_the_embedder(self) -> None:
        """Nothing to embed, so nothing may pay the model-load wait."""
        with _Harness() as h:
            cc._memory_cmd(_search_args(query=""))
        assert h.store.kwargs["query_embedding"] is None
        assert h.embedder_lookups == 0

    def test_the_store_is_still_closed_on_the_degraded_path(self) -> None:
        with _Harness(model_present=False) as h:
            cc._memory_cmd(_search_args())
        assert h.store.closed


class TestUnembeddedCorpusStillReachable:
    """A NULL-embedding episodic row must stay reachable from the CLI.

    ``write_episodic(defer_embedding=True)`` bulk writers, `memory import` and
    `memory migrate` (this store binds no ``embed_fn`` on those paths), and rows
    cleared by ``reconcile_embedding_space`` all leave rows that neither vector
    leg can see — both filter on ``embedding IS NOT NULL`` — until the gateway's
    boot-only, paced backfill sweep reaches them. An empty semantic result over
    such a corpus is not "no such memory", so the keyword leg is retried once.
    """

    def test_empty_vector_pass_retries_the_keyword_leg(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A NULL-embedding row (deferred / imported / re-embed-pending) must
        still be findable: the vector legs cannot see it, the keyword leg can."""
        calls: list[dict[str, Any]] = []

        def _search(**kwargs: Any) -> list:
            calls.append(kwargs)
            if kwargs.get("query_embedding") is not None:
                return []  # corpus has no vectors yet
            return [{"text": "cut the release from mainline", "importance": 0.8, "tags": "[]"}]

        with _Harness() as h:
            h.store.search_episodic = _search  # type: ignore[method-assign]
            cc._memory_cmd(_search_args())
        out = capsys.readouterr()
        assert [c.get("query_embedding") is not None for c in calls] == [True, False]
        assert "cut the release from mainline" in out.out
        assert "No episodic memories found." not in out.out
        assert cc._SEARCH_KEYWORD_ONLY_PENDING_ROWS in out.err

    def test_a_truly_empty_store_reports_nothing_and_stays_quiet(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _search(**kwargs: Any) -> list:
            return []

        with _Harness() as h:
            h.store.search_episodic = _search  # type: ignore[method-assign]
            cc._memory_cmd(_search_args())
        out = capsys.readouterr()
        assert "No episodic memories found." in out.out
        assert cc._SEARCH_KEYWORD_ONLY_PENDING_ROWS not in out.err

    def test_a_degraded_query_does_not_run_the_keyword_leg_twice(self) -> None:
        calls: list[dict[str, Any]] = []

        def _search(**kwargs: Any) -> list:
            calls.append(kwargs)
            return []

        with _Harness(model_present=False) as h:
            h.store.search_episodic = _search  # type: ignore[method-assign]
            cc._memory_cmd(_search_args())
        assert len(calls) == 1
