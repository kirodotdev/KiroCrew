"""The Knowledge Library vector leg: one query norm, a sniffed blob, two equal paths.

``HybridRetriever._vector_search`` scores its candidate rows either as one numpy
matrix-vector product (when numpy imports, behind ``_HAS_NUMPY`` like the memory
layer) or as a per-row stdlib loop. On both paths the query's own norm is derived
once per search rather than once per row, and ``_bytes_to_floats`` sniffs a blob's
first significant byte before offering it to ``json.loads``, so a struct-packed
blob does not pay a failed parse and its exception on every row. These tests pin
those three mechanics and the one property that makes the numpy path safe: it
ranks identically to the pure-Python fallback on a corpus that mixes both stored
encodings with the rows every search must skip.
"""

from __future__ import annotations

import codecs
import json
import random
import struct
from pathlib import Path

import pytest

from kiro_crew.knowledge import retrieval
from kiro_crew.knowledge.embedder import floats_to_bytes
from kiro_crew.knowledge.retrieval import (
    ANY_EMBEDDING_SPACE,
    HybridRetriever,
    _bytes_to_floats,
    _looks_like_json,
)
from kiro_crew.knowledge.store import KnowledgeStore

_DIM = 8
# _mixed_corpus shape: how many rows are struct-packed at the query's width
# (six random + the zero vector) and how many are admitted for scoring at all
# (those seven + three legacy JSON rows).
_PACKED_ROWS = 7
_ADMITTED_ROWS = 10


@pytest.fixture()
def store(tmp_path: Path):
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    yield s
    s.close()


def _vec(rng: random.Random, dim: int = _DIM) -> list[float]:
    return [rng.gauss(0.0, 1.0) for _ in range(dim)]


