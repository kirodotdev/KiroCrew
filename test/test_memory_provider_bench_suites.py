"""Each benchmark suite's loader, on small rows in its upstream shape.

The fetch step is replaced by the rows themselves, so each test checks what a loader
does with its data: the variant and limit it honours, the units it builds, and the
refusals it raises (an unknown variant, a missing reader, a suite the forget probe
cannot sit on). Fetching and hash-pinning are covered by the dataset tests.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.eval.bench.errors import BenchRefusal
from kiro_crew.eval.bench.suites import beam, forget, halumem, mab, personamem, precisionmem, public

BEAM_CHAT = [
    {
        "batch_number": 1,
        "turns": [
            [
                {"role": "user", "id": 0, "time_anchor": "Mar-15-2024", "content": "Sprint ends."},
                {"role": "assistant", "id": 1, "time_anchor": "bad", "content": "Noted."},
            ]
        ],
    }
]
BEAM_QUESTIONS = {
    "information_extraction": [{"question": "When?", "answer": "March", "source_chat_ids": [0]}]
}


def test_beam_loads_one_unit_per_conversation_up_to_the_limit(monkeypatch) -> None:
    def fake(spec, *, allow_download):
        return BEAM_QUESTIONS if "questions" in str(spec) else BEAM_CHAT

    monkeypatch.setattr(beam, "load_json", fake)
    monkeypatch.setattr(beam, "beam_spec", lambda variant, n, part: f"{variant}/{n}/{part}")
    corpus = beam.load("100K", limit=2)
    assert [u.unit_id for u in corpus.units] == ["beam-100K-1", "beam-100K-2"]
    assert corpus.units[0].items[0].at is not None and corpus.units[0].items[1].at is None
    with pytest.raises(ValueError):
        beam.load("nope")


def test_halumem_reads_one_user_per_line_up_to_the_limit(monkeypatch) -> None:
    user = {
        "uuid": "u",
        "sessions": [
            {
                "dialogue": [{"role": "user", "content": "I am Ana.", "timestamp": "never"}],
                "memory_points": [{"memory_content": "User's name is Ana"}],
                "questions": [{"question": "Name?", "answer": "Ana", "evidence": [1]}],
            }
        ],
    }
    text = "\n".join([json.dumps(user), "", json.dumps({**user, "uuid": "v"})])
    monkeypatch.setattr(halumem, "load_text", lambda spec, *, allow_download: text)
    assert len(halumem.load(limit=1).units) == 1
    assert len(halumem.load().units) == 2


def test_mab_needs_pyarrow_and_keeps_only_the_wanted_source(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_pyarrow(name, *a, **k):
        if name.startswith("pyarrow"):
            raise ImportError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_pyarrow)
    with pytest.raises(BenchRefusal):
        mab._rows("mab_conflict_resolution", allow_download=False)
    monkeypatch.setattr(builtins, "__import__", real_import)

    row = {
        "context": "Facts:\n0. A is in X.\n1. A is in Y.",
        "questions": ["Where is A?"],
        "answers": [["Y"]],
    }
    base, prefix = "fc_sh", mab.VARIANTS["fc_sh"][1]
    wanted = prefix + mab.DEFAULT_SIZE[base]
    rows = [
        {**row, "metadata": {"source": wanted}},
        {**row, "metadata": {"source": "other"}},
    ]
    monkeypatch.setattr(mab, "_rows", lambda spec_key, *, allow_download: rows)
    assert len(mab.load(base).units) == 1
    with pytest.raises(ValueError):
        mab.load("nope")


def test_personamem_groups_rows_by_history_and_reads_each_history(monkeypatch) -> None:
    csv_text = (
        "chat_history_32k_link,user_query,correct_answer,incorrect_answers\n"
        "https://h/a.json,\"{'role': 'user', 'content': 'Ideas?'}\",Yoga,\"['Run']\"\n"
        "https://h/b.json,not a literal,Tea,\"['Coffee']\"\n"
    )
    history = {"chat_history": [{"role": "user", "content": "I do yoga."}]}
    monkeypatch.setattr(personamem, "load_text", lambda spec, *, allow_download: csv_text)
    monkeypatch.setattr(personamem, "load_json", lambda spec, *, allow_download: history)
    monkeypatch.setattr(personamem, "personamem_history_spec", lambda link: link)
    corpus = personamem.load(limit=1)
    assert len(corpus.units) == 1 and corpus.units[0].queries
    assert len(personamem.load().units) == 2


def test_precisionmem_loads_one_unit_and_cuts_queries_to_the_limit(monkeypatch) -> None:
    seed = [
        {"_id": "b1", "content": "Likes tea.", "created_at": "2025-01-01T00:00:00Z"},
        {"_id": "b2", "content": "Uses Postgres.", "created_at": "not a date"},
        "junk",
    ]
    cases = [
        {"caseId": "c1", "query": "drinks", "expect": {"pinnedFacts": {"mustInclude": ["b1"]}}},
        {"caseId": "c2", "query": "db", "expect": {"pinnedFacts": {"mustInclude": ["b2"]}}},
    ]
    monkeypatch.setattr(
        precisionmem,
        "load_json",
        lambda spec, *, allow_download: (
            seed if spec is precisionmem.SUITE_SPECS["precisionmem_seed"] else cases
        ),
    )
    full = precisionmem.load()
    assert [i.item_id for i in full.units[0].items] == ["b1", "b2"]
    assert len(precisionmem.load(limit=1).units[0].queries) == 1


def test_the_public_suites_and_forget_load_through_their_adapters(monkeypatch) -> None:
    from kiro_crew.eval.bench.item_corpus import ItemCorpus

    seen: list = []
    monkeypatch.setattr(public, "load_json", lambda key, *, allow_download: seen.append(key) or {})
    monkeypatch.setattr(public, "load_locomo", lambda raw: "locomo")
    monkeypatch.setattr(public, "load_longmemeval", lambda raw, variant: "lme")
    monkeypatch.setattr(
        public, "from_corpus", lambda corpus, limit=None: ItemCorpus(corpus, "v", ())
    )
    assert public._locomo().suite == "locomo"
    assert public._longmemeval("s").suite == "lme" and public._longmemeval("oracle")
    assert seen == ["locomo10", "longmemeval_s", "longmemeval_oracle"]
    built = forget.load("locomo", limit=1)
    assert built.suite == "forget"
    with pytest.raises(ValueError):
        forget.load("isolation")
