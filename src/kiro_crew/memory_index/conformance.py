"""The memory index conformance kit.

Runs the contract against one backend and says, check by check, what holds:

- after ``upsert`` a search finds the document; after ``remove`` it does not;
- upserting one ident twice leaves one document, and removing an unknown
  ident does nothing;
- a search during ``rebuild`` sees the old content, and after ``swap`` only
  the new;
- ``status`` reports the seqs and the embedding space it was given;
- ``filters`` are applied, unless the backend leaves ``filters`` out of its
  capabilities;
- substring search on English, Chinese and mixed text returns the same rows as
  the ``exact`` backend;
- a terms search returns exactly the documents holding ``min_match`` of the
  query terms, as ``exact`` does;
- the top 10 of a vector search overlap the ``exact`` top 10 by at least 0.95
  on a fixed 2,000-document fixture.

A capability the backend does not declare is reported as skipped, not failed.
Every check gets a fresh instance from the factory, so one check's leftovers
cannot pass or fail another.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from kiro_crew import memory_v2
from kiro_crew.memory_index.contract import (
    CAP_FILTERS,
    CAP_SUBSTRING,
    CAP_TERMS,
    CAP_VECTOR,
    EmbeddingSpace,
    IndexDoc,
    IndexQuery,
    MemoryIndex,
    StoreRef,
)
from kiro_crew.memory_index.exact import ExactIndex

DIM = 16
SPACE = EmbeddingSpace(signature="conformance", dim=DIM)
STORE = StoreRef(store_id="conformance")
TOP10_FLOOR = 0.95
FIXTURE_ROWS = 2000
_QUERIES = 50

_TEXTS = (
    ("en", "rebase the branch onto main before the push"),
    ("en2", "the worktree cleanup keeps every bot home folder"),
    ("zh", "合并之前先跑测试,不要碰正在运行的网关"),
    ("zh2", "工作树清理时保留机器人的主目录"),
    ("mix", "kas 回归测试在 pr41790 上失败"),
    ("mix2", "slack 线程里的 bug 报告"),
)
_SUBSTRING_QUERIES = (
    "rebase",
    "worktree clean",
    "4179",
    "测试",
    "工作树",
    "kas 回归",
    "slack 线程",
    "失败 pr",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # "pass" | "fail" | "skip"
    detail: str = ""


def _vec(values) -> bytes:
    array = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(array))
    return (array / norm if norm else array).astype(np.float32).tobytes()


def _axis(i: int) -> bytes:
    values = [0.0] * DIM
    values[i % DIM] = 1.0
    return _vec(values)


def _doc(
    ident: str, *, vector: bytes | None = None, text: str = "", kind: str = "fact", **filters: str
) -> IndexDoc:
    terms = " ".join(sorted(memory_v2.terms(text)))
    return IndexDoc(("items", ident), kind, vector, text, terms, dict(filters))


def _open(factory: Callable[[], MemoryIndex]) -> MemoryIndex:
    index = factory()
    index.open(STORE, SPACE, {})
    return index


def _idents(index: MemoryIndex, query: IndexQuery) -> list[str]:
    return [hit.ident[1] for hit in index.search(query)]


def _check_upsert_remove(index: MemoryIndex) -> str | None:
    index.upsert(
        [_doc("a", vector=_axis(0), text="alpha note"), _doc("b", vector=_axis(1), text="beta")],
        seq=1,
    )
    if CAP_VECTOR in index.capabilities and "a" not in _idents(
        index, IndexQuery("vector", 5, vector=_axis(0))
    ):
        return "a vector search did not find an upserted document"
    if CAP_TERMS in index.capabilities and "a" not in _idents(
        index, IndexQuery("terms", 5, text="alpha")
    ):
        return "a terms search did not find an upserted document"
    index.remove([("items", "a")], seq=2)
    for mode, query in (
        (CAP_VECTOR, IndexQuery("vector", 5, vector=_axis(0))),
        (CAP_TERMS, IndexQuery("terms", 5, text="alpha")),
        (CAP_SUBSTRING, IndexQuery("substring", 5, text="alpha")),
    ):
        if mode in index.capabilities and "a" in _idents(index, query):
            return f"a {mode} search returned a removed document"
    return None


def _check_idempotent(index: MemoryIndex) -> str | None:
    doc = _doc("a", vector=_axis(0), text="alpha")
    index.upsert([doc], seq=1)
    index.upsert([doc], seq=2)
    index.remove([("items", "never-held")], seq=3)
    mode = "vector" if CAP_VECTOR in index.capabilities else "terms"
    query = (
        IndexQuery("vector", 5, vector=_axis(0))
        if mode == "vector"
        else IndexQuery("terms", 5, text="alpha")
    )
    found = _idents(index, query)
    if found.count("a") != 1:
        return f"one ident upserted twice came back {found.count('a')} times"
    if index.status().rows != 1:
        return f"status counts {index.status().rows} rows after two upserts of one ident"
    return None


def _check_build_swap(index: MemoryIndex) -> str | None:
    index.upsert([_doc("old", vector=_axis(0), text="old content")], seq=1)
    handle = index.rebuild([_doc("new", vector=_axis(0), text="new content")], build_seq=5)
    mode = "vector" if CAP_VECTOR in index.capabilities else "terms"

    def found() -> set[str]:
        if mode == "vector":
            return set(_idents(index, IndexQuery("vector", 5, vector=_axis(0))))
        return set(_idents(index, IndexQuery("terms", 5, text="old new content")))

    if found() != {"old"}:
        return f"a search before swap saw {sorted(found())}, not the old content"
    index.swap(handle)
    if found() != {"new"}:
        return f"a search after swap saw {sorted(found())}, not only the new content"
    return None


def _check_status(index: MemoryIndex) -> str | None:
    index.swap(index.rebuild([_doc("a", vector=_axis(0), text="alpha")], build_seq=7))
    status = index.status()
    if (status.built_seq, status.applied_seq) != (7, 7):
        return f"after a build at seq 7, status reports built {status.built_seq}, applied {status.applied_seq}"
    index.upsert([_doc("b", vector=_axis(1), text="beta")], seq=9)
    status = index.status()
    problems = []
    if status.applied_seq != 9:
        problems.append(f"applied_seq {status.applied_seq} after an upsert at seq 9")
    if status.built_seq != 7:
        problems.append(f"built_seq {status.built_seq} moved without a build")
    if status.space_signature != SPACE.signature:
        problems.append(f"space_signature {status.space_signature!r}")
    if status.backend != index.name or status.state != "ready":
        problems.append(f"backend {status.backend!r}, state {status.state!r}")
    return "; ".join(problems) or None


def _check_filters(index: MemoryIndex) -> str | None:
    index.upsert(
        [
            _doc("in", vector=_axis(0), text="shared words", scope="kiro"),
            _doc("out", vector=_axis(0), text="shared words", scope="other"),
        ],
        seq=1,
    )
    for mode, query in (
        (CAP_VECTOR, IndexQuery("vector", 5, vector=_axis(0), filters={"scope": "kiro"})),
        (CAP_TERMS, IndexQuery("terms", 5, text="shared", filters={"scope": "kiro"})),
        (CAP_SUBSTRING, IndexQuery("substring", 5, text="shared", filters={"scope": "kiro"})),
    ):
        if mode in index.capabilities and set(_idents(index, query)) != {"in"}:
            return f"a filtered {mode} search returned {sorted(_idents(index, query))}"
    return None


def _check_substring(index: MemoryIndex) -> str | None:
    docs = [_doc(name, text=text) for name, text in _TEXTS]
    index.upsert(docs, seq=1)
    reference = _open(ExactIndex)
    reference.upsert(docs, seq=1)
    for text in _SUBSTRING_QUERIES:
        query = IndexQuery("substring", 50, text=text)
        want, got = set(_idents(reference, query)), set(_idents(index, query))
        if want != got:
            return f"substring {text!r}: expected {sorted(want)}, got {sorted(got)}"
    return None


def _check_terms(index: MemoryIndex) -> str | None:
    rng = np.random.default_rng(7)
    vocab = [f"t{i}" for i in range(60)] + ["common"] * 20
    docs = [
        _doc(f"d{i:03d}", text=" ".join(rng.choice(vocab, size=int(rng.integers(1, 8)))))
        for i in range(400)
    ]
    index.upsert(docs, seq=1)
    reference = _open(ExactIndex)
    reference.upsert(docs, seq=1)
    for words, need in (
        ("t1 t2 t3", 1),
        ("t1 t2 t3 common", 2),
        ("common t5 t9 t11", 3),
        ("t40", 1),
    ):
        query = IndexQuery("terms", 1000, text=words, min_match=need)
        want, got = set(_idents(reference, query)), set(_idents(index, query))
        if want != got:
            return f"terms {words!r} min_match={need}: {len(want ^ got)} rows differ from exact"
    return None


def _fixture() -> tuple[list[IndexDoc], list[bytes]]:
    rng = np.random.default_rng(20261126)
    centers = rng.normal(size=(40, DIM))
    rows = centers[rng.integers(0, 40, FIXTURE_ROWS)] + rng.normal(
        scale=0.35, size=(FIXTURE_ROWS, DIM)
    )
    docs = [_doc(f"r{i:04d}", vector=_vec(row)) for i, row in enumerate(rows)]
    queries = centers[rng.integers(0, 40, _QUERIES)] + rng.normal(scale=0.35, size=(_QUERIES, DIM))
    return docs, [_vec(q) for q in queries]


def _check_vector_quality(index: MemoryIndex) -> str | None:
    docs, queries = _fixture()
    index.upsert(docs, seq=1)
    reference = _open(ExactIndex)
    reference.upsert(docs, seq=1)
    overlap = 0.0
    for vector in queries:
        query = IndexQuery("vector", 10, vector=vector)
        overlap += len(set(_idents(reference, query)) & set(_idents(index, query))) / 10
    mean = overlap / len(queries)
    return (
        None
        if mean >= TOP10_FLOOR
        else f"top-10 overlap with exact is {mean:.3f}, under {TOP10_FLOOR}"
    )


_CHECKS: tuple[tuple[str, frozenset[str], Callable[[MemoryIndex], str | None]], ...] = (
    ("upsert_then_remove", frozenset(), _check_upsert_remove),
    ("idempotent_upsert_and_remove", frozenset(), _check_idempotent),
    ("build_then_swap", frozenset(), _check_build_swap),
    ("status_reports_seqs_and_space", frozenset(), _check_status),
    ("filters_applied", frozenset({CAP_FILTERS}), _check_filters),
    ("substring_matches_exact", frozenset({CAP_SUBSTRING}), _check_substring),
    ("terms_min_match_matches_exact", frozenset({CAP_TERMS}), _check_terms),
    ("vector_top10_overlap", frozenset({CAP_VECTOR}), _check_vector_quality),
)


def run_conformance(factory: Callable[[], MemoryIndex]) -> list[CheckResult]:
    """Run every check on fresh instances from *factory*."""
    results = []
    for name, needs, check in _CHECKS:
        index: MemoryIndex | None = None
        try:
            index = _open(factory)
            if not needs <= set(index.capabilities):
                results.append(CheckResult(name, "skip", f"needs {', '.join(sorted(needs))}"))
                continue
            if not ({CAP_VECTOR, CAP_TERMS} & set(index.capabilities)):
                results.append(
                    CheckResult(name, "fail", "a backend searches by vector or by terms")
                )
                continue
            problem = check(index)
        except Exception as exc:  # a conformance kit reports a crash as a failure
            problem = f"raised {type(exc).__name__}: {exc}"
        finally:
            if index is not None:
                index.close()
        results.append(CheckResult(name, "fail" if problem else "pass", problem or ""))
    return results
