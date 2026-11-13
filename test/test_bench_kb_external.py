"""Tests for the externally anchored KB golden set (``bench kb-retrieval --external``).

Offline: a tiny BEIR-shaped task stands in for NFCorpus, so nothing here downloads.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from kiro_crew.cli_bench import bench_cmd
from kiro_crew.eval.bench import datasets, kb_external


class _Args:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


def _write_beir(root: Path) -> tuple[Path, Path, Path]:
    docs = [{"_id": f"D{i}", "title": f"title {i}", "text": f"paper {i} text"} for i in range(1, 9)]
    queries = [{"_id": f"Q{i}", "text": f"question {i}"} for i in range(1, 6)]
    qrels = [
        ("Q1", "D1", 2),
        ("Q1", "D2", 1),
        ("Q2", "D2", 2),  # shares D2 with Q1, so Q2's other gold (D3) is a rival for Q1
        ("Q2", "D3", 1),
        ("Q3", "D4", 2),  # single-gold: never sampled
        ("Q4", "D5", 1),
        ("Q4", "D6", 1),
        ("Q4", "D8", 0),  # judged NOT relevant: never gold
    ]
    paths = (root / "corpus.jsonl.gz", root / "queries.jsonl.gz", root / "test.tsv")
    for path, rows in zip(paths[:2], (docs, queries)):
        path.write_bytes(gzip.compress("".join(json.dumps(r) + "\n" for r in rows).encode()))
    lines = ["query-id\tcorpus-id\tscore", *(f"{q}\t{d}\t{s}" for q, d, s in qrels)]
    paths[2].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return paths


def test_sampler_builds_a_reproducible_valid_set_with_provenance(tmp_path: Path) -> None:
    corpus, queries, qrels = kb_external.read_beir(*_write_beir(tmp_path))

    manifest = kb_external.sample_manifest(queries, qrels, seed=7)

    assert manifest == kb_external.sample_manifest(queries, qrels, seed=7)
    assert manifest.keys() >= {"name", "benchmark", "revisions", "seed"}
    assert manifest["seed"] == 7
    assert manifest["query_ids"] == ["Q1", "Q2", "Q4"]
    assert manifest["doc_ids"] == ["D1", "D2", "D3", "D5", "D6"]
    golden = kb_external.build_golden(manifest, corpus, queries, qrels)
    assert {q.id: q.gold_doc_ids for q in golden.queries} == {
        "Q1": ("D1", "D2"),
        "Q2": ("D2", "D3"),
        "Q4": ("D5", "D6"),
    }


def test_committed_manifest_records_the_pinned_sources() -> None:
    manifest = json.loads(kb_external.manifest_path().read_text(encoding="utf-8"))

    assert manifest["name"] == kb_external.NAME
    assert manifest["seed"] == kb_external.SEED
    for revision in manifest["revisions"].values():
        assert any(revision in spec.url for spec in kb_external.SPECS)
    assert len(set(manifest["query_ids"])) == kb_external.N_QUERIES
    assert manifest["doc_ids"] == sorted(set(manifest["doc_ids"]))


def test_external_flag_prints_the_external_report_after_the_authored_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus, queries, qrels = kb_external.read_beir(*_write_beir(tmp_path))
    golden = kb_external.build_golden(
        kb_external.sample_manifest(queries, qrels), corpus, queries, qrels
    )
    monkeypatch.setattr(kb_external, "load_external_golden", lambda: golden)

    rc = bench_cmd(
        _Args(
            bench_action="kb-retrieval",
            golden=None,
            k=3,
            real_embedder=False,
            no_embeddings=True,
            external=True,
        )
    )

    assert rc == 0
    out = capsys.readouterr().out
    authored = out.index("KB retrieval eval: kb_golden_v2")
    external = out.index(f"KB retrieval eval: {kb_external.NAME}")
    assert external > authored
    assert "abstention_rate" in out[authored:external]
    assert "abstention_rate" not in out[external:]  # no abstention queries, no rate


def test_external_fetch_failure_refuses_before_any_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def offline() -> None:
        raise datasets.CorpusFetchError("network error fetching the corpus")

    monkeypatch.setattr(kb_external, "load_external_golden", offline)

    rc = bench_cmd(
        _Args(
            bench_action="kb-retrieval",
            golden=None,
            k=3,
            real_embedder=False,
            no_embeddings=True,
            external=True,
        )
    )

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "network error" in captured.err
