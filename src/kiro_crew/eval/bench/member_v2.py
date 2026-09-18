"""Reproducible full member V2 retrieval evaluation with an existing local model.

Unlike admission.py this calls the shipped hybrid candidate scan, relevance
ranking, MMR, and bounded recall formatter. It reports retrieval quality, not
generated-answer quality. No provider calls, downloads, or user memory reads.

    python -m kiro_crew.eval.bench.member_v2 --model-path /existing/model.gguf \
        --json /output/member-v2-hybrid.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import struct
import tempfile
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew import memory_v2
from kiro_crew.memory_recall import (
    MAX_RECALL_PAYLOAD_BYTES,
    _transport_size,
    recall_json,
)
from kiro_crew.vector_memory import VectorMemoryStore

from .admission import validate_corpus
from .admission_corpus import ADMISSION_TOPICS

MODES = ("full_vectors", "short_vectors_missing", "no_embeddings")
CJK_CASES = (
    ("zh", "灯塔部署位置", "灯塔部署位置已经定在法兰克福机房。"),
    ("ja", "灯台デプロイ先", "灯台デプロイ先は東京リージョンです。"),
    ("ko", "등대 배포 위치", "등대 배포 위치는 서울 리전으로 확정했습니다."),
)


def ranking_metrics(ids: list[str], relevant: set[str], k: int = 8) -> dict[str, float]:
    selected = ids[:k]
    hits = sum(item in relevant for item in selected)
    dcg = sum(1 / math.log2(i + 2) for i, item in enumerate(selected) if item in relevant)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(relevant))))
    return {
        "precision_returned": hits / len(selected) if selected else 0.0,
        "fragment_recall": hits / len(relevant),
        "topic_hit": float(hits > 0),
        "ndcg_at_8": dcg / ideal,
    }


def _new_store(home: Path, name: str, dim: int) -> VectorMemoryStore:
    directory = home / "memory_stores" / name
    directory.mkdir(parents=True)
    (directory / "member-memory.json").write_text(
        json.dumps({"memory_version": 2, "owner_member": name}), encoding="utf-8"
    )
    store = VectorMemoryStore(db_path=directory / "memory.db", embedding_dim=dim)
    store.init()
    if store.algorithm_version != "v2":
        raise RuntimeError("Evaluation did not open the owned V2 policy")
    return store


def _insert(store: VectorMemoryStore, text: str, identity: str, vector: list[float] | None) -> None:
    if not store.write_episodic(text, defer_embedding=True):
        raise RuntimeError(f"Corpus ingestion refused {identity}")
    # Stable labels/timestamps make tie breaks and age weighting reproducible.
    # All quality cases intentionally have equal recency/importance; separate
    # policy tests cover old memory and importance extremes.
    blob = struct.pack(f"{len(vector)}f", *vector) if vector else None
    today = datetime.now(timezone.utc).date().isoformat() + "T00:00:00+00:00"
    updated = store.db.execute(
        "UPDATE memory_items SET id = ?, embedding = ?, created_at = ? WHERE text = ?",
        (identity, blob, today, text.strip()),
    ).rowcount
    if updated != 1:
        store.db.rollback()
        raise RuntimeError(f"Corpus identity assignment failed for {identity}")
    store.db.commit()
    store._invalidate_episodic_scoring()


def _mode_report(home: Path, mode: str, vectors: dict[str, list[float]]) -> dict:
    dim = len(next(iter(vectors.values())))
    store = _new_store(home, "eval-" + mode.replace("_", "-"), dim)
    try:
        for topic in ADMISSION_TOPICS:
            for kind in ("short", "long"):
                text = getattr(topic, kind)
                vector = (
                    vectors[text]
                    if mode == "full_vectors"
                    or (mode == "short_vectors_missing" and kind == "long")
                    else None
                )
                _insert(store, text, f"{topic.topic_id}.{kind}", vector)
        if mode != "no_embeddings":
            store.embed_fn = lambda text, **_: vectors[text]
        queries = []
        confusion = dict(tp=0, fp=0, fn=0, tn=0)
        bounds_ok = True
        for topic in ADMISSION_TOPICS:
            gold = {f"{topic.topic_id}.short", f"{topic.topic_id}.long"}
            query_vector = vectors[topic.query] if mode != "no_embeddings" else None
            admitted = store.search_episodic(
                query_vector, topic.query, limit=100, mmr=False, relevance_filter=True
            )
            admitted_ids = {row["id"] for row in admitted}
            tp = len(gold & admitted_ids)
            fp = len(admitted_ids - gold)
            for key, count in {"tp": tp, "fp": fp, "fn": 2 - tp, "tn": 98 - fp}.items():
                confusion[key] += count
            ranked = store.search_episodic(
                query_vector, topic.query, limit=8, mmr=True, relevance_filter=True
            )
            context = store.recall(topic.query, cap=3000)
            for cap in (0, 20, 150, 300, 1000, 3000):
                bounded = store.recall(topic.query, cap=cap)
                actual = sum(
                    len(bounded[key])
                    for key in ("semantic_context", "episodic_context", "lessons_context")
                )
                bounds_ok &= actual == bounded["total_chars"] and actual <= cap
            context_ids = [row["id"] for row in context["retrieval"]["episodes"]]
            evidence_ok = all(
                row["retrieval"]["admitted"]
                and f"[memory:{row['id']}]" in context["episodic_context"]
                for row in context["retrieval"]["episodes"]
            )
            if not evidence_ok:
                raise RuntimeError("Returned evidence does not describe the rendered context")
            queries.append(
                {
                    "topic": topic.topic_id,
                    "ranked": ranking_metrics([row["id"] for row in ranked], gold),
                    "context": ranking_metrics(context_ids, gold),
                    "context_chars": context["total_chars"],
                    "selected": [
                        {"id": row["id"], "evidence": row["retrieval"]}
                        for row in context["retrieval"]["episodes"]
                    ],
                }
            )
        tp, fp, fn = (confusion[key] for key in ("tp", "fp", "fn"))
        summary = {
            phase: {
                metric: sum(row[phase][metric] for row in queries) / len(queries)
                for metric in queries[0][phase]
            }
            for phase in ("ranked", "context")
        }
        return {
            "mode": mode,
            "documents": 100,
            "queries": 50,
            "pairs": 5000,
            "admission": {
                **confusion,
                "precision": tp / (tp + fp) if tp + fp else 0,
                "recall": tp / (tp + fn),
            },
            "macro_metrics": summary,
            "context_bounds_passed": bounds_ok,
            "context_max_chars": max(row["context_chars"] for row in queries),
            "per_query": queries,
        }
    finally:
        store.close()


def _bounded_oversized_result(context: dict) -> dict:
    episodes = context["retrieval"]["episodes"]
    selected_ids = [row["id"] for row in episodes]
    oversized = next((row for row in episodes if row["id"] == "oversized"), None)
    actual_chars = sum(
        len(context[key]) for key in ("semantic_context", "episodic_context", "lessons_context")
    )
    encoded_transport = recall_json(context, ensure_ascii=False, mcp_envelope=True)
    transport_bytes = _transport_size(encoded_transport, mcp_envelope=True)
    transported = json.loads(encoded_transport)
    transport_episodes = (transported.get("retrieval") or {}).get("episodes") or []
    transport_selected_ids = [row["id"] for row in transport_episodes]
    checks = {
        "selected": oversized is not None,
        "nonempty_text": bool(oversized and oversized.get("text")),
        "text_truncated": bool(oversized and oversized.get("text_truncated") is True),
        "rendered_id": "[memory:oversized]" in context["episodic_context"],
        "total_chars_matches": context["total_chars"] == actual_chars,
        "within_context_cap": actual_chars <= 200,
        "within_transport_budget": transport_bytes <= MAX_RECALL_PAYLOAD_BYTES,
        "transport_retained_id": (
            "oversized" in transport_selected_ids
            and "[memory:oversized]" in transported.get("episodic_context", "")
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "selected_ids": selected_ids,
        "selected_count": len(selected_ids),
        "total_chars": context["total_chars"],
        "actual_chars": actual_chars,
        "context_cap": 200,
        "transport_bytes": transport_bytes,
        "transport_byte_cap": MAX_RECALL_PAYLOAD_BYTES,
        "transport_selected_ids": transport_selected_ids,
    }


def _edge_report(home: Path, vectors: dict[str, list[float]]) -> dict:
    store = _new_store(home, "eval-edge-cases", len(next(iter(vectors.values()))))
    try:
        for language, query, text in CJK_CASES:
            _insert(store, text, language, None)
            store.set_semantic(f"project.{language}", text, 1.0, "user_explicit")
        results = {}
        for language, query, _ in CJK_CASES:
            context = store.recall(query, cap=1000)
            results[language] = {
                "episode_hit": language in {row["id"] for row in context["retrieval"]["episodes"]},
                "fact_hit": f"project.{language}"
                in {row["key"] for row in context["retrieval"]["facts"]},
                "within_budget": context["total_chars"] <= 1000,
            }
        _insert(store, "budgettoken memory " + "detail " * 180, "oversized", None)
        context = store.recall("budgettoken memory", cap=200)
        results["bounded_oversized_isolated"] = _bounded_oversized_result(context)

        _insert(store, "budgettoken memory fits in a small prompt.", "small", None)
        context = store.recall("budgettoken memory", cap=200)
        # The competing-record observation reports selection separately from
        # formatter retention so a false result identifies which contract moved.
        results["bounded_oversized"] = _bounded_oversized_result(context)
        store.delete_episodic("small")
        results["forgotten_excluded"] = {
            "passed": "small"
            not in {
                row["id"]
                for row in store.search_episodic(
                    query_text="budgettoken memory", relevance_filter=True
                )
            }
        }
        return results
    finally:
        store.close()


def structural_failures(report: dict) -> list[str]:
    """Fail transport, retention and deletion contracts without gating recall scores."""
    failed = []
    modes = {row["mode"]: row for row in report.get("modes", [])}
    for mode in MODES:
        if modes.get(mode, {}).get("context_bounds_passed") is not True:
            failed.append(f"{mode}: context bounds")
    edges = report.get("edge_cases", {})
    for language, _query, _text in CJK_CASES:
        for check in ("episode_hit", "fact_hit", "within_budget"):
            if edges.get(language, {}).get(check) is not True:
                failed.append(f"{language}: {check}")
    for case in ("bounded_oversized_isolated", "forgotten_excluded"):
        if edges.get(case, {}).get("passed") is not True:
            failed.append(case)
    # Which competing fragment ranks first remains reported evidence. Its
    # serialization must still honor the same structural bounds.
    competing = edges.get("bounded_oversized", {}).get("checks", {})
    for check in ("total_chars_matches", "within_context_cap", "within_transport_budget"):
        if competing.get(check) is not True:
            failed.append(f"bounded_oversized: {check}")
    return failed


def evaluate(embed: Callable[[str], list[float] | None], home: Path) -> dict:
    """Evaluate only committed public corpus; caller provides an isolated home."""
    validate_corpus()
    texts = [
        getattr(topic, field) for topic in ADMISSION_TOPICS for field in ("query", "short", "long")
    ]
    vectors = {}
    for index, text in enumerate(texts):
        vector = embed(text)
        if not vector or not all(math.isfinite(value) for value in vector):
            raise RuntimeError(f"Model failed to embed corpus input {index}")
        vectors[text] = vector
    if len({len(vector) for vector in vectors.values()}) != 1:
        raise RuntimeError("Embedding dimensions changed during evaluation")
    corpus = json.dumps(
        [asdict(topic) for topic in ADMISSION_TOPICS], ensure_ascii=False, sort_keys=True
    )
    return {
        "schema_version": 1,
        "policy_revision": memory_v2.ALGORITHM_VERSION,
        "corpus_sha256": hashlib.sha256(corpus.encode()).hexdigest(),
        "protocol": "50 committed topics; short+long relevant to own query, all98 other fragments labelled irrelevant; real SQLite V2 admission/rank/MMR/recall; equal age and importance; six context caps per query",
        "limitations": [
            "Corpus-informed operating point, not held-out calibration or a production quality guarantee.",
            "No answer generation evaluated; topic_hit means either of two equivalent fragments survived.",
            "No-embedding and partial-vector runs measure graceful degradation, not semantic model quality.",
            "CJK and budget fixtures are explicit structural checks, not a multilingual benchmark.",
        ],
        "modes": [_mode_report(home, mode, vectors) for mode in MODES],
        "edge_cases": _edge_report(home, vectors),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", required=True, type=Path)
    parser.add_argument("--json", required=True, type=Path)
    args = parser.parse_args()
    if not args.model_path.is_file():
        parser.error("An existing local GGUF model is required; downloads are disabled")
    previous = {
        key: os.environ.get(key) for key in ("KIROCREW_HOME", "KIROCREW_SKIP_MODEL_DOWNLOAD")
    }
    try:
        with tempfile.TemporaryDirectory(prefix="member-v2-hybrid-") as temporary:
            os.environ["KIROCREW_HOME"] = temporary
            os.environ["KIROCREW_SKIP_MODEL_DOWNLOAD"] = "1"
            from kiro_crew.embeddings import LlamaCppEmbedder

            backend = LlamaCppEmbedder(model_path=args.model_path)
            try:
                if not backend.wait_ready(timeout=60):
                    raise RuntimeError("Existing local model could not initialize")
                report = evaluate(backend.embed, Path(temporary))
                with args.model_path.open("rb") as handle:
                    model_hash = hashlib.file_digest(handle, "sha256").hexdigest()
                report["model"] = {
                    "model_id": backend.model_id,
                    "sha256": model_hash,
                    "dimension": backend.dim,
                }
            finally:
                backend.close()
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    mode["mode"]: {
                        "admission": mode["admission"],
                        "quality": mode["macro_metrics"],
                        "bounds": mode["context_bounds_passed"],
                    }
                    for mode in report["modes"]
                },
                indent=2,
            )
        )
        print(json.dumps(report["edge_cases"], ensure_ascii=False))
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    main()