def _mixed_corpus(store: KnowledgeStore, rng: random.Random) -> dict[str, str]:
    """Every stored shape the leg meets in one library. Returns ``{role: item_id}``.

    The admitted encodings -- legacy JSON as TEXT, legacy JSON as bytes (once with
    the leading whitespace RFC 8259 allows) and the compact struct-packed form --
    plus the rows that must never rank: a vector of another dimensionality, a
    zero vector, an empty blob, and a JSON value that is not a list at all.
    """
    ids = {}
    ids["json_text"] = store.add_item("json text", "c", "doc", embedding=json.dumps(_vec(rng)))
    ids["json_bytes"] = store.add_item(
        "json bytes", "c", "doc", embedding=json.dumps(_vec(rng)).encode()
    )
    ids["json_padded"] = store.add_item(
        "json padded", "c", "doc", embedding=b" \n" + json.dumps(_vec(rng)).encode()
    )
    for i in range(6):
        ids[f"binary_{i}"] = store.add_item(
            f"binary {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng))
        )
    ids["mismatched"] = store.add_item(
        "mismatched", "c", "doc", embedding=floats_to_bytes(_vec(rng, _DIM // 2))
    )
    ids["zero"] = store.add_item("zero", "c", "doc", embedding=floats_to_bytes([0.0] * _DIM))
    ids["empty"] = store.add_item("empty", "c", "doc", embedding=b"")
    ids["not_a_list"] = store.add_item(
        "not a list", "c", "doc", embedding=json.dumps({"k": 1}).encode()
    )
    return ids


def _retriever(store: KnowledgeStore, query_vec: list[float]) -> HybridRetriever:
    return HybridRetriever(
        store, embedder=lambda _q: list(query_vec), embed_sig=ANY_EMBEDDING_SPACE
    )


class TestRankingParity:
    """The numpy path is held to the Python loop: same rows in, same ranking out."""

    def test_numpy_and_fallback_rank_identically(self, store, monkeypatch):
        pytest.importorskip("numpy")
        rng = random.Random(8890)
        ids = _mixed_corpus(store, rng)
        query_vec = _vec(rng)
        retriever = _retriever(store, query_vec)

        assert retrieval._HAS_NUMPY is True
        with_numpy = retriever._vector_search("q", limit=50)
        monkeypatch.setattr(retrieval, "_HAS_NUMPY", False)
        fallback = retriever._vector_search("q", limit=50)

        assert with_numpy == fallback
        assert with_numpy, "a random corpus has at least one positive cosine"
        ranked = {item_id for item_id, _ in with_numpy}
        # Every row the leg must skip is absent from both paths.
        for role in ("mismatched", "zero", "empty", "not_a_list"):
            assert ids[role] not in ranked
        # Both stored encodings compete in one ranking: the rows that made the cut
        # are a subset of the admitted ones, and nothing else got in.
        admitted = {ids[k] for k in ids if k.startswith(("json_", "binary_"))}
        assert ranked <= admitted

    def test_numpy_path_takes_the_matrix_route(self, store, monkeypatch):
        """The fast path really is taken: every packed row is viewed by ONE
        ``frombuffer`` over the joined blobs, not unpacked row by row."""
        np = pytest.importorskip("numpy")
        rng = random.Random(1)
        _mixed_corpus(store, rng)
        retriever = _retriever(store, _vec(rng))

        calls: list[bytes] = []
        real_frombuffer = np.frombuffer

        def spy(buf, *a, **kw):
            calls.append(bytes(buf))
            return real_frombuffer(buf, *a, **kw)

        monkeypatch.setattr(retrieval.np, "frombuffer", spy)
        retriever._vector_search("q", limit=50)
        assert len(calls) == 1
        assert len(calls[0]) == _PACKED_ROWS * _DIM * 4

    def test_scores_match_to_float64_rounding(self, store):
        """Beyond rank order: the raw cosines agree to double rounding, so a
        downstream threshold cannot see which path produced them."""
        pytest.importorskip("numpy")
        rng = random.Random(2)
        _mixed_corpus(store, rng)
        query_vec = _vec(rng)
        rows = store.db.execute("SELECT id, embedding FROM items").fetchall()

        numpy_scored, numpy_mismatched = HybridRetriever._score_rows_numpy(rows, query_vec)
        python_scored, python_mismatched = HybridRetriever._score_rows_python(rows, query_vec)

        assert numpy_mismatched == python_mismatched == 1
        assert [i for i, _ in numpy_scored] == [i for i, _ in python_scored]
        for (_, a), (_, b) in zip(numpy_scored, python_scored):
            assert a == pytest.approx(b, rel=1e-12, abs=1e-12)

    def test_zero_query_vector_ranks_nothing_on_both_paths(self, store, monkeypatch):
        pytest.importorskip("numpy")
        rng = random.Random(3)
        _mixed_corpus(store, rng)
        retriever = _retriever(store, [0.0] * _DIM)
        assert retriever._vector_search("q") == []
        monkeypatch.setattr(retrieval, "_HAS_NUMPY", False)
        assert retriever._vector_search("q") == []


class TestQueryNormHoisted:
    """The query norm is derived once per search, not once per row -- the same
    hoisting the lesson scorer in ``vector_memory`` does."""

    def test_fallback_computes_query_norm_once(self, store, monkeypatch):
        rng = random.Random(4)
        _mixed_corpus(store, rng)
        query_vec = _vec(rng)
        retriever = _retriever(store, query_vec)

        norm_args: list[list[float]] = []
        real_norm = retrieval._l2_norm

        def spy(vec):
            norm_args.append(list(vec))
            return real_norm(vec)

        monkeypatch.setattr(retrieval, "_l2_norm", spy)
        monkeypatch.setattr(retrieval, "_HAS_NUMPY", False)
        retriever._vector_search("q", limit=50)

        query_norm_calls = [v for v in norm_args if v == query_vec]
        assert len(query_norm_calls) == 1, (
            f"query norm computed {len(query_norm_calls)}x -- it must be hoisted out "
            "of the per-row loop"
        )
        # One norm per admitted row (the zero row is admitted; its norm is what
        # rejects it). Mismatched / empty / non-list rows never reach the norm.
        assert len(norm_args) == 1 + _ADMITTED_ROWS


class TestBlobSniff:
    """``json.loads`` is attempted only on blobs that can be JSON; a struct-packed
    blob is decoded without paying a failed parse and the exception it raises."""

    @pytest.fixture()
    def json_loads_spy(self, monkeypatch):
        calls: list[object] = []
        real = json.loads

        def spy(s, *a, **kw):
            calls.append(s)
            return real(s, *a, **kw)

        monkeypatch.setattr(retrieval.json, "loads", spy)
        return calls

    def test_binary_blob_skips_json_parse(self, json_loads_spy):
        vec = [float(i) for i in range(_DIM)]
        blob = floats_to_bytes(vec)
        assert not _looks_like_json(blob)
        assert _bytes_to_floats(blob) == vec
        assert json_loads_spy == [], "a packed blob must not be offered to json.loads"

    def test_json_bytes_and_text_still_parse(self, json_loads_spy):
        vec = [1.0, 2.0, 3.0]
        assert _bytes_to_floats(json.dumps(vec).encode()) == vec
        assert _bytes_to_floats(json.dumps(vec)) == vec
        assert _bytes_to_floats(b"\t\r\n " + json.dumps(vec).encode()) == vec
        assert len(json_loads_spy) == 3

    def test_binary_blob_that_starts_like_json_falls_through(self, json_loads_spy):
        """A packed blob whose first byte happens to be ``[`` (0x5B) is sniffed as
        JSON, fails to parse, and decodes as binary -- exactly the pre-sniff result."""
        raw = bytearray(floats_to_bytes([float(i) for i in range(_DIM)]))
        raw[0] = 0x5B
        blob = bytes(raw)
        assert _looks_like_json(blob)
        assert _bytes_to_floats(blob) == list(struct.unpack(f"{_DIM}f", blob))
        assert len(json_loads_spy) == 1

    def test_rejections_are_unchanged(self, json_loads_spy):
        assert _bytes_to_floats(b"") == []
        assert _bytes_to_floats(None) == []
        assert _bytes_to_floats(b"not json") == []
        assert _bytes_to_floats(b"[1, 2, 3") == []  # sniffed JSON, malformed, too short
        assert _bytes_to_floats(json.dumps({"k": 1}).encode()) == []  # JSON, not a list
        assert _bytes_to_floats(b"\x00" * 15) == []  # packed but below the 16-byte floor
        assert _bytes_to_floats(b"\x00" * 18) == []  # packed but not a multiple of 4

    def test_every_json_encoding_json_loads_accepts_is_sniffed(self, json_loads_spy):
        """The sniff mirrors ``json.loads`` own admission: a JSON list in any
        encoding it decodes (UTF-8 with a BOM, UTF-16/32 either endianness, with
        or without a BOM, after leading whitespace) still parses as JSON and never
        reaches the binary decoder."""
        vec = [1.0, 2.0, 3.0, 4.0, 5.0]
        text = json.dumps(vec)
        blobs = [
            codecs.BOM_UTF8 + text.encode(),
            text.encode("utf-16"),
            text.encode("utf-16-le"),
            text.encode("utf-16-be"),
            text.encode("utf-32"),
            text.encode("utf-32-le"),
            text.encode("utf-32-be"),
            (" \n" + text).encode("utf-16-le"),
            (" \n" + text).encode("utf-32-be"),
            # JSON puts no bound on leading whitespace: well past any prefix a
            # sniff might read, in the narrowest and the widest encoding.
            (" " * 300 + text).encode(),
            ("\n" * 200 + text).encode("utf-32-be"),
        ]
        for blob in blobs:
            assert json.loads(blob) == vec, "fixture must be something json accepts"
            assert _looks_like_json(blob), blob[:8]
            assert _bytes_to_floats(blob) == vec, blob[:8]
        assert len(json_loads_spy) == 2 * len(blobs)

    def test_packed_blob_with_a_json_lead_byte_still_decodes_as_binary(self, json_loads_spy):
        """A float32 blob whose first byte is NUL or a BOM byte takes the full sniff,
        is not a JSON list, and decodes as binary without a parse attempt."""
        for lead in (0x00, 0xEF, 0xFE, 0xFF, 0x20):
            raw = bytearray(floats_to_bytes([float(i) + 1.5 for i in range(_DIM)]))
            raw[0] = lead
            blob = bytes(raw)
            assert _bytes_to_floats(blob) == list(struct.unpack(f"{_DIM}f", blob)), hex(lead)
        assert json_loads_spy == []


class TestBatching:
    """The numpy scorer works in bounded batches and the batch seam is invisible."""

    def test_batched_scoring_matches_python_path_and_keeps_order(self, store, monkeypatch):
        np = pytest.importorskip("numpy")
        rng = random.Random(5)
        _mixed_corpus(store, rng)
        for i in range(9):
            store.add_item(f"extra {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        query_vec = _vec(rng)
        rows = store.db.execute("SELECT id, embedding FROM items").fetchall()
        python_scored, _ = HybridRetriever._score_rows_python(rows, query_vec)

        calls: list[int] = []
        real_frombuffer = np.frombuffer

        def spy(buf, *a, **kw):
            calls.append(len(buf))
            return real_frombuffer(buf, *a, **kw)

        monkeypatch.setattr(retrieval.np, "frombuffer", spy)
        monkeypatch.setattr(retrieval, "_SCORE_BATCH_ROWS", 4)
        numpy_scored, _ = HybridRetriever._score_rows_numpy(rows, query_vec)

        assert [i for i, _ in numpy_scored] == [i for i, _ in python_scored]
        for (_, a), (_, b) in zip(numpy_scored, python_scored):
            assert a == pytest.approx(b, rel=1e-12, abs=1e-12)
        # 19 admitted rows in batches of 4 -> 5 batches, each with at most 4
        # packed rows in its frombuffer.
        assert len(calls) == 5
        assert all(n <= 4 * _DIM * 4 for n in calls)

    def test_json_rows_are_decoded_and_scored_batch_by_batch(self, store, monkeypatch):
        """A legacy JSON corpus does not accumulate every decoded list before any
        scoring happens: decoding and scoring interleave at the batch boundary, so
        working memory is bounded by the batch and not by the library."""
        pytest.importorskip("numpy")
        rng = random.Random(6)
        for i in range(10):
            store.add_item(f"json {i}", "c", "doc", embedding=json.dumps(_vec(rng)).encode())
        query_vec = _vec(rng)
        rows = store.db.execute("SELECT id, embedding FROM items").fetchall()

        events: list[str] = []
        real_decode = retrieval._bytes_to_floats
        real_score = retrieval._score_batch

        def decode_spy(blob):
            events.append("decode")
            return real_decode(blob)

        def score_spy(ids, vectors, *a, **kw):
            events.append(f"score:{len(vectors)}")
            return real_score(ids, vectors, *a, **kw)

        monkeypatch.setattr(retrieval, "_bytes_to_floats", decode_spy)
        monkeypatch.setattr(retrieval, "_score_batch", score_spy)
        monkeypatch.setattr(retrieval, "_SCORE_BATCH_ROWS", 4)
        scored, _ = HybridRetriever._score_rows_numpy(rows, query_vec)
        python_scored, _ = HybridRetriever._score_rows_python(rows, query_vec)

        assert [i for i, _ in scored] == [i for i, _ in python_scored]
        # 10 rows, batches of 4: the first batch is scored after its 4 decodes and
        # BEFORE the fifth row is decoded; no batch holds more than 4 rows.
        assert events[:5] == ["decode"] * 4 + ["score:4"]
        assert [e for e in events if e.startswith("score")] == ["score:4", "score:4", "score:2"]
